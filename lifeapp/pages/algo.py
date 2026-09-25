"""算法刷题：我自己的题库记录本 + 艾宾浩斯复习排期。

存的都是我录进去的东西：标题、标签、力扣题号/链接、我自己写的题解（可多条）、
备注、写作历史（次数 + 每次时间 + 当时是否独立做出来）、下一次复习时间。
不存第三方站点抓来的正文 —— 题目内容一律点链接跳浏览器看。

复习排期不在这一页自说自话：每个艾宾浩斯记忆点都落成「算法复习」清单里的
一条待办（排期逻辑全在 services.algo_*）。在待办页勾掉它时会问一句
「独立做出来了吗」，答"是"进下一档、答"否"退回一档重记。
"""
from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt, QDate, QTimer, QUrl
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QKeySequence, QShortcut,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListWidgetItem, QPlainTextEdit, QScrollArea, QFrame,
    QSplitter, QAbstractItemView,
)

from .. import popups, services, solution_tabs, sounds, theme, widgets
from .base import Page

# 列表项上挂的题目 id
_ROLE_PID = Qt.UserRole

_SAVE_MS = 700          # 备注/题解的自动保存节流：打字过程中不落库


class AlgoPage(Page):
    def __init__(self):
        super().__init__("算法刷题",
                         "记录题目与题解，复习点自动进待办的「算法复习」清单")
        self._pid = 0
        self._show_archived = False
        self._loading = False
        self._first_show = True
        self._n_rows = 0
        self._editor_pid = 0
        self._select_sol = 0        # 重画详情时要选中哪一版解法（0=保持）
        self._sol_map: dict[int, str] = {}

        self._build_stats()
        self._build_toolbar()

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_left())
        split.addWidget(self._build_right())
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([320, 620])
        split.setCollapsible(0, False)
        split.setCollapsible(1, False)
        self.body().addWidget(split, 1)
        self._bind_shortcuts()

        # 自动保存：备注 / 题解改动攒到停手后再一次性落库
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(_SAVE_MS)
        self._save_timer.timeout.connect(self._flush)

        if theme.manager is not None:
            theme.manager.changed.connect(self._on_theme_changed)
        self.reload()

    # ------------------------------------------------------------------ 统计
    def _build_stats(self) -> None:
        # 缩成小胶囊挂在大标题同一行的右边，不再单独占一整排
        self.card_active = widgets.StatCard("在刷", "0", "accent", mini=True)
        self.card_due = widgets.StatCard("到期待复习", "0", "amber", mini=True)
        self.card_grad = widgets.StatCard("已毕业", "0", "green", mini=True)
        self.card_writes = widgets.StatCard("累计写过", "0", "blue", mini=True)
        for c in (self.card_active, self.card_due,
                  self.card_grad, self.card_writes):
            self.header().addWidget(c)

    # ---------------------------------------------------------------- 工具栏
    def _build_toolbar(self) -> None:
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self.search = QLineEdit()
        self.search.setObjectName("TodoSearch")
        self.search.setPlaceholderText("🔍 搜标题 / 标签 / 题号 / 备注")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(140)
        self.search.setMaximumWidth(240)
        # 逐字符重建整张列表的话，300 题实测打一个中文要等 2.0 秒（手比机器快得多）。
        # 停 200ms 再筛，回车立刻筛 —— 和笔记页那套防抖同一个形状。
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._reload_list)
        self.search.textChanged.connect(self._search_timer.start)
        self.search.returnPressed.connect(self._reload_list)
        bar.addWidget(self.search)

        self.tag_filter = widgets.ComboBox()
        self.tag_filter.setMinimumWidth(110)
        self.tag_filter.currentIndexChanged.connect(self._reload_list)
        bar.addWidget(self.tag_filter)

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

        add = QPushButton("＋ 录一道题")
        add.setObjectName("Primary")
        add.setCursor(Qt.PointingHandCursor)
        add.setToolTip("录完会自动排出第一个复习点（明天），并生成一条待办")
        add.clicked.connect(self._add_problem)
        bar.addWidget(add)

        self.body().addLayout(bar)

    def _set_scope(self, archived: bool) -> None:
        self._flush()
        self._show_archived = archived
        self.seg_active.setChecked(not archived)
        self.seg_arch.setChecked(archived)
        self.reload()

    # ------------------------------------------------------------------ 左表
    def _build_left(self) -> QWidget:
        w = QFrame()
        w.setObjectName("Card")
        w.setMinimumWidth(240)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        self.list = widgets.FittingList()
        self.list.setFrameShape(QFrame.NoFrame)
        # 资源管理器那套选择语义：单击、Ctrl 加选、Shift 连选，右键对选中这批生效
        self.list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.currentRowChanged.connect(self._on_row_changed)
        self.list.customContextMenuRequested.connect(self._list_menu)
        self.list.itemSelectionChanged.connect(self._update_hint)
        self.list.itemSelectionChanged.connect(self._update_batch_bar)
        lay.addWidget(self.list, 1)

        # 批量操作条：选中两道以上才出现。和八股页同一套形状 —— 同类实体在两个
        # 姊妹页上的操作路径要一致，别让「归档」一边是按钮、一边只藏在右键里。
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
                 lambda: self._batch_archive(self._selected_ids(), True)),
                ("删除", "删除选中的题及其题解、写作历史与复习待办",
                 lambda: self._batch_delete(self._selected_ids())),
                ("⋯", "更多批量操作（取消归档 / 标签 / 全选）",
                 lambda: self._batch_more(self._selected_ids())),
                ("✕", "取消选择（等同 Esc）", self._clear_selection)):
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

    def _row_widget(self, p: dict, n_writes: int) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)
        title = QLabel(services.algo_display(p))
        f = QFont()
        f.setBold(True)
        title.setFont(f)
        title.setWordWrap(True)
        lay.addWidget(title)
        meta = []
        if p.get("tags"):
            meta.append(p["tags"].replace(",", " · "))
        if n_writes:
            meta.append("写过 %d 次" % n_writes)
        code = self._sol_map.get(p["id"]) or ""
        if code:
            meta.append("代码 " + code)
        if p.get("next_review"):
            meta.append("复习 " + p["next_review"])
        elif int(p.get("stage") or 0) >= services.ALGO_GRADUATED:
            meta.append("已毕业")
        else:
            meta.append("未排复习")
        sub = QLabel("  |  ".join(meta))
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        lay.addWidget(sub)
        return w

    def _reload_list(self) -> None:
        self._flush()
        prev = self._pid
        # 筛一次字不该把视口弹回顶部：clear() 会把滚动条归零，所以先记下位置，
        # 重建完再滚回来（下一帧才滚得动，几何这时才定下来）。
        saved = self.list.verticalScrollBar().value()
        self.list.blockSignals(True)
        self.list.clear()
        rows = services.algo_problem_list(
            archived=1 if self._show_archived else 0,
            keyword=self.search.text().strip(),
            tag=self.tag_filter.currentData() or "")
        counts = _write_counts()
        self._sol_map = services.algo_solution_map()
        today = date.today().isoformat()
        for p in rows:
            item = QListWidgetItem()
            item.setData(_ROLE_PID, p["id"])
            self.list.addItem(item)
            w = self._row_widget(p, counts.get(p["id"], 0))
            self.list.setItemWidget(item, w)
            # 到期/逾期的标成琥珀色，一眼看出今天该动哪几道
            if p.get("next_review") and p["next_review"] <= today:
                item.setForeground(QColor(theme.get("amber")))
        self.list.blockSignals(False)
        # 行高要按视口真实宽度量，窄栏里才会折行而不是被裁掉
        self.list.fit_rows()
        # 选中项没变也要重画详情：复习结论是在待办页写的，这边 _pid 不变，
        # 只靠 currentRowChanged 会让详情停在旧数据上。
        # keep 要在这一下改判 prev 之前算：还在原选中题上才滚回原位置，
        # 换了题（原题被筛掉）就该跟着 _select 走到新题那里。
        keep = prev != 0 and any(r["id"] == prev for r in rows)
        if not rows:
            self._pid = 0
        else:
            if not any(r["id"] == prev for r in rows):
                prev = int(rows[0]["id"])
            self._pid = int(prev)
            self._select(self._pid)
        self._render_detail()
        self._n_rows = len(rows)
        self._update_hint()
        if keep:
            sb = self.list.verticalScrollBar()
            QTimer.singleShot(0, lambda: sb.setValue(saved))

    def _update_hint(self) -> None:
        self.hint.setText("%d 道题%s" % (
            self._n_rows,
            "（已归档）" if self._show_archived else ""))

    def _update_batch_bar(self) -> None:
        n = len(self.list.selectedItems())
        self.batch_lbl.setText("已选 %d 道" % n)
        self.batch.setVisible(n >= 2)

    def _clear_selection(self) -> None:
        self.list.clearSelection()

    def _bind_shortcuts(self) -> None:
        """列表这套快捷键和八股页一一对齐：资源管理器的手感不该两个姊妹页各半套。

        「让位在文本框里」是这条的前提：题解编辑器 / 备注框 / 搜索框正被编辑时，
        Delete 和 Ctrl+A 归那个控件，不然改个字一按 Delete 整批题就被删了。
        """
        for seq, slot in (
                (QKeySequence.SelectAll, self._on_select_all),
                (QKeySequence(Qt.Key_Delete), self._on_delete_key),
                (QKeySequence(Qt.Key_Escape), self._on_esc),
        ):
            sc = QShortcut(seq, self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)

    def _editing_text(self) -> bool:
        return isinstance(self.focusWidget(), (QLineEdit, QPlainTextEdit))

    def _on_select_all(self) -> None:
        if not self._editing_text():
            self.list.selectAll()

    def _on_delete_key(self) -> None:
        if not self._editing_text() and self.list.selectedItems():
            self._batch_delete(self._selected_ids())

    def _on_esc(self) -> None:
        if self._editing_text():
            return
        if self.list.hasFocus() and self.list.selectedItems():
            self._clear_selection()     # 列表里按 Esc = 取消选择

    def focus_item(self, pid: int) -> None:
        """从待办页点复习条目跳进来：先清掉筛选，保证这道题一定在列表里。

        清的是本页真实存在的筛选项（搜索框、标签、归档视图）。
        """
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.tag_filter.blockSignals(True)
        self.tag_filter.setCurrentIndex(0)
        self.tag_filter.blockSignals(False)
        self._show_archived = False
        self.seg_active.setChecked(True)
        self.seg_arch.setChecked(False)
        self._pid = int(pid)
        self.reload()
        self._select(self._pid)

    def _select(self, pid: int) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(_ROLE_PID) == pid:
                self.list.setCurrentRow(i)
                return

    def _on_row_changed(self, row: int) -> None:
        if row < 0 or row >= self.list.count():
            return
        pid = int(self.list.item(row).data(_ROLE_PID))
        if pid == self._pid:
            return
        self._flush()
        self._pid = pid
        self._render_detail()

    # ---------------------------------------------------------------- 右详情
    def _build_right(self) -> QWidget:
        holder = QWidget()
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.empty = QLabel("左边挑一道题，或点「＋ 录一道题」")
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
        lay.setSpacing(10)
        scroll.setWidget(card)
        outer.addWidget(scroll, 1)

        self.title = QLineEdit()
        self.title.setObjectName("DetailTitleInput")
        self.title.setPlaceholderText("题目标题（可留空，只填题号也行）")
        self.title.editingFinished.connect(self._save_fields)
        lay.addWidget(self.title)

        tag_row = QHBoxLayout()
        tag_row.setSpacing(8)
        tag_row.addWidget(self._label("标签"))
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("逗号分隔，中英文都行，如 二叉树，递归")
        self.tags.editingFinished.connect(self._save_fields)
        tag_row.addWidget(self.tags, 1)
        lay.addLayout(tag_row)

        lc_row = QHBoxLayout()
        lc_row.setSpacing(8)
        lc_row.addWidget(self._label("力扣"))
        self.lc_ref = QLineEdit()
        self.lc_ref.setPlaceholderText("题号或 slug，如 15 / three-sum")
        self.lc_ref.setMaximumWidth(180)
        self.lc_ref.editingFinished.connect(self._save_fields)
        lc_row.addWidget(self.lc_ref)
        self.url = QLineEdit()
        self.url.setPlaceholderText("题目链接（只填题号会自动拼）")
        self.url.editingFinished.connect(self._save_fields)
        lc_row.addWidget(self.url, 1)
        self.open_btn = QPushButton("打开题目")
        self.open_btn.setObjectName("Ghost")
        self.open_btn.setCursor(Qt.PointingHandCursor)
        self.open_btn.clicked.connect(self._open_problem)
        lc_row.addWidget(self.open_btn)
        lay.addLayout(lc_row)

        self.seps = [widgets.hline(), widgets.hline(), widgets.hline()]
        lay.addWidget(self.seps[0])

        # ---- 复习排期
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

        log_row = QHBoxLayout()
        log_row.setSpacing(8)
        self.log_btn = QPushButton("✓ 记一次写过")
        self.log_btn.setObjectName("Primary")
        self.log_btn.setCursor(Qt.PointingHandCursor)
        self.log_btn.setToolTip("记进写作历史，并按「是否独立做出来」重排下一个记忆点")
        # 不能直接 connect：clicked 会把 checked 布尔当第一个位置参数塞进来，
        # 正好落进 _log_write 的 pid 上
        self.log_btn.clicked.connect(lambda: self._log_write())
        log_row.addWidget(self.log_btn)
        self.writes_lbl = QLabel("")
        self.writes_lbl.setObjectName("Muted")
        self.writes_lbl.setWordWrap(True)
        log_row.addWidget(self.writes_lbl, 1)
        lay.addLayout(log_row)

        self.timeline = QVBoxLayout()
        self.timeline.setSpacing(2)
        lay.addLayout(self.timeline)

        lay.addWidget(self.seps[1])

        # ---- 题解 / 解题代码
        sol_head = QHBoxLayout()
        sol_head.setSpacing(8)
        sol_head.addWidget(self._label("题解与代码"))
        sol_head.addStretch(1)
        self.sol_sum = QLabel("")
        self.sol_sum.setObjectName("Muted")
        sol_head.addWidget(self.sol_sum)
        lay.addLayout(sol_head)
        # 一版解法 = 一个语言标签，加一版只多一个标签，不再往下堆卡片
        self.sol_tabs = solution_tabs.SolutionTabs(
            caption="题解", show_source=True,
            on_save=lambda sid, **f: services.algo_solution_update(sid, **f),
            on_delete=services.algo_solution_delete,
            on_add=self._add_solution,
            on_touch=self.mark_dirty,
            on_commit=self.reload)
        lay.addWidget(self.sol_tabs)

        lay.addWidget(self.seps[2])

        lay.addWidget(self._label("备注"))
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("思路：怎么想到的、复杂度、有什么坑；卡在哪、下次注意什么…")
        self.note.setMinimumHeight(72)
        self.note.textChanged.connect(self.mark_dirty)
        lay.addWidget(self.note)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.arch_btn = QPushButton("归档")
        self.arch_btn.setObjectName("Ghost")
        self.arch_btn.setCursor(Qt.PointingHandCursor)
        self.arch_btn.setToolTip("归档 = 不再排复习，但记录全部留着")
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

        # 内容控件集中登记，没选题时整块隐藏
        self._detail_widgets = [
            self.title, self.tags, self.lc_ref, self.url, self.open_btn,
            self.review_date, self.review_lbl, self.log_btn, self.writes_lbl,
            self.sol_tabs, self.sol_sum, self.note,
            self.arch_btn, self.del_btn, *self.seps,
        ]
        scroll.hide()
        self._scroll = scroll
        return holder

    def _label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("CardTitle")
        return lbl

    # ------------------------------------------------------------------ 数据
    def reload(self) -> None:
        self._refresh_tags()
        self._reload_list()
        st = services.algo_stats()
        self.card_active.set_value(str(st["active"]))
        self.card_due.set_value(str(st["due"]))
        self.card_grad.set_value(str(st["graduated"]))
        self.card_writes.set_value(str(st["writes"]))

    def _refresh_tags(self) -> None:
        want = self.tag_filter.currentData() or ""
        self.tag_filter.blockSignals(True)
        self.tag_filter.clear()
        self.tag_filter.addItem("全部标签", "")
        for t in services.algo_tags():
            self.tag_filter.addItem(t, t)
        self.tag_filter.setCurrentIndex(max(self.tag_filter.findData(want), 0))
        self.tag_filter.blockSignals(False)

    def _render_detail(self) -> None:
        p = services.algo_problem_get(self._pid) if self._pid else {}
        has = bool(p)
        for w in self._detail_widgets:
            w.setVisible(has)
        self.empty.setVisible(not has)
        self._scroll.setVisible(has)
        self._editor_pid = self._pid if has else 0
        if not has:
            return

        self._loading = True
        self.title.setText(p["title"])
        self.tags.setText(p.get("tags") or "")
        self.lc_ref.setText(p.get("lc_ref") or "")
        self.url.setText(p.get("url") or "")
        self.note.setPlainText(p.get("note") or "")
        d = p.get("next_review") or ""
        self.review_date.setDate(
            QDate.fromString(d, "yyyy-MM-dd") if d else QDate())
        self.review_lbl.setText(self._review_text(p))
        self.log_btn.setText("✓ 记一次写过" if int(p.get("solved") or 0)
                             else "✓ 第一次写这题")
        self.arch_btn.setText("取消归档" if p.get("archived") else "归档")

        writes = services.algo_write_list(p["id"])
        self.writes_lbl.setText(
            "写过 %d 次%s" % (len(writes),
                             "，最近 " + writes[0]["written_at"] if writes else ""))
        _clear_layout(self.timeline)
        for w in writes[:6]:
            lbl = QLabel("· %s · %s" % (
                w["written_at"],
                "独立做出来" if int(w.get("independent") or 0) else "没独立做出来"))
            lbl.setObjectName("Muted")
            self.timeline.addWidget(lbl)

        sols = services.algo_solution_list(p["id"])
        self.sol_tabs.set_solutions(sols, select_id=self._select_sol)
        self._select_sol = 0
        self.sol_sum.setText(solution_tabs.summary(sols))
        self._loading = False

    def _review_text(self, p: dict) -> str:
        stage = int(p.get("stage") or 0)
        nxt = p.get("next_review") or ""
        if not nxt:
            if stage >= services.ALGO_GRADUATED:
                return "已越过最后一档（30 天），不再排复习"
            return "没排复习 —— 在左边填个日期就能排上"
        try:
            d = date.fromisoformat(nxt)
        except ValueError:
            return "下次复习 %s" % nxt
        delta = (d - date.today()).days
        when = ("就是今天" if delta == 0 else
                "逾期 %d 天" % -delta if delta < 0 else "还有 %d 天" % delta)
        gap = (services.ALGO_INTERVALS[stage]
               if stage < len(services.ALGO_INTERVALS) else 0)
        return ("第 %d 档 · 间隔 %d 天 · %s（%s）· 已进待办「算法复习」"
                % (stage + 1, gap, nxt, when))

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
        self._save_timer.stop()
        if not self._pid or self._loading:
            return
        # 编辑器里显示的还是别的题时绝不能写：刚录完一道新题、面板还没换
        # 过去的时候落盘，会把上一题的备注和题解原样盖到新题上（真丢数据）。
        if self._editor_pid != self._pid:
            self._render_detail()
            return
        p = services.algo_problem_get(self._pid)
        if not p:
            return
        note = self.note.toPlainText()
        if (p.get("note") or "") != note:
            services.algo_problem_update(self._pid, note=note)
        self.sol_tabs.flush()

    def _save_fields(self) -> None:
        """单行字段失焦即落库；只填了题号就把链接拼出来。"""
        if self._loading or not self._pid:
            return
        title = self.title.text().strip()
        ref = self.lc_ref.text().strip()
        url = self.url.text().strip()
        if not title and not ref and not url:
            # 标题和题号不能同时空着，否则这道题在列表里认不出来
            self.title.setText(services.algo_problem_get(self._pid)["title"])
            return
        services.algo_problem_update(
            self._pid, title=title, tags=self.tags.text().strip(),
            lc_ref=ref, url=url)
        self._reload_list()      # 末尾会重画详情，拼出来的链接从库里回读

    def _save_review_date(self) -> None:
        if self._loading or not self._pid:
            return
        d = self.review_date.date()
        services.algo_set_next_review(
            self._pid, d.toString("yyyy-MM-dd") if d.isValid() else "")
        self.reload()

    # ------------------------------------------------------------------ 动作
    def _add_problem(self) -> None:
        """录一道题。标题和题号至少给一个，题解/代码到右侧面板里写。"""
        self._flush()
        title, ok = popups.ask_text(self, "录一道题",
                                    "题目标题（可留空，只填题号也行）")
        if not ok:
            return
        ref, ok2 = popups.ask_text(self, "录一道题",
                                   "力扣题号或 slug（可留空）")
        if not ok2:
            return
        if not title.strip() and not ref.strip():
            popups.notify(self, "至少给一个",
                          "标题和力扣题号不能都空着，否则这道题没法认出来。")
            return
        pid = services.algo_problem_add(title, "", ref)
        if not pid:
            return
        self._pid = pid
        self.reload()
        popups.notify(
            self, "已录入",
            "第一个复习点排在 %s，待办「算法复习」清单里已经挂着这条了。\n"
            "题解、思路、代码直接在右侧面板里写，停手就自动存。"
            % services.algo_problem_get(pid)["next_review"])

    def _log_write(self, pid: int = 0) -> None:
        pid = pid or self._pid
        if not pid:
            return
        opts = ["独立做出来了", "没独立做出来"]
        pick, ok = popups.get_item(self, "复习结论",
                                   "这次是独立做出来的吗？", opts)
        if not ok:
            return
        services.algo_log_write(pid, independent=(pick == opts[0]))
        sounds.play("answer_correct" if pick == opts[0] else "answer_wrong")
        self.reload()

    def _add_solution(self) -> None:
        """多加一个语言标签，光标直接落在新标签的代码块里。"""
        if not self._pid:
            return
        self._flush()
        sid = services.algo_solution_add(self._pid, allow_empty=True)
        if sid:
            self._select_sol = sid
            self._render_detail()   # 只重画右侧，别把左侧列表也刷没了
            self.sol_tabs.code.setFocus()

    def _toggle_archive(self) -> None:
        if not self._pid:
            return
        self._flush()
        p = services.algo_problem_get(self._pid)
        on = not p.get("archived")
        services.algo_problem_set_archived(self._pid, on)
        # 跟着这道题切到它新所在的视图：不然点完归档它当场从列表里消失，
        # 选中项跳到别的题上，看着像把别的题归档了。
        self._show_archived = on
        self.seg_active.setChecked(not on)
        self.seg_arch.setChecked(on)
        self.reload()

    def _delete_problem(self) -> None:
        if not self._pid:
            return
        p = services.algo_problem_get(self._pid)
        if not popups.confirm(self, "删除这道题",
                              "「%s」的题解、写作历史和复习待办会一起删掉。"
                              % services.algo_display(p)):
            return
        services.algo_problem_delete(self._pid)
        self._pid = 0
        self.reload()

    def _open_problem(self) -> None:
        self._open_one(self._pid)

    def _open_one(self, pid: int) -> None:
        p = services.algo_problem_get(pid) if pid else None
        url = (p or {}).get("url") or ""
        if not url:
            popups.notify(self, "还没有链接",
                          "填了力扣题号或链接就能跳浏览器。")
            return
        if not QDesktopServices.openUrl(QUrl(url)):
            popups.notify(self, "打不开链接",
                          "系统浏览器没能打开：\n%s" % url, danger=True)

    # ------------------------------------------------------ 多选 / 右键菜单
    def _selected_ids(self) -> list[int]:
        return [int(it.data(_ROLE_PID)) for it in self.list.selectedItems()]

    def _after_batch(self) -> None:
        """批量操作后统一收尾：清掉选择再重刷，别让选中态指向已消失的行。"""
        self.list.clearSelection()
        self.reload()

    def _list_menu(self, pos) -> None:
        """右键：落在没选中的行上时先只选它（资源管理器行为），再对这批生效。"""
        it = self.list.itemAt(pos)
        if it is not None and not it.isSelected():
            self.list.setCurrentItem(it)
        ids = self._selected_ids()
        if ids:
            self._batch_more(ids)

    def _batch_more(self, ids: list[int]) -> None:
        one = len(ids) == 1
        del_act = "删除这道题" if one else "删除这 %d 道题" % len(ids)
        acts = (["打开题目", "记一次写过"] if one else []) + [
            "归档", "取消归档", "加标签", "移除标签", del_act, "全选"]
        head = (services.algo_display(services.algo_problem_get(ids[0]) or {})
                if one else "对选中的 %d 道题做什么？" % len(ids))
        pick, ok = popups.get_item(self, "这道题" if one else "批量操作",
                                   head, acts)
        if not ok:
            return
        if pick == "打开题目":
            self._open_one(ids[0])
        elif pick == "记一次写过":
            self._log_write(ids[0])
        elif pick == "归档":
            self._batch_archive(ids, True)
        elif pick == "取消归档":
            self._batch_archive(ids, False)
        elif pick == "加标签":
            self._batch_add_tags(ids)
        elif pick == "移除标签":
            self._batch_remove_tags(ids)
        elif pick == "全选":
            self.list.selectAll()
        elif pick == del_act:
            self._batch_delete(ids)

    def _batch_archive(self, ids: list[int], on: bool) -> None:
        self._flush()
        for pid in ids:
            services.algo_problem_set_archived(pid, on)
        # 跟到这批题新所在的视图，否则点完归档它们当场从列表里消失，
        # 看着像没生效
        self._show_archived = on
        self.seg_active.setChecked(not on)
        self.seg_arch.setChecked(on)
        self._after_batch()
        popups.notify(self, "已归档" if on else "已取消归档",
                      "处理了 %d 道题。" % len(ids))

    def _batch_delete(self, ids: list[int]) -> None:
        self._flush()
        names = "、".join(
            services.algo_display(services.algo_problem_get(pid) or {})
            for pid in ids[:3])
        if not popups.confirm(
                self, "删除 %d 道题" % len(ids),
                "「%s%s」的题解、写作历史和复习待办会一起删掉，找不回来。"
                % (names, "…" if len(ids) > 3 else "")):
            return
        for pid in ids:
            services.algo_problem_delete(pid)
        self._pid = 0
        self._after_batch()
        popups.notify(self, "已删除", "删掉了 %d 道题。" % len(ids))

    def _batch_add_tags(self, ids: list[int]) -> None:
        raw, ok = popups.ask_text(self, "加标签",
                                  "要加的标签（多个用逗号分隔，中英文都行）")
        if not ok or not raw.strip():
            return
        add = services.split_tags(raw)
        for pid in ids:
            cur = services.split_tags(
                (services.algo_problem_get(pid) or {}).get("tags"))
            merged = cur + [t for t in add if t not in cur]
            services.algo_problem_update(pid, tags=",".join(merged))
        self._after_batch()
        popups.notify(self, "已加标签",
                      "给 %d 道题加了「%s」。" % (len(ids), "、".join(add)))

    def _batch_remove_tags(self, ids: list[int]) -> None:
        # 只列选中题身上真实存在的标签，点了才发现「没这个标签」很烦
        present: list[str] = []
        for pid in ids:
            for t in services.split_tags(
                    (services.algo_problem_get(pid) or {}).get("tags")):
                if t not in present:
                    present.append(t)
        if not present:
            popups.notify(self, "没有可移除的标签", "选中的题都没打标签。")
            return
        tag, ok = popups.get_item(self, "移除标签", "移除哪个标签", present)
        if not ok:
            return
        n = 0
        for pid in ids:
            cur = services.split_tags(
                (services.algo_problem_get(pid) or {}).get("tags"))
            left = [t for t in cur if t != tag]
            if len(left) != len(cur):
                services.algo_problem_update(pid, tags=",".join(left))
                n += 1
        self._after_batch()
        popups.notify(self, "已移除标签", "从 %d 道题上移除了「%s」。" % (n, tag))

    def _on_theme_changed(self) -> None:
        self._flush()
        # 高亮色是建规则时算死的，换肤后要按新主题重建一遍才会跟着变
        self.sol_tabs.refresh_theme()
        self.reload()


def _clear_layout(lay) -> None:
    while lay.count():
        it = lay.takeAt(0)
        if it.widget():
            it.widget().deleteLater()


def _write_counts() -> dict:
    """一次查出每题写过几次。逐题单查会在列表里做出 N+1。"""
    with services.db.connect() as conn:
        rows = conn.execute(
            "SELECT problem_id, COUNT(*) AS c FROM algo_writes "
            "GROUP BY problem_id").fetchall()
    return {r["problem_id"]: r["c"] for r in rows}
