"""日历数据模型：可见性过滤、按日期分组、视图公用工具与拖拽 mime。"""
from __future__ import annotations

import json
from datetime import date, timedelta

from PySide6.QtCore import QDate, QTime

from ... import dateparse, db, services, theme
from . import feeds

# 拖拽 mime：携带「任务 id + 原始周期日」，未排期任务周期日为空
MIME_CAL = "application/x-lifesystem-cal"

# 视图：与滴答清单一致的五种，日/周/月/多日/多周
VIEWS = [
    ("day", "日", "D/1"),
    ("week", "周", "W/2"),
    ("month", "月", "M/3"),
    ("days5", "多日", "5 天"),
    ("weeks2", "多周", "2 周"),
]
VIEW_NAMES = {k: n for k, n, _ in VIEWS}

# 四象限过滤器：与待办页 QUADRANTS 同一套语义（值即优先级）
QUADRANTS = [
    ("3", "重要且紧急", "red"),
    ("2", "紧急不重要", "amber"),
    ("1", "重要不紧急", "blue"),
    ("0", "不重要不紧急", "muted"),
]

# 优先级 → 主题色键（色条底色取该色的柔和版，与滴答一致）
PRIO_COLOR = {0: "muted", 1: "blue", 2: "amber", 3: "red"}

_SETTING_KEY = "cal_filter"


# ---------------------------------------------------------------- 日期工具
def from_qd(d: QDate) -> date:
    return date(d.year(), d.month(), d.day())


def iso(d: QDate) -> str:
    return from_qd(d).isoformat()


def parse(s: str) -> QDate:
    d = QDate.fromString(s or "", "yyyy-MM-dd")
    return d if d.isValid() else QDate.currentDate()


def sunday_week_start(d: QDate) -> QDate:
    """滴答清单的周以周日为首。"""
    return d.addDays(-(d.dayOfWeek() % 7))


