# -*- coding: utf-8 -*-
"""剪辑态快捷键: 一套固定默认值, 但每个键都能在这张表里直接改。

为什么不是"完全不能改": 有人习惯 J/K/L, 有人嫌 Delete 远。默认给一套顺手的,
想改就在 F1 面板里点那个键、按新键, 冲突会拦下来。改完立刻生效并存盘。

为什么不是"全自定义编辑器": 键就这十几个, 一张能点的表比一个设置页更快。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QFrame,
                               QGridLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from . import theme as T
from .widgets import CheckCheckBox

# (id, 默认键, 说明, 分组, 能否改)
# ⚠️ v157: 原来"滚轮版"是**另起一条动作**（wheel_zoom_in / wheel_seek_next …），
#    和键盘那条并排显示。可 v135 把缩放锚点从鼠标改成裁剪线之后，两边干的事一模
#    一样（只差步长），表里读起来就是"同一个功能写了两遍"。
#    现在一个动作就一条，滚轮录在它的**备用键**里（见 DEFAULT_ALT）—— 一个动作
#    两个槽，本来就是为此设计的；滚轮键位照样能改成别的、也能清掉。
ACTIONS: list[tuple[str, str, str, str, bool]] = [
    # v168 起整张表(快捷键页 + 排序页)都是"按顺序两列一排",不再有独立的
    # 配对表(ROW_PAIRS) —— 顺序挪成什么样, 两页就显示成什么样,不会两张皮。
    # 所以"谁和谁同一行"就是出厂顺序里的相邻两项:
    ("left",         "Left",        "播放头左移 0.5 秒",      "播放与定位", True),
    ("right",        "Right",       "播放头右移 0.5 秒",      "播放与定位", True),
    ("shift_left",   "Shift+Left",  "播放头左移 2 秒",        "播放与定位", True),
    ("shift_right",  "Shift+Right", "播放头右移 2 秒",        "播放与定位", True),
    ("home",         "Home",        "跳到开头",              "播放与定位", True),
    ("end",          "End",         "跳到末尾",              "播放与定位", True),
    ("play",         "Space",       "播放 / 暂停",           "播放与定位", True),
    # 帧相关(v161 归到一组): 逐帧退/进 + 关键帧。都是"画面停在某一帧"的操作,
    # 放一起比原来一半在"播放与定位"、一半在"关键帧"更好找。
    # 关键帧: 照 LosslessCut 的手感(Alt+← / Alt+→ 找关键帧), 因为只有落在关键帧上
    # 才能真的无损下刀 —— 这条比"随便拖"更值得一个键。
    ("prev_frame",   ",",           "退一帧",                "帧", True),
    ("next_frame",   ".",           "进一帧",                "帧", True),
    ("prev_kf",      "Alt+Left",    "上一个关键帧",           "帧", True),
    ("next_kf",      "Alt+Right",   "下一个关键帧",           "帧", True),
    ("snap_kf",      "K",           "最近的关键帧",           "帧", True),
    ("undo",         "Ctrl+Z",      "撤销上一步",            "编辑", True),
    ("redo",         "Ctrl+Y",      "重做",                  "编辑", True),
    ("split",        "S",           "在裁剪线处切开选中片段",  "片段", True),
    ("delete",       "Del",         "删除选中的片段",         "片段", True),
    ("esc",          "Esc",         "取消选中",              "片段", True),
    ("zoom_in",      "Ctrl+=",      "放大时间线",            "视图", True),
    ("zoom_out",     "Ctrl+-",      "缩小时间线",            "视图", True),
    # 平移没有键盘版: 这两条就只有滚轮(Shift+滚轮)。
    ("wheel_pan_left",   "Shift+Wheel+Up",     "向左移（滚轮上）",        "视图", True),
    ("wheel_pan_right",  "Shift+Wheel+Down",   "向右移（滚轮下）",        "视图", True),
    ("fit",          "Ctrl+0",      "全览整条时间线",         "视图", True),
    # 截图 / 封面: 键位照 LosslessCut(C = 捕获快照), 封面在它旁边加个 Shift。
    ("capture_frame", "C",          "截图：当前帧存成图片",     "截图与封面", True),
    ("set_cover",    "Shift+C",     "设为封面：写进视频文件",   "截图与封面", True),
    ("help",         "F1",          "打开这张快捷键表",       "帮助", True),
]

# 出厂就带上的**备用键**: 这些动作本来就该有一个滚轮按法(v157 之前是单列一条)。
# 只给"滚轮上/下各管一件相反的事"的动作配, 方向和动作对应死, 不会互相顶。
DEFAULT_ALT: dict[str, str] = {
    "zoom_in":    "Ctrl+Wheel+Up",
    "zoom_out":   "Ctrl+Wheel+Down",
    "right":      "Alt+Wheel+Up",
    "left":       "Alt+Wheel+Down",
    "next_frame": "Ctrl+Alt+Wheel+Up",
    "prev_frame": "Ctrl+Alt+Wheel+Down",
}

# 旧版把滚轮动作单列成一条; 读盘时并到新动作的备用键上(v157)。
# 不迁的话, 用户自己改过的滚轮键会静悄悄失效。
LEGACY_WHEEL: dict[str, str] = {
    "wheel_zoom_in": "zoom_in",
    "wheel_zoom_out": "zoom_out",
    "wheel_seek_next": "right",
    "wheel_seek_prev": "left",
    "wheel_frame_next": "next_frame",
    "wheel_frame_prev": "prev_frame",
}

# 不进表但要在面板里说明的(鼠标操作, 改不了)
# ⚠️ v169: 「拖片段左右边缘」不再只是一条说明 —— 它能选"裁剪/改倍速"两种
#    干法, 挪去 `_edge_mode_row()` 做成两个可点的按钮了。
FIXED_HINTS = [
    ("点预览画面", "播放 / 暂停"),
    ("点轨道任意位置 / 拖裁剪线", "裁剪线跟过来（靠近素材接缝会自动吸附），中间预览立刻换到那一秒"),
    ("Ctrl + 点片段", "多选；Delete 一次删掉选中的全部"),
    ("Ctrl + 点素材框里的素材", "多选；Delete 把这些移出素材框（磁盘文件不动）"),
    ("拖片段", "自由摆放，可跨轨；选中多个会整组一起挪"),
    ("右键片段 / 轨道空白", "分离音频 / 静音 / 分割 / 删除 / 新增或删除轨道 / 对比 / 发送到已开的对比窗口"),
    ("双击左侧轨名", "重命名这条轨道（回车确认，点空白处 / Esc 放弃）"),
    # v192: 右键里的「重命名」弹窗撤掉了, 两个就地改名入口 + 对比窗口口径
    # v193: 就地改名一律「只有回车确认」，点输入框外面或 Esc 都放弃
    ("双击片段文字带 / 色块", "就地重命名这个片段（回车确认，点空白处 / Esc 放弃；文件后缀不可改）"),
    ("点轨道片段卡片右下小铅笔", "就地重命名这个片段（不弹窗，回车确认；文件后缀不可改）"),
    ("右键「对比」", "选 1 个文件叫「对比」，选多个叫「对比（N个文件）」，都新建一个带编号的对比窗口"),
    ("右键「发送到对比N」", "把选中的文件追加进已经开着的第 N 个对比窗口（不重开窗口）"),
]

# 每个动作最多两个键位: 主键 + 备用键, 按哪个都触发(v140)。
SLOT_COUNT = 2
SLOT_NAMES = ("主键", "备用键")


def row_pairs(aids: list[str]) -> list[tuple[str, ...]]:
    """按顺序两列一排: [a,b,c] → [(a,b),(c,)]。

    v168 起整张表只有这一种排法(以前是 ROW_PAIRS 固定配对):
    排序页怎么挪, 快捷键页就怎么显示, 两页永远一致。
    """
    return [tuple(aids[i:i + 2]) for i in range(0, len(aids), 2)]


# v168 之前的出厂顺序(play 在播放组最前、fit 在视图组中间)。
# 那时"谁和谁同一行"靠 ROW_PAIRS 另配, 顺序本身不影响排版; 现在排版直接由
# 顺序决定, 老用户存盘里那份"没动过的出厂顺序"读进来会把左右移 0.5/2 秒
# 混在一行 —— 所以读盘时认出这份旧出厂顺序就直接丢掉, 换新出厂排版。
_LEGACY_DEFAULT_ORDER = [
    "play", "left", "right", "shift_left", "shift_right", "home", "end",
    "prev_frame", "next_frame", "prev_kf", "next_kf", "snap_kf",
    "undo", "redo", "split", "delete", "esc",
    "zoom_in", "zoom_out", "fit", "wheel_pan_left", "wheel_pan_right",
    "capture_frame", "set_cover", "help",
]


# ---------------------------------------------------------------- 面板排版(v161)
# 用户可以在「排序」模式里挪项、改归属、改名。存盘只存跟出厂不一样的部分,
# 出厂结构永远从 ACTIONS 推出来 —— 以后加动作不用改存盘格式。
_ACTION_GROUP = {a: g for a, _k, _d, g, _c in ACTIONS}
_ACTION_DESC = {a: d for a, _k, d, _g, _c in ACTIONS}


def default_layout() -> dict:
    """默认排版: 没存过「我的默认」就是出厂(组/顺序/名字全按 ACTIONS)。

    ⚠️ v176: 点过「设为默认」之后, 顺序/分组/名字也以存下的那一份为准。
    """
    base = {"order": [a for a, _k, _d, _g, _c in ACTIONS],
            "groups": {}, "names": {}, "group_names": {}}
    try:
        from ..merge import settings
        saved = settings.load_edit_key_layout_custom()
    except Exception:
        saved = None
    if not saved:
        return base
    order = [a for a in (saved.get("order") or []) if a in _ACTION_DESC]
    if order:
        order += [a for a in base["order"] if a not in order]
        base["order"] = order
    for key in ("groups", "names", "group_names"):
        v = saved.get(key)
        if isinstance(v, dict):
            base[key] = {str(k): str(x) for k, x in v.items() if x}
    base["groups"] = {a: g for a, g in base["groups"].items()
                      if a in _ACTION_DESC}
    return base


def load_layout() -> dict:
    """出厂排版 + 用户自定义。

    ⚠️ 盘上的 order 可能缺项(旧存盘 / 以后新加的动作): 缺的按出厂顺序补到最后,
    否则那些动作在面板上直接消失。
    """
    lay = default_layout()
    try:
        from ..merge import settings
        saved = settings.load_edit_key_layout() or {}
    except Exception:
        saved = {}
    order = [a for a in (saved.get("order") or []) if a in _ACTION_DESC]
    if order == _LEGACY_DEFAULT_ORDER:
        order = []          # 旧出厂顺序(排版那时另配), 升级后直接换新出厂排版
    order += [a for a in lay["order"] if a not in order]
    lay["order"] = order
    for key in ("groups", "names", "group_names"):
        v = saved.get(key)
        if isinstance(v, dict):
            lay[key] = {str(k): str(x) for k, x in v.items() if x}
    # groups 里只认还在表里的动作
    lay["groups"] = {a: g for a, g in lay["groups"].items() if a in _ACTION_DESC}
    return lay


def save_layout(lay: dict) -> bool:
    """只存跟出厂不一样的部分。"""
    d = default_layout()
    out = {}
    if list(lay.get("order") or []) != d["order"]:
        out["order"] = list(lay.get("order") or [])
    for key in ("groups", "names", "group_names"):
        if lay.get(key):
            out[key] = dict(lay[key])
    try:
        from ..merge import settings
        return settings.save_edit_key_layout(out)
    except Exception:
        return False


def save_as_default(bindings: dict, layout: dict) -> bool:
    """把**当前这一套**(键位 + 排版)存成「我的默认」(v176)。

    存的是整份快照, 不是"跟出厂的差异" —— 以后出厂表加了新动作, 那些动作
    仍然按新出厂值走(快照里没有的项就落到出厂默认)。

    ⚠️ 存完要顺手把 `edit_keys` / `edit_key_layout` 清空: 那两处存的是
    "跟默认不一样的部分", 默认变了以后原来的差异记录就对不上了(会出现
    "明明点过设为默认, 打开还是老键位")。清空后一切以新默认为基准。
    """
    keys = {k: as_slots(v) for k, v in (bindings or {}).items() if k}
    lay = {"order": list((layout or {}).get("order") or []),
           "groups": dict((layout or {}).get("groups") or {}),
           "names": dict((layout or {}).get("names") or {}),
           "group_names": dict((layout or {}).get("group_names") or {})}
    try:
        from ..merge import settings
        ok1 = settings.save_edit_keys_custom(keys)
        ok2 = settings.save_edit_key_layout_custom(lay)
        if ok1 and ok2:
            # 基准变了: 差异记录作废(否则读回来会叠在旧差异上)
            settings.save_edit_keys({})
            settings.save_edit_key_layout({})
        return bool(ok1 and ok2)
    except Exception:
        return False


def clear_default() -> bool:
    """清掉「我的默认」, 回到真正的出厂(供「全部恢复默认」之外的复位用)。"""
    try:
        from ..merge import settings
        return bool(settings.save_edit_keys_custom(None)
                    and settings.save_edit_key_layout_custom(None))
    except Exception:
        return False


def layout_groups(lay: dict) -> list[tuple[str, str, list[str]]]:
    """排好序的分组: [(组 id, 组显示名, [动作 id...])]。

    组顺序 = 该组**第一个动作**在 order 里的位置; 动作顺序 = order 里的顺序。
    组 id 默认就是出厂组名(改名只改显示, 不改 id, 否则"挪回原组"会分裂成两组)。
    """
    order = list(lay.get("order") or [])
    gmap = lay.get("groups") or {}
    gnames = lay.get("group_names") or {}
    out: list[list] = []
    index: dict[str, int] = {}
    for aid in order:
        gid = gmap.get(aid) or _ACTION_GROUP.get(aid) or "其他"
        if gid not in index:
            index[gid] = len(out)
            out.append([gid, gnames.get(gid, gid), []])
        out[index[gid]][2].append(aid)
    return [(g, n, list(a)) for g, n, a in out]


# 面板一行放两个动作, 说明列只有一百多像素 —— 长句子会把两列挤歪, 所以
# ACTIONS 里写短标签, 细节放这里当悬浮提示(信息一个字没少, 只是不占地方)。
DESC_TIPS = {
    "split": "在裁剪线处切开选中的片段",
    "capture_frame": "截图：把当前这一帧存成图片",
    "set_cover": "设为封面：把当前这一帧写进视频文件",
    "zoom_in": "放大时间线（围着裁剪线放大）",
    "zoom_out": "缩小时间线（围着裁剪线缩小）",
    "left": "播放头后退 0.5 秒（滚轮版是 Alt+滚轮下）",
    "right": "播放头前进 0.5 秒（滚轮版是 Alt+滚轮上）",
    "prev_frame": "退一帧（滚轮版是 Ctrl+Alt+滚轮下）",
    "next_frame": "进一帧（滚轮版是 Ctrl+Alt+滚轮上）",
    "wheel_pan_left": "时间线向左移（滚轮上）",
    "wheel_pan_right": "时间线向右移（滚轮下）",
}

# 键序列 → 面板上显示的样子(QKeySequence 的原生串对方向键不友好)
_DISPLAY = {
    "Space": "空格", "Left": "←", "Right": "→", "Home": "Home", "End": "End",
    "Del": "Delete", "Delete": "Delete", "Esc": "Esc", "Shift+Left": "Shift + ←",
    "Shift+Right": "Shift + →", "Ctrl+=": "Ctrl + ＋", "Ctrl+-": "Ctrl + －",
    "Ctrl+0": "Ctrl + 0",
    "Alt+Left": "Alt + ←", "Alt+Right": "Alt + →",
}


# ---------------------------------------------------------------- 序列语法
# 键盘: "Ctrl+Shift+K" ; 滚轮: "Ctrl+Wheel" / "Ctrl+Wheel+Up" / "Ctrl+Wheel+Down"
WHEEL = "Wheel"
_MOD_ORDER = ("Ctrl", "Alt", "Shift")

# 滚轮往下滚时, 反向动作(没配就还是执行自己)
WHEEL_PAIRS = {
    "zoom_in": "zoom_out", "zoom_out": "zoom_in",
    "left": "right", "right": "left",
    "shift_left": "shift_right", "shift_right": "shift_left",
    "prev_frame": "next_frame", "next_frame": "prev_frame",
    "prev_kf": "next_kf", "next_kf": "prev_kf",
    "home": "end", "end": "home",
}


def parse_seq(seq: str) -> dict:
    """拆成 {mods:set, wheel:bool, dir:str, key:str}。"""
    out = {"mods": set(), "wheel": False, "dir": "", "key": ""}
    for p in (seq or "").split("+"):
        q = p.strip()
        if not q:
            continue
        low = q.lower()
        if low == "ctrl":
            out["mods"].add("Ctrl")
        elif low == "alt":
            out["mods"].add("Alt")
        elif low == "shift":
            out["mods"].add("Shift")
        elif low == "wheel":
            out["wheel"] = True
        elif low in ("up", "down"):
            out["dir"] = "Up" if low == "up" else "Down"
        else:
            out["key"] = q
    return out


def normalize_seq(seq: str) -> str:
    """统一写法(修饰键按 Ctrl/Alt/Shift 排, 便于比较是否同一个组合)。"""
    p = parse_seq(seq)
    parts = [m for m in _MOD_ORDER if m in p["mods"]]
    if p["wheel"]:
        parts.append(WHEEL)
        if p["dir"]:
            parts.append(p["dir"])
    elif p["key"]:
        parts.append(p["key"])
    return "+".join(parts)


def is_wheel_seq(seq: str) -> bool:
    return parse_seq(seq)["wheel"]


def match_wheel(bindings: dict, mods: set, direction: str) -> tuple[str, str] | None:
    """滚轮事件命中哪个动作: 返回 (动作 id, 命中的那一条键位)。

    ⚠️ 必须连**命中的键位**一起返回(v157): 键位带方向(Ctrl+Wheel+Up)就说明
    "上滚只干这一件事", 调用方要直接执行该动作; 键位不带方向(Ctrl+Wheel)才是
    "上滚干这个、下滚干反向那个"。以前只返回 id, 拿不到这个区别, 于是
    「缩小时间线」录了 Ctrl+Wheel+Down 之后往下滚反而执行放大。
    带方向的优先于不分方向的, 同类里按表里的先后取第一条。
    ⚠️ 每个动作有两个槽, 要逐个槽看 —— 只看第一个会漏掉录在备用键里的滚轮。
    """
    order = [a for a, _k, _d, _g, _c in ACTIONS]
    ranked = {a: i for i, a in enumerate(order)}
    best = None
    best_key = None
    for aid, val in (bindings or {}).items():
        for seq in as_slots(val):
            p = parse_seq(seq or "")
            if not p["wheel"] or p["mods"] != set(mods):
                continue
            if p["dir"] and p["dir"] != direction:
                continue
            rank = (0 if p["dir"] else 1, ranked.get(aid, 999))
            if best_key is None or rank < best_key:
                best, best_key = (aid, seq), rank
    return best


def display_key(seq: str) -> str:
    s = (seq or "").strip()
    if not s:
        return "未设置"
    if s in _DISPLAY:
        return _DISPLAY[s]
    p = parse_seq(s)
    if p["wheel"]:
        txt = "滚轮"
        if p["dir"] == "Up":
            txt = "滚轮上"
        elif p["dir"] == "Down":
            txt = "滚轮下"
        mods = [m for m in _MOD_ORDER if m in p["mods"]]
        return " + ".join(mods + [txt]) if mods else txt
    txt = QKeySequence(s).toString(QKeySequence.NativeText) or s
    for a, b in (("Ctrl", "Ctrl"), ("Shift", "Shift"), ("Alt", "Alt")):
        txt = txt.replace(a + "+", b + " + ")
    return txt


def as_slots(val) -> list[str]:
    """把内存/盘上的值统一成 [主键, 备用键]。

    ⚠️ v139 及以前存的是**一个字符串**(`{"play": "Space"}`), 老配置文件直接读进来
    会炸(`for seq in "Space"` 会逐字符遍历)。这里统一兜住, 旧格式自动当成主键。
    """
    if isinstance(val, str):
        return [val.strip(), ""] + [""] * (SLOT_COUNT - 2)
    if isinstance(val, (list, tuple)):
        out = [str(x or "").strip() for x in list(val)[:SLOT_COUNT]]
        while len(out) < SLOT_COUNT:
            out.append("")
        return out
    return [""] * SLOT_COUNT


def default_bindings() -> dict:
    """出厂: 每条动作一个主键 + 该有的备用键。

    ⚠️ v157 起默认就带 `DEFAULT_ALT` 里那几个滚轮键 —— 以前它们是被单列成
    "wheel_zoom_in" 之类独立动作的, 并进来以后不预置就等于"滚轮缩放没了"。
    其余动作的备用键仍然空着(键位是手感, 凭空塞一个反而是干扰)。

    ⚠️ v176: 用户点过「设为默认」后, "默认" = 他存的那一份(键位 + 排版)。
    「全部恢复默认」也恢复到那一份; 没存过才回真正的出厂。
    """
    base = {aid: [key, DEFAULT_ALT.get(aid, "")]
            for aid, key, _d, _g, _c in ACTIONS}
    try:
        from ..merge import settings
        saved = settings.load_edit_keys_custom()
    except Exception:
        saved = None
    if saved:
        for aid, vals in saved.items():
            if aid in base:
                slots = as_slots(vals)
                # ⚠️ v178 修: 同 load_bindings —— 显式清空的槽原样保留,
                # 不回退出厂(否则"我的默认"里清掉的键会被复活)。
                if isinstance(vals, str) or (isinstance(vals, (list, tuple))
                                            and len(vals) < SLOT_COUNT):
                    base[aid] = [slots[0] or base[aid][0],
                                 slots[1] or base[aid][1]]
                else:
                    base[aid] = list(slots)
    return base


def load_bindings() -> dict:
    """出厂默认 + 用户改过的部分(值是 [主键, 备用键])。

    ⚠️ 两处兼容(都在这里做完, 别的地方不用再管):
    1. 旧版单列的滚轮动作(wheel_zoom_in 之类) → 并到对应动作的备用键上;
    2. 存盘里只有一格有值(比如只改了主键) → 另一格**保留出厂值**, 不要清空。
    """
    out = default_bindings()
    try:
        from ..merge import settings
        saved = settings.load_edit_keys() or {}
        # 1) 旧滚轮动作并过来(只在目标那一格还是出厂值时覆盖, 不抢用户新改的)
        for old, new in LEGACY_WHEEL.items():
            if old not in saved or new not in out:
                continue
            slots = as_slots(saved[old])
            val = slots[0] or slots[1]
            if val and out[new][1] == DEFAULT_ALT.get(new, ""):
                out[new][1] = val
        # 2) 用户改过的那部分
        for k, v in saved.items():
            if k in out:
                slots = as_slots(v)
                # ⚠️ v178 修: 用户**显式清空**某一格(空串)必须原样保留,
                # 不能 `or` 回退到出厂值 —— 否则"清掉 A 的键"重载后会被悄悄复活,
                # 之后给 B 录同一个键时还在报"和 A 冲突"。
                # 仅极老的单值/单槽存盘(没有备用键概念)才回退出厂, 保留其余槽。
                if isinstance(v, str) or (isinstance(v, (list, tuple))
                                          and len(v) < SLOT_COUNT):
                    out[k] = [slots[0] or out[k][0], slots[1] or out[k][1]]
                else:
                    out[k] = list(slots)
    except Exception:
        pass
    return out


def save_bindings(mapping: dict) -> bool:
    """只存跟默认不一样的部分。

    ⚠️ v178 修: 即使一个动作**两个槽都清空**(空串)也要存下来 —— 那是用户
    显式清掉了这个动作的键, 必须原样记住。原来写的 `(slots[0] or slots[1])`
    会在"全清空"时把它整个丢出 diff, 等于"清了等于没清"; 重载后回到出厂键,
    之后给别的功能录同一个键就会还报"和它冲突"。
    """
    d = default_bindings()
    diff = {}
    for k, v in (mapping or {}).items():
        slots = as_slots(v)
        if d.get(k) != slots:
            diff[k] = slots
    try:
        from ..merge import settings
        return settings.save_edit_keys(diff)
    except Exception:
        return False


def _mini_btn(text: str, tip: str = "") -> QPushButton:
    """排序模式里的小方按钮(↑ / ↓)。

    ⚠️ 小图标按钮必须 padding:0: 主题给 QPushButton 的左右内边距比这 24px 还宽,
    不写死的话文字区是负的, 只剩一个空框(踩过)。
    """
    b = QPushButton(text)
    b.setFixedSize(24, 24)
    b.setFocusPolicy(Qt.NoFocus)
    b.setCursor(Qt.PointingHandCursor)
    b.setStyleSheet("padding: 0; font-size: 13px;")
    if tip:
        b.setToolTip(tip)
    return b


class _KeyButton(QPushButton):
    """一个键位: 平时显示键名, 点一下进入"等你按键"状态。"""

    def __init__(self, seq: str, parent=None):
        super().__init__(display_key(seq), parent)
        self.seq = seq
        self.setObjectName("keyBtn")
        self.setFocusPolicy(Qt.NoFocus)      # 空格不能落到它头上
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(112)


class _KeyCaptureDialog(QDialog):
    """录入**一格**键位的小窗口(主键或备用键, 由打开它的那个按钮决定)。

    为什么单独开一个窗口而不是就地"按一个键…": 滚轮事件没法定向喂给某个按钮
    (它只落在鼠标底下那个控件上), 开一个专门的大窗口让鼠标停在上面滚,
    才能稳稳录到; 顺带把"按了什么"当场显示出来, 比就地改字更清楚。

    为什么里面只有一块捕获区(v142): 主面板上每个动作已经并排两个按钮了,
    点「主键」就录主键、点「备用键」就录备用键 —— 弹窗里再摆两块,
    反而让人分不清"我现在改的是哪一格"。现在标题写成「主键 · 播放 / 暂停」,
    一眼看全: 改的是哪一格、哪个动作。

    ⚠️ v133 修的关键: 键盘事件**不再依赖焦点**。
    原来这里靠 `grabKeyboard()` + `keyPressEvent`, 结果窗口刚构造完(还没显示)
    就去抓键盘, 抓了个空 —— 滚轮照收(它走鼠标路径), 键盘一个也进不来,
    表现就是"点了没反应, 只有带滚轮的组合能录"。现在改成:
      · 打开期间在 **QApplication 上挂一个事件过滤器**(和焦点无关, 一定收得到);
      · 吃到键盘/滚轮后 `return True` 吞掉, 免得触发剪辑区那些 QShortcut;
      · 关掉窗口立刻摘掉过滤器, 不影响别处。
    """

    def __init__(self, desc: str, current=None, parent=None, slot: int = 0,
                 occupied: dict | None = None):
        super().__init__(parent)
        self._active = int(slot) if int(slot or 0) in range(SLOT_COUNT) else 0
        # 已被占用的键 → 占用者的名字(冲突时红字点名; 不含正在改的这一格)
        self._occupied = dict(occupied or {})
        # current 可以是 [主键, 备用键](主面板传整条), 也可以只给"这一格"的字符串
        if isinstance(current, str):
            vals = [""] * SLOT_COUNT
            vals[self._active] = normalize_seq(current) if current else ""
        else:
            vals = [normalize_seq(s) if s else "" for s in as_slots(current)]
        # 只保留正在录的那一格: 另一格这次不参与, 存空串(免得被误当成"清空另一格")
        self._slots = [""] * SLOT_COUNT
        self._slots[self._active] = vals[self._active]
        self._result = None          # None=取消; 否则=这一格的新键(空串=清掉)
        self._waiting = False
        self._filtering = False
        self._boxes: list[QFrame] = []
        self._bigs: list[QLabel] = []
        self._hints: list[QLabel] = []
        # 标题写「哪一格 · 哪个动作」: 原来一律叫「录入快捷键」,
        # 开着好几个、或者从列表下边点进来时根本不知道自己改的是哪一条。
        name = SLOT_NAMES[self._active] if self._active < len(SLOT_NAMES) else "键位"
        self.setWindowTitle(f"{name} · {desc}" if desc else name)
        self.setModal(True)
        self.setMinimumSize(400, 300)
        self.resize(420, 320)
        # ⚠️ 同主面板: `~枚举` 会顺手把关闭按钮一起清掉, 这里走 int 位运算
        flags = int(self.windowFlags()) | int(Qt.WindowCloseButtonHint)
        flags &= ~int(Qt.WindowContextHelpButtonHint) & 0xFFFFFFFF
        self.setWindowFlags(Qt.WindowFlags(flags))
        self._build()

    # ---------------------------------------------------------------- 构建
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)

        tip = QLabel(f"这一格设的是「{SLOT_NAMES[self._active]}」。"
                     "直接按键，或把鼠标放在框里滚一下滚轮。")
        tip.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
        tip.setWordWrap(True)
        root.addWidget(tip)

        root.addWidget(self._make_box(), 1)

        bar = QHBoxLayout()
        bar.setSpacing(10)
        clear = QPushButton("清除")
        clear.setFocusPolicy(Qt.NoFocus)
        clear.setCursor(Qt.PointingHandCursor)
        clear.setToolTip("把这一格的键位清掉（窗口不关，可以接着录新的）")
        clear.clicked.connect(self._clear)
        bar.addWidget(clear)                     # 居左
        bar.addStretch(1)
        ok = QPushButton("保存")
        ok.setFocusPolicy(Qt.NoFocus)
        ok.setCursor(Qt.PointingHandCursor)
        ok.setProperty("accent", "true")
        ok.setMinimumWidth(88)
        ok.clicked.connect(self._accept)
        self.ok_btn = ok                  # 冲突时要把它点灰(v167)
        bar.addWidget(ok)                        # 居右
        root.addLayout(bar)

    def _make_box(self) -> QFrame:
        """唯一的捕获区: 大字键位 + 小字提示(哪一格已经写在标题里了)。"""
        box = QFrame()
        box.setObjectName("capBox")
        box.setMinimumHeight(120)
        box.setCursor(Qt.PointingHandCursor)
        bl = QVBoxLayout(box)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.setSpacing(2)
        big = QLabel("")
        big.setAlignment(Qt.AlignCenter)
        big.setWordWrap(True)
        hint = QLabel("")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color: {T.TEXT_WEAK}; font-size: 12px; background: transparent;")
        bl.addStretch(1)
        bl.addWidget(big)
        bl.addWidget(hint)
        bl.addStretch(1)
        self._boxes.append(box)
        self._bigs.append(big)
        self._hints.append(hint)
        self._refresh()
        return box

    def _box_qss(self):
        """等输入时边框换成实线主色; 冲突时整框变红 —— 一眼看出"这个不行"。"""
        box = self._boxes[0]
        if self._conflict_of():
            box.setStyleSheet(
                f"QFrame#capBox{{background:{T.DANGER_SOFT};"
                f" border:2px solid {T.DANGER}; border-radius:8px;}}")
        elif self._waiting:
            box.setStyleSheet(
                f"QFrame#capBox{{background:{T.ACCENT_SOFT};"
                f" border:2px solid {T.ACCENT}; border-radius:8px;}}")
        else:
            box.setStyleSheet(
                f"QFrame#capBox{{background:{T.ACCENT_SOFT};"
                f" border:1px dashed {T.BORDER_STRONG}; border-radius:8px;}}")

    def _conflict_of(self) -> str:
        """这一格现在的键跟谁撞了 → 撞了的那一项的名字; 没撞返回空串。"""
        seq = self._slots[self._active]
        if not seq or self._waiting:
            return ""
        return self._occupied.get(seq, "")

    def _hint_text(self) -> str:
        if self._waiting:
            return "正在等输入……按一个键，或滚一下滚轮"
        if self._slots[self._active]:
            return "直接按键替换，或点这里重新录"
        return "点这里录入（可按住 Ctrl / Alt / Shift）"

    def _refresh(self):
        seq = self._slots[self._active]
        hit = self._conflict_of()
        big = self._bigs[0]
        big.setText("请按键…" if self._waiting else display_key(seq))
        if hit:
            # 冲突: 键位大字和提示都改成红字, 点名跟谁撞(v167)
            big.setStyleSheet(
                f"color: {T.DANGER}; font-size: 26px; font-weight: 600;"
                " background: transparent;")
            self._hints[0].setStyleSheet(
                f"color: {T.DANGER}; font-size: 12px; background: transparent;")
            self._hints[0].setText(f"已和「{hit}」冲突，换一个键才能保存")
        else:
            big.setStyleSheet(
                f"color: {T.ACCENT if self._waiting else T.TEXT}; font-size: 26px;"
                f" font-weight: 600; background: transparent;")
            self._hints[0].setStyleSheet(
                f"color: {T.TEXT_WEAK}; font-size: 12px; background: transparent;")
            self._hints[0].setText(self._hint_text())
        self._box_qss()
        # 冲突时「保存」点不了(回车也一样会被 _accept 拦下)
        if getattr(self, "ok_btn", None) is not None:
            self.ok_btn.setEnabled(not hit)

    # ---------------------------------------------------------------- 过滤器
    def showEvent(self, event):
        super().showEvent(event)
        app = QApplication.instance()
        if app is not None and not self._filtering:
            app.installEventFilter(self)
            self._filtering = True
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.OtherFocusReason)

    def _drop_filter(self):
        if not self._filtering:
            return
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except Exception:
                pass
        self._filtering = False

    def _box_at(self, w) -> int:
        """这个控件是不是落在捕获区里(是 → 0; 不是 → -1)。

        只看捕获区, 不能把「清除」「保存」也算进来 —— 那会把按钮的点击一起吞掉。
        ⚠️ 应用级过滤器收到的对象不一定是 QWidget: 鼠标落在窗口边框/标题栏时
        过来的是 QWindow, 它没有 parentWidget() —— 直接调用会抛 AttributeError
        (v133 日志里真出现过, 那时点一下窗口边缘就等于白扔一次事件)。
        """
        while w is not None:
            for i, box in enumerate(self._boxes):
                if w is box:
                    return i
            if w is self:
                return -1
            w = w.parentWidget() if hasattr(w, "parentWidget") else None
        return -1

    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.Type.KeyPress:
            self._on_key(event)
            return True                       # 吞掉: 别让剪辑区的快捷键也跟着响
        if t == QEvent.Type.Wheel:
            self._on_wheel(event)
            return True
        if t == QEvent.Type.MouseButtonPress:
            i = self._box_at(obj)
            if i >= 0 and event.button() == Qt.LeftButton:
                self._begin(i)
                return True
        return False

    # ---------------------------------------------------------------- 录入
    def _set_seq(self, seq: str):
        self._slots[self._active] = normalize_seq(seq)
        self._waiting = False
        self._refresh()

    def _begin(self, _i: int = 0):
        """点捕获区 = 清空它, 重新等输入(不是关窗, 也不是"取消")。"""
        self._waiting = True
        self._slots[self._active] = ""
        self._refresh()

    def _mods_of(self, event) -> list:
        mods = event.modifiers()
        out = []
        if mods & Qt.ControlModifier:
            out.append("Ctrl")
        if mods & Qt.AltModifier:
            out.append("Alt")
        if mods & Qt.ShiftModifier:
            out.append("Shift")
        return out

    def _on_key(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self._cancel()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._accept()
            return
        if key in (Qt.Key_Backspace, Qt.Key_Delete):
            self._set_seq("")               # 只清掉框里的字, 窗口留着
            return
        # 只按修饰键不算一个键
        if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            return
        name = QKeySequence(key).toString()
        if not name:
            return
        self._set_seq("+".join(self._mods_of(event) + [name]))

    def _on_wheel(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.angleDelta().x()      # 触控板横向滚也算
        if delta == 0:
            return
        # 滚轮一律分上下: 录"上滚"就是上滚, 录"下滚"就是下滚
        mods = self._mods_of(event)
        direction = "Up" if delta > 0 else "Down"
        self._set_seq("+".join(list(mods) + [WHEEL, direction]))
        event.accept()

    # 兜底的直投路径(过滤器万一没挂上也不至于"完全没反应")
    def keyPressEvent(self, event):
        self._on_key(event)

    def wheelEvent(self, event):
        self._on_wheel(event)

    # ---------------------------------------------------------------- 收尾
    def result_seq(self):
        """None=取消; 否则这一格的新键(空串 = 清掉了)。"""
        return self._result

    def result_slots(self):
        """兼容旧调用: 返回 [主键, 备用键], 只有正在录的那一格有值。

        ⚠️ 别拿它整条塞回 bindings —— 另一格是空的, 会把人家清掉。
        主窗口现在只认 `result_seq()`。
        """
        if self._result is None:
            return None
        return list(self._slots)

    def _accept(self):
        # 冲突的键**不能被录入**: 点「保存」、按回车都拦在这里, 窗口留着换键
        if self._conflict_of():
            self._refresh()
            return
        self._result = self._slots[self._active]
        self.close()

    def _clear(self):
        """清掉**当前这块**的键位, **窗口不关** —— 清完可以接着录新的。

        真正生效还是要点「保存」: 关掉窗口(=取消)就当没动过。
        """
        self._set_seq("")

    def _cancel(self):
        self._result = None
        self.close()

    def closeEvent(self, event):
        self._drop_filter()
        return super().closeEvent(event)


class ShortcutHelpDialog(QDialog):
    """快捷键一览 + 就地改键。非模态(可边看边操作)。"""

    bindingsChanged = Signal(dict)

    def __init__(self, bindings: dict | None = None, parent=None):
        super().__init__(parent)
        # 值统一成 [主键, 备用键]: 外面可能传进来旧格式(单个字符串)或半截 list
        raw = load_bindings() if bindings is None else bindings
        self.bindings = {k: as_slots(v) for k, v in (raw or {}).items()}
        self._saved = {k: list(v) for k, v in self.bindings.items()}   # 盘上那份
        self._dirty = False
        self._btns: dict[str, list] = {}
        self._desc_labels: list = []      # 每个动作那行说明(验收要量有没有折行)
        # 面板排版(v161): 顺序 / 归属 / 名字都能在「排序」模式里改
        self._layout = load_layout()
        self._saved_layout = self._copy_layout(self._layout)
        self._editing = False             # True = 排序编辑模式
        self._rebuilding = False          # 重建内容区时挡住控件的 editingFinished 回流
        self.setWindowTitle("")
        # 一行两个动作, 比 v140 的"一行一个"矮一大截: 宽度给足(说明才不会折行),
        # 高度反而可以收一点。
        self.setMinimumSize(740, 560)
        self.resize(820, 660)
        # ⚠️ 右上角 ✕ 点不动的真因: 原来写的
        #     `self.windowFlags() & ~Qt.WindowContextHelpButtonHint`
        #   —— PySide6 的 `~枚举` 只按**这个枚举自己的位宽**取反, 得到 0x1feffff,
        #   高位(0x8000000 = WindowCloseButtonHint)全被清掉了。于是窗口变成
        #   "有系统菜单但没有关闭提示", Windows 就把标题栏那个 ✕ 画成**灰的**:
        #   看得见, 点不动。这里改成 int 位运算, 再显式把关闭按钮补回来。
        flags = int(self.windowFlags()) | int(Qt.WindowCloseButtonHint)
        flags &= ~int(Qt.WindowContextHelpButtonHint) & 0xFFFFFFFF
        self.setWindowFlags(Qt.WindowFlags(flags))
        self._build()

    # ---------------------------------------------------------------- 构建
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # 标题那行文字和自绘 × 都不要了(v133): 只留系统标题栏那个 ✕。
        # 空标题 = 看着像一块浮出来的面板, 不再是"又一个窗口"。
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addStretch(1)
        root.addLayout(head)

        self.tip = QLabel("每个动作有两个键位（主键 / 备用键），按哪个都行。"
                          "点哪个键位就只改那一格，按键或滚一下滚轮都行；改完记得点「保存」。")
        self.tip.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
        self.tip.setWordWrap(True)
        root.addWidget(self.tip)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self._body = QWidget()
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(4)
        self._fill_body()
        scroll.setWidget(self._body)
        root.addWidget(scroll, 1)
        self._fit_key_buttons()

        bar = QHBoxLayout()
        bar.setSpacing(10)
        # v176: 「设为默认」在「全部恢复默认」左侧 —— 把当前这套(键位 + 排序)
        # 立成新的默认, 以后「全部恢复默认」回的就是它。
        self.as_default_btn = QPushButton("设为默认")
        self.as_default_btn.setFocusPolicy(Qt.NoFocus)
        self.as_default_btn.setCursor(Qt.PointingHandCursor)
        self.as_default_btn.setToolTip(
            "把当前这套键位和当前这个排序存成「我的默认」。\n"
            "以后点「全部恢复默认」恢复的就是这一套（不存就是出厂那套）")
        self.as_default_btn.clicked.connect(self._save_as_default)
        bar.addWidget(self.as_default_btn)
        self.reset_btn = QPushButton("全部恢复默认")
        self.reset_btn.setFocusPolicy(Qt.NoFocus)
        self.reset_btn.setCursor(Qt.PointingHandCursor)
        self.reset_btn.setToolTip("全部改回出厂键位（还要点「保存」才写盘）")
        self.reset_btn.clicked.connect(self._reset_all)
        bar.addWidget(self.reset_btn)          # 居左
        # 排序(v161): 挪位置 / 换分组 / 改名, 都在这一个按钮里
        self.edit_btn = QPushButton("排序")
        self.edit_btn.setFocusPolicy(Qt.NoFocus)
        self.edit_btn.setCursor(Qt.PointingHandCursor)
        self.edit_btn.setToolTip("进入排序模式：↑ ↓ ← → 挪位置、「移动到」换分组、"
                                 "点名字直接改；改完点「保存」，"
                                 "再点右上角 × 回到快捷键页")
        self.edit_btn.clicked.connect(self._toggle_edit)
        bar.addWidget(self.edit_btn)
        # v177: 「默认开启」在「排序」右侧 —— 选哪些剪辑开关(磁吸/吸附/跟随移动/
        # 进度条/声波)是默认开着的, 进剪辑就按这套来。
        self.feature_btn = QPushButton("默认开启")
        self.feature_btn.setFocusPolicy(Qt.NoFocus)
        self.feature_btn.setCursor(Qt.PointingHandCursor)
        self.feature_btn.setToolTip("选择进入剪辑时默认开启的开关：\n"
                                    "磁吸 / 吸附 / 跟随移动 / 进度条 / 声波")
        self.feature_btn.clicked.connect(self._open_feature_defaults)
        bar.addWidget(self.feature_btn)
        # v180: 「恢复」—— 万一键位被误清空了(比如某个脚本在真机配置上跑过),
        # 写盘前存下的 merge.json.bak 里还有上一套, 点一下就能捞回来。
        self.restore_btn = QPushButton("恢复")
        self.restore_btn.setFocusPolicy(Qt.NoFocus)
        self.restore_btn.setCursor(Qt.PointingHandCursor)
        self.restore_btn.setToolTip(
            "从备份里捞回上一套键位和排序。\n"
            "（每次保存配置前都会自动备份一次，\n"
            "  万一键位被误清空了，点这里就能找回来）")
        self.restore_btn.clicked.connect(self._restore_from_backup)
        self.restore_btn.setVisible(self._has_backup())
        bar.addWidget(self.restore_btn)
        bar.addStretch(1)
        self.save_btn = QPushButton("保存")
        self.save_btn.setFocusPolicy(Qt.NoFocus)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.setProperty("accent", "true")
        self.save_btn.setMinimumWidth(88)
        self.save_btn.clicked.connect(self._save)
        bar.addWidget(self.save_btn)           # 居右
        root.addLayout(bar)
        self._sync_dirty()

    # ---------------------------------------------------------------- 内容区
    @staticmethod
    def _copy_layout(lay: dict) -> dict:
        return {"order": list(lay.get("order") or []),
                "groups": dict(lay.get("groups") or {}),
                "names": dict(lay.get("names") or {}),
                "group_names": dict(lay.get("group_names") or {})}

    def _rebuild(self):
        """重画内容区(排序模式里挪一项就重排一次)。"""
        if self._rebuilding:              # 控件被删时的 editingFinished 不许重入
            return
        self._rebuilding = True
        try:
            self._fill_body()
        finally:
            self._rebuilding = False

    def _clear_body(self):
        while self._body_lay.count():
            item = self._body_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
                continue
            sub = item.layout()
            if sub is not None:
                self._drop_layout(sub)

    def _drop_layout(self, lay):
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            else:
                sub = item.layout()
                if sub is not None:
                    self._drop_layout(sub)

    def _fill_body(self):
        self._btns = {}
        self._desc_labels = []
        self._clear_body()
        lay = self._body_lay
        if self._editing:
            self._fill_edit(lay)
        else:
            self._fill_table(lay)
        lay.addStretch(1)
        self._fit_key_buttons()

    def _fill_table(self, lay):
        """普通模式: 一行两个动作, 全表一个网格(列宽才一致)。"""
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        r = 0
        for gid, gname, aids in layout_groups(self._layout):
            h = QLabel(gname)
            h.setStyleSheet(
                f"color: {T.TEXT_SUB}; font-size: 13px; font-weight: 600;"
                f" padding-top: 8px;")
            grid.addWidget(h, r, 0, 1, 2)
            r += 1
            for row_aids in row_pairs(aids):
                for c, aid in enumerate(row_aids):
                    grid.addLayout(self._cell(aid, self._name_of(aid)), r, c)
                r += 1
        lay.addLayout(grid)
        lay.addSpacing(14)
        h2 = QLabel("鼠标操作")
        h2.setStyleSheet(
            f"color: {T.TEXT_SUB}; font-size: 13px; font-weight: 600;")
        lay.addWidget(h2)
        # v169: 拖片段边缘干什么, 在这里挑(裁剪 / 改变倍率) —— 用户点名的
        lay.addLayout(self._edge_mode_row())
        for i in range(0, len(FIXED_HINTS), 2):
            lay.addLayout(self._pair_hint(FIXED_HINTS[i: i + 2]))

    def _edge_mode_row(self) -> QHBoxLayout:
        """「拖片段左右边缘」= 裁剪首尾 / 改变倍率, 两个按钮二选一(v169)。

        选了立刻存盘生效(时间线那边每次按下都读最新值), 不用点「保存」。
        """
        from ..merge import settings as st
        row = QHBoxLayout()
        row.setSpacing(8)
        a = QLabel("拖片段左右边缘")
        a.setMinimumWidth(112)
        a.setStyleSheet(f"color: {T.TEXT}; font-size: 12px;")
        a.setToolTip("在右边两个里挑一个：拖片段边缘时到底干什么")
        row.addWidget(a, 0)
        self._edge_btns: dict[str, QPushButton] = {}
        try:
            mode = st.load_edge_drag_mode()
        except Exception:
            mode = "trim"
        for m, txt, tip in (
                ("trim", "裁剪首尾", "拖片段左右边缘 = 裁剪首尾（视频自动吸附关键帧）"),
                ("speed", "改变倍率",
                 "拖片段左右边缘 = 拉长/压短来改倍速（内容一点不丢，导出时这段重编码）")):
            b = QPushButton(txt)
            b.setObjectName("edgeModeBtn")
            b.setCheckable(True)
            b.setChecked(mode == m)
            b.setFixedHeight(26)
            b.setMinimumWidth(88)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.setToolTip(tip)
            # v170: 光靠 checked 状态在 QSS 下一点都看不出来(用户: 分不清
            # 选的是哪个) —— 选中 = 主题色实底白字, 没选 = 普通浅底, 一眼可辨
            b.setStyleSheet(
                f"QPushButton#edgeModeBtn {{ padding: 0 12px;"
                f" font-size: 12px; color: {T.TEXT};"
                f" background: {T.SURFACE_ALT};"
                f" border: 1px solid {T.BORDER}; border-radius: 4px; }}"
                f"QPushButton#edgeModeBtn:checked {{ background: {T.ACCENT};"
                f" color: #FFFFFF; border-color: {T.ACCENT};"
                f" font-weight: 600; }}")
            b.clicked.connect(lambda _c=False, mm=m: self._set_edge_mode(mm))
            self._edge_btns[m] = b
            row.addWidget(b)
        row.addStretch(1)
        return row

    def _set_edge_mode(self, mode: str):
        from ..merge import settings as st
        mode = "speed" if mode == "speed" else "trim"
        try:
            st.save_edge_drag_mode(mode)
        except Exception:
            pass
        for m, b in (self._edge_btns or {}).items():
            b.setChecked(m == mode)

    def _fill_edit(self, lay):
        """排序模式(v168 重做): 只管**顺序和分组**, 键位不在这页显示。

        每项 = 名字 + ↑↓←→ + 「移动到」; 和快捷键页同一套"两列一排",
        这页怎么挪、那边就怎么显示。组头是输入框(改组名) + ↑↓(整组上下移,
        v170: 用户点名分组也要能挪)。
        """
        groups = layout_groups(self._layout)
        n_groups = len(groups)
        for gi, (gid, gname, aids) in enumerate(groups):
            # 分组标题: 输入框改组名 + ↑↓ 把整组上下移一格
            hrow = QHBoxLayout()
            hrow.setSpacing(6)
            for text, delta, tip, on in (
                    ("↑", -1, "把这一组往上移一格", gi - 1 >= 0),
                    ("↓", +1, "把这一组往下移一格", gi + 1 < n_groups)):
                b = _mini_btn(text, tip)
                b.setEnabled(on)
                b.setProperty("gid", gid)
                b.clicked.connect(
                    lambda _c=False, g=gid, d=delta: self._move_group(g, d))
                hrow.addWidget(b)
            gedit = QLineEdit(gname)
            gedit.setObjectName("groupName")
            gedit.setFixedWidth(180)
            gedit.setToolTip("分组名（点进来改，回车生效）")
            gedit.editingFinished.connect(
                lambda g=gid, w=gedit: self._rename_group(g, w.text()))
            hrow.addWidget(gedit, 0)
            hrow.addWidget(QLabel(f"共 {len(aids)} 项"),
                         1, Qt.AlignLeft)
            lay.addSpacing(6)
            lay.addLayout(hrow)
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(18)
            grid.setVerticalSpacing(5)
            for r, pair in enumerate(row_pairs(aids)):
                for c, aid in enumerate(pair):
                    i = aids.index(aid)
                    grid.addLayout(
                        self._edit_cell(aid, gid, groups, i, len(aids)), r, c)
            lay.addLayout(grid)

    def _move_group(self, gid: str, delta: int):
        """组头的 ↑↓: 整组在分组顺序里上下移一格(v170)。

        组顺序 = 该组第一个动作在 order 里的位置 → 挪组就是把两组的成员
        在 order 里换成两个连续块(块内顺序不变)。跨组穿插的旧存盘会被
        顺手"块化", 正好也是排版想要的。
        """
        if delta not in (-1, 1):
            return
        groups = layout_groups(self._layout)
        gids = [g[0] for g in groups]
        if gid not in gids:
            return
        i = gids.index(gid)
        j = i + delta
        if not (0 <= j < len(gids)):
            return
        members = {g[0]: g[2] for g in groups}
        seq = list(gids)
        seq[i], seq[j] = seq[j], seq[i]
        seen = set()
        new_order = []
        for g in seq:
            for a in members[g]:
                new_order.append(a)
                seen.add(a)
        for a in self._order():        # 防御: 漏网的补到末尾(理论不会有)
            if a not in seen:
                new_order.append(a)
        self._layout["order"] = new_order
        self._rebuild()
        self._sync_dirty()

    def _edit_cell(self, aid: str, cur_gid: str, groups,
                   i: int, n: int):
        """排序模式里的一格: 名字 + ↑↓←→ + 「移动到」。"""
        cell = QHBoxLayout()
        cell.setSpacing(4)
        ne = QLineEdit(self._name_of(aid))
        ne.setMinimumWidth(110)
        ne.setToolTip("这项显示什么名字（回车生效）")
        ne.editingFinished.connect(
            lambda a=aid, w=ne: self._rename_action(a, w.text()))
        cell.addWidget(ne, 1)
        # ↑↓ 是换一行(两列一排 → 隔一位), ←→ 是和隔壁换位; 到边上的那几个点灰
        for text, delta, tip, on in (
                ("↑", -2, "往上挪一行", i - 2 >= 0),
                ("↓", +2, "往下挪一行", i + 2 < n),
                ("←", -1, "和左边那项换位", i - 1 >= 0),
                ("→", +1, "和右边那项换位", i + 1 < n)):
            b = _mini_btn(text, tip)
            b.setEnabled(on)
            b.setProperty("aid", aid)          # 验收脚本按这个定位按钮
            b.setProperty("delta", delta)
            b.clicked.connect(
                lambda _c=False, a=aid, d=delta: self._move_action(a, d))
            cell.addWidget(b)
        mv = QPushButton("移动到")
        mv.setObjectName("moveBtn")
        mv.setProperty("aid", aid)             # 验收脚本按这个定位按钮
        mv.setFocusPolicy(Qt.NoFocus)
        mv.setCursor(Qt.PointingHandCursor)
        mv.setFixedHeight(24)
        mv.setStyleSheet("padding: 2px 10px; font-size: 12px;")
        mv.setToolTip("把这一项挪到别的分组")
        menu = QMenu(mv)
        for gid2, gname2, _aids2 in groups:
            if gid2 == cur_gid:
                continue
            # v170: 菜单条目直接写组名 —— 按钮自己已经叫「移动到」了,
            # 条目再写一遍"移动到「帧」"就是重复一句话。
            act = menu.addAction(gname2)
            act.triggered.connect(lambda _=False, a=aid, g=gname2:
                                   QTimer.singleShot(
                                       0, lambda a=a, g=g: self._set_group(a, g)))
        menu.addAction("新建分组…").triggered.connect(
            lambda _=False, a=aid: QTimer.singleShot(
                0, lambda a=a: self._new_group_for(a)))
        # ⚠️ 菜单动作里会 _rebuild() 重建内容区, 菜单的宿主按钮也一起被删;
        # 直接在 triggered 里干, menu.exec() 还挂在栈上就等到删除事件 → 崩。
        # 所以一律 singleShot 推到菜单关掉之后再生效。
        mv.clicked.connect(
            lambda _c=False, m=menu, w=mv: m.exec(
                w.mapToGlobal(QPoint(0, w.height()))))
        mv._menu = menu            # 验收脚本要翻菜单里的条目
        cell.addWidget(mv)
        return cell

    def _key_row(self, aid: str):
        row = QHBoxLayout()
        row.setSpacing(4)
        slots = as_slots(self.bindings.get(aid))
        btns = []
        for i in range(SLOT_COUNT):
            b = _KeyButton(slots[i])
            # 一行要放两个动作, 键位块得瘦一点(字号小一档、内边距收窄);
            # 宽度最后统一成同一个值, 否则「说明」长短一差, 两列的按钮就错开。
            b.setStyleSheet("font-size: 11px; padding: 4px 6px;")
            b.setToolTip(f"{SLOT_NAMES[i]}（点一下只改这一格）")
            b.clicked.connect(lambda _c=False, a=aid, s=i: self._begin_capture(a, s))
            btns.append(b)
            row.addWidget(b)
        self._btns[aid] = btns
        return row

    # ---------------------------------------------------------------- 排序模式
    def _toggle_edit(self):
        if self._editing:
            self._exit_edit()
        else:
            self._editing = True
            # v170: 不要「完成」按钮了 —— 退出排序 = 点右上角 ✕
            self.edit_btn.setVisible(False)
            self._set_tip()
            self._rebuild()

    def _exit_edit(self):
        """退出排序模式回到快捷键主列表(v170: 右上角 ✕ 或再点「排序」都行)。"""
        self._editing = False
        self.edit_btn.setText("排序")
        self.edit_btn.setVisible(True)
        self._set_tip()
        self._rebuild()

    def _set_tip(self):
        if self._editing:
            self.tip.setText(
                "排序模式：↑ ↓ ← → 挪位置，分组头上的 ↑ ↓ 移动整组，"
                "「移动到」换分组，点名字直接改。\n"
                "改完点「保存」；点右上角 × 回到快捷键页。")
        else:
            self.tip.setText("每个动作有两个键位（主键 / 备用键），按哪个都行。"
                             "点哪个键位就只改那一格，按键或滚一下滚轮都行；"
                             "改完记得点「保存」。")

    def _order(self) -> list:
        return list(self._layout.get("order") or [])

    def _gname(self, gid: str) -> str:
        return (self._layout.get("group_names") or {}).get(gid) or gid

    def _gid_for_name(self, name: str) -> str:
        """显示名 → 组 id。没见过的名字就当新建一组(组 id 直接用这个名字)。"""
        name = (name or "").strip()
        for gid in self._all_gids():
            if self._gname(gid) == name:
                return gid
        return name

    def _all_gids(self) -> list:
        out = []
        for gid in list(_ACTION_GROUP.values()) + list(self._layout["groups"].values()) \
                + list((self._layout.get("group_names") or {}).keys()):
            if gid not in out:
                out.append(gid)
        return out

    def _move_action(self, aid: str, delta: int):
        """↑↓←→: 在本组里换位置。

        ←→(±1) 和隔壁那项换位; ↑↓(±2) 换到上/下一行(两列一排, 一行隔一项)。
        找人按**组内下标**(组员在全局 order 里并不保证挨着), 换的是全局
        order 里这两位的位置; 挪到边上的按钮本来就是灰的, 不会白点。
        """
        for _gid, _gname, aids in layout_groups(self._layout):
            if aid not in aids:
                continue
            i = aids.index(aid)
            j = i + delta
            if not (0 <= j < len(aids)):
                return
            other = aids[j]
            order = self._order()
            ia, ib = order.index(aid), order.index(other)
            order[ia], order[ib] = order[ib], order[ia]
            self._layout["order"] = order
            self._rebuild()
            self._sync_dirty()
            return

    def _new_group_for(self, aid: str):
        """「移动到 → 新建分组…」: 问个名字, 把这项挪进去。"""
        name, ok = QInputDialog.getText(self, "新建分组", "新分组的名字：")
        name = (name or "").strip()
        if ok and name:
            self._set_group(aid, name)

    def _set_group(self, aid: str, name: str):
        """换分组: 挪到目标组最后一项后面(新建组就落到末尾)。"""
        name = (name or "").strip()
        if not name:
            return
        gid = self._gid_for_name(name)
        cur = (self._layout.get("groups") or {}).get(aid) or _ACTION_GROUP.get(aid)
        if gid == cur:
            # 名字没变(可能只是点开又关上), 顺手把显示名登记一下
            return
        self._layout["groups"][aid] = gid
        order = [a for a in self._order() if a != aid]
        last = -1
        for i, a in enumerate(order):
            g = (self._layout.get("groups") or {}).get(a) or _ACTION_GROUP.get(a)
            if g == gid:
                last = i
        order.insert(last + 1 if last >= 0 else len(order), aid)
        self._layout["order"] = order
        self._rebuild()
        self._sync_dirty()

    def _rename_action(self, aid: str, text: str):
        name = (text or "").strip()
        if not name or name == self._name_of(aid):
            return
        if name == _ACTION_DESC.get(aid):
            self._layout.get("names", {}).pop(aid, None)   # 改回出厂名 = 不留痕迹
        else:
            self._layout["names"][aid] = name
        self._rebuild()
        self._sync_dirty()

    def _rename_group(self, gid: str, text: str):
        name = (text or "").strip()
        if not name or name == self._gname(gid):
            return
        self._layout["group_names"][gid] = name
        self._rebuild()
        self._sync_dirty()

    def _cell(self, aid: str, desc: str):
        """一格 = 说明 + 主键 + 备用键(两块都能点, 点哪块录哪块)。"""
        row = QHBoxLayout()
        row.setSpacing(6)
        d = QLabel(desc)
        d.setStyleSheet(f"color: {T.TEXT}; font-size: 12px;")
        d.setWordWrap(True)                 # 长说明折行, 别撑破格子
        d.setMinimumWidth(84)
        d.setToolTip(DESC_TIPS.get(aid, desc))
        row.addWidget(d, 1)
        self._desc_labels.append(d)
        row.addLayout(self._key_row(aid))
        return row

    def _fit_key_buttons(self):
        """所有键位块统一宽度: 取最宽的那个, 既不切字又竖着对齐。

        ⚠️ 别用 `sizeHint()` 量: 按钮一旦被固定宽度, sizeHint 就跟着变成那个宽度,
        以后再录进更长的组合键量出来还是老值(永远长不大)。按字号自己算最稳。
        """
        wide = 92
        for btns in self._btns.values():
            for b in btns:
                wide = max(wide, b.fontMetrics().horizontalAdvance(b.text()) + 18)
        for btns in self._btns.values():
            for b in btns:
                b.setFixedWidth(int(wide))

    def _pair_hint(self, hints):
        row = QHBoxLayout()
        row.setSpacing(18)
        for k, d in hints:
            cell = QHBoxLayout()
            cell.setSpacing(8)
            a = QLabel(k)
            # v170: 键名给固定宽, 说明列给个下限 —— 否则说明列被压到 100
            # 多像素, 一句话折成四五行的窄柱(截图里看着像排版坏了)。
            a.setFixedWidth(150)
            a.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
            b = QLabel(d)
            b.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
            b.setWordWrap(True)
            b.setMinimumWidth(190)
            cell.addWidget(a, 0)
            cell.addWidget(b, 1)
            row.addLayout(cell, 1)
        if len(hints) < 2:
            row.addStretch(1)
        return row

    # ---------------------------------------------------------------- 保存
    def _sync_dirty(self):
        self._dirty = (self.bindings != self._saved
                       or self._layout != self._saved_layout)
        self.save_btn.setEnabled(self._dirty)
        self.save_btn.setText("保存" if self._dirty else "已保存")

    def _save(self):
        ok_keys = save_bindings(self.bindings)
        ok_lay = save_layout(self._layout)
        if ok_keys and ok_lay:
            self._saved = {k: list(v) for k, v in self.bindings.items()}
            self._saved_layout = self._copy_layout(self._layout)
            self._sync_dirty()
            self.tip.setText("已保存")
        else:
            self.tip.setText("保存失败（配置文件写不进去）")

    # ---------------------------------------------------------------- 改键
    def _begin_capture(self, aid: str, slot: int = 0):
        """点某个键位按钮 → 开录入窗口, **只改这一格**(主键或备用键)。"""
        slot = int(slot) if int(slot or 0) in range(SLOT_COUNT) else 0
        # 谁占着哪些键(不含正在改的这一格): 录入窗口里当场红字点名、拦保存(v167)
        occupied: dict = {}
        for a, vals in self.bindings.items():
            for j, s2 in enumerate(as_slots(vals)):
                s2 = normalize_seq(s2) if str(s2 or "").strip() else ""
                if not s2 or (a == aid and j == slot):
                    continue
                occupied.setdefault(s2, self._desc_of(a))
        dlg = _KeyCaptureDialog(self._desc_of(aid), as_slots(self.bindings.get(aid)),
                                self, slot=slot, occupied=occupied)
        dlg.exec()
        seq = dlg.result_seq()
        if seq is None:
            return                      # 取消: 什么都不改, 也不用再提示一句
        seq = normalize_seq(seq) if str(seq or "").strip() else ""
        # 兜底: 录入窗口已经拦了冲突, 这里再核一遍(比如窗口开着时别处改了键)
        if seq:
            hit = None
            for a, vals in self.bindings.items():
                for j, s2 in enumerate(as_slots(vals)):
                    if a == aid and j == slot:
                        continue
                    if s2 and normalize_seq(s2) == seq:
                        hit = (seq, a)
                        break
                if hit:
                    break
            if hit:
                self.tip.setText(f"「{display_key(hit[0])}」已经被「"
                                 f"{self._desc_of(hit[1])}」用了，换一个")
                return
        vals = as_slots(self.bindings.get(aid))
        if vals[slot] == seq:
            self._sync_dirty()          # 没改, 但按钮文案跟着当前脏/净走
            return                      # 别把「已保存」弄脏
        vals[slot] = seq
        self._set_binding(aid, vals)

    def _set_binding(self, aid: str, slots):
        vals = [normalize_seq(s) if (s or "").strip() else "" for s in as_slots(slots)]
        self.bindings[aid] = vals
        for i, btn in enumerate(self._btns.get(aid, [])):
            btn.seq = vals[i]
            btn.setText(display_key(vals[i]))
        self._fit_key_buttons()          # 键变长了就把整列一起放宽, 别切字
        # 立刻生效(好用不好用当场能试), 但要"保存"才写盘
        self.bindingsChanged.emit(dict(self.bindings))
        self._sync_dirty()

    def _name_of(self, aid: str) -> str:
        """这一项在面板上显示的名字(用户改过就用改过的)。"""
        return (self._layout.get("names") or {}).get(aid) or _ACTION_DESC.get(aid, aid)

    def _desc_of(self, aid: str) -> str:
        """兼容旧调用: 现在一律走 `_name_of`(用户可能改过名)。"""
        return self._name_of(aid)

    def _reload_buttons(self):
        for aid, btns in self._btns.items():
            vals = as_slots(self.bindings.get(aid))
            for i, btn in enumerate(btns):
                btn.seq = vals[i]
                btn.setText(display_key(vals[i]))
        self._fit_key_buttons()

    def _save_as_default(self):
        """把当前键位 + 当前排序立成「我的默认」(v176)。"""
        ok = save_as_default(self.bindings, self._layout)
        if ok:
            # 立完默认: 盘上的"差异记录"已被清空, 这一套就等于新默认, 不脏了
            self._saved = {k: list(v) for k, v in self.bindings.items()}
            self._saved_layout = self._copy_layout(self._layout)
            self._sync_dirty()
            self.tip.setText("已设为默认：以后「全部恢复默认」恢复的就是这一套"
                             "（键位和排序都算）")
        else:
            self.tip.setText("设为默认失败（配置文件写不进去）")

    def _open_feature_defaults(self):
        """「默认开启」: 选哪些剪辑开关默认开着(v177)。

        改的是"进入剪辑时的默认状态", 不影响当前正在看的这次(除非你重新进剪辑)。

        v179 用户定的两处:
        - 勾选框用 ✅ 样式(蓝底 + 白勾)。普通 QCheckBox 在这套 QSS 下只画得出
          一个蓝色实心方块, 根本看不出"到底勾没勾" —— 见 widgets.CheckCheckBox。
        - **不要「取消」按钮**: 勾一个就直接生效(当场存盘), 所以没有"取消"可按;
          右上角 × 或「确定」都是关窗。少一个按钮, 少一次"我到底改没改"的犹豫。
        """
        from ..merge import settings as _st
        fd = _st.load_feature_defaults()
        # 进度条走单独的存盘设置(预览进度条也读它), 这里并进来一起管
        fd["scrub"] = bool(_st.load_scrub_on())

        items = [
            ("magnetic", "磁吸（同轨自动相接）"),
            ("snap", "吸附（拖动自动对齐片段首尾 / 播放头）"),
            # v194: 挤占默认开/关由用户在这页定(出厂关, 不改变原有拖拽手感)
            ("crowd", "挤占（拖到已有片段之间时插入并顺延）"),
            ("follow", "跟随移动（播放时裁剪线钉住、轨道滚）"),
            ("scrub", "进度条（画面下方显示进度）"),
            ("wave", "声波（显示声音波形）"),
        ]
        dlg = QDialog(self)
        dlg.setWindowTitle("默认开启")
        dlg.setMinimumWidth(280)
        dlg.setMaximumWidth(360)
        lay = QVBoxLayout(dlg)
        tip = QLabel("进入剪辑时默认开启这些开关（勾上即刻生效）：")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        checks = {}

        def _save_all():
            """把当前勾选写盘(每个勾都单独存, 不依赖"确定")。"""
            out = {k: bool(c.isChecked()) for k, c in checks.items()}
            _st.save_feature_defaults({k: out[k] for k in
                                       ("magnetic", "snap", "follow", "wave",
                                        "crowd")})
            _st.save_scrub_on(bool(out["scrub"]))

        for key, label in items:
            # ✅ 样式: 蓝底 + 白勾(v179)。普通 QCheckBox 在 QSS 下看不出勾没勾。
            cb = CheckCheckBox(label)
            cb.setChecked(bool(fd.get(key)))
            checks[key] = cb
            lay.addWidget(cb)
            # 勾一下就存盘 —— 所以这个窗口没有"取消"按钮
            cb.toggled.connect(_save_all)
        lay.addStretch(1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        ok = QPushButton("确定")
        ok.setFocusPolicy(Qt.NoFocus)
        ok.setCursor(Qt.PointingHandCursor)
        ok.setProperty("accent", "true")
        ok.setMinimumWidth(88)
        ok.clicked.connect(dlg.accept)
        btns.addWidget(ok)
        lay.addLayout(btns)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            _save_all()          # 兜底再存一次(勾选已经存过, 这里只保底)
            self.tip.setText("已保存「默认开启」设置（下次进入剪辑生效）")

    def _reset_all(self):
        # v176: 恢复到「我的默认」(存过就是那一套, 没存过就是出厂)
        self.bindings = default_bindings()
        # 排版也一起回默认(顺序/归属/名字), 否则"恢复默认"只恢复一半
        self._layout = default_layout()
        self._reload_buttons()
        self._rebuild()
        self.bindingsChanged.emit(dict(self.bindings))
        self._sync_dirty()
        try:
            from ..merge import settings
            has_custom = bool(settings.load_edit_keys_custom())
        except Exception:
            has_custom = False
        self.tip.setText("已恢复到「我的默认」（点「保存」才写盘）" if has_custom
                         else "已全部恢复出厂默认（点「保存」才写盘）")

    # ---------------------------------------------------------------- 恢复(v180)
    def _has_backup(self) -> bool:
        """备份里有没有可用的键位(有才显示「恢复」按钮)。"""
        try:
            from ..merge import settings
            bak = settings.load_config_backup()
        except Exception:
            return False
        keys = bak.get("edit_keys")
        if isinstance(keys, dict) and keys:
            return True
        # "我的默认"那套也在备份里的话也算能恢复
        return bool(bak.get("edit_keys_custom"))

    def _restore_from_backup(self):
        """从备份(merge.json.bak)把上一套键位和排序捞回来。

        什么时候需要: 键位被误清空了(比如某个脚本在真机配置上跑过「设为默认」)。
        每次写配置前都会自动备份一次, 所以上一套还在备份里。
        """
        try:
            from ..merge import settings
            bak = settings.load_config_backup()
        except Exception:
            bak = {}
        keys = bak.get("edit_keys")
        custom = bak.get("edit_keys_custom")
        lay = bak.get("edit_key_layout")
        lay_custom = bak.get("edit_key_layout_custom")
        if not (isinstance(keys, dict) and keys) and not custom:
            self.tip.setText("没有可恢复的备份")
            return
        # 键位: 备份里存的是"跟默认不一样的部分", 套到当前默认上
        try:
            from ..merge import settings as _st
            base = default_bindings()
            if isinstance(custom, dict) and custom:
                base = {k: list(as_slots(v)) for k, v in custom.items()}
            out = {}
            for k, v in base.items():
                out[k] = list(as_slots(v))
            if isinstance(keys, dict):
                for k, v in keys.items():
                    if k in out:
                        out[k] = list(as_slots(v))
            self.bindings = out
            # 排序
            if isinstance(lay_custom, dict) and lay_custom:
                self._layout = self._copy_layout(lay_custom)
            elif isinstance(lay, dict) and lay:
                self._layout = self._copy_layout(lay)
            self._reload_buttons()
            self._rebuild()
            self.bindingsChanged.emit(dict(self.bindings))
            self._sync_dirty()
            self.restore_btn.setVisible(self._has_backup())
            self.tip.setText("已从备份恢复键位（点「保存」才写盘）")
        except Exception as e:
            self.tip.setText(f"恢复失败：{e}")

    def closeEvent(self, event):
        # v170: 排序模式里点右上角 ✕ = 退出排序, 回到快捷键主列表(不是关窗)。
        # 再点一次 ✕ 才是真关窗(那时没保存的话会问要不要保存)。
        if self._editing:
            self._exit_edit()
            event.ignore()
            return
        if self._dirty:
            box = QMessageBox(self)
            box.setWindowTitle("快捷键")
            box.setText("快捷键改了，还没保存。")
            box.setIcon(QMessageBox.Question)
            sv = box.addButton("保存", QMessageBox.AcceptRole)
            box.addButton("不保存", QMessageBox.DestructiveRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.setDefaultButton(sv)
            box.exec()
            clicked = box.clickedButton()
            if clicked is None or box.buttonRole(clicked) == QMessageBox.RejectRole:
                event.ignore()
                return
            if box.buttonRole(clicked) == QMessageBox.AcceptRole:
                self._save()
            else:
                # 不保存: 退回盘上那套(键位 + 排版)
                self.bindings = {k: list(v) for k, v in self._saved.items()}
                self._layout = self._copy_layout(self._saved_layout)
                self._rebuild()
                self._reload_buttons()
                self.bindingsChanged.emit(dict(self.bindings))
                self._sync_dirty()
        return super().closeEvent(event)
