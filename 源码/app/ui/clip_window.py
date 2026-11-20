# -*- coding: utf-8 -*-
"""剪辑页的独立窗口（v128）。

用户要求：「剪辑」做成一个独立的窗口，主界面（排序 / 合并）原封不动留在后面，
两个窗口可以同时存在 —— 所以页里那条「← 返回分组」按钮和「剪辑」标题都删了，
退出剪辑就是关窗。

为什么不用 QDialog + exec()：
- exec() 是模态的，主窗口会被锁住，和"两个窗口同时用"矛盾；
- 关掉就销毁的话，时间线/素材/撤销历史全丢。所以这里是普通顶层窗口，
  **close 只是藏起来**，下次点「剪辑」原样回来。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget


class ClipWindow(QWidget):
    """把 ClipEditor 装进一个独立的顶层窗口。"""

    closeRequested = Signal()      # 用户点了 ×（主窗口负责收尾: 停播放等）
    activated = Signal()           # v199: 窗口获得焦点（主窗口据此切换激活会话）

    def __init__(self, editor: QWidget, parent=None, num: int = 1):
        # ⚠️ parent 必须是 None: 挂主窗口的话它就不是"独立窗口", 会跟着主窗口
        #    一起关、也不会单独出现在任务栏/切换器里。
        super().__init__(None)
        self.setWindowFlag(Qt.Window, True)
        self.num = num                       # v199: 剪辑1 / 剪辑2 … 固定编号
        self.setWindowTitle(f"剪辑{num}" if num else "剪辑")
        self.editor = editor

        # ⚠️ 不发这一段, 窗口就是"黑色主题":
        #   主题里只有 `QMainWindow, QDialog { background: BG }` 给了底色, 而普通
        #   QWidget(本窗口)只匹配到 `QWidget { background: transparent }` → 底是透
        #   明的 → 直接透出下面, 在深色桌面上整窗发黑。
        #   两件事缺一不可: WA_StyledBackground 让 QWidget **真的去画** QSS 背景
        #   (普通 QWidget 默认不画), styleSheet 给要画的颜色。
        self.setObjectName("clipWindow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        try:
            from .theme import BG
        except Exception:
            BG = "#F4F6F9"
        self.setStyleSheet(f"QWidget#clipWindow {{ background: {BG}; }}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(editor)

        self._placed = False       # 首次显示才摆位置(之后用户自己挪过就别管)
        self._allow_close = False  # True 时 close() 才真的关(程序退出用)

        if parent is not None:
            try:
                self.setWindowIcon(parent.windowIcon())
            except Exception:
                pass

    # ------------------------------------------------------------ 显示
    def show_for(self, parent: QWidget | None = None):
        """首次显示时摆在主窗口旁边(比主窗口略小), 之后保持用户挪的位置。"""
        if not self._placed:
            self._placed = True
            self._place_near(parent)
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _place_near(self, parent: QWidget | None):
        from PySide6.QtWidgets import QApplication
        try:
            scr = (parent.screen() if parent is not None else None) \
                or QApplication.primaryScreen()
            avail = scr.availableGeometry() if scr is not None else None
        except Exception:
            avail = None
        if avail is None:
            self.resize(1440, 900)
            return
        pg = parent.frameGeometry() if parent is not None else avail
        # 比主窗口略小一点、往右下错开, 让人一眼看出"这是另一个窗口"，主界面还露着
        w = max(1000, min(int(pg.width() * 0.88), avail.width() - 80))
        h = max(680, min(int(pg.height() * 0.90), avail.height() - 80))
        x = pg.left() + max(0, (pg.width() - w) // 2) + 40
        y = pg.top() + max(0, (pg.height() - h) // 2) + 30
        x = max(avail.left(), min(x, avail.right() - w))
        y = max(avail.top(), min(y, avail.bottom() - h))
        self.setGeometry(x, y, w, h)

    # ------------------------------------------------------------ 关闭
    def shutdown(self):
        """真的关掉(主程序退出时用)。平时 close() 只是藏起来。"""
        self._allow_close = True
        self.close()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        try:
            self.activated.emit()
        except Exception:
            pass

    def closeEvent(self, event):
        """点 ×: 通知主窗口(它会按会话做二次确认 + 彻底清理)。

        v199 起 × 不再"只是藏起来": 每个窗口是一套独立会话, 关掉就销毁现场。
        是否真关由主窗口确认后调用 shutdown(), 这里先 ignore 住。
        """
        if self._allow_close:
            event.accept()
            return
        event.ignore()
        try:
            self.closeRequested.emit()
        except Exception:
            pass
