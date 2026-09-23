"""科研管理：科研看板 + 研究课题（按六步科研路线组织）。

课题的「阶段」就是科研路线本身：
广泛阅读 → 定缺口与任务 → baseline 评测 → 数据 pipeline → 模型创新 → 写作与投稿。
每个课题另外记录投稿目标（会议 / 期刊 + 截稿日期 + 本人作者角色）和一串自定义 DDL
（实验、消融、baseline、开会…）。DDL 可一键同步成一条待办 —— 本应用的日历页就是
「带日期的待办」的时间轴视图，所以同步之后日历上直接能看到。

论文库不在这里重复建设：元数据与 PDF 归档由 Arxiver 负责，本模块对
``~/.arxiver/library.db`` 全程 mode=ro 只读，只存 arxiv_id + 标题快照。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QDate, QRectF, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QPlainTextEdit, QScrollArea, QFrame, QDialog, QListWidget, QListWidgetItem,
    QStackedWidget, QCheckBox, QSizePolicy, QAbstractItemView,
)

from .. import popups, services, sounds, theme, widgets
from .base import Page
from .finance import SummaryHero
from .habits import EmojiBadge

# 路线步骤的视觉编码（名称与说明文案取自 services.ROUTE_STEPS，避免两处维护）
STEP_COLOR = {"s1": "blue", "s2": "blue", "s3": "amber", "s4": "amber",
              "s5": "accent", "s6": "green", "done": "muted"}
STEP_ICON = {"s1": "📖", "s2": "🔍", "s3": "📊", "s4": "🧱", "s5": "🧪",
             "s6": "✍️", "done": "🏁"}
STEP_NUM = {k: (str(i + 1) if k != "done" else "✓")
            for i, k in enumerate(services.ROUTE_KEYS)}
ROLES = ["一作", "共一", "二作", "三作", "参与", "待定"]
ROLE_COLOR = {"一作": "red", "共一": "amber", "二作": "blue",
              "三作": "blue", "参与": "muted", "待定": "muted"}
PRIORITY_META = {0: ("低", "blue"), 1: ("中", "amber"), 2: ("高", "red")}

VIEWS = (("overview", "📊", "科研看板"), ("projects", "🔬", "研究课题"))


def _step_label(key: str) -> str:
    return services.route_label(key)


def _step_of(proj: dict) -> str:
    """取课题所在的路线步骤；库里存了不认识的值时退回第一步，不整页崩。"""
    key = proj.get("status")
    return key if key in STEP_NUM else services.ROUTE_KEYS[0]


def _step_desc(key: str) -> str:
    return services.route_desc(key)


def _days_left(s: str) -> int | None:
    if not s:
        return None
    d = QDate.fromString(s, "yyyy-MM-dd")
    return QDate.currentDate().daysTo(d) if d.isValid() else None


def _countdown(s: str) -> tuple[str, str] | None:
    """日期 → (文案, 主题色键)。越临近越红，截稿类日期靠这个施压。"""
    left = _days_left(s)
    if left is None:
        return None
    if left < 0:
        return f"已过期 {-left} 天", "red"
    if left == 0:
        return "就是今天", "red"
    if left <= 7:
        return f"剩 {left} 天", "red"
    if left <= 21:
        return f"剩 {left} 天", "amber"
    return f"剩 {left} 天", "blue"


def _fmt_minutes(minutes: float) -> str:
    m = int(round(minutes or 0))
    if m < 60:
        return f"{m} 分钟"
    h, r = divmod(m, 60)
    return f"{h} 小时 {r} 分" if r else f"{h} 小时"


def _split_kw(text: str) -> list[str]:
    return [k.strip() for k in (text or "").split(",") if k.strip()]


def _brief(title: str, cap: int = 14) -> str:
    return services.brief_title(title, cap)


def _paper_open_target(paper: dict) -> tuple[str, str] | None:
    """打开论文的回退顺序：Arxiver 归档好的本地 PDF → PDF 链接 → 摘要页。"""
    local = (paper.get("local_path") or "").strip()
    if local and os.path.exists(local):
        return "file", local
    for key in ("pdf_url", "abs_url"):
        url = (paper.get(key) or "").strip()
        if url.startswith("http"):
            return "url", url
    aid = (paper.get("arxiv_id") or "").strip()
    if aid:
        return "url", f"https://arxiv.org/abs/{aid}"
    return None


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------
def _clear_layout(lay) -> None:
    """清空布局里的所有条目。四条都是踩出来的：

    1. 只处理 ``item.widget()`` 不够 —— 嵌套子布局里的控件不清会留在容器上按
       旧几何继续画，重建一次多一层残影，所以子布局要递归清。
    2. 不要 ``takeAt`` —— 它把 QLayoutItem 的所有权交给调用方，包装器一被回收
       可能连带销毁里面的控件，正在处理点击的那个控件会被自己销毁。
    3. 不要 ``setParent(None)`` —— 同样是所有权转移、当场析构；用 ``hide()``
       加 ``deleteLater()``。
    4. ``addStretch()`` 是 QSpacerItem，既不是控件也不是子布局，不单独处理会让
       这个循环原地卡死。
    """
    while lay.count():
        item = lay.itemAt(0)
        w = item.widget()
        if w is not None:
            lay.removeWidget(w)
            w.hide()
            w.deleteLater()
            continue
        sub = item.layout()
        if sub is not None:
            _clear_layout(sub)
            lay.removeItem(sub)
            continue
        lay.removeItem(item)


def _scroll(content_margins=(0, 0, 8, 0)) -> tuple[QScrollArea, QVBoxLayout]:
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.NoFrame)
    sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    host = QWidget()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(*content_margins)
    lay.setSpacing(10)
    sc.setWidget(host)
    return sc, lay


def _empty_hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("EmptyHint")
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setWordWrap(True)
    return lbl


def _field(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("FieldLabel")
    return lbl


def _link_btn(text: str, on_click, tip: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("LinkBtn")
    b.setCursor(Qt.PointingHandCursor)
    if tip:
        b.setToolTip(tip)
    # clicked 带 checked 布尔：直接连过去，PySide 会把它当成回调的第一个位置参数
    # 塞进去 —— lambda k=默认值 的 k 就被 False 覆盖（曾经把 status 写成 "0"），
    # 无默认参数的绑定方法则直接 TypeError。所以固定吃掉这个参数。
    b.clicked.connect(lambda _checked=False: on_click())
    return b


class DistRow(QWidget):
    """分布行：名称 + 计数 + 细比例条，可点击跳转筛选。"""

    clicked = Signal()

    def __init__(self, name: str, color: str, count: str, ratio: float,
                 clickable: bool = True, parent: QWidget | None = None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(5)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.name_lbl = widgets.ElidedLabel(name)
        self.name_lbl.setObjectName("TileLabel")
        top.addWidget(self.name_lbl, 1)
        cnt = QLabel(count)
        cnt.setObjectName("SideRowCount")
        top.addWidget(cnt)
        v.addLayout(top)
        self.bar = widgets.StatBar(color, 6)
        self.bar.set_value(ratio)
        v.addWidget(self.bar)
        if clickable:
            self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        left = event.button() == Qt.LeftButton
        super().mousePressEvent(event)
        if left:
            self.clicked.emit()


class MiniRow(QFrame):
    """可点击浓缩行：图标 + 主副标题（点行跳到对应课题）。"""

    def __init__(self, icon: str, title: str, sub: str, on_click,
                 tint: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("LedgerRow")
        self.setCursor(Qt.PointingHandCursor)
        self._on_click = on_click
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 6, 8, 6)
        h.setSpacing(8)
        ic = QLabel(icon)
        ic.setFixedWidth(20)
        ic.setAlignment(Qt.AlignCenter)
        if tint:
            ic.setStyleSheet(f"color: {theme.get(tint)}; font-weight: 700;")
        h.addWidget(ic)
        col = QVBoxLayout()
        col.setSpacing(1)
        t = widgets.ElidedLabel(title)
        t.setObjectName("TileLabel")
        col.addWidget(t)
        s = widgets.ElidedLabel(sub)
        s.setObjectName("Meta")
        col.addWidget(s)
        h.addLayout(col, 1)
        arrow = QLabel("›")
        arrow.setObjectName("Meta")
        h.addWidget(arrow)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        left = event.button() == Qt.LeftButton
        super().mousePressEvent(event)
        if left:
            self._on_click()


class RouteBar(QWidget):
    """六步路线的迷你进度条：走过的实心、当前步描边高亮、后面灰。

    画出来而不是排 7 个按钮：课题卡里要一眼看到「走到哪了」，又不该占那么多
    横向空间；切换步骤在详情面板里做，这里只作展示。
    """

    def __init__(self, parent: QWidget | None = None, height: int = 15):
        super().__init__(parent)
        self._key = "s1"
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_step(self, key: str) -> None:
        self._key = key if key in services.ROUTE_KEYS else "s1"
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        keys = services.ROUTE_ACTIVE
        n = len(keys)
        idx = services.route_index(self._key)
        finished = self._key == "done"
        gap, h = 4.0, float(self.height())
        w = (self.width() - gap * (n - 1)) / n
        color = STEP_COLOR.get(self._key, "accent")
        for i in range(n):
            rect = QRectF(i * (w + gap), 0, w, h)
            p.setPen(Qt.PenStyle.NoPen)
            if finished or i < idx:
                p.setBrush(QColor(theme.get("green")))
            elif i == idx:
                p.setBrush(QColor(theme.get(color + "_soft")))
            else:
                p.setBrush(QColor(theme.get("surface_hi")))
            p.drawRoundedRect(rect, 3, 3)
            if i == idx and not finished:
                p.setPen(QPen(QColor(theme.get(color)), 1.4))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(rect, 3, 3)
                p.setPen(QColor(theme.get(color)))
            else:
                p.setPen(QColor("#ffffff") if (finished or i < idx)
                         else QColor(theme.get("muted")))
            f = QFont()
            f.setPixelSize(max(8, int(h * 0.6)))
            f.setBold(i == idx or finished)
            p.setFont(f)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(i + 1))


class RouteCell(QFrame):
    """路线一格：第几步 + 这一步有几个课题 + 是哪几个。点一下跳到该步的列表。"""

    def __init__(self, key: str, names: list[str], on_click,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setCursor(Qt.PointingHandCursor)
        self._on_click = on_click
        color = STEP_COLOR[key]
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 8, 8)
        v.setSpacing(3)

        head = QHBoxLayout()
        head.setSpacing(5)
        num = QLabel(STEP_NUM[key])
        num.setFixedWidth(15)
        num.setAlignment(Qt.AlignCenter)
        num.setStyleSheet(
            f"color: {theme.get(color)}; font-size: 13px; font-weight: 800;")
        head.addWidget(num)
        name = QLabel(_step_label(key))
        name.setObjectName("TileLabel")
        head.addWidget(name, 1)
        v.addLayout(head)

        cnt = QLabel(f"{len(names)} 个课题" if names else "—")
        cnt.setStyleSheet(
            f"color: {theme.get(color if names else 'muted')}; "
            f"font-size: 12px; font-weight: 700;")
        v.addWidget(cnt)

        who = QLabel("、".join(names[:2]) + ("…" if len(names) > 2 else "")
                     if names else _step_desc(key))
        who.setObjectName("Meta")
        who.setWordWrap(True)
        v.addWidget(who)
        self.setToolTip(f"{STEP_NUM[key]} {_step_label(key)}：{_step_desc(key)}")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        left = event.button() == Qt.LeftButton
        super().mousePressEvent(event)
        if left:
            self._on_click()


# ---------------------------------------------------------------------------
# 对话框
# ---------------------------------------------------------------------------
class ProjectDialog(QDialog):
    """新增 / 编辑课题：标题、方向、投稿目标、截稿日期、角色、路线步骤、备注。"""

    def __init__(self, parent, data: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("编辑课题" if data else "新增课题")
        self.setMinimumWidth(540)
        self._data = data or {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(9)

        self.title_input = QLineEdit(self._data.get("title", ""))
        self.title_input.setPlaceholderText("课题标题 *")
        self.title_input.setStyleSheet("font-size: 14px; font-weight: 600;")
        lay.addWidget(self.title_input)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.field_input = QLineEdit(self._data.get("field", ""))
        self.field_input.setPlaceholderText("研究方向（模型合并 / CV 标定 / Agent…）")
        row1.addWidget(self.field_input, 1)
        self.role_combo = widgets.ComboBox()
        self.role_combo.addItems(ROLES)
        role = self._data.get("role") or "待定"
        self.role_combo.setCurrentIndex(ROLES.index(role) if role in ROLES else 0)
        self.role_combo.setFixedWidth(110)
        row1.addWidget(self.role_combo)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.venue_input = QLineEdit(self._data.get("venue", ""))
        self.venue_input.setPlaceholderText("投稿目标（CVPR 2027 / NeurIPS 2027 / 期刊名）")
        row2.addWidget(self.venue_input, 1)
        self.venue_due = widgets.DateInput()
        self.venue_due.setPlaceholderText("截稿日期")
        vd = self._data.get("venue_deadline") or ""
        if vd:
            self.venue_due.setDate(QDate.fromString(vd, "yyyy-MM-dd"))
        self.venue_due.setFixedWidth(150)
        row2.addWidget(self.venue_due)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(8)
        self.step_combo = widgets.ComboBox()
        for k in services.ROUTE_KEYS:
            self.step_combo.addItem(f"{STEP_NUM[k]} {_step_label(k)}", k)
        cur = self._data.get("status", "s1")
        self.step_combo.setCurrentIndex(
            services.route_index(cur if cur in services.ROUTE_KEYS else "s1"))
        self.step_combo.setFixedWidth(160)
        row3.addWidget(self.step_combo)
        self.priority_combo = widgets.ComboBox()
        for i in (0, 1, 2):
            self.priority_combo.addItem(f"优先级·{PRIORITY_META[i][0]}", i)
        self.priority_combo.setCurrentIndex(
            min(int(self._data.get("priority", 1) or 0), 2))
        self.priority_combo.setFixedWidth(124)
        row3.addWidget(self.priority_combo)
        self.plan_due = widgets.DateInput()
        self.plan_due.setPlaceholderText("自定计划完成日")
        pdue = self._data.get("due_date") or ""
        if pdue:
            self.plan_due.setDate(QDate.fromString(pdue, "yyyy-MM-dd"))
        row3.addWidget(self.plan_due, 1)
        lay.addLayout(row3)

        lay.addWidget(_field("科研投入关键词（番茄任务名命中即计入本课题）"))
        self.kw_input = QLineEdit(self._data.get("focus_keywords", ""))
        self.kw_input.setPlaceholderText("逗号分隔，如：消融, merge, 模型合并")
        lay.addWidget(self.kw_input)

        lay.addWidget(_field("备注 / 进展记录"))
        self.notes_input = QPlainTextEdit()
        self.notes_input.setPlaceholderText("研究问题、技术路线、本周进展、现在卡在哪…")
        self.notes_input.setFixedHeight(160)
        self.notes_input.setPlainText(self._data.get("notes", ""))
        lay.addWidget(self.notes_input)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存")
        ok.setObjectName("Primary")
        ok.clicked.connect(self._accept)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)

    def _accept(self) -> None:
        if not self.title_input.text().strip():
            self.title_input.setFocus()
            return
        self.accept()

    def result_data(self) -> dict:
        vd = self.venue_due.date()
        pd = self.plan_due.date()
        return {
            "title": self.title_input.text().strip(),
            "field": self.field_input.text().strip(),
            "status": self.step_combo.currentData(),
            "priority": self.priority_combo.currentIndex(),
            "notes": self.notes_input.toPlainText().strip(),
            "due_date": pd.toString("yyyy-MM-dd") if pd.isValid() else "",
            "focus_keywords": self.kw_input.text().strip(),
            "venue": self.venue_input.text().strip(),
            "venue_deadline": vd.toString("yyyy-MM-dd") if vd.isValid() else "",
            "role": self.role_combo.currentText(),
        }


class MilestoneAddDialog(QDialog):
    """加 / 改一条自定义 DDL：名称 + 截止日期 + 备注 + 是否同步到日历。"""

    def __init__(self, parent, ms: dict | None = None):
        super().__init__(parent)
        self._ms = ms
        self.setWindowTitle("改这条 DDL" if ms else "新增 DDL")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(9)

        self.title_input = QLineEdit((ms or {}).get("title", ""))
        self.title_input.setPlaceholderText("DDL 名称，如：跑完消融实验 *")
        lay.addWidget(self.title_input)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.date_edit = widgets.DateInput()
        self.date_edit.setPlaceholderText("截止日期（不填就不进日历）")
        due = (ms or {}).get("due_date", "")
        if due:
            self.date_edit.setDate(QDate.fromString(due, "yyyy-MM-dd"))
        row.addWidget(self.date_edit, 1)
        quick = QHBoxLayout()
        quick.setSpacing(2)
        for label, days in (("1周", 7), ("2周", 14), ("1月", 30), ("3月", 90)):
            quick.addWidget(_quick_day_btn(label, self.date_edit, days))
        row.addLayout(quick)
        lay.addLayout(row)

        self.note_input = QLineEdit((ms or {}).get("note", ""))
        self.note_input.setPlaceholderText("备注（可选）")
        lay.addWidget(self.note_input)

        self.sync_cb = QCheckBox("同步到日历（生成一条待办，日历页按日期显示）")
        # 新建默认勾上；编辑时尊重现状（已经同步过的别给取消掉）
        self.sync_cb.setChecked(True if ms is None else bool(ms.get("todo_id")))
        lay.addWidget(self.sync_cb)
        # 没有日期就同步不出待办 —— 让勾选框自己说清楚，别让用户点了没反应
        self.date_edit.textChanged.connect(lambda _: self._sync_available())
        self._sync_available()

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存" if ms else "添加")
        ok.setObjectName("Primary")
        ok.clicked.connect(self._accept)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)
        self.title_input.setFocus()

    def _sync_available(self) -> None:
        ok = self.date_edit.date().isValid()
        was = self.sync_cb.isEnabled()
        self.sync_cb.setEnabled(ok)
        self.sync_cb.setToolTip(
            "" if ok else "先填截止日期，日历上才有地方摆这条待办")
        if not ok:
            self.sync_cb.setChecked(False)
        elif not was:
            self.sync_cb.setChecked(True)

    def _accept(self) -> None:
        if not self.title_input.text().strip():
            self.title_input.setFocus()
            return
        self.accept()

    def values(self) -> tuple[str, str, str, bool]:
        d = self.date_edit.date()
        return (self.title_input.text().strip(),
                d.toString("yyyy-MM-dd") if d.isValid() else "",
                self.note_input.text().strip(),
                self.sync_cb.isChecked())


def _quick_day_btn(label: str, date_edit, days: int) -> QPushButton:
    def _set(_=None):
        date_edit.setDate(QDate.currentDate().addDays(days))
    return _link_btn(label, _set, f"{days} 天后")


class ArxivPickerDialog(QDialog):
    """从 Arxiver 论文库里挑论文挂到课题上（对它的库只读，不写任何东西）。

    选择走 Windows 原生肌肉记忆：单击选中、Ctrl 加选、Shift 连选、双击直接关联，
    不要求用户去点小勾选框才能多选。
    """

    def __init__(self, parent, project_id: int = 0):
        super().__init__(parent)
        self._pid = project_id
        proj = services.research_get(project_id) if project_id else {}
        self.setWindowTitle(f"从 Arxiver 挑选论文 · {(proj or {}).get('title', '')}")
        self.setMinimumSize(640, 480)
        self._linked = {p["arxiv_id"] for p in services.research_paper_list(project_id)}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(9)

        # 防抖定时器要先于 textChanged 接线：构造过程中任何 setText 都会立刻打到
        # _schedule_load，那时 _timer 还不存在就炸。
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(320)
        self._timer.timeout.connect(self._load)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 搜标题 / 中文标题 / 作者 / 标签 / arXiv ID"
                                       "（空格分词，多个词同时命中）")
        self.search.textChanged.connect(self._schedule_load)
        bar.addWidget(self.search, 1)
        self.local_only = QCheckBox("只看已下载")
        self.local_only.toggled.connect(lambda _: self._load())
        bar.addWidget(self.local_only)
        lay.addLayout(bar)

        self.hint = QLabel("")
        self.hint.setObjectName("Hint")
        lay.addWidget(self.hint)

        self.list = QListWidget()
        self.list.setUniformItemSizes(True)
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.itemSelectionChanged.connect(self._update_count)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        lay.addWidget(self.list, 1)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        tip = QLabel("单击选一条 · Ctrl 加选 · Shift 连选 · 双击直接关联")
        tip.setObjectName("Meta")
        foot.addWidget(tip)
        foot.addStretch(1)
        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("Meta")
        foot.addWidget(self.count_lbl)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("关联所选")
        ok.setObjectName("Primary")
        ok.clicked.connect(self.accept)
        foot.addWidget(cancel)
        foot.addWidget(ok)
        lay.addLayout(foot)
        # 首次加载放到最后：_load() 会回头写底部那几个控件，先建好才不会踩空。
        if not services.arxiver_available():
            self.hint.setText(
                f"没找到 Arxiver 论文库：{services.arxiver_db_path()}\n"
                "先在 Arxiver 里抓过论文，这里才会出现候选。")
        else:
            self._load()
            self.search.setFocus()

    def _schedule_load(self) -> None:
        self._timer.start()

    def _load(self) -> None:
        rows = services.arxiver_search(self.search.text(), limit=200,
                                       only_local=self.local_only.isChecked())
        self.list.clear()
        linked_shown = 0
        for r in rows:
            zh = (r.get("title_zh") or "").strip()
            bits = [x for x in ((r.get("published") or "")[:4],
                                (r.get("authors") or "").split(",")[0].strip(),
                                "已下载" if (r.get("local_path") or "").strip() else "")
                    if x]
            text = r.get("title") or r.get("arxiv_id") or ""
            # 中文标题为空时别留一个孤零零的「|」
            head = f"{text}｜{zh}" if zh else text
            already = r.get("arxiv_id") in self._linked
            if already:
                linked_shown += 1
            item = QListWidgetItem(
                f"{'✓ ' if already else ''}{head}\n{' · '.join(bits)}　[{r.get('arxiv_id')}]")
            item.setData(Qt.ItemDataRole.UserRole, r)
            # QListWidgetItem 默认就带 ItemIsUserCheckable，不清掉会在行首画出
            # 一个永远用不上的空勾选框。
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            if already:
                item.setToolTip("这个课题已经关联过了")
                item.setForeground(QColor(theme.get("muted")))
            self.list.addItem(item)
        if services.arxiver_available():
            extra = f" · 其中 {linked_shown} 条已关联（灰显）" if linked_shown else ""
            self.hint.setText(
                f"命中 {self.list.count()} 条 · 按 Arxiver 抓取时间倒序，最多列 200 条"
                f"{extra}；关键词写得更具体能缩小范围")
        self._update_count()

    def _update_count(self) -> None:
        n = len(self.selected())
        self.count_lbl.setText(f"已选 {n} 篇" if n else "")

    def selected(self) -> list[dict]:
        """当前选中、且这个课题还没关联过的论文。"""
        out = []
        for item in self.list.selectedItems():
            r = item.data(Qt.ItemDataRole.UserRole)
            if r and r.get("arxiv_id") not in self._linked:
                out.append(r)
        return out


class KeywordsDialog(QDialog):
    """编辑「哪些番茄任务算科研投入」的全局关键词。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("科研投入关键词")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(9)
        tip = QLabel("番茄钟里任务名命中下面任一关键词，就计入看板的「科研专注投入」。逗号分隔。")
        tip.setWordWrap(True)
        tip.setObjectName("Meta")
        lay.addWidget(tip)
        self.input = QLineEdit(",".join(services.research_keywords()))
        lay.addWidget(self.input)
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存")
        ok.setObjectName("Primary")
        ok.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)

    def keywords_text(self) -> str:
        return self.input.text().strip()

