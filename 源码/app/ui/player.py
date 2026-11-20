# -*- coding: utf-8 -*-
"""剪辑态的播放: ffmpeg 吐原始帧, 我们按帧喂给界面。

为什么不用 QtMultimedia(QMediaPlayer):
- 它要额外带 multimedia 后端插件, PyInstaller 打包容易漏, 漏了就是"点了没反应";
- 系统解码器不一定认用户手里的格式;
- 我们已经有 ffmpeg, 而且它是唯一能按"源时间轴精确取帧"的东西。

做法: 起一个长跑的 ffmpeg 进程, 输出 -f rawvideo -pix_fmt rgb24 到管道,
线程按帧大小读, 转成 QImage 发给主线程。音频不播(第一期只要画面),
播放头由主线程按帧推进。
"""
from __future__ import annotations

import os
import subprocess
import time

from PySide6.QtCore import QMutex, QThread, Signal
from PySide6.QtGui import QImage

PLAY_FPS = 15               # 预览帧率(不追求流畅, 能看清动作就够)
PLAY_W = 640                # 兜底预览宽度(窗口尺寸还没量出来时用)
PREVIEW_MAX_W = 1920        # 抓静帧最宽: 预览窗多大就要多大, 但不必超过 1080p
PLAY_MAX_W = 1280           # 播放时最宽: 15fps 原始帧走管道, 再宽就跟不上了

CREATE_NO_WINDOW = 0x08000000


def out_size(src_w: int, src_h: int, target_w: int = 0) -> tuple[int, int]:
    """按想要的宽度算 ffmpeg 输出宽高(高度取偶数, ffmpeg 的 yuv 要求)。

    v150: 以前固定 640 宽 —— 预览窗比 640 宽时, 这一帧被放大铺满,
    看着就是"预览图很模糊"。现在由调用方把**预览窗的实际像素宽**传进来,
    窗口多大就要多大的帧, 只缩不放。
    """
    tw = int(target_w or 0)
    if tw < 2:
        tw = PLAY_W
    tw = min(tw, PREVIEW_MAX_W)
    if not src_w or not src_h:
        h = int(round(tw * 9 / 16.0))
        return max(2, tw - tw % 2), max(2, h - h % 2)
    w = min(tw, int(src_w))
    h = int(round(src_h * w / float(src_w)))
    h -= h % 2
    return max(2, w - w % 2), max(2, h)



def image_rgb(buf: bytes, w: int, h: int) -> QImage:
    """ffmpeg 吐出来的 rgb24 原始字节 → QImage。

    ⚠️ **必须显式给每行字节数 w*3**。QImage 的四参构造 `QImage(data, w, h, fmt)`
    会把每行补齐到 4 字节对齐: 宽度不是 4 的倍数时, Qt 认为每行是 w*3+2 字节,
    而我们手里是紧密排布的 rgb24 —— 差 2 字节, 于是每一行都往右错 2 字节,
    越往下错得越多, 颜色被逐行搓成灰白 + 斜向错位(v152 用户报的
    「预览是灰度显示的」就是这个: 4K 屏缩放比 3.5, 预览宽度算出来 1694,
    1694*3=5082 不是 4 的倍数, 整幅画面就废了)。
    预览宽度随窗口大小变, 随时可能不是 4 的倍数, 所以这里写死。
    """
    return QImage(bytes(buf), int(w), int(h), int(w) * 3,
                  QImage.Format_RGB888).copy()


