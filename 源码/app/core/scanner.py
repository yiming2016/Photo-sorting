# -*- coding: utf-8 -*-
"""文件夹扫描: 收集文件 → 批量读取元数据 → 构造 Photo 列表。"""
from __future__ import annotations

import os
from typing import Iterable

from PySide6.QtCore import QThread, Signal

from .exiftool import ExifTool
from .photo import Photo, kind_of

CHUNK = 40


def _work_subdir() -> str:
    """统一格式副本目录名(扫描必须跳过它)。"""
    try:
        from ..merge.norm_paths import WORK_SUBDIR
        return WORK_SUBDIR
    except Exception:
        return "_photosort_norm"


def collect_files(folder: str, recursive: bool = False) -> list[str]:
    out: list[str] = []
    work = _work_subdir()
    if recursive:
        for root, dirs, files in os.walk(folder):
            # v217: 跳过 _photosort_norm —— 否则副本会被当成新视频加进列表
            # (再「统一」还会报"不是视频或源文件已不存在")
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and d != work]
            for f in files:
                if f.startswith("."):
                    continue
                out.append(os.path.join(root, f))
    else:
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_file():
                        out.append(entry.path)
        except OSError:
            return []
    # 兜一层: 任何落在副本目录里的文件都不算素材(直接打开该目录也适用)
    keep = [p for p in out
            if kind_of(p) != "other"
            and work not in p.replace("\\", "/").split("/")[:-1]]
    keep.sort(key=lambda p: os.path.basename(p).lower())
    return keep


class Scanner(QThread):
    """后台扫描线程, 独占一个 exiftool 进程。"""

    progress = Signal(int, int, str)
    finished_scan = Signal(list)
    failed = Signal(str)

    def __init__(self, folder: str, files: list[str], parent=None):
        super().__init__(parent)
        self.folder = folder
        self.files = files
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        et = None
        try:
            et = ExifTool()
            et.start()
        except Exception as exc:
            self.failed.emit(f"无法启动 exiftool: {exc}")
            self.finished_scan.emit([])
            return

        total = len(self.files)
        photos: list[Photo] = []
        try:
            for i in range(0, total, CHUNK):
                if self._cancel:
                    break
                batch = self.files[i:i + CHUNK]
                try:
                    records = et.read_json(batch)
                except Exception:
                    records = []
                by_src = {}
                for rec in records:
                    src = rec.pop("SourceFile", None)
                    if src:
                        by_src[os.path.normcase(os.path.abspath(src))] = rec
                for path in batch:
                    key = os.path.normcase(os.path.abspath(path))
                    meta = by_src.get(key)
                    if meta is None:
                        meta = {}
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        size = 0
                    photos.append(Photo(
                        path=os.path.normpath(path),
                        name=os.path.basename(path),
                        size=size,
                        kind=kind_of(path),
                        meta=meta,
                    ))
                self.progress.emit(min(i + CHUNK, total), total, os.path.basename(batch[-1]))
            photos.sort(key=lambda p: p.sort_dt(), reverse=True)
        finally:
            et.close()
        self.finished_scan.emit(photos)
