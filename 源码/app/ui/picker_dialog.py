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
import re
import sys

from PySide6.QtCore import (QEvent, QItemSelection, QItemSelectionModel, QObject, QPoint,
                            QRect, QRectF, QSize, Qt, QDir, QFileInfo,
                            QModelIndex, QSortFilterProxyModel, QStandardPaths, QTimer, QUrl,
                            Signal)
from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QFontMetrics, QIcon,
                           QImage, QLinearGradient, QMouseEvent, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygon,
                           QDesktopServices)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileIconProvider,
    QFileSystemModel, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QPushButton, QSizePolicy, QStackedWidget, QStyle, QStyleOptionViewItem,
    QStyledItemDelegate, QTreeView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from . import theme as T
from .dialogs import _CenterAlignDelegate, _CenterCombo, _DownComboBox
from .widgets import CheckCheckBox, Marquee

DRIVE_NAMES = ["C:", "D:"]

# 显示方式: (名称, 模式, 图标边长, 网格宽, 网格高, 是否显示"类型/大小"副标题)
VIEW_MODES = [
    ("超大图标", "xl", 128, 168, 186, True),
    ("大图标", "lg", 96, 132, 150, True),
    ("中图标", "md", 48, 104, 116, False),
    ("小图标", "sm", 32, 88, 96, False),
    ("详细信息", "details", 16, 0, 0, False),
]
DEFAULT_VIEW_MODE = "lg"          # 默认"大图标"
PREVIEW_W = 232                   # 右侧"预览"栏宽度(顶栏右侧要为它留白, 见 _build_ui)
MID_SPACING = 10                  # 中部各栏之间的间距
PATH_MIN_W = 280                  # 路径框最小宽度：窗口缩到最小时仍能完整看到一段路径

# 选中样式：选中/失焦两种情况都显式指定，保证任何情况下都能看到高亮。
# v220: 加悬浮一档(浅蓝), 选中改成深一档的浅蓝 + 深色字 —— 和资源管理器一致。
# ⚠️ 顺序不能反: 两条选择器权重相同, 后写的赢, 所以 :hover 必须在 :selected 前面。
_SELECTED_QSS = (
    "QTreeView::item:hover {"
    "  background: " + T.HOVER_SOFT + ";"
    "}"
    "QTreeView::item:selected {"
    "  background: " + T.SEL_SOFT + ";"
    "  color: " + T.TEXT + ";"
    "}"
    "QTreeView::item:selected:!active {"
    "  background: " + T.SEL_SOFT + ";"
    "  color: " + T.TEXT + ";"
    "}"
)

# 右侧预览列表的两种底色：悬浮(浅蓝) / 双击选中(更深蓝)。
# 用 Python 手动上色(而非 QSS :hover/:selected)，避免与 Fusion 默认高亮打架，
# 也保证"选中态"能跨悬浮持续存在（移到别处再回来仍是更深蓝）。
_PREVIEW_HOVER = T.HOVER_SOFT      # 悬浮：浅蓝
_PREVIEW_SELECTED = T.SEL_SOFT     # 双击选中：更深一档
# ⚠️ 高亮态只用这个自定义 role 存，绝不碰 BackgroundRole：
# item.setBackground(QColor()) 会写入"无效色刷(QColor(Invalid))"，带全局主题时该项被画成黑色，
# 这就是"鼠标划过就变黑"的真凶。BackgroundRole 保持 None(白底)即可。
_ROLE_HL = Qt.UserRole + 5         # 'hover' / 'selected' / None

_ROLE_PATH = Qt.UserRole          # 0x0100
_ROLE_KIND = Qt.UserRole + 1      # disk / folder / file / placeholder
_ROLE_LOADED = Qt.UserRole + 2    # 是否已加载过子项
_ROLE_EXPANDED = Qt.UserRole + 3  # 上一次已知的展开状态（区分"点小三角"与"点文字"）


