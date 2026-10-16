# -*- coding: utf-8 -*-
"""写入执行: 变更清单 → 快照 → 写入 → 校验。

唯一会动硬盘的一层。任何写入之前必定先落快照, 任何写入之后必定读回比对。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from PySide6.QtCore import QThread, Signal

from . import backup, fields as F
from .exiftool import ExifTool
from .photo import Photo, STATUS_DONE, STATUS_ERROR, STATUS_NORMAL

LEVEL_INFO, LEVEL_WARN, LEVEL_ERROR = 0, 1, 2


@dataclass
class Change:
    path: str
    key: str
    label: str
    old: str | None
    new: str
    section: str = "时间"


@dataclass
class WriteResult:
    snapshot_id: str = ""
    total_files: int = 0
    total_changes: int = 0
    ok_files: list = field(default_factory=list)
    failed: list = field(default_factory=list)      # [(path, msg)]
    mismatches: list = field(default_factory=list)  # [(path, key, 期望, 实际)]
    meta_updates: dict = field(default_factory=dict)  # path -> 写入后的完整元数据


def should_write(photo: Photo, target: str, fill_xmp: bool, fill_qt: bool) -> bool:
    """判断某个目标字段是否该写入该文件。

    原则:
    - 文件系统时间一律写;
    - 文件里已经有的字段一律写;
    - 没有的字段按类型补, 但不跨类型硬塞(EXIF 不塞进视频, QuickTime 不塞进图片);
    - fill_xmp / fill_qt 控制是否给没有 XMP / QuickTime 结构的文件新建。
    """
    if target in F.FILE_TIME_KEYS:
        return True
    if target in photo.meta:
        return True
    group = target.split(":", 1)[0].lower()
    if photo.kind == "video":
        if group.startswith(("quicktime", "track", "media", "keys", "itemlist", "userdata")):
            return True
        if group.startswith("xmp"):
            return fill_xmp
        if group.startswith(("exif", "ifd")):
            return False
        return False
    # 图片
    if group.startswith(("exif", "ifd")):
        return target in ("EXIF:DateTimeOriginal", "EXIF:CreateDate", "EXIF:ModifyDate")
    if group.startswith("xmp"):
        return fill_xmp
    if group.startswith(("quicktime", "track", "media")):
        return False
    return False


def build_changes(photos: list[Photo], thorough: bool = False,
                  fill_xmp: bool = False, fill_qt: bool = False) -> list[Change]:
    changes: list[Change] = []
    for photo in photos:
        touched: set[str] = set()
        taken_dt = photo.planned.get(F.TAKEN)
        for sf in (F.TAKEN, F.CREATED, F.MODIFIED):
            dt = photo.planned.get(sf)
            if dt is None:
                continue
            for target in F.WRITE_TARGETS[sf]:
                if target in touched:
                    continue
                if not should_write(photo, target, fill_xmp, fill_qt):
                    continue
                touched.add(target)
                old = photo.meta.get(target)
                new = F.format_for(target, dt, old)
                if old is not None and F.strip_tz(str(old)) == F.strip_tz(new):
                    continue
                changes.append(Change(photo.path, target, F.SYNTHETIC_LABEL[sf],
                                      old, new, "时间"))
        if thorough and taken_dt is not None:
            for key, val in photo.meta.items():
                if key in touched or not F.is_datetime_value(val):
                    continue
                if not F.writable(key):
                    continue
                touched.add(key)
                new = F.format_for(key, taken_dt, val)
                if F.strip_tz(val) == F.strip_tz(new):
                    continue
                changes.append(Change(photo.path, key, key.split(":", 1)[-1], val, new, "时间"))
        for key, val in photo.pending.items():
            if key in touched:
                continue
            touched.add(key)
            old = photo.meta.get(key)
            if old is not None and str(old) == str(val):
                continue
            changes.append(Change(photo.path, key, key.split(":", 1)[-1],
                                  old, str(val), "属性"))
    return changes


def group_by_file(changes: list[Change]) -> dict[str, list[Change]]:
    out: dict[str, list[Change]] = {}
    for c in changes:
        out.setdefault(c.path, []).append(c)
    return out


def sort_assignments(assignments: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """文件系统时间必须放最后, 否则会被元数据写入刷新掉。"""
    normal = [a for a in assignments if a[0] not in F.FILE_TIME_KEYS]
    file_times = [a for a in assignments if a[0] in F.FILE_TIME_KEYS]
    return normal + file_times


class WriteWorker(QThread):
    progress = Signal(int, int, str)
    log = Signal(str, int)
    finished_write = Signal(object)

    def __init__(self, changes: list[Change], folder: str = "", in_place: bool = False,
                 parent=None):
        super().__init__(parent)
        self.changes = changes
        self.folder = folder
        self.in_place = in_place
        self._cancel = False
        self.result = WriteResult()

    def cancel(self):
        self._cancel = True

    def run(self):
        by_file = group_by_file(self.changes)
        paths = list(by_file.keys())
        self.result = WriteResult(total_files=len(paths),
                                  total_changes=len(self.changes))
        et = None
        try:
            et = ExifTool()
            et.start()
        except Exception as exc:
            self.log.emit(f"无法启动 exiftool: {exc}", LEVEL_ERROR)
            self.finished_write.emit(self.result)
            return

        try:
            snap = self._snapshot(et, by_file)
            self.result.snapshot_id = snap.id
            self.log.emit(f"已保存回滚快照 {snap.id}（{snap.file_count} 个文件 / "
                          f"{snap.change_count} 项）", LEVEL_INFO)

            done = 0
            for path in paths:
                if self._cancel:
                    self.log.emit("已取消, 后续文件未处理", LEVEL_WARN)
                    break
                done += 1
                self.progress.emit(done, len(paths), os.path.basename(path))
                items = by_file[path]
                assignments = sort_assignments([(c.key, c.new) for c in items])
                try:
                    err = et.write(path, assignments, in_place=self.in_place)
                except Exception as exc:
                    self.result.failed.append((path, str(exc)))
                    self.log.emit(f"写入失败 {os.path.basename(path)}: {exc}", LEVEL_ERROR)
                    continue
                if err and "error" in err.lower():
                    self.result.failed.append((path, err.strip()))
                    self.log.emit(f"写入异常 {os.path.basename(path)}: {err.strip()}", LEVEL_ERROR)
                    continue
                self._verify(et, path, items)
                if not any(f[0] == path for f in self.result.failed):
                    self.result.ok_files.append(path)
        finally:
            et.close()
        self.finished_write.emit(self.result)

    def _snapshot(self, et: ExifTool, by_file: dict) -> backup.Snapshot:
        snap = backup.Snapshot(folder=self.folder)
        paths = list(by_file.keys())
        keys_by_path = {p: [c.key for c in by_file[p]] for p in paths}
        for i in range(0, len(paths), 40):
            batch = paths[i:i + 40]
            try:
                records = et.read_json(batch)
            except Exception:
                records = []
            index = {}
            for rec in records:
                src = rec.pop("SourceFile", None)
                if src:
                    index[os.path.normcase(os.path.abspath(src))] = rec
            for path in batch:
                meta = index.get(os.path.normcase(os.path.abspath(path)), {})
                old = {k: meta.get(k) for k in keys_by_path[path]}
                try:
                    st = os.stat(path)
                    stat = (st.st_mtime, getattr(st, "st_birthtime", st.st_ctime))
                except OSError:
                    stat = None
                snap.add_file(path, old, stat)
        snap.save()
        return snap

    def _verify(self, et: ExifTool, path: str, items: list[Change]):
        try:
            records = et.read_json([path])
        except Exception:
            records = []
        meta = {}
        for rec in records:
            src = rec.pop("SourceFile", None)
            if src and os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(path)):
                meta = rec
                break
        self.result.meta_updates[path] = meta
        for c in items:
            actual = meta.get(c.key)
            if c.key in F.FILE_TIME_KEYS or F.is_datetime_value(c.new):
                exp = F.parse_dt(c.new)
                got = F.parse_dt(actual) if actual else None
                if exp is None or got is None:
                    self.result.mismatches.append((path, c.key, c.new, str(actual)))
                    continue
                if exp.timestamp() != got.timestamp():
                    self.result.mismatches.append((path, c.key, c.new, str(actual)))
            else:
                if str(actual) != str(c.new):
                    self.result.mismatches.append((path, c.key, c.new, str(actual)))


class RollbackWorker(QThread):
    progress = Signal(int, int, str)
    log = Signal(str, int)
    finished_rollback = Signal(object)

    def __init__(self, snapshot_path: str, parent=None):
        super().__init__(parent)
        self.snapshot_path = snapshot_path
        self.ok: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def run(self):
        snap = backup.Snapshot.load(self.snapshot_path)
        entries = snap.data.get("files", [])
        et = None
        try:
            et = ExifTool()
            et.start()
        except Exception as exc:
            self.log.emit(f"无法启动 exiftool: {exc}", LEVEL_ERROR)
            self.finished_rollback.emit(self)
            return
        try:
            total = len(entries)
            for i, entry in enumerate(entries, 1):
                path = entry.get("path")
                self.progress.emit(i, total, os.path.basename(path or ""))
                if not path or not os.path.exists(path):
                    self.failed.append((path or "?", "文件不存在"))
                    continue
                assignments = sort_assignments(backup.build_assignments_for_rollback(entry))
                try:
                    et.write(path, assignments)
                    self.ok.append(path)
                except Exception as exc:
                    self.failed.append((path, str(exc)))
        finally:
            et.close()
        try:
            snap.data["rolled_back"] = True
            with open(self.snapshot_path, "w", encoding="utf-8") as f:
                import json
                json.dump(snap.data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        self.finished_rollback.emit(self)
