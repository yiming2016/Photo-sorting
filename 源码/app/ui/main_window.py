# -*- coding: utf-8 -*-
"""主窗口: 顶栏开关 / 照片网格 / 右侧信息 / 底部写入条。"""
from __future__ import annotations

import os
import json
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QKeySequence, QPixmap
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QListView, QMainWindow, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QSplitter, QStackedWidget,
                               QVBoxLayout, QWidget)

from . import theme as T
from .. import __build__
from ..resources import app_resource
from .picker_dialog import PickerDialog, _CenterComboArrow
from .dialogs import (ChangePreviewDialog, IntervalDialog, ProgressDialog, RollbackDialog,
                      _CenterAlignDelegate, confirm_destructive)
from .grouplist import GroupView
from .inspector import Inspector
from .widgets import SwitchButton, set_accent
from ..core import fields as F, timeplan, writer
from ..core.exiftool import ExifTool, find_exiftool
from ..core.photo import (PhotoModel, STATUS_DONE, STATUS_NORMAL, STATUS_PENDING,
                          THUMB_FAIL, THUMB_NONE, THUMB_OK)
from ..core.scanner import Scanner, collect_files
from ..core.thumbs import ThumbWorker

SETTINGS_LAST_FOLDER = "lastFolder"

# 主界面六档显示方式: (名称, 模式, 网格宽, 网格高)
# 前四档为图标网格, 后两档为横向列表(单行/双行)。Ctrl+滚轮在六档间步进。
# 主网格"显示"下拉: 最小档就是"小图标"(不含列表/详细信息), 由小到大排列。
MAIN_VIEW_MODES = [
    ("小图标", "sm", 88, 96),
    ("中等图标", "md", 104, 116),
    ("大图标", "lg", 132, 150),
    ("超大图标", "xl", 168, 202),
]

# 安装 Qt 中文翻译: 让非原生选择框(及标准控件)内部的英文变成中文
_zh_tr = None


