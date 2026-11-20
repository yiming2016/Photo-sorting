# -*- coding: utf-8 -*-
"""把视频按「可无损合并」的规格签名分组。

判定原则(与 ffmpeg concat demuxer + -c copy 一致):
  视频编码、分辨率、像素格式、帧率、音轨编码、采样率、声道数、流数量
  全部一致, 才能无损拼接; 任一不同则会失败或卡顿, 需走转码。
分组采用「签名相同归一组」: 同规格的视频聚到一起, 组内成员 >1 即「可无损合并」。
"""


def fps_to_float(s) -> float:
    try:
        n, d = str(s).split("/")
        n, d = float(n), float(d)
        return n / d if d else 0.0
    except Exception:
        try:
            return float(s)
        except Exception:
            return 0.0


def _norm_rotation(v) -> int:
    """旋转元数据: 无(None)与 0° 都算「不旋转」, 归一成 0。"""
    try:
        r = int(v)
    except Exception:
        return 0
    return 0 if r in (0, 360, -360) else r


def _norm_field_order(v) -> str:
    """扫描方式: 空/unknown/progressive 都算「逐行」, tt/bb/tb/bt 等隔行保留原值。"""
    s = str(v or "").strip().lower()
    return "progressive" if s in ("", "unknown", "progressive") else s


def _norm_stream_layout(spec: dict):
    """流结构: 有 video_streams/audio_streams 用 (视频条数, 音频条数), 否则退回总流数。"""
    vs = spec.get("video_streams")
    as_ = spec.get("audio_streams")
    if vs is None and as_ is None:
        return spec.get("nb_streams")
    try:
        return (int(vs or 0), int(as_ or 0))
    except Exception:
        return spec.get("nb_streams")


# 签名取值器: 这几个参数的「可比对值」不是原始字段, 要归一化后再比
_SIG_EXTRACT = {
    "width": lambda s: (s.get("width"), s.get("height")),
    "r_frame_rate": lambda s: round(fps_to_float(s.get("r_frame_rate", "0/0")), 3),
    "nb_streams": _norm_stream_layout,
    "rotation": lambda s: _norm_rotation(s.get("rotation")),
    "field_order": lambda s: _norm_field_order(s.get("field_order")),
    "time_base": lambda s: str(s.get("time_base") or ""),
}


def _sig_value(key: str, spec: dict):
    fn = _SIG_EXTRACT.get(key)
    if fn is not None:
        return fn(spec)
    return spec.get(key)


def signature(spec: dict) -> tuple:
    """规格签名: 完全一致 = 可无损拼接(按用户当前的分档设置动态生成)。

    纳入所有「当前被设为 必须一致(fatal)」的参数；用户在 ⚙️ 里改分档后,
    签名随之变化 —— 分组、全选可无损、能否无损合并全部跟着变。
    """
    if not spec:
        return (None,)
    return tuple(_sig_value(k, spec) for k, _cn, lv in FIELD_LEVELS if lv == "fatal")


# ---------------------------------------------------------------- 致命参数分类
# level: "fatal" = 必须一致(红) / "warn" = 建议一致(橙) / None = 仅供参考(不标色)
# 顺序即展示顺序, 也是「对比」清单的分档顺序(必须一致 → 建议一致 → 仅参考)。
#
# v98 起: 分档用户可在 ⚙️ 里改, 且**连「能否无损合并」的判定一起生效**
# (signature 动态纳入所有 fatal 档参数)。FIELD_LEVELS 是运行时副本,
# DEFAULT_FIELD_LEVELS 是出厂默认, 「恢复默认」回到这份。
DEFAULT_FIELD_LEVELS: list[tuple[str, str, str | None]] = [
    # ---- 必须一致(出厂默认 = 无损合并硬门槛) ----
    ("video_codec",     "视频编码",     "fatal"),
    ("width",           "分辨率",       "fatal"),
    ("r_frame_rate",    "帧率",         "fatal"),
    ("pix_fmt",         "像素格式",     "fatal"),
    ("bits",            "位深",         "fatal"),
    ("color_transfer",  "HDR 传输",     "fatal"),
    ("audio_codec",     "音频编码",     "fatal"),
    ("sample_fmt",      "音频采样格式", "fatal"),
    ("sample_rate",     "采样率",       "fatal"),
    ("channels",        "声道数",       "fatal"),
    ("nb_streams",      "流结构",       "fatal"),
    ("rotation",        "旋转",         "fatal"),
    ("field_order",     "扫描方式",     "fatal"),
    # ---- 建议一致(多数能拼, 但可能花屏/音画异常) ----
    ("profile",         "编码档次",     "warn"),
    ("level",           "编码等级",     "warn"),
    ("audio_profile",   "音频档次",     "warn"),
    ("time_base",       "时间基",       "warn"),
    ("sar",             "像素宽高比",   "warn"),
    ("color_range",     "色彩范围",     "warn"),
    ("color_space",     "色彩空间",     "warn"),
    ("color_primaries", "三原色",       "warn"),
    ("channel_layout",  "声道布局",     "warn"),
    # ---- 仅供参考(不影响能否合并) ----
    ("duration",        "时长",         None),
    ("video_bitrate",   "视频码率",     None),
    ("format_name",     "容器格式",     None),
]