# ---------------------------------------------------------------------------
# 课题卡 / DDL 行 / 关联论文行
# ---------------------------------------------------------------------------
class ProjectCard(QFrame):
    """课题卡：一眼看到「投稿到哪、还有几天、走到路线第几步、下一个 DDL 是什么」。"""

    def __init__(self, page: "ResearchPage", proj: dict, papers: int, ms: dict):
        super().__init__()
        self.page = page
        self.rid = proj["id"]
        self.setObjectName("Card")
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 13, 14, 13)
        lay.setSpacing(7)

        key = _step_of(proj)
        color = STEP_COLOR.get(key, "accent")
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(EmojiBadge(STEP_ICON.get(key, "🔬"), color, 28))
        title = widgets.ElidedLabel(proj["title"])
        title.setObjectName("RowTitle")
        top.addWidget(title, 1)
        role = proj.get("role") or ""
        if role and role != "待定":
            top.addWidget(widgets.Tag(role, ROLE_COLOR.get(role, "muted")))
        if int(proj.get("priority", 1) or 0) == 2:
            top.addWidget(widgets.Tag("优先级·高", PRIORITY_META[2][1]))
        lay.addLayout(top)

        # 投稿目标 + 截稿倒计时（科研最硬的时间压力，放最显眼的一行）
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        venue = (proj.get("venue") or "").strip()
        cd = _countdown(proj.get("venue_deadline") or "")
        if venue:
            row2.addWidget(widgets.Tag(f"🎯 {venue}", color))
        if cd:
            row2.addWidget(widgets.Tag(f"⏳ {cd[0]}", cd[1]))
        elif not venue:
            row2.addWidget(widgets.Tag("未定投稿目标", "muted"))
        row2.addStretch(1)
        lay.addLayout(row2)

        bar = RouteBar()
        bar.set_step(key)
        lay.addWidget(bar)

        # 只写步骤名，不写整句说明：同一步的几张卡会重复同一长句，窄窗口下
        # 光这句就吃掉三行。完整说明挂在鼠标上，详情面板里也有一份。
        step_txt = QLabel(f"{STEP_NUM[key]} {_step_label(key)}")
        step_txt.setObjectName("Meta")
        step_txt.setToolTip(_step_desc(key))
        step_txt.setStyleSheet("font-size: 12px;")
        lay.addWidget(step_txt)

        row3 = QHBoxLayout()
        row3.setSpacing(6)
        total = ms.get("total", 0)
        if total:
            row3.addWidget(widgets.Tag(
                f"DDL {ms.get('done', 0)}/{total} 完成",
                "red" if ms.get("overdue") else "accent"))
            if ms.get("overdue"):
                row3.addWidget(widgets.Tag(f"⚠ 逾期 {ms['overdue']}", "red"))
        if papers:
            row3.addWidget(widgets.Tag(f"📄 Arxiver {papers} 篇", "blue"))
        row3.addStretch(1)
        nxt = self._next_open_milestone()
        if nxt:
            n = widgets.ElidedLabel(f"下一个：{nxt[0]}（{nxt[1]}）")
            n.setObjectName("Meta")
            row3.addWidget(n, 2)
        lay.addLayout(row3)

        if proj.get("notes"):
            note = widgets.ElidedLabel(proj["notes"].replace("\n", " "))
            note.setObjectName("Meta")
            lay.addWidget(note)

        acts = QHBoxLayout()
        acts.setSpacing(4)
        acts.addStretch(1)
        nxt_step = self._next_step(key)
        if nxt_step:
            acts.addWidget(_link_btn(
                f"→ {_step_label(nxt_step)}",
                lambda k=nxt_step: self.page.set_step(self.rid, k),
                "推进到路线下一步"))
        # 不再放「详情」按钮：单击卡片本身就是选中并在右边打开详情，重复一个入口
        # 只会让人以为点卡片不够。
        dele = QPushButton("🗑")
        dele.setObjectName("IconBtn")
        dele.setCursor(Qt.PointingHandCursor)
        dele.setToolTip("删除课题")
        dele.clicked.connect(lambda: self.page.delete_project(self.rid))
        acts.addWidget(dele)
        lay.addLayout(acts)

    def _next_open_milestone(self) -> tuple[str, str] | None:
        for m in services.milestone_list(self.rid):
            if m["done"]:
                continue
            cd = _countdown(m["due_date"])
            return (m["title"], cd[0] if cd else "未定日期")
        return None

    @staticmethod
    def _next_step(key: str) -> str | None:
        if key == "done":
            return None
        i = services.route_index(key)
        return services.ROUTE_ACTIVE[i + 1] if i + 1 < len(services.ROUTE_ACTIVE) else None

    def set_selected(self, on: bool) -> None:
        widgets._apply_property(self, "selected", "true" if on else "false")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        left = event.button() == Qt.LeftButton
        super().mousePressEvent(event)
        if left:
            self.page.select_project(self.rid)


