# -*- coding: utf-8 -*-
"""剪辑态的声音: ffmpeg 解出 PCM → 直接送声卡。

⚠️ 为什么不用 QtMultimedia(QAudioOutput/QMediaPlayer): 这台机器装的 PySide6 里
**根本没带 Qt6Multimedia**(打包产物里也没有), 引了就是 ImportError。所以这里
用 Windows 自带的 waveOut(ctypes 调 winmm.dll), 不新增依赖、不要额外插件。

做法: 起一个 ffmpeg, `-vn -f s16le -ar 48000 -ac 2` 把 PCM 吐到管道; 线程按块
读、塞进 waveOut 的环形缓冲区 —— 缓冲区播完才复用, 于是速度天然等于实时速度。

⚠️ 两个踩过的坑(都是"崩在完全不相干的地方"那种):
  1) 别"播一次就 new 一个线程 + 开一次声卡": 拖进度条/切片段时一秒好几次,
     线程对象疯狂创建销毁, 声卡也跟着反复开关;
  2) 掐声(waveOutReset)之后**必须等缓冲区被标成 DONE 再释放**, 没等就释放,
     驱动会往回收掉的内存里写, 之后进程随机 access violation。
所以现在是"一条常驻线程 + 一张常开着的声卡", 掐声只是 reset, 不关、不重建。
"""
from __future__ import annotations

import ctypes
import subprocess
import time
from ctypes import wintypes

from PySide6.QtCore import QMutex, QThread, Signal

CREATE_NO_WINDOW = 0x08000000

RATE = 48000
CHANNELS = 2
CHUNK = 8192                  # 每块约 43ms(48k/立体声/16bit)
SLOTS = 8                     # 缓冲区个数: 最多缓冲约 0.35 秒

MMSYSERR_NOERROR = 0
WAVE_MAPPER = 0xFFFFFFFF      # -1
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0
WHDR_DONE = 0x00000001

_winmm = None


def _mm():
    global _winmm
    if _winmm is None:
        try:
            dll = ctypes.WinDLL("winmm")
        except Exception:
            _winmm = False
            return None
        dll.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.UINT,
                                    ctypes.c_void_p, ctypes.c_size_t,
                                    ctypes.c_size_t, wintypes.DWORD]
        dll.waveOutOpen.restype = wintypes.UINT
        for name in ("waveOutPrepareHeader", "waveOutWrite",
                     "waveOutUnprepareHeader", "waveOutReset", "waveOutClose"):
            fn = getattr(dll, name)
            fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
            fn.restype = wintypes.UINT
        _winmm = dll
    return _winmm if _winmm else None


def audio_available() -> bool:
    """这台机器能不能出声(没声卡/没 winmm 就安静地跳过)。"""
    return _mm() is not None


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", ctypes.c_uint16),
                ("nChannels", ctypes.c_uint16),
                ("nSamplesPerSec", ctypes.c_uint32),
                ("nAvgBytesPerSec", ctypes.c_uint32),
                ("nBlockAlign", ctypes.c_uint16),
                ("wBitsPerSample", ctypes.c_uint16),
                ("cbSize", ctypes.c_uint16)]


class WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p),
                ("dwBufferLength", ctypes.c_uint32),
                ("dwBytesRecorded", ctypes.c_uint32),
                ("dwUser", ctypes.c_size_t),
                ("dwFlags", ctypes.c_uint32),
                ("dwLoops", ctypes.c_uint32),
                ("lpNext", ctypes.c_void_p),
                ("reserved", ctypes.c_size_t)]


