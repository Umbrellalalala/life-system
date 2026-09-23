"""右侧面板：迷你月历 + 可见性（所有 / 清单 / 标签 / 过滤器）+ 安排任务抽屉。

对应滴答清单日历右上角「⋯ → 安排任务」和那个侧边栏开关。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QDate, QPoint, Signal, QMimeData
from PySide6.QtGui import QDrag, QIcon
from PySide6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QGridLayout, QVBoxLayout, QLabel,
    QPushButton, QScrollArea,
)

from ... import services, theme, widgets
from . import holidays, model, style

PRIO_GROUP = {0: "无优先级", 1: "低优先级", 2: "中优先级", 3: "高优先级"}


def _icon(kind: str, color: str = "muted", size: int = 15) -> QLabel:
    """线性图标；``color`` 可以是主题色键，也可以是清单自定义的十六进制色。"""
    lbl = QLabel()
    lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
    style.reapply(lbl, lambda: lbl.setPixmap(style.grab(kind, color, size)))
    return lbl


class MiniCalendar(QFrame):
    """面板顶部的小月历：点某天跳转，节日画个小点。"""

    jumped = Signal(QDate)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CalMini")
        self._month = QDate.currentDate()
        self._selected = QDate.currentDate()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(4)

        head = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("CalMiniTitle")
        head.addWidget(self.title)
        head.addStretch(1)
        for text, slot in (("‹", lambda: self._shift(-1)),
                           ("今天", self._today),
                           ("›", lambda: self._shift(1))):
            b = QPushButton(text)
            b.setObjectName("CalMiniNav")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(slot)
            head.addWidget(b)
        lay.addLayout(head)

        grid_host = QWidget()
        self.grid = QGridLayout(grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(1)
        for i, wd in enumerate("日一二三四五六"):
            lbl = QLabel(wd)
            lbl.setObjectName("CalMiniWd")
            lbl.setAlignment(Qt.AlignCenter)
            self.grid.addWidget(lbl, 0, i)
        self._btns: list[QPushButton] = []
        for i in range(42):
            b = QPushButton()
            b.setObjectName("CalMiniDay")
            b.setFixedSize(34, 30)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, btn=b: self._pick(btn))
            self.grid.addWidget(b, 1 + i // 7, i % 7)
            self._btns.append(b)
        lay.addWidget(grid_host)
        self._refresh()

    def _shift(self, n: int) -> None:
        first = QDate(self._month.year(), self._month.month(), 1)
        self._month = first.addMonths(n)
        self._refresh()

    def _today(self) -> None:
        self._selected = QDate.currentDate()
        self._month = QDate(self._selected.year(), self._selected.month(), 1)
        self._refresh()
        self.jumped.emit(self._selected)

    def _pick(self, btn: QPushButton) -> None:
        d = getattr(btn, "_date", None)
        if d and d.isValid():
            self._selected = d
            self._month = QDate(d.year(), d.month(), 1)
            self._refresh()
            self.jumped.emit(d)

    def set_anchor(self, d: QDate) -> None:
        """跟随主视图翻月。"""
        self._selected = d
        self._month = QDate(d.year(), d.month(), 1)
        self._refresh()

    def _refresh(self) -> None:
        self.title.setText(f"{self._month.month()}月 {self._month.year()}年")
        start = model.sunday_week_start(self._month)
        today = QDate.currentDate()
        for i, b in enumerate(self._btns):
            d = start.addDays(i)
            b._date = d
            b.setText(str(d.day()))
            state = ("sel" if d == self._selected else
                     "today" if d == today else
                     "off" if d.month() != self._month.month() else "")
            widgets._apply_property(b, "miniState", state)
            fest = holidays.festival(d)
            b.setToolTip(f"{d.toString('M月d日')} {fest}" if fest else "")
            widgets._apply_property(b, "fest", "true" if fest else "false")


class _Row(QFrame):
    """面板里的一行：图标 + 名称 + 右侧勾选/单选控件。"""

    clicked_ = Signal()

    def __init__(self, kind: str, label: str, shape: str = "check",
                 color: str = "muted", parent=None):
        super().__init__(parent)
        self.setObjectName("CalSideRow")
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(6)
        self.mark = _icon(kind, color)
        lay.addWidget(self.mark)
        self.text = QLabel(label)
        self.text.setObjectName("CalSideRowText")
        lay.addWidget(self.text, 1)
        self.box = QLabel()
        self.box.setObjectName("CalSideBox")
        self.box.setFixedSize(16, 16)
        lay.addWidget(self.box)
        self._shape = shape
        self._on = False
        self._color = color
        self.sync()

    def set_on(self, on: bool) -> None:
        self._on = on
        self.sync()

    def sync(self) -> None:
        col = theme.get(self._color) if self._color in ("red", "amber", "blue",
                                                        "muted", "green", "accent") \
            else self._color
        ring = col if self._on else theme.get("border_strong")
        radius = 8 if self._shape == "radio" else 4
        fill = col if self._on else "transparent"
        self.box.setStyleSheet(
            f"#CalSideBox {{ background: {fill};"
            f" border: 1.5px solid {ring}; border-radius: {radius}px; }}")

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.clicked_.emit()
        super().mouseReleaseEvent(event)


class SidePanel(QScrollArea):
    """迷你月历 + 可见性控制。"""

    filter_changed = Signal()
    jumped = Signal(QDate)

    def __init__(self, filt: model.CalFilter, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CalSide")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFixedWidth(296)
        self._f = filt

        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(10, 10, 6, 12)
        lay.setSpacing(8)
        self.setWidget(host)

        self.mini = MiniCalendar()
        self.mini.jumped.connect(self.jumped.emit)
        lay.addWidget(self.mini)

        self.all_row = _Row("list", "所有", shape="radio", color="accent")
        self.all_row.clicked_.connect(self._pick_all)
        lay.addWidget(self.all_row)

        self._lists_host = self._section(lay, "清单", "list")
        self._tags_host = self._section(lay, "标签", "tag")
        self._filters_host = self._section(lay, "过滤器", "filter")
        self._rows_lists: list[tuple[_Row, str]] = []
        self._rows_tags: list[tuple[_Row, str]] = []
        self._rows_quad: list[tuple[_Row, str]] = []
        lay.addStretch(1)
        self.rebuild()

    def _section(self, lay, title: str, icon: str) -> QVBoxLayout:
        head = QHBoxLayout()
        head.setSpacing(5)
        head.addWidget(_icon(icon, "muted", 14))
        lbl = QLabel(title)
        lbl.setObjectName("CalSideHead")
        head.addWidget(lbl)
        head.addStretch(1)
        lay.addLayout(head)
        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(1)
        lay.addLayout(box)
        return box

    # ------------------------------------------------------------ 构建
    def rebuild(self) -> None:
        for host, rows in ((self._lists_host, self._rows_lists),
                           (self._tags_host, self._rows_tags),
                           (self._filters_host, self._rows_quad)):
            while host.count():
                it = host.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
            rows.clear()

        for l in services.list_all():
            if l.get("kind", "list") != "list":
                continue
            row = _Row("list", l["name"], color=l.get("color") or "muted")
            row.set_on(l["name"] in self._f.lists)
            row.clicked_.connect(lambda n=l["name"]: self._toggle_list(n))
            self._lists_host.addWidget(row)
            self._rows_lists.append((row, l["name"]))

        for t in services.tag_all():
            row = _Row("tag", t["name"], color=t.get("color") or "muted")
            row.set_on(t["name"] in self._f.tags)
            row.clicked_.connect(lambda n=t["name"]: self._toggle_tag(n))
            self._tags_host.addWidget(row)
            self._rows_tags.append((row, t["name"]))

        for value, label, color in model.QUADRANTS:
            row = _Row("quad", label, shape="radio", color=color)
            row.set_on(self._f.quadrant == value)
            row.clicked_.connect(lambda v=value: self._toggle_quad(v))
            self._filters_host.addWidget(row)
            self._rows_quad.append((row, value))
        self._sync_all_row()

    def _sync_all_row(self) -> None:
        self.all_row.set_on(not self._f.narrowing and not self._f.quadrant)

    def _commit(self) -> None:
        self._f.save()
        self._sync_all_row()
        self.filter_changed.emit()

    def _pick_all(self) -> None:
        self._f.reset()
        for row, _ in self._rows_lists + self._rows_tags + self._rows_quad:
            row.set_on(False)
        self._commit()

    def _toggle_list(self, name: str) -> None:
        if name in self._f.lists:
            self._f.lists.discard(name)
        else:
            self._f.lists.add(name)
        self._f.mode = "custom" if (self._f.lists or self._f.tags) else "all"
        for row, n in self._rows_lists:
            row.set_on(n in self._f.lists)
        self._commit()

    def _toggle_tag(self, name: str) -> None:
        if name in self._f.tags:
            self._f.tags.discard(name)
        else:
            self._f.tags.add(name)
        self._f.mode = "custom" if (self._f.lists or self._f.tags) else "all"
        for row, n in self._rows_tags:
            row.set_on(n in self._f.tags)
        self._commit()

    def _toggle_quad(self, value: str) -> None:
        self._f.quadrant = "" if self._f.quadrant == value else value
        for row, v in self._rows_quad:
            row.set_on(v == self._f.quadrant)
        self._commit()

    def set_anchor(self, d: QDate) -> None:
        self.mini.set_anchor(d)


class _Chip(QFrame):
    """安排任务抽屉里的一条未排期任务，可拖到日历上排期。"""

    def __init__(self, row: dict, parent=None):
        super().__init__(parent)
        self.row = row
        self.setObjectName("CalChip")
        self.setCursor(Qt.PointingHandCursor)
        self._press: QPoint | None = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 8, 3)
        lay.setSpacing(5)
        prio = int(row.get("priority") or 0)
        if prio:
            lay.addWidget(_icon("flag", model.PRIO_COLOR[prio], 13))
        lbl = QLabel(row["title"])
        lbl.setObjectName("CalChipText")
        lay.addWidget(lbl, 1)
        bg, fg = model.bar_color(prio)
        self.setStyleSheet(
            f"#CalChip {{ background: {bg}; border: none; border-radius: 5px; }}"
            f"#CalChipText {{ color: {fg}; font-size: 11.5px; background: transparent; }}")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        self._press = event.position().toPoint()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press is not None and \
                (event.position().toPoint() - self._press).manhattanLength() > 8:
            mime = QMimeData()
            mime.setData(model.MIME_CAL, f"{self.row['id']}|".encode())
            mime.setText(self.row["title"])
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec(Qt.CopyAction)
            self._press = None


class _Group(QFrame):
    """抽屉里的一个分组：可折叠的表头（箭头 + 图标 + 名称 + 数量）+ 条目区。"""

    def __init__(self, icon: str, color: str, name: str, count: int,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CalDrawerGroupRow")
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 0, 2)
        lay.setSpacing(5)
        self._open = True
        # 箭头方向是状态，不能像别的图标那样写死：换肤重画时要按当前开合来画。
        self.chev = QLabel()
        self.chev.setAttribute(Qt.WA_TransparentForMouseEvents)
        style.reapply(self.chev, self._sync_chev)
        lay.addWidget(self.chev)
        lay.addWidget(_icon(icon, color, 14))
        self.lbl = QLabel(name)
        self.lbl.setObjectName("CalDrawerGroup")
        lay.addWidget(self.lbl)
        self.cnt = QLabel(str(count))
        self.cnt.setObjectName("CalDrawerCount")
        lay.addWidget(self.cnt)
        lay.addStretch(1)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(3)

    def _sync_chev(self) -> None:
        self.chev.setPixmap(style.grab(
            "chevron_down" if self._open else "chevron_right", "muted", 12))

    def add(self, w: QWidget) -> None:
        self.body.addWidget(w)

    def toggle(self) -> None:
        self._open = not self._open
        for i in range(self.body.count()):
            it = self.body.itemAt(i)
            if it.widget():
                it.widget().setVisible(self._open)
        self._sync_chev()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.toggle()
        super().mouseReleaseEvent(event)


class ScheduleDrawer(QFrame):
    """「安排任务」抽屉：按清单 / 标签 / 优先级分组列出未排期任务。

    与滴答一致：和右侧面板互斥（由页面负责切换），顶部「所有 ›」是根节点，
    点它回到不分组的一整列。
    """

    changed = Signal()

    def __init__(self, filt: model.CalFilter, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CalDrawer")
        self.setFixedWidth(300)
        self._f = filt
        self._mode = 0
        self._flat = False
        self._skip_lists: set[str] = set()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 8, 10)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("安排任务")
        title.setObjectName("CalDrawerTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.filter_btn = QPushButton()
        self.filter_btn.setObjectName("CardIconBtn")
        style.themed_icon(self.filter_btn, "filter", "muted", 15)
        self.filter_btn.setToolTip("筛选清单")
        self.filter_btn.setCursor(Qt.PointingHandCursor)
        self.filter_btn.clicked.connect(self._filter_menu)
        head.addWidget(self.filter_btn)
        close = QPushButton("✕")
        close.setObjectName("CardIconBtn")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(lambda: self.setVisible(False))
        head.addWidget(close)
        lay.addLayout(head)

        track = QWidget()
        track.setObjectName("PopupTabTrack")
        tl = QHBoxLayout(track)
        tl.setContentsMargins(3, 3, 3, 3)
        tl.setSpacing(3)
        self._tabs: list[QPushButton] = []
        for i, name in enumerate(("清单", "标签", "优先级")):
            b = QPushButton(name)
            b.setObjectName("PopupTab")
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, ix=i: self._switch(ix))
            tl.addWidget(b, 1)
            self._tabs.append(b)
        lay.addWidget(track)

        crumb = QHBoxLayout()
        crumb.setSpacing(4)
        self.crumb_btn = QPushButton()
        self.crumb_btn.setObjectName("CalCrumb")
        self.crumb_btn.setCursor(Qt.PointingHandCursor)
        self.crumb_btn.clicked.connect(self._go_root)
        crumb.addWidget(self.crumb_btn)
        crumb.addStretch(1)
        lay.addLayout(crumb)
        style.reapply(self.crumb_btn, self._sync_crumb)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.host = QWidget()
        self.body = QVBoxLayout(self.host)
        self.body.setContentsMargins(0, 0, 6, 0)
        self.body.setSpacing(6)
        self.body.addStretch(1)
        self.scroll.setWidget(self.host)
        lay.addWidget(self.scroll, 1)

    def _sync_crumb(self) -> None:
        label = "所有" if self._flat else (
            "清单" if self._mode == 0 else "标签" if self._mode == 1 else "优先级")
        self.crumb_btn.setText(f"  {label} ›" if not self._flat else "  所有")
        self.crumb_btn.setIcon(QIcon(style.grab(
            "list" if self._flat else "folder_open", "accent", 14)))

    def _go_root(self) -> None:
        """「所有 ›」= 回到不分组的一整列。"""
        self._flat = not self._flat
        self._sync_crumb()
        self.rebuild()

    def _filter_menu(self) -> None:
        menu = style.menu(self)
        names = [l["name"] for l in services.list_all()
                 if l.get("kind", "list") == "list"] or ["收集箱"]
        for name in names:
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(name not in self._skip_lists)
            act.setData(name)
        chosen = menu.exec(self.filter_btn.mapToGlobal(
            QPoint(0, self.filter_btn.height() + 4)))
        if chosen is None:
            return
        name = str(chosen.data())
        if name in self._skip_lists:
            self._skip_lists.discard(name)
        else:
            self._skip_lists.add(name)
        self.rebuild()

    def _switch(self, i: int) -> None:
        self._mode = i
        self._flat = False
        for k, b in enumerate(self._tabs):
            b.setChecked(k == i)
        self._sync_crumb()
        self.rebuild()

    def _rows(self) -> list[dict]:
        return [r for r in model.unscheduled(self._f)
                if (r.get("list_name") or "收集箱") not in self._skip_lists]

    def rebuild(self) -> None:
        while self.body.count():
            it = self.body.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        rows = self._rows()
        if self._flat:
            g = _Group("list", "muted", "所有", len(rows))
            for r in rows:
                g.add(_Chip(r))
            self.body.addWidget(g)
            self.body.addLayout(g.body)
            self.body.addStretch(1)
            return
        groups: dict[str, list[dict]] = {}
        meta: dict[str, tuple[str, str]] = {}
        for r in rows:
            if self._mode == 0:
                key = r.get("list_name") or "收集箱"
                meta.setdefault(key, ("list", "muted"))
            elif self._mode == 1:
                for t in (r.get("tags") or [{}]):
                    key = t.get("name") or "无标签"
                    meta.setdefault(key, ("tag", t.get("color") or "muted"))
                    groups.setdefault(key, []).append(r)
                continue
            else:
                p = int(r.get("priority") or 0)
                key = PRIO_GROUP[p]
                meta.setdefault(key, ("flag", model.PRIO_COLOR[p]))
            groups.setdefault(key, []).append(r)
        # 清单分组用清单自己的图标和颜色
        if self._mode == 0:
            by_name = {l["name"]: l for l in services.list_all()}
            for key in groups:
                l = by_name.get(key)
                if l:
                    meta[key] = (l.get("icon") or "list", l.get("color") or "muted")
        for name, items in groups.items():
            icon, color = meta.get(name, ("list", "muted"))
            g = _Group(icon, color, name, len(items))
            for r in items:
                g.add(_Chip(r))
            self.body.addWidget(g)
            self.body.addLayout(g.body)
        self.body.addStretch(1)