class MilestoneRow(QFrame):
    """详情面板里的一条 DDL：勾选完成 / 改期 / 是否已同步日历 / 删除。"""

    def __init__(self, page: "ResearchPage", ms: dict):
        super().__init__()
        self.setObjectName("LedgerRow")
        self.page = page
        self.ms_id = ms["id"]
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 5, 6, 5)
        h.setSpacing(7)

        cb = QCheckBox()
        cb.setChecked(bool(ms["done"]))
        cb.setToolTip("标记完成")
        cb.toggled.connect(lambda on: page.toggle_milestone(self.ms_id, on))
        h.addWidget(cb)

        col = QVBoxLayout()
        col.setSpacing(1)
        t = widgets.ElidedLabel(ms["title"])
        t.setObjectName("TileLabel")
        if ms["done"]:
            t.setStyleSheet("text-decoration: line-through; opacity: .55;")
        col.addWidget(t)
        cd = _countdown(ms["due_date"])
        sub = ms["due_date"] or "没填日期 · 不进日历"
        if cd and not ms["done"]:
            sub = f"{ms['due_date']} · {cd[0]}"
        if ms.get("note"):
            sub += f" · {ms['note']}"
        s = widgets.ElidedLabel(sub)
        s.setObjectName("Meta")
        if cd and not ms["done"] and cd[1] == "red":
            s.setStyleSheet(f"color: {theme.get('red')};")
        col.addWidget(s)
        h.addLayout(col, 1)

        edit = QPushButton("✎")
        edit.setObjectName("IconBtn")
        edit.setCursor(Qt.PointingHandCursor)
        edit.setToolTip("改名称 / 日期 / 备注（双击这一行也可以）")
        edit.clicked.connect(lambda: page.edit_milestone(self.ms_id))
        h.addWidget(edit)

        # 两个状态都用文字，别一边是图标一边是文字：单看一个 📅 猜不出是「已同步」
        # 还是「点一下同步」。字数压到最短，否则面板只有 372px 宽，DDL 名字先被截。
        if ms.get("todo_id"):
            h.addWidget(_link_btn("已同步",
                                  lambda: page.unsync_milestone(self.ms_id),
                                  "已在日历里（清单：科研DDL）；点一下取消同步，"
                                  "会删掉那条待办"))
        elif ms["due_date"]:
            h.addWidget(_link_btn("同步", lambda: page.sync_milestone(self.ms_id),
                                  "生成一条待办，日历页按日期显示"))
        dele = QPushButton("🗑")
        dele.setObjectName("IconBtn")
        dele.setCursor(Qt.PointingHandCursor)
        dele.setToolTip("删除这条 DDL")
        dele.clicked.connect(lambda: page.delete_milestone(self.ms_id))
        h.addWidget(dele)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.page.edit_milestone(self.ms_id)
            return
        super().mouseDoubleClickEvent(event)


