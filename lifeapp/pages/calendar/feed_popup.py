"""「日历订阅」管理弹层：列出订阅、开关可见、手动同步、删除、添加。

挂在 ⋯ 菜单的「日历订阅」上。数据与抓取逻辑都在 feeds.py，这里只管交互。
继承 popups.PopupCard，所以自带「点外面收起 / Esc 收起」，而且输入框能用输入法
（Qt.Popup 的窗口不会被激活，见 [[qt-popup-black-border-windows]]）。
"""
from __future__ import annotations

from PySide6.QtCore import (
    Qt, QObject, QRunnable, QThreadPool, Signal,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, QPushButton)

from ... import popups, theme
from . import feeds, style

WIDTH = 320


class Dot(QFrame):
    """订阅颜色点：可见时实心、隐藏时只留一圈描边。点它就是切可见性。"""

    clicked_ = Signal()

    def __init__(self, color: str, on: bool, parent=None):
        super().__init__(parent)
        self._color = color
        self._on = on
        self.setFixedSize(18, 18)
        self.setCursor(Qt.PointingHandCursor)

    def set_state(self, color: str, on: bool) -> None:
        self._color, self._on = color, on
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self._color)
        if self._on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
        else:
            p.setPen(QPen(c, 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(3, 3, 12, 12)
        p.end()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 同 PickerRow：release 上才动作，免得自己打开的对话框被这一下的
        # release 关掉
        if (event.button() == Qt.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked_.emit()


class _SyncDone(QObject):
    """后台同步的回执载体。跨线程只能靠信号回 GUI 线程，不能直接碰控件。"""

    done = Signal(int, bool, str)       # fid, 成功?, 给人看的话


class _SyncJob(QRunnable):
    """一次订阅同步。取 ICS 最长要等 12 秒，放在 GUI 线程里就是整个应用假死。"""

    def __init__(self, sig: _SyncDone, fid: int):
        super().__init__()
        self._sig, self.fid = sig, fid

    def run(self) -> None:
        ok, msg = feeds.sync(self.fid)      # sync 自己兜异常，不会往外抛
        self._sig.done.emit(self.fid, ok, msg)


class FeedPopup(popups.PopupCard):
    """订阅列表 + 内联的「添加订阅」表单。"""

    changed = Signal()          # 订阅集合或可见性变了 → 页面要重画日历

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, width=WIDTH)
        self.card.setObjectName("FeedCard")
        self._caption("日历订阅")
        self.rows = QVBoxLayout()
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(2)
        self.lay.addLayout(self.rows)
        # 回执对象故意不 parent 到本弹层：跑着的线程还持有它的引用，跟着弹层
        # 一起析构会让工作线程往死对象上发信号。
        self._sig = _SyncDone()
        self._sig.done.connect(self._synced)
        self._busy: set[int] = set()
        self._build_adder()
        self.rebuild()

    # ------------------------------------------------------------ 添加表单
    def _build_adder(self) -> None:
        self.add_btn = QPushButton("  添加订阅")
        self.add_btn.setObjectName("FeedAdd")
        self.add_btn.setIcon(style.icon("plus", 15))
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.clicked.connect(lambda _checked=False: self._open_adder(True))
        self.lay.addWidget(self.add_btn)

        self.form = QFrame()
        fl = QVBoxLayout(self.form)
        fl.setContentsMargins(0, 6, 0, 0)
        fl.setSpacing(6)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("订阅名称，如「团队日历」")
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("粘贴 .ics 链接或本地文件路径")
        for w in (self.name_edit, self.url_edit):
            w.setObjectName("FeedInput")
            _soft_field(w)
            fl.addWidget(w)
        row = QHBoxLayout()
        row.setSpacing(8)
        ok = QPushButton("添加并同步")
        ok.setObjectName("Primary")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(lambda _checked=False: self._add())
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda _checked=False: self._open_adder(False))
        row.addWidget(ok, 1)
        row.addWidget(cancel, 1)
        fl.addLayout(row)
        self.form.hide()
        self.lay.addWidget(self.form)

    def _open_adder(self, on: bool) -> None:
        self.form.setVisible(on)
        self.add_btn.setVisible(not on)
        if on:
            self.name_edit.setFocus()

    def _add(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            self.url_edit.setFocus()
            return
        fid = feeds.add_feed(self.name_edit.text(), url)
        self.name_edit.clear()
        self.url_edit.clear()
        self._open_adder(False)
        self.changed.emit()
        self._sync(fid)         # 第一次取内容也在后台，别按着界面等网络

    # ------------------------------------------------------------ 列表
    def rebuild(self) -> None:
        while self.rows.count():
            it = self.rows.takeAt(0)
            if it.widget():
                # 先 hide 再 deleteLater：待析构的行还挂在父控件上，不 hide 的话
                # 它照样能被 findChildren 找到、照样吃鼠标事件，看着就像有重复行
                it.widget().hide()
                it.widget().deleteLater()
        items = feeds.all_feeds()
        if not items:
            hint = QLabel("还没有订阅。支持公开日历的 .ics 链接，或本地 .ics 文件。")
            hint.setObjectName("FeedHint")
            self.rows.addWidget(hint)
        for f in items:
            self.rows.addWidget(self._row_for(f))
        self.adjustSize()

    def _row_for(self, f: dict) -> QFrame:
        row = QFrame()
        row.setObjectName("FeedRow")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(2, 4, 2, 4)
        lay.setSpacing(8)

        dot = Dot(f["color"], bool(f["visible"]))
        dot.clicked_.connect(lambda fid=f["id"], on=not bool(f["visible"]):
                             self._toggle(fid, on))
        lay.addWidget(dot)

        box = QVBoxLayout()
        box.setSpacing(1)
        name = QLabel(f["name"])
        name.setObjectName("FeedName")
        busy = f["id"] in self._busy
        sub = QLabel("正在同步…" if busy else (
            f["error"] or (f"上次同步 {f['last_sync']}"
                           if f["last_sync"] else "还没同步过")))
        sub.setObjectName("FeedSub")
        sub.setProperty("bad", "true" if f["error"] and not busy else "false")
        box.addWidget(name)
        box.addWidget(sub)
        lay.addLayout(box, 1)

        sync = QPushButton()
        sync.setObjectName("FeedBtn")
        sync.setIcon(style.icon("repeat", 14))
        sync.setToolTip("立即同步")
        sync.setCursor(Qt.PointingHandCursor)
        sync.setEnabled(not busy)
        sync.clicked.connect(lambda _checked=False, fid=f["id"]: self._sync(fid))
        lay.addWidget(sync)

        drop = QPushButton()
        drop.setObjectName("FeedBtn")
        drop.setIcon(style.icon("trash", 14))
        drop.setToolTip("删除订阅")
        drop.setCursor(Qt.PointingHandCursor)
        drop.clicked.connect(lambda _checked=False, fid=f["id"]: self._remove(fid))
        lay.addWidget(drop)
        return row

    def _toggle(self, fid: int, on: bool) -> None:
        feeds.set_visible(fid, on)
        self.changed.emit()
        self.rebuild()

    def _sync(self, fid: int) -> None:
        """把同步丢给线程池：取 ICS 最长要等 12 秒，在 GUI 线程里就是全应用假死。"""
        if fid in self._busy:
            return                      # 同一个订阅只让一个任务在飞，连点不叠
        self._busy.add(fid)
        self.rebuild()                  # 先让这一行显示「正在同步」
        QThreadPool.globalInstance().start(_SyncJob(self._sig, fid))

    def _synced(self, fid: int, ok: bool, msg: str) -> None:
        """回执由 Qt 排回 GUI 线程；弹层要是已经关了，这个槽根本不会被调。"""
        self._busy.discard(fid)
        self.changed.emit()
        self.rebuild()
        if not ok:
            popups.notify(self, "同步失败", msg, True)

    def _remove(self, fid: int) -> None:
        if not popups.confirm(self, "删除订阅",
                              "删除后订阅事件会从日历里移除，确定吗？", "删除"):
            return
        feeds.remove_feed(fid)
        self.changed.emit()
        self.rebuild()


def _soft_field(w: QWidget) -> None:
    from PySide6.QtGui import QPalette
    pal = w.palette()
    pal.setColor(QPalette.PlaceholderText,
                 QColor("#6a6a80" if theme.is_dark() else "#b4b7c3"))
    w.setPalette(pal)
