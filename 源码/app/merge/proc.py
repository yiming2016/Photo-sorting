# -*- coding: utf-8 -*-
"""统一封装 ffmpeg/ffprobe 的子进程调用。

两个必须解决的问题:
1) **编码**: 中文版 Windows 上 `text=True` 会按 GBK 解码子进程输出, 而 ffmpeg/ffprobe
   输出的是 UTF-8(且 `-show_format` 的 json 里就含中文文件名) → 读取线程抛
   UnicodeDecodeError → stdout 变空、returncode 仍是 0, 表现为"ffprobe 返回码 0"。
   所以这里一律 `encoding="utf-8", errors="replace"` 自己解码。
2) **黑窗闪一下**: 打包成 --windowed 后本进程没有控制台, 启动控制台程序(ffmpeg)
   时 Windows 会为它新分配一个控制台窗口 → 用户看到 cmd 窗口一闪而过。
   用 CREATE_NO_WINDOW + 隐藏的 STARTUPINFO 消除。
"""
from __future__ import annotations

import os
import subprocess


def _no_window_kwargs() -> dict:
    """让子进程不弹黑窗。非 Windows 返回空 dict。"""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    flags = 0
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return {"startupinfo": si, "creationflags": flags}


def run(cmd: list, timeout: float = 120, cwd: str | None = None) -> subprocess.CompletedProcess:
    """运行命令, 捕获输出(UTF-8 解码, 不弹黑窗)。异常交调用方处理。"""
    kw = dict(capture_output=True, stdin=subprocess.DEVNULL,
              encoding="utf-8", errors="replace", timeout=timeout)
    kw.update(_no_window_kwargs())
    if cwd:
        kw["cwd"] = cwd
    return subprocess.run(cmd, **kw)


def run_bytes(cmd: list, timeout: float = 300) -> subprocess.CompletedProcess:
    """同上, 但 stdout 保持 bytes(取 PCM 波形这类二进制输出时用)。"""
    kw = dict(capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout)
    kw.update(_no_window_kwargs())
    return subprocess.run(cmd, **kw)


def popen(cmd: list, stdout=None, stderr=None, stdin=subprocess.DEVNULL,
          cwd: str | None = None) -> subprocess.Popen:
    """以"无黑窗"方式启动命令并返回 Popen, 供需要边跑边读输出的场景(合并进度)使用。

    stdout 建议传 subprocess.PIPE 按行读进度; stderr 建议重定向到文件,
    避免 stderr 管道写满后 ffmpeg 阻塞(既是死锁隐患, 也读不到进度)。
    """
    kw = dict(stdout=stdout, stderr=stderr, stdin=stdin)
    kw.update(_no_window_kwargs())
    if cwd:
        kw["cwd"] = cwd
    return subprocess.Popen(cmd, **kw)
