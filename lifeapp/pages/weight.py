"""体重管理：打卡风格主页（大号体重 + BMI/体脂率 + 减重进度）+ 趋势曲线。

- Hero 卡：当前体重超大数字、BMI / 体脂率、与上次对比文案、减重进度条。
- 体重 / BMI 双趋势图（日/周/月/年平均值统计 + 全屏查看 + 鼠标悬停气泡）。
- 记录/编辑体重：经典日历弹窗选择日期、可顺带设置身高、体脂率选填。
"""
from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QDoubleSpinBox, QScrollArea, QFrame, QDialog, QButtonGroup, QCalendarWidget, QDateEdit)

from .. import popups, services, sounds, widgets, db, theme


def _bmi_category(bmi: float) -> tuple[str, str]:
    if bmi < 18.5:
        return "偏瘦", "blue"
    if bmi < 24:
        return "正常", "green"
    if bmi < 28:
        return "偏胖", "amber"
    return "肥胖", "red"


def _short_label(date_str: str) -> str:
    """'2026-08-29' -> '8/29'"""
    d = QDate.fromString(date_str, "yyyy-MM-dd")
    return f"{d.month()}/{d.day()}" if d.isValid() else date_str


def _record_time(rec: dict) -> str:
    """记录展示时间：date + created_at 的时分（如 2026/08/29 11:14）。"""
    d = QDate.fromString(rec["date"], "yyyy-MM-dd")
    base = d.toString("yyyy/MM/dd") if d.isValid() else rec["date"]
    created = rec.get("created_at") or ""
    if len(created) >= 16:
        return f"{base} {created[11:16]}"
    return base


def _pick_date(parent: QWidget, current: QDate) -> QDate | None:
    """经典日历选择器：弹窗内 QCalendarWidget + 确定 / 取消。"""
    dlg = QDialog(parent)
    dlg.setWindowTitle("选择日期")
    dlg.setModal(True)
    lay = QVBoxLayout(dlg)
    lay.setSpacing(12)
    cal = QCalendarWidget()
    cal.setSelectedDate(current)
    cal.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
    cal.setGridVisible(False)
    lay.addWidget(cal)
    btns = QHBoxLayout()
    cancel = QPushButton("取消")
    cancel.setObjectName("Ghost")
    cancel.clicked.connect(dlg.reject)
    ok = QPushButton("确定")
    ok.setObjectName("Primary")
    ok.setDefault(True)
    ok.clicked.connect(dlg.accept)
    btns.addStretch(1)
    btns.addWidget(cancel)
    btns.addWidget(ok)
    lay.addLayout(btns)
    if dlg.exec() == QDialog.Accepted:
        return cal.selectedDate()
    return None


def _aggregate(data: list[dict], gran: str) -> tuple[list[str], list[float], list[str], list[str]]:
    """按粒度聚合体重为平均值：day / week / month / year(按季度)。
    返回 (labels, weights, sub_labels, full_dates)。
    sub_labels[i] 是 x 轴第二行文本：日/月 = 年份（2026），周 = '~MM/DD'（周结束日），
    年视图按季度 → 主行 '第N季'、副行年份。
    full_dates[i] 是该聚合点对应的一个代表性完整日期（用于点击跳转编辑）。
    """
    if not data:
        return [], [], [], []
    groups: dict[str, list[tuple[float, str]]] = {}
    for d in data:
        ds = d["date"]
        if gran == "day":
            key = ds
        elif gran == "week":
            y, m, day = int(ds[:4]), int(ds[5:7]), int(ds[8:10])
            iso = date(y, m, day).isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
        elif gran == "month":
            key = ds[:7]
        else:  # year → 按季度聚合
            key = f"{ds[:4]}-Q{(int(ds[5:7]) - 1) // 3 + 1}"
        groups.setdefault(key, []).append((d["weight"], ds))
    labels: list[str] = []
    weights: list[float] = []
    subs: list[str] = []
    full_dates: list[str] = []
    for k in sorted(groups):
        items = groups[k]
        vals = [v for v, _ in items]
        weights.append(round(sum(vals) / len(vals), 2))
        if gran == "day":
            labels.append(_short_label(k))
            subs.append(k[:4])
            full_dates.append(k)
        elif gran == "week":
            monday = date.fromisocalendar(int(k[:4]), int(k[6:8]), 1)
            sunday = monday + timedelta(days=6)
            labels.append(monday.strftime("%m/%d"))
            subs.append(f"~{sunday.strftime('%m/%d')}")
            full_dates.append(monday.strftime("%Y-%m-%d"))
        elif gran == "month":
            labels.append(f"{int(k[5:7])}月")
            subs.append(k[:4])
            full_dates.append(k + "-01")
        else:  # 季度
            y, q = k.split("-Q")
            labels.append(f"第{q}季")
            subs.append(y)
            full_dates.append(f"{y}-{(int(q) - 1) * 3 + 1:02d}-01")
    return labels, weights, subs, full_dates


