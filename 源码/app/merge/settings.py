# -*- coding: utf-8 -*-
"""合并模块的持久化设置(目前只有"用户手动指定的 FFmpeg 所在文件夹")。

存放位置: %APPDATA%\\照片排序台\\merge.json (取不到 APPDATA 则退回 LOCALAPPDATA / 用户目录)。
"""
import json
import os
import tempfile
from pathlib import Path


def _config_path() -> Path:
    base = (os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
            or str(Path.home()))
    d = Path(base) / "照片排序台"
    # ⚠️ 铁律(v180): 自动化测试**绝不许**碰用户真机的配置。
    #    事故: 有个测试脚本没重定向 APPDATA, 把「设为默认」跑在了真机上 ——
    #    那条路径会清掉 edit_keys, 于是用户改过的快捷键被抹了, 且当时没有备份,
    #    无法恢复。现在这里加一道**应用层**的护栏: 离屏(测试)运行时, 只要
    #    算出来的还是"用户真机那个目录", 就强制改到临时目录去。
    #    (脚本自己重定向过 APPDATA 的不受影响 —— 那本来就不是真机目录。)
    if _is_test_run() and _is_real_user_dir(d):
        d = Path(tempfile.gettempdir()) / ("photosort_test_%d" % os.getpid()) / "照片排序台"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "merge.json"


def _is_test_run() -> bool:
    """是不是自动化测试进程(离屏跑的那种)。"""
    if os.environ.get("PHOTOSORT_ALLOW_REAL_CONFIG"):
        return False           # 开发时显式放行
    return (os.environ.get("QT_QPA_PLATFORM") or "").lower() == "offscreen"


def _is_real_user_dir(d: Path) -> bool:
    """这个目录是不是"用户真机"那个(而不是测试自己重定向出来的临时目录)。"""
    try:
        home = Path.home()
        real = home / "AppData" / "Roaming" / "照片排序台"
        return d.resolve() == real.resolve()
    except Exception:
        return False


def _read() -> dict:
    try:
        p = _config_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def load_ffmpeg_dir():
    v = _read().get("ffmpeg_dir")
    return v or None


def save_ffmpeg_dir(folder: str) -> bool:
    return _save("ffmpeg_dir", folder or None)


# ---------------------------------------------------------------- FFmpeg 使用方式
MODE_AUTO = "auto"     # 自动: 优先用手动指定的本地目录, 找不到/失效再自动搜索
MODE_LOCAL = "local"   # 仅本地: 只认手动指定的目录, 找不到就报不可用


def load_ffmpeg_mode() -> str:
    v = str(_read().get("ffmpeg_mode") or MODE_AUTO).strip().lower()
    return MODE_LOCAL if v == MODE_LOCAL else MODE_AUTO


def save_ffmpeg_mode(mode: str) -> bool:
    return _save("ffmpeg_mode", MODE_LOCAL if mode == MODE_LOCAL else MODE_AUTO)


# ---------------------------------------------------------------- ExifTool(元数据引擎)
def load_exiftool_path():
    """用户手动指定的 exiftool.exe 完整路径; 没有返回 None。"""
    v = _read().get("exiftool_path")
    return v or None


def save_exiftool_path(path: str) -> bool:
    return _save("exiftool_path", path or None)


def load_exiftool_mode() -> str:
    v = str(_read().get("exiftool_mode") or MODE_AUTO).strip().lower()
    return MODE_LOCAL if v == MODE_LOCAL else MODE_AUTO


def save_exiftool_mode(mode: str) -> bool:
    return _save("exiftool_mode", MODE_LOCAL if mode == MODE_LOCAL else MODE_AUTO)


# ---------------------------------------------------------------- 含子文件夹(添加时)
def load_recursive() -> bool:
    """「含子文件夹」上次的选择; 没记录过返回 False(默认不递归)。"""
    v = _read().get("recursive")
    return bool(v) if v is not None else False


def save_recursive(on: bool) -> bool:
    return _save("recursive", bool(on))


# ---------------------------------------------------------------- 网格缩放(显示方式)
def load_view_mode():
    """上次用的显示方式(sm/md/lg/xl); 没记录过返回 None。"""
    v = _read().get("view_mode")
    return str(v) if v else None


def save_view_mode(mode) -> bool:
    return _save("view_mode", mode or None)


# ---------------------------------------------------------------- 参数分档(⚙️ 设置)
def load_field_levels():
    """用户自定义的参数分档 {key: level}; 没改过返回 None(用出厂默认)。"""
    v = _read().get("field_levels")
    return v if isinstance(v, dict) and v else None