class LinkedPaperRow(QFrame):
    """关联论文一行：标题 + 中文标题/年份 + 打开 / 移除。"""

    def __init__(self, page: "ResearchPage", paper: dict):
        super().__init__()
        self.setObjectName("LedgerRow")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 6, 6, 6)
        h.setSpacing(8)
        col = QVBoxLayout()
        col.setSpacing(1)
        t = widgets.ElidedLabel(paper["title"] or paper["arxiv_id"])
        t.setObjectName("TileLabel")
        col.addWidget(t)
        bits = [(paper.get("title_zh") or "").strip(),
                (paper.get("published") or "")[:4],
                f"arXiv:{paper['arxiv_id']}"]
        if paper.get("missing"):
            bits.append("Arxiver 里已不存在")
        if (paper.get("local_path") or "").strip():
            bits.append("已下载")
        s = widgets.ElidedLabel(" · ".join(b for b in bits if b))
        s.setObjectName("Meta")
        col.addWidget(s)
        h.addLayout(col, 1)
        if not paper.get("missing"):
            h.addWidget(_link_btn(
                "打开", lambda: page.open_arxiv_paper(paper),
                "优先打开 Arxiver 归档的本地 PDF，否则开 arXiv 页面"))
        dele = QPushButton("🗑")
        dele.setObjectName("IconBtn")
        dele.setCursor(Qt.PointingHandCursor)
        dele.setToolTip("解除关联（不会删 Arxiver 里的论文）")
        dele.clicked.connect(lambda: page.unlink_paper(paper["id"]))
        h.addWidget(dele)