class _PreviewDelegate(QStyledItemDelegate):
    """右侧预览列表的绘制代理。

    之所以自绘背景: 一旦给 QListWidget 设置 ``QListWidget::item { ... }`` 样式,
    Qt 会切换到"样式表项渲染"模式, 此时 ``item.setBackground()``(BackgroundRole) 被忽略,
    悬浮/选中的蓝底怎么设都不显示。这里改成在代理里自己画底, 并且:

    1) **先画一层不透明浅色底**, 把样式表/系统主题在 hover 时可能画出来的暗色/黑色阴影
       整个盖掉(无论系统是 Fusion 还是原生 Windows 主题, 都不会再透出黑底);
    2) 再在底上画圆角蓝底(悬浮浅蓝 / 双击更深蓝);
    3) 调用父类 paint 前, 清掉选中/悬浮/焦点状态位并清空 backgroundBrush,
       让父类只画文字和图标, **绝不**再画任何背景, 杜绝黑底回潮。
    """

    def paint(self, painter, option, index):
        hl = index.data(_ROLE_HL)          # 'hover' / 'selected' / None
        # 1) 不透明浅色底, 盖掉任何系统默认的背景/阴影
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(T.SURFACE)))
        painter.drawRect(option.rect)
        # 2) 蓝底(悬浮/选中) —— 读自定义 role, 不碰 BackgroundRole
        color = None
        if hl == "hover":
            color = QColor(_PREVIEW_HOVER)
        elif hl == "selected":
            color = QColor(_PREVIEW_SELECTED)
        if color is not None:
            painter.setBrush(QBrush(color))
            painter.drawRoundedRect(option.rect.adjusted(1, 1, -1, -1), 5, 5)
        painter.restore()
        # 3) 让父类只画文字/图标, 不画任何背景
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.backgroundBrush = QBrush()
        opt.state &= ~(QStyle.State_Selected | QStyle.State_MouseOver |
                       QStyle.State_HasFocus | QStyle.State_Active)
        super().paint(painter, opt, index)



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
    """给视图补一个"看得见的"拉框, 并且**跟着滚轮/滚动条走**。

    为什么自己实现:
    - 全局 QSS 里有 `QWidget { background: transparent; }`, 系统风格的橡皮筋会被
      画成透明, 于是"能选中但看不见框";
    - Qt 自带的橡皮筋只在鼠标移动时才算框: 一边拉框一边滚滚轮, 框不会跟着内容走,
      新滚出来的文件也选不上;
    - 自带的那个还会在"从条目上按下"的拖动里插一脚, 于是屏幕上出现**两个框**。

    做法: 左键在文件区按下之后, **所有移动都由这里接管**(不再交给 Qt), 框的两个角
    记成**内容坐标**, 于是滚轮/滚动条一动, 框在视口里就跟着变, 新露出来的文件也一并
    选中。选择也自己算: 按 **rootIndex** 遍历行(视图是以文件夹为根的, 顶层
    rowCount() 恒为 1), 多列视图按整行宽算。

    验证脚本: out/repro/verify_marquee.py(框内条目 == 选中条目, 双视图各 17 项)。
    """

    def _mq_init(self):
        self._mq = None
        self._mq_anchor = None     # 起点(内容坐标)
        self._mq_last = None       # 鼠标位置(内容坐标)
        self._mq_mouse = None      # 鼠标位置(视口坐标, 滚轮后要拿它重算)
        self._mq_press_view = None  # 按下时鼠标位置(视口坐标, 用来判断"算不算拖动")
        self._mq_add = False       # 按住 Ctrl 拉框 = 追加
        self._mq_base: list = []   # 按下时已经选中的
        self._mq_on = False        # 左键按下中(可能还没真的拉出框)
        self._mq_live = False      # 已经拖出框了
        self._mq_scr = False       # 滚动条信号是否已接
        self._on_zoom = None       # Ctrl+滚轮回调: 参数 True=放大 / False=缩小

    # ------------------------------------------------------------ 坐标换算
    def _mq_bar_px(self, sb, horizontal: bool = False) -> int:
        """滚动条 → 滚动**像素**偏移。

        ⚠️ 不能直接把 `scrollBar().value()` 当像素: 详细信息视图是按**行**滚的
        (value 是行号, 得乘行高), 而大图标视图虽然也报 ScrollPerItem, 实际却是按
        像素滚的 —— 按 mode 分支一定会算错一边。统一用"第一行/第一列现在落在视口
        哪个位置"反推, 两种视图都准; 反推不出来才退回原值。
        """
        if sb is None:
            return 0
        val = sb.value()
        if val == 0:
            return 0
        m, root = self.model(), self.rootIndex()
        if m is not None and m.rowCount(root) > 0:
            vr = self.visualRect(m.index(0, 0, root))
            if vr.isValid():
                off = -vr.left() if horizontal else -vr.top()
                if off > 0:
                    return off
        return val

    def _mq_offsets(self):
        """(水平, 垂直) 滚动像素偏移。"""
        return (self._mq_bar_px(self.horizontalScrollBar(), True),
                self._mq_bar_px(self.verticalScrollBar(), False))

    def _mq_content(self, pos: QPoint) -> QPoint:
        hx, vy = self._mq_offsets()
        return QPoint(pos.x() + hx, pos.y() + vy)

    # ------------------------------------------------------------ 事件
    def _mq_press(self, event) -> bool:
        """左键按下。返回 True = 空白处按下(自己管选择); False = 按在条目上(交给 Qt 点选)。

        注意: 即使返回 False, 后面的**拖动**仍然由 _mq_move 接管 —— 否则 Qt 会自己
        起一个橡皮筋, 画面上就会出现第二个框(用户报的"两个框选框")。
        """
        if event.button() != Qt.LeftButton:
            return False
        self._mq_teardown()             # 防残留: 上一轮没能正常收尾也不能留下旧框
        self._mq_hook_scroll()
        pos = event.position().toPoint()
        sm = self.selectionModel()
        self._mq_on = True
        self._mq_live = False
        self._mq_add = bool(event.modifiers() & Qt.ControlModifier)
        self._mq_base = list(sm.selectedIndexes()) if (self._mq_add and sm) else []
        self._mq_anchor = self._mq_content(pos)
        self._mq_last = self._mq_anchor
        self._mq_mouse = pos
        self._mq_press_view = pos
        if self.indexAt(pos).isValid():
            return False                # 条目上: 先让 Qt 完成点选, 拖动再看
        self.setFocus(Qt.MouseFocusReason)
        if sm is not None and not self._mq_add:
            sm.clearSelection()
        return True

    def _mq_move(self, event):
        """左键拖动: 达到阈值就正式拉框; 返回 True 表示"这次移动已被我们吃掉"。

        只要左键是按下的, 就**绝不**把移动交给 Qt —— Qt 的橡皮筋也是在这条路上起
        来的, 让它们同时跑就是"两个框"。
        """
        if not self._mq_on:
            # 没走过 _mq_press 却按着左键(例如拖到一半被右键打断)也不交给 Qt:
            # Qt 的橡皮筋就是在 super().mouseMoveEvent 里起来的, 交给它就可能多一个框。
            return bool(event.buttons() & Qt.LeftButton)
        if not (event.buttons() & Qt.LeftButton):
            # 左键已经松开了却没收到 release(拖到窗口外松手、被别的窗口抢走):
            # 这时候再移动鼠标就把残框收掉, 否则屏幕上会留下一个不跟手的旧框。
            self._mq_release()
            return False
        pos = event.position().toPoint()
        if not self._mq_live:
            if self._mq_press_view is None:
                return True
            if (pos - self._mq_press_view).manhattanLength() < 4:
                return True             # 还只是"点了一下", 别急着画框
            self._mq_live = True
            if not self._mq_add:
                sm = self.selectionModel()
                if sm is not None:
                    sm.clearSelection()
                self._mq_base = []
        self._mq_update(pos)
        return True

    def _mq_widget(self):
        """拿到当前那个可见的框; 有就复用, 没有才新建。"""
        if self._mq is not None and self._mq.isVisible():
            return self._mq
        self._mq_teardown()
        self._mq = Marquee(self.viewport())
        self._mq.show()
        return self._mq

    def _mq_teardown(self):
        """彻底销毁框控件(不是只隐藏), 保证画面上最多只有一个。"""
        w = self._mq
        self._mq = None
        if w is not None:
            try:
                w.hide()
                w.deleteLater()
            except Exception:
                pass

    def _mq_hook_scroll(self):
        """滚动条一变就重算框: 滚轮/拖滚动条/键盘翻页全都跟得上。"""
        if self._mq_scr:
            return
        for sb in (self.verticalScrollBar(), self.horizontalScrollBar()):
            if sb is not None:
                sb.valueChanged.connect(self._mq_after_wheel)
        self._mq_scr = True

    def _mq_update(self, pos: QPoint, force: bool = False):
        """鼠标在 pos(视口坐标); force=True 用于滚动后重新算框。"""
        if self._mq_anchor is None:
            return
        self._mq_mouse = pos
        self._mq_last = self._mq_content(pos)
        hx, vy = self._mq_offsets()
        anchor_view = QPoint(self._mq_anchor.x() - hx, self._mq_anchor.y() - vy)
        if not force and not self._mq_live and \
                (pos - anchor_view).manhattanLength() < 4:
            return                       # 还没到"拉框"的程度, 只是点了一下
        box = QRect(anchor_view, pos).normalized()
        self._mq_widget().setGeometry(box)
        self._mq_widget().raise_()
        self._mq_select(QRect(self._mq_anchor, self._mq_last).normalized())

    def _mq_first_row(self, model, root, rows: int, top_view: int) -> int:
        """二分找"第一个还没被框绕过"的行(参数是视口坐标的框顶)。

        `visualRect().bottom()` 随行号单调不减(有换行的网格也一样), 所以可以二分。
        取不到可信几何(还没布局完)就退回 0 全扫 —— 宁可慢一点, 不能漏选。
        """
        lo, hi, best = 0, rows - 1, rows - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            vr = self.visualRect(model.index(mid, 0, root))
            if not vr.isValid() or vr.isEmpty():
                return 0
            if vr.bottom() < top_view:
                lo = mid + 1
            else:
                best = mid
                hi = mid - 1
        return best

    def _mq_select(self, rect: QRect):
        """选中内容矩形里的所有条目(rect 是内容坐标)。

        ⚠️ 遍历必须带 **rootIndex**: 这两个视图是"以当前文件夹为根"的
        (`setRootIndex`), 顶层的 `model.rowCount()` 恒等于 1 —— 照它遍历只会检查
        一行, 于是"拉框一个都选不中"(v124 修掉的正是这个)。
        """
        sm = self.selectionModel()
        model = self.model()
        if sm is None or model is None:
            return
        root = self.rootIndex()
        rows = model.rowCount(root)
        if rows <= 0:
            return
        hx, vy = self._mq_offsets()
        vp = self.viewport().rect()
        # 是不是"一行有好几列"的详细信息视图? 不能看 columnCount —— 文件模型的
        # columnCount 恒为 4(名称/大小/类型/日期), 但大图标视图只显示第 0 列。
        multi_col = (model.columnCount(root) > 1
                     and self.visualRect(model.index(0, 1, root)).isValid())
        # 从框顶那一行开始扫(几千个文件时不必每次从第 0 行数起)。
        # ⚠️ 不能拿"左上角 indexAt 到的行"当起点: 大图标是网格, 行号是按列优先
        #    排的, indexAt 命中的那一行会跳过它上方同一屏的其它行 → 上半截选不中。
        #    visualRect().bottom() 随行号单调不减, 二分才是对的。
        first = self._mq_first_row(model, root, rows, rect.top() - vy)
        hits = []
        for r in range(first, min(rows, first + 20000)):
            idx = model.index(r, 0, root)
            if not idx.isValid():
                continue
            vr = self.visualRect(idx)
            if not vr.isValid() or vr.isEmpty():
                continue
            if multi_col:
                # ⚠️ 多列视图里 visualRect(index(r,0)) 只有第一列那么宽(100px),
                #    框拉在右边就永远打不中 —— 一行得按整行宽算。
                vr = QRect(0, vr.top(), max(vr.width(), vp.width()), vr.height())
            vr = vr.translated(hx, vy)      # 视口坐标 → 内容坐标
            if vr.top() > rect.bottom():
                break                       # 行是自上而下排的(含网格换行), 后面更靠下
            if vr.bottom() < rect.top():
                continue
            if vr.intersects(rect):
                hits.append(idx)
        sel = QItemSelection()
        for idx in list(self._mq_base) + hits:
            if idx.isValid():
                sel.select(idx, idx)
        flag = QItemSelectionModel.Select if self._mq_add else QItemSelectionModel.ClearAndSelect
        sm.select(sel, flag | QItemSelectionModel.Rows)

    def _mq_release(self):
        self._mq_teardown()
        self._mq_anchor = None
        self._mq_last = None
        self._mq_mouse = None
        self._mq_press_view = None
        self._mq_base = []
        self._mq_on = False
        self._mq_live = False

    # ------------------------------------------------------------ 滚轮
    def _mq_wheel(self, event) -> bool:
        """Ctrl+滚轮 → 交给回调切换图标档位；返回 True 表示已消费。"""
        if (event.modifiers() & Qt.ControlModifier) and self._on_zoom is not None:
            self._on_zoom(event.angleDelta().y() > 0)
            event.accept()
            return True
        return False

    def _mq_after_wheel(self, *args):
        """滚动位置变了(滚轮 / 拖滚动条 / 键盘翻页): 框和选中一起跟着内容走。

        ⚠️ 一定要带 *args: 这个函数同时接在 scrollBar().valueChanged(int) 上。
        """
        if self._mq_on and self._mq_live and self._mq_mouse is not None:
            self._mq_update(self._mq_mouse, force=True)


def _norm_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path or ""))


ROLE_DIM = Qt.UserRole + 31   # 目录行：里面的文件已经全加进去了 → 灰色、不可选


def _kind_of(path: str) -> str:
    from ..core.photo import kind_of
    return kind_of(path)


