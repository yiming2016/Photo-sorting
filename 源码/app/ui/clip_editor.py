# -*- coding: utf-8 -*-
"""剪辑页(整个窗口, 剪映式排布)。

v117 起不再塞在左栏里, 而是占满整窗:
    顶部返回栏
    ┌───────────┬──────────────────┬──────────┐
    │  素材框    │      预览         │  属性     │
    ├───────────┴──────────────────┴──────────┤
    │  工具条                                  │
    │  时间线(横跨全宽, 多条轨道)                │
    └─────────────────────────────────────────┘

分工:
- 本页管"看"(预览)和"摆"(时间线交互、工具条按钮);
- 素材框与属性面板是独立控件, 由主窗口喂数据;
- 按钮只发信号, 真正改数据在主窗口(它持有时间线与素材列表)。

按钮一律"图标 + 文字": 不让人对着一串图标猜哪个是分割。
"""
from __future__ import annotations

import time

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QImageReader, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMenu, QPushButton,
                               QSizePolicy, QSlider, QSplitter, QToolButton,
                               QVBoxLayout, QWidget)

from . import theme as T
from .clip_list import ClipListPanel
from .clip_media import MediaPanel
from .clip_panel import ClipPanel
from .edit_icons import make_icon
from .widgets import fit_popup_width, prepare_combo_popup
from .timeline import (KF_HIT_TOL, MAX_PPS as TL_MAX_PPS,
                       MIN_PPS as TL_MIN_PPS, TimelineWidget, fmt_time)
from ..merge import settings

FRAME_STEP = 1.0 / 25.0        # 逐帧步进(没有帧率信息前按 25fps 走)

# 截图范围: 按钮上依次循环这三个 (v150 由下拉改成点一下换一个)
SHOT_SCOPES = (("当前", "current"), ("选中", "selected"), ("所有", "all"))


class ToolButton(QToolButton):
    """图标在上、文字在下(图标自绘, 颜色跟主题)。

    ⚠️ 必须 NoFocus: 按钮一旦留住焦点, 空格/回车就等于再点一次它
    (用户踩过: 点完「剪辑」再按空格直接退出剪辑态)。键盘操作全交给 QShortcut。
    """

    def __init__(self, icon_name: str, text: str, tip: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("tlTool")
        self.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.setIcon(make_icon(icon_name, T.TEXT_SUB, 20))
        self.setIconSize(QSize(20, 20))
        self.setText(text)
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(54, 46)


class PreviewView(QLabel):
    """预览窗: **点一下就播放/暂停**(播放按钮已撤, 这是唯一的播放入口)。

    v163: 画面自己管缩放 —— 收一张**没缩过的源图**存着(`set_source`), 按当前的
    内容区重新缩; 窗口一变就重缩。这样"画面"永远是控件里的最大值, 底下那条
    进度条才能真的贴着画面下沿, 而不是贴着控件下沿、中间还夹一条黑边。
    """

    clicked = Signal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._src = None             # 源图(没缩放过的)
        self._ratio = None           # 源图宽高比, 由它算"这块地方该多高"
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        # Ignored: 别让 QLabel 拿 pixmap 尺寸去反推 sizeHint(会跟自适应高度打架)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setStyleSheet(
            f"background: {T.SURFACE_ALT}; border: 1px solid {T.BORDER};"
            f" border-radius: 10px; color: {T.TEXT_WEAK}; font-size: 12px;")

    # ------------------------------------------------------------ 画面
    def set_source(self, pm):
        """给一张源图(原尺寸)。传 None = 清空画面(回到文字提示)。"""
        if pm is None or (hasattr(pm, "isNull") and pm.isNull()):
            self._src = None
            self._ratio = None
            self.setPixmap(QPixmap())
            return
        self._src = pm
        try:
            self._ratio = float(pm.width()) / max(1.0, float(pm.height()))
        except Exception:
            self._ratio = None
        self.apply_scale()

    def has_source(self) -> bool:
        return self._src is not None

    def aspect(self) -> float | None:
        """画面宽高比(没有画面时 None)。"""
        return self._ratio

    def apply_scale(self):
        """按当前内容区重缩一遍源图(窗口尺寸变了、高度变了都走这里)。"""
        if self._src is None:
            return
        r = self.contentsRect()
        dpr = 1.0
        try:
            dpr = float(self.devicePixelRatioF() or 1.0)
        except Exception:
            dpr = 1.0
        w = max(60, int(r.width() * dpr))
        h = max(60, int(r.height() * dpr))
        out = self._src.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        try:
            out.setDevicePixelRatio(dpr)
        except Exception:
            pass
        super().setPixmap(out)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.apply_scale()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            return
        super().mousePressEvent(event)


def _rgba(color: str, alpha: float) -> str:
    """把主题里的 #RRGGBB 变成 QSS 能认的 rgba(...)。"""
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha:.2f})"


class MediaScrubber(QWidget):
    """浮在预览窗下沿的进度条。

    三种时候出现(同一条, 按语境换范围):
      · 点素材框里的素材 → 长度 = 这个素材, 可以拖到它的任意一秒(不用先上轨);
      · 点右栏的片段卡片 → 长度 = **这一段**, 拖它就是让裁剪线在这一段里走(v141);
      · 播整条轨道 → 长度 = **所有轨道的总长**, 拖它就是定位整条时间轴(v159)。
    """

    seekRequested = Signal(float)      # 拖动过程中(实时): 走到屏幕上的这一秒
    seekCommitted = Signal(float)      # 松手 / 点一下(定下来): 该续播的续播

    H = 22          # v163: 26 → 22, 紧贴画面时更紧凑(空间宝贵)
    # 拖动中发信号的间隔(v161): 鼠标一动就发的话, 一秒几十次重启抓帧/停播器,
    # 反而卡; 25 帧/秒已经够"跟手"了。
    LIVE_MS = 0.04

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dur = 0.0
        self._pos = 0.0
        self._drag = False
        self._last_live = 0.0
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setVisible(False)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background: transparent;")

    # ------------------------------------------------------------ 数据
    def set_range(self, duration: float):
        """改范围(每秒会被调用几十次: 值没变就别重画, 白重画会拖慢播放)。"""
        d = max(0.0, float(duration or 0.0))
        if abs(d - self._dur) < 1e-6:
            return
        self._dur = d
        if self._pos > d:
            self._pos = d
        self.update()

    def set_pos(self, t: float):
        # ⚠️ 正在拖的时候不许改 _pos: 播放推进那个 100ms 定时器每秒来十次,
        # 它一改手柄就往回弹 —— 用户看到的就是"根本拖不动"。
        if self._drag:
            return
        p = max(0.0, min(self._dur or 1e9, float(t or 0.0)))
        if abs(p - self._pos) < 1e-9:
            return
        self._pos = p
        self.update()

    # ------------------------------------------------------------ 几何
    def _time_w(self) -> int:
        """右边留给时间文字("00:00.0 / 00:00.0")的宽度。

        v159: 进度条也会显示"整条轨道"的总长, 十分钟以上是两位数分钟 + 两位
        秒数 —— 写死 100px 会把文字挤出去, 按最长的样子量一次就行。
        """
        f = self.font()
        f.setPixelSize(11)
        f.setBold(True)
        try:
            w = QFontMetrics(f).horizontalAdvance("00:00.0 / 00:00.0")
        except Exception:
            w = 96
        return max(90, w + 14)

    def _track(self):
        r = self.rect()
        return r.adjusted(8, 0, -(8 + self._time_w()), 0)   # 右边留给时间文字

    def _t_at(self, x: int) -> float:
        tr = self._track()
        if tr.width() <= 0 or self._dur <= 0:
            return 0.0
        f = (x - tr.left()) / float(tr.width())
        return max(0.0, min(self._dur, f * self._dur))

    # ------------------------------------------------------------ 交互
    @property
    def dragging(self) -> bool:
        """手柄正被按着(主窗口据此判断"先把画面跟过去, 别急着续播")。"""
        return self._drag

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = True
            self._pos = self._t_at(int(event.position().x()))
            self.update()
            # 这一条只有 26px 高, 鼠标一抖就滑出去了; 不抓鼠标的话后面的
            # move / release 会落到别的控件上, 拖到一半就断 —— 手感就是"拖不动"。
            self.grabMouse()
            self._last_live = time.perf_counter()
            self.seekRequested.emit(self._pos)   # 按下就跳(点一下也能定位)
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag:
            self._pos = self._t_at(int(event.position().x()))
            self.update()
            # v161: 拖动过程中也要发 —— 以前只在按下/松手发, 用户看到的是
            # "按住不动, 松手画面才换"。画面由主窗口用抓帧线程顶着, 这里只管节流。
            now = time.perf_counter()
            if now - self._last_live >= self.LIVE_MS:
                self._last_live = now
                self.seekRequested.emit(self._pos)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag and event.button() == Qt.LeftButton:
            self._drag = False
            try:
                self.releaseMouse()
            except Exception:
                pass
            self.seekRequested.emit(self._pos)
            self.seekCommitted.emit(self._pos)
            return
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------ 画
    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QPen
        if self._dur <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        # 半透明胶囊底(贴在画面下沿, 不挡主要内容)
        p.setPen(QPen(QColor(T.BORDER)))
        bg = QColor(T.SURFACE)
        bg.setAlpha(235)
        p.setBrush(bg)
        p.drawRoundedRect(r.adjusted(0, 0, 0, 0), 6, 6)
        tr = self._track()
        cy = r.center().y()
        # 轨道
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(T.BORDER_STRONG), 3, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(tr.left(), cy, tr.right(), cy)
        # 已播过的部分
        x = tr.left() + int(tr.width() * (self._pos / self._dur)) if self._dur else tr.left()
        p.setPen(QPen(QColor(T.ACCENT), 3, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(tr.left(), cy, max(tr.left(), x), cy)
        # 手柄
        p.setBrush(QColor(T.ACCENT))
        p.setPen(Qt.NoPen)
        p.drawEllipse(int(x) - 5, cy - 5, 10, 10)
        # 时间(用像素字号: pointSize 7 在高 DPI 下糊成一团, 根本看不清几分几秒)
        p.setPen(QColor(T.TEXT_SUB))
        f = p.font()
        f.setPixelSize(11)
        f.setBold(True)
        p.setFont(f)
        p.drawText(self.rect().adjusted(0, 0, -8, 0),
                   Qt.AlignRight | Qt.AlignVCenter,
                   f"{fmt_time(self._pos)} / {fmt_time(self._dur)}")
        p.end()


def _vsep() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.VLine)
    line.setFixedWidth(1)
    line.setStyleSheet(f"color: {T.BORDER}; background: {T.BORDER};")
    line.setContentsMargins(0, 6, 0, 6)
    return line


def _chip(text: str, tip: str = "") -> QPushButton:
    """工具条上的小文字按钮(和磁吸/吸附同一副样子)。

    为什么不用 ToolButton 那种"图标在上"的大块: 第二行要摆 6 个,
    大块占宽一倍多, 而且这些功能都是低频的、用文字更好认。
    """
    b = QPushButton(text)
    b.setObjectName("chipToggle")
    b.setFixedHeight(28)
    b.setMinimumWidth(52)
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.NoFocus)
    if tip:
        b.setToolTip(tip)
    return b


