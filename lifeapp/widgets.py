"""通用 UI 组件：卡片、统计卡、标签、图表、环形进度。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QPointF, QPoint, QSize, QObject, QEvent, QDate
from PySide6.QtGui import (
    QColor, QPainter, QPainterPath, QPen, QFont, QLinearGradient, QPolygonF,
)
from PySide6.QtWidgets import (
    QFrame, QLabel, QVBoxLayout, QHBoxLayout, QWidget, QSizePolicy,
    QAbstractSpinBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit, QComboBox,
    QLineEdit, QDialog, QCalendarWidget, QListWidget,
)

from . import theme


class _NoWheelFilter(QObject):
    """拦截滚轮事件：鼠标悬停在输入控件上时滚动不再改值（需点击/键盘才调整）。"""

    def eventFilter(self, obj, event):  # noqa: ANN001
        if event.type() == QEvent.Type.Wheel:
            return True
        return False


_no_wheel_filter = _NoWheelFilter()


def disable_wheel(widget: QWidget) -> QWidget:
    """禁用控件上的滚轮操作（QSpinBox / QComboBox / QDateEdit 等）。"""
    widget.installEventFilter(_no_wheel_filter)
    return widget


# ---------- 输入控件：自绘 QSS 接管子控件后不再绘制的按钮箭头 ----------


class _SpinButtonMixin:
    """QAbstractSpinBox 系列的上下调节三角自绘。

    QSS 一旦声明 ::up-button/::down-button，Qt 便不再绘制默认箭头，
    这里按与 QSS 相同的几何（右侧 20px）补画，保证按钮可见可点。
    """

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        if self.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(_c("muted"))
        w, h = self.width(), self.height()
        cx = w - (self._dd_width() + 10)
        t = 4.0
        for cy, flip in ((h * 0.27, False), (h * 0.73, True)):
            path = QPainterPath()
            s = -1.0 if flip else 1.0
            path.moveTo(cx - t, cy - s * t * 0.55)
            path.lineTo(cx + t, cy - s * t * 0.55)
            path.lineTo(cx, cy + s * t * 0.6)
            path.closeSubpath()
            p.drawPath(path)

    def _dd_width(self) -> float:
        """右侧下拉区宽度（与 QSS ::drop-down 的 width 保持一致）。"""
        return 0.0


class DateEdit(_SpinButtonMixin, QDateEdit):
    """日期框：右侧自绘日历图标，点击弹出日历；可直接键入或方向键微调。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCalendarPopup(True)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setDisplayFormat("yyyy-MM-dd")
        self.setCursor(Qt.PointingHandCursor)

    def _dd_width(self) -> float:
        return 26.0

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        s = 13.0
        x = w - 26 + (26 - s) / 2
        y = (h - s) / 2
        col = _c("muted")
        p.setPen(QPen(col, 1.4))
        p.setBrush(Qt.NoBrush)
        # 日历主体 + 顶部横线 + 两个挂耳
        p.drawRoundedRect(QRectF(x, y + 2, s, s - 2), 2.5, 2.5)
        p.drawLine(QPointF(x, y + 5.2), QPointF(x + s, y + 5.2))
        p.drawLine(QPointF(x + 3.2, y), QPointF(x + 3.2, y + 3.2))
        p.drawLine(QPointF(x + s - 3.2, y), QPointF(x + s - 3.2, y + 3.2))
        # 两个日期格子示意点
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(x + s * 0.35, y + 9), 1.1, 1.1)
        p.drawEllipse(QPointF(x + s * 0.68, y + 9), 1.1, 1.1)


