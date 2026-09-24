"""日历页：滴答清单式的头部 + 五种视图 + 右侧可见性面板。

结构
    9月 2026年 ⌄            [月 ⌄] [‹ 今天 ›] [侧栏] [⋯]
    ────────────────────────────────────────────────────
    视图（日 / 周 / 月 / 多日 / 多周）        │ 右侧面板

日历不再是独立的一套「事件」数据，而是待办任务的时间轴视图：
色条 = 某个任务在某个周期上的呈现，拖动即改期，点开即就地编辑。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QDate, QPoint, QPointF, QRectF, Signal
from PySide6.QtGui import (
    QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QMenu,
    QLineEdit, QTextEdit, QStackedWidget, QApplication, QDialog)

from ... import popups, services, sounds, theme, widgets
from ..base import Page
from ..todo import DatePickerPopup, _review_ask_sub
from . import model, style
from .feed_popup import FeedPopup
from .month_view import MonthView
from .new_card import NewTaskCard
from .repeat_dialog import RepeatDialog
from .row_menu import RowMenu
from .side_panel import MiniCalendar, ScheduleDrawer, SidePanel
from .task_card import TaskCard, _menu_icon
from .time_grid import TimeGridView


def _panel_icon(size: int = 18) -> QIcon:
    """滴答那个「收起右侧栏」图标：外框 + 竖线分出右侧栏 + 栏里一个左指箭头。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(theme.get("muted")), 1.4))
    r = QRectF(1.5, 2.5, size - 3, size - 5)
    p.drawRoundedRect(r, 3, 3)
    x = r.left() + r.width() * 0.62
    p.drawLine(int(x), int(r.top()), int(x), int(r.bottom()))
    cy = r.center().y()
    ax = x + (r.right() - x) / 2
    p.drawLine(QPointF(ax + 2.2, cy - 2.6), QPointF(ax - 1.4, cy))
    p.drawLine(QPointF(ax - 1.4, cy), QPointF(ax + 2.2, cy + 2.6))
    p.end()
    return QIcon(pm)


class TitleButton(QFrame):
    """滴答式标题：「9月」大号粗体 + 「2026年」小号 + ⌄，整块可点。"""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CalTitleBox")
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 2, 0)
        lay.setSpacing(4)
        self.big = QLabel()
        self.big.setObjectName("CalTitleBig")
        self.small = QLabel()
        self.small.setObjectName("CalTitleSmall")
        caret = QLabel("⌄")
        caret.setObjectName("CalTitleCaret")
        lay.addWidget(self.big)
        lay.addWidget(self.small)
        lay.addWidget(caret)
        lay.addStretch(1)

    def set_parts(self, big: str, small: str) -> None:
        self.big.setText(big)
        self.small.setText(small)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        widgets._apply_property(self, "hover", "true")
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        widgets._apply_property(self, "hover", "false")
        super().leaveEvent(event)


