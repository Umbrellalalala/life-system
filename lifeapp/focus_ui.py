"""专注模块专用自绘控件。

集中放置番茄钟 / 统计页需要的定制控件，避免继续膨胀 widgets.py：
- FocusRing      极细环形进度（大号时间文字）
- FlipClock      沉浸模式翻页时钟
- SegmentedControl 分段控件（track / pill 两种外观）
- MenuIcon       线性图标（用于「⋯」弹层）
- PopupMenu      带图标的弹层菜单
- DonutChart     环形占比图
- TimelineChart  专注时间线
- HourBarChart   最佳专注时间（按小时柱状）
- HeatmapChart   年度热力图
- fade_in        通用淡入动画
"""
from __future__ import annotations

import math

from PySide6.QtCore import (
    Qt, QRectF, QPointF, QSize, QTimer, Signal, QEasingCurve, QPropertyAnimation,
)
from PySide6.QtGui import (
    QColor, QPainter, QPen, QFont, QFontMetrics, QPainterPath, QPolygonF,
    QLinearGradient, QBrush,
)
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QRadioButton, QVBoxLayout, QHBoxLayout,
    QSizePolicy, QGraphicsOpacityEffect, QPushButton, QToolTip,
)

from . import theme


def _c(name: str) -> QColor:
    return QColor(theme.get(name))


# 专注模块要用的几处**中性灰**（不带蓝味）。主题色板里的 surface_hi 是 #eef0f6，
# 偏蓝，用在参考图那种纯灰卡片/胶囊上会发紫，所以单独给一对亮/暗值。
def _neutral(light: str, dark: str) -> QColor:
    return QColor(dark if theme.is_dark() else light)


def tile_bg() -> QColor:
    """概览四卡的底色（参考图实测 #f8f8f8，中性灰，不是蓝灰）。"""
    return _neutral("#f8f8f8", "#232330")


def pill_idle_bg() -> QColor:
    """分段控件 / tab **未选中项**的胶囊底（参考图实测 #f1f1f1）。"""
    return _neutral("#f1f1f1", "#282835")


# 参考图的概览卡文字是**纯中性灰**（实测 #959595），而主题色板里的 muted
# 是 #8b8fa3，带蓝味、放在灰底卡片上会发紫。这几档只给专注页的自绘控件用。
_NEUTRAL_TEXT = {
    "label": ("#959595", "#8e8ea8"),
    "value": ("#1f1f1f", "#f2f2f7"),
    "time": ("#9b9b9b", "#84849a"),
    "title": ("#1a1a1a", "#f2f2f7"),
    "group": ("#a3a3a3", "#7c7c92"),
}


def neutral_text(role: str) -> QColor:
    return _neutral(*_NEUTRAL_TEXT[role])


def paint_tomato_glyph(p: QPainter, cx: float, cy: float, R: float,
                       col: QColor, bg: QColor) -> None:
    """一枚番茄剪影：顶部咬出浅凹槽的果身 + 坐在凹槽上方的五角萼冠。

    尺寸全部以**底盘半径 R** 为基准，比例是从参考图量的。参考图里萼冠和果身
    之间是有底盘色空隙的（冠浮在凹口上方），完全贴合会画成石榴 / 洋葱。
    ``bg`` 是那道空隙和凹口的颜色，一般就是底盘色。
    """
    p.setPen(Qt.NoPen)
    p.setBrush(col)
    p.drawEllipse(QPointF(cx, cy + R * 0.34), R * 0.62, R * 0.60)
    # 用底盘色在果身顶部压出一道浅凹槽：椭圆比果身宽，所以只咬得掉中间，
    # 两侧的肩膀会留下来，正是参考图那两条弧线。
    p.setBrush(bg)
    p.drawEllipse(QPointF(cx, cy - R * 0.60), R * 0.78, R * 0.50)
    # 萼冠：压扁的五角小冠，下沿几乎贴进凹槽（画大了就是一颗飘着的星星）
    crown = QPainterPath()
    crown_cy = cy - R * 0.30
    outer, inner, squash = R * 0.34, R * 0.18, 0.50
    for i in range(10):
        ang = math.radians(-90.0 + i * 36.0)
        rad = outer if i % 2 == 0 else inner
        pt = QPointF(cx + rad * math.cos(ang),
                     crown_cy + rad * math.sin(ang) * squash)
        if i == 0:
            crown.moveTo(pt)
        else:
            crown.lineTo(pt)
    crown.closeSubpath()
    p.setBrush(col)
    p.drawPath(crown)


def stats_bg() -> QColor:
    """统计页页底：参考图实测 #f5f5f5 中性灰（主题 bg 偏蓝，卡与底分不开）。"""
    return _neutral("#f5f5f5", "#121219")


def tomato_disc(p: QPainter, cx: float, cy: float, disc_r: float,
                accent: QColor | None = None) -> None:
    """淡蓝圆盘 + 盘中番茄，记录轨道和详情卡标题共用这一份。"""
    col = accent if accent is not None else _c("focus")
    disc = _c("focus_soft")
    p.setPen(Qt.NoPen)
    p.setBrush(disc)
    p.drawEllipse(QPointF(cx, cy), disc_r, disc_r)
    paint_tomato_glyph(p, cx, cy, disc_r, col, disc)


def fmt_tile_dur(minutes: float) -> str:
    """概览卡里的时长：``0 m`` / ``319 h 6 m`` —— 数字与单位之间留空格。

    和 :func:`_fmt_dur` 是两套写法，**不是疏漏**：参考图的概览卡就是这么排的，
    而统计页各处沿用无空格的紧凑格式。只给概览卡用，别处不要引这个。
    """
    h, m = divmod(int(minutes), 60)
    if h <= 0:
        return f"{m} m"
    if m == 0:
        return f"{h} h"
    return f"{h} h {m} m"


def _fmt_dur(minutes: float) -> str:
    """全应用统一的时长格式：``1h20m`` / ``45m`` / ``1h``。

    刻意不留空格 —— 之前 ``fmt_hm`` 产出 ``"1 h 20 m"``、``fmt_rec`` 产出
    ``"1h 20m"``，而统计页又到处 ``.replace(" ", "")`` 打补丁，
    同一个应用里出现过四种写法，统一收敛到这一个实现。
    """
    h, m = divmod(int(minutes), 60)
    if h <= 0:
        return f"{m}m"
    if m == 0:
        return f"{h}h"
    return f"{h}h{m}m"


def fmt_hm(minutes: float) -> str:
    """时长格式化（保留旧名，行为与 :func:`fmt_rec` 一致）。"""
    return _fmt_dur(minutes)


def fmt_rec(minutes: float) -> str:
    """时长格式化（保留旧名，行为与 :func:`fmt_hm` 一致）。"""
    return _fmt_dur(minutes)


# 所有 Qt.Popup 弹层统一用这组窗口标志。
#
# 只给 Qt.Popup 的话，Windows 会按**矩形**给弹窗加原生投影，而弹层为了做圆角
# 开了 WA_TranslucentBackground —— 两者叠加，圆角外面就糊出一圈约 #b5b5b5 的
# 深灰描边（用户截图里那条"黑边"）。实测：
#   Popup                      -> 边框 #b5b5b5（错）
#   Popup + NoDropShadow       -> 边框 #b5b5b5（没用）
#   Popup + Frameless + NoDrop -> 边框 #e6e8f0 = QSS 里设计的颜色（对）
# 关键是 FramelessWindowHint，单独去掉投影无效。
POPUP_FLAGS = (Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
               | Qt.WindowType.NoDropShadowWindowHint)


def fade_in(widget: QWidget, duration: int = 160) -> QPropertyAnimation:
    """给控件加一层淡入动画（打开弹窗 / 切换页面时用）。"""
    eff = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start(QPropertyAnimation.DeleteWhenStopped)
    return anim


