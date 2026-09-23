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
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QPlainTextEdit, QScrollArea, QFrame,
    QSplitter,
)

from .. import popups, services, solution_card, sounds, theme, widgets
from .base import Page, stats_row

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
        self._editor_pid = 0
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
        self.card_active = widgets.StatCard("在刷", "0", "accent")
        self.card_due = widgets.StatCard("到期待复习", "0", "amber")
        self.card_grad = widgets.StatCard("已毕业", "0", "green")
        self.card_writes = widgets.StatCard("累计写过", "0", "blue")
        self.body().addLayout(stats_row(
            [self.card_active, self.card_due, self.card_grad, self.card_writes]))

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
        self.search.textChanged.connect(self._reload_list)
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

        self.list = QListWidget()
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(self._on_row_changed)
        lay.addWidget(self.list, 1)

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
            # setItemWidget 不会自己撑行高，不显式给 sizeHint 的话整行被压成
            # 一条窄带，标题和副信息全被裁掉看不见。
            item.setSizeHint(w.sizeHint())
            # 到期/逾期的标成琥珀色，一眼看出今天该动哪几道
            if p.get("next_review") and p["next_review"] <= today:
                item.setForeground(QColor(theme.get("amber")))
        self.list.blockSignals(False)
        # 选中项没变也要重画详情：复习结论是在待办页写的，这边 _pid 不变，
        # 只靠 currentRowChanged 会让详情停在旧数据上。
        if not rows:
            self._pid = 0
        else:
            if not any(r["id"] == prev for r in rows):
                prev = int(rows[0]["id"])
            self._pid = int(prev)
            self._select(self._pid)
        self._render_detail()
        self.hint.setText("%d 道题%s" % (
            len(rows), "（已归档）" if self._show_archived else ""))

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
        self.tags.setPlaceholderText("逗号分隔，如 二叉树,递归")
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
        self.log_btn.clicked.connect(self._log_write)
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
        self.add_sol_btn = QPushButton("＋ 加一版解法")
        self.add_sol_btn.setObjectName("Ghost")
        self.add_sol_btn.setCursor(Qt.PointingHandCursor)
        self.add_sol_btn.setToolTip("默认 C++；同一语言可以再存几版（暴力 / 最优）")
        self.add_sol_btn.clicked.connect(self._add_solution)
        sol_head.addWidget(self.add_sol_btn)
        lay.addLayout(sol_head)
        self.sol_box = QVBoxLayout()
        self.sol_box.setSpacing(8)
        lay.addLayout(self.sol_box)
        self.no_sol = QLabel("还没写解法")
        self.no_sol.setObjectName("Muted")
        lay.addWidget(self.no_sol)

        lay.addWidget(self.seps[2])

        lay.addWidget(self._label("备注"))
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("卡在哪、下次注意什么…")
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
            self.add_sol_btn, self.sol_sum, self.no_sol, self.note,
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

        _clear_layout(self.sol_box)
        sols = services.algo_solution_list(p["id"])
        for s in sols:
            self.sol_box.addWidget(self._new_card(s))
        self.no_sol.setVisible(not sols)
        self.sol_sum.setText(solution_card.summary(sols))
        self._loading = False

    def _new_card(self, sol: dict) -> solution_card.SolutionCard:
        return solution_card.SolutionCard(
            sol, caption="题解", show_source=True,
            on_save=lambda sid, **f: services.algo_solution_update(sid, **f),
            on_delete=services.algo_solution_delete,
            on_touch=self.mark_dirty,
            on_commit=self.reload)

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
        for card in self._solution_cards():
            card.flush()

    def _solution_cards(self) -> list[solution_card.SolutionCard]:
        return [self.sol_box.itemAt(i).widget()
                for i in range(self.sol_box.count())
                if isinstance(self.sol_box.itemAt(i).widget(),
                              solution_card.SolutionCard)]

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

    def _log_write(self) -> None:
        if not self._pid:
            return
        opts = ["独立做出来了", "没独立做出来"]
        pick, ok = popups.get_item(self, "复习结论",
                                   "这次是独立做出来的吗？", opts)
        if not ok:
            return
        services.algo_log_write(self._pid, independent=(pick == opts[0]))
        sounds.play("answer_correct" if pick == opts[0] else "answer_wrong")
        self.reload()

    def _add_solution(self) -> None:
        """先长一张空卡再往里写：默认 C++，停手自动存，比弹框舒服。"""
        if not self._pid:
            return
        self._flush()
        sid = services.algo_solution_add(
            self._pid, allow_empty=True)
        if sid:
            self._render_detail()   # 只重画右侧，别把左侧列表也刷没了

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
                              % p["title"]):
            return
        services.algo_problem_delete(self._pid)
        self._pid = 0
        self.reload()

    def _open_problem(self) -> None:
        p = services.algo_problem_get(self._pid) if self._pid else None
        url = (p or {}).get("url") or ""
        if not url:
            popups.notify(self, "还没有链接",
                          "填了力扣题号或链接就能跳浏览器。")
            return
        if not QDesktopServices.openUrl(QUrl(url)):
            popups.notify(self, "打不开链接",
                          "系统浏览器没能打开：\n%s" % url, danger=True)

    def _on_theme_changed(self) -> None:
        self._flush()
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
