# -*- coding: utf-8 -*-
"""崩溃日志: 出事之后能知道"死在哪"。

为什么需要:
- 打包成 --windowed 的 exe 没有控制台, stderr 是 None, Qt 的致命消息(qFatal)
  和 Python 的异常全都看不见 —— 用户只看到"点一下窗口就没了";
- 这次闪退(Win 事件日志: Qt6Core.dll / 0xc0000409)就是 Qt 内部 abort,
  报错文字全被吞掉了, 只能靠猜。

做法:
1. `qInstallMessageHandler` 把所有 Qt 消息(含 Fatal)写进日志文件;
2. `faulthandler` 抓原生崩溃(访问越界/abort), 会打印**出事那一刻 每个线程的 Python 栈**;
3. `sys.excepthook` / `threading.excepthook` 记未捕获的 Python 异常。

日志位置: 程序数据目录下 logs/app.log(超过 2MB 就轮转一份 app.old.log)。
"""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback

_state = {"file": None, "path": ""}
MAX_BYTES = 2 * 1024 * 1024


def log_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "照片排序台", "logs")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        path = os.path.join(os.path.expanduser("~"), ".照片排序台_logs")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            return ""
    return path


def log_path() -> str:
    d = log_dir()
    return os.path.join(d, "app.log") if d else ""


def _open():
    path = log_path()
    if not path:
        return None
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_BYTES:
            old = path.replace("app.log", "app.old.log")
            try:
                os.replace(path, old)
            except OSError:
                pass
        fp = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return None
    fp.write("\n===== %s 启动 =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    fp.flush()
    return fp


def write(text: str) -> None:
    """随手记一行(工具/导出这类关键动作可以用)。"""
    fp = _state.get("file")
    if fp is None:
        return
    try:
        fp.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), text))
        fp.flush()
    except Exception:
        pass


def install() -> str:
    """装上全部钩子; 返回日志路径(拿不到就返回空串)。"""
    if _state["file"] is not None:
        return _state["path"]
    fp = _open()
    if fp is None:
        return ""
    _state["file"] = fp
    _state["path"] = log_path()

    # 1) 原生崩溃(访问越界 / abort / qFatal) → 每个线程的 Python 栈
    try:
        faulthandler.enable(file=fp, all_threads=True)
    except Exception:
        pass

    # 2) Qt 自己的消息(致命消息在 abort 之前会走到这里)
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        names = {
            QtMsgType.QtDebugMsg: "调试",
            QtMsgType.QtInfoMsg: "信息",
            QtMsgType.QtWarningMsg: "警告",
            QtMsgType.QtCriticalMsg: "严重",
            QtMsgType.QtFatalMsg: "致命",
        }

        def handler(mode, context, message):
            try:
                tag = names.get(mode, str(mode))
                where = ""
                try:
                    if context is not None and context.file:
                        where = " (%s:%s)" % (context.file, context.line)
                except Exception:
                    where = ""
                fp.write("[%s] Qt%s: %s%s\n"
                         % (time.strftime("%H:%M:%S"), tag, message, where))
                fp.flush()
            except Exception:
                pass

        qInstallMessageHandler(handler)
    except Exception:
        pass

    # 3) 未捕获异常
    def _hook(exc_type, exc, tb):
        try:
            fp.write("[%s] 未捕获异常\n" % time.strftime("%H:%M:%S"))
            fp.write("".join(traceback.format_exception(exc_type, exc, tb)))
            fp.flush()
        except Exception:
            pass

    sys.excepthook = _hook

    def _thread_hook(args):
        try:
            fp.write("[%s] 线程里未捕获异常 (%s)\n"
                     % (time.strftime("%H:%M:%S"),
                        getattr(args.thread, "name", "?")))
            fp.write("".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback)))
            fp.flush()
        except Exception:
            pass

    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass

    write("版本 %s" % _version())
    return _state["path"]


def _version() -> str:
    try:
        from . import __version__

        return str(__version__)
    except Exception:
        return "?"