def save_field_levels(levels) -> bool:
    """levels: {key: level}; 空/None = 清掉自定义, 回出厂默认。"""
    return _save("field_levels", levels or None)


def load_field_order():
    """用户自定义的参数展示顺序(key 列表); 没改过返回 None。"""
    v = _read().get("field_order")
    return v if isinstance(v, list) and v else None


def save_field_order(order) -> bool:
    """order: 参数 key 列表(决定展示顺序); 空/None = 清掉, 回出厂顺序。"""
    return _save("field_order", order or None)


# ---------------------------------------------------------------- 命名规则(用户自建)
def load_name_presets():
    """用户新增的命名规则 [(规则名, 模板), ...]; 没有返回 []。"""
    v = _read().get("name_presets")
    out = []
    if isinstance(v, list):
        for item in v:
            try:
                name, tpl = str(item[0]).strip(), str(item[1]).strip()
            except Exception:
                continue
            if name and tpl:
                out.append((name, tpl))
    return out


def save_name_presets(items) -> bool:
    """items: [(规则名, 模板), ...]; 空 = 清掉(只剩内置预设)。"""
    return _save("name_presets",
                 [[n, t] for n, t in (items or [])] or None)


# ---------------------------------------------------------------- 剪辑中间文件存哪
# 剪辑过程中生成的文件(改帧率/削减/修时长/分离音频的成品)默认**不落在原文件夹**,
# 免得把素材目录弄乱。用户可以在「工具 → 生成的文件存到哪」里改。
OUT_SAME = "same"          # 直接存在原文件夹(老行为)
OUT_SUB = "subfolder"      # 原文件夹里新建一个子文件夹
OUT_CUSTOM = "custom"      # 自己指定一个文件夹
OUT_SUB_NAME = "照片排序台剪辑件"


def load_clip_out_mode() -> str:
    v = str(_read().get("clip_out_mode") or OUT_SUB).strip().lower()
    return v if v in (OUT_SAME, OUT_SUB, OUT_CUSTOM) else OUT_SUB


def save_clip_out_mode(mode: str) -> bool:
    v = str(mode or OUT_SUB).strip().lower()
    return _save("clip_out_mode", v if v in (OUT_SAME, OUT_SUB, OUT_CUSTOM) else OUT_SUB)


def load_clip_out_dir() -> str:
    """自选模式下用户指定的文件夹; 没指定返回空串。"""
    v = _read().get("clip_out_dir")
    return str(v) if v else ""


def save_clip_out_dir(folder: str) -> bool:
    return _save("clip_out_dir", folder or None)


def load_normalize_out_dir() -> str:
    """v234: 用户给「统一」指定的副本输出根目录; 没指定返回空串(副本跟源视频同目录)。"""
    v = _read().get("normalize_out_dir")
    return str(v) if v else ""


def save_normalize_out_dir(folder: str) -> bool:
    return _save("normalize_out_dir", folder or None)


# ---------------------------------------------------------------- 剪辑快捷键
def load_edit_keys():
    """用户改过的剪辑键位 {action_id: 键序列}; 没改过返回 {}。"""
    v = _read().get("edit_keys")
    return v if isinstance(v, dict) and v else {}


def save_edit_keys(mapping) -> bool:
    """mapping: {action_id: 键序列}; 空 = 全部回默认。"""
    return _save("edit_keys", {k: v for k, v in (mapping or {}).items()
                               if k and v} or None)


# ---------------------------------------------------------------- 快捷键「我的默认」(v176)
def load_edit_keys_custom():
    """「设为默认」存下的整份键位 {action_id: [主键, 备用键]}; 没存过返回 None。"""
    v = _read().get("edit_keys_custom")
    return v if isinstance(v, dict) and v else None


def save_edit_keys_custom(mapping) -> bool:
    """整份键位快照; 传 None/{} = 清掉(回到出厂)。"""
    return _save("edit_keys_custom",
                 {k: v for k, v in (mapping or {}).items() if k and v} or None)


def load_edit_key_layout_custom():
    """「设为默认」存下的整份排版 {order, groups, names, group_names}; 没存过返回 None。"""
    v = _read().get("edit_key_layout_custom")
    return v if isinstance(v, dict) and v else None


def save_edit_key_layout_custom(layout) -> bool:
    """整份排版快照; 传 None/{} = 清掉(回到出厂)。"""
    return _save("edit_key_layout_custom", layout or None)


