# -*- coding: utf-8 -*-
"""ExifTool 设置对话框: 查看当前版本、选自定义 exe、检测、一键更新、获取、清除自定义。

口径参照 FFmpeg 设置(merge/ffmpeg_settings.py): 使用方式两档 + 路径行 + 检测结果 +
按钮排 + 安装日志。更新源是 oliverbetz.de 的免安装包(和程序自带那份同一个发行渠道,
exiftool.org 官方从 2024 起也用他的启动器), 版本号走他家的 exiftool_latest_version.txt,
拿不到再退回解析 exiftool.org 首页。
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QVBoxLayout)

from ..core import exiftool as et
from ..merge import settings
from ..merge import proc
from . import theme as T

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_DL_BASE = "https://oliverbetz.de/cms/files/Artikel/ExifTool-for-Windows/"
_VER_TXT = _DL_BASE + "exiftool_latest_version.txt"
_HOMEPAGE = "https://exiftool.org/"


def run_version(exe: str):
    """跑 <exe> -ver。返回 (是否成功, 版本号/错误说明)。"""
    if not exe or not Path(exe).exists():
        return False, "（文件不存在）"
    try:
        out = proc.run([exe, "-ver"], timeout=15)
    except Exception as e:
        return False, f"无法运行：{e}"
    first = (out.stdout or out.stderr or "").strip().splitlines()
    if out.returncode == 0 and first:
        return True, first[0].strip()
    return False, f"运行失败（返回码 {out.returncode}）"


def _ver_tuple(s: str):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", str(s))[:4])
    except Exception:
        return ()


def _bits() -> str:
    import platform
    m = (platform.machine() or "").lower()
    return "32" if ("86" in m and "64" not in m) else "64"


class UpdateWorker(QThread):
    """一键更新: 查官方最新版 → 下载 zip → 解压 → 换掉程序用的那份。"""

    line = Signal(str)
    done = Signal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    # ---------------------------------------------------------------- 网络步骤
    def _fetch(self, url: str, timeout: int) -> bytes:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def _latest_version(self) -> str | None:
        try:
            v = self._fetch(_VER_TXT, 20).decode("utf-8", "replace").strip()
            m = re.match(r"^(\d+\.\d+)", v)
            if m:
                return m.group(1)
        except Exception as e:
            self.line.emit(f"版本号文本读取失败（{e}），改读官网首页…")
        try:
            html = self._fetch(_HOMEPAGE, 20).decode("utf-8", "replace")
            m = re.search(r"Version\s+([0-9][0-9.]*)", html)
            if m:
                return m.group(1)
        except Exception as e:
            self.line.emit(f"官网首页也读不到：{e}")
        return None

    # ---------------------------------------------------------------- 安装步骤
    def _target_dir(self) -> Path:
        """更新落到哪: 用户指定过 exe 就更新那份; 否则更新程序自带的 tools 目录。"""
        custom = settings.load_exiftool_path()
        if custom and Path(custom).exists():
            return Path(custom).parent
        return et.app_root() / "tools"

    def _retry(self, op, what: str):
        """带重试的单步操作: 正在跑的 exiftool 会短暂锁文件。"""
        last = None
        for attempt in range(5):
            if self._cancelled:
                raise RuntimeError("已取消")
            try:
                op()
                return
            except OSError as e:
                last = e
                self.line.emit(f"{what}被占用，重试 {attempt + 1}/5…")
                self.msleep(600)
        raise last

    def _kill_holders(self, exe_path: Path):
        """结束跑着这份 exiftool.exe 的残留进程(程序崩溃后可能留下孤儿常驻进程,
        它们锁着文件, 换不了)。只杀这个路径的, 不碰别的程序。"""
        if os.name != "nt":
            return
        exe = str(exe_path).replace("'", "''")
        try:
            p = proc.popen(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | Where-Object { "
                 f"($_.ExecutablePath -eq '{exe}') }} | ForEach-Object {{ "
                 "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p.wait(timeout=15)
            self.msleep(400)
        except Exception:
            pass

    def _install(self, zip_data: bytes, target: Path) -> None:
        src = Path(tempfile.mkdtemp(prefix="exiftool_up_"))
        try:
            with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                z.extractall(src)
            # 包里主程序叫 ExifTool.exe(大小写随版本), 统一落成 exiftool.exe
            names = [n for n in os.listdir(src)
                     if n.lower() in ("exiftool.exe", "exiftool(-k).exe")]
            if not names:
                raise RuntimeError("压缩包里没有找到 ExifTool 主程序")
            new_exe = src / names[0]
            new_files = src / "exiftool_files"
            if not new_files.exists():
                raise RuntimeError("压缩包里没有 exiftool_files 目录")

            # 正在跑的 exiftool 会锁文件: 先关常驻进程, 再重试换文件
            try:
                et.refresh_exiftool()
            except Exception:
                pass
            self._kill_holders(target / "exiftool.exe")

            files_dir = target / "exiftool_files"
            stale = target / "exiftool_files.old"
            if stale.exists():
                shutil.rmtree(stale, ignore_errors=True)
            if files_dir.exists():
                self._retry(lambda: os.rename(files_dir, stale), "旧版本目录")
            self._retry(lambda: shutil.move(str(new_files), str(files_dir)),
                        "新版本文件")
            try:
                self._retry(lambda: os.replace(str(new_exe),
                                               str(target / "exiftool.exe")),
                            "主程序")
            except Exception:
                # 主程序没换成 → 把旧文件目录还原回去, 别留半新半旧
                try:
                    shutil.move(str(files_dir), str(target / "exiftool_files.new"))
                    os.rename(stale, files_dir)
                except Exception:
                    pass
                raise
            finally:
                shutil.rmtree(stale, ignore_errors=True)
                shutil.rmtree(target / "exiftool_files.new", ignore_errors=True)

            # 用户指定过别的名字(如 exiftool(-k).exe) → 指到新的 exiftool.exe 上
            custom = settings.load_exiftool_path()
            if custom:
                settings.save_exiftool_path(str(target / "exiftool.exe"))
        finally:
            shutil.rmtree(src, ignore_errors=True)

    # ---------------------------------------------------------------- 主流程
    def run(self):
        exe = et.find_exiftool()
        cur = "（未安装）"
        if exe:
            ok, v = run_version(exe)
            cur = v if ok else "未知"
        self.line.emit(f"当前版本：{cur}")
        if not exe:
            self.line.emit("当前没找到可用的 ExifTool，将直接下载安装最新版。")

        latest = self._latest_version()
        if not latest:
            self.done.emit(False, "查不到官方最新版本号（网络问题）。"
                                 "可稍后再试，或点「获取 ExifTool…」手动下载。")
            return
        self.line.emit(f"官方最新版：{latest}")

        if exe and _ver_tuple(cur) >= _ver_tuple(latest):
            self.done.emit(True, f"已是最新版本（{cur}），无需更新。")
            return
        if self._cancelled:
            self.done.emit(False, "已取消。")
            return

        try:
            url = f"{_DL_BASE}exiftool-{latest}_{_bits()}.zip"
            self.line.emit(f"下载 {url}")
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                total = int(r.headers.get("Content-Length") or 0)
                chunks, got = [], 0
                while True:
                    if self._cancelled:
                        self.done.emit(False, "已取消。")
                        return
                    b = r.read(64 * 1024)
                    if not b:
                        break
                    chunks.append(b)
                    got += len(b)
                    if total:
                        self.line.emit(f"下载中… {got * 100 // total}%"
                                       f"（{got // 1024} KB / {total // 1024} KB）")
            data = b"".join(chunks)
            self.line.emit(f"下载完成（{len(data) // 1024} KB），正在解压安装…")
        except Exception as e:
            self.done.emit(False, f"下载失败：{e}")
            return

        target = self._target_dir()
        try:
            self._install(data, target)
        except Exception as e:
            self.done.emit(False, f"安装失败：{e}\n（若提示文件被占用，"
                                 "请关掉其他正在用 ExifTool 的程序后重试。）")
            return

        # 换完文件重启常驻进程, 立刻按新版本跑
        try:
            et.refresh_exiftool()
        except Exception:
            pass
        ok, v = run_version(et.find_exiftool() or "")
        if ok:
            self.done.emit(True, f"已更新到 {v}，元数据引擎已用新版重启。")
        else:
            self.done.emit(False, f"文件已替换，但检测失败：{v}")
        try:
            et.get_exiftool()
        except Exception:
            pass


class ExifToolSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ExifTool 设置 · 照片排序台")
        self.setMinimumWidth(620)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        tip = QLabel("读取和修改照片 / 视频的拍摄信息（EXIF 等）靠 ExifTool。"
                     "程序自带一份，一般不用管；也可以换用自己的版本、"
                     "检测能不能跑，或一键更新到官方最新版。")
        tip.setWordWrap(True)
        tip.setProperty("muted", True)
        root.addWidget(tip)

        # ---- 使用方式: 自动(优先本地) / 仅用本地指定 ----
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

        if settings.load_exiftool_mode() == settings.MODE_LOCAL:
            self.local_btn.setChecked(True)
        else:
            self.auto_btn.setChecked(True)
        self.auto_btn.toggled.connect(self._on_mode_changed)
        self.local_btn.toggled.connect(self._on_mode_changed)

        # 路径行 + 选择按钮
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("未找到 ExifTool")
        self.path_edit.setMinimumWidth(300)
        self.choose_btn = QPushButton("选择本地 ExifTool…")
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
        self.update_btn = QPushButton("一键更新")
        self.update_btn.setToolTip("连官方渠道（oliverbetz.de，exiftool.org 官方"
                                   "Windows 包的同一作者）检查并下载最新版，"
                                   "自动替换程序正在用的那份")
        self.update_btn.clicked.connect(self._update)
        self.get_btn = QPushButton("获取 ExifTool…")
        self.get_btn.clicked.connect(self._get)
        self.clear_btn = QPushButton("清除自定义")
        self.clear_btn.clicked.connect(self._clear)
        btn_row.addWidget(self.test_btn)
        btn_row.addWidget(self.update_btn)
        btn_row.addWidget(self.get_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.clear_btn)
        root.addLayout(btn_row)

        # 更新日志(只在更新时出现)
        self.update_log = QPlainTextEdit()
        self.update_log.setReadOnly(True)
        self.update_log.setFixedHeight(140)
        self.update_log.setStyleSheet(
            f"font-family: {T.MONO_FAMILY}; font-size: 11px;"
            f"color: {T.TEXT_SUB}; background: {T.SURFACE_ALT};"
            f"border: 1px solid {T.BORDER}; border-radius: 6px;")
        self.update_log.setVisible(False)
        root.addWidget(self.update_log)

        self._worker = None
        self._refresh()

    # ---------------------------------------------------------------- 逻辑
    def _mode(self) -> str:
        return settings.MODE_LOCAL if self.local_btn.isChecked() else settings.MODE_AUTO

    def _on_mode_changed(self, checked: bool):
        if not checked:
            return
        settings.save_exiftool_mode(self._mode())
        self.result.setText("")
        self._refresh()

    def _set_test_state(self, ok):
        if ok is True:
            bg = T.OK
            self.test_btn.setStyleSheet(
                f"QPushButton{{background:{bg};border:1px solid {bg};"
                f"color:#FFFFFF;font-weight:600;}}"
                f"QPushButton:hover{{background:#1A7E48;border-color:#1A7E48;}}")
            self.test_btn.setToolTip("检测通过：ExifTool 可以正常运行")
        elif ok is False:
            self.test_btn.setStyleSheet(
                f"QPushButton{{color:{T.DANGER};border-color:#F0BEBE;}}"
                f"QPushButton:hover{{background:{T.DANGER_SOFT};}}")
            self.test_btn.setToolTip("检测未通过：ExifTool 无法正常运行")
        else:
            self.test_btn.setStyleSheet("")
            self.test_btn.setToolTip("")

    def _refresh(self):
        mode = self._mode()
        if mode == settings.MODE_LOCAL:
            self.mode_tip.setText("仅用本地：只用你手动指定的 ExifTool，不会自动搜索。")
        else:
            self.mode_tip.setText("自动：优先用本地指定的 ExifTool，没有或失效时"
                                  "用程序自带的那份。")
        exe = et.find_exiftool()
        custom = settings.load_exiftool_path()
        self._set_test_state(None)

        if exe:
            src = ("自定义" if (custom and Path(exe) == Path(custom))
                   else "自动查找")
            self.path_edit.setText(exe)
            self.path_edit.setCursorPosition(0)
            self.path_edit.setToolTip(exe)
            self.path_label.setText(f"当前来源：{src}")
            self.path_label.setStyleSheet(f"color:{T.TEXT_SUB};")
        else:
            self.path_edit.setText("")
            self.path_edit.setToolTip("")
            if mode == settings.MODE_LOCAL:
                self.path_label.setText("当前：没有指定本地 ExifTool（「仅用本地指定」"
                                        "模式下不会自动查找）")
            else:
                self.path_label.setText("当前：未找到 ExifTool")
            self.path_label.setStyleSheet(f"color:{T.DANGER};")
        self.clear_btn.setEnabled(bool(custom))

    def _choose(self):
        start = self.path_edit.text() or os.getcwd()
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 exiftool 可执行文件", start,
            "exiftool (exiftool.exe exiftool(-k).exe ExifTool.exe);;"
            "可执行文件 (*.exe);;所有文件 (*.*)")
        if not path:
            return
        ok, v = run_version(path)
        if not ok:
            self.result.setText(f"⚠ 这个文件跑不起来：{v}\n{path}")
            self.result.setStyleSheet(f"color:{T.DANGER};")
            self._set_test_state(False)
            return
        settings.save_exiftool_path(path)
        et.refresh_exiftool()
        self.result.setText(f"已保存并启用：{path}（版本 {v}）")
        self.result.setStyleSheet(f"color:{T.TEXT};")
        self._refresh()

    def _test(self):
        exe = et.find_exiftool()
        if not exe:
            self.result.setText("未找到 ExifTool，无法检测。")
            self.result.setStyleSheet(f"color:{T.DANGER};")
            self._set_test_state(False)
            return
        ok, v = run_version(exe)
        self.result.setText(f"ExifTool：{v}")
        self.result.setStyleSheet(f"color:{T.OK if ok else T.DANGER};")
        self._set_test_state(ok)

    def _get(self):
        QDesktopServices.openUrl(QUrl("https://exiftool.org/"))

    # ---------------------------------------------------------------- 一键更新
    def _update(self):
        if self._worker is not None:
            return
        ret = QMessageBox.question(
            self, "一键更新 ExifTool",
            "将联网检查官方最新版并自动下载替换（约 13 MB）。\n"
            "更新时元数据引擎会短暂重启，请别同时进行写入操作。\n\n要继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self.update_log.clear()
        self.update_log.setVisible(True)
        self.result.setText("正在检查更新…")
        self.result.setStyleSheet(f"color:{T.TEXT_SUB};")
        self.update_btn.setEnabled(False)
        self.update_btn.setText("更新中…")
        for b in (self.test_btn, self.choose_btn, self.clear_btn,
                  self.auto_btn, self.local_btn):
            b.setEnabled(False)
        self._worker = UpdateWorker(self)
        self._worker.line.connect(self._on_update_line)
        self._worker.done.connect(self._on_update_done)
        self._worker.start()

    def _on_update_line(self, text: str):
        # 下载进度行刷新太快会刷屏, 同前缀的替换掉上一行
        doc = self.update_log.toPlainText().splitlines()
        if doc and text.startswith("下载中") and doc[-1].startswith("下载中"):
            self.update_log.setPlainText("\n".join(doc[:-1] + [text]))
        else:
            self.update_log.appendPlainText(text)
        bar = self.update_log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_update_done(self, ok: bool, msg: str):
        self._worker = None
        self.update_btn.setEnabled(True)
        self.update_btn.setText("一键更新")
        for b in (self.test_btn, self.choose_btn,
                  self.auto_btn, self.local_btn):
            b.setEnabled(True)
        self.clear_btn.setEnabled(bool(settings.load_exiftool_path()))
        self.update_log.appendPlainText(msg)
        self.result.setText(msg.splitlines()[0])
        self.result.setStyleSheet(f"color:{T.OK if ok else T.DANGER};")
        self._refresh()
        if ok:
            # 用新文件跑一遍检测, 让「检测」按钮翻绿(结果文字保留结论)
            exe = et.find_exiftool()
            if exe:
                v_ok, _ = run_version(exe)
                self._set_test_state(v_ok)

    def closeEvent(self, event):
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(4000)
            self._worker = None
        super().closeEvent(event)

    def _clear(self):
        settings.save_exiftool_path("")
        et.refresh_exiftool()
        if self._mode() == settings.MODE_LOCAL:
            self.result.setText("已清除自定义路径。当前是「仅用本地指定」模式，"
                                "需要重新选择本地 ExifTool 才能读写元数据。")
        else:
            self.result.setText("已清除自定义路径，改回用程序自带的那份。")
        self.result.setStyleSheet(f"color:{T.TEXT};")
        self._refresh()
