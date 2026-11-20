# -*- coding: utf-8 -*-
"""剪辑页工具箱的底层能力(不碰界面, 可单独测)。

共五件事, 一条总原则: **能不重新编码就不重新编码**。

1. 改帧率 = 改播放速度(无损)
   ffmpeg 的 `-itsscale` 只缩放时间戳, 码流一个字节都不动(`-c copy`)。
   25fps → 50fps: 时长减半、播放快一倍, 声音跟着快、音调变高(和快进磁带一样)。
   帧数与倍速是同一件事: `倍速 = 新帧率 / 原帧率`, 界面上改一个另一个跟着变。

2. 大幅削减(只留关键帧, 无损)
   先用 `-discard nokey` 丢掉所有非关键帧得到裸流, 再按目标帧率重新封装。
   帧数只剩关键帧 → 时长大幅缩短、看起来像延时摄影。全程 copy, 不重编码。
   只支持 H.264 / HEVC(裸流要 annexb), 其他编码直接说清楚。

3. 波形: 解成 2kHz 单声道 PCM, 按桶取峰值(给时间线画包络用)。

4. 检测: 黑场 / 无声 / 场景变化 / 关键帧。前三个要整片解码, 会慢, 所以支持
   进度回调和取消; 关键帧只读索引, 很快。

5. 修复错误时长: 部分文件容器里写的时长和实际包对不上(播放器进度条乱跳)。
   重新封装(`-c copy`)让容器按真实包重算时长即可, 画面不动。

铁律: 子进程一律走 merge/proc(中文 Windows 下 text=True 会按 GBK 解码炸掉)。
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import tempfile

from ..merge import proc
from ..merge.grouping import fps_to_float

TOOL_TIMEOUT = 600          # 单个 ffmpeg 的兜底超时(秒)
ANNEXB_CODECS = ("h264", "hevc")   # 能走"裸流重封装"的编码


class ToolError(RuntimeError):
    """可以直接展示给用户看的错误(不用带 traceback)。"""


class Cancelled(RuntimeError):
    """用户点了取消。"""


# ---------------------------------------------------------------- 基础
def pair(ffmpeg: str | None = None, ffprobe: str | None = None):
    """(ffmpeg, ffprobe)。调用方通常已经查过, 没给就自己找一次。"""
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
    from ..merge import ffmpeg_locate
    try:
        found = ffmpeg_locate.find_ffmpeg_pair()
    except Exception:
        found = None
    if not found:
        raise ToolError("没找到 FFmpeg。这个功能要靠它干活。")
    return ffmpeg or found[0], ffprobe or found[1]


def _part_path(out: str) -> str:
    """临时文件要保留原扩展名, 否则 ffmpeg 认不出该封什么格式。"""
    stem, ext = os.path.splitext(out)
    return stem + ".part" + (ext or ".mp4")


def _guard_out(src: str, out: str):
    """v217: 产物路径绝不能等于源文件 —— 后面是 os.replace, 一撞就替掉原片。"""
    try:
        a = os.path.normcase(os.path.abspath(src))
        b = os.path.normcase(os.path.abspath(out))
    except Exception:
        return
    if a and a == b:
        raise ToolError("生成的产物不能和源文件同名，否则会覆盖原片。换一个输出位置再试。")


# ---------------------------------------------------------------- 产物存哪
def default_out_dir() -> str:
    """默认存处: 图片库下的「照片排序台剪辑件」(取不到就退回文档/用户目录)。"""
    from ..merge import settings
    base = os.path.expanduser("~")
    for name in ("Pictures", "Documents"):
        p = os.path.join(base, name)
        if os.path.isdir(p):
            base = p
            break
    return os.path.join(base, settings.OUT_SUB_NAME)


def out_dir_for(src: str) -> str:
    """按设置算出这个源文件的新文件该放哪个文件夹(目录不存在则建)。"""
    from ..merge import settings
    mode = settings.load_clip_out_mode()
    if mode == settings.OUT_SAME:
        d = os.path.dirname(os.path.abspath(src)) or "."
    elif mode == settings.OUT_CUSTOM:
        d = settings.load_clip_out_dir() or default_out_dir()
    else:
        d = os.path.join(os.path.dirname(os.path.abspath(src)) or ".",
                         settings.OUT_SUB_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.dirname(os.path.abspath(src)) or "."
    return d


def output_path_for(src: str, tag: str, ext: str = "") -> str:
    """给"由 src 生成的新文件"算一个不撞名的路径。

    tag 是中文后缀(如 `_50fps`、`_只留关键帧`、`_音频`), 扩展名默认跟源文件。
    同名已存在就加 (2)(3)…, 绝不覆盖老文件。
    """
    stem, e = os.path.splitext(os.path.basename(src))
    ext = ext or e or ".mp4"
    if ext and not ext.startswith("."):
        ext = "." + ext
    d = out_dir_for(src)
    base = f"{stem}{tag}{ext}"
    out = os.path.join(d, base)
    if not os.path.exists(out):
        return out
    n = 2                                # 撞名: 第二个叫 (2), 跟 Windows 一个习惯
    while True:
        cand = os.path.join(d, f"{stem}{tag}({n}){ext}")
        if not os.path.exists(cand):
            return cand
        n += 1


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise Cancelled("已取消")


def _tail(text: str, n: int = 400) -> str:
    lines = [x for x in (text or "").strip().splitlines() if x.strip()]
    return (lines[-1][:n] if lines else "")


def info(src: str, ffprobe: str | None = None, ffmpeg: str | None = None) -> dict:
    """读一份够用的规格: 帧率 / 编码 / 时长 / 有没有声音。"""
    from ..merge import probe as P
    _, fp = pair(ffmpeg, ffprobe)
    spec, err = P.probe_ex(src, fp)
    if not spec:
        # 音频文件没有视频流, probe_ex 会失败 —— 这里只关心"有没有声音"
        dur = P.probe_duration(src, fp)
        return {"fps": 0.0, "codec": "", "duration": dur, "has_audio": dur > 0,
                "spec": None, "err": err}
    fps = fps_to_float(spec.get("r_frame_rate") or "0/0")
    if not (0 < fps < 480):
        fps = fps_to_float(spec.get("avg_frame_rate") or "0/0")
    return {
        "fps": fps if 0 < fps < 480 else 0.0,
        "codec": (spec.get("video_codec") or "").lower(),
        "duration": float(spec.get("duration") or 0.0),
        "has_audio": bool(spec.get("audio_codec")),
        "width": int(spec.get("width") or 0),
        "height": int(spec.get("height") or 0),
        "spec": spec,
        "err": "",
    }


def probe_keyframes(src: str, ffprobe: str | None = None,
                    ffmpeg: str | None = None, limit: int = 4000) -> list[float]:
    from ..merge import probe as P
    _, fp = pair(ffmpeg, ffprobe)
    return P.probe_keyframes(src, fp, limit=limit)


def fps_round(v: float) -> float:
    """把 29.970029... 收成 29.97 这种能读的写法。"""
    if not v or v <= 0:
        return 0.0
    for std in (23.976, 24.0, 25.0, 29.97, 30.0, 48.0, 50.0, 59.94, 60.0,
                120.0, 240.0):
        if abs(v - std) < 0.02:
            return std
    return round(v, 3)


def speed_of_fps(orig_fps: float, new_fps: float) -> float:
    """新帧率对应的播放倍速。"""
    if not orig_fps or orig_fps <= 0 or not new_fps or new_fps <= 0:
        return 1.0
    return float(new_fps) / float(orig_fps)


def fps_of_speed(orig_fps: float, speed: float) -> float:
    """倍速对应的新帧率。"""
    if not orig_fps or orig_fps <= 0:
        return 0.0
    return float(orig_fps) * float(speed)


# ---------------------------------------------------------------- 1. 改帧率
def change_fps(src: str, out: str, new_fps: float,
               ffmpeg: str | None = None, ffprobe: str | None = None,
               cancel=None) -> dict:
    """把整段视频的时间戳整体缩放, 达到 new_fps 的播放速度(无损)。

    返回 {"ok", "speed", "before", "after", "out"}。
    """
    ff, fp = pair(ffmpeg, ffprobe)
    meta = info(src, fp, ff)
    orig = float(meta.get("fps") or 0.0)
    if orig <= 0:
        raise ToolError("读不出这个视频的帧率，换一个文件试试。")
    if new_fps <= 0:
        raise ToolError("帧率要大于 0。")
    speed = speed_of_fps(orig, new_fps)
    if abs(speed - 1.0) < 1e-4:
        raise ToolError("帧率没变，速度和原来一样。")
    _guard_out(src, out)
    _check_cancel(cancel)
    its = 1.0 / speed
    tmp = _part_path(out)
    cmd = [ff, "-y", "-v", "error", "-itsscale", f"{its:.8f}", "-i", src,
           "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero",
           "-movflags", "+faststart", tmp]
    res = proc.run(cmd, timeout=TOOL_TIMEOUT)
    if res.returncode != 0 or not os.path.exists(tmp):
        # 有些容器(字幕/附件流)整包 copy 到 mp4 会失败, 退回只搬图像与声音
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        cmd = [ff, "-y", "-v", "error", "-itsscale", f"{its:.8f}", "-i", src,
               "-map", "0:v:0?", "-map", "0:a?", "-c", "copy",
               "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", tmp]
        res = proc.run(cmd, timeout=TOOL_TIMEOUT)
    if res.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        raise ToolError("这个文件没能无损改动时间轴：" + (_tail(res.stderr) or "原因不明"))
    os.replace(tmp, out)
    after = info(out, fp, ff)
    return {
        "ok": True, "out": out, "speed": speed,
        "before": float(meta.get("duration") or 0.0),
        "after": float(after.get("duration") or 0.0),
        "fps_before": orig,
        "fps_after": float(after.get("fps") or new_fps),
    }


# ---------------------------------------------------------------- 2. 大幅削减
def _annexb_ext(codec: str) -> str:
    return codec   # -f h264 / -f hevc


def decimate(src: str, out: str, every: int = 1, out_fps: float | None = None,
             ffmpeg: str | None = None, ffprobe: str | None = None,
             progress=None, cancel=None) -> dict:
    """只保留关键帧(每 every 个留一个), 按 out_fps 重新封装 → 时长大幅缩短。

    every=1 走"整段丢非关键帧"的快路径(一次解码), every>1 逐帧抽取再拼接。
    """
    ff, fp = pair(ffmpeg, ffprobe)
    every = max(1, int(every or 1))
    _guard_out(src, out)
    meta = info(src, fp, ff)
    codec = str(meta.get("codec") or "").lower()
    if codec not in ANNEXB_CODECS:
        raise ToolError("这个视频的编码是 %s，只抽关键帧需要 H.264 或 HEVC。"
                        % (codec.upper() or "未知"))
    fps = float(meta.get("fps") or 0.0) or 25.0
    target_fps = float(out_fps or fps)
    if target_fps <= 0:
        target_fps = fps

    kfs = probe_keyframes(src, fp, ff, limit=100000)
    if not kfs:
        raise ToolError("读不到这个视频的关键帧，没法只抽关键帧。")
    keep = kfs[::every]
    total = len(keep)

    tmpdir = tempfile.mkdtemp(prefix="ps_decimate_")
    try:
        raw = os.path.join(tmpdir, "kf." + _annexb_ext(codec))
        bsf = ["-bsf:v", f"{codec}_mp4toannexb"]
        if every == 1:
            if progress:
                progress(5, "正在丢掉非关键帧…")
            cmd = [ff, "-y", "-v", "error", "-discard", "nokey", "-i", src,
                   "-map", "0:v:0", "-c", "copy"] + bsf + \
                  ["-f", _annexb_ext(codec), raw]
            res = proc.run(cmd, timeout=TOOL_TIMEOUT)
            if res.returncode != 0 or not os.path.exists(raw):
                raise ToolError("没能抽出关键帧：" + (_tail(res.stderr) or "原因不明"))
        else:
            parts = []
            for i, t in enumerate(keep):
                _check_cancel(cancel)
                if progress:
                    progress(3 + int(80 * i / max(1, total)),
                             f"正在取第 {i + 1}/{total} 个关键帧…")
                part = os.path.join(tmpdir, "p%05d.%s" % (i, codec))
                cmd = [ff, "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", src,
                       "-map", "0:v:0", "-frames:v", "1", "-c", "copy"] + bsf + \
                      ["-f", _annexb_ext(codec), part]
                res = proc.run(cmd, timeout=120)
                if res.returncode != 0 or not os.path.exists(part):
                    continue
                parts.append(part)
            if not parts:
                raise ToolError("一个关键帧都没取出来。")
            with open(raw, "wb") as dst:
                for p in parts:
                    with open(p, "rb") as f:
                        shutil.copyfileobj(f, dst)
        _check_cancel(cancel)
        if progress:
            progress(88, "正在按新帧率封装…")
        cmd = [ff, "-y", "-v", "error", "-r", f"{target_fps:.6f}", "-i", raw,
               "-c", "copy", "-movflags", "+faststart", out]
        res = proc.run(cmd, timeout=TOOL_TIMEOUT)
        if res.returncode != 0 or not os.path.exists(out):
            raise ToolError("封装失败：" + (_tail(res.stderr) or "原因不明"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    after = info(out, fp, ff)
    return {
        "ok": True, "out": out,
        "frames": total,
        "before": float(meta.get("duration") or 0.0),
        "after": float(after.get("duration") or 0.0),
        "fps": target_fps,
        "kept": total, "all_keyframes": len(kfs),
    }


# ---------------------------------------------------------------- 3. 波形
WAVE_RATE = 2000            # 采样率(只画包络, 2kHz 足够, 再高只是白占内存)
WAVE_BUCKETS = 1600         # 固定桶数, 与窗口宽度无关(绘制时再插值)


def waveform(src: str, buckets: int = WAVE_BUCKETS, rate: int = WAVE_RATE,
             ffmpeg: str | None = None, cancel=None) -> dict:
    """读整段音频的峰值包络(0~1)。没有音轨时 peaks 为空。"""
    ff, _ = pair(ffmpeg, None)
    _check_cancel(cancel)
    cmd = [ff, "-v", "error", "-i", src, "-map", "0:a:0",
           "-ac", "1", "-ar", str(int(rate)), "-f", "s16le", "-"]
    res = proc.run_bytes(cmd, timeout=TOOL_TIMEOUT)
    data = res.stdout or b""
    if res.returncode != 0 or len(data) < 2:
        return {"peaks": [], "duration": 0.0, "rate": rate}
    n = len(data) // 2
    data = data[:n * 2]
    buckets = max(16, int(buckets))
    step = max(1, n // buckets)
    peaks: list[float] = []
    m = 0
    i = 0
    # 小端 int16 流式扫一遍: 长视频也就几百万个样本, 后台线程里跑很快
    for (v,) in struct.iter_unpack("<h", data):
        if v < 0:
            v = -v
        if v > m:
            m = v
        i += 1
        if i % step == 0:
            peaks.append(m / 32768.0)
            m = 0
    if i % step:
        peaks.append(m / 32768.0)
    if peaks:
        top = max(peaks)
        if top > 0:
            peaks = [min(1.0, p / top) for p in peaks]   # 拉满, 小音量也看得见
    return {"peaks": peaks, "duration": n / float(rate), "rate": rate}


# ---------------------------------------------------------------- 4. 检测
_RE_BLACK_START = re.compile(r"black_start:\s*([0-9.]+)")
_RE_BLACK_END = re.compile(r"black_end:\s*([0-9.]+)")
_RE_SIL_START = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_RE_SIL_END = re.compile(r"silence_end:\s*([0-9.]+)")
_RE_PTS = re.compile(r"pts_time:\s*([0-9.]+)")
_RE_SCORE = re.compile(r"scene_score=\s*([0-9.]+)")
_RE_TIME = re.compile(r"out_time_ms=(\d+)")


def _stream_ffmpeg(ff: str, cmd: list, duration: float, on_line,
                   progress=None, cancel=None):
    """跑一条 ffmpeg 并逐行喂给 on_line; 同时按 out_time_ms 报进度。"""
    full = [ff, "-hide_banner", "-nostats", "-progress", "pipe:1"] + cmd
    p = proc.popen(full, stdout=-1, stderr=-2)   # 两路都读到 stderr
    try:
        fh = p.stderr or p.stdout
        for raw in iter(fh):
            _check_cancel(cancel)
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            on_line(line)
            m = _RE_TIME.search(line)
            if m and progress and duration > 0:
                try:
                    pct = min(99, 100.0 * (int(m.group(1)) / 1e6) / duration)
                    progress(int(pct), "")
                except Exception:
                    pass
        p.wait(timeout=TOOL_TIMEOUT)
    finally:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass


def detect_black(src: str, ffmpeg: str | None = None, ffprobe: str | None = None,
                 min_dur: float = 0.3, pix_th: float = 0.10,
                 progress=None, cancel=None) -> list[dict]:
    """黑场区间 [{"start", "end"}]。"""
    ff, fp = pair(ffmpeg, ffprobe)
    dur = float(info(src, fp, ff).get("duration") or 0.0)
    spans: list[dict] = []
    cur = None

    def on_line(line):
        # ⚠️ ffmpeg 会把 start 和 end 打在**同一行**:
        #    "black_start:0 black_end:2 black_duration:2"
        # 所以不能看到 start 就 return, 否则 end 永远读不到 → 整段被判成黑场。
        nonlocal cur
        ms = _RE_BLACK_START.search(line)
        me = _RE_BLACK_END.search(line)
        if ms:
            cur = float(ms.group(1))
        if me and cur is not None and float(me.group(1)) > cur:
            end = float(me.group(1))
            if end - cur >= min_dur:
                spans.append({"start": cur, "end": end})
            cur = None

    _stream_ffmpeg(ff, ["-i", src, "-an",
                        "-vf", f"blackdetect=d={min_dur:.2f}:pic_th=0.98:"
                               f"pix_th={pix_th:.2f}", "-f", "null", os.devnull],
                   dur, on_line, progress, cancel)
    if cur is not None and dur - cur >= min_dur:
        spans.append({"start": cur, "end": dur})
    return spans


def detect_silence(src: str, ffmpeg: str | None = None,
                   ffprobe: str | None = None, min_dur: float = 0.5,
                   noise_db: int = -35, progress=None, cancel=None) -> list[dict]:
    """静音区间 [{"start", "end"}]。"""
    ff, fp = pair(ffmpeg, ffprobe)
    meta = info(src, fp, ff)
    dur = float(meta.get("duration") or 0.0)
    if not meta.get("has_audio"):
        return []
    spans: list[dict] = []
    cur = None

    def on_line(line):
        nonlocal cur
        ms = _RE_SIL_START.search(line)
        me = _RE_SIL_END.search(line)
        if ms:
            cur = max(0.0, float(ms.group(1)))
        if me and cur is not None and float(me.group(1)) > cur:
            end = float(me.group(1))
            if end - cur >= min_dur:
                spans.append({"start": cur, "end": end})
            cur = None

    _stream_ffmpeg(ff, ["-i", src, "-vn",
                        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur:.2f}",
                        "-f", "null", os.devnull], dur, on_line, progress, cancel)
    if cur is not None and dur - cur >= min_dur:
        spans.append({"start": cur, "end": dur})
    return spans


def detect_scene(src: str, ffmpeg: str | None = None, ffprobe: str | None = None,
                 threshold: float = 0.35, progress=None, cancel=None) -> list[dict]:
    """画面切换点 [{"t", "score"}]。"""
    ff, fp = pair(ffmpeg, ffprobe)
    dur = float(info(src, fp, ff).get("duration") or 0.0)
    out: list[dict] = []
    last_t = None

    def on_line(line):
        nonlocal last_t
        m = _RE_PTS.search(line)
        if m:
            last_t = float(m.group(1))
            return
        m = _RE_SCORE.search(line)
        if m and last_t is not None:
            out.append({"t": last_t, "score": float(m.group(1))})
            last_t = None

    _stream_ffmpeg(ff, ["-i", src, "-an",
                        "-vf", f"select='gt(scene,{threshold:.3f})',metadata=print",
                        "-f", "null", os.devnull], dur, on_line, progress, cancel)
    return out


def detect_all(src: str, kinds: list[str], ffmpeg: str | None = None,
               ffprobe: str | None = None, progress=None, cancel=None,
               scene_th: float = 0.35) -> dict:
    """一次跑多项检测; progress 会按项均分。"""
    ff, fp = pair(ffmpeg, ffprobe)
    order = [k for k in ("black", "silence", "scene", "keyframes") if k in kinds]
    result = {"black": [], "silence": [], "scene": [], "keyframes": []}
    total = max(1, len(order))
    for i, kind in enumerate(order):
        _check_cancel(cancel)

        def sub(pct, _msg, i=i):
            if progress:
                progress(int((i * 100 + max(0, min(100, pct))) / total), "")

        if kind == "black":
            result["black"] = detect_black(src, ff, fp, progress=sub, cancel=cancel)
        elif kind == "silence":
            result["silence"] = detect_silence(src, ff, fp, progress=sub, cancel=cancel)
        elif kind == "scene":
            result["scene"] = detect_scene(src, ff, fp, threshold=scene_th,
                                           progress=sub, cancel=cancel)
        elif kind == "keyframes":
            if progress:
                progress(int((i * 100 + 50) / total), "")
            result["keyframes"] = probe_keyframes(src, fp, ff, limit=20000)
    return result


# ---------------------------------------------------------------- 5. 修复时长
def declared_duration(src: str, ffprobe: str | None = None,
                      ffmpeg: str | None = None) -> float:
    """容器自己写的时长(可能不准)。"""
    from ..merge import probe as P
    _, fp = pair(ffmpeg, ffprobe)
    return P.probe_duration(src, fp)


def real_duration(src: str, ffprobe: str | None = None,
                  ffmpeg: str | None = None) -> float:
    """按包里真实的时间戳算出来的结尾(容器写错时以它为准)。"""
    _, fp = pair(ffmpeg, ffprobe)
    cmd = [fp, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0", src]
    res = proc.run(cmd, timeout=TOOL_TIMEOUT)
    if res.returncode != 0:
        return 0.0
    end = 0.0
    for line in (res.stdout or "").splitlines():
        parts = line.strip().split(",")
        if len(parts) < 1 or not parts[0]:
            continue
        try:
            t = float(parts[0])
        except Exception:
            continue
        d = 0.0
        if len(parts) > 1:
            try:
                d = float(parts[1])
            except Exception:
                d = 0.0
        if t + d > end:
            end = t + d
    return end


def duration_problem(src: str, ffprobe: str | None = None,
                     ffmpeg: str | None = None) -> dict:
    """{"declared", "real", "bad", "diff"} —— 差的超过 0.25 秒就算写错了。"""
    decl = declared_duration(src, ffprobe, ffmpeg)
    real = real_duration(src, ffprobe, ffmpeg)
    diff = (real - decl) if (decl > 0 and real > 0) else 0.0
    return {"declared": decl, "real": real,
            "bad": bool(decl > 0 and real > 0 and abs(diff) > 0.25),
            "diff": diff}


def fix_duration(src: str, out: str | None = None, ffmpeg: str | None = None,
                 ffprobe: str | None = None, cancel=None) -> dict:
    """重新封装, 让容器按真实包重算时长(画面不动)。原文件不改。

    返回 {"ok", "out", "before", "after", "real"}。
    """
    ff, fp = pair(ffmpeg, ffprobe)
    _check_cancel(cancel)
    before = duration_problem(src, fp, ff)
    if out is None:
        stem, ext = os.path.splitext(src)
        out = stem + "（修复时长）" + (ext or ".mp4")
    _guard_out(src, out)
    tmp = _part_path(out)
    cmd = [ff, "-y", "-v", "error", "-fflags", "+genpts", "-i", src,
           "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero"]
    if os.path.splitext(out)[1].lower() in (".mp4", ".mov", ".m4v"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(tmp)
    res = proc.run(cmd, timeout=TOOL_TIMEOUT)
    if res.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise ToolError("重新封装失败：" + (_tail(res.stderr) or "原因不明"))
    os.replace(tmp, out)
    after = duration_problem(out, fp, ff)
    return {"ok": True, "out": out,
            "before": before["declared"], "after": after["declared"],
            "real": after["real"], "still_bad": after["bad"]}
