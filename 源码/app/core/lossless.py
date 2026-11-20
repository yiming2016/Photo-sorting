# -*- coding: utf-8 -*-
"""无损剪辑判定: 这条时间线能不能「不重新编码」就导出来。

用户定的三条铁律(不要改):
1. 能无损就无损 —— 导出优先走 `-c copy`(秒级、画质零损失);
2. 做不到无损的**必须当场说清楚**, 不许闷头转码;
3. 画面重叠 = 必须重新合成 = 直接判「不建议剪辑」;
   切点不在关键帧 → 自动贴到最近关键帧(入点最多差零点几秒), 仍然无损。

判定结果分三档:
- fatal  不改就导不出来无损(必须重新编码) → 界面标红 + 顶部写「不建议剪辑」
- warn   能无损, 但会有副作用(空隙被吃掉 / 静音要重生成音轨 / 没读到关键帧)
- info   自动处理过的细节(入点贴关键帧, 差了多少秒)

导出的序列在 `ExportPlan.segments` 里: 已按时间排序、已吸附关键帧,
`merge/export.py` 拿它直接干活, 不再重新判定。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .clips import Clip, Timeline

FATAL = "fatal"
WARN = "warn"
INFO = "info"

_EPS = 1e-4
_SHIFT_EPS = 0.02          # 入点偏差小于这个就不打扰用户
_KF_MAX_SHIFT = 2.0        # 入点偏移超过 2 秒说明关键帧太稀, 值得提醒


@dataclass
class Reason:
    """一条「为什么不能无损」, 按"故障 → 涉及谁 → 差在哪 → 会怎样"来组织。

    用户的要求: 不要写技术参数对比, 要说**可能出现什么故障**
    (比如"掉帧、花屏"、"这一段可能拼不上"), 关键的字要加粗。
    所以这里拆成四段, 界面用富文本渲染(items 的 label 加粗):
    - title    故障本身        「编码参数不一致」
    - subject  涉及哪些素材    「视频A、视频B」
    - items    差在哪          [("分辨率", "1920×1080 与 1280×720 不一样")]
    - effect   会出什么事      「拼起来会掉帧、花屏」
    """

    level: str
    code: str
    text: str = ""                              # 纯文本兜底(顶部红条/日志)
    title: str = ""
    subject: str = ""
    items: list = field(default_factory=list)   # [(标签, 值)] 标签加粗
    effect: str = ""
    clips: list = field(default_factory=list)   # 涉及的片段(时间线上要标红)
    fix: str = ""         # 一键修复动作标识(空=没法自动修)
    fix_text: str = ""    # 修复按钮上的字

    def __post_init__(self):
        if not self.title:
            self.title = self.text
        if not self.text:
            self.text = self.plain()

    @property
    def fatal(self) -> bool:
        return self.level == FATAL

    @property
    def key(self) -> str:
        return self.code

    def plain(self) -> str:
        """一句话版本(给顶部红条、状态栏、日志用)。"""
        parts = [self.title]
        if self.subject:
            parts.append(f"（{self.subject}）")
        if self.items:
            parts.append("：" + "；".join(f"{k}{v}" for k, v in self.items))
        if self.effect:
            parts.append(f" → {self.effect}")
        return "".join(parts)


@dataclass
class Segment:
    """导出序列里的一段: 源 + 源时间轴上的区间 + 在时间线上的位置。"""

    clip: Clip
    kind: str
    src_in: float = 0.0
    src_out: float = 0.0
    duration: float = 0.0      # 时间线上占的时长
    start: float = 0.0         # 时间线上的位置(排序用, 导出时会被压掉空隙)
    muted: bool = False
    speed: float = 1.0
    volume: float = 1.0       # v169: 音量倍数(1.0 = 原样)
    in_shift: float = 0.0      # 入点因贴关键帧产生的偏移: 正=后移(少保留了一点)

    @property
    def source(self) -> str:
        return self.clip.source

    @property
    def name(self) -> str:
        return self.clip.name


@dataclass
class ExportPlan:
    """一次导出的完整方案。"""

    ok: bool = False           # 能不能无损
    mode: str = "empty"        # "lossless" / "reencode" / "empty" / "blocked"
    segments: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    total: float = 0.0         # 导出后的总时长(已去掉空隙)
    gap: float = 0.0           # 被吃掉的空隙总秒数

    @property
    def exportable(self) -> bool:
        """能不能点导出(没片段 / 没 ffmpeg 时连转码都做不了)。"""
        return self.mode in ("lossless", "reencode")

    @property
    def fatal_reasons(self) -> list:
        return [r for r in self.reasons if r.level == FATAL]

    @property
    def warn_reasons(self) -> list:
        return [r for r in self.reasons if r.level == WARN]

    def summary(self) -> str:
        """一句话结论(给状态胶囊和弹窗标题用)。"""
        if self.mode == "empty":
            return "时间线上还没有片段"
        if self.mode == "blocked":
            return "现在还导不出来"
        if self.ok:
            return "可以无损拼接"
        return "不建议剪辑"

    def bad_clips(self) -> set:
        """所有被判致命的片段(时间线上标红用)。"""
        out = set()
        for r in self.fatal_reasons:
            for c in r.clips:
                out.add(id(c))
        return out

    def warn_clips(self) -> set:
        out = set()
        for r in self.warn_reasons:
            for c in r.clips:
                out.add(id(c))
        return out


# ---------------------------------------------------------------- 规格比对
def _fatal_keys() -> list:
    from ..merge import grouping
    return [k for k, _cn, lv in grouping.FIELD_LEVELS if lv == "fatal"]


def _fmt_value(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, tuple):
        return "×".join(_fmt_value(x) for x in v)
    s = str(v)
    return s if len(s) <= 18 else s[:17] + "…"


def spec_diff(a: dict, b: dict, limit: int = 4) -> list[tuple[str, str]]:
    """两个规格不一样的地方, 返回 [(参数名, 人话差异)]; 参数名在界面上加粗。"""
    from ..merge import grouping
    keys = _fatal_keys()
    sig_a, sig_b = grouping.signature(a or {}), grouping.signature(b or {})
    if len(sig_a) != len(keys) or len(sig_b) != len(keys):
        return []
    cn = {k: c for k, c, _lv in grouping.FIELD_LEVELS}
    out = []
    for i, k in enumerate(keys):
        va, vb = sig_a[i], sig_b[i]
        if va != vb:
            out.append((cn.get(k, k) + "：",
                        f"{_fmt_value(va)} 与 {_fmt_value(vb)} 不一样"))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 判定
def evaluate(timeline: Timeline, specs: dict | None = None,
             ffmpeg_ok: bool = True) -> ExportPlan:
    """看一遍时间线, 给出能不能无损 + 具体原因 + 导出序列。

    specs: {路径: 探测到的规格}; 传 None/缺项时规格比不了 → 提示"还没读完"。
    """
    specs = specs or {}
    plan = ExportPlan()
    clips = list(getattr(timeline, "clips", []) or [])
    if not clips:
        plan.mode = "empty"
        return plan
    if not ffmpeg_ok:
        plan.mode = "blocked"
        plan.reasons.append(Reason(
            FATAL, "no_ffmpeg",
            title="没找到 FFmpeg",
            effect="没有它既不能无损拼接、也不能转码，先到右栏左下角指定它的路径"))
        return plan

    ordered = sorted(clips, key=lambda c: (c.start, c.track, c.name))

    # ---- 1. 重叠: 两条轨上同时有**画面** → 必须重新合成 ----
    # ⚠️ 音频不算: 音乐压在视频上是最正常的用法(导出时本来就是混音),
    #    算进来会把每条带配乐的时间线都判成"不建议剪辑"。
    visual = [c for c in ordered if c.kind != "audio"]
    for a, b in zip(visual, visual[1:]):
        if b.start < a.end - _EPS:
            plan.reasons.append(Reason(
                FATAL, "overlap",
                title="画面叠在一起了",
                subject=_names([a, b]),
                items=[("叠在：", f"{fmt_sec(b.start)} 这一时刻，两层画面同时存在")],
                effect="两层画面必须合成一层，一定要重新编码",
                clips=[a, b], fix="separate", fix_text="挪开"))
            break          # 一条就够, 修完再看还有没有

    # ---- 2. 逐片段的硬伤 ----
    images = [c for c in ordered if c.kind == "image"]
    if images:
        plan.reasons.append(Reason(
            FATAL, "image",
            title=f"有 {len(images)} 个图片素材",
            subject=_names(images),
            items=[("为什么：", "图片是静帧，本身没有视频流")],
            effect="要变成能播的视频就得逐帧编出来，画面一定是重新编码的",
            clips=images, fix="drop_image", fix_text="去掉图片"))

    # v185: 变速不再是"一票否决整条片子"。导出时只把这几段的时间轴按比例
    # 压缩(stream copy, 画面一帧不重编码), 其余片段照旧无损拼接。
    # 所以这里降成 WARN —— 仍列出来让人看见, 但不再把 plan.mode 推到 reencode。
    fast = [c for c in ordered if abs((c.speed or 1.0) - 1.0) > 1e-6]
    if fast:
        plan.reasons.append(Reason(
            WARN, "speed",
            title=f"{len(fast)} 段改了速度",
            subject=_names(fast),
            items=[("倍速：", "、".join(f"{c.speed:g}×" for c in fast[:3]))],
            effect="导出时只有这几段单独变速，其余片段仍然无损拼接",
            clips=fast, fix="reset_speed", fix_text="恢复 1 倍速"))

    # v169: 音量 ≠ 100% 的段, 声音必须重算(音量滤镜做不了流复制)。
    # 画面本身可以不动, 但无损那条路是"流复制拼接", 没法只重编一段的音轨,
    # 所以整条转走重编码路径 —— 和"改了速度"同一档。
    loud = [c for c in ordered
            if abs(float(getattr(c, "volume", 1.0) or 1.0) - 1.0) > 1e-6]
    if loud:
        plan.reasons.append(Reason(
            FATAL, "volume",
            title="有片段改了音量",
            subject=_names(loud),
            items=[("音量：", "、".join(
                "%d%%" % round(float(getattr(c, "volume", 1.0)) * 100)
                for c in loud[:3]))],
            effect="调响度必须重算这一段的声音，整条导出转重新编码",
            clips=loud, fix="reset_volume", fix_text="恢复 100%"))

    vids = [c for c in ordered if c.kind == "video"]
    # ⚠️ "自己分离出来的音频"和"外来的音频素材"要分开(v148): 前者是这段视频原本的
    # 声音(只是搬到独立一条轨上), 后者才是"配乐混进视频"。混在一起说会让人把
    # "去掉音频素材"当成修复 —— 一点下去刚分离出来的声音就没了。
    all_auds = [c for c in ordered if c.kind == "audio"]
    detached_auds = [c for c in all_auds if (getattr(c, "src_parent", "") or "")]
    auds = [c for c in all_auds if c not in detached_auds]
    if auds and vids:
        plan.reasons.append(Reason(
            FATAL, "audio_mix",
            title="音频素材和视频混在一起",
            subject=_names(auds),
            items=[("为什么：", "音频要和视频的声音混成同一条音轨")],
            effect="混音必须重新编码，拼不上",
            clips=auds, fix="drop_audio", fix_text="去掉音频素材"))
    # ⚠️ 纯音频时间线里两段音频叠在同一时刻(v158): 以前只查"画面重叠",
    #    音频互相压着没人管 —— 于是被判成"可以无损拼接", 可导出序列是把每段
    #    首尾接起来, 两段同时的那 10 秒会变成 20 秒, 导出时长凭空翻倍。
    auds_all = [c for c in ordered if c.kind == "audio"]
    if not vids:
        for a, b in zip(auds_all, auds_all[1:]):
            if b.start < a.end - _EPS:
                plan.reasons.append(Reason(
                    FATAL, "audio_overlap",
                    title="两段音频叠在同一时刻",
                    subject=_names([a, b]),
                    items=[("叠在：", f"{fmt_sec(b.start)} 这一刻，两段声音同时存在")],
                    effect="两段声音要混成一条轨，必须重新编码",
                    clips=[a, b], fix="separate", fix_text="挪开"))
                break
        # 纯音频时间线(v167): 无损那条路只会切视频流(纯音频连画面都没有),
        # 一律走转码 —— 声音重编一层, 画面垫黑的。
        # (条件是"没有任何有画面的段": 只有图片+音频时本来就因图片进转码,
        #  不凑热闹报一条"只有音频"。)
        if auds_all and not visual:
            plan.reasons.append(Reason(
                FATAL, "audio_only",
                title="只有音频素材",
                subject=_names(auds_all[:3]),
                items=[("为什么：", "要混成一条音轨，还要垫一层黑画面")],
                effect="声音重新编码，输出画面是黑的",
                clips=auds_all, fix="", fix_text=""))
    if detached_auds and vids:
        plan.reasons.append(Reason(
            FATAL, "audio_detached",
            title=f"{len(detached_auds)} 段的声音被分离到独立音轨了",
            subject=_names(detached_auds),
            items=[("声音：", "一个字节都没丢，就在下面那条轨上"),
                   ("画面：", "可以照旧无损，混音这一步要重编")],
            effect="多一条音轨就得混音，混音要重新编码（只是声音那段，画面不受影响）",
            clips=detached_auds, fix="undetach",
            fix_text="取消分离（声音放回视频）"))

    # ---- 3. 视频规格必须一致 ----
    missing = []
    sig_map: dict[tuple, list] = {}
    for c in vids:
        spec = specs.get(c.source)
        if not spec:
            missing.append(c)
            continue
        sig_map.setdefault(_signature_of(spec), []).append(c)

    # 3a. 「有的有声音、有的没有」单独说人话 —— 光说"音频编码 aac ≠ —"没人看得懂,
    #     而这个场景很常见(手机拍的有声, 有些导出件是静音的)。
    silent_src = [c for c in vids if specs.get(c.source)
                  and not _has_audio(specs.get(c.source))]
    loud_src = [c for c in vids if _has_audio(specs.get(c.source))]
    audio_mix = bool(silent_src) and bool(loud_src)
    if audio_mix:
        plan.reasons.append(Reason(
            FATAL, "audio_presence",
            title="有的视频有声音、有的没有",
            subject=_names(silent_src + loud_src),
            items=[("没声音的：", _names(silent_src)),
                   ("有声音的：", _names(loud_src))],
            effect="拼在一起各段音轨对不上，会出现某段突然没声或者直接拼不上",
            clips=silent_src + loud_src, fix="", fix_text=""))

    # 3b. 规格比对: 上一条已经把"有没有声音"讲清楚了, 这里就别再比音频那几项,
    #     否则同一件事会被列两遍(而且文案还更难懂)。
    if audio_mix:
        sig_map = {}
        for c in vids:
            spec = specs.get(c.source)
            if not spec:
                continue
            sig_map.setdefault(_video_signature_of(spec), []).append(c)
    if len(sig_map) > 1:
        groups = list(sig_map.values())
        base = groups[0]
        other = next((g for g in groups[1:] if g), base)
        diffs = spec_diff(specs.get(base[0].source, {}),
                          specs.get(other[0].source, {}))
        bad = [c for g in groups[1:] for c in g]
        plan.reasons.append(Reason(
            FATAL, "spec",
            title="编码参数不一致",
            subject=_names([base[0], other[0]]),
            items=diffs,
            effect="这些参数必须完全一样才能直接拼，不一样就会掉帧、花屏",
            clips=bad, fix="", fix_text=""))
    if missing and vids:
        plan.reasons.append(Reason(
            WARN, "no_spec",
            title=f"还有 {len(missing)} 段没读完参数",
            subject=_names(missing),
            effect="读完之后才能确定到底能不能无损，现在只是暂定",
            clips=missing))

    # ---- 4. 能无损但有副作用 ----
    # ⚠️ 只报**真·静音**的(v148): 声音被"分离音频"搬走的那些不算 —— 声音一个字节
    # 都没丢(就在下面那条轨上), 报"被静音了"还会把"取消静音"当修复卖给人家,
    # 一点下去原视频和音频轨同时出声, 变成两份。
    muted = [c for c in ordered if c.muted]
    if muted:
        plan.reasons.append(Reason(
            WARN, "muted",
            title=f"{len(muted)} 段被静音了",
            subject=_names(muted),
            items=[("画面：", "照旧原封不动，不受影响")],
            effect="那几秒的声音要重新生成一段静音，只有音频是重新编的",
            clips=muted, fix="demute", fix_text="取消静音"))

    gap = 0.0
    prev_end = None
    for c in ordered:
        if prev_end is not None and c.start > prev_end + _EPS:
            gap += c.start - prev_end
        prev_end = max(prev_end or 0.0, c.end)
    if gap > _EPS:
        plan.reasons.append(Reason(
            WARN, "gap",
            title="中间有空档",
            items=[("空档：", f"一共 {gap:.1f} 秒")],
            effect="无损拼接会把空档去掉，后面的片段整体往前移",
            fix="compact", fix_text="合拢"))
    plan.gap = gap

    # ---- 5. 生成导出序列(入点贴关键帧) ----
    shift_notes = []
    no_kf: list = []
    t = 0.0
    for c in ordered:
        # ⚠️ 声音被分离走的段同样"不带自己的音轨"(否则和下面那条音频轨重复出声),
        # 但它**不是**静音 —— 静音那一栏只统计真静音, 别混。
        seg = Segment(clip=c, kind=c.kind, start=c.start, duration=c.duration,
                      muted=bool(c.muted or getattr(c, "detached", False)),
                      speed=c.speed or 1.0,
                      volume=max(0.0, float(getattr(c, "volume", 1.0) or 1.0)))
        if c.kind == "image":
            seg.src_in, seg.src_out = 0.0, 0.0
        elif c.kind == "audio":
            seg.src_in, seg.src_out = c.src_in, c.src_out
        else:
            seg.src_in, seg.src_out = c.src_in, c.src_out
            kf = _snap_in(c)
            if kf is not None and abs(kf - c.src_in) > _EPS:
                seg.in_shift = kf - c.src_in
                seg.src_in = kf
                seg.duration = max(0.0, seg.src_out - seg.src_in)
                if abs(seg.in_shift) > _SHIFT_EPS:
                    shift_notes.append(
                        f"{c.name} 差 {abs(seg.in_shift):.2f} 秒"
                        f"（{'少' if seg.in_shift > 0 else '多'}保留一点）")
            elif c.keyframes is None or not c.keyframes:
                no_kf.append(c)
        plan.segments.append(seg)
        t += seg.duration
    plan.total = t

    if no_kf:
        plan.reasons.append(Reason(
            WARN, "no_kf",
            title="没读到关键帧位置",
            subject=_names(no_kf),
            items=[("缺的是：", "能无损下刀的位置（关键帧）")],
            effect="这一段可能拼不上：会出现掉帧、花屏，或者导出直接失败",
            clips=no_kf))

    if shift_notes:
        plan.reasons.append(Reason(
            INFO, "kf_shift",
            title="切点会自动贴到最近的关键帧",
            items=[("偏差：", "；".join(shift_notes[:3])
                       + ("…" if len(shift_notes) > 3 else ""))],
            effect="画面不重新编码，代价是入点差一点点"))

    # 入点偏移太大, 得让人知道画面少/多了一截
    big = [s for s in plan.segments if abs(s.in_shift) > _KF_MAX_SHIFT]
    if big:
        plan.reasons.append(Reason(
            WARN, "kf_far",
            title="关键帧太稀，切点差得多",
            subject=_names([s.clip for s in big]),
            items=[("附近：", f"{_KF_MAX_SHIFT:g} 秒内没有关键帧，"
                          f"入点只能贴到 {abs(big[0].in_shift):.1f} 秒外")],
            effect="画面会明显多一截或少一截",
            clips=[s.clip for s in big]))

    plan.ok = not plan.fatal_reasons
    plan.mode = "lossless" if plan.ok else "reencode"
    if not plan.ok and plan.segments:
        # 要转码时按时间轴布局合成: 空隙和重叠都保留, 总长看最晚结束的那段
        plan.total = max(s.start + s.duration for s in plan.segments)
    return plan


def _names(clips, limit: int = 2) -> str:
    """把片段名拼成「视频A、视频B」; 超过 limit 个就写「等 N 个」。"""
    seen: list[str] = []
    for c in clips or []:
        nm = getattr(c, "name", "") or os.path.basename(getattr(c, "source", "") or "")
        nm = os.path.splitext(nm)[0]        # 去掉 .mp4 之类, 读起来更像"视频A"
        if nm and nm not in seen:
            seen.append(nm)
    if not seen:
        return ""
    if len(seen) <= limit:
        return "、".join(seen)
    return "、".join(seen[:limit]) + f" 等 {len(seen)} 个"


def _signature_of(spec: dict) -> tuple:
    from ..merge import grouping
    return grouping.signature(spec or {})


# 「有没有音轨」会连带改变这几项: 音频四件套 + 流数(有音轨就多一条流)。
# 当"有的有声音有的没有"已经单独讲过时, 这几项都要从画面比对里剔除,
# 否则同一件事会被列两遍(而且"流结构 2 ≠ 1"这种文案更难懂)。
_AUDIO_KEYS = ("audio_codec", "sample_fmt", "sample_rate", "channels",
               "nb_streams")


def _has_audio(spec: dict | None) -> bool:
    return bool((spec or {}).get("audio_codec"))


def _video_signature_of(spec: dict) -> tuple:
    """只比画面那几项的签名(「有没有声音」已经单独讲过时用这个)。"""
    from ..merge import grouping
    keys = [k for k, _cn, lv in grouping.FIELD_LEVELS
            if lv == "fatal" and k not in _AUDIO_KEYS]
    return tuple(grouping._sig_value(k, spec or {}) for k in keys)


def _snap_in(clip: Clip):
    """入点该贴到哪个关键帧(源时间轴); 没有关键帧信息返回 None。

    取**最近**的关键帧(前后都行): 偏差最小, 用户感知的画面增减最少。
    """
    ks = list(clip.keyframes or [])
    if not ks:
        return None
    return min(ks, key=lambda k: abs(k - clip.src_in))


# ---------------------------------------------------------------- 一键修复
def apply_fix(timeline: Timeline, code: str) -> bool:
    """按 Reason.fix 执行一键修复; 返回是否真的改了东西。"""
    clips = list(getattr(timeline, "clips", []) or [])
    if not clips:
        return False
    if code == "compact":
        # 跨轨也算: 按时间排序后全部排到同一条轨上首尾相接
        ordered = sorted(clips, key=lambda c: (c.start, c.track))
        timeline.track_count = max(1, timeline.track_count)
        t = 0.0
        for c in ordered:
            c.track = 0
            c.start = t
            t += c.duration
        timeline.playhead = min(timeline.playhead, max(0.0, t - _EPS))
        return True
    if code == "demute":
        hit = [c for c in clips if c.muted]
        for c in hit:
            c.muted = False
        return bool(hit)
    if code == "reset_speed":
        hit = [c for c in clips if abs((c.speed or 1.0) - 1.0) > 1e-6]
        for c in hit:
            c.speed = 1.0
        return bool(hit)
    if code == "reset_volume":
        hit = [c for c in clips
               if abs(float(getattr(c, "volume", 1.0) or 1.0) - 1.0) > 1e-6]
        for c in hit:
            c.volume = 1.0
        return bool(hit)
    if code == "undetach":
        # 把分离出来的音频片段删掉(走 timeline.remove —— 它会自动把原视频的
        # detached 还原, 这里别再自己置一次, 免得两处逻辑打架)。
        hit = [c for c in clips
               if c.kind == "audio" and (getattr(c, "src_parent", "") or "")]
        for c in hit:
            if c in timeline.clips:
                timeline.remove(c)
        for c in timeline.clips:
            if getattr(c, "detached", False):
                c.detached = False
        return bool(hit)
    if code in ("drop_image", "drop_audio"):
        kind = "image" if code == "drop_image" else "audio"
        # ⚠️ 只删"外来的"音频素材: 自己分离出来的声音不能当成垃圾丢掉(v148)
        hit = [c for c in clips if c.kind == kind
               and (kind != "audio" or not (getattr(c, "src_parent", "") or ""))]
        for c in hit:
            if c in timeline.clips:
                timeline.clips.remove(c)
        return bool(hit)
    if code == "separate":
        # 重叠可能是**跨轨**的(同轨本来就叠不上), 所以不能只问 free_slot(它只看同轨):
        # 按时间顺序往后推 —— 每一段的起点不早于前一段的终点, 推完再在同轨里避让。
        ordered = sorted(clips, key=lambda c: (c.start, c.track))
        prev_end = 0.0
        changed = False
        for c in ordered:
            want = max(c.start, prev_end)
            got = timeline.free_slot(c.track, want, c.duration, ignore=c)
            if abs(got - c.start) > _EPS:
                c.start = got
                changed = True
            prev_end = got + c.duration
        return changed
    return False


def fmt_sec(t: float) -> str:
    t = max(0.0, float(t))
    m = int(t // 60)
    s = t - m * 60
    return f"{m}:{s:04.1f}"