# ---------------------------------------------------------------------------
# 极细环形进度
# ---------------------------------------------------------------------------
class FocusRing(QWidget):
    """番茄钟主环：居中大号时间 + 外圈轨道。

    两种外观，对应顶栏的两个分段（参考图里明显不同，不能混用）：

    * ``plain``（番茄计时）：一条**连续细圆环**当轨道，进度用主色圆弧覆盖上去。
      实测参考图轨道宽 / 直径 ≈ 0.011。
    * ``ticks``（正计时）：**120 根等长细刻度**（每 3° 一根）拼成圆环，
      进度用「点亮前 N 根刻度」表达，不画连续弧。
      刻度长 / 外半径 ≈ 0.104，刻度宽 / 外半径 ≈ 0.013，均从参考图量得。

    两种外观都刻意留出中心空白放时间文字，视觉重心在中间的数字上。
    """

    TICKS = 120              # 3° 一根
    TICK_LEN_RATIO = 0.104   # 刻度长 / 外半径
    TICK_W_RATIO = 0.013     # 刻度宽 / 外半径
    EDGE_PAD = 2.0           # 最外圈刻度离控件边缘的留白
    PLAIN_W_RATIO = 0.011    # plain 轨道线宽 / 直径

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._progress = 0.0
        self._text = "00:00"
        self._accent = "focus"
        self._track = "focus_track"
        self._text_key = "text_hi"
        self._font_ratio = 0.172
        self._style = "plain"
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_progress(self, p: float) -> None:
        self._progress = max(0.0, min(1.0, p))
        self.update()

    def set_text(self, text: str) -> None:
        self._text = text
        self.update()

    def set_accent(self, key: str) -> None:
        self._accent = key
        self.update()

    def set_text_color(self, key: str) -> None:
        self._text_key = key
        self.update()

    def set_font_ratio(self, ratio: float) -> None:
        self._font_ratio = ratio
        self.update()

    def set_style(self, style: str) -> None:
        """``plain`` = 连续圆环（番茄计时），``ticks`` = 刻度环（正计时）。"""
        if style == self._style:
            return
        self._style = style
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        d = min(w, h)
        cx, cy = w / 2.0, h / 2.0
        r_out = d / 2.0 - self.EDGE_PAD
        track_c, accent_c = _c(self._track), _c(self._accent)

        if self._style == "plain":
            self._paint_plain(p, cx, cy, r_out, d, track_c, accent_c)
        else:
            self._paint_ticks(p, cx, cy, r_out, track_c, accent_c)

        p.setPen(_c(self._text_key))
        f = QFont()
        f.setPixelSize(max(18, int(d * self._font_ratio)))
        f.setWeight(QFont.Medium)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, self._text)

    def _paint_plain(self, p: QPainter, cx: float, cy: float, r: float,
                     d: float, track_c: QColor, accent_c: QColor) -> None:
        """连续细圆环轨道 + 主色进度弧（12 点起顺时针）。"""
        pen = QPen(track_c, max(2.0, d * self.PLAIN_W_RATIO))
        pen.setCapStyle(Qt.FlatCap)
        p.setPen(pen)
        p.drawEllipse(QPointF(cx, cy), r, r)

        if self._progress <= 0.0005:
            return
        pen.setColor(accent_c)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        span = int(round(self._progress * 360 * 16))
        p.drawArc(rect, 90 * 16, -span)

    def _paint_ticks(self, p: QPainter, cx: float, cy: float, r_out: float,
                     track_c: QColor, accent_c: QColor) -> None:
        """等长刻度环：前 N 根换成主色表示进度。"""
        r_in = r_out * (1.0 - self.TICK_LEN_RATIO)
        tick_w = max(1.4, r_out * self.TICK_W_RATIO)
        # 进度按刻度根数点亮：0 根 = 全灰，120 根 = 全蓝
        lit = int(round(self._progress * self.TICKS)) if self._progress > 0.0005 else 0
        lit = max(0, min(self.TICKS, lit))

        pen = QPen()
        pen.setWidthF(tick_w)
        pen.setCapStyle(Qt.FlatCap)
        step = 360.0 / self.TICKS
        for i in range(self.TICKS):
            # 12 点方向起、顺时针排布
            a = math.radians(-90.0 + i * step)
            ca, sa = math.cos(a), math.sin(a)
            pen.setColor(accent_c if i < lit else track_c)
            p.setPen(pen)
            p.drawLine(QPointF(cx + r_in * ca, cy + r_in * sa),
                       QPointF(cx + r_out * ca, cy + r_out * sa))


