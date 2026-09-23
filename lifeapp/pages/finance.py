"""理财管理模块：记账、预算、统计、账户资产、周期账单。

交互约定（与常见记账 App 对齐）：
- 记账/资产记录都以"账户余额"为核心：记一笔可关联账户，保存后自动调整该账户余额；
- 任何记录都可编辑、删除（删除需确认），不再只能删了重记；
- 明细按日期分组，可按类型/分类/关键词筛选；
- 统计范围（本月/上月/本年/全部）同时驱动概览卡片与趋势图；
- 资产总览归属切换（汇总/我/伴侣/对比）只在资产页生效；
- 分类、账户、归属均带矢量图标徽章（widgets.IconBadge）。
"""
from __future__ import annotations

import uuid

from PySide6.QtCore import Qt, QDate, QSize, Signal
from PySide6.QtGui import QIcon, QColor, QPainter
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QPushButton, QDateEdit, QDoubleSpinBox, QComboBox, QScrollArea, QFrame,
    QProgressBar, QDialog, QListWidget, QListWidgetItem, QSpinBox,  QTabWidget, QButtonGroup, QAbstractSpinBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QToolButton,  QMenu,
    QAbstractItemView, QStyledItemDelegate)

from .. import popups, services, sounds, theme, widgets, db
from .base import stats_row

EXPENSE_CATS = ["餐饮", "交通", "购物", "住房", "娱乐", "医疗", "教育", "其他"]
INCOME_CATS = ["工资", "奖金", "理财", "兼职", "其他"]

ACCENT_CYCLE = ["accent", "green", "blue", "amber", "red", "muted"]

OWNER_LABEL = {"self": "我", "partner": "伴侣"}


def _money(v: float, sign: bool = False) -> str:
    """金额格式化：千分位 + 正负号。"""
    s = f"{abs(v):,.2f}"
    if sign:
        return f"{'+' if v >= 0 else '-'}{s}"
    return s


def _accent_for(index: int) -> str:
    return ACCENT_CYCLE[index % len(ACCENT_CYCLE)]


COLOR_OPTIONS = [
    ("accent", "紫色"), ("green", "绿色"), ("blue", "蓝色"),
    ("amber", "橙色"), ("red", "红色"), ("muted", "灰色"),
]


def _account_color(name: str) -> str | None:
    """账户的自定义配色键；未设置返回 None（用默认）。"""
    color = services.asset_account_colors().get(name)
    return color or None


def _category_style(name: str) -> tuple[str | None, str | None]:
    """分类的自定义 (图标, 颜色)；未自定义返回 (None, None)。"""
    st = services.custom_category_styles().get(name)
    if not st:
        return None, None
    return st.get("icon") or None, st.get("color") or None


def _category_icon(name: str, size: int = 26) -> QIcon:
    icon, color = _category_style(name)
    return QIcon(widgets.icon_pixmap(name, "category", size,
                                     color=color, icon=icon))


def _category_badge(name: str, size: int = 34) -> "widgets.IconBadge":
    icon, color = _category_style(name)
    return widgets.IconBadge(name, "category", size, color=color, icon=icon)


def _account_icon(name: str, size: int = 22, color: str | None = None) -> QIcon:
    return QIcon(widgets.icon_pixmap(name, "account", size, color))


def _fill_accounts(combo: QComboBox, accounts: list[str],
                   with_none: bool = True) -> None:
    """账户下拉：带图标（含自定义配色）；with_none 时首项为「不关联」。"""
    combo.clear()
    if with_none:
        combo.addItem("不关联", "")
    for a in accounts:
        combo.addItem(_account_icon(a, color=_account_color(a)), a, a)


class ColorPickerDialog(QDialog):
    """账户配色选择：一组预设色块。"""

    def __init__(self, parent, current: str = ""):
        super().__init__(parent)
        self.setWindowTitle("选择账户配色")
        self._color = current
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        tip = QLabel("配色会同时决定账户图标的前景色与背景色")
        tip.setObjectName("Muted")
        lay.addWidget(tip)

        row = QHBoxLayout()
        row.setSpacing(12)
        self._btns: list[tuple[str, QPushButton]] = []
        for key, label in COLOR_OPTIONS:
            b = QPushButton()
            b.setFixedSize(40, 40)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(label)
            b.clicked.connect(lambda _=False, k=key: self._pick(k))
            row.addWidget(b)
            self._btns.append((key, b))
        row.addStretch(1)
        lay.addLayout(row)
        self._sync_styles()

        btns = QHBoxLayout()
        reset = QPushButton("恢复默认")
        reset.setObjectName("Ghost")
        reset.clicked.connect(self._reset)
        btns.addWidget(reset)
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        lay.addLayout(btns)

    def _sync_styles(self) -> None:
        for key, b in self._btns:
            fg = theme.get(key)
            if key == self._color:
                b.setStyleSheet(
                    f"background: {fg}; border-radius: 20px; "
                    f"border: 3px solid {theme.get('text_hi')};")
            else:
                b.setStyleSheet(
                    f"background: {fg}; border-radius: 20px; "
                    f"border: 2px solid {theme.get('border')};")

    def _pick(self, key: str) -> None:
        self._color = key
        self.accept()

    def _reset(self) -> None:
        self._color = ""
        self.accept()

    @property
    def color(self) -> str:
        return self._color


