# -*- coding: utf-8 -*-
"""合并相关的后台线程: 规格探测 + 合并执行。

都继承 QThread, 结果通过信号回主线程; 不在 UI 线程做任何可能阻塞的操作
(否则窗口画不出来, 用户会以为"点了没反应")。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal


class FfmpegCheckWorker(QThread):
    """只查 FFmpeg: 有没有、在哪。用于排序模式下给按钮定绿/红。

    查找可能很慢(WinGet 包目录 + 有限深度递归), 必须放后台, 否则窗口会卡住。
    ok 为 True 才算"检测通过"(ffmpeg 与 ffprobe 都在且能跑 -version)。
    """

    done = Signal(bool, str, str)     # 通过?, ffmpeg路径, ffprobe路径

    def run(self):
        from . import ffmpeg_locate
        try:
            pair = ffmpeg_locate.find_ffmpeg_pair()
        except Exception:
            pair = None
        if not pair:
            self.done.emit(False, "", "")
            return
        from .ffmpeg_settings import run_version
        ok = False
        try:
            ok = run_version(pair[0])[0] and run_version(pair[1])[0]
        except Exception:
            ok = False
        self.done.emit(bool(ok), pair[0], pair[1])


class ProbeWorker(QThread):
    """查找 ffmpeg, 然后逐个 ffprobe 探测视频规格。"""

    found = Signal(str, str)          # ffmpeg, ffprobe
    no_ffmpeg = Signal()
    one = Signal(int, object, str)    # 索引, spec(或 None), 错误原因
    finished_all = Signal()

    def __init__(self, videos, parent=None):
        super().__init__(parent)
        self.videos = list(videos)
        self._stop = False

    def run(self):
        from . import ffmpeg_locate, probe
        try:
            pair = ffmpeg_locate.find_ffmpeg_pair()
        except Exception:
            pair = None
        if not pair:
            self.no_ffmpeg.emit()
            self.finished_all.emit()
            return
        self.found.emit(pair[0], pair[1])
        for i, p in enumerate(self.videos):
            if self._stop:
                return
            try:
                spec, err = probe.probe_ex(p.path, pair[1], with_bitrate_mode=True)
            except Exception as e:
                spec, err = None, f"异常：{e}"
            if self._stop:
                return
            self.one.emit(i, spec, err)
        if not self._stop:
            self.finished_all.emit()

    def stop(self):
        self._stop = True


class NormalizeWorker(QThread):
    """v200: 批量把选中视频统一成同一套 H.265 规格(生成工作副本)。"""

    one = Signal(int, bool, str, bool)    # 索引, 成功?, 副本路径/错误, 是否跳过
    progress = Signal(int, float)         # 当前文件百分比, 剩余秒数
    finished_all = Signal()

    def __init__(self, items, profile, ffmpeg, ffprobe, parent=None,
                 channels=None):
        super().__init__(parent)
        self.items = list(items)
        self.profile = profile
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.channels = channels
        self._cancel = False

    def stop(self):
        self._cancel = True

    def run(self):
        from . import normalize
        for i, item in enumerate(self.items):
            if self._cancel:
                break
            try:
                ok, result, skipped = normalize.convert_one(
                    item.path, self.profile, self.ffmpeg, self.ffprobe,
                    spec=item.spec,
                    progress=lambda pct, eta: self.progress.emit(int(pct), float(eta)),
                    cancelled=lambda: self._cancel,
                    channels=self.channels,
                )
            except Exception as e:
                ok, result, skipped = False, f"异常：{e}", False
            self.one.emit(i, bool(ok), str(result or ""), bool(skipped))
        self.finished_all.emit()


class MergeWorker(QThread):
    """执行合并(无损 -c copy 或 转码), 边跑边报进度, 可中途取消。"""

    result = Signal(bool, str)
    progress = Signal(int, float)      # 百分比 0~100, 剩余秒数(<0 表示算不出)

    def __init__(self, ffmpeg, photos, out, reencode, total_duration=0.0,
                 parent=None, fix_audio: bool = False):
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self.photos = list(photos)
        self.out = out
        self.reencode = reencode
        self.total_duration = float(total_duration or 0)
        self.fix_audio = bool(fix_audio)
        self._cancel = False

    def stop(self):
        """请求取消: merge_run 读到标志后结束 ffmpeg 并删除半成品。"""
        self._cancel = True

    def run(self):
        from . import merge_run
        try:
            ok, msg = merge_run.merge(
                self.ffmpeg, self.photos, self.out,
                reencode=self.reencode,
                total_duration=self.total_duration,
                progress=lambda pct, eta: self.progress.emit(int(pct), float(eta)),
                cancelled=lambda: self._cancel,
                fix_audio=self.fix_audio,
            )
        except Exception as e:
            ok, msg = False, f"合并异常: {e}"
        self.result.emit(ok, msg)


class ExportWorker(QThread):
    """剪辑导出: 按 ExportPlan 执行(无损 copy 拼接 或 转码合成)。

    导出可能很久(转码尤其), 必须在后台跑, 否则窗口画不出来、看起来像卡死。
    """

    result = Signal(bool, str)        # 成功?, 输出路径 / 错误信息
    progress = Signal(int, float)     # 百分比, 剩余秒数(<0 = 算不出)

    def __init__(self, ffmpeg, plan, out, specs=None, parent=None,
                 fix_audio: bool = False):
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self.plan = plan
        self.out = out
        self.specs = dict(specs or {})
        self.fix_audio = bool(fix_audio)
        self._cancel = False

    def stop(self):
        self._cancel = True

    def run(self):
        from . import export
        try:
            ok, msg = export.export(
                self.ffmpeg, self.plan, self.out, specs=self.specs,
                progress=lambda pct, eta: self.progress.emit(int(pct), float(eta)),
                cancelled=lambda: self._cancel,
                fix_audio=self.fix_audio,
            )
        except Exception as e:
            ok, msg = False, f"导出异常: {e}"
        self.result.emit(ok, msg)


class ClipProbeWorker(QThread):
    """剪辑态: 逐个读取素材时长(视频/音频) + 关键帧 + 有没有音轨。

    视频走 probe_ex(顺带拿全规格)再补关键帧; 纯音频没有视频流, 退到 probe_duration。
    探测"可能很慢", 按铁律放后台线程, 结果用信号回主线程。
    """

    one = Signal(str, object)           # 路径, {duration, keyframes, has_audio, spec}
    finished_all = Signal()

    def __init__(self, paths, parent=None, need_keyframes: bool = True):
        super().__init__(parent)
        self.paths = list(paths)
        self.need_keyframes = bool(need_keyframes)
        self._stop = False

    def run(self):
        from . import ffmpeg_locate, probe
        from ..core.clips import kind_of
        try:
            pair = ffmpeg_locate.find_ffmpeg_pair()
        except Exception:
            pair = None
        if not pair:
            self.finished_all.emit()
            return
        ffprobe = pair[1]
        for path in self.paths:
            if self._stop:
                return
            info = {"duration": 0.0, "keyframes": [], "has_audio": False}
            try:
                kind = kind_of(path)
                spec, _err = probe.probe_ex(path, ffprobe)
                # 全规格也带回去: 无损判定要比编码参数, 光有时长不够
                if spec:
                    info["spec"] = spec
                dur = float((spec or {}).get("duration") or 0.0)
                if dur <= 0:
                    dur = probe.probe_duration(path, ffprobe)
                info["duration"] = dur
                info["has_audio"] = bool((spec or {}).get("audio_codec")) \
                    or kind == "audio"
                if self.need_keyframes and kind == "video" and dur > 0:
                    info["keyframes"] = probe.probe_keyframes(path, ffprobe)
                if kind == "audio" and not info["has_audio"]:
                    info["has_audio"] = bool(probe.probe_audio_stream(path, ffprobe))
            except Exception:
                pass
            if self._stop:
                return
            self.one.emit(path, info)
        if not self._stop:
            self.finished_all.emit()

    def stop(self):
        self._stop = True


class ExtractAudioWorker(QThread):
    """把视频的音轨原样抽成独立音频文件(-vn -c:a copy, 不重新编码)。

    输出扩展名按源音轨的编码选: aac→m4a, mp3→mp3, vorbis/opus→ogg, flac→flac…
    认不出的编码才转成 aac(这时会有损失, 状态栏会说明)。
    """

    result = Signal(bool, str, str)     # 成功?, 输出路径, 说明(失败原因或提示)

    # 编码 → (容器扩展名, 能否 -c copy)
    _EXT = {
        "aac": ".m4a", "alac": ".m4a", "mp3": ".mp3", "flac": ".flac",
        "vorbis": ".ogg", "opus": ".ogg", "wav": ".wav", "pcm_s16le": ".wav",
        "wma": ".wma", "ac3": ".ac3", "eac3": ".eac3", "dts": ".dts",
    }

    def __init__(self, ffmpeg, ffprobe, src, out_dir: str = "", parent=None):
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.src = src
        self.out_dir = out_dir          # 空 = 跟源文件同目录(老行为)

    def run(self):
        from . import probe
        import os
        if not self.ffmpeg:
            self.result.emit(False, "", "没有可用的 ffmpeg")
            return
        info = probe.probe_audio_stream(self.src, self.ffprobe)
        codec = str(info.get("codec") or "").lower()
        if not codec:
            self.result.emit(False, "", "这个片段没有音轨")
            return
        ext = self._EXT.get(codec, ".m4a")
        copy_ok = codec in self._EXT
        base = os.path.splitext(os.path.basename(self.src))[0]
        # 存处: 优先按设置给的文件夹, 没有就跟源文件同目录
        try:
            from ..core.media_tools import output_path_for
            out = output_path_for(self.src, "_音频", ext)
        except Exception:
            out = ""
        if not out:
            out = os.path.join(os.path.dirname(self.src), f"{base}_音频{ext}")
            try:
                n = 1
                while os.path.exists(out):
                    out = os.path.join(os.path.dirname(self.src),
                                       f"{base}_音频({n}){ext}")
                    n += 1
            except Exception:
                pass
        cmd = [self.ffmpeg, "-v", "error", "-y", "-i", self.src, "-vn",
               "-map", "a:0"]
        cmd += ["-c:a", "copy"] if copy_ok else ["-c:a", "aac", "-b:a", "192k"]
        cmd += [out]
        try:
            from . import proc
            r = proc.run(cmd, timeout=600)
        except Exception as e:
            self.result.emit(False, "", f"抽音轨失败：{e}")
            return
        if r.returncode != 0:
            err = (r.stderr or "").strip().splitlines()
            self.result.emit(False, "", err[-1][:160] if err else "抽音轨失败")
            return
        if not os.path.isfile(out):
            self.result.emit(False, "", "没有生成音频文件")
            return
        tip = "音轨原样抽出，没有重新编码" if copy_ok \
            else f"源音轨是 {codec}，已转成 aac（有轻微损失）"
        self.result.emit(True, out, tip)


class ToolWorker(QThread):
    """跑一个耗时函数: fn(progress, cancel) -> 任意结果。

    剪辑页的工具箱(改帧率/削减/检测/波形/修时长)全都走它 —— 这些活可能
    几秒也可能几分钟, 放在界面线程里就是"点了没反应"。
    """

    progress = Signal(int, str)
    done = Signal(bool, object, str)      # 成功?, 结果, 错误话术

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        from ..core.media_tools import Cancelled
        try:
            result = self._fn(
                lambda pct, msg="": self.progress.emit(int(pct), str(msg)),
                lambda: self._cancel)
        except Cancelled:
            self.done.emit(False, None, "已取消")
        except Exception as e:
            msg = str(e) or type(e).__name__
            self.done.emit(False, None, msg)
        else:
            self.done.emit(True, result, "")