# ---------------------------------------------------------------------------
# 翻页时钟
# ---------------------------------------------------------------------------
class FlipClock(QWidget):
    """沉浸模式翻页时钟：暗色圆角卡片 + 大号数字，数字变化时做翻页动画。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._groups: list[str] = ["50", "00"]
        self._pending: list[tuple[str, str]] = []
        self._t = 1.0
        self._card_h = 190
        self._gap = 40
        self._radius_ratio = 0.115
        self._card_bg = QColor("#1b1b1f")
        self._digit = QColor("#c6c6cc")
        self._seam = QColor("#0a0a0c")
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._step)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    # -- 对外 -------------------------------------------------------------
    def set_time(self, text: str) -> None:
        """text 形如 50:00 / 1:02:33，冒号分段，每段一张卡片。"""
        groups = [g for g in text.split(":") if g != ""]
        if len(groups) != len(self._groups):
            self._groups = groups
            self._pending = [(g, g) for g in groups]
            self._t = 1.0
            self._sync_height()
            self.update()
            return
        changed = False
        new_pending = []
        for i, g in enumerate(groups):
            old = self._groups[i]
            if g != old:
                new_pending.append((old, g))
                changed = True
            else:
                new_pending.append((old, old))
        if changed:
            self._pending = new_pending
            self._t = 0.0
            self._timer.start()
        self._groups = groups
        self.update()

    def set_card_height(self, h: int) -> None:
        self._card_h = max(60, h)
        self._sync_height()
        self.update()

    # -- 内部 -------------------------------------------------------------
    def _sync_height(self) -> None:
        self.setMinimumHeight(self._card_h)
        self.setMaximumHeight(self._card_h)

    def _step(self) -> None:
        self._t = min(1.0, self._t + 16 / 280)
        self.update()
        if self._t >= 1.0:
            self._timer.stop()
            self._pending = [(g, g) for g in self._groups]

    def _card_width(self, text: str, fm) -> float:
        return fm.horizontalAdvance(text) + self._card_h * 0.30

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont("Segoe UI", 1)
        f.setPixelSize(int(self._card_h * 0.63))
        f.setWeight(QFont.Bold)
        p.setFont(f)
        fm = p.fontMetrics()

        widths = [self._card_width(g, fm) for g in self._groups]
        total = sum(widths) + self._gap * max(len(widths) - 1, 0)
        x = (self.width() - total) / 2
        y = (self.height() - self._card_h) / 2
        for i, g in enumerate(self._groups):
            rect = QRectF(x, y, widths[i], self._card_h)
            old, new = self._pending[i] if i < len(self._pending) else (g, g)
            self._paint_card(p, rect, old, new, self._t)
            x += widths[i] + self._gap

    def _paint_card(self, p: QPainter, rect: QRectF, old: str, new: str,
                    t: float) -> None:
        r = self._card_h * self._radius_ratio
        p.setPen(Qt.NoPen)
        p.setBrush(self._card_bg)
        p.drawRoundedRect(rect, r, r)

        mid = rect.center().y()
        top = QRectF(rect.left(), rect.top(), rect.width(), rect.height() / 2)
        bot = QRectF(rect.left(), mid, rect.width(), rect.height() / 2)

        # 数字始终在**整张卡片**里居中绘制，靠 setClipRect 只露出上半 / 下半。
        # 之前把 top / bot 当绘制矩形传进 drawText，等于「在半个卡片里居中画一个
        # 完整数字」，于是上下各出现一个被裁过的完整数字，而不是一个数字的两半。
        p.save()
        p.setClipRect(bot)
        p.setPen(self._digit)
        p.drawText(rect, Qt.AlignCenter, new)
        p.restore()

        p.save()
        p.setClipRect(top)
        if t < 0.5:
            sy = max(0.0, 1.0 - 2 * t)
            if sy > 0.02:
                p.save()
                p.translate(rect.center().x(), mid)
                p.scale(1.0, sy)
                p.translate(-rect.center().x(), -mid)
                p.setPen(self._digit.darker(115))
                p.drawText(rect, Qt.AlignCenter, old)
                p.restore()
        else:
            p.setPen(self._digit)
            p.drawText(rect, Qt.AlignCenter, new)
        p.restore()

        # 中缝
        p.setPen(QPen(self._seam, 1))
        p.drawLine(QPointF(rect.left() + 2, mid), QPointF(rect.right() - 2, mid))


# ---------------------------------------------------------------------------
# 分段控件
# ---------------------------------------------------------------------------
class SegmentedControl(QWidget):
    """分段控件。

    style="track"：灰色轨道 + 白色选中胶囊（统计页顶栏）
    style="pill" ：无轨道，选中项淡色底 + 主题色文字（专注页顶栏）
    """

    changed = Signal(int)

    def __init__(self, items: list[str], style: str = "track",
                 height: int = 32, parent: QWidget | None = None):
        super().__init__(parent)
        self._items = list(items)
        self._style = style
        self._index = 0
        self._hover = -1
        self._anim_x = 0.0
        self._anim_w = 0.0
        self._ready = False
        self._h = height
        self._gap = 6 if style == "pill" else 3
        self.setFixedHeight(height)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._slide)

    # -- 对外 -------------------------------------------------------------
    def current_index(self) -> int:
        return self._index

    def set_index(self, idx: int, emit: bool = False) -> None:
        idx = max(0, min(idx, len(self._items) - 1))
        if idx == self._index and self._ready:
            return
        self._index = idx
        self._start_slide()
        if emit:
            self.changed.emit(idx)

    # -- 几何 -------------------------------------------------------------
    def _natural_width(self) -> float:
        f = self.font()
        f.setPixelSize(13)
        f.setBold(True)
        fm = self._fm_for(f)
        widths = [max(56.0, fm.horizontalAdvance(s) + 30.0) for s in self._items]
        if not widths:
            return 120.0
        return sum(widths) + self._gap * (len(widths) - 1)

    def sizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize as _QSize
        return _QSize(int(self._natural_width()) + 8, self._h)

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()

    def _seg_rects(self) -> list[QRectF]:
        n = len(self._items)
        if n == 0:
            return []
        f = self.font()
        f.setPixelSize(13)
        f.setBold(True)
        fm = self._fm_for(f)
        widths = [max(56.0, fm.horizontalAdvance(s) + 30.0) for s in self._items]
        total = sum(widths) + self._gap * (n - 1)
        x = (self.width() - total) / 2
        out = []
        for w in widths:
            out.append(QRectF(x, 0, w, self._h))
            x += w + self._gap
        return out

    @staticmethod
    def _fm_for(f: QFont):
        from PySide6.QtGui import QFontMetricsF
        return QFontMetricsF(f)

    def _slide(self) -> None:
        rects = self._seg_rects()
        if not rects:
            self._timer.stop()
            return
        target_x = rects[self._index].left()
        target_w = rects[self._index].width()
        dx = (target_x - self._anim_x) * 0.32
        dw = (target_w - self._anim_w) * 0.32
        self._anim_x += dx
        self._anim_w += dw
        if abs(dx) < 0.4 and abs(dw) < 0.4:
            self._anim_x, self._anim_w = target_x, target_w
            self._timer.stop()
        self.update()

    def _start_slide(self) -> None:
        rects = self._seg_rects()
        if not self._ready and rects:
            self._anim_x = rects[self._index].left()
            self._anim_w = rects[self._index].width()
            self._ready = True
        self._timer.start()
        self.update()

    # -- 事件 -------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        rects = self._seg_rects()
        if rects:
            # 尺寸变化时直接吸附到目标位置，不做动画
            self._anim_x = rects[self._index].left()
            self._anim_w = rects[self._index].width()
            self._ready = True

    def _hit(self, pos) -> int:
        for i, r in enumerate(self._seg_rects()):
            if r.contains(QPointF(pos)):
                return i
        return -1

    def mousePressEvent(self, event) -> None:  # noqa: N802
        idx = self._hit(event.position())
        if idx >= 0 and idx != self._index:
            self._index = idx
            self._start_slide()
            self.changed.emit(idx)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        h = self._hit(event.position())
        if h != self._hover:
            self._hover = h
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rects = self._seg_rects()
        if not rects:
            return
        if not self._ready:
            self._anim_x = rects[self._index].left()
            self._anim_w = rects[self._index].width()
            self._ready = True
        radius = self._h / 2

        if self._style == "track":
            track = QRectF(min(r.left() for r in rects) - 3, 0,
                           sum(r.width() for r in rects) + self._gap * (len(rects) - 1) + 6,
                           self._h)
            p.setPen(Qt.NoPen)
            p.setBrush(_c("surface_hi"))
            p.drawRoundedRect(track, radius, radius)
            p.setBrush(_c("bg_alt"))
            pill = QRectF(self._anim_x, 1.5, self._anim_w, self._h - 3)
            p.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        elif self._index < len(rects):
            # 参考图里两个分段**各自都是一颗胶囊**：未选中是浅灰底，
            # 选中才换成淡蓝底 + 主色文字。只画选中项会显得右边那截是空的。
            p.setPen(Qt.NoPen)
            for i, r in enumerate(rects):
                if i == self._index:
                    continue
                p.setBrush(pill_idle_bg())
                box = QRectF(r.left(), 1.5, r.width(), self._h - 3)
                p.drawRoundedRect(box, box.height() / 2, box.height() / 2)
            p.setBrush(_c("focus_soft"))
            pill = QRectF(self._anim_x, 1.5, self._anim_w, self._h - 3)
            p.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)

        f = self.font()
        f.setPixelSize(13)
        p.setFont(f)
        for i, r in enumerate(rects):
            active = i == self._index
            if self._style == "track":
                color = _c("text_hi") if active else _c("muted")
            else:
                color = _c("focus") if active else _c("muted")
            if not active and i == self._hover:
                color = _c("text")
            f.setWeight(QFont.DemiBold if active else QFont.Normal)
            p.setFont(f)
            p.setPen(color)
            p.drawText(r, Qt.AlignCenter, self._items[i])


# ---------------------------------------------------------------------------
# 圆形单选按钮
# ---------------------------------------------------------------------------
class RoundRadio(QRadioButton):
    """圆形单选按钮：细描边圆环，选中时中心填一个实心圆点。

    不用 QSS 的 ``::indicator``。选中态若想用粗边框表达「环 + 点」
    （``border: 5px solid`` + ``border-radius: 8px``，16px 的框），
    Qt 会把圆角渲染成**圆角方块**。实测同样 16px 的框：
    ``border: 1.5px`` + ``radius: 8px`` 是正圆，换成 ``border: 5px`` 就退化。
    所以这里整块自己画。
    """

    _D = 17        # 外径
    _RING = 1.6    # 圆环线宽
    _DOT = 0.52    # 中心点直径 / 外径
    _GAP = 8       # 圆与文字间距

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        # 自带 indicator 收成 0，位置留给自绘
        self.setStyleSheet("QRadioButton::indicator { width: 0px; height: 0px; }")
        self.toggled.connect(self.update)

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetrics(self.font())
        w = self._D + self._GAP + fm.horizontalAdvance(self.text())
        return QSize(w + 2, max(self._D, fm.height()))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def enterEvent(self, event) -> None:  # noqa: N802
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        d = self._D
        box = QRectF(0, (self.height() - d) / 2, d, d)
        checked = self.isChecked()

        # 外环：选中或悬停时用主色
        p.setBrush(Qt.NoBrush)
        hot = checked or self.underMouse()
        p.setPen(QPen(_c("focus") if hot else _c("border_strong"), self._RING))
        inset = self._RING / 2
        p.drawEllipse(box.adjusted(inset, inset, -inset, -inset))

        # 中心点
        if checked:
            r = d * self._DOT / 2
            p.setPen(Qt.NoPen)
            p.setBrush(_c("focus"))
            p.drawEllipse(box.center(), r, r)

        # 文字
        p.setFont(self.font())
        p.setPen(_c("text"))
        tr = QRectF(d + self._GAP, 0,
                    max(0.0, self.width() - d - self._GAP), self.height())
        p.drawText(tr, int(Qt.AlignVCenter | Qt.AlignLeft), self.text())


# ---------------------------------------------------------------------------
# 线性图标
# ---------------------------------------------------------------------------
class MenuIcon(QWidget):
    """1.5px 线性图标：immersive / mini / stats / settings。"""

    def __init__(self, kind: str, size: int = 17, color_key: str = "text",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._kind = kind
        self._color_key = color_key
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_color_key(self, key: str) -> None:
        self._color_key = key
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(_c(self._color_key), 1.5)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        s = min(self.width(), self.height())
        k = self._kind
        if k == "immersive":
            a = s * 0.30
            m = s * 0.10
            for cx, cy, sx, sy in ((m, m, 1, 1), (s - m, m, -1, 1),
                                   (m, s - m, 1, -1), (s - m, s - m, -1, -1)):
                p.drawLine(QPointF(cx, cy), QPointF(cx + a * sx, cy))
                p.drawLine(QPointF(cx, cy), QPointF(cx, cy + a * sy))
        elif k == "mini":
            r = QRectF(s * 0.12, s * 0.20, s * 0.76, s * 0.60)
            p.drawRoundedRect(r, 2.5, 2.5)
            p.setBrush(_c(self._color_key))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(r.right() - s * 0.34, r.bottom() - s * 0.26,
                                     s * 0.30, s * 0.22), 1.5, 1.5)
        elif k == "stats":
            c = QPointF(s / 2, s / 2)
            p.drawEllipse(c, s * 0.40, s * 0.40)
            p.drawLine(c, QPointF(c.x(), c.y() - s * 0.22))
            p.drawLine(c, QPointF(c.x() + s * 0.17, c.y() + s * 0.10))
        elif k == "settings":
            for i, yy in enumerate((0.24, 0.5, 0.76)):
                y = s * yy
                p.drawLine(QPointF(s * 0.10, y), QPointF(s * 0.90, y))
                p.setBrush(_c(self._color_key))
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(s * (0.34 if i != 1 else 0.66), y), 1.6, 1.6)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
        elif k == "delete":
            p.drawLine(QPointF(s * 0.18, s * 0.30), QPointF(s * 0.82, s * 0.30))
            p.drawLine(QPointF(s * 0.38, s * 0.30), QPointF(s * 0.43, s * 0.15))
            p.drawLine(QPointF(s * 0.62, s * 0.30), QPointF(s * 0.57, s * 0.15))
            p.drawLine(QPointF(s * 0.43, s * 0.15), QPointF(s * 0.57, s * 0.15))
            p.drawRoundedRect(QRectF(s * 0.28, s * 0.30, s * 0.44, s * 0.55), 2, 2)
            p.drawLine(QPointF(s * 0.42, s * 0.42), QPointF(s * 0.42, s * 0.73))
            p.drawLine(QPointF(s * 0.58, s * 0.42), QPointF(s * 0.58, s * 0.73))
        elif k == "chevron":
            # 下拉箭头：开口向下的 V
            p.drawLine(QPointF(s * 0.28, s * 0.42), QPointF(s * 0.50, s * 0.62))
            p.drawLine(QPointF(s * 0.50, s * 0.62), QPointF(s * 0.72, s * 0.42))
        elif k == "search":
            # 放大镜：圆 + 右下斜柄
            r = s * 0.28
            c = QPointF(s * 0.44, s * 0.44)
            p.drawEllipse(c, r, r)
            p.drawLine(QPointF(s * 0.64, s * 0.64), QPointF(s * 0.86, s * 0.86))
        elif k == "calendar":
            # 日历小图标：日期筛选按钮前缀（参考图里是线框日历 + 日期数字）
            box = QRectF(s * 0.12, s * 0.18, s * 0.76, s * 0.70)
            p.drawRoundedRect(box, 2.5, 2.5)
            p.drawLine(QPointF(box.left(), s * 0.38), QPointF(box.right(), s * 0.38))
            p.drawLine(QPointF(s * 0.32, s * 0.10), QPointF(s * 0.32, s * 0.24))
            p.drawLine(QPointF(s * 0.68, s * 0.10), QPointF(s * 0.68, s * 0.24))
            p.setBrush(_c(self._color_key))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(s * 0.26, s * 0.48, s * 0.20, s * 0.20),
                              1.5, 1.5)
        elif k == "timer":
            # 秒表：外圈 + 顶部按钮 + 中心实心点（参考图记录详情卡的第一行图标）
            c = QPointF(s * 0.5, s * 0.56)
            r = s * 0.34
            p.drawEllipse(c, r, r)
            p.drawLine(QPointF(c.x(), s * 0.06), QPointF(c.x(), s * 0.18))
            p.drawLine(QPointF(s * 0.36, s * 0.10), QPointF(s * 0.64, s * 0.10))
            p.setBrush(_c(self._color_key))
            p.setPen(Qt.NoPen)
            p.drawEllipse(c, s * 0.10, s * 0.10)
        elif k == "edit":
            # 铅笔：斜杆 + 笔尖小三角（常用专注菜单「编辑」）
            p.drawLine(QPointF(s * 0.24, s * 0.76), QPointF(s * 0.70, s * 0.30))
            p.drawLine(QPointF(s * 0.70, s * 0.30), QPointF(s * 0.82, s * 0.42))
            p.drawLine(QPointF(s * 0.82, s * 0.42), QPointF(s * 0.36, s * 0.88))
            p.drawLine(QPointF(s * 0.36, s * 0.88), QPointF(s * 0.24, s * 0.76))
            p.drawLine(QPointF(s * 0.62, s * 0.22), QPointF(s * 0.70, s * 0.30))
            p.drawLine(QPointF(s * 0.70, s * 0.30), QPointF(s * 0.78, s * 0.22))
            p.drawLine(QPointF(s * 0.78, s * 0.22), QPointF(s * 0.62, s * 0.22))
        elif k == "add_record":
            # 加号：常用专注菜单「添加记录」，和主页那枚 ＋ 同一种笔划
            p.drawLine(QPointF(s * 0.50, s * 0.18), QPointF(s * 0.50, s * 0.82))
            p.drawLine(QPointF(s * 0.18, s * 0.50), QPointF(s * 0.82, s * 0.50))
        elif k == "archive":
            # 归档箱：上盖 + 箱体 + 中间一道提手缝
            p.drawRoundedRect(QRectF(s * 0.12, s * 0.20, s * 0.76, s * 0.20),
                              2, 2)
            p.drawRoundedRect(QRectF(s * 0.20, s * 0.40, s * 0.60, s * 0.42),
                              2, 2)
            p.drawLine(QPointF(s * 0.40, s * 0.58), QPointF(s * 0.60, s * 0.58))
        elif k == "tomato":
            # 详情卡标题那枚图标是**蓝盘 + 白色番茄**（凹口露出蓝盘本色），
            # 和记录轨道上的「淡蓝盘 + 蓝色番茄」正好反色，两处不要合并。
            disc_r = s * 0.46
            p.setPen(Qt.NoPen)
            p.setBrush(_c("focus"))
            p.drawEllipse(QPointF(s / 2, s / 2), disc_r, disc_r)
            paint_tomato_glyph(p, s / 2, s / 2, disc_r,
                               QColor("#ffffff"), _c("focus"))


# ---------------------------------------------------------------------------
# 补录弹窗用的下拉字段
# ---------------------------------------------------------------------------
class PickerField(QFrame):
    """表单里的「下拉字段」：左边文字 + 右边细箭头，整块可点。

    参考设计里这类字段是白底 + 1px 浅边 + 右侧细箭头，文字为占位态时用 muted；
    打开对应弹层期间边框换成主色（``set_active(True)``）。
    """

    clicked = Signal()

    def __init__(self, placeholder: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("PickerField")
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 10, 0)
        lay.setSpacing(8)
        self.label = QLabel(placeholder)
        self.label.setObjectName("PickerFieldText")
        lay.addWidget(self.label, 1)
        lay.addWidget(MenuIcon("chevron", 16, "muted"))
        self._placeholder = placeholder
        self.set_value(placeholder, placeholder=True)

    def set_value(self, text: str, placeholder: bool = False) -> None:
        self.label.setText(text)
        self.label.setProperty("placeholder", "true" if placeholder else "false")
        self.label.style().unpolish(self.label)
        self.label.style().polish(self.label)

    def placeholder_text(self) -> str:
        return self._placeholder

    def set_active(self, on: bool) -> None:
        self.setProperty("active", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()


# ---------------------------------------------------------------------------
# 带图标的弹层菜单
# ---------------------------------------------------------------------------
class PopupMenuRow(QFrame):
    clicked = Signal()

    def __init__(self, kind: str, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("FocusMenuRow")
        self.setCursor(Qt.PointingHandCursor)
        # 破坏性操作（删除）按惯例标红，悬停也不跟着变主题色
        self._danger = kind == "delete"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(10)
        self.icon = MenuIcon(kind, 17, "red" if self._danger else "text")
        lay.addWidget(self.icon)
        self.label = QLabel(text)
        self.label.setObjectName("FocusMenuLabel")
        if self._danger:
            self.label.setProperty("danger", "true")
        lay.addWidget(self.label)
        lay.addStretch(1)
        self.setFixedHeight(38)

    def set_hover(self, on: bool) -> None:
        self.setProperty("hover", "true" if on else "false")
        if not self._danger:
            self.icon.set_color_key("accent" if on else "text")
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()


class PopupMenu(QFrame):
    """自定义弹层菜单：图标 + 文案，行高 38。"""

    def __init__(self, items: list[tuple[str, str]], parent: QWidget | None = None):
        super().__init__(parent, POPUP_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("FocusMenu")
        card = QFrame(self)
        card.setObjectName("FocusMenuCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(1)
        self.rows: list[PopupMenuRow] = []
        for kind, text in items:
            row = PopupMenuRow(kind, text, card)
            lay.addWidget(row)
            self.rows.append(row)
        self.setFixedWidth(176)

    def set_hover(self, idx: int) -> None:
        for i, r in enumerate(self.rows):
            r.set_hover(i == idx)

    def hideEvent(self, event) -> None:  # noqa: N802
        """关闭即销毁。

        每次打开都是新实例（parent 是页面），而 ``close()`` 只是隐藏、不销毁，
        对象会一直挂在页面上 —— 实测反复开关 12 次就留下 12 个 PopupMenu。
        Qt.Popup 被点外部关闭时走的是 hide 而不是 close，所以挂在 hideEvent 上。
        """
        super().hideEvent(event)
        self.deleteLater()


# ---------------------------------------------------------------------------
# 环形占比图
# ---------------------------------------------------------------------------
# 环形图 / 图例共用的色阶。明度从 50% 均匀递降到 89%：
# 旧的一组前 5 个递减后又跳回中蓝（#e2eaff → #93b0f5），
# 而且相邻色差太小，7 个任务时后几个扇区在图里根本分不出来。
# 只留 6 档、拉大明度间隔，超过 6 个任务从头循环
# （循环处是最深接最浅，对比反而更强）。
DONUT_COLORS = ["#2b57d6", "#4c7dff", "#6f9aff",
                "#92b6ff", "#b3ccff", "#d0e0ff"]


class DonutChart(QWidget):
    """环形占比图，中心显示标题 + 副标题。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._items: list[tuple[str, float]] = []
        self._center_top = "0h0m"
        self._center_bottom = "专注时长"
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, items: list[tuple[str, float]], center_top: str = "",
                 center_bottom: str = "",
                 color_keys: list[str | None] | None = None) -> None:
        """``color_keys`` 与 ``items`` 一一对应，元素是**主题色键**（可为 None）。

        给了就按它上色 —— 「已完成 / 未完成 / 已逾期」这类有语义的切片
        不能共用蓝色梯度，否则三片几乎分不出来，逾期也不会是红的。
        没给就用 ``DONUT_COLORS`` 的蓝色梯度（时长占比那种纯分类场景）。
        用主题键而不是十六进制，是为了明暗切换时自动跟着变。
        """
        keys = list(color_keys) if color_keys else []
        self._items = []
        for i, (n, v) in enumerate(items):
            if v > 0:
                self._items.append(
                    (n, float(v), keys[i] if i < len(keys) else None))
        self._center_top = center_top
        self._center_bottom = center_bottom
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        d = min(w, h) - 24
        if d <= 20:
            return
        rect = QRectF((w - d) / 2, (h - d) / 2, d, d)
        thick = max(14.0, d * 0.14)

        if not self._items:
            pen = QPen(_c("border"), thick)
            p.setPen(pen)
            p.drawArc(rect, 0, 360 * 16)
            p.setPen(_c("muted"))
            f = QFont()
            f.setPixelSize(13)
            p.setFont(f)
            p.drawText(rect, Qt.AlignCenter, "暂无数据")
            return

        total = sum(v for _, v, _ in self._items) or 1.0
        start = 90 * 16
        for i, (_, v, key) in enumerate(self._items):
            span = int(-360 * 16 * (v / total))
            color = _c(key) if key else QColor(
                DONUT_COLORS[i % len(DONUT_COLORS)])
            pen = QPen(color, thick)
            pen.setCapStyle(Qt.FlatCap)
            p.setPen(pen)
            p.drawArc(rect, start, span)
            start += span

        p.setPen(_c("text_hi"))
        f = QFont()
        f.setPixelSize(max(13, int(d * 0.12)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(rect.left(), rect.center().y() - d * 0.16,
                          rect.width(), d * 0.20),
                   Qt.AlignCenter, self._center_top)
        p.setPen(_c("muted"))
        f.setPixelSize(11)
        f.setBold(False)
        p.setFont(f)
        p.drawText(QRectF(rect.left(), rect.center().y() + d * 0.03,
                          rect.width(), d * 0.18),
                   Qt.AlignCenter, self._center_bottom)


# ---------------------------------------------------------------------------
# 专注时间线
# ---------------------------------------------------------------------------
class TimelineChart(QWidget):
    """横条时间线：y 轴为时刻，x 轴为星期；每条记录画一根圆角横条。"""

    HOUR_LABELS = [(0, "00:00"), (4, "04:00"), (8, "08:00"),
                   (12, "12:00"), (16, "16:00"), (20, "20:00")]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._bars: list[tuple[int, float, float, bool]] = []  # (weekday, start_h, hours, is_today)
        self._wd_labels = ["一", "二", "三", "四", "五", "六", "日"]
        self.setMinimumHeight(210)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, bars: list[tuple[int, float, float, bool]],
                 wd_labels: list[str] | None = None) -> None:
        self._bars = list(bars)
        if wd_labels:
            self._wd_labels = list(wd_labels)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 40, 10, 8, 22
        plot_w = max(10, w - pad_l - pad_r)
        plot_h = max(10, h - pad_t - pad_b)

        f = QFont()
        f.setPixelSize(10)
        p.setFont(f)

        # 横向网格
        p.setPen(QPen(_c("border"), 1))
        for hh, _ in self.HOUR_LABELS:
            y = pad_t + plot_h * (hh / 24.0)
            p.drawLine(QPointF(pad_l, y), QPointF(pad_l + plot_w, y))
        p.setPen(_c("muted"))
        for hh, lab in self.HOUR_LABELS:
            y = pad_t + plot_h * (hh / 24.0)
            p.drawText(QRectF(0, y - 8, pad_l - 6, 16),
                       Qt.AlignRight | Qt.AlignVCenter, lab)

        n = len(self._wd_labels)
        col_w = plot_w / max(n, 1)
        # 今日列高亮
        for i in range(n):
            if self._wd_labels[i] == "今":
                p.fillRect(QRectF(pad_l + col_w * i, pad_t, col_w, plot_h),
                           QColor(theme.get("surface_hi")))
        p.setPen(_c("muted"))
        for i, lab in enumerate(self._wd_labels):
            p.drawText(QRectF(pad_l + col_w * i, pad_t + plot_h + 3, col_w, 16),
                       Qt.AlignCenter, lab)

        bar_h = max(5.0, col_w * 0.24)
        for wd, sh, hours, is_today in self._bars:
            x = pad_l + col_w * wd + col_w * 0.14
            bw = col_w * 0.72
            y = pad_t + plot_h * (sh / 24.0)
            bh = max(bar_h, plot_h * (hours / 24.0))
            color = QColor(theme.get("focus"))
            if not is_today:
                color.setAlpha(110)
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(x, y, bw, bh), bar_h / 2, bar_h / 2)