class _DateButton(QPushButton):
    """显示日期，点击弹出经典日历选择器。"""

    def __init__(self, current: QDate, parent: QWidget | None = None):
        super().__init__(parent)
        self._date = current
        self.setObjectName("Ghost")
        self.setFixedHeight(38)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点击选择日期")
        self.clicked.connect(self._choose)
        self._refresh()

    def _refresh(self) -> None:
        self.setText(f"📅 {self._date.toString('yyyy-MM-dd')}")

    def _choose(self) -> None:
        d = _pick_date(self.window(), self._date)
        if d is not None:
            self.set_date(d)

    def set_date(self, d: QDate) -> None:
        self._date = d
        self._refresh()

    def date(self) -> QDate:
        return self._date


class RecordDialog(QDialog):
    """记录/编辑体重：经典日历选日期 + 可顺带设置身高 + 可选体脂率。

    传入 record（dict）时进入编辑模式，预填该记录的值并回写；
    弹窗内实时显示相对上次（编辑时为相对原记录）的变化。
    """

    def __init__(self, parent, last_weight: float = 60.0, record: dict | None = None,
                 height: float = 0.0):
        super().__init__(parent)
        editing = record is not None
        self._base_word = "原记录" if editing else "上次"
        self._base_weight = float(record["weight"]) if record else float(last_weight)
        self._record_id = record["id"] if record else None
        self.deleted = False  # 删除后置 True，供调用方判断
        self.setWindowTitle("编辑记录" if editing else "记录体重")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        init_weight = self._base_weight
        init_fat = record.get("body_fat") if record else None
        init_date = QDate.currentDate()
        if record:
            d = QDate.fromString(record["date"], "yyyy-MM-dd")
            if d.isValid():
                init_date = d

        # 大号预览 + 相对上次的变化
        pv_row = QHBoxLayout()
        pv_row.setSpacing(8)
        pv_row.addStretch(1)
        self.preview = QLabel(f"{init_weight:.2f}")
        self.preview.setObjectName("HeroWeight")
        self.preview.setAlignment(Qt.AlignCenter)
        pv_row.addWidget(self.preview)
        unit = QLabel("公斤")
        unit.setObjectName("HeroUnit")
        pv_row.addWidget(unit)
        pv_row.addStretch(1)
        lay.addLayout(pv_row)

        self.delta_lbl = QLabel("")
        self.delta_lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.delta_lbl)

        # 体重（单位外置）
        w_row = QHBoxLayout()
        w_row.setSpacing(8)
        w_lbl = QLabel("体重")
        w_lbl.setFixedWidth(56)
        w_row.addWidget(w_lbl)
        self.weight_spin = widgets.DoubleSpinBox()
        self.weight_spin.setRange(20, 300)
        self.weight_spin.setDecimals(2)
        self.weight_spin.setSingleStep(0.1)
        self.weight_spin.setValue(init_weight)
        self.weight_spin.setFixedHeight(38)
        self.weight_spin.valueChanged.connect(self._on_weight_changed)
        w_row.addWidget(self.weight_spin, 1)
        w_unit = QLabel("kg")
        w_unit.setObjectName("Meta")
        w_row.addWidget(w_unit)
        lay.addLayout(w_row)

        # 身高（记录时可顺带设置）
        h_row = QHBoxLayout()
        h_row.setSpacing(8)
        h_lbl = QLabel("身高")
        h_lbl.setFixedWidth(56)
        h_row.addWidget(h_lbl)
        self.height_spin = widgets.DoubleSpinBox()
        self.height_spin.setRange(0, 250)
        self.height_spin.setDecimals(0)
        self.height_spin.setValue(height)
        self.height_spin.setSpecialValueText("未设置")
        self.height_spin.setFixedHeight(38)
        h_row.addWidget(self.height_spin, 1)
        h_unit = QLabel("cm")
        h_unit.setObjectName("Meta")
        h_row.addWidget(h_unit)
        lay.addLayout(h_row)

        # 日期：可输入 + 日历弹窗 + 快捷按钮
        d_row = QHBoxLayout()
        d_row.setSpacing(8)
        d_lbl = QLabel("日期")
        d_lbl.setFixedWidth(56)
        d_row.addWidget(d_lbl)
        self.date_edit = widgets.DateInput()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(init_date)
        self.date_edit.setFixedHeight(38)
        self.date_edit.setToolTip("可直接键入日期，或点击右侧箭头弹出日历")
        d_row.addWidget(self.date_edit, 1)
        for days_ago, label in ((0, "今天"), (1, "昨天"), (2, "前天")):
            b = QPushButton(label)
            b.setObjectName("TagChip")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _, k=days_ago: self.date_edit.setDate(
                QDate.currentDate().addDays(-k)))
            d_row.addWidget(b)
        lay.addLayout(d_row)

        # 体脂率（可选）
        f_row = QHBoxLayout()
        f_row.setSpacing(8)
        f_lbl = QLabel("体脂率")
        f_lbl.setFixedWidth(56)
        f_row.addWidget(f_lbl)
        self.fat_spin = widgets.DoubleSpinBox()
        self.fat_spin.setRange(0, 60)
        self.fat_spin.setDecimals(2)
        self.fat_spin.setSpecialValueText("不记录")
        self.fat_spin.setFixedHeight(38)
        if init_fat:
            self.fat_spin.setValue(float(init_fat))
        f_row.addWidget(self.fat_spin, 1)
        f_unit = QLabel("%")
        f_unit.setObjectName("Meta")
        f_row.addWidget(f_unit)
        f_note = QLabel("选填")
        f_note.setObjectName("Meta")
        f_row.addWidget(f_note)
        lay.addLayout(f_row)

        btns = QHBoxLayout()
        if editing:
            delete = QPushButton("删除")
            delete.setObjectName("Danger")
            delete.setCursor(Qt.PointingHandCursor)
            delete.clicked.connect(self._confirm_delete)
            btns.addWidget(delete)
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存记录")
        save.setObjectName("Primary")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(save)
        lay.addLayout(btns)

        self._on_weight_changed(init_weight)

    def _on_weight_changed(self, v: float) -> None:
        """预览大数字 + 实时显示相对上次（编辑时为原记录）的变化。"""
        self.preview.setText(f"{v:.2f}")
        diff = round(v - self._base_weight, 2)
        if abs(diff) < 0.005:
            self.delta_lbl.setText(f"与{self._base_word}持平")
            color = "muted"
        elif diff < 0:
            self.delta_lbl.setText(f"较{self._base_word}下降 {abs(diff):.2f} kg")
            color = "green"
        else:
            self.delta_lbl.setText(f"较{self._base_word}上升 {diff:.2f} kg")
            color = "red"
        self.delta_lbl.setStyleSheet(
            f"color: {theme.get(color)}; font-weight: 600;")

    @property
    def record_date(self) -> str:
        d = self.date_edit.date()
        if d.isValid():
            return d.toString("yyyy-MM-dd")
        return QDate.currentDate().toString("yyyy-MM-dd")  # 兜底：非法回退今天

    def _confirm_delete(self) -> None:
        """编辑模式下删除当前记录。"""
        if not popups.confirm(self, "删除记录",
                              "确定删除这条体重记录吗？删除后无法恢复。", "删除"):
            return
        services.weight_delete(self._record_id)
        self.deleted = True
        self.accept()

    @property
    def body_fat(self) -> float | None:
        v = self.fat_spin.value()
        return v if v > 0 else None

    @property
    def height(self) -> float:
        return self.height_spin.value()


