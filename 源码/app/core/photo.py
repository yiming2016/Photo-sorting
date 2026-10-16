# -*- coding: utf-8 -*-
"""照片数据模型与列表模型。

列表顺序 = 唯一真相。分组只是"视图层的连续分段":
按当前顺序遍历, 相邻两项分组键不同就插入一个标题行。
这样跨年跨月拖拽后, 被拖走的图会在新位置自然形成自己的分段。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QPixmap

from . import fields as F

IMAGE_EXT = {
    "jpg", "jpeg", "jpe", "png", "webp", "heic", "heif", "avif", "bmp", "gif",
    "tif", "tiff", "dng", "cr2", "cr3", "nef", "nrw", "arw", "srf", "sr2",
    "rw2", "orf", "raf", "pef", "ptx", "x3f", "r3d", "erf", "mrw", "mef",
}
VIDEO_EXT = {
    "mp4", "mov", "m4v", "avi", "mkv", "webm", "3gp", "3g2", "mts", "m2ts",
    "mpg", "mpeg", "ts", "flv", "wmv", "hevc", "insv",
}

STATUS_NORMAL = 0
STATUS_PENDING = 1
STATUS_DONE = 2
STATUS_ERROR = 3

THUMB_NONE = 0
THUMB_LOADING = 1
THUMB_OK = 2
THUMB_FAIL = 3


def kind_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return "other"


@dataclass
class Photo:
    path: str
    name: str = ""
    size: int = 0
    kind: str = "image"
    meta: dict = field(default_factory=dict)
    planned: dict = field(default_factory=dict)   # 合成字段计划值 {"taken": datetime}
    pending: dict = field(default_factory=dict)   # 其它字段待写入 {"EXIF:Artist": "x"}
    status: int = STATUS_NORMAL
    error: str = ""
    thumb: QPixmap | None = None
    thumb_state: int = THUMB_NONE
    removed: bool = False
    # 手动拖到别的分组后, 这张图就跟着新分组走(不再按自己的日期算)。
    # 没有手动拖过就是 None —— 分组仍按日期来。
    group_override: tuple | None = None

    def __post_init__(self):
        if not self.name:
            self.name = os.path.basename(self.path)

    # ---------- 时间 ----------
    def raw(self, key: str) -> str | None:
        for k in F.READ_PRIORITY.get(key, ()):
            v = self.meta.get(k)
            if v:
                return v
        return None

    def planned_dt(self, key: str) -> datetime | None:
        return self.planned.get(key)

    def taken_dt(self) -> datetime | None:
        """生效的拍摄时间: 计划值优先, 其次元数据, 最后文件名兜底。"""
        p = self.planned.get(F.TAKEN)
        if p:
            return F.local_naive(p)
        dt = F.parse_dt(self.raw(F.TAKEN))
        if dt:
            return F.local_naive(dt)
        return F.parse_from_filename(self.name)

    def sort_dt(self) -> datetime:
        """用于排序与分组的时间, 统一为本地朴素时间。"""
        dt = self.taken_dt()
        if dt:
            return dt
        try:
            return datetime.fromtimestamp(os.path.getmtime(self.path))
        except OSError:
            return datetime.min

    def synthetic_display(self, key: str) -> str:
        p = self.planned.get(key)
        if p:
            return p.strftime("%Y-%m-%d %H:%M:%S")
        if key == F.TAKEN:
            dt = self.taken_dt()
            return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"
        return F.display(self.raw(key))

    def all_datetime_keys(self) -> list[str]:
        return [k for k, v in self.meta.items() if F.is_datetime_value(v)]

    # ---------- 待写入 ----------
    @property
    def dirty(self) -> bool:
        return bool(self.planned or self.pending)

    def pending_count(self) -> int:
        n = len(self.planned) * 3 + len(self.pending)  # 粗略估计, 真实值由 writer 计算
        return n

    def clear_pending(self):
        self.planned.clear()
        self.pending.clear()
        if self.status == STATUS_PENDING:
            self.status = STATUS_NORMAL


@dataclass
class HeaderRow:
    level: int                 # 0=年 1=月 2=日
    key: tuple                 # (2022,) / (2022,3) / (2022,3,11)
    label: str
    count: int = 0
    collapsed: bool = False
    year: int = 0
    month_on: bool = False     # 仅年行使用
    day_on: bool = False       # 仅年行使用
    month_enabled: bool = True
    day_enabled: bool = True


@dataclass
class PhotoRow:
    photo: Photo
    index: int = 0             # 在选中集合 / 全表中的序号(1 起)


class PhotoModel(QAbstractListModel):
    """照片列表模型: 维护 photos 顺序 + 计算分组标题行。"""

    progress = Signal(int, int)     # 已完成, 总数
    orderChanged = Signal()

    ROLE_ROW = Qt.UserRole + 1
    ROLE_KIND = Qt.UserRole + 2
    ROLE_PHOTO = Qt.UserRole + 3
    ROLE_HEADER = Qt.UserRole + 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.photos: list[Photo] = []
        self.rows: list[PhotoRow | HeaderRow] = []
        self.folder = ""
        self.recursive = False
        self.master = {"year": True, "month": False, "day": False}
        self.per_year: dict[int, dict] = {}
        self.collapsed: set[tuple] = set()
        self.filter_text = ""

    # ---------- 基础 ----------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == self.ROLE_ROW:
            return row
        if role == self.ROLE_KIND:
            return "header" if isinstance(row, HeaderRow) else "photo"
        if isinstance(row, HeaderRow):
            if role == self.ROLE_HEADER:
                return row
            if role == Qt.DisplayRole:
                return row.label
            return None
        if role == self.ROLE_PHOTO:
            return row.photo
        if role == Qt.DisplayRole:
            return row.photo.name
        if role == Qt.ToolTipRole:
            return row.photo.path
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        base = super().flags(index)
        if not index.isValid():
            return base
        row = self.rows[index.row()]
        if isinstance(row, HeaderRow):
            return Qt.ItemIsEnabled
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled

    # ---------- 载入 ----------
    def set_folder(self, folder: str, recursive: bool = False):
        self.folder = folder
        self.recursive = recursive

    def set_photos(self, photos: Iterable[Photo]):
        self.beginResetModel()
        self.photos = list(photos)
        self.per_year = {}
        self.collapsed = set()
        self.rows = []
        self.endResetModel()
        self.rebuild()

    def add_photos(self, photos: Iterable[Photo], dedupe: bool = True):
        """累加照片: 追加到现有列表末尾并去重, 不重置折叠/分组状态。

        用于「打开文件夹 / 打开文件」的累加模式 —— 多次打开的内容合并到同一列表,
        已存在的路径不会重复加入(已有对象被保留, 不会覆盖用户的待写入计划)。
        """
        existing_paths = {p.path for p in self.photos}
        to_add: list[Photo] = []
        for p in photos:
            if dedupe and p.path in existing_paths:
                continue
            existing_paths.add(p.path)
            to_add.append(p)
        if not to_add:
            self.rebuild()  # 即便没有新增, 也确保视图与最新数据一致
            return
        self.photos = self.photos + to_add
        self.rebuild()

    def year_state(self, year: int) -> dict:
        st = self.per_year.get(year)
        if st is None:
            st = {"month": False, "day": False}
            self.per_year[year] = st
        return st

    def set_master(self, year: bool, month: bool, day: bool):
        self.master["year"] = year
        self.master["month"] = month
        self.master["day"] = day
        # 主开关关闭时, 子开关同步失效; 打开时恢复为跟随主开关
        for y, st in self.per_year.items():
            st["month"] = month
            st["day"] = day
        self.rebuild()

    def set_year_toggle(self, year: int, which: str, on: bool):
        st = self.year_state(year)
        st[which] = on
        self.rebuild()

    # ---------- 分组 ----------
    def _segment_keys(self) -> list[tuple]:
        keys: list[tuple] = []
        for p in self.photos:
            if self.filter_text and self.filter_text.lower() not in p.name.lower():
                keys.append(None)
                continue
            # 被手动拖到别的分组过 -> 跟着那个分组, 不再按自己的日期算。
            # 这样把 2023 的图拖进 2024 段, 不会在中间又冒出一个 "2023" 标题。
            if getattr(p, "group_override", None) is not None:
                keys.append(p.group_override)
                continue
            dt = p.sort_dt()
            if dt == datetime.min:
                keys.append(("未知",))
                continue
            seg: list = []
            year = dt.year
            if self.master["year"]:
                seg.append(year)
                st = self.year_state(year)
                show_month = st.get("month", False)
                if show_month:
                    seg.append(dt.month)
                    if st.get("day", False):
                        seg.append(dt.day)
            else:
                seg.append("全部")
            keys.append(tuple(seg))
        return keys

    def rebuild(self):
        if not self.master["year"]:
            self.beginResetModel()
            self.rows = [PhotoRow(p, i + 1) for i, p in enumerate(self.photos)
                         if self._match_filter(p)]
            self.endResetModel()
            return

        keys = self._segment_keys()
        counts: dict[tuple, int] = {}
        for key in keys:
            if key is None:
                continue
            for i in range(1, len(key) + 1):
                sub = tuple(key[:i])
                counts[sub] = counts.get(sub, 0) + 1

        rows: list[PhotoRow | HeaderRow] = []
        prev: tuple | None = None
        idx = 0
        for photo, key in zip(self.photos, keys):
            if key is None:
                continue
            # 与上一张相同的层级深度
            same = 0
            if prev is not None:
                while same < min(len(prev), len(key)) and prev[same] == key[same]:
                    same += 1
            # 最深可展开的层级(遇到折叠层就停)
            max_depth = len(key)
            collapsed_found = False
            for i in range(1, len(key) + 1):
                if tuple(key[:i]) in self.collapsed:
                    max_depth = i
                    collapsed_found = True
                    break
            for depth in range(same, max_depth):
                sub = tuple(key[:depth + 1])
                rows.append(self._make_header(sub, depth, counts.get(sub, 0),
                                              collapsed=sub in self.collapsed))
            prev = key
            if collapsed_found:
                continue  # 该图处于折叠分组中(含其自身层级被折叠)
            idx += 1
            rows.append(PhotoRow(photo, idx))

        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def _match_filter(self, photo: Photo) -> bool:
        if not self.filter_text:
            return True
        return self.filter_text.lower() in photo.name.lower()

    def _make_header(self, key: tuple, depth: int, count: int, collapsed: bool = False) -> HeaderRow:
        if depth == 0:
            first = key[0]
            if isinstance(first, int):
                label = f"{first}"
                year = first
            else:
                label = str(first)
                year = 0
            st = self.year_state(year) if year else {"month": False, "day": False}
            return HeaderRow(
                level=0, key=key, label=label, count=count, year=year,
                month_on=st.get("month", False), day_on=st.get("day", False),
                month_enabled=True, day_enabled=True,
                collapsed=collapsed,
            )
        if depth == 1:
            return HeaderRow(level=1, key=key, label=f"{key[1]} 月", count=count,
                             year=key[0] if isinstance(key[0], int) else 0)
        return HeaderRow(level=2, key=key, label=f"{key[1]} 月 {key[2]} 日", count=count,
                         year=key[0] if isinstance(key[0], int) else 0)

    def toggle_collapse(self, key: tuple):
        if key in self.collapsed:
            self.collapsed.discard(key)
        else:
            self.collapsed.add(key)
        self.rebuild()

    # ---------- 顺序操作 ----------
    def photo_rows(self) -> list[PhotoRow]:
        return [r for r in self.rows if isinstance(r, PhotoRow)]

    def clear_group_overrides(self):
        """丢掉所有"手动归组"的痕迹, 分组重新按日期算。

        用于自动排序、反转、写入完成等"顺序被整体重排"的时刻。
        """
        for p in self.photos:
            p.group_override = None

    def move_rows(self, row_indexes: list[int], target: int, after: bool,
                  live: bool = False) -> list[int]:
        """把若干显示行(图片)移动到 target 显示行附近, 返回移动后的新行号。

        live=True 用于"拖动过程中实时让位": 只调位置、不广播 orderChanged,
        避免每动一下就重算一遍待写入计划。
        """
        moving: list[Photo] = []
        for i in row_indexes:
            if 0 <= i < len(self.rows) and isinstance(self.rows[i], PhotoRow):
                moving.append(self.rows[i].photo)
        if not moving:
            return []
        ref = None
        if 0 <= target < len(self.rows):
            row = self.rows[target]
            if isinstance(row, PhotoRow):
                ref = row.photo
            else:
                for j in range(target + 1, len(self.rows)):
                    if isinstance(self.rows[j], PhotoRow):
                        ref = self.rows[j].photo
                        after = False
                        break
                if ref is None:
                    for j in range(target - 1, -1, -1):
                        if isinstance(self.rows[j], PhotoRow):
                            ref = self.rows[j].photo
                            after = True
                            break
        # 移动前先记下每张图当前属于哪个分组, 用来决定"被拖走的图该跟谁一组"
        key_map = {id(p): k for p, k in zip(self.photos, self._segment_keys())}
        dragging = set(id(p) for p in moving)
        remaining = [p for p in self.photos if id(p) not in dragging]
        ordered = [p for p in self.photos if id(p) in dragging]

        # 被拖的照片跟着"落点那张图"的分组走 —— 日期不动, 但不会再在新位置
        # 自己另起一个标题(否则会出现 2023 → 2024 → 2023 这种重复标题)。
        apply_override = True
        if ref is None:
            ref_key = key_map.get(id(remaining[-1])) if remaining else None
        elif id(ref) in dragging:
            apply_override = False          # 落点就在自己身上, 保持原样
        else:
            ref_key = key_map.get(id(ref))
        if apply_override:
            for p in ordered:
                p.group_override = ref_key

        if ref is None or id(ref) in dragging:
            pos = len(remaining)
        else:
            pos = remaining.index(ref) + (1 if after else 0)
        self.photos = remaining[:pos] + ordered + remaining[pos:]
        self.rebuild()
        if not live:
            self.orderChanged.emit()
        ids = set(id(p) for p in ordered)
        return [i for i, r in enumerate(self.rows)
                if isinstance(r, PhotoRow) and id(r.photo) in ids]

    def sort_photos(self, key: str, reverse: bool = False):
        self.clear_group_overrides()   # 顺序整体重排了, 手动归组的痕迹作废
        if key == "name":
            self.photos.sort(key=lambda p: p.name.lower(), reverse=reverse)
        elif key == "time":
            self.photos.sort(key=lambda p: p.sort_dt(), reverse=reverse)
        elif key == "size":
            self.photos.sort(key=lambda p: p.size, reverse=reverse)
        elif key == "path":
            self.photos.sort(key=lambda p: p.path.lower(), reverse=reverse)
        self.rebuild()
        self.orderChanged.emit()

    def reverse_photos(self):
        self.clear_group_overrides()
        self.photos.reverse()
        self.rebuild()
        self.orderChanged.emit()

    def selected_photos(self, indexes: list[QModelIndex]) -> list[Photo]:
        out: list[Photo] = []
        for idx in indexes:
            if not idx.isValid():
                continue
            row = self.rows[idx.row()] if idx.row() < len(self.rows) else None
            if isinstance(row, PhotoRow):
                out.append(row.photo)
        return out

    def all_photos(self) -> list[Photo]:
        return [p for p in self.photos if not p.removed]

    def photo_indexes(self, photos: list[Photo]) -> list[int]:
        ids = set(id(p) for p in photos)
        return [i for i, r in enumerate(self.rows) if isinstance(r, PhotoRow) and id(r.photo) in ids]

    def refresh_photo(self, photo: Photo):
        for i, r in enumerate(self.rows):
            if isinstance(r, PhotoRow) and r.photo is photo:
                self.dataChanged.emit(self.index(i), self.index(i))
                break