# ---------------------------------------------------------------- 参数悬浮说明(v251)
# 每个参数: 逐条列举常见格式+详解(用户编辑「参数说明.txt」后回填, 每条一行)。
# 三处对比区的 ⓘ 角标都取这里(单一来源); 用户可在「参数说明.txt」里改文案后回填。
FIELD_DESC: dict[str, str] = {
    "video_codec": """H.264/AVC：全球兼容性最强、应用最广的有损视频编码标准，几乎所有播放器、智能设备、视频平台都原生支持
H.265/HEVC：又称高效视频编码，压缩效率比H.264提升约一倍，同等画质下文件体积更小，是4K/HDR视频的主流编码
VP9：谷歌推出的开源免版税视频编码，压缩效率优于H.264，多用于在线流媒体平台
AV1：新一代开源免版税视频编码，压缩效率大幅领先H.265，是8K、超高清流媒体的主流发展方向
MPEG-4：早期有损视频编码标准，压缩效率一般，多见于早年网络视频与数码拍摄素材
MPEG-2：广播电视与DVD采用的经典编码，压缩效率较低，主要应用于广电播出与光盘介质""",
    "width": """1920：全高清1080p视频的标准水平像素数，搭配1080垂直像素构成1920×1080分辨率
1280：高清720p视频的标准水平像素数，搭配720垂直像素构成1280×720分辨率
3840：超高清4K视频的标准水平像素数，搭配2160垂直像素构成3840×2160分辨率
720：标清视频的常见水平像素数，多用于早期网络视频与移动设备
640：低清视频的常见水平像素数，多用于小体积流媒体与老旧视频素材""",
    "r_frame_rate": """24：电影级标准帧率（源文件常为 23.976），是院线影视与文艺视频的主流帧率
25：PAL制式标准帧率，中国、欧洲等地区的电视与广电内容普遍采用
30：民用视频常用帧率（源文件常为 29.97），广泛用于手机拍摄、网络短视频与直播
50：PAL制式高帧率规格，运动画面更流畅，常见于国内广电与直播内容
60：高帧率标准，运动画面顺滑细腻，多用于游戏、体育赛事与高端视频""",
    "pix_fmt": """yuv420p：最通用的YUV平面像素格式，色度水平垂直均减半采样，兼容性极强，是民用视频的标配
yuv422p：YUV平面像素格式，色度仅水平方向减半采样，色彩精度更高，多用于专业剪辑
yuv444p：YUV平面像素格式，色度无采样压缩，色彩精度最高，多用于专业母版与特效制作
yuv420p10le：10位深的YUV420平面格式，是HDR视频的主流像素格式
nv12：半平面YUV格式，色度数据交错存储，广泛用于移动端、硬件编码与摄像头输出
p010le：10位深的半平面YUV格式，是硬件解码HDR视频的常用像素格式""",
    "bits": """8bit：标准色彩位深，单通道可呈现256级色彩，合计约1677万色，是SDR视频的通用规格
10bit：高色彩位深，单通道可呈现1024级色彩，色阶过渡更平滑，是HDR视频的主流规格
12bit：专业级色彩位深，单通道可呈现4096级色彩，多用于电影母版与专业后期制作""",
    "color_transfer": """bt709：SDR视频的标准光电转换特性，对应高清电视的伽马标准，是绝大多数普通视频的默认规格
smpte2084（PQ）：HDR的主流传递函数，又称PQ制式，基于感知量化，支持超高亮度范围，是蓝光、流媒体HDR的标准
arib-std-b67（HLG）：HDR的混合对数伽马传递函数，兼容SDR显示设备，多用于广电直播类HDR内容
gamma2.2：通用SDR伽马标准，是电脑显示器、sRGB色域的标准伽马值""",
    "audio_codec": """AAC：当前最主流的有损音频编码，同码率音质优于MP3，是MP4视频的标配音频格式
MP3：普及率最高的有损音频编码，全设备兼容，广泛用于音乐播放与网络音频
FLAC：无损音频编码，可完整还原原始音质，体积小于无压缩格式，是无损音乐的主流选择
AC3：又称杜比数字，5.1声道环绕经典编码，广泛用于DVD、蓝光与影视音轨
E-AC3：AC3升级版本，又称杜比数字+，支持更高码率与更多声道，常见于流媒体影视
Opus：开源低延迟音频编码，音质与压缩效率优于AAC，多用于实时语音、直播与Web视频
PCM：无压缩原始音频编码，音质无损但体积较大，是专业剪辑、CD与蓝光的原始格式
DTS：影院级环绕音频编码，码率更高声音细节更丰富，多用于蓝光原盘与高端影视""",
    "sample_fmt": """fltp：浮点平面型音频采样格式，精度高，是现代视频编码器的主流音频采样格式
s16：16位整型打包采样格式，兼容性强，是普通音频与CD的通用采样格式
s32：32位整型采样格式，精度更高，多用于专业音频处理与后期制作
s16p：16位整型平面采样格式，分通道独立存储，适合编码处理场景""",
    "sample_rate": """44100 Hz：CD级标准采样率，可覆盖人耳听觉范围，是音乐、民用音频的通用规格
48000 Hz：影视视频的标准采样率，是视频制作、广电播出的标配采样规格
96000 Hz：高清音频采样率，又称Hi-Res规格，高频细节更丰富，多用于发烧音乐与专业制作
192000 Hz：专业级高清采样率，多用于高端母带制作与发烧音频""",
    "channels": """1（单声道）：仅一路声音信号，无空间感，多用于语音、广播与老旧素材
2（立体声）：左右双声道，具备基础空间方位感，是耳机、普通设备的标准配置
6（5.1）：5.1环绕声道，包含左、中、右、左环绕、右环绕与低音炮，是家庭影院的主流规格
8（7.1）：7.1环绕声道，在5.1基础上新增后环绕声道，包围感更强，是高端影院规格""",
    "nb_streams": """1（纯视频/纯音频）：文件内仅包含1路视频流或1路音频流
2（1路视频+1路音频）：最常见的媒体文件配置，包含1路视频与1路对应音频
3及以上（多音轨/多字幕）：包含多路音轨、字幕流，常见于多语言影视、蓝光压制资源""",
    "rotation": """0°：无旋转，画面正常显示，是绝大多数视频的默认状态
90°：画面顺时针旋转90度，通常对应竖屏拍摄的视频元数据
180°：画面旋转180度，对应倒置拍摄的视频
270°：画面逆时针旋转90度，同样对应竖屏拍摄的元数据标记""",
    "field_order": """progressive（逐行）：逐行扫描，一帧画面完整一次性输出，画面无闪烁，是数字视频的主流方式
tt/bb（隔行）：隔行扫描，一帧画面分两场交替输出，是老式模拟电视、磁带的扫描方式""",
    "profile": """Baseline：H.264基础档次，解码复杂度最低，多用于低性能设备、实时通讯与标清视频
Main：H.264主流档次，压缩效率与复杂度平衡，早年曾广泛用于网络视频
High：H.264高端档次，压缩效率最高，是1080p及以上高清影视的主流档次
Main：H.265主流档次，支持8位深SDR内容，是H.265编码的基础常用规格
Main 10：H.265 10位档次，支持10位深色彩，是HDR视频的主流编码档次
Main 12：H.265 12位档次，支持12位深色彩，多用于专业级HDR与母版制作""",
    "level": """3.1：H.264 等级，最高支持 1280×720（720p）30 帧以内，常见于早期高清与移动设备（H.265 无此显示值）
4.0：H.264 支持 1080p 30 帧；H.265 对应原始 idc 120，支持 1080p 30 帧，均为全高清入门等级
4.1：H.264 支持 1080p 30 帧高码率；H.265 对应原始 idc 123，支持 1080p 60 帧，均为全高清主流等级
5.0：H.264 支持 2K/1080p 高帧率；H.265 对应原始 idc 150，支持 4K 30 帧，进入超高清规格
5.1：H.264 支持 4K 30 帧；H.265 对应原始 idc 153，支持 4K 60 帧，是 4K 高清高码率常用等级
6.0：仅 H.265 有此显示值，对应原始 idc 180，支持 8K 30 帧超高清，适配顶级高码率内容
1：AV1 专有等级序号，最低档，多见于 640×360 30 帧的小尺寸短视频
4：AV1 专有等级序号，对应 640×360 60 帧或 640×480 30 帧的低清视频
5：AV1 专有等级序号，对应 720p 30 帧，是 AV1 网络视频的常见规格
8：AV1 专有等级序号，对应 1080p 30 帧或 720p 60 帧，高清主流规格
9：AV1 专有等级序号，对应 1080p 60 帧，高帧率高清规格
12：AV1 专有等级序号，对应 1440p~4K 30 帧，进入超高清规格
13：AV1 专有等级序号，对应 4K 60 帧，最高档超高清规格""",
    "audio_profile": """LC：AAC低复杂度规格，音质与解码平衡，兼容性最强，是网络视频的标配
HE-AAC：高效AAC，在低码率下音质优于LC-AAC，多用于低码率流媒体与广播
HE-AACv2：高效AAC第二版，进一步优化极低码率表现，多用于语音广播与低带宽场景
LD：AAC低延迟规格，解码延迟极低，多用于实时通讯、直播互动场景""",
    "time_base": """1/90000：MPEG系统标准时间基，是视频流最常用的时间戳单位
1/1000：毫秒级时间基，计算直观，多用于部分封装格式与处理工具
1/30：对应30帧率的时间基，以单帧时长为最小单位
1/25：对应25帧率的时间基，以单帧时长为最小单位
1/48000：音频常用时间基，与48kHz采样率对应""",
    "sar": """1:1（方形像素）：像素为正方形，是民用数字视频的通用标准，画面无变形
4:3：非方形像素，多见于老式标清电视素材与专业广电设备
16:11：非方形像素，常见于部分标清转高清的适配素材""",
    "color_range": """tv/limited（有限）：有限色彩范围，亮度取值16-235，是视频内容的标准规格
pc/full（完整）：完整色彩范围，亮度取值0-255，是电脑图形、图片的标准规格""",
    "color_space": """bt709：高清视频标准色彩矩阵，对应1080p及以上SDR视频，是当前主流规格
bt601：标清视频标准色彩矩阵，多用于DVD、老式标清电视内容
bt2020nc：超高清广色域色彩矩阵，是4K/8K HDR视频的标准规格
smpte170m：标清NTSC制式色彩矩阵，常见于北美地区老式视频素材""",
    "color_primaries": """bt709：高清SDR标准三原色标准，色域与sRGB接近，是普通视频的主流规格
bt2020：超高清广色域三原色标准，色域范围极大，是HDR视频的标准规格
bt470bg：PAL制式标清三原色标准，多见于欧洲、中国老式标清素材
smpte170m：NTSC制式标清三原色标准，多见于北美老式标清素材
DCI-P3：数字影院级广色域标准，色彩饱和度更高，广泛用于电影与高端HDR内容""",
    "channel_layout": """mono：单声道布局，仅单个声道发声，无空间方位感
stereo：立体声布局，包含左右双声道，是最通用的民用声道布局
5.1：5.1环绕声布局，包含左、中、右、左环绕、右环绕与低频效果声道
7.1：7.1环绕声布局，在5.1基础上新增双侧后环绕声道，包围感更强
5.1(side)：侧置环绕的5.1布局，是流媒体与蓝光常见的5.1声道排布方式""",
    "duration": """5秒：短视频片段的常见时长
10秒：短视频、素材片段的常见时长
30秒：广告、短视频内容的常见时长
60秒及以上：长视频、完整影视内容的时长""",
    "video_bitrate": """1000～8000 kbps（1080p）：1080p全高清视频的常见码率区间，码率越高画质越精细
8000～40000 kbps（4K）：4K超高清视频的常见码率区间，适配高分辨率的画质需求""",
    "format_name": """mov/mp4：最通用的媒体容器格式，兼容性极强，广泛用于网络传播、移动设备与剪辑
matroska/mkv：开源容器格式，支持多音轨、多字幕、多种编码，常用于影视资源压制
avi：早期经典容器格式，兼容性广但支持的编码有限，多见于老旧视频素材
flv：Flash视频容器，曾是网页视频的主流格式，现已逐步被淘汰
ts：传输流容器，抗损坏能力强，广泛用于广电直播、数字电视与录制内容
webm：开源Web容器格式，搭配VP9/AV1与Opus编码，多用于网页在线视频""",
}


