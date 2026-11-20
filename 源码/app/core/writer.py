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
    mismatch_paths: set = field(default_factory=set)  # v256: 读回不一致的文件, 不能标完成
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
    group = target.split(":", 1)[0].lower()
    # 视频一律不写 EXIF: 即便文件里已经带着 EXIF 时间字段, 也只走 QuickTime。
    # 必须放在 `target in photo.meta` 早返回之前, 否则视频自带的 EXIF 字段会被误放行。
    if photo.kind == "video" and group.startswith(("exif", "ifd")):
        return False
    if target in photo.meta:
        return True
    if photo.kind == "video":
        if group.startswith(("quicktime", "track", "media", "keys", "itemlist", "userdata")):
            return True
        if group.startswith("xmp"):
            return fill_xmp
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
                # 视频不写 EXIF(与 should_write 一致): 只走 QuickTime / XMP
                if photo.kind == "video" and key.split(":", 1)[0].lower().startswith(("exif", "ifd")):
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
            snap, unsafe = self._snapshot(et, by_file)
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
                # v217: 写之前读不出元数据 = 快照里没有原值。这时候照写下去,
                # 万一以后回滚, old=None 会被当成"原本没有"→ 把字段删掉。
                # 所以这类文件直接不碰, 报出来让用户处理(通常是文件损坏/被独占)。
                if path in unsafe:
                    msg = "写入前读不到元数据(文件可能损坏或被占用)，已跳过以免无法回滚"
                    self.result.failed.append((path, msg))
                    self.log.emit(f"跳过 {os.path.basename(path)}: {msg}", LEVEL_ERROR)
                    continue
                assignments = sort_assignments([(c.key, c.new) for c in items])
                try:
                    ok_write, detail = et.write(path, assignments, in_place=self.in_place)
                except Exception as exc:
                    self.result.failed.append((path, str(exc)))
                    self.log.emit(f"写入失败 {os.path.basename(path)}: {exc}", LEVEL_ERROR)
                    continue
                if not ok_write:
                    self.result.failed.append((path, detail.strip() or "写入没有生效"))
                    self.log.emit(f"写入异常 {os.path.basename(path)}: "
                                  f"{detail.strip()}", LEVEL_ERROR)
                    continue
                self._verify(et, path, items)
                # v256: 读回校验不一致的不算"完成"(否则"完成N个"把写错的也数进去)
                if (not any(f[0] == path for f in self.result.failed)
                        and path not in self.result.mismatch_paths):
                    self.result.ok_files.append(path)
        finally:
            et.close()
        self.finished_write.emit(self.result)

    def _read_meta(self, et: ExifTool, path: str):
        """读单个文件的元数据; 读不出返回 None(区别于"读到了但没这些字段")。"""
        try:
            records = et.read_json([path])
        except Exception:
            return None
        want = os.path.normcase(os.path.abspath(path))
        for rec in records:
            src = rec.pop("SourceFile", None)
            if src and os.path.normcase(os.path.abspath(src)) == want:
                return rec
        return None

    def _snapshot(self, et: ExifTool, by_file: dict):
        """落快照。返回 (snapshot, unsafe_paths)。

        v217: 以前整批读失败就 `records=[]` → 所有 old 都是 None → 回滚时等于
        "删掉字段"。文件本来有值, 回滚反而抹了它。现在:
          * 整批失败 → 逐文件重试, 别让一个坏文件带走一整批的旧值;
          * 单文件也读不出来 → 记进 unsafe, 写入时跳过它, 回滚也跳过它,
            并在快照里标 `snapshot_ok=False` 留下证据。
        """
        snap = backup.Snapshot(folder=self.folder)
        paths = list(by_file.keys())
        keys_by_path = {p: [c.key for c in by_file[p]] for p in paths}
        unsafe: set[str] = set()
        for i in range(0, len(paths), 40):
            batch = paths[i:i + 40]
            index = None
            try:
                records = et.read_json(batch)
            except Exception:
                records = None
            if records is not None:
                index = {}
                for rec in records:
                    src = rec.pop("SourceFile", None)
                    if src:
                        index[os.path.normcase(os.path.abspath(src))] = rec
            for path in batch:
                key = os.path.normcase(os.path.abspath(path))
                meta = index.get(key) if index is not None else None
                if meta is None:
                    meta = self._read_meta(et, path)     # 单文件重试
                ok = meta is not None
                if not ok:
                    meta = {}
                    unsafe.add(path)
                old = {k: meta.get(k) for k in keys_by_path[path]}
                try:
                    st = os.stat(path)
                    stat = (st.st_mtime, getattr(st, "st_birthtime", st.st_ctime))
                except OSError:
                    stat = None
                snap.add_file(path, old, stat, ok=ok)
        snap.save()
        return snap, unsafe

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
                    self.result.mismatch_paths.add(path)
                    continue
                if exp.timestamp() != got.timestamp():
                    self.result.mismatches.append((path, c.key, c.new, str(actual)))
                    self.result.mismatch_paths.add(path)
            else:
                if str(actual) != str(c.new):
                    self.result.mismatches.append((path, c.key, c.new, str(actual)))
                    self.result.mismatch_paths.add(path)


def should_mark_rolled_back(ok: list, failed: list) -> bool:
    """只有'有成功项且无任何失败'才把快照标记为已回滚(v256)。

    部分文件被占用/失败时保持快照'可用', 让用户能重试那几个失败的文件。
    """
    return bool(ok) and not bool(failed)


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
                if entry.get("snapshot_ok", True) is False:
                    # v217: 快照没记到原值(写入前读取失败) → 这里的 None 不能当
                    # "原本没有"用, 否则回滚会把这个字段删掉。宁可跳过。
                    self.failed.append(
                        (path, "快照未记录该文件的原值（写入前读取失败），已跳过以免误删"))
                    continue
                assignments = sort_assignments(backup.build_assignments_for_rollback(entry))
                try:
                    ok_write, detail = et.write(path, assignments)
                    if ok_write:
                        self.ok.append(path)
                    else:
                        self.failed.append((path, detail.strip() or "回滚没有生效"))
                except Exception as exc:
                    self.failed.append((path, str(exc)))
        finally:
            et.close()
        # v256: 只有全部文件都成功还原才标记"已回滚"。之前无论成功失败都写
        # rolled_back=True → 部分文件被占用/失败后快照永久变灰不可再用, 用户
        # 无法重试那几个失败的文件。失败时保持"可用", 下次还能再回滚。
        if should_mark_rolled_back(self.ok, self.failed):
            try:
                snap.data["rolled_back"] = True
                with open(self.snapshot_path, "w", encoding="utf-8") as f:
                    import json
                    json.dump(snap.data, f, ensure_ascii=False, indent=1)
            except Exception:
                pass
        self.finished_rollback.emit(self)
