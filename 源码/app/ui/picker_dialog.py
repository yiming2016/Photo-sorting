"""自定义"添加"选择框。

替代系统 QFileDialog：
- 顶部：←/→ 箭头按钮（仿 Windows 文件管理器 后退/前进）+ 路径面包屑（每个路径段都是可点击按钮，点哪段跳哪段；点面包屑空白处进入编辑，整行选中可重输完整路径）
- 左侧栏：带图标的导航树，直接列出 C盘、D盘、桌面、文档、下载、图片、音乐、视频、3D打印；
  点开任一项就地展开，显示它里面的子文件夹和文件（子文件夹可继续手动逐级展开）
- 中间文件/文件夹列表（可多选）
- 底部「文件类型」筛选 + 「添加」「取消」按钮
- 全部中文
"""

import os
import sys

from PySide6.QtCore import (QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt, QDir, QFileInfo,
                            QModelIndex, QStandardPaths, QTimer, QUrl)
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QIcon, QLinearGradient,
                           QMouseEvent, QPainter, QPainterPath, QPen, QPixmap, QPolygon)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileIconProvider, QFileSystemModel, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSizePolicy, QStackedWidget, QStyle,
    QStyledItemDelegate, QTreeView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from . import theme as T
from .dialogs import _CenterAlignDelegate, _CenterCombo
from .widgets import CheckCheckBox, Marquee

DRIVE_NAMES = ["C:", "D:"]

# 显示方式: (名称, 模式, 图标边长, 网格宽, 网格高, 是否显示"类型/大小"副标题)
VIEW_MODES = [
    ("超大图标", "xl", 128, 168, 186, True),
    ("大图标", "lg", 96, 132, 150, True),
    ("中等图标", "md", 48, 104, 116, False),
    ("小图标", "sm", 32, 88, 96, False),
    ("详细信息", "details", 16, 0, 0, False),
]
DEFAULT_VIEW_MODE = "lg"          # 默认"大图标"
PREVIEW_W = 232                   # 右侧"预览"栏宽度(顶栏右侧要为它留白, 见 _build_ui)
MID_SPACING = 10                  # 中部各栏之间的间距

# 选中样式：选中/失焦两种情况都显式指定，保证任何情况下都能看到高亮。
_SELECTED_QSS = (
    "QTreeView::item:selected {"
    "  background: " + T.ACCENT + ";"
    "  color: #FFFFFF;"
    "}"
    "QTreeView::item:selected:!active {"
    "  background: " + T.ACCENT + ";"
    "  color: #FFFFFF;"
    "}"
)

_ROLE_PATH = Qt.UserRole          # 0x0100
_ROLE_KIND = Qt.UserRole + 1      # disk / folder / file / placeholder
_ROLE_LOADED = Qt.UserRole + 2    # 是否已加载过子项
_ROLE_EXPANDED = Qt.UserRole + 3  # 上一次已知的展开状态（区分"点小三角"与"点文字"）


def _special_folders():
    """返回 [(显示名, 绝对路径), ...]，跳过不存在的。"""
    mapping = [
        ("桌面", QStandardPaths.StandardLocation.DesktopLocation),
        ("文档", QStandardPaths.StandardLocation.DocumentsLocation),
        ("下载", QStandardPaths.StandardLocation.DownloadLocation),
        ("图片", QStandardPaths.StandardLocation.PicturesLocation),
        ("音乐", QStandardPaths.StandardLocation.MusicLocation),
        ("视频", QStandardPaths.StandardLocation.MoviesLocation),
    ]
    items = []
    for name, loc in mapping:
        paths = QStandardPaths.standardLocations(loc)
        if paths:
            p = os.path.normpath(paths[0])
            if os.path.exists(p):
                items.append((name, p))
    # 3D打印（系统名"3D 对象"）
    prof = QStandardPaths.standardLocations(QStandardPaths.StandardLocation.HomeLocation)
    if prof:
        for cand in (os.path.join(prof[0], "3D Objects"), os.path.join(prof[0], "3D 对象")):
            if os.path.exists(cand):
                items.append(("3D打印", os.path.normpath(cand)))
                break
    return items


# ---------------------------------------------------------------- 显示方式下拉
class _CenterComboArrow(_CenterCombo):
    """关闭态文字在整框几何中心、且右侧带一个"不占布局宽度"的下拉小三角。

    _CenterCombo 为让文字真居中，把 ::drop-down 的宽度设为 0（箭头区会让文字
    相对整框偏左约 7px）。代价是关闭态看不到下拉箭头，用户不易察觉可点。
    这里在 paintEvent 末尾把一个小三角重叠画在右边缘——视觉上仍是下拉箭头，
    但不参与布局，文字依旧严格居中。
    """

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        cx = self.width() - 12
        cy = self.height() / 2
        hw, hh = 4.0, 2.4          # 三角半宽 / 半高
        tri = QPolygon([
            QPoint(int(cx - hw), int(cy - hh)),
            QPoint(int(cx + hw), int(cy - hh)),
            QPoint(int(cx), int(cy + hh)),
        ])
        painter.setPen(Qt.NoPen)
        painter.setBrush(self.palette().color(self.foregroundRole()))
        painter.drawPolygon(tri)
        painter.end()


# ---------------------------------------------------------------- 拉框
class _MarqueeMixin:
    """给视图补一个"看得见的"拉框。

    选择逻辑仍然完全交给 Qt(它本来就支持框选), 这里只在上面叠一层自绘矩形。
    为什么不直接用 Qt 自带的: 全局 QSS 里有 `QWidget { background: transparent; }`,
    系统风格的橡皮筋会跟着被画成透明, 于是"能选中但看不见框"。
    """

    def _mq_init(self):
        self._mq = None
        self._mq_origin = None
        self._on_zoom = None      # Ctrl+滚轮回调: 参数 True=放大 / False=缩小

    def _mq_wheel(self, event) -> bool:
        """Ctrl+滚轮 → 交给回调切换图标档位；返回 True 表示已消费。"""
        if (event.modifiers() & Qt.ControlModifier) and self._on_zoom is not None:
            self._on_zoom(event.angleDelta().y() > 0)
            event.accept()
            return True
        return False

    def _mq_press(self, event):
        if event.button() == Qt.LeftButton:
            self._mq_origin = event.position().toPoint()

    def _mq_move(self, event):
        if not (event.buttons() & Qt.LeftButton) or self._mq_origin is None:
            return
        pos = event.position().toPoint()
        if self._mq is None:
            # 移动超过一点点才算"拉框", 否则只是普通点击
            if (pos - self._mq_origin).manhattanLength() < 5:
                return
            self._mq = Marquee(self.viewport())
            self._mq.show()
        self._mq.setGeometry(QRect(self._mq_origin, pos).normalized())
        self._mq.raise_()

    def _mq_release(self):
        if self._mq is not None:
            self._mq.hide()
            self._mq.deleteLater()
            self._mq = None
        self._mq_origin = None