class CategoryStyleDialog(QDialog):
    """分类图标 + 颜色选择器。"""

    def __init__(self, parent, icon: str = "", color: str = ""):
        super().__init__(parent)
        self._icon = icon or "dots"
        self._color = color or "accent"
        self.setWindowTitle("选择图标与颜色")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        lay.addWidget(self._section("图标"))
        self._grid = QGridLayout()
        self._grid.setSpacing(6)
        lay.addLayout(self._grid)

        lay.addWidget(self._section("颜色"))
        crow = QHBoxLayout()
        crow.setSpacing(10)
        self._color_btns: list[tuple[str, QPushButton]] = []
        for key, label in COLOR_OPTIONS:
            b = QPushButton()
            b.setFixedSize(34, 34)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(label)
            b.clicked.connect(lambda _=False, k=key: self._pick_color(k))
            crow.addWidget(b)
            self._color_btns.append((key, b))
        crow.addStretch(1)
        lay.addLayout(crow)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        ok = QPushButton("确定")
        ok.setObjectName("Primary")
        ok.clicked.connect(self.accept)
        btns.addWidget(ok)
        lay.addLayout(btns)

        self._build_icons()
        self._sync_colors()

    @staticmethod
    def _section(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("Meta")
        return lbl

    def _build_icons(self) -> None:
        while self._grid.count():
            it = self._grid.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._icon_btns: list[tuple[str, QToolButton]] = []
        for i, (name, label) in enumerate(widgets.ICON_LIBRARY):
            b = QToolButton()
            b.setObjectName("IconPick")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(label)
            b.setFixedSize(56, 56)
            b.setIconSize(QSize(30, 30))
            b.setIcon(QIcon(widgets.icon_pixmap(label, "category", 30,
                                                color=self._color, icon=name)))
            b.setChecked(name == self._icon)
            b.clicked.connect(lambda _=False, n=name: self._pick_icon(n))
            self._grid.addWidget(b, i // 6, i % 6)
            self._icon_btns.append((name, b))

    def _pick_icon(self, name: str) -> None:
        self._icon = name
        for n, b in self._icon_btns:
            b.setChecked(n == name)

    def _pick_color(self, key: str) -> None:
        self._color = key
        self._sync_colors()
        for name, b in self._icon_btns:
            b.setIcon(QIcon(widgets.icon_pixmap(name, "category", 30,
                                                color=self._color, icon=name)))

    def _sync_colors(self) -> None:
        for key, b in self._color_btns:
            fg = theme.get(key)
            if key == self._color:
                b.setStyleSheet(
                    f"background: {fg}; border-radius: 17px; "
                    f"border: 3px solid {theme.get('text_hi')};")
            else:
                b.setStyleSheet(
                    f"background: {fg}; border-radius: 17px; "
                    f"border: 2px solid {theme.get('border')};")

    @property
    def icon(self) -> str:
        return self._icon

    @property
    def color(self) -> str:
        return self._color


class SummaryHero(QFrame):
    """顶部汇总卡：大号主数值 + 若干指标 + 进度条（HeroCard 渐变底）。"""

    def __init__(self, caption: str = "结余", parent: QWidget | None = None,
                 action: str = ""):
        super().__init__(parent)
        self.setObjectName("HeroCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        inner = QFrame()
        inner.setObjectName("HeroInner")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(18, 14, 18, 16)
        lay.setSpacing(10)
        outer.addWidget(inner)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.caption = QLabel(caption)
        self.caption.setObjectName("HeroCaption")
        top.addWidget(self.caption)
        self.extra = QLabel("")
        self.extra.setObjectName("HeroCaption")
        top.addWidget(self.extra)
        top.addStretch(1)
        self.action_btn = QPushButton(action) if action else None
        if self.action_btn is not None:
            self.action_btn.setObjectName("LinkBtn")
            self.action_btn.setCursor(Qt.PointingHandCursor)
            top.addWidget(self.action_btn)
        lay.addLayout(top)

        self.big = QLabel("¥0.00")
        self.big.setObjectName("HeroBig")
        lay.addWidget(self.big)

        self.metrics_holder = QWidget()
        self.metrics = QHBoxLayout(self.metrics_holder)
        self.metrics.setContentsMargins(0, 0, 0, 0)
        self.metrics.setSpacing(26)
        lay.addWidget(self.metrics_holder)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        lay.addWidget(self.bar)

        self.note = QLabel("")
        self.note.setObjectName("Hint")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

    def set_caption(self, text: str, extra: str = "") -> None:
        self.caption.setText(text)
        self.extra.setText(extra)

    def set_value(self, text: str) -> None:
        self.big.setText(text)

    def set_metrics(self, items: list[tuple[str, str, str]]) -> None:
        """items: [(名称, 数值, 颜色键)]；数量变化时自动重建。

        每个指标用独立 QWidget 承载（值 + 标签两行），这样清理时
        takeAt().widget() 能拿到并 deleteLater，不会残留叠加。
        """
        while self.metrics.count():
            item = self.metrics.takeAt(0)
            w = item.widget()
            if w:
                w.hide()        # deleteLater 是延迟销毁，先隐藏避免残影
                w.deleteLater()
        for name, value, color in items:
            unit = QWidget()
            v = QVBoxLayout(unit)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(1)
            vl = QLabel(value)
            vl.setObjectName("HeroMetricVal")
            widgets._apply_property(vl, "strongColor", color)
            nl = QLabel(name)
            nl.setObjectName("HeroMetricName")
            v.addWidget(vl)
            v.addWidget(nl)
            self.metrics.addWidget(unit)
        self.metrics.addStretch(1)

    def set_progress(self, ratio: float, over: bool = False) -> None:
        self.bar.setValue(int(max(0.0, min(1.0, ratio)) * 1000))
        widgets._apply_property(self.bar, "over", "true" if over else "false")

    def set_note(self, text: str, level: str = "") -> None:
        self.note.setText(text)
        widgets._apply_property(self.note, "level", level)


class SegGroup(QWidget):
    """分段切换按钮组（日/月/年、支出/收入等）。"""

    changed = Signal(int)

    def __init__(self, items: list[str], parent: QWidget | None = None,
                 current: int = 0, compact: bool = False):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._btns: list[QPushButton] = []
        for i, text in enumerate(items):
            b = QPushButton(text)
            b.setObjectName("SegBtn")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            if compact:
                b.setStyleSheet("padding: 5px 11px;")
            self._group.addButton(b, i)
            lay.addWidget(b)
            self._btns.append(b)
        self._btns[current].setChecked(True)
        # 用 idToggled 而非 idClicked：后者只在用户点击时发出，
        # 代码里 set_current() 改变选中项时不会通知外部。
        self._group.idToggled.connect(self._on_toggled)

    def _on_toggled(self, index: int, checked: bool) -> None:
        if checked:
            self.changed.emit(index)

    def current_index(self) -> int:
        return self._group.checkedId()

    def set_current(self, index: int) -> None:
        if 0 <= index < len(self._btns):
            self._btns[index].setChecked(True)


class AmountInput(QWidget):
    """大号金额输入：预览 + 输入 + 清空。"""

    textChanged = Signal(str)
    returnPressed = Signal()

    def __init__(self, parent: QWidget | None = None, placeholder: str = "0.00"):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.preview = QLabel("0.00")
        self.preview.setObjectName("HeroWeight")
        self.preview.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head.addStretch(1)
        head.addWidget(self.preview)
        unit = QLabel("元")
        unit.setObjectName("HeroUnit")
        head.addWidget(unit)
        lay.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setAlignment(Qt.AlignRight)
        self.edit.textChanged.connect(self._on_text)
        self.edit.returnPressed.connect(self.returnPressed.emit)
        row.addWidget(self.edit, 1)
        clear = QPushButton("清空")
        clear.setObjectName("Ghost")
        clear.setCursor(Qt.PointingHandCursor)
        clear.clicked.connect(self.edit.clear)
        row.addWidget(clear)
        lay.addLayout(row)

    def _on_text(self, text: str) -> None:
        raw = self.raw()
        if raw is not None:
            self.preview.setText(f"{raw:,.2f}")
        self.textChanged.emit(text)

    def set_text(self, text: str) -> None:
        self.edit.setText(text)

    def raw(self) -> float | None:
        """数值；非法输入返回 None（空视为 0）。"""
        text = self.edit.text().strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return None

    def value(self) -> float:
        return self.raw() or 0.0

    def set_focus(self) -> None:
        self.edit.setFocus()
        self.edit.selectAll()


class CategoryPicker(QWidget):
    """分类选择：图标 + 文字的方格，一次点击完成选择。"""

    changed = Signal(str)

    def __init__(self, parent: QWidget | None = None, columns: int = 6):
        super().__init__(parent)
        self._columns = columns
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(8)
        self._btns: list[QToolButton] = []
        self._current = ""

    def set_categories(self, cats: list[str], selected: str = "") -> None:
        for b in self._btns:
            b.deleteLater()
        self._btns = []
        for i, c in enumerate(cats):
            b = QToolButton()
            b.setObjectName("CatChip")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setIcon(_category_icon(c, 26))
            b.setIconSize(QSize(26, 26))
            b.setFixedSize(80, 64)
            fm = b.fontMetrics()
            b.setText(fm.elidedText(c, Qt.ElideRight, 72))
            b.setToolTip(c)
            b.clicked.connect(lambda _=False, name=c: self._pick(name))
            self._grid.addWidget(b, i // self._columns, i % self._columns,
                                 Qt.AlignLeft)
            self._btns.append(b)
        self._current = selected if selected in cats else (cats[0] if cats else "")
        self._sync_checked()

    def _pick(self, name: str) -> None:
        self._current = name
        self._sync_checked()
        self.changed.emit(name)

    def _sync_checked(self) -> None:
        for b in self._btns:
            b.setChecked(b.text() == self._current or b.toolTip() == self._current)

    def current(self) -> str:
        return self._current

    def categories(self) -> list[str]:
        return [b.toolTip() for b in self._btns]


class AccountCard(QFrame):
    """可点击的账户余额卡片。"""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("AccountCard")
        self.setCursor(Qt.PointingHandCursor)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(12, 10, 12, 10)
        self._lay.setSpacing(10)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)


class CategoryDialog(QDialog):
    """自定义分类管理（支出/收入共用，顶部切换；支持图标与颜色）。"""

    def __init__(self, parent, is_expense: bool = True):
        super().__init__(parent)
        self.setWindowTitle("分类管理")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        self.kind_seg = SegGroup(["支出分类", "收入分类"], self,
                                 0 if is_expense else 1)
        self.kind_seg.changed.connect(lambda _i: self._reload())
        lay.addWidget(self.kind_seg)

        self.list_widget = QListWidget()
        self.list_widget.setSpacing(2)
        lay.addWidget(self.list_widget, 1)

        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("新分类名（可逗号分隔多个）")
        self.input.returnPressed.connect(self._add)
        row.addWidget(self.input, 1)
        add_btn = QPushButton("添加")
        add_btn.setObjectName("Primary")
        add_btn.clicked.connect(self._add)
        row.addWidget(add_btn)
        lay.addLayout(row)

        btns = QHBoxLayout()
        self.tip = QLabel("")
        self.tip.setObjectName("Muted")
        btns.addWidget(self.tip)
        btns.addStretch(1)
        close_btn = QPushButton("完成")
        close_btn.setObjectName("Ghost")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(close_btn)
        lay.addLayout(btns)

        self._reload()

    @property
    def kind(self) -> str:
        return "expense" if self.kind_seg.current_index() == 0 else "income"

    def _base(self) -> list[str]:
        return EXPENSE_CATS if self.kind == "expense" else INCOME_CATS

    def _reload(self) -> None:
        self.list_widget.clear()
        for c in services.get_custom_categories(self.kind):
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 46))
            self.list_widget.addItem(item)
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(10, 4, 10, 4)
            h.setSpacing(10)
            h.addWidget(_category_badge(c, 28))
            name = QLabel(c)
            name.setObjectName("RowTitle")
            h.addWidget(name, 1)
            style_btn = QPushButton("图标")
            style_btn.setObjectName("LinkBtn")
            style_btn.setCursor(Qt.PointingHandCursor)
            style_btn.clicked.connect(lambda _=False, n=c: self._edit_style(n))
            h.addWidget(style_btn)
            dele = QPushButton("✕")
            dele.setObjectName("IconBtn")
            dele.setCursor(Qt.PointingHandCursor)
            dele.clicked.connect(lambda _=False, n=c: self._del(n))
            h.addWidget(dele)
            self.list_widget.setItemWidget(item, row)
        self.tip.setText(f"内置 {len(self._base())} 个，自定义 "
                         f"{self.list_widget.count()} 个；点「图标」换图标/颜色")

    def _edit_style(self, name: str) -> None:
        icon, color = _category_style(name)
        dlg = CategoryStyleDialog(self, icon or "", color or "")
        if dlg.exec() != QDialog.Accepted:
            return
        services.set_custom_category_style(name, dlg.icon, dlg.color)
        self._reload()

    def _add(self) -> None:
        text = self.input.text().strip().replace("，", ",")
        if not text:
            return
        base = self._base()
        cats = services.get_custom_categories(self.kind)
        for part in [p.strip() for p in text.split(",") if p.strip()]:
            if part not in cats and part not in base:
                cats.append(part)
        services.set_custom_categories(self.kind, cats)
        self.input.clear()
        self._reload()

    def _del(self, name: str) -> None:
        cats = services.get_custom_categories(self.kind)
        if name in cats:
            cats.remove(name)
            services.set_custom_categories(self.kind, cats)
        self._reload()