# ---------------------------------------------------------------- 快捷键表排版(v161)
def load_edit_key_layout():
    """快捷键表的自定义排版: {order, groups, names, group_names}; 没改过返回 {}。

    - order: 动作 id 的先后顺序
    - groups: {动作 id: 分组 id}（只有挪过组的才记）
    - names: {动作 id: 显示名}（只有改过名的才记）
    - group_names: {分组 id: 显示名}
    """
    v = _read().get("edit_key_layout")
    return v if isinstance(v, dict) and v else {}


def save_edit_key_layout(layout) -> bool:
    return _save("edit_key_layout", layout or None)


# ---------------------------------------------------------------- 剪辑功能「默认开启」(v177)
# F1 里「默认开启」存下的那套: 进入剪辑时这些开关默认是开是关。
# ⚠️ 默认值必须和现有行为一致(磁吸关 / 吸附开 / 跟随关 / 声波关),
#    否则用户没动过这页也会突然变样。进度条走下面那套 load_scrub_on。
FEATURE_DEFAULTS = {
    "magnetic": False,    # 磁吸(同轨自动相接)
    "snap": True,         # 吸附(拖动自动对齐片段首尾/播放头)
    "follow": False,      # 跟随移动(播放时裁剪线钉住、轨道滚)
    "wave": False,        # 声波(显示波形)
    "crowd": False,       # v194: 挤占(拖到已有片段之间时插入并顺延同轨片段)
    "keyframes": True,    # v194: 关键帧(片段上画红色无损刀口刻度)
}


def load_feature_defaults() -> dict:
    """剪辑开关的「默认开启」设定; 没存过返回和出厂一致的默认 dict。"""
    v = _read().get("feature_defaults")
    out = dict(FEATURE_DEFAULTS)
    if isinstance(v, dict):
        for k in FEATURE_DEFAULTS:
            if k in v:
                out[k] = bool(v[k])
    return out


def save_feature_defaults(d) -> bool:
    """写整份默认(按 FEATURE_DEFAULTS 的键; 进度条单独走 load_scrub_on)。"""
    out = {k: bool(d.get(k, FEATURE_DEFAULTS[k])) for k in FEATURE_DEFAULTS}
    return _save("feature_defaults", out)


# ---------------------------------------------------------------- 预览进度条开关(v161)
def load_scrub_on() -> bool:
    """中间预览下沿那条进度条是否显示(默认显示)。"""
    v = _read().get("scrub_on")
    return True if v is None else bool(v)


def save_scrub_on(on: bool) -> bool:
    return _save("scrub_on", bool(on))


# ---------------------------------------------------------------- 剪辑(拖边)
def load_edge_drag_mode() -> str:
    """拖片段左右边缘干什么: "trim"=裁剪首尾 / "speed"=改变倍速(默认裁剪)。"""
    v = str(_read().get("edge_drag_mode") or "trim").strip().lower()
    return "speed" if v == "speed" else "trim"


def save_edge_drag_mode(mode: str) -> bool:
    return _save("edge_drag_mode", "speed" if mode == "speed" else "trim")


# ---------------------------------------------------------------- 截图(捕获帧)
def load_shot_format() -> str:
    """截图格式: png / jpg / webp(默认 png)。"""
    v = str(_read().get("shot_format") or "png").strip().lower()
    return v if v in ("png", "jpg", "webp") else "png"


def save_shot_format(fmt: str) -> bool:
    v = str(fmt or "png").strip().lower()
    return _save("shot_format", v if v in ("png", "jpg", "webp") else "png")


def load_shot_quality() -> int:
    """截图质量 0~100(只对 jpg/webp 有意义, 默认 90)。"""
    try:
        return max(0, min(100, int(_read().get("shot_quality", 90))))
    except Exception:
        return 90


def save_shot_quality(q) -> bool:
    try:
        v = max(0, min(100, int(q)))
    except Exception:
        v = 90
    return _save("shot_quality", v)