def field_desc(key: str) -> str:
    """参数悬浮说明；未登记的参数回落为空串。"""
    return FIELD_DESC.get(key, "")


# ---------------------------------------------------------------- 参数对比归类(v248)
# 「参数对比」在三处 UI(右键对比窗口 / 右侧面板 / 统一页)共用的归类排序依据。
# media: video=视频类 / audio=音频类 / other=流结构·容器·时长等中性项。
# sub:   归类小项, 同一 media 内按 SUB_ORDER 顺序排。
# ⚠️ 只影响「对比清单的展示顺序与分隔线」, 不影响 FIELD_LEVELS 分档、签名与合并判定。
FIELD_CATEGORY: dict[str, tuple[str, str]] = {
    # ---- 视频类 ----
    "video_codec":    ("video", "编码"),
    "profile":        ("video", "编码"),
    "level":          ("video", "编码"),
    "width":          ("video", "分辨率"),
    "sar":            ("video", "分辨率"),
    "r_frame_rate":   ("video", "帧率"),
    "time_base":      ("video", "帧率"),
    "pix_fmt":        ("video", "像素"),
    "bits":           ("video", "像素"),
    "color_transfer": ("video", "色彩"),
    "color_range":    ("video", "色彩"),
    "color_space":    ("video", "色彩"),
    "color_primaries":("video", "色彩"),
    "video_bitrate":  ("video", "码率"),
    "bitrate_mode":   ("video", "码率"),
    # ---- 音频类 ----
    "audio_codec":    ("audio", "编码"),
    "audio_profile":  ("audio", "编码"),
    "sample_fmt":     ("audio", "采样"),
    "sample_rate":    ("audio", "采样"),
    "channels":       ("audio", "声道"),
    "channel_layout": ("audio", "声道"),
    # ---- 中性/流结构 ----
    "nb_streams":     ("other", "流结构"),
    "rotation":       ("other", "流结构"),
    "field_order":    ("other", "流结构"),
    "format_name":    ("other", "容器"),
    "duration":       ("other", "其它"),
}
# media 大组顺序 + 每组内小项顺序(数字越小越靠前)
MEDIA_ORDER = {"video": 0, "audio": 1, "other": 2}
SUB_ORDER = {
    "video": ["编码", "分辨率", "帧率", "像素", "色彩", "码率"],
    "audio": ["编码", "采样", "声道"],
    "other": ["流结构", "容器", "其它"],
}