class RecordsEditorDialog(QDialog):
    """某时间点（日/月）的记录编辑窗口：列出记录，逐条编辑或删除。"""

    editRequested = Signal(int)
    deleteRequested = Signal(int)

    def __init__(self, parent, title: str, records: list[dict]):
        super().__init__(parent)
        self.setWindowTitle(f"记录 · {title}")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        tip = QLabel("「编辑」可修改这笔记录，「✕」删除（会同步回滚账户余额）")
        tip.setObjectName("Muted")
        lay.addWidget(tip)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        self.list_layout = QVBoxLayout(container)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(2)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(container)
        self.scroll.setMinimumHeight(320)
        lay.addWidget(self.scroll, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("Primary")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        lay.addLayout(btns)

        self._records = records
        self.refresh(records)

    def refresh(self, records: list[dict]) -> None:
        self._records = records
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for r in self._records:
            row = QFrame()
            row.setObjectName("LedgerRow")
            h = QHBoxLayout(row)
            h.setContentsMargins(10, 6, 10, 6)
            h.setSpacing(10)
            h.addWidget(_category_badge(r.get("category", "其他"), 32))
            col = QVBoxLayout()
            col.setSpacing(1)
            note = (r.get("note") or "").removesuffix("（周期）")
            t = widgets.ElidedLabel(note or r.get("category", ""))
            t.setObjectName("RowTitle")
            col.addWidget(t)
            sub_parts = []
            if note:
                sub_parts.append(r.get("category", ""))
            owner = OWNER_LABEL.get(r.get("owner", "self"), "我")
            if r.get("account"):
                sub_parts.append(f"{owner}·{r['account']}")
            sub_parts.append(r["date"])
            s = widgets.ElidedLabel(" · ".join(sub_parts))
            s.setObjectName("DaySum")
            col.addWidget(s)
            h.addLayout(col, 1)

            is_income = r["type"] == "income"
            amt = QLabel(f"{'+' if is_income else '-'}¥{_money(float(r['amount']))}")
            amt.setObjectName("Money")
            widgets._apply_property(amt, "sign",
                                    "income" if is_income else "expense")
            h.addWidget(amt)

            edit = QPushButton("编辑")
            edit.setObjectName("LinkBtn")
            edit.setCursor(Qt.PointingHandCursor)
            edit.clicked.connect(lambda _=False, i=r["id"]:
                                 self.editRequested.emit(i))
            h.addWidget(edit)
            dele = QPushButton("✕")
            dele.setObjectName("IconBtn")
            dele.setCursor(Qt.PointingHandCursor)
            dele.clicked.connect(lambda _=False, i=r["id"]:
                                 self.deleteRequested.emit(i))
            h.addWidget(dele)
            self.list_layout.insertWidget(self.list_layout.count() - 1, row)


class RecordDialog(QDialog):
    """记一笔 / 编辑记录。"""

    def __init__(self, parent, accounts: list[str], record: dict | None = None):
        super().__init__(parent)
        self.record = record
        self._accounts = accounts
        self.setWindowTitle("编辑记录" if record else "记一笔")
        self.setMinimumWidth(560)

        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        self.type_seg = SegGroup(["支出", "收入"], self)
        self.type_seg.changed.connect(self._on_type_change)
        lay.addWidget(self.type_seg)

        self.amount = AmountInput(self)
        self.amount.textChanged.connect(lambda _t: self._refresh_account_hint())
        lay.addWidget(self.amount)

        lay.addWidget(self._section("分类"))
        self.picker = CategoryPicker(self)
        lay.addWidget(self.picker)
        cat_row = QHBoxLayout()
        self.cat_manage_btn = QPushButton("管理分类")
        self.cat_manage_btn.setObjectName("LinkBtn")
        self.cat_manage_btn.setCursor(Qt.PointingHandCursor)
        self.cat_manage_btn.clicked.connect(self._manage_categories)
        cat_row.addWidget(self.cat_manage_btn)
        cat_row.addStretch(1)
        lay.addLayout(cat_row)

        lay.addWidget(self._section("归属与账户（选账户后保存，余额自动同步）"))
        orow = QHBoxLayout()
        orow.setSpacing(8)
        self.owner_seg = SegGroup(["我的", "伴侣的"], self, compact=True)
        self.owner_seg.changed.connect(lambda _i: self._refresh_account_hint())
        orow.addWidget(self.owner_seg)
        orow.addStretch(1)
        lay.addLayout(orow)

        acc_row = QHBoxLayout()
        acc_row.setSpacing(8)
        self.account_combo = widgets.ComboBox()
        _fill_accounts(self.account_combo, accounts)
        self.account_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_account_hint())
        acc_row.addWidget(self.account_combo, 1)
        self.account_hint = QLabel("")
        self.account_hint.setObjectName("Muted")
        acc_row.addWidget(self.account_hint)
        lay.addLayout(acc_row)

        lay.addWidget(self._section("日期"))
        drow = QHBoxLayout()
        drow.setSpacing(6)
        self.date_edit = widgets.DateInput()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setFixedWidth(140)
        drow.addWidget(self.date_edit)
        for label, offset in (("今天", 0), ("昨天", 1), ("前天", 2)):
            b = QPushButton(label)
            b.setObjectName("LinkBtn")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(
                lambda _=False, o=offset: self.date_edit.setDate(
                    QDate.currentDate().addDays(-o)))
            drow.addWidget(b)
        drow.addStretch(1)
        lay.addLayout(drow)

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("备注（可选）")
        lay.addWidget(self.note_edit)

        self.hint = QLabel("")
        self.hint.setObjectName("Hint")
        self.hint.setWordWrap(True)
        self.hint.setVisible(False)
        lay.addWidget(self.hint)

        btns = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(cancel)
        if record:
            save = QPushButton("保存修改")
            save.setObjectName("Primary")
            save.clicked.connect(lambda: self._accept(False))
            btns.addWidget(save)
        else:
            again = QPushButton("保存并继续")
            again.setObjectName("Ghost")
            again.clicked.connect(lambda: self._accept(True))
            btns.addWidget(again)
            save = QPushButton("保存")
            save.setObjectName("Primary")
            save.clicked.connect(lambda: self._accept(False))
            btns.addWidget(save)
        save.setDefault(True)
        self.amount.returnPressed.connect(lambda: self._accept(False))
        lay.addLayout(btns)

        self._refresh_categories()
        self._refresh_account_hint()
        if record:
            self._fill(record)
        self.amount.set_focus()

    @staticmethod
    def _section(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("Meta")
        return lbl

    def _fill(self, r: dict) -> None:
        self.type_seg.set_current(0 if r.get("type", "expense") == "expense" else 1)
        self.owner_seg.set_current(0 if r.get("owner", "self") == "self" else 1)
        self._refresh_categories()
        self.amount.set_text(f"{float(r.get('amount', 0)):.2f}")
        cat = r.get("category", "")
        if cat:
            cats = self.picker.categories()
            if cat not in cats:
                cats.append(cat)
            self.picker.set_categories(cats, cat)
        acc = r.get("account", "") or ""
        idx = self.account_combo.findData(acc)
        if idx < 0 and acc:
            self.account_combo.addItem(_account_icon(acc), acc, acc)
            idx = self.account_combo.findData(acc)
        self.account_combo.setCurrentIndex(max(idx, 0))
        d = QDate.fromString(r.get("date", ""), "yyyy-MM-dd")
        if d.isValid():
            self.date_edit.setDate(d)
        self.note_edit.setText(r.get("note", ""))

    def _categories(self) -> list[str]:
        kind = "expense" if self.type_seg.current_index() == 0 else "income"
        base = EXPENSE_CATS if kind == "expense" else INCOME_CATS
        custom = services.get_custom_categories(kind)
        return list(base) + [c for c in custom if c not in base]

    def _refresh_categories(self) -> None:
        cats = self._categories()
        keep = self.picker.current()
        self.picker.set_categories(cats, keep if keep in cats else cats[0])

    def _on_type_change(self, _idx: int) -> None:
        self._refresh_categories()
        self._refresh_account_hint()

    def _manage_categories(self) -> None:
        CategoryDialog(self, self.type_seg.current_index() == 0).exec()
        self._refresh_categories()

    def _refresh_account_hint(self) -> None:
        acc = self.account_combo.currentData() or ""
        if not acc:
            self.account_hint.setText("")
            return
        bal = services.asset_balance(self.owner, acc)
        if bal is None:
            self.account_hint.setText("该账户还没有余额记录，本次不会同步")
            return
        amt = self.amount.value()
        delta = -amt if self.type_seg.current_index() == 0 else amt
        self.account_hint.setText(
            f"当前 ¥{_money(bal)}　→　¥{_money(bal + delta)}")

    def _show_error(self, text: str) -> None:
        self.hint.setText(text)
        widgets._apply_property(self.hint, "level", "error")
        self.hint.setVisible(True)

    def _accept(self, keep_open: bool) -> None:
        amt = self.amount.raw()
        if amt is None:
            self._show_error("金额请填写数字，例如 38.5")
            self.amount.set_focus()
            return
        if amt <= 0:
            self._show_error("金额需大于 0")
            self.amount.set_focus()
            return
        if not self.picker.current():
            self._show_error("请选择一个分类")
            return
        self.hint.setVisible(False)
        self.done(2) if keep_open else self.accept()

    @property
    def owner(self) -> str:
        return "self" if self.owner_seg.current_index() == 0 else "partner"

    def data(self) -> dict:
        return {
            "date": self.date_edit.date().toString("yyyy-MM-dd"),
            "type": "expense" if self.type_seg.current_index() == 0 else "income",
            "category": self.picker.current(),
            "amount": self.amount.value(),
            "note": self.note_edit.text().strip(),
            "account": self.account_combo.currentData() or "",
            "owner": self.owner,
        }


class BalanceDialog(QDialog):
    """更新账户余额：「设为新余额」或「按增减调整」。"""

    def __init__(self, parent, accounts: list[str], owner: str = "self",
                 account: str = ""):
        super().__init__(parent)
        self.setWindowTitle("更新账户余额")
        self.setMinimumWidth(500)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)
        self.badge = widgets.IconBadge(account or accounts[0] if accounts else "其他",
                                       "account", 40, _account_color(account))
        head.addWidget(self.badge)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(QLabel("调整账户余额"))
        tip = QLabel("选择「设为新余额」直接覆盖，或「按增减调整」填入变动额")
        tip.setObjectName("Muted")
        col.addWidget(tip)
        head.addLayout(col, 1)
        lay.addLayout(head)

        orow = QHBoxLayout()
        orow.setSpacing(8)
        orow.addWidget(QLabel("归属"))
        self.owner_seg = SegGroup(["我", "伴侣"], self,
                                  0 if owner == "self" else 1, compact=True)
        self.owner_seg.changed.connect(lambda _i: self._refresh())
        orow.addWidget(self.owner_seg)
        orow.addStretch(1)
        lay.addLayout(orow)

        arow = QHBoxLayout()
        arow.setSpacing(8)
        arow.addWidget(QLabel("账户"))
        self.account_combo = widgets.ComboBox()
        _fill_accounts(self.account_combo, accounts, with_none=False)
        if account:
            idx = self.account_combo.findData(account)
            if idx >= 0:
                self.account_combo.setCurrentIndex(idx)
        self.account_combo.currentIndexChanged.connect(self._on_account_change)
        arow.addWidget(self.account_combo, 1)
        lay.addLayout(arow)

        self.mode_seg = SegGroup(["设为新余额", "按增减调整"], self)
        self.mode_seg.changed.connect(lambda _i: self._refresh())
        lay.addWidget(self.mode_seg)

        self.amount = AmountInput(self)
        self.amount.textChanged.connect(lambda _t: self._refresh())
        lay.addWidget(self.amount)

        self.result_lbl = QLabel("")
        self.result_lbl.setObjectName("Hint")
        self.result_lbl.setWordWrap(True)
        lay.addWidget(self.result_lbl)

        drow = QHBoxLayout()
        drow.addWidget(QLabel("日期"))
        self.date_edit = widgets.DateInput()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setFixedWidth(140)
        drow.addWidget(self.date_edit)
        drow.addStretch(1)
        lay.addLayout(drow)

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("备注（可选）")
        lay.addWidget(self.note_edit)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        save = QPushButton("保存")
        save.setObjectName("Primary")
        save.clicked.connect(self._accept)
        btns.addWidget(save)
        lay.addLayout(btns)

        save.setDefault(True)
        self.amount.returnPressed.connect(self._accept)
        self._refresh()
        self.amount.set_focus()

    def _on_account_change(self, _idx: int) -> None:
        name = self.account_combo.currentData() or "其他"
        self.badge._name = name
        self.badge.update()
        self._refresh()

    def _current_balance(self) -> float | None:
        return services.asset_balance(self.owner, self.account_combo.currentData())

    def _refresh(self) -> None:
        adjust = self.mode_seg.current_index() == 1
        self.amount.edit.setPlaceholderText(
            "例：-200 表示少了 200 元" if adjust else "账户当前实际余额")
        cur = self._current_balance()
        val = self.amount.raw()
        if val is None:
            self._show_error("请输入有效数字")
            return
        if not adjust and val < 0:
            self._show_error("余额不能为负数")
            return
        if adjust:
            base = cur if cur is not None else 0.0
            new = base + val
            text = (f"当前 ¥{_money(base)}　{'+' if val >= 0 else '-'}"
                    f"¥{_money(val)}　→　¥{_money(new)}")
        else:
            text = f"直接设为 ¥{_money(val)}"
            if cur is not None:
                text = f"当前 ¥{_money(cur)}　→　¥{_money(val)}"
        self.result_lbl.setText(text)
        widgets._apply_property(self.result_lbl, "level", "ok")

    def _accept(self) -> None:
        val = self.amount.raw()
        if val is None:
            self._show_error("请输入有效数字")
            return
        if self.mode_seg.current_index() == 1:
            if val == 0:
                self._show_error("变动金额不能为 0")
                return
        elif val < 0:
            self._show_error("余额不能为负数")
            return
        self.accept()

    def _show_error(self, text: str) -> None:
        self.result_lbl.setText(text)
        widgets._apply_property(self.result_lbl, "level", "error")

    @property
    def owner(self) -> str:
        return "self" if self.owner_seg.current_index() == 0 else "partner"

    def result_amount(self) -> float:
        val = self.amount.value()
        if self.mode_seg.current_index() == 1:
            cur = self._current_balance()
            return (cur or 0.0) + val
        return val

    def data(self) -> dict:
        return {
            "owner": self.owner,
            "account": self.account_combo.currentData() or "",
            "amount": self.result_amount(),
            "date": self.date_edit.date().toString("yyyy-MM-dd"),
            "note": self.note_edit.text().strip(),
        }


