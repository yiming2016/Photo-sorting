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
from PySide6.QtGui import QPixmap

from .exiftool import ExifTool

THUMB_SIZE = 360
EMBEDDED_TAGS = ("ThumbnailImage", "PreviewImage", "JpgFromRaw", "OtherImage",
                 "PhotoshopThumbnail", "ThumbnailTIFF", "CoverArt")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


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


def _decode_with_ffmpeg(path: str, dest: str) -> bool:
    import shutil
    exe = shutil.which("ffmpeg")
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
    """按顺序生成缩略图, 支持把指定路径提到队首。"""

    thumbReady = Signal(str, QPixmap)
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

    def _make(self, path: str) -> QPixmap | None:
        dest = os.path.join(cache_dir(), cache_key(path) + ".jpg")
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            pix = QPixmap(dest)
            if not pix.isNull():
                return pix
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
            return QPixmap(dest)
        return None
