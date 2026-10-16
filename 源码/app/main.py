# -*- coding: utf-8 -*-
"""程序入口。"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import __version__
from .resources import app_resource
from .ui.main_window import MainWindow
from .ui.theme import apply as apply_theme


def _consume_noactivate() -> bool:
    """若 exe 同目录存在 .noactivate 标记文件, 删除它并返回 True。

    用于"脱离桌面拉起但不抢焦点"：启动脚本在 exe 旁写好该标记再拉起,
    主窗口据此以 WA_ShowWithoutActivating + WindowDoesNotActivate 显示,
    即正常出现在屏幕上、但**不**抢占用户当前的前台窗口焦点。
    """
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), ".noactivate"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), ".noactivate"))
    for c in candidates:
        if os.path.exists(c):
            try:
                os.remove(c)
            except OSError:
                pass
            return True
    return False


def main() -> int:
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(app_resource("logo.ico")))
    app.setApplicationName("照片排序台")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("PhotoSort")
    apply_theme(app)

    window = MainWindow()
    # 不抢焦点模式: 显示但不激活(不影响用户正在用的窗口)
    if _consume_noactivate():
        window.setAttribute(Qt.WA_ShowWithoutActivating, True)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