# ---------------------------------------------------------------------------
# 右栏：课题详情
# ---------------------------------------------------------------------------
class ProjectDetailPane(QFrame):
    """投稿信息、路线 stepper、DDL 清单、投入、进展记录、关联论文。"""

    def __init__(self, page: "ResearchPage"):
        super().__init__(page)
        self.page = page
        self._rid = 0
        self._loading = False
        self._pending_rid = 0
        self._flash_mid = 0
        self.setObjectName("DetailPane")
        self.setFixedWidth(396)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.empty = QLabel("点击左侧课题\n查看投稿进度与 DDL")
        self.empty.setObjectName("DetailEmpty")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.hide()
        root.addWidget(self.empty, 1)

        self.scroll, form = _scroll((16, 14, 14, 16))
        self.scroll.hide()
        root.addWidget(self.scroll, 1)

        head = QHBoxLayout()
        head.setSpacing(9)
        self.badge = EmojiBadge("🔬", "accent", 32)
        head.addWidget(self.badge, 0, Qt.AlignTop)
        self.title_lbl = QLabel("")
        self.title_lbl.setObjectName("DetailTitleInput")
        self.title_lbl.setWordWrap(True)
        head.addWidget(self.title_lbl, 1)
        self.edit_btn = QPushButton("✎")
        self.edit_btn.setObjectName("ToolBtn")
        self.edit_btn.setCursor(Qt.PointingHandCursor)
        self.edit_btn.setToolTip("编辑课题（标题 / 方向 / 备注全文）")
        self.edit_btn.clicked.connect(lambda: self.page.edit_project(self._rid))
        head.addWidget(self.edit_btn, 0, Qt.AlignTop)
        form.addLayout(head)

        # ---- 投稿 ----
        form.addWidget(_field("投稿目标与截稿"))
        self.venue_edit = QLineEdit()
        self.venue_edit.setPlaceholderText("CVPR 2027 / NeurIPS 2027 / 期刊名")
        self.venue_edit.textChanged.connect(lambda _: self._schedule_save())
        form.addWidget(self.venue_edit)
        vrow = QHBoxLayout()
        vrow.setSpacing(8)
        self.venue_due = widgets.DateInput()
        self.venue_due.setPlaceholderText("截稿日期未定")
        self.venue_due.textChanged.connect(lambda _: self._schedule_save())
        vrow.addWidget(self.venue_due, 1)
        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("HeroBig")
        self.count_lbl.setStyleSheet("font-size: 22px;")
        self.count_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        vrow.addWidget(self.count_lbl)
        form.addLayout(vrow)

        # ---- 路线 ----
        form.addWidget(_field("科研路线（点一下切到现在这步）"))
        self._step_btns: dict[str, QPushButton] = {}
        grid = QHBoxLayout()
        grid.setSpacing(5)
        keys = services.ROUTE_KEYS
        half = 4
        for group in (keys[:half], keys[half:]):
            col = QVBoxLayout()
            col.setSpacing(5)
            for k in group:
                b = QPushButton(f"{STEP_NUM[k]} {_step_label(k)}")
                b.setObjectName("TagChip")
                b.setCheckable(True)
                b.setCursor(Qt.PointingHandCursor)
                widgets._apply_property(b, "tagColor", STEP_COLOR[k])
                b.setToolTip(_step_desc(k))
                b.clicked.connect(lambda _, key=k: self.page.set_step(self._rid, key))
                self._step_btns[k] = b
                col.addWidget(b)
            grid.addLayout(col, 1)
        form.addLayout(grid)
        self.step_desc = QLabel("")
        self.step_desc.setObjectName("Meta")
        self.step_desc.setWordWrap(True)
        self.step_desc.setStyleSheet("padding: 2px 2px 0;")
        form.addWidget(self.step_desc)

        # ---- DDL ----
        ddl_head = QHBoxLayout()
        ddl_head.setSpacing(6)
        ddl_head.addWidget(_field("自定义 DDL"))
        self.ddl_count = QLabel("")
        self.ddl_count.setObjectName("Meta")
        ddl_head.addWidget(self.ddl_count)
        ddl_head.addStretch(1)
        ddl_head.addWidget(_link_btn("＋ 加一条", self.page.add_milestone,
                                     "如：跑完消融 / baseline 评测 / 开组会"))
        form.addLayout(ddl_head)
        self.ddl_holder = QVBoxLayout()
        self.ddl_holder.setSpacing(2)
        form.addLayout(self.ddl_holder)

        # ---- 角色 / 优先级 ----
        row = QHBoxLayout()
        row.setSpacing(8)
        c1 = QVBoxLayout()
        c1.setSpacing(4)
        c1.addWidget(_field("我的角色"))
        self.role_combo = widgets.ComboBox()
        self.role_combo.addItems(ROLES)
        self.role_combo.currentTextChanged.connect(self._set_role)
        c1.addWidget(self.role_combo)
        row.addLayout(c1, 1)
        c2 = QVBoxLayout()
        c2.setSpacing(4)
        c2.addWidget(_field("优先级"))
        self.priority_combo = widgets.ComboBox()
        for i in (0, 1, 2):
            self.priority_combo.addItem(PRIORITY_META[i][0], i)
        self.priority_combo.currentIndexChanged.connect(self._set_priority)
        c2.addWidget(self.priority_combo)
        row.addLayout(c2, 1)
        form.addLayout(row)

        self.meta_holder = QVBoxLayout()
        self.meta_holder.setSpacing(2)
        form.addLayout(self.meta_holder)

        form.addWidget(_field("科研投入关键词（逗号分隔）"))
        self.kw_edit = QLineEdit()
        self.kw_edit.setPlaceholderText("命中番茄任务名即计入本课题投入")
        self.kw_edit.editingFinished.connect(self._save_keywords)
        self.kw_edit.textChanged.connect(lambda _: self._schedule_save())
        form.addWidget(self.kw_edit)

        notes_head = QHBoxLayout()
        notes_head.setSpacing(6)
        notes_head.addWidget(_field("进展记录"))
        notes_head.addStretch(1)
        self.focus_lbl = QLabel("")
        self.focus_lbl.setObjectName("Meta")
        notes_head.addWidget(self.focus_lbl)
        form.addLayout(notes_head)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("研究问题、技术路线、本周进展、现在卡在哪…")
        self.notes_edit.setMinimumHeight(140)
        self.notes_edit.textChanged.connect(self._schedule_save)
        form.addWidget(self.notes_edit)

        paper_head = QHBoxLayout()
        paper_head.setSpacing(6)
        paper_head.addWidget(_field("关联论文（Arxiver）"))
        paper_head.addStretch(1)
        paper_head.addWidget(_link_btn("＋ 挑选", self.page.pick_papers,
                                       "从 Arxiver 论文库里搜索并关联"))
        paper_head.addWidget(_link_btn("打开 Arxiver ›", self.page.open_arxiver_tool))
        form.addLayout(paper_head)
        self.paper_holder = QVBoxLayout()
        self.paper_holder.setSpacing(2)
        form.addLayout(self.paper_holder)

        acts = QHBoxLayout()
        acts.setSpacing(5)
        acts.addStretch(1)
        dele = QPushButton("删除这个课题")
        dele.setObjectName("Ghost")
        dele.setCursor(Qt.PointingHandCursor)
        dele.clicked.connect(lambda: self.page.delete_project(self._rid))
        # 颜色不在这里定：详情面板是一次性搭出来的常驻控件，切主题时不会重建，
        # 所以每次回填时重刷（见 show_project）。
        self.del_btn = dele
        acts.addWidget(dele)
        form.addLayout(acts)

        # 输入即存：不能只挂 editingFinished —— 用户敲完常是点面板空白处
        # （QLabel 不可聚焦，焦点根本没离开输入框），那时它不触发，改动就丢了。
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(800)
        self._timer.timeout.connect(self._flush_pending)

    # ---------- 回填 ----------
    def show_project(self, proj: dict | None) -> None:
        # 回填前先把没保存的字冲回库里。只挡 hasFocus() 不够：焦点一旦离开输入框
        # （点了步骤 chip、按了 Tab、切了课题）就挡不住，而 800ms 防抖可能还没到点，
        # 下面 setText 会把库里没保存的旧值盖回输入框 —— 用户刚打的字凭空消失。
        # 切课题时更要先落盘，否则这段字会串到新课题上（_pending_rid 记着归属）。
        if self._timer.isActive() and self._pending_rid:
            self._timer.stop()
            self._flush_pending()
        if not proj:
            self._rid = 0
            self.empty.show()
            self.scroll.hide()
            return
        self._loading = True
        self._rid = proj["id"]
        self.empty.hide()
        self.scroll.show()
        self.del_btn.setStyleSheet(
            f"color: {theme.get('red')}; padding: 7px 12px;")

        key = _step_of(proj)
        color = STEP_COLOR.get(key, "accent")
        self.badge.set_icon(STEP_ICON.get(key, "🔬"), color)
        self.title_lbl.setText(proj["title"])
        if not self.venue_edit.hasFocus():
            self.venue_edit.setText(proj.get("venue") or "")
        vd = proj.get("venue_deadline") or ""
        if not self.venue_due.hasFocus():
            self.venue_due.setDate(
                QDate.fromString(vd, "yyyy-MM-dd")) if vd else self.venue_due.setText("")
        cd = _countdown(vd)
        if cd:
            self.count_lbl.setText(cd[0])
            self.count_lbl.setStyleSheet(
                f"font-size: 22px; color: {theme.get(cd[1])}; font-weight: 800;")
        else:
            self.count_lbl.setText("未定")
            self.count_lbl.setStyleSheet(
                f"font-size: 22px; color: {theme.get('muted')}; font-weight: 800;")
        for k, b in self._step_btns.items():
            b.setChecked(k == key)
        self.step_desc.setText(f"{STEP_NUM[key]} {_step_label(key)}：{_step_desc(key)}")

        role = proj.get("role") or "待定"
        self.role_combo.setCurrentText(role if role in ROLES else "待定")
        self.priority_combo.setCurrentIndex(
            min(int(proj.get("priority", 1) or 0), 2))

        _clear_layout(self.meta_holder)
        if proj.get("field"):
            self.meta_holder.addWidget(MetaRowLite("🧭", "研究方向 · " + proj["field"]))
        plan = _countdown(proj.get("due_date") or "")
        if plan:
            self.meta_holder.addWidget(MetaRowLite(
                "🗓", f"自定计划 {proj['due_date']} · {plan[0]}"))
        self.meta_holder.addWidget(MetaRowLite(
            "🕘", "最近更新 · " + (proj.get("updated_at") or "")[:16]))

        if not self.kw_edit.hasFocus():
            self.kw_edit.setText(proj.get("focus_keywords") or "")
        if not self.notes_edit.hasFocus():
            self.notes_edit.setPlainText(proj.get("notes") or "")
        minutes = services.project_focus_minutes(proj)
        self.focus_lbl.setText(f"近 30 天 {_fmt_minutes(minutes)}")

        _clear_layout(self.ddl_holder)
        mss = services.milestone_list(proj["id"])
        done = sum(1 for m in mss if m["done"])
        self.ddl_count.setText(f"{done}/{len(mss)} 完成" if mss else "还没有 DDL")
        if not mss:
            self.ddl_holder.addWidget(_empty_hint(
                "点「＋ 加一条」记录实验 / 消融 / baseline / 开会这些节点"))
        for m in mss:
            self.ddl_holder.addWidget(MilestoneRow(self.page, m))

        _clear_layout(self.paper_holder)
        papers = services.research_paper_list(proj["id"])
        if not papers:
            self.paper_holder.addWidget(_empty_hint(
                "还没关联论文" if services.arxiver_available()
                else "未检测到 Arxiver 论文库"))
        for p in papers:
            self.paper_holder.addWidget(LinkedPaperRow(self.page, p))
        self._loading = False
        # 行是刚重建出来的，从看板带过来的高亮要重新贴一次
        self._apply_flash()

    def locate_milestone(self, mid: int) -> None:
        """滚到某条 DDL 并亮一下（看板点进来时用）。

        高亮状态挂在面板上而不是那一行上：行会被后续刷新重建，而且行自己定的
        撤销定时器会跑在重建之前，结果就是用户什么都没看到。
        """
        self._flash_mid = mid
        self._apply_flash()
        QTimer.singleShot(1600, self._clear_flash)

    def _apply_flash(self) -> None:
        """给当前指向那条 DDL 上高亮；面板每次重建后都要补一次。"""
        if not self._flash_mid:
            return
        # ddl_holder 是 QVBoxLayout 不是控件，addWidget 后行的 parent 是内容面板，
        # 所以只能从 findChildren 里捞（MilestoneRow 只出现在 DDL 清单里）。
        hit = False
        for r in self.findChildren(MilestoneRow):
            if r.ms_id == self._flash_mid:
                r.setStyleSheet(
                    f"background: {theme.get('accent_soft')}; border-radius: 8px;")
                self.scroll.ensureWidgetVisible(r, 0, 120)
                hit = True
            elif r.styleSheet():
                r.setStyleSheet("")
        if not hit:
            self._flash_mid = 0

    def _clear_flash(self) -> None:
        self._flash_mid = 0
        for r in self.findChildren(MilestoneRow):
            if r.styleSheet():
                r.setStyleSheet("")

    # ---------- 写回 ----------
    def _update(self, **fields) -> None:
        if self._rid:
            services.research_update(self._rid, **fields)

    def _schedule_save(self) -> None:
        if self._loading:
            return
        self._pending_rid = self._rid
        self._timer.start()

    def _flush_pending(self) -> None:
        rid = self._pending_rid or self._rid
        self._pending_rid = 0
        if not rid:
            return
        fields = {
            "venue": self.venue_edit.text().strip(),
            "notes": self.notes_edit.toPlainText().strip(),
            "focus_keywords": self.kw_edit.text().strip(),
        }
        text = self.venue_due.text().strip()
        d = self.venue_due.date()
        if not text:
            fields["venue_deadline"] = ""
        elif d.isValid():
            fields["venue_deadline"] = d.toString("yyyy-MM-dd")
        # 只敲了一半的非法日期不写，免得把原来的截稿日期冲掉
        services.research_update(rid, **fields)
        self.page.reload()

    def _set_role(self, text: str) -> None:
        if self._loading or not self._rid:
            return
        self._update(role=text)
        self.page.reload()

    def _set_priority(self) -> None:
        if self._loading or not self._rid:
            return
        self._update(priority=self.priority_combo.currentIndex())
        self.page.reload()

    def _save_keywords(self) -> None:
        if self._loading:
            return
        self._update(focus_keywords=self.kw_edit.text().strip())
        self.page.reload()


