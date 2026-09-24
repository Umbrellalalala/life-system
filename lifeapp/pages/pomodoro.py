"""番茄钟模块：番茄计时 / 正计时 + 任务选择 + 专注设置 + Mini 模式。"""
from __future__ import annotations

import json
import random
import sys
import uuid
from datetime import date, datetime, timedelta

from PySide6.QtCore import (
    Qt, QTimer, Signal, QDateTime, QRectF, QRect, QPoint, QPointF, QSize,
    QObject, QEvent, QAbstractNativeEventFilter,
)
from PySide6.QtGui import (
    QColor, QPainter, QPen, QFont, QKeySequence, QShortcut, QAction, QPolygonF,
    QPalette,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QSpinBox, QLineEdit, QApplication, QSystemTrayIcon, QComboBox, QDialog,
    QFrame, QProgressBar, QScrollArea, QSlider, QButtonGroup, QAbstractButton,
    QSizePolicy, QMenu, QStackedWidget, QSplitter, QTextEdit,
    QGraphicsDropShadowEffect, QAbstractSpinBox,
)

from .. import services, theme, widgets, db, sounds, todo_icons
from .. import focus_ui
from .. import popups
from .base import Page
from .focus_stats import StatsView

MODE_META = {
    "work": ("专注工作", "red", "专注中"),
    "short": ("短休息", "green", "休息中"),
    "long": ("长休息", "blue", "休息中"),
}

# 概览侧栏可拖动宽度（像素）。
# 参考图里侧栏占了整整一半宽度（实测 806 / 1606 逻辑像素），所以默认值按页面
# 宽度比例算出来，而不是写死一个像素数 —— 窗口最大化和小窗口下比例才对得上。
SIDE_RATIO = 0.50
# 侧栏下限按内容实测：概览列自己的布局最小宽是 241（统计网格 204 + 左右边距），
# 原来写 320 等于凭空给整个窗口加了 80px 缩不下去的余量。
SIDE_MIN = 232
SIDE_MAX = 900

# 主环直径 / 左区宽度：参考图实测 357 / 854 ≈ 0.42
RING_RATIO = 0.42

# 专注记录：一次查这么多，但**分批渲染**（一屏只有十几条，全建是白建）。
RECORD_LIMIT = 120      # 取多少条进 plan
RECORD_BATCH = 24       # 每批渲染多少个 plan 项（记录行 + 日期标题）
RECORD_LOAD_AHEAD = 320  # 距底部多少像素就提前补下一批


def _side_default(width: int) -> int:
    """按页面宽度给出参考图比例的侧栏宽度。"""
    return max(SIDE_MIN, min(SIDE_MAX, int(width * SIDE_RATIO)))

QUOTES = [
    "每一个不曾起舞的日子，都是对生命的辜负。",
    "星光不问赶路人，时光不负有心人。",
    "以蝼蚁之行，展鸿鹄之志。",
    "不要因为走得太远，就忘了当初为什么出发。",
    "种一棵树最好的时间是十年前，其次是现在。",
    "自律者自由。",
    "专注当下，未来自来。",
    "不积跬步，无以至千里。",
    "怕什么真理无穷，进一寸有一寸的欢喜。",
    "凡是过往，皆为序章。",
]

# 时长格式化统一走 focus_ui.fmt_rec（无空格：1h20m / 45m / 1h）。
# 这里原来有一份 _fmt_hm / _fmt_rec 的本地副本，和 focus_ui 里的实现重复，
# 且产出的 "1 h 20 m" 与统计页的 "1h20m" 不一致，已删除。


def _pal(color: QColor) -> QPalette:
    """给 QLabel 造一个只改前景色的 QPalette（绕开主题 QSS 的字体/颜色规则）。"""
    pal = QPalette()
    pal.setColor(QPalette.WindowText, color)
    pal.setColor(QPalette.Text, color)
    return pal


def _fmt_clock(seconds: int) -> str:
    s = max(int(seconds), 0)
    if s >= 3600:
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}"
    m, sec = divmod(s, 60)
    return f"{m:02d}:{sec:02d}"


def _s(key: str, default: str) -> str:
    return db.get_setting(key, default)


def _s_int(key: str, default: int) -> int:
    try:
        return int(db.get_setting(key, str(default)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 可点击标签
# ---------------------------------------------------------------------------
class ClickLabel(QLabel):
    clicked = Signal()

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _SplitHandleFilter(QObject):
    """概览栏分隔条：双击复位到默认宽度。"""

    def __init__(self, page: "PomodoroPage"):
        super().__init__(page)
        self._page = page

    def eventFilter(self, obj, event):  # noqa: ANN001
        if event.type() == QEvent.MouseButtonDblClick:
            self._page.reset_side_width()
            return True
        return False


class _HoverFilter(QObject):
    """让弹层菜单的行在鼠标移入时高亮（同一时刻只高亮一行）。"""

    def __init__(self, menu: focus_ui.PopupMenu):
        super().__init__(menu)
        self._menu = menu

    def eventFilter(self, obj, event):  # noqa: ANN001
        et = event.type()
        if et == QEvent.Enter:
            try:
                self._menu.set_hover(self._menu.rows.index(obj))
            except ValueError:
                pass
        elif et == QEvent.Leave:
            self._menu.set_hover(-1)
        return False


# ---------------------------------------------------------------------------
# iOS 风格开关
# ---------------------------------------------------------------------------
class Toggle(QAbstractButton):
    def __init__(self, checked: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(46, 26)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(2, 2, -2, -2)
        p.setBrush(QColor(theme.get("accent") if self.isChecked() else theme.get("border")))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(r, 11, 11)
        x = r.right() - r.height() if self.isChecked() else r.left()
        knob = QRectF(x, r.top(), r.height(), r.height()).adjusted(2, 2, -2, -2)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(knob)


# ---------------------------------------------------------------------------
# 概览瓦片
# ---------------------------------------------------------------------------
class OverviewTile(QFrame):
    """概览瓦片：小标签 + 大数值。

    底色和文字全部自绘 / 走 QPalette，不走主题 QSS：
    ``#FocusTile`` 的底色是偏蓝的 ``surface_hi``、``#FocusTileValue`` 是 700 字重，
    都和参考图对不上（实测底 #f8f8f8、数值常规字重）。为一个卡片去改全局
    theme.py 不划算，所以这里换成不被 QSS 命中的 objectName 自己控制。
    """

    RADIUS = 10

    def __init__(self, label: str):
        super().__init__()
        self.setObjectName("FocusTileCard")
        self.setFixedHeight(76)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 13, 14, 12)
        lay.setSpacing(2)
        self.label_lbl = QLabel(label)
        self.label_lbl.setObjectName("FocusTileCardLabel")
        self.value_lbl = QLabel("0")
        self.value_lbl.setObjectName("FocusTileCardValue")
        lay.addWidget(self.label_lbl)
        lay.addWidget(self.value_lbl)
        lay.addStretch(1)
        self.apply_theme()

    def apply_theme(self) -> None:
        """主题切换后要重新取一次中性灰（QPalette 不会跟着 QSS 自动变）。"""
        lf = QFont()
        lf.setPixelSize(13)
        self.label_lbl.setFont(lf)
        self.label_lbl.setPalette(_pal(focus_ui.neutral_text("label")))
        vf = QFont()
        vf.setPixelSize(25)
        vf.setWeight(QFont.Normal)
        self.value_lbl.setFont(vf)
        self.value_lbl.setPalette(_pal(focus_ui.neutral_text("value")))

    def set_value(self, value: str) -> None:
        """数值里的单位字母（h / m）按参考图缩小一档，数字保持大字号。"""
        parts = value.split(" ")
        if len(parts) == 1 and parts[0].isdigit():
            self.value_lbl.setText(parts[0])
            return
        html = []
        for tok in parts:
            size = 25 if tok.isdigit() else 13
            html.append(f'<span style="font-size:{size}px">{tok}</span>')
        self.value_lbl.setText(" ".join(html))

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(focus_ui.tile_bg())
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0, 0, -1, -1),
                          self.RADIUS, self.RADIUS)


class _RecordLine(widgets.ElidedLabel):
    """专注记录里的一行文字（时间 / 任务名 / 时长）。

    颜色走中性灰而非主题里偏蓝的 ``muted``，所以用 QPalette + QFont 直接指定，
    不挂主题里那批 ``FocusRecord*`` QSS 名。
    """

    _SIZE = {"time": 12.5, "task": 13.5, "dur": 12.5}
    _ROLE = {"time": "time", "task": "title", "dur": "time"}

    def __init__(self, text: str, role: str, line_h: int):
        super().__init__(text)
        self._role = role
        self.setFixedHeight(line_h)
        self.apply_theme()

    def apply_theme(self) -> None:
        f = QFont()
        f.setPixelSize(self._SIZE[self._role])
        f.setWeight(QFont.Medium if self._role == "task" else QFont.Normal)
        self.setFont(f)
        self.setPalette(_pal(focus_ui.neutral_text(self._ROLE[self._role])))


class _DateHeader(QLabel):
    """记录列表的日期分组标题（参考图：中性灰、不加蓝、字重中等）。"""

    def __init__(self, text: str):
        super().__init__(text)
        self.setContentsMargins(0, 14, 0, 6)
        self.apply_theme()

    def apply_theme(self) -> None:
        f = QFont()
        f.setPixelSize(14)
        f.setWeight(QFont.DemiBold)
        self.setFont(f)
        self.setPalette(_pal(focus_ui.neutral_text("group")))