def field_category(key: str) -> tuple[str, str]:
    """字段的 (media, sub); 未知字段归到中性其它, 保证排序不崩。"""
    return FIELD_CATEGORY.get(key, ("other", "其它"))


# 运行时分档(可被 set_field_levels 覆盖)
FIELD_LEVELS: list[tuple[str, str, str | None]] = list(DEFAULT_FIELD_LEVELS)


def apply_config(levels: dict | None, order: list[str] | None = None):
    """应用用户分档 + 顺序。levels: {key: level}; order: key 列表(决定展示顺序)。

    order 里缺的 key(将来新增的参数)会按出厂顺序补在末尾, 不会丢。
    """
    global FIELD_LEVELS
    levels = levels or {}
    by_key = {k: (cn, levels.get(k, lv)) for k, cn, lv in DEFAULT_FIELD_LEVELS}
    keys = [k for k in (order or []) if k in by_key]
    for k in by_key:
        if k not in keys:
            keys.append(k)
    FIELD_LEVELS = [(k, by_key[k][0], by_key[k][1]) for k in keys]


def set_field_levels(user: dict | None):
    """只改分档、保持现有顺序(向后兼容)。None/空 = 恢复出厂默认。"""
    apply_config(user, current_order())


def current_levels() -> dict:
    """当前分档 {key: level}（存盘 / 设置界面回显用）。"""
    return {k: lv for k, _cn, lv in FIELD_LEVELS}


