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
READONLY_GROUPS = {"Composite", "System", "ExifTool", "Warning", "Error"}
FILE_WRITABLE = ("File:FileModifyDate", "File:FileCreateDate")

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
    "GainControl": "增益控制", "SensingMethod": "感光元件", "CustomRendered": "自定义渲染",
    "DigitalZoomRatio": "数码变焦比", "SceneType": "场景类型", "CompressedBitsPerPixel": "压缩比",
    "Rendering": "渲染", "ExposureIndex": "曝光指数", "SubjectDistance": "主体距离",
    "SubjectDistanceRange": "主体距离范围", "ColorSpace": "色彩空间", "BitDepth": "位深度",
    "SamplesPerPixel": "每像素采样数", "PhotometricInterpretation": "色彩解释",
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
    "ColorSpaceData": "色彩空间", "Gamma": "伽马", "Temperature": "色温",
    "Tint": "色调", "Vibrance": "自然饱和度",
    # 其它常见
    "XPComment": "注释", "XPSubject": "主题", "XPKeywords": "关键词",
    "XPAuthor": "作者", "MakeAndModel": "厂商与型号", "ShortOwnerName": "所有者简称",
    "CameraLabel": "相机标签", "CameraID": "相机ID",
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
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    rest = value.strip()[m.end():]
    tz = None
    tm = TZ_SUFFIX_RE.search(rest)
    if tm:
        token = tm.group(1)
        if token == "Z":
            tz = timezone.utc
        else:
            token = token.replace(":", "")
            sign = 1 if token[0] == "+" else -1
            hh = int(token[1:3])
            mm = int(token[3:5]) if len(token) >= 5 else 0
            tz = timezone(sign * timedelta(hours=hh, minutes=mm))
    try:
        return datetime(y, mo, d, h, mi, s, tzinfo=tz)
    except ValueError:
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
    if key in FILE_WRITABLE:
        return True
    return _group_of(key) not in READONLY_GROUPS
