# -*- coding: utf-8 -*-
"""ExifTool 常驻进程(-stay_open)封装。

三个关键点:
1. 通过 -common_args 固定 UTF-8 文件名与输出编码, 解决中文路径乱码;
2. 每次执行带随机 sentinel 作为结束标记, 避免二进制缩略图与结束符冲突;
3. 单实例 + 可重入锁, 允许后台线程并发调用。
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterable, Sequence

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def app_root() -> Path:
    """应用根目录: 打包后为临时解压目录, 开发时为仓库根目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[2]


def find_exiftool() -> str | None:
    """优先随程序附带的 exiftool, 其次 PATH, 最后常见安装目录。"""
    here = Path(__file__).resolve().parents[2]
    for base in (app_root(), here):
        for rel in ("tools/exiftool.exe", "tools/exiftool"):
            cand = base / rel
            if cand.exists():
                return str(cand)
    found = shutil.which("exiftool") or shutil.which("exiftool.exe")
    if found:
        return found
    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(env_key)
        if not root:
            continue
        for rel in (
            "ExifTool/exiftool.exe",
            "ExifTool/exiftool(-k).exe",
            "Programs/ExifTool/exiftool.exe",
        ):
            cand = Path(root) / rel
            if cand.exists():
                return str(cand)
    win = Path("C:/Windows/exiftool.exe")
    return str(win) if win.exists() else None


class ExifToolError(RuntimeError):
    pass


class ExifTool:
    """线程安全的 ExifTool 常驻进程客户端。"""

    _instance: "ExifTool | None" = None
    _inst_lock = threading.Lock()

    def __init__(self, exe: str | None = None):
        self.exe = exe or find_exiftool()
        if not self.exe:
            raise ExifToolError("未找到 exiftool, 请把 exiftool.exe 放到程序目录下的 tools 文件夹")
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._stderr = bytearray()
        self._stderr_thread: threading.Thread | None = None
        self._common = [
            "-charset", "filename=UTF8",
            "-charset", "UTF8",
            "-api", "largefilesupport=1",
            "-api", "QuickTimeUTC=1",
            "-m",
        ]

    # ---------- 进程生命周期 ----------
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def version(self) -> str:
        try:
            return self.execute_text("-ver")
        except Exception:
            return "unknown"

    def start(self) -> None:
        if self.running:
            return
        startupinfo = None
        kwargs: dict = {}
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            [self.exe, "-stay_open", "True", "-@", "-", "-common_args", *self._common],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )
        self._stderr.clear()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        # 预热一次, 确保进程真的活着
        try:
            self.execute("-ver")
        except Exception as exc:  # pragma: no cover
            self._proc = None
            raise ExifToolError(f"exiftool 启动失败: {exc}") from exc

    def _drain_stderr(self):
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                chunk = proc.stderr.read1(4096)
                if not chunk:
                    break
                self._stderr.extend(chunk)
        except Exception:
            pass

    @property
    def last_error(self) -> str:
        text = bytes(self._stderr).decode("utf-8", errors="replace").strip()
        return text

    def restart(self) -> None:
        with self._lock:
            self.close()
            self.start()

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin:
                proc.stdin.write(b"-stay_open\nFalse\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ---------- 核心执行 ----------
    def execute(self, *args: str, binary: bool = False, _retry: bool = True):
        """执行一条命令, 返回 bytes。"""
        if not self.running:
            self.start()
        args = [a for a in args if a is not None]
        seq = random.randint(100000, 99999999)
        sentinel = f"{{ready{seq}}}".encode("ascii")
        payload = "\n".join(str(a) for a in args) + f"\n-execute{seq}\n"
        with self._lock:
            self._stderr.clear()
            try:
                self._proc.stdin.write(payload.encode("utf-8"))
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError, AttributeError):
                if _retry:
                    self.restart()
                    return self.execute(*args, binary=binary, _retry=False)
                raise ExifToolError("exiftool 进程已断开")
            out = self._proc.stdout
            buf = bytearray()
            # Windows 下 exiftool 用 CRLF, 结束标记后面还跟着 \r\n, 必须一并吃掉
            endings = (sentinel + b"\r\n", sentinel + b"\n", sentinel)
            while True:
                chunk = out.read1(65536) if hasattr(out, "read1") else out.read(65536)
                if not chunk:
                    break
                buf += chunk
                hit = None
                for ending in endings:
                    if buf.endswith(ending):
                        hit = ending
                        break
                if hit is not None:
                    del buf[len(buf) - len(hit):]
                    break
            return bytes(buf)

    def execute_text(self, *args: str) -> str:
        return self.execute(*args).decode("utf-8", errors="replace").strip()

    # ---------- 常用封装 ----------
    def read_json(self, paths: Sequence[str], extra: Sequence[str] | None = None) -> list[dict]:
        """批量读取元数据, 自动按命令行长度分批。"""
        result: list[dict] = []
        batch: list[str] = []
        size = 0
        for p in paths:
            plen = len(p) + 4
            if batch and size + plen > 24000:
                result.extend(self._read_json_batch(batch, extra))
                batch, size = [], 0
            batch.append(p)
            size += plen
        if batch:
            result.extend(self._read_json_batch(batch, extra))
        return result

    def _read_json_batch(self, paths: Sequence[str], extra: Sequence[str] | None) -> list[dict]:
        args = ["-json", "-G", "-s", "-a", "-ee"]
        if extra:
            args.extend(extra)
        args.extend(paths)
        raw = self.execute(*args).decode("utf-8", errors="replace").strip()
        if not raw:
            return []
        # 跳过输出前面的告警之类的杂音, 取最靠前的 JSON 起始符
        start = -1
        for opener in ("[", "{"):
            i = raw.find(opener)
            if i >= 0 and (start < 0 or i < start):
                start = i
        if start > 0:
            raw = raw[start:]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else [data]

    def extract_binary(self, tag: str, path: str) -> bytes:
        """提取内嵌二进制(如缩略图)。"""
        return self.execute("-b", f"-{tag}", path, binary=True)

    def write(self, path: str, assignments: Sequence[tuple[str, str]], in_place: bool = False) -> str:
        """写入字段。assignments 已按调用方要求的顺序排列。"""
        args: list[str] = []
        for key, value in assignments:
            args.append(f"-{key}={value}")
        args.append("-overwrite_original_in_place" if in_place else "-overwrite_original")
        args.append(path)
        out = self.execute_text(*args)
        err = self.last_error
        return "\n".join(x for x in (out, err) if x).strip()


def get_exiftool() -> ExifTool:
    with ExifTool._inst_lock:
        if ExifTool._instance is None:
            ExifTool._instance = ExifTool()
        return ExifTool._instance
