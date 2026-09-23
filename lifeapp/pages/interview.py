"""八股刷题：自己上传的题库 + 抽题自答 + 艾宾浩斯复习。

玩法抄的是用户自己写的那套 tkinter 版 AnswerMachine：加权随机抽题（错的、
从没答对过的优先）、自己写答案、跟标准答案比相似度打分并把对不上的部分用
[] 标出来、手动判答对/答错/跳过，全部答对算一轮。

差别在于这里的数据落在 life_system 的库里，而且每道在刷的题会往待办的
「八股复习」清单挂一条复习待办 —— 排期规则跟算法页共用同一套内核。
"""
from __future__ import annotations

import os
from datetime import date, datetime

from PySide6.QtCore import Qt, QDate, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QPlainTextEdit, QScrollArea, QFrame,
    QSplitter, QStackedWidget, QFileDialog, QAbstractItemView,
)

from .. import popups, services, solution_card, sounds, theme, widgets
from .base import Page, stats_row

_ROLE_IID = Qt.UserRole
_SAVE_MS = 700


class ClickableStat(widgets.StatCard):
    """可点的统计卡：点「到期待复习」直接筛出那几道题。"""

    clicked = Signal()

    def __init__(self, label, value="0", accent="accent"):
        super().__init__(label, value, accent)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


