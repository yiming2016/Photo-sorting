# -*- coding: utf-8 -*-
"""时间字段模型: 合成字段定义、读取优先级、写入展开规则。

核心约定(对应需求 2.A 时区方案):
- EXIF 系列按"本地时间、不带时区"写入
- XMP / QuickTime / 文件系统系列按"带 +08:00 时区"写入, 由 exiftool 自动换算为 UTC
- 对未知字段, 沿用该字段原有值的风格(原值带时区就带, 不带就不带)
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

DEFAULT_TZ = timezone(timedelta(hours=8))  # 北京时间

DATETIME_RE = re.compile(r"^(\d{4})[-:](\d{2})[-:](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")

# 三个合成时间字段
TAKEN = "taken"
CREATED = "created"
MODIFIED = "modified"
SYNTHETIC = (TAKEN, CREATED, MODIFIED)

SYNTHETIC_LABEL = {
    TAKEN: "拍摄时间",
    CREATED: "创建时间",
    MODIFIED: "修改时间",
}

# 读取优先级: 依次尝试, 取第一个有值的
READ_PRIORITY = {
    TAKEN: [
        "EXIF:DateTimeOriginal",
        "EXIF:SubSecDateTimeOriginal",
        "XMP:DateTimeOriginal",
        "XMP:CreateDate",
        "XMP:DateCreated",
        "QuickTime:CreationDate",
        "QuickTime:CreateDate",
        "QuickTime:MediaCreateDate",
        "QuickTime:TrackCreateDate",
    ],
    CREATED: [
        "File:FileCreateDate",
        "EXIF:CreateDate",
        "XMP:MetadataDate",
        "File:FileModifyDate",
    ],
    MODIFIED: [
        "File:FileModifyDate",
        "EXIF:ModifyDate",
        "XMP:ModifyDate",
        "QuickTime:ModifyDate",
    ],
}

# 写入目标: 每个合成字段展开成的真实字段
WRITE_TARGETS = {
    TAKEN: [
        "EXIF:DateTimeOriginal",
        "EXIF:CreateDate",
        "XMP:DateTimeOriginal",
        "XMP:CreateDate",
        "XMP:DateCreated",
        "QuickTime:CreationDate",
        "QuickTime:CreateDate",
        "QuickTime:MediaCreateDate",
        "QuickTime:TrackCreateDate",
    ],
    CREATED: [
        "EXIF:CreateDate",
        "File:FileCreateDate",
    ],
    MODIFIED: [
        "EXIF:ModifyDate",
        "XMP:ModifyDate",
        "XMP:MetadataDate",
        "QuickTime:ModifyDate",
        "QuickTime:TrackModifyDate",
        "QuickTime:MediaModifyDate",
        "File:FileModifyDate",
    ],
}

# 文件系统的两个字段必须最后写(写元数据会刷新 mtime)
FILE_TIME_KEYS = ("File:FileModifyDate", "File:FileCreateDate")

# 不可写入的只读分组(File 组只有下面两个时间字段例外, 见 writable)
# ICC_Profile 是二进制色彩配置文件, 逐个标签改写会破坏 profile
READONLY_GROUPS = {"Composite", "System", "ExifTool", "Warning", "Error", "ICC_Profile"}
FILE_WRITABLE = ("File:FileModifyDate", "File:FileCreateDate")

# 由文件本身/文件结构决定、或者由软件自动维护的字段: 不该让人手改。
# - 文件类型/大小/权限: 由磁盘上那个文件客观决定, 写元数据改不了它
# - 图像几何/编码参数: 改了也不会真的改变像素, 只会让元数据与画面不一致, 甚至损坏文件
# - 缩略图/数据块偏移: 结构字段, 乱写直接把文件弄坏
# - XMP 文档身份/修订历史: Photoshop 等软件自动维护的审计记录, 手改没意义
# - 媒体时长/码率/帧率: 由实际音视频数据算出来的
READONLY_TAGS = {
    # 文件类型 / 大小 / 权限
    "filetype", "filetypeextension", "mimetype", "filesize", "filepermissions",
    # 图像几何 / 编码参数
    "imagewidth", "imageheight", "exifimagewidth", "exifimageheight",
    "pixelxdimension", "pixelydimension", "imagesize", "megapixels",
    "bitdepth", "bitspersample", "samplesperpixel", "photometricinterpretation",
    "compression", "encodingprocess", "exifbyteorder", "colortransform",
    "ycbcrpositioning", "colorspace", "imagecount", "totalframes",
    # 缩略图 / 数据块偏移
    "thumbnailimage", "thumbnailoffset", "thumbnaillength",
    "stripoffsets", "jpeginterchangeformat", "jpeginterchangeformatlength",
    # XMP 文档身份 / 修订历史
    "documentid", "instanceid", "originaldocumentid", "documentancestors",
    "format", "nativedigest",
    # 媒体时长 / 码率 / 帧率
    "duration", "videoframerate", "framerate", "avgbitrate", "moviedatasize",
    "timescale", "mediatimescale", "trackduration", "mediaduration",
}

# 前缀匹配的只读字段(XMP 修订历史 / 派生来源)
READONLY_TAG_PREFIXES = ("history", "derivedfrom")

# 右侧面板里优先展示的常用字段
COMMON_FIELDS = [
    "Make", "Model", "LensModel", "LensID", "FNumber", "ExposureTime", "ISO",
    "FocalLength", "Orientation", "Software", "Artist", "Copyright",
    "ImageDescription", "UserComment", "Title", "Description", "Subject",
    "Keywords", "Rating", "Label", "GPSPosition", "GPSLatitude", "GPSLongitude",
    "ImageWidth", "ImageHeight", "Duration", "MIMEType", "Rotation",
]

# 右侧"全部元数据"树里可准确翻译的字段标签 (exiftool 原始 tag → 中文)。
# 仅翻译"标签"部分(冒号后的字段名); 命名空间(EXIF/XMP/QuickTime 等)保留原样,
# 便于用户对照定位。原英文 key 仍保留在每行的 tooltip 上。
FIELD_CN = {
    # 相机 / 设备
    "Make": "厂商", "Model": "型号", "CameraModelName": "相机型号",
    "LensModel": "镜头型号", "LensID": "镜头型号ID", "LensMake": "镜头厂商",
    "Lens": "镜头", "LensInfo": "镜头信息", "LensSerialNumber": "镜头序列号",
    "SerialNumber": "机身序列号", "BodySerialNumber": "机身序列号",
    # 拍摄参数
    "FNumber": "光圈值", "Aperture": "光圈", "ApertureValue": "光圈值",
    "ExposureTime": "曝光时间", "ShutterSpeed": "快门速度", "ShutterSpeedValue": "快门速度值",
    "ISO": "感光度", "ISOSpeed": "感光度", "PhotographicSensitivity": "感光度",
    "FocalLength": "焦距", "FocalLengthIn35mmFormat": "等效35mm焦距",
    "FocalLengthIn35mmFilm": "等效35mm焦距",
    "ExposureProgram": "曝光程序", "ExposureCompensation": "曝光补偿", "ExposureBias": "曝光补偿",
    "ExposureMode": "曝光模式", "MeteringMode": "测光模式", "Flash": "闪光灯",
    "WhiteBalance": "白平衡", "WhiteBalanceMode": "白平衡模式", "LightSource": "光源",
    "SceneCaptureType": "场景类型", "Contrast": "对比度", "Saturation": "饱和度",
    "Sharpness": "锐度", "BrightnessValue": "亮度", "MaxApertureValue": "最大光圈",
    "GainControl": "增益控制", "SensingMethod": "感光方式", "CustomRendered": "自定义渲染",
    "DigitalZoomRatio": "数码变焦比", "SceneType": "场景类型", "CompressedBitsPerPixel": "压缩位数",
    "Rendering": "渲染", "ExposureIndex": "曝光指数", "SubjectDistance": "主体距离",
    "SubjectDistanceRange": "主体距离范围", "ColorSpace": "色彩空间", "BitDepth": "位深度",
    "SamplesPerPixel": "每像素采样数", "PhotometricInterpretation": "光度解释",
    "Orientation": "方向", "Rotation": "旋转", "ImageRotation": "图像旋转",
    # 时间
    "DateTimeOriginal": "拍摄时间", "CreateDate": "创建时间", "ModifyDate": "修改时间",
    "DateCreated": "创建日期", "MetadataDate": "元数据修改时间", "CreationDate": "创建时间",
    "ContentCreateDate": "内容创建时间", "TrackCreateDate": "轨道创建时间",
    "TrackModifyDate": "轨道修改时间", "MediaCreateDate": "媒体创建时间",
    "MediaModifyDate": "媒体修改时间", "FileModifyDate": "文件修改时间",
    "FileCreateDate": "文件创建时间", "FileAccessDate": "文件访问时间",
    "FileInodeChangeDate": "索引节点修改时间", "GPSDateStamp": "GPS日期",
    # 尺寸 / 文件
    "ImageWidth": "图像宽度", "ImageHeight": "图像高度", "ImageSize": "图像尺寸",
    "ExifImageWidth": "图像宽度", "ExifImageHeight": "图像高度", "Megapixels": "百万像素",
    "PixelXDimension": "像素宽度", "PixelYDimension": "像素高度",
    "Duration": "时长", "FileType": "文件类型", "FileTypeExtension": "文件扩展名",
    "MIMEType": "文件类型(MIME)", "FileName": "文件名", "Directory": "目录",
    "FilePermissions": "文件权限", "FileSize": "文件大小", "FileNumber": "文件编号",
    # GPS
    "GPSPosition": "GPS位置", "GPSLatitude": "GPS纬度", "GPSLongitude": "GPS经度",
    "GPSAltitude": "GPS海拔", "GPSDateTime": "GPS时间", "GPSMapDatum": "GPS基准",
    "GPSVersionID": "GPS版本",
    # 版权 / 作者 / 描述
    "Artist": "作者", "Copyright": "版权", "ImageDescription": "图像描述",
    "UserComment": "用户备注", "Comment": "备注", "Title": "标题",
    "Description": "描述", "Subject": "主题", "Keywords": "关键词",
    "Rating": "评分", "RatingPercent": "评分(百分比)", "Label": "标签",
    "Software": "处理软件", "ProcessingSoftware": "处理软件", "HostComputer": "计算机",
    "CameraOwnerName": "相机所有者", "CopyrightNotice": "版权声明",
    "Rights": "版权", "Creator": "创建者", "CreatorTool": "创建工具",
    "Instructions": "说明", "Credit": "署名", "Source": "来源",
    # 视频 / QuickTime
    "VideoFrameRate": "视频帧率", "FrameRate": "帧率", "AudioSampleRate": "音频采样率",
    "AudioBitsPerSample": "音频位深", "VideoCodec": "视频编码", "AudioCodec": "音频编码",
    "CompressorName": "压缩格式", "ImageCount": "帧数", "TotalFrames": "总帧数",
    "MovieDataSize": "视频数据大小", "AvgBitrate": "平均码率",
    # 色彩 / 配置
    "ProfileDescription": "色彩配置描述", "ProfileName": "配置文件名",
    "ICCProfileName": "ICC配置名", "ColorMode": "色彩模式",
    "ColorSpaceData": "色彩空间", "Gamma": "伽马值", "Temperature": "色温",
    "Tint": "色调", "Vibrance": "自然饱和度",
    # 其它常见
    "XPComment": "注释", "XPSubject": "主题", "XPKeywords": "关键词",
    "XPAuthor": "作者", "MakeAndModel": "厂商与型号", "ShortOwnerName": "所有者简称",
    "CameraLabel": "相机标签", "CameraID": "相机ID",
    # 分辨率细项 / 像素排布
    "XResolution": "水平分辨率", "YResolution": "垂直分辨率",
    "ResolutionUnit": "分辨率单位", "BitsPerSample": "每通道位深",
    "PlanarConfiguration": "平面配置", "ColorComponents": "颜色分量数",
    "YCbCrPositioning": "亮度色度位置", "ReferenceBlackWhite": "黑白基准",
    "ThumbnailImage": "缩略图数据", "PreviewImage": "预览图数据",
    "JpgFromRaw": "内嵌原始JPG", "StripOffsets": "图像数据偏移",
    "RowsPerStrip": "每行带高度", "StripByteCounts": "数据字节数",
    # 曝光 / 场景补充
    "SubSecTimeOriginal": "原始时间亚秒", "SubSecDateTimeOriginal": "原始时间(含亚秒)",
    "SubSecTime": "时间亚秒", "SubSecTimeDigitized": "数字化时间亚秒",
    "OffsetTime": "时区偏移", "OffsetTimeOriginal": "原始时区偏移",
    "OffsetTimeDigitized": "数字化时区偏移",
    "LensSpecification": "镜头规格", "OwnerName": "所有者名称",
    "SubjectArea": "主体区域", "FileSource": "文件来源",
    "InteropIndex": "互操作标识",
    # QuickTime / 视频音频细项
    "CompressorID": "压缩格式ID", "VideoCodecID": "视频编码ID",
    "AudioFormat": "音频格式", "AudioCodecID": "音频编码ID",
    "AudioChannels": "声道数", "ChannelCount": "声道数",
    "BitRate": "码率", "MediaDuration": "媒体时长",
    "TimeScale": "时基", "MediaTimeScale": "媒体时基",
    "TrackType": "轨道类型", "TrackID": "轨道ID", "HandlerType": "处理器类型",
    "PixelFormat": "像素格式", "Depth": "颜色深度",
    "ColorPrimaries": "色彩原色", "TransferCharacteristics": "传输特性",
    "MatrixCoefficients": "矩阵系数", "VideoFullRangeFlag": "视频全范围标记",
    "GPSCoordinates": "GPS坐标", "LocationInformation": "位置信息",
    "MajorBrand": "主品牌",
    "MinorVersion": "次版本", "CompatibleBrands": "兼容品牌",
    "HandlerDescription": "处理器说明", "Balance": "声道平衡",
    # 地点
    "City": "城市", "Country": "国家", "Province": "省份",
    "State": "州/省", "CountryCode": "国家代码", "Location": "地点",
    "GPSHPositioningError": "GPS水平定位误差",
}

# 可准确翻译的分组名 (命名空间); 标准缩写 EXIF/XMP/QuickTime/IPTC/PNG 等保留原样
GROUP_CN = {
    "Composite": "合成", "System": "系统", "ExifTool": "ExifTool工具",
    "Warning": "警告", "Error": "错误", "Other": "其它", "File": "文件",
}


def strip_tz(value: str) -> str:
    """去掉时区后缀, 只留 19 位主时间。"""
    return TZ_SUFFIX_RE.sub("", value).strip()


def parse_dt(value: str | None) -> datetime | None:
    """把 exiftool 输出的时间字符串解析成 datetime(带时区则保留时区)。"""
    if not value or not isinstance(value, str):
        return None
    m = DATETIME_RE.match(value.strip())
    if not m:
        return None
    try:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        rest = value.strip()[m.end():]
        tz = None
        tm = TZ_SUFFIX_RE.search(rest)
        if tm:
            token = tm.group(1)
            if token == "Z":
                tz = timezone.utc
            else:
                t = token.replace(":", "")
                sign = 1 if t[0] == "+" else -1
                hh = int(t[1:3])
                mm = int(t[3:5]) if len(t) >= 5 else 0
                # v196: 第三方设备偶尔写出 +99:99 / +24:00 / +08:99 这类畸形
                # 偏移。不校验的话 timezone() 会抛 ValueError —— 扫描排序没有
                # 兜底, 一个坏文件就能让整个文件夹加载不出来; 而 +08:99 不抛
                # 异常却被算成 +9h39m, 时间显示与写回会静默偏 1 小时 39 分。
                # 时区不可信就当「没写时区」, 日期时间主体照样保留。
                if hh <= 23 and mm <= 59:
                    tz = timezone(sign * timedelta(hours=hh, minutes=mm))
        return datetime(y, mo, d, h, mi, s, tzinfo=tz)
    except (ValueError, OverflowError, IndexError):
        return None


def parse_from_filename(name: str) -> datetime | None:
    """从 IMG_20220311_090700 / VID-20220311-WA0001 等文件名里兜底提取时间。"""
    pats = [
        r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})[-_ T]?(\d{2})[-:]?(\d{2})[-:]?(\d{2})",
        r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})",
    ]
    for pat in pats:
        m = re.search(pat, name)
        if not m:
            continue
        g = m.groups()
        try:
            if len(g) >= 6:
                return datetime(*(int(x) for x in g[:6]))
            return datetime(int(g[0]), int(g[1]), int(g[2]))
        except ValueError:
            continue
    return None


def local_naive(dt: datetime | None) -> datetime | None:
    """统一成本地时区的朴素时间, 便于比较与显示。"""
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(DEFAULT_TZ).replace(tzinfo=None)


def is_datetime_value(value) -> bool:
    return isinstance(value, str) and bool(DATETIME_RE.match(value.strip()))


def _group_of(key: str) -> str:
    return key.split(":", 1)[0] if ":" in key else "Other"


def _tag_of(key: str) -> str:
    return key.split(":", 1)[1] if ":" in key else key


def format_for(key: str, dt: datetime, old_value: str | None = None) -> str:
    """按字段所属分组决定写入格式。

    - EXIF: 本地时间, 不带时区
    - XMP / QuickTime / Track / Media: 带 +08:00
    - 其它: 沿用原值风格
    """
    group = _group_of(key).lower()
    tag = _tag_of(key).lower()
    local = dt.astimezone(DEFAULT_TZ) if dt.tzinfo else dt.replace(tzinfo=DEFAULT_TZ).astimezone(DEFAULT_TZ)
    if group in ("exif", "ifd0", "ifd1", "exif ifd", "subifd"):
        return local.strftime("%Y:%m:%d %H:%M:%S")
    if group in ("xmp", "xmp-exif", "xmp-tiff", "xmp-photoshop", "quicktime", "track", "track1",
                 "track2", "track3", "track4", "media", "keys", "itemlist", "userdata"):
        return local.strftime("%Y:%m:%d %H:%M:%S") + "+08:00"
    if group in ("file", "system"):
        return local.strftime("%Y:%m:%d %H:%M:%S") + "+08:00"
    # 未知分组: 沿用原值风格
    if old_value and TZ_SUFFIX_RE.search(old_value.strip()):
        return local.strftime("%Y:%m:%d %H:%M:%S") + "+08:00"
    return local.strftime("%Y:%m:%d %H:%M:%S")


def display(value: str | None) -> str:
    """统一显示格式: 2022-03-11 09:07:00"""
    dt = parse_dt(value)
    if dt is None:
        return str(value) if value else "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# 显示层专用: 中文/英文括号里的时区说明, 如 "2024-08-15 14:30:22（+08:00）"
_TZ_PAREN_RE = re.compile(r"[（(]\s*[+-]\d{1,2}:?\d{2}\s*[）)]\s*$")


def display_value(value) -> str:
    """UI 显示元数据原始值时去掉时区后缀, 只留日期时间主体。

    ⚠️ 只用于「显示」, 绝不用于写入 / 比较 / 解析:
    - 写入格式仍由 format_for() 决定(EXIF 不带时区, XMP/QuickTime/File 带 +08:00);
    - 非时间型值原样返回, 不影响文件大小、GPS、编码名等其它字段。
    v198: 用户要求界面里任何位置都不再显示东八时区。
    """
    s = "" if value is None else str(value)
    if not DATETIME_RE.match(s.strip()):
        return s
    s = _TZ_PAREN_RE.sub("", s).strip()
    return TZ_SUFFIX_RE.sub("", s).strip()


def group_sort_key(key: str) -> tuple:
    """分组显示顺序: 时间 → EXIF → XMP → QuickTime → 文件 → 其它"""
    order = ["EXIF", "XMP", "IPTC", "QuickTime", "Track", "Media", "Keys",
             "ItemList", "UserData", "PNG", "JFIF", "ICC_Profile", "Other"]
    g = _group_of(key)
    for i, name in enumerate(order):
        if g.lower().startswith(name.lower()):
            return (i, _tag_of(key).lower())
    return (len(order), key.lower())


def writable(key: str) -> bool:
    """这个元数据字段能不能由用户直接改写?

    判定顺序: 文件系统白名单 → 只读分组 → 只读字段表 → 其余视为可写。
    """
    group = _group_of(key)
    if group == "File":
        # File 组只有两个时间字段能写(其余是文件名/大小/权限这类客观信息)
        return key in FILE_WRITABLE
    if group in READONLY_GROUPS:
        return False
    tag = _tag_of(key).lower()
    if tag in READONLY_TAGS:
        return False
    if tag.startswith(READONLY_TAG_PREFIXES):
        return False
    return True