class _VolumeValue(QLineEdit):
    """音量数字: 平时就是一行普通数字, 点一下变成小输入框(v187)。

    v188: 框里**只有数字**, 「%」「dB」是框外的普通文字(_VolumeUnit)。
    用只读 QLineEdit 而不是 QLabel —— 点一下把只读关掉就是输入框, 不用换控件、
    不跳位置、也不另开弹窗; 回车或点别处提交, Esc 放弃。
    """

    def __init__(self, width: int, owner, parent=None):
        super().__init__(parent)
        self._owner = owner
        self._editing = False
        self._base_w = width
        self.setFixedWidth(width)
        self.setFixedHeight(22)
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.setFrame(False)       # 去掉输入框内边框留白, 数字才贴得住框外单位
        # v192: QLineEdit 自带左右各约 2px 的文本内边距(QSS padding:0 管不到)。
        # 数字右对齐时, 宽度只按字宽算就会把最左边那个字符(0.0 的首个 0)裁掉。
        self.setTextMargins(0, 0, 0, 0)
        self.setReadOnly(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setCursor(Qt.PointingHandCursor)
        self._paint(False)

    def _paint(self, editing: bool):
        if editing:
            self.setStyleSheet(
                "QLineEdit { background: #FFFFFF; border: 1px solid %s;"
                " border-radius: 4px; color: %s; font-size: 11px; padding: 0; }"
                % (T.ACCENT, T.TEXT))
        else:
            # padding 必须是 0: 数字右对齐后要和框外的「%」贴在一起, 中间
            # 多 2px 内边距就会看出「100  %」这种裂开的读数。
            self.setStyleSheet(
                "QLineEdit { background: transparent; border: none;"
                " color: %s; font-size: 11px; padding: 0; }"
                " QLineEdit:disabled { color: %s; }"
                % (T.TEXT_SUB, T.TEXT_WEAK))

    def display(self, text: str):
        """没在编辑时才刷数字 —— 别把用户正在敲的内容盖掉。

        平时让输入框**正好包住这个数字**: 框外的「%」「dB」才贴得上来读成
        「100%」, 不会因为固定宽度在中间裂开一条缝。空出来的宽度由组里的
        补位撑住, 整组不会忽宽忽窄。
        """
        if self._editing:
            return
        self.setText(text)
        # v192: +8 是给字形抗锯齿 / 光标留的余量。以前只 +2, 叠上 QLineEdit
        # 自带的文本内边距后「0.0」最左边那个 0 会被裁掉一点点。
        w = max(16, min(self._base_w,
                        QFontMetrics(self.font()).horizontalAdvance(text) + 8))
        self.setFixedWidth(w)

    def _begin(self):
        self._editing = True
        self.setFixedWidth(self._base_w)   # 编辑时留够地方输更长的数字
        self.setTextMargins(2, 0, 2, 0)   # 编辑态有边框, 字别贴着框线
        self.setReadOnly(False)
        self._paint(True)
        self.setFocus(Qt.MouseFocusReason)
        self._owner._volume_edit_begin()

    def begin_edit(self):
        """点旁边的单位文字也要能进来改这个数字。"""
        if self.isEnabled() and not self._editing:
            self._begin()
            self.selectAll()

    def mousePressEvent(self, ev):
        if not self.isEnabled():
            return super().mousePressEvent(ev)
        first = not self._editing
        if first:
            self._begin()
        super().mousePressEvent(ev)
        if first:
            self.selectAll()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._finish(True)
            ev.accept()
            return
        if ev.key() == Qt.Key_Escape:
            self._finish(False)
            ev.accept()
            return
        super().keyPressEvent(ev)

    def focusOutEvent(self, ev):
        if self._editing:
            self._finish(True)
        super().focusOutEvent(ev)

    def _finish(self, accept: bool):
        if not self._editing:
            return
        text = self.text().strip() if accept else None
        self._editing = False
        self.setReadOnly(True)
        self.setTextMargins(0, 0, 0, 0)   # 回到「数字正好包住」的显示态
        self._paint(False)
        self.clearFocus()
        self._owner._volume_edit_commit(self, text)


class _VolumeUnit(QLabel):
    """单位(% / dB): 输入框**外面**的普通文字; 点它一样能开始改数字。"""

    def __init__(self, text: str, value: _VolumeValue, parent=None):
        super().__init__(text, parent)
        self._value = value
        self.setFixedHeight(22)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            "QLabel { background: transparent; color: %s; font-size: 11px;"
            " padding: 0; } QLabel:disabled { color: %s; }"
            % (T.TEXT_SUB, T.TEXT_WEAK))

    def mousePressEvent(self, ev):
        if self.isEnabled():
            self._value.begin_edit()
        super().mousePressEvent(ev)


class _VolumeSlider(QSlider):
    """音量拖动条: 双击 = 归位到 100%(0.0 dB)。

    ⚠️ 这套 QSS 必须挂在**控件自己**身上: 只写在全局主题里的话, Fusion
    仍会给 QSlider 画一整块灰色凹槽背景, 全局样式压不住它。
    v189: 控件底色仍透明(那块灰色底框不要), 但 groove 要像「全览」右边
    的缩放条一样**整条细线可见** —— 0% 到 200% 的两端都看得见终点。
    """

    resetRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._handle_on = None
        self.set_handle_enabled(True)

    def set_handle_enabled(self, on: bool):
        """没选中片段时手柄也跟着灰下去(不能靠 :disabled 伪态规则, 那条会让
        整份 QSS 解析失效, 灰底又回来了)。"""
        if self._handle_on is bool(on):
            return
        self._handle_on = bool(on)
        self.setStyleSheet(
            "QSlider { background: transparent; border: none; }"
            # 整条细轨道: 和 QSlider#zoomSlider 同一副样子, 终点看得见。
            # 不写 add-page 规则 —— Qt 默认就是透明, 写出来反而会把 groove 盖掉。
            "QSlider::groove:horizontal { background: %s; border: none;"
            " height: 4px; border-radius: 2px; }"
            "QSlider::sub-page:horizontal { background: %s; border: none;"
            " height: 4px; border-radius: 2px; }"
            "QSlider::handle:horizontal { width: 12px; margin: -5px 0;"
            " border: none; border-radius: 6px; background: %s; }"
            % (T.BORDER_STRONG, T.ACCENT_BORDER,
               T.ACCENT if on else T.TEXT_WEAK))

    def mouseDoubleClickEvent(self, ev):
        ev.accept()
        self.resetRequested.emit()


