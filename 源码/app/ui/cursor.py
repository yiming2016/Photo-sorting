# -*- coding: utf-8 -*-
"""v216: 全局「能点的地方一律抓手」光标。

以前是每个按钮自己写一句 `setCursor(Qt.PointingHandCursor)`, 结果是**漏一个就
还是箭头** —— 顶栏胶囊、年月日开关、组标题开关、右栏那些行、菜单项、列表格子,
到处都是漏网的; 每加一个控件还得记着补一句。

这里反过来做: 在 QApplication 上挂一层过滤器, 按**控件类型 / 有没有自己实现
鼠标按下松开**判定"它能不能点", 能点就把光标设成小手; 控件被禁用就还回箭头。
一次性装好, 以后新增控件不用再管。

判定口径(约定):
  * 按钮/下拉/菜单/菜单栏/标签页           → 手
  * 列表/表格/树(**内容区 viewport**)      → 手(格子能点能拖)
  * 自己重写了 mousePress/Release/DblClick  → 手(自绘胶囊、可点标签、时间线…)
  * 带可点链接的 QLabel                    → 手
  * 输入框(含数字框)/滑杆/滚动条/分隔条     → **不动**(人家是 I 形 / 拖拽箭头)
  * 控件自己设过光标(WA_SetCursor)          → **不动**(优先尊重控件自身语义)
  * 控件可用 `setProperty("handCursor", False/True)` 显式豁免 / 显式指定
    (例: 照片网格 GroupView 自己按区域判光标, 就设成 False)

⚠️ 只在"控件首次上屏(Polish/Show) / 由禁用变可用 / 窗口显示"这几个时机设光标, 之后不再
   插手 —— 拖动时间线、clip 列表悬停这些会**动态改光标**的地方, 它们后来设的值照样生效。
⚠️ 列表只染 viewport, 不染整个 QAbstractScrollArea, 否则滚动条也变成小手。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSlider,
                               QAbstractSpinBox, QApplication, QComboBox, QLabel,
                               QLineEdit, QMenu, QMenuBar, QPlainTextEdit,
                               QScrollBar, QSplitterHandle, QTabBar, QTextEdit,
                               QWidget)

HAND = Qt.PointingHandCursor

# 本模块亲手设过的标记(禁用 / 重新启用时用来判断"这光标是不是我设的")
_PROP = "_psHandCursor"

# 自带光标语义, 抢过来反而难用
_NO_HAND = (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox,
            QAbstractSlider, QScrollBar, QSplitterHandle)

# 天生就是"点"的控件
_ALWAYS_HAND = (QAbstractButton, QComboBox, QTabBar, QMenuBar, QMenu)


def _owning_class(cls, name: str):
    for k in cls.__mro__:
        if name in k.__dict__:
            return k
    return None


def _owns_mouse(cls) -> bool:
    """类自己实现了鼠标按下/松开/双击 —— 说明它有点击交互。"""
    for name in ("mousePressEvent", "mouseReleaseEvent", "mouseDoubleClickEvent"):
        owner = _owning_class(cls, name)
        if owner is not None and owner is not QWidget:
            return True
    return False


def is_clickable(w) -> bool:
    if not isinstance(w, QWidget):
        return False
    # 显式豁免 / 显式指定, 优先级最高
    flag = w.property("handCursor")
    if flag is False:
        return False
    if flag is True:
        return True
    if isinstance(w, _NO_HAND):
        return False
    if isinstance(w, _ALWAYS_HAND):
        return True
    if isinstance(w, QAbstractItemView):
        return True
    # 列表/表格的内容区: 格子能点能拖
    p = w.parentWidget()
    if isinstance(p, QAbstractItemView) and p.viewport() is w:
        return p.property("handCursor") is not False
    if isinstance(w, QLabel):
        return bool(w.textInteractionFlags() & Qt.LinksAccessibleByMouse)
    return _owns_mouse(type(w))


def _target(w):
    """返回真正该设光标的控件(None = 不设)。"""
    try:
        if not w.isWidgetType() or not w.isEnabled() or not is_clickable(w):
            return None
        if isinstance(w, QAbstractItemView):
            if w.property("handCursor") is False:
                return None
            vp = w.viewport()
            return vp if vp is not None else None
        return w
    except RuntimeError:                        # C++ 对象已销毁
        return None


def apply_hand(w):
    """给单个控件(及其 viewport)设小手; 控件自己的光标优先。"""
    t = _target(w)
    if t is None:
        return False
    try:
        if t.testAttribute(Qt.WA_SetCursor):
            return False                        # 别人(或控件自己)设过, 不动
        t.setCursor(HAND)
        t.setProperty(_PROP, True)
        return True
    except RuntimeError:
        return False


def clear_hand(w):
    try:
        for cand in (w, w.viewport() if isinstance(w, QAbstractItemView) else None):
            if cand is not None and cand.property(_PROP):
                cand.setProperty(_PROP, False)
                cand.unsetCursor()
    except RuntimeError:
        pass


def sweep(root):
    """整棵子树补一遍(用于窗口首次显示 / 安装时的存量控件)。"""
    if not isinstance(root, QWidget):
        return
    apply_hand(root)
    for w in root.findChildren(QWidget):
        apply_hand(w)


class HandCursorWatcher(QObject):
    """QApplication 级过滤器: 控件首次上屏 / 启用状态变化时补光标。

    ⚠️ 为什么不用 `ChildAdded`: 事件在 C++ 构造过程中就发出来了, 这时 PySide 的
       Python 包装**还没升成子类**(拿到手的是个 QWidget), 认不出 QPushButton,
       判定必然落空(踩过一次: 除了自己写了 setCursor 的按钮全是箭头)。
       `Polish` / `Show` 是控件上屏前才发的, 那时类型已经是真子类, 更可靠。
    """

    def eventFilter(self, obj, ev):
        et = ev.type()
        if et == QEvent.Polish:
            if isinstance(obj, QWidget):
                apply_hand(obj)
        elif et == QEvent.Show:
            if isinstance(obj, QWidget):
                # 顶层窗口: 整棵补一遍(无父的弹窗/菜单/运行时新建的页面都在这里兜住)
                sweep(obj) if obj.isWindow() else apply_hand(obj)
        elif et == QEvent.EnabledChange:
            if isinstance(obj, QWidget):
                if obj.isEnabled():
                    apply_hand(obj)
                else:
                    clear_hand(obj)             # 点不动的按钮不该是手
        return False


_INSTALLED = None


def install(app=None):
    """装一次就够(重复调用无副作用)。main() 与 MainWindow 都会调。"""
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED
    app = app or QApplication.instance()
    if app is None:
        return None
    _INSTALLED = HandCursorWatcher(app)
    app.installEventFilter(_INSTALLED)
    for w in app.topLevelWidgets():             # 存量(装之前就建好的)
        sweep(w)
    return _INSTALLED