# ---------------------------------------------------------------------------
# 专注记录详情卡
# ---------------------------------------------------------------------------
class RecordCardPopup(popups.PopupCard):
    """点击专注记录 → 浮层详情卡（参考图里那张白色圆角卡）。

    卡片给出番茄图标 + 任务名、完整起止时间、时长，以及一个「记录你的想法」
    文本域；关闭时把文本写回 ``pomodoro.note``。

    建在 ``popups.PopupCard`` 上而不是裸 ``Qt.Popup``：那个文本域是给人打中文的，
    而 Popup 窗口在 Windows 上「显示但不激活」，输入法只跟随被激活的窗口。
    阴影也交给 PopupCard 的 shell 画，不再自己挂 QGraphicsDropShadowEffect。
    """

    W = 434
    H = 306

    def __init__(self, rec: dict, parent: QWidget | None = None, on_delete=None):
        super().__init__(parent, width=self.W)
        self._rid = rec.get("id")
        self._on_delete = on_delete
        self._del_armed = False
        card = self.card
        card.setStyleSheet(
            "QFrame#FocusRecCardNote { background: %s; border: none;"
            " border-radius: 10px; padding: 10px 12px; color: %s;"
            " font-size: 13.5px; }"
            % (focus_ui.pill_idle_bg().name(), theme.get("text")))
        lay = self.lay
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(0)

        started = (rec.get("started_at") or "")[:16]
        dur = int(rec.get("duration_min") or 0)
        end_str = started
        if started:
            try:
                end_str = (datetime.strptime(started, "%Y-%m-%d %H:%M")
                           + timedelta(minutes=dur)).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass

        head = QHBoxLayout()
        head.setSpacing(9)
        head.addWidget(focus_ui.MenuIcon("tomato", 22))
        title = widgets.ElidedLabel("、".join(
            [t.strip() for t in (rec.get("task") or "").replace("，", ",").split(",")
             if t.strip()]) or "自由专注")
        title.setStyleSheet("background: transparent; border: none;")
        tf = QFont()
        tf.setPixelSize(16)
        tf.setWeight(QFont.Medium)
        title.setFont(tf)
        title.setPalette(_pal(QColor(theme.get("text_hi"))))
        head.addWidget(title, 1)
        lay.addLayout(head)
        lay.addSpacing(14)

        for kind, text in (("timer", f"{started} - {end_str}" if started else "—"),
                           ("stats", focus_ui.fmt_rec(dur))):
            row = QHBoxLayout()
            row.setSpacing(9)
            row.addWidget(focus_ui.MenuIcon(kind, 15, "muted"))
            lbl = QLabel(text)
            lbl.setStyleSheet(
                "background: transparent; border: none;"
                " color: %s; font-size: 13.5px;" % theme.get("muted"))
            row.addWidget(lbl, 1)
            lay.addLayout(row)
            lay.addSpacing(6)
        lay.addSpacing(8)

        self.note_edit = QTextEdit()
        self.note_edit.setObjectName("FocusRecCardNote")
        self.note_edit.setPlaceholderText("记录你的想法...")
        self.note_edit.setFixedHeight(120)
        self.note_edit.setPlainText(rec.get("note") or "")
        # 交给 PopupCard：延后一轮激活窗口 + 把焦点给到这个框，中文输入法才跟得上
        self.editor = self.note_edit
        lay.addWidget(self.note_edit)

        # 删除入口：原来只有「记录行右键」这一条路，右键是看不见的功能，
        # 记错一条的人根本找不到怎么删。放在详情卡里，点到记录就顺手能删。
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 12, 0, 0)
        self.del_btn = QPushButton("删除记录")
        self.del_btn.setCursor(Qt.PointingHandCursor)
        self.del_btn.clicked.connect(self._ask_delete)
        self._del_style()
        foot.addWidget(self.del_btn)
        foot.addStretch(1)
        lay.addLayout(foot)
        self._del_timer = QTimer(self)
        self._del_timer.setSingleShot(True)
        self._del_timer.setInterval(4000)
        self._del_timer.timeout.connect(self._reset_del)

    def _del_style(self):
        armed = self._del_armed
        col = theme.get("red") if armed else theme.get("muted")
        self.del_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: %s;"
            " font-size: 12.5px; padding: 2px 0; text-align: left; }"
            " QPushButton:hover { color: %s; }"
            % (col, theme.get("red")))

    def _ask_delete(self):
        # 两步确认：这一下点下去是删数据，不能靠「用户应该知道自己在干什么」
        if not self._del_armed:
            self._del_armed = True
            self.del_btn.setText("再点一次确认删除")
            self._del_style()
            self._del_timer.start()
            return
        self._del_timer.stop()
        self._rid = None              # 关掉时不再回写笔记，人已经删了
        if self._on_delete is not None:
            self._on_delete()
        self.close()

    def _reset_del(self):
        self._del_armed = False
        self.del_btn.setText("删除记录")
        self._del_style()

    def closeEvent(self, event) -> None:  # noqa: N802
        """关闭即落盘。PopupCard 点外面 / Esc 都走 close，不再是 hideEvent。"""
        if self._rid is not None:
            services.pomodoro_update(int(self._rid), note=self.note_edit.toPlainText())
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# 补录记录弹窗
# ---------------------------------------------------------------------------
class AddRecordDialog(QDialog):
    """添加专注记录：专注任务 / 开始时间 / 结束时间 / 类型 / 专注笔记。

    时间与时长是同一份数据的不同视图，任一处修改都会同步另外两处：

    * 改「开始时间」→ 结束时间 = 开始 + 时长
    * 改「结束时间」→ 时长 = 结束 - 开始
    * 改「类型」（N 个番茄 / H 小时 M 分钟）→ 时长变，结束时间跟着走

    开始时间未选时保存不可用（参考设计里「保存」初始就是灰的）。
    """

    def __init__(self, default_minutes: int, focus_min: int = 25,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("添加专注记录")
        self.setFixedWidth(500)
        self.setStyleSheet("QDialog { background: %s; }" % theme.get("bg_alt"))

        self._focus_min = max(int(focus_min), 1)
        self._duration = max(int(default_minutes), 1)
        self._start: datetime | None = None
        self._task = ""
        self._popup: QWidget | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 28, 22)
        lay.setSpacing(0)

        title = QLabel("添加专注记录")
        title.setObjectName("SettingsTitle")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)
        lay.addSpacing(18)

        form = QGridLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setColumnMinimumWidth(0, 84)
        form.setColumnStretch(1, 1)

        self.task_field = focus_ui.PickerField("选择任务")
        self.task_field.clicked.connect(self._pick_task)
        self.start_field = focus_ui.PickerField("设置时间")
        self.start_field.clicked.connect(lambda: self._pick_time("start"))
        self.end_field = focus_ui.PickerField("设置时间")
        self.end_field.clicked.connect(lambda: self._pick_time("end"))
        self.type_field = focus_ui.PickerField("番茄计时：0 个番茄")
        self.type_field.clicked.connect(self._pick_type)

        for row, (text, field) in enumerate((
                ("专注任务", self.task_field),
                ("开始时间", self.start_field),
                ("结束时间", self.end_field),
                ("类型", self.type_field))):
            lbl = QLabel(text)
            lbl.setObjectName("FormLabel")
            lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            form.addWidget(lbl, row, 0)
            form.addWidget(field, row, 1)

        note_lbl = QLabel("专注笔记")
        note_lbl.setObjectName("FormLabel")
        note_lbl.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.note_edit = QTextEdit()
        self.note_edit.setObjectName("PickerNote")
        self.note_edit.setPlaceholderText("记录你的想法...")
        self.note_edit.setFixedHeight(130)
        self.note_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        form.addWidget(note_lbl, 4, 0)
        form.addWidget(self.note_edit, 4, 1)
        lay.addLayout(form)

        lay.addSpacing(18)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        btn_row.addStretch(1)
        self.ok_btn = QPushButton("保存")
        self.ok_btn.setObjectName("FocusPrimary")
        self.ok_btn.setFixedSize(104, 36)
        self.ok_btn.setCursor(Qt.PointingHandCursor)
        self.ok_btn.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.setObjectName("FocusGhost")
        cancel.setFixedSize(104, 36)
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.ok_btn)
        btn_row.addWidget(cancel)
        lay.addLayout(btn_row)

        self._sync()
        focus_ui.fade_in(self, 140)

    # -- 弹层管理 ---------------------------------------------------------
    def _open_popup(self, popup: QWidget, field: focus_ui.PickerField) -> None:
        """把弹层贴到字段正下方；同一时刻只留一个。

        弹层是 ``Qt.Popup`` 独立窗口，所以定位必须用 **全局坐标**
        （``mapToGlobal``）；用 ``mapTo(self)`` 会把它丢到屏幕角落。
        另外不要在 show 之前 ``setParent`` —— 会丢掉 Qt.Popup 标志。
        """
        self._close_popup()
        popup.adjustSize()
        pw = popup.width()
        pos = field.mapToGlobal(QPoint(0, field.height()))
        x = max(0, min(pos.x(), self.x() + self.width() - pw - 8))
        popup.move(x, pos.y() + 6)
        field.set_active(True)
        popup.closed.connect(lambda: field.set_active(False))
        popup.show()
        self._popup = popup

    def _close_popup(self) -> None:
        p, self._popup = self._popup, None
        if p is not None:
            try:
                p.close()
            except RuntimeError:
                pass

    # -- 三个选择器 -------------------------------------------------------
    def _pick_task(self) -> None:
        popup = TaskPickerPopup(None, self)
        popup.picked.connect(self._on_task)
        self._open_popup(popup, self.task_field)

    def _on_task(self, title: str) -> None:
        self._task = title
        self.task_field.set_value(title or "自由专注", placeholder=not title)

    def _pick_time(self, which: str) -> None:
        if which == "start":
            base = self._start or datetime.now().replace(second=0, microsecond=0)
        else:
            base = (self._start + timedelta(minutes=self._duration)
                    if self._start else datetime.now().replace(second=0, microsecond=0))
        popup = DateTimePickerPopup(base, self)
        popup.picked.connect(lambda dt, w=which: self._on_time(w, dt))
        self._open_popup(popup, self.start_field if which == "start" else self.end_field)

    def _on_time(self, which: str, dt: datetime) -> None:
        if which == "start":
            self._start = dt
            if self._duration <= 0:
                self._duration = self._focus_min
        else:
            # 结束时间决定时长；结束早于开始就当作次日（跨零点专注）
            if self._start is not None:
                delta = int((dt - self._start).total_seconds() // 60)
                if delta <= 0:
                    delta += 24 * 60
                self._duration = delta
            else:
                self._start = dt - timedelta(minutes=self._duration)
        self._sync()

    def _pick_type(self) -> None:
        popup = TypePickerPopup(self._duration, self._focus_min, self)
        # 参考设计里「类型」弹层和字段一样宽（日期弹层才比字段窄）
        popup.setMinimumWidth(self.type_field.width())
        popup.picked.connect(self._on_type)
        self._open_popup(popup, self.type_field)

    def _on_type(self, minutes: int) -> None:
        if minutes > 0:
            self._duration = minutes
        self._sync()

    # -- 刷新显示 ---------------------------------------------------------
    def _type_text(self) -> str:
        n = max(1, int(round(self._duration / self._focus_min)))
        if abs(n * self._focus_min - self._duration) < 1:
            return "番茄计时：%d 个番茄" % n
        h, m = divmod(self._duration, 60)
        if h and m:
            return "正计时：%d 小时 %d 分钟" % (h, m)
        if h:
            return "正计时：%d 小时" % h
        return "正计时：%d 分钟" % m

    def _sync(self) -> None:
        if self._start is None:
            self.start_field.set_value("设置时间", placeholder=True)
            self.end_field.set_value("设置时间", placeholder=True)
        else:
            self.start_field.set_value(self._start.strftime("%H:%M"), placeholder=False)
            end = self._start + timedelta(minutes=self._duration)
            self.end_field.set_value(end.strftime("%H:%M"), placeholder=False)
        self.type_field.set_value(self._type_text(), placeholder=False)
        self.ok_btn.setEnabled(self._start is not None and self._duration > 0)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._close_popup()
        super().closeEvent(event)

    # -- 结果 -------------------------------------------------------------
    def values(self) -> tuple[str, int, str, str]:
        """返回 (任务, 时长分钟, 开始时间 'YYYY-MM-DD HH:MM:SS', 笔记)。"""
        started = (self._start or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        return (self._task.strip(), self._duration, started,
                self.note_edit.toPlainText().strip())


# ---------------------------------------------------------------------------
# 沉浸模式：全屏纯黑 + 翻页时钟
# ---------------------------------------------------------------------------
class ImmersiveOverlay(QWidget):
    """沉浸模式：纯黑全屏，翻页时钟 + 页码圆点，支持左右切页 / 空格暂停。"""

    def __init__(self, page: "PomodoroPage"):
        super().__init__(None, Qt.Window)
        self.page = page
        self.setWindowTitle("沉浸专注")
        self.setStyleSheet("background-color:#000000;")
        self.setFocusPolicy(Qt.StrongFocus)
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(400)
        self._sync_timer.timeout.connect(self.sync)

        root = QVBoxLayout(self)
        root.setContentsMargins(40, 40, 40, 18)
        root.setSpacing(0)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_clock_page())
        self.stack.addWidget(self._build_info_page())
        root.addWidget(self.stack, 1)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addStretch(1)
        self.dots = focus_ui.PageDots(2)
        self.dots.changed.connect(self._goto)
        bottom.addWidget(self.dots)
        bottom.addStretch(1)
        root.addLayout(bottom)

        self.exit_btn = QPushButton(self)
        self.exit_btn.setFixedSize(34, 34)
        self.exit_btn.setCursor(Qt.PointingHandCursor)
        self.exit_btn.setToolTip("退出沉浸模式（Esc）")
        self.exit_btn.setStyleSheet(
            "QPushButton { background:transparent; border:none; border-radius:8px; }"
            "QPushButton:hover { background:#1e1e22; }")
        icon_lay = QVBoxLayout(self.exit_btn)
        icon_lay.setContentsMargins(0, 0, 0, 0)
        icon = focus_ui.MenuIcon("immersive", 17, "muted")
        icon_lay.addWidget(icon, 0, Qt.AlignCenter)
        self.exit_btn.clicked.connect(self.close)

    # -- 页面构建 ---------------------------------------------------------
    def _build_clock_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addStretch(1)
        self.clock = focus_ui.FlipClock()
        self.clock.set_card_height(190)
        lay.addWidget(self.clock)
        lay.addSpacing(46)
        self.hint = QLabel("准备好开始专注吧")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setStyleSheet(
            "color:#8b8b95; font-size:12.5px; background:transparent;")
        lay.addWidget(self.hint)
        lay.addSpacing(16)
        self.main_btn = QPushButton("开始")
        self.main_btn.setFixedSize(128, 36)
        self.main_btn.setCursor(Qt.PointingHandCursor)
        self.main_btn.setStyleSheet(
            "QPushButton { background:#3f6ef5; color:#ffffff; border:none;"
            " border-radius:18px; font-size:13.5px; font-weight:600; }"
            "QPushButton:hover { background:#5480f7; }"
            "QPushButton:pressed { background:#3462e6; }")
        self.main_btn.clicked.connect(self._toggle)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.main_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def _build_info_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        lay.addStretch(1)
        self.info_task = QLabel("自由专注")
        self.info_task.setAlignment(Qt.AlignCenter)
        self.info_task.setStyleSheet(
            "color:#f2f2f5; font-size:24px; font-weight:700; background:transparent;")
        lay.addWidget(self.info_task)
        self.info_stats = QLabel("")
        self.info_stats.setAlignment(Qt.AlignCenter)
        self.info_stats.setStyleSheet(
            "color:#9a9aa4; font-size:13px; background:transparent;")
        lay.addWidget(self.info_stats)
        lay.addSpacing(22)
        self.info_quote = QLabel("")
        self.info_quote.setAlignment(Qt.AlignCenter)
        self.info_quote.setWordWrap(True)
        self.info_quote.setStyleSheet(
            "color:#6d6d78; font-size:13px; background:transparent;")
        lay.addWidget(self.info_quote)
        lay.addStretch(1)
        return w

    # -- 交互 -------------------------------------------------------------
    def _goto(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)
        self.dots.set_index(idx)

    def _toggle(self) -> None:
        self.page._toggle()
        self.sync()

    def sync(self) -> None:
        """时钟 / 按钮状态。每 400ms 一次，**不查库**。

        今日统计那两行拆到 refresh_stats()，由页面的 _refresh_stats 在记录
        真正增删时推过来 —— 挂在这里等于每 400ms 打两条 SQL。
        """
        pg = self.page
        if pg.timer_mode == "countup":
            self.clock.set_time(_fmt_clock(pg.countup_elapsed))
        else:
            self.clock.set_time(pg._fmt(max(pg.remaining, 0)))

        if pg.running:
            self.hint.setText(f"专注中 · {pg._task or '自由专注'}")
            self.main_btn.setText("暂停")
        elif pg.timer_mode == "countup" and pg.countup_elapsed > 0:
            self.hint.setText("已暂停")
            self.main_btn.setText("继续")
        elif pg.timer_mode == "pomodoro" and pg.total and pg.remaining < pg.total:
            self.hint.setText("已暂停" if pg.remaining > 0 else "本轮已结束")
            self.main_btn.setText("继续" if pg.remaining > 0 else "开始")
        else:
            self.hint.setText("准备好开始专注吧")
            self.main_btn.setText("开始")

        self.info_task.setText(pg._task or "自由专注")
        if not self.info_quote.text():
            self.info_quote.setText("“ " + random.choice(QUOTES))

    def refresh_stats(self) -> None:
        today = services.pomodoro_total_minutes_today()
        self.info_stats.setText(
            f"今日 {services.pomodoro_today_count()} 个番茄 · "
            f"{focus_ui.fmt_rec(today)}")

    def keyPressEvent(self, event) -> None:  # noqa: N802
        k = event.key()
        if k == Qt.Key_Escape:
            self.close()
        elif k == Qt.Key_Space:
            self._toggle()
        elif k in (Qt.Key_Right, Qt.Key_Down):
            self._goto(min(self.stack.currentIndex() + 1, 1))
        elif k in (Qt.Key_Left, Qt.Key_Up):
            self._goto(max(self.stack.currentIndex() - 1, 0))

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta > 0:
            self._goto(max(self.stack.currentIndex() - 1, 0))
        elif delta < 0:
            self._goto(min(self.stack.currentIndex() + 1, 1))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.exit_btn.move(self.width() - self.exit_btn.width() - 18,
                           self.height() - self.exit_btn.height() - 10)

    def showEvent(self, event) -> None:  # noqa: N802
        self.sync()
        self.refresh_stats()     # 统计行不再由 sync 的轮询负责，打开时先取一次
        self._sync_timer.start()
        self.setFocus()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._sync_timer.stop()


# ---------------------------------------------------------------------------
# 任务选择弹窗（「专注 work >」）
# ---------------------------------------------------------------------------
DEFAULT_HABITS = ["冥想", "早起", "早睡", "运动"]


def _fmt_due_label(due: str) -> str:
    """到期日显示成参考图那种人话：今天 / 明天 / 9月14日，而不是 2026-09-14。"""
    if not due:
        return ""
    try:
        d = datetime.strptime(due[:10], "%Y-%m-%d").date()
    except ValueError:
        return due
    delta = (d - datetime.now().date()).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    return f"{d.month}月{d.day}日"


class _FilterMenu(QFrame):
    """日期筛选弹层：智能清单 + 清单 + 标签，带分组标题和选中标记。

    不复用 ``focus_ui.PopupMenu``，因为那个只支持扁平的图标+文字行，
    这里需要分组标题和右侧的 ✓。
    """

    chosen = Signal(str)

    def __init__(self, options: list[tuple[str, str, str]], current: str,
                 parent: QWidget | None = None):
        super().__init__(parent, focus_ui.POPUP_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("FocusMenu")
        card = QFrame(self)
        card.setObjectName("FilterMenuCard")
        card.setStyleSheet(
            "QFrame#FilterMenuCard { background: %s; border: 1px solid %s;"
            " border-radius: 10px; }" % (theme.get("surface"), theme.get("border")))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(1)
        self.setFixedWidth(186)

        for kind, key, label in options:
            if kind == "section":
                sec = QLabel(label)
                sec.setStyleSheet(
                    "background: transparent; border: none; padding: 8px 8px 2px;"
                    " color: %s; font-size: 11.5px;" % theme.get("muted"))
                lay.addWidget(sec)
                continue
            row = QPushButton(label)
            row.setCursor(Qt.PointingHandCursor)
            checked = key == current
            row.setStyleSheet(
                "QPushButton { background: %s; border: none; border-radius: 7px;"
                " padding: 7px 10px; text-align: left; color: %s; font-size: 13px; }"
                "QPushButton:hover { background: %s; }"
                % (theme.get("focus_soft") if checked else "transparent",
                   theme.get("focus") if checked else theme.get("text"),
                   theme.get("surface_hi")))
            row.clicked.connect(lambda _c=False, k=key: (self.chosen.emit(k),
                                                         self.close()))
            lay.addWidget(row)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.deleteLater()


class TaskPickerPopup(popups.PopupCard):
    """任务选择弹窗：任务/习惯 tab + 搜索 + 日期筛选 + 分组列表。

    建在 ``popups.PopupCard`` 而不是裸 ``Qt.Popup`` 上：Popup 窗口在 Windows 上
    「显示但不激活」，而输入法只跟随被激活的窗口 —— 搜索框里能敲英文数字、
    切中文却没反应。PopupCard 用 Qt.Tool + 延后 activateWindow/给焦点绕开这点。
    """

    picked = Signal(str)
    fav_picked = Signal(dict)
    manage_favs = Signal()
    closed = Signal()

    def __init__(self, page: "PomodoroPage", parent: QWidget | None = None,
                 fav_tab: bool = False):
        """``fav_tab=True`` 才多出「常用专注」那一签。

        只有主页那枚「专注 ›」要它（点了就是开始一个专注）。补录记录、Mini 窗、
        以及「添加常用专注」里那把 🔗 都只是在挑一个**任务名**，那一签点了没反应
        —— 挑中一个常用专注对它们没有任何意义，留着就是个死路。
        """
        super().__init__(parent, width=324)
        self.page = page
        self._filter = "today"
        card = self.card
        # 只覆盖内部各件的样式，卡片本身的底色/圆角交给 AppPopup（popups 统一）。
        # 主题里 Picker* 那组是给 420px 宽的老版本调的，参考图实测只有 323 宽、
        # 行距 32，且灰底不带蓝味。
        card.setStyleSheet(
            "QFrame#PickerSearchBox { background: %s; border: none;"
            " border-radius: 8px; }"
            "QLineEdit#PickerSearchInner { background: transparent; border: none;"
            " font-size: 13px; color: %s; }"
            "QLabel#PickerGroup { background: transparent; border: none;"
            " font-size: 12.5px; font-weight: 500; color: %s; padding: 8px 6px 2px; }"
            "QFrame#PickerRow { background: transparent; border: none;"
            " border-radius: 6px; }"
            "QFrame#PickerRow:hover { background: %s; }"
            "QLabel#PickerTitle { background: transparent; border: none;"
            " font-size: 13.5px; color: %s; }"
            "QLabel#PickerDate { background: transparent; border: none;"
            " font-size: 12.5px; color: %s; }"
            "QLabel#PickerDate[overdue=\"true\"] { color: %s; }"
            "QLabel#PickerDate[today=\"true\"] { color: %s; }"
            # 键盘选中态：放在 :hover 之后，两者同时命中时以选中为准
            "QFrame#PickerRow[sel=\"true\"] { background: %s; }"
            % (focus_ui.tile_bg().name(), theme.get("muted"),
               focus_ui.neutral_text("group").name(),
               theme.get("surface_hi"), theme.get("text_hi"),
               theme.get("muted"), theme.get("red"), theme.get("focus"),
               theme.get("focus_soft")))
        lay = self.lay
        lay.setContentsMargins(0, 0, 0, 6)
        lay.setSpacing(0)

        # tab 行：参考图里两颗胶囊**居中**、右上角没有关闭按钮，
        # 直接复用顶部那条 SegmentedControl（未选中也有灰底胶囊）。
        # 用 _kinds 存每一签的语义，索引会随 fav_tab 变，不能再拿数字当签。
        self._kinds = ["task", "fav", "habit"] if fav_tab else ["task", "habit"]
        labels = {"task": "任务", "fav": "常用专注", "habit": "习惯"}
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(18, 14, 18, 10)
        tab_row.addStretch(1)
        self._tabs = focus_ui.SegmentedControl(
            [labels[k] for k in self._kinds], "pill", 28)
        self._tabs.changed.connect(lambda _i: self._refresh())
        tab_row.addWidget(self._tabs)
        tab_row.addStretch(1)
        lay.addLayout(tab_row)

        # 搜索（图标 + 无边框输入框放进同一个灰底容器，避免用位图图标糊边）
        search_row = QHBoxLayout()
        search_row.setContentsMargins(14, 0, 14, 8)
        search_box = QFrame()
        search_box.setObjectName("PickerSearchBox")
        search_box.setFixedHeight(34)
        sb = QHBoxLayout(search_box)
        sb.setContentsMargins(10, 0, 10, 0)
        sb.setSpacing(8)
        sb.addWidget(focus_ui.MenuIcon("search", 15, "muted"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索，Enter 选第一个")
        self.search.setObjectName("PickerSearchInner")
        # 列表本身不抢焦点（焦点得留在搜索框里好接着打字），所以键盘事件
        # 要在这个框上截：Enter 选第一条、↑↓ 移动高亮。
        self._rows: list[tuple[QFrame, str]] = []
        self._sel = -1
        self.search.installEventFilter(self)
        # 防抖：_refresh 会把列表全删重建并整表查一次 todo_list()，
        # 直接挂在 textChanged 上等于每敲一个字就来一遍。
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._refresh)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        # 交给 PopupCard：_arm() 会延后一轮激活窗口并把焦点给到它，输入法才跟得上
        self.editor = self.search
        sb.addWidget(self.search, 1)
        search_row.addWidget(search_box)
        lay.addLayout(search_row)

        # 日期筛选：参考图是**无边框**的「📅 今天 」文字按钮，点开是一组
        # 智能清单 / 清单 / 标签；不是带边框的 QComboBox。放进一个可隐藏的容器，
        # 因为「常用专注 / 习惯」两签没有日期维度，留着这行会误导。
        self.filter_row = QWidget()
        date_row = QHBoxLayout(self.filter_row)
        date_row.setContentsMargins(14, 0, 14, 8)
        date_row.setSpacing(5)
        self.date_btn = QPushButton("今天")
        self.date_btn.setObjectName("PickerDateBtn")
        self.date_btn.setCursor(Qt.PointingHandCursor)
        self.date_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            " color: %s; font-size: 13px; padding: 2px 0; }"
            "QPushButton:hover { color: %s; }"
            % (theme.get("text_hi"), theme.get("focus")))
        self.date_btn.clicked.connect(self._open_date_menu)
        date_row.addWidget(focus_ui.MenuIcon("calendar", 15, "muted"))
        date_row.addWidget(self.date_btn)
        date_row.addWidget(focus_ui.MenuIcon("chevron", 12, "muted"))
        date_row.addStretch(1)
        lay.addWidget(self.filter_row)

        # 浏览时列表只装「今天」，打字搜索却会跨到全部 —— 这个差别得说明白，
        # 否则「今天」为空时看起来像一条任务都没有。
        self.filter_hint = QLabel("")
        self.filter_hint.setContentsMargins(16, 0, 16, 6)
        self.filter_hint.setStyleSheet(
            "background: transparent; border: none; font-size: 11.5px;"
            " color: %s;" % theme.get("muted"))
        self.filter_hint.hide()
        lay.addWidget(self.filter_hint)

        # 列表
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFixedHeight(332)
        self.list_content = QWidget()
        self.list_layout = QVBoxLayout(self.list_content)
        self.list_layout.setContentsMargins(8, 0, 8, 0)
        self.list_layout.setSpacing(0)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_content)
        lay.addWidget(self.scroll)

        self._refresh()

    # -- 日期 / 清单 / 标签筛选 ------------------------------------------
    def _filter_options(self) -> list[tuple[str, str, str]]:
        """(kind, key, label) —— kind 为 "item" 或 "section"。

        缓存一份：开一次菜单要读它两遍（菜单本身 + 按钮文案），每遍都要
        查一次清单表和一次标签表。弹窗每次打开都是新实例，所以缓存到
        实例生命周期就够，不会读到过期的清单列表。
        """
        if getattr(self, "_options_cache", None) is None:
            self._options_cache = self._build_filter_options()
        return self._options_cache

    def _build_filter_options(self) -> list[tuple[str, str, str]]:
        out = [("item", "today", "今天"), ("item", "tomorrow", "明天"),
               ("item", "soon7", "最近7天"), ("item", "all", "全部")]
        out.append(("section", "", "清单"))
        out.append(("item", "list:inbox", "收集箱"))
        for lst in services.list_all():
            out.append(("item", f"list:{lst['id']}", lst["name"]))
        tags = services.tag_all()
        if tags:
            out.append(("section", "", "标签"))
            for t in tags:
                out.append(("item", f"tag:{t['id']}", t["name"]))
        return out

    def _open_date_menu(self) -> None:
        menu = _FilterMenu(self._filter_options(), self._filter_key(), self)
        menu.chosen.connect(self._apply_filter)
        btn = self.sender() or self.date_btn
        pos = btn.mapToGlobal(QPoint(0, btn.height() + 4)) \
            if isinstance(btn, QWidget) else self.mapToGlobal(QPoint(14, 120))
        menu.move(pos)
        menu.show()

    def _filter_key(self) -> str:
        return self._filter

    def _apply_filter(self, key: str) -> None:
        self._filter = key
        # 注意解包顺序是 (kind, key, label) —— 之前按 (k, _, t) 取，把 kind 当成了
        # 字典键，于是这里永远查不到、按钮文字选了也不变。
        labels = {k: t for _kind, k, t in self._filter_options()}
        self.date_btn.setText(labels.get(key, key))
        self._refresh()

    def _make_row(self, todo: dict) -> QWidget:
        row = QFrame()
        row.setObjectName("PickerRow")
        row.setFixedHeight(32)
        row.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 0, 10, 0)
        h.setSpacing(9)
        # 参考图里行首是**优先级色的空心圆环**，不是一个 ○ 字符
        circle = todo_icons.PrioCheckBox(size=15, shape="circle")
        circle.set_priority(int(todo.get("priority") or 0))
        circle.setEnabled(False)          # 只作展示，点击交给整行
        h.addWidget(circle)
        title = widgets.ElidedLabel(todo["title"])
        title.setObjectName("PickerTitle")
        h.addWidget(title, 1)
        due = todo.get("due_date") or ""
        date_lbl = QLabel(_fmt_due_label(due))
        date_lbl.setObjectName("PickerDate")
        if due:
            try:
                d = datetime.strptime(due, "%Y-%m-%d").date()
            except ValueError:
                d = None
            if d is not None:
                today_d = datetime.now().date()
                if d < today_d:
                    date_lbl.setProperty("overdue", "true")
                elif d == today_d:
                    date_lbl.setProperty("today", "true")
        h.addWidget(date_lbl)
        row.mousePressEvent = lambda e, t=todo["title"]: self._pick(t)
        self._rows.append((row, lambda t=todo["title"]: self._pick(t)))
        return row

    def _make_fav_row(self, fav: dict) -> QWidget:
        """「常用专注」签里的一行：emoji + 名称 + 时长。点它 = 选这个专注并开始。"""
        row = QFrame()
        row.setObjectName("PickerRow")
        row.setFixedHeight(40)
        row.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 0, 10, 0)
        h.setSpacing(9)
        chip = QLabel(fav.get("emoji") or DEFAULT_EMOJI)
        chip.setFixedWidth(20)
        chip.setAlignment(Qt.AlignCenter)
        h.addWidget(chip)
        title = widgets.ElidedLabel(fav.get("name") or "未命名")
        title.setObjectName("PickerTitle")
        h.addWidget(title, 1)
        dur = ("正计时" if fav.get("mode") == "countup"
               else f"{fav.get('minutes', 25)}m")
        sub = QLabel(dur)
        sub.setObjectName("PickerDate")
        h.addWidget(sub)
        row.mousePressEvent = lambda e, f=fav: self._pick_fav(f)
        self._rows.append((row, lambda f=fav: self._pick_fav(f)))
        return row

    def _pick_fav(self, fav: dict) -> None:
        self.fav_picked.emit(fav)
        self.close()

    def _make_manage_row(self) -> QWidget:
        """常用专注签底部的「管理」入口 → 打开整页。"""
        box = QFrame()
        v = QVBoxLayout(box)
        v.setContentsMargins(6, 6, 6, 2)
        sep = QFrame()
        sep.setObjectName("FocusSep")
        sep.setFixedHeight(1)
        v.addWidget(sep)
        v.addSpacing(4)
        btn = QPushButton("☰  管理常用专注")
        btn.setObjectName("PickerAction")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(34)
        btn.clicked.connect(lambda: (self.manage_favs.emit(), self.close()))
        v.addWidget(btn)
        return box

    # -- 键盘选择 --------------------------------------------------------
    def _set_sel(self, idx: int) -> None:
        if not self._rows:
            self._sel = -1
            return
        self._sel = max(0, min(idx, len(self._rows) - 1))
        for i, (w, _fn) in enumerate(self._rows):
            w.setProperty("sel", "true" if i == self._sel else "false")
            w.style().unpolish(w)
            w.style().polish(w)
        self.scroll.ensureWidgetVisible(self._rows[self._sel][0], 0, 20)

    def _pick_current(self) -> None:
        if self._rows:
            i = self._sel if 0 <= self._sel < len(self._rows) else 0
            self._rows[i][1]()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self._set_sel(self._sel + (1 if key == Qt.Key.Key_Down else -1))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._pick_current()
                return True
        return super().eventFilter(obj, event)

    def _pick(self, title: str):
        self.picked.emit(title)
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.closed.emit()
        super().closeEvent(event)

    def _match_filter(self, t: dict, due) -> bool:
        """任务是否落在当前选中的智能清单 / 清单 / 标签里。"""
        f = self._filter
        today = datetime.now().date()
        if f == "all":
            return True
        if f.startswith("list:"):
            value = f[5:]
            if value == "inbox":
                return not t.get("list_id")
            return str(t.get("list_id") or "") == str(value)
        if f.startswith("tag:"):
            try:
                raw = t.get("tag_ids") or "[]"
                ids = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                ids = []
            return str(f[4:]) in [str(i) for i in ids]
        if due is None:
            return False
        if f == "today":
            return due <= today                      # 滴答的「今天」含已过期
        if f == "tomorrow":
            return due == today + timedelta(days=1)
        if f == "soon7":
            return today <= due <= today + timedelta(days=6)
        return True

    def _kind(self) -> str:
        return self._kinds[self._tabs.current_index()]

    def _refresh(self) -> None:
        """重建列表，并把键盘高亮放回第一条：搜索词一改，最该选的就是它。"""
        self._rows = []
        self.filter_hint.hide()
        # 日期筛选行只对「任务」签有意义，另两签没有日期维度
        self.filter_row.setVisible(self._kind() == "task")
        self._rebuild_rows()
        self._set_sel(0)
        self._show_filter_hint()

    def _show_filter_hint(self) -> None:
        # 只在「任务」签的浏览态提示：打字搜索本来就已经跨出筛选了，再说一遍是噪音。
        if (self._kind() != "task" or self.search.text().strip()
                or self._filter == "all"):
            return
        self.filter_hint.setText(
            "只列「%s」，输入关键词可搜全部任务" % self.date_btn.text())
        self.filter_hint.show()

    def _rebuild_rows(self):
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        keyword = self.search.text().strip().lower()
        kind = self._kind()
        if kind == "fav":                  # 常用专注
            favs = [f for f in _load_favorites()
                    if not int(f.get("archived") or 0)]
            if keyword:
                favs = [f for f in favs
                        if keyword in (f.get("name") or "").lower()]
            for fav in favs:
                self.list_layout.insertWidget(
                    self.list_layout.count() - 1, self._make_fav_row(fav))
            self.list_layout.insertWidget(
                self.list_layout.count() - 1, self._make_manage_row())
            if not favs:
                hint = QLabel("没有匹配的常用专注" if keyword
                              else "还没有常用专注，点下方「管理」去新建")
                hint.setObjectName("Muted")
                hint.setAlignment(Qt.AlignCenter)
                hint.setFixedHeight(56)
                self.list_layout.insertWidget(0, hint)
            return
        if kind == "habit":                # 习惯
            try:
                habit_names = [h["name"] for h in services.habit_list(archived=0)]
            except Exception:
                habit_names = []
            pool = habit_names or DEFAULT_HABITS
            items = [h for h in pool
                     if not keyword or keyword in h.lower()]
            if not items:
                hint = QLabel("无匹配习惯")
                hint.setObjectName("Muted")
                hint.setAlignment(Qt.AlignCenter)
                hint.setFixedHeight(80)
                self.list_layout.insertWidget(0, hint)
                return
            for name in items:
                self.list_layout.insertWidget(
                    self.list_layout.count() - 1,
                    self._make_row({"title": name, "due_date": "", "priority": 0}))
            return
        today = datetime.now().date()
        todos = [t for t in services.todo_list() if not t["done"]]
        groups = [("已过期", []), ("今天", []), ("最近 7 天", []), ("未来", [])]
        for t in todos:
            if keyword and keyword not in t["title"].lower():
                continue
            due_str = t.get("due_date") or ""
            try:
                due = datetime.strptime(due_str, "%Y-%m-%d").date()
            except ValueError:
                due = None
            # 一旦在打字搜索，就不再受「今天 / 某清单 / 某标签」那层筛选约束：
            # 人是按名字找那条任务的，名字对得上却因为排在后天而「暂无任务」，
            # 看起来像搜索坏了。空着搜索框时才按筛选浏览。
            if not keyword and not self._match_filter(t, due):
                continue
            if due is None or due < today:
                groups[0][1].append(t)
            elif due == today:
                groups[1][1].append(t)
            elif (due - today).days <= 7:
                groups[2][1].append(t)
            else:
                groups[3][1].append(t)
        inserted = False
        for group_name, items in groups:
            if not items:
                continue
            header = QLabel(group_name)
            header.setObjectName("PickerGroup")
            self.list_layout.insertWidget(self.list_layout.count() - 1, header)
            for t in items:
                self.list_layout.insertWidget(self.list_layout.count() - 1, self._make_row(t))
                inserted = True
        if not inserted:
            hint = QLabel("无匹配任务" if keyword else "暂无任务")
            hint.setObjectName("Muted")
            hint.setAlignment(Qt.AlignCenter)
            hint.setFixedHeight(80)
            self.list_layout.insertWidget(0, hint)