class _DownCombo(QComboBox):
    """永远朝下弹出的小下拉(v134)。

    默认 QComboBox 会"就近"弹: 工具条贴着时间线, 下方判定不够高或选中项靠下时
    就翻到上面去盖住预览, 选一次要点两下。这里固定摆在按钮正下方。
    靠 0ms 定时器摆第二次 —— Qt 在 showEvent 里还会自己挪一下, 抢不过它。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        prepare_combo_popup(self)      # v215: 弹层只留一层圆角底

    def showPopup(self):
        fit_popup_width(self)          # v215: 弹层放得下最长选项
        super().showPopup()
        QTimer.singleShot(0, self._pin_down)

    def _pin_down(self):
        panel = self.view().window()
        if panel is None or not panel.isVisible():
            return
        panel.move(self.mapToGlobal(QPoint(0, self.height() + 1)))


class ClipEditor(QWidget):
    """全宽剪辑页: 顶部返回栏 + 素材/预览/属性 + 工具条 + 时间线。"""

    backRequested = Signal()         # (v128 起废弃: 剪辑是独立窗口了, 关窗即回)
    splitRequested = Signal()        # S / 分割按钮
    deleteRequested = Signal()       # Delete / 删除按钮
    addMediaRequested = Signal()     # 添加素材
    helpRequested = Signal()         # F1 / 帮助按钮
    togglePlayRequested = Signal()   # 空格 / 播放按钮
    exportRequested = Signal()       # 导出(能不能无损由主窗口判定后告知)
    lossReportRequested = Signal()   # 打开「为什么不能无损」清单
    toolsRequested = Signal()        # (v135 起由 toolRequested 取代, 保留兼容)
    # "align_kf" / "fps" / "decimate" / "detect" / "fix_duration" / "out_dir"
    # v183: "save_project" / "open_project" / "reorder_reverse" /
    #       "reorder_time_new" / "reorder_time_old" / "reorder_name"
    toolRequested = Signal(str)
    waveformToggled = Signal(bool)   # 显示声音波形
    volumeRequested = Signal(float)  # v187: 音量拖动条改选中片段的音量倍数
    volumeEditing = Signal(bool)     # v187: 数字框正在手输(临时停用剪辑键位)
    mediaDeleteRequested = Signal(list)   # 素材框里 Ctrl 选中后按 Delete
    mediaPreviewRequested = Signal(str)   # 素材框里点中某个素材(没点 ＋)
    mediaBlankClicked = Signal()          # 点素材框空白处
    mediaPlayRequested = Signal()         # 双击素材 = 播它
    mediaSeekRequested = Signal(float)    # 拖动进度条过程中(实时): 走到某一秒
    mediaSeekCommitted = Signal(float)    # 进度条松手/点一下: 定下来(v161)
    scrubToggled = Signal(bool)           # 「进度条」开关: 中间是否显示进度条(v161)
    playheadMoved = Signal(float)    # 裁剪线动了(预览要跟着换画面, 不用先播放)
    captureRequested = Signal()      # 相机按钮 / C: 截图
    coverRequested = Signal()        # 设为封面: 把这一帧写进视频文件
    scopeChanged = Signal(str)       # 截图范围变了(current/selected/all)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._photos_by_path: dict[str, object] = {}
        self._playing = False
        self._frame = None           # 播放中的当前帧(预览窗跟着它走)
        self._still = None           # 裁剪线停住时抓来的那一帧(不播放也有画面)
        self._upper = None           # 上半区容器(v163: 预览高度上限看它)
        self._build()

    # ---------------------------------------------------------------- 构建
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # v128: 顶部那条「← 返回分组 / 剪辑」已经删掉 —— 剪辑现在是独立窗口,
        # 关窗即回、用不着返回按钮, 也没必要在页内再写一遍"剪辑"。
        # v162: 顶部那条「不建议剪辑 —— …」红条也删了: 导出按钮左侧的胶囊已经
        # 写着「× 不建议剪辑」, 顶上再来一条纯属重复占掉 34px 画面高度。
        # 原因改挂胶囊的悬停说明, 想看清单一律点胶囊(打开原因清单)。

        # ---- 中部: 素材框 | 预览 | 属性 ----
        # v163: 三栏之间不留白(用户: 页面空间宝贵, 只要能看出是不同元素就行)。
        # 各栏自己都有 1px 描边 + 独立底色, 靠这个就能分开, 不需要 6px 的空当。
        upper = QWidget()
        self._upper = upper            # v163: 算预览高度上限要用它
        upper.installEventFilter(self)  # 上部区变高变矮(拖分隔条)都要重算画面高度
        ul = QHBoxLayout(upper)
        ul.setContentsMargins(0, 0, 0, 0)
        ul.setSpacing(2)

        self.media = MediaPanel()
        self.media.setMinimumWidth(216)
        self.media.setMaximumWidth(400)
        self.media.deleteRequested.connect(self.mediaDeleteRequested.emit)
        self.media.picked.connect(self.mediaPreviewRequested.emit)
        self.media.blankClicked.connect(self.mediaBlankClicked.emit)
        self.media.playRequested.connect(self.mediaPlayRequested.emit)
        ul.addWidget(self.media, 0)

        self.preview = PreviewView("从左边素材框点 ＋ 加入时间线")
        self.preview.setMinimumHeight(160)
        self.preview.clicked.connect(self.togglePlayRequested.emit)

        # 预览列(v161): 画面在上, 进度条**紧贴画面下方**(同一列布局里)。
        # 以前进度条是浮在画面上的浮层, 位置靠 preview 几何手算 —— 拖中间那道
        # 分隔条时本页不 resize, 它就停在老地方, 而且还会压住画面。
        self._preview_col = QWidget()
        pc = QVBoxLayout(self._preview_col)
        pc.setContentsMargins(0, 0, 0, 0)
        pc.setSpacing(0)                 # v163: 进度条紧贴画面, 不留 2px 空当
        pc.addWidget(self.preview, 1)

        self.scrubber = MediaScrubber(self)
        self.scrubber.setFixedHeight(MediaScrubber.H)
        # v164: 不再"收起也占着 22px"。以前收起时还留着这块高度, 于是「进度条」
        # 一关, 画面底下就多出一条 22px 的空白 —— 用户: 不显示就别占地方。
        # 收掉的高度由 _fit_preview 还给时间线(它知道现在该多高)。
        sp = self.scrubber.sizePolicy()
        sp.setRetainSizeWhenHidden(False)
        self.scrubber.setSizePolicy(sp)
        self.scrubber.seekRequested.connect(self.mediaSeekRequested.emit)
        self.scrubber.seekCommitted.connect(self.mediaSeekCommitted.emit)
        pc.addWidget(self.scrubber, 0)
        # v163: 这一列的高度就是"画面 + 进度条", 多出来的高度给左右两栏的列表
        # (否则布局会在画面和进度条之间平摊空隙 —— 用户看到的"进度条没贴住").
        self._preview_col.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # v188: 左右栏宽度一变, 预览列会变宽但上部区整体尺寸没动 ——
        # 只听 upper 的 Resize 收不到这一下, 画面会卡在旧宽度算出的高度。
        self._preview_col.installEventFilter(self)
        ul.addWidget(self._preview_col, 1)
        ul.setAlignment(self._preview_col, Qt.AlignTop)

        # 操作反馈(v152): 截图成功之类的"成了"要看得见 —— 在预览上方浮一条
        # 绿字提示, 两秒多自己收掉。状态栏在最底下, 眼睛在画面中间, 很容易漏。
        self.toast = QLabel(self)
        self.toast.setObjectName("toast")
        self.toast.setAlignment(Qt.AlignCenter)
        self.toast.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.toast.setVisible(False)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.setInterval(2600)
        self._toast_timer.timeout.connect(lambda: self.toast.setVisible(False))

        # 右侧属性栏按用户要求先撤掉(后期重新设计再加回来)。
        # 仍然保留对象: 主窗口有一串 set_clip 调用, 留着不会崩, 也不占地方。
        self.panel = ClipPanel(self)
        self.panel.setVisible(False)

        # 右栏(v140): 轨道片段清单 —— 一个片段一张卡片, 可拖动改轨道顺序
        self.clip_list = ClipListPanel()
        ul.addWidget(self.clip_list, 0)

        # ---- 下部: 工具条 + 时间线 ----
        # v163: 四周不留白(空间宝贵), 只留 2px 让上下两块分得开
        lower = QWidget()
        ll = QVBoxLayout(lower)
        # v164: 顶部那 2px 撤掉 —— 进度条底下到工具条之间不该有空白(空间宝贵),
        # 上面那条 1px 分隔线本身就是边界。
        ll.setContentsMargins(0, 0, 0, 2)
        ll.setSpacing(3)

        self.timeline_view = TimelineWidget()
        self.timeline_view.playheadChanged.connect(self._on_playhead_changed)
        self.timeline_view.orderChanged.connect(self._on_order_changed)
        self.timeline_view.clipSelected.connect(self._on_clip_selected)

        bar = QHBoxLayout()
        bar.setSpacing(4)
        # 第二行(v135): 三个开关 + 原来「工具」菜单里的功能, 全部摆出来当按钮。
        # 为什么另起一行: 这些功能一共 6 个, 全挤在第一行会把 1360 宽的窗口顶满,
        # 再窄一点就被截断 —— 而且它们本来就不是每步都要点的东西。
        bar2 = QHBoxLayout()
        bar2.setSpacing(4)

        # 只摆能用的按钮: 做不出的先不摆, 免得一直去点没反应的东西。
        # 「播放」不摆 —— 点中间预览画面就是播放/暂停(和剪映一致);
        # 「添加素材」不摆 —— 素材框最后一个格子就是「＋ 添加素材」。
        self.split_btn = ToolButton("split", "分割", "在裁剪线处切开选中的片段（S）")
        self.del_btn = ToolButton("delete", "删除", "删除选中的片段（Delete）")
        self.split_btn.clicked.connect(self.splitRequested.emit)
        self.del_btn.clicked.connect(self.deleteRequested.emit)
        bar.addWidget(self.split_btn)
        bar.addWidget(self.del_btn)
        bar.addWidget(_vsep())

        self.fit_btn = ToolButton("fit", "全览", "缩放到整条时间线刚好铺满（Ctrl+0）")
        self.fit_btn.clicked.connect(self.timeline_view.fit_timeline)
        bar.addWidget(self.fit_btn)

        # 缩放滑轨(v169): 「全览」右边一条, 拖着就能缩放整条时间线。
        # 映射用对数刻度(MIN_PPS~MAX_PPS) —— 线性刻度下小倍率全挤在左端,
        # 拖半天出不来"每秒多少像素"的差别。
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setObjectName("zoomSlider")
        self.zoom_slider.setRange(0, 1000)
        self.zoom_slider.setFixedWidth(130)
        self.zoom_slider.setFixedHeight(22)
        self.zoom_slider.setFocusPolicy(Qt.NoFocus)
        self.zoom_slider.setToolTip("拖动缩放整条时间线（围着裁剪线）")
        self._zoom_syncing = False
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider)
        self.timeline_view.zoomChanged.connect(self._on_timeline_zoom)
        bar.addWidget(self.zoom_slider)
        bar.addWidget(_vsep())

        self.help_btn = ToolButton("help", "快捷键", "查看与修改快捷键（F1）")
        self.help_btn.clicked.connect(self.helpRequested.emit)
        bar.addWidget(self.help_btn)

        # v188: 「存剪辑 / 打开剪辑 / 存放位置」一组, 紧跟在「快捷键」右边。
        # 「组」= 前后各一条竖线分隔, 不是套一个浅底圆角框。
        self.project_sep_left = _vsep()
        bar.addWidget(self.project_sep_left)
        self.save_project_btn = _chip(
            "存剪辑", "把现在的时间线存成一个小文件，关掉软件也能接着剪…")
        self.save_project_btn.clicked.connect(
            lambda: self.toolRequested.emit("save_project"))
        bar.addWidget(self.save_project_btn)
        self.open_project_btn = _chip(
            "打开剪辑", "打开之前存的剪辑方案，恢复时间线接着剪…")
        self.open_project_btn.clicked.connect(
            lambda: self.toolRequested.emit("open_project"))
        bar.addWidget(self.open_project_btn)
        self.outdir_btn = _chip("存放位置", "改「截帧 / 导出 / 工具产出」存在哪个文件夹…")
        self.outdir_btn.clicked.connect(lambda: self.toolRequested.emit("out_dir"))
        bar.addWidget(self.outdir_btn)
        self.project_sep_right = _vsep()
        bar.addWidget(self.project_sep_right)

        # 磁吸/吸附: 剪映的两个开关, 直接摆在工具条上, 一眼能看到当前状态
        self.mag_btn = QPushButton("磁吸")
        self.mag_btn.setObjectName("chipToggle")
        self.mag_btn.setCheckable(True)
        self.mag_btn.setFocusPolicy(Qt.NoFocus)
        self.mag_btn.setCursor(Qt.PointingHandCursor)
        self.mag_btn.setToolTip("同轨自动相接")
        self.mag_btn.toggled.connect(self._on_magnetic_toggled)
        bar2.addWidget(self.mag_btn)

        self.snap_btn = QPushButton("吸附")
        self.snap_btn.setObjectName("chipToggle")
        self.snap_btn.setCheckable(True)
        self.snap_btn.setChecked(True)
        self.snap_btn.setFocusPolicy(Qt.NoFocus)
        self.snap_btn.setCursor(Qt.PointingHandCursor)
        self.snap_btn.setToolTip("拖动时自动对齐片段首尾、播放头（Alt）")
        self.snap_btn.toggled.connect(self._on_snap_toggled)
        bar2.addWidget(self.snap_btn)

        # v194: 「挤占」紧跟在「吸附」右侧。开启后把片段拖到已有片段之间时,
        # 落点之后的同轨片段整体顺延(插入编辑), 而不是被 free_slot 弹到末尾。
        self.crowd_btn = QPushButton("挤占")
        self.crowd_btn.setObjectName("chipToggle")
        self.crowd_btn.setCheckable(True)
        self.crowd_btn.setFocusPolicy(Qt.NoFocus)
        self.crowd_btn.setCursor(Qt.PointingHandCursor)
        self.crowd_btn.setToolTip("拖到已有片段之间时插入，后面的片段整体顺延（只影响目标轨）")
        self.crowd_btn.toggled.connect(self._on_crowd_toggled)
        bar2.addWidget(self.crowd_btn)

        # 跟随移动: 播放时把裁剪线钉在现在的位置, 让轨道往前滚。
        # (默认关 —— 平时想让裁剪线自己走, 只有看长片子才需要钉住)
        self.follow_btn = QPushButton("跟随移动")
        self.follow_btn.setObjectName("chipToggle")
        self.follow_btn.setCheckable(True)
        self.follow_btn.setFocusPolicy(Qt.NoFocus)
        self.follow_btn.setCursor(Qt.PointingHandCursor)
        self.follow_btn.setToolTip("播放时裁剪线停在现在的位置，轨道滚动")
        self.follow_btn.toggled.connect(self._on_follow_toggled)
        bar2.addWidget(self.follow_btn)

        # 进度条(v161): 决定中间画面下方那条要不要出现。默认开 —— 平时看素材/
        # 看某一段/播整条轨道/拖裁剪线都要有进度可看, 只有想要干净画面时才关。
        self.scrub_btn = QPushButton("进度条")
        self.scrub_btn.setObjectName("chipToggle")
        self.scrub_btn.setCheckable(True)
        self.scrub_btn.setChecked(True)
        self.scrub_btn.setFocusPolicy(Qt.NoFocus)
        self.scrub_btn.setCursor(Qt.PointingHandCursor)
        self.scrub_btn.setToolTip("中间画面下方显示进度条（关掉就是干净画面）")
        self.scrub_btn.toggled.connect(self.scrubToggled.emit)
        bar2.addWidget(self.scrub_btn)

        # v188: 「声波」+ 音量拖动条 + 数值 = 一组, 前后各一条竖线分隔。
        # 这个 QWidget 只是把控件收在一起量宽度, 本身完全透明、没有底框。
        self.volume_group = QWidget()
        vg = QHBoxLayout(self.volume_group)
        vg.setContentsMargins(0, 0, 0, 0)
        vg.setSpacing(0)

        # v194: 「关键帧」和「声波」同属这一组显示开关, 摆在声波前面共用竖线。
        self.keyframe_btn = QPushButton("关键帧")
        self.keyframe_btn.setObjectName("chipToggle")
        self.keyframe_btn.setCheckable(True)
        self.keyframe_btn.setChecked(True)
        self.keyframe_btn.setFocusPolicy(Qt.NoFocus)
        self.keyframe_btn.setCursor(Qt.PointingHandCursor)
        self.keyframe_btn.setToolTip("全局显示片段上的关键帧刻度（红色细竖线＝能无损下刀的位置）")
        self.keyframe_btn.toggled.connect(self._on_keyframe_toggled)
        vg.addWidget(self.keyframe_btn)
        vg.addSpacing(6)

        self.wave_btn = QPushButton("声波")
        self.wave_btn.setObjectName("chipToggle")
        self.wave_btn.setCheckable(True)
        self.wave_btn.setFocusPolicy(Qt.NoFocus)
        self.wave_btn.setCursor(Qt.PointingHandCursor)
        self.wave_btn.toggled.connect(self._on_wave_toggled)
        vg.addWidget(self.wave_btn)
        vg.addSpacing(6)

        self.vol_slider = _VolumeSlider()
        self.vol_slider.setObjectName("volumeSlider")
        self.vol_slider.setRange(0, 200)
        self.vol_slider.setSingleStep(1)
        self.vol_slider.setPageStep(5)
        self.vol_slider.setValue(100)
        self.vol_slider.setFixedWidth(96)
        self.vol_slider.setFixedHeight(22)
        self.vol_slider.setFocusPolicy(Qt.NoFocus)
        self.vol_slider.setCursor(Qt.PointingHandCursor)
        vg.addWidget(self.vol_slider)
        vg.addSpacing(8)

        # 数字框里只有数字, 单位摆在框外面(点单位一样能进编辑)。
        self.vol_pct = _VolumeValue(32, self)
        self.pct_unit = _VolumeUnit("%", self.vol_pct)
        self.vol_db = _VolumeValue(40, self)
        self.db_unit = _VolumeUnit("dB", self.vol_db)
        vg.addWidget(self.vol_pct)
        vg.addWidget(self.pct_unit)
        # 数字变短时空出来的宽度由这两块补位吃掉: 「100%」贴紧, 整组又不忽宽忽窄
        self.pct_gap = QWidget()
        self.pct_gap.setFixedHeight(1)
        self.pct_gap.setFixedWidth(8)
        vg.addWidget(self.pct_gap)
        vg.addWidget(self.vol_db)
        vg.addSpacing(2)          # "-6.0 dB" 之间留一个字的空隙
        vg.addWidget(self.db_unit)
        self.vol_tail = QWidget()
        self.vol_tail.setFixedHeight(1)
        self.vol_tail.setFixedWidth(0)
        vg.addWidget(self.vol_tail)

        self._vol_value = 1.0
        self._vol_applied = 1.0
        self._vol_timer = QTimer(self)
        self._vol_timer.setSingleShot(True)
        # 拖完不马上写片段: 等过双击判定窗口再提交, 双击归位才不会被
        # 前面那两下点击拆成好几步撤销。
        self._vol_timer.setInterval(QApplication.doubleClickInterval() + 60)
        self._vol_timer.timeout.connect(self._commit_volume)
        self.vol_slider.valueChanged.connect(self._on_volume_slider)
        self.vol_slider.resetRequested.connect(self._on_volume_reset)
        self._set_volume_display(1.0)
        self.volume_sep_left = _vsep()
        self.volume_sep_right = _vsep()
        bar2.addWidget(self.volume_sep_left)
        bar2.addWidget(self.volume_group)
        bar2.addWidget(self.volume_sep_right)

        # 原来「工具」菜单里的东西(v135): 全部做成按钮摆出来, 不用再点开菜单找。
        # 「波形」和菜单里那条重复, 菜单那份删了(上面已经有开关);
        # 截图 / 设为封面也早就在第一行和快捷键里, 不再重复一份。
        # v192: 用户定的顺序 —— 帧率/倍速 → 精简 → 重排 → 对齐关键帧 → 检测。
        # 只调 addWidget 的先后, 按钮对象与信号一个都不动。
        # 帧率按钮直接写"现在多少 fps / 多少倍速"(v136/v165): 先看到数, 再决定
        # 要不要改。没选中片段时是「帧率/倍速」。
        self.fps_btn = _chip("帧率/倍速", "帧率/倍速（改帧率就是变速，无损）")
        # 按钮文字会在「帧率/倍速」和「45/1.3」之间换: 不钉住最小宽度的话,
        # 一选片段这排按钮整排往左挪十几像素(数字比字窄), 看着像闪一下。
        self.fps_btn.ensurePolished()
        self.fps_btn.setMinimumWidth(
            max(56, self.fps_btn.sizeHint().width()))
        self.fps_btn.clicked.connect(lambda: self.toolRequested.emit("fps"))
        bar2.addWidget(self.fps_btn)

        self.decimate_btn = _chip("精简", "只留关键帧（文件小很多，动作会跳）…")
        self.decimate_btn.clicked.connect(lambda: self.toolRequested.emit("decimate"))
        bar2.addWidget(self.decimate_btn)

        # v188: 存/取剪辑方案与「存放位置」已搬到第一行「快捷键」右边;
        # 这一行只留整条时间线的整体重排。
        self.reorder_btn = _chip(
            "重排", "把轨道上的片段整体重排（倒序 / 按拍摄时间 / 按文件名）")
        self.reorder_btn.clicked.connect(self._show_reorder_menu)
        bar2.addWidget(self.reorder_btn)

        self.kf_btn = _chip("对齐关键帧", "把选中片段的首尾挪到最近的关键帧上\n"
                                        "（只有关键帧处下刀才是真无损）")
        self.kf_btn.clicked.connect(lambda: self.toolRequested.emit("align_kf"))
        bar2.addWidget(self.kf_btn)

        self.detect_btn = _chip("检测", "黑场 / 无声 / 场景变化 / 关键帧…")
        self.detect_btn.clicked.connect(lambda: self.toolRequested.emit("detect"))
        bar2.addWidget(self.detect_btn)
        bar2.addStretch(1)
        self._sync_tool_buttons()
        # 截图设置(v133): 相机 + 范围 + 格式 + 质量(C = 截图, 存在源文件旁边)。
        # 放在工具条上而不是设置页里 —— 截完发现格式不对, 顺手就能改。
        # v135: 「截图」文字→相机图标按钮; 加"范围"下拉(当前/选中/所有);
        #       质量改下拉(100~50 六档); PNG 没有质量 — **整个框隐藏**, 不留个灰的。
        bar.addWidget(_vsep())
        self.shot_btn = QPushButton()
        self.shot_btn.setObjectName("shotBtn")
        self.shot_btn.setFixedSize(32, 28)
        self.shot_btn.setCursor(Qt.PointingHandCursor)
        self.shot_btn.setFocusPolicy(Qt.NoFocus)
        self.shot_btn.setIcon(make_icon("camera", T.TEXT_SUB, 18))
        self.shot_btn.setIconSize(QSize(18, 18))
        self.shot_btn.setToolTip(self._shot_tip())
        self.shot_btn.clicked.connect(self.captureRequested.emit)
        bar.addWidget(self.shot_btn)

        # 范围: 当前(裁剪线这一帧) / 选中(选中片段里的每个关键帧各一张) /
        #       所有(整条轨道每个片段的每个关键帧各一张)
        # v150: 改成点一下换一个(循环), 不再用下拉 —— 只有三个值, 下拉要两次点击。
        self.shot_scope = QPushButton("当前")
        self.shot_scope.setObjectName("shotScope")
        # v152: 宽度只要容下两个字 —— 74 是照"当前 / 选中 / 所有"最长的算的,
        #       实际三个值都是两个字, 留一半空白看着像没填满。
        self.shot_scope.setFixedSize(40, 28)
        self.shot_scope.setFocusPolicy(Qt.NoFocus)
        self.shot_scope.setCursor(Qt.PointingHandCursor)
        self._scope = "current"
        self.shot_scope.setToolTip(
            "当前：裁剪线正对这一帧，只截一张\n"
            "选中：选中的片段，每个关键帧各一张\n"
            "所有：时间线上每段的每个关键帧，各一张")
        self.shot_scope.clicked.connect(self._cycle_shot_scope)
        bar.addWidget(self.shot_scope)

        self.shot_fmt = _DownCombo()
        self.shot_fmt.setObjectName("shotFmt")
        self.shot_fmt.setFixedWidth(66)
        self.shot_fmt.setFixedHeight(28)
        self.shot_fmt.setFocusPolicy(Qt.NoFocus)
        self.shot_fmt.setCursor(Qt.PointingHandCursor)
        for label, val in (("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp")):
            self.shot_fmt.addItem(label, val)
        self.shot_fmt.setToolTip("截图格式")
        bar.addWidget(self.shot_fmt)

        # 质量: 下拉六档(100/90/80/70/60/50)。PNG 是无损, 整个框不显示。
        self.shot_q = _DownCombo()
        self.shot_q.setObjectName("shotFmt")
        self.shot_q.setFixedWidth(64)
        self.shot_q.setFixedHeight(28)
        self.shot_q.setFocusPolicy(Qt.NoFocus)
        self.shot_q.setCursor(Qt.PointingHandCursor)
        for v in (100, 90, 80, 70, 60, 50):
            self.shot_q.addItem(str(v), v)
        self.shot_q.setToolTip("截图质量")
        bar.addWidget(self.shot_q)

        # 设为封面(v152): 把裁剪线这一帧写成这个视频文件的封面。
        # 和"截图"是两件事(截图存成图片文件; 这个是改视频本身), 所以单独一个按钮。
        self.cover_btn = _chip("设为封面", "把裁剪线这一帧写成这个视频的封面\n"
                                         "（不重新编码，只多一条封面图）")
        self.cover_btn.clicked.connect(self.coverRequested.emit)
        bar.addWidget(self.cover_btn)
        self._shot_quality = 90      # 真实质量(PNG 时框隐藏, 这个值照样留着)
        self._load_shot_prefs()
        self.shot_fmt.currentIndexChanged.connect(self._on_shot_changed)
        self.shot_q.currentIndexChanged.connect(self._on_shot_qty_changed)
        self._sync_shot_quality_visible()

        bar.addStretch(1)
        self.time_label = QLabel("00:00.0 / 00:00.0")
        self.time_label.setStyleSheet(
            f"color: {T.TEXT_SUB}; font-size: 12px; font-family: Consolas, monospace;")
        bar.addWidget(self.time_label)

        bar.addSpacing(12)
        # 无损状态胶囊: 一眼看到这次能不能无损, 点开是完整原因清单
        self.loss_chip = QPushButton("—")
        self.loss_chip.setFixedHeight(26)
        self.loss_chip.setFocusPolicy(Qt.NoFocus)
        self.loss_chip.setCursor(Qt.PointingHandCursor)
        self.loss_chip.clicked.connect(self.lossReportRequested.emit)
        bar.addWidget(self.loss_chip)

        self.export_btn = ToolButton("export", "导出", "按时间线导出（无损优先）")
        self.export_btn.clicked.connect(self.exportRequested.emit)
        bar.addWidget(self.export_btn)

        ll.addLayout(bar)
        ll.addLayout(bar2)
        ll.addWidget(self.timeline_view, 1)

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(upper)
        self.splitter.addWidget(lower)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([470, 380])
        # v163: 拖分隔条 = 上部可用高度变了, 画面得重新按比例收紧
        self.splitter.splitterMoved.connect(lambda *_: self._fit_preview())
        upper.setMinimumHeight(200)
        lower.setMinimumHeight(280)
        root.addWidget(self.splitter, 1)
        # 滑轨先摆到当前缩放级别(布局算完宽度才知道"全览"是多少, 推一拍)
        QTimer.singleShot(0, self.sync_zoom_slider)

    def _show_reorder_menu(self):
        """「重排」的四选一: 反转 / 拍摄时间两种方向 / 文件名。

        用菜单而不是四个按钮: 这本来就是"选一种排法", 不是四个独立动作,
        全摊开会把第二行工具条顶满(v135 的教训)。
        """
        m = QMenu(self)
        items = (("反转顺序", "reorder_reverse"),
                 ("按拍摄时间（新→旧）", "reorder_time_new"),
                 ("按拍摄时间（旧→新）", "reorder_time_old"),
                 ("按文件名", "reorder_name"))
        for label, key in items:
            act = m.addAction(label)
            act.triggered.connect(
                lambda _checked=False, k=key: self.toolRequested.emit(k))
        m.exec(self.reorder_btn.mapToGlobal(self.reorder_btn.rect().bottomLeft()))

    # ---------------------------------------------------------------- 数据
    def set_photos(self, photos):
        """主窗口传入当前照片列表(用于按路径找缩略图)。"""
        self._photos_by_path = {p.path: p for p in photos}
        thumbs = {}
        for p in photos:
            pm = getattr(p, "thumb", None)
            if pm is not None and not pm.isNull():
                thumbs[p.path] = pm
        self.timeline_view.set_photo_thumbs(thumbs)

    def set_timeline(self, timeline):
        self.timeline_view.set_timeline(timeline)
        # v177: 进剪辑时按「默认开启」那套开关初始化(不影响用户当次手动改动)
        try:
            fd = settings.load_feature_defaults()
            timeline.magnetic = bool(fd["magnetic"])
            timeline.snap_on = bool(fd["snap"])
            self.timeline_view.set_follow(bool(fd["follow"]))
            self.timeline_view.set_wave_visible(bool(fd["wave"]))
            self.timeline_view.set_keyframes_visible(bool(fd["keyframes"]))
            self.timeline_view.set_crowd(bool(fd["crowd"]))
            self.follow_btn.setChecked(bool(fd["follow"]))
            self.wave_btn.setChecked(bool(fd["wave"]))
            self.keyframe_btn.setChecked(bool(fd["keyframes"]))
            self.crowd_btn.setChecked(bool(fd["crowd"]))
            self.mag_btn.setChecked(bool(fd["magnetic"]))
            self.snap_btn.setChecked(bool(fd["snap"]))
        except Exception:
            pass
        self._refresh_time_label()
        self._sync_buttons()
        self.refresh_shot_keyframe_state()
        self.sync_volume_control()      # v187: 进剪辑先把音量组摆到正确状态

    def selected_clip(self):
        return self.timeline_view.selected_clip()

    # ---------------------------------------------------- 缩放滑轨(v169)
    def _zoom_lo(self) -> float:
        """滑轨左端对应的 pps = 当前"全览"档(动态: 内容/宽度变了它会变)。"""
        try:
            return max(TL_MIN_PPS, self.timeline_view.min_pps())
        except Exception:
            return TL_MIN_PPS

    def _on_zoom_slider(self, value: int):
        """滑轨动了 → 缩放时间线(围裁剪线, 和滚轮缩放同一套锚点)。

        映射用对数刻度 —— 线性刻度下"全览~2×全览"那段全挤在左端,
        拖半天出不来"每秒多少像素"的差别。左端就是「全览」。
        """
        if self._zoom_syncing:
            return
        from math import log
        lo, hi = self._zoom_lo(), TL_MAX_PPS
        pps = lo * (hi / lo) ** (max(0, min(1000, int(value))) / 1000.0)
        self.timeline_view.set_pps(pps)

    def _on_timeline_zoom(self, pps: float):
        """时间线缩放级别变了(滚轮/快捷键/全览) → 滑轨挪到对应位置。"""
        if self._zoom_syncing:
            return
        from math import log
        lo, hi = self._zoom_lo(), TL_MAX_PPS
        try:
            pps = max(lo, min(hi, float(pps)))
            v = int(round(1000 * log(pps / lo) / log(hi / lo)))
        except Exception:
            return
        self._zoom_syncing = True
        try:
            self.zoom_slider.setValue(max(0, min(1000, v)))
        finally:
            self._zoom_syncing = False

    def sync_zoom_slider(self):
        """窗口刚建好/时间线刚换时, 把滑轨摆到当前缩放级别上。"""
        self._on_timeline_zoom(self.timeline_view._pps)

    def _on_magnetic_toggled(self, on: bool):
        tl = self.timeline_view.timeline
        if tl is None:
            return
        tl.magnetic = bool(on)
        if on:
            for t in range(tl.track_count):
                tl.compact(t)
            self.timeline_view.refresh()

    def _on_wave_toggled(self, on: bool):
        self.timeline_view.set_wave_visible(bool(on))
        self.waveformToggled.emit(bool(on))

    def _on_keyframe_toggled(self, on: bool):
        """v194: 全局关键帧刻度显示开关(和声波同一组)。"""
        tv = getattr(self, "timeline_view", None)
        if tv is not None:
            tv.set_keyframes_visible(bool(on))

    def _on_crowd_toggled(self, on: bool):
        """v194: 挤占只影响拖动落位, 不写进时间线快照(不是编辑内容)。"""
        tv = getattr(self, "timeline_view", None)
        if tv is not None:
            tv.set_crowd(bool(on))

    # ---------------------------------------------------- 音量拖动条(v187/v188)
    def playhead_clip(self):
        """裁剪线正下方**恰好只有一个**片段时返回它, 重叠/空白都返回 None。

        v188: 没手动选中片段时, 帧率按钮和音量条就拿它当显示对象。
        """
        tl = self.timeline_view.timeline
        if tl is None:
            return None
        t = float(getattr(tl, "playhead", 0.0) or 0.0)
        eps = 1e-6
        hits = [c for c in (getattr(tl, "clips", None) or [])
                if float(getattr(c, "start", 0.0)) - eps <= t
                < float(getattr(c, "end", 0.0)) - eps]
        return hits[0] if len(hits) == 1 else None

    def volume_targets(self):
        """能调音量的片段: 选中的优先; 没选中就看裁剪线上那唯一一段。

        静音的、声音已分离走的不算。注意这里**不改选中状态** —— 只决定
        显示什么、改动落在谁身上; 真正要改时才由主窗口把那一段选中。
        """
        try:
            sel = [c for c in self.timeline_view.selected_clips()
                   if getattr(c, "kind", "") in ("video", "audio")
                   and not c.silent]
        except Exception:
            sel = []
        if sel:
            return sel
        c = self.playhead_clip()
        if c is None or getattr(c, "kind", "") not in ("video", "audio") \
                or c.silent:
            return []
        return [c]

    def _volume_targets(self):
        return self.volume_targets()

    @staticmethod
    def _volume_text(value: float):
        """「50%」+「-6.0 dB」: 音量是幅度倍数, dB = 20·log10(倍数)。"""
        from math import log10
        v = max(0.0, min(2.0, float(value)))
        pct = int(round(v * 100))
        if v <= 0.0:
            # v189: 静音也要带 dB 单位(用户要求读数格式统一), 0% 就是 -∞ dB。
            return f"{pct}%", "-∞ dB"
        db = 20.0 * log10(v)
        if abs(db) < 0.05:
            return f"{pct}%", "0.0 dB"
        return f"{pct}%", f"{db:+.1f} dB"

    def _set_volume_text(self, value: float):
        """数字框里只放数字; % 和 dB 是框外的普通文字。"""
        pct, db = self._volume_text(value)
        self.vol_pct.display(pct[:-1])          # "50%" → "50"
        self.vol_db.display(db[:-3].strip())    # "-6.0 dB" → "-6.0" / "-∞ dB" → "-∞"
        self.db_unit.setVisible(True)           # v189: 静音时也照样显示 dB
        self._rebalance_volume_widths()

    def _rebalance_volume_widths(self):
        """数字框按内容缩宽后, 把省下的宽度还给补位, 后面的控件不挪位置。"""
        self.pct_gap.setFixedWidth(
            max(0, 8 + self.vol_pct._base_w - self.vol_pct.width()))
        self.vol_tail.setFixedWidth(
            max(0, self.vol_db._base_w - self.vol_db.width()))

    def _set_volume_display(self, value: float):
        """滑块 + 两处数字一起摆到某个音量(不触发提交)。"""
        v = max(0.0, min(2.0, float(value)))
        self._vol_value = v
        self._vol_applied = v
        self._vol_timer.stop()
        self.vol_slider.blockSignals(True)
        self.vol_slider.setValue(int(round(v * 100)))
        self.vol_slider.blockSignals(False)
        self._set_volume_text(v)

    def sync_volume_control(self, clip=None):
        """选区/裁剪线一变就跟着换数字 / 灰亮(撤销、重排也走这里)。

        v188: 没选中但裁剪线上恰好一段时, 显示这一段的音量并允许改,
        **不**顺手把它选上 —— 选中效果留到真正改的那一下。
        clip: 调用方明确给了片段就以它为准(还没落选中状态的入口用)。
        """
        if clip is not None:
            targets = ([clip] if getattr(clip, "kind", "") in ("video", "audio")
                       and not clip.silent else [])
            has_sel = bool(targets)
        else:
            targets = self.volume_targets()
            try:
                has_sel = bool(self.timeline_view.selected_clips())
            except Exception:
                has_sel = False
        on = bool(targets)
        for w in (self.vol_slider, self.vol_pct, self.pct_unit,
                  self.vol_db, self.db_unit):
            w.setEnabled(on)
        self.vol_slider.set_handle_enabled(on)
        if not on:
            tip = "先选中片段，或把裁剪线移到一个片段上"
        elif has_sel:
            tip = (f"选中 {len(targets)} 段，一起改音量（导出时才生效）"
                   "；双击归位 100%")
        else:
            tip = "裁剪线这一段：拖动就改它（会先选中）；双击归位 100%"
        self.vol_slider.setToolTip(tip)
        for w in (self.vol_pct, self.vol_db, self.pct_unit, self.db_unit):
            w.setToolTip("点一下可以手填数值" if on else tip)
        v = 1.0
        if on:
            try:
                v = float(getattr(targets[0], "volume", 1.0))
            except (TypeError, ValueError):
                v = 1.0
        self._set_volume_display(v)

    def _on_volume_slider(self, value: int):
        self._vol_value = max(0.0, min(2.0, value / 100.0))
        self._set_volume_text(self._vol_value)
        # 拖的过程中只换数字; 提交延后到双击判定窗口过去, 一次拖动 / 一次
        # 双击归位都只记一步撤销。
        self._vol_timer.start()

    def _on_volume_reset(self):
        """双击拖动条 = 归位到 100% / 0.0 dB。"""
        self._vol_timer.stop()
        self.vol_slider.blockSignals(True)
        self.vol_slider.setValue(100)
        self.vol_slider.blockSignals(False)
        self._vol_value = 1.0
        self._set_volume_text(1.0)
        if abs(self._vol_applied - 1.0) < 1e-6:
            return
        self._vol_applied = 1.0
        self.volumeRequested.emit(1.0)

    def _commit_volume(self):
        self._vol_timer.stop()
        if abs(self._vol_value - self._vol_applied) < 1e-6:
            return
        self._vol_applied = self._vol_value
        self.volumeRequested.emit(float(self._vol_value))

    def _volume_edit_begin(self):
        # 手输数字这段时间先把剪辑键位停一下, 免得数字键被 QShortcut 抢走。
        self.volumeEditing.emit(True)
        self._rebalance_volume_widths()   # 编辑态框变宽, 补位跟着收

    def _volume_edit_commit(self, which, text):
        self.volumeEditing.emit(False)
        if text is None or not text:
            self.sync_volume_control()      # Esc / 空 → 恢复原来的数字
            return
        raw = (text.replace("%", "").replace("dB", "").replace("DB", "")
               .replace("db", "").strip())
        if "∞" in raw or "静音" in raw:
            # v189: 静音读数就是「-∞」, 手填它(或「静音」)也要能设回 0%。
            vol = 0.0
        else:
            try:
                num = float(raw)
            except (TypeError, ValueError):
                self.sync_volume_control()
                return
            if which is self.vol_db:
                vol = 0.0 if num <= -60.0 else 10.0 ** (min(num, 6.0206) / 20.0)
            else:
                vol = max(0.0, min(200.0, num)) / 100.0
        vol = max(0.0, min(2.0, vol))
        self._set_volume_display(vol)
        self.volumeRequested.emit(float(vol))

    def _on_snap_toggled(self, on: bool):
        tl = self.timeline_view.timeline
        if tl is None:
            return
        tl.snap_on = bool(on)

    def _on_follow_toggled(self, on: bool):
        """「跟随移动」: 只是个看的方式, 不改时间线数据。"""
        self.timeline_view.set_follow(bool(on))

    # ---------------------------------------------------------------- 播放头
    def nudge_playhead(self, delta: float):
        tl = self.timeline_view.timeline
        if tl is None:
            return
        total = tl.total_duration()
        t = max(0.0, min(total, tl.playhead + delta))
        self.timeline_view.set_playhead(t)
        self._refresh_time_label()
        self.playheadMoved.emit(t)
        # v186: 代码设播放头不发 playheadChanged, 截图按钮的蓝底会停在旧状态
        self.refresh_shot_keyframe_state()

    def set_playhead(self, t: float):
        self.timeline_view.set_playhead(t)
        tl = self.timeline_view.timeline
        real = float(tl.playhead) if tl is not None else float(t or 0.0)
        self._refresh_time_label()
        self.playheadMoved.emit(real)
        # v186: 上/下一个关键帧、K 贴关键帧、跳开头结尾全走这里; 以前只有
        # 鼠标拖裁剪线(发 playheadChanged)才重判, 所以按快捷键跳完按钮不蓝。
        self.refresh_shot_keyframe_state()

    # ---------------------------------------------------------------- 预览
    def show_clip(self, clip, text: str = ""):
        """预览窗显示选中片段: 有缩略图用缩略图, 否则显示类型与文件名。"""
        self._sync_buttons()
        self._still = None
        if clip is None:
            # 顺序要紧: 先清画面再 setText, 反了会把文字吞掉
            self.preview.set_source(None)
            self.preview.setText(text or "从左边素材框点 ＋ 加入时间线")
            self._fit_preview()
            return
        photo = self._photos_by_path.get(clip.source)
        pm = None
        if clip.kind == "image":
            # 图片就用原图缩到预览窗大小(v150): 主列表那张缩略图只有格子那么大,
            # 铺满预览窗必然是糊的。
            # v163: 高度按"上部区能有多高"给(不是当前画面高度) —— 图片换上之后
            # 预览高度会按它的比例重算, 按旧高度读图会读小、再放就糊了。
            cap_h = 480
            try:
                if self._upper is not None and self._upper.height() > 80:
                    cap_h = self._upper.height()
            except Exception:
                pass
            pm = self._large_image(clip.source,
                                   max(60, self.preview.width()),
                                   max(60, cap_h))
        if pm is None:
            pm = getattr(photo, "thumb", None) if photo is not None else None
        if pm is None or pm.isNull():
            # 主列表没缩略图(比如剪辑态里现加进来的素材) → 用时间线已经抽到的帧
            pm = self.timeline_view.cached_frame(clip.source)
        if pm is not None and not pm.isNull():
            # v163: 交给 PreviewView 自己缩放(存源图, 尺寸变了会重缩)
            self.preview.set_source(pm)
        elif clip.kind == "audio":
            self.preview.set_source(None)
            self.preview.setText(f"音频片段\n{clip.name}")
        else:
            self.preview.set_source(None)
            self.preview.setText(f"{clip.kind_label}片段\n{clip.name}")
        self._fit_preview()

    @staticmethod
    def _large_image(path: str, w: int, h: int):
        """按预览窗大小读一张图片(读的时候就缩好, 不占内存也不糊)。"""
        try:
            r = QImageReader(path)
            r.setAutoTransform(True)      # 手机照片的旋转方向要认
            s = r.size()
            if not s.isValid() or s.width() <= 0 or s.height() <= 0:
                return None
            k = min(max(1, w) / float(s.width()), max(1, h) / float(s.height()))
            if k < 1.0:
                r.setScaledSize(QSize(max(1, int(s.width() * k)),
                                      max(1, int(s.height() * k))))
            img = r.read()
            if img is None or img.isNull():
                return None
            return QPixmap.fromImage(img)
        except Exception:
            return None

    # ---------------------------------------------------------------- 播放
    def set_playing(self, on: bool):
        """播放态标记(播放按钮已经撤掉, 点画面即可)。

        v149: 画面上的悬停提示全部取消 —— "点画面播放/暂停"属于看一眼就会用的事,
        鼠标停在画面上就冒一块提示反而挡画面。
        """
        self._playing = bool(on)
        self.preview.setToolTip("")

    def is_playing(self) -> bool:
        return self._playing

    def show_frame(self, img):
        """播放中的一帧(原样塞进预览窗, 不缩到缩略图尺寸)。"""
        if img is None or img.isNull():
            return
        self._frame = img
        self._still = None
        self.preview.setText("")
        # v163: 交源图, 由 PreviewView 按当前内容区缩(尺寸一变就重缩)
        self.preview.set_source(QPixmap.fromImage(img))
        self._fit_preview()

    def show_still(self, img):
        """裁剪线停住时抓来的那一帧(不播放也有画面)。"""
        if img is None or img.isNull():
            return
        self._still = img
        self.preview.setText("")
        self.preview.set_source(QPixmap.fromImage(img))
        self._fit_preview()

    def _scaled_frame(self, img):
        """按预览窗的**物理像素**缩放(v163 起由 PreviewView 自己重缩, 这里保留给
        外部一次性用途)。抓帧那边已经按窗口宽度要了足够大的图
        (见 main_window._preview_out_size), 这里只做缩小。
        """
        dpr = 1.0
        try:
            dpr = float(self.preview.devicePixelRatioF() or 1.0)
        except Exception:
            dpr = 1.0
        w = max(60, int(self.preview.width() * dpr))
        h = max(60, int(self.preview.height() * dpr))
        out = img.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        try:
            out.setDevicePixelRatio(dpr)
        except Exception:
            pass
        return out


    def clear_frame(self):
        self._frame = None

    # ---------------------------------------------------------------- 进度条
    def _scrub_h(self) -> int:
        """进度条现在真占的高度(收起来就是 0)。

        ⚠️ 用 `isHidden()` 而不是 `isVisible()`: 剪辑窗口还没显示出来的时候,
        子控件的 isVisible() 也是 False —— 那会让"该占 22px"被算成 0, 上部被压
        矮一条, 等窗口一显示进度条就把画面挤掉一块。
        """
        try:
            return 0 if self.scrubber.isHidden() else MediaScrubber.H
        except Exception:
            return MediaScrubber.H

    def _fit_preview(self, force: bool = False):
        """按画面比例收紧预览高度(v163)。

        用户: 页面空间宝贵, 进度条要贴着**画面**下沿。以前预览窗吃满上部高度,
        画面按比例居中 → 上下各留一条黑边, 进度条贴的是"控件"下沿, 中间就夹着
        那条黑边。现在预览窗高度 = 宽度 ÷ 画面比例(超过可用高度才夹住),
        画面正好铺满, 进度条紧贴其下; 省出来的高度归素材框/片段列表。

        force=True: 「进度条」开关切换时用 —— 那时候上部**该长该缩都得照办**
        (平时只管收不回来的那半: 用户往下拖分隔条是想把时间线拉高, 不能拽回去)。
        """
        pv = self.preview
        w = pv.width()
        # v188: 预览列变宽时(窗口变宽 / 工具条撑宽了窗口), 画面该跟着长高。
        # 以前只写了"上部偏高就收回"那半边, 宽度变大后 want 变大却长不回去 ——
        # 画面被钉在旧宽度算出来的高度, 进度条底下就多出一条空白。
        # 只在**宽度真的变大**时补这半边: 用户主动把分隔条往矮拖(宽度没变)照旧尊重。
        last_w = getattr(self, "_fit_last_w", None)
        self._fit_last_w = w
        grew = last_w is not None and w > last_w + 1
        # 还没画面(空时间线 / 只有文字提示)时按 16:9 占位, 不然预览会被压成一条线
        ratio = pv.aspect() or (16.0 / 9.0)
        if w < 60 or ratio <= 0.05:
            return                     # 布局还没算完
        bar_h = self._scrub_h()
        try:
            sizes = self.splitter.sizes()
        except Exception:
            sizes = []
        total = (sizes[0] + sizes[1]) if len(sizes) == 2 else 0
        if (force or grew) and total > 200:
            # 开关刚切完 / 预览列刚变宽: 上部高度还停在上一种状态上 ——
            # 按总量重算, 别拿旧高度当上限(否则画面会一直缩在旧尺寸里)。
            cap = max(140, total - 120 - bar_h)
        else:
            cap = 420                  # 上部可用高度还不知道时的兜底
            try:
                if self._upper is not None and self._upper.height() > 80:
                    # 上限只能扣掉进度条那一条: 扣多了(以前还多留 4px)会让上部
                    # 永远比"画面+进度条"高一点, 那点高度就变成画面和下方之间
                    # 的一条空白。
                    cap = max(140, self._upper.height() - bar_h)
            except Exception:
                pass
        want = int(round(w / float(ratio)))
        h = max(120, min(want, cap))
        if not (pv.minimumHeight() == h and pv.maximumHeight() == h):
            pv.setFixedHeight(h)
            # 高度变了, 画面要按新内容区重缩一次(没有源图时它自己会跳过)
            QTimer.singleShot(0, pv.apply_scale)
        # 上部多出来的空块(画面+进度条撑不满的部分)直接还给时间线:
        # 空间宝贵, 别在画面下面留一片空白。拖大分隔条也不会多看出画面 —— 宽度
        # 才是画面的限制, 所以这里收回去不会让人"拖不动"。
        # ⚠️ 必须按"当前总量"给两侧: setSizes 的两个值加起来小于实际高度时,
        #    Qt 会把差额再分回来(实测要求 294 却拿到 296), 那 2px 就是用户看到的
        #    "进度条下面还有一条缝"。
        # ⚠️ 平时只在"上部偏高"时收回: 往矮了拖是用户想把时间线拉高(画面会按 cap
        #    自己缩), 这时候把他拽回来就成了"分隔条拖不动"。
        try:
            need = h + bar_h
            if len(sizes) == 2 and (force or grew or sizes[0] - need > 1):
                self.splitter.setSizes([need, max(120, total - need)])
        except Exception:
            pass

    def _place_scrubber(self):
        """进度条已改成"紧贴画面下方的一条"(v161), 位置交给布局, 这里只守高度。

        v160 及以前它是浮在预览画面上的浮层: 位置靠 `preview` 的几何手算。
        拖动中间那道分隔条(改时间线占的高度)时本页自己不 resize, 算出来的
        位置就一直停在老地方 —— 用户看到的就是"画面缩了, 进度条还在原地"。
        现在它是预览列里的一个普通控件: 画面怎么变都跟着走, 也绝不会压住画面。
        """
        h = MediaScrubber.H
        if self.scrubber.height() != h:
            self.scrubber.setFixedHeight(h)

    def show_scrubber(self, duration: float):
        self.scrubber.set_range(duration)
        self.scrubber.set_pos(0.0)
        self._place_scrubber()
        was = not self.scrubber.isHidden()
        self.scrubber.setVisible(True)
        if not was:
            # 显出来就要占 22px: 上部得跟着长高, 不然画面会把进度条挤没
            QTimer.singleShot(0, lambda: self._fit_preview(True))

    def hide_scrubber(self):
        was = not self.scrubber.isHidden()
        self.scrubber.setVisible(False)
        if was:
            # 收起来就别再占那 22px: 高度还给时间线(不是留一条白带)
            QTimer.singleShot(0, lambda: self._fit_preview(True))

    # ------------------------------------------------------------ 浮层提示
    def flash(self, text: str, kind: str = "ok"):
        """在预览上方浮一条提示, 两秒多自动收掉。

        kind: ok(绿) / warn(橙) / err(红)。截图成功走 ok —— 用户要的就是
        "截成了"这件事在画面旁边直接看到, 而不是去最底下的状态栏找一行小字。
        """
        color = {"ok": T.OK, "warn": T.WARN, "err": T.DANGER}.get(kind, T.OK)
        bg = {"ok": "#EAF7EF", "warn": "#FDF6E8", "err": "#FDECEC"}.get(kind, "#EAF7EF")
        bd = {"ok": "#B7E3C6", "warn": "#F0D9A8", "err": "#F3C0C0"}.get(kind, "#B7E3C6")
        self.toast.setStyleSheet(
            f"QLabel#toast {{ background: {bg}; border: 1px solid {bd};"
            f" border-radius: 13px; color: {color}; font-size: 12px;"
            f" font-weight: 600; padding: 6px 16px; }}")
        self.toast.setText(text)
        self.toast.adjustSize()
        self.toast.setVisible(True)      # 摆位置前先显示: _place_toast 要量可见性
        self._place_toast()
        self.toast.raise_()
        self._toast_timer.start()

    def _place_toast(self):
        """摆在预览窗上方居中(挂在预览窗里, 不挡进度条)。"""
        try:
            if not self.toast.isVisible():
                return
            r = QRect(self.preview.mapTo(self, QPoint(0, 0)), self.preview.size())
            if r.width() <= 80 or r.height() <= 40:
                return
            x = r.left() + (r.width() - self.toast.width()) // 2
            y = r.top() + 14
            self.toast.move(max(r.left() + 8, x), y)
        except Exception:
            pass

    def set_scrub_range(self, duration: float):
        """只改进度条的范围, 不动手柄位置(播放中总长变了会用到)。"""
        self.scrubber.set_range(duration)

    def set_scrub_pos(self, t: float):
        self.scrubber.set_pos(t)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_scrubber()
        self._place_toast()
        # v163: 画面按比例收紧(进度条要贴着画面下沿, 不是贴着控件下沿)。
        # 画面本身由 PreviewView 自己重缩, 这里不用再缩一遍。
        self._fit_preview()

    def showEvent(self, event):
        super().showEvent(event)
        # 第一次显示时布局还没算完(preview.width() 还是 0), 等这一轮事件走完再量
        QTimer.singleShot(0, self._fit_preview)

    def eventFilter(self, obj, event):
        """上部区尺寸变了(拖分隔条 / setSizes)就重算画面高度。

        ⚠️ 不能只靠 splitterMoved: 代码里 `splitter.setSizes()` 不会发那个信号,
        上部被压到比"画面+进度条"还矮时画面就会溢出去和进度条叠在一起。
        """
        if obj is self._upper and event.type() == event.Type.Resize:
            QTimer.singleShot(0, self._fit_preview)
        elif (obj is getattr(self, "_preview_col", None)
              and event.type() == event.Type.Resize):
            # v188: 只认「宽度」变化。高度变化是 _fit_preview 自己 setSizes 造出来的,
            # 连着它也重算就会绕圈; 宽度变了才说明画面比例该重算。
            new_w = self._preview_col.width()
            old_w = getattr(self, "_fit_col_w", None)
            self._fit_col_w = new_w
            if old_w is not None and new_w != old_w:
                QTimer.singleShot(0, self._fit_preview)
        return super().eventFilter(obj, event)

    # ---------------------------------------------------------------- 刷新
    def _sync_buttons(self):
        """分割/删除只在选中片段时可点; 播放要时间线上有东西。"""
        has = self.selected_clip() is not None
        n = len(self.timeline_view.selected_clips())
        self.split_btn.setEnabled(has)
        self.del_btn.setEnabled(has)
        self.split_btn.setToolTip("在裁剪线处切开选中的片段（S）" if has
                                  else "先选中一个片段，再把播放头移到要切的位置")
        self.del_btn.setToolTip(
            (f"删除选中的 {n} 个片段（Delete）" if n > 1 else "删除选中的片段（Delete）")
            if has else "先选中一个片段（按住 Ctrl 可选多个）")
        tl = self.timeline_view.timeline
        self.fit_btn.setEnabled(bool(tl and tl.clips))

    def _on_clip_selected(self, clip):
        # 选中变化由主窗口统一处理预览与属性面板, 这里只管按钮
        self._sync_buttons()
        # v187: 音量组跟着选区亮/灰, 数字换成这一段自己的音量
        try:
            self.sync_volume_control()
        except Exception:
            pass

    # ---------------------------------------------------------------- 无损状态
    def set_loss_state(self, plan):
        """把判定结果贴到界面上: 胶囊 + 导出按钮(v162 起不再有顶部红条)。

        不能无损时胶囊自己变红并写明原因(悬停有完整说明), 导出时再正式警告一次 ——
        剪辑页顶部不再挂一条重复的红条, 那 34px 留给画面。
        """
        if plan is None:
            self.loss_chip.setText("—")
            self.loss_chip.setToolTip("")
            self.export_btn.setEnabled(False)
            return
        ok = bool(plan.ok) and plan.exportable
        warn_only = ok and bool(plan.warn_reasons)
        if not plan.exportable:
            text, color = "× " + plan.summary(), T.DANGER
        elif ok:
            # ⚠️ 别写 "✓": 雅黑没有 U+2713 的字形, 胶囊里会渲染成一个空方块。
            text = ("可以无损" if not warn_only
                    else f"! {len(plan.warn_reasons)} 处要注意")
            color = T.OK if not warn_only else T.WARN
        else:
            text, color = "× 不建议剪辑", T.DANGER
        self.loss_chip.setText(text)
        # ⚠️ QSS 不认 #RRGGBBAA 这种 8 位写法(会被当无效值丢掉), 半透明底必须用 rgba()
        self.loss_chip.setStyleSheet(
            f"QPushButton {{ background: {_rgba(color, 0.12)}; color: {color};"
            f" border: 1px solid {color}; border-radius: 13px;"
            f" padding: 2px 12px; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {_rgba(color, 0.22)}; }}")

        # 第一处必须重编码的原因直接写进胶囊的悬停说明, 想看全部就点它。
        fatal = plan.fatal_reasons
        if fatal:
            more = f"\n另有 {len(fatal) - 1} 处，点这里看全部原因" if len(fatal) > 1 else ""
            self.loss_chip.setToolTip(
                "不建议剪辑 —— " + fatal[0].text + more + "\n点这里看全部原因")
        else:
            self.loss_chip.setToolTip("点这里看无损判定的详细清单")

        self.export_btn.setEnabled(bool(plan.exportable))
        self.export_btn.setToolTip(
            "无需重编码，无损拼接，速度快" if ok else
            "导出（这次要重新编码，会先跟你确认）")

    def undo_hint(self, index: int, total: int):
        """撤销栈提示(挂成时间读数的悬停说明, 不额外占地方)。"""
        if total <= 0:
            self.time_label.setToolTip("Ctrl+Z 撤销 / Ctrl+Y 重做")
            return
        self.time_label.setToolTip(
            f"撤销栈：第 {max(1, index + 1)} / {total} 步（Ctrl+Z 撤销，Ctrl+Y 重做）")

    # ---------------------------------------------------------------- 工具按钮
    def _sync_tool_buttons(self, has_video: bool = False, fps: float = 0.0,
                           speed: float = 1.0, has_audio: bool = False,
                           fps_enabled: bool | None = None):
        """这几个必须选中一个视频片段才有意义 —— 没选中就灰着。

        灰着但**不隐藏**: 位置固定, 用户知道"先选个片段就能用",
        比按钮时有时无更好找。

        fps / speed: 当前片段的帧率和倍速, 写成「45/1.3」(v165)。
        音频没有帧率, 只写倍速「1.3」(v167); 读不到就退回「帧率/倍速」。
        v188: 没选中、但裁剪线上恰好一段时也可以只亮「帧率/倍速」并显示
        那一段的数字 —— 用 fps_enabled 单独放开, 别的按钮仍旧灰着。
        """
        for b in (self.kf_btn, self.decimate_btn, self.detect_btn):
            b.setEnabled(bool(has_video))
        # 「帧率/倍速」对音频也开放: 那一侧只是改速度(导出时重编码)(v167)
        if fps_enabled is None:
            fps_enabled = bool(has_video or has_audio)
        self.fps_btn.setEnabled(bool(fps_enabled))
        self.set_fps_label(fps, speed, audio=bool(has_audio))

    def set_fps_label(self, fps: float, speed: float = 1.0, audio: bool = False):
        """把当前帧率 / 倍速写到按钮上: 视频 "45/1.3", 音频只有倍速 "1.3"。"""
        try:
            fps = float(fps or 0.0)
        except Exception:
            fps = 0.0
        try:
            speed = float(speed or 1.0)
        except Exception:
            speed = 1.0
        if speed <= 0:
            speed = 1.0
        if audio:
            self.fps_btn.setText("%.1f" % speed)
            self.fps_btn.setToolTip("音频倍速（导出时重新编码，音调不变）")
        elif 0 < fps < 480:
            # v185: 前面这个数字是「等效帧率」= 源帧率 × 倍速 —— 倍速一改,
            # 帧率跟着变, 正好是用户心智里的「改帧率就是变速」。格式仍是 "45/1.3"。
            eff = fps * speed
            f = ("%.1f" % eff).rstrip("0").rstrip(".")
            self.fps_btn.setText("%s/%.1f" % (f, speed))
            self.fps_btn.setToolTip("等效帧率/倍速（改帧率就是变速，导出时才落地）")
        else:
            self.fps_btn.setText("帧率/倍速")
            self.fps_btn.setToolTip("等效帧率/倍速（改帧率就是变速，导出时才落地）")

    # ---------------------------------------------------------------- 截图设置
    def _shot_tip(self) -> str:
        """截图按钮的提示: 「截图(主键)」—— 键位跟着实际设置走, 别写死 C。"""
        key = ""
        try:
            from .edit_shortcuts import as_slots, load_bindings
            slots = as_slots((load_bindings() or {}).get("capture_frame"))
            key = next((s for s in slots if s), "")
        except Exception:
            key = ""
        return f"截图({key})" if key else "截图"

    def refresh_shortcut_tips(self):
        """快捷键被改过之后, 依赖键位的提示要跟着换字。"""
        try:
            self.shot_btn.setToolTip(self._shot_tip())
        except Exception:
            pass

    def _load_shot_prefs(self):
        fmt, q = "png", 90
        try:
            from ..merge import settings
            v = str(settings.load_shot_format() or "").lower()
            if v in ("png", "jpg", "webp"):
                fmt = v
            q = int(settings.load_shot_quality())
        except Exception:
            pass
        self._shot_quality = self._nearest_quality(q)   # 真实质量(和界面上的档位分开存)
        i = self.shot_fmt.findData(fmt)
        self.shot_fmt.setCurrentIndex(max(0, i))
        self._select_quality(self._shot_quality)

    @staticmethod
    def _nearest_quality(q: int) -> int:
        """设置里可能是任意 0~100 的旧值, 收拢到最接近的档位。"""
        steps = (100, 90, 80, 70, 60, 50)
        try:
            q = int(q)
        except Exception:
            q = 90
        return min(steps, key=lambda s: abs(s - q))

    def _select_quality(self, q: int):
        i = self.shot_q.findData(self._nearest_quality(q))
        self.shot_q.blockSignals(True)
        self.shot_q.setCurrentIndex(max(0, i))
        self.shot_q.blockSignals(False)

    def _sync_shot_quality_visible(self):
        """PNG 是无损格式, 没有质量可调 —— **整个质量框不显示**。

        之前是"灰着的 90", 看着像坏了; 换成「—」也还是多一个框。
        既然调不了, 就别占地方: 选 PNG 时它直接消失, 选 JPEG/WEBP 再出来。
        """
        png = self.capture_format() == "png"
        self.shot_q.setVisible(not png)
        if not png:
            self._select_quality(self._shot_quality)

    def _on_shot_changed(self, *_a):
        """格式变了: PNG 就把质量框收起来, 顺手存盘。"""
        self._sync_shot_quality_visible()
        self._save_shot_prefs()

    def _on_shot_qty_changed(self, *_a):
        """用户真改了质量的档位。"""
        v = self.shot_q.currentData()
        if v is None:
            return
        self._shot_quality = int(v)
        self._save_shot_prefs()

    def _cycle_shot_scope(self):
        """点一下按钮 = 换下一个范围(当前 → 选中 → 所有 → 当前)。"""
        order = [v for _label, v in SHOT_SCOPES]
        try:
            i = order.index(self._scope)
        except ValueError:
            i = 0
        self.set_capture_scope(order[(i + 1) % len(order)])

    def set_capture_scope(self, scope: str):
        """切到某个截图范围(按钮文字和状态一起换, 并通知主窗口)。"""
        scope = str(scope or "current")
        if scope not in [v for _label, v in SHOT_SCOPES]:
            scope = "current"
        self._scope = scope
        self.shot_scope.setText(dict((v, lb) for lb, v in SHOT_SCOPES).get(scope, "当前"))
        self.scopeChanged.emit(scope)

    def _save_shot_prefs(self):
        try:
            from ..merge import settings
            settings.save_shot_format(self.capture_format())
            settings.save_shot_quality(self.capture_quality())
        except Exception:
            pass

    def capture_format(self) -> str:
        """png / jpg / webp。"""
        v = self.shot_fmt.currentData()
        return str(v or "png")

    def capture_quality(self) -> int:
        """真实质量。PNG 时框是隐藏的, 但记住用户上次选的档位。"""
        q = getattr(self, "_shot_quality", None)
        if q is None:
            v = self.shot_q.currentData()
            return int(v) if v is not None else 90
        return int(q)

    def capture_scope(self) -> str:
        """current / selected / all —— 截一张, 还是按关键帧批量截。"""
        return str(getattr(self, "_scope", "") or "current")

    def _on_playhead_changed(self, t):
        self._refresh_time_label()
        # 裁剪线动了: 主窗口去抓这一秒的画面(不播放也要能看到)
        self.playheadMoved.emit(float(t or 0.0))
        # 裁剪线是否正好压在关键帧上 → 截图按钮变蓝(v177)
        self.refresh_shot_keyframe_state()

    # ---------------------------------------------------------------- 截图按钮(v177)
    def refresh_shot_keyframe_state(self):
        """裁剪线是否**精确**落在关键帧上: 是就让「截图」按钮变蓝。

        v186: 换算统一走 Clip.timeline_keyframe_times()(含裁切/偏移/倍速), 和
        「上/下一个关键帧」以及轨道上的红色刻度用同一套坐标, 不再各算各的。
        容差从最宽 0.12s(约 3 帧)收到半帧以内 —— 以前差两三帧也变蓝,
        看着像落上了, 真下刀却不是无损点。

        关键帧没加载出来时任何裁剪线都不算"在关键帧上"。
        """
        tl = self.timeline_view.timeline
        on = False
        if tl is not None and getattr(tl, "clips", None):
            t = float(getattr(tl, "playhead", 0.0) or 0.0)
            try:
                px = float(self.timeline_view._snap_tol())
            except Exception:
                px = 0.1
            tol = max(0.004, min(px, KF_HIT_TOL))
            for c in tl.clips:
                if getattr(c, "kind", None) != "video":
                    continue
                try:
                    if t < c.start - tol or t > c.end + tol:
                        continue
                    ks = c.timeline_keyframe_times()
                except Exception:
                    continue
                if ks and min(abs(k - t) for k in ks) <= tol:
                    on = True
                    break
        self._set_shot_blue(on)

    def _set_shot_blue(self, on: bool):
        if on:
            self.shot_btn.setStyleSheet(
                "QPushButton#shotBtn{background:%s;color:#fff;border:none;"
                "border-radius:6px;}" % T.ACCENT)
            try:
                self.shot_btn.setIcon(make_icon("camera", "#FFFFFF", 18))
            except Exception:
                pass
        else:
            self.shot_btn.setStyleSheet("")
            try:
                self.shot_btn.setIcon(make_icon("camera", T.TEXT_SUB, 18))
            except Exception:
                pass

    def _on_order_changed(self):
        # 时间线内容变了(加/删/移动片段) → 播放、全览的可用状态要跟着变,
        # 不能等用户点一下片段才刷新, 否则第一次加完片按钮还是灰的。
        self._sync_buttons()
        self._refresh_time_label()
        # 片段挪了 → 它在时间线上的关键帧位置也变了, 截图按钮的蓝底要重判
        # (裁剪线没动, 所以 playheadChanged 不会触发, 这里补一次, v179)。
        self.refresh_shot_keyframe_state()

    def _refresh_time_label(self):
        tl = self.timeline_view.timeline
        total = tl.total_duration() if tl else 0.0
        cur = tl.playhead if tl else 0.0
        txt = f"{fmt_time(cur)} / {fmt_time(total)}"
        # 60Hz 推进时这个函数每 16ms 调一次, 但读数只到 0.1 秒 —— 没变就别 setText
        # (重复 setText 会连带触发布局/重绘, 白烧 CPU, v156)
        if txt != getattr(self, "_time_txt", None):
            self._time_txt = txt
            self.time_label.setText(txt)
