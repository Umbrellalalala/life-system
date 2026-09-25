"""科研管理：一个课题一行，只回答「现在该做什么、什么时候交」。

日常只看到收起态的三行 —— 课题名、投稿与倒计时、**下一步**。点「更多」才展开
六步科研路线（services.ROUTE_STEPS：广泛阅读 → 定缺口与任务 → baseline 评测 →
数据 pipeline → 模型创新 → 写作与投稿）、全部 DDL、角色优先级、关联论文、投入
关键词和进展记录。

带日期的 DDL 可一键同步成一条待办 —— 本应用的日历页就是「带日期的待办」的时间轴
视图，所以同步之后日历上直接能看到。

论文库不在这里重复建设：元数据与 PDF 归档由 Arxiver 负责，本模块对
``~/.arxiver/library.db`` 全程 mode=ro 只读，只存 arxiv_id + 标题快照。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QDate, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QPlainTextEdit, QScrollArea, QFrame, QDialog, QListWidget, QListWidgetItem,
    QCheckBox, QAbstractItemView,
)

from .. import popups, services, sounds, theme, widgets
from .base import Page
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
    # 行内小按钮永远不该是回车目标：留在对话框里它会以 autoDefault 身份抢走默认按钮
    b.setAutoDefault(False)
    b.setDefault(False)
    b.setCursor(Qt.PointingHandCursor)
    if tip:
        b.setToolTip(tip)
    # clicked 带 checked 布尔：直接连过去，PySide 会把它当成回调的第一个位置参数
    # 塞进去 —— lambda k=默认值 的 k 就被 False 覆盖（曾经把 status 写成 "0"），
    # 无默认参数的绑定方法则直接 TypeError。所以固定吃掉这个参数。
    b.clicked.connect(lambda _checked=False: on_click())
    return b

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
        ok.setDefault(True)      # 回车=提交；不设的话六个弹窗按回车都没反应
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
        ok.setDefault(True)      # 回车=提交；不设的话六个弹窗按回车都没反应
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
        ok.setDefault(True)      # 回车=提交；不设的话六个弹窗按回车都没反应
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
        ok.setDefault(True)      # 回车=提交；不设的话六个弹窗按回车都没反应
        ok.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)

    def keywords_text(self) -> str:
        return self.input.text().strip()

# ---------------------------------------------------------------------------
# 课题卡 / DDL 行 / 关联论文行
# ---------------------------------------------------------------------------
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
# 课题卡：收起时只回答「现在该做什么、什么时候交」
# ---------------------------------------------------------------------------
class NextStepCard(QFrame):
    """一个课题一张卡。

    收起态只给三行：课题名、投稿与倒计时、**下一步是什么**。六步路线、全部 DDL、
    角色优先级、关联论文、投入关键词、进展记录都收在「更多」里 —— 日常不需要看。

    卡片是常驻的：ResearchPage 只原地 sync()，不重建。重建会把展开态里正在敲的字
    连同 800ms 防抖一起销毁，那是这个页面以前最难用的地方之一。
    """

    def __init__(self, page: "ResearchPage", rid: int):
        super().__init__(page)
        self.page = page
        self.rid = rid
        self._open = False
        self._loading = False
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 13, 14, 13)
        lay.setSpacing(7)

        # ---- 行 1：标题 + 身份 ----
        top = QHBoxLayout()
        top.setSpacing(8)
        self.badge = EmojiBadge("🔬", "accent", 26)
        top.addWidget(self.badge, 0, Qt.AlignVCenter)
        self.title_lbl = widgets.ElidedLabel("")
        self.title_lbl.setObjectName("TileLabel")
        self.title_lbl.setStyleSheet("font-size: 14px; font-weight: 700;")
        top.addWidget(self.title_lbl, 1)
        self.role_tag = widgets.Tag("待定", "muted")
        self.role_tag.hide()
        top.addWidget(self.role_tag, 0, Qt.AlignVCenter)
        self.prio_tag = widgets.Tag("优先级·中", "amber")
        self.prio_tag.hide()
        top.addWidget(self.prio_tag, 0, Qt.AlignVCenter)
        self.more_btn = _link_btn("更多 ▾", lambda: self.page.toggle_expand(self.rid))
        top.addWidget(self.more_btn, 0, Qt.AlignVCenter)
        lay.addLayout(top)

        # ---- 行 2：投稿与截稿 ----
        self.venue_row = QHBoxLayout()
        self.venue_row.setSpacing(6)
        self.venue_row.addWidget(
            widgets.ElidedLabel("还没填投稿目标（点「更多」补）"), 1)
        lay.addLayout(self.venue_row)

        # ---- 行 3：下一步（这行的字最大，是整页的主角）----
        nxt_row = QHBoxLayout()
        nxt_row.setSpacing(8)
        col = QVBoxLayout()
        col.setSpacing(1)
        cap = QLabel("下一步")
        cap.setObjectName("Meta")
        col.addWidget(cap)
        self.next_lbl = widgets.ElidedLabel("")
        self.next_lbl.setStyleSheet("font-size: 15px; font-weight: 700;")
        col.addWidget(self.next_lbl)
        self.next_sub = widgets.ElidedLabel("")
        self.next_sub.setObjectName("Meta")
        col.addWidget(self.next_sub)
        nxt_row.addLayout(col, 1)
        self.done_btn = QPushButton("做完了")
        self.done_btn.setObjectName("Primary")
        self.done_btn.setCursor(Qt.PointingHandCursor)
        self.done_btn.setStyleSheet("padding: 7px 13px;")
        self.done_btn.clicked.connect(self.page.complete_next)
        self.done_btn.setToolTip("勾掉当前这条下一步，自动露出后面那条")
        nxt_row.addWidget(self.done_btn, 0, Qt.AlignVCenter)
        # 勾完紧接着就是「下一条是什么」，别让这个最高频的动作要先展开「更多」
        self.quick_add = QPushButton("＋ 加一条")
        self.quick_add.setObjectName("Ghost")
        self.quick_add.setCursor(Qt.PointingHandCursor)
        self.quick_add.setStyleSheet("padding: 7px 12px;")
        self.quick_add.clicked.connect(lambda: self.page.add_milestone(self.rid))
        self.quick_add.setToolTip("给这个课题加一条 DDL（实验 / 消融 / 开会…）")
        nxt_row.addWidget(self.quick_add, 0, Qt.AlignVCenter)
        lay.addLayout(nxt_row)

        # ---- 展开区 ----
        self.body = QWidget()
        self.body.hide()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 6, 0, 0)
        bl.setSpacing(9)
        lay.addWidget(self.body)

        bl.addWidget(self._sep())

        # 投稿目标（可编辑）
        vrow = QHBoxLayout()
        vrow.setSpacing(8)
        vrow.addWidget(_field("投稿目标"))
        vrow.addStretch(1)
        self.edit_btn = _link_btn("改标题/备注全文", lambda: self.page.edit_project(self.rid))
        vrow.addWidget(self.edit_btn)
        bl.addLayout(vrow)
        frow = QHBoxLayout()
        frow.setSpacing(8)
        self.venue_edit = QLineEdit()
        self.venue_edit.setPlaceholderText("CVPR 2027 / NeurIPS 2027 / 期刊名")
        self.venue_edit.textChanged.connect(lambda *_: self._schedule_save())
        frow.addWidget(self.venue_edit, 1)
        self.venue_due = widgets.DateInput()
        self.venue_due.setPlaceholderText("截稿日期未定")
        self.venue_due.setFixedWidth(150)
        self.venue_due.textChanged.connect(lambda *_: self._schedule_save())
        frow.addWidget(self.venue_due)
        bl.addLayout(frow)

        # 六步路线
        bl.addWidget(_field("科研路线（点一下切到现在这步）"))
        self._step_btns: dict[str, QPushButton] = {}
        grid = QHBoxLayout()
        grid.setSpacing(5)
        keys = services.ROUTE_KEYS
        for group in (keys[:4], keys[4:]):
            gcol = QVBoxLayout()
            gcol.setSpacing(5)
            for k in group:
                b = QPushButton(f"{STEP_NUM[k]} {_step_label(k)}")
                b.setObjectName("TagChip")
                b.setCheckable(True)
                b.setCursor(Qt.PointingHandCursor)
                widgets._apply_property(b, "tagColor", STEP_COLOR[k])
                b.setToolTip(_step_desc(k))
                b.clicked.connect(lambda _c=False, key=k: self.page.set_step(self.rid, key))
                self._step_btns[k] = b
                gcol.addWidget(b)
            grid.addLayout(gcol, 1)
        bl.addLayout(grid)
        self.step_desc = QLabel("")
        self.step_desc.setObjectName("Meta")
        self.step_desc.setWordWrap(True)
        bl.addWidget(self.step_desc)

        # DDL 清单
        ddl_head = QHBoxLayout()
        ddl_head.setSpacing(6)
        ddl_head.addWidget(_field("自定义 DDL"))
        self.ddl_count = QLabel("")
        self.ddl_count.setObjectName("Meta")
        ddl_head.addWidget(self.ddl_count)
        ddl_head.addStretch(1)
        ddl_head.addWidget(_link_btn("＋ 加一条", self.page.add_milestone,
                                     "如：跑完消融 / baseline 评测 / 开组会"))
        bl.addLayout(ddl_head)
        self.ddl_holder = QVBoxLayout()
        self.ddl_holder.setSpacing(2)
        bl.addLayout(self.ddl_holder)

        # 角色 / 优先级
        crow = QHBoxLayout()
        crow.setSpacing(8)
        c1 = QVBoxLayout()
        c1.setSpacing(4)
        c1.addWidget(_field("我的角色"))
        self.role_combo = widgets.ComboBox()
        self.role_combo.addItems(ROLES)
        self.role_combo.currentTextChanged.connect(self._set_role)
        c1.addWidget(self.role_combo)
        crow.addLayout(c1, 1)
        c2 = QVBoxLayout()
        c2.setSpacing(4)
        c2.addWidget(_field("优先级"))
        self.priority_combo = widgets.ComboBox()
        for i in (0, 1, 2):
            self.priority_combo.addItem(PRIORITY_META[i][0], i)
        self.priority_combo.currentIndexChanged.connect(self._set_priority)
        c2.addWidget(self.priority_combo)
        crow.addLayout(c2, 1)
        bl.addLayout(crow)

        # 进展
        n_head = QHBoxLayout()
        n_head.setSpacing(6)
        n_head.addWidget(_field("进展记录"))
        n_head.addStretch(1)
        self.focus_lbl = QLabel("")
        self.focus_lbl.setObjectName("Meta")
        n_head.addWidget(self.focus_lbl)
        n_head.addWidget(_link_btn("＋ 记一笔", self.page.log_progress,
                                   "按今天日期追加一行，写周报时直接抄"))
        bl.addLayout(n_head)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("研究问题、技术路线、本周进展、现在卡在哪…")
        # QPlainTextEdit 竖向默认 Expanding，不给上限会把展开区拉出一大片空白
        self.notes_edit.setFixedHeight(120)
        self.notes_edit.textChanged.connect(lambda *_: self._schedule_save())
        bl.addWidget(self.notes_edit)

        # 论文
        p_head = QHBoxLayout()
        p_head.setSpacing(6)
        p_head.addWidget(_field("关联论文（Arxiver）"))
        p_head.addStretch(1)
        p_head.addWidget(_link_btn("＋ 挑选", self.page.pick_papers,
                                   "从 Arxiver 论文库里搜索并关联"))
        p_head.addWidget(_link_btn("打开 Arxiver ›", self.page.open_arxiver_tool))
        bl.addLayout(p_head)
        self.paper_holder = QVBoxLayout()
        self.paper_holder.setSpacing(2)
        bl.addLayout(self.paper_holder)

        # 关键词 + 删除
        bl.addWidget(_field("投入关键词（逗号分隔）"))
        self.kw_edit = QLineEdit()
        self.kw_edit.setPlaceholderText("哪些番茄任务算这个课题的投入")
        self.kw_edit.textChanged.connect(lambda *_: self._schedule_save())
        bl.addWidget(self.kw_edit)

        d_row = QHBoxLayout()
        d_row.addStretch(1)
        self.del_btn = _link_btn("删除这个课题", lambda: self.page.delete_project(self.rid))
        self.del_btn.setToolTip("连带删掉它的 DDL 与同步出去的日历待办")
        d_row.addWidget(self.del_btn)
        bl.addLayout(d_row)

        # 输入即存：不能只挂 editingFinished —— 用户敲完常是点面板空白处
        # （QLabel 不可聚焦，焦点根本没离开输入框），那时它不触发，改动就丢了。
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(800)
        self._timer.timeout.connect(self._flush_pending)

    @staticmethod
    def _sep() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"border: none; border-top: 1px solid {theme.get('border')};")
        return line

    # ---------- 展开 / 收起 ----------
    def set_open(self, on: bool) -> None:
        self._open = on
        self.body.setVisible(on)
        self.more_btn.setText("收起 ▴" if on else "更多 ▾")
        # 收起态也要回填：那三行（标题 / 投稿 / 下一步）就是整页的主角
        self.sync()

    # ---------- 回填（原地，不重建） ----------
    def sync(self) -> None:
        # 先把自己没保存的字落盘。只挡 hasFocus() 不够：点步骤 chip、按 Tab 都会让
        # 输入框失焦，而 800ms 防抖可能还没到点，下面 setText 会把库里旧值盖回输入框。
        if self._timer.isActive():
            self._timer.stop()
            self._flush_pending(resync=False)
        proj = services.research_get(self.rid)
        if not proj:
            return
        self._loading = True
        key = _step_of(proj)
        color = STEP_COLOR.get(key, "accent")
        self.badge.set_icon(STEP_ICON.get(key, "🔬"), color)
        # 危险操作要和普通链接按钮区分开；颜色在回填时定，切主题才会跟着换
        self.del_btn.setStyleSheet(f"color: {theme.get('red')};")
        self.title_lbl.setText(proj["title"])

        role = proj.get("role") or ""
        self.role_tag.setVisible(bool(role))
        if role:
            self.role_tag.setText(role)
            widgets._apply_property(self.role_tag, "tagColor",
                                    ROLE_COLOR.get(role, "muted"))
        prio = int(proj.get("priority", 1) or 0)
        self.prio_tag.setVisible(prio != 1)
        self.prio_tag.setText(f"优先级·{PRIORITY_META[prio][0]}")
        widgets._apply_property(self.prio_tag, "tagColor", PRIORITY_META[prio][1])

        open_n = sum(1 for m in services.milestone_list(self.rid) if not m["done"])
        self._fill_venue_row(proj, open_n)
        self._fill_next(proj)

        if self._open:
            if not self.venue_edit.hasFocus():
                self.venue_edit.setText(proj.get("venue") or "")
            vd = proj.get("venue_deadline") or ""
            if not self.venue_due.hasFocus():
                if vd:
                    self.venue_due.setDate(QDate.fromString(vd, "yyyy-MM-dd"))
                else:
                    self.venue_due.setText("")
            for k, b in self._step_btns.items():
                b.setChecked(k == key)
            self.step_desc.setText(_step_desc(key))
            self.role_combo.setCurrentText(role if role in ROLES else "待定")
            self.priority_combo.setCurrentIndex(prio)
            if not self.kw_edit.hasFocus():
                self.kw_edit.setText(proj.get("focus_keywords") or "")
            if not self.notes_edit.hasFocus():
                self.notes_edit.setPlainText(proj.get("notes") or "")
            minutes = services.project_focus_minutes(proj)
            self.focus_lbl.setText(f"近 30 天 {_fmt_minutes(minutes)}" if minutes else "")
        # DDL 行不受「展开」条件保护：它们身上有 theme.get() 算出来的内联色
        # （逾期的红），收起时不重建就会在切主题后留着旧色值，展开那一瞬先闪错的。
        self._fill_ddls()
        # 论文行反过来要挡：它每行都要去 Arxiver 的库里现查元数据（上千篇的库，
        # 一次十几毫秒），而收起态根本不显示论文 —— 三张卡白查三遍。
        if self._open:
            self._fill_papers()
        self._loading = False

    def _fill_venue_row(self, proj: dict, open_n: int) -> None:
        _clear_layout(self.venue_row)
        venue = (proj.get("venue") or "").strip()
        cd = _countdown(proj.get("venue_deadline") or "")
        if venue:
            self.venue_row.addWidget(widgets.Tag(f"🎯 {venue}", "accent"))
        else:
            self.venue_row.addWidget(widgets.Tag("未定投稿目标", "muted"))
        if cd:
            self.venue_row.addWidget(widgets.Tag(f"⏳ {cd[0]}", cd[1]))
        # 一眼看出这行下面还有几条，勾完知不知道有东西顶上来
        if open_n > 1:
            self.venue_row.addWidget(widgets.Tag(f"还有 {open_n - 1} 条", "muted"))
        self.venue_row.addStretch(1)

    def _fill_next(self, proj: dict) -> None:
        """下一步 = 最早那条没做完的 DDL；没有 DDL 就说这一步该干什么。"""
        if proj.get("status") == "done":
            # 结题的排在最后，但也要一眼看出它是「不用管了」而不是「还欠着事」
            self.next_lbl.setText("已收尾，不再推进")
            self.next_sub.setText("点「更多」可以改回在研，或直接删掉")
            self.next_lbl.setStyleSheet(
                f"font-size: 15px; font-weight: 700; color: {theme.get('muted')};")
            self.badge.set_icon("🏁", "muted")
            self.done_btn.setEnabled(False)
            self.done_btn.setToolTip("这个课题已经收尾了")
            return
        nxt = None
        for m in services.milestone_list(self.rid):
            if m["done"]:
                continue
            cd = _countdown(m["due_date"])
            nxt = (m, cd)
            break
        if nxt:
            m, cd = nxt
            self.next_lbl.setText(m["title"])
            sub = f"{m['due_date']} · {cd[0]}" if cd else "没填日期 · 只做记录不进日历"
            if m.get("note"):
                sub += f" · {m['note']}"
            self.next_sub.setText(sub)
            tint = cd[1] if cd else "muted"
            self.next_lbl.setStyleSheet(
                f"font-size: 15px; font-weight: 700; color: {theme.get(tint)};")
            self.badge.set_icon(STEP_ICON.get(_step_of(proj), "🔬"),
                                "red" if cd and cd[1] == "red"
                                else STEP_COLOR.get(_step_of(proj), "accent"))
            self.done_btn.setEnabled(True)
            self.done_btn.setToolTip("勾掉这条，自动露出后面那条")
        else:
            key = _step_of(proj)
            open_ddl = services.milestone_list(self.rid)
            if open_ddl:
                self.next_lbl.setText("剩下的都勾完了")
                self.next_sub.setText("点「更多」加下一条，或把路线推到下一步")
            else:
                self.next_lbl.setText(f"{STEP_NUM[key]} {_step_label(key)}")
                self.next_sub.setText(_step_desc(key))
            self.next_lbl.setStyleSheet(
                f"font-size: 15px; font-weight: 700; color: {theme.get('muted')};")
            self.done_btn.setEnabled(False)
            self.done_btn.setToolTip("还没有待办的 DDL，先点「更多 → ＋ 加一条」")

    def _fill_ddls(self) -> None:
        _clear_layout(self.ddl_holder)
        mss = services.milestone_list(self.rid)
        done = sum(1 for m in mss if m["done"])
        self.ddl_count.setText(f"{done}/{len(mss)} 完成" if mss else "还没有")
        if not mss:
            self.ddl_holder.addWidget(_empty_hint(
                "点「＋ 加一条」记下实验 / 消融 / baseline / 开组会这些节点"))
        for m in mss:
            self.ddl_holder.addWidget(MilestoneRow(self.page, m))

    def _fill_papers(self) -> None:
        _clear_layout(self.paper_holder)
        papers = services.research_paper_list(self.rid)
        if not papers:
            self.paper_holder.addWidget(_empty_hint(
                "还没关联论文" if services.arxiver_available()
                else "未检测到 Arxiver 论文库"))
        for p in papers:
            self.paper_holder.addWidget(LinkedPaperRow(self.page, p))

    # ---------- 写回（只写这张卡自己的字段） ----------
    def _schedule_save(self) -> None:
        if self._loading:
            return
        self._timer.start()

    def _flush_pending(self, resync: bool = True) -> None:
        """把输入框里的内容写库。resync=False 给 sync() 用，避免回填时递归。"""
        if not self.rid:
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
        services.research_update(self.rid, **fields)
        if resync:
            self.page.sync_cards()

    def _set_role(self, text: str) -> None:
        if self._loading or not self.rid:
            return
        services.research_update(self.rid, role=text)
        self.page.sync_cards()

    def _set_priority(self) -> None:
        if self._loading or not self.rid:
            return
        services.research_update(self.rid, priority=self.priority_combo.currentIndex())
        self.page.sync_cards()

    def pending_edit(self) -> bool:
        """防抖还没到点 —— 整页重建前要先把它冲下去。"""
        return self._timer.isActive()


# ---------------------------------------------------------------------------
# 页面：一屏到底的「下一步」清单
# ---------------------------------------------------------------------------
class ResearchPage(Page):
    """科研管理：一个课题一行，只回答「现在该做什么、什么时候交」。

    六步科研路线（services.ROUTE_STEPS）仍然是课题的分组方式，但它退到了
    「更多」里面 —— 之前这里同时摆了看板、路线条、课题列表、右侧详情栏，
    同一批课题在四个地方出现，反而没人知道该在哪动手。
    """

    def __init__(self):
        super().__init__("科研管理",
                         "每个课题一行，按最近要交的排：下一步做什么、什么时候交",
                         scrollable=True)
        self._cards: dict[int, NextStepCard] = {}
        self._open_rid = 0
        self._locate_mid = 0
        self._reload_pending = False

        lay = self.body()
        self.summary = QLabel("")
        self.summary.setObjectName("PageSubtitle")
        lay.addWidget(self.summary)
        self.list_holder = QVBoxLayout()
        self.list_holder.setSpacing(10)
        lay.addLayout(self.list_holder)

        add = QPushButton("＋ 新增课题")
        add.setObjectName("Ghost")
        add.setCursor(Qt.PointingHandCursor)
        add.setStyleSheet(
            f"padding: 12px; color: {theme.get('accent')}; "
            f"border-style: dashed; border-width: 1px; "
            f"border-color: {theme.get('border')}; border-radius: 10px;")
        add.clicked.connect(self.add_project)
        lay.addWidget(add)
        self.add_btn = add
        lay.addStretch(1)

        # 页面里有一批「建控件那一刻用 theme.get() 算出来的内联配色」，主窗口切主题
        # 只 update() 不重建，所以自己接一下。changed 是无参信号，别写成 lambda _: 。
        theme.manager.changed.connect(self._on_theme)

        self.reload()

    def _on_theme(self) -> None:
        self.add_btn.setStyleSheet(
            f"padding: 12px; color: {theme.get('accent')}; "
            f"border-style: dashed; border-width: 1px; "
            f"border-color: {theme.get('border')}; border-radius: 10px;")
        self.reload()

    # ---------- 刷新 ----------
    def reload(self) -> None:
        """合并到下一个事件循环轮次，并且只原地 sync，不重建卡片。

        重建会把展开态里正在敲的字和没到点的防抖一起销毁 —— 那是「字莫名其妙没了」
        的来源。这里只在课题增减时增删卡片，其余情况一律 sync_cards()。
        """
        if self._reload_pending:
            return
        self._reload_pending = True
        QTimer.singleShot(0, self._do_reload)

    def sync_cards(self) -> None:
        for card in self._cards.values():
            card.sync()
        self._fill_summary()

    def _do_reload(self) -> None:
        self._reload_pending = False
        for card in self._cards.values():
            if card.pending_edit():
                card._timer.stop()
                card._flush_pending()
        projects = self._by_urgency(services.research_list())
        ids = [p["id"] for p in projects]

        for rid in [r for r in self._cards if r not in ids]:
            card = self._cards.pop(rid)
            self.list_holder.removeWidget(card)
            card.hide()
            card.deleteLater()
        for pos, rid in enumerate(ids):
            card = self._cards.get(rid)
            if card is None:
                card = NextStepCard(self, rid)
                self._cards[rid] = card
                self.list_holder.insertWidget(pos, card)
            else:
                self.list_holder.insertWidget(pos, card)   # 按紧迫度重排
            card.set_open(rid == self._open_rid)
        if not ids:
            self.list_holder.addWidget(self._empty_state())
        self._fill_summary()
        if self._locate_mid:
            mid, self._locate_mid = self._locate_mid, 0
            self._locate_milestone(mid)

    @staticmethod
    def _by_urgency(projects: list[dict]) -> list[dict]:
        """按「最近要交的」排，不是按优先级。

        这页回答的是「这周该干什么」，所以 5 天后要开组会的 EditCalib 应该排在
        优先级最高但还早的 Agent+ 前面；优先级只用来打破平局。
        """
        def key(p):
            days = [_days_left(p.get("venue_deadline") or "")]
            days += [_days_left(m["due_date"]) for m in services.milestone_list(p["id"])
                     if not m["done"]]
            soon = [d for d in days if d is not None]
            # 第一位必须是「结没结题」：已收尾的课题往往留着过去的截稿日，
            # 只按天数排会让它顶着「逾期 N 天」冲到最上面。
            return (1 if p.get("status") == "done" else 0,
                    9e9 if not soon else min(soon),
                    -int(p.get("priority", 1) or 0), p["id"])
        return sorted(projects, key=key)

    def _fill_summary(self) -> None:
        projects = [p for p in services.research_list() if p["status"] != "done"]
        if not projects:
            self.summary.setText("还没有课题 —— 用下面那个按钮立一个，"
                                 "填上投哪儿、什么时候交就行。")
            return
        dated = []
        for p in projects:
            left = _days_left(p.get("venue_deadline") or "")
            if left is not None:
                dated.append((left, f"{_brief(p['title'])} 截稿"))
        for m in services.milestone_open_all(60):
            left = _days_left(m["due_date"])
            if left is not None:
                dated.append((left, m["title"]))
        dated.sort()
        week = services.research_focus_summary()["week"]
        bits = [f"{len(projects)} 个在研"]
        if dated:
            left, what = dated[0]
            when = f"已逾期 {-left} 天" if left < 0 else (
                "就是今天" if left == 0 else f"{left} 天后")
            bits.append(f"最紧的是{when}：{what}")
        else:
            bits.append("还没有任何截止日期")
        over = sum(1 for left, _ in dated if left < 0)
        if over:
            bits.append(f"⚠ {over} 条已逾期")
        if week:
            bits.append(f"本周投入 {_fmt_minutes(week)}")
        self.summary.setText(" · ".join(bits))

    def _empty_state(self) -> QWidget:
        card = widgets.Card()
        t = QLabel("先立一个课题")
        t.setObjectName("CardTitle")
        card.body().addWidget(t)
        tip = QLabel(
            "填三样就够开始：课题叫什么、打算投哪儿（CVPR / NeurIPS / 期刊）、"
            "什么时候交。之后每做一步就加一条 DDL，这一行会自己告诉你下一步是什么。")
        tip.setWordWrap(True)
        tip.setObjectName("Meta")
        card.body().addWidget(tip)
        row = QHBoxLayout()
        row.setSpacing(8)
        b1 = QPushButton("＋ 新增课题")
        b1.setObjectName("Primary")
        b1.setCursor(Qt.PointingHandCursor)
        b1.clicked.connect(self.add_project)
        row.addWidget(b1)
        if services.arxiver_available():
            b2 = QPushButton("打开 Arxiver 看论文 ›")
            b2.setObjectName("Ghost")
            b2.setCursor(Qt.PointingHandCursor)
            b2.clicked.connect(self.open_arxiver_tool)
            row.addWidget(b2)
        row.addStretch(1)
        card.body().addLayout(row)
        return card

    def _locate_milestone(self, mid: int) -> None:
        for ms in services.milestone_open_all(80):
            if ms["id"] == mid:
                self._open_rid = ms["project_id"]
                self.reload()
                return
        self._open_rid = 0

    # ---------- 展开 ----------
    def toggle_expand(self, rid: int) -> None:
        self._open_rid = 0 if self._open_rid == rid else rid
        for cid, card in self._cards.items():
            card.set_open(cid == self._open_rid)

    # ---------- 课题操作 ----------
    def add_project(self) -> None:
        dlg = ProjectDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            rid = services.research_add(**dlg.result_data())
            self._open_rid = rid
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
        if not popups.confirm(self, "删除课题",
                              f"确定删除「{data['title']}」？{tail}", ok_text="删除课题"):
            return
        services.research_delete(rid)
        if self._open_rid == rid:
            self._open_rid = 0
        self.reload()

    def set_step(self, rid: int, key: str) -> None:
        if rid:
            services.research_update(rid, status=key)
            sounds.play("research_stage")
        self.sync_cards()

    def log_progress(self, rid: int | None = None) -> None:
        """按今天日期往进展记录里追加一行（写周报时能直接抄）。"""
        rid = rid or self._open_rid
        card = self._cards.get(rid)
        if card is None:
            return
        text, ok = popups.ask_text(self, "记一笔进展",
                                   f"{QDate.currentDate().toString('MM-dd')} · 做了什么")
        if not ok or not text.strip():
            return
        old = card.notes_edit.toPlainText().rstrip()
        line = f"[{QDate.currentDate().toString('MM-dd')}] {text.strip()}"
        card.notes_edit.setPlainText(f"{old}\n{line}".strip())
        card._timer.stop()
        card._flush_pending()
        self._toast("已记一笔")

    # ---------- DDL 操作 ----------
    def _target_rid(self) -> int:
        return self._open_rid

    def add_milestone(self, rid: int | None = None) -> None:
        rid = rid or self._target_rid()
        if not rid:
            popups.notify(self, "先展开一个课题",
                          "点课题右下角的「更多」，再往它下面加 DDL。")
            return
        dlg = MilestoneAddDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            title, due, note, sync = dlg.values()
            if title:
                services.milestone_add(rid, title, due, note, sync)
                self.sync_cards()

    def complete_next(self, _checked=False) -> None:
        """勾掉当前显示的这条下一步。"""
        sender = self.sender()
        rid = 0
        for cid, card in self._cards.items():
            if sender is card.done_btn:
                rid = cid
                break
        if not rid:
            return
        for m in services.milestone_list(rid):
            if not m["done"]:
                self.toggle_milestone(m["id"], True)
                self._toast(f"已完成：{m['title']}")
                return

    def toggle_milestone(self, mid: int, done: bool) -> None:
        services.milestone_update(mid, done=int(done))
        if done:
            sounds.play("milestone_done")
        ms = services.milestone_get(mid)
        if ms and ms.get("todo_id"):
            services.todo_update(ms["todo_id"], done=int(done))
        self.sync_cards()

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
        self.sync_cards()
        self._toast("已同步到日历（清单：科研DDL）")

    def unsync_milestone(self, mid: int) -> None:
        services.unsync_milestone_todo(mid)
        self.sync_cards()

    def edit_milestone(self, mid: int) -> None:
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
        self.sync_cards()

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
            self.sync_cards()
            self._toast("已删除（日历里那条待办也一并去掉了）")
            return
        services.milestone_delete(mid)
        self.sync_cards()
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
        rid = self._target_rid()
        if not rid:
            popups.notify(self, "先展开一个课题",
                          "点课题右下角的「更多」，再往它下面关联论文。")
            return
        if not services.arxiver_available():
            popups.notify(
                self, "没找到 Arxiver 论文库",
                f"预期路径：\n{services.arxiver_db_path()}\n\n"
                "在 Arxiver 里抓过论文后再来关联。", danger=True)
            return
        dlg = ArxivPickerDialog(self, rid)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            picked = dlg.selected()
            if not picked:
                popups.notify(self, "没选论文",
                              "在列表里点一下要关联的论文（可以多选）。")
                return
            for p in picked:
                services.research_paper_add(rid, p.get("arxiv_id", ""),
                                            p.get("title", ""))
            self.sync_cards()
            self._toast(f"已关联 {len(picked)} 篇到本课题")

    def unlink_paper(self, link_id: int) -> None:
        services.research_paper_delete(link_id)
        self.sync_cards()

    def edit_keywords(self) -> None:
        dlg = KeywordsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            services.set_research_keywords(_split_kw(dlg.keywords_text()))
            self.sync_cards()

    # ---------- 小反馈 ----------
    def _toast(self, text: str) -> None:
        lbl = getattr(self, "_toast_lbl", None)
        if lbl is None:
            lbl = QLabel(self)
            lbl.setObjectName("Toast")
            lbl.setAlignment(Qt.AlignCenter)
            self._toast_lbl = lbl
        lbl.setStyleSheet(
            f"background: {theme.get('surface')}; color: {theme.get('text')};"
            f"border: 1px solid {theme.get('border')}; padding: 8px 14px;"
            "border-radius: 10px; font-size: 12px;")
        lbl.setText(text)
        lbl.adjustSize()
        lbl.move(max(12, self.width() - lbl.width() - 24),
                 max(12, self.height() - lbl.height() - 24))
        lbl.show()
        lbl.raise_()
        QTimer.singleShot(1800, lbl.hide)
