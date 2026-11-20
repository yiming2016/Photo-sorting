# -*- coding: utf-8 -*-
"""剪辑导出: 无损拼接(默认) 与 转码合成(被警告过才走)。

两条路完全不同, 别混着用:
- **无损**: 每段单独 `-c copy` 抽出来(切点已贴关键帧, 所以开头一定是完整的一帧),
  再用 concat demuxer 拼起来。不重新编码 → 秒级完成、画质零损失。
- **转码**: 一条 filter_complex 把所有片段按时间轴合成(黑底 + overlay 定位 +
  amix 混音)。这一条就是"传统剪辑软件"的路子, 慢、画质有损 —— 所以
  `core/lossless.py` 判定为不能无损时必须先弹警告, 用户点头才走这里。

铁律: 子进程一律走 `merge.proc.popen`(中文 Windows 下 text=True 会按 GBK 解码炸掉)。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

from . import pathsafe, proc, norm_paths

_EPS = 1e-4


def _src(seg) -> str:
    """v200: 剪辑时间线仍记原文件路径; 真正喂 ffmpeg 时用统一格式工作副本。"""
    return norm_paths.resolve(getattr(seg, "source", ""))


def _escape(p: str) -> str:
    return p.replace("'", "'\\''")


def _terminate(p) -> None:
    try:
        p.terminate()
        p.wait(timeout=3)
        return
    except Exception:
        pass
    try:
        p.kill()
        p.wait(timeout=3)
    except Exception:
        pass


def _read_err(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _fps_of(spec: dict | None) -> float:
    try:
        from .grouping import fps_to_float
        v = fps_to_float((spec or {}).get("r_frame_rate", "0/0"))
        return v if 0 < v < 120 else 30.0
    except Exception:
        return 30.0


def _audio_of(spec: dict | None) -> tuple[int, str]:
    """(采样率, 声道布局); 读不到就按 48k 立体声。"""
    spec = spec or {}
    try:
        rate = int(float(spec.get("sample_rate") or 0)) or 48000
    except Exception:
        rate = 48000
    try:
        ch = int(spec.get("channels") or 0)
    except Exception:
        ch = 0
    layout = "mono" if ch == 1 else "stereo"
    return rate, layout


# ---------------------------------------------------------------- 无损
def _has_audio(spec: dict | None) -> bool:
    return bool((spec or {}).get("audio_codec"))


def _cut_cmd(ffmpeg: str, seg, part: str, spec: dict | None) -> list:
    """一段的 copy 命令: 只切不编。

    静音段: 视频照旧 copy, 音轨用 anullsrc 现生成(只编音频, 很快, 画面仍无损)。

    ⚠️ ffmpeg 的位置参数规则: **所有 -i 必须先写完, 再写 -map**。
    把 `-map 0:v:0` 插在第二个 `-i`(anullsrc) 前面, ffmpeg 会认为这个 -map 是
    给下一个输入用的 → 报 "Option map cannot be applied to input url anullsrc"。
    (v120 修: 静音段以前 100% 切不出来。)
    """
    dur = max(0.01, seg.duration)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-progress", "pipe:1"]
    if seg.kind == "image":
        cmd += ["-loop", "1", "-t", f"{dur:.6f}", "-i", _src(seg)]
    else:
        cmd += ["-ss", f"{seg.src_in:.6f}", "-t", f"{dur:.6f}", "-i", _src(seg)]
    src_audio = _has_audio(spec)
    # 静音 → 补一条同参数的静音轨(画面仍是 copy, 只有这几秒的音频是新生成的);
    # 源本身就没音轨 → 不补, 输出纯视频(无损路径下所有段要么全有要么全有, 不会混)。
    gen_silence = bool(seg.muted)
    if gen_silence:
        rate, layout = _audio_of(spec)
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={rate}:cl={layout}"]
    # ---- 输入写完, 下面才轮到 -map ----
    cmd += ["-map", "0:v:0"]
    if gen_silence:
        cmd += ["-map", "1:a", "-shortest", "-c:a", "aac", "-b:a", "128k"]
    elif src_audio:
        cmd += ["-map", "0:a:0"]
    cmd += ["-c:v", "copy", "-avoid_negative_ts", "make_zero"]
    if seg.speed and abs(seg.speed - 1.0) > 1e-6:      # 无损路径不该出现, 兜个底
        cmd += ["-c:v", "libx264", "-preset", "veryfast"]
    cmd += [part]
    return cmd


# ------------------------------------------------- v185 变速段: 导出时才落地
def _fast_cmds(ffmpeg: str, seg, part: str, spec: dict | None,
               tmp: str) -> list:
    """变速段的两步命令 —— **两步都是流复制, 画面一帧都不重新编码**。

    剪辑期只在时间线上记一个倍速; 到导出这一刻才:
      ① 按源时间轴切出 [src_in, src_in + 时间线时长×倍速) 这一截(无损)
      ② -itsscale 1/倍速 把整段时间轴按比例压缩(无损) → 时长正好是
         时间线时长, 等效帧率 = 源帧率 × 倍速
    这跟以前「按钮提前生成一份新素材」是同一招, 只是挪到导出时、且只作用于
    这一段, 不再动素材文件、不再波及其它片段。
    """
    import copy as _copy
    sp = float(seg.speed or 1.0) or 1.0
    mid = part + ".raw.mp4"
    raw = _copy.copy(seg)
    raw.duration = max(0.01, seg.duration * sp)   # ① 要读的源时间长度
    raw.speed = 1.0
    c1 = _cut_cmd(ffmpeg, raw, mid, spec)
    c2 = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
          "-progress", "pipe:1",
          "-itsscale", f"{1.0 / sp:.8f}", "-i", mid,
          "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero"]
    if os.path.splitext(part)[1].lower() == ".mp4":
        c2 += ["-movflags", "+faststart"]
    c2 += [part]
    return [c1, c2]


def _fast_reencode_cmd(ffmpeg: str, seg, part: str, spec: dict | None) -> list:
    """兜底: 变速段的时间轴压缩件和其它段拼不上时, **只重编这一段**。

    显式对齐到整条片子的尺寸/帧率/采样率, 别的段仍然是 copy 的无原件。
    (不同帧率的 mp4 用 concat 拼, 大多数时候没问题; 真拼不上才走这里。)
    """
    sp = float(seg.speed or 1.0) or 1.0
    src_dur = max(0.01, seg.duration * sp)
    spec = spec or {}
    try:
        W = int(float(spec.get("width") or 0)) or 1920
        H = int(float(spec.get("height") or 0)) or 1080
    except (TypeError, ValueError):
        W, H = 1920, 1080
    fps = _fps_of(spec)
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:.6f},"
          f"format=yuv420p,setpts=(PTS-STARTPTS)/{sp:.6f}")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-progress", "pipe:1",
           "-ss", f"{seg.src_in:.6f}", "-t", f"{src_dur:.6f}", "-i", _src(seg),
           "-map", "0:v:0", "-vf", vf, "-t", f"{seg.duration:.6f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-r", f"{fps:.6f}"]
    if _has_audio(spec) and not seg.muted:
        af = (_tempo_chain(sp) +
              ",aformat=sample_rates=48000:channel_layouts=stereo")
        cmd += ["-map", "0:a:0", "-af", af,
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    cmd += ["-avoid_negative_ts", "make_zero", part]
    return cmd


def _lossless(ffmpeg: str, plan, out_path: str, specs: dict | None,
              progress=None, cancelled=None, fix_audio: bool = False) -> tuple[bool, str]:
    segs = [s for s in plan.segments if s.duration > _EPS]
    if not segs:
        return False, "没有可导出的片段"
    specs = specs or {}
    n = len(segs)
    per = 85.0 / n if n else 85.0       # 切割阶段占 0~85%, 拼接占剩余
    fast_any = any(abs(float(s.speed or 1.0) - 1.0) > 1e-6 for s in segs)
    with tempfile.TemporaryDirectory(prefix="psx_") as tmp:

        def _run(cmd, err, src_dur=0.0, base=0.0, per_part=0.0):
            """跑一条 ffmpeg; 若给了 src_dur 就解析 -progress 回报段内进度。

            返回 (returncode, 是否被取消)。切割是 -c copy, 单段可能跑好几秒,
            不解析进度就会"卡在 0、跑完瞬跳满" —— 所以这里把 ffmpeg 的
            out_time_us 按源时长换算成"段内百分比", 映射到 base~base+per_part。
            """
            cflag = False
            try:
                with open(err, "wb") as ef:
                    p = proc.popen(cmd, stdout=subprocess.PIPE, stderr=ef)
                    try:
                        for raw in iter(p.stdout.readline, b""):
                            if cancelled is not None and cancelled():
                                cflag = True
                                break
                            if progress is not None and src_dur > 0:
                                line = raw.decode("utf-8", "replace").strip()
                                if line.startswith("out_time_us="):
                                    try:
                                        us = int(line.split("=", 1)[1])
                                    except Exception:
                                        continue
                                    if us < 0:
                                        continue
                                    done = us / 1_000_000.0
                                    frac = min(1.0, done / src_dur)
                                    pct = base + frac * per_part
                                    progress(int(min(99, pct)), -1.0)
                    finally:
                        try:
                            p.stdout.close()
                        except Exception:
                            pass
                        if cflag:
                            _terminate(p)
                        else:
                            try:
                                p.wait()
                            except Exception:
                                pass
                    return p.returncode, cflag
            except FileNotFoundError:
                return -1, cflag
            except Exception:
                return -2, cflag

        def _make_parts(reencode_fast: bool = False):
            made: list[str] = []
            for i, seg in enumerate(segs):
                if cancelled is not None and cancelled():
                    return None, "已取消"
                part = os.path.join(tmp, f"p{i:03d}.mp4")
                base = i * per
                sp = float(seg.speed or 1.0) or 1.0
                fast = abs(sp - 1.0) > 1e-6
                errs: list[str] = []
                if fast and reencode_fast:
                    cmd = _fast_reencode_cmd(ffmpeg, seg, part,
                                             specs.get(seg.source))
                    e = os.path.join(tmp, f"e{i:03d}.txt")
                    errs = [e]
                    rc, cf = _run(cmd, e, seg.duration * sp, base, per)
                elif fast:
                    c1, c2 = _fast_cmds(ffmpeg, seg, part,
                                        specs.get(seg.source), tmp)
                    e1 = os.path.join(tmp, f"e{i:03d}a.txt")
                    e2 = os.path.join(tmp, f"e{i:03d}b.txt")
                    errs = [e1, e2]
                    # 两步都是流复制: 第一步切源(占 80%), 第二步 -itsscale 重排(20%)
                    rc, cf = _run(c1, e1, seg.duration * sp, base, per * 0.8)
                    if rc == 0 and not cf:
                        rc, cf = _run(c2, e2, seg.duration * sp,
                                      base + per * 0.8, per * 0.2)
                else:
                    cmd = _cut_cmd(ffmpeg, seg, part, specs.get(seg.source))
                    e = os.path.join(tmp, f"e{i:03d}.txt")
                    errs = [e]
                    rc, cf = _run(cmd, e, seg.duration, base, per)
                if cf:
                    return None, "已取消"
                if rc != 0 or not os.path.exists(part):
                    tail_lines: list[str] = []
                    for e in errs:
                        t = _read_err(e).strip().splitlines()[-6:]
                        tail_lines.extend(t)
                    tail = "\n".join(tail_lines)
                    if tail:
                        return None, f"第 {i + 1} 段切不出来：\n{tail}"
                    return None, f"第 {i + 1} 段切不出来（ffmpeg {rc}）"
                made.append(part)
                if progress is not None:
                    progress(int((i + 1) / n * 85), -1.0)
            return made, ""

        # v256: 拼接先写同目录临时文件, 全成功才 os.replace 到 out_path。
        # 用户填了已存在的输出名并确认覆盖后, 取消/失败不再删掉他的旧文件。
        ext_out = os.path.splitext(out_path)[1].lower()
        tmp_out = pathsafe.temp_output(out_path)

        def _drop_tmp():
            """只删本次的临时半成品, 绝不碰用户原有的 out_path。"""
            try:
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)
            except OSError:
                pass

        def _concat(parts) -> tuple[bool, str]:
            if len(parts) == 1:
                import shutil
                shutil.copyfile(parts[0], tmp_out)
            else:
                list_path = os.path.join(tmp, "concat.txt")
                with open(list_path, "w", encoding="utf-8") as f:
                    for pth in parts:
                        f.write(f"file '{_escape(pth)}'\n")
                err = os.path.join(tmp, "concat_err.txt")
                cmd = [ffmpeg, "-y", "-hide_banner", "-nostats", "-loglevel", "error",
                       "-f", "concat", "-safe", "0", "-i", list_path]
                if fix_audio:
                    # 视频流复制; 音频只重编一次, 消掉每段 AAC priming 造成的间隙。
                    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                            "-ar", "48000", "-af", "aresample=async=1:first_pts=0"]
                else:
                    cmd += ["-c", "copy"]
                if ext_out == ".mp4":
                    cmd += ["-movflags", "+faststart"]
                cmd += [tmp_out]
                try:
                    with open(err, "wb") as ef:
                        p = proc.popen(cmd, stdout=subprocess.DEVNULL, stderr=ef)
                        # v217: 以前这里 p.wait() 一等等到底, 取消按钮在这段时间里
                        # 完全没反应(拼接恰好是最后 15%, 用户以为卡死了)。
                        while True:
                            try:
                                p.wait(timeout=0.2)
                                break
                            except subprocess.TimeoutExpired:
                                if cancelled is not None and cancelled():
                                    _terminate(p)
                                    _drop_tmp()
                                    return False, "已取消"
                except Exception as e:
                    _drop_tmp()
                    return False, f"拼接失败: {e}"
                if p.returncode != 0:
                    _drop_tmp()
                    tail = "\n".join(_read_err(err).strip().splitlines()[-8:])
                    return False, f"拼接失败：\n{tail}"
            if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) == 0:
                _drop_tmp()
                return False, "输出文件没生成"
            # 全部成功才原子替换到用户指定的输出位置(覆盖旧文件是用户确认过的)
            try:
                os.replace(tmp_out, out_path)
            except OSError as e:
                _drop_tmp()
                return False, f"写入输出文件失败: {e}"
            return True, out_path

        parts, msg = _make_parts(False)
        if parts is None:
            return False, msg
        ok, msg = _concat(parts)
        if ok:
            if progress is not None:
                progress(100, 0.0)
            return True, out_path
        # 变速段时间轴压缩件的等效帧率和别的段不一样, 偶尔拼不上 ——
        # 只把这几段重新生成成对齐规格的编码件再拼一次, 其它段仍是无损原件。
        if fast_any:
            parts2, msg2 = _make_parts(True)
            if parts2 is not None:
                ok2, msg2 = _concat(parts2)
                if ok2:
                    return True, out_path
        return False, msg
    return False, "导出没完成"


# ---------------------------------------------------------------- 转码
def _tempo_chain(speed: float) -> str:
    """atempo 只吃 0.5~2.0, 超出要串几个。"""
    if speed <= 0:
        return "atempo=1.0"
    chain = []
    left = speed
    while left > 2.0:
        chain.append("atempo=2.0")
        left /= 2.0
    while left < 0.5:
        chain.append("atempo=0.5")
        left /= 0.5
    chain.append(f"atempo={left:.4f}")
    return ",".join(chain)


def _reencode(ffmpeg: str, plan, out_path: str, specs: dict | None,
              progress=None, cancelled=None) -> tuple[bool, str]:
    segs = [s for s in plan.segments if s.duration > _EPS]
    if not segs:
        return False, "没有可导出的片段"
    specs = specs or {}

    base_spec = None
    for s in segs:
        sp = specs.get(s.source)
        if sp:
            base_spec = sp
            break
    try:
        W = int((base_spec or {}).get("width") or 0) or 1920
        H = int((base_spec or {}).get("height") or 0) or 1080
    except Exception:
        W, H = 1920, 1080
    fps = _fps_of(base_spec)
    total = max((s.start + s.duration) for s in segs)

    cmd = [ffmpeg, "-y", "-hide_banner", "-nostats",
           "-progress", "pipe:1", "-stats_period", "0.5", "-loglevel", "error"]
    for s in segs:
        if s.kind == "image":
            cmd += ["-loop", "1", "-t", f"{s.duration:.6f}", "-i", _src(s)]
        else:
            cmd += ["-ss", f"{s.src_in:.6f}", "-t", f"{s.duration * (s.speed or 1.0):.6f}",
                    "-i", _src(s)]

    vsegs: list[tuple] = []      # (段, 画面标签): 只有真有画面的段才进画面链
    alabels = []
    filters = [f"color=c=black:s={W}x{H}:r={fps}:d={total:.3f}[base]"]
    for i, s in enumerate(segs):
        # ⚠️ 音频片段**没有视频流**, 不能给它建 [i:v] 链 —— 建了 ffmpeg 直接
        #    报 "Stream specifier ':v' matches no streams", 整个导出失败(v167 修:
        #    带音频素材/分离音频的时间线从来就没导出成功过)。
        is_aud = (s.kind == "audio")
        if not is_aud:
            if s.kind == "image":
                v0 = (f"[{i}:v] scale={W}:{H}:force_original_aspect_ratio=decrease,"
                      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
                      f"format=yuv420p,setpts=PTS-STARTPTS")
            else:
                sp = (s.speed or 1.0)
                v0 = (f"[{i}:v] scale={W}:{H}:force_original_aspect_ratio=decrease,"
                      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
                      f"format=yuv420p,setpts=(PTS-STARTPTS)/{sp:.6f}")
            filters.append(v0 + f"[v{i}]")
            vsegs.append((s, f"v{i}"))
        # ⚠️ 只有**确实有音轨**的输入才能引用 [i:a]: 静音/图片/本身无声的素材
        # 引用了不存在的流, ffmpeg 会整个崩在 "Stream specifier ':a'"(v120 修)。
        # 音频文件本身merge_specs 里没有(探测只收视频) → 但它**当然**有音轨。
        if not s.muted and (is_aud or (s.kind != "image"
                                       and _has_audio(specs.get(s.source)))):
            sp = (s.speed or 1.0)
            delay = int(round(max(0.0, s.start) * 1000))
            # v169: 音量(响度) ≠ 100% 的段加 volume 滤镜 —— 做不了流复制,
            # 所以 lossless 判定看到这种情况会整条转走重编码(见 lossless.py)
            vol = float(getattr(s, "volume", 1.0) or 1.0)
            vol_chain = f"volume={vol:.3f}," if abs(vol - 1.0) > 1e-6 else ""
            filters.append(
                f"[{i}:a] atrim=0:{s.duration * sp:.6f},asetpts=PTS-STARTPTS,"
                f"{_tempo_chain(sp)},{vol_chain}aformat=sample_fmts=fltp:"
                f"sample_rates=48000:channel_layouts=stereo,"
                f"adelay={delay}|{delay}[a{i}]")
            alabels.append(f"a{i}")

    cur = "base"
    last = len(vsegs) - 1
    for j, (s, lab) in enumerate(vsegs):
        a, b = max(0.0, s.start), max(0.0, s.start + s.duration)
        nxt = "vout" if j == last else f"b{j}"
        filters.append(f"[{cur}][{lab}] overlay=x=0:y=0:eof_action=pass:"
                       f"enable='between(t,{a:.3f},{b:.3f})' [{nxt}]")
        cur = nxt
    if alabels:
        filters.append("".join(f"[{a}]" for a in alabels)
                       + f" amix=inputs={len(alabels)}:normalize=0:dropout_transition=0 [aout]")

    cmd += ["-filter_complex", ";".join(filters), "-map", f"[{cur}]"]
    if alabels:
        cmd += ["-map", "[aout]"]
    cmd += ["-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium",
            "-pix_fmt", "yuv420p", "-crf", "20"]
    if alabels:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += ["-an"]
    if os.path.splitext(out_path)[1].lower() == ".mp4":
        cmd += ["-movflags", "+faststart"]
    # v256: 先写临时文件, 全成功才 os.replace 到 out_path —— 取消/失败不删用户旧文件
    tmp_out = pathsafe.temp_output(out_path)
    cmd += [tmp_out]

    err = os.path.join(tempfile.gettempdir(), "psx_reencode_err.txt")
    started = time.monotonic()
    eta_smooth = -1.0
    stopped = False
    try:
        with open(err, "wb") as ef:
            p = proc.popen(cmd, stdout=subprocess.PIPE, stderr=ef)
            try:
                for raw in iter(p.stdout.readline, b""):
                    if cancelled is not None and cancelled():
                        stopped = True
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("out_time_us=") or progress is None:
                        continue
                    try:
                        us = int(line.split("=", 1)[1])
                    except Exception:
                        continue
                    if us < 0 or total <= 0:
                        continue
                    done = us / 1_000_000.0
                    pct = max(0.0, min(99.0, done / total * 100.0))
                    elapsed = max(0.05, time.monotonic() - started)
                    speed = done / elapsed
                    if speed > 0:
                        eta = max(0.0, (total - done) / speed)
                        eta_smooth = eta if eta_smooth < 0 else eta_smooth * 0.7 + eta * 0.3
                    progress(int(pct), eta_smooth)
            finally:
                try:
                    p.stdout.close()
                except Exception:
                    pass
                if stopped:
                    _terminate(p)
                else:
                    try:
                        p.wait()
                    except Exception:
                        pass
    except FileNotFoundError:
        return False, "找不到 ffmpeg 程序"
    except Exception as e:
        return False, f"启动 ffmpeg 失败: {e}"

    if stopped:
        # 只删本次临时半成品, 不动用户原有的 out_path
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except Exception:
            pass
        return False, "已取消"
    if p.returncode != 0:
        tail = "\n".join(_read_err(err).strip().splitlines()[-10:])
        return False, f"导出失败：\n{tail}"
    if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) == 0:
        return False, "输出文件没生成"
    try:
        os.replace(tmp_out, out_path)
    except OSError as e:
        return False, f"写入输出文件失败: {e}"
    if progress is not None:
        progress(100, 0.0)
    return True, out_path


# ---------------------------------------------------------------- 入口
def export(ffmpeg: str, plan, out_path: str, specs: dict | None = None,
           progress=None, cancelled=None, fix_audio: bool = False) -> tuple[bool, str]:
    """按 ExportPlan 导出。返回 (ok, 输出路径或错误信息)。"""
    if plan is None:
        return False, "时间线上还没有片段"
    # 先判"根本导不出来"(比如没 ffmpeg), 再说"没片段" —— 顺序反了会误导人
    if plan.mode == "blocked" or not ffmpeg:
        return False, "没有可用的 FFmpeg，先到右栏左下角指定它的路径。"
    if not plan.segments:
        return False, "时间线上还没有片段"
    # v217: 输出撞素材(且 -y)会把素材截断 —— ffmpeg 看不到 list.txt 背后的真实输入
    bad = pathsafe.check_output(
        out_path, [getattr(s, "source", "") for s in plan.segments])
    if bad:
        return False, bad
    out_dir = os.path.dirname(os.path.abspath(out_path))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        return False, f"建不了输出文件夹：{e}"
    if plan.mode == "blocked":
        return False, "没有可用的 FFmpeg，先到右栏左下角指定它的路径。"
    if plan.mode == "lossless":
        ok, msg = _lossless(ffmpeg, plan, out_path, specs, progress, cancelled,
                            fix_audio=fix_audio)
        if not ok and msg and "取消" not in msg:
            # 判定说能无损但实际拼不上(容器/编码太挑剔) → 不能只报个"失败"了事,
            # 调用方要能识别出来并给"改用转码"的台阶, 否则等于食言。
            return ok, "无损拼接没能成功：" + msg
        return ok, msg
    return _reencode(ffmpeg, plan, out_path, specs, progress, cancelled)
