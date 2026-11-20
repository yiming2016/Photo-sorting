# -*- coding: utf-8 -*-
"""「参数对比」的共用规则: 数据取自 grouping.compare_rows, 这里只负责上色和分档标题。

⚠️ 两套颜色逻辑, 别混:
  A. 主界面分组标题(delegate.draw_header) —— 值按致命度**常驻**标色, 不看是否冲突;
  B. 右侧「参数对比」+ 右键「对比」窗口 —— **冲突时才**按致命度标色, 一致显示绿色。
本模块只管 B。两个界面的"列哪些参数/怎么排序/怎么标色"都从这里出, 免得以后改一处漏一处。
"""
from __future__ import annotations

import base64
import html
from functools import lru_cache

from PySide6.QtCore import QBuffer, QEvent, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QTextDocument
from PySide6.QtWidgets import QApplication, QLabel, QToolTip

from ..merge import grouping
from . import theme as T


# bitrate_mode 是抽样估计值, 不在用户回填的 115 条 FIELD_DESC 里, 但对比区
# 会显示这一行。没有说明就成了"有ⓘ但点了没内容"的死图标 —— 用右键对比窗口
# 的同款文案兜底(单一来源的补充说明), 富文本 title 与立即弹窗都用同一个。
_DESC_FALLBACK = {
    "bitrate_mode": "码率类型：按前 20 秒逐秒码率的波动估计（可变/恒定）。文件里通常没有写死 CBR/VBR 标记，故仅供参考。",
}


def desc_for(key: str) -> str:
    """参数说明单一入口: 用户回填详解优先, 缺的用兜底文案。"""
    return grouping.field_desc(key) or _DESC_FALLBACK.get(key, "")


def row_color(level: str | None, same: bool) -> str:
    """B 逻辑: 一致=绿; 冲突按致命度 → 硬致命红 / 软致命橙 / 仅参考普通色。"""
    if same:
        return T.OK
    if level == "fatal":
        return T.DANGER
    if level == "warn":
        return T.WARN
    return T.TEXT


def build_rows(specs: list[dict], *, compact: bool = True,
               desc: bool = True) -> list[dict]:
    """带颜色的对比行: [{key, cn, level, same, values, color}], 已按档分组排好序。"""
    rows = grouping.compare_rows(specs, compact=compact, desc=desc)
    for r in rows:
        r["color"] = row_color(r["level"], r["same"])
    return rows


def section_title(level: str | None) -> str:
    return grouping.SECTION_TITLES.get(level, "")


def selection_summary(photos: list, specs: dict) -> str:
    """合并/统一两页共用的「已选 N 段 · 总时长 … · 约 … MB」摘要。

    数据口径与合并页旧逻辑一致: 时长取规格里的 duration 之和, 体积取 Photo.size 之和;
    没有对应数据就不显示那一段。无勾选时回落成统一提示。
    """
    n = len(photos)
    if not n:
        return "还没有勾选视频"
    total = 0.0
    for p in photos:
        spec = specs.get(getattr(p, "path", "")) or {}
        try:
            total += float(spec.get("duration") or 0)
        except (TypeError, ValueError):
            pass
    size = sum(getattr(p, "size", 0) or 0 for p in photos)
    parts = [f"已选 {n} 段"]
    if total > 0:
        parts.append(f"总时长 {grouping.fmt_duration(total)}")
    if size > 0:
        parts.append(f"约 {grouping.fmt_size(size)}")
    return " · ".join(parts)


def media_divider_before(rows: list[dict], i: int) -> bool:
    """v248: 第 i 行之前是否需要「视频 / 音频」分隔线。

    条件: i 与前一行属于同一大项(level 相同), 且两行 media 在 video↔audio 之间切换。
    大项交界由小标题负责, 不在这里画线。三处 UI 共用此判定, 口径一致。
    """
    if i <= 0 or i >= len(rows):
        return False
    prev, cur = rows[i - 1], rows[i]
    if prev.get("level") != cur.get("level"):
        return False
    return {prev.get("media"), cur.get("media")} == {"video", "audio"}


