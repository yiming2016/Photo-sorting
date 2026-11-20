# -*- coding: utf-8 -*-
"""用 ffprobe 读取视频/音频规格, 供「能否无损合并」判定使用。

两个历史坑(都已修):
- 打包成 --windowed 时进程没有控制台, 直接起控制台程序会弹黑窗 → 统一走 proc.run。
- 中文 Windows 下按 GBK 解码 ffprobe 的 UTF-8 输出(里面含中文文件名)会抛
  UnicodeDecodeError, 结果 stdout 空、返回码 0 → 曾显示成"ffprobe 返回码 0"。
  proc.run 强制 UTF-8 解码, 问题消除。
probe_ex 额外返回失败原因, 便于面板上显示"为什么读不出来"。
"""
import json
import os
import tempfile

from . import proc, norm_paths

LOG_NAME = "photosort_merge.log"


def log_line(text: str) -> None:
    """把诊断信息追加到 %TEMP%\\photosort_merge.log, 方便排查现场问题。"""
    try:
        p = os.path.join(tempfile.gettempdir(), LOG_NAME)
        with open(p, "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except Exception:
        pass


def probe_ex(path: str, ffprobe: str, *, with_bitrate_mode: bool = False,
             resolve_path: bool = True):
    """返回 (spec, err)。成功时 err 为空串; 失败时 spec 为 None、err 为简短原因。

    v198: with_bitrate_mode=True 时额外抽样判定码率类型(CBR/VBR), 只给需要
    展示的调用方开(主列表批量探测 / 属性窗口), 每次约 30~60 ms。
    v200: resolve_path=True 时, 若源文件已有统一格式工作副本, 探测副本而不是原片。
          规划转码时传 False —— 必须看原片参数才能判断要不要重做。
    """
    if not ffprobe:
        return None, "未找到 ffprobe"
    real_path = norm_paths.resolve(path) if resolve_path else path
    cmd = [ffprobe, "-v", "error", "-show_format", "-show_streams",
           "-of", "json", real_path]
    try:
        out = proc.run(cmd, timeout=120)
    except FileNotFoundError:
        return None, "找不到 ffprobe 程序"
    except Exception as e:
        # subprocess.TimeoutExpired 也走这里
        name = type(e).__name__
        if name == "TimeoutExpired":
            return None, "读取超时"
        log_line(f"[probe] 启动失败 {name}: {e} | ffprobe={ffprobe}")
        return None, f"启动失败：{e}"

    stdout = out.stdout or ""
    if out.returncode != 0 or not stdout.strip():
        err = (out.stderr or "").strip()
        last = err.splitlines()[-1] if err else ""
        # ffprobe 报错行通常以 "<文件路径>: " 开头, 去掉路径避免把列表撑宽
        for pref in (path, f'"{path}"', f"'{path}'"):
            if last.startswith(pref):
                last = last[len(pref):].lstrip(": ").strip()
                break
        if not last:
            last = (f"ffprobe 返回码 {out.returncode}，但没有输出任何内容")
        log_line(f"[probe] 失败 rc={out.returncode} file={path!r} "
                 f"stdout_len={len(stdout)} stderr={err[:400]!r}")
        return None, last[:160]
    try:
        data = json.loads(stdout)
    except Exception:
        log_line(f"[probe] json 解析失败 file={path!r} head={stdout[:200]!r}")
        return None, "ffprobe 输出无法解析"

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        return None, "不是视频（没有视频流）"
    # 流结构: 几条视频 / 几条音频(只比这两类; 字幕/data 流不影响画面声音, 不参与)
    video_streams = sum(1 for s in streams if s.get("codec_type") == "video")
    audio_streams = sum(1 for s in streams if s.get("codec_type") == "audio")

    def gi(d, k, default=None):
        v = d.get(k)
        return v if v is not None else default

    try:
        width = int(gi(video, "width", 0) or 0)
        height = int(gi(video, "height", 0) or 0)
    except Exception:
        width = height = 0

    duration = 0.0
    try:
        duration = float(data.get("format", {}).get("duration", 0) or 0)
    except Exception:
        pass

    # ---- 致命参数: 位深 / SAR / 色彩 / HDR / 旋转 / 编码档次 ----
    pix_fmt = gi(video, "pix_fmt")
    bits = _int_or(gi(video, "bits_per_raw_sample"))
    if bits is None:
        bits = _bits_from_pix_fmt(pix_fmt)

    rotation = None
    for sd in (video.get("side_data_list") or []):
        if str(sd.get("side_data_type", "")).lower() in ("display matrix", "displaymatrix"):
            rotation = _int_or(sd.get("rotation"))
            if rotation is not None:
                break
    if rotation is None:
        rotation = _int_or((video.get("tags") or {}).get("rotate"))

    spec = {
        "video_codec": gi(video, "codec_name"),
        "width": width,
        "height": height,
        "pix_fmt": pix_fmt,
        "bits": bits,
        "profile": gi(video, "profile"),
        "level": _int_or(gi(video, "level")),          # 41=h264 4.1 / 120=hevc 4.0
        "field_order": gi(video, "field_order"),       # progressive / tt / bb
        "time_base": gi(video, "time_base"),           # 时间基(1/30 与 1/90000 不同会抖动)
        "video_bitrate": _int_or(gi(video, "bit_rate")),
        "sar": gi(video, "sample_aspect_ratio"),
        "color_range": gi(video, "color_range"),
        "color_space": gi(video, "color_space"),
        "color_transfer": gi(video, "color_transfer"),
        "color_primaries": gi(video, "color_primaries"),
        "rotation": rotation,
        "r_frame_rate": gi(video, "r_frame_rate", "0/0"),
        "audio_codec": gi(audio, "codec_name") if audio else None,
        "audio_profile": gi(audio, "profile") if audio else None,   # LC / HE-AAC
        "sample_rate": gi(audio, "sample_rate") if audio else None,
        "channels": gi(audio, "channels") if audio else None,
        "sample_fmt": gi(audio, "sample_fmt") if audio else None,
        "channel_layout": gi(audio, "channel_layout") if audio else None,
        "nb_streams": len(streams),
        "video_streams": video_streams,
        "audio_streams": audio_streams,
        "format_name": data.get("format", {}).get("format_name"),
        "duration": duration,
    }
    if with_bitrate_mode:
        try:
            spec["bitrate_mode"] = bitrate_mode(real_path, ffprobe)
        except Exception:
            spec["bitrate_mode"] = "无法判断"
    return spec, ""


# v198: 逐秒码率变异系数 <= 这个阈值算「恒定码率」。
CBR_CV = 0.12


def bitrate_mode(path: str, ffprobe: str) -> str:
    """判断视频是恒定码率(CBR)还是可变码率(VBR)。

    ⚠️ 文件里通常**没有**写死 CBR/VBR 标记(exiftool 只给 MaxBitrate /
    AvgBitrate, ffprobe 只给 bit_rate), 只能按「前 20 秒逐秒码率波动」估计:
      - 自适应分桶(桶宽 0.2~1.0 秒, 至少 4 桶), 算每桶字节数的变异系数 CV;
      - CV <= 0.12 → 恒定码率（CBR）; CV > 0.12 → 可变码率（VBR）;
      - 读不到包 / 样本太少 → 无法判断。
    只读容器索引(不解码), 大文件也只读前 20 秒。结论是估计值, 不是元数据声明。
    """
    if not ffprobe:
        return "无法判断"
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-read_intervals", "%+20",
           "-show_entries", "packet=pts_time,size", "-of", "csv=p=0", path]
    try:
        out = proc.run(cmd, timeout=10)
    except Exception:
        return "无法判断"
    if out.returncode != 0:
        return "无法判断"
    pairs = []
    for line in (out.stdout or "").splitlines():
        cols = line.strip().split(",")
        if len(cols) < 2:
            continue
        try:
            t, size = float(cols[0]), float(cols[1])
        except ValueError:
            continue
        if size > 0:
            pairs.append((t, size))
    if len(pairs) < 8:
        return "无法判断"
    lo = min(t for t, _ in pairs)
    hi = max(t for t, _ in pairs)
    span = hi - lo
    width = max(0.2, min(1.0, span / 6)) if span > 0 else 0.0
    per_group = max(1, len(pairs) // 6)
    buckets: dict[int, float] = {}
    for i, (t, size) in enumerate(pairs):
        # pts 全相同时(少数容器不写 pts_time)退回按包序均分成 6 组
        k = int((t - lo) / width) if width > 0 else i // per_group
        buckets[k] = buckets.get(k, 0.0) + size
    vals = [v for _k, v in sorted(buckets.items())]
    if len(vals) < 4:
        return "无法判断"
    mean = sum(vals) / len(vals)
    if mean <= 0:
        return "无法判断"
    cv = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5 / mean
    return "恒定码率（CBR）" if cv <= CBR_CV else "可变码率（VBR）"


def _int_or(v, default=None):
    try:
        return int(v)
    except Exception:
        return default


def _bits_from_pix_fmt(pix_fmt):
    """从像素格式名推断位深: yuv420p→8, yuv420p10le→10, p010→10…"""
    if not pix_fmt:
        return None
    if pix_fmt in ("yuv420p", "yuvj420p", "yuv422p", "yuvj422p", "yuv444p",
                   "yuvj444p", "nv12", "nv21", "rgb24", "bgr24", "gray", "gbrp"):
        return 8
    for n in ("16", "12", "10"):
        if pix_fmt.endswith(n + "le") or pix_fmt.endswith(n + "be") \
                or pix_fmt.startswith("p0" + n):
            return int(n)
    return None


def probe(path: str, ffprobe: str):
    """兼容旧调用: 只要 spec。"""
    return probe_ex(path, ffprobe)[0]


def probe_keyframes(path: str, ffprobe: str, limit: int = 4000) -> list[float]:
    """列出视频的关键帧时间点(秒, 源时间轴), 升序。

    关键帧 = 能无损下刀的位置。给时间线画刻度、给裁剪做吸附用。
    大文件关键帧可能上万, 超过 limit 就截断(刻度够用即可)。
    失败返回空列表(调用方按"没有关键帧信息"处理, 不吸附)。
    """
    if not ffprobe:
        return []
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-skip_frame", "nokey", "-show_entries", "frame=pts_time",
           "-of", "csv=p=0", path]
    try:
        out = proc.run(cmd, timeout=180)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    ks: list[float] = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # csv 可能带 ",key" 之类的附加列
        first = line.split(",")[0].strip()
        try:
            t = float(first)
        except Exception:
            continue
        if t >= 0:
            ks.append(t)
        if len(ks) >= limit:
            break
    ks.sort()
    return ks