def current_order() -> list[str]:
    """当前展示顺序(参数的 key 列表, 存盘用)。"""
    return [k for k, _cn, _lv in FIELD_LEVELS]

# 三档小标题(「对比」窗口与右侧清单共用同一文案)
SECTION_TITLES = {
    "fatal": "必须一致，否则无法无损合并",
    "warn": "建议一致，不同可能画面或声音出错",
    None: "仅供参考，不影响合并",
}

# 分组标题里展示的字段(空间有限): 致命项全要, 再加分辨率/帧率两个核心信息。
# 顺序即展示顺序: 视频编码 → 像素 → 音频 → HDR → 分辨率/帧率。
HEADER_FIELDS = ["video_codec", "pix_fmt", "audio_codec", "sample_fmt",
                 "sample_rate", "channels", "nb_streams", "color_transfer",
                 "width", "r_frame_rate"]


def field_level(key: str) -> str | None:
    for k, _cn, lv in FIELD_LEVELS:
        if k == key:
            return lv
    return None


def field_cn(key: str) -> str:
    for k, cn, _lv in FIELD_LEVELS:
        if k == key:
            return cn
    return key


def _fps_text(spec: dict) -> str:
    fps = fps_to_float(spec.get("r_frame_rate", "0/0"))
    if fps <= 0:
        return ""
    return f"{fps:.0f}fps" if abs(fps - round(fps)) < 0.05 else f"{fps:.2f}fps"