class DateInput(QLineEdit):
    """自由文本日期输入 + 右侧日历图标（点击弹出日历）。

    替代 QDateEdit 的段式键入（光标卡在分隔符、清空某段后显示残缺）：
    - 直接键入 2026-09-09 / 2026/9/9 / 9-9 等，失焦自动规范化为 yyyy-MM-dd；
    - 解析失败回退上一个合法值，不会带着非法日期保存；
    - date()/setDate(QDate) 与 QDateEdit 调用方接口兼容
      （空文本 = invalid QDate；2000-01-01 哨兵按「无日期」处理为空文本）。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._valid_text = ""  # 上一次合法值（解析失败时回退）
        self.setTextMargins(0, 0, 26, 0)  # 右侧为日历图标留白
        self.textChanged.connect(self._remember_if_valid)

    # ---- 兼容 QDateEdit 的调用接口（no-op） ----
    def setCalendarPopup(self, _on: bool) -> None:  # noqa: N802
        pass

    def setDisplayFormat(self, _fmt: str) -> None:  # noqa: N802
        pass

    def setButtonSymbols(self, _sym) -> None:  # noqa: ANN001, N802
        pass

    def setMinimumDate(self, _d: QDate) -> None:  # noqa: N802
        pass

    def setSpecialValueText(self, text: str) -> None:  # noqa: N802
        self.setPlaceholderText(text)

    # ---- 值读写 ----
    def _parse(self, text: str) -> QDate:
        t = text.strip()
        if not t:
            return QDate()  # invalid = 无日期
        for fmt in ("yyyy-MM-dd", "yyyy/M/d", "yyyy.M.d", "yyyy年M月d日"):
            d = QDate.fromString(t, fmt)
            if d.isValid():
                return d
        for fmt in ("M/d", "M-d"):
            d = QDate.fromString(t, fmt)
            if d.isValid():
                return QDate(QDate.currentDate().year(), d.month(), d.day())
        return QDate()

    def date(self) -> QDate:
        return self._parse(self.text())

    def setDate(self, d: QDate) -> None:  # noqa: N802
        if not d.isValid() or d.year() <= 2000:  # 2000-01-01 = 「无日期」哨兵
            self._valid_text = ""
            self.setText("")
            return
        self._valid_text = d.toString("yyyy-MM-dd")
        self.setText(self._valid_text)

    def _remember_if_valid(self, text: str) -> None:
        if self._parse(text).isValid() or not text.strip():
            self._valid_text = text

    # ---- 交互 ----
    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._normalize_text()
        super().focusOutEvent(event)

    def _normalize_text(self) -> None:
        """失焦时把自由文本规范化为 yyyy-MM-dd；解析失败回退合法值。"""
        d = self._parse(self.text())
        if d.isValid():
            self.setText(d.toString("yyyy-MM-dd"))
        elif self.text().strip():
            self.setText(self._valid_text)  # 非法输入回退

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and \
                event.position().x() >= self.width() - 26:
            self._show_calendar()
            return
        super().mousePressEvent(event)

    def _show_calendar(self) -> None:
        cur = self.date()
        # 三个标志缺一不可（见 focus_ui.POPUP_FLAGS）：只 Frameless 不去了投影，
        # 圆角外面还是会糊一圈深灰边
        popup = QDialog(self, Qt.Popup | Qt.FramelessWindowHint
                        | Qt.NoDropShadowWindowHint)
        lay = QVBoxLayout(popup)
        lay.setContentsMargins(0, 0, 0, 0)
        cal = QCalendarWidget()
        cal.setSelectedDate(cur if cur.isValid() else QDate.currentDate())
        cal.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        cal.setGridVisible(False)
        cal.clicked.connect(lambda q: (self.setDate(q), popup.close()))
        lay.addWidget(cal)
        popup.adjustSize()
        popup.move(self.mapToGlobal(QPoint(0, self.height())))
        popup.exec()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        s = 13.0
        x = w - 26 + (26 - s) / 2
        y = (h - s) / 2
        col = _c("muted")
        p.setPen(QPen(col, 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(x, y + 2, s, s - 2), 2.5, 2.5)
        p.drawLine(QPointF(x, y + 5.2), QPointF(x + s, y + 5.2))
        p.drawLine(QPointF(x + 3.2, y), QPointF(x + 3.2, y + 3.2))
        p.drawLine(QPointF(x + s - 3.2, y), QPointF(x + s - 3.2, y + 3.2))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(x + s * 0.35, y + 9), 1.1, 1.1)
        p.drawEllipse(QPointF(x + s * 0.68, y + 9), 1.1, 1.1)


class TimeEdit(_SpinButtonMixin, QTimeEdit):
    """时间框：右侧自绘上下调节三角。"""


class SpinBox(_SpinButtonMixin, QSpinBox):
    """整数调节框：右侧自绘上下调节三角。"""


class DoubleSpinBox(_SpinButtonMixin, QDoubleSpinBox):
    """小数调节框：右侧自绘上下调节三角。"""


class ComboBox(QComboBox):
    """下拉框：QSS 接管 ::drop-down 后自绘下拉箭头。"""

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(_c("muted"))
        w, h = self.width(), self.height()
        cx, cy = w - 12, h / 2
        t = 4.5
        path = QPainterPath()
        path.moveTo(cx - t, cy - t * 0.5)
        path.lineTo(cx + t, cy - t * 0.5)
        path.lineTo(cx, cy + t * 0.5)
        path.closeSubpath()
        p.drawPath(path)


def _c(name: str) -> QColor:
    return QColor(theme.get(name))


def _axis_fmt(v: float) -> str:
    """y 轴刻度格式化：完整数值 + 千分位 + 两位小数。

    不做「万 / k / M」缩写——缩写会把 16.0万 这类相邻刻度抹成同一个标签，
    导致数值的细微变化在 y 轴上完全看不出来。
    """
    return f"{v:,.2f}"


def _apply_property(widget: QWidget, name: str, value: str) -> None:
    """设置动态属性并刷新样式，使 QSS 属性选择器生效。"""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def drop_widget(widget: QWidget) -> None:
    """把控件从布局里摘掉并销毁 —— 必须先 hide 再 setParent(None)。

    对一个正显示着的控件调 `setParent(None)`，它当场就成了顶层窗口：Qt 会给它
    建一个带标题栏的空窗，而 `deleteLater` 要到下一轮事件循环才跑到，
    用户看到的就是「一瞬间弹出一堆空窗口」（重建详情子任务 / 重建列表行时）。
    """
    widget.hide()
    widget.setParent(None)
    widget.deleteLater()


def _height_at(row: QWidget, w: int) -> int:
    """这行被限定成宽度 w 时到底要多高。

    不能读 row.sizeHint().height()：开了 wordWrap 的 QLabel，它的 sizeHint
    算的是「整句不折行需要多宽」那一版，跟当前宽度无关 —— 于是窄栏里行高少算
    一截，最后一行被裁掉。只能逐个问子控件「宽度限定成这么多时你要多高」。
    """
    lay = row.layout()
    if lay is None:
        return row.sizeHint().height()
    m = lay.contentsMargins()
    inner = max(w - m.left() - m.right(), 20)
    heights: list[int] = []
    for j in range(lay.count()):
        item = lay.itemAt(j)
        if item is None:
            continue
        wid = item.widget()
        if wid is not None and wid.isVisible():
            heights.append(wid.heightForWidth(inner)
                           if wid.hasHeightForWidth()
                           else wid.sizeHint().height())
        elif item.layout() is not None:
            heights.append(item.layout().sizeHint().height())
    if not heights:
        return row.sizeHint().height()
    return max(m.top() + m.bottom()
               + sum(heights) + lay.spacing() * (len(heights) - 1),
               row.sizeHint().height())


class FittingList(QListWidget):
    """行宽始终等于视口宽的列表。

    QListWidget 是按 itemWidget 的 sizeHint 定行大小的，而开了 wordWrap 的
    QLabel，它的 sizeHint 宽度是「整句不折行需要多宽」—— 于是把左栏拖窄之后
    行仍然是原来的宽，列表横向滚动条又是关掉的，文字就被裁掉而不是折行。
    这里在每次尺寸变化时把行宽钉回视口宽，再按这个真实宽度量一次高度。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def fit_rows(self) -> None:
        # QListView 把行控件摆在 item 矩形里偏 (1,1) 的位置，宽高各再少 1：
        # 按视口全宽去量会少算一截，最后一行正好被裁掉
        w = max(self.viewport().width() - 2, 58)
        for i in range(self.count()):
            it = self.item(i)
            row = self.itemWidget(it)
            if row is None:
                continue
            # 已经按这个宽度量过的行跳过：拖着分割条时每帧重排整列会发涩。
            # 判据里带上 item 的 sizeHint 宽度，所以重新填过列表（新行没定过宽）
            # 和视口变宽变窄都会重新量，不会因为缓存漏掉。
            if (it.sizeHint().width() == w + 2
                    and row.minimumWidth() == w and row.maximumWidth() == w):
                continue
            row.setFixedWidth(w)
            it.setSizeHint(QSize(w + 2, _height_at(row, w) + 2))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.fit_rows()


class Card(QFrame):
    """圆角卡片容器。"""

    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 14, 16, 16)
        self._layout.setSpacing(10)
        if title:
            lbl = QLabel(title)
            lbl.setObjectName("CardTitle")
            self._layout.addWidget(lbl)

    def body(self) -> QVBoxLayout:
        return self._layout


