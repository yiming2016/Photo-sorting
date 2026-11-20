# -*- coding: utf-8 -*-
"""FFmpeg 设置对话框: 查看当前路径、选择本地 FFmpeg、检测、获取 FFmpeg、清除自定义。"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QVBoxLayout)

from . import ffmpeg_locate, proc, settings
from ..ui import theme as T


def _decode(b: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", "replace")


class InstallWorker(QThread):
    """用 winget 自动安装 FFmpeg, 边跑边把输出吐给对话框。"""

    line = Signal(str)
    done = Signal(bool, str)

    WINGET_ID = "Gyan.FFmpeg"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None

    def run(self):
        exe = shutil.which("winget") or shutil.which("winget.exe")
        if not exe:
            self.done.emit(False, "系统里没有 winget，无法自动安装。"
                                 "请点「获取 FFmpeg…」手动下载。")
            return
        cmd = [exe, "install", "--id", self.WINGET_ID, "-e",
               "--accept-source-agreements", "--accept-package-agreements",
               "--disable-interactivity"]
        self.line.emit("$ " + " ".join(cmd))
        try:
            self._proc = proc.popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
        except Exception as e:
            self.done.emit(False, f"无法启动 winget：{e}")
            return
        try:
            while True:
                chunk = self._proc.stdout.readline()
                if not chunk:
                    break
                text = _decode(chunk)
                # winget 的下载进度用 \r 反复刷新同一行, 只留最后一段
                if "\r" in text:
                    text = text.split("\r")[-1]
                text = text.rstrip("\r\n")
                if text.strip():
                    self.line.emit(text)
            code = self._proc.wait()
        except Exception as e:
            self.done.emit(False, f"安装过程出错：{e}")
            return
        self.done.emit(code == 0, f"winget 结束，退出码 {code}")

    def cancel(self):
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass


def run_version(exe: str):
    """跑 <exe> -version。返回 (是否成功, 首行/错误说明)。"""
    if not exe or not Path(exe).exists():
        return False, "（文件不存在）"
    try:
        out = proc.run([exe, "-version"], timeout=15)
    except Exception as e:
        return False, f"无法运行：{e}"
    first = (out.stdout or out.stderr or "").strip().splitlines()
    if out.returncode == 0 and first:
        return True, first[0]
    return False, f"运行失败（返回码 {out.returncode}）"


_run_version = run_version    # 旧名保留


class FFmpegSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("FFmpeg 设置 · 照片排序台")
        self.setMinimumWidth(600)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        tip = QLabel("合并视频需要 FFmpeg。程序会自动查找；若找不到或用不了，"
                     "可在此手动指定本地 FFmpeg 所在的文件夹（该文件夹需同时包含 "
                     "ffmpeg 与 ffprobe）。")
        tip.setWordWrap(True)
        tip.setProperty("muted", True)
        root.addWidget(tip)

        # ---- 使用方式: 自动(优先本地) / 仅用本地指定 (选择会被保存为默认) ----
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        mode_lab = QLabel("使用方式")
        mode_lab.setStyleSheet(f"color:{T.TEXT_SUB}; font-size:12px;")
        mode_row.addWidget(mode_lab)
        self.auto_btn = QPushButton("自动（优先本地）")
        self.local_btn = QPushButton("仅用本地指定")
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        for b in (self.auto_btn, self.local_btn):
            b.setCheckable(True)
            b.setProperty("seg", "true")
            b.setCursor(Qt.PointingHandCursor)
            self._mode_group.addButton(b)
            mode_row.addWidget(b)
        mode_row.addStretch(1)
        root.addLayout(mode_row)
        self.mode_tip = QLabel("")
        self.mode_tip.setWordWrap(True)
        self.mode_tip.setProperty("muted", True)
        root.addWidget(self.mode_tip)

        # 先定初值再连信号, 免得初始化时反写一次配置
        if settings.load_ffmpeg_mode() == settings.MODE_LOCAL:
            self.local_btn.setChecked(True)
        else:
            self.auto_btn.setChecked(True)
        self.auto_btn.toggled.connect(self._on_mode_changed)
        self.local_btn.toggled.connect(self._on_mode_changed)

        # 左: 本地 FFmpeg 地址(只读, 可复制)   右: 选择按钮
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("未找到 FFmpeg")
        self.path_edit.setMinimumWidth(300)
        self.choose_btn = QPushButton("选择本地 FFmpeg…")
        self.choose_btn.clicked.connect(self._choose)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.choose_btn, 0)
        root.addLayout(path_row)

        self.path_label = QLabel("")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.path_label)

        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.result)

        btn_row = QHBoxLayout()
        self.test_btn = QPushButton("检测")
        self.test_btn.setMinimumWidth(72)
        self.test_btn.clicked.connect(self._test)
        self.get_btn = QPushButton("获取 FFmpeg…")
        self.get_btn.clicked.connect(self._get)
        self.install_btn = QPushButton("命令安装 FFmpeg")
        self.install_btn.setToolTip("用 Windows 自带的 winget 自动下载安装 FFmpeg"
                                    "（Gyan.FFmpeg，约 100 MB）")
        self.install_btn.clicked.connect(self._install)
        self.clear_btn = QPushButton("清除自定义")
        self.clear_btn.clicked.connect(self._clear)
        btn_row.addWidget(self.test_btn)
        btn_row.addWidget(self.get_btn)
        btn_row.addWidget(self.install_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.clear_btn)
        root.addLayout(btn_row)

        # 安装日志(只在安装时出现)
        self.install_log = QPlainTextEdit()
        self.install_log.setReadOnly(True)
        self.install_log.setFixedHeight(120)
        self.install_log.setStyleSheet(
            f"font-family: {T.MONO_FAMILY}; font-size: 11px;"
            f"color: {T.TEXT_SUB}; background: {T.SURFACE_ALT};"
            f"border: 1px solid {T.BORDER}; border-radius: 6px;")
        self.install_log.setVisible(False)
        root.addWidget(self.install_log)

        self._worker = None
        self._refresh()

    # ---------------------------------------------------------------- 逻辑
    def _mode(self) -> str:
        return settings.MODE_LOCAL if self.local_btn.isChecked() else settings.MODE_AUTO

    def _on_mode_changed(self, checked: bool):
        if not checked:
            return
        settings.save_ffmpeg_mode(self._mode())
        self.result.setText("")
        self._refresh()

    def _set_test_state(self, ok):
        """检测按钮的呈现: True=可用(绿), False=不可用(红), None=未检测(默认)。"""
        if ok is True:
            bg = T.OK
            self.test_btn.setStyleSheet(
                f"QPushButton{{background:{bg};border:1px solid {bg};"
                f"color:#FFFFFF;font-weight:600;}}"
                f"QPushButton:hover{{background:#1A7E48;border-color:#1A7E48;}}")
            self.test_btn.setToolTip("检测通过：FFmpeg 可以正常运行")
        elif ok is False:
            self.test_btn.setStyleSheet(
                f"QPushButton{{color:{T.DANGER};border-color:#F0BEBE;}}"
                f"QPushButton:hover{{background:{T.DANGER_SOFT};}}")
            self.test_btn.setToolTip("检测未通过：FFmpeg 无法正常运行")
        else:
            self.test_btn.setStyleSheet("")
            self.test_btn.setToolTip("")

    def _refresh(self):
        """重新查找并刷新「当前路径」, 同时把检测状态复位(路径变了, 旧结果作废)。"""
        mode = self._mode()
        if mode == settings.MODE_LOCAL:
            self.mode_tip.setText("仅用本地：只用你手动指定的 FFmpeg，不会自动搜索。")
        else:
            self.mode_tip.setText("自动：优先用本地指定的 FFmpeg，没有或失效时自动搜索。")
        pair = ffmpeg_locate.find_ffmpeg_pair(mode)
        ud = settings.load_ffmpeg_dir()
        # 路径可能已变化, 之前的检测结论不再可靠
        self._set_test_state(None)

        if pair:
            src = ("自定义" if (ud and str(Path(pair[0]).parent) == str(Path(ud)))
                   else "自动查找")
            self.path_edit.setText(pair[0])
            self.path_edit.setCursorPosition(0)
            self.path_edit.setToolTip(pair[0])
            self.path_label.setText(f"当前来源：{src}　·　ffprobe：{pair[1]}")
            self.path_label.setStyleSheet(f"color:{T.TEXT_SUB};")
        else:
            self.path_edit.setText("")
            self.path_edit.setToolTip("")
            if mode == settings.MODE_LOCAL:
                self.path_label.setText("当前：没有指定本地 FFmpeg（「仅用本地指定」模式下"
                                        "不会自动查找）")
            else:
                self.path_label.setText("当前：未找到 FFmpeg / ffprobe")
            self.path_label.setStyleSheet(f"color:{T.DANGER};")
        self.clear_btn.setEnabled(bool(ud))

    def _choose(self):
        start = self.path_edit.text() or os.getcwd()
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ffmpeg 可执行文件", start,
            "ffmpeg (ffmpeg.exe ffmpeg);;可执行文件 (*.exe);;所有文件 (*.*)")
        if not path:
            return
        folder = str(Path(path).parent)
        ext = ".exe" if os.name == "nt" else ""
        has_ff = (Path(folder) / f"ffmpeg{ext}").exists()
        has_fp = (Path(folder) / f"ffprobe{ext}").exists()
        if not (has_ff and has_fp):
            self.result.setText(f"⚠ 该文件夹需同时包含 ffmpeg 与 ffprobe。\n{folder}")
            self.result.setStyleSheet(f"color:{T.DANGER};")
            return
        settings.save_ffmpeg_dir(folder)
        self.result.setText(f"已保存：{folder}")
        self.result.setStyleSheet(f"color:{T.TEXT};")
        self._refresh()

    def _test(self):
        pair = ffmpeg_locate.find_ffmpeg_pair(self._mode())
        if not pair:
            self.result.setText("未找到 FFmpeg，无法检测。")
            self.result.setStyleSheet(f"color:{T.DANGER};")
            self._set_test_state(False)
            return
        ff_ok, fv = _run_version(pair[0])
        fp_ok, pv = _run_version(pair[1])
        ok = ff_ok and fp_ok
        self.result.setText(f"ffmpeg：{fv}\nffprobe：{pv}")
        self.result.setStyleSheet(f"color:{T.OK if ok else T.DANGER};")
        self._set_test_state(ok)

    def _get(self):
        QDesktopServices.openUrl(QUrl("https://ffmpeg.org/download.html"))

    # ---------------------------------------------------------------- 命令安装
    def _install(self):
        if self._worker is not None:
            return
        ret = QMessageBox.question(
            self, "命令安装 FFmpeg",
            "将用 Windows 自带的 winget 自动下载并安装 FFmpeg（Gyan.FFmpeg）。\n"
            "需要联网，约 100 MB，视网速可能要几分钟。\n\n要继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self.install_log.clear()
        self.install_log.setVisible(True)
        self.result.setText("正在通过 winget 安装…（若系统弹出权限提示，请允许）")
        self.result.setStyleSheet(f"color:{T.TEXT_SUB};")
        self.install_btn.setEnabled(False)
        self.install_btn.setText("正在安装…")
        self.test_btn.setEnabled(False)
        self.choose_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)
        self._worker = InstallWorker(self)
        self._worker.line.connect(self._on_install_line)
        self._worker.done.connect(self._on_install_done)
        self._worker.start()

    def _on_install_line(self, text: str):
        self.install_log.appendPlainText(text)
        bar = self.install_log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_install_done(self, ok: bool, msg: str):
        self._worker = None
        self.install_btn.setEnabled(True)
        self.install_btn.setText("命令安装 FFmpeg")
        self.test_btn.setEnabled(True)
        self.choose_btn.setEnabled(True)
        self.clear_btn.setEnabled(bool(settings.load_ffmpeg_dir()))
        self.install_log.appendPlainText(msg)
        if ok:
            self.result.setText("安装完成，正在重新检测…")
            self.result.setStyleSheet(f"color:{T.OK};")
            # winget 收尾还要几秒才落盘, 稍等一下再查
            QTimer.singleShot(1200, self._after_install)
        else:
            self.result.setText("自动安装没有成功。可点「获取 FFmpeg…」手动下载，"
                                "或用「选择本地 FFmpeg…」指定已有目录。")
            self.result.setStyleSheet(f"color:{T.DANGER};")
            self._refresh()
            self.install_btn.setEnabled(True)

    def _after_install(self):
        self._refresh()
        self._test()          # 装完直接翻绿, 不用用户再点一次「检测」
        self.install_log.setVisible(False)

    def closeEvent(self, event):
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(2500)
            self._worker = None
        super().closeEvent(event)

    def _clear(self):
        settings.save_ffmpeg_dir("")
        if self._mode() == settings.MODE_LOCAL:
            self.result.setText("已清除自定义路径。当前是「仅用本地指定」模式，"
                                "需要重新选择本地 FFmpeg 才能合并。")
        else:
            self.result.setText("已清除自定义路径，改回自动查找。")
        self.result.setStyleSheet(f"color:{T.TEXT};")
        self._refresh()