# ---------------------------------------------------------------------------
# 按小时柱状（最佳专注时间）
# ---------------------------------------------------------------------------
def _fmt_axis_minutes(m: float) -> str:
    m = int(round(m))
    if m < 60:
        return f"{m}m"
    hh, mm = divmod(m, 60)
    return f"{hh}h{mm}m" if mm else f"{hh}h"


# y 轴步长阶梯（分钟）：先 1/2/5 分钟，再 10/15/30 分钟，
# 然后整小时、整两小时、整三小时…… 保证刻度永远落在好读的时间点上。
# 刻意不收 20 / 90 —— 它们会造出 ``1h20m`` / ``1h30m`` / ``4h30m`` 这类刻度，
# 换成 15 / 30 / 120 之后整张轴都是整点或半点。
_AXIS_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 180, 240,
               300, 360, 480, 600, 720, 1440)


def _nice_axis(vmax: float, max_ticks: int = 6,
               floor: float = 20.0) -> tuple[float, float]:
    """把「数据最大分钟数」换算成易读的 y 轴，返回 ``(轴顶值, 步长)``。

    之前是 ``nice = vmax * 1.12`` 再硬性等分，于是刻度出现
    ``1h13m / 2h26m / 3h38m`` 这种没人读得出来的值。改成从步长阶梯里
    挑一个使刻度数不超过 ``max_ticks`` 的步长，轴顶取步长的整数倍，
    刻度就永远是 ``0m / 1h / 2h…`` 这类整点时间。
    """
    vmax = max(float(vmax), float(floor))
    target = vmax * 1.05          # 留 5% 顶部余量，柱子不顶到最上面的网格线
    for step in _AXIS_STEPS:
        ticks = math.ceil(target / step)
        if ticks <= max_ticks:
            return float(step * ticks), float(step)
    step = _AXIS_STEPS[-1]        # 兜底：极端大的值按 24h 一档
    return float(step * math.ceil(target / step)), float(step)


