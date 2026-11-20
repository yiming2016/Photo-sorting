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
    # 合并模式下格子第二行的简介(规格 · 时长 / 读取失败原因), 由主窗口写入
    merge_line: str = ""

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
    # 合并模式的组标题专用: "lossless"(可无损) / "transcode"(需转码) / "single"(单独一个)
    # 非空时标题行不画月/日开关, 改为画彩色标签 + 右侧说明
    merge_tag: str = ""
    merge_note: str = ""
    # 带参数名的规格分段: [(参数名, 值, 等级), ...] 等级 "fatal"/"warn"/None
    merge_parts: list = field(default_factory=list)
    # 组标题左侧"全选本组"勾选框的状态: 0=未选 1=部分选中 2=整组选中
    # (排序模式按年/月/日, 合并模式按规格组, 两边共用这一个字段)
    group_checked: int = 0
    # 统一页三级分组(编码→分辨率→帧率): 走精简标题绘制(只显示 label + "N个",
    # 不画规格串/共N个/段数), 其余差异落在卡片规格行 merge_line 上。
    norm_mode: bool = False
    norm_need: int = 0             # 该节点下需转码的视频数(0 则显示"可直接快合")
    # v244: 分辨率级标题在"N个"右侧的横竖屏提示("横屏"/"竖屏"; 未知或方形为空)
    norm_orientation: str = ""
    # 统一页组标题上的"分辨率/帧率"开关(对齐排序年行上的月/日开关), 仅编码级标题使用
    norm_res_on: bool = False
    norm_fps_on: bool = False
    norm_res_enabled: bool = True
    norm_fps_enabled: bool = True


@dataclass
class PhotoRow:
    photo: Photo
    index: int = 0             # 在选中集合 / 全表中的序号(1 起)


@dataclass
class PackRow(PhotoRow):
    """组块(v226 紧凑卡 → v231 推广): 统一页叶级分组渲染成"带标题的卡片块" ——
    组头(标题条)横贯块宽画在上半部, 本组的视频作为子卡横排在标题条正下方。

    1 个文件 = 一格紧凑卡(组头+唯一卡片, 与 v226 完全一致);
    2+ 个文件 = 一条宽块, extra 里是第 2 张起的子卡(每张一个 PhotoRow, 带全局序号)。
    块内勾选按**路径**逐张管理(main_window._grid_sel_paths 是权威), 条目级的
    Qt 选中只是投影 —— 子卡高亮由 delegate 读 model.grid_checked 画。

    **继承 PhotoRow** —— 选择/缩略图/按行找图的既有路径自动兼容, 组头交互
    (折叠/勾选)与子卡点击需要视图特判(grouplist)。
    """
    header: HeaderRow = None
    extra: list = field(default_factory=list)   # 第 2 张起的子卡: [PhotoRow, ...]

    def all_rows(self) -> list:
        """块内全部子卡(含首卡): [PhotoRow(photo, index)] + extra。"""
        return [PhotoRow(self.photo, self.index)] + list(self.extra)

    def all_photos(self) -> list:
        """块内全部照片对象(勾选/缩略图加载按路径逐张用)。"""
        return [self.photo] + [r.photo for r in self.extra]


@dataclass
class CollapsedPack:
    """收起的叶级组块(v232): 统一页叶级组收起时只画头块(标题条), 宽一格。

    以前(v231 及以前)收起会变成**满宽 HeaderRow** —— 标题从块头的位置跳到
    独占一整行, 后面的内容整体下移, 用户口径"点击收起的时候直接收起即可,
    位置不需要变"。CollapsedPack 仍按普通条目参与流式布局(占一个格子位),
    头块绘制与展开时的组头完全同款(draw_pack_head), 点它可再展开。

    **不继承 PhotoRow**(没有可见的照片) —— data()/flags() 要守卫, 勾选/选中/
    缩略图等按 PhotoRow 找图的路径天然跳过它; 组头勾选状态由 main_window
    _sync_group_checks 从 row.header 同步。
    """
    header: HeaderRow = None