class _AccountDelegate(QStyledItemDelegate):
    """账户列表项：左侧图标+名称，右侧绘制余额。"""

    def paint(self, painter, option, index) -> None:  # noqa: N802
        super().paint(painter, option, index)
        bal = index.data(Qt.UserRole + 1)
        if not bal:
            return
        painter.save()
        painter.setPen(QColor(theme.get("muted")))
        rect = option.rect.adjusted(0, 0, -14, 0)
        painter.drawText(rect, Qt.AlignRight | Qt.AlignVCenter, bal)
        painter.restore()


class AccountManagerDialog(QDialog):
    """账户管理：拖拽排序、双击重命名、按余额排序。"""

    def __init__(self, parent, accounts: list[str],
                 balances: dict[str, float] | None = None):
        super().__init__(parent)
        self._balances = dict(balances or {})
        self._order = list(accounts)
        self.setWindowTitle("账户管理")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        tip = QLabel("账户是你存放钱的地方，比如现金、银行卡、微信、支付宝。\n"
                     "拖拽可调整顺序；双击账户可重命名；右键可重命名或删除。")
        tip.setObjectName("Muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        add_card = widgets.Card("新增账户")
        arow = QHBoxLayout()
        arow.setSpacing(8)
        self.input = QLineEdit()
        self.input.setPlaceholderText("新账户名，如：零钱通、京东白条")
        self.input.returnPressed.connect(self._add)
        arow.addWidget(self.input, 1)
        add = QPushButton("添加")
        add.setObjectName("Primary")
        add.clicked.connect(self._add)
        arow.addWidget(add)
        add_card.body().addLayout(arow)
        lay.addWidget(add_card)

        self.list_widget = QListWidget()
        self.list_widget.setIconSize(QSize(24, 24))
        self.list_widget.setSpacing(2)
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.setItemDelegate(_AccountDelegate(self.list_widget))
        self.list_widget.itemDoubleClicked.connect(self._on_double_click)
        self.list_widget.customContextMenuRequested.connect(self._on_context_menu)
        self.list_widget.model().rowsMoved.connect(self._on_rows_moved)
        lay.addWidget(self.list_widget, 1)

        self.hint = QLabel("")
        self.hint.setObjectName("Hint")
        lay.addWidget(self.hint)

        btns = QHBoxLayout()
        del_btn = QPushButton("删除选中")
        del_btn.setObjectName("Ghost")
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.clicked.connect(self._del_selected)
        btns.addWidget(del_btn)
        btns.addStretch(1)
        close = QPushButton("完成")
        close.setObjectName("Primary")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        lay.addLayout(btns)

        self._reload()

    # ---------- 数据 ----------
    def _reload(self) -> None:
        self.list_widget.clear()
        for a in self._order:
            bal = self._balances.get(a, 0.0)
            item = QListWidgetItem(_account_icon(a, 24, _account_color(a)), a)
            item.setData(Qt.UserRole, a)
            item.setData(Qt.UserRole + 1, f"¥{bal:,.2f}")
            item.setSizeHint(QSize(0, 40))
            self.list_widget.addItem(item)

    def _current_order(self) -> list[str]:
        return [self.list_widget.item(i).data(Qt.UserRole)
                for i in range(self.list_widget.count())]

    def _sync_from_widget(self) -> None:
        self._order = self._current_order()

    def _on_rows_moved(self, *_args) -> None:
        self._sync_from_widget()

    def _persist(self) -> None:
        services.set_asset_accounts(self._order)

    # ---------- 操作 ----------
    def _add(self) -> None:
        name = self.input.text().strip()
        if not name:
            return
        if name in self._order:
            self._set_hint("该账户已存在")
            return
        self._order.append(name)
        self._balances[name] = 0.0
        self._persist()
        self.input.clear()
        self._set_hint("")
        self._reload()

    def _on_double_click(self, item: QListWidgetItem) -> None:
        self._rename(item.text())

    def _on_context_menu(self, pos) -> None:
        item = self.list_widget.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        act_rename = menu.addAction("重命名")
        act_color = menu.addAction("配色")
        act_del = menu.addAction("删除")
        act = menu.exec(self.list_widget.mapToGlobal(pos))
        if act == act_rename:
            self._rename(item.text())
        elif act == act_color:
            self._pick_color(item.text())
        elif act == act_del:
            self._del(item.text())

    def _pick_color(self, name: str) -> None:
        dlg = ColorPickerDialog(self, _account_color(name) or "")
        if dlg.exec() != QDialog.Accepted:
            return
        services.set_asset_account_color(name, dlg.color)
        self._reload()

    def _rename(self, old: str) -> None:
        new, ok = popups.ask_text(self, "重命名账户", "新名称：", old)
        new = (new or "").strip()
        if not ok or not new or new == old:
            return
        if new in self._order:
            self._set_hint("该账户已存在")
            return
        services.asset_rename_channel(old, new)
        self._order[self._order.index(old)] = new
        self._balances[new] = self._balances.pop(old, 0.0)
        self._persist()
        self._set_hint("")
        self._reload()

    def _del(self, name: str) -> None:
        if any(r["channel"] == name for r in services.asset_records()):
            self._set_hint(f"「{name}」已有余额记录，不能删除；可先改名")
            return
        if not popups.confirm(self, "删除账户", f"删除账户「{name}」？", "确定"):
            return
        self._order.remove(name)
        self._balances.pop(name, None)
        self._persist()
        self._set_hint("")
        self._reload()

    def _del_selected(self) -> None:
        item = self.list_widget.currentItem()
        if not item:
            return
        self._del(item.text())

    def _set_hint(self, text: str) -> None:
        self.hint.setText(text)
        widgets._apply_property(self.hint, "level", "error" if text else "")

    def accounts(self) -> list[str]:
        self._sync_from_widget()
        return list(self._order)


class RecurringDialog(QDialog):
    """周期账单：固定收支，到日子自动帮你记一笔。"""

    def __init__(self, parent, accounts: list[str]):
        super().__init__(parent)
        self._accounts = accounts
        self.setWindowTitle("周期账单")
        self.setMinimumWidth(600)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        tip = QLabel("把每个月固定不变的开销或收入设在这里，比如房租、会员订阅、"
                     "工资。\n到日子后会自动记一笔，不用每个月手动重复记。")
        tip.setObjectName("Muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        add_card = widgets.Card("新增周期账单")
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.addWidget(QLabel("名称"), 0, 0)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("如：房租")
        grid.addWidget(self.name_input, 0, 1)
        grid.addWidget(QLabel("金额"), 0, 2)
        self.amount_spin = widgets.DoubleSpinBox()
        self.amount_spin.setRange(0, 1_000_000)
        self.amount_spin.setDecimals(2)
        self.amount_spin.setPrefix("¥ ")
        grid.addWidget(self.amount_spin, 0, 3)
        grid.addWidget(QLabel("类型"), 0, 4)
        self.type_combo = widgets.ComboBox()
        self.type_combo.addItems(["支出", "收入"])
        grid.addWidget(self.type_combo, 0, 5)
        grid.addWidget(QLabel("分类"), 1, 0)
        self.cat_input = QLineEdit()
        self.cat_input.setPlaceholderText("如：住房")
        grid.addWidget(self.cat_input, 1, 1)
        grid.addWidget(QLabel("账户"), 1, 2)
        self.acc_combo = widgets.ComboBox()
        _fill_accounts(self.acc_combo, accounts)
        grid.addWidget(self.acc_combo, 1, 3)
        grid.addWidget(QLabel("每月"), 1, 4)
        self.day_spin = widgets.SpinBox()
        self.day_spin.setRange(1, 31)
        self.day_spin.setValue(1)
        self.day_spin.setSuffix(" 号")
        grid.addWidget(self.day_spin, 1, 5)
        add_btn = QPushButton("＋ 添加这条周期账单")
        add_btn.setObjectName("Primary")
        add_btn.clicked.connect(self._add)
        grid.addWidget(add_btn, 2, 0, 1, 6)
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(3, 2)
        add_card.body().addLayout(grid)
        lay.addWidget(add_card)

        self.list_widget = QListWidget()
        self.list_widget.setSpacing(2)
        lay.addWidget(self.list_widget, 1)

        self.hint = QLabel("")
        self.hint.setObjectName("Hint")
        lay.addWidget(self.hint)

        btns = QHBoxLayout()
        btns.addStretch(1)
        close = QPushButton("完成")
        close.setObjectName("Primary")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        lay.addLayout(btns)

        self._reload()

    @staticmethod
    def _status(b: dict) -> tuple[str, str]:
        enabled = b.get("enabled", True)
        month = QDate.currentDate().toString("yyyy-MM")
        if not enabled:
            return "已停用", "muted"
        if month in (b.get("months") or []):
            return "本月已记账", "green"
        if QDate.currentDate().day() >= int(b.get("day", 1)):
            return "可补记", "amber"
        return f"{b.get('day', 1)} 号记账", "muted"

    def _reload(self) -> None:
        generate_recurring()  # 打开时先补齐到期账单，保证状态显示准确
        bills = services.get_recurring_bills()
        self.list_widget.clear()
        if not bills:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 60))
            self.list_widget.addItem(item)
            empty = QLabel("还没有周期账单，用上面的表单添加一条吧\n"
                           "例如：房租，每月 1 号，支出 ¥1500")
            empty.setObjectName("Muted")
            empty.setAlignment(Qt.AlignCenter)
            self.list_widget.setItemWidget(item, empty)
            return
        for b in bills:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 60))
            self.list_widget.addItem(item)
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(10, 6, 10, 6)
            h.setSpacing(10)
            h.addWidget(_category_badge(b.get("category", "其他"), 34))
            col = QVBoxLayout()
            col.setSpacing(2)
            name = widgets.ElidedLabel(b.get("name", ""))
            name.setObjectName("RowTitle")
            col.addWidget(name)
            summary = (f"每月 {b.get('day', 1)} 号 · "
                       f"{'支出' if b.get('type', 'expense') == 'expense' else '收入'}"
                       f" ¥{float(b.get('amount', 0)):,.2f}")
            if b.get("account"):
                summary += f" · {b['account']}"
            sub = widgets.ElidedLabel(summary)
            sub.setObjectName("DaySum")
            col.addWidget(sub)
            h.addLayout(col, 1)

            status, color = self._status(b)
            h.addWidget(widgets.Tag(status, color))

            if status == "可补记":
                fill = QPushButton("现在补记")
                fill.setObjectName("LinkBtn")
                fill.setCursor(Qt.PointingHandCursor)
                fill.clicked.connect(lambda _=False, i=b.get("id"): self._fill_now(i))
                h.addWidget(fill)
            toggle = QPushButton("停用" if b.get("enabled", True) else "启用")
            toggle.setObjectName("LinkBtn")
            toggle.setCursor(Qt.PointingHandCursor)
            toggle.clicked.connect(lambda _=False, i=b.get("id"): self._toggle(i))
            h.addWidget(toggle)
            dele = QPushButton("✕")
            dele.setObjectName("IconBtn")
            dele.setCursor(Qt.PointingHandCursor)
            dele.clicked.connect(lambda _=False, i=b.get("id"): self._del(i))
            h.addWidget(dele)
            self.list_widget.setItemWidget(item, row)

    def _add(self) -> None:
        name = self.name_input.text().strip()
        amount = self.amount_spin.value()
        if not name or amount <= 0:
            self.hint.setText("请填写名称与金额")
            widgets._apply_property(self.hint, "level", "error")
            return
        bills = services.get_recurring_bills()
        bills.append({
            "id": f"rb_{uuid.uuid4().hex[:10]}",
            "name": name,
            "amount": amount,
            "type": "expense" if self.type_combo.currentIndex() == 0 else "income",
            "category": self.cat_input.text().strip() or "其他",
            "account": self.acc_combo.currentData() or "",
            "day": self.day_spin.value(),
            "enabled": True,
            "months": [],
        })
        services.set_recurring_bills(bills)
        self.name_input.clear()
        self.amount_spin.setValue(0)
        self.hint.setText("已添加，到设定日期会自动记账")
        widgets._apply_property(self.hint, "level", "ok")
        self._reload()

    def _toggle(self, bid: str) -> None:
        bills = services.get_recurring_bills()
        for b in bills:
            if b.get("id") == bid:
                b["enabled"] = not b.get("enabled", True)
                break
        services.set_recurring_bills(bills)
        self._reload()

    def _del(self, bid: str) -> None:
        bills = services.get_recurring_bills()
        name = next((b.get("name", "") for b in bills if b.get("id") == bid), "")
        if not popups.confirm(
                self, "删除周期账单",
                f"删除「{name}」？\n已自动记下的历史记录会保留。", "确定"):
            return
        services.set_recurring_bills([b for b in bills if b.get("id") != bid])
        self._reload()

    def _fill_now(self, bid: str) -> None:
        count = generate_recurring(force_bill_id=bid)
        if count:
            sounds.play("finance_bill_filled")
        self._reload()
        self.hint.setText(f"已补记 {count} 笔" if count else "本月已记过")
        widgets._apply_property(self.hint, "level", "ok" if count else "")


