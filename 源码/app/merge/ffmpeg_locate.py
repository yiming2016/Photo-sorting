# -*- coding: utf-8 -*-
"""定位 ffmpeg / ffprobe。

查找顺序:
  1) 软件内置: <app根>/tools/ffmpeg(.exe) 与 ffprobe(.exe)
     (打包后为 软件/_internal/tools/ ; 开发时为 源码/tools/ )
  2) 常见扁平目录: ProgramFiles/ffmpeg/bin、LOCALAPPDATA/ffmpeg/bin、C:/ffmpeg/bin
  3) WinGet 安装: LOCALAPPDATA/Microsoft/WinGet/Packages/<pkg>/ffmpeg-*/bin
     (本机最常通过 winget 安装 ffmpeg, 路径非常规, 必须特殊处理)
  4) 有限深度递归兜底: 在 ProgramFiles / LOCALAPPDATA 下做深度<=7 的扫描
  5) 系统 PATH
ffmpeg 与 ffprobe 尽量成对匹配(同源), 避免混用不同版本导致异常。
"""
import os
import shutil
from pathlib import Path


def _bundle_dir() -> Path:
    # 本文件位于 app/merge/ffmpeg_locate.py -> parents[2] 即 app 根(源码 或 _internal)
    return Path(__file__).resolve().parents[2] / "tools"


def _common_flat_dirs():
    dirs = []
    if os.name == "nt":
        for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(v)
            if base:
                dirs.append(Path(base) / "ffmpeg" / "bin")
        dirs.append(Path("C:/ffmpeg/bin"))
    else:
        dirs.append(Path("/usr/bin"))
        dirs.append(Path("/usr/local/bin"))
        dirs.append(Path.home() / "ffmpeg" / "bin")
    return [d for d in dirs if d]


def _winget_dirs():
    """WinGet 安装的 ffmpeg: LOCALAPPDATA/Microsoft/WinGet/Packages/<pkg>/ffmpeg-*/bin。"""
    la = os.environ.get("LOCALAPPDATA", "")
    if not la:
        return []
    wg = Path(la) / "Microsoft" / "WinGet" / "Packages"
    out = []
    if not wg.is_dir():
        return out
    try:
        for pkg in os.scandir(wg):
            if not pkg.is_dir():
                continue
            try:
                for sub in os.scandir(pkg.path):
                    if sub.is_dir() and sub.name.lower().startswith("ffmpeg"):
                        binp = Path(sub.path) / "bin"
                        if binp.is_dir():
                            out.append(binp)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _recursive_dirs(name: str):
    """在 ProgramFiles / LOCALAPPDATA 下做有限深度(<=7)递归, 兜底非常规安装。"""
    if os.name != "nt":
        return []
    ext = ".exe"
    target = f"{name}{ext}"
    roots = []
    for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(v)
        if base:
            roots.append(Path(base))
    out = []
    for root in roots:
        if not root.is_dir():
            continue
        stack = [(root, 0)]
        while stack:
            d, depth = stack.pop()
            if depth >= 7:
                continue
            try:
                entries = list(os.scandir(d))
            except Exception:
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if len(out) < 16:
                            stack.append((Path(e.path), depth + 1))
                    elif e.name.lower() == target.lower():
                        out.append(Path(e.path).parent)
                        if len(out) >= 16:
                            return out
                except Exception:
                    continue
    return out


def _which(name: str):
    return shutil.which(name) or shutil.which(name + ".exe")


def _find_one(name: str, prefer_dir: str | None = None) -> str | None:
    ext = ".exe" if os.name == "nt" else ""
    target = f"{name}{ext}"
    # 1) 与成对伙伴同目录优先(保证 ffmpeg / ffprobe 同源)
    if prefer_dir:
        c = Path(prefer_dir) / target
        if c.exists():
            return str(c)
    # 2) 内置 tools/
    cand = _bundle_dir() / target
    if cand.exists():
        return str(cand)
    # 3) 常见扁平目录
    for d in _common_flat_dirs():
        c = d / target
        if c.exists():
            return str(c)
    # 4) WinGet 安装
    for d in _winget_dirs():
        c = d / target
        if c.exists():
            return str(c)
    # 5) 有限深度递归兜底
    for d in _recursive_dirs(name):
        c = d / target
        if c.exists():
            return str(c)
    # 6) PATH
    return _which(name)


def find_ffmpeg_pair(mode: str | None = None) -> tuple[str, str] | None:
    """返回 (ffmpeg路径, ffprobe路径); 任一缺失则返回 None。

    mode: "auto"(默认) = 先用手动指定的本地目录, 找不到再自动搜索;
          "local"      = 只认手动指定的本地目录, 没有就直接判定不可用。
    """
    try:
        from . import settings
        user_dir = settings.load_ffmpeg_dir()
        if mode is None:
            mode = settings.load_ffmpeg_mode()
    except Exception:
        user_dir, mode = None, "auto"
    if user_dir:
        ext = ".exe" if os.name == "nt" else ""
        ff = Path(user_dir) / f"ffmpeg{ext}"
        fp = Path(user_dir) / f"ffprobe{ext}"
        if ff.exists() and fp.exists():
            return str(ff), str(fp)
    if str(mode).lower() == "local":
        return None

    ff = _find_one("ffmpeg")
    if not ff:
        return None
    # ffprobe 优先取与 ffmpeg 同目录, 保证版本一致
    fp = _find_one("ffprobe", prefer_dir=str(Path(ff).parent))
    if ff and fp:
        return ff, fp
    return None