# ---- 对比区里的横线(QLabel 富文本, 右侧面板 / 统一页共用) ----
# ⚠️ 别用带 background 的 <div>: Qt 富文本会把块级背景铺到文本末尾(整片灰, v249 踩过)。
# ⚠️ 别用 <hr>: 上下间距是引擎硬编码 10/9px, margin/padding 全部无效(实测)。
# 用「<table width=100%><td border-top>」: 宽度自适应不溢出, 间距约 4px 可控, 纯 CSS 无外部依赖。
DIVIDER_COLOR = "#C8CED8"          # 视频/音频交界的灰线
SECTION_LINE_COLOR = T.ACCENT      # 「建议一致/仅供参考」档位上方的蓝线
BLUE_LINE_W = 2                    # 蓝线加粗(px)
INFO_COLOR = "#B0B8C6"             # ⓘ 角标的淡灰(实心, v255 调淡; 三处 UI 单一来源)
INFO_SIZE = 11                     # ⓘ 角标显示尺寸(px): 小号上标


def _rule_html(color: str, width: int = 1) -> str:
    """一条横贯整行的细线(块级元素, join 时前后不加 <br>)。"""
    return (f'<table width="100%" cellpadding="0" cellspacing="0" border="0">'
            f'<tr><td style="border-top:{width}px solid {color};'
            f'height:1px;line-height:1px;font-size:1px;">&nbsp;</td></tr></table>')


DIVIDER_HTML = _rule_html(DIVIDER_COLOR, 1)          # 灰线: 上下约 4px
SECTION_LINE_HTML = _rule_html(SECTION_LINE_COLOR, BLUE_LINE_W)  # 蓝线: 加粗
# join_rows 里这些是块级线, 前后都不插 <br>
_BLOCK_LINES = (DIVIDER_HTML, SECTION_LINE_HTML)


@lru_cache(maxsize=1)
def _make_info_uri() -> str:
    """自绘灰色ⓘ角标 → base64 data URI(规避字体缺字形的风险)。

    ⚠️ 必须延迟到首次调用(此时 QApplication 已创建)再画: 模块导入期画会
    在打包 exe 里触发 "Must construct a QGuiApplication" 静默闪退(v190 同款坑)。
    """
    im = QImage(16, 16, QImage.Format_ARGB32)
    im.fill(QColor(0, 0, 0, 0))
    p = QPainter(im)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(INFO_COLOR)); p.setPen(Qt.NoPen)
    p.drawEllipse(1, 1, 14, 14)
    p.setPen(QColor("#FFFFFF"))
    f = QFont("Arial", 11); f.setBold(True); p.setFont(f)
    p.drawText(im.rect(), Qt.AlignCenter, "i")
    p.end()
    buf = QBuffer(); buf.open(QBuffer.WriteOnly)
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(bytes(buf.data())).decode()


def info_html(key: str) -> str:
    """参数名右侧紧贴的灰色ⓘ角标(上标): 悬浮显示常见格式说明。

    <a title> 被 QLabel 解析为 charFormat.toolTip, 悬浮即显示; vertical-align:super
    让图标像文字角标一样贴在右上角, 而不是居中占一整行高。
    """
    # \n 原样保留: QLabel 富文本 <a title> 会把换行存进 charFormat.toolTip,
    # Qt 悬浮提示按多行显示(用户发回的详解逐条一行, 不能被替换成空格)。
    desc = desc_for(key)
    if not desc:
        return ""
    # html.escape 连同 & < > " ' 一起转义 —— 用户回填文案里出现这些字符时,
    # 富文本 title 属性不会被截断/注入(原先只 replace('"') 不够)。
    desc = html.escape(desc, quote=True)
    uri = _make_info_uri()   # 延迟生成, 避免导入期构造 QPainter 闪退
    return (f'<a href="info:{key}" title="{desc}">'
            f'<img src="{uri}" width="{INFO_SIZE}" height="{INFO_SIZE}" '
            f'style="vertical-align:super;"></a>')


def join_rows(parts: list[str]) -> str:
    """把对比区片段拼成富文本：普通行之间用 <br>，块级横线前后不插 <br>。

    <table> 横线自身换行且带约 4px 上下留白；前面再放 <br> 会多出一整行空白
    (旧 <hr> 方案 "<br>+<hr>+<br>" 实测上下各 25/24px)。大项之间的空串 ""
    仍照常产生空行，不受影响。
    """
    out = ""
    for p in parts:
        if not out:
            out = p
        elif p in _BLOCK_LINES or any(out.endswith(b) for b in _BLOCK_LINES):
            out += p          # 块级横线自身换行，前后都不加 <br>
        else:
            out += "<br>" + p
    return out


