# -*- coding: utf-8 -*-
"""剪辑态的时间线控件(全自绘): 多轨自由摆放 + 吸附 + 缩略图条。

为什么自绘而不用 QListWidget/QGraphicsView:
- 全局 QSS 有 `QWidget { background: transparent; }`, 子控件样式会被主题吃掉
  (预览列表黑底踩过四次坑, 见 MEMORY.md 排错铁律);
- 片段要按"时间→像素"精确落位, 还要叠标尺/播放头/缩略图条/吸附线, 自绘最直接。

坐标约定:
- 内容坐标 = 时间(秒); 视图 x = GUTTER_W + 时间*pps - 横向滚动值。
- 轨道 y 由轨道数动态算出(轨多时轨道变矮), 见 `_th()`。

v117 交互:
- 拖动 = 自由摆放(可跨轨, 可留空隙), 靠近片段首尾/播放头/0 时吸附(按住 Alt 临时关闭);
- 拖边缘: 默认「裁剪首尾」(视频/音频吸附关键帧并当场告知偏差); F1 面板里可以
  改成「改变倍率」—— 拉长/压短边缘就是改这一段的倍速(v169);
- 片段文字(名字 + 时长/帧率/倍速)画在片段**上方**的薄字带里(v169);
- 轨名区右键 = 加轨/删轨; 底部虚线行 = 添加轨道。
"""
from __future__ import annotations

import math
import os

from PySide6.QtCore import QLineF, QPoint, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QImage,
                           QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtWidgets import QLabel, QLineEdit, QScrollBar, QWidget

from . import theme as T
from .widgets import OutsideClickWatcher, split_name_stem
from ..core import filmstrip as FS
from ..core.clips import MIN_LEN
from ..merge import settings as _settings

RULER_H = 26                    # 标尺高
GUTTER_W = 56                   # 左侧轨名区宽(不随滚动移动)
TRACK_GAP = 6
TRACK_H_MIN, TRACK_H_MAX = 46, 96   # v172: 字带 12pt(24) + 本体 ≥22
HBAR_H = 12
LEFT_PAD = 8
RIGHT_PAD = 24
MIN_PPS = 0.05                  # 绝对下限: 再小就啥也看不见了
MAX_PPS = 240.0
DEFAULT_PPS = 40.0
TRIM_HOT = 6                    # 片段左右边缘多宽算"可裁剪"热区(px)
KF_MIN_GAP = 4                  # 关键帧刻度过密就不画(px)
# v186: "裁剪线正压在关键帧上"的容差(秒)。跳关键帧、红线高亮、截图按钮变蓝
# 必须共用这一个值, 否则会出现"按钮蓝了、线却没对齐"这种互相打架的反馈。
KF_HIT_TOL = 0.02               # 半帧@25fps, 精确到能无损下刀
SNAP_PX = 8                     # 吸附容差(px)
FRAME_CACHE_MAX = 800           # 内存里留多少张缩略图
# v169: 片段文字(名字 + 时长/帧率/倍速)搬到片段**上方**的小字带里。
# 带子薄一点 —— 原来在片段内部要两行字, 现在轨道行 = 字带 + 片段本体。
CAPTION_H = 24                  # v172: 12pt 一行实测 21px, 带子 24
CAPTION_PT = 12                 # 字带字号(v172: 10→12; v171 试过 40, 太大退回)
# 拖边"改变倍速"档的倍速范围(和「音频倍速」对话框一致)
EDGE_SPEED_MIN, EDGE_SPEED_MAX = 0.05, 20.0

_KIND_COLOR = {
    "video": T.ACCENT,          # 蓝
    "image": "#7A5AF8",         # 紫
    "audio": T.OK,              # 绿
}
_KF_COLOR = "#E24B4A"           # 关键帧刻度: 红(能无损下刀的位置)
_KF_HIT_COLOR = "#FF7A3D"       # 裁剪线正压着的那根: 加粗提亮, 和同色裁剪线区分
_SNAP_COLOR = "#8B5CF6"         # 吸附线: 紫
_WAVE_COLOR = "#5FE0C0"         # 波形: 青绿(压暗的缩略图上最跳)
# 检测标记的颜色: 黑场用灰(它本来就是黑的)、静音用蓝、场景变化用橙
_MARK_COLOR = {"black": "#0B1220", "silence": "#3B82F6", "scene": "#F59E0B"}
# 黑场要压暗(它本来就是黑的), 静音只要一层薄蓝
_MARK_ALPHA = {"black": 118, "silence": 62}

_TICK_STEPS = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800]
_MIN_TICK_PX = 64               # 相邻刻度标签至少要隔这么远


def snap_px(v: float) -> int:
    """浮点像素 → 整数像素(四舍五入)。

    ⚠️ 播放头**必须**用它, 不能直接拿浮点 x 去画(v160):
    「跟随移动」时滚动条只能停在整数像素上, 而播放头的时间每帧都在变,
    于是它相对屏幕的位置 = 跟随点 ± 0.5px 来回扫 —— 抗锯齿把三角形画成
    不同灰度, 肉眼看到的就是"裁剪线在小范围晃动"。
    滚动和绘制统一用这一个取整(不用 round: 它的银行家舍入在 .5 上会差 1px),
    跟随点也取整, 播放头就钉死在同一列像素上。
    """
    return int(math.floor(float(v) + 0.5))


def ok_num(*vals) -> bool:
    """几何量是不是正常数字(NaN/inf 会把绘制引擎带崩, 宁可不画)。"""
    for v in vals:
        try:
            if v is None or not math.isfinite(float(v)):
                return False
        except (TypeError, ValueError):
            return False
    return True


def fmt_time(t: float) -> str:
    """秒 → mm:ss.s(供标签/属性面板共用)。"""
    if t < 0:
        t = 0.0
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:04.1f}"


