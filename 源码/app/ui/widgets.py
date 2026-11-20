# -*- coding: utf-8 -*-
"""自绘小部件: 胶囊开关 / 勾选框 / 就地编辑的点外部取消。"""
from __future__ import annotations

import os

from PySide6.QtCore import (QEvent, QObject, QRect, QRectF, QSize, Qt, Signal)
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen)
from PySide6.QtWidgets import (QAbstractButton, QApplication, QCheckBox, QLabel,
                               QRubberBand, QSizePolicy, QStyle, QStyleOptionButton,
                               QWidget, QWidgetAction)

from . import theme as T


def split_name_stem(current: str, path: str) -> tuple[str, str]:
    """v194: 把显示名拆成「可编辑主名 + 只读后缀」。

    后缀一律取**磁盘文件的真实扩展名**(用户定的口径), 不从显示名猜 ——
    显示名在旧版本里可能被整名改过、后缀甚至被删掉了, 只有 path 是可信的。
    返回 (stem, ext); ext 含点号(如 ".mp4"), 没有扩展名时为空串。
    """
    ext = os.path.splitext(path or "")[1]
    stem = str(current or "")
    if ext and stem.lower().endswith(ext.lower()):
        stem = stem[:-len(ext)]
    return stem, ext


class OutsideClickWatcher(QObject):
    """v193: 就地改名的输入框期间，「点输入框外面」一律放弃修改。

    ⚠️ 只靠 `focusOutEvent` 不够: QListWidget 的 viewport / 自绘时间线默认不接
       焦点, 点空白处时 QLineEdit 可能压根不失焦, 于是「点走」永远收不到信号。
       这里在 QApplication 上临时挂一层过滤, 按「按下的控件是不是输入框自己
       或它的子控件」判定, 与焦点策略无关; 输入框一关就摘掉, 不留全局钩子。
    """

    outsideClicked = Signal()

    def __init__(self, edit: QWidget, parent=None):
        super().__init__(edit)          # 生命周期跟随输入框
        self._edit = edit
        self._armed = False
        self._extra: list[QWidget] = []      # v194: 后缀标签这类「看着属于输入框」的只读控件

    def add_inside(self, w: QWidget | None):
        """v194: 把只读后缀标签也算作输入框内部, 点它不该放弃编辑。"""
        if w is not None and w not in self._extra:
            self._extra.append(w)

    def start(self):
        if self._armed:
            return
        app = QApplication.instance()
        if app is None:
            return
        self._armed = True
        app.installEventFilter(self)

    def stop(self):
        if not self._armed:
            return
        self._armed = False
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.MouseButtonPress:
            w = obj if isinstance(obj, QWidget) else None
            ed = self._edit
            if ed is not None and w is not None:
                inside = False
                while w is not None:
                    if w is ed or w in self._extra:
                        inside = True
                        break
                    w = w.parentWidget()
                if not inside:
                    self.outsideClicked.emit()
                    self.stop()
        return False                    # 事件照常发给原控件, 只做旁听


class Marquee(QRubberBand):
    """自绘的框选矩形(鼠标拉框)。

    为什么不用系统自带的那一个: 全局 QSS 里有 `QWidget { background: transparent; }`,
    Qt 的橡皮筋是按 QWidget 参与样式计算的, 于是被画成透明 —— 拉框时能看到"选中了",
    却看不到框本身。这里直接接管 paintEvent, 不依赖任何风格, 保证一定可见。
    """

    def __init__(self, parent=None):
        super().__init__(QRubberBand.Rectangle, parent)
        # 不能吃掉鼠标事件, 否则下面的视图收不到 move/release
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event):
        p = QPainter(self)
        r = self.rect().adjusted(0, 0, -1, -1)
        # ⚠️ "#RRGGBBAA" 不是 Qt 认的写法(8 位会被当成 AARRGGBB → 框会变绿),
        #    要半透明就老老实实 setAlpha
        fill = QColor(T.ACCENT)
        fill.setAlpha(43)
        p.fillRect(r, fill)                             # 半透明蓝底
        p.setPen(QPen(QColor(T.ACCENT), 1))             # 蓝色实线边框
        p.setBrush(Qt.NoBrush)
        p.drawRect(r)
        p.end()