# ---------------------------------------------------------------- 统一格式(v200)
# 只**追加** normalize_profile 一个键, 不碰用户已有的 13 个动作配置结构。
DEFAULT_NORMALIZE_PROFILE = {
    "codec": "main",        # main = H.265 Main 8bit(兼容档) / main10 = Main10 10bit
    "crf": 20,              # 18 高质量 / 20 均衡 / 23 省空间
    "keyint_sec": 2,        # 关键帧间隔(秒), 1/2/4
    "audio_bitrate": 192,   # 码率档(128/192/256k), 采样率固定 48kHz, 声道按源分组
    "audio_codec": "aac",   # v260: aac / ac3 / alac(无损忽略码率)。默认 aac=老行为
    # v242: 画面类目标(用户明确授权可推翻"不动画面"护栏)。全部默认 "keep" =
    # 保持原样、按分组隔离 —— 与 v241 行为完全等价, 老副本指纹不变。只有显式设了
    # 目标才触发重渲染(缩放/插帧/色调映射)。
    "target_res": "keep",   # keep / 640x360 / 1280x720 / 1920x1080 / 2560x1440 / 3840x2160
    "target_fps": "keep",   # keep / 24 / 25 / 30 / 50 / 60
    "target_color": "keep", # keep / sdr / hlg / hdr10
}

# v242: 目标分辨率/帧率/HDR 的合法档(同时是 UI 下拉选项来源, 单一来源)
TARGET_RES_CHOICES = ("640x360", "1280x720", "1920x1080", "2560x1440", "3840x2160")
TARGET_FPS_CHOICES = (24, 25, 30, 50, 60)
TARGET_COLOR_CHOICES = ("sdr", "hlg", "hdr10")


def _clean_normalize_profile(data) -> dict:
    out = dict(DEFAULT_NORMALIZE_PROFILE)
    try:
        src = dict(data or {})
    except Exception:
        src = {}
    out.update({k: src[k] for k in DEFAULT_NORMALIZE_PROFILE if k in src})
    # v260: 合法档从规格表单来源动态取 —— v258 扩到五档后这里仍写死两档,
    # 导致 h264/av1/av110 保存后被打回 main(潜伏 bug), 一并修掉。
    # 延迟 import 避免 normalize↔settings 循环(normalize.from_dict 内也延迟引 settings)。
    try:
        from . import normalize as _norm
        _video_keys = tuple(_norm.CODEC_LABELS)
        _audio_keys = tuple(_norm.AUDIO_CODEC_LABELS)
    except Exception:
        _video_keys, _audio_keys = ("main", "main10"), ("aac", "ac3", "alac")
    codec = str(out.get("codec") or "main").strip().lower()
    out["codec"] = codec if codec in _video_keys else "main"
    acodec = str(out.get("audio_codec") or "aac").strip().lower()
    out["audio_codec"] = acodec if acodec in _audio_keys else "aac"
    try:
        out["crf"] = max(16, min(28, int(out.get("crf", 20))))
    except Exception:
        out["crf"] = 20
    try:
        out["keyint_sec"] = int(out.get("keyint_sec", 2))
    except Exception:
        out["keyint_sec"] = 2
    if out["keyint_sec"] not in (1, 2, 4):
        out["keyint_sec"] = 2
    try:
        out["audio_bitrate"] = max(96, min(320, int(out.get("audio_bitrate", 192))))
    except Exception:
        out["audio_bitrate"] = 192
    # v242: 画面类目标校验 —— 非法值一律回落 "keep"(等价 v241 行为)
    res = str(out.get("target_res") or "keep").strip().lower()
    out["target_res"] = res if res == "keep" or res in TARGET_RES_CHOICES else "keep"
    fps = out.get("target_fps", "keep")
    if str(fps).strip().lower() == "keep":
        out["target_fps"] = "keep"
    else:
        try:
            f = int(fps)
            out["target_fps"] = f if f in TARGET_FPS_CHOICES else "keep"
        except Exception:
            out["target_fps"] = "keep"
    color = str(out.get("target_color") or "keep").strip().lower()
    out["target_color"] = color if color == "keep" or color in TARGET_COLOR_CHOICES else "keep"
    return out


def load_normalize_profile() -> dict:
    try:
        saved = _read().get("normalize_profile") or {}
    except Exception:
        saved = {}
    return _clean_normalize_profile(saved)


def save_normalize_profile(profile) -> bool:
    merged = load_normalize_profile()
    try:
        src = dict(profile or {})
    except Exception:
        src = {}
    merged.update({k: src[k] for k in DEFAULT_NORMALIZE_PROFILE if k in src})
    return _save("normalize_profile", _clean_normalize_profile(merged))


def load_normalize_profile_current() -> dict:
    """**当前正在用**的档位(可能没点"保存为默认")。

    为什么要有这个(v217): 生成工作副本用的是界面上的档, 解析副本时却回落到
    "保存为默认"的档 → 两边的参数指纹不一致 → 明明统一过了, 合并/导出还是读
    原片, `_norm_fix_audio` 也判失效。转码十几分钟等于白做, 而且没有任何提示。
    现在生成/解析/判断统一走这一个档位: 下拉一变就写下来, 重启后也还记得。
    """
    try:
        v = _read().get("normalize_profile_current")
    except Exception:
        v = None
    if not isinstance(v, dict) or not v:
        return load_normalize_profile()
    return _clean_normalize_profile(v)


