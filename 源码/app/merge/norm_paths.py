# -*- coding: utf-8 -*-
"""统一格式工作副本的路径规则(轻量、无 Qt、无 ffmpeg 依赖)。

设计要点:
- 主列表/元数据写入永远用**原文件路径**, 这里只给 ffmpeg 边界(probe / merge / export)
  提供"实际该读哪个文件"的解析。
- 副本放在源文件同级的 `_photosort_norm/`, 文件名 = 参数档 + 源文件指纹。
  源文件改了(size/mtime)或参数档改了, 指纹就变, 老副本自然失效, 不需要维护映射表。
- 程序重启后仍可重算得到同一个路径, 所以不依赖内存状态。
"""
from __future__ import annotations

import hashlib
import os

WORK_SUBDIR = "_photosort_norm"
PREFIX = "_ns_"

VIDEO_EXT = {
    ".mp4", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".flv", ".webm",
    ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".3g2",
}


def is_work_copy(path: str) -> bool:
    """这个路径是不是我们生成的工作副本本身(避免二次解析)。"""
    if not path:
        return False
    p = os.path.normpath(path)
    parent = os.path.basename(os.path.dirname(p))
    return parent == WORK_SUBDIR and os.path.basename(p).startswith(PREFIX)


def is_video(path: str) -> bool:
    try:
        return os.path.splitext(str(path or ""))[1].lower() in VIDEO_EXT
    except Exception:
        return False


def profile_key(profile=None) -> str:
    """参数档指纹。profile 可以是 NormProfile / dict / 已是 key 的字符串。

    传 None 时用**当前正在用**的档(settings.normalize_profile_current),
    而不是"保存为默认"的档 —— 否则用非默认档生成的副本永远解析不到。
    """
    if isinstance(profile, str) and profile:
        return profile
    if profile is None:
        from . import settings
        profile = settings.load_normalize_profile_current()
    if hasattr(profile, "key"):
        try:
            return str(profile.key())
        except Exception:
            pass
    try:
        codec = str(profile.get("codec", "main"))
        crf = int(profile.get("crf", 20))
        keyint = int(profile.get("keyint_sec", 2))
        audio = int(profile.get("audio_bitrate", 192))
    except Exception:
        codec, crf, keyint, audio = "main", 20, 2, 192
    return f"{codec}|{crf}|{keyint}|{audio}"


def work_path(path: str, profile=None):
    """算出源文件对应的工作副本路径; 不是视频/源文件不存在时返回 None。"""
    if not path or not is_video(path) or is_work_copy(path):
        return None
    try:
        abs_path = os.path.abspath(path)
        st = os.stat(abs_path)
    except OSError:
        return None
    raw = "|".join((
        os.path.normcase(abs_path),
        str(st.st_size),
        _content_tag(abs_path, st),      # v217: 用内容标识, 不再用 mtime
        profile_key(profile),
    ))
    digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]
    codec = "m10" if str(profile_key(profile)).startswith("main10") else "m8"
    # v234: 用户在「统一」页选了副本输出根目录时, 副本落到该根下的 _photosort_norm/,
    # 否则维持"源视频同级"(默认行为不变)。根目录只影响**放哪**, 不影响文件名/指纹
    # (指纹仍由源路径+内容+档位算), 所以 resolve/probe/merge/export 调 work_path 时
    # 自动读到同一个根, 不会出现"写在一处、读另一处"。
    root = None
    try:
        from . import settings as _st
        root = _st.load_normalize_out_dir()
    except Exception:
        pass
    base = root if root and os.path.isdir(root) else os.path.dirname(abs_path)
    return os.path.join(base, WORK_SUBDIR, f"{PREFIX}{codec}_{digest}.mp4")


# ---------------------------------------------------------------- 内容指纹
# 为什么不用 mtime(v217): 写完时间戳/设封面/改帧率之后 mtime 就变了, 指纹跟着变,
# 昨天生成的副本立刻变成"孤儿"(几十 GB, 又没有任何入口清理); 而副本里装的是
# 视频流, 元数据改动根本不影响它。改成"大小 + 首尾各 64KB 的内容哈希":
# 内容没动就不变, 真换了片子(大小或头尾变了)才失效。
_TAG_CACHE: dict = {}
_TAG_CACHE_MAX = 4096
_TAG_CHUNK = 65536