class SwitchButton(QAbstractButton):
    """胶囊开关: 开=蓝底白字, 关=灰底灰字, 禁用=更淡。"""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._text = text
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(26)

    def setText(self, text: str):
        self._text = text
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        return self._text

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(QFont())
        return QSize(fm.horizontalAdvance(self._text) + 26, 26)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(1, 3, -1, -3)
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), rect.height() / 2, rect.height() / 2)
        on = self.isChecked()
        enabled = self.isEnabled()
        if not enabled:
            painter.fillPath(path, QBrush(QColor("#F7F8FA")))
            painter.setPen(QPen(QColor(T.BORDER), 1))
            color = QColor(T.TEXT_WEAK)
        elif on:
            painter.fillPath(path, QBrush(QColor(T.ACCENT)))
            painter.setPen(QPen(QColor(T.ACCENT), 1))
            color = QColor("#FFFFFF")
        else:
            painter.fillPath(path, QBrush(QColor("#F2F4F7")))
            painter.setPen(QPen(QColor(T.BORDER_STRONG), 1))
            color = QColor(T.TEXT_SUB)
        painter.drawPath(path)

        font = QFont()
        font.setPointSize(9)
        if on and enabled:
            font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(self.rect(), Qt.AlignCenter, self._text)
        painter.end()


def set_accent(widget, on: bool = True):
    widget.setProperty("accent", "true" if on else "false")
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class CheckCheckBox(QCheckBox):
    """QCheckBox: 选中时在 indicator 区域覆盖绘制蓝色圆角背景 + 白色对勾,
    解决 Fusion 风格下 QSS image:/color: 对 indicator 勾选标记无效的问题。"""

    def paintEvent(self, event):
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 先让 Fusion 绘制完整 checkbox（文字、间距、未选中状态等）
        self.style().drawControl(QStyle.CE_CheckBox, opt, p, self)
        # 选中时覆盖绘制蓝底+白勾
        if self.isChecked():
            ir = self.style().subElementRect(QStyle.SE_CheckBoxIndicator, opt, self)
            if not ir.isEmpty():
                p.save()
                r = ir.adjusted(1, 1, -1, -1)
                # 蓝色圆角背景
                bg = QPainterPath()
                bg.addRoundedRect(QRectF(r), 3, 3)
                p.fillPath(bg, QBrush(QColor(T.ACCENT)))
                # 白色对勾
                pen = QPen(QColor("#FFFFFF"))
                pen.setWidthF(2.0)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                p.setPen(pen)
                tick = QPainterPath()
                tick.moveTo(r.x() + 3, r.y() + r.height() * 0.55)
                tick.lineTo(r.x() + r.width() * 0.4, r.y() + r.height() * 0.75)
                tick.lineTo(r.x() + r.width() - 3, r.y() + 3)
                p.drawPath(tick)
                p.restore()
        p.end()


class ElidedLabel(QLabel):
    """单行文字: 放得下才画,**不把窗口最小宽顶大**。

    v211: 顶栏右侧状态文字用它。QLabel 默认按整串文字报最小宽, 一句
    「已选中 1 张 / 共 1 张 · 没有待写入的改动」就能把窗口最小宽顶到 1742px,
    连带把顶栏的统一参数条挤出屏幕。这里最小宽恒 0, 富余空间照吃。
    v214: 宽度不够时不再画成"已…", 而是整条不画(详见 paintEvent)。
    """

    def __init__(self, text: str = "", parent=None,
                 align=Qt.AlignRight | Qt.AlignVCenter):
        super().__init__(text, parent)
        self._align = align
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event):
        p = QPainter(self)
        fm = QFontMetrics(self.font())
        p.setPen(self.palette().color(self.foregroundRole()))
        txt = self.text()
        # v214: 放不下就整条不画 —— 只剩一两个字 + "…"（如统一页顶栏被参数条挤满时
        # 出现"已…"）比干脆留白更像出了故障。宁可整条不显示, 也不显示半截。
        if (not txt or self.width() < 24
                or fm.horizontalAdvance(txt) > self.width()):
            p.end()
            return
        p.drawText(self.rect(), self._align, txt)
        p.end()


class BoldPartMenuAction(QWidgetAction):
    """菜单项里只把某几个字加粗(QAction 只认纯文本, 局部加粗只能塞富文本标签)。

    用法: BoldPartMenuAction(menu, "3个发送到", "剪辑")
    标签设为"鼠标穿透", 点击/悬停仍由 QMenu 处理, triggered 正常发;
    悬停时手动刷一层浅蓝底, 和其它项反馈一致。
    """

    def __init__(self, menu, prefix: str, bold_part: str, tip: str = ""):
        super().__init__(menu)
        self._lab = QLabel()
        self._lab.setTextFormat(Qt.RichText)
        self._lab.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._lab.setContentsMargins(14, 5, 26, 5)
        self._lab.setStyleSheet("background: transparent; border-radius: 6px;")
        self._lab.setText(
            f'<span style="font-size:13px; color:{T.TEXT};">{prefix}'
            f'<b>{bold_part}</b></span>')
        # 纯文本给 QMenu 做宽度计算/无障碍用; 显示的是上面那个富文本标签
        self.setText(f"{prefix}{bold_part}")
        if tip:
            self.setToolTip(tip)
        self.setDefaultWidget(self._lab)
        menu.hovered.connect(self._on_hovered)

    def _on_hovered(self, act):
        bg = T.ACCENT_SOFT if act is self else "transparent"
        self._lab.setStyleSheet(f"background: {bg}; border-radius: 6px;")