def month_matrix(year: int, month: int) -> list[list[QDate]]:
    """月视图的日期矩阵：周日为首，行数按当月实际占用的整周数。"""
    first = QDate(year, month, 1)
    if not first.isValid():
        return []
    start = sunday_week_start(first)
    total = first.daysInMonth()
    # 从当月 1 号所在的周日开始，覆盖到当月最后一天所在的周六
    weeks = (start.daysTo(first.addDays(total - 1)) // 7) + 1
    rows: list[list[QDate]] = []
    for r in range(weeks):
        rows.append([start.addDays(r * 7 + c) for c in range(7)])
    return rows


def month_span(year: int, month: int) -> tuple[QDate, QDate]:
    rows = month_matrix(year, month)
    return rows[0][0], rows[-1][-1]


# ---------------------------------------------------------------- 配色
def bar_color(priority: int, done: bool = False) -> tuple[str, str]:
    """色条的 (底色, 文字色)。滴答的色条是优先级的浅色调，不是纯色块。

    实测参考图：蓝条 #92acfc ≈ 白→滴答蓝 混 55%，无优先级是中性灰 #cbcbcb。
    用色板的 border_strong 调出来的灰偏蓝又太浅，日视图白底下几乎看不见。
    """
    key = PRIO_COLOR.get(int(priority or 0), "muted")
    dark = theme.is_dark()
    if key == "muted":
        return ("#3a3d4a" if dark else "#cbcbcb",
                theme.get("muted") if done
                else ("#c8cbe0" if dark else "#535353"))
    bg = theme.lerp_color(theme.get("surface"), theme.get(key),
                          0.32 if dark else 0.55)
    fg = theme.get("muted" if done else "text_hi")
    return bg, fg


def bar_at(view, pos):
    """视图里某个坐标上的那条色条 / 时间块（承载 ``.row`` 的那个控件）。

    右键菜单要的是「点中了哪条」，而色条嵌在格子、格子又在滚动视口里，
    一层层往上找比给每个中间层都加一条转发信号省事。
    """
    w = view.childAt(pos)
    while w is not None and not hasattr(w, "row"):
        w = w.parentWidget()
    return w


REPEAT_CN = {"daily": "每天", "workday": "每个工作日", "weekly": "每周",
             "biweekly": "每两周", "monthly": "每月", "yearly": "每年"}
_WD_CN = "日一二三四五六"

# 「算法复习」「八股复习」这两个清单里的待办是刷题页按艾宾浩斯排出来的，
# 日历只负责看，不负责挪：改了日期，题目那边的 next_review 不会跟着动，
# 而启动对账只补「被删/被勾掉」的，不会把被改动的日期改回来 —— 两边就悄悄
# 对不上了。判据用清单名（就是这两个页写入的地方），不用查库：
# 每次 reload 每格每条都要问一次，那里省下来的是实打实的耗时。
TRAINER_LISTS = {services.ALGO_LIST_NAME: services.TRAINER_PAGES["algo"],
                 services.INTERVIEW_LIST_NAME: services.TRAINER_PAGES["interview"]}


def trainer_of(row: dict) -> str:
    """这条是哪个刷题页排的（返回页名），不是就返回空串。"""
    return TRAINER_LISTS.get(row.get("list_name") or "", "")


def trainer_drag_blocked(row: dict, global_pos) -> bool:
    """想拖一条复习待办时调这个：True = 已经拦下来了。

    拦了又不说原因，用户只会以为「这日历怎么拖不动」，所以在鼠标位置顶一句
    提示 —— QToolTip 非阻塞、自动消失，不像弹层那样要点掉，也不会卡在
    鼠标事件里。
    """
    who = trainer_of(row)
    if not who:
        return False
    from PySide6.QtWidgets import QToolTip
    QToolTip.showText(global_pos,
                      f"这条的日期由「{who}」页排（艾宾浩斯），"
                      "在日历里改会和那边对不上", None)
    return True


def row_tooltip(row: dict) -> str:
    """色条 / 色块上悬停看到的那几行字。

    月视图一格最多摆四条、标题还会被省略号截掉，不悬停看全文就只能点开卡片；
    时间轴里的块被别的块挤窄时同理。内容全部取自行数据、不再查库 ——
    每次 reload 每格每条都要建一次 tooltip，那里省下来的是实打实的耗时。
    """
    lines = [row.get("title") or "（无标题）"]
    head = []
    d = row.get("date") or ""
    if d:
        qd = parse(d)
        head.append(f"{qd.month()}月{qd.day()}日 周{_WD_CN[qd.dayOfWeek() % 7]}")
    if row.get("time"):
        t = row["time"]
        mins = int(row.get("duration") or 0)
        end = QTime.fromString(t, "HH:mm")
        span = dateparse.human_time(t)
        if mins and end.isValid():
            e = end.addSecs(mins * 60)
            over = " +1天" if e < end else ""
            span += f" - {dateparse.human_time(f'{e.hour():02d}:{e.minute():02d}')}"\
                    f"{over}"
        head.append(span)
    rep = REPEAT_CN.get(row.get("repeat") or "")
    if rep:
        head.append(f"{rep}重复")
    if head:
        lines.append("　".join(head))
    tail = [f"清单 {row['list_name']}" if row.get("list_name") else ""]
    tags = "、".join(t["name"] for t in (row.get("tags") or [])[:4])
    if tags:
        tail.append(f"标签 {tags}")
    done, total = int(row.get("sub_done") or 0), int(row.get("sub_total") or 0)
    if total:
        tail.append(f"子任务 {done}/{total}")
    line = "　".join(x for x in tail if x)
    if line:
        lines.append(line)
    note = (row.get("note") or "").strip().splitlines()
    if note:
        first = note[0]
        lines.append(first if len(first) <= 60 else first[:60] + "…")
    if row.get("done"):
        lines.append("已完成")
    who = trainer_of(row)
    if who:
        lines.append(f"日期由{who}页排，在日历里不能改")
    return "\n".join(lines)


def row_bar_color(row: dict) -> tuple[str, str]:
    """一条日历条目该画成什么颜色。月视图色条和时间轴色块共用。

    订阅事件（.ics 拉来的）没有优先级，用它所属订阅的颜色。
    """
    if row.get("feed"):
        return (theme.lerp_color(theme.get("surface"), row["feed_color"],
                                 0.32 if theme.is_dark() else 0.55),
                theme.get("text_hi"))
    return bar_color(row["priority"], bool(row["done"]))


# ---------------------------------------------------------------- 可见性过滤
class CalFilter:
    """右侧面板的可见性状态：所有 / 勾选清单 / 勾选标签 / 四象限 / 已完成。

    持久化到 settings，重开应用还是上次的筛选。
    """

    def __init__(self):
        self.mode = "all"            # all | custom
        self.lists: set[str] = set()
        self.tags: set[str] = set()
        self.quadrant = ""           # '' | '0'..'3'
        self.show_done = True
        self.load()

    def reset(self) -> None:
        self.mode = "all"
        self.lists.clear()
        self.tags.clear()
        self.quadrant = ""

    @property
    def narrowing(self) -> bool:
        """是否处于「按清单/标签收窄」的状态。"""
        return self.mode == "custom" and bool(self.lists or self.tags)

    def keep(self, r: dict) -> bool:
        if r.get("feed"):
            # 订阅事件不归这套筛选管：它自己的「显示/隐藏」开关在日历订阅里，
            # 而且它没有优先级、也不会被勾成已完成
            return True
        if r["done"] and not self.show_done:
            return False
        if self.narrowing:
            in_list = r["list_name"] in self.lists
            in_tag = bool(self.tags) and any(
                t["name"] in self.tags for t in r.get("tags") or [])
            if not (in_list or in_tag):
                return False
        if self.quadrant and str(int(r["priority"] or 0)) != self.quadrant:
            return False
        return True

    def save(self) -> None:
        db.set_setting(_SETTING_KEY, json.dumps({
            "mode": self.mode, "lists": sorted(self.lists),
            "tags": sorted(self.tags), "quadrant": self.quadrant,
            "show_done": self.show_done}, ensure_ascii=False))

    def load(self) -> None:
        raw = db.get_setting(_SETTING_KEY)
        if not raw:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        self.mode = data.get("mode", "all")
        self.lists = set(data.get("lists") or [])
        self.tags = set(data.get("tags") or [])
        self.quadrant = data.get("quadrant") or ""
        self.show_done = bool(data.get("show_done", True))


def group_by_date(start: QDate, end: QDate,
                  filt: CalFilter) -> dict[str, list[dict]]:
    """区间内的日历条目，按显示日期分组（月视图 / 时间轴共用）。

    待办任务之外还并进来「日历订阅」的只读事件（.ics 拉来的），
    这样五种视图都能看到订阅，而不用每个视图各接一遍。
    """
    rows = services.cal_occurrences(iso(start), iso(end), include_notes=True)
    out: dict[str, list[dict]] = {}
    for r in rows:
        if filt.keep(r):
            out.setdefault(r["date"], []).append(r)
    for day, feed_rows in feeds.events_in(iso(start), iso(end)).items():
        for r in feed_rows:
            if filt.keep(r):
                out.setdefault(day, []).append(r)
    for v in out.values():
        v.sort(key=lambda r: (r["time"] or "99:99", r["sort"], r["id"]))
    return out


def unscheduled(filt: CalFilter) -> list[dict]:
    """安排任务抽屉的数据源（未排期任务）。"""
    rows = [t for t in services.cal_unscheduled(include_notes=True)]
    if filt.narrowing:
        rows = [t for t in rows
                if (t.get("list_name") or "收集箱") in filt.lists
                or any(x["name"] in filt.tags for x in t.get("tags") or [])]
    if filt.quadrant:
        rows = [t for t in rows if str(int(t.get("priority") or 0)) == filt.quadrant]
    return rows


def shift_days(d: QDate, n: int) -> QDate:
    return from_qd(from_qd(d) + timedelta(days=n))
