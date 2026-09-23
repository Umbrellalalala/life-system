"""滴答清单风格的弹层：输入 / 多行 / 单选 / 确认 / 提示。

替代原生 QInputDialog、QMessageBox：那两套是系统外观，和 app 里其余自绘弹层
对不上，而且日夜主题切换时它们不跟着变。

两种用法：
- 信号式（不阻塞）：建好 Popup → `place_popup(pop, anchor)` → `pop.show()`，连
  `accepted` 信号。
- 阻塞式：`ask_text` / `ask_number` / `ask_note` / `get_item` / `confirm` / `notify`，
  返回值签名刻意对齐 QInputDialog / QMessageBox，方便逐处替换调用点。
"""
from __future__ import annotations

from PySide6.QtCore import (
    Qt, QEvent, QTimer, QEventLoop, QPoint, QRectF, Signal,
)
from PySide6.QtGui import QColor, QPainter, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout,
    QScrollArea, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QApplication,
)

from . import theme, widgets

SHADOW = 10          # 卡片四周给阴影留的空白
RADIUS = 12


# ---------------------------------------------------------------------------
# 底座
# ---------------------------------------------------------------------------
class PopupCard(QFrame):
    """无边框圆角弹层卡片，四周留白画阴影。

    顶层窗 + WA_TranslucentBackground + 无边框是配套的：一旦保留原生标题栏，
    Windows 会把圆角外的透明区合成成黑色，看起来就是一圈黑边。

    窗口类型用 Qt.Tool 而不是 Qt.Popup：**输入法只跟随被激活的窗口**，
    而 Popup 在 Windows 上是「显示但不激活」的，所以框里能敲英文却切不出中文。
    代价是 Popup 免费给的「点外面 / Esc 自动收起」没了，下面自己补。
    """

    finished = Signal()

    def __init__(self, parent: QWidget | None = None, width: int = 260):
        if parent is None:
            # 从模态对话框里点开的弹层必须挂在那个对话框下面：Windows 上 Qt 会把
            # 模态期间「对话框层级之外」的顶层窗在 Win32 层面 EnableWindow(FALSE)，
            # 父为空的 Qt.Tool 弹层于是整块变灰、点了没反应（实测 IsWindowEnabled
            # 为假；TickMenu 用 Qt.Popup 窗天然豁免）。
            inst = QApplication.instance()
            parent = inst.activeModalWidget() if inst is not None else None
        super().__init__(parent)
        self.setObjectName("AppPopupShell")
        self.setWindowFlags(Qt.WindowType.Tool
                            | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(width + SHADOW * 2)
        self._armed = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW, SHADOW, SHADOW, SHADOW)
        self.card = QFrame(self)
        self.card.setObjectName("AppPopup")
        outer.addWidget(self.card)

        self.lay = QVBoxLayout(self.card)
        self.lay.setContentsMargins(14, 12, 14, 12)
        self.lay.setSpacing(9)

        self._value = None
        self._done = False
        self._child = None   # 本弹层又打开的子弹层，见 eventFilter

    # ---- 子类用 ----
    def _accept(self, value) -> None:
        self._value, self._done = value, True
        self.finished.emit()
        self.close()

    def _reject(self) -> None:
        self._done = False
        self.finished.emit()
        self.close()

    def _footer(self, ok_text: str = "确定", on_ok=None) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)
        ok = QPushButton(ok_text)
        ok.setObjectName("Primary")
        ok.clicked.connect(on_ok or (lambda: None))
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self._reject)
        row.addWidget(ok, 1)
        row.addWidget(cancel, 1)
        self.lay.addLayout(row)

    def _caption(self, text: str) -> None:
        if not text:
            return
        cap = QLabel(text)
        cap.setObjectName("PopupCap")
        self.lay.addWidget(cap)

    def focus_editor(self) -> None:
        ed = getattr(self, "editor", None)
        if ed is None:
            return
        ed.setFocus()
        if isinstance(ed, QLineEdit):
            ed.selectAll()
        elif isinstance(ed, QPlainTextEdit):
            ed.moveCursor(QTextCursor.MoveOperation.End)

    # ---- 事件 ----
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._armed:
            self._armed = True
            QTimer.singleShot(0, self._arm)

    def _arm(self) -> None:
        """显示之后（下一轮事件循环）做三件事：挂点外部过滤器、激活窗口、给焦点。

        为什么要延后一轮：
        - 过滤器同步挂会自己把自己关掉 —— 打开弹层的那次按下还在沿父控件链
          往上传，QCoreApplication::notify 对链上每个对象都会再跑一遍应用级
          过滤器，于是「点按钮弹出确认框」这一下立刻被当成「点了弹层外面」。
        - activateWindow 在窗口真正映射之前调是空转，输入法照样跟不上。
        """
        if not (self._armed and self.isVisible()):
            return
        QApplication.instance().installEventFilter(self)
        self.raise_()
        self.activateWindow()
        if getattr(self, "editor", None) is not None \
                and not self.editor.hasFocus():
            self.focus_editor()

    def closeEvent(self, event) -> None:  # noqa: N802
        """点弹层外部时 Qt 只 hide 不 close 回调；这里补一次 finished，
        否则阻塞式调用会永远等不到返回值。"""
        if self._armed:
            QApplication.instance().removeEventFilter(self)
            self._armed = False
        self.finished.emit()
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._reject()
            return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """点到弹层外面就收起。输入法候选窗是别的进程的窗口，
        它的点击不会进到我们应用的事件队列，所以不会被误判成「点外面」。

        `_child` 是「本弹层自己又打开的弹层」：没有这层守卫的话，
        在新建卡里点「收集箱」→ 再点「新建清单」→ 在输入框里敲一下，
        外层会被连着关掉，卡片关掉时还会把半截标题静默建成一条任务。
        """
        if obj is not self and event.type() in (
                QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            if self._child is not None and self._child.isVisible():
                return False
            if not self.geometry().contains(event.globalPosition().toPoint()) \
                    and QApplication.activePopupWidget() is None:
                self.close()
        return super().eventFilter(obj, event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(SHADOW, SHADOW,
                     max(0.0, self.width() - SHADOW * 2),
                     max(0.0, self.height() - SHADOW * 2))
        dark = theme.is_dark()
        for i, alpha in ((4, 10), (3, 16), (2, 22), (1, 28)):
            c = QColor(0, 0, 0, alpha if not dark else int(alpha * 1.6))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(box.adjusted(-i, 1 - i, i, 1 + i),
                              RADIUS + i, RADIUS + i)
        p.end()
        super().paintEvent(event)


def place_popup(pop: QWidget, anchor: QWidget) -> None:
    """摆在锚点下方并夹回屏幕内；下方放不下就翻到上方。

    不做夹取时，靠屏幕下沿的控件（表单最后几行）一点，弹层整块跑到屏幕外，
    看起来就像「点了没反应」。
    """
    pop.adjustSize()
    scr = (anchor.screen() or QApplication.primaryScreen()).availableGeometry()
    w, h = pop.width(), pop.height()
    pos = anchor.mapToGlobal(QPoint(0, anchor.height() + 6))
    if pos.y() + h > scr.bottom():
        pos.setY(max(anchor.mapToGlobal(QPoint(0, 0)).y() - h - 6,
                     scr.top() + 4))
    pos.setX(min(max(pos.x(), scr.left() + 4),
                 max(scr.left() + 4, scr.right() - w - 4)))
    pos.setY(min(pos.y(), max(scr.top() + 4, scr.bottom() - h - 4)))
    pop.move(pos)


def center_popup(pop: QWidget, owner: QWidget) -> None:
    """没有明确锚点时（阻塞式调用）居中在所属窗口上。"""
    pop.adjustSize()
    win = owner.window() if owner is not None else None
    if win is not None and win is not QApplication.instance():
        rect = win.geometry()
    else:
        rect = QApplication.primaryScreen().availableGeometry()
    pop.move(rect.x() + (rect.width() - pop.width()) // 2,
             max(rect.y() + 40, rect.y() + (rect.height() - pop.height()) // 3))


# ---------------------------------------------------------------------------
# 具体弹层
# ---------------------------------------------------------------------------
class InputPopup(PopupCard):
    """单行文本 / 整数 / 小数输入。"""

    accepted = Signal(str)

    def __init__(self, kind: str = "text", initial="", tip: str = "",
                 lo: float = 1, hi: float = 9999, decimals: int = 0,
                 parent: QWidget | None = None, width: int = 268,
                 placeholder: str = ""):
        super().__init__(parent, width)
        self._caption(tip)
        if kind == "int":
            self.editor = QSpinBox()
            self.editor.setRange(int(lo), int(hi))
            self.editor.setValue(int(float(initial or lo)))
        elif kind == "double":
            self.editor = QDoubleSpinBox()
            self.editor.setDecimals(decimals)
            self.editor.setRange(float(lo), float(hi))
            self.editor.setValue(float(initial if initial != "" else lo))
        else:
            self.editor = QLineEdit("" if initial is None else str(initial))
        self.editor.setObjectName("PopupEdit")
        if placeholder and isinstance(self.editor, QLineEdit):
            self.editor.setPlaceholderText(placeholder)
        self.lay.addWidget(self.editor)
        self._footer(on_ok=self._submit)
        if hasattr(self.editor, "returnPressed"):
            self.editor.returnPressed.connect(self._submit)

    def _submit(self) -> None:
        if isinstance(self.editor, (QSpinBox, QDoubleSpinBox)):
            val = str(self.editor.value())
        else:
            val = self.editor.text().strip()
        self.accepted.emit(val)
        self._accept(val)


class NotePopup(PopupCard):
    """多行输入（打卡日志、备注）。"""

    accepted = Signal(str)

    def __init__(self, title: str = "", text: str = "",
                 parent: QWidget | None = None, width: int = 330,
                 height: int = 96):
        super().__init__(parent, width)
        self._caption(title)
        self.editor = QPlainTextEdit(text)
        self.editor.setObjectName("PopupNote")
        self.editor.setFixedHeight(height)
        self.lay.addWidget(self.editor)
        self._footer("保存", on_ok=self._submit)

    def _submit(self) -> None:
        text = self.editor.toPlainText().strip()
        self.accepted.emit(text)
        self._accept(text)


class ChoicePopup(PopupCard):
    """单选列表，替代 QInputDialog.getItem。"""

    accepted = Signal(str)

    def __init__(self, items: list[str], current: str = "", tip: str = "",
                 parent: QWidget | None = None, width: int = 268):
        super().__init__(parent, width)
        self._caption(tip)
        holder = QWidget()
        col = QVBoxLayout(holder)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        for it in items:
            b = QPushButton(it)
            b.setObjectName("PopupRow")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            widgets._apply_property(b, "rowColor",
                                    "accent" if it == current else "muted")
            b.clicked.connect(lambda _=False, v=it: self._submit(v))
            col.addWidget(b)
        col.addStretch(1)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setMaximumHeight(min(264, 32 * max(1, len(items)) + 8))
        area.setWidget(holder)
        self.lay.addWidget(area)

    def _submit(self, value: str) -> None:
        self.accepted.emit(value)
        self._accept(value)


class ConfirmPopup(PopupCard):
    """破坏性操作的确认卡。"""

    accepted = Signal()

    def __init__(self, text: str, ok_text: str = "删除",
                 parent: QWidget | None = None, width: int = 276):
        super().__init__(parent, width)
        lbl = QLabel(text)
        lbl.setObjectName("ConfirmText")
        lbl.setWordWrap(True)
        self.lay.addWidget(lbl)
        row = QHBoxLayout()
        row.setSpacing(8)
        ok = QPushButton(ok_text)
        ok.setObjectName("Danger")
        ok.clicked.connect(lambda: (self.accepted.emit(), self._accept(True)))
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self._reject)
        row.addStretch(1)
        row.addWidget(ok)
        row.addWidget(cancel)
        self.lay.addLayout(row)


class NotifyPopup(PopupCard):
    """轻量提示，替代 QMessageBox.information / warning。"""

    def __init__(self, title: str, text: str, danger: bool = False,
                 parent: QWidget | None = None, width: int = 310):
        super().__init__(parent, width)
        self._caption(title)
        body = QLabel(text)
        body.setObjectName("NotifyText")
        body.setWordWrap(True)
        if danger:
            widgets._apply_property(body, "danger", "true")
        self.lay.addWidget(body)
        ok = QPushButton("知道了")
        ok.setObjectName("Ghost")
        ok.clicked.connect(self._reject)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(ok)
        self.lay.addLayout(row)


# ---------------------------------------------------------------------------
# 阻塞式包装：签名对齐 QInputDialog / QMessageBox，便于逐处替换
# ---------------------------------------------------------------------------
def _exec(pop: PopupCard, owner: QWidget, center: bool = False):
    """跑一个局部事件循环等结果。

    退出路径是 `finished`（`_accept` / `_reject` / closeEvent 都会发）。再加一个
    看门狗轮询可见性：弹层要是被别的途径藏掉（父窗口整块 hide，走不到
    closeEvent），补一次退出，别把调用方永久挂住。
    早先这里是「120 秒后强制 close」，结果慢一点打字就被静默取消，
    所以改成只盯可见性 —— 人还看得见就继续等。
    """
    loop = QEventLoop(pop)
    pop.finished.connect(loop.quit)
    host = owner if isinstance(owner, PopupCard) else None
    if host is not None:
        host._child = pop
        pop.finished.connect(lambda: setattr(host, "_child", None))
    (center_popup if center else place_popup)(pop, owner)
    pop.show()                      # 激活 / 给焦点由 PopupCard._arm 在下一轮做
    watchdog = QTimer(pop)
    watchdog.setInterval(250)
    watchdog.timeout.connect(lambda: None if pop.isVisible() else loop.quit())
    watchdog.start()
    try:
        loop.exec()
    finally:
        watchdog.stop()
    return pop


def ask_text(parent: QWidget, title: str, label: str = "", text: str = "",
             center: bool = True) -> tuple[str, bool]:
    pop = _exec(InputPopup("text", text, tip=label or title, parent=parent),
                parent, center)
    return (pop._value, True) if pop._done else ("", False)


def ask_number(parent: QWidget, title: str, label: str = "", value=0,
               lo: float = 0, hi: float = 9999, decimals: int = 0,
               center: bool = True):
    """decimals=0 时返回 int（同 QInputDialog.getInt），否则返回 float。"""
    pop = _exec(InputPopup("double" if decimals > 0 else "int", value,
                           tip=label or title, lo=lo, hi=hi,
                           decimals=decimals, parent=parent), parent, center)
    if not pop._done:
        return (0.0 if decimals > 0 else 0, False)
    num = float(pop._value)
    return (round(num, decimals) if decimals > 0 else int(num), True)


def ask_double(parent: QWidget, title: str, label: str = "", value: float = 0.0,
               lo: float = 0.0, hi: float = 9999.0, step: float = 0.1,
               center: bool = True):
    return ask_number(parent, title, label, value, lo, hi, 2, center)


def ask_note(parent: QWidget, title: str, label: str = "", text: str = "",
             center: bool = True, width: int = 330,
             height: int = 96) -> tuple[str, bool]:
    """多行输入。width/height 可调：粘贴整段面经时 330×96 太小，看不清自己贴了什么。"""
    pop = _exec(NotePopup(label or title, text, parent=parent,
                          width=width, height=height), parent, center)
    return (pop._value, True) if pop._done else ("", False)


def get_item(parent: QWidget, title: str, label: str = "", items=(),
             current: int = 0, center: bool = True) -> tuple[str, bool]:
    items = list(items)
    cur = items[current] if 0 <= current < len(items) else ""
    pop = _exec(ChoicePopup(items, cur, tip=label or title, parent=parent),
                parent, center)
    return (pop._value, True) if pop._done else ("", False)


def confirm(parent: QWidget, title: str, text: str,
            ok_text: str = "删除", center: bool = True,
            width: int = 276) -> bool:
    """width 可调：导入前的分组预览要横向空间，276 会折成一长条看不到头。"""
    pop = _exec(ConfirmPopup(text, ok_text, parent=parent, width=width),
                parent, center)
    return bool(pop._done)


def notify(parent: QWidget, title: str, text: str,
           danger: bool = False, center: bool = True,
           width: int = 310) -> None:
    _exec(NotifyPopup(title, text, danger, parent=parent, width=width),
          parent, center)