class DangerMenuAction(QWidgetAction):
    """危险操作菜单项: 文字用红色(QAction 本身设不了颜色, 只能塞一个富文本标签)。

    标签设为"鼠标穿透", 点击/悬停仍由 QMenu 自己处理, 所以 triggered 正常发;
    悬停时手动给标签刷一层浅红底, 保持和其它项一致的反馈。
    """

    def __init__(self, menu, text: str):
        super().__init__(menu)
        self._menu = menu
        self._text = text
        self._lab = QLabel(text)
        self._lab.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._lab.setContentsMargins(14, 5, 26, 5)
        self._lab.setStyleSheet(f"color: {T.DANGER}; font-size: 13px;"
                                " background: transparent; border-radius: 6px;")
        self.setDefaultWidget(self._lab)
        menu.hovered.connect(self._on_hovered)

    def _on_hovered(self, act):
        on = act is self
        bg = T.DANGER_SOFT if on else "transparent"
        self._lab.setStyleSheet(f"color: {T.DANGER}; font-size: 13px;"
                                f" background: {bg}; border-radius: 6px;")

# ------------------------------------------------------------------ 下拉弹层(v215)
def prepare_combo_popup(combo):
    """让下拉弹层只保留"一层圆角底"。

    Qt 的弹层容器是 QComboBoxPrivateContainer(一个 QFrame): 它自己在整个窗口矩形里
    铺一块**直角**底色, 我们再靠 QSS 给里面的 QAbstractItemView 画圆角边框 —— 结果
    四角露出直角色, 看着就是"方底 + 圆角"两层背景叠在一起。
    这里把容器设成透明/无系统背景, 并去掉系统投影(投影也是按方窗口画的), 只剩 view
    自己画的那层圆角面板。⚠️ 只能在弹层第一次显示**之前**调用(设置属性会重建窗口)。
    """
    try:
        view = combo.view()
        pop = view.window()
    except Exception:
        return None
    if pop is None or pop is combo:
        return None
    # ⚠️ 只设 WA_TranslucentBackground 不够: 容器还会把自己刷成一块**直角**白底
    #    (那层"方底"就是它), 四角盖住圆角面板。实测 app 级 QSS 到不了这个弹层容器
    #    (它是独立顶层窗口), 必须挂到它自己身上 —— 用带 #id 的选择器, 避免像裸写
    #    `background: transparent` 那样连里面的 QListView 一起洗掉(列表会整个透明)。
    pop.setObjectName("comboPopup")
    pop.setStyleSheet("QFrame#comboPopup { background: transparent; border: none; }")
    pop.setAttribute(Qt.WA_TranslucentBackground, True)
    pop.setAttribute(Qt.WA_NoSystemBackground, True)
    pop.setWindowFlag(Qt.FramelessWindowHint, True)
    pop.setWindowFlag(Qt.NoDropShadowWindowHint, True)
    st = pop.style()
    st.unpolish(pop)
    st.polish(pop)
    return pop


# 弹层比最长选项多留的宽度: 项左右内边距 10+10 + 视图 border/padding 1+1/4+4 + 余量 10
POPUP_EXTRA_W = 40


def fit_popup_width(combo):
    """把弹层宽度撑到"最长选项完整放得下"。

    v213 把控件宽度收到"刚好放下最长文案"后, 弹层宽度沿用控件宽度 → 项的文字区比
    控件文字区还窄(项有 10+10 内边距, 视图再吃掉 4+4 padding 与 1+1 边框), 居中文字
    就被省略号截成 "H.265 Main...10bit"。这里只放宽**弹层**, 控件本身宽度不动。
    """
    try:
        view = combo.view()
    except Exception:
        return
    if view is None or combo.count() == 0:
        return
    fm = combo.fontMetrics()
    need = max(fm.horizontalAdvance(combo.itemText(i)) for i in range(combo.count()))
    view.setMinimumWidth(max(combo.width(), need + POPUP_EXTRA_W))