class HourBarChart(QWidget):
    """24 小时柱状图，y 轴刻度按分钟数格式化。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._values: list[float] = [0] * 24
        self._ticks = 6
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, values: list[float]) -> None:
        vals = list(values)[:24]
        while len(vals) < 24:
            vals.append(0)
        self._values = vals
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 52, 10, 10, 22
        plot_w = max(10, w - pad_l - pad_r)
        plot_h = max(10, h - pad_t - pad_b)

        vmax = max(max(self._values), 1.0)
        nice, step = _nice_axis(vmax, self._ticks)
        ticks = int(round(nice / step)) or 1
        f = QFont()
        f.setPixelSize(10)
        p.setFont(f)

        p.setPen(QPen(_c("border"), 1))
        for i in range(ticks + 1):
            y = pad_t + plot_h * (1 - i / ticks)
            p.drawLine(QPointF(pad_l, y), QPointF(pad_l + plot_w, y))
        p.setPen(_c("muted"))
        for i in range(ticks + 1):
            y = pad_t + plot_h * (1 - i / ticks)
            p.drawText(QRectF(0, y - 8, pad_l - 6, 16),
                       Qt.AlignRight | Qt.AlignVCenter,
                       _fmt_axis_minutes(step * i))

        slot = plot_w / 24.0
        bar_w = slot * 0.62
        for i, v in enumerate(self._values):
            if v <= 0:
                continue
            bh = plot_h * (v / nice)
            x = pad_l + slot * i + (slot - bar_w) / 2
            y = pad_t + plot_h - bh
            p.setPen(Qt.NoPen)
            p.setBrush(_c("focus"))
            p.drawRoundedRect(QRectF(x, y, bar_w, max(bh, 3)), 2, 2)

        for i in range(0, 24, 3):
            x = pad_l + slot * i
            p.setPen(_c("muted"))
            p.drawText(QRectF(x - slot, pad_t + plot_h + 3, slot * 2 + bar_w, 16),
                       Qt.AlignCenter, f"{i:02d}:00")


# ---------------------------------------------------------------------------
# 年度热力图
# ---------------------------------------------------------------------------
HEAT_LEVELS = ["#eef0f5", "#c9daff", "#8fb0ff", "#5c86f7", "#3a5fd9"]
HEAT_DARK = ["#242433", "#243055", "#2f4a90", "#3d63c9", "#5b86ff"]
HEAT_LABELS = ["0m", "0-1h", "1h-3h", "3h-5h", ">5h"]


class HeatmapChart(QWidget):
    """年度热力图：列 = 周，行 = 周一~周日。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._data: dict[str, int] = {}
        self._start = None
        self._end = None
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, data: dict[str, int], start, end) -> None:
        self._data = dict(data)
        self._start = start
        self._end = end
        self.update()

    def _level(self, minutes: int) -> int:
        if minutes <= 0:
            return 0
        if minutes < 60:
            return 1
        if minutes < 180:
            return 2
        if minutes < 300:
            return 3
        return 4

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if self._start is None or self._end is None:
            return
        from datetime import date as _date, timedelta as _td
        start = self._start
        end = self._end
        # 对齐到周一
        start = start - _td(days=start.weekday())
        weeks = ((end - start).days // 7) + 1

        legend_h = 22
        pad_t = 16
        pad_l = 4
        avail_w = w - pad_l - 8
        avail_h = h - pad_t - legend_h
        cell = max(4.0, min(avail_w / max(weeks, 1), avail_h / 7) - 1.6)
        gap = 1.6

        palette = HEAT_DARK if theme.is_dark() else HEAT_LEVELS
        f = QFont()
        f.setPixelSize(10)
        p.setFont(f)

        # 月份标签
        p.setPen(_c("muted"))
        last_month = -1
        for i in range(weeks):
            d = start + _td(days=i * 7)
            if d.month != last_month and d.day <= 7:
                last_month = d.month
                p.drawText(QRectF(pad_l + i * (cell + gap), 0, 40, 14),
                           Qt.AlignLeft, f"{d.month}月")

        for i in range(weeks):
            for j in range(7):
                d = start + _td(days=i * 7 + j)
                if d > end:
                    continue
                lv = self._level(self._data.get(d.isoformat(), 0))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(palette[lv]))
                p.drawRoundedRect(
                    QRectF(pad_l + i * (cell + gap), pad_t + j * (cell + gap),
                           cell, cell), 1.6, 1.6)

        # 图例（右下）
        f.setPixelSize(10)
        p.setFont(f)
        lx = w - 8
        ly = h - 15
        for i in range(len(palette) - 1, -1, -1):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(palette[i]))
            p.drawRoundedRect(QRectF(lx - 9, ly, 9, 9), 1.6, 1.6)
            lx -= 9
            label = HEAT_LABELS[i]
            p.setPen(_c("muted"))
            tw = p.fontMetrics().horizontalAdvance(label) + 8
            p.drawText(QRectF(lx - tw, ly - 3, tw, 15),
                       Qt.AlignRight | Qt.AlignVCenter, label)
            lx -= tw + 4


