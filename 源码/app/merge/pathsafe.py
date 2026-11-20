# -*- coding: utf-8 -*-
"""输出路径的安全校验(合并 / 导出共用)。

为什么必须有这一层:
- 合并走 `-f concat -i list.txt`, 输入是**列表文件**而不是视频本身, 所以
  ffmpeg 自带的"输出文件与输入文件相同"保护完全不生效; 再叠一个 `-y`,
  一旦用户把输出名填成某个源视频的名字, 那个源视频会被**直接截断**,
  读到一半就断了 —— 成品是坏的, 原片也没了。
- 导出同理: 无损路径先切到临时 part, 最后 concat 到 out; ffmpeg 更看不到
  "这个 out 是某个源文件"。转码路径虽然会读源, 但 `-y` 一样先截断。

所以这里在**进 ffmpeg 之前**用路径比较挡一道, 并且:
  * 比较用 normcase(abspath), 兼容大小写 / 相对路径 / 正反斜杠;
  * 不只看原文件, 也看统一格式工作副本(resolve 之后的路径);
  * 顺手禁止把输出写进 `_photosort_norm/`(那是副本目录, 塞进去会被当成
    副本参与解析, 且清理副本时会一起删掉)。
"""
from __future__ import annotations

import os

from . import norm_paths


def norm(path: str) -> str:
    """比较用的规范化路径; 空串返回空串。

    只做 normcase(abspath) —— 绝不 expandvars/expanduser: 用户手输的文件名里
    含 `%TEMP%` / `%USERNAME%` 这类字面文本时, 展开会把两个本不相同的路径
    变得相同(或把相同的变得不同), 让 same_file() 漏判"输出撞素材"而把源片截断。
    """
    try:
        p = os.fspath(path)
    except TypeError:
        return ""
    if not p:
        return ""
    return os.path.normcase(os.path.abspath(p))


def temp_output(out_path: str) -> str:
    """输出的临时落地路径: 与 out_path 同目录(便于 os.replace 原子替换),
    但以点号开头 —— collect_files() 会跳过 dotfile, 不会被当成新素材扫进列表。
    ffmpeg 仍按末尾扩展名选封装格式。
    """
    d = os.path.dirname(os.path.abspath(out_path))
    base = os.path.basename(out_path)
    ext = os.path.splitext(base)[1].lower()
    return os.path.join(d, "." + base + ".photosort_tmp" + ext)


def same_file(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    return bool(na) and na == nb


def in_work_dir(path: str) -> bool:
    """这个路径是不是落在统一副本目录里。"""
    n = norm(path)
    if not n:
        return False
    return os.path.basename(os.path.dirname(n)) == norm_paths.WORK_SUBDIR


def conflict(out_path: str, inputs) -> str:
    """out_path 撞上哪个输入就返回那个输入(原样), 没撞返回 ""。

    inputs 可以是原文件路径, 也可以是 Photo/Clip 之类的对象里取出来的路径;
    这里只认字符串, 调用方自己挑字段。
    """
    for src in inputs or ():
        if not src:
            continue
        if same_file(out_path, src):
            return src
        # 工作副本也算: 覆盖副本会让解析读到坏文件, 且清理逻辑会误删
        try:
            res = norm_paths.resolve(src)
        except Exception:
            res = ""
        if res and same_file(out_path, res):
            return res
    return ""


def check_output(out_path: str, inputs) -> str:
    """返回**给用户看的**错误文本; 空串 = 可以继续。"""
    if not str(out_path or "").strip():
        return "输出文件名为空。"
    hit = conflict(out_path, inputs)
    if hit:
        return (f"输出文件不能和素材同名，否则原文件会被覆盖：\n{hit}\n\n"
                "请换一个输出文件名再试。")
    if in_work_dir(out_path):
        return ("输出文件不能放在统一格式的副本目录（"
                f"{norm_paths.WORK_SUBDIR}）里，\n"
                "那里是自动生成的临时副本，清理时会被一并删除。\n\n"
                "请换一个输出位置再试。")
    return ""
