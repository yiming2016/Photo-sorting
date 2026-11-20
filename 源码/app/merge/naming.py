"""命名模板：点输入框弹变量菜单选择变量，也可以直接手动输入。

「新建文件夹」的文件夹命名 / 文件命名、「按组合并」的文件命名三处共用。
变量和普通文字混写（如 旅行_%y%mo%d_%ia），下面一行实时显示示例。

变量分五类（菜单顺序）：常用 / 日期时间 / 增量 / 随机 / 计算机。
日期时间类变量取「拍摄时间」（读不到时退回文件时间）。
"""
from __future__ import annotations

import os
import re
import socket
import uuid
from datetime import datetime
from random import choice, randint
import getpass

# 菜单结构：(类名, [(变量, 面板上显示的中文短名, 完整说明), ...])
# ⚠️ 面板按钮只显示中文短名 —— 用户不该看到 %fn / %ia 这类符号；
#    符号只在插入到输入框之后出现，说明文字放 tooltip。
CATEGORIES = [
    # 「常用」= 最常点的那几个, 面板再矮也能在第一屏看到, 不用滚。
    # ⚠️ 中文短名给用户看: 就是输入框里显示的文字, 也是面板按钮上的文字。
    #    一律用 ≥2 字的词, 避开「年」「月」「日」「时」这种单字 ——
    #    否则用户想写一句普通话(如「秋天的日子」)也会被当变量吃掉。
    ("常用", [
        ("%fn", "原名", "原文件名（不带后缀）"),
        ("%spec", "规格", "视频规格，如 h264_1080p"),
        ("%ia", "序号", "递增序号 01、02…（按总数自动补零）"),
        ("%date", "拍摄日期", "拍摄日期，如 2024-08-15"),
        ("%time", "拍摄时间", "拍摄时间，如 143022"),
        ("%y", "年份", "年份，如 2024"),
        ("%mo", "月份", "月份，如 08"),
        ("%d", "几号", "当月第几天，如 15"),
    ]),
    ("日期时间", [
        ("%y", "年份", "年份，如 2024"),
        ("%yy", "两位数年份", "年份后两位，如 24"),
        ("%mo", "月份", "月份，如 08"),
        ("%mon", "中文月份", "月份中文名，如 八月"),
        ("%mon2", "英文月份", "月份英文名，如 August"),
        ("%w", "星期", "星期几，如 星期四"),
        ("%w2", "英文星期", "英文星期，如 Thursday"),
        ("%wy", "第几周", "本年第几周"),
        ("%d", "几号", "当月第几天，如 15"),
        ("%h", "小时", "小时，如 14"),
        ("%mi", "分钟", "分钟，如 30"),
        ("%s", "秒钟", "秒数，如 22"),
        ("%pm", "上午下午", "上午 / 下午"),
        ("%unix", "时间戳", "Unix 时间戳"),
    ]),
    ("增量", [
        ("%ia", "序号", "递增序号 01、02…（按总数自动补零）"),
        ("%ia3", "三位序号", "递增序号 001、002…"),
        ("%iAa", "字母序号", "递增字母 A、B、C…"),
        ("%ib36", "36进制序号", "三十六进制递增"),
        ("%ix2", "16进制序号", "十六进制递增（2 位）"),
    ]),
    ("随机", [
        ("%rn", "随机数字", "随机一位数字 0~9"),
        ("%rna4", "随机验证码", "4 位不易混淆的字母数字"),
        ("%ra", "随机字母", "随机一个字母"),
        ("%rx4", "随机16进制", "4 位十六进制"),
        ("%guid", "随机GUID", "随机 GUID"),
        ("%radjective", "随机形容词", "随机英文形容词"),
        ("%ranimal", "随机动物", "随机英文动物名"),
        ("%remoji", "随机表情", "随机 emoji 表情"),
    ]),
    ("计算机", [
        ("%un", "用户名", "当前用户名"),
        ("%uln", "登录名", "当前用户登录名"),
        ("%cn", "电脑名", "当前计算机名称"),
    ]),
]

_TOKEN_RE = re.compile(
    r"%(date|time|yy|y|mon2|mon|mo|w2|wy|w|unix|pm|ms|mi|iAa|ia\d*|ib\d*|ix\d*|"
    r"rna\d*|radjective|ranimal|remoji\d*|rx\d*|rn|ra|guid|uln|un|cn|fn|spec|h|d|s)")

_INVALID = re.compile(r'[\\/:*?"<>|]')
_LEADING_INDEX = re.compile(r"^\d{1,3}[_\-\s.、]+(?!\d)")

# 取「拍摄时间」的变量: dt 缺失时整段留空(不能原样吐 %y 进文件名)
_TIME_TOKENS = {"date", "time", "y", "yy", "mo", "mon", "mon2", "w", "w2",
                "wy", "d", "h", "mi", "s", "pm", "unix"}