# ---------------------------------------------------------------------------
# 小工具：卡片 / 图标按钮
# ---------------------------------------------------------------------------
def icon_button(text: str, tip: str = "", size: int = 30,
                obj: str = "FocusIconBtn") -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName(obj)
    btn.setFixedSize(size, size)
    btn.setCursor(Qt.PointingHandCursor)
    if tip:
        btn.setToolTip(tip)
    return btn


def plain_card() -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("FocusCard")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(10)
    return card, lay


# ---------------------------------------------------------------------------
# 通用柱状图（y 轴按分钟格式化）
# ---------------------------------------------------------------------------
class BarSeries(QWidget):
    """轻量柱状图：y 轴刻度按分钟格式化，底部标签。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._values: list[float] = []
        self._labels: list[str] = []
        self._ticks = 6
        self._color = "focus"
        self._bar_ratio = 0.42
        self._radius = 3.0
        self._highlight = -1
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, values: list[float], labels: list[str] | None = None,
                 color: str = "focus", highlight: int = -1) -> None:
        self._values = list(values)
        self._labels = list(labels) if labels else [""] * len(values)
        self._color = color
        self._highlight = highlight
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 52, 8, 8, 22
        plot_w = max(10, w - pad_l - pad_r)
        plot_h = max(10, h - pad_t - pad_b)
        f = QFont()
        f.setPixelSize(10)
        p.setFont(f)

        vmax = max(max(self._values), 1.0) if self._values else 1.0
        nice, step = _nice_axis(vmax, self._ticks)
        ticks = int(round(nice / step)) or 1
        p.setPen(QPen(_c("border"), 1))
        for i in range(ticks + 1):
            y = pad_t + plot_h * (1 - i / ticks)
            p.drawLine(QPointF(pad_l, y), QPointF(pad_l + plot_w, y))
        p.setPen(_c("muted"))
        for i in range(ticks + 1):
            y = pad_t + plot_h * (1 - i / ticks)
            p.drawText(QRectF(0, y - 8, pad_l - 6, 16),
                       Qt.AlignRight | Qt.AlignVCenter,
                       _fmt_axis_minutes(step * i))

        n = len(self._values)
        if n == 0:
            return
        slot = plot_w / n
        bar_w = min(slot * self._bar_ratio, 40.0)
        for i, v in enumerate(self._values):
            x = pad_l + slot * i + (slot - bar_w) / 2
            if v > 0:
                bh = plot_h * (v / nice)
                y = pad_t + plot_h - bh
                color = _c(self._color)
                if self._highlight >= 0 and i != self._highlight:
                    color.setAlpha(90)
                p.setPen(Qt.NoPen)
                p.setBrush(color)
                p.drawRoundedRect(QRectF(x, y, bar_w, max(bh, 3)),
                                  self._radius, self._radius)
            if i < len(self._labels):
                p.setPen(_c("muted"))
                p.drawText(QRectF(pad_l + slot * i, pad_t + plot_h + 3, slot, 16),
                           Qt.AlignCenter, self._labels[i])


# ---------------------------------------------------------------------------
# 总览页：趋势小图 / 本周打卡环
# ---------------------------------------------------------------------------
class MiniTrend(QWidget):
    """统计页「总览」的四张趋势小图。

    两种画法（参考图里同一页并存）：

    ``line`` —— 折线 + 线下渐变面积 + 空心圆点，带虚线网格（最近已完成、最近番茄数）
    ``bar``  —— 每天一根整高浅灰轨道，前景细柱从底长出，无网格（最近完成率、最近专注时长）

    ``unit`` 决定 y 轴刻度怎么格式化：count 整数 / percent 固定 0~100% / minutes 时长。
    两种画法共用同一套边距与刻度代码，只差网格和柱子怎么画。
    """

    def __init__(self, style: str = "line", unit: str = "count",
                 zoom: bool = False, divisions: int = 6,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._style = style
        self._unit = unit
        # zoom：y 轴贴着数据范围画（成就值那种「累计分」几乎是一条平线，
        # 从 0 起画的话涨的那点全压在最顶上看不见）。默认从 0 起。
        self._zoom = zoom
        # 网格几格：常用专注页那张柱状图参考图只画 3 格（0/20m/40m/1h），
        # 6 格会把标签挤成一列小字。
        self._divisions = divisions
        self._values: list[float] = []
        self._labels: list[str] = []
        self._highlight = -1
        # 悬停读数：柱子/折线本身读不出「这天到底多少」，鼠标过去要能报数
        self.setMouseTracking(True)
        self._hover = -1
        self._geo = (0.0, 0.0)          # (plot 左边界, 每格宽)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, values: list[float], labels: list[str] | None = None,
                 highlight: int = -1) -> None:
        self._values = list(values)
        self._labels = list(labels) if labels else [""] * len(values)
        self._highlight = highlight
        self._hover = -1
        self.update()

    # -- 悬停 ----------------------------------------------------------
    def _index_at(self, x: float) -> int:
        pad_l, slot = self._geo
        if slot <= 0:
            return -1
        i = int((x - pad_l) // slot)
        return i if 0 <= i < len(self._values) else -1

    def _tip(self, i: int) -> str:
        v = self._values[i]
        if self._unit == "percent":
            txt = f"{v:.0f}%"
        elif self._unit == "minutes":
            txt = _fmt_axis_minutes(v)
        elif self._unit == "score":
            txt = f"{int(round(v)):,}"
        else:
            txt = f"{v:g} 个"
        label = self._labels[i] if i < len(self._labels) else ""
        return f"{label} · {txt}" if label else txt

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        i = self._index_at(event.position().x())
        if i != self._hover:
            self._hover = i
            self.update()
        if i >= 0:
            QToolTip.showText(event.globalPosition().toPoint(), self._tip(i), self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(event)

    def _axis(self, vmax: float) -> tuple[float, float]:
        if self._unit == "percent":
            return 100.0, 20.0
        # 计数轴的地板值取 1（4 个任务就该是 0~5 的轴，而不是被时长轴的
        # floor 拉成 0~25）；时长轴地板取一小时——全 0 的时候参考图也是
        # 画到 1h，画到 5m 会让整张图看起来像坏了。
        floor = 1.0 if self._unit == "count" else 60.0
        return _nice_axis(vmax, self._divisions, floor)

    def _fmt(self, v: float) -> str:
        if self._unit == "percent":
            return f"{int(round(v))}%"
        if self._unit == "count":
            return str(int(round(v)))
        if self._unit == "score":
            return f"{int(round(v)):,}"
        return _fmt_axis_minutes(v)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 40, 10, 12, 22
        plot_w = max(10, w - pad_l - pad_r)
        plot_h = max(10, h - pad_t - pad_b)
        n = len(self._values)
        if n == 0:
            return

        f = QFont()
        f.setPixelSize(10)
        p.setFont(f)
        label_col = _neutral("#b6b6b6", "#6e6e86")
        vmax = max(max(self._values), 1.0) if self._values else 1.0
        if self._zoom and self._values:
            # y 轴贴着数据范围：成就值这种累计分几乎平，从 0 起画涨的那点
            # 全压在最顶上看不见。上下各留 12% 余量，刻度取整到易读步长。
            vmin = min(self._values)
            span = max(vmax - vmin, 1.0)
            _, step = _nice_axis(span, 5, 1.0)
            lo = math.floor((vmin - span * 0.12) / step) * step
            hi = math.ceil((vmax + span * 0.12) / step) * step
            if hi <= lo:
                hi = lo + step
        else:
            lo = 0.0
            hi, step = self._axis(vmax)
        ticks = max(int(round((hi - lo) / step)), 1)

        def y_of(v):
            return pad_t + plot_h * (1 - (v - lo) / (hi - lo))

        grid = QPen(_neutral("#f5f5f5", "#262633"), 1)
        grid.setStyle(Qt.PenStyle.CustomDashLine)
        grid.setDashPattern([3, 4])
        for i in range(ticks + 1):
            y = y_of(lo + step * i)
            if self._style == "line":
                p.setPen(grid)
                p.drawLine(QPointF(pad_l, y), QPointF(pad_l + plot_w, y))
            p.setPen(label_col)
            p.drawText(QRectF(0, y - 8, pad_l - 8, 16),
                       Qt.AlignRight | Qt.AlignVCenter, self._fmt(lo + step * i))

        slot = plot_w / n
        self._geo = (pad_l, slot)
        accent = _c("focus")
        if self._style == "bar":
            track = _neutral("#f6f6f6", "#262633")
            hover_track = _neutral("#e9e9e9", "#34343f")
            bar_w = 4.0
            for i, v in enumerate(self._values):
                cx = pad_l + slot * i + slot / 2
                x = cx - bar_w / 2
                p.setPen(Qt.NoPen)
                p.setBrush(hover_track if i == self._hover else track)
                p.drawRoundedRect(QRectF(x, pad_t, bar_w, plot_h), 2, 2)
                if v > 0:
                    by = y_of(v)
                    bh = max((pad_t + plot_h) - by, 3.0)
                    p.setBrush(accent)
                    p.drawRoundedRect(QRectF(x, by, bar_w, bh), 2, 2)
        else:
            pts = [QPointF(pad_l + slot * i + slot / 2, y_of(v))
                   for i, v in enumerate(self._values)]
            base = pad_t + plot_h
            area = QPolygonF(pts + [QPointF(pts[-1].x(), base),
                                    QPointF(pts[0].x(), base)])
            grad = QLinearGradient(0, pad_t, 0, base)
            top_col = QColor(accent)
            top_col.setAlpha(56)
            zero_col = QColor(accent)
            zero_col.setAlpha(0)
            grad.setColorAt(0.0, top_col)
            grad.setColorAt(1.0, zero_col)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(grad))
            p.drawPolygon(area)
            p.setPen(QPen(accent, 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for i in range(1, len(pts)):
                p.drawLine(pts[i - 1], pts[i])
            for i, pt in enumerate(pts):
                hovered = i == self._hover
                p.setPen(QPen(accent, 1.6))
                if hovered:
                    p.setBrush(accent)          # 悬停的那个点实心，好定位
                else:
                    # 空心点要透出卡片底色，取 surface 而不是写死白，暗色下才不留白斑
                    p.setBrush(QColor(theme.get("surface")))
                r = 4.2 if hovered else 3.0
                p.drawEllipse(pt, r, r)

        for i in range(n):
            if i >= len(self._labels) or not self._labels[i]:
                continue
            p.setPen(_c("focus") if i == self._highlight else label_col)
            p.drawText(QRectF(pad_l + slot * i, pad_t + plot_h + 4, slot, 16),
                       Qt.AlignCenter, self._labels[i])


class WeekRings(QWidget):
    """本周打卡进展：七个细环，进度 = 当天任务完成率。

    参考图这一格是**周日起**（日一二三四五六），和统计页其它按周一开算的
    周视图不一致 —— 这里跟着参考图走，不套用 ``WD_LABELS``。
    """

    LABELS = ["日", "一", "二", "三", "四", "五", "六"]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._ratios: list[float] = [0.0] * 7
        self._start = None
        self._counts: list[tuple[int, int]] | None = None
        self.setMouseTracking(True)
        self._hover = -1
        # 参考图实测：环外径 42 逻辑像素、描边 5.5、下面 16 的星期标签
        self.setMinimumHeight(72)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(self, ratios: list[float], start=None,
                 counts: list[tuple[int, int]] | None = None) -> None:
        vals = list(ratios)[:7]
        while len(vals) < 7:
            vals.append(0.0)
        self._ratios = [max(0.0, min(float(v), 100.0)) for v in vals]
        self._start = start
        self._counts = list(counts)[:7] if counts else None
        self._hover = -1
        self.update()

    # -- 悬停：环上只看得出「有没有打卡」，日期和完成率得报出来 ------
    def _index_at(self, x: float) -> int:
        i = int(x / (self.width() / 7.0)) if self.width() else -1
        return i if 0 <= i < 7 else -1

    def _tip(self, i: int) -> str:
        from datetime import date, timedelta
        ratio = f"完成率 {self._ratios[i]:.0f}%"
        if self._start is None:
            return f"周{self.LABELS[i]} · {ratio}"
        d = self._start + timedelta(days=i)
        day = f"{d.month}月{d.day}日"
        if d > date.today():
            return f"{day} · 还没到"
        if self._counts:
            done, total = self._counts[i]
            # 「空环」有两种：那天没安排任务，和安排了但一个没做完。
            # 只报完成率会把前者说成后者。
            if not total:
                return f"{day} · 没安排任务"
            return f"{day} · 完成 {done}/{total}"
        return f"{day} · {ratio}"

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        i = self._index_at(event.position().x())
        if i != self._hover:
            self._hover = i
            self.update()
        if i >= 0:
            QToolTip.showText(event.globalPosition().toPoint(), self._tip(i), self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        slot = w / 7.0
        pen_w = 5.5
        # drawEllipse 的半径是描边中心线，所以外径 = r + pen_w/2
        r = max(8.0, min(21.0 - pen_w / 2, slot * 0.34))
        cy = r + pen_w / 2 + 2
        f = QFont()
        f.setPixelSize(11)
        p.setFont(f)
        for i, ratio in enumerate(self._ratios):
            cx = slot * i + slot / 2
            rect = QRectF(cx - r, cy - r, r * 2, r * 2)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_neutral("#ebebee", "#34343f") if i == self._hover
                          else _neutral("#f2f2f2", "#2c2c3a"), pen_w))
            p.drawEllipse(rect)
            if ratio > 0:
                # drawArc 的角度单位是 1/16 度，不是度
                span = int(round(360 * 16 * ratio / 100))
                p.setPen(QPen(_c("focus"), pen_w, Qt.SolidLine, Qt.RoundCap))
                p.drawArc(rect, 90 * 16, -span)
            p.setPen(_neutral("#a3a3a3", "#7c7c92"))
            p.drawText(QRectF(slot * i, cy + r + 4, slot, 16),
                       Qt.AlignCenter, self.LABELS[i])


# ---------------------------------------------------------------------------
# 记录行左侧轨道（圆点 + 竖线）
# ---------------------------------------------------------------------------
class RecordRail(QWidget):
    """专注记录左侧时间线。

    一条记录 = 一行时间（蓝底番茄图标）+ N 行任务（空心蓝圈），
    圈与圈之间用浅蓝竖线连起来；同一日期分组内，最后一行还要把线
    拖到下一条记录，让整组看起来是一条不断的轴（参考图就是这样）。
    """

    LINE_H = 27          # 每行高度（参考图实测 40 物理 px / 150% 缩放）
    GAP_H = 15           # 记录之间的空隙
    COL_W = 21           # 图标列宽（参考图：圆心距侧栏左缘 26 逻辑像素）

    def __init__(self, task_lines: int, tail: bool = False,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._task_lines = max(1, int(task_lines))
        self._tail = tail
        lines = 1 + self._task_lines
        self.setFixedWidth(self.COL_W)
        self.setFixedHeight(lines * self.LINE_H + (self.GAP_H if tail else 0))

    def dot_y(self, idx: int) -> float:
        """第 idx 行（0 = 时间行）的圆心 y 坐标。"""
        return idx * self.LINE_H + self.LINE_H / 2.0

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx = self.width() / 2.0
        accent = _c("focus")
        n = 1 + self._task_lines

        # 连接线：从第一个圆心连到最后一个圆心（tail 时再拖过记录间距）
        p.setPen(QPen(_c("focus_soft"), 1.6))
        last_y = self.dot_y(n - 1)
        end_y = self.height() if self._tail else last_y
        p.drawLine(QPointF(cx, self.dot_y(0)), QPointF(cx, end_y))

        # 时间行：淡蓝圆盘 + 盘中番茄
        tomato_disc(p, cx, self.dot_y(0), 10.5, accent)

        # 任务行：空心圈
        p.setPen(QPen(accent, 1.4))
        p.setBrush(_c("bg_alt"))
        for i in range(1, n):
            p.drawEllipse(QPointF(cx, self.dot_y(i)), 3.6, 3.6)


# ---------------------------------------------------------------------------
# 沉浸模式页码圆点
# ---------------------------------------------------------------------------
class PageDots(QWidget):
    changed = Signal(int)

    def __init__(self, count: int = 2, parent: QWidget | None = None):
        super().__init__(parent)
        self._count = count
        self._index = 0
        self.setFixedHeight(16)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(count * 20)

    def set_index(self, idx: int) -> None:
        if idx != self._index:
            self._index = idx
            self.update()

    def _hit(self, pos) -> int:
        step = 20
        total = self._count * step
        x0 = (self.width() - total) / 2 + step / 2
        for i in range(self._count):
            if abs(pos.x() - (x0 + i * step)) <= 10:
                return i
        return -1

    def mousePressEvent(self, event) -> None:  # noqa: N802
        idx = self._hit(event.position())
        if idx >= 0 and idx != self._index:
            self._index = idx
            self.update()
            self.changed.emit(idx)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        step = 20
        x0 = (self.width() - self._count * step) / 2 + step / 2
        cy = self.height() / 2
        p.setPen(Qt.NoPen)
        for i in range(self._count):
            p.setBrush(QColor("#e8e8ea") if i == self._index else QColor("#4a4a52"))
            r = 3.4 if i == self._index else 2.8
            p.drawEllipse(QPointF(x0 + i * step, cy), r, r)


# ---------------------------------------------------------------------------
# 统计页 KPI 单元
# ---------------------------------------------------------------------------
class KpiCell(QWidget):
    """一格 KPI：标签 + 数值 + 副文案。

    ``value_first=True`` 时把数值放在最上面（滴答清单「统计 → 任务 → 概览」
    就是这个排法：大号数字在上，名称在下），默认仍是「标签在上」。
    副文案后面可以挂一个趋势箭头（``set_value(..., trend="up")``），
    箭头单独着色，和灰色的文案区分开。
    """

    def __init__(self, label: str, parent: QWidget | None = None,
                 value_first: bool = False):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)
        self.label = QLabel(label)
        self.label.setObjectName("FocusKpiLabel")
        self.label.setAlignment(Qt.AlignCenter)
        self.value = QLabel("0")
        self.value.setObjectName("FocusKpiValue")
        self.value.setAlignment(Qt.AlignCenter)
        self.sub = QLabel(" ")
        self.sub.setObjectName("FocusKpiSub")
        self.arrow = QLabel("")
        self.arrow.setObjectName("FocusKpiSub")
        self.arrow.hide()
        self.sub_row = QWidget()
        srow = QHBoxLayout(self.sub_row)
        srow.setContentsMargins(0, 0, 0, 0)
        srow.setSpacing(3)
        srow.addStretch(1)
        srow.addWidget(self.sub)
        srow.addWidget(self.arrow)
        srow.addStretch(1)

        order = (self.value, self.label) if value_first else (self.label, self.value)
        for w in order:
            lay.addWidget(w)
        lay.addWidget(self.sub_row)

    def set_value(self, value: str, sub: str = " ", trend: str = "") -> None:
        self.value.setText(value)
        self.sub.setText(sub)
        if trend in ("up", "down"):
            self.arrow.setText("↑" if trend == "up" else "↓")
            self.arrow.setObjectName("FocusSubUp" if trend == "up" else "FocusSubDown")
            self.arrow.show()
        else:
            self.arrow.hide()
        # 改了 objectName 必须重新抛光，否则新样式不生效
        self.arrow.style().unpolish(self.arrow)
        self.arrow.style().polish(self.arrow)


# ---------------------------------------------------------------------------
# 小标题行（卡片头：标题 + 右侧控件）
# ---------------------------------------------------------------------------
def card_header(title: str) -> tuple[QWidget, QHBoxLayout]:
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    lbl = QLabel(title)
    lbl.setObjectName("FocusCardTitle")
    lay.addWidget(lbl)
    lay.addStretch(1)
    return row, lay