class SettingsDialog(QDialog):
    """身高 / 目标体重 / 目标日期 设置（分组布局 + BMI 实时反馈）。"""

    def __init__(self, parent, height: float, goal: float, goal_date: str,
                 current_weight: float | None = None):
        super().__init__(parent)
        self._current = current_weight
        self.setWindowTitle("体重设置")
        self.setMinimumWidth(460)
        root = QVBoxLayout(self)
        root.setSpacing(12)

        # ---- 基本信息 ----
        g1 = QLabel("基本信息")
        g1.setObjectName("GroupTitle")
        root.addWidget(g1)
        hrow = QHBoxLayout()
        hrow.setSpacing(8)
        hl = QLabel("身高")
        hl.setFixedWidth(72)
        hrow.addWidget(hl)
        self.height_spin = widgets.DoubleSpinBox()
        self.height_spin.setRange(0, 250)
        self.height_spin.setDecimals(0)
        self.height_spin.setValue(height)
        self.height_spin.setFixedHeight(36)
        self.height_spin.valueChanged.connect(self._update_hint)
        hrow.addWidget(self.height_spin, 1)
        h_unit = QLabel("cm")
        h_unit.setObjectName("Meta")
        hrow.addWidget(h_unit)
        root.addLayout(hrow)

        # ---- 目标 ----
        g2 = QLabel("减重 / 增重目标")
        g2.setObjectName("GroupTitle")
        root.addWidget(g2)
        grow = QHBoxLayout()
        grow.setSpacing(8)
        gl = QLabel("目标体重")
        gl.setFixedWidth(72)
        grow.addWidget(gl)
        self.goal_spin = widgets.DoubleSpinBox()
        self.goal_spin.setRange(0, 300)
        self.goal_spin.setDecimals(2)
        self.goal_spin.setValue(goal)
        self.goal_spin.setSpecialValueText("未设置")
        self.goal_spin.setFixedHeight(36)
        self.goal_spin.valueChanged.connect(self._update_hint)
        grow.addWidget(self.goal_spin, 1)
        g_unit = QLabel("kg")
        g_unit.setObjectName("Meta")
        grow.addWidget(g_unit)
        root.addLayout(grow)

        drow = QHBoxLayout()
        drow.setSpacing(8)
        dl = QLabel("目标日期")
        dl.setFixedWidth(72)
        drow.addWidget(dl)
        d = QDate.fromString(goal_date, "yyyy-MM-dd")
        self.goal_date = widgets.DateInput()
        self.goal_date.setCalendarPopup(True)
        self.goal_date.setDisplayFormat("yyyy-MM-dd")
        self.goal_date.setDate(d if d.isValid() else QDate.currentDate().addMonths(3))
        self.goal_date.setFixedHeight(36)
        self.goal_date.setToolTip("可直接键入日期，或点击右侧箭头弹出日历")
        drow.addWidget(self.goal_date, 1)
        root.addLayout(drow)

        # 目标日期快捷设定
        qrow = QHBoxLayout()
        qrow.setSpacing(6)
        qrow.addSpacing(80)
        qh = QLabel("快捷")
        qh.setObjectName("Meta")
        qrow.addWidget(qh)
        for months, label in ((3, "3 个月"), (6, "6 个月"), (12, "1 年")):
            b = QPushButton(label)
            b.setObjectName("TagChip")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _, m=months: self.goal_date.setDate(
                QDate.currentDate().addMonths(m)))
            qrow.addWidget(b)
        qrow.addStretch(1)
        root.addLayout(qrow)

        # BMI 实时反馈
        self.hint = QLabel("")
        self.hint.setObjectName("Meta")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        # BMI 参考（彩色标签）
        ref_row = QHBoxLayout()
        ref_row.setSpacing(6)
        ref_lbl = QLabel("BMI 参考")
        ref_lbl.setObjectName("Meta")
        ref_row.addWidget(ref_lbl)
        for label, color in (("偏瘦 <18.5", "blue"), ("正常 18.5-24", "green"),
                             ("偏胖 24-28", "amber"), ("肥胖 ≥28", "red")):
            ref_row.addWidget(widgets.Tag(label, color))
        ref_row.addStretch(1)
        root.addLayout(ref_row)

        root.addStretch(1)
        btns = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.setObjectName("Primary")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(save)
        root.addLayout(btns)

        self._update_hint()

    def _update_hint(self) -> None:
        """身高/目标变化时，实时显示当前与目标体重的 BMI 反馈。"""
        h = self.height_spin.value()
        g = self.goal_spin.value()
        parts = []
        if h > 0:
            if self._current:
                bmi = self._current / (h / 100) ** 2
                cat, _ = _bmi_category(bmi)
                parts.append(f"当前 {self._current:.2f} kg → BMI {bmi:.2f}（{cat}）")
            if g > 0:
                bmi = g / (h / 100) ** 2
                cat, _ = _bmi_category(bmi)
                parts.append(f"目标 {g:.2f} kg → BMI {bmi:.2f}（{cat}）")
        self.hint.setText(
            "；".join(parts) if parts else "填写身高后，这里实时显示 BMI 反馈")