class WaveOut:
    """waveOut 的极简封装: 只管"塞 PCM 进去, 播完的缓冲区自动回收"。"""

    def __init__(self, rate: int = RATE, channels: int = CHANNELS):
        self.rate = int(rate)
        self.channels = max(1, int(channels))
        self._h = ctypes.c_void_p(0)
        self._bufs: list = []
        self._hdrs: list = []
        self._busy: list = []
        self._open = False
        self._stop = False

    # ------------------------------------------------------------ 生命周期
    def open(self) -> bool:
        mm = _mm()
        if mm is None:
            return False
        fmt = WAVEFORMATEX()
        fmt.wFormatTag = WAVE_FORMAT_PCM
        fmt.nChannels = self.channels
        fmt.nSamplesPerSec = self.rate
        fmt.wBitsPerSample = 16
        fmt.nBlockAlign = self.channels * 2
        fmt.nAvgBytesPerSec = self.rate * fmt.nBlockAlign
        fmt.cbSize = 0
        if mm.waveOutOpen(ctypes.byref(self._h), WAVE_MAPPER,
                          ctypes.cast(ctypes.byref(fmt), ctypes.c_void_p),
                          0, 0, CALLBACK_NULL) != MMSYSERR_NOERROR:
            return False
        self._open = True
        self._stop = False
        for _ in range(SLOTS):
            b = ctypes.create_string_buffer(CHUNK)
            h = WAVEHDR()
            h.lpData = ctypes.cast(b, ctypes.c_void_p)
            self._bufs.append(b)
            self._hdrs.append(h)
            self._busy.append(False)
        return True

    def reset(self):
        """把还没放完的声音掐掉(**不关声卡**, 下一次接着用)。

        ⚠️ 这里是整个声音通路最容易踩雷的地方: `waveOutReset` 只是"请求"驱动把
        缓冲区还回来, DONE 标记不是立刻就有的。没等 DONE 就 unprepare、再把
        ctypes 缓冲区释放掉, 驱动会往已经回收的内存里写 —— 表现是"点了暂停/
        切了片段之后, 过一会儿整个进程 access violation 崩掉", 而且崩在完全不相
        干的代码里(导入模块、画界面……), 极难查。所以必须轮询到 DONE 再收。
        """
        mm = _mm()
        if mm is None or not self._open:
            return
        try:
            mm.waveOutReset(self._h)
        except Exception:
            pass
        t0 = time.time()
        while time.time() - t0 < 1.5:
            self._reclaim_done()
            if not any(self._busy):
                break
            time.sleep(0.01)
        self._reclaim_all()

    def close(self):
        if not self._open:
            return
        mm = _mm()
        self._stop = True
        self.reset()                      # 先把缓冲区全部收回来
        ok = False
        try:
            for _ in range(50):
                if mm.waveOutClose(self._h) == MMSYSERR_NOERROR:
                    ok = True
                    break
                time.sleep(0.02)
        except Exception:
            pass
        self._open = False
        if ok:
            self._bufs.clear()
            self._hdrs.clear()
            self._busy.clear()
        # 关不掉就**不释放**缓冲区: 宁可泄漏这几十 KB, 也不能让驱动写野指针。

    # ------------------------------------------------------------ 播放
    def write(self, data: bytes, timeout: float = 3.0) -> bool:
        """送一块 PCM 进去; 缓冲区都在忙就等 —— 于是播放速度就是实时速度。"""
        if not self._open or self._stop:
            return False
        idx = self._wait_free(timeout)
        if idx is None:
            return False
        mm = _mm()
        buf, hdr = self._bufs[idx], self._hdrs[idx]
        n = min(len(data), CHUNK)
        buf[:n] = data[:n]
        hdr.dwBufferLength = n
        hdr.dwFlags = 0
        sz = ctypes.sizeof(hdr)
        if mm.waveOutPrepareHeader(self._h, ctypes.byref(hdr), sz) != MMSYSERR_NOERROR:
            self._busy[idx] = False
            return False
        if mm.waveOutWrite(self._h, ctypes.byref(hdr), sz) != MMSYSERR_NOERROR:
            try:
                mm.waveOutUnprepareHeader(self._h, ctypes.byref(hdr), sz)
            except Exception:
                pass
            self._busy[idx] = False
            return False
        self._busy[idx] = True
        return True

    def drain(self, timeout: float = 3.0) -> bool:
        """把已经送进去的声音放完(不然结尾会被硬切一截)。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not any(self._busy):
                return True
            self._reclaim_done()
            time.sleep(0.02)
        return False

    # ------------------------------------------------------------ 内部
    def _reclaim_done(self):
        mm = _mm()
        if mm is None:
            return
        for i in range(len(self._busy)):
            if not self._busy[i]:
                continue
            hdr = self._hdrs[i]
            if hdr.dwFlags & WHDR_DONE:
                try:
                    mm.waveOutUnprepareHeader(self._h, ctypes.byref(hdr),
                                              ctypes.sizeof(hdr))
                except Exception:
                    pass
                self._busy[i] = False

    def _reclaim_all(self):
        mm = _mm()
        if mm is None:
            return
        for i in range(len(self._busy)):
            if self._busy[i]:
                try:
                    mm.waveOutUnprepareHeader(self._h, ctypes.byref(self._hdrs[i]),
                                              ctypes.sizeof(self._hdrs[i]))
                except Exception:
                    pass
                self._busy[i] = False

    def _wait_free(self, timeout: float):
        t0 = time.time()
        while not self._stop:
            for i in range(len(self._busy)):
                if not self._busy[i]:
                    return i
                if self._hdrs[i].dwFlags & WHDR_DONE:
                    self._reclaim_done()
                    if not self._busy[i]:
                        return i
            if time.time() - t0 > timeout:
                return None
            time.sleep(0.004)
        return None


class SoundPlayer(QThread):
    """常驻的声音线程: 一次只播一段, 新任务直接顶掉旧的。

    为什么不"播一次 new 一个 QThread + 开一次声卡":
      1) 拖进度条/切片段时一秒能来好几次, 线程对象疯狂创建销毁(线程铁律);
      2) 声卡反复 open/close 是这套通路里最容易出事的地方 —— 驱动还没把缓冲区
         还回来我们就释放它, 进程会在之后某个不相干的地方 access violation;
      3) 常驻之后"掐声"只是一次 waveOutReset, 立刻静音、也没有开关延迟。

    ⚠️ 所有 waveOut 调用都在**同一个线程**里(声音线程自己), 不在外面调。
    """

    failed = Signal(str)

    def __init__(self, ffmpeg: str = "", parent=None):
        super().__init__(parent)
        self.ffmpeg = ffmpeg or ""
        self._job = None            # [(源文件, 起始秒, 结束秒), ...]: 只留最新一组
        self._mute = False          # 外部要静音
        self._stop_flag = False     # 线程要退出
        self._gen = 0               # 任务代号: 新任务一来, 正在播的那段立刻让位
        self._proc = None
        self._mutex = QMutex()
        self._failed_once = False

    def set_ffmpeg(self, exe: str | None):
        self.ffmpeg = exe or ""

    # ------------------------------------------------------------ 外部控制
    def request(self, jobs):
        """要播这一段(正在播的直接被顶掉)。

        jobs: [(源文件, 起始秒, 结束秒), ...] —— 同一时刻**好几条轨可能都有声音**
        (分离音频之后就是这样: 画面在上面的视频轨且已静音, 声音在下面那条音频轨)。
        只挑一条播的话, 分离出来的声音就永远听不见。

        ⚠️ 一路还是走原来那条最简单的命令; 只有真的要叠两路以上才上 amix ——
        amix 一出错就是"整段没声", 能不用就不用。
        """
        if isinstance(jobs, tuple):          # 兼容老的单个 (src, start, end)
            jobs = [jobs]
        jobs = [(s, max(0.0, float(a)), float(b)) for s, a, b in (jobs or [])
                if s and float(b) - float(a) > 0.05]
        if not jobs:
            return
        self._mutex.lock()
        self._job = jobs
        self._mute = False
        self._gen += 1
        self._mutex.unlock()
        p = self._proc
        if p is not None:
            try:
                p.kill()            # 正在播的那段立刻掐掉, 别等它自然放完
            except Exception:
                pass

    def silence(self):
        """立刻不出声(但线程和声卡都留着, 下次还能马上播)。"""
        self._mutex.lock()
        self._job = None
        self._mute = True
        self._mutex.unlock()
        p = self._proc
        if p is not None:
            try:
                p.kill()            # 掐掉 ffmpeg, 读循环会自己退出来
            except Exception:
                pass

    def stop(self):
        """整个收掉(退出剪辑页/关程序时用): 线程退出前会关声卡。"""
        self._stop_flag = True
        self.silence()

    # ------------------------------------------------------------ 命令
    def _build_cmd(self, jobs: list) -> list:
        """一路 = 最朴素的那条; 两路以上 = amix 叠在一起(同时开始, 长的说了算)。"""
        n = len(jobs)
        tail = ["-vn", "-sn", "-f", "s16le", "-ar", str(RATE),
                "-ac", str(CHANNELS), "-"]
        if n == 1:
            src, start, end = jobs[0]
            return [self.ffmpeg, "-v", "error",
                    "-ss", f"{start:.3f}", "-i", src,
                    "-t", f"{max(0.05, end - start):.3f}"] + tail
        cmd = [self.ffmpeg, "-v", "error"]
        for src, start, end in jobs:
            # ⚠️ 多输入时 `-t` 必须写在 `-i` **前面**(当输入选项用), 写在后面
            # 会被当成下一个输出文件的时长, 结果只有最后一个输入被截断。
            cmd += ["-ss", f"{start:.3f}",
                    "-t", f"{max(0.05, end - start):.3f}", "-i", src]
        cmd += ["-filter_complex",
                "".join(f"[{i}:a]" for i in range(n))
                + f"amix=inputs={n}:normalize=0:dropout_transition=0[a]",
                "-map", "[a]"] + tail
        return cmd

    # ------------------------------------------------------------ 内部
    def _take(self):
        self._mutex.lock()
        job, self._job = self._job, None
        mute = self._mute
        self._mutex.unlock()
        return job, mute

    def _muted(self):
        self._mutex.lock()
        m = self._mute
        self._mutex.unlock()
        return m

    def _take_gen(self) -> int:
        self._mutex.lock()
        g = self._gen
        self._mutex.unlock()
        return g

    def _fail(self, msg: str):
        if self._failed_once:
            return
        self._failed_once = True
        self.failed.emit(msg)

    # ------------------------------------------------------------ 线程体
    def run(self):
        w = None
        while not self._stop_flag:
            job, mute = self._take()
            if job is None:
                self.msleep(15)
                continue
            if mute or not self.ffmpeg:
                continue
            if w is None:
                w = WaveOut(RATE, CHANNELS)
                if not w.open():
                    self._fail("打不开声卡")
                    w = None
                    continue
            w.reset()                    # 清掉上一段的残声(不关声卡)
            if self._stop_flag:
                break
            cmd = self._build_cmd(job)
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL,
                                     creationflags=CREATE_NO_WINDOW,
                                     bufsize=CHUNK * 4)
            except Exception as e:
                self._fail(f"起不来 ffmpeg：{e}")
                continue
            self._proc = p
            my_gen = self._take_gen()
            try:
                while not self._stop_flag and not self._muted():
                    if self._take_gen() != my_gen:
                        break               # 有新任务顶掉这段了
                    buf = p.stdout.read(CHUNK)
                    if not buf:
                        break
                    if not w.write(buf):
                        break
            except Exception:
                pass
            finally:
                try:
                    p.kill()
                except Exception:
                    pass
                try:
                    p.stdout.close()
                except Exception:
                    pass
                self._proc = None
            if (not self._stop_flag and not self._muted()
                    and self._take_gen() == my_gen):
                w.drain()                # 自然播完: 让最后一小截放出来
        if w is not None:
            w.close()
