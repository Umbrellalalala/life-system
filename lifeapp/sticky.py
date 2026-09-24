"""桌面便签：把一条待办「贴」在桌面上。

一个便签 = 一个独立顶层窗口，内容直接读写 `todos`，**不另存一份**：
副本迟早和列表漂开，滴答的便签本身也是任务的另一种视图。
开没开过用 `todos.sticky` 记，重启时 `restore_all()` 把窗口放回来。
"""
from __future__ import annotations

import json
from datetime import date

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QTextEdit, QVBoxLayout)

from . import db, services
from .todo_icons import PrioCheckBox, TickIcon

# 滴答便签的默认色板（第一格黄是它的默认值）
STICKY_COLORS = [
    ("黄", "#fff3ae"), ("粉", "#ffd9c2"), ("红", "#ffc9cf"), ("天蓝", "#c9e9ff"),
    ("蓝", "#cddcff"), ("紫", "#ded1ff"), ("绿", "#c9f0cd"), ("灰", "#eeeeee"),
    ("黑", "#2c2c34"), ("深蓝", "#20304f"),
]
FONT_SIZES = {"正常": 13, "大": 16}
GAPS = {"无": 0, "正常": 16, "大": 28, "超大": 44}
_DEFAULTS = {"sticky_color": "#fff3ae", "sticky_opacity": "100",
             "sticky_font": "正常", "sticky_topmost": "1", "sticky_gap": "正常"}


def prefs() -> dict:
    """设置页那几个值。缺省按滴答：黄底、不透明、正常字号、置顶、间距 16。"""
    for k, v in _DEFAULTS.items():
        if db.get_setting(k) is None:
            db.set_setting(k, v)
    return {"color": db.get_setting("sticky_color") or "#fff3ae",
            "opacity": max(30, min(100, int(db.get_setting("sticky_opacity") or 100))),
            "font": db.get_setting("sticky_font") or "正常",
            "topmost": db.get_setting("sticky_topmost") != "0",
            # 存的是档位名（无 / 正常 / 大 / 超大），不是像素数
            "gap": GAPS.get(db.get_setting("sticky_gap") or "正常", 16)}


def _is_dark(hex_value: str) -> bool:
    c = hex_value.lstrip("#")
    if len(c) != 6:
        return False
    r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    return 0.299 * r + 0.587 * g + 0.114 * b < 140


_OPEN: dict[int, "StickyNote"] = {}


def open_note(todo_id: int) -> "StickyNote":
    """打开（或聚焦）这条任务的便签。同一任务只会有一个窗口。"""
    note = _OPEN.get(todo_id)
    if note is None:
        note = StickyNote(todo_id)
        _OPEN[todo_id] = note
        note._apply_saved_geom()
        services.sticky_opened(todo_id, 1)
    note.show()
    note.raise_()
    note.activateWindow()
    return note


_LISTENERS: list = []


def on_change(fn) -> None:
    """待办页注册一个回调：便签里改了字，列表要跟着刷新（不然两边看着不一样）。"""
    _LISTENERS.append(fn)


def refresh(todo_id: int) -> None:
    """列表 / 详情面板改了这条，便签跟着改（反过来也一样，所以只在没编辑时刷）。"""
    note = _OPEN.get(todo_id)
    if note is not None:
        note.pull()


def reopen_for_prefs() -> None:
    """设置改完刷一遍开着的便签。

    置顶那条改的是 windowFlags —— Qt 只在窗口重建时才认，所以状态不一致的
    便签直接关掉重开（位置在关的时候存进了 sticky_geom，重开会摆回原处）。
    """
    want_top = prefs()["topmost"]
    for tid, note in list(_OPEN.items()):
        if bool(note.windowFlags() & Qt.WindowStaysOnTopHint) != want_top:
            note.close()
            open_note(tid)
            continue
        note.refresh_style()


def close_all() -> None:
    for note in list(_OPEN.values()):
        note.close()