def _bitrate_text(v) -> str:
    try:
        n = float(v)
    except Exception:
        return ""
    if n <= 0:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} Mbps"
    return f"{n / 1000:.0f} kbps"


def _sample_rate_text(v) -> str:
    """采样率 -> 48kHz / 44.1kHz(整数值不带小数)。"""
    try:
        n = float(v)
    except Exception:
        # v226: 没探出采样率(None/缺失)不能写出字面量 "None" 占一段
        return "" if v is None else str(v)
    if n <= 0:
        return ""
    khz = n / 1000.0
    if abs(khz - round(khz)) < 0.05:
        return f"{round(khz)}kHz"
    return f"{khz:.1f}kHz"


def _level_text(level, codec) -> str:
    """ffprobe 的 level 整数 -> 4.1 / 5.0 这样的可读等级。"""
    try:
        n = int(level)
    except Exception:
        return ""
    if n <= 0 or n == -99:      # -99 = 未知
        return ""
    # HEVC 用 general_level_idc(120/150/153...), H.264 用 level_idc(41/51...),
    # AV1 用 seq_level_idx(1/5/8/12... 直接是等级序号, 不是 idc)。
    codec = str(codec or "").lower()
    if codec == "av1":
        return str(n)
    if codec in ("hevc", "h265"):
        return f"{n / 30:.1f}"
    return f"{n / 10:.1f}" if n < 100 else f"{n / 30:.1f}"


def field_value(spec: dict, key: str, compact: bool = False) -> str:
    """字段的展示值(像素格式附位深, 分辨率拼宽高, 旋转带度)。

    compact=True 时像素格式不再带「（10bit）」后缀 —— 对比清单里位深单独占一行,
    重复写会显得啰嗦; 分组标题仍用完整写法。
    """
    if not spec:
        return ""
    if key == "width":
        w, h = spec.get("width"), spec.get("height")
        if w and h:
            return f"{w}×{h}"
        return ""
    if key == "r_frame_rate":
        return _fps_text(spec)
    if key == "bits":
        bits = spec.get("bits")
        return f"{bits}bit" if bits else ""
    if key == "pix_fmt":
        v = spec.get("pix_fmt")
        if not v:
            return ""
        bits = spec.get("bits")
        if compact or not bits:
            return str(v)
        return f"{v}（{bits}bit）"
    if key == "level":
        return _level_text(spec.get("level"), spec.get("video_codec"))
    if key == "video_bitrate":
        return _bitrate_text(spec.get("video_bitrate"))
    if key == "bitrate_mode":
        # v198: 抽样判定的 CBR/VBR 标签(probe.bitrate_mode 写进 spec)
        return str(spec.get("bitrate_mode") or "")
    if key == "field_order":
        v = spec.get("field_order")
        if v is None or str(v).strip() == "":
            return ""   # 无此元数据时不臆断显示
        # 与 _norm_field_order(签名判定)共用归一化: unknown 按逐行处理,
        # 避免"签名当逐行、界面显示隔行"的自相矛盾(v257)。
        return "逐行" if _norm_field_order(v) == "progressive" else "隔行"
    if key == "time_base":
        v = spec.get("time_base")
        return str(v) if v else ""
    if key == "sample_rate":
        return _sample_rate_text(spec.get("sample_rate"))
    if key == "channels":
        v = spec.get("channels")
        try:
            return f"{int(v)}声道" if v else ""
        except Exception:
            return str(v)
    if key == "nb_streams":
        vs = spec.get("video_streams")
        as_ = spec.get("audio_streams")
        if vs is not None or as_ is not None:
            parts = []
            if vs:
                parts.append(f"{int(vs)}视频")
            if as_:
                parts.append(f"{int(as_)}音频")
            if parts:
                return "+".join(parts)
        v = spec.get("nb_streams")
        try:
            return f"{int(v)}流" if v else ""
        except Exception:
            return str(v)
    if key == "duration":
        return fmt_duration(spec.get("duration"))
    if key == "rotation":
        r = spec.get("rotation")
        return "" if r is None else f"{r}°"
    v = spec.get(key)
    if v is None:
        return ""
    return str(v)


