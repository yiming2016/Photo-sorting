# -*- coding: utf-8 -*-
"""统一格式(v200): 把选中视频的"必须一致"参数重写成同一套规格。

只动 ffmpeg 边界; 原文件、主列表、EXIF/XMP/QuickTime 写入一律不受影响。
工作副本规则见 norm_paths.py。
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field

from . import norm_paths, proc

# 与 UI 严重等级一致: 红 = 必须统一, 橙 = 建议统一, 灰 = 仅供参考
LEVEL_FATAL = "fatal"
LEVEL_WARN = "warn"
LEVEL_INFO = None

# v258: 编码档位集中定义(单一来源)。codec key 直接进 NormProfile.key 指纹,
# main/main10 保持原值 → 老副本指纹不变、不失效; 新增 h264/av1/av110 三个档。
# 标签不再写 8bit/10bit(用户 v258 要求)——位深/像素格式在 UI 里按当前档显示确定值。
# 三套编码器的参数体系完全不同, 必须逐档写清, 别散落硬编码:
#   h264: -profile:v high + -level:v 4.1(标准名) + -x264-params keyint... (level 不能塞 params)
#   hevc: -profile:v main/main10 + -x265-params level-idc=<名>(ffprobe 读 idc=名*30)
#   av1 : -pix_fmt 区分位深(profile 都叫 Main) + 编码器自算 level + -svtav1-params keyint...
CODEC_SPECS = {
    "main":   dict(label="H.265 Main（兼容）",        cname="hevc", pix_fmt="yuv420p",     bits=8,  profile="main",   tag="hvc1", family="hevc"),
    "main10": dict(label="H.265 Main10（高质量）",    cname="hevc", pix_fmt="yuv420p10le", bits=10, profile="main10", tag="hvc1", family="hevc"),
    "h264":   dict(label="H.264 High（最兼容）",      cname="h264", pix_fmt="yuv420p",     bits=8,  profile="high",   tag="avc1", family="h264"),
    "av1":    dict(label="AV1 Main（省空间）",        cname="av1",  pix_fmt="yuv420p",     bits=8,  profile="main",   tag="av01", family="av1"),
    "av110":  dict(label="AV1 Main10（省空间）",      cname="av1",  pix_fmt="yuv420p10le", bits=10, profile="main",   tag="av01", family="av1"),
}
CODEC_LABELS = {k: v["label"] for k, v in CODEC_SPECS.items()}
CRF_CHOICES = (18, 20, 23)
KEYINT_CHOICES = (1, 2, 4)

# v260: 音频编码档(mp4 容器实测仅这三种能封装; MP3/Opus/E-AC3 无 mp4 tag 会失败)。
# codec = ffprobe 的 codec_name(直接用它做目标与回读校验); encoder = ffmpeg 编码器名;
# sample_fmt 关键: AAC/AC3 是 fltp, ALAC 无损是 s16p —— 目标签名必须按档给, 否则
# 转完 ALAC 回读 s16p 与目标 fltp 对不上, 同批永远判不统一。
# lossless=True 的档忽略码率: build_cmd 不写 -b:a, UI 把码率下拉置灰。
AUDIO_CODEC_SPECS = {
    "aac":  dict(label="AAC-LC（通用兼容）", encoder="aac",  sample_fmt="fltp", lossless=False),
    "ac3":  dict(label="AC-3 杜比（电视兼容）", encoder="ac3",  sample_fmt="fltp", lossless=False),
    "alac": dict(label="ALAC 苹果无损（体积大）", encoder="alac", sample_fmt="s16p", lossless=True),
}
AUDIO_CODEC_LABELS = {k: v["label"] for k, v in AUDIO_CODEC_SPECS.items()}
AUDIO_BITRATE_CHOICES = (128, 192, 256)


def audio_bitrate_label(v: int) -> str:
    """v260: 码率档文案单一来源(弹窗+顶栏共用); 去掉与固定行重复的 AAC-LC/48kHz。"""
    return f"{int(v)}k" + ("（省空间）" if int(v) == 128 else
                          "（均衡）" if int(v) == 192 else "（高质量）")


@dataclass(frozen=True)
class NormProfile:
    codec: str = "main"
    crf: int = 20
    keyint_sec: int = 2
    audio_bitrate: int = 192
    audio_codec: str = "aac"          # v260: aac / ac3 / alac (默认 aac = 老行为, 老副本指纹不变)
    # v242: 画面类目标。默认 keep = 保持原样(等价于 v241), 不触发重渲染。
    target_res: str = "keep"
    target_fps: object = "keep"      # "keep" 或 int(帧率)
    target_color: str = "keep"

    @property
    def spec(self) -> dict:
        return CODEC_SPECS[self.codec]

    @property
    def audio_spec(self) -> dict:
        return AUDIO_CODEC_SPECS[self.audio_codec]

    @property
    def audio_lossless(self) -> bool:
        return bool(self.audio_spec["lossless"])

    @property
    def audio_sample_fmt(self) -> str:
        return self.audio_spec["sample_fmt"]

    @property
    def pix_fmt(self) -> str:
        return self.spec["pix_fmt"]

    @property
    def hevc_profile(self) -> str:
        """命令行 -profile:v 用的档次名(main/main10/high)。"""
        return self.spec["profile"]

    @property
    def probe_profile(self) -> str:
        """ffprobe 读回的档次名(Main/Main 10/High), 校验用。"""
        return {"main": "Main", "main10": "Main 10", "high": "High"}[self.hevc_profile]

    @property
    def codec_name(self) -> str:
        """ffprobe 的 codec_name: hevc/h264/av1。"""
        return self.spec["cname"]

    @property
    def bits(self) -> int:
        return int(self.spec["bits"])

    @property
    def codec_tag(self) -> str:
        return self.spec["tag"]

    @property
    def codec_family(self) -> str:
        return self.spec["family"]

    @property
    def has_video_rebuild(self) -> bool:
        """是否显式要求了画面类重渲染(缩放/插帧/色调映射)。"""
        return (self.target_res != "keep" or self.target_fps != "keep"
                or self.target_color != "keep")

    def key(self) -> str:
        base = f"{self.codec}|{int(self.crf)}|{int(self.keyint_sec)}|{int(self.audio_bitrate)}"
        # v260: 非默认音频编码档才追加指纹段 —— aac 档保持老指纹, 老副本不失效。
        # 无损 ALAC 忽略码率, 指纹里码率值不影响结果, 但保留也无妨(切回有损时档一致)。
        if self.audio_codec != "aac":
            base += f"|ac:{self.audio_codec}"
        # v242: 只在显式设了目标时追加指纹段 —— 默认档指纹与 v241 完全一致,
        # 老副本不会因为升级到新版本而集体失效。
        if self.has_video_rebuild:
            base += f"|{self.target_res}|{self.target_fps}|{self.target_color}"
        return base

    def to_dict(self) -> dict:
        return {"codec": self.codec, "crf": int(self.crf),
                "keyint_sec": int(self.keyint_sec),
                "audio_bitrate": int(self.audio_bitrate),
                "audio_codec": self.audio_codec,
                "target_res": self.target_res,
                "target_fps": self.target_fps,
                "target_color": self.target_color}

    @classmethod
    def from_dict(cls, d=None) -> "NormProfile":
        from . import settings
        # None = "当前在用"的档(不是保存为默认的档), 见 settings 里的说明
        raw = settings.load_normalize_profile_current()
        try:
            raw.update({k: v for k, v in dict(d or {}).items()
                        if k in raw})
        except Exception:
            pass
        codec = str(raw.get("codec", "main")).strip().lower()
        if codec not in CODEC_LABELS:
            codec = "main"
        try:
            crf = int(raw.get("crf", 20))
        except Exception:
            crf = 20
        if crf not in CRF_CHOICES:
            crf = 20
        try:
            keyint = int(raw.get("keyint_sec", 2))
        except Exception:
            keyint = 2
        if keyint not in KEYINT_CHOICES:
            keyint = 2
        try:
            audio = int(raw.get("audio_bitrate", 192))
        except Exception:
            audio = 192
        acodec = str(raw.get("audio_codec", "aac")).strip().lower()
        if acodec not in AUDIO_CODEC_LABELS:
            acodec = "aac"
        # v242: 经 settings._clean 校验过的三个画面目标(非法值已回落 keep)
        clean = settings._clean_normalize_profile(raw)
        return cls(codec, crf, keyint, audio, acodec,
                   clean["target_res"], clean["target_fps"], clean["target_color"])


@dataclass
class NormItem:
    path: str
    spec: dict | None = None
    need: bool = False
    reasons: list = field(default_factory=list)
    group_key: tuple = ()
    error: str = ""

    @property
    def duration(self) -> float:
        try:
            return float((self.spec or {}).get("duration") or 0)
        except Exception:
            return 0.0


@dataclass
class NormPlan:
    items: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    need_count: int = 0
    total_duration: float = 0.0
    need_duration: float = 0.0
    # v219: 整批统一后的目标声道数(声道是合并侧 fatal, 转码原本不会动它)
    target_channels: int = 2

    @property
    def ok(self) -> bool:
        return bool(self.items) and not self.errors

    @property
    def estimate_seconds(self) -> int:
        # 只是给用户一个量级, 按 1× 素材时长粗估; 不假装精确。
        return int(round(self.need_duration)) if self.need_count else 0


def _fps_value(text) -> float:
    try:
        a, b = str(text or "0/0").split("/", 1)
        a, b = float(a), float(b)
        return a / b if b > 0 else 0.0
    except Exception:
        return 0.0


def _group_key(spec: dict, profile: NormProfile) -> tuple:
    """按"合并时不能混"的字段分组; 分辨率/帧率/色彩/声道只分组, 绝不强行统一。"""
    return (
        spec.get("width"), spec.get("height"), spec.get("r_frame_rate"),
        spec.get("field_order"), spec.get("color_range"),
        spec.get("color_space"), spec.get("color_transfer"),
        spec.get("color_primaries"), spec.get("channels"),
        spec.get("channel_layout"), profile.pix_fmt,
    )


def _group_label(spec: dict, count: int) -> str:
    w, h = spec.get("width"), spec.get("height")
    fps = str(spec.get("r_frame_rate") or "").replace("/1", "")
    ch = spec.get("channels")
    parts = [f"{w}×{h}" if w and h else "分辨率未知",
             f"{fps}fps" if fps and fps != "0/0" else "帧率未知"]
    if ch:
        parts.append(f"{ch}声道")
    if spec.get("color_transfer") and str(spec.get("color_transfer")).lower() not in ("unknown", "unspecified"):
        parts.append(str(spec.get("color_transfer")))
    return " · ".join(parts) + f" · {count} 个"


# v219: 转码「能」消灭的差异 / 「消灭不了只能分组隔离」的差异。
# 判据源头是 grouping.FIELD_LEVELS 里 fatal 的维度 —— 合并侧靠 signature 决定能否
# 无损拼接, 统一侧就必须保证「转完之后 signature 一致」, 否则会出现"UI 说可直接
# 快合、合并时又拼不上"。以前这里是 8 条手写 if, 漏了声道/多音轨/HDR 三处。
_FIXABLE = ("video_codec", "pix_fmt", "bits", "profile", "rotation",
            "field_order", "nb_streams", "audio_codec", "sample_rate",
            "sample_fmt", "channels", "time_base", "level",
            # v242: 画面类重渲染。_diff_reasons 里"源无该字段就跳过"的保护 + 默认
            # keep 目标Spec不覆写这些字段 → 默认档不会因此多判一个"需转码"。
            "width", "r_frame_rate", "color_transfer")

HDR_KINDS = {"smpte2084": "HDR10", "arib-std-b67": "HLG"}


def hdr_kind(spec: dict) -> str:
    """HDR 类别: SDR / HLG / HDR10。统一页拿它把不同 HDR 的素材分成不同组。"""
    t = str((spec or {}).get("color_transfer") or "").strip().lower()
    return HDR_KINDS.get(t, "SDR")


def _same_type(sample, num):
    """分组签名按原值比较, 目标值要和源同类型/同写法, 否则"不用转"会被判成"要转"。"""
    return str(num) if isinstance(sample, str) else num


# HEVC level 名 → level_idc(ffprobe 返回的就是 idc: 4.1 = 123)
_LEVEL_IDC = {"3.1": 93, "4.0": 120, "4.1": 123, "4.2": 126, "5.0": 150,
              "5.1": 153, "5.2": 156, "6.0": 180}
# 时间基: build_cmd 统一给 -video_track_timescale 90000, 目标值必须跟着变。
TARGET_TIME_BASE = "1/90000"


def hevc_level(spec: dict) -> tuple:
    """按画面规格给个够用的 HEVC level, 返回 (名字, idc)。

    宁高勿低 —— 高了只是标注宽松, 低了 x265 直接编不动。level 是合并侧可 fatal 的
    维度(用户在 ⚙ 里调过), 不给显式值的话两条源转完可能一个 4.0 一个 4.1,
    永远拼不上; 所以按「短边 + 帧率」确定性地算, 同规格必然得同一个 level。
    取值贴近 HEVC 标准表(1080p30 = 4.0、1080p60 = 4.1、4K30 = 5.0),
    免得本来达标的素材被标成"要转"。
    """
    src = spec or {}
    try:
        w = int(src.get("width") or 0)
        h = int(src.get("height") or 0)
    except Exception:
        w = h = 0
    dim = min(w, h) if (w and h) else (h or w or 0)
    fps = _fps_value(src.get("r_frame_rate")) or 30.0
    if dim <= 720:
        name = "3.1" if fps <= 30.0 else "4.0"
    elif dim <= 1080:
        name = "4.0" if fps <= 30.0 else ("4.1" if fps <= 60.0 else "5.0")
    elif dim <= 1440:
        name = "5.0" if fps <= 30.0 else "5.1"
    elif dim <= 2160:
        name = "5.0" if fps <= 30.0 else ("5.1" if fps <= 60.0 else "5.2")
    else:
        name = "6.0"
    return name, _LEVEL_IDC[name]


def av1_level(spec: dict) -> int:
    """SVT-AV1 自算的 seq_level_idx —— 实测按像素规模分档, 与 H.264/H.265 无关。

    实测(本机 SVT-AV1 4.2): 640x360@30=1、640x480@30=4、720p@30=5、
    720p@60=8、1080p@24/25/30=8、1080p@60=9、1440p@30=12、4K@30=12、4K@60=13。
    目标值取同口径, 保证同组(分辨率/帧率已隔离)转出的 level 一致、文案准确。
    """
    src = spec or {}
    try:
        w, h = int(src.get("width") or 0), int(src.get("height") or 0)
    except Exception:
        w = h = 0
    px = w * h
    fps = _fps_value(src.get("r_frame_rate")) or 30.0
    hi = fps > 45
    if px <= 240_000:       # 640x360
        return 4 if hi else 1
    if px <= 400_000:       # 640x480
        return 6 if hi else 4
    if px <= 1_000_000:     # 720p
        return 8 if hi else 5
    if px <= 2_400_000:     # 1080p
        return 9 if hi else 8
    if px <= 4_500_000:     # 1440p
        return 13 if hi else 12
    return 13 if hi else 12  # 4K: 30帧=12、60帧=13


def _video_level_idc(spec: dict, family: str) -> int:
    """目标 level 的 ffprobe 整数值, 按编码家族分。

    HEVC 用 general_level_idc(名*30, 4.0=120), H.264 用 level_idc(名*10, 4.1=41);
    AV1 的 level 是编码器按分辨率自算的 seq_level_idx(720p30=5), 没有 idc 换算,
    用 av1_level() 按同一套分辨率分档算出目标值。
    """
    if family == "av1":
        return av1_level(spec)
    name, idc = hevc_level(spec)
    if family == "h264":
        try:
            return int(round(float(name) * 10))
        except Exception:
            return idc
    return idc


def _level_display(level, family: str) -> str:
    """理由文案里的等级显示, 与 grouping._level_text 同口径。"""
    try:
        n = int(level)
    except Exception:
        return ""
    if n <= 0 or n == -99:
        return ""
    if family == "av1":
        return str(n)
    if family == "h264":
        return f"{n / 10:.1f}"
    return f"{n / 30:.1f}"


def pick_target_channels(specs) -> int:
    """目标声道数: 整批都只有单声道就保持单声道, 只要有一个多声道就统一立体声。

    声道数在合并侧是 fatal(不同就不能无损拼), 而 ffmpeg 转码默认保留源声道 ——
    不显式给 -ac 就永远统一不了, 所以目标值必须由整批素材一起定。
    """
    vals = []
    for s in specs:
        try:
            c = int((s or {}).get("channels") or 0)
        except Exception:
            c = 0
        if c > 0:
            vals.append(c)
    if not vals:
        return 2
    return 1 if max(vals) <= 1 else 2


def _target_res_value(profile: NormProfile):
    if profile.target_res == "keep":
        return None
    try:
        w, h = str(profile.target_res).lower().split("x", 1)
        return int(w), int(h)
    except Exception:
        return None


def _target_fps_text(profile: NormProfile):
    """目标帧率写成 ffprobe 同款 'N/1' 字符串(分组签名按字符串比)。"""
    if profile.target_fps == "keep":
        return None
    try:
        return f"{int(profile.target_fps)}/1"
    except Exception:
        return None


# HDR 目标 → (ffprobe color_transfer 值, ffprobe color_primaries, color_space)
_HDR_TARGET = {
    "sdr":   ("bt709", "bt709", "bt709"),
    "hlg":   ("arib-std-b67", "bt2020", "bt2020nc"),
    "hdr10": ("smpte2084", "bt2020", "bt2020nc"),
}


def _ff_color(value, default: str) -> str:
    """ffprobe 色彩值 → ffmpeg setparams 可用值; 空/unknown 回落默认。"""
    s = str(value or "").strip().lower()
    return default if s in ("", "unknown", "unspecified", "none") else s


def target_spec(spec: dict, profile: NormProfile, channels=None) -> dict:
    """转码后「必然变成」的规格, 用来和 grouping.signature 直接对比。

    v241: 只覆写 build_cmd 会改写的字段, 分辨率/帧率/HDR 保持原值(靠分组隔离)。
    v242: 用户授权画面类可重渲染 —— 显式设了目标时, build_cmd 会用 scale/fps/
    zscale 改写这三项, 这里就必须同步反映目标值, 否则"要转"判不出来、分组也归不拢。
    默认(keep)时行为与 v241 完全一致。
    """
    src = dict(spec or {})
    audio = int(src.get("audio_streams") or 0) > 0 or bool(src.get("audio_codec"))
    tgt = dict(src)
    tgt["video_codec"] = profile.codec_name
    tgt["pix_fmt"] = profile.pix_fmt
    tgt["bits"] = profile.bits
    tgt["profile"] = profile.probe_profile
    tgt["rotation"] = 0
    tgt["field_order"] = "progressive"
    tgt["time_base"] = TARGET_TIME_BASE
    # v242: 画面类目标 —— 只在显式设置时覆写, 且 level 要按目标重新算
    lvl_src = dict(src)
    res = _target_res_value(profile)
    if res:
        tgt["width"], tgt["height"] = res
        lvl_src["width"], lvl_src["height"] = res
    fps = _target_fps_text(profile)
    if fps:
        tgt["r_frame_rate"] = fps
        lvl_src["r_frame_rate"] = fps
    if profile.target_color != "keep":
        tr, prim, sp = _HDR_TARGET.get(profile.target_color, ("bt709", "bt709", "bt709"))
        tgt["color_transfer"] = tr
        tgt["color_primaries"] = prim
        tgt["color_space"] = sp
    # level 必须按(目标)分辨率+帧率算, 否则高分辨率目标会被标成低 level → 拼不上。
    # v258: 按编码家族换算 idc —— H.265=名*30、H.264=名*10、AV1 沿用源 seq_idx。
    tgt["level"] = _video_level_idc(lvl_src, profile.codec_family)
    # 流结构: 转码只留 1 视频 + 1 音频(注意签名按 video_streams/audio_streams 归一,
    # 光改 nb_streams 不生效 —— 多音轨能不能拼看的就是这两个)
    tgt["video_streams"] = 1
    tgt["audio_streams"] = 1 if audio else 0
    tgt["nb_streams"] = 2 if audio else 1
    if audio:
        ch = int(channels or 2)
        # v260: 音频编码/sample_fmt 按档给; ALAC 无损是 s16p, 目标必须跟着档走,
        # 否则转完回读 s16p 与目标 fltp 对不上, 同批永远判不统一。
        tgt.update(audio_codec=profile.audio_codec, sample_fmt=profile.audio_sample_fmt,
                   sample_rate=_same_type(src.get("sample_rate"), 48000),
                   channels=_same_type(src.get("channels"), ch),
                   channel_layout="stereo" if ch > 1 else "mono")
    else:
        for k in ("audio_codec", "sample_fmt", "sample_rate",
                  "channels", "channel_layout"):
            tgt[k] = None
    return tgt


def _reason_text(key: str, cn: str, src: dict, tgt: dict,
                 profile: NormProfile) -> str:
    if key == "video_codec":
        return f"视频编码不是 {profile.spec['label'].split('（')[0]}"
    if key == "pix_fmt":
        return f"像素格式不是 {profile.pix_fmt}"
    if key == "bits":
        return f"位深不是 {profile.bits}bit"
    if key == "profile":
        return f"编码档次不是 {profile.probe_profile}"
    if key == "rotation":
        return "带旋转元数据，需要烘进画面"
    if key == "field_order":
        return "隔行扫描，需要转成逐行"
    if key == "nb_streams":
        return "多音轨或额外流，只保留一路声音"
    if key == "audio_codec":
        return f"音频编码不是 {profile.audio_spec['label'].split('（')[0]}"
    if key == "sample_rate":
        return "音频采样率不是 48kHz"
    if key == "sample_fmt":
        return "音频采样格式不一致"
    if key == "channels":
        return f"声道数不一致（统一为 {tgt.get('channels')} 声道）"
    if key == "time_base":
        return "时间基不统一"
    if key == "level":
        return f"编码等级不是 {_level_display(tgt.get('level'), profile.codec_family)}"
    if key == "width":
        return f"分辨率不是 {tgt.get('width')}×{tgt.get('height')}"
    if key == "r_frame_rate":
        return f"帧率不是 {str(tgt.get('r_frame_rate') or '').replace('/1', '')}fps"
    if key == "color_transfer":
        return f"HDR 传输不是 {tgt.get('color_transfer')}"
    return f"{cn}不一致"


def _diff_reasons(spec: dict, profile: NormProfile, channels=None):
    """返回 (转码能消灭的理由, 转码消灭不了的理由)。

    两套分组(合并 13 个 fatal / 统一 3 级)曾各写一套判定, 中间漏项就是 bug 温床;
    这里直接拿 grouping 的 fatal 清单逐项比, 档位在 ⚙ 里改也会自动跟随。
    分辨率/帧率/HDR 传输不参与 —— 转码不改写它们, 只能靠统一页分组隔离。
    """
    from . import grouping
    src = dict(spec or {})
    if not src:
        return [], []
    tgt = target_spec(src, profile, channels)
    fix = []
    for key, cn, lv in grouping.FIELD_LEVELS:
        if lv != "fatal" or key not in _FIXABLE:
            continue
        try:
            sv = grouping._sig_value(key, src)
        except Exception:
            continue
        # 源里没这个信息 → 不判。"不知道"不等于"不合格": 以前极简/半残 spec
        # (缺 nb_streams、time_base 等)会被判成需要转码, 白转一遍还转不绿。
        if sv is None or (isinstance(sv, str) and not sv.strip()):
            continue
        try:
            if sv == grouping._sig_value(key, tgt):
                continue
        except Exception:
            continue
        fix.append(_reason_text(key, cn, src, tgt, profile))
    return fix, []


def _need_reasons(spec: dict, profile: NormProfile, channels=None) -> list:
    """兼容旧调用: 需要转码的全部理由(含"只能分组"的提示)。"""
    fix, block = _diff_reasons(spec, profile, channels)
    return fix + block


def plan(paths, ffprobe: str, profile: NormProfile | None = None,
         specs: dict | None = None) -> NormPlan:
    """规划批量统一。specs 可复用主窗口已探测的结果, 缺的才现读原片。"""
    from . import probe
    profile = profile or NormProfile.from_dict()
    specs = specs or {}
    out = NormPlan()
    grouped: dict = {}
    # 先读齐规格: 目标声道要按整批算, 不能边读边判。
    raw = []
    for path in paths:
        spec = specs.get(path)
        err = ""
        if not spec:
            spec, err = probe.probe_ex(path, ffprobe, resolve_path=False)
        raw.append((path, spec, err))
    out.target_channels = pick_target_channels(
        [s for _p, s, _e in raw if s])
    for path, spec, err in raw:
        item = NormItem(path=path, spec=spec, error=err)
        if not spec:
            item.reasons = [err or "读取失败"]
            item.group_key = ("error", err)
            out.errors.append(f"{os.path.basename(path)}：{err or '读取失败'}")
            out.items.append(item)
            continue
        fix, block = _diff_reasons(spec, profile, out.target_channels)
        item.need = bool(fix)
        item.reasons = fix + block
        # v219: 按「转码之后」的规格分组 —— 转码会把 field_order / channels 等
        # 归一, 拿源 spec 分组会多出并不存在的组(文案说 3 个合并组、屏幕只有 2 个)。
        item.group_key = _group_key(
            target_spec(spec, profile, out.target_channels), profile)
        grouped.setdefault(item.group_key, []).append(item)
        out.total_duration += item.duration
        if item.need:
            out.need_count += 1
            out.need_duration += item.duration
        out.items.append(item)
    for key, members in grouped.items():
        # label 也按统一后的规格写(声道会变), 免得组卡说"1 声道"实际统一成 2
        head = target_spec(members[0].spec or {}, profile, out.target_channels)
        out.groups.append({
            "key": key,
            "label": _group_label(head, len(members)),
            "items": members,
            "need_count": sum(1 for m in members if m.need),
        })
    return out


def _video_rebuild_filters(spec: dict, profile: NormProfile) -> list:
    """v242: 画面类重渲染的 -vf 滤镜链(默认 keep → 空, 完全等价 v241)。

    顺序: 缩放 → 帧率 → HDR 色调映射。HDR 用 zscale+tonemap(需要 ffmpeg 带
    libzimg; 主流构建都有)。只在显式设了目标时才加, 责任链与 target_spec 一致。
    """
    if not profile.has_video_rebuild:
        return []
    filters = []
    res = _target_res_value(profile)
    if res:
        w, h = res
        # force_original_aspect_ratio=decrease + pad: 不变形, 不足处补黑边
        filters.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease")
        filters.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
    fps = _target_fps_text(profile)
    if fps:
        filters.append(f"fps={fps}")
    if profile.target_color != "keep":
        # ⚠️ zscale 需要源有明确的色彩元数据才能推断转换路径; 缺元数据的源会报
        # "no path between colorspaces" 直接转码失败。先用 setparams 按源 spec 补
        # 一套(缺失就回落到 SDR/BT.709), 保证链在任何源上都能跑。
        src_prim = _ff_color(spec.get("color_primaries"), "bt709")
        src_trc = _ff_color(spec.get("color_transfer"), "bt709")
        src_sp = _ff_color(spec.get("color_space"), "bt709")
        filters.append(f"setparams=color_primaries={src_prim}:color_trc={src_trc}:colorspace={src_sp}")
        if profile.target_color == "sdr":
            # HDR→SDR: 线性光 → reinhard 色调映射 → BT.709
            filters.append("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709")
            filters.append("tonemap=tonemap=reinhard:desat=0")
            filters.append("zscale=t=bt709:m=bt709:r=tv")
        else:
            # SDR→HLG/HDR10: 转成 BT.2020 原色 + 对应传输特性
            tr = "arib-std-b67" if profile.target_color == "hlg" else "smpte2084"
            filters.append(f"zscale=p=bt2020:t={tr}:m=bt2020nc")
    # 统一输出像素格式(跟随编码档), 与 -pix_fmt 双保险
    filters.append(f"format={profile.pix_fmt}")
    return filters


def build_cmd(src: str, dst: str, spec: dict, profile: NormProfile,
              ffmpeg: str, channels=None) -> list:
    """生成单文件转码命令。VFR 用 -fps_mode passthrough 保留原时间戳, 不改写帧序。"""
    keyint = max(1, min(300, int(round((_fps_value(spec.get("r_frame_rate")) or 30.0)
                                       * int(profile.keyint_sec)))))
    # v242: 目标帧率变了, keyint 要按目标帧率算(否则关键帧间隔秒数会偏移)
    tfps = _target_fps_text(profile)
    if tfps:
        keyint = max(1, min(300, int(round(_fps_value(tfps) * int(profile.keyint_sec)))))
    lvl_src = dict(spec or {})
    if tfps:
        lvl_src["r_frame_rate"] = tfps
    family = profile.codec_family
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-progress", "pipe:1", "-stats_period", "0.5",
        "-i", src,
        "-map", "0:v:0",
        "-map_metadata", "0", "-map_chapters", "-1",
    ]
    # v258: 三套编码器的档次/等级/参数写法完全不同, 按 family 分支生成。
    if family == "hevc":
        level_name = hevc_level(lvl_src)[0]
        x265 = (f"keyint={keyint}:min-keyint={keyint}:scenecut=0:"
                f"open-gop=0:repeat-headers=1:level-idc={level_name}")
        cmd += [
            "-c:v", "libx265", "-preset", "medium", "-crf", str(int(profile.crf)),
            "-profile:v", profile.hevc_profile, "-pix_fmt", profile.pix_fmt,
            "-tag:v", profile.codec_tag, "-x265-params", x265,
        ]
    elif family == "h264":
        # ⚠️ level 必须走 -level:v 标准名(4.1), 塞进 x264-params 会被当数字解析
        # → 档次降级成 Constrained Baseline(实测)。keyint 等才进 x264-params。
        level_name = hevc_level(lvl_src)[0]
        x264 = (f"keyint={keyint}:min-keyint={keyint}:scenecut=0:"
                f"open-gop=0:repeat-headers=1")
        cmd += [
            "-c:v", "libx264", "-preset", "medium", "-crf", str(int(profile.crf)),
            "-profile:v", profile.hevc_profile, "-level:v", level_name,
            "-pix_fmt", profile.pix_fmt, "-tag:v", profile.codec_tag,
            "-x264-params", x264,
        ]
    else:  # av1
        # SVT-AV1: 8/10bit 都叫 Main profile(靠 -pix_fmt 区分), level 由编码器按
        # 分辨率/帧率自算(实测 720p30=5), 不指定 level-idc。crf 用通用数值即可。
        svt = f"keyint={keyint}:min-keyint={keyint}:scenecut=0"
        cmd += [
            "-c:v", "libsvtav1", "-preset", "8", "-crf", str(int(profile.crf)),
            "-pix_fmt", profile.pix_fmt, "-tag:v", profile.codec_tag,
            "-svtav1-params", svt,
        ]
    # v242: 画面类重渲染滤镜链。⚠️ fps 滤镜与 -fps_mode passthrough 冲突,
    # 有帧率目标时必须去掉 passthrough(让 fps 滤镜决定时间戳)。
    vf = _video_rebuild_filters(spec, profile)
    if vf:
        cmd += ["-vf", ",".join(vf)]
        if not tfps:
            cmd += ["-fps_mode", "passthrough"]
    else:
        cmd += ["-fps_mode", "passthrough"]
    cmd += [
        "-video_track_timescale", "90000",
        "-avoid_negative_ts", "make_zero",
        "-metadata:s:v:0", "rotate=0",
    ]
    # v242: HDR 目标时用目标色彩元数据(与 target_spec / zscale 一致), 否则用源值
    color_target = None
    if profile.target_color != "keep":
        tr, prim, sp = _HDR_TARGET.get(profile.target_color, ("bt709", "bt709", "bt709"))
        color_target = {"color_space": sp, "color_transfer": tr, "color_primaries": prim}
    for key, opt in (("color_range", "-color_range"),
                     ("color_space", "-colorspace"),
                     ("color_transfer", "-color_trc"),
                     ("color_primaries", "-color_primaries")):
        v = (color_target or {}).get(key) if color_target else None
        if v is None:
            v = spec.get(key)
        if v and str(v).lower() not in ("unknown", "unspecified"):
            cmd += [opt, str(v)]
    if int(spec.get("audio_streams") or 0) > 0:
        # v219: -ac 必须显式给 —— 声道数是合并侧 fatal, 不写就永远统一不了。
        cmd += ["-map", "0:a:0", "-c:a", profile.audio_spec["encoder"]]
        # v260: ALAC 无损编码忽略 -b:a (ffmpeg 会忽略, 但写出来会误导), 不写。
        if not profile.audio_lossless:
            cmd += ["-b:a", f"{int(profile.audio_bitrate)}k"]
        cmd += ["-ac", str(int(channels or 2)),
                "-ar", "48000", "-af", "aresample=async=1:first_pts=0"]
    cmd.append(dst)
    return cmd


def _tail(path: str, n: int = 8) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "\n".join(f.read().strip().splitlines()[-n:])
    except Exception:
        return ""


def convert_one(src: str, profile: NormProfile, ffmpeg: str, ffprobe: str,
                spec: dict | None = None, progress=None, cancelled=None,
                channels=None):
    """转一个文件。返回 (ok, 结果路径或错误文本, 是否跳过)。

    v222: 包装层负责"正在写入"标记 —— 转码期间 resolve() 对这个源文件一律
    回退原片, 防止同时进行的合并/导出把写到一半的副本拼进去。
    """
    norm_paths.mark_writing(src)
    try:
        return _convert_one_impl(src, profile, ffmpeg, ffprobe, spec=spec,
                                 progress=progress, cancelled=cancelled,
                                 channels=channels)
    finally:
        norm_paths.unmark_writing(src)


def _convert_one_impl(src: str, profile: NormProfile, ffmpeg: str, ffprobe: str,
                      spec: dict | None = None, progress=None, cancelled=None,
                      channels=None):
    """转一个文件。返回 (ok, 结果路径或错误文本, 是否跳过)。"""
    from . import probe
    dst = norm_paths.work_path(src, profile)
    if not dst:
        return False, "不是视频或源文件已不存在", False
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return True, dst, True
    spec = spec or {}
    if not spec:
        spec, err = probe.probe_ex(src, ffprobe, resolve_path=False)
        if not spec:
            return False, err or "读取源文件失败", False
    norm_paths.ensure_dir(dst)
    part = dst[:-4] + ".part.mp4" if dst.lower().endswith(".mp4") else dst + ".part"
    err_path = part + ".stderr.txt"
    cmd = build_cmd(src, part, spec, profile, ffmpeg, channels=channels)
    total = max(0.001, float(spec.get("duration") or 0))
    started = time.monotonic()
    eta_smooth = -1.0
    stopped = False
    rc = -1
    try:
        with open(err_path, "wb") as ef:
            p = proc.popen(cmd, stdout=subprocess.PIPE, stderr=ef)
            try:
                for raw in iter(p.stdout.readline, b""):
                    if cancelled is not None and cancelled():
                        stopped = True
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("out_time_us="):
                        continue
                    try:
                        us = int(line.split("=", 1)[1])
                    except Exception:
                        continue
                    done = max(0.0, us / 1_000_000.0)
                    pct = max(0.0, min(99.0, done / total * 100.0))
                    elapsed = max(0.05, time.monotonic() - started)
                    speed = done / elapsed
                    if speed > 0:
                        eta = max(0.0, (total - done) / speed)
                        eta_smooth = eta if eta_smooth < 0 else eta_smooth * 0.7 + eta * 0.3
                    if progress is not None:
                        progress(int(pct), eta_smooth)
            finally:
                try:
                    p.stdout.close()
                except Exception:
                    pass
            if stopped:
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
            else:
                rc = p.wait()
    except Exception as e:
        return False, f"转码异常：{e}", False

    if stopped:
        for pth in (part, err_path):
            try:
                if os.path.exists(pth):
                    os.remove(pth)
            except OSError:
                pass
        return False, "已取消", False
    if rc != 0:
        msg = _tail(err_path) or f"ffmpeg 返回码 {rc}"
        try:
            os.remove(part)
        except OSError:
            pass
        return False, msg, False

    # 验收: 真跑一遍 ffprobe, 参数不对就不交付副本。
    got, err = probe.probe_ex(part, ffprobe, resolve_path=False)
    if not got:
        try:
            os.remove(part)
        except OSError:
            pass
        return False, f"转码结果无法读取：{err}", False
    bad = []
    if str(got.get("video_codec") or "").lower() != profile.codec_name:
        bad.append(f"编码不是 {profile.codec_name}")
    if str(got.get("pix_fmt") or "") != profile.pix_fmt:
        bad.append(f"像素格式不是 {profile.pix_fmt}")
    # v258: ffprobe 的档次名带空格("Main 10"), 命令行的 -profile:v 是 main10 ——
    # 旧代码拿后者比前者, main10 档每次都误判失败(潜伏 bug), 改用 probe_profile。
    if str(got.get("profile") or "").strip() != profile.probe_profile:
        bad.append(f"档次不是 {profile.probe_profile}")
    # v258: level 按家族校验 —— H.264 读 idc*10、H.265 读 idc*30。AV1 的 level
    # 完全由编码器按分辨率自算(可能随版本/码率浮动), 严格比会对不上导致白转,
    # 故只校验前两者; AV1 的目标 level 仅用于分组/文案(见 av1_level)。
    if profile.codec_family != "av1":
        want_lvl = _video_level_idc(spec, profile.codec_family)
        try:
            if want_lvl > 0 and int(got.get("level") or 0) != want_lvl:
                bad.append(f"等级不是 {_level_display(want_lvl, profile.codec_family)}")
        except Exception:
            pass
    if int(spec.get("audio_streams") or 0) > 0:
        if str(got.get("audio_codec") or "").lower() != profile.audio_codec:
            bad.append(f"音频不是 {profile.audio_spec['label'].split('（')[0]}")
        try:
            if channels and int(got.get("channels") or 0) != int(channels):
                bad.append(f"声道数不是 {int(channels)}")
        except Exception:
            pass
    if bad:
        try:
            os.remove(part)
        except OSError:
            pass
        return False, "转码结果校验失败：" + "、".join(bad), False

    try:
        os.replace(part, dst)
    except OSError as e:
        return False, f"保存副本失败：{e}", False
    try:
        if os.path.exists(err_path):
            os.remove(err_path)
    except OSError:
        pass
    if progress is not None:
        progress(100, 0.0)
    return True, dst, False