def _ensure_zh_translator():
    global _zh_tr
    import os, sys
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTranslator, QLibraryInfo
    if _zh_tr is not None:
        return
    app = QApplication.instance()
    if app is None:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    bases = []
    if getattr(sys, "frozen", False):
        mp = getattr(sys, "_MEIPASS", "")
        if mp:
            bases.append(mp)
        exe_dir = os.path.dirname(sys.executable)
        bases.append(exe_dir)
        bases.append(os.path.join(exe_dir, "_internal"))
    bases.append(os.path.join(here, "..", ".."))   # 开发模式: 项目 translations/
    rel_dirs = [
        "translations",
        os.path.join("_internal", "translations"),
        os.path.join("_internal", "PySide6", "translations"),
    ]
    candidates = []
    for b in bases:
        for rd in rel_dirs:
            candidates.append(os.path.join(b, rd, "qtbase_zh_CN.qm"))
    candidates.append(os.path.join(QLibraryInfo.path(QLibraryInfo.TranslationsPath), "qtbase_zh_CN.qm"))
    tr = QTranslator(app)  # 父对象持有, 避免被 GC 后译文失效
    for c in candidates:
        if c and os.path.exists(c) and tr.load(c):
            app.installTranslator(tr)
            _zh_tr = tr
            return


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        _ensure_zh_translator()
        self.setWindowTitle(self._win_title())
        self.resize(1440, 900)
        self.setAcceptDrops(True)

        self.model = PhotoModel(self)
        self.path_index: dict[str, object] = {}
        self.sources: list[dict] = []   # 累加模式下的来源记录: {"type":"folder"/"files", ...}
        # 会话文件: 自动保存最近一次来源集合, 供「恢复上次来源」一键重载
        if getattr(sys, "frozen", False):
            # 打包后 exe 位于 软件\照片排序台\照片排序台.exe, 会话文件放在 软件\ 同级持久目录
            _root = os.path.dirname(os.path.dirname(sys.executable))
        else:
            # 开发模式: 源码\app\ui\main_window.py -> 项目根(需向上 4 级)
            _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        self.session_path = os.path.join(_root, ".photosort_session.photolist")
        self.worker = None
        self.progress_dialog = None
        self._pending_photos_cache: list = []

        self._build_ui()
        self._connect()
        self._refresh_top_status()
        self._apply_main_view_mode()  # 应用默认显示方式(小图标)
        QTimer.singleShot(60, self._check_exiftool)

    # ---------------------------------------------------------------- 界面
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        root.addWidget(self._build_topbar())

        self.stack = QStackedWidget()
        self.view = GroupView()
        self.view.setModel(self.model)
        # 主网格 Ctrl+滚轮缩放 → 切换六档显示方式
        self.view._on_zoom = self._zoom_main_step
        # 顶栏"显示方式"下拉(在 _build_topbar 里创建)的信号在这里连,
        # 因为 _build_topbar 执行时 self.view 还没建好。
        if hasattr(self, "view_mode_combo"):
            self.view_mode_combo.currentIndexChanged.connect(self._apply_main_view_mode)
        self.stack.addWidget(self.view)

        empty = QWidget()
        el = QVBoxLayout(empty)
        el.setAlignment(Qt.AlignCenter)
        el.setSpacing(14)
        icon = QLabel(" DPR")
        icon.setStyleSheet(
            f"font-size: 56px; color: {T.BORDER_STRONG}; "
            f"background: {T.SURFACE_ALT}; border-radius: 28px; "
            f"padding: 12px 18px; margin-bottom: 8px;")
        icon.setAlignment(Qt.AlignCenter)
        el.addWidget(icon, alignment=Qt.AlignCenter)
        t = QLabel("把照片文件夹或文件拖进来，或点上面的「添加」")
        t.setProperty("h2", True)
        el.addWidget(t, alignment=Qt.AlignCenter)
        s = QLabel("排序后按当前顺序分配时间戳，一次性写回 修改时间 / 创建时间 / 拍摄时间")
        s.setProperty("muted", True)
        el.addWidget(s, alignment=Qt.AlignCenter)
        self.stack.addWidget(empty)
        self.empty_page = empty

        self.inspector = Inspector()
        self.inspector.setMinimumWidth(320)
        self.inspector.setMaximumWidth(360)  # 详情栏最多 360px, 元素完整即可, 不无限拉伸

        # 右侧栏 = 详情面板 + 底部三个操作按钮(清除计划 / 自动间隔 / 写入到文件)。
        # 这三个按钮属于右半部分, 所以放进右栏内部(而不是横跨整窗的底栏),
        # 这样它们和左侧网格彻底分开, 并且始终跟详情栏同宽、对齐。
        self.right_col = QWidget()
        self.right_col.setMaximumWidth(360)
        rv = QVBoxLayout(self.right_col)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        rv.addWidget(self.inspector, 1)
        rv.addWidget(self._build_bottombar())

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.stack)
        self.splitter.addWidget(self.right_col)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([1000, 360])
        root.addWidget(self.splitter, 1)

        # 最底部那条状态栏整行已按要求删掉。
        # status_label 只保留对象(永不显示), 这样历史代码里的 setText 不会报错;
        # 需要"信息提示"的地方一律走弹窗。
        self.status_label = QLabel(self)
        self.status_label.setVisible(False)

        self.thumb_worker = ThumbWorker(self)
        self.thumb_worker.thumbReady.connect(self._on_thumb_ready)
        self.thumb_worker.thumbFailed.connect(self._on_thumb_failed)
        self.thumb_worker.start()

    def _build_topbar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(54)
        bar.setObjectName("topbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(10)

        # 左侧 logo（与 exe 图标一致）
        self.logo_lbl = QLabel()
        self.logo_lbl.setObjectName("topLogo")
        _pm = QPixmap(app_resource("logo.ico")).scaled(
            30, 30, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.logo_lbl.setPixmap(_pm)
        self.logo_lbl.setToolTip("照片排序台")
        lay.addWidget(self.logo_lbl)

        self.open_btn = QPushButton("添加")
        self.open_btn.setMinimumWidth(80)
        self.open_btn.setCursor(Qt.PointingHandCursor)
        set_accent(self.open_btn, True)
        self.open_btn.setToolTip("选择文件夹或文件，程序自动判断（也可拖拽）")
        lay.addWidget(self.open_btn)

        self.reload_btn = QPushButton("重新扫描")
        self.reload_btn.setMinimumWidth(80)
        lay.addWidget(self.reload_btn)

        self.clear_list_btn = QPushButton("清空列表")
        self.clear_list_btn.setMinimumWidth(80)
        self.clear_list_btn.setToolTip("移除当前已加载的所有照片/文件（不删除磁盘文件，也不撤销已写入的改动）")
        lay.addWidget(self.clear_list_btn)

        self.recursive = False  # 「含子文件夹」开关：默认不递归；值由“添加”弹窗内的勾选框写入并持久化到本会话

        self.path_label = QLabel("未选择文件夹")
        self.path_label.setProperty("muted", True)
        self.path_label.setStyleSheet("font-size: 12px;")
        lay.addWidget(self.path_label, 1)

        # 顶栏右侧状态: 共 N 张 / 已选中 X 张 / 待写入信息
        # (原"共 N 张 · 没有待写入的改动"从底栏移上来, 以让左侧网格空间更大)
        self.top_status = QLabel("")
        self.top_status.setProperty("muted", True)
        self.top_status.setStyleSheet("font-size: 12px;")
        lay.addWidget(self.top_status)

        lay.addWidget(self._vline())
        cap = QLabel("分组")
        cap.setProperty("muted", True)
        lay.addWidget(cap)

        self.sw_year = SwitchButton("年")
        self.sw_month = SwitchButton("月")
        self.sw_day = SwitchButton("日")
        self.sw_year.setChecked(True)
        for sw in (self.sw_year, self.sw_month, self.sw_day):
            sw.setToolTip("总开关：决定全部分组的最细层级")
        lay.addWidget(self.sw_year)
        lay.addWidget(self.sw_month)
        lay.addWidget(self.sw_day)

        # 显示方式下拉(六档) + 主网格 Ctrl+滚轮缩放
        vcap = QLabel("显示")
        vcap.setProperty("muted", True)
        lay.addWidget(vcap)
        self.view_mode_combo = _CenterComboArrow()
        self.view_mode_combo.setObjectName("mainViewCombo")
        for _name, mode, _gw, _gh in MAIN_VIEW_MODES:
            self.view_mode_combo.addItem(_name, mode)
        idx = next((i for i, c in enumerate(MAIN_VIEW_MODES) if c[1] == "sm"), 0)
        self.view_mode_combo.blockSignals(True)
        self.view_mode_combo.setCurrentIndex(idx)
        self.view_mode_combo.blockSignals(False)
        self.view_mode_combo.setItemDelegate(_CenterAlignDelegate(self.view_mode_combo))
        self.view_mode_combo.setStyleSheet(
            "#mainViewCombo { min-width: 96px; max-width: 96px;"
            " padding-left: 4px; padding-right: 2px; }"
            "#mainViewCombo::drop-down { width: 0px; border: none; }")
        self.view_mode_combo.setToolTip("切换显示方式（也可在主网格上按住 Ctrl 滚动滚轮）")
        lay.addWidget(self.view_mode_combo)

        lay.addWidget(self._vline())

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("过滤文件名…")
        self.search_edit.setFixedWidth(160)
        self.search_edit.setClearButtonEnabled(True)
        lay.addWidget(self.search_edit)

        self.sort_btn = QPushButton("排序 ▾")
        self.sort_btn.setObjectName("sortBtn")
        lay.addWidget(self.sort_btn)

        self.more_btn = QPushButton("⋯")
        self.more_btn.setFixedWidth(38)
        self.more_btn.setToolTip("更多")
        lay.addWidget(self.more_btn)
        return bar

    def _vline(self) -> QWidget:
        line = QWidget()
        line.setFixedSize(1, 22)
        line.setStyleSheet(f"background: {T.BORDER};")
        return line

    def _build_bottombar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(54)
        bar.setObjectName("bottombar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(12)

        lay.addStretch(1)

        self.clear_btn = QPushButton("清除计划")
        self.clear_btn.setToolTip("丢弃未写入的改动 (Ctrl+Shift+R)")
        lay.addWidget(self.clear_btn)

        self.interval_btn = QPushButton("自动间隔…")
        self.interval_btn.setToolTip("按当前顺序分配时间戳 (Ctrl+I)")
        lay.addWidget(self.interval_btn)

        self.write_btn = QPushButton("写入到文件")
        self.write_btn.setMinimumWidth(96)
        self.write_btn.setToolTip("执行写入 (Ctrl+Enter)")
        self.write_btn.setCursor(Qt.PointingHandCursor)
        set_accent(self.write_btn, True)
        lay.addWidget(self.write_btn)
        return bar

    # ---------------------------------------------------------------- 信号
    def _connect(self):
        self.open_btn.clicked.connect(self.pick_open)
        self.reload_btn.clicked.connect(self.reload_with_confirm)
        self.clear_list_btn.clicked.connect(self.clear_list)
        for sw, name in ((self.sw_year, "year"), (self.sw_month, "month"), (self.sw_day, "day")):
            sw.toggled.connect(lambda _=False, n=name: self._on_master_toggle(n))
        self.search_edit.textChanged.connect(self._on_filter)
        self.sort_btn.clicked.connect(self._show_sort_menu)
        self.more_btn.clicked.connect(self._show_more_menu)
        self.clear_btn.clicked.connect(self.clear_plan)
        self.interval_btn.clicked.connect(self.auto_interval)
        self.write_btn.clicked.connect(self.do_write)

        # 快捷键
        for key, fn in (
            (QKeySequence.Open, self.pick_folder),
            (QKeySequence("Ctrl+Shift+O"), self.pick_files),
            (QKeySequence("Ctrl+I"), self.auto_interval),
            (QKeySequence("Ctrl+Return"), self.do_write),
            (QKeySequence("Ctrl+R"), self.reload_with_confirm),
            (QKeySequence("Ctrl+Shift+R"), self.clear_plan),
            (QKeySequence("Ctrl+Shift+Delete"), self.clear_list),
            (QKeySequence("F5"), self.reload_with_confirm),
            (QKeySequence("Ctrl+A"), self.view.select_all_photos),
        ):
            act = QAction(self)
            act.setShortcut(key)
            act.setShortcutContext(Qt.WindowShortcut)
            act.triggered.connect(fn)
            self.addAction(act)

        self.view.toggleCollapse.connect(self.model.toggle_collapse)
        self.view.toggleSwitch.connect(self._on_group_toggle)
        self.view.orderChanged.connect(self._on_order_changed)
        self.view.needThumbs.connect(self._on_need_thumbs)
        self.view.customContextMenuRequested.connect(self._show_context_menu)
        self.view.deleteRequested.connect(self._delete_selected)
        self.view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.view.doubleClicked.connect(self._on_view_double_clicked)

        self.inspector.planChanged.connect(self._update_pending)
        self.inspector.editsChanged.connect(self._update_pending)
        self.inspector.apply_all_btn.clicked.connect(self._apply_first_time_to_all)

    # ---------------------------------------------------------------- 文件夹
    def pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择照片文件夹",
                                                  self.model.folder or "")
        if folder:
            self.open_folder(folder)

    def pick_files(self):
        from ..core.photo import IMAGE_EXT, VIDEO_EXT
        all_exts = sorted(IMAGE_EXT | VIDEO_EXT)
        patterns = " ".join(f"*.{e}" for e in all_exts)
        filt = "图片和视频 ({})".format(patterns)
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择图片或视频文件",
            self.model.folder or "", filt)
        if not paths:
            return
        self._add_files(paths)

    def _add_files(self, paths: list[str]):
        from ..core.photo import kind_of
        paths = [p for p in paths if os.path.isfile(p) and kind_of(p) != "other"]
        if not paths:
            QMessageBox.information(self, "没有可用文件",
                                    "选中的文件里没有支持的图片或视频格式。")
            return
        # 去重: 跳过已经在整个列表中存在的路径(含来自文件夹的照片)
        existing_paths = {p.path for p in self.model.photos}
        new_paths = [p for p in paths if p not in existing_paths]
        if not new_paths:
            self.status_label.setText("选中的文件都已在该列表中，未新增")
            return
        self.sources.append({"type": "files", "paths": new_paths})
        self.model.set_folder(self._primary_dir(), False)
        self._update_title()
        self._save_session()
        self._start_scan_files(new_paths)

    def pick_open(self):
        """统一的"添加"入口: 一个对话框既能选文件夹也能选文件, 由程序判断类型。"""
        from ..core.photo import kind_of
        dlg = PickerDialog(self, start_path=self.model.folder or os.path.expanduser("~"),
                           recursive=self.recursive)
        dlg.setMinimumSize(1080, 620)
        dlg.resize(1200, 700)
        if not dlg.exec():
            return
        folders = [os.path.normpath(p) for p in dlg.selected_folders()]
        files = [os.path.normpath(p) for p in dlg.selected_files()
                 if os.path.isfile(p) and kind_of(p) != "other"]
        if not folders and not files:
            return
        self.recursive = dlg.recursive()
        recursive = self.recursive
        # 记录来源(去重)
        for f in folders:
            if not any(s["type"] == "folder" and s["path"] == f and s["recursive"] == recursive
                       for s in self.sources):
                self.sources.append({"type": "folder", "path": f, "recursive": recursive})
        if files:
            self.sources.append({"type": "files", "paths": files})
        # 收集所有文件, 一次性累加扫描(避免多次扫描相互竞争导致文件丢失)
        all_files: list[str] = []
        seen: set[str] = set()
        for f in folders:
            for fp in collect_files(f, recursive):
                k = os.path.normcase(os.path.abspath(fp))
                if k not in seen:
                    seen.add(k)
                    all_files.append(fp)
        for fp in files:
            k = os.path.normcase(os.path.abspath(fp))
            if k not in seen:
                seen.add(k)
                all_files.append(fp)
        if not all_files:
            # 选中了内容却什么都没收集到（如选中的都是非图片/视频文件）。
            # 注：选中的文件夹“因未勾选含子文件夹而落空”的情况，已由弹窗在 _on_accept 内拦截并留住对话框，
            # 此处仅兜底处理其余残留的空结果，避免出现“无声失败”。
            self.status_label.setText("选中的内容里没有找到图片或视频")
            QMessageBox.information(
                self, "没有可添加的内容",
                "选中的内容里没有找到图片或视频，未添加任何文件。")
            self.stack.setCurrentWidget(self.view if self.model.photos else self.empty_page)
            return
        self.model.set_folder(self._primary_dir(), False)
        self._update_title()
        self._save_session()
        self._scan_files(all_files, accumulate=True)

    def open_folder(self, folder: str, accumulate: bool = True):
        if not os.path.isdir(folder):
            return
        recursive = self.recursive
        # 去重: 同一文件夹(含相同递归设置)不重复记录来源
        for s in self.sources:
            if s["type"] == "folder" and s["path"] == folder and s["recursive"] == recursive:
                break
        else:
            self.sources.append({"type": "folder", "path": folder, "recursive": recursive})
        self.model.set_folder(self._primary_dir(), False)
        self._update_title()
        self._save_session()
        files = collect_files(folder, recursive)
        if not files:
            self.status_label.setText(f"这个文件夹里没有找到图片或视频：{folder}")
            self.stack.setCurrentWidget(self.view if self.model.photos else self.empty_page)
            return
        self._scan_files(files, accumulate)

    def reload_folder(self):
        if not self.sources:
            return
        files: list[str] = []
        for s in self.sources:
            if s["type"] == "folder":
                files += collect_files(s["path"], s["recursive"])
            else:
                files += s["paths"]
        if files:
            self._scan_files(files, accumulate=False)

    def reload_with_confirm(self):
        """重新扫描: 重读所有来源元数据。若有未写入的改动, 先确认以免无声丢失。"""
        pending = self._pending_photos()
        if pending:
            ret = QMessageBox.question(
                self, "重新扫描",
                f"重新扫描会重新读取所有来源的文件元数据，\n"
                f"将丢失 {len(pending)} 个文件上尚未写入的时间/字段计划。\n"
                f"继续吗？")
            if ret != QMessageBox.Yes:
                return
        self.reload_folder()

    def clear_list(self):
        """清空当前已加载的所有照片/文件(内存中), 不删除磁盘文件, 不撤销已写入改动。"""
        if not self.model.photos:
            self.status_label.setText("列表已经是空的")
            return
        ret = QMessageBox.question(
            self, "清空列表",
            f"将移除当前已加载的 {len(self.model.photos)} 个文件。\n"
            f"这不会删除磁盘上的文件，也不会撤销已写入磁盘的改动，\n"
            f"但会丢失尚未写入的排序/时间计划。继续吗？")
        if ret != QMessageBox.Yes:
            return
        self.sources = []
        # 注意: 不清空会话文件, 这样"恢复上次来源"仍能找回清空前的工作集
        self.model.set_folder("", False)
        self.model.set_photos([])
        self.path_index = {}
        self._update_title()
        self.stack.setCurrentWidget(self.empty_page)
        self.status_label.setText("列表已清空，可重新打开文件夹或文件")
        self._update_pending()

    def save_sources(self):
        """把当前的来源集合(文件夹 + 文件)保存成 .photolist 清单, 供日后一键重载。"""
        if not self.sources:
            QMessageBox.information(self, "没有来源", "当前列表是空的，没有可保存的来源。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存来源清单", "照片来源.photolist", "照片来源清单 (*.photolist)")
        if not path:
            return
        if not path.lower().endswith(".photolist"):
            path += ".photolist"
        data = {"version": 1, "sources": self.sources}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.status_label.setText(f"已保存来源清单：{os.path.basename(path)}")
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    def export_csv(self):
        """把当前列表(按当前顺序)导出为 CSV, 方便写入前核对顺序与时间计划。"""
        from datetime import datetime
        if not self.model.photos:
            QMessageBox.information(self, "没有内容", "当前列表是空的，没有可导出的内容。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出清单 (CSV)", "照片清单.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            import csv
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                wtr = csv.writer(f)
                wtr.writerow(["序号", "文件名", "路径", "原拍摄时间", "计划拍摄时间", "大小(字节)"])
                for i, p in enumerate(self.model.photos, 1):
                    taken = p.taken_dt()
                    taken_str = taken.strftime("%Y-%m-%d %H:%M:%S") if taken else ""
                    planned = p.planned.get(F.TAKEN)
                    if isinstance(planned, datetime):
                        planned_str = planned.strftime("%Y-%m-%d %H:%M:%S")
                    elif planned:
                        planned_str = str(planned)
                    else:
                        planned_str = ""
                    wtr.writerow([i, p.name, p.path, taken_str, planned_str, p.size])
            self.status_label.setText(f"已导出清单：{os.path.basename(path)}（{len(self.model.photos)} 行）")
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def _save_session(self):
        """把当前来源集合自动持久化到会话文件(供'恢复上次来源'使用)。"""
        try:
            with open(self.session_path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "sources": self.sources}, f,
                          ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _apply_sources_file(self, path: str):
        """读取 .photolist 并替换当前列表(被'打开来源清单'与'恢复上次来源'复用)。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            QMessageBox.warning(self, "无法读取", str(exc))
            return
        sources = data.get("sources", [])
        if not sources:
            QMessageBox.information(self, "清单为空", "这份清单里没有来源。")
            return
        if self.model.photos:
            ret = QMessageBox.question(
                self, "打开来源清单",
                "打开清单会替换当前已加载的列表（不删除磁盘文件），继续吗？")
            if ret != QMessageBox.Yes:
                return
        # 收集所有文件(去重)
        all_files: list[str] = []
        seen: set[str] = set()
        for s in sources:
            if s.get("type") == "folder" and os.path.isdir(s["path"]):
                for fp in collect_files(s["path"], s.get("recursive", False)):
                    k = os.path.normcase(os.path.abspath(fp))
                    if k not in seen:
                        seen.add(k)
                        all_files.append(fp)
            elif s.get("type") == "files":
                for fp in s.get("paths", []):
                    if os.path.isfile(fp):
                        k = os.path.normcase(os.path.abspath(fp))
                        if k not in seen:
                            seen.add(k)
                            all_files.append(fp)
        if not all_files:
            QMessageBox.information(self, "没有可用文件",
                                    "清单里的来源当前都找不到文件（可能路径已变动）。")
            return
        self.sources = sources
        self.model.set_folder(self._primary_dir(), False)
        self._update_title()
        self._save_session()
        self._scan_files(all_files, accumulate=False)

    def load_sources(self):
        """从 .photolist 清单重载来源集合(替换当前列表)。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "打开来源清单", "", "照片来源清单 (*.photolist)")
        if not path:
            return
        self._apply_sources_file(path)

    def restore_session(self):
        """恢复上次自动保存的来源集合(替换当前列表)。"""
        if not os.path.exists(self.session_path):
            QMessageBox.information(self, "没有上次来源",
                                    "尚未自动保存过来源集合，无法恢复。先打开一些文件夹/文件即可。")
            return
        self._apply_sources_file(self.session_path)

    def _start_scan_files(self, files: list[str], accumulate: bool = True):
        """直接扫描指定文件列表（不从文件夹收集）。"""
        self._scan_files(files, accumulate)

    def _scan_files(self, files: list[str], accumulate: bool = False):
        if self.worker is not None and self.worker.isRunning():
            return
        self.stack.setCurrentWidget(self.view)
        self.thumb_worker.clear()
        self.worker = Scanner(self.model.folder, files, self)
        self.worker.progress.connect(self._on_scan_progress)
        self.worker.finished_scan.connect(
            lambda photos: self._on_scan_finished(photos, accumulate))
        self.worker.failed.connect(lambda msg: QMessageBox.warning(self, "扫描失败", msg))
        self.status_label.setText(f"正在读取 {len(files)} 个文件的元数据…")
        self.worker.start()

    def _on_scan_progress(self, done: int, total: int, name: str):
        self.status_label.setText(f"读取元数据 {done} / {total}　{name}")

    def _on_scan_finished(self, photos: list, accumulate: bool = False):
        if accumulate:
            self.model.add_photos(photos, dedupe=True)
        else:
            self.model.set_photos(photos)
        self.path_index = {p.path: p for p in self.model.photos}
        self.stack.setCurrentWidget(self.view if self.model.photos else self.empty_page)
        n = len(self.model.photos)
        # 不再在底部状态栏显示"拖拽可调整顺序"这类提示, 让界面更干净
        self.status_label.setText(f"已载入 {n} 个文件")
        self._update_pending()
        self.view.request_visible_thumbs()
        # 首次打开(非累加)时自动选中第一张照片, 立即在右侧面板显示数据
        if not accumulate and photos:
            QTimer.singleShot(50, self._select_first_photo)

    # ---------------------------------------------------------------- 来源管理
    def _primary_dir(self) -> str:
        """返回用于快照标注/回滚显示的代表目录(首个文件夹来源, 否则首个文件所在目录)。"""
        for s in self.sources:
            if s["type"] == "folder":
                return s["path"]
        for s in self.sources:
            if s["type"] == "files" and s["paths"]:
                return os.path.dirname(s["paths"][0])
        return ""

    def _win_title(self, suffix=""):
        return f"照片排序台 v{__build__}{suffix}"


    def _update_title(self):
        n_folder = sum(1 for s in self.sources if s["type"] == "folder")
        n_file_src = sum(1 for s in self.sources if s["type"] == "files")
        n_files = sum(len(s["paths"]) for s in self.sources if s["type"] == "files")
        if not self.sources:
            self.path_label.setText("未选择文件夹")
            self.setWindowTitle(self._win_title())
            return
        if len(self.sources) == 1 and self.sources[0]["type"] == "folder":
            f = self.sources[0]
            self.path_label.setText(f["path"])
            self.setWindowTitle(self._win_title(f" — {os.path.basename(f['path']) or f['path']}"))
        elif len(self.sources) == 1 and self.sources[0]["type"] == "files":
            f = self.sources[0]
            folder = os.path.dirname(f["paths"][0])
            self.path_label.setText(f"{folder} （{len(f['paths'])} 个文件）")
            self.setWindowTitle(self._win_title(f" — {len(f['paths'])} 个文件"))
        else:
            self.path_label.setText(
                f"已累加：{n_folder} 个文件夹 + {n_files} 个文件")
            self.setWindowTitle(self._win_title(f" — 累加 {n_folder} 文件夹 / {n_files} 文件"))

    def _select_first_photo(self):
        m = self.model
        from ..core.photo import PhotoRow
        from PySide6.QtCore import QItemSelectionModel
        for i in range(len(m.rows)):
            if isinstance(m.rows[i], PhotoRow):
                idx = m.index(i)
                self.view.selectionModel().select(idx, QItemSelectionModel.ClearAndSelect)
                self.view.scrollTo(idx)
                break

    # ---------------------------------------------------------------- 分组开关
    def _on_master_toggle(self, _name: str = ""):
        year = self.sw_year.isChecked()
        month = self.sw_month.isChecked()
        day = self.sw_day.isChecked()
        self.sw_month.setEnabled(year)
        self.sw_day.setEnabled(year)
        if not year:
            self.sw_month.setChecked(False)
            self.sw_day.setChecked(False)
        if not month:
            self.sw_day.setChecked(False)
        self.model.set_master(year, month, day)

    def _on_group_toggle(self, year: int, which: str, on: bool):
        """分组标题里的月/日开关被点击。

        - 只影响该年份分组
        - 如果上方总开关是关的, 自动把它打开（仅作为"有分组启用"的指示）
        - 不会影响其它分组的开关状态
        - 日 依赖 月: 开 日 时自动开 月
        """
        self.model.set_year_toggle(year, which, on)
        # 日依赖月: 开日时自动开月
        if on and which == "day":
            st = self.model.year_state(year)
            if not st.get("month"):
                self.model.set_year_toggle(year, "month", True)
                top_month = self.sw_month
                if not top_month.isChecked():
                    top_month.blockSignals(True)
                    top_month.setChecked(True)
                    self.model.master["month"] = True
                    top_month.blockSignals(False)
        # 同步顶栏开关
        top_sw = self.sw_month if which == "month" else self.sw_day
        if on and not top_sw.isChecked():
            top_sw.blockSignals(True)
            top_sw.setChecked(True)
            self.model.master[which] = True
            top_sw.blockSignals(False)
        elif not on:
            any_on = any(st.get(which, False) for st in self.model.per_year.values())
            if not any_on and top_sw.isChecked():
                top_sw.blockSignals(True)
                top_sw.setChecked(False)
                self.model.master[which] = False
                top_sw.blockSignals(False)
                # 关月时也关日
                if which == "month" and self.sw_day.isChecked():
                    self.sw_day.blockSignals(True)
                    self.sw_day.setChecked(False)
                    self.model.master["day"] = False
                    self.sw_day.blockSignals(False)

    def _on_filter(self, text: str):
        self.model.filter_text = text.strip()
        self.model.rebuild()

    def _on_order_changed(self):
        self._update_pending()

    # ---------------------------------------------------------------- 选择
    def _on_selection_changed(self):
        photos = self.view.selected_photos()
        self.inspector.set_photos(photos)
        self._refresh_top_status()

    def _selected_or_all(self) -> list:
        photos = self.view.selected_photos()
        return photos if photos else self.model.all_photos()

    # ---------------------------------------------------------------- 排序
    def _show_sort_menu(self):
        menu = QMenu(self)
        has_sel = bool(self.view.selected_photos())
        scope = "选中项" if has_sel else "全部"
        menu.addAction("自动间隔分配时间…", self.auto_interval)
        menu.addAction("反转顺序", self.reverse_order)
        menu.addSeparator()
        menu.addAction(f"按拍摄时间（新→旧）　（{scope}）", lambda: self.sort_by("time", True))
        menu.addAction(f"按拍摄时间（旧→新）　（{scope}）", lambda: self.sort_by("time", False))
        menu.addSeparator()
        menu.addAction(f"按文件名升序　（{scope}）", lambda: self.sort_by("name", False))
        menu.addAction(f"按文件名降序　（{scope}）", lambda: self.sort_by("name", True))
        menu.addSeparator()
        menu.addAction(f"按文件大小　（{scope}）", lambda: self.sort_by("size", True))
        menu.exec(self.sort_btn.mapToGlobal(self.sort_btn.rect().bottomLeft()))

    def _show_more_menu(self):
        menu = QMenu(self)
        menu.addAction("保存来源清单…", self.save_sources)
        menu.addAction("打开来源清单…", self.load_sources)
        menu.addAction("恢复上次来源…", self.restore_session)
        menu.addAction("导出清单 (CSV)…", self.export_csv)
        menu.addSeparator()
        menu.addAction("备份与回滚…", self.open_rollback)
        menu.addAction("清除所有时间计划", self.clear_plan)
        menu.addSeparator()
        menu.addAction("在资源管理器中显示所选", self.reveal_selected)
        menu.addAction("清除缩略图缓存", self.clear_thumb_cache)
        menu.addSeparator()
        menu.addAction("关于", self.show_about)
        menu.exec(self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft()))

    def sort_by(self, key: str, reverse: bool):
        sel = self.view.selected_photos()
        if not sel:
            self.model.sort_photos(key, reverse)
        else:
            self._sort_subset(sel, key, reverse)
        self._after_order_change()

    def _sort_subset(self, photos: list, key: str, reverse: bool):
        ids = set(id(p) for p in photos)
        positions = [i for i, p in enumerate(self.model.photos) if id(p) in ids]
        if not positions:
            return
        if key == "name":
            photos.sort(key=lambda p: p.name.lower(), reverse=reverse)
        elif key == "time":
            photos.sort(key=lambda p: p.sort_dt(), reverse=reverse)
        elif key == "size":
            photos.sort(key=lambda p: p.size, reverse=reverse)
        for i, p in zip(positions, photos):
            self.model.photos[i] = p
        self.model.rebuild()

    def reverse_order(self):
        sel = self.view.selected_photos()
        if not sel:
            self.model.reverse_photos()
        else:
            ids = set(id(p) for p in sel)
            positions = [i for i, p in enumerate(self.model.photos) if id(p) in ids]
            reversed_photos = list(reversed([self.model.photos[i] for i in positions]))
            for i, p in zip(positions, reversed_photos):
                self.model.photos[i] = p
            self.model.rebuild()
        self._after_order_change()

    def _after_order_change(self):
        self.model.orderChanged.emit()
        self.view.request_visible_thumbs()

    def auto_interval(self):
        photos = self._selected_or_all()
        if not photos:
            return
        dlg = IntervalDialog(photos, self)
        if dlg.exec() != IntervalDialog.Accepted:
            return
        timeplan.apply_plan(photos, dlg.start_datetime, dlg.step_seconds,
                            dlg.direction, dlg.sync_mode)
        self._after_order_change()
        self._update_pending()
        self.status_label.setText(
            f"已为 {len(photos)} 张分配时间，第 1 张 = "
            f"{dlg.start_datetime:%Y-%m-%d %H:%M:%S}")

    def _apply_first_time_to_all(self):
        photos = self.view.selected_photos()
        if len(photos) < 2:
            return
        self.inspector.apply_time_to_all(photos)
        self._after_order_change()
        self._update_pending()

    def clear_plan(self):
        photos = self._selected_or_all()
        if not photos:
            return
        n = sum(1 for p in photos if p.dirty)
        if n == 0:
            return
        ret = QMessageBox.question(self, "清除计划",
                                   f"将丢弃 {n} 个文件上尚未写入的时间与字段改动，继续吗？")
        if ret != QMessageBox.Yes:
            return
        for p in photos:
            p.clear_pending()
        self.model.rebuild()
        self.inspector.refresh()
        self._update_pending()

    # ---------------------------------------------------------------- 写入
    def _pending_photos(self) -> list:
        return [p for p in self.model.photos if p.dirty]

    def _refresh_top_status(self):
        """顶栏右侧: 共 N 张 / 已选中 X 张 / 待写入信息。"""
        total = len(self.model.photos)
        sel = self.view.selectionCount()
        pending = self._pending_photos()
        if sel:
            text = f"已选中 {sel} 张 / 共 {total} 张"
        else:
            text = f"共 {total} 张"
        if pending:
            text += f"　·　待写入 {len(pending)} 项"
        else:
            text += "　·　没有待写入的改动"
        self.top_status.setText(text)
        if pending:
            self.top_status.setStyleSheet(f"color: {T.WARN}; font-size: 12px;")
        else:
            self.top_status.setStyleSheet("color: #5F6B7A; font-size: 12px;")

    def _update_pending(self):
        self._refresh_top_status()
        self.view.viewport().update()

    # ---------------------------------------------------------------- 显示方式 / 缩放
    def _apply_main_view_mode(self):
        """把顶栏"显示方式"下拉的当前模式应用到主网格。"""
        if not hasattr(self, "view") or self.view is None:
            return
        mode = self.view_mode_combo.currentData()
        cfg = next((c for c in MAIN_VIEW_MODES if c[1] == mode), MAIN_VIEW_MODES[0])
        _name, _mode, gw, gh = cfg
        horiz = _mode in ("list", "details")
        # 图标档与"详细信息"显示副标题(拍摄时间); "列表"档只有一行文件名
        self.view.delegate.set_cell(gw, gh, horizontal=horiz, show_info=(_mode != "list"))
        self.view.setFlow(QListView.TopToBottom if horiz else QListView.LeftToRight)
        self.view.setWrapping(True)
        self.view.scheduleDelayedItemsLayout()
        self.view.viewport().update()
        self.view._thumb_timer.start()

    def _zoom_main_step(self, zoom_in: bool):
        """Ctrl+滚轮：在四档显示方式之间步进（上滚=放大，最小到"小图标"为止）。"""
        modes = [m[1] for m in MAIN_VIEW_MODES]
        cur = self.view_mode_combo.currentData()
        i = modes.index(cur) if cur in modes else 0
        # 档位由小到大排列(小图标在最前) → 上滚放大 = 索引增大; 下滚缩小 = 索引减小
        i = max(0, min(len(modes) - 1, i + (1 if zoom_in else -1)))
        self.view_mode_combo.setCurrentIndex(i)

    def do_write(self):
        photos = self._pending_photos()
        if not photos:
            QMessageBox.information(self, "没有改动",
                                    "先用拖拽排好顺序，再用「自动间隔」或右侧面板改动字段。")
            return
        self._pending_photos_cache = photos
        changes = writer.build_changes(photos)
        dlg = ChangePreviewDialog(changes, len(photos), self, rebuilder=self._rebuild_changes)
        if dlg.exec() != ChangePreviewDialog.Accepted:
            return
        opts = dlg.options()
        changes = writer.build_changes(photos, thorough=opts["thorough"],
                                       fill_xmp=opts["fill_xmp"], fill_qt=opts["fill_qt"])
        if not changes:
            QMessageBox.information(self, "没有改动", "没有需要写入的字段。")
            return
        ret = QMessageBox.question(
            self, "再次确认",
            f"即将写入 {len(photos)} 个文件、{len(changes)} 项改动。\n"
            f"写入前会自动保存回滚快照。确定继续吗？")
        if ret != QMessageBox.Yes:
            return
        self._run_write(changes, opts)

    def _rebuild_changes(self, dlg: ChangePreviewDialog):
        opts = dlg.options()
        changes = writer.build_changes(self._pending_photos_cache,
                                       thorough=opts["thorough"],
                                       fill_xmp=opts["fill_xmp"], fill_qt=opts["fill_qt"])
        dlg.refill(changes, len(self._pending_photos_cache))

    def _run_write(self, changes, opts: dict):
        self.progress_dialog = ProgressDialog("写入中", self)
        self.write_worker = writer.WriteWorker(changes, self.model.folder,
                                               opts.get("in_place", True), self)
        self.write_worker.progress.connect(self.progress_dialog.on_progress)
        self.write_worker.log.connect(self.progress_dialog.on_log)
        self.write_worker.finished_write.connect(self._on_write_finished)
        self.progress_dialog.canceled.connect(self.write_worker.cancel)
        self.write_worker.start()
        self.progress_dialog.exec()

    def _on_write_finished(self, result):
        if self.progress_dialog:
            self.progress_dialog.finish()
        for path, meta in result.meta_updates.items():
            photo = self.path_index.get(path)
            if photo is None:
                continue
            photo.meta = meta
            photo.planned.clear()
            photo.pending.clear()
            photo.status = STATUS_DONE
        failed_paths = set(p for p, _ in result.failed)
        for photo in self.model.photos:
            if photo.path in failed_paths:
                photo.status = 3
        # 时间已按新顺序写进文件, 分组重新交给日期来算
        self.model.clear_group_overrides()
        self.model.rebuild()
        self.inspector.refresh()
        self._update_pending()
        lines = [f"完成 {len(result.ok_files)} 个文件，共 {result.total_changes} 项改动。"]
        if result.failed:
            lines.append(f"失败 {len(result.failed)} 个文件。")
        if result.mismatches:
            lines.append(f"有 {len(result.mismatches)} 项读回校验不一致，建议抽查。")
        lines.append(f"回滚快照：{result.snapshot_id}")
        if self.progress_dialog:
            self.progress_dialog.on_log("　".join(lines))
        self.status_label.setText("　".join(lines))

    # ---------------------------------------------------------------- 回滚
    def open_rollback(self):
        dlg = RollbackDialog(self)
        dlg.rollbackRequested.connect(self._run_rollback)
        dlg.exec()

    def _run_rollback(self, snapshot_path: str):
        dlg = ProgressDialog("回滚中", self)
        worker = writer.RollbackWorker(snapshot_path, self)
        worker.progress.connect(dlg.on_progress)
        worker.log.connect(dlg.on_log)
        worker.finished_rollback.connect(lambda w: self._on_rollback_finished(w, dlg))
        dlg.canceled.connect(lambda: None)
        worker.start()
        dlg.exec()

    def _on_rollback_finished(self, worker, dlg: ProgressDialog):
        dlg.finish()
        dlg.on_log(f"已还原 {len(worker.ok)} 个文件，失败 {len(worker.failed)} 个。")
        for path in worker.ok:
            photo = self.path_index.get(path)
            if photo:
                photo.status = STATUS_NORMAL
        self.reload_folder()
        self._update_pending()

    # ---------------------------------------------------------------- 其它
    def _show_context_menu(self, pos):
        photos = self.view.selected_photos()
        menu = QMenu(self)
        scope = f"选中 {len(photos)} 张" if photos else "全部"
        menu.addAction(f"自动间隔分配时间…　（{scope}）", self.auto_interval)
        menu.addAction("反转顺序", self.reverse_order)
        menu.addSeparator()
        menu.addAction("按拍摄时间（新→旧）", lambda: self.sort_by("time", True))
        menu.addAction("按拍摄时间（旧→新）", lambda: self.sort_by("time", False))
        menu.addSeparator()
        menu.addAction("按文件名升序", lambda: self.sort_by("name", False))
        menu.addAction("按文件名降序", lambda: self.sort_by("name", True))
        menu.addSeparator()
        menu.addAction("按文件大小", lambda: self.sort_by("size", True))
        menu.addSeparator()
        if len(photos) > 1:
            menu.addAction("把第一张的时间应用到全部选中", self._apply_first_time_to_all)
        menu.addAction("全选", self.view.select_all_photos)
        menu.addAction("在资源管理器中显示", self.reveal_selected)
        menu.addSeparator()
        menu.addAction("清除这批改动", self.clear_plan)
        if photos:
            # 仅在有选中项时出现，避免误导（这两项都是对"选中项"生效）
            menu.addSeparator()
            menu.addAction("从列表移除（不动磁盘文件）", self._remove_selected_from_list)
            menu.addAction("永久删除文件…", self._delete_selected)
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _on_view_double_clicked(self, index):
        """主列表双击: 用系统默认应用打开该文件(图片用看图, 视频用播放器等)。"""
        if not index.isValid():
            return
        row = self.model.data(index, PhotoModel.ROLE_KIND)
        if row != "photo":
            return
        photo = self.model.data(index, PhotoModel.ROLE_PHOTO)
        if photo is None:
            return
        path = getattr(photo, "path", None)
        if not path or not os.path.exists(path):
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as ex:
            QMessageBox.warning(self, "打开失败", f"无法用默认应用打开:\n{path}\n\n{ex}")

    # ---------------------------------------------------------------- 移除 / 删除
    def _remove_from_list(self, photos: list):
        """仅从内存列表中移除这些照片对象（磁盘文件不动）。"""
        if not photos:
            return
        kill = {id(p) for p in photos}
        self.model.photos = [p for p in self.model.photos if id(p) not in kill]
        self.model.rebuild()
        self.path_index = {p.path: p for p in self.model.photos}
        self.inspector.set_photos([])
        self.stack.setCurrentWidget(self.view if self.model.photos else self.empty_page)
        self._update_title()
        self._update_pending()

    def _remove_selected_from_list(self):
        """右键「从列表移除」：只从列表拿掉，不碰磁盘文件，二次确认。"""
        photos = self.view.selected_photos()
        if not photos:
            return
        pending = [p for p in photos if p.dirty]
        extra = (f"\n注意：其中 {len(pending)} 个文件有尚未写入的时间改动，移除后会丢失。"
                 if pending else "")
        text = (f"将从列表移除 {len(photos)} 项。\n"
                f"这不会删除磁盘上的文件，也不会撤销已写入的改动。{extra}\n\n继续吗？")
        if not confirm_destructive(self, "从列表移除", text):
            return
        n = len(photos)
        self._remove_from_list(photos)
        self.status_label.setText(f"已从列表移除 {n} 项（磁盘文件未改动）")

    def _delete_selected(self):
        """右键「永久删除」/ DEL 键：把选中项从磁盘永久删除（不进回收站），二次确认。"""
        photos = self.view.selected_photos()
        if not photos:
            QMessageBox.information(self, "没有选中项", "请先在列表里选中要删除的文件。")
            return
        pending = [p for p in photos if p.dirty]
        extra = (f"\n注意：其中 {len(pending)} 个文件有尚未写入的时间改动，删除后会一起丢失。"
                 if pending else "")
        names = "\n".join(os.path.basename(p.path) for p in photos[:8])
        if len(photos) > 8:
            names += f"\n… 等共 {len(photos)} 个文件"
        text = (f"将从磁盘上永久删除以下 {len(photos)} 个文件。\n"
                f"此操作不进回收站、无法恢复！{extra}\n\n{names}\n\n确定要删除吗？")
        if not confirm_destructive(self, "永久删除文件", text):
            return
        removed, failed = [], []
        for p in photos:
            try:
                os.remove(p.path)
                removed.append(p)
            except OSError as ex:
                failed.append(f"{os.path.basename(p.path)}：{ex}")
        if removed:
            self._remove_from_list(removed)
        if failed:
            QMessageBox.warning(self, "部分文件删除失败",
                                "以下文件未能删除：\n\n" + "\n".join(failed[:10]))
            self.status_label.setText(f"已删除 {len(removed)} 个，{len(failed)} 个失败")
        else:
            self.status_label.setText(f"已永久删除 {len(removed)} 个文件")

    def reveal_selected(self):
        photos = self.view.selected_photos() or self.model.photos[:1]
        if not photos:
            return
        path = os.path.normpath(photos[0].path)
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception:
            pass

    def clear_thumb_cache(self):
        from ..core.thumbs import cache_dir
        folder = cache_dir()
        count = 0
        try:
            for name in os.listdir(folder):
                os.remove(os.path.join(folder, name))
                count += 1
        except OSError:
            pass
        QMessageBox.information(self, "缓存已清理", f"已删除 {count} 个缩略图缓存文件。")

    def show_about(self):
        from ..core.exiftool import get_exiftool
        try:
            ver = get_exiftool().version()
        except Exception:
            ver = "未找到"
        dlg = QDialog(self)
        dlg.setWindowTitle(f"关于 照片排序台 v{__build__}")
        dlg.setFixedWidth(380)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(22, 22, 22, 22)
        lay.setSpacing(12)

        # 顶部 logo（与 exe 图标一致）
        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)
        pm = QPixmap(app_resource("logo.ico")).scaled(
            72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        logo.setPixmap(pm)
        lay.addWidget(logo)

        title = QLabel("照片排序台")
        title.setObjectName("aboutTitle")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)

        info = QLabel(
            "拖拽排顺序 → 分配时间戳 → 一次写回 修改时间 / 创建时间 / "
            "拍摄时间（含 EXIF、XMP、QuickTime 与文件系统时间）。\n\n"
            f"软件版本：{__version__}（构建 v{__build__}）\n"
            f"ExifTool 版本：{ver}\n"
            "每次写入前都会保存回滚快照，可在「⋯ → 备份与回滚」里还原。"
        )
        info.setWordWrap(True)
        info.setProperty("muted", True)
        lay.addWidget(info)

        sc = QLabel(
            "快捷键：\n"
            "  Ctrl+O 打开文件夹　Ctrl+Shift+O 打开文件\n"
            "  Ctrl+I 自动间隔　Ctrl+Enter 写入到文件\n"
            "  Ctrl+R / F5 重新扫描　Ctrl+Shift+R 清除计划\n"
            "  Ctrl+A 全选　右键 排序 / 间隔 / 全选"
        )
        sc.setWordWrap(True)
        sc.setProperty("muted", True)
        lay.addWidget(sc)

        lay.addStretch(1)
        btn = QPushButton("确定")
        btn.setDefault(True)
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, 0, Qt.AlignCenter)
        dlg.exec()

    # ---------------------------------------------------------------- 缩略图
    def _on_need_thumbs(self, paths: list):
        if paths:
            self.thumb_worker.request(paths)

    def _on_thumb_ready(self, path: str, pix):
        photo = self.path_index.get(path)
        if photo is None:
            return
        photo.thumb = pix
        photo.thumb_state = THUMB_OK
        if photo in self.inspector.photos:
            self.inspector.set_photos(self.inspector.photos)
        self.model.refresh_photo(photo)

    def _on_thumb_failed(self, path: str):
        photo = self.path_index.get(path)
        if photo is not None:
            photo.thumb_state = THUMB_FAIL

    # ---------------------------------------------------------------- 启动检查
    def _check_exiftool(self):
        exe = find_exiftool()
        if not exe:
            QMessageBox.warning(
                self, "缺少 ExifTool",
                "没有找到 exiftool。请把 exiftool.exe 放到程序目录下的 tools 文件夹里，\n"
                "或者安装后加入系统 PATH，再重新启动。")
            self.status_label.setText("未找到 exiftool，无法读写元数据")
            return
        self.status_label.setText(f"ExifTool 已就绪：{exe}")

    # ---------------------------------------------------------------- 关闭
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            return
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        try:
            self.thumb_worker.stop()
            self.thumb_worker.wait(2000)
        except Exception:
            pass
        super().closeEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if path and (os.path.isdir(path) or os.path.isfile(path)):
                    event.acceptProposedAction()
                    return

    def dropEvent(self, event):
        from ..core.photo import kind_of
        urls = event.mimeData().urls()
        file_paths = []
        folder_path = None
        for url in urls:
            path = url.toLocalFile()
            if not path:
                continue
            if os.path.isdir(path):
                folder_path = path
                break
            elif os.path.isfile(path) and kind_of(path) != "other":
                file_paths.append(path)
        if folder_path:
            self.open_folder(folder_path, accumulate=True)
        elif file_paths:
            existing_paths = {p.path for p in self.model.photos}
            new_paths = [p for p in file_paths if p not in existing_paths]
            if not new_paths:
                self.status_label.setText("拖入的文件都已在该列表中，未新增")
                return
            self.sources.append({"type": "files", "paths": new_paths})
            self.model.set_folder(self._primary_dir(), False)
            self._update_title()
            self._scan_files(new_paths, accumulate=True)
