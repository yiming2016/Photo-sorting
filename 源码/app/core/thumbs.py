# -*- coding: utf-8 -*-
"""缩略图生成: 内嵌缩略图优先, 其次 Pillow 解码, 视频交给 ffmpeg。

单线程 + 优先级队列: 界面滚动时把可见区域提到队首, 体验上像是"边看边加载"。
磁盘缓存按 路径+修改时间+大小 做键, 文件变了会自动重新生成。
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
from typing import Iterable

from PySide6.QtCore import QThread, QMutex, Signal
from PySide6.QtGui import QImage, QPixmap

from .exiftool import ExifTool

THUMB_SIZE = 360
VIDEO_EXTS = {"mp4", "mov", "m4v", "avi", "mkv", "webm", "3gp", "3g2", "mts", "m2ts"}
EMBEDDED_TAGS = ("ThumbnailImage", "PreviewImage", "JpgFromRaw", "OtherImage",
                 "PhotoshopThumbnail", "ThumbnailTIFF", "CoverArt")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def thumb_pixmap(path: str) -> QPixmap:
    """同步取一张缩略图(鼠标悬浮预览用)。

    顺序: 磁盘缓存 → QPixmap 直读(顺手落缓存) → Pillow/ffmpeg 解码落缓存。
    失败时返回空 QPixmap(不是 None), 调用方用 isNull() 判断即可。
    """
    if not path or not os.path.exists(path):
        return QPixmap()
    try:
        dest = os.path.join(cache_dir(), cache_key(path) + ".jpg")
    except OSError:
        dest = ""
    if dest and os.path.exists(dest) and os.path.getsize(dest) > 0:
        pix = QPixmap(dest)
        if not pix.isNull():
            return pix
    pix = QPixmap(path)
    if not pix.isNull():
        if dest:
            try:
                pix.save(dest, "JPEG", 86)
            except Exception:
                pass
        return pix
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    ok = _decode_with_ffmpeg(path, dest) if ext in VIDEO_EXTS else _decode_with_pillow(path, dest)
    if ok and dest and os.path.exists(dest):
        return QPixmap(dest)
    return QPixmap()


def cache_dir() -> str:
    base = os.environ.get("PHOTOSORT_HOME") or os.path.join(
        os.path.expanduser("~"), ".photosort")
    path = os.path.join(base, "cache", "thumbs")
    os.makedirs(path, exist_ok=True)
    return path


def cache_key(path: str) -> str:
    try:
        st = os.stat(path)
        raw = f"{os.path.normcase(os.path.abspath(path))}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        raw = path
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def _save_jpeg(data: bytes, dest: str) -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return False
    try:
        if hasattr(im, "n_frames") and im.n_frames > 1:
            im.seek(0)
        im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
        w, h = im.size
        if max(w, h) > THUMB_SIZE:
            scale = THUMB_SIZE / max(w, h)
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        im.save(dest, "JPEG", quality=86, optimize=True)
        return True
    except Exception:
        return False


def _decode_with_pillow(path: str, dest: str) -> bool:
    try:
        import pillow_heif  # noqa
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > THUMB_SIZE:
                scale = THUMB_SIZE / max(w, h)
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            im.save(dest, "JPEG", quality=86, optimize=True)
        return True
    except Exception:
        return False


_FFMPEG_CACHE = []


def _ffmpeg_exe():
    """取 ffmpeg 路径: 先 PATH, 再退回合并模块的多路查找(winget 安装等)。

    视频缩略图靠它, 所以不能只认 PATH —— 否则在本机(ffmpeg 装在 winget 目录、
    没进 PATH)视频格子全变成"视频"占位文字。
    """
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE[0]
    import shutil
    exe = shutil.which("ffmpeg")
    if not exe:
        try:
            from ..merge.ffmpeg_locate import find_ffmpeg_pair
            pair = find_ffmpeg_pair()
            exe = pair[0] if pair else None
        except Exception:
            exe = None
    if exe:
        _FFMPEG_CACHE.append(exe)
    return exe


def _decode_with_ffmpeg(path: str, dest: str) -> bool:
    exe = _ffmpeg_exe()
    if not exe:
        return False
    for seek in ("0.5", "0"):
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-ss", seek, "-i", path,
               "-frames:v", "1", "-vf", f"scale={THUMB_SIZE}:-2", "-y", dest]
        kwargs = {}
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = si
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            subprocess.run(cmd, timeout=25, **kwargs)
        except Exception:
            continue
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return True
    return False


class ThumbWorker(QThread):
    """按顺序生成缩略图, 支持把指定路径提到队首。

    ⚠️ 信号里发 **QImage**: 这个线程生成的是跨线程对象, QPixmap 不能在工作线程
    创建/传递(Qt 明确规定只能界面线程用)。界面线程收到后 `QPixmap.fromImage()`。
    """

    thumbReady = Signal(str, QImage)
    thumbFailed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: list[str] = []
        self._seen: set[str] = set()
        self._mutex = QMutex()
        self._stop = False
        self._et: ExifTool | None = None

    def stop(self):
        self._stop = True
        self._mutex.lock()
        self._queue.clear()
        self._mutex.unlock()

    def clear(self):
        self._mutex.lock()
        self._queue.clear()
        self._seen.clear()
        self._mutex.unlock()

    def request(self, paths: Iterable[str], front: bool = True):
        self._mutex.lock()
        fresh = [p for p in paths if p not in self._seen]
        for p in fresh:
            self._seen.add(p)
        if front:
            self._queue = fresh + self._queue
        else:
            self._queue.extend(fresh)
        self._mutex.unlock()

    def _pop(self) -> str | None:
        self._mutex.lock()
        path = self._queue.pop(0) if self._queue else None
        self._mutex.unlock()
        return path

    def run(self):
        try:
            self._et = ExifTool()
            self._et.start()
        except Exception:
            self._et = None
        while not self._stop:
            path = self._pop()
            if path is None:
                self.msleep(120)
                continue
            pix = self._make(path)
            if pix is None or pix.isNull():
                self.thumbFailed.emit(path)
            else:
                self.thumbReady.emit(path, pix)
        if self._et:
            self._et.close()

    def _make(self, path: str) -> QImage | None:
        dest = os.path.join(cache_dir(), cache_key(path) + ".jpg")
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            img = QImage(dest)
            if not img.isNull():
                return img
        ok = False
        if self._et is not None:
            for tag in EMBEDDED_TAGS:
                try:
                    data = self._et.extract_binary(tag, path)
                except Exception:
                    data = b""
                if len(data) > 2048 and data[:3] in (b"\xff\xd8\xff", b"\x89PN"):
                    if _save_jpeg(data, dest):
                        ok = True
                        break
        if not ok:
            kind = "video" if os.path.splitext(path)[1].lower().lstrip(".") in {
                "mp4", "mov", "m4v", "avi", "mkv", "webm", "3gp", "3g2", "mts", "m2ts",
            } else "image"
            ok = _decode_with_ffmpeg(path, dest) if kind == "video" else _decode_with_pillow(path, dest)
        if ok and os.path.exists(dest):
            img = QImage(dest)
            return img if not img.isNull() else None
        return None
