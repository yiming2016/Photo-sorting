# -*- coding: utf-8 -*-
"""运行时资源定位：打包后从 _MEIPASS 取, 开发时从 源码/tools 取。"""
from __future__ import annotations

import sys
from pathlib import Path


def app_resource(name: str) -> str:
    """返回打包目录或开发目录下 tools/ 中资源文件的绝对路径。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / name
        if p.exists():
            return str(p)
    # app/resources.py -> parents[1] == 源码
    p = Path(__file__).resolve().parents[1] / "tools" / name
    return str(p)