_MON_CN = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"]
_WEEK_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
_WEEK_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_ADJECTIVES = ["autumn", "brave", "calm", "clear", "cloudy", "cozy", "crimson", "dawn",
               "fancy", "gentle", "golden", "happy", "ivory", "jolly", "lucky", "maple",
               "misty", "noble", "pearl", "quiet", "rapid", "rosy", "sunny", "silver",
               "smooth", "spring", "stellar", "summer", "tidy", "velvet", "warm", "winter"]
_ANIMALS = ["bear", "bird", "cat", "crane", "deer", "dolphin", "eagle", "fox", "horse",
            "koala", "lion", "owl", "panda", "penguin", "rabbit", "seal", "shark",
            "swan", "tiger", "turtle", "whale", "wolf"]
_EMOJI = ["🌈", "🌻", "🍀", "🌊", "🐱", "🐶", "🦋", "⭐", "🌙", "☀️",
          "🍓", "🍁", "🐳", "🦊", "🐧", "🐢", "🦁", "🌸", "🪐", "🎯"]


# 内置命名规则（规则名, 模板）—— 「示例」点开就是这张表，选中即套用。
# 顺序即展示顺序，越靠前越常用。
DEFAULT_PRESETS = [
    ("序号_原名", "%ia_%fn"),
    ("只有序号", "%ia"),
    ("三位序号_原名", "%ia3_%fn"),
    ("原名", "%fn"),
    ("拍摄日期_序号", "%date_%ia"),
    ("年份月份_序号", "%y-%mo_%ia"),
    ("年份月份几号_序号", "%y%mo%d_%ia"),
    ("拍摄日期_拍摄时间", "%date_%time"),
    ("序号_规格", "%ia_%spec"),
    ("分组_序号", "分组_%ia"),
    ("合并_序号", "合并_%ia"),
]


def all_presets() -> list[tuple[str, str]]:
    """内置规则 + 用户自己新增的（排在后面）。"""
    from . import settings as _st
    out = list(DEFAULT_PRESETS)
    seen = {n for n, _t in out}
    for name, tpl in _st.load_name_presets():
        if name not in seen:
            out.append((name, tpl))
            seen.add(name)
    return out


def add_preset(name: str, tpl: str) -> bool:
    """把一条规则存到用户规则里（同名视为覆盖内置前面的显示顺序前提下的新增）。"""
    from . import settings as _st
    name, tpl = (name or "").strip(), (tpl or "").strip()
    if not name or not tpl:
        return False
    items = _st.load_name_presets()
    for i, (n, _t) in enumerate(items):
        if n == name:
            items[i] = (name, tpl)
            return _st.save_name_presets(items)
    items.append((name, tpl))
    return _st.save_name_presets(items)


def _build_cn_maps():
    """变量符号 ↔ 中文短名。中文短名就是输入框里显示给用户看的文字。"""
    tok2cn, cn2tok = {}, {}
    for _title, items in CATEGORIES:
        for token, short, _desc in items:
            tok2cn.setdefault(token, short)
            cn2tok.setdefault(short, token)
    return tok2cn, cn2tok


TOKEN_TO_CN, CN_TO_TOKEN = _build_cn_maps()

# 长名优先, 否则「两位数年份」会被「年份」先吃掉一半
_CN_RE = re.compile("|".join(
    re.escape(k) for k in sorted(CN_TO_TOKEN, key=len, reverse=True)))


def to_display(tpl: str) -> str:
    """内部写法 → 用户看到的中文（%ia → 序号）。"""
    if not tpl:
        return ""
    # 正则捕获的是不含 % 的部分, 查表要补回 % 前缀
    return _TOKEN_RE.sub(
        lambda m: TOKEN_TO_CN.get("%" + m.group(1), m.group(0)), tpl)


def from_display(text: str) -> str:
    """用户看到的中文 → 内部写法（序号 → %ia）。"""
    if not text:
        return ""
    return _CN_RE.sub(lambda m: CN_TO_TOKEN[m.group(0)], text)


def sanitize(s: str) -> str:
    """去掉 Windows 文件名非法字符。"""
    return _INVALID.sub("_", s)


def strip_index(stem: str) -> str:
    """去掉开头序号(01_ / 1- / 02 )，避免移动后变成 01_01_xxx。

    只在「序号后不是数字」时才剥离，否则 2019-08-15、08-15_开场 这类
    日期开头的名字会被误伤。剥完为空返回空串，由调用方退化成纯序号名。
    """
    return _LEADING_INDEX.sub("", stem).strip()


def spec_text(spec: dict) -> str:
    """视频规格简述（用于 %spec）：h264_1080p。"""
    if not spec:
        return ""
    codec = (spec.get("video_codec") or "").lower()
    h = spec.get("height")
    return f"{codec}_{h}p" if codec and h else (codec or "")


