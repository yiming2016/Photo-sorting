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
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterable, Sequence

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 行首的 "Error"/"Error:" 才是真失败; "[minor]" 之类的警告行不算。
_WRITE_ERR_RE = re.compile(r"^\s*Error\b", re.MULTILINE)
_WRITE_OK_RE = re.compile(r"(\d+)\s+image files?\s+(updated|created)", re.IGNORECASE)


def _judge_write(text: str) -> bool:
    """按 exiftool 的输出文本判定这次写入是否成功。"""
    if not text:
        return True
    if _WRITE_ERR_RE.search(text):
        return False
    m = _WRITE_OK_RE.search(text)
    if m is not None:
        try:
            return int(m.group(1)) > 0
        except ValueError:
            return True
    # 其余(例如 "Nothing to do.") 不算失败, 由调用方读回校验兜底
    return True


def app_root() -> Path:
    """应用根目录: 打包后为临时解压目录, 开发时为仓库根目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[2]


def _load_settings():
    """惰性导入, 避免 core ← merge 在模块加载期循环。"""
    from ..merge import settings as _s
    return _s


def find_exiftool(mode: str | None = None) -> str | None:
    """优先用户自定义的 exiftool.exe, 其次随程序附带的, 再 PATH / 常见安装目录。

    mode: None=读用户设置; "auto"=优先自定义再自动搜索; "local"=只用自定义。
    """
    s = _load_settings()
    if mode is None:
        mode = s.load_exiftool_mode()
    custom = s.load_exiftool_path()
    if custom and Path(custom).exists():
        return custom
    if mode == s.MODE_LOCAL:
        return None
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


def refresh_exiftool():
    """换过 exiftool 或更新过文件后调用: 关掉常驻进程, 下次用时按新路径重启。"""
    with ExifTool._inst_lock:
        inst = ExifTool._instance
        ExifTool._instance = None
    if inst is not None:
        try:
            inst.close()
        except Exception:
            pass


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
        # ⚠️ 整个启动过程在锁内 + 二次检查 running: execute() 里的
        # `if not self.running: self.start()` 在锁外, 两个线程可能同时看到
        # 未启动而各起一个进程(前一个泄漏)。这里保证只有一个真正启动。
        # self._lock 是 RLock, restart()/execute() 持锁时再进来可重入。
        with self._lock:
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
            # 预热一次, 确保进程真的活着。
            # ⚠️ 必须 _retry=False: 若 exe 一启动就退出(配置错/损坏), 默认 retry 会
            # 走 restart()→start()→execute("-ver")→再 retry ... 无限递归直到栈溢出。
            try:
                self.execute("-ver", _retry=False)
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

    def write(self, path: str, assignments: Sequence[tuple[str, str]],
              in_place: bool = False) -> tuple[bool, str]:
        """写入字段, 返回 (是否成功, 原始输出)。

        v217: 以前调用方拿 `"error" in 输出.lower()` 判成败 —— 只要 exiftool 在
        `[minor]` 警告里带了个 Error 字样就算失败(其实文件已改), 而真正的
        "Error: ..." 行反而可能被 lowercase 后的其它词掩盖。这里按行首判定:
          * 行首 `Error` → 失败;
          * `N image files updated/created` 里 N == 0 → 失败(说明什么都没写);
          * `Nothing to do` → 成功(值本来就对);
          * 其余情况交给调用方的读回校验兜底。
        """
        args: list[str] = []
        for key, value in assignments:
            args.append(f"-{key}={value}")
        args.append("-overwrite_original_in_place" if in_place else "-overwrite_original")
        args.append(path)
        out = self.execute_text(*args)
        err = self.last_error
        text = "\n".join(x for x in (out, err) if x).strip()
        return _judge_write(text), text


def get_exiftool() -> ExifTool:
    with ExifTool._inst_lock:
        if ExifTool._instance is None:
            ExifTool._instance = ExifTool()
        return ExifTool._instance