class MetaRowLite(QFrame):
    """详情面板里的一行只读元信息。"""

    def __init__(self, icon: str, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("DetailMetaRow")
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 5, 8, 5)
        h.setSpacing(8)
        ic = QLabel(icon)
        ic.setObjectName("DetailMetaText")
        ic.setFixedWidth(16)
        h.addWidget(ic)
        lbl = widgets.ElidedLabel(text)
        lbl.setObjectName("DetailMetaText")
        h.addWidget(lbl, 1)


# ---------------------------------------------------------------------------
# 视图：科研看板
# ---------------------------------------------------------------------------
class OverviewView(QWidget):
    """看板：最近的截稿、路线分布、接下来要交的 DDL、科研投入。"""

    def __init__(self, page: "ResearchPage"):
        super().__init__()
        self.page = page
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        # 视图名与「新增课题」在页面顶栏里，这里不再叠一层标题头
        self.scroll, self.lay = _scroll((20, 8, 20, 20))
        self.lay.setSpacing(14)
        root.addWidget(self.scroll, 1)

    @staticmethod
    def _card(title: str, hint: str = "") -> tuple[QFrame, QVBoxLayout, QHBoxLayout]:
        card = widgets.Card()
        head = QHBoxLayout()
        head.setSpacing(6)
        t = QLabel(title)
        t.setObjectName("CardTitle")
        head.addWidget(t)
        if hint:
            s = QLabel(hint)
            s.setObjectName("Meta")
            head.addWidget(s)
        head.addStretch(1)
        card.body().addLayout(head)
        return card, card.body(), head

    @staticmethod
    def _dist_card(title: str, hint: str, rows: list[tuple]) -> QFrame:
        """分布卡：rows = [(名称, 色键, 计数文字, 比例, 点击回调或 None)]。"""
        card, lay, head = OverviewView._card(title, hint)
        lay.setSpacing(10)
        if not rows:
            lay.addWidget(_empty_hint("暂无数据"))
        for name, color, count, ratio, cb in rows:
            row = DistRow(name, color, count, ratio, clickable=cb is not None)
            if cb is not None:
                row.clicked.connect(cb)
            lay.addWidget(row)
        return card

    @staticmethod
    def _list_card(title: str, hint: str, items: list[QWidget], empty_text: str,
                   extra: QWidget | None = None) -> QFrame:
        card, lay, head = OverviewView._card(title, hint)
        if extra is not None:
            head.addWidget(extra)
        lay.setSpacing(2)
        if not items:
            lay.addWidget(_empty_hint(empty_text))
        for w in items:
            lay.addWidget(w)
        return card

    def reload(self) -> None:
        page = self.page
        projects = services.research_list()
        counts = services.research_paper_counts()
        ms_counts = services.milestone_counts()
        open_ddls = services.milestone_open_all(12)
        focus = services.research_focus_summary()

        sb = self.scroll.verticalScrollBar()
        pos = sb.value()
        _clear_layout(self.lay)

        if not projects:
            card, lay, _ = self._card("先立一个课题")
            tip = QLabel(
                "还没有研究课题。新建一个课题，填上投稿目标（CVPR / NeurIPS / 期刊）"
                "和截稿日期，再按科研路线标出现在走到第几步；下面这些卡片就会自动"
                "汇总出倒计时、路线分布和接下来要交的 DDL。")
            tip.setWordWrap(True)
            tip.setObjectName("Meta")
            lay.addWidget(tip)
            row = QHBoxLayout()
            row.setSpacing(8)
            b1 = QPushButton("＋ 新增课题")
            b1.setObjectName("Primary")
            b1.setCursor(Qt.PointingHandCursor)
            b1.clicked.connect(page.add_project)
            b2 = QPushButton("打开 Arxiver ›")
            b2.setObjectName("Ghost")
            b2.setCursor(Qt.PointingHandCursor)
            b2.clicked.connect(page.open_arxiver_tool)
            row.addWidget(b1)
            row.addWidget(b2)
            row.addStretch(1)
            lay.addLayout(row)
            self.lay.addWidget(card)
            self.lay.addStretch(1)
            sb.setValue(pos)
            return

        # 最近的一个截稿（含课题自身的计划日与 DDL）
        dated = []
        for p in projects:
            if p["status"] == "done":
                continue
            if p.get("venue_deadline"):
                dated.append((_days_left(p["venue_deadline"]),
                              f"{p['venue'] or '投稿'} 截稿 · {_brief(p['title'])}",
                              p["venue_deadline"], p["id"], 0))
        for m in open_ddls:
            if m["due_date"]:
                dated.append((_days_left(m["due_date"]),
                              f"{m['title']} · {_brief(m['project_title'])}",
                              m["due_date"], m["project_id"], m["id"]))
        dated = [x for x in dated if x[0] is not None]
        dated.sort(key=lambda x: x[0])
        nearest = dated[0] if dated else None
        overdue_n = sum(1 for x in dated if x[0] < 0)
        open_total = sum(v["open"] for v in ms_counts.values())
        active = [p for p in projects if p["status"] != "done"]

        # 1) Hero：最紧迫的一件事
        hero = SummaryHero(caption="最紧迫的截止", action="进入课题列表 ›")
        hero.layout().setContentsMargins(12, 12, 12, 12)
        hero.action_btn.clicked.connect(lambda: page.set_view("projects"))
        if nearest:
            left = nearest[0]
            hero.set_value(f"已逾期 {abs(left)} 天" if left < 0
                           else f"{abs(left)} 天后")
            hero.set_caption(nearest[1][:46], nearest[2])
        else:
            hero.set_value("没有排期")
            hero.set_caption("所有课题都没设截稿日期与 DDL")
        wk = focus["week"]
        hero.set_metrics([
            ("在研", str(len(active)), "accent"),
            ("待办 DDL", str(open_total), "blue"),
            ("逾期", str(overdue_n), "red" if overdue_n else "muted"),
            ("本周投入", "—" if not wk else
             (f"{wk / 60:.1f} 小时" if wk >= 60 else f"{int(wk)} 分钟"), "green"),
        ])
        done_ddl = sum(v["done"] for v in ms_counts.values())
        total_ddl = sum(v["total"] for v in ms_counts.values())
        hero.set_progress(done_ddl / total_ddl if total_ddl else 0.0)
        hero.bar.setVisible(total_ddl > 0)
        note = (f"{len(projects)} 个课题 · DDL 完成 {done_ddl}/{total_ddl}"
                if total_ddl else f"{len(projects)} 个课题 · 还没有一条 DDL")
        if overdue_n:
            note += f" · ⚠ {overdue_n} 个截止已过期"
        hero.set_note(note, level="error" if overdue_n else "ok")
        self.lay.addWidget(hero)

        # 2) 科研路线：六个步骤各卡着哪些课题（点一格跳到该步的列表）
        strip = QHBoxLayout()
        strip.setSpacing(9)
        for k in services.ROUTE_KEYS:
            names = [_brief(p["title"]) for p in projects
                     if p.get("status", "s1") == k]
            strip.addWidget(RouteCell(k, names,
                                      lambda key=k: page.pick_stage(key)), 1)
        self.lay.addLayout(strip)

        # 3) 接下来要交的（跨课题 DDL + 截稿日，按日期排）
        items = []
        for left, label, day, rid, mid in dated[:10]:
            tint = "red" if left < 0 else ("amber" if left <= 14 else "")
            items.append(MiniRow(
                "⏰", label, f"{day} · {('逾期 ' + str(-left) + ' 天') if left < 0 else ('剩 ' + str(left) + ' 天')}",
                lambda r=rid, m=mid: page.goto_project(r, m), tint=tint))
        self.lay.addWidget(self._list_card(
            "接下来要交的", "DDL 与截稿日一起排", items,
            "所有课题都没设截稿日期和 DDL"))

        # 4) 投入趋势 | 时间花在哪 —— 没数据时收成一张窄条：两张空图能占掉小半屏，
        #    把真正有用的「接下来要交的」挤出视野。
        weekly = services.research_focus_weekly(6)
        tops = services.research_focus_top_tasks(30, 6)
        has_focus = any(w["minutes"] for w in weekly) or bool(tops)
        gear = _link_btn("⚙ 投入关键词", page.edit_keywords,
                         "设定哪些番茄任务算科研投入")
        if not has_focus:
            card, clay, head = self._card("科研专注投入", "还没统计到")
            head.addWidget(gear)
            hint = QLabel(
                "还没有命中关键词的番茄记录，所以这里统计不出来。用番茄钟记一次"
                "「看论文 / 跑实验 / 写论文」就会长出来；任务名对不上就点右上角"
                "「投入关键词」改成你实际用的词。")
            hint.setWordWrap(True)
            hint.setObjectName("Meta")
            clay.addWidget(hint)
            self.lay.addWidget(card)
        else:
            trend_card, tlay, thead = self._card("科研专注投入", "近 6 周（小时）")
            thead.addWidget(gear)
            chart = widgets.BarChart()
            chart.set_data([round(w["minutes"] / 60, 1) for w in weekly],
                           [w["label"] for w in weekly], "accent")
            chart.setMinimumHeight(168)
            tlay.addWidget(chart)
            row3 = QHBoxLayout()
            row3.setSpacing(12)
            row3.addWidget(trend_card, 1)
            peak = max([t["minutes"] for t in tops] + [1])
            row3.addWidget(self._dist_card("近 30 天时间花在哪", "", [
                (t["task"], "blue", _fmt_minutes(t["minutes"]),
                 t["minutes"] / peak, None) for t in tops]), 1)
            self.lay.addLayout(row3)

        # 5) 课题一览（点卡片跳详情）
        rows = []
        for p in active:
            mc = ms_counts.get(p["id"], {})
            key = _step_of(p)
            rows.append(MiniRow(
                STEP_ICON.get(key, "🔬"),
                f"{STEP_NUM[key]} {_step_label(key)} · {p['title']}",
                " · ".join(x for x in (
                    p.get("venue") or "未定投稿",
                    (f"DDL {mc.get('done', 0)}/{mc.get('total', 0)}"
                     if mc.get("total") else "无 DDL"),
                    (f"{counts.get(p['id'], 0)} 篇论文" if counts.get(p["id"]) else ""),
                ) if x),
                lambda rid=p["id"]: page.goto_project(rid),
                tint=STEP_COLOR.get(key, "")))
        self.lay.addWidget(self._list_card("在研课题一览", "点进详情", rows,
                                           "没有在研课题"))

        self.lay.addStretch(1)
        sb.setValue(pos)