class StatCard(Card):
    """顶部统计卡片。mini=True 时是挂在页面大标题那一行的小胶囊：
    数字和文字横排、贴着内容宽，不再独占一整排。"""

    def __init__(self, label: str, value: str = "0", accent: str = "accent",
                 parent: QWidget | None = None, mini: bool = False):
        super().__init__(parent=parent)
        self._mini = mini
        self._base_size = 16 if mini else 26
        if mini:
            self._layout.setContentsMargins(10, 4, 10, 5)
            self._layout.setSpacing(0)
            row = QHBoxLayout()
            row.setSpacing(6)
            self._layout.addLayout(row)
            # Maximum：不抢标题行 stretch 的空间，但窄到放不下时可以缩
            self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        else:
            row = self._layout
            self._layout.setSpacing(4)
        self.value_lbl = QLabel(value)
        # 小胶囊故意不走 #StatValue：那条规则带了 color，而 ID 选择器压过
        # [statColor=...] 属性选择器，写在那儿会让强调色永远不生效
        self.value_lbl.setObjectName("MiniValue" if mini else "StatValue")
        _apply_property(self.value_lbl, "statColor", accent)
        self.label_lbl = QLabel(label)
        self.label_lbl.setObjectName("MiniLabel" if mini else "StatLabel")
        row.addWidget(self.value_lbl)
        row.addWidget(self.label_lbl)

    def set_value(self, value: str) -> None:
        self.value_lbl.setText(value)
        if self._mini:
            return          # 16px 的数字撑不爆胶囊，不用降字号
        # 数字过长时逐级降字号，避免被截断
        n = len(value)
        size = self._base_size
        if n > 13:
            size = 17
        elif n > 10:
            size = 20
        elif n > 8:
            size = 23
        self.value_lbl.setStyleSheet(
            "" if size == self._base_size else f"font-size: {size}px;")

    def set_label(self, label: str) -> None:
        self.label_lbl.setText(label)

    def set_accent(self, accent: str) -> None:
        _apply_property(self.value_lbl, "statColor", accent)


