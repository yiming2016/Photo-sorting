# -*- coding: utf-8 -*-
"""剪辑态的数据模型: 片段(Clip)与时间线(Timeline)。

v117 起的定位模型:
- **多条通用轨**(默认 3 条, 可增减), 轨道不再区分"视频轨/音频轨", 什么片段都能放;
- 每个片段有**显式起点 start**(时间线秒数), 不再靠"前面片段时长之和"推导,
  所以可以在轨上自由摆放、可以留空隙;
- **同一条轨上不许重叠**(拖过去会自动避开); 不同轨可以同时有画面,
  画面归属按轨道序号: 序号小的在上层, 覆盖下层(与时间线的显示顺序一致);
- `magnetic`(磁吸): 开启时同轨自动合拢、不留空隙 —— 这正是无损拼接需要的前提。
  默认关闭(用户要的是自由摆放), 想无缝时打开。

字段含义:
- duration: 视频/音频 = (src_out - src_in) / speed; 图片 = hold
- keyframes: 源时间轴上的关键帧(能无损下刀的位置), 图片/音频为空
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# 音频扩展名(照片模型里没有"音频"这一类, 这里自己判)
AUDIO_EXT = {
    "mp3", "m4a", "aac", "wav", "flac", "ogg", "oga", "opus", "wma",
    "aiff", "aif", "ape", "m4b", "amr",
}
IMAGE_DEFAULT_SEC = 3.0        # 图片静帧默认时长(秒)
MIN_LEN = 0.05                 # 片段最短时长(秒), 防止裁成 0 或负数
DEFAULT_TRACKS = 3             # 默认轨道数

# 兼容旧引用: 现在只是"第 1 轨 / 第 2 轨", 没有音视频之分
TRACK_MAIN = 0
TRACK_AUDIO = 1

_EPS = 1e-6


def kind_of(path: str) -> str:
    """文件类型: audio / video / image / other。"""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in AUDIO_EXT:
        return "audio"
    from .photo import IMAGE_EXT, VIDEO_EXT      # 复用照片/视频判定(无循环依赖)
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return "other"


@dataclass
class Clip:
    """时间线上的一个片段。

    source  源文件路径
    kind    video / image / audio
    track   所在轨(0 开始)
    start   在时间线上的起点(秒)
    src_in  源入点(秒); 图片忽略
    src_out 源出点(秒); 图片忽略
    hold    图片静帧时长(秒); 仅 kind=="image" 有意义
    speed   倍速(恒 1.0)
    volume  音量倍数(1.0 = 原样; 0.5 = 一半响, 2.0 = 两倍响)。只在导出时
            生效 —— 改它不生成新文件, 撤销也能退回来(v169)
    muted   静音: **用户主动**删掉这个片段的声音(导出时补一段静音轨)
    detached  音轨已分离: 声音被"分离音频"搬到下面那条轨的独立片段上了,
              原片段因此不再出声(不是被静音 —— 声音没丢, 只是换了个位置躺)
              ⚠️ 这两个是两回事, 别混用: 静音是"不要这段声音", 分离是"声音搬走了"
    src_parent  音频片段记着它是从哪个源文件分离出来的(删掉它时好把原视频还原)
    src_duration  源文件的总时长(秒), 裁剪的上界; 没探测到就是 0
    keyframes     源时间轴上的关键帧秒数(能无损下刀的位置); 图片/音频为空
    """

    source: str
    kind: str = "video"
    track: int = TRACK_MAIN
    start: float = 0.0
    src_in: float = 0.0
    src_out: float = 0.0
    hold: float = IMAGE_DEFAULT_SEC
    speed: float = 1.0
    volume: float = 1.0
    muted: bool = False
    detached: bool = False
    src_parent: str = ""
    src_duration: float = 0.0
    keyframes: list = field(default_factory=list)
    name: str = ""

    def __post_init__(self):
        if not self.name:
            self.name = os.path.basename(self.source)

    # ---------------------------------------------------------------- 裁剪
    @property
    def silent(self) -> bool:
        """这一段自己出不出声(静音了, 或者声音已经被分离走了)。"""
        return bool(self.muted or self.detached)

    @property
    def src_limit(self) -> float:
        """裁剪能取到的最大源时间。没探测到总时长时退回当前出点。"""
        if self.src_duration > 0:
            return self.src_duration
        return max(self.src_out, self.src_in)

    def trim_to(self, src_in: float, src_out: float) -> None:
        """裁剪到 [src_in, src_out](源时间轴), 自动夹紧且至少留 MIN_LEN 秒。"""
        lim = self.src_limit
        lo = max(0.0, min(float(src_in), lim - MIN_LEN))
        hi = max(lo + MIN_LEN, min(float(src_out), max(lim, lo + MIN_LEN)))
        if self.kind == "image":
            self.hold = max(0.1, hi - lo)
            return
        self.src_in, self.src_out = lo, hi

    def nearest_keyframe(self, t: float, max_gap: float = 1e9):
        """离 t 最近的关键帧(源时间轴); 没有关键帧 / 超过 max_gap 返回 None。

        v196: 原来两个分支都返回 best, max_gap 形同虚设(拖边对齐关键帧的两处
        调用没传这个参数, 所以没暴露)。现在超出间距就明确返回 None。
        """
        ks = self.keyframes or []
        if not ks:
            return None
        best = min(ks, key=lambda k: abs(k - t))
        return best if abs(best - t) <= max_gap else None

    def timeline_keyframe_times(self) -> list[float]:
        """关键帧(源时间秒) → 这段在**时间线**上的秒数。

        ⚠️ 跳关键帧 / 画红色刻度 / 截图按钮变蓝三处必须共用这一个换算(v186)。
        源时间与时间线时间的关系: 源 = src_in + (时间线 - start) * speed,
        反过来就是下面这个式子。以前跳关键帧漏了 /speed, 变速片段会一次跳过
        好几个关键帧、落点也不在红线上; 变蓝判定又另算一套, 三处永远对不齐。

        只留在片段可见范围 [src_in, src_out] 内的关键帧, 精度不四舍五入 ——
        「变蓝」代表的就是真的能在这儿无损下刀, 差半帧都不算。
        """
        ks = list(self.keyframes or [])
        if not ks or self.kind != "video":
            return []
        try:
            speed = float(self.speed or 1.0)
            src_in = float(self.src_in or 0.0)
            src_out = float(self.src_out or 0.0)
            start = float(self.start or 0.0)
        except Exception:
            return []
        if speed <= 0:
            return []
        out = []
        for k in ks:
            try:
                k = float(k)
            except Exception:
                continue
            if k < src_in - 1e-6 or k > src_out + 1e-6:
                continue
            t = start + (k - src_in) / speed
            if t < start - 1e-6 or t > self.end + 1e-6:
                continue
            out.append(t)
        return sorted(set(out))

    @property
    def duration(self) -> float:
        """片段在时间线上占的时长(秒)。"""
        if self.kind == "image":
            return max(0.1, self.hold)
        d = max(0.0, self.src_out - self.src_in)
        if self.speed and self.speed > 0:
            d = d / self.speed
        return d

    @property
    def end(self) -> float:
        """在时间线上的终点(秒)。"""
        return self.start + self.duration

    @property
    def track_label(self) -> str:
        return f"轨道 {self.track + 1}"

    @property
    def kind_label(self) -> str:
        return {"video": "视频", "image": "图片", "audio": "音频"}.get(self.kind, self.kind)


class Timeline:
    """多条通用轨上的自由摆放; 同轨不重叠, `magnetic` 开启时自动合拢。"""

    def __init__(self, tracks: int = DEFAULT_TRACKS):
        self.clips: list[Clip] = []
        self.playhead: float = 0.0
        self.track_count: int = max(1, int(tracks))
        self.magnetic: bool = False     # 磁吸: 开=同轨首尾相接(无损友好), 关=可留空隙
        self.snap_on: bool = True       # 拖动吸附(片段首尾 / 播放头 / 0)
        # 轨道自定义名: 空串 = 没改过, 显示"轨 N"。下标永远和轨号对齐。
        self.track_names: list[str] = [""] * self.track_count

    # ---------- 轨道 ----------
    def track_label(self, track: int) -> str:
        """轨名的显示文字(没改名就是"轨 N")。"""
        try:
            i = int(track)
        except Exception:
            return ""
        if 0 <= i < len(self.track_names) and (self.track_names[i] or "").strip():
            return self.track_names[i].strip()
        return f"轨 {i + 1}"

    def rename_track(self, track: int, name: str) -> bool:
        """改轨名; 传空串 = 恢复成"轨 N"。"""
        i = int(track)
        if not (0 <= i < self.track_count):
            return False
        self._fit_names()
        self.track_names[i] = (name or "").strip()
        return True

    def _fit_names(self) -> None:
        """让 track_names 的长度和轨数一致(加轨补空、删轨砍掉尾巴)。"""
        n = max(1, int(self.track_count))
        names = list(self.track_names or [])
        if len(names) < n:
            names += [""] * (n - len(names))
        elif len(names) > n:
            names = names[:n]
        self.track_names = names

    def set_track_count(self, n: int) -> bool:
        """改轨道数; 减少时若被删的轨上还有片段就拒绝(返回 False)。"""
        n = max(1, min(12, int(n)))
        if n < self.track_count:
            busy = [c for c in self.clips if c.track >= n]
            if busy:
                return False
        self.track_count = n
        for c in self.clips:
            c.track = min(c.track, n - 1)
        self._fit_names()
        return True

    def add_track(self) -> int:
        self.track_count = min(12, self.track_count + 1)
        self._fit_names()
        return self.track_count

    def remove_track(self, track: int) -> bool:
        """删掉一条空轨(有片段的轨不许删)。"""
        if self.track_count <= 1:
            return False
        if any(c.track == track for c in self.clips):
            return False
        self._fit_names()
        for c in self.clips:
            if c.track > track:
                c.track -= 1
        if 0 <= track < len(self.track_names):
            del self.track_names[track]
        self.track_count -= 1
        self._fit_names()
        return True

    # ---------- 增删 ----------
    def add(self, path: str, duration: float | None = None,
            track: int = TRACK_MAIN, start: float | None = None) -> Clip | None:
        """把文件放到指定轨; start=None 表示接在该轨末尾。不支持的类型返回 None。"""
        kind = kind_of(path)
        if kind not in ("video", "image", "audio"):
            return None
        track = max(0, min(self.track_count - 1, int(track)))
        clip = Clip(source=path, kind=kind, track=track,
                    src_in=0.0, src_out=float(duration or 0.0))
        if start is None:
            start = self.track_end(track)
        clip.start = max(0.0, float(start))
        self.clips.append(clip)
        self.place(clip, track, clip.start)
        return clip

    def insert_clip(self, clip: Clip, track: int, index: int) -> Clip:
        """兼容旧调用: 插到某轨第 index 个片段的位置(按时间推导落点)。"""
        lst = self.track_clips(track)
        index = max(0, min(len(lst), int(index)))
        start = lst[index].start if index < len(lst) else self.track_end(track)
        clip.track = max(0, min(self.track_count - 1, int(track)))
        self.clips.append(clip)
        self.place(clip, clip.track, start)
        return clip

    def remove(self, clip: Clip) -> None:
        if clip in self.clips:
            self.clips.remove(clip)
            self._reattach_audio(clip)

    def _reattach_audio(self, gone: Clip) -> None:
        """删掉一个"分离出来的音频片段" → 原视频的声音要还回去。

        ⚠️ 不还原的话: 用户把抽出来的音频删了, 原视频还挂着 detached(不出声),
        界面上还写着"已分离" —— 于是"删了个没用的东西, 结果整段没声音了",
        而且看不出为什么。
        """
        if gone.kind != "audio":
            return
        src = getattr(gone, "src_parent", "") or ""
        if not src:
            return
        if any(c.kind == "audio" and (c.src_parent or "") == src
               for c in self.clips):
            return                          # 还有别的音频片段顶着
        for c in self.clips:
            if c.source == src and c.detached:
                c.detached = False

    def clear(self) -> None:
        self.clips.clear()
        self.playhead = 0.0

    def can_split_at(self, clip: Clip, t_global: float) -> bool:
        """这里能不能下刀(**只问不改**)。

        ⚠️ 菜单/按钮要判断"分割可不可点"时只能问这个: 以前直接调 `split_at()`
        看返回值, 于是**光是右键弹出菜单就把片段切成两段了**(v146 用户报的
        "分离音频之后视频变成两段, 像按了一下分割")。
        ⚠️ 图片那侧的边界是 0.1 而不是 MIN_LEN: `Clip.duration` 对图片有
        `max(0.1, hold)` 的下限, 若只留 0.05, 前半段会被撑到 0.1 秒, 而
        后半段从 0.06 开始 —— 两段重叠 0.04 秒(同轨不许重叠)。
        """
        if clip is None or clip not in self.clips:
            return False
        off = t_global - clip.start
        margin = max(MIN_LEN, 0.1) if clip.kind == "image" else MIN_LEN
        return margin < off < clip.duration - margin

    def split_at(self, clip: Clip, t_global: float) -> Clip | None:
        """在时间线时间 t_global 处把片段切成两段, 返回后半段(太靠边则不切)。

        切的是源时间轴: 前半段收尾到切点, 后半段从切点起头, 都不重新编码。
        """
        if not self.can_split_at(clip, t_global):
            return None
        off = t_global - clip.start
        idx = self.clips.index(clip)
        if clip.kind == "image":
            right = Clip(source=clip.source, kind="image", track=clip.track,
                         start=t_global, hold=clip.duration - off,
                         name=clip.name)
            clip.hold = off
        else:
            span = off * (clip.speed or 1.0)          # 时间线时长 → 源时长
            mid = clip.src_in + span
            right = Clip(source=clip.source, kind=clip.kind, track=clip.track,
                         start=t_global, src_in=mid, src_out=clip.src_out,
                         speed=clip.speed, volume=clip.volume,
                         muted=clip.muted,
                         detached=clip.detached, src_parent=clip.src_parent,
                         src_duration=clip.src_duration,
                         keyframes=list(clip.keyframes or []),
                         name=clip.name)
            clip.src_out = mid
        self.clips.insert(idx + 1, right)
        return right

    # ---------- 位置 ----------
    def track_clips(self, track: int, sorted_by_time: bool = True) -> list[Clip]:
        lst = [c for c in self.clips if c.track == track]
        if sorted_by_time:
            lst.sort(key=lambda c: (c.start, c.name))
        return lst

    def clip_start(self, clip: Clip) -> float:
        return clip.start

    def clip_end(self, clip: Clip) -> float:
        return clip.end

    def track_end(self, track: int) -> float:
        """该轨占用到的时间点(最后一个片段的终点)。"""
        ends = [c.end for c in self.clips if c.track == track]
        return max(ends) if ends else 0.0

    def track_duration(self, track: int) -> float:
        return self.track_end(track)

    def total_duration(self) -> float:
        return max([self.track_end(t) for t in range(self.track_count)] or [0.0])

    def clip_at(self, track: int, t: float) -> Clip | None:
        for c in self.track_clips(track):
            if c.start - _EPS <= t < c.end - _EPS:
                return c
        return None

    def top_clip_at(self, t: float) -> Clip | None:
        """同一时刻多条轨都有画面时, 取轨号最小的那条(上层覆盖下层)。"""
        for track in range(self.track_count):
            c = self.clip_at(track, t)
            if c is not None and c.kind != "audio":
                return c
        return None

    # ---------- 摆放 / 避让 ----------
    def spans(self, track: int, ignore=None) -> list[tuple[float, float]]:
        """该轨上所有片段占的区间(排好序)。

        `ignore` 可以是单个片段, 也可以是一批(多选拖动时整组都要排除)。
        """
        if ignore is None:
            skip = ()
        elif isinstance(ignore, (list, tuple, set, frozenset)):
            skip = tuple(ignore)
        else:
            skip = (ignore,)
        out = []
        for c in self.clips:
            if c.track != track or any(c is s for s in skip):
                continue
            out.append((c.start, c.end))
        out.sort()
        return out

    def clamp_start(self, track: int, start: float, dur: float, cur: float,
                    ignore=None) -> float:
        """把 start 夹到**当前这个空隙**里, 不许压到邻居身上(拖动时用)。

        和 `free_slot` 的分工:
          free_slot 允许**绕过去** —— 撞了就跳到别的空隙, 而"离得最近的空位"
          常常正好是原位, 于是用户看到"松手被弹回去";
          clamp_start 只许在**当前空隙里滑动**, 推到邻居身上就停住 ——
          "重叠这个动作根本不发生", 手感是撞墙, 不是弹回。
        """
        start = max(0.0, float(start))
        dur = max(0.0, float(dur))
        spans = self.spans(track, ignore)
        # 参考点自己就撞上了(刚拖到别的轨): 先找个不撞的落点再说
        if self._hit(float(cur), dur, spans):
            return self.free_slot(track, cur, dur, ignore)
        lo, hi = 0.0, None
        for s, e in spans:
            if s >= cur - _EPS:                       # 在参考点右边 → 顶到它头上
                hi = (s - dur) if hi is None else min(hi, s - dur)
            else:                                     # 在参考点左边 → 贴着它尾巴
                lo = max(lo, e)
        if hi is None:                                # 右边空着, 随便往右
            hi = start
        hi = max(lo, hi)
        return max(lo, min(hi, start))

    @staticmethod
    def _hit(a: float, dur: float, spans: list[tuple[float, float]]) -> bool:
        b = a + max(0.0, dur)
        for s, e in spans:
            if a < e - _EPS and s < b - _EPS:
                return True
        return False

    def overlaps(self, track: int, start: float, dur: float,
                 ignore: Clip | None = None) -> bool:
        return self._hit(float(start), float(dur), self.spans(track, ignore))

    def free_slot(self, track: int, start: float, dur: float,
                  ignore: Clip | None = None) -> float:
        """离 start 最近的不重叠位置(只在该轨上空隙里找)。"""
        start = max(0.0, float(start))
        dur = max(0.0, float(dur))
        spans = self.spans(track, ignore)
        if not self._hit(start, dur, spans):
            return start
        cands = [0.0, start]
        for s, e in spans:
            cands.append(e)
            cands.append(max(0.0, s - dur))
        best = None
        for c in cands:
            c = max(0.0, c)
            if self._hit(c, dur, spans):
                continue
            d = abs(c - start)
            if best is None or d < best[0] - _EPS:
                best = (d, c)
        return best[1] if best is not None else start

    def place(self, clip: Clip, track: int, start: float) -> None:
        """把片段放到 track 上 start 处; 重叠就挪到最近空位; 磁吸时整轨合拢。"""
        clip.track = max(0, min(self.track_count - 1, int(track)))
        clip.start = self.free_slot(clip.track, start, clip.duration, ignore=clip)
        if self.magnetic:
            self.compact(clip.track)

    def insert_point(self, track: int, start: float,
                     ignore: list[Clip] | None = None) -> float:
        """v195: 挤占的落点必须是一个「插入边界」(片段首尾 / 空隙), 不能落在
        某个已有片段的中间。

        ⚠️ 为什么必须对齐: insert_place 只顺延**起点在落点之后**的片段。落点
           压在 A 中间时, A 的起点更早、不会被推, 松手就变成 A 和 D 同轨重叠,
           既违反本模块「同一条轨上不许重叠」, 也违反用户定的「不裁不覆盖」。
           落在片段内部时, 就近对齐到它的起点或终点(等距取起点); 本来就在
           边界 / 空隙上的落点原样返回。
        """
        at = max(0.0, float(start))
        for s, e in self.spans(track, ignore):
            if s + _EPS < at < e - _EPS:
                return s if (at - s) <= (e - at) else e
        return at

    def insert_place(self, clip: Clip, track: int, start: float,
                     ignore: list[Clip] | None = None) -> None:
        """v194「挤占」: 把 clip **插入** track/start, 落点之后的同轨片段整体
        右移 clip.duration —— 只推目标轨, 不裁不覆盖, 其它轨道保持原位。

        和 place() 的区别: place() 走 free_slot(), 只在现成空隙里挑位置,
        永远不推动已有片段(D 拖到 A/B 之间会被弹到末尾); 这里才是插入编辑。
        ⚠️ 调用时 clip.start / clip.track 已经是拖动过程中改过的落点, 所以
           先把「别人」顺延, 再把 clip 钉回 at —— 顺序反了会把自己也推走。
        ⚠️ v195: at 先过 insert_point(), 保证落点不压在任何片段内部。
        """
        track = max(0, min(self.track_count - 1, int(track)))
        skip_list = [clip] + list(ignore or [])
        at = self.insert_point(track, start, skip_list)
        dur = max(0.0, float(clip.duration))
        skip = {id(c) for c in skip_list}
        for c in self.clips:
            if c.track != track or id(c) in skip:
                continue
            if c.start >= at - _EPS:
                c.start += dur
        clip.track = track
        clip.start = at
        if self.magnetic:
            self.compact(track)

    def move_to(self, clip: Clip, track: int, start: float) -> None:
        self.place(clip, track, start)

    def compact(self, track: int) -> None:
        """磁吸: 该轨按起点排序后首尾相接, 不留空隙。"""
        t = 0.0
        for c in self.track_clips(track):
            c.start = t
            t += c.duration

    # ---------- 吸附 ----------
    def snap_targets(self, ignore: Clip | None = None,
                     include_playhead: bool = True) -> list[float]:
        """可吸附的时间点: 所有片段的首尾 + 播放头 + 时间轴开头。"""
        out = {0.0}
        for c in self.clips:
            if c is ignore:
                continue
            out.add(round(c.start, 3))
            out.add(round(c.end, 3))
        if include_playhead:
            out.add(round(self.playhead, 3))
        return sorted(out)

    def snap(self, t: float, ignore: Clip | None, tolerance: float) -> tuple[float, float | None]:
        """t 靠近某个吸附点(容差内)就对齐; 返回 (对齐后的 t, 命中的吸附点)。"""
        if not self.snap_on or tolerance <= 0:
            return t, None
        best = None
        for c in self.snap_targets(ignore):
            d = abs(c - t)
            if d <= tolerance and (best is None or d < best[0]):
                best = (d, c)
        if best is None:
            return t, None
        return best[1], best[1]

    def snap_move(self, clip: Clip, start: float, dur: float,
                  tolerance: float) -> tuple[float, float | None]:
        """拖整个片段: 首尾都能吸附, 取更近的那一端。"""
        if not self.snap_on or tolerance <= 0:
            return start, None
        best = None
        for target in self.snap_targets(clip):
            d1 = abs(start - target)                  # 首
            if d1 <= tolerance and (best is None or d1 < best[0]):
                best = (d1, target)
            d2 = abs(start + dur - target)            # 尾
            if d2 <= tolerance and (best is None or d2 < best[0]):
                best = (d2, target - dur)
        if best is None:
            return start, None
        return max(0.0, best[1]), best[1]

    # ---------- 旧接口 ----------
    def move_clip(self, clip: Clip, target_index: int) -> None:
        """兼容旧调用: 移到同轨第 target_index 个位置(按时间重排)。"""
        if clip not in self.clips:
            return
        lst = [c for c in self.track_clips(clip.track) if c is not clip]
        target_index = max(0, min(len(lst), int(target_index)))
        t = lst[target_index].start if target_index < len(lst) else self.track_end(clip.track)
        self.place(clip, clip.track, t)

    # ---------- 撤销 / 重做 ----------
    # 为什么用"整表快照"而不是"记录每个操作的反向动作":
    # 时间线上的操作互相牵连(拖动会避让、裁剪会吸关键帧、加轨会夹轨号),
    # 反向动作要写一大套而且极容易漏。整表快照只是一份 dict 列表, 快、稳、不会漏。
    def snapshot(self) -> dict:
        """当前时间线的完整状态(纯数据, 可直接比较/复制)。"""
        return {
            "track_count": int(self.track_count),
            "track_names": list(self.track_names or []),
            "playhead": float(self.playhead),
            "magnetic": bool(self.magnetic),
            "clips": [{
                "source": c.source,
                "kind": c.kind,
                "track": int(c.track),
                "start": float(c.start),
                "src_in": float(c.src_in),
                "src_out": float(c.src_out),
                "hold": float(c.hold),
                "speed": float(c.speed or 1.0),
                # v169: 音量也进快照 —— 漏掉的话"改个音量 → 撤销"会退回原响度
                "volume": float(getattr(c, "volume", 1.0) or 1.0),
                "muted": bool(c.muted),
                "detached": bool(c.detached),
                "src_parent": str(c.src_parent or ""),
                "src_duration": float(c.src_duration),
                "keyframes": list(c.keyframes or []),
                # ⚠️ 名字必须进快照(v158): 不然"改个名 → 撤销"会把名字丢回文件名,
                #    而且任何一次别的编辑之后撤销, 之前改过的名字也全没了
                #    (restore 是重建片段对象, 不带上就是默认的 basename)。
                "name": str(c.name or ""),
            } for c in self.clips],
        }

    def restore(self, snap: dict) -> None:
        """回到某个快照。片段对象会重建, 外部持有的旧引用要重新取。"""
        snap = snap or {}
        self.track_count = max(1, int(snap.get("track_count") or 1))
        names = snap.get("track_names")
        self.track_names = [str(v or "") for v in names] if isinstance(names, list) else []
        self._fit_names()
        self.playhead = max(0.0, float(snap.get("playhead") or 0.0))
        self.magnetic = bool(snap.get("magnetic"))
        clips: list[Clip] = []
        for row in snap.get("clips") or []:
            clips.append(Clip(
                source=row.get("source", ""),
                kind=row.get("kind", "video"),
                track=max(0, min(self.track_count - 1, int(row.get("track") or 0))),
                start=max(0.0, float(row.get("start") or 0.0)),
                src_in=float(row.get("src_in") or 0.0),
                src_out=float(row.get("src_out") or 0.0),
                hold=float(row.get("hold") or IMAGE_DEFAULT_SEC),
                speed=float(row.get("speed") or 1.0),
                volume=max(0.0, float(row.get("volume") or 1.0)),
                muted=bool(row.get("muted")),
                detached=bool(row.get("detached")),
                src_parent=str(row.get("src_parent") or ""),
                src_duration=float(row.get("src_duration") or 0.0),
                keyframes=list(row.get("keyframes") or []),
                name=str(row.get("name") or ""),
            ))
        self.clips = clips