def generate_recurring(force_bill_id: str | None = None) -> int:
    """生成到期的周期账单；force_bill_id 指定时忽略日期与已生成限制。"""
    now = QDate.currentDate()
    month_str = now.toString("yyyy-MM")
    bills = services.get_recurring_bills()
    changed = False
    created = 0
    for bill in bills:
        if not bill.get("enabled", True):
            continue
        months = bill.get("months")
        if months is None:
            # 兼容旧格式：靠备注检测本月是否已生成过
            mark = f"{bill.get('name', '')}（周期）"
            existed = any(d["note"] == mark and d["date"].startswith(month_str)
                          for d in services.finance_list())
            months = [month_str] if existed else []
            bill["months"] = months
            changed = True
        # force：手动「立即生成」，忽略日期与"本月已生成"限制
        force = force_bill_id is not None and bill.get("id") == force_bill_id
        if force_bill_id is not None and not force:
            continue
        if month_str in months and not force:
            continue
        day = min(int(bill.get("day", 1)), now.daysInMonth())
        if not force and now.day() < day:
            continue
        date_str = QDate(now.year(), now.month(), day).toString("yyyy-MM-dd")
        services.finance_add(
            date_str,
            bill.get("type", "expense"),
            bill.get("category", "其他"),
            float(bill.get("amount", 0)),
            bill.get("name", ""),
            account=bill.get("account", "") or "",
            source=f"recurring:{bill.get('id', '')}",
        )
        if month_str not in months:
            months.append(month_str)
        changed = True
        created += 1
    if changed:
        services.set_recurring_bills(bills)
    return created


def _make_scroll_tab(tabs: QTabWidget, name: str) -> QVBoxLayout:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    content = QWidget()
    lay = QVBoxLayout(content)
    lay.setContentsMargins(0, 0, 8, 0)
    lay.setSpacing(16)
    scroll.setWidget(content)
    tabs.addTab(scroll, name)
    return lay


