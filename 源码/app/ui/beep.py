# -*- coding: utf-8 -*-
"""把 Qt 在 Windows 上那声「嘟」关掉(v220)。

用户听到的零星"嘟嘟"声**不是我们代码在响**: Qt 6 的 `QApplication.beep()` 走平台插件,
`plugins/platforms/qwindows.dll` 里直接调 `user32!MessageBeep`。控件内部会调它 ——
组合框按了匹配不上的字母、微调框输了非数字、快捷键撞车、某些键没人处理……都会响一声,
而且没有任何提示, 所以表现就是"不知道哪来的、偶尔嘟一下"。

Qt 没有"关掉提示音"的开关, 但这些调用都发生在**本进程内**: 把本进程里
`MessageBeep` 的入口改成 `xor eax,eax; ret` 就等于全静音。改动只落在本进程
(Windows 对 DLL 代码页是写时复制, 其它进程不受影响), 且只动这一个函数。

排查时想听原声: 设环境变量 `PHOTOSORT_BEEP=1`。任何一步不顺就静默放弃 ——
宁可不静音, 也不能因为调 API 出问题让程序起不来。
"""
from __future__ import annotations

import os

_DONE = False


def silence() -> bool:
    """把本进程的 `MessageBeep` 变成空函数; 返回是否成功(失败不抛异常)。"""
    global _DONE
    if _DONE:
        return True
    if os.name != "nt" or os.environ.get("PHOTOSORT_BEEP") == "1":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        kernel32 = ctypes.WinDLL("kernel32")
        addr = ctypes.cast(user32.MessageBeep, ctypes.c_void_p).value
        if not addr:
            return False

        kernel32.VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                            wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
        kernel32.VirtualProtect.restype = wintypes.BOOL
        kernel32.FlushInstructionCache.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                   ctypes.c_size_t]
        kernel32.FlushInstructionCache.restype = wintypes.BOOL

        page_execute_readwrite = 0x40
        old = wintypes.DWORD(0)
        if not kernel32.VirtualProtect(ctypes.c_void_p(addr), ctypes.c_size_t(3),
                                       page_execute_readwrite, ctypes.byref(old)):
            return False
        # xor eax,eax; ret  → 直接返回 0, 不出声
        ctypes.memmove(ctypes.c_void_p(addr), b"\x31\xC0\xC3", 3)
        kernel32.VirtualProtect(ctypes.c_void_p(addr), ctypes.c_size_t(3), old,
                                ctypes.byref(wintypes.DWORD(0)))
        kernel32.FlushInstructionCache(ctypes.c_void_p(-1), None, ctypes.c_size_t(0))
        _DONE = True
        return True
    except Exception:
        return False