def fmt_dur(t: float) -> str:
    """秒 → "12.3 秒" / "5 分 12.3 秒" / "1 时 05 分"(片段有多长)。

    为什么不和 fmt_time 共用一个格式(v155): 片段块右边那个数写 `00:12.3` 时,
    用户会当成"这一段结束在时间轴 12.3 秒处"。带上"秒/分/时"才一眼看出是长度。
    """
    try:
        t = max(0.0, float(t or 0.0))
    except (TypeError, ValueError):
        t = 0.0
    if t < 60:
        return f"{t:.1f} 秒"
    if t < 3600:
        m = int(t // 60)
        return f"{m} 分 {t - m * 60:.1f} 秒"
    h = int(t // 3600)
    return f"{h} 时 {int((t - h * 3600) // 60):02d} 分"


def _fmt_fps(v: float) -> str:
    """帧率 → "120" / "29.97"(最多两位小数, 整数不带 .0)。"""
    try:
        v = float(v or 0.0)
    except (TypeError, ValueError):
        return "0"
    s = ("%.2f" % v).rstrip("0").rstrip(".")
    return s or "0"


def _fmt_speed(v: float) -> str:
    """倍速 → "1" / "1.5" / "1.511"(v166)。

    用户口径: 最多三位小数, 按实际值显示 —— 末尾是 0 就连小数点一起去掉
    (1.0 → "1", 1.500 → "1.5", 1.511 → "1.511")。
    """
    try:
        v = float(v or 1.0)
    except (TypeError, ValueError):
        v = 1.0
    if v <= 0:
        v = 1.0
    s = ("%.3f" % v).rstrip("0").rstrip(".")
    return s or "1"


class _TrackNameEdit(QLineEdit):
    """轨名 / 片段名就地改名用的小输入框(双击时临时盖在原文字上)。

    为什么不弹 QInputDialog: 改名是个高频小动作, 弹窗要挪手去点确定。
    v193 口径: **只有回车算确认**; 点输入框外面(包括空白处)或 Esc 一律放弃。
    """

    committed = Signal(str)
    cancelled = Signal()

    def __init__(self, text: str, parent=None, *, align=None, font_px: int = 11):
        super().__init__(text or "", parent)
        self._done = False
        self.setObjectName("trackNameEdit")
        # v192: 片段名也用这一套就地输入框 —— 左对齐、字号跟字带走。
        self.setAlignment(Qt.AlignCenter if align is None else align)
        self.setFrame(False)
        self.setStyleSheet(
            f"QLineEdit#trackNameEdit{{background:{T.SURFACE}; color:{T.TEXT};"
            f" border:1px solid {T.ACCENT}; border-radius:3px;"
            f" padding:0px 2px; font-size:{font_px}px;}}")
        self._outside = OutsideClickWatcher(self)
        self._outside.outsideClicked.connect(self._cancel)

    def start_outside(self):
        self._outside.start()

    def stop_outside(self):
        self._outside.stop()

    def add_inside_widget(self, w):
        """v194: 只读后缀标签算输入框内部, 点它不放弃编辑。"""
        self._outside.add_inside(w)

    def keyPressEvent(self, event):
        if self._done:
            return
        key = event.key()
        if key == Qt.Key_Escape:
            self._cancel()
            event.accept()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._done = True
            self.committed.emit(self.text())
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        if not self._done:
            self._cancel()
        super().focusOutEvent(event)

    def _cancel(self):
        if self._done:
            return
        self._done = True
        self.cancelled.emit()


class TimelineWidget(QWidget):
    """时间线: 标尺 + N 条轨道 + 播放头 + 缩略图条。"""

    clipSelected = Signal(object)      # Clip / None
    orderChanged = Signal()            # 片段被拖动/换轨
    playheadChanged = Signal(float)
    sourceDropped = Signal(str, int, float)   # 素材拖进来: 路径 / 轨 / 时间
    trimChanged = Signal(object)       # 裁剪结束, 带被裁的 clip
    zoomChanged = Signal(float)        # 缩放级别变了(pps), 滑轨要跟着走(v169)
    dragFinished = Signal()            # 拖动松手定案(判定"能不能无损"要跟着刷新)
    contextMenuRequested = Signal(object, QPoint)   # clip(可 None), 全局坐标
    trackMenuRequested = Signal(int, QPoint)        # 轨号, 全局坐标
    trackCountChanged = Signal()
    trackRenamed = Signal(int)                      # 轨号(改名后由主窗口落盘)
    clipRenamed = Signal(object)                    # v192: 片段就地改名(Clip)
    frameArrived = Signal(str)                      # 抽帧回来了: 源路径

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timeline = None
        self._pps = DEFAULT_PPS
        self._selected = None          # 主片段(分割/属性面板用它)
        self._sel: list = []           # 选中的全部(Ctrl 点选/框选; 一起拖一起删)
        self._hover = None
        self._scrub = False             # 正在拖播放头
        self._drag = None               # 正在拖片段: dict(clip, x0, t0, moved, from_track)
        self._trim = None               # 正在裁剪: [clip, "left"/"right", 偏差秒]
        self._drop_ind = None           # 从素材区拖入时的落点 (track, time)
        self._snap_line = None          # 命中吸附时画一条竖线
        self._frames: dict = {}         # (source, 量化时间) → QPixmap
        # 已经按格子尺寸缩好的帧(v155): 每重绘一次都 scaled() 一遍太贵 ——
        # 「跟随移动」时整条时间线每帧重绘, 十几格 SmoothTransformation 叠加
        # 就是肉眼可见的掉帧。key = (源, 量化时间, 宽, 高)。
        self._cell_cache: dict = {}
        # 「跟随移动」自己滚的滚动条: 这种滚动**不算**换屏, 别去抽帧队列里清场(v155)
        self._scroll_quiet = 0
        self._bad_ids: set = set()      # 判定为"不能无损"的片段(画红框)
        self._warn_ids: set = set()     # 有副作用但仍能无损的(画黄框)
        self._photo_thumbs: dict = {}   # 图片路径 → QPixmap(主窗口给)
        self._fps_map: dict = {}        # 源路径 → 帧率(片段上写 "120fps/1×")
        self._waves: dict = {}          # 源路径 → 峰值包络(list[float])
        self._marks: dict = {}          # 源路径 → {black/silence/scene: ...}
        self.show_wave = False          # 波形开关(工具条上的「波形」)
        self.show_keyframes = True      # v194: 关键帧刻度全局显示开关(默认开)
        self.crowd = False              # v194: 挤占(插入式落位, 只推目标轨)
        self._insert_ind = None         # v194: 挤占拖动时的插入位置 (track, time)
        self.follow = False             # 「跟随移动」: 播放时裁剪线钉住、轨道滚
        self._follow_x = None           # 钉在控件的哪个横坐标上(None = 下次播时取)
        self._hbar = QScrollBar(Qt.Horizontal, self)
        self._rename_edit = None       # 双击轨名时那个临时输入框
        self._rename_suffix = None     # v194: 片段名输入框右边的只读后缀标签
        self._hbar.setFixedHeight(HBAR_H)
        # 位置变了, 之前排队要抽的那些帧大多已经不在画面里了 → 丢掉,
        # 让线程先去抽"现在看得见的"(不然得等它把屏幕外的抽完才轮到这一屏)
        self._hbar.valueChanged.connect(self._film_drop_stale)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setMinimumHeight(190)
        # ⚠️ 要点一下就能拿到焦点: 素材框一进剪辑就握着焦点, 时间线要是 NoFocus,
        #    Delete 就永远落在素材框上(点过片段也一样) —— 片段删不掉。
        self.setFocusPolicy(Qt.ClickFocus)
        # 滚轮交给主窗口按"快捷键表里录的那套"分派(返回 True = 已处理)。
        # 没接钩子时退回下面写死的 Ctrl+滚轮缩放 / 直接平移。
        self.wheel_hook = None

    # ---------------------------------------------------------------- 数据
    def set_timeline(self, timeline):
        self._end_rename()
        self.timeline = timeline
        self._selected = None
        self._sel = []
        self._hover = None
        self._drag = None
        self._trim = None
        self._scrub = False
        self._sync_scrollbar()
        self.update()
        self.clipSelected.emit(None)

    def _film_drop_stale(self, *_a):
        """画面换了就去重排抽帧的活: 过期的丢掉, 先抽眼前这一屏。

        ⚠️ 「跟随移动」播着时滚动条每帧都在动 —— 要是每次都清队列, 抽帧线程
        刚取的活被不停打断, 缩略图永远抽不出来, 看着就是"轨道内容在掉帧"(v155)。
        这种"自己滚的"只重绘、不清队列。
        """
        if self._scroll_quiet <= 0:
            w = getattr(self, "_film_worker", None)
            try:
                if w is not None and hasattr(w, "clear_pending"):
                    w.clear_pending()
            except Exception:
                pass
        self.update()

    def selected_clip(self):
        clips = self.selected_clips()
        return clips[0] if clips else None

    def selected_clips(self) -> list:
        """当前选中的全部片段(Ctrl 点选出来的多个)。

        ⚠️ 必须过滤掉"已经不在时间线上"的: 删片段只动了 `timeline.clips`,
        `_sel` 里还留着那个对象。不过滤的话, 删完再按 S / C / 点工具按钮,
        动的还是那个**已经删掉的**片段 —— 用户看到的是"按了没反应"或
        "截图截出刚才删掉那段"(v158)。
        """
        tl = getattr(self, "timeline", None)
        out = [c for c in self._sel if c is not None]
        if tl is not None and getattr(tl, "clips", None) is not None:
            out = [c for c in out if c in tl.clips]
        return out

    def select_clip(self, clip):
        self._set_selection([clip] if clip is not None else [])

    def select_clips(self, clips):
        self._set_selection(list(clips or []))

    def _set_selection(self, clips: list):
        """统一入口: 单选/多选/清空都走这里, 主片段永远是第一个。"""
        self._sel = [c for c in clips if c is not None]
        self._selected = self._sel[0] if self._sel else None
        self.update()

    def is_selected(self, clip) -> bool:
        return any(c is clip for c in self._sel)

    def set_photo_thumbs(self, mapping: dict):
        """图片片段没有抽帧流程, 直接用主列表已经生成好的缩略图。"""
        self._photo_thumbs = dict(mapping or {})
        self.update()

    def on_frame(self, source: str, t: float, pix):
        """抽帧线程回来了: 存进内存缓存并重绘。

        ⚠️ 后台线程只能给 QImage(QPixmap 只属于界面线程), 这里统一转成 QPixmap。
        """
        if pix is None or pix.isNull():
            return
        if isinstance(pix, QImage):
            pix = QPixmap.fromImage(pix)
            if pix.isNull():
                return
        if len(self._frames) >= FRAME_CACHE_MAX:
            # ⚠️ 别整个 clear(): 来回缩放/前后滚的时候等于白抽一遍,
            #    画面又是空的 —— 丢最旧的四分之一就够了(dict 按插入顺序)。
            for k in list(self._frames)[:max(1, FRAME_CACHE_MAX // 4)]:
                self._frames.pop(k, None)
        # 键就是抽帧那边算好的量化值, 不要再 quantize 一次(两次浮点运算未必逐位相同,
        # 键对不上就等于这张白抽了 —— 画的时候按同一个 FS.quantize 算)。
        self._frames[(source, float(t))] = pix
        # 右栏卡片可能正好缺图(主列表那张还没生成), 给它一张现成的
        self.frameArrived.emit(str(source))
        self.update()

    # ---------------------------------------------------------------- 波形/检测标记
    def set_waveform(self, source: str, peaks):
        """某个源文件的峰值包络(0~1, 均分覆盖整段素材)。"""
        if source:
            self._waves[source] = list(peaks or [])
        self.update()

    def waveform_of(self, source: str):
        return self._waves.get(source) or []

    def has_waveform(self, source: str) -> bool:
        return bool(self._waves.get(source))

    def set_wave_visible(self, on: bool):
        self.show_wave = bool(on)
        self.update()

    def set_keyframes_visible(self, on: bool):
        """v194: 全局关键帧刻度显示开关(只管画不画, 数据由后台探测补)。"""
        self.show_keyframes = bool(on)
        self.update()

    def set_crowd(self, on: bool):
        """v194: 挤占开关(拖动时按插入式落位, 松手顺延同轨后面的片段)。"""
        self.crowd = bool(on)
        if not self.crowd:
            self._insert_ind = None
        self.update()

    def set_marks(self, by_source: dict | None):
        """检测结果: {源路径: {"black": [(s,e)], "silence": [(s,e)], "scene": [t]}}。

        存的是**源时间**, 画的时候按每个片段自己的 in/out 换算成时间线位置,
        所以拖动/裁剪/缩放之后标记不会错位。
        """
        self._marks = dict(by_source or {})
        self.update()

    def _src_to_global(self, clip, t: float):
        """源时间 → 时间线时间; 落在片段外返回 None。"""
        if t < clip.src_in - 1e-6 or t > clip.src_out + 1e-6:
            return None
        return clip.start + (t - clip.src_in) / (clip.speed or 1.0)

    def set_loss_marks(self, bad=None, warn=None):
        """标出「不能无损」/「有副作用」的片段: 传进来的是 id(clip) 集合。"""
        self._bad_ids = set(bad or ())
        self._warn_ids = set(warn or ())
        self.update()

    def set_fps_map(self, mapping: dict | None):
        """源路径 → 帧率(v166)。片段右下角那行要写 "120fps/1.5"。

        帧率不在 Clip 上(它属于源文件), 所以由主窗口按 merge_specs 算好传进来;
        没有就不写第二行(比如音频、读不出帧率的)。
        """
        new = dict(mapping or {})
        if new == self._fps_map:
            return
        self._fps_map = new
        self.update()

    def cached_frame(self, source: str):
        """取这个源已经抽到的任意一帧(预览窗没缩略图时拿它兜底)。"""
        for (src, _t), pix in self._frames.items():
            if src == source and pix is not None and not pix.isNull():
                return pix
        return None

    def refresh(self):
        """数据被外部改动后重算: 选中项若已被删掉就清空, 再重绘。"""
        if self.timeline is None:
            return
        # 已被删掉的片段要从选中里拿掉(多选时可能一次删好几个)
        alive = [c for c in self._sel
                 if any(c is x for x in self.timeline.clips)]
        if len(alive) != len(self._sel):
            self._sel = alive
            self._selected = alive[0] if alive else None
            self.clipSelected.emit(self._selected)
        self._sync_scrollbar()
        self.update()
        self.orderChanged.emit()      # 外部靠它刷新时间读数

    def tracks(self) -> int:
        return self.timeline.track_count if self.timeline else 3

    # ---------------------------------------------------------------- 几何
    def _th(self) -> float:
        """轨道高度: 轨多就变矮, 保证所有轨都塞得下。"""
        n = max(1, self.tracks())
        avail = (self.height() - RULER_H - HBAR_H - (n + 1) * TRACK_GAP)
        return max(TRACK_H_MIN, min(TRACK_H_MAX, avail / n))

    def _track_y(self, track: int) -> float:
        return RULER_H + TRACK_GAP + track * (self._th() + TRACK_GAP)

    def _content_bottom(self) -> float:
        return self._track_y(max(0, self.tracks() - 1)) + self._th()

    def _track_at_y(self, y: float) -> int | None:
        """y 落在哪条轨上(None=不在任何轨上)。"""
        if y <= RULER_H:
            return None
        th = self._th()
        for track in range(self.tracks()):
            ty = self._track_y(track)
            if ty <= y <= ty + th:
                return track
        return None

    def _track_label(self, track: int) -> str:
        if self.timeline is not None:
            try:
                return self.timeline.track_label(track)
            except Exception:
                pass
        return f"轨 {track + 1}"

    def _time_to_x(self, t: float) -> float:
        return GUTTER_W + LEFT_PAD + t * self._pps - self._hbar.value()

    def _x_to_time(self, x: float) -> float:
        return max(0.0, (x + self._hbar.value() - GUTTER_W - LEFT_PAD) / self._pps)

    def _clip_rect(self, clip) -> QRectF:
        """片段**本体**的矩形(v169 起: 轨道行顶部那 CAPTION_H 留给小字带)。"""
        if not ok_num(clip.start, clip.duration) or not ok_num(self._pps):
            return QRectF()
        x = self._time_to_x(clip.start)
        w = max(6.0, clip.duration * self._pps)
        th = self._th()
        return QRectF(x, self._track_y(clip.track) + CAPTION_H,
                      w, max(6.0, th - CAPTION_H))

    def _caption_rect(self, clip) -> QRectF:
        """片段上方那条小字带(名字 + 时长/帧率/倍速, v169)。"""
        if not ok_num(clip.start, clip.duration) or not ok_num(self._pps):
            return QRectF()
        x = self._time_to_x(clip.start)
        w = max(6.0, clip.duration * self._pps)
        return QRectF(x, self._track_y(clip.track), w, CAPTION_H)

    def _clip_zone(self, clip) -> QRectF:
        """命中判定用的矩形 = 整个轨道行里属于这个片段的范围(含小字带)。"""
        if not ok_num(clip.start, clip.duration) or not ok_num(self._pps):
            return QRectF()
        x = self._time_to_x(clip.start)
        w = max(6.0, clip.duration * self._pps)
        th = self._th()
        return QRectF(x, self._track_y(clip.track) + 2, w, th - 4)

    def _clip_at(self, x: float, y: float):
        if self.timeline is None:
            return None
        for track in range(self.tracks()):
            ty = self._track_y(track)
            if not (ty <= y <= ty + self._th()):
                continue
            for clip in self.timeline.track_clips(track):
                if self._clip_zone(clip).contains(x, y):
                    return clip
        return None

    def _playhead_x(self, t: float | None = None) -> float:
        """播放头画在屏幕上的横坐标: **整数像素**(见 snap_px 的说明)。

        跟随移动时它相对滚动条是"小数在扫", 只有量化到整数才不会左右微晃。
        """
        tl = self.timeline
        if t is None:
            t = tl.playhead if tl is not None else 0.0
        if not ok_num(t, self._pps):
            return float(GUTTER_W)
        return float(snap_px(self._time_to_x(float(t))))

    def _playhead_handle_rect(self) -> QRectF:
        x = self._playhead_x()
        return QRectF(x - 7, 2, 14, RULER_H - 2)

    def _content_width(self) -> float:
        total = self.timeline.total_duration() if self.timeline else 0.0
        return GUTTER_W + LEFT_PAD + total * self._pps + RIGHT_PAD

    def _sync_scrollbar(self):
        over = max(0.0, self._content_width() - self.width())
        self._hbar.setRange(0, int(over))
        self._hbar.setPageStep(max(10, int(self.width() * 0.9)))
        self._hbar.setSingleStep(max(8, int(24 * self._pps / DEFAULT_PPS)))

    def _snap_tol(self) -> float:
        """吸附容差换算成秒。"""
        return SNAP_PX / max(self._pps, 1e-6)

    # ---------------------------------------------------------------- 绘制
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect()
        p.fillRect(r, QColor(T.SURFACE))

        n = self.tracks()
        th = self._th()

        # ---- 轨道底 + 轨名 ----
        for track in range(n):
            ty = self._track_y(track)
            lane = QRectF(0, ty, r.width(), th)
            p.fillRect(lane, QColor(T.SURFACE_ALT if track % 2 else T.SURFACE))
            p.setPen(QPen(QColor(T.BORDER), 1))
            p.drawLine(0, int(ty), int(r.width()), int(ty))
            p.setPen(QColor(T.TEXT_WEAK))
            f = QFont()
            f.setPointSize(8)
            p.setFont(f)
            name = self._track_label(track)
            name = QFontMetrics(f).elidedText(name, Qt.ElideRight, int(GUTTER_W) - 8)
            p.drawText(QRectF(0, ty, GUTTER_W, th), Qt.AlignCenter, name)
        p.setPen(QPen(QColor(T.BORDER), 1))
        p.drawLine(0, int(self._content_bottom()), int(r.width()),
                   int(self._content_bottom()))
        p.drawLine(int(GUTTER_W), 0, int(GUTTER_W), int(self._content_bottom()))

        # ---- 标尺 ----
        ruler = QRectF(0, 0, r.width(), RULER_H)
        p.fillRect(ruler, QColor(T.HEADER_BG))
        p.setPen(QPen(QColor(T.BORDER), 1))
        p.drawLine(0, RULER_H, int(r.width()), RULER_H)
        if self.timeline is not None:
            step = self._tick_step()
            t0 = max(0.0, self._x_to_time(0))
            t1 = self._x_to_time(r.width())
            t = (int(t0 / step)) * step
            f = QFont()
            f.setPointSize(9)
            p.setFont(f)
            # 只画在轨名区右边(否则第一个标签会被切在 GUTTER_W 上)
            p.save()
            p.setClipRect(QRectF(GUTTER_W, 0, max(0.0, r.width() - GUTTER_W),
                                 RULER_H + 2))
            while t <= t1 + step:
                x = self._time_to_x(t)
                if x >= GUTTER_W:
                    major = abs(t / max(step, 1e-6) - round(t / max(step, 1e-6))) < 1e-6
                    p.setPen(QPen(QColor(T.BORDER_STRONG if major else T.BORDER), 1))
                    p.drawLine(int(x), int(RULER_H - (8 if major else 4)),
                               int(x), int(RULER_H))
                    if major:
                        p.setPen(QColor(T.TEXT_WEAK))
                        label = fmt_time(t)
                        if step >= 60:
                            m, s = divmod(int(round(t)), 60)
                            label = f"{m}:{s:02d}"
                        p.drawText(QRectF(x - 40, 0, 80, RULER_H - 9),
                                   Qt.AlignCenter, label)
                t += step
            p.restore()

        # ---- 片段(裁到内容区, 免得盖住轨名) ----
        if self.timeline is not None:
            p.save()
            p.setClipRect(QRectF(GUTTER_W + 1, RULER_H,
                                 r.width() - GUTTER_W - 1,
                                 max(0.0, self._content_bottom() - RULER_H)))
            for track in range(n):
                for clip in self.timeline.track_clips(track):
                    self._paint_clip(p, clip)
            p.restore()

        # ---- 吸附线 ----
        if self._snap_line is not None:
            x = self._time_to_x(self._snap_line)
            if x >= GUTTER_W:
                pen = QPen(QColor(_SNAP_COLOR), 1, Qt.DashLine)
                p.setPen(pen)
                p.drawLine(int(x), int(RULER_H), int(x), int(self._content_bottom()))

        # ---- 裁剪时的落点提示气泡 ----
        if self._trim is not None:
            self._paint_trim_bubble(p)

        # ---- 从素材区拖入的落点 ----
        if self._drop_ind is not None:
            track, t = self._drop_ind
            ty = self._track_y(track)
            bx = self._time_to_x(t)
            pen = QPen(QColor(T.ACCENT), 2)
            p.setPen(pen)
            p.drawLine(int(bx), int(ty + 2), int(bx), int(ty + th - 2))
            head = QRectF(bx - 5, ty + 2, 10, 8)
            path = QPainterPath()
            path.moveTo(head.left(), head.top())
            path.lineTo(head.right(), head.top())
            path.lineTo(head.center().x(), head.bottom())
            path.closeSubpath()
            p.fillPath(path, QColor(T.ACCENT))

        # ---- v194 挤占: 插入位置(只推目标轨, 松手才真的顺延) ----
        if self._insert_ind is not None:
            track, t = self._insert_ind
            ty = self._track_y(track)
            bx = self._time_to_x(t)
            pen = QPen(QColor(T.ACCENT), 2, Qt.DashLine)
            p.setPen(pen)
            p.drawLine(int(bx), int(ty + 2), int(bx), int(ty + th - 2))

        # ---- 播放头 ----
        if self.timeline is not None:
            x = self._playhead_x()
            if x >= GUTTER_W:
                p.setPen(QPen(QColor(T.DANGER), 2))
                p.drawLine(int(x), int(RULER_H), int(x), int(self._content_bottom()))
            handle = QRectF(x - 6, 3, 12, 12)
            path = QPainterPath()
            path.moveTo(handle.left(), handle.top())
            path.lineTo(handle.right(), handle.top())
            path.lineTo(handle.center().x(), handle.bottom())
            path.closeSubpath()
            p.fillPath(path, QColor(T.DANGER))

        # ---- 关键帧命中高亮(v186) ----
        # 裁剪线和关键帧刻度都是红的, 重合时根本分不出"到底落没落上"; 片段又是
        # 先画、播放头后画, 所以这根必须画在播放头**之后**才不会被盖掉。
        # 命中时把片段里那截线加粗成橙色: 蓝按钮 + 橙线 + 窗口内回执, 三重反馈。
        if self.timeline is not None:
            t_now = float(self.timeline.playhead or 0.0)
            for c in self.timeline.clips:
                if getattr(c, "kind", None) != "video":
                    continue
                try:
                    kts = c.timeline_keyframe_times()
                except Exception:
                    continue
                if not any(abs(k - t_now) <= KF_HIT_TOL for k in kts):
                    continue
                cr = self._clip_rect(c)
                if cr.isNull() or not ok_num(cr.width(), cr.height()):
                    continue
                hx = self._time_to_x(t_now)
                if hx < GUTTER_W or hx > self.width():
                    continue
                p.setRenderHint(QPainter.Antialiasing, False)
                p.setPen(QPen(QColor(_KF_HIT_COLOR), 3))
                p.drawLine(int(hx), int(cr.top()), int(hx), int(cr.bottom()))

        p.end()

    # ---------------------------------------------------------------- 片段
    def _paint_clip(self, p: QPainter, clip):
        rect = self._clip_rect(clip)
        if rect.isNull() or not ok_num(rect.width(), rect.height()):
            return
        if rect.right() < GUTTER_W or rect.left() > self.width():
            return
        color = QColor(_KIND_COLOR.get(clip.kind, T.TEXT_WEAK))
        selected = self.is_selected(clip)
        hovered = clip is self._hover and not selected
        path = QPainterPath()
        path.addRoundedRect(rect, 4, 4)

        p.fillPath(path, color.lighter(118) if hovered else color)

        # ---- 缩略图条: 按像素位置平铺真帧(视频) / 重复同一张(图片) ----
        if rect.width() >= 20 and clip.kind in ("video", "image"):
            p.save()
            p.setClipPath(path, Qt.IntersectClip)
            self._paint_filmstrip(p, clip, rect)
            p.restore()

        # 描边优先级: 不能无损(红) > 有副作用(黄) > 默认(浅白)
        if id(clip) in self._bad_ids:
            p.setPen(QPen(QColor(T.DANGER), 2))
        elif id(clip) in self._warn_ids:
            p.setPen(QPen(QColor(T.WARN), 2))
        else:
            p.setPen(QPen(QColor(255, 255, 255, 150), 1))
        # ⚠️ 必须清掉 brush: drawPath 会用当前 brush **填充整块**, 只描边就点破相了
        #    (踩过: 上一段"不能无损"的红三角把 brush 留成红色, 后面每段都被涂成一片红)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

        # ---- 选中: 外面套一圈包裹框(不压住缩略图, 一眼看出选的是哪块) ----
        if selected:
            frame = QRectF(rect).adjusted(-3, -3, 3, 3)
            # 主片段实线粗框, 其余选中的细一点 —— 一眼看出"删/切作用在哪个上"
            main = clip is self._selected
            p.setPen(QPen(QColor(T.ACCENT), 2 if main else 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(frame, 6, 6)
        if id(clip) in self._bad_ids and rect.width() > 24:
            # 右上角一个小红角: 缩略图花的时候也能一眼看到"这段有问题"
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T.DANGER))
            tri = QPainterPath()
            tx, ty = rect.right() - 13, rect.top() + 1
            tri.moveTo(tx, ty)
            tri.lineTo(tx + 12, ty)
            tri.lineTo(tx + 12, ty + 12)
            tri.closeSubpath()
            p.fillPath(tri, QColor(T.DANGER))
            p.setBrush(Qt.NoBrush)

        # ---- 声音波形: 视频压在底部、音频铺满整块(开关在工具条) ----
        # ⚠️ 没声音的地方不画: 静音的、声音被分离走的(比如分离音频后的原视频)
        if self.show_wave and not clip.silent and rect.width() >= 12 \
                and clip.kind in ("video", "audio"):
            p.save()
            p.setClipPath(path, Qt.IntersectClip)
            self._paint_wave(p, clip, rect)
            p.restore()

        # ---- 检测标记(黑场/静音色带 + 场景变化虚线) ----
        self._paint_marks_for(p, clip, rect, path)

        # ---- 关键帧刻度: 能无损下刀的位置(红的细竖线) ----
        # v186: 和跳关键帧/截图按钮共用 Clip.timeline_keyframe_times()(含倍速),
        #       以前这里除 speed、跳转那条不除, 变速片段的红线和落点对不上。
        # v194: 「关键帧」按钮关掉时全局不画刻度(数据还在, Alt+←/→ 照常能跳)
        if self.show_keyframes and clip.kind == "video" and clip.keyframes \
                and rect.width() >= 12 and self._th() >= 24:
            p.save()
            p.setClipPath(path, Qt.IntersectClip)
            # 关掉抗锯齿: 1px 竖线被抹成两个半透明像素就又细又虚, 看不出"能切在哪"
            p.setRenderHint(QPainter.Antialiasing, False)
            p.setPen(QPen(QColor(_KF_COLOR), 1))
            last_x = -1e9
            for t in clip.timeline_keyframe_times():
                x = self._time_to_x(t)
                if x < GUTTER_W or x > self.width() or x - last_x < KF_MIN_GAP:
                    continue
                last_x = x
                p.drawLine(int(x), int(rect.top()), int(x), int(rect.bottom()))
            p.restore()

        # ---- 文字(v169): 全部搬去片段上方的薄字带, 片段本体干干净净 ----
        self._paint_caption(p, clip)

    def caption_text(self, clip) -> str:
        """字带右边的硬信息: 视频「时长 帧率/倍速」, 音频「时长 倍速」。"""
        info = self.clip_info_text(clip)
        dur = fmt_dur(clip.duration)
        return f"{dur}  {info}" if info else dur

    def _paint_caption(self, p: QPainter, clip):
        """片段上方的小字带: 名字(左) + 时长 帧率/倍速(右)。

        音频没有帧率 → 右边是「时长 倍速」; 视频是「时长 帧率/倍速」。
        字带只有一行, 高度 CAPTION_H(v170 起 10pt —— 8pt 时用户嫌看不清)。
        """
        cap = self._caption_rect(clip)
        if cap.isNull() or not ok_num(cap.width()) or cap.right() < GUTTER_W:
            return
        f = QFont()
        f.setPointSize(CAPTION_PT)
        p.setFont(f)
        fm = QFontMetrics(f)
        right = self.caption_text(clip)
        right_w = fm.horizontalAdvance(right)
        # 右边是硬信息(时长/倍速): 窄的时候宁可丢名字, 也不能把时长挤没了
        cap = cap.adjusted(4, 0, -4, 0)
        if cap.width() < right_w + 8:
            right = fmt_dur(clip.duration)
            right_w = fm.horizontalAdvance(right)
        if cap.width() < right_w + 8:
            right = fm.elidedText(right, Qt.ElideRight, max(10, int(cap.width())))
            right_w = fm.horizontalAdvance(right)
        name = ""
        if cap.width() > right_w + 30:
            if clip.detached:
                label = clip.name + "（音轨已分离）"
            elif clip.muted:
                label = clip.name + "（静音）"
            else:
                label = clip.name
            name = fm.elidedText(label, Qt.ElideRight,
                                 max(10, int(cap.width()) - right_w - 8))
        color = self.caption_color(clip)
        rr = QRectF(cap.right() - right_w, cap.top(), right_w, cap.height())
        p.setPen(color)
        p.drawText(rr, Qt.AlignVCenter | Qt.AlignRight | Qt.TextSingleLine, right)
        if name:
            lr = QRectF(cap.left(), cap.top(),
                        cap.width() - right_w - 8, cap.height())
            p.drawText(lr, Qt.AlignVCenter | Qt.AlignLeft | Qt.TextSingleLine, name)

    def caption_color(self, clip) -> QColor:
        """字带文字颜色: 没选=次要灰; 选中=主题蓝(和选中框同色)。

        ⚠️ 以前选中写白色 —— 字带已经搬到片段**上方**、底色是轨道浅色,
        白字上去直接看不见(v171 用户报"选中时字体消失")。
        """
        return QColor(T.ACCENT if self.is_selected(clip) else T.TEXT_SUB)

    def clip_info_text(self, clip) -> str:
        """片段上方的硬信息: 视频「120fps/1.511×」, 音频只有倍率「1.5×」(v167)。

        音频没有帧率 → 永远显示当前倍率(包括 1×);
        读不出帧率的视频只有在倍率 ≠ 1 时才写「1.5×」, 平时不凑这个热闹。

        v187: 视频这个帧率写的是**等效帧率** = 源帧率 × 倍速, 和工具条
        「帧率/倍速」按钮同一套口径 —— 拖片段边缘改了倍率, 帧率文字要跟着变,
        不能还挂着源文件那个旧数字。
        """
        if clip is None:
            return ""
        try:
            speed = float(getattr(clip, "speed", 1.0) or 1.0)
        except Exception:
            speed = 1.0
        if speed <= 0:
            speed = 1.0
        if getattr(clip, "kind", "") == "audio":
            return "%s×" % _fmt_speed(speed)
        try:
            fps = float(self._fps_map.get(clip.source) or 0.0)
        except Exception:
            fps = 0.0
        if not (0 < fps < 480):
            # 帧率不知道的视频: 只有在改过速度时才值得写这一行
            if abs(speed - 1.0) > 1e-6:
                return "%s×" % _fmt_speed(speed)
            return ""
        return "%sfps/%s×" % (_fmt_fps(fps * speed), _fmt_speed(speed))

    def _paint_wave(self, p: QPainter, clip, rect: QRectF):
        """把峰值包络贴在片段上(每列一条竖线, 批量 drawLines 才不卡)。

        只在"确实有声音"的片段上画: 静音的、声音被分离走的(分离音频后的原视频)不画
        —— 哪里真有声音, 波形才出现在哪里。
        """
        if clip.silent:
            return
        peaks = self._waves.get(clip.source)
        if not peaks:
            return
        src_dur = float(clip.src_duration or 0.0) or float(clip.src_out or 0.0)
        if src_dur <= 0:
            src_dur = float(len(peaks))
        speed = clip.speed or 1.0
        if clip.kind == "audio":
            band = QRectF(rect.left(), rect.top() + 2, rect.width(), rect.height() - 4)
        else:
            h = min(rect.height() * 0.42, max(8.0, rect.height() - 16))
            band = QRectF(rect.left(), rect.bottom() - h - 1, rect.width(), h)
            p.fillRect(band, QColor(0, 0, 0, 100))      # 先压暗, 波形才跳得出来
        mid = band.center().y()
        half = max(1.0, band.height() / 2 - 1)
        n = len(peaks)
        x0 = max(int(rect.left()), GUTTER_W)
        x1 = min(int(rect.right()), self.width())
        if x1 <= x0:
            return
        lines = []
        for x in range(x0, x1 + 1):
            t_src = clip.src_in + (x - rect.left()) / max(self._pps, 1e-6) * speed
            idx = int(t_src / src_dur * n)
            if idx < 0:
                idx = 0
            if idx >= n:
                idx = n - 1
            amp = peaks[idx] * half
            if amp < 0.4:
                amp = 0.4
            lines.append(QLineF(x, mid - amp, x, mid + amp))
        pen = QPen(QColor(_WAVE_COLOR), 1)
        p.setPen(pen)
        p.drawLines(lines)

    def _paint_marks_for(self, p: QPainter, clip, rect: QRectF, path):
        """黑场/静音画半透明色带, 场景变化画橙色虚线(全在片段内部, 不越界)。"""
        data = self._marks.get(clip.source)
        if not data or clip is None:
            return
        p.save()
        p.setClipPath(path, Qt.IntersectClip)
        for kind in ("black", "silence"):
            spans = data.get(kind) or []
            if not spans:
                continue
            col = QColor(_MARK_COLOR[kind])
            fill = QColor(col)
            fill.setAlpha(_MARK_ALPHA.get(kind, 60))
            for item in spans:
                try:
                    s0, e0 = float(item[0]), float(item[1])
                except Exception:
                    continue
                a = self._src_to_global(clip, s0)
                b = self._src_to_global(clip, e0)
                if a is None:
                    if s0 < clip.src_in:
                        a = clip.start
                    else:
                        continue
                if b is None:
                    if e0 > clip.src_out:
                        b = clip.end
                    else:
                        continue
                if b <= a:
                    continue
                x1 = self._time_to_x(a)
                x2 = self._time_to_x(b)
                p.fillRect(QRectF(x1, rect.top(), max(2.0, x2 - x1), rect.height()), fill)
                p.setPen(QPen(col, 2))
                p.drawLine(int(x1), int(rect.top()), int(x1), int(rect.bottom()))
                # 右端再画一条淡一点的线: 一眼看出这段从哪结束
                end_col = QColor(col)
                end_col.setAlpha(150)
                p.setPen(QPen(end_col, 1))
                p.drawLine(int(x2), int(rect.top()), int(x2), int(rect.bottom()))
        scenes = data.get("scene") or []
        if scenes:
            pen = QPen(QColor(_MARK_COLOR["scene"]), 2, Qt.DashLine)
            p.setPen(pen)
            for t in scenes:
                try:
                    g = self._src_to_global(clip, float(t))
                except Exception:
                    continue
                if g is None:
                    continue
                x = self._time_to_x(g)
                p.drawLine(int(x), int(rect.top() + 1), int(x), int(rect.bottom() - 1))
        p.restore()

    def _paint_filmstrip(self, p: QPainter, clip, rect: QRectF):
        """在片段里按格子平铺帧; 缺的帧丢给后台抽帧线程。

        ⚠️ 只画**看得见的那几个格子**: 放大后一个长片段能有几千格宽, 全画一遍
        既卡又会把抽帧队列塞爆(用户报"缩放时闪退"就发生在这个量级上)。
        """
        th = rect.height()
        fw = max(28.0, min(96.0, th * 16.0 / 9.0))
        speed = clip.speed or 1.0
        need: list = []

        if not ok_num(self._pps) or self._pps <= 0:
            return
        # 一格代表多少秒素材 → 用哪一档"代表帧"粒度。
        # 看全貌时一格是好几分钟, 每格都抽一张纯属浪费(肉眼根本分不出来),
        # 抽的张数少几倍, 显示自然就快; 放大后再回到细粒度。
        step = FS.step_for((fw / self._pps) * speed)
        vis_l = max(rect.left(), GUTTER_W - fw)
        vis_r = min(rect.right(), self.width() + fw)
        if vis_r <= vis_l:
            return
        # 对齐到格子边界, 滚动/缩放时画面才不会"抖"
        x = rect.left() + int((vis_l - rect.left()) / fw) * fw
        x_end = vis_r
        while x < x_end:
            # 这一格中心对应的源时间
            center = (x + fw / 2) - rect.left()
            t_src = clip.src_in + (center / self._pps) * speed
            t_src = max(clip.src_in, min(clip.src_out, t_src))

            if clip.kind == "image":
                pm = self._photo_thumbs.get(clip.source)
                if pm is not None and not pm.isNull():
                    self._draw_frame(p, pm, QRectF(x, rect.top(), fw, th),
                                     (clip.source, -1.0))
            else:
                q = FS.quantize(t_src, step)
                pm = self._frames.get((clip.source, q))
                if pm is not None and not pm.isNull():
                    self._draw_frame(p, pm, QRectF(x, rect.top(), fw, th),
                                     (clip.source, q))
                elif self._frame_worth_asking(clip, q):
                    need.append((q, step))
            x += fw

        if need and self._film_worker is not None:
            self._film_worker.request(clip.source, need)

    @staticmethod
    def _frame_worth_asking(clip, t_src: float) -> bool:
        """时间点已经超出素材本身的时长, 就别去抽了 —— 最后一格最常踩到,
        抽不出来不说, 后台还会一直起子进程去撞同一个失败。"""
        try:
            dur = float(getattr(clip, "src_duration", 0.0) or 0.0)
        except Exception:
            return True
        if dur <= 0:
            return True
        return t_src < dur - 0.05

    def _scaled_cell(self, key, pm: QPixmap, w: float, h: float):
        """按格子尺寸缩好的帧(带缓存)。

        `QPixmap.scaled(...SmoothTransformation)` 每格每帧都要重算一次,
        播着「跟随移动」时整条时间线每帧重绘, 十几格叠起来就是掉帧(v155)。
        缩放结果只跟 (源, 量化时间, 格子宽高) 有关, 存下来直接用。
        """
        k = (key, int(w), int(h))
        got = self._cell_cache.get(k)
        if got is not None:
            return got
        scaled = pm.scaled(int(w), int(h),
                           Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        if len(self._cell_cache) > 600:
            self._cell_cache.clear()      # 缩放/换素材才会走到这里, 清一次不要紧
        self._cell_cache[k] = scaled
        return scaled

    def _draw_frame(self, p: QPainter, pm: QPixmap, cell: QRectF, key=None):
        """按"覆盖"方式贴一格: 保持比例, 填满格子, 居中裁切。"""
        if cell.width() < 2 or cell.height() < 2:
            return
        if key is None:
            scaled = pm.scaled(int(cell.width()), int(cell.height()),
                               Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        else:
            scaled = self._scaled_cell(key, pm, cell.width(), cell.height())
        p.drawPixmap(int(cell.left() - (scaled.width() - cell.width()) / 2),
                     int(cell.top() - (scaled.height() - cell.height()) / 2),
                     scaled)

    def _paint_trim_bubble(self, p: QPainter):
        """裁剪时浮在片段上方: 告诉用户刀实际落在哪、偏了多少。"""
        clip = self._trim[0]
        rect = self._clip_zone(clip)
        text = self._trim_bubble_text()
        if not text:
            return
        f = QFont()
        f.setPointSize(9)
        p.setFont(f)
        fm = QFontMetrics(f)
        w = fm.horizontalAdvance(text) + 18
        h = 22
        x = max(4.0, min(rect.center().x() - w / 2, self.width() - w - 4))
        y = max(2.0, rect.top() - h - 3)
        box = QRectF(x, y, w, h)
        off = self._trim[2]
        edge = QColor(T.DANGER if off > 0.005 else T.OK)
        p.setPen(QPen(edge, 1))
        p.setBrush(QColor(T.SURFACE))
        p.drawRoundedRect(box, 5, 5)
        p.setPen(QColor(T.TEXT))
        p.drawText(box, Qt.AlignCenter, text)

    def _tick_step(self) -> float:
        for step in _TICK_STEPS:
            if step * self._pps >= _MIN_TICK_PX:
                return step
        return _TICK_STEPS[-1]

    # ---------------------------------------------------------------- 缩放
    def fit_pps(self) -> float:
        """整条时间线刚好铺满可视宽度所需的 pps。"""
        total = self.total_duration()
        if total <= 0:
            return DEFAULT_PPS
        avail = max(80.0, self.width() - GUTTER_W - LEFT_PAD - RIGHT_PAD)
        return avail / max(0.1, total)

    def min_pps(self) -> float:
        """缩放下限: 最小就是"全览", 保证所有已加入的块都在画面里。"""
        return max(MIN_PPS, self.fit_pps())

    def _zoom_anchor(self) -> float:
        """缩放锚点 = **裁剪线**(播放头)在视口里的 x(v135)。

        为什么不再用鼠标位置: 滚轮缩放时手会抖, 画面就绕着鼠标乱飘,
        你正在看的那一帧被推着跑。围着裁剪线缩放, 那一帧永远钉在原地。
        裁剪线滚出视口时退回画面中心 —— 否则放大后会把看不到的地方拉进来。
        """
        try:
            tl = self.timeline
            if tl is not None:
                x = self._playhead_x()      # 和画出来的位置同一取整(v160)
                if 0.0 <= x <= self.width():
                    return x
        except Exception:
            pass
        return self.width() / 2

    def set_pps(self, value: float, anchor_x: float | None = None):
        lo = self.min_pps()
        value = max(lo, min(MAX_PPS, float(value)))
        at_min = value <= lo + 1e-9
        if at_min:
            # 缩到最小 = 全览: 整条时间线刚好铺满, 必须从最左开始显示,
            # 否则会停在中间、开头那截反而看不到(用户报的问题)。
            value = lo
        if abs(value - self._pps) < 1e-9 and not at_min:
            return
        anchor_x = self._zoom_anchor() if anchor_x is None else anchor_x
        t_anchor = self._x_to_time(anchor_x)
        self._pps = value
        self.zoomChanged.emit(float(value))
        # 缩放级别变了 → 需要的"代表帧"粒度也变了, 旧需求全作废
        self._film_drop_stale()
        self._sync_scrollbar()
        if at_min:
            self._hbar.setValue(0)
            self.update()
            return
        dx = self._time_to_x(t_anchor) - anchor_x
        self._hbar.setValue(int(max(self._hbar.minimum(),
                                    min(self._hbar.maximum(),
                                        self._hbar.value() + dx))))
        self.update()

    def zoom_in(self):
        self.set_pps(self._pps * 1.25)

    def zoom_out(self):
        self.set_pps(self._pps / 1.25)

    def fit_timeline(self):
        if self.timeline is None or self.total_duration() <= 0:
            return
        self.set_pps(self.fit_pps())

    def total_duration(self) -> float:
        return self.timeline.total_duration() if self.timeline else 0.0

    # ---------------------------------------------------------------- 裁剪
    def _trim_side(self, clip, x: float, y: float):
        """鼠标落在片段的哪条边上(在热区内才返回); 不在边上返回 None。"""
        rect = self._clip_zone(clip)
        if not (rect.top() <= y <= rect.bottom()):
            return None
        if abs(x - rect.left()) <= TRIM_HOT and self._side_ok(clip, "left"):
            return "left"
        if abs(x - rect.right()) <= TRIM_HOT and self._side_ok(clip, "right"):
            return "right"
        return None

    @staticmethod
    def _side_ok(clip, side: str) -> bool:
        """图片是静帧, 没有"从哪一秒开始"这回事, 只能裁尾巴。"""
        return not (clip.kind == "image" and side == "left")

    def _neighbors(self, clip):
        """同轨左右邻居的边界(用于裁剪时不压到别人身上)。"""
        spans = self.timeline.spans(clip.track, ignore=clip)
        left = max([e for s, e in spans if e <= clip.start + 1e-6] or [0.0])
        right = min([s for s, e in spans if s >= clip.end - 1e-6]
                    or [self.timeline.total_duration() + 3600.0])
        return left, right

    def _apply_trim(self, x: float, alt: bool = False,
                    allow_snap: bool = True):
        """按鼠标位置改片段的首尾; 视频/音频吸附到最近关键帧, 记下偏差。

        v169: 快捷键面板里可以把拖边换成「改变倍率」档 —— 拉长/压短边缘
        不再动刀口, 而是改这一段的倍速(时长跟着变, 画面内容一点不丢)。
        图片没有倍速, 两种档都还是调静帧时长。
        v189: 倍速档在「吸附」开着时, 边缘也会粘到别的片段首尾。
        allow_snap=False 用在刚按下鼠标那一下 —— 点一下边缘不该立刻跳过去。
        """
        clip, side, _off = self._trim
        if clip is None or self.timeline is None:
            return
        if self.edge_drag_mode() == "speed" and clip.kind in ("video", "audio"):
            self._apply_edge_speed(x, alt, allow_snap)
            return
        speed = clip.speed or 1.0
        t = self._x_to_time(x)
        left_lim, right_lim = self._neighbors(clip)
        off = 0.0

        if side == "left":
            end = clip.end
            lo = max(left_lim, 0.0)
            hi = clip.end - MIN_LEN
            new_start = max(lo, min(hi, t))
            if clip.kind != "image":
                raw_src = clip.src_in + (new_start - clip.start) * speed
                kf = clip.nearest_keyframe(raw_src)
                if kf is not None:
                    cand = clip.start + (kf - clip.src_in) / speed
                    cand = max(lo, min(hi, cand))
                    off = abs(cand - new_start)
                    new_start = cand
                new_src_in = clip.src_in + (new_start - clip.start) * speed
                clip.trim_to(new_src_in, clip.src_out)
            else:
                clip.hold = max(0.1, end - new_start)
            clip.start = max(0.0, end - clip.duration)
        else:
            lo = clip.start + MIN_LEN
            hi = min(right_lim, clip.start + clip.src_limit / speed)
            new_end = max(lo, min(hi, t))
            if clip.kind == "image":
                clip.hold = max(0.1, new_end - clip.start)
            else:
                raw_src = clip.src_out + (new_end - clip.end) * speed
                kf = clip.nearest_keyframe(raw_src)
                if kf is not None:
                    cand = clip.end + (kf - clip.src_out) / speed
                    cand = max(lo, min(hi, cand))
                    off = abs(cand - new_end)
                    new_end = cand
                new_src_out = clip.src_out + (new_end - clip.end) * speed
                clip.trim_to(clip.src_in, new_src_out)

        self._trim[2] = off
        self.update()

    def edge_drag_mode(self) -> str:
        """拖片段左右边缘现在干什么: "trim" / "speed"。

        每次按下才读一次盘(拖动过程中不重复读), F1 面板里改完立刻生效。
        """
        try:
            return _settings.load_edge_drag_mode()
        except Exception:
            return "trim"

    def _apply_edge_speed(self, x: float, alt: bool = False,
                          allow_snap: bool = True):
        """拖边「改变倍率」档(v169): 边拉多长, 这段就按多长重算倍速。

        倍速 = 源那段长度 / 拖出来的时长。上限(0.05~20×)和左右邻居都会
        限制块能拉到多长 —— 倍速到头后块就不再跟鼠标走, 这不是 bug。
        不吸附关键帧: 这一段导出必然重编码, 没有"无损刀口"可谈。

        v189: 「吸附」开着时改成吸附**时间线对齐点** —— 别的片段首尾
        (上下轨道的也算)、播放头、时间轴开头。用户要的是「上方还有一个
        片段, 拖边缘能和它的边缘对齐」, 所以这里不用整数倍速档位。
        Alt 临时关掉吸附(和拖整个片段同一个手势); 关掉「吸附」就完全不粘。
        """
        clip, side, _off = self._trim
        if clip is None or self.timeline is None:
            return
        t = self._x_to_time(x)
        hit = None
        if allow_snap and not alt and self.timeline.snap_on:
            # ignore=clip: 不能粘到自己身上, 否则一拖就动不了
            t, hit = self.timeline.snap(t, clip, self._snap_tol())
        self._snap_line = hit
        left_lim, right_lim = self._neighbors(clip)
        span = max(MIN_LEN, float(clip.src_out or 0.0) - float(clip.src_in or 0.0))
        if span <= 0:
            return
        # 时长的可行区间: 再快也不能快过倍速上限, 再慢也不能慢过下限,
        # 还得塞得进左右邻居之间
        dur_lo = max(MIN_LEN, span / EDGE_SPEED_MAX)
        dur_hi = span / EDGE_SPEED_MIN
        if side == "left":
            end = clip.end
            lo = max(left_lim, 0.0)
            hi = end - MIN_LEN
            want = max(lo, min(hi, t))
            if hit is not None and abs(want - hit) > 1e-6:
                self._snap_line = None   # 吸附点被上下限夹掉了, 别留误导的紫线
            dur = max(dur_lo, min(dur_hi, end - want, end - lo))
            clip.speed = span / max(MIN_LEN, dur)
            clip.start = max(0.0, end - clip.duration)
        else:
            lo = clip.start + MIN_LEN
            hi = right_lim
            want = max(lo, min(hi, t))
            if hit is not None and abs(want - hit) > 1e-6:
                self._snap_line = None
            dur = max(dur_lo, min(dur_hi, want - clip.start,
                                   right_lim - clip.start))
            clip.speed = span / max(MIN_LEN, dur)
        self._trim[2] = 0.0
        self.update()

    def _trim_bubble_text(self):
        """裁剪时浮在片段上方的一行字(告诉用户刀实际落在哪)。"""
        if self._trim is None or self.timeline is None:
            return ""
        clip, side, off = self._trim
        if self.edge_drag_mode() == "speed" and clip.kind in ("video", "audio"):
            sp = clip.speed or 1.0
            return f"倍速 {_fmt_speed(sp)}×，时长 {fmt_dur(clip.duration)}"
        at = clip.src_in if side == "left" else clip.src_out
        if clip.kind == "image":
            return f"静帧时长 {clip.duration:.1f} 秒"
        if off > 0.005:
            return f"实际切在 {fmt_time(at)}，偏差 {off:.1f} 秒"
        return f"切在 {fmt_time(at)}（关键帧上，无损）"

    # ---------------------------------------------------------------- 鼠标
    def mouseDoubleClickEvent(self, event):
        """双击左侧轨名 = 改轨名; 双击片段文字带/色块 = 就地改片段名(v192)。

        v192: 两处右键菜单里的「重命名这个片段…」弹窗撤掉了, 改名只在轨道上
        原地输入(回车/点走确认, Esc 放弃), 不再有任何模态窗口。
        """
        if event.button() != Qt.LeftButton or self.timeline is None:
            return
        x, y = event.position().x(), event.position().y()
        if x < GUTTER_W and y > RULER_H:
            track = self._track_at_y(y)
            if track is not None:
                self._begin_rename(track)
                return
        if x >= GUTTER_W and y > RULER_H and self._trim is None:
            clip = self._clip_at(x, y)
            if clip is not None:
                rect = self._clip_rect(clip)
                # 左右边缘是拖裁剪/改倍速的热区, 别在那儿弹输入框
                edge = (rect.isValid()
                        and (x < rect.left() + TRIM_HOT
                             or x > rect.right() - TRIM_HOT))
                if not edge:
                    self._begin_clip_rename(clip)
                    return
        super().mouseDoubleClickEvent(event)

    # ---------------------------------------------------------------- 轨名
    def _begin_rename(self, track: int):
        self._end_rename()
        ty = self._track_y(track)
        th = max(22.0, self._th())
        ed = _TrackNameEdit(self._track_label(track), self)
        ed.setGeometry(2, int(ty + (self._th() - th) / 2 + 1),
                       int(GUTTER_W - 4), int(th - 2))
        ed.selectAll()
        ed.show()
        ed.setFocus(Qt.MouseFocusReason)
        ed.committed.connect(lambda name, t=track: self._finish_rename(t, name))
        ed.cancelled.connect(self._end_rename)
        self._rename_edit = ed
        ed.start_outside()            # v193: 点空白处 / Esc 放弃

    def _end_rename(self):
        ed = self._rename_edit
        self._rename_edit = None
        lab = getattr(self, "_rename_suffix", None)
        self._rename_suffix = None
        if lab is not None:
            try:
                lab.hide()
                lab.deleteLater()
            except Exception:
                pass
        if ed is not None:
            try:
                ed.stop_outside()
                ed.blockSignals(True)
                ed.hide()
                ed.deleteLater()
            except Exception:
                pass

    def _finish_rename(self, track: int, name: str):
        name = (name or "").strip()
        self._end_rename()
        if self.timeline is None or not name:
            self.update()
            return
        self.timeline.rename_track(track, name)
        self.update()
        self.trackRenamed.emit(track)

    # ------------------------------------------------- 片段名就地编辑(v192)
    def _begin_clip_rename(self, clip):
        """在片段上方那条文字带上盖一个输入框, 原地改名(不弹窗)。

        v194: 输入框只放主名, 后缀作为只读标签贴在右边(取磁盘文件真实扩展名)。
        """
        self._end_rename()
        cap = self._caption_rect(clip)
        x0 = max(GUTTER_W + 2.0, cap.left() + 2.0)
        x1 = min(self.width() - 2.0, cap.right() - 2.0)
        if x1 - x0 < 72:
            x0 = max(GUTTER_W + 2.0, min(x0, self.width() - 74.0))
            x1 = min(self.width() - 2.0, x0 + 72.0)
        h = max(20.0, min(24.0, self._th() - 2.0))
        y = cap.top() + (cap.height() - h) / 2.0
        stem, ext = split_name_stem(str(getattr(clip, "name", "") or ""),
                                    str(getattr(clip, "source", "") or ""))
        ed = _TrackNameEdit(stem, self,
                            align=Qt.AlignLeft | Qt.AlignVCenter,
                            font_px=12)
        suffix_w = 0
        if ext:
            suffix_w = QFontMetrics(ed.font()).horizontalAdvance(ext) + 10
            lab = QLabel(ext, self)
            lab.setObjectName("trackNameSuffix")
            lab.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            lab.setStyleSheet(
                f"QLabel#trackNameSuffix{{background:{T.SURFACE}; color:{T.TEXT_SUB};"
                f" border:1px solid {T.ACCENT}; border-left:none;"
                f" padding:0px 3px; font-size:12px;}}")
            lab.setGeometry(int(x0 + max(40, x1 - x0 - suffix_w)), int(y),
                            int(suffix_w), int(h))
            lab.show()
            self._rename_suffix = lab
            ed.add_inside_widget(lab)
        ed.setGeometry(int(x0), int(y),
                       int(max(40, x1 - x0 - suffix_w)), int(h))
        ed.selectAll()
        ed.show()
        ed.setFocus(Qt.MouseFocusReason)
        ed.committed.connect(lambda name, c=clip, e=ext:
                             self._finish_clip_rename(c, name, e))
        ed.cancelled.connect(self._end_rename)
        self._rename_edit = ed
        ed.start_outside()            # v193: 点空白处 / Esc 放弃

    def _finish_clip_rename(self, clip, name: str, ext: str = ""):
        name = (name or "").strip()
        self._end_rename()
        old = str(getattr(clip, "name", "") or "")
        full = name + (ext or "")
        if not name or full == old:
            self.update()
            return
        clip.name = full
        self.update()
        self.clipRenamed.emit(clip)   # 主窗口: 右栏清单 / 属性 / 撤销点 / 状态栏

    def mousePressEvent(self, event):
        if self.timeline is None:
            return
        x, y = event.position().x(), event.position().y()

        if event.button() == Qt.RightButton:
            # 轨名区右键 / 轨道空白处右键 = 轨道菜单(加/删轨、磁吸);
            # 片段上右键 = 片段菜单(主窗口会把加/删轨也挂进去)。
            if x < GUTTER_W and y > RULER_H:
                track = self._track_at_y(y)
                if track is not None:
                    self.trackMenuRequested.emit(track,
                                                 event.globalPosition().toPoint())
                    return
            clip = self._clip_at(x, y)
            if clip is not None:
                self.contextMenuRequested.emit(clip, event.globalPosition().toPoint())
                return
            track = self._track_at_y(y)
            if track is not None:
                self.trackMenuRequested.emit(track,
                                             event.globalPosition().toPoint())
                return
            self.contextMenuRequested.emit(None, event.globalPosition().toPoint())
            return

        if event.button() != Qt.LeftButton:
            return

        if y <= RULER_H or self._playhead_handle_rect().contains(x, y):
            self._scrub = True
            self._set_playhead_x(x)
            return

        clip = self._clip_at(x, y)

        # 裁剪线(播放头)跟随点击: 点片段、点片段之间的空档、点空轨道, 都要跟过来。
        # 注意只在"按下"时对一次位, 不进入 scrub —— 否则拖片段就变成拖播放头了。
        # v193: 按住 Ctrl 是在多选片段, 这时播放头不能跟着跑, 否则刚排好的
        #       选取范围会被自己的点击挪走。标尺 / 播放头手柄不受影响(上面已 return)。
        ctrl = bool(event.modifiers() & Qt.ControlModifier)
        if x >= GUTTER_W and not ctrl:
            self._set_playhead_x(x)

        # Ctrl 点选: 加一个 / 撤一个, 其余选中保持(凑够了可以一起删、一起拖)
        if clip is not None and ctrl:
            if self.is_selected(clip):
                self._sel = [c for c in self._sel if c is not clip]
            else:
                self._sel.append(clip)
            self._selected = self._sel[0] if self._sel else None
            self.clipSelected.emit(self._selected)
            self.update()
            return
        # 点已选中的(多选状态): 保持整组, 好一起拖
        if clip is None or not (len(self._sel) > 1 and self.is_selected(clip)):
            if clip is not self._selected or len(self._sel) > 1:
                self._set_selection([clip] if clip is not None else [])
                self.clipSelected.emit(clip)
        if clip is None:
            # 按在空轨道上并拖动 = 拖播放头（Ctrl 多选时不进 scrub, 免得又带走裁剪线）
            if not ctrl:
                self._scrub = True
            self.update()
            return
        side = self._trim_side(clip, x, y)
        if side is not None:
            self._trim = [clip, side, 0.0]
            # 刚按下这一下不吸附: 点一下边缘不该立刻跳到别的片段边上
            self._apply_trim(x, allow_snap=False)
            return
        group = [(c, c.start, c.track) for c in self._sel] \
            if self.is_selected(clip) else [(clip, clip.start, clip.track)]
        self._drag = {
            "clip": clip,
            "x0": x,
            "t0": clip.start,
            "offset": self._x_to_time(x) - clip.start,
            "from_track": clip.track,
            "moved": False,
            "group": group,          # 多选时整组一起挪(保持相对位置)
        }
        self.update()

    def mouseMoveEvent(self, event):
        if self.timeline is None:
            return
        x, y = event.position().x(), event.position().y()

        if self._scrub:
            self._set_playhead_x(x)
            return
        if self._trim is not None:
            # Alt 临时关掉吸附(和拖整个片段同一个手势)
            self._apply_trim(x, bool(event.modifiers() & Qt.AltModifier))
            return
        if self._drag is not None:
            d = self._drag
            if abs(x - d["x0"]) > 4 or d["moved"]:
                d["moved"] = True
                clip = d["clip"]
                raw = max(0.0, self._x_to_time(x) - d["offset"])
                # Alt 临时关掉吸附(剪映也是这个手势)
                if event.modifiers() & Qt.AltModifier:
                    start, hit = raw, None
                else:
                    start, hit = self.timeline.snap_move(
                        clip, raw, clip.duration, self._snap_tol())
                self._snap_line = hit
                # 跨轨: 按鼠标所在轨
                track = d["from_track"]
                for i in range(self.tracks()):
                    ty = self._track_y(i)
                    if ty <= y <= ty + self._th():
                        track = i
                        break
                d_track = track - d["from_track"]
                # v194 挤占: 单片段拖动且开关打开时允许压在已有片段上 ——
                # 松手按插入落位, 落点之后的同轨片段整体顺延(只推目标轨)。
                # 多选整组拖动仍走原来的避让规则(整组插入的顺延量没有唯一解)。
                crowd = self.crowd and len(d["group"]) == 1
                # 不许压在别的片段身上: 直接把落点夹到空隙里(磁吸开着时
                # 整轨本来就是首尾相接的, 那时靠"换顺序"来放, 不夹)。
                if not self.timeline.magnetic and not crowd:
                    ign = [g[0] for g in d["group"]] or [clip]
                    start = self.timeline.clamp_start(
                        track, start, clip.duration, clip.start, ign)
                # v195: 虚线要和松手后的真实落位一致 —— 落点压在片段内部时
                # insert_place 会就近对齐到该片段首尾, 预览也用同一个函数算,
                # 否则用户看到的是「线在 A 中间, 松手 D 却跳到 A 前面」。
                if crowd:
                    self._insert_ind = (track, self.timeline.insert_point(
                        track, start, [clip]))
                else:
                    self._insert_ind = None
                d_time = start - d["t0"]
                last = self.timeline.track_count - 1
                # 整组跟着主片段平移(多选时保持相对位置, 落轨按主片段)
                for c, s0, t0 in d["group"]:
                    c.start = max(0.0, s0 + d_time)
                    c.track = max(0, min(last, t0 + d_track))
            self.update()
            return

        clip = self._clip_at(x, y)
        if clip is not self._hover:
            self._hover = clip
            self.update()
        if clip is not None and self._trim_side(clip, x, y) is not None:
            self.setCursor(Qt.SplitHCursor)
        else:
            self.setCursor(Qt.PointingHandCursor if clip is not None
                            or y <= RULER_H else Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        if self._scrub:
            self._scrub = False
            self._snap_line = None
            self.update()
        if self._trim is not None:
            clip = self._trim[0]
            self._trim = None
            self._snap_line = None
            self.update()
            self.trimChanged.emit(clip)
            return
        if self._drag is not None:
            d = self._drag
            moved = d["moved"]
            group = d["group"] or []
            self._drag = None
            self._snap_line = None
            self._insert_ind = None
            if moved:
                # v194: 挤占(单片段) = 插入式落位, 后面的同轨片段整体顺延。
                # 其余仍是「重叠就挪到最近空位, 磁吸开时整轨合拢」;
                # 多选时逐个落位(主片段先落, 其余按相对顺序跟上)。
                if self.crowd and len(group) == 1:
                    c = group[0][0]
                    self.timeline.insert_place(c, c.track, c.start, ignore=[c])
                else:
                    for c in [g[0] for g in group]:
                        self.timeline.place(c, c.track, c.start)
                self.orderChanged.emit()
                self.dragFinished.emit()
            self.update()

    def wheel_zoom_at(self, bigger: bool, anchor_x: float | None = None):
        """放大 / 缩小时间线。**锚点是裁剪线**(v135), anchor_x 只为兼容旧调用。"""
        self.set_pps(self._pps * (1.2 if bigger else 1 / 1.2), self._zoom_anchor())

    def wheel_pan(self, delta_px: int):
        sb = self._hbar
        sb.setValue(int(max(sb.minimum(), min(sb.maximum(), sb.value() + delta_px))))

    def wheelEvent(self, event):
        hook = getattr(self, "wheel_hook", None)
        if hook is not None:
            try:
                if hook(event):
                    event.accept()
                    return
            except Exception:
                pass                    # 分派出错也不能把滚轮吞了, 退回默认
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            # 锚点用裁剪线, 不用鼠标位置(v135)
            self.set_pps(self._pps * (1.2 if delta > 0 else 1 / 1.2),
                         self._zoom_anchor())
        else:
            self._hbar.setValue(int(self._hbar.value() - event.angleDelta().y()))

    # ---------------------------------------------------------------- 拖入素材
    def _drop_track(self, y: float) -> int:
        """落点 y 落在哪条轨上; 标尺区/空白一律落到第 1 轨。"""
        for track in range(self.tracks()):
            ty = self._track_y(track)
            if ty <= y <= ty + self._th():
                return track
        return 0

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasText() or md.hasUrls():
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        md = event.mimeData()
        if not (md.hasText() or md.hasUrls()):
            event.ignore()
            return
        event.setDropAction(Qt.CopyAction)
        event.accept()
        pos = event.position()
        track = self._drop_track(pos.y())
        t, hit = self.timeline.snap(self._x_to_time(pos.x()), None, self._snap_tol())
        self._snap_line = hit
        self._drop_ind = (track, t)
        self.update()

    def dragLeaveEvent(self, event):
        self._drop_ind = None
        self._snap_line = None
        self.update()

    def dropEvent(self, event):
        md = event.mimeData()
        path = ""
        if md.hasText():
            path = md.text().strip()
        elif md.hasUrls():
            urls = md.urls()
            if urls:
                path = urls[0].toLocalFile()
        self._drop_ind = None
        self._snap_line = None
        if not path:
            event.ignore()
            self.update()
            return
        pos = event.position()
        track = self._drop_track(pos.y())
        t = self._x_to_time(pos.x())
        event.setDropAction(Qt.CopyAction)
        event.accept()
        self.sourceDropped.emit(path, track, t)
        self.update()

    def _set_playhead_x(self, x: float):
        t = self._x_to_time(x)
        total = self.timeline.total_duration()
        if t > total:
            t = total
        # 裁剪线也吸附(v140): 片段的首尾(也就是"两个素材之间的接缝"、素材的
        # 顶端和末端)以及每条轨道的 0 点。差 8 像素以内就贴上去, 拖的时候画一条
        # 紫色竖线告诉你"已经咬住了"。
        # ⚠️ 不能用 timeline.snap(): 它把**播放头自己**也算一个吸附点, 于是
        #    裁剪线一拖就死死粘在原地, 手一松又弹回去 —— 怎么拖都动不了。
        #    这里显式排除播放头(include_playhead=False)。
        snapped = None
        if self.timeline is not None and self.timeline.snap_on:
            tol = self._snap_tol()
            if tol > 0:
                best = None
                for c in self.timeline.snap_targets(None, include_playhead=False):
                    d = abs(c - t)
                    if d <= tol and (best is None or d < best[0]):
                        best = (d, c)
                if best is not None:
                    t, snapped = best[1], best[1]
        if self._scrub:
            # 只有真在拖裁剪线时才挂吸附线; 点一下定位时别留一条紫线在那儿
            self._snap_line = snapped
        if abs(t - self.timeline.playhead) > 1e-6:
            self.timeline.playhead = t
            self.playheadChanged.emit(t)
        self.update()

    def set_playhead(self, t: float):
        if self.timeline is not None:
            self.timeline.playhead = max(0.0, min(self.total_duration(), t))
            self.update()

    # ---------------------------------------------------------------- 跟随移动
    def set_follow(self, on: bool):
        """开关「跟随移动」: 播放时裁剪线钉在原地, 反过来滚轨道。

        ⚠️ 只影响"看", 不动时间线数据; 关了以后播放回到"裁剪线自己走"。
        """
        self.follow = bool(on)
        self._follow_x = None           # 重新钉一次(按下次播放时的位置)
        self.update()

    def reset_follow_anchor(self):
        """下次播放时按"裁剪线当时的位置"重新钉住(每次开播都重新取)。"""
        self._follow_x = None

    def follow_scroll(self):
        """播放头往前走时滚时间线, 让裁剪线停在固定的横坐标上。

        ⚠️ 只在播放时调用: 暂停时用户自己拖滚动条, 不该被拽回去。
        """
        tl = self.timeline
        if not self.follow or tl is None or not ok_num(self._pps) or self._pps <= 0:
            return
        if self._follow_x is None:
            # 第一次: 把裁剪线现在的横坐标钉住(超出可视区就夹回看得见的地方)
            # ⚠️ 必须**取整**: 播放头是按整数像素画的, 跟随点落在半像素上就会
            #    每帧左右各差半格 —— 看着就是裁剪线在原地抖(v160)。
            lo = GUTTER_W + LEFT_PAD
            hi = max(lo, self.width() - 48)
            self._follow_x = float(snap_px(
                min(max(lo, self._time_to_x(tl.playhead)), hi)))
        target = GUTTER_W + LEFT_PAD + tl.playhead * self._pps - self._follow_x
        bar = self._hbar
        v = snap_px(min(max(target, bar.minimum()), bar.maximum()))
        if v == bar.value():
            return
        # 这一下是"自己在滚", 不是用户换屏 —— 别把抽帧队列清空(v155)
        self._scroll_quiet += 1
        try:
            bar.setValue(v)
        finally:
            self._scroll_quiet -= 1

    # ---------------------------------------------------------------- 布局
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._hbar.setGeometry(0, self.height() - HBAR_H, self.width(), HBAR_H)
        # 宽度变了 → "全览"所需的 pps 也变了。set_pps 在数值没变时会直接返回,
        # 所以这里要自己夹一次, 否则窗口变窄后就不再是完整可见。
        lo = self.min_pps()
        if self._pps < lo:
            self._pps = lo
            self._hbar.setValue(0)
            self.zoomChanged.emit(float(lo))
        self._sync_scrollbar()
        self.update()

    # 抽帧线程由外面挂进来(时间线只管画)
    _film_worker = None

    def set_film_worker(self, worker):
        self._film_worker = worker
