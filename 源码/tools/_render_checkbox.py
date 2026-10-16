# -*- coding: utf-8 -*-
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication, QWidget, QHBoxLayout
from PySide6.QtCore import Qt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "app.ui.theme" in sys.modules:
    del sys.modules["app.ui.theme"]
from app.ui import theme
from app.ui.widgets import CheckCheckBox

app = QApplication(sys.argv)
theme.apply(app)

w = QWidget()
lay = QHBoxLayout(w)
cb = CheckCheckBox("联动")
cb.setChecked(True)
lay.addWidget(cb)
cb2 = CheckCheckBox("仅可写")
cb2.setChecked(True)
lay.addWidget(cb2)
# 也加一个未勾选的对比
cb3 = CheckCheckBox("未勾选")
lay.addWidget(cb3)
w.setLayout(lay)
w.adjustSize()
w.show()

pix = w.grab()
pix.save(r"C:\software\Photo sorting\源码\tools\_render_checkbox_v3.png", "PNG")

# 放大 indicator
cb.adjustSize()
ind = cb.grab()
ind.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(r"C:\software\Photo sorting\源码\tools\_render_indicator_v3.png", "PNG")

print("saved v3")