def restore_all() -> None:
    """启动后把上次开着的便签放回来。"""
    try:
        ids = [r["id"] for r in _rows("SELECT id FROM todos WHERE sticky = 1 "
                                      "AND deleted_at = ''")]
    except Exception:                                    # noqa: BLE001
        return
    for tid in ids:
        note = open_note(tid)
        note._apply_saved_geom()


def _rows(sql: str) -> list:
    with db.connect() as conn:
        return conn.execute(sql).fetchall()


def _geom_map() -> dict:
    try:
        return json.loads(db.get_setting("sticky_geom") or "{}")
    except ValueError:
        return {}


def _save_geom(todo_id: int, x: int, y: int, w: int, h: int) -> None:
    m = _geom_map()
    m[str(todo_id)] = [x, y, w, h]
    db.set_setting("sticky_geom", json.dumps(m))


class StickyNote(QFrame):
    W, H = 268, 236

    def __init__(self, todo_id: int):
        p = prefs()
        flags = Qt.Window | Qt.FramelessWindowHint
        if p["topmost"]:
            flags |= Qt.WindowStaysOnTopHint
        super().__init__(None, flags)
        self._id = todo_id
        self._drag: tuple[int, int] | None = None
        self._refreshing = False
        self.setObjectName("StickyNote")
        # 圆角外面要透明，必须靠内层卡片画底色：顶层 translucent 窗口上给根
        # 控件写 QSS background 是不绘制的（和弹层同一个坑）
        self.setFixedWidth(self.W)
        card = QFrame(self)
        card.setObjectName("StickyCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 6, 10, 10)
        lay.setSpacing(4)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(6)
        self.grip = QLabel("", card)
        self.grip.setObjectName("StickyDate")
        bar.addWidget(self.grip)
        bar.addStretch(1)
        # 用 ×，不用图钉：图钉在 Windows 上看着像「置顶」，而这颗是「收起便签」，
        # 任务本身还在列表里
        self.pin = TickIcon("close", 13, "muted", card)
        self.pin.setToolTip("关闭便签（任务还在列表里）")
        self.pin.setCursor(Qt.PointingHandCursor)
        self.pin.mousePressEvent = lambda _e: self.close()
        bar.addWidget(self.pin)
        lay.addLayout(bar)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        self.check = PrioCheckBox(False, 18)
        self.check.toggled.connect(self._on_check)
        row.addWidget(self.check, 0, Qt.AlignTop | Qt.AlignLeft)
        self.title = QTextEdit(card)
        self.title.setObjectName("StickyTitle")
        self.title.setLineWrapMode(QTextEdit.WidgetWidth)
        self.title.setFixedHeight(52)          # 标题最多两行，多了归描述
        self.title.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        row.addWidget(self.title, 1)
        lay.addLayout(row)

        self.note = QTextEdit(card)
        self.note.setObjectName("StickyNoteBody")
        lay.addWidget(self.note, 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._push)
        self.title.textChanged.connect(self._save_timer.start)
        self.note.textChanged.connect(self._save_timer.start)

        self._apply_prefs()
        self.pull()
        self.resize(self.W, self.H)
        self.destroyed.connect(lambda _o, i=todo_id: _OPEN.pop(i, None))

    # ---- 外观 ----
    def _apply_prefs(self) -> None:
        p = prefs()
        self.setWindowOpacity(p["opacity"] / 100.0)
        dark = _is_dark(p["color"])
        self._hex = p["color"]
        head = "color: %s;" % ("#f2f2f2" if dark else "#202124")
        f = QFont("Microsoft YaHei UI", FONT_SIZES.get(p["font"], 13))
        f.setBold(True)
        self.title.setFont(f)
        self.title.setStyleSheet(
            "QTextEdit#StickyTitle { background: transparent; border: none; %s }" % head)
        self.note.setStyleSheet(
            "QTextEdit#StickyNoteBody { background: transparent; border: none; "
            "color: %s; font-size: %dpx; }"
            % ("#dcdce4" if dark else "#5f6368", max(11, FONT_SIZES.get(p["font"], 13) - 2)))
        self.grip.setStyleSheet("QLabel#StickyDate { color: %s; font-size: 11px; }"
                                % ("#b8b8c4" if dark else "#8b8fa3"))
        self.pin.set_color_hex("#b8b8c4" if dark else "#8b8fa3")
        card = self.findChild(QFrame, "StickyCard")
        card.setStyleSheet("QFrame#StickyCard { background: %s; border: 1px solid "
                           "rgba(0,0,0,0.10); border-radius: 12px; }" % p["color"])

    def refresh_style(self) -> None:
        self._apply_prefs()

    # ---- 数据 ----
    def pull(self) -> None:
        t = services.todo_get(self._id)
        if not t:
            self.close()
            return
        for w in (self.title, self.note):
            w.blockSignals(True)
        self.title.setPlainText(t["title"])
        self.note.setPlainText(t.get("note") or "")
        for w in (self.title, self.note):
            w.blockSignals(False)
        self.check.blockSignals(True)
        self.check.set_checked(self._is_done(t))
        self.check.set_priority(int(t.get("priority") or 0))
        self.check.blockSignals(False)
        due = t.get("due_date") or ""
        self.grip.setText("%s · %s" % (due[5:].replace("-", "/") if due else "无日期",
                                       t.get("list_name") or "收集箱"))

    def _push(self) -> None:
        title = self.title.toPlainText().strip()
        if not title:
            return                      # 空标题不写库，否则列表里会冒出一条空白
        services.todo_update(self._id, title=title,
                             note=self.note.toPlainText().rstrip())
        services.sticky_opened(self._id, 1)
        for fn in list(_LISTENERS):
            fn(self._id)

    def _today(self) -> str:
        return date.today().isoformat()

    def _is_done(self, t: dict) -> bool:
        """重复任务的勾记在「哪一周期」上，`todos.done` 那一位从来不动。
        所以直接读 t["done"] 会画成永远没勾上 —— 用户看着就是「勾了弹回来」。"""
        if not (t.get("repeat") or "").strip():
            return bool(t["done"])
        return any(r["done"] for r in services.cal_occurrences(self._today(),
                                                              self._today())
                   if r["id"] == self._id)

    def _on_check(self, checked: bool) -> None:
        # 周期日不能传空串：occ_set_done 会写出一条 occ_date='' 的例外，
        # 而日历/统计只按真实日期查例外，那个勾就成了谁也看不见的死数据。
        services.occ_set_done(self._id, self._today(), checked)
        self.pull()

    # ---- 无边框窗口要自己拖 ----
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag = (event.globalPosition().toPoint().x() - self.x(),
                          event.globalPosition().toPoint().y() - self.y())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            g = event.globalPosition().toPoint()
            self.move(g.x() - self._drag[0], g.y() - self._drag[1])
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag = None
        super().mouseReleaseEvent(event)

    def _apply_saved_geom(self) -> None:
        g = _geom_map().get(str(self._id))
        if g:
            self.setGeometry(*g)
        else:
            self._cascade()

    def _cascade(self) -> None:
        """第一次打开：从屏幕右上角按间距往下排，别叠成一坨。"""
        screen = (QApplication.primaryScreen().availableGeometry())
        gap = prefs()["gap"]
        n = len(_OPEN)
        self.move(screen.right() - self.W - 40 - (n % 4) * (self.W + gap),
                  screen.top() + 40 + (n % 6) * (self.H // 3))

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        if getattr(self, "_refreshing", False):
            return
        self._save_timer.stop()
        self._push()
        services.sticky_opened(self._id, 0)
        _OPEN.pop(self._id, None)
        _save_geom(self._id, self.x(), self.y(), self.width(), self.height())