def _pad_num(i: int, width: int) -> str:
    return f"{max(0, i):0{max(1, width)}d}"


def _to_base(i: int, base: int) -> str:
    charset = "0123456789abcdefghijklmnopqrstuvwxyz"
    if i <= 0:
        return "0"
    out = []
    while i:
        i, r = divmod(i, base)
        out.append(charset[r])
    return "".join(reversed(out)) or "0"


def _letters(i: int) -> str:
    """1→A, 2→B … 26→Z, 27→AA"""
    n, out = max(0, i - 1), ""
    while True:
        out = chr(65 + n % 26) + out
        n = n // 26 - 1
        if n < 0:
            return out


def _replace(m: re.Match, *, index: int, total: int, name: str,
            dt: datetime | None, spec: str) -> str:
    t = m.group(1)
    if dt is not None:
        if t == "date":
            return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        if t == "time":
            return f"{dt.hour:02d}{dt.minute:02d}{dt.second:02d}"
        if t == "y":
            return dt.strftime("%Y")
        if t == "yy":
            return dt.strftime("%y")
        if t == "mo":
            return f"{dt.month:02d}"
        if t == "mon":
            return f"{_MON_CN[dt.month - 1]}月"
        if t == "mon2":
            return dt.strftime("%B")
        if t == "w":
            return _WEEK_CN[dt.weekday()]
        if t == "w2":
            return _WEEK_EN[dt.weekday()]
        if t == "wy":
            return str(dt.isocalendar()[1])
        if t == "d":
            return f"{dt.day:02d}"
        if t == "h":
            return f"{dt.hour:02d}"
        if t == "mi":
            return f"{dt.minute:02d}"
        if t == "s":
            return f"{dt.second:02d}"
        if t == "pm":
            return "上午" if dt.hour < 12 else "下午"
        if t == "unix":
            return str(int(dt.timestamp()))
    if t in _TIME_TOKENS:
        # 读不到拍摄时间: 宁可这段留空, 也绝不能把 %y 这种字符写进文件名
        return ""
    if t == "ia" or t.startswith("ia") and t[2:].isdigit():
        w = int(t[2:]) if len(t) > 2 else max(2, len(str(max(1, total))))
        return _pad_num(index, w)
    if t == "iAa":
        return _letters(index)
    if t.startswith("ib") and (len(t) == 2 or t[2:].isdigit()):
        base = int(t[2:]) if len(t) > 2 else 10
        return _to_base(max(1, index), min(62, max(2, base)))
    if t.startswith("ix") and (len(t) == 2 or t[2:].isdigit()):
        w = int(t[2:]) if len(t) > 2 else 1
        return f"{max(0, index):0{w}x}"
    if t == "rn":
        return str(randint(0, 9))
    if t.startswith("rna"):
        n = int(t[3:]) if len(t) > 3 else 4
        pool = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
        return "".join(choice(pool) for _ in range(n))
    if t == "ra":
        return choice("abcdefghijklmnopqrstuvwxyz")
    if t.startswith("rx"):
        n = int(t[2:]) if len(t) > 2 else 4
        return "".join(choice("0123456789abcdef") for _ in range(n))
    if t == "guid":
        return str(uuid.uuid4())
    if t == "radjective":
        return choice(_ADJECTIVES)
    if t == "ranimal":
        return choice(_ANIMALS)
    if t.startswith("remoji"):
        n = int(t[6:]) if len(t) > 6 else 1
        return "".join(choice(_EMOJI) for _ in range(n))
    if t == "un":
        try:
            return os.environ.get("USERNAME") or getpass.getuser()
        except Exception:
            return ""
    if t == "uln":
        try:
            return getpass.getuser()
        except Exception:
            return ""
    if t == "cn":
        return socket.gethostname()
    if t == "fn":
        return name or ""
    if t == "spec":
        return spec or ""
    return m.group(0)


def render(template: str, *, index: int, total: int = 1, name: str = "",
           dt: datetime | None = None, spec: str = "") -> str:
    """把模板里的变量替换成真实值，返回清洗后的名字（空模板返回空串）。"""
    if not template:
        return ""
    s = _TOKEN_RE.sub(
        lambda m: _replace(m, index=index, total=total, name=name, dt=dt, spec=spec),
        template)
    return sanitize(s).strip()


def sample(template: str, *, index: int = 1, total: int = 3,
           name: str = "IMG_1234", spec: str = "h264_1080p",
           dt: datetime | None = None) -> str:
    """预览示例：没有真实时间时用固定样例，随机变量也照常出样例。"""
    if dt is None:
        dt = datetime(2024, 8, 15, 14, 30, 22)
    return render(template, index=index, total=total, name=name, dt=dt, spec=spec)
