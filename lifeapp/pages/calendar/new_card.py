"""点日历空白格弹出来的「新建任务」卡，对齐滴答清单。

    [日历] 下周三, 9月30日                       [旗]
    ──────────────────────────────────────────────────
    准备做什么?                                  [子任务]
    描述
    …
    [→] 收集箱

和 task_card.TaskCard 的分工：那张是**已存在任务**的就地编辑器（有勾选框、
提醒/重复/更多那一排，改一笔就落库）；这张还没有 todo id，所以标题非空时
才真正建一条任务 —— 回车或点卡片外面都建，按 Esc 是放弃。

窗口标志那套约束同 page.QuickAdd：必须 Qt.Tool（Qt.Popup 的窗口不会被激活，
输入法跟随不上），而且 raise/activateWindow/给焦点要延后一轮做。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QDate, QPoint, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, QTextEdit,
    QPushButton, QApplication)

from ... import dateparse, popups, services, theme, todo_icons, widgets
from ..todo import DatePickerPopup
from . import style

CARD_WIDTH = 410          # 实测：卡片本体 615 物理 px ÷ 1.5
CARD_MIN_H = 297          # 实测 446 物理 px ÷ 1.5
PAD = 22                  # 左右内边距，实测 35 物理 px
WEEK_CN = "日一二三四五六"


def _week_anchor(d: QDate) -> QDate:
    """周日为一周之首（和日历页的列顺序一致）。"""
    return d.addDays(-(d.dayOfWeek() % 7))


def _soft_placeholder(w: QWidget) -> None:
    """占位文字取中性浅灰。QSS 没有 ``::placeholder-text``，只能改调色板。"""
    pal = w.palette()
    pal.setColor(QPalette.ColorRole.PlaceholderText,
                 QColor("#6a6a80" if theme.is_dark() else "#b4b7c3"))
    w.setPalette(pal)


def rel_label(d: QDate) -> tuple[str, str]:
    """日期胶囊的（文案, 颜色键）。

    滴答是按周来说话的：本周内「周五, 9月25日」，隔一周「下周三, 9月30日」，
    再远才退化成「N天前/后」。过期整段转红，其余用滴答蓝 —— 三张参考图都是
    这个规律（上周三是红的，本周五和下周三都是蓝的）。
    """
    today = QDate.currentDate()
    left = today.daysTo(d)
    tail = f"{d.month()}月{d.day()}日"
    wd = _week_anchor(d).daysTo(_week_anchor(today)) // 7
    name = WEEK_CN[d.dayOfWeek() % 7]
    if left == 0:
        text = f"今天, {tail}"
    elif left == 1:
        text = f"明天, {tail}"
    elif left == -1:
        text = f"昨天, {tail}"
    elif wd == 0:
        text = f"周{name}, {tail}"
    elif wd == 1:
        text = f"上周{name}, {tail}"
    elif wd == -1:
        text = f"下周{name}, {tail}"
    elif left > 0:
        text = f"{left}天后, {tail}"
    else:
        text = f"{-left}天前, {tail}"
    return text, ("red" if left < 0 else "tick")


class PickerRow(QFrame):
    """清单选择器里的一行：图标 + 名字 + （选中打勾 / 文件夹展开箭头）。"""

    clicked_ = Signal(str)
    expand_toggled = Signal(int)
    new_requested = Signal()

    def __init__(self, name: str, icon: str, current: str, indent: int = 0,
                 folder_id: int = 0, expanded: bool = False, new_list: bool = False,
                 parent=None):
        super().__init__(parent)
        self._name = name
        self._folder_id = folder_id
        self._new_list = new_list
        self.setObjectName("PickerRow")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(34)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(indent + 6, 0, 6, 0)
        lay.setSpacing(8)

        icon_w = todo_icons.TickIcon(icon, 16, "muted")
        pic = QLabel()
        pic.setFixedSize(18, 18)
        icon_w.setParent(pic)
        icon_w.move(1, 1)
        icon_w.show()
        lay.addWidget(pic)

        txt = QLabel(name)
        txt.setObjectName("PickerText")
        widgets._apply_property(txt, "on", "true" if name == current else "false")
        lay.addWidget(txt)
        lay.addStretch(1)

        if name == current:
            tick = QLabel("✓")
            tick.setObjectName("PickerTick")
            lay.addWidget(tick)
        elif folder_id:
            caret = QLabel("⌄" if expanded else "›")
            caret.setObjectName("PickerHint")
            lay.addWidget(caret)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 在 release 上动作（和 QPushButton 一致）。挂在 press 上的话，
        # 「新建清单」这种会再弹出对话框的行，会被同一次点击的 release
        # 当场把刚弹出的对话框关掉。
        if event.button() != Qt.LeftButton                 or not self.rect().contains(event.position().toPoint()):
            return
        if self._new_list:
            self.new_requested.emit()
        elif self._folder_id:
            self.expand_toggled.emit(self._folder_id)
        else:
            self.clicked_.emit(self._name)


class ListPicker(popups.PopupCard):
    """点卡片底部「收集箱」弹出来的那块：搜索 + 清单 + 可展开的文件夹。

    继承 PopupCard 是为了共用「Qt.Tool + 点外面收起 + Esc 收起」那一套
    （也顺带让搜索框能用输入法）。
    """

    accepted = Signal(str)

    def __init__(self, current: str, parent: QWidget | None = None):
        super().__init__(parent, width=214)
        self._current = current or "收集箱"
        self._expanded: set[int] = set()
        self.card.setObjectName("ListPicker")

        head = QHBoxLayout()
        head.setSpacing(6)
        holder = QLabel()
        holder.setFixedSize(16, 16)
        todo_icons.TickIcon("search", 15, "muted", holder).show()
        head.addWidget(holder)
        self.search = QLineEdit()
        self.search.setObjectName("PickerSearch")
        self.search.setPlaceholderText("搜索")
        self.search.setFrame(False)
        _soft_placeholder(self.search)
        self.search.textChanged.connect(self._refill)
        head.addWidget(self.search, 1)
        self.lay.addLayout(head)

        self.rows = QVBoxLayout()
        self.rows.setContentsMargins(0, 4, 0, 0)
        self.rows.setSpacing(0)
        self.lay.addLayout(self.rows)
        self._refill()

    def _entries(self, needle: str) -> list[tuple]:
        """(名字, 图标, 缩进, 文件夹id) —— 收集箱在最上，文件夹可展开子清单。"""
        needle = needle.strip().lower()
        out: list[tuple] = []
        if not needle or "收集箱" in needle:
            out.append(("收集箱", "inbox", 0, 0))
        alls = services.list_all()
        for lst in alls:
            if lst.get("kind") == "folder":
                if needle and needle not in lst["name"].lower():
                    continue
                out.append((lst["name"], "folder", 0, lst["id"]))
                if lst["id"] not in self._expanded:
                    continue
                for sub in alls:
                    if (sub.get("kind") == "folder"
                            or sub.get("folder_id") != lst["id"]
                            or (needle and needle not in sub["name"].lower())):
                        continue
                    out.append((sub["name"], sub.get("icon") or "list", 20, 0))
            elif not lst.get("folder_id"):
                if needle and needle not in lst["name"].lower():
                    continue
                out.append((lst["name"], lst.get("icon") or "list", 0, 0))
        return out

    def _refill(self) -> None:
        while self.rows.count():
            it = self.rows.takeAt(0)
            if it.widget():
                it.widget().hide()      # 待析构的行还挂在父控件上，不 hide 会继续吃鼠标事件
                it.widget().deleteLater()
        for name, icon, indent, fid in self._entries(self.search.text()):
            row = PickerRow(name, icon, self._current, indent, fid,
                            fid in self._expanded)
            row.clicked_.connect(self._pick)
            row.expand_toggled.connect(self._toggle_folder)
            self.rows.addWidget(row)
        if not self.search.text().strip():
            # 搜索时收起「新建清单」，免得它混进结果里
            add = PickerRow("新建清单", "plus", self._current, new_list=True)
            add.new_requested.connect(self._new_list)
            self.rows.addWidget(add)
        self.adjustSize()

    def _new_list(self) -> None:
        # 不能在行点击的处理函数里直接开阻塞式对话框：那一次点击还没走完，
        # 刚弹出来的命名框会被同一次交互关掉（表现就是「点新建清单没反应」）。
        QTimer.singleShot(0, self._ask_new_list)

    def _ask_new_list(self) -> None:
        name, ok = popups.ask_text(self, "新建清单", "清单名称：")
        name = (name or "").strip()
        if not ok or not name:
            return
        services.list_add(name)
        self._pick(name)

    def _toggle_folder(self, fid: int) -> None:
        if fid in self._expanded:
            self._expanded.discard(fid)
        else:
            self._expanded.add(fid)
        self._refill()

    def _pick(self, name: str) -> None:
        self._current = name
        self.accepted.emit(name)
        self._reject()


class NewTaskCard(QFrame):
    """点空白格子建任务的那张卡。"""

    created = Signal()
    closed = Signal()

    def __init__(self, date: QDate, time_s: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("NewTaskCard")
        self.setWindowFlags(Qt.WindowType.Tool
                            | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setFixedWidth(CARD_WIDTH)
        self.setMinimumHeight(CARD_MIN_H)
        style.apply_to(self)

        self._date = date
        self._time = time_s
        self._no_date = False       # 点了「清除」= 建成未排期
        self._priority = 0
        self._list = "收集箱"
        self._subs: list[str] = []
        self._done = False          # 已经落库 / 已放弃，closeEvent 不再补建
        self._armed = False
        self._child = None

        # 内边距放在各段自己的布局上，不放根布局 —— 滴答头部那条分隔线是
        # 通栏的（一直顶到卡片左右两边），根布局留了边距就做不到。
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._build_head(root)
        root.addWidget(style.sep_line())
        self._build_body(root)      # 中间那片空白由描述的拉伸项占住
        self._build_foot(root)
        self._sync_date()

    # ------------------------------------------------------------ 构建
    def _build_head(self, root: QVBoxLayout) -> None:
        head = QHBoxLayout()
        head.setContentsMargins(PAD, 10, PAD - 8, 10)     # 实测头部 52 高
        head.setSpacing(6)
        self.date_btn = QPushButton()
        self.date_btn.setObjectName("CardDateChip")
        self.date_btn.setCursor(Qt.PointingHandCursor)
        self.date_btn.clicked.connect(self._pick_date)
        head.addWidget(self.date_btn)
        head.addStretch(1)
        self.flag = todo_icons.FlagButton(size=30)
        self.flag.clicked_.connect(self._pick_priority)
        head.addWidget(self.flag)
        root.addLayout(head)

    def _build_body(self, root: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(PAD, 10, PAD - 8, 0)
        row.setSpacing(6)
        self.title = QLineEdit()
        self.title.setObjectName("NewTitle")
        self.title.setPlaceholderText("准备做什么?")
        self.title.setFrame(False)
        _soft_placeholder(self.title)
        self.title.textChanged.connect(self._on_typed)
        self.title.returnPressed.connect(self._submit)
        row.addWidget(self.title, 1)
        self.sub_btn = style.icon_btn("subtask_list", "添加子任务",
                                      self._toggle_subs)
        row.addWidget(self.sub_btn)
        root.addLayout(row)

        self.desc = QTextEdit()
        self.desc.setObjectName("NewDesc")
        self.desc.setPlaceholderText("描述")
        self.desc.setFrameShape(QFrame.Shape.NoFrame)
        _soft_placeholder(self.desc)
        self.desc.setMinimumHeight(26)   # 别让它自己的 sizeHint 把卡片顶高
        desc_row = QHBoxLayout()
        desc_row.setContentsMargins(PAD, 2, PAD - 8, 0)
        desc_row.addWidget(self.desc)
        # 描述吃掉中间所有空间（滴答就是「描述 + 一大片空白 + 收集箱」），
        # 所以这里不能再放 addStretch，否则卡片按 sizeHint 长到 330 高。
        root.addLayout(desc_row, 1)

        self.sub_wrap = QFrame()
        sl = QVBoxLayout(self.sub_wrap)
        sl.setContentsMargins(PAD, 4, PAD - 8, 0)
        sl.setSpacing(2)
        self.sub_shown = QLabel("")
        self.sub_shown.setObjectName("PickerHint")
        self.sub_add = QLineEdit()
        self.sub_add.setObjectName("CardSubAdd")
        self.sub_add.setPlaceholderText("回车添加子任务")
        _soft_placeholder(self.sub_add)
        self.sub_add.returnPressed.connect(self._add_sub)
        sl.addWidget(self.sub_shown)
        sl.addWidget(self.sub_add)
        self.sub_wrap.hide()
        root.addWidget(self.sub_wrap)

    def _build_foot(self, root: QVBoxLayout) -> None:
        foot = QHBoxLayout()
        foot.setContentsMargins(PAD, 0, PAD - 8, 12)
        foot.setSpacing(2)
        self.list_btn = QPushButton()
        self.list_btn.setObjectName("NewListBtn")
        self.list_btn.setCursor(Qt.PointingHandCursor)
        self.list_btn.clicked.connect(self._pick_list)
        foot.addWidget(self.list_btn)
        foot.addStretch(1)
        root.addLayout(foot)

    # ------------------------------------------------------------ 显示同步
    def _sync_date(self) -> None:
        text, color = rel_label(self._date)
        if self._time:
            text += f" {dateparse.human_time(self._time)}"
        self.date_btn.setText(" " + text)
        self.date_btn.setIcon(style.icon(
            "calendar", 15,
            hex_color=theme.get("red") if color == "red" else style.tick_blue()))
        widgets._apply_property(self.date_btn, "chipColor", color)
        self._sync_list()

    def _sync_list(self) -> None:
        self.list_btn.setText(f"  {self._list}")
        self.list_btn.setIcon(style.icon(
            "inbox" if self._list == "收集箱" else "list", 15))

    def _sync_subs(self) -> None:
        self.sub_shown.setText("、".join(self._subs))
        self.sub_shown.setVisible(bool(self._subs))

    # ------------------------------------------------------------ 交互
    def _submit(self) -> None:
        """回车：建任务并收起；标题还空着就什么都不做（滴答也是空的不建）。"""
        if self.commit():
            self.close()

    def _on_typed(self, _text: str) -> None:
        """标题里写出「明天三点」这类话，日期胶囊跟着走（滴答就是这个行为）。"""
        p = dateparse.parse_datetime(self.title.text())
        if not p.found:
            return
        d = QDate.fromString(p.date, "yyyy-MM-dd") if p.date else QDate()
        if d.isValid():
            self._date = d
        if p.time:
            self._time = p.time
        self._sync_date()

    def _toggle_subs(self) -> None:
        self.sub_wrap.setVisible(not self.sub_wrap.isVisible())
        if self.sub_wrap.isVisible():
            self.sub_add.setFocus()

    def _add_sub(self) -> None:
        text = self.sub_add.text().strip()
        if not text:
            return
        self._subs.append(text)
        self.sub_add.clear()
        self._sync_subs()

    def _pick_priority(self) -> None:
        from .task_card import PriorityMenu
        menu = PriorityMenu(self._priority, self)
        act = menu.exec(self._below(self.flag))
        if act is None:
            return
        self._priority = int(act.data())
        self.flag.set_priority(self._priority)

    def _pick_list(self) -> None:
        pop = ListPicker(self._list, self)
        pop.accepted.connect(self._apply_list)
        pop.finished.connect(lambda: setattr(self, "_child", None))
        self._child = pop
        popups.place_popup(pop, self.list_btn)
        pop.show()

    def _apply_list(self, name: str) -> None:
        self._list = name
        self._sync_list()

    def _pick_date(self) -> None:
        pop = DatePickerPopup(self._date.toString("yyyy-MM-dd"), self._time,
                              parent=self)
        pop.accepted.connect(self._apply_date)
        pop.cleared.connect(self._clear_date)
        # 不用清 _child：DatePickerPopup 是 Qt.Popup，点外面自己就 hide 了，
        # 过滤器那侧判的是 _child.isVisible()，一 hide 守卫自然失效
        self._child = pop
        anchor = self._below(self.date_btn)
        pop.move(anchor.x(), anchor.y() + 4)
        pop.show()

    def _apply_date(self, new_date: str, new_time: str) -> None:
        d = QDate.fromString(new_date, "yyyy-MM-dd")
        if d.isValid():
            self._date = d
        self._time = new_time or ""
        self._no_date = False
        self._sync_date()

    def _clear_date(self) -> None:
        self._time = ""
        self._no_date = True
        self.date_btn.setText(" 无日期")
        self.date_btn.setIcon(style.icon("calendar", 15, color_key="muted"))
        widgets._apply_property(self.date_btn, "chipColor", "muted")

    def _below(self, w: QWidget) -> QPoint:
        return self.mapToGlobal(QPoint(w.x(), w.y() + w.height() + 6))

    # ------------------------------------------------------------ 落库
    def commit(self) -> bool:
        """标题非空才建任务。已落库或已放弃过就什么都不做。"""
        if self._done:
            return True
        raw = self.title.text().strip()
        if not raw:
            self._done = True
            return True
        p = dateparse.parse_datetime(raw)
        tid = services.todo_add(
            p.cleaned or raw, priority=p.priority or self._priority,
            due_date=p.date or ("" if self._no_date
                                 else self._date.toString("yyyy-MM-dd")),
            due_time=p.time or self._time,
            list_name=p.list_name or self._list,
            note=self.desc.toPlainText().strip())
        for text in self._subs:
            services.subtask_add(tid, text)
        self._done = True
        self.created.emit()
        return True

    @property
    def dirty(self) -> bool:
        return bool(self.title.text().strip())

    # ------------------------------------------------------------ 窗口行为
    def show_at(self, global_pos: QPoint) -> None:
        """摆在点击处并夹回屏幕内。激活和给焦点交给 _watch_clicks。

        高度直接取实测值，不用 adjustSize()：QTextEdit 的 sizeHint 自带十行高，
        adjustSize 会把卡片顶到 330 左右，比滴答那张明显胖一圈。
        """
        self.resize(CARD_WIDTH, CARD_MIN_H)
        scr = (QApplication.screenAt(global_pos)
               or QApplication.primaryScreen()).availableGeometry()
        self.move(min(max(global_pos.x(), scr.left() + 6),
                      max(scr.left() + 6, scr.right() - self.width() - 6)),
                  min(max(global_pos.y(), scr.top() + 6),
                      max(scr.top() + 6, scr.bottom() - self.height() - 6)))
        self.show()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._armed:
            self._armed = True
            QTimer.singleShot(0, self._watch_clicks)

    def _watch_clicks(self) -> None:
        """延后一轮：挂点外面过滤器 + 激活窗口 + 给焦点。原因见 page.QuickAdd 同段注释。"""
        if not (self._armed and self.isVisible()):
            return
        QApplication.instance().installEventFilter(self)
        self.raise_()
        self.activateWindow()
        self.title.setFocus(Qt.OtherFocusReason)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._armed:
            QApplication.instance().removeEventFilter(self)
            self._armed = False
        self.commit()          # 点外面关掉时也落库（滴答的做法）
        self.closed.emit()
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._done = True          # Esc 是放弃，不建
            self.close()
            return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is not self and event.type() in (
                event.Type.MouseButtonPress, event.Type.MouseButtonDblClick):
            if self._child is not None and self._child.isVisible():
                return False             # 子弹层开着，那一下不算「点外面」
            if QApplication.activePopupWidget() is not None:
                return False
            if not self.geometry().contains(event.globalPosition().toPoint()):
                self.close()
        return super().eventFilter(obj, event)