class _HideAddedProxy(QSortFilterProxyModel):
    """已加入的**文件**藏起来；内容全部加过的**目录**则留着但灰掉不可选。

    目录不藏的原因：藏了用户就没法继续往里点。改成"灰掉"既表达了
    「这层已经没有可加的东西了」，又保留了浏览能力。
    """

    _WALK_LIMIT = 4000        # 递归判断时的上限，避免超大目录把界面卡住

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hidden: set[str] = set()
        self.recursive_getter = None      # 由 PickerDialog 注入：() -> bool（含子文件夹）
        self._dir_cache: dict[str, bool] = {}
        # 顺序不交给 proxy: lessThan() 直接沿用源模型的行序（QFileSystemModel 保证
        # "文件夹在前"）。过滤保持动态，切文件夹/换筛选后都能立刻生效。

    def _bump(self):
        self._dir_cache.clear()

    def set_hidden(self, paths):
        norm = {_norm_key(p) for p in (paths or [])}
        if norm != self.hidden:
            self.hidden = norm
            self._bump()
            self.invalidateFilter()

    def add_hidden(self, paths):
        new = {_norm_key(p) for p in (paths or [])} - self.hidden
        if new:
            self.hidden |= new
            self._bump()
            self.invalidateFilter()

    # ---------------------------------------------------------------- 目录判定
    def all_added(self, path: str) -> bool:
        """这个目录里（可加入的图片/视频）是不是已经全在 hidden 里了。"""
        key = os.path.normcase(path)
        hit = self._dir_cache.get(key)
        if hit is not None:
            return hit
        res = self._probe_all_added(path)
        self._dir_cache[key] = res
        return res

    def _probe_all_added(self, path: str) -> bool:
        has_file = False
        try:
            with os.scandir(path) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:
                        if not e.is_file():
                            continue
                    except OSError:
                        continue
                    if _kind_of(e.path) == "other":
                        continue           # 只看会被加进列表的图片/视频
                    has_file = True
                    if _norm_key(e.path) not in self.hidden:
                        return False       # 还有没加的 → 这层不算空
        except OSError:
            return False
        if not has_file:
            return False                   # 没有直接文件 → 不下判断（可能只有子文件夹）
        if not self._recursive():
            return True
        # 开了「含子文件夹」：还要确认整棵子树里也没有漏的
        return self._tree_all_added(path)

    def _tree_all_added(self, path: str) -> bool:
        n = 0
        try:
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for f in files:
                    if f.startswith("."):
                        continue
                    p = os.path.join(root, f)
                    if _kind_of(p) == "other":
                        continue
                    n += 1
                    if n > self._WALK_LIMIT or _norm_key(p) not in self.hidden:
                        return False
        except OSError:
            return False
        return True

    def _recursive(self) -> bool:
        try:
            return bool(self.recursive_getter and self.recursive_getter())
        except Exception:
            return False

    def _dim_dir(self, index) -> str:
        src = self.sourceModel()
        if src is None:
            return ""
        try:
            si = self.mapToSource(index) if index.model() is self else index
        except Exception:
            return ""
        try:
            if not src.isDir(si):      # 用模型缓存判目录，比 os.path.isdir 省一次 stat
                return ""
        except Exception:
            return ""
        path = src.filePath(si)
        if not path:
            return ""
        return path if self.all_added(path) else ""

    def filterAcceptsRow(self, row, parent):
        src = self.sourceModel()
        if src is None:
            return True
        idx = src.index(row, 0, parent)
        if not idx.isValid():
            return True
        path = src.filePath(idx)
        if not path or os.path.isdir(path):
            return True          # 目录/未加载的行一律放行
        return _norm_key(path) not in self.hidden

    # ---------------------------------------------------------------- 灰掉目录
    def flags(self, index):
        # v141: 以前这里把"里面已经全部添加过"的目录设成**不可选中**, 于是
        # 用户想再把这个文件夹加一次时怎么也点不动 —— 看着像"文件丢了还加不回来"。
        # 现在只保留灰字提示, 照样能选、能双击进去看; 真加进去时由主窗口去重。
        return super().flags(index)

    def data(self, index, role=Qt.DisplayRole):
        if role == ROLE_DIM:
            return bool(self._dim_dir(index))
        if role == Qt.ForegroundRole and self._dim_dir(index):
            return QBrush(QColor(T.TEXT_WEAK))
        return super().data(index, role)

    def lessThan(self, left, right):
        """不自己排：完完整整沿用 QFileSystemModel 的顺序（它保证"文件夹在前"）。

        若交给 proxy 默认排序（按显示名字符串），目录会和文件混在一起，
        与"添加"弹窗一直以来的观感不符。
        """
        return left.row() < right.row()

    def sort(self, column, order=Qt.AscendingOrder):
        """点表头排序时转交给源模型，proxy 仍只做"跟随"而不重排。"""
        src = self.sourceModel()
        if src is not None and hasattr(src, "sort"):
            src.sort(column, order)
        else:
            super().sort(column, order)


def _human_size(size: int) -> str:
    """把字节数格式化成带单位的可读字符串（与 PickerDialog._fmt_size 一致）。"""
    if size is None or size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(size)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.1f} {units[i]}" if i else f"{int(v)} B"


class _ThumbTip(QWidget):
    """悬浮提示框：上面一张缩略图，中间文件名，下面文件大小（取代原来只显示路径的纯文字提示）。"""

    def __init__(self):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setObjectName("thumbTip")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        self.img = QLabel()
        self.img.setAlignment(Qt.AlignCenter)
        self.img.setFixedSize(196, 150)
        lay.addWidget(self.img)
        self.name = QLabel()
        self.name.setAlignment(Qt.AlignCenter)
        self.name.setWordWrap(True)
        self.name.setMaximumWidth(196)
        lay.addWidget(self.name)
        self.size_lbl = QLabel()
        self.size_lbl.setAlignment(Qt.AlignCenter)
        self.size_lbl.setMaximumWidth(196)
        self.size_lbl.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 11px;")
        self.size_lbl.setVisible(False)
        lay.addWidget(self.size_lbl)
        self.setStyleSheet(
            f"QWidget#thumbTip {{ background: {T.SURFACE};"
            f" border: 1px solid {T.BORDER_STRONG}; border-radius: 8px; }}"
            f"QWidget#thumbTip QLabel {{ background: transparent; border: none;"
            f" color: {T.TEXT}; font-size: 12px; }}")

    def show_for(self, path: str, global_pos=None):
        if not path:
            return
        self.name.setText(os.path.basename(path) or path)
        # 文件大小一行（仅文件显示，文件夹不显示）
        if os.path.isfile(path):
            try:
                self.size_lbl.setText(_human_size(os.path.getsize(path)))
                self.size_lbl.setVisible(True)
            except OSError:
                self.size_lbl.setVisible(False)
        else:
            self.size_lbl.setVisible(False)
        pm = None
        if os.path.isfile(path):
            try:
                from ..core.thumbs import thumb_pixmap
                pm = thumb_pixmap(path)
            except Exception:
                pm = None
        if pm is None or pm.isNull():
            self.img.setPixmap(QPixmap())
            self.img.setText("无预览" if os.path.isfile(path) else "文件夹")
            self.img.setStyleSheet(f"color: {T.TEXT_WEAK};")
        else:
            self.img.setStyleSheet("")
            self.img.setPixmap(pm.scaled(196, 150, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))
        self.adjustSize()
        pos = global_pos if global_pos is not None else QCursor.pos()
        scr = QApplication.screenAt(pos) or QApplication.primaryScreen()
        x, y = pos.x() + 16, pos.y() + 16
        if scr is not None:
            geo = scr.availableGeometry()
            if x + self.width() > geo.right():
                x = pos.x() - self.width() - 12
            if y + self.height() > geo.bottom():
                y = pos.y() - self.height() - 12
            x = max(geo.left(), x)
            y = max(geo.top(), y)
        self.move(x, y)
        self.show()
        self.raise_()