class DateTimePickerPopup(QFrame):
    """日期 + 时间选择弹层：月历网格 + 时:分 输入 + 确定/取消。

    参考设计是「月份 + 前后翻页」在顶行，星期表头一行，6×7 日期网格，
    下面是居中的时间输入，最后是确定/取消。选中日画成主色实心圆。
    """

    picked = Signal(object)   # datetime
    closed = Signal()

    WEEKDAYS = ("日", "一", "二", "三", "四", "五", "六")
    DAY_W = 32

    def __init__(self, value: datetime, parent: QWidget | None = None):
        super().__init__(parent, focus_ui.POPUP_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._sel = value.replace(second=0, microsecond=0)
        self._view = self._sel.replace(day=1)
        self._grid_start = self._view

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("CalPopup")
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(2)
        self.month_lbl = QLabel()
        self.month_lbl.setObjectName("CalMonth")
        head.addWidget(self.month_lbl)
        head.addStretch(1)
        for text, delta in (("‹", -1), ("›", 1)):
            btn = QPushButton(text)
            btn.setObjectName("CalNav")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, d=delta: self._shift_month(d))
            head.addWidget(btn)
        lay.addLayout(head)

        wk = QHBoxLayout()
        wk.setSpacing(2)
        for s in self.WEEKDAYS:
            l = QLabel(s)
            l.setObjectName("CalWeek")
            l.setAlignment(Qt.AlignCenter)
            l.setFixedWidth(self.DAY_W)
            wk.addWidget(l)
        lay.addLayout(wk)

        self.grid = QGridLayout()
        self.grid.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._days: list[QPushButton] = []
        for i in range(42):
            b = QPushButton()
            b.setObjectName("CalDay")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self._group.addButton(b, i)
            self.grid.addWidget(b, i // 7, i % 7)
            self._days.append(b)
        self._group.idClicked.connect(self._on_day)
        lay.addLayout(self.grid)

        self.time_edit = QLineEdit(self._sel.strftime("%H:%M"))
        self.time_edit.setObjectName("CalTime")
        self.time_edit.setAlignment(Qt.AlignCenter)
        self.time_edit.setMaxLength(5)
        self.time_edit.returnPressed.connect(self._confirm)
        lay.addWidget(self.time_edit)

        row = QHBoxLayout()
        row.setSpacing(8)
        ok = QPushButton("确定")
        ok.setObjectName("FocusPrimary")
        ok.setFixedSize(88, 32)
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self._confirm)
        cancel = QPushButton("取消")
        cancel.setObjectName("FocusGhost")
        cancel.setFixedSize(88, 32)
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.close)
        row.addWidget(ok)
        row.addWidget(cancel)
        lay.addLayout(row)

        self._render()

    def _shift_month(self, delta: int) -> None:
        m = self._view.month - 1 + delta
        self._view = self._view.replace(year=self._view.year + m // 12,
                                        month=m % 12 + 1, day=1)
        self._render()

    def _render(self) -> None:
        self.month_lbl.setText("%d月 %d年" % (self._view.month, self._view.year))
        # 周日排第一列：Python 里 weekday() 周一是 0
        lead = (self._view.weekday() + 1) % 7
        self._grid_start = self._view - timedelta(days=lead)
        for i, b in enumerate(self._days):
            d = (self._grid_start + timedelta(days=i)).date()
            b.setText(str(d.day))
            b.setProperty("muted", "true" if d.month != self._view.month else "false")
            b.style().unpolish(b)
            b.style().polish(b)
            b.setChecked(d == self._sel.date())

    def _on_day(self, idx: int) -> None:
        d = (self._grid_start + timedelta(days=idx)).date()
        self._sel = self._sel.replace(year=d.year, month=d.month, day=d.day)

    def _confirm(self) -> None:
        hh, mm = self._sel.hour, self._sel.minute
        raw = self.time_edit.text().strip().replace("：", ":")
        try:
            parts = [p for p in raw.split(":")]
            hh = max(0, min(23, int(parts[0])))
            if len(parts) > 1 and parts[1] != "":
                mm = max(0, min(59, int(parts[1])))
        except (ValueError, IndexError):
            pass
        self.picked.emit(self._sel.replace(hour=hh, minute=mm, second=0, microsecond=0))
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.closed.emit()
        super().closeEvent(event)


class TypePickerPopup(QFrame):
    """类型弹层：番茄计时 / 正计时 两个 tab + 对应数量输入 + 确定/取消。

    两个 tab 是同一份「时长」的两种输入法：
    番茄计时按「N 个番茄 × 单个番茄时长」，正计时直接按「H 小时 M 分钟」。
    """

    picked = Signal(int)   # 时长（分钟）
    closed = Signal()

    def __init__(self, minutes: int, focus_min: int,
                 parent: QWidget | None = None):
        super().__init__(parent, focus_ui.POPUP_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._focus_min = max(int(focus_min), 1)
        self._minutes = max(int(minutes), 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("CalPopup")
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)

        self.seg = focus_ui.SegmentedControl(["番茄计时", "正计时"], style="pill",
                                             height=30)
        self.seg.changed.connect(self._on_tab)
        seg_row = QHBoxLayout()
        seg_row.addStretch(1)
        seg_row.addWidget(self.seg)
        seg_row.addStretch(1)
        lay.addLayout(seg_row)

        spin_qss = ("QSpinBox { background:%s; border:none; border-radius:6px;"
                    " padding:5px 6px; font-size:13px; }" % theme.get("surface_hi"))

        def mk_spin(val: int, hi: int) -> QSpinBox:
            sp = widgets.disable_wheel(widgets.SpinBox())
            sp.setRange(0, hi)
            sp.setValue(val)
            sp.setFixedWidth(88)
            sp.setAlignment(Qt.AlignCenter)
            sp.setStyleSheet(spin_qss)
            sp.valueChanged.connect(self._on_spin)
            return sp

        # 番茄计时：N 个番茄
        self.tomato_page = QWidget()
        t_row = QHBoxLayout(self.tomato_page)
        t_row.setContentsMargins(0, 0, 0, 0)
        t_row.setSpacing(8)
        t_row.addStretch(1)
        self.tomato_spin = mk_spin(max(1, round(self._minutes / self._focus_min)), 24)
        t_row.addWidget(self.tomato_spin)
        t_row.addWidget(QLabel("个番茄"))
        t_row.addStretch(1)

        # 正计时：H 小时 M 分钟
        self.count_page = QWidget()
        c_row = QHBoxLayout(self.count_page)
        c_row.setContentsMargins(0, 0, 0, 0)
        c_row.setSpacing(8)
        c_row.addStretch(1)
        h, m = divmod(self._minutes, 60)
        self.hour_spin = mk_spin(h, 23)
        c_row.addWidget(self.hour_spin)
        c_row.addWidget(QLabel("小时"))
        self.min_spin = mk_spin(m, 59)
        c_row.addWidget(self.min_spin)
        c_row.addWidget(QLabel("分钟"))
        c_row.addStretch(1)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.tomato_page)
        self.stack.addWidget(self.count_page)
        self.stack.setFixedHeight(38)
        lay.addWidget(self.stack)

        row = QHBoxLayout()
        row.setSpacing(8)
        ok = QPushButton("确定")
        ok.setObjectName("FocusPrimary")
        ok.setFixedSize(88, 32)
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self._confirm)
        cancel = QPushButton("取消")
        cancel.setObjectName("FocusGhost")
        cancel.setFixedSize(88, 32)
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.close)
        row.addWidget(ok)
        row.addWidget(cancel)
        lay.addLayout(row)

        self._on_tab(0)

    def _on_tab(self, idx: int) -> None:
        self.stack.setCurrentIndex(1 if idx == 1 else 0)
        self.adjustSize()

    def _on_spin(self, _v: int) -> None:
        self._minutes = max(1, self._current_minutes())

    def _current_minutes(self) -> int:
        if self.stack.currentIndex() == 0:
            return max(1, self.tomato_spin.value()) * self._focus_min
        return self.hour_spin.value() * 60 + self.min_spin.value()

    def _confirm(self) -> None:
        self.picked.emit(max(1, self._current_minutes()))
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.closed.emit()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# 常用专注：数据 + emoji 选择 + 添加弹窗 + 选择弹窗
# ---------------------------------------------------------------------------
FAV_KEY = "pomodoro_favorites"
DEFAULT_EMOJI = "😊"

EMOJI_SET = [
    "😊", "📖", "💻", "📚", "🎓", "📝", "✏️", "📋", "🧠", "🎵",
    "🌙", "😴", "🌞", "☕", "🍚", "🥕", "🍭", "🧘", "🏃", "🚶",
    "🚴", "🏊", "🏋", "💪", "💰", "🛒", "🧹", "🎧", "🎬", "🎮",
    "🎹", "🎨", "❤️", "👍", "📅", "✅", "🀄", "🧊", "🍜", "🏃‍♀️",
]


def _load_favorites() -> list[dict]:
    try:
        data = json.loads(db.get_setting(FAV_KEY, "[]"))
        data = data if isinstance(data, list) else []
    except (ValueError, TypeError):
        data = []
    # 老数据可能缺 id（删除要按 id，之前按「name + emoji」过滤会把同名同表情的
    # 两条一起删掉）或缺 archived（坚持中/已归档分栏要用）。缺哪个补哪个并写回。
    dirty = False
    for f in data:
        if not isinstance(f, dict):
            continue
        if not f.get("id"):
            f["id"] = uuid.uuid4().hex
            dirty = True
        if "archived" not in f:
            f["archived"] = 0
            dirty = True
    if dirty:
        _save_favorites(data)
    return data


def _set_fav_archived(fav_id: str, archived: int) -> None:
    """归档 / 取消归档一个常用专注（按 id）。"""
    favs = _load_favorites()
    for f in favs:
        if f.get("id") == fav_id:
            f["archived"] = 1 if archived else 0
    _save_favorites(favs)


def _save_favorites(favs: list[dict]) -> None:
    for f in favs:
        if isinstance(f, dict) and not f.get("id"):
            f["id"] = uuid.uuid4().hex
    db.set_setting(FAV_KEY, json.dumps(favs, ensure_ascii=False))


class EmojiPicker(QDialog):
    """emoji 网格选择弹窗。"""

    chosen = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, focus_ui.POPUP_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(368, 240)
        card = QFrame(self)
        card.setObjectName("PickerCard")
        card.setGeometry(self.rect())
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        for i, emo in enumerate(EMOJI_SET):
            btn = QPushButton(emo)
            btn.setObjectName("EmojiCell")
            btn.setFixedSize(32, 32)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda e=emo: (self.chosen.emit(e), self.close()))
            grid.addWidget(btn, i // 10, i % 10)
        scroll.setWidget(content)
        lay.addWidget(scroll)


class AddFocusDialog(QDialog):
    """「添加/编辑常用专注」：emoji 头像 + 名称 + 计时模式，可关联待办任务。

    传 ``fav`` 就是编辑：各字段预填，保存时保留原 id 与 archived（否则一改就把
    它当新建、id 变了，历史记录的 fav_id 就接不上了）。
    """

    def __init__(self, page: "PomodoroPage", fav: dict | None = None):
        super().__init__(page)
        self.page = page
        self._fav = fav or {}
        self.task = self._fav.get("task", "")
        self.result_fav: dict | None = None
        self.emoji = self._fav.get("emoji") or DEFAULT_EMOJI
        editing = bool(fav)
        self.setWindowTitle("编辑常用专注" if editing else "添加常用专注")
        self.setFixedWidth(440)
        self.setStyleSheet("QDialog { background: %s; }" % theme.get("bg_alt"))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(16)

        title = QLabel("编辑常用专注" if editing else "添加常用专注")
        title.setAlignment(Qt.AlignCenter)
        # 参考图的标题是常规字重的近黑色，不是各设置页那种粗体
        title.setStyleSheet(
            "background: transparent; color: %s; font-size: 16px;"
            % theme.get("text_hi"))
        outer.addWidget(title)

        # 头像 + 名称（名称框右侧内嵌链接图标 → 关联待办任务）
        name_row = QHBoxLayout()
        name_row.setSpacing(12)
        avatar = QFrame()
        avatar.setObjectName("FocusAvatar")
        avatar.setFixedSize(48, 48)
        avatar.setCursor(Qt.PointingHandCursor)
        avatar.setToolTip("更换图标")
        av_lay = QVBoxLayout(avatar)
        av_lay.setContentsMargins(0, 0, 0, 0)
        self.avatar_emoji = QLabel(DEFAULT_EMOJI)
        self.avatar_emoji.setObjectName("FocusAvatarEmoji")
        self.avatar_emoji.setAlignment(Qt.AlignCenter)
        av_lay.addWidget(self.avatar_emoji)
        avatar.mousePressEvent = lambda e: self._pick_emoji()
        name_row.addWidget(avatar)

        self.input_box = QFrame()
        self.input_box.setObjectName("FocusInputBox")
        self.input_box.setFixedHeight(48)
        box = QHBoxLayout(self.input_box)
        box.setContentsMargins(12, 0, 8, 0)
        box.setSpacing(6)
        self.name_input = QLineEdit()
        self.name_input.setObjectName("FocusNameInput")
        self.name_input.setPlaceholderText("名称")
        self.name_input.textChanged.connect(lambda _: self._sync_save())
        box.addWidget(self.name_input, 1)
        link_lbl = QLabel("🔗")
        link_lbl.setObjectName("FocusInputAction")
        link_lbl.setCursor(Qt.PointingHandCursor)
        link_lbl.setToolTip("关联待办任务")
        link_lbl.mousePressEvent = lambda e: self._pick_task()
        box.addWidget(link_lbl)
        name_row.addWidget(self.input_box, 1)
        outer.addLayout(name_row)

        # 计时模式
        mode_lbl = QLabel("计时模式")
        mode_lbl.setStyleSheet(
            "background: transparent; color: %s; font-size: 13.5px;"
            % theme.get("text"))
        outer.addWidget(mode_lbl)

        self.rb_pomo = focus_ui.RoundRadio("番茄计时")
        self.rb_pomo.setChecked(True)
        self.rb_count = focus_ui.RoundRadio("正计时")
        self.minutes_spin = widgets.disable_wheel(widgets.SpinBox())
        self.minutes_spin.setRange(1, 600)
        self.minutes_spin.setValue(page._work_min)
        self.minutes_spin.setFixedWidth(66)
        self.minutes_spin.setAlignment(Qt.AlignCenter)
        # 参考图里这就一个纯灰底数字框，没有上下调节箭头
        self.minutes_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.minutes_spin.setStyleSheet(
            "QSpinBox { background:%s; border:none; border-radius:6px;"
            " padding:5px 8px; font-size:13px; color:%s; }"
            % (focus_ui.tile_bg().name(), theme.get("text_hi")))
        pomo_row = QHBoxLayout()
        pomo_row.setSpacing(10)
        pomo_row.addWidget(self.rb_pomo)
        pomo_row.addSpacing(6)
        pomo_row.addWidget(self.minutes_spin)
        self.unit_lbl = QLabel("分钟")
        pomo_row.addWidget(self.unit_lbl)
        pomo_row.addStretch(1)
        outer.addLayout(pomo_row)

        count_row = QHBoxLayout()
        count_row.setSpacing(10)
        count_row.addWidget(self.rb_count)
        count_row.addStretch(1)
        outer.addLayout(count_row)

        self.task_tip = QLabel(" ")
        self.task_tip.setObjectName("FocusMuted")
        outer.addWidget(self.task_tip)

        outer.addSpacing(6)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch(1)
        self.save_btn = QPushButton("保存")
        self.save_btn.setObjectName("FocusPrimary")
        self.save_btn.setFixedSize(92, 36)
        self.save_btn.setEnabled(False)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.setObjectName("FocusGhost")
        cancel.setFixedSize(92, 36)
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.save_btn)
        btn_row.addWidget(cancel)
        outer.addLayout(btn_row)
        if self._fav:
            self._prefill()
        self._sync_save()
        self.rb_pomo.toggled.connect(self._sync_minutes)
        self._sync_minutes(self.rb_pomo.isChecked())   # 编辑正计时时要立刻灰掉
        self.name_input.returnPressed.connect(
            lambda: self._save() if self.save_btn.isEnabled() else None)
        focus_ui.fade_in(self, 140)

    def _sync_minutes(self, pomo: bool) -> None:
        """选了正计时，那个「分钟」框就没有意义了 —— 灰掉，别让它看着还能填。"""
        self.minutes_spin.setEnabled(pomo)
        self.unit_lbl.setEnabled(pomo)

    def _prefill(self):
        f = self._fav
        self.avatar_emoji.setText(f.get("emoji") or DEFAULT_EMOJI)
        self.name_input.setText(f.get("name", ""))
        if f.get("mode") == "countup":
            self.rb_count.setChecked(True)
        else:
            self.rb_pomo.setChecked(True)
        self.minutes_spin.setValue(int(f.get("minutes") or 25))
        if self.task:
            self.task_tip.setText(f"已关联任务：{self.task}")

    def _sync_save(self):
        self.save_btn.setEnabled(bool(self.name_input.text().strip()))

    def _pick_emoji(self):
        picker = EmojiPicker(self)
        picker.chosen.connect(self.avatar_emoji.setText)
        pos = self.input_box.mapToGlobal(QPoint(0, self.input_box.height() + 4))
        picker.move(pos)
        picker.show()

    def _pick_task(self):
        popup = TaskPickerPopup(self.page, self)
        popup.picked.connect(self._on_task_picked)
        popup.setAttribute(Qt.WA_DeleteOnClose, True)
        popups.place_popup(popup, self.input_box)
        popup.show()

    def _on_task_picked(self, title: str):
        self.task = title
        if not self.name_input.text().strip():
            self.name_input.setText(title)
            self._sync_save()
        self.task_tip.setText(f"已关联任务：{title}")

    def _save(self):
        name = self.name_input.text().strip()
        if not name:
            return
        self.result_fav = {
            "id": self._fav.get("id") or uuid.uuid4().hex,
            "archived": int(self._fav.get("archived") or 0),
            "name": name,
            "emoji": self.avatar_emoji.text(),
            "mode": "pomodoro" if self.rb_pomo.isChecked() else "countup",
            "minutes": self.minutes_spin.value(),
            "task": self.task,
        }
        self.accept()


