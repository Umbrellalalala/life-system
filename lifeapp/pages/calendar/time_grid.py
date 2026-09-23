"""时间轴视图：日 / 周 / 多日共用一套（只是列数不同）。

对齐滴答清单：
- 顶部日期表头（星期 + 数字 + 休/班角标，今天蓝圆）
- 「全天」带钉在滚动区外面，不跟着时间轴滚走
- 0–23 小时网格、左侧小时刻度、当前时间一条红线
- 有时间的任务按时长排成块，同一时段重叠的块并排分栏
- 块可拖：纵向改时间、横向改日期；空白处点击即在该时段新建

两块画布（全天带 / 时间网格）共用同一套「按天等分 + 绝对定位」的算法，
所以列宽永远对得齐。
"""
from __future__ import annotations

from PySide6.QtCore import (
    Qt, QDate, QTime, QPoint, QRectF, Signal, QMimeData, QTimer,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QDrag
from PySide6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QScrollArea,
)

from ... import dateparse, theme, widgets
from . import holidays, model, month_view

GUTTER = 60         # 左侧小时刻度宽
HOUR_H = 58         # 一小时的高度（时间轴要能滚起来）
MIN_BLOCK_H = 30    # 块的最小高度
DEFAULT_MIN = 60    # 没设时长时按 1 小时排块
ROW_H = 23          # 全天带里一条的高度
SNAP_MIN = 15       # 拖下边缘改时长时，吸附到 15 分钟
RESIZE_ZONE = 8     # 块底部多少像素内按下算「拖时长」而不是「拖走」


def _minutes(time_s: str) -> int:
    try:
        h, m = time_s.split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return 0


def _span_text(row: dict) -> str:
    """块上那行时间写成「10:00 - 11:30」。

    只写开始时间的话，块再高也看不出这是多久的事，拖完下边缘也没法确认改成了
    多少；跨到第二天就显示「+1天」而不是把 25:00 这种写出来。
    """
    start = row["time"]
    mins = max(int(row.get("duration") or 0) or DEFAULT_MIN, SNAP_MIN)
    end = _minutes(start) + mins
    tail = f" +{end // 1440}天" if end >= 1440 else ""
    eh, em = (end % 1440) // 60, end % 60
    return (f"{dateparse.human_time(start)} - "
            f"{dateparse.human_time(f'{eh:02d}:{em:02d}')}") + tail


def _lanes(rows: list[dict]) -> list[tuple[dict, int, int]]:
    """给同一天的有时长任务分栏：重叠的并排，不重叠的占满整列宽。

    返回 (row, lane, lane_count)。先按开始时间切成互相重叠的簇，
    簇内贪心找第一个空栏，簇的栏数决定每块多宽。
    """
    items = sorted((r for r in rows if r.get("time")),
                   key=lambda r: (_minutes(r["time"]), r["sort"], r["id"]))
    out: list[tuple[dict, int, int]] = []
    cluster: list[tuple[dict, int]] = []
    ends: list[int] = []
    cluster_end = -1

    def flush() -> None:
        n = max((lane + 1 for _, lane in cluster), default=1)
        out.extend((r, lane, n) for r, lane in cluster)

    for r in items:
        st = _minutes(r["time"])
        en = st + max(int(r.get("duration") or 0) or DEFAULT_MIN, 30)
        if cluster and st >= cluster_end:
            flush()
            cluster, ends = [], []
        for i, e in enumerate(ends):
            if e <= st:
                lane = i
                ends[i] = en
                break
        else:
            ends.append(en)
            lane = len(ends) - 1
        cluster.append((r, lane))
        cluster_end = max(cluster_end, en)
    flush()
    return out