class CalendarPage(Page):
    """日历页。"""

    focusRequested = Signal(str, str)    # 右键「开始专注」→ 主窗口跳番茄钟

    def __init__(self):
        super().__init__("日历", "", bare=True)
        self.layout().setContentsMargins(14, 8, 14, 12)
        self.layout().setSpacing(8)

        self._f = model.CalFilter()
        self._view = "month"
        self._anchor = QDate.currentDate()
        self._card: TaskCard | None = None
        self._quick: NewTaskCard | None = None
        self._feeds: FeedPopup | None = None
        self._shown: tuple = ()        # 已渲染的 (视图, 年月)，用来区分「换月」和「原地刷新」

        style.apply_to(self)
        self._build_header()
        self._build_body()
        self._build_shortcuts()
        self.refresh()
        if theme.manager is not None:
            theme.manager.changed.connect(self._restyle)

    def _restyle(self) -> None:
        """主题切换后 QSS 里的颜色要重新取一遍。"""
        style.apply_to(self)
        for m in (self, self.panel, self.drawer):
            m.style().unpolish(m)
            m.style().polish(m)

    # ------------------------------------------------------------ 头部
    def _build_header(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.title_btn = TitleButton()
        self.title_btn.clicked.connect(self._jump_popup)
        row.addWidget(self.title_btn)
        row.addStretch(1)

        self.view_btn = QPushButton()
        self.view_btn.setObjectName("CalViewBtn")
        self.view_btn.setCursor(Qt.PointingHandCursor)
        self.view_btn.clicked.connect(self._view_menu)
        row.addWidget(self.view_btn)

        nav = QFrame()
        nav.setObjectName("CalNavGroup")
        nl = QHBoxLayout(nav)
        nl.setContentsMargins(2, 2, 2, 2)
        nl.setSpacing(0)
        # 滴答的上一/下一用的是上下箭头，不是左右尖括号
        for text, slot, tip in (
                ("⌃", self._prev, "上一个"),
                ("今天", self._goto_today, "回到今天"),
                ("⌄", self._next, "下一个")):
            b = QPushButton(text)
            b.setObjectName("CalToday" if tip == "回到今天" else "CalNavBtn")
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            nl.addWidget(b)
        row.addWidget(nav)

        self.panel_btn = QPushButton()
        self.panel_btn.setObjectName("CalIconBtn")
        style.reapply(self.panel_btn,
                      lambda: self.panel_btn.setIcon(_panel_icon()))
        self.panel_btn.setToolTip("显示 / 隐藏侧边面板")
        self.panel_btn.setCheckable(True)
        self.panel_btn.setChecked(True)
        self.panel_btn.setCursor(Qt.PointingHandCursor)
        self.panel_btn.toggled.connect(self._toggle_panel)
        row.addWidget(self.panel_btn)

        more = QPushButton()
        more.setObjectName("CalIconBtn")
        style.reapply(more, lambda: more.setIcon(_menu_icon("more", "muted", 18)))
        more.setToolTip("更多")
        more.setCursor(Qt.PointingHandCursor)
        more.clicked.connect(self._more_menu)
        self.more_btn = more
        row.addWidget(more)

        self.layout().addLayout(row)

    def _build_body(self) -> None:
        body = QHBoxLayout()
        body.setSpacing(8)

        self.stack = QStackedWidget()
        self.month = MonthView()
        self.grid = TimeGridView()
        self.stack.addWidget(self.month)
        self.stack.addWidget(self.grid)
        for view in (self.month, self.grid):
            view.bar_clicked.connect(self._open_card)
            view.bar_right.connect(self._row_menu)
            view.dropped.connect(self._on_dropped)
            view.day_more.connect(self._focus_day)
        self.month.set_provider(
            lambda s, e: model.group_by_date(model.parse(s), model.parse(e), self._f))
        self.month.anchor_changed.connect(self._on_anchor_changed)
        self.month.day_add.connect(self._quick_add)
        self.grid.slot_add.connect(self._quick_add_slot)
        self.grid.resized.connect(self._on_resized)
        body.addWidget(self.stack, 1)

        self.drawer = ScheduleDrawer(self._f)
        self.drawer.setVisible(False)
        self.drawer.changed.connect(self.refresh)
        body.addWidget(self.drawer)

        self.panel = SidePanel(self._f)
        self.panel.filter_changed.connect(self.refresh)
        self.panel.jumped.connect(self._jump_to)
        body.addWidget(self.panel)
        self.layout().addLayout(body, 1)

    def _build_shortcuts(self) -> None:
        """滴答用 D/1、W/2、M/3 切视图、← → 翻页。

        上下文必须是 WindowShortcut：日历里的格子、色条都不取焦点，
        WidgetWithChildrenShortcut 在这种页面上根本不会触发。
        输入框有焦点时让路，不打断打字。
        """
        def fire(fn) -> object:
            def _run():
                fw = QApplication.focusWidget()
                if isinstance(fw, (QLineEdit, QTextEdit)):
                    return
                fn()
            return _run

        for seq, view in (("D", "day"), ("W", "week"), ("M", "month"),
                          ("5", "days5"), ("2", "weeks2")):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(fire(lambda v=view: self._set_view(v)))
        for seq, delta in (("Left", -1), ("Right", 1)):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(fire(lambda n=delta: self._shift(n)))

    # ------------------------------------------------------------ 视图切换
    def _set_view(self, view: str) -> None:
        if view != self._view:
            self._shown = ()
        self._view = view
        self.refresh()

    def _shift(self, n: int) -> None:
        step = {"day": 1, "week": 7, "days5": 5, "weeks2": 14,
                "month": 0}[self._view]
        if not step:
            if self._view == "month":
                self.month.scroll_to_month(n)     # 带 220ms 缓动，不是硬翻
                return
            first = QDate(self._anchor.year(), self._anchor.month(), 1)
            self._anchor = first.addMonths(n)
        else:
            self._anchor = self._anchor.addDays(step * n)
        self.refresh()

    def _prev(self) -> None:
        self._shift(-1)

    def _next(self) -> None:
        self._shift(1)

    def _on_anchor_changed(self, d: QDate) -> None:
        """连续滚动把带子挪到了别的月份。"""
        self._anchor = QDate(d.year(), d.month(), 1)
        self._shown = ("month", self._anchor.year(), self._anchor.month())
        start, end = self._window()
        self._sync_title(start, end)
        self.panel.set_anchor(self._anchor)

    def _goto_today(self) -> None:
        today = QDate.currentDate()
        if self._view == "month" and self._shown[:1] == ("month",):
            cur = self.month.anchor()
            delta = (cur.year() - today.year()) * 12 + (cur.month() - today.month())
            if delta:
                self.month.scroll_to_month(-delta)
        self._anchor = today
        # 已经在同一个月就别重建带子，否则会把刚滑到位的滚动位置弹回月初
        self._shown = ("month", today.year(), today.month())
        self.refresh()

    def _jump_to(self, d: QDate) -> None:
        self._anchor = d
        self._shown = ()
        self.refresh()

    def _focus_day(self, d: QDate) -> None:
        """点「+N 更多」→ 切到当天视图。"""
        self._anchor = d
        self._view = "day"
        self._shown = ()
        self.refresh()

    def _build_view_menu(self) -> QMenu:
        """建菜单和弹菜单分开：exec 是模态的，拆开才测得到内容。"""
        menu = style.menu(self)
        for key, name, hint in model.VIEWS:
            act = menu.addAction(f"{name}          {hint}")
            act.setData(key)
            if key == self._view:
                act.setCheckable(True)
                act.setChecked(True)
        return menu

    def _view_menu(self) -> None:
        menu = self._build_view_menu()
        act = menu.exec(self.view_btn.mapToGlobal(
            QPoint(0, self.view_btn.height() + 4)))
        if act:
            self._set_view(str(act.data()))

    def _build_more_menu(self) -> tuple[QMenu, dict]:
        """⋯ 菜单：显示设置 / 安排任务 / 日历订阅 / 打印，每行带图标。"""
        menu = style.menu(self)
        disp = style.menu(menu)
        disp.setTitle("显示设置")
        show_done = disp.addAction("显示已完成任务")
        show_done.setCheckable(True)
        show_done.setChecked(self._f.show_done)
        disp_act = menu.addMenu(disp)
        disp_act.setIcon(_menu_icon("gear", "muted", 15))

        sched = menu.addAction(_menu_icon("note", "muted", 15), "安排任务")
        sched.setCheckable(True)
        sched.setChecked(self.drawer.isVisible())

        feed = menu.addAction(_menu_icon("rss", "muted", 15), "日历订阅")

        print_menu = style.menu(menu)
        print_menu.setTitle("打印")
        thumb = print_menu.addAction("缩略打印")
        detail = print_menu.addAction("打印详细")
        print_act = menu.addMenu(print_menu)     # 不 addMenu 父菜单里看不到「打印」
        print_act.setIcon(_menu_icon("printer", "muted", 15))
        return menu, {"done": show_done, "sched": sched, "feed": feed,
                      "thumb": thumb, "detail": detail}

    def _more_menu(self) -> None:
        menu, acts = self._build_more_menu()
        act = menu.exec(self.more_btn.mapToGlobal(
            QPoint(-100, self.more_btn.height() + 4)))
        if act is acts["done"]:
            self._f.show_done = acts["done"].isChecked()
            self._f.save()
            self.refresh()
        elif act is acts["sched"]:
            self._show_drawer(acts["sched"].isChecked())
        elif act is acts["feed"]:
            self._show_feeds()
        elif act in (acts["thumb"], acts["detail"]):
            self._print_view(detailed=(act is acts["detail"]))

    def _show_feeds(self) -> None:
        """日历订阅管理弹层。同一时刻只留一个，重复点就把它拉到最前。"""
        if self._feeds is not None and self._feeds.isVisible():
            self._feeds.raise_()
            self._feeds.activateWindow()
            return
        if self._feeds is not None:
            self._feeds.deleteLater()      # 上一次的实例已经关掉了，别攒着
        pop = FeedPopup(self)
        pop.changed.connect(self.refresh)
        self._feeds = pop
        popups.place_popup(pop, self.more_btn)
        pop.show()

    def _show_drawer(self, on: bool) -> None:
        """安排任务和右侧面板互斥，和滴答一样二选一。"""
        self.drawer.setVisible(on)
        if on and self.panel.isVisible():
            self.panel_btn.setChecked(False)     # 触发 _toggle_panel 收起面板
        if on:
            self.drawer.rebuild()

    def _toggle_panel(self, on: bool) -> None:
        self.panel.setVisible(on)
        if on and self.drawer.isVisible():
            self.drawer.setVisible(False)

    def _print_view(self, detailed: bool = False, printer=None) -> bool:
        """缩略 = 当前视图整页缩进一张纸；详细 = 按原始尺寸分页拼印。

        ``printer`` 只为测试留的注入口（跑真打印机对话框没法自动化）；
        返回是否真的写出了内容，调用方不关心。
        """
        try:
            from PySide6.QtPrintSupport import QPrinter, QPrintDialog
        except ImportError:
            popups.notify(self, "打印", "当前环境缺少 QtPrintSupport，无法打印。",
                          danger=True)
            return False
        pix = self.stack.currentWidget().grab()
        if pix.width() < 10 or pix.height() < 10:
            return False                       # 视图还没排好版，没什么可印
        if printer is None:
            printer = QPrinter(QPrinter.HighResolution)
            dlg = QPrintDialog(printer, self)
            dlg.setWindowTitle("打印日历")
            if dlg.exec() != QDialog.Accepted:
                return False
        out = QPainter(printer)
        page = out.viewport()
        if page.width() < 10 or page.height() < 10:
            out.end()
            popups.notify(self, "打印", "打印机返回了空画幅，已取消。",
                          danger=True)
            return False
        if not detailed:
            scaled = pix.scaled(page.size(), Qt.KeepAspectRatio,
                                Qt.SmoothTransformation)
            out.drawPixmap(page.topLeft(), scaled)
        else:
            # 原始尺寸横向铺满纸面，纵向按页高切片
            ratio = page.width() / pix.width()
            sh = max(1, int(page.height() / ratio))
            y = 0
            while y < pix.height():
                strip = pix.copy(0, y, pix.width(), min(sh, pix.height() - y))
                out.drawPixmap(page.topLeft(), strip.scaled(
                    page.width(), max(1, int(strip.height() * ratio)),
                    Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
                y += sh
                if y < pix.height():
                    out.newPage()
        out.end()
        return True

    def _jump_popup(self) -> None:
        pop = QFrame(self, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        pop.setObjectName("DatePickerPopup")
        style.apply_to(pop)
        lay = QVBoxLayout(pop)
        lay.setContentsMargins(6, 6, 6, 6)
        mini = MiniCalendar()
        mini.set_anchor(self._anchor)
        mini.jumped.connect(lambda d: (self._jump_to(d), pop.close()))
        lay.addWidget(mini)
        pop.adjustSize()
        pop.show()

    # ------------------------------------------------------------ 刷新
    def _window(self) -> tuple[QDate, QDate]:
        """当前视图要渲染的日期区间。"""
        if self._view == "day":
            return self._anchor, self._anchor
        if self._view == "week":
            start = model.sunday_week_start(self._anchor)
            return start, start.addDays(6)
        if self._view == "days5":
            start = model.sunday_week_start(self._anchor)
            return start, start.addDays(4)
        if self._view == "weeks2":
            start = model.sunday_week_start(self._anchor)
            return start, start.addDays(13)
        first = QDate(self._anchor.year(), self._anchor.month(), 1)
        return model.month_span(first.year(), first.month())

    def refresh(self, keep_card: bool = False) -> None:
        """重画当前视图。keep_card=True 用于卡片内部编辑后刷新，
        否则改个优先级就会把正在编辑的卡片自己关掉。"""
        start, end = self._window()
        rows = model.group_by_date(start, end, self._f)
        if not keep_card:
            self._close_card()

        key = (self._view, self._anchor.year(), self._anchor.month())
        if self._view == "month":
            self.stack.setCurrentWidget(self.month)
            if key != self._shown:
                self.month.show_month(self._anchor)
            else:
                self.month.reload()          # 只是数据/筛选变了，别把滚动位置弹回去
        elif self._view == "weeks2":
            self.stack.setCurrentWidget(self.month)
            self.month.show_two_weeks(self._anchor)
        else:
            n = {"day": 1, "week": 7, "days5": 5}[self._view]
            s = (self._anchor if self._view == "day"
                 else model.sunday_week_start(self._anchor))
            days = [s.addDays(i) for i in range(n)]
            self.stack.setCurrentWidget(self.grid)
            self.grid.show_days(days, rows)

        self._shown = key
        self._sync_title(start, end)
        self.panel.set_anchor(self._anchor)
        self.view_btn.setText(f"{model.VIEW_NAMES[self._view]}  ⌄")
        if self.drawer.isVisible():
            self.drawer.rebuild()

    def _sync_title(self, start: QDate, end: QDate) -> None:
        """滴答的标题是「9月」大字 + 「2026年」小字，其它视图把日期段拆开排。"""
        wd = "日一二三四五六"
        if self._view in ("month", "weeks2"):
            self.title_btn.set_parts(f"{self._anchor.month()}月",
                                     f"{self._anchor.year()}年")
        elif self._view == "day":
            self.title_btn.set_parts(
                f"{self._anchor.day()}日",
                f"{self._anchor.month()}月 · 周{wd[self._anchor.dayOfWeek() % 7]}")
        elif start == end:
            self.title_btn.set_parts(f"{start.day()}日", f"{start.month()}月")
        elif start.month() == end.month():
            self.title_btn.set_parts(
                f"{start.month()}月",
                f"{start.day()}日 - {end.day()}日 · {start.year()}年")
        else:
            self.title_btn.set_parts(
                f"{start.month()}月{start.day()}日 - {end.month()}月{end.day()}日",
                f"{end.year()}年")

    # ------------------------------------------------------------ 交互
    def _open_card(self, row: dict, pos: QPoint) -> None:
        if row.get("feed"):
            return          # 订阅事件是只读的，点开没有可编辑的东西
        self._close_card()
        self._card = TaskCard(row, self)
        self._card.changed.connect(self._card_changed)
        self._card.closed.connect(lambda: setattr(self, "_card", None))
        self._card.show_near(pos + QPoint(0, 4))

    def _card_changed(self) -> None:
        card = self._card
        self.refresh(keep_card=True)
        if card:
            card._sync()       # 色条重建了，卡片跟着刷新成库里的最新值

    def _close_card(self) -> None:
        if self._card:
            card = self._card
            self._card = None
            card.save_note()
            card.close()

    def _on_dropped(self, todo_id: int, occ: str, target: QDate,
                    time: str | None = None) -> None:
        """拖拽落点 / 菜单上的快捷日期：未排期任务直接排期，
        重复任务先问改哪个范围。

        刷新一律走 _card_changed（保住可能开着的编辑卡），
        否则「卡片开着 + 拖另一条」会把用户正在写的备注冲掉。
        """
        todo = services.todo_get(todo_id)
        if not todo:
            return
        new_date = target.toString("yyyy-MM-dd")
        if not occ:                                   # 从「安排任务」拖进来
            services.todo_update(todo_id, due_date=new_date,
                                 due_time=time if time is not None else "")
            sounds.play("calendar_moved")
            self._card_changed()
            return
        if time is not None:
            if occ == new_date and time == (todo.get("due_time") or ""):
                return
            if todo.get("repeat"):
                scope = RepeatDialog.ask(
                    self, "修改重复任务",
                    "你正在修改重复任务的时间，请确认修改范围。")
                if scope is None:
                    return
                if scope == "all":
                    services.series_shift(todo_id, occ, new_date)
                    if time:
                        services.todo_update(todo_id, due_time=time)
                else:
                    services.occ_move(todo_id, occ, new_date, time)
            else:
                services.occ_move(todo_id, occ, new_date, time)
            sounds.play("calendar_moved")
            self._card_changed()
            return
        if todo.get("repeat"):
            scope = RepeatDialog.ask(
                self, "修改重复任务",
                "你正在修改重复任务的时间，请确认修改范围。")
            if scope is None:
                return
            if scope == "all":
                services.series_shift(todo_id, occ, new_date)
            else:
                services.occ_move(todo_id, occ, new_date)
        else:
            services.occ_move(todo_id, occ, new_date)
        sounds.play("calendar_moved")
        self._card_changed()

    def _on_resized(self, row: dict, mins: int) -> None:
        """拖时间轴色块下边缘改时长。

        这是全应用唯一能设 duration_min 的入口 —— 卡片上没有时长字段，以前这个
        字段只有默认值，用户无论如何在界面上改不动。
        时长是任务级的（重复任务的各周期共用一个），所以不用像改期那样问范围。
        """
        tid = int(row["id"])
        todo = services.todo_get(tid)
        if not todo or int(todo.get("duration_min") or 0) == mins:
            self.refresh()          # 没变也要把拖动中的临时高度复位
            return
        services.todo_update(tid, duration_min=mins)
        self._card_changed()

    # ------------------------------------------------------------ 右键快捷菜单
    def _row_menu(self, row: dict, pos: QPoint) -> None:
        """右键某条色条 → 滴答那张快捷菜单。选完再落库，见 row_menu 模块说明。"""
        self._menu_pos = pos          # 「挑日期」要在菜单刚收起的位置接着开选择器
        menu = RowMenu(row, QDate.currentDate(), self)
        touched: list[str] = []
        menu.applied.connect(
            lambda verb, payload: self._menu_apply(row, verb, payload, touched))
        menu.exec(pos)
        if menu.chosen is not None:
            self._menu_do(row, *menu.chosen)
        elif touched:
            self._card_changed()      # 只打了标签：菜单开着时不刷，收起后补一次
        menu.deleteLater()            # parent 是页面，不显式收掉会一次攒一个

    def _menu_apply(self, row: dict, verb: str, payload, touched: list) -> None:
        """「立刻落库但菜单不关」的那类动作（连着打标签）。"""
        if verb == "tag":
            self._menu_tag(int(row["id"]), int(payload))
            touched.append(verb)

    def _menu_pick_date(self, row: dict) -> None:
        pop = DatePickerPopup(row.get("date") or "",
                              (services.todo_get(int(row["id"])) or
                               {}).get("due_time") or "", parent=self)
        self._menu_picker = pop       # 局部变量的话会被 Python 提前回收
        pop.accepted.connect(
            lambda d_s, t_s, *_rest: self._menu_date_picked(row, d_s, t_s))
        scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        pos = getattr(self, "_menu_pos", QPoint())
        pop.adjustSize()
        pop.move(min(max(pos.x(), scr.left() + 4), scr.right() - pop.width() - 4),
                 min(max(pos.y() + 4, scr.top() + 4),
                     scr.bottom() - pop.height() - 4))
        pop.show()

    def _menu_date_picked(self, row: dict, date_s: str, time_s: str,
                         *_rest) -> None:
        d = QDate.fromString(date_s, "yyyy-MM-dd")
        if d.isValid():
            self._on_dropped(int(row["id"]), row.get("occ") or "", d,
                             time_s or "")

    def _menu_do(self, row: dict, verb: str, payload) -> None:
        tid, occ = int(row["id"]), row.get("occ") or ""
        if verb == "date":
            self._on_dropped(tid, occ, payload)   # 重复任务在里面问范围并刷新
            return
        if verb == "pick_date":
            self._menu_pick_date(row)
            return
        if verb == "prio":
            services.todo_update(tid, priority=int(payload))
        elif verb == "list":
            services.todo_update(tid, list_name=str(payload))
        elif verb == "tag":
            self._menu_tag(tid, int(payload))
        elif verb == "tag_new":
            self._menu_tag_new(tid)
        elif verb == "done":
            sid = int(row.get("sub_id") or 0)
            if sid:
                # 摊出来的一道题：勾它等于在待办页勾那个子任务，所以问结论这一步
                # 不能省 —— 不问就是不答，答不答决定这题往下走一档还是退一档。
                # 没答的话 _review_ask_sub 自己会把 done 退回 0。
                services.subtask_update(sid, done=1 if payload else 0)
                if payload:
                    _review_ask_sub(self, {"id": sid})
                # 待办页靠这个版本号决定切回去要不要重刷：不问结论的那两种改勾
                # （取消勾、勾一条用户自己加的检查事项）services 那边不会 bump。
                services.bump_review_rev()
            else:
                services.occ_set_done(tid, occ, bool(payload))
        elif verb == "copy":
            self._menu_copy(row)
        elif verb == "convert":
            # 从库里读当前类型：菜单带回来的 row 是打开那一刻的快照，
            # 连着转两次就会把第二次也写成「笔记」。
            now = (services.todo_get(tid) or {}).get("kind") or "task"
            services.todo_update(tid, kind="task" if now == "note" else "note")
        elif verb == "date_clear":
            self._menu_clear(row)
        elif verb == "delete":
            gone = int(row["id"])
            self._menu_delete(row)
            # 删除是软删（进垃圾桶），todo_get 照样读得回来，卡片不会自己收；
            # 留着就是一张指向已删任务的空壳，这里显式关掉。
            if self._card is not None and int(self._card.row["id"]) == gone:
                self._close_card()
        elif verb == "focus":
            self.focusRequested.emit(row["title"], str(payload))
            return
        else:
            return
        # 用 _card_changed 而不是 refresh：refresh 默认会把编辑卡关掉，
        # 那样「卡片开着、右键另一条改个优先级」就会把用户正在写的备注冲掉。
        self._card_changed()

    def _menu_tag(self, tid: int, tag_id: int) -> None:
        mine = [t["id"] for t in services.todo_tags(tid)]
        services.todo_set_tags(
            tid, mine + [tag_id] if tag_id not in mine
            else [m for m in mine if m != tag_id])

    def _menu_tag_new(self, tid: int) -> None:
        name, ok = popups.ask_text(self, "新建标签", "标签名称：")
        name = (name or "").strip()
        if not ok or not name:
            return
        existing = {t["name"]: t["id"] for t in services.tag_all()}
        tag_id = existing.get(name) or services.tag_add(name)
        mine = [t["id"] for t in services.todo_tags(tid)]
        if tag_id not in mine:
            services.todo_set_tags(tid, mine + [tag_id])

    def _menu_copy(self, row: dict) -> None:
        t = services.todo_get(int(row["id"])) or {}
        if not t:
            return
        new_id = services.todo_add(
            f"{t['title']} 副本", note=t.get("note") or "",
            priority=int(t.get("priority") or 0),
            due_date=row.get("date") or t.get("due_date") or "",
            due_time=t.get("due_time") or "",
            list_name=t.get("list_name") or "收集箱",
            kind=t.get("kind") or "task",
            duration_min=int(t.get("duration_min") or 0))
        services.todo_set_tags(new_id, [x["id"] for x in
                                        services.todo_tags(int(row["id"]))])

    def _menu_clear(self, row: dict) -> None:
        """取消排期。重复任务分「只跳过这一周期」和「整个系列不再重复」。"""
        tid, occ = int(row["id"]), row.get("occ") or ""
        if not (services.todo_get(tid) or {}).get("repeat"):
            services.todo_update(tid, due_date="", due_time="")
            return
        scope = RepeatDialog.ask(
            self, "从日历上移除", "这一条是重复任务，只移除这个周期，"
            "还是整个系列都不再排期？")
        if scope is None:
            return
        if scope == "all":
            services.todo_update(tid, repeat="", due_date="", due_time="")
        else:
            services.occ_delete(tid, occ)

    def _menu_delete(self, row: dict) -> None:
        tid, occ = int(row["id"]), row.get("occ") or ""
        if not (services.todo_get(tid) or {}).get("repeat"):
            if not popups.confirm(self, "删除任务",
                                  f"「{row['title']}」会进垃圾桶。", "删除"):
                return
            services.todo_delete(tid)
            return
        scope = RepeatDialog.ask(
            self, "删除重复任务", "这一条是重复任务，只删这个周期，"
            "还是整个系列一起删？")
        if scope is None:
            return
        if scope == "all":
            services.todo_delete(tid)
        else:
            services.occ_delete(tid, occ)

    def _quick_add(self, date: QDate, pos: QPoint, time_s: str = "") -> None:
        """点空白格子（或时间轴空档）→ 滴答式「新建任务」卡。

        同时只留一张：再点别处先把上一张关掉，关掉时它自己会把已写的标题落库。
        """
        if self._quick is not None:
            self._quick.close()
        pop = NewTaskCard(date, time_s, self)
        pop.created.connect(self.refresh)
        pop.closed.connect(lambda: setattr(self, "_quick", None))
        self._quick = pop
        anchor = pos if not pos.isNull() else \
            self.mapToGlobal(QPoint(self.width() // 2, 120))
        pop.show_at(anchor)

    def _quick_add_slot(self, date: QDate, time: str, pos: QPoint) -> None:
        self._quick_add(date, pos, time)

    # ------------------------------------------------------------ 生命周期
    def reload(self) -> None:
        self._f.load()
        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 复习排期是在刷题页改的：那边改了下次复习日，切回日历得看到它挪了日子
        seen = getattr(self, "_seen_review_rev", None)
        rev = services.review_rev()
        self._seen_review_rev = rev
        if seen is not None and seen != rev:
            self.refresh()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._close_card()
