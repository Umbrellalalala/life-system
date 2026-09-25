"""习惯打卡：滴答清单式两栏界面（习惯列表 + 详情面板）。

一比一复刻滴答清单「习惯打卡」：
- 左栏：标题 + 坚持中/已归档分段 + 新增/更多；习惯行（圆底 emoji 徽章 +
  名称 + 最近 10 天打卡点阵 + 总坚持天数）
- 右栏：详情（徽章 + 标题 + 已归档标记、月打卡/总打卡/月完成率/当前连续
  四统计卡、可翻月打卡月历、本月打卡日志）
- 添加/编辑习惯对话框：图标选择器、频率（每天/每周指定日）、
  目标（当天完成打卡 / 当天完成一定量）、开始日期、坚持天数、
  所属分组、提醒、自动弹出打卡日志
"""
from __future__ import annotations

import re

from PySide6.QtCore import (Qt, QDate, QPoint, QMimeData, QTimer, Signal,
                            QRectF)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QDrag
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QLabel, QLineEdit,
    QPushButton, QComboBox, QCheckBox, QDialog, QScrollArea, QSpinBox,
    QButtonGroup, QApplication)

from .. import db, popups, services, sounds, theme, widgets
from ..focus_ui import RoundRadio
from ..todo_icons import TickIcon

# 图标选择器备选 emoji（滴答风格彩色圆底）
HABIT_ICONS = [
    "😊", "🙂", "🌞", "🌙", "💤", "💧", "🥛", "🍳", "🍌", "🚭",
    "🧘", "🏃", "🚴", "🏊", "🤸", "🧗", "⚽", "🏋", "🎹", "🎮",
    "📖", "📝", "✏", "💼", "💰", "💵", "🚫", "🍹", "🚰", "📵",
    "❤", "🦷", "💊", "🧴", "🛏", "🧹", "📱", "🎯", "⏰", "🌧",
    "🌈", "🎧", "🎬", "🙏", "🧠", "⚙",
]
BADGE_COLORS = ["green", "blue", "amber", "red", "accent"]
DEFAULT_GROUPS = ["上午", "下午", "晚上", "其他"]


def _sep() -> QFrame:
    """1px 浅灰分隔线。widgets.hline() 用的是 Qt 默认凹痕 HLine，在浅色主题下发黑。

    走 QSS 的 RowLine 而不是行内 setStyleSheet，这样切换日夜主题会自己跟着变。
    """
    line = QFrame()
    line.setObjectName("RowLine")
    line.setFixedHeight(1)
    return line


def _today_str() -> str:
    return QDate.currentDate().toString("yyyy-MM-dd")


def _human_day(d: QDate) -> str:
    return f"{d.month()}月{d.day()}日"


# ---------------------------------------------------------------------------
# 基础小组件
# ---------------------------------------------------------------------------
class EmojiBadge(QWidget):
    """圆形软色底 + emoji/文字徽章（习惯图标）。"""

    def __init__(self, icon: str = "😊", color: str = "green", size: int = 30,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._icon = icon
        self._color = color
        self._size = size
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_icon(self, icon: str, color: str | None = None) -> None:
        self._icon = icon
        if color:
            self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self._size
        soft_key = f"{self._color}_soft"
        # 判色表里有没有这个 soft 键，而不是判主色键在不在：
        # "muted" 在色表里但没有 "muted_soft"，theme.get 会兜底成纯黑，
        # 于是未读/归档类的灰徽章在深色模式下变成一团黑底。
        if soft_key not in theme.LIGHT:
            soft_key = "surface_hi"
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.get(soft_key)))
        p.drawEllipse(QRectF(0, 0, s, s))
        p.setPen(QColor(theme.get(self._color)))
        f = p.font()
        # 纯 ASCII 符号/文字用主题色粗体；emoji 用大号默认字体渲染彩色字形
        if all(ord(ch) < 0x2000 for ch in self._icon):
            f.setPixelSize(int(s * 0.52))
            f.setBold(True)
            p.setFont(f)
        else:
            f.setPixelSize(int(s * 0.56))
            p.setFont(f)
        p.drawText(QRectF(0, 0, s, s), Qt.AlignmentFlag.AlignCenter, self._icon)


class ComboBtn(QPushButton):
    """表单值按钮：左侧文字、右侧自绘下拉小箭头（模仿 QComboBox 外观）。"""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("ComboBtn")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.get("muted")))
        w, h = self.width(), self.height()
        cx, cy = w - 15, h / 2
        t = 4.2
        path = QPainterPath()
        path.moveTo(cx - t, cy - t * 0.5)
        path.lineTo(cx + t, cy - t * 0.5)
        path.lineTo(cx, cy + t * 0.5)
        path.closeSubpath()
        p.drawPath(path)


class HabitDot(QPushButton):
    """列表行内打卡点：已打卡蓝圆白勾，未打卡空心圈，频率没排到的日子淡到看不见。"""

    def __init__(self, habit_id: int, date: str, done: bool,
                 due: bool = True, parent=None):
        super().__init__(parent)
        self.habit_id = habit_id
        self.date = date
        # 固定成正方形，QSS 里 border-radius 取一半才是正圆；
        # 只靠 min/max-width 约束会被布局再拉宽，画出来是圆角方块。
        self.setFixedSize(16, 16)
        self.setObjectName("HabitDot")
        self.setToolTip(date)
        self.set_state(done, due)

    def set_state(self, done: bool, due: bool = True) -> None:
        state = "done" if done else ("off" if not due else "todo")
        widgets._apply_property(self, "dotState", state)
        self.setText("✓" if done else "")
        self.setCursor(Qt.CursorShape.PointingHandCursor
                       if done or due else Qt.CursorShape.ArrowCursor)
        if not due and not done:
            self.setToolTip(f"{self.date}（这个习惯这天不打卡）")
        else:
            self.setToolTip(self.date)


class MiniSpin(QSpinBox):
    """弹窗内小号数字框。"""

    def __init__(self, lo: int, hi: int, val: int, parent=None):
        super().__init__(parent)
        self.setObjectName("MiniSpin")
        self.setRange(lo, hi)
        self.setValue(val)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)


# 弹层实现统一收在 lifeapp/popups.py：习惯页和别的页面共用一套，不再各画一遍。
_place_popup = popups.place_popup
PopupCard = popups.PopupCard
InputPopup = popups.InputPopup
NotePopup = popups.NotePopup
ConfirmPopup = popups.ConfirmPopup



