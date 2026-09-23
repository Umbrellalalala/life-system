"""右键日历上的任务色条弹出的快捷菜单，对齐滴答那张图。

菜单只负责「收集意图」：选完把 (动作, 参数) 记在 ``chosen`` 上并收起，
落库和刷新都在 page.py 的 `_menu_do` 里。两件事因此留在那边而不是这里：
重复任务要先问「仅此周期 / 所有周期」（和拖拽共用 `_on_dropped`），
以及删除、新建标签这类还要再弹一层对话框 —— 菜单自己的事件循环还开着时
套模态框最容易出问题。

``chosen`` 的取值：
    ("date", QDate) / ("date_clear", None) / ("prio", int) / ("list", str)
    ("tag_new", None) / ("done", bool) / ("focus", str)
    ("copy", None) / ("convert", None) / ("delete", None)
打标签走 ``applied("tag", tag_id)``：立刻落库、菜单不关，好让人连着勾好几个。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QDate, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QMenu,
    QWidgetAction,
)

from ... import services
from . import model, style


def _icon(kind: str, color: str = "muted", size: int = 15, glyph: str = ""):
    return QIcon(style.grab(kind, color, size, glyph))


class _IconRow(QWidget):
    """菜单里嵌的一行：小标题 + 一排图标按钮（滴答的「日期」「优先级」两行）。"""

    def __init__(self, caption: str, on_pick, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CalMenuRow")
        self._on_pick = on_pick
        col = QVBoxLayout(self)
        col.setContentsMargins(10, 6, 10, 6)
        col.setSpacing(3)
        cap = QLabel(caption)
        cap.setObjectName("CalMenuCap")
        col.addWidget(cap)
        self.line = QHBoxLayout()
        self.line.setContentsMargins(0, 0, 0, 0)
        self.line.setSpacing(6)
        col.addLayout(self.line)

    def add(self, verb: str, payload=None, kind: str = "today", tip: str = "",
            color: str = "muted", glyph: str = "", on: bool = False) -> None:
        b = QPushButton()
        b.setObjectName("CalMenuIcon")
        b.setProperty("on", "true" if on else "false")
        b.setFixedSize(31, 31)
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tip)
        b.setIcon(_icon(kind, color, 17, glyph))
        b.clicked.connect(lambda _checked=False, v=verb, p=payload:
                          self._on_pick(v, p))
        self.line.addWidget(b)


class RowMenu(QMenu):
    """一条任务（某个周期）的右键菜单。"""

    applied = Signal(str, object)    # 立刻生效、菜单不关（连着打标签）

    def __init__(self, row: dict, today: QDate,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CalCardMenu")
        self.setStyleSheet(style.qss())
        self.row = row
        self.today = today
        self.chosen: tuple[str, object] | None = None
        self._embed(self._date_row())
        self._embed(self._prio_row())
        self.addSeparator()
        self._list_menu()
        self._tag_menu()
        self.addSeparator()
        self._done_action()
        self._focus_menu()
        self.addSeparator()
        self._flat("copy", "创建副本", "copy")
        self._convert_action()
        self.addSeparator()
        self._flat("delete", "删除", "trash", danger=True)

    # ------------------------------------------------------------ 内部
    def _pick(self, verb: str, payload=None) -> None:
        self.chosen = (verb, payload)
        self.close()

    def _embed(self, w: QWidget) -> None:
        act = QWidgetAction(self)
        act.setDefaultWidget(w)
        self.addAction(act)

    def _sub(self, icon: str, text: str, parent: QMenu | None = None) -> QMenu:
        sub = (parent or self).addMenu(_icon(icon), text)
        sub.setObjectName("CalCardMenu")
        sub.setStyleSheet(style.qss())
        return sub

    def _list_item(self, parent: QMenu, name: str, icon: str = "list") -> None:
        a = parent.addAction(_icon(icon), name)
        if name == (self.row.get("list_name") or "收集箱"):
            a.setCheckable(True)
            a.setChecked(True)
        a.triggered.connect(lambda _checked=False, n=name: self._pick("list", n))

    def _flat(self, verb: str, text: str, icon: str, payload=None,
              danger: bool = False) -> None:
        # 危险项只把图标画成红的（滴答也是这样）：QSS 选不到 QAction 的动态属性，
        # 想整行红字得改自绘菜单，不值当。
        a = self.addAction(_icon(icon, "red" if danger else "muted"), text)
        a.triggered.connect(lambda _checked=False, v=verb, p=payload:
                            self._pick(v, p))

    # ------------------------------------------------------------ 两行快捷
    def _date_row(self) -> _IconRow:
        row = _IconRow("日期", self._pick, self)
        row.add("date", self.today, kind="today", tip="今天")
        row.add("date", self.today.addDays(1), kind="sunrise", tip="明天")
        row.add("date", self.today.addDays(7), kind="calendar", glyph="7",
                tip="一周后")
        # 滴答这里是一个日历格子图标点开日期选择器；带「7」的那个才是一周后
        row.add("pick_date", kind="week", tip="挑个日期")
        row.add("date_clear", kind="close", tip="从日历上移除（取消排期）")
        return row

    def _prio_row(self) -> _IconRow:
        cur = int(self.row.get("priority") or 0)
        row = _IconRow("优先级", self._pick, self)
        for value, tip in ((3, "高优先级"), (2, "中优先级"), (1, "低优先级")):
            row.add("prio", value, kind="flag", tip=tip,
                    color=model.PRIO_COLOR[value], on=cur == value)
        row.add("prio", 0, kind="flag_none", tip="无优先级", on=cur == 0)
        return row

    # ------------------------------------------------------------ 子菜单
    def _list_menu(self) -> None:
        """移动到：清单平铺，文件夹做成二级菜单。

        滴答的清单是可以有文件夹的；全平铺出来时，用户在十来个名字里
        找自己那个子清单很费劲，所以按文件夹归一下。
        """
        sub = self._sub("folder_open", "移动到")
        alls = services.list_all()
        lists = [l for l in alls if l.get("kind") != "folder"]
        # 收集箱是虚拟的（todo_lists 里没有这一行），不手动补就永远移不回去
        self._list_item(sub, "收集箱", "inbox")
        for l in lists:
            if not l.get("folder_id"):
                self._list_item(sub, l["name"], l.get("icon") or "list")
        for f in alls:
            if f.get("kind") != "folder":
                continue
            kids = [l for l in lists if l.get("folder_id") == f["id"]]
            if kids:
                grp = self._sub("folder_open", f["name"], parent=sub)
                for l in kids:
                    self._list_item(grp, l["name"], l.get("icon") or "list")

    def _tag_menu(self) -> None:
        sub = self._sub("tag", "标签")
        tags = services.tag_all()
        mine = {t["id"] for t in services.todo_tags(self.row["id"])}
        for t in tags:
            a = sub.addAction(t["name"])
            a.setCheckable(True)
            a.setChecked(t["id"] in mine)
            # 打标签经常一次要打好几个：这一项不关菜单，勾完接着勾，
            # 落库立刻做、界面等菜单收起后再刷（见 page._row_menu）
            a.triggered.connect(lambda _checked=False, tid=t["id"]:
                                self.applied.emit("tag", tid))
        if tags:
            sub.addSeparator()
        sub.addAction("新建标签…").triggered.connect(
            lambda: self._pick("tag_new"))

    def _focus_menu(self) -> None:
        # 滴答那格是个番茄计时器；「quad」（四象限方块）画出来是一坨灰方块，
        # 认不出来，用时钟更贴近「开始专注 = 计时」。
        sub = self._sub("clock", "开始专注")
        for mode, text in (("pomodoro", "番茄专注"), ("countup", "正计时")):
            sub.addAction(text).triggered.connect(
                lambda _checked=False, m=mode: self._pick("focus", m))

    # ------------------------------------------------------------ 单条动作
    def _done_action(self) -> None:
        done = bool(self.row.get("done"))
        self._flat("done", "取消完成" if done else "完成",
                   "check_circle" if done else "check", payload=not done)

    def _convert_action(self) -> None:
        is_note = (self.row.get("kind") or "task") == "note"
        self._flat("convert", "转换为任务" if is_note else "转换为笔记",
                   "list" if is_note else "note")