# ---------------------------------------------------------------------------
# 设置弹窗
# ---------------------------------------------------------------------------
class SettingsDialog(QDialog):
    """专注设置弹窗：计时选项 / 自动选项 / 提示音 / Mini 模式。"""

    def __init__(self, page: "PomodoroPage"):
        super().__init__(page)
        self.page = page
        self.setWindowTitle("专注设置")
        self.setFixedSize(460, 640)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 标题行
        header = QHBoxLayout()
        header.setContentsMargins(20, 16, 14, 12)
        title = QLabel("专注设置")
        title.setObjectName("SettingsTitle")
        header.addWidget(title)
        header.addStretch(1)
        close_btn = QPushButton("✕")
        close_btn.setObjectName("PickerClose")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        header.addWidget(close_btn)
        outer.addLayout(header)

        sep = QFrame()
        sep.setObjectName("SettingsSep")
        sep.setFixedHeight(1)
        outer.addWidget(sep)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(18, 14, 18, 18)
        lay.setSpacing(6)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # ---- 计时选项 ----
        card = self._card(lay, "计时选项")
        self.work_spin = self._add_spin_row(card, "番茄时长", "分钟", 1, 180,
                                            _s_int("pomodoro_work", 25), "pomodoro_work")
        self.short_spin = self._add_spin_row(card, "短休息时长", "分钟", 1, 60,
                                             _s_int("pomodoro_short", 5), "pomodoro_short")
        self.long_spin = self._add_spin_row(card, "长休息时长", "分钟", 1, 120,
                                            _s_int("pomodoro_long", 15), "pomodoro_long")
        self.interval_spin = self._add_spin_row(card, "长休息间隔番茄数", "个", 1, 12,
                                                _s_int("pomodoro_interval", 4), "pomodoro_interval")

        # ---- 自动选项 ----
        card = self._card(lay, "自动选项")
        self.auto_next = self._add_toggle_row(card, "自动开始下个番茄",
                                              _s_int("pomodoro_auto_next", 1), "pomodoro_auto_next")
        self.auto_rest = self._add_toggle_row(card, "自动休息",
                                              _s_int("pomodoro_auto_rest", 1), "pomodoro_auto_rest")
        self.auto_count = self._add_spin_row(card, "自动番茄专注次数", "次", 1, 20,
                                             _s_int("pomodoro_auto_count", 4), "pomodoro_auto_count")

        # ---- 番茄提示音 ----
        # 这两个下拉和设置页「音效」里的番茄结束 / 休息结束是同一份配置
        # （共用 pomodoro_ring_work / pomodoro_ring_rest 两个 db key），
        # 所以回显要走 sounds 的实时解析，不能写死旧默认值。
        card = self._card(lay, "番茄提示音")
        self.ring_work = self._add_combo_row(
            card, "番茄结束铃声", sounds.RING_NAMES,
            sounds.event_clip_label("pomodoro_work_end"), "pomodoro_ring_work")
        self.ring_rest = self._add_combo_row(
            card, "休息结束铃声", sounds.RING_NAMES,
            sounds.event_clip_label("pomodoro_rest_end"), "pomodoro_ring_rest")

        # ---- Mini 模式 ----
        card = self._card(lay, "Mini 模式")
        style_lbl = QLabel("样式")
        style_lbl.setStyleSheet("font-size:13px;")
        style_row = QHBoxLayout()
        style_row.setContentsMargins(0, 10, 0, 6)
        style_row.addWidget(style_lbl)
        style_row.addStretch(1)
        for i in range(3):
            thumb = _MiniThumb(i, _s_int("pomodoro_mini_style", 0) == i)
            thumb.clicked.connect(lambda idx=i, b=thumb: self._select_mini_style(idx, b))
            style_row.addWidget(thumb)
        style_row.addStretch(1)
        card.addLayout(style_row)
        self._mini_thumbs: list[_MiniThumb] = [style_row.itemAt(i + 2).widget() for i in range(3)]

        self.mini_theme = self._add_combo_row(card, "主题", ["深色", "浅色"],
                                              _s("pomodoro_mini_theme", "深色"), "pomodoro_mini_theme")
        op_row, self.mini_opacity = self._add_slider_row(card, "不透明度", 20, 100,
                                                         _s_int("pomodoro_mini_opacity", 90), "pomodoro_mini_opacity")
        self.mini_auto = self._add_toggle_row(card, "专注时自动开启",
                                              _s_int("pomodoro_mini_auto", 1), "pomodoro_mini_auto")
        hotkey_row = QHBoxLayout()
        hotkey_row.setContentsMargins(0, 10, 0, 4)
        hotkey_row.addWidget(QLabel("开启/关闭专注 Mini 窗口"))
        hotkey_row.addStretch(1)
        self.hotkey_btn = QPushButton(_s("pomodoro_mini_hotkey", "") or "设置快捷键")
        self.hotkey_btn.setObjectName("GhostBtn")
        self.hotkey_btn.clicked.connect(self._set_hotkey)
        hotkey_row.addWidget(self.hotkey_btn)
        clear_btn = QPushButton("清除")
        clear_btn.setObjectName("GhostBtn")
        clear_btn.clicked.connect(self._clear_hotkey)
        hotkey_row.addWidget(clear_btn)
        self.hotkey_clear = clear_btn
        card.addLayout(hotkey_row)
        self.hotkey_hint = QLabel()
        self.hotkey_hint.setContentsMargins(0, 0, 0, 8)
        self.hotkey_hint.setStyleSheet("font-size:12px; color:%s;" % theme.get("muted"))
        card.addWidget(self.hotkey_hint)

        lay.addStretch(1)
        self._refresh_hotkey_btn()

    # ---- 构建辅助 ----
    @staticmethod
    def _section_title(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("SettingsSection")
        return lbl

    def _card(self, parent: QVBoxLayout, title: str) -> QVBoxLayout:
        """插入分区标题 + 圆角卡片，返回卡片内部布局。"""
        parent.addWidget(self._section_title(title))
        box = QFrame()
        box.setObjectName("SettingsCard")
        inner = QVBoxLayout(box)
        inner.setContentsMargins(16, 4, 16, 4)
        inner.setSpacing(0)
        parent.addWidget(box)
        return inner

    def _add_spin_row(self, parent: QVBoxLayout, label: str, unit: str,
                      lo: int, hi: int, value: int, key: str) -> QSpinBox:
        row = QHBoxLayout()
        row.setContentsMargins(0, 10, 0, 10)
        lbl = QLabel(label)
        lbl.setStyleSheet("font-size:13px;")
        row.addWidget(lbl)
        row.addStretch(1)
        spin = widgets.disable_wheel(widgets.SpinBox())
        spin.setRange(lo, hi)
        spin.setValue(value)
        spin.setFixedWidth(78)
        spin.setFixedHeight(30)
        spin.valueChanged.connect(lambda v, k=key: self._save(k, str(v)))
        row.addWidget(spin)
        unit_lbl = QLabel(unit)
        unit_lbl.setObjectName("Muted")
        row.addWidget(unit_lbl)
        parent.addLayout(row)
        return spin

    def _add_toggle_row(self, parent: QVBoxLayout, label: str, checked: int, key: str) -> Toggle:
        row = QHBoxLayout()
        row.setContentsMargins(0, 10, 0, 10)
        lbl = QLabel(label)
        lbl.setStyleSheet("font-size:13px;")
        row.addWidget(lbl)
        row.addStretch(1)
        toggle = Toggle(bool(checked))
        toggle.toggled.connect(lambda v, k=key: self._save(k, "1" if v else "0"))
        row.addWidget(toggle)
        parent.addLayout(row)
        return toggle

    def _add_combo_row(self, parent: QVBoxLayout, label: str,
                       items: list[str], value: str, key: str) -> QComboBox:
        row = QHBoxLayout()
        row.setContentsMargins(0, 10, 0, 10)
        lbl = QLabel(label)
        lbl.setStyleSheet("font-size:13px;")
        row.addWidget(lbl)
        row.addStretch(1)
        combo = widgets.disable_wheel(widgets.ComboBox())
        combo.setObjectName("TextCombo")
        combo.addItems(items)
        if value in items:
            combo.setCurrentText(value)
        combo.currentTextChanged.connect(lambda v, k=key: self._save(k, v))
        row.addWidget(combo)
        parent.addLayout(row)
        return combo

    def _add_slider_row(self, parent: QVBoxLayout, label: str, lo: int, hi: int,
                        value: int, key: str):
        row = QHBoxLayout()
        row.setContentsMargins(0, 10, 0, 10)
        lbl = QLabel(label)
        lbl.setStyleSheet("font-size:13px;")
        row.addWidget(lbl)
        row.addStretch(1)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(value)
        slider.setFixedWidth(170)
        val_lbl = QLabel(f"{value}%")
        val_lbl.setObjectName("Muted")
        slider.valueChanged.connect(lambda v, l=val_lbl: l.setText(f"{v}%"))
        slider.valueChanged.connect(lambda v, k=key: self._save(k, str(v)))
        row.addWidget(slider)
        row.addWidget(val_lbl)
        parent.addLayout(row)
        return row, slider

    def _save(self, key: str, value: str):
        db.set_setting(key, value)
        self.page._on_setting_changed(key, value)

    def _select_mini_style(self, idx: int, _btn):
        for t in self._mini_thumbs:
            t.set_selected(False)
        _btn.set_selected(True)
        self._save("pomodoro_mini_style", str(idx))
        self.page._mini_style = idx
        if self.page._mini:
            self.page._mini.set_style(idx)

    def _refresh_hotkey_btn(self):
        hk = _s("pomodoro_mini_hotkey", "")
        self.hotkey_btn.setText(hk if hk else "设置快捷键")
        self.hotkey_clear.setVisible(bool(hk))
        self.hotkey_hint.setText(
            "留空即不使用快捷键。" if not hk else
            ("全局生效：主窗口没焦点、缩到托盘时也能按。" if self.page._hotkey_global
             else "仅本程序窗口内生效：该组合键无法做系统级注册（不支持或被占用）。"))

    def _clear_hotkey(self):
        if not _s("pomodoro_mini_hotkey", ""):
            return
        self._save("pomodoro_mini_hotkey", "")
        self.page._mini_hotkey = ""
        self._refresh_hotkey_btn()

    def _set_hotkey(self):
        dlg = _HotkeyDialog(self, _s("pomodoro_mini_hotkey", ""))
        if dlg.exec() == QDialog.Accepted:
            seq = dlg.sequence()
            if seq:
                self._save("pomodoro_mini_hotkey", seq)
                self.page._mini_hotkey = seq
                self._refresh_hotkey_btn()


class _MiniThumb(QFrame):
    """Mini 模式样式缩略图。"""
    clicked = Signal()

    def __init__(self, index: int, selected: bool):
        super().__init__()
        self._index = index
        self._selected = selected
        self.setFixedSize(90, 56)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("MiniThumb" + (" MiniThumbSel" if selected else ""))

    def set_selected(self, sel: bool):
        self._selected = sel
        self.setObjectName("MiniThumb" + (" MiniThumbSel" if sel else ""))
        self.setStyleSheet(self.styleSheet())  # 刷新样式
        self.update()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        colors = [("#2c2c3e", "卡片"), ("#3a3a52", "圆环"), ("#4a4a68", "简洁")]
        bg, _ = colors[self._index]
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(bg))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)
        # 缩略图内的简笔预览
        w, hgt = self.width(), self.height()
        inner = QColor("#8a8aa8")
        p.setBrush(inner)
        if self._index == 0:
            p.drawRoundedRect(QRectF(w * 0.16, hgt * 0.3, w * 0.68, hgt * 0.4), 4, 4)
        elif self._index == 1:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(inner, 2))
            p.drawEllipse(QRectF(w * 0.3, hgt * 0.22, w * 0.4, hgt * 0.56))
        else:
            p.drawRoundedRect(QRectF(w * 0.22, hgt * 0.38, w * 0.56, hgt * 0.24), 3, 3)
        if self._selected:
            # 右下角蓝色对勾圆标
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.get("accent")))
            p.drawEllipse(QRectF(w - 16, hgt - 16, 12, 12))
            p.setPen(QPen(QColor("white"), 1.6))
            p.drawLine(QPoint(int(w - 13), int(hgt - 10)), QPoint(int(w - 11), int(hgt - 8)))
            p.drawLine(QPoint(int(w - 11), int(hgt - 8)), QPoint(int(w - 7), int(hgt - 13)))
        else:
            # 左上角黄色三角
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#f6b100"))
            p.drawPolygon(QPolygonF([QPointF(1, 1), QPointF(14, 1), QPointF(1, 14)]))


# ---------------------------------------------------------------------------
# 全局快捷键
# ---------------------------------------------------------------------------
# 为什么不能只用 QShortcut：Mini 窗口是 Qt.Tool，点了不会抢焦点，主窗口还可能
# 缩在托盘里 —— 这两种情况下 Qt 的快捷键都收不到按键。只有 RegisterHotKey
# 注册到系统，才真的"任何时候按都响"。
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000        # Win7+：长按同一个键不连发
_WM_HOTKEY = 0x0312
_HOTKEY_ID = 0xACC1           # 只给我们自己的窗口用，不与 Qt 内部的 id 段重叠

# Qt 键值 → Windows 虚拟键码。只列能一一对上的；对不上的（标点、多媒体键）
# 不硬猜，退回 QShortcut。
_VK_BY_QT_KEY: dict[int, int] = {}
for _i in range(24):                                  # F1..F24 = 0x70..
    _VK_BY_QT_KEY[int(Qt.Key.Key_F1) + _i] = 0x70 + _i
for _i in range(26):                                  # A..Z 与 ASCII 同码
    _VK_BY_QT_KEY[int(Qt.Key.Key_A) + _i] = 0x41 + _i
for _i in range(10):                                  # 0..9 同上
    _VK_BY_QT_KEY[int(Qt.Key.Key_0) + _i] = 0x30 + _i
_VK_BY_QT_KEY.update({
    int(Qt.Key.Key_Space): 0x20,
    int(Qt.Key.Key_Return): 0x0D,
    int(Qt.Key.Key_Enter): 0x0D,
    int(Qt.Key.Key_Escape): 0x1B,
    int(Qt.Key.Key_Home): 0x24,
    int(Qt.Key.Key_End): 0x23,
    int(Qt.Key.Key_Left): 0x25,
    int(Qt.Key.Key_Up): 0x26,
    int(Qt.Key.Key_Right): 0x27,
    int(Qt.Key.Key_Down): 0x28,
    int(Qt.Key.Key_Insert): 0x2D,
    int(Qt.Key.Key_Delete): 0x2E,
})


def _hotkey_native(seq: str):
    """把 "Ctrl+Alt+P" 翻成 RegisterHotKey 要的 (fsModifiers, vk)。

    不支持时返回 None：非 Windows、组合键里有对不上虚拟键码的键、
    或者压根没修饰键（不带修饰键的全局注册会把那个键在整个系统里吞掉，
    打字都打不出来，绝不能这么干）。
    """
    keysequence = QKeySequence(seq)
    if keysequence.count() != 1:
        return None
    combo = keysequence[0]
    vk = _VK_BY_QT_KEY.get(int(combo.key()))
    if vk is None:
        return None
    mods = combo.keyboardModifiers()
    flags = 0
    if mods & Qt.KeyboardModifier.ControlModifier:
        flags |= _MOD_CONTROL
    if mods & Qt.KeyboardModifier.AltModifier:
        flags |= _MOD_ALT
    if mods & Qt.KeyboardModifier.ShiftModifier:
        flags |= _MOD_SHIFT
    if mods & Qt.KeyboardModifier.MetaModifier:
        flags |= _MOD_WIN
    if not flags:
        return None
    return flags | _MOD_NOREPEAT, vk


class _HotkeyFilter(QAbstractNativeEventFilter):
    """从 Qt 的原生消息循环里截 WM_HOTKEY。"""

    def __init__(self, owner: "_GlobalHotkey"):
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, event_type, message):
        if not self._owner.hwnd or event_type != b"windows_generic_MSG":
            return False, 0
        try:
            from ctypes import wintypes
            msg = wintypes.MSG.from_address(int(message))
        except (TypeError, ValueError, OSError):
            return False, 0
        if (msg.message != _WM_HOTKEY or int(msg.wParam or 0) != _HOTKEY_ID
                or msg.hWnd != self._owner.hwnd):
            return False, 0
        if self._owner.callback is not None:
            # 窗口过程里直接 show 一个顶层窗口会重入，扔回 Qt 事件循环
            QTimer.singleShot(0, self._owner.callback)
        return True, 0


class _GlobalHotkey:
    """一个系统级快捷键的持有者（整个程序只用这一个，不做什么注册表）。"""

    def __init__(self):
        self.hwnd: int = 0
        self.callback = None
        self._user32 = None
        self._filter: _HotkeyFilter | None = None

    def _load_user32(self):
        if self._user32 is None and sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_uint,
                                              ctypes.c_uint, ctypes.c_uint]
            user32.RegisterHotKey.restype = ctypes.c_bool
            user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_uint]
            user32.UnregisterHotKey.restype = ctypes.c_bool
            self._user32 = user32
        return self._user32

    def bind(self, window: QWidget, seq: str, callback) -> bool:
        """注册成功返回 True；False 表示调用方该退回 QShortcut。"""
        self.unbind()
        user32 = self._load_user32()
        app = QApplication.instance()
        if user32 is None or app is None or not seq:
            return False
        parsed = _hotkey_native(seq)
        if parsed is None:
            return False
        fs_mods, vk = parsed
        hwnd = int(window.winId())
        if not user32.RegisterHotKey(hwnd, _HOTKEY_ID, fs_mods, vk):
            return False       # 多半是被别的程序占走了
        self.hwnd = hwnd
        self.callback = callback
        self._filter = _HotkeyFilter(self)
        app.installNativeEventFilter(self._filter)
        return True

    def unbind(self):
        user32, hwnd = self._user32, self.hwnd
        self.hwnd = 0
        self.callback = None
        if self._filter is not None:
            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self._filter)
            self._filter = None
        if user32 is not None and hwnd:
            user32.UnregisterHotKey(hwnd, _HOTKEY_ID)


_GLOBAL_HOTKEY = _GlobalHotkey()


class _HotkeyDialog(QDialog):
    """快捷键设置弹窗。"""

    def __init__(self, parent: QWidget, current: str):
        super().__init__(parent)
        self.setWindowTitle("设置快捷键")
        self.setFixedWidth(320)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.addWidget(QLabel("请按下快捷键组合："))
        self._edit = QLineEdit(current)
        self._edit.setPlaceholderText("例如 Ctrl+Alt+P")
        self._edit.setReadOnly(True)
        self._edit.setFocus()
        lay.addWidget(self._edit)
        row = QHBoxLayout()
        ok = QPushButton("确定")
        ok.setObjectName("Primary")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(ok)
        row.addWidget(cancel)
        lay.addLayout(row)
        self._seq: str = current

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta):
            return
        mods = event.modifiers()
        seq = []
        if mods & Qt.ControlModifier:
            seq.append("Ctrl")
        if mods & Qt.AltModifier:
            seq.append("Alt")
        if mods & Qt.ShiftModifier:
            seq.append("Shift")
        seq.append(QKeySequence(key).toString())
        self._seq = "+".join(seq)
        self._edit.setText(self._seq)

    def sequence(self) -> str:
        return self._seq


# ---------------------------------------------------------------------------
# Mini 悬浮窗
# ---------------------------------------------------------------------------
MINI_MENU_QSS = """
QMenu { background:#262636; border:1px solid #3a3a52; border-radius:10px; padding:6px; }
QMenu::item { color:#e8e8f2; padding:8px 30px 8px 14px; border-radius:6px;
              font-size:13px; background:transparent; }
QMenu::item:selected { background:#3a3a52; }
QMenu::item:checked { color:#7a8dff; }
QMenu::separator { height:1px; background:#3a3a52; margin:5px 8px; }
"""

STAT_MODES = [
    ("today_focus", "今日专注"),
    ("today_count", "今日番茄"),
    ("week_focus", "本周专注"),
    ("week_count", "本周番茄"),
]