# ---------------------------------------------------------------------------
# 视图：研究课题
# ---------------------------------------------------------------------------
class ProjectsView(QWidget):
    """左：按路线步骤分组的课题列表；右：选中课题的详情面板。"""

    def __init__(self, page: "ResearchPage"):
        super().__init__()
        self.page = page
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        col = QWidget()
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        head = QWidget()
        h = QHBoxLayout(head)
        h.setContentsMargins(20, 12, 20, 6)
        h.setSpacing(8)
        # 步骤筛选入口放在看板的路线条上，这里不再排一排 chip：详情面板占掉
        # 372px 后列表只剩 350px 上下，八个 chip 会被挤到文字都读不出来。
        self.sub_lbl = QLabel("")
        self.sub_lbl.setObjectName("Meta")
        h.addWidget(self.sub_lbl)
        h.addStretch(1)
        self.filter_lbl = QLabel("")
        self.filter_lbl.setObjectName("Meta")
        h.addWidget(self.filter_lbl)
        self.clear_btn = _link_btn("清除筛选", lambda: self.page.pick_stage("all"),
                                   "回到显示全部课题")
        h.addWidget(self.clear_btn)
        v.addWidget(head)

        self.scroll, self.lay = _scroll((14, 6, 14, 16))
        self.lay.setSpacing(9)
        v.addWidget(self.scroll, 1)
        root.addWidget(col, 1)

        self.detail = ProjectDetailPane(page)
        root.addWidget(self.detail)

    def reload(self, projects: list[dict], counts: dict[int, int],
               ms_counts: dict[int, dict]) -> None:
        sb = self.scroll.verticalScrollBar()
        pos = sb.value()
        _clear_layout(self.lay)
        stage = self.page.stage_filter
        shown = [p for p in projects
                 if stage == "all" or p.get("status", "s1") == stage]
        active = [p for p in projects if p["status"] != "done"]
        self.sub_lbl.setText(f"{len(active)} 个在研 · 共 {len(projects)} 个")
        filtered = stage != "all"
        self.filter_lbl.setVisible(filtered)
        self.clear_btn.setVisible(filtered)
        if filtered:
            self.filter_lbl.setText(
                f"只看 {STEP_NUM[stage]} {_step_label(stage)}（{len(shown)} 个）")

        if not shown:
            self.lay.addWidget(_empty_hint(
                f"「{_step_label(stage)}」这一步还没有课题 —— "
                "点上面的「清除筛选」看全部，或点右上角「＋ 新增课题」"))
        else:
            for p in shown:
                card = ProjectCard(self.page, p, counts.get(p["id"], 0),
                                   ms_counts.get(p["id"], {}))
                card.set_selected(p["id"] == self.page.selected_project)
                self.lay.addWidget(card)
        self.lay.addStretch(1)
        sb.setValue(pos)