class _FileListView(_MarqueeMixin, QListView):
    """大/中/小图标 与 列表 四种方式共用的视图。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mq_init()
        self.setSelectionMode(QListView.ExtendedSelection)
        self.setMovement(QListView.Static)
        self.setResizeMode(QListView.Adjust)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setSpacing(8)
        self.setEditTriggers(QListView.NoEditTriggers)
        self.setTextElideMode(Qt.ElideMiddle)
        self.setMouseTracking(True)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._mq_press(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        self._mq_move(event)

    def mouseReleaseEvent(self, event):
        self._mq_release()
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._mq_wheel(event):
            return
        super().wheelEvent(event)


class _FileTreeView(_MarqueeMixin, QTreeView):
    """详细信息方式: 名称/大小/类型 表格。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mq_init()
        self.setSelectionMode(QTreeView.ExtendedSelection)
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setSortingEnabled(True)
        self.setEditTriggers(QTreeView.NoEditTriggers)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._mq_press(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        self._mq_move(event)

    def mouseReleaseEvent(self, event):
        self._mq_release()
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._mq_wheel(event):
            return
        super().wheelEvent(event)


# ---------------------------------------------------------------- 图标自绘
class _ThumbDelegate(QStyledItemDelegate):
    """图标/列表方式下的自绘: 图片显示真实缩略图, 文件夹等用系统图标。

    之所以自己画而不是给 QFileSystemModel 换图标提供者: 模型是在后台线程里取图标的,
    而 QPixmap 只能在界面线程创建, 在委托里画是唯一安全又简单的做法。
    """

    IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.thumb = 96
        self.grid = QSize(132, 150)
        self.show_size = True
        self.horizontal = False      # "列表"方式: 图标在左、文字在右
        self._cache: dict[str, object] = {}
        self._miss: set = set()

    def configure(self, thumb: int, grid: QSize, show_size: bool):
        if thumb != self.thumb:
            self._cache.clear()       # 尺寸变了, 旧的缩略图作废
            self._miss.clear()
        self.thumb = thumb
        self.grid = QSize(grid)
        self.show_size = show_size

    def sizeHint(self, option, index) -> QSize:
        return QSize(self.grid)

    # --- 缩略图(带缓存) ---
    def _pixmap(self, path: str):
        if path in self._miss:
            return None
        pm = self._cache.get(path)
        if pm is not None:
            return pm
        ext = os.path.splitext(path)[1].lower()
        # 走两条路:
        #   1) jpg/png 等常用图且缩略图够大: QImageReader 直读, 最快;
        #   2) HEIC/RAW/视频/小图标: 复用主程序的 thumbs 管线(磁盘缓存 + Pillow+heif + ffmpeg),
        #      慢一点但能出图。
        if ext in self.IMG_EXT and self.thumb >= 48:
            pm = self._load(path)
        else:
            pm = self._load_heavy(path)
        if pm is None or pm.isNull():
            self._miss.add(path)
            return None
        if len(self._cache) > 600:
            self._cache.clear()
        self._cache[path] = pm
        return pm

    def _load(self, path: str, thumb: int | None = None):
        """读取常用格式图片(jpg/png/...)，长边缩放到 thumb*2。thumb 省略时用当前档位尺寸。

        预览栏会传入更大的 thumb 以得到清晰的大图。
        """
        from PySide6.QtGui import QImageReader
        try:
            reader = QImageReader(path)
            reader.setAutoTransform(True)      # 按 EXIF 摆正方向
            orig = reader.size()
            if not orig.isValid() or orig.width() <= 0 or orig.height() <= 0:
                return None
            n = (thumb or self.thumb) * 2      # 按 2 倍读, 高分屏下不虚
            scale = min(n / orig.width(), n / orig.height())
            if scale > 1:
                scale = 1.0
            tw = max(1, int(orig.width() * scale))
            th = max(1, int(orig.height() * scale))
            reader.setScaledSize(QSize(tw, th))
            img = reader.read()
        except Exception:
            return None
        if img.isNull():
            return None
        return QPixmap.fromImage(img)

    def _load_heavy(self, path: str):
        """HEIC/RAW/视频 的重负载路径。

        复用主程序 ``app.core.thumbs`` 的磁盘缓存与解码能力:
          - 命中缓存(<1ms): 直接读
          - 视频: ffmpeg -ss 抽 1 帧
          - 图片(含 HEIC): Pillow + pillow_heif
        缓存里统一存 360px 的 jpeg, 加载后由 paint 时再按 self.thumb 缩放。
        """
        try:
            from ..core.thumbs import cache_dir, cache_key, _decode_with_ffmpeg, _decode_with_pillow
        except Exception:
            return None
        try:
            key = cache_key(path)
            dest = os.path.join(cache_dir(), key + ".jpg")
            if not (os.path.exists(dest) and os.path.getsize(dest) > 0):
                ext = os.path.splitext(path)[1].lower().lstrip(".")
                if ext in {
                    "mp4", "mov", "m4v", "avi", "mkv", "webm", "wmv", "flv",
                    "mpg", "mpeg", "3gp", "3g2", "mts", "m2ts", "ts",
                }:
                    _decode_with_ffmpeg(path, dest)
                else:
                    # HEIC/RAW 都靠 Pillow(pillow_heif 已注册过 opener)
                    _decode_with_pillow(path, dest)
        except Exception:
            return None
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return QPixmap(dest)
        return None

    # --- 绘制 ---
    def paint(self, painter, option, index):
        path = index.data(QFileSystemModel.FilePathRole)
        if not path:
            super().paint(painter, option, index)
            return
        r = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        if selected:
            box = QRect(r).adjusted(2, 2, -2, -2)
            p = QPainterPath()
            p.addRoundedRect(QRectF(box), 7, 7)
            painter.fillPath(p, QBrush(QColor(T.ACCENT_SOFT)))
            painter.setPen(QPen(QColor(T.ACCENT), 1.2))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(p)

        name = index.data(Qt.DisplayRole) or ""
        if self.horizontal:
            n = min(self.thumb, r.height() - 4)
            icon_rect = QRect(r.x() + 5, r.y() + (r.height() - n) // 2, n, n)
            text_rect = QRect(icon_rect.right() + 7, r.y(),
                              max(10, r.right() - icon_rect.right() - 10), r.height())
        else:
            n = min(self.thumb, r.width() - 10, r.height() - (34 if self.show_size else 28))
            n = max(16, n)
            icon_rect = QRect(r.x() + (r.width() - n) // 2, r.y() + 5, n, n)
            text_rect = QRect(r.x() + 4, icon_rect.bottom() + 4, r.width() - 8,
                              max(10, r.bottom() - icon_rect.bottom() - 8))

        pm = self._pixmap(path)
        if pm is not None and not pm.isNull():
            scaled = pm.scaled(icon_rect.size(), Qt.KeepAspectRatio,
                               Qt.SmoothTransformation)
            dr = QRect(QPoint(0, 0), scaled.size())
            dr.moveCenter(icon_rect.center())
            painter.drawPixmap(dr, pm)
            painter.setPen(QPen(QColor(T.BORDER), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(dr)
        else:
            icon = index.data(Qt.DecorationRole)
            if isinstance(icon, QIcon):
                icon.paint(painter, icon_rect, Qt.AlignCenter)

        painter.setPen(QColor(T.TEXT))
        fm = QFontMetrics(option.font)
        if self.horizontal:
            painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter,
                             fm.elidedText(name, Qt.ElideMiddle, text_rect.width()))
        else:
            painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                             fm.elidedText(name, Qt.ElideMiddle, text_rect.width()))
        # 副标题（"大小 · 类型"）已删: 大/超大图标模式下，文件名下方不再画第二行小字（如 Joint Photographic 等）。
        painter.restore()

    # _sub_text() 副标题抽取方法已弃用（v31 删除副标题后）：保留这个方法以免引用遗失，
    # 期间一直返回空串，正式清理留待确认不再需要后由专门的清理轮处理。

    def _sub_text(self, index) -> str:  # pragma: no cover - 兼容占位
        return ""


class _PressResetFilter(QObject):
    """每次鼠标按下先清空 _toggled_item，保证该标记只反映"当前这一次点击"，
    不会被上一次点击（或键盘展开）残留的状态带到下一次判断里。"""

    def __init__(self, dlg):
        super().__init__(dlg)
        self._dlg = dlg

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            self._dlg._toggled_item = None
        return False


class _PathLineEdit(QLineEdit):
    """单击即选中全部文本（方便复制/修改整条路径），且不显示闪烁竖线。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setClearButtonEnabled(False)
        # 不显示闪烁的输入光标竖线（cursorWidth 属性在部分 PySide6 版本未暴露 setCursorWidth 方法，用 setProperty 兜底）
        self.setProperty("cursorWidth", 0)
        self._selected_once = False

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self.selectAll()
        self._selected_once = True

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if not self._selected_once:
            self.selectAll()


def _split_path(path: str) -> list:
    """把路径拆成 [(显示名, 前缀路径), ...]，用于面包屑跳转。

    例： ``C:\\Users\\29773\\Pictures\\深圳``
      -> [('C:', 'C:\\'), ('Users', 'C:\\Users'), ('29773', 'C:\\Users\\29773'),
          ('Pictures', 'C:\\Users\\29773\\Pictures'), ('深圳', 'C:\\Users\\29773\\Pictures\\深圳')]
    点任一段 = 跳到对应前缀路径。
    """
    path = os.path.normpath(path)
    drive, tail = os.path.splitdrive(path)
    segs = [s for s in tail.split(os.sep) if s]
    parts = []
    if drive:
        parts.append((drive, drive + os.sep))
        cur = drive + os.sep
        for s in segs:
            cur = os.path.join(cur, s)
            parts.append((s, cur))
    else:
        cur = ""
        for s in segs:
            cur = os.path.join(cur, s)
            parts.append((s, cur))
    return parts


class _BreadcrumbBar(QWidget):
    """路径面包屑：每个路径段都是一个可点击按钮，点哪段跳哪段；
    点面包屑空白处进入编辑（整行选中，可重输完整路径）。"""

    def __init__(self, on_navigate, parent=None):
        super().__init__(parent)
        self._on_navigate = on_navigate
        self._path = ""

        self._crumbs_row = QWidget()
        self._crumbs_layout = QHBoxLayout(self._crumbs_row)
        self._crumbs_layout.setContentsMargins(4, 0, 4, 0)
        self._crumbs_layout.setSpacing(0)

        self._edit = _PathLineEdit()
        self._edit.returnPressed.connect(self._on_edit_return)
        self._edit.editingFinished.connect(self._on_edit_finished)
        self._edit.setVisible(False)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._crumbs_row)
        self._stack.addWidget(self._edit)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack, 1)

        # 高度"按内容定"、不参与纵向拉伸。
        # 原因：普通 QWidget 默认纵向可增长，放进顶部那一行后会跟下面的文件区平分富余高度，
        # 导致整条工具栏被撑到两三百像素（输入框看起来高得离谱、文件区被压扁）。
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._stack.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self._crumbs_row.installEventFilter(self)
        self.setStyleSheet(
            "QPushButton { border:none; background:transparent; padding:3px 7px;"
            " border-radius:4px; }"
            "QPushButton:hover { background: rgba(120,120,120,0.20); }"
        )

    # ---- 外部接口 ----
    def set_path(self, path: str):
        self._path = path
        if self._stack.currentWidget() is self._edit:
            self._stack.setCurrentWidget(self._crumbs_row)
        self._edit.setText(path)
        self._build_crumbs(path)

    def eventFilter(self, obj, event):
        # 点面包屑空白处（非段按钮）→ 进入编辑模式
        if obj is self._crumbs_row and event.type() == QEvent.Type.MouseButtonPress:
            self._enter_edit()
            return True
        return super().eventFilter(obj, event)

    # ---- 内部 ----
    def _build_crumbs(self, path: str):
        while self._crumbs_layout.count():
            w = self._crumbs_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        parts = _split_path(path)
        for i, (label, prefix) in enumerate(parts):
            btn = QPushButton(label)
            btn.setFlat(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, p=prefix: self._on_segment_clicked(p))
            self._crumbs_layout.addWidget(btn)
            if i < len(parts) - 1:
                sep = QLabel("›")
                sep.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                sep.setAlignment(Qt.AlignCenter)
                self._crumbs_layout.addWidget(sep)
        self._crumbs_layout.addStretch(1)

    def _on_segment_clicked(self, prefix: str):
        self._on_navigate(prefix)

    def _enter_edit(self):
        self._edit.setText(self._path)
        self._stack.setCurrentWidget(self._edit)
        self._edit.setFocus()
        self._edit.selectAll()

    def _leave_edit(self):
        self._stack.setCurrentWidget(self._crumbs_row)

    def _on_edit_return(self):
        p = self._edit.text().strip()
        self._leave_edit()
        if p and os.path.isdir(p):
            self._on_navigate(p)
        else:
            QMessageBox.information(self, "路径不存在", f"找不到这个位置：{p}")
            self.set_path(self._path)

    def _on_edit_finished(self):
        if self._stack.currentWidget() is self._edit:
            self._leave_edit()


class PickerDialog(QDialog):
    def __init__(self, parent=None, start_path: str = None, recursive: bool = False):
        super().__init__(parent)
        self._recursive = recursive
        self.setWindowTitle("添加照片（文件夹或文件）")
        self.setMinimumSize(1080, 620)
        self.resize(1200, 700)

        self._model = None
        self._current = None
        self._icon_provider = QFileIconProvider()
        self._folder_icon = None        # 文件夹图标（只向系统取一次后缓存）
        self._file_icons: dict = {}     # 扩展名 -> 图标（避免逐个文件向系统取图标导致卡顿、丢点击）
        # 展开后"延后跳转"右主区域（防抖）：期间若被收起就取消跳转。
        # 目的：首次展开触发的主区域重建会占住界面，导致紧接着点"收起"没反应。
        self._nav_timer = QTimer(self)
        self._nav_timer.setSingleShot(True)
        self._nav_pending = None
        self._nav_timer.timeout.connect(self._flush_nav)
        self._toggled_item = None  # 本次点击是否触发过展开/折叠（用于区分点小三角/点名字）
        self._selected_folders: list[str] = []
        self._selected_files: list[str] = []
        self._preview_src = None        # 当前预览的原始大图(示例图/真实照片), 由 _refit_preview 按框尺寸裁剪填满
        # 浏览历史（仿 Windows 文件管理器：← 后退 / → 前进）
        self._history: list[str] = []
        self._hist_idx: int = -1

        self._build_ui()

        # 初始位置：传入的 start_path，否则"图片"库，否则用户主目录
        start = start_path
        if not start or not os.path.isdir(start):
            pics = QStandardPaths.standardLocations(QStandardPaths.StandardLocation.PicturesLocation)
            start = pics[0] if pics and os.path.isdir(pics[0]) else os.path.expanduser("~")
        self._navigate(os.path.normpath(start))

    # ---------------------------------------------------------------- 事件
    def showEvent(self, event):
        super().showEvent(event)
        self._refit_preview()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refit_preview()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(MID_SPACING)  # 左侧列与右侧预览栏之间的间距

        # 左侧列：顶部工具行 + 中部浏览区 + 底部按钮行
        left = QVBoxLayout()
        left.setSpacing(8)

        # 顶部：后退/前进箭头 + 文件位置输入框 + 显示方式
        top = QHBoxLayout()
        style = self.style()
        self.back_btn = QPushButton()
        self.back_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_ArrowBack))
        self.back_btn.setFixedSize(34, 28)
        self.back_btn.setToolTip("后退（上一个位置）")
        self.back_btn.clicked.connect(self._on_back)
        self.fwd_btn = QPushButton()
        self.fwd_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward))
        self.fwd_btn.setFixedSize(34, 28)
        self.fwd_btn.setToolTip("前进（下一个位置）")
        self.fwd_btn.clicked.connect(self._on_forward)
        top.addWidget(self.back_btn)
        top.addWidget(self.fwd_btn)
        self.path_edit = _BreadcrumbBar(self._navigate)
        top.addWidget(self.path_edit, 1)
        # 显示方式：关闭态与下拉选项文字都居中。
        # 关闭态居中沿用 dialogs._CenterCombo（字画在整框几何中心，下拉小三角不占布局宽度）；
        # 展开选项居中用 _CenterAlignDelegate（只强制 displayAlignment，渲染仍走基类完整链路）。
        self.view_combo = _CenterComboArrow()
        self.view_combo.setObjectName("viewCombo")
        self.view_combo.setToolTip("切换显示方式（也可在文件区按住 Ctrl 滚动滚轮）")
        for name, mode, _ico, _gw, _gh, _sub in VIEW_MODES:
            self.view_combo.addItem(name, mode)
        idx = next((i for i, c in enumerate(VIEW_MODES) if c[1] == DEFAULT_VIEW_MODE), 1)
        self.view_combo.setCurrentIndex(idx)
        self.view_combo.setItemDelegate(_CenterAlignDelegate(self.view_combo))
        self.view_combo.setStyleSheet(
            "#viewCombo { min-width: 96px; max-width: 96px;"
            " padding-left: 4px; padding-right: 2px; }"
            "#viewCombo::drop-down { width: 0px; border: none; }"
            "#viewCombo QAbstractItemView {"
            "  border-radius: 8px; padding: 4px; outline: none; font-size: 13px; }"
            "#viewCombo QAbstractItemView::item {"
            "  padding: 5px 10px; border-radius: 4px; }")
        self.view_combo.currentIndexChanged.connect(self._apply_view_mode)
        top.addWidget(self.view_combo)

        left.addLayout(top, 0)     # 明示：顶部工具栏不吃富余高度

        # 中部：左侧导航树 + 右列（文件列表 + 底部按钮行）
        # 导航树与右侧预览栏一样"上下贯穿"到底；底栏则挪到文件区下方那一列，
        # 使"文件类型"标签的左缘正好与文件显示区的左缘对齐。
        body = QHBoxLayout()
        body.setSpacing(MID_SPACING)

        self.nav_tree = QTreeWidget()
        self.nav_tree.setFixedWidth(190)
        self.nav_tree.setColumnCount(1)
        self.nav_tree.setHeaderHidden(True)
        self.nav_tree.setAnimated(False)  # 关掉展开/折叠动画，避免快速连点小三角时点击被吞
        self._nav_exp = {}  # 已废弃：改用 item 自带的 _ROLE_EXPANDED，避免 id() 地址复用导致状态误判  # id(item) -> 上次已知的展开状态，用于区分"点小三角"与"点文字"
        self.nav_tree.itemExpanded.connect(self._on_expand)
        self.nav_tree.itemCollapsed.connect(self._on_collapse)
        self.nav_tree.itemClicked.connect(self._on_nav_click)
        self.nav_tree.viewport().installEventFilter(_PressResetFilter(self))
        self._build_nav_top()
        body.addWidget(self.nav_tree)

        # 右列：文件列表（上）+ 底部按钮行（下）
        right_col = QVBoxLayout()
        right_col.setSpacing(8)
        mid = QHBoxLayout()
        mid.setSpacing(MID_SPACING)

        # 详细信息 = 表格；其余五种 = 同一个列表视图换参数
        self.list_view = _FileListView()
        self.list_delegate = _ThumbDelegate(self.list_view)
        self.list_view.setItemDelegate(self.list_delegate)
        self.list_view.doubleClicked.connect(self._on_double_click)

        self.tree = _FileTreeView()
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.doubleClicked.connect(self._on_double_click)
        # 选中样式：全局 QSS 只定义了 QTreeView::item 却没有 ::item:selected，
        # 一旦给 ::item 写过样式，selection-background-color 就不再生效，选中会看不见。
        # 这里只给本弹窗的两个树单独补上高对比的选中样式（不影响主窗口的照片网格）。
        self.tree.setStyleSheet(_SELECTED_QSS)
        self.nav_tree.setStyleSheet(_SELECTED_QSS)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setStretchLastSection(False)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.list_view)
        self.stack.addWidget(self.tree)
        mid.addWidget(self.stack, 1)
        right_col.addLayout(mid, 1)   # 明示：富余高度全部给文件浏览区

        # 底部：文件类型 + 按钮（在右列里，左缘即文件区左缘）
        bottom = QHBoxLayout()
        self.type_label = QLabel("文件类型：")
        bottom.addWidget(self.type_label)
        self.type_combo = QComboBox()
        self.type_combo.view().setMouseTracking(True)  # 弹出项要能显示悬浮高亮
        self.type_combo.setItemDelegate(_CenterAlignDelegate(self.type_combo))
        self.type_combo.addItem("全部文件 (*.*)", "*")
        self.type_combo.addItem("图片 (*.jpg *.jpeg *.png *.heic *.bmp *.gif *.tif *.tiff *.webp *.cr2 *.cr3 *.nef *.arw)", "image")
        self.type_combo.addItem("视频 (*.mp4 *.mov *.avi *.mkv *.mts *.m2ts *.wmv *.flv *.mpg *.mpeg)", "video")
        # 默认"全部"：右侧同时显示文件夹与文件，方便浏览与双击进入子目录。
        # 上一轮曾以"图片"为默认（让选图时不再被一堆目录占满），用户反馈更希望"全部显示"，
        # 故改回 index=0；想只看图/视频时再手动切。
        self.type_combo.setCurrentIndex(0)
        self.type_combo.currentIndexChanged.connect(self._apply_filter)
        bottom.addWidget(self.type_combo)

        # 「含子文件夹」开关：从主窗口工具栏挪到此处（文件类型下拉的右侧）
        self.recursive_check = CheckCheckBox("含子文件夹")
        self.recursive_check.setToolTip("勾选后会一并读取子文件夹里的照片")
        self.recursive_check.setChecked(self._recursive)
        bottom.addWidget(self.recursive_check)

        bottom.addStretch(1)
        # 「取消」按钮已删除：保留右上角系统 ❌ 与 Esc 关闭即可，避免同一对话框出现两个并行的取消入口。
        self.ok_btn = QPushButton("添加")
        self.ok_btn.setFixedWidth(90)
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self._on_accept)
        bottom.addWidget(self.ok_btn)
        right_col.addLayout(bottom)

        body.addLayout(right_col, 1)
        left.addLayout(body, 1)    # 导航树满高：从顶栏下方一直到底

        root.addLayout(left, 1)

        # 右侧预览栏：选中「文件夹」→ 列出它里面的内容；选中「文件」→ 显示大图。
        # 跨满整个窗口高度（顶到窗口最上、底到最下），连顶部"显示方式"行与底部"添加"行的高度也一并占掉。
        self.preview_panel = QWidget()
        self.preview_panel.setFixedWidth(PREVIEW_W)
        pv = QVBoxLayout(self.preview_panel)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(6)
        self.preview_title = QLabel("预览")
        self.preview_title.setProperty("h2", True)
        self.preview_title.setWordWrap(True)
        pv.addWidget(self.preview_title)
        self.preview_image = QLabel()
        self.preview_image.setAlignment(Qt.AlignCenter)
        self.preview_image.setWordWrap(True)
        self.preview_image.setMinimumHeight(140)
        self.preview_image.setStyleSheet(
            f"background: {T.THUMB_BG}; border-radius: 8px; color: {T.TEXT_WEAK};")
        pv.addWidget(self.preview_image, 1)
        self.preview_list = QListWidget()
        self.preview_list.setSelectionMode(QListWidget.NoSelection)
        self.preview_list.setFocusPolicy(Qt.NoFocus)
        self.preview_list.setVisible(False)
        pv.addWidget(self.preview_list, 1)
        self.preview_hint = QLabel("在左侧点选文件或文件夹，这里显示预览")
        self.preview_hint.setProperty("weak", True)
        self.preview_hint.setWordWrap(True)
        pv.addWidget(self.preview_hint)
        root.addWidget(self.preview_panel)

        self._apply_view_mode()

    # ---------------------------------------------------------------- 显示方式
    def _apply_view_mode(self):
        mode = self.view_combo.currentData() if self.view_combo else DEFAULT_VIEW_MODE
        if mode == "details":
            self.stack.setCurrentWidget(self.tree)
            return
        cfg = next((c for c in VIEW_MODES if c[1] == mode), VIEW_MODES[1])
        _name, mode, icon, gw, gh, sub = cfg
        v = self.list_view
        is_list = mode == "list"
        v.setViewMode(QListView.ListMode if is_list else QListView.IconMode)
        v.setFlow(QListView.TopToBottom if is_list else QListView.LeftToRight)
        v.setWrapping(True)
        v.setIconSize(QSize(icon, icon))
        v.setGridSize(QSize(gw, gh))
        v.setSpacing(2 if is_list else 8)
        v.setUniformItemSizes(True)
        self.list_delegate.configure(icon, QSize(gw, gh), bool(sub) and not is_list)
        self.list_delegate.horizontal = is_list
        self.stack.setCurrentWidget(v)
        v.scheduleDelayedItemsLayout()
        v.viewport().update()
        self._render_preview()

    # ---------------------------------------------------------------- Ctrl+滚轮缩放
    def _zoom_step(self, zoom_in: bool):
        """Ctrl+滚轮：在 超大/大/中/小图标 四档之间步进（其余模式先跳到"中等图标"）。"""
        icon_modes = [m[1] for m in VIEW_MODES[:4]]
        cur = self.view_combo.currentData()
        i = icon_modes.index(cur) if cur in icon_modes else 2
        i = max(0, min(len(icon_modes) - 1, i + (-1 if zoom_in else 1)))
        self.view_combo.setCurrentIndex(i)   # 触发 _apply_view_mode

    # ---------------------------------------------------------------- 右侧预览
    def _on_preview_selection(self, *_):
        self._render_preview()

    def _render_preview(self):
        """按当前选中项刷新右侧预览：文件夹→内容清单；文件→大图；其余→提示。"""
        view = self.stack.currentWidget()
        if self._model is None:
            return
        sm = view.selectionModel()
        idxs = [i for i in sm.selectedIndexes() if i.column() == 0] if sm else []
        paths = [os.path.normpath(self._model.filePath(i)) for i in idxs]
        dirs = [p for p in paths if os.path.isdir(p)]
        files = [p for p in paths if os.path.isfile(p)]

        self.preview_image.clear()
        self.preview_image.setVisible(False)
        self.preview_list.clear()
        self.preview_list.setVisible(False)
        self.preview_hint.setVisible(True)
        self._preview_src = None

        if len(paths) == 1 and dirs:
            self._show_folder_preview(dirs[0])
        elif len(paths) == 1 and files:
            self._show_file_preview(files[0])
        elif len(paths) > 1:
            self.preview_title.setText("预览")
            self.preview_hint.setText(f"已选中 {len(paths)} 项")
        else:
            self._show_preview_example()

    def _refit_preview(self):
        """按比例裁剪填满 preview_image 当前可用区域(cover), 保证右侧预览不出现上下留白。

        渲染发生在布局定稿之前时 preview_image.size() 还是旧值(偏小), 直接按它画的图会居中后
        上下留白; 这里统一在窗口首次显示 / 缩放时, 用真实尺寸把原始大图裁切填满。
        """
        if not self.preview_image.isVisible():
            return
        src = self._preview_src
        if src is None or src.isNull():
            return
        avail = self.preview_image.size()
        w, h = avail.width(), avail.height()
        if w <= 0 or h <= 0:
            return
        scaled = src.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        dx = max(0, (scaled.width() - w) // 2)
        dy = max(0, (scaled.height() - h) // 2)
        self.preview_image.setPixmap(scaled.copy(QRect(dx, dy, w, h)))

    def _show_preview_example(self):
        """未选中任何项时: 右侧预览区只显示一个空框（浅色占位，不画装饰示例图）。"""
        self.preview_title.setText("预览")
        self.preview_image.setVisible(True)
        self.preview_image.setPixmap(QPixmap())   # 清空内容，仅留浅色背景空框
        self.preview_hint.setVisible(False)
        self._preview_src = None

    def _show_file_preview(self, path: str):
        self.preview_title.setText(os.path.basename(path))
        self.preview_image.setVisible(True)
        self.preview_hint.setVisible(False)
        # 加载较大尺寸, 再交给 _refit_preview 裁剪填满(cover), 避免竖长面板里照片居中留白。
        pm = self._preview_pixmap(path, 900)
        if pm is None or pm.isNull():
            self.preview_image.clear()
            self.preview_image.setText("（无法预览该文件）")
            self._preview_src = None
            return
        self._preview_src = pm
        self._refit_preview()

    def _show_folder_preview(self, folder: str):
        name = os.path.basename(folder) or folder
        self.preview_list.setVisible(True)
        self.preview_hint.setVisible(False)
        try:
            entries = list(os.scandir(folder))
        except OSError as ex:
            self.preview_list.setVisible(False)
            self.preview_hint.setVisible(True)
            self.preview_title.setText(name)
            self.preview_hint.setText(f"无法读取该文件夹：{ex}")
            return
        dirs = sorted([e for e in entries if e.is_dir()], key=lambda e: e.name.lower())
        files = sorted([e for e in entries if e.is_file()], key=lambda e: e.name.lower())
        self.preview_title.setText(f"{name}（{len(dirs) + len(files)} 项）")
        for e in dirs + files:
            it = QListWidgetItem(("[文件夹] " if e.is_dir() else "[文件]　") + e.name)
            if e.is_file():
                try:
                    it.setToolTip(f"{e.name}\n{self._fmt_size(e.stat().st_size)}")
                except OSError:
                    it.setToolTip(e.name)
            else:
                it.setToolTip(e.name)
            self.preview_list.addItem(it)
        if not dirs and not files:
            self.preview_hint.setVisible(True)
            self.preview_hint.setText("这个文件夹是空的")

    def _preview_pixmap(self, path: str, max_edge: int):
        """预览取图：常用图片走 QImageReader 直读缩放；HEIC/RAW/视频走 thumbs 管线（含磁盘缓存）。"""
        d = self.list_delegate
        ext = os.path.splitext(path)[1].lower()
        pm = None
        if ext in d.IMG_EXT:
            pm = d._load(path, max_edge)
        if pm is None or pm.isNull():
            pm = d._load_heavy(path)
        return pm

    @staticmethod
    def _fmt_size(size: int) -> str:
        if size <= 0:
            return "0 B"
        units = ["B", "KB", "MB", "GB"]
        i = 0
        v = float(size)
        while v >= 1024 and i < len(units) - 1:
            v /= 1024.0
            i += 1
        return f"{v:.1f} {units[i]}" if i else f"{int(v)} B"

    # ---------------------------------------------------------------- 左侧导航树
    def _icon_for(self, path: str):
        """顶层项用：向系统取真实图标（数量少，可承受）。"""
        return self._icon_provider.icon(QFileInfo(path))

    def _icon_cached(self, path: str, is_dir: bool):
        """子项用：图标按"文件夹/扩展名"缓存，避免每条目都向系统取图标造成卡顿、快速连点被吞。"""
        if is_dir:
            if self._folder_icon is None:
                self._folder_icon = self._icon_provider.icon(QFileIconProvider.Folder)
            return self._folder_icon
        ext = os.path.splitext(path)[1].lower()
        icon = self._file_icons.get(ext)
        if icon is None:
            icon = self._icon_provider.icon(QFileInfo(path))
            self._file_icons[ext] = icon
        return icon

    def _add_nav_item(self, parent, name: str, path: str, kind: str, exact_icon: bool = False):
        item = QTreeWidgetItem(parent, [name])
        is_dir = kind in ("disk", "folder")
        item.setIcon(0, self._icon_for(path) if exact_icon else self._icon_cached(path, is_dir))
        item.setData(0, _ROLE_PATH, os.path.normpath(path))
        item.setData(0, _ROLE_KIND, kind)
        item.setData(0, _ROLE_LOADED, kind == "file")  # 文件无子项，视为已加载
        item.setData(0, _ROLE_EXPANDED, False)
        # 文件夹/盘：放一个占位子项，使其出现"可展开"三角
        if is_dir:
            ph = QTreeWidgetItem(item, ["（点击展开）"])
            ph.setData(0, _ROLE_KIND, "placeholder")
        return item

    def _build_nav_top(self):
        self.nav_tree.clear()
        for d in DRIVE_NAMES:
            path = d + os.sep
            if os.path.isdir(path):
                self._add_nav_item(self.nav_tree, d, path, "disk", exact_icon=True)
        for name, path in _special_folders():
            self._add_nav_item(self.nav_tree, name, path, "folder", exact_icon=True)

    def _populate(self, item: QTreeWidgetItem):
        path = item.data(0, _ROLE_PATH)
        if not path or not os.path.isdir(path):
            return
        # 先标记为已加载：快速连点可能重入，避免重复加载/重复添加子项
        item.setData(0, _ROLE_LOADED, True)

        dir_obj = QDir(path)
        entries = dir_obj.entryList(
            QDir.Dirs | QDir.Files | QDir.NoDotAndDotDot,
            QDir.DirsFirst | QDir.Name | QDir.Type,
        )
        # 先把真实子项挂上（此时占位子项仍在，childCount 不会归零）
        # —— 归零会让"已展开"的项被判为无可展开内容而塌回去，表现为连点小三角没反应
        for name in entries:
            child_path = os.path.join(path, name)
            kind = "folder" if os.path.isdir(child_path) else "file"
            self._add_nav_item(item, name, child_path, kind)
        # 子项就位后，再移除占位子项
        for i in range(item.childCount()):
            child = item.child(i)
            if child is not None and child.data(0, _ROLE_KIND) == "placeholder":
                item.removeChild(child)
                break

    def _on_expand(self, item: QTreeWidgetItem):
        if item.data(0, _ROLE_KIND) == "placeholder":
            return
        if not item.data(0, _ROLE_LOADED):
            self._populate(item)
        # 展开【不立刻】跳转主区域，改为延后跳转（见 _schedule_navigate）。
        # 若这里直接 _navigate，首次展开会触发右主区域重建（要为大量文件取图标），
        # 界面被占住，紧接着点击"收起"就没反应，必须等一下才行。
        kind = item.data(0, _ROLE_KIND)
        if kind in ("disk", "folder"):
            path = item.data(0, _ROLE_PATH)
            if path and os.path.isdir(path):
                self._schedule_navigate(path)
        item.setData(0, _ROLE_EXPANDED, True)
        self._toggled_item = item

    def _on_collapse(self, item: QTreeWidgetItem):
        """收起：立刻取消尚未执行的跳转，保证小三角始终跟手。"""
        item.setData(0, _ROLE_EXPANDED, False)
        self._toggled_item = item
        self._cancel_navigate()

    # --- 延后跳转（防抖）---
    def _schedule_navigate(self, path: str):
        self._nav_pending = path
        self._nav_timer.start(260)

    def _cancel_navigate(self):
        self._nav_timer.stop()
        self._nav_pending = None

    def _flush_nav(self):
        path = self._nav_pending
        self._nav_pending = None
        if path and os.path.isdir(path):
            self._navigate(path)

    def _on_nav_click(self, item: QTreeWidgetItem, _col: int):
        kind = item.data(0, _ROLE_KIND)
        if kind in (None, "placeholder"):
            return
        # 本次点击刚触发过展开/折叠（=点在小三角上）：不做立即跳转，交给防抖定时器，
        # 这样小三角始终跟手，不会被右主区域的重建拖住。
        # 用"同一个 item 对象"判断，不依赖信号先后顺序，也不依赖 id() 地址。
        if self._toggled_item is item:
            self._toggled_item = None
            return
        # 点的是名字：立即跳转（并把可能挂着的延后跳转取消掉）
        path = item.data(0, _ROLE_PATH)
        if kind == "file":
            path = os.path.dirname(path)
        if path and os.path.isdir(path):
            self._cancel_navigate()
            self._navigate(path)

    # ---------------------------------------------------------------- 导航（主区域）
    def _navigate(self, path: str, from_history: bool = False):
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            return
        # 已经在当前位置就别重复重建：快速连点时反复 setRootPath 会让主区域不停刷新，
        # 视觉上就像点击没被响应
        if self._current and os.path.normcase(path) == os.path.normcase(self._current):
            self.path_edit.set_path(path)
            return
        if self._model is None:
            self._model = QFileSystemModel()
            self._model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot | QDir.AllDirs)
            self.tree.setModel(self._model)
            self.list_view.setModel(self._model)
            # 选中项变化 → 刷新右侧预览；Ctrl+滚轮 → 切换图标档位
            self.list_view.selectionModel().selectionChanged.connect(self._on_preview_selection)
            self.tree.selectionModel().selectionChanged.connect(self._on_preview_selection)
            self.list_view._on_zoom = self._zoom_step
            self.tree._on_zoom = self._zoom_step
        self._current = path
        self._model.setRootPath(path)
        root = self._model.index(path)
        self.tree.setRootIndex(root)
        self.list_view.setRootIndex(root)
        self._model.sort(0, Qt.AscendingOrder)   # 两种方式都按名称排，文件夹在前
        self.path_edit.set_path(path)
        self._apply_filter()
        self._render_preview()
        if not from_history:
            self._push_history(path)

    # ---------------------------------------------------------------- 浏览历史（仿 Windows 文件管理器）
    def _push_history(self, path: str):
        """记录一次导航；若当前停在历史中段（用过"前进"后又去了新位置），截断其后半段。"""
        if 0 <= self._hist_idx < len(self._history) - 1:
            self._history = self._history[:self._hist_idx + 1]
        if not self._history or os.path.normcase(self._history[-1]) != os.path.normcase(path):
            self._history.append(path)
            self._hist_idx = len(self._history) - 1
        self._update_nav_arrows()

    def _update_nav_arrows(self):
        if not hasattr(self, "back_btn"):
            return
        self.back_btn.setEnabled(self._hist_idx > 0)
        self.fwd_btn.setEnabled(self._hist_idx < len(self._history) - 1)

    def _on_back(self):
        """← 后退：回到上一个访问过的位置。"""
        if self._hist_idx > 0:
            self._hist_idx -= 1
            self._navigate(self._history[self._hist_idx], from_history=True)
            self._update_nav_arrows()

    def _on_forward(self):
        """→ 前进：跳到"后退"之前的位置。"""
        if self._hist_idx < len(self._history) - 1:
            self._hist_idx += 1
            self._navigate(self._history[self._hist_idx], from_history=True)
            self._update_nav_arrows()

    def _on_double_click(self, index: QModelIndex):
        path = os.path.normpath(self._model.filePath(index))
        if os.path.isdir(path):
            self._navigate(path)

    def _apply_filter(self):
        if self._model is None:
            return
        mode = self.type_combo.currentData()
        if mode == "image":
            pat = "*.jpg *.jpeg *.png *.heic *.bmp *.gif *.tif *.tiff *.webp *.cr2 *.cr3 *.nef *.arw"
        elif mode == "video":
            pat = "*.mp4 *.mov *.avi *.mkv *.mts *.m2ts *.wmv *.flv *.mpg *.mpeg"
        else:
            pat = "*"
        # 任何过滤模式下都同时显示目录与文件: 用户希望"全部显示"，方便浏览与双击进入子目录。
        # (v29 的旧做法在 image/video 模式下用 setFilter(AllEntries|NoDotAndDotDot) 把目录一起挡掉;
        #  v31 改为统一保留 AllDirs，用户可手动切到"全部"看图与目录并存。)
        self._model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot | QDir.AllDirs)
        self._model.setNameFilters(pat.split())
        self._model.setNameFilterDisables(False)

    # ---------------------------------------------------------------- 确认
    @staticmethod
    def _folder_yields_content(folder: str, recursive: bool) -> bool:
        """快速判定：该文件夹按当前递归设置是否含有可添加的图片/视频（找到第一个即返回）。"""
        from ..core.photo import kind_of
        try:
            if recursive:
                for root, dirs, names in os.walk(folder):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for f in names:
                        if f.startswith("."):
                            continue
                        if kind_of(os.path.join(root, f)) != "other":
                            return True
            else:
                with os.scandir(folder) as it:
                    for entry in it:
                        if entry.name.startswith("."):
                            continue
                        if entry.is_file() and kind_of(entry.path) != "other":
                            return True
        except OSError:
            return False
        return False

    def _on_accept(self):
        view = self.stack.currentWidget()
        # 注意: 在 QListView 上 selectedRows() 会返回空(只有 selectedIndexes() 有值),
        # 所以统一从 selectedIndexes() 里筛第 0 列, 两种视图都稳妥。
        idxs = [i for i in view.selectionModel().selectedIndexes() if i.column() == 0]
        folders: list[str] = []
        files: list[str] = []
        for idx in idxs:
            fp = os.path.normpath(self._model.filePath(idx))
            if os.path.isdir(fp):
                folders.append(fp)
            elif os.path.isfile(fp):
                files.append(fp)
        # 允许不选具体项时，直接把当前所在文件夹作为文件夹添加
        if not folders and not files and os.path.isdir(self._current):
            folders.append(self._current)

        # 未选中任何具体文件时，先判定选中的文件夹会不会“什么都没添加进来”：
        # 若全部选中文件夹经当前递归设置后都为空，弹窗提示并留住对话框，让用户勾选「含子文件夹」重试。
        if folders and not files:
            recursive = self.recursive_check.isChecked()
            empty_folders = [f for f in folders
                            if not self._folder_yields_content(f, recursive)]
            if empty_folders and len(empty_folders) == len(folders):
                sub_only = []
                for f in empty_folders:
                    try:
                        with os.scandir(f) as it:
                            if any(e.is_dir() for e in it):
                                sub_only.append(f)
                    except OSError:
                        pass
                if not recursive and sub_only:
                    names = "、".join(os.path.basename(f) for f in sub_only[:3])
                    more = " 等" if len(sub_only) > 3 else ""
                    QMessageBox.information(
                        self, "没有可添加的内容",
                        f"「{names}{more}」里只有子文件夹，没有直接的图片或视频。\n"
                        f"如需读入子文件夹里的照片，请勾选下方「含子文件夹」后再点“添加”。")
                else:
                    names = "、".join(os.path.basename(f) for f in empty_folders[:3])
                    more = " 等" if len(empty_folders) > 3 else ""
                    QMessageBox.information(
                        self, "没有可添加的内容",
                        f"在「{names}{more}」里没有找到图片或视频。")
                return  # 不关闭对话框，方便用户修正后重试

        self._selected_folders = folders
        self._selected_files = files
        self.accept()

    # ---------------------------------------------------------------- 结果
    def selected_folders(self) -> list[str]:
        return self._selected_folders

    def selected_files(self) -> list[str]:
        return self._selected_files

    def recursive(self) -> bool:
        """是否勾选了「含子文件夹」（由弹窗底部勾选框决定）。"""
        return self.recursive_check.isChecked()
