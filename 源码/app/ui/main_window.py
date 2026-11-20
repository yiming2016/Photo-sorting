# -*- coding: utf-8 -*-
"""主窗口: 顶栏开关 / 照片网格 / 右侧信息 / 底部写入条。"""
from __future__ import annotations

import os
import json
import re
import subprocess
import sys
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal, QSize
from PySide6.QtGui import (QAction, QFont, QImage, QKeySequence, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QFileDialog, QFrame,
                               QHBoxLayout, QInputDialog, QLabel, QLayout,
                               QLineEdit,
                               QListView, QMainWindow, QMenu, QStyle, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QSizePolicy, QSplitter,
                               QStackedWidget, QTextEdit, QToolButton,
                               QVBoxLayout, QWidget)

from . import theme as T
from . import cursor as cursor_hand
from .. import __build__
from ..resources import app_resource
from .picker_dialog import PickerDialog, _CenterComboArrow
from .dialogs import (ChangePreviewDialog, IntervalDialog, MoveToMergeDialog,
                      ProgressDialog, RollbackDialog, _CenterAlignDelegate,
                      confirm_destructive, confirm_yes_no, confirm_delete_files)
from .grouplist import GroupView
from .inspector import Inspector
from .merge_panel import MergePanel
from .normalize_dialog import NormalizeDialog
from .norm_page import NormParamSlot, NormWorkspace
from .clip_editor import ClipEditor, FRAME_STEP
from .clip_session import ClipSession, SESSION_FIELDS, session_property
from .timeline import fmt_time
from .edit_shortcuts import (ShortcutHelpDialog, WHEEL_PAIRS, as_slots,
                             is_wheel_seq, load_bindings, match_wheel,
                             parse_seq)
from .clip_export import LosslessDialog
from .player import FramePlayer, out_size, PLAY_FPS, PLAY_MAX_W, PREVIEW_MAX_W
from .widgets import (BoldPartMenuAction, DangerMenuAction, ElidedLabel,
                      SwitchButton, set_accent)
from ..core import fields as F, timeplan, writer
from ..core import lossless as LS
from ..core import media_tools as MT
from ..core.clips import (Clip, Timeline, kind_of as clip_kind_of,
                          TRACK_AUDIO, TRACK_MAIN, IMAGE_DEFAULT_SEC)
from ..core.filmstrip import FilmstripWorker
from ..core.exiftool import ExifTool, find_exiftool
from ..merge import grouping as mgrouping
from ..merge import naming as mnaming
from ..merge import pathsafe
from ..merge import settings as merge_settings
from ..core.photo import (CollapsedPack, HeaderRow, PackRow, PhotoModel, PhotoRow,
                          STATUS_DONE, STATUS_NORMAL,
                          STATUS_PENDING, THUMB_FAIL, THUMB_NONE, THUMB_OK)
from ..core.scanner import Scanner, collect_files
from ..core.thumbs import ThumbWorker


def send_to_recycle_bin(paths):
    """把文件移入 Windows 回收站（可在回收站中恢复），而非永久删除。

    返回成功移入回收站的路径列表；未能移入的（被占用 / 过大等）不会出现在返回里。
    使用 ctypes 调用 SHFileOperationW（FOF_ALLOWUNDO），不引入第三方依赖。
    """
    if not paths:
        return []
    try:
        import ctypes
    except Exception:
        return []
    try:
        shell32 = ctypes.windll.shell32
    except Exception:
        return []

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_uint),
            ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    FO_DELETE = 0x0003
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_NOERRORUI = 0x0400
    FOF_SILENT = 0x0004

    removed = []
    for p in paths:
        if not os.path.exists(p):
            continue
        struc = _SHFILEOPSTRUCTW()
        struc.hwnd = None
        struc.wFunc = FO_DELETE
        # SHFileOperationW 要绝对路径(相对路径会按 shell 的当前目录解释, 常出错)
        struc.pFrom = os.path.abspath(p) + "\0\0"
        struc.pTo = None
        struc.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
        try:
            res = shell32.SHFileOperationW(ctypes.byref(struc))
        except Exception:
            res = 1
        if res == 0 and not os.path.exists(p):
            removed.append(p)
    return removed


SETTINGS_LAST_FOLDER = "lastFolder"


# ---------------------------------------------------------------- 小工具
def _unique_subdir(parent_dir: str, base: str) -> str:
    """在 parent_dir 下取一个不重名的子文件夹: 合并_001 / 合并_002 …"""
    for i in range(1, 1000):
        p = os.path.join(parent_dir, f"{base}_{i:03d}")
        if not os.path.exists(p):
            return p
    return os.path.join(parent_dir, f"{base}_{os.getpid()}")


def _unique_path(path: str) -> str:
    """同名文件已存在时加 (2)(3)…，绝不覆盖已有文件。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(2, 1000):
        cand = f"{stem} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
    return path


def _fmt_bytes(n) -> str:
    """体积显示(不四舍五入成 0)。"""
    try:
        n = max(0.0, float(n))
    except (TypeError, ValueError):
        n = 0.0
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f} MB"
    return f"{n / 1024:.0f} KB"


_LEADING_INDEX = re.compile(r"^\d{1,3}[_\-\s.、]+(?!\d)")


def _strip_leading_index(stem: str) -> str:
    """去掉原名开头的序号(01_ / 1- / 02 )，避免移动后变成 01_01_xxx。

    只在"序号后不是数字"时才剥离 —— 否则 2019-08-15_婚礼、08-15_开场
    这类日期开头的名字会被误伤(2024 是 4 位, 已由 \\d{1,3} 排除;
    08-15 的后半段以数字开头, 由 (?!\\d) 排除)。
    剥完为空时返回空串, 由调用方退化成纯序号名。
    """
    return _LEADING_INDEX.sub("", stem).strip()

# 主界面六档显示方式: (名称, 模式, 网格宽, 网格高)
# 前四档为图标网格, 后两档为横向列表(单行/双行)。Ctrl+滚轮在六档间步进。
# 主网格"显示"下拉: 最小档就是"小图标"(不含列表/详细信息), 由小到大排列。
MAIN_VIEW_MODES = [
    ("小图标", "sm", 88, 96),
    ("中图标", "md", 104, 116),
    ("大图标", "lg", 132, 150),
    ("超大图标", "xl", 168, 202),
]

# 安装 Qt 中文翻译: 让非原生选择框(及标准控件)内部的英文变成中文
_zh_tr = None


def _ensure_zh_translator():
    global _zh_tr
    import os, sys
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTranslator, QLibraryInfo
    if _zh_tr is not None:
        return
    app = QApplication.instance()
    if app is None:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    bases = []
    if getattr(sys, "frozen", False):
        mp = getattr(sys, "_MEIPASS", "")
        if mp:
            bases.append(mp)
        exe_dir = os.path.dirname(sys.executable)
        bases.append(exe_dir)
        bases.append(os.path.join(exe_dir, "_internal"))
    bases.append(os.path.join(here, "..", ".."))   # 开发模式: 项目 translations/
    rel_dirs = [
        "translations",
        os.path.join("_internal", "translations"),
        os.path.join("_internal", "PySide6", "translations"),
    ]
    candidates = []
    for b in bases:
        for rd in rel_dirs:
            candidates.append(os.path.join(b, rd, "qtbase_zh_CN.qm"))
    candidates.append(os.path.join(QLibraryInfo.path(QLibraryInfo.TranslationsPath), "qtbase_zh_CN.qm"))
    tr = QTranslator(app)  # 父对象持有, 避免被 GC 后译文失效
    for c in candidates:
        if c and os.path.exists(c) and tr.load(c):
            app.installTranslator(tr)
            _zh_tr = tr
            return


class _AreaTap(QObject):
    """只干一件事: 记下"用户最后在哪一块上按过鼠标"(给 Ctrl+A 分范围用)。

    不拦事件(一律 return False), 只是顺手看一眼。
    """

    def __init__(self, area: str, setter):
        super().__init__()
        self._area = area
        self._set = setter

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.MouseButtonPress,
                            QEvent.MouseButtonDblClick,
                            QEvent.Wheel, QEvent.KeyPress, QEvent.FocusIn):
            self._set(self._area)
        return False


class _TopBar(QWidget):
    """顶栏容器: 最小宽封顶。

    v211: 形态切换的那一帧里, 上一形态的控件还没隐藏、下一形态的已经显示
    (比如统一页的参数条 + 合并页的「无损/独立」板块同时可见), 顶栏最小宽会
    瞬间涨到 1400+, Qt 当场把主窗口撑宽 —— 而且切回去也不会自动缩回。
    这里给最小宽封个顶: 挤的时候让顶栏内部自己裁, 绝不反过来撑窗口。
    """

    MIN_W = 1040

    def minimumSizeHint(self) -> QSize:
        s = super().minimumSizeHint()
        return QSize(min(s.width(), self.MIN_W), s.height())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # v216: 「能点的地方一律小手」。必须在建控件之前装好 —— 之后每个控件的
        # ChildAdded 都会被过滤器看到并补上光标(main() 里也会装一次, 幂等)。
        cursor_hand.install()
        # v199 剪辑多会话: 先建引导会话, 让 __init__ 里那批剪辑态字段赋值
        # (clip_editor / edit_timeline / _undo_stack … 经 property 路由)有落点。
        # 真正打开第一个剪辑窗口时, 这个会话会被提升成「剪辑1」。
        self._boot_session = ClipSession(1)
        self._active_session: ClipSession | None = None
        self._clip_sessions: list[ClipSession] = []
        self._clip_seq = 0                 # 固定编号: 关掉不重排, 全关才归零
        _ensure_zh_translator()
        # 参数分档(⚙️ 设置): 启动时恢复用户上次的选择与顺序, signature/对比/属性全跟着走
        mgrouping.apply_config(merge_settings.load_field_levels(),
                               merge_settings.load_field_order())
        self.setWindowTitle(self._win_title())
        # v208: 顶栏参数条 4 个下拉加宽(文字完整显示), 默认窗口横向加宽以容纳。
        # v214: 统一页顶栏又多了 编码/分辨率/帧率 三个层级开关 + 显示方式下拉 → 再放宽一档,
        #       否则默认尺寸下顶栏会被挤到控件互相叠住。
        # v228: 参数格改为可压缩后, 统一态顶栏自然宽 = 参数条 630 + 其余 ≈ 1387;
        #       窗口 1800(左栏 1436)放得下 —— 默认尺寸参数文字完整、也不重叠。
        self.resize(1800, 900)
        self.setAcceptDrops(True)

        self.model = PhotoModel(self)
        self.path_index: dict[str, object] = {}
        self.sources: list[dict] = []   # 累加模式下的来源记录: {"type":"folder"/"files", ...}
        # 会话文件: 自动保存最近一次来源集合, 供「恢复上次来源」一键重载
        if getattr(sys, "frozen", False):
            # 打包后 exe 位于 软件\照片排序台\照片排序台.exe, 会话文件放在 软件\ 同级持久目录
            _root = os.path.dirname(os.path.dirname(sys.executable))
        else:
            # 开发模式: 源码\app\ui\main_window.py -> 项目根(需向上 4 级)
            _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        self.session_path = os.path.join(_root, ".photosort_session.photolist")
        self.worker = None
        self.progress_dialog = None
        self._pending_photos_cache: list = []

        # ---- 合并模式(主界面的第二种形态) ----
        self.merge_mode = False
        # v206: 统一格式独立成第三种大形态(排序 → 合并 → 统一), 与 merge_mode 互斥
        self.norm_mode = False
        # v220: 网格勾选的**权威集合**(按路径)。合并/统一两态下"勾上"是业务状态,
        # 而 Qt 每次 beginResetModel(折叠/展开、重新分组、探测回填都算)都会把选中
        # 清空且不发 selectionChanged —— 只靠 QItemSelection 存不住: 收起组里的勾选
        # 会悄悄丢, 组标题又各自按"当前选中"重算, 于是表现为"勾一个、别的跟着变"。
        # v231: 每次变更都同步一份镜像到 model.grid_checked, 供 delegate 画
        # 多文件组块的逐张高亮(条目级 Qt 选中表达不了"块内只勾一部分")。
        self._grid_sel: set[str] = set()
        self.merge_specs: dict[str, dict] = {}
        self.merge_errors: dict[str, str] = {}
        # 单独记时长(秒): merge_specs 是"全规格", 拿来判无损的, 不能拿假数据凑。
        # 用户手点的操作(＋上轨 / 点素材看预览)同步读一次时长就放这儿, 免得
        # 片段按 0 秒建出来(时间线上看着就像"只加了一帧")。
        self._dur_cache: dict[str, float] = {}
        self._dur_miss: set[str] = set()     # 读不出来的(坏文件): 别反复卡界面
        self._spec_index: dict[str, dict] = {}   # 统一写法 → 全规格(懒建)
        self._spec_index_n = -1
        self.merge_ffmpeg: str | None = None
        self.merge_ffprobe: str | None = None
        self._probe_worker = None
        self._merge_worker = None
        self._prop_dialogs: list = []   # 已打开的非模态「属性」窗口(防被回收 + 错开位置)
        self._clip_prop_dialogs: list = []  # 剪辑态的属性窗口(v174 非模态, 同上)
        self._cmp_dialogs: list = []    # 已打开的非模态「对比」窗口(同上)
        self._norm_dialog = None        # 非模态「统一格式」窗口(v203: 打开后仍可操作主界面)
        self._norm_params_dialog = None  # v261: 非模态「统一参数配置」窗口(同上, 防被回收)
        self._norm_params_token = None   # v261: 销毁回调只清掉"自己那一任"的引用
        # v192: 对比窗口按打开顺序给固定编号(标题「对比1」)。关掉其中一两个
        # **不重排号**; 只有全部关掉才归零, 下次重新从「对比1」开始。
        self._cmp_seq = 0
        self._merge_out = ""          # 最近一次成功合并的成品路径
        # FFmpeg 可用性: None=还没查过 / True=通过 / False=不可用。
        # 与"有没有视频"无关 —— 空列表也要如实反映。
        self.ffmpeg_ok: bool | None = None
        self.ffmpeg_pair: tuple | None = None
        self._ff_check_worker = None
        self._ff_check_again = False

        # ---- 剪辑模式(合并形态下的第三种形态, 占满整窗) ----
        self.edit_mode = False
        self.edit_timeline = Timeline(3)  # 默认 3 条通用轨, 可增减
        self._edit_built = False          # 时间线是否已建好(切走再回来保留现场)
        self._edit_entered = False        # 是否进过剪辑(第二次起不清撤销历史)
        self._edit_extra: set[str] = set()   # 送进素材框、但不在主列表里的素材
        self._media_hidden: set[str] = set()  # 用户手动从素材框移除的(不动磁盘)
        # 进剪辑时「合并」页勾选的那批文件(统一写法)。素材框只列这些 ——
        # 没勾的连素材框都不出现(v140)。None = 没这层限制(不在合并模式)。
        self._edit_allowed: set[str] | None = None
        # v183: 从「剪辑方案」载入的源文件(统一写法)。即使合并页后来改了勾选、
        # 或者关窗再开, 素材框也照样认这些文件 —— 否则方案载入了却看不见素材。
        self._project_sources: set[str] = set()
        self._clip_project_path = ""      # 上次存/开的方案路径(下次存盘默认落在那儿)
        self._clip_probe_worker = None    # 后台读素材时长
        self._probe_tried: set = set()    # 已经探过时长的路径(探不出来也别反复起线程)
        self._kf_tried: set = set()       # 已经探过关键帧的路径(v130: 关键帧快捷键要用)
        self._kf_cache: dict = {}         # v194: 路径 → 已探到的关键帧(后加片段直接取)
        self._film_worker = None          # 后台抽帧(时间线缩略图条)
        self._export_out_dir = ""         # 本次导出的目标路径(无损失败要改转码时用)
        self._undo_stack: list = []       # 时间线整表快照(撤销/重做)
        self._undo_index: int = -1
        self._loss_plan = None            # 最近一次"能不能无损"的判定结果
        self._export_worker = None        # 导出线程(剪辑页)
        self._export_out = ""             # 上次导出的文件
        self._merge_videos: list = []     # 进合并模式时的视频列表(剪辑态返回分组视图要用)
        self._shortcut_help = None        # 快捷键一览(非模态, 只留一份)
        self._edit_shortcuts: list = []   # 剪辑态键位(改键后整批重建)
        self._edit_bindings: dict = {}    # action_id → 键序列
        # ---- 播放(ffmpeg 抽帧, 不引 QtMultimedia: 打包会漏后端) ----
        self._player = None               # FramePlayer
        self._play_timer = QTimer(self)   # 图片/音频片段: 只用来"看是不是到段尾了"
        self._play_timer.setInterval(33)
        self._play_timer.timeout.connect(self._on_play_tick)
        # ⚠️ 播放头**不能**跟着解码帧走(v156): 预览是 15fps(PLAY_FPS), 跟着帧推进
        #    就是每 66ms 才动一次、一次跳 4 个像素 —— 开着「跟随移动」看轨道,
        #    缩略图和刻度数字都是一跳一跳的。改成自己按**墙上时间** 60Hz 推进,
        #    画面帧只管画, 轨道就匀速滚了(真正的画面帧率不变)。
        self._smooth_timer = QTimer(self)
        self._smooth_timer.setTimerType(Qt.PreciseTimer)
        self._smooth_timer.setInterval(16)      # ≈60Hz
        self._smooth_timer.timeout.connect(self._on_smooth_tick)
        self._clock_t = 0.0               # 上一次推进的 perf_counter
        self._audio_worker = None         # 分离音频的后台线程(要留住引用)
        # ---- 工具箱(v122): 改帧率 / 削减 / 检测 / 波形 / 修时长 ----
        self._tool_worker = None          # 工具线程(同一时间只跑一个)
        self._wave_worker = None          # 读波形的线程
        self._detect: dict = {}           # 源路径 → 检测结果(完整)
        self._detect_marks: dict = {}     # 源路径 → 画在时间线上的标记
        self._playing_clip = None         # 正在播的片段(到点了切下一个)
        self._play_from = 0.0             # 这一段是从哪开始播的(判断有没有真播成)
        # ---- 预览跟随裁剪线: 一条常驻的抓帧线程(只换任务, 不反复 new) ----
        self._grab_worker = None          # FrameGrabber(常驻)
        self._pending_grab = None         # (源文件, 源秒数): 最新想抓的位置
        # ---- 素材框选中的素材: 预览临时显示它, 点画面就能播 ----
        self._media_preview = None        # 当前预览的素材路径(不在时间线上)
        self._media_playing = False       # 正在播的是"素材预览"而不是时间线
        # ---- 素材预览的悬浮进度条(v127): 可拖到素材任意一秒 ----
        self._media_pos = 0.0             # 素材内播到第几秒
        self._media_dur = 0.0             # 这个素材总长
        self._media_clock = 0.0           # 上一次对齐的墙上时间(按真实耗时推进进度)
        # ---- 点右栏片段后: 中间预览正看着"这一段", 进度条按片段长度走(v141) ----
        self._clip_scrub = None           # 正在按片段预览的 Clip
        self._clip_scrub_pos = 0.0        # 这一段内播到第几秒
        # ---- 播整条轨道时: 预览下沿那条进度条改成"所有轨道的总进度"(v159) ----
        self._tl_scrub = False            # True = 进度条范围是整条时间线
        self._tl_seeking = False          # 正在拖那条整体进度条(别被自己当成"挪裁剪线")
        # ---- 「进度条」开关(v161): 中间画面下方那条要不要显示 ----
        self._scrub_on = True             # 默认开; 进剪辑时按存盘的值覆盖
        self._media_seek_paused = False   # 拖素材进度条时顺手把播放停了(松手要续播)
        self._clip_seeking = False        # 正在拖"这一段"的进度条(别被当成挪裁剪线)
        self._media_ui_timer = QTimer(self)
        self._media_ui_timer.setInterval(100)
        self._media_ui_timer.timeout.connect(self._on_media_ui_tick)
        # ---- 声音(v127): 系统里没有 QtMultimedia, 用 winmm 的 waveOut 直接放 PCM ----
        self._sound = None                # AudioPlayer(播一段声音)
        # ---- 播放中拖裁剪线: 画面先跟过去, 停稳后再从那里接着播 ----
        self._seek_playing = False        # 正在"找位置"(播放器已停, 画面用抓帧顶着)
        self._player_gen = 0              # 播放器代号: 换过代的旧播放器送来的帧不画
        self._seek_timer = QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(140)
        self._seek_timer.timeout.connect(self._on_seek_settled)
        # ---- QThread 回收站: 线程没真停下之前绝不丢引用(否则 Qt 直接 abort) ----
        self._dead_threads: list = []
        self._thread_gc = QTimer(self)
        self._thread_gc.setInterval(1000)
        self._thread_gc.timeout.connect(self._purge_threads)
        self._thread_gc.start()

        self._build_ui()
        self._connect()
        self._refresh_top_status()
        self._apply_main_view_mode()  # 应用"显示方式"(上次用过的档位, 没记录过就小图标)
        QTimer.singleShot(60, self._check_exiftool)
        # 启动即查一次(此时就是排序模式), 让合并界面一进去就有真实的绿/红
        QTimer.singleShot(200, self._check_ffmpeg_async)

    # ---------------------------------------------------------------- 界面
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        # 顶栏不再横跨整窗: 它与文件网格一起装进"左栏"(见下方 splitter),
        # 这样右侧详情栏能从窗口顶端开始, 预览图顺势向上多占一条顶栏的高度。
        self.stack = QStackedWidget()
        self.view = GroupView()
        self.view.setModel(self.model)
        # 主网格 Ctrl+滚轮缩放 → 切换六档显示方式
        self.view._on_zoom = self._zoom_main_step
        # 图标档文件名被省略号截断时, 悬浮显示完整文件名
        self.view.setMouseTracking(True)
        self.view.entered.connect(self._on_grid_entered)
        # v220: 模型重建(折叠/展开/重新分组/探测回填)会把选中清空, 这里按路径还原,
        # 否则"收一下组"就把整批勾选和被收起的素材一起弄丢了。
        # v234: modelReset 发出时 Qt **已经**清空选中且不发 selectionChanged ——
        # 合并/统一态靠 _grid_sel_paths(Python 集合)还原, 排序态的"勾选"就是 Qt
        # 选中本身, 没有集合兜底 → 折叠任意组会把整表选中清空(实测 [3,6]→[])。
        # 用 modelAboutToBeReset(此刻选中还在)留快照, reset 后按 Photo 对象还原。
        self.model.modelAboutToBeReset.connect(self._on_grid_about_to_reset)
        self.model.modelReset.connect(self._on_grid_model_reset)
        self.stack.addWidget(self.view)

        empty = QWidget()
        # v212: 空态沿用「有文件」时那张白卡的外观(#emptyPage 见 theme), 空/满不跳背景
        empty.setObjectName("emptyPage")
        empty.setAttribute(Qt.WA_StyledBackground, True)  # 纯 QWidget 才认 QSS 背景+边框
        el = QVBoxLayout(empty)
        el.setAlignment(Qt.AlignCenter)
        el.setSpacing(14)
        icon = QLabel(" DPR")
        icon.setStyleSheet(
            f"font-size: 56px; color: {T.BORDER_STRONG}; "
            f"background: {T.THUMB_BG}; border-radius: 28px; "
            f"padding: 12px 18px; margin-bottom: 8px;")
        icon.setAlignment(Qt.AlignCenter)
        el.addWidget(icon, alignment=Qt.AlignCenter)
        self.empty_title = QLabel("把照片文件夹或文件拖进来，或点上面的「添加」")
        self.empty_title.setProperty("h2", True)
        el.addWidget(self.empty_title, alignment=Qt.AlignCenter)
        self.empty_sub = QLabel("排序后按当前顺序分配时间戳，一次性写回 修改时间 / 创建时间 / 拍摄时间")
        self.empty_sub.setProperty("muted", True)
        el.addWidget(self.empty_sub, alignment=Qt.AlignCenter)
        self.stack.addWidget(empty)
        self.empty_page = empty

        self.inspector = Inspector()
        self.inspector.setMinimumWidth(320)
        self.inspector.setMaximumWidth(360)  # 详情栏最多 360px, 元素完整即可, 不无限拉伸

        # 合并模式的右栏(预览 + 输出设置 + 开始合并), 与详情栏叠在同一个位置
        self.merge_panel = MergePanel()
        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self.inspector)    # 0 = 排序模式
        self.right_stack.addWidget(self.merge_panel)  # 1 = 合并模式

        # v206: 统一格式第三形态 —— 左右两块分别塞进主区栈 / 右栏栈。
        # 这样排序/合并/统一共用同一套 splitter 骨架(只换两个栈的当前页)。
        self.norm_workspace = NormWorkspace(self)
        self.stack.addWidget(self.norm_workspace.preview)   # 2 = 统一页左主区(分组预览)
        self.right_stack.addWidget(self.norm_workspace.panel)  # 2 = 统一页右栏(参数目标)
        self.norm_workspace.startRequested.connect(self._run_normalize)
        # v209: 统一右栏「预览/打开位置/剪辑」接线
        self.norm_workspace.panel.previewRequested.connect(self._norm_open_preview)
        self.norm_workspace.panel.revealRequested.connect(self._norm_reveal)
        self.norm_workspace.panel.editRequested.connect(self.toggle_edit)
        # v242: ⚙ 参数配置弹窗
        self.norm_workspace.panel.paramsRequested.connect(self.open_norm_params)

        # 剪辑页(v128 起是**独立窗口**): 这里只建好编辑器, 真正装进窗口是
        # 第一次点「剪辑」的时候(_show_clip_window)。主界面不再切换整窗。
        self.clip_editor = ClipEditor()
        self.clip_editor.set_timeline(self.edit_timeline)
        self.clip_editor.timeline_view.clipSelected.connect(self._on_edit_clip_selected)
        self.clip_panel = self.clip_editor.panel     # 属性面板(在剪辑页右栏)
        self._clip_window = None                     # 剪辑独立窗口(懒建, 只一份)
        # v141: Ctrl+A 要知道"最后点的是哪一块"(左素材 / 右片段 / 下轨道)
        self._edit_area = None
        self._area_taps: list = []
        self._install_area_taps()

        # 右侧栏 = 详情面板 + 底部三个操作按钮(取消改动 / 自动间隔 / 保存改动)。
        # 这三个按钮属于右半部分, 所以放进右栏内部(而不是横跨整窗的底栏),
        # 这样它们和左侧网格彻底分开, 并且始终跟详情栏同宽、对齐。
        self.right_col = QWidget()
        self.right_col.setMaximumWidth(360)
        rv = QVBoxLayout(self.right_col)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        rv.addWidget(self.right_stack, 1)
        self.bottom_bar = self._build_bottombar()
        rv.addWidget(self.bottom_bar)

        # 左栏 = 顶栏 + 文件网格; 顶栏只覆盖左栏, 右栏因此能从窗口顶端开始
        self.left_side = QWidget()
        lv = QVBoxLayout(self.left_side)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        lv.addWidget(self._build_topbar())
        lv.addWidget(self.stack, 1)
        # 顶栏"显示方式"下拉 → 主网格缩略图尺寸(+ 记住这次的档位)
        self.view_mode_combo.currentIndexChanged.connect(self._on_view_mode_changed)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.left_side)
        self.splitter.addWidget(self.right_col)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([1000, 360])

        # 外层栈: 排序 / 合并 两种形态(剪辑已经独立成窗口, 不再塞进这里)
        self.window_stack = QStackedWidget()
        self.window_stack.addWidget(self.splitter)
        root.addWidget(self.window_stack, 1)

        # 最底部那条状态栏整行已按要求删掉。
        # status_label 只保留对象(永不显示), 这样历史代码里的 setText 不会报错;
        # 需要"信息提示"的地方一律走弹窗。
        self.status_label = QLabel(self)
        self.status_label.setVisible(False)

        self.thumb_worker = ThumbWorker(self)
        self.thumb_worker.thumbReady.connect(self._on_thumb_ready)
        self.thumb_worker.thumbFailed.connect(self._on_thumb_failed)
        self.thumb_worker.start()

    def _build_topbar(self) -> QWidget:
        bar = _TopBar()
        bar.setFixedHeight(54)
        bar.setObjectName("topbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(8)

        self.open_btn = QPushButton("添加")
        self.open_btn.setMinimumWidth(72)
        self.open_btn.setCursor(Qt.PointingHandCursor)
        set_accent(self.open_btn, True)
        self.open_btn.setToolTip("选择文件夹或文件，程序自动判断（也可拖拽）")
        lay.addWidget(self.open_btn)

        # 「清空」紧跟「添加」右侧: 三形态一致(v208)。
        self.clear_list_btn = QPushButton("清空")
        self.clear_list_btn.setMinimumWidth(72)
        self.clear_list_btn.setToolTip("清空当前已加载的列表（不删除电脑里的文件，Backspace；有选中时只清选中项）")
        lay.addWidget(self.clear_list_btn)
        self.vl1 = self._vline()
        lay.addWidget(self.vl1)

        # v207: 「清空」右侧 = 统一页的 4 个可调参数(编码/音频/关键帧/码率)。
        # 参数栏本体由 NormPanel 持有, 这里只是 reparent 过来显示, 信号/profile 链路不变。
        # v228: 槽换成 NormParamSlot —— 富余时各下拉精确取 fit 宽度(文字完整),
        # 顶栏挤的时候按比例整体压缩, 绝不把右侧开关/滑块叠到参数条上(以前
        # SetFixedSize 把槽 min/max 锁死, 顶栏分不到空间就出现"按钮重叠")。
        self.norm_param_slot = NormParamSlot()
        nps = QHBoxLayout(self.norm_param_slot)
        nps.setContentsMargins(0, 0, 0, 0)
        nps.setSpacing(0)
        nps.addWidget(self.norm_workspace.panel.param_host())
        self.norm_param_slot.register(self.norm_workspace.panel.param_cells())
        self.norm_param_slot.setFixedHeight(
            self.norm_workspace.panel.param_host().sizeHint().height())
        self.norm_param_slot.setVisible(False)
        lay.addWidget(self.norm_param_slot)
        self.vl_norm_param = self._vline()
        self.vl_norm_param.setVisible(False)
        lay.addWidget(self.vl_norm_param)

        # 合并模式专用「无损」板块: 圆角边框把它圈起来, 左边是标题「无损」, 右边三个按钮。
        # (以前是三个裸按钮, 看不出跟无损有关; 圈起来后一眼知道这组都是无损相关的操作)
        self.lossless_box = QFrame()
        self.lossless_box.setObjectName("losslessBox")
        self.lossless_box.setStyleSheet(
            f"#losslessBox {{ border: 1px solid {T.BORDER_STRONG}; border-radius: 8px;"
            f" background: {T.SURFACE}; }}")
        box = QHBoxLayout(self.lossless_box)
        box.setContentsMargins(10, 4, 8, 4)
        box.setSpacing(6)
        box_title = QLabel("无损")
        box_title.setStyleSheet(
            f"color: {T.TEXT_SUB}; font-size: 12px; font-weight: 600;"
            f" border: none; background: transparent;")
        box.addWidget(box_title)

        # 一键选中/取消所有「可无损合并」的组(按规格自动判断, 不用一个个点)
        self.sel_spec_btn = QPushButton("全选")
        self.sel_spec_btn.setToolTip("点一下选中所有可无损合并的组，再点一下取消全选")
        self.sel_spec_btn.setCursor(Qt.PointingHandCursor)
        box.addWidget(self.sel_spec_btn)

        # 按组合并: 每个组各合并成一个文件, 组与组互不串
        self.group_merge_btn = QPushButton("按组合并")
        self.group_merge_btn.setToolTip("把每个可无损合并的组各合并成一个文件（组内可改名称/无损或转码）")
        self.group_merge_btn.setCursor(Qt.PointingHandCursor)
        box.addWidget(self.group_merge_btn)

        # 新建文件夹: 每组各自建一个子文件夹装进去
        self.group_folder_btn = QPushButton("新建文件夹")
        self.group_folder_btn.setToolTip("把每个组分别移入各自的子文件夹（可选按 01_原名 重命名）")
        self.group_folder_btn.setCursor(Qt.PointingHandCursor)
        box.addWidget(self.group_folder_btn)

        self.lossless_box.setVisible(False)
        # 垂直居中: 不让它被拉满整个 54px 顶栏(否则里面的按钮比「添加」高出一截)
        lay.addWidget(self.lossless_box, 0, Qt.AlignVCenter)

        # 合并模式专用「独立」板块(v226: 原"单独"): 与「无损」并排, 圈起来表示
        # 这组都是"独立分组"的操作。「全选」= 选中所有独立分组的视频(再点一次取消);
        # 「移动」= 把它们挪进一个文件夹(弹窗与右键「移动到…」同一个)。
        self.solo_box = QFrame()
        self.solo_box.setObjectName("soloBox")
        self.solo_box.setStyleSheet(
            f"#soloBox {{ border: 1px solid {T.BORDER_STRONG}; border-radius: 8px;"
            f" background: {T.SURFACE}; }}")
        sbox = QHBoxLayout(self.solo_box)
        sbox.setContentsMargins(10, 4, 8, 4)
        sbox.setSpacing(6)
        sbox_title = QLabel("独立")
        sbox_title.setStyleSheet(
            f"color: {T.TEXT_SUB}; font-size: 12px; font-weight: 600;"
            f" border: none; background: transparent;")
        sbox.addWidget(sbox_title)

        # 一键选中/取消所有「独立」分组的视频(规格与谁都合不上的那些)
        self.solo_all_btn = QPushButton("全选")
        self.solo_all_btn.setToolTip("点一下选中所有独立分组的视频，再点一下取消全选")
        self.solo_all_btn.setCursor(Qt.PointingHandCursor)
        sbox.addWidget(self.solo_all_btn)

        # 移动: 把选中的视频挪进目标文件夹(可选按顺序重命名)
        self.solo_move_btn = QPushButton("移动")
        self.solo_move_btn.setToolTip("把选中的视频移动到一个文件夹（可选按 01_原名 重命名）")
        self.solo_move_btn.setCursor(Qt.PointingHandCursor)
        sbox.addWidget(self.solo_move_btn)

        self.solo_box.setVisible(False)
        lay.addWidget(self.solo_box, 0, Qt.AlignVCenter)

        # 「剪辑」按钮挪到了合并右栏底部(v132): 它属于"合并完接着去剪"的动线,
        # 放在「开始合并」原位更顺手, 顶栏不再占一个位置。
        # 过滤文件名 + 排序: 放在「刷新」左边(靠近添加, 与右侧视图/分组控件分开)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("过滤文件名…")
        self.search_edit.setMinimumWidth(110)
        self.search_edit.setMaximumWidth(150)
        self.search_edit.setClearButtonEnabled(True)
        lay.addWidget(self.search_edit)

        self.sort_btn = QPushButton("排序 ▾")
        self.sort_btn.setObjectName("sortBtn")
        lay.addWidget(self.sort_btn)

        # 「刷新」改为纯图标按钮(省空间); 来源路径仍通过 tooltip 展示(见 _sync_path_tooltip)
        self.reload_btn = QPushButton()
        self.reload_btn.setObjectName("reloadBtn")
        self.reload_btn.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.reload_btn.setIconSize(QSize(18, 18))
        self.reload_btn.setFixedSize(32, 30)
        self.reload_btn.setCursor(Qt.PointingHandCursor)
        self.reload_btn.setToolTip("刷新（重新读取所有来源的文件元数据）")
        lay.addWidget(self.reload_btn)

        # 「合并」按钮在顶栏最右端创建(见下方 more_btn 之后), 两种形态都用主题蓝

        # 「含子文件夹」开关: 值由"添加"弹窗里的勾选框写入, 并持久化到配置
        # (v217: 以前重启就回 False, 拖文件夹进来还会沿用上一次的旧值, 前后不一致)
        try:
            self.recursive = merge_settings.load_recursive()
        except Exception:
            self.recursive = False

        # 路径不再占用顶栏空间: 只保留一个隐藏的 QLabel 存文本,
        # 具体路径改为鼠标悬浮在「刷新」按钮上时用 tooltip 显示(见 _sync_path_tooltip)。
        self.path_label = QLabel(bar)
        self.path_label.setVisible(False)
        self._sync_path_tooltip()

        # 弹性空隙: 把右侧那组控件整体推到右边。v211 起由 top_status(ElidedLabel)
        # 自己吃掉富余空间并右对齐绘制, 所以这里不再单独放 stretch。

        # 顶栏右侧状态: 共 N 张 / 已选中 X 张 / 待写入信息
        # (原"共 N 张 · 没有待写入的改动"从底栏移上来, 以让左侧网格空间更大)
        self.top_status = ElidedLabel("")
        self.top_status.setProperty("muted", True)
        self.top_status.setStyleSheet("font-size: 12px;")
        lay.addWidget(self.top_status, 1)

        self.vl2 = self._vline()
        lay.addWidget(self.vl2)

        self.sw_year = SwitchButton("年")
        self.sw_month = SwitchButton("月")
        self.sw_day = SwitchButton("日")
        self.sw_year.setChecked(True)
        for sw in (self.sw_year, self.sw_month, self.sw_day):
            sw.setToolTip("总开关：决定全部分组的最细层级")
        lay.addWidget(self.sw_year)
        lay.addWidget(self.sw_month)
        lay.addWidget(self.sw_day)

        # v214: 统一页的层级开关(编码 / 分辨率 / 帧率) —— 和上面 年/月/日 放在同一个位置,
        # 只是按形态互换显示。默认三级全开(= v209 的三级分组), 关掉哪一级就少一层标题。
        self.sw_codec = SwitchButton("编码")
        self.sw_res = SwitchButton("分辨率")
        self.sw_fps = SwitchButton("帧率")
        for sw in (self.sw_codec, self.sw_res, self.sw_fps):
            sw.setChecked(True)
            sw.setToolTip("总开关：决定统一页分组的最细层级")
            sw.setVisible(False)
            lay.addWidget(sw)

        # 显示方式下拉(四档) + 主网格 Ctrl+滚轮缩放; 选中档位会记住, 下次启动沿用
        self.view_mode_combo = _CenterComboArrow()
        self.view_mode_combo.setObjectName("mainViewCombo")
        for _name, mode, _gw, _gh in MAIN_VIEW_MODES:
            self.view_mode_combo.addItem(_name, mode)
        saved = merge_settings.load_view_mode()
        idx = next((i for i, c in enumerate(MAIN_VIEW_MODES) if c[1] == saved),
                   next((i for i, c in enumerate(MAIN_VIEW_MODES) if c[1] == "sm"), 0))
        self.view_mode_combo.blockSignals(True)
        self.view_mode_combo.setCurrentIndex(idx)
        self.view_mode_combo.blockSignals(False)
        self.view_mode_combo.setItemDelegate(_CenterAlignDelegate(self.view_mode_combo))
        self.view_mode_combo.setStyleSheet(
            "#mainViewCombo { padding: 0px; }"
            "#mainViewCombo::drop-down { width: 0px; border: none; }")
        self.view_mode_combo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.view_mode_combo.setToolTip("切换显示方式（也可在主网格上按住 Ctrl 滚动滚轮）")
        self._fit_view_combo()
        lay.addWidget(self.view_mode_combo)

        self.vl3 = self._vline()
        lay.addWidget(self.vl3)

        self.more_btn = QPushButton("⋯")
        self.more_btn.setFixedWidth(38)
        self.more_btn.setToolTip("更多")
        lay.addWidget(self.more_btn)

        # v211: 顶栏三形态滑块(「排序 | 合并 | 统一」), 选中段实心蓝底白字。
        # ⚠️ 尺寸一律交给 sizeHint(v211 教训): 之前 QSS 的 min-width 46 + padding 让每个
        #    按钮实际 66px 宽, 三个共 198px 溢出容器写死的 162px → 蓝块右侧被轨道裁掉。
        #    现在容器用 SetFixedSize 跟随内容, 按钮只留 padding, 轨道高度按内容(不再拉满顶栏)。
        self.mode_switch = QFrame()
        self.mode_switch.setObjectName("modeSwitch")
        msl = QHBoxLayout(self.mode_switch)
        msl.setContentsMargins(0, 0, 0, 0)
        msl.setSpacing(0)
        msl.setSizeConstraint(QLayout.SetFixedSize)   # 轨道 = 三个按钮 + 内边距, 不多不少
        self.sort_mode_btn = QPushButton("排序")
        self.merge_btn = QPushButton("合并")     # 保留旧名, 兼容历史引用
        self.norm_mode_btn = QPushButton("统一")
        for _b in (self.sort_mode_btn, self.merge_btn, self.norm_mode_btn):
            _b.setObjectName("modeBtn")
            _b.setCheckable(True)
            _b.setAutoExclusive(True)
            _b.setCursor(Qt.PointingHandCursor)
            _b.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            msl.addWidget(_b)
        self.mode_switch.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.sort_mode_btn.setChecked(True)
        self.sort_mode_btn.setToolTip("照片排序界面（按时间分组、写时间戳）")
        self.merge_btn.setToolTip("检测视频规格，按能否无损拼接分组")
        self.norm_mode_btn.setToolTip("把视频统一成同一套兼容规格，生成工作副本")
        self.sort_mode_btn.clicked.connect(self.goto_sort)
        self.merge_btn.clicked.connect(self.goto_merge)
        self.norm_mode_btn.clicked.connect(self.goto_norm)
        lay.addWidget(self.mode_switch, 0, Qt.AlignVCenter)
        return bar

    def _sync_path_tooltip(self):
        """路径只通过「刷新」按钮的 tooltip 展示(只显示路径本身, 不加说明文字)。"""
        if not hasattr(self, "reload_btn"):
            return
        p = self.path_label.text().strip() if hasattr(self, "path_label") else ""
        self.reload_btn.setToolTip(p if p else "未选择文件夹")

    def _vline(self) -> QWidget:
        line = QWidget()
        line.setFixedSize(1, 22)
        line.setStyleSheet(f"background: {T.BORDER};")
        return line

    def _build_bottombar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(54)
        bar.setObjectName("bottombar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(12)

        lay.addStretch(1)

        self.clear_btn = QPushButton("取消改动")
        self.clear_btn.setToolTip("未保存的改动将被还原（Ctrl+Shift+R）")
        lay.addWidget(self.clear_btn)

        self.interval_btn = QPushButton("自动间隔…")
        self.interval_btn.setToolTip("按当前顺序分配时间戳 (Ctrl+I)")
        lay.addWidget(self.interval_btn)

        self.write_btn = QPushButton("保存改动")
        self.write_btn.setMinimumWidth(96)
        self.write_btn.setToolTip("保存已经修改的改动（Ctrl+Enter）")
        self.write_btn.setCursor(Qt.PointingHandCursor)
        set_accent(self.write_btn, True)
        lay.addWidget(self.write_btn)
        return bar

    # ---------------------------------------------------------------- 信号
    def _connect(self):
        self.open_btn.clicked.connect(self.pick_open)
        self.reload_btn.clicked.connect(self.reload_with_confirm)
        self.clear_list_btn.clicked.connect(self.clear_list)
        for sw, name in ((self.sw_year, "year"), (self.sw_month, "month"), (self.sw_day, "day")):
            sw.toggled.connect(lambda _=False, n=name: self._on_master_toggle(n))
        # v214: 统一页的层级开关(编码/分辨率/帧率)
        for sw in (self.sw_codec, self.sw_res, self.sw_fps):
            sw.toggled.connect(lambda _=False: self._on_norm_master_toggle())
        self.search_edit.textChanged.connect(self._on_filter)
        self.sort_btn.clicked.connect(self._show_sort_menu)
        self.more_btn.clicked.connect(self._show_more_menu)
        self.clear_btn.clicked.connect(self.clear_plan)
        self.interval_btn.clicked.connect(self.auto_interval)
        self.write_btn.clicked.connect(self.do_write)

        # 快捷键
        for key, fn in (
            (QKeySequence.Open, self.pick_folder),
            (QKeySequence("Ctrl+Shift+O"), self.pick_files),
            (QKeySequence("Ctrl+I"), self.auto_interval),
            (QKeySequence("Ctrl+Return"), self.do_write),
            (QKeySequence("Ctrl+R"), self.reload_with_confirm),
            (QKeySequence("Ctrl+Shift+R"), self.clear_plan),
            (QKeySequence("Ctrl+Shift+Delete"), self.clear_list),
            (QKeySequence("F5"), self.reload_with_confirm),
            # 全选的范围看"最后点的是哪一块"(v141), 不再一律全选主列表
            (QKeySequence("Ctrl+A"), self._on_select_all),
        ):
            act = QAction(self)
            act.setShortcut(key)
            act.setShortcutContext(Qt.WindowShortcut)
            act.triggered.connect(fn)
            self.addAction(act)

        self.view.toggleCollapse.connect(self.model.toggle_collapse)
        self.view.toggleSwitch.connect(self._on_group_toggle)
        self.view.normToggle.connect(self._on_norm_group_toggle)
        self.view.orderChanged.connect(self._on_order_changed)
        # v217: 模型自己的 orderChanged 以前**没有任何人监听** —— 按名称/时间/大小
        # 排序、反序之后, 待写入状态不刷新、时间计划也不跟随。拖拽那条路走的是
        # view 的信号(一次), 这里补上模型这条(排序/反序各一次), 两边不会重复。
        self.model.orderChanged.connect(self._on_order_changed)
        self.view.needThumbs.connect(self._on_need_thumbs)
        self.view.customContextMenuRequested.connect(self._show_context_menu)
        # v191: 右侧预览图上点右键 = 左侧那张菜单（排序 / 合并各挂各的，菜单项不另加）。
        # 菜单位置必须按预览控件自己的坐标算，别再套 view.viewport().mapToGlobal()。
        for _w, _fn in ((self.inspector.preview, self._show_inspector_menu),
                        (self.merge_panel.preview, self._show_merge_preview_menu)):
            _w.setContextMenuPolicy(Qt.CustomContextMenu)
            _w.customContextMenuRequested.connect(_fn)
        self.view.deleteRequested.connect(self._delete_selected)
        self.view.selectionModel().selectionChanged.connect(self._on_selection_changed)

        # 合并模式: 顶栏「无损」板块(全选/按组合并/新建文件夹)+ 右栏面板信号
        self.sel_spec_btn.clicked.connect(self.toggle_select_lossless)
        self.group_merge_btn.clicked.connect(self.open_group_merge)
        self.group_folder_btn.clicked.connect(self.open_group_folder)
        # 合并模式: 顶栏「独立」板块(全选/移动)
        self.solo_all_btn.clicked.connect(self.toggle_select_single)
        self.solo_move_btn.clicked.connect(self.move_to_merge_folder)
        self.view.toggleGroupCheck.connect(self._toggle_group_check)
        self.merge_panel.startRequested.connect(self.start_merge)
        self.merge_panel.stopRequested.connect(self._cancel_merge)
        self.merge_panel.ffmpegRequested.connect(self.open_merge_settings)
        self.merge_panel.exiftoolRequested.connect(self.open_exiftool_settings)
        self.merge_panel.paramLevelsRequested.connect(self.open_param_levels)
        self.merge_panel.cleanNormRequested.connect(self._clean_norm_copies)
        self.merge_panel.previewRequested.connect(self._open_merge_preview)
        self.merge_panel.editRequested.connect(self.toggle_edit)
        self.view.doubleClicked.connect(self._on_view_double_clicked)
        # v231 统一页多文件组块: 子卡逐张点击 / 双击打开某一张
        self.view.gridPathPress.connect(self._on_grid_path_press)
        self.view.openSubPhoto.connect(self._on_view_open_photo)

        self.inspector.planChanged.connect(self._update_pending)
        self.inspector.editsChanged.connect(self._update_pending)
        self.inspector.apply_all_btn.clicked.connect(self._apply_first_time_to_all)

        # 剪辑页: 工具条按钮 / 素材框(＋加入、右键) / 时间线(拖入、裁剪、菜单、轨道)
        self.clip_editor.splitRequested.connect(self.edit_split)
        self.clip_editor.deleteRequested.connect(self.edit_delete)
        self.clip_editor.addMediaRequested.connect(self.pick_media)
        self.clip_editor.helpRequested.connect(self.show_shortcut_help)
        self.clip_editor.togglePlayRequested.connect(self.edit_toggle_play)
        self.clip_editor.exportRequested.connect(self._export_timeline)
        self.clip_editor.lossReportRequested.connect(self._show_loss_report)
        self.clip_editor.toolRequested.connect(self._on_tool_requested)
        self.clip_editor.waveformToggled.connect(self._on_waveform_toggled)
        # v187: 音量拖动条(声波右边那组) → 只改选中的片段
        self.clip_editor.volumeRequested.connect(self._on_edit_volume_requested)
        self.clip_editor.volumeEditing.connect(self._on_volume_editing)
        self.clip_editor.captureRequested.connect(self._edit_capture_frame)
        self.clip_editor.coverRequested.connect(self._edit_set_cover)
        self.clip_editor.mediaDeleteRequested.connect(self._remove_media)
        # 裁剪线一动, 中间预览就换成那一秒的画面(不用先播放)
        self.clip_editor.playheadMoved.connect(self._on_playhead_moved)
        tv = self.clip_editor.timeline_view
        tv.sourceDropped.connect(self._edit_drop_source)
        tv.trimChanged.connect(self._on_clip_trimmed)
        tv.dragFinished.connect(self._after_edit_changed)
        tv.contextMenuRequested.connect(self._on_edit_context_menu)
        tv.trackMenuRequested.connect(self._on_track_menu)
        tv.trackCountChanged.connect(self._after_edit_changed)
        tv.trackRenamed.connect(lambda _t: self._after_edit_changed())
        # v192: 双击片段文字带就地改名 → 右栏 / 属性 / 撤销点一起跟上
        tv.clipRenamed.connect(self._on_clip_name_changed)
        self.clip_editor.media.addRequested.connect(self.pick_media)
        self.clip_editor.media.plusClicked.connect(self._edit_plus_to_timeline)
        self.clip_editor.media.contextRequested.connect(self._on_media_context_menu)
        # 素材框里点中一个素材(没点 ＋) → 中间预览显示它; 点空白 → 回到裁剪线
        self.clip_editor.mediaPreviewRequested.connect(self._preview_media)
        self.clip_editor.mediaBlankClicked.connect(self._clear_media_preview)
        # 双击素材 = 播它: 从当前停的那一秒开始(而不是每次都从头)
        self.clip_editor.mediaPlayRequested.connect(
            lambda: self._play_media_preview(self._media_pos))
        self.clip_editor.mediaSeekRequested.connect(self._on_media_seek_live)
        self.clip_editor.mediaSeekCommitted.connect(self._on_media_seek)
        # 「进度条」开关(v161): 中间画面下方那条要不要显示
        self.clip_editor.scrubToggled.connect(self._on_scrub_toggled)
        # 右栏片段清单(v140): 点卡片定位 / 拖动改轨道顺序
        self.clip_editor.clip_list.clipPicked.connect(self._on_clip_list_picked)
        self.clip_editor.clip_list.menuRequested.connect(self._on_clip_list_menu)
        # v141: 拖动是"实时"的 —— 过程中每一格都发一次(只改轨道, 不重建清单),
        # 松手再正式提交一次(重算无损 / 记撤销点)。
        self.clip_editor.clip_list.orderPreview.connect(
            lambda order: self._on_clip_list_order(order, False))
        self.clip_editor.clip_list.orderChanged.connect(self._on_clip_list_order)
        # v180: 右栏「轨道片段」自己多选了(Ctrl+点 / 长按空白拉框)
        # → 同步到轨道上, 这样"删除/分割/对齐关键帧"这些工具作用在选中的那几段。
        self.clip_editor.clip_list.selChanged.connect(self._on_clip_list_sel_changed)
        # v192: 卡片右端小铅笔就地改完名 → 轨道 / 属性 / 撤销点一起跟上
        self.clip_editor.clip_list.renameCommitted.connect(self._on_clip_name_changed)
        # 时间线片段的缩略图条: 后台串行抽帧, 抽好回主线程贴上去
        # ⚠️ 要用 parent= 传, 第一个位置参数是 ffmpeg 路径(踩过: 主窗口被当路径)
        self._film_worker = FilmstripWorker(parent=self)
        self._film_worker.frameReady.connect(tv.on_frame)
        # 抽到的帧顺手补给右栏卡片(主列表缩略图还没生成时卡片是空的)
        tv.frameArrived.connect(self._clip_list_fill_thumb)
        self._film_worker.start()
        tv.set_film_worker(self._film_worker)
        self._build_edit_shortcuts()
        self._kill_button_focus()

    # ---------------------------------------------------------------- 文件夹
    def pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择照片文件夹",
                                                  self.model.folder or "")
        if folder:
            self.open_folder(folder)

    def pick_files(self):
        from ..core.photo import IMAGE_EXT, VIDEO_EXT
        all_exts = sorted(IMAGE_EXT | VIDEO_EXT)
        patterns = " ".join(f"*.{e}" for e in all_exts)
        filt = "图片和视频 ({})".format(patterns)
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择图片或视频文件",
            self.model.folder or "", filt)
        if not paths:
            return
        self._add_files(paths)

    def _add_files(self, paths: list[str]):
        from ..core.photo import kind_of
        paths = [p for p in paths if os.path.isfile(p) and kind_of(p) != "other"]
        if not paths:
            QMessageBox.information(self, "没有可用文件",
                                    "选中的文件里没有支持的图片或视频格式。")
            return
        # 去重: 跳过已经在整个列表中存在的路径(含来自文件夹的照片)
        existing_paths = {os.path.normcase(os.path.abspath(p.path))
                          for p in self.model.photos}
        new_paths = [p for p in paths
                     if os.path.normcase(os.path.abspath(p)) not in existing_paths]
        if not new_paths:
            self.status_label.setText("选中的文件都已在该列表中，未新增")
            return
        self.sources.append({"type": "files", "paths": new_paths})
        self.model.set_folder(self._primary_dir(), False)
        self._update_title()
        self._save_session()
        self._start_scan_files(new_paths)

    def pick_open(self):
        """统一的"添加"入口: 一个对话框既能选文件夹也能选文件, 由程序判断类型。"""
        # 已经在列表里的不再显示，免得重复挑
        existing = [p.path for p in self.model.photos]
        dlg = PickerDialog(self, start_path=self.model.folder or os.path.expanduser("~"),
                           recursive=self.recursive, existing=existing)
        dlg.setMinimumSize(1080, 620)
        dlg.resize(1200, 700)
        # v191: 回车 / 右键「添加」**立刻**进主列表（弹窗不关、不用等"确定"）。
        # keys 只用来在最后点「添加」时去重，不再承担"攒着最后一起扫"的职责。
        live: dict = {"keys": set()}
        dlg.pathsAdded.connect(lambda paths, d=dlg: self._picker_live_add(paths, d, live))
        dlg.exec()
        # 「确定」选中的项里去掉本次已经实时提交过的（弹窗自己也过滤了一遍，这里双保险）
        rest = [p for p in (dlg.selected_folders() + dlg.selected_files())
                if os.path.normcase(os.path.abspath(p)) not in live["keys"]]
        if rest:
            n = self._picker_commit(rest, dlg, live)
            if n == 0 and not live["keys"]:
                self.status_label.setText("选中的内容里没有找到图片或视频")
                QMessageBox.information(
                    self, "没有可添加的内容",
                    "选中的内容里没有找到图片或视频，未添加任何文件。")
                self._restore_mode_page()
        self.recursive = dlg.recursive()
        try:
            merge_settings.save_recursive(self.recursive)
        except Exception:
            pass

    def _restore_mode_page(self):
        """加文件 / 空列表时回到"当前形态"该显示的页: 统一页不再被踢回排序。

        v207: 以前这里写死 self.view / empty_page, 统一形态加文件后会跳回排序网格,
        看起来像"提示要回排序添加"。现在按形态决定落到哪一页。
        """
        if getattr(self, "norm_mode", False):
            videos = [p for p in self.model.photos
                      if getattr(p, "kind", "") == "video"]
            # v207: 统一页主区已改成复用 self.view(合并式网格), 不再是 preview
            self.stack.setCurrentWidget(
                self.view if videos else self.empty_page)
        else:
            self.stack.setCurrentWidget(
                self.view if self.model.photos else self.empty_page)

    def _picker_live_add(self, paths, dlg, live: dict):
        """弹窗里回车 / 右键「添加」：立刻累加扫描，弹窗保持打开还能继续挑。"""
        n_dir = sum(1 for p in (paths or []) if os.path.isdir(p))
        n_file = sum(1 for p in (paths or []) if os.path.isfile(p))
        self._picker_commit(paths, dlg, live)
        parts = []
        if n_dir:
            parts.append(f"{n_dir} 个文件夹")
        if n_file:
            parts.append(f"{n_file} 个文件")
        if parts:
            self.status_label.setText("已加入 " + "、".join(parts) + "，可继续选择")

    def _picker_commit(self, paths, dlg, live: dict) -> int:
        """把一批路径立即记进来源并累加扫描。返回值 = 本批真正排进队列的文件数。

        弹窗是 exec() 模态，但 Qt 的嵌套事件循环照常把扫描线程的信号投递回主线程，
        所以列表在弹窗还开着时就会长出来。
        """
        from ..core.photo import kind_of
        recursive = dlg.recursive()
        folders, files, picked = [], [], []
        for raw in paths or []:
            p = os.path.normpath(raw)
            k = os.path.normcase(os.path.abspath(p))
            if k in live["keys"]:
                continue
            if os.path.isdir(p):
                folders.append(p)
                picked.append(p)
            elif os.path.isfile(p) and kind_of(p) != "other":
                files.append(p)
                picked.append(p)
        if not picked:
            return 0
        live["keys"].update(os.path.normcase(os.path.abspath(p)) for p in picked)

        # 记录来源(去重)。实时加入的也要记，否则关窗后"恢复上次来源"会漏掉它们。
        for f in folders:
            if not any(s["type"] == "folder" and s["path"] == f and s["recursive"] == recursive
                       for s in self.sources):
                self.sources.append({"type": "folder", "path": f, "recursive": recursive})
        if files:
            self.sources.append({"type": "files", "paths": files})

        # 展开文件夹；已经在主列表里的不再重复扫
        known = {os.path.normcase(os.path.abspath(p.path)) for p in self.model.photos}
        all_files: list[str] = []
        seen: set[str] = set()
        for f in folders:
            for fp in collect_files(f, recursive):
                k = os.path.normcase(os.path.abspath(fp))
                if k in known or k in seen:
                    continue
                seen.add(k)
                all_files.append(fp)
        for fp in files:
            k = os.path.normcase(os.path.abspath(fp))
            if k in known or k in seen:
                continue
            seen.add(k)
            all_files.append(fp)
        if not all_files:
            self.status_label.setText("选中的内容里没有找到图片或视频")
            return 0
        self.model.set_folder(self._primary_dir(), False)
        self._update_title()
        self._save_session()
        self._enqueue_scan(all_files)
        return len(all_files)

    def _enqueue_scan(self, files: list[str]):
        """累加扫描排队：_scan_files 在 worker 忙时会直接 return，连点多次不能丢文件。"""
        pending = getattr(self, "_picker_pending", None)
        if pending is None:
            pending = self._picker_pending = []
        for f in files or []:
            if f not in pending:
                pending.append(f)
        self._drain_picker_pending()

    def _drain_picker_pending(self):
        busy = self.worker is not None and self.worker.isRunning()
        # 扫描忙的时候进来的"整体重扫"请求: 排在这一轮之后(它自带完整文件集)
        rep = getattr(self, "_pending_replace", None)
        if rep:
            if busy:
                QTimer.singleShot(0, self._drain_picker_pending)
                return
            self._pending_replace = None
            self._scan_files(rep, accumulate=False)
            return
        pending = getattr(self, "_picker_pending", None)
        if not pending:
            return
        if busy:
            # finished 信号发出时线程可能还没把 isRunning() 翻成 False，下一轮再试，
            # 期间 pending 一直留着，绝不会丢。
            QTimer.singleShot(0, self._drain_picker_pending)
            return
        batch = list(pending)
        pending.clear()
        self._scan_files(batch, accumulate=True)

    def open_folder(self, folder: str, accumulate: bool = True):
        if not os.path.isdir(folder):
            return
        recursive = self.recursive
        # 去重: 同一文件夹(含相同递归设置)不重复记录来源
        for s in self.sources:
            if s["type"] == "folder" and s["path"] == folder and s["recursive"] == recursive:
                break
        else:
            self.sources.append({"type": "folder", "path": folder, "recursive": recursive})
        self.model.set_folder(self._primary_dir(), False)
        self._update_title()
        self._save_session()
        files = collect_files(folder, recursive)
        if not files:
            self.status_label.setText(f"这个文件夹里没有找到图片或视频：{folder}")
            self.stack.setCurrentWidget(self.view if self.model.photos else self.empty_page)
            return
        self._scan_files(files, accumulate)

    def open_folders(self, folders: list[str], accumulate: bool = True):
        """一次打开多个文件夹并累加到同一列表（拖入多个文件夹时用）。

        不能逐個调用 open_folder：每个 open_folder 都会起一个异步扫描 worker，
        而 _scan_files 有"worker 运行中则直接 return"的防重入保护，
        第二个文件夹的扫描会被静默丢弃（表现为"只吃进第一个文件夹"）。
        这里把所有文件夹的文件一次性收集，只触发一次扫描。
        """
        folders = [f for f in (folders or []) if f and os.path.isdir(f)]
        if not folders:
            return
        recursive = self.recursive
        # 记录来源（去重，避免重复文件夹）
        for folder in folders:
            for s in self.sources:
                if s["type"] == "folder" and s["path"] == folder and s["recursive"] == recursive:
                    break
            else:
                self.sources.append({"type": "folder", "path": folder, "recursive": recursive})
        self.model.set_folder(self._primary_dir(), False)
        self._update_title()
        self._save_session()
        files: list[str] = []
        for folder in folders:
            files += collect_files(folder, recursive)
        if not files:
            self.status_label.setText("这些文件夹里都没有找到图片或视频")
            self.stack.setCurrentWidget(self.view if self.model.photos else self.empty_page)
            return
        self._scan_files(files, accumulate)

    def reload_folder(self):
        if not self.sources:
            return
        files: list[str] = []
        # v217: 去重。来源里同时有"文件夹"和"该文件夹里的某个文件"时(先加文件夹
        # 再单独加一个文件是很常见的操作), 刷新会把同一个文件收两遍 —— 列表里
        # 出现两份, 写入时同一路径的变更被算两遍, 待写入数也翻倍。
        seen: set[str] = set()

        def _add(p: str):
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                return
            seen.add(key)
            files.append(p)

        for s in self.sources:
            if s["type"] == "folder":
                for p in collect_files(s["path"], s["recursive"]):
                    _add(p)
            else:
                for p in s["paths"]:
                    _add(p)
        if files:
            self._scan_files(files, accumulate=False)

    def reload_with_confirm(self):
        """重新扫描: 重读所有来源元数据。若有未写入的改动, 先确认以免无声丢失。"""
        pending = self._pending_photos()
        if pending:
            ret = QMessageBox.question(
                self, "刷新",
                f"刷新会重新读取所有来源的文件元数据，\n"
                f"将丢失 {len(pending)} 个文件上尚未写入的时间/字段计划。\n"
                f"继续吗？")
            if ret != QMessageBox.Yes:
                return
        self.reload_folder()

    # ---------------------------------------------------------------- 合并模式
    def open_merge(self):
        """顶栏「合并」: 把同一个窗口就地切成合并形态(不再弹独立小窗)。"""
        try:
            self.enter_merge()
        except Exception:
            self._report_ui_error("进入合并界面", "photosort_merge_error.log")

    def _report_ui_error(self, what: str, logname: str):
        """--windowed 下异常会被 Qt 静默吞掉(表现为"点了没反应"), 统一兜底成弹窗 + 日志。"""
        import traceback
        import tempfile
        tb = traceback.format_exc()
        log_path = os.path.join(tempfile.gettempdir(), logname)
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(tb)
        except Exception:
            pass
        QMessageBox.critical(self, f"{what}出错",
                             f"{what}时出错：\n\n{tb[-1200:]}"
                             f"\n\n（详细日志：{log_path}）")

    def toggle_mode(self):
        """历史兼容入口(v210 顶栏已改成三按钮各自直达): 在合并 / 统一之间切。"""
        if self.norm_mode:
            self.goto_merge()
        else:
            self.goto_norm()

    def goto_sort(self):
        """顶栏「排序」: 从合并 / 统一任一形态回到排序。"""
        try:
            if self.norm_mode:
                self.leave_norm()
            elif self.merge_mode:
                self.leave_merge()
        except Exception:
            self._report_ui_error("返回排序界面", "photosort_merge_error.log")

    def goto_merge(self):
        """顶栏「合并」: 进合并形态(若正在统一, 先干净退出)。"""
        try:
            if self.norm_mode:
                self.leave_norm()
            self.open_merge()
        except Exception:
            self._report_ui_error("进入合并界面", "photosort_merge_error.log")

    def goto_norm(self):
        """顶栏「统一」: 进统一格式大形态(若正在合并, 先干净退出)。"""
        try:
            self.open_norm_page()
        except Exception:
            self._report_ui_error("进入统一格式", "photosort_normalize_error.log")

    def open_norm_page(self):
        """顶栏「统一」: 用主列表全部视频进统一页, 优先复用已探测规格。"""
        if self.norm_mode:
            return
        if self.merge_mode:
            self.leave_merge()          # 内含停剪辑 / 停探测 / 切回排序 UI
        if self._merge_worker is not None:
            QMessageBox.information(self, "正在合并",
                                    "合并还在进行中，请等它完成或先点「取消」。")
            return

        self.norm_mode = True
        # v207: 统一页主区也用合并式网格(缩略图+分组标题+规格行+勾选框)。
        # 只开视图层的 merge_mode(渲染勾选/规格行), 形态标志仍是 norm_mode。
        self.view.merge_mode = True
        self.view.delegate.merge_mode = True
        # v233: 统一页勾选走路径级语义(与组块子卡统一), 不吃合并式的点选加选
        self.view.norm_mode = True
        # 素材 = 主列表全部视频(和合并页同一批); 规格复用 merge_specs, 缺的后台补测
        videos = [p for p in self.model.photos if getattr(p, "kind", "") == "video"]
        if not (self.merge_ffmpeg and self.merge_ffprobe) and self.ffmpeg_pair:
            self.merge_ffmpeg, self.merge_ffprobe = self.ffmpeg_pair

        self.right_stack.setCurrentWidget(self.norm_workspace.panel)
        self.norm_workspace.set_context(videos, self.merge_specs,
                                        self.merge_ffmpeg, self.merge_ffprobe)
        self.norm_workspace.selected_paths = set()
        self._apply_norm_groups()
        if videos:
            self.stack.setCurrentWidget(self.view)
            # v207: 默认全选所有视频, 勾选框亮起; 用户可去掉不想统一的
            self._grid_sel_paths = {p.path for p in videos}
            self.view.select_all_photos()
            self.norm_workspace.selected_paths = set(self._grid_sel_paths)
            self._sync_group_checks()
        else:
            self._grid_sel_paths = set()
            self.stack.setCurrentWidget(self.empty_page)
        self.window_stack.setCurrentWidget(self.splitter)
        self.bottom_bar.setVisible(False)
        self._sync_mode_ui()
        self._refresh_top_status()

        missing = [p for p in videos if p.path not in self.merge_specs]
        if missing:
            if self.merge_ffmpeg and self.merge_ffprobe:
                self._start_probe()          # 后台补测, 完成回调里刷新统一页
            else:
                self._check_ffmpeg_async(force=True)

    def leave_norm(self):
        if not self.norm_mode:
            return
        if self._probe_worker is not None:
            w = self._probe_worker
            self._probe_worker = None
            self._stop_thread(w, 2000)
        self.norm_mode = False
        # v207: 统一页借用了合并网格, 离开时恢复成排序网格的铺行
        self.view.merge_mode = False
        self.view.delegate.merge_mode = False
        self.view.norm_mode = False
        for p in self.model.photos:
            p.merge_line = ""
            for _a in ("norm_key", "norm_labels", "norm_need"):
                if hasattr(p, _a):
                    delattr(p, _a)
        self.model.set_merge_groups(None)
        self.model.set_norm_groups(None)
        self.norm_workspace.selected_paths = set()
        self._grid_sel_paths = set()     # v220: 离开统一态, 勾选集合不作数了
        self.right_stack.setCurrentWidget(self.inspector)
        self.bottom_bar.setVisible(True)
        self.window_stack.setCurrentWidget(self.splitter)
        self.stack.setCurrentWidget(self.view if self.model.photos else self.empty_page)
        self._on_selection_changed()
        self._refresh_top_status()
        self._sync_mode_ui()

    def enter_merge(self):
        if self.merge_mode:
            return
        # v211: 先把统一页的顶栏参数条收起来 —— 否则它和下面要显示的「无损/单独」
        # 板块同帧可见, 顶栏最小宽瞬间超窗口宽, Qt 会直接把窗口撑宽(切回去也不缩回)。
        self.norm_param_slot.setVisible(False)
        videos = [p for p in self.model.photos if getattr(p, "kind", "") == "video"]
        self._merge_videos = videos
        self.merge_mode = True
        self.merge_specs = {}
        self.merge_errors = {}
        # 用排序模式下已经查到的结果垫底; 真实查找仍由 _start_probe 在后台重做一遍
        self.merge_ffmpeg = self.ffmpeg_pair[0] if self.ffmpeg_pair else None
        self.merge_ffprobe = self.ffmpeg_pair[1] if self.ffmpeg_pair else None
        self._merge_out = ""

        # 网格切成合并形态: 勾选框、规格行、拖拽只在同组内生效
        self.view.merge_mode = True
        self.view.delegate.merge_mode = True
        self.view.norm_mode = False          # v233: 合并态保持"点卡即勾/取消"的旧口径
        self._grid_sel_paths = set()          # v220: 每次进合并都是空勾选
        self.view.clearSelection()
        self.model.filter_text = ""
        self.search_edit.clear()

        # 每次进合并都是一次新的分组, 上次收起过哪些组就不作数了(规格签名会变,
        # 留着只会让人以为"这组是空的")。
        self.model.collapsed.clear()
        # 先给一个"正在检测"的临时组, 让视频立刻出现在网格里(而不是空白等着)
        for p in videos:
            p.merge_line = "检测中…"
        if videos:
            self.model.set_merge_groups([{
                "key": ("__probe__",), "label": "正在检测规格…", "tag": "single",
                "note": "", "photos": videos,
            }])
        else:
            self.model.set_merge_groups([])

        self.right_stack.setCurrentWidget(self.merge_panel)
        self.bottom_bar.setVisible(False)
        # 先按"可用"渲染, 免得一进合并界面就弹红字; 真正结果由探测回调覆盖
        self.merge_panel.set_context({}, {}, self.ffmpeg_ok is not False)
        self.merge_panel.set_ffmpeg_state(self.ffmpeg_ok)
        self.merge_panel.set_selection([])
        self.merge_panel.hint.setText("正在查找 FFmpeg 并检测视频规格…" if videos
                                      else "还没有视频可合并，先「排序」回去把视频加进来。")
        self.merge_panel.hint.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
        self.merge_panel.set_busy(False)
        if not self.merge_panel.out_dir():
            self.merge_panel.set_dir(os.path.dirname(videos[0].path) if videos else "")
        self.merge_panel.name_edit.setText(self._next_merge_name(videos))

        self._sync_mode_ui()
        self.stack.setCurrentWidget(self.view if videos else self.empty_page)
        self._refresh_top_status()
        if videos:
            self._start_probe()

    def leave_merge(self):
        if not self.merge_mode:
            return
        if self._merge_worker is not None:
            QMessageBox.information(self, "正在合并",
                                    "合并还在进行中，请等它完成或先点「取消」。")
            return
        if self._probe_worker is not None:
            w = self._probe_worker
            self._probe_worker = None
            self._stop_thread(w, 2000)

        self.merge_mode = False
        self.view.merge_mode = False
        self.view.delegate.merge_mode = False
        self.view.norm_mode = False          # v233: 回排序态, 恢复 Qt 原生选择语义
        self._grid_sel_paths = set()          # v220: 离开合并态, 勾选集合清空
        for p in self.model.photos:
            p.merge_line = ""
        self.model.set_norm_groups(None)       # 保险: 清掉可能的统一态残留
        self.model.set_merge_groups(None)      # 回到按时间分组
        self.right_stack.setCurrentWidget(self.inspector)
        self.bottom_bar.setVisible(True)
        self.edit_mode = False                 # 离开合并形态时剪辑态一并退出
        self._edit_pause()                     # 声音也要跟着收(里面含 _stop_audio)
        # v199: 离开合并 = 退出所有剪辑会话(形态切换, 静默关窗不弹确认)
        self._shutdown_all_clips()
        self._set_edit_shortcuts(False)
        self.window_stack.setCurrentWidget(self.splitter)
        self._set_edit_shortcuts(False)
        self._sync_mode_ui()
        self.stack.setCurrentWidget(self.view if self.model.photos else self.empty_page)
        self._on_selection_changed()
        self._refresh_top_status()
        # 每次切回排序模式都重新查一次 FFmpeg, 下次进合并界面按钮就是真实颜色
        self._check_ffmpeg_async(force=True)

    # ---------------------------------------------------------------- 剪辑态
    def toggle_edit(self):
        """合并页「剪辑」按钮(v199): 每次都新开一个剪辑窗口(不再"收起")。"""
        self._new_clip_session(self.view.selected_photos())

    # ------------------------------------------------------------ 剪辑窗口 / 会话
    def _clip_window_get(self):
        """当前激活会话的剪辑窗口(没有则建)。兼容旧代码里对单窗口的引用。"""
        sess = self._active_session
        if sess is None:
            sess = self._boot_session
        if sess.window is not None:
            return sess.window
        from .clip_window import ClipWindow
        w = ClipWindow(self.clip_editor, self, num=sess.num)
        w.closeRequested.connect(lambda s=sess: self._on_clip_window_closed(s))
        w.activated.connect(lambda s=sess: self._on_clip_window_activated(s))
        sess.window = w
        return w

    def _show_clip_window(self):
        w = self._clip_window_get()
        w.show_for(self)
        # 快捷键挂在剪辑窗口上(WindowShortcut), 换了顶层窗口要重建一次才认
        self._rebuild_edit_shortcuts()

    def _next_clip_num(self) -> int:
        """分配下一个固定编号(关掉中间的不重排, 全关归零)。"""
        self._clip_seq += 1
        return self._clip_seq

    def _activate_session(self, sess: ClipSession):
        """切到某个会话: 先把当前会话的声音/播放停掉(只有一个窗口发声),
        再换 _active_session 指针 —— 所有 self.clip_editor 等引用随之切换。
        """
        if sess is None or sess is self._active_session:
            return
        if self.edit_mode:
            try:
                self._edit_pause()
            except Exception:
                pass
        self._active_session = sess
        self._sync_mode_ui()

    def _on_clip_window_activated(self, sess: ClipSession):
        """用户点/切到某个剪辑窗口: 把它设为激活会话。"""
        self._activate_session(sess)

    def _new_clip_session(self, photos=None):
        """新开一个剪辑会话(「新增剪辑」/「剪辑」按钮): 用当前勾选做素材。"""
        if not self.merge_mode:
            self.enter_merge()
        if self._merge_worker is not None:
            QMessageBox.information(self, "正在合并",
                                    "合并还在进行中，请等它完成或先点「取消」。")
            return
        # 先停掉当前会话的播放/声音(只留一个窗口响)
        if self._active_session is not None and self.edit_mode:
            try:
                self._edit_pause()
            except Exception:
                pass

        num = self._next_clip_num()
        sess = ClipSession(num)
        # 新会话先挂成 active, 后面的 self.* 赋值都落进它
        self._active_session = sess
        self._clip_sessions.append(sess)

        # 全新一套编辑现场
        self.edit_timeline = Timeline(3)
        self.clip_editor = ClipEditor()
        self.clip_editor.set_timeline(self.edit_timeline)
        self.clip_editor.timeline_view.clipSelected.connect(self._on_edit_clip_selected)
        self._connect_clip_editor_signals()
        self.clip_panel = self.clip_editor.panel
        self._edit_built = True
        self._edit_entered = True
        self._edit_allowed = {self._canon(p.path) for p in (photos or [])}
        self._install_area_taps()
        self._build_edit_shortcuts()

        w = self._clip_window_get()
        w.show_for(self)
        self.edit_mode = True
        self._sync_mode_ui()
        self._refresh_media_panel()
        self._refresh_top_status()
        self._start_clip_probe_if_needed()
        self.status_label.setText(
            f"{sess.title} 已打开：勾选的 {len(photos or [])} 个文件在素材框里，"
            "点格子右下角 ＋ 加到时间线")

    def _connect_clip_editor_signals(self):
        """把一个新建的 ClipEditor 的信号接到主窗口(新会话复用, 与 __init__ 同套)。"""
        ed = self.clip_editor
        ed.splitRequested.connect(self.edit_split)
        ed.deleteRequested.connect(self.edit_delete)
        ed.addMediaRequested.connect(self.pick_media)
        ed.helpRequested.connect(self.show_shortcut_help)
        ed.togglePlayRequested.connect(self.edit_toggle_play)
        ed.exportRequested.connect(self._export_timeline)
        ed.lossReportRequested.connect(self._show_loss_report)
        ed.toolRequested.connect(self._on_tool_requested)
        ed.waveformToggled.connect(self._on_waveform_toggled)
        ed.volumeRequested.connect(self._on_edit_volume_requested)
        ed.volumeEditing.connect(self._on_volume_editing)
        ed.captureRequested.connect(self._edit_capture_frame)
        ed.coverRequested.connect(self._edit_set_cover)
        ed.mediaDeleteRequested.connect(self._remove_media)
        ed.playheadMoved.connect(self._on_playhead_moved)
        tv = ed.timeline_view
        tv.sourceDropped.connect(self._edit_drop_source)
        tv.trimChanged.connect(self._on_clip_trimmed)
        tv.dragFinished.connect(self._after_edit_changed)
        tv.contextMenuRequested.connect(self._on_edit_context_menu)
        tv.trackMenuRequested.connect(self._on_track_menu)
        tv.trackCountChanged.connect(self._after_edit_changed)
        tv.trackRenamed.connect(lambda _t: self._after_edit_changed())
        tv.clipRenamed.connect(self._on_clip_name_changed)
        ed.media.addRequested.connect(self.pick_media)
        ed.media.plusClicked.connect(self._edit_plus_to_timeline)
        ed.media.contextRequested.connect(self._on_media_context_menu)
        ed.mediaPreviewRequested.connect(self._preview_media)
        ed.mediaBlankClicked.connect(self._clear_media_preview)
        ed.mediaPlayRequested.connect(lambda: self._play_media_preview(self._media_pos))
        ed.mediaSeekRequested.connect(self._on_media_seek_live)
        ed.mediaSeekCommitted.connect(self._on_media_seek)
        ed.scrubToggled.connect(self._on_scrub_toggled)
        ed.clip_list.clipPicked.connect(self._on_clip_list_picked)
        ed.clip_list.menuRequested.connect(self._on_clip_list_menu)
        ed.clip_list.orderPreview.connect(
            lambda order: self._on_clip_list_order(order, False))
        ed.clip_list.orderChanged.connect(self._on_clip_list_order)
        ed.clip_list.selChanged.connect(self._on_clip_list_sel_changed)
        ed.clip_list.renameCommitted.connect(self._on_clip_name_changed)
        # 抽帧 worker 跟着新 editor 的时间线视图
        fw = FilmstripWorker(parent=self)
        fw.frameReady.connect(tv.on_frame)
        tv.frameArrived.connect(self._clip_list_fill_thumb)
        fw.start()
        tv.set_film_worker(fw)
        self._film_worker = fw

    def _shutdown_all_clips(self):
        """离开合并/退出程序时静默关掉所有剪辑会话(不弹确认、不留窗口)。"""
        for sess in list(self._clip_sessions):
            for attr in ("_clip_probe_worker", "_film_worker", "_audio_worker",
                         "_player", "_export_worker", "_tool_worker", "_wave_worker"):
                w = getattr(sess, attr, None)
                if w is not None:
                    try:
                        self._stop_thread(w, 1000)
                    except Exception:
                        pass
            sess.stop_workers()
            w = sess.window
            if w is not None:
                try:
                    w.shutdown()
                except Exception:
                    pass
                try:
                    w.deleteLater()
                except Exception:
                    pass
                sess.window = None
        self._clip_sessions.clear()
        self._clip_seq = 0
        self._active_session = None
        self._boot_session = ClipSession(1)

    def _on_clip_window_closed(self, sess: ClipSession = None):
        """点某个剪辑窗口的 ×: 非空先二次确认, 确认后彻底销毁这个会话。"""
        if sess is None:
            sess = self._active_session or self._boot_session
        if sess.has_content():
            if not confirm_yes_no(
                    self, "关闭剪辑窗口",
                    f"{sess.title} 里还有时间线内容或未导出的改动。\n"
                    "关闭后这个窗口的现场会被丢弃，确定关闭吗？",
                    center=True):
                return
        self._destroy_clip_session(sess)

    def _destroy_clip_session(self, sess: ClipSession):
        """彻底关掉一个会话: 停线程/声音、销窗口、移出列表。"""
        was_active = (sess is self._active_session)
        # 切到它再停, 保证 _edit_pause / 各 stop 落在正确现场
        prev = self._active_session
        self._active_session = sess
        try:
            if self.edit_mode:
                self._edit_pause()
        except Exception:
            pass
        for attr in ("_clip_probe_worker", "_film_worker", "_audio_worker",
                     "_player", "_export_worker", "_tool_worker", "_wave_worker"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    self._stop_thread(w, 1500)
                except Exception:
                    pass
        sess.stop_workers()
        # 关掉该会话开着的属性弹窗
        for d in list(getattr(sess, "_clip_prop_dialogs", []) or []):
            try:
                d.close()
            except Exception:
                pass
        w = sess.window
        if w is not None:
            try:
                w.shutdown()             # 真关(允许 closeEvent accept)
            except Exception:
                pass
            try:
                w.deleteLater()
            except Exception:
                pass
            sess.window = None
        if sess in self._clip_sessions:
            self._clip_sessions.remove(sess)

        if not self._clip_sessions:
            # 全关: 先退出剪辑态(此时 active 还指向刚被关的会话, clip_editor 有效),
            # 再清编号 / active / 重建全新引导会话。
            if self.edit_mode:
                self.leave_edit()
            self._clip_seq = 0
            self._active_session = None
            self._boot_session = ClipSession(1)   # 丢弃旧现场, 下次 enter_edit 用全新一份
        else:
            # 还有别的窗口: 激活剩最后一个, 并把现场切过去
            nxt = self._clip_sessions[-1]
            self._active_session = nxt
            if nxt.window is not None:
                nxt.window.show_for(self)
            self._sync_mode_ui()

    # ------------------------------------------------------------ 剪辑窗口全屏
    def _toggle_clip_fullscreen(self):
        """F11: 剪辑窗口全屏 / 复原。

        ⚠️ 主窗口的 keyPressEvent 收不到 —— 剪辑是**另一个顶层窗口**, 按键先给
        它。所以这条必须在 clip_editor 上挂 WindowShortcut(v155 之前一直没生效
        就是这个原因: 只在主窗口里写了 keyPressEvent)。
        """
        w = getattr(self, "_clip_window", None)
        if w is None:
            return
        if w.isFullScreen():
            w.showNormal()
        else:
            w.showFullScreen()
        self.clip_editor.setFocus()
        self._restore_clip_front()

    def _clip_fullscreen_exit(self) -> bool:
        """在全屏就退出全屏并返回 True(给 Esc 用)。"""
        w = getattr(self, "_clip_window", None)
        if w is None or not w.isFullScreen():
            return False
        w.showNormal()
        self._restore_clip_front()
        return True

    def open_edit(self):
        try:
            self.enter_edit()
        except Exception:
            self._report_ui_error("进入剪辑界面", "photosort_edit_error.log")

    def enter_edit(self):
        if not self.merge_mode or self.edit_mode:
            return
        if self._merge_worker is not None:
            QMessageBox.information(self, "正在合并",
                                    "合并还在进行中，请等它完成或先点「取消」。")
            return
        # v199: 第一次进剪辑 —— 把 __init__ 建好的引导会话提升成「剪辑1」
        # 并登记进会话表(之后 _new_clip_session 会建剪辑2、剪辑3…)。
        if not self._clip_sessions:
            boot = self._boot_session
            boot.num = self._next_clip_num()
            boot.title = f"剪辑{boot.num}"
            self._active_session = boot
            self._clip_sessions.append(boot)
        if not self._edit_built:
            self._build_edit_timeline()
            self._edit_built = True
        first_time = not self._edit_entered
        self._edit_entered = True
        self.edit_mode = True
        # 合并页勾了哪几个, 剪辑的素材框就只列哪几个(v140); 一个都没勾 = 空框。
        # 每次进剪辑都重新取一遍: 用户可能回去改了勾选又进来。
        try:
            allowed = {self._canon(p.path) for p in self.view.selected_photos()}
            # v183: 方案里带进来的素材不能因为"这次合并页没勾"就从素材框消失
            allowed |= set(self._project_sources)
            self._edit_allowed = allowed
        except Exception:
            self._edit_allowed = None
        self._media_preview = None      # 进剪辑时不认上次选中的素材
        self._media_playing = False
        self._media_pos = 0.0
        self._media_dur = 0.0
        self._media_ui_timer.stop()
        self.clip_editor.hide_scrubber()
        self._clip_scrub = None           # 进剪辑不认上次看的那一段
        self._clip_scrub_pos = 0.0
        self._tl_scrub = False            # 进度条也回到"没有范围"
        self._clip_seeking = False
        self._media_seek_paused = False
        # 「进度条」开关按存盘的值恢复(默认开), 按钮状态跟着走
        try:
            from ..merge import settings as _st
            self._scrub_on = bool(_st.load_scrub_on())
        except Exception:
            self._scrub_on = True
        try:
            self.clip_editor.scrub_btn.blockSignals(True)
            self.clip_editor.scrub_btn.setChecked(self._scrub_on)
            self.clip_editor.scrub_btn.blockSignals(False)
        except Exception:
            pass
        self._stop_audio()
        self._seek_playing = False
        self._seek_timer.stop()
        self.clip_editor.set_photos(self.model.photos)
        self.clip_editor.show_clip(None)
        self.clip_panel.set_clip(None, self.edit_timeline,
                                 {p.path: p for p in self.model.photos})
        self._show_clip_window()           # 独立窗口: 主界面留在后面, 不切
        self._sync_mode_ui()
        self._refresh_top_status()
        self._start_clip_probe_if_needed()
        self._set_edit_shortcuts(True)
        self._refresh_media_panel()
        self._refresh_clip_list()
        # 进剪辑就把预览填上(轨道上只有一个素材时, 那一个就是预览内容)
        self._refresh_preview_at(self.edit_timeline.playhead
                                 if self.edit_timeline else 0.0)
        self._refresh_lossless()
        if first_time:
            # 只在**第一次**进剪辑时清空历史; 之后关窗再开要能接着撤销
            self._undo_stack.clear()
            self._undo_index = -1
            self._edit_commit()

    def leave_edit(self):
        if not self.edit_mode:
            return
        self._edit_pause()                 # 走人前先把 ffmpeg / 声音收掉
        self.edit_mode = False
        # 进度条的范围状态别带到下次进来(v161: 现在拖裁剪线也会显示它)
        self._tl_scrub = False
        self._clip_scrub = None
        self._clip_seeking = False
        self._media_seek_paused = False
        try:
            self.clip_editor.hide_scrubber()
        except Exception:
            pass
        self._set_edit_shortcuts(False)
        w = self._clip_window
        if w is not None:
            # F11 全屏着退出的话, 下次点「剪辑」会一上来就全屏 —— 用户会觉得
            # "怎么突然铺满整个屏幕了"。走之前先复原(v158)。
            try:
                if w.isFullScreen():
                    w.showNormal()
            except Exception:
                pass
            w.hide()                       # 只藏起来: 时间线/素材/撤销历史都留着
        self.window_stack.setCurrentWidget(self.splitter)
        # ⚠️ 不能用 `_merge_videos`: 那是**进合并那一刻**的快照, 之后扫出来的
        # 视频不在里面 —— 结果就是"关掉剪辑窗口, 合并页一片空白"(v141)。
        # 有没有文件只看主列表自己。
        self._resync_merge_list()
        self.right_stack.setCurrentWidget(self.merge_panel)
        self._sync_mode_ui()
        self._refresh_top_status()

    def _resync_merge_list(self):
        """回到合并页时把主列表的行重算一遍(关闭剪辑窗口也会调)。

        合并模式的行是按"规格分组"算的, 分组没到位(探测没跑完/一个组都没有)
        时行是空的 —— 不重算就会看到"文件全没了"。
        """
        try:
            self.model.rebuild()
        except Exception:
            pass
        self.stack.setCurrentWidget(
            self.view if self.model.photos else self.empty_page)
        try:
            self.view.viewport().update()
        except Exception:
            pass

    # ------------------------------------------------------------ 快捷键
    def _build_edit_shortcuts(self):
        """剪辑态专用键位: 只在剪辑态启用, 免得在排序界面按 S 把照片切了。"""
        self._edit_bindings = load_bindings()
        self._rebuild_edit_shortcuts()

    def _rebuild_edit_shortcuts(self):
        """按当前键位整批重建(改键后调用)。键位来自 F1 面板里改的那套。"""
        for sc in getattr(self, "_edit_shortcuts", []):
            try:
                sc.setEnabled(False)
                sc.deleteLater()
            except Exception:
                pass
        self._edit_shortcuts = []
        b = self._edit_bindings or {}
        nudge = self.clip_editor.nudge_playhead
        tv = self.clip_editor.timeline_view
        table = {
            "play": self.edit_toggle_play,
            "left": lambda: nudge(-0.5),
            "right": lambda: nudge(0.5),
            "shift_left": lambda: nudge(-2.0),
            "shift_right": lambda: nudge(2.0),
            "prev_frame": lambda: nudge(-FRAME_STEP),
            "next_frame": lambda: nudge(FRAME_STEP),
            "prev_kf": lambda: self._edit_jump_keyframe(-1),
            "next_kf": lambda: self._edit_jump_keyframe(1),
            "snap_kf": self._edit_snap_keyframe,
            "home": lambda: self.clip_editor.set_playhead(0.0),
            "end": lambda: self.clip_editor.set_playhead(
                self.edit_timeline.total_duration()),
            "undo": self._edit_undo,
            "redo": self._edit_redo,
            "split": self.edit_split,
            "delete": self.edit_delete,
            "esc": self.edit_clear_selection,
            "zoom_in": tv.zoom_in,
            "zoom_out": tv.zoom_out,
            "fit": tv.fit_timeline,
            # 平移没有键盘版: 这两条就只有滚轮(Shift+滚轮)。
            "wheel_pan_left": lambda: tv.wheel_pan(-60),
            "wheel_pan_right": lambda: tv.wheel_pan(60),
            "capture_frame": self._edit_capture_frame,
            "set_cover": self._edit_set_cover,
            "help": self.show_shortcut_help,
        }
        # 每个动作"直接执行自己"的入口 —— 滚轮键位带了方向(滚轮上/下)时就用它,
        # 不走下面的反向逻辑(v157)。
        self._edit_actions = dict(table)
        # 滚轮类键位: QShortcut 认不了滚轮(它只吃键盘), 单独走时间线的 wheel 分派。
        # 滚轮录在哪个动作的备用键里就执行哪个动作(见 _edit_wheel_dispatch)。
        wheel_extra = {
            "wheel_pan_left":   lambda s, x: tv.wheel_pan(-60),
            "wheel_pan_right":  lambda s, x: tv.wheel_pan(60),
        }
        self._edit_wheel_actions = {}
        for aid, fn0 in table.items():
            if aid in wheel_extra:
                continue
            pair = WHEEL_PAIRS.get(aid)
            if pair and pair in table:
                self._edit_wheel_actions[aid] = (
                    lambda s=1, x=0, a=aid, p=pair: (table[a]() if s > 0 else table[p]()))
            else:
                self._edit_wheel_actions[aid] = lambda s=1, x=0, f=fn0: f()
        self._edit_wheel_actions.update(wheel_extra)
        tv.wheel_hook = self._edit_wheel_dispatch

        on = bool(self.edit_mode)
        # 每个动作两个槽(v140): 主键和备用键各注册一个, 按哪个都触发。
        # 同一个键注册两遍会在 Qt 里变成 ambiguous(两个都不响应), 这里先去重。
        seen: set[str] = set()
        for aid, fn in table.items():
            for key in as_slots(b.get(aid)):
                key = str(key or "").strip()
                if not key or is_wheel_seq(key) or key in seen:
                    continue
                seen.add(key)
                try:
                    # ⚠️ 挂在 clip_editor 上而不是主窗口: 剪辑是独立窗口(v128),
                    #    WindowShortcut 只对"父控件的顶层窗口"生效 —— 挂主窗口的话
                    #    剪辑窗口激活时按空格/ S 全都没反应。
                    sc = QShortcut(QKeySequence(key), self.clip_editor)
                except Exception:
                    continue
                sc.setContext(Qt.WindowShortcut)
                sc.activated.connect(fn)
                sc.setEnabled(on)
                self._edit_shortcuts.append(sc)

        # 全选(v141): 不进键位表(它是"按当前在哪一块"决定选什么的), 单独挂一条。
        # ⚠️ 必须挂在 clip_editor 上: 剪辑是独立窗口, 挂主窗口的话剪辑窗口激活时
        #    WindowShortcut 收不到。
        try:
            sc = QShortcut(QKeySequence("Ctrl+A"), self.clip_editor)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(self._on_select_all)
            sc.setEnabled(on)
            self._edit_shortcuts.append(sc)
        except Exception:
            pass

        # F11 全屏(v155): 剪辑是独立顶层窗口, 主窗口的 keyPressEvent 收不到按键 ——
        # 必须挂在 clip_editor 上(和上面那条同一个道理)。
        try:
            sc = QShortcut(QKeySequence("F11"), self.clip_editor)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(self._toggle_clip_fullscreen)
            sc.setEnabled(on)
            self._edit_shortcuts.append(sc)
        except Exception:
            pass

        # v184: 片段 Ctrl+C 复制 / Ctrl+V 粘贴(粘到裁剪线)。固定两个键,
        # **不进 F1 键位表、不写用户配置**; 用户已经把这两个键绑到别的动作
        # 时就不注册 —— 同一个键挂两条 QShortcut 会被 Qt 判成 ambiguous,
        # 结果连用户原来那条也不响应了。
        used = {QKeySequence(k).toString().lower() for k in seen}
        used.add("ctrl+a")          # Ctrl+A 是单独挂的, 没进 seen
        for key, fn in (("Ctrl+C", self._clipboard_copy),
                        ("Ctrl+V", self._clipboard_paste)):
            if QKeySequence(key).toString().lower() in used:
                continue
            try:
                sc = QShortcut(QKeySequence(key), self.clip_editor)
                sc.setContext(Qt.WindowShortcut)
                sc.activated.connect(fn)
                sc.setEnabled(on)
                self._edit_shortcuts.append(sc)
            except Exception:
                pass

    def _set_edit_shortcuts(self, on: bool):
        for sc in getattr(self, "_edit_shortcuts", []):
            sc.setEnabled(bool(on))

    def _edit_wheel_dispatch(self, event) -> bool:
        """时间线上滚滚轮: 按快捷键表里录的那套分派。

        返回 False 就退回时间线自带的(Ctrl+滚轮缩放 / 直接平移), 所以把滚轮
        键位全清掉也不会变成"滚轮失灵"。
        """
        if not self.edit_mode:
            return False
        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.angleDelta().x()      # 触控板横向滚也算
        if delta == 0:
            return False
        mods = event.modifiers()
        hit = set()
        if mods & Qt.ControlModifier:
            hit.add("Ctrl")
        if mods & Qt.AltModifier:
            hit.add("Alt")
        if mods & Qt.ShiftModifier:
            hit.add("Shift")
        aid_seq = match_wheel(self._edit_bindings or {}, hit, "Up" if delta > 0 else "Down")
        if not aid_seq:
            return False
        aid, seq = aid_seq
        x = event.position().x()
        # ⚠️ 键位把方向写死了(Ctrl+Wheel+Up) → 就执行这条动作本身。
        #    以前只拿到 aid, 一律按"下滚执行反向动作"处理, 于是「缩小时间线」
        #    录了 Ctrl+Wheel+Down 之后往下滚反而放大(v157 修)。
        if parse_seq(seq or "").get("dir"):
            fn0 = (getattr(self, "_edit_actions", None) or {}).get(aid)
            if fn0 is None:
                return False
            try:
                fn0()
            except Exception:
                return False
            return True
        # 键位没写方向(Ctrl+Wheel) → 上滚干这个, 下滚干它的反向动作
        fn = (getattr(self, "_edit_wheel_actions", None) or {}).get(aid)
        if fn is None:
            return False
        try:
            fn(1 if delta > 0 else -1, x)
        except Exception:
            return False
        return True

    # ------------------------------------------------------------ 关键帧
    def _edit_target_clip(self):
        """关键帧落在哪个片段上: 选中的优先, 没有就用裁剪线底下那个。"""
        tl = self.edit_timeline
        if tl is None:
            return None
        tv = self.clip_editor.timeline_view
        clip = getattr(tv, "_selected", None)
        if clip is not None and clip in tl.clips:
            return clip
        t = tl.playhead
        best = None
        for c in tl.clips:
            if c.start - 1e-6 <= t <= c.end + 1e-6:
                if best is None or c.track < best.track:
                    best = c
        return best

    def _edit_keyframe_times(self, clip) -> list[float]:
        """兼容旧调用: 统一委托给 Clip.timeline_keyframe_times()(含倍速, v186)。"""
        try:
            return clip.timeline_keyframe_times()
        except Exception:
            return []

    def _edit_all_keyframe_times(self) -> list[float]:
        """整条时间线上所有视频片段的关键帧(时间线秒), 排好序去好重。

        v186: 以前只认「选中的那一段」—— 选中了音频/图片、或裁剪线在别的片段上,
        就提示"先选一个片段 / 还没读到关键帧", 用户拿 ← / → 找关键帧时像按键失灵。
        现在一路按下去能把整条片子的关键帧挨个找完, 也不会挑错片段。
        """
        tl = self.edit_timeline
        if tl is None:
            return []
        out = []
        for c in tl.clips:
            if getattr(c, "kind", None) != "video":
                continue
            try:
                out.extend(c.timeline_keyframe_times())
            except Exception:
                pass
        return sorted(set(out))

    def _edit_jump_keyframe(self, direction: int):
        """上一个 / 下一个关键帧(用户把 ← / → 绑在这两个动作上)。"""
        tl = self.edit_timeline
        if tl is None:
            return
        ks = self._edit_all_keyframe_times()
        if not ks:
            self.status_label.setText("还没读到关键帧，正在后台读…")
            try:
                self.clip_editor.flash("还没读到关键帧，正在后台读取…", "warn")
            except Exception:
                pass
            self._start_clip_probe_if_needed()
            return
        t = float(tl.playhead or 0.0)
        if direction < 0:
            prev = [k for k in ks if k < t - 1e-4]
            nt = prev[-1] if prev else ks[0]
        else:
            nxt = [k for k in ks if k > t + 1e-4]
            nt = nxt[0] if nxt else ks[-1]
        self.clip_editor.set_playhead(nt)
        self.status_label.setText(f"关键帧 {nt:.3f}s")
        # v187: 不再弹「已落到关键帧 …」浮层 —— 红线高亮 + 截图按钮变蓝
        # 已经足够说明落点, 画面上再盖一条提示反而挡事(用户明确不要)。

    def _edit_snap_keyframe(self):
        """把裁剪线贴到最近的关键帧(无损下刀点)。"""
        tl = self.edit_timeline
        if tl is None:
            return
        ks = self._edit_all_keyframe_times()
        if not ks:
            self.status_label.setText("还没读到关键帧，正在后台读…")
            try:
                self.clip_editor.flash("还没读到关键帧，正在后台读取…", "warn")
            except Exception:
                pass
            self._start_clip_probe_if_needed()
            return
        t = float(tl.playhead or 0.0)
        nt = min(ks, key=lambda k: abs(k - t))
        self.clip_editor.set_playhead(nt)
        self.status_label.setText(f"已贴到关键帧 {nt:.3f}s（差 {abs(nt - t):.3f}s）")
        # v187: 同「已落到关键帧」—— 这类落点回执浮层一并撤掉。

    def _edit_align_keyframes(self):
        """把片段的首尾挪到最近的关键帧(照 LosslessCut 的做法)。

        为什么值得一个按钮: 无损导出只能在关键帧下刀。起止点不在关键帧上,
        要么被 ffmpeg 悄悄挪一点(时长对不上), 要么直接判成"不能无损"。
        LosslessCut 的做法就是**自动把切点吸到关键帧**, 这里手动触发一次。

        作用对象: 选中的片段; 一个都没选就整条时间线。改的是 src_in/src_out,
        `start`(在时间线上的位置)不动 —— 片段原地微微伸长/缩短, 不挪位置。
        """
        tl = self.edit_timeline
        if tl is None:
            return
        sel = [c for c in self.clip_editor.timeline_view.selected_clips()
               if c.kind == "video"]
        targets = sel or [c for c in tl.clips if c.kind == "video"]
        if not targets:
            self.status_label.setText("时间线上还没有视频片段")
            return
        # v183: 真正干活的抽到 `_align_keyframes_for`, 右键「批量对齐」也走它
        self._align_keyframes_for(targets, "片段")

    # ------------------------------------------------------------ 截图 / 封面
    def _edit_src_time(self, clip) -> float:
        """时间线的播放头换算成这个片段源文件里的第几秒。"""
        tl = self.edit_timeline
        t = float(tl.playhead if tl is not None else 0.0)
        try:
            speed = float(clip.speed or 1.0)
            src = float(clip.src_in) + (t - float(clip.start)) * speed
            return max(float(clip.src_in), min(float(clip.src_out) or src, src))
        except Exception:
            return 0.0

    def _shot_target(self):
        """截图/封面要作用在哪个片段 + 源文件的第几秒。"""
        clip = self._edit_target_clip()
        if clip is None:
            self.status_label.setText("先把裁剪线放到一个片段上（截图取的是那一帧）")
            return None, 0.0
        return clip, self._edit_src_time(clip)

    def _shot_out_path(self, src: str, t_src: float, ext: str) -> str:
        """源文件同目录 + 「原名_时分秒毫秒.扩展名」; 重名不覆盖, 往后加 _2、_3。"""
        d = os.path.dirname(os.path.abspath(src)) or "."
        stem = os.path.splitext(os.path.basename(src))[0]
        ms = int(round(max(0.0, t_src) * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        base = f"{stem}_{h:02d}-{m:02d}-{s:02d}-{ms:03d}"
        out = os.path.join(d, base + "." + ext)
        n = 2
        while os.path.exists(out):
            out = os.path.join(d, f"{base}_{n}.{ext}")
            n += 1
        return out

    def _ffmpeg_frame_args(self, fmt: str, quality: int) -> list:
        """按格式给一组编码参数。

        JPEG 的 -q:v 是 2(最好)~31(最差), 和用户看到的 0~100 正好反过来;
        WEBP 直接用 -quality 0~100; PNG 无损, 没有质量可调。
        """
        if fmt == "png":
            return ["-c:v", "png"]
        if fmt == "webp":
            return ["-c:v", "libwebp", "-quality", str(int(quality))]
        q = int(round(31 - max(0, min(100, quality)) * 29 / 100))
        return ["-c:v", "mjpeg", "-q:v", str(max(2, min(31, q)))]

    def _clip_keyframe_srcs(self, clip, min_gap: float = 0.2) -> list:
        """片段范围里的关键帧(源时间秒)。太密的按 min_gap 抽稀, 否则一次几十张。"""
        ks = list(getattr(clip, "keyframes", None) or [])
        if not ks:
            try:
                from ..core import media_tools as MT
                ks = list(MT.probe_keyframes(clip.source, self.merge_ffprobe,
                                             self.merge_ffmpeg) or [])
                clip.keyframes = list(ks)
                self._refresh_shot_kf()
            except Exception:
                ks = []
        try:
            lo = float(clip.src_in)
            hi = float(clip.src_out) or float(getattr(clip, "src_duration", 0.0) or 0.0)
        except Exception:
            return []
        picked = []
        for k in sorted(float(x) for x in ks):
            if k < lo - 1e-6 or k > hi + 1e-6:
                continue
            if picked and k - picked[-1] < min_gap:
                continue
            picked.append(k)
        return picked

    def _shot_jobs(self, scope: str) -> list:
        """按范围算出要截哪些帧: [(clip, 源时间秒)]。

        current  = 裁剪线正对这一帧(和以前一样, 一张)
        selected = 选中的每个视频片段, 每个关键帧一张
        all      = 整条时间线上所有片段的每个关键帧各一张
        """
        if scope == "current":
            clip, t = self._shot_target()
            return [(clip, t)] if clip is not None else []
        if scope == "selected":
            clips = [c for c in self.clip_editor.timeline_view.selected_clips()
                     if c.kind == "video"]
            if not clips:
                self.status_label.setText(
                    "先选中要截图的片段（「选中」是按选中的片段来的）")
                return []
        else:
            tl = self.edit_timeline
            clips = [c for c in tl.clips if c.kind == "video"] if tl else []
        if not clips:
            self.status_label.setText("时间线上还没有视频片段")
            return []
        jobs = []
        for c in sorted(clips, key=lambda x: (x.track, x.start)):
            jobs.extend((c, t) for t in self._clip_keyframe_srcs(c))
        if not jobs:
            self.status_label.setText("这些片段还没读到关键帧，正在后台读…")
            self._start_clip_probe_if_needed()
        return jobs

    def _edit_capture_frame(self):
        """截图(捕获帧): 把画面存成图片, 放在源文件旁边。

        用 ffmpeg 抽**源文件**的那一帧(而不是截预览窗里的缩略图), 存的是原始分辨率。
        范围(current/selected/all)在工具条相机右边那个下拉里选。
        """
        scope = self.clip_editor.capture_scope()
        jobs = self._shot_jobs(scope)
        if not jobs:
            return
        if not self.merge_ffmpeg:
            self.status_label.setText("没有可用的 ffmpeg，先去设置里指定")
            return
        fmt = self.clip_editor.capture_format()
        q = self.clip_editor.capture_quality()
        many = len(jobs) > 1
        if len(jobs) > 30:
            from .dialogs import confirm_yes_no
            if not confirm_yes_no(
                    self._dlg_parent(), "批量截图",
                    "这一次会生成 %d 张图，都存在各自源文件的旁边。\n\n"
                    "继续吗？" % len(jobs), center=True, red_yes=False):
                return
        from ..merge.proc import run as run_proc
        ok, fail = 0, 0
        last_err = ""
        for i, (clip, t_src) in enumerate(jobs, 1):
            out = self._shot_out_path(clip.source, t_src, fmt)
            cmd = [self.merge_ffmpeg, "-hide_banner", "-nostdin", "-y",
                   "-ss", "%.3f" % max(0.0, float(t_src)), "-i", clip.source,
                   "-frames:v", "1"] + self._ffmpeg_frame_args(fmt, q) + [out]
            try:
                r = run_proc(cmd, timeout=90)
            except Exception as e:
                fail += 1
                last_err = str(e)
                continue
            if getattr(r, "returncode", 1) == 0 and os.path.exists(out):
                ok += 1
            else:
                fail += 1
                lines = (getattr(r, "stderr", "") or "").strip().splitlines()
                last_err = lines[-1] if lines else "ffmpeg 出错"
            if many:
                self.status_label.setText("截图 %d/%d…" % (i, len(jobs)))
                QApplication.processEvents()
        if not many and ok:
            out = self._shot_out_path(jobs[0][0].source, jobs[0][1], fmt)
            self.status_label.setText("已截图：%s（%s）"
                                      % (os.path.basename(out), fmt.upper()))
            # v152: 截图成功在预览上方浮一条绿字 —— 状态栏在最底下, 容易漏掉
            self.clip_editor.flash("已截图：%s" % os.path.basename(out))
            return
        if fail and not ok:
            self.status_label.setText("截图失败：%s" % last_err)
            return
        self.status_label.setText(
            "已截 %d 张" % ok
            + ("，%d 张失败（%s）" % (fail, last_err) if fail else "")
            + "（%s，存在各自源文件旁边）" % fmt.upper())
        self.clip_editor.flash(
            "已截 %d 张（%s）" % (ok, fmt.upper()) if not fail
            else "已截 %d 张，%d 张失败" % (ok, fail),
            "ok" if not fail else "warn")

    def _edit_set_cover(self):
        """设为封面: 把裁剪线这一帧写进这个视频文件, 当它的封面图。

        做法: 先抽一张 JPEG, 再**不重新编码**(全部 -c copy)把它作为
        attached_pic 附到视频里 —— 画质一点不变, 只是多了一条封面流。
        """
        clip, t_src = self._shot_target()
        if clip is None:
            return
        if clip.kind != "video":
            self.status_label.setText("只有视频文件才有封面")
            return
        if not self.merge_ffmpeg:
            self.status_label.setText("没有可用的 ffmpeg，先去设置里指定")
            return
        ext = os.path.splitext(clip.source)[1].lower()
        if ext not in (".mp4", ".m4v", ".mov"):
            self.status_label.setText(f"{ext or '这种格式'} 不支持写封面（只支持 mp4 / mov）")
            return
        from .dialogs import confirm_destructive
        if not confirm_destructive(
                self._dlg_parent(), "设为封面",
                f"把当前这一帧写成「{os.path.basename(clip.source)}」的封面？\n\n"
                f"· 会重写这个视频文件（原文件进回收站，可还原）\n"
                f"· 不重新编码，画质不变；只是多一条封面图\n"
                f"· 每写一次大概要花和「复制一遍这个文件」差不多的时间"):
            return
        self._set_cover_do(clip, t_src, ext)

    def _set_cover_do(self, clip, t_src: float, ext: str):
        src = clip.source
        d = os.path.dirname(os.path.abspath(src)) or "."
        stem = os.path.splitext(os.path.basename(src))[0]
        tmp_img = os.path.join(d, f".{stem}_cover.jpg")
        tmp_vid = os.path.join(d, f".{stem}_cover_tmp{ext}")
        try:
            from ..merge.proc import run as run_proc
            r1 = run_proc([self.merge_ffmpeg, "-hide_banner", "-nostdin", "-y",
                           "-ss", f"{max(0.0, t_src):.3f}", "-i", src,
                           "-frames:v", "1", "-c:v", "mjpeg", "-q:v", "2",
                           tmp_img], timeout=120)
            if getattr(r1, "returncode", 1) != 0 or not os.path.exists(tmp_img):
                self.status_label.setText("设为封面失败：抽不出这一帧")
                return
            # 只保留第一路视频 + 音频 + 字幕, 再挂上新封面(旧封面因此被换掉)
            r2 = run_proc([self.merge_ffmpeg, "-hide_banner", "-nostdin", "-y",
                           "-i", src, "-i", tmp_img,
                           "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
                           "-map", "1:v:0",
                           "-c", "copy", "-map_metadata", "0",
                           "-disposition:v:1", "attached_pic",
                           tmp_vid], timeout=1800)
            if getattr(r2, "returncode", 1) != 0 or not os.path.exists(tmp_vid):
                err = (getattr(r2, "stderr", "") or "").strip().splitlines()
                self.status_label.setText(
                    f"设为封面失败：{err[-1] if err else 'ffmpeg 出错'}")
                self._unlink(tmp_vid)
                return
        finally:
            self._unlink(tmp_img)
        # 原文件进回收站(可还原), 再把新文件顶上原来的位置
        if src not in (send_to_recycle_bin([src]) or []):
            self._unlink(tmp_vid)
            self.status_label.setText("设为封面失败：原文件挪不进回收站（可能被占用）")
            return
        try:
            os.replace(tmp_vid, src)
        except Exception as e:
            self.status_label.setText(f"设为封面失败：{e}（新文件在 {tmp_vid}）")
            return
        self.status_label.setText(f"封面已写入：{os.path.basename(src)}")
        self.clip_editor.flash("封面已写入：%s" % os.path.basename(src))
        self.clip_editor.timeline_view.update()

    @staticmethod
    def _unlink(path: str):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def show_shortcut_help(self):
        """快捷键一览: 非模态, 重复触发只把它提到前面。点键位可以改。

        ⚠️ 挂主窗口的话, 弹窗一 show 主窗口(合并界面)就跟着被激活到最前,
        剪辑窗口被压在后面 —— 用户看到的就是"点快捷键, 弹出的是合并界面"。
        """
        parent = self._modal_parent()
        dlg = self._shortcut_help
        if dlg is not None and dlg.parent() is not parent:
            # 剪辑窗口是后来才开的: 换挂到它身上(旧的那个直接丢掉重建)
            try:
                dlg.close()
                dlg.setParent(None)
                dlg.deleteLater()
            except Exception:
                pass
            dlg = None
        if dlg is None:
            dlg = ShortcutHelpDialog(load_bindings(), parent)
            dlg.bindingsChanged.connect(self._on_bindings_changed)
            self._shortcut_help = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self._restore_clip_front()

    def _on_bindings_changed(self, mapping):
        # 值是两个槽的 list, 拷一份再存(别和弹窗共享同一个 list 对象)
        self._edit_bindings = {k: as_slots(v) for k, v in (mapping or {}).items()}
        self._rebuild_edit_shortcuts()
        # 提示里写着键位的控件(截图按钮)要跟着换字
        try:
            self.clip_editor.refresh_shortcut_tips()
        except Exception:
            pass
        self.status_label.setText("快捷键已更新")

    def _kill_button_focus(self):
        """顶栏/工具条按钮一律不吃键盘焦点。

        ⚠️ 按钮留住焦点后, 空格就等于再点一次它。用户踩过: 点「剪辑」进剪辑态,
        再按空格又点了一次「剪辑」→ 直接退出。键盘操作全部交给 QShortcut。
        """
        for cls in (QPushButton, QToolButton):
            for btn in self.findChildren(cls):
                btn.setFocusPolicy(Qt.NoFocus)

    # ------------------------------------------------------------ 素材
    def _modal_parent(self):
        """弹窗该挂谁: 剪辑窗口开着就挂它(和 `_dlg_parent` 同一套口径)。

        ⚠️ 挂主窗口的话, 弹窗一 show/关, Windows 会把主窗口激活到最前 ——
        用户看到的就是"一点添加素材/改帧率, 合并界面自己跑到前面来了",
        剪辑窗口被压在后面(v128 修过一次, v146 又把工具箱那几个漏了)。
        """
        return self._dlg_parent()

    def _restore_clip_front(self):
        """弹窗/外部程序抢走焦点后, 把剪辑窗口重新提到前面。"""
        w = self._clip_window
        if self.edit_mode and w is not None and w.isVisible():
            w.raise_()
            try:
                w.activateWindow()
            except Exception:
                pass

    def pick_media(self):
        """「添加素材」: 复用添加弹窗, 选中的文件只进素材框(不上轨道)。"""
        # 弹窗会把"已经有了"的文件藏起来、并把已加全的文件夹置灰不可选。
        # v164: 只藏**素材框里还看得见**的 —— 移出过素材框的文件正是用户想
        # 加回来的那批, 以前它们也算"已有", 于是弹窗里根本选不中。
        existing = [p.path for p in self.model.photos
                    if not self._media_hidden_at(p.path)]
        existing += [p for p in sorted(self._edit_extra)
                     if not self._media_hidden_at(p)]
        dlg = PickerDialog(self._modal_parent(),
                           start_path=self.model.folder or os.path.expanduser("~"),
                           recursive=False, existing=existing)
        dlg.setMinimumSize(1000, 600)
        dlg.resize(1100, 660)
        # 弹窗里点「添加」立刻进来(弹窗不关), 关掉时再补一次勾选的
        dlg.pathsAdded.connect(self._on_media_picked)
        dlg.exec()
        rest = list(dlg.selected_files()) + list(dlg.selected_folders())
        if rest:
            self._on_media_picked(rest)
        # 弹窗一关, 焦点会回到它的父窗口; 剪辑开着就得把剪辑重新顶到前面
        self._restore_clip_front()

    def _on_media_picked(self, paths):
        """把选中的文件/文件夹里的素材放进**素材框**(不上轨道)。

        ⚠️ 用户明确要求：「添加素材」只是把文件摆进素材框，要不要上轨道由素材格
        右下角那个 ＋ 决定。以前这里直接调 `_edit_add_source`，于是"添加素材"
        进来的文件全被自动塞进了轨道。
        """
        files: list[str] = []
        for p in paths or []:
            p = os.path.normpath(str(p))
            if os.path.isdir(p):
                try:
                    for name in sorted(os.listdir(p)):
                        fp = os.path.join(p, name)
                        if os.path.isfile(fp) and clip_kind_of(fp) in (
                                "video", "image", "audio"):
                            files.append(fp)
                except OSError:
                    continue
            elif os.path.isfile(p):
                files.append(p)
        files = [f for f in dict.fromkeys(files) if self._media_add_source(f)]
        if files:
            self._refresh_media_panel()
            self._start_clip_probe_if_needed()   # 时长要读出来, 进度条才知道多长
            # v151: 素材进框了就把中间的画面填上(以前要再点一下素材格才出画面,
            # 刚添加完素材时中间一直是空的灰白框)。
            self._refresh_preview_at(self.edit_timeline.playhead)
            what = (f"已加入素材框：{os.path.basename(files[0])}" if len(files) == 1
                    else f"已加入素材框 {len(files)} 个")
            self.status_label.setText(what + "（点格子右下角的 ＋ 才上轨道）")

    def _media_add_source(self, path: str) -> bool:
        """只把素材放进素材框, 时间线一根毫毛都不动。"""
        kind = clip_kind_of(path)
        if kind not in ("video", "image", "audio"):
            return False
        self._unhide_media([path])           # 之前被移除过的话, 这次要能重新出现
        # 剪辑态是"只列合并页勾过的文件": 用户明确把它加进来, 就该算数,
        # 否则文件进了框又被这条规则挡掉(看着像"添加没反应")。
        if self._edit_allowed is not None:
            self._edit_allowed.add(self._canon(path))
        self._remember_extra(path)
        return True

    def _edit_add_source(self, path: str, track: int = TRACK_MAIN,
                         at: float | None = None) -> bool:
        """把一个素材加到时间线: 默认第 1 轨、播放头处(和别的东西撞了就自动避开)。

        v117 起**允许同一素材用多次**(剪映也这样), 所以不再因"已在轨上"拒绝。
        """
        kind = clip_kind_of(path)
        if kind not in ("video", "image", "audio"):
            return False
        # ⚠️ 这里是用户点了 ＋ / 拖进来: 时长必须**当场**读出来。以前只查后台
        # 探测的结果(常常还没回来) → dur=0 → 片段在时间线上只有一帧宽。
        dur = self._ensure_duration(path)
        if kind != "image" and dur <= 0:
            # 实在读不出来(坏文件/ffprobe 不在): 给个能看见的占位长度,
            # 后台探到真值会把它改回来, 总比一个 0 秒的片段强。
            dur = IMAGE_DEFAULT_SEC
            self._start_clip_probe_if_needed()
        clip = Clip(source=path, kind=kind, src_out=dur, src_duration=dur)
        # v194: 后台早先已探到关键帧就当场取来用, 别再等一趟(那一趟会被
        # _kf_tried 拦掉, 结果就是这个新片段一直没有红色刻度)。
        clip.keyframes = list(self._kf_cache.get(self._canon(path), []))
        start = self.edit_timeline.playhead if at is None else max(0.0, float(at))
        clip.track = max(0, min(self.edit_timeline.track_count - 1, int(track)))
        self.edit_timeline.clips.append(clip)
        self.edit_timeline.place(clip, clip.track, start)
        self._remember_extra(path)
        # 关键帧这会儿还是空的: 让后台补一趟, 补到之后 Alt+←/→ 才有得跳
        if kind == "video" and not clip.keyframes:
            self._start_clip_probe_if_needed()
        self._after_edit_changed()
        self.clip_editor.timeline_view.select_clip(clip)
        self._on_edit_clip_selected(clip)
        return True

    def _edit_plus_to_timeline(self, path: str):
        """素材格右下角 ＋: 加到播放头所在位置。"""
        if not self._edit_add_source(path):
            self.status_label.setText("这个文件不能放进时间线")
            return
        self.status_label.setText(
            f"已加入 {os.path.basename(path)}（落在播放头 {fmt_time(self.edit_timeline.playhead)}）")

    def _edit_drop_source(self, path: str, track: int, start: float):
        """从素材框拖到时间线的落点: 建片段放到指定轨、指定时间。"""
        if not self._edit_add_source(path, track=track, at=start):
            self.status_label.setText("这个文件不能放进时间线")

    def _on_media_context_menu(self, path: str, pos):
        self._media_menu(path).exec(pos)

    def _refresh_shot_kf(self):
        """关键帧刚载入 → 让截图按钮按当前裁剪线重新判定要不要变蓝(v177)。"""
        try:
            ce = getattr(self, "clip_editor", None)
            if ce is not None:
                ce.refresh_shot_keyframe_state()
        except Exception:
            pass

    def _media_menu(self, path: str) -> QMenu:
        """素材框右键菜单(单独构建: 离屏测试能直接看内容, 不用真弹窗)。"""
        menu = self._hand_menu()
        # v177: 在空白处右键(框选后还选着东西)时传进来的 path 是空串 ——
        # 用选中集合里的第一项当代表, 多选动作仍按整批 selected_paths() 来。
        if not path:
            sel0 = self.clip_editor.media.selected_paths()
            path = sel0[0] if sel0 else ""
        menu.addAction("加入时间线（落在播放头）",
                       lambda: self._edit_plus_to_timeline(path))
        menu.addSeparator()
        menu.addAction("在文件夹里显示", lambda: self._reveal_path(path))
        sel = self.clip_editor.media.selected_paths()
        if path in sel and len(sel) > 1:
            menu.addAction(f"把选中的 {len(sel)} 个移出素材框（不动磁盘文件）",
                           lambda: self._remove_media(sel))
        menu.addAction("从素材框移除（不动磁盘文件）",
                       lambda: self._remove_from_media(path))
        # v192: 右键落在选中集里就按整个选中集对比(含右键那个自己), 单选也能开窗口
        paths = list(dict.fromkeys(sel)) if sel else []
        if path and path not in paths:
            paths.append(path)
        # v182: 最后一组只放「对比」+「属性」; 「发送到对比N」属于上面那组操作
        self._add_compare_send_actions(menu, paths)
        menu.addSeparator()
        act = menu.addAction(self._compare_label(paths),
                             lambda: self._open_compare_for_paths(paths))
        if act is not None:
            act.setEnabled(bool(paths))
        # v172: 素材也能看属性(还没放进时间线时看的就是这个文件本身)
        menu.addAction("属性…", lambda: self._show_file_props(path))
        return menu

    def _show_file_props(self, path: str):
        """素材的「属性」: 三页都和「合并」页那边一个口径(文件本身)。

        素材不一定在时间线上, 所以这里不传 clip —— 「常规」页就不会有
        入点/出点那些"这一段"的参数。v174: 非模态, 不锁死剪辑操作。
        """
        from .tools_dialog import ClipPropsDialog
        if not path:
            return
        self._open_clip_props_dialog(
            lambda parent: ClipPropsDialog(parent, path=path))

    def _open_clip_props_dialog(self, build):
        """非模态打开属性窗口(可同时开几个, 依次错开), 和合并页一个做法。"""
        dlg = build(self._dlg_parent())
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.destroyed.connect(lambda *_: self._forget_clip_prop_dialog(dlg))
        self._clip_prop_dialogs.append(dlg)
        dlg.show()
        n = len(self._clip_prop_dialogs) - 1
        geo = dlg.frameGeometry()
        ref = self._clip_window if self.edit_mode and \
            self._clip_window is not None and self._clip_window.isVisible() \
            else self
        geo.moveCenter(ref.frameGeometry().center())
        geo.translate((n % 6) * 26, (n % 6) * 26)
        dlg.move(geo.topLeft())
        dlg.raise_()
        dlg.activateWindow()

    def _forget_clip_prop_dialog(self, dlg):
        try:
            self._clip_prop_dialogs.remove(dlg)
        except ValueError:
            pass

    def _remove_from_media(self, path: str):
        """只是从素材框里拿掉, 磁盘文件和时间线上已有的片段都不动。"""
        was_preview = self._media_preview == path
        if was_preview:
            self._end_media_preview()
        self._edit_extra.discard(path)
        self._hide_media([path])
        self._refresh_media_panel()
        self._after_media_gone(was_preview)
        self._edit_commit()          # 可撤销(v145)
        self.status_label.setText(f"已从素材框移除 {os.path.basename(path)}")

    # ------------------------------------------------------------ 轨道
    def _on_track_menu(self, track: int, pos):
        """轨道空白处 / 轨名区右键: 加轨、删轨、清空轨、磁吸开关。

        v133 起底部那行「＋ 添加轨道」没了, 加轨删轨全走右键(片段菜单里也有)。
        """
        self._track_menu(track).exec(pos)

    def _track_menu(self, track: int) -> QMenu:
        """轨道菜单(单独一个方法: 离屏测试能直接看内容, 不用真弹窗)。"""
        tl = self.edit_timeline
        menu = self._hand_menu()
        menu.addAction("新增一条轨道", self._add_track)
        if track >= 0:
            busy = any(c.track == track for c in tl.clips)
            act = menu.addAction(f"删除「{tl.track_label(track)}」",
                                 lambda: self._remove_track(track))
            act.setEnabled(not busy and tl.track_count > 1)
            if busy:
                act.setToolTip("这条轨上还有片段，先挪走或删掉再删轨")
            elif tl.track_count <= 1:
                act.setToolTip("至少要留一条轨道")
            # v155: 「清空」放在「删除」下面 —— 想整条不要了又懒得一段段挪的人，
            #        点这个就能把片段清光（轨还留着，删轨另点上面那条）。
            act_clear = menu.addAction(f"清空「{tl.track_label(track)}」",
                                       lambda: self._clear_track(track))
            act_clear.setEnabled(busy)
            if not busy:
                act_clear.setToolTip("这条轨上没有片段")
        menu.addSeparator()
        if tl.magnetic:
            menu.addAction("关闭磁吸（允许留空隙）", self._toggle_magnetic)
        else:
            menu.addAction("开启磁吸（首尾相接，无缝拼接）", self._toggle_magnetic)
        return menu

    def _add_track(self):
        n = self.edit_timeline.add_track()
        self.clip_editor.mag_btn.setChecked(self.edit_timeline.magnetic)
        self._after_edit_changed()
        self.status_label.setText(f"已加到 {n} 条轨道")

    def _remove_track(self, track: int):
        label = self.edit_timeline.track_label(track)
        if not self.edit_timeline.remove_track(track):
            self.status_label.setText("这条轨上还有片段，先挪走再删")
            return
        self._after_edit_changed()
        self.status_label.setText(f"已删除「{label}」")

    def _clear_track(self, track: int):
        """清空一条轨: 把这条轨上的片段全删掉(可撤销), 轨道本身留着。

        v155: 「删除轨」要求先把片段挪走, 想"这一条不要了"的人得先一段段删 ——
        多一个「清空」就不用绕这一圈。
        """
        from .dialogs import confirm_destructive
        tl = self.edit_timeline
        clips = [c for c in tl.clips if c.track == track]
        if not clips:
            self.status_label.setText("这条轨上没有片段")
            return
        label = tl.track_label(track)
        names = "、".join(c.name for c in clips[:3])
        if len(clips) > 3:
            names += f" 等 {len(clips)} 段"
        if not confirm_destructive(
                self._dlg_parent(), "清空轨道",
                f"把「{label}」上的 {len(clips)} 段全删掉？<br>"
                f"<span style='color:{T.TEXT_WEAK}'>{names}</span><br>"
                "只从时间线上拿掉，磁盘上的文件不动（可以 Ctrl+Z 撤销）。"):
            self._restore_clip_front()
            return
        self._restore_clip_front()
        for c in list(clips):
            tl.remove(c)
        self._after_edit_changed()
        self.status_label.setText(f"已清空「{label}」（{len(clips)} 段）")

    def _toggle_magnetic(self):
        tl = self.edit_timeline
        tl.magnetic = not tl.magnetic
        if tl.magnetic:
            for t in range(tl.track_count):
                tl.compact(t)
        self.clip_editor.mag_btn.setChecked(tl.magnetic)
        self._after_edit_changed()
        self.status_label.setText("磁吸已开：同轨首尾相接" if tl.magnetic
                                  else "磁吸已关：可以留空隙")

    def _remember_extra(self, path: str):
        """不在主列表里的素材单独记一份, 免得刷新素材框时丢掉。"""
        if not any(p.path == path for p in self.model.photos):
            self._edit_extra.add(path)

    def _canon(self, path: str) -> str:
        """路径的"统一写法"。

        ⚠️ 同一个文件在程序里会出现两套写法: 主列表扫出来的是 `C:\\a\\b.mp4`,
        添加弹窗/Qt 给的是 `C:/a/b.mp4`。字典按原始字符串查就会"明明探过却查不
        到" —— 时长一直是 0, 进度条因此永远不显示。查和存都用这一份写法。
        """
        try:
            return os.path.normcase(os.path.normpath(str(path)))
        except Exception:
            return str(path or "")

    # ---------------------------------------------------- 素材框"已移出"的那批
    # ⚠️ 一律按统一写法存/查(v164): 移出时记的是主列表那份写法, 而"添加素材"
    # 弹窗回来的是另一套分隔符 —— 直接比字符串会"移出了却没记住/添加了还是
    # 不显示"。
    def _hide_media(self, paths):
        for p in (paths or []):
            if p:
                self._media_hidden.add(self._canon(p))

    def _unhide_media(self, paths):
        for p in (paths or []):
            if p:
                self._media_hidden.discard(self._canon(p))

    def _media_hidden_at(self, path: str) -> bool:
        return bool(path) and self._canon(path) in self._media_hidden

    def _spec_of(self, path: str):
        """按统一写法找全规格(同一文件可能用两套分隔符各存了一份)。"""
        spec = self.merge_specs.get(path)
        if spec is not None:
            return spec
        n = len(self.merge_specs)
        if not self._spec_index or self._spec_index_n != n:
            idx = {}
            for k, v in self.merge_specs.items():
                idx.setdefault(self._canon(k), v)
            self._spec_index, self._spec_index_n = idx, n
        return self._spec_index.get(self._canon(path))

    def _known_duration(self, path: str) -> float:
        """已探测过就直接用(全规格优先, 其次同步读到的缓存), 都没有才是 0。"""
        spec = self._spec_of(path)
        try:
            d = float(spec.get("duration") or 0.0) if spec else 0.0
        except Exception:
            d = 0.0
        if d > 0:
            return d
        try:
            return float(self._dur_cache.get(self._canon(path)) or 0.0)
        except Exception:
            return 0.0

    def _ensure_duration(self, path: str) -> float:
        """用户**手动**要用到时长时调: 没读到就当场读一次(几十毫秒)。

        为什么不能只靠后台探测: 后台那批是"有空再探", 常常还没回来用户就已经
        点了 ＋ —— 于是 `_edit_add_source` 拿 0 去建片段, 时间线上就是一帧宽,
        等探回来才变长。上轨/看预览都是一次性的手动操作, 同步读一次完全值得。
        """
        d = self._known_duration(path)
        if d > 0 or not path or not os.path.isfile(path):
            return d
        if clip_kind_of(path) == "image":
            return 0.0                       # 图片没有"时长", 走 hold
        if self._canon(path) in self._dur_miss:
            return 0.0                       # 之前读不出来: 别每次都卡一下
        ffprobe = self.merge_ffprobe
        if not ffprobe:
            try:
                from ..merge.ffmpeg_locate import find_ffmpeg_pair
                pair = find_ffmpeg_pair()
                ffprobe = pair[1] if pair else None
                if pair and not self.merge_ffmpeg:
                    self.merge_ffmpeg, self.merge_ffprobe = pair[0], pair[1]
            except Exception:
                ffprobe = None
        if not ffprobe:
            return 0.0
        try:
            from ..merge.probe import probe_duration
            d = float(probe_duration(path, ffprobe) or 0.0)
        except Exception:
            d = 0.0
        if d > 0:
            self._dur_cache[self._canon(path)] = d
            self._dur_miss.discard(self._canon(path))
        else:
            self._dur_miss.add(self._canon(path))
        return d

    def _after_edit_changed(self):
        """时间线结构变了: 重绘时间线 / 刷新素材框与顶栏, 缺的时长后台补。"""
        if self.clip_editor.is_playing():
            self._edit_pause()          # 结构变了, 继续播会对不上
        # 片段被删/裁短之后, 裁剪线可能落在时间线之外了 —— 不夹回来, 时间读数会
        # 出现「00:20.0 / 00:12.0」这种比自己总长还大的数, 预览也永远抓不到帧。
        try:
            _tl = self.edit_timeline
            if _tl is not None:
                _tl.playhead = max(0.0, min(_tl.playhead, _tl.total_duration()))
        except Exception:
            pass
        self._sync_scrub()            # 轨道长短变了: 进度条范围/位置跟着重算(v159)
        self.clip_editor.timeline_view.refresh()
        self.clip_editor._refresh_time_label()
        self._refresh_media_panel()
        self._refresh_clip_list()
        self._refresh_top_status()
        self._start_clip_probe_if_needed()
        self._refresh_lossless()      # 结构一变, "能不能无损"的结论就要重算
        # 选中的片段可能被删掉了 → 工具按钮该跟着灰/亮
        self._sync_edit_tool_buttons()
        if self.clip_editor.wave_btn.isChecked():
            self._load_waveforms()    # 波形开着: 新加进来的素材也要读一遍
        self._edit_commit()           # 记一步, Ctrl+Z 能退回来
        # 结构变了, 预览也可能是空的了(比如刚删掉唯一那个片段)
        if self.edit_mode and not self.clip_editor.is_playing():
            self._refresh_preview_at(self.edit_timeline.playhead)

    def _refresh_media_panel(self):
        """素材框 = 主列表里能剪的文件(视频/图片) + 剪辑态单独送进来的。

        ⚠️ 只列「合并」页勾选过的(v140): 没勾的文件连素材框都不出现 ——
        用户的意思很明确"我在合并页勾了哪几个, 剪辑里就只该看到哪几个"。
        一个都没勾就是空框(用户要的就是这个, 别自作聪明显示全部)。
        剪辑里现加进来的(`_edit_extra`)不受这条限制。
        """
        rows = []
        used = {c.source for c in self.edit_timeline.clips}
        label_of = {"video": "视频", "image": "图片", "audio": "音频"}
        for p in self.model.photos:
            kind = getattr(p, "kind", "")
            if kind not in ("video", "image"):
                continue
            if self._media_hidden_at(p.path):
                continue
            if self._edit_allowed is not None and self._canon(p.path) not in self._edit_allowed:
                continue
            rows.append((p.path, label_of.get(kind, ""), p.path in used,
                         getattr(p, "thumb", None)))
        for path in sorted(self._edit_extra):
            if not os.path.isfile(path) or self._media_hidden_at(path):
                continue
            rows.append((path, label_of.get(clip_kind_of(path), "素材"),
                         path in used, None))
        self.clip_editor.media.set_sources(rows)
        # 没缩略图的(比如从没在主列表露过面的视频, 或剪辑态现加进来的) → 现去生成,
        # 不然素材框就是一排灰格子。生成好会回填到对应那一项(不重建整表, 多选不丢)。
        missing = self.clip_editor.media.missing_thumbs()
        if missing and self.thumb_worker is not None:
            self.thumb_worker.request(missing, front=True)

    # ------------------------------------------------------------ 片段操作
    def edit_split(self):
        """在裁剪线处切开选中的片段(切的是源时间, 不重新编码)。

        v155: Ctrl 点选了好几段时, **每一段**都在裁剪线处切一刀(只切真的
        跨住裁剪线的那几段, 其余原样留着)。
        """
        clips = self.clip_editor.timeline_view.selected_clips()
        if not clips:
            clip = self.clip_editor.selected_clip()
            clips = [clip] if clip is not None else []
        if not clips:
            return
        self._split_clips_at(clips)

    def edit_delete(self):
        """Delete: 删掉选中的**全部**片段(Ctrl 可以选多个)。

        删什么看"最后点的是哪一块"(`_edit_area`), 不看焦点:
        ⚠️ 右栏片段清单是 NoFocus(空格要留给播放), 点它不会把焦点从素材框抢走 ——
           以前按"焦点在素材框"分流, 于是**在右栏按 Del 删掉的是素材框里那个早先
           选中的素材**, 片段一动不动; 而素材移出框不进撤销栈 ⇒ 再按 Ctrl+Z 退的是
           更早的"加片段", 看着就像"按键失灵、片段反而全没了"(v145 复现过)。
        """
        media = self.clip_editor.media
        if self._edit_area == "media":
            paths = media.selected_paths()
            if paths:
                self._remove_media(paths)
                return
        clips = self.clip_editor.timeline_view.selected_clips()
        if not clips:
            clip = self.clip_editor.selected_clip()
            clips = [clip] if clip is not None else []
        if not clips:
            # 片段一个都没选中, 但素材框里确实选着东西 → 那就删素材(兜底, 不吞按键)
            paths = media.selected_paths() if media.list.hasFocus() else []
            if paths:
                self._remove_media(paths)
            return
        for clip in clips:
            self.edit_timeline.remove(clip)
        self._after_edit_changed()
        self.clip_editor.show_clip(None)
        self.clip_panel.set_clip(None, self.edit_timeline,
                                 {p.path: p for p in self.model.photos})
        self.status_label.setText(
            f"已删除片段 {clips[0].name}" if len(clips) == 1
            else f"已删除 {len(clips)} 个片段")

    # ------------------------------------------------------------ 素材框
    def _remove_media(self, paths):
        """把素材移出素材框(只是不显示, 磁盘上的文件一个都不动)。"""
        paths = [p for p in (paths or []) if p]
        if not paths:
            return
        was_preview = self._media_preview in paths
        if was_preview:
            self._end_media_preview()       # 正在预览的素材被移走了
        self._hide_media(paths)
        self._edit_extra.difference_update(paths)
        self._refresh_media_panel()
        self._after_media_gone(was_preview)
        # 移出素材框也是一次编辑: 记进撤销栈, Ctrl+Z 能把素材放回来(v145)
        self._edit_commit()
        self.status_label.setText(
            f"已把 {os.path.basename(paths[0])} 移出素材框" if len(paths) == 1
            else f"已把 {len(paths)} 个素材移出素材框（文件都还在）")

    def _after_media_gone(self, was_preview: bool):
        """素材被移出框后, 中间那块要跟着重画。

        v164 修的 bug: 素材和片段全删光之后, 画面还挂着最后那张素材图 ——
        以前只有"切换片段"才重画预览, 移素材这条路上没人负责清屏。
        """
        if not self.edit_mode or self.clip_editor.is_playing():
            return
        tl = self.edit_timeline
        try:
            left = bool(self.clip_editor.media.first_path())
        except Exception:
            left = True
        if (tl is None or not tl.clips) and not left:
            self.clip_editor.show_clip(None)      # 什么都没有了: 清回提示文字
            self.clip_editor.hide_scrubber()
            return
        if was_preview:
            self._refresh_preview_at(tl.playhead if tl is not None else 0.0)

    # ------------------------------------------------------------ 预览
    def _on_playhead_moved(self, t: float):
        """裁剪线动了: 中间预览立刻跟到那一秒。

        停着的时候 → 抓这一帧显示; **播着的时候 → 从那里接着播**(以前这里直接
        return, 于是"裁剪线挪了、画面还在老地方", 只有先暂停再点一次才跟得上)。
        """
        if not self.edit_mode:
            return
        # v188: 裁剪线换到别的片段上 → 帧率按钮 / 音量条的信息跟着换。
        # 正在拖音量滑块时别插一手, 不然会把拖到一半的数字刷回去。
        try:
            if not self.clip_editor.vol_slider.isSliderDown():
                self._sync_edit_tool_buttons()
        except Exception:
            pass
        if self._media_preview:
            self._end_media_preview()   # 动裁剪线 = 回到时间线预览
        t = float(t or 0.0)
        tl = self.edit_timeline
        if tl is not None:
            t = tl.playhead
        # 停着的时候拖裁剪线 = 用户正在时间线上找位置: 中间下方那条要按
        # **整条轨道**摆出来(v161)。以前只有播放时才出现, 拖裁剪线反而没有
        # 进度可看 —— 用户明确要求"通过轨道裁剪线选位置时也要有进度条"。
        # ⚠️ 拖"某一段"的进度条也会走到这里(它会改 playhead): 那种情况不许
        #    把范围换成整条, 否则手柄会突然跳一下、范围也变了。
        if (not self.clip_editor.is_playing() and not self._tl_seeking
                and not self._clip_seeking):
            has_tl = tl is not None and bool(getattr(tl, "clips", None))
            if self._scrub_on and has_tl:
                if not self._tl_scrub:
                    self._begin_timeline_scrub()
                # 已经在显示: 下面那句 _sync_scrub 会把位置摆过去
            elif self._tl_scrub:
                self._end_timeline_scrub()
        if self.clip_editor.is_playing() and not self._media_playing:
            self._seek_while_playing()
            return
        self._sync_scrub()              # 进度条跟着播放头(v141/v159)
        self._refresh_preview_at(t)

    def _seek_while_playing(self):
        """播放中把裁剪线拖到别处: 画面先跟过去, 停稳 140ms 再从那里接着播。

        为什么不在每次移动就重启 ffmpeg: 拖裁剪线一秒能发几十个信号, 每次都重启
        抽帧进程会卡死(而且会不停打断)。所以拖动期间用抓帧顶着画面, 手停下来
        再真正从新位置开播。
        """
        if not self._seek_playing:
            # 第一次: 先把正在喂帧的播放器换掉(非阻塞, 旧线程交给回收站),
            # 否则它会一直把旧位置的帧盖回来。
            p = self._player
            self._player = None
            self._playing_clip = None
            try:
                p.stop()                # 立刻掐掉 ffmpeg(非阻塞), 别让它继续吐旧帧
            except Exception:
                pass
            self._retire_thread(p)      # 线程没真停前只能回收, 不能删
            self._seek_playing = True
        self._play_timer.stop()
        # 用户正在拖裁剪线/找位置: 播放头这时候**不能被自己推进**(v156)
        self._smooth_timer.stop()
        self._refresh_preview_at(self.edit_timeline.playhead
                                 if self.edit_timeline else 0.0)
        self._seek_timer.start()

    def _on_seek_settled(self):
        """拖动停稳了: 从裁剪线现在的位置接着播。"""
        if not self._seek_playing:
            return
        self._seek_playing = False
        if not self.edit_mode or not self.clip_editor.is_playing():
            return
        self._clock_t = time.perf_counter()
        self._smooth_timer.start()
        self._play_segment()

    def _refresh_preview_at(self, t: float):
        """按时间线时间 t 决定预览窗显示什么。

        优先级: 素材框里选中的素材 → 裁剪线所在片段 → 轨道上唯一素材 → 空提示。
        """
        if self._media_preview:
            return                # 用户正在看素材框里选中的那个, 别抢他的画面
        tl = self.edit_timeline
        if tl is None or not tl.clips:
            # v151: 轨道还空着的时候, 中间不该是一块灰白空框 —— 用户点「剪辑」
            # 或第一次添加素材进来, 看到的就是"预览是黑白的/没画面"。素材框里
            # 有素材就把第一个的画面直接显示出来(视频抓第一帧, 图片读原图)。
            p = ""
            try:
                p = self.clip_editor.media.first_path()
            except Exception:
                p = ""
            if p:
                self._show_media_frame_at(0.0, p)
            else:
                self.clip_editor.show_clip(None)
            return
        clip = tl.top_clip_at(t)
        # 轨道上只有一个素材: 不管裁剪线在哪, 预览就显示它(用户要的)
        if clip is None and len(tl.clips) == 1:
            clip = tl.clips[0]
        if clip is None:
            self.clip_editor.show_clip(
                None, "裁剪线这里没有画面\n把它移到片段上就能看到那一秒")
            return
        if clip.kind != "video" or not self.merge_ffmpeg:
            self.clip_editor.show_clip(clip)     # 图片/音频用缩略图(或类型文字)
            return
        speed = clip.speed or 1.0
        src_t = clip.src_in + (t - clip.start) * speed
        src_t = max(clip.src_in, min(clip.src_out, src_t))
        self._grab_frame(clip.source, src_t)

    def _grabber(self):
        """常驻的抓帧线程(第一次用的时候才建)。

        ⚠️ 以前是"每抓一帧 new 一个 QThread", 缩放/拖裁剪线时一秒几十次:
        线程对象疯狂创建销毁, 而且旧线程的 finished 迟到时会去 deleteLater
        **新的**那个 —— 删一个还在跑的 QThread, Qt 内部直接 abort
        (Windows 上就是 0xc0000409 闪退)。现在线程一直在, 只换任务。
        """
        g = self._grab_worker
        if g is not None:
            g.set_ffmpeg(self.merge_ffmpeg or "")
            return g
        try:
            from .player import FrameGrabber
            g = FrameGrabber(self.merge_ffmpeg or "", parent=self)
        except Exception:
            return None
        g.frameReady.connect(self._on_grab_frame)
        g.start()
        self._grab_worker = g
        return g

    def _preview_out_size(self, spec: dict, cap: int) -> tuple[int, int]:
        """预览要多大的一帧: 按预览窗的实际像素宽(乘缩放比)算, 只缩不放。

        v150: 以前一律按 640 宽出图, 预览窗往往比这宽得多 —— 一放大就成了
        "预览图很模糊"。现在窗口多大就要多大, 高分屏把缩放比也算进去。
        """
        w = 0
        try:
            pv = self.clip_editor.preview
            w = int(pv.width() * float(pv.devicePixelRatioF() or 1.0))
        except Exception:
            w = 0
        return out_size(int(spec.get("width") or 0),
                        int(spec.get("height") or 0), min(cap, w) if w > 0 else 0)

    def _grab_frame(self, src: str, t_src: float):
        """抓一帧(后台)。线程只留最新位置, 抓完再抓 —— 天然节流。"""
        if not self.merge_ffmpeg or not src:
            return
        t = max(0.0, float(t_src))
        self._pending_grab = (src, t)
        spec = self.merge_specs.get(src) or {}
        w, h = self._preview_out_size(spec, PREVIEW_MAX_W)

        g = self._grabber()
        if g is None:
            return
        try:
            g.request(src, t, w, h)
        except Exception:
            pass

    def _on_grab_frame(self, img):
        """播放中不抢画面(播放器自己在喂帧); 但"正在找位置"时要抢, 否则画面不跟手。"""
        if not self.clip_editor.is_playing() or self._seek_playing:
            self.clip_editor.show_still(img)

    def _start_grab(self):
        """兼容旧调用: 把记下的位置立刻交给抓帧线程。"""
        job = self._pending_grab
        if job is None or not self.edit_mode:
            return
        self._grab_frame(job[0], job[1])

    # ------------------------------------------------------------ 素材框预览
    def _preview_media(self, path: str):
        """点中素材框里的一个素材(没点 ＋): 中间预览就显示它, 也能直接播。

        不动时间线、不占轨道; 点素材框空白处或点时间线, 就回到裁剪线那一秒。
        """
        path = path or ""
        if not self.edit_mode or not path:
            return
        if self.clip_editor.is_playing():
            self._edit_pause()
        # v145: 左素材 / 右片段 互斥 —— 选中素材就把片段那一侧的选中撤掉
        self._clear_clip_selection()
        # 看素材 = 离开"播整条轨道"那个语境: 整体进度条的记忆也清掉,
        # 免得回头点一下时间线它又冒出来(v159)
        self._end_timeline_scrub()
        self._media_preview = path
        self._media_playing = False
        self._media_pos = 0.0
        # 用户是**手动**点开这个素材的: 时长当场读一次, 进度条立刻就能用
        # (以前只查后台结果, 后台还没回来就是 0 → 进度条干脆不显示)。
        self._media_dur = self._ensure_duration(path)
        self._media_clock = 0.0
        # 有长度的(视频/音频)才浮出进度条: 可以拖着看整条素材
        if self._media_dur > 0 and self._scrub_on:
            self.clip_editor.show_scrubber(self._media_dur)
            self.clip_editor.set_scrub_pos(0.0)
        else:
            self.clip_editor.hide_scrubber()
            # 时长还没读出来(刚"添加素材"进来的文件就是这样) → 催一次后台探测,
            # 读回来在 _on_clip_probe_one 里补进度条。
            if clip_kind_of(path) != "image":
                self._start_clip_probe_if_needed()
        self._show_media_frame(path)

    def _end_media_preview(self):
        """退出"看素材框里的素材"这个状态: 收进度条、停声音、清标记。"""
        self.clip_editor.hide_scrubber()
        self._media_ui_timer.stop()
        self._media_preview = None
        self._media_playing = False
        self._media_pos = 0.0
        self._media_dur = 0.0
        self._stop_audio()

    def _clear_media_preview(self):
        """点空白处: 预览回到裁剪线所在的那一秒。"""
        if not self._media_preview:
            self.clip_editor.hide_scrubber()
            return
        self._end_media_preview()
        if self.edit_mode and not self.clip_editor.is_playing():
            self._refresh_preview_at(self.edit_timeline.playhead)
            # 回到时间线就等于"在看整条轨道": 进度条按总长摆出来(v161)
            tl = self.edit_timeline
            if self._scrub_on and tl is not None and tl.clips:
                self._begin_timeline_scrub()

    def _scrub_value(self, t: float) -> float:
        """进度条给的秒数 → 夹到当前范围里(素材时长 / 片段长度 / 总长)。"""
        t = max(0.0, float(t or 0.0))
        scope = self._scrub_scope()
        if scope == "media" and self._media_dur > 0:
            return min(t, self._media_dur)
        return t

    def _on_media_seek_live(self, t: float):
        """**拖进度条的过程中**: 把画面实时换到这一秒(v161)。

        以前只在按下和松手发信号 —— 用户按住拖的时候画面一动不动, 松手才跳,
        看着像"进度条拖不动"。这里不重启播放器: 画面交给常驻抓帧线程,
        拖得再快也只是不断替换"最新要抓的那一帧"。
        """
        if not self.edit_mode:
            return
        scope = self._scrub_scope()
        # 整条轨道 / 某一段: 走原来的定位路 —— 播放中会掐掉播放器、改由抓帧顶着
        # (见 _seek_while_playing), 停着就是抓这一帧; 两条路都不重启 ffmpeg 播放。
        if scope == "timeline":
            self._seek_timeline_scrub(t)
            return
        if scope == "clip":
            self._seek_clip_scrub(t)
            return
        path = self._media_preview
        if not path:
            return
        t = self._scrub_value(t)
        self._media_pos = t
        self.clip_editor.set_scrub_pos(t)
        if self._media_playing:
            # 只停一次: 播放线程还在送旧帧的话, 抓回来的新帧马上被它盖掉。
            # 松手在 _on_media_seek 里从新位置续播。
            self._media_seek_paused = True
            self._stop_player()
            self._stop_audio()
            self._media_ui_timer.stop()
            self._media_playing = False
            self.clip_editor.set_playing(False)
        self._show_media_frame_at(t)

    def _on_media_seek(self, t: float):
        """进度条**松手/点一下**: 定到这一秒, 该续播的续播。"""
        if not self.edit_mode:
            return
        scope = self._scrub_scope()
        # 播整条轨道时: 进度条是"所有轨道"的, 拖它 = 定位整条时间轴(v159)
        if scope == "timeline":
            self._seek_timeline_scrub(t)
            return
        # 正看着"这一段"(点过右栏卡片): 进度条是**这一段的**, 0 = 片段开头
        if scope == "clip":
            self._seek_clip_scrub(t)
            return
        path = self._media_preview
        if not path:
            return
        t = self._scrub_value(t)
        self._media_pos = t
        self.clip_editor.set_scrub_pos(t)
        self._media_clock = time.time()
        if self._media_playing:
            self._edit_pause()                  # 先整个停下, 再从新位置起播
            self._play_media_preview(t)
        elif self._media_seek_paused:
            # 拖的时候是我们自己停的: 松手接着播(用户刚才是"在播"的状态)
            self._media_seek_paused = False
            self._play_media_preview(t)
        else:
            self._show_media_frame_at(t)

    def _on_scrub_toggled(self, on: bool):
        """「进度条」开关: 关掉就是干净画面; 开回来按当前语境立刻出现。"""
        self._scrub_on = bool(on)
        try:
            from ..merge import settings as _st
            _st.save_scrub_on(self._scrub_on)
        except Exception:
            pass
        if not self._scrub_on:
            try:
                self.clip_editor.hide_scrubber()
            except Exception:
                pass
            return
        if not self.edit_mode:
            return
        # 开回来: 照当前在看什么摆一条(素材 / 某一段 / 整条轨道)
        if self._media_preview:
            if self._media_dur > 0:
                self.clip_editor.show_scrubber(self._media_dur)
                self.clip_editor.set_scrub_pos(self._media_pos)
            return
        tl = self.edit_timeline
        if tl is None or not tl.clips:
            return
        if self._clip_scrub is not None and self._clip_scrub in tl.clips:
            self._begin_clip_scrub(self._clip_scrub)
        else:
            self._begin_timeline_scrub()
            self._sync_scrub()

    def _on_media_ui_tick(self):
        """素材预览播着的时候: 进度条按真实耗时往前走(到头就停)。"""
        if not self._media_playing:
            self._media_ui_timer.stop()
            return
        now = time.time()
        dt = now - self._media_clock
        self._media_clock = now
        if dt <= 0 or dt > 2.0:
            dt = 0.1                            # 卡顿/后台切回来: 别一下跳一大截
        self._media_pos += dt
        if self._media_dur > 0 and self._media_pos >= self._media_dur:
            # 自然播完: 回到开头(再点播放就是从头来一遍)
            self._media_pos = 0.0
            self.clip_editor.set_scrub_pos(0.0)
            self._media_ui_timer.stop()
            self._edit_pause()
            return
        self.clip_editor.set_scrub_pos(self._media_pos)

    def _media_clip(self, path: str):
        """给"素材预览"临时造一个片段对象(只为复用 show_clip 的显示逻辑)。"""
        try:
            dur = self._known_duration(path)
            return Clip(source=path, kind=clip_kind_of(path), src_in=0.0,
                        src_out=dur, src_duration=dur)
        except Exception:
            return None

    def _show_media_frame(self, path: str):
        """素材预览: 视频抓第一帧, 图片/音频用缩略图或文字。"""
        self._show_media_frame_at(0.0, path)

    def _show_media_frame_at(self, t: float, path: str = ""):
        """素材预览: 抓第 t 秒那一帧(拖进度条时用)。"""
        path = path or self._media_preview or ""
        if not path:
            return
        kind = clip_kind_of(path)
        if kind == "video" and self.merge_ffmpeg:
            self._grab_frame(path, max(0.0, float(t or 0.0)))
            return
        clip = self._media_clip(path)
        if clip is None:
            self.clip_editor.show_clip(None, os.path.basename(path))
            return
        self.clip_editor.show_clip(clip)

    def _play_media_preview(self, start: float = 0.0):
        """播素材框里选中的那个素材(从 start 秒开始, 不动时间线)。"""
        path = self._media_preview
        if not path:
            return
        if self.clip_editor.is_playing():
            self._edit_pause()
            return
        kind = clip_kind_of(path)
        start = max(0.0, float(start or 0.0))
        dur = self._media_dur or self._known_duration(path)
        self._media_dur = dur
        if dur <= 0:
            dur = 24 * 3600.0                # 时长还没读出来: 让它播到文件结束
        if start >= dur - 0.05:
            start = 0.0
        self._media_pos = start
        self._media_clock = time.time()
        # 播之前先把进度条摆出来(时长是刚读到的, 之前可能还没显示)
        if self._media_dur > 0:
            self.clip_editor.show_scrubber(self._media_dur)
            self.clip_editor.set_scrub_pos(start)
        if kind == "image":
            self._show_media_frame_at(start, path)   # 图片没得播, 显示样子就够
            return
        if kind != "video" or not self.merge_ffmpeg:
            # 纯音频: 没有画面, 但有声音(进度条照样走)
            self._show_media_frame_at(start, path)
            if not self.merge_ffmpeg:
                return
            self._media_playing = True
            self.clip_editor.set_playing(True)
            self._play_sound([(path, start, dur)])
            self._media_ui_timer.start()
            return
        spec = self._spec_of(path) or {}
        w, h = self._preview_out_size(spec, PLAY_MAX_W)
        try:
            p = FramePlayer(self.merge_ffmpeg, self)
            self._player_gen += 1
            p._gen = self._player_gen
            p.frameReady.connect(self._on_player_frame)
            p.ended.connect(self._on_player_ended)
            p.failed.connect(self._on_player_failed)
            self._player = p
            self._media_playing = True
            self.clip_editor.set_playing(True)
            self._play_sound([(path, start, dur)])
            self._media_ui_timer.start()
            p.play(path, start, dur, w, h)
        except Exception as e:
            self._media_playing = False
            self._media_ui_timer.stop()
            self.clip_editor.set_playing(False)
            self.status_label.setText(f"播放失败：{e}")

    def edit_clear_selection(self):
        # Esc 在剪辑窗口全屏时先退全屏(v155): 全屏后找不到"退出"的地方,
        # 一条 Esc 就能回来比让人去猜 F11 更省事。
        if self._clip_fullscreen_exit():
            return
        self.clip_editor.timeline_view.select_clip(None)
        self.clip_panel.set_clip(None, self.edit_timeline,
                                 {p.path: p for p in self.model.photos})
        # 取消选中只是"不选了", 预览还该显示裁剪线那一刻(别清成空)
        if self.edit_mode and not self.clip_editor.is_playing():
            self._refresh_preview_at(self.edit_timeline.playhead)
        else:
            self.clip_editor.show_clip(None)

    def _build_edit_timeline(self):
        """建一条空时间线(3 条通用轨)。

        v117 起**不再把列表里的文件自动排上轨**: 用户要的是"文件先进素材框,
        点格子右下角 ＋ 才进时间线"。已经摆过的现场要保留(切走再回来还在),
        所以这里只在时间线还是空的时候才重建。
        """
        if self.edit_timeline is not None and self.edit_timeline.clips:
            self.clip_editor.set_timeline(self.edit_timeline)
            return
        tl = Timeline(3)
        self.edit_timeline = tl
        self.clip_editor.set_timeline(tl)

    def _start_clip_probe_if_needed(self):
        """时长为 0 的视频/音频 → 后台读时长, 回来后补上。

        来源有两处, 都要补:
          - 时间线里的片段(不然没法裁、也没法判无损);
          - **素材框里刚加进来的素材**(v128: 以前只探时间线, 于是"添加素材"进来的
            文件时长一直是 0 —— 预览下沿的进度条因为 dur<=0 干脆不显示)。
        """
        if self._clip_probe_worker is not None:
            return
        # v194: 先把已探到的关键帧回填给还没数据的片段(后加进来的那些)。
        # 这些路径已被 _kf_tried 标记、不会再重探, 不回填就永远画不出红色刻度。
        filled = False
        for c in self.edit_timeline.clips:
            if c.kind == "video" and not c.keyframes:
                ks = self._kf_cache.get(self._canon(c.source))
                if ks:
                    c.keyframes = list(ks)
                    filled = True
        if filled:
            try:
                self.clip_editor.timeline_view.update()
            except AttributeError:
                pass
        cand = [c.source for c in self.edit_timeline.clips
                if c.kind != "image" and c.duration <= 0]
        # 关键帧单独一批: 主列表里探过的素材时长早就有了, 但**关键帧没读过**
        # (probe_ex 不返回它) —— 不补这一趟, Alt+←/→ 永远提示"还没读到关键帧"。
        kf_need = [c.source for c in self.edit_timeline.clips
                   if c.kind == "video" and not c.keyframes]
        for p in self.model.photos:
            if getattr(p, "kind", "") == "video":
                cand.append(p.path)
        cand.extend(sorted(self._edit_extra))
        pending: list[str] = []
        for p in cand:
            if self._canon(p) in self._probe_tried or self._media_hidden_at(p):
                continue
            if not os.path.isfile(p) or clip_kind_of(p) == "image":
                continue
            if self._known_duration(p) > 0:
                continue                    # 已经知道多长了, 不用再探
            if p not in pending:
                pending.append(p)
        for p in kf_need:
            cp = self._canon(p)
            if cp in self._kf_tried or self._media_hidden_at(p):
                continue
            if not os.path.isfile(p):
                continue
            if p not in pending:
                pending.append(p)
        if not pending:
            return
        # 探过的别再探(探不出来也不会反复起线程)
        self._probe_tried.update(self._canon(p) for p in pending)
        self._kf_tried.update(self._canon(p) for p in pending)
        try:
            from ..merge.workers import ClipProbeWorker
            w = ClipProbeWorker(pending, self)
            w.one.connect(self._on_clip_probe_one)
            w.finished_all.connect(self._on_clip_probe_done)
            self._clip_probe_worker = w
            w.start()
        except Exception:
            self._clip_probe_worker = None

    def _stop_clip_probe(self):
        w = self._clip_probe_worker
        self._clip_probe_worker = None
        self._stop_thread(w, 2000)

    def _refresh_edit_durations(self):
        """把已探测到的时长回填进时间线片段(只填还是 0 的, 不覆盖用户裁剪值)。"""
        changed = False
        for clip in self.edit_timeline.clips:
            if clip.kind == "image" or clip.src_out > 0:
                continue
            spec = self.merge_specs.get(clip.source)
            if not spec:
                continue
            try:
                dur = float(spec.get("duration") or 0.0)
            except Exception:
                dur = 0.0
            if dur > 0:
                clip.src_out = dur
                changed = True
        if changed:
            self.clip_editor.timeline_view.update()
            self.clip_editor._refresh_time_label()
            self._refresh_top_status()

    def _on_clip_probe_one(self, path: str, info):
        """回填时长 + 关键帧 + 全规格(后两者决定能不能无损下刀/拼接)。"""
        info = info or {}
        try:
            dur = float(info.get("duration") or 0.0)
        except Exception:
            dur = 0.0
        kfs = list(info.get("keyframes") or [])
        # v194: 结果先按路径存一份。片段常常是在探测回来**之后**才拖进时间线,
        # 那时结果没处写, 路径又被 _kf_tried 拦住不再重探 —— 这就是「后加进来
        # 的片段没有关键帧」的根因。缓存让之后新建的片段能直接取到。
        if kfs:
            self._kf_cache[self._canon(path)] = list(kfs)
        spec = info.get("spec")
        if spec:
            # 剪辑态现加进来的素材在 merge_specs 里没有记录, 判定要用它
            self.merge_specs[path] = spec
        if dur > 0:
            # ⚠️ 以前只有拿到**全规格**才记时长, 结果 probe_ex 失败(或只探到时长)
            # 的那批素材时长永远停在 0 —— 素材框/进度条一看 dur=0 就不显示。
            # 时长单独存一份(不掺进 merge_specs, 那东西是拿来判无损的),
            # _known_duration 查得到就行。
            self._dur_cache[self._canon(path)] = dur
            self._dur_miss.discard(self._canon(path))
            s = self.merge_specs.get(path)
            if s is not None:
                s["duration"] = dur
        touched = bool(spec) or dur > 0
        for clip in self.edit_timeline.clips:
            if clip.source != path:
                continue
            if dur > 0:
                if clip.src_out <= 0:
                    clip.src_out = dur
                    touched = True
                if clip.src_duration <= 0:
                    clip.src_duration = dur
            if kfs and not clip.keyframes:
                clip.keyframes = kfs
                touched = True
        if touched:
            self.clip_editor.timeline_view.update()
            self.clip_editor._refresh_time_label()
            self._refresh_lossless()      # 规格/关键帧刚到, 结论可能从"不知道"变成确定
        # 时长刚读出来, 而用户正在看这条素材 → 补上进度条(v128)
        if (path == self._media_preview and dur > 0
                and self._media_dur <= 0 and self.edit_mode):
            self._media_dur = dur
            self.clip_editor.show_scrubber(dur)
            self.clip_editor.set_scrub_pos(self._media_pos)

    def _on_clip_probe_done(self):
        self._clip_probe_worker = None
        self.clip_editor.timeline_view.update()
        self.clip_editor._refresh_time_label()
        self._refresh_lossless()
        # ⚠️ 这一批跑的时候, 用户可能又加了新素材(那时因为"已经有线程在跑"被
        # 直接跳过) —— 收工后立刻再看一眼, 别把后加的那些漏掉。
        # (pending 会因 _probe_tried 收敛, 探不出来也不会无限起线程。)
        self._start_clip_probe_if_needed()

    def _on_clip_trimmed(self, clip):
        """裁完 / 拖完倍速: 统一刷新 + 记一步可撤销。

        v184: 以前这里只刷了时间读数和无损判定, **没刷工具条按钮和右栏清单**。
        「改变倍率」档拖边改的是 clip.speed, 数字其实变了, 但工具条上
        「帧率/倍速」还写着旧的 25/1.0、右栏也不动 —— 用户看到的就是
        "F1 里明明设了改变倍率, 拖了却没起作用"。统一走 _after_edit_changed()
        (里面已含 _edit_commit, 别再单独记一次撤销)。
        """
        self._after_edit_changed()
        if clip is None:
            return
        self.clip_panel.set_clip(clip, self.edit_timeline,
                                 {p.path: p for p in self.model.photos})
        # 倍率档松手给一句明确回执, 不用自己去看字带上那个小数字
        try:
            tv = self.clip_editor.timeline_view
            if (tv.edge_drag_mode() == "speed"
                    and clip.kind in ("video", "audio")
                    and abs(float(clip.speed or 1.0) - 1.0) > 1e-6):
                self.status_label.setText(
                    "倍速已设为 %g×（时长 %.1f 秒，导出时这段重新编码）"
                    % (float(clip.speed), float(clip.duration)))
        except Exception:
            pass

    # ------------------------------------------------------------ 工具箱(v122)
    # 改帧率 / 大幅削减 / 检测 / 波形 / 修复时长。
    # 共同点: 都是"对文件干活", 一律生成新文件(原文件不动), 一律在后台线程跑。
    def _selected_video_clip(self):
        clip = self.clip_editor.selected_clip()
        return clip if (clip is not None and clip.kind == "video") else None

    def _clip_fps(self, clip) -> float:
        """这个片段源文件的帧率(读已经探测过的规格, 不再跑一次 ffprobe)。

        工具条上的「帧率/倍速」按钮要显示它(v136/v165)。merge_specs 里没有就
        返回 0 —— 按钮会退回「帧率/倍速」四个字, 等后台探测填上后自己会变。
        """
        if clip is None or getattr(clip, "kind", "") != "video":
            return 0.0
        spec = self.merge_specs.get(getattr(clip, "source", "")) or {}
        try:
            from ..merge.grouping import fps_to_float
        except Exception:
            return 0.0
        for key in ("r_frame_rate", "avg_frame_rate"):
            try:
                v = float(fps_to_float(spec.get(key) or "0/0"))
            except Exception:
                continue
            if 0 < v < 480:
                return v
        return 0.0

    def _clip_speed(self, clip) -> float:
        """这个片段现在的倍速(默认 1.0)。「帧率/倍速」按钮要用它(v165)。"""
        try:
            v = float(getattr(clip, "speed", 1.0) or 1.0)
        except Exception:
            return 1.0
        return v if v > 0 else 1.0

    # ------------------------------------------- 工具条信息(v188)
    def _edit_info_clip(self):
        """工具条要显示谁: 选中的优先; 没选中且裁剪线上恰好一段 → 那一段。"""
        ce = self.clip_editor
        try:
            sel = ce.selected_clip()
        except Exception:
            sel = None
        return sel if sel is not None else ce.playhead_clip()

    def _sync_edit_tool_buttons(self, clip=None):
        """帧率按钮 / 音量条跟着「选中的」或「裁剪线上唯一那段」显示信息。

        v188: 没选中时只显示 + 允许改, **不自动选中**那一段; 真正改的那一刻
        才由 _on_edit_volume_requested / _clip_for_fps_tool 把选中效果做出来。
        clip: 调用方(比如 _on_edit_clip_selected)明确给了片段就以它为准 ——
        有些入口只发信号、还没来得及在轨道上落选中状态。
        """
        ce = self.clip_editor
        try:
            sel = ce.selected_clip()
        except Exception:
            sel = None
        explicit = clip is not None
        info = clip if explicit else (sel if sel is not None
                                     else ce.playhead_clip())
        has_sel = explicit or sel is not None
        is_video = info is not None and getattr(info, "kind", "") == "video"
        is_audio = info is not None and getattr(info, "kind", "") == "audio"
        try:
            ce._sync_tool_buttons(
                has_sel and is_video,
                self._clip_fps(info), self._clip_speed(info),
                has_audio=is_audio,
                fps_enabled=info is not None)
            ce.sync_volume_control(clip)
        except Exception:
            pass

    def _clip_for_fps_tool(self):
        """点「帧率/倍速」要改时: 没选中就把裁剪线上那一段选出来再改。"""
        ce = self.clip_editor
        clip = ce.selected_clip()
        if clip is not None:
            return clip
        clip = ce.playhead_clip()
        if clip is None:
            return None
        try:
            ce.timeline_view.select_clip(clip)
            self._on_edit_clip_selected(clip)
        except Exception:
            pass
        return clip

    def _on_tool_requested(self, what: str):
        """工具条第二行那几个按钮(v135)。

        原来是「工具」菜单里的条目 —— 用户不想为了一个动作先点开菜单,
        现在每个都是一个按钮, 这里只做 id → 方法的映射。
        """
        table = {
            "align_kf": self._edit_align_keyframes,
            "fps": self._tool_change_fps,
            "decimate": self._tool_decimate,
            "detect": self._tool_detect,
            "fix_duration": self._tool_fix_duration,
            "out_dir": self._choose_clip_out_dir,
            # v183: 存/取剪辑方案 + 整体重排
            "save_project": self._save_clip_project,
            "open_project": self._open_clip_project,
            "reorder_reverse": lambda: self._reorder_clips("reverse"),
            "reorder_time_new": lambda: self._reorder_clips("time_new"),
            "reorder_time_old": lambda: self._reorder_clips("time_old"),
            "reorder_name": lambda: self._reorder_clips("name"),
        }
        fn = table.get(what)
        if fn is not None:
            fn()

    # ------------------------------- v183 快速剪辑: 方案 / 复制 / 批量 / 重排
    # 口径: 全部只改时间线数据(顺序 / 属性 / 名字), 不动源文件、不重新渲染,
    # 点一下立刻完成; 统一走 _after_edit_changed() 记一步撤销。
    # ⚠️ 不新增、不改动任何快捷键 —— 这些都是菜单/按钮入口。

    PROJECT_TAG = "photosort-clip-project"
    PROJECT_VER = 1

    def _project_payload(self) -> dict:
        """方案的纯数据: 时间线整表快照 + 素材框现场 + 源路径清单。

        直接复用撤销栈那套 snapshot/restore —— 它已经把名字、音量、倍速、
        关键帧、轨道数都带上了, 单独再写一份迟早会漏字段。
        """
        tl = self.edit_timeline
        return {
            "format": self.PROJECT_TAG,
            "version": self.PROJECT_VER,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "timeline": tl.snapshot(),
            "media_hidden": sorted(self._media_hidden),
            "edit_extra": sorted(self._edit_extra),
            "sources": sorted({str(c.source) for c in tl.clips if c.source}),
        }

    def _write_clip_project(self, path: str) -> bool:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._project_payload(), f,
                          ensure_ascii=False, indent=2)
        except Exception as e:
            QMessageBox.warning(self._dlg_parent(), "没存成", str(e))
            self._restore_clip_front()
            return False
        return True

    def _save_clip_project(self):
        if self.edit_timeline is None:
            return
        start = self._clip_project_path or os.path.join(
            os.path.expanduser("~"), "剪辑方案.clipproj")
        path, _ = QFileDialog.getSaveFileName(
            self._dlg_parent(), "保存剪辑方案", start, "剪辑方案 (*.clipproj)")
        self._restore_clip_front()
        if not path:
            return
        if not path.lower().endswith(".clipproj"):
            path += ".clipproj"
        if not self._write_clip_project(path):
            return
        self._clip_project_path = path
        self.status_label.setText(
            f"剪辑方案已存：{os.path.basename(path)}"
            f"（{len(self.edit_timeline.clips)} 段）")

    def _read_clip_project(self, path: str):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.warning(self._dlg_parent(), "打不开", str(e))
            self._restore_clip_front()
            return None
        if not isinstance(data, dict) or data.get("format") != self.PROJECT_TAG:
            QMessageBox.information(self._dlg_parent(), "打不开",
                                    "这个文件不是剪辑方案。")
            self._restore_clip_front()
            return None
        return data

    def _open_clip_project(self):
        start = self._clip_project_path or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self._dlg_parent(), "打开剪辑方案", start, "剪辑方案 (*.clipproj)")
        self._restore_clip_front()
        if not path:
            return
        data = self._read_clip_project(path)
        if data is None:
            return
        rows = (data.get("timeline") or {}).get("clips") or []
        missing = [str(r.get("source") or "") for r in rows
                   if r.get("source") and not os.path.isfile(str(r["source"]))]
        msg = (f"打开「{os.path.basename(path)}」会用方案里的 {len(rows)} 段"
               f"替换当前时间线。<br>当前这次可以用 Ctrl+Z 撤销回来。<br>继续吗？")
        if missing:
            msg += (f"<br><br>有 {len(missing)} 个源文件已经找不到，"
                    f"这些片段仍会放回时间线，但预览会是空的。")
        ans = QMessageBox.question(
            self._dlg_parent(), "打开剪辑方案", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        self._restore_clip_front()
        if ans != QMessageBox.Yes:
            return
        self._apply_clip_project(data, path)

    def _apply_clip_project(self, data: dict, path: str = "") -> bool:
        """把方案装回现场。片段对象会重建, 外部旧引用全部作废。"""
        if self.clip_editor.is_playing():
            self._edit_pause()
        snap = dict(data.get("timeline") or {})
        self._media_hidden = set(data.get("media_hidden") or [])
        self._edit_extra = {str(p) for p in (data.get("edit_extra") or [])
                            if p and os.path.isfile(str(p))}
        srcs = [str(s) for s in (data.get("sources") or []) if s]
        self._project_sources = {self._canon(s) for s in srcs}
        # 方案里带进来、但主列表这次没有的文件: 补进素材框, 别让它们"看得见
        # 时间线、看不见素材格"。
        known = {self._canon(p.path) for p in self.model.photos}
        for s in srcs:
            if os.path.isfile(s) and self._canon(s) not in known:
                self._edit_extra.add(s)
        if self._edit_allowed is not None:
            self._edit_allowed |= set(self._project_sources)
        snap["media_hidden"] = sorted(self._media_hidden)
        self.edit_timeline.restore(snap)
        tv = self.clip_editor.timeline_view
        tv.select_clip(None)
        tv.refresh()
        self.clip_editor._refresh_time_label()
        self._refresh_media_panel()
        self._refresh_clip_list()
        self._refresh_top_status()
        self._start_clip_probe_if_needed()
        self._refresh_lossless()
        self.clip_panel.set_clip(None, self.edit_timeline,
                                 {p.path: p for p in self.model.photos})
        if self.edit_mode and not self.clip_editor.is_playing():
            self._refresh_preview_at(self.edit_timeline.playhead)
        # 记一步: 载入方案也要能 Ctrl+Z 退回载入前
        self._edit_commit()
        self._sync_undo_hint()
        if path:
            self._clip_project_path = path
        self.status_label.setText(
            f"已打开剪辑方案（{len(self.edit_timeline.clips)} 段）")
        return True

    # ------------------------------------------------------------- 复制 / 重复
    # -------------------------------- 片段剪贴板(Ctrl+C / Ctrl+V, v184)
    def _clipboard_copy(self):
        """把选中的片段记进内部剪贴板。

        只存**描述**, 不存片段对象 —— 撤销/恢复会重建 Clip, 存对象引用会
        指到已经不在时间线上的旧对象。粘到哪由裁剪线决定, 所以还要记住
        每组片段的相对起点(多选时保持原来的前后间隔)。
        """
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit)):
            try:                  # 轨名/输入框里 Ctrl+C 还是照常复制文字
                fw.copy()
            except Exception:
                pass
            return
        tl = self.edit_timeline
        if tl is None or not self.edit_mode:
            return
        try:
            sel = [c for c in self.clip_editor.timeline_view.selected_clips()
                   if c is not None]
        except Exception:
            sel = []
        if not sel:
            one = self.clip_editor.selected_clip()
            sel = [one] if one is not None else []
        known = list(tl.clips)
        sel = [c for c in self._ordered_clips()
               if any(c is x for x in sel) and any(c is x for x in known)]
        if not sel:
            self.status_label.setText("先选中片段，再按 Ctrl+C 复制")
            return
        base = min(float(c.start) for c in sel)
        self._clip_clipboard = [{
            "source": str(c.source or ""), "kind": str(c.kind or "video"),
            "track": int(c.track), "offset": float(c.start) - base,
            "src_in": float(c.src_in), "src_out": float(c.src_out),
            "hold": float(c.hold), "speed": float(c.speed or 1.0),
            "volume": float(getattr(c, "volume", 1.0) or 1.0),
            "muted": bool(c.muted), "detached": bool(c.detached),
            "src_parent": str(c.src_parent or ""),
            "src_duration": float(c.src_duration or 0.0),
            "keyframes": list(c.keyframes or []), "name": str(c.name or ""),
        } for c in sel]
        self.status_label.setText(
            "已复制 %d 段；把裁剪线放到要插入的位置，按 Ctrl+V" % len(sel))

    def _clipboard_paste(self):
        """把剪贴板里的片段插到裁剪线位置; 同轨自动让开, 磁吸开着就合拢。"""
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit)):
            try:
                fw.paste()
            except Exception:
                pass
            return
        tl = self.edit_timeline
        if tl is None or not self.edit_mode:
            return
        buf = getattr(self, "_clip_clipboard", None)
        if not buf:
            self.status_label.setText("还没有复制过片段（先选中片段按 Ctrl+C）")
            return
        at = max(0.0, float(getattr(tl, "playhead", 0.0) or 0.0))
        last = max(0, int(getattr(tl, "track_count", 1) or 1) - 1)
        made, cursor = [], {}
        for d in buf:
            t = max(0, min(last, int(d["track"])))
            st = max(0.0, float(cursor.get(t, at + float(d["offset"]))))
            nc = Clip(
                source=d["source"], kind=d["kind"], track=t, start=st,
                src_in=d["src_in"], src_out=d["src_out"], hold=d["hold"],
                speed=d["speed"], volume=d["volume"], muted=d["muted"],
                detached=d["detached"], src_parent=d["src_parent"],
                src_duration=d["src_duration"],
                keyframes=list(d["keyframes"]), name=d["name"])
            tl.clips.append(nc)
            cursor[t] = st + float(nc.duration)
            made.append(nc)
        for nc in made:
            tl.place(nc, nc.track, nc.start)
        if tl.magnetic:
            for t in sorted({c.track for c in made}):
                tl.compact(t)
        self._after_edit_changed()
        try:
            self.clip_editor.timeline_view.select_clips(made)
        except Exception:
            pass
        self.status_label.setText("已在裁剪线处粘贴 %d 段" % len(made))

    def _copy_clips(self, clips, times: int = 1) -> list:
        """复制选中的片段(可一次复制好几份), 接在这组后面, 同轨自动避让。

        只复制"时间线上的描述", 源文件一个字节都不动 —— 所以点一下就好。
        """
        tl = self.edit_timeline
        if tl is None:
            return []
        known = list(tl.clips)
        srcs = [c for c in (clips or [])
                if c is not None and any(c is x for x in known)]
        if not srcs:
            self.status_label.setText("先选中要复制的片段")
            return []
        times = max(1, min(int(times or 1), 50))
        srcs = [c for c in self._ordered_clips() if any(c is x for x in srcs)]
        anchor = max(c.end for c in srcs)
        made = []
        for n in range(times):
            cursor = {}
            round_made = []
            for c in srcs:
                t = int(c.track)
                st = float(cursor.get(t, anchor))
                suffix = " 副本" if times == 1 else f" {n + 2}"
                nc = Clip(
                    source=c.source, kind=c.kind, track=t, start=st,
                    src_in=float(c.src_in), src_out=float(c.src_out),
                    hold=float(c.hold), speed=float(c.speed or 1.0),
                    volume=float(getattr(c, "volume", 1.0) or 1.0),
                    muted=bool(c.muted), detached=bool(c.detached),
                    src_parent=str(c.src_parent or ""),
                    src_duration=float(c.src_duration or 0.0),
                    keyframes=list(c.keyframes or []),
                    name=str(c.name or "") + suffix)
                tl.clips.append(nc)
                cursor[t] = st + nc.duration
                round_made.append(nc)
                made.append(nc)
            # 让位(同轨不重叠); 磁吸开着 place 会顺手整轨合拢
            for nc in round_made:
                tl.place(nc, nc.track, nc.start)
            if round_made:
                anchor = max(nc.end for nc in round_made)
        tv = self.clip_editor.timeline_view
        self._after_edit_changed()
        tv.select_clips(made)
        self.status_label.setText(
            f"已复制 {len(srcs)} 段" if times == 1
            else f"已把 {len(srcs)} 段再复制 {times} 份（共新增 {len(made)} 段）")
        return made

    def _repeat_clips(self, clips):
        """问一句"再复制几份", 然后一次排好(做卡点用)。"""
        tl = self.edit_timeline
        srcs = [c for c in (clips or [])
                if c is not None and tl is not None
                and any(c is x for x in tl.clips)]
        if not srcs:
            self.status_label.setText("先选中要重复的片段")
            return
        n, ok = QInputDialog.getInt(
            self._dlg_parent(), "重复片段",
            f"把选中的 {len(srcs)} 段再复制几份？（1~50）", 2, 1, 50, 1)
        self._restore_clip_front()
        if not ok:
            return
        self._copy_clips(srcs, n)

    # ------------------------------------------------------------- 批量改
    def _batch_rename_clips(self, clips):
        """框选一片 → 按轨道顺序自动编号(只改显示名, 不动磁盘文件)。"""
        tl = self.edit_timeline
        order = self._ordered_clips()
        group = [c for c in order
                 if any(c is x for x in (clips or []))]
        if len(group) < 2:
            self.status_label.setText("先框选至少 2 段再编号")
            return
        prefix, ok = QInputDialog.getText(
            self._dlg_parent(), "按顺序编号", "名称前缀：",
            QLineEdit.Normal, "片段")
        self._restore_clip_front()
        if not ok:
            return
        prefix = (prefix or "").strip() or "片段"
        width = max(2, len(str(len(group))))
        for i, c in enumerate(group, 1):
            c.name = f"{prefix} {i:0{width}d}"
        self._after_edit_changed()
        self.status_label.setText(f"已把 {len(group)} 段按顺序编号为「{prefix}」")

    def _align_keyframes_for(self, targets, label: str = "片段"):
        """把这几段的首尾吸到最近的关键帧(改源入出点, 时间线位置不动)。"""
        tl = self.edit_timeline
        targets = [c for c in (targets or []) if c is not None
                   and getattr(c, "kind", "") == "video"]
        if not targets:
            self.status_label.setText("选中的片段里没有视频")
            return 0
        moved, skipped = 0, 0
        for c in targets:
            ks = list(getattr(c, "keyframes", None) or [])
            if not ks:
                try:
                    ks = list(MT.probe_keyframes(
                        c.source, self.merge_ffprobe, self.merge_ffmpeg) or [])
                    c.keyframes = list(ks)
                    self._refresh_shot_kf()
                except Exception:
                    ks = []
            if not ks:
                skipped += 1
                continue
            a = float(min(ks, key=lambda k: abs(float(k) - float(c.src_in))))
            b = float(min(ks, key=lambda k: abs(float(k) - float(c.src_out))))
            if b - a < 0.04:                 # 首尾吸到同一帧 → 会变成空片段
                skipped += 1
                continue
            if abs(a - c.src_in) > 1e-3 or abs(b - c.src_out) > 1e-3:
                c.src_in, c.src_out = a, b
                moved += 1
        if moved:
            self._after_edit_changed()
            extra = f"，{skipped} 个没动" if skipped else ""
            self.status_label.setText(
                f"已把 {moved} 个{label}对齐到关键帧{extra}")
        else:
            self.status_label.setText(
                "这些片段已经在关键帧上了" if not skipped
                else "读不到关键帧，先让它把片段读完再试")
        return moved

    def _batch_align_keyframes(self, clips):
        self._align_keyframes_for(clips, "选中片段")

    def _batch_speed(self, clips):
        """批量改倍速: 只对音频段(只记 speed, 导出时用 atempo)。

        ⚠️ 视频变速走的是"改帧率生成无损新文件", 一段一个文件、要等,
        不能混进"框一片一次改完"这条路里 —— 想改视频请用「帧率/倍速」。
        """
        tl = self.edit_timeline
        audio = [c for c in (clips or [])
                 if c is not None and getattr(c, "kind", "") == "audio"
                 and tl is not None and any(c is x for x in tl.clips)]
        if not audio:
            self.status_label.setText(
                "批量倍速只管音频片段；视频请用「帧率/倍速」逐段改")
            return
        old = self._clip_speed(audio[0])
        v, ok = QInputDialog.getDouble(
            self._dlg_parent(), "批量倍速",
            f"{len(audio)} 段音频的倍速（0.5 = 慢一半，2 = 快一倍）：",
            old, 0.25, 4.0, 2)
        self._restore_clip_front()
        if not ok or v <= 0 or abs(v - old) < 1e-6:
            return
        for c in audio:
            c.speed = float(v)
        for c in audio:
            tl.place(c, c.track, c.start)
        if tl.magnetic:
            for t in sorted({int(c.track) for c in audio}):
                tl.compact(t)
        self._after_edit_changed()
        self.status_label.setText(
            f"已把 {len(audio)} 段音频设为 {v:g}×（导出时重新编码声音）")

    # ------------------------------------------------------------- 整体重排
    def _clip_time_key(self, clip):
        """片段的拍摄时间(秒)。主列表查不到就用文件修改时间兜底。"""
        for p in self.model.photos:
            if p.path == clip.source:
                try:
                    return float(p.sort_dt().timestamp())
                except Exception:
                    break
        try:
            return float(os.path.getmtime(clip.source))
        except Exception:
            return 0.0

    def _reorder_clips(self, mode: str):
        """把轨道上的片段整体重排(沿用右栏拖拽那套: 位置留轨道号、首尾相接)。"""
        old = self._ordered_clips()
        if len(old) < 2:
            self.status_label.setText("轨道上至少要有两段才能重排")
            return
        if mode == "reverse":
            new = list(reversed(old))
            label = "倒序"
        elif mode in ("time_new", "time_old"):
            new = sorted(old, key=self._clip_time_key,
                         reverse=(mode == "time_new"))
            label = "按拍摄时间（新→旧）" if mode == "time_new" \
                else "按拍摄时间（旧→新）"
        elif mode == "name":
            new = sorted(old, key=lambda c: (
                c.name or os.path.basename(c.source)).lower())
            label = "按文件名"
        else:
            return
        if [id(c) for c in new] == [id(c) for c in old]:
            self.status_label.setText(f"{label}：顺序本来就是这样")
            return
        self._apply_clip_order(new, old)
        self._after_edit_changed()
        self.status_label.setText(
            f"已{label}重排 {len(new)} 段（同轨首尾相接）")

    # ---------------------------------------------------- 通用工具线程外壳
    def _stop_tool(self):
        """叫停正在跑的工具线程。⚠️ 不能在这里把引用置空:
        线程还在收尾, 对象一旦被回收就是 "Destroyed while thread is still running"。"""
        if self._tool_worker is not None:
            try:
                self._tool_worker.cancel()
            except Exception:
                pass

    def _retire_thread(self, worker):
        """一条线程用完了: 放进回收站, **等它真停下**再删。

        Qt 里删一个还在跑的 QThread 会直接 abort 整个进程(闪退),
        所以绝不能"信号一来就 deleteLater/丢引用"。
        """
        if worker is None:
            return
        if worker not in self._dead_threads:
            self._dead_threads.append(worker)
        self._purge_threads()

    def _purge_threads(self):
        """回收站清理: 只删已经停下的线程。"""
        alive = []
        for w in list(self._dead_threads):
            try:
                if w.isRunning():
                    alive.append(w)
                    continue
                w.deleteLater()
            except Exception:
                continue
        self._dead_threads = alive

    def _stop_thread(self, worker, timeout=3000):
        """尽力停下并等它退出(退出前不丢引用)。"""
        if worker is None:
            return
        try:
            if hasattr(worker, "stop"):
                worker.stop()
        except Exception:
            pass
        try:
            worker.wait(timeout)
        except Exception:
            pass
        self._retire_thread(worker)

    def _release_worker(self, worker):
        """线程跑完了: 放开引用, 但对象先进回收站(没真停下之前不删)。"""
        if self._tool_worker is worker:
            self._tool_worker = None
        if self._wave_worker is worker:
            self._wave_worker = None
        self._retire_thread(worker)

    def _run_tool(self, title: str, fn, on_ok):
        """开一个后台线程 + 进度框; 成功回调 on_ok(结果)。"""
        from ..merge.workers import ToolWorker
        self._stop_tool()
        w = ToolWorker(fn, self)
        self._tool_worker = w
        # ⚠️ 进度框必须挂**剪辑窗口**(用户此刻在看的那一个): 挂主窗口的话,
        #    模态框一出来, Windows 会把主窗口连人带框抬到剪辑窗口上面 ——
        #    用户看到的就是"改个帧率, 排序台自己弹出来把剪辑盖住了"(v146)。
        dlg = ProgressDialog(title, self._dlg_parent())
        dlg.bar.setMaximum(100)
        dlg.bar.setValue(0)
        dlg.label.setText("准备中…")
        dlg.log.setPlainText(title + "：这段时间别关窗口。")
        w.progress.connect(lambda pct, msg: self._tool_progress(dlg, title, pct, msg))
        w.done.connect(lambda ok, res, err: self._on_tool_done(dlg, ok, res, err, on_ok))
        w.finished.connect(lambda w=w: self._release_worker(w))
        dlg.canceled.connect(w.cancel)
        w.start()
        dlg.exec()
        self._restore_clip_front()      # 框关掉后剪辑窗口要回到最前面

    @staticmethod
    def _tool_progress(dlg, title, pct, msg):
        dlg.bar.setValue(max(0, min(100, int(pct))))
        dlg.label.setText(msg or "%s %d%%" % (title, int(pct)))

    def _on_tool_done(self, dlg, ok, res, err, on_ok):
        dlg.finish()
        if ok:
            dlg.label.setText("完成")
            try:
                on_ok(res)
            except Exception:
                pass
            return
        if err == "已取消":
            dlg.label.setText("已取消")
            return
        dlg.label.setText("没做成")
        QMessageBox.warning(self._dlg_parent(), "没做成", err or "原因不明")

    # ---------------------------------------------------- 改帧率 / 变速
    def _tool_change_fps(self):
        """「帧率/倍速」(v185): 只在选中的这一段上记倍速, 素材文件一个字节不动。

        v185 口径: 导出之前, 变速只是时间线上的一个记号 —— 按钮 / 拖边 / 批量改
        是同一套, 随时能改、Ctrl+Z 能退; 到导出那一步才真正落地(变速那段单独
        处理, 其余片段照旧无损拼接)。

        以前这里会调 MT.change_fps 另存一份新素材, 再把时间线上所有引用旧素材
        的片段一起换掉 —— 既提前动了文件, 又不止改选中的这一段。现在都不做了。
        「改帧率」仍然按用户的心智模型当入口: 填新帧率, 换算成倍速记下来,
        工具条上显示等效帧率, 一眼看得出「改帧率 = 改倍速」。
        """
        from .tools_dialog import FpsDialog
        clip = self._clip_for_fps_tool()
        if clip is None:
            self.status_label.setText(
                "先选中一个片段，或把裁剪线移到某个片段上")
            return
        if clip.kind == "audio":
            self._tool_change_audio_speed(clip)     # 音频没有帧率, 只改倍速(v167)
            return
        if clip.kind != "video":
            self.status_label.setText("先在时间线上选中一个视频片段")
            return
        fps = self._clip_fps(clip)
        if fps <= 0:
            QMessageBox.information(self._dlg_parent(), "改帧率 / 变速",
                                    "还没读出这个视频的帧率，等它探测完再试。")
            return
        span = max(0.0, float(clip.src_out or 0.0) - float(clip.src_in or 0.0))
        if span <= 0.05:
            span = float(clip.src_duration or 0.0)
        old = self._clip_speed(clip)
        dlg = FpsDialog(self._dlg_parent(), clip.name, fps,
                        span, cur_speed=old)
        if dlg.exec() != QDialog.Accepted:
            return
        new_fps = dlg.value()
        speed = (new_fps / fps) if fps > 0 else 1.0
        if speed <= 0 or abs(speed - old) < 1e-6:
            return
        clip.speed = speed
        tl = self.edit_timeline
        # 变快就变短: 同轨可能压到邻居, 让它回到合法位置; 磁吸开着就整轨合拢
        tl.place(clip, clip.track, clip.start)
        if tl.magnetic:
            tl.compact(clip.track)
        self._after_edit_changed()      # 里面会记一步撤销, 别重复 commit
        self.status_label.setText(
            "这段已设为 %g×（等效帧率 %g fps，导出时才落地；其它片段不受影响）"
            % (speed, new_fps))

    def _edit_clip_volume(self, clips):
        """片段音量(响度, v169): 只把倍数记在片段上, 导出时才重编码这段声音。

        和音频倍速同一套思路 —— 音量滤镜做不了流复制, 所以不改文件、
        随时可改、Ctrl+Z 能退; 导出时哪段音量 ≠ 100% 哪段的声音重新编码。
        """
        from .tools_dialog import VolumeDialog
        clips = [c for c in (clips or [])
                 if c.kind in ("video", "audio")] or []
        if not clips:
            return
        cur = float(getattr(clips[0], "volume", 1.0) or 1.0)
        dlg = VolumeDialog(self._dlg_parent(), clips[0].name, cur, len(clips))
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.value()
        changed = [c for c in clips
                   if abs(float(getattr(c, "volume", 1.0) or 1.0) - v) > 1e-6]
        if not changed:
            return
        for c in changed:
            c.volume = v
        self.clip_editor.timeline_view.update()
        # 记一步撤销(同静音): 不然"改音量 → Ctrl+Z"退掉的是更早那步
        self._edit_commit()
        self._refresh_top_status()
        pct = int(round(v * 100))
        self.status_label.setText(
            "音量已设为 %d%%（%d 段，导出时这段声音重新编码）"
            % (pct, len(changed)))

    def _tool_change_audio_speed(self, clip):
        """音频的「帧率/倍速」: 只改倍速, 不生成新文件(v167)。

        视频那套"改时间戳"的无损变速对音频不存在 —— 音频没有帧, 变速必须
        重新采样。所以这里只把倍速记在片段上(随时可改可撤销), 导出时才编码;
        原文件一个字节不动。时长按倍速缩放, 同轨要是撞上会自动让位。
        """
        from .tools_dialog import AudioSpeedDialog
        if not os.path.isfile(clip.source):
            QMessageBox.information(self._dlg_parent(), "音频倍速",
                                    "这个片段的源文件找不到了。")
            return
        old = self._clip_speed(clip)
        span = max(0.0, float(clip.src_out or 0.0) - float(clip.src_in or 0.0))
        if span <= 0.05 and float(clip.src_duration or 0.0) > 0.05:
            span = float(clip.src_duration)
        dlg = AudioSpeedDialog(self._dlg_parent(), clip.name, old, span)
        if dlg.exec() != QDialog.Accepted:
            return
        new = dlg.value()
        if new <= 0 or abs(new - old) < 1e-6:
            return
        tl = self.edit_timeline
        clip.speed = new
        # 变长/变短了: 同轨可能压到邻居, 让它回到合法位置; 磁吸开着就整轨合拢
        tl.place(clip, clip.track, clip.start)
        if tl.magnetic:
            tl.compact(clip.track)
        self._after_edit_changed()
        self.status_label.setText(
            "音频已设为 %g×（时长 %.1f → %.1f 秒，导出时重新编码）" % (
                new, (span / old if old > 0 else span), (span / new)))

    def _after_change_fps(self, old_src: str, speed: float, r: dict):
        """新文件好了: 把时间线上引用旧文件的地方换成新文件(时间按倍速缩放)。"""
        new = r.get("out") or ""
        if not new:
            return
        kfs = MT.probe_keyframes(new, self.merge_ffprobe, self.merge_ffmpeg)
        try:
            spec = MT.info(new, self.merge_ffprobe, self.merge_ffmpeg).get("spec")
            if spec:
                self.merge_specs[new] = spec
                self.merge_specs.pop(old_src, None)
        except Exception:
            pass
        tl = self.edit_timeline
        speed = speed if speed > 0 else 1.0
        touched = 0
        for clip in tl.clips:
            if clip.source != old_src:
                continue
            clip.source = new
            clip.name = os.path.basename(new)
            clip.src_in = max(0.0, clip.src_in / speed)
            clip.src_out = max(clip.src_in + 0.05, clip.src_out / speed)
            if clip.src_duration > 0:
                clip.src_duration = clip.src_duration / speed
            if clip.src_duration > 0 and clip.src_out > clip.src_duration:
                clip.src_out = clip.src_duration
            clip.keyframes = list(kfs)
            self._refresh_shot_kf()
            clip.speed = 1.0
            touched += 1
        if tl.magnetic:
            for t in range(tl.track_count):
                tl.compact(t)
        self._remember_extra(new)
        self._after_edit_changed()
        # 新文件不一定在原文件夹, 提示里要说清放在哪了
        self.status_label.setText(
            "已生成 %s（%.1f 秒 → %.1f 秒，%g 倍速，放在「%s」）%s" % (
                os.path.basename(new), r.get("before", 0), r.get("after", 0),
                speed, self._out_dir_hint(new),
                "" if touched else "；可以从素材框拖进时间线"))

    # ---------------------------------------------------- 大幅削减
    def _tool_decimate(self):
        from .tools_dialog import DecimateDialog
        clip = self._selected_video_clip()
        if clip is None:
            self.status_label.setText("先在时间线上选中一个视频片段")
            return
        meta = MT.info(clip.source, self.merge_ffprobe, self.merge_ffmpeg)
        kfs = MT.probe_keyframes(clip.source, self.merge_ffprobe, self.merge_ffmpeg,
                                 limit=20000)
        if not kfs:
            QMessageBox.information(self._dlg_parent(), "大幅削减",
                                    "这个视频读不到关键帧，没法只留关键帧。")
            return
        dlg = DecimateDialog(self._dlg_parent(), os.path.basename(clip.source),
                             float(meta.get("fps") or 25.0),
                             float(meta.get("duration") or 0.0), len(kfs))
        if dlg.exec() != QDialog.Accepted:
            return
        every, out_fps = dlg.value()
        out = MT.output_path_for(clip.source, "_只留关键帧")
        src = clip.source
        self._run_tool(
            "大幅削减",
            lambda progress, cancel: MT.decimate(
                src, out, every, out_fps, self.merge_ffmpeg, self.merge_ffprobe,
                progress=progress, cancel=cancel),
            lambda r: self._after_decimate(r))

    def _after_decimate(self, r: dict):
        new = r.get("out") or ""
        if not new:
            return
        self._remember_extra(new)
        self._refresh_media_panel()
        self.status_label.setText(
            "已生成 %s：%.1f 秒 → %.2f 秒，只留了 %d 帧（没有声音，放在「%s」）。"
            "要用的话从素材框把它拖进时间线" % (
                os.path.basename(new), r.get("before", 0), r.get("after", 0),
                r.get("kept", 0), self._out_dir_hint(new)))

    # ---------------------------------------------------- 检测
    def _tool_detect(self):
        from .tools_dialog import DetectDialog
        clip = self._selected_video_clip()
        if clip is None:
            self.status_label.setText("先在时间线上选中一个视频片段")
            return
        src = clip.source
        # 关键帧是加到轨道就自动后台读的(v139): 读到了就把那一项置灰显示数量,
        # 不用再让 ffprobe 把整个文件扫一遍。同一个素材只要有一个片段读到就算数。
        kf = len(getattr(clip, "keyframes", None) or [])
        if not kf:
            for c in self.edit_timeline.clips:
                if c.source == src and len(getattr(c, "keyframes", None) or []) > kf:
                    kf = len(c.keyframes)
        dlg = DetectDialog(self._dlg_parent(), os.path.basename(src), kf)
        dlg.detectRequested.connect(lambda kinds: self._run_detect(src, kinds, dlg))
        dlg.actionRequested.connect(lambda code: self._apply_detect(code, src, dlg))
        dlg.stopped.connect(self._stop_tool)
        dlg.exec()

    def _run_detect(self, src: str, kinds, dlg):
        from ..merge.workers import ToolWorker
        self._stop_tool()
        w = ToolWorker(
            lambda progress, cancel: MT.detect_all(
                src, list(kinds), self.merge_ffmpeg, self.merge_ffprobe,
                progress=progress, cancel=cancel),
            self)
        self._tool_worker = w
        w.progress.connect(lambda pct, _msg: dlg.set_progress(pct))
        w.done.connect(lambda ok, res, err: self._on_detect_done(dlg, src, ok, res, err))
        w.finished.connect(lambda w=w: self._release_worker(w))
        w.start()

    def _on_detect_done(self, dlg, src, ok, res, err):
        if not ok:
            dlg.set_error("已取消" if err == "已取消" else err)
            return
        res = res or {}
        self._detect[src] = res
        dlg.set_result(res)
        self._detect_marks[src] = {
            "black": [(b.get("start", 0), b.get("end", 0))
                      for b in res.get("black") or []],
            "silence": [(s.get("start", 0), s.get("end", 0))
                        for s in res.get("silence") or []],
            "scene": [s.get("t", 0) for s in res.get("scene") or []],
        }
        tv = self.clip_editor.timeline_view
        tv.set_marks(self._detect_marks)
        kfs = res.get("keyframes") or []
        if kfs:
            for clip in self.edit_timeline.clips:
                if clip.source == src:
                    clip.keyframes = list(kfs)
            self._refresh_shot_kf()
            self._refresh_lossless()   # 关键帧刚到: "没读到关键帧"的提醒要撤掉
        tv.update()
        self.status_label.setText("检测完成，结果已经标在时间线上")

    def _apply_detect(self, code: str, src: str, dlg):
        if code == "fix_duration":
            # v197: 不再一上来就关掉检测页。结论写回检测窗口, 只有用户确认
            # 「确实要修」之后才离开检测页去跑修复(见 _tool_fix_duration)。
            self._tool_fix_duration(dlg=dlg, src=src)
            return
        res = self._detect.get(src) or {}
        if not res:
            return
        dlg.accept()          # 先关掉对话框, 再改时间线
        if code in ("drop_black", "drop_silence"):
            key = "black" if code == "drop_black" else "silence"
            ranges = [(float(s.get("start", 0)), float(s.get("end", 0)))
                      for s in res.get(key) or []]
            n = self._cut_source_ranges(src, ranges)
            self._after_edit_changed()
            self.status_label.setText(
                "已删掉 %d 段%s" % (n, "黑场" if code == "drop_black" else "无声部分"))
            if n == 0:
                QMessageBox.information(
                    self._dlg_parent(), "没动",
                    "这些段落没有落在当前用到的片段里（可能片段只用了其中一小截）。")
        elif code == "cut_scene":
            times = [float(s.get("t", 0)) for s in res.get("scene") or []]
            n = self._split_at_source_times(src, times)
            self._after_edit_changed()
            self.status_label.setText("已在 %d 处切开" % n)

    def _cut_source_ranges(self, src: str, ranges) -> int:
        """把某个源文件上的若干时间段从时间线里删掉(每个片段分别算)。"""
        if not ranges:
            return 0
        count = 0
        for a, b in sorted(ranges):
            if b - a <= 0.05:
                continue
            for clip in list(self.edit_timeline.clips):
                if clip.source != src:
                    continue
                if b <= clip.src_in + 1e-6 or a >= clip.src_out - 1e-6:
                    continue
                if self._cut_source_range(clip, a, b):
                    count += 1
        return count

    def _cut_source_range(self, clip, a: float, b: float) -> bool:
        """在单个片段里挖掉源时间 [a, b]（前后留下, 中间删掉）。"""
        tl = self.edit_timeline
        speed = clip.speed or 1.0
        a = max(a, clip.src_in)
        b = min(b, clip.src_out)
        if b - a <= 0.05:
            return False
        if a <= clip.src_in + 0.05:
            g = clip.start + (b - clip.src_in) / speed
            tl.split_at(clip, g)
            tl.remove(clip)
            return True
        if b >= clip.src_out - 0.05:
            g = clip.start + (a - clip.src_in) / speed
            right = tl.split_at(clip, g)
            if right is not None:
                tl.remove(right)
            return True
        g1 = clip.start + (a - clip.src_in) / speed
        g2 = clip.start + (b - clip.src_in) / speed
        mid = tl.split_at(clip, g1)
        if mid is None:
            return False
        tl.split_at(mid, g2)
        tl.remove(mid)
        return True

    def _split_at_source_times(self, src: str, times) -> int:
        """在这些源时间点把片段切开(用于「在场景变化处切开」)。"""
        tl = self.edit_timeline
        n = 0
        for t in sorted(times):
            for clip in list(tl.clips):
                if clip.source != src:
                    continue
                if not (clip.src_in + 0.05 < t < clip.src_out - 0.05):
                    continue
                g = clip.start + (t - clip.src_in) / (clip.speed or 1.0)
                if tl.split_at(clip, g) is not None:
                    n += 1
        return n

    # ---------------------------------------------------- 修复时长
    def _tool_fix_duration(self, dlg=None, src=None):
        """核对文件写着的时长和实际内容长度; 不一致才重新封装一次(不重编码)。

        v197: 从检测弹窗里点进来时(dlg 不为 None), 结论一律写回检测窗口内,
        不弹独立提示框、不退出检测页 —— 只有用户确认要修之后才离开检测页。
        工具条/其它入口仍按老口径用独立提示框。
        """
        if not src:
            clip = self.clip_editor.selected_clip()
            src = clip.source if clip is not None else ""
        if not src or not os.path.isfile(src):
            src = self._export_out if os.path.isfile(self._export_out or "") else ""

        def say(msg):
            if dlg is not None:
                dlg.set_fix_status(msg)
            else:
                QMessageBox.information(self._dlg_parent(), "修复时长", msg)

        if not src:
            say("先在时间线上选中一个片段（或者先导出一个文件），再来修。")
            return
        self.status_label.setText("正在核对时长…")
        prob = MT.duration_problem(src, self.merge_ffprobe, self.merge_ffmpeg)
        if not prob.get("real"):
            say("读不出这个文件的内容，修不了。")
            return
        if not prob.get("bad"):
            say("时长没问题：文件写的时长和实际一致（%.1f 秒），不用修。"
                % prob["declared"])
            return
        # 确认框挂当前检测弹窗: 它是应用模态的, 挂到被它挡住的主窗口上会点不动。
        parent = dlg if dlg is not None else self._dlg_parent()
        if not confirm_yes_no(
                parent, "发现时长写错了",
                "文件里写的时长：%.1f 秒\n实际内容长度：%.1f 秒\n\n"
                "修复方式是重新封装一次（画面不重新编码），会生成一个新文件，"
                "原文件保持不动。要修吗？" % (prob["declared"], prob["real"]),
                center=True):
            if dlg is not None:
                dlg.set_fix_status("已取消修复，没有改动任何文件。")
            return
        if dlg is not None:
            if getattr(dlg, "_running", False):
                dlg.stopped.emit()      # 检测还在跑: 先停, 别把结果丢在已关掉的窗口里
            dlg.accept()
            # 等检测弹窗的模态循环收干净, 再开修复的进度框(避免嵌套模态)。
            QTimer.singleShot(0, lambda: self._do_fix_duration(src))
        else:
            self._do_fix_duration(src)

    def _do_fix_duration(self, src: str):
        """确认要修之后才落地: 生成新文件, 原文件一律不动。"""
        out = MT.output_path_for(src, "（修复时长）")
        self._run_tool(
            "修复时长",
            lambda progress, cancel: MT.fix_duration(
                src, out, self.merge_ffmpeg, self.merge_ffprobe, cancel=cancel),
            lambda r: self._after_fix_duration(r))

    def _after_fix_duration(self, r: dict):
        new = r.get("out") or ""
        if not new:
            return
        self._remember_extra(new)
        self._refresh_media_panel()
        if r.get("still_bad"):
            self.status_label.setText(
                "已生成 %s，但这个文件的时长仍然不准" % os.path.basename(new))
        else:
            self.status_label.setText(
                "修好了：%.1f 秒 → %.1f 秒（%s，放在「%s」）" % (
                    r.get("before", 0), r.get("after", 0), os.path.basename(new),
                    self._out_dir_hint(new)))

    # ---------------------------------------------------- 新文件存哪
    @staticmethod
    def _out_dir_hint(path: str) -> str:
        """状态栏里说明新文件落在哪个文件夹(只是文件夹名, 不甩一长串路径)。"""
        d = os.path.dirname(os.path.abspath(path))
        return os.path.basename(d) or d

    def _choose_clip_out_dir(self):
        """剪辑生成的文件存到哪: 原文件夹旁新建 / 原文件夹 / 自己指定。"""
        from .tools_dialog import ClipOutDialog
        dlg = ClipOutDialog(self._dlg_parent())
        if dlg.exec() == QDialog.Accepted:
            dlg.save()
            self.status_label.setText("已改好生成文件的存放位置")

    # ---------------------------------------------------- 波形
    def _on_waveform_toggled(self, on: bool):
        self.clip_editor.timeline_view.set_wave_visible(bool(on))
        if on:
            self._load_waveforms()

    def _on_edit_volume_requested(self, value: float):
        """v187: 音量拖动条 → 只改选中的片段(多选就一起改), 导出时才生效。

        和原来的右键「音量」是同一份数据(clip.volume), 只是入口换到工具条上;
        不改源文件、Ctrl+Z 能退。
        v188: 没选中、但裁剪线上恰好一段时, 在**真正要改的这一下**才把那段
        选出来(之前只显示它的信息, 不动选中状态)。
        """
        try:
            v = max(0.0, min(2.0, float(value)))
        except (TypeError, ValueError):
            return
        ce = self.clip_editor
        tv = ce.timeline_view
        try:
            if not tv.selected_clips():
                clip = ce.playhead_clip()
                if clip is None:
                    return
                tv.select_clip(clip)
                self._on_edit_clip_selected(clip)
            clips = [c for c in tv.selected_clips()
                     if getattr(c, "kind", "") in ("video", "audio")
                     and not c.silent]
        except Exception:
            return
        changed = [c for c in clips
                   if abs(float(getattr(c, "volume", 1.0) or 1.0) - v) > 1e-6]
        if not changed:
            return
        for c in changed:
            c.volume = v
        tv.update()
        self._edit_commit()
        self._refresh_top_status()
        try:
            ce.sync_volume_control()
        except Exception:
            pass
        self.status_label.setText(
            "音量已设为 %d%%（%d 段，导出时才生效）"
            % (int(round(v * 100)), len(changed)))

    def _on_volume_editing(self, on: bool):
        """手输数值时临时停用剪辑键位, 免得数字键被 QShortcut 抢走。

        只动 QShortcut 的启用状态, 一行键位配置都不碰; 输完恢复成剪辑态
        本来的状态(不在剪辑态就继续关着)。
        """
        self._set_edit_shortcuts(False if on else bool(self.edit_mode))

    def _load_waveforms(self):
        """把时间线上还没读过的源各读一遍波形(串行, 后台线程)。"""
        from ..merge.workers import ToolWorker
        tv = self.clip_editor.timeline_view
        todo = []
        for clip in self.edit_timeline.clips:
            if clip.kind in ("video", "audio") and clip.source not in todo \
                    and not tv.has_waveform(clip.source):
                todo.append(clip.source)
        if not todo or self._wave_worker is not None:
            return
        self.status_label.setText("正在读声音波形…")

        def work(progress, cancel):
            out = {}
            for i, src in enumerate(todo):
                if cancel():
                    break
                try:
                    w = MT.waveform(src, ffmpeg=self.merge_ffmpeg, cancel=cancel)
                    out[src] = w.get("peaks") or []
                except Exception:
                    out[src] = []
                progress(int(100 * (i + 1) / max(1, len(todo))),
                         os.path.basename(src))
            return out

        w = ToolWorker(work, self)
        self._wave_worker = w
        w.done.connect(self._on_wave_done)
        w.finished.connect(lambda w=w: self._release_worker(w))
        w.start()

    def _on_wave_done(self, ok, res, err):
        if not ok or not isinstance(res, dict):
            return
        tv = self.clip_editor.timeline_view
        for src, peaks in res.items():
            tv.set_waveform(src, peaks)
        self.status_label.setText("波形已显示" if any(res.values())
                                  else "这些素材没有声音，没有波形可显示")

    # ------------------------------------------------------- 撤销 / 重做
    def _edit_commit(self):
        """把当前时间线状态记进撤销栈(状态没变就跳过)。

        用整表快照而不是"反向动作": 拖动会避让、裁剪会吸关键帧、加轨会夹轨号,
        反向动作要写一大套还容易漏; 快照只是一份 dict 列表, 稳且不会漏。
        """
        tl = getattr(self, "edit_timeline", None)
        if tl is None:
            return
        try:
            snap = tl.snapshot()
        except Exception:
            return
        # 素材框的"被移出"集合也进快照: 不然把素材移出框之后按 Ctrl+Z, 退的是更早
        # 的片段改动, 素材照样不见 —— 用户看到的就是"撤销没生效"(v145)。
        try:
            snap["media_hidden"] = sorted(self._media_hidden)
        except Exception:
            pass
        if 0 <= self._undo_index < len(self._undo_stack) \
                and self._same_edit_state(self._undo_stack[self._undo_index], snap):
            return
        del self._undo_stack[self._undo_index + 1:]   # 有新动作 → 弃掉重做分支
        self._undo_stack.append(snap)
        if len(self._undo_stack) > 120:
            self._undo_stack.pop(0)
        self._undo_index = len(self._undo_stack) - 1
        self._sync_undo_hint()

    @staticmethod
    def _same_edit_state(a: dict, b: dict) -> bool:
        """两份快照在"用户看得见的结构"上是否一样。

        ⚠️ 不能直接比整份 dict: 里面还带着播放头位置和后台探测回填的时长/关键帧 ——
        播放头动一下、探测跑完一次, 都会被当成"用户改了东西"而多记一步,
        撤销时就会退到一堆什么都没变的状态上。只比真正算编辑的字段。
        """
        if not a or not b:
            return False
        if int(a.get("track_count") or 0) != int(b.get("track_count") or 0):
            return False
        if bool(a.get("magnetic")) != bool(b.get("magnetic")):
            return False
        if list(a.get("media_hidden") or []) != list(b.get("media_hidden") or []):
            return False
        ca, cb = a.get("clips") or [], b.get("clips") or []
        if len(ca) != len(cb):
            return False
        keys = ("source", "kind", "track", "start", "src_in", "src_out",
                "hold", "speed", "muted",
                # v169: 音量也算一步编辑 —— 漏掉的话"改个音量"不入撤销栈
                "volume",
                # ⚠️ name / detached / src_parent 也必须比(v158): 漏掉 name 的话
                #    "改个名"被判成"什么都没变" → 压根不入栈, 撤销退的是更早那步;
                #    漏掉 detached 的话"分离音频"这一步有时也记不上。
                #    (src_duration / keyframes 不比 —— 那是后台探测回填的。)
                "name", "detached", "src_parent")
        for x, y in zip(ca, cb):
            for k in keys:
                if x.get(k) != y.get(k):
                    return False
        return True

    def _edit_undo(self):
        if self._undo_index <= 0:
            self.status_label.setText("没有可撤销的操作了")
            return
        self._undo_index -= 1
        self._restore_edit_snapshot(self._undo_stack[self._undo_index],
                                    "已撤销")

    def _edit_redo(self):
        if self._undo_index >= len(self._undo_stack) - 1:
            self.status_label.setText("没有可重做的操作了")
            return
        self._undo_index += 1
        self._restore_edit_snapshot(self._undo_stack[self._undo_index],
                                    "已重做")

    def _restore_edit_snapshot(self, snap: dict, verb: str):
        """回到某个快照并刷新整页(片段对象是重建的, 选中项要清掉)。"""
        if self.clip_editor.is_playing():
            self._edit_pause()
        # 素材框"被移出"的那批也一起复原(移出素材框现在也算一步, 见 _edit_commit)
        self._media_hidden = set(snap.get("media_hidden") or [])
        self.edit_timeline.restore(snap)
        tv = self.clip_editor.timeline_view
        tv.select_clip(None)
        tv.refresh()
        self.clip_editor._refresh_time_label()
        self._refresh_media_panel()
        # 右栏片段清单也要跟着复原: 少这一句的话, 时间线上已经退回上一步了,
        # 右栏还挂着原来的卡片数 —— 看着就像"撤销没反应"(v145)。
        self._refresh_clip_list()
        self._refresh_top_status()
        self._refresh_lossless()
        # 撤销/重做后中间的画面也要重画(v164): 片段是重建的, 原来那张帧可能属于
        # 已经不存在的片段 —— 不重画就会一直挂着上一张图。
        if self.edit_mode and not self.clip_editor.is_playing():
            self._refresh_preview_at(self.edit_timeline.playhead)
        # 快照里的时长/关键帧可能是旧的(片段对象是重建的) → 缺的重新探一遍
        self._start_clip_probe_if_needed()
        self.clip_panel.set_clip(None, self.edit_timeline,
                                 {p.path: p for p in self.model.photos})
        self.status_label.setText(
            f"{verb}（第 {self._undo_index + 1} / {len(self._undo_stack)} 步）")
        self._sync_undo_hint()
        # v187: 撤销/重做会把 clip.volume 一起退回去, 音量组得跟着换数字
        try:
            self.clip_editor.sync_volume_control()
        except Exception:
            pass

    def _sync_undo_hint(self):
        """把"能不能撤销"挂到工具提示上, 免得点了没反应还以为坏了。"""
        try:
            self.clip_editor.undo_hint(self._undo_index,
                                       len(self._undo_stack))
        except Exception:
            pass

    # ------------------------------------------------------- 无损判定 / 导出
    def _evaluate_lossless(self):
        """重算一次"这次能不能无损"。规格没读完时会提示, 不硬判。"""
        return LS.evaluate(self.edit_timeline, self.merge_specs,
                           ffmpeg_ok=bool(self.merge_ffmpeg))

    def _refresh_lossless(self):
        """把判定结果贴到界面: 胶囊 / 顶部红条 / 时间线标红。"""
        plan = self._evaluate_lossless()
        self._loss_plan = plan
        self.clip_editor.set_loss_state(plan)
        tv = self.clip_editor.timeline_view
        tv.set_loss_marks(plan.bad_clips(), plan.warn_clips())
        # 片段右下角那行「120fps/1.5」要知道每段的源帧率(v166)。规格是
        # 后台探测陆续回来的, 所以每次重算无损都顺手同步一遍(它自己会去重)。
        tv.set_fps_map(self._clip_fps_map())
        return plan

    def _clip_fps_map(self) -> dict:
        """时间线上每一个源文件 → 帧率(读 merge_specs, 不再跑 ffprobe)。"""
        out = {}
        tl = self.edit_timeline
        if tl is None:
            return out
        for clip in tl.clips:
            src = getattr(clip, "source", "")
            if not src or src in out:
                continue
            fps = self._clip_fps(clip)
            if fps > 0:
                out[src] = fps
        return out

    def _apply_loss_fix(self, code: str) -> bool:
        """清单里的一键修复(合拢 / 取消静音 / 恢复 1 倍速 / 去掉图片…)。"""
        try:
            ok = bool(LS.apply_fix(self.edit_timeline, code))
        except Exception:
            ok = False
        if ok:
            self._after_edit_changed()
            self.status_label.setText(
                "已取消分离：声音放回视频" if code == "undetach"
                else "已按提示调整时间线")
        return ok

    def _dlg_parent(self):
        """影音里的弹窗该挂在谁身上。

        ⚠️ 剪辑是**独立窗口**, 主窗口(也就是「合并」界面)还留在后面没关。
        弹窗如果一律 `parent=self`(主窗口), 弹出来的瞬间 Windows 会把父窗口
        带到前台 —— 用户看到的就是"点一下导出, 合并界面跳出来了"。
        所以剪辑窗口开着就挂它。
        """
        w = getattr(self, "_clip_window", None)
        if w is not None and w.isVisible():
            return w
        return self

    def _show_loss_report(self):
        """点胶囊 / 红条: 只看原因 + 一键修复, 不导出。"""
        plan = self._refresh_lossless()
        dlg = LosslessDialog(self._dlg_parent(), plan,
                             evaluate=self._evaluate_lossless,
                             fix=self._apply_loss_fix, readonly=True)
        dlg.exec()

    def _default_export_path(self) -> str:
        """默认输出: 第一个片段所在文件夹 / 剪辑导出.mp4(重名就 +1)。"""
        base = ""
        for c in sorted(self.edit_timeline.clips, key=lambda c: c.start):
            d = os.path.dirname(c.source or "")
            if d and os.path.isdir(d):
                base = d
                break
        base = base or os.path.expanduser("~")
        p = os.path.join(base, "剪辑导出.mp4")
        i = 1
        while os.path.exists(p):
            p = os.path.join(base, f"剪辑导出({i}).mp4")
            i += 1
        return p

    def _export_timeline(self):
        """点导出: 先判定, 不能无损的必须在这个弹窗里被明确警告一次。"""
        # v223: 与 start_merge 同口径 —— 统一还在后台转码时不导出。导出与合并
        # 一样会把工作副本/原片拼进成片, 且和转码抢 CPU; resolve() 虽有半成品
        # 回退, 但导出中途副本状态变化仍可能让"无损判定"与实际文件对不上。
        if getattr(self, "_normalize_worker", None) is not None:
            QMessageBox.information(
                self._dlg_parent(), "统一进行中",
                "统一格式还在后台转码，完成后再导出（届时工作副本会自动参与）。\n"
                "如不想等，可先在统一进度窗口里取消。")
            return
        plan = self._refresh_lossless()
        if not plan.exportable:
            QMessageBox.information(self._dlg_parent(), "导出",
                                    plan.summary() + "。\n\n"
                                    "点右上角的胶囊可以看到具体原因。")
            return
        dlg = LosslessDialog(self._dlg_parent(), plan,
                             evaluate=self._evaluate_lossless,
                             fix=self._apply_loss_fix, readonly=False,
                             default_out=self._default_export_path())
        if dlg.exec() != QDialog.Accepted:
            return
        plan = dlg.plan
        out = (dlg.out_path or "").strip()
        if not out or not plan.exportable:
            return
        # v217: 导出名撞素材 → -y 会把素材截断(无损路径尤其看不出来)
        bad = pathsafe.check_output(
            out, [getattr(s, "source", "") for s in plan.segments])
        if bad:
            QMessageBox.warning(self._dlg_parent(), "输出文件名冲突", bad)
            return
        if os.path.exists(out):
            if not confirm_yes_no(self._dlg_parent(), "覆盖输出文件",
                                  f"这个文件已经存在，要覆盖它吗？\n\n{out}",
                                  center=True, red_yes=False):
                return
        self._run_export(plan, out)

    def _run_export(self, plan, out: str):
        from ..merge.workers import ExportWorker
        self._export_out_dir = out        # 无损失败要改转码时用得上
        audio_paths = [s.source for s in plan.segments
                       if clip_kind_of(getattr(s, "source", "")) == "video"]
        self._export_worker = ExportWorker(
            self.merge_ffmpeg, plan, out, self.merge_specs, self,
            fix_audio=self._norm_fix_audio(audio_paths))
        # v197: × 改成「先二次确认再取消导出」; 标题栏补一个最小化图标(在 × 左边)。
        # 只对这个导出进度框生效, 写入 / 回滚 / 工具进度框一律不受影响。
        dlg = ProgressDialog("导出中", self._dlg_parent(), cancelable=False,
                             close_guard=self._confirm_cancel_export,
                             minimizable=True)
        dlg.bar.setMaximum(100)
        dlg.bar.setValue(0)
        dlg.label.setText("准备中…")
        dlg.log.setPlainText(
            ("无损拼接：不重新编码，几秒就好。" if plan.ok else
             "重新编码中：这会比较慢，画质也会有损失。"))
        self._export_worker.progress.connect(
            lambda pct, eta: self._export_progress(dlg, pct, eta))
        self._export_worker.result.connect(
            lambda ok, msg: self._on_export_done(dlg, ok, msg))
        self._export_worker.start()
        dlg.exec()

    def _export_progress(self, dlg, pct, eta):
        dlg.bar.setValue(max(0, min(100, int(pct))))
        txt = f"导出中 {int(pct)}%"
        try:
            if eta and float(eta) > 0:
                txt += f"　还剩约 {int(float(eta))} 秒"
        except Exception:
            pass
        dlg.label.setText(txt)

    def _confirm_cancel_export(self, dlg) -> bool:
        """v197: 导出中点右上角 ×(或 Esc) —— 先二次确认, 默认「继续导出」。

        True = 放行关窗; False = 保持窗口(继续导出 / 等取消结果显示)。
        确认框挂当前进度弹窗: 它是应用模态的, 挂到被挡住的主窗口上会点不动。
        """
        if self._export_worker is None:
            return True                 # 已经跑完了, 直接关
        yes = confirm_yes_no(
            dlg, "取消导出",
            "导出还没完成，确定要取消吗？<br><br>"
            "选「是」会停止合成并删掉已经生成的半成品文件；"
            "选「否」继续导出，进度窗口保持不变。",
            red_yes=True, default_yes=False)
        if not yes:
            return False                # 继续导出
        self._cancel_export()
        return False                    # 留住窗口, 让「已取消」的结果显示出来

    def _cancel_export(self):
        if self._export_worker is not None:
            self._export_worker.stop()

    def _on_export_done(self, dlg, ok: bool, msg: str):
        self._export_worker = None
        dlg.finish()
        if dlg.isMinimized():           # 最小化期间跑完: 自动恢复, 把结果摆出来
            dlg.showNormal()
            dlg.raise_()
        if ok:
            dlg.label.setText("导出完成")
            self._export_out = msg
            self.status_label.setText(f"已导出 {os.path.basename(msg)}")
            self._refresh_top_status()
            self._export_done_dialog(msg)
        elif msg == "已取消":
            dlg.label.setText("已取消，没有生成文件")
            QMessageBox.information(self._dlg_parent(), "已取消",
                                    "导出已取消，没有生成任何文件。")
        elif msg.startswith("无损拼接没能成功"):
            # 判定说能无损但 ffmpeg 实际拼不上(容器/编码太挑剔)。
            # 不能只弹一句"失败"就完事 —— 得给台阶: 转码能出片, 但必须用户点头。
            dlg.label.setText("无损拼接没能成功")
            self._offer_reencode(msg)
        else:
            dlg.label.setText("导出失败")
            QMessageBox.warning(self._dlg_parent(), "导出失败", msg)

    def _offer_reencode(self, err: str):
        """无损拼不上时的台阶: 转码能出片, 但慢且有损, 必须用户明确点头。"""
        plan = self._evaluate_lossless()
        if not plan.exportable or not self._export_out_dir:
            QMessageBox.warning(self._dlg_parent(), "导出失败", err)
            return
        mb = QMessageBox(self._dlg_parent())
        mb.setWindowTitle("没能无损拼接")
        mb.setIcon(QMessageBox.Warning)
        mb.setText("这次没能按无损方式拼起来。\n\n"
                   "画面本身不需要重新编码，但直接拼接失败了，"
                   "改用重新编码可以出片 —— 会慢很多，画质也有损失。")
        mb.setDetailedText(err)
        again = mb.addButton("改用重新编码导出（慢 · 画质有损）", QMessageBox.AcceptRole)
        give_up = mb.addButton("算了，不导了", QMessageBox.RejectRole)
        mb.setDefaultButton(give_up)
        mb.setEscapeButton(give_up)
        mb.exec()
        if mb.clickedButton() is not again:
            return
        forced = LS.evaluate(self.edit_timeline, self.merge_specs,
                             ffmpeg_ok=bool(self.merge_ffmpeg))
        forced.ok = False
        forced.mode = "reencode"
        self._run_export(forced, self._export_out_dir)

    def _export_done_dialog(self, path: str):
        mb = QMessageBox(self._dlg_parent())
        mb.setWindowTitle("导出完成")
        mb.setIcon(QMessageBox.Information)
        mb.setText(f"已生成：\n{path}")
        open_btn = mb.addButton("打开文件所在位置", QMessageBox.AcceptRole)
        mb.setDefaultButton(open_btn)
        mb.exec()
        if mb.clickedButton() is open_btn:
            self._reveal_path(path)

    # ------------------------------------------------------------ 右键菜单
    def _on_edit_context_menu(self, clip, pos):
        """时间线右键: 分离音频 / 静音 / 分割 / 删除 / 在文件夹里显示。"""
        if clip is None:
            return
        self._edit_clip_menu(clip).exec(pos)

    def _edit_clip_menu(self, clip) -> QMenu:
        """右键片段的菜单(单独一个方法, 离屏测试能直接看内容, 不用真弹窗)。

        ⚠️ v155: 右键**不改选中**, 所以要分清两件事:
          · 右键那一块(`clip`) —— 只决定"菜单挂在谁身上"、重命名改谁;
          · 真正动手的范围(`targets`) —— 右键的那块在**多选**里就是整组,
            不在多选里就只有它自己。用户 Ctrl 点了几段再右键, 期望的是
            "这几段一起分离/静音/分割/删除", 而不是只动鼠标底下那一段。
        """
        menu = self._hand_menu()
        sel = self.clip_editor.timeline_view.selected_clips()
        picked = [c for c in sel if c is clip]
        targets = list(sel) if picked and len(sel) > 1 else [clip]
        many = len(targets) if len(targets) > 1 else 0
        tl = self.edit_timeline
        # v192: 右键「重命名这个片段…」弹窗撤掉 —— 改成双击片段上方文字带就地改名。
        # 对比的文件集合按选中片段的源文件去重(单选也有 1 个, 照样能开窗口)。
        paths = list(dict.fromkeys(c.source for c in targets if c is not None))

        vids = [c for c in targets if c.kind == "video"]
        det = [c for c in vids if c.detached]
        live = [c for c in vids if not c.detached]
        if vids:
            # ⚠️ v150: 已经分离过的就**不再给**「分离音频」—— 声音已经在下面那条
            #    轨上了, 再点一次只会多出第二份。这时只留「取消分离」。
            # ⚠️ "分离" / "静音" 是两件事(v148 + v150):
            #    分离 = 声音搬到下面那条轨(一个字节都没丢); 静音 = 这一段不要声音。
            #    所以标签只写一件事, 不再写"删除原音（静音）"这种混着的说法。
            if det:
                menu.addAction("取消分离",
                               lambda: self._undo_detach_many(det))
            if live:
                # v187: 菜单文字只留动作本身, 括号里的说明/段数全撤 —— 悬停
                # 看提示就够了, 一行长句把菜单撑得又宽又难扫。
                act = menu.addAction("分离音频",
                                     lambda: self.edit_extract_audio(live))
                act.setToolTip("声音抽到下面一条轨，不重新编码")
            if live:
                if all(c.muted for c in live):
                    menu.addAction("取消静音",
                                   lambda: self._set_muted_many(live, False))
                else:
                    menu.addAction("静音",
                                   lambda: self._set_muted_many(live, True))
            menu.addSeparator()

        # v187: 「音量」挪到工具条「声波」右边的拖动条上, 这里不再摆菜单项。

        # v193: 三组固定顺序（分割、对齐关键帧、按顺序编号）（移除）（复制）。
        # 括号里的「（N 段）」全部撤掉 —— 选中了几段靠选中态看得见, 菜单不必复读。
        act_split = menu.addAction("在裁剪线处分割",
                                   lambda: self._split_clips_at(targets))
        # ⚠️ 这里只能"问能不能切", 绝不能拿 split_at() 的返回值当判断 ——
        #    那个函数**真的会切**: 以前光是右键弹出菜单, 片段就被切成两段了
        #    (用户看到的是"分离音频之后视频变成两段", v146)。
        act_split.setEnabled(any(tl.can_split_at(c, tl.playhead) for c in targets))
        if many > 1:
            menu.addAction("对齐关键帧",
                           lambda: self._batch_align_keyframes(targets))
            menu.addAction("按顺序编号…",
                           lambda: self._batch_rename_clips(targets))
            aud = [c for c in targets if c.kind == "audio"]
            if aud:
                menu.addAction("倍速…", lambda: self._batch_speed(aud))
        menu.addSeparator()

        # 第 2 组: 移除
        if many > 1:
            menu.addAction(f"移除选中的 {many} 个片段", self.edit_delete)
        menu.addAction("移除此片段", lambda: self._delete_clip(clip))
        menu.addSeparator()

        # 第 3 组: 复制
        menu.addAction("复制这个片段" if many <= 1 else f"复制选中的 {many} 段",
                       lambda: self._copy_clips(targets))
        menu.addAction("再复制几份…" if many <= 1 else f"把这 {many} 段再复制几份…",
                       lambda: self._repeat_clips(targets))
        menu.addSeparator()
        menu.addAction("截图：把这一帧存成图片", self._edit_capture_frame)
        act_cover = menu.addAction("设为封面（写入视频文件）",
                                   self._edit_set_cover)
        act_cover.setEnabled(clip.kind == "video")
        if clip.kind != "video":
            act_cover.setToolTip("只有视频文件才有封面")
        menu.addSeparator()
        menu.addAction("新增一条轨道", self._add_track)
        act_del = menu.addAction(f"删除「{tl.track_label(clip.track)}」",
                                 lambda: self._remove_track(clip.track))
        act_del.setEnabled(not any(c.track == clip.track for c in tl.clips)
                           and tl.track_count > 1)
        act_clear = menu.addAction(f"清空「{tl.track_label(clip.track)}」",
                                   lambda: self._clear_track(clip.track))
        act_clear.setEnabled(any(c.track == clip.track for c in tl.clips))
        menu.addSeparator()
        menu.addAction("在文件夹里显示", lambda: self._reveal_clip(clip))
        # v182: 「对比」+「属性」合成最后一组 —— 这条横线下面不许再夹别的项
        # v192: 「发送到对比N」平铺在横线上方的操作组里
        self._add_compare_send_actions(menu, paths)
        menu.addSeparator()
        act = menu.addAction(self._compare_label(paths),
                             lambda: self._open_compare_for_paths(paths))
        if act is not None:
            act.setEnabled(bool(paths))
        menu.addAction("属性…", lambda: self._show_clip_props(clip))
        return menu

    def _split_clips_at(self, clips):
        """在裁剪线处切开这几段(v155: 多选时整组一起切)。"""
        if not isinstance(clips, (list, tuple)):
            clips = [clips]
        tl = self.edit_timeline
        targets = [c for c in clips
                   if c is not None and tl.can_split_at(c, tl.playhead)]
        if not targets:
            self.status_label.setText("把裁剪线移到片段中间再切")
            return
        made = []
        for c in targets:
            right = tl.split_at(c, tl.playhead)
            if right is not None:
                made.append(right)
        if not made:
            self.status_label.setText("把裁剪线移到片段中间再切")
            return
        tv = self.clip_editor.timeline_view
        self._after_edit_changed()
        tv.select_clips(made)          # 切完选中最右边那几段(接着往下切顺手)
        self._on_edit_clip_selected(made[0])
        self.status_label.setText(
            "已切开，两段都在原处" if len(made) == 1
            else f"已切开 {len(made)} 段，都在原处")

    def _reveal_clip(self, clip):
        self._reveal_path(clip.source)

    def _show_clip_props(self, clip):
        """片段属性(v170): 时间线和右栏卡片的右键菜单都开这个。

        v174: 完全采用「合并」属性窗口的格式 —— 左上缩略图(点了用默认播放器放)、
        顶部文件名、视频信息两列; 且**非模态**, 不锁死剪辑操作, 可以同时开几个。
        """
        from .tools_dialog import ClipPropsDialog
        if clip is None:
            return
        try:
            label = self.edit_timeline.track_label(clip.track)
        except Exception:
            label = ""
        try:
            fps = float(self.clip_editor.timeline_view._fps_map.get(clip.source) or 0.0)
        except Exception:
            fps = 0.0
        self._open_clip_props_dialog(
            lambda parent: ClipPropsDialog(parent, clip=clip,
                                           track_label=label, fps=fps))

    def _rename_clip(self, clip):
        """改片段在时间线上显示的名字(不改文件名, 也不动源文件)。"""
        from .dialogs import prompt_text
        name = prompt_text(self._dlg_parent(), "重命名片段", "片段名称", clip.name)
        self._restore_clip_front()      # 弹窗关掉后剪辑窗口要回到前面
        name = (name or "").strip()
        if not name or name == clip.name:
            return
        clip.name = name
        self.clip_editor.timeline_view.refresh()
        self._refresh_clip_list()
        self.clip_panel.set_clip(clip, self.edit_timeline,
                                 {p.path: p for p in self.model.photos})
        # 改名也算一步编辑(v158): 名字现在进撤销快照了, 这里必须记账,
        # 否则撤销退掉的是更早那步, 名字还留着新的。
        self._edit_commit()
        self.status_label.setText(f"已改名为：{name}")

    def _on_clip_name_changed(self, clip):
        """v192: 轨道 / 右栏卡片上的就地改名完成后, 两边同步并记一步撤销。

        两个入口(双击片段文字带、点卡片小铅笔)都汇到这里, 免得一个改了
        另一个还挂着旧名字。改名也算一步编辑, 必须进撤销快照。
        """
        if clip is None:
            return
        try:
            self.clip_editor.timeline_view.update()
            self._refresh_clip_list()
            self.clip_panel.set_clip(clip, self.edit_timeline,
                                     {p.path: p for p in self.model.photos})
            self._edit_commit()
            self.status_label.setText(f"已改名为：{clip.name}")
        except Exception:
            pass

    def _undo_detach(self, clip):
        """取消分离: 把抽出来的音频片段删掉, 声音回到原视频(不是"静音"的反操作)。

        ⚠️ 走 `tl.remove()` 删音频 —— 它会自动把原视频的 detached 还原,
        不用(也不能)自己再置一次, 否则两处逻辑会打架。
        """
        self._undo_detach_many([clip])

    def _undo_detach_many(self, clips):
        """取消分离(可一次好几段, v155)。"""
        tl = self.edit_timeline
        clips = [c for c in (clips or []) if c is not None]
        if not clips:
            return
        for clip in clips:
            auds = [c for c in tl.clips
                    if c.kind == "audio"
                    and (getattr(c, "src_parent", "") or "") == clip.source]
            if auds:
                for a in list(auds):
                    tl.remove(a)
            else:
                clip.detached = False    # 音频片段已经被删了: 直接还原
        self._after_edit_changed()
        self.clip_editor.show_clip(clips[0])
        self.status_label.setText(
            f"已取消分离：{clips[0].name}（声音回到视频里）" if len(clips) == 1
            else f"已取消分离：{len(clips)} 段（声音都回到视频里了）")

    def _set_muted(self, clip, on: bool):
        self._set_muted_many([clip], on)

    def _set_muted_many(self, clips, on: bool):
        """静音 / 取消静音(可一次好几段, v155)。"""
        clips = [c for c in (clips or []) if c is not None]
        if not clips:
            return
        for clip in clips:
            clip.muted = bool(on)
        self.clip_editor.timeline_view.update()
        self.clip_panel.set_clip(clips[0], self.edit_timeline,
                                 {p.path: p for p in self.model.photos})
        # ⚠️ 必须记一步撤销(v158): 静音不进撤销栈的话, 用户"静音一段 → Ctrl+Z"
        #    退掉的是**更早**那步(常常是"加入片段"), 看起来就是"按撤销把片段弄没了"。
        self._edit_commit()
        self._refresh_top_status()
        self.status_label.setText(
            f"已{'静音' if on else '取消静音'}：{clips[0].name}" if len(clips) == 1
            else f"已{'静音' if on else '取消静音'}：{len(clips)} 段")

    # ------------------------------------------------------------ 分离音频
    def edit_extract_audio(self, clips):
        """把视频的音轨原样抽成独立音频文件, 再作为片段放到音频轨。

        v155: 可以一次传好几段(Ctrl 点选后右键「分离音频」) —— 抽音轨要起
        ffmpeg, 一段一段排队来(只有一条 `_audio_worker`), 抽完一段自动接下一段。
        """
        if clips is None:
            return
        if not isinstance(clips, (list, tuple)):
            clips = [clips]
        todo = [c for c in clips
                if c is not None and c.kind == "video" and not c.detached]
        if not todo:
            return
        if not self.merge_ffmpeg:
            self.status_label.setText("没有可用的 ffmpeg，先去设置里指定")
            return
        if self._audio_worker is not None and self._audio_worker.isRunning():
            self.status_label.setText("正在抽上一条的音轨…")
            return
        self._audio_queue = list(todo)
        self._audio_total = len(todo)
        self._audio_done = 0
        self._audio_start_next()

    def _audio_start_next(self):
        """从队列里取下一段开始抽; 抽空了就收尾。"""
        queue = getattr(self, "_audio_queue", None) or []
        if not queue:
            return
        clip = queue.pop(0)
        try:
            from ..merge.workers import ExtractAudioWorker
            # ⚠️ 第 4 个位置参数是 parent(线程要在 finished 后才释放), 别塞别的
            w = ExtractAudioWorker(self.merge_ffmpeg, self.merge_ffprobe,
                                   clip.source, parent=self)
        except Exception as e:
            self.status_label.setText(f"抽音轨失败：{e}")
            self._audio_start_next()
            return
        w.result.connect(lambda ok, out, msg, c=clip:
                         self._on_audio_extracted(c, ok, out, msg))
        self._audio_worker = w
        total = int(getattr(self, "_audio_total", 1) or 1)
        done = int(getattr(self, "_audio_done", 0) or 0)
        self.status_label.setText(
            f"正在抽出音轨：{clip.name} …" if total <= 1
            else f"正在抽出音轨（{done + 1}/{total}）：{clip.name} …")
        w.start()

    def _on_audio_extracted(self, clip, ok: bool, out: str, msg: str):
        self._audio_worker = None
        self._audio_done = int(getattr(self, "_audio_done", 0) or 0) + 1
        if not ok:
            self.status_label.setText(f"分离音频失败：{msg}")
            self._audio_start_next()
            return
        # 抽出来的音频放在原片段**下面**那条轨、同一时间起点。
        # ⚠️ 下面没轨就加一条: 塞回原轨的话会排到视频后面, 时间线凭空变长一倍,
        #    看着就像"视频被切成两段"(v146)。
        src_track = getattr(clip, "track", TRACK_MAIN)
        tl = self.edit_timeline
        target = src_track + 1
        if target >= tl.track_count:
            if tl.track_count < 12:
                tl.add_track()
            else:
                target = tl.track_count - 1
        # ⚠️ 时长当场读一次(`_ensure_duration`): 只查缓存的话刚生成的文件查不到,
        #    片段长度就是 0 —— 时间线上是一根线、右栏写 "0.0s"(v146)。
        dur = self._ensure_duration(out)
        # 声音要跟原片段**对齐**: 原片段是从源文件第 3 秒剪出来的, 抽出的音频也得
        # 从第 3 秒开始、到同一个收尾点 —— 否则接缝处声画错位。
        a_in = max(0.0, float(getattr(clip, "src_in", 0.0) or 0.0))
        a_out = float(getattr(clip, "src_out", 0.0) or 0.0)
        if a_out <= a_in:
            a_out = dur
        if dur > 0:
            a_in = min(a_in, dur)
            a_out = min(max(a_out, a_in + 0.05), dur)
        # v189: 视频改过帧率/倍速(「帧率/倍速」按钮或拖边)后, 分离出来的声音
        # 要跟画面一样快 —— 音频文件本身还是原速, 倍速记在片段上(导出时才落地)。
        # 两边 src_in/src_out 相同、speed 相同 → 时长也相同, 声画不会错开。
        # 只在分离这一刻继承; 之后两边各自独立改(用户定的口径)。
        src_speed = float(getattr(clip, "speed", 1.0) or 1.0)
        if src_speed <= 0:
            src_speed = 1.0
        new_clip = Clip(source=out, kind="audio", track=target,
                        src_in=a_in, src_out=a_out,
                        src_duration=dur or a_out,
                        speed=src_speed,
                        src_parent=clip.source)
        self.edit_timeline.clips.append(new_clip)
        self.edit_timeline.place(new_clip, target,
                                 getattr(clip, "start", 0.0))
        # ⚠️ 这里是**分离**, 不是静音(v148): 声音只是搬到下面那条轨上了, 一个字节
        # 都没丢。标 muted 的话界面会写"已静音"、无损检查会报"N 段被静音了", 还把
        # "取消静音"当成修复 —— 用户点了就变成两份声音。所以单用一个 detached。
        # (出声判定统一看 `Clip.silent`: 静音 / 分离 都不出声)
        clip.detached = True
        self._remember_extra(out)
        self._after_edit_changed()
        total = int(getattr(self, "_audio_total", 1) or 1)
        self.status_label.setText(
            f"已分离音轨：{os.path.basename(out)}（{msg}）；"
            f"声音放到下面那条轨了，原视频不再重复出声"
            f"（放在「{self._out_dir_hint(out)}」）" if total <= 1 else
            f"已分离音轨（{getattr(self, '_audio_done', 1)}/{total}）："
            f"{os.path.basename(out)}")
        # 队列里还有下一段就接着抽(v155)
        self._audio_start_next()

    # ------------------------------------------------------------ 播放
    def edit_toggle_play(self):
        """空格 / 点预览画面: 播放 ↔ 暂停。

        素材框里正选着一个素材(没上轨)时, 播的就是这个素材;
        否则播时间线(从裁剪线开始)。
        """
        if self.clip_editor.is_playing():
            self._edit_pause()
        elif self._media_preview:
            self._play_media_preview(self._media_pos)   # 从刚才停的那一秒接着播
        else:
            self._edit_play()

    def _edit_play(self):
        tl = self.edit_timeline
        if not tl or not tl.clips:
            return
        if tl.playhead >= tl.total_duration() - 0.01:
            tl.playhead = 0.0
        # 「跟随移动」每次开播都按"裁剪线现在的位置"重新钉一次
        self.clip_editor.timeline_view.reset_follow_anchor()
        self.clip_editor.set_playing(True)
        # v159: 播整条轨道 → 预览下沿那条进度条换成"所有轨道的总进度"
        self._begin_timeline_scrub()
        # 播放头按墙上时间走(和画面帧率解耦), 见 _on_smooth_tick
        self._clock_t = time.perf_counter()
        self._smooth_timer.start()
        self._play_segment()

    def _edit_pause(self):
        self._seek_timer.stop()
        self._seek_playing = False
        self._smooth_timer.stop()
        self._stop_player()
        self._stop_audio()
        self._play_timer.stop()
        self._media_ui_timer.stop()
        self._media_playing = False
        # v205: 全关剪辑会话后 boot 是空壳 ClipSession, clip_editor 为 None ——
        # 此时「返回排序」(leave_merge) 仍会无条件调到这里。定时器/播放器/声音
        # 这些通用清理上面已做完且不依赖剪辑现场; 没有编辑器就没有画面和时间线
        # 要停, 直接返回, 别让 None.set_playing / clear_frame 抛 AttributeError。
        if self.clip_editor is None:
            return
        self.clip_editor.set_playing(False)
        # 停下后画面要停在裁剪线那一秒(否则最后一帧会一直挂着)
        self.clip_editor.clear_frame()
        if self.edit_mode:
            if self._media_preview:
                # ⚠️ 别把位置清零: 暂停只是"停在这一秒", 再播要从这儿接着走
                # (以前清零 → 再播永远从头开始, 拖进度条也白拖)
                self.clip_editor.set_scrub_pos(self._media_pos)
                self._show_media_frame_at(self._media_pos, self._media_preview)
            else:
                self._sync_scrub()        # 进度条停在停下的那一秒
                self._refresh_preview_at(self.edit_timeline.playhead)

    def _stop_player(self):
        p = self._player
        self._player = None
        self._playing_clip = None
        # 播放线程要真停下才能删(删正在跑的 QThread 会让整个进程 abort)
        self._stop_thread(p, 1500)

    # ------------------------------------------------------------ 声音
    def _sounder(self):
        """常驻的声音线程(第一次要用才建, 之后一直复用)。

        ⚠️ 绝不"播一段 new 一个线程": 拖进度条/切片段时一秒好几次, 线程疯狂
        创建销毁是闪退的老路子; 而且声卡反复开关会踩到驱动的缓冲区回收。
        """
        s = self._sound
        if s is not None:
            s.set_ffmpeg(self.merge_ffmpeg or "")
            return s
        try:
            from .audio_out import SoundPlayer, audio_available
            if not audio_available():
                return None
        except Exception:
            return None
        try:
            s = SoundPlayer(self.merge_ffmpeg or "", self)
            s.failed.connect(self._on_sound_failed)
            self._sound = s
            s.start()
        except Exception:
            self._sound = None
            s = None
        return s

    def _stop_audio(self):
        """立刻静音(线程和声卡都留着, 下次马上还能播)。"""
        self._sound_sig = None        # 掐掉了: 下一次同样的段也要真的重播
        s = self._sound
        if s is None:
            return
        try:
            s.silence()
        except Exception:
            pass

    def _play_sound(self, jobs, start=None, end=None):
        """放这些源区间的声音: [(源文件, 起, 止), ...], 同一时刻可以好几路叠着。

        (src, start, end) 三个参数的老写法也认 —— 改签名最容易漏的就是调用点,
        漏一处就是"一点播放就报错"。


        ⚠️ 为什么不用 QtMultimedia: 打包产物里根本没有 Qt6Multimedia.dll,
        引 QAudioOutput 就是"一点播放就崩"。这里用 ctypes 直接调系统 winmm 的
        waveOut 放 PCM(ffmpeg 先把音频解成 s16le 吐给管道)。
        """
        if isinstance(jobs, tuple):          # 兼容老的单个 (src, start, end)
            jobs = [jobs]
        elif start is not None or end is not None:
            jobs = [(jobs, start if start is not None else 0.0,
                     end if end is not None else 0.0)]
        jobs = [j for j in (jobs or []) if j and j[0]]
        if not jobs or not self.merge_ffmpeg:
            return
        # 同一段已经在响了就别再启一次 ffmpeg(空隙处播放头每 0.1s 会重新走一遍
        # _play_segment, 不挡住的话就是一秒重启十次 ffmpeg)
        sig = tuple((s, round(a, 2), round(b, 2)) for s, a, b in jobs)
        if sig == getattr(self, "_sound_sig", None):
            return
        self._sound_sig = sig
        try:
            s = self._sounder()
            if s is None:
                return
            s.request(jobs)
        except Exception:
            pass

    def _sound_jobs_at(self, t: float) -> list:
        """t 这一刻所有该出声的源区间。

        ⚠️ 光看"最上面那条轨"是不够的: **分离音频之后视频是静音的, 声音在下面
        那条音频轨上**, 只播 top clip 就是"分离完反而听不见了"。所以要把这一刻
        每一条轨上没被静音、又确实有声音的片段都收进来(多路由 amix 叠在一起)。
        """
        tl = self.edit_timeline
        if not tl:
            return []
        jobs: list = []
        seen: set = set()
        for tr in range(getattr(tl, "track_count", 1) or 1):
            c = tl.clip_at(tr, t)
            # 静音的、声音已经被分离走的, 都不出声(出声判定统一看 Clip.silent)
            if c is None or getattr(c, "silent", False):
                continue
            if c.kind == "image":
                continue
            if c.kind != "audio" and not self._source_has_audio(c.source):
                continue                     # 这个视频本来就没音轨
            a = c.src_in + (t - tl.clip_start(c))
            b = c.src_out
            if b - a <= 0.05:
                continue
            key = (c.source, round(a, 3), round(b, 3))
            if key in seen:
                continue
            seen.add(key)
            jobs.append((c.source, a, b))
        if len(jobs) > 1:
            # 要叠在一起了: 每一路都必须**确定**有音轨, 否则 amix 整段报错
            jobs = [j for j in jobs if self._certain_audio(j[0])]
        return jobs

    def _source_has_audio(self, src: str) -> bool:
        """这个文件有没有音轨(拿不准就当有: 播不出来只是没声, 不会出错)。"""
        if not src:
            return False
        cache = getattr(self, "_audio_known", None)
        if cache is None:
            cache = self._audio_known = {}
        if src in cache:
            return cache[src]
        spec = self._spec_of(src)
        if spec:
            cache[src] = bool(spec.get("audio_codec"))
            return cache[src]
        return True                          # 还没探过: 乐观放行

    def _certain_audio(self, src: str) -> bool:
        """必须拿准(多轨混音时): 缓存里没有就当场问一次 ffprobe。

        ⚠️ amix 里混进一个**没有音轨**的输入, ffmpeg 会整条命令报错退出 —— 那就
        变成"叠了两条轨之后反而一声都没有"。单路不受影响, 所以只有混音时才查。
        """
        cache = getattr(self, "_audio_known", None)
        if cache is None:
            cache = self._audio_known = {}
        if src in cache:
            return cache[src]
        spec = self._spec_of(src)
        if spec:
            cache[src] = bool(spec.get("audio_codec"))
            return cache[src]
        has = True
        exe = self.merge_ffprobe
        if exe and src and os.path.isfile(src):
            try:
                from ..merge import proc as _proc
                res = _proc.run([exe, "-v", "error", "-select_streams", "a:0",
                                 "-show_entries", "stream=codec_type",
                                 "-of", "csv=p=0", src], timeout=8)
                has = "audio" in (res.stdout or "")
            except Exception:
                has = True
        cache[src] = has
        return has

    def _on_sound_failed(self, msg: str):
        if getattr(self, "_sound_warned", False):
            return
        self._sound_warned = True
        self.status_label.setText(f"声音放不出来：{msg}")

    def _play_segment(self):
        """从当前播放头开始播一段; 播完自动接下一段(跨片段连续播放)。"""
        tl = self.edit_timeline
        if not self.clip_editor.is_playing():
            return
        t = tl.playhead
        self._play_from = t
        self._stop_audio()               # 上一段的声音先收掉(换段了)
        # 同一时刻多条轨都有画面时, 取最上面那条(轨号小的优先)
        clip = tl.top_clip_at(t)
        if clip is None:
            # 这一刻没有任何画面(只有音频)或已经到末尾
            if t >= tl.total_duration() - 0.01:
                self._edit_pause()
                return
            # 轨上只剩声音(画面那段被删了)时也得有声音可听
            self._play_sound(self._sound_jobs_at(t))
            self._play_timer.start()     # 推进交给 _smooth_timer(v156)
            return
        self._playing_clip = clip
        start = tl.clip_start(clip)
        end = tl.clip_end(clip)
        # ⚠️ 声音不能只看上面这条画面轨: 分离音频之后画面那段是静音的, 声音躺在
        # 下面那条音频轨上 —— 不一起收进来的话, 分离完就"没声音了"。
        jobs = self._sound_jobs_at(t)
        if clip.kind == "video" and self.merge_ffmpeg:
            spec = self.merge_specs.get(clip.source) or {}
            w, h = self._preview_out_size(spec, PLAY_MAX_W)
            # v184: 片段改过倍速后, 时间线上 1 秒对应源文件 speed 秒。以前这里
            # 按 1:1 取起点, 一按播放就从片段开头重放, 看着像"倍速没生效"。
            sp = float(clip.speed or 1.0) or 1.0
            src_in = clip.src_in + (t - start) * sp
            self._play_timer.stop()
            try:
                p = FramePlayer(self.merge_ffmpeg, self)
                self._player_gen += 1
                p._gen = self._player_gen
                p.frameReady.connect(self._on_player_frame)
                p.ended.connect(self._on_player_ended)
                p.failed.connect(self._on_player_failed)
                self._player = p
                self._play_sound(jobs)
                p.play(clip.source, src_in, clip.src_out, w, h, sp)
            except Exception as e:
                self.status_label.setText(f"播放失败：{e}")
                self._edit_pause()
            return
        # 图片(静帧) / 音频(没画面): 只推播放头, 预览窗显示该片段的样子
        self.clip_editor.show_clip(clip)
        self._play_sound(jobs)
        self._play_timer.start()

    def _on_player_frame(self, img):
        if not self.clip_editor.is_playing():
            return
        # 换过代的播放器(比如刚拖了裁剪线, 旧的那个还没停)送来的帧不能画,
        # 否则它会把旧位置的画面盖在新位置上。
        p = self.sender()
        if p is not None and getattr(p, "_gen", -1) != self._player_gen:
            return
        self.clip_editor.show_frame(img)
        # ⚠️ 这里**不再**推播放头(v156): 播放头由 _smooth_timer 按墙上时间推进。
        #    跟着解码帧走的话, 15fps 的预览就是每 66ms 跳 4 像素, 轨道看着一顿一顿。

    def _on_player_failed(self, msg: str):
        """ffmpeg 起不来/读不出: 整个停下, 绝不重试(重试就是原地死循环)。"""
        p = self.sender()
        if p is not None and getattr(p, "_gen", -1) != self._player_gen:
            return                       # 已经换过代的旧播放器, 不用管
        self._player = None
        self.status_label.setText(msg)
        self._edit_pause()

    def _on_player_ended(self):
        """一段放完: 把播放头挪到这段末尾, 接着播下一段。

        ⚠️ 播不出来(文件没了/ffmpeg 不在)也必须往前挪一段再试,
        否则会在原地反复重启 ffmpeg —— 那就是死循环。
        """
        p = self.sender()
        if p is not None and getattr(p, "_gen", -1) != self._player_gen:
            return                       # 换过代的旧播放器: 别拿它接着播
        self._player = None
        if self._media_playing:
            # 素材播完了: 停在它的第一帧, 时间线一根毫毛都不动
            self._media_playing = False
            self._media_ui_timer.stop()
            self._stop_audio()
            self.clip_editor.set_playing(False)
            self.clip_editor.clear_frame()
            if self._media_preview:
                self._media_pos = 0.0
                self.clip_editor.set_scrub_pos(0.0)
                self._show_media_frame(self._media_preview)
            return
        clip = self._playing_clip
        self._playing_clip = None
        if not self.clip_editor.is_playing():
            return
        tl = self.edit_timeline
        if clip is not None:
            tl.playhead = max(tl.playhead + 0.05, tl.clip_end(clip))
            self.clip_editor.timeline_view.follow_scroll()
            self.clip_editor.timeline_view.update()
            self.clip_editor._refresh_time_label()
        if tl.playhead >= tl.total_duration() - 0.01:
            self._edit_pause()
            return
        self._play_segment()

    def _on_play_tick(self):
        """图片/音频片段: 只看"是不是到段尾了", 推进由 _smooth_timer 做。"""
        tl = self.edit_timeline
        if not self.clip_editor.is_playing():
            self._play_timer.stop()
            return
        clip = self._playing_clip
        if clip is not None and tl.playhead >= tl.clip_end(clip) - 0.005:
            tl.playhead = max(tl.playhead + 0.05, tl.clip_end(clip))
            self._playing_clip = None
            self._play_segment()
            return
        if tl.playhead >= tl.total_duration() - 0.01:
            self._edit_pause()

    def _on_smooth_tick(self):
        """播放头按**墙上时间**往前走(≈60Hz), 与画面解码帧率无关。

        ⚠️ 以前是"来一帧画面就推 1/PLAY_FPS 秒"(PLAY_FPS=15), 于是时间线每 66ms
        才动一次、一次挪 4 个像素; 开着「跟随移动」时缩略图和刻度数字都是一顿一顿
        往前蹦。现在画面照旧 15fps, 轨道改成 60Hz 匀速滚 —— 动画顺滑靠的是**位置
        更新频率**, 不是解码帧率。
        """
        if not self.clip_editor.is_playing() or self._media_playing:
            self._smooth_timer.stop()
            return
        now = time.perf_counter()
        dt = now - self._clock_t
        self._clock_t = now
        if dt <= 0.0 or dt > 0.5:
            dt = 0.016            # 卡了一下/刚开播: 别一下跳一大截
        self._advance_playhead(dt)

    def _advance_playhead(self, delta: float):
        tl = self.edit_timeline
        end = tl.total_duration()
        clip = self._playing_clip
        if clip is not None:
            # 视频段播完要等 _on_player_ended 来切下一段; 这里先停在段尾, 免得
            # 播放头冲过头把下一段的开头"吃掉"。图片/音频段到点了由 _on_play_tick 切。
            try:
                end = min(end, tl.clip_end(clip))
            except Exception:
                pass
        tl.playhead = max(0.0, min(end, tl.playhead + delta))
        tv = self.clip_editor.timeline_view
        tv.follow_scroll()                      # 「跟随移动」开着就滚轨道
        tv.update()
        self.clip_editor._refresh_time_label()
        self._sync_scrub()                      # 播着的时候进度条跟着走

    # ---------------------------------------------------- 右栏片段清单(v140)
    def _ordered_clips(self) -> list:
        """轨道上的片段按"先后顺序"排一遍: 先按轨道, 再按起点。"""
        return sorted(self.edit_timeline.clips,
                      key=lambda c: (int(c.track), float(c.start), c.name))

    def _refresh_clip_list(self):
        """把排好序的片段交给右栏清单。"""
        try:
            cl = self.clip_editor.clip_list
            # 正在拖的时候别重建 —— 会把用户手里正拎着的那张卡片抽走
            # (v141 起拖动是自己接管的, Qt 的 state() 不会变, 所以问面板自己)
            if cl.is_dragging():
                return
        except Exception:
            return
        clips = self._ordered_clips()
        by_path = getattr(self.clip_editor, "_photos_by_path", {}) or {}
        tv = self.clip_editor.timeline_view
        thumbs = {}
        for c in clips:
            src = str(getattr(c, "source", "") or "")
            if not src or src in thumbs:
                continue
            p = by_path.get(src)
            pm = getattr(p, "thumb", None) if p is not None else None
            if pm is None or pm.isNull():
                # 主列表那张缩略图是逐张补出来的(视频尤其慢) —— 没等到就用
                # 时间线已经抽到的帧, 卡片上别留一块空白占位。
                pm = tv.cached_frame(src)
            if (pm is None or pm.isNull()) and getattr(c, "kind", "") == "image":
                # 图片没有抽帧流程: 直接把原图缩到卡片大小读一张
                pm = self.clip_editor._large_image(src, 96, 96)
            if pm is not None and not pm.isNull():
                thumbs[src] = pm
        cl.set_clips(clips, thumbs)
        cl.select_clip(self.clip_editor.selected_clip())

    def _clip_list_fill_thumb(self, source: str):
        """时间线抽到一帧 → 右栏卡片如果还空着就补上(不重建整张表)。"""
        try:
            pm = self.clip_editor.timeline_view.cached_frame(source)
            if pm is not None and not pm.isNull():
                self.clip_editor.clip_list.set_thumb(source, pm)
        except Exception:
            pass

    def _on_clip_list_picked(self, clip, force_jump: bool = False):
        """点了右栏的卡片(v159 改了口径, 之前单击无条件跳到起点)。

        · 裁剪线**不在**这一段上 → 单击就跳到这一段开头(想找哪段点一下就到);
        · 裁剪线**正好在这一段里** → 单击只把它选中(不动裁剪线, 方便原地看/改),
          双击才把裁剪线拉回这一段开头。
        """
        if clip is None or clip not in self.edit_timeline.clips:
            return
        tl = self.edit_timeline
        if force_jump or not self._playhead_in_clip(clip):
            tl.playhead = max(0.0, float(clip.start))
            self.clip_editor.set_playhead(tl.playhead)
        self.clip_editor.timeline_view.select_clip(clip)
        # select_clip 自己不发 clipSelected(只有鼠标点轨道才发), 这里补一次:
        # 属性面板 / 工具按钮 / 右栏高亮都是靠它刷新的。
        self._on_edit_clip_selected(clip)
        # v141: 点卡片 = "现在看的是这一段" → 中间浮出**这一段自己**的进度条
        # (0 ~ 片段长度), 可以拖着在这一段里来回看。
        self._begin_clip_scrub(clip)
        self._refresh_top_status()

    def _on_clip_list_sel_changed(self, clips):
        """右栏「轨道片段」里多选了几段(v180) → 轨道上同步选中它们。

        ⚠️ 只同步"选中", **不动裁剪线**: 用户是在挑要操作哪几段, 不是要跳去看。
        裁剪线归 `_on_clip_list_picked` 管(单击那张卡才跳)。
        """
        clips = [c for c in (clips or []) if c is not None]
        if not clips:
            # v181: 右栏点空白取消多选 → 轨道上也要跟着清掉,
            # 否则"看着取消了, 按 Del 还会删掉刚才那几段"。
            try:
                self.clip_editor.timeline_view.select_clip(None)
                self.clip_editor.clip_list.select_clip(None)
                self._sync_edit_tool_buttons()
                self.clip_panel.set_clip(None, self.edit_timeline,
                                         {p.path: p for p in self.model.photos})
                self.status_label.setText("已取消选择")
            except Exception:
                pass
            return
        try:
            tv = self.clip_editor.timeline_view
            tv.select_clips(clips)
            # select_clips 不发 clipSelected(只有鼠标点轨道才发), 这里补一次,
            # 让属性面板 / 工具按钮的可用状态跟着更新。
            self._on_edit_clip_selected(clips[0])
            self.status_label.setText(f"已选中 {len(clips)} 个片段")
        except Exception:
            pass

    def _on_clip_list_menu(self, clip, gpos, build_only=False):
        """右栏卡片上点右键(v152): 重命名 / 删除 / 属性。

        为什么不在轨道上点: 片段一多时间线横向滚得很远, 想改某一段的名字
        得先把它滚到眼前。右栏这一列是竖着一目了然的, 在这儿能直接改。

        build_only: 只把菜单造出来返回(离屏验证用, 不弹真菜单)。
        """
        # v181: 多选状态面板自己记着, 这里问一句 —— 框选/Ctrl 多选后右键,
        # 「对比」要真的能点; 右键落在空白处(clip=None)也要弹整组菜单。
        try:
            group = list(self.clip_editor.clip_list.selected_clips())
        except Exception:
            group = []
        if clip is None and not group:
            return None
        if clip is not None and (len(group) < 2 or not any(c is clip for c in group)):
            group = []
        menu = self._clip_list_menu(clip, group)
        if build_only:
            return menu
        menu.exec(gpos)
        return None

    def _clip_list_menu(self, clip, group=None) -> QMenu:
        """右栏卡片的菜单(抽出来单独一个方法, 离屏测试直接看内容)。

        v181: group 有多段时按"批量"出菜单 —— 对比用这些片段的源文件,
        删除用轨道上已同步的选区。group 为空/单段时维持原来的单段菜单。
        """
        tl_clips = list(getattr(getattr(self, "edit_timeline", None),
                                "clips", None) or [])
        group = [c for c in (group or [])
                 if c is not None and (not tl_clips or any(c is x for x in tl_clips))]
        multi = len(group) > 1
        batch = multi or (clip is None and bool(group))   # 空白右键也算整组菜单
        menu = self._hand_menu()

        # v193: 三组固定顺序（对齐关键帧、按顺序编号）（移除）（复制）。
        # 右栏菜单本来没有「分割」, 不新增; 括号里的段数全部撤掉。
        if multi:
            live = [c for c in group
                    if c.kind in ("video", "audio") and not c.detached]
            all_muted = bool(live) and all(c.muted for c in live)
            if live:
                menu.addAction(
                    "取消静音" if all_muted else "静音",
                    lambda: self._set_muted_many(live, not all_muted))
            # v187: 「音量」菜单项撤掉, 改用工具条上的拖动条。
            menu.addAction("对齐关键帧",
                           lambda: self._batch_align_keyframes(group))
            menu.addAction("按顺序编号…",
                           lambda: self._batch_rename_clips(group))
            aud = [c for c in group if c.kind == "audio"]
            if aud:
                menu.addAction("倍速…", lambda: self._batch_speed(aud))
            menu.addSeparator()

        # 第 2 组: 移除
        if multi:
            menu.addAction(f"移除选中的 {len(group)} 个片段",
                           lambda: self._delete_clip_group(group))
        elif clip is not None:
            menu.addAction("移除此片段", lambda: self._delete_clip(clip))
        if multi or clip is not None:
            menu.addSeparator()

        # 第 3 组: 复制 / 重复
        ops = group if (multi or clip is None) else [clip]
        if ops:
            menu.addAction(
                "复制这个片段" if len(ops) <= 1 else f"复制选中的 {len(ops)} 段",
                lambda: self._copy_clips(ops))
            menu.addAction(
                "再复制几份…" if len(ops) <= 1 else f"把这 {len(ops)} 段再复制几份…",
                lambda: self._repeat_clips(ops))
        # v182: 「取消选择」属于上面那一组操作, 不能夹在「对比」和「属性」中间
        if batch:
            menu.addAction("取消选择", self.clip_editor.clip_list.clear_selection)

        # v192: 三处剪辑右键用同一套对比文案; 「发送到对比N」平铺在横线上方。
        # v182: 最后一组只放「对比」+「属性」两项, 中间不再插横线,
        #       这条横线下面也不许再有别的项。
        paths = []
        for c in ops:
            s = str(getattr(c, "source", "") or "")
            if s and s not in paths:
                paths.append(s)
        self._add_compare_send_actions(menu, paths)
        menu.addSeparator()
        act = menu.addAction(self._compare_label(paths),
                             lambda: self._open_compare_for_paths(paths))
        if act is not None:      # 离屏验证里可能换成假菜单(addAction 不返回 action)
            act.setEnabled(bool(paths))

        # 空白处右键(clip=None)也要能看属性: 拿选区里的第一段当代表
        props_clip = clip if clip is not None else (group[0] if group else None)
        if props_clip is not None:
            menu.addAction("属性…", lambda: self._show_clip_props(props_clip))
        return menu

    def _delete_clip(self, clip):
        """删掉指定的这一个片段(撤销点 / 预览 / 右栏清单都会跟着更新)。"""
        tl = self.edit_timeline
        if tl is None or clip is None:
            return
        name = clip.name
        tl.remove(clip)
        self.clip_editor.timeline_view.select_clip(None)
        self._after_edit_changed()
        self.clip_panel.set_clip(None, tl, {p.path: p for p in self.model.photos})
        self.status_label.setText(f"已删除片段 {name}")

    def _delete_clip_group(self, group):
        """右栏多选菜单: 一次删掉这几段(v181)。

        不复用 `edit_delete` —— 那个按 `_edit_area` 分流, 最后一次点在素材框时
        会把"删片段"变成"移走素材"。这里按身份直接删, 最稳。
        """
        tl = self.edit_timeline
        if tl is None:
            return
        n = 0
        for c in (group or []):
            for x in list(getattr(tl, "clips", None) or []):
                if x is c:
                    tl.remove(x)
                    n += 1
                    break
        if not n:
            return
        self.clip_editor.timeline_view.select_clip(None)
        self._after_edit_changed()
        self.clip_panel.set_clip(None, tl, {p.path: p for p in self.model.photos})
        self.status_label.setText(f"已删除 {n} 个片段")

    def _on_clip_list_order(self, order, commit: bool = True):
        """右栏拖动改了顺序: 真的把轨道上的片段按新顺序重排。

        规则(和用户确认过): 拖到哪个位置, 第 i 个位置**沿用它原来的轨道号**,
        只把内容换过去 —— 这样把卡片拖到另一条轨道的位置等于"那一段换到那条轨",
        轨道数量不会因此变多, 也不会凭空多出空轨。
        起点: 每条轨道还从它原来最早那个片段的起点开始, 之后首尾相接。

        ⚠️ v141: commit=False 是"拖动过程中"的实时更新 —— 只改轨道并重画,
        不刷新右栏清单(会把手里那张卡片抽走), 也不重算无损(拖一下算一次太贵)。
        """
        clips = list(order or [])
        cur = self._ordered_clips()
        if len(clips) != len(cur) or sorted(id(c) for c in clips) != sorted(id(c) for c in cur):
            return                       # 对不上号就别乱改
        if [id(c) for c in clips] != [id(c) for c in cur]:
            self._apply_clip_order(clips, cur)
        if commit:
            self._after_edit_changed()
            self.status_label.setText("已按新顺序重排片段")
        else:
            try:
                self.clip_editor.timeline_view.update()
                self.clip_editor._refresh_time_label()
            except Exception:
                pass

    def _apply_clip_order(self, clips: list, old: list):
        """把 old 的轨道号按位置留给 clips, 再让每条轨从原起点首尾相接。"""
        track_of = [int(c.track) for c in old]
        base = {}
        for c in old:
            t = int(c.track)
            base[t] = min(base.get(t, float(c.start)), float(c.start))
        for i, c in enumerate(clips):
            c.track = track_of[i] if i < len(track_of) else c.track
        for t in sorted(set(int(c.track) for c in clips)):
            t0 = float(base.get(t, 0.0))
            for c in [x for x in clips if int(x.track) == t]:
                c.start = t0
                t0 += c.duration

    # --------------------------------------------- 预览下沿的进度条(v141/v159)
    def _scrub_scope(self) -> str:
        """进度条现在是"谁的"进度: 素材 / 整条轨道 / 某一段 / 没有。

        ⚠️ 素材优先: 看素材框里的素材时, 那条进度条就是这条素材的, 哪怕之前
        播过整条轨道(_tl_scrub 还留着 True)。
        """
        if self._media_preview:
            return "media"
        if self._tl_scrub:
            return "timeline"
        if self._clip_scrub is not None:
            return "clip"
        return "none"

    def _playhead_in_clip(self, clip) -> bool:
        """裁剪线是不是正落在这一段里(含起点, 不含终点)。"""
        tl = self.edit_timeline
        if tl is None or clip is None:
            return False
        try:
            s = float(clip.start)
            e = s + max(0.0, float(getattr(clip, "duration", 0.0) or 0.0))
            t = float(tl.playhead)
        except Exception:
            return False
        return (s - 1e-3) <= t < (e - 1e-3)

    def _begin_timeline_scrub(self):
        """开始播整条轨道: 进度条范围 = 所有轨道从 0 到最晚结束那一刻。"""
        tl = self.edit_timeline
        if tl is None:
            return
        if self._media_preview:
            # 正在看素材框里的素材: 那条进度条先留着(scope 里素材优先),
            # 等素材预览结束, _sync_scrub 会把整条轨道的进度接回来。
            self._tl_scrub = True
            return
        total = max(0.0, float(tl.total_duration() or 0.0))
        self._clip_scrub = None
        self._clip_scrub_pos = 0.0
        if total <= 0.0:
            self._tl_scrub = False
            self.clip_editor.hide_scrubber()
            return
        self._tl_scrub = True
        if not self._scrub_on:               # 「进度条」关着: 状态记着, 但不显示
            self.clip_editor.hide_scrubber()
            return
        self.clip_editor.show_scrubber(total)
        self.clip_editor.set_scrub_pos(max(0.0, min(total, float(tl.playhead))))

    def _end_timeline_scrub(self):
        self._tl_scrub = False
        try:
            self.clip_editor.hide_scrubber()
        except Exception:
            pass

    def _begin_clip_scrub(self, clip):
        """中间预览正看着"这一段": 进度条按片段长度摆出来(0 起)。"""
        if clip is None:
            self._end_clip_scrub()
            return
        self._tl_scrub = False           # 两种范围互斥, 别叠着
        self._clip_scrub = clip
        dur = float(getattr(clip, "duration", 0.0) or 0.0)
        if dur <= 0:
            self._clip_scrub_pos = 0.0
            self.clip_editor.hide_scrubber()
            return
        # 裁剪线已经在这一段里时(单击只选中, 不跳), 进度条要停在它现在那一秒
        t = float(self.edit_timeline.playhead) - float(getattr(clip, "start", 0.0))
        t = 0.0 if t < 0 else (dur if t > dur else t)
        self._clip_scrub_pos = t
        if not self._scrub_on:               # 「进度条」关着: 只记状态
            self.clip_editor.hide_scrubber()
            return
        self.clip_editor.show_scrubber(dur)
        self.clip_editor.set_scrub_pos(t)

    def _end_clip_scrub(self):
        self._clip_scrub = None
        self._clip_scrub_pos = 0.0
        try:
            self.clip_editor.hide_scrubber()
        except Exception:
            pass

    def _sync_scrub(self):
        """播放头动了: 把进度条摆到对的位置(整条轨道 / 某一段)。"""
        tl = self.edit_timeline
        if tl is None:
            return
        if not self._scrub_on:
            # 「进度条」关着: 状态照旧维护, 只是不显示(打开开关能立刻回来)
            return
        if self._scrub_scope() == "timeline":
            total = max(0.0, float(tl.total_duration() or 0.0))
            if total <= 0.0:             # 轨道空了: 没进度可显示
                self._end_timeline_scrub()
                return
            ed = self.clip_editor
            try:
                if not ed.scrubber.isVisible():
                    ed.show_scrubber(total)   # 中途被素材预览收掉过, 播起来要回来
                else:
                    ed.set_scrub_range(total)  # 增删片段后总长会变
                ed.set_scrub_pos(max(0.0, min(total, float(tl.playhead))))
            except Exception:
                pass
            return
        clip = self._clip_scrub
        if clip is None:
            return
        if clip not in tl.clips:            # 这段已经被删了
            self._end_clip_scrub()
            return
        dur = float(getattr(clip, "duration", 0.0) or 0.0)
        t = float(tl.playhead) - float(getattr(clip, "start", 0.0))
        t = 0.0 if t < 0 else (dur if (dur > 0 and t > dur) else t)
        self._clip_scrub_pos = t
        try:
            self.clip_editor.set_scrub_pos(t)
        except Exception:
            pass

    def _seek_timeline_scrub(self, t: float):
        """拖整体进度条: 把裁剪线放到整条时间线的第 t 秒(在播就接着播)。"""
        tl = self.edit_timeline
        if tl is None:
            return
        total = max(0.0, float(tl.total_duration() or 0.0))
        t = max(0.0, min(total, float(t or 0.0))) if total > 0 else 0.0
        tl.playhead = t
        # set_playhead 会发 playheadMoved: 播放中自动走 _seek_while_playing
        # (先掐掉旧播放器, 手停稳再从新位置起播), 停着就是抓这一帧。
        self._tl_seeking = True          # 这一下是进度条自己引起的, 别把进度条收掉
        try:
            self.clip_editor.set_playhead(t)
        finally:
            self._tl_seeking = False
        self.clip_editor.timeline_view.follow_scroll()
        self.clip_editor._refresh_time_label()

    def _seek_clip_scrub(self, t: float):
        """拖进度条: 在这一段里跳到 t 秒(播放头跟着走, 画面跟手)。"""
        clip = self._clip_scrub
        tl = self.edit_timeline
        if clip is None or tl is None or clip not in tl.clips:
            self._end_clip_scrub()
            return
        dur = float(getattr(clip, "duration", 0.0) or 0.0)
        t = max(0.0, min(dur, float(t or 0.0))) if dur > 0 else 0.0
        self._clip_scrub_pos = t
        self.clip_editor.set_scrub_pos(t)
        tl.playhead = max(0.0, float(clip.start) + t)
        self._clip_seeking = True        # 这一下是片段进度条自己改的 playhead
        try:
            self.clip_editor.set_playhead(tl.playhead)
        finally:
            self._clip_seeking = False
        # v165: 拖进度条也要跟着滚轨道 —— 和拖整条那条一样, 否则「跟随移动」
        # 开着时拖进度条, 裁剪线跑到看不见的地方去了(follow_scroll 自己会看开关)。
        try:
            self.clip_editor.timeline_view.follow_scroll()
        except Exception:
            pass
        try:
            self.clip_editor.timeline_view.update()
        except Exception:
            pass
        if self.clip_editor.is_playing():
            # v159: 不再"暂停再重播" —— 那会走一遍 _edit_play, 把进度条范围从
            # "这一段"改成"整条轨道"。这里和拖裁剪线走同一条路(手停稳接着播)。
            self._seek_while_playing()
        else:
            self._refresh_preview_at(tl.playhead)

    # ------------------------------------------------- Ctrl+A 的范围(v141)
    def _install_area_taps(self):
        """记住"最后点的是哪一块": 左素材 / 右片段清单 / 下方轨道。

        ⚠️ 不能看焦点: 右栏清单是 NoFocus(空格要留给播放), 焦点永远不会落在它身上,
        靠 hasFocus() 分范围的话那一栏永远分不到。
        """
        targets = (("media", self.clip_editor.media.list),
                   ("media", self.clip_editor.media),
                   ("clips", self.clip_editor.clip_list.list),
                   ("clips", self.clip_editor.clip_list),
                   ("timeline", self.clip_editor.timeline_view))
        for area, w in targets:
            if w is None:
                continue
            tap = _AreaTap(area, self._set_edit_area)
            w.installEventFilter(tap)
            vp = w.viewport() if hasattr(w, "viewport") else None
            if vp is not None:
                vp.installEventFilter(tap)
            self._area_taps.append(tap)     # 留住引用, 不然过滤器会被销毁

    def _set_edit_area(self, area: str):
        self._edit_area = area

    def _on_select_all(self):
        """Ctrl+A: 全选什么, 取决于最后点的是哪一块。

        左素材框 → 全选素材; 右片段清单 / 下方轨道 → 全选轨道上的片段;
        在剪辑页但还没点过任何一块(或就在主界面) → 全选主列表。
        """
        if self.edit_mode and self._edit_area:
            area = self._edit_area
            if area == "media":
                try:
                    n = self.clip_editor.media.select_all()
                except Exception:
                    n = 0
                self.status_label.setText(
                    f"已选中 {n} 个素材" if n else "素材框里还没有素材")
                return
            if area in ("clips", "timeline"):
                tv = self.clip_editor.timeline_view
                clips = list(getattr(self.edit_timeline, "clips", None) or [])
                if clips:
                    tv.select_clips(clips)
                    self._on_edit_clip_selected(tv.selected_clip())
                    self.status_label.setText(f"已选中 {len(clips)} 个片段")
                else:
                    self.status_label.setText("轨道上还没有片段")
                return
        self.view.select_all_photos()

    def _clear_clip_selection(self):
        """撤掉"选中片段"这一侧: 轨道上的选中 / 右栏高亮 / 属性 / 段内进度条。

        v145: 剪辑页里"左侧素材"和"右侧片段"不允许同时选中 —— 选中一边就把
        另一边清掉。这里只管片段那一侧(素材侧由 `_on_edit_clip_selected` 清)。
        """
        tv = self.clip_editor.timeline_view
        if tv.selected_clips():
            tv.select_clip(None)
            tv.update()
        cl = self.clip_editor.clip_list
        try:
            cl.select_clip(None)
            cl.set_multi_highlight(None)
        except Exception:
            pass
        if self._clip_scrub is not None:
            self._end_clip_scrub()
        try:
            self.clip_editor.show_clip(None)
            self.clip_panel.set_clip(None, self.edit_timeline,
                                     {p.path: p for p in self.model.photos})
            self._sync_edit_tool_buttons()
        except Exception:
            pass

    def _on_edit_clip_selected(self, clip):
        """选中片段: 预览显示**裁剪线那一秒**的画面(而不是缩略图), 少一次闪烁。"""
        if self._media_preview:
            self._end_media_preview()   # 点了时间线 = 回到时间线预览
        # v145: 素材框和右栏片段互斥 —— 选中片段就把素材框里的选择清掉
        # (传 None = 清空选中, 那时不动素材框)
        if clip is not None:
            try:
                self.clip_editor.media.clear_selection()
            except Exception:
                pass
        # 换了选中的片段 → 收掉"看这一段"的进度条(点右栏卡片会紧接着重新摆出来)
        if self._clip_scrub is not None and self._clip_scrub is not clip:
            self._end_clip_scrub()
        # 右栏清单里的高亮跟着走(点卡片进来时已经在那一行了, 这里是无害的重复设置)
        try:
            cl = self.clip_editor.clip_list
            cl.select_clip(clip)
            # 轨道上同时选中了好几段(Ctrl+A / 右栏框选): 卡片们一起亮(v141)。
            # v181: 单选也把这一份传过去 —— 只传 >1 的话, 轨道那边只要刷一次
            # 选中(点工具按钮等)就会把右栏刚 Ctrl 多选出来的选区清成空。
            sel = self.clip_editor.timeline_view.selected_clips()
            cl.set_multi_highlight(sel)
        except Exception:
            pass
        # 有没选中决定「对齐关键帧 / 帧率 / 精简 / 检测」能不能点(v135);
        # 顺手把当前帧率写到按钮上(v136); 音频只亮「帧率/倍速」(v167)。
        # v188: 没选中但裁剪线上恰好一段时, 帧率 / 音量显示那一段的信息。
        self._sync_edit_tool_buttons(clip)
        self.clip_panel.set_clip(clip, self.edit_timeline,
                                 {p.path: p for p in self.model.photos})
        if self.edit_mode and not self.clip_editor.is_playing():
            self._refresh_preview_at(self.edit_timeline.playhead)
        else:
            self.clip_editor.show_clip(clip)

    def _sync_mode_ui(self):
        """三形态(排序 / 合并 / 统一)共用一个窗口: 只切控件可见性与按钮高亮。"""
        m = self.merge_mode
        n = self.norm_mode
        e = self.edit_mode
        # 排序专用控件: 合并 / 统一两种形态都隐藏
        # v208: 「清空」三形态都显示(和「添加」一样紧跟其后), 不进隐藏列表
        # v214: 显示方式下拉(小/中/大/超大图标)三形态都用 → 不再隐藏; 它前面留一条
        #       分隔线(vl3), 下面再接形态滑块。
        for w in (self.vl1, self.search_edit, self.sort_btn,
                  self.reload_btn, self.sw_year, self.sw_month, self.sw_day,
                  self.more_btn):
            w.setVisible(not (m or n))
        self.view_mode_combo.setVisible(True)
        self.vl3.setVisible(True)
        # 层级开关: 排序是 年/月/日, 统一是 编码/分辨率/帧率(同一个位置互换);
        # 合并没有层级概念, 连前面那条竖线一起收起来。
        self.vl2.setVisible(not m)
        self.sw_codec.setVisible(n)
        self.sw_res.setVisible(n)
        self.sw_fps.setVisible(n)
        self._sync_norm_switches()
        self.clear_list_btn.setVisible(True)
        # v207: 顶栏参数条(编码/音频/关键帧/码率)只统一态显示
        self.norm_param_slot.setVisible(n)
        self.vl_norm_param.setVisible(n)
        # 剪辑是独立窗口(v128): 打开时主界面照样能用, 所以顶栏这些一律不藏。
        self.lossless_box.setVisible(m)
        self.solo_box.setVisible(m)
        # v199: 「剪辑」按钮永远是"剪辑"(点一下新开一个剪辑窗口), 不再有"收起剪辑"。
        # 关窗口一律点各自窗口的 ×。
        eb = self.merge_panel.edit_btn
        eb.setText("剪辑")
        eb.setToolTip("新开一个剪辑窗口（素材先进素材框，点 ＋ 加入时间线，"
                      "多条轨道自由摆放，可用无损或转码导出）")
        # v210: 滑块三按钮互斥高亮(选中段实心蓝底白字, 由 #modeBtn:checked 绘制)。
        # autoExclusive 只管鼠标点击, 代码切换形态要手动 setChecked 同步。
        self.sort_mode_btn.setChecked(not (m or n))
        self.merge_btn.setChecked(m)
        self.norm_mode_btn.setChecked(n)
        # 空状态文案也要跟着换
        if hasattr(self, "empty_title"):
            if n:
                self.empty_title.setText("这个列表里还没有视频")
                self.empty_sub.setText("点顶栏「添加」把视频加进来，就能统一成同一套兼容规格")
            elif m:
                self.empty_title.setText("这个列表里还没有视频")
                self.empty_sub.setText("视频才能参与合并；先「排序」回去把视频加进来")
            else:
                self.empty_title.setText("把照片文件夹或文件拖进来，或点上面的「添加」")
                self.empty_sub.setText("排序后按当前顺序分配时间戳，一次性写回 修改时间 / 创建时间 / 拍摄时间")

    # ---------------------------------------------------------------- FFmpeg 检查
    def _check_ffmpeg_async(self, force: bool = False):
        """后台查一次 FFmpeg(查找可能要扫 WinGet 目录, 放 UI 线程会卡窗口)。

        排序模式下每次切回都会查一次, 结果决定合并界面「FFmpeg」按钮的绿/红。
        """
        if self._ff_check_worker is not None:
            if force:
                self._ff_check_again = True
            return
        try:
            from ..merge.workers import FfmpegCheckWorker
            self._ff_check_worker = FfmpegCheckWorker(self)
            self._ff_check_worker.done.connect(self._on_ffmpeg_checked)
            self._ff_check_worker.start()
        except Exception:
            self._ff_check_worker = None
            return
        if self.ffmpeg_ok is None:
            self.merge_panel.set_ffmpeg_state(None)   # 首次: 按钮显示"检查中"

    def _on_ffmpeg_checked(self, ok: bool, ffmpeg: str, ffprobe: str):
        self._ff_check_worker = None
        self.ffmpeg_ok = bool(ok)
        self.ffmpeg_pair = (ffmpeg, ffprobe) if ok else None
        if ok:
            # 探测线程可能还没返回: 先把能用的路径挂上, 顶栏就不会误报"未找到"
            self.merge_ffmpeg, self.merge_ffprobe = ffmpeg, ffprobe
        self.merge_panel.set_ffmpeg_state(self.ffmpeg_ok)
        self._refresh_top_status()
        # v206: 统一页等着 FFmpeg 结果才能分析 → 同步过去; 有缺口就接着补测
        if self.norm_mode:
            videos = [p for p in self.model.photos if getattr(p, "kind", "") == "video"]
            self.norm_workspace.set_context(videos, self.merge_specs,
                                            self.merge_ffmpeg, self.merge_ffprobe)
            if ok and [p for p in videos if p.path not in self.merge_specs]:
                self._start_probe()
        if self._ff_check_again:
            self._ff_check_again = False
            QTimer.singleShot(0, self._check_ffmpeg_async)

    # ---------------------------------------------------------------- 规格探测
    def _start_probe(self, force: bool = False):
        if self._probe_worker is not None:
            return
        # 直接按 photos 过滤, 不用 merge_videos(): 临时分组里可能还没包含新加的视频
        videos = [p for p in self.model.photos if getattr(p, "kind", "") == "video"]
        if not videos:
            # 没有视频也要如实显示 FFmpeg 状态(以前这里直接判 False → 空列表必红字)
            self.merge_panel.set_context({}, {}, self.ffmpeg_ok is not False)
            self.merge_panel.set_ffmpeg_state(self.ffmpeg_ok)
            if self.ffmpeg_ok is None:
                self._check_ffmpeg_async()
            self._refresh_top_status()
            return
        todo = videos if force else [p for p in videos if p.path not in self.merge_specs]
        if not todo:
            return
        if force:
            self.merge_specs.clear()
            self.merge_errors.clear()
            for p in videos:
                p.merge_line = "检测中…"
            self.view.viewport().update()
        from ..merge.workers import ProbeWorker
        self._probe_videos = list(todo)
        self._probe_worker = ProbeWorker(todo, self)
        self._probe_worker.found.connect(self._on_probe_found)
        self._probe_worker.no_ffmpeg.connect(self._on_probe_no_ffmpeg)
        self._probe_worker.one.connect(self._on_probe_one)
        self._probe_worker.finished_all.connect(self._on_probe_done)
        self._probe_worker.start()

    def _on_probe_found(self, ffmpeg: str, ffprobe: str):
        self.merge_ffmpeg = ffmpeg
        self.merge_ffprobe = ffprobe
        self.ffmpeg_ok = True
        self.ffmpeg_pair = (ffmpeg, ffprobe)
        self.merge_panel.set_ffmpeg_state(True)
        # v206: 统一页用探测到的 ffprobe 刷新结论条(此时可能还没有规格)
        if self.norm_mode:
            self.norm_workspace.ffmpeg, self.norm_workspace.ffprobe = ffmpeg, ffprobe
            self.norm_workspace._refresh_result()

    def _on_probe_no_ffmpeg(self):
        self.merge_ffmpeg = None
        self.merge_ffprobe = None
        self.ffmpeg_ok = False
        self.ffmpeg_pair = None
        self.merge_panel.set_ffmpeg_state(False)
        for p in getattr(self, "_probe_videos", []):
            p.merge_line = "未找到 FFmpeg"
        self.view.viewport().update()
        if self.norm_mode:
            self.norm_workspace.ffmpeg, self.norm_workspace.ffprobe = None, None
            self.norm_workspace._refresh_result()

    def _on_probe_one(self, index: int, spec, err: str):
        videos = getattr(self, "_probe_videos", [])
        if not (0 <= index < len(videos)):
            return
        p = videos[index]
        self.merge_specs[p.path] = spec
        if err:
            self.merge_errors[p.path] = err
        if spec:
            p.merge_line = mgrouping.cell_line(spec, self._wide_cells(),
                                               skip=self._cell_skip_keys(p))
        else:
            p.merge_line = f"读取失败：{err or '未知原因'}"
        self.view.viewport().update()

    def _on_probe_done(self):
        self._probe_worker = None
        self._apply_merge_groups()
        self.merge_panel.set_context(self.merge_specs, self.merge_errors,
                                     bool(self.merge_ffmpeg))
        self.merge_panel.set_selection(self.view.selected_photos())
        self._refresh_top_status()
        # 剪辑态: 探测刚跑完, 把时长回填进时间线(用户可能在探测完成前就点了「剪辑」)
        if self.edit_mode:
            self._refresh_edit_durations()
        # v207: 统一页刚补齐规格 → 原位重算 plan, 重铺合并式网格(不动参数档/形态)
        if getattr(self, "norm_mode", False):
            self.norm_workspace.set_specs(self.merge_specs)
            self._apply_norm_groups()

    def _wide_cells(self) -> bool:
        """当前显示档位够不够宽: 大/超大图标显示完整规格, 小/中图标只显示 "1080p"。"""
        return self.view.delegate.cell_w >= 132

    def _cell_skip_keys(self, p) -> set:
        """组标题已经展示、格子第二行不该重复的字段 key(v224)。

        统一页: 标题是 编码→分辨率→帧率 三级, 只跳**当前展开到的那几级**;
        其余形态(合并): 标题规格串来自 HEADER_FIELDS, 值非空的字段都算展示了。
        """
        if getattr(self, "norm_mode", False):
            m = self.model
            skip = set()
            if not m.norm_master["codec"]:
                return skip
            key = getattr(p, "norm_key", None)
            if not key:
                return skip
            skip.add("video_codec")
            st = m.norm_state(tuple(key)[:1])
            if st.get("res"):
                skip.add("width")
                if st.get("fps"):
                    skip.add("r_frame_rate")
            return skip
        spec = self.merge_specs.get(p.path)
        return mgrouping.header_skip_keys(spec) if spec else set()

    def _apply_merge_groups(self):
        videos = [p for p in self.model.photos if getattr(p, "kind", "") == "video"]
        keep = {p.path for p in self.view.selected_photos()}
        groups = mgrouping.build_merge_groups(videos, self.merge_specs, self.merge_errors)
        # 规格行按当前档位重算(切档位后文字长度不一样)
        wide = self._wide_cells()
        for p in videos:
            spec = self.merge_specs.get(p.path)
            if spec:
                p.merge_line = mgrouping.cell_line(spec, wide,
                                                   skip=mgrouping.header_skip_keys(spec))
            elif p.path in self.merge_errors:
                p.merge_line = f"读取失败：{self.merge_errors[p.path]}"
        self.model.set_merge_groups(groups)
        # 重建分组会清空选中: 按路径把之前的勾选恢复回来
        if keep:
            self._select_photos([p for p in videos if p.path in keep])
        self._sync_group_checks()

    def _apply_norm_groups(self):
        """v209: 把统一计划铺成三级网格(编码→分辨率→帧率, 对齐排序年/月/日)。"""
        nw = self.norm_workspace
        groups = nw.groups_for_grid()
        wide = self._wide_cells()
        by_path = {getattr(it, "path", ""): it for it in (nw.plan.items or [])}
        for p in nw.photos:
            it = by_path.get(getattr(p, "path", ""))
            spec = (getattr(it, "spec", None) if it else None) or self.merge_specs.get(p.path)
            if spec:
                p.merge_line = mgrouping.cell_line(spec, wide,
                                                   skip=self._cell_skip_keys(p))
            elif it is not None and getattr(it, "error", ""):
                p.merge_line = f"读取失败：{it.error}"
            else:
                p.merge_line = "检测中…"
        self.model.norm_groups = groups
        if groups:
            self.model.set_norm_groups(groups)
        elif nw.photos:
            # 分组还没算出来: 先给个默认 key, 免得网格空白
            for p in nw.photos:
                if not getattr(p, "norm_key", None):
                    p.norm_key = ("正在分析…",)
                    p.norm_labels = ("正在分析…",)
                    p.norm_need = 0
            self.model.set_norm_groups([{"key": ("__pending__",), "photos": list(nw.photos)}])
        else:
            self.model.set_norm_groups([])
        self._sync_group_checks()

    def _restart_norm_view(self):
        """统一形态下列表变了(添加/刷新): 重取视频、重算 plan、缺规格的后台补测。"""
        videos = [p for p in self.model.photos if getattr(p, "kind", "") == "video"]
        self.norm_workspace.set_context(videos, self.merge_specs,
                                        self.merge_ffmpeg, self.merge_ffprobe)
        self.norm_workspace.selected_paths = set()
        self._apply_norm_groups()
        if videos:
            self.view.select_all_photos()
            self.norm_workspace.selected_paths = {p.path for p in videos}
            self._sync_group_checks()
        if videos and not self.norm_workspace.ffprobe:
            self._check_ffmpeg_async(force=True)
        elif videos:
            missing = [p for p in videos if p.path not in self.merge_specs]
            if missing:
                self._start_probe()
        self._refresh_top_status()

    def _restart_merge_view(self):
        """合并模式下列表变了(添加/刷新): 重新分组; 只补测还没有规格的视频。"""
        videos = [p for p in self.model.photos if getattr(p, "kind", "") == "video"]
        if not videos:
            self.model.set_merge_groups([])
            self.merge_panel.set_selection([])
            self.stack.setCurrentWidget(self.empty_page)
            self._sync_lossless_btn()
            self._sync_solo_btn()
            self._refresh_top_status()
            return
        missing = [p for p in videos if p.path not in self.merge_specs]
        if missing:
            for p in missing:
                p.merge_line = "检测中…"
            self.model.set_merge_groups([{
                "key": ("__probe__",), "label": "正在检测规格…", "tag": "single",
                "note": "", "photos": videos,
            }])
            self.stack.setCurrentWidget(self.view)
            self._sync_lossless_btn()
            self._sync_solo_btn()
            self._start_probe()
        else:
            self._apply_merge_groups()
            self.stack.setCurrentWidget(self.view)
        self._refresh_top_status()

    # ---------------------------------------------------------------- 全选可无损
    def toggle_select_lossless(self):
        """点一下选中所有「可无损合并」组, 再点一下取消全选(切换)。"""
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        groups = self.model.merge_groups or []
        lossless = [g for g in groups if g.get("tag") == "lossless"]
        if not lossless:
            QMessageBox.information(self, "全选可无损",
                                    "当前没有可无损合并的组（每组至少 2 段同规格视频）。")
            return
        members = []
        for g in lossless:
            members.extend(g.get("photos") or [])
        # 所有成员是否已全选: 全选则取消, 否则全选
        sel_ids = {id(p) for p in self.view.selected_photos()}
        all_on = members and all(id(p) in sel_ids for p in members)
        ids = {id(p) for p in members}
        sel = QItemSelection()
        for i, row in enumerate(self.model.rows):
            if isinstance(row, PhotoRow) and id(row.photo) in ids:
                ix = self.model.index(i)
                sel.select(ix, ix)
        if not sel.count():
            return
        mode = (QItemSelectionModel.Deselect if all_on
                else QItemSelectionModel.ClearAndSelect)
        self.view.selectionModel().select(sel, mode)
        if not all_on:
            self.view.scrollTo(sel.indexes()[0])

    def select_same_spec(self):
        """选中光标所在规格组的全部视频(没光标时用已选第一段所在的组)。右键菜单沿用。"""
        target = None
        idx = self.view.currentIndex()
        rows = self.model.rows
        if idx.isValid() and 0 <= idx.row() < len(rows):
            row = rows[idx.row()]
            if isinstance(row, PhotoRow):
                target = row.photo
        if target is None:
            sel = self.view.selected_photos()
            target = sel[0] if sel else None
        groups = self.model.merge_groups or []
        if target is None:
            # 没有任何参照: 选第一个"可无损合并"的组
            for g in groups:
                if g.get("tag") == "lossless":
                    self._select_photos(g["photos"])
                    return
            return
        key = self.model.group_key_of(target)
        for g in groups:
            if g.get("key") == key:
                self._select_photos(g["photos"])
                return

    def _lossless_groups(self) -> list:
        """当前所有「可无损合并」组(成员 >= 2)。"""
        return [g for g in (self.model.merge_groups or [])
                if g.get("tag") == "lossless" and len(g.get("photos") or []) >= 2]

    # ---------------------------------------------------------------- 独立分组(v226: 原"单独")
    def _single_groups(self) -> list:
        """当前所有「独立」分组(规格与谁都合不上, 组里只有一段视频)。"""
        return [g for g in (self.model.merge_groups or [])
                if g.get("tag") == "single"]

    def toggle_select_single(self):
        """点一下选中所有「独立」分组的视频, 再点一下取消全选(切换)。"""
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        groups = self._single_groups()
        if not groups:
            QMessageBox.information(self, "全选独立",
                                    "当前没有独立分组的视频（每段规格都独一无二才会独立成组）。")
            return
        members = []
        for g in groups:
            members.extend(g.get("photos") or [])
        # 所有成员是否已全选: 全选则取消, 否则全选
        sel_ids = {id(p) for p in self.view.selected_photos()}
        all_on = bool(members) and all(id(p) in sel_ids for p in members)
        ids = {id(p) for p in members}
        sel = QItemSelection()
        for i, row in enumerate(self.model.rows):
            if isinstance(row, PhotoRow) and id(row.photo) in ids:
                ix = self.model.index(i)
                sel.select(ix, ix)
        if not sel.count():
            return
        mode = (QItemSelectionModel.Deselect if all_on
                else QItemSelectionModel.ClearAndSelect)
        self.view.selectionModel().select(sel, mode)
        if not all_on:
            self.view.scrollTo(sel.indexes()[0])

    def _sync_solo_btn(self):
        """「独立」板块「全选」三态: 全选=蓝 / 选中部分=半蓝 / 没选=灰。"""
        if not self.merge_mode:
            return
        members = [p for g in self._single_groups() for p in (g.get("photos") or [])]
        if not members:
            self.solo_all_btn.setProperty("solo", "")
            self.solo_all_btn.setEnabled(False)
            self._repolish(self.solo_all_btn)
            return
        sel_ids = {id(p) for p in self.view.selected_photos()}
        hit = sum(1 for p in members if id(p) in sel_ids)
        state = "" if hit == 0 else ("full" if hit == len(members) else "part")
        self.solo_all_btn.setEnabled(True)
        self.solo_all_btn.setProperty("solo", state)
        self._repolish(self.solo_all_btn)

    def open_group_merge(self):
        if self._merge_worker is not None:
            QMessageBox.information(self, "按组合并", "合并正在进行中，请先等它完成或取消。")
            return
        groups = self._lossless_groups()
        if not groups:
            QMessageBox.information(self, "按组合并",
                                    "当前没有可无损合并的组（每组至少 2 段同规格视频）。")
            return
        from .dialogs import GroupMergeDialog
        default_dir = self.merge_panel.out_dir() or self.merge_panel.default_dir()
        dlg = GroupMergeDialog(groups, default_dir, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        jobs = dlg.jobs()
        if not jobs:
            return
        self._run_group_merges(jobs)

    def open_group_folder(self):
        if self._merge_worker is not None:
            QMessageBox.information(self, "新建文件夹", "合并正在进行中，请先等它完成或取消。")
            return
        groups = self._lossless_groups()
        if not groups:
            QMessageBox.information(self, "新建文件夹",
                                    "当前没有可无损合并的组（每组至少 2 段同规格视频）。")
            return
        from .dialogs import GroupFolderDialog
        default_dir = self.merge_panel.out_dir() or self.merge_panel.default_dir()
        dlg = GroupFolderDialog(groups, default_dir, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        folder_template = dlg.folder_name()
        rename = dlg.rename_enabled()
        file_template = dlg.rename_template()
        base_dir = dlg.base_dir()
        if not base_dir:
            QMessageBox.warning(self, "新建文件夹", "请选择目标位置。")
            return
        self._move_groups_to_folders(groups, base_dir, folder_template, rename,
                                     file_template)

    def _select_photos(self, photos: list):
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        ids = {id(p) for p in photos}
        sel = QItemSelection()
        for i, row in enumerate(self.model.rows):
            if isinstance(row, PhotoRow) and id(row.photo) in ids:
                ix = self.model.index(i)
                sel.select(ix, ix)
        if sel.count():
            self.view.selectionModel().select(sel, QItemSelectionModel.ClearAndSelect)
            self.view.scrollTo(sel.indexes()[0])

    # ---------------------------------------------------------------- 组标题整组勾选
    def _group_of_key(self, key: tuple):
        for g in (self.model.merge_groups or []):
            if tuple(g.get("key") or ()) == tuple(key):
                return g
        return None

    def _toggle_group_check(self, key: tuple):
        """点组标题左侧的勾选框: 整组勾上 / 整组取消(其它组的勾选保持不变)。

        两种形态共用: 合并模式按规格组, 排序模式按年 / 月 / 日 —— 成员口径各自
        由 `_group_members` 决定, 这里只管"全选/全不选"。

        v220: 合并/统一两态下勾选存在 `_grid_sel_paths` 里(按路径), 不读 QItemSelection
        —— 收起的组根本没有格子可选中, 拿选中集当勾选集就会"点一个、别的全被清掉"。
        """
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        members = self._group_members(key)
        if not members:
            return
        if self.merge_mode or getattr(self, "norm_mode", False):
            paths = {p.path for p in members if getattr(p, "path", "")}
            all_on = paths <= self._grid_sel_paths       # 组里全勾上 = 整组已选
            if all_on:
                self._grid_sel_paths -= paths
            else:
                self._grid_sel_paths |= paths
            # 收起状态不影响勾选(集合里有就行), 只把可见的那部分投影成高亮
            self._apply_grid_selection(self._grid_sel_paths)
            self._sync_group_checks()
            self._sync_norm_checked()
            self._refresh_top_status()
            return
        # 排序态: 勾选就是 Qt 的选中, 照旧
        if tuple(key) in self.model.collapsed:
            self.model.toggle_collapse(tuple(key))
        sel_ids = {id(p) for p in self.view.selected_photos()}
        all_on = all(id(p) in sel_ids for p in members)
        ids = {id(p) for p in members}
        sel = QItemSelection()
        for i, row in enumerate(self.model.rows):
            if isinstance(row, PhotoRow) and id(row.photo) in ids:
                ix = self.model.index(i)
                sel.select(ix, ix)
        if not sel.count():
            return
        mode = (QItemSelectionModel.Deselect if all_on
                else QItemSelectionModel.Select)
        self.view.selectionModel().select(sel, mode)
        # 收起的分组 / 还没刷新的标题行: 勾选框状态当场对齐(别等下一次选完才变)
        self._sync_group_checks()

    # ---------------------------------------------------------------- 网格勾选集合(v220)
    @property
    def _grid_sel_paths(self) -> set:
        """网格勾选权威集合(按路径)。赋值时同步镜像到 model.grid_checked。"""
        return self._grid_sel

    @_grid_sel_paths.setter
    def _grid_sel_paths(self, v):
        self._grid_sel = set(v or ())
        m = getattr(self, "model", None)
        if m is not None:
            m.grid_checked = set(self._grid_sel)

    def _grid_member_paths(self) -> set[str]:
        """网格里参与勾选的全部素材路径(**含被收起、当前看不见的**)。"""
        if getattr(self, "norm_mode", False):
            return {p.path for p in self.model.photos
                    if getattr(p, "norm_key", None) and getattr(p, "path", "")}
        if self.merge_mode:
            live = {id(p) for p in self.model.photos}
            out = set()
            for g in (self.model.merge_groups or []):
                for p in (g.get("photos") or []):
                    if id(p) in live and getattr(p, "path", ""):
                        out.add(p.path)
            return out
        return set()

    def _visible_row_paths(self) -> set[str]:
        out: set[str] = set()
        for r in self.model.rows:
            if isinstance(r, PackRow):
                # v231: 组块里每张子卡的路径都算"可见"
                out |= {p.path for p in r.all_photos() if getattr(p, "path", "")}
            elif isinstance(r, PhotoRow) and getattr(r.photo, "path", ""):
                out.add(r.photo.path)
        return out

    def _visible_checked_paths(self) -> set[str]:
        """看得见的格子里当前"勾着"的路径: 单照片格看 Qt 选中,
        多文件组块看镜像集合与块内路径的交集。"""
        out: set[str] = set()
        sm = self.view.selectionModel()
        if sm is None:
            return out
        for i, r in enumerate(self.model.rows):
            if not isinstance(r, PhotoRow) or not sm.isSelected(self.model.index(i)):
                continue
            if isinstance(r, PackRow) and len(r.all_rows()) > 1:
                out |= ({p.path for p in r.all_photos() if getattr(p, "path", "")}
                        & self._grid_sel_paths)
            elif getattr(r.photo, "path", ""):
                out.add(r.photo.path)
        return out

    def _absorb_block_selection(self, selected, deselected):
        """用户手势带来的块级选中变化 → 换算成整组勾选/取消(v231)。

        框选 / Ctrl+A / 点选到某个组块 = 勾上块里全部视频; 从选中里去掉 =
        勾掉全部(可见的块整体去勾, 与单照片格"取消选中=取消勾选"同口径)。
        """
        if selected is None and deselected is None:
            return
        rows = self.model.rows

        def blocks(sel):
            out = []
            if sel is None:
                return out
            for idx in sel.indexes():
                r = rows[idx.row()] if 0 <= idx.row() < len(rows) else None
                if isinstance(r, PackRow) and len(r.all_rows()) > 1:
                    out.append(r)
            return out

        for r in blocks(selected):
            self._grid_sel_paths |= {p.path for p in r.all_photos()
                                     if getattr(p, "path", "")}
        for r in blocks(deselected):
            self._grid_sel_paths -= {p.path for p in r.all_photos()
                                     if getattr(p, "path", "")}

    def _on_grid_path_press(self, path: str, additive: bool, right: bool):
        """统一页多文件组块里某张子卡被点击: 按路径(而不是整块)改勾选集合(v231)。

        左键=只勾这张(其它可见勾选清掉, 与单照片格同口径);
        Ctrl/Shift=这张勾上/取消; 右键=没勾过就补勾(绝不取消已勾)。
        """
        if not (self.merge_mode or getattr(self, "norm_mode", False)) or not path:
            return
        if right:
            if path not in self._grid_sel_paths:
                self._grid_sel_paths |= {path}
        elif additive:
            if path in self._grid_sel_paths:
                self._grid_sel_paths -= {path}
            else:
                self._grid_sel_paths |= {path}
        else:
            hidden = self._grid_member_paths() - self._visible_row_paths()
            self._grid_sel_paths = {path} | (self._grid_sel_paths & hidden)
        self._apply_grid_selection(self._grid_sel_paths)
        self._sync_group_checks()
        self._sync_norm_checked()
        self._refresh_top_status()

    def _checked_grid_photos(self) -> list:
        """合并态当前勾选的视频, 按网格(分组→组内顺序)排, **含收起组里的**。"""
        if not self.merge_mode:
            return []
        live = {id(p) for p in self.model.photos}
        out = []
        for g in (self.model.merge_groups or []):
            for p in (g.get("photos") or []):
                if id(p) in live and getattr(p, "path", "") in self._grid_sel_paths:
                    out.append(p)
        return out

    def _apply_grid_selection(self, paths):
        """把路径集合投影成网格里的可见选中(收起组里的那些没格子, 自然不在这)。

        v231: 多文件组块 = 只要块里有勾选就选中该条目(块内逐张高亮由
        delegate 按 model.grid_checked 镜像画); 投影全程带 _grid_applying
        保护 —— 投影引发的 selectionChanged 不得反向吸收块级选中。
        """
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        paths = set(paths or ())
        sel = QItemSelection()
        for i, row in enumerate(self.model.rows):
            if isinstance(row, PackRow) and len(row.all_rows()) > 1:
                if ({p.path for p in row.all_photos() if getattr(p, "path", "")}
                        & paths):
                    ix = self.model.index(i)
                    sel.select(ix, ix)
            elif isinstance(row, PhotoRow) and row.photo.path in paths:
                ix = self.model.index(i)
                sel.select(ix, ix)
        sm = self.view.selectionModel()
        if sm is None:
            return
        self._grid_applying = True
        try:
            if sel.count():
                sm.select(sel, QItemSelectionModel.ClearAndSelect)
            else:
                sm.clearSelection()
        finally:
            self._grid_applying = False

    def _sync_norm_checked(self):
        """统一态把勾选集合交给右栏(开始统一/预览都按它取素材)。"""
        if getattr(self, "norm_mode", False):
            self.norm_workspace.selected_paths = set(self._grid_sel_paths)

    def _on_grid_about_to_reset(self):
        """模型重建**前**留一份排序态选中快照(v234)。

        beginResetModel 会清空 Qt 选中且不发 selectionChanged; 排序态的"勾选"
        就是 Qt 选中本身, 合并/统一态有 _grid_sel_paths 兜底但排序态没有 ——
        从这里按 Photo 对象快照, endResetModel 后由 _on_grid_model_reset 还原。
        """
        if not hasattr(self, "top_status"):
            return
        self._sel_snapshot = list(self.view.selected_photos())

    def _on_grid_model_reset(self):
        """模型重建之后: 按路径把勾选还原(重建会把 Qt 的选中清空且不发 selectionChanged)。"""
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        if not hasattr(self, "top_status"):
            return                      # 构建期间(顶栏还没建好)不折腾
        if not (self.merge_mode or getattr(self, "norm_mode", False)):
            # v234: 排序态勾选=Qt 选中本身, 用 aboutToReset 快照按对象还原(行号会变)
            snapshot = getattr(self, "_sel_snapshot", None)
            if snapshot is not None:
                self._sel_snapshot = None
                ids = {id(p) for p in snapshot}
                sel = QItemSelection()
                for i, row in enumerate(self.model.rows):
                    if isinstance(row, PhotoRow) and id(row.photo) in ids:
                        ix = self.model.index(i)
                        sel.select(ix, ix)
                if sel.count():
                    self.view.selectionModel().select(
                        sel, QItemSelectionModel.ClearAndSelect)
            return
        # 先剔掉已经不在列表里的路径(换了文件夹 / 删了文件)
        self._grid_sel_paths &= {p.path for p in self.model.photos
                                 if getattr(p, "path", "")}
        if self._grid_sel_paths:
            self._apply_grid_selection(self._grid_sel_paths)
        self._sync_group_checks()
        self._sync_norm_checked()
        self._refresh_top_status()

    def _group_members(self, key: tuple) -> list:
        """某个组标题底下该算进来的照片(合并=规格组, 排序=年/月/日, 统一=三级前缀)。"""
        if getattr(self, "norm_mode", False):
            key = tuple(key or ())
            return [p for p in self.model.photos
                    if tuple(getattr(p, "norm_key", ()) or ())[:len(key)] == key]
        if self.merge_mode:
            g = self._group_of_key(key)
            if not g:
                return []
            live = {id(p) for p in self.model.photos}
            return [p for p in (g.get("photos") or []) if id(p) in live]
        return self.model.group_members(key)

    def _sync_group_checks(self, photos=None):
        """按当前勾选情况刷新每个组标题的勾选框: 全选/部分/未选。

        合并模式数"规格组里的视频", 排序模式数"该年 / 月 / 日底下的照片"
        (总数直接用标题上那个 N 张/N 段, 只对**选中的**照片算归属)。
        photos 传进来就省一次 selected_photos()(框选时每次移动都会刷)。
        """
        grid_merge = self.merge_mode or getattr(self, "norm_mode", False)
        if not grid_merge and not any(isinstance(r, HeaderRow)
                                      for r in self.model.rows):
            return              # 没有标题行(纯列表 / 合并分组还没算出来): 没得刷
        sel = photos if photos is not None else self.view.selected_photos()
        sel_ids = {id(p) for p in sel}
        changed = False
        if grid_merge:
            n = getattr(self, "norm_mode", False)
            groups = {tuple(g.get("key") or ()): g
                      for g in (self.model.merge_groups or [])}
            for row in self.model.rows:
                if isinstance(row, PackRow):
                    row = row.header       # v226: 紧凑子组卡的组头也要同步勾选
                elif isinstance(row, CollapsedPack):
                    row = row.header       # v232: 收起叶级组块的头块同样同步
                if not isinstance(row, HeaderRow):
                    continue
                if n:
                    if not row.norm_mode:
                        continue
                    members = self._group_members(tuple(row.key))
                else:
                    if not row.merge_tag:
                        continue
                    g = groups.get(tuple(row.key))
                    if not g:
                        continue
                    members = g.get("photos") or []
                hit = sum(1 for p in members
                          if getattr(p, "path", "") in self._grid_sel_paths)
                state = 2 if members and hit == len(members) else (1 if hit else 0)
                if row.group_checked != state:
                    row.group_checked = state
                    changed = True
            if changed:
                self.view.viewport().update()
            self._sync_lossless_btn()
            self._sync_solo_btn()
            return

        # 排序模式: 只算选中的那几张各自属于哪几组(几百张全选也只是几百次比对),
        # 组的总数沿用标题行上的 count。
        if not sel:
            hits: dict[tuple, int] = {}
        else:
            hits = {}
            for p in sel:
                k = self.model.key_of(p)
                if not k:
                    continue
                for i in range(1, len(k) + 1):
                    sub = tuple(k[:i])
                    hits[sub] = hits.get(sub, 0) + 1
        for row in self.model.rows:
            if isinstance(row, PackRow):
                row = row.header           # v226: 紧凑子组卡的组头也要同步勾选
            elif isinstance(row, CollapsedPack):
                row = row.header           # v237: 排序页收起的单文件组块头块同样同步
            if not isinstance(row, HeaderRow) or row.merge_tag:
                continue
            total = int(row.count or 0)
            hit = hits.get(tuple(row.key), 0)
            state = 2 if total and hit >= total else (1 if hit else 0)
            if row.group_checked != state:
                row.group_checked = state
                changed = True
        if changed:
            self.view.viewport().update()

    def _sync_lossless_btn(self):
        """「无损」板块「全选」三态 + 按组合并/新建文件夹 是否可点。

        全选 = 蓝 / 选中部分 = 半蓝 / 没选 = 灰;
        只有选了(至少一段可无损)才让「按组合并」「新建文件夹」可点。
        """
        if not self.merge_mode:
            return
        members = [p for g in self._lossless_groups() for p in (g.get("photos") or [])]
        if not members:
            self.sel_spec_btn.setProperty("lossless", "")
            self.sel_spec_btn.setEnabled(False)
            self.group_merge_btn.setEnabled(False)
            self.group_folder_btn.setEnabled(False)
            self._repolish(self.sel_spec_btn)
            return
        sel_ids = {id(p) for p in self.view.selected_photos()}
        hit = sum(1 for p in members if id(p) in sel_ids)
        state = "" if hit == 0 else ("full" if hit == len(members) else "part")
        self.sel_spec_btn.setEnabled(True)
        self.sel_spec_btn.setProperty("lossless", state)
        self.group_merge_btn.setEnabled(hit > 0)
        self.group_folder_btn.setEnabled(hit > 0)
        self._repolish(self.sel_spec_btn)

    def _repolish(self, w):
        w.style().unpolish(w)
        w.style().polish(w)
        w.update()

    # ---------------------------------------------------------------- 执行合并
    def _next_merge_name(self, videos: list) -> str:
        """默认输出名: 合并_001.mp4, 001 随同目录已有文件自动往后排。"""
        d = os.path.dirname(videos[0].path) if videos else ""
        for i in range(1, 1000):
            name = f"合并_{i:03d}.mp4"
            if not d or not os.path.exists(os.path.join(d, name)):
                return name
        return "合并_001.mp4"

    def _merge_out_path(self) -> str:
        name = self.merge_panel.out_name().strip()
        if not name:
            name = "合并_001.mp4"
        if not os.path.splitext(name)[1]:
            name += ".mp4"
        d = self.merge_panel.out_dir().strip() or self.merge_panel.default_dir()
        return os.path.join(d, name) if d else name

    def start_merge(self, reencode: bool):
        # v220: 勾选以 `_grid_sel_paths` 为准 —— 收起的分组只是"不显示", 里面的视频
        # 照样是勾上的(组标题的勾选框也这么显示), 合并必须把它们一起拼进去。
        # v222: 统一还在后台转码时不让合并 —— 同一批文件, 副本状态一直在变,
        # 两个重型任务还抢 CPU; 等统一完成(副本自动参与合并)或先取消。
        if getattr(self, "_normalize_worker", None) is not None:
            QMessageBox.information(
                self, "统一进行中",
                "统一格式还在后台转码，完成后再合并（届时工作副本会自动参与）。\n"
                "如不想等，可先在统一进度窗口里取消。")
            return
        photos = self._checked_grid_photos() or self.view.selected_photos()
        if len(photos) < 2:
            QMessageBox.information(self, "合并", "请先在网格里选中至少 2 段视频。")
            return
        if not self.merge_ffmpeg:
            QMessageBox.warning(self, "合并", "没有可用的 FFmpeg，无法合并。\n"
                                          "点右栏「⚙」→「FFmpeg 路径 / 检测…」指定。")
            self._check_ffmpeg_async(force=True)
            return
        # 选了「转码合并」但这几段其实同规格(可无损) → 问一句要不要改无损(v93)
        if reencode and self._can_merge_lossless(photos):
            if confirm_yes_no(self, "采用无损合并",
                              "选中的这些视频规格一致，<b>可以无损合并</b>（速度快、画质不损失）。"
                              "<br><br>要改用无损合并吗？",
                              center=True, red_yes=False, default_yes=True):
                reencode = False
                # 右栏按钮跟着切到「无损合并」，免得界面还显示转码
                self.merge_panel.lossless_btn.setChecked(True)
        out = self._merge_out_path()
        # v217: 输出名撞上选中的源视频 → 会直接截断原片(concat 看不到真实输入)
        bad = pathsafe.check_output(out, [p.path for p in photos])
        if bad:
            QMessageBox.warning(self, "输出文件名冲突", bad)
            return
        # 保存位置可能是用户手输的新文件夹 → 先建出来, 否则 ffmpeg 报"找不到路径"
        try:
            d = os.path.dirname(os.path.abspath(out))
            if d:
                os.makedirs(d, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "保存位置", f"建不了输出文件夹：\n{d}\n\n{exc}")
            return
        if os.path.exists(out):
            if not confirm_yes_no(self, "覆盖输出文件",
                                  f"这个文件已经存在，要覆盖它吗？\n\n{out}",
                                  center=True, red_yes=False):
                return
        from ..merge.workers import MergeWorker
        total = 0.0
        for p in photos:
            try:
                total += float((self.merge_specs.get(p.path) or {}).get("duration") or 0)
            except Exception:
                pass
        self.merge_panel.hint.setText(f"正在合并 {len(photos)} 段视频…")
        self.merge_panel.hint.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        self.merge_panel.set_busy(True)
        self._merge_worker = MergeWorker(
            self.merge_ffmpeg, photos, out, reencode, total, self,
            fix_audio=self._norm_fix_audio([p.path for p in photos]))
        self._merge_worker.progress.connect(self.merge_panel.set_progress)
        self._merge_worker.result.connect(self._on_merge_done)
        self._merge_worker.start()

    def _can_merge_lossless(self, photos) -> bool:
        """这些视频是不是同一种规格(能 -c copy 直接拼接)。规格读不到就算作不能。"""
        specs = [self.merge_specs.get(p.path) for p in photos]
        if not specs or any(s is None for s in specs):
            return False
        return len({mgrouping.signature(s) for s in specs}) == 1

    def _on_merge_done(self, ok: bool, msg: str):
        self._merge_worker = None
        self.merge_panel.set_busy(False)
        self.merge_panel.progress.setValue(0 if not ok else 100)
        if ok:
            self._merge_out = msg
            self.merge_panel.name_edit.setText(self._next_merge_name(self.model.merge_videos()))
            self.merge_panel.hint.setText("合并完成。")
            self.merge_panel.hint.setStyleSheet(f"color: {T.OK}; font-size: 12px;")
            self._merge_done_dialog(msg)
        elif msg == "已取消":
            self.merge_panel.hint.setText("已取消合并，未生成文件。")
            self.merge_panel.hint.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
            QMessageBox.information(self, "已取消", "合并已取消，没有生成任何文件。")
        else:
            self.merge_panel.hint.setText("合并失败。")
            self.merge_panel.hint.setStyleSheet(f"color: {T.DANGER}; font-size: 12px;")
            QMessageBox.warning(self, "合并失败", msg)

    def _merge_done_dialog(self, path: str):
        """合并完成弹窗: 除了「好」再给一个「打开文件所在位置」。

        两个按钮都用 AcceptRole: 这样排列才是 Windows 习惯的
        「[打开文件所在位置] [好]」—— 用 ActionRole 会把「打开」排到最右边。
        """
        mb = QMessageBox(self)
        mb.setWindowTitle("合并完成")
        mb.setIcon(QMessageBox.Information)
        mb.setText(f"已生成：\n{path}")
        open_btn = mb.addButton("打开文件所在位置", QMessageBox.AcceptRole)
        ok_btn = mb.addButton("好", QMessageBox.AcceptRole)
        mb.setDefaultButton(ok_btn)
        mb.setEscapeButton(ok_btn)
        mb.exec()
        if mb.clickedButton() is open_btn:
            self._reveal_path(path)

    def _cancel_merge(self):
        w = self._merge_worker
        if w is None:
            return
        w.stop()                   # merge_run 读到标志后 terminate ffmpeg + 删半成品
        self.merge_panel.mark_cancelling()

    # ---------------------------------------------------------------- 分组合并(队列)
    def _run_group_merges(self, jobs: list):
        """逐组执行合并(无损优先, 每组合并方式由弹窗决定), 组与组互不串。"""
        if not self.merge_ffmpeg:
            QMessageBox.warning(self, "按组合并", "没有可用的 FFmpeg，无法合并。\n"
                                  "点右栏「⚙」→「FFmpeg 路径 / 检测…」指定。")
            self._check_ffmpeg_async(force=True)
            return
        existing = [j["out"] for j in jobs if os.path.exists(j["out"])]
        if existing:
            if not confirm_yes_no(self, "覆盖输出文件",
                                  f"有 {len(existing)} 个输出文件已经存在，要覆盖吗？\n\n"
                                  + "\n".join(existing[:8])
                                  + ("\n…" if len(existing) > 8 else ""),
                                  center=True, red_yes=False):
                return
        self._gm_queue = list(jobs)
        self._gm_total = len(jobs)
        self._gm_done: list = []
        self._gm_failed: list = []
        self.merge_panel.set_busy(True)
        self._next_group_merge()

    def _next_group_merge(self):
        if not self._gm_queue:
            self._finish_group_merges()
            return
        from ..merge.workers import MergeWorker
        job = self._gm_queue[0]
        idx = self._gm_total - len(self._gm_queue) + 1
        total = 0.0
        for p in job["photos"]:
            try:
                total += float((self.merge_specs.get(p.path) or {}).get("duration") or 0)
            except Exception:
                pass
        self.merge_panel.hint.setText(f"正在合并（第 {idx}/{self._gm_total} 组）：{job['label']}…")
        self.merge_panel.hint.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        self._merge_worker = MergeWorker(
            self.merge_ffmpeg, job["photos"], job["out"],
            job["reencode"], total, self,
            fix_audio=self._norm_fix_audio([p.path for p in job["photos"]]))
        self._merge_worker.progress.connect(self.merge_panel.set_progress)
        self._merge_worker.result.connect(self._on_group_merge_step)
        self._merge_worker.start()

    def _on_group_merge_step(self, ok: bool, msg: str):
        self._merge_worker = None
        job = self._gm_queue.pop(0)
        if ok:
            self._gm_done.append(msg)
        elif msg == "已取消":
            self._gm_queue.clear()          # 取消则丢弃剩余队列
            self._gm_failed.append(f"{job['label']}：已取消")
        else:
            self._gm_failed.append(f"{job['label']}：{msg}")
        self._next_group_merge()

    def _finish_group_merges(self):
        self.merge_panel.set_busy(False)
        self.merge_panel.progress.setValue(100 if self._gm_done and not self._gm_failed else 0)
        good = not self._gm_failed
        self.merge_panel.hint.setText("分组合并完成。" if good else "分组合并有失败项。")
        self.merge_panel.hint.setStyleSheet(
            f"color: {T.OK if good else T.DANGER}; font-size: 12px;")
        lines = []
        if self._gm_done:
            lines.append(f"成功 {len(self._gm_done)} 个：")
            lines += [f"  {p}" for p in self._gm_done[:12]]
            if len(self._gm_done) > 12:
                lines.append("  …")
        if self._gm_failed:
            lines.append(f"失败 {len(self._gm_failed)} 个：")
            lines += [f"  {s}" for s in self._gm_failed[:12]]
            if len(self._gm_failed) > 12:
                lines.append("  …")
        QMessageBox.information(self, "按组合并", "\n".join(lines))

    # ---------------------------------------------------------------- 新建文件夹
    def _move_groups_to_folders(self, groups, base_dir, folder_template, rename,
                                file_template):
        import shutil
        os.makedirs(base_dir, exist_ok=True)
        ok_items, failed = [], []
        moved: list[tuple[str, "Photo"]] = []    # (旧路径, Photo)
        moved_ok = 0
        total_groups = len(groups)
        for gi, g in enumerate(groups, 1):
            photos = g.get("photos") or []
            p0 = photos[0] if photos else None
            first_dt = p0.taken_dt() if p0 else None
            first_name = (mnaming.strip_index(os.path.splitext(p0.name)[0])
                          if p0 else "")
            spec = mnaming.spec_text(g.get("spec") or {})
            folder_name = mnaming.render(folder_template, index=gi,
                                         total=total_groups, name=first_name,
                                         dt=first_dt, spec=spec)
            if not folder_name:
                folder_name = f"分组_{gi:03d}"
            folder = os.path.join(base_dir, folder_name)
            folder = _unique_path(folder)          # 同名文件夹加 (2)(3)，绝不覆盖
            try:
                os.makedirs(folder, exist_ok=True)
            except Exception as e:
                failed.append(f"{os.path.basename(folder)}（{e}）")
                continue
            width = max(2, len(str(len(photos))))
            for i, p in enumerate(photos, 1):
                ext = os.path.splitext(p.path)[1]
                if rename:
                    stem = mnaming.render(
                        file_template, index=i, total=len(photos),
                        name=mnaming.strip_index(os.path.splitext(p.name)[0]),
                        dt=p.taken_dt(), spec=spec)
                    base = stem or f"{i:0{width}d}"
                else:
                    base = os.path.splitext(p.name)[0]
                dst = _unique_path(os.path.join(folder, base + ext))
                old = p.path
                try:
                    shutil.move(old, dst)
                except Exception as e:
                    failed.append(f"{p.name}（{e}）")
                    continue
                p.path = dst
                p.name = os.path.basename(dst)
                ok_items.append(p)
                moved.append((old, p))
                moved_ok += 1
        # 路径变了: 索引 / 规格 / 模型都跟着换 key
        # ⚠️ v217: 以前是先改 p.path 再拿**新路径**去查以**旧路径**为 key 的
        #    merge_specs → 全部查不到 → 字典被清空, 分组标题还在但规格变"未检测",
        #    再点合并还会被当成"规格不同"劝你转码。照 _move_photos 的做法搬 key。
        self._rebuild_path_index()
        for old, p in moved:
            if old == p.path:
                continue
            spec = self.merge_specs.pop(old, None)
            if spec is not None:
                self.merge_specs[p.path] = spec
            err = self.merge_errors.pop(old, None)
            if err:
                self.merge_errors[p.path] = err
        self.model.rebuild()
        self._update_title()
        self._refresh_top_status()
        if failed:
            QMessageBox.warning(self, "新建文件夹",
                                f"已移动 {moved_ok} 个，失败 {len(failed)} 个：\n\n"
                                + "\n".join(failed[:12])
                                + ("\n…" if len(failed) > 12 else ""))
        else:
            QMessageBox.information(self, "新建文件夹",
                                    f"已把 {len(groups)} 组共 {moved_ok} 个文件移到：\n{base_dir}")

    def _open_with_default(self, path: str):
        """用系统默认程序打开(文件→看图/播放器, 文件夹→资源管理器)。"""
        if not path or not os.path.exists(path):
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as ex:
            QMessageBox.warning(self, "打开失败", f"无法用默认应用打开:\n{path}\n\n{ex}")

    def _open_merge_preview(self):
        """点合并界面右侧的预览图: 用默认程序打开这个文件(和「排序」一键打开一致)。"""
        photos = list(getattr(self.merge_panel, "_photos", None) or [])
        if photos:
            self._open_with_default(photos[0].path)

    def reveal_merge_output(self):
        """「打开位置」: 有合并成品就定位它; 还没合并过就打开保存位置。"""
        path = self._merge_out
        if path and os.path.exists(path):
            self._reveal_path(path)
            return
        folder = (self.merge_panel.out_dir() or "").strip()
        if not folder:
            photos = list(getattr(self.merge_panel, "_photos", None) or [])
            folder = os.path.dirname(photos[0].path) if photos else ""
        if folder and os.path.isdir(folder):
            self._open_with_default(folder)      # 目录: 直接进资源管理器
            return
        QMessageBox.information(self, "打开位置", "还没有可打开的位置（先添加视频或选保存位置）。")

    def open_merge_settings(self):
        try:
            from ..merge.ffmpeg_settings import FFmpegSettingsDialog
            dlg = FFmpegSettingsDialog(self)
            dlg.exec()
        except Exception:
            self._report_ui_error("打开 FFmpeg 设置", "photosort_ffmpeg_error.log")

    def open_exiftool_settings(self):
        """ExifTool(元数据引擎)设置: 自定义路径 / 检测 / 一键更新。"""
        try:
            from .exiftool_settings import ExifToolSettingsDialog
            dlg = ExifToolSettingsDialog(self._dlg_parent())
            dlg.exec()
        except Exception:
            self._report_ui_error("打开 ExifTool 设置", "photosort_exiftool_error.log")
            return
        # 路径 / 使用方式可能被改过: 重新查找并重测全部视频
        self._check_ffmpeg_async(force=True)
        self._start_probe(force=True)

    def open_norm_params(self):
        """v261: 统一页 ⚙ 参数配置弹窗改为非模态 —— 开着也能操作主界面。

        v242-v260 用 exec() 把主窗口锁死; 现在按「属性」窗口的既有做法:
        单一实例 + show() + WA_DeleteOnClose。点「保存」时 QDialog.accept() 会关窗
        并释放实例, 下次再开自然按当前配置重建。
        """
        try:
            from .norm_page import NormParamsDialog
            old = getattr(self, "_norm_params_dialog", None)
            if old is not None:
                try:
                    if old.isVisible():
                        old.raise_()
                        old.activateWindow()
                        return
                    old.close()
                    old.deleteLater()
                except RuntimeError:   # 已被 Qt 销毁
                    pass
                self._norm_params_dialog = None

            token = object()
            self._norm_params_token = token
            dlg = NormParamsDialog(self._dlg_parent())
            dlg.setWindowModality(Qt.NonModal)
            dlg.setAttribute(Qt.WA_DeleteOnClose, True)
            dlg.applied.connect(self._on_norm_params_applied)
            # v246: 外露勾选实时生效 —— 复用同一刷新(档位未变, rebuild 安全)
            dlg.exposureChanged.connect(self._on_norm_params_applied)
            dlg.destroyed.connect(lambda *_: self._forget_norm_params_dialog(token))
            self._norm_params_dialog = dlg
            dlg.show()
            geo = dlg.frameGeometry()
            geo.moveCenter(self.frameGeometry().center())
            dlg.move(geo.topLeft())
            dlg.raise_()
            dlg.activateWindow()
        except Exception:
            self._report_ui_error("打开统一参数配置", "photosort_norm_params_error.log")

    def _forget_norm_params_dialog(self, token):
        """v261: 旧弹窗的延迟销毁回调不能把新弹窗的引用也清掉。"""
        if getattr(self, "_norm_params_token", None) is token:
            self._norm_params_dialog = None
            self._norm_params_token = None

    def _on_norm_params_applied(self):
        """v242: 弹窗改完档位/外露 → 同步顶栏 combo、重建参数格并重算计划。"""
        try:
            panel = self.norm_workspace.panel
            panel.rebuild_params()
            # 参数格数量可能变了, 重新 register 给顶栏槽(v228 压缩逻辑不变)
            self.norm_param_slot.register(panel.param_cells())
            # v244: rebuild 里旧格子是 deleteLater(), 此刻 param_bar.sizeHint().height()
            # 还是 0 —— 直接拿它 setFixedHeight(0) 会把整条参数栏锁死成空白(重启才
            # 恢复)。等事件循环处理完删除与布局重算, 再量真实高度。
            from PySide6.QtCore import QTimer
            def _sync_param_height():
                h = panel.param_host().sizeHint().height()
                if h > 0:
                    self.norm_param_slot.setFixedHeight(h)
            QTimer.singleShot(0, _sync_param_height)
            self.norm_workspace.reload()
        except Exception:
            self._report_ui_error("应用统一参数配置", "photosort_norm_params_apply_error.log")

    def open_param_levels(self):
        """⚙️ 参数分档设置: 弹窗内改档立即生效并保存(弹窗自己回调 _on_param_levels_changed)。"""
        try:
            from .dialogs import ParamLevelsDialog
            dlg = ParamLevelsDialog(self._on_param_levels_changed, self)
            dlg.exec()
        except Exception:
            self._report_ui_error("打开参数分档设置", "photosort_levels_error.log")

    def _on_param_levels_changed(self):
        """分档变了: 重新分组(签名随 fatal 档变) + 刷新右栏与全选状态。"""
        self._apply_merge_groups()
        self.merge_panel.set_context(self.merge_specs, self.merge_errors,
                                     bool(self.merge_ffmpeg))
        self.merge_panel.set_selection(self.view.selected_photos())
        self._sync_lossless_btn()
        self._sync_solo_btn()
        self.view.viewport().update()

    # ---------------------------------------------------------------- 合并模式右键
    def _show_inspector_menu(self, pos):
        """排序模式：右侧预览图上右键，弹左侧那张菜单。"""
        self._show_context_menu(pos, self.inspector.preview.mapToGlobal(pos))

    def _show_merge_preview_menu(self, pos):
        """合并模式：右侧预览图上右键，弹左侧那张菜单。"""
        self._show_merge_menu(pos, self.merge_panel.preview.mapToGlobal(pos))

    def _show_merge_menu(self, pos, global_pos=None):
        menu = self._merge_menu()
        menu.exec(global_pos if global_pos is not None
                  else self.view.viewport().mapToGlobal(pos))

    def _merge_menu(self) -> QMenu:
        """合并界面的右键菜单(单独构建, 离屏测试能直接看内容, 不用真弹窗)。"""
        photos = self.view.selected_photos()
        menu = self._hand_menu()
        n = len(photos)
        if n:
            menu.addAction(f"{n}个移动到…", self.move_to_merge_folder)
            # v199: 「新增剪辑」+「发送到剪辑1/2…」——每次可新开窗口, 或送进已有窗口
            new_act = BoldPartMenuAction(
                menu, "新增", "剪辑",
                tip="用这几个文件新开一个剪辑窗口（时间线不会被动）")
            new_act.triggered.connect(
                lambda _=False, ps=photos: self._new_clip_session(ps))
            menu.addAction(new_act)
            for sess in self._clip_sessions:
                menu.addAction(
                    f"发送到{sess.title}",
                    lambda _=False, ps=photos, s=sess: self.send_to_edit(ps, s))
            menu.addSeparator()
        menu.addAction("全选可无损", self.toggle_select_lossless)
        menu.addAction("全选所有视频", self.view.select_all_photos)
        menu.addSeparator()
        if n:
            menu.addAction("打开文件所在位置", self.reveal_selected)
            menu.addAction("从列表中移除（不会删除文件）", self._remove_selected_from_list)
            menu.addSeparator()
            if n >= 2:
                menu.addAction("对比…", self.open_compare)
            menu.addAction("属性…", self.show_properties)
        return menu

    # ---------------------------------------------------------------- 统一格式(v200)
    def _norm_fix_audio(self, paths) -> bool:
        """这批源文件是否全都已经在用工作副本 —— 是才做"视频复制+音频重编一次"。"""
        from ..merge import norm_paths
        paths = [p for p in (paths or []) if p]
        return bool(paths) and all(norm_paths.is_normalized(p) for p in paths)

    def open_normalize(self):
        """合并右栏「统一格式」(v206): 跳到统一的第三大形态, 不再弹独立窗口。"""
        self.goto_norm()

    def _norm_open_preview(self):
        """v209: 点统一右栏预览图 → 用默认程序打开首个勾选视频。"""
        sel = self.norm_workspace.selected_paths
        pics = [p for p in self.norm_workspace.photos
                if not sel or getattr(p, "path", "") in sel]
        if pics:
            self._open_with_default(pics[0].path)

    def _norm_reveal(self):
        """v209: 「打开位置」→ 定位已生成的统一副本 / 源视频所在目录。"""
        done = list(getattr(self, "_normalize_ok", []) or [])
        if done:
            self._reveal_path(done[-1])
            return
        sel = self.norm_workspace.selected_paths
        pics = [p for p in self.norm_workspace.photos
                if not sel or getattr(p, "path", "") in sel]
        if pics:
            self._open_with_default(os.path.dirname(pics[0].path))
        else:
            QMessageBox.information(self, "打开位置", "还没有可打开的位置（先在左侧勾选视频）。")

    def _clean_norm_copies(self):
        """v217: ⚙ → 清理统一格式副本。

        以前副本没有任何清理入口: 换了档位/换了片子, 旧的副本(每个都是完整视频)
        一直躺在 `_photosort_norm/` 里, 只有用户自己去资源管理器里翻。
        这里只删自动生成的副本, **源视频一个都不碰**。
        """
        from ..merge import norm_paths, pathsafe
        # v222: 统一还在转码时, ffmpeg 正往副本目录里写文件 —— 这时候删副本
        # 会删到半成品, 还可能让正在进行的转码报错。
        if getattr(self, "_normalize_worker", None) is not None:
            QMessageBox.information(
                self._dlg_parent(), "统一进行中",
                "统一格式还在后台转码（副本正在写入），\n"
                "请等它完成，或先在进度窗口里取消，再来清理副本。")
            return
        folders: list[str] = []
        for s in (self.sources or []):
            p = (s or {}).get("path") or ""
            if p and os.path.isdir(p) and pathsafe.norm(p) not in [
                    pathsafe.norm(x) for x in folders]:
                folders.append(p)
        # v234: 用户给「统一」指定了副本根目录时, 也纳入清理扫描(副本可能不在源目录)
        try:
            from ..merge import settings as _st
            custom_root = _st.load_normalize_out_dir()
            if custom_root and os.path.isdir(custom_root) \
                    and pathsafe.norm(custom_root) not in [
                        pathsafe.norm(x) for x in folders]:
                folders.append(custom_root)
        except Exception:
            pass
        if not folders:
            for ph in self.model.all_photos()[:1]:
                d = os.path.dirname(ph.path)
                if d and os.path.isdir(d):
                    folders.append(d)
        if not folders:
            QMessageBox.information(self, "清理统一格式副本",
                                    "还没有打开过文件夹，没有可清理的副本。")
            return
        paths: list[str] = []
        seen: set[str] = set()
        for f in folders:
            for p in norm_paths.find_work_copies(f):
                k = os.path.normcase(os.path.abspath(p))
                if k not in seen:
                    seen.add(k)
                    paths.append(p)
        if not paths:
            QMessageBox.information(
                self, "清理统一格式副本",
                "没有找到统一格式副本。\n\n"
                f"（副本放在源视频同级的 {norm_paths.WORK_SUBDIR} 文件夹里）")
            return
        size = norm_paths.work_copies_size(paths)
        if not confirm_yes_no(
                self._dlg_parent(), "清理统一格式副本",
                f"将删除 <b>{len(paths)}</b> 个统一副本，释放约 "
                f"<b>{_fmt_bytes(size)}</b>。<br><br>"
                "源视频不受影响；下次「开始统一」会重新生成副本。<br>"
                "确定要清理吗？",
                center=True, red_yes=True, default_yes=False):
            return
        n, freed, failed = norm_paths.remove_work_copies(paths)
        # 副本没了, 合并规格要按原片重新探测
        for ph in self.model.all_photos():
            self.merge_specs.pop(ph.path, None)
        self._start_probe()
        msg = f"已删除 {n} 个副本，释放 {_fmt_bytes(freed)}。"
        if failed:
            msg += f"\n\n有 {len(failed)} 个删不掉（可能正在被占用）：\n" + "\n".join(
                os.path.basename(p) for p, _ in failed[:5])
        QMessageBox.information(self._dlg_parent(), "清理统一格式副本", msg)
        self.status_label.setText(msg.splitlines()[0])
        self._refresh_top_status()

    def _forget_normalize_dialog(self, *_):
        self._norm_dialog = None

    def _run_normalize(self, profile: dict, plan):
        from ..merge import normalize
        from ..merge.workers import NormalizeWorker
        if getattr(self, "_normalize_worker", None) is not None:
            QMessageBox.information(
                self._dlg_parent(), "统一进行中",
                "统一格式还在后台转码，请等它完成，或先在进度窗口里取消。")
            return
        items = list(getattr(plan, "items", []))
        if not items:
            return
        # v217: 生成副本的档位必须和之后解析副本的档位一致 —— 先把它落成"当前档"
        try:
            from ..merge import settings as _st
            _st.save_normalize_profile_current(profile)
        except Exception:
            pass
        prof = normalize.NormProfile.from_dict(profile)
        # v219: 声道目标按整批算(合并侧 fatal, 转码不显式 -ac 就永远统一不了)
        channels = getattr(plan, "target_channels", None)
        if not channels:
            channels = normalize.pick_target_channels(
                [getattr(it, "spec", None) for it in items])
        self._normalize_ok = []
        self._normalize_worker = NormalizeWorker(
            items, prof, self.merge_ffmpeg, self.merge_ffprobe, self,
            channels=channels)
        dlg = ProgressDialog("统一格式中", self._dlg_parent(), cancelable=False,
                             close_guard=self._confirm_cancel_normalize,
                             minimizable=True, modal=False)
        dlg.bar.setMaximum(100)
        dlg.bar.setValue(0)
        dlg.label.setText("准备中…")
        dlg.log.setPlainText(
            "原文件不会被改动；正在生成统一规格的工作副本。\n"
            "转码在后台进行，期间可以正常使用软件的其他功能。")
        self._normalize_worker.progress.connect(
            lambda pct, eta: self._normalize_progress(dlg, pct, eta))
        self._normalize_worker.one.connect(
            lambda i, ok, result, skipped: self._on_normalize_one(
                dlg, items, i, ok, result, skipped))
        self._normalize_worker.finished_all.connect(
            lambda: self._on_normalize_done(dlg, items))
        # v222: 非模态 —— 转码期间主窗口保持可用; 进度框记录在 _norm_dialog,
        # 主窗关闭/清理副本时用它做协调(见 closeEvent / _clean_norm_copies)。
        self._norm_dialog = dlg
        dlg.destroyed.connect(self._forget_normalize_dialog)
        self._normalize_worker.start()
        dlg.show()

    def _normalize_progress(self, dlg, pct, eta):
        dlg.bar.setValue(max(0, min(100, int(pct))))
        txt = f"统一格式中 {int(pct)}%"
        try:
            if eta and float(eta) > 0:
                txt += f"　还剩约 {int(float(eta))} 秒"
        except Exception:
            pass
        dlg.label.setText(txt)
        # v222: 非模态后用户可能切到别的页面 —— 顶栏状态同步一份, 走哪都能看到
        try:
            self.status_label.setText(txt)
        except Exception:
            pass

    def _on_normalize_one(self, dlg, items, i: int, ok: bool,
                          result: str, skipped: bool):
        try:
            name = os.path.basename(items[i].path)
        except Exception:
            name = f"第 {i + 1} 个"
        if skipped:
            dlg.on_log(f"{name}：已有工作副本，跳过。", 0)
            self._normalize_ok.append(getattr(items[i], "path", ""))
        elif ok:
            dlg.on_log(f"{name}：完成 → {os.path.basename(result)}", 0)
            self._normalize_ok.append(getattr(items[i], "path", ""))
        else:
            dlg.on_log(f"{name}：失败 — {result}", 2)

    def _confirm_cancel_normalize(self, dlg) -> bool:
        if getattr(self, "_normalize_worker", None) is None:
            return True
        yes = confirm_yes_no(
            dlg, "取消统一格式",
            "统一格式还没完成，确定要取消吗？<br><br>"
            "选「是」会停止当前转码并删掉半成品；已完成的工作副本会保留。",
            red_yes=True, default_yes=False)
        if not yes:
            return False
        self._normalize_worker.stop()
        return False

    def _on_normalize_done(self, dlg, items):
        self._normalize_worker = None
        dlg.finish()
        if dlg.isMinimized():
            dlg.showNormal()
            dlg.raise_()
        done = list(getattr(self, "_normalize_ok", []))
        failed = [i.path for i in items if i.path not in done]
        dlg.label.setText("统一格式完成" if not failed else "统一格式结束（有失败项）")
        # v222: 非模态 —— 主窗口状态栏也报一声(用户可能根本没盯着进度框)
        self.status_label.setText(
            f"统一格式完成：成功 {len(done)} 个，失败 {len(failed)} 个")
        for path in done:
            self.merge_specs.pop(path, None)      # 强制按工作副本重新探测
        if done:
            self._start_probe()
        self._refresh_top_status()

    def send_to_edit(self, photos=None, sess: ClipSession = None):
        """「发送到剪辑N」/「新增剪辑」: 文件进某个会话的素材框, 时间线不动。

        v199 修复旧 bug: 以前窗口已开着时 enter_edit() 直接 return,
        「允许素材集」(_edit_allowed) 不刷新, 新勾的文件全被素材框过滤掉 ——
        看着就像"点了没反应"。现在改为显式补 allowed + 刷新素材框。
        """
        photos = list(photos if photos is not None else self.view.selected_photos())
        paths = [p.path for p in photos if getattr(p, "path", "")]
        try:
            if not self.merge_mode:
                self.enter_merge()
            if sess is None:
                sess = self._active_session
            if sess is None:
                # 还没有任何剪辑窗口: 用这批文件开第一个会话
                self._new_clip_session(photos)
                return
            # 切到目标会话(会停掉原会话声音), 再把文件补进它的素材集
            self._activate_session(sess)
            allowed = set(self._edit_allowed or set())
            allowed |= {self._canon(p) for p in paths}
            self._edit_allowed = allowed
            for path in paths:
                self._remember_extra(path)
            self._unhide_media(paths)
            if sess.window is not None:
                sess.window.show_for(self)
            self._refresh_media_panel()
            self._start_clip_probe_if_needed()
            self.status_label.setText(
                f"已把 {len(paths)} 个文件放进 {sess.title} 的素材框，"
                "点格子右下角 ＋ 加到时间线")
        except Exception:
            self._report_ui_error("发送到剪辑", "photosort_edit_error.log")

    def move_to_merge_folder(self):
        """「单独」板块 / 右键「N个移动到…」: 搬主列表选中的文件。"""
        self._move_photos(self.view.selected_photos())

    def _move_from_compare(self, photos):
        """v198: 对比窗口右键「N个移动到…」→ 与主界面同一个搬文件流程。"""
        photos = [p for p in (photos or []) if p is not None]
        if not photos:
            QMessageBox.information(self, "移动", "没有可移动的文件。")
            return
        self._move_photos(photos)

    def _move_photos(self, photos):
        """把这几个文件挪进目标文件夹, 可选按顺序重命名成 01_原名.ext。

        v198: 对比窗口也能调用(传入它自己选中的 Photo); 搬完后表头名字、
        预览图路径、规格缓存的 key 全部跟着换新, 所有开着的对比窗口原地刷新。
        """
        photos = list(photos or [])
        if not photos:
            QMessageBox.information(self, "移动", "请先选中要移动的视频。")
            return
        base_dir = os.path.dirname(photos[0].path)
        target_dir = _unique_subdir(base_dir, "合并")

        # 规格是否一致(只影响提示, 不阻止移动)
        sigs = {mgrouping.signature(self.merge_specs.get(p.path)) for p in photos}
        mixed = len(sigs) > 1

        dlg = MoveToMergeDialog(len(photos), target_dir, mixed, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        target_dir = dlg.chosen_dir()
        rename = dlg.rename_enabled()
        if not target_dir:
            QMessageBox.warning(self, "移动", "请填写目标位置。")
            return

        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "移动失败",
                                 f"无法创建目标文件夹：\n{target_dir}\n\n{e}")
            return

        import shutil
        width = max(2, len(str(len(photos))))
        ok_items, failed, moved = [], [], []
        for i, p in enumerate(photos, 1):
            ext = os.path.splitext(p.path)[1]
            if rename:
                stem = _strip_leading_index(os.path.splitext(p.name)[0])
                base = f"{i:0{width}d}_{stem}" if stem else f"{i:0{width}d}"
            else:
                base = os.path.splitext(p.name)[0]
            dst = _unique_path(os.path.join(target_dir, base + ext))
            old = p.path
            try:
                shutil.move(p.path, dst)
            except Exception as e:
                failed.append(f"{p.name}（{e}）")
                continue
            p.path = dst
            p.name = os.path.basename(dst)
            ok_items.append(p)
            moved.append((old, p))

        # 路径变了: 索引、缩略图状态、探测结果都跟着换 key
        # ⚠️ 不能拿新路径去 merge_specs 里查(旧路径才是 key), 也不能只保留
        # 本次移动的文件 —— 那样没被移动的文件规格会整批丢掉。
        self._rebuild_path_index()
        for old, p in moved:
            if old == p.path:
                continue
            spec = self.merge_specs.pop(old, None)
            if spec is not None:
                self.merge_specs[p.path] = spec
            err = self.merge_errors.pop(old, None)
            if err:
                self.merge_errors[p.path] = err
        self.model.rebuild()
        self._update_title()
        self._refresh_top_status()
        # v198: 对比窗口里的 Photo 与主列表是同一批对象; 表头名字虽会跟着变,
        # 预览图里存的还是旧路径, 不重建的话点开会找不到文件。
        for win in list(getattr(self, "_cmp_dialogs", [])):
            try:
                win.refresh_after_move()
            except (RuntimeError, AttributeError):
                pass

        if failed:
            QMessageBox.warning(self, "部分文件未能移动",
                                f"已移动 {len(ok_items)} 个，失败 {len(failed)} 个：\n\n"
                                + "\n".join(failed[:12])
                                + ("\n…" if len(failed) > 12 else ""))
        else:
            detail = "文件名已按选中顺序加上序号。" if rename else "文件名保持不变。"
            QMessageBox.information(self, "已移动",
                                    f"{len(ok_items)} 个文件已移到：\n{target_dir}\n\n{detail}")

    def _rebuild_path_index(self):
        self.path_index = {p.path: p for p in self.model.photos}

    def clear_list(self):
        """清空当前已加载的所有照片/文件(内存中), 不删除磁盘文件, 不撤销已写入改动。"""
        if not self.model.photos:
            self.status_label.setText("列表已经是空的")
            return
        n = len(self.model.photos)
        red = T.DANGER
        text = (f"将<font color='{red}'>移除</font>此处已"
                f"<font color='{red}'>加载</font>的 {n} 个文件，"
                f"<font color='{red}'>不会</font>在电脑中"
                f"<font color='{red}'>删除</font>文件。")
        if not confirm_yes_no(self, "清空", text, center=True, red_yes=False):
            return
        self.sources = []
        # 注意: 不清空会话文件, 这样"恢复上次来源"仍能找回清空前的工作集
        self.model.set_folder("", False)
        self.model.set_photos([])
        if self.merge_mode:
            self.model.set_merge_groups([])
            self.merge_panel.set_selection([])
        self.path_index = {}
        self._update_title()
        self.stack.setCurrentWidget(self.empty_page)
        self.status_label.setText("列表已清空，可重新打开文件夹或文件")
        self._update_pending()

    def save_sources(self):
        """把当前的来源集合(文件夹 + 文件)保存成 .photolist 清单, 供日后一键重载。"""
        if not self.sources:
            QMessageBox.information(self, "没有来源", "当前列表是空的，没有可保存的来源。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存来源清单", "照片来源.photolist", "照片来源清单 (*.photolist)")
        if not path:
            return
        if not path.lower().endswith(".photolist"):
            path += ".photolist"
        data = {"version": 1, "sources": self.sources}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.status_label.setText(f"已保存来源清单：{os.path.basename(path)}")
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    def export_csv(self):
        """把当前列表(按当前顺序)导出为 CSV, 方便写入前核对顺序与时间计划。"""
        from datetime import datetime
        if not self.model.photos:
            QMessageBox.information(self, "没有内容", "当前列表是空的，没有可导出的内容。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出清单 (CSV)", "照片清单.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            import csv
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                wtr = csv.writer(f)
                wtr.writerow(["序号", "文件名", "路径", "原拍摄时间", "计划拍摄时间", "大小(字节)"])
                for i, p in enumerate(self.model.photos, 1):
                    taken = p.taken_dt()
                    taken_str = taken.strftime("%Y-%m-%d %H:%M:%S") if taken else ""
                    planned = p.planned.get(F.TAKEN)
                    if isinstance(planned, datetime):
                        planned_str = planned.strftime("%Y-%m-%d %H:%M:%S")
                    elif planned:
                        planned_str = str(planned)
                    else:
                        planned_str = ""
                    wtr.writerow([i, p.name, p.path, taken_str, planned_str, p.size])
            self.status_label.setText(f"已导出清单：{os.path.basename(path)}（{len(self.model.photos)} 行）")
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def _save_session(self):
        """把当前来源集合自动持久化到会话文件(供'恢复上次来源'使用)。"""
        try:
            with open(self.session_path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "sources": self.sources}, f,
                          ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _apply_sources_file(self, path: str):
        """读取 .photolist 并替换当前列表(被'打开来源清单'与'恢复上次来源'复用)。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            QMessageBox.warning(self, "无法读取", str(exc))
            return
        sources = data.get("sources", [])
        if not sources:
            QMessageBox.information(self, "清单为空", "这份清单里没有来源。")
            return
        if self.model.photos:
            ret = QMessageBox.question(
                self, "打开来源清单",
                "打开清单会替换当前已加载的列表（不删除磁盘文件），继续吗？")
            if ret != QMessageBox.Yes:
                return
        # 收集所有文件(去重)
        all_files: list[str] = []
        seen: set[str] = set()
        for s in sources:
            if s.get("type") == "folder" and os.path.isdir(s["path"]):
                for fp in collect_files(s["path"], s.get("recursive", False)):
                    k = os.path.normcase(os.path.abspath(fp))
                    if k not in seen:
                        seen.add(k)
                        all_files.append(fp)
            elif s.get("type") == "files":
                for fp in s.get("paths", []):
                    if os.path.isfile(fp):
                        k = os.path.normcase(os.path.abspath(fp))
                        if k not in seen:
                            seen.add(k)
                            all_files.append(fp)
        if not all_files:
            QMessageBox.information(self, "没有可用文件",
                                    "清单里的来源当前都找不到文件（可能路径已变动）。")
            return
        self.sources = sources
        self.model.set_folder(self._primary_dir(), False)
        self._update_title()
        self._save_session()
        self._scan_files(all_files, accumulate=False)

    def load_sources(self):
        """从 .photolist 清单重载来源集合(替换当前列表)。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "打开来源清单", "", "照片来源清单 (*.photolist)")
        if not path:
            return
        self._apply_sources_file(path)

    def restore_session(self):
        """恢复上次自动保存的来源集合(替换当前列表)。"""
        if not os.path.exists(self.session_path):
            QMessageBox.information(self, "没有上次来源",
                                    "尚未自动保存过来源集合，无法恢复。先打开一些文件夹/文件即可。")
            return
        self._apply_sources_file(self.session_path)

    def _start_scan_files(self, files: list[str], accumulate: bool = True):
        """直接扫描指定文件列表（不从文件夹收集）。"""
        self._scan_files(files, accumulate)

    def _scan_files(self, files: list[str], accumulate: bool = False):
        if self.worker is not None and self.worker.isRunning():
            # v217: 以前这里直接 return —— 拖文件进来/添加时如果正在扫描, 文件就
            # 被静默丢掉: 来源已经记下了、标题数字也变大了, 列表却不长。
            # 现在改成排队: 累加的进累加队列, 整体替换的存一份"下一轮重扫"。
            if accumulate:
                self._enqueue_scan(files)
            else:
                self._pending_replace = list(files)
                QTimer.singleShot(0, self._drain_picker_pending)
            return
        if getattr(self, "norm_mode", False):
            self._restore_mode_page()   # 统一页保持自己的页, 不回排序
        else:
            self.stack.setCurrentWidget(self.view)
        self.thumb_worker.clear()
        self.worker = Scanner(self.model.folder, files, self)
        self.worker.progress.connect(self._on_scan_progress)
        self.worker.finished_scan.connect(
            lambda photos: self._on_scan_finished(photos, accumulate))
        self.worker.failed.connect(lambda msg: QMessageBox.warning(self, "刷新失败", msg))
        # 「添加」弹窗还开着时可能已经又实时加了几批：这一批真跑完就接着扫下一批
        self.worker.finished.connect(self._drain_picker_pending)
        self.status_label.setText(f"正在读取 {len(files)} 个文件的元数据…")
        self.worker.start()

    def _on_scan_progress(self, done: int, total: int, name: str):
        self.status_label.setText(f"读取元数据 {done} / {total}　{name}")

    def _on_scan_finished(self, photos: list, accumulate: bool = False):
        if accumulate:
            self.model.add_photos(photos, dedupe=True)
        else:
            self.model.set_photos(photos)
        self.path_index = {p.path: p for p in self.model.photos}
        self._restore_mode_page()
        n = len(self.model.photos)
        # 不再在底部状态栏显示"拖拽可调整顺序"这类提示, 让界面更干净
        self.status_label.setText(f"已载入 {n} 个文件")
        self._update_pending()
        self.view.request_visible_thumbs()
        # 合并模式下列表变了(添加/刷新/清除): 重新分组, 只补测还没规格的新视频
        if self.merge_mode:
            self._restart_merge_view()
        # v207: 统一形态下列表变了: 重算 plan + 铺分组网格 + 补测, 不踢回排序
        elif getattr(self, "norm_mode", False):
            self._restart_norm_view()
        # 首次打开(非累加)时自动选中第一张照片, 立即在右侧面板显示数据
        if not accumulate and photos:
            QTimer.singleShot(50, self._select_first_photo)

    # ---------------------------------------------------------------- 来源管理
    def _primary_dir(self) -> str:
        """返回用于快照标注/回滚显示的代表目录(首个文件夹来源, 否则首个文件所在目录)。"""
        for s in self.sources:
            if s["type"] == "folder":
                return s["path"]
        for s in self.sources:
            if s["type"] == "files" and s["paths"]:
                return os.path.dirname(s["paths"][0])
        return ""

    def _win_title(self, suffix=""):
        return f"照片排序台 v{__build__}{suffix}"


    def _update_title(self):
        n_folder = sum(1 for s in self.sources if s["type"] == "folder")
        n_file_src = sum(1 for s in self.sources if s["type"] == "files")
        n_files = sum(len(s["paths"]) for s in self.sources if s["type"] == "files")
        if not self.sources:
            self.path_label.setText("未选择文件夹")
            self.setWindowTitle(self._win_title())
            self._sync_path_tooltip()
            return
        if len(self.sources) == 1 and self.sources[0]["type"] == "folder":
            f = self.sources[0]
            self.path_label.setText(f["path"])
            self.setWindowTitle(self._win_title(f" — {os.path.basename(f['path']) or f['path']}"))
        elif len(self.sources) == 1 and self.sources[0]["type"] == "files":
            f = self.sources[0]
            folder = os.path.dirname(f["paths"][0])
            self.path_label.setText(f"{folder} （{len(f['paths'])} 个文件）")
            self.setWindowTitle(self._win_title(f" — {len(f['paths'])} 个文件"))
        else:
            self.path_label.setText(
                f"已累加：{n_folder} 个文件夹 + {n_files} 个文件")
            self.setWindowTitle(self._win_title(f" — 累加 {n_folder} 文件夹 / {n_files} 文件"))
        self._sync_path_tooltip()

    def _select_first_photo(self):
        if self.merge_mode:
            return          # 合并模式下不自动勾选, 交给用户自己选
        m = self.model
        from ..core.photo import PhotoRow
        from PySide6.QtCore import QItemSelectionModel
        for i in range(len(m.rows)):
            if isinstance(m.rows[i], PhotoRow):
                idx = m.index(i)
                self.view.selectionModel().select(idx, QItemSelectionModel.ClearAndSelect)
                self.view.scrollTo(idx)
                break

    # ---------------------------------------------------------------- 分组开关
    def _on_master_toggle(self, _name: str = ""):
        year = self.sw_year.isChecked()
        month = self.sw_month.isChecked()
        day = self.sw_day.isChecked()
        self.sw_month.setEnabled(year)
        self.sw_day.setEnabled(year)
        if not year:
            self.sw_month.setChecked(False)
            self.sw_day.setChecked(False)
        # v225: 开"日"自动开"月" —— 与组标题开关(_on_group_toggle)、统一页
        # (开帧率自动开分辨率)同一条规则。只对"日"开关**自身**被点亮生效
        # (_name 区分), 否则"关月连带关日"等关闭级联会被反向弹回。
        # 不补这条的话"日"会被下面的 not month 分支立刻弹回去(开关没亮),
        # 而外层那次 set_master 仍把 master["day"] 写成 True —— 开关与实际
        # 分组状态不一致。
        if _name == "day" and day and not month:
            self.sw_month.blockSignals(True)
            self.sw_month.setChecked(True)
            self.sw_month.blockSignals(False)
            month = True
        if not month:
            self.sw_day.setChecked(False)
        # 落盘以开关的**最终状态**为准: 开关级联会递归进本方法, 内层已经把
        # 中间状态写过一遍; 外层再用进入时读到的旧值调 set_master 会覆盖
        # 内层 —— 表现为"开关灭了、model 里却还开着"(v225 修的状态不一致)。
        self.model.set_master(self.sw_year.isChecked(), self.sw_month.isChecked(),
                              self.sw_day.isChecked())

    def _on_group_toggle(self, year: int, which: str, on: bool):
        """分组标题里的月/日开关被点击。

        - 只影响该年份分组
        - 如果上方总开关是关的, 自动把它打开（仅作为"有分组启用"的指示）
        - 不会影响其它分组的开关状态
        - 日 依赖 月: 开 日 时自动开 月
        """
        self.model.set_year_toggle(year, which, on)
        # 日依赖月: 开日时自动开月
        if on and which == "day":
            st = self.model.year_state(year)
            if not st.get("month"):
                self.model.set_year_toggle(year, "month", True)
                top_month = self.sw_month
                if not top_month.isChecked():
                    top_month.blockSignals(True)
                    top_month.setChecked(True)
                    self.model.master["month"] = True
                    top_month.blockSignals(False)
        # 同步顶栏开关
        top_sw = self.sw_month if which == "month" else self.sw_day
        if on and not top_sw.isChecked():
            top_sw.blockSignals(True)
            top_sw.setChecked(True)
            self.model.master[which] = True
            top_sw.blockSignals(False)
        elif not on:
            any_on = any(st.get(which, False) for st in self.model.per_year.values())
            if not any_on and top_sw.isChecked():
                top_sw.blockSignals(True)
                top_sw.setChecked(False)
                self.model.master[which] = False
                top_sw.blockSignals(False)
                # 关月时也关日
                if which == "month" and self.sw_day.isChecked():
                    self.sw_day.blockSignals(True)
                    self.sw_day.setChecked(False)
                    self.model.master["day"] = False
                    self.sw_day.blockSignals(False)

    def _on_filter(self, text: str):
        self.model.filter_text = text.strip()
        self.model.rebuild()

    # ---------------------------------------------------------------- 统一页层级开关
    def _sync_norm_switches(self):
        """把顶栏 编码/分辨率/帧率 三个胶囊对齐到 model.norm_master 上(代码改状态时用)。"""
        for sw, key in ((self.sw_codec, "codec"), (self.sw_res, "res"),
                        (self.sw_fps, "fps")):
            sw.blockSignals(True)
            sw.setChecked(bool(self.model.norm_master.get(key, True)))
            sw.blockSignals(False)
        codec = self.sw_codec.isChecked()
        self.sw_res.setEnabled(codec)
        # 帧率依赖分辨率: 分辨率关着时"帧率"没有意义(和 日 依赖 月 同一条规则)
        self.sw_fps.setEnabled(codec and self.sw_res.isChecked())

    def _on_norm_master_toggle(self, _name: str = ""):
        """统一页顶栏 编码/分辨率/帧率 总开关(口径与排序的 年/月/日 完全一致)。"""
        codec = self.sw_codec.isChecked()
        res = self.sw_res.isChecked()
        fps = self.sw_fps.isChecked()
        self.sw_res.setEnabled(codec)
        if not codec:
            self.sw_res.setChecked(False)
            self.sw_fps.setChecked(False)
        if not res:
            self.sw_fps.setChecked(False)
        self.sw_fps.setEnabled(codec and res)
        keep = {p.path for p in self.view.selected_photos()}
        self.model.set_norm_master(codec, res, fps)
        self._after_norm_regroup(keep)

    def _on_norm_group_toggle(self, key: tuple, which: str, on: bool):
        """统一页某个「编码」组标题上的 分辨率/帧率 开关被点击。

        - 只影响该编码组(其它组的开关不变)
        - 开 帧率 时自动开 分辨率(与排序"开日必开月"同一条规则)
        - 顶栏总开关跟着亮/灭, 保持"总开关=有没有这一级"的直觉
        """
        key = tuple(key or ())
        if on and which == "fps":
            st = self.model.norm_state(key)
            if not st.get("res"):
                self.model.set_norm_toggle(key, "res", True)
                top = self.sw_res
                if not top.isChecked():
                    top.blockSignals(True)
                    top.setChecked(True)
                    self.model.norm_master["res"] = True
                    top.blockSignals(False)
        keep = {p.path for p in self.view.selected_photos()}
        self.model.set_norm_toggle(key, which, on)
        top_sw = self.sw_res if which == "res" else self.sw_fps
        if on and not top_sw.isChecked():
            top_sw.blockSignals(True)
            top_sw.setChecked(True)
            self.model.norm_master[which] = True
            top_sw.blockSignals(False)
        elif not on:
            any_on = any(st.get(which, False) for st in self.model.per_norm.values())
            if not any_on and top_sw.isChecked():
                top_sw.blockSignals(True)
                top_sw.setChecked(False)
                self.model.norm_master[which] = False
                top_sw.blockSignals(False)
                if which == "res" and self.sw_fps.isChecked():
                    self.sw_fps.blockSignals(True)
                    self.sw_fps.setChecked(False)
                    self.model.norm_master["fps"] = False
                    self.sw_fps.blockSignals(False)
        self.sw_res.setEnabled(self.sw_codec.isChecked())
        self.sw_fps.setEnabled(self.sw_codec.isChecked() and self.sw_res.isChecked())
        self._after_norm_regroup(keep)

    def _after_norm_regroup(self, keep_paths=None):
        """层级开关改完: 按路径把勾选投影回来(否则一开一关就把整批勾选清空,
        「开始统一」会变成"没有勾选")。

        v220: 权威集合是 `_grid_sel_paths`(重建不会丢), `keep_paths` 只为兼容旧调用。
        """
        if not self.norm_mode:
            return
        self._apply_grid_selection(self._grid_sel_paths)
        self._sync_norm_checked()
        self._sync_group_checks()
        self._refresh_top_status()

    def _on_order_changed(self):
        self._update_pending()
        # 合并模式拖拽会重建行(HeaderRow 是新建的) → 勾选框状态要按选中重算
        self._sync_group_checks()
        # v217: 排序态拖完要跟时间计划对齐(详见 _maybe_reapply_plan)
        if not self.merge_mode and not getattr(self, "norm_mode", False):
            self._maybe_reapply_plan()

    # ---------------------------------------------------------------- 时间计划跟随顺序
    def _remember_plan(self, photos, start, step: float,
                       direction: str, mode: str):
        """记下"这次自动间隔用了什么参数", 供顺序变化后重算。"""
        self._last_plan = {
            "ids": [id(p) for p in photos],
            "start": start, "step": float(step),
            "direction": direction, "mode": mode,
            "snapshot": {id(p): dict(p.planned) for p in photos},
        }
        self._plan_stale = False

    def _maybe_reapply_plan(self):
        """顺序变了 → 按上次的间隔重排时间。

        "第 1 张最新、逐张递减" 只在点"自动间隔"那一刻成立, 之后一拖就脱钩:
        界面顺序变了, 即将写入的时间还是老的, 而且没有任何提示。这里按新顺序
        重算一遍。
        ⚠️ 只在**没人手改过时间**时才自动重算(拿 apply 时的快照逐张比对);
           有人手改过就只提示"没有自动重排", 绝不悄悄把用户改的值冲掉。
        """
        plan = getattr(self, "_last_plan", None)
        if not plan:
            return
        by_id = {id(p): p for p in self.model.photos}
        if any(i not in by_id for i in plan["ids"]):
            self._last_plan = None        # 集合变了, 计划作废
            return
        # ⚠️ 必须按"当前列表顺序"排, 不能按记下来的旧顺序 —— 否则重排的是旧次序,
        #    新拖到第一位的那张拿到的却是它原来的时间。
        pos = {id(p): i for i, p in enumerate(self.model.photos)}
        photos = sorted((by_id[i] for i in plan["ids"]),
                        key=lambda p: pos.get(id(p), 0))
        edited = [p for p in photos
                  if dict(p.planned) != plan["snapshot"].get(id(p), {})]
        if edited:
            if not getattr(self, "_plan_stale", False):
                self._plan_stale = True
                self.status_label.setText(
                    f"顺序已变：有 {len(edited)} 个文件的时间被手改过，"
                    "没有自动重排（如需统一请重新点「自动间隔」）")
            return
        timeplan.apply_plan(photos, plan["start"], plan["step"],
                            plan["direction"], plan["mode"])
        plan["snapshot"] = {id(p): dict(p.planned) for p in photos}
        self._plan_stale = False
        first = photos[0].planned.get(F.TAKEN) if photos else None
        tip = f"顺序已变，已按原间隔重排 {len(photos)} 个文件的时间"
        if first:
            tip += f"（第 1 张 = {first:%Y-%m-%d %H:%M:%S}）"
        self.status_label.setText(tip)
        self.model.rebuild()
        self._update_pending()

    # ---------------------------------------------------------------- 选择
    def _on_selection_changed(self, selected=None, deselected=None):
        photos = self.view.selected_photos()
        if self.merge_mode or getattr(self, "norm_mode", False):
            # v231: 用户手势(点/框选/Ctrl+A)选中了多文件组块 → 整组勾上;
            # 取消选中 → 整组勾去掉。我们自己的投影(_apply_grid_selection)带
            # _grid_applying 保护不会进来, 部分勾选的块才不会被冲成全选/全不选。
            if not getattr(self, "_grid_applying", False):
                self._absorb_block_selection(selected, deselected)
            # v220: 勾选集合 = 看得见的勾着 + 收起组里原本就勾着的(它们没有格子)
            # v231: 可见部分 = 单照片格的 Qt 选中 + 组块按镜像集合投影
            # (块内可以只勾一部分, Qt 的条目级选中表达不了)
            vis = self._visible_checked_paths()
            hidden = self._grid_member_paths() - self._visible_row_paths()
            self._grid_sel_paths = vis | (self._grid_sel_paths & hidden)
        if self.merge_mode:
            self.merge_panel.set_selection(photos)
        elif getattr(self, "norm_mode", False):
            # v207: 统一页按勾选决定"开始统一"处理哪些视频
            self._sync_norm_checked()
            # v209: 右栏预览/参数对比跟随勾选(含收起的组 —— 它们照样会被统一)
            specs = {it.path: it.spec for it in (self.norm_workspace.plan.items or [])
                     if getattr(it, "spec", None)}
            specs.update(self.merge_specs)
            checked = [p for p in self.model.photos
                       if getattr(p, "path", "") in self._grid_sel_paths]
            self.norm_workspace.panel.set_selection(checked, specs)
        else:
            self.inspector.set_photos(photos)
        # 两种形态的组标题都有"全选本组"勾选框(v145): 选中变了就要跟着刷新
        self._sync_group_checks(photos)
        self._refresh_top_status()

    def _selected_or_all(self) -> list:
        photos = self.view.selected_photos()
        return photos if photos else self.model.all_photos()

    # ---------------------------------------------------------------- 排序
    def _hand_menu(self, parent=None) -> QMenu:
        """建一个弹出菜单: 菜单项悬浮时也是抓手(和顶栏那些按钮一个手感)。

        「合并 / 排序」两种形态下的右键菜单、以及「排序 ▾」弹出的那张菜单都走这里,
        免得光标在菜单里又变回箭头。
        """
        menu = QMenu(parent or self)
        menu.setCursor(Qt.PointingHandCursor)
        return menu

    def _show_sort_menu(self):
        menu = self._hand_menu()
        has_sel = bool(self.view.selected_photos())
        scope = "选中项" if has_sel else "全部"
        menu.addAction("自动间隔分配时间…", self.auto_interval)
        menu.addAction("反转顺序", self.reverse_order)
        menu.addSeparator()
        menu.addAction(f"按拍摄时间（新→旧）　（{scope}）", lambda: self.sort_by("time", True))
        menu.addAction(f"按拍摄时间（旧→新）　（{scope}）", lambda: self.sort_by("time", False))
        menu.addSeparator()
        menu.addAction(f"按文件名升序　（{scope}）", lambda: self.sort_by("name", False))
        menu.addAction(f"按文件名降序　（{scope}）", lambda: self.sort_by("name", True))
        menu.addSeparator()
        menu.addAction(f"按文件大小　（{scope}）", lambda: self.sort_by("size", True))
        menu.exec(self.sort_btn.mapToGlobal(self.sort_btn.rect().bottomLeft()))

    def _show_more_menu(self):
        menu = self._hand_menu()
        menu.addAction("保存来源清单…", self.save_sources)
        menu.addAction("打开来源清单…", self.load_sources)
        menu.addAction("恢复上次来源…", self.restore_session)
        menu.addAction("导出清单 (CSV)…", self.export_csv)
        menu.addSeparator()
        menu.addAction("备份与回滚…", self.open_rollback)
        menu.addAction("清除所有时间计划", self.clear_plan)
        menu.addSeparator()
        menu.addAction("在资源管理器中显示所选", self.reveal_selected)
        menu.addAction("清除缩略图缓存", self.clear_thumb_cache)
        menu.addSeparator()
        menu.addAction("关于", self.show_about)
        menu.exec(self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft()))

    def sort_by(self, key: str, reverse: bool):
        sel = self.view.selected_photos()
        if not sel:
            self.model.sort_photos(key, reverse)
        else:
            self._sort_subset(sel, key, reverse)
        self._after_order_change()

    def _sort_subset(self, photos: list, key: str, reverse: bool):
        ids = set(id(p) for p in photos)
        positions = [i for i, p in enumerate(self.model.photos) if id(p) in ids]
        if not positions:
            return
        if key == "name":
            photos.sort(key=lambda p: p.name.lower(), reverse=reverse)
        elif key == "time":
            photos.sort(key=lambda p: p.sort_dt(), reverse=reverse)
        elif key == "size":
            photos.sort(key=lambda p: p.size, reverse=reverse)
        for i, p in zip(positions, photos):
            self.model.photos[i] = p
        self.model.rebuild()

    def reverse_order(self):
        sel = self.view.selected_photos()
        if not sel:
            self.model.reverse_photos()
        else:
            ids = set(id(p) for p in sel)
            positions = [i for i, p in enumerate(self.model.photos) if id(p) in ids]
            reversed_photos = list(reversed([self.model.photos[i] for i in positions]))
            for i, p in zip(positions, reversed_photos):
                self.model.photos[i] = p
            self.model.rebuild()
        self._after_order_change()

    def _after_order_change(self):
        self.model.orderChanged.emit()
        self.view.request_visible_thumbs()

    def auto_interval(self):
        photos = self._selected_or_all()
        if not photos:
            return
        dlg = IntervalDialog(photos, self)
        if dlg.exec() != IntervalDialog.Accepted:
            return
        timeplan.apply_plan(photos, dlg.start_datetime, dlg.step_seconds,
                            dlg.direction, dlg.sync_mode)
        self._remember_plan(photos, dlg.start_datetime, dlg.step_seconds,
                            dlg.direction, dlg.sync_mode)
        self._after_order_change()
        self._update_pending()
        self.status_label.setText(
            f"已为 {len(photos)} 张分配时间，第 1 张 = "
            f"{dlg.start_datetime:%Y-%m-%d %H:%M:%S}")

    def _apply_first_time_to_all(self):
        photos = self.view.selected_photos()
        if len(photos) < 2:
            return
        self.inspector.apply_time_to_all(photos)
        self._after_order_change()
        self._update_pending()

    def clear_plan(self):
        photos = self._selected_or_all()
        if not photos:
            return
        n = sum(1 for p in photos if p.dirty)
        if n == 0:
            return
        ret = QMessageBox.question(self, "取消改动",
                                   f"将丢弃 {n} 个文件上尚未写入的时间与字段改动，继续吗？")
        if ret != QMessageBox.Yes:
            return
        for p in photos:
            p.clear_pending()
        self.model.rebuild()
        self.inspector.refresh()
        self._update_pending()

    # ---------------------------------------------------------------- 写入
    def _pending_photos(self) -> list:
        return [p for p in self.model.photos if p.dirty]

    def _refresh_top_status(self):
        """顶栏右侧: 共 N 张 / 已选中 X 张 / 待写入信息。"""
        if self.edit_mode:
            n_main = len(self.edit_timeline.track_clips(0))
            n_audio = len(self.edit_timeline.track_clips(1))
            total = self.edit_timeline.total_duration()
            from .timeline import fmt_time
            text = f"时间线 {fmt_time(total)}　·　主轨 {n_main} 段 / 音频轨 {n_audio} 段"
            self.top_status.setText(text)
            self.top_status.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
            return
        if self.merge_mode or getattr(self, "norm_mode", False):
            # ⚠️ 收起某一组只是"不显示", 视频还在列表里(和排序界面一个道理),
            # 所以这里数**全部**视频, 不能按当前行列数 —— 否则一收起数字就变小,
            # 看着像文件少了几段。
            total = len([p for p in self.model.photos
                         if getattr(p, "kind", "") == "video"])
            # v253: 顶部只保留「共 N 段视频」, 删掉「已选 N 段 /」(选了几段由右栏摘要显示)。
            text = f"共 {total} 段视频"
            if self.ffmpeg_ok is False:
                text += "　·　未找到 FFmpeg"
            self.top_status.setText(text)
            self.top_status.setStyleSheet(
                f"color: {T.DANGER if self.ffmpeg_ok is False else T.TEXT_SUB}; font-size: 12px;")
            return
        total = len(self.model.photos)
        sel = self.view.selectionCount()
        pending = self._pending_photos()
        if sel:
            text = f"已选中 {sel} 张 / 共 {total} 张"
        else:
            text = f"共 {total} 张"
        if pending:
            # 这里是"有改动的文件数", 不是字段数 —— 以前写"项"会让人以为少了几倍
            text += f"　·　待写入 {len(pending)} 个文件"
        else:
            text += "　·　没有待写入的改动"
        self.top_status.setText(text)
        if pending:
            self.top_status.setStyleSheet(f"color: {T.WARN}; font-size: 12px;")
        else:
            self.top_status.setStyleSheet("color: #5F6B7A; font-size: 12px;")
        # 「清空」按钮提示: 红字标出"移除 / 永久", 数字随已加载文件数变化
        n = len(self.model.photos)
        self.clear_list_btn.setToolTip(
            f"<font color='{T.DANGER}'>移除</font>已加载的 {n} 个文件，"
            f"不会在电脑中<font color='{T.DANGER}'>永久</font>删除。")

    def _update_pending(self):
        self._refresh_top_status()
        self.view.viewport().update()

    # ---------------------------------------------------------------- 显示方式 / 缩放
    def _on_view_mode_changed(self, *_):
        """下拉换档: 应用到网格 + 记住档位(下次启动沿用这次的选择)。"""
        self._apply_main_view_mode()
        merge_settings.save_view_mode(self.view_mode_combo.currentData())

    def _fit_view_combo(self):
        """显示方式下拉按"最长档名"定宽(v214)。

        这个下拉是三态(排序/合并/统一)共用的, 合并/统一顶栏本来就挤, 所以不再写死
        92px, 改成按最长档名(现在是"超大图标")现算: 左右各留 18px —— 文字在整框居中,
        右端那个"不占布局宽度"的小三角也压不到字上, 文字永远完整显示。
        """
        cb = getattr(self, "view_mode_combo", None)
        if cb is None:
            return
        fm = cb.fontMetrics()
        need = max((fm.horizontalAdvance(cb.itemText(i)) for i in range(cb.count())),
                   default=0)
        cb.setFixedWidth(need + 36)

    def _apply_main_view_mode(self):
        """把顶栏"显示方式"下拉的当前模式应用到主网格。"""
        self._fit_view_combo()
        if not hasattr(self, "view") or self.view is None:
            return
        mode = self.view_mode_combo.currentData()
        cfg = next((c for c in MAIN_VIEW_MODES if c[1] == mode), MAIN_VIEW_MODES[0])
        _name, _mode, gw, gh = cfg
        horiz = _mode in ("list", "details")
        # 图标档与"详细信息"显示副标题(拍摄时间); "列表"档只有一行文件名
        self.view.delegate.set_cell(gw, gh, horizontal=horiz, show_info=(_mode != "list"))
        self.view.setFlow(QListView.TopToBottom if horiz else QListView.LeftToRight)
        self.view.setWrapping(True)
        self.view.scheduleDelayedItemsLayout()
        self.view.viewport().update()
        self.view._thumb_timer.start()
        # 合并模式: 格子第二行的规格文字按档位长短不同, 换档后重算
        if getattr(self, "merge_mode", False):
            self._refresh_merge_lines()

    def _refresh_merge_lines(self):
        """按当前档位宽度重算每个视频的"规格 · 时长"行(不重建分组, 不动选中)。"""
        wide = self._wide_cells()
        for p in self.model.merge_videos():
            spec = self.merge_specs.get(p.path)
            if spec:
                p.merge_line = mgrouping.cell_line(spec, wide,
                                                   skip=mgrouping.header_skip_keys(spec))
            elif p.path in self.merge_errors:
                p.merge_line = f"读取失败：{self.merge_errors[p.path]}"
        self.view.viewport().update()

    def _zoom_main_step(self, zoom_in: bool):
        """Ctrl+滚轮：在四档显示方式之间步进（上滚=放大，最小到"小图标"为止）。"""
        modes = [m[1] for m in MAIN_VIEW_MODES]
        cur = self.view_mode_combo.currentData()
        i = modes.index(cur) if cur in modes else 0
        # 档位由小到大排列(小图标在最前) → 上滚放大 = 索引增大; 下滚缩小 = 索引减小
        i = max(0, min(len(modes) - 1, i + (1 if zoom_in else -1)))
        self.view_mode_combo.setCurrentIndex(i)

    def _on_grid_entered(self, index):
        """图标档: 悬浮在照片卡片上时, 把完整文件名设为 tooltip(被省略号截断时看全名)。"""
        from ..core.photo import PhotoModel
        row = index.data(PhotoModel.ROLE_ROW)
        if row is not None and getattr(row, "photo", None) is not None:
            self.view.setToolTip(row.photo.name)

    def do_write(self):
        # v223: 统一在后台转码时, ffmpeg 正在逐个读这些原片 —— 此时写时间戳/
        # 元数据, exiftool 和 ffmpeg 在同一个文件上撞车(写入报"被占用", 或者
        # 就算都成功, 副本里烤进去的还是写之前的旧元数据, 合并/导出的成品
        # 带错时间)。等统一完成或先取消, 再来写入。
        if getattr(self, "_normalize_worker", None) is not None:
            QMessageBox.information(
                self, "统一进行中",
                "统一格式还在后台转码，正在读取这些原文件，\n"
                "现在写入时间戳可能冲突。\n\n"
                "请等它完成，或先在统一进度窗口里取消，再来写入。")
            return
        photos = self._pending_photos()
        if not photos:
            QMessageBox.information(self, "没有改动",
                                    "先用拖拽排好顺序，再用「自动间隔」或右侧面板改动字段。")
            return
        self._pending_photos_cache = photos
        changes = writer.build_changes(photos)
        dlg = ChangePreviewDialog(changes, len(photos), self, rebuilder=self._rebuild_changes)
        if dlg.exec() != ChangePreviewDialog.Accepted:
            return
        opts = dlg.options()
        changes = writer.build_changes(photos, thorough=opts["thorough"],
                                       fill_xmp=opts["fill_xmp"], fill_qt=opts["fill_qt"])
        if not changes:
            QMessageBox.information(self, "没有改动", "没有需要写入的字段。")
            return
        ret = QMessageBox.question(
            self, "再次确认",
            f"即将写入 {len(photos)} 个文件、{len(changes)} 项改动。\n"
            f"写入前会自动保存回滚快照。确定继续吗？")
        if ret != QMessageBox.Yes:
            return
        self._run_write(changes, opts)

    def _rebuild_changes(self, dlg: ChangePreviewDialog):
        opts = dlg.options()
        changes = writer.build_changes(self._pending_photos_cache,
                                       thorough=opts["thorough"],
                                       fill_xmp=opts["fill_xmp"], fill_qt=opts["fill_qt"])
        dlg.refill(changes, len(self._pending_photos_cache))

    def _run_write(self, changes, opts: dict):
        self.progress_dialog = ProgressDialog("写入中", self)
        self.write_worker = writer.WriteWorker(changes, self.model.folder,
                                               opts.get("in_place", True), self)
        self.write_worker.progress.connect(self.progress_dialog.on_progress)
        self.write_worker.log.connect(self.progress_dialog.on_log)
        self.write_worker.finished_write.connect(self._on_write_finished)
        self.progress_dialog.canceled.connect(self.write_worker.cancel)
        self.write_worker.start()
        self.progress_dialog.exec()

    def _on_write_finished(self, result):
        if self.progress_dialog:
            self.progress_dialog.finish()
        # v256: 读回校验不一致的文件不能当成功处理 —— 必须保留计划、标错误,
        # 让用户能重试(或抽查)。之前一律 highlight DONE + 清空 planned/pending,
        # 结果实际值没写对也显示完成, 且原计划被抹掉无法重写。
        mismatch_paths = getattr(result, "mismatch_paths", set()) or set()
        for path, meta in result.meta_updates.items():
            photo = self.path_index.get(path)
            if photo is None:
                continue
            photo.meta = meta
            if path in mismatch_paths:
                photo.status = 3   # STATUS_ERROR
                continue           # 保留 planned/pending
            photo.planned.clear()
            photo.pending.clear()
            photo.status = STATUS_DONE
        failed_paths = set(p for p, _ in result.failed) | mismatch_paths
        for photo in self.model.photos:
            if photo.path in failed_paths:
                photo.status = 3
        # 时间已按新顺序写进文件, 分组重新交给日期来算
        self.model.clear_group_overrides()
        self.model.rebuild()
        self.inspector.refresh()
        self._update_pending()
        lines = [f"完成 {len(result.ok_files)} 个文件，共 {result.total_changes} 项改动。"]
        if result.failed:
            lines.append(f"失败 {len(result.failed)} 个文件。")
        if result.mismatches:
            lines.append(f"有 {len(result.mismatches)} 项读回校验不一致，"
                         f"已保留这些文件的计划并标为错误，请抽查后重试写入。")
        lines.append(f"回滚快照：{result.snapshot_id}")
        if self.progress_dialog:
            self.progress_dialog.on_log("　".join(lines))
        self.status_label.setText("　".join(lines))

    # ---------------------------------------------------------------- 回滚
    def open_rollback(self):
        # v256: 回滚结束会 reload_folder() 整表重扫, 未写入的时间/字段计划会
        # 无声丢失 —— 和"刷新"同款风险, 复用同一道确认守卫。
        pending = self._pending_photos()
        if pending:
            ret = QMessageBox.question(
                self, "回滚",
                f"回滚会重新读取所有来源的文件元数据，\n"
                f"将丢失 {len(pending)} 个文件上尚未写入的时间/字段计划。\n"
                f"继续吗？")
            if ret != QMessageBox.Yes:
                return
        dlg = RollbackDialog(self)
        dlg.rollbackRequested.connect(self._run_rollback)
        dlg.exec()

    def _run_rollback(self, snapshot_path: str):
        dlg = ProgressDialog("回滚中", self)
        worker = writer.RollbackWorker(snapshot_path, self)
        worker.progress.connect(dlg.on_progress)
        worker.log.connect(dlg.on_log)
        worker.finished_rollback.connect(lambda w: self._on_rollback_finished(w, dlg))
        dlg.canceled.connect(lambda: None)
        worker.start()
        dlg.exec()

    def _on_rollback_finished(self, worker, dlg: ProgressDialog):
        dlg.finish()
        dlg.on_log(f"已还原 {len(worker.ok)} 个文件，失败 {len(worker.failed)} 个。")
        for path in worker.ok:
            photo = self.path_index.get(path)
            if photo:
                photo.status = STATUS_NORMAL
        self.reload_folder()
        self._update_pending()

    # ---------------------------------------------------------------- 其它
    def _show_context_menu(self, pos, global_pos=None):
        if self.merge_mode or getattr(self, "norm_mode", False):
            self._show_merge_menu(pos, global_pos)
            return
        photos = self.view.selected_photos()
        menu = self._hand_menu()
        scope = f"选中 {len(photos)} 张" if photos else "全部"
        menu.addAction(f"自动间隔分配时间…　（{scope}）", self.auto_interval)
        menu.addAction("反转顺序", self.reverse_order)
        menu.addSeparator()
        menu.addAction("按拍摄时间（新→旧）", lambda: self.sort_by("time", True))
        menu.addAction("按拍摄时间（旧→新）", lambda: self.sort_by("time", False))
        menu.addSeparator()
        menu.addAction("按文件名升序", lambda: self.sort_by("name", False))
        menu.addAction("按文件名降序", lambda: self.sort_by("name", True))
        menu.addSeparator()
        menu.addAction("按文件大小", lambda: self.sort_by("size", True))
        menu.addSeparator()
        if len(photos) > 1:
            menu.addAction("把第一张的时间应用到全部选中", self._apply_first_time_to_all)
        menu.addAction("全选", self.view.select_all_photos)
        menu.addAction("打开文件所在位置", self.reveal_selected)
        menu.addSeparator()
        menu.addAction("清除这批改动", self.clear_plan)
        if photos:
            # 仅在有选中项时出现，避免误导（这几项都是对"选中项"生效）
            menu.addSeparator()
            menu.addAction("从列表中移除（不会删除文件）", self._remove_selected_from_list)
            danger = DangerMenuAction(menu, "永久删除文件…")
            danger.triggered.connect(self._delete_selected)
            menu.addAction(danger)
            menu.addSeparator()
            menu.addAction("属性…", self.show_properties)      # 放最下方
        menu.exec(global_pos if global_pos is not None
                  else self.view.viewport().mapToGlobal(pos))

    def open_compare(self):
        """主界面右键「对比」: 逐参数对比选中文件「能否无损合并」。

        非模态(与「属性」一致): 可以同时开多个对比窗口。
        ⚠️ 主界面仍要求 ≥2 个文件(v192 的「单选先开窗口 + 发送到」只在剪辑态三处)。
        """
        photos = self.view.selected_photos()
        if len(photos) < 2:
            QMessageBox.information(self, "对比", "请至少选中 2 个文件。")
            return
        try:
            self._new_compare_dialog(photos, self)
        except Exception:
            self._report_ui_error("打开对比", "photosort_compare.log")

    # ------------------------------------------------------- 对比窗口(编号/追加)
    def _new_compare_dialog(self, photos, parent, ref=None):
        """v192: 建一个带固定编号的非模态对比窗口(主界面 / 剪辑态共用)。

        编号 = 打开顺序, 写在窗口标题里(「对比1」), 「发送到对比N」按这个号找窗口。
        关掉其中一两个不重排号; 全部关掉后编号归零, 下次从 1 重新开始。
        """
        from .dialogs import CompareDialog
        self._cmp_seq += 1
        # v203: parent 必须留 None —— QDialog 带 parent 在 Windows 是 owned window,
        # Z-order 永远压在 owner(合并窗口)之上, 想把合并窗口点到前面都做不到。
        # 改成独立顶层窗口后正常参与层叠; 居中仍用传进来的 parent/ref 当锚点。
        dlg = CompareDialog(photos, self.merge_specs, None, index=self._cmp_seq)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.destroyed.connect(lambda *_: self._forget_cmp_dialog(dlg))
        # v193: 对比窗口里改的是显示名(Photo.name), 主列表与其它对比窗口同步刷新
        dlg.renamed.connect(self._on_compare_renamed)
        # v198: 对比窗口右键「N个移动到…」→ 主窗口搬文件并刷新所有窗口
        dlg.moveRequested.connect(self._move_from_compare)
        self._cmp_dialogs.append(dlg)
        dlg.show()
        # 多个对比窗口依次错开(往左下偏移, 免得和属性窗口叠在同一个位置)
        n = len(self._cmp_dialogs) - 1
        geo = dlg.frameGeometry()
        geo.moveCenter((ref or parent).frameGeometry().center())
        geo.translate(-((n % 6) * 28), (n % 6) * 28)
        dlg.move(geo.topLeft())
        dlg.raise_()
        dlg.activateWindow()
        return dlg

    def _on_compare_renamed(self, paths):
        """v193: 对比窗口右键改了显示名 → 原地刷新主列表文字，磁盘文件不动。

        用 dataChanged 而不是 model.rebuild(): 名字可能被拿去兜底解析拍摄时间，
        整表重建会把分组顺序重排，用户只是想换个显示名，不该动顺序。
        """
        wanted = {self._canon(str(p or "")) for p in (paths or []) if p}
        if not wanted:
            return
        try:
            for row in list(self.model.rows):
                if isinstance(row, PhotoRow) and \
                        self._canon(row.photo.path) in wanted:
                    self.model.refresh_photo(row.photo)
            src = self.sender()
            for other in list(self._cmp_dialogs):
                if other is src:
                    continue
                try:
                    other.refresh_names()
                except RuntimeError:
                    pass
        except Exception:
            pass

    def _forget_cmp_dialog(self, dlg):
        try:
            self._cmp_dialogs.remove(dlg)
        except ValueError:
            pass
        if not self._cmp_dialogs:
            self._cmp_seq = 0        # 全关了才重新计算编号

    def _photos_for_compare(self, paths):
        """剪辑态的路径 → Photo 列表(去重 + 跳过没有对应 Photo 的项)。"""
        by_path = getattr(self.clip_editor, "_photos_by_path", {}) or {}
        seen, photos = set(), []
        for p in paths or []:
            k = self._canon(str(p or ""))
            if not k or k in seen:
                continue
            seen.add(k)
            photo = by_path.get(k) or by_path.get(str(p))
            if photo is not None:
                photos.append(photo)
        return photos

    def _compare_label(self, paths) -> str:
        """v192: 三处剪辑右键统一的「对比」文案。

        选 1 个文件 = 「对比」; 选 N 个 = 「对比（N个文件）」。N 按实际参与
        对比的去重文件数算(素材框以前写「和选中的 N 个对比」没算右键那个自己)。
        """
        n = len({self._canon(str(p)) for p in (paths or []) if str(p or "")})
        return "对比" if n <= 1 else f"对比（{n}个文件）"

    def _add_compare_send_actions(self, menu, paths):
        """v192: 已经有开着的对比窗口时, 在横线上方的操作组里平铺
        「发送到对比1 / 对比2 …」(按编号升序)。最后一组仍只有 对比 + 属性。
        """
        if not paths:
            return
        wins = [d for d in self._cmp_dialogs
                if getattr(d, "cmp_index", None)]
        if not wins:
            return
        for dlg in sorted(wins, key=lambda d: d.cmp_index):
            idx, ps = dlg.cmp_index, list(paths)
            act = menu.addAction(f"发送到对比{idx}",
                                 lambda _=None, i=idx, p=ps: self._send_to_compare(i, p))
            if act is not None:      # 离屏验证里可能换成假菜单(不返回 action)
                act.setToolTip(f"把选中的 {len(ps)} 个文件追加到对比{idx}")

    def _send_to_compare(self, index: int, paths):
        """把当前选中的文件追加进已经开着的第 N 个对比窗口(不重开窗口)。"""
        dlg = next((d for d in self._cmp_dialogs
                    if getattr(d, "cmp_index", None) == index), None)
        if dlg is None:
            self.status_label.setText(f"对比{index} 已经关闭了")
            return
        photos = self._photos_for_compare(paths)
        if not photos:
            self.status_label.setText("这些文件没有可对比的参数")
            return
        try:
            n = dlg.add_photos(photos, self.merge_specs)
        except RuntimeError:
            return                    # 窗口刚好被关掉了
        try:
            dlg.raise_()
            dlg.activateWindow()
        except RuntimeError:
            pass
        if n:
            self.status_label.setText(
                f"已加入对比{index}（新增 {n} 个，共 {len(dlg.photo_paths())} 个文件）")
        else:
            self.status_label.setText(f"这些文件已经在对比{index} 里了")

    def _open_compare_for_paths(self, paths) -> bool:
        """剪辑态「对比」(v176 / v192): 新建一个对比窗口。

        v192: 单选也能开(先列这一个文件的参数), 之后用「发送到对比N」把别的文件
        追加进来。规格用加入素材时就后台探好的 merge_specs —— 不在这里起探测线程,
        否则窗口会先空几秒; 规格没到的行在 compare_model 里自然显示"未知"。
        """
        photos = self._photos_for_compare(paths)
        if not photos:
            QMessageBox.information(self._dlg_parent(), "对比",
                                    "没有可对比的文件。")
            return False
        try:
            parent = self._dlg_parent()
            ref = self._clip_window if self.edit_mode and \
                self._clip_window is not None and self._clip_window.isVisible() \
                else self
            self._new_compare_dialog(photos, parent, ref=ref)
            return True
        except Exception:
            self._report_ui_error("打开对比", "photosort_compare.log")
            return False

    def show_properties(self):
        """右键「属性」: 比 Windows 属性更全的文件/拍摄/视频信息 + 本软件内状态。

        非模态: 可以同时开好几个文件的属性, 也不用关掉它才能操作主界面。
        """
        photos = self.view.selected_photos()
        if not photos:
            return
        # 排序位置按"看得见的顺序"算: 合并态是规格分组顺序, 排序态是时间顺序
        order, total = {}, 0
        for row in self.model.rows:
            if isinstance(row, PhotoRow):
                total += 1
                order[row.photo.path] = total
        try:
            from .props_dialog import PropertiesDialog
            ffprobe = (self.ffmpeg_pair[1] if self.ffmpeg_pair else
                       self.merge_ffprobe)
            ctx = {
                "order": order,
                "total": total,
                "merge_groups": self.model.merge_groups if self.merge_mode else None,
                "ffprobe": ffprobe,
            }
            dlg = PropertiesDialog(photos, ctx, self)
            dlg.setAttribute(Qt.WA_DeleteOnClose, True)
            dlg.destroyed.connect(lambda *_: self._forget_prop_dialog(dlg))
            self._prop_dialogs.append(dlg)
            dlg.show()
            # 多个属性窗口时依次错开, 免得太阳重叠看不出开了几个
            n = len(self._prop_dialogs) - 1
            geo = dlg.frameGeometry()
            geo.moveCenter(self.frameGeometry().center())
            geo.translate((n % 6) * 26, (n % 6) * 26)
            dlg.move(geo.topLeft())
            dlg.raise_()
            dlg.activateWindow()
        except Exception as e:
            import traceback
            import tempfile
            try:
                log = os.path.join(tempfile.gettempdir(), "photosort_props.log")
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write(traceback.format_exc() + "\n")
            except Exception:
                pass
            QMessageBox.critical(self, "属性", f"打开属性窗口失败：{e}")

    def _forget_prop_dialog(self, dlg):
        try:
            self._prop_dialogs.remove(dlg)
        except ValueError:
            pass

    def _on_view_double_clicked(self, index):
        """主列表双击: 用系统默认应用打开该文件(图片用看图, 视频用播放器等)。"""
        if not index.isValid():
            return
        row = self.model.data(index, PhotoModel.ROLE_KIND)
        if row != "photo":
            return
        photo = self.model.data(index, PhotoModel.ROLE_PHOTO)
        self._open_photo_path(photo)

    def _on_view_open_photo(self, photo):
        """v231: 统一页多文件组块里双击某张子卡 → 打开那张对应的文件。"""
        self._open_photo_path(photo)

    def _open_photo_path(self, photo):
        path = getattr(photo, "path", None)
        if not path or not os.path.exists(path):
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as ex:
            QMessageBox.warning(self, "打开失败", f"无法用默认应用打开:\n{path}\n\n{ex}")

    # ---------------------------------------------------------------- 移除 / 删除
    def _remove_from_list(self, photos: list):
        """仅从内存列表中移除这些照片对象（磁盘文件不动）。"""
        if not photos:
            return
        kill = {id(p) for p in photos}
        self.model.photos = [p for p in self.model.photos if id(p) not in kill]
        self.model.rebuild()
        self.path_index = {p.path: p for p in self.model.photos}
        self.inspector.set_photos([])
        self.stack.setCurrentWidget(self.view if self.model.photos else self.empty_page)
        self._update_title()
        self._update_pending()

    def _remove_selected_from_list(self):
        """右键「从列表移除」：只从列表拿掉，不碰磁盘文件，二次确认。"""
        photos = self.view.selected_photos()
        if not photos:
            return
        pending = [p for p in photos if p.dirty]
        extra = (f"\n注意：其中 {len(pending)} 个文件有尚未写入的时间改动，移除后会丢失。"
                 if pending else "")
        text = (f"将从列表移除 {len(photos)} 项。\n"
                f"这不会删除磁盘上的文件，也不会撤销已写入的改动。{extra}\n\n继续吗？")
        if not confirm_destructive(self, "从列表移除", text):
            return
        n = len(photos)
        self._remove_from_list(photos)
        self.status_label.setText(f"已从列表移除 {n} 项（磁盘文件未改动）")

    def _delete_selected(self):
        """右键「永久删除」/ DEL 键：把选中项移入回收站（可在回收站恢复），二次确认。"""
        photos = self.view.selected_photos()
        if not photos:
            QMessageBox.information(self, "没有选中项", "请先在列表里选中要删除的文件。")
            return
        names = [os.path.basename(p.path) for p in photos]
        paths = [p.path for p in photos]
        prompt = (f"从电脑上 <font color='{T.DANGER}'>永久删除</font>文件？"
                  f"(回收站可恢复)")
        if not confirm_delete_files(self, prompt, names, paths):
            return
        removed_paths = set(send_to_recycle_bin(paths))
        removed_photos = [p for p in photos if p.path in removed_paths]
        failed = [p for p in photos if p.path not in removed_paths]
        if removed_photos:
            self._remove_from_list(removed_photos)
            self.inspector.refresh()
            self._update_pending()
        if failed:
            QMessageBox.warning(self, "部分文件未能移入回收站",
                                "以下文件未能移入回收站（可能正被其他程序占用，"
                                "或体积超过回收站上限）：\n\n"
                                + "\n".join(os.path.basename(p.path) for p in failed[:10]))
            self.status_label.setText(f"已移入回收站 {len(removed_photos)} 个，{len(failed)} 个失败")
        else:
            self.status_label.setText(f"已移入回收站 {len(removed_photos)} 个文件（可在回收站恢复）")

    def reveal_selected(self):
        photos = self.view.selected_photos() or self.model.photos[:1]
        if not photos:
            return
        self._reveal_path(os.path.normpath(photos[0].path))

    def _reveal_path(self, path: str):
        """在系统文件管理器里定位到该文件(Windows 为选中它)。"""
        path = os.path.normpath(path)
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception:
            pass

    def clear_thumb_cache(self):
        from ..core.thumbs import cache_dir
        folder = cache_dir()
        count = 0
        try:
            for name in os.listdir(folder):
                os.remove(os.path.join(folder, name))
                count += 1
        except OSError:
            pass
        QMessageBox.information(self, "缓存已清理", f"已删除 {count} 个缩略图缓存文件。")

    def show_about(self):
        from ..core.exiftool import get_exiftool
        try:
            ver = get_exiftool().version()
        except Exception:
            ver = "未找到"
        dlg = QDialog(self)
        dlg.setWindowTitle(f"关于 照片排序台 v{__build__}")
        dlg.setFixedWidth(380)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(22, 22, 22, 22)
        lay.setSpacing(12)

        # 顶部 logo（与 exe 图标一致）
        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)
        pm = QPixmap(app_resource("logo.ico")).scaled(
            72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        logo.setPixmap(pm)
        lay.addWidget(logo)

        title = QLabel("照片排序台")
        title.setObjectName("aboutTitle")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)

        info = QLabel(
            "拖拽排顺序 → 分配时间戳 → 一次写回 修改时间 / 创建时间 / "
            "拍摄时间（含 EXIF、XMP、QuickTime 与文件系统时间）。\n\n"
            f"软件版本：{__version__}（构建 v{__build__}）\n"
            f"ExifTool 版本：{ver}\n"
            "每次写入前都会保存回滚快照，可在「⋯ → 备份与回滚」里还原。"
        )
        info.setWordWrap(True)
        info.setProperty("muted", True)
        lay.addWidget(info)

        sc = QLabel(
            "快捷键：\n"
            "  Ctrl+O 打开文件夹　Ctrl+Shift+O 打开文件\n"
            "  Ctrl+I 自动间隔　Ctrl+Enter 保存改动\n"
            "  Ctrl+R / F5 刷新　Ctrl+Shift+R 取消改动\n"
            "  Ctrl+A 全选　右键 排序 / 间隔 / 全选　Backspace 清空（有选中则只清选中项）"
        )
        sc.setWordWrap(True)
        sc.setProperty("muted", True)
        lay.addWidget(sc)

        lay.addStretch(1)
        btn = QPushButton("确定")
        btn.setDefault(True)
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, 0, Qt.AlignCenter)
        dlg.exec()

    # ---------------------------------------------------------------- 缩略图
    def _on_need_thumbs(self, paths: list):
        if paths:
            self.thumb_worker.request(paths)

    def _on_thumb_ready(self, path: str, pix):
        # 后台线程只能给 QImage(QPixmap 只能在界面线程用), 到这儿才变成 QPixmap
        if isinstance(pix, QImage):
            pix = QPixmap.fromImage(pix)
        # 素材框里等这张图的格子(剪辑态现加进来的素材不在主列表里, 上面那段会 return)
        if self.edit_mode and pix is not None and not pix.isNull():
            self.clip_editor.media.set_thumb(path, pix)
        photo = self.path_index.get(path)
        if photo is None:
            return
        photo.thumb = pix
        photo.thumb_state = THUMB_OK
        if photo in self.inspector.photos:
            self.inspector.set_photos(self.inspector.photos)
        self.model.refresh_photo(photo)

    def _on_thumb_failed(self, path: str):
        photo = self.path_index.get(path)
        if photo is not None:
            photo.thumb_state = THUMB_FAIL

    # ---------------------------------------------------------------- 启动检查
    def _check_exiftool(self):
        exe = find_exiftool()
        if not exe:
            QMessageBox.warning(
                self, "缺少 ExifTool",
                "没有找到 exiftool。请把 exiftool.exe 放到程序目录下的 tools 文件夹里，\n"
                "或者安装后加入系统 PATH，再重新启动。")
            self.status_label.setText("未找到 exiftool，无法读写元数据")
            return
        self.status_label.setText(f"ExifTool 已就绪：{exe}")

    # ---------------------------------------------------------------- 关闭
    def keyPressEvent(self, event):
        # Backspace = 清空列表(仅当焦点不在可输入控件上时, 避免误触输入框)
        if event.key() == Qt.Key_Backspace:
            fw = self.focusWidget()
            if isinstance(fw, (QLineEdit, QTextEdit, QComboBox, QPlainTextEdit)):
                super().keyPressEvent(event)
                return
            # 有选中项时, 只清空框选的这几项(从列表移除, 不动磁盘);
            # 没选中任何项时, 才是"清空整个列表"。作用范围与 DEL 一致:
            # DEL 也只在有选中项时才对那几项动手。
            chosen = self.view.selected_photos()
            if chosen:
                self._remove_selected_from_list()
            else:
                self.clear_list()
            event.accept()
            return
        if event.key() == Qt.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            return
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
            return
        if event.key() == Qt.Key_Escape:
            # 排序 / 合并模式通用: ESC = 取消全部选中(输入框里按 ESC 仍归输入框处理)
            fw = self.focusWidget()
            if isinstance(fw, (QLineEdit, QTextEdit, QComboBox, QPlainTextEdit)):
                super().keyPressEvent(event)
                return
            sm = self.view.selectionModel()
            if sm is not None and sm.hasSelection():
                sm.clearSelection()
                self.view.viewport().update()
                event.accept()
                return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        # v203: 对比窗口改成独立顶层窗口(无 parent), Qt 不会替主窗口自动关 ——
        # 这里逐个真关, 否则主窗口退了它们还挂着, 进程退不干净。
        for d in list(getattr(self, "_cmp_dialogs", [])):
            try:
                d.close()
            except RuntimeError:
                pass
        nd = getattr(self, "_norm_dialog", None)
        if nd is not None:
            try:
                # v222: 统一进度框是非模态的, 关窗前它的守卫会弹"确定取消吗" ——
                # 但关主窗 = 退出程序, worker 本来就会被 stop+wait, 别再问一遍。
                nd._allow_close = True
                nd.close()
            except RuntimeError:
                pass
        # 剪辑是独立窗口(v128): 主窗口一关就得把它们全关掉, 否则进程退不干净
        # v199: 现在有多个会话窗口, 逐个真关
        for sess in list(self._clip_sessions) + [self._boot_session]:
            w = getattr(sess, "window", None)
            if w is not None:
                try:
                    w.shutdown()
                except Exception:
                    pass
                try:
                    w.deleteLater()
                except Exception:
                    pass
                sess.window = None
        # 退出前把所有线程都停下: 进程带着活线程往下走, 轻则丢帧重则闪退
        self._thread_gc.stop()
        self._purge_threads()
        for attr in ("thumb_worker", "_probe_worker", "_merge_worker",
                     "_film_worker", "_grab_worker", "_player", "_audio_worker",
                     "_sound", "_tool_worker", "_wave_worker", "_clip_probe_worker",
                     "_export_worker", "_ff_check_worker", "_normalize_worker",
                     "worker"):
            w = getattr(self, attr, None)
            if w is None:
                continue
            # 统一/合并这类 worker 有自己的"停止"语义(要杀 ffmpeg 子进程), 先叫停
            for meth in ("cancel", "stop"):
                fn = getattr(w, meth, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
            self._stop_thread(w, 2000)
            setattr(self, attr, None)
        self._purge_threads()
        super().closeEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if path and (os.path.isdir(path) or os.path.isfile(path)):
                    event.acceptProposedAction()
                    return

    def dropEvent(self, event):
        from ..core.photo import kind_of
        urls = event.mimeData().urls()
        folder_paths = []
        file_paths = []
        for url in urls:
            path = url.toLocalFile()
            if not path:
                continue
            if os.path.isdir(path):
                folder_paths.append(path)
            elif os.path.isfile(path) and kind_of(path) != "other":
                file_paths.append(path)
        if folder_paths:
            # 支持一次拖入多个文件夹：合并成一次扫描，避免逐个 open_folder 被防重入保护丢掉的扫描
            self.open_folders(folder_paths, accumulate=True)
            return
        if file_paths:
            existing_paths = {os.path.normcase(os.path.abspath(p.path))
                              for p in self.model.photos}
            new_paths = [p for p in file_paths
                         if os.path.normcase(os.path.abspath(p)) not in existing_paths]
            if not new_paths:
                self.status_label.setText("拖入的文件都已在该列表中，未新增")
                return
            self.sources.append({"type": "files", "paths": new_paths})
            self.model.set_folder(self._primary_dir(), False)
            self._update_title()
            self._save_session()
            self._scan_files(new_paths, accumulate=True)


# v199: 把剪辑态字段挂成"按当前激活会话路由"的 property。
# 这样现有 300+ 处 self.clip_editor / self.edit_timeline 引用零改动,
# 切换会话只要换 _active_session 指针, 整套现场自动跟着切。
for _f in SESSION_FIELDS:
    if not hasattr(MainWindow, _f):
        setattr(MainWindow, _f, session_property(_f))
