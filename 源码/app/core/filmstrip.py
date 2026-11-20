# -*- coding: utf-8 -*-
"""时间线片段的缩略图条(filmstrip): 按时间点抽真帧 + 磁盘缓存。

为什么单独一个模块:
- 时间线要按像素位置平铺很多张帧, 每张都是一次 ffmpeg 解码, 必须**去重**
  并且落到磁盘缓存, 否则每次重绘/缩放都会重新解码, 界面直接卡住;

v138 的两处提速(实测, 1080p 素材):
- **一次 ffmpeg 连抽多张**: 单独抽一张约 100ms(大部分花在起进程 + seek),
  而同一个源、时间挨得近的一批帧可以合成一条命令一次吐出来, 摊到每张只要 16ms。
  用 select 精确定点; ⚠️ select 里的 t 是**相对 -ss 起点**算的(已验证: 不减开头
  会抓错帧), 减掉之后与"单独抽那个时间点"像素完全一致。
- **几条线程并行**: 抽帧是子进程在干活, Python 这边只是等, 多线程不会打架。

另外一个关键点是**量化粒度按缩放走**(STEP_LADDER): 缩放到看全貌时, 一格代表
好几分钟素材, 没必要为每一格都抽一张 —— 相邻几格共用一张"代表帧"完全看不出差别,
要抽的帧数一下少好几倍, 来回滚时缓存也能命中。

铁律: 子进程一律走 merge/proc.run(中文 Windows 下 text=True 会按 GBK 解码炸掉);
       工作线程只许产出 QImage, QPixmap 只能在主线程造。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile

from PySide6.QtCore import (QMutex, QMutexLocker, QObject, QThread, Signal)
from PySide6.QtGui import QImage

from .thumbs import cache_dir

FRAME_Q = 0.5           # 时间点量化粒度(秒), 放大后用的最小档
FRAME_W = 160           # 抽出来的帧宽(画的时候再缩放)

# 量化档位: 一格代表多少秒素材, 就往上取最接近的那一档
STEP_LADDER = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)

# 什么情况下值得"一次抽一批":
# - CLUSTER_GAP: 两格时间点挨得这么近才算一簇(隔开太远, 中间那段得解出来, 白解);
# - CLUSTER_SPAN: 一簇的**总跨度**上限。这条最要命 —— 缩到看全貌时相邻格子隔着
#   十几秒甚至几分钟, 一簇拉满是几十分钟素材, 一次性解完比一张张 seek 慢得多。
#   (实测: 解 1 秒 1080p 约 70ms, 所以 8 秒封顶 ≈ 0.5 秒一批, 摊到每张仍远快于单独的 110ms)
CLUSTER_GAP = 2.0
CLUSTER_SPAN = 8.0
BATCH_MAX = 16          # 一次命令最多吐几张
MAX_SEEN = 8000         # 去重表上限, 超了连同待办一起清空重来
MAX_QUEUE = 600         # 待抽条目上限(缩放刷屏时留最新的)
IDLE_MS = 20            # 队列空了歇多久再来看(70ms 时"第一张等到发慌")


def quantize(t: float, q: float = FRAME_Q) -> float:
    if q <= 0:
        return max(0.0, float(t))
    return round(max(0.0, float(t)) / q) * q


def step_for(span_sec: float) -> float:
    """这一格代表多少秒素材 → 该用哪一档量化粒度。

    档位固定成阶梯(而不是直接用 span), 是为了让相邻几个缩放级别算出同一个键,
    来回缩放时能命中同一份缓存。
    """
    try:
        span = abs(float(span_sec))
    except Exception:
        return FRAME_Q
    if not (span > 0):
        return FRAME_Q
    for s in STEP_LADDER:
        if s >= span:
            return s
    return STEP_LADDER[-1]


def _film_dir() -> str:
    path = os.path.join(cache_dir(), "film")
    os.makedirs(path, exist_ok=True)
    return path


def frame_path(source: str, t: float, w: int = FRAME_W, q: float = FRAME_Q) -> str:
    """缓存文件路径(键含 修改时间+大小+量化档, 文件换了就自动重抽)。"""
    try:
        st = os.stat(source)
        raw = (f"{os.path.normcase(os.path.abspath(source))}|{st.st_mtime_ns}|"
               f"{st.st_size}|{quantize(t, q):.2f}|{int(w)}|{q:g}")
    except OSError:
        raw = f"{source}|{quantize(t, q):.2f}|{int(w)}|{q:g}"
    key = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]
    return os.path.join(_film_dir(), key + ".jpg")


def cached(source: str, t: float, w: int = FRAME_W, q: float = FRAME_Q) -> QImage | None:
    dest = frame_path(source, t, w, q)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        img = QImage(dest)
        if not img.isNull():
            return img
    return None


def extract(ffmpeg: str, source: str, t: float, w: int = FRAME_W,
            q: float = FRAME_Q) -> QImage | None:
    """同步抽一帧(已缓存就直接返回)。失败返回 None。

    ⚠️ 返回 QImage 而不是 QPixmap: 这个函数是在**工作线程**里跑的, QPixmap 只能
    在界面线程使用(跨线程共享同一份像素数据会踩内存), QImage 才是线程安全的。
    界面线程拿到后再 `QPixmap.fromImage()`。
    """
    hit = cached(source, t, w, q)
    if hit is not None:
        return hit
    if not ffmpeg or not os.path.isfile(source):
        return None
    dest = frame_path(source, t, w, q)
    try:
        from ..merge import proc as P
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error",
               "-ss", f"{quantize(t, q):.3f}", "-i", source,
               "-frames:v", "1", "-vf", f"scale={int(w)}:-2",
               "-q:v", "4", "-y", dest]
        P.run(cmd, timeout=25)
    except Exception:
        return None
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        img = QImage(dest)
        return img if not img.isNull() else None
    return None


def extract_batch(ffmpeg: str, source: str, items, w: int = FRAME_W):
    """一次 ffmpeg 抽多帧。

    items: [(时间点, 量化档), ...] —— 时间点已经是量化过的值。
    返回 {时间点: QImage}; 返回 None 表示"这批没成功", 调用方退回逐张抽。

    ⚠️ 为什么敢批处理: select 里的 t 是**相对 -ss 起点**的秒数(ffmpeg 9 实测),
    所以每个目标时间要先减去 seek 起点; 减完之后与"单独抽那一张"像素完全一致。

    ⚠️ 为什么一律校验张数: 只要窗口里多命中或少命中一张, 后面所有帧就整体错位
    (画面配错时间), 所以张数对不上就整批作废, 宁可慢也不给错图。
    """
    if not ffmpeg or not os.path.isfile(source):
        return None
    pairs = sorted({(round(float(t), 3), float(q)) for t, q in items})
    if len(pairs) < 2:
        return None
    times = [t for t, _ in pairs]
    start = max(0.0, times[0] - 0.5)
    expr = "+".join("between(t\\,%.3f\\,%.3f)" % (t - start - 0.02, t - start + 0.02)
                    for t in times)
    tmp = tempfile.mkdtemp(prefix="fsb_")
    try:
        from ..merge import proc as P
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error",
               "-ss", f"{start:.3f}", "-i", source,
               "-vf", f"select='{expr}',scale={int(w)}:-2",
               # ⚠️ 必须限定输出张数: 不限的话 ffmpeg 会把**整段都解码到文件尾**
               # 才收工(实测 5 张要从 1660ms 掉回 140ms), 加了才是"抽完就走"。
               "-frames:v", str(len(times)),
               "-fps_mode", "vfr", "-q:v", "4",
               "-y", os.path.join(tmp, "f%04d.jpg")]
        # 超时按张数放宽: 一簇最多 BATCH_MAX 张, 正常一两秒就完
        P.run(cmd, timeout=20 + 2 * len(times))
        files = sorted(f for f in os.listdir(tmp)
                       if f.startswith("f") and f.endswith(".jpg"))
        if len(files) != len(times):
            return None                       # 错位的风险 > 慢一点
        out = {}
        for i, name in enumerate(files):
            t, q = pairs[i]
            src = os.path.join(tmp, name)
            try:
                if os.path.getsize(src) <= 0:
                    return None
            except OSError:
                return None
            dest = frame_path(source, t, w, q)
            try:
                os.replace(src, dest)          # 同一盘: 原子替换
            except OSError:
                try:
                    shutil.move(src, dest)
                except Exception:
                    return None
            img = QImage(dest)
            if img.isNull():
                return None
            out[t] = img
        return out
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class FrameWishlist:
    """待抽清单(多条线程共享)。

    和原来"一条队列挨个取"的区别:
    - 同一 (源, 时间) 只留一份(进来就去重);
    - 取的时候按**源轮转**, 多个片段同时往前长, 不会一个长片段把别的全堵住;
    - 一次取"一簇"(时间挨得近的几张), 正好喂给 extract_batch 一次抽完。
    """

    def __init__(self):
        self._mutex = QMutex()
        self._src: dict = {}        # source -> {量化时间: 量化档}
        self._order: list = []
        self._seen: set = set()
        self._dead: set = set()     # 抽不出来的(比如时间点落在文件末尾外)
        self._rr = 0

    def clear(self):
        """缩放/滚动之后丢掉过期的(下次重绘会重新要一遍)。"""
        with QMutexLocker(self._mutex):
            self._src.clear()
            self._order.clear()
            self._seen.clear()
            self._dead.clear()

    def fail(self, source: str, key: float):
        """这一张抽不出来: 记下来, 别每重绘一次就去起一次 ffmpeg。

        最后那一格常常正好落在文件末尾之外 —— 不加这道防线的话, 每次缩放/滚动
        都会再起一个注定失败的子进程去撞一次。
        """
        if not source:
            return
        with QMutexLocker(self._mutex):
            self._dead.add((source, float(key)))

    def add(self, source: str, items):
        """登记需求。items = [(时间, 量化档), ...], 也可以是只有时间的列表。"""
        if not source:
            return
        with QMutexLocker(self._mutex):
            for it in items:
                try:
                    if isinstance(it, (tuple, list)):
                        t, q = float(it[0]), float(it[1])
                    else:
                        t, q = float(it), FRAME_Q
                except Exception:
                    continue
                k = quantize(t, q)
                key = (source, k)
                if key in self._seen or key in self._dead:
                    continue
                self._seen.add(key)
                d = self._src.get(source)
                if d is None:
                    d = self._src[source] = {}
                    self._order.append(source)
                d[k] = q
        # 留着没意义(画面早过时了), 清了重来; 下面一轮重绘会补回来。
        # ⚠️ _dead 不清: 它记的是"这个文件压根抽不出来", 与画面无关。
        if len(self._seen) > MAX_SEEN or sum(
                len(d) for d in self._src.values()) > MAX_QUEUE:
            self._src.clear()
            self._order.clear()
            self._seen.clear()

    def take(self, max_items: int = BATCH_MAX):
        """取一簇活儿: [(source, 量化时间, 量化档), ...]; 没活返回 None。"""
        with QMutexLocker(self._mutex):
            n = len(self._order)
            if n <= 0:
                return None
            src = None
            d = None
            for i in range(n):
                cand = self._order[(self._rr + i) % n]
                got = self._src.get(cand)
                if got:
                    src, d = cand, got
                    self._rr = (self._rr + i + 1) % n
                    break
            if not d:
                self._order = [s for s in self._order if self._src.get(s)]
                self._rr = 0
                return None
            ordered = sorted(d.items())
            picked = [ordered[0]]
            for k, q in ordered[1:]:
                if k - picked[-1][0] > CLUSTER_GAP:      # 中间隔太远, 别硬凑
                    break
                if k - picked[0][0] > CLUSTER_SPAN:      # 整簇跨度封顶(见上面注释)
                    break
                if len(picked) >= max_items:
                    break
                picked.append((k, q))
            for k, _q in picked:
                d.pop(k, None)
            if not d:
                self._src.pop(src, None)
                if src in self._order:
                    self._order.remove(src)
                n2 = len(self._order)
                self._rr = self._rr % n2 if n2 else 0
            return [(src, k, q) for k, q in picked]


class _StripThread(QThread):
    """一条抽帧线程; 几条这样的线程共用一张 FrameWishlist。"""

    frameReady = Signal(str, float, QImage)     # source, 量化后的时间, 图

    def __init__(self, wish: FrameWishlist, ffmpeg_box: dict, parent=None):
        super().__init__(parent)
        self._wish = wish
        self._ffmpeg_box = ffmpeg_box
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def run(self):
        while not self._stop_flag:
            job = self._wish.take()
            if job is None:
                self.msleep(IDLE_MS)
                continue
            ffmpeg = self._ffmpeg_box.get("exe") or ""
            if not ffmpeg:
                try:
                    from .thumbs import _ffmpeg_exe
                    ffmpeg = _ffmpeg_exe() or ""
                    self._ffmpeg_box["exe"] = ffmpeg
                except Exception:
                    ffmpeg = ""
            if not ffmpeg:
                self.msleep(120)
                continue
            source = job[0][0]
            try:
                if len(job) >= 2:
                    got = extract_batch(ffmpeg, source,
                                        [(t, q) for _s, t, q in job], FRAME_W)
                    if got:
                        for t, img in got.items():
                            if self._stop_flag:
                                return
                            if img is not None and not img.isNull():
                                self.frameReady.emit(source, t, img)
                        continue
                    # 批处理没成(通常是那一段 seeking 有点怪) → 退回一张张抽
                for _s, t, q in job:
                    if self._stop_flag:
                        return
                    img = extract(ffmpeg, source, t, FRAME_W, q)
                    if img is not None and not img.isNull():
                        self.frameReady.emit(source, t, img)
                    else:
                        self._wish.fail(source, t)   # 记进黑名单, 别再撞了
            except Exception:
                continue


def thread_count(default: int = 0) -> int:
    """开几条抽帧线程。

    抽帧是 ffmpeg 子进程在干活(它自己还会开多线程解码), Python 这边只是等,
    所以不用给太多; 多了反而把别的活(预览/波形/导出)挤慢。
    """
    if default > 0:
        return max(1, int(default))
    try:
        cores = os.cpu_count() or 4
    except Exception:
        cores = 4
    if cores >= 8:
        return 3
    if cores >= 4:
        return 2
    return 1


class FilmstripWorker(QObject):
    """抽帧的小线程池 + 待抽清单(对外还是以前那个用法)。

    对外保留的方法: set_ffmpeg / request / stop / start / wait / frameReady,
    所以 main_window 那边不用改。
    """

    frameReady = Signal(str, float, QImage)

    def __init__(self, ffmpeg: str | None = None, parent=None, workers: int = 0):
        super().__init__(parent)
        self._wish = FrameWishlist()
        self._ffmpeg_box = {"exe": ffmpeg or ""}
        self._threads: list = []
        n = thread_count(workers)
        for _ in range(n):
            th = _StripThread(self._wish, self._ffmpeg_box, self)
            th.frameReady.connect(self.frameReady)
            self._threads.append(th)

    # ---- 以前的接口 ----
    def set_ffmpeg(self, exe: str | None):
        self._ffmpeg_box["exe"] = exe or ""

    def request(self, source: str, times) -> None:
        self._wish.add(source, times)

    def stop(self):
        for th in self._threads:
            try:
                th.stop()
            except Exception:
                pass

    def start(self, *_a, **_k):
        for th in self._threads:
            if not th.isRunning():
                th.start()

    def clear_pending(self):
        """缩放/滚动之后把还没抽的旧需求丢掉(重绘会重新要当前这一屏)。"""
        self._wish.clear()

    def pending(self) -> int:
        try:
            return sum(len(d) for d in self._wish._src.values())
        except Exception:
            return 0

    # ---- 生命周期(main_window._stop_thread 会调 stop() + wait()) ----
    def wait(self, timeout: int = 3000):
        for th in self._threads:
            try:
                th.wait(int(timeout))
            except Exception:
                pass

    def isRunning(self) -> bool:
        return any(th.isRunning() for th in self._threads)