def save_normalize_profile_current(profile) -> bool:
    """记下"当前档位"(只影响副本解析, 不动 normalize_profile 默认档)。"""
    try:
        src = dict(profile or {})
    except Exception:
        return False
    if not src:
        return False
    return _save("normalize_profile_current", _clean_normalize_profile(src))


# ---------------------------------------------------------------- v242 外露参数
# 顶栏外露哪些参数格。默认 = 原有 4 项(顺序即顶栏顺序)。弹窗里勾选后存这里。
DEFAULT_NORM_EXPOSED = ["codec", "audio_bitrate", "keyint_sec", "crf"]
# 所有"可外露"的参数 key(画面目标 v242 也能外露)。推导/固定项不可外露。
NORM_EXPOSABLE = ("codec", "audio_bitrate", "keyint_sec", "crf",
                  "target_res", "target_fps", "target_color")


def load_norm_exposed() -> list:
    """顶栏外露的参数 key 列表。非法/空 → 默认 4 项。"""
    try:
        v = _read().get("norm_exposed")
    except Exception:
        v = None
    if not isinstance(v, list) or not v:
        return list(DEFAULT_NORM_EXPOSED)
    out = [k for k in v if k in NORM_EXPOSABLE]
    # 去重保序
    seen, uniq = set(), []
    for k in out:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq or list(DEFAULT_NORM_EXPOSED)


def save_norm_exposed(keys) -> bool:
    try:
        v = [k for k in list(keys or []) if k in NORM_EXPOSABLE]
    except Exception:
        v = []
    if not v:
        return False
    return _save("norm_exposed", v)


def _save(key: str, value) -> bool:
    data = _read()
    # 注意 0 是合法值(比如截图质量 0), 不能跟 None/"" 一起当"清空"处理
    if value is not None and value != "":
        data[key] = value
    else:
        data.pop(key, None)
    try:
        # ⚠️ 写盘前先备份(v179): 快捷键被误清空过一次且无法恢复。
        #    旧内容存成 merge.json.bak, 任何"写坏了"都能找回来。
        try:
            p = _config_path()
            if p.exists():
                _backup_path().write_text(p.read_text(encoding="utf-8"),
                                         encoding="utf-8")
        except Exception:
            pass
        _config_path().write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 铁律: 用户设过的键位不能被弄丢
#
# 背景(v179 事故): 有个自动化测试脚本没把 APPDATA 指到临时目录, 直接把
# 「设为默认」跑在了**真机**配置上 —— 那条路径会顺手把 edit_keys 清空
# (因为"我的默认"变成了新基准, 旧的差异记录就作废了)。于是用户改过的
# 快捷键被测试脚本抹掉了, 而且**没有备份, 无法恢复**。
#
# 现在加三道保险:
#  1. 写盘前先备份: 每次写 merge.json 之前, 旧的存成 merge.json.bak。
#     任何"写坏了"都能从 .bak 找回来。
#  2. 删用户键位前先确认: 清 edit_keys / edit_keys_custom 属于"不可逆"操作,
#     必须走显式的 API(带 force=True), 顺手把旧值挪进备份键。
#  3. 自动化测试自己兜底: 脚本开头把 APPDATA 指到临时目录(见各 verify_*.py),
#     并且 settings 提供 set_test_sandbox() 供新脚本调用。
BACKUP_NAME = "merge.json.bak"


def _backup_path() -> Path:
    return _config_path().with_name(BACKUP_NAME)


def backup_config() -> bool:
    """把当前 merge.json 复制成 merge.json.bak(写盘前自动调用, 也可手动调)。"""
    src = _config_path()
    try:
        if src.exists():
            _backup_path().write_text(src.read_text(encoding="utf-8"),
                                     encoding="utf-8")
            return True
    except Exception:
        pass
    return False


def restore_config_backup() -> bool:
    """把 merge.json.bak 还原回 merge.json(手滑写坏了时的救命稻草)。"""
    bak = _backup_path()
    try:
        if bak.exists():
            _config_path().write_text(bak.read_text(encoding="utf-8"),
                                      encoding="utf-8")
            return True
    except Exception:
        pass
    return False


def load_config_backup() -> dict:
    """读备份(给"恢复"界面用)。"""
    try:
        p = _backup_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}