def probe_audio_stream(path: str, ffprobe: str) -> dict:
    """读音频流信息(codec / 采样率 / 声道 / 时长)。没有音轨返回 {}。"""
    if not ffprobe:
        return {}
    cmd = [ffprobe, "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=codec_name,sample_rate,channels",
           "-show_entries", "format=duration",
           "-of", "json", path]
    try:
        out = proc.run(cmd, timeout=60)
    except Exception:
        return {}
    if out.returncode != 0 or not (out.stdout or "").strip():
        return {}
    try:
        data = json.loads(out.stdout)
    except Exception:
        return {}
    streams = data.get("streams") or []
    if not streams:
        return {}
    a = streams[0]
    info = {
        "codec": a.get("codec_name"),
        "sample_rate": a.get("sample_rate"),
        "channels": a.get("channels"),
    }
    try:
        info["duration"] = float(data.get("format", {}).get("duration", 0) or 0)
    except Exception:
        info["duration"] = 0.0
    return info


def probe_duration(path: str, ffprobe: str) -> float:
    """只读总时长(秒)。音频文件没有视频流, probe_ex 会失败, 用这个。

    只取 format.duration, 不解析流, 比 probe_ex 轻。失败返回 0.0。
    """
    if not ffprobe:
        return 0.0
    cmd = [ffprobe, "-v", "error", "-show_format", "-of", "json", path]
    try:
        out = proc.run(cmd, timeout=60)
    except Exception:
        return 0.0
    if out.returncode != 0 or not (out.stdout or "").strip():
        return 0.0
    try:
        data = json.loads(out.stdout)
        return float(data.get("format", {}).get("duration", 0) or 0)
    except Exception:
        return 0.0