class WeightPage(QWidget):
    def __init__(self):
        super().__init__()
        self._height = float(db.get_setting("height_cm", "0"))
        self._goal = float(db.get_setting("weight_goal", "0"))
        self._goal_date = db.get_setting("weight_goal_date", "")
        self._range = "day"  # 趋势粒度：day / week / month / year

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        # 标题行 + 操作按钮
        head = QHBoxLayout()
        head.setSpacing(10)
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("体重管理")
        title.setObjectName("PageTitle")
        sub = QLabel("记录体重变化，坚持就是胜利")
        sub.setObjectName("PageSubtitle")
        title_col.addWidget(title)
        title_col.addWidget(sub)
        head.addLayout(title_col)
        head.addStretch(1)
        setting_btn = QPushButton("⚙ 设置")
        setting_btn.setObjectName("Ghost")
        setting_btn.setCursor(Qt.PointingHandCursor)
        setting_btn.clicked.connect(self._open_settings)
        head.addWidget(setting_btn)
        add_btn = QPushButton("＋ 记录体重")
        add_btn.setObjectName("Primary")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(self._open_record)
        head.addWidget(add_btn)
        root.addLayout(head)

        # 滚动内容
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(14)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        self._body = body

        self._build_hero()
        self._build_charts()
        self._build_list()

        self.reload()

    # ---------- Hero 卡 ----------
    def _build_hero(self) -> None:
        hero = QFrame()
        hero.setObjectName("HeroCard")
        lay = QVBoxLayout(hero)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(24)

        left = QVBoxLayout()
        left.setSpacing(0)
        self.hero_date = QLabel("--")
        self.hero_date.setObjectName("Meta")
        wr = QHBoxLayout()
        wr.setSpacing(8)
        self.hero_weight = QLabel("--")
        self.hero_weight.setObjectName("HeroWeight")
        unit = QLabel("公斤")
        unit.setObjectName("HeroUnit")
        wr.addWidget(self.hero_weight)
        wr.addWidget(unit)
        wr.addStretch(1)
        left.addWidget(self.hero_date)
        left.addLayout(wr)
        top.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(6)
        self._metric_rows: dict[str, tuple[QLabel, QLabel]] = {}
        for name in ("BMI", "体脂率"):
            row = QHBoxLayout()
            row.setSpacing(18)
            n = QLabel(name)
            n.setObjectName("HeroMetricName")
            v = QLabel("--")
            v.setObjectName("HeroMetricVal")
            row.addWidget(n)
            row.addStretch(1)
            row.addWidget(v)
            right.addLayout(row)
            self._metric_rows[name] = (n, v)
        top.addLayout(right)
        lay.addLayout(top)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setObjectName("hline")
        lay.addWidget(sep)

        self.hero_compare = QLabel("记录第一笔体重后开始对比")
        self.hero_compare.setObjectName("HeroCompare")
        lay.addWidget(self.hero_compare)

        self.goal_box = QFrame()
        self.goal_box.setObjectName("HeroInner")
        glay = QVBoxLayout(self.goal_box)
        glay.setContentsMargins(18, 14, 18, 14)
        glay.setSpacing(8)
        self.goal_progress = widgets.GoalProgress()
        glay.addWidget(self.goal_progress)
        grow = QHBoxLayout()
        grow.setSpacing(10)
        init_col = QVBoxLayout()
        init_col.setSpacing(0)
        init_lbl = QLabel("初始")
        init_lbl.setObjectName("GoalLabel")
        self.goal_init_val = QLabel("--")
        self.goal_init_val.setObjectName("GoalNum")
        self.goal_init_date = QLabel("")
        self.goal_init_date.setObjectName("GoalLabel")
        init_col.addWidget(init_lbl)
        init_col.addWidget(self.goal_init_val)
        init_col.addWidget(self.goal_init_date)
        grow.addLayout(init_col)
        grow.addStretch(1)
        self.goal_day_lbl = QLabel("")
        self.goal_day_lbl.setObjectName("GoalLabel")
        grow.addWidget(self.goal_day_lbl)
        grow.addStretch(1)
        goal_col = QVBoxLayout()
        goal_col.setSpacing(0)
        goal_lbl = QLabel("目标")
        goal_lbl.setObjectName("GoalLabel")
        goal_lbl.setAlignment(Qt.AlignRight)
        self.goal_target_val = QLabel("--")
        self.goal_target_val.setObjectName("GoalNum")
        self.goal_target_val.setAlignment(Qt.AlignRight)
        self.goal_target_date = QLabel("")
        self.goal_target_date.setObjectName("GoalLabel")
        self.goal_target_date.setAlignment(Qt.AlignRight)
        goal_col.addWidget(goal_lbl)
        goal_col.addWidget(self.goal_target_val)
        goal_col.addWidget(self.goal_target_date)
        grow.addLayout(goal_col)
        glay.addLayout(grow)
        lay.addWidget(self.goal_box)

        self.hero_no_goal = QLabel("在 ⚙ 设置 中填写目标体重后，这里会显示减重进度")
        self.hero_no_goal.setObjectName("Meta")
        self.hero_no_goal.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.hero_no_goal)

        self._body.addWidget(hero)

    # ---------- 趋势图 ----------
    def _build_charts(self) -> None:
        weight_card = widgets.Card()
        whead = QHBoxLayout()
        wt = QLabel("体重趋势")
        wt.setObjectName("CardTitle")
        whead.addWidget(wt)
        whead.addStretch(1)

        # 粒度切换：日 / 周 / 月 / 年（内嵌在标题行右侧）
        self._range_group = QButtonGroup(self)
        self._range_group.setExclusive(True)
        for key, label in (("day", "日"), ("week", "周"),
                           ("month", "月"), ("year", "年")):
            b = QPushButton(label)
            b.setObjectName("TagChip")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            widgets._apply_property(b, "tagColor", "accent")
            b.clicked.connect(lambda _, k=key: self._set_range(k))
            self._range_group.addButton(b)
            if key == self._range:
                b.setChecked(True)
            whead.addWidget(b)
        wfull = QPushButton("⛶")
        wfull.setObjectName("Ghost")
        wfull.setCursor(Qt.PointingHandCursor)
        wfull.setToolTip("全屏")
        wfull.clicked.connect(lambda: self._fullscreen_chart("体重趋势", self.weight_chart))
        whead.addWidget(wfull)
        weight_card.body().addLayout(whead)

        self.weight_chart = widgets.LineChart()
        self.weight_chart.set_on_click(self._on_chart_click)
        weight_card.body().addWidget(self.weight_chart)
        self._body.addWidget(weight_card)

        self.bmi_card = widgets.Card()
        bhead = QHBoxLayout()
        bt = QLabel("BMI 曲线")
        bt.setObjectName("CardTitle")
        bhead.addWidget(bt)
        bhead.addStretch(1)
        bfull = QPushButton("⛶")
        bfull.setObjectName("Ghost")
        bfull.setCursor(Qt.PointingHandCursor)
        bfull.setToolTip("全屏")
        bfull.clicked.connect(lambda: self._fullscreen_chart("BMI 曲线", self.bmi_chart))
        bhead.addWidget(bfull)
        self.bmi_card.body().addLayout(bhead)
        self.bmi_chart = widgets.LineChart()
        self.bmi_chart.set_on_click(self._on_bmi_chart_click)
        self.bmi_card.body().addWidget(self.bmi_chart)
        self._body.addWidget(self.bmi_card)

    def _set_range(self, key: str) -> None:
        self._range = key
        self._update_charts(services.weight_list())

    def _on_chart_click(self, idx: int) -> None:
        """点击体重曲线图某点 → 跳转到对应日期的记录并编辑。"""
        data = services.weight_list()
        if not data or idx < 0:
            return
        labels, weights, subs, full_dates = _aggregate(data, self._range)
        if idx >= len(full_dates):
            return
        target_date = full_dates[idx]
        # 找到该日期对应的最近一条记录
        rec = None
        for r in reversed(data):
            if r["date"] == target_date or (
                    self._range != "day" and r["date"][:len(target_date)] == target_date):
                rec = r
                break
        if rec is None:
            # fallback: 用全局索引映射
            n = len(data)
            chunk = max(1, n // max(1, len(labels)))
            est = min(n - 1, idx * chunk)
            rec = data[est] if est < n else None
        if rec:
            self._edit(rec["id"])

    def _on_bmi_chart_click(self, idx: int) -> None:
        self._on_chart_click(idx)

    def _fullscreen_chart(self, title: str, chart: widgets.LineChart) -> None:
        """全屏查看折线图：完整展示全部数据，数据多时自动分多行。"""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setModal(True)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 18, 24, 24)
        lay.setSpacing(12)
        head = QHBoxLayout()
        t = QLabel(title)
        t.setObjectName("CardTitle")
        head.addWidget(t)
        head.addStretch(1)
        close = QPushButton("关闭 (Esc)")
        close.setObjectName("Ghost")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(dlg.accept)
        head.addWidget(close)
        lay.addLayout(head)

        # 按每行最多 40 点分段，垂直堆叠多张图，完整展示全部数据
        row_max = 40
        values = chart._values
        labels = chart._labels
        subs = chart._sub_labels
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(0, 0, 6, 0)
        vbox.setSpacing(10)
        n = len(values)
        for i in range(0, max(n, 1), row_max):
            seg = widgets.LineChart()
            seg.setFixedHeight(260)
            seg.set_data(values[i:i + row_max], labels[i:i + row_max],
                         color=chart._color, decimals=2,
                         sub_labels=subs[i:i + row_max] if subs else None)
            vbox.addWidget(seg)
        vbox.addStretch(1)
        scroll.setWidget(container)
        lay.addWidget(scroll, 1)
        dlg.showMaximized()
        dlg.exec()

    # ---------- 记录列表 ----------
    def _build_list(self) -> None:
        list_card = widgets.Card("历史记录")
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setMinimumHeight(420)
        container = QWidget()
        self.list_layout = QVBoxLayout(container)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(container)
        list_card.body().addWidget(self.scroll)
        self._body.addWidget(list_card)

    # ---------- 操作 ----------
    def _open_record(self) -> None:
        data = services.weight_list()
        last = data[-1]["weight"] if data else 60.0
        dlg = RecordDialog(self, last, None, self._height)
        if dlg.exec() == QDialog.Accepted:
            services.weight_add(dlg.record_date, dlg.weight_spin.value(), dlg.body_fat)
            sounds.play("weight_logged")
            self._apply_height(dlg.height)
            self.reload()

    def _edit(self, wid: int) -> None:
        rec = next((r for r in services.weight_list() if r["id"] == wid), None)
        if not rec:
            return
        dlg = RecordDialog(self, rec["weight"], rec, self._height)
        if dlg.exec() == QDialog.Accepted:
            if getattr(dlg, "deleted", False):  # 已在对话框内删除，仅刷新
                self.reload()
                return
            services.weight_update(wid, dlg.record_date, dlg.weight_spin.value(),
                                   dlg.body_fat)
            self._apply_height(dlg.height)
            self.reload()

    def _apply_height(self, h: float) -> None:
        if h > 0 and abs(h - self._height) > 1e-6:
            self._height = h
            db.set_setting("height_cm", str(h))

    def _open_settings(self) -> None:
        data = services.weight_list()
        cur = data[-1]["weight"] if data else None
        dlg = SettingsDialog(self, self._height, self._goal, self._goal_date, cur)
        if dlg.exec() == QDialog.Accepted:
            self._height = dlg.height_spin.value()
            self._goal = dlg.goal_spin.value()
            self._goal_date = dlg.goal_date.date().toString("yyyy-MM-dd")  # QDateEdit.date() → QDate
            db.set_setting("height_cm", str(self._height))
            db.set_setting("weight_goal", str(self._goal))
            db.set_setting("weight_goal_date", self._goal_date)
            self.reload()

    def _delete(self, wid: int) -> None:
        if not popups.confirm(self, "删除记录",
                              "确定删除这条体重记录吗？删除后无法恢复。", "删除"):
            return
        services.weight_delete(wid)
        self.reload()

    # ---------- 渲染 ----------
    def reload(self) -> None:
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        data = services.weight_list()

        if data:
            last = data[-1]
            self.hero_date.setText(_record_time(last))
            self.hero_weight.setText(f"{last['weight']:.2f}")
            fat = last.get("body_fat")
            self._metric_rows["体脂率"][1].setText(f"{fat:.2f}" if fat else "--")
            if self._height > 0:
                bmi = last["weight"] / (self._height / 100) ** 2
                self._metric_rows["BMI"][1].setText(f"{bmi:.2f}")
            else:
                self._metric_rows["BMI"][1].setText("--")
            if len(data) >= 2:
                prev = data[-2]
                d1 = QDate.fromString(prev["date"], "yyyy-MM-dd")
                d2 = QDate.fromString(last["date"], "yyyy-MM-dd")
                days = d1.daysTo(d2) if d1.isValid() and d2.isValid() else 0
                diff = last["weight"] - prev["weight"]
                when = f"对比{days}天前" if days > 0 else "较上次"
                if abs(diff) < 0.05:
                    self.hero_compare.setText(f"{when}保持不变")
                elif diff < 0:
                    self.hero_compare.setText(
                        f"{when}下降了 {abs(diff):.2f} 公斤，继续保持！")
                else:
                    self.hero_compare.setText(f"{when}上升了 {diff:.2f} 公斤")
            else:
                self.hero_compare.setText("再记录一次即可开始对比")
        else:
            self.hero_date.setText("--")
            self.hero_weight.setText("--")
            self._metric_rows["BMI"][1].setText("--")
            self._metric_rows["体脂率"][1].setText("--")
            self.hero_compare.setText("点击右上角「＋ 记录体重」开始打卡")

        self._update_goal(data)
        self._update_charts(data)
        self._update_list(data)

    def _update_goal(self, data: list[dict]) -> None:
        has_goal = self._goal > 0 and len(data) >= 1
        self.goal_box.setVisible(has_goal)
        self.hero_no_goal.setVisible(not has_goal)
        if not has_goal:
            return
        initial = data[0]
        current = data[-1]["weight"]
        self.goal_init_val.setText(f"{initial['weight']:.2f}")
        d0 = QDate.fromString(initial["date"], "yyyy-MM-dd")
        self.goal_init_date.setText(d0.toString("yyyy/MM/dd") if d0.isValid() else initial["date"])
        self.goal_target_val.setText(f"{self._goal:.2f}")
        gd = QDate.fromString(self._goal_date, "yyyy-MM-dd")
        self.goal_target_date.setText(gd.toString("yyyy/MM/dd") if gd.isValid() else "")
        span = self._goal - initial["weight"]
        ratio = (current - initial["weight"]) / span if abs(span) > 1e-6 else 0
        self.goal_progress.set_progress(ratio, f"{max(0.0, min(1.0, ratio)) * 100:.1f}%")
        days = QDate.currentDate().daysTo(d0)
        days = abs(min(0, days)) + 1
        verb = "减重" if self._goal < initial["weight"] else "增重"
        self.goal_day_lbl.setText(f"{verb}第 {days} 天")

    def _update_charts(self, data: list[dict]) -> None:
        labels, weights, subs, _ = _aggregate(data, self._range)
        self.weight_chart.set_data(weights, labels, color="amber", decimals=2,
                                   sub_labels=subs)

        if self._height > 0 and weights:
            bmis = [round(w / (self._height / 100) ** 2, 2) for w in weights]
            self.bmi_chart.set_data(bmis, labels, color="green", decimals=2,
                                    sub_labels=subs)
            self.bmi_card.setVisible(True)
        else:
            self.bmi_chart.set_data([])
            self.bmi_card.setVisible(False)

    @staticmethod
    def _delta_for(rec: dict, data: list[dict]) -> float | None:
        """返回该记录相对其前一条的体重变化；首条返回 None。"""
        idx = next((i for i, r in enumerate(data) if r["id"] == rec["id"]), -1)
        if idx <= 0:
            return None
        return rec["weight"] - data[idx - 1]["weight"]

    def _update_list(self, data: list[dict]) -> None:
        if not data:
            # 一条都没有时要留一句话：这块地方空着，看着像界面坏了，
            # 而不像「还没有记录」。按钮名字和顶上那颗对齐。
            empty = QLabel("还没有体重记录，点上面「＋ 记录体重」记第一条。")
            empty.setObjectName("Muted")
            self.list_layout.insertWidget(self.list_layout.count() - 1, empty)
            return
        for d in reversed(data[-50:]):
            row = widgets.Card()
            lay = row.body()
            lay.setContentsMargins(18, 14, 18, 14)
            lay.setSpacing(8)

            top = QHBoxLayout()
            top.setSpacing(8)
            date_lbl = QLabel(_record_time(d))
            date_lbl.setObjectName("Meta")
            top.addWidget(date_lbl)
            weight_lbl = QLabel(f"{d['weight']:.2f} 公斤")
            weight_lbl.setObjectName("Strong")
            top.addWidget(weight_lbl)
            delta = self._delta_for(d, data)
            if delta is not None:
                if abs(delta) < 0.05:
                    mark, color = "— 不变", "muted"
                elif delta < 0:
                    mark, color = f"↓ {abs(delta):.2f}", "green"
                else:
                    mark, color = f"↑ {delta:.2f}", "red"
                dlbl = QLabel(mark)
                dlbl.setObjectName("Meta")
                dlbl.setStyleSheet(
                    f"color: {theme.get(color)}; font-weight: 600;")
                top.addWidget(dlbl)
            top.addStretch(1)
            edit_btn = QPushButton("编辑")
            edit_btn.setObjectName("Ghost")
            edit_btn.setCursor(Qt.PointingHandCursor)
            edit_btn.clicked.connect(lambda _, i=d["id"]: self._edit(i))
            del_btn = QPushButton("删除")
            del_btn.setObjectName("IconBtn")
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.clicked.connect(lambda _, i=d["id"]: self._delete(i))
            top.addWidget(edit_btn)
            top.addWidget(del_btn)
            lay.addLayout(top)

            if d.get("body_fat") or self._height > 0:
                sub = QHBoxLayout()
                sub.setSpacing(8)
                if d.get("body_fat"):
                    fat_lbl = QLabel(f"体脂 {d['body_fat']:.2f}%")
                    fat_lbl.setObjectName("Meta")
                    sub.addWidget(fat_lbl)
                if self._height > 0:
                    bmi = d["weight"] / (self._height / 100) ** 2
                    cat, color = _bmi_category(bmi)
                    sub.addWidget(widgets.Tag(f"BMI {bmi:.2f}·{cat}", color))
                sub.addStretch(1)
                lay.addLayout(sub)

            self.list_layout.insertWidget(self.list_layout.count() - 1, row)