class InfoPopupFilter(QObject):
    """给富文本对比 QLabel 上的 ⓘ 加"立即弹出"说明窗(v255)。

    ⚠️ 为什么不用 QLabel 富文本自带的 `<a title>` tooltip：
    小 ⓘ 是 11px 上标，命中区极小 + 原生 tooltip 有约 1 秒延迟, 实测几乎看不到
    (项目早期 _InfoLabel 注释就记载过这个坑)。这里改成鼠标进入 ⓘ 立即弹无边框
    QLabel 弹窗, 移开即隐藏, 和右键对比窗口的 delegate tooltip 一致的体验。

    用法: `f = InfoPopupFilter(label)` —— 内部 installEventFilter; 调用方在
    setText 之后调 `f.sync()` 重建镜像文档(富文本变化时命中坐标会变)。
    弹窗与镜像文档都懒创建/在事件里构造, 不在模块级建 Qt 对象(避免打包闪退)。
    """

    def __init__(self, label: QLabel):
        super().__init__(label)
        self._label = label
        self._doc = QTextDocument(self)   # 镜像文档: 只为 anchorAt 命中, 不显示
        self._doc.setDocumentMargin(4)    # 与 QLabel 默认 documentMargin 一致
        self._popup = None                # 无边框 QLabel, 首次命中时创建
        self._last = None                 # (text, width) 缓存, 变了自动重建镜像
        label.installEventFilter(self)
        # v256: 弹窗是无父置顶窗口, 应用失焦(Alt+Tab/最小化)时不会自己消失,
        # 会一直悬浮遮挡别的程序。挂在 QApplication 上捕获失焦主动收起。
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _maybe_sync(self):
        """文本/宽度变了就重建镜像文档 —— 免得在每个 setText 调用点手动 sync。"""
        state = (self._label.text(), self._label.width())
        if state != self._last:
            self._last = state
            self._doc.setHtml(state[0])
            self._doc.setTextWidth(max(state[1], 1))

    def sync(self):
        """手动强制重建(可选, 通常 _maybe_sync 已够)。"""
        self._last = None
        self._maybe_sync()

    def _key_at(self, pos) -> str | None:
        self._maybe_sync()
        lay = self._doc.documentLayout()
        if lay is None:
            return None
        href = lay.anchorAt(QPointF(pos))
        if isinstance(href, str) and href.startswith("info:"):
            return href[5:]
        return None

    def _ensure_popup(self) -> QLabel:
        if self._popup is None:
            p = QLabel(None)
            p.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint |
                             Qt.WindowStaysOnTopHint)
            p.setAttribute(Qt.WA_ShowWithoutActivating)
            p.setWordWrap(True)
            p.setTextFormat(Qt.PlainText)
            p.setMaximumWidth(420)
            p.setStyleSheet(
                f"background: {T.TOOLTIP_BG}; color: {T.TOOLTIP_FG};"
                f" padding: 8px 12px; border-radius: 6px; font-size: 13px;")
            self._popup = p
        return self._popup

    def _show(self, key: str, global_pos):
        desc = desc_for(key)
        if not desc:
            self._hide()
            return
        p = self._ensure_popup()
        p.setText(desc)
        p.adjustSize()
        gp = (global_pos.toPoint() if hasattr(global_pos, "toPoint")
              else QPoint(global_pos.x(), global_pos.y()))
        p.move(gp.x() + 12, gp.y() + 14)
        p.show()

    def _hide(self):
        if self._popup is not None:
            self._popup.hide()

    def eventFilter(self, obj, ev):
        # v256: 应用失焦/状态切换(Alt+Tab、最小化)时收起弹窗, 避免无父置顶窗残留。
        app = QApplication.instance()
        if obj is app and app is not None:
            et = ev.type()
            # ApplicationDeactivate 本身就代表要失去焦点, 直接收起, 不要
            # 再查 applicationState()(事件投递那一刻状态可能还没翻转)。
            if et == QEvent.Type.ApplicationDeactivate:
                self._hide()
            elif et == QEvent.Type.ApplicationStateChange:
                if app.applicationState() != Qt.ApplicationActive:
                    self._hide()
            return False
        if obj is not self._label:
            return False
        et = ev.type()
        if et == QEvent.Type.MouseMove:
            pos = ev.position()
            key = self._key_at(pos)
            if key:
                self._show(key, ev.globalPosition())
            else:
                self._hide()
        elif et in (QEvent.Type.Leave, QEvent.Type.Hide):
            # Hide: 面板切走(排序↔合并↔统一)时 label 隐藏, 弹窗不能留在屏幕上
            self._hide()
        elif et == QEvent.Type.Destroy:
            # label 销毁时弹窗无父, 单独删掉, 不留悬空顶层窗
            if self._popup is not None:
                self._popup.deleteLater()
                self._popup = None
        return False   # 不吞事件, 不影响 QLabel 其它行为