class FinancePage(QWidget):
    """理财管理：收支记账 / 资产总览 两个标签页。"""

    RANGES = ["本月", "上月", "本年", "全部"]

    def __init__(self):
        super().__init__()
        self._range = 0
        self._trend_gran = "day"  # 趋势图粒度：day / week / month / year
        self._budget = float(db.get_setting("budget_monthly", "0") or 0)
        self._asset_owner = 0  # 0 汇总 / 1 我 / 2 伴侣
        self._asset_sort_desc = False  # 首次点击「按余额排序」→ 降序
        self._filter_type = 0  # 0 全部 / 1 支出 / 2 收入
        self._filter_cat = ""
        self._keyword = ""
        self._first_show = True

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(10)
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("理财管理")
        title.setObjectName("PageTitle")
        sub = QLabel("记账、预算与账户余额联动，收支一目了然")
        sub.setObjectName("PageSubtitle")
        title_col.addWidget(title)
        title_col.addWidget(sub)
        head.addLayout(title_col)
        head.addStretch(1)
        root.addLayout(head)

        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        self._ledger_lay = _make_scroll_tab(tabs, "收支记账")
        self._build_ledger()

        self._asset_lay = _make_scroll_tab(tabs, "资产总览")
        self._build_assets()

        generate_recurring()
        self.reload()
        self._reload_assets()

    def showEvent(self, event) -> None:  # noqa: N802
        """切回本页时刷新（页面在启动时已构造，避免看到过期数据）。"""
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            return
        generate_recurring()
        self.reload()
        self._reload_assets()

    # ===================== 通用 =====================
    def _accounts(self) -> list[str]:
        return services.asset_accounts()

    def _range_bounds(self) -> tuple[str, str] | None:
        now = QDate.currentDate()
        if self._range == 0:
            start = QDate(now.year(), now.month(), 1)
            end = QDate(now.year(), now.month(), now.daysInMonth())
        elif self._range == 1:
            prev = now.addMonths(-1)
            start = QDate(prev.year(), prev.month(), 1)
            end = QDate(prev.year(), prev.month(), prev.daysInMonth())
        elif self._range == 2:
            start = QDate(now.year(), 1, 1)
            end = QDate(now.year(), 12, 31)
        else:
            return None
        return start.toString("yyyy-MM-dd"), end.toString("yyyy-MM-dd")

    def _in_range(self, date_str: str) -> bool:
        bounds = self._range_bounds()
        if bounds is None:
            return True
        return bounds[0] <= date_str <= bounds[1]

    def _range_name(self) -> str:
        return ["本月", "上月", "本年", "累计"][self._range]

    # ===================== 收支记账 =====================
    def _build_ledger(self) -> None:
        lay = self._ledger_lay

        self.hero = SummaryHero("本月结余", action="设置预算")
        self.hero.action_btn.clicked.connect(self._set_budget)
        lay.addWidget(self.hero)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.add_btn = QPushButton("＋ 记一笔")
        self.add_btn.setObjectName("Primary")
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.clicked.connect(self._add)
        tools.addWidget(self.add_btn)
        self.rec_btn = QPushButton("周期账单")
        self.rec_btn.setObjectName("Ghost")
        self.rec_btn.setCursor(Qt.PointingHandCursor)
        self.rec_btn.clicked.connect(self._manage_recurring)
        tools.addWidget(self.rec_btn)
        tools.addStretch(1)
        self.range_seg = SegGroup(self.RANGES, self)
        self.range_seg.changed.connect(self._on_range_change)
        tools.addWidget(self.range_seg)
        lay.addLayout(tools)

        charts_row = QHBoxLayout()
        charts_row.setSpacing(12)
        trend_card = widgets.Card()
        trow = QHBoxLayout()
        trow.setSpacing(8)
        ttitle = QLabel("收支趋势")
        ttitle.setObjectName("CardTitle")
        trow.addWidget(ttitle)
        trow.addStretch(1)
        self.trend_gran = SegGroup(["日", "周", "月", "年"], self, compact=True)
        self.trend_gran.changed.connect(self._on_trend_gran_change)
        trow.addWidget(self.trend_gran)
        trend_card.body().addLayout(trow)

        mrow = QHBoxLayout()
        mrow.setSpacing(8)
        mrow.addStretch(1)
        self.trend_mode = SegGroup(["支出", "收入", "收支", "净收入", "总资产"],
                                   self, compact=True)
        self.trend_mode.changed.connect(self._on_trend_mode_change)
        mrow.addWidget(self.trend_mode)
        trend_card.body().addLayout(mrow)

        self.trend_chart = widgets.MultiLineChart()
        self.trend_chart.set_on_click(self._on_trend_point_clicked)
        trend_card.body().addWidget(self.trend_chart)
        charts_row.addWidget(trend_card, 3)

        cat_card = widgets.Card("支出结构")
        self.chart = widgets.BarChart()
        cat_card.body().addWidget(self.chart)
        self.cat_list = QListWidget()
        self.cat_list.setIconSize(QSize(20, 20))
        self.cat_list.setMaximumHeight(150)
        self.cat_list.itemClicked.connect(self._on_cat_click)
        cat_card.body().addWidget(self.cat_list)
        charts_row.addWidget(cat_card, 2)
        lay.addLayout(charts_row)

        list_card = widgets.Card("收支明细")
        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.type_seg = SegGroup(["全部", "支出", "收入"], self, compact=True)
        self.type_seg.changed.connect(lambda _i: self._on_filter_change())
        filters.addWidget(self.type_seg)
        self.cat_combo = widgets.ComboBox()
        self.cat_combo.setFixedWidth(130)
        self.cat_combo.currentIndexChanged.connect(
            lambda _i: self._on_filter_change())
        filters.addWidget(self.cat_combo)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索备注 / 分类")
        self.search_edit.textChanged.connect(self._on_filter_change)
        filters.addWidget(self.search_edit, 1)
        clear_btn = QPushButton("清空筛选")
        clear_btn.setObjectName("LinkBtn")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.clicked.connect(self._clear_filters)
        filters.addWidget(clear_btn)
        list_card.body().addLayout(filters)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setMinimumHeight(320)
        container = QWidget()
        self.list_layout = QVBoxLayout(container)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(2)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(container)
        list_card.body().addWidget(self.scroll)
        lay.addWidget(list_card)
        lay.addStretch(1)

    # ---------- 交互 ----------
    def _manage_recurring(self) -> None:
        RecurringDialog(self, self._accounts()).exec()
        self.reload()

    def _on_range_change(self, idx: int) -> None:
        self._range = idx
        self.reload()

    def _on_trend_mode_change(self, _idx: int) -> None:
        self._update_trend_chart()

    def _on_trend_gran_change(self, idx: int) -> None:
        self._trend_gran = ["day", "week", "month", "year"][idx]
        self._update_trend_chart()

    def _records_for_point(self, date: str) -> list[dict]:
        """某趋势数据点对应时间跨度的记账记录（按当前粒度）。"""
        gran = self._trend_gran
        if gran == "day":
            return [d for d in services.finance_list() if d["date"] == date]
        if gran == "week":
            monday = QDate.fromString(date, "yyyy-MM-dd")
            sunday = monday.addDays(6)
            s = monday.toString("yyyy-MM-dd")
            e = sunday.toString("yyyy-MM-dd")
            return [d for d in services.finance_list() if s <= d["date"] <= e]
        if gran == "month":
            return [d for d in services.finance_list()
                    if d["date"].startswith(date[:7])]
        # year → 代表日期为该季度首月 1 日，按季度取记录
        q = QDate.fromString(date, "yyyy-MM-dd")
        s = QDate(q.year(), (q.month() - 1) // 3 * 3 + 1, 1)
        e = s.addMonths(3).addDays(-1)
        return [d for d in services.finance_list()
                if s.toString("yyyy-MM-dd") <= d["date"]
                <= e.toString("yyyy-MM-dd")]

    def _on_trend_point_clicked(self, index: int) -> None:
        """点击趋势图数据点：弹出该时间点的记录编辑窗口。"""
        dates = getattr(self, "_trend_dates", [])
        if not (0 <= index < len(dates)):
            return
        date = dates[index]
        records = self._records_for_point(date)
        if not records:
            popups.notify(self, "无记录",
                                    f"{date} 没有可编辑的记账记录")
            return
        dlg = RecordsEditorDialog(self, date, records)

        def _after_change(_fid: int) -> None:
            self.reload()
            dlg.refresh(self._records_for_point(date))

        dlg.editRequested.connect(self._edit)
        dlg.deleteRequested.connect(self._delete)
        dlg.editRequested.connect(_after_change)
        dlg.deleteRequested.connect(_after_change)
        dlg.exec()
        self.reload()

    def _set_budget(self) -> None:
        val, ok = popups.ask_double(
            self, "设置每月预算",
            "每月支出预算（元）；填 0 表示不设置：",
            self._budget, 0, 10_000_000)
        if not ok:
            return
        self._budget = val
        db.set_setting("budget_monthly", str(val))
        self.reload()

    def _on_cat_click(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.UserRole)
        if not name:
            return
        self._filter_cat = "" if self._filter_cat == name else name
        self.reload()

    def _on_filter_change(self) -> None:
        self._filter_type = self.type_seg.current_index()
        self._filter_cat = self.cat_combo.currentData() or ""
        self._keyword = self.search_edit.text().strip().lower()
        self._reload_list()

    def _clear_filters(self) -> None:
        self.type_seg.set_current(0)
        if self.cat_combo.count():
            self.cat_combo.setCurrentIndex(0)
        self.search_edit.clear()
        self._filter_type = 0
        self._filter_cat = ""
        self._keyword = ""
        self._reload_list()

    def _add(self) -> None:
        dlg = RecordDialog(self, self._accounts())
        while True:
            code = dlg.exec()
            if code == QDialog.Rejected:
                return
            d = dlg.data()
            services.finance_add(d["date"], d["type"], d["category"], d["amount"],
                                 d["note"], account=d["account"], owner=d["owner"])
            sounds.play("finance_saved")
            self._sync_account(d["date"], d["account"], d["amount"], d["type"],
                               d["owner"])
            self.reload()
            if code != 2:  # 2 = 保存并继续
                return
            dlg.amount.set_text("")
            dlg.note_edit.clear()
            dlg.amount.set_focus()

    def _edit(self, fid: int) -> None:
        old = services.finance_get(fid)
        if not old:
            return
        accounts = self._accounts()
        if old.get("account") and old["account"] not in accounts:
            accounts = accounts + [old["account"]]
        dlg = RecordDialog(self, accounts, old)
        if dlg.exec() != QDialog.Accepted:
            return
        d = dlg.data()
        self._sync_account(old.get("date", ""), old.get("account", ""),
                           float(old.get("amount", 0)), old.get("type", ""),
                           old.get("owner", "self"), revert=True)
        services.finance_update(fid, d["date"], d["type"], d["category"],
                                d["amount"], d["note"], d["account"], d["owner"])
        self._sync_account(d["date"], d["account"], d["amount"], d["type"],
                           d["owner"])
        self.reload()

    def _delete(self, fid: int) -> None:
        rec = services.finance_get(fid)
        if not rec:
            return
        text = (f"删除 {rec['date']}　{rec.get('category', '')}　"
                f"¥{_money(float(rec['amount']))}？")
        if rec.get("source", "").startswith("recurring:"):
            text += ("\n\n这是周期账单自动生成的记录，本月不会重复生成；"
                     "如需补回，请到「周期账单」选中对应账单点「立即生成」。")
        if not popups.confirm(self, "删除记录", text, "确定"):
            return
        self._sync_account(rec.get("date", ""), rec.get("account", ""),
                           float(rec.get("amount", 0)), rec.get("type", ""),
                           rec.get("owner", "self"), revert=True)
        services.finance_delete(fid)
        self.reload()

    def _sync_account(self, date: str, account: str, amount: float,
                      ftype: str, owner: str = "self",
                      revert: bool = False) -> None:
        """记账与账户余额联动：支出减少余额，收入增加余额。"""
        if not account or not ftype:
            return
        cur = services.asset_balance(owner, account)
        if cur is None:
            return
        delta = -amount if ftype == "expense" else amount
        if revert:
            delta = -delta
        services.asset_set_balance(owner, account, cur + delta, date,
                                   "记账自动同步")

    # ---------- 数据 ----------
    def _filtered(self) -> list[dict]:
        data = [d for d in services.finance_list() if self._in_range(d["date"])]
        if self._filter_type == 1:
            data = [d for d in data if d["type"] == "expense"]
        elif self._filter_type == 2:
            data = [d for d in data if d["type"] == "income"]
        if self._filter_cat:
            data = [d for d in data if d.get("category") == self._filter_cat]
        if self._keyword:
            kw = self._keyword
            data = [d for d in data
                    if kw in (d.get("note") or "").lower()
                    or kw in (d.get("category") or "").lower()
                    or kw in (d.get("account") or "").lower()]
        return data

    def _update_hero(self, data: list[dict]) -> None:
        name = self._range_name()
        income = sum(d["amount"] for d in data if d["type"] == "income")
        expense = sum(d["amount"] for d in data if d["type"] == "expense")
        balance = income - expense
        self.hero.set_caption(f"{name}结余", f"共 {len(data)} 笔")
        self.hero.set_value("¥0.00" if balance == 0
                            else f"¥{_money(balance, sign=True)}")
        self.hero.set_metrics([
            ("收入", f"¥{_money(income)}", "green"),
            ("支出", f"¥{_money(expense)}", "red"),
            (f"{name}日均支出", f"¥{_money(expense / max(1, self._days_in_scope()))}",
             "accent"),
        ])
        self._update_budget()

    def _days_in_scope(self) -> int:
        """统计范围已过天数（用于日均支出）。"""
        now = QDate.currentDate()
        if self._range == 0:
            return max(1, now.day())
        if self._range == 1:
            return QDate(now.year(), now.month(), 1).addDays(-1).daysInMonth()
        start = QDate(now.year(), 1, 1) if self._range == 2 else None
        if start is None:
            dates = [d["date"] for d in services.finance_list()]
            start = QDate.fromString(min(dates), "yyyy-MM-dd") if dates else now
        return max(1, start.daysTo(now) + 1)

    def _update_budget(self) -> None:
        now = QDate.currentDate()
        month_str = now.toString("yyyy-MM")
        month_expense = sum(
            float(d["amount"]) for d in services.finance_list()
            if d["type"] == "expense" and d["date"].startswith(month_str))
        btn = self.hero.action_btn
        if btn is not None:
            btn.setText(f"预算 ¥{_money(self._budget)}" if self._budget > 0
                        else "设置预算")
        if self._budget <= 0:
            self.hero.set_progress(0)
            self.hero.set_note("设置每月预算后，这里会显示剩余可用额度", "")
            return
        left = self._budget - month_expense
        pct = month_expense / self._budget
        self.hero.set_progress(pct, over=pct > 1)
        days_left = max(1, now.daysInMonth() - now.day() + 1)
        if left >= 0:
            self.hero.set_note(
                f"本月预算 ¥{_money(self._budget)}　已用 {pct * 100:.0f}%　"
                f"剩余 ¥{_money(left)}（还剩 {days_left} 天，"
                f"日均可用 ¥{_money(left / days_left)}）", "")
        else:
            self.hero.set_note(
                f"本月预算 ¥{_money(self._budget)}　已超支 ¥{_money(-left)}",
                "error")

    def _update_charts(self, data: list[dict]) -> None:
        # 支出结构（随统计范围）
        if not data:
            self.chart.set_data([], [])
            self.cat_list.clear()
        else:
            cat_totals: dict[str, float] = {}
            for d in data:
                if d["type"] == "expense":
                    cat_totals[d["category"]] = cat_totals.get(d["category"], 0) \
                        + d["amount"]
            cats = sorted(cat_totals, key=lambda c: -cat_totals[c])
            total = sum(cat_totals.values())
            # 柱状图只画前 8 项，其余合并，避免 x 轴拥挤
            top = cats[:8]
            top_vals = [cat_totals[c] for c in top]
            top_labels = list(top)
            if len(cats) > 8:
                top_labels.append("其余")
                top_vals.append(sum(cat_totals[c] for c in cats[8:]))
            self.chart.set_data(top_vals, top_labels, color="red")
            self.cat_list.clear()
            for c in cats:
                pct = cat_totals[c] / total * 100 if total else 0
                item = QListWidgetItem(
                    _category_icon(c, 20),
                    f"{c}　¥{_money(cat_totals[c])}　{pct:.0f}%")
                item.setData(Qt.UserRole, c)
                self.cat_list.addItem(item)
        self._update_trend_chart()

    # ---------- 趋势图（日/周/月/年 粒度，日期设置同体重页） ----------
    @staticmethod
    def _bucket_key(ds: str, gran: str) -> str:
        if gran == "day":
            return ds
        if gran == "week":
            q = QDate.fromString(ds, "yyyy-MM-dd")
            y, w = q.weekNumber()
            return f"{y}-W{w:02d}"
        if gran == "month":
            return ds[:7]
        # year → 按季度聚合（与体重管理年视图一致）
        return f"{ds[:4]}-Q{(int(ds[5:7]) - 1) // 3 + 1}"

    @staticmethod
    def _point_for(offset: int, gran: str, today: QDate):
        """往前第 offset 个桶的 (key, 主标签, 副标签, 代表日期)。"""
        if gran == "day":
            d = today.addDays(-offset)
            ds = d.toString("yyyy-MM-dd")
            return ds, ds[5:], ds[:4], ds
        if gran == "week":
            dow = today.dayOfWeek()  # 1=周一 .. 7=周日
            monday = today.addDays(-(dow - 1) - offset * 7)
            sunday = monday.addDays(6)
            y, w = monday.weekNumber()
            key = f"{y}-W{w:02d}"
            return (key, monday.toString("MM/dd"),
                    f"~{sunday.toString('MM/dd')}",
                    monday.toString("yyyy-MM-dd"))
        if gran == "month":
            m = today.addMonths(-offset)
            key = m.toString("yyyy-MM")
            return key, f"{m.month()}月", str(m.year()), f"{key}-01"
        # year → 按季度：往前第 offset 个季度，主行「第N季」、副行年份
        q = today.year() * 4 + (today.month() - 1) // 3 - offset
        y, qi = q // 4, q % 4 + 1
        return f"{y}-Q{qi}", f"第{qi}季", str(y), f"{y}-{qi * 3 - 2:02d}-01"

    def _trend_aggregate(self, gran: str):
        """按粒度聚合收支，时间轴到今天为止（最新点在最右侧）。"""
        # 年视图按季度（2 年 8 个季度），与体重管理年视图一致
        n = {"day": 30, "week": 12, "month": 12, "year": 8}[gran]
        today = QDate.currentDate()
        agg: dict[str, list] = {}
        for d in services.finance_list():
            key = self._bucket_key(d["date"], gran)
            row = agg.setdefault(key, [0.0, 0.0])
            row[0 if d["type"] == "income" else 1] += d["amount"]
        labels, dates, inc, exp, subs = [], [], [], [], []
        for i in range(n - 1, -1, -1):
            key, label, sub, date_str = self._point_for(i, gran, today)
            ii, ee = agg.get(key, (0.0, 0.0))
            labels.append(label)
            dates.append(date_str)
            inc.append(ii)
            exp.append(ee)
            subs.append(sub)
        return labels, dates, inc, exp, subs

    def _update_trend_chart(self) -> None:
        gran = self._trend_gran
        labels, dates, inc, exp, subs = self._trend_aggregate(gran)
        if not services.finance_list():
            self.trend_chart.set_series([], [])
            self._trend_dates = []
            return
        mode = self.trend_mode.current_index() if hasattr(self, "trend_mode") else 0
        if mode == 0:
            series = [{"name": "支出", "color": "red", "values": exp}]
        elif mode == 1:
            series = [{"name": "收入", "color": "green", "values": inc}]
        elif mode == 2:
            series = [
                {"name": "收入", "color": "green", "values": inc},
                {"name": "支出", "color": "red", "values": exp},
            ]
        elif mode == 3:
            net = [i - e for i, e in zip(inc, exp)]
            series = [{"name": "净收入", "color": "accent", "values": net}]
        else:
            # 预估总资产变化：当前总资产为终点，往前用累计净收入反推。
            # 年视图按季度聚合，但当前总资产是「今天」的快照，
            # 因此只从当前季度起往前反推，之前的季度留空（断线）。
            base = services.asset_summary()["combined"]
            net = [i - e for i, e in zip(inc, exp)]
            assets: list[float | None] = [None] * len(net)
            run = 0.0
            for i in range(len(net) - 1, -1, -1):
                if gran == "year" and i < len(net) - 1 and assets[i + 1] is None:
                    break
                assets[i] = base - run
                run += net[i]
            series = [{"name": "总资产", "color": "accent", "values": assets}]
        self._trend_dates = dates
        self.trend_chart.set_series(series, labels, dates=dates,
                                    sub_labels=subs, legend=(mode == 2))

    def _reload_list(self) -> None:
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        data = self._filtered()
        today = QDate.currentDate()
        groups: dict[str, list[dict]] = {}
        for d in data:
            groups.setdefault(d["date"], []).append(d)

        for date_str in sorted(groups.keys(), reverse=True):
            rows = groups[date_str]
            day_exp = sum(r["amount"] for r in rows if r["type"] == "expense")
            day_inc = sum(r["amount"] for r in rows if r["type"] == "income")
            self.list_layout.insertWidget(
                self.list_layout.count() - 1,
                self._day_header(date_str, today, day_inc, day_exp))
            for r in rows:
                self.list_layout.insertWidget(
                    self.list_layout.count() - 1, self._ledger_row(r))

        if not data:
            tip = QLabel("没有符合条件的记录"
                         if any(services.finance_list())
                         else "还没有记过账，点上方「＋ 记一笔」开始")
            tip.setObjectName("Muted")
            tip.setAlignment(Qt.AlignCenter)
            tip.setMinimumHeight(60)
            self.list_layout.insertWidget(self.list_layout.count() - 1, tip)

    def _day_header(self, date_str: str, today: QDate, income: float,
                    expense: float) -> QWidget:
        d = QDate.fromString(date_str, "yyyy-MM-dd")
        delta = d.daysTo(today)
        if delta == 0:
            title = "今天"
        elif delta == 1:
            title = "昨天"
        else:
            title = f"{date_str}　周{'一二三四五六日'[(d.dayOfWeek() - 1) % 7]}"
        frame = QFrame()
        frame.setObjectName("DayHeader")
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(6, 10, 6, 4)
        lay.setSpacing(8)
        t = QLabel(title)
        t.setObjectName("DayTitle")
        lay.addWidget(t)
        lay.addStretch(1)
        if income:
            i = QLabel(f"收 ¥{_money(income)}")
            i.setObjectName("DaySum")
            lay.addWidget(i)
        if expense:
            e = QLabel(f"支 ¥{_money(expense)}")
            e.setObjectName("DaySum")
            lay.addWidget(e)
        return frame

    def _ledger_row(self, r: dict) -> QWidget:
        row = QFrame()
        row.setObjectName("LedgerRow")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(10)

        category = r.get("category", "其他")
        lay.addWidget(_category_badge(category, 34))

        col = QVBoxLayout()
        col.setSpacing(1)
        note = (r.get("note") or "").removesuffix("（周期）")
        title = widgets.ElidedLabel(note or category)
        title.setObjectName("RowTitle")
        parts = [category] if note else []
        owner = OWNER_LABEL.get(r.get("owner", "self"), "我")
        if r.get("account"):
            parts.append(f"{owner}·{r['account']}")
        parts.append(r["date"])
        sub = widgets.ElidedLabel(" · ".join(parts))
        sub.setObjectName("DaySum")
        col.addWidget(title)
        col.addWidget(sub)
        lay.addLayout(col, 1)

        if (r.get("source", "").startswith("recurring:")
                or (r.get("note") or "").endswith("（周期）")):
            lay.addWidget(widgets.Tag("周期", "muted"))

        is_income = r["type"] == "income"
        amt = QLabel(f"{'+' if is_income else '-'}¥{_money(float(r['amount']))}")
        amt.setObjectName("Money")
        widgets._apply_property(amt, "sign", "income" if is_income else "expense")
        lay.addWidget(amt)

        edit = QPushButton("编辑")
        edit.setObjectName("LinkBtn")
        edit.setCursor(Qt.PointingHandCursor)
        edit.clicked.connect(lambda _=False, i=r["id"]: self._edit(i))
        lay.addWidget(edit)
        dele = QPushButton("✕")
        dele.setObjectName("IconBtn")
        dele.setCursor(Qt.PointingHandCursor)
        dele.clicked.connect(lambda _=False, i=r["id"]: self._delete(i))
        lay.addWidget(dele)
        return row

    def reload(self) -> None:
        cats = sorted({d.get("category", "") for d in services.finance_list()
                       if d.get("category")})
        current = self._filter_cat
        self.cat_combo.blockSignals(True)
        self.cat_combo.clear()
        self.cat_combo.addItem("全部分类", "")
        for c in cats:
            self.cat_combo.addItem(_category_icon(c, 18), c, c)
        idx = self.cat_combo.findData(current)
        self.cat_combo.setCurrentIndex(max(idx, 0))
        self.cat_combo.blockSignals(False)
        self._filter_cat = self.cat_combo.currentData() or ""

        scope = [d for d in services.finance_list() if self._in_range(d["date"])]
        self._update_hero(scope)
        self._update_charts(scope)
        self._reload_list()

    # ===================== 资产总览 =====================
    def _build_assets(self) -> None:
        lay = self._asset_lay

        self.asset_hero = SummaryHero("总资产")
        lay.addWidget(self.asset_hero)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.asset_add_btn = QPushButton("更新余额")
        self.asset_add_btn.setObjectName("Primary")
        self.asset_add_btn.setCursor(Qt.PointingHandCursor)
        self.asset_add_btn.clicked.connect(self._asset_add)
        tools.addWidget(self.asset_add_btn)
        self.account_btn = QPushButton("账户管理")
        self.account_btn.setObjectName("Ghost")
        self.account_btn.setCursor(Qt.PointingHandCursor)
        self.account_btn.clicked.connect(self._manage_accounts)
        tools.addWidget(self.account_btn)
        tools.addStretch(1)
        self.owner_seg = SegGroup(["汇总", "我", "伴侣"], self)
        self.owner_seg.changed.connect(self._on_asset_owner_change)
        tools.addWidget(self.owner_seg)
        lay.addLayout(tools)

        self.accounts_card = widgets.Card()
        acc_header = QHBoxLayout()
        acc_header.setSpacing(8)
        acc_title = QLabel("账户余额")
        acc_title.setObjectName("CardTitle")
        acc_header.addWidget(acc_title)
        acc_header.addStretch(1)
        self.sort_accounts_btn = QPushButton("按余额排序")
        self.sort_accounts_btn.setObjectName("LinkBtn")
        self.sort_accounts_btn.setCursor(Qt.PointingHandCursor)
        self.sort_accounts_btn.setToolTip("按账户余额（两人合计）从高到低排序")
        self.sort_accounts_btn.clicked.connect(self._sort_accounts_by_balance)
        acc_header.addWidget(self.sort_accounts_btn)
        self.accounts_card.body().addLayout(acc_header)
        self.accounts_grid = QGridLayout()
        self.accounts_grid.setSpacing(10)
        self.accounts_card.body().addLayout(self.accounts_grid)
        lay.addWidget(self.accounts_card)

        chart_card = widgets.Card("资产趋势")
        self.asset_chart = widgets.MultiLineChart()
        chart_card.body().addWidget(self.asset_chart)
        lay.addWidget(chart_card)

        list_card = widgets.Card("余额变动记录")
        tip = QLabel("每次更新余额都会留下一条记录，删除后余额按剩余记录重算。")
        tip.setObjectName("Muted")
        list_card.body().addWidget(tip)
        self.asset_scroll = QScrollArea()
        self.asset_scroll.setWidgetResizable(True)
        self.asset_scroll.setFrameShape(QFrame.NoFrame)
        self.asset_scroll.setMinimumHeight(280)
        container = QWidget()
        self.asset_list_layout = QVBoxLayout(container)
        self.asset_list_layout.setContentsMargins(0, 0, 6, 0)
        self.asset_list_layout.setSpacing(2)
        self.asset_list_layout.addStretch(1)
        self.asset_scroll.setWidget(container)
        list_card.body().addWidget(self.asset_scroll)
        lay.addWidget(list_card)
        lay.addStretch(1)

    def _on_asset_owner_change(self, idx: int) -> None:
        self._asset_owner = idx
        self._reload_assets()

    def _sort_accounts_by_balance(self) -> None:
        """按账户余额（两人合计）排序账户，并持久化顺序。"""
        accounts = self._accounts()
        summary = services.asset_summary()

        def bal(a: str) -> float:
            return (summary["self_by_channel"].get(a, 0.0)
                    + summary["partner_by_channel"].get(a, 0.0))

        self._asset_sort_desc = not self._asset_sort_desc
        accounts.sort(key=bal, reverse=self._asset_sort_desc)
        services.set_asset_accounts(accounts)
        self.sort_accounts_btn.setText(
            "按余额 ↓" if self._asset_sort_desc else "按余额 ↑")
        self._reload_assets()

    def _manage_accounts(self) -> None:
        accounts = self._accounts()
        summary = services.asset_summary()
        balances = {
            a: summary["self_by_channel"].get(a, 0.0)
            + summary["partner_by_channel"].get(a, 0.0)
            for a in accounts
        }
        dlg = AccountManagerDialog(self, accounts, balances)
        if dlg.exec() == QDialog.Accepted:
            services.set_asset_accounts(dlg.accounts())
        self._reload_assets()

    def _asset_add(self) -> None:
        accounts = self._accounts()
        if not accounts:
            popups.notify(self, "提示", "请先在「账户管理」中添加账户")
            return
        preset = "self" if self._asset_owner in (0, 1) else "partner"
        dlg = BalanceDialog(self, accounts, preset)
        if dlg.exec() != QDialog.Accepted:
            return
        d = dlg.data()
        if not d["account"]:
            return
        services.asset_set_balance(d["owner"], d["account"], d["amount"],
                                   d["date"], d["note"])
        self._reload_assets()

    def _asset_delete(self, aid: int) -> None:
        recs = [r for r in services.asset_records_with_delta() if r["id"] == aid]
        if not recs:
            return
        r = recs[0]
        key = (r["date"], r["id"])
        same = [x for x in services.asset_records_with_delta()
                if x["owner"] == r["owner"] and x["channel"] == r["channel"]]
        if any((x["date"], x["id"]) > key for x in same):
            extra = "\n这是较早的记录，删除后当前余额不变。"
        else:
            older = [x for x in same if (x["date"], x["id"]) < key]
            prev = float(older[0]["amount"]) if older else 0.0
            extra = f"\n删除后该账户余额变为 ¥{_money(prev)}。"
        if not popups.confirm(
                self, "删除余额记录",
                f"删除 {r['date']}　{OWNER_LABEL.get(r['owner'], r['owner'])}"
                f"·{r['channel']} 的记录？{extra}", "确定"):
            return
        services.asset_delete(aid)
        self._reload_assets()

    def _reload_assets(self) -> None:
        summary = services.asset_summary()
        mode = ["combined", "self", "partner"][self._asset_owner]
        accounts = self._accounts()

        self.asset_hero.set_caption(
            "总资产", "汇总两人" if mode == "combined" else
            ("我" if mode == "self" else "伴侣"))
        shown = summary["combined"] if mode == "combined" else (
            summary["self"] if mode == "self" else summary["partner"])
        self.asset_hero.set_value(f"¥{_money(shown)}")

        s_total = summary["self"]
        p_total = summary["partner"]
        if mode == "combined":
            # 我/伴侣对比直接并入汇总：大小于号 + 差值正负 + 百分比
            diff = s_total - p_total
            pct = (diff / p_total * 100) if p_total else None
            if diff > 0:
                cmp_val, cmp_color = "我 > 伴侣", "green"
            elif diff < 0:
                cmp_val, cmp_color = "我 < 伴侣", "red"
            else:
                cmp_val, cmp_color = "我 = 伴侣", "muted"
            if diff == 0:
                cmp_lbl = "¥0.00 · 0%"
            else:
                sign = "+" if diff > 0 else "-"
                pct_txt = f" · {sign}{abs(pct):.1f}%" if pct is not None else ""
                cmp_lbl = f"{sign}¥{_money(abs(diff))}{pct_txt}"
            self.asset_hero.set_metrics([
                ("我的资产", f"¥{_money(s_total)}", "blue"),
                ("伴侣资产", f"¥{_money(p_total)}", "green"),
                (cmp_lbl, cmp_val, cmp_color),
            ])
        else:
            self.asset_hero.set_metrics([
                ("我的资产", f"¥{_money(s_total)}", "blue"),
                ("伴侣资产", f"¥{_money(p_total)}", "green"),
                ("账户数", f"{len(accounts)} 个", "accent"),
            ])

        # 账户卡
        while self.accounts_grid.count():
            item = self.accounts_grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        total = max(summary["combined"], 1e-6)
        cols = 3
        for i, acc in enumerate(accounts):
            self.accounts_grid.addWidget(
                self._account_card(acc, mode, summary, total), i // cols, i % cols)

        # 趋势
        self.asset_chart.set_hint("再记录一次余额，就能看到资产变化趋势")
        t = services.asset_trend(mode)
        series = [
            {"name": acc, "color": _accent_for(i),
             "values": t["series"].get(acc, [])}
            for i, acc in enumerate(accounts)
        ]
        labels = [d[5:] for d in t["dates"]]
        self.asset_chart.set_series(series, labels)

        # 变动记录
        while self.asset_list_layout.count() > 1:
            item = self.asset_list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        records = services.asset_records_with_delta()
        if mode == "self":
            records = [r for r in records if r["owner"] == "self"]
        elif mode == "partner":
            records = [r for r in records if r["owner"] == "partner"]
        for r in records:
            self.asset_list_layout.insertWidget(
                self.asset_list_layout.count() - 1, self._asset_row(r))
        if not records:
            empty = QLabel("还没有余额记录，点「更新余额」开始")
            empty.setObjectName("Muted")
            empty.setAlignment(Qt.AlignCenter)
            empty.setMinimumHeight(60)
            self.asset_list_layout.insertWidget(
                self.asset_list_layout.count() - 1, empty)

    def _account_card(self, acc: str, mode: str, summary: dict,
                      total: float) -> QWidget:
        card = AccountCard()
        card.clicked.connect(lambda a=acc: self._open_account(a))
        card._lay.addWidget(widgets.IconBadge(acc, "account", 34,
                                              _account_color(acc)))

        col = QVBoxLayout()
        col.setSpacing(2)
        name = widgets.ElidedLabel(acc)
        name.setObjectName("AccountName")
        col.addWidget(name)

        sv = summary["self_by_channel"].get(acc, 0.0)
        pv = summary["partner_by_channel"].get(acc, 0.0)
        if mode == "self":
            v = sv
        elif mode == "partner":
            v = pv
        else:
            v = sv + pv
        val = widgets.ElidedLabel(f"¥{_money(v)}")
        val.setObjectName("AccountBalance")
        col.addWidget(val)

        if mode == "combined":
            # 每个账户也展示两人对比：大小于号 + 差值 + 百分比
            diff = sv - pv
            if diff > 0:
                rel, color, sign = "我 > 伴侣", "green", "+"
            elif diff < 0:
                rel, color, sign = "我 < 伴侣", "red", "-"
            else:
                rel, color, sign = "我 = 伴侣", "muted", ""
            if diff == 0:
                cmp_text = "（我 = 伴侣）"
            else:
                pct = (diff / pv * 100) if pv else None
                pct_txt = f" · {sign}{abs(pct):.1f}%" if pct is not None else ""
                cmp_text = f"（{rel} {sign}¥{_money(abs(diff))}{pct_txt}）"
            cmp = widgets.ElidedLabel(cmp_text)
            cmp.setObjectName("DaySum")
            widgets._apply_property(cmp, "strongColor", color)
            col.addWidget(cmp)
        elif v > 0:
            ratio = v / total * 100
            sub = QLabel(f"占总资产 {ratio:.0f}%")
            sub.setObjectName("DaySum")
            col.addWidget(sub)

        card._lay.addLayout(col, 1)
        return card

    def _open_account(self, acc: str) -> None:
        dlg = BalanceDialog(self, self._accounts(),
                            "self" if self._asset_owner in (0, 1) else "partner",
                            acc)
        if dlg.exec() != QDialog.Accepted:
            return
        d = dlg.data()
        if not d["account"]:
            return
        services.asset_set_balance(d["owner"], d["account"], d["amount"],
                                   d["date"], d["note"])
        self._reload_assets()

    def _asset_row(self, r: dict) -> QWidget:
        row = QFrame()
        row.setObjectName("LedgerRow")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(10)

        lay.addWidget(widgets.IconBadge(r["channel"], "account", 34,
                                        _account_color(r["channel"])))

        col = QVBoxLayout()
        col.setSpacing(1)
        owner = OWNER_LABEL.get(r["owner"], r["owner"])
        title = widgets.ElidedLabel(f"{owner} · {r['channel']}")
        title.setObjectName("RowTitle")
        parts = [r["date"]]
        if r.get("note"):
            parts.append(r["note"])
        sub = widgets.ElidedLabel(" · ".join(parts))
        sub.setObjectName("DaySum")
        col.addWidget(title)
        col.addWidget(sub)
        lay.addLayout(col, 1)

        delta = r.get("delta")
        if delta is None:
            delta_lbl = QLabel("初始余额")
            delta_lbl.setObjectName("DaySum")
        else:
            delta_lbl = QLabel(f"{'+' if delta >= 0 else '-'}¥{_money(delta)}")
            delta_lbl.setObjectName("Money")
            widgets._apply_property(delta_lbl, "sign",
                                    "income" if delta >= 0 else "expense")
        delta_lbl.setMinimumWidth(110)
        delta_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(delta_lbl)

        bal = QLabel(f"¥{_money(float(r['amount']))}")
        bal.setObjectName("Money")
        widgets._apply_property(bal, "sign", "flat")
        bal.setMinimumWidth(130)
        bal.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(bal)

        dele = QPushButton("✕")
        dele.setObjectName("IconBtn")
        dele.setCursor(Qt.PointingHandCursor)
        dele.clicked.connect(lambda _=False, i=r["id"]: self._asset_delete(i))
        lay.addWidget(dele)
        return row