class _HoverCursorFilter(QObject):
    """v224: 树/列表**空白处箭头、条目上小手**。

    全局光标过滤器(cursor.py)给 QAbstractItemView 的**整个视口**染小手,
    导航树/预览列表一大片空白也全是抓手 —— 与本弹窗的资源管理器口径不符。
    挂本过滤器的视图要同时设 `handCursor=False` 豁免全局染色, 光标改由
    "悬停位置有没有条目"逐点判定。
    """

    def eventFilter(self, obj, ev):
        v = obj.parent()
        et = ev.type()
        if et == QEvent.Type.MouseMove:
            try:
                on = v.indexAt(ev.position().toPoint()).isValid()
            except RuntimeError:
                return False
            v.setCursor(Qt.PointingHandCursor if on else Qt.ArrowCursor)
        elif et == QEvent.Type.Leave:
            v.unsetCursor()
        return False


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
        # v224: 别让全局过滤器整块染小手 —— 光标由 _sync_hover 按悬停位置逐点判
        self.setProperty("handCursor", False)

    def mousePressEvent(self, event):
        if self._mq_press(event):
            return                     # 空白处按下 = 自己拉框(选择也自己管)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._mq_move(event):
            return                     # 左键按下期间全归我们, 不让 Qt 起自己的橡皮筋
        self._sync_hover(event.position().toPoint())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._sync_hover(None)         # 移出列表: 悬停高亮收掉
        super().leaveEvent(event)

    def viewportEvent(self, event):
        # 移到滚动条/边框上时拿到的是**视口**的 Leave(控件本身的 leaveEvent 不响),
        # 悬停高亮也得跟着收, 否则那一格会一直亮着。
        if event.type() == QEvent.Type.Leave:
            self._sync_hover(None)
        return super().viewportEvent(event)

    def mouseReleaseEvent(self, event):
        self._mq_release()
        super().mouseReleaseEvent(event)

    # ---- v220: 悬停高亮(自绘代理不认 QSS 的 :hover, 由视图把目标喂给它) ----
    def _sync_hover(self, pos):
        path = ""
        on_item = False
        if pos is not None:
            idx = self.indexAt(pos)
            if idx.isValid():
                on_item = True
                path = idx.data(QFileSystemModel.FilePathRole) or ""
        d = self.itemDelegate()
        if hasattr(d, "set_hover_path"):
            d.set_hover_path(path)
        # v224: 文件上小手、空白处箭头(全局过滤器原来给整个视口染小手)
        self.setCursor(Qt.PointingHandCursor if on_item else Qt.ArrowCursor)

    def wheelEvent(self, event):
        if self._mq_wheel(event):
            return
        super().wheelEvent(event)
        self._mq_after_wheel()
        # 滚动后光标底下换成了别的文件: 按视口坐标重算一次
        self._sync_hover(self.viewport().mapFromGlobal(QCursor.pos()))


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
        # 必须开鼠标跟踪: 关着的时候"没按键的移动"根本送不到视图,
        # 万一松手事件丢了(在窗外松手), 就没机会把残留的框收掉。
        self.setMouseTracking(True)
        # v224: 别让全局过滤器整块染小手 —— 空白处箭头、文件行上小手(见 mouseMoveEvent)
        self.setProperty("handCursor", False)

    def mousePressEvent(self, event):
        if self._mq_press(event):
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._mq_move(event):
            return
        self.setCursor(Qt.PointingHandCursor
                       if self.indexAt(event.position().toPoint()).isValid()
                       else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._mq_release()
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._mq_wheel(event):
            return
        super().wheelEvent(event)
        self._mq_after_wheel()


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
        # 视频/HEIC/RAW 的重解码不能放在 paint 里同步做(见 _pixmap 注释)。
        # worker 由 PickerDialog 注入; 未注入时退回同步, 保证仍能出图。
        self.worker = None
        self._pending: set = set()   # 已排进后台队列、还没回填的路径
        self._folder_icon = None     # 纯文件夹图标(只向系统取一次后缓存)
        # v220: 鼠标所在的路径(悬停高亮用)。这里按**路径**而不是 QModelIndex 记:
        # 模型外面还套着过滤代理, index 在排序/刷新后容易失效, 路径永远稳。
        self.hover_path = ""

    def set_hover_path(self, path: str):
        """视图把"鼠标当前在哪个文件上"喂进来; 只在真的换了目标时重绘。"""
        path = path or ""
        if path == self.hover_path:
            return
        self.hover_path = path
        v = self.parent()
        if isinstance(v, QAbstractItemView):
            v.viewport().update()

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
        # 文件夹: 不生成/不排队缩略图(避免 Windows 把文件夹图标渲染成内含图片的
        # "随机缩略图")；统一用纯文件夹图标, 见 paint()。
        if path and os.path.isdir(path):
            return None
        if path in self._miss:
            return None
        pm = self._cache.get(path)
        if pm is not None:
            return pm
        ext = os.path.splitext(path)[1].lower()
        # 分三条路:
        #   1) jpg/png 等常用图且缩略图够大: QImageReader 直读, 同步最快;
        #   2) 视频 / HEIC / RAW: 先查磁盘缓存(命中就几毫秒); 没有则交给后台线程解码,
        #      本次先返回 None(画系统图标), 解码完再回填刷新。
        #      —— 绝不能在这里同步调 ffmpeg: 一个装几百个视频的文件夹会逐个抽帧,
        #         每帧几十~几百毫秒, 界面就被"加载缩略图"卡死。
        #   3) 没注入后台线程时(理论上不会): 退回同步, 保证仍能出图。
        if ext in self.IMG_EXT and self.thumb >= 48:
            pm = self._load(path)
        else:
            pm = self._disk_cached(path)
            if pm is None:
                if self.worker is not None:
                    if path not in self._pending:
                        self._pending.add(path)
                        self.worker.request([path], front=False)
                    return None
                pm = self._load_heavy(path)
        if pm is None or pm.isNull():
            self._miss.add(path)
            return None
        if len(self._cache) > 600:
            self._cache.clear()
        self._cache[path] = pm
        return pm

    def _disk_cached(self, path: str):
        """只查磁盘缩略图缓存(不解码): 命中时读一张 360px jpg, 通常几毫秒。"""
        try:
            from ..core.thumbs import cache_dir, cache_key
            dest = os.path.join(cache_dir(), cache_key(path) + ".jpg")
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                pm = QPixmap(dest)
                if not pm.isNull():
                    return pm
        except Exception:
            pass
        return None

    def accept_thumb(self, path: str, pm):
        """后台线程解码完成 → 主线程回填缓存(下一帧 paint 就能画出来)。"""
        self._pending.discard(path)
        if pm is None or pm.isNull():
            self._miss.add(path)
            return
        self._miss.discard(path)
        if len(self._cache) > 600:
            self._cache.clear()
        self._cache[path] = pm

    def accept_fail(self, path: str):
        """后台解码失败: 记入 _miss, 不再反复排队(否则坏文件会被无限重试)。"""
        self._pending.discard(path)
        self._miss.add(path)

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
        # v220: 悬停 = 浅蓝底板(资源管理器手感); 选中 = 更深一档, 一划一选分得清
        hovered = (not selected and bool(self.hover_path)
                   and path == self.hover_path)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        if selected or hovered:
            box = QRect(r).adjusted(2, 2, -2, -2)
            p = QPainterPath()
            p.addRoundedRect(QRectF(box), 7, 7)
            if selected:
                painter.fillPath(p, QBrush(QColor(T.SEL_SOFT)))
                painter.setPen(QPen(QColor(T.ACCENT_BORDER), 1.2))
                painter.setBrush(Qt.NoBrush)
                painter.drawPath(p)
            else:
                painter.fillPath(p, QBrush(QColor(T.HOVER_SOFT)))

        name = index.data(Qt.DisplayRole) or ""
        is_dir = bool(path) and os.path.isdir(path)
        dim = bool(index.data(ROLE_DIM))      # 目录里的文件已经全加过了 → 画灰
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

        # 文件夹: 一律用纯文件夹图标(不向系统取"含缩略图预览"的动态图标,
        # 否则文件夹内若有图片, Windows 会把它渲染成随机缩略图)。
        pm = None if is_dir else self._pixmap(path)
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
            icon = self._folder_icon_cached() if is_dir else index.data(Qt.DecorationRole)
            if isinstance(icon, QIcon):
                if dim:
                    painter.setOpacity(0.4)   # 灰掉的目录：图标也一起变淡
                icon.paint(painter, icon_rect, Qt.AlignCenter)
                painter.setOpacity(1.0)

        painter.setPen(QColor(T.TEXT_WEAK if dim else T.TEXT))
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

    def _folder_icon_cached(self):
        """纯文件夹图标(只向系统取一次后缓存), 用于代替系统可能"内含缩略图"的动态文件夹图标。"""
        if self._folder_icon is None:
            self._folder_icon = QFileIconProvider().icon(QFileIconProvider.Folder)
        return self._folder_icon


class _PanImage(QWidget):
    """可拖动查看的预览图: 默认按比例完整显示(contain), 鼠标按住拖动平移,
    滚轮以光标为中心缩放, 便于在较小的预览窗里查看完整照片。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._src = QPixmap()
        self._scale = 1.0
        self._off = QPoint(0, 0)
        self._placeholder = ""
        self._dragging = False
        self._last = QPoint()
        self.setMinimumHeight(140)
        # v224: 有图可拖才显示抓手, 空白占位时是普通箭头
        self.setCursor(Qt.ArrowCursor)
        self.setStyleSheet(f"background: {T.THUMB_BG}; border-radius: 8px;")

    # ---- 对外接口(兼容原 QLabel 用法) ----
    def set_source(self, pm):
        self._src = pm if pm is not None and not (hasattr(pm, "isNull") and pm.isNull()) else QPixmap()
        self.setCursor(Qt.OpenHandCursor if not self._src.isNull() else Qt.ArrowCursor)
        self.refit()

    def set_placeholder(self, text):
        self._placeholder = text or ""
        self.update()

    def setText(self, text):       # 兼容 QLabel.setText
        self.set_placeholder(text)

    def clear(self):               # 兼容 QLabel.clear
        self._src = QPixmap()
        self.update()

    def refit(self):
        """重新按 contain 完整显示并居中(重置缩放/平移)。"""
        if self._src.isNull():
            self.update()
            return
        aw, ah = self.width(), self.height()
        if aw <= 0 or ah <= 0:
            self.update()
            return
        sw, sh = self._src.width(), self._src.height()
        if sw <= 0 or sh <= 0:
            self.update()
            return
        self._scale = min(aw / sw, ah / sh)
        self._center()
        self.update()

    def _center(self):
        sw = self._src.width() * self._scale
        sh = self._src.height() * self._scale
        self._off = QPoint(int((self.width() - sw) / 2), int((self.height() - sh) / 2))

    def _clamp(self):
        if self._src.isNull():
            return
        sw = self._src.width() * self._scale
        sh = self._src.height() * self._scale
        if sw <= self.width():
            self._off.setX(int((self.width() - sw) / 2))
        else:
            self._off.setX(min(0, max(self.width() - sw, self._off.x())))
        if sh <= self.height():
            self._off.setY(int((self.height() - sh) / 2))
        else:
            self._off.setY(min(0, max(self.height() - sh, self._off.y())))

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(T.THUMB_BG))
        if self._src.isNull():
            if self._placeholder:
                p.setPen(QColor(T.TEXT_WEAK))
                p.drawText(self.rect(), Qt.AlignCenter, self._placeholder)
            p.end()
            return
        pm = self._src.scaled(int(self._src.width() * self._scale),
                              int(self._src.height() * self._scale),
                              Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p.drawPixmap(self._off, pm)
        p.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._src.isNull():
            self.refit()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._src.isNull():
            self._dragging = True
            self._last = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._off += event.position().toPoint() - self._last
            self._last = event.position().toPoint()
            self._clamp()
            self.update()
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            self.setCursor(Qt.OpenHandCursor)
            event.accept()

    def wheelEvent(self, event):
        if self._src.isNull():
            return
        factor = 1.12 if event.angleDelta().y() > 0 else 1 / 1.12
        new_scale = max(0.05, min(8.0, self._scale * factor))
        # 以光标为锚点缩放: 光标下的图片坐标缩放前后保持不变
        cx, cy = event.position().x(), event.position().y()
        px = (cx - self._off.x()) / self._scale
        py = (cy - self._off.y()) / self._scale
        self._scale = new_scale
        self._off = QPoint(int(cx - px * self._scale), int(cy - py * self._scale))
        self._clamp()
        self.update()
        event.accept()


class _ClickableLabel(QLabel):
    """可点击的标题标签: 鼠标悬停显示手型光标, 单击时发出 clicked 信号。

    用于预览区顶部文件名——点击后按系统默认关联程序打开该文件
    (图片→看图工具, 视频→播放器, 文件夹→资源管理器)。
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()


class _TreeTextDelegate(QStyledItemDelegate):
    """详细信息(树)视图专用: 仅改写显示文字, 不改变模型数据/路径/排序。

    - 名称列(0): 仅对"文件"去掉扩展名(文件夹保留)。
    - 类型列(2): 去掉前导的 video/视频 字样(如 "video/mp4" -> "mp4")。
    """

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        col = index.column()
        if col == 0:
            info = self._file_info(index)
            if info.isFile():
                base, _ = os.path.splitext(option.text)
                option.text = base
        elif col == 2:
            s = re.sub(r'^(video|影片|视频)\b[\s:：/-]*', '', option.text, flags=re.I).strip()
            if s:
                option.text = s

    @staticmethod
    def _file_info(index):
        """取 QFileInfo：模型可能是过滤代理，得先映射回 QFileSystemModel。"""
        m = index.model()
        try:
            if not hasattr(m, "fileInfo") and hasattr(m, "sourceModel"):
                src = m.sourceModel()
                if src is not None and hasattr(src, "fileInfo"):
                    return src.fileInfo(m.mapToSource(index))
            return m.fileInfo(index)
        except Exception:
            return QFileInfo()


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
    """路径输入框，行为仿 Windows 资源管理器地址栏（且不显示闪烁竖线）。

    - 点路径中间某一层（如 "Users"）：直接跳到那一层
    - 点右侧空白处：全选整条路径，可直接粘贴/改写新路径
    - 鼠标悬停某一层：该层淡淡高亮 + 手型光标，提示"这里可以点"
    """

    _SEPS = "\\/"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setClearButtonEnabled(False)
        # 不显示闪烁的输入光标竖线（cursorWidth 属性在部分 PySide6 版本未暴露 setCursorWidth 方法，用 setProperty 兜底）
        self.setProperty("cursorWidth", 0)
        # 不按键也要收到 mouseMoveEvent，才能做悬停高亮
        self.setMouseTracking(True)
        self._selected_once = False
        self._on_seg_click = None     # 点了某一层时的回调，参数=那一层的路径
        self._hover = None            # 当前悬停的字符区间 (start, end)

    # ---- 分段：把一个点击位置换算成"路径里的哪一层" ----
    def _seg_span_at(self, pos):
        """pos(控件内坐标) 落在哪一段路径上；落在文字右侧空白处返回 None。"""
        text = self.text()
        n = len(text)
        if not text:
            return None
        idx = self.cursorPositionAt(pos)
        if idx >= n:
            return None                       # 文字之后的空白 → 交给"全选"
        if text[idx] in self._SEPS:           # 点在分隔符上：算它右边那一层
            while idx < n and text[idx] in self._SEPS:
                idx += 1
            if idx >= n:
                return None
        start = idx
        while start > 0 and text[start - 1] not in self._SEPS:
            start -= 1
        end = idx
        while end < n and text[end] not in self._SEPS:
            end += 1
        return (start, end)

    def _target_of(self, span) -> str:
        """该层对应的目录路径：段结尾之前的部分；"C:" 要补成 "C:\\"。"""
        prefix = self.text()[:span[1]]
        if prefix.endswith(":"):
            prefix += os.sep
        return prefix

    # ---- 鼠标 ----
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._on_seg_click is not None:
            span = self._seg_span_at(event.position().toPoint())
            if span is not None:
                self._on_seg_click(self._target_of(span))
                return                        # 点的是路径层：只跳转，不进入编辑态
        super().mousePressEvent(event)
        self.selectAll()
        self._selected_once = True

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        span = (self._seg_span_at(event.position().toPoint())
                if self._on_seg_click is not None else None)
        if span != self._hover:
            self._hover = span
            self.setCursor(Qt.CursorShape.PointingHandCursor if span
                           else Qt.CursorShape.IBeamCursor)
            self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        if self._hover is not None:
            self._hover = None
            self.unsetCursor()
            self.update()

    def _x_at(self, idx: int):
        """字符 idx 左缘的 x 坐标：二分 + cursorPositionAt 反查。

        不用 cursorRect(idx)——本机 PySide6 没绑定带参数的重载；
        也不用 fontMetrics 硬算——那样得自己补 QSS padding 与文本滚动偏移。
        cursorPositionAt 是 Qt 自己的映射，含 padding/滚动，反查最准。
        """
        y = self.contentsRect().center().y()
        hi = max(self.width() - 1, 0)
        if self.cursorPositionAt(QPoint(hi, y)) < idx:
            return None                       # 该处被裁到框外（长路径滚动）
        lo = 0
        while lo < hi:                        # 找第一个 cursorPosition >= idx 的 x
            mid = (lo + hi) // 2
            if self.cursorPositionAt(QPoint(mid, y)) >= idx:
                hi = mid
            else:
                lo = mid + 1
        return lo

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._hover:
            return
        s, e = self._hover
        x1 = self._x_at(s)
        x2 = self._x_at(e)
        if x1 is None or x2 is None or x2 <= x1:
            return
        r = self.contentsRect()
        rect = QRect(x1 - 2, r.top() + 1, (x2 - x1) + 4, r.height() - 2)
        rect = rect.intersected(self.rect().adjusted(0, 0, -1, -1))
        if rect.width() <= 0 or rect.height() <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        c = QColor(T.ACCENT)
        c.setAlpha(30)                        # 很淡的蓝，压在字上也不影响读
        p.setBrush(c)
        p.drawRoundedRect(QRectF(rect), 4, 4)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if not self._selected_once:
            self.selectAll()


class _PathBar(QWidget):
    """路径输入框（常驻）：一直显示为可编辑的输入框，展示完整路径。

    交互仿 Windows 资源管理器地址栏：
    - 点路径中间某一层：直接跳到那一层
    - 点右侧空白：全选整条路径（便于复制或整条替换）
    - 回车：跳转到输入的路径；路径不存在则提示并还原
    - 失焦：文本被改动过但没回车，则还原为当前真实路径，避免框里显示的不是实际位置
    """

    def __init__(self, on_navigate, parent=None):
        super().__init__(parent)
        self._on_navigate = on_navigate
        self._path = ""

        self._edit = _PathLineEdit()
        self._edit.setPlaceholderText("输入或粘贴文件夹路径，回车跳转")
        self._edit._on_seg_click = self._on_seg_click
        self._edit.returnPressed.connect(self._on_edit_return)
        self._edit.editingFinished.connect(self._on_edit_finished)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._edit, 1)

        # 高度"按内容定"、不参与纵向拉伸。
        # 原因：普通 QWidget 默认纵向可增长，放进顶部那一行后会跟下面的文件区平分富余高度，
        # 导致整条工具栏被撑到两三百像素（输入框看起来高得离谱、文件区被压扁）。
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    # ---- 外部接口 ----
    def set_path(self, path: str):
        self._path = path
        self._edit.setText(path)
        # 光标归零：长路径时框里露出的是最前面的盘符与首段，而不是被尾部挤出视野
        self._edit.setCursorPosition(0)

    # ---- 内部 ----
    def _on_seg_click(self, target: str):
        """点了路径里的某一层：跳过去。点错/不存在就静默忽略，不弹窗打断浏览。"""
        if not target or not os.path.isdir(target):
            return
        if os.path.normcase(target) == os.path.normcase(self._path):
            return                             # 点的就是当前目录
        self._edit.deselect()
        self._on_navigate(target)

    def _on_edit_return(self):
        p = self._edit.text().strip().strip('"')
        if p and os.path.isdir(p):
            self._on_navigate(p)
        else:
            QMessageBox.information(self, "路径不存在", f"找不到这个位置：{p}")
            self.set_path(self._path)

    def _on_edit_finished(self):
        # 回车跳转成功后文本已与 _path 一致，这里不会误还原
        if self._edit.text() != self._path:
            self.set_path(self._path)


