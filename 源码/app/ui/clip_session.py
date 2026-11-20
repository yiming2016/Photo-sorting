# -*- coding: utf-8 -*-
"""剪辑会话（v199）：一个会话 = 一套完整的剪辑现场（时间线 + 素材框 + 撤销栈 +
播放状态 + 独立窗口）。

v128 起剪辑是独立窗口，但全程只有**一份**现场（主窗口上一堆单例字段：
clip_editor / edit_timeline / _undo_stack / _media_* / 各 worker …）。v199 要支持
"剪辑1 / 剪辑2"多个窗口并存、各自独立，于是把这一整套现场收进一个对象。

为了让现有 300+ 处 `self.clip_editor` / `self.edit_timeline` 引用零改动，
MainWindow 把这些字段做成 property，**按当前激活会话（_active_session）读写**；
切换会话 = 换一个指针，所有现场自动跟着切。这里只负责"存现场 / 建现场 / 销毁现场"。
"""
from __future__ import annotations


class ClipSession:
    """一套剪辑现场。字段与 MainWindow 上原来的单例字段一一对应。"""

    def __init__(self, num: int):
        self.num = num                      # 窗口固定编号：剪辑1 / 剪辑2 …
        self.window = None                  # ClipWindow（独立顶层窗口）
        self.title = f"剪辑{num}"

        # ---- 核心对象（建现场时赋值）----
        self.clip_editor = None             # ClipEditor
        self.edit_timeline = None           # Timeline
        self.clip_panel = None              # editor.panel

        # ---- 素材集 ----
        self._edit_built = False
        self._edit_entered = False
        self._edit_extra: set = set()
        self._media_hidden: set = set()
        self._edit_allowed = None
        self._project_sources: set = set()
        self._clip_project_path = ""

        # ---- 探测 / 抽帧 ----
        self._clip_probe_worker = None
        self._probe_tried: set = set()
        self._kf_tried: set = set()
        self._kf_cache: dict = {}
        self._film_worker = None

        # ---- 撤销 ----
        self._undo_stack: list = []
        self._undo_index: int = -1

        # ---- 无损 / 导出 ----
        self._loss_plan = None
        self._export_out_dir = ""
        self._export_worker = None
        self._export_out = ""

        # ---- 播放 / 预览 ----
        self._audio_worker = None
        self._player = None
        self._playing_clip = None
        self._media_preview = None
        self._media_playing = False
        self._media_pos = 0.0
        self._media_dur = 0.0
        self._media_clock = 0.0
        self._media_seek_paused = False
        self._seek_playing = False
        self._scrub_on = True
        self._edit_area = None

        # ---- 快捷键 / 事件过滤 / 弹窗 ----
        self._edit_shortcuts: list = []
        self._edit_actions: dict = {}
        self._edit_wheel_actions: dict = {}
        self._area_taps: list = []
        self._clip_prop_dialogs: list = []

    # 兼容主窗口里大量 self._clip_window 引用：路由到本会话的窗口
    @property
    def _clip_window(self):
        return self.window

    @_clip_window.setter
    def _clip_window(self, value):
        self.window = value

    # 现场是否有实际内容（关窗二次确认用）：时间线上有片段、或撤销栈有改动、或加了额外素材
    def has_content(self) -> bool:
        try:
            if self.edit_timeline is not None and self.edit_timeline.clips:
                return True
        except Exception:
            pass
        if self._edit_extra:
            return True
        if len(self._undo_stack) > 1:
            return True
        if self._clip_project_path:
            return True
        return False

    def stop_workers(self):
        """销毁会话前把绑定自己的后台线程引用清掉（线程本体由主窗口统一停）。"""
        for name in ("_clip_probe_worker", "_film_worker", "_export_worker",
                     "_audio_worker", "_player", "_tool_worker", "_wave_worker"):
            try:
                setattr(self, name, None)
            except Exception:
                pass


# 需要"按当前激活会话路由"的字段。MainWindow 上这些名字做成 property，
# 读写都落到 _active_session。清单必须包含 __init__ 里所有剪辑态单例字段，
# 漏一个就会在多窗口间串台。
SESSION_FIELDS = (
    "clip_editor", "edit_timeline", "clip_panel", "_clip_window",
    "_edit_built", "_edit_entered",
    "_edit_extra", "_media_hidden", "_edit_allowed",
    "_project_sources", "_clip_project_path",
    "_clip_probe_worker", "_probe_tried", "_kf_tried", "_kf_cache", "_film_worker",
    "_undo_stack", "_undo_index",
    "_loss_plan", "_export_out_dir", "_export_worker", "_export_out",
    "_audio_worker", "_player", "_playing_clip",
    "_media_preview", "_media_playing", "_media_pos", "_media_dur",
    "_media_clock", "_media_seek_paused", "_seek_playing", "_scrub_on", "_edit_area",
    "_edit_shortcuts", "_edit_actions", "_edit_wheel_actions",
    "_area_taps", "_clip_prop_dialogs",
)


def session_property(name):
    """生成一个把字段读写转发到当前激活会话的 property。"""
    def _get(self):
        sess = self._active_session
        if sess is None:
            return getattr(self._boot_session, name)
        return getattr(sess, name)

    def _set(self, value):
        sess = self._active_session
        if sess is None:
            setattr(self._boot_session, name, value)
        else:
            setattr(sess, name, value)

    return property(_get, _set)
