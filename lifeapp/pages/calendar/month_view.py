"""月视图 / 多周视图：一格一天，任务以色条排布。

对齐滴答清单的月视图细节：
- 周日为首；跨月的日子数字变灰、整格淡底；周末列淡底
- 日期号：今天 = 白字蓝圆；休/班角标贴在数字右上；节日名绿色右对齐
- 色条底色 = 优先级浅色（无=灰 低=蓝 中=黄 高=红），已完成划掉
- 格子放不下时收成「+N」，点它切到当天
- 色条可拖到别的格子改期；重复任务拖之前先问「仅此周期 / 所有未完成周期」
"""
from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import (
    Qt, QDate, QEasingCurve, QEvent, QPoint, QPropertyAnimation,
    Signal, QMimeData,
)
from PySide6.QtGui import QFont, QDrag, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QGridLayout, QVBoxLayout, QLabel,
    QPushButton, QScrollArea,
)

from ... import dateparse, theme, widgets
from . import holidays, model

BAR_H = 18          # 色条行距（滴答实测：条 17 + 缝 1）
HEAD_H = 28         # 日期号那一行的高度
WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]


class TaskBar(QFrame):
    """一条任务色条。"""

    clicked_ = Signal(dict, QPoint)
    dragged = Signal(dict)

    def __init__(self, row: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.row = row
        self._press: QPoint | None = None
        self.setObjectName("CalBar")
        self.setToolTip(model.row_tooltip(row))
        self.setFixedHeight(BAR_H - 1)
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 5, 0)
        lay.setSpacing(3)
        text = row["title"]
        if row.get("time"):
            text = f"{dateparse.human_time(row['time'])} {text}"
        self.lbl = widgets.ElidedLabel(text)
        self.lbl.setObjectName("CalBarText")
        f = QFont()
        f.setStrikeOut(bool(row["done"]))
        self.lbl.setFont(f)
        lay.addWidget(self.lbl)
        self._apply_colors(False)

    def _apply_colors(self, hover: bool) -> None:
        bg, fg = model.row_bar_color(self.row)
        if hover:
            bg = theme.lerp_color(bg, theme.get("text_hi"),
                                  0.10 if theme.is_dark() else 0.06)
        self.setStyleSheet(
            f"#CalBar {{ background: {bg}; border: none; border-radius: 4px; }}"
            f"#CalBarText {{ color: {fg}; font-size: 11.5px; background: transparent; }}")

    def enterEvent(self, event) -> None:  # noqa: N802
        self._apply_colors(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._apply_colors(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and not self.row.get("feed"):
            self._press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (event.button() == Qt.LeftButton and self._press is not None
                and self.rect().contains(event.position().toPoint())):
            self.clicked_.emit(self.row, self.mapToGlobal(QPoint(0, self.height())))
        self._press = None
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press is not None and \
                (event.position().toPoint() - self._press).manhattanLength() > 8:
            if model.trainer_drag_blocked(self.row,
                                          event.globalPosition().toPoint()):
                self._press = None
                return
            self._start_drag()
            self._press = None
        super().mouseMoveEvent(event)

    def _start_drag(self, at: QPoint | None = None) -> None:
        """按住位置即预览图上的热区，拖起来跟手；预览图自己画，
        不去 grab 一个从没显示过的临时控件。"""
        mime = QMimeData()
        mime.setData(model.MIME_CAL,
                     f"{self.row['id']}|{self.row['occ']}".encode())
        mime.setText(self.row["title"])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self._drag_pixmap())
        grab = at or self._press or QPoint(8, 8)
        drag.setHotSpot(QPoint(min(grab.x(), self.width() - 1),
                               min(grab.y(), 14)))
        drag.exec(Qt.DropAction.MoveAction)

    def _drag_pixmap(self):
        """按色条本来的样子画一张拖拽预览（含省略号）。"""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QFontMetrics
        text = self.lbl.text()
        fm = QFontMetrics(self.lbl.font())
        w = min(max(fm.horizontalAdvance(text) + 20, 60), self.width())
        text = fm.elidedText(text, Qt.ElideRight, w - 18)
        pm = QPixmap(w, 26)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        bg, fg = model.row_bar_color(self.row)
        p.setPen(QPen(QColor(theme.get("border_strong"))))
        p.setBrush(QColor(bg))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, 25), 5, 5)
        p.setPen(QColor(fg))
        p.setFont(self.lbl.font())
        p.drawText(QRectF(9, 0, w - 18, 26),
                   Qt.AlignVCenter | Qt.AlignLeft, text)
        p.end()
        return pm