def spec_parts(spec: dict, keys: list[str] | None = None) -> list[tuple[str, str | None]]:
    """分组标题规格分段: [(值, 等级), ...] 只写值、不写参数名, 值按致命度标色。"""
    keys = keys or HEADER_FIELDS
    parts = []
    for key in keys:
        val = field_value(spec, key)
        if not val:
            continue
        parts.append((val, field_level(key)))
    return parts


def compare_rows(specs: list[dict], *, compact: bool = True,
                 desc: bool = True) -> list[dict]:
    """逐参数对比数据(与 UI 无关): 右侧「参数对比」与右键「对比」共用这一份规则。

    返回 [{key, cn, level, same, values, media, sub}]，按三档分组 + 档内归类排序：
      大项顺序 = fatal → warn → None（desc=False 时整表反向）；
      档内顺序(v248) = media(video→audio→other) → sub(SUB_ORDER) → 出厂顺序,
      **不再把冲突项提到前面**(旧逻辑已移除)。
    values 里空值统一成 "—"，与表格里显示的一致。
    """
    items = []
    for key, cn, lv in FIELD_LEVELS:
        vals = [field_value(s, key, compact=compact) for s in specs]
        if all(not v for v in vals):
            continue                      # 全部没值的不占行
        if key == "rotation" and set(vals) == {"0°"}:
            continue                      # 无旋转不值得占一行
        if key == "field_order" and set(vals) == {"逐行"}:
            continue                      # 都是逐行(默认值)也不占一行
        same = len({v or "" for v in vals}) == 1
        media, sub = field_category(key)
        items.append({"key": key, "cn": cn, "level": lv, "same": same,
                      "media": media, "sub": sub,
                      "values": [v if v else "—" for v in vals]})

    # v198: 码率类型是「抽样估计值」, 特意不进 FIELD_LEVELS —— 否则用户能在
    # ⚙️ 里把它设成「必须一致」, 拿一个估计值去卡「能否无损合并」的判定。
    # 这里作为固定的「仅供参考」行追加, 三处对比 UI 自动一起显示。
    modes = [field_value(s, "bitrate_mode") for s in specs]
    if any(modes):
        media, sub = field_category("bitrate_mode")
        items.append({"key": "bitrate_mode", "cn": "码率类型", "level": None,
                      "same": len({v or "" for v in modes}) == 1,
                      "media": media, "sub": sub,
                      "values": [v if v else "—" for v in modes]})

    def rank(it):
        sec = 0 if it["level"] == "fatal" else 1 if it["level"] == "warn" else 2
        # v248: 档内按 media → sub 归类; 同一类的相对次序交给底层稳定排序(出厂顺序)。
        # 旧逻辑「冲突在前」已移除 —— 用户要求按类型归类, 不再把不同值提到前面。
        media = it.get("media", "other")
        subs = SUB_ORDER.get(media, [])
        sub_i = subs.index(it.get("sub")) if it.get("sub") in subs else len(subs)
        return (sec, MEDIA_ORDER.get(media, 9), sub_i)

    items.sort(key=rank)   # Python sort 稳定: 同 key 保持 FIELD_LEVELS 出厂顺序
    if not desc:
        items.reverse()
    return items


def diff_fields(specs: list[dict]) -> set[str]:
    """多段之间值不一致的字段 key 集合(空值视为「无」, 也能参与比对)。

    例: 一段有音轨(aac)另一段没有 → audio_codec 判不一致。
    """
    diff: set[str] = set()
    for key, _cn, _lv in FIELD_LEVELS:
        vals = {field_value(s, key) or "无" for s in specs if s}
        if len(vals) > 1:
            diff.add(key)
    return diff


def group_by_spec(items: list) -> list:
    """items: [(photo, spec), ...] 按当前列表顺序。
    返回分组列表: [{"key": sig, "photos":[...], "specs":[...], "mergeable": bool}]。
    同签名聚一组; 组内数量 >1 即 mergeable(可无损合并)。"""
    buckets = {}
    order = []
    for photo, spec in items:
        # 探测失败(spec=None)的文件无法判断规格, 各自独立、不强行合并
        sig = signature(spec) if spec else ("__unknown__", photo.path)
        if sig not in buckets:
            buckets[sig] = {"key": sig, "photos": [], "specs": []}
            order.append(sig)
        buckets[sig]["photos"].append(photo)
        buckets[sig]["specs"].append(spec)
    groups = [buckets[k] for k in order]
    for g in groups:
        g["mergeable"] = len(g["photos"]) > 1
    return groups