class PickerDialog(QDialog):
    # 已经在弹窗里「加进去」的条目（Enter / 右键添加 立即生效，弹窗不关）
    pathsAdded = Signal(list)

    def __init__(self, parent=None, start_path: str = None, recursive: bool = False,
                 existing=None):
        super().__init__(parent)
        self._recursive = recursive
        # existing: 主列表里已有的路径 —— 打开时就不显示，免得重复挑
        self._known: set[str] = {_norm_key(p) for p in (existing or [])}
        self._added: set[str] = set()      # 本次弹窗里已经提交过的
        self._thumb_tip = None
        # 悬浮卡片的自检定时器: 鼠标一离开列表就把它收掉(见 _check_thumb_tip)
        self._tip_timer = QTimer(self)
        self._tip_timer.setInterval(120)
        self._tip_timer.timeout.connect(self._check_thumb_tip)
        self._hover_item = None      # 右侧预览列表当前悬浮高亮的项
        self._selected_item = None    # 右侧预览列表被双击"选中"(更深蓝)的项
        self._prop_dialogs: list = []      # 弹窗里开的"属性"窗口(非模态, 自己持有引用)
        self._cmp_dialogs: list = []       # 弹窗里开的"对比"窗口(同上)
        self._cmp_workers: list = []       # 对比时后台读视频规格的线程(没真停前不许丢引用)
        self.setWindowTitle("添加照片（文件夹或文件）")
        self.setMinimumSize(1080, 620)
        self.resize(1200, 700)

        self._model = None
        self._proxy = _HideAddedProxy(self)
        self._proxy.recursive_getter = lambda: self.recursive()
        self._proxy.set_hidden(self._known)
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

        # 视频/HEIC/RAW 的缩略图交给后台线程解码: paint 只查缓存, 未命中先画系统图标
        # 并排队, 解码完再批量刷新。这样打开"装着几百个视频"的文件夹不会逐个抽帧卡死。
        from ..core.thumbs import ThumbWorker
        self._thumb_worker = ThumbWorker(self)
        self.list_delegate.worker = self._thumb_worker
        self._thumb_worker.thumbReady.connect(self._on_thumb_ready)
        self._thumb_worker.thumbFailed.connect(self._on_thumb_failed)
        self._thumb_worker.start()
        # 刷新节流: 后台可能一秒回填好几张, 攒 40ms 统一重绘一次, 避免整屏反复刷新。
        self._thumb_refresh = QTimer(self)
        self._thumb_refresh.setSingleShot(True)
        self._thumb_refresh.setInterval(40)
        self._thumb_refresh.timeout.connect(self._flush_thumbs)

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

    # ---------------------------------------------------------------- 缩略图(后台)
    def _on_thumb_ready(self, path: str, pm):
        # 工作线程来的是 QImage(QPixmap 不能跨线程), 这里换成 QPixmap 再进缓存
        if isinstance(pm, QImage):
            pm = QPixmap.fromImage(pm)
        self.list_delegate.accept_thumb(path, pm)
        if not self._thumb_refresh.isActive():
            self._thumb_refresh.start()

    def _on_thumb_failed(self, path: str):
        self.list_delegate.accept_fail(path)
        if not self._thumb_refresh.isActive():
            self._thumb_refresh.start()

    def _flush_thumbs(self):
        self.list_view.viewport().update()

    def stop_thumbs(self):
        """停掉后台解码线程(关闭/确认时调用, 避免线程带着 ffmpeg 子进程一起被析构)。"""
        worker = getattr(self, "_thumb_worker", None)
        if worker is None:
            return
        try:
            worker.stop()
            worker.wait(2500)
        except Exception:
            pass

    def _cleanup(self):
        self._hide_thumb_tip()
        for dlg in list(getattr(self, "_prop_dialogs", [])):
            try:
                dlg.close()
            except Exception:
                pass
        for dlg in list(getattr(self, "_cmp_dialogs", [])):
            try:
                dlg.close()
            except Exception:
                pass
        # 对比的探测线程必须真停了才能随弹窗一起析构
        for w in list(getattr(self, "_cmp_workers", [])):
            try:
                w.stop()
                w.wait(2000)
            except Exception:
                pass

    def closeEvent(self, event):
        self._cleanup()
        self.stop_thumbs()
        super().closeEvent(event)

    def done(self, result: int):
        self._cleanup()
        self.stop_thumbs()
        super().done(result)

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
        self.back_btn.setCursor(Qt.PointingHandCursor)   # 可点的东西一律抓手
        self.back_btn.clicked.connect(self._on_back)
        self.fwd_btn = QPushButton()
        self.fwd_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward))
        self.fwd_btn.setFixedSize(34, 28)
        self.fwd_btn.setToolTip("前进（下一个位置）")
        self.fwd_btn.setCursor(Qt.PointingHandCursor)
        self.fwd_btn.clicked.connect(self._on_forward)
        top.addWidget(self.back_btn)
        top.addWidget(self.fwd_btn)
        self.path_edit = _PathBar(self._navigate)
        # "常驻"：给路径框一个最小宽度，窗口缩到最小时也不会被右侧控件挤扁到看不清路径。
        self.path_edit.setMinimumWidth(PATH_MIN_W)
        top.addWidget(self.path_edit, 1)

        # 文件类型 + 含子文件夹 + 添加：原在底部一行，现整体上移到顶部「显示方式」左侧。
        # 底部不再放控件，让文件浏览区直接占满整个高度（多显示一行）。
        # 「文件类型：」文字标签已删（按下拉框内容自明），省下的宽度全部让给路径框。
        self.type_combo = _DownComboBox()
        self.type_combo.view().setMouseTracking(True)  # 弹出项要能显示悬浮高亮
        self.type_combo.setItemDelegate(_CenterAlignDelegate(self.type_combo))
        self.type_combo.addItem("全部文件 (*.*)", "*")
        self.type_combo.addItem("图片 (*.jpg *.jpeg *.png *.heic *.bmp *.gif *.tif *.tiff *.webp *.cr2 *.cr3 *.nef *.arw)", "image")
        self.type_combo.addItem("视频 (*.mp4 *.mov *.avi *.mkv *.mts *.m2ts *.wmv *.flv *.mpg *.mpeg)", "video")
        # 默认"全部"：同时显示文件夹与文件，方便浏览与双击进入子目录。
        self.type_combo.setCurrentIndex(0)
        # 挪到顶部后空间紧张：固定 150px（关闭态"全部文件 (*.*)"可完整显示，长选项由控件省略）。
        # 比之前的 160 再让出 10px 给路径框，保证路径框在最小窗口下也守得住最小宽度。
        self.type_combo.setFixedWidth(150)
        self.type_combo.setCursor(Qt.PointingHandCursor)
        self.type_combo.currentIndexChanged.connect(self._apply_filter)
        top.addWidget(self.type_combo)

        # 「含子文件夹」开关（文件类型下拉右侧）
        self.recursive_check = CheckCheckBox("含子文件夹")
        self.recursive_check.setToolTip("勾选后会一并读取子文件夹里的照片")
        self.recursive_check.setCursor(Qt.PointingHandCursor)
        self.recursive_check.setChecked(self._recursive)
        # 开关一变，"目录是否已全部加入"的结论也跟着变（递归与否看的范围不同）
        self.recursive_check.toggled.connect(self._on_recursive_toggled)
        top.addWidget(self.recursive_check)

        # 「添加」主按钮：主题蓝
        self.ok_btn = QPushButton("添加")
        self.ok_btn.setFixedWidth(90)
        self.ok_btn.setProperty("accent", "true")
        self.ok_btn.setCursor(Qt.PointingHandCursor)
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self._on_accept)
        top.addWidget(self.ok_btn)
        # 显示方式：关闭态与下拉选项文字都居中。
        # 关闭态居中沿用 dialogs._CenterCombo（字画在整框几何中心，下拉小三角不占布局宽度）；
        # 展开选项居中用 _CenterAlignDelegate（只强制 displayAlignment，渲染仍走基类完整链路）。
        self.view_combo = _CenterComboArrow()
        self.view_combo.setObjectName("viewCombo")
        self.view_combo.setToolTip("切换显示方式（也可在文件区按住 Ctrl 滚动滚轮）")
        self.view_combo.setCursor(Qt.PointingHandCursor)
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
        # v224: 导航树/预览列表共用一个"空白处箭头、条目上小手"过滤器(见 _HoverCursorFilter)
        self._hover_cursor_filter = _HoverCursorFilter(self)

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
        self.nav_tree.setProperty("handCursor", False)
        self.nav_tree.viewport().installEventFilter(self._hover_cursor_filter)
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
        # 图标档(超大/大/中/小图标): 文件名被省略号截断时, 悬浮显示完整文件名
        self.list_view.setMouseTracking(True)
        self.list_view.entered.connect(self._on_list_entered)
        self.list_view.viewportEntered.connect(lambda: self.list_view.setToolTip(""))

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
        # 详细信息视图: 名称去后缀、类型去 "video" 前缀(仅改显示, 不影响选择/路径/排序)
        self.tree.setItemDelegate(_TreeTextDelegate(self.tree))

        # 右键菜单（添加 / 对比 / 属性）+ 回车直接加入（加入后弹窗不关闭）
        for _v in (self.list_view, self.tree):
            _v.setContextMenuPolicy(Qt.CustomContextMenu)
            _v.customContextMenuRequested.connect(
                lambda pos, v=_v: self._show_file_menu(v, pos))
            _v.installEventFilter(self)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.list_view)
        self.stack.addWidget(self.tree)
        mid.addWidget(self.stack, 1)
        right_col.addLayout(mid, 1)   # 明示：富余高度全部给文件浏览区

        # 原底部那一行（文件类型 / 含子文件夹 / 添加）已整体上移到顶部工具行，
        # 这里不再保留底栏 —— 文件浏览区(mid, stretch=1)直接占满整列高度。

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
        self.preview_title = _ClickableLabel("预览")
        self.preview_title.setProperty("h2", True)
        self.preview_title.setWordWrap(True)
        self.preview_title.clicked.connect(self._on_preview_title_clicked)
        self._preview_path = None
        pv.addWidget(self.preview_title)
        self.preview_image = _PanImage()
        pv.addWidget(self.preview_image, 1)
        self.preview_list = QListWidget()
        self.preview_list.setSelectionMode(QListWidget.NoSelection)
        self.preview_list.setFocusPolicy(Qt.NoFocus)
        # 显式给浅色底: 无论主题/ hover 状态如何, 列表背景始终是浅色(不会变黑)。
        # 注意: 不要写 ``QListWidget::item { ... }`` 样式, 否则 Qt 会忽略 item.setBackground(),
        # 蓝底就画不出来(改用 _PreviewDelegate 自绘圆角底)。
        self.preview_list.setStyleSheet(
            "QListWidget {"
            f"  background: {T.SURFACE};"
            f"  border: 1px solid {T.BORDER};"
            "  border-radius: 10px;"
            "  outline: none;"
            "}"
        )
        self.preview_list.setSpacing(2)
        self.preview_list.setItemDelegate(_PreviewDelegate())
        self.preview_list.setVisible(False)
        # 悬浮在列表里的某一项上: 弹出"缩略图在上、文件名在下"的小卡片(取代原来的纯路径提示)
        self.preview_list.setMouseTracking(True)
        self.preview_list.itemEntered.connect(self._on_preview_item_entered)
        self.preview_list.viewportEntered.connect(self._on_preview_viewport_left)
        # ⚠️ viewportEntered 是"鼠标**进入**列表"的信号, 鼠标离开时它不发 ——
        #    以前只连了它, 于是缩略图挂上去以后经常不消失。真正的"离开"要靠
        #    viewport 的 Leave 事件(装个事件过滤器), 再加一个定时自检兜底。
        self.preview_list.viewport().installEventFilter(self)
        self.preview_list.doubleClicked.connect(self._on_preview_double_click)
        self.preview_list.clicked.connect(self._on_preview_clicked)
        self.preview_list.setProperty("handCursor", False)
        self.preview_list.viewport().installEventFilter(self._hover_cursor_filter)
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
        """Ctrl+滚轮：在 超大/大/中/小图标 四档之间步进（其余模式先跳到"中图标"）。"""
        icon_modes = [m[1] for m in VIEW_MODES[:4]]
        cur = self.view_combo.currentData()
        i = icon_modes.index(cur) if cur in icon_modes else 2
        i = max(0, min(len(icon_modes) - 1, i + (-1 if zoom_in else 1)))
        self.view_combo.setCurrentIndex(i)   # 触发 _apply_view_mode

    # ---------------------------------------------------------------- 右侧预览
    def _on_preview_selection(self, *_):
        self._render_preview()

    def _on_list_entered(self, index):
        """图标档: 鼠标悬浮在条目上时, 把完整文件名设为 tooltip(文件名被省略号截断时看全名)。"""
        name = index.data(Qt.DisplayRole)
        self.list_view.setToolTip(name if name else "")

    # ---------------------------------------------------------------- 路径换算
    def _path_of(self, idx) -> str:
        """视图里的索引 → 磁盘绝对路径（自动把代理索引映射回 QFileSystemModel）。"""
        try:
            if self._model is None or not idx.isValid():
                return ""
            src = idx
            if idx.model() is self._proxy:
                src = self._proxy.mapToSource(idx)
            return os.path.normpath(self._model.filePath(src))
        except Exception:
            return ""

    def _selected_paths(self, view=None) -> tuple[list[str], list[str]]:
        """当前选中项 → (文件夹列表, 文件列表)。列表视图/表格视图都适用。"""
        view = view or self.stack.currentWidget()
        sm = view.selectionModel() if view is not None else None
        folders: list[str] = []
        files: list[str] = []
        if sm is None:
            return folders, files
        for idx in sm.selectedIndexes():
            if idx.column() != 0:
                continue
            p = self._path_of(idx)
            if not p:
                continue
            if os.path.isdir(p):
                folders.append(p)
            elif os.path.isfile(p):
                files.append(p)
        # 去重（表格视图里同一行的多列会各给一个索引）
        return list(dict.fromkeys(folders)), list(dict.fromkeys(files))

    def _render_preview(self):
        """按当前选中项刷新右侧预览：文件夹→内容清单；文件→大图；其余→提示。"""
        view = self.stack.currentWidget()
        if self._model is None:
            return
        sm = view.selectionModel()
        idxs = [i for i in sm.selectedIndexes() if i.column() == 0] if sm else []
        paths = [p for p in (self._path_of(i) for i in idxs) if p]
        dirs = [p for p in paths if os.path.isdir(p)]
        files = [p for p in paths if os.path.isfile(p)]

        self.preview_image.set_source(None)
        self.preview_image.setVisible(False)
        self.preview_list.clear()
        self._hover_item = None
        self._selected_item = None
        self.preview_list.setVisible(False)
        self.preview_hint.setVisible(True)
        self._preview_src = None
        self._preview_path = None
        self.preview_title.setToolTip("")

        if len(paths) == 1 and dirs:
            self._show_folder_preview(dirs[0])
        elif len(paths) == 1 and files:
            self._show_file_preview(files[0])
        elif len(paths) > 1:
            self._show_multi_preview(dirs, files)
        else:
            self._show_preview_example()

    def _show_multi_preview(self, dirs: list, files: list):
        """框选多项: 右侧直接列出所选项的文件名, 不再只留一句"已选中 N 项"。

        旧做法把大图区隐藏、只把提示塞进底部 hint, 结果 hint 被布局挤到面板最底部,
        看起来像整体错位。改为用列表铺满预览区, 标题写明数量, 一眼就能核对选了哪些文件。
        """
        self.preview_title.setText(f"已选中 {len(dirs) + len(files)} 项")
        self.preview_list.setVisible(True)
        self.preview_hint.setVisible(False)
        self._preview_path = None
        self.preview_title.setToolTip("")
        # 文件夹在前、文件在后, 各自按名称排, 与左侧列表的阅读顺序一致。
        for p in sorted(dirs, key=lambda s: os.path.basename(s).lower()):
            self._add_preview_item(p)
        for p in sorted(files, key=lambda s: os.path.basename(s).lower()):
            self._add_preview_item(p)

    def _add_preview_item(self, path: str):
        """右侧列表加一项：显示名，完整路径存进 UserRole 供悬浮卡片取图。"""
        it = QListWidgetItem(os.path.basename(path) or path)
        it.setData(Qt.UserRole, os.path.normpath(path))
        self.preview_list.addItem(it)
        return it

    # ---------------------------------------------------------------- 悬浮卡片
    def _tip(self) -> "_ThumbTip":
        if self._thumb_tip is None:
            self._thumb_tip = _ThumbTip()
        return self._thumb_tip

    def _hide_thumb_tip(self):
        if self._thumb_tip is not None:
            self._thumb_tip.hide()
        timer = getattr(self, "_tip_timer", None)
        if timer is not None:
            timer.stop()

    def _check_thumb_tip(self):
        """兜底自检: 悬浮卡片是独立顶层窗口, 鼠标离开列表时它自己不会知道。

        每 120ms 看一眼: 鼠标还在这个列表里、还停在同一项上才留, 否则收起。
        (离开 viewport 有 Leave 事件兜着, 但"移到条目之间的空白 / 滚轮把条目滚走 /
         窗口失焦"这些路径不产生 Leave, 只靠事件就一定会遇到"缩略图挂着不走"。)
        """
        tip = self._thumb_tip
        if tip is None or not tip.isVisible():
            timer = getattr(self, "_tip_timer", None)
            if timer is not None:
                timer.stop()
            return
        pl = self.preview_list
        vp = pl.viewport()
        if not pl.isVisible() or not vp.isVisible() or not vp.underMouse():
            self._on_preview_viewport_left()
            return
        idx = pl.indexAt(vp.mapFromGlobal(QCursor.pos()))
        if not idx.isValid() or pl.item(idx.row()) is not self._hover_item:
            self._on_preview_viewport_left()

    def _paint_preview_item(self, it):
        """把某一项恢复到"非悬浮"应有的高亮态: 选中项=更深蓝, 其余=无高亮。

        ⚠️ 只能用 `_ROLE_HL` 存, **不要用 setBackground(QColor())**——无效色刷会让该项变黑。
        """
        if it is None:
            return
        it.setData(_ROLE_HL, "selected" if it is self._selected_item else None)

    def _on_preview_item_entered(self, item):
        path = item.data(Qt.UserRole) if item is not None else None
        if not path:
            self._hide_thumb_tip()
            return
        # 悬浮高亮: 先还原上一项(若是选中项则保持更深蓝), 再给当前项上浅蓝底。
        # 注意: 当前项若已是"选中"项, 不覆盖它的更深蓝, 避免悬浮时反而变浅。
        if self._hover_item is not None and self._hover_item is not item:
            self._paint_preview_item(self._hover_item)
        self._hover_item = item
        # 当前项若已是"选中"项, 不覆盖它的更深蓝, 避免悬浮时反而变浅。
        item.setData(_ROLE_HL, "selected" if item is self._selected_item else "hover")
        self._tip().show_for(path)
        self._tip_timer.start()      # 开始自检(鼠标一离开就收)

    def _on_preview_viewport_left(self):
        """鼠标离开列表区：收起悬浮卡片并清除悬浮高亮(选中项保留更深蓝)。"""
        self._hide_thumb_tip()
        if self._hover_item is not None:
            self._paint_preview_item(self._hover_item)
            self._hover_item = None

    def _mark_preview_selected(self, index: QModelIndex):
        """把 index 对应项标记为"选中"(更深蓝)，旧选中项还原。

        单击/双击共用。⚠️ 只用 `_ROLE_HL`，不碰 BackgroundRole(无效色刷会让项变黑)。
        """
        if not index.isValid():
            return
        item = self.preview_list.item(index.row())
        if item is None:
            return
        if self._selected_item is not None and self._selected_item is not item:
            self._selected_item.setData(_ROLE_HL, None)
        self._selected_item = item
        item.setData(_ROLE_HL, "selected")

    def _on_preview_clicked(self, index: QModelIndex):
        """右侧预览列表单击：把该项标成"选中"(更深蓝)，不打开文件(打开留给双击)。"""
        self._mark_preview_selected(index)

    def _on_preview_double_click(self, index: QModelIndex):
        """右侧预览列表双击：标成"选中"(更深蓝) 并用系统默认程序打开。"""
        if not index.isValid():
            return
        path = index.data(Qt.UserRole)
        if not path or not os.path.exists(path):
            return
        self._mark_preview_selected(index)
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.normpath(path)))

    def _on_preview_title_clicked(self):
        """点击预览区顶部文件名/文件夹名: 用系统默认关联程序打开。

        图片→默认看图工具, 视频→默认播放器, 文件夹→资源管理器。
        QDesktopServices.openUrl + file:// 是 Qt 调起系统默认程序的惯用做法。
        """
        path = getattr(self, "_preview_path", None)
        if not path or not os.path.exists(path):
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.normpath(path)))

    def _refit_preview(self):
        """窗口显示/缩放时, 让右侧预览图按当前尺寸重新完整显示(contain)。"""
        if not self.preview_image.isVisible():
            return
        self.preview_image.refit()

    def _show_preview_example(self):
        """未选中任何项时: 右侧预览区只显示一个空框（浅色占位，不画装饰示例图）。"""
        self.preview_title.setText("预览")
        self.preview_image.setVisible(True)
        self.preview_image.set_source(None)
        self.preview_image.set_placeholder("")
        self.preview_hint.setVisible(False)
        self._preview_src = None
        self._preview_path = None
        self.preview_title.setToolTip("")

    def _show_file_preview(self, path: str):
        self.preview_title.setText(os.path.basename(path))
        self.preview_image.setVisible(True)
        self.preview_hint.setVisible(False)
        self._preview_path = path
        self.preview_title.setToolTip("点击用系统默认程序打开（图片→看图工具 / 视频→播放器）")
        # 加载较大尺寸, 交给 _PanImage 按比例完整显示(contain), 小窗里也能看到整张图,
        # 还能按住拖动平移 / 滚轮缩放查看局部。
        pm = self._preview_pixmap(path, 1600)
        if pm is None or pm.isNull():
            self.preview_image.set_source(None)
            self.preview_image.set_placeholder("（无法预览该文件）")
            self._preview_src = None
            return
        self._preview_src = pm
        self.preview_image.set_source(pm)

    def _show_folder_preview(self, folder: str):
        name = os.path.basename(folder) or folder
        self.preview_list.setVisible(True)
        self.preview_hint.setVisible(False)
        self._preview_path = folder
        self.preview_title.setToolTip("点击在资源管理器中打开此文件夹")
        try:
            entries = list(os.scandir(folder))
        except OSError as ex:
            self.preview_list.setVisible(False)
            self.preview_hint.setVisible(True)
            self.preview_title.setText(name)
            self.preview_hint.setText(f"无法读取该文件夹：{ex}")
            self._preview_path = None
            self.preview_title.setToolTip("")
            return
        dirs = sorted([e for e in entries if e.is_dir()], key=lambda e: e.name.lower())
        files = sorted([e for e in entries if e.is_file()], key=lambda e: e.name.lower())
        self.preview_title.setText(f"{name}（{len(dirs) + len(files)} 项）")
        for e in dirs + files:
            it = QListWidgetItem(e.name)
            it.setData(Qt.UserRole, os.path.normpath(e.path))
            # 不再设原生 tooltip：避免与悬浮卡片（缩略图 + 文件名 + 大小）重复弹出
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
            # 视图统一挂过滤代理（已加进主列表的文件会被藏起来）；
            # 顺序仍由 QFileSystemModel 决定，见 _HideAddedProxy.lessThan。
            self._proxy.setSourceModel(self._model)
            self.tree.setModel(self._proxy)
            self.list_view.setModel(self._proxy)
            # 选中项变化 → 刷新右侧预览；Ctrl+滚轮 → 切换图标档位
            self.list_view.selectionModel().selectionChanged.connect(self._on_preview_selection)
            self.tree.selectionModel().selectionChanged.connect(self._on_preview_selection)
            self.list_view._on_zoom = self._zoom_step
            self.tree._on_zoom = self._zoom_step
        self._current = path
        self._model.setRootPath(path)
        root = self._proxy.mapFromSource(self._model.index(path))
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
        path = self._path_of(index)
        if not path:
            return
        if os.path.isdir(path):
            self._navigate(path)
        elif os.path.isfile(path):
            # 文件：用系统默认关联程序打开（图片→看图工具，视频→播放器）
            self._open_with_default(path)

    @staticmethod
    def _open_with_default(path: str):
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.normpath(path)))
        except Exception:
            pass

    # ---------------------------------------------------------------- 加入（不关闭弹窗）
    def _add_paths(self, folders: list[str], files: list[str]) -> int:
        """把选中的项交给主窗口，并把它们从浏览区里藏起来（弹窗保持打开）。"""
        folders = [f for f in (folders or []) if f and os.path.isdir(f)]
        files = [f for f in (files or []) if f and os.path.isfile(f)]
        if not folders and not files:
            return 0
        view = self.stack.currentWidget()
        sm = view.selectionModel() if view is not None else None
        if sm is not None:
            sm.clearSelection()          # 先清选中，免得过滤后残留悬空索引
        self._hide_thumb_tip()
        picked = list(folders) + list(files)
        # 文件夹要连带它里面的文件一起算"已加入"：否则浏览区看不出任何变化
        # （目录又不会被隐藏），用户会以为按回车没生效；同时这样目录才能立刻变灰。
        inside: list[str] = []
        for f in folders:
            inside.extend(self._files_under(f))
        self._added |= {_norm_key(p) for p in picked}
        self._added |= {_norm_key(p) for p in inside}
        self._proxy.add_hidden(picked + inside)
        if view is not None:
            view.viewport().update()     # 让刚变空的目录立刻变灰
        self._render_preview()
        self.pathsAdded.emit(picked)
        return len(picked)

    def _on_recursive_toggled(self, _on: bool):
        self._proxy._bump()
        self._proxy.invalidateFilter()
        view = self.stack.currentWidget() if hasattr(self, "stack") else None
        if view is not None:
            view.viewport().update()

    def _files_under(self, folder: str) -> list[str]:
        """文件夹里会被加入的图片/视频（跟随「含子文件夹」开关）。"""
        try:
            from ..core.scanner import collect_files
            return collect_files(folder, self.recursive())
        except Exception:
            return []

    def _add_selected(self) -> int:
        """回车 / 右键「添加」：把当前选中的项加进去，弹窗不关。"""
        folders, files = self._selected_paths()
        return self._add_paths(folders, files)

    def eventFilter(self, obj, event):
        """文件区里的回车 = 加入（不关闭弹窗）；右侧预览列表的鼠标离开 = 收起缩略图。"""
        if event.type() == QEvent.Type.KeyPress and obj in (self.list_view, self.tree):
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self._add_selected()
                return True
        pl = getattr(self, "preview_list", None)
        if pl is not None and obj is pl.viewport() and event.type() == QEvent.Type.Leave:
            # 鼠标离开列表: 立刻收起(不等 120ms 自检)
            self._on_preview_viewport_left()
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        """焦点不在文件区时按回车，同样是"加入"而不是"确定并关闭"。

        例外：焦点在按钮/下拉框/路径框上时按它们自己的语义走（按钮=点击，路径框=跳转）。
        """
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            w = QApplication.focusWidget()
            if isinstance(w, (QPushButton, QComboBox, QLineEdit)):
                super().keyPressEvent(event)
                return
            self._add_selected()
            return
        super().keyPressEvent(event)

    def _show_file_menu(self, view, pos):
        """文件区右键菜单：添加 / 对比 / 属性（最后一组只放「对比 + 属性」）。"""
        idx = view.indexAt(pos)
        if idx.isValid():
            sm = view.selectionModel()
            if sm is not None and not sm.isSelected(idx):
                # 右键落在没选中的项上：先补选中它（与资源管理器一致）
                sm.select(idx, QItemSelectionModel.SelectionFlag.Select
                          | QItemSelectionModel.SelectionFlag.Rows)
        menu, act_add, act_cmp, act_prop, files = self._build_file_menu(view)
        chosen = menu.exec(view.viewport().mapToGlobal(pos))
        if chosen is act_add:
            self._add_selected()
        elif chosen is act_cmp:
            self._show_compare(files)
        elif chosen is act_prop:
            self._show_properties(files)

    def _build_file_menu(self, view):
        """按当前选中项生成右键菜单（拆出来便于离屏测试，exec 在 _show_file_menu 里）。"""
        folders, files = self._selected_paths(view)
        paths = folders + files
        menu = QMenu(self)
        menu.setCursor(Qt.PointingHandCursor)   # 菜单项悬浮也是抓手
        act_add = menu.addAction(f"添加（{len(paths)} 项）" if paths else "添加")
        act_add.setEnabled(bool(paths))
        menu.addSeparator()
        # v191: 和主界面同口径 —— 至少要 2 个文件才有的比；单选时留着但置灰,
        # 别让用户以为点了没反应。横线下面只放「对比 / 属性」两项。
        act_cmp = menu.addAction("对比…")
        act_cmp.setEnabled(len(files) >= 2)
        if len(files) < 2:
            act_cmp.setToolTip("至少选中 2 个文件才能对比")
        act_prop = menu.addAction("属性")
        act_prop.setEnabled(bool(files))
        return menu, act_add, act_cmp, act_prop, files

    def _show_properties(self, paths: list[str]):
        """弹窗里直接看文件属性（与主窗口同一个属性窗口）。"""
        if not paths:
            return
        try:
            from ..core.photo import Photo, kind_of
            from .props_dialog import PropertiesDialog
            photos = []
            for p in paths:
                if not os.path.isfile(p):
                    continue
                k = kind_of(p)
                if k == "other":
                    continue
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                photos.append(Photo(os.path.normpath(p), kind=k, size=size))
            if not photos:
                return
            dlg = PropertiesDialog(photos, {}, self)
            dlg.setAttribute(Qt.WA_DeleteOnClose, True)
            dlg.destroyed.connect(lambda *_: self._forget_prop(dlg))
            self._prop_dialogs.append(dlg)
            dlg.show()
            dlg.raise_()
        except Exception as ex:
            QMessageBox.warning(self, "属性", f"打不开属性窗口：{ex}")

    def _forget_prop(self, dlg):
        try:
            self._prop_dialogs.remove(dlg)
        except ValueError:
            pass

    def _show_compare(self, paths: list[str]):
        """弹窗里右键「对比」：和主界面同一个对比窗口（非模态、可多开）。

        弹窗里挑的文件还没进主列表，merge_specs 里没有它们的规格 —— 先把窗口
        开出来（点了立刻有反馈），再用后台线程 ffprobe 读视频参数，读完后
        `set_specs()` 原地重填。图片 / 没装 FFmpeg 时与主界面口径一致：没有规格
        的参数行就不占行，给一条说明文字。
        """
        from ..core.photo import Photo, kind_of
        from .dialogs import CompareDialog

        photos: list = []
        cache = getattr(getattr(self, "list_delegate", None), "_cache", {}) or {}
        for raw in paths or []:
            p = os.path.normpath(raw)
            if not os.path.isfile(p):
                continue
            k = kind_of(p)
            if k == "other":
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            photo = Photo(p, kind=k, size=size)
            pm = cache.get(p)          # 弹窗里已经解码过的缩略图直接拿来用
            if pm is not None and not pm.isNull():
                photo.thumb = pm
            photos.append(photo)
        if len(photos) < 2:
            return
        try:
            # 父窗口挂弹窗：exec() 模态下这个非模态窗口也能正常操作（和「属性」一致）
            dlg = CompareDialog(photos, {}, self)
            dlg.setAttribute(Qt.WA_DeleteOnClose, True)
            dlg.destroyed.connect(lambda *_: self._forget_cmp(dlg))
            self._cmp_dialogs.append(dlg)
            dlg.show()
            n = len(self._cmp_dialogs) - 1
            geo = dlg.frameGeometry()
            geo.moveCenter(self.frameGeometry().center())
            geo.translate((n % 6) * 28, (n % 6) * 28)
            dlg.move(geo.topLeft())
            dlg.raise_()
        except Exception as ex:
            QMessageBox.warning(self, "对比", f"打不开对比窗口：{ex}")
            return

        videos = [p for p in photos if getattr(p, "kind", "") == "video"]
        if not videos:
            try:
                dlg.set_specs({}, "选中的都是图片，没有可对比的视频参数")
            except RuntimeError:
                pass
            return
        try:
            from ..merge.workers import ProbeWorker
            worker = ProbeWorker(videos, self)
            self._cmp_workers.append(worker)   # 线程没真停前不许丢引用
            specs: dict = {}
            state = {"hint": ""}

            def _on_one(i, spec, _err):
                if 0 <= i < len(videos) and spec:
                    specs[videos[i].path] = spec

            def _on_no_ffmpeg():
                state["hint"] = "未找到 FFmpeg，无法读取视频参数"

            def _on_done():
                hint = state["hint"]
                if not hint and not specs:
                    hint = "没有读到这些文件的视频参数"
                try:
                    dlg.set_specs(specs, hint)
                except RuntimeError:
                    pass                # 对比窗已经关了

            def _on_finished():
                try:
                    self._cmp_workers.remove(worker)
                except ValueError:
                    pass

            worker.one.connect(_on_one)
            worker.no_ffmpeg.connect(_on_no_ffmpeg)
            worker.finished_all.connect(_on_done)
            worker.finished.connect(_on_finished)
            dlg.show_loading()
            worker.start()
        except Exception:
            try:
                dlg.set_specs({}, "读取视频参数失败")
            except RuntimeError:
                pass

    def _forget_cmp(self, dlg):
        try:
            self._cmp_dialogs.remove(dlg)
        except ValueError:
            pass

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
        # 注意: 在 QListView 上 selectedRows() 会返回空(只有 selectedIndexes() 有值),
        # 所以统一从 selectedIndexes() 里筛第 0 列, 两种视图都稳妥。
        folders, files = self._selected_paths()
        # 之前用回车/右键已经加过的不再重复计入（它们已经提交给主窗口了）
        if self._added:
            folders = [p for p in folders if _norm_key(p) not in self._added]
            files = [p for p in files if _norm_key(p) not in self._added]
        # 允许不选具体项时，直接把当前所在文件夹作为文件夹添加
        # （但若本次已经用回车/右键加过东西，就别再顺手把整个当前文件夹塞进去）
        if (not folders and not files and not self._added
                and os.path.isdir(self._current)):
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