class _MiniMinutesDialog(QDialog):
    """「修改番茄时长」小弹窗（深色，跟随 Mini 风格）。"""

    def __init__(self, mini: "MiniWindow"):
        super().__init__(mini, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(250, 150)
        card = QFrame(self)
        card.setGeometry(self.rect())
        card.setStyleSheet(
            "QFrame { background:#262636; border-radius:12px; }"
            "QLabel { color:#e8e8f2; background:transparent; border:none; font-size:13px; }"
            "QSpinBox { background:#1c1c2c; border:1.5px solid #4c6fff; border-radius:6px;"
            " color:white; font-size:14px; padding:4px 8px; }")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.spin = QSpinBox()
        self.spin.setRange(5, 180)
        self.spin.setValue(mini.page._work_min)
        self.spin.setFixedWidth(74)
        self.spin.setFixedHeight(32)
        self.spin.setAlignment(Qt.AlignCenter)
        row.addWidget(self.spin)
        row.addWidget(QLabel("分钟"))
        row.addStretch(1)
        lay.addLayout(row)
        tip = QLabel("番茄时长可选范围：5~180分钟")
        tip.setStyleSheet("color:#8a8aa0; font-size:11px;")
        lay.addWidget(tip)
        btns = QHBoxLayout()
        ok = QPushButton("确定")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setStyleSheet("QPushButton { background:#4c6fff; color:white; border:none;"
                         " border-radius:8px; padding:7px 26px; font-size:13px; }"
                         "QPushButton:hover { background:#5d7dff; }")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setStyleSheet("QPushButton { background:transparent; color:#cfcfdd;"
                             " border:1px solid #4a4a62; border-radius:8px;"
                             " padding:7px 26px; font-size:13px; }"
                             "QPushButton:hover { border-color:#8a8aa0; }")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)

    def value(self) -> int:
        return self.spin.value()