def _content_tag(abs_path: str, st) -> str:
    key = (os.path.normcase(abs_path), int(st.st_size),
           int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))))
    hit = _TAG_CACHE.get(key)
    if hit is not None:
        return hit
    h = hashlib.sha1()
    h.update(str(int(st.st_size)).encode("ascii"))
    try:
        with open(abs_path, "rb") as f:
            h.update(f.read(_TAG_CHUNK))
            if st.st_size > _TAG_CHUNK * 2:
                f.seek(-_TAG_CHUNK, os.SEEK_END)
                h.update(f.read(_TAG_CHUNK))
    except OSError:
        # 读不了内容(被占用/权限) → 退回 mtime, 至少不会算成"同一个文件"
        h.update(str(key[2]).encode("ascii"))
    tag = h.hexdigest()[:20]
    if len(_TAG_CACHE) >= _TAG_CACHE_MAX:
        _TAG_CACHE.clear()
    _TAG_CACHE[key] = tag
    return tag


def resolve(path: str, profile=None) -> str:
    """ffmpeg 边界用: 有可用工作副本就返回副本路径, 否则原样返回。

    v222: 统一改成非模态后, 转码和合并/导出可能同时进行 —— 副本写到一半时
    size 早就 > 0, 单看大小会把半成品当有效副本拼进成片。所以转码中的源文件
    在这里一律回退用原片(合并/导出拿到的至少是完整文件), 见 mark_writing()。
    """
    if not path or is_work_copy(path):
        return path
    try:
        key = os.path.normcase(os.path.abspath(path))
    except Exception:
        key = path
    if key in _WRITING:
        return path
    candidate = work_path(path, profile)
    if not candidate:
        return path
    try:
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    except OSError:
        pass
    return path


# v222: 正在被统一转码改写的源文件(normcase 绝对路径)。convert_one 标记,
# 转码结束(含失败/取消)解除; resolve 看到就回退原片, 防止拼进半成品副本。
_WRITING: set = set()


def mark_writing(path: str):
    try:
        _WRITING.add(os.path.normcase(os.path.abspath(path)))
    except Exception:
        pass


def unmark_writing(path: str):
    try:
        _WRITING.discard(os.path.normcase(os.path.abspath(path)))
    except Exception:
        pass


def is_normalized(path: str, profile=None) -> bool:
    """这个源文件当前有没有生效中的工作副本。"""
    return resolve(path, profile) != path


def ensure_dir(path: str) -> str:
    folder = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    return folder


# ---------------------------------------------------------------- 副本清理
def find_work_copies(folder: str, recursive: bool = True) -> list[str]:
    """列出 folder 下所有统一副本(含中断留下的 .part / .stderr)。

    recursive=True 时连同各级子文件夹里的副本一起找(源视频可能在深层目录里)。
    """
    out: list[str] = []
    folder = os.path.abspath(folder or ".")
    if not os.path.isdir(folder):
        return out

    def _scan(d: str):
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_file() and is_work_copy(entry.path):
                            # v256: 只收我们自己生成的 _ns_ 副本(含 .part /
                            # .stderr.txt 中间产物, 它们也带 _ns_ 前缀)。之前
                            # 不过滤 → 用户手动放进 _photosort_norm 的文件被
                            # "清理副本"一起删掉, 且 os.remove 不进回收站。
                            out.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            pass

    _scan(os.path.join(folder, WORK_SUBDIR))
    if not recursive:
        return out
    for root, dirs, _files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != WORK_SUBDIR]
        _scan(os.path.join(root, WORK_SUBDIR))
    return out


def work_copies_size(paths) -> int:
    total = 0
    for p in paths or ():
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def remove_work_copies(paths) -> tuple[int, int, list]:
    """删副本。返回 (删掉的文件数, 释放的字节数, 失败列表)。"""
    n, freed, failed = 0, 0, []
    for p in paths or ():
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        try:
            os.remove(p)
            n += 1
            freed += size
        except OSError as e:
            failed.append((p, str(e)))
    # 顺手把空掉的 _photosort_norm 目录删掉(非空则保留)
    for parent in {os.path.dirname(p) for p in paths or ()}:
        try:
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
        except OSError:
            pass
    return n, freed, failed