class CheckinSettingsPopup(PopupCard):
    """打卡设置：目前只有「在今日 / 最近7天里显示今日打卡」这一项。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, 340)
        cap = QLabel("打卡设置")
        cap.setObjectName("PopupTitle")
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lay.addWidget(cap)
        self.show_cb = QCheckBox("在“今天”、“最近7天”中显示")
        self.show_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_cb.setChecked(services.habit_checkin_in_smart())
        self.lay.addWidget(self.show_cb)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        ok = QPushButton("确定")
        ok.setObjectName("Primary")
        ok.setFixedWidth(110)
        ok.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.setFixedWidth(110)
        cancel.clicked.connect(self._reject)
        row.addWidget(ok)
        row.addWidget(cancel)
        row.addStretch(1)
        self.lay.addLayout(row)

    def _save(self) -> None:
        services.habit_set_checkin_in_smart(self.show_cb.isChecked())
        # 待办页的「今日打卡」分组要跟着变
        page = next((p for p in getattr(self.window(), "_pages", [])
                     if p.__class__.__name__ == "TodoPage"), None)
        if page is not None:
            page.reload()
        self._accept(None)


class FrequencyPopup(PopupCard):
    """频率弹窗：每天 / 每周指定星期几。"""

    accepted = Signal(str, str)  # (freq_type, freq_days)

    def __init__(self, freq_type: str = "daily", freq_days: str = "", parent=None):
        super().__init__(parent, 300)
        lay = self.lay

        self.mode = QComboBox()
        self.mode.addItems(["每天", "每周"])
        self.mode.setCurrentIndex(0 if freq_type == "daily" else 1)
        self.mode.currentIndexChanged.connect(self._sync)
        lay.addWidget(self.mode)

        self.days_wrap = QWidget()
        dl = QVBoxLayout(self.days_wrap)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(8)
        tip = QLabel("在这些天")
        tip.setObjectName("Meta")
        dl.addWidget(tip)
        row = QHBoxLayout()
        row.setSpacing(5)
        self._day_group = QButtonGroup(self)
        self._day_group.setExclusive(False)
        # 截图顺序：日 一 二 三 四 五 六（Qt dayOfWeek：周一=1 … 周日=7）
        for i, name in enumerate(["日", "一", "二", "三", "四", "五", "六"]):
            wd = 7 if i == 0 else i  # 周日=7
            b = QPushButton(name)
            b.setCheckable(True)
            b.setObjectName("CircleDay")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self._day_group.addButton(b, wd)
            row.addWidget(b, 1)
        dl.addLayout(row)
        self.days_wrap.hide()
        lay.addWidget(self.days_wrap)

        saved = {int(x) for x in freq_days.split(",") if x.strip().isdigit()}
        for b in self._day_group.buttons():
            b.setChecked(self._day_group.id(b) in saved)

        btns = QHBoxLayout()
        ok = QPushButton("确定")
        ok.setObjectName("Primary")
        ok.clicked.connect(self._ok)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.close)
        btns.addWidget(ok, 1)
        btns.addWidget(cancel, 1)
        lay.addLayout(btns)

        self._sync(self.mode.currentIndex())

    def _sync(self, idx: int) -> None:
        self.days_wrap.setVisible(idx == 1)
        self.adjustSize()

    def _ok(self) -> None:
        if self.mode.currentIndex() == 0:
            self.accepted.emit("daily", "")
        else:
            days = sorted(self._day_group.id(b) for b in self._day_group.buttons()
                          if b.isChecked())
            if not days:
                return
            self.accepted.emit("weekly", ",".join(str(d) for d in days))
        self.close()


class GoalPopup(PopupCard):
    """目标弹窗：当天完成打卡 / 当天完成一定量（每天 N 次、打卡记录方式）。"""

    accepted = Signal(dict)  # goal 配置

    def __init__(self, goal: dict, parent=None):
        super().__init__(parent, 320)
        self._goal = dict(goal)
        self._detail_open = True  # 「每天 N 次」明细默认展开
        lay = self.lay

        # 这两个是互斥的单选，不是可以同选的复选框：
        # 之前用两个独立 QCheckBox，勾上「一定量」后「打卡」还留着勾，
        # 看着像同时生效，实际只有后者起作用。
        self.rb_check = RoundRadio("当天完成打卡")
        self.rb_amount = RoundRadio("当天完成一定量")
        self._goal_group = QButtonGroup(self)
        self._goal_group.setExclusive(True)
        self._goal_group.addButton(self.rb_check)
        self._goal_group.addButton(self.rb_amount)
        if self._goal.get("goal_type", "check") == "amount":
            self.rb_amount.setChecked(True)
        else:
            self.rb_check.setChecked(True)
        lay.addWidget(self.rb_check)
        lay.addWidget(self.rb_amount)

        # 「每天 N 次」值行（点击展开/收起明细）
        self.per_btn = ComboBtn()
        self.per_btn.clicked.connect(self._toggle_detail)
        lay.addWidget(self.per_btn)

        self.detail = QFrame()
        dl = QVBoxLayout(self.detail)
        dl.setContentsMargins(2, 2, 2, 2)
        dl.setSpacing(8)
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("每天"))
        self.per_spin = MiniSpin(1, 99, int(goal.get("goal_per_day") or 1))
        self.per_spin.valueChanged.connect(self._changed)
        r1.addWidget(self.per_spin)
        r1.addWidget(QLabel("次"))
        r1.addStretch(1)
        dl.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("打卡时"))
        self.auto_combo = QComboBox()
        self.auto_combo.addItems(["自动记录", "手动记录"])
        self.auto_combo.setCurrentIndex(0 if int(goal.get("goal_auto", 1)) else 1)
        self.auto_combo.currentIndexChanged.connect(self._changed)
        r2.addWidget(self.auto_combo, 1)
        dl.addLayout(r2)
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("每次记录（次）"))
        self.each_spin = MiniSpin(1, 999, int(goal.get("goal_each") or 1))
        self.each_spin.valueChanged.connect(self._changed)
        r3.addWidget(self.each_spin)
        r3.addStretch(1)
        dl.addLayout(r3)
        lay.addWidget(self.detail)

        btns = QHBoxLayout()
        ok = QPushButton("确定")
        ok.setObjectName("Primary")
        ok.clicked.connect(self._ok)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.close)
        btns.addWidget(ok, 1)
        btns.addWidget(cancel, 1)
        lay.addLayout(btns)

        # 控件全部就绪后再接信号（互斥勾选联动 + 值显示刷新）
        self.rb_check.toggled.connect(self._sync)
        self.rb_amount.toggled.connect(self._sync)
        self._sync()
        self._changed()

    def _changed(self) -> None:
        n = self.per_spin.value()
        self.per_btn.setText(f"每天 {n} 次")

    def _sync(self) -> None:
        amount = self.rb_amount.isChecked()
        self.per_btn.setVisible(amount)
        self.detail.setVisible(amount and self._detail_open)
        self.adjustSize()

    def _toggle_detail(self) -> None:
        self._detail_open = not self._detail_open
        self._sync()

    def _ok(self) -> None:
        self.accepted.emit({
            "goal_type": "amount" if self.rb_amount.isChecked() else "check",
            "goal_per_day": self.per_spin.value(),
            "goal_auto": 0 if self.auto_combo.currentIndex() else 1,
            "goal_each": self.each_spin.value(),
        })
        self.close()


class IconPickerPopup(PopupCard):
    """图标选择器：emoji 彩色圆底网格，点中哪一格就高亮哪一格。"""

    picked = Signal(str, str)  # (icon, color)

    def __init__(self, icon: str = "😊", color: str = "green",
                 parent=None):
        # 452 = 10 列 × 36 + 9 × 6 间距 + 卡片左右内边距 14×2 + 一点余量。
        # 之前给 432，网格比视口宽 12px，于是白送一条横向滚动条。
        super().__init__(parent, 452)
        self._icon, self._color = icon, color

        lay = self.lay

        # 当前选择预览
        prev = QHBoxLayout()
        prev.setSpacing(14)
        self.prev_emoji = EmojiBadge(icon, color, 44)
        prev.addWidget(self.prev_emoji)
        prev.addStretch(1)
        lay.addLayout(prev)

        line = widgets.hline()
        line.setObjectName("hline")
        lay.addWidget(line)

        grid_holder = QScrollArea()
        grid_holder.setWidgetResizable(True)
        grid_holder.setFrameShape(QFrame.NoFrame)
        grid_holder.setFixedHeight(238)
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        self._cells: dict[str, QLabel] = {}
        for i, emoji in enumerate(HABIT_ICONS):
            cell = QLabel(emoji)
            cell.setObjectName("BadgeCell")
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.setCursor(Qt.CursorShape.PointingHandCursor)
            cell.setProperty("iconValue", emoji)
            cell.mousePressEvent = (  # type: ignore[method-assign]
                lambda e, c=cell: self._pick(c))
            grid.addWidget(cell, i // 10, i % 10)
            self._cells[emoji] = cell
        grid_holder.setWidget(inner)
        lay.addWidget(grid_holder)

        btns = QHBoxLayout()
        ok = QPushButton("确定")
        ok.setObjectName("Primary")
        ok.clicked.connect(self._confirm)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.close)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        btns.addStretch(1)
        lay.addLayout(btns)

        # 打开时就按当前值描好选中框，否则重开弹窗看不出选的是哪个
        self._mark(icon)

    def _mark(self, icon: str) -> None:
        for emo, cell in self._cells.items():
            widgets._apply_property(cell, "checked", "true" if emo == icon else "false")

    def _pick(self, cell: QLabel) -> None:
        self._icon = cell.property("iconValue")
        idx = HABIT_ICONS.index(self._icon) if self._icon in HABIT_ICONS else 0
        self._color = BADGE_COLORS[idx % len(BADGE_COLORS)]
        self.prev_emoji.set_icon(self._icon, self._color)
        self._mark(self._icon)

    def _confirm(self) -> None:
        self.picked.emit(self._icon, self._color)
        self.close()


# ---------------------------------------------------------------------------
# 添加 / 编辑习惯对话框
# ---------------------------------------------------------------------------
class HabitDialog(QDialog):
    """添加 / 编辑习惯（滴答清单样式表单）。"""

    def __init__(self, habit: dict | None = None, parent=None):
        super().__init__(parent)
        self.habit = habit or {}
        self._icon = self.habit.get("icon", "😊")
        self._color = self.habit.get("color", "green")
        self._freq = (self.habit.get("freq_type", "daily"),
                      self.habit.get("freq_days", ""))
        self._goal = {
            "goal_type": self.habit.get("goal_type", "check"),
            "goal_per_day": self.habit.get("goal_per_day", 1),
            "goal_auto": self.habit.get("goal_auto", 1),
            "goal_each": self.habit.get("goal_each", 1),
        }
        self._target_days = int(self.habit.get("target_days") or 0)
        self._group = self.habit.get("group_name", "其他")
        self._reminder = self.habit.get("reminder", "")

        self.setWindowTitle("编辑习惯" if habit else "添加习惯")
        # 保留原生标题栏，所以不能开 WA_TranslucentBackground：
        # Windows 会把圆角外的透明区合成成黑色，看起来就是一圈黑边。
        self.setObjectName("TickDialog")
        self.setModal(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 18, 28, 22)
        lay.setSpacing(13)

        title = QLabel("编辑习惯" if habit else "添加习惯")
        title.setObjectName("PopupTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)

        # 图标 + 名称
        top = QHBoxLayout()
        top.setSpacing(14)
        self.icon_badge = EmojiBadge(self._icon, self._color, 44)
        self.icon_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.icon_badge.setToolTip("点击选择图标")
        self.icon_badge.mousePressEvent = lambda e: self._pick_icon()  # type: ignore
        top.addWidget(self.icon_badge)
        self.name_input = QLineEdit()
        self.name_input.setObjectName("HabitNameInput")
        self.name_input.setPlaceholderText("每天进步一点点")
        self.name_input.setText(self.habit.get("name", ""))
        top.addWidget(self.name_input, 1)
        lay.addLayout(top)

        # 表单行
        self.freq_btn = ComboBtn()
        self.freq_btn.clicked.connect(self._pick_freq)
        self.goal_btn = ComboBtn()
        self.goal_btn.clicked.connect(self._pick_goal)
        self.start_btn = ComboBtn()
        self.start_btn.clicked.connect(self._pick_start)
        self.target_btn = ComboBtn()
        self.target_btn.setToolTip(
            "设定一共要坚持多少天；达成后在详情标题下标出来（不会自动归档）")
        self.target_btn.clicked.connect(self._pick_target)
        self.group_btn = ComboBtn()
        self.group_btn.clicked.connect(self._pick_group)
        self.reminder_btn = ComboBtn()
        self.reminder_btn.clicked.connect(self._pick_reminder)
        self._rows = [
            ("频率", self.freq_btn), ("目标", self.goal_btn),
            ("开始日期", self.start_btn), ("坚持天数", self.target_btn),
            ("所属分组", self.group_btn), ("提醒", self.reminder_btn),
        ]
        for label, btn in self._rows:
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setObjectName("FormLabel")
            row.addWidget(lbl)
            row.addStretch(1)
            btn.setFixedWidth(306)
            row.addWidget(btn)
            lay.addLayout(row)

        self.auto_log_cb = QCheckBox("自动弹出打卡日志")
        self.auto_log_cb.setChecked(bool(self.habit.get("auto_log", 0)))
        lay.addWidget(self.auto_log_cb)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        btns.addStretch(1)
        save = QPushButton("保存")
        save.setObjectName("Primary")
        save.setFixedWidth(150)
        save.setDefault(True)
        save.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.setFixedWidth(150)
        cancel.clicked.connect(self.reject)
        btns.addWidget(save)
        btns.addWidget(cancel)
        btns.addStretch(1)
        lay.addLayout(btns)

        # 名称框回车即保存；空名称时保存按钮置灰，避免点了没反应
        self._save_btn = save
        save.setEnabled(bool(self.name_input.text().strip()))
        self.name_input.returnPressed.connect(self._save)
        self.name_input.textChanged.connect(
            lambda t: self._save_btn.setEnabled(bool(t.strip())))
        self.setFixedWidth(500)
        self._sync_values()
        self.name_input.setFocus()

    # ---- 值显示 ----
    def _sync_values(self) -> None:
        ftype, fdays = self._freq
        if ftype == "weekly":
            names = {"7": "日", "1": "一", "2": "二", "3": "三",
                     "4": "四", "5": "五", "6": "六"}
            ds = [names.get(x, x) for x in (fdays or "").split(",") if x]
            self.freq_btn.setText(f"每周 {'、'.join(ds)}")
        else:
            self.freq_btn.setText("每天")
        g = self._goal
        self.goal_btn.setText(
            "当天完成一定量" if g["goal_type"] == "amount" else "当天完成打卡")
        sd = self.habit.get("start_date") or _today_str()
        d = QDate.fromString(sd, "yyyy-MM-dd")
        self.start_btn.setText(_human_day(d) if d.isValid() else _human_day(QDate.currentDate()))
        self.target_btn.setText("永远" if not self._target_days else f"{self._target_days} 天")
        self.group_btn.setText(self._group or "其他")
        # 未设提醒时滴答只显示一枚淡灰的「＋」，不是黑字
        self.reminder_btn.setText(self._reminder or "+")
        widgets._apply_property(self.reminder_btn, "placeholder",
                                "false" if self._reminder else "true")

    # ---- 各值弹窗 ----
    def _show_popup(self, pop: QFrame, anchor: QPushButton) -> None:
        _place_popup(pop, anchor)
        pop.show()

    def _show_tick_menu(self, items: list, anchor: QPushButton,
                   checked: object = None, on_pick=None) -> None:
        """滴答式弹层菜单，替掉原生 QMenu（原生那套和其他页面对不上）。"""
        from .todo import TickMenu
        menu = TickMenu(items, anchor, checked=checked)
        if on_pick:
            menu.picked.connect(on_pick)
        self._popup = menu
        _place_popup(menu, anchor)
        menu.show()

    def _ask_value(self, anchor: QPushButton, kind: str, initial: str,
                   on_done, tip: str = "", lo: int = 1) -> None:
        pop = InputPopup(kind, initial, tip=tip, lo=lo, hi=9999)
        pop.accepted.connect(on_done)
        self._popup = pop
        _place_popup(pop, anchor)
        pop.show()
        pop.focus_editor()

    def _pick_icon(self) -> None:
        pop = IconPickerPopup(self._icon, self._color)
        pop.picked.connect(self._on_icon)
        self._popup = pop
        self._show_popup(pop, self.icon_badge)

    def _on_icon(self, icon: str, color: str) -> None:
        self._icon, self._color = icon, color
        self.icon_badge.set_icon(icon, color)

    def _pick_freq(self) -> None:
        pop = FrequencyPopup(*self._freq)
        pop.accepted.connect(self._on_freq)
        self._popup = pop
        self._show_popup(pop, self.freq_btn)

    def _on_freq(self, ftype: str, fdays: str) -> None:
        self._freq = (ftype, fdays)
        self._sync_values()

    def _pick_goal(self) -> None:
        pop = GoalPopup(self._goal)
        pop.accepted.connect(self._on_goal)
        self._popup = pop
        self._show_popup(pop, self.goal_btn)

    def _on_goal(self, goal: dict) -> None:
        self._goal = goal
        self._sync_values()

    def _pick_start(self) -> None:
        from .todo import DatePickerPopup
        cur = self.habit.get("start_date") or _today_str()
        pop = DatePickerPopup(cur)
        pop.accepted.connect(self._on_start)
        self._popup = pop
        self._show_popup(pop, self.start_btn)

    def _on_start(self, date: str, _time: str, *_rest) -> None:
        self.habit["start_date"] = date
        self._sync_values()

    def _pick_target(self) -> None:
        items = [(str(v), "circle", lbl) for lbl, v in
                 (("永远", 0), ("7 天", 7), ("21 天", 21), ("30 天", 30),
                  ("100 天", 100), ("365 天", 365))]
        items.append(("custom", "edit", "自定义天数…"))
        self._show_tick_menu(items, self.target_btn, str(self._target_days),
                        self._on_target_menu)

    def _on_target_menu(self, value: object) -> None:
        if str(value) == "custom":
            self._ask_value(self.target_btn, "int",
                            str(self._target_days or 30), self._on_target_days)
            return
        self._target_days = int(value)
        self._sync_values()

    def _on_target_days(self, text: str) -> None:
        if text.strip().isdigit():
            self._target_days = max(1, int(text))
            self._sync_values()

    def _pick_group(self) -> None:
        items = [(g, "quad", g) for g in services.habit_groups()]
        items.append(("__add__", "plus", "添加分组"))
        self._show_tick_menu(items, self.group_btn, self._group, self._on_group_menu)

    def _on_group_menu(self, value: object) -> None:
        if str(value) == "__add__":
            self._ask_value(self.group_btn, "text", "", self._on_new_group,
                            tip="分组名称")
            return
        self._group = str(value)
        self._sync_values()

    def _on_new_group(self, name: str) -> None:
        name = name.strip()
        if not name:
            return
        services.habit_add_group(name)
        self._group = name
        self._sync_values()

    def _pick_reminder(self) -> None:
        items = []
        if self._reminder:
            items.append(("", "trash", "删除提醒"))
        items += [(t, "clock", t) for t in
                  ("08:00", "12:00", "18:00", "20:00", "22:00")]
        items.append(("custom", "edit", "自定义时间…"))
        self._show_tick_menu(items, self.reminder_btn, self._reminder or None,
                        self._on_reminder_menu)

    def _on_reminder_menu(self, value: object) -> None:
        text = str(value)
        if text == "custom":
            self._ask_value(self.reminder_btn, "text",
                            self._reminder or "20:00", self._on_reminder_text,
                            tip="HH:MM")
            return
        self._reminder = text
        self._sync_values()

    def _on_reminder_text(self, text: str) -> None:
        text = text.strip()
        if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", text):
            self._reminder = f"{int(text.split(':')[0]):02d}:{text.split(':')[1]}"
            self._sync_values()

    # ---- 保存 ----
    def _save(self) -> None:
        name = self.name_input.text().strip()
        if not name:
            self.name_input.setFocus()
            return
        fields = dict(
            name=name, icon=self._icon, color=self._color,
            freq_type=self._freq[0], freq_days=self._freq[1],
            goal_type=self._goal["goal_type"],
            goal_per_day=self._goal["goal_per_day"],
            goal_auto=self._goal["goal_auto"],
            goal_each=self._goal["goal_each"],
            start_date=self.habit.get("start_date") or _today_str(),
            target_days=self._target_days,
            group_name=self._group, reminder=self._reminder,
            auto_log=1 if self.auto_log_cb.isChecked() else 0,
        )
        if self.habit.get("id"):
            services.habit_update(self.habit["id"], **fields)
        else:
            hid = services.habit_add(**fields)
            self.habit = {"id": hid, **fields}
        self.accept()


# ---------------------------------------------------------------------------
# 习惯行 / 统计卡 / 月历 / 详情
# ---------------------------------------------------------------------------
class HabitRow(QFrame):
    """习惯列表行：徽章 + 名称 + 最近 10 天点阵 + 总坚持天数。"""

    selected = Signal(int)
    toggled = Signal(int, str)  # (habit_id, date) 点击点阵切换打卡
    contextRequested = Signal(int, QWidget)  # 右键：(habit_id, 作为锚点的行)

    DOT_DAYS = 8

    def __init__(self, habit: dict, checks: dict, selected: bool = False):
        super().__init__()
        self.habit = habit
        self._press: QPoint | None = None
        self.setObjectName("HabitRow")
        self.setFixedHeight(54)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        widgets._apply_property(self, "selected", "true" if selected else "false")
        self.line = QFrame(self)
        self.line.setObjectName("RowLine")
        self.line.setFixedHeight(1)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 4, 14, 4)
        lay.setSpacing(10)

        self.badge = EmojiBadge(habit["icon"], habit.get("color", "green"), 30)
        lay.addWidget(self.badge)

        # 用 ElidedLabel：普通 QLabel 的最小宽度就是全文宽度，习惯名一长
        # 整行的 sizeHint 被顶到 500+，右边八颗打卡点和「共坚持」直接被裁没。
        self.name_lbl = widgets.ElidedLabel(habit["name"])
        self.name_lbl.setObjectName("HabitName")
        lay.addWidget(self.name_lbl, 1)

        today = QDate.currentDate()
        self._dots: list[HabitDot] = []
        for i in range(self.DOT_DAYS - 1, -1, -1):
            d = today.addDays(-i)
            ds = d.toString("yyyy-MM-dd")
            done = services.habit_is_done(habit, checks, ds)
            dot = HabitDot(habit["id"], ds, done,
                           services.habit_due_on(habit, d))
            dot.clicked.connect(
                lambda _=False, h=habit["id"], dd=ds: self.toggled.emit(h, dd))
            lay.addWidget(dot)
            self._dots.append(dot)

        total = sum(1 for rec in checks.values()
                    if rec["count"] >= services.habit_goal_count(habit))
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.count_lbl = QLabel(f"{total} 天")
        self.count_lbl.setObjectName("HabitCount")
        self.count_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.count_sub = QLabel("共坚持")
        self.count_sub.setObjectName("HabitCountSub")
        self.count_sub.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        col.addWidget(self.count_lbl)
        col.addWidget(self.count_sub)
        lay.addLayout(col)

    def set_compact(self, on: bool) -> None:
        """窄窗口下只留徽章 + 名字：点阵和「N 天 / 共坚持」收掉。

        列表栏被压到 200 出头时这两块放不下，与其把名字截断、把点阵挤没，
        不如干脆收起来 —— 反正右边详情里都有。
        """
        hide = not on
        for dot in self._dots:
            dot.setVisible(hide)
        self.count_lbl.setVisible(hide)
        self.count_sub.setVisible(hide)

    def refresh(self, checks: dict) -> None:
        """只更新点阵和总天数，不重建整行（点一次打卡就重建列表太抖）。"""
        for dot in self._dots:
            done = services.habit_is_done(self.habit, checks, dot.date)
            qd = QDate.fromString(dot.date, "yyyy-MM-dd")
            dot.set_state(done, services.habit_due_on(self.habit, qd))
        total = sum(1 for rec in checks.values()
                    if rec["count"] >= services.habit_goal_count(self.habit))
        self.count_lbl.setText(f"{total} 天")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 分隔线从名称下方起，不贴左边缘（滴答的行线都是缩进的）
        self.line.setGeometry(14, self.height() - 1,
                              self.width() - 28, 1)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position().toPoint()
            self.selected.emit(self.habit["id"])
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        # 按住拖出一段距离才起拖：不然单击选中都会被判成拖拽
        if (self._press is not None
                and event.buttons() & Qt.MouseButton.LeftButton
                and (event.position().toPoint() - self._press).manhattanLength()
                >= QApplication.startDragDistance()):
            spot = self._press
            self._press = None
            self._start_drag(spot)
        super().mouseMoveEvent(event)

    def _start_drag(self, spot: QPoint) -> None:
        mime = QMimeData()
        mime.setData("application/x-life-habit", str(self.habit["id"]).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        pm = self.grab()
        drag.setPixmap(pm)
        drag.setHotSpot(spot)
        drag.exec(Qt.DropAction.MoveAction)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 右键整行弹菜单，和滴答一致（它的习惯行没有别的右键语义）
        if event.button() == Qt.MouseButton.RightButton:
            self.contextRequested.emit(self.habit["id"], self)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class HabitTile(QFrame):
    """详情统计卡：小图标 + 标签 / 大数字 + 单位。

    给了 ``variants`` 就多一枚 ⇄，点一下在两组「标签 + 数值」之间切
    —— 滴答把「当前连续 / 最高连续」并在一格里，就是这么切的。
    """

    def __init__(self, icon: str, color: str, label: str):
        super().__init__()
        self.setObjectName("HabitTile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(7)
        badge = EmojiBadge(icon, color, 20)
        top.addWidget(badge)
        # ElidedLabel：普通 QLabel 的最小宽度 = 全文宽度，四张统计卡两两并排时
        # 会把整个详情面板钉住。但「月完成率」这种本来就 4 个字，让它省成
        # 「月完…」只是难看，所以给个能放下四个字的下限。
        self.caption = widgets.ElidedLabel(label)
        self.caption.setObjectName("TileLabel")
        self.caption.setMinimumWidth(52)
        top.addWidget(self.caption)
        self._variants: list[tuple[str, str, str]] = []
        self._vi = 0
        self.swap_btn = QPushButton("⇄")
        self.swap_btn.setObjectName("TileSwap")
        self.swap_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swap_btn.setFixedSize(22, 18)
        self.swap_btn.hide()
        self.swap_btn.clicked.connect(self._cycle)
        top.addWidget(self.swap_btn)
        top.addStretch(1)
        lay.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setSpacing(3)
        self.value_lbl = QLabel("0")
        self.value_lbl.setObjectName("TileBig")
        self.unit_lbl = QLabel("")
        self.unit_lbl.setObjectName("TileUnit")
        bottom.addWidget(self.value_lbl)
        bottom.addWidget(self.unit_lbl)
        bottom.addStretch(1)
        lay.addLayout(bottom)

    def set_value(self, value: str, unit: str = "") -> None:
        self.value_lbl.setText(value)
        self.unit_lbl.setText(unit)

    def set_variants(self, variants: list[tuple[str, str, str]]) -> None:
        """``[(标签, 数值, 单位)]``。刷新时保持用户已经切到的那一项，不弹回默认。"""
        if len(variants) != len(self._variants):
            self._vi = 0
        self._variants = list(variants)
        self._vi = min(self._vi, len(self._variants) - 1)
        self.swap_btn.setVisible(len(self._variants) > 1)
        self._apply()

    def _cycle(self) -> None:
        if len(self._variants) > 1:
            self._vi = (self._vi + 1) % len(self._variants)
            self._apply()

    def _apply(self) -> None:
        label, value, unit = self._variants[self._vi]
        self.caption.setText(label)
        self.value_lbl.setText(value)
        self.unit_lbl.setText(unit)
        nxt = self._variants[(self._vi + 1) % len(self._variants)][0]
        self.swap_btn.setToolTip(f"切换到「{nxt}」")


class CalDayCell(QWidget):
    """月历里的一格，圆底自己画。

    走 QSS 的 ``border-radius: 16px`` 时，格子尺寸一变圆就退化成药丸，
    而月历要跟着一块块铺满详情面板的宽度、尺寸是浮动的，所以干脆自绘。
    """

    clicked_ = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.date = QDate()
        self.text = ""
        self.state = "past"        # done / today / past / dim
        self._hover = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("habit-day")

    def set_day(self, date: QDate, text: str, state: str) -> None:
        self.date, self.text, self.state = date, text, state
        # 只有真能打卡的日子才给手型光标：以前未来日、以及「每周三」这种
        # 没排到的日子也是一只手，点下去什么也不发生。
        live = state in ("done", "today", "past")
        self.setCursor(Qt.CursorShape.PointingHandCursor if live
                       else Qt.CursorShape.ArrowCursor)
        if state == "dim" and date <= QDate.currentDate():
            self.setToolTip(f"{date.toString('yyyy-MM-dd')}（这个习惯今天不打卡）")
        else:
            self.setToolTip(date.toString("yyyy-MM-dd"))
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked_.emit()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = float(min(self.width(), self.height()))
        box = QRectF((self.width() - s) / 2.0 + 1.0,
                     (self.height() - s) / 2.0 + 1.0, s - 2.0, s - 2.0)
        fg = theme.get("text")
        bg: QColor | None = None
        if self.state == "done":
            bg, fg = QColor(theme.get("accent")), "#ffffff"
        elif self.state == "today":
            # 滴答给今天也是同一层浅灰底，只把字染成蓝色加粗（用户截图对得上）
            fg = theme.get("accent")
            bg = QColor(theme.get("surface_hi"))
        elif self.state == "dim":
            fg = theme.get("border_strong")
        else:                                    # past：过去的空日子给个浅灰圆
            bg = QColor(theme.get("surface_hi"))
        if self._hover and self.state in ("past", "today", "done") and bg is not None:
            bg = bg.darker(108)
        if bg is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawEllipse(box)
        f = p.font()
        f.setPixelSize(max(11, int(s * 0.40)))
        f.setBold(self.state in ("done", "today"))
        p.setFont(f)
        p.setPen(QColor(fg))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text)


class HabitCalendar(QWidget):
    """打卡月历：星期表头和日期格共用一个网格，圆底尺寸随面板宽度走。"""

    dayClicked = Signal(str)  # yyyy-MM-dd

    def __init__(self):
        super().__init__()
        today = QDate.currentDate()
        self._month = QDate(today.year(), today.month(), 1)
        self._checks: dict = {}
        self._habit: dict | None = None
        self._habit_id = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        head = QHBoxLayout()
        self.prev_btn = QPushButton("‹")
        self.next_btn = QPushButton("›")
        for b in (self.prev_btn, self.next_btn):
            b.setObjectName("CalNav")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prev_btn.clicked.connect(lambda: self._shift(-1))
        self.next_btn.clicked.connect(lambda: self._shift(1))
        self.title_lbl = QLabel()
        self.title_lbl.setObjectName("CalHeader")
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 滴答把 ‹ › 贴在年月两侧，而不是分别顶在左右两端
        head.addStretch(1)
        head.addWidget(self.prev_btn)
        head.addWidget(self.title_lbl)
        head.addWidget(self.next_btn)
        head.addStretch(1)
        lay.addLayout(head)

        # 表头放第 0 行、日子从第 1 行起，两者共用列宽才不会错位
        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 4, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(6)
        self._wd: list[QLabel] = []
        for col, name in enumerate(("日", "一", "二", "三", "四", "五", "六")):
            lbl = QLabel(name)
            lbl.setObjectName("CalWd")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.grid.addWidget(lbl, 0, col)
            self._wd.append(lbl)
        self._btns: list[CalDayCell] = []
        for i in range(42):
            cell = CalDayCell(self)
            cell.clicked_.connect(lambda c=cell: self._on_day(c))
            self.grid.addWidget(cell, 1 + i // 7, i % 7)
            self._btns.append(cell)
        for col in range(7):
            self.grid.setColumnStretch(col, 1)
        lay.addLayout(self.grid)
        self._cell = 34
        self._relayout()

    def _relayout(self) -> None:
        """格子尽量铺满面板宽度，但不超过滴答那种 46px 的圆。

        只给上限、下限留 26：早先用 setFixedSize，minimumSizeHint 就跟着
        「上一次算出来的宽度」走（46×7 + 间距 = 370），详情面板一窄整个 content
        就被这个数钉住 —— 窄窗口下月历的周六那列会被直接切掉（横向滚动条是关的）。
        """
        gut = self.grid.horizontalSpacing() * 6
        cell = max(26, min(46, (self.width() - gut) // 7))
        if cell == self._cell:
            return
        self._cell = cell
        for b in self._btns:
            b.setMinimumWidth(26)
            b.setMaximumWidth(cell)
            b.setFixedHeight(cell)
        for lbl in self._wd:
            lbl.setMinimumWidth(26)
            lbl.setMaximumWidth(cell)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def set_habit(self, habit: dict | None, checks: dict) -> None:
        self._habit = habit
        self._habit_id = habit["id"] if habit else 0
        self._checks = checks
        self._refresh()

    def _shift(self, delta: int) -> None:
        self._month = self._month.addMonths(delta)
        self._refresh()

    def month(self) -> QDate:
        return self._month

    def set_month(self, d: QDate) -> None:
        self._month = QDate(d.year(), d.month(), 1)
        self._refresh()

    def _on_day(self, cell: CalDayCell) -> None:
        if not cell.date.isValid() or not self._habit:
            return
        if cell.date > QDate.currentDate():
            return
        if cell.state == "dim":       # 频率没排到的日子，不给打
            return
        self.dayClicked.emit(cell.date.toString("yyyy-MM-dd"))

    def _refresh(self) -> None:
        self.title_lbl.setText(f"{self._month.year()}年{self._month.month():02d}月")
        start = self._month.addDays(-(self._month.dayOfWeek() % 7))
        today = QDate.currentDate()
        checks = self._checks
        habit = self._habit or {}
        for i, cell in enumerate(self._btns):
            d = start.addDays(i)
            done = (checks.get(d.toString("yyyy-MM-dd"), {}).get("count", 0)
                    >= services.habit_goal_count(habit)) if habit else False
            # 滴答的圆底只看「日子在不在今天之前」，不看是不是本月：
            # 过去的空日子是浅灰圆、今天是蓝字无底、未来是浅灰字无底、打过卡是蓝圆白字。
            # 「每周三、五」这类习惯一周里有五天根本不排到，以前会和漏打长得
            # 一模一样（一周看着像断了五天），而且点下去还真能写进打卡记录。
            due = services.habit_due_on(habit, d) if habit else True
            if done:
                state = "done"                  # 打过的一律照实显示，好撤
            elif d > today or not due:
                state = "dim"
            elif d == today:
                state = "today"
            else:
                state = "past"
            cell.set_day(d, str(d.day()), state)


class HabitDetail(QFrame):
    """右侧详情面板：标题区 + 统计卡 + 月历 + 打卡日志。"""

    changed = Signal()          # 打卡数据变化（刷新列表）
    moreRequested = Signal(dict)  # 详情 ⋯：菜单由 HabitPane 统一弹
    closeRequested = Signal()   # 窄窗口覆盖模式下点 ✕

    def __init__(self):
        super().__init__()
        self.setObjectName("HabitDetailPane")
        self._habit: dict | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.addStretch(2)
        # 用自绘的灰色插画，不用 📋 emoji：emoji 字形由系统决定、颜色固定，
        # 深色模式下会亮成一块，和待办页的空状态也不是同一套。
        ico = TickIcon("empty_box", 76, "border_strong")
        ico.setFixedSize(76, 76)
        el.addWidget(ico, 0, Qt.AlignmentFlag.AlignHCenter)
        # 原来写的是「点击习惯标题查看详情」—— 可这页点整行任意位置都进详情，
        # 而且一个习惯都没有时这句话根本无从下手。
        tip = QLabel("点左侧任意一行看详情，或点右上角 ＋ 新建一个习惯")
        tip.setObjectName("DetailEmpty")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(tip)
        el.addStretch(3)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        form = QVBoxLayout(self.content)
        form.setContentsMargins(22, 20, 22, 20)
        form.setSpacing(14)
        self.scroll.setWidget(self.content)

        # 标题行
        head = QHBoxLayout()
        head.setSpacing(10)
        # 窄窗口下详情是整屏覆盖的，需要一个 ✕ 回到列表；两栏并排时没有它
        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("DetailClose")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setToolTip("返回习惯列表")
        self.close_btn.clicked.connect(lambda: self.closeRequested.emit())
        self.close_btn.hide()
        head.addWidget(self.close_btn)
        self.badge = EmojiBadge("😊", "green", 40)
        head.addWidget(self.badge)
        self.title_col = QVBoxLayout()
        self.title_col.setSpacing(1)
        self.title_lbl = QLabel()
        self.title_lbl.setObjectName("HabitDetailTitle")
        self.title_col.addWidget(self.title_lbl)
        self.sub_lbl = QLabel()
        self.sub_lbl.setObjectName("HabitDetailSub")
        self.sub_lbl.hide()
        self.title_col.addWidget(self.sub_lbl)
        head.addLayout(self.title_col, 1)
        self.more_btn = QPushButton("⋯")
        self.more_btn.setObjectName("SettingsBtn")
        self.more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more_btn.clicked.connect(self._show_menu)
        head.addWidget(self.more_btn)
        form.addLayout(head)

        # 统计卡 2×2
        grid_holder = QWidget()
        grid = QGridLayout(grid_holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        self.tile_month = HabitTile("✔", "green", "月打卡")
        self.tile_total = HabitTile("📅", "green", "总打卡")
        self.tile_rate = HabitTile("%", "amber", "月完成率")
        self.tile_streak = HabitTile("📈", "blue", "当前连续")
        for col, t in enumerate((self.tile_month, self.tile_total)):
            grid.addWidget(t, 0, col)
        for col, t in enumerate((self.tile_rate, self.tile_streak)):
            grid.addWidget(t, 1, col)
        form.addWidget(grid_holder)

        form.addWidget(_sep())

        # 月历
        self.calendar = HabitCalendar()
        self.calendar.dayClicked.connect(self._toggle_day)
        form.addWidget(self.calendar)

        form.addWidget(_sep())

        # 打卡日志
        self.log_title = QLabel()
        self.log_title.setObjectName("HabitLogTitle")
        form.addWidget(self.log_title)
        self.log_list = QVBoxLayout()
        self.log_list.setSpacing(2)
        form.addLayout(self.log_list)
        self.log_empty = QLabel("这个月没有写打卡日志哦")
        self.log_empty.setObjectName("DetailEmpty")
        self.log_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form.addWidget(self.log_empty)
        form.addStretch(1)

        root.addWidget(self.empty)
        root.addWidget(self.scroll)
        self.scroll.hide()

    # ---- 数据 ----
    def show_habit(self, habit: dict | None) -> None:
        self._habit = habit
        if not habit:
            self.scroll.hide()
            self.empty.show()
            return
        self.empty.hide()
        self.scroll.show()
        self.badge.set_icon(habit["icon"], habit.get("color", "green"))
        self.title_lbl.setText(habit["name"])
        self.reload()

    def reload(self) -> None:
        if not self._habit:
            return
        checks = services.habit_checks_map(self._habit["id"])
        stats = services.habit_stats(self._habit, checks)
        # 坚持天数以前只在表单里存着，界面上从不回报，tooltip 却写着「自动归档」。
        # 把它算出来的进度显示出来，至少让用户看到设了有用。
        bits = []
        if self._habit.get("archived"):
            bits.append("已归档")
        target = int(self._habit.get("target_days") or 0)
        if target:
            bits.append(f"目标 {stats['total_days']}/{target} 天"
                        + ("，已达成" if stats["target_reached"] else ""))
        self.sub_lbl.setText(" · ".join(bits))
        self.sub_lbl.setVisible(bool(bits))
        self.tile_month.set_value(str(stats["month_days"]), "天")
        self.tile_total.set_value(str(stats["total_days"]), "天")
        self.tile_rate.set_value(f"{stats['month_rate']}", "%")
        self.tile_streak.set_variants([
            ("当前连续", str(stats["streak"]), "天"),
            ("最高连续", str(stats["longest_streak"]), "天"),
        ])
        self._celebrate(stats)
        self.calendar.set_habit(self._habit, checks)
        self._build_logs(checks)

    def _celebrate(self, stats: dict) -> None:
        """刷新最长连续 / 达成目标时响一声。

        基线记在 settings 里：第一次看到某个习惯只记数不响 —— 否则一打开详情
        就会为早就创下的记录放音乐。之后数字再往上走，才算「刚刚发生」。
        """
        hid = self._habit["id"]
        best = int(stats["longest_streak"])
        key = f"sound_habit_best_{hid}"
        try:
            prev = int(db.get_setting(key, "") or 0)
        except ValueError:
            prev = 0
        if best > prev:
            db.set_setting(key, str(best))
            if prev > 0:
                sounds.play("habit_streak_record")
        if stats["target_reached"]:
            tkey = f"sound_habit_target_{hid}"
            if db.get_setting(tkey) != "1":
                db.set_setting(tkey, "1")
                sounds.play("habit_target_reached")

    # ---- 打卡 ----
    def _toggle_day(self, date: str) -> None:
        if not self._habit:
            return
        habit = self._habit
        checks = services.habit_checks_map(habit["id"])
        rec = checks.get(date, {"count": 0, "note": ""})
        done = rec["count"] >= services.habit_goal_count(habit)
        services.habit_set_count(habit["id"], date, 0 if done else
                                 services.habit_goal_count(habit))
        if not done:
            sounds.play("habit_checkin")
        if not done and date == _today_str() and habit.get("auto_log") \
                and not rec["note"].strip():
            self._open_log(date, "")
        self.reload()
        self.changed.emit()

    def _open_log(self, date: str, initial: str) -> None:
        if not self._habit:
            return
        hid = self._habit["id"]
        pop = NotePopup(f"{date} 的打卡日志", initial, self)
        pop.accepted.connect(lambda text: self._save_log(hid, date, text))
        self._popup = pop
        _place_popup(pop, self.title_lbl)
        pop.show()
        pop.focus_editor()

    def _save_log(self, habit_id: int, date: str, text: str) -> None:
        services.habit_set_note(habit_id, date, text)
        self.reload()
        self.changed.emit()

    # ---- 日志 ----
    def _build_logs(self, checks: dict) -> None:
        while self.log_list.count():
            it = self.log_list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        month = self.calendar.month()
        prefix = month.toString("yyyy-MM")
        # 参考图里打了卡但没写字的日子并不进日志，日志区整块显示空提示 ——
        # 「打卡日志」记的是文字，不是打卡本身（打卡在上面的月历里已经看得到了）。
        rows = sorted((ds, rec) for ds, rec in checks.items()
                      if ds.startswith(prefix) and (rec.get("note") or "").strip())
        self.log_title.setText(f"{month.month()}月打卡日志")
        self.log_empty.setVisible(not rows)
        for ds, rec in rows:
            qd = QDate.fromString(ds, "yyyy-MM-dd")
            row = QFrame()
            row.setObjectName("HabitLogRow")
            h = QHBoxLayout(row)
            h.setContentsMargins(8, 5, 8, 5)
            h.setSpacing(8)
            date_lbl = QLabel(f"{qd.month()}月{qd.day()}日  ×{rec['count']}")
            date_lbl.setObjectName("HabitLogDate")
            h.addWidget(date_lbl)
            text = QLabel(rec["note"].strip())
            text.setObjectName("HabitLogText")
            h.addWidget(text, 1)
            btn = QPushButton("写日志")
            btn.setObjectName("LinkBtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, d=ds: self._edit_log(d))
            h.addWidget(btn)
            self.log_list.addWidget(row)

    def _edit_log(self, date: str) -> None:
        if not self._habit:
            return
        rec = services.habit_checks_map(self._habit["id"]).get(date, {})
        self._open_log(date, rec.get("note", ""))

    # ---- 菜单 ----
    def _show_menu(self) -> None:
        """菜单本身交给 HabitPane 统一构建，两处（详情 ⋯ / 行右键）长得一样。"""
        if self._habit:
            self.moreRequested.emit(self._habit)


# ---------------------------------------------------------------------------
# 整体两栏
# ---------------------------------------------------------------------------
class _RowsArea(QWidget):
    """习惯行的容器：接拖拽、画插入线。

    Windows 上的 QDrag 走 OLE、要真实光标移动才起得来，QTest 模拟不了整条链路，
    所以这里只负责「把事件翻成 rows_holder 内的 y 坐标」，落点计算和落库都在
    HabitPane 的方法上 —— 测试可以直接调那两个方法验算。
    """

    MIME = "application/x-life-habit"

    def __init__(self, pane: "HabitPane", parent: QWidget | None = None):
        super().__init__(parent)
        self.pane = pane
        self.setAcceptDrops(True)
        self.line = QFrame(self)
        self.line.setObjectName("HabitDropLine")
        self.line.setFixedHeight(2)
        self.line.hide()

    @staticmethod
    def _drag_id(event) -> int | None:
        m = event.mimeData()
        if not m.hasFormat(_RowsArea.MIME):
            return None
        try:
            return int(bytes(m.data(_RowsArea.MIME)).decode())
        except (ValueError, UnicodeDecodeError):
            return None

    def _y(self, event) -> int:
        return event.position().toPoint().y()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._drag_id(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        hid = self._drag_id(event)
        if hid is None:
            event.ignore()
            return
        self._show_line(self.pane._drop_target(self._y(event), hid))
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.line.hide()

    def dropEvent(self, event) -> None:  # noqa: N802
        hid = self._drag_id(event)
        self.line.hide()
        if hid is None:
            event.ignore()
            return
        event.acceptProposedAction()
        tgt = self.pane._drop_target(self._y(event), hid)
        if tgt is not None:
            group, before, _ = tgt
            self.pane._apply_drop(hid, group, before)

    def _show_line(self, target) -> None:
        if target is None:
            self.line.hide()
            return
        _, _, ly = target
        self.line.setGeometry(0, ly, self.width(), 2)
        self.line.show()
        self.line.raise_()


class HabitPane(QWidget):
    """习惯打卡两栏：左列表（坚持中/已归档）+ 右详情。"""

    # (习惯名, "pomodoro" | "countup") —— 主窗口据此跳到番茄钟页并开计时
    focusRequested = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tab = "active"     # active / archived
        self._current_id: int | None = None
        self._row_widgets: dict[int, HabitRow] = {}
        # 每个习惯所在分组的有序 id 列表，「上移 / 下移」按组内位置决定给不给
        self._row_group: dict[int, list[int]] = {}

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 左栏 ----
        left = QFrame()
        left.setObjectName("HabitListPane")
        # 不用 setFixedWidth：窗口一窄整页就撑不下去。上限 452 保持滴答的比例，
        # 下限让它跟着窗口缩，覆盖模式下再放开。
        self.left = left
        left.setMinimumWidth(240)
        left.setMaximumWidth(452)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(24, 18, 14, 14)
        ll.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.pane_title = QLabel("习惯")
        self.pane_title.setObjectName("HabitPaneTitle")
        head.addWidget(self.pane_title)
        head.addStretch(1)
        self.seg_group = QButtonGroup(self)
        self.seg_active = QPushButton("坚持中")
        self.seg_archived = QPushButton("已归档")
        for i, bt in enumerate((self.seg_active, self.seg_archived)):
            bt.setCheckable(True)
            bt.setObjectName("SegBtn")
            bt.setCursor(Qt.CursorShape.PointingHandCursor)
            self.seg_group.addButton(bt, i)
            head.addWidget(bt)
        head.addStretch(1)
        self.seg_active.setChecked(True)
        self.seg_group.idToggled.connect(
            lambda idx, ck: self._set_tab("active" if idx == 0 else "archived")
            if ck else None)
        head.addStretch(1)
        self.add_btn = QPushButton("＋")
        self.add_btn.setObjectName("SettingsBtn")
        self.add_btn.setToolTip("新建习惯")
        self.add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_btn.clicked.connect(self._add_habit)
        head.addWidget(self.add_btn)
        self.more_btn = QPushButton("⋯")
        self.more_btn.setObjectName("SettingsBtn")
        self.more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more_btn.clicked.connect(self._list_menu)
        head.addWidget(self.more_btn)
        ll.addLayout(head)

        self.rows_holder = _RowsArea(self)
        self.rows_lay = QVBoxLayout(self.rows_holder)
        self.rows_lay.setContentsMargins(0, 0, 0, 0)
        self.rows_lay.setSpacing(2)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.rows_holder)
        ll.addWidget(self.scroll, 1)

        # ---- 右栏 ----
        self.detail = HabitDetail()
        self.detail.changed.connect(self.reload)
        self.detail.moreRequested.connect(
            lambda h: self._habit_menu(h, self.detail.more_btn))
        self.detail.closeRequested.connect(self._close_detail)

        root.addWidget(left)
        root.addWidget(self.detail, 1)
        self._compact = False
        self.reload()
        self._seen_rev = services.habit_rev()
        self._apply_mode()

    # ---- 视图 ----
    COMPACT_BELOW = 900        # 窄于此宽度：详情从右边推上来，列表收成窄列
    COMPACT_LIST_W = 290       # 详情推上来时列表留多宽：表头实测 242 + 左右边距 38

    def showEvent(self, event) -> None:  # noqa: N802
        """从别的页切回来时，如果打卡数据在别处被改过就重建。

        待办页底部也挂着「今日打卡」行，在那边打了卡切回这页，点阵还停在旧状态。
        """
        super().showEvent(event)
        rev = services.habit_rev()
        if rev != self._seen_rev:
            self._seen_rev = rev
            self.reload()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_mode()

    def _apply_mode(self) -> None:
        """窄窗口下别把整屏换成详情：列表一直留在左边（收成一条窄列），
        点开某个习惯才把详情从右边推上来。

        覆盖式的问题是点完一行就只剩详情，想换看别的习惯必须先退回列表 ——
        而窗口窄的时候恰恰最常连着看好几个。滴答也是留列表 + 右推详情。
        """
        compact = self.width() < self.COMPACT_BELOW
        if compact == self._compact:
            return
        self._compact = compact
        self.detail.close_btn.setVisible(compact)
        if compact:
            # 滴答窄窗口下先给你列表，不是详情；这里把自动选中的那行退回未选
            self._current_id = None
            self.detail.show_habit(None)
        elif self._current_id is None:
            # 反过来也要补回选中：构造时宽度还没生效，会先进一次紧凑模式，
            # 不补回来的话拉宽窗口后右栏就一直是空的。
            habits = services.habit_list(archived=1 if self._tab == "archived" else 0)
            if habits:
                self._current_id = habits[0]["id"]
                self.detail.show_habit(habits[0])
                for hid, row in self._row_widgets.items():
                    widgets._apply_property(row, "selected",
                                            "true" if hid == self._current_id else "false")
        self._sync_panes()

    def _sync_panes(self) -> None:
        if not self._compact:
            self.left.show()
            self.left.setMinimumWidth(240)
            self.left.setMaximumWidth(452)
            self.pane_title.setVisible(True)
            self.detail.show()
            for row in self._row_widgets.values():
                row.set_compact(False)
            return
        showing = self._current_id is not None
        # 窄到连「列表 290 + 详情」都放不下时退回覆盖式。要的是详情 content 的
        # 实时最小宽，别写死数字：之前写 414，后来月历改成能压缩、真值是 274，
        # 那个常量就把 700 以下全打成覆盖，还没人发现。
        need = self.detail.content.minimumSizeHint().width()
        cover = showing and (self.width() - self.COMPACT_LIST_W) < need
        self.detail.setVisible(showing)
        self.left.setVisible(not cover)
        # 没推出详情时列表要铺满整宽。之前这里给的是 452 上限：布局里唯一
        # 带拉伸的详情被藏掉了，多出来的两百多像素没处去，就把列表整块推到
        # 中间（实测 left.x = 117/124/64），看起来像莫名其妙居中了。
        self.left.setMinimumWidth(180 if showing else 240)
        self.left.setMaximumWidth(self.COMPACT_LIST_W if showing else 16777215)
        # 窄列里「习惯」这俩字和分段控件、＋ ⋯ 挤不下；侧边栏已经标了这是哪页
        self.pane_title.setVisible(not showing)
        for row in self._row_widgets.values():
            row.set_compact(showing)

    def _close_detail(self) -> None:
        self._current_id = None
        self.detail.show_habit(None)
        self.reload()

    def _set_tab(self, tab: str) -> None:
        self._tab = tab
        self._current_id = None
        self.reload(keep_scroll=False)     # 换的是另一份清单，回顶部才对

    def reload(self, keep_scroll: bool = True,
               reveal_id: int | None = None) -> None:
        # 整表重建会把滚动条甩回顶部：新建 / 编辑 / 归档 / 挪顺序之后视野都跳走，
        # 而新建的习惯一律排在最后，等于「点了保存什么都没发生」。
        saved = self.scroll.verticalScrollBar().value()
        while self.rows_lay.count():
            it = self.rows_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        archived = 1 if self._tab == "archived" else 0
        buckets = services.habit_list_grouped(archived=archived)
        habits = [h for _, hs in buckets for h in hs]
        # 紧凑模式下不自动选中：一自动选就把详情推上来，✕ 永远退回不去列表
        if self._current_id is None and habits and not self._compact:
            self._current_id = habits[0]["id"]
        found = False
        self._row_widgets = {}
        self._row_group = {}
        all_checks = services.habit_checks_maps([h["id"] for h in habits])
        # 只有一个分组时不顶一行标题出来，白占高度
        show_heads = len(buckets) > 1
        for gname, hs in buckets:
            ids = [h["id"] for h in hs]
            for hid in ids:
                self._row_group[hid] = ids
            if show_heads:
                cap = QLabel(gname)
                cap.setObjectName("HabitGroupHead")
                self.rows_lay.addWidget(cap)
            for h in hs:
                checks = all_checks.get(h["id"], {})
                row = HabitRow(h, checks, selected=h["id"] == self._current_id)
                row.selected.connect(self._select)
                row.toggled.connect(self._toggle_check)
                row.contextRequested.connect(
                    lambda hid, w: self._habit_menu(services.habit_get(hid), w))
                self.rows_lay.addWidget(row)
                self._row_widgets[h["id"]] = row
                if h["id"] == self._current_id:
                    found = True
        self.rows_lay.addStretch(1)
        if not habits:
            hint = QLabel("还没有习惯，点击 ＋ 新建一个吧")
            hint.setObjectName("DetailEmpty")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.rows_lay.addWidget(hint)
        if not found:
            self._current_id = None
            self.detail.show_habit(None)
        else:
            habit = next(h for h in habits if h["id"] == self._current_id)
            self.detail.show_habit(habit)
        self._sync_panes()

        # 几何要等下一帧布局跑完才定得下来，同帧滚是滚不动的
        if reveal_id is not None:
            self._reveal(reveal_id)
        elif keep_scroll:
            sb = self.scroll.verticalScrollBar()
            QTimer.singleShot(0, lambda: sb.setValue(saved))
        else:
            sb = self.scroll.verticalScrollBar()
            # 光靠「不恢复」是回不了顶的：Qt 在新范围仍然容得下旧值时会留着不动
            QTimer.singleShot(0, lambda: sb.setValue(0))

    def _reveal(self, habit_id: int) -> None:
        """把某一习惯滚进视野。

        试两次而不是一次：整表重建之后滚动范围还会再变一两帧（行高、分组标题的
        padding、ElidedLabel 重新截字），实测第一次 ensureWidgetVisible 只滚到
        当时的 max=944，随后范围涨到 1030，新行还是差一屏。
        """
        def once() -> None:
            row = self._row_widgets.get(habit_id)
            if row is None:
                return
            self.scroll.ensureWidgetVisible(row, 0, 60)

        QTimer.singleShot(0, once)
        QTimer.singleShot(140, once)

    def _select(self, habit_id: int) -> None:
        """换选中只翻两行的属性，不重建列表。

        走 reload() 的话每次点击都要销毁重建所有行：悬停态被清掉、
        列表越长越明显地闪一下。
        """
        if habit_id == self._current_id and not self._compact:
            return
        prev = self._row_widgets.get(self._current_id or 0)
        if prev is not None:
            widgets._apply_property(prev, "selected", "false")
        self._current_id = habit_id
        cur = self._row_widgets.get(habit_id)
        if cur is not None:
            widgets._apply_property(cur, "selected", "true")
        habit = services.habit_get(habit_id)
        self.detail.show_habit(habit)
        self._sync_panes()

    # ---- 拖拽排序 ----
    def _drop_target(self, y: int, dragging: int):
        """光标在 rows_holder 内的 y 坐标 → ``(落到哪个组, 排在哪条之前, 插入线 y)``。

        落点所属的组取「光标压在谁身上」那一行，不是取下一行 —— 不然瞄着某组
        最后一行的下半段松手，会被判给下一组，白改一次分组。
        组内落在上半段 = 插到它前面，下半段 = 插到它后面（也就是下一行前面；
        下一行已经出了这个组，就顺延到本组末尾）。
        """
        lines = [self.rows_lay.itemAt(i).widget()
                 for i in range(self.rows_lay.count())]
        rows = [w for w in lines
                if isinstance(w, HabitRow) and w.habit["id"] != dragging]
        if not rows:
            return None
        for k, w in enumerate(rows):
            rect = w.geometry()
            g = str(w.habit.get("group_name") or "其他")
            if y < rect.top():
                return g, w.habit["id"], rect.top() - 1
            if y <= rect.bottom():
                if y < rect.center().y():
                    return g, w.habit["id"], rect.top() - 1
                nxt = rows[k + 1] if k + 1 < len(rows) else None
                ng = (str(nxt.habit.get("group_name") or "其他")
                      if nxt else None)
                if nxt is None or ng != g:
                    return g, None, rect.bottom() + 1
                return g, nxt.habit["id"], nxt.geometry().top() - 1
        last = rows[-1]
        return (str(last.habit.get("group_name") or "其他"), None,
                last.geometry().bottom() + 1)

    def _apply_drop(self, habit_id: int, group: str,
                    before_id: int | None) -> None:
        if services.habit_move_to(habit_id, group, before_id):
            self.reload()

    # ---- 打卡 ----
    def _toggle_check(self, habit_id: int, date: str) -> None:
        habit = services.habit_get(habit_id)
        if not habit:
            return
        checks = services.habit_checks_map(habit_id)
        rec = checks.get(date, {"count": 0, "note": ""})
        done = rec["count"] >= services.habit_goal_count(habit)
        # 频率没排到的日子不给打（「每周三」的周一）；已经打过的还允许撤掉，
        # 不然历史上误打的记录就永久留在库里撤不掉了。
        qd = QDate.fromString(date, "yyyy-MM-dd")
        if not done and qd.isValid() and not services.habit_due_on(habit, qd):
            return
        new_count = 0 if done else services.habit_goal_count(habit)
        if habit.get("goal_type") == "amount" and habit.get("goal_auto") \
                and not done and rec["count"] > 0:
            new_count = rec["count"] + habit.get("goal_each", 1)
            if new_count > services.habit_goal_count(habit):
                new_count = services.habit_goal_count(habit)
        services.habit_set_count(habit_id, date, new_count)
        # 计数型习惯一次点击可能只加一步，没到目标不算打卡成功
        if not done and new_count >= services.habit_goal_count(habit):
            sounds.play("habit_checkin")
        row = self._row_widgets.get(habit_id)
        if row is not None:
            row.habit = habit
            row.refresh(services.habit_checks_map(habit_id))
        else:
            self.reload()
        if habit_id == self._current_id:
            self.detail.reload()
        # 先让点阵变成已打卡，再问日志：反过来会让人以为刚才那一下没点上
        if not done and date == _today_str() and habit.get("auto_log") \
                and not rec["note"].strip():
            self._ask_note(habit_id, date, row or self.add_btn)

    def _ask_note(self, habit_id: int, date: str,
                  anchor: QWidget) -> None:
        pop = NotePopup(f"{date} 的打卡日志", "", self)
        pop.accepted.connect(
            lambda text: self._save_note_and_show(habit_id, date, text))
        self._popup = pop
        # 挂在被点的那一行上。以前固定挂 add_btn（列表右上角的 ＋），
        # 在长列表底部打个卡，日志框却从顶上冒出来，看着不像同一件事。
        _place_popup(pop, anchor)
        pop.show()
        pop.focus_editor()

    def _save_note_and_show(self, habit_id: int, date: str,
                            text: str) -> None:
        services.habit_set_note(habit_id, date, text)
        # 只写库不刷界面：日志那一栏要等下一次别的刷新才冒出这条，
        # 用户写完点保存看着像没存进去。
        row = self._row_widgets.get(habit_id)
        if row is not None:
            row.refresh(services.habit_checks_map(habit_id))
        if self._current_id == habit_id:
            self.detail.reload()

    # ---- 增删改 ----
    def _add_habit(self) -> None:
        dlg = HabitDialog(parent=self)
        if dlg.exec():
            new_id = dlg.habit.get("id")
            self._current_id = new_id
            if self._tab != "active":
                self.seg_active.setChecked(True)
            # 新建的排在最后，不滚过去用户看不到自己刚建了什么
            self.reload(reveal_id=new_id)

    def _edit_habit(self, habit: dict) -> None:
        dlg = HabitDialog(services.habit_get(habit["id"]), parent=self)
        if dlg.exec():
            self.reload()

    def _archive_habit(self, habit: dict) -> None:
        new_val = 0 if habit.get("archived") else 1
        services.habit_update(habit["id"], archived=new_val)
        if new_val and self._tab == "active":
            self._current_id = None
        self.reload()

    def _delete_habit(self, habit: dict, anchor: QWidget | None = None) -> None:
        """删除不可逆，先在触发它的那枚 ⋯ 下方弹一张确认小卡。"""
        anchor = anchor or self.detail.more_btn
        pop = ConfirmPopup(f"确定删除「{habit['name']}」及其全部打卡记录吗？",
                           parent=anchor)
        pop.accepted.connect(lambda: self._do_delete(habit["id"]))
        self._popup = pop
        _place_popup(pop, anchor)
        pop.show()

    def _do_delete(self, habit_id: int) -> None:
        services.habit_delete(habit_id)
        self._current_id = None
        self.reload()

    # ---- 单个习惯的操作菜单（详情 ⋯ 与行右键共用） ----
    def _habit_menu(self, habit: dict | None, anchor: QWidget) -> None:
        if not habit:
            return
        from .todo import TickMenu
        archived = bool(habit.get("archived"))
        # 列表一直是按 sort_order 排的，可除了新建谁也没写过它 —— 顺序定了就改不了。
        # 拖拽在 QTest 里验不了（Windows 的 QDrag 要真实光标），所以给菜单项。
        ids = self._row_group.get(habit["id"], [])
        pos = ids.index(habit["id"]) if habit["id"] in ids else -1
        items = [("edit", "edit", "编辑"),
                 ("focus", "habit", "开始专注")]
        if pos > 0:
            items.append(("up", "arrow_up", "上移"))
        if 0 <= pos < len(ids) - 1:
            items.append(("down", "arrow_down", "下移"))
        items += [("archive", "archive", "取消归档" if archived else "归档"),
                  ("trash", "trash", "删除")]
        menu = TickMenu(items, anchor, danger=("trash",))
        menu.picked.connect(lambda v: self._on_habit_menu(v, habit, anchor))
        self._popup = menu
        _place_popup(menu, anchor)
        menu.show()

    def _on_habit_menu(self, value: object, habit: dict, anchor: QWidget) -> None:
        key = str(value)
        if key == "edit":
            self._edit_habit(habit)
        elif key == "archive":
            self._archive_habit(habit)
        elif key == "trash":
            self._delete_habit(habit, anchor)
        elif key == "focus":
            self._focus_menu(habit, anchor)
        elif key in ("up", "down"):
            if services.habit_move(habit["id"], -1 if key == "up" else 1):
                self.reload()

    def _focus_menu(self, habit: dict, anchor: QWidget) -> None:
        """滴答的「开始专注」是二级菜单：番茄专注 / 正计时。"""
        from .todo import TickMenu
        items = [("pomodoro", "clock", "开始番茄专注"),
                 ("countup", "duration", "开始正计时")]
        menu = TickMenu(items, anchor)
        menu.picked.connect(
            lambda v: self.focusRequested.emit(habit["name"], str(v)))
        self._popup = menu
        _place_popup(menu, anchor)
        menu.show()

    # ---- 列表右上角 ⋯：打卡设置 / 导出 ----
    def _list_menu(self) -> None:
        from .todo import TickMenu
        items = [("settings", "edit", "打卡设置"),
                 ("export", "archive", "导出")]
        menu = TickMenu(items, self.more_btn)
        menu.picked.connect(self._on_list_menu)
        self._popup = menu
        _place_popup(menu, self.more_btn)
        menu.show()

    def _on_list_menu(self, value: object) -> None:
        if str(value) == "settings":
            self._open_checkin_settings()
        elif str(value) == "export":
            self._export_xlsx()

    def _open_checkin_settings(self) -> None:
        pop = CheckinSettingsPopup(self)
        self._popup = pop
        _place_popup(pop, self.more_btn)
        pop.show()

    def _export_xlsx(self) -> None:
        """按滴答「习惯导出」的格式写 xlsx：一个习惯一个 sheet。"""
        from PySide6.QtWidgets import QFileDialog
        from .. import xlsx
        sheets, n_habits, n_checks = services.habit_export_sheets()
        if not sheets:
            popups.notify(self, "导出", "还没有任何习惯，先去新建一个吧。")
            return
        stamp = QDate.currentDate().toString("yyyyMMdd")
        chosen, _ = QFileDialog.getSaveFileName(
            self, "导出习惯", f"习惯_{stamp}.xlsx", "Excel 工作簿 (*.xlsx)")
        if not chosen:
            return
        if not chosen.lower().endswith(".xlsx"):
            chosen += ".xlsx"
        try:
            xlsx.write_xlsx(chosen, sheets)
        except OSError as e:
            popups.notify(self, "导出失败", str(e), danger=True)
            return
        popups.notify(self, "导出完成",
                      f"已导出 {n_habits} 个习惯、{n_checks} 条打卡记录到：\n{chosen}")


class HabitPage(HabitPane):
    """侧边栏「习惯打卡」入口。

    待办清单里只以「今日打卡」分组内嵌打卡行（对齐滴答清单），
    习惯的新建 / 统计 / 归档都在这页。
    """

    def __init__(self):
        super().__init__()
        self.setObjectName("HabitPage")