class MiniWindow(QWidget):
    """Mini 悬浮窗：播放/暂停 + 任务 + 大计时 + 统计区；右键菜单
    （专注于 / 修改计时 / Mini 样式 / 置顶 / 白噪音 / 沉浸模式 / 主窗口 / 关闭）；
    贴近屏幕边缘自动缩起成把手，悬停展开。"""

    SNAP_PX = 32  # 距边缘多少像素内触发贴边

    def __init__(self, page: "PomodoroPage"):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.page = page
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._style = _s_int("pomodoro_mini_style", 0)
        self._opacity = _s_int("pomodoro_mini_opacity", 90) / 100
        self.setWindowOpacity(self._opacity)
        self._pinned = True
        self._docked: str | None = None  # None / "left" / "right" / "top" / "bottom"
        self._size = (216, 148)
        self._stat_mode = "today_focus"
        self._noise_kind: str | None = None
        self._stats = {"today_focus": 0, "today_count": 0,
                       "week_focus": 0, "week_count": 0}
        # 主题色缓存下来：以前 paintEvent 每次重绘都 _s() 查一次库，
        # 而重绘是跟着倒计时每秒好几次的。
        self._dark = _s("pomodoro_mini_theme", "深色") == "深色"
        self._placed = False
        self.set_style(self._style)
        self._drag_pos: QPoint | None = None
        self._press_global: QPoint | None = None
        self._moved = False
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(400)
        # 只负责重绘（Mini 上要走倒计时和进度环）。统计值改成事件驱动，
        # 由 PomodoroPage._refresh_stats() 调 refresh_stats() 推过来 ——
        # 之前这里每 400ms 打 3 条 SQL，而今日/本周总量只在记录增删时才变。
        self._sync_timer.timeout.connect(self.update)
        self.refresh_stats()

    # ---- 统计数据 ----
    def refresh_stats(self):
        try:
            week = services.pomodoro_week_stats()
            self._stats = {
                "today_focus": services.pomodoro_total_minutes_today(),
                "today_count": services.pomodoro_today_count(),
                "week_focus": week["minutes"],
                "week_count": week["count"],
            }
        except Exception:  # noqa: BLE001
            pass
        self.update()

    def reload_theme(self):
        """设置里改了 Mini 主题后重新取一次缓存值。"""
        self._dark = _s("pomodoro_mini_theme", "深色") == "深色"
        self.update()

    @staticmethod
    def _fmt_stat(key: str, v: int) -> str:
        v = int(v or 0)
        if key.endswith("_count"):
            return str(v)
        if v >= 60:
            h, m = divmod(v, 60)
            return f"{h}h{m}m" if m else f"{h}h"
        return f"{v}m"

    # ---- 贴边缩起 ----
    def set_style(self, idx: int):
        self._style = idx
        self._size = [(216, 148), (110, 110), (150, 46)][idx]
        if not self._docked:
            self.setFixedSize(*self._size)
        self.update()

    def _screen_rect(self):
        scr = QApplication.screenAt(self.geometry().center())
        if scr is None:
            scr = QApplication.primaryScreen()
        return scr.availableGeometry()

    def _maybe_dock(self):
        """拖拽结束后判断是否贴边缩起。"""
        if self._docked:
            return
        g = self.geometry()
        r = self._screen_rect()
        edge = None
        if g.left() <= r.left() + self.SNAP_PX:
            edge = "left"
        elif g.right() >= r.right() - self.SNAP_PX:
            edge = "right"
        elif g.top() <= r.top() + self.SNAP_PX:
            edge = "top"
        elif g.bottom() >= r.bottom() - self.SNAP_PX:
            edge = "bottom"
        if edge:
            self._dock(edge)

    def _dock(self, edge: str):
        self._docked = edge
        r = self._screen_rect()
        c = self.geometry().center()
        if edge in ("left", "right"):
            self.setFixedSize(10, 64)
            y = max(r.top() + 4, min(c.y() - 32, r.bottom() - 68))
            self.move(r.left() if edge == "left" else r.right() - 9, y)
        else:
            self.setFixedSize(64, 10)
            x = max(r.left() + 4, min(c.x() - 32, r.right() - 68))
            self.move(x, r.top() if edge == "top" else r.bottom() - 9)
        self.update()

    def _undock(self):
        edge, self._docked = self._docked, None
        if not edge:
            return
        w, h = self._size
        self.setFixedSize(w, h)
        r = self._screen_rect()
        c = self.geometry().center()
        x = max(r.left() + 2, min(c.x() - w // 2, r.right() - w - 2))
        y = max(r.top() + 2, min(c.y() - h // 2, r.bottom() - h - 2))
        if edge == "left":
            self.move(r.left() + 2, y)
        elif edge == "right":
            self.move(r.right() - w - 2, y)
        elif edge == "top":
            self.move(x, r.top() + 2)
        else:
            self.move(x, r.bottom() - h - 2)
        self.update()

    def enterEvent(self, event):
        if self._docked:
            self._undock()
        super().enterEvent(event)

    # ---- 绘制 ----
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        dark = self._dark
        bg = QColor(38, 38, 54) if dark else QColor(245, 245, 250)
        fg = QColor(232, 232, 242) if dark else QColor(30, 30, 44)
        sub = QColor(138, 138, 160) if dark else QColor(120, 120, 140)
        panel = QColor(50, 50, 72) if dark else QColor(230, 230, 242)
        btn_bg = QColor(58, 58, 82) if dark else QColor(224, 224, 238)
        accent = QColor(theme.get("accent"))
        p.setPen(Qt.NoPen)

        if self._docked:  # 缩起态：细长把手
            p.setBrush(bg)
            p.drawRoundedRect(self.rect(), 5, 5)
            p.setBrush(accent)
            if self._docked in ("left", "right"):
                p.drawRoundedRect(3, self.height() // 2 - 9, 4, 18, 2, 2)
            else:
                p.drawRoundedRect(self.width() // 2 - 9, 3, 18, 4, 2, 2)
            return

        p.setBrush(bg)
        r = self.rect().adjusted(1, 1, -1, -1)
        p.drawRoundedRect(r, 14, 14)
        if not dark:
            p.setPen(QPen(QColor(theme.get("border")), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, 14, 14)
        pg = self.page
        if pg.timer_mode == "countup":
            text = _fmt_clock(pg.countup_elapsed)
        else:
            text = pg._fmt(pg.remaining)
        task = (pg._task or "专注")[:10]

        if self._style == 0:
            # 播放 / 暂停圆按钮
            pc = QPoint(46, 46)
            p.setPen(Qt.NoPen)
            p.setBrush(btn_bg)
            p.drawEllipse(pc, 26, 26)
            p.setBrush(accent)
            if pg.running:
                p.drawRoundedRect(pc.x() - 10, pc.y() - 11, 6, 22, 2, 2)
                p.drawRoundedRect(pc.x() + 4, pc.y() - 11, 6, 22, 2, 2)
            else:
                p.drawPolygon(QPolygonF([
                    QPointF(pc.x() - 7, pc.y() - 12),
                    QPointF(pc.x() - 7, pc.y() + 12),
                    QPointF(pc.x() + 13, pc.y())]))
            # 任务标签 + 大时间
            p.setPen(sub)
            p.setFont(QFont("Microsoft YaHei", 9))
            p.drawText(QRect(96, 16, r.width() - 104, 20), Qt.AlignVCenter,
                       f"{task} >")
            p.setPen(fg)
            p.setFont(QFont("Microsoft YaHei", 19, 700))
            p.drawText(QRect(96, 36, r.width() - 104, 38), Qt.AlignVCenter, text)
            # 统计条（左列可切换指标，右列固定本周专注）
            p.setPen(Qt.NoPen)
            p.setBrush(panel)
            p.drawRoundedRect(10, 94, r.width() - 20, 44, 10, 10)
            half = r.width() // 2
            label = dict(STAT_MODES)[self._stat_mode]
            self._draw_stat(p, 24, label,
                            self._fmt_stat(self._stat_mode,
                                           self._stats.get(self._stat_mode, 0)),
                            fg, sub, clock=True)
            self._draw_stat(p, half + 14, "本周专注",
                            self._fmt_stat("week_focus",
                                           self._stats.get("week_focus", 0)),
                            fg, sub, clock=False)
            return

        if self._style == 1:  # 圆环
            cx = r.center().x()
            cy = r.center().y() + 6
            rad = 34
            p.setPen(QPen(QColor(80, 80, 100), 4))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPoint(cx, cy), rad, rad)
            prog = min(pg.countup_elapsed / max(pg.total, 1), 1.0) \
                if pg.timer_mode == "countup" \
                else (pg.total - pg.remaining) / pg.total if pg.total else 0
            p.setPen(QPen(accent, 4))
            p.drawArc(cx - rad, cy - rad, rad * 2, rad * 2, 90 * 16,
                      int(-prog * 360 * 16))
            p.setPen(fg)
            p.setFont(QFont("Microsoft YaHei", 14, 700))
            p.drawText(r, Qt.AlignCenter, text)
        else:  # 简洁条
            p.setPen(fg)
            p.setFont(QFont("Microsoft YaHei", 16, 700))
            p.drawText(r, Qt.AlignCenter, text)

    def _draw_stat(self, p: QPainter, x: int, label: str, value: str,
                   fg: QColor, sub: QColor, clock: bool):
        """统计条一列：小图标 + 标签 + 数值。"""
        # 小图标：时钟 / 日历
        p.setPen(QPen(sub, 1.4))
        p.setBrush(Qt.NoBrush)
        if clock:
            p.drawEllipse(QPoint(x + 5, 104), 5, 5)
            p.drawLine(x + 5, 104, x + 5, 100)
            p.drawLine(x + 5, 104, x + 8, 104)
        else:
            p.drawRoundedRect(QRect(x, 98, 10, 11), 2, 2)
            p.drawLine(x, 101, x + 10, 101)
            p.drawLine(x + 3, 96, x + 3, 100)
        p.setPen(sub)
        p.setFont(QFont("Microsoft YaHei", 8))
        p.drawText(QRect(x + 16, 96, 76, 16), Qt.AlignVCenter, label)
        p.setPen(fg)
        p.setFont(QFont("Microsoft YaHei", 11, 700))
        p.drawText(QRect(x, 112, 84, 22), Qt.AlignVCenter, value)

    # ---- 命中区域 ----
    def _hit_play(self, pos: QPoint) -> bool:
        return self._style == 0 and QRect(16, 16, 60, 60).contains(pos)

    def _hit_task(self, pos: QPoint) -> bool:
        return self._style == 0 and QRect(92, 12, self.width() - 100, 26).contains(pos)

    def _hit_stats(self, pos: QPoint) -> bool:
        return self._style == 0 and QRect(8, 92, self.width() - 16, 50).contains(pos)

    # ---- 右键菜单 ----
    def contextMenuEvent(self, event):
        menu = self._build_menu()
        menu.exec(event.globalPos())

    def _build_menu(self) -> QMenu:
        pg = self.page
        menu = QMenu(self)
        menu.setStyleSheet(MINI_MENU_QSS)

        act_focus = QAction("🔗  专注于", menu)
        act_focus.triggered.connect(self._pick_task)
        menu.addAction(act_focus)

        mod_menu = menu.addMenu("⏱  修改计时")
        mod_menu.setStyleSheet(MINI_MENU_QSS)
        act_minutes = QAction("修改番茄时长", mod_menu)
        act_minutes.triggered.connect(self._edit_minutes)
        mod_menu.addAction(act_minutes)
        act_switch = QAction("切换正计时" if pg.timer_mode == "pomodoro"
                             else "切换番茄计时", mod_menu)
        act_switch.triggered.connect(self._switch_timer_mode)
        mod_menu.addAction(act_switch)

        style_menu = menu.addMenu("👕  Mini 样式")
        style_menu.setStyleSheet(MINI_MENU_QSS)
        for i, name in enumerate(("卡片", "圆环", "简洁")):
            a = QAction(name, style_menu)
            a.setCheckable(True)
            a.setChecked(i == self._style)
            a.triggered.connect(lambda _, idx=i: self._apply_style(idx))
            style_menu.addAction(a)

        act_pin = QAction("取消置顶" if self._pinned else "窗口置顶", menu)
        act_pin.triggered.connect(self._toggle_pin)
        menu.addAction(act_pin)

        noise_menu = menu.addMenu("🎵  白噪音")
        noise_menu.setStyleSheet(MINI_MENU_QSS)
        from PySide6.QtGui import QActionGroup
        grp = QActionGroup(noise_menu)
        a_off = QAction("关闭", noise_menu)
        a_off.setCheckable(True)
        a_off.setChecked(self._noise_kind is None)
        a_off.triggered.connect(lambda: self._set_noise(None))
        grp.addAction(a_off)
        noise_menu.addAction(a_off)
        for kind, name in sounds.sound_names().items():
            a = QAction(name, noise_menu)
            a.setCheckable(True)
            a.setChecked(self._noise_kind == kind)
            a.triggered.connect(lambda _, k=kind: self._set_noise(k))
            grp.addAction(a)
            noise_menu.addAction(a)

        act_imm = QAction("⛶  沉浸模式", menu)
        act_imm.triggered.connect(self._enter_immersive)
        menu.addAction(act_imm)

        menu.addSeparator()
        act_main = QAction("🖥  打开主窗口", menu)
        act_main.triggered.connect(self._open_main)
        menu.addAction(act_main)
        act_close = QAction("⏻  关闭", menu)
        # 必须显式接掉 triggered 的 checked 布尔：直接连过去的话它会当成
        # stop_noise 传进来，而 triggered 发的永远是 False，「关闭」就停不了噪音。
        act_close.triggered.connect(lambda _checked=False: self._close_mini(True))
        menu.addAction(act_close)
        return menu

    # ---- 菜单动作 ----
    def _pick_task(self):
        popup = TaskPickerPopup(self.page, self)
        popup.picked.connect(lambda _t: self.update())
        pos = self.mapToGlobal(QPoint(24, self.height() + 6))
        popup.move(pos.x(), pos.y())
        popup.show()

    def _open_stat_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(MINI_MENU_QSS)
        for key, label in STAT_MODES:
            a = QAction(label, menu)
            a.setCheckable(True)
            a.setChecked(key == self._stat_mode)
            a.triggered.connect(lambda _, k=key: self._set_stat_mode(k))
            menu.addAction(a)
        menu.exec(self.mapToGlobal(QPoint(12, self.height() - 8)))

    def _set_stat_mode(self, key: str):
        self._stat_mode = key
        self.update()

    def _edit_minutes(self):
        dlg = _MiniMinutesDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            v = dlg.value()
            db.set_setting("pomodoro_work", str(v))
            pg = self.page
            pg._work_min = v
            fav_pomo = pg._active_fav and pg._active_fav.get("mode") == "pomodoro"
            if not pg.running and pg.mode == "work" and not fav_pomo:
                pg._set_mode("work")
            self.update()

    def _switch_timer_mode(self):
        pg = self.page
        pg._seg.set_index(1 if pg.timer_mode == "pomodoro" else 0, emit=True)

    def _apply_style(self, idx: int):
        self.set_style(idx)
        db.set_setting("pomodoro_mini_style", str(idx))
        self.page._mini_style = idx

    def _toggle_pin(self):
        self._pinned = not self._pinned
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self._pinned)
        self.show()

    def _set_noise(self, kind: str | None):
        self._noise_kind = kind
        sounds.play_noise(kind)

    def _enter_immersive(self):
        self._close_mini()
        self.page._enter_focus_mode()

    def _open_main(self):
        win = self.page.window()
        show_tray = getattr(win, "show_from_tray", None)
        if callable(show_tray):
            show_tray()
        else:
            win.show()
            win.raise_()
            win.activateWindow()
        self._close_mini()

    def _close_mini(self, stop_noise: bool = False) -> None:
        """收起 mini 窗。

        stop_noise 只给「⏻ 关闭」和双击传 True：白噪音的开关长在 mini 窗的右键
        菜单上，把它收掉却留着噪音，用户就没有任何地方能按停 —— 只能等它一直
        循环。进沉浸模式 / 打开主窗口两条路径是「导航」不是「丢弃」，会话还在
        跑，氛围音得留着，所以走默认的 False。
        """
        if stop_noise and self._noise_kind:
            sounds.stop_noise()
            self._noise_kind = None       # 清掉选择，重开 mini 时菜单如实显示「关闭」
        self.hide()
        self.page._mini_shown = False

    # ---- 交互 ----
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_global = event.globalPos()
            self._moved = False
            if self._docked:
                self._undock()
                self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
                super().mousePressEvent(event)
                return
            pos = event.position().toPoint()
            if self._hit_play(pos):
                self.page._toggle()
                self.update()
                return
            if self._hit_task(pos):
                self._pick_task()
                return
            if self._hit_stats(pos):
                self._open_stat_menu()
                return
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_global is not None:
            if (event.globalPos() - self._press_global).manhattanLength() > 6:
                self._moved = True
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self._drag_pos)
            # 拖拽中限制在屏幕内，避免丢失窗口
            r = self._screen_rect()
            g = self.geometry()
            x = max(r.left() - g.width() + 24, min(g.left(), r.right() - 24))
            y = max(r.top(), min(g.top(), r.bottom() - 24))
            if (x, y) != (g.left(), g.top()):
                self.move(x, y)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        self._press_global = None
        # 仅在真正拖动后判定贴边：点击展开/点击控件不触发
        if self._moved and not self._docked:
            self._maybe_dock()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        # 双击是「收起它」的手势，和菜单里的「关闭」同一类，噪音一起停
        self._close_mini(True)

    def showEvent(self, event):
        self._sync_timer.start()
        self.refresh_stats()
        if not self._placed:
            self._placed = True
            r = self._screen_rect()
            self.move(max(r.left() + 8, r.right() - self.width() - 28),
                      r.top() + 90)
        super().showEvent(event)

    def closeEvent(self, event):
        self._sync_timer.stop()
        super().closeEvent(event)

    def set_opacity(self, value: float):
        self._opacity = value
        self.setWindowOpacity(max(0.2, min(1.0, value)))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space:
            self.page._toggle()
        elif event.key() == Qt.Key_Escape:
            self.hide()


# ---------------------------------------------------------------------------
# 常用专注：整页（列表 + 计时坞 + 每个专注的统计）
# ---------------------------------------------------------------------------
def _fmt_min(m: int) -> str:
    """分钟数压成参考图那种紧凑写法：0m / 45m / 1h30m / 2h。"""
    m = int(m or 0)
    if m < 60:
        return f"{m}m"
    h, r = divmod(m, 60)
    return f"{h}h{r}m" if r else f"{h}h"


class _FavListRow(QFrame):
    """常用专注整页左列表的一行：emoji + 名称 + 累计时长 + ▶。

    单击选中（右侧看它的统计），点 ▶ 直接开始，右键出 编辑/添加记录/归档/删除。
    行右侧那个数字是这个专注**累计专注了多少**（参考图口径），
    配置的模式/时长只在右侧详情里显示，不放行内。
    """

    selected = Signal(dict)
    started = Signal(dict)
    menu_requested = Signal(dict, object)

    def __init__(self, fav: dict, total_min: int = 0,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("FavListRow")
        self.setFixedHeight(56)
        self.setCursor(Qt.PointingHandCursor)
        self._fav = fav
        self._running = False
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 0, 12, 0)
        h.setSpacing(12)
        chip = QLabel(fav.get("emoji") or DEFAULT_EMOJI)
        chip.setObjectName("FavChip")
        chip.setAlignment(Qt.AlignCenter)
        chip.setFixedSize(34, 34)
        h.addWidget(chip)
        title = widgets.ElidedLabel(fav.get("name") or "未命名")
        title.setObjectName("FavName")
        h.addWidget(title, 1)
        self.total_lbl = QLabel(_fmt_min(total_min))
        self.total_lbl.setObjectName("FavTotal")
        h.addWidget(self.total_lbl)
        self.play_btn = QPushButton("▶")
        self.play_btn.setObjectName("FavPlay")
        self.play_btn.setFixedSize(30, 30)
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setToolTip("开始这个专注")
        self.play_btn.clicked.connect(lambda: self.started.emit(self._fav))
        h.addWidget(self.play_btn)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos, f=fav: self.menu_requested.emit(f, self.mapToGlobal(pos)))

    def set_selected(self, on: bool) -> None:
        self.setProperty("sel", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_running(self, on: bool) -> None:
        """正在计时的这一个，行尾的 ▶ 换成 ⏸ —— 点它就是暂停。

        整页开着时列表里有好几条，光看行分不出「现在跑的是哪个」，
        左下角的坞又只报名字。
        """
        if self._running == on:
            return
        self._running = on
        self.play_btn.setText("⏸" if on else "▶")
        self.play_btn.setToolTip("暂停" if on else "开始这个专注")

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.selected.emit(self._fav)
        super().mouseReleaseEvent(event)


class _TimerDock(QFrame):
    """常用专注页左下角的计时坞：显示当前专注，点它退回主计时器，点 ▶ 启停。"""

    def __init__(self, page: "PomodoroPage", on_open_back, parent=None):
        super().__init__(parent)
        self.page = page
        self.on_open_back = on_open_back
        self.setObjectName("TimerDock")
        self.setFixedHeight(64)
        self.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 0, 12, 0)
        h.setSpacing(12)
        self.icon = QLabel("🍅")
        self.icon.setObjectName("DockIcon")
        self.icon.setFixedSize(30, 30)
        self.icon.setAlignment(Qt.AlignCenter)
        h.addWidget(self.icon)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        self.name_lbl = widgets.ElidedLabel("专注")
        self.name_lbl.setObjectName("DockName")
        col.addWidget(self.name_lbl)
        self.time_lbl = QLabel("25:00")
        self.time_lbl.setObjectName("DockTime")
        col.addWidget(self.time_lbl)
        h.addLayout(col, 1)
        self.play_btn = QPushButton("▶")
        self.play_btn.setObjectName("DockPlay")
        self.play_btn.setFixedSize(34, 34)
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.clicked.connect(self._toggle)
        h.addWidget(self.play_btn)

    def _toggle(self):
        self.page._toggle()
        self.refresh()
        mgr = getattr(self.page, "_fav_manager", None)
        if mgr is not None:
            mgr._sync_running_marks()   # 暂停后行上的 ⏸ 要变回 ▶

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 点坞体（非 ▶ 按钮）→ 收起常用专注页，回到原来的全屏计时界面
        if event.button() == Qt.LeftButton:
            self.on_open_back()
        super().mouseReleaseEvent(event)

    def refresh(self):
        p = self.page
        if p.mode in ("short", "long"):
            # 休息阶段顶着任务名＋番茄图标，看着像还在专注
            self.name_lbl.setText(MODE_META[p.mode][0])
            self.icon.setText("☕")
        else:
            self.name_lbl.setText(p._task or "专注")
            self.icon.setText("⏱" if p.timer_mode == "countup" else "🍅")
        secs = p.countup_elapsed if p.timer_mode == "countup" else p.remaining
        self.time_lbl.setText(p._fmt(secs))
        self.play_btn.setText("⏸" if p.running else "▶")


class FocusManagerView(QWidget):
    """常用专注整页（参考图2）：左=坚持中/已归档 列表 + 底部计时坞，右=选中专注的统计。"""

    def __init__(self, page: "PomodoroPage"):
        super().__init__(page)
        self.page = page
        self.setObjectName("FocusManagerPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._tab = 0                 # 0 坚持中 / 1 已归档
        self._selected: dict | None = None
        self._grain = "week"          # week / month
        self._offset = 0

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_left(), 5)
        root.addWidget(self._build_right(), 4)
        self._apply_style()
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.close)

    # ---- 样式 ----
    def _apply_style(self) -> None:
        self.setStyleSheet(
            "QWidget#FocusManagerPage { background: %s; }"
            "QLabel { background: transparent; }"
            "QFrame#FavListRow { background: transparent; border: none;"
            "  border-bottom: 1px solid %s; }"
            "QFrame#FavListRow:hover { background: %s; }"
            "QFrame#FavListRow[sel=\"true\"] { background: %s; }"
            "QLabel#FavChip { background: %s; border-radius: 17px; font-size: 17px; }"
            "QLabel#FavName { color: %s; font-size: 15px; }"
            "QLabel#FavSub { color: %s; font-size: 12px; }"
            "QLabel#FavTotal { color: %s; font-size: 13px; }"
            "QPushButton#FavPlay { background: transparent; border: none;"
            "  color: %s; font-size: 14px; border-radius: 15px; }"
            "QPushButton#FavPlay:hover { background: %s; }"
            "QPushButton#DockPlay { background: %s; border: none;"
            "  color: %s; font-size: 14px; border-radius: 17px; }"
            "QPushButton#DockPlay:hover { background: %s; }"
            "QFrame#TimerDock { background: %s; border-top: 1px solid %s; }"
            "QLabel#DockName { color: %s; font-size: 12.5px; }"
            "QLabel#DockTime { color: %s; font-size: 18px; font-weight: 600; }"
            "QLabel#MgrTitle { color: %s; font-size: 20px; font-weight: 700; }"
            "QLabel#MgrClose { color: %s; font-size: 13.5px; }"
            "QLabel#MgrFavName { color: %s; font-size: 17px; font-weight: 600; }"
            "QLabel#MgrFavSub { color: %s; font-size: 12.5px; }"
            "QLabel#MgrBigNum { color: %s; font-size: 26px; font-weight: 700; }"
            % (focus_ui.stats_bg().name(),
               theme.get("border"),
               theme.get("surface_hi"), theme.get("focus_soft"),
               focus_ui.tile_bg().name(), theme.get("text_hi"),
               focus_ui.neutral_text("group").name(),
               focus_ui.neutral_text("label").name(), theme.get("focus"),
               theme.get("surface_hi"),
               theme.get("focus_soft"), theme.get("focus"),
               theme.get("focus_soft"),
               theme.get("surface"), theme.get("border"),
               focus_ui.neutral_text("label").name(), theme.get("text_hi"),
               theme.get("text_hi"), theme.get("focus"),
               theme.get("text_hi"), focus_ui.neutral_text("label").name(),
               theme.get("text_hi")))

    def apply_theme(self) -> None:
        self._apply_style()
        for t in getattr(self, "tiles", []):
            t.apply_theme()

    # ---- 左：列表 + 计时坞 ----
    def _build_left(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(20, 18, 12, 0)
        v.setSpacing(0)
        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel("专注")
        title.setObjectName("MgrTitle")
        head.addWidget(title)
        head.addSpacing(8)
        self._tabs = focus_ui.SegmentedControl(["坚持中", "已归档"], "pill", 28)
        self._tabs.changed.connect(self._on_tab)
        head.addWidget(self._tabs)
        head.addStretch(1)
        add_btn = focus_ui.icon_button("+", "新建常用专注", 30)
        add_btn.clicked.connect(lambda: self.page._open_add_favorite())
        head.addWidget(add_btn)
        more = focus_ui.icon_button("⋯", "更多", 30)
        more.clicked.connect(lambda: self.page._open_more_menu())
        head.addWidget(more)
        v.addLayout(head)
        v.addSpacing(12)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_host = QWidget()
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 6, 0)
        self.list_lay.setSpacing(2)
        self.list_lay.addStretch(1)
        self.scroll.setWidget(self.list_host)
        v.addWidget(self.scroll, 1)

        self.dock = _TimerDock(self.page, self.close)
        # 坞横跨到左列底部（去掉左右内缩，贴边更像参考图）
        v.setContentsMargins(20, 18, 12, 0)
        v.addWidget(self.dock)
        return w

    # ---- 右：选中专注的统计 ----
    def _build_right(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(20, 18, 24, 20)
        v.setSpacing(0)
        head = QHBoxLayout()
        close = QPushButton("关闭")
        close.setObjectName("MgrClose")
        close.setCursor(Qt.PointingHandCursor)
        close.setFlat(True)
        close.clicked.connect(self.close)
        head.addWidget(close)
        head.addStretch(1)
        self.more_btn = focus_ui.icon_button("⋯", "更多操作", 30)
        self.more_btn.clicked.connect(self._detail_menu)
        head.addWidget(self.more_btn)
        v.addLayout(head)
        v.addSpacing(10)

        info = QHBoxLayout()
        info.setSpacing(12)
        self.d_chip = QLabel("😊")
        self.d_chip.setObjectName("FavChip")
        self.d_chip.setFixedSize(44, 44)
        self.d_chip.setAlignment(Qt.AlignCenter)
        info.addWidget(self.d_chip)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.d_name = widgets.ElidedLabel("未选择")
        self.d_name.setObjectName("MgrFavName")
        col.addWidget(self.d_name)
        self.d_sub = QLabel("")
        self.d_sub.setObjectName("MgrFavSub")
        col.addWidget(self.d_sub)
        info.addLayout(col, 1)
        v.addLayout(info)
        v.addSpacing(18)

        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.tiles = []
        for label in ("专注天数", "今日时长", "总时长"):
            t = OverviewTile(label)
            self.tiles.append(t)
            tiles.addWidget(t, 1)
        v.addLayout(tiles)
        v.addSpacing(22)

        # 趋势块：大号总时长 + 周/月粒度 + 日期导航 + 柱状图
        top = QHBoxLayout()
        self.d_total = QLabel("0m")
        self.d_total.setObjectName("MgrBigNum")
        top.addWidget(self.d_total)
        top.addSpacing(6)
        unit = QLabel("m")
        unit.setObjectName("MgrFavSub")
        top.addWidget(unit)
        top.addStretch(1)
        self.grain = widgets.ComboBox()
        self.grain.setObjectName("FocusPillCombo")
        self.grain.addItems(["周", "月"])
        self.grain.setFixedSize(64, 28)
        self.grain.setCursor(Qt.PointingHandCursor)
        self.grain.currentIndexChanged.connect(self._on_grain)
        top.addWidget(self.grain)
        v.addLayout(top)
        v.addSpacing(8)

        nav = QHBoxLayout()
        nav.setSpacing(2)
        prev = QPushButton("‹")
        nxt = QPushButton("›")
        for b in (prev, nxt):
            b.setObjectName("FocusLink")
            b.setFixedSize(22, 24)
            b.setCursor(Qt.PointingHandCursor)
        prev.clicked.connect(self._prev_range)
        nxt.clicked.connect(self._next_range)
        self.range_lbl = QLabel("")
        self.range_lbl.setObjectName("MgrFavSub")
        nav.addWidget(prev)
        nav.addWidget(self.range_lbl)
        nav.addWidget(nxt)
        nav.addStretch(1)
        v.addLayout(nav)
        v.addSpacing(6)

        self.chart = focus_ui.MiniTrend("bar", "minutes", divisions=3)
        self.chart.setMinimumHeight(200)
        v.addWidget(self.chart, 1)
        return w

    # ---- 交互 ----
    def _on_tab(self, idx: int) -> None:
        self._tab = idx
        self._refresh_list()

    def _on_grain(self, idx: int) -> None:
        self._grain = "week" if idx == 0 else "month"
        self._offset = 0
        self._refresh_detail()

    def _prev_range(self) -> None:
        self._offset += 1
        self._refresh_detail()

    def _next_range(self) -> None:
        self._offset = max(0, self._offset - 1)
        self._refresh_detail()

    def _range(self) -> tuple[date, date, str]:
        today = date.today()
        if self._grain == "week":
            # 参考图这一格是周日起
            cur_sun = today - timedelta(days=(today.weekday() + 1) % 7)
            start = cur_sun - timedelta(days=self._offset * 7)
            end = start + timedelta(days=6)
            return start, end, f"{start.month}月{start.day}日 - {end.month}月{end.day}日"
        y, m = self._shift_month(today.year, today.month, -self._offset)
        start = date(y, m, 1)
        ny, nm = self._shift_month(y, m, 1)
        return start, date(ny, nm, 1) - timedelta(days=1), f"{y}年{m}月"

    @staticmethod
    def _shift_month(y: int, m: int, delta: int) -> tuple[int, int]:
        m = m + delta
        return y + (m - 1) // 12, (m - 1) % 12 + 1

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.apply_theme()
        self._refresh_list()
        self.dock.refresh()
        self.setFocus()          # ↑↓/Enter 要先进到这一页的 keyPressEvent

    def closeEvent(self, event) -> None:  # noqa: N802
        self.hide()
        event.ignore()

    def refresh_dock(self) -> None:
        if self.isVisible():
            self.dock.refresh()
            self._sync_running_marks()

    def _sync_running_marks(self) -> None:
        act = (self.page._active_fav or {}).get("id") if self.page.running else None
        for r in self.findChildren(_FavListRow):
            r.set_running(bool(act) and r._fav.get("id") == act)

    # ---- 列表 ----
    def _refresh_list(self) -> None:
        while self.list_lay.count() > 1:
            item = self.list_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        favs = [f for f in _load_favorites()
                if int(f.get("archived") or 0) == self._tab]
        totals = services.pomodoro_fav_totals()
        if not favs:
            hint = QLabel("还没有常用专注，点右上角 ＋ 新建" if self._tab == 0
                          else "没有已归档的常用专注")
            hint.setObjectName("FavSub")
            hint.setAlignment(Qt.AlignCenter)
            hint.setContentsMargins(0, 30, 0, 0)
            self.list_lay.insertWidget(0, hint)
            self._selected = None
            self._refresh_detail()
            return
        # 保持选中项（切标签/新建后不丢焦点）
        sel_id = (self._selected or {}).get("id")
        if not any(f.get("id") == sel_id for f in favs):
            self._selected = favs[0]
        for fav in favs:
            row = _FavListRow(fav, totals.get(fav.get("id") or "", 0))
            row.selected.connect(self._select)
            row.started.connect(self._start)
            row.menu_requested.connect(self._row_menu)
            row.set_selected(fav.get("id") == (self._selected or {}).get("id"))
            self.list_lay.insertWidget(self.list_lay.count() - 1, row)
        self._sync_running_marks()
        self._refresh_detail()

    def _select(self, fav: dict) -> None:
        self._selected = fav
        for i in range(self.list_lay.count()):
            w = self.list_lay.itemAt(i).widget()
            if isinstance(w, _FavListRow) and w._fav.get("id") == fav.get("id"):
                w.set_selected(True)
                self.scroll.ensureWidgetVisible(w, 0, 24)
            elif isinstance(w, _FavListRow):
                w.set_selected(False)
        self._refresh_detail()

    # ---- 键盘：↑↓ 换选中、Enter 直接开始 ----
    def keyPressEvent(self, event) -> None:  # noqa: N802
        rows = self.findChildren(_FavListRow)
        if not rows:
            super().keyPressEvent(event)
            return
        cur = next((i for i, r in enumerate(rows)
                    if r._fav.get("id") == (self._selected or {}).get("id")), -1)
        key = event.key()
        if key == Qt.Key_Down:
            self._select(rows[min(cur + 1, len(rows) - 1)]._fav)
        elif key == Qt.Key_Up:
            self._select(rows[max(cur - 1, 0)]._fav)
        elif key in (Qt.Key_Return, Qt.Key_Enter) and cur >= 0:
            self._start(rows[cur]._fav)
        else:
            super().keyPressEvent(event)

    def _start(self, fav: dict) -> None:
        self._select(fav)
        p = self.page
        same = bool(fav.get("id")) and (p._active_fav or {}).get("id") == fav.get("id")
        if same and p.running:
            p._toggle()          # 行上此刻画的是 ⏸，点它就是暂停
        else:
            p._start_favorite(fav)
        self.dock.refresh()
        self._sync_running_marks()

    # ---- 右侧详情 ----
    def _refresh_detail(self) -> None:
        fav = self._selected
        if not fav:
            self.d_chip.setText("😊")
            self.d_name.setText("未选择")
            self.d_sub.setText("")
            for t in self.tiles:
                t.set_value("0")
            self.d_total.setText("0")
            self.chart.set_data([0.0] * 7, [""] * 7)
            self.range_lbl.setText("")
            return
        self.d_chip.setText(fav.get("emoji") or DEFAULT_EMOJI)
        self.d_name.setText(fav.get("name") or "未命名")
        self.d_sub.setText("正计时" if fav.get("mode") == "countup"
                           else f"番茄计时 {fav.get('minutes', 25)}m")
        fav_id = fav.get("id") or ""
        s = services.pomodoro_fav_summary(fav_id)
        self.tiles[0].set_value(str(s["days"]))
        self.tiles[1].set_value(f"{s['today_min']} m")
        self.tiles[2].set_value(f"{s['total_min']} m")
        start, end, label = self._range()
        self.range_lbl.setText(label)
        daily = services.pomodoro_fav_daily(fav_id, start.isoformat(), end.isoformat())
        self.d_total.setText(str(sum(d["minutes"] for d in daily)))
        if self._grain == "week":
            labels = ["日", "一", "二", "三", "四", "五", "六"]
        else:
            labels = [d["date"][8:10] for d in daily]
        today_iso = date.today().isoformat()
        hi = next((i for i, d in enumerate(daily) if d["date"] == today_iso), -1)
        self.chart.set_data([float(d["minutes"]) for d in daily], labels,
                            highlight=hi)

    # ---- 操作菜单（行右键 / 右上 ⋯）----
    def _row_menu(self, fav: dict, global_pos) -> None:
        self._select(fav)
        self._fav_menu_at(global_pos, fav)

    def _detail_menu(self) -> None:
        if not self._selected:
            return
        self._fav_menu_at(self.more_btn.mapToGlobal(QPoint(-140, self.more_btn.height())),
                          self._selected)

    def _fav_menu_at(self, global_pos, fav: dict) -> None:
        archived = int(fav.get("archived") or 0)
        items = [("edit", "编辑"), ("add_record", "添加记录"),
                 ("archive", "取消归档" if archived else "归档"),
                 ("delete", "删除")]
        menu = focus_ui.PopupMenu([tuple(x) for x in items], self)
        slots = [lambda: self.page._open_add_favorite(fav),
                 lambda: self.page._add_record_for_fav(fav),
                 lambda: self._toggle_archive(fav),
                 lambda: self._delete(fav)]
        for row, slot in zip(menu.rows, slots):
            row.clicked.connect(slot)
            row.clicked.connect(menu.close)
            row.installEventFilter(_HoverFilter(menu))
        menu.move(global_pos)
        menu.show()

    def _toggle_archive(self, fav: dict) -> None:
        _set_fav_archived(fav.get("id"), 0 if int(fav.get("archived") or 0) else 1)
        self._selected = None
        self._refresh_list()

    def _delete(self, fav: dict) -> None:
        # 删下去就找不回来了，而且它名下的记录会一起变成「不属于任何常用专注」，
        # 所以先确认一次，并说清记录留不留（习惯页删习惯是同一种口径）。
        n = services.pomodoro_fav_summary(fav.get("id") or "")["count"]
        tail = (f"它已产生的 {n} 条专注记录仍会留在记录列表里。" if n
                else "它还没有专注记录。")
        if not popups.confirm(
                self, "删除常用专注",
                f"确定删除「{fav.get('name') or '未命名'}」吗？{tail}"):
            return
        favs = [f for f in _load_favorites() if f.get("id") != fav.get("id")]
        _save_favorites(favs)
        if self.page._active_fav and self.page._active_fav.get("id") == fav.get("id"):
            self.page._active_fav = None
            self.page._fav_id = ""
            self.page.task_lbl.setText("专注 ›")
        self._selected = None
        self._refresh_list()


# ---------------------------------------------------------------------------
# 主页面
# ---------------------------------------------------------------------------
class PomodoroPage(Page):
    """番茄钟主页：番茄计时 / 正计时 + 任务选择 + 概览面板。"""

    todo_changed = Signal()

    def __init__(self):
        super().__init__("专注", scrollable=False, bare=True)
        self.timer_mode: str = "pomodoro"  # "pomodoro" | "countup"
        self.mode: str = "work"
        self.remaining: int = 0
        self.total: int = 0
        self.countup_elapsed: int = 0
        self.running: bool = False
        self.session_count: int = 0
        self._task: str = ""
        self._active_fav: dict | None = None
        # 本轮计时归属的常用专注 id（落库时写进记录的 fav_id，供其统计归集）
        self._fav_id: str = ""
        self._auto_done_in_round: int = 0
        # 本轮计时的真实开始时刻（落库用），None = 还没开始
        self._session_start: datetime | None = None

        # 设置
        self._load_settings()

        # 计时器
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)

        # 会话持久化：运行中每 10 秒落一次状态，防止退出/崩溃丢失进行中的计时
        self._persist_timer = QTimer(self)
        self._persist_timer.setInterval(10000)
        self._persist_timer.timeout.connect(self._persist_session)

        # Mini 窗
        self._mini: MiniWindow | None = None
        self._mini_shown: bool = False
        self._immersive: ImmersiveOverlay | None = None
        self._stats: StatsView | None = None
        self._fav_manager: FocusManagerView | None = None
        self._mini_shortcut: QShortcut | None = None
        self._hotkey_global: bool = False
        # 首次装配时页面还没挂到主窗口上，取不到 HWND，转一圈事件循环再注册
        self._hotkey_retry = QTimer(self)
        self._hotkey_retry.setSingleShot(True)
        self._hotkey_retry.timeout.connect(self._register_hotkey)
        self._state_text: str = "待开始"
        self._side_applying: bool = False

        self._build_ui()
        self._set_mode("work")
        self._refresh_stats()
        self._reload_records()
        self._register_hotkey()
        self._restore_session()
        if theme.manager is not None:
            theme.manager.changed.connect(self._on_theme_changed)

    def _on_theme_changed(self):
        """主题切换后，自绘控件需要重绘才能拿到新配色。"""
        self._apply_surface_colors()
        if self._stats is not None:
            self._stats.apply_theme()
        if self._fav_manager is not None:
            self._fav_manager.apply_theme()
        for tile in getattr(self, "tiles", []):
            tile.apply_theme()
        for line in self.findChildren(_RecordLine):
            line.apply_theme()
        for hdr in self.findChildren(_DateHeader):
            hdr.apply_theme()
        # 目标达成那行的绿色是内联样式，换肤时得重算
        self._refresh_status()
        self.update()
        for w in self.findChildren(QWidget):
            w.update()

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------
    def _load_settings(self):
        self._work_min = _s_int("pomodoro_work", 25)
        self._short_min = _s_int("pomodoro_short", 5)
        self._long_min = _s_int("pomodoro_long", 15)
        self._interval = _s_int("pomodoro_interval", 4)
        self._goal = _s_int("pomodoro_goal", 8)
        self._auto_next = _s_int("pomodoro_auto_next", 1)
        self._auto_rest = _s_int("pomodoro_auto_rest", 1)
        self._auto_count = _s_int("pomodoro_auto_count", 4)
        self._mini_style = _s_int("pomodoro_mini_style", 0)
        self._mini_opacity = _s_int("pomodoro_mini_opacity", 90)
        self._mini_auto = _s_int("pomodoro_mini_auto", 1)
        self._mini_hotkey = _s("pomodoro_mini_hotkey", "")

    def _on_setting_changed(self, key: str, value: str):
        if key == "pomodoro_work":
            self._work_min = int(value)
            if not self.running and self.mode == "work":
                self._set_mode("work")
        elif key == "pomodoro_short":
            self._short_min = int(value)
            if not self.running and self.mode == "short":
                self._set_mode("short")
        elif key == "pomodoro_long":
            self._long_min = int(value)
            if not self.running and self.mode == "long":
                self._set_mode("long")
        elif key == "pomodoro_interval":
            self._interval = int(value)
        elif key == "pomodoro_goal":
            self._goal = int(value)
            self._refresh_stats()
        elif key == "pomodoro_auto_next":
            self._auto_next = int(value)
        elif key == "pomodoro_auto_rest":
            self._auto_rest = int(value)
        elif key == "pomodoro_auto_count":
            self._auto_count = int(value)
        elif key == "pomodoro_mini_opacity":
            self._mini_opacity = int(value)
            if self._mini:
                self._mini.set_opacity(self._mini_opacity / 100)
        elif key == "pomodoro_mini_auto":
            self._mini_auto = int(value)
        elif key == "pomodoro_mini_theme":
            if self._mini:
                self._mini.reload_theme()
        elif key == "pomodoro_mini_hotkey":
            self._mini_hotkey = value
            self._register_hotkey()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self):
        outer = self.layout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.setSpacing(0)

        # ---------------- 左侧主区 ----------------
        left = QWidget()
        left.setObjectName("FocusMain")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(28, 22, 24, 22)
        ll.setSpacing(0)

        # 顶栏：标题 / 居中分段 / 右侧图标
        head = QGridLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        title = QLabel("专注")
        title.setObjectName("PageTitle")
        head.addWidget(title, 0, 0, Qt.AlignLeft | Qt.AlignVCenter)

        self._seg = focus_ui.SegmentedControl(["番茄计时", "正计时"], "pill", 30)
        self._seg.changed.connect(self._on_seg_changed)
        head.addWidget(self._seg, 0, 1, Qt.AlignCenter)

        right_box = QWidget()
        rb = QHBoxLayout(right_box)
        rb.setContentsMargins(0, 0, 0, 0)
        rb.setSpacing(2)
        rb.addStretch(1)
        add_btn = focus_ui.icon_button("+", "常用专注")
        add_btn.clicked.connect(self._open_fav_manager)
        rb.addWidget(add_btn)
        self._more_btn = focus_ui.icon_button("⋯", "更多")
        self._more_btn.clicked.connect(self._open_more_menu)
        rb.addWidget(self._more_btn)
        head.addWidget(right_box, 0, 2)
        head.setColumnStretch(0, 1)
        head.setColumnStretch(2, 1)
        ll.addLayout(head)

        # 中央计时区
        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addStretch(1)

        self.task_lbl = ClickLabel("专注 ›")
        self.task_lbl.setObjectName("FocusTaskSelector")
        self.task_lbl.setAlignment(Qt.AlignCenter)
        self.task_lbl.setCursor(Qt.PointingHandCursor)
        self.task_lbl.clicked.connect(self._open_task_picker)
        cl.addWidget(self.task_lbl, 0, Qt.AlignHCenter)
        cl.addSpacing(38)

        self.ring = focus_ui.FocusRing()
        # 初值只是占位，真实尺寸在 _apply_side_width 里按左区宽度算
        self.ring.setFixedSize(300, 300)
        cl.addWidget(self.ring, 0, Qt.AlignHCenter)
        cl.addSpacing(34)

        self.start_btn = QPushButton("开始")
        self.start_btn.setObjectName("FocusStart")
        self.start_btn.setFixedSize(150, 46)
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.clicked.connect(self._toggle)
        cl.addWidget(self.start_btn, 0, Qt.AlignHCenter)

        self.finish_btn = QPushButton("结束并记录")
        self.finish_btn.setObjectName("FocusGhost")
        self.finish_btn.setFixedSize(180, 36)
        self.finish_btn.setCursor(Qt.PointingHandCursor)
        self.finish_btn.clicked.connect(self._finish_countup)
        self.finish_btn.hide()
        cl.addSpacing(10)
        cl.addWidget(self.finish_btn, 0, Qt.AlignHCenter)

        cl.addSpacing(16)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(10)
        self.reset_btn = QPushButton("重置")
        self.reset_btn.setObjectName("FocusLink")
        self.reset_btn.setCursor(Qt.PointingHandCursor)
        self.reset_btn.setToolTip("放弃本轮，回到初始状态")
        self.reset_btn.clicked.connect(self._reset)
        self.reset_btn.hide()
        status_row.addStretch(1)
        status_row.addWidget(self.reset_btn)
        self.goal_lbl = QLabel()
        self.goal_lbl.setObjectName("FocusMuted")
        self.goal_lbl.setAlignment(Qt.AlignCenter)
        self.goal_lbl.setCursor(Qt.PointingHandCursor)
        self.goal_lbl.setToolTip("点击打开专注设置")
        self.goal_lbl.mousePressEvent = lambda e: self._open_settings()
        status_row.addWidget(self.goal_lbl)
        status_row.addStretch(1)
        cl.addLayout(status_row)
        cl.addStretch(1)

        ll.addWidget(center, 1)

        # 概览侧栏宽度可拖动调整（双击分隔条复位）
        side = self._build_overview()
        side.setMinimumWidth(SIDE_MIN)
        side.setMaximumWidth(SIDE_MAX)

        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setObjectName("FocusSplit")
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(8)
        self._splitter.addWidget(left)
        self._splitter.addWidget(side)
        # 左区吃掉多余宽度，右栏保持「绝对宽度」不随窗口缩放
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._side_ratio = min(0.85, max(0.18,
                                         _s_int("pomodoro_side_ratio",
                                                int(SIDE_RATIO * 100)) / 100.0))
        self._side_width = _side_default(1180)
        self._splitter.setSizes([max(1, 1180 - self._side_width), self._side_width])
        self._splitter.splitterMoved.connect(self._on_split_moved)
        handle = self._splitter.handle(1)
        if handle is not None:
            handle.setCursor(Qt.SplitHCursor)
            handle.setToolTip("拖动调整概览栏宽度（双击复位）")
            handle.installEventFilter(_SplitHandleFilter(self))
        wrap.addWidget(self._splitter)
        outer.addLayout(wrap)
        self._left_panel = left
        self._apply_surface_colors()

    def _apply_surface_colors(self):
        """左侧主区铺白底。

        参考图整页都是纯白；我们全局的 ``bg`` 是 #f4f5f9，只有待办页的画布
        （``#TodoCanvas``）例外地用了 ``bg_alt``。专注页按同一约定处理，
        既对得上参考图，也不破坏应用内已有的一致性。
        """
        if getattr(self, "_left_panel", None) is None:
            return
        self._left_panel.setStyleSheet(
            "QWidget#FocusMain { background: %s; }" % theme.get("bg_alt"))

    def _on_split_moved(self, pos: int, index: int):
        """拖动分隔条：记住的是**比例**，换窗口尺寸后仍保持参考图的版面关系。

        自己调用 setSizes 校准比例时也会触发 splitterMoved，
        靠 ``_side_applying`` 挡住，否则每次窗口 resize 都会把比例往回漂。
        """
        if self._side_applying:
            return
        sizes = self._splitter.sizes()
        total = sum(sizes)
        if len(sizes) < 2 or total <= 0:
            return
        self._side_width = sizes[1]
        self._side_ratio = max(0.18, min(0.85, sizes[1] / total))
        db.set_setting("pomodoro_side_ratio", str(int(self._side_ratio * 100)))

    def reset_side_width(self):
        """把概览栏比例复位到参考图的默认值。"""
        self._side_ratio = SIDE_RATIO
        self._apply_side_width()
        db.set_setting("pomodoro_side_ratio", str(int(SIDE_RATIO * 100)))

    def _apply_side_width(self):
        """按保存的比例校准概览栏宽度，并让主环跟着左区宽度缩放。

        QSplitter 在控件还没拿到真实尺寸时 setSizes 会被按比例缩放，
        所以首帧布局结束、以及每次窗口改变宽度后都要重新对齐一次。
        """
        total = self._splitter.width()
        if total <= 0:
            return
        want = max(SIDE_MIN, min(SIDE_MAX, int(total * self._side_ratio)))
        self._side_width = want
        sizes = self._splitter.sizes()
        if len(sizes) >= 2 and abs(sizes[1] - want) > 2:
            self._side_applying = True
            try:
                self._splitter.setSizes([total - want, want])
            finally:
                self._side_applying = False
        # 参考图里环直径 / 左区宽 ≈ 0.42（357 / 854）。写死 356 的话，
        # 窗口一小环就顶满整块左区，所以跟着左区实际宽度走。
        left_w = max(1, total - want - self._splitter.handleWidth())
        ring = max(180, min(420, int(left_w * RING_RATIO)))
        if self.ring.width() != ring:
            self.ring.setFixedSize(ring, ring)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not getattr(self, "_split_inited", False):
            self._split_inited = True
            QTimer.singleShot(0, self._apply_side_width)

    def _build_overview(self) -> QWidget:
        side = QFrame()
        side.setObjectName("FocusSide")
        lay = QVBoxLayout(side)
        lay.setContentsMargins(19, 22, 18, 16)
        lay.setSpacing(12)

        title = QLabel("概览")
        title.setObjectName("FocusSideTitle")
        lay.addWidget(title)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.tiles: list[OverviewTile] = []
        for i, label in enumerate(["今日番茄", "今日专注时长", "总番茄", "总专注时长"]):
            tile = OverviewTile(label)
            self.tiles.append(tile)
            grid.addWidget(tile, i // 2, i % 2)
        lay.addLayout(grid)
        lay.addSpacing(4)

        # 专注记录
        rec_header = QHBoxLayout()
        rec_header.setContentsMargins(0, 0, 0, 0)
        rec_header.setSpacing(6)
        rec_title = QLabel("专注记录")
        rec_title.setObjectName("FocusSideTitle")
        rec_header.addWidget(rec_title)
        rec_header.addStretch(1)
        add_btn = focus_ui.icon_button("+", "添加专注记录")
        add_btn.clicked.connect(self._add_record)
        rec_header.addWidget(add_btn)
        lay.addLayout(rec_header)

        self.records_scroll = QScrollArea()
        self.records_scroll.setWidgetResizable(True)
        self.records_scroll.setFrameShape(QFrame.NoFrame)
        self.records_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 原来 260 的下限把整个页面的最小高度顶到 550；记录区自己会滚，
        # 留 120 足够看见一条记录 + 滚动条提示。
        self.records_scroll.setMinimumHeight(120)
        self.records_content = QWidget()
        self.records_layout = QVBoxLayout(self.records_content)
        self.records_layout.setContentsMargins(0, 0, 0, 0)
        self.records_layout.setSpacing(0)
        self.records_layout.addStretch(1)
        self.records_scroll.setWidget(self.records_content)
        self.records_scroll.verticalScrollBar().valueChanged.connect(
            self._on_records_scrolled)
        lay.addWidget(self.records_scroll, 1)
        return side

    # ------------------------------------------------------------------
    # 模式切换
    # ------------------------------------------------------------------
    def _on_seg_changed(self, idx: int):
        new_mode = "pomodoro" if idx == 0 else "countup"
        if new_mode == self.timer_mode:
            return
        self.timer.stop()
        self._persist_timer.stop()
        self.running = False
        self._clear_session()
        self.timer_mode = new_mode
        self._update_ring()
        if new_mode == "countup":
            self.countup_elapsed = 0
            self.total = self._work_min * 60
            self.start_btn.setText("开始")
            self._set_state("待开始")
        else:
            self._set_mode("work")

    def _set_mode(self, mode: str):
        self.mode = mode
        if (mode == "work" and self._active_fav
                and self._active_fav.get("mode") == "pomodoro"):
            minutes = int(self._active_fav.get("minutes") or self._work_min)
        else:
            minutes = {"work": self._work_min, "short": self._short_min,
                       "long": self._long_min}[mode]
        self.total = minutes * 60
        self.remaining = self.total
        self._update_ring()

    def _update_ring(self):
        # 参考图里两个模式的环长得就不一样：番茄计时是连续细圆环，正计时才是刻度环
        self.ring.set_style("plain" if self.timer_mode == "pomodoro" else "ticks")
        if self.timer_mode == "countup":
            prog = min(self.countup_elapsed / max(self.total, 1), 1.0)
            self.ring.set_text(_fmt_clock(self.countup_elapsed))
        else:
            prog = (self.total - self.remaining) / self.total if self.total else 0
            self.ring.set_text(self._fmt(max(self.remaining, 0)))
        self.ring.set_progress(prog)
        self._refresh_status()
        if self._fav_manager is not None and self._fav_manager.isVisible():
            self._fav_manager.refresh_dock()

    def _set_state(self, text: str):
        """记录当前状态文案，并刷新环下方那行小字。"""
        self._state_text = text
        self._refresh_status()

    def _refresh_status(self):
        """环下方那行小字。

        参考图的待机态这里**是空的**，所以只在计时中（或已有进度）才显示，
        待机时整行隐藏，避免多出一行「今日目标 0/8 个番茄」破坏版面。
        """
        btn = getattr(self, "reset_btn", None)
        has_progress = self._has_progress()
        if btn is not None:
            btn.setVisible(has_progress)
        fb = getattr(self, "finish_btn", None)
        if fb is not None:
            # 暂停状态也要能「结束并记录」：它是正计时唯一的落库入口，
            # 只在 running 时露出的话，暂停一下再重置就把这段专注悄悄丢掉了。
            fb.setVisible(self.timer_mode == "countup" and self.countup_elapsed > 0)
        if not has_progress:
            self.goal_lbl.setVisible(False)
            return
        # 今日番茄数只在记录增删时变（那几条路径都会走 _refresh_stats），
        # 缓存下来，免得计时中每秒查一次库。初值 0 无害：没进度时这行本来就隐藏。
        today_count = getattr(self, "_today_count", 0)
        state = getattr(self, "_state_text", "待开始")
        # 达成目标时给点正反馈：灰字变绿、文案说「已达成」，否则 8/8 和 0/8
        # 长得一样，做满一天也看不出区别。
        if today_count >= self._goal:
            goal = f"今日目标已达成 {today_count}/{self._goal}"
            self.goal_lbl.setStyleSheet("color: %s;" % theme.get("green"))
        else:
            goal = f"今日目标 {today_count}/{self._goal} 个番茄"
            self.goal_lbl.setStyleSheet("")
        self.goal_lbl.setText(f"{state} · {goal}" if state else goal)
        self.goal_lbl.setVisible(True)

    def _has_progress(self) -> bool:
        """本轮是否已经有进度（决定要不要露出「重置」）。"""
        if self.timer_mode == "countup":
            return self.countup_elapsed > 0
        return bool(self.total) and self.remaining < self.total

    def _fmt(self, seconds: int) -> str:
        m, s = divmod(max(int(seconds), 0), 60)
        return f"{m:02d}:{s:02d}"

    # ------------------------------------------------------------------
    # 计时控制
    # ------------------------------------------------------------------
    def _toggle(self):
        if self.running:
            self.running = False
            self.timer.stop()
            self._persist_timer.stop()
            self._persist_session()  # 暂停时也保存进度，防崩溃
            self.start_btn.setText("继续")
            self._set_state("已暂停")
            # 暂停分支原来不刷环：全局快捷键 / Mini 上暂停时，常用专注页的坞和
            # 行尾那枚 ⏸ 会一直停在「还在跑」的样子。
            self._update_ring()
            sounds.play("pomodoro_pause")
        else:
            if self.timer_mode == "pomodoro":
                if self.remaining >= self.total:
                    self._auto_done_in_round = 0
                    self._session_start = datetime.now()
            else:
                if self.countup_elapsed == 0:
                    self._auto_done_in_round = 0
                    self._session_start = datetime.now()
            self.running = True
            self.timer.start()
            self._persist_timer.start()
            self._persist_session()
            self.start_btn.setText("暂停")
            self._update_ring()
            self._maybe_show_mini()
            sounds.play("pomodoro_start")

    def _started_at(self) -> str:
        """本轮的**开始**时刻。

        以前两处 pomodoro_add 都不传 started_at，落的是建库默认值
        ``datetime('now','localtime')`` —— 也就是**完成**时刻。于是 09:00 开始
        的 25 分钟番茄被存成 09:25 开始，记录行按 start+时长推出来的结束时间
        跑到未来，时间线整体偏一个番茄长，跨零点还会归错天。
        """
        if self._session_start is None:
            return ""
        return self._session_start.strftime("%Y-%m-%d %H:%M:%S")

    def _reset(self):
        # 只有真的在跑/跑过一轮才算「放弃」，空状态下点重置不该出声
        if self.running or self._session_start is not None:
            sounds.play("pomodoro_abandon")
        self.timer.stop()
        self._persist_timer.stop()
        self.running = False
        self._session_start = None
        if self.timer_mode == "countup":
            self.countup_elapsed = 0
        else:
            self._set_mode(self.mode)
        self._clear_session()
        self.start_btn.setText("开始")
        self._set_state("待开始")
        # 正计时分支不走 _set_mode，不补这一下环上会一直挂着上一次的读数
        self._update_ring()

    def _tick(self):
        if self.timer_mode == "countup":
            self.countup_elapsed += 1
            self._update_ring()
            return
        self.remaining -= 1
        self._update_ring()
        if self.remaining <= 0:
            self._finish_session()

    def _finish_countup(self):
        if self.countup_elapsed <= 0:
            return
        self.timer.stop()
        self._persist_timer.stop()
        self.running = False
        minutes = max(1, round(self.countup_elapsed / 60))
        services.pomodoro_add(minutes, self._task, 1,
                              started_at=self._started_at(), fav_id=self._fav_id)
        self._notify("专注已记录", f"本次专注 {minutes} 分钟")
        sounds.play("countup_recorded")
        self.countup_elapsed = 0
        self._clear_session()
        self.start_btn.setText("开始")
        self._set_state("待开始")
        self._update_ring()
        self._refresh_stats()
        self._reload_records()

    def _finish_session(self):
        self.timer.stop()
        self._persist_timer.stop()
        self.running = False
        started = self._started_at()      # 后面会 _set_mode / 自动接下一阶段
        self._clear_session()
        if self.mode == "work":
            services.pomodoro_add(max(1, self.total // 60), self._task, 1,
                                  started_at=started, fav_id=self._fav_id)
            self.session_count += 1
            self._auto_done_in_round += 1
            self._notify("番茄完成", f"已完成 {self.session_count} 个番茄")
            sounds.play("pomodoro_work_end")
            self._refresh_stats()
            self._reload_records()
            # 决定下一个阶段
            if self.session_count > 0 and self.session_count % self._interval == 0:
                nxt = "long"
            else:
                nxt = "short"
            self._set_mode(nxt)
            if self._auto_rest:
                self._auto_start_next_phase()
            else:
                self.start_btn.setText("开始")
                self._set_state("待开始")
        else:
            # 休息结束
            sounds.play("pomodoro_rest_end")
            self._notify("休息结束", "准备开始下一个番茄")
            self._set_mode("work")
            if self._auto_next and self._auto_done_in_round < self._auto_count:
                self._auto_start_next_phase()
            else:
                self.start_btn.setText("开始")
                self._set_state("待开始")

    def _auto_start_next_phase(self):
        # 自动接上的休息/番茄以前只 timer.start()，既不锚定开始时刻也不起
        # 快照定时器 —— 崩溃或退出就整段丢失，落库时间还会退化成完成时刻。
        self._session_start = datetime.now()
        self.running = True
        self.timer.start()
        self._persist_timer.start()
        self._persist_session()
        self.start_btn.setText("暂停")
        self._update_ring()
        self._maybe_show_mini()

    def _maybe_show_mini(self):
        if not self._mini_auto or self.mode != "work":
            return
        if self._mini is None:
            self._mini = MiniWindow(self)
        self._mini.show()
        self._mini.raise_()
        self._mini_shown = True

    # ------------------------------------------------------------------
    # 任务选择
    # ------------------------------------------------------------------
    def _open_task_picker(self):
        """「专注 ›」→ 任务选择弹窗（任务 / 常用专注 / 习惯 三签，参考图6）。

        以前这里直接弹「常用专注」选择框，点「从待办中选择」才进任务列表 ——
        和滴答反了：主入口应是选任务，常用专注只是其中一个签。
        """
        popup = TaskPickerPopup(self, self, fav_tab=True)
        popup.picked.connect(self._on_task_picked)
        popup.fav_picked.connect(self._start_favorite)
        popup.manage_favs.connect(self._open_fav_manager)
        # PopupCard 关闭时不自己销毁，不加这个每开一次就留一个挂在页面下。
        popup.setAttribute(Qt.WA_DeleteOnClose, True)
        popups.place_popup(popup, self.task_lbl)
        popup.show()

    def _on_fav_picked(self, fav: dict):
        self._active_fav = fav
        self._fav_id = fav.get("id") or ""
        self._task = fav.get("task") or fav["name"]
        self.task_lbl.setText(f"{fav.get('emoji', DEFAULT_EMOJI)} {fav['name']} ›")
        # 常用专注自带计时模式（添加弹窗里就能选正计时），选中它必须切过去，
        # 否则建一个正计时专注、点了却还在番茄倒计时。
        want_countup = fav.get("mode") == "countup"
        idx = 1 if want_countup else 0
        if self.timer_mode != ("countup" if want_countup else "pomodoro"):
            self._seg.set_index(idx)
            self._on_seg_changed(idx)
        if not self.running and self.timer_mode == "pomodoro":
            self._set_mode("work")

    def _open_add_favorite(self, fav: dict | None = None):
        """新增（fav=None）或编辑（传 fav）一个常用专注。"""
        dlg = AddFocusDialog(self, fav)
        if dlg.exec() == QDialog.Accepted and dlg.result_fav:
            self._upsert_favorite(dlg.result_fav, notify=True)

    def _upsert_favorite(self, fav: dict, notify: bool = False) -> bool:
        """按 id 新增或更新一个常用专注。

        重名要去重（否则列表里两条一模一样、点起来无法区分），但编辑自己时
        不算重名。返回是否写入。
        """
        favs = _load_favorites()
        name = fav["name"]
        if any(f.get("name") == name and f.get("id") != fav.get("id") for f in favs):
            self._notify("已存在同名常用专注", f"「{name}」已经在列表里了")
            return False
        idx = next((i for i, f in enumerate(favs)
                    if f.get("id") == fav.get("id")), -1)
        if idx >= 0:
            favs[idx] = fav
        else:
            favs.append(fav)
        _save_favorites(favs)
        act = self._active_fav or {}
        if act.get("id") == fav.get("id"):
            if self.running:
                # 正在计时时只换显示的名字，别去动模式/时长，那会把这一轮打断
                self._active_fav = fav
                self.task_lbl.setText(
                    f"{fav.get('emoji', DEFAULT_EMOJI)} {fav['name']} ›")
            else:
                self._on_fav_picked(fav)
        self._refresh_fav_manager()
        if notify:
            self._notify("已保存常用专注", name)
        return True

    def _start_favorite(self, fav: dict) -> None:
        """从常用专注页/选择器直接开始一个专注：配置 + 立即计时。"""
        same = bool(fav.get("id")) and \
            (self._active_fav or {}).get("id") == fav.get("id")
        if self.running and not same:
            # 计时中途换专注：光改名字的话环上剩的还是上一个专注的时长，
            # 会出现「冥想计了 50 分钟」这种记录，而上一截半专注悄悄就没了。
            cur = (f"已计 {self._fmt(self.countup_elapsed)}"
                   if self.timer_mode == "countup"
                   else f"还剩 {self._fmt(self.remaining)}")
            if not popups.confirm(
                    self, "换专注",
                    f"「{self._task or '专注'}」还在计时（{cur}），"
                    f"结束它并开始「{fav['name']}」？未完成的这段不会记入记录。"):
                return
            self._reset()
        self._on_fav_picked(fav)
        if not self.running:
            self._toggle()

    def _open_more_menu(self):
        """「⋯」菜单：沉浸模式 / Mini 模式 / 统计 / 专注设置。"""
        menu = focus_ui.PopupMenu([
            ("immersive", "沉浸模式"),
            ("mini", "Mini 模式"),
            ("stats", "统计"),
            ("settings", "专注设置"),
        ], self)
        slots = [self._enter_focus_mode, self._toggle_mini,
                 self._open_stats, self._open_settings]
        for row, slot in zip(menu.rows, slots):
            row.clicked.connect(slot)
            row.clicked.connect(menu.close)
            row.installEventFilter(_HoverFilter(menu))
        anchor = self._more_btn
        pos = anchor.mapToGlobal(QPoint(anchor.width() - menu.width(),
                                        anchor.height() + 6))
        menu.move(pos)
        menu.show()

    def _open_stats(self):
        if self._stats is None:
            self._stats = StatsView(self)
        self._stats.refresh()
        self._stats.setGeometry(self.rect())
        self._stats.show()
        self._stats.raise_()
        focus_ui.fade_in(self._stats, 140)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._stats is not None and self._stats.isVisible():
            self._stats.setGeometry(self.rect())
        if self._fav_manager is not None and self._fav_manager.isVisible():
            self._fav_manager.setGeometry(self.rect())
        QTimer.singleShot(0, self._apply_side_width)

    def _on_task_picked(self, title: str):
        self._active_fav = None
        self._fav_id = ""
        self._task = title
        self.task_lbl.setText(f"{title} ›")
        if not self.running:
            self._set_mode("work")

    def start_focus_for(self, title: str, mode: str = "pomodoro"):
        """从别的页面带着任务名进来并直接开始计时（习惯页「开始专注」）。

        mode: "pomodoro" 番茄倒计时 / "countup" 正计时。
        """
        self._active_fav = None
        # _fav_id 也要一起清：它才是这轮计时落到哪条常用专注上的凭据，
        # 只清 _active_fav 的话界面看着是「无」，记史时仍然记到上一条常用专注名下
        self._fav_id = ""
        self._task = title
        self.task_lbl.setText(f"{title} ›")
        want_countup = mode == "countup"
        if self.timer_mode != ("countup" if want_countup else "pomodoro"):
            self._seg.set_index(1 if want_countup else 0)
            self._on_seg_changed(1 if want_countup else 0)
        if self.running:
            return                  # 已经在计了，只换任务名，不打断
        if self.timer_mode == "pomodoro":
            self._set_mode("work")
        self._toggle()

    # ------------------------------------------------------------------
    # 设置弹窗
    # ------------------------------------------------------------------
    def _open_settings(self):
        SettingsDialog(self).exec()

    # ------------------------------------------------------------------
    # Mini 快捷键
    # ------------------------------------------------------------------
    def _register_hotkey(self):
        # 先把上一个拆掉：以前每次改键都新建一个 QShortcut 挂到 self 上，
        # 旧的既没 deleteLater 也没断信号，于是老快捷键一直有效，
        # 改几次键就叠几个生效的快捷方式。
        old = getattr(self, "_mini_shortcut", None)
        if old is not None:
            old.setEnabled(False)
            old.deleteLater()
        self._mini_shortcut = None
        _GLOBAL_HOTKEY.unbind()
        self._hotkey_global = False
        if not self._mini_hotkey:
            return
        if self.window() is self:
            # __init__ 里还没挂进主窗口，这时取 winId() 会凭空造一个原生窗口，
            # 挂上去之后句柄也不是同一个。等事件循环转一圈、装配完了再注册。
            self._hotkey_retry.start(0)
            return
        # 优先系统级：Mini 窗不吃焦点、主窗缩到托盘时也只有这样按得响
        self._hotkey_global = _GLOBAL_HOTKEY.bind(
            self.window(), self._mini_hotkey, self._toggle_mini)
        if self._hotkey_global:
            return
        sc = QShortcut(QKeySequence(self._mini_hotkey), self)
        sc.activated.connect(self._toggle_mini)
        self._mini_shortcut = sc

    def _toggle_mini(self):
        if self._mini is None:
            self._mini = MiniWindow(self)
        if self._mini_shown and self._mini.isVisible():
            self._mini.hide()
            self._mini_shown = False
        else:
            self._mini.show()
            self._mini.raise_()
            self._mini_shown = True

    # ------------------------------------------------------------------
    # 沉浸模式
    # ------------------------------------------------------------------
    def _enter_focus_mode(self):
        if not self.running:
            self._toggle()
        if getattr(self, "_immersive", None) is None:
            self._immersive = ImmersiveOverlay(self)
        self._immersive.showFullScreen()
        self._immersive.raise_()
        self._immersive.activateWindow()

    # ------------------------------------------------------------------
    # 补录
    # ------------------------------------------------------------------
    def _add_record(self):
        dlg = AddRecordDialog(self._work_min, self._work_min, self)
        if dlg.exec() == QDialog.Accepted:
            task, minutes, started, note = dlg.values()
            services.pomodoro_add(minutes, task, 1, started_at=started, note=note)
            self._refresh_stats()
            self._reload_records()

    def _add_record_for_fav(self, fav: dict):
        """从常用专注页给某个专注补录一条记录（带 fav_id，统计才归到它头上）。"""
        minutes = int(fav.get("minutes") or self._work_min)
        dlg = AddRecordDialog(minutes, minutes, self)
        name = fav.get("name") or ""
        if name:
            dlg._on_task(name)
        if dlg.exec() == QDialog.Accepted:
            task, mins, started, note = dlg.values()
            services.pomodoro_add(mins, task, 1, started_at=started, note=note,
                                  fav_id=fav.get("id") or "")
            self._refresh_stats()
            self._reload_records()

    # ------------------------------------------------------------------
    # 常用专注整页
    # ------------------------------------------------------------------
    def _open_fav_manager(self):
        """打开常用专注整页（覆盖在主页上，和统计页一个套路）。"""
        if self._fav_manager is None:
            self._fav_manager = FocusManagerView(self)
        self._fav_manager.setGeometry(self.rect())
        self._fav_manager.show()
        self._fav_manager.raise_()
        focus_ui.fade_in(self._fav_manager, 140)

    def _refresh_fav_manager(self) -> None:
        """整页开着时让它重读一次库；没开着就是空操作。

        列表行右侧的累计、右侧的三张卡和柱状图都从库里算，页面自己不查就没时机更新。
        """
        mgr = getattr(self, "_fav_manager", None)
        if mgr is not None and mgr.isVisible():
            mgr._refresh_list()

    # ------------------------------------------------------------------
    # 概览 / 统计刷新
    # ------------------------------------------------------------------
    def _refresh_stats(self):
        today_count = services.pomodoro_today_count()
        today_min = services.pomodoro_total_minutes_today()
        total_stats = services.pomodoro_total_stats()
        self._today_count = today_count
        self.tiles[0].set_value(str(today_count))
        self.tiles[1].set_value(focus_ui.fmt_tile_dur(today_min))
        self.tiles[2].set_value(str(total_stats["count"]))
        self.tiles[3].set_value(focus_ui.fmt_tile_dur(total_stats["minutes"]))
        self._refresh_status()
        # Mini / 沉浸模式的统计数字改成由这里推，不再各自 400ms 轮询查库
        if self._mini:
            self._mini.refresh_stats()
        if self._immersive is not None and self._immersive.isVisible():
            self._immersive.refresh_stats()
        if self._stats is not None and self._stats.isVisible():
            self._stats.refresh()

    # ------------------------------------------------------------------
    # 专注记录
    # ------------------------------------------------------------------
    def _reload_records(self):
        """重建专注记录列表。

        一次建满 120 条要约 200ms（每条记录 = 一个 QFrame + 自绘轨道 + 若干标签），
        而侧栏一屏只装得下十几条，剩下全是看不见的白建。所以先把「要渲染什么」
        算成一份 plan，再按批次往布局里塞，滚到接近底部时才补下一批。
        """
        while self.records_layout.count() > 1:
            item = self.records_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._record_shown = 0
        records = services.pomodoro_records(RECORD_LIMIT)
        if self._stats is not None and self._stats.isVisible():
            self._stats.refresh()
        # 记录一变，常用专注页的行尾累计和右侧三张卡就过期了；它没有别的刷新时机，
        # 全部挂在这里（补录 / 完成番茄 / 退出结算 / 删记录都走这一条路）。
        self._refresh_fav_manager()
        if not records:
            hint = QLabel("暂无专注记录")
            hint.setObjectName("FocusMuted")
            hint.setAlignment(Qt.AlignCenter)
            hint.setContentsMargins(0, 24, 0, 0)
            self.records_layout.insertWidget(0, hint)
            self._record_plan = []
            return
        # 按日期分组
        groups: dict[str, list[dict]] = {}
        for r in records:
            day = (r.get("started_at") or "")[:10]
            groups.setdefault(day, []).append(r)
        plan: list[tuple] = []
        for day in sorted(groups.keys(), reverse=True):
            plan.append(("header", self._format_day(day)))
            day_rows = groups[day]
            for i, r in enumerate(day_rows):
                # 组内最后一条不画尾线：参考图的轴线到日期分组末尾就收住。
                # tail 按整组算，所以分批截断也不会把线画错。
                plan.append(("row", r, i < len(day_rows) - 1))
        self._record_plan = plan
        self._render_more_records()

    def _render_more_records(self, batch: int = RECORD_BATCH) -> None:
        """渲染下一批记录；顺带处理「一屏还没填满」的情况。"""
        if getattr(self, "_rendering_records", False):
            return
        plan = getattr(self, "_record_plan", [])
        if self._record_shown >= len(plan):
            return
        self._rendering_records = True
        try:
            end = min(len(plan), self._record_shown + batch)
            while self._record_shown < end:
                item = plan[self._record_shown]
                w = (_DateHeader(item[1]) if item[0] == "header"
                     else self._make_record_row(item[1], item[2]))
                self.records_layout.insertWidget(
                    self.records_layout.count() - 1, w)
                self._record_shown += 1
        finally:
            self._rendering_records = False
        # 记录行比预想的矮、一屏装下了一批还有余量时，滚动条永远不会动，
        # 也就不会再触发加载 —— 这里主动补一批。
        sb = self.records_scroll.verticalScrollBar()
        if (self._record_shown < len(plan)
                and sb.maximum() - sb.value() <= RECORD_LOAD_AHEAD):
            QTimer.singleShot(0, self._render_more_records)

    def _on_records_scrolled(self, value: int) -> None:
        sb = self.records_scroll.verticalScrollBar()
        if sb.maximum() - value <= RECORD_LOAD_AHEAD:
            self._render_more_records()

    @staticmethod
    def _format_day(day_str: str) -> str:
        try:
            d = datetime.strptime(day_str, "%Y-%m-%d")
            return f"{d.year}年{d.month}月{d.day}日"
        except ValueError:
            return day_str

    def _make_record_row(self, r: dict, tail: bool = False) -> QWidget:
        """一条专注记录：时间行 + 每个任务各一行，左侧共用一条轨道。

        参考图里多任务记录是**逐行铺开**的（一个任务一行空心圈），
        不是把 "背单词,清深实习" 原样打印出来。
        """
        tasks = [t.strip() for t in (r.get("task") or "").replace("，", ",").split(",")
                 if t.strip()] or ["自由专注"]
        row = QFrame()
        row.setObjectName("FocusRecordRow")
        row.setContextMenuPolicy(Qt.CustomContextMenu)
        row.setToolTip("单击查看/记录想法，右键删除这条记录")
        row.customContextMenuRequested.connect(
            lambda pos, rr=r, w=row: self._record_menu(w, rr, pos))
        rid = r.get("id")
        if rid is not None:
            row.mouseReleaseEvent = lambda e, rr=r, w=row: (
                self._open_record_card(w, rr)
                if e.button() == Qt.LeftButton else None)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        rail = focus_ui.RecordRail(len(tasks), tail)
        lay.addWidget(rail, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        started = (r.get("started_at") or "")
        start_hm = started[11:16] if len(started) >= 16 else ""
        dur = int(r.get("duration_min") or 0)
        end_hm = ""
        if start_hm:
            try:
                end_dt = (datetime.strptime(started[:16], "%Y-%m-%d %H:%M")
                          + timedelta(minutes=dur))
                end_hm = end_dt.strftime("%H:%M")
            except ValueError:
                end_hm = ""
        time_lbl = _RecordLine(f"{start_hm} - {end_hm}" if start_hm else "—",
                               "time", rail.LINE_H)
        col.addWidget(time_lbl)
        for t in tasks:
            col.addWidget(_RecordLine(t, "task", rail.LINE_H))
        lay.addLayout(col, 1)

        dur_lbl = _RecordLine(focus_ui.fmt_rec(dur), "time", rail.LINE_H)
        dur_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(dur_lbl, 0, Qt.AlignTop)
        return row

    def _open_record_card(self, row: QWidget, r: dict) -> None:
        """弹出记录详情卡：贴在记录行右侧，位置参考图 7。"""
        fresh = services.pomodoro_get(int(r["id"])) if r.get("id") is not None else None
        rec = fresh or r
        card = RecordCardPopup(rec, self, on_delete=lambda: self._delete_record(rec))
        card.setAttribute(Qt.WA_DeleteOnClose, True)
        # PopupCard 的 shell 四周有阴影留白，用 resize 定高而不是 adjustSize：
        # 卡里有 QTextEdit，它的 sizeHint 自带十行左右，会把卡片顶得比设计值胖。
        card.resize(RecordCardPopup.W + popups.SHADOW * 2,
                    RecordCardPopup.H + popups.SHADOW * 2)
        side = self.records_scroll
        panel = side.mapTo(self, side.rect().topLeft())
        # 侧栏窄于卡宽时（SIDE_MIN 232 < 434）这里会算出负偏移把卡片顶出屏幕，
        # 所以夹回页面范围内。
        x = panel.x() + max(8, int((side.width() - card.width()) * 0.62))
        x = max(8, min(x, self.width() - card.width() - 8))
        y = row.mapTo(self, QPoint(0, 0)).y()
        y = max(8, min(y, self.height() - card.height() - 8))
        if self._stats is not None and self._stats.isVisible():
            # 统计页是盖在主页上的覆盖层：按主页侧栏的位置摆，卡片会落在
            # 它自己挡住的区域里（等于点了没反应）。这一页改成居中弹。
            x = max(8, (self.width() - card.width()) // 2)
            y = max(8, (self.height() - card.height()) // 2)
        card.move(x, y)
        card.show()

    def _record_menu(self, row: QWidget, r: dict, pos) -> None:
        """记录行右键菜单。"""
        menu = focus_ui.PopupMenu([("delete", "删除这条记录")], self)
        menu.rows[0].clicked.connect(lambda: self._delete_record(r))
        menu.rows[0].clicked.connect(menu.close)
        menu.rows[0].installEventFilter(_HoverFilter(menu))
        menu.move(row.mapToGlobal(pos))
        menu.show()

    def _delete_record(self, r: dict) -> None:
        rid = r.get("id")
        if rid is None:
            return
        services.pomodoro_delete(int(rid))
        self._refresh_stats()
        self._reload_records()
        self._notify("已删除专注记录", r.get("task") or "自由专注")

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------
    def _notify(self, title: str, msg: str):
        tray = QApplication.instance().findChild(QSystemTrayIcon)
        if tray is not None and tray.supportsMessages():
            tray.showMessage(title, msg, QSystemTrayIcon.Information, 4000)

    def _ensure_sync(self):
        """供 MiniWindow 调用，确保数据最新。"""
        pass

    # ------------------------------------------------------------------
    # 会话持久化（防退出/崩溃丢失进行中的计时）
    # ------------------------------------------------------------------
    SESSION_KEY = "pomodoro_session"

    def _persist_session(self):
        """把进行中的计时状态写入 settings；未在计时则清空。"""
        if not self.running:
            db.set_setting(self.SESSION_KEY, "")
            return
        data = {
            "timer_mode": self.timer_mode,
            "mode": self.mode,
            "remaining": self.remaining,
            "total": self.total,
            "countup_elapsed": self.countup_elapsed,
            "task": self._task,
            "session_count": self.session_count,
            "auto_done_in_round": self._auto_done_in_round,
        }
        db.set_setting(self.SESSION_KEY, json.dumps(data, ensure_ascii=False))

    def _clear_session(self):
        db.set_setting(self.SESSION_KEY, "")

    def _restore_session(self):
        """启动时恢复上次未完成的计时状态（不自动开始，仅恢复进度）。"""
        raw = db.get_setting(self.SESSION_KEY, "")
        if not raw:
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            self._clear_session()
            return
        self.timer_mode = data.get("timer_mode", "pomodoro")
        self.mode = data.get("mode", "work")
        self.remaining = int(data.get("remaining", 0))
        self.total = int(data.get("total", 0))
        self.countup_elapsed = int(data.get("countup_elapsed", 0))
        self._task = data.get("task", "")
        self.session_count = int(data.get("session_count", 0))
        self._auto_done_in_round = int(data.get("auto_done_in_round", 0))
        if self._task:
            self.task_lbl.setText(f"{self._task} ›")
        self._seg.set_index(0 if self.timer_mode == "pomodoro" else 1)
        self.start_btn.setText("继续")
        self._set_state("上次计时未完成")
        self._update_ring()
        # 恢复完成后再清理，避免重复恢复
        self._clear_session()

    def save_on_exit(self):
        """应用退出时：正计时进行中直接结算落库；番茄倒计时保留进度。"""
        if self.timer_mode == "countup":
            # 正计时：已发生的专注时间不能丢，直接按当前累计结算
            if self.countup_elapsed > 0:
                minutes = max(1, round(self.countup_elapsed / 60))
                services.pomodoro_add(minutes, self._task, 1, fav_id=self._fav_id)
            self._clear_session()
        else:
            # 番茄倒计时：进行中则保存进度，未开始则清空
            if self.running:
                self._persist_session()
            else:
                self._clear_session()