class Tag(QLabel):
    """圆角小标签（tagColor 动态属性驱动配色）。"""

    def __init__(self, text: str, color: str = "blue", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("Tag")
        self.setAlignment(Qt.AlignCenter)
        _apply_property(self, "tagColor", color)


class LineChart(QWidget):
    """折线图（QPainter 绘制，主题色动态读取）。

    支持：折线（非曲线）、完整展示全部数据点、点击数据点回调、
    x 轴两行标签（主行日期 + 副行年份/周范围）。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._values: list[float] = []
        self._labels: list[str] = []
        self._color = "accent"
        self._annotate: bool | None = None
        self._hover_idx = -1
        self._decimals: int | None = None
        self._on_point_click: callable | None = None
        self._press_x: float | None = None
        self._sub_labels: list[str] = []
        self.setMinimumHeight(280)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def set_data(self, values: list[float], labels: list[str] | None = None,
                 color: str = "accent", annotate: bool | None = None,
                 decimals: int | None = None,
                 sub_labels: list[str] | None = None) -> None:
        """sub_labels：x 轴第二行文本（年份 / 周范围），与 values 对齐。"""
        self._values = list(values)
        self._labels = list(labels) if labels else [""] * len(values)
        self._color = color
        self._annotate = annotate
        self._hover_idx = -1
        self._decimals = decimals
        self._sub_labels = list(sub_labels) if sub_labels else []
        self.update()

    def set_on_click(self, cb: callable) -> None:
        """设置点击数据点的回调：cb(index: int)"""
        self._on_point_click = cb

    def _fmt(self, v: float) -> str:
        return f"{v:.{self._decimals}f}" if self._decimals is not None else f"{v:g}"

    @staticmethod
    def _visible_indices(n: int, plot_w: float) -> list[int]:
        slot = plot_w / max(n, 1)
        step = 1
        if slot < 56:
            step = int(56 / slot) + 1
        return [i for i in range(n) if i % step == 0 or i == n - 1]

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pad_r, pad_t, pad_b = 24, 30, 44

        p.setPen(Qt.NoPen)
        p.setBrush(_c("surface"))
        p.drawRoundedRect(QRectF(0, 0, w, h), 12, 12)

        if not self._values:
            p.setPen(_c("muted"))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "暂无数据")
            return

        # 全部数据（完整展示，不截断）
        vals = self._values
        labs = self._labels if self._labels else [""] * len(vals)
        n = len(vals)

        vmin, vmax = min(vals), max(vals)
        if vmax - vmin < 1e-6:
            vmin -= 1
            vmax += 1
        span = vmax - vmin
        vmin -= span * 0.12
        vmax += span * 0.12
        span = vmax - vmin

        # y 轴刻度为完整数值，左边距按实际文字宽度自适应，避免大数被截断
        fm = p.fontMetrics()
        tick_vals = [vmax - (vmax - vmin) * g / 4 for g in range(5)]
        pad_l = max(54, max(fm.horizontalAdvance(_axis_fmt(tv))
                            for tv in tick_vals) + 12)
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b
        baseline = pad_t + plot_h

        def px(i: int) -> float:
            return pad_l + plot_w * i / (n - 1) if n > 1 else pad_l + plot_w / 2

        def py(v: float) -> float:
            return pad_t + plot_h * (1 - (v - vmin) / span)

        # 横向网格
        p.setPen(QPen(_c("border"), 1))
        for g in range(5):
            y = pad_t + plot_h * g / 4
            p.drawLine(QPointF(pad_l, y), QPointF(pad_l + plot_w, y))
            val = vmax - (vmax - vmin) * g / 4
            p.setPen(_c("muted"))
            p.drawText(QRectF(0, y - 8, pad_l - 6, 16),
                       Qt.AlignRight | Qt.AlignVCenter, _axis_fmt(val))
            p.setPen(QPen(_c("border"), 1))

        points = [QPointF(px(i), py(v)) for i, v in enumerate(vals)]

        # 竖向虚线 + x 轴两行标签（主行：日期；副行：年份 / 周范围）
        dash = QPen(_c("border"), 1, Qt.DashLine)
        subs = self._sub_labels
        for i in self._visible_indices(n, plot_w):
            p.setPen(dash)
            p.drawLine(QPointF(px(i), pad_t), QPointF(px(i), baseline))
            p.setPen(_c("muted"))
            p.drawText(QRectF(px(i) - 40, baseline + 6, 80, 16),
                       Qt.AlignHCenter | Qt.AlignVCenter, labs[i])
            if i < len(subs) and subs[i]:
                p.drawText(QRectF(px(i) - 40, baseline + 22, 80, 16),
                           Qt.AlignHCenter | Qt.AlignVCenter, subs[i])

        # 折线（直线连接，非曲线）
        c = QColor(theme.get(self._color))
        if n >= 2:
            p.setPen(QPen(c, 2.5))
            p.setBrush(Qt.NoBrush)
            p.drawPolyline(QPolygonF(points))

        # 渐变填充
        if n >= 2:
            fill = QPainterPath()
            fill.moveTo(points[0])
            for pt in points[1:]:
                fill.lineTo(pt)
            fill.lineTo(points[-1].x(), baseline)
            fill.lineTo(points[0].x(), baseline)
            fill.closeSubpath()
            top_c = QColor(c); top_c.setAlpha(80)
            bot_c = QColor(c); bot_c.setAlpha(0)
            grad = QLinearGradient(0, pad_t, 0, baseline)
            grad.setColorAt(0.0, top_c)
            grad.setColorAt(1.0, bot_c)
            p.setPen(Qt.NoPen)
            p.setBrush(grad)
            p.drawPath(fill)

        # 数据点 + 数值标注
        show_vals = self._annotate if self._annotate is not None else n <= 13
        for i, pt in enumerate(points):
            is_last = i == n - 1
            p.setPen(QPen(_c("surface"), 2))
            p.setBrush(c)
            p.drawEllipse(pt, 4.5, 4.5)
            if show_vals:
                cx = min(max(pt.x(), pad_l + 26), w - pad_r - 26)
                p.setPen(_c("text_hi"))
                f = p.font(); f.setPointSizeF(8.5); f.setBold(is_last)
                p.setFont(f)
                p.drawText(QRectF(cx - 32, pt.y() - 26, 64, 16),
                           Qt.AlignHCenter | Qt.AlignBottom, self._fmt(vals[i]))

        # 悬停高亮
        if 0 <= self._hover_idx < n:
            self._paint_hover(p, points, baseline, pad_t, w)

    # ---------- 交互 ----------
    def _index_at(self, x: float) -> int:
        """x 坐标 → 数据索引；不在绘图区内返回 -1。"""
        n = len(self._values)
        if n == 0:
            return -1
        pad_l, pad_r = 54, 24
        plot_w = self.width() - pad_l - pad_r
        if n <= 1:
            mid = pad_l + plot_w / 2
            return 0 if abs(x - mid) < 45 else -1
        step = plot_w / (n - 1)
        if x < pad_l - step * 0.5 or x > pad_l + plot_w + step * 0.5:
            return -1
        idx = int(round((x - pad_l) / step))
        return max(0, min(n - 1, idx))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        idx = self._index_at(event.position().x())
        if idx != self._hover_idx:
            self._hover_idx = idx
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_x = event.position().x()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self._press_x is None:
            return
        x = event.position().x()
        moved = abs(x - self._press_x)
        self._press_x = None
        if moved > 6:
            return
        gidx = self._index_at(x)
        if gidx >= 0 and self._on_point_click is not None:
            self._on_point_click(gidx)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover_idx != -1:
            self._hover_idx = -1
            self.update()

    def _paint_hover(self, p: QPainter, points: list, baseline: float,
                     pad_t: float, w: float) -> None:
        i = self._hover_idx
        if not (0 <= i < len(points)):
            return
        pt = points[i]
        c = QColor(theme.get(self._color))

        # 竖向高亮虚线
        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 130), 1, Qt.DashLine))
        p.drawLine(QPointF(pt.x(), pad_t), QPointF(pt.x(), baseline))

        # 放大的高亮数据点
        p.setPen(QPen(_c("surface"), 2))
        p.setBrush(c)
        p.drawEllipse(pt, 6.5, 6.5)

        # 数值气泡（日期 + 数值，贴边自动收拢/翻转）
        label = self._labels[i] if i < len(self._labels) else ""
        tip = f"{label}  {self._fmt(self._values[i])}"
        f = p.font()
        f.setPointSizeF(9.5)
        f.setBold(True)
        p.setFont(f)
        tw = p.fontMetrics().horizontalAdvance(tip) + 22
        th = 27
        bx = min(max(pt.x() - tw / 2, 6), w - tw - 6)
        by = pt.y() - th - 16
        if by < 4:
            by = pt.y() + 16
        p.setBrush(_c("surface_hi"))
        p.setPen(QPen(_c("border"), 1))
        p.drawRoundedRect(QRectF(bx, by, tw, th), 9, 9)
        p.setPen(_c("text_hi"))
        p.drawText(QRectF(bx, by, tw, th), Qt.AlignCenter, tip)


class MultiLineChart(QWidget):
    """多系列折线图（用于资产渠道趋势 / 多人对比）。

    每个系列 dict：{"name": str, "color": str, "values": list[float|None]}；
    None 表示该点无数据（断线）。共享同一组 x 轴标签。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._series: list[dict] = []
        self._labels: list[str] = []
        self._legend = True
        self._hint = ""
        self._dates: list[str] = []
        self._sub_labels: list[str] = []
        self._hover_idx = -1
        self._on_point_click: callable | None = None
        self._press_x: float | None = None
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def set_series(self, series: list[dict], labels: list[str] | None = None,
                   legend: bool = True, dates: list[str] | None = None,
                   sub_labels: list[str] | None = None) -> None:
        self._series = [dict(s) for s in series]
        n = max((len(s.get("values", [])) for s in self._series), default=0)
        self._labels = list(labels) if labels else [""] * n
        self._dates = list(dates) if dates else list(self._labels)
        self._sub_labels = list(sub_labels) if sub_labels else []
        self._legend = legend
        self._hover_idx = -1
        self.update()

    def set_hint(self, text: str) -> None:
        """数据不足时替代默认「暂无数据」的提示文案。"""
        self._hint = text
        self.update()

    def set_on_click(self, cb: callable) -> None:
        """点击数据点回调：cb(index: int)。"""
        self._on_point_click = cb

    def _geo(self) -> tuple:
        """返回 (pad_l, pad_t, plot_w, plot_h, w, h)。"""
        w, h = self.width(), self.height()
        # pad_l 由 paintEvent 按 y 轴标签实际宽度算出（完整数值较宽）
        pad_l = getattr(self, "_pad_l", 54)
        pad_r, pad_t, pad_b = 20, 22, 34
        legend_h = 26 if self._legend and self._series else 0
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b - legend_h
        return pad_l, pad_t, plot_w, plot_h, w, h

    def _index_at(self, x: float) -> int:
        n = max((len(s.get("values", [])) for s in self._series), default=0)
        if n == 0:
            return -1
        pad_l, _pad_t, plot_w, _plot_h, _w, _h = self._geo()
        if n <= 1:
            mid = pad_l + plot_w / 2
            return 0 if abs(x - mid) < 45 else -1
        step = plot_w / (n - 1)
        if x < pad_l - step * 0.5 or x > pad_l + plot_w + step * 0.5:
            return -1
        idx = int(round((x - pad_l) / step))
        return max(0, min(n - 1, idx))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        idx = self._index_at(event.position().x())
        if idx != self._hover_idx:
            self._hover_idx = idx
            self.update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_x = event.position().x()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self._press_x is None:
            return
        x = event.position().x()
        moved = abs(x - self._press_x)
        self._press_x = None
        if moved > 6:  # 拖动则不算点击
            return
        gidx = self._index_at(x)
        if gidx >= 0 and self._on_point_click is not None:
            self._on_point_click(gidx)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover_idx != -1:
            self._hover_idx = -1
            self.update()
        super().leaveEvent(event)

    @staticmethod
    def _visible_indices(n: int, plot_w: float) -> list[int]:
        slot = plot_w / max(n, 1)
        step = 1
        if slot < 56:
            step = int(56 / slot) + 1
        return [i for i in range(n) if i % step == 0 or i == n - 1]

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pad_r, pad_t = 20, 22
        pad_b = 48 if self._sub_labels else 34
        legend_h = 26 if self._legend and self._series else 0

        p.setPen(Qt.NoPen)
        p.setBrush(_c("surface"))
        p.drawRoundedRect(QRectF(0, 0, w, h), 12, 12)

        n = max((len(s.get("values", [])) for s in self._series), default=0)
        all_vals = [v for s in self._series for v in s.get("values", [])
                    if v is not None]
        if n == 0 or not self._series or not all_vals or (n < 2 and self._hint):
            p.setPen(_c("muted"))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter,
                       self._hint or "暂无数据")
            return
        vmin, vmax = min(all_vals), max(all_vals)
        if vmax - vmin < 1e-6:
            vmin -= 1
            vmax += 1
        span = vmax - vmin
        vmin -= span * 0.12
        vmax += span * 0.12
        span = vmax - vmin

        # y 轴刻度为完整数值，左边距按实际文字宽度自适应；存下来供 _geo 命中测试复用
        fm = p.fontMetrics()
        tick_vals = [vmax - (vmax - vmin) * g / 4 for g in range(5)]
        pad_l = max(54, max(fm.horizontalAdvance(_axis_fmt(tv))
                            for tv in tick_vals) + 12)
        self._pad_l = pad_l
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b - legend_h
        baseline = pad_t + plot_h

        def px(i: int) -> float:
            return pad_l + plot_w * i / (n - 1) if n > 1 else pad_l + plot_w / 2

        def py(v: float) -> float:
            return pad_t + plot_h * (1 - (v - vmin) / span)

        # 网格
        p.setPen(QPen(_c("border"), 1))
        for g in range(5):
            y = pad_t + plot_h * g / 4
            p.drawLine(QPointF(pad_l, y), QPointF(pad_l + plot_w, y))
            val = vmax - (vmax - vmin) * g / 4
            p.setPen(_c("muted"))
            p.drawText(QRectF(0, y - 8, pad_l - 6, 16),
                       Qt.AlignRight | Qt.AlignVCenter, _axis_fmt(val))
            p.setPen(QPen(_c("border"), 1))

        dash = QPen(_c("border"), 1, Qt.DashLine)
        for i in self._visible_indices(n, plot_w):
            p.setPen(dash)
            p.drawLine(QPointF(px(i), pad_t), QPointF(px(i), baseline))
            p.setPen(_c("muted"))
            lab = self._labels[i] if i < len(self._labels) else ""
            p.drawText(QRectF(px(i) - 44, baseline + 4, 88, 18),
                       Qt.AlignHCenter | Qt.AlignVCenter, lab)
            if (self._sub_labels and i < len(self._sub_labels)
                    and self._sub_labels[i]):
                p.drawText(QRectF(px(i) - 44, baseline + 20, 88, 16),
                           Qt.AlignHCenter | Qt.AlignVCenter,
                           self._sub_labels[i])

        # 各系列折线（跳过 None 断点）
        for s in self._series:
            vals = s.get("values", [])
            color = QColor(theme.get(s.get("color", "accent")))
            pts = [(px(i), py(v)) for i, v in enumerate(vals) if v is not None]
            if len(pts) < 2:
                # 单点：画个小圆
                if pts:
                    p.setPen(QPen(color, 2))
                    p.setBrush(color)
                    p.drawEllipse(QPointF(*pts[0]), 4, 4)
                continue
            path = QPainterPath()
            path.moveTo(QPointF(*pts[0]))
            for pt in pts[1:]:
                path.lineTo(QPointF(*pt))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(color, 2.2))
            p.drawPath(path)
            for x, y in pts:
                p.setPen(QPen(_c("surface"), 2))
                p.setBrush(color)
                p.drawEllipse(QPointF(x, y), 3.2, 3.2)

        # 图例
        if self._legend and self._series:
            x = pad_l
            ly = h - 18
            for s in self._series:
                color = QColor(theme.get(s.get("color", "accent")))
                p.setPen(Qt.NoPen)
                p.setBrush(color)
                p.drawRoundedRect(QRectF(x, ly - 4, 12, 8), 2, 2)
                p.setPen(_c("text"))
                name = s.get("name", "")
                if p.fontMetrics().horizontalAdvance(name) > 84:
                    name = name[:6] + "…"
                f = p.font()
                f.setPointSizeF(8.5)
                p.setFont(f)
                p.drawText(QRectF(x + 16, ly - 8, 110, 16),
                           Qt.AlignLeft | Qt.AlignVCenter, name)
                x += 16 + p.fontMetrics().horizontalAdvance(name) + 22

        # 悬停：竖向虚线 + 放大数据点 + 数值气泡
        if 0 <= self._hover_idx < n:
            i = self._hover_idx
            hx = px(i)
            p.setPen(QPen(_c("border_strong"), 1, Qt.DashLine))
            p.drawLine(QPointF(hx, pad_t), QPointF(hx, baseline))
            for s in self._series:
                vals = s.get("values", [])
                if i < len(vals) and vals[i] is not None:
                    color = QColor(theme.get(s.get("color", "accent")))
                    p.setPen(QPen(_c("surface"), 2))
                    p.setBrush(color)
                    p.drawEllipse(QPointF(hx, py(vals[i])), 5.5, 5.5)

            date = self._dates[i] if i < len(self._dates) else ""
            lab = self._labels[i] if i < len(self._labels) else ""
            sub = (self._sub_labels[i] if self._sub_labels
                   and i < len(self._sub_labels) else "")
            # 主标签 + 副标签（第N季 + 年份）；无副标签时回落到代表日期
            head = " ".join(x for x in (lab, sub) if x) or date
            parts = [head] if head else []
            for s in self._series:
                vals = s.get("values", [])
                if i < len(vals) and vals[i] is not None:
                    parts.append(f"{s.get('name', '')} {_axis_fmt(vals[i])}")
            tip = "　".join(parts)
            if tip:
                f = p.font()
                f.setPointSizeF(9.5)
                f.setBold(True)
                p.setFont(f)
                tw = min(p.fontMetrics().horizontalAdvance(tip) + 24, w - 12)
                th = 28
                bx = min(max(hx - tw / 2, 6), w - tw - 6)
                p.setBrush(_c("surface_hi"))
                p.setPen(QPen(_c("border"), 1))
                p.drawRoundedRect(QRectF(bx, 6, tw, th), 9, 9)
                p.setPen(_c("text_hi"))
                p.drawText(QRectF(bx, 6, tw, th), Qt.AlignCenter, tip)