def build_merge_groups(items: list, specs: dict, errors: dict) -> list:
    """items: 视频列表(按当前顺序); specs/errors: path -> spec / 失败原因。

    返回渲染用的分组: [{"key","label","tag","note","photos","spec"}]。
    排序: 成员多的可无损组在前(3 段 > 2 段), 独一个的排最后。
    """
    raw = group_by_spec([(p, specs.get(p.path)) for p in items])
    groups = []
    unknown = []
    for g in raw:
        photos = g["photos"]
        spec = g["specs"][0] if g["specs"] else None
        if spec is None:
            unknown.extend(photos)      # 读不出规格的合并成一个"读取失败"组
            continue
        if len(photos) > 1:
            label, tag = "可无损合并", "lossless"
        else:
            label, tag = "独立", "single"
        groups.append({
            "key": g["key"], "label": label, "tag": tag, "note": spec_desc(spec),
            "parts": spec_parts(spec),
            "photos": photos, "spec": spec,
        })
    if unknown:
        groups.append({
            "key": ("__unknown__",), "label": "读取失败", "tag": "single",
            "note": "无法读取规格，不参与合并",
            "photos": unknown, "spec": None,
        })
    # 可无损组按人数降序, 单打独斗的保持原顺序排在最后
    mergeable = [g for g in groups if g["tag"] == "lossless"]
    mergeable.sort(key=lambda g: len(g["photos"]), reverse=True)
    singles = [g for g in groups if g["tag"] != "lossless"]
    return mergeable + singles


# ---------------------------------------------------------------- 展示用格式化
def fmt_duration(sec) -> str:
    """秒 -> 00:12 / 1:02:33"""
    try:
        total = int(round(float(sec)))
    except Exception:
        return ""
    if total <= 0:
        return ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_size(n) -> str:
    """字节 -> 128 MB"""
    try:
        v = float(n)
    except Exception:
        return ""
    if v <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return (f"{v:.0f} {unit}" if unit in ("B", "KB")
                    else f"{v:.1f} {unit}".replace(".0 ", " "))
        v /= 1024.0
    return ""


def spec_desc(spec: dict, skip=()) -> str:
    """规格一句话: h264 · 1920×1080 · 30fps · aac

    skip: 组标题已经展示过的字段 key —— 格子第二行不再重复这些项
    (用户口径: 分组标签打开了编码/分辨率, 文件名下面就不用再写一遍)。
    全被跳过时返回空串(由调用方决定是否只剩时长)。
    """
    if not spec:
        return "规格未知"
    parts = []
    codec = spec.get("video_codec")
    if codec and "video_codec" not in skip:
        parts.append(str(codec))
    w, h = spec.get("width"), spec.get("height")
    if w and h and "width" not in skip:
        parts.append(f"{w}×{h}")
    fps = fps_to_float(spec.get("r_frame_rate", "0/0"))
    if fps > 0 and "r_frame_rate" not in skip:
        parts.append(f"{fps:.0f}fps" if abs(fps - round(fps)) < 0.05 else f"{fps:.2f}fps")
    acodec = spec.get("audio_codec")
    if acodec and "audio_codec" not in skip:
        parts.append(str(acodec))
    if parts:
        return " · ".join(parts)
    # 老口径兜底(没传 skip 的调用方保持原样); 传了 skip 说明剩下的都归组标题, 留空即可
    return "" if skip else "规格未知"


def header_skip_keys(spec: dict) -> set:
    """组标题上**实际展示了**、而格子第二行会重复的字段 key(值非空才算展示)。

    组标题的规格串来自 HEADER_FIELDS; 这里只挑格子第二行用得到的那几个,
    且字段值为空时标题上本来就没写, 格子里要照常补上。
    """
    if not spec:
        return set()
    return {k for k in ("video_codec", "width", "r_frame_rate", "audio_codec")
            if field_value(spec, k)}


def cell_line(spec: dict, long: bool, duration=None, skip=()) -> str:
    """格子第二行: 小图标 '720p · 00:12' / 大图标 'h264 · 1280×720 · 25fps · aac · 00:12'。

    skip: 组标题已展示的字段 key —— 这些项不再重复; 全被跳过且没时长时返回空串
    (标题已经把规格写全了, 格子下面不留一行废字)。
    """
    if spec:
        if long:
            base = spec_desc(spec, skip=skip)
        else:
            base = "" if "width" in skip else (
                f"{spec.get('height')}p" if spec.get("height") else "?")
    else:
        base = "规格未知"
    d = fmt_duration(duration if duration is not None else (spec or {}).get("duration"))
    if base and d:
        return f"{base} · {d}"
    return base or d
