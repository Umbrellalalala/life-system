"""专注统计页：概览 / 任务 / 专注 三个标签页。

被 PomodoroPage 以覆盖层方式弹出（点「⋯ → 统计」或顶部标题栏的统计入口），
覆盖整个专注页区域，点「完成」返回。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea, QGridLayout, QStackedWidget, QSizePolicy,
)

from .. import services, widgets, focus_ui, theme, db
from ..focus_ui import fmt_rec

WD_LABELS = ["一", "二", "三", "四", "五", "六", "日"]
TIME_SLOTS = [("凌晨", 0, 6), ("上午", 6, 12), ("下午", 12, 18), ("晚上", 18, 24)]
# 参考图实测：整页内容被限制在一列里居中（两张半栏卡 472 + 15 间距 = 960 逻辑像素），
# 两侧留大片空白，而不是把卡片拉满窗口宽度。
STATS_COL = 960


class _CenterCol(QWidget):
    """把内容限宽到 ``STATS_COL`` 并水平居中：宽窗口两侧留白，窄窗口铺满。

    两侧不能用 ``addStretch``：弹簧会按比例抢走多余空间，窗口一窄（小于
    960+边距）内层就只拿到自己 sizeHint 那么宽 —— 实测 1000 宽的窗口里内容列
    只有 546，卡片凭空白白挤成窄条。改成 resize 时直接算左右边距。
    """

    def __init__(self, inner: QWidget, max_w: int = STATS_COL,
                 side: int = 28, parent: QWidget | None = None):
        super().__init__(parent)
        self._max = max_w
        self._side = side
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(side, 0, side, 0)
        inner.setMaximumWidth(max_w)
        inner.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.lay.addWidget(inner)

    def resizeEvent(self, event) -> None:  # noqa: N802
        m = max(self._side, (self.width() - self._max) // 2)
        if self.lay.contentsMargins().left() != m:
            self.lay.setContentsMargins(m, 0, m, 0)
        super().resizeEvent(event)


def _delta(diff: float, unit: str, fmt=fmt_rec,
           suffix: str = "") -> tuple[str, str]:
    """返回 ``(文案, 趋势)``，趋势取 up / down / flat。

    箭头由调用方单独着色（绿色↑ / 红色↓），所以文案里不带箭头。
    ``fmt`` 必须显式传 —— 默认的 ``fmt_rec`` 是**时长**格式化器，
    直接拿它格式化「3 个任务」会得到 "3m"。
    """
    if abs(diff) < 1e-6:
        return f"与{unit}持平", "flat"
    txt = f"{fmt(abs(diff))}{suffix}"
    if diff > 0:
        return f"比{unit}多{txt}", "up"
    return f"比{unit}少{txt}", "down"


def _count(v: float) -> str:
    return str(int(round(v)))


def _percent(v: float) -> str:
    return f"{v:.0f}"


class StatsView(QWidget):
    """专注统计覆盖页。"""

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        # 这一页是盖在专注主页之上的覆盖层，底色必须跟主页一致（主页已改成
        # bg_alt 白，主题里 #FocusPage 仍是蓝灰的 @bg@，两层叠在一起能看出色差）。
        # 换名 + 内联样式是为了不动全局 theme.py。
        self.setObjectName("FocusStatsPage")
        self.setAttribute(Qt.WA_StyledBackground, True)

        # 时间范围状态
        self._range_mode = "week"        # week | month | year
        self._offset = 0                 # 0 = 当前周期
        self._detail_group = 0           # 0 按任务 / 1 按时段
        self._month_offset = 0
        self._year_offset = 0
        # 「任务」标签有独立的时间粒度（按日 / 按周 / 按月），不复用上面的
        # _range_mode —— 那个是「总览 / 专注」用的周/月/年，两套语义不同，
        # 混用会让切换任务标签的粒度时把另外两个标签的周期也改掉。
        self._tk_mode = "day"
        self._tk_offset = 0              # 0 = 当前周期，负数为往前翻
        self._tk_group = 0               # 0 按清单 / 1 按标签 / 2 按优先级

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QGridLayout()
        head.setContentsMargins(0, 20, 0, 6)
        head.setSpacing(8)
        title = QLabel("统计")
        title.setObjectName("FocusStatsTitle")
        head.addWidget(title, 0, 0, Qt.AlignLeft | Qt.AlignVCenter)

        self.tabs = focus_ui.SegmentedControl(["总览", "任务", "专注"], "track", 32)
        # 上次停在哪个页签，这次就从哪个开始：每次都跳回「专注」，等于把
        # 用户刚在看的那一签忘掉。
        self._start_tab = self._saved_tab()
        self.tabs.set_index(self._start_tab)
        head.addWidget(self.tabs, 0, 1, Qt.AlignCenter)

        right_box = QWidget()
        rb = QHBoxLayout(right_box)
        rb.setContentsMargins(0, 0, 0, 0)
        rb.addStretch(1)
        done = QPushButton("完成")
        done.setObjectName("FocusPrimary")
        done.setCursor(Qt.PointingHandCursor)
        done.clicked.connect(self.close)
        rb.addWidget(done)
        head.addWidget(right_box, 0, 2)
        head.setColumnStretch(0, 1)
        head.setColumnStretch(2, 1)
        head_w = QWidget()
        head_w.setLayout(head)
        root.addWidget(self._column(head_w))

        self.stack = QStackedWidget()
        # 三个签全建一遍实测 157ms，而打开统计页只会看到其中一个 —— 另外两个
        # 是白等的头两帧。先各挂一个空壳，第一次真去看它时才建它。
        self._tab_builders = [self._build_overview_tab,
                              self._build_task_tab,
                              self._build_focus_tab]
        self._tab_built = [False] * len(self._tab_builders)
        for _ in self._tab_builders:
            self.stack.addWidget(QWidget())
        root.addWidget(self.stack, 1)
        self.stack.setCurrentIndex(self._start_tab)
        self._ensure_tab(self._start_tab)
        self.tabs.changed.connect(self._on_tab)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.close)

    def _ensure_tab(self, idx: int) -> None:
        """把第 idx 个页签真正建出来（建过的直接返回）。"""
        if self._tab_built[idx]:
            return
        self._tab_built[idx] = True          # 先置位，别让它回头再触发自己
        host = self._tab_builders[idx]()
        old = self.stack.widget(idx)
        self.stack.removeWidget(old)
        old.deleteLater()
        self.stack.insertWidget(idx, host)
        self.stack.setCurrentIndex(idx)      # 摘占位时当前索引会漂

    @staticmethod
    def _saved_tab() -> int:
        try:
            idx = int(db.get_setting("focus_stats_tab", "2"))
        except ValueError:
            idx = 2
        return max(0, min(idx, 2))

    # ------------------------------------------------------------------
    # 通用小部件
    # ------------------------------------------------------------------
    @staticmethod
    def _column(inner: QWidget) -> QWidget:
        return _CenterCol(inner)

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout, QHBoxLayout]:
        card = QFrame()
        card.setObjectName("FocusCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(4)
        lbl = QLabel(title)
        lbl.setObjectName("FocusCardTitle")
        head.addWidget(lbl)
        head.addStretch(1)
        lay.addLayout(head)
        return card, lay, head

    @staticmethod
    def _nav(on_prev, on_next) -> tuple[QWidget, QLabel, QPushButton, QPushButton]:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        prev = QPushButton("‹")
        nxt = QPushButton("›")
        for b in (prev, nxt):
            b.setObjectName("FocusLink")
            b.setFixedSize(22, 24)
            b.setCursor(Qt.PointingHandCursor)
        lbl = QLabel("本周")
        lbl.setObjectName("FocusNavText")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setMinimumWidth(66)
        prev.clicked.connect(on_prev)
        nxt.clicked.connect(on_next)
        h.addWidget(prev)
        h.addWidget(lbl)
        h.addWidget(nxt)
        return w, lbl, prev, nxt

    @staticmethod
    def _scroll(content: QWidget) -> QScrollArea:
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 滚动区里放的是居中容器，不是内容本身：内容那一列要限宽
        sc.setWidget(StatsView._column(content))
        return sc

    @staticmethod
    def _hint(text: str) -> QLabel:
        lbl = QLabel(f"ⓘ {text}")
        lbl.setObjectName("FocusCardHint")
        return lbl

    # ------------------------------------------------------------------
    # 时间范围
    # ------------------------------------------------------------------
    # 所有 ``_offset`` 一律**正数 = 更早**，和 ‹ / › 的方向一致。
    # 原来 day/week 分支按「正数=更早」算、month/year 分支按「正数=更晚」算，
    # 于是同一个 ‹ 按钮在周粒度下翻到**下一周**（空白）去了 —— 实测三处反了：
    # 总览/专注的周、任务页的日与周。
    @staticmethod
    def _shift_month(y: int, m: int, delta: int) -> tuple[int, int]:
        m = m + delta
        return y + (m - 1) // 12, (m - 1) % 12 + 1

    def _range(self) -> tuple[date, date, str]:
        today = date.today()
        if self._range_mode == "day":
            d = today - timedelta(days=self._offset)
            label = ("今天" if self._offset == 0 else
                     "昨天" if self._offset == 1 else f"{d.month}月{d.day}日")
            return d, d, label
        if self._range_mode == "week":
            base = today - timedelta(days=self._offset * 7)
            start = base - timedelta(days=base.weekday())
            end = start + timedelta(days=6)
            label = "本周" if self._offset == 0 else f"{start.month}月{start.day}日"
        elif self._range_mode == "month":
            y, m = self._shift_month(today.year, today.month, -self._offset)
            start = date(y, m, 1)
            nxt = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
            end = nxt - timedelta(days=1)
            label = f"{y}年{m}月"
        else:
            y = today.year - self._offset
            start, end = date(y, 1, 1), date(y, 12, 31)
            label = f"{y}年"
        return start, end, label

    def _month_range(self) -> tuple[date, date, str]:
        today = date.today()
        y, m = self._shift_month(today.year, today.month, -self._month_offset)
        start = date(y, m, 1)
        nxt = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
        return start, nxt - timedelta(days=1), f"{m}月"

    def _year_range(self) -> tuple[date, date, str]:
        y = date.today().year - self._year_offset
        return date(y, 1, 1), date(y, 12, 31), str(y)

    # ------------------------------------------------------------------
    # 标签页 1：总览
    # ------------------------------------------------------------------
    OV_GRAINS = ("day", "week", "month")
    # 顶部汇总条的四项，和参考图同序：任务 / 已完成 / 清单 / 使用天数
    OV_UNITS = ("任务", "已完成", "清单", "使用天数")

    def _build_overview_tab(self) -> QWidget:
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 10, 0, 24)
        lay.setSpacing(14)

        # ---- 顶部汇总条 ----
        band = QFrame()
        band.setObjectName("FocusCard")
        blow = QHBoxLayout(band)
        blow.setContentsMargins(18, 20, 18, 20)
        blow.setSpacing(0)
        self.ov_band: list[tuple[QLabel, QLabel]] = []
        for unit in self.OV_UNITS:
            num = QLabel("0")
            txt = QLabel(unit)
            blow.addWidget(num)
            blow.addSpacing(5)
            blow.addWidget(txt)
            blow.addSpacing(22)
            self.ov_band.append((num, txt))
        blow.addStretch(1)
        self.ov_band_note = QLabel("")
        self.ov_band_note.setTextFormat(Qt.RichText)
        blow.addWidget(self.ov_band_note)
        lay.addWidget(band)

        grid = QGridLayout()
        grid.setSpacing(16)

        # ---- 概览：2 行 3 列，今日三项在上、总量三项在下 ----
        kpi, klay, _ = self._card("概览")
        kgrid = QGridLayout()
        kgrid.setHorizontalSpacing(10)
        # 参考图两行 KPI 的中心相距 90 逻辑像素；挤在 10 的间距里，卡片上半
        # 会空出一大片，看着像没排完。
        kgrid.setVerticalSpacing(26)
        self.ov_kpi = []
        for i, label in enumerate(("今日已完成", "今日番茄", "今日专注时长",
                                   "总已完成", "总番茄", "总专注时长")):
            cell = focus_ui.KpiCell(label, value_first=True)
            cell.setMinimumHeight(80)
            self.ov_kpi.append(cell)
            kgrid.addWidget(cell, i // 3, i % 3)
        klay.addStretch(1)
        klay.addLayout(kgrid)
        klay.addStretch(1)
        grid.addWidget(kpi, 0, 0)

        # ---- 我的成就值：可核对的加权累计（完成任务×10 + 番茄×5），
        # 不是滴答那个不公开公式的分；迷你线画最近 7 天的累计走势。
        ach, alay, ahead = self._card("我的成就值")
        self.ov_total = QLabel("0")
        ahead.addWidget(self.ov_total)
        hint = QLabel("完成任务 ×10 + 番茄 ×5")
        hint.setObjectName("FocusCardHint")
        alay.addWidget(hint)
        self.ov_spark = focus_ui.MiniTrend("line", "score", zoom=True)
        self.ov_spark.setMinimumHeight(247)
        alay.addWidget(self.ov_spark)
        grid.addWidget(ach, 0, 1)

        # ---- 四张趋势卡：左列折线、右列带轨道的柱 ----
        self.ov_charts: dict[str, tuple[focus_ui.MiniTrend, widgets.ComboBox]] = {}
        self._ov_grain = {k: "day" for k in ("done", "rate", "tomato", "focus")}
        specs = (("done", "最近已完成趋势", "line", "count"),
                 ("rate", "最近完成率趋势", "bar", "percent"),
                 ("tomato", "最近番茄数趋势", "line", "count"),
                 ("focus", "最近专注时长趋势", "bar", "minutes"))
        for i, (key, title, style, unit) in enumerate(specs):
            card, clay, chead = self._card(title)
            combo = widgets.ComboBox()
            combo.setObjectName("FocusPillCombo")
            combo.addItems(["日", "周", "月"])
            combo.setFixedSize(62, 28)
            combo.setCursor(Qt.PointingHandCursor)
            combo.currentIndexChanged.connect(
                lambda _idx, k=key: self._on_ov_grain(k))
            chead.addWidget(combo)
            chart = focus_ui.MiniTrend(style, unit)
            chart.setMinimumHeight(250)
            clay.addWidget(chart)
            grid.addWidget(card, 1 + i // 2, i % 2)
            self.ov_charts[key] = (chart, combo)

        # ---- 本周打卡进展 ----
        wk, wlay, _ = self._card("本周打卡进展")
        self.ov_rings = focus_ui.WeekRings()
        wlay.addWidget(self.ov_rings)
        grid.addWidget(wk, 3, 0)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        lay.addStretch(1)
        return self._scroll(content)

    # -- 总览的数据装配 --------------------------------------------------
    def _ov_buckets(self, mode: str) -> list[tuple[str, date, date]]:
        """把「最近」按日/周/月切成若干桶，返回 (标签, 起, 止)。

        日 = 最近 7 天（最后一格写「今天」，和参考图一致）；
        周 = 最近 8 个自然周（标签取周一起始日）；月 = 最近 6 个自然月。
        未来的天不生成桶，末尾一律截到今天。
        """
        today = date.today()
        out: list[tuple[str, date, date]] = []
        if mode == "day":
            for i in range(6, -1, -1):
                d = today - timedelta(days=i)
                out.append(("今天" if d == today else f"{d.day}日", d, d))
        elif mode == "week":
            monday = today - timedelta(days=today.weekday())
            for i in range(7, -1, -1):
                s = monday - timedelta(days=i * 7)
                out.append((f"{s.month}/{s.day}", s,
                            min(s + timedelta(days=6), today)))
        else:
            y, m = today.year, today.month
            for i in range(5, -1, -1):
                yy = y + (m - 1 - i) // 12
                mm = (m - 1 - i) % 12 + 1
                s = date(yy, mm, 1)
                nxt = date(yy + (mm == 12), 1 if mm == 12 else mm + 1, 1)
                out.append((f"{mm}月", s, min(nxt - timedelta(days=1), today)))
        return out

    def _daily_merged(self, start: date, end: date) -> list[dict]:
        """任务侧与专注侧的按日数据并成一张表：done/total/minutes/count。

        两条 GROUP BY（各自内部已经补零），不是一天查一次。
        """
        todos = {d["date"]: d for d in
                 services.todo_daily_stats(start.isoformat(), end.isoformat())}
        pomos = {d["date"]: d for d in
                 services.pomodoro_range_daily(start.isoformat(), end.isoformat())}
        out = []
        cur = start
        while cur <= end:
            ds = cur.isoformat()
            t, r = todos.get(ds, {}), pomos.get(ds, {})
            out.append({"date": ds, "done": t.get("done", 0),
                        "total": t.get("total", 0),
                        "minutes": r.get("minutes", 0),
                        "count": r.get("count", 0)})
            cur += timedelta(days=1)
        return out

    @staticmethod
    def _bucket_rows(daily: list[dict],
                     buckets: list[tuple[str, date, date]]) -> list[list[dict]]:
        base = date.fromisoformat(daily[0]["date"]) if daily else None
        rows = []
        for _label, s, e in buckets:
            if base is None:
                rows.append([])
                continue
            i0 = max((s - base).days, 0)
            i1 = min((e - base).days, len(daily) - 1)
            rows.append(daily[i0:i1 + 1] if i1 >= i0 else [])
        return rows

    def _on_ov_grain(self, key: str):
        combo = self.ov_charts[key][1]
        self._ov_grain[key] = self.OV_GRAINS[
            max(0, min(combo.currentIndex(), 2))]
        self._refresh_overview_charts()

    def _refresh_overview(self) -> None:
        today = date.today()
        tot = services.todo_overview_totals()
        week = services.todo_range_stats(
            (today - timedelta(days=6)).isoformat(), today.isoformat())
        for (num, _txt), val in zip(self.ov_band, (tot["total"], tot["done"],
                                                   tot["lists"], tot["days"])):
            num.setText(str(val))
        # 参考图右侧是「你比 81% 的用户更勤奋」—— 那是拿全体用户做分位的说法，
        # 单机版没有别人，这里换成同样一句话形、但完全可核的近期完成数。
        self.ov_band_note.setText(
            '最近 7 天完成 <span style="color:%s;font-weight:700;">%d</span> 个'
            % (theme.get("focus"), week["done"]))
        self._style_band()
        self._refresh_overview_charts()

    def _refresh_overview_charts(self) -> None:
        today = date.today()
        daily = self._daily_merged(today - timedelta(days=185), today)
        agg = {"done": lambda rs: float(sum(r["done"] for r in rs)),
               "tomato": lambda rs: float(sum(r["count"] for r in rs)),
               "focus": lambda rs: float(sum(r["minutes"] for r in rs)),
               "rate": lambda rs: (sum(r["done"] for r in rs) * 100.0
                                   / sum(r["total"] for r in rs))
               if sum(r["total"] for r in rs) else 0.0}
        for key, (chart, _combo) in self.ov_charts.items():
            buckets = self._ov_buckets(self._ov_grain[key])
            rows = self._bucket_rows(daily, buckets)
            chart.set_data([agg[key](rs) for rs in rows],
                           [b[0] for b in buckets],
                           highlight=len(buckets) - 1)

        # 我的成就值：总分 + 最近 7 天的累计走势（每天赚的分累加上去，
        # 所以这条线单调往上，和参考图里那条累计线一个形状）
        buckets = self._ov_buckets("day")
        rows = self._bucket_rows(daily, buckets)
        total_score = services.achievement_score()
        day_scores = [sum(r["done"] for r in rs) * 10
                      + sum(r["count"] for r in rs) * 5 for rs in rows]
        after = [0] * len(rows)
        run = 0
        for i in range(len(rows) - 1, -1, -1):
            after[i] = run
            run += day_scores[i]
        self.ov_spark.set_data(
            [float(total_score - after[i]) for i in range(len(rows))],
            [b[0] for b in buckets], highlight=len(rows) - 1)
        self.ov_total.setText(f"{total_score:,}")

        # 本周打卡进展：周日为一周起点，进度 = 当天任务完成率
        sunday = today - timedelta(days=(today.weekday() + 1) % 7)
        idx = {d["date"]: d for d in daily}
        ratios, counts = [], []
        for i in range(7):
            r = idx.get((sunday + timedelta(days=i)).isoformat()) or {}
            done, total = r.get("done", 0), r.get("total", 0)
            ratios.append(done * 100.0 / total if total else 0.0)
            counts.append((done, total))
        self.ov_rings.set_data(ratios, sunday, counts)

    def _style_band(self) -> None:
        """汇总条与累计卡的数字字号/颜色。

        参考图实测：数字和单位都是 #191919（单位只是字号小一点、不加粗），
        所以走中性色而不是主题里偏蓝的 muted；写内联样式是为了不动全局 theme.py。
        """
        strong = focus_ui.neutral_text("value").name()
        for num, txt in self.ov_band:
            num.setStyleSheet(f"font-size:15px; font-weight:700; color:{strong};")
            txt.setStyleSheet(f"font-size:13px; color:{strong};")
        self.ov_band_note.setStyleSheet(
            "font-size:13px; color:%s;"
            % focus_ui.neutral_text("label").name())
        self.ov_total.setStyleSheet(
            "font-size:19px; font-weight:700; color:%s;" % theme.get("focus"))

    # ------------------------------------------------------------------
    # 标签页 2：任务
    # ------------------------------------------------------------------
    def _build_task_tab(self) -> QWidget:
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 10, 0, 24)
        lay.setSpacing(14)

        # ---- 顶部工具条：粒度 + 周期切换 ----
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.tk_grain = widgets.ComboBox()
        self.tk_grain.setObjectName("FocusPillCombo")
        self.tk_grain.addItems(["按日", "按周", "按月"])
        self.tk_grain.setFixedSize(88, 28)
        self.tk_grain.setCursor(Qt.PointingHandCursor)
        self.tk_grain.currentIndexChanged.connect(self._on_task_grain)
        bar.addWidget(self.tk_grain)

        pill = QWidget()
        pill.setObjectName("FocusPill")
        pill.setFixedHeight(28)
        ph = QHBoxLayout(pill)
        ph.setContentsMargins(6, 1, 6, 1)
        ph.setSpacing(0)
        nav, self.tk_nav_lbl, _, _ = self._nav(self._task_prev, self._task_next)
        ph.addWidget(nav)
        bar.addWidget(pill)
        bar.addStretch(1)
        lay.addLayout(bar)

        grid = QGridLayout()
        grid.setSpacing(14)

        # ---- 概览：完成数 + 完成率 ----
        ov, ovlay, _ = self._card("概览")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.tk_done = focus_ui.KpiCell("完成数", value_first=True)
        self.tk_rate = focus_ui.KpiCell("完成率", value_first=True)
        row.addWidget(self.tk_done, 1)
        row.addWidget(self.tk_rate, 1)
        ovlay.addStretch(1)
        ovlay.addLayout(row)
        ovlay.addStretch(1)
        grid.addWidget(ov, 0, 0)

        # ---- 完成率分布 ----
        dist, dlay, _ = self._card("完成率分布")
        self.tk_dist = focus_ui.DonutChart()
        self.tk_dist.setFixedHeight(162)
        dlay.addWidget(self.tk_dist, 1)
        # 空状态只留环自己的「暂无数据」，和参考图一致；
        # 有数据时在下面补图例，否则三片颜色分不清谁是谁。
        self.tk_dist_legend = QVBoxLayout()
        self.tk_dist_legend.setSpacing(8)
        dlay.addLayout(self.tk_dist_legend)
        grid.addWidget(dist, 0, 1)

        # ---- 已完成分类统计 ----
        cls, clay, chead = self._card("已完成分类统计")
        self.tk_group = widgets.ComboBox()
        self.tk_group.setObjectName("FocusPillCombo")
        self.tk_group.addItems(["按清单", "按标签", "按优先级"])
        self.tk_group.setFixedSize(98, 28)
        self.tk_group.setCursor(Qt.PointingHandCursor)
        self.tk_group.currentIndexChanged.connect(self._on_task_group)
        chead.addWidget(self.tk_group)
        # 环形在上、图例在下 —— 参考图里这张卡是窄卡（半栏），
        # 环居中占满整宽，空状态就只剩环自己的「暂无数据」，不会重复出现两处。
        self.tk_class = focus_ui.DonutChart()
        self.tk_class.setFixedHeight(162)
        clay.addWidget(self.tk_class)
        self.tk_class_legend = QVBoxLayout()
        self.tk_class_legend.setSpacing(8)
        clay.addLayout(self.tk_class_legend)
        grid.addWidget(cls, 1, 0)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        lay.addStretch(1)
        return self._scroll(content)

    # ------------------------------------------------------------------
    # 标签页 3：专注
    # ------------------------------------------------------------------
    def _build_focus_tab(self) -> QWidget:
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 10, 0, 24)
        lay.setSpacing(14)

        # ---- 概览 KPI ----
        kpi, klay, _ = self._card("概览")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.fc_kpi = []
        # 顺序按参考图「统计 → 专注」：今日番茄 / 总番茄 / 今日专注时长 / 总专注时长。
        # 上一轮为了跟主页概览四卡「统一」改成今日两项相邻，拿到参考图后确认是错的。
        for label in ("今日番茄", "总番茄", "今日专注时长", "总专注时长"):
            cell = focus_ui.KpiCell(label, value_first=True)
            self.fc_kpi.append(cell)
            row.addWidget(cell, 1)
        klay.addLayout(row)
        lay.addWidget(kpi)

        # ---- 专注详情 + 专注记录 ----
        row2 = QHBoxLayout()
        row2.setSpacing(14)

        detail, dlay, dhead = self._card("专注详情")
        self.detail_group = widgets.ComboBox()
        self.detail_group.setObjectName("TextCombo")
        self.detail_group.addItems(["按任务", "按时段"])
        self.detail_group.currentIndexChanged.connect(self._on_detail_group)
        self.detail_group.setFixedWidth(76)
        dhead.addWidget(self.detail_group)
        self.detail_grain = widgets.ComboBox()
        self.detail_grain.setObjectName("TextCombo")
        self.detail_grain.addItems(["日", "周", "月", "年"])
        self.detail_grain.setFixedWidth(62)
        # 默认周：先落到 1 再接信号，否则一接就把 _range_mode 改成「日」
        self.detail_grain.setCurrentIndex(1)
        self.detail_grain.currentIndexChanged.connect(self._on_grain)
        dhead.addWidget(self.detail_grain)
        dn, self.detail_nav_lbl, _, _ = self._nav(self._shift_prev, self._shift_next)
        dhead.addWidget(dn)
        self.detail_donut = focus_ui.DonutChart()
        self.detail_donut.setMinimumHeight(190)
        dlay.addWidget(self.detail_donut, 1)
        self.detail_legend = QVBoxLayout()
        self.detail_legend.setSpacing(6)
        dlay.addLayout(self.detail_legend)
        row2.addWidget(detail, 3)

        rec_card, rlay, rhead = self._card("专注记录")
        self.rec_nav, self.rec_nav_lbl, _, _ = self._nav(self._shift_prev, self._shift_next)
        rhead.addWidget(self.rec_nav)
        # 参考图这张卡右上角有个「+」：在统计页里也能直接补录，
        # 不用先退回主页再找入口。
        add_btn = focus_ui.icon_button("+", "补录一条专注记录", 26, "FocusIconBtn")
        add_btn.clicked.connect(self._add_record_from_stats)
        rhead.addWidget(add_btn)
        self.rec_scroll = QScrollArea()
        self.rec_scroll.setWidgetResizable(True)
        self.rec_scroll.setFrameShape(QFrame.NoFrame)
        self.rec_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.rec_scroll.setMinimumHeight(230)
        self.rec_content = QWidget()
        self.rec_layout = QVBoxLayout(self.rec_content)
        self.rec_layout.setContentsMargins(0, 0, 0, 0)
        self.rec_layout.setSpacing(0)
        self.rec_layout.addStretch(1)
        self.rec_scroll.setWidget(self.rec_content)
        rlay.addWidget(self.rec_scroll, 1)
        rec_card.setMinimumWidth(320)
        row2.addWidget(rec_card, 2)
        lay.addLayout(row2)

        # ---- 专注趋势 + 专注时间线 ----
        row3 = QHBoxLayout()
        row3.setSpacing(14)
        trend, tlay, thead = self._card("专注趋势")
        self.fc_trend_avg = QLabel("每日平均：0m")
        self.fc_trend_avg.setObjectName("FocusSub")
        tlay.addWidget(self.fc_trend_avg)
        self.fc_trend = focus_ui.BarSeries()
        self.fc_trend.setMinimumHeight(180)
        tlay.addWidget(self.fc_trend)
        row3.addWidget(trend, 1)

        tl_card, tllay, tlhead = self._card("专注时间线")
        tlw, self.tl_lbl, _, _ = self._nav(self._shift_prev, self._shift_next)
        tlhead.addWidget(tlw)
        tllay.addWidget(self._hint("按每段专注的开始时刻绘制"))
        self.timeline = focus_ui.TimelineChart()
        self.timeline.setMinimumHeight(180)
        tllay.addWidget(self.timeline)
        row3.addWidget(tl_card, 1)
        lay.addLayout(row3)

        # ---- 最佳专注时间 + 年度热力图 ----
        row4 = QHBoxLayout()
        row4.setSpacing(14)
        hour_card, hlay, hhead = self._card("最佳专注时间")
        hw, self.hour_lbl, _, _ = self._nav(self._month_prev, self._month_next)
        hhead.addWidget(hw)
        hlay.addWidget(self._hint("按小时统计专注时长"))
        self.hour_chart = focus_ui.HourBarChart()
        self.hour_chart.setMinimumHeight(180)
        hlay.addWidget(self.hour_chart)
        row4.addWidget(hour_card, 1)

        heat_card, he_lay, he_head = self._card("年度热力图")
        yw, self.heat_lbl, _, _ = self._nav(self._year_prev, self._year_next)
        he_head.addWidget(yw)
        he_lay.addWidget(self._hint("颜色越深表示当天专注时间越长"))
        self.heat = focus_ui.HeatmapChart()
        self.heat.setMinimumHeight(180)
        he_lay.addWidget(self.heat)
        row4.addWidget(heat_card, 1)
        lay.addLayout(row4)
        lay.addStretch(1)
        return self._scroll(content)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _on_grain(self, idx: int):
        self._range_mode = ("day", "week", "month", "year")[max(0, min(idx, 3))]
        self._offset = 0
        self.detail_grain.blockSignals(True)
        self.detail_grain.setCurrentIndex(idx)
        self.detail_grain.blockSignals(False)
        self.refresh()

    def _on_detail_group(self, idx: int):
        self._detail_group = idx
        self.refresh()

    def _shift_prev(self):
        self._offset += 1
        self.refresh()

    def _shift_next(self):
        # › 不许越过今天：以前能翻到未来那一周，图上全空、又看不出为什么
        self._offset = max(0, self._offset - 1)
        self.refresh()

    def _month_prev(self):
        self._month_offset += 1
        self.refresh()

    def _month_next(self):
        self._month_offset = max(0, self._month_offset - 1)
        self.refresh()

    def _year_prev(self):
        self._year_offset += 1
        self.refresh()

    def _year_next(self):
        self._year_offset = max(0, self._year_offset - 1)
        self.refresh()

    # -- 「任务」标签的周期与回调 -----------------------------------------
    def _task_range(self) -> tuple[date, date, str]:
        today = date.today()
        if self._tk_mode == "day":
            d = today - timedelta(days=self._tk_offset)
            return d, d, ("今天" if self._tk_offset == 0
                          else f"{d.month}月{d.day}日")
        if self._tk_mode == "week":
            base = today - timedelta(days=self._tk_offset * 7)
            start = base - timedelta(days=base.weekday())
            end = start + timedelta(days=6)
            return start, end, ("本周" if self._tk_offset == 0
                                else f"{start.month}月{start.day}日")
        y, m = self._shift_month(today.year, today.month, -self._tk_offset)
        start = date(y, m, 1)
        nxt = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
        return start, nxt - timedelta(days=1), f"{y}年{m}月"

    def _prev_task_range(self) -> tuple[date, date]:
        """上一个同粒度周期（「比前一天 / 前一周 / 前一月」的对照基准）。"""
        keep = self._tk_offset
        self._tk_offset = keep + 1
        try:
            start, end, _ = self._task_range()
        finally:
            self._tk_offset = keep
        return start, end

    def _on_task_grain(self, idx: int):
        self._tk_mode = ("day", "week", "month")[max(0, min(idx, 2))]
        self._tk_offset = 0
        self._refresh_task_tab()

    def _on_task_group(self, idx: int):
        self._tk_group = max(0, min(idx, 2))
        self._refresh_task_tab()

    def _task_prev(self):
        self._tk_offset += 1
        self._refresh_task_tab()

    def _task_next(self):
        # 不允许翻到未来 —— 「今天的完成率」没有未来可言
        if self._tk_offset > 0:
            self._tk_offset -= 1
            self._refresh_task_tab()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.hide()

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------
    def apply_theme(self, force: bool = False) -> None:
        """页底色。

        参考图实测页底是**中性浅灰 #f5f5f5**、卡片纯白，所以这里既不用主题的
        ``bg``（#f4f5f9 偏蓝），也不能跟专注主页的白底统一 —— 统计页是卡片式
        仪表盘，卡与底必须有明度差才分得开。上一轮按「覆盖层要和主页一致」改成
        白底是判断错了，拿到参考图后改回。
        """
        col = focus_ui.stats_bg().name()
        # setStyleSheet 会让整棵子树重新抛光（实测一次 290ms），而这里绝大多数
        # 刷新根本没换主题 —— 颜色没变就直接返回，只在主题真的翻转时重设。
        if col == getattr(self, "_bg_applied", None) and not force:
            return
        self._bg_applied = col
        self.setStyleSheet("QWidget#FocusStatsPage { background: %s; }" % col)
        if hasattr(self, "ov_band"):
            self._style_band()

    def _on_tab(self, idx: int) -> None:
        self._ensure_tab(idx)
        self.stack.setCurrentIndex(idx)
        db.set_setting("focus_stats_tab", str(idx))
        self._refresh_tab(idx)

    def _add_record_from_stats(self) -> None:
        """统计页里的「+」：复用主页的补录弹窗。

        主页的 ``_add_record`` 保存后会走 ``_refresh_stats``，统计页正开着，
        那条路径会把本页也刷一遍，所以这里不用再手动 refresh。
        """
        self.page._add_record()

    def _refresh_tab(self, idx: int) -> None:
        if idx == 0:
            self._refresh_kpi(0)
            self._refresh_overview()
        elif idx == 1:
            self._refresh_task_tab()
        else:
            self._refresh_kpi(2)
            self._refresh_range_charts()
            self._refresh_month_chart()
            self._refresh_heatmap()

    def refresh(self) -> None:
        """只刷当前可见的那个页签。

        三签全刷一遍实测 0.55s（每签十几条 SQL，而 db.connect() 是语句级新建
        连接），番茄钟一结束就卡这么一下没道理；切到别的页签时再补刷那一份。
        """
        self.apply_theme()
        self._refresh_tab(self.stack.currentIndex())

    def _refresh_kpi(self, tab: int = 0) -> None:
        today = date.today()
        t = services.pomodoro_day_stat(today.isoformat())
        y = services.pomodoro_day_stat((today - timedelta(days=1)).isoformat())
        total = services.pomodoro_total_stats()
        # 今天和昨天的完成数一次查完（每多调一个 services 函数就多开一次连接）
        two = services.todo_daily_stats(
            (today - timedelta(days=1)).isoformat(), today.isoformat())
        td_done = two[1]["done"] if len(two) > 1 else 0
        ty_done = two[0]["done"] if two else 0
        tot = services.todo_overview_totals()

        # 按标签取值，不按位置 —— 原来 values/subs 是位置对齐的四元素列表，
        # 只要有一处改了 KpiCell 的排列顺序，数值就会静默贴到别的标签上。
        # 差值一律走 _delta 并显式传格式化器：早先用的是 _signed，它把 fmt_rec
        # 写死在里面，于是「今日番茄」的个数差被渲染成「比前一天多1m」。
        cnt_txt, cnt_trend = _delta(t["count"] - y["count"], "前一天",
                                    _count, "个")
        min_txt, min_trend = _delta(t["minutes"] - y["minutes"], "前一天",
                                    fmt_rec)
        done_txt, done_trend = _delta(td_done - ty_done, "前一天",
                                      _count, "个")
        by_label = {
            "今日已完成":   (str(td_done), done_txt, done_trend),
            "今日番茄":     (str(t["count"]), cnt_txt, cnt_trend),
            "今日专注时长": (fmt_rec(t["minutes"]), min_txt, min_trend),
            "总已完成":     (str(tot["done"]),
                            f"共 {tot['total']} 个任务", ""),
            "总番茄":       (str(total["count"]),
                            f"累计 {total['days']} 天有记录", ""),
            "总专注时长":   (fmt_rec(total["minutes"]),
                            f"日均 {fmt_rec(total['avg'])}", ""),
        }
        cells = self.ov_kpi if tab == 0 else self.fc_kpi
        for cell in cells:
            got = by_label.get(cell.label.text())
            if got is not None:
                cell.set_value(*got)

    def _refresh_task_tab(self) -> None:
        """「任务」标签：完成数 / 完成率 / 完成率分布 / 已完成分类统计。"""
        if not hasattr(self, "tk_done"):
            return
        start, end, label = self._task_range()
        self.tk_nav_lbl.setText(label)
        s, e = start.isoformat(), end.isoformat()
        cur = services.todo_range_stats(s, e)
        ps, pe = self._prev_task_range()
        prev = services.todo_range_stats(ps.isoformat(), pe.isoformat())
        unit = {"day": "前一天", "week": "前一周",
                "month": "前一月"}[self._tk_mode]

        txt, trend = _delta(cur["done"] - prev["done"], unit,
                            _count, "个")
        self.tk_done.set_value(str(cur["done"]), txt, trend)
        txt, trend = _delta(cur["rate"] - prev["rate"], unit,
                            _percent, "%")
        self.tk_rate.set_value(f"{cur['rate']:.0f}%", txt, trend)

        # 完成率分布：把完成率拆成「已完成 / 未完成 / 已逾期」三块
        dist = services.todo_status_distribution(s, e)
        keys = [d.get("color") for d in dist]
        items = [(d["name"], float(d["count"])) for d in dist]
        self.tk_dist.set_data(items, f"{cur['rate']:.0f}%", "完成率",
                              color_keys=keys)
        self._fill_legend(self.tk_dist_legend, items,
                          sum(v for _, v in items), unit="个",
                          empty_hint=False, color_keys=keys)

        # 已完成分类统计
        by = ("list", "tag", "priority")[self._tk_group]
        rows = services.todo_completed_breakdown(s, e, by)
        items = [(d["name"], float(d["count"])) for d in rows]
        total = sum(v for _, v in items)
        self.tk_class.set_data(items, _count(total), "已完成")
        self._fill_legend(self.tk_class_legend, items, total, unit="个",
                          empty_hint=False)

    def _refresh_range_charts(self) -> None:
        start, end, label = self._range()
        for lbl in (getattr(self, "detail_nav_lbl", None),
                    getattr(self, "rec_nav_lbl", None), getattr(self, "tl_lbl", None)):
            if lbl is not None:
                lbl.setText(label)

        daily = services.pomodoro_range_daily(start.isoformat(), end.isoformat())
        total_min = sum(d["minutes"] for d in daily)
        # 平均只除以「已经过去的天数」。daily 是逐日补 0 的，当月 25 号去算
        # len(daily) 会把还没来的 5 天当成 0 分钟一起摊平（年视图更夸张：365 天
        # 里只有 268 天发生过）。热力图那边同样按今天截断，这里得一致。
        elapsed = max((min(end, date.today()) - start).days + 1, 1)
        avg = total_min / elapsed

        # 专注趋势
        if hasattr(self, "fc_trend"):
            self.fc_trend_avg.setText(f"每日平均：{fmt_rec(avg)}")
            labels, values, hi = self._trend_series(daily, start, end)
            self.fc_trend.set_data(values, labels, highlight=hi)

        # 专注详情（环形）
        if hasattr(self, "detail_donut"):
            items = self._detail_items(start, end)
            self.detail_donut.set_data(
                items, fmt_rec(total_min), "专注时长")
            self._fill_legend(self.detail_legend, items, total_min)

        # 任务标签页走自己的周期状态，见 _refresh_task_tab()

        # 时间线
        if hasattr(self, "timeline"):
            bars, tl_labels = self._timeline_bars(start, end)
            self.timeline.set_data(bars, tl_labels)

        # 专注记录
        if hasattr(self, "rec_layout"):
            self._fill_records(start, end)

    def _trend_series(self, daily: list[dict], start: date, end: date):
        span = (end - start).days + 1
        today = date.today()
        if span <= 31:
            labels = [d["date"][8:10] for d in daily]
        elif span <= 120:
            labels = [(f"{d['date'][5:7]}/{d['date'][8:10]}"
                       if i % 7 == 0 else "") for i, d in enumerate(daily)]
        else:
            labels = [(f"{d['date'][5:7]}月" if d["date"][8:10] == "01" else "")
                      for d in daily]
        values = [d["minutes"] for d in daily]
        hi = -1
        for i, d in enumerate(daily):
            if d["date"] == today.isoformat():
                hi = i
        return labels, values, hi

    def _detail_items(self, start: date, end: date) -> list[tuple[str, float]]:
        if self._detail_group == 1:
            profile = services.pomodoro_hourly_profile(start.isoformat(),
                                                       end.isoformat())
            out = []
            for name, h0, h1 in TIME_SLOTS:
                m = sum(profile[h0:h1])
                if m > 0:
                    out.append((name, float(m)))
            return out
        dist = services.pomodoro_task_distribution_range(
            start.isoformat(), end.isoformat(), 8)
        return [(d["task"], float(d["minutes"])) for d in dist]

    def _fill_legend(self, layout, items: list[tuple[str, float]],
                     total: float, unit: str = "",
                     empty_hint: bool = True,
                     color_keys: list[str | None] | None = None) -> None:
        """填图例。``unit`` 非空时按「个数」格式化（任务统计用），
        否则按专注时长格式化。``empty_hint=False`` 时不写「暂无数据」——
        环形图自己已经居中写了，再补一行就重复了。
        ``color_keys`` 与 ``items`` 对应，元素是主题色键（可为 None），
        要和环形图的切片颜色保持一致。"""
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not items:
            if not empty_hint:
                return
            hint = QLabel("暂无数据")
            hint.setObjectName("FocusMuted")
            hint.setAlignment(Qt.AlignCenter)
            layout.addWidget(hint)
            return
        total = total or sum(v for _, v in items) or 1.0
        for i, (name, v) in enumerate(items):
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)
            dot = QLabel()
            dot.setFixedSize(9, 9)
            key = color_keys[i] if color_keys and i < len(color_keys) else None
            color = (theme.get(key) if key
                     else focus_ui.DONUT_COLORS[i % len(focus_ui.DONUT_COLORS)])
            dot.setStyleSheet(
                f"background:{color}; border-radius:4px;")
            h.addWidget(dot)
            nm = widgets.ElidedLabel(name)
            nm.setObjectName("FocusLabel")
            h.addWidget(nm, 1)
            val = QLabel(f"{_count(v)}{unit} · {v / total * 100:.0f}%"
                         if unit else
                         f"{fmt_rec(v)} · {v / total * 100:.0f}%")
            val.setObjectName("FocusSub")
            h.addWidget(val)
            layout.addWidget(row)

    def _timeline_bars(self, start: date, end: date):
        recs = services.pomodoro_records_range(start.isoformat(),
                                               end.isoformat(), 400)
        today = date.today()
        span = (end - start).days + 1
        if span <= 7:
            labels = [WD_LABELS[(start.weekday() + i) % 7] for i in range(span)]
        else:
            labels = list(WD_LABELS)
        bars = []
        for r in recs:
            s = (r.get("started_at") or "")
            if len(s) < 16:
                continue
            try:
                dt = datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            d = dt.date()
            idx = (d - start).days
            wd = idx if span <= 7 else d.weekday()
            if wd < 0 or wd > 6:
                continue
            start_h = dt.hour + dt.minute / 60
            hours = max(int(r.get("duration_min") or 0), 1) / 60
            bars.append((wd, start_h, hours, d == today))
        return bars, labels

    def _fill_records(self, start: date, end: date) -> None:
        while self.rec_layout.count() > 1:
            item = self.rec_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        recs = services.pomodoro_records_range(start.isoformat(),
                                               end.isoformat(), 120)
        if not recs:
            hint = QLabel("暂无专注记录")
            hint.setObjectName("FocusMuted")
            hint.setAlignment(Qt.AlignCenter)
            hint.setContentsMargins(0, 24, 0, 0)
            self.rec_layout.insertWidget(0, hint)
            return
        groups: dict[str, list[dict]] = {}
        for r in recs:
            groups.setdefault((r.get("started_at") or "")[:10], []).append(r)
        # 延迟导入：pomodoro 也 import 了本模块，模块级互相引用会成环
        from .pomodoro import _DateHeader
        for day in sorted(groups.keys(), reverse=True):
            # 用主页那个 _DateHeader，不再走主题里 12px muted 的 FocusRecordDate，
            # 否则同一条记录列表在两页里日期标题长得不一样
            header = _DateHeader(self.page._format_day(day))
            self.rec_layout.insertWidget(self.rec_layout.count() - 1, header)
            day_rows = groups[day]
            for i, r in enumerate(day_rows):
                self.rec_layout.insertWidget(
                    self.rec_layout.count() - 1,
                    self.page._make_record_row(
                        r, tail=i < len(day_rows) - 1))

    def _refresh_month_chart(self) -> None:
        start, end, label = self._month_range()
        if hasattr(self, "hour_lbl"):
            self.hour_lbl.setText(label)
        if hasattr(self, "hour_chart"):
            self.hour_chart.set_data(
                services.pomodoro_hourly_profile(start.isoformat(), end.isoformat()))

    def _refresh_heatmap(self) -> None:
        start, end, label = self._year_range()
        if hasattr(self, "heat_lbl"):
            self.heat_lbl.setText(label)
        daily = services.pomodoro_range_daily(start.isoformat(), end.isoformat())
        data = {d["date"]: d["minutes"] for d in daily}
        end_clip = min(end, date.today())
        if hasattr(self, "heat"):
            self.heat.set_data(data, start, end_clip)