class TimeBlock(QFrame):
    """时间轴上的一个任务块。

    按住**下边缘**拖动 = 改时长（滴答就是这么改的，库里除了这个入口没有别的
    地方能设 duration_min）；按住块的其他地方拖动 = 挪到别的时间/日子。
    """

    clicked_ = Signal(dict, QPoint)
    resized = Signal(dict, int)       # (这条日历条目, 新的分钟数)

    def __init__(self, row: dict, canvas: QWidget):
        super().__init__(canvas)
        self.row = row
        self._press: QPoint | None = None
        self._resizing = False
        self._grab_y = 0
        self._h0 = 0
        self.setObjectName("CalBlock")
        self.setToolTip(model.row_tooltip(row))
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)      # 不按键也要知道鼠标靠近了下边缘
        lay = QVBoxLayout(self)
        lay.setContentsMargins(5, 2, 4, 2)
        lay.setSpacing(0)
        title = QLabel(row["title"])
        title.setObjectName("CalBlockTitle")
        f = QFont()
        f.setStrikeOut(bool(row["done"]))
        title.setFont(f)
        lay.addWidget(title)
        if row.get("time"):
            sub = QLabel(_span_text(row))
            sub.setObjectName("CalBlockTime")
            lay.addWidget(sub)
        self._paint(False)

    def _paint(self, hover: bool) -> None:
        bg, fg = model.row_bar_color(self.row)
        edge = theme.lerp_color(bg, fg, 0.30)
        if hover:
            bg = theme.lerp_color(bg, theme.get("text_hi"),
                                  0.08 if theme.is_dark() else 0.05)
        self.setStyleSheet(
            f"#CalBlock {{ background: {bg}; border: 1px solid {edge};"
            " border-radius: 5px; }"
            f"#CalBlockTitle {{ color: {fg}; font-size: 13.5px;"
            " background: transparent; border: none; }}"
            f"#CalBlockTime {{ color: {fg}; font-size: 12px;"
            " background: transparent; border: none; }}")

    def enterEvent(self, event) -> None:  # noqa: N802
        self._paint(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._paint(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 订阅事件是只读的：它的 id 是 cal_feed_event 的行号，不是 todo id，
        # 一旦让它起拖，落点会去改到「同号码的那条待办」上，等于静默改错数据
        if event.button() == Qt.LeftButton and not self.row.get("feed"):
            if event.position().toPoint().y() >= self.height() - RESIZE_ZONE:
                # 下边缘：改时长。别起 QDrag，否则鼠标一挪就被当成「拖去改期」
                self._resizing = True
                self._grab_y = event.globalPosition().toPoint().y()
                self._h0 = self.height()
                event.accept()
                return
            self._press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._resizing:
            self._resizing = False
            self.setCursor(Qt.PointingHandCursor)
            # 拖动时把 tooltip 借去显示实时分钟数了，拖完换回正常那份
            self.setToolTip(model.row_tooltip(self.row))
            self.resized.emit(self.row, self._minutes_now())
            event.accept()
            return
        if (self._press is not None
                and self.rect().contains(event.position().toPoint())):
            self.clicked_.emit(self.row, self.mapToGlobal(QPoint(0, self.height())))
        self._press = None
        super().mouseReleaseEvent(event)

    def _minutes_now(self) -> int:
        """把当前块高换算回分钟，吸附到 SNAP_MIN。

        排块时高度是 `dur/60*HOUR_H - 2`（那 2px 是块之间的缝），反算要加回来，
        否则拖完一轮时长会每次少掉约 2 分钟地往下漂。
        """
        raw = (self.height() + 2) / HOUR_H * 60
        return max(SNAP_MIN, round(raw / SNAP_MIN) * SNAP_MIN)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        p = event.position().toPoint()
        if self._resizing:
            if event.buttons() & Qt.LeftButton:
                dy = event.globalPosition().toPoint().y() - self._grab_y
                self.resize(self.width(), max(MIN_BLOCK_H, self._h0 + dy))
                self.setToolTip(f"时长 {self._minutes_now()} 分钟")
            return
        if not event.buttons():
            near = (p.y() >= self.height() - RESIZE_ZONE
                    and not self.row.get("feed"))
            self.setCursor(Qt.SizeVerCursor if near else Qt.PointingHandCursor)
            return
        if self._press is not None and \
                (p - self._press).manhattanLength() > 8:
            mime = QMimeData()
            mime.setData(model.MIME_CAL,
                         f"{self.row['id']}|{self.row['occ']}".encode())
            mime.setText(self.row["title"])
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec(Qt.CopyAction)
            self._press = None
        super().mouseMoveEvent(event)


class _CanvasBase(QWidget):
    """按天等分列宽 + 绝对定位的公共部分。"""

    dropped = Signal(int, str, QDate, str)
    block_clicked = Signal(dict, QPoint)
    block_resized = Signal(dict, int)     # 拖下边缘改了时长
    day_more = Signal(QDate)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._days: list[QDate] = []
        self._rows: dict[str, list[dict]] = {}
        self._kids: list[QWidget] = []
        self.setAcceptDrops(True)

    def set_data(self, days: list[QDate], rows: dict[str, list[dict]]) -> None:
        self._days = days
        self._rows = rows
        self.relayout()

    def _col_w(self) -> float:
        if not self._days:
            return 0.0
        return (self.width() - GUTTER) / len(self._days)

    def _day_at(self, x: int) -> QDate | None:
        w = self._col_w()
        if w <= 0:
            return None
        i = int((x - GUTTER) // w)
        return self._days[i] if 0 <= i < len(self._days) else None

    def _take_payload(self, event) -> tuple[int, str] | None:
        raw = bytes(event.mimeData().data(model.MIME_CAL)).decode()
        if "|" not in raw:
            return None
        tid, occ = raw.split("|", 1)
        return int(tid), occ

    def _clear_kids(self) -> None:
        for k in self._kids:
            k.deleteLater()
        self._kids.clear()

    def relayout(self) -> None:
        raise NotImplementedError


class AllDayCanvas(_CanvasBase):
    """全天带：没有时间的任务排在这里，钉在滚动区外面。"""

    slot_add = Signal(QDate, QPoint)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._lines = 2
        self.setFixedHeight(self.need_h(2))

    @staticmethod
    def need_h(lines: int) -> int:
        return 6 + ROW_H * max(1, min(lines, 3))

    def set_data(self, days: list[QDate], rows: dict[str, list[dict]]) -> None:
        counts = [sum(1 for r in rows.get(model.iso(d), []) if not r.get("time"))
                  for d in days] or [0]
        lines = max(1, min(max(counts), 3))
        self.setFixedHeight(self.need_h(lines))
        super().set_data(days, rows)

    def relayout(self) -> None:
        self._clear_kids()
        w = self._col_w()
        if w <= 0:
            QTimer.singleShot(0, self.relayout)
            return
        for i, d in enumerate(self._days):
            x0 = GUTTER + i * w
            allday = [r for r in self._rows.get(model.iso(d), [])
                      if not r.get("time")]
            for k, row in enumerate(allday[:2]):
                chip = month_view.TaskBar(row, self)
                chip.setGeometry(int(x0 + 2), 4 + k * ROW_H,
                                 max(20, int(w - 6)), ROW_H - 3)
                chip.clicked_.connect(self.block_clicked.emit)
                chip.show()
                self._kids.append(chip)
            if len(allday) > 2:
                more = QPushButton(f"+{len(allday) - 2}", self)
                more.setObjectName("CalMore")
                more.setGeometry(int(x0 + 2), 4 + 2 * ROW_H,
                                 max(20, int(w - 6)), ROW_H - 4)
                more.clicked.connect(lambda _=False, dd=d: self.day_more.emit(dd))
                more.show()
                self._kids.append(more)
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._days:
            self.relayout()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setPen(QColor(theme.get("muted")))
        f = QFont()
        f.setPointSizeF(10.5)
        p.setFont(f)
        p.drawText(QRectF(0, 4, GUTTER - 8, 16),
                   Qt.AlignRight | Qt.AlignTop, "全天")
        p.setPen(QColor(theme.get("border")))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # childAt 是 QWidget 的方法，不是 QMouseEvent 的：写成 event.childAt 会
        # 让「点空白处新建」直接抛 AttributeError，日/周/多日视图这条路径一直是死的
        if event.button() != Qt.LeftButton or self.childAt(
                event.position().toPoint()):
            return
        d = self._day_at(event.position().toPoint().x())
        if d:
            self.slot_add.emit(d, event.globalPosition().toPoint())

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(model.MIME_CAL):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        payload = self._take_payload(event)
        d = self._day_at(event.position().toPoint().x())
        if payload and d:
            self.dropped.emit(payload[0], payload[1], d, "")
            event.acceptProposedAction()


class TimeCanvas(_CanvasBase):
    """24 小时网格：自己画线，用绝对坐标挂任务块。"""

    slot_add = Signal(QDate, str, QPoint)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(HOUR_H * 24)

    def _slot_at(self, y: int) -> str:
        """y → 就近的半点时间。"""
        mins = int(max(0.0, min(23.99, y / HOUR_H * 60)) / 30) * 30
        return f"{mins // 60:02d}:{mins % 60:02d}"

    def relayout(self) -> None:
        self._clear_kids()
        w = self._col_w()
        if w <= 0:
            QTimer.singleShot(0, self.relayout)
            return
        for i, d in enumerate(self._days):
            x0 = GUTTER + i * w
            rows = self._rows.get(model.iso(d), [])
            for row, lane, total in _lanes(rows):
                st = _minutes(row["time"])
                dur = max(int(row.get("duration") or 0) or DEFAULT_MIN, 30)
                bw = (w - 6) / total
                blk = TimeBlock(row, self)
                blk.setGeometry(int(x0 + 2 + lane * bw),
                                int(st / 60 * HOUR_H),
                                max(20, int(bw - 2)),
                                max(MIN_BLOCK_H, int(dur / 60 * HOUR_H) - 2))
                blk.clicked_.connect(self.block_clicked.emit)
                blk.resized.connect(self.block_resized.emit)
                blk.show()
                self._kids.append(blk)
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._days:
            self.relayout()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self._col_w()
        line = QColor(theme.get("border"))
        half = QColor(theme.get("border"))
        half.setAlpha(110)
        f = QFont()
        f.setPointSizeF(10.5)
        p.setFont(f)

        # 周末 / 今天底色。日视图只有一列，再涂底色就等于整屏刷灰/刷蓝，
        # 滴答的日视图是白底，所以这两种高亮都只在多列视图里画。
        multi = len(self._days) > 1
        for i, d in enumerate(self._days):
            x = GUTTER + i * w
            if multi and d.dayOfWeek() >= 6:
                p.fillRect(QRectF(x, 0, w, HOUR_H * 24),
                           QColor(theme.get("surface_hi")))
            if multi and d == QDate.currentDate():
                p.fillRect(QRectF(x, 0, w, HOUR_H * 24),
                           QColor(theme.get("accent_soft")))

        # 小时线（整点实线 + 半点浅色细线）与左侧刻度
        for h in range(25):
            y = h * HOUR_H
            p.setPen(QPen(line))
            p.drawLine(GUTTER, y, self.width(), y)
            if h < 24:
                p.setPen(QColor(theme.get("muted")))
                p.drawText(QRectF(0, y + 2, GUTTER - 8, 16),
                           Qt.AlignRight | Qt.AlignTop, f"{h:02d}:00")
                p.setPen(QPen(half))
                p.drawLine(GUTTER, y + HOUR_H // 2, self.width(), y + HOUR_H // 2)

        # 列分隔线
        p.setPen(QPen(line))
        for i in range(len(self._days) + 1):
            x = int(GUTTER + i * w)
            p.drawLine(x, 0, x, HOUR_H * 24)

        # 当前时间红线
        today = QDate.currentDate()
        if today in self._days and w > 0:
            t = QTime.currentTime()
            y = (t.hour() * 60 + t.minute()) / 60 * HOUR_H
            p.setPen(QPen(QColor(theme.get("red")), 1.4))
            p.drawLine(GUTTER, int(y), self.width(), int(y))
            x = GUTTER + self._days.index(today) * w
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.get("red")))
            p.drawEllipse(QRectF(x - 3, y - 3, 7, 7))
        p.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # childAt 是 QWidget 的方法，不是 QMouseEvent 的：写成 event.childAt 会
        # 让「点空白处新建」直接抛 AttributeError，日/周/多日视图这条路径一直是死的
        if event.button() != Qt.LeftButton or self.childAt(
                event.position().toPoint()):
            return
        d = self._day_at(event.position().toPoint().x())
        if d:
            self.slot_add.emit(d, self._slot_at(event.position().toPoint().y()),
                               event.globalPosition().toPoint())

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(model.MIME_CAL):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        payload = self._take_payload(event)
        d = self._day_at(event.position().toPoint().x())
        if payload and d:
            self.dropped.emit(payload[0], payload[1], d,
                              self._slot_at(event.position().toPoint().y()))
            event.acceptProposedAction()


class TimeGridView(QWidget):
    """日 / 周 / 多日视图外壳：日期表头 + 全天带 + 可滚动时间网格。"""

    bar_clicked = Signal(dict, QPoint)
    bar_right = Signal(dict, QPoint)      # 右键色块 → 快捷菜单
    resized = Signal(dict, int)           # 拖下边缘改时长
    dropped = Signal(int, str, QDate, str)
    slot_add = Signal(QDate, str, QPoint)
    day_more = Signal(QDate)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._days: list[QDate] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.head = QFrame()
        self.head.setObjectName("CalTimeHead")
        self._head_lay = QHBoxLayout(self.head)
        self._head_lay.setContentsMargins(0, 0, 0, 0)
        self._head_lay.setSpacing(0)
        root.addWidget(self.head)

        self.allday = AllDayCanvas()
        self.allday.block_clicked.connect(self.bar_clicked.emit)
        self.allday.dropped.connect(self.dropped.emit)
        self.allday.day_more.connect(self.day_more.emit)
        self.allday.slot_add.connect(lambda d, pos: self.slot_add.emit(d, "", pos))
        root.addWidget(self.allday)

        self.canvas = TimeCanvas()
        self.canvas.block_clicked.connect(self.bar_clicked.emit)
        self.canvas.block_resized.connect(self.resized.emit)
        self.canvas.dropped.connect(self.dropped.emit)
        self.canvas.slot_add.connect(self.slot_add.emit)
        self.canvas.day_more.connect(self.day_more.emit)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.canvas)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        root.addWidget(self.scroll, 1)

        self._tick = QTimer(self)
        self._tick.timeout.connect(self.canvas.update)
        self._tick.start(60_000)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """右键色块 / 全天带的条 → 弹快捷菜单（和月视图同一套）。"""
        bar = model.bar_at(self, event.pos())
        if bar is not None and not bar.row.get("feed"):
            self.bar_right.emit(bar.row, event.globalPos())
            event.accept()
            return
        super().contextMenuEvent(event)

    def show_days(self, days: list[QDate], rows: dict[str, list[dict]]) -> None:
        changed = days != self._days
        self._days = days
        self._rebuild_head()
        self.allday.set_data(days, rows)
        self.canvas.set_data(days, rows)
        if changed:
            self._scroll_to_now()

    def _scroll_to_now(self) -> None:
        """打开时间轴时定位到当前时刻前两小时（滴答的行为）。"""
        t = QTime.currentTime()
        mins = max(0, t.hour() * 60 + t.minute() - 120)
        self.scroll.verticalScrollBar().setValue(int(mins / 60 * HOUR_H))

    def _rebuild_head(self) -> None:
        while self._head_lay.count():
            it = self._head_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        pad = QWidget()
        pad.setFixedWidth(GUTTER)
        self._head_lay.addWidget(pad)
        today = QDate.currentDate()
        for d in self._days:
            cell = QFrame()
            cell.setObjectName("CalTimeHeadCell")
            widgets._apply_property(cell, "today", "true" if d == today else "false")
            widgets._apply_property(cell, "dim",
                                    "true" if d.month() != today.month() else "false")
            lay = QHBoxLayout(cell)
            lay.setContentsMargins(0, 5, 0, 5)
            lay.setSpacing(4)
            lay.addStretch(1)
            wd = QLabel("日一二三四五六"[d.dayOfWeek() % 7])
            wd.setObjectName("CalTimeWd")
            num = QLabel(str(d.day()))
            num.setObjectName("CalTimeNum")
            lay.addWidget(wd)
            lay.addWidget(num)
            flag = holidays.day_type(d)
            if flag:
                b = QLabel(flag)
                b.setObjectName("CalFlag")
                widgets._apply_property(b, "flagKind",
                                        "off" if flag == "休" else "work")
                lay.addWidget(b)
            lay.addStretch(1)
            self._head_lay.addWidget(cell, 1)
