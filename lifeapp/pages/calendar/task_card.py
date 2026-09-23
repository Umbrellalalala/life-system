"""任务卡片弹层：点日历上的色条后弹出的那张就地编辑卡片。

对齐滴答清单的卡片结构：
    ☐ │  5天前, 9月14日 🔁                          ⚑
    ────────────────────────────────────────────────────
    每天2小时学推荐系统                                ≡
    描述…
    ☐ 子任务 / 回车添加下一项              （仅「详情」展开后）
    ────────────────────────────────────────────────────
    📥 收集箱                                    ⏰ 🔁 ⋯

用 Qt.Tool 而不是 Qt.Popup：卡片里要打开日期选择器、优先级菜单等子弹窗，
Qt.Popup 的鼠标抓取会在子弹窗打开时把卡片自己关掉。
「点外面就关闭」改由应用级事件过滤器实现。

与滴答的已知差异：描述区是纯文本（滴答是富文本，本项目的 note 字段全库
按纯文本存，接富文本会波及待办页与笔记页），所以不放那排格式化按钮。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QDate, QDateTime, QPoint, Signal
from PySide6.QtGui import QFont, QIcon, QKeyEvent
from PySide6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QVBoxLayout, QLineEdit, QTextEdit,
    QLabel, QPushButton, QMenu, QApplication, QListWidget, QListWidgetItem)

from ... import dateparse, popups, services, todo_icons, widgets
from ..todo import DatePickerPopup
from . import model, style
from .repeat_dialog import RepeatDialog

CARD_WIDTH = 470


def _menu_icon(kind: str, color_key: str = "muted", size: int = 15) -> QIcon:
    pm = todo_icons.TickIcon(kind, size, color_key).grab()
    return QIcon(pm)


def _chip_text(row: dict) -> tuple[str, str]:
    """日期胶囊的文案与颜色键：过期红、今天/明天蓝、其余正常。"""
    d = QDate.fromString(row["date"], "yyyy-MM-dd")
    if not d.isValid():
        return "无日期", "muted"
    left = QDate.currentDate().daysTo(d)
    label = f"{d.month()}月{d.day()}日"
    if left < 0:
        return f"{-left}天前, {label}", "red"
    if left == 0:
        return f"今天, {label}", "accent"
    if left == 1:
        return f"明天, {label}", "accent"
    text = f"{left}天后, {label}"
    if row.get("time"):
        text += f" {dateparse.human_time(row['time'])}"
    return text, "text" if not row.get("time") else "accent"


class PriorityMenu(QMenu):
    """滴答式优先级菜单：彩旗 + 当前档打勾。"""

    def __init__(self, current: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CalCardMenu")
        self.setStyleSheet(style.qss())
        for value, label in ((3, "高优先级"), (2, "中优先级"),
                             (1, "低优先级"), (0, "无优先级")):
            kind = "flag" if value else "flag_none"
            act = self.addAction(_menu_icon(kind, model.PRIO_COLOR[value]), label)
            act.setData(value)
            if value == int(current or 0):
                act.setCheckable(True)
                act.setChecked(True)


class TaskCard(QFrame):
    """一条任务（某个周期）的就地编辑卡片。"""

    changed = Signal()
    closed = Signal()

    def __init__(self, row: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("TaskCard")
        self.setWindowFlags(Qt.WindowType.Tool
                            | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setFixedWidth(CARD_WIDTH)
        style.apply_to(self)
        self.row = dict(row)
        self._detail = False
        self._filtered = False

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 10)
        root.setSpacing(6)
        self._build_header(root)
        self._build_body(root)
        self._build_detail(root)
        # 用日历自己的发丝线：widgets.hline() 是个没有 objectName 的裸 HLine，
        # 落到 Fusion 默认样式上会画成一条深灰凹槽，比滴答重得多
        root.addWidget(style.sep_line())
        self._build_footer(root)
        self._sync()

    # ------------------------------------------------------------ 构建
    def _build_header(self, root: QVBoxLayout) -> None:
        head = QHBoxLayout()
        head.setSpacing(8)
        self.check = todo_icons.PrioCheckBox(size=23)
        self.check.toggled.connect(self._on_check)
        head.addWidget(self.check)

        sep = QLabel("|")
        sep.setObjectName("CardSep")
        head.addWidget(sep)

        self.date_btn = QPushButton()
        self.date_btn.setObjectName("CardDateChip")
        self.date_btn.setCursor(Qt.PointingHandCursor)
        self.date_btn.clicked.connect(self._pick_date)
        head.addWidget(self.date_btn)

        self.repeat_btn = QPushButton()
        self.repeat_btn.setObjectName("CardIconBtn")
        self.repeat_btn.setIcon(_menu_icon("repeat", "muted", 15))
        self.repeat_btn.setToolTip("重复")
        self.repeat_btn.setCursor(Qt.PointingHandCursor)
        self.repeat_btn.clicked.connect(self._pick_repeat)
        head.addWidget(self.repeat_btn)
        head.addStretch(1)

        self.flag = todo_icons.FlagButton(size=32)
        self.flag.clicked_.connect(self._pick_priority)
        head.addWidget(self.flag)
        root.addLayout(head)

    def _build_body(self, root: QVBoxLayout) -> None:
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        self.title = QLineEdit()
        self.title.setObjectName("CardTitle")
        self.title.setPlaceholderText("添加标题")
        self.title.editingFinished.connect(self._save_title)
        title_row.addWidget(self.title, 1)

        self.detail_btn = QPushButton()
        self.detail_btn.setObjectName("CardIconBtn")
        self.detail_btn.setCheckable(True)
        self.detail_btn.setToolTip("详情")
        self.detail_btn.setCursor(Qt.PointingHandCursor)
        self.detail_btn.toggled.connect(self._toggle_detail)
        title_row.addWidget(self.detail_btn)
        root.addLayout(title_row)

        self.desc = QTextEdit()
        self.desc.setObjectName("CardDesc")
        self.desc.setPlaceholderText("描述")
        self.desc.setFixedHeight(66)
        root.addWidget(self.desc)

    def _build_detail(self, root: QVBoxLayout) -> None:
        self.detail_wrap = QFrame()
        self.detail_wrap.setObjectName("CardDetail")
        dl = QVBoxLayout(self.detail_wrap)
        dl.setContentsMargins(0, 4, 0, 0)
        dl.setSpacing(4)

        self.sub_list = QListWidget()
        self.sub_list.setObjectName("CardSubList")
        self.sub_list.itemChanged.connect(self._on_sub_toggled)
        dl.addWidget(self.sub_list)

        self.sub_add = QLineEdit()
        self.sub_add.setObjectName("CardSubAdd")
        self.sub_add.setPlaceholderText("回车添加下一项")
        self.sub_add.returnPressed.connect(self._add_subtask)
        dl.addWidget(self.sub_add)

        self.tag_row = QHBoxLayout()
        self.tag_row.setSpacing(4)
        self.tag_row.addStretch(1)
        dl.addLayout(self.tag_row)
        self.detail_wrap.hide()
        root.addWidget(self.detail_wrap)

    def _build_footer(self, root: QVBoxLayout) -> None:
        foot = QHBoxLayout()
        foot.setSpacing(2)

        self.list_btn = QPushButton()
        self.list_btn.setObjectName("CardListBtn")
        self.list_btn.setCursor(Qt.PointingHandCursor)
        self.list_btn.clicked.connect(self._pick_list)
        foot.addWidget(self.list_btn)
        foot.addStretch(1)

        for kind, tip, slot in (
            ("clock", "提醒", self._pick_reminder),
            ("repeat", "重复", self._pick_repeat),
            ("more", "更多", self._more_menu),
        ):
            b = QPushButton()
            b.setObjectName("CardIconBtn")
            b.setIcon(_menu_icon(kind, "muted", 15))
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(slot)
            foot.addWidget(b)
        root.addLayout(foot)

    # ------------------------------------------------------------ 显示同步
    def _sync(self) -> None:
        r = self.row
        full = services.todo_get(r["id"])
        if not full:
            self.close()
            return
        # 描述是关卡片时才落库的，所以「框里的文字 ≠ row 里那份」= 用户正在写、
        # 还没保存。光看 hasFocus 挡不住：点卡片自己的旗子/勾选、或者从右键菜单
        # 改这条，焦点早就不在输入框上了，那一下会把半截备注直接擦掉。
        note_dirty = self.desc.toPlainText() != (r.get("note") or "")
        r["title"] = full.get("title", "")
        r["note"] = full.get("note") or ""
        r["priority"] = int(full.get("priority") or 0)
        r["repeat"] = full.get("repeat") or ""
        r["list_name"] = full.get("list_name") or "收集箱"
        r["kind"] = full.get("kind") or "task"

        self.check.set_priority(r["priority"])
        self.check.set_checked(bool(r["done"]))
        self.check.setVisible(r["kind"] != "note")
        text, color = _chip_text(r)
        self.date_btn.setText(" " + text)
        self.date_btn.setIcon(_menu_icon("calendar", color, 14))
        widgets._apply_property(self.date_btn, "chipColor", color)
        self.repeat_btn.setVisible(bool(r["repeat"]))
        self.flag.set_priority(r["priority"])
        if not self.title.hasFocus():
            self.title.setText(r["title"])
            f = self.title.font()
            f.setStrikeOut(bool(r["done"]))
            self.title.setFont(f)
        if not note_dirty and not self.desc.hasFocus():
            self.desc.setPlainText(r["note"])
        self.detail_btn.setIcon(_menu_icon("menu", "muted", 15))
        self.list_btn.setText(f"  {r['list_name']}")
        self.list_btn.setIcon(_menu_icon("inbox", "muted", 15))
        self._rebuild_subs()
        self._rebuild_tags()
        self.adjustSize()

    def _rebuild_subs(self) -> None:
        subs = services.subtask_list(self.row["id"])
        self.sub_list.blockSignals(True)
        self.sub_list.clear()
        for s in subs:
            it = QListWidgetItem(s["title"])
            it.setData(Qt.ItemDataRole.UserRole, s["id"])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if s["done"]
                             else Qt.CheckState.Unchecked)
            f = QFont()
            f.setStrikeOut(bool(s["done"]))
            it.setFont(f)
            self.sub_list.addItem(it)
        self.sub_list.blockSignals(False)
        self.sub_list.setVisible(bool(subs))
        self.sub_list.setFixedHeight(min(132, 26 * max(len(subs), 1) + 10))

    def _rebuild_tags(self) -> None:
        """只显示已打上的标签，末尾挂一个「＋标签」入口（滴答的做法）。"""
        while self.tag_row.count():
            it = self.tag_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for tag in services.todo_tags(self.row["id"]):
            chip = QPushButton(tag["name"])
            chip.setObjectName("TagChip")
            chip.setCheckable(True)
            chip.setChecked(True)
            chip.setCursor(Qt.PointingHandCursor)
            widgets._apply_property(chip, "tagColor", tag["color"])
            chip.clicked.connect(lambda _, t=tag: self._toggle_tag(t["id"]))
            self.tag_row.addWidget(chip)
        add = QPushButton()
        add.setObjectName("CardIconBtn")
        add.setIcon(_menu_icon("tag", "muted", 15))
        add.setToolTip("标签")
        add.setCursor(Qt.PointingHandCursor)
        add.clicked.connect(self._tag_menu)
        self.tag_row.addWidget(add)
        self.tag_row.addStretch(1)

    def _tag_menu(self) -> None:
        menu = style.menu(self)
        current = {t["id"] for t in services.todo_tags(self.row["id"])}
        for tag in services.tag_all():
            act = menu.addAction(tag["name"])
            act.setData(tag["id"])
            act.setCheckable(True)
            act.setChecked(tag["id"] in current)
        chosen = menu.exec(self._below(self.sender()))
        if chosen is None:
            return
        self._toggle_tag(int(chosen.data()))
        self._sync()

    # ------------------------------------------------------------ 编辑动作
    def _save_title(self) -> None:
        text = self.title.text().strip()
        if text and text != self.row["title"]:
            services.todo_update(self.row["id"], title=text)
            self.row["title"] = text
            self.changed.emit()

    def save_note(self) -> None:
        """描述落库（关闭前由页面显式调用，避免焦点切换时丢字）。"""
        text = self.desc.toPlainText()
        if text != (self.row.get("note") or ""):
            services.todo_update(self.row["id"], note=text)
            self.row["note"] = text
            self.changed.emit()

    def _on_check(self, checked: bool) -> None:
        services.occ_set_done(self.row["id"], self.row["occ"], checked)
        self.row["done"] = checked
        self.changed.emit()
        self._sync()

    def _on_sub_toggled(self, item: QListWidgetItem) -> None:
        sub_id = item.data(Qt.ItemDataRole.UserRole)
        if sub_id:
            services.subtask_update(
                sub_id, done=int(item.checkState() == Qt.CheckState.Checked))
            self._rebuild_subs()
            self.changed.emit()

    def _add_subtask(self) -> None:
        text = self.sub_add.text().strip()
        if not text:
            return
        services.subtask_add(self.row["id"], text)
        self.sub_add.clear()
        self._rebuild_subs()
        self.changed.emit()

    def _toggle_tag(self, tag_id: int) -> None:
        current = [t["id"] for t in services.todo_tags(self.row["id"])]
        if tag_id in current:
            current.remove(tag_id)
        else:
            current.append(tag_id)
        services.todo_set_tags(self.row["id"], current)
        self._rebuild_tags()
        self.changed.emit()

    def _pick_priority(self) -> None:
        menu = PriorityMenu(self.row["priority"], self)
        act = menu.exec(self._below(self.flag))
        if act is None:
            return
        services.todo_update(self.row["id"], priority=int(act.data()))
        self.changed.emit()
        self._sync()

    def _pick_date(self) -> None:
        pop = DatePickerPopup(self.row["date"], self.row.get("time") or "",
                              parent=self)
        pop.accepted.connect(self._apply_date)
        pop.cleared.connect(self._clear_date)
        anchor = self._below(self.date_btn)
        pop.move(anchor.x(), anchor.y() + 4)
        pop.show()

    def _apply_date(self, new_date: str, new_time: str) -> None:
        same = new_date == self.row["date"] and new_time == (self.row.get("time") or "")
        if same:
            return
        if self.row["repeat"]:
            scope = RepeatDialog.ask(
                self, "修改重复任务",
                "你正在修改重复任务的时间，请确认修改范围。")
            if scope is None:
                self._sync()
                return
            if scope == "all":
                services.series_shift(self.row["id"], self.row["occ"], new_date)
            else:
                services.occ_move(self.row["id"], self.row["occ"], new_date,
                                  new_time)
        else:
            services.occ_move(self.row["id"], self.row["occ"], new_date, new_time)
        self.changed.emit()
        self.close()

    def _clear_date(self) -> None:
        services.todo_update(self.row["id"], due_date="", due_time="")
        self.changed.emit()
        self.close()

    def _pick_repeat(self) -> None:
        menu = style.menu(self)
        for value, label in services.REPEAT_OPTIONS:
            act = menu.addAction(label)
            act.setData(value)
            if value == (self.row.get("repeat") or ""):
                act.setCheckable(True)
                act.setChecked(True)
        act = menu.exec(self._below(self.repeat_btn))
        if act is None:
            return
        services.todo_set_repeat(self.row["id"], str(act.data()))
        self.changed.emit()
        self._sync()

    def _pick_list(self) -> None:
        menu = style.menu(self)
        names = ["收集箱"] + [l["name"] for l in services.list_all()
                              if l.get("kind", "list") == "list"]
        for name in dict.fromkeys(names):
            act = menu.addAction(name)
            act.setData(name)
            if name == self.row["list_name"]:
                act.setCheckable(True)
                act.setChecked(True)
        menu.addSeparator()
        add = menu.addAction("新建清单…")
        act = menu.exec(self._below(self.list_btn))
        if act is None:
            return
        if act is add:
            name, ok = popups.ask_text(self, "新建清单", "清单名称：")
            name = name.strip()
            if not ok or not name:
                return
            services.list_add(name)
            services.todo_update(self.row["id"], list_name=name)
        else:
            services.todo_update(self.row["id"], list_name=str(act.data()))
        self.changed.emit()
        self._sync()

    def _pick_reminder(self) -> None:
        menu = style.menu(self)
        for label, mins in (("准时", 0), ("提前 5 分钟", 5), ("提前 15 分钟", 15),
                            ("提前 30 分钟", 30), ("提前 1 小时", 60),
                            ("提前 1 天", 1440)):
            act = menu.addAction(label)
            act.setData(mins)
        menu.addSeparator()
        off = menu.addAction("取消提醒")
        act = menu.exec(self._below(self.list_btn))
        if act is None:
            return
        if act is off:
            services.todo_update(self.row["id"], reminder="")
        else:
            stamp = _reminder_at(self.row["date"], self.row.get("time") or "",
                                 int(act.data()))
            services.todo_update(self.row["id"], reminder=stamp)
        self.changed.emit()

    def _more_menu(self) -> None:
        menu = style.menu(self)
        today = menu.addAction("移到今天")
        dup = menu.addAction("创建副本")
        menu.addSeparator()
        delete = menu.addAction("删除此周期" if self.row["repeat"] else "删除任务")
        act = menu.exec(self._below(self.list_btn))
        if act is None:
            return
        if act is today:
            self._apply_date(QDate.currentDate().toString("yyyy-MM-dd"),
                             self.row.get("time") or "")
        elif act is dup:
            services.todo_add(self.row["title"], note=self.row.get("note") or "",
                              priority=self.row["priority"],
                              due_date=self.row["date"],
                              due_time=self.row.get("time") or "",
                              list_name=self.row["list_name"])
            self.changed.emit()
        elif act is delete:
            services.occ_delete(self.row["id"], self.row["occ"])
            self.changed.emit()
            self.close()

    def _toggle_detail(self, on: bool) -> None:
        self._detail = on
        self.detail_btn.setChecked(on)
        self.detail_wrap.setVisible(on)
        self.detail_btn.setToolTip("收起详情" if on else "详情")
        self.adjustSize()

    # ------------------------------------------------------------ 弹出与关闭
    def _below(self, anchor: QWidget) -> QPoint:
        return anchor.mapToGlobal(QPoint(0, anchor.height() + 4))

    def show_near(self, global_pos: QPoint) -> None:
        self.adjustSize()
        screen = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        x = min(max(global_pos.x(), avail.left() + 6),
                avail.right() - self.width() - 6)
        y = min(max(global_pos.y(), avail.top() + 6),
                avail.bottom() - self.height() - 6)
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        self.title.setFocus()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._filtered:
            QApplication.instance().installEventFilter(self)
            self._filtered = True

    def closeEvent(self, event) -> None:  # noqa: N802
        self.save_note()
        if self._filtered:
            QApplication.instance().removeEventFilter(self)
            self._filtered = False
        self.closed.emit()
        super().closeEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """应用级过滤器：点到卡片和子弹窗之外就收起卡片。"""
        if obj is not self and event.type() in (
                event.Type.MouseButtonPress, event.Type.MouseButtonDblClick):
            gp = event.globalPosition().toPoint()
            if not self.geometry().contains(gp) \
                    and QApplication.activePopupWidget() is None:
                self.close()
        return super().eventFilter(obj, event)


def _reminder_at(date_s: str, time_s: str, mins: int) -> str:
    """提醒时刻 = 截止时间减去提前量；无时间按 9:00 起算。"""
    dt = QDateTime.fromString(f"{date_s} {time_s or '09:00'}",
                              "yyyy-MM-dd HH:mm")
    if not dt.isValid():
        return ""
    return dt.addSecs(-mins * 60).toString("yyyy-MM-dd HH:mm")