class BarChart(QWidget):
    """轻量柱状图。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._values: list[float] = []
        self._labels: list[str] = []
        self._color = "accent"
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, values: list[float], labels: list[str] | None = None,
                 color: str = "accent") -> None:
        self._values = list(values)
        self._labels = list(labels) if labels else [""] * len(values)
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 40, 16, 18, 30
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b

        p.setPen(Qt.NoPen)
        p.setBrush(_c("surface"))
        p.drawRoundedRect(QRectF(0, 0, w, h), 12, 12)

        if not self._values:
            p.setPen(_c("muted"))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "暂无数据")
            return

        vmax = max(max(self._values), 1e-6) * 1.15
        n = len(self._values)
        slot = plot_w / max(n, 1)
        bar_w = min(slot * 0.6, 48)
        step = 1
        if slot < 44:
            step = int(44 / slot) + 1

        for i, v in enumerate(self._values):
            x = pad_l + slot * i + (slot - bar_w) / 2
            bh = plot_h * (v / vmax)
            y = pad_t + plot_h - bh
            # 0 值不画柱：高度 0 的圆角矩形会被 QPainter 涂成一条基线横线，
            # 看着像一根被压扁的柱子，反而读成「有值」。
            if bh > 0.5:
                p.setBrush(_c(self._color))
                p.drawRoundedRect(QRectF(x, y, bar_w, bh), 5, 5)
            p.setPen(_c("muted"))
            p.drawText(QRectF(x - 10, y - 20, bar_w + 20, 16), Qt.AlignCenter, f"{v:g}")
            if i % step == 0 or i == n - 1:
                p.drawText(QRectF(pad_l + slot * i, pad_t + plot_h + 4, slot, 20),
                           Qt.AlignCenter, self._labels[i] if i < len(self._labels) else "")


class CircularProgress(QWidget):
    """环形进度条，中心支持三段文字（顶部任务名 / 中间时间 / 底部状态）。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._progress = 0.0
        self._text = "00:00"
        self._top_text = ""
        self._bottom_text = ""
        self._color = "accent"
        self._white_text = False
        self.setMinimumSize(240, 240)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_progress(self, p: float) -> None:
        self._progress = max(0.0, min(1.0, p))
        self.update()

    def set_text(self, text: str) -> None:
        self._text = text
        self.update()

    def set_top_text(self, text: str) -> None:
        self._top_text = text
        self.update()

    def set_bottom_text(self, text: str) -> None:
        self._bottom_text = text
        self.update()

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def set_white_text(self, white: bool) -> None:
        """学霸模式等深色背景下，让中心/上下文字强制用白色系。"""
        self._white_text = white
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        d = min(w, h)
        margin = 14
        rect = QRectF((w - d) / 2 + margin, (h - d) / 2 + margin,
                      d - 2 * margin, d - 2 * margin)
        ring = max(10.0, d * 0.055)

        # 背景环
        pen = QPen(_c("surface_hi"), ring)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, 0, 360 * 16)

        # 前景环（进度）
        if self._progress > 0:
            pen.setColor(_c(self._color))
            p.setPen(pen)
            start = 90 * 16
            span = int(-360 * 16 * self._progress)
            p.drawArc(rect, start, span)

        # 中心三段文字
        inner = rect.adjusted(ring * 1.6, ring * 1.6, -ring * 1.6, -ring * 1.6)
        ih = inner.height()
        top_rect = QRectF(inner.left(), inner.top(), inner.width(), ih * 0.24)
        center_rect = QRectF(inner.left(), inner.top() + ih * 0.26,
                             inner.width(), ih * 0.46)
        bottom_rect = QRectF(inner.left(), inner.top() + ih * 0.74,
                             inner.width(), ih * 0.26)

        sub_color = QColor("#9aa0b5") if self._white_text else _c("muted")
        center_color = QColor("#ffffff") if self._white_text else _c("text_hi")

        p.setPen(sub_color)
        f_top = QFont()
        f_top.setPixelSize(max(11, int(d * 0.052)))
        p.setFont(f_top)
        if self._top_text:
            p.drawText(top_rect, Qt.AlignHCenter | Qt.AlignVCenter, self._top_text)

        p.setPen(center_color)
        f_mid = QFont()
        f_mid.setPixelSize(int(d * 0.155))
        f_mid.setBold(True)
        p.setFont(f_mid)
        p.drawText(center_rect, Qt.AlignCenter, self._text)

        p.setPen(sub_color)
        f_bot = QFont()
        f_bot.setPixelSize(max(11, int(d * 0.05)))
        p.setFont(f_bot)
        if self._bottom_text:
            p.drawText(bottom_rect, Qt.AlignHCenter | Qt.AlignVCenter, self._bottom_text)