# ---------------------------------------------------------------------------
# 页面壳
# ---------------------------------------------------------------------------
class ResearchPage(Page):
    def __init__(self):
        super().__init__("科研管理", "科研路线 · 投稿 · DDL", bare=True)
        self.body().setContentsMargins(0, 0, 0, 0)
        self.body().setSpacing(0)

        self._view = "overview"
        self._stage = "all"
        self._selected = 0
        self._locate_mid = 0
        self._reload_pending = False

        shell = QWidget()
        v = QVBoxLayout(shell)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_topbar())

        self.stack = QStackedWidget()
        self.stack.setObjectName("ResearchCanvas")
        self.overview = OverviewView(self)
        self.projects_view = ProjectsView(self)
        self.stack.addWidget(self.overview)
        self.stack.addWidget(self.projects_view)
        v.addWidget(self.stack, 1)
        self.body().addWidget(shell, 1)

        # 页面里有一批「建控件那一刻用 theme.get() 算出来的内联配色」（路线格数字、
        # 逾期 DDL 的红、倒计时大字）。主窗口切主题只 update() 不重建，所以这里
        # 自己接一下，整页重刷一遍才会换成新主题的色值。
        # 注意 changed 是无参信号：写成 lambda _: ... 会 TypeError，信号根本不通。
        theme.manager.changed.connect(self.reload)

        self.reload()

    def _build_topbar(self) -> QWidget:
        """视图切换放顶部，不再开第二条左栏：应用本身已有 214px 侧边栏，"
        页面内再塞一条会把内容挤成四列（980px 最小宽度下列表只剩 250px）。"""
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(20, 14, 20, 6)
        h.setSpacing(8)
        self._view_btns: dict[str, QPushButton] = {}
        for key, icon, name in VIEWS:
            b = QPushButton(f"{icon} {name}")
            b.setObjectName("SegBtn")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _, k=key: self.set_view(k))
            self._view_btns[key] = b
            h.addWidget(b)
        h.addStretch(1)
        self.week_lbl = QLabel("")
        self.week_lbl.setObjectName("Meta")
        h.addWidget(self.week_lbl)
        add = QPushButton("＋ 新增课题")
        add.setObjectName("Primary")
        add.setCursor(Qt.PointingHandCursor)
        add.setStyleSheet("padding: 7px 14px;")
        add.clicked.connect(self.add_project)
        h.addWidget(add)
        self.arxiver_btn = QPushButton("📡 打开 Arxiver")
        self.arxiver_btn.setObjectName("Ghost")
        self.arxiver_btn.setCursor(Qt.PointingHandCursor)
        self.arxiver_btn.setStyleSheet("padding: 7px 12px;")
        self.arxiver_btn.clicked.connect(self.open_arxiver_tool)
        h.addWidget(self.arxiver_btn)
        return bar

    # ---------- 视图与筛选 ----------
    @property
    def stage_filter(self) -> str:
        return self._stage

    @property
    def selected_project(self) -> int:
        return self._selected

    def set_view(self, key: str) -> None:
        self._view = key
        self.stack.setCurrentIndex(0 if key == "overview" else 1)
        self.reload()

    def pick_stage(self, key: str) -> None:
        """点路线 chip 只看这一步的课题；再点一次当前选中的就回到「全部」。"""
        self._stage = "all" if (self._stage == key
                               or key == "all") else key
        self._view = "projects"
        self.stack.setCurrentIndex(1)
        self.reload()

    def set_step(self, rid: int, key: str) -> None:
        if rid:
            services.research_update(rid, status=key)
            sounds.play("research_stage")
        self.reload()

    def goto_project(self, rid: int, mid: int = 0) -> None:
        """从看板跳到某个课题；带 mid 时顺手滚到那条 DDL 并闪一下。

        看板上点一条 DDL，只把课题选出来是不够的 —— 用户要找的就是那一行。
        """
        self._stage = "all"
        self._selected = rid
        self._locate_mid = mid
        self._view = "projects"
        self.stack.setCurrentIndex(1)
        self.reload()

    # ---------- 课题操作 ----------
    def add_project(self) -> None:
        dlg = ProjectDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            rid = services.research_add(**dlg.result_data())
            self._selected = rid
            self._stage = "all"
            self._view = "projects"
            self.stack.setCurrentIndex(1)
            self.reload()

    def edit_project(self, rid: int) -> None:
        data = services.research_get(rid)
        if not data:
            return
        dlg = ProjectDialog(self, data)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            services.research_update(rid, **dlg.result_data())
            self.reload()

    def delete_project(self, rid: int) -> None:
        data = services.research_get(rid)
        if not data:
            return
        ms = services.milestone_list(rid)
        synced = sum(1 for m in ms if m.get("todo_id"))
        extra = []
        if services.research_paper_counts().get(rid):
            extra.append("关联的论文只解除关联，Arxiver 里的论文不动")
        if synced:
            extra.append(f"同步出去的 {synced} 条日历待办会一并删除")
        tail = ("\n" + "；".join(extra) + "。") if extra else ""
        if not popups.confirm(
                self, "删除课题", f"确定删除「{data['title']}」？{tail}",
                ok_text="删除课题"):
            return
        services.research_delete(rid)
        if self._selected == rid:
            self._selected = 0
        self.reload()

    def select_project(self, rid: int) -> None:
        """选中课题：只改卡片高亮 + 回填详情，不整页重建。

        重建会把当前正在处理点击的那张卡片一起销毁（它就在这次重建的容器里）。
        """
        self._selected = rid
        for card in self.projects_view.findChildren(ProjectCard):
            card.set_selected(card.rid == rid)
        self.projects_view.detail.show_project(services.research_get(rid) or None)

    # ---------- DDL 操作 ----------
    def add_milestone(self) -> None:
        if not self._selected:
            popups.notify(self, "先选一个课题",
                          "在课题列表里点中一个课题，再往它下面加内容。")
            return
        dlg = MilestoneAddDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            title, due, note, sync = dlg.values()
            if title:
                services.milestone_add(self._selected, title, due, note, sync)
                self.reload()

    def edit_milestone(self, mid: int) -> None:
        """改 DDL 的名称/日期/备注/同步开关。以前只能删了重加，改期根本没法弄。"""
        ms = services.milestone_get(mid)
        if not ms:
            return
        dlg = MilestoneAddDialog(self, ms)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        title, due, note, sync = dlg.values()
        if not title:
            return
        services.milestone_update(mid, title=title, due_date=due, note=note)
        if sync:
            services.sync_milestone_todo(mid)
        else:
            services.unsync_milestone_todo(mid)
        self.reload()

    def toggle_milestone(self, mid: int, done: bool) -> None:
        services.milestone_update(mid, done=int(done))
        if done:
            sounds.play("milestone_done")
        ms = services.milestone_get(mid)
        if ms and ms.get("todo_id"):
            services.todo_update(ms["todo_id"], done=int(done))
        self.reload()

    def sync_milestone(self, mid: int) -> None:
        ms = services.milestone_get(mid)
        if not ms:
            return
        if not ms["due_date"]:
            popups.notify(
                self, "需要日期",
                "这条 DDL 还没填截止日期 —— 日历上没法摆一条没有日期的事。")
            return
        services.sync_milestone_todo(mid)
        self.reload()
        self._toast("已同步到日历（清单：科研DDL）")

    def unsync_milestone(self, mid: int) -> None:
        services.unsync_milestone_todo(mid)
        self.reload()

    def delete_milestone(self, mid: int) -> None:
        ms = services.milestone_get(mid)
        if not ms:
            return
        # 只在有「看不见的连带后果」时才拦一下：删一条没同步过的 DDL 就是删一行字，
        # 每次都弹确认太烦；但它如果已经同步成待办，删了会连日历里那条一起消失。
        if ms.get("todo_id"):
            if not popups.confirm(
                    self, "删除 DDL",
                    f"确定删除「{ms['title']}」？\n"
                    "这条已经同步到日历，日历里对应的那条待办会一起删掉。",
                    ok_text="删除"):
                return
            services.milestone_delete(mid)
            self.reload()
            self._toast("已删除（日历里那条待办也一并去掉了）")
            return
        services.milestone_delete(mid)
        self.reload()
        self._toast("已删除这条 DDL")

    # ---------- Arxiver ----------
    def open_arxiver_tool(self) -> None:
        """跳到侧边栏的 Arxiver 工具页；找不到就退回打开它的库目录。"""
        win = self.window()
        opener = getattr(win, "open_tool", None)
        if callable(opener) and opener("arxiver"):
            return
        path = os.path.dirname(services.arxiver_db_path())
        if os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            return
        popups.notify(self, "没找到 Arxiver", "没有在侧边栏里找到 Arxiver 工具页。",
                      danger=True)

    def open_arxiv_paper(self, paper: dict) -> None:
        target = _paper_open_target(paper)
        if not target:
            popups.notify(self, "打不开", "这篇论文既没有本地 PDF 也没有链接。", danger=True)
            return
        kind, loc = target
        if kind == "file":
            QDesktopServices.openUrl(QUrl.fromLocalFile(loc))
        else:
            QDesktopServices.openUrl(QUrl(loc))

    def pick_papers(self) -> None:
        if not self._selected:
            popups.notify(self, "先选一个课题",
                          "在课题列表里点中一个课题，再往它下面加内容。")
            return
        if not services.arxiver_available():
            popups.notify(
                self, "没找到 Arxiver 论文库",
                f"预期路径：\n{services.arxiver_db_path()}\n\n"
                "在 Arxiver 里抓过论文后再来关联。", danger=True)
            return
        dlg = ArxivPickerDialog(self, self._selected)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            picked = dlg.selected()
            if not picked:
                return
            for p in picked:
                services.research_paper_add(self._selected, p.get("arxiv_id", ""),
                                            p.get("title", ""))
            self.reload()
            self._toast(f"已关联 {len(picked)} 篇到本课题")

    def unlink_paper(self, link_id: int) -> None:
        services.research_paper_delete(link_id)
        self.reload()

    def edit_keywords(self) -> None:
        dlg = KeywordsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            services.set_research_keywords(_split_kw(dlg.keywords_text()))
            self.reload()

    # ---------- 轻提示 ----------
    def _toast(self, text: str) -> None:
        """右下角瞬时反馈：复制、同步这类操作不值得弹模态框。"""
        lbl = getattr(self, "_toast_lbl", None)
        if lbl is None:
            lbl = QLabel(self)
            self._toast_lbl = lbl
        lbl.setStyleSheet(
            f"background: {theme.get('surface_hi')};"
            f"color: {theme.get('text')};"
            f"border: 1px solid {theme.get('border')};"
            "border-radius: 10px; padding: 6px 12px; font-size: 12px;")
        lbl.setText(text)
        lbl.adjustSize()
        lbl.move(max(12, self.width() - lbl.width() - 24),
                 max(12, self.height() - lbl.height() - 24))
        lbl.show()
        lbl.raise_()
        QTimer.singleShot(1800, lbl.hide)

    # ---------- 总刷新 ----------
    def reload(self) -> None:
        """重建合并到下一个事件循环轮次。

        点路线 chip / 课题卡 / 看板浓缩行时，处理事件的那个控件本身就在这次要
        重建的容器里：同步重建会在事件处理函数返回前把它销毁。延后一轮既避开
        自毁，也把一次操作触发的多次刷新合并成一次。
        """
        if self._reload_pending:
            return
        self._reload_pending = True
        QTimer.singleShot(0, self._do_reload)

    def _do_reload(self) -> None:
        self._reload_pending = False
        projects = services.research_list()
        counts = services.research_paper_counts()
        ms_counts = services.milestone_counts()

        for key, btn in self._view_btns.items():
            btn.setChecked(key == self._view)
        if not self._selected and projects:
            visible = [p for p in projects if self._stage == "all"
                       or p.get("status", "s1") == self._stage]
            if visible:
                self._selected = visible[0]["id"]
        week = services.research_focus_summary()["week"]
        self.week_lbl.setText(f"本周科研投入 {_fmt_minutes(week)}" if week else "")

        self.projects_view.reload(projects, counts, ms_counts)
        self.overview.reload()
        if self._selected:
            self.projects_view.detail.show_project(
                services.research_get(self._selected) or None)
        else:
            self.projects_view.detail.show_project(None)
        if self._locate_mid:
            mid, self._locate_mid = self._locate_mid, 0
            self.projects_view.detail.locate_milestone(mid)
        self.arxiver_btn.setText(
            "📡 打开 Arxiver" if services.arxiver_available() else "📡 Arxiver 未检测到")
