# -*- coding: utf-8 -*-
"""文件夹扫描: 收集文件 → 批量读取元数据 → 构造 Photo 列表。"""
from __future__ import annotations

import os
from typing import Iterable

from PySide6.QtCore import QThread, Signal

from .exiftool import ExifTool
from .photo import Photo, kind_of

CHUNK = 40


def collect_files(folder: str, recursive: bool = False) -> list[str]:
    out: list[str] = []
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
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
    keep = [p for p in out if kind_of(p) != "other"]
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
