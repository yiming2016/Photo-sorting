# -*- coding: utf-8 -*-
"""调用 ffmpeg 执行合并(无损 -c copy / 转码 libx264+aac)。

用 concat demuxer + 临时列表文件(避免把长路径列表塞进命令行),
列表用绝对路径并单引号包裹(路径含空格也能用)。

进度与取消:
- ffmpeg 加 `-progress pipe:1 -nostats`, 每 0.5 秒向 stdout 吐一行
  `out_time_us=...`。用"已处理时长 / 总时长"算百分比,
  用"已用时 / 已处理时长"算速度, 再推算剩余时间(ETA, 单位秒)。
  **不用 `-stats`**: 它写 stderr 且是给人看的文本, 解析脆弱。
- 取消: 边读边检查 cancelled(), 一旦为真就 terminate(3 秒不退则 kill),
  并删掉半截输出文件, 避免留下"看起来合成了其实残缺"的文件。
"""
import os
import subprocess
import tempfile
import time

from . import pathsafe, proc, norm_paths


def _escape_concat_path(p: str) -> str:
    return p.replace("'", "'\\''")


def _build_concat_list(paths, tmpdir: str) -> str:
    list_path = os.path.join(tmpdir, "concat.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in paths:
            f.write(f"file '{_escape_concat_path(p)}'\n")
    return list_path


def _terminate(p) -> None:
    """结束子进程: 先 terminate, 3 秒不退再 kill。"""
    try:
        p.terminate()
    except Exception:
        pass
    try:
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


def merge(ffmpeg: str, group_photos: list, out_path: str,
          reencode: bool = False, total_duration: float = 0.0,
          progress=None, cancelled=None, fix_audio: bool = False) -> tuple[bool, str]:
    """合并一组视频(按列表顺序拼接)。返回 (ok, 信息/错误)。

    lossless: -c copy ; reencode: libx264 -pix_fmt yuv420p + aac。
    progress(pct:int, eta_seconds:float) 在合并过程中被反复调用(eta<0 表示算不出);
    cancelled() 返回 True 时中断并删除半成品。
    """
    # v200: 有统一格式工作副本就读副本; 主列表仍保留原文件路径。
    paths = [norm_paths.resolve(p.path) for p in group_photos]
    if len(paths) < 2:
        return False, "至少需要 2 段视频才能合并"
    # v217: 输出撞源文件会把原片截断(concat 的输入是 list.txt, ffmpeg 的保护不生效)
    bad = pathsafe.check_output(out_path, paths)
    if bad:
        return False, bad

    try:
        total = float(total_duration or 0)
    except Exception:
        total = 0.0

    with tempfile.TemporaryDirectory() as tmp:
        list_path = _build_concat_list(paths, tmp)
        err_path = os.path.join(tmp, "ffmpeg_stderr.txt")
        cmd = [ffmpeg, "-y", "-hide_banner", "-nostats",
               "-progress", "pipe:1", "-stats_period", "0.5",
               "-f", "concat", "-safe", "0", "-i", list_path]
        if reencode:
            cmd += ["-c:v", "libx264", "-preset", "medium",
                    "-pix_fmt", "yuv420p", "-c:a", "aac"]
        elif fix_audio:
            # 视频仍流复制(秒级); 音频重编一次, 消掉每段 AAC 的 priming 间隙。
            cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-ar", "48000", "-af", "aresample=async=1:first_pts=0"]
        else:
            cmd += ["-c", "copy"]
        ext = os.path.splitext(out_path)[1].lower()
        if ext == ".mp4":
            cmd += ["-movflags", "+faststart"]
        # v256: ffmpeg 一律写同目录的临时文件, 成功后才 os.replace 到 out_path。
        # 之前直接写 out_path: 用户填了已存在的输出名并确认覆盖后, 中途取消或
        # ffmpeg 失败会 os.remove(out_path) —— 把用户原有的旧文件一起删掉,
        # 新旧两空。临时文件方案下, 取消/失败绝不触碰 out_path。
        tmp_path = pathsafe.temp_output(out_path)
        cmd += [tmp_path]

        started = time.monotonic()
        eta_smooth = -1.0
        stopped = False
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
                        if progress is None or total <= 0:
                            continue
                        try:
                            us = int(line.split("=", 1)[1])
                        except Exception:
                            continue
                        if us < 0:
                            continue
                        done = us / 1_000_000.0
                        pct = max(0.0, min(99.0, done / total * 100.0))
                        elapsed = max(0.05, time.monotonic() - started)
                        speed = done / elapsed          # 内容秒 / 实际秒
                        if speed > 0:
                            eta = max(0.0, (total - done) / speed)
                            eta_smooth = (eta if eta_smooth < 0
                                          else eta_smooth * 0.7 + eta * 0.3)
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
        except Exception as e:
            if isinstance(e, FileNotFoundError):
                return False, "找不到 ffmpeg 程序"
            return False, f"启动 ffmpeg 失败: {e}"

        if stopped:
            # 只删本次的临时半成品, 不碰用户原有的 out_path
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return False, "已取消"

        if p.returncode != 0:
            tail = "\n".join(_read_err(err_path).strip().splitlines()[-8:])
            return False, f"ffmpeg 返回 {p.returncode}:\n{tail}"
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            return False, "输出文件未生成(ffmpeg 可能未真正写入)"
        # 全部成功才原子替换到用户指定的输出位置(覆盖旧文件是用户确认过的)
        try:
            os.replace(tmp_path, out_path)
        except OSError as e:
            return False, f"写入输出文件失败: {e}"
        if progress is not None:
            progress(100, 0.0)
        return True, out_path