class GoalProgress(QWidget):
    """减重进度条：渐变填充 + 末端百分比胶囊 + 两端圆点（打卡 App 风格）。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._ratio = 0.0
        self._text = "0%"
        self.setFixedHeight(34)

    def set_progress(self, ratio: float, text: str) -> None:
        self._ratio = max(0.0, min(1.0, ratio))
        self._text = text
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        bar_h = 12
        y = (h - bar_h) / 2
        x0, x1 = 6, w - 6

        # 底条
        p.setPen(Qt.NoPen)
        p.setBrush(_c("surface_hi"))
        p.drawRoundedRect(QRectF(x0, y, x1 - x0, bar_h), bar_h / 2, bar_h / 2)

        # 前景渐变条
        fw = max(bar_h, (x1 - x0) * self._ratio)
        grad = QLinearGradient(x0, 0, x0 + fw, 0)
        grad.setColorAt(0.0, QColor(theme.get("green")))
        grad.setColorAt(1.0, QColor(theme.get("accent")))
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(x0, y, fw, bar_h), bar_h / 2, bar_h / 2)

        # 两端白色小圆点
        for cx in (x0, x1):
            p.setBrush(QColor(theme.get("surface")))
            p.setPen(QPen(QColor(theme.get("border")), 1))
            p.drawEllipse(QPointF(cx, y + bar_h / 2), 4.5, 4.5)

        # 末端百分比胶囊
        f = p.font()
        f.setPointSizeF(9)
        f.setBold(True)
        p.setFont(f)
        tw = p.fontMetrics().horizontalAdvance(self._text) + 22
        th = 24
        tx = min(max(x0 + fw - tw / 2, x0), x1 - tw)
        ty = (h - th) / 2
        p.setBrush(QColor(theme.get("surface")))
        p.setPen(QPen(QColor(theme.get("border")), 1))
        p.drawRoundedRect(QRectF(tx, ty, tw, th), th / 2, th / 2)
        p.setPen(QColor(theme.get("green")))
        p.drawText(QRectF(tx, ty, tw, th), Qt.AlignCenter, self._text)


class StatBar(QWidget):
    """细比例条：轨道 + 按主题色键填充，用于分布行 / 进度行。

    比 QProgressBar 轻：颜色走主题键（不用为每档色写一条 QSS，
    全局 QSS 体积对换肤耗时影响很大，见 theme.ThemeManager._swap）。
    """

    def __init__(self, color: str = "accent", height: int = 6,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._ratio = 0.0
        self._color = color
        self._h = height
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_value(self, ratio: float, color: str | None = None) -> None:
        self._ratio = max(0.0, min(1.0, ratio))
        if color:
            self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self._h
        r = h / 2
        p.setPen(Qt.NoPen)
        p.setBrush(_c("surface_hi"))
        p.drawRoundedRect(QRectF(0, 0, w, h), r, r)
        if self._ratio <= 0:
            return
        # 填充过窄时画不出圆角，退化为纯色块
        fw = max(h, w * self._ratio)
        p.setBrush(_c(self._color))
        p.drawRoundedRect(QRectF(0, 0, fw, h), r, r)


# ---------- 图标系统（纯 QPainter 绘制，不依赖字体与图片资源） ----------
# 分类：(图标名, 主题色键)
CATEGORY_STYLE = {
    "餐饮": ("bowl", "amber"),
    "交通": ("car", "blue"),
    "购物": ("bag", "accent"),
    "住房": ("home", "green"),
    "娱乐": ("game", "red"),
    "医疗": ("medkit", "red"),
    "教育": ("book", "blue"),
    "其他": ("dots", "muted"),
    "工资": ("wallet", "green"),
    "奖金": ("star", "amber"),
    "理财": ("chart", "accent"),
    "兼职": ("briefcase", "blue"),
}

# 账户/归属：(图标名, 主题色键, 文字；文字为空则画图标)
ACCOUNT_STYLE = {
    "微信": ("chat", "green", ""),
    "支付宝": ("alipay", "blue", "支"),
    "银行卡": ("card", "accent", ""),
    "现金": ("cash", "amber", ""),
    "其他": ("wallet", "muted", ""),
}

# 归属人
OWNER_STYLE = {
    "我": ("person", "accent", ""),
    "伴侣": ("person", "green", ""),
}

_SOFT_KEYS = ("accent", "green", "red", "amber", "blue")
_FALLBACK_COLORS = ("accent", "green", "blue", "amber", "red")

# 可供用户选择的自定义分类图标库（图标名 → 中文名）
ICON_LIBRARY = [
    ("bowl", "餐饮"), ("car", "交通"), ("bag", "购物"), ("home", "住房"),
    ("game", "游戏"), ("medkit", "医疗"), ("book", "学习"), ("wallet", "钱包"),
    ("star", "奖励"), ("chart", "理财"), ("briefcase", "工作"), ("card", "卡片"),
    ("cash", "现金"), ("chat", "聊天"), ("person", "人物"), ("dots", "其他"),
    ("gift", "礼物"), ("phone", "数码"), ("pet", "宠物"), ("travel", "旅行"),
    ("coffee", "饮品"), ("heart", "爱心"),
]


def style_for(name: str, kind: str = "category") -> tuple[str, str, str]:
    """返回 (图标名, 颜色键, 文字)。未知名称按名称稳定哈希取色。"""
    table = CATEGORY_STYLE if kind == "category" else (
        OWNER_STYLE if kind == "owner" else ACCOUNT_STYLE)
    if name in table:
        item = table[name]
        if len(item) == 2:          # (图标, 颜色)
            return item[0], item[1], ""
        return item[0], item[1], item[2]
    color = _FALLBACK_COLORS[sum(ord(c) for c in name) % len(_FALLBACK_COLORS)]
    if kind == "account":
        return "wallet", color, (name[:1] or "?")
    return "dots", color, ""


def _icon_path(name: str) -> QPainterPath:
    """24×24 网格内的线性图标路径（stroke 风格，圆头圆角）。"""
    p = QPainterPath()
    if name == "bowl":            # 餐饮：碗 + 蒸汽
        p.moveTo(3.5, 12.5)
        p.quadTo(12, 21, 20.5, 12.5)
        p.moveTo(2, 12.5)
        p.lineTo(22, 12.5)
        p.moveTo(9, 9)
        p.quadTo(10.3, 6.8, 9, 4.5)
        p.moveTo(14.5, 9)
        p.quadTo(15.8, 6.8, 14.5, 4.5)
    elif name == "car":           # 交通：车
        p.addRoundedRect(3, 11.5, 18, 5.5, 2, 2)
        p.moveTo(6.8, 11.3)
        p.lineTo(9.2, 7.8)
        p.lineTo(15.2, 7.8)
        p.lineTo(17.4, 11.3)
        p.addEllipse(6.6, 16, 3.2, 3.2)
        p.addEllipse(14.2, 16, 3.2, 3.2)
    elif name == "bag":           # 购物：购物袋
        p.moveTo(5, 8)
        p.lineTo(19, 8)
        p.lineTo(17.6, 20)
        p.lineTo(6.4, 20)
        p.closeSubpath()
        p.moveTo(9.2, 8)
        p.quadTo(12, 2.8, 14.8, 8)
    elif name == "home":          # 住房：房子
        p.moveTo(3, 11.5)
        p.lineTo(12, 4)
        p.lineTo(21, 11.5)
        p.addRect(5.5, 11.5, 13, 8.5)
        p.addRect(10.2, 15, 3.6, 5)
    elif name == "game":          # 娱乐：手柄
        p.addRoundedRect(2.5, 8, 19, 8.5, 3.5, 3.5)
        p.moveTo(7.4, 12.2)
        p.lineTo(10.4, 12.2)
        p.moveTo(8.9, 10.7)
        p.lineTo(8.9, 13.7)
        p.addEllipse(14.2, 11.2, 1.9, 1.9)
        p.addEllipse(16.8, 13.4, 1.9, 1.9)
    elif name == "medkit":        # 医疗：医药箱
        p.addRoundedRect(3, 6.5, 18, 13, 2.5, 2.5)
        p.moveTo(12, 9.5)
        p.lineTo(12, 16.5)
        p.moveTo(8.5, 13)
        p.lineTo(15.5, 13)
    elif name == "book":          # 教育：书
        p.moveTo(3, 6.8)
        p.lineTo(9.5, 5.2)
        p.lineTo(9.5, 19)
        p.lineTo(3, 17.6)
        p.closeSubpath()
        p.moveTo(21, 6.8)
        p.lineTo(14.5, 5.2)
        p.lineTo(14.5, 19)
        p.lineTo(21, 17.6)
        p.closeSubpath()
        p.moveTo(12, 5.2)
        p.lineTo(12, 19)
    elif name == "wallet":        # 钱包 / 工资
        p.addRoundedRect(3, 6.5, 18, 12, 2.5, 2.5)
        p.moveTo(3, 10.8)
        p.lineTo(21, 10.8)
        p.addEllipse(15.6, 14.2, 2, 2)
    elif name == "star":          # 奖金：五角星
        import math
        pts = []
        for i in range(10):
            r = 8.2 if i % 2 == 0 else 3.5
            a = math.radians(-90 + i * 36)
            pts.append(QPointF(12 + r * math.cos(a), 12 + r * math.sin(a)))
        p.addPolygon(pts)
        p.closeSubpath()
    elif name == "chart":         # 理财：走势
        p.moveTo(3.5, 4)
        p.lineTo(3.5, 20)
        p.lineTo(20.5, 20)
        p.moveTo(6.5, 17)
        p.lineTo(10.5, 11.5)
        p.lineTo(14, 14.5)
        p.lineTo(19.5, 7)
        p.moveTo(16.2, 7)
        p.lineTo(19.5, 7)
        p.lineTo(19.5, 10.3)
    elif name == "briefcase":     # 兼职：公文包
        p.addRoundedRect(2.5, 8, 19, 11.5, 2, 2)
        p.moveTo(9, 8)
        p.lineTo(9, 5.8)
        p.lineTo(15, 5.8)
        p.lineTo(15, 8)
        p.moveTo(2.5, 12.5)
        p.lineTo(21.5, 12.5)
    elif name == "card":          # 银行卡
        p.addRoundedRect(2.5, 6, 19, 12, 2.5, 2.5)
        p.moveTo(2.5, 9.8)
        p.lineTo(21.5, 9.8)
        p.addRect(5.5, 13.2, 4.5, 2.6)
    elif name == "cash":          # 现金
        p.addEllipse(3.5, 3.5, 17, 17)
        p.moveTo(9, 8.2)
        p.lineTo(12, 12)
        p.lineTo(15, 8.2)
        p.moveTo(12, 12)
        p.lineTo(12, 17.5)
        p.moveTo(8.6, 12.8)
        p.lineTo(15.4, 12.8)
        p.moveTo(8.6, 15.6)
        p.lineTo(15.4, 15.6)
    elif name == "chat":          # 微信：对话气泡
        p.addRoundedRect(2.5, 4.5, 14, 10.5, 3, 3)
        p.moveTo(6, 15)
        p.lineTo(6, 19.5)
        p.lineTo(9.6, 15.2)
        p.addRoundedRect(10.5, 9.5, 11, 8, 2.5, 2.5)
    elif name == "alipay":        # 支付宝（文字兜底，见 IconBadge）
        p.addEllipse(3, 3, 18, 18)
    elif name == "person":        # 归属人
        p.addEllipse(8.2, 3.6, 7.6, 7.6)
        p.moveTo(3.6, 20)
        p.quadTo(12, 12.2, 20.4, 20)
    elif name == "gift":          # 礼物：盒子 + 蝴蝶结
        p.addRect(4, 9, 16, 11)
        p.moveTo(4, 9)
        p.lineTo(20, 9)
        p.moveTo(12, 9)
        p.lineTo(12, 20)
        p.moveTo(9, 9)
        p.quadTo(9, 5, 7.5, 4.5)
        p.quadTo(6, 4, 6, 6.5)
        p.quadTo(6, 8, 9, 9)
        p.moveTo(15, 9)
        p.quadTo(15, 5, 16.5, 4.5)
        p.quadTo(18, 4, 18, 6.5)
        p.quadTo(18, 8, 15, 9)
    elif name == "phone":         # 数码：手机
        p.addRoundedRect(7, 3, 10, 18, 2, 2)
        p.moveTo(10.2, 18)
        p.lineTo(13.8, 18)
    elif name == "pet":           # 宠物：猫头
        p.addEllipse(5, 9, 14, 12)
        p.moveTo(6, 10)
        p.lineTo(7.5, 4.5)
        p.lineTo(11, 8)
        p.moveTo(13, 8)
        p.lineTo(16.5, 4.5)
        p.lineTo(18, 10)
        p.addEllipse(9.2, 13.5, 1.4, 1.4)
        p.addEllipse(13.4, 13.5, 1.4, 1.4)
    elif name == "travel":        # 旅行：行李箱
        p.addRoundedRect(5, 6, 14, 14, 2, 2)
        p.moveTo(10, 6)
        p.lineTo(10, 4)
        p.lineTo(14, 4)
        p.lineTo(14, 6)
        p.moveTo(12, 9.5)
        p.lineTo(12, 16.5)
    elif name == "coffee":        # 饮品：咖啡杯
        p.addRect(5, 8, 12, 9)
        p.moveTo(5, 8)
        p.lineTo(17, 8)
        p.moveTo(17, 11)
        p.quadTo(21, 11.5, 17, 15)
        p.moveTo(9, 4.5)
        p.quadTo(10, 6.5, 9, 8)
        p.moveTo(13, 4.5)
        p.quadTo(14, 6.5, 13, 8)
    elif name == "heart":         # 爱心
        p.moveTo(12, 19.5)
        p.cubicTo(5, 14.5, 4, 8, 8.5, 6.2)
        p.cubicTo(10.8, 5.1, 12, 7, 12, 7)
        p.cubicTo(12, 7, 13.2, 5.1, 15.5, 6.2)
        p.cubicTo(20, 8, 19, 14.5, 12, 19.5)
        p.closeSubpath()
    else:                         # dots：其他
        p.addEllipse(4.6, 10.6, 2.4, 2.4)
        p.addEllipse(10.8, 10.6, 2.4, 2.4)
        p.addEllipse(17, 10.6, 2.4, 2.4)
    return p


class IconBadge(QWidget):
    """圆角方形图标徽章。

    kind="category" 用线性矢量图标；kind="account" 优先画图标，
    对于品牌类（支付宝等）直接绘制文字，辨识度更高。
    """

    def __init__(self, name: str, kind: str = "category", size: int = 34,
                 color: str | None = None, icon: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._name = name
        self._kind = kind
        self._size = size
        self._color = color
        self._icon = icon
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = self._size
        icon, color, text = style_for(self._name, self._kind)
        if self._color:
            color = self._color
        if self._icon:
            icon = self._icon

        soft_key = f"{color}_soft" if color in _SOFT_KEYS else "surface_hi"
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.get(soft_key)))
        p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.3, s * 0.3)

        fg = QColor(theme.get(color))
        if text:
            f = p.font()
            f.setPixelSize(int(s * 0.5))
            f.setBold(True)
            p.setFont(f)
            p.setPen(fg)
            p.drawText(QRectF(0, 0, s, s), Qt.AlignCenter, text)
            return

        inner = s * 0.62
        off = (s - inner) / 2
        p.save()
        p.translate(off, off)
        p.scale(inner / 24, inner / 24)
        pen = QPen(fg, 2.0)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.strokePath(_icon_path(icon), pen)
        p.restore()


def icon_pixmap(name: str, kind: str = "category", size: int = 28,
                color: str | None = None, icon: str | None = None) -> "QPixmap":
    """把图标徽章渲染成 QPixmap（供 QToolButton / QComboBox 使用）。"""
    return IconBadge(name, kind, size, color, icon).grab()


class ElidedLabel(QLabel):
    """文本过长自动显示省略号，并在 tooltip 中保留全文。"""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._full = text
        self._shown = text
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        """长文本不应撑破布局：最小宽度固定为一个小值。"""
        return QSize(28, super().minimumSizeHint().height())

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text
        self._apply()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        fm = self.fontMetrics()
        elided = fm.elidedText(self._full, Qt.ElideRight, max(self.width(), 0))
        # 文本没变就不要重设，否则会与布局互相触发（resize ↔ setText 递归）
        if elided == self._shown:
            return
        self._shown = elided
        super().setText(elided)
        self.setToolTip(self._full if elided != self._full else "")


def hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    return line