class FramePlayer(QThread):
    """播放一个视频片段的某一截(源时间轴的 [src_in, src_out])。"""

    frameReady = Signal(object)     # QImage(已复制, 可跨线程用)
    ended = Signal()                # 这一段放完了(或被停掉)
    failed = Signal(str)

    def __init__(self, ffmpeg: str, parent=None):
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self._job = None             # (src, src_in, src_out, w, h)
        self._stop = False
        self._proc = None

    # ------------------------------------------------------------ 控制
    def play(self, src: str, src_in: float, src_out: float,
             out_w: int, out_h: int, speed: float = 1.0):
        """开始播一段。已经在跑就先停掉(切片段时复用同一个线程对象不行,
        所以外部应每次 new 一个; 这里只做防御)。

        v184: speed = 片段倍速。画面仍按 PLAY_FPS 出帧, 但**出帧间隔**按倍速
        缩放 —— 2× 就是半秒当一秒放, 0.5× 就是慢一倍。播放头不用乘:
        时间线上片段时长本来就已经是 源时长 / speed 了。
        """
        try:
            sp = float(speed or 1.0)
        except Exception:
            sp = 1.0
        if not (0.05 <= sp <= 20.0):
            sp = 1.0
        self._job = (src, max(0.0, float(src_in)), float(src_out),
                     int(out_w), int(out_h), sp)
        self._stop = False
        if not self.isRunning():
            self.start()

    def stop(self):
        self._stop = True
        p = self._proc
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass

    # ------------------------------------------------------------ 线程体
    def run(self):
        job = self._job
        if not job or not self.ffmpeg:
            self.ended.emit()
            return
        src, src_in, src_out, w, h = job[:5]
        sp = float(job[5]) if len(job) > 5 else 1.0
        if not (0.05 <= sp <= 20.0):
            sp = 1.0
        dur = max(0.05, src_out - src_in)
        frame_size = w * h * 3
        cmd = [self.ffmpeg, "-v", "error",
               "-ss", f"{src_in:.3f}", "-i", src,
               "-t", f"{dur:.3f}",
               "-filter:v", f"scale={w}:{h},fps={PLAY_FPS}",
               "-an", "-sn",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        env = dict(os.environ)
        try:
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                bufsize=frame_size * 2, env=env)
        except Exception as e:
            self.failed.emit(f"起不来 ffmpeg：{e}")
            self.ended.emit()
            return
        self._proc = p
        # v184: 倍速只改"墙上时间里的出帧间隔", 不动 ffmpeg 的解码帧率 ——
        # 2× 的段每帧间隔减半, 源 10 秒内容就在时间线上那 5 秒里播完。
        step = 1.0 / (PLAY_FPS * sp)
        try:
            while not self._stop:
                t0 = time.perf_counter()
                buf = p.stdout.read(frame_size)
                if not buf or len(buf) < frame_size:
                    break
                img = image_rgb(buf, w, h)
                if self._stop:
                    break
                self.frameReady.emit(img)
                rest = step - (time.perf_counter() - t0)
                if rest > 0:
                    time.sleep(rest)
        except Exception as e:
            if not self._stop:
                self.failed.emit(f"播放中断：{e}")
        finally:
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.stdout.close()
                p.stderr.close()
            except Exception:
                pass
            self._proc = None
        self.ended.emit()


class FrameGrabber(QThread):
    """不播放, 只抓**一帧**: 裁剪线停在哪一秒, 预览就显示那一秒。

    为什么单独做一个: 播放用的是长跑的 ffmpeg(连续吐帧), 而"拖动裁剪线看画面"
    要的是"给个秒数 → 出一张图", 拿播放器去 seek 会不停重启进程。

    ⚠️ v124: 改成**一条常驻线程 + 覆盖式任务**(以前每抓一帧就 new 一个 QThread)。
    每帧新建线程有两个致命问题:
      1) 缩放/拖裁剪线时一秒能发几十次, QThread 对象疯狂创建销毁;
      2) 外面用 `self._grab_worker` 记"当前那个", 旧线程的 finished 迟到时会把
         **新的**那个 deleteLater 掉 —— 删一个还在跑的 QThread, Qt 内部直接 abort
         (Windows 上就是 0xc0000409 闪退)。
    现在线程一直在, 只换任务: 只留最新的一个(天然节流), 永远不会删到活线程。
    """

    frameReady = Signal(object)     # QImage(已 copy, 可跨线程用)

    def __init__(self, ffmpeg: str = "", parent=None):
        super().__init__(parent)
        self.ffmpeg = ffmpeg or ""
        self._job = None             # (src, 源秒数, w, h)
        self._mutex = QMutex()
        self._stop_flag = False

    def set_ffmpeg(self, exe: str | None):
        self.ffmpeg = exe or ""

    def request(self, src: str, t_src: float, out_w: int, out_h: int) -> None:
        """记一个待抓位置; 旧的没抓完就直接被顶掉(只画最新的那个)。"""
        self._mutex.lock()
        self._job = (src, max(0.0, float(t_src)),
                     max(2, int(out_w)), max(2, int(out_h)))
        self._mutex.unlock()

    def stop(self):
        self._stop_flag = True
        self._mutex.lock()
        self._job = None
        self._mutex.unlock()

    def _take(self):
        self._mutex.lock()
        job, self._job = self._job, None
        self._mutex.unlock()
        return job

    def run(self):
        while not self._stop_flag:
            job = self._take()
            if job is None:
                self.msleep(15)
                continue
            try:
                img = self._grab(*job)
            except Exception:
                img = None
            if self._stop_flag:
                return
            if img is not None and not img.isNull():
                self.frameReady.emit(img)

    def _grab(self, src: str, t_src: float, w: int, h: int):
        """真的跑一次 ffmpeg 抓一帧(失败返回 None, 不抛)。"""
        if not self.ffmpeg or not src:
            return None
        frame_size = w * h * 3
        cmd = [self.ffmpeg, "-v", "error",
               "-ss", f"{t_src:.3f}", "-i", src,
               "-frames:v", "1", "-an", "-sn",
               "-filter:v", f"scale={w}:{h}",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                bufsize=frame_size * 2)
        except Exception:
            return None
        try:
            buf = p.stdout.read(frame_size)
        except Exception:
            buf = b""
        finally:
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.stdout.close()
            except Exception:
                pass
        if not buf or len(buf) < frame_size:
            return None
        return image_rgb(buf, w, h)