@dataclass
class RowBreak:
    """换行条(v227): 宽度与视口同宽正好占满一排, 后面的条目自然换到新的一行。

    separator=True(v229): 合并页「无损→独立」的分界 —— 画一条 ACCENT 蓝横线,
    把「可无损合并」区和「独立」区隔开; False 时保持隐形(纯换行条)。
    """
    def __init__(self, separator: bool = False):
        self.separator = separator


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
        # ---- 合并模式 ----
        # None = 按时间分组(排序界面)；非 None = 按规格分组(合并界面)。
        # 每组: {"key": sig, "label": 组标题, "tag": lossless/transcode/single,
        #        "note": 右侧说明, "photos": [...]}
        self.merge_groups: list | None = None
        self.merge_specs: dict[str, dict] = {}
        self.merge_errors: dict[str, str] = {}
        # v231: 网格勾选的镜像集合 —— main_window._grid_sel_paths 每次变更都会
        # 同步进来, delegate 画组块子卡的逐张高亮时从这里读(条目级 Qt 选中
        # 表达不了"块内只勾一部分")。
        self.grid_checked: set[str] = set()
        # ---- 统一页三级分组(编码→分辨率→帧率) ----
        # 非 None 时 rebuild 走 _rebuild_norm(): 三级可折叠 HeaderRow(norm_mode=True),
        # 标题精简为"N个", 视频卡片的规格行照旧显示差异。
        self.norm_groups: list | None = None
        # v214: 统一页三级开关(对齐排序的年/月/日) —— codec=编码(总开关) / res=分辨率 / fps=帧率,
        # 顶栏三个胶囊开关改这里, 每个编码组还能单独覆盖(per_norm, key=(编码,))。
        self.norm_master = {"codec": True, "res": True, "fps": True}
        self.per_norm: dict[tuple, dict] = {}

    # ---------- 基础 ----------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == self.ROLE_ROW:
            return row
        if isinstance(row, (RowBreak, CollapsedPack)):
            return None               # 换行条/收起组块: 除 ROLE_ROW 外什么都不给
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
        if isinstance(row, (HeaderRow, RowBreak, CollapsedPack)):
            return Qt.ItemIsEnabled   # 换行条/收起组块不可选/不可拖, 免得混进选中集合
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

    # ---------- 统一页三级开关(编码 / 分辨率 / 帧率, 对齐年月日) ----------
    def norm_state(self, key) -> dict:
        """某个编码组自己的"分辨率/帧率"开关; 没单独设过就跟随总开关。"""
        key = tuple(key or ())[:1]
        st = self.per_norm.get(key)
        if st is None:
            st = {"res": self.norm_master["res"], "fps": self.norm_master["fps"]}
            self.per_norm[key] = st
        return st

    def set_norm_master(self, codec: bool, res: bool, fps: bool):
        self.norm_master["codec"] = bool(codec)
        self.norm_master["res"] = bool(res)
        self.norm_master["fps"] = bool(fps)
        for st in self.per_norm.values():
            st["res"] = bool(res)
            st["fps"] = bool(fps)
        self.rebuild()

    def set_norm_toggle(self, key, which: str, on: bool):
        self.norm_state(key)[which] = bool(on)
        self.rebuild()

    def norm_key_of(self, photo) -> tuple | None:
        """这张视频在当前开关下显示到第几级(与排序 key_of 同口径)。

        photo.norm_key 恒为完整三级 (编码, 分辨率, 帧率); 这里按总开关 + 该编码组
        自己的开关把它截到该显示的深度。编码总开关关掉时返回 None(=不分组)。
        """
        full = getattr(photo, "norm_key", None)
        if not full:
            return None
        if not self.norm_master["codec"]:
            return None
        key = tuple(full)
        st = self.norm_state(key[:1])
        depth = 1
        if st.get("res"):
            depth = 2
            if st.get("fps"):
                depth = 3
        return key[:min(depth, len(key))]

    # ---------- 分组 ----------
    def key_of(self, photo) -> tuple | None:
        """这一张属于哪个分组(年份 / 年月 / 年月日), 口径与 rebuild 完全一致。

        不匹配当前筛选 / 照片已被过滤 → None(它根本没列出来, 也就不该被算进
        任何一组的"全选")。
        """
        if photo is None or not self._match_filter(photo):
            return None
        # 被手动拖到别的分组过 -> 跟着那个分组, 不再按自己的日期算。
        # 这样把 2023 的图拖进 2024 段, 不会在中间又冒出一个 "2023" 标题。
        if getattr(photo, "group_override", None) is not None:
            return tuple(photo.group_override)
        dt = photo.sort_dt()
        if dt == datetime.min:
            return ("未知",)
        seg: list = []
        if self.master["year"]:
            year = dt.year
            seg.append(year)
            st = self.year_state(year)
            if st.get("month", False):
                seg.append(dt.month)
                if st.get("day", False):
                    seg.append(dt.day)
        else:
            seg.append("全部")
        return tuple(seg)

    def _segment_keys(self) -> list[tuple]:
        return [self.key_of(p) for p in self.photos]

    def group_members(self, key: tuple) -> list:
        """某个标题行(年/月/日)底下**当前列得出来**的照片, 口径与 rebuild 一致。

        收起的分组照样算进来 —— "全选本组"选的是整组, 跟此刻这组展开没展开无关。
        """
        key = tuple(key or ())
        if not key:
            return []
        n = len(key)
        out = []
        for p in self.photos:
            k = self.key_of(p)
            if k and len(k) >= n and tuple(k[:n]) == key:
                out.append(p)
        return out

    def rebuild(self):
        # v209: 统一页三级分组(编码→分辨率→帧率), 对齐排序年/月/日的可折叠行为。
        # 三级 key 来自每个视频的 norm_key 属性, 与排序一样按前缀逐级铺 HeaderRow。
        if self.norm_groups is not None:
            self._rebuild_norm()
            return
        # ⚠️ v141: 合并模式的行列是靠"规格分组"算的。分组还没算出来时
        # (探测没跑完 / 一个组都没凑成) 会**一行都没有** —— 用户看到的就是
        # "刚加进来的文件全没了", 可 model.photos 里明明还在(所以添加弹窗
        # 还把它们当成已存在、不让再选)。这时先按普通分组把列表铺出来。
        if self.merge_groups:
            self._rebuild_merge()
            return
        if (self.merge_groups is not None
                and any(getattr(p, "kind", "") == "video" for p in self.photos)):
            self._rebuild_merge_pending()
            return
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
        last_collapsed: tuple | None = None   # v237: 刚画过收起的单文件叶组块(防重复)
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
            # v226: 叶分组只有 1 张且不在最顶层(年/编码级带开关, 不打包)时,
            # 组头 + 唯一卡片合成一格 —— 单文件组不再独占一整行。
            leaf_key = tuple(key[:max_depth])
            pack_leaf = (not collapsed_found and max_depth >= 2
                         and counts.get(leaf_key, 0) == 1)
            # v237: 单文件叶组收起时原本要画成"满宽 HeaderRow"(独占一整行),
            # 紧凑卡并排的组头就会跳到下一排开头 —— 用户口径"点收起这组不要动,
            # 下面的往上走"。改成和统一页 v232 一样的 CollapsedPack: 原位一格
            # 只画头块, 位置不跳。多文件叶组收起仍走满宽 HeaderRow(位置本就不跳)。
            leaf_collapsed = (collapsed_found and max_depth == len(key)
                              and max_depth >= 2 and counts.get(leaf_key, 0) == 1)
            for depth in range(same, max_depth - (1 if (pack_leaf or leaf_collapsed) else 0)):
                sub = tuple(key[:depth + 1])
                rows.append(self._make_header(sub, depth, counts.get(sub, 0),
                                              collapsed=sub in self.collapsed))
            prev = key
            if collapsed_found:
                if leaf_collapsed and last_collapsed != leaf_key:
                    last_collapsed = leaf_key
                    rows.append(CollapsedPack(header=self._make_header(
                        leaf_key, max_depth - 1, 1, collapsed=True)))
                continue  # 该图处于折叠分组中(含其自身层级被折叠)
            last_collapsed = None
            idx += 1
            if pack_leaf:
                self._pad_break_before_pack(rows)
                rows.append(PackRow(photo, idx,
                                    header=self._make_header(leaf_key, max_depth - 1, 1)))
            else:
                rows.append(PhotoRow(photo, idx))

        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    # ---------- 合并模式(按规格分组) ----------
    @staticmethod
    def _pad_break_before_pack(rows, separator: bool = False):
        """紧凑卡前垫一条换行条(v227 合并页引入, v228 推广到排序/统一)。

        v229 起三种模式分流:
        - 统一页**不再垫条**: 紧凑卡允许跟在别的组的照片后面同排(空间不够时
          QListView 自然换行 —— 用户口径"空间允许就同一行, 占满一整行才另起一行")。
        - 排序页保留隐形垫条(separator=False), 紧凑卡仍另起一行。
        - 合并页垫条并画蓝线(separator=True): 作为「可无损合并」和「独立」两区
          的分界横线; 前面是折叠的无损组(只剩满宽标题)时也垫 —— 标题本来就
          自带换行, 垫条是为了画出分界线。
        """
        last = rows[-1] if rows else None
        if isinstance(last, PackRow) or last is None:
            return
        if separator:
            # 合并分界: 跟在普通照片或组标题(折叠的无损组)后都垫 + 画蓝线
            if isinstance(last, (PhotoRow, HeaderRow)):
                rows.append(RowBreak(separator=True))
            return
        if isinstance(last, PhotoRow) and not isinstance(last, PackRow):
            rows.append(RowBreak())

    def _rebuild_merge_pending(self):
        """分组还没算出来时的兜底: 先按"加了没有分组"把视频铺出来。

        只列视频(合并页本来就是给视频用的), 顺序跟主列表一致。
        等探测跑完、分组到位, `set_merge_groups` 会重建一遍换成真正的分组。
        """
        idx = 0
        rows: list[PhotoRow | HeaderRow] = []
        for p in self.photos:
            if getattr(p, "kind", "") != "video" or not self._match_filter(p):
                continue
            idx += 1
            rows.append(PhotoRow(p, idx))
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def _rebuild_merge(self):
        """合并模式的行动序列: 每个规格组一条标题 + 组内卡片。

        组内先后跟随 self.photos 的当前位置(拖拽重排改的就是它);
        组的先后由 merge_groups 固定(可无损组在前、单打独斗的在后),
        因此跨组拖拽会被视图层拦下(见 GroupView._resolve_target)。
        """
        pos = {id(p): i for i, p in enumerate(self.photos)}
        rows: list[PhotoRow | HeaderRow] = []
        idx = 0
        for g in self.merge_groups:
            members = [p for p in g.get("photos", [])
                       if id(p) in pos and self._match_filter(p)]
            if not members:
                continue
            members.sort(key=lambda p: pos[id(p)])
            key = tuple(g.get("key") or ())
            # ⚠️ v143: 这一套是按"规格分组"单独算的, 以前**完全没看 self.collapsed**
            # —— 点组标题左边的小三角, 键虽然记进去了, 卡片照样全列出来,
            # 表现得就像"点了没反应"。排序模式那套(按年月分组)一直是对的。
            collapsed = key in self.collapsed
            if len(members) == 1:
                # v226: 单文件组 → 组头+唯一卡片合成一格(一行可并排多个),
                # 组头信息(标签/规格串)折行画在卡片上半部, 一项不丢。
                # v227: 收起也保持紧凑卡(只画头块, 不占整行); 且在紧凑卡前
                # 垫一条隐形换行条, 让所有独立卡另起一行(v229: 排序页维持
                # 此口径, 统一页已放开; 垫条同时画蓝线 = 无损/独立分界)。
                self._pad_break_before_pack(rows, separator=True)
                idx += 1
                rows.append(PackRow(members[0], idx, header=HeaderRow(
                    level=0, key=key, label=g.get("label", ""),
                    count=1, year=0,
                    merge_tag=g.get("tag", ""), merge_note=g.get("note", ""),
                    merge_parts=g.get("parts", []),
                    collapsed=collapsed,
                )))
                continue
            rows.append(HeaderRow(
                level=0, key=key, label=g.get("label", ""),
                count=len(members), year=0,
                merge_tag=g.get("tag", ""), merge_note=g.get("note", ""),
                merge_parts=g.get("parts", []),
                collapsed=collapsed,
            ))
            if key in self.collapsed:
                continue                  # 收起来了: 这一组的卡片先不列
            for p in members:
                idx += 1
                rows.append(PhotoRow(p, idx))
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def _rebuild_norm(self):
        """统一页三级分组: 编码 → 分辨率 → 帧率(对齐排序的年/月/日三级)。

        每个视频的 norm_key=(codec, resolution, fps) 由 NormWorkspace 按当前档位算好
        写进 photo.norm_key。这里完全复用排序 rebuild 的"前缀逐级展开 + collapsed
        折叠"算法, 只是标题走 norm_mode 精简绘制(只写该级名 + N个)。
        need 计数也从 photo.norm_need 读, 逐级汇总到父标题。

        v214: 铺到第几级由 norm_key_of() 决定(顶栏 编码/分辨率/帧率 三个开关 +
        每个编码组自己的开关)。编码总开关关掉时和排序的"年"关掉一样, 直接平铺文件。
        """
        if not self.norm_master["codec"]:
            self.beginResetModel()
            self.rows = [PhotoRow(p, i + 1) for i, p in enumerate(self.photos)
                         if getattr(p, "norm_key", None)]
            self.endResetModel()
            return
        keys = [self.norm_key_of(p) for p in self.photos]
        counts: dict[tuple, int] = {}
        needs: dict[tuple, int] = {}
        labels: dict[tuple, str] = {}
        orient: dict[tuple, str] = {}   # v244: 分辨率级标题的横竖屏提示
        # v221: 铺行算法靠"同前缀相邻"增量画组头, 而 photos 的顺序是文件添加
        # 顺序、与分组无关 —— 横竖屏交错的素材会把同一组隔开, 同一个组头就被
        # 画两次(用户看到两个一模一样的 1920×1080, 中间夹着别的组)。
        # 统一页没有手动排序语义(跨组拖拽本来就被拦截), 按 key 稳定排序:
        # 同组必然相邻, 组内保持原有先后。key 为 None(不该显示)的排到最后,
        # 反正下面会被跳过。
        pairs = [(p, k) for p, k in zip(self.photos, keys)]
        pairs.sort(key=lambda pk: (pk[1] is None, pk[1] or ()))
        for p, key in pairs:
            if not key:
                continue
            need = int(getattr(p, "norm_need", 0) or 0)
            lab = getattr(p, "norm_labels", None) or ()
            for i in range(1, len(key) + 1):
                sub = tuple(key[:i])
                counts[sub] = counts.get(sub, 0) + 1
                needs[sub] = needs.get(sub, 0) + need
                if i <= len(lab):
                    labels.setdefault(sub, str(lab[i - 1]))
                # v244: 只给分辨率级(i==2)记录, 编码级/帧率级不显示横竖屏
                if i == 2:
                    orient.setdefault(sub, getattr(p, "norm_orientation", "") or "")
        rows: list[PhotoRow | HeaderRow] = []
        prev: tuple | None = None
        idx = 0
        last_pack: PackRow | None = None    # v231: 当前正在累积的叶级组块
        last_collapsed: tuple | None = None  # v232: 刚画过收起组块的叶 key
        for photo, key in pairs:
            if key is None:
                continue
            same = 0
            if prev is not None:
                while same < min(len(prev), len(key)) and prev[same] == key[same]:
                    same += 1
            max_depth = len(key)
            collapsed_found = False
            for i in range(1, len(key) + 1):
                if tuple(key[:i]) in self.collapsed:
                    max_depth = i
                    collapsed_found = True
                    break
            # v231: 叶分组(最深层级)一律打包成"组块"(1 文件=紧凑卡, 2+ 文件=宽块)。
            # v232: 叶级组收起时也按"组块"渲染(只画头块, 位置不变), 不再变满宽标题。
            # 叶级留在第 0 层(编码级, 带开关)时不打包, 照旧铺满宽标题。
            leaf_key = tuple(key[:max_depth])
            pack_leaf = not collapsed_found and max_depth >= 2
            # 收起的层级就是该素材的最深层级(叶级)且至少在编码下一级 → 收起组块;
            # 收起的是中间层(如编码级收起整个分辨率子树)仍走满宽标题(位置本来就不跳)。
            leaf_collapsed = (collapsed_found and max_depth == len(key)
                              and max_depth >= 2)
            leaf_block = pack_leaf or leaf_collapsed
            for depth in range(same, max_depth - (1 if leaf_block else 0)):
                sub = tuple(key[:depth + 1])
                st = self.norm_state(key[:1])
                rows.append(HeaderRow(
                    level=depth, key=sub,
                    label=labels.get(sub, str(sub[-1]) if sub else ""),
                    count=counts.get(sub, 0), norm_mode=True,
                    norm_need=needs.get(sub, 0),
                    norm_orientation=orient.get(sub, ""),
                    collapsed=sub in self.collapsed,
                    norm_res_on=bool(st.get("res")),
                    norm_fps_on=bool(st.get("fps")),
                    norm_res_enabled=bool(self.norm_master["codec"]),
                    norm_fps_enabled=bool(self.norm_master["codec"] and st.get("res")),
                ))
            prev = key
            if collapsed_found:
                if leaf_collapsed and last_collapsed != leaf_key:
                    # v232: 收起的叶级组 = 只画头块(标题条), 不占整行、不挪位置
                    leaf_st = self.norm_state(key[:1])
                    last_collapsed = leaf_key
                    last_pack = None
                    rows.append(CollapsedPack(header=HeaderRow(
                        level=max_depth - 1, key=leaf_key,
                        label=labels.get(leaf_key, str(leaf_key[-1])),
                        count=counts.get(leaf_key, 0), norm_mode=True,
                        norm_need=needs.get(leaf_key, 0),
                        norm_orientation=orient.get(leaf_key, ""),
                        collapsed=True,
                        norm_res_on=bool(leaf_st.get("res")),
                        norm_fps_on=bool(leaf_st.get("fps")),
                        norm_res_enabled=bool(self.norm_master["codec"]),
                        norm_fps_enabled=bool(self.norm_master["codec"] and leaf_st.get("res")),
                    )))
                continue
            last_collapsed = None
            idx += 1
            if pack_leaf:
                leaf_st = self.norm_state(key[:1])
                # v231: 叶级组(最深层级)一律打包成"组块" —— 1 个文件=一格紧凑卡,
                # 2+ 个文件=标题条横贯块宽、视频横排在标题条正下方(v231 用户口径
                # "把文件放到分组栏的下面"); 同一叶组的后续照片并入上一块的 extra。
                if (last_pack is not None
                        and tuple(last_pack.header.key) == leaf_key):
                    last_pack.extra.append(PhotoRow(photo, idx))
                else:
                    last_pack = PackRow(photo, idx, header=HeaderRow(
                        level=max_depth - 1, key=leaf_key,
                        label=labels.get(leaf_key, str(leaf_key[-1])),
                        count=counts.get(leaf_key, 1), norm_mode=True,
                        norm_need=needs.get(leaf_key, 0),
                        norm_orientation=orient.get(leaf_key, ""),
                        collapsed=False,
                        norm_res_on=bool(leaf_st.get("res")),
                        norm_fps_on=bool(leaf_st.get("fps")),
                        norm_res_enabled=bool(self.norm_master["codec"]),
                        norm_fps_enabled=bool(self.norm_master["codec"] and leaf_st.get("res")),
                    ))
                    rows.append(last_pack)
            else:
                last_pack = None
                rows.append(PhotoRow(photo, idx))
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def set_norm_groups(self, groups: list | None):
        """进入统一页(传三级信息)/退出(None)。groups 仅作"有/无"标志与标题元数据,
        真正的三级 key/label/need 由每个 Photo 的 norm_key/norm_labels/norm_need 携带。"""
        self.beginResetModel()
        self.norm_groups = groups
        self.rows = []
        self.endResetModel()
        self.rebuild()

    def set_merge_groups(self, groups: list | None):
        """进入合并模式(传分组列表) / 退出(None)。"""
        self.beginResetModel()
        self.merge_groups = groups
        self.rows = []
        self.endResetModel()
        self.rebuild()

    def merge_videos(self) -> list[Photo]:
        """合并模式下当前列出的全部视频, 按显示顺序。"""
        return [r.photo for r in self.rows if isinstance(r, PhotoRow)]

    def group_key_of(self, photo: Photo):
        """合并模式下这张图属于哪个规格组(不在合并模式时返回 None)。"""
        if self.merge_groups is None:
            return None
        for g in self.merge_groups:
            for p in g.get("photos", []):
                if p is photo:
                    return g.get("key")
        return None

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
                             year=key[0] if isinstance(key[0], int) else 0,
                             collapsed=collapsed)      # v226: 月级折叠三角也要转向
        return HeaderRow(level=2, key=key, label=f"{key[1]} 月 {key[2]} 日", count=count,
                         year=key[0] if isinstance(key[0], int) else 0,
                         collapsed=collapsed)

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
        if apply_override and self.merge_groups is None and self.norm_groups is None:
            # v217: 只有排序态才写 group_override。合并/统一页里拖一下(同组内)
            # 以前也会把被拖的照片钉到落点的日期键上 —— 回到排序页它就跑到别人
            # 的年月组底下了, 而且 override 深度固定在拖拽那一刻, 之后开关"日"
            # 会 key 深度不匹配, 卡片被塞进上一个日组。
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
            if isinstance(row, PackRow):
                # v231: 多文件组块整块被选中 = 块里每张都算
                out.extend(row.all_photos())
            elif isinstance(row, PhotoRow):
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