class DayCell(QFrame):
    """一格一天。"""

    bar_clicked = Signal(dict, QPoint)
    dropped = Signal(int, str, QDate)       # (todo_id, occ, 目标日期)
    add_clicked = Signal(QDate, QPoint)
    more_clicked = Signal(QDate)

    def __init__(self, date: QDate, home_month: int, parent=None):
        super().__init__(parent)
        self._date = date
        self._home_month = home_month
        self._bars: list[TaskBar] = []
        self._rows: list[dict] = []
        self._max_bars = 4
        self.setObjectName("CalCell")
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WA_StyledBackground, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(2)

        head = QHBoxLayout()
        head.setSpacing(4)
        self.num = QLabel(str(date.day()))
        self.num.setObjectName("CalDayNum")
        self.num.setAlignment(Qt.AlignCenter)
        self.num.setMinimumWidth(24)
        self.num.setFixedHeight(24)
        head.addWidget(self.num)

        flag = holidays.day_type(date)
        if flag:
            badge = QLabel(flag)
            badge.setObjectName("CalFlag")
            widgets._apply_property(badge, "flagKind", "off" if flag == "休" else "work")
            head.addWidget(badge)
        head.addStretch(1)

        # 滴答实测：节日名贴在格子右端（「10 ……… 教师节」），不跟着日号排
        name = holidays.festival(date)
        if name:
            fest = QLabel(name)
            fest.setObjectName("CalFestival")
            widgets._apply_property(fest, "major", "true" if holidays.is_major(date)
                                    else "false")
            head.addWidget(fest)

        plus = QPushButton("+", self)
        plus.setObjectName("CalCellPlus")
        plus.setFixedSize(20, 20)
        plus.setCursor(Qt.PointingHandCursor)
        plus.setToolTip("在此天新建任务")
        plus.clicked.connect(lambda: self.add_clicked.emit(
            self._date, self.mapToGlobal(QPoint(8, HEAD_H))))
        self.plus = plus
        plus.hide()
        lay.addLayout(head)

        self.bar_box = QWidget()
        self.bar_lay = QVBoxLayout(self.bar_box)
        self.bar_lay.setContentsMargins(0, 0, 0, 0)
        self.bar_lay.setSpacing(1)     # 条 17 + 缝 1 = 行距 BAR_H
        lay.addWidget(self.bar_box)

        self.more = QPushButton()
        self.more.setObjectName("CalMore")
        self.more.setCursor(Qt.PointingHandCursor)
        self.more.clicked.connect(lambda: self.more_clicked.emit(self._date))
        self.more.hide()
        lay.addWidget(self.more)
        lay.addStretch(1)
        self._sync_state()

    # ------------------------------------------------------------ 状态
    def _sync_state(self) -> None:
        today = QDate.currentDate()
        widgets._apply_property(self, "dim",
                                "true" if self._date.month() != self._home_month
                                else "false")
        widgets._apply_property(self, "weekend",
                                "true" if self._date.dayOfWeek() >= 6 else "false")
        widgets._apply_property(self, "today",
                                "true" if self._date == today else "false")
        # 滴答的写法：每月 1 号写全「9月1日」，其余只写日号
        self.num.setText(f"{self._date.month()}月1日" if self._date.day() == 1
                         else str(self._date.day()))

    def date(self) -> QDate:
        return self._date

    def set_rows(self, rows: list[dict]) -> None:
        self._rows = rows
        self._rebuild()

    def set_max_bars(self, n: int) -> None:
        if n != self._max_bars:
            self._max_bars = n
            self._rebuild()

    def _rebuild(self) -> None:
        while self.bar_lay.count():
            it = self.bar_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._bars.clear()
        shown = self._rows if self._max_bars >= 99 else self._rows[:self._max_bars]
        for row in shown:
            bar = TaskBar(row)
            bar.clicked_.connect(self.bar_clicked.emit)
            self.bar_lay.addWidget(bar)
            self._bars.append(bar)
        left = len(self._rows) - len(shown)
        self.more.setVisible(left > 0)
        if left > 0:
            self.more.setText(f"+{left} 更多")

    # ------------------------------------------------------------ 交互
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.plus.move(self.width() - self.plus.width() - 4, 2)
        # 行高固定，能放几条由格子自己多高决定，多的收进 +N
        limit = max(1, int((self.height() - HEAD_H - 10) // BAR_H))
        if limit != self._max_bars:
            self.set_max_bars(limit)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.plus.show()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.plus.hide()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and not self._bars:
            self.add_clicked.emit(self._date, event.globalPosition().toPoint())
        elif event.button() == Qt.LeftButton:
            y = event.position().toPoint().y()
            if y > HEAD_H + len(self._bars[:self._max_bars]) * BAR_H + 6:
                self.add_clicked.emit(self._date, event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(model.MIME_CAL):
            widgets._apply_property(self, "drop", "true")
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        widgets._apply_property(self, "drop", "false")

    def dropEvent(self, event) -> None:  # noqa: N802
        widgets._apply_property(self, "drop", "false")
        raw = bytes(event.mimeData().data(model.MIME_CAL)).decode()
        if "|" not in raw:
            return
        tid, occ = raw.split("|", 1)
        self.dropped.emit(int(tid), occ, self._date)
        event.acceptProposedAction()


class MonthView(QWidget):
    """月视图：一条可以连续滚动的长周条（上月 + 本月 + 下月）。

    滴答的月视图不是「把 5 行硬塞进窗口」，而是固定行高、超出部分滚动、
    最后一行被窗口切掉。所以这里用滚动区 + 绝对定位；滚到跨月时悄悄把
    带子整体往前/往后挪一个月，并把滚动位置对齐回同一个日期，
    看起来就是一条滚不完的带子。
    """

    bar_clicked = Signal(dict, QPoint)
    bar_right = Signal(dict, QPoint)      # 右键色条 → 快捷菜单
    dropped = Signal(int, str, QDate)
    day_add = Signal(QDate, QPoint)
    day_more = Signal(QDate)
    anchor_changed = Signal(QDate)      # 滚动跨月后告诉外面「现在顶到几月了」

    ROW_MIN = 150        # 行高下限
    ROW_DIV = 5.0        # 滴答实测：视口正好看见 5 行（第 6 行露头）

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._mode = "month"             # month = 可滚动长条；weeks2 = 固定两行
        self._anchor = QDate.currentDate()
        self._weeks: list[list[QDate]] = []
        self._mid = 0                    # 本月第一周在 _weeks 里的下标
        self._cells: list[DayCell] = []
        self._provider = None
        self._rows: dict[str, list[dict]] = {}
        self._row_h = 194
        self._reanchoring = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QFrame()
        head.setObjectName("CalWeekHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(1)
        for name in WEEKDAYS:
            lbl = QLabel(name)
            lbl.setObjectName("CalWd")
            lbl.setAlignment(Qt.AlignCenter)
            hl.addWidget(lbl, 1)
        root.addWidget(head)

        self.content = QWidget()
        self.grid = QGridLayout(self.content)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.content)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        root.addWidget(self.scroll, 1)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)
        # 视口的最终尺寸比第一次布局晚到，装个监听在它定型后重排一次，
        # 免得行高/列宽用着没定型的数值（列宽交给网格了，行高还得自己算）
        self.scroll.viewport().installEventFilter(self)

        self._anim = QPropertyAnimation(self.scroll.verticalScrollBar(), b"value", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(self._anim_done)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """右键落在某条色条上 → 交给页面弹快捷菜单。

        色条自己不接这个事件，Qt 会往父控件传，所以这里一次搞定，
        不用给 DayCell / 滚动视口各加一条转发信号。
        """
        bar = model.bar_at(self, event.pos())
        if bar is not None and not bar.row.get("feed"):
            self.bar_right.emit(bar.row, event.globalPos())
            event.accept()
            return
        super().contextMenuEvent(event)

    @contextmanager
    def _suppressed(self):
        """程序化改滚动位置期间不触发重锚。

        setValue 是同步发 valueChanged 的，所以进出处加锁、退出处还原就够了。
        早先是 singleShot(0) 延后解锁，结果缓动动画还在跑锁就掉了，动画中间的
        滚动值照样触发重锚，月份被弹回去 —— 看起来就是「翻页」而不是「滑动」。
        """
        prev = self._reanchoring
        self._reanchoring = True
        try:
            yield
        finally:
            self._reanchoring = prev

    # ------------------------------------------------------------ 数据入口
    def set_provider(self, fn) -> None:
        """fn(start_iso, end_iso) -> {日期: [条目]}；挪带子时由视图自己取数。"""
        self._provider = fn

    def show_month(self, anchor: QDate, keep_top: QDate | None = None) -> None:
        self._anim.stop()
        self._mode = "month"
        self._anchor = QDate(anchor.year(), anchor.month(), 1)
        with self._suppressed():
            self._build_window(keep_top)

    def show_two_weeks(self, anchor: QDate) -> None:
        """多周视图：固定两行铺满，不滚动。"""
        self._anim.stop()
        self._mode = "weeks2"
        self._anchor = anchor
        start = model.sunday_week_start(anchor)
        self._weeks = [[start.addDays(r * 7 + c) for c in range(7)] for r in range(2)]
        self._mid = 0
        self._fetch()
        self._layout()

    def reload(self) -> None:
        """筛选条件变了：重新取数、重排，但不动滚动位置。"""
        self._fetch()
        self._layout()

    def _build_window(self, keep_top: QDate | None, keep_frac: int = 0) -> None:
        # 带子取「锚点前两个月 + 锚点 + 后一个月」。只取前后各一个月时，
        # 锚点那个月的「第一周」常常正好是带子的第 0 行（9月1日所在的那周
        # 从8月30日开始），按 ← 回到 9 月就会停在顶端，被下一次重锚当成
        # 「用户滚到了上边界」而把月份又往前挪一格。多留两个月就没这问题。
        months = [self._anchor.addMonths(k) for k in (-2, -1, 0, 1)]
        # month_matrix 会带上跨月的首尾周，8 月的末周和 9 月的首周是同一周，
        # 直接 extend 会把那一周画两遍，按周首日去重
        weeks: list[list[QDate]] = []
        seen: set[str] = set()
        for m in months:
            for row in model.month_matrix(m.year(), m.month()):
                k = model.iso(row[0])
                if k in seen:
                    continue
                seen.add(k)
                weeks.append(row)
        self._weeks = weeks
        first = QDate(self._anchor.year(), self._anchor.month(), 1)
        self._mid = next((i for i, row in enumerate(weeks)
                          if row[0] <= first <= row[6]), 0)
        self._fetch()
        self._layout()
        self._scroll_to(self._offset_of(keep_top) + keep_frac if keep_top
                        else self._mid * self._row_h)

    def _fetch(self) -> None:
        if not self._weeks or self._provider is None:
            self._rows = {}
            return
        self._rows = self._provider(model.iso(self._weeks[0][0]),
                                    model.iso(self._weeks[-1][-1]))

    # ------------------------------------------------------------ 排布
    def _scroll_to(self, value: int) -> None:
        sb = self.scroll.verticalScrollBar()
        sb.setRange(0, max(0, self.content.height()
                           - self.scroll.viewport().height()))
        sb.setValue(int(value))

    def _row_height(self) -> int:
        if self._mode == "weeks2":
            return max(120, self.scroll.viewport().height() // 2 - 1)
        return max(self.ROW_MIN, int(self.scroll.viewport().height() / self.ROW_DIV))

    def _offset_of(self, d: QDate) -> int:
        for i, week in enumerate(self._weeks):
            if week[0] <= d <= week[6]:
                return i * self._row_h
        return self._mid * self._row_h

    def _top_date(self) -> QDate:
        i = int(self.scroll.verticalScrollBar().value() // max(1, self._row_h))
        i = max(0, min(len(self._weeks) - 1, i))
        return self._weeks[i][0]

    def _layout(self) -> None:
        """列宽交给网格自己分配。

        之前是拿 viewport().width() 手算绝对坐标，但首次布局时视口还没定型
        （宽 640、稳定后 1324），坐标和宽度来自两次不同的测量，格子会叠成
        一片空白。让 QGridLayout 自己分配就没这个时序问题。
        """
        for c in self._cells:
            c.deleteLater()
        self._cells.clear()
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not self._weeks:
            return
        self._row_h = self._row_height()
        rows = len(self._weeks)
        # 先把旧的行高设置清空：从月视图（18 行）切到多周（2 行）时，
        # 残留的 16 行最小高度还挂在那儿，网格会把 941px 平摊给 18 行，
        # 每格只剩 53px，多周视图被压成顶部两条细条。
        for r in range(max(self.grid.rowCount(), rows)):
            self.grid.setRowMinimumHeight(r, 0)
            self.grid.setRowStretch(r, 0)
        for r in range(rows):
            self.grid.setRowMinimumHeight(r, self._row_h)
        for c, d in enumerate(self._weeks[0]):
            self.grid.setColumnStretch(c, 1)
        for r, week in enumerate(self._weeks):
            last = (r == rows - 1) and self._mode == "weeks2"
            for c, d in enumerate(week):
                cell = DayCell(d, self._anchor.month())
                widgets._apply_property(cell, "sep", "false" if last else "true")
                widgets._apply_property(cell, "edge", "last" if c == 6 else "")
                cell.set_rows(self._rows.get(model.iso(d), []))
                cell.bar_clicked.connect(self.bar_clicked.emit)
                cell.dropped.connect(self.dropped.emit)
                cell.add_clicked.connect(self.day_add.emit)
                cell.more_clicked.connect(self.day_more.emit)
                self.grid.addWidget(cell, r, c)
                cell.show()
                self._cells.append(cell)
        self.content.setFixedHeight(max(rows * self._row_h,
                                      self.scroll.viewport().height()))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.scroll.viewport() and event.type() == QEvent.Resize \
                and self._weeks:
            self._relayout_keep_pos()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._weeks:
            self._relayout_keep_pos()

    def _relayout_keep_pos(self) -> None:
        keep = self._top_date()
        row_before = self._row_h
        with self._suppressed():
            self._layout()
            if self._mode == "month":
                # 行高变了就按周数比例换算，保证视觉上还在同一周
                frac = self.scroll.verticalScrollBar().value() % max(1, row_before)
                self._scroll_to(self._offset_of(keep) + frac * self._row_h
                                / max(1, row_before))

    # ------------------------------------------------------------ 连续滚动
    def _viewport_month(self, value: int) -> QDate:
        """视口里占格子最多的那个月 —— 标题跟着它走。

        取「顶部那一周属于哪个月」会错：9月1日所在的那周从 8/30 就开始，
        按 ← 回到 9 月时顶部正好是 8 月那周，标题会被判成 8 月，
        再被重锚拽到 7 月去。
        """
        row_h = max(1, self._row_h)
        lead = int(value // row_h)
        rows = max(1, self.scroll.viewport().height() // row_h + 1)
        band = self._weeks[lead: lead + rows]
        if not band:
            return QDate(self._anchor.year(), self._anchor.month(), 1)
        counts: dict[tuple[int, int], int] = {}
        for week in band:
            for d in week:
                key = (d.year(), d.month())
                counts[key] = counts.get(key, 0) + 1
        mid = band[len(band) // 2][3]          # 正中间那一周的周三，用来打破平票
        y, m = max(counts, key=lambda k: (counts[k],
                                          k == (mid.year(), mid.month())))
        return QDate(y, m, 1)

    def _on_scroll(self, value: int) -> None:
        """只在滚到带子两端时才补一节，并且保住「顶部是哪一周、差多少像素」，
        否则用户会觉得滚一下就被弹走。"""
        if self._mode != "month" or self._reanchoring or not self._weeks:
            return
        lead = value // max(1, self._row_h)          # 顶部在第几周
        frac = value % max(1, self._row_h)           # 该周内的像素偏移
        visible = max(1, self.scroll.viewport().height() // max(1, self._row_h))
        near_top = lead < 1
        near_bottom = lead + visible > len(self._weeks) - 2
        if not (near_top or near_bottom):
            return
        top = self._top_date()
        anchor = self._viewport_month(value)
        if anchor == self._anchor:
            return
        self._reanchoring = True
        try:
            self._anchor = anchor
            self._build_window(keep_top=top, keep_frac=frac)
        finally:
            self._reanchoring = False
        self.anchor_changed.emit(self._anchor)

    def _row_of(self, d: QDate) -> int | None:
        """d 落在带子的第几周；带子装不下就返回 None。"""
        for i, week in enumerate(self._weeks):
            if week[0] <= d <= week[6]:
                return i
        return None

    def _anim_done(self) -> None:
        self._reanchoring = False
        # 缓动结束后补一次重锚：滑到带子末端时把带子悄悄挪回中间，
        # 位置按「顶部是哪一周」还原，用户看不出来，但下一格不会滚不动。
        self._on_scroll(self.scroll.verticalScrollBar().value())

    def scroll_to_month(self, delta: int) -> None:
        """上一月 / 下一月：在当前这条带子里缓动滑过去，不是整页硬翻。

        带子本来就是「上月+本月+下月」，±1 月一定在里头，所以换月不需要重建，
        直接补间到目标月第一行即可 —— 中途画面连续，没有闪一下的翻页感。
        """
        target = QDate(self._anchor.year(), self._anchor.month(), 1).addMonths(delta)
        self._anchor = target
        if self._mode != "month":
            self.anchor_changed.emit(self._anchor)
            return
        row = self._row_of(target)
        if row is None:
            # 隔了好几个月（例如迷你月历直接跳过去），带子装不下，只能直接定位
            with self._suppressed():
                self._build_window(None)
            self.anchor_changed.emit(self._anchor)
            return
        self._mid = row                 # 带子没重建，本月首行的位置得跟着挪
        self._anim.stop()
        self._reanchoring = True          # 缓动途中的中间值不能触发重锚把月份弹回去
        self._anim.setStartValue(self.scroll.verticalScrollBar().value())
        self._anim.setEndValue(row * self._row_h)
        self._anim.start()
        self.anchor_changed.emit(self._anchor)

    def anchor(self) -> QDate:
        return self._anchor