def _time_ago(text: str) -> str:
    """'2026-09-21 14:30' → '3 天前'。刷题时想知道这题多久没碰过了。"""
    try:
        then = datetime.strptime((text or "").strip()[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return "从未见过"
    secs = (datetime.now() - then).total_seconds()
    if secs < 0:
        return "刚刚"
    if secs < 3600:
        return "%d 分钟前" % int(secs // 60)
    if secs < 86400:
        return "%d 小时前" % int(secs // 3600)
    return "%d 天前" % int(secs // 86400)


class InterviewPage(Page):
    def __init__(self):
        super().__init__("八股刷题",
                         "题库自己上传，抽题自答打分，复习点自动进待办的「八股复习」清单")
        self._iid = 0
        self._show_archived = False
        self._due_only = False
        self._sol_map: dict[int, str] = {}
        # 编辑器当前显示的是哪道题。_flush 靠它判断能不能写盘。
        self._editor_iid = 0
        self._loading = False
        self._first_show = True
        self._drill_id = 0          # 刷题模式当前抽到的题
        self._submitted = False

        self._build_stats()
        self._build_toolbar()

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_bank())
        self.stack.addWidget(self._build_drill())
        self.body().addWidget(self.stack, 1)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(_SAVE_MS)
        self._save_timer.timeout.connect(self._flush)

        if theme.manager is not None:
            theme.manager.changed.connect(self._on_theme_changed)
        self._bind_shortcuts()
        self.reload()

    def _bind_shortcuts(self) -> None:
        """刷题是高频重复动作，手不该在键盘和鼠标之间来回找按钮。
        判定用 Alt+1/2/3（Qt 的 (&N) 助记键自动生效），这里只补提交/看答案/退出。"""
        for seq, slot in (
                ("Ctrl+Return", self._submit),
                ("Ctrl+K", self._reveal),
                ("Esc", self._on_esc),
        ):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)
        # 资源管理器那两个快捷键。必须让位在文本框里：否则改个字一按 Delete
        # 或 Ctrl+A，整批题就被选中/删掉了。
        QShortcut(QKeySequence.SelectAll, self).activated.connect(self._on_select_all)
        QShortcut(QKeySequence(Qt.Key_Delete), self).activated.connect(self._on_delete_key)

    def _editing_text(self) -> bool:
        w = self.focusWidget()
        return isinstance(w, (QLineEdit, QPlainTextEdit))

    def _on_esc(self) -> None:
        if self.stack.currentIndex() == 1:
            self._set_mode("bank")
        elif self.list.hasFocus() and self.list.selectedItems():
            self.list.clearSelection()   # 列表里按 Esc = 取消选择

    def _on_select_all(self) -> None:
        if self.stack.currentIndex() == 0 and not self._editing_text():
            self.list.selectAll()

    def _on_delete_key(self) -> None:
        if self.stack.currentIndex() == 0 and not self._editing_text() \
                and self.list.selectedItems():
            self._batch_delete()

    # ------------------------------------------------------------------ 统计
    def _build_stats(self) -> None:
        self.card_total = widgets.StatCard("题库", "0", "accent")
        self.card_due = ClickableStat("到期待复习", "0", "amber")
        self.card_due.clicked.connect(self._toggle_due_filter)
        self.card_round = widgets.StatCard("本轮已答对", "0", "green")
        self.card_tried = widgets.StatCard("累计作答", "0", "blue")
        self.body().addLayout(stats_row(
            [self.card_total, self.card_due, self.card_round, self.card_tried]))

    def _toggle_due_filter(self) -> None:
        self._due_only = not self._due_only
        self._reload_list()

    # ---------------------------------------------------------------- 工具栏
    def _build_toolbar(self) -> None:
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self.seg_bank = QPushButton("题库")
        self.seg_bank.setObjectName("SegBtn")
        self.seg_bank.setCheckable(True)
        self.seg_bank.setChecked(True)
        self.seg_bank.setCursor(Qt.PointingHandCursor)
        self.seg_bank.clicked.connect(lambda: self._set_mode("bank"))
        bar.addWidget(self.seg_bank)

        self.seg_drill = QPushButton("刷题")
        self.seg_drill.setObjectName("SegBtn")
        self.seg_drill.setCheckable(True)
        self.seg_drill.setCursor(Qt.PointingHandCursor)
        self.seg_drill.setToolTip("按错误率加权随机抽题，自己写答案再对照打分")
        self.seg_drill.clicked.connect(lambda: self._set_mode("drill"))
        bar.addWidget(self.seg_drill)

        self.search = QLineEdit()
        self.search.setObjectName("TodoSearch")
        self.search.setPlaceholderText("🔍 搜问题 / 答案 / 标签")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(120)
        self.search.setMaximumWidth(220)
        self.search.textChanged.connect(self._reload_list)
        bar.addWidget(self.search)

        self.tag_filter = widgets.ComboBox()
        self.tag_filter.setMinimumWidth(104)
        self.tag_filter.setToolTip("按标签限定范围：题库列表和刷题抽题都跟着筛")
        self.tag_filter.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self.tag_filter)

        self.sort_box = widgets.ComboBox()
        self.sort_box.setMinimumWidth(104)
        self.sort_box.setToolTip("列表排序口径：找弱项时按「错得最多」或「最低分」排")
        for key, label in (("review", "按复习日"), ("weak", "错得最多"),
                           ("score", "得分最低"), ("stale", "最久没碰"),
                           ("new", "最近录入")):
            self.sort_box.addItem(label, key)
        self.sort_box.currentIndexChanged.connect(self._reload_list)
        bar.addWidget(self.sort_box)

        self.seg_active = QPushButton("在刷")
        self.seg_active.setObjectName("SegBtn")
        self.seg_active.setCheckable(True)
        self.seg_active.setChecked(True)
        self.seg_active.setCursor(Qt.PointingHandCursor)
        self.seg_active.clicked.connect(lambda: self._set_scope(False))
        bar.addWidget(self.seg_active)

        self.seg_arch = QPushButton("已归档")
        self.seg_arch.setObjectName("SegBtn")
        self.seg_arch.setCheckable(True)
        self.seg_arch.setCursor(Qt.PointingHandCursor)
        self.seg_arch.clicked.connect(lambda: self._set_scope(True))
        bar.addWidget(self.seg_arch)

        bar.addStretch(1)

        # 三个入口合成一个菜单：并排放会把整页最小宽顶到 1055px，
        # 窗口连半屏都缩不了（先试过窄屏换短标签，但短标签本身才是最小宽的来源，
        # 页面永远窄不到触发切换的宽度，属于鸡生蛋，所以直接合并）。
        add = QPushButton("＋ 添加")
        add.setObjectName("Primary")
        add.setCursor(Qt.PointingHandCursor)
        add.setToolTip("录一道 / 粘贴面经 / 从 txt 导入题库")
        add.clicked.connect(self._add_menu)
        bar.addWidget(add)

        self.body().addLayout(bar)

    def _set_mode(self, mode: str) -> None:
        self._flush()
        drill = mode == "drill"
        self.stack.setCurrentIndex(1 if drill else 0)
        self.seg_bank.setChecked(not drill)
        self.seg_drill.setChecked(drill)
        # 标签下拉两种模式都留着：刷题时最想「只刷某一科」，藏掉就只能整库乱刷
        self.tag_filter.setVisible(True)
        self.search.setVisible(not drill)
        self.sort_box.setVisible(not drill)
        self.seg_active.setVisible(not drill)
        self.seg_arch.setVisible(not drill)
        if drill:
            self._next_question(first=True)
        else:
            self.reload()

    def _on_filter_changed(self) -> None:
        """标签下拉在两种模式下都生效：题库列表跟着筛，刷题也重新抽一题。"""
        self._reload_list()
        if self.stack.currentIndex() == 1:
            self._next_question(first=True)

    def _set_scope(self, archived: bool) -> None:
        self._flush()
        self._show_archived = archived
        self.seg_active.setChecked(not archived)
        self.seg_arch.setChecked(archived)
        self.reload()

    # ================================================================ 题库模式
    def _build_bank(self) -> QWidget:
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_list_pane())
        split.addWidget(self._build_editor())
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([320, 620])
        split.setCollapsible(0, False)
        split.setCollapsible(1, False)
        return split

    def _build_list_pane(self) -> QWidget:
        w = QFrame()
        w.setObjectName("Card")
        w.setMinimumWidth(230)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        self.list = QListWidget()
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # ExtendedSelection 直接就是资源管理器语义：单击选、Ctrl+单击加减选、
        # Shift+单击连选（含从下往上）、点空白清空 —— 不用自己实现
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        self.list.currentRowChanged.connect(self._on_row_changed)
        self.list.itemSelectionChanged.connect(self._update_batch_bar)
        lay.addWidget(self.list, 1)

        # 批量操作条：选中两道以上才出现，窄栏放不下五个按钮，
        # 所以只留最常用的两个直给，其余收进 ⋯
        self.batch = QFrame()
        self.batch.setObjectName("InnerCard")
        bl = QHBoxLayout(self.batch)
        bl.setContentsMargins(10, 6, 8, 6)
        bl.setSpacing(6)
        self.batch_lbl = QLabel("")
        self.batch_lbl.setObjectName("Muted")
        bl.addWidget(self.batch_lbl)
        bl.addStretch(1)
        for text, tip, slot in (
                ("归档", "把选中的题归档（停止排复习、不再被抽到）",
                 lambda: self._batch_archive(True)),
                ("删除", "删除选中的题及其作答历史与复习待办", self._batch_delete),
                ("⋯", "更多批量操作（取消归档 / 标签 / 全选）", self._batch_more),
                ("✕", "取消选择", self._clear_selection)):
            b = QPushButton(text)
            b.setObjectName("Danger" if text == "删除" else "Ghost")
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setStyleSheet("padding: 3px 9px;")
            b.clicked.connect(slot)
            bl.addWidget(b)
        self.batch.hide()
        lay.addWidget(self.batch)

        self.hint = QLabel("")
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)
        return w

    def _row_widget(self, p: dict) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)
        title = QLabel(services._shorten(services.interview_display(p), 60))
        f = QFont()
        f.setBold(True)
        title.setFont(f)
        title.setWordWrap(True)
        lay.addWidget(title)
        meta = []
        if p.get("tags"):
            meta.append(p["tags"].replace(",", " · "))
        meta.append("对%d 错%d" % (int(p.get("correct_count") or 0),
                                  int(p.get("incorrect_count") or 0)))
        if int(p.get("best_score") or 0):
            meta.append("最高 %d 分" % p["best_score"])
        code = self._sol_map.get(p["id"]) or ""
        if code:
            meta.append("代码 " + code)
        if p.get("next_review"):
            meta.append("复习 " + p["next_review"])
        # 没写标准答案的题打不了分，必须一眼看出来，否则刷到它只会拿到莫名的 0 分
        if int(p.get("no_answer") or 0):
            meta.append("⚠ 缺标准答案")
        sub = QLabel("  |  ".join(meta))
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        if int(p.get("no_answer") or 0):
            sub.setStyleSheet("color: %s;" % theme.get("red"))
        lay.addWidget(sub)
        return w

    def _reload_list(self) -> None:
        self._flush()
        prev = self._iid
        self.list.blockSignals(True)
        self.list.clear()
        rows = services.interview_problem_list(
            archived=1 if self._show_archived else 0,
            keyword=self.search.text().strip(),
            tag=self.tag_filter.currentData() or "",
            due_only=self._due_only,
            order=self.sort_box.currentData() or "review")
        today = date.today().isoformat()
        self._sol_map = services.interview_solution_map()
        for p in rows:
            item = QListWidgetItem()
            item.setData(_ROLE_IID, p["id"])
            self.list.addItem(item)
            w = self._row_widget(p)
            self.list.setItemWidget(item, w)
            # setItemWidget 不自己撑行高，不给 sizeHint 整行会被压成一条窄带
            item.setSizeHint(w.sizeHint())
            if p.get("next_review") and p["next_review"] <= today:
                item.setForeground(QColor(theme.get("amber")))
        self.list.blockSignals(False)
        if not rows:
            self._iid = 0
        elif not any(r["id"] == prev for r in rows):
            self._iid = int(rows[0]["id"])
            self._select(self._iid)
        else:
            self._iid = int(prev)
            self._select(self._iid)
        self._render_editor()
        # 筛选生效时给卡片加个 ▾，否则用户不知道列表为什么变短了
        self.card_due.label_lbl.setText(
            "到期待复习 ▾" if self._due_only else "到期待复习")
        self.hint.setText("%d 道题%s%s" % (
            len(rows),
            "（已归档）" if self._show_archived else "",
            "（只看到期）" if self._due_only else ""))

    def focus_item(self, iid: int) -> None:
        """从待办页点复习条目跳进来：先把筛选全清掉，保证这道题一定在列表里。"""
        self._set_mode("bank")
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.tag_filter.blockSignals(True)
        self.tag_filter.setCurrentIndex(0)
        self.tag_filter.blockSignals(False)
        self._due_only = False
        self._show_archived = False
        self.seg_active.setChecked(True)
        self.seg_arch.setChecked(False)
        self._iid = int(iid)
        self.reload()
        self._select(self._iid)

    def _select(self, iid: int) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(_ROLE_IID) == iid:
                self.list.setCurrentRow(i)
                return

    def _on_row_changed(self, row: int) -> None:
        if row < 0 or row >= self.list.count():
            return
        iid = int(self.list.item(row).data(_ROLE_IID))
        if iid == self._iid:
            return
        self._flush()
        self._iid = iid
        self._render_editor()

    # ---------------------------------------------------------------- 编辑器
    def _build_editor(self) -> QWidget:
        holder = QWidget()
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.empty = QLabel("左边挑一道题，或点「＋ 录一道」「⇪ 导入题库」")
        self.empty.setObjectName("DetailEmpty")
        self.empty.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.empty, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 16)
        lay.setSpacing(8)
        scroll.setWidget(card)
        outer.addWidget(scroll, 1)

        lay.addWidget(self._label("问题"))
        self.question = QPlainTextEdit()
        # 题干可以不填：只记个力扣题号也算一道题，列表里显示「力扣 15」
        self.question.setPlaceholderText("题干（可留空，只填下面的题号也行）")
        self.question.setMinimumHeight(58)
        self.question.textChanged.connect(self.mark_dirty)
        lay.addWidget(self.question)

        lay.addWidget(self._label("标准答案"))
        self.answer = QPlainTextEdit()
        self.answer.setPlaceholderText("面试时打算怎么答的那版答案，打分拿它当参照")
        self.answer.setMinimumHeight(120)
        self.answer.textChanged.connect(self.mark_dirty)
        lay.addWidget(self.answer)

        lay.addWidget(self._label("解题代码"))
        code_head = QHBoxLayout()
        code_head.setSpacing(8)
        self.code_sum = QLabel("")
        self.code_sum.setObjectName("Muted")
        code_head.addWidget(self.code_sum, 1)
        self.add_sol_btn = QPushButton("＋ 加一版解法")
        self.add_sol_btn.setObjectName("Ghost")
        self.add_sol_btn.setCursor(Qt.PointingHandCursor)
        self.add_sol_btn.setToolTip("默认 C++；同一语言也能存多版（暴力 / 最优）")
        self.add_sol_btn.clicked.connect(self._add_solution)
        code_head.addWidget(self.add_sol_btn)
        lay.addLayout(code_head)
        self.sol_box = QVBoxLayout()
        self.sol_box.setSpacing(8)
        lay.addLayout(self.sol_box)
        self.no_sol = QLabel("还没写代码解法")
        self.no_sol.setObjectName("Muted")
        lay.addWidget(self.no_sol)
        # 数字题号拼不出链接，得提示用户自己粘，否则他会以为按钮坏了
        self.url_hint = QLabel("")
        self.url_hint.setObjectName("Muted")
        self.url_hint.setWordWrap(True)
        lay.addWidget(self.url_hint)

        tag_row = QHBoxLayout()
        tag_row.setSpacing(8)
        tag_row.addWidget(self._label("标签"))
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("逗号分隔，如 操作系统,内存")
        self.tags.editingFinished.connect(self._save_line_fields)
        tag_row.addWidget(self.tags, 1)
        lay.addLayout(tag_row)

        url_row = QHBoxLayout()
        url_row.setSpacing(8)
        url_row.addWidget(self._label("力扣"))
        self.lc_ref = QLineEdit()
        self.lc_ref.setPlaceholderText("题号或 slug")
        self.lc_ref.setMaximumWidth(150)
        self.lc_ref.editingFinished.connect(self._save_line_fields)
        url_row.addWidget(self.lc_ref)
        self.url = QLineEdit()
        self.url.setPlaceholderText("题目链接 / 参考博客（填题号会自动拼）")
        self.url.editingFinished.connect(self._save_line_fields)
        url_row.addWidget(self.url, 1)
        self.open_btn = QPushButton("打开")
        self.open_btn.setObjectName("Ghost")
        self.open_btn.setCursor(Qt.PointingHandCursor)
        self.open_btn.clicked.connect(self._open_url)
        url_row.addWidget(self.open_btn)
        lay.addLayout(url_row)

        self.seps = [widgets.hline(), widgets.hline()]
        lay.addWidget(self.seps[0])

        lay.addWidget(self._label("复习"))
        review_row = QHBoxLayout()
        review_row.setSpacing(8)
        self.review_date = widgets.DateInput()
        self.review_date.setMaximumWidth(150)
        self.review_date.setPlaceholderText("下次复习日")
        self.review_date.editingFinished.connect(self._save_review_date)
        review_row.addWidget(self.review_date)
        self.review_lbl = QLabel("")
        self.review_lbl.setObjectName("Muted")
        self.review_lbl.setWordWrap(True)
        review_row.addWidget(self.review_lbl, 1)
        lay.addLayout(review_row)

        self.record_lbl = QLabel("")
        self.record_lbl.setObjectName("Muted")
        self.record_lbl.setWordWrap(True)
        lay.addWidget(self.record_lbl)

        self.timeline = QVBoxLayout()
        self.timeline.setSpacing(2)
        lay.addLayout(self.timeline)

        lay.addWidget(self.seps[1])

        lay.addWidget(self._label("备注"))
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("哪里容易说漏、下次要补什么…")
        self.note.setMinimumHeight(64)
        self.note.textChanged.connect(self.mark_dirty)
        lay.addWidget(self.note)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.log_btn = QPushButton("✓ 刚答过一次")
        self.log_btn.setObjectName("Primary")
        self.log_btn.setCursor(Qt.PointingHandCursor)
        self.log_btn.setToolTip("口头答完也能记一笔，按结果重排下一个记忆点")
        self.log_btn.clicked.connect(self._log_manual)
        bottom.addWidget(self.log_btn)
        self.arch_btn = QPushButton("归档")
        self.arch_btn.setObjectName("Ghost")
        self.arch_btn.setCursor(Qt.PointingHandCursor)
        self.arch_btn.setToolTip("归档 = 不再排复习也不再抽到，记录全留着")
        self.arch_btn.clicked.connect(self._toggle_archive)
        bottom.addWidget(self.arch_btn)
        self.del_btn = QPushButton("删除这道题")
        self.del_btn.setObjectName("Ghost")
        self.del_btn.setCursor(Qt.PointingHandCursor)
        self.del_btn.clicked.connect(self._delete_problem)
        bottom.addWidget(self.del_btn)
        bottom.addStretch(1)
        lay.addLayout(bottom)
        lay.addStretch(1)

        self._editor_widgets = [self.question, self.answer, self.tags,
                                self.lc_ref, self.url, self.open_btn,
                                self.review_date, self.review_lbl,
                                self.record_lbl, self.note, self.log_btn,
                                self.arch_btn, self.del_btn, self.code_sum,
                                self.add_sol_btn, self.no_sol, self.url_hint,
                                *self.seps]
        scroll.hide()
        self._editor_scroll = scroll
        return holder

    def _label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("CardTitle")
        return lbl

    def _render_editor(self) -> None:
        p = services.interview_problem_get(self._iid) if self._iid else {}
        has = bool(p)
        for w in self._editor_widgets:
            w.setVisible(has)
        self.empty.setVisible(not has)
        self._editor_scroll.setVisible(has)
        self._editor_iid = self._iid if has else 0
        if not has:
            return

        self._loading = True
        self.question.setPlainText(p.get("question") or "")
        self.answer.setPlainText(p.get("answer") or "")
        self.tags.setText(p.get("tags") or "")
        self.lc_ref.setText(p.get("lc_ref") or "")
        self.url.setText(p.get("url") or "")
        self.url_hint.setText(
            "题号是纯数字，「打开」会进力扣搜索页；填英文 slug 才能直达题面。"
            if services.interview_ref_is_numeric(p) else "")
        self.note.setPlainText(p.get("note") or "")
        d = p.get("next_review") or ""
        self.review_date.setDate(
            QDate.fromString(d, "yyyy-MM-dd") if d else QDate())
        self.review_lbl.setText(self._review_text(p))
        self.record_lbl.setText(
            "答对 %d 次 · 答错 %d 次 · 已见 %d 次 · 最高 %d 分 · 上次 %s"
            % (int(p.get("correct_count") or 0), int(p.get("incorrect_count") or 0),
               int(p.get("seen_count") or 0), int(p.get("best_score") or 0),
               _time_ago(p.get("last_seen_at") or "")))
        self.arch_btn.setText("取消归档" if p.get("archived") else "归档")

        _clear_layout(self.timeline)
        for a in services.interview_attempt_list(p["id"])[:6]:
            verdict = {"correct": "答对", "incorrect": "答错"}.get(
                a["decision"], "跳过")
            txt = "· %s · %s" % (a["answered_at"], verdict)
            if (a.get("user_answer") or "").strip():
                txt += " · %d 分" % int(a.get("score") or 0)
            lbl = QLabel(txt)
            lbl.setObjectName("Muted")
            self.timeline.addWidget(lbl)

        _clear_layout(self.sol_box)
        sols = services.interview_solution_list(p["id"])
        for sol in sols:
            self.sol_box.addWidget(self._new_card(sol))
        self.no_sol.setVisible(not sols)
        self.code_sum.setText(self._code_summary(sols))
        self._loading = False

    def _new_card(self, sol: dict) -> solution_card.SolutionCard:
        return solution_card.SolutionCard(
            sol,
            on_save=lambda sid, **f: services.interview_solution_update(sid, **f),
            on_delete=services.interview_solution_delete,
            on_touch=self.mark_dirty,
            on_commit=self.reload)

    def _format_solutions(self, sols: list[dict]) -> str:
        return solution_card.as_text(sols)

    def _code_summary(self, sols: list[dict]) -> str:
        return solution_card.summary(sols)

    def _solution_cards(self) -> list[solution_card.SolutionCard]:
        return [self.sol_box.itemAt(i).widget()
                for i in range(self.sol_box.count())
                if isinstance(self.sol_box.itemAt(i).widget(),
                              solution_card.SolutionCard)]

    def _add_solution(self) -> None:
        if not self._iid:
            return
        self._flush()
        sid = services.interview_solution_add(
            self._iid, "", "", services.SOLUTION_DEFAULT_LANG,
            allow_empty=True)
        if sid:
            self._render_editor()   # 只重画右侧，别把左侧列表也刷没了

    def _review_text(self, p: dict) -> str:
        stage = int(p.get("stage") or 0)
        nxt = p.get("next_review") or ""
        if not nxt:
            if stage >= len(services.ALGO_INTERVALS):
                return "已越过最后一档（30 天），不再排复习"
            return "没排复习 —— 在左边填个日期就能排上"
        try:
            delta = (date.fromisoformat(nxt) - date.today()).days
        except ValueError:
            return "下次复习 %s" % nxt
        when = ("就是今天" if delta == 0 else
                "逾期 %d 天" % -delta if delta < 0 else "还有 %d 天" % delta)
        gap = (services.ALGO_INTERVALS[stage]
               if stage < len(services.ALGO_INTERVALS) else 0)
        return ("第 %d 档 · 间隔 %d 天 · %s（%s）· 已进待办「八股复习」"
                % (stage + 1, gap, nxt, when))

    # ------------------------------------------------------------ 批量操作
    def _selected_ids(self) -> list[int]:
        return [int(it.data(_ROLE_IID)) for it in self.list.selectedItems()]

    def _update_batch_bar(self) -> None:
        n = len(self.list.selectedItems())
        self.batch_lbl.setText("已选 %d 道" % n)
        self.batch.setVisible(n >= 2 and self.stack.currentIndex() == 0)

    def _clear_selection(self) -> None:
        self.list.clearSelection()

    def _after_batch(self, keep: int = 0) -> None:
        """批量操作后统一收尾：清掉选择再重刷，避免选中态指向已消失的行。"""
        self.list.clearSelection()
        self._iid = keep
        self.reload()

    def _batch_archive(self, on: bool) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        n = services.interview_archive_many(ids, on)
        self._after_batch()
        popups.notify(self, "批量归档" if on else "批量取消归档",
                      "处理了 %d 道题。" % n)

    def _batch_delete(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        if not popups.confirm(
                self, "删除 %d 道题" % len(ids),
                "这些题的作答历史和复习待办会一起删掉，删掉之后找不回来。"):
            return
        n = services.interview_delete_many(ids)
        self._after_batch()
        popups.notify(self, "已删除", "删掉了 %d 道题。" % n)

    def _batch_add_tags(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        raw, ok = popups.ask_text(self, "加标签",
                                  "要加的标签（多个用逗号分隔）")
        if not ok or not raw.strip():
            return
        n = services.interview_tags_add(ids, raw)
        self._after_batch()
        popups.notify(self, "已加标签", "给 %d 道题加了「%s」。" % (n, raw.strip()))

    def _batch_remove_tags(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        # 只列选中题身上真实存在的标签，点了才发现「没这个标签」很烦
        present: list[str] = []
        for iid in ids:
            for t in (services.interview_problem_get(iid).get("tags") or "").split(","):
                t = t.strip()
                if t and t not in present:
                    present.append(t)
        if not present:
            popups.notify(self, "没有可移除的标签", "选中的题都没打标签。")
            return
        tag, ok = popups.get_item(self, "移除标签", "移除哪个标签", present)
        if not ok:
            return
        n = services.interview_tags_remove(ids, tag)
        self._after_batch()
        popups.notify(self, "已移除标签", "从 %d 道题上移除了「%s」。" % (n, tag))

    def _batch_select_missing(self) -> None:
        """只选中缺标准答案的题 —— 导入完最常见的下一步。"""
        missing = {x["id"] for x in services.interview_problem_list(archived=-1)
                   if int(x.get("no_answer") or 0)}
        self.list.clearSelection()
        hit = 0
        for i in range(self.list.count()):
            it = self.list.item(i)
            if int(it.data(_ROLE_IID)) in missing:
                it.setSelected(True)
                hit += 1
        if not hit:
            popups.notify(self, "没有缺答案的题", "当前列表里每道都有标准答案。")

    def _batch_more(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        acts = ["取消归档", "加标签", "移除标签", "只选中缺标准答案的", "全选"]
        pick, ok = popups.get_item(self, "批量操作",
                                   "对选中的 %d 道题做什么？" % len(ids), acts)
        if not ok:
            return
        if pick == "取消归档":
            self._batch_archive(False)
        elif pick == "加标签":
            self._batch_add_tags()
        elif pick == "移除标签":
            self._batch_remove_tags()
        elif pick == "全选":
            self.list.selectAll()
        elif pick == "只选中缺标准答案的":
            self._batch_select_missing()

    def _list_menu(self, pos) -> None:
        """右键：落在没选中的行上时先只选它（资源管理器行为），再弹同一套动作。"""
        it = self.list.itemAt(pos)
        if it is not None and not it.isSelected():
            self.list.setCurrentItem(it)
        if not self._selected_ids():
            return
        self._batch_more()

    # ================================================================ 刷题模式
    def _build_drill(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)

        self.drill_progress = QLabel("")
        self.drill_progress.setObjectName("Muted")
        self.drill_progress.setWordWrap(True)
        lay.addWidget(self.drill_progress)
        self.drill_bar = widgets.GoalProgress()
        lay.addWidget(self.drill_bar)

        lay.addWidget(widgets.hline())

        self.drill_question = QLabel("点下面开始")
        f = QFont()
        f.setBold(True)
        f.setPointSizeF(14)
        self.drill_question.setFont(f)
        self.drill_question.setWordWrap(True)
        self.drill_question.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.drill_question)

        self.drill_meta = QLabel("")
        self.drill_meta.setObjectName("Muted")
        lay.addWidget(self.drill_meta)

        self.drill_answer = QPlainTextEdit()
        self.drill_answer.setPlaceholderText("在这儿写下你的答案，写完提交就能拿到与标准答案的相似度")
        self.drill_answer.setMinimumHeight(120)
        lay.addWidget(self.drill_answer)

        act = QHBoxLayout()
        act.setSpacing(8)
        self.submit_btn = QPushButton("提交答案")
        self.submit_btn.setObjectName("Primary")
        self.submit_btn.setCursor(Qt.PointingHandCursor)
        self.submit_btn.setToolTip("对照标准答案打分并标出差异（Ctrl+Enter）")
        self.submit_btn.clicked.connect(self._submit)
        act.addWidget(self.submit_btn)
        # 口述作答是八股的主流用法：不打字也得能看答案再自评
        self.reveal_btn = QPushButton("看答案")
        self.reveal_btn.setObjectName("Ghost")
        self.reveal_btn.setCursor(Qt.PointingHandCursor)
        self.reveal_btn.setToolTip("只显示标准答案，不打分（Ctrl+K）")
        self.reveal_btn.clicked.connect(self._reveal)
        act.addWidget(self.reveal_btn)
        self.drill_open = QPushButton("打开参考链接")
        self.drill_open.setObjectName("Ghost")
        self.drill_open.setCursor(Qt.PointingHandCursor)
        self.drill_open.clicked.connect(self._open_drill_url)
        act.addWidget(self.drill_open)
        act.addStretch(1)
        lay.addLayout(act)

        self.drill_result = QPlainTextEdit()
        self.drill_result.setReadOnly(True)
        self.drill_result.setMinimumHeight(150)
        self.drill_result.setPlaceholderText("提交后这里显示得分、标准答案，以及对不上的部分")
        lay.addWidget(self.drill_result)

        grade = QHBoxLayout()
        grade.setSpacing(8)
        # (&1) 是 Windows 的下划线助记键：Alt+1/2/3 判定，手不用离开键盘
        self.btn_right = QPushButton("答对(&1)")
        self.btn_wrong = QPushButton("答错(&2)")
        self.btn_skip = QPushButton("跳过(&3)")
        for btn, verdict, key in ((self.btn_right, "correct", "Alt+1"),
                                  (self.btn_wrong, "incorrect", "Alt+2"),
                                  (self.btn_skip, "skip", "Alt+3")):
            btn.setObjectName("Primary" if verdict == "correct" else "Ghost")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip("%s 判定并换下一题（%s）" % (
                {"correct": "答对", "incorrect": "答错", "skip": "跳过"}[verdict], key))
            btn.clicked.connect(lambda _c=False, v=verdict: self._grade(v))
            grade.addWidget(btn)
        grade.addStretch(1)
        lay.addLayout(grade)

        self.drill_hint = QLabel("到期优先：先把复习日到点的题清掉，再按错误率加权抽。"
                                 "Ctrl+Enter 提交 · Ctrl+K 看答案 · Alt+1/2/3 判定 · Esc 回题库")
        self.drill_hint.setObjectName("Muted")
        self.drill_hint.setWordWrap(True)
        lay.addWidget(self.drill_hint)

        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        # 必须套滚动区：卡片里光题干+答题框+结果框就 ~500px，判定按钮排在最底下。
        # 不滚的话窗口一矮（768 高的笔记本很常见）按钮就掉到视口外，
        # 而 Qt 的 isVisible() 照样返回 True，看着「在」其实点不到。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(card)
        wl.addWidget(scroll)
        self._drill_scroll = scroll
        return wrap

    def _refresh_progress(self) -> None:
        st = services.interview_stats()
        d = st["dist"]
        self.drill_progress.setText(
            "本轮进度 %d/%d　|　正确次数分布：0次 %d　1次 %d　2次 %d　3次 %d　>3次 %d"
            % (st["mastered"], st["active"], d[0], d[1], d[2], d[3], d[4]))
        ratio = (st["mastered"] / st["active"]) if st["active"] else 0.0
        self.drill_bar.set_progress(
            ratio, "%d / %d" % (st["mastered"], st["active"]))

    def _next_question(self, first: bool = False) -> None:
        self._refresh_progress()
        if not services.interview_problem_list(archived=0):
            self._drill_id = 0
            self.drill_question.setText("题库还是空的")
            self.drill_meta.setText("先在「题库」里录题，或用「⇪ 导入题库」导一份 txt")
            self.drill_result.clear()
            self.drill_answer.clear()
            for b in (self.submit_btn, self.reveal_btn, self.btn_right,
                      self.btn_wrong, self.btn_skip, self.drill_open):
                b.setEnabled(False)
            return
        if services.interview_round_complete() and not first:
            sounds.play("round_complete")
            again, _ok = popups.get_item(
                self, "这一轮答完了", "每题都至少答对过一次。",
                ["重开一轮（清空本轮标记，保留统计）", "就到这儿"])
            if again and again.startswith("重开"):
                services.interview_reset_round()
                self.reload()
            else:
                self._set_mode("bank")
                return
            self._refresh_progress()
        for b in (self.submit_btn, self.btn_right, self.btn_wrong,
                  self.btn_skip, self.drill_open):
            b.setEnabled(True)
        p = services.interview_pick(exclude_id=self._drill_id,
                                    tag=self.tag_filter.currentData() or "")
        if not p:                       # 选了标签但那科一道题都没有
            self._drill_id = 0
            self.drill_question.setText("这个标签下还没有题")
            self.drill_meta.setText("换个标签，或到「题库」里给题打上标签")
            for b in (self.submit_btn, self.reveal_btn, self.btn_right,
                      self.btn_wrong, self.btn_skip, self.drill_open):
                b.setEnabled(False)
            return
        self._drill_id = int(p["id"])
        self._submitted = False
        self.drill_question.setText(services.interview_display(p))
        today = date.today().isoformat()
        due = bool(p.get("next_review") and p["next_review"] <= today)
        bits = ["对 %d" % int(p.get("correct_count") or 0),
                "错 %d" % int(p.get("incorrect_count") or 0),
                "已见 %d" % int(p.get("seen_count") or 0),
                "最高 %d 分" % int(p.get("best_score") or 0),
                "上次 " + _time_ago(p.get("last_seen_at") or "")]
        if due:
            bits.insert(0, "⏰ 今天到期")
        if p.get("tags"):
            bits.append("标签 " + p["tags"])
        if int(p.get("no_answer") or 0):
            bits.append("⚠ 缺标准答案（打不了分）")
        self.drill_meta.setText("　·　".join(bits))
        self.drill_answer.clear()
        self.drill_result.clear()
        self.drill_open.setEnabled(bool((p.get("url") or "").strip()))
        left = len([x for x in services.interview_problem_list(
            archived=0, due_only=True) if x["id"] != self._drill_id])
        self.drill_hint.setText(
            "到期优先：先把复习日到点的题清掉，再按错误率加权抽（错得越多越常来）。"
            "今天还剩 %d 道到期。" % left)

    def _ref_of_current(self) -> str:
        p = services.interview_problem_get(self._drill_id) if self._drill_id else {}
        return (p or {}).get("answer") or ""

    def _in_drill(self) -> bool:
        """Ctrl+Enter / Ctrl+K 是刷题用的，题库模式下不能作用在「上次刷过的那题」上。"""
        return self.stack.currentIndex() == 1 and bool(self._drill_id)

    def _reveal(self) -> None:
        """只给看标准答案，不打分 —— 口述作答后对答案用的。"""
        if not self._in_drill():
            return
        ref = self._ref_of_current()
        sols = services.interview_solution_list(self._drill_id)
        extra = "\n\n" + self._format_solutions(sols) if sols else ""
        if not ref:
            # 没写文字版答案但存了代码时，照样得能复习自己的解法
            if sols:
                self.drill_result.setPlainText(
                    "这题还没写文字版答案，下面是你存过的解法：%s" % extra)
                return
            self.drill_result.setPlainText(
                "这题还没写标准答案。\n切到「题库」补上之后就能打分了。")
            return
        self.drill_result.setPlainText("标准答案：\n%s%s" % (ref, extra.lstrip()))

    def _submit(self) -> None:
        if not self._in_drill():
            return
        p = services.interview_problem_get(self._drill_id)
        if not p:
            return
        user = self.drill_answer.toPlainText().strip()
        ref = p.get("answer") or ""
        sols = services.interview_solution_list(self._drill_id)
        # 存过的解法每次都带上：复习时最想看的正是自己当初怎么写的
        extra = "\n\n" + self._format_solutions(sols) if sols else ""
        if not ref:
            # 没有参照物就别给分：报 0 分只会让人以为是自己答得差
            self._last_score = 0
            self._submitted = True
            self.drill_result.setPlainText(
                "这题还没有标准答案，打不了分。\n\n"
                "你写的：\n%s%s\n\n"
                "自己对照一下，直接按答对/答错判定就行；"
                "想要打分请到「题库」给这题补上标准答案。" % (user, extra))
            return
        score = services.interview_score(ref, user)
        self._last_score = score
        self._submitted = True
        if not user:
            self.drill_result.setPlainText(
                "没写答案，就不打分了。\n\n标准答案：\n%s%s" % (ref, extra))
        else:
            shown = services.interview_highlight(ref, user)
            # 一次性 setPlainText：QPlainTextEdit 没有 append()，分两次写会当场抛
            # AttributeError 而被 Qt 悄悄吞掉，结果框就少了「你写的」那一段。
            self.drill_result.setPlainText(
                "得分：%d/100\n\n"
                "标准答案（[ ] 里是你没对上或说岔的部分）：\n%s\n\n"
                "你写的：\n%s%s" % (score, shown, user, extra))

    def _grade(self, verdict: str) -> None:
        if not self._drill_id:
            return
        user = self.drill_answer.toPlainText().strip()
        p = services.interview_problem_get(self._drill_id)
        score = getattr(self, "_last_score", 0) if self._submitted else \
            services.interview_score((p or {}).get("answer") or "", user)
        services.interview_log_attempt(self._drill_id, user, score, verdict)
        # 跳过不出声，只有真判了对/错才给反馈
        sounds.play({"correct": "answer_correct",
                     "incorrect": "answer_wrong"}.get(verdict, ""))
        self.reload()
        self._next_question()

    def _open_drill_url(self) -> None:
        p = services.interview_problem_get(self._drill_id)
        if p and p.get("url"):
            _open_url(p["url"], self)

    # ------------------------------------------------------------------ 数据
    def reload(self) -> None:
        self._refresh_tags()
        self._reload_list()
        st = services.interview_stats()
        self.card_total.set_value(str(st["active"]))
        self.card_due.set_value(str(st["due"]))
        self.card_round.set_value("%d/%d" % (st["mastered"], st["active"]))
        self.card_tried.set_value(str(st["attempts"]))
        if self.stack.currentIndex() == 1:
            self._refresh_progress()

    def _refresh_tags(self) -> None:
        want = self.tag_filter.currentData() or ""
        self.tag_filter.blockSignals(True)
        self.tag_filter.clear()
        self.tag_filter.addItem("全部标签", "")
        for t in services.interview_tags():
            self.tag_filter.addItem(t, t)
        self.tag_filter.setCurrentIndex(max(self.tag_filter.findData(want), 0))
        self.tag_filter.blockSignals(False)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            return
        self.reload()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._flush()               # 切走之前把没落盘的编辑写掉，别丢
        super().hideEvent(event)

    # -------------------------------------------------------------- 自动保存
    def mark_dirty(self) -> None:
        if not self._loading:
            self._save_timer.start()

    def _flush(self) -> None:
        """把编辑器里所有可编辑字段落库。

        标签和链接也必须在这里一起存：`_flush` 会触发列表重刷，
        而重刷会拿库里的值覆盖输入框 —— 只存备注/题干的话，用户正在标签框里
        打了一半的字就会被悄悄吃掉（日历卡上栽过同一个坑）。
        """
        self._save_timer.stop()
        if not self._iid or self._loading:
            return
        # 编辑器里显示的还是别的题时绝不能写：刚录完一道新题、面板还没换
        # 过去的时候落盘，会把上一题的题干和答案原样盖到新题上（真丢数据）。
        if self._editor_iid != self._iid:
            self._render_editor()
            return
        p = services.interview_problem_get(self._iid)
        if not p:
            return
        fields = {}
        q = self.question.toPlainText().strip()
        if q and (p.get("question") or "") != q:
            fields["question"] = q
        ans = self.answer.toPlainText().strip()
        if (p.get("answer") or "") != ans:
            fields["answer"] = ans
        if (p.get("note") or "") != self.note.toPlainText():
            fields["note"] = self.note.toPlainText()
        tags = self.tags.text().strip()
        if (p.get("tags") or "") != tags:
            fields["tags"] = tags
        ref = self.lc_ref.text().strip()
        if (p.get("lc_ref") or "") != ref:
            fields["lc_ref"] = ref
        url = self.url.text().strip()
        if (p.get("url") or "") != url:
            fields["url"] = url
        if fields:
            services.interview_problem_update(self._iid, **fields)
            self._reload_list()
        # 代码/思路每 700ms 落一次盘：写几十行代码中途崩了不该全没
        for card in self._solution_cards():
            card.flush()

    def _save_line_fields(self) -> None:
        if self._loading or not self._iid:
            return
        services.interview_problem_update(
            self._iid, tags=self.tags.text().strip(),
            lc_ref=self.lc_ref.text().strip(),
            url=self.url.text().strip())
        self._reload_list()

    def _save_review_date(self) -> None:
        if self._loading or not self._iid:
            return
        d = self.review_date.date()
        services.interview_set_next_review(
            self._iid, d.toString("yyyy-MM-dd") if d.isValid() else "")
        self.reload()

    # ------------------------------------------------------------------ 动作
    def _add_problem(self) -> None:
        """录一道题。题干和题号至少给一个，答案/代码到右侧面板里写。

        以前是先弹答案框 —— 但那是多行内容，塞在 96px 的小弹层里很难受，
        而且面板里本来就有自动保存的编辑框，直接在面板写更顺手。
        """
        self._flush()
        q, ok = popups.ask_text(self, "录一道题", "问题（可留空，只填题号也行）")
        if not ok:
            return
        ref, ok2 = popups.ask_text(self, "录一道题",
                                   "力扣题号或 slug（可留空）")
        if not ok2:
            return
        if not q.strip() and not ref.strip():
            popups.notify(self, "至少给一个",
                          "问题和力扣题号不能都空着，否则这道题没法认出来。")
            return
        iid = services.interview_problem_add(q, "", "", "", "", ref)
        if not iid:
            return
        self._iid = iid
        self.reload()
        popups.notify(self, "已录入",
                      "答案、代码、思路直接在右侧面板里写，停手就自动存。")

    _IMPORT_DIR_KEY = "bagu_import_dir"

    def _import_start_dir(self) -> str:
        last = services.db.get_setting(self._IMPORT_DIR_KEY, "")
        if last and os.path.isdir(last):
            return last
        return os.path.expanduser("~")

    def _import_deck(self) -> None:
        self._flush()
        paths, _flt = QFileDialog.getOpenFileNames(
            self, "选择题库 txt（可多选）", self._import_start_dir(),
            "文本文件 (*.txt);;所有文件 (*)")
        if not paths:
            return
        services.db.set_setting(
            self._IMPORT_DIR_KEY, os.path.dirname(paths[0]))
        # 一次选多科：每份文件单独解析，坏掉的那份报出来但不拖累其余
        texts: list[tuple[str, str]] = []
        bad: list[str] = []
        for path in paths:
            text = _read_text(path)
            if text is None:
                bad.append(os.path.basename(path) + "（编码不是 UTF-8/GBK）")
                continue
            if not services.interview_parse_deck(text):
                bad.append(os.path.basename(path) + "（没解析出题目）")
                continue
            texts.append((path, text))
        if not texts:
            popups.notify(self, "没能导入", "\n".join(bad) or "没选中文件。",
                          danger=True)
            return
        total = sum(len(services.interview_parse_deck(t)) for _p, t in texts)
        names = "、".join(os.path.basename(p) for p, _t in texts[:4])
        if len(texts) > 4:
            names += " 等 %d 份" % len(texts)
        if not popups.confirm(
                self, "导入题库",
                "%s\n\n共解析出 %d 道题。重复的问题会自动跳过，不会导两遍。"
                % (names, total), ok_text="导入"):
            return
        tag, _ok = popups.ask_text(
            self, "导入题库", "给这批题打个标签？（可留空，一般填科目名）")
        tag = (tag or "").strip()
        created = skipped = 0
        for _p, t in texts:
            res = services.interview_import_text(t, tag)
            created += res["created"]
            skipped += res["skipped"]
        self.reload()
        msg = "新增 %d 道，跳过重复 %d 道。\n每道都排好了第一个复习点。" % (
            created, skipped)
        if bad:
            msg += "\n\n这 %d 份没读进去：\n%s" % (len(bad), "\n".join(bad))
        popups.notify(self, "导入完成", msg)

    ADD_ACTIONS = ["录一道题", "粘贴面经（一组多条）", "从 txt 导入题库"]

    def _add_menu(self) -> None:
        pick, ok = popups.get_item(self, "添加到题库", "怎么加？", self.ADD_ACTIONS)
        if not ok:
            return
        if pick == self.ADD_ACTIONS[0]:
            self._add_problem()
        elif pick == self.ADD_ACTIONS[1]:
            self._paste_notes()
        elif pick == self.ADD_ACTIONS[2]:
            self._import_deck()

    def _paste_notes(self) -> None:
        """粘贴面经：一组一面 → 若干编号问题。只有问题没有答案，所以
        组名转成标签、答案留空，导完直接按该标签筛出来逐条补。"""
        self._flush()
        text, ok = popups.ask_note(
            self, "粘贴面经",
            "每组用「字节llm算法一面：」开头，下面写 1. 2. 3. 编号问题；"
            "「代码：xxx」单独成一条，「代码：无」自动忽略",
            width=640, height=320)
        if not ok or not (text or "").strip():
            return
        parsed = services.interview_parse_notes(text)
        if not parsed:
            popups.notify(self, "没识别出问题",
                          "问题要写成编号列表，例如：\n\n字节llm算法一面：\n"
                          "1. 用户的画像是怎么获取的？\n2. 讲下 fid 和 lpips",
                          danger=True)
            return
        groups: dict[str, list[str]] = {}
        for g, q in parsed:
            groups.setdefault(g or "（无组名）", []).append(q)
        lines = []
        for g, qs in list(groups.items())[:10]:
            preview = "、".join(services._shorten(q, 14) for q in qs[:3])
            if len(qs) > 3:
                preview += " …"
            lines.append("· %s：%d 条（%s）" % (g, len(qs), preview))
        if len(groups) > 10:
            lines.append("… 另外 %d 组" % (len(groups) - 10))
        summary = ("共 %d 组 %d 条：\n%s\n\n"
                   "组名会存成标签；答案先留空，导入后用批量操作里的"
                   "「只选中缺标准答案的」逐条补。"
                   % (len(groups), len(parsed), "\n".join(lines)))
        if not popups.confirm(self, "确认录入", summary, ok_text="录入",
                              width=470):
            return
        res = services.interview_import_notes(text)
        self.reload()
        # 录入完顺手切到第一组的标签视图：用户下一步就是补答案，别让他再找
        first = next(iter(groups), "")
        idx = self.tag_filter.findData(first)
        if idx >= 0:
            self.tag_filter.setCurrentIndex(idx)
        popups.notify(
            self, "录入完成",
            "新增 %d 条，跳过重复 %d 条。\n当前按「%s」筛选。"
            % (res["created"], res["skipped"], first or "全部"), width=430)

    def _log_manual(self) -> None:
        if not self._iid:
            return
        opts = ["答对了", "没答上来"]
        pick, ok = popups.get_item(self, "记一次作答",
                                   "这次答得怎么样？", opts)
        if not ok:
            return
        services.interview_log_attempt(
            self._iid, "", 0, "correct" if pick == opts[0] else "incorrect")
        sounds.play("answer_correct" if pick == opts[0] else "answer_wrong")
        self.reload()

    def _toggle_archive(self) -> None:
        if not self._iid:
            return
        self._flush()
        p = services.interview_problem_get(self._iid)
        on = not p.get("archived")
        services.interview_problem_set_archived(self._iid, on)
        # 跟着这道题切到它新所在的视图，不然点完归档它当场从列表里消失
        self._show_archived = on
        self.seg_active.setChecked(not on)
        self.seg_arch.setChecked(on)
        self.reload()

    def _delete_problem(self) -> None:
        if not self._iid:
            return
        p = services.interview_problem_get(self._iid)
        if not popups.confirm(self, "删除这道题",
                              "「%s」的作答历史和复习待办会一起删掉。"
                              % services._shorten(p["question"], 24)):
            return
        services.interview_problem_delete(self._iid)
        self._iid = 0
        self.reload()

    def _open_url(self) -> None:
        p = services.interview_problem_get(self._iid) if self._iid else None
        url = (p or {}).get("url") or ""
        if not url:
            popups.notify(self, "还没有链接", "在「链接」里填一个就能跳浏览器。")
            return
        _open_url(url, self)

    def _on_theme_changed(self) -> None:
        self._flush()
        self.reload()


def _open_url(url: str, parent=None) -> None:
    if not url:
        return
    if not QDesktopServices.openUrl(QUrl(url)):
        popups.notify(parent, "打不开链接",
                      "系统浏览器没能打开：\n%s" % url, danger=True)


def _read_text(path: str):
    """题库 txt 常见就两种编码，先按 UTF-8 读，失败再退 GBK。"""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
        except OSError:
            return None
    return None


def _clear_layout(lay) -> None:
    while lay.count():
        it = lay.takeAt(0)
        if it.widget():
            it.widget().deleteLater()
