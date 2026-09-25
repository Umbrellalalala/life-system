"""待办清单：对齐滴答清单的三栏界面（导航 + 任务列表 + 详情面板）。

- 左栏：智能清单（今天 / 最近7天 / 收集箱 / 全部）→ 清单（含文件夹）→ 标签
  → 过滤器（四象限 + 自定义）→ 已完成 / 垃圾桶
- 中栏：视图标题 + 滴答式快速添加 + 分组任务列表（已过期可一键顺延、今日打卡）
- 右栏：任务详情，改动即时落盘（滴答没有「保存」按钮）
- 优先级四档：无 / 低 / 中 / 高，用勾选框描边色表达（滴答的做法）
- 快速添加识别：日期时间 + ``#清单`` + ``@标签`` + ``!`` 优先级

图标一律 QPainter 自绘（见 :mod:`lifeapp.todo_icons`），不用 emoji：
emoji 的字形由系统决定、颜色固定，是「不像滴答」最明显的破绽。
"""
from __future__ import annotations

import re

from PySide6.QtCore import (
    Qt, QDate, QDateTime, QTime, QSize, QPointF, QUrl, Signal, QTimer, QMimeData,
    QRectF, QPoint, QEvent, QVariantAnimation, QEasingCurve,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPen, QDrag, QDesktopServices,
    QTextCursor, QPixmap,
    QKeySequence, QShortcut,
    QTextBlockFormat, QTextListFormat,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QLabel, QLineEdit,
    QPushButton, QListWidget, QTextEdit, QScrollArea, QCheckBox,
    QSizePolicy, QApplication, QSplitter,
)

from .. import dateparse, db, popups, services, sounds, theme, widgets
from ..focus_ui import POPUP_FLAGS
from ..todo_icons import (TickIcon, ColorDot, PrioCheckBox, FlagButton,
                           PRIO_COLOR_KEY)

# 自定义拖拽 mime：携带任务 id，用于列表内排序 + 拖到左侧导航改属性
MIME_TODO = "application/x-lifesystem-todo"
# 左栏导航行自己的拖拽：携带那一行的 key，用于组内上下排序
MIME_NAV = "application/x-lifesystem-nav"

# 智能栏那四行 + 底部两行的可拖顺序（存在 settings 里）。分成两组：上面那组是
# 「视图」，下面那组是「已完成 / 垃圾桶」，互不串组。
NAV_SMART = ("soon7", "today", "quad", "inbox")
NAV_FOOT = ("done", "trash")
# 四个优先级过滤器是写死的（不在 todo_filters 表里），和自建过滤器排在同一串里，
# 所以整串的顺序另外存在 settings 的 todo_nav_filter。
NAV_SETTINGS = {"smart": "todo_nav_smart", "foot": "todo_nav_foot",
                "filter": "todo_nav_filter"}


def _nav_group(key: str) -> str:
    """这一行属于哪个可排序的组。跨组不许互拖（清单不能拖进标签区）。"""
    if key.startswith(("list:", "folder:")):
        return "list"
    if key.startswith("tag:"):
        return "tag"
    if key.startswith("filter:") or key in QUAD_KEYS:
        return "filter"
    if key in NAV_SMART:
        return "smart"
    if key in NAV_FOOT:
        return "foot"
    return ""


def _nav_parent(key: str) -> str:
    """同组里还得同「一串」：子清单之间、顶层清单之间、子标签之间才互拖。"""
    if key.startswith("list:"):
        lst = next((l for l in services.list_all(include_archived=True)
                    if l["name"] == key[5:] and l.get("kind") != "folder"), None)
        return str(int((lst or {}).get("folder_id") or 0))
    if key.startswith("folder:"):
        return "0"
    if key.startswith("tag:"):
        try:
            tid = int(key[4:])
        except ValueError:
            return "0"
        tag = next((t for t in services.tag_all() if t["id"] == tid), None)
        return str(int((tag or {}).get("parent_id") or 0))
    return "0"

PRIORITY_META = {
    0: ("无", "muted"),
    1: ("低", "blue"),
    2: ("中", "amber"),
    3: ("高", "red"),
}
# 重复规则的取值 / 标签统一在 services.REPEAT_OPTIONS（日历的周期展开也读同一份），
# 这里不再抄一份，免得两边词表悄悄跑偏。

# 时长档位（分钟）。日历的时间轴按 todos.duration_min 排块，这里给它一个入口。
DURATION_OPTIONS = [15, 30, 45, 60, 90, 120]

# 行内检查事项：只列前几条，剩下的靠「还有 N 项」进详情看。
# 滴答也是这个做法 —— 一条任务二十个勾选项会把整屏占满，看不见别的任务。
SUB_INLINE_MAX = 3
# 「还有 N 项」那行的缩进：行本身有 10px 左边距，49 + 10 = 59 正好落在子任务文字上
SUB_TEXT_INDENT = 49


# 待办里可点开的链接：从备注 / 描述里捞第一个 http，行上出🔗。
_URL_RE = re.compile(r"""https?://[^\s<>"'）)，。]+""")


def _first_url(data: dict) -> str:
    for field in ("note", "title"):
        m = _URL_RE.search((data.get(field) or ""))
        if m:
            return m.group(0)
    return ""


def _open_url(url: str, parent=None) -> None:
    if not url:
        return
    if not QDesktopServices.openUrl(QUrl(url)):
        popups.notify(parent, "打不开链接",
                      "系统浏览器没能打开：\n%s" % url, danger=True)


# 复习待办分两页（算法 / 八股），勾掉时问的话不一样，但流程完全相同。
_REVIEW_ASK = {
    "algo": ("这道题这次独立做出来了吗？", ["独立做出来了", "没独立做出来"]),
    "interview": ("这道八股这次答对了吗？", ["答对了", "没答上来"]),
}
# 勾整条大任务 = 这天剩下的题目按同一个结论一起结掉
_REVIEW_ASK_ALL = {
    "algo": ("这天的 %d 道题一起记，按哪个结论？",
             ["都独立做出来了", "都没独立做出来"]),
    "interview": ("这天的 %d 道八股一起记，按哪个结论？",
                  ["都答对了", "都没答上来"]),
}


def _review_ask_sub(parent, sub: dict) -> bool:
    """勾掉一条复习子任务后问一句结论，据此重排下一个记忆点。

    返回 False 表示用户没答，调用方要把这条退回未勾：结论没记下来就不能
    往前走排期，否则这道题会被默认当成「做出来了」，越排越远。
    """
    kind = services.review_kind_of_sub(sub["id"])
    if not kind:
        return True                     # 不是复习条目，正常放行
    ask, opts = _REVIEW_ASK[kind]
    pick, ok = popups.get_item(parent, "复习结论", ask, opts)
    if not ok:
        services.subtask_update(sub["id"], done=0)
        return False
    # 重排会把这条子任务摘掉（下一档排到别的日子），大任务空了自己收尾
    services.review_resolve_sub(sub["id"], independent=(pick == opts[0]))
    sounds.play("answer_correct" if pick == opts[0] else "answer_wrong")
    return True


def _review_ask_group(parent, todo_id: int) -> bool:
    """勾整条复习大任务：把这天还没做的题目按同一个结论逐个结掉。

    没有未勾的复习条目时直接放行 —— 那时它就是条普通待办（用户自己往大任务
    下加过检查事项，或者题目都毕业后手动勾一遍收尾）。
    """
    pend = services.review_pending_subs(todo_id)
    if not pend:
        return True
    kind = pend[0]["kind"]      # 一天一门课一个大任务，不会混
    ask, opts = _REVIEW_ASK_ALL[kind]
    pick, ok = popups.get_item(parent, "复习结论", ask % len(pend), opts)
    if not ok:
        return False
    indep = pick == opts[0]
    for entry in pend:
        services.review_resolve_sub(entry["sub"]["id"], independent=indep)
    sounds.play("answer_correct" if indep else "answer_wrong")
    return True


# TickMenu 里三种「不是一行普通项」的东西用这三个标记区分（见 TickMenu 的 docstring）。
# 取值带 NUL，任何真实菜单值都不可能撞上。
SEP = "\x00sep"          # 一条分隔线
CAP = "\x00cap"          # 一段小标题（日期 / 优先级）
STRIP = "\x00strip"      # 一整格自定义控件（横排图标条）


def _prio_items() -> list[tuple]:
    """四档优先级菜单项，旗子颜色和行上勾选框的描边色同一套来源
    （todo_icons.PRIO_COLOR_KEY），免得两处各写一份颜色漂掉。"""
    return [(3, "flag", "高优先级", PRIO_COLOR_KEY[3]),
            (2, "flag", "中优先级", PRIO_COLOR_KEY[2]),
            (1, "flag", "低优先级", PRIO_COLOR_KEY[1]),
            (0, "circle", "无优先级", "border_strong")]


def _human_reminder(rem: str) -> str:
    """提醒存的是「YYYY-MM-DD HH:MM」，界面上只留「09-20 09:00」。

    不能无条件 [5:]：这一列是自由文本，日历那边也写它，形状不对时切片会
    把「onTime」显示成「me」这种鬼话。
    """
    if not rem:
        return ""
    head = rem.split(" ")[0]
    if len(head) == 10 and head[4] == "-" and head[7] == "-":
        return rem[5:].replace(" ", " ")
    return rem


def _human_duration(mins: int) -> str:
    """分钟数 → 「45 分钟」/「1 小时」/「1.5 小时」。"""
    mins = int(mins or 0)
    if mins <= 0:
        return ""
    if mins < 60:
        return f"{mins} 分钟"
    hours = mins / 60
    return f"{int(hours)} 小时" if mins % 60 == 0 else f"{hours:g} 小时"


GROUP_ORDER = ["overdue", "today", "soon", "later", "nodate", "done"]
WEEKDAY_CN = "周一 周二 周三 周四 周五 周六 周日"

# 四象限过滤器（滴答的「重要紧急四象限」）：key / 名称 / 文字色键 / 方块色
# 名称和滴答象限页对齐：中=重要不紧急、低=不重要但紧急（以前这两条写反了）。
# 最后一格滴答画的是白底描边方块，文字也是常规黑，不能用 muted 灰
QUADRANTS = [
    ("p3", "重要且紧急", "red", "#f0435f"),
    ("p2", "重要不紧急", "amber", "#ed9a12"),
    ("p1", "不重要但紧急", "blue", "#3d8bff"),
    ("p0", "不重要不紧急", "", "#ffffff"),
]
# 象限页每格的角标：滴答用罗马数字 Ⅰ–Ⅳ，第四格是绿的（和侧栏那个白底方块不是一回事）
QUAD_PAGE = {"p3": ("Ⅰ", "red"), "p2": ("Ⅱ", "amber"),
             "p1": ("Ⅲ", "blue"), "p0": ("Ⅳ", "green")}
# 这四行在左栏「过滤器」段里，key 就是 p0-p3（_nav_group 认它们为一组）
QUAD_KEYS = tuple(k for k, *_ in QUADRANTS)
# 清单 / 标签可选的自定义色（滴答的调色板）
PALETTE = [
    ("灰", "#8b8fa3"), ("红", "#f0435f"), ("橙", "#ed9a12"), ("黄", "#e8c33a"),
    ("绿", "#0db987"), ("蓝", "#3d8bff"), ("紫", "#8b5cf6"), ("彩", "#ff6ba6"),
]
# 旧数据里 tags.color 存的是语义键，这里换算成十六进制给圆点用
KEY_HEX = {"red": "#f0435f", "amber": "#ed9a12", "blue": "#3d8bff",
           "green": "#0db987", "muted": "#8b8fa3", "accent": "#00a5ff"}


def color_hex(name: str) -> str:
    """语义色键或已是十六进制 → 十六进制色值。"""
    if not name:
        return ""
    if name.startswith("#"):
        return name
    return KEY_HEX.get(name, theme.get(name) or "")


def _today_str() -> str:
    return QDate.currentDate().toString("yyyy-MM-dd")


def _weekday_cn(d: QDate) -> str:
    return WEEKDAY_CN[(d.dayOfWeek() - 1) * 3:(d.dayOfWeek() - 1) * 3 + 2]


def _settled(t: dict) -> bool:
    """这条算不算「已经翻篇」：做完了 + 放弃了的。

    角标、列表路由、组标题必须共用这一个判据，否则放弃的任务会一边在角标里
    计数、一边在列表里根本不出现，两边的数字怎么数都对不上。
    """
    return bool(t["done"]) or bool(t.get("abandoned"))


def _review_date_guard(owner, todo_id: int) -> bool:
    """复习大任务那天的日期是刷题页排出来的，待办这边改不得。

    挪一下 `todos.due_date`，`*_reviews.due_date` 和 `problems.next_review`
    就和待办对不上了；而 `_review_day_task` 是信映射行的 —— 别的日子排出来的题目
    会跟着挂到这条上，「一天一门课一条」的不变量当场破掉。
    日历同一件事是直接灰掉（`task_card._apply_lock`），这里给一句去哪儿改。
    """
    who = services.review_page_of_todo(todo_id)
    if not who:
        return True
    popups.notify(owner, "这条日期改不了",
                  f"这天的复习是「{who}」页排出来的。\n"
                  f"要挪日期，请到「{who}」页改那道题的下次复习。")
    return False


def _group_label(key: str, items: list) -> str:
    """分组标题。滴答会把日期分组写成「今天, 周六」这种带星期的形式。"""
    today = QDate.currentDate()
    if key == "overdue":
        return "已过期"
    if key == "today":
        return f"今天, {_weekday_cn(today)}"
    if key == "soon":
        return "未来7天"
    if key == "later":
        return "稍后"
    if key == "nodate":
        return "待安排"
    if key == "done":
        # 滴答的写法：这一组里真出现了放弃项，标题就带上「& 已放弃」，
        # 免得放弃的东西看着像被做完了
        if any(int(t.get("abandoned") or 0) and not int(t.get("done") or 0)
               for t in items):
            return "已完成 & 已放弃"
        return "已完成"
    if key == "checkin":
        return f"今日打卡, {_weekday_cn(today)}"
    if key.startswith("bylist:"):
        return key[7:] or "收集箱"
    if key.startswith("bytag:"):
        return key[6:] or "无标签"
    if key.startswith("prio:"):
        return PRIORITY_META.get(int(key[5:] or 0), ("其它", ""))[0]
    if key == "flat":
        return ""
    return key


def _holidays():
    """假期角标那个模块。calendar 包反过来要 import 本模块，只能在用时取。"""
    from .calendar import holidays
    return holidays


def _detail_date_label(due: str, due_time: str = "", end: str = "") -> str:
    """详情面板日期胶囊的文案：滴答对过期任务写「5天前, 9月14日」。

    `end` 非空且不等于起始日时写成跨天「今天, 11月1日-11月3日」。
    """
    d = QDate.fromString(due, "yyyy-MM-dd")
    if not d.isValid():
        return due
    left = QDate.currentDate().daysTo(d)
    date_part = f"{d.month()}月{d.day()}日"
    e = QDate.fromString(end, "yyyy-MM-dd") if end else QDate()
    if e.isValid() and e > d:
        tail = f"{e.month()}月{e.day()}日"
        date_part += f"{e.year()}年{tail}" if e.year() != d.year() else f"-{tail}"
    if left < 0:
        head = f"{-left}天前"
    elif left == 0:
        head = "今天"
    elif left == 1:
        head = "明天"
    elif left == 2:
        head = "后天"
    else:
        head = _weekday_cn(d)
    text = f"{head}, {date_part}"
    if due_time:
        text += f" {dateparse.human_time(due_time)}"
    return text


def _date_state(due: str, due_time: str = "", countdown: bool = False,
                end: str = "") -> tuple[str, str]:
    """截止日期/时间 → (展示文本, 状态色键)。

    countdown = ⋯ 菜单里的「显示倒数日」：滴答把「3月5日」换成「剩余N天」，
    逾期那条换成「已逾期N天」。今天/明天本来就是相对说法，不再套一层。
    """
    if not due:
        return "", "normal"
    d = QDate.fromString(due, "yyyy-MM-dd")
    if not d.isValid():
        return due, "normal"
    e = QDate.fromString(end, "yyyy-MM-dd") if end else QDate()
    if e.isValid() and e > d:
        # 跨天的不套「今天 / 剩余N天」那套相对说法，直接写区间；
        # 逾期要过了结束日才算，按起始日判会提前红起来
        text = (f"{d.month()}月{d.day()}日-"
                + (f"{e.year()}年" if e.year() != d.year() else "")
                + f"{e.month()}月{e.day()}日")
        if due_time:
            text += f" {dateparse.human_time(due_time)}"
        return text, ("overdue" if QDate.currentDate().daysTo(e) < 0
                      else "normal")
    left = QDate.currentDate().daysTo(d)
    if left < 0:
        text, state = (f"已逾期{-left}天" if countdown
                       else f"{d.month()}月{d.day()}日"), "overdue"
    elif left == 0:
        text, state = "今天", "today"
    elif left == 1:
        text, state = "明天", "normal"
    else:
        text = f"剩余{left}天" if countdown else f"{d.month()}月{d.day()}日"
        state = "normal"
    if due_time:
        text += f" {dateparse.human_time(due_time)}"
    return text, state


def _plain_preview(note: str) -> str:
    """描述 → 「显示详细」那一行：去掉 markdown 记号，取第一行非空文本。"""
    for raw in (note or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)      # 图片
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)  # 链接留文字
        line = re.sub(r"^#{1,6}\s*", "", line)                # 标题
        line = re.sub(r"^[-*+]\s+|^\d+\.\s+", "", line)       # 列表符号
        line = re.sub(r"^>\s*", "", line)                     # 引用
        line = re.sub(r"`{1,3}", "", line)                    # 行内代码 / 围栏
        line = re.sub(r"\s*\*\*?\s*|\s*__?\s*", "", line)     # 加粗斜体
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            return line
    return ""


def _icon_color_for_priority(prio: int) -> str:
    return {1: "blue", 2: "amber", 3: "red"}.get(int(prio or 0), "muted")


# QLineEdit::cursorRect() 与真实排版原点之间的固定偏差（逻辑像素）。padding 只
# 作用在绘制路径上，cursorRect 走的是另一套 contents 计算，报出来的原点偏左。
# 用「把药丸隐形、重画笔色设成正文色」的办法扫过 3~7：5.0 起整行墨迹外接框
# 与不高亮那版完全重合，3.0 会左偏 3 物理像素（药丸右缘露出一截黑弧）。
_NLP_ANCHOR_FIX = 5.0

# 左栏导航的默认 / 最大宽度（像素）。最大放宽到 360 才拖得开。
NAV_DEFAULT_W = 204
NAV_MAX_W = 360

# 导航日历图标里的星期缩写，按 QDate.dayOfWeek()（1=周一）取。
# 滴答用的是英文两字母，不跟系统 locale 走，这里也固定写死。
_WEEK_ABBR = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")


class HighlightLineEdit(QLineEdit):
    """把识别出的时间 / #清单 / @标签 画成滴答那样的小药丸。

    滴答的配色是「不透明的淡靛底 + 同色系蓝字」，而 QLineEdit 只会用自己的纯色
    画字。早先只盖一层半透明底，颜色发青、而且一路铺满输入框的高度，看着像选中
    块不像药丸。现在改成：让父类先把整行画完，再用不透明底色把命中段盖掉、按同
    一套 char_x 坐标把字重画成蓝色 —— 底和字用的是同一个取坐标的函数，不会错位。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._spans: list[tuple[int, int]] = []

    def set_spans(self, spans: list[tuple[int, int]]) -> None:
        if spans != self._spans:
            self._spans = spans
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        text = self.text()
        if not self._spans or not text:
            super().paintEvent(event)
            return
        fm = self.fontMetrics()
        box = self.cursorRect()
        pos = self.cursorPosition()
        # 以光标实际位置为锚换算任意字符的 x（自动兼容水平滚动）。
        # cursorRect 报出的文字原点比真正排版的位置偏左 3px：padding 走的是
        # QSS，只有绘制路径认它，cursorRect 那套算的是另一份 contents。
        # 不补这 3px，整颗药丸会左移，最后一个字的右半边盖不住、露出一截黑弧。
        cur_x = box.left() + _NLP_ANCHOR_FIX

        def char_x(n: int) -> float:
            n = max(0, min(n, len(text)))
            if n >= pos:
                return cur_x + fm.horizontalAdvance(text[pos:n])
            return cur_x - fm.horizontalAdvance(text[n:pos])

        baseline = box.top() + (box.height() - fm.height()) / 2 + fm.ascent()
        top = baseline - fm.ascent()
        hgt = float(fm.height())
        pad = 3.0
        hit = []
        for s, e in self._spans:
            s, e = max(0, min(s, len(text))), max(0, min(e, len(text)))
            if e > s:
                hit.append((s, e, char_x(s), char_x(e)))

        # 相邻两段之间的空格往往只有一个字宽，左右各留 3px 就会把两颗药丸粘成
        # 一条。所以边距最多加到「与邻段的中点」，保证中间始终留一道缝。
        edges = []
        for i, (s, e, x1, x2) in enumerate(hit):
            left = x1 - pad if i == 0 else max(x1 - pad, (hit[i - 1][3] + x1) / 2)
            right = x2 + pad if i == len(hit) - 1 else min(x2 + pad, (x2 + hit[i + 1][2]) / 2)
            # 光标贴着段首/段尾时那一侧不留边距，否则一整条光标会被底色盖掉
            if pos == s:
                left = x1
            if pos == e:
                right = x2
            edges.append((s, e, x1, x2, left, right))

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.get("nlp_bg")))
        for s, e, x1, x2, left, right in edges:
            p.drawRoundedRect(QRectF(left, top, right - left, hgt), 4, 4)
        p.end()

        super().paintEvent(event)

        # 再把命中段那一格用同色封掉、重画成蓝色。封的宽度取字身而不是药丸，
        # 所以不会碰到左右邻居；黑字也画不出药丸之外，正好盖干净。
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.get("nlp_bg")))
        for s, e, x1, x2, left, right in edges:
            # 封套也画成圆角、且只圈字身：方角会在药丸的圆角外头支出一小块
            # 同色方角（颜色一样但轮廓多出来），放大看就是药丸边缘发方。
            # 右端多封 1.5px —— 末字的墨迹会略微越过排版笔位，只封到 x2 会在
            # 药丸右缘留一道黑边；多这点落在下一个字的左内边距里，看不出来。
            p.drawRoundedRect(QRectF(x1, top, x2 - x1 + 1.5, hgt), 2, 2)
        p.setPen(QColor(theme.get("nlp_fg")))
        for s, e, x1, x2, left, right in edges:
            p.drawText(QPointF(x1, baseline), text[s:e])
        p.end()


class TagPickPopup(QFrame):
    """标签多选弹层：点一下勾上/取消，菜单不关，可以连着选几个。

    复用 TickMenu 的外观，但不能用 TickMenu —— 它点一项就 close，
    给任务加两个标签要点两次。
    """

    toggled = Signal(int)

    def __init__(self, tags: list[dict], on_task: set[int], parent=None,
                 width: int = 186):
        super().__init__(parent, POPUP_FLAGS)
        self.setObjectName("TickMenu")
        self.setAttribute(Qt.WA_TranslucentBackground)
        card = QFrame(self)
        card.setObjectName("TickMenuCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(5, 5, 5, 5)
        lay.setSpacing(1)
        if not tags:
            empty = QLabel("还没有标签")
            empty.setObjectName("TickMenuLabel")
            lay.addWidget(empty)
        self._rows: dict[int, _MenuRow] = {}
        for tg in tags:
            row = _MenuRow("tag", tg["name"], int(tg["id"]) in on_task,
                           False, card)
            row.clicked_.connect(lambda i=tg["id"]: self._fire(i))
            lay.addWidget(row)
            self._rows[int(tg["id"])] = row
        self.setFixedWidth(width)

    def set_checked(self, ids: set[int]) -> None:
        """只改勾选不重开菜单：连着勾两个标签时闪一下很扰。"""
        for i, row in self._rows.items():
            row.set_on(i in ids)

    def _fire(self, tag_id: int) -> None:
        self.toggled.emit(tag_id)

    def exec_at(self, global_pos: QPoint) -> None:
        self.adjustSize()
        self.move(global_pos)
        self.show()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.deleteLater()


class IconBtn(QPushButton):
    """带一枚自绘线性图标的按钮：没文字就居中，有文字就靠左留出图标位。

    日期弹层那排快捷按钮原来是 emoji 字符（☀ 🌅 🌙），Windows 上会渲染成彩色，
    和这个模块「一律线性图标」的取向直接冲突，也跟参考图的细线图标对不上。
    """

    def __init__(self, kind: str = "", size: int = 16, color_key: str = "muted",
                 text: str = "", parent=None):
        super().__init__(text, parent)
        self.icon_w = TickIcon(kind, size, color_key, self)
        self.icon_w.show()
        if text:
            self.setStyleSheet("padding-left: 24px; text-align: left;")
        self._place()

    def set_icon(self, kind: str) -> None:
        self.icon_w.set_kind(kind)

    def set_color_key(self, key: str) -> None:
        self.icon_w.set_color_key(key)

    def setText(self, text: str) -> None:  # noqa: N802
        # 图标位置取决于有没有文字，而文字常常是构造之后才设的（时间行）
        super().setText(text)
        if text:
            self.setStyleSheet("padding-left: 24px; text-align: left;")
        self._place()

    def _place(self) -> None:
        ic = self.icon_w
        if self.text():
            ic.move(5, (self.height() - ic.height()) // 2)
        else:
            ic.move((self.width() - ic.width()) // 2,
                    (self.height() - ic.height()) // 2)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place()


class MonthPickPopup(QFrame):
    """小月历：持续时间段里「开始 / 结束」那两格从这儿挑。

    不复用 DatePickerPopup 自己那张网格 —— 一个面板里两套字段共用一个网格，
    点了不知道在改哪一头；滴答也是再弹一层小月历。
    """

    picked = Signal(str)          # yyyy-MM-dd

    def __init__(self, date_s: str = "", parent=None):
        super().__init__(parent, POPUP_FLAGS)
        self.setObjectName("TickMenu")
        self.setAttribute(Qt.WA_TranslucentBackground)
        card = QFrame(self)
        card.setObjectName("TickMenuCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)
        d = QDate.fromString(date_s, "yyyy-MM-dd")
        self._sel = d if d.isValid() else QDate.currentDate()
        self._month = QDate(self._sel.year(), self._sel.month(), 1)

        head = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("CalHeader")
        head.addWidget(self.title)
        head.addStretch(1)
        for icon, cb in (("‹", lambda: self._shift(-1)),
                         ("○", self._goto_today),
                         ("›", lambda: self._shift(1))):
            b = QPushButton(icon)
            b.setObjectName("CalNav")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(cb)
            head.addWidget(b)
        lay.addLayout(head)
        wd = QHBoxLayout()
        wd.setSpacing(2)
        for name in ("日", "一", "二", "三", "四", "五", "六"):
            lbl = QLabel(name)
            lbl.setObjectName("CalWd")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            wd.addWidget(lbl, 1)
        lay.addLayout(wd)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        self._btns: list[QPushButton] = []
        for i in range(42):
            b = QPushButton()
            b.setObjectName("CalDay")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, ix=i: self._pick(ix))
            grid.addWidget(b, i // 7, i % 7)
            self._btns.append(b)
        lay.addLayout(grid)
        self.setFixedWidth(238)
        self._refresh()

    def _refresh(self) -> None:
        today = QDate.currentDate()
        self.title.setText(f"{self._month.month()}月 {self._month.year()}年")
        start = self._month.addDays(-(self._month.dayOfWeek() % 7))
        for i, btn in enumerate(self._btns):
            d = start.addDays(i)
            btn.setText(str(d.day()))
            btn._date = d  # noqa: SLF001 仅内部使用
            state = ("off" if d.month() != self._month.month()
                     else "sel" if d == self._sel
                     else "today" if d == today else "")
            widgets._apply_property(btn, "calState", state)

    def _shift(self, delta: int) -> None:
        self._month = self._month.addMonths(delta)
        self._refresh()

    def _goto_today(self) -> None:
        self._sel = QDate.currentDate()
        self._month = QDate(self._sel.year(), self._sel.month(), 1)
        self._refresh()

    def _pick(self, idx: int) -> None:
        self.picked.emit(self._btns[idx]._date.toString("yyyy-MM-dd"))
        self.close()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.deleteLater()


class TimePickPopup(QFrame):
    """时 / 分两列的小弹层：持续时间段那两个时刻框用。"""

    picked = Signal(str)          # HH:MM

    def __init__(self, time_s: str = "", parent=None):
        super().__init__(parent, POPUP_FLAGS)
        self.setObjectName("TickMenu")
        self.setAttribute(Qt.WA_TranslucentBackground)
        card = QFrame(self)
        card.setObjectName("TickMenuCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        h, m = (9, 0)
        if ":" in time_s:
            try:
                h, m = (int(x) for x in time_s.split(":")[:2])
            except ValueError:
                pass
        self._hours = self._col(lay, 24, h)
        self._mins = self._col(lay, 60, m)
        self.setFixedWidth(150)

    def _col(self, lay, count: int, at: int) -> QListWidget:
        lst = QListWidget()
        lst.setObjectName("TimeList")
        lst.setFixedHeight(132)
        lst.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for i in range(count):
            lst.addItem("%02d" % i)
        if 0 <= at < count:
            lst.setCurrentRow(at)
            # 打开就滚到当前值那一行，不然永远从 00 开始、看不出选的是几点
            QTimer.singleShot(0, lambda: lst.scrollToItem(lst.item(at)))
        lst.itemClicked.connect(lambda _it: self._commit())
        lay.addWidget(lst, 1)
        return lst

    def _commit(self) -> None:
        # 不关：点和时之后还要接着点分，一点就收等于永远只能选到整点。
        # 值本身是即时生效的，点外面弹层自己收（Qt.Popup）。
        self.picked.emit("%02d:%02d" % (max(self._hours.currentRow(), 0),
                                        max(self._mins.currentRow(), 0)))

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.deleteLater()


class DatePickerPopup(QFrame):
    """滴答清单式日期选择弹窗：快捷日期 + 月历 + 时间，确定/清除。

    `allow_range` 打开时顶部多一排「日期 / 时间段」分段切换，时间段那页是
    开始/结束 + 全天三行。只有真能把 end_date 存回库里的入口才给这一排 ——
    习惯的起始日、日历的单日卡片都没有范围可存，摆上去就是骗人点。
    """

    accepted = Signal(str, str, str, str)   # (开始日期, 开始时间, 结束日期, 结束时间)
    cleared = Signal()
    repeatPicked = Signal(str)      # 重复规则，"" = 不重复
    reminderPicked = Signal(str)    # 提醒的绝对时间串，"" = 无提醒

    def __init__(self, date: str = "", time_v: str = "", parent=None,
                 repeat: str = "", reminder: str = "",
                 end_date: str = "", end_time: str = "",
                 allow_range: bool = False):
        super().__init__(parent)
        self._repeat = repeat or ""
        self._reminder = reminder or ""
        self._reminder_choice = ""
        self.setObjectName("DatePickerPopup")
        self.setWindowFlags(POPUP_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumWidth(304)

        today = QDate.currentDate()
        self._selected = QDate.fromString(date, "yyyy-MM-dd") if date else today
        if not self._selected.isValid():
            self._selected = today
        self._time = time_v or ""
        e = QDate.fromString(end_date, "yyyy-MM-dd")
        self._end = e if (e.isValid() and e >= self._selected) else self._selected
        self._end_time = end_time or ""
        self._mode = "range" if (allow_range and end_date) else "date"
        first = QDate(self._selected.year(), self._selected.month(), 1)
        self._month = first

        # 根节点是 translucent 顶层窗口，QSS 背景画不上去 —— 底色垫在
        # 内层 card 上（和 TickMenu 一样），否则弹出来是透明的
        card = QFrame(self)
        card.setObjectName("DatePopupCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        root = QVBoxLayout(card)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        # 「日期 / 时间段」分段。第三轮里这个 tab 被删过，因为当时库里没有
        # end_date，点过去是一行「即将上线」—— 现在两端都齐了才放回来，
        # 而且存不下范围的那几个入口（习惯起始日、日历单日卡）压根不给它。
        self._tabs: dict[str, QPushButton] = {}
        tabbar = QFrame()
        tabbar.setObjectName("DateTabBar")
        tb = QHBoxLayout(tabbar)
        tb.setContentsMargins(3, 3, 3, 3)
        tb.setSpacing(3)
        for key, label in (("date", "日期"), ("range", "时间段")):
            b = QPushButton(label)
            b.setObjectName("DateTab")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(26)
            widgets._apply_property(b, "on", "false")
            b.clicked.connect(lambda _c=False, k=key: self._set_mode(k))
            tb.addWidget(b, 1)
            self._tabs[key] = b
        tabbar.setVisible(allow_range)
        root.addWidget(tabbar)

        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(0, 4, 0, 0)
        ml.setSpacing(8)
        root.addWidget(main, 1)

        # 日期页：快捷四枚 + 月历 + 时间行
        self.page_date = QWidget()
        dl = QVBoxLayout(self.page_date)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(8)
        ml.addWidget(self.page_date, 1)

        quick_row = QHBoxLayout()
        quick_row.setSpacing(4)
        for kind, glyph, tip, cb in (
            ("sun", "", "今天", lambda: self._quick_date(today)),
            ("sunrise", "", "明天", lambda: self._quick_date(today.addDays(1))),
            ("week", "+7", "下周", lambda: self._quick_date(
                today.addDays((8 - today.dayOfWeek()) % 7 or 7))),
            ("moon", "", "今晚（20:00）", self._quick_tonight),
        ):
            b = IconBtn(kind, 17, "muted")
            if glyph:
                b.icon_w.set_glyph(glyph)
            b.setObjectName("QuickBtn")
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(38)
            b.clicked.connect(cb)
            quick_row.addWidget(b, 1)
        dl.addLayout(quick_row)

        # 月历
        head = QHBoxLayout()
        self.cal_title = QLabel()
        self.cal_title.setObjectName("CalHeader")
        head.addWidget(self.cal_title)
        # 年份单独一个标签：不是今年的时候要能单独上色
        self.cal_year = QLabel()
        self.cal_year.setObjectName("CalYear")
        head.addWidget(self.cal_year)
        head.addStretch(1)
        for icon, cb in (("‹", lambda: self._shift_month(-1)),
                         ("○", self._goto_today),
                         ("›", lambda: self._shift_month(1))):
            b = QPushButton(icon)
            b.setObjectName("CalNav")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(cb)
            head.addWidget(b)
        dl.addLayout(head)

        wd_row = QHBoxLayout()
        wd_row.setSpacing(2)
        for name in ("日", "一", "二", "三", "四", "五", "六"):
            lbl = QLabel(name)
            lbl.setObjectName("CalWd")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            wd_row.addWidget(lbl, 1)
        dl.addLayout(wd_row)

        grid_holder = QWidget()
        self.grid = QGridLayout(grid_holder)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(2)
        self._day_btns: list[QPushButton] = []
        for i in range(42):
            b = QPushButton()
            b.setObjectName("CalDay")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, ix=i: self._pick_day(ix))
            self.grid.addWidget(b, i // 7, i % 7)
            self._day_btns.append(b)
        dl.addWidget(grid_holder)

        # 时间行（点击展开/收起选择器）
        self.time_btn = self._popup_row(dl, "clock", "时间", "accent")
        self.time_btn.clicked.connect(self._toggle_time_panel)

        # 时间选择面板（小时 / 分钟两列）
        self.time_panel = QFrame()
        tp = QHBoxLayout(self.time_panel)
        tp.setContentsMargins(0, 0, 0, 0)
        tp.setSpacing(6)
        self.hour_list = self._make_time_list(24, self._on_hour_pick)
        self.minute_list = self._make_time_list(60, self._on_minute_pick)
        tp.addWidget(self.hour_list, 1)
        tp.addWidget(self.minute_list, 1)
        self.time_panel.hide()
        dl.addWidget(self.time_panel)

        # 时间段页：开始 / 结束 各一个日期格 + 一个时刻格，再加「全天」
        self.page_range = QWidget()
        rl = QVBoxLayout(self.page_range)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        ml.addWidget(self.page_range, 1)
        self._rb_start_date, self._rb_start_time = self._range_row(rl, "开始")
        self._rb_end_date, self._rb_end_time = self._range_row(rl, "结束")
        self.all_day = QCheckBox("全天")
        self.all_day.setObjectName("DateAllDay")
        self.all_day.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_day.toggled.connect(self._on_all_day)
        ad_row = QHBoxLayout()
        ad_row.addWidget(self.all_day)
        ad_row.addStretch(1)
        rl.addLayout(ad_row)
        rl.addStretch(1)

        # 提醒 / 重复：滴答这两行是可点的，原来只是摆着（点了没反应）
        self.remind_btn = self._popup_row(ml, "clock", "提醒")
        self.remind_btn.clicked.connect(
            lambda: self._open_choice(self.remind_btn, self._reminder_options,
                                      self._pick_reminder))
        self.repeat_btn = self._popup_row(ml, "repeat", "重复")
        self.repeat_btn.clicked.connect(
            lambda: self._open_choice(self.repeat_btn, self._repeat_options,
                                      self._pick_repeat))
        self._sync_choice_labels()

        # 确定 / 清除
        btns = QHBoxLayout()
        btns.setSpacing(8)
        ok = QPushButton("确定")
        ok.setObjectName("Primary")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setFixedHeight(38)
        ok.clicked.connect(self._accept)
        clear = QPushButton("清除")
        clear.setObjectName("Ghost")
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.setFixedHeight(38)
        clear.clicked.connect(self._clear)
        btns.addWidget(ok, 2)
        btns.addWidget(clear, 1)
        ml.addLayout(btns)

        self._sync_time_btn()
        self._refresh_calendar()
        self.all_day.blockSignals(True)
        self.all_day.setChecked(not self._time and not self._end_time)
        self.all_day.blockSignals(False)
        self._sync_range()
        self._set_mode(self._mode)

    # ---- 构建/刷新 ----
    def _make_time_list(self, count: int, on_pick) -> QListWidget:
        lst = QListWidget()
        lst.setObjectName("TimeList")
        lst.setFixedHeight(132)
        lst.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for i in range(count):
            lst.addItem(f"{i:02d}")
        lst.itemClicked.connect(lambda it: on_pick(int(it.text())))
        return lst

    def _popup_row(self, ml: QVBoxLayout, kind: str, text: str,
                   color: str = "muted") -> QPushButton:
        """弹层里一行「图标 + 名称 ……… 当前值 + ›」，和滴答一样三行长齐。

        用带布局的 QPushButton 而不是 setText 拼字符串：名称要贴左、值和箭头
        要贴右，字符串办不到。图标一律自绘 TickIcon，不用 emoji —— Windows 上
        emoji 渲染成彩色，和这个模块「一律线性图标」的取向直接冲突。
        """
        btn = QPushButton()
        btn.setObjectName("PopupRow")
        btn.setProperty("rowColor", color)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(btn)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(TickIcon(kind, 14, color))
        lbl = QLabel(text)
        lay.addWidget(lbl)
        lay.addStretch(1)
        val = QLabel("")
        val.setObjectName("PopupRowValue")
        val.setVisible(False)
        lay.addWidget(val)
        lay.addWidget(TickIcon("chevron_right", 11, "muted"))
        btn._row_lbl, btn._row_val = lbl, val
        ml.addWidget(btn)
        return btn

    def _set_row_value(self, btn: QPushButton, text: str) -> None:
        btn._row_val.setText(text)
        btn._row_val.setVisible(bool(text))

    def _open_choice(self, anchor: QPushButton, options, on_pick) -> None:
        items = options()
        menu = TickMenu(items, anchor, checked=on_pick(peek=True))
        menu.picked.connect(lambda v: on_pick(str(v)))
        menu.exec_at(anchor.mapToGlobal(QPoint(4, anchor.height())))

    def _reminder_options(self):
        return [("at_time", "clock", "准时"), ("1", "clock", "提前 1 天"),
                ("7", "clock", "提前 1 周"), ("clear", "circle", "无提醒")]

    def _repeat_options(self):
        return [(val, "repeat", lab) for val, lab in services.REPEAT_OPTIONS]

    def _pick_repeat(self, value: str = None, peek: bool = False):
        if peek or value is None:
            return self._repeat
        self._repeat = value
        self._sync_choice_labels()
        self.repeatPicked.emit(value)

    def _pick_reminder(self, value: str = None, peek: bool = False):
        if peek or value is None:
            return self._reminder_choice
        due = self._selected.toString("yyyy-MM-dd")
        if value == "clear":
            self._reminder = ""
        elif value == "at_time":
            self._reminder = f"{due} {self._time or '09:00'}"
        else:
            self._reminder = (self._selected.addDays(int(value))
                              .toString("yyyy-MM-dd") + " 09:00")
        self._reminder_choice = "" if value == "clear" else value
        self._sync_choice_labels()
        self.reminderPicked.emit(self._reminder)

    def _sync_choice_labels(self) -> None:
        self._set_row_value(
            self.repeat_btn, services.REPEAT_NAMES.get(self._repeat, ""))
        self._set_row_value(self.remind_btn, _human_reminder(self._reminder))

    def _refresh_calendar(self) -> None:
        today = QDate.currentDate()
        self.cal_title.setText(f"{self._month.month()}月")
        self.cal_year.setText(f"{self._month.year()}年")
        # 跨年时年份标蓝：手输「2027年3月5日」这类，标题上不留个记号就会看错年
        widgets._apply_property(
            self.cal_year, "otherYear",
            "true" if self._month.year() != today.year() else "false")
        start = self._month.addDays(-(self._month.dayOfWeek() % 7))
        for i, btn in enumerate(self._day_btns):
            d = start.addDays(i)
            btn.setText(str(d.day()))
            btn._date = d  # noqa: SLF001 仅内部使用
            if d.month() != self._month.month():
                state = "off"
            elif d == self._selected:
                state = "sel"
            elif d == today:
                state = "today"
            else:
                state = ""
            widgets._apply_property(btn, "calState", state)

    def _sync_time_btn(self) -> None:
        self._set_row_value(self.time_btn,
                            dateparse.human_time(self._time) if self._time else "")
        if self._time:
            h, m = (int(x) for x in self._time.split(":"))
            self.hour_list.setCurrentRow(h)
            self.minute_list.setCurrentRow(m)

    # ---- 时间段（持续日期）----
    def _range_row(self, lay, label: str) -> tuple[QPushButton, QPushButton]:
        """一行「开始 [11/01] [16:00]」：两格各自弹小月历 / 时分两列。"""
        row = QHBoxLayout()
        row.setSpacing(6)
        cap = QLabel(label)
        cap.setObjectName("RangeCap")
        row.addWidget(cap)
        d = QPushButton()
        d.setObjectName("RangeField")
        d.setCursor(Qt.CursorShape.PointingHandCursor)
        t = QPushButton()
        t.setObjectName("RangeField")
        t.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(d, 1)
        row.addWidget(t, 1)
        lay.addLayout(row)
        which = "start" if label == "开始" else "end"
        d.clicked.connect(lambda _c=False, w=which, a=d: self._pick_range_date(w, a))
        t.clicked.connect(lambda _c=False, w=which, a=t: self._pick_range_time(w, a))
        return d, t

    def _set_mode(self, key: str) -> None:
        self._mode = key
        if key == "range":
            # 日期页那张网格改过起始日，切过来时得重新对齐（结束不许倒挂到前面）
            if self._end < self._selected:
                self._end = self._selected
            self._sync_range()
        self.page_date.setVisible(key == "date")
        self.page_range.setVisible(key == "range")
        for k, b in self._tabs.items():
            widgets._apply_property(b, "on", "true" if k == key else "false")
        self.adjustSize()

    def _sync_range(self) -> None:
        """四个格子上的字跟着状态走；勾了全天就把两个时刻格清空禁用。"""
        all_day = self.all_day.isChecked()
        self._rb_start_date.setText(self._selected.toString("MM/dd"))
        self._rb_end_date.setText(self._end.toString("MM/dd"))
        for btn, val in ((self._rb_start_time, self._time),
                         (self._rb_end_time, self._end_time)):
            btn.setText(dateparse.human_time(val) if val else "--:--")
            btn.setEnabled(not all_day)

    def _on_all_day(self, on: bool) -> None:
        """全天 = 两个时刻清空（我们的模型里 due_time 为空就是全天）。"""
        if on:
            self._time = ""
            self._end_time = ""
            self.time_panel.hide()
        else:
            self._time = self._time or "09:00"
            self._end_time = self._end_time or "18:00"
        self._sync_time_btn()
        self._sync_range()

    def _pick_range_date(self, which: str, anchor: QPushButton) -> None:
        cur = self._selected if which == "start" else self._end
        pop = MonthPickPopup(cur.toString("yyyy-MM-dd"), self)
        pop.picked.connect(lambda s: self._set_range_date(which, s))
        popups.place_popup(pop, anchor)
        pop.show()

    def _set_range_date(self, which: str, iso: str) -> None:
        d = QDate.fromString(iso, "yyyy-MM-dd")
        if not d.isValid():
            return
        if which == "start":
            self._selected = d
            if self._end < d:
                self._end = d      # 结束早于开始没有意义，跟着抬上来
        else:
            self._end = d if d >= self._selected else self._selected
        self._month = QDate(self._selected.year(), self._selected.month(), 1)
        self._refresh_calendar()
        self._sync_range()

    def _pick_range_time(self, which: str, anchor: QPushButton) -> None:
        cur = self._time if which == "start" else self._end_time
        pop = TimePickPopup(cur, self)
        pop.picked.connect(lambda s: self._set_range_time(which, s))
        popups.place_popup(pop, anchor)
        pop.show()

    def _set_range_time(self, which: str, hhmm: str) -> None:
        if which == "start":
            self._time = hhmm
        else:
            self._end_time = hhmm
        self._sync_time_btn()
        self._sync_range()

    # ---- 交互 ----
    def _shift_month(self, delta: int) -> None:
        self._month = self._month.addMonths(delta)
        self._refresh_calendar()

    def _goto_today(self) -> None:
        self._selected = QDate.currentDate()
        self._month = QDate(self._selected.year(), self._selected.month(), 1)
        self._refresh_calendar()

    def _pick_day(self, idx: int) -> None:
        d = self._day_btns[idx]._date
        self._selected = d
        if (d.year(), d.month()) != (self._month.year(), self._month.month()):
            # 点上下月那几行灰日子：整页翻过去，标题才和选中的那天对得上
            self._month = QDate(d.year(), d.month(), 1)
        self._refresh_calendar()

    def _quick_date(self, d: QDate) -> None:
        self._selected = d
        self._month = QDate(d.year(), d.month(), 1)
        self._refresh_calendar()

    def _quick_tonight(self) -> None:
        self._quick_date(QDate.currentDate())
        self._time = "20:00"
        self._sync_time_btn()

    def _toggle_time_panel(self) -> None:
        self.time_panel.setVisible(not self.time_panel.isVisible())
        self.adjustSize()

    def _on_hour_pick(self, h: int) -> None:
        m = int(self._time.split(":")[1]) if self._time else 0
        self._time = f"{h:02d}:{m:02d}"
        self._sync_time_btn()

    def _on_minute_pick(self, m: int) -> None:
        h = int(self._time.split(":")[0]) if self._time else 9
        self._time = f"{h:02d}:{m:02d}"
        self._sync_time_btn()

    def _accept(self) -> None:
        if not self._selected.isValid():
            self._selected = QDate.currentDate()
        if self._mode == "range":
            end_d = self._end.toString("yyyy-MM-dd")
            end_t = "" if self.all_day.isChecked() else self._end_time
        else:
            # 从时间段切回日期页 = 这条不再是跨天的了，结束端要一起清掉
            end_d = end_t = ""
        self.accepted.emit(self._selected.toString("yyyy-MM-dd"), self._time,
                           end_d, end_t)
        self.close()

    def _clear(self) -> None:
        self.cleared.emit()
        self.close()


def _now() -> str:
    return QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss")


def _place_caret(edit: QLineEdit, global_pos: QPoint) -> None:
    """把「点在哪」换成编辑框里的光标位置。

    刚 show 出来的 QLineEdit 还没排版，坐标算不准，所以先让父布局 activate 一次。
    """
    parent = edit.parentWidget()
    lay = parent.layout() if parent is not None else None
    if lay is not None:
        lay.activate()
    edit.setCursorPosition(
        edit.cursorPositionAt(edit.mapFromGlobal(global_pos)))


class ClickableLabel(QLabel):
    """可点击标签：单击打开详情 / 就地改名、按住拖动启动拖拽。

    clicked 带的是**点击坐标**：就地改名要把光标放到点中的那个字上，
    只发「被点了」就只能退回全选那种有停顿的做法。"""

    clicked = Signal(QPoint)
    doubleClicked = Signal()
    dragStarted = Signal()      # 由父行接管拖拽：缩略图和 mime 都在行上构造

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._press_pos = None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            # 只记下按下点，**不在这里发 clicked**：一发就开改名框，
            # 后面的移动全被 QLineEdit 当成选字，「按住文字拖动这一行」永远起不来。
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (self._press_pos is not None and event.buttons() & Qt.LeftButton
                and (event.position().toPoint() - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()):
            self._press_pos = None
            self.dragStarted.emit()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            # 以前「按下即发」是为了躲「详情面板一开、行宽变了、松手时鼠标不在标题上」；
            # 现在详情是松手才开，这一帧几何不动，所以可以安心等到松手再发。
            start = self._press_pos
            self._press_pos = None
            if start is not None and self.rect().contains(event.position().toPoint()):
                self.clicked.emit(start)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.doubleClicked.emit()



class _MenuRow(QFrame):
    clicked_ = Signal()

    def __init__(self, icon_kind: str, text: str, on: bool = False,
                 danger: bool = False, color: str = "", parent=None,
                 arrow: bool = False, indent: int = 0):
        super().__init__(parent)
        self.setObjectName("TickMenuRow")
        widgets._apply_property(self, "hover", "false")
        self.setFixedHeight(32)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8 + indent, 0, 8, 0)
        lay.setSpacing(8)
        self.icon = None
        # icon_kind 给 None = 这一行不要图标列（滴答的二级菜单就是纯文字，
        # 图标只在一级菜单当扫视锚点，二级里再放一排反而把文字推歪）
        if icon_kind is not None:
            # 三个子控件都必须当场就认这个 parent：没有父对象的 QWidget 是「顶层窗口」，
            # 下面那句 setVisible(True) 会把对勾当成一个独立弹窗 show 出来，
            # 弹层因此抢不住鼠标抓取，整个菜单闪一下就 self-deleteLater 了
            self.icon = TickIcon(icon_kind or "circle", 14,
                                 "red" if danger else (color or "text"), self)
            lay.addWidget(self.icon)
        lbl = QLabel(text, self)
        lbl.setObjectName("TickMenuLabel")
        if danger:
            widgets._apply_property(lbl, "danger", "true")
        lay.addWidget(lbl, 1)
        # 对勾常驻、只用可见性开关：⋯ 菜单里的开关行点一下就要改状态，
        # 再往布局里塞控件会抖一下行高
        self.tick = TickIcon("check", 14, "accent", self)
        self.tick.setVisible(on)
        lay.addWidget(self.tick)
        self.arrow = None
        if arrow:
            self.arrow = TickIcon("chevron_right", 12, "muted", self)
            lay.addWidget(self.arrow)

    def set_on(self, on: bool) -> None:
        self.tick.setVisible(bool(on))

    def enterEvent(self, event) -> None:  # noqa: N802
        widgets._apply_property(self, "hover", "true")

    def leaveEvent(self, event) -> None:  # noqa: N802
        widgets._apply_property(self, "hover", "false")

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked_.emit()


class TickMenu(QFrame):
    """滴答式弹层菜单。

    ``items`` 里可以混三种东西：

    - ``(取值, 图标 kind, 文案[, 图标颜色])`` —— 一行普通项。取值和图标名分开，
      是因为优先级 / 重复这类菜单里多行会共用同一个图标（三面旗子），
      若按图标回传就会全部落到第一行；第四位可选，给需要单独上色的图标
      （优先级四档要红 / 黄 / 蓝，见 `_prio_items`）。
    - ``(SEP,)`` 一条分隔线、``(CAP, "日期")`` 一段小标题 —— 滴答把右键菜单
      分成了「日期 / 优先级 / 操作 / 危险」几段，平铺一长条读不出层次。
    - ``(STRIP, 控件)`` 一整格自定义行，给日期五宫格和优先级四旗那种横排图标条。
    """

    picked = Signal(object)

    def __init__(self, items: list[tuple], parent=None,
                 checked: object = None, danger: tuple[object, ...] = (),
                 width: int = 176, arrows: tuple[object, ...] = ()):
        # 必须用 POPUP_FLAGS：裸 Qt.Popup 在 Windows 上会按矩形加原生投影，
        # 圆角外面糊出一圈 ~#b5b5b5 的深灰描边（就是用户说的「白边 / 黑边」）。
        super().__init__(parent, POPUP_FLAGS)
        self.setObjectName("TickMenu")
        self.setAttribute(Qt.WA_TranslucentBackground)
        card = QFrame(self)
        card.setObjectName("TickMenuCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(5, 5, 5, 5)
        lay.setSpacing(1)
        for item in items:
            if len(item) == 1:
                lay.addWidget(_Hairline(parent=card))
                continue
            if item[0] == CAP:
                cap = QLabel(item[1], card)
                cap.setObjectName("MenuSection")
                lay.addWidget(cap)
                continue
            if item[0] == STRIP:
                strip = item[1]
                strip.setParent(card)
                lay.addWidget(strip)
                continue
            value, icon_kind, label = item[:3]
            color = item[3] if len(item) > 3 else ""
            on = (value in checked) if isinstance(
                checked, (tuple, list, set, frozenset)) else value == checked
            row = _MenuRow(icon_kind, label, on,
                           value in danger, color, card,
                           arrow=value in arrows)
            row.clicked_.connect(lambda v=value: self._fire(v))
            lay.addWidget(row)
        self.setFixedWidth(width)

    def _fire(self, value: object) -> None:
        self.picked.emit(value)
        # picked 的槽里常常直接开一个模态框（dlg.exec() 跑嵌套事件循环），
        # hideEvent 排队的 deleteLater 会在那次循环里被处理掉，
        # 等回到这里 C++ 对象已经销毁 —— 再 close 就是野指针 RuntimeError。
        try:
            self.close()
        except RuntimeError:  # noqa: BLE001
            pass

    def pick(self, value: object) -> None:
        """给菜单里的自定义控件（图标条）用：点一格 = 点一行，选完就关。"""
        self._fire(value)

    def exec_at(self, global_pos: QPoint) -> None:
        self.adjustSize()
        self.move(global_pos)
        self.show()

    def hideEvent(self, event) -> None:  # noqa: N802
        """Qt.Popup 点外部只 hide 不 close，不销毁就会在页面上堆积菜单。"""
        super().hideEvent(event)
        self.deleteLater()


class SortGroupPopup(QFrame):
    """顶栏 ⅋ 的弹层：滴答把「分组」和「排序」放同一个面板的两段里。

    原来只有四个排序项的平铺菜单，既没有分组维度，也看不出这两件事是分开的。
    """

    picked = Signal(str, object)        # ("group" | "sort", 取值)

    def __init__(self, sections: list[tuple[str, list[tuple[object, str, str]]]],
                 checked: dict, parent=None, width: int = 186):
        super().__init__(parent, POPUP_FLAGS)
        self.setObjectName("TickMenu")
        self.setAttribute(Qt.WA_TranslucentBackground)
        card = QFrame(self)
        card.setObjectName("TickMenuCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(5, 7, 5, 5)
        lay.setSpacing(1)
        for i, (title, items) in enumerate(sections):
            if i:
                lay.addWidget(_Hairline(parent=card))
            cap = QLabel(title, card)
            cap.setObjectName("MenuSection")
            lay.addWidget(cap)
            for value, icon_kind, label in items:
                # card 要按关键字传：_MenuRow 的第五个位置参数是 color，
                # 以前直接把 card 塞在那里（= 图标色变成一个 QFrame），parent 反而是空的，
                # 行于是「无父对象的顶层窗口」，整个面板因此拿不住鼠标抓取
                row = _MenuRow(icon_kind, label, checked.get(title) == value,
                               parent=card)
                row.clicked_.connect(lambda v=value, t=title: self._fire(t, v))
                lay.addWidget(row)
        self.setFixedWidth(width)

    def _fire(self, title: str, value: object) -> None:
        self.picked.emit("group" if title == "分组" else "sort", value)
        self.close()

    def exec_at(self, global_pos: QPoint) -> None:
        self.adjustSize()
        self.move(global_pos)
        self.show()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.deleteLater()


class _ViewBtn(QPushButton):
    """「视图」那一格：图标居中，选中档用主色 + 淡底。"""

    def __init__(self, kind: str, tip: str, parent=None):
        super().__init__(parent)
        self.setObjectName("ViewBtn")
        self.setFixedHeight(30)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tip)
        self.setProperty("active", "false")
        self.icon = TickIcon(kind, 17, "muted", self)

    def set_active(self, on: bool) -> None:
        self.icon.set_color_key("accent" if on else "muted")
        widgets._apply_property(self, "active", "true" if on else "false")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.icon.move((self.width() - self.icon.width()) // 2,
                       (self.height() - self.icon.height()) // 2)


class MoreMenuPopup(QFrame):
    """⋯ 菜单：滴答把「视图」切换、四个显示开关和打印放在同一个面板里。

    原来这里只有三行纯动作（显示已完成分组 / 归档视图 / 清空已完成），既切不了
    视图，也没有那几个行内显示开关。开关点完面板不关，可以连着调 —— 滴答也是
    这样，调完点外面才收。
    """

    viewPicked = Signal(str)        # list | board
    toggled = Signal(str)           # hide_done | detail | countdown | checks
    picked = Signal(str)            # archive | clear_done
    printRequested = Signal()

    def __init__(self, state: dict, parent=None):
        super().__init__(parent, POPUP_FLAGS)
        self.setObjectName("TickMenu")
        self.setAttribute(Qt.WA_TranslucentBackground)
        card = QFrame(self)
        card.setObjectName("TickMenuCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(6, 8, 6, 6)
        lay.setSpacing(1)

        cap = QLabel("视图")
        cap.setObjectName("MenuSection")
        lay.addWidget(cap)
        row = QHBoxLayout()
        row.setSpacing(4)
        self._views: dict[str, _ViewBtn] = {}
        for key, kind, tip in (("list", "list", "列表视图"),
                               ("board", "board", "看板视图"),
                               ("timeline", "timeline", "时间线视图（甘特）")):
            b = _ViewBtn(kind, tip, card)
            b.set_active(state.get("view") == key)
            b.clicked.connect(lambda _c=False, k=key: self.viewPicked.emit(k))
            row.addWidget(b, 1)
            self._views[key] = b
        lay.addLayout(row)
        lay.addSpacing(3)
        lay.addWidget(_Hairline())
        lay.addSpacing(3)

        self._rows: dict[str, _MenuRow] = {}
        for value, kind, label in (("hide_done", "done", "隐藏已完成"),
                                   ("detail", "menu", "显示详细"),
                                   ("countdown", "clock", "显示倒数日"),
                                   ("checks", "subtask_list", "显示检查事项")):
            r = _MenuRow(kind, label, bool(state.get(value)), parent=card)
            r.clicked_.connect(lambda v=value, w=r: self._flip(v, w))
            lay.addWidget(r)
            self._rows[value] = r
        pr = _MenuRow("printer", "打印", arrow=True, parent=card)
        pr.clicked_.connect(self._on_print)
        lay.addWidget(pr)
        lay.addSpacing(3)
        lay.addWidget(_Hairline())
        lay.addSpacing(3)
        # 滴答没有这两项，是我们自己加的入口（归档任务只能从这里回到视野）
        for value, kind, label, danger in (
                ("archive", "archive", "归档视图", False),
                ("clear_done", "trash", "清空已完成", True)):
            r = _MenuRow(kind, label, danger=danger, parent=card)
            r.clicked_.connect(lambda v=value: self._fire(v))
            lay.addWidget(r)
        self.setFixedWidth(198)

    def _flip(self, value: str, row: _MenuRow) -> None:
        row.set_on(not row.tick.isVisible())
        self.toggled.emit(value)

    def _fire(self, value: str) -> None:
        self.picked.emit(value)
        self.close()

    def _on_print(self) -> None:
        # 二级菜单要盖在别处，先收自己再开：两个 Qt.Popup 叠着会把父层 deleteLater
        # 掉，子层是它的孩子就一起没了
        self.printRequested.emit()
        self.close()

    def set_view(self, key: str) -> None:
        for k, b in self._views.items():
            b.set_active(k == key)

    def exec_at(self, global_pos: QPoint) -> None:
        self.adjustSize()
        self.move(global_pos)
        self.show()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.deleteLater()


class _PrioBtn(QPushButton):
    """优先级弹层里的一格旗子：选中那一格描一圈框（滴答就是这么标的）。"""

    def __init__(self, value: int, kind: str, color_key: str, tip: str,
                 parent=None):
        super().__init__(parent)
        self.value = value
        self.setObjectName("PrioBtn")
        self.setFixedSize(36, 32)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tip)
        widgets._apply_property(self, "on", "false")
        self.icon = TickIcon(kind, 17, color_key or "border_strong", self)

    def set_on(self, on: bool) -> None:
        widgets._apply_property(self, "on", "true" if on else "false")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.icon.move((self.width() - self.icon.width()) // 2,
                       (self.height() - self.icon.height()) // 2)


class _IconStrip(QFrame):
    """菜单里那一横排图标按钮（日期五宫格 / 优先级四旗）。

    滴答把「改期」和「改优先级」做成一排点一下就完事的格子，而不是五个 / 四个
    菜单行 —— 这两件事用户是扫一眼就选的，摊成文字行会把整张菜单撑长一半。
    """

    picked = Signal(object)

    def __init__(self, specs: list[tuple], parent=None, margin: int = 8):
        super().__init__(parent)
        self.setObjectName("MenuStrip")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(margin, 2, margin, 4)
        lay.setSpacing(2)
        for spec in specs:
            value, kind, tip, glyph, on = spec[:5]
            color = spec[5] if len(spec) > 5 else "muted"
            btn = _ViewBtn(kind, tip, self)
            if glyph:
                btn.icon.set_glyph(glyph)
            # 不用 _ViewBtn.set_active：它会把图标一起染成主色，
            # 优先级那四格要的正是「红黄蓝各自保持自己的色，只有一格有底色」
            widgets._apply_property(btn, "active", "true" if on else "false")
            if color and color != "muted":
                btn.icon.set_color_key(color)
            btn.clicked.connect(lambda _=False, v=value: self.picked.emit(v))
            lay.addWidget(btn)


def _date_strip_specs(due: str, repeat: str) -> list[tuple]:
    """日期那一排：今天 / 明天 / 下周 / 选日期 / 清除日期。

    「下周」就是 +7 天（滴答的 +7 格）；重复任务没有「这一周期没日期」这个状态，
    清除那一格改成「跳过这一周期」，图标不变、tooltip 说清楚。
    """
    today = QDate.currentDate()
    return [
        ("d0", "sun", "今天", "", due == today.toString("yyyy-MM-dd")),
        ("d1", "sunrise", "明天", "", due == today.addDays(1).toString("yyyy-MM-dd")),
        ("d7", "calendar", "下周", "+7",
         due == today.addDays(7).toString("yyyy-MM-dd")),
        ("pick", "calendar", "选择日期", "", False),
        ("none", "calendar", "跳过此周期" if repeat else "清除日期", "×", False),
    ]


def _prio_strip_specs(prio: int) -> list[tuple]:
    """优先级那一排四格旗子：红 / 黄 / 蓝 / 空心。

    没选中的那三格也照色画 —— 滴答就是靠颜色本身当图例，不像文字菜单那样
    只在选中项上上色。
    """
    return [("p%d" % v, "flag_none" if v == 0 else "flag", label, "", v == prio,
             color or "border_strong")
            for v, _kind, label, color in _prio_items()]


class QuickMorePopup(QFrame):
    """快速添加条上那枚旗子的弹层：滴答把优先级和「添加到 清单 / 标签」放一起。

    这四个值（优先级 / 清单 / 标签 / 日期）原来只有日期有入口，其余全靠标题里
    的 ``!`` / ``#清单`` / ``@标签`` 语法，打不出来就没有入口。
    """

    prioPicked = Signal(int)
    listRequested = Signal()
    tagsRequested = Signal()

    def __init__(self, prio: int, list_name: str, tags, parent=None):
        super().__init__(parent, POPUP_FLAGS)
        self.setObjectName("TickMenu")
        self.setAttribute(Qt.WA_TranslucentBackground)
        card = QFrame(self)
        card.setObjectName("TickMenuCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(6, 8, 6, 6)
        lay.setSpacing(1)

        cap = QLabel("优先级")
        cap.setObjectName("MenuSection")
        lay.addWidget(cap)
        row = QHBoxLayout()
        row.setSpacing(4)
        for value, kind, label, color in _prio_items():
            b = _PrioBtn(value, kind, color, label, card)
            b.set_on(value == int(prio or 0))
            b.clicked.connect(lambda _c=False, v=value: self._pick_prio(v))
            row.addWidget(b, 1)
        lay.addLayout(row)
        lay.addSpacing(3)
        lay.addWidget(_Hairline(card))
        lay.addSpacing(3)

        cap2 = QLabel("添加到")
        cap2.setObjectName("MenuSection")
        lay.addWidget(cap2)
        lr = _MenuRow("list", list_name or "收集箱", arrow=True, parent=card)
        lr.clicked_.connect(self._ask_list)
        lay.addWidget(lr)
        tr = _MenuRow("tag", "标签" + (f"（{len(tags)}）" if tags else ""),
                      arrow=True, parent=card)
        tr.clicked_.connect(self._ask_tags)
        lay.addWidget(tr)
        self.setFixedWidth(198)

    def _pick_prio(self, value: int) -> None:
        self.prioPicked.emit(value)
        self.close()

    def _ask_list(self) -> None:
        # 二级菜单要盖在别处，先收自己再开：两个 Qt.Popup 叠着会把父层
        # deleteLater 掉，子层是它的孩子就一起没了（同 MoreMenuPopup）
        self.listRequested.emit()
        self.close()

    def _ask_tags(self) -> None:
        self.tagsRequested.emit()
        self.close()

    def exec_at(self, global_pos: QPoint) -> None:
        self.adjustSize()
        self.move(global_pos)
        self.show()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.deleteLater()


class UndoBar(QFrame):
    """完成之后浮在中栏底部中间的撤销条：滴答那版是「标题 已完成 ↺」。

    整条都能点，点一下退回未完成；6 秒自己收。同一时刻只留最新一条 ——
    连着勾好几条时，最早那条已经不是用户要撤的对象了。
    """

    undone = Signal(object)          # ("todo", id, occ) / ("habit", id, date)
    HOLD = 6000

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.setObjectName("UndoBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._payload = None
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 12, 0)
        lay.setSpacing(8)
        self.text_lbl = QLabel("")
        self.text_lbl.setObjectName("UndoText")
        self.verb_lbl = QLabel("已完成")
        self.verb_lbl.setObjectName("UndoVerb")
        # 深底上一条，图标和字都得是白的（QSS 管不到自绘图标）
        self.icon = TickIcon("restore", 16, "text")
        self.icon.set_color_hex("#ffffff")
        lay.addWidget(self.text_lbl)
        lay.addWidget(self.verb_lbl)
        lay.addSpacing(2)
        lay.addWidget(self.icon)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def payload(self):
        return self._payload

    def show_for(self, payload, text: str, verb: str = "已完成") -> None:
        self._payload = payload
        self.verb_lbl.setText(verb)
        f = QFont(self.text_lbl.font())
        f.setPixelSize(13)
        # 标题长到一定程度就截断：这条只有 260px 左右宽，不截会把撤销箭头顶出去
        self.text_lbl.setText(QFontMetrics(f).elidedText(
            text or "", Qt.ElideRight, 260))
        self.text_lbl.setFont(f)
        self.verb_lbl.setFont(f)
        self.adjustSize()
        self.place()
        self.show()
        self.raise_()
        self._timer.start(self.HOLD)

    def place(self) -> None:
        host = self.parentWidget()
        if host is None:
            return
        want = self.sizeHint().width()
        w = max(160, min(want, host.width() - 24))
        self.setFixedWidth(w)
        self.move(max(8, (host.width() - w) // 2),
                  max(8, host.height() - self.height() - 18))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        event.accept()        # 别冒到 canvas 上被「点空白收起详情」吃掉

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (event.button() == Qt.LeftButton
                and self.rect().contains(event.position().toPoint())):
            payload, self._payload = self._payload, None
            self.hide()
            if payload is not None:
                self.undone.emit(payload)
            return
        super().mouseReleaseEvent(event)


class TaskListArea(QScrollArea):
    """任务列表容器。

    旧实现 ``AnimatedListWidget.clear_items()`` 在清空时把布局末尾的 stretch 一起
    ``takeAt`` 掉了，于是 ``add_widget`` 里 ``count() - 1`` 恒等于 0，每条任务都被
    插到 index 0 —— 整个列表倒序、分组标题掉到最底部并重复。这里改成「永远保留
    末尾 stretch」，并且用 ``setParent(None)`` 立即把旧控件从布局摘掉：
    ``deleteLater`` 只排队销毁，截屏 / 快速重绘时旧行还会留在画面上。
    """

    dropRequested = Signal(int, object)   # (todo_id, 容器坐标)
    emptyClicked = Signal()               # 点在列表空白处（收起详情）
    # 键盘走查：原来整个待办页一个快捷键都没有，删一条任务只能右键
    keyMove = Signal(int)                 # ±1，上下换行
    keyActivate = Signal(str)             # "space" 勾选 / "enter" 开详情
    keyDelete = Signal()
    keyEscape = Signal()
    keyRename = Signal()                  # F2，资源管理器那个改名键

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TodoListScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addStretch(1)
        self.setWidget(self._container)
        # 行会自己吃掉按下事件（见 TodoRow.mousePressEvent），所以能冒到
        # 容器 / viewport 上的就只剩真正的空白。
        self._container.installEventFilter(self)
        self.viewport().installEventFilter(self)

        self._widgets: list[QWidget] = []
        # 预览线挂在 viewport 上：它的坐标就是视口坐标，不需要任何 mapTo。
        # 之前挂在 container 上、用 viewport().mapTo(container, …) 换算，
        # 而 QScrollArea 里这个换算并不是 child.mapTo(viewport) 的逆运算
        # （实测 74 反算成 364），预览线就会跳到别的行 —— 用户看到的「位置不对」。
        self._indicator = QFrame(self.viewport())
        self._indicator.setObjectName("InsertLine")
        self._indicator.setFixedHeight(2)
        self._indicator_color = ""    # 见 _show_indicator：换色才重设样式
        self._indicator.hide()
        self._insert_idx = self._insert_y = -1
        self._drag_group = ""
        self._reorder_enabled = True
        # 拖出列表外 / 直接松手时 Qt 不一定补发 dragLeave，线会留在原地；
        # 改成「一段时间没有拖拽移动就自己收起来」。
        self._idle_hide = QTimer(self)
        self._idle_hide.setSingleShot(True)
        self._idle_hide.setInterval(260)
        self._idle_hide.timeout.connect(self._hide_indicator)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if (event.type() == QEvent.MouseButtonPress
                and event.button() == Qt.LeftButton
                and obj in (self._container, self.viewport())):
            self.setFocus(Qt.OtherFocusReason)
            self.emptyClicked.emit()
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        k = event.key()
        if k in (Qt.Key_Down, Qt.Key_Up):
            self.keyMove.emit(1 if k == Qt.Key_Down else -1)
        elif k == Qt.Key_Space:
            self.keyActivate.emit("space")
        elif k in (Qt.Key_Return, Qt.Key_Enter):
            self.keyActivate.emit("enter")
        elif k in (Qt.Key_Delete, Qt.Key_Backspace):
            self.keyDelete.emit()
        elif k == Qt.Key_Escape:
            self.keyEscape.emit()
        elif k == Qt.Key_F2:
            self.keyRename.emit()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # ---- 条目管理 ----
    def clear_items(self) -> None:
        while self._layout.count() > 1:          # 末尾 stretch 必须留着
            it = self._layout.takeAt(0)
            w = it.widget()
            if w is not None:
                widgets.drop_widget(w)
        self._widgets.clear()
        self._hide_indicator()

    def add_widget(self, widget: QWidget) -> None:
        widget.setParent(self._container)
        widget.show()
        self._layout.insertWidget(self._layout.count() - 1, widget)
        self._widgets.append(widget)

    def finalize_layout(self) -> None:
        self._widgets = [
            self._layout.itemAt(i).widget()
            for i in range(self._layout.count() - 1)
            if self._layout.itemAt(i).widget() is not None
        ]
        self._layout.activate()

    def scroll_value(self) -> int:
        return self.verticalScrollBar().value()

    def set_scroll_value(self, v: int) -> None:
        self.verticalScrollBar().setValue(v)

    # ---- 拖放 ----
    def set_reorder_enabled(self, on: bool) -> None:
        """只有「自定义排序」能重排。

        按优先级/日期排序时顺序是算出来的，拖了也不会变；
        这时候还画出蓝色插入线，就是在骗用户「这里能放」。
        """
        if self._reorder_enabled == on:
            return
        self._reorder_enabled = on
        if not on:
            self._hide_indicator()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if (self._reorder_enabled and self._drag_group
                and event.mimeData().hasFormat(MIME_TODO)):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._reorder_enabled and self._drag_group                 and event.mimeData().hasFormat(MIME_TODO):
            event.acceptProposedAction()
            pos = event.position().toPoint()
            self._show_indicator(pos)
            self._auto_scroll(pos)
            self._idle_hide.start()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._hide_indicator()

    def dropEvent(self, event) -> None:  # noqa: N802
        ok = False
        if self._reorder_enabled and event.mimeData().hasFormat(MIME_TODO):
            idx, _y, ok = self._slot_at(event.position().toPoint().y())
            if ok:
                tid = int(bytes(event.mimeData().data(MIME_TODO)).decode())
                self.dropRequested.emit(tid, idx)
                event.acceptProposedAction()
        self._hide_indicator()
        if not ok:
            event.ignore()

    def set_drag_group(self, key: str) -> None:
        """拖拽开始时由行告诉列表「我属于哪个分组」。空串 = 没在拖。"""
        self._drag_group = key or ""
        if not self._drag_group:
            self._hide_indicator()

    @staticmethod
    def _group_of(w: QWidget) -> str:
        """条目属于哪个分组：任务行 / 打卡行看 _group_key，组标题看 key。"""
        g = getattr(w, "_group_key", None)
        if g is not None:
            return str(g)
        return str(getattr(w, "key", "") or "")

    def _slot_at(self, y: int) -> tuple[int, int, bool]:
        """光标 y → (插到第几个任务行之前, 预览线该画的 y, 这个落点能不能放)。

        以前的边界只在「任务行」里数，组标题和打卡行不算数：光标一移到打卡区，
        最近的任务行边界已经是下一个分组了，线就画到别的分组上去，松手又被
        「只允许组内重排」挡掉 —— 就是「拖的是上面这条，显示在未来7天，
        有时候还拖不动」。

        现在按完整条目序列算，落点合法与否看**线上下两个邻居**：有一个属于被拖
        那条的分组就能放（另一个方向是组尾，也是组内位置）；两个都不是就不画线，
        不给「这里能放」的假象。
        """
        vp = self.viewport()
        g = self._drag_group
        idx = 0
        prev = None
        tail_y = 0
        for w in self._widgets:
            is_task = getattr(w, "_todo_id", None) is not None
            if w.isVisible() and w.height() > 0:
                top = w.mapTo(vp, QPoint(0, 0)).y()
                bottom = top + w.height()
                if y < top + w.height() / 2:
                    below = self._group_of(w) == g
                    above = prev is not None and self._group_of(prev) == g
                    # 落在别的组的组标题上面时，线要画到标题下面，
                    # 否则线看着在上一组末尾、东西其实插进了这一组
                    line = bottom if (below and not above
                                      and not is_task) else top
                    return idx, line, below or above
                prev = w
                tail_y = bottom
            if is_task:
                idx += 1        # 折叠掉的行也要计数，和 _handle_drop 口径一致
        # 光标在所有条目下面：落在最后一行的末尾
        return idx, tail_y, prev is not None and self._group_of(prev) == g

    def _show_indicator(self, vp_pos: QPoint) -> None:
        idx, y, ok = self._slot_at(vp_pos.y())
        if not ok or not self._drag_group:
            self._hide_indicator()
            return
        if idx == self._insert_idx and y == self._insert_y:
            return
        self._insert_idx, self._insert_y = idx, y
        col = theme.get("accent")
        if col != self._indicator_color:
            # 拖动时每帧 setStyleSheet 都会触发一次样式重抛光，手感就是在这儿变涩的
            self._indicator_color = col
            self._indicator.setStyleSheet(
                f"background: {col}; border-radius: 1px;")
        # 左端从勾选框之后起，和行内容的文字区对齐（行内边距 10 + 勾选框 19 + 间距 8）
        self._indicator.setGeometry(37, y - 1,
                                    max(self.viewport().width() - 49, 10), 2)
        self._indicator.show()
        self._indicator.raise_()

    def _hide_indicator(self) -> None:
        self._idle_hide.stop()
        self._insert_idx = self._insert_y = -1
        self._indicator.hide()

    def _auto_scroll(self, vp_pos: QPoint) -> None:
        """拖到列表上下边缘时滚动。

        没有它，长列表里只能把任务拖到当前屏幕上看得见的位置。
        """
        bar = self.verticalScrollBar()
        margin, h = 46, self.viewport().height()
        if vp_pos.y() < margin:
            bar.setValue(bar.value() - max(4, (margin - vp_pos.y()) // 3))
        elif vp_pos.y() > h - margin:
            bar.setValue(bar.value() + max(4, (vp_pos.y() - (h - margin)) // 3))


class ColumnReorderMixin:
    """给「一列卡片」这种容器加拖拽重排：算插槽、画预览线、发落点信号。

    看板的一列和四象限的一格结构一样（body 里一个 QVBoxLayout，末尾一根 stretch），
    所以两边共用这一份，不再各写一遍。列表页 TaskListArea 早就有同构的实现，
    它按整张表的索引算落点；这里改成报「插到哪张卡之前/之后」，由页面换算全局顺序 ——
    因为列只是全局序列里的一段，列内索引对不上 services.todo_reorder 要的全局 id 表。

    刻意限制：只接受**属于本列**的任务落进本列。跨列拖不改清单也不改优先级，
    那两个语义已经分别由「拖到左栏」和「拖进别的象限」负责，这里不抢。
    """

    cardDropped = Signal(int, object, bool)   # (todo_id, 参照卡 todo_id 或 None, 插到它之前?)

    def _setup_reorder(self) -> None:
        self._reorder_on = False
        self.setAcceptDrops(True)
        line = QFrame(self.body)
        line.setObjectName("ColumnDropLine")
        line.setFixedHeight(2)
        line.hide()
        self._drop_line = line

    def _cards(self) -> list:
        """本列里可重排的卡片（带 _todo_id 的那些）。"""
        lay = self.body.layout()
        out = []
        for i in range(lay.count()):
            it = lay.itemAt(i)
            w = it.widget() if it is not None else None
            if w is not None and getattr(w, "_todo_id", None) is not None:
                out.append(w)
        return out

    def set_reorder_enabled(self, on: bool) -> None:
        self._reorder_on = bool(on)
        if not on:
            self._drop_line.hide()

    def _slot_for(self, tid: int, y: int):
        """光标 y（body 坐标）→ (参照卡, 插到它之前?)；落点不合法返回 None。"""
        cards = self._cards()
        if not any(c._todo_id == tid for c in cards):
            return None                     # 不是本列的卡：不给「这里能放」的假象
        for c in cards:
            if y < c.geometry().center().y():
                return (c, True) if c._todo_id != tid else None
        if not cards:
            return (None, True)
        last = cards[-1]
        return (None, True) if last._todo_id == tid else (last, False)

    def _show_line(self, slot) -> None:
        if slot is None:
            self._drop_line.hide()
            return
        anchor, before = slot
        lay = self.body.layout()
        if anchor is None:                  # 本列末尾
            w = lay.itemAt(lay.count() - 2).widget() if lay.count() > 1 else None
            y = (w.geometry().bottom() + 1) if w is not None else 2
        else:
            y = anchor.geometry().top() - 4 if before else anchor.geometry().bottom() + 4
        self._drop_line.setStyleSheet(
            f"background: {theme.get('accent')}; border-radius: 1px;")
        self._drop_line.setGeometry(2, max(y, 0), max(self.body.width() - 12, 10), 2)
        self._drop_line.show()
        self._drop_line.raise_()

    # ---- Qt 拖放三件套 ----
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._reorder_on and event.mimeData().hasFormat(MIME_TODO):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if not (self._reorder_on and event.mimeData().hasFormat(MIME_TODO)):
            return
        tid = int(bytes(event.mimeData().data(MIME_TODO)).decode())
        slot = self._slot_for(tid, self.body.mapFrom(self,
                                                     event.position().toPoint()).y())
        if slot is None:
            self._drop_line.hide()
            event.ignore()
            return
        event.acceptProposedAction()
        self._show_line(slot)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._drop_line.hide()

    def dropEvent(self, event) -> None:  # noqa: N802
        self._drop_line.hide()
        if not (self._reorder_on and event.mimeData().hasFormat(MIME_TODO)):
            event.ignore()
            return
        tid = int(bytes(event.mimeData().data(MIME_TODO)).decode())
        slot = self._slot_for(tid, self.body.mapFrom(self, event.position().toPoint()).y())
        if slot is None:
            event.ignore()
            return
        anchor, before = slot
        self.cardDropped.emit(tid, None if anchor is None else anchor._todo_id, before)
        event.acceptProposedAction()


class BoardColumn(QFrame, ColumnReorderMixin):
    """看板里的一列：组标题在上，卡片往下堆。

    列本身不画底 —— 滴答的看板也是只有卡片有边框，列只是排布的槽位。
    """

    def __init__(self, width: int, parent=None):
        super().__init__(parent)
        self.setObjectName("BoardColumn")
        self.setFixedWidth(width)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.head = QWidget()
        hl = QVBoxLayout(self.head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 2, 8, 10)
        bl.setSpacing(8)
        bl.addStretch(1)
        lay.addWidget(self.head)
        lay.addWidget(self.body, 1)
        self._setup_reorder()

    def set_header(self, w: QWidget) -> None:
        w.setParent(self.head)
        self.head.layout().addWidget(w)
        w.show()

    def add_card(self, w: QWidget) -> None:
        w.setParent(self.body)
        bl = self.body.layout()
        bl.insertWidget(bl.count() - 1, w)
        w.show()


class BoardArea(QScrollArea):
    """看板视图：分组横向排成列、任务变卡片。

    和 TaskListArea 暴露同一套 add_widget / clear_items / finalize_layout 接口，
    reload 里那条「组标题 → 行」的调用序列不用写两份：遇到 GroupHeader 就开一列，
    其它控件都进当前列。列内支持拖拽重排（ColumnReorderMixin），键盘导航仍是列表的事。
    """

    emptyClicked = Signal()
    reorderDropped = Signal(int, object, bool)   # 透传 BoardColumn.cardDropped
    COL_W = 292

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("BoardScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._container = QWidget()
        self._container.setObjectName("BoardContainer")
        self._lay = QHBoxLayout(self._container)
        self._lay.setContentsMargins(10, 0, 10, 0)
        self._lay.setSpacing(12)
        self._lay.addStretch(1)
        self.setWidget(self._container)
        self._widgets: list[QWidget] = []
        self._columns: list[BoardColumn] = []
        self._current: BoardColumn | None = None
        self._container.installEventFilter(self)

    # ---- 与 TaskListArea 同形的最小接口 ----
    def set_reorder_enabled(self, on: bool) -> None:
        """列是 reload 时新建的，所以这里既刷已有的、也记住开关给以后的。"""
        self._reorder_on = bool(on)
        for col in self._columns:
            col.set_reorder_enabled(on)

    def _new_column(self) -> BoardColumn:
        col = BoardColumn(self.COL_W)
        col.cardDropped.connect(self.reorderDropped.emit)
        col.set_reorder_enabled(getattr(self, "_reorder_on", False))
        self._lay.insertWidget(self._lay.count() - 1, col)
        self._columns.append(col)
        return col

    def clear_items(self) -> None:
        while self._lay.count() > 1:          # 末尾 stretch 必须留着
            it = self._lay.takeAt(0)
            w = it.widget()
            if w is not None:
                widgets.drop_widget(w)
        self._widgets.clear()
        self._columns.clear()
        self._current = None

    def add_widget(self, widget: QWidget) -> None:
        if isinstance(widget, GroupHeader):
            col = self._new_column()
            col.set_header(widget)
            self._current = col
        else:
            if self._current is None:
                # 「分组=无」时没有组标题，也要有一列来装卡片
                self._current = self._new_column()
            self._current.add_card(widget)
        self._widgets.append(widget)

    def finalize_layout(self) -> None:
        self._lay.activate()

    def scroll_value(self) -> int:
        return self.horizontalScrollBar().value()

    def set_scroll_value(self, v: int) -> None:
        self.horizontalScrollBar().setValue(v)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """点列与列之间的空白也收起详情，和中栏列表一致。"""
        if (obj is self._container
                and event.type() in (QEvent.MouseButtonPress,
                                     QEvent.MouseButtonDblClick)
                and event.button() == Qt.LeftButton):
            self.emptyClicked.emit()
        return super().eventFilter(obj, event)


class QuadCard(QFrame, ColumnReorderMixin):
    """四象限页里的一格：彩色罗马数字角标 + 标题，下面按日期分组列任务。

    和 BoardColumn 一样只暴露 `add_widget`，reload 里那条「组标题 → 行」的
    调用序列不用为这一页再写一遍。

    落点分两种，互不抢：拖的是**本格里的卡** → 格内重排；拖的是别的格的卡 →
    维持原语义「拖进来就是改优先级」。
    """

    dropped = Signal(int, int)         # (todo_id, 目标优先级)

    def __init__(self, numeral: str, title: str, color_key: str,
                 prio: int = 0, parent=None):
        super().__init__(parent)
        self._prio = prio
        self.setAcceptDrops(True)
        self.setObjectName("QuadCard")
        self.setMinimumHeight(300)
        self.setMinimumWidth(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 10, 6)
        lay.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(7)
        badge = QLabel(numeral, self)
        badge.setObjectName("QuadBadge")
        badge.setFixedSize(18, 18)
        badge.setAlignment(Qt.AlignCenter)
        widgets._apply_property(badge, "tint", color_key)
        head.addWidget(badge)
        lbl = QLabel(title, self)
        lbl.setObjectName("QuadTitle")
        widgets._apply_property(lbl, "tint", color_key)
        head.addWidget(lbl)
        head.addStretch(1)
        lay.addLayout(head)
        self.body = QWidget(self)
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 4, 0, 0)
        bl.setSpacing(0)
        bl.addStretch(1)
        lay.addWidget(self.body, 1)
        self._setup_reorder()
        self._reordering = False

    def add_widget(self, w: QWidget) -> None:
        w.setParent(self.body)
        bl = self.body.layout()
        bl.insertWidget(bl.count() - 1, w)
        w.show()

    # ---- 落点：本格内的卡 → 重排；别处的卡 → 改优先级 ----
    @staticmethod
    def _drag_tid(event) -> int | None:
        try:
            return int(bytes(event.mimeData().data(MIME_TODO)).decode())
        except (ValueError, TypeError):
            return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if not event.mimeData().hasFormat(MIME_TODO):
            return
        tid = self._drag_tid(event)
        self._reordering = tid is not None and any(
            c._todo_id == tid for c in self._cards())
        if self._reordering:
            ColumnReorderMixin.dragEnterEvent(self, event)
            return
        widgets._apply_property(self, "drop", "true")
        event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._reordering:
            ColumnReorderMixin.dragMoveEvent(self, event)
        elif event.mimeData().hasFormat(MIME_TODO):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._reordering = False
        widgets._apply_property(self, "drop", "false")
        self._drop_line.hide()

    def dropEvent(self, event) -> None:  # noqa: N802
        widgets._apply_property(self, "drop", "false")
        if not event.mimeData().hasFormat(MIME_TODO):
            return
        if self._reordering:
            self._reordering = False
            ColumnReorderMixin.dropEvent(self, event)
            return
        self._reordering = False
        raw = bytes(event.mimeData().data(MIME_TODO)).decode()
        self.dropped.emit(int(raw), self._prio)
        event.acceptProposedAction()


class QuadArea(QScrollArea):
    """四象限页：2×2 四格，整页纵向滚动。

    同一行两格等高（滴答也是这样：空的另一格会撑出「没有任务」那一行），
    所以格子只给最小高度、由内容把行撑开，而不是各自限死一个高度。
    """

    emptyClicked = Signal()
    reorderDropped = Signal(int, object, bool)   # 透传 QuadCard.cardDropped

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("QuadScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._grid = QGridLayout(self._container)
        self._grid.setContentsMargins(10, 2, 10, 12)
        self._grid.setSpacing(12)
        # 两列等宽，且允许被压到视口以内：不写这两行，长标题的最小宽度会把整页
        # 撑得比视口还宽，而横向滚动条是关的 —— 右边那一列就直接被裁掉了
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, 1)
        self._grid.setColumnMinimumWidth(0, 0)
        self._grid.setColumnMinimumWidth(1, 0)
        self.setWidget(self._container)
        self._widgets: list[QWidget] = []
        self._cards: list[QuadCard] = []
        self._container.installEventFilter(self)

    def clear_items(self) -> None:
        while self._grid.count():
            it = self._grid.takeAt(0)
            w = it.widget()
            if w is not None:
                widgets.drop_widget(w)
        self._widgets.clear()
        self._cards.clear()

    def add_card(self, idx: int, card: QuadCard) -> None:
        self._grid.addWidget(card, idx // 2, idx % 2)
        card.cardDropped.connect(self.reorderDropped.emit)
        card.set_reorder_enabled(getattr(self, "_reorder_on", False))
        self._cards.append(card)

    def add_widget(self, w: QWidget) -> None:
        self._widgets.append(w)

    def finalize_layout(self) -> None:
        self._grid.activate()

    def set_reorder_enabled(self, on: bool) -> None:
        """格子是 reload 时新建的，所以既刷已有的、也记住开关给以后的。"""
        self._reorder_on = bool(on)
        for card in self._cards:
            card.set_reorder_enabled(on)

    def scroll_value(self) -> int:
        return self.verticalScrollBar().value()

    def set_scroll_value(self, v: int) -> None:
        self.verticalScrollBar().setValue(v)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if (obj is self._container
                and event.type() in (QEvent.MouseButtonPress,
                                     QEvent.MouseButtonDblClick)
                and event.button() == Qt.LeftButton):
            self.emptyClicked.emit()
        return super().eventFilter(obj, event)


class TimelineLane(QWidget):
    """一条任务一行，按日期画一根横条。"""

    clicked_ = Signal(object)            # 把 TodoRow 原样交回页面（要 occ / 显示日）
    blank = Signal()

    H = 34
    BAR_H = 26

    def __init__(self, row: "TodoRow", x0: int, span: int, overdue: bool,
                 area: "TimelineArea", parent=None):
        super().__init__(parent)
        self.row = row
        self.x0 = x0
        self.w = max(28, span * area.DAY_W - 6)
        self.overdue = overdue
        self.area = area
        self._hover = False
        self.setFixedHeight(self.H)
        self.setCursor(Qt.PointingHandCursor)

    def bar_rect(self) -> QRectF:
        return QRectF(self.x0 + 3, (self.H - self.BAR_H) / 2,
                      self.w, self.BAR_H)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self.area.paint_background(p, self.H)
        r = self.bar_rect()
        sel = self.row._todo_id == self.area.selected_id
        key = ("red" if self.overdue and not sel
               else "accent_hi" if sel else "accent")
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.get(key)))
        p.drawRoundedRect(r, 6, 6)
        # 条上的字：标题 + 跨天时带上日期，短到放不下就省略号
        p.setPen(QColor("#ffffff"))
        f = QFont(self.font())
        f.setPixelSize(12)
        p.setFont(f)
        text = self.row.data.get("title") or ""
        if self.overdue:
            text = "%s  已逾期" % text
        box = r.adjusted(9, 0, -6, 0)
        # drawText 的重载不接受 ElideMode 和 AlignmentFlag 相或，自己量着截
        text = QFontMetrics(f).elidedText(text, Qt.ElideRight, int(box.width()))
        p.drawText(box, Qt.AlignVCenter, text)
        if self._hover and not sel:
            p.setBrush(QColor(255, 255, 255, 34))
            p.drawRoundedRect(r, 6, 6)
        p.end()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        if self.bar_rect().contains(event.position().toPoint()):
            self.clicked_.emit(self.row)
        else:
            self.blank.emit()


class TimelineGroup(QWidget):
    """时间线里的分组标题行（「未分组 4」「已过期 2」那一行）。"""

    blank = Signal()

    def __init__(self, header: GroupHeader, area: "TimelineArea", parent=None):
        super().__init__(parent)
        self.header = header
        self.area = area
        self.setFixedHeight(30)
        self.setCursor(Qt.PointingHandCursor)
        header.toggled.connect(lambda _k, _on: self.update())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.blank.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        # 标题钉在视口左边：这一行和横条一样宽（5000 多 px），跟着滚动走的话
        # 往右一拖组名就没了
        x = self.area.hbar().value() + 10.0
        f = QFont(self.font())
        f.setPixelSize(12.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(theme.get("text_hi")))
        p.drawText(QRectF(x, 0, 300, self.height()),
                   Qt.AlignVCenter, self.header.title_text)
        p.end()


class TimelineArea(QWidget):
    """时间线（甘特）视图：滴答那排「视图」里的第三档。

    顶上是一条日期轴（今天标红、周末 / 法定假带 休、调休带 班），下面每条任务
    一根横条 —— 单日的就是一格，持续时间段的拉成一条长的。reload 那套
    add_widget 契约和看板一样，所以列表 / 看板 / 时间线共用一条渲染路径。
    """

    emptyClicked = Signal()
    barClicked = Signal(object)
    DAY_W = 92
    LEAD = 7            # 轴从今天往前再多铺 7 天，逾期任务不至于看不见
    SPAN = 60

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TimelineWrap")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.start = QDate.currentDate().addDays(-self.LEAD)
        self.selected_id = 0
        self.head = QWidget()
        self.head.setObjectName("TimelineHead")
        self.head.setFixedHeight(34)
        # 轴上的格子直接按 x 摆，不走布局：走布局的话这条 5500px 宽的轴会把
        # 整个窗口顶出一个缩不下去的下限（窗口直接变全屏宽）。子控件超出
        # 父控件的部分本来就被裁掉，所以不用额外做视口。
        self.head_inner = QWidget(self.head)
        self.head_inner.setGeometry(0, 0, self.SPAN * self.DAY_W, 34)
        lay.addWidget(self.head)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("TimelineScroll")
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.inner = QWidget()
        self.inner.setObjectName("TimelineContainer")
        # widgetResizable 会把内容压到视口宽，横向往滚就没有范围了 ——
        # 撑住最小宽度，横向滚动条才有得滚
        self.inner.setMinimumWidth(self.SPAN * self.DAY_W)
        self.lay = QVBoxLayout(self.inner)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(0)
        self.lay.addStretch(1)
        self.scroll.setWidget(self.inner)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.verticalScrollBar().valueChanged.connect(
            lambda _v: self.head_inner.move(-self.hbar().value(), 0))
        self.hbar().valueChanged.connect(
            lambda _v: self.head_inner.move(-_v, 0))
        lay.addWidget(self.scroll, 1)
        # _container 这个名字是给页面复用的：折叠分组时它拿
        # _area._container.layout().activate()
        self._container = self.inner
        self._widgets: list[QWidget] = []
        self._lanes: list[QWidget] = []
        self._build_head()

    def _build_head(self) -> None:
        for i in range(self.SPAN):
            strip = TimelineHeadStrip(self.start.addDays(i), self.head_inner)
            strip.move(i * self.DAY_W, 0)

    def hbar(self):
        return self.scroll.horizontalScrollBar()

    # ---- 与 TaskListArea / BoardArea 同形的最小接口 ----
    def clear_items(self) -> None:
        while self.lay.count() > 1:
            it = self.lay.takeAt(0)
            w = it.widget()
            if w is not None:
                widgets.drop_widget(w)
        self._widgets.clear()
        self._lanes.clear()

    def add_widget(self, widget: QWidget) -> None:
        if isinstance(widget, GroupHeader):
            g = TimelineGroup(widget, self, self.inner)
            g.blank.connect(self.emptyClicked.emit)
            self.lay.insertWidget(self.lay.count() - 1, g)
            self._lanes.append(g)
            self._widgets.append(widget)
            return
        if not isinstance(widget, TodoRow):
            # 打卡行之类的别的行控件：时间线上没有日期可摆，摘掉父级收走
            # （不能 deleteLater，页面的 _checkin_rows_by_id 还可能指着它）
            widget.hide()
            widget.setParent(None)
            return
        data = widget.data
        # 行控件本身在时间线上没地方摆，但页面的 _items / _mark_selected 还指着
        # 它们，删掉就成了野指针。挂进布局藏起来：隐藏的控件在布局里不占位，
        # 下次 clear_items 又能连着一起收走。
        due = QDate.fromString(data.get("due_date") or "", "yyyy-MM-dd")
        self._widgets.append(widget)
        self.lay.insertWidget(self.lay.count() - 1, widget)
        widget.hide()
        if not due.isValid():
            # 没有日期的任务在时间线上没有位置可放；列表 / 看板里照常看得见
            return
        end = QDate.fromString(data.get("end_date") or "", "yyyy-MM-dd")
        if not end.isValid() or end < due:
            end = due
        x0 = self.start.daysTo(due) * self.DAY_W
        span = max(1, due.daysTo(end) + 1)
        lane = TimelineLane(widget, x0, span, end < QDate.currentDate(), self)
        lane.clicked_.connect(self.barClicked.emit)
        lane.blank.connect(self.emptyClicked.emit)
        self.lay.insertWidget(self.lay.count() - 1, lane)
        self._lanes.append(lane)

    def scroll_value(self) -> int:
        return self.scroll.verticalScrollBar().value()

    def set_scroll_value(self, v: int) -> None:
        self.scroll.verticalScrollBar().setValue(v)

    def finalize_layout(self) -> None:
        self.lay.activate()
        # 打开就滚到「今天」附近，不然得手动拖半天。滚动范围要等这一轮布局
        # 走完才算得出来，当场 setValue 会被 0 上限夹掉。
        today_x = max(0, (self.start.daysTo(QDate.currentDate()) - 1) * self.DAY_W)
        QTimer.singleShot(0, lambda: self.hbar().setValue(today_x))

    def set_reorder_enabled(self, on: bool) -> None:
        """时间线上不做拖拽排序：横条的位置就是它的日期，拖法只能是改日期。"""

    def paint_background(self, p: QPainter, h: float) -> None:
        """周末 / 今天的底纹。每行自己画，横向滚动天然跟着走。"""
        hol = _holidays()
        for i in range(self.SPAN):
            d = self.start.addDays(i)
            x = i * self.DAY_W
            if d == QDate.currentDate():
                p.fillRect(QRectF(x, 0, self.DAY_W, h), QColor(theme.get("accent_soft")))
            elif hol.day_type(d) == "休":
                p.fillRect(QRectF(x, 0, self.DAY_W, h), QColor(theme.get("surface_hi")))
            p.setPen(QPen(QColor(theme.get("border")), 1))
            p.drawLine(QPointF(x, 0), QPointF(x, h))


class TimelineHeadStrip(QWidget):
    """日期轴上的一格：日号 + 周末 / 假期的 休 / 班 角标。"""

    def __init__(self, day: QDate, parent=None):
        super().__init__(parent)
        self.day = day
        self.setFixedSize(TimelineArea.DAY_W, 34)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        today = self.day == QDate.currentDate()
        mark = _holidays().day_type(self.day)
        if today:
            p.fillRect(self.rect(), QColor(theme.get("accent_soft")))
        elif mark == "休":
            p.fillRect(self.rect(), QColor(theme.get("surface_hi")))
        f = QFont(self.font())
        f.setPixelSize(13)
        f.setBold(today)
        p.setFont(f)
        p.setPen(QColor(theme.get("red" if today else "muted")))
        p.drawText(QRectF(0, 0, self.width() - 16, self.height()),
                   Qt.AlignCenter, str(self.day.day()))
        if mark:
            g = QFont(self.font())
            g.setPixelSize(9)
            p.setFont(g)
            p.setPen(QColor(theme.get("green" if mark == "休" else "muted")))
            p.drawText(QRectF(self.width() - 18, 4, 16, 14),
                       Qt.AlignCenter, mark)
        p.setPen(QPen(QColor(theme.get("border")), 1))
        p.drawLine(QPointF(self.width() - 1, 0), QPointF(self.width() - 1, self.height()))
        p.end()


class _RowSub(QFrame):
    """「显示检查事项」打开时，行内列出的那条子任务。

    只想要一个能点的小勾选框 + 一行字，所以不复用详情面板里的 _SubRow（那套带拖拽
    排序、删除按钮和输入态，塞进列表行会把行高和事件都搞乱）。
    """

    toggled_sub = Signal(object, bool)
    subClicked = Signal(dict)

    def __init__(self, sub: dict, indent: int = 0, parent=None):
        super().__init__(parent)
        self.sub = sub
        self.setObjectName("RowSub")
        self.setFixedHeight(24)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(indent, 0, 0, 0)
        lay.setSpacing(7)
        self.check = PrioCheckBox(bool(sub["done"]), 15)
        self.check.toggled.connect(lambda c: self.toggled_sub.emit(sub, c))
        lay.addWidget(self.check)
        # 复习条目点标题 = 跳到刷题页那一题（大任务那一行点开是详情，不是跳转）
        self.lbl = ClickableLabel(sub["title"])
        self.lbl.setObjectName("RowSubText")
        self.lbl.clicked.connect(lambda _p: self.subClicked.emit(self.sub))
        widgets._apply_property(self.lbl, "done", "true" if sub["done"] else "false")
        f = QFont()
        f.setStrikeOut(bool(sub["done"]))
        self.lbl.setFont(f)
        lay.addWidget(self.lbl, 1)


class TodoRow(QFrame):
    """滴答式任务行：优先级色勾选框 + 标题 + 右侧元信息（子任务/清单/重复/日期）。

    `disp` 是 ⋯ 菜单里的三个显示开关（detail 描述预览 / countdown 倒数日 /
    checks 行内子任务），关掉时行高仍是原来的 40。
    """

    titleClicked = Signal(int)
    changed = Signal()
    contextRequested = Signal(int, object)   # (todo_id, 行内坐标)
    dragGroup = Signal(str)                  # 拖拽开始 / 结束时把所属分组报给列表
    subToggled = Signal(object, bool)        # (子任务 dict, 勾选)
    subClicked = Signal(dict)                # 点行内某条子任务的标题
    doneToggled = Signal(int, str, bool)     # (todo_id, 周期日, 最终是否完成)

    def __init__(self, data: dict, show_list: bool = True, disp: dict | None = None):
        super().__init__()
        disp = disp or {}
        self._disp = disp
        self.data = data
        self._todo_id = data["id"]
        # 重复任务摊平后一行代表一个周期；普通任务这字段为空，occ_* 会走原始分支
        self._occ = data.get("occ") or ""
        self._repeat = (data.get("repeat") or "").strip()
        self._press: QPoint | None = None
        self._drag_start: QPoint | None = None
        # 就地改名那一刻的全局按下点：编辑框会抢走焦点，长按拖行要靠它判别
        self._rename_at: QPoint | None = None
        self.setObjectName("TodoRow")
        self.setCursor(Qt.PointingHandCursor)
        self.line = QFrame(self)
        self.line.setObjectName("RowLine")
        self.line.setFixedHeight(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 0, 8, 0)
        outer.setSpacing(0)
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        outer.addLayout(lay, 1)

        self.check = PrioCheckBox(bool(data["done"]), 19)
        self.check.set_priority(data.get("priority", 0))
        self.check.toggled.connect(self._toggle)
        lay.addWidget(self.check)

        if int(data.get("sub_total", 0) or 0):
            lay.addWidget(TickIcon("subtask", 14, "accent"))
        self.title_lbl = ClickableLabel(data["title"])
        self.title_lbl.setObjectName("TodoTitle")
        # 标签的 minimumSizeHint 默认等于整行文字的宽度：象限页两列并排时，
        # 这个宽度会把页面撑出视口。允许被压缩，超出部分裁掉。
        self.title_lbl.setMinimumWidth(40)
        self.title_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        # 单击标题就地改字（TickTick 的做法）；整行其它地方点开详情
        self.title_lbl.clicked.connect(self._click_title)
        self.title_lbl.dragStarted.connect(self._begin_drag)
        lay.addWidget(self.title_lbl, 1)

        self.title_edit = QLineEdit()
        self.title_edit.setObjectName("TodoRename")
        self.title_edit.hide()
        self.title_edit.returnPressed.connect(self._finish_rename)
        self.title_edit.editingFinished.connect(self._finish_rename)
        self.title_edit.installEventFilter(self)
        lay.addWidget(self.title_edit, 1)

        # 带链接的待办（算法复习）在行上给一个跳转图标。放在标题之后、meta 之前：
        # meta 里那些 TickIcon 是纯绘制控件，不接点击。
        self.link_btn = None
        url = _first_url(data)
        if url:
            self.link_btn = QPushButton()
            self.link_btn.setObjectName("ToolBtn")
            self.link_btn.setFixedSize(22, 22)
            self.link_btn.setCursor(Qt.PointingHandCursor)
            self.link_btn.setToolTip("在浏览器打开\n%s" % url)
            TickIcon("link", 13, "accent", self.link_btn).move(5, 5)
            self.link_btn.clicked.connect(
                lambda _checked=False, u=url: _open_url(u, self))
            lay.addWidget(self.link_btn)

        self.meta = QWidget()
        m = QHBoxLayout(self.meta)
        m.setContentsMargins(0, 0, 0, 0)
        m.setSpacing(7)
        done_n = int(data.get("sub_done", 0) or 0)
        total_n = int(data.get("sub_total", 0) or 0)
        if total_n and not disp.get("checks"):
            sub = QLabel(f"{done_n}/{total_n}")
            sub.setObjectName("TodoSub")
            widgets._apply_property(sub, "subState",
                                    "full" if done_n == total_n else "")
            m.addWidget(sub)
        if int(data.get("pinned") or 0):
            m.addWidget(TickIcon("pin", 12, "amber"))
        if data.get("repeat"):
            m.addWidget(TickIcon("repeat", 13, "muted"))
        if data.get("reminder"):
            m.addWidget(TickIcon("clock", 13, "muted"))
        if data.get("note") and not disp.get("detail"):
            m.addWidget(TickIcon("note", 13, "muted"))
        # 滴答在每行右侧都写清单名，收集箱也写（「最近7天」这类跨清单视图里
        # 不写反而看不出这条属于哪儿）。只有在清单 / 收集箱自己的视图里才省掉。
        # 「显示详细」打开时清单名挪到标题下面单独一行；看板卡片同理，
        # 日期和清单一起挪到第二行（滴答的卡片就是这么排的）。
        card = bool(disp.get("card"))
        has_list = show_list and data.get("list_name")
        list_lbl = None
        if has_list and not disp.get("detail") and not card:
            list_lbl = QLabel(data["list_name"])
            list_lbl.setObjectName("TodoList")
            m.addWidget(list_lbl)
        text, state = _date_state(data["due_date"], data.get("due_time", ""),
                                  bool(disp.get("countdown")),
                                  data.get("end_date") or "")
        date_ws: list[QWidget] = []
        if text:
            if state == "overdue":
                date_ws.append(TickIcon("overdue", 12, "red"))
            dl = QLabel(text)
            dl.setObjectName("TodoDate")
            widgets._apply_property(dl, "dateState", state)
            date_ws.append(dl)
        if not card:
            for wdg in date_ws:
                m.addWidget(wdg)
        lay.addWidget(self.meta)

        # ---- 附加行：描述预览、日期/清单、行内子任务 ----
        extra = 0
        self._note_lbl = None
        if card:
            # 卡片没有行分隔线，靠边框和圆角把自己从列里分出来
            self.line.hide()
            widgets._apply_property(self, "card", "true")
        if disp.get("detail"):
            preview = _plain_preview(data.get("note") or "")
            if preview:
                self._note_lbl = QLabel(preview)
                self._note_lbl.setObjectName("TodoNotePreview")
                self._note_lbl.setTextFormat(Qt.PlainText)
                self._note_lbl.setIndent(27)   # 和标题左缘对齐（10+19+8）
                # 宽度还没定，先允许被压缩，resizeEvent 里再按真实宽度截断
                self._note_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                self._note_full = preview
                outer.addWidget(self._note_lbl)
                extra += self._note_lbl.sizeHint().height()
        if card:
            # 滴答的卡片是「标题 / 描述 / 日期·清单」这个顺序，日期那行永远在最下面
            line2 = QWidget()
            l2 = QHBoxLayout(line2)
            l2.setContentsMargins(27, 0, 0, 4)
            l2.setSpacing(7)
            for wdg in date_ws:
                l2.addWidget(wdg)
            if has_list:
                cl = QLabel(data["list_name"])
                cl.setObjectName("TodoList")
                l2.addWidget(cl)
            l2.addStretch(1)
            outer.addWidget(line2)
            extra += 20
        elif disp.get("detail") and has_list:
            ll = QLabel(data["list_name"])
            ll.setObjectName("TodoListLine")
            ll.setIndent(27)
            outer.addWidget(ll)
            extra += ll.sizeHint().height()
        if disp.get("checks"):
            subs = data.get("subs") or []
            # 复习大任务默认折着：一天十几道题摊开会把整屏占满，点开详情再逐条勾。
            cap = 0 if data.get("review_group") else SUB_INLINE_MAX
            for s in subs[:cap]:
                row = _RowSub(s, 27)
                row.toggled_sub.connect(self.subToggled.emit)
                row.subClicked.connect(self.subClicked.emit)
                outer.addWidget(row)
                extra += row.height()
            left = len(subs) - cap
            if left > 0:
                # 复习行一条都没摊，那行是「共几项」；普通行摊了三条，才是「还剩几项」
                text = ("%d 项待复习" % len(subs)) if cap == 0 else "还有 %d 项" % left
                more = ClickableLabel(text)
                more.setObjectName("RowSubMore")
                more.setIndent(SUB_TEXT_INDENT)
                more.setFixedHeight(20)
                more.clicked.connect(
                    lambda _p: self.titleClicked.emit(self._todo_id))
                outer.addWidget(more)
                extra += 20
        self.setFixedHeight(40 + extra)

        self._refresh_style()

    # ---- 状态 ----
    def _refresh_style(self) -> None:
        # 「放弃」按完成态那套画（划掉 + 灰字）：它和完成一样，都是「不再出现在待做里」
        done = bool(self.data["done"]) or bool(self.data.get("abandoned"))
        widgets._apply_property(self.title_lbl, "done", "true" if done else "false")
        f = QFont()
        f.setStrikeOut(done)
        self.title_lbl.setFont(f)
        self.check.set_checked(done)
        # 放弃的那条框里画 ✕：滴答用这一笔把「做完了」和「不做了」分开，
        # 划掉 + 灰字两边一样，只有勾本身能看出来
        self.check.set_cross(bool(self.data.get("abandoned")) and not int(self.data["done"] or 0))
        self.check.set_priority(self.data.get("priority", 0))

    def set_selected(self, on: bool) -> None:
        """当前开着详情的那一行要有常驻底色：不然点完看不出选的是哪条。"""
        widgets._apply_property(self, "selected", "true" if on else "false")

    def set_checked_only(self, checked: bool) -> None:
        """只改勾选框，不触发 toggled（避免程序化更新回灌成一次用户操作）。"""
        self.check.set_checked(checked)

    # ---- 交互 ----
    def _make_drag_mime(self) -> QMimeData:
        mime = QMimeData()
        mime.setData(MIME_TODO, str(self._todo_id).encode())
        return mime

    def _cancel_rename(self) -> None:
        # 放在可见性判断之前：编辑框已经收起来时也得清掉，否则残留的按下点
        # 会让下一次随手一划被误判成拖拽起手。
        self._rename_at = None
        if not self.title_edit.isVisible():
            return
        self.title_edit.setText(self.data["title"])
        self.title_edit.hide()
        self.title_lbl.show()
        self.meta.show()

    def _begin_drag(self) -> None:
        """从行上任意空白处都能拖；拖拽时带上整行的快照，鼠标才有东西跟着走。"""
        # 标题和行身各自都会判阈值，一次按下可能两边都触发 —— 先清掉按下点，
        # 后面那条路就走不进来了（不然会连着 exec 两次 QDrag）
        self._press = None
        # 按下已经开了就地改名，接着拖的话要把编辑框收回去再拖
        self._cancel_rename()
        drag = QDrag(self)
        drag.setMimeData(self._make_drag_mime())
        pix = widgets.drag_snapshot(self, "bg_alt")
        drag.setPixmap(pix)
        drag.setHotSpot(QPoint(24, self.height() // 2))
        self.dragGroup.emit(str(getattr(self, "_group_key", "") or ""))
        drag.exec(Qt.MoveAction)
        self.dragGroup.emit("")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press = event.position().toPoint()
            # 必须吃掉：QWidget 默认 ignore 鼠标按下，事件会冒到列表容器上，
            # 被「点空白收起详情」误判成点了空白。
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        start = getattr(self, "_press", None)
        if (start is not None and event.buttons() & Qt.LeftButton
                and (event.position().toPoint() - start).manhattanLength()
                >= QApplication.startDragDistance()):
            self._press = None
            self._begin_drag()
            return
        super().mouseMoveEvent(event)

    def _toggle(self, checked: bool) -> None:
        self.data["done"] = int(checked)
        # occ_set_done 内部区分「重复系列的某周期写 todo_occ」和「普通任务写 todos.done」
        services.occ_set_done(self._todo_id, self._occ, checked)
        if checked and not _review_ask_group(self, self._todo_id):
            services.occ_set_done(self._todo_id, self._occ, False)
            self.data["done"] = 0       # 复习结论没答，这条已经退回未完成
        elif checked and not services.review_kind_of_todo(self._todo_id):
            sounds.play("todo_done")
        self._refresh_style()
        self.changed.emit()
        # 复习没答完会退回 0，所以发的是「最终状态」而不是入参
        self.doneToggled.emit(self._todo_id, self._occ, bool(self.data["done"]))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """改名编辑框上的两件事：Esc 取消；按住标题横向拖改成拖整行。

        按下标题会就地开编辑框，编辑框随即拿到焦点 —— 用户想长按拖这一行时，
        移动事件落在 QLineEdit 上被当成选字，行自己的 mouseMoveEvent 根本收不到，
        而且标题控件此时已经隐藏，它的 dragStarted 也永远不会发。
        表现就是「点在文字上就拖不动，反而开始输入」。
        所以在编辑框上补这一道：按住左键移动超过 startDragDistance，
        就收起编辑框、改走整行拖拽。
        """
        if obj is not self.title_edit:
            return super().eventFilter(obj, event)
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.title_edit.setText(self.data["title"])
            self.title_edit.clearFocus()
            return True
        if event.type() == QEvent.MouseButtonRelease:
            self._rename_at = None        # 这一下手势结束了，别留给下一次
        elif (event.type() == QEvent.MouseMove
                and self._rename_at is not None
                and event.buttons() & Qt.LeftButton
                and (event.globalPosition().toPoint()
                     - self._rename_at).manhattanLength()
                >= QApplication.startDragDistance()):
            # 先清标志再触发：一次按下只能起一次拖，后面同一个手势里
            # 还会连续来好几个 move。不靠 _begin_drag 顺手帮忙清。
            self._rename_at = None
            self._begin_drag()
            return True
        return super().eventFilter(obj, event)

    def _click_title(self, at: QPoint) -> None:
        """点标题（松手才算点开）：开详情 + 就地进入改名编辑。

        整条链子都挪到松手了 —— 按下就开的话，改名框会吃掉移动事件、
        详情栏会挤窄中栏，「按住文字拖动这一行」永远起手不了（用户连报两次）。
        """
        self.titleClicked.emit(self._todo_id)
        self._start_rename(at)

    def _start_rename(self, at: QPoint | None = None) -> None:
        self.title_edit.setText(self.data["title"])
        g = self.title_lbl.mapToGlobal(at) if at is not None else None
        self._rename_at = g           # 只有鼠标按出来的改名才有这个点，键盘触发的没有
        self.title_lbl.hide()
        self.meta.hide()
        self.title_edit.show()
        self.title_edit.setFocus()
        if g is None:
            self.title_edit.selectAll()
        else:
            _place_caret(self.title_edit, g)

    def _finish_rename(self) -> None:
        self._rename_at = None
        if not self.title_edit.isVisible():
            return
        text = self.title_edit.text().strip()
        self.title_edit.hide()
        self.title_lbl.show()
        self.meta.show()
        if text and text != self.data["title"]:
            self.data["title"] = text
            self.title_lbl.setText(text)
            services.todo_update(self._todo_id, title=text)
            QTimer.singleShot(0, self.changed.emit)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 线从勾选框右缘起、不贴到左边，才像滴答那样是「内容」的分隔而不是整行
        self.line.setGeometry(34, self.height() - 1,
                              self.width() - 34 - 8, 1)
        if getattr(self, "_note_lbl", None) is not None:
            # 描述预览只占一行：按行的真实宽度截断，否则 sizeHint 会把行撑宽，
            # 列表跟着出横向滚动条
            avail = max(self.width() - 45 - 27, 40)
            self._note_lbl.setText(self._note_lbl.fontMetrics().elidedText(
                self._note_full, Qt.ElideRight, avail))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 必须清掉 _press：在行上按下、移到行外松开时这里不会被调用，
        # 残留的按下点会让下一次「按住别的键在行上移动」被误判成拖拽起手。
        if (event.button() == Qt.LeftButton and self._press is not None
                and self.rect().contains(event.position().toPoint())):
            # _press 还在 = 这一把手势没走到拖拽起手，就是「点开详情」
            self.titleClicked.emit(self._todo_id)
        self._press = None
        if event.button() == Qt.RightButton:
            self.contextRequested.emit(self._todo_id, event.position().toPoint())
            return
        super().mouseReleaseEvent(event)


class GroupHeader(QFrame):
    """分组标题：▾ 标题 计数 ……（可选右侧动作，如「顺延」「查看更多」）。"""

    toggled = Signal(str, bool)
    action = Signal(str)

    def __init__(self, key: str, title: str, count: int, expanded: bool = True,
                 action_text: str = "", warn: bool = False,
                 icon_kind: str = "", icon_hex: str = ""):
        super().__init__()
        self.key = key
        self.title_text = title      # 时间线那一栏要重画这个标题，得留个副本
        self.expanded = expanded
        self.setObjectName("GroupHeader")
        self.setFixedHeight(34)
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 8, 0)
        lay.setSpacing(5)
        self.arrow = TickIcon("chevron_down", 12, "muted")
        lay.addWidget(self.arrow)
        if icon_kind:
            # 文件夹视图按子清单分组时，组标题要带上那个清单自己的图标
            ic = TickIcon(icon_kind, 14, "muted")
            if icon_hex:
                ic.set_color_hex(icon_hex)
            lay.addWidget(ic)
        name = QLabel(title)
        name.setObjectName("GroupWarn" if warn else "GroupTitle")
        lay.addWidget(name)
        cnt = QLabel(str(count))
        cnt.setObjectName("GroupCount")
        lay.addWidget(cnt)
        lay.addStretch(1)
        if action_text:
            btn = QPushButton(action_text)
            btn.setObjectName("LinkBtn")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda: self.action.emit(self.key))
            lay.addWidget(btn)

    def _set_arrow(self) -> None:
        self.arrow.set_kind("chevron_down" if self.expanded else "chevron_right")

    def _blank_span(self) -> tuple[int, int]:
        """标题内容右边缘 ~ 右侧动作按钮左边缘之间那段空白。

        布局里只有一根 stretch，它两边就是「有字的地方」和「按钮」，
        中间这段没有控件，点它应当算点空白而不是折叠分组。
        """
        lay = self.layout()
        gap = next((i for i in range(lay.count())
                    if lay.itemAt(i).spacerItem() is not None), -1)
        if gap < 0:
            return self.width(), self.width()
        left, right = 0, self.width()
        for i in range(gap):
            w = lay.itemAt(i).widget()
            if w is not None:
                left = max(left, w.x() + w.width())
        for i in range(gap + 1, lay.count()):
            w = lay.itemAt(i).widget()
            if w is not None:
                right = min(right, w.x())
        return left, right

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        lo, hi = self._blank_span()
        if lo <= event.position().x() <= hi:
            # 冒泡到列表容器，走「点空白收起详情」那条路
            event.ignore()
            return
        self.expanded = not self.expanded
        self._set_arrow()
        self.toggled.emit(self.key, self.expanded)


class SideRow(QWidget):
    """左栏导航行：图标 + 文字 + 右对齐计数，悬停显示 ＋ / ⋯。

    做成复合控件而不是 QPushButton，是因为滴答的计数要贴着右边缘，
    而 QPushButton 的富文本没法同时做「左对齐标题 + 右对齐数字」。
    """

    picked = Signal(str)
    dropped = Signal(str, int)
    moved = Signal(str, str, bool)        # 组内排序：(被拖的 key, 目标 key, 拖到其后)
    addRequested = Signal(str)
    moreRequested = Signal(str, object)
    expandToggled = Signal(str)

    def __init__(self, key: str, text: str, icon_kind: str = "list",
                 icon_key: str = "muted", icon_hex: str = "", tint: str = "",
                 count: int | str = "", indent: int = 0, expandable: bool = False,
                 expanded: bool = True, show_add: bool = False,
                 dot_hex: str = "", parent=None, reorderable: bool = False):
        super().__init__(parent)
        self.key = key
        self._checked = False
        self._hover = False
        self._expandable = expandable
        self._expanded = expanded
        # 只有同组的两行之间能排序（清单不能拖去和标签换位置）
        self._reorderable = reorderable
        self._press = None
        self.setObjectName("SideRow")
        # 裸 QWidget 默认不理会 QSS 的 background，选中/悬停的底色必须显式打开
        # WA_StyledBackground，否则切 tab 只看到文字变化、完全没有滴答那层高亮。
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(31)
        self.setCursor(Qt.PointingHandCursor)
        self.setAcceptDrops(True)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6 + indent, 0, 6, 0)
        lay.setSpacing(6)

        self.chevron = TickIcon("chevron_down", 11, "muted")
        self.chevron.setFixedSize(12, 12)
        self.chevron.setVisible(expandable)
        # 初始朝向要跟着 expanded 走，否则折叠着的文件夹也画成向下箭头
        self.chevron.set_kind("chevron_down" if expanded else "chevron_right")
        lay.addWidget(self.chevron)

        if icon_hex == "dot":
            self.dot = ColorDot(icon_key, 9)
            self.dot.setFixedSize(12, 12)
            lay.addWidget(self.dot)
            self.icon = None
        else:
            self.dot = None
            self.icon = TickIcon(icon_kind, 15, icon_key or "muted")
            if icon_hex:
                self.icon.set_color_hex(icon_hex)
            lay.addWidget(self.icon)

        self.label = QLabel(text)
        self.label.setObjectName("SideRowText")
        if tint:
            widgets._apply_property(self.label, "tint", tint)
        lay.addWidget(self.label, 1)

        self.add_btn = QPushButton("＋")
        self.add_btn.setObjectName("RowBtn")
        self.add_btn.setFixedSize(18, 18)
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.clicked.connect(lambda: self.addRequested.emit(self.key))
        self.add_btn.setVisible(False)
        if show_add:
            lay.addWidget(self.add_btn)
        self._show_add = show_add

        # 滴答在「名字 … 色点 计数」里用那枚点标出清单 / 标签自己的颜色
        self.dot = ColorDot(dot_hex, 8) if dot_hex else None
        if self.dot is not None:
            self.dot.setFixedSize(12, 12)
            lay.addWidget(self.dot)
        self.count_lbl = QLabel("" if count == "" else str(count))
        self.count_lbl.setObjectName("SideRowCount")
        lay.addWidget(self.count_lbl)

        self.more_btn = QPushButton("⋯")
        self.more_btn.setObjectName("RowBtn")
        self.more_btn.setFixedSize(18, 18)
        self.more_btn.setCursor(Qt.PointingHandCursor)
        self.more_btn.clicked.connect(
            lambda: self.moreRequested.emit(
                self.key, self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft())))
        self.more_btn.setVisible(False)
        lay.addWidget(self.more_btn)
        self._has_menu = True      # _side_row 里按 key 关掉
        self._sync_hover()

    # ---- 状态 ----
    def set_checked(self, on: bool) -> None:
        self._checked = on
        self._repolish()

    def set_text(self, text: str) -> None:
        self.label.setText(text)

    def set_count(self, count: int | str) -> None:
        self.count_lbl.setText("" if count == "" else str(count))

    def set_expanded(self, on: bool) -> None:
        self._expanded = on
        self.chevron.set_kind("chevron_down" if on else "chevron_right")

    def set_icon(self, kind: str = "", key: str = "", hex_value: str = "") -> None:
        if self.icon is None:
            return
        if kind:
            self.icon.set_kind(kind)
        if hex_value:
            self.icon.set_color_hex(hex_value)
        elif key:
            self.icon.set_color_key(key)

    def _repolish(self) -> None:
        widgets._apply_property(self, "checked", "true" if self._checked else "false")

    def _sync_hover(self) -> None:
        self.more_btn.setVisible(self._hover and self._has_menu)
        if self._show_add:
            self.add_btn.setVisible(self._hover and not self._checked)
        if self._hover and not self._checked:
            widgets._apply_property(self, "hover", "true")
        else:
            widgets._apply_property(self, "hover", "false")

    def set_has_menu(self, on: bool) -> None:
        """没有右键菜单的行不显示 ⋯，否则悬停出一枚点了没反应的按钮。"""
        self._has_menu = on
        self._sync_hover()

    # ---- 事件 ----
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self._sync_hover()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self._sync_hover()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press = event.position().toPoint()
            # 吃掉：不吃的话按下会冒到导航容器上，也拿不到后续的 move 事件
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        start = self._press
        if (self._reorderable and start is not None
                and event.buttons() & Qt.LeftButton
                and (event.position().toPoint() - start).manhattanLength()
                >= QApplication.startDragDistance()):
            self._press = None
            mime = QMimeData()
            mime.setData(MIME_NAV, self.key.encode())
            drag = QDrag(self)
            drag.setMimeData(mime)
            pix = widgets.drag_snapshot(self, "nav_bg")
            drag.setPixmap(pix)
            drag.setHotSpot(QPoint(16, self.height() // 2))
            drag.exec(Qt.MoveAction)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 起过拖就没有「按下点」了，松手不再当一次点击（否则拖完顺手切了视图）
        if event.button() != Qt.LeftButton or self._press is None:
            self._press = None
            return
        self._press = None
        if self._expandable and event.position().x() < 26:
            self.set_expanded(not self._expanded)
            self.expandToggled.emit(self.key)
            return
        self.picked.emit(self.key)

    def _nav_drag_ok(self, event) -> bool:
        md = event.mimeData()
        if not md.hasFormat(MIME_NAV):
            return False
        src = bytes(md.data(MIME_NAV)).decode()
        grp = _nav_group(src)
        return bool(grp) and src != self.key and grp == _nav_group(self.key) \
            and _nav_parent(src) == _nav_parent(self.key)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._nav_drag_ok(event):
            event.acceptProposedAction()
            return
        if event.mimeData().hasFormat(MIME_TODO):
            event.acceptProposedAction()
            self._set_drop_hot(True)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._nav_drag_ok(event):
            event.acceptProposedAction()
            self._set_nav_edge("below" if event.position().y()
                               > self.height() // 2 else "above")
            return
        if event.mimeData().hasFormat(MIME_TODO):
            event.acceptProposedAction()
            self._set_drop_hot(True)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._set_drop_hot(False)
        self._set_nav_edge("")

    def dropEvent(self, event) -> None:  # noqa: N802
        self._set_drop_hot(False)
        self._set_nav_edge("")
        if self._nav_drag_ok(event):
            src = bytes(event.mimeData().data(MIME_NAV)).decode()
            after = event.position().y() > self.height() // 2
            self.moved.emit(src, self.key, after)
            event.acceptProposedAction()
            return
        if event.mimeData().hasFormat(MIME_TODO):
            tid = int(bytes(event.mimeData().data(MIME_TODO)).decode())
            self.dropped.emit(self.key, tid)
            event.acceptProposedAction()

    def _set_nav_edge(self, edge: str) -> None:
        widgets._apply_property(self, "navEdge", edge)

    def _set_drop_hot(self, on: bool) -> None:
        """拖任务悬在左栏某行上时把那行点亮。

        没有这个反馈，用户根本看不出「收集箱 / 某个清单」是可以放上去的目标，
        体验上就等于「拖不过去」。
        """
        if bool(getattr(self, "_drop_hot", False)) == on:
            return
        self._drop_hot = on
        widgets._apply_property(self, "dropHot", "true" if on else "false")


class DateChip(QFrame):
    """详情面板顶部的日期胶囊：📅 5天前, 9月14日 ↻ —— 点开日历弹窗。"""

    clicked_ = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DetailMetaRow")
        self.setFixedHeight(28)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(6)
        self.icon = TickIcon("calendar", 15, "muted")
        lay.addWidget(self.icon)
        self.text = QLabel("设置日期")
        self.text.setObjectName("DetailMetaText")
        widgets._apply_property(self.text, "placeholder", "true")
        lay.addWidget(self.text)
        self.repeat_icon = TickIcon("repeat", 13, "muted")
        self.repeat_icon.setVisible(False)
        lay.addWidget(self.repeat_icon)
        lay.addStretch(1)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked_.emit()


_MD_PREFIX = {"-": "ul", "*": "ul", "+": "ul",
              "#": "h1", "##": "h2", "###": "h3", "####": "h4",
              ">": "quote"}


class MarkdownEdit(QTextEdit):
    """描述框：行首敲 ``- `` / ``1. `` / ``# `` 就地变成列表 / 标题。

    存出去的是 markdown 文本（``toMarkdown`` / ``setMarkdown`` 往返），
    所以重新打开任务时列表和标题还在；正文里没用到语法时就是普通一段话。
    """

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key, mods = event.key(), event.modifiers()
        if mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            super().keyPressEvent(event)
            return
        blk = self.textCursor().block()
        line = blk.text()
        if key == Qt.Key_Space:
            kind = _MD_PREFIX.get(line.strip())
            if kind and self._at_line_end():
                self._apply_block(kind)
                return
        if key == Qt.Key_Period and line.strip().isdigit() and self._at_line_end():
            self._apply_block("ol")
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if self._on_enter(blk, line):
                return
        super().keyPressEvent(event)

    def _at_line_end(self) -> bool:
        c = self.textCursor()
        return c.position() == c.block().position() + c.block().length() - 1

    def _apply_block(self, kind: str) -> None:
        c = self.textCursor()
        c.select(QTextCursor.LineUnderCursor)
        c.removeSelectedText()
        if kind in ("h1", "h2", "h3", "h4"):
            bf = QTextBlockFormat()
            bf.setHeadingLevel(int(kind[1]))
            c.mergeBlockFormat(bf)
            return
        if kind == "quote":
            bf = QTextBlockFormat()
            bf.setLeftMargin(14)
            c.mergeBlockFormat(bf)
            return
        lst = c.insertList(QTextListFormat.Style.ListDisc if kind == "ul"
                           else QTextListFormat.Style.ListDecimal)
        # insertList 是「另起一块」而不是就地转换：刚才清出来的那个空行要收掉，
        # 否则每条列表前都会多一个空行。
        prev = c.block().previous()
        if (prev.isValid() and not prev.text() and prev.textList() is None
                and lst is not None):
            dc = QTextCursor(prev)
            dc.select(QTextCursor.BlockUnderCursor)
            dc.removeSelectedText()
            dc.deleteChar()
            self.setTextCursor(c)

    def _on_enter(self, blk, line: str) -> bool:
        """空条目上回车退出列表；标题后回车不该继续是标题。其余交给 Qt 续条目。"""
        if blk.blockFormat().headingLevel() > 0:
            self.textCursor().insertBlock(QTextBlockFormat())
            return True
        lst = blk.textList()
        if lst is not None and not line.strip():
            lst.remove(blk)
            bf = QTextBlockFormat()
            self.textCursor().mergeBlockFormat(bf)
            return True
        return False


class TitleEdit(QTextEdit):
    """详情面板的标题框：长标题折行显示，但永远只算一句话。

    以前是 QLineEdit —— 单行控件不折行，长标题两头被裁（滴答那边是换行展示的）。
    换成 QTextEdit 才折得开，代价是要自己把「真的换行」这条路堵掉：
    回车改成提交、粘贴带进来的换行压成空格。

    选 QTextEdit 而不是 QPlainTextEdit：后者只在 resizeEvent 里才把 textWidth
    写给文档，实测 documentSizeChanged 报的是 (344, 3.0) 这种没排版过的值，
    高度算不出来。描述框（MarkdownEdit）也是 QTextEdit，
    _sync_note_height 那套 doc.size().height() 在生产里是准的，跟着它走。
    """

    entered = Signal()      # 回车：交给面板去提交，不在文档里插换行

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            event.accept()
            self.entered.emit()
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        text = source.text() if source is not None else ""
        if text:
            self.insertPlainText(" ".join(text.split()))


class DetailPane(QFrame):
    """右侧任务详情：结构对齐滴答 —— 顶部日期行、标题、描述、子任务，底部清单栏。

    改动即时落盘（滴答没有「保存」按钮）。提醒 / 重复 / 时长不占独立行，
    收进右下角「⋯」菜单，和滴答一样保持面板紧凑。
    """

    doneFlipped = Signal(int, str, bool)
    taskSaved = Signal(int)
    taskDeleted = Signal(int)
    tagsChanged = Signal()        # 只改标签：刷角标就够，别整页重建
    backRequested = Signal()      # 窄屏整页化时的「← 返回列表」
    subClicked = Signal(dict)     # 点某条子任务标题（复习条目 → 跳到那一题）

    def __init__(self):
        super().__init__()
        self.setObjectName("DetailPane")
        # 可压缩区间：固定 420 会给整个窗口顶出一个缩不下去的下限。
        # 但只给 min/max 的话布局按 sizeHint 分配，宽屏下也只剩 300 宽，
        # 所以再把 sizeHint 明确写成 420（见下方 sizeHint）。
        self.setMinimumWidth(self.MIN_W)
        self.setMaximumWidth(self.MAX_W)
        self._task: dict | None = None
        # 从重复周期打开时带上周期标识与显示日期，勾选 / 改期都只作用在这一周期上
        self._occ = ""
        self._disp_date = ""
        self._disp_time = ""
        self._loading = False
        self._date_popup: DatePickerPopup | None = None
        # 边打字边存：500ms 静默后落盘
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(0, 0, 0, 0)
        el.setSpacing(16)
        el.addStretch(1)
        art = _EmptyArt()
        art.setFixedSize(132, 132)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(art)
        row.addStretch(1)
        el.addLayout(row)
        cap = QLabel("点击任务标题查看详情")
        cap.setObjectName("DetailEmpty")
        cap.setAlignment(Qt.AlignCenter)
        el.addWidget(cap)
        el.addStretch(2)

        # ---- 顶部：清单图标 | 日期 ……… 优先级旗 ----
        head = QFrame()
        head.setObjectName("DetailHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 10, 12, 10)
        hl.setSpacing(8)
        # 窄屏时详情整页铺满，靠这颗返回箭头回到列表；宽屏下没有意义，先藏着
        self.back_btn = QPushButton("‹")
        self.back_btn.setObjectName("DetailBack")
        self.back_btn.setFixedSize(24, 24)
        self.back_btn.setCursor(Qt.PointingHandCursor)
        self.back_btn.clicked.connect(lambda: self.backRequested.emit())
        self.back_btn.hide()
        hl.addWidget(self.back_btn)
        # 滴答把完成勾选框放在顶栏左上角。原来这个位置是一个纯装饰的清单图标
        # （点不动），标题行反而挂着勾选框 —— 换过来。清单名底栏本来就有。
        self.done_check = PrioCheckBox(False, 19)
        self.done_check.toggled.connect(self._on_check)
        hl.addWidget(self.done_check)
        bar = QFrame()
        bar.setObjectName("DetailVBar")
        bar.setFixedWidth(1)
        bar.setFixedHeight(16)
        hl.addWidget(bar)
        self.date_chip = DateChip()
        self.date_chip.clicked_.connect(self._open_date_popup)
        hl.addWidget(self.date_chip, 1)
        self.flag = FlagButton(0, 30)
        self.flag.clicked_.connect(self._open_flag_menu)
        hl.addWidget(self.flag)
        root.addWidget(head)
        head_line = _Hairline()
        root.addWidget(head_line)

        # ---- 主体 ----
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.form = QWidget()
        form = QVBoxLayout(self.form)
        form.setContentsMargins(16, 12, 16, 12)
        form.setSpacing(8)
        self.scroll.setWidget(self.form)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.title_input = TitleEdit()
        self.title_input.setObjectName("DetailTitleInput")
        self.title_input.setPlaceholderText("标题")
        # 折行靠控件宽度，不要横向滚动条；高度自己算，所以纵向条也关掉
        self.title_input.setFrameStyle(QFrame.NoFrame)
        self.title_input.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.title_input.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.title_input.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.title_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # QTextEdit 默认给文档 4px 留白；标题的上下留白已经由 QSS 的
        # padding: 2px 0 负责，文档自己再留一层就会短标题也撑出两行高。
        self.title_input.document().setDocumentMargin(0)
        # textEdited 是 QLineEdit 才有的信号；多行框只有 textChanged，
        # 它连程序赋值也会发，所以回调里必须看 _loading。
        self.title_input.textChanged.connect(self._on_title_edited)
        self.title_input.entered.connect(self._on_title_entered)
        # 和描述框一模一样的连法：contentsChanged 在 QTextDocument 上不在控件上，
        # 而 documentSizeChanged 对 QTextEdit 是准的（对 QPlainTextEdit 不准，
        # 见 TitleEdit 的类注释）。
        self.title_input.document().documentLayout().documentSizeChanged.connect(
            lambda _s: self._sync_title_height())
        title_row.addWidget(self.title_input, 1)
        # 详情头部也给一个跳转按钮：行上的图标小，想在编辑器里核对链接时点这个
        self.link_btn = QPushButton()
        self.link_btn.setObjectName("ToolBtn")
        self.link_btn.setFixedSize(26, 26)
        self.link_btn.setCursor(Qt.PointingHandCursor)
        self.link_btn.setToolTip("在浏览器打开")
        TickIcon("link", 15, "accent", self.link_btn).move(6, 6)
        self.link_btn.clicked.connect(self._open_link)
        self.link_btn.hide()
        title_row.addWidget(self.link_btn)
        self.subs_toggle = QPushButton()
        self.subs_toggle.setObjectName("ToolBtn")
        self.subs_toggle.setFixedSize(26, 26)
        self.subs_toggle.setCursor(Qt.PointingHandCursor)
        self.subs_toggle.setToolTip("添加子任务")
        TickIcon("subtask_list", 15, "muted", self.subs_toggle).move(6, 6)
        self.subs_toggle.clicked.connect(self._toggle_subs)
        title_row.addWidget(self.subs_toggle)
        form.addLayout(title_row)

        self.note_input = MarkdownEdit()
        self.note_input.setObjectName("DetailDescInput")
        self.note_input.setPlaceholderText("描述（支持 markdown：- 无序、1. 有序、# 标题）")
        # 原来固定 64~220 + Expanding，只写三行时下面也空出一大块，子任务被
        # 顶到面板中间，整块看着像坏了。改成跟着文档实际高度长，最多 240。
        self._note_min_h, self._note_max_h = 44, 240
        self.note_input.setMinimumHeight(self._note_min_h)
        self.note_input.setMaximumHeight(self._note_max_h)
        self.note_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.note_input.document().documentLayout().documentSizeChanged.connect(
            lambda _s: self._sync_note_height())
        self.note_input.textChanged.connect(lambda: self._save_timer.start())
        form.addWidget(self.note_input)

        self.sub_wrap = QWidget()
        sw = QVBoxLayout(self.sub_wrap)
        sw.setContentsMargins(0, 6, 0, 0)
        sw.setSpacing(0)
        self.sub_list = QVBoxLayout()
        self.sub_list.setSpacing(0)
        sw.addLayout(self.sub_list)
        self.sub_add_row = QWidget()
        sa = QHBoxLayout(self.sub_add_row)
        sa.setContentsMargins(0, 0, 0, 0)
        sa.setSpacing(8)
        self._add_check = PrioCheckBox(False, 17)
        self._add_check.setEnabled(False)
        sa.addWidget(self._add_check)
        self.sub_add = QLineEdit()
        self.sub_add.setObjectName("SubAddInput")
        self.sub_add.setPlaceholderText("添加子任务")
        self.sub_add.returnPressed.connect(self._add_subtask)
        sa.addWidget(self.sub_add, 1)
        sw.addWidget(self.sub_add_row)
        form.addWidget(self.sub_wrap)
        # 默认不留「添加子任务」那一行，点标题右侧的子任务按钮才出现（滴答这样）
        self.sub_add_row.setVisible(False)

        # 标签：theme 里 TagChip 那套配色早就写好了，一直没被引用过
        self.tags_wrap = QWidget()
        tw = QHBoxLayout(self.tags_wrap)
        tw.setContentsMargins(0, 2, 0, 0)
        tw.setSpacing(6)
        self.tag_add_btn = QPushButton("＋ 标签")
        self.tag_add_btn.setObjectName("TagAddBtn")
        self.tag_add_btn.setCursor(Qt.PointingHandCursor)
        self.tag_add_btn.clicked.connect(self._open_tag_picker)
        tw.addWidget(self.tag_add_btn)
        tw.addStretch(1)
        form.addWidget(self.tags_wrap)
        form.addStretch(1)
        root.addWidget(self.scroll, 1)

        # ---- 底部：清单 ……… 样式 / 评论 / 更多 ----
        foot = QFrame()
        foot.setObjectName("DetailFoot")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(16, 8, 12, 8)
        fl.setSpacing(6)
        self.foot_list_btn = QPushButton()
        self.foot_list_btn.setObjectName("ToolBtn")
        self.foot_list_btn.setCursor(Qt.PointingHandCursor)
        self.foot_list_btn.setIconSize(QSize(16, 16))
        self.foot_list_btn.setFixedHeight(26)
        self.foot_list_btn.clicked.connect(self._open_list_menu)
        fl.addWidget(self.foot_list_btn)
        fl.addStretch(1)
        self._foot_more = QPushButton()
        self._foot_more.setObjectName("ToolBtn")
        self._foot_more.setFixedSize(26, 26)
        self._foot_more.setCursor(Qt.PointingHandCursor)
        TickIcon("more", 15, "muted", self._foot_more).move(6, 6)
        self._foot_more.clicked.connect(self._open_more_menu)
        fl.addWidget(self._foot_more)
        foot_line = _Hairline()
        root.addWidget(foot_line)
        root.addWidget(foot)

        # 顺序：空状态 / 顶栏 / 分隔线 / 主体 / 分隔线 / 底栏（insertWidget 把空状态提到最前）
        root.insertWidget(0, self.empty)
        self._head, self._foot = head, foot
        self._head_line, self._foot_line = head_line, foot_line
        self._head.hide()
        self._foot.hide()
        self._head_line.hide()
        self._foot_line.hide()
        self.scroll.hide()

    # ---- 尺寸区间 ----
    # 宽度由 TodoPage 的 QSplitter 管（可拖、可拖到边收起），这里只给上下限，
    # 不再 setFixedWidth —— 钉死宽度会让分割器拖不动。
    FULL_W, MIN_W = 420, 260
    MAX_W = 620

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.FULL_W, 480)

    def has_task(self) -> bool:
        return self._task is not None

    def set_floating(self, on: bool) -> None:
        """浮起模式（窄窗口抽屉）：左上角从「返回箭头」换成 ✕，
        并把 min/max 放开 —— 宽度这时由页面直接摆，不再由分割器管。"""
        self.back_btn.setText("✕" if on else "‹")
        self.back_btn.setVisible(on)
        self.setMinimumWidth(0 if on else self.MIN_W)
        self.setMaximumWidth(16777215 if on else self.MAX_W)
        self.updateGeometry()

    # ---- 装载 ----
    def _current_note(self) -> str:
        """编辑器里现在这份文本，按「会怎么落库」的方式取。"""
        return (self.note_input.toMarkdown() if self._has_md_blocks()
                else self.note_input.toPlainText()).strip()

    def show_task(self, task: dict, occ: str = "", disp_date: str = "",
                  disp_time: str = "") -> None:
        same = (self._task is not None and self._task["id"] == task["id"]
                and occ == self._occ)
        # 勾完成 / 改期之后会 reload 再回到这里装载同一条。描述框是 500ms
        # 防抖才落库的，不判脏就直接覆盖，会把用户正在打的字吃掉。
        dirty_title = same and self.title_input.toPlainText() != (
            self._task.get("title") or "")
        dirty_note = same and self._current_note() != (
            self._task.get("note") or "")
        # 上面两条比的是「库里旧值 vs 框里的字」。防抖落库时会先把新值同步进
        # self._task，于是两条都判成「不脏」，这里就把一模一样的内容重灌一遍 ——
        # 光标弹回开头，打字每停 500ms 跳一次。再比一次「和要装的新值一样吗」，
        # 一样就根本不碰这两个框（光标、滚动位置都留在原处）。
        keep_title = same and self.title_input.toPlainText() == (
            task.get("title") or "")
        keep_note = same and self._current_note() == (
            (task.get("note") or "").strip())
        self._task = task
        self._occ = occ
        self._disp_date = disp_date
        self._disp_time = disp_time
        self._loading = True
        self.empty.hide()
        self._head.show()
        self._head_line.show()
        self._foot.show()
        self._foot_line.show()
        self.scroll.show()
        if not dirty_title and not keep_title:
            self.title_input.setPlainText(task["title"])
        if not dirty_note and not keep_note:
            self._load_md(self.note_input, task.get("note") or "")
        self._link_url = _first_url(task)
        self.link_btn.setVisible(bool(self._link_url))
        if self._link_url:
            self.link_btn.setToolTip("在浏览器打开\n%s" % self._link_url)
        self.flag.set_priority(task.get("priority", 0))
        # 放弃的在行上是「勾着 + ✕」，详情这枚框也必须显示成勾着，
        # 否则同一条任务在两个地方说法不一样
        self.done_check.set_checked(_settled(task))
        self.done_check.set_priority(task.get("priority", 0))
        self._sync_date_row()
        self._sync_list_rows()
        self._build_subtasks()
        self._rebuild_tags()
        self._loading = False
        # 装载过程本身会触发 textChanged，这里掐掉，避免把「读取」当成「编辑」回写。
        # 但上面判脏跳过了覆盖，说明用户还有没落库的字 —— 这种时候不能掐，
        # 反而要重新计时，否则字留在框里却永远不进库。
        if dirty_title or dirty_note:
            self._save_timer.start()
        else:
            self._save_timer.stop()

    def clear(self) -> None:
        self._task = None
        self._link_url = ""
        self._occ = self._disp_date = self._disp_time = ""
        self._save_timer.stop()
        self._head.hide()
        self._head_line.hide()
        self._foot.hide()
        self._foot_line.hide()
        self.scroll.hide()
        self.empty.show()

    def _sync_date_row(self) -> None:
        t = self._task
        date = self._disp_date or t["due_date"]
        time_v = self._disp_time if self._disp_date else (t.get("due_time") or "")
        if not date:
            self.date_chip.text.setText("设置日期")
            widgets._apply_property(self.date_chip.text, "placeholder", "true")
            self.date_chip.icon.set_color_key("muted")
            self.date_chip.repeat_icon.setVisible(False)
            return
        self.date_chip.text.setText(
            _detail_date_label(date, time_v,
                               "" if self._occ else t.get("end_date") or ""))
        widgets._apply_property(self.date_chip.text, "placeholder", "false")
        # 跨天的任务要过了结束日才算逾期，按起始日判会提前红起来
        ref = (t.get("end_date") or "") if not self._occ else ""
        rd = QDate.fromString(ref, "yyyy-MM-dd")
        if not (rd.isValid() and rd > QDate.fromString(date, "yyyy-MM-dd")):
            rd = QDate.fromString(date, "yyyy-MM-dd")
        left = QDate.currentDate().daysTo(rd)
        self.date_chip.icon.set_color_key("red" if left < 0 else "accent")
        self.date_chip.text.setStyleSheet(
            f"color: {theme.get('red') if left < 0 else theme.get('text')};")
        self.date_chip.repeat_icon.setVisible(bool((t.get("repeat") or "").strip()))

    def _sync_list_rows(self) -> None:
        """底栏显示所属清单的图标 + 名字（顶栏那个位置现在是完成勾选框）。"""
        name = self._task.get("list_name") or "收集箱"
        icon_kind = "inbox"
        if name != "收集箱":
            for lst in services.list_all():
                if lst["name"] == name:
                    icon_kind = lst.get("icon") or "list"
                    break
        else:
            icon_kind = "inbox"
        # 底栏按钮：图标嵌在文字左边
        self.foot_list_btn.setText(f"  {name}")
        self.foot_list_btn.setStyleSheet(
            "text-align: left; padding-left: 24px; font-size: 13px;")
        for ch in self.foot_list_btn.findChildren(TickIcon):
            ch.deleteLater()
        ic = TickIcon(icon_kind, 15, "muted", self.foot_list_btn)
        ic.move(4, 6)
        ic.show()

    def _toggle_subs(self) -> None:
        """标题右侧的子任务按钮 = 「添加子任务」：点一下才出现输入行并聚焦。"""
        self.sub_wrap.setVisible(True)
        self.sub_add_row.setVisible(True)
        self.sub_add.setFocus()

    def _open_link(self) -> None:
        _open_url(getattr(self, "_link_url", ""), self)

    # ---- 勾选 / 标题 ----
    def _on_check(self, checked: bool) -> None:
        if self._loading or not self._task:
            return
        if (not checked and int(self._task.get("abandoned") or 0)
                and not int(self._task.get("done") or 0)):
            # 框上那个「勾」其实是放弃的 ✕：取消它 = 回到待做。
            # 只写 done=0 的话框还是勾着的，看着像点了没反应
            services.todo_update(self._task["id"], abandoned=0)
            self._task["abandoned"] = 0
        self._task["done"] = int(checked)
        services.occ_set_done(self._task["id"], self._occ, checked)
        if checked and not _review_ask_group(self, self._task["id"]):
            services.occ_set_done(self._task["id"], self._occ, False)
            self._task["done"] = 0      # 复习结论没答，这条已经退回未完成
        elif checked and not services.review_kind_of_todo(self._task["id"]):
            sounds.play("todo_done")
        self.doneFlipped.emit(self._task["id"], self._occ,
                              bool(self._task["done"]))
        self.taskSaved.emit(self._task["id"])

    def _on_title_edited(self) -> None:
        # 多行框只有 textChanged，程序赋值也会发（show_task 里那句 setText）。
        # 不挡 _loading 就会把刚装载的标题当成用户改动再写回库。
        if self._loading or not self._task:
            return
        text = self.title_input.toPlainText()
        if text.strip():
            self._task["title"] = text
            self._save_timer.start()

    def _on_title_entered(self) -> None:
        """回车 = 提交。QLineEdit 会自己发 editingFinished，多行框不会。"""
        self._save()

    def _sync_title_height(self) -> None:
        """把高度重算推到下一轮事件循环。

        documentSizeChanged 触发的**当时** document().size() 还是旧值
        （实测刚 setPlainText 一个短标题，它报 0.0），当场算会把控件
        留在上一个、更长的标题的高度上。defer 一拍才量到新的。
        """
        QTimer.singleShot(0, self._apply_title_height)

    def _apply_title_height(self) -> None:
        """标题框高度跟着折行结果走：一行高起步，最多约六行，再多才内部滚动。

        驱动有两路：documentSizeChanged 管打字和重新排版，resizeEvent 管面板
        被 QSplitter 拖窄这一路（宽度变了但文档内容没变）。
        """
        ed = self.title_input
        fm = ed.fontMetrics()
        lo = fm.height() + 8
        hi = lo + 5 * fm.lineSpacing()
        # documentMargin 已在构造里归零，这里再加一次就成了凭空多出一行
        want = int(ed.document().size().height() + 2 * ed.frameWidth() + 8)
        want = max(lo, min(hi, want))
        if ed.height() != want:
            ed.setFixedHeight(want)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_title_height()

    # ---- 菜单 ----
    def _menu(self, items: list[tuple[object, str, str]], on_pick,
              anchor: QWidget, checked: object = None,
              danger: tuple[object, ...] = ()) -> None:
        menu = TickMenu(items, anchor, checked=checked, danger=danger)
        menu.picked.connect(on_pick)
        menu.exec_at(anchor.mapToGlobal(QPoint(0, anchor.height())))

    def _open_flag_menu(self) -> None:
        if not self._task:
            return
        prio = int(self._task.get("priority", 0) or 0)

        def pick(v: object) -> None:
            self._task["priority"] = int(v)
            self.flag.set_priority(int(v))
            self._save()
        self._menu(_prio_items(), pick, self.flag, checked=prio)

    def _open_list_menu(self) -> None:
        if not self._task:
            return
        items = [("收集箱", "inbox", "收集箱")]
        items += [(l["name"], "list", l["name"])
                  for l in services.list_all() if l.get("kind", "list") == "list"]

        def pick(name: object) -> None:
            self._task["list_name"] = str(name)
            self._sync_list_rows()
            self._save()
        self._menu(items, pick, self.foot_list_btn,
                   checked=self._task.get("list_name") or "收集箱")

    def _open_more_menu(self) -> None:
        """提醒 / 重复 / 时长收在这个菜单里，详情面板才不会堆一排元信息行。

        三项都得把当前值写在标题上：原来设完界面上没有任何地方读得回来，
        duration_min 存进去就等于消失。
        """
        if not self._task:
            return
        t = self._task
        rem = t.get("reminder") or ""
        rep = services.REPEAT_NAMES.get(t.get("repeat") or "", "")
        dur = int(t.get("duration_min") or 0)
        items = [
            ("remind", "clock",
             ("提醒：" + _human_reminder(rem)) if rem else "提醒"),
            ("repeat", "repeat", ("重复：" + rep) if rep else "重复"),
            ("duration", "duration",
             ("时长：" + _human_duration(dur)) if dur else "时长"),
            ("archive", "archive",
             "取消归档" if t.get("archived") else "归档任务"),
            ("delete", "trash", "删除")]
        self._menu(items, self._pick_more, self._foot_more, danger=("delete",))

    def _pick_more(self, v: object) -> None:
        v = str(v)
        if v == "remind":
            self._open_remind_menu()
        elif v == "repeat":
            self._open_repeat_menu()
        elif v == "duration":
            self._open_duration_menu()
        elif v == "archive":
            tid = self._task["id"]
            # 右键菜单照滴答改窄之后，这里是「取消归档」的唯一入口（在已归档视图里）
            services.todo_update(tid, archived=0 if self._task.get("archived") else 1)
            self.clear()
            self.taskDeleted.emit(tid)
        elif v == "delete":
            self._delete()

    def _open_repeat_menu(self) -> None:
        if not self._task:
            return
        def pick(v: object) -> None:
            self._task["repeat"] = str(v)
            self._sync_date_row()
            self._save()
        items = [(val, "repeat" if val else "circle", lab)
                 for val, lab in services.REPEAT_OPTIONS]
        self._menu(items, pick, self._foot_more,
                   checked=self._task.get("repeat") or "")

    def _open_remind_menu(self) -> None:
        if not self._task:
            return
        def pick(v: object) -> None:
            v = str(v)
            if v == "clear":
                self._task["reminder"] = ""
            elif v == "at_time":
                due = self._task.get("due_date") or ""
                tm = self._task.get("due_time") or "09:00"
                self._task["reminder"] = f"{due} {tm}" if due else ""
            else:
                base = (self._task.get("due_date")
                        or QDate.currentDate().toString("yyyy-MM-dd"))
                d = QDate.fromString(base, "yyyy-MM-dd")
                self._task["reminder"] = (
                    d.addDays(int(v)).toString("yyyy-MM-dd") + " 09:00")
            self._save()
        # reminder 存的是绝对时间串，得反推当初选的是哪一档，否则只要设过提醒
        # 勾就永远打在「准时」上（哪怕选的是提前 1 天）
        rem = (self._task.get("reminder") or "").strip()
        due = self._task.get("due_date") or ""
        cur = ""
        if rem:
            if rem == f"{due} {self._task.get('due_time') or '09:00'}".strip():
                cur = "at_time"
            else:
                base = QDate.fromString(
                    due or QDate.currentDate().toString("yyyy-MM-dd"),
                    "yyyy-MM-dd")
                for k in ("1", "7"):
                    if (base.isValid()
                            and rem == base.addDays(int(k)).toString("yyyy-MM-dd")
                            + " 09:00"):
                        cur = k
                        break
        items = [("at_time", "clock", "准时"), ("1", "clock", "提前 1 天"),
                 ("7", "clock", "提前 1 周"), ("clear", "circle", "无提醒")]
        self._menu(items, pick, self._foot_more, checked=cur)

    def _open_duration_menu(self) -> None:
        if not self._task:
            return
        cur = int(self._task.get("duration_min") or 0)

        def pick(v: object) -> None:
            self._task["duration_min"] = int(v or 0)
            self._save()
        items = [(m, "duration", _human_duration(m)) for m in DURATION_OPTIONS]
        items.append((0, "circle", "无时长"))
        self._menu(items, pick, self._foot_more,
                   checked=cur if cur in DURATION_OPTIONS else None)

    # ---- 日期弹窗 ----
    def _open_date_popup(self) -> None:
        if not self._task:
            return
        t = self._task
        pop = DatePickerPopup(self._disp_date or t.get("due_date") or "",
                              (self._disp_time if self._disp_date
                               else t.get("due_time")) or "",
                              repeat=t.get("repeat") or "",
                              reminder=t.get("reminder") or "",
                              end_date="" if self._occ else t.get("end_date") or "",
                              end_time="" if self._occ else t.get("end_time") or "",
                              # 单个重复周期没有「跨天」可存（occ 表只有改期字段）
                              allow_range=not self._occ)
        pop.accepted.connect(self._on_date_picked)
        pop.cleared.connect(self._on_date_cleared)
        pop.repeatPicked.connect(self._on_repeat_picked)
        pop.reminderPicked.connect(self._on_reminder_picked)
        self._date_popup = pop
        pop.adjustSize()
        anchor = self.date_chip.mapToGlobal(self.date_chip.rect().bottomLeft())
        pop.move(anchor.x(), anchor.y() + 4)
        pop.show()

    def _on_repeat_picked(self, value: str) -> None:
        """弹层里改了重复规则：直接落到系列上（occ 视图下改周期也走同一套）。"""
        if not self._task:
            return
        services.todo_set_repeat(self._task["id"], value)
        self._task["repeat"] = value
        self._save()

    def _on_reminder_picked(self, value: str) -> None:
        if not self._task:
            return
        self._task["reminder"] = value
        self._save()

    def _on_date_picked(self, date: str, time_v: str, end_date: str = "",
                        end_time: str = "") -> None:
        if not self._task:
            return
        if not _review_date_guard(self, self._task["id"]):
            return
        if self._occ:
            # 从周期打开的就只挪这个周期，其它周期留在系列原位
            services.occ_move(self._task["id"], self._occ, date, time_v)
            self._disp_date, self._disp_time = date, time_v
        else:
            self._task["due_date"], self._task["due_time"] = date, time_v
            self._task["end_date"], self._task["end_time"] = end_date, end_time
        self._sync_date_row()
        self._save()

    def _on_date_cleared(self) -> None:
        if not self._task:
            return
        if self._occ:
            services.occ_delete(self._task["id"], self._occ)   # 重复系列=跳过此周期
            self._disp_date = self._disp_time = ""
        else:
            self._task["due_date"] = self._task["due_time"] = ""
            self._task["end_date"] = self._task["end_time"] = ""
            # 提醒是挂在截止日上的，日期清掉不清提醒的话，行上会一直留着
            # 一枚点了没反应的闹钟图标
            self._task["reminder"] = ""
        self._sync_date_row()
        self._save()

    # ---- 子任务 ----
    def _build_subtasks(self) -> None:
        while self.sub_list.count():
            it = self.sub_list.takeAt(0)
            w = it.widget()
            if w is not None:
                # takeAt 之后 item 就不再持有该控件，必须只取一次
                widgets.drop_widget(w)
        self.sub_add_row.setVisible(False)   # 换任务就收回去，点按钮再出来
        if not self._task:
            self.sub_wrap.setVisible(False)
            return
        subs = services.subtask_list(self._task["id"])
        for sub in subs:
            self._append_subtask_row(sub)
        self.sub_wrap.setVisible(bool(subs))

    def _append_subtask_row(self, sub: dict) -> None:
        row = _SubRow(sub)
        row.toggled_sub.connect(self._on_subtask_check)
        row.deleted_sub.connect(self._del_subtask)
        row.moved.connect(self._on_sub_moved)
        row.subClicked.connect(self.subClicked.emit)
        self.sub_list.addWidget(row)

    def _on_sub_moved(self, src_id: int, over_id: int, after: bool) -> None:
        """拖完一行：按新顺序重写整列 sort_order（子任务不多，全量写最省事）。"""
        if not self._task:
            return
        ids = [s["id"] for s in services.subtask_list(self._task["id"])]
        if src_id not in ids or over_id not in ids:
            return
        ids.remove(src_id)
        ids.insert(ids.index(over_id) + (1 if after else 0), src_id)
        for i, sid in enumerate(ids):
            services.subtask_update(sid, sort_order=i)
        self._build_subtasks()

    def _on_subtask_check(self, sub: dict, checked: bool) -> None:
        services.subtask_update(sub["id"], done=int(checked))
        sub["done"] = int(checked)
        if checked and not _review_ask_sub(self, sub):
            return            # 复习结论没答，这条已经退回未勾，不用刷音也不用重建
        # 答完结论的复习条目会被重排到别的日子、这一条直接从库里消失，
        # 所以那两种情况不再叠一层「子任务完成」音（答对/答错音已经响过了）
        if checked and not services.review_kind_of_sub(sub["id"]):
            sounds.play("subtask_done")
        if self._task:
            self.taskSaved.emit(self._task["id"])

    def _add_subtask(self) -> None:
        text = self.sub_add.text().strip()
        if not text or not self._task:
            return
        services.subtask_add(self._task["id"], text)
        self.sub_add.clear()
        self._build_subtasks()
        # 连着加好几条时不用每次再点一次按钮：输入行留在原地并继续聚焦
        self.sub_wrap.setVisible(True)
        self.sub_add_row.setVisible(True)
        self.sub_add.setFocus()
        self.taskSaved.emit(self._task["id"])

    def _del_subtask(self, sub_id: int) -> None:
        services.subtask_delete(sub_id)
        self._build_subtasks()
        if self._task:
            self.taskSaved.emit(self._task["id"])

    # ---- 删除 ----
    def _delete(self) -> None:
        if not self._task:
            return
        tid = self._task["id"]
        services.occ_delete(tid, self._occ)   # 有周期=跳过此周期，否则整条进垃圾桶
        self.clear()
        self.taskDeleted.emit(tid)

    # ---- 标签 ----
    def _task_tag_ids(self) -> list[int]:
        return [t["id"] for t in services.todo_tags(self._task["id"])]

    def _rebuild_tags(self) -> None:
        """把「这条挂了哪些标签」显出来。原来只能靠 @标签 语法或拖到左栏，
        界面上既看不到也改不了；theme 里 TagChip 那套配色一直是死样式。"""
        row = getattr(self, "tags_wrap", None)
        if row is None or not self._task:
            return
        lay = row.layout()
        # 清掉旧 chip：第 0 个是「＋ 标签」按钮、末尾是 stretch，两个都留着
        while lay.count() > 2:
            it = lay.takeAt(1)
            w = it.widget()
            if w is not None:
                widgets.drop_widget(w)
        by_id = {t["id"]: t for t in services.tag_all()}
        for tid in self._task_tag_ids():
            tg = by_id.get(tid)
            if tg is None:
                continue
            chip = QPushButton(tg["name"])
            chip.setObjectName("TagChip")
            chip.setCheckable(True)
            chip.setChecked(True)
            widgets._apply_property(chip, "tagColor", tg.get("color") or "muted")
            chip.setToolTip("点击移除该标签")
            chip.setCursor(Qt.PointingHandCursor)
            chip.clicked.connect(lambda _=False, i=tid: self._toggle_tag(i))
            lay.insertWidget(1, chip)

    def _open_tag_picker(self) -> None:
        if not self._task:
            return
        pop = TagPickPopup(services.tag_all(), set(self._task_tag_ids()), self)
        pop.toggled.connect(self._toggle_tag)
        self._tag_popup = pop
        pop.exec_at(self.tag_add_btn.mapToGlobal(
            QPoint(0, self.tag_add_btn.height())))

    def _toggle_tag(self, tag_id: int) -> None:
        if not self._task:
            return
        cur = self._task_tag_ids()
        nxt = [x for x in cur if x != tag_id] if tag_id in cur else cur + [tag_id]
        services.todo_set_tags(self._task["id"], nxt)
        self._rebuild_tags()
        # 不能发 taskSaved：那条链会 reload() 整页重建，顺带把标签弹层踢掉，
        # 「连着勾几个标签」就退化成勾一个关一次。整页重建也要 600ms+。
        self.tagsChanged.emit()

    # ---- 落盘 ----
    @staticmethod
    def _load_md(edit, text: str) -> None:
        edit.setMarkdown(text)

    def _sync_note_height(self) -> None:
        """描述框跟着文档实际高度走：空的时候两行高，写多了才长到上限再内滚。"""
        doc = self.note_input.document()
        want = int(doc.size().height() + 2 * self.note_input.frameWidth()
                   + 2 * doc.documentMargin() + 6)
        want = max(self._note_min_h, min(self._note_max_h, want))
        if self.note_input.height() != want:
            self.note_input.setFixedHeight(want)

    def _has_md_blocks(self) -> bool:
        """文档里有没有列表 / 标题块。没有就按纯文本存 —— toMarkdown 会给
        ``*`` ``_`` ``[`` 这些字符加反斜杠转义，普通备注走一遍就被弄脏了。"""
        block = self.note_input.document().firstBlock()
        while block.isValid():
            if block.blockFormat().headingLevel() or block.textList() is not None:
                return True
            block = block.next()
        return False

    def _save(self) -> None:
        if self._loading or not self._task:
            return
        title = self.title_input.toPlainText().strip()
        if not title:
            return
        self._task["title"] = title
        self._task["note"] = self._current_note()
        fields: dict = dict(
            title=title,
            note=self._task["note"],
            priority=self._task.get("priority", 0),
            reminder=self._task.get("reminder", ""),
            repeat=self._task.get("repeat", ""),
            duration_min=int(self._task.get("duration_min") or 0),
            list_name=self._task.get("list_name", "收集箱"),
        )
        # 从某个重复周期打开时不提交截止日：那是整个系列的日期，
        # 单周期的挪动已经由 occ_move 写成例外了。
        if not self._occ:
            fields["due_date"] = self._task.get("due_date", "")
            fields["due_time"] = self._task.get("due_time", "")
            fields["end_date"] = self._task.get("end_date", "")
            fields["end_time"] = self._task.get("end_time", "")
        services.todo_update(self._task["id"], **fields)
        self.taskSaved.emit(self._task["id"])


class _Hairline(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Hairline")
        self.setFixedHeight(1)


_SUB_MIME = "application/x-life-subtask"


class _SubRow(QFrame):
    """子任务行：⋮⋮ 拖拽柄 + 方框勾选 + 文字，悬停右侧出现 提醒 / 删除。"""

    toggled_sub = Signal(dict, bool)
    deleted_sub = Signal(int)
    subClicked = Signal(dict)               # 点标题：复习条目是跳转，别的是改名
    moved = Signal(int, int, bool)      # (被拖的 id, 落到哪一行, 落在它后面吗)

    def __init__(self, sub: dict, parent=None):
        super().__init__(parent)
        self.sub = sub
        self._press = None
        self._drop_hint = ""
        self._cancelling = False
        self.setObjectName("SubRow")
        self.setFixedHeight(38)
        self.setAcceptDrops(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        grip = TickIcon("grip", 12, "border_strong")
        lay.addWidget(grip)
        self.check = PrioCheckBox(bool(sub["done"]), 17)
        self.check.toggled.connect(
            lambda ck, sd=sub: self.toggled_sub.emit(sd, ck))
        lay.addWidget(self.check)
        self.title = ClickableLabel(sub["title"])
        self.title.setObjectName("SubTaskTitle")
        widgets._apply_property(self.title, "done",
                                "true" if sub["done"] else "false")
        self.title.clicked.connect(self._title_clicked)
        lay.addWidget(self.title, 1)
        self.edit = QLineEdit(sub["title"])
        self.edit.setObjectName("SubRename")
        self.edit.hide()
        self.edit.installEventFilter(self)
        self.edit.editingFinished.connect(self._finish_rename)
        lay.addWidget(self.edit, 1)
        self.tools = QWidget()
        tl = QHBoxLayout(self.tools)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(2)
        del_btn = QPushButton()
        del_btn.setObjectName("ToolBtn")
        del_btn.setFixedSize(22, 22)
        del_btn.setCursor(Qt.PointingHandCursor)
        TickIcon("trash", 12, "muted", del_btn).move(5, 5)
        del_btn.clicked.connect(lambda: self.deleted_sub.emit(self.sub["id"]))
        tl.addWidget(del_btn)
        self.tools.setVisible(False)
        lay.addWidget(self.tools)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.tools.setVisible(True)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.tools.setVisible(False)

    # ---- 拖动排序 ----
    # 原来那枚 ⋮⋮ 只是画上去的，按下去什么也不会发生。
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press is None or not (event.buttons() & Qt.LeftButton):
            return
        if (event.position().toPoint() - self._press).manhattanLength() < 8:
            return
        start = self._press
        self._press = None
        mime = QMimeData()
        mime.setData(_SUB_MIME, str(self.sub["id"]).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        # 直接 grab() 会把底色烤成调色板那一档，夜间模式下发白
        drag.setPixmap(widgets.drag_snapshot(self, "bg_alt"))
        drag.setHotSpot(start)
        drag.exec(Qt.MoveAction)

    def _above(self, y: float) -> bool:
        return y < self.height() / 2

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(_SUB_MIME):
            event.acceptProposedAction()
            self._show_drop(self._above(event.position().y()))

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(_SUB_MIME):
            event.acceptProposedAction()
            self._show_drop(self._above(event.position().y()))

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._show_drop(None)

    def dropEvent(self, event) -> None:  # noqa: N802
        raw = bytes(event.mimeData().data(_SUB_MIME) or b"").decode()
        self._show_drop(None)
        if not raw.isdigit() or int(raw) == self.sub["id"]:
            return
        above = self._above(event.position().y())
        event.acceptProposedAction()
        self.moved.emit(int(raw), self.sub["id"], not above)

    def _show_drop(self, above) -> None:
        val = "" if above is None else ("above" if above else "below")
        if self._drop_hint == val:
            return
        self._drop_hint = val
        widgets._apply_property(self, "drop", val)

    # ---- 改名 ----
    def _title_clicked(self, at: QPoint | None = None) -> None:
        """复习条目点标题 = 跳到刷题页那一题。

        给它改名没意义：标题是从题干拼出来的，改完下一次对账又被覆盖。
        """
        if services.review_kind_of_sub(self.sub["id"]):
            self.subClicked.emit(self.sub)
            return
        self._rename(at)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        if (obj is self.edit and ev.type() == QEvent.KeyPress
                and ev.key() == Qt.Key_Escape):
            self._cancel_rename()
            return True
        return super().eventFilter(obj, ev)

    def _rename(self, at: QPoint | None = None) -> None:
        g = self.title.mapToGlobal(at) if at is not None else None
        self.title.hide()
        self.edit.show()
        self.edit.setFocus()
        if g is None:
            self.edit.selectAll()
        else:
            _place_caret(self.edit, g)

    def _cancel_rename(self) -> None:
        """Esc 是「不改了」。原来 editingFinished 在 Esc 后照样落库，
        等于按了取消却存了盘。"""
        if not self.edit.isVisible():
            return
        # hide() 会先补发一次 editingFinished，靠 _cancelling 挡掉，
        # 而不是靠「把文本还原成原值所以不算改动」这种隐式约定
        self._cancelling = True
        self.edit.setText(self.sub["title"])
        self.edit.hide()
        self.title.show()
        self._cancelling = False

    def _finish_rename(self) -> None:
        if self._cancelling or not self.edit.isVisible():
            return
        text = self.edit.text().strip()
        self.edit.hide()
        self.title.show()
        if text and text != self.sub["title"]:
            self.sub["title"] = text
            self.title.setText(text)
            services.subtask_update(self.sub["id"], title=text)


class _EmptyArt(QWidget):
    """空状态插画：浅灰圆底 + 文档线稿 + 蓝色鼠标箭头（滴答右栏空着时那个）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(170, 170)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtGui import QPolygonF
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = float(min(self.width(), self.height()))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.get("surface_hi")))
        p.drawEllipse(QRectF(0, 0, s, s))
        # 文档
        p.setBrush(QColor(theme.get("bg_alt")))
        doc = QRectF(s * .30, s * .24, s * .30, s * .42)
        p.drawRoundedRect(doc, 3, 3)
        p.setBrush(QColor(theme.get("border_strong")))
        for i, y in enumerate((.32, .40, .48)):
            w = s * (.20 if i < 2 else .12)
            p.drawRoundedRect(QRectF(s * .35, s * y, w, s * .035), 2, 2)
        # 折角
        p.setBrush(QColor(theme.get("border")))
        p.drawPolygon(QPolygonF([QPointF(s * .60, s * .24), QPointF(s * .60, s * .33),
                                 QPointF(s * .51, s * .24)]))
        # 鼠标箭头（滴答用品牌蓝）
        p.setBrush(QColor(theme.get("accent")))
        p.drawPolygon(QPolygonF([
            QPointF(s * .50, s * .46), QPointF(s * .50, s * .74),
            QPointF(s * .565, s * .675), QPointF(s * .615, s * .775),
            QPointF(s * .655, s * .755), QPointF(s * .605, s * .655),
            QPointF(s * .685, s * .645)]))

class CheckinRow(QFrame):
    """今日打卡行：滴答把习惯内嵌在任务列表底部的样子。

    点勾选框 = 就地打卡；点行的其它地方 = 右栏出这个习惯的详情（滴答就是这么分的，
    整行都拿去打卡就没法看月历和日志了）。"""

    toggled = Signal(int, str)     # (habit_id, date)
    opened = Signal(int)           # 请求在右栏打开这个习惯的详情

    def __init__(self, habit: dict, done: bool, streak: int, card: bool = False):
        super().__init__()
        self.setObjectName("TodoRow")
        # 看板视图里打卡行也是卡片：列与列之间没有分隔线，靠边框把自己分出来
        widgets._apply_property(self, "card", "true" if card else "false")
        self._habit_id = habit["id"]
        self._group_key = "checkin"      # 拖任务时这里不是落点，靠它判断
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 8, 0)
        lay.setSpacing(8)

        self.check = PrioCheckBox(done, 19)
        self.check.toggled.connect(
            lambda ck, h=habit["id"]: self.toggled.emit(h, _today_str()))
        lay.addWidget(self.check)

        from .habits import EmojiBadge
        lay.addWidget(EmojiBadge(habit["icon"], habit.get("color", "green"), 20))
        name = QLabel(habit["name"])
        name.setObjectName("TodoTitle")
        self.name_lbl = name
        lay.addWidget(name, 1)
        # 连续天数标签常驻（0 的时候藏起来）：这样打卡只改一行就够了，
        # 不用为了「多出一个标签」把整个列表重建
        self.streak_lbl = QLabel("")
        self.streak_lbl.setObjectName("TodoDate")
        lay.addWidget(self.streak_lbl)
        self.streak_icon = TickIcon("habit", 13, "muted")
        lay.addWidget(self.streak_icon)
        self.set_state(done, streak)
        day = QLabel("今天")
        day.setObjectName("TodoDate")
        widgets._apply_property(day, "dateState", "today")
        lay.addWidget(day)

    def set_state(self, done: bool, streak: int) -> None:
        self.check.set_checked(done)          # set_checked 不发 toggled，不会回灌
        widgets._apply_property(self.name_lbl, "done", "true" if done else "false")
        self.streak_lbl.setText(f"{streak} 天")
        self.streak_lbl.setVisible(bool(streak))
        self.streak_icon.setVisible(bool(streak))

    def set_selected(self, on: bool) -> None:
        widgets._apply_property(self, "selected", "true" if on else "false")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 按下就吃事件、并在这里开详情。一是行里的名字是 QLabel，它 ignore
        # 鼠标事件，按下会冒到列表容器上被「点空白收起详情」判成点了空白，
        # 收起补间和刚打开的抽屉打架；二是动作放「松手」的话，勾选框翻完状态
        # 后事件继续冒上来，一行会被翻两次 = 看着没反应。
        if event.button() == Qt.LeftButton:
            event.accept()
            self.opened.emit(self._habit_id)
        else:
            super().mousePressEvent(event)


class RightColumn(QFrame):
    """右栏：任务详情 / 习惯详情二选一。

    分割器只认一个第 3 栏，所以两个面板共用这一个容器，靠显隐切换
    （同一时刻只有一个可见，布局就把整块给它）。
    本来用 QStackedWidget 更顺，但实测 `page.grab()` 会整个跳过它的子树
    —— 单独 grab 这个容器有内容、抓父窗口时那块是纯白，截图和走查全都
    看不见右栏。所以退回普通 QFrame + 布局。
    宽度约束（min / max / sizeHint）统一挂在本容器上。
    """

    def __init__(self, task_pane: QWidget, habit_pane: QWidget, parent=None):
        super().__init__(parent)
        self.setObjectName("RightColumn")
        self.task = task_pane
        self.habit = habit_pane
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(task_pane)
        lay.addWidget(habit_pane)
        habit_pane.hide()
        self._current = task_pane

    def current_pane(self) -> QWidget:
        return self._current

    def show_pane(self, which: QWidget) -> None:
        self._current = which
        self.task.setVisible(which is self.task)
        self.habit.setVisible(which is self.habit)
        self.updateGeometry()

    def set_floating(self, on: bool) -> None:
        self.task.set_floating(on)
        self.habit.close_btn.setVisible(on)
        self.setMinimumWidth(0 if on else DetailPane.MIN_W)
        self.setMaximumWidth(16777215 if on else DetailPane.MAX_W)
        self.updateGeometry()

    def sizeHint(self):
        return self._current.sizeHint()

    def minimumSizeHint(self):
        return self._current.minimumSizeHint()


class TodoPage(QWidget):
    """待办清单主页：导航 + 任务列表 + 详情三栏。"""

    openHabits = Signal()     # 请求切到「习惯」页（点今日打卡行的空白处）
    openReview = Signal(str, int)   # (kind, 题目 id) 点复习条目 → 跳到刷题页那道题
    focusRequested = Signal(str, str)   # (任务名, "pomodoro"|"countup") 右键「开始专注」

    def __init__(self):
        super().__init__()
        self._view = "today"
        self._search = ""
        self._sort = "custom"          # custom / priority / created / due
        self._group = "date"           # date / list / tag / priority / none
        self._selected_id = 0          # 当前开着详情的那条，行上要有常驻底色
        self._selected_habit = 0       # 右栏开的是习惯详情时，哪一行该亮
        self._checkin_rows_by_id: dict[int, CheckinRow] = {}
        self._items: dict[int, TodoRow] = {}
        self._headers: dict[str, GroupHeader] = {}
        self._groups: dict[str, list] = {}
        self._group_order = GROUP_ORDER
        # 已完成分组默认展开：勾完任务要能立刻看到它挪过去，而不是「消失了」
        self._collapsed: set[str] = set()
        self._open_folders: set[int] = set()
        self._nav_rows: dict[str, SideRow] = {}
        self._pending_date = ""
        self._pending_time = ""
        self._date_pinned = False      # 日期是从日历弹层手选的（不是文字里解析出来的）
        self._pending_repeat = ""
        self._pending_reminder = ""
        self._pending_list = ""
        self._pending_tags: list[str] = []
        self._pending_prio = 0
        self._pending_end = ""           # 持续时间段：结束日 / 结束时刻
        self._pending_end_time = ""
        self._date_popup: DatePickerPopup | None = None
        self._quick_more_popup: QuickMorePopup | None = None
        self._quick_tag_popup: TagPickPopup | None = None
        self._show_done_inline = True

        # 三栏用分割器：左右都能拖宽拖窄，拖到最边就整栏收起（滴答的做法）。
        # 中间列表设成不可收起 —— 不然一拖把任务列表拖没了，只剩两边。
        self._split = QSplitter(Qt.Horizontal, self)
        self._split.setObjectName("TodoSplit")
        self._split.setHandleWidth(6)
        self._split.setChildrenCollapsible(True)
        self._split.addWidget(self._build_nav())
        self._canvas = self._build_canvas()
        self._split.addWidget(self._canvas)
        self.detail = DetailPane()
        self.detail.taskSaved.connect(self._on_task_saved)
        self.detail.taskDeleted.connect(lambda _id: self.reload())
        self.detail.tagsChanged.connect(self._on_tags_changed)
        self.detail.backRequested.connect(self._close_detail)
        self.detail.doneFlipped.connect(self._on_done_flipped)
        self.detail.subClicked.connect(self._open_sub_review)
        # 习惯详情直接复用习惯页那个面板（统计卡 + 月历 + 打卡日志），
        # 不另写一套，两边长相和口径才不会漂
        from .habits import HabitDetail
        self.habit_detail = HabitDetail()
        self.habit_detail.changed.connect(self.reload)
        self.habit_detail.closeRequested.connect(self._close_detail)
        self.habit_detail.moreRequested.connect(self._habit_more_menu)
        # 那个 ✕ 在习惯页的文案是「返回习惯列表」，在待办页它只是收起右栏
        self.habit_detail.close_btn.setToolTip("收起详情")
        self.right = RightColumn(self.detail, self.habit_detail)
        self._split.addWidget(self.right)
        self._split.setCollapsible(0, True)
        self._split.setCollapsible(1, False)
        self._split.setCollapsible(2, True)
        self._split.setStretchFactor(0, 0)
        self._split.setStretchFactor(1, 1)
        self._split.setStretchFactor(2, 0)
        self._split.splitterMoved.connect(self._on_split_moved)
        # 不给初始尺寸，分割器会按各栏 sizeHint 分，左栏会被压到下限 150
        self._split.setSizes([NAV_DEFAULT_W, 776, DetailPane.FULL_W])
        # 拖之前记住的宽度：收起后再展开要回到这个值
        self._pane_w = {0: NAV_DEFAULT_W, 2: DetailPane.FULL_W}
        self._overlay = False       # 详情栏现在是浮层还是分割器里的一栏

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._split)

        # Ctrl+F 搜索：作用域限在本页及子控件，不跟其它页的快捷键抢
        find = QShortcut(QKeySequence("Ctrl+F"), self)
        find.setContext(Qt.WidgetWithChildrenShortcut)
        find.activated.connect(self._focus_search)

        # 便签里改了字要回灌列表，否则桌面便签和待办列表两边看着不一样
        from .. import sticky
        sticky.on_change(lambda _tid: self.reload())

        self._rebuild_nav()
        self._restore_prefs()
        self._set_view(self._view)

    # ---- 视图偏好持久化 ----
    # 视图 / 分组 / 排序 / 折叠状态原来都是裸实例属性，每次重启都回到
    # 「今天 + 自定义排序 + 全展开」，用户排好的顺序全丢。
    def _restore_prefs(self) -> None:
        view = db.get_setting("todo_view") or "today"
        kind, _, ident = view.partition(":")
        if ident and not self._view_exists(kind, ident):
            view = "today"          # 清单 / 标签 / 过滤器可能已被删掉
        self._view = view
        self._sort = db.get_setting("todo_sort") or "custom"
        self._group = db.get_setting("todo_group") or "date"
        self._show_done_inline = db.get_setting("todo_done_inline") != "0"
        # ⋯ 菜单的显示偏好：视图（列表 / 看板）+ 三个行内显示开关，默认全关，
        # 关掉时行高和版式与改动前完全一致
        self._mode_pref = (db.get_setting("todo_view_mode") or "list")
        self._row_detail = db.get_setting("todo_row_detail") == "1"
        self._show_countdown = db.get_setting("todo_show_countdown") == "1"
        # 行内检查事项默认就摊开（滴答的默认也是这样）：藏起来的话，
        # 一条任务有几个勾选项在列表里完全看不出来
        self._show_checks = db.get_setting("todo_show_checks", "1") == "1"
        self._collapsed = {k for k in db.get_setting("todo_collapsed").split(",") if k}
        self._open_folders = {int(x) for x in db.get_setting("todo_folders").split(",")
                              if x.isdigit()}
        self._want_sizes = [int(x) for x in db.get_setting("todo_split_sizes").split(",")
                            if x.strip().isdigit()]

    def showEvent(self, event) -> None:  # noqa: N802
        # 分割器要有真实宽度才设得进去，所以放到首帧布局之后
        super().showEvent(event)
        # 底部「今日打卡」行是缓存的界面：在习惯页打了卡再切回来，勾选不会自己
        # 跟上。用 services 的习惯版本号判过期，两边都靠它。
        # 必须放在下面那个早退之前 —— 首帧之后 _sizes_applied 一直是 True。
        seen = getattr(self, "_seen_habit_rev", None)
        rev = services.habit_rev()
        self._seen_habit_rev = rev
        if seen is not None and seen != rev:
            self.reload()
        # 同理，复习排期也是「在别的页面改的」：在八股/算法页改了下次复习日，
        # 切回清单必须看到那条子任务挪到新的一天。
        seen_rv = getattr(self, "_seen_review_rev", None)
        rev_rv = services.review_rev()
        self._seen_review_rev = rev_rv
        if seen_rv is not None and seen_rv != rev_rv:
            self.reload()
        if getattr(self, "_sizes_applied", False) or not self._want_sizes:
            return
        self._sizes_applied = True
        if len(self._want_sizes) == 3:
            QTimer.singleShot(0, lambda: self._split.setSizes(
                self._normalize(self._want_sizes)))
        # 布局跑完才知道各栏真实宽度，浮层 / 停靠要再判一次
        QTimer.singleShot(0, self._sync_detail_mode)
        if not getattr(self, "_sticky_restored", False):
            self._sticky_restored = True
            # 上次退出时开着的便签放回来（sticky=1 的那些任务）
            from .. import sticky
            QTimer.singleShot(400, sticky.restore_all)

    @staticmethod
    def _view_exists(kind: str, ident: str) -> bool:
        # 清单的导航 key 是 `list:{名字}`、文件夹是 `folder:{id}`，两种都得认。
        # 原来这里先 int(ident)，清单那一档永远抛 ValueError 直接 False，于是
        # 「上次开的是哪张清单」从来没被恢复过 —— 每次启动都回到「今天」。
        if kind in ("list", "folder"):
            rows = services.list_all(include_archived=True)
            return any(e["name"] == ident or str(e["id"]) == ident for e in rows)
        try:
            i = int(ident)
        except ValueError:
            return False
        if kind == "tag":
            return any(t["id"] == i for t in services.tag_all())
        if kind == "filter":
            return any(f["id"] == i for f in services.filter_all())
        return False

    def _save_prefs(self) -> None:
        db.set_setting("todo_view", self._view)
        db.set_setting("todo_sort", self._sort)
        db.set_setting("todo_group", self._group)
        db.set_setting("todo_done_inline", "1" if self._show_done_inline else "0")
        db.set_setting("todo_view_mode", self._mode_pref)
        db.set_setting("todo_row_detail", "1" if self._row_detail else "0")
        db.set_setting("todo_show_countdown", "1" if self._show_countdown else "0")
        db.set_setting("todo_show_checks", "1" if self._show_checks else "0")
        db.set_setting("todo_collapsed", ",".join(sorted(self._collapsed)))
        db.set_setting("todo_folders", ",".join(str(i) for i in sorted(self._open_folders)))
        sizes = [self._pane_real_w(i) for i in range(self._split.count())]
        # 浮层模式下分割器只有两栏，存下来重启会对不上号，直接跳过
        if not self._overlay and sum(sizes) > 0:
            db.set_setting("todo_split_sizes", ",".join(str(x) for x in sizes))

    # 中间列表还能正常读的最小宽度。加上左栏和详情默认宽度塞不进窗口时，
    # 详情栏改成「浮在右侧的抽屉」盖住列表右半边 —— 滴答就是这么做的。
    # （以前是整页化：点任务把列表整块换掉，只剩一行「点击任务标题查看详情」
    # 在那儿等着，用户明确说不要这样。）
    CANVAS_MIN_W = 420

    def _want_overlay(self) -> bool:
        if self._view == "quad":
            # 四象限是 2×2 整页版式，中栏独占宽度；详情从右侧滑出来盖住它
            return True
        # 首帧布局还没跑的时候左栏量出来是 0，会把「塞不下」误判成「塞得下」，
        # 所以量不到就退回记住的宽度
        nav = self._pane_real_w(0) or self._pane_w.get(0, NAV_DEFAULT_W)
        need = nav + self.CANVAS_MIN_W + DetailPane.FULL_W             + 2 * self._split.handleWidth()
        return need > self.width()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_detail_mode(from_resize=True)

    def _sync_detail_mode(self, from_resize: bool = False) -> None:
        """在「分割器里的一栏」和「浮在右侧的抽屉」之间切换。

        from_resize：这次是窗口尺寸变了引起的。窄到放不下三栏时详情只能盖在
        列表上，留着等于挡住用户正在看的东西，所以直接收起（点任务仍会开抽屉）。
        """
        overlay = self._want_overlay()
        if overlay == self._overlay:
            if overlay:
                self._place_overlay()
            return
        self._overlay = overlay
        self.right.set_floating(overlay)
        widgets._apply_property(self.right, "floating",
                                "true" if overlay else "false")
        if overlay:
            # QSplitter 没有 removeWidget，只能先摘掉父再挂回来
            self.right.setParent(None)
            self.right.setParent(self)
            # 没开着详情时别把抽屉 show 出来：它自带「点标题看详情」的空状态，
            # 盖在四象限这种整页版式上就像凭空多出一栏
            if self._detail_open() and from_resize:
                self._close_detail()
            elif self._detail_open():
                self.right.show()
                self.right.raise_()
            else:
                self.right.hide()
            self._place_overlay()
        else:
            self._split.insertWidget(2, self.right)
            self._split.setCollapsible(2, True)
            self._split.setStretchFactor(2, 0)
            want = (self._pane_w.get(2, DetailPane.FULL_W)
                    if self._detail_open() else 0)
            # 同 showEvent：分割器要先拿到新宽度，setSizes 才设得进去
            QTimer.singleShot(0, lambda: self._apply_sizes(2, want))

    def _overlay_geom(self) -> tuple[int, int]:
        w = min(self._pane_w.get(2, DetailPane.FULL_W),
                max(DetailPane.MIN_W, int(self.width() * 0.78)))
        return w, self.height()

    def _place_overlay(self) -> None:
        w, h = self._overlay_geom()
        self.right.setGeometry(self.width() - w, 0, w, h)
        self.right.raise_()

    def _slide_overlay(self, on: bool, on_end=None) -> None:
        w, h = self._overlay_geom()
        x1 = self.width() - w if on else self.width()
        x0 = self.right.x()
        if abs(x0 - x1) <= 1:
            self.right.setGeometry(x1, 0, w, h)
            if on_end is not None:
                on_end()
            return
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(200)
        anim.setEasingCurve(QEasingCurve.InOutCubic)
        anim.valueChanged.connect(
            lambda t: self.right.setGeometry(int(x0 + (x1 - x0) * t), 0, w, h))
        if on_end is not None:
            anim.finished.connect(on_end)
        self._overlay_anim = anim       # 保住引用，不然会被 GC 掉中断动画
        anim.start()

    # ==================================================================
    # 左栏
    # ==================================================================
    def _build_nav(self) -> QFrame:
        nav = QFrame()
        nav.setObjectName("TodoNav")
        # 固定宽会把整个窗口顶出一个缩不下去的下限；分割器里还要能拖宽，
        # 所以上限放到 360，默认 204
        nav.setMinimumWidth(150)
        nav.setMaximumWidth(NAV_MAX_W)
        self._nav = nav
        outer = QVBoxLayout(nav)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._nav_scroll = QScrollArea()
        self._nav_scroll.setObjectName("TodoNavScroll")
        self._nav_scroll.setWidgetResizable(True)
        self._nav_scroll.setFrameShape(QFrame.NoFrame)
        self._nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        self._nav_lay = QVBoxLayout(holder)
        self._nav_lay.setContentsMargins(8, 12, 8, 12)
        self._nav_lay.setSpacing(5)
        self._nav_lay.addStretch(1)
        self._nav_scroll.setWidget(holder)
        outer.addWidget(self._nav_scroll)
        return nav

    def _nav_insert(self, w: QWidget) -> None:
        """插到末尾 stretch 之前。"""
        self._nav_lay.insertWidget(self._nav_lay.count() - 1, w)

    def _section_header(self, text: str, on_add=None) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(12, 10, 8, 3)
        lay.setSpacing(4)
        lbl = QLabel(text)
        lbl.setObjectName("SideSection")
        lay.addWidget(lbl)
        lay.addStretch(1)
        if on_add:
            btn = QPushButton()
            btn.setObjectName("RowBtn")
            btn.setFixedSize(18, 18)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(f"新建{text}")
            TickIcon("plus", 11, "muted", btn).move(4, 4)
            btn.clicked.connect(on_add)
            lay.addWidget(btn)
        return w

    def _side_row(self, key: str, text: str, **kw) -> SideRow:
        row = SideRow(key, text, reorderable=bool(_nav_group(key)), **kw)
        # 只有 _nav_more 认得的 key 才有右键菜单，别给智能视图挂空按钮
        row.set_has_menu(key.startswith(("list:", "folder:", "tag:", "filter:"))
                         or key == "trash")
        row.picked.connect(self._set_view)
        row.dropped.connect(self._on_nav_drop)
        row.moreRequested.connect(self._nav_more)
        row.addRequested.connect(self._nav_add_into)
        row.expandToggled.connect(self._toggle_folder)
        row.moved.connect(self._on_nav_reorder)
        self._nav_rows[key] = row
        self._nav_insert(row)
        return row

    # ---- 左栏排序 ----
    def _nav_order(self, group: str, parent: str) -> list[str]:
        """这一串当前从上到下的 key。串与串之间互不相干。"""
        if group == "list":
            keys = []
            for e in services.list_all():
                kind, fid = e.get("kind"), int(e.get("folder_id") or 0)
                pid = "0" if kind == "folder" else str(fid)
                if pid == parent:
                    keys.append(f"folder:{e['id']}" if kind == "folder"
                                else f"list:{e['name']}")
            return keys
        if group == "tag":
            # 标签的顺序就存在 tags.sort_order 里，父级在前、子级跟在父级后面
            return [f"tag:{t['id']}" for t in services.tag_all()
                    if str(int(t.get("parent_id") or 0)) == parent]
        if group in ("smart", "foot"):
            keys = list(NAV_SMART if group == "smart" else NAV_FOOT)
        else:
            # 四格优先级是写死的（不在 todo_filters 表里），和自建过滤器排同一串
            keys = list(QUAD_KEYS) + [f"filter:{fl['id']}"
                                      for fl in services.filter_all()]
        raw = (db.get_setting(NAV_SETTINGS[group]) or "").split(",")
        order = [k for k in raw if k in keys]
        order += [k for k in keys if k not in order]   # 新建的排在末尾
        return order

    def _on_nav_reorder(self, src: str, dst: str, after: bool) -> None:
        """拖完把这一串重排后写回去：清单 / 标签落 sort_order，其余落 settings。"""
        grp, par = _nav_group(src), _nav_parent(src)
        if not grp or src == dst or grp != _nav_group(dst) or par != _nav_parent(dst):
            return
        order = self._nav_order(grp, par)
        if src not in order or dst not in order:
            return
        order.remove(src)
        order.insert(order.index(dst) + (1 if after else 0), src)
        if grp == "list":
            # 导航 key 里清单用名字、文件夹用 id，写 sort_order 要的是 id
            key2id = {}
            for e in services.list_all():
                key2id[f"folder:{e['id']}" if e.get("kind") == "folder"
                       else f"list:{e['name']}"] = e["id"]
            services.list_reorder([key2id[k] for k in order if k in key2id])
        elif grp == "tag":
            services.tag_reorder([int(k[4:]) for k in order])
        else:
            db.set_setting(NAV_SETTINGS[grp], ",".join(order))
        # 拖完重建要推迟一帧：现在还在 QDrag.exec() 的栈里，当场把发起拖拽的那行
        # 删掉是 Qt 明确说过会崩的（源控件在 exec 返回前被销毁）。
        QTimer.singleShot(0, self._rebuild_nav)

    def _smart_counts(self) -> dict:
        """左栏角标。

        今天 / 最近7天 会展开重复任务，所以角标也不能数系列本身，
        要数区间内摊出来的未完成周期，否则角标和列表行数对不上。
        """
        todos = services.todo_list()
        hidden = services.smart_hidden_lists()
        today = QDate.currentDate()
        today_s = today.toString("yyyy-MM-dd")
        in7_s = today.addDays(7).toString("yyyy-MM-dd")
        plain = lambda t: (not _settled(t)   # noqa: E731
                           and not (t.get("repeat") or "").strip()
                           and (t.get("list_name") or "收集箱") not in hidden)
        spans = {"today": (today_s, today_s), "soon7": (today_s, in7_s)}
        bounds = {"today": lambda d: d <= today_s, "soon7": lambda d: d <= in7_s}
        # 放弃的重复任务：列表里那行会被归到已完成，角标却还在数周期，
        # 两边口径要一致
        gone = {t["id"] for t in todos if int(t.get("abandoned") or 0)}
        out: dict[str, int] = {}
        for view, (start, end) in spans.items():
            n = sum(1 for t in todos
                    if plain(t) and t["due_date"] and bounds[view](t["due_date"]))
            n += sum(1 for o in services.cal_occurrences(start, end)
                     if o["repeat"] and not o["done"] and o["id"] not in gone
                     and (o["list_name"] or "收集箱") not in hidden)
            out[view] = n
        out["inbox"] = sum(1 for t in todos if not _settled(t)
                           and (t.get("list_name") or "收集箱") == "收集箱")
        # 「已完成」那一栏本来就收放弃的，角标也得一起算，不然点进去数字对不上
        out["done"] = sum(1 for t in todos if _settled(t))
        out["trash"] = len(services.trash_list())
        return out

    def _rebuild_nav(self) -> None:
        """整栏重建。清单/标签/过滤器会随数据变，逐条 diff 不划算。"""
        while self._nav_lay.count() > 1:
            it = self._nav_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                widgets.drop_widget(w)
        self._nav_rows.clear()

        todos = services.todo_list()
        sc = self._smart_counts()
        smart = {"soon7": dict(text="最近7天", icon_kind="week", count=sc["soon7"]),
                 "today": dict(text="今天", icon_kind="today", count=sc["today"]),
                 # 滴答把这个入口放在左侧图标栏，我们沿用文字导航，就排在「今天」
                 # 下面。四格全铺时中栏要占满宽度，详情走抽屉（见 _want_overlay）。
                 "quad": dict(text="四象限", icon_kind="quad",
                              icon_hex="#3d8bff", icon_key="accent"),
                 "inbox": dict(text="收集箱", icon_kind="inbox",
                               count=sc["inbox"])}
        for key in self._nav_order("smart", "0"):
            self._side_row(key, **smart[key])
        # 滴答这两个日历图标格子里分别写着今天的日期号和星期两字母缩写（9/19 周六
        # 就是「19」和「Sa」），是这两个入口最主要的辨识点。格子要容得下两位数字，
        # 所以比其它导航图标略大一号。
        _today = QDate.currentDate()
        for _k, _g in (("today", str(_today.day())),
                       ("soon7", _WEEK_ABBR[_today.dayOfWeek() - 1])):
            _row = self._nav_rows[_k]
            _row.icon.setFixedSize(17, 17)
            _row.icon.set_glyph(_g)

        # ---- 清单（含文件夹）----
        self._nav_insert(self._section_header("清单", self._add_list))
        entries = services.list_all()
        # 按 list_all 的顺序一路排下来（文件夹和顶层清单是同一串，能互相拖），
        # 文件夹后面紧跟它的子清单。原来文件夹永远整块排在顶层清单前面。
        for e in entries:
            if e.get("kind") == "folder":
                kids = [x for x in entries
                        if x.get("kind") != "folder"
                        and x.get("folder_id") == e["id"]]
                opened = e["id"] in self._open_folders
                fcount = sum(1 for t in todos if not _settled(t)
                             and t.get("list_name") in {k["name"] for k in kids})
                self._side_row(f"folder:{e['id']}", e["name"],
                               icon_kind="folder_open"
                               if opened else "folder",
                               dot_hex=e.get("color") or "",
                               expandable=True, expanded=opened, count=fcount,
                               show_add=True)
                if opened:
                    for k in kids:
                        self._list_row(k, todos, indent=18)
            elif not e.get("folder_id"):
                self._list_row(e, todos, indent=0)
        # 归档掉的清单默认不列出来，原来就没有任何入口能把它取回来 ——
        # 归档等于永久删除。这里补一行，点开逐个取消归档。
        arch = [e for e in services.list_all(include_archived=True)
                if e.get("archived")]
        if arch:
            row = SideRow("archived_lists", "已归档清单", icon_kind="archive",
                          icon_key="muted", count=len(arch))
            # 这行没有右键菜单（_nav_more 不认这个 key），⋯ 挂着就是假按钮
            row.set_has_menu(False)
            row.picked.connect(lambda _k: self._archived_lists_menu(row))
            self._nav_rows["archived_lists"] = row
            self._nav_insert(row)

        # ---- 标签（层级）----
        self._nav_insert(self._section_header("标签", self._add_tag))
        tags = services.tag_all()
        for t in tags:
            if not t.get("parent_id"):
                self._tag_row(t, tags, todos, indent=0)
                for c in tags:
                    if c.get("parent_id") == t["id"]:
                        self._tag_row(c, tags, todos, indent=18)
        if not tags:
            self._nav_insert(self._hint_row("还没有标签"))

        # ---- 过滤器 ----
        self._nav_insert(self._section_header("过滤器", self._add_filter))
        quads = {k: (name, color, square) for k, name, color, square in QUADRANTS}
        fls = {f"filter:{fl['id']}": fl for fl in services.filter_all()}
        # 四格优先级和自建过滤器是同一串，顺序可以互相拖着换
        for key in self._nav_order("filter", "0"):
            if key in quads:
                name, color, square = quads[key]
                self._side_row(key, name, icon_kind="quad", icon_hex=square,
                               tint=color,
                               count=sum(1 for t in todos if not _settled(t)
                                         and t["priority"] == int(key[1:])))
            elif key in fls:
                fl = fls[key]
                self._side_row(key, fl["name"], icon_kind="filter",
                               count=self._filter_count(fl, todos))
        for key in self._nav_order("foot", "0"):
            self._side_row(key, "已完成" if key == "done" else "垃圾桶",
                           icon_kind="done" if key == "done" else "trash")

    def _list_row(self, lst: dict, todos: list, indent: int) -> None:
        count = sum(1 for t in todos if not _settled(t)
                    and t.get("list_name") == lst["name"])
        # 颜色只落到右边那枚小圆点上（滴答就是这个意思）：图标本身一律中性灰，
        # 否则同一张清单会出现「图标一个色、圆点一个色」两种说法
        self._side_row(f"list:{lst['name']}", lst["name"],
                       icon_kind=lst.get("icon") or "list",
                       dot_hex=lst.get("color") or "",
                       count=count, indent=indent, show_add=True)

    def _tag_row(self, tag: dict, all_tags: list, todos: list, indent: int) -> None:
        ids = {tag["id"]} | {c["id"] for c in all_tags
                             if c.get("parent_id") == tag["id"]}
        linked = set()
        for tid in ids:
            linked.update(services.tag_todo_ids(tid))
        self._side_row(f"tag:{tag['id']}", tag["name"], icon_kind="tag",
                       icon_hex=tag.get("hex") or color_hex(tag.get("color", "blue")),
                       dot_hex=tag.get("hex") or color_hex(tag.get("color", "blue")),
                       indent=indent)

    def _hint_row(self, text: str) -> QWidget:
        lbl = QLabel(text)
        lbl.setObjectName("EmptyHint")
        lbl.setContentsMargins(12, 2, 8, 4)
        return lbl

    # ==================================================================
    # 中栏
    # ==================================================================
    def _build_canvas(self) -> QFrame:
        canvas = QFrame()
        canvas.setObjectName("TodoCanvas")
        lay = QVBoxLayout(canvas)
        lay.setContentsMargins(20, 12, 12, 12)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.nav_toggle = QPushButton()
        self.nav_toggle.setObjectName("ToolBtn")
        self.nav_toggle.setFixedSize(28, 28)
        self.nav_toggle.setCursor(Qt.PointingHandCursor)
        self.nav_toggle.setToolTip("收起 / 展开侧栏")
        TickIcon("menu", 16, "muted", self.nav_toggle).move(6, 6)
        self.nav_toggle.clicked.connect(self._toggle_nav)
        head.addWidget(self.nav_toggle)
        # 清单 / 文件夹视图在标题前显示它自己的图标（滴答就是这么标的）
        self.title_icon = TickIcon("list", 19, "muted")
        self.title_icon.setVisible(False)
        head.addWidget(self.title_icon)
        self.view_title = QLabel("今天")
        self.view_title.setObjectName("ViewTitle")
        head.addWidget(self.view_title)
        head.addStretch(1)

        self.search_btn = QPushButton()
        self.search_btn.setObjectName("ToolBtn")
        self.search_btn.setFixedSize(28, 28)
        self.search_btn.setCursor(Qt.PointingHandCursor)
        self.search_btn.setToolTip("搜索 (Ctrl+F)")
        TickIcon("search", 15, "muted", self.search_btn).move(7, 7)
        self.search_btn.clicked.connect(self._toggle_search)
        head.addWidget(self.search_btn)

        self.sort_btn = QPushButton()
        self.sort_btn.setObjectName("ToolBtn")
        self.sort_btn.setFixedSize(28, 28)
        self.sort_btn.setCursor(Qt.PointingHandCursor)
        self.sort_btn.setToolTip("排序")
        TickIcon("sort", 15, "muted", self.sort_btn).move(7, 7)
        self.sort_btn.clicked.connect(self._open_sort_menu)
        head.addWidget(self.sort_btn)

        self.more_btn = QPushButton()
        self.more_btn.setObjectName("ToolBtn")
        self.more_btn.setFixedSize(28, 28)
        self.more_btn.setCursor(Qt.PointingHandCursor)
        self.more_btn.setToolTip("视图 / 显示选项 / 打印")
        TickIcon("more", 15, "muted", self.more_btn).move(7, 7)
        self.more_btn.clicked.connect(self._open_view_menu)
        head.addWidget(self.more_btn)
        lay.addLayout(head)

        # 搜索框默认收着，点放大镜才出现 —— 滴答顶栏上并没有一个常驻搜索框
        self.search_row = QWidget()
        sr = QHBoxLayout(self.search_row)
        sr.setContentsMargins(0, 0, 0, 0)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("TodoSearch")
        self.search_input.setPlaceholderText("搜索任务标题或描述")
        self.search_input.textChanged.connect(self._on_search)
        self.search_input.returnPressed.connect(self._search_enter)
        self.search_input.installEventFilter(self)
        sr.addWidget(self.search_input, 1)
        self.search_row.hide()
        lay.addWidget(self.search_row)

        quick = QFrame()
        quick.setObjectName("QuickAdd")
        # 焦点事件过滤器会立刻用到这个引用，得在建框的时候就给，不能等函数末尾
        self._quick_frame = quick
        q = QHBoxLayout(quick)
        q.setContentsMargins(6, 3, 8, 3)
        q.setSpacing(4)
        self._quick_icon = TickIcon("plus", 15, "muted")
        q.addWidget(self._quick_icon)
        self.quick_input = HighlightLineEdit()
        self.quick_input.setObjectName("QuickAddInput")
        self.quick_input.returnPressed.connect(self._quick_add)
        self.quick_input.textChanged.connect(self._on_quick_changed)
        self.quick_input.installEventFilter(self)
        q.addWidget(self.quick_input, 1)
        self.quick_chip = QPushButton()
        self.quick_chip.setObjectName("DateChip")
        self.quick_chip.setCursor(Qt.PointingHandCursor)
        self.quick_chip.clicked.connect(self._open_date_popup)
        self._chip_icon = TickIcon("calendar", 14, "accent", self.quick_chip)
        self._chip_icon.show()
        self.quick_chip.hide()
        q.addWidget(self.quick_chip)
        # 旗子是优先级 / 清单 / 标签三个值的入口。原来这三个只能靠标题里写
        # ``!`` / ``#清单`` / ``@标签``，打不出来就没有任何地方能设。
        self.quick_flag = FlagButton(0, 26, quick)
        self.quick_flag.setToolTip("优先级 / 清单 / 标签")
        self.quick_flag.clicked_.connect(self._open_quick_more)
        q.addWidget(self.quick_flag)
        lay.addWidget(quick)

        self.list_widget = TaskListArea()
        self.list_widget.dropRequested.connect(self._handle_drop)
        self.list_widget.emptyClicked.connect(self._close_detail)
        self.list_widget.keyMove.connect(self._on_key_move)
        self.list_widget.keyActivate.connect(self._on_key_activate)
        self.list_widget.keyDelete.connect(self._on_key_delete)
        self.list_widget.keyRename.connect(self._on_key_rename)
        self.list_widget.keyEscape.connect(
            lambda: self._close_detail(keep_cursor=True))
        lay.addWidget(self.list_widget, 1)

        # 看板：⋯ 菜单「视图」第二档。和中栏列表占同一个格子，按视图切换显隐
        self.board = BoardArea()
        self.board.emptyClicked.connect(self._close_detail)
        self.board.hide()
        lay.addWidget(self.board, 1)

        # 时间线（甘特）：清单对话框「视图」第三档
        self.timeline = TimelineArea()
        self.timeline.emptyClicked.connect(self._close_detail)
        self.timeline.barClicked.connect(self._on_timeline_bar)
        self.timeline.hide()
        lay.addWidget(self.timeline, 1)

        # 四象限：左栏那个 tab 的整页版式，同样占中栏这一格
        self.quad = QuadArea()
        self.quad.emptyClicked.connect(self._close_detail)
        self.quad.hide()
        lay.addWidget(self.quad, 1)

        # 看板列内 / 象限格内的拖拽重排：落点由容器算，这里换算成全局顺序再落库
        self.board.reorderDropped.connect(self._handle_column_drop)
        self.quad.reorderDropped.connect(self._handle_quad_drop)

        # 完成后浮在中间的撤销条：挂在 canvas 上跟着中栏走，不进布局（浮层）
        self._undo_bar = UndoBar(canvas)
        self._undo_bar.undone.connect(self._undo_last)

        # 顶栏那一行、快速添加条的留白、这几处四周的边距都落在 canvas 本体上
        # （QLabel / QFrame 不吃按下事件，会冒到父控件），所以只过滤 canvas
        # 就能覆盖中栏除条目以外的全部区域。
        canvas.installEventFilter(self)
        return canvas

    def _current_list(self) -> dict | None:
        """当前视图对应的那张清单，不在清单里返回 None。

        导航 key 用的是 `list:{名字}`（历史如此，重命名不用换 key），
        而表里的主键是 id —— 之前两处都直接 `int(key[5:])`，一定抛 ValueError
        然后退回全局那档，于是清单对话框里选的「看板 / 时间线」从来没生效过。
        """
        if not self._view.startswith("list:"):
            return None
        want = self._view[5:]
        for e in services.list_all(include_archived=True):
            if e["name"] == want or str(e["id"]) == want:
                return e
        return None

    @property
    def _view_mode(self) -> str:
        """当前该用哪个视图。

        滴答把「列表 / 看板 / 时间线」存在清单自己身上（清单对话框里那排「视图」），
        所以进了某个清单就以它自己的 view_kind 为准；智能清单 / 文件夹 / 标签 /
        过滤器没有这个字段，才用 ⋯ 菜单存的全局那档。
        """
        lst = self._current_list()
        if lst is not None:
            vk = lst.get("view_kind") or ""
            if vk in ("kanban", "timeline"):
                return "board" if vk == "kanban" else vk
            if vk == "list":
                return "list"
        return self._mode_pref

    @property
    def _area(self) -> QWidget:
        """当前装条目的那块：列表视图是中栏列表，看板视图是看板。

        reload 只往这里加控件，两个视图共用同一条渲染路径。
        """
        if self._view == "quad":
            return self.quad
        mode = self._view_mode
        if mode == "board":
            return self.board
        if mode == "timeline":
            return self.timeline
        return self.list_widget

    def _toggle_nav(self) -> None:
        # 收起时 Qt 只把宽度压到 0，isVisible() 依旧为 True，所以判据用尺寸
        self._set_nav_visible(self._pane_real_w(0) <= 0)

    def _toggle_search(self) -> None:
        if self.search_row.isVisible():
            self.search_input.clear()
            self.search_row.hide()
        else:
            self._focus_search()

    def _search_enter(self) -> None:
        """搜索框回车 = 走到第一条命中并打开详情。

        以前回车挂的是 _toggle_search，也就是「收起并清空」—— 打了半天字按回车，
        字没了、框也没了，等于搜索根本没有「执行」这个动作。收起留给 Esc。
        """
        self._selected_id = 0          # 让 _on_key_move 从「没选中」起步 -> 第一条
        self._on_key_move(1)
        self.list_widget.setFocus(Qt.OtherFocusReason)
        self._on_key_activate("enter")

    def _focus_search(self) -> None:
        self.search_row.show()
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _sync_title_icon(self, key: str) -> None:
        """清单 / 文件夹视图：标题前挂上该清单自己的图标和颜色。"""
        if key.startswith("list:") or key.startswith("folder:"):
            want = key.split(":", 1)[1]
            for lst in services.list_all():
                if str(lst["id"]) == want or lst["name"] == want:
                    self.title_icon.set_kind(
                        "folder_open" if lst.get("kind") == "folder"
                        else (lst.get("icon") or "list"))
                    self.title_icon.set_color_key("muted")
                    self.title_icon.setVisible(True)
                    return
        self.title_icon.setVisible(False)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """焦点驱动 QuickAdd 的高亮边框（QSS 没有 :focus-within）；
        外加两个输入框的 Esc：搜索框收回去，快速添加清空退回列表。

        搜索框比 quick_input 先建好，这期间事件过滤器就会被叫到，所以两个
        属性都得先取一次再用，不能直接 self.xxx。
        """
        quick = getattr(self, "quick_input", None)
        if event.type() == QEvent.Resize and obj is getattr(self, "_canvas", None):
            # 中栏一改变宽，撤销条要重新居中（它是浮层，不在布局里）
            bar = getattr(self, "_undo_bar", None)
            if bar is not None:
                bar.place()
        if (event.type() == QEvent.MouseButtonPress
                and event.button() == Qt.LeftButton
                and (obj is getattr(self, "_canvas", None) or obj is quick)):
            # 快速添加条上那行占位文字就是输入框本体，不单独算一次的话，
            # 点它仍然收不起抽屉 —— 用户圈的正是这一整条。
            self._close_detail()
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            if obj is getattr(self, "search_input", None):
                self._toggle_search()
                self.list_widget.setFocus(Qt.OtherFocusReason)
                return True
            if obj is quick:
                self._reset_quick_input()
                self.list_widget.setFocus(Qt.OtherFocusReason)
                return True
        if obj is quick and event.type() in (QEvent.FocusIn, QEvent.FocusOut):
            widgets._apply_property(self._quick_frame, "focused",
                                    "true" if event.type() == QEvent.FocusIn else "false")
        return super().eventFilter(obj, event)

    def _reset_quick_input(self) -> None:
        self.quick_input.clear()        # textChanged 会把日期 chip 一起清掉
        self._pending_repeat = ""
        self._pending_reminder = ""
        self._pending_prio = 0
        self._pending_list = ""
        self._pending_tags = []
        self._pending_end = ""
        self._pending_end_time = ""
        self._sync_quick_flag()
        self._update_chip()

    # ==================================================================
    # 视图切换
    # ==================================================================
    def _set_view(self, key: str) -> None:
        self._view = key
        self._save_prefs()
        for k, row in self._nav_rows.items():
            row.set_checked(k == key)
        name = self._view_name(key)
        self.view_title.setText(name)
        self._sync_title_icon(key)
        self.quick_input.setPlaceholderText(self._quick_hint(key))
        self.reload()
        # 四象限要整宽（详情改成抽屉），而抽屉 / 停靠是按窗口宽度算的：
        # 不在这儿补一次，切进来那一帧详情还占着第三栏，右边那一列直接被裁掉
        QTimer.singleShot(0, self._sync_detail_mode)

    def _quick_hint(self, key: str) -> str:
        """占位文案要说清「这条到底落到哪」。原来不管在哪一页都写
        「添加“今天”的任务至“收集箱”」，在清单页 / 标签页里是假的。"""
        target = self._default_list_for_view()
        if key in ("today", "soon7"):
            return f'添加“今天”的任务至“{target}”'
        if key.startswith("tag:"):
            return f'添加任务并打上“{self._view_name(key)}”'
        if key in ("p3", "p2", "p1", "p0"):
            return f'添加任务并设为“{self._view_name(key)}”'
        if key == "quad":
            return '添加任务（不设优先级会落在“不重要不紧急”那一格）'
        return f'添加任务至“{target}”'

    def _view_name(self, key: str) -> str:
        fixed = {"today": "今天", "soon7": "最近7天", "quad": "四象限",
                 "inbox": "收集箱", "done": "已完成", "archived": "已归档",
                 "trash": "垃圾桶"}
        if key in fixed:
            return fixed[key]
        for k, name, _tint, _sq in QUADRANTS:
            if k == key:
                return name
        if key.startswith("list:"):
            return key[5:]
        if key.startswith("folder:"):
            for f in services.list_all():
                if f"folder:{f['id']}" == key:
                    return f["name"]
        if key.startswith("tag:"):
            for t in services.tag_all():
                if str(t["id"]) == key[4:]:
                    return t["name"]
        if key.startswith("filter:"):
            for fl in services.filter_all():
                if str(fl["id"]) == key[7:]:
                    return fl["name"]
        return key

    def _toggle_folder(self, key: str) -> None:
        fid = int(key.split(":")[1])
        if fid in self._open_folders:
            self._open_folders.discard(fid)
        else:
            self._open_folders.add(fid)
        self._rebuild_nav()
        self._set_view(self._view)

    # ==================================================================
    # 数据收集
    # ==================================================================
    def _folder_lists(self, view: str) -> list:
        """文件夹里的子清单名，顺序和左栏一致。"""
        try:
            fid = int(view[7:])
        except ValueError:
            return []
        return [l["name"] for l in services.list_all()
                if l.get("kind", "list") != "folder"
                and l.get("folder_id") == fid]

    def _group_of(self, t: dict, today: str, in7: str) -> str:
        due = t["due_date"]
        if not due:
            return "nodate"
        if due < today:
            # 跨天的还没走完就不算过期，留在「今天」这一组
            return "today" if (t.get("end_date") or "") > today else "overdue"
        if due == today:
            return "today"
        if due <= in7:
            return "soon"
        return "later"

    def _match_search(self, t: dict) -> bool:
        return not self._search or self._search.lower() in (
            (t["title"] + " " + (t["note"] or "")).lower())

    def _filter_count(self, fl: dict, todos: list) -> int:
        return sum(1 for t in todos
                   if not _settled(t) and self._match_filter(t, fl["cond"]))

    def _match_filter(self, t: dict, c: dict) -> bool:
        lists = c.get("lists") or []
        if lists and (t.get("list_name") or "收集箱") not in lists:
            return False
        tags = c.get("tags") or []
        if tags:
            names = {x["name"] for x in services.todo_tags(t["id"])}
            if not names & set(tags):
                return False
        prio = c.get("priority") or []
        if prio and str(t["priority"]) not in prio:
            return False
        kw = (c.get("keyword") or "").strip().lower()
        if kw and kw not in (t["title"] + " " + (t["note"] or "")).lower():
            return False
        kind = c.get("type") or "all"
        if kind != "all" and (t.get("kind") or "task") != kind:
            return False
        date = c.get("date") or "all"
        if date != "all":
            today = QDate.currentDate().toString("yyyy-MM-dd")
            in7 = QDate.currentDate().addDays(7).toString("yyyy-MM-dd")
            due = t["due_date"]
            if date == "none" and due:
                return False
            if date == "overdue" and not (due and due < today):
                return False
            if date == "today" and due != today:
                return False
            if date == "next7" and not (due and due <= in7):
                return False
        return True

    # 滴答只在「今天 / 最近7天」这类日期驱动的智能清单里把重复任务摊成每个
    # 周期；清单 / 标签 / 过滤器视图显示系列本身（行尾的重复图标已经说明了）。
    EXPANDED_VIEWS = ("today", "soon7")

    def _expand_window(self, view: str) -> tuple[str, str]:
        today = QDate.currentDate()
        if view == "today":
            start = end = today
        else:
            start, end = today, today.addDays(7)
        return (start.toString("yyyy-MM-dd"), end.toString("yyyy-MM-dd"))

    def _occurrence_rows(self, start: str, end: str) -> list[dict]:
        """把区间内的重复周期摊平成与 todos 同形的行，供分组 / 渲染复用。"""
        base = {t["id"]: t for t in services.todo_list(include_archived=True)}
        rows = []
        for o in services.cal_occurrences(start, end, include_notes=True):
            if not o["repeat"]:
                continue            # 非重复任务仍由原始 todo 那条路径处理
            src = base.get(o["id"]) or {}
            # 以库里原始那条为底，只覆盖「展开后会变」的那几列。以前是逐列手抄，
            # 漏一列就少一块功能还查不出来：漏 pinned 时今天视图里置顶没反应，
            # 漏 abandoned 时放弃的周期行既画不出 ✕ 也不会归到已完成。
            row = dict(src)
            row.update({
                "id": o["id"], "occ": o["occ"],
                "title": o["title"], "note": o["note"],
                "priority": o["priority"], "list_name": o["list_name"],
                "due_date": o["date"], "due_time": o["time"],
                "done": int(o["done"]), "repeat": o["repeat"],
                "kind": o["kind"],
                "sub_done": o["sub_done"], "sub_total": o["sub_total"],
                "sort_order": o["sort"],
            })
            rows.append(row)
        return rows

    def _collect(self) -> tuple[dict, list]:
        todos = (services.trash_list() if self._view == "trash"
                 else services.todo_list(include_archived=True)
                 if self._view == "archived" else services.todo_list())
        today = QDate.currentDate().toString("yyyy-MM-dd")
        in7 = QDate.currentDate().addDays(7).toString("yyyy-MM-dd")
        buckets: dict[str, list] = {k: [] for k in GROUP_ORDER}
        flat: list = []
        view = self._view
        hidden = services.smart_hidden_lists()
        tag_ids = None
        if view.startswith("tag:"):
            tag_ids = set(services.tag_todo_ids(int(view[4:])))
            tag_ids |= {c["id"] for c in services.tag_all()
                        if c.get("parent_id") == int(view[4:])}
            linked = set()
            for tid in tag_ids:
                linked.update(services.tag_todo_ids(tid))
            tag_ids = linked
        cond = None
        if view.startswith("filter:"):
            cond = next((f["cond"] for f in services.filter_all()
                         if str(f["id"]) == view[7:]), {})

        expanded = view in self.EXPANDED_VIEWS
        for t in todos:
            if not self._match_search(t):
                continue
            if expanded and (t.get("repeat") or "").strip():
                continue            # 交给下面的周期展开，避免系列本身和周期同时出现
            if view == "trash":
                flat.append(t)
                continue
            if view == "archived":
                if t.get("archived"):
                    flat.append(t)
                continue
            if view == "done":
                # 「已完成」这一栏顺带收放弃的：两者都是「不再出现在待做里」，
                # 滴答也是把它们放在同一个视图里靠划线的样子区分
                if _settled(t):
                    flat.append(t)
                continue
            if _settled(t):
                if self._show_done_inline:
                    buckets["done"].append(t)
                continue
            if view in ("p3", "p2", "p1", "p0"):
                if t["priority"] == int(view[1]):
                    flat.append(t)
            elif view == "today":
                if t["due_date"] and t["due_date"] <= today \
                        and (t.get("list_name") or "收集箱") not in hidden:
                    buckets["overdue" if t["due_date"] < today else "today"].append(t)
            elif view == "soon7":
                if t["due_date"] and t["due_date"] <= in7 \
                        and (t.get("list_name") or "收集箱") not in hidden:
                    buckets[self._group_of(t, today, in7)].append(t)
            elif view.startswith("list:"):
                if t.get("list_name") == view[5:]:
                    buckets[self._group_of(t, today, in7)].append(t)
            elif view.startswith("folder:"):
                # 滴答在文件夹视图里是按「子清单」分组的，不是按日期
                if t.get("list_name") in self._folder_lists(view):
                    buckets.setdefault(
                        f'bylist:{t.get("list_name") or "收集箱"}',
                        []).append(t)
            elif view.startswith("tag:"):
                if t["id"] in tag_ids:
                    buckets[self._group_of(t, today, in7)].append(t)
            elif view.startswith("filter:"):
                if self._match_filter(t, cond or {}):
                    buckets[self._group_of(t, today, in7)].append(t)
            elif view == "inbox":
                if (t.get("list_name") or "收集箱") == "收集箱":
                    buckets[self._group_of(t, today, in7)].append(t)

        if expanded:
            start, end = self._expand_window(view)
            for t in self._occurrence_rows(start, end):
                if not self._match_search(t):
                    continue
                if (t.get("list_name") or "收集箱") in hidden:
                    continue
                if _settled(t):
                    if self._show_done_inline:
                        buckets["done"].append(t)
                else:
                    buckets[self._group_of(t, today, in7)].append(t)

        if view.startswith("folder:"):
            order = [f"bylist:{n}" for n in self._folder_lists(view)]
            if buckets.get("nodate"):
                order.append("nodate")
            order.append("done")
            self._group_order = order
        elif self._group != "date":
            # 「分组」不是按日期时，把上面按日期装好的桶倒出来重新按维度装一次。
            # 放在最后做，就不必在 view 的每个分支里各插一遍。
            self._regroup(buckets)
        else:
            self._group_order = GROUP_ORDER
        return buckets, flat

    def _regroup(self, buckets: dict) -> None:
        keep = {k: buckets.pop(k) for k in ("done", "checkin") if k in buckets}
        pool = [t for rows in buckets.values() for t in rows]
        buckets.clear()
        buckets.update(keep)
        order: list[str] = []
        for t in pool:
            key = self._bucket_key(t)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(t)
        if self._group == "priority":
            order.sort(key=lambda k: -int(k[5:] or 0))
        self._group_order = order + ([k for k in ("done",) if k in buckets])

    def _bucket_key(self, t: dict) -> str:
        if self._group == "none":
            return "flat"
        if self._group == "list":
            return "bylist:" + (t.get("list_name") or "收集箱")
        if self._group == "priority":
            return "prio:%d" % int(t.get("priority") or 0)
        if self._group == "tag":
            names = [g["name"] for g in services.todo_tags(t["id"])]
            return "bytag:" + (", ".join(names) if names else "")
        return "nodate"

    def _sort_items(self, items: list) -> list:
        # 置顶永远压在组头下面第一屏：sorted 是稳定的，所以只把置顶往前抬，
        # 组内原本的次序（自定义 / 按日期 / 按优先级）不受影响
        return sorted(self._sort_plain(items), key=lambda t: 0 if t.get("pinned") else 1)

    def _sort_plain(self, items: list) -> list:
        if self._sort == "priority":
            return sorted(items, key=lambda t: (
                -t["priority"], t["due_date"] or "9999-12-31", t["id"]))
        if self._sort == "created":
            return sorted(items, key=lambda t: (t.get("created_at") or "", t["id"]),
                          reverse=True)
        if self._sort == "due":
            return sorted(items, key=lambda t: (
                t["due_date"] or "9999-12-31", t.get("due_time") or "99:99", t["id"]))
        return sorted(items, key=lambda t: (t["sort_order"], t["id"]))

    # ==================================================================
    # 渲染
    # ==================================================================
    def reload(self) -> None:
        # 视图档和控件显隐要对齐：偏好是从库里读回来的，构造期不知道
        quad = self._view == "quad"
        mode = "" if quad else self._view_mode
        board = mode == "board"
        line = mode == "timeline"
        self.list_widget.setVisible(not (board or line or quad))
        self.board.setVisible(board)
        self.timeline.setVisible(line)
        self.quad.setVisible(quad)
        if quad:
            self._reload_quad()
            return
        keep_scroll = self._area.scroll_value()
        # reload 有多个提前 return 的分支，开关放在最前面才不会漏掉某一条
        self._area.set_reorder_enabled(self._sort == "custom")
        self._area.clear_items()
        self._items.clear()
        self._groups.clear()
        self._headers.clear()      # 不清的话每次 reload 都在字典里堆一批已销毁的标题

        buckets, flat = self._collect()
        flat = self._sort_items(flat)
        for k in buckets:
            buckets[k] = self._sort_items(buckets[k])
        view = self._view

        # 子任务进度要在建行之前的取数阶段带上
        review_ids = services.review_todo_ids()
        groups = list(buckets.values()) + [flat]
        subs_map = services.subtasks_by_ids(
            [t["id"] for lst in groups for t in lst])
        for lst in groups:
            for t in lst:
                subs = subs_map.get(t["id"], [])
                t["sub_total"] = len(subs)
                t["sub_done"] = sum(1 for s in subs if s["done"])
                if self._show_checks:
                    t["subs"] = subs     # 行内列出检查事项要用
                t["review_group"] = t["id"] in review_ids

        self._refresh_nav_counts()

        if view in ("p3", "p2", "p1", "p0", "done", "trash", "archived"):
            for t in flat:
                self._append_task(t, trash=(view == "trash"), group=view, card=board)
            if not flat:
                self._append_empty_hint(
                    "垃圾桶是空的" if view == "trash"
                    else "没有归档任务" if view == "archived" else "暂无任务")
            self._area.finalize_layout()
            self._area.set_scroll_value(keep_scroll)
            self._mark_selected()
            return

        # 今日打卡：滴答把它排在「今天」这一组后面
        checkin_rows = []
        # 每轮重建都要清空：这一轮没算打卡（比如标签 / 过滤器视图）时，
        # 留着上一轮的行控件会把已删除的对象再塞进布局
        self._done_checkins = []
        # 「打卡设置」那个开关只管两个智能清单；清单视图里始终显示
        in_smart = view in ("today", "soon7") and services.habit_checkin_in_smart()
        if in_smart or view.startswith("list:"):
            checkin_rows = self._checkin_rows()
        checkin_done = not checkin_rows

        def emit_checkin() -> None:
            header = GroupHeader("checkin", _group_label("checkin", checkin_rows),
                                 len(checkin_rows), "checkin" not in self._collapsed,
                                 action_text="管理")
            header.toggled.connect(self._on_group_toggle)
            header.action.connect(lambda _k: self.openHabits.emit())
            self._area.add_widget(header)
            self._headers["checkin"] = header
            self._groups["checkin"] = []
            for w in checkin_rows:
                self._area.add_widget(w)
                self._groups["checkin"].append(w)
                if "checkin" in self._collapsed:
                    w.setHidden(True)

        list_meta = {l["name"]: l for l in services.list_all()}
        for key in self._group_order:
            # 没有「今天」这一组时（清单 / 文件夹视图），打卡组排在已完成之前
            if key == "done" and not checkin_done:
                emit_checkin()
                checkin_done = True
            items = buckets.get(key) or []
            extra = self._done_checkins if key == "done" else []
            if not (items or extra):
                continue
            if key == "flat":          # 分组=无：不画组标题，直接铺列表
                for t in items:
                    self._append_task(t, group=key, card=board)
                continue
            expanded = key not in self._collapsed
            if key == "done" and not self._show_done_inline:
                continue
            action = "顺延" if key == "overdue" else (
                "" if expanded else ("查看更多" if key == "done" else ""))
            icon_kind = icon_hex = ""
            if key.startswith("bylist:"):
                lm = list_meta.get(key[7:]) or {}
                icon_kind = lm.get("icon") or "list"
                icon_hex = lm.get("color") or ""
            header = GroupHeader(key, _group_label(key, items),
                                 len(items) + len(extra),
                                 expanded, action, icon_kind=icon_kind,
                                 icon_hex=icon_hex)
            header.toggled.connect(self._on_group_toggle)
            header.action.connect(self._on_group_action)
            self._area.add_widget(header)
            self._headers[key] = header
            self._groups[key] = []
            for t in items:
                w = self._append_task(t, group=key, card=board)
                self._groups[key].append(w)
                if not expanded:
                    w.setHidden(True)
            for w in extra:
                # 打过卡的习惯：滴答也是把它从「今日打卡」挪到「已完成」里
                self._area.add_widget(w)
                self._groups[key].append(w)
                if not expanded:
                    w.setHidden(True)
            if key == "today" and not checkin_done:
                emit_checkin()
                checkin_done = True

        if not checkin_done:
            emit_checkin()

        self._area.finalize_layout()
        # 任务一条没有、但打卡行还在的时候不能再叠一个「点击 + 号添加任务」，
        # 那等于同时说「这里没东西」和「这里有东西」
        if not self._items and not checkin_rows:
            self._append_empty_hint("点击 + 号添加任务" if view != "trash"
                                    else "垃圾桶是空的")
        self._area.set_scroll_value(keep_scroll)
        self._mark_selected()

    def _on_quad_drop(self, todo_id: int, prio: int) -> None:
        """拖到别的象限 = 改优先级；顺手把行上的勾选框描边换成那一档的颜色。"""
        t = services.todo_get(todo_id)
        if not t or int(t.get("priority") or 0) == prio:
            return
        services.todo_update(todo_id, priority=prio)
        self.reload()

    def _reload_quad(self) -> None:
        """四象限页：优先级定格子，格子里再按日期分组。

        取数直接借 `_collect` —— 把 self._view 临时换成对应的 p* 过滤器跑一遍，
        搜索、隐藏清单、重复展开这些口径就和中栏列表完全一致，不用抄第二份。
        """
        keep_scroll = self.quad.scroll_value()
        # 象限分支在主 reload 里提前 return，够不到那句统一的开关，自己补上；
        # 放在 clear_items 之前，新建的格子才会从 QuadArea 身上继承到这个状态
        self.quad.set_reorder_enabled(self._sort == "custom")
        self.quad.clear_items()
        self._items.clear()
        self._groups.clear()
        self._headers.clear()
        today = QDate.currentDate().toString("yyyy-MM-dd")
        in7 = QDate.currentDate().addDays(7).toString("yyyy-MM-dd")
        saved = self._view
        try:
            for i, (key, name, _tint, _sq) in enumerate(QUADRANTS):
                self._view = key
                buckets, flat = self._collect()
                groups: dict[str, list] = {}
                for t in flat:
                    groups.setdefault(self._group_of(t, today, in7), []).append(t)
                for t in buckets.get("done", []):
                    if int(t.get("priority") or 0) == int(key[1:]):
                        groups.setdefault("done", []).append(t)
                numeral, tint = QUAD_PAGE[key]
                card = QuadCard(numeral, name, tint, prio=int(key[1:]))
                card.dropped.connect(self._on_quad_drop)
                self.quad.add_card(i, card)
                total = 0
                for gkey in GROUP_ORDER:
                    items = self._sort_items(groups.get(gkey) or [])
                    if not items:
                        continue
                    ck = f"{key}:{gkey}"
                    expanded = ck not in self._collapsed
                    header = GroupHeader(ck, _group_label(gkey, items),
                                         len(items), expanded)
                    header.toggled.connect(self._on_group_toggle)
                    card.add_widget(header)
                    self._headers[ck] = header
                    self._groups[ck] = []
                    for t in items:
                        w = self._append_task(t, group=ck, into=card)
                        self._groups[ck].append(w)
                        if not expanded:
                            w.setHidden(True)
                        total += 1
                if not total:
                    hint = QLabel("没有任务")
                    hint.setObjectName("QuadEmpty")
                    hint.setAlignment(Qt.AlignCenter)
                    card.add_widget(hint)
        finally:
            self._view = saved
        self.quad.finalize_layout()
        self._refresh_nav_counts()
        self.quad.set_scroll_value(keep_scroll)
        self._mark_selected()

    def _disp(self, card: bool = False) -> dict:
        """⋯ 菜单那三个行内开关，打包给 TodoRow / 看板卡片。"""
        return {"detail": self._row_detail, "countdown": self._show_countdown,
                "checks": self._show_checks, "card": card}

    def _append_task(self, t: dict, trash: bool = False,
                     group: str = "", card: bool = False,
                     into: QWidget | None = None) -> TodoRow:
        show_list = not self._view.startswith("list:") and self._view != "inbox"
        w = TodoRow(t, show_list=show_list, disp=self._disp(card=card))
        # 记住这一行属于哪个分组：拖放只允许组内重排，跨组拖动不生效
        w._group_key = group
        w.titleClicked.connect(
            lambda tid, row=w: self._show_detail(
                tid, row._occ,
                row.data.get("due_date", "") if row._occ else "",
                row.data.get("due_time", "") if row._occ else ""))
        w.changed.connect(self.reload)
        w.contextRequested.connect(self._row_menu)
        w.dragGroup.connect(self.list_widget.set_drag_group)
        w.subToggled.connect(self._on_row_sub_toggle)
        w.subClicked.connect(self._open_sub_review)
        w.doneToggled.connect(self._on_done_flipped)
        (into if into is not None else self._area).add_widget(w)
        self._items[t["id"]] = w
        return w

    def _on_row_sub_toggle(self, sub: dict, checked: bool) -> None:
        """行内勾选子任务：写库 + 只改这一条，不整页 reload（reload 会打断连续勾）。

        复习条目是例外：勾了要问结论，结论又会把它挪到别的日子（这一行连同
        计数都得重排），所以那一支走完整页重建 —— 但推到下一轮事件循环再做，
        正在发信号的控件就是这一行里的，当场删掉它会炸。
        """
        services.subtask_update(sub["id"], done=int(checked))
        sub["done"] = int(checked)
        if checked and services.review_kind_of_sub(sub["id"]):
            _review_ask_sub(self, sub)      # 答对/答错音在里面响，不再补完成音
            QTimer.singleShot(0, self.reload)
            return
        if checked:
            sounds.play("subtask_done")
        w = self._items.get(sub["todo_id"])
        if w is not None:
            total = int(w.data.get("sub_total", 0) or 0)
            w.data["sub_done"] = sum(
                1 for s in (w.data.get("subs") or []) if s["done"])
            # 「显示检查事项」开着时计数不显示，改了也没人看
            if total and not self._show_checks:
                for lbl in w.meta.findChildren(QLabel):
                    if "/" in lbl.text():
                        lbl.setText(f"{w.data['sub_done']}/{total}")
                        widgets._apply_property(
                            lbl, "subState",
                            "full" if w.data["sub_done"] == total else "")
                        break

    def _open_sub_review(self, sub: dict) -> None:
        """点复习条目那一行 → 跳到刷题页的那一道题。

        以前这个跳转挂在大任务那一行上（一题一行时行就是题）；现在一天一行，
        行本身点开是「这天要复习的题目」，跳转挪到了每一道题目上。
        """
        kind, item_id = services.review_owner_of_sub(sub["id"])
        if kind:
            self.openReview.emit(kind, item_id)

    def _append_empty_hint(self, text: str) -> None:
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 60, 0, 0)
        lay.setSpacing(10)
        art = TickIcon("empty_box", 72, "border_strong")
        art.setFixedHeight(72)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(art)
        row.addStretch(1)
        lay.addLayout(row)
        hint = QLabel(text)
        hint.setObjectName("EmptyHint")
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)
        self._area.add_widget(holder)

    def _checkin_rows(self) -> list:
        from ..services import (habit_checks_map, habit_due_on, habit_goal_count,
                                habit_is_done, habit_list)
        out = []
        board = self._view_mode == "board"
        self._checkin_rows_by_id.clear()   # 每次重建都换一批行，旧引用不能留
        today = _today_str()
        # 搜索时习惯也要跟着过滤：原来搜「论文」任务一条没有，底下却还挂着
        # 十行打卡，同时又叠一个「点击 + 号添加任务」的空状态
        kw = (self._search or "").strip().lower()
        for h in habit_list():
            if kw and kw not in h["name"].lower():
                continue
            if not habit_due_on(h, QDate.currentDate()):
                continue
            checks = habit_checks_map(h["id"])
            done = habit_is_done(h, checks, today)
            streak = sum(1 for rec in checks.values()
                         if rec["count"] >= habit_goal_count(h))
            w = CheckinRow(h, done, streak, card=board)
            w.toggled.connect(self._on_checkin)
            w.opened.connect(self._show_habit)
            self._checkin_rows_by_id[h["id"]] = w
            (self._done_checkins if done else out).append(w)
        return out

    def _on_done_flipped(self, todo_id: int, occ: str, checked: bool) -> None:
        """勾上就浮撤销条；反向操作时如果条上正是这条，顺手收掉。"""
        cur = self._undo_bar.payload()
        if not checked:
            if cur and cur[0] == "todo" and cur[1] == todo_id:
                self._undo_bar.hide()
            return
        if services.review_kind_of_todo(todo_id):
            # 复习大任务不浮撤销条：那天题目是按「答对/没答上来」逐条记进排期的，
            # 撤销只能把这条待办退回未勾，回收不了十几笔复习结论 —— 
            # 浮一个点了什么也不会退回来的条，不如不浮。
            if cur and cur[0] == "todo" and cur[1] == todo_id:
                self._undo_bar.hide()
            return
        t = services.todo_get(todo_id) or {}
        self._undo_bar.show_for(("todo", todo_id, occ), t.get("title") or "")

    def _on_habit_checkin(self, habit: dict, date: str, checked: bool) -> None:
        cur = self._undo_bar.payload()
        if not checked:
            if cur and cur[0] == "habit" and cur[1] == habit["id"]:
                self._undo_bar.hide()
            return
        self._undo_bar.show_for(("habit", habit["id"], date), habit["name"])

    def _undo_last(self, payload) -> None:
        kind = payload[0]
        if kind == "todo":
            _, tid, occ = payload
            services.occ_set_done(tid, occ, False)
        elif kind == "del":
            _, tid, occ = payload
            t = services.todo_get(tid) or {}
            if t.get("deleted_at"):
                services.todo_restore(tid)              # 从垃圾桶捞回来
            elif occ:
                services.occ_set_done(tid, occ, False)  # 取消「跳过这一周期」
        else:
            _, hid, date_s = payload
            services.habit_set_count(hid, date_s, 0)
        QTimer.singleShot(0, self.reload)

    def _on_checkin(self, habit_id: int, date: str) -> None:
        from ..services import (habit_checks_map, habit_goal_count, habit_get,
                                habit_is_done, habit_set_count)
        h = habit_get(habit_id)
        if not h:
            return
        cur = habit_checks_map(habit_id).get(date)
        target = 0 if cur and cur["count"] >= habit_goal_count(h) \
            else habit_goal_count(h)
        habit_set_count(habit_id, date, target)
        if target:
            sounds.play("habit_checkin")
        self._on_habit_checkin(h, date, bool(target))
        # 只改这一行。整表 reload 会在鼠标事件还开着的时候把正在收事件的行
        # 销毁掉，表现就是「点勾选框弹一堆窗口、勾反而没生效」，
        # 而且每勾一下都要重建全部行 —— 白付一次全表刷新。
        row = self._checkin_rows_by_id.get(habit_id)
        if row is not None:
            checks = habit_checks_map(habit_id)
            streak = sum(1 for rec in checks.values()
                         if rec["count"] >= habit_goal_count(h))
            row.set_state(habit_is_done(h, checks, date), streak)
        shown = getattr(self.habit_detail, "_habit", None)
        if shown and shown["id"] == habit_id:
            self.habit_detail.reload()
        # 打过卡的那行要搬去「已完成」组，必须重排一次。推迟一帧：现在还在
        # 这行的鼠标事件里，当场重建会把正在收事件的行销毁（老 bug）。
        QTimer.singleShot(0, self.reload)

    def _refresh_nav_counts(self) -> None:
        todos = services.todo_list()
        counts = self._smart_counts()
        for k, row in self._nav_rows.items():
            if k in counts:
                row.set_count(counts[k])
            elif k in ("p3", "p2", "p1", "p0"):
                row.set_count(sum(1 for t in todos if not _settled(t)
                                  and t["priority"] == int(k[1])))
            elif k.startswith("list:"):
                row.set_count(sum(1 for t in todos if not _settled(t)
                                  and t.get("list_name") == k[5:]))
            elif k.startswith("folder:"):
                names = {l["name"] for l in services.list_all()
                         if l.get("folder_id") == int(k[7:])}
                row.set_count(sum(1 for t in todos if not _settled(t)
                                  and t.get("list_name") in names))
            elif k.startswith("tag:"):
                ids = {int(k[4:])} | {c["id"] for c in services.tag_all()
                                      if c.get("parent_id") == int(k[4:])}
                linked = set()
                for tid in ids:
                    linked.update(services.tag_todo_ids(tid))
                row.set_count(sum(1 for t in todos if not _settled(t)
                                  and t["id"] in linked))
        for fl in services.filter_all():
            row = self._nav_rows.get(f"filter:{fl['id']}")
            if row is not None:
                row.set_count(self._filter_count(fl, todos))

    def _on_group_toggle(self, key: str, expanded: bool) -> None:
        if expanded:
            self._collapsed.discard(key)
        else:
            self._collapsed.add(key)
        self._save_prefs()
        for w in self._groups.get(key, []):
            w.setHidden(not expanded)
        self._area._container.layout().activate()
        self._area.finalize_layout()

    def _on_group_action(self, key: str) -> None:
        if key == "overdue":
            overdue = self._collect()[0].get("overdue", [])
            plain = [t["id"] for t in overdue if not (t.get("repeat") or "").strip()]
            services.todo_postpone(plain, 1)
            tomorrow = QDate.currentDate().addDays(1).toString("yyyy-MM-dd")
            for t in overdue:
                if (t.get("repeat") or "").strip():
                    # 系列平移：把该周期挪到明天，其后所有未完成周期跟着走
                    services.series_shift(t["id"], t.get("occ") or t["due_date"],
                                          tomorrow)
            self.reload()
        elif key == "done":
            self._collapsed.discard("done")
            self.reload()

    # ==================================================================
    # 交互
    # ==================================================================
    def _on_tags_changed(self) -> None:
        """改标签后只刷左栏角标；当前视图正按标签筛选、或列表正按标签分组时
        要重建，否则组标题还停在旧标签上。"""
        self._refresh_nav_counts()
        if self._view.startswith("tag:") or self._group == "tag":
            self.reload()

    # ---- 分割器：拖拽 / 拖到边收起 ----
    def _on_split_moved(self, pos: int, idx: int) -> None:
        for i in (0, 2):
            if self._split.widget(i) is None:
                continue        # 详情浮出去时分割器只剩两栏，别把它当成「拖到 0 收起」
            w = self._pane_real_w(i)
            if w > 0:
                self._pane_w[i] = w
            elif i == 2:
                # 详情被拖到 0 = 收起，选中态跟着清掉
                self._selected_id = 0
                self.detail.clear()
        self._save_prefs()

    def _pane_lim(self, i: int, v: int) -> int:
        """把某一栏的目标宽度夹进它自己的 min/max。0 表示整栏收起，
        分割器对 collapsible 的栏允许越过 minimumWidth。"""
        w = self._split.widget(i)
        if w is None or v <= 0:
            return 0
        return max(w.minimumWidth(), min(w.maximumWidth(), v))

    def _pane_real_w(self, i: int) -> int:
        """某一栏真正占掉的宽度。

        两个来源都得防一手：sizes() 在撞到 maximumWidth 后会虚高（控件
        只画到上限，多出来的那份变成幻影），而整栏收起时控件又还留着
        minimumWidth 那么宽、只是被挪到 x=-1。所以取两者的小值，0 优先。
        """
        w = self._split.widget(i)
        if w is None or not w.isVisible():
            return 0
        s = self._split.sizes()[i]
        if s <= 0:
            return 0
        return min(s, w.width()) if w.width() > 0 else s

    def _normalize(self, want: list[int]) -> list[int]:
        """把一栏目标宽度表夹进各栏的 min/max，剩下的余量全给中间列表。

        中间那栏是唯一没有上限的，所以让它吸收差额 —— 拖左栏或右栏时
        另一侧不该跟着变宽，否则两边互相顶，宽度永远收不拢。
        """
        n = self._split.count()
        mid = 1 if n > 1 else 0
        new = [self._pane_lim(i, want[i]) for i in range(n)]
        total = self._split.width()
        new[mid] = self._pane_lim(
            mid, max(0, total - sum(new[i] for i in range(n) if i != mid)))
        return new

    def _apply_sizes(self, idx: int, w: int) -> None:
        """精确设某一栏的宽度，其余栏保持原样。

        两个坑叠在一起量出来的（2026-09-21「中间一大块空白」）：
        - 基准必须用控件的真实宽度，不能用 sizes()。某栏撞到 maximumWidth
          （左栏 360）时，setSizes 仍会把超限那份记进 sizes，控件却只画到
          360，下一轮再按比例放大就滚出一条越滚越宽的幻影宽度。
        - 传进 setSizes 的每个值都得先夹到该栏的限内，否则分割器按幻影值
          排位置，把中间栏整体顶到右边，左边露出它自己的底色（白色空白条）。
        """
        if self._split.width() <= 0 or idx >= self._split.count():
            return
        n = self._split.count()
        want = [self._pane_real_w(i) for i in range(n)]
        want[idx] = w
        self._split.setSizes(self._normalize(want))

    def _tween_pane(self, idx: int, target: int, on_end=None) -> None:
        cur = self._pane_real_w(idx)
        if abs(cur - target) <= 1:
            self._apply_sizes(idx, target)
            if on_end is not None:
                on_end()
            self._save_prefs()
            return
        anim = QVariantAnimation(self)
        anim.setStartValue(cur)
        anim.setEndValue(target)
        anim.setDuration(200)
        anim.setEasingCurve(QEasingCurve.InOutCubic)
        anim.valueChanged.connect(lambda v: self._apply_sizes(idx, max(0, int(v))))
        if on_end is not None:
            anim.finished.connect(on_end)
        anim.finished.connect(self._save_prefs)   # 程序化改宽不发 splitterMoved
        self._split_anim = anim           # 保住引用，不然会被 GC 掉中断动画
        anim.start()

    def _set_nav_visible(self, on: bool) -> None:
        self._tween_pane(0, self._pane_w.get(0, NAV_DEFAULT_W) if on else 0)

    def _on_timeline_bar(self, row: "TodoRow") -> None:
        """点时间线上的横条 = 点列表里的这一行，开详情的上下文都一样。"""
        self._show_detail(row._todo_id, row._occ,
                          row.data.get("due_date", "") if row._occ else "",
                          row.data.get("due_time", "") if row._occ else "")

    def _mark_selected(self) -> None:
        """把选中底色落到「当前打开详情那一条」上。reload 会重建行，所以
        建完行也要再调一次。"""
        for tid, row in self._items.items():
            row.set_selected(tid == self._selected_id)
        self.timeline.selected_id = self._selected_id
        for lane in self.timeline._lanes:
            lane.update()
        for w in self._area._widgets:
            if w.__class__.__name__ == "CheckinRow":
                w.set_selected(w._habit_id == self._selected_habit)

    # ---- 键盘走查 ----
    def _ordered_rows(self) -> list[tuple[int, str]]:
        """当前列表里看得见的任务行，按显示顺序排成 (todo_id, occ)。

        不叫 _nav_rows —— 那是左栏导航行的字典，同名会被实例属性盖掉。"""
        return [(w._todo_id, getattr(w, "_occ", "") or "")
                for w in self._area._widgets
                if getattr(w, "_todo_id", None) is not None]

    def _selected_occ(self) -> str:
        for tid, occ in self._ordered_rows():
            if tid == self._selected_id:
                return occ
        return ""

    def _on_key_move(self, step: int) -> None:
        rows = self._ordered_rows()
        if not rows:
            return
        ids = [t for t, _o in rows]
        if self._selected_id in ids:
            i = max(0, min(len(rows) - 1, ids.index(self._selected_id) + step))
        else:
            i = 0 if step > 0 else len(rows) - 1
        self._selected_id = rows[i][0]
        self._mark_selected()
        row = self._items.get(self._selected_id)
        if row is not None:
            self.list_widget.ensureWidgetVisible(row, 0, 24)

    def _on_key_activate(self, kind: str) -> None:
        if not self._selected_id:
            return
        if kind == "space":
            row = self._items.get(self._selected_id)
            if row is not None:
                row.check.toggle()      # 走和鼠标完全同一条路径
        else:
            self._show_detail(self._selected_id, self._selected_occ())

    def _on_key_delete(self) -> None:
        if not self._selected_id:
            return
        self._delete_task(self._selected_id, self._selected_occ())

    def _on_key_rename(self) -> None:
        """F2 就地改名 —— 改名这件事以前只有鼠标能做（右键菜单或双击标题）。

        `at=None` 走的是键盘那支：标题整条预选好，直接敲新的就覆盖，
        和鼠标按出来时光标落在点击位置是两套，别混。
        """
        row = self._items.get(self._selected_id) if self._selected_id else None
        if row is not None:
            row._start_rename()

    def _show_detail(self, todo_id: int, occ: str = "", disp_date: str = "",
                     disp_time: str = "") -> None:
        # 复习大任务点开就是详情：这天要复习的题目都挂在下面的检查事项里，
        # 点某一道题才跳到刷题页那一题（跳转从「一行一题」挪到了子任务上）。
        task = services.todo_get(todo_id)
        if task:
            self._selected_id = todo_id
            self._mark_selected()
            self._selected_habit = 0
            self.right.show_pane(self.detail)
            self.detail.show_task(task, occ=occ, disp_date=disp_date,
                                  disp_time=disp_time)
            self._sync_detail_mode()      # 先定浮层/停靠，再展开
            self._expand_detail()
            self._mark_selected()

    def _show_habit(self, habit_id: int) -> None:
        """点今日打卡那一行：右栏出这个习惯的详情（统计卡 + 月历 + 打卡日志）。

        原来整行点下去就是打卡，习惯的月历和日志在待办页完全没有入口，
        要看得切到习惯页。现在点行出详情、点勾选框才是打卡。"""
        h = services.habit_get(habit_id)
        if not h:
            return
        self._selected_id = 0
        self._selected_habit = habit_id
        self.right.show_pane(self.habit_detail)
        self.habit_detail.show_habit(h)
        self._sync_detail_mode()
        self._expand_detail()
        self._mark_selected()

    def _habit_more_menu(self, habit: dict) -> None:
        """习惯详情右上角 ⋯。习惯页那份带「上移 / 下移」，那是习惯列表里的
        排序，在待办页没意义，所以这里只给编辑 / 归档 / 删除。"""
        archived = bool(habit.get("archived"))
        btn = self.habit_detail.more_btn

        def pick(v: object) -> None:
            v = str(v)
            if v == "edit":
                from .habits import HabitDialog
                if HabitDialog(habit, self).exec():
                    self.habit_detail.show_habit(services.habit_get(habit["id"]))
                    self.reload()
            elif v == "archive":
                services.habit_update(habit["id"],
                                      archived=0 if archived else 1)
                self.reload()
            elif v == "trash":
                services.habit_delete(habit["id"])
                self._close_detail()
                self.reload()
        self._menu_at([("edit", "edit", "编辑习惯"),
                       ("archive", "archive", "取消归档" if archived else "归档"),
                       ("trash", "trash", "删除习惯")],
                      btn.mapToGlobal(QPoint(0, btn.height())), pick,
                      danger=("trash",))

    def _close_detail(self, keep_cursor: bool = False) -> None:
        """点列表空白 / 返回：清空并收起右栏（滴答就是这一下关掉），带宽度补间。

        keep_cursor：按 Esc 收起详情时，高亮那一条要留在原地 —— 资源管理器
        关掉预览也不会取消选中，否则紧接着按 Delete 会没作用对象。
        """
        if not keep_cursor:
            self._selected_id = 0
            self._selected_habit = 0
            self._mark_selected()
        if not self.right.isVisible():
            return
        self.detail.clear()
        self.habit_detail.show_habit(None)
        self._collapse_detail()

    def _detail_open(self) -> bool:
        return bool(self._selected_id or self._selected_habit)

    def _expand_detail(self) -> None:
        if self._overlay:
            self.right.show()
            self.right.raise_()
            self._slide_overlay(True)
            return
        if not self.right.isVisible():
            self.right.show()
            self._apply_sizes(2, 0)
        self._tween_pane(2, self._pane_w.get(2, DetailPane.FULL_W))

    def _collapse_detail(self) -> None:
        if self._overlay:
            self._slide_overlay(False, on_end=self.right.hide)
            return
        self._tween_pane(2, 0, on_end=self.right.hide)

    def _delete_task(self, todo_id: int, occ: str = "") -> None:
        # 重复任务=跳过这一周期，普通任务=整条进垃圾桶（occ_delete 内部区分）
        services.occ_delete(todo_id, occ)
        sounds.play("todo_deleted")
        if self.detail._task and self.detail._task["id"] == todo_id:
            self.detail.clear()
        # 删除是这一页最容易手滑的动作，而且软删之后界面上什么都不剩 ——
        # 没有撤销条的话用户只能自己想到「去垃圾桶捞」
        t = services.todo_get(todo_id) or {}
        if t.get("deleted_at") or occ:
            self._undo_bar.show_for(("del", todo_id, occ),
                                    t.get("title") or "", verb="已删除")
        self.reload()

    def _on_task_saved(self, todo_id: int) -> None:
        self.reload()
        task = services.todo_get(todo_id)
        if task:
            self.detail.show_task(task)

    def _on_search(self, text: str) -> None:
        self._search = text.strip()
        self.reload()

    def _open_sort_menu(self) -> None:
        groups = [("date", "calendar", "按日期"), ("list", "list", "清单"),
                  ("tag", "tag", "标签"), ("priority", "flag", "优先级"),
                  ("none", "none", "无")]
        sorts = [("custom", "list", "自定义"), ("due", "calendar", "截止日期"),
                 ("created", "clock", "创建时间"), ("priority", "flag", "优先级")]
        # parent 不能省：没有父对象的 Qt.Popup 是个孤儿顶层窗口，拿不住鼠标抓取，
        # show 之后马上被 hide，而 hideEvent 里是 deleteLater —— 表现为菜单闪一下就没。
        # 这个应用里其它弹层都挂在触发按钮上，只有这里漏了。
        menu = SortGroupPopup([("分组", groups), ("排序", sorts)],
                              {"分组": self._group, "排序": self._sort},
                              self.sort_btn, width=190)

        def pick(which: str, value: object) -> None:
            value = str(value)
            if which == "group":
                self._group = value
                # 换分组维度时把重排锁回自定义，否则「优先级排序 + 优先级分组」
                # 会让每组只剩同一条排序键，看着像没生效
                if self._sort == "priority" and value == "priority":
                    self._sort = "custom"
            else:
                self._sort = value
            self._save_prefs()
            self.reload()
        menu.picked.connect(pick)
        menu.exec_at(self.sort_btn.mapToGlobal(QPoint(0, self.sort_btn.height())))

    def _open_view_menu(self) -> None:
        state = {"view": self._view_mode,
                 "hide_done": not self._show_done_inline,
                 "detail": self._row_detail, "countdown": self._show_countdown,
                 "checks": self._show_checks}
        menu = MoreMenuPopup(state, self.more_btn)
        # 切完视图面板不关（滴答也是不关），不连着 set_view 的话
        # 列表→看板之后，那一排还亮着「列表」
        menu.viewPicked.connect(menu.set_view)
        menu.viewPicked.connect(self._on_view_mode)
        menu.toggled.connect(self._on_display_toggle)
        menu.picked.connect(self._on_more_action)
        menu.printRequested.connect(self._open_print_menu)
        menu.exec_at(self.more_btn.mapToGlobal(QPoint(0, self.more_btn.height())))

    def _on_view_mode(self, mode: str) -> None:
        if self._view_mode == mode:
            return
        lst = self._current_list()
        if lst is not None and mode in ("list", "board", "timeline"):
            # 在某个清单里切视图 = 改这个清单自己的设置，下次进来还是它
            services.list_update(
                lst["id"], view_kind={"board": "kanban",
                                      "timeline": "timeline"}.get(mode, "list"))
        else:
            self._mode_pref = mode
        self._save_prefs()
        self.reload()

    def _on_display_toggle(self, key: str) -> None:
        attr = {"hide_done": "_show_done_inline", "detail": "_row_detail",
                "countdown": "_show_countdown", "checks": "_show_checks"}[key]
        if key == "hide_done":
            self._show_done_inline = not self._show_done_inline
        else:
            setattr(self, attr, not getattr(self, attr))
        self._save_prefs()
        self.reload()

    def _on_more_action(self, value: str) -> None:
        if value == "clear_done":
            done = [t for t in services.todo_list() if _settled(t)]
            # 以前点一下立刻把整批已完成塞进垃圾桶，一条不问、也不说动了几条
            if not done or not popups.confirm(
                    self, "清空已完成",
                    "把这 %d 条已完成的待办移入垃圾桶？" % len(done)):
                return
            for t in done:
                services.todo_delete(t["id"])
            self.reload()
        elif value == "archive":
            self._set_view("archived")

    def _open_print_menu(self) -> None:
        """滴答把打印做成二级菜单；父层已经收了，这里在同一位置再开一层。"""
        items = [("title", "printer", "打印标题"),
                 ("full", "printer", "打印标题和内容")]
        menu = TickMenu(items, self.more_btn)
        menu.picked.connect(lambda v: self._print_view(str(v)))
        menu.exec_at(self.more_btn.mapToGlobal(QPoint(0, self.more_btn.height())))

    def _print_view(self, mode: str) -> None:
        """把当前视图打成一张清单。滴答的「打印」就是这个：不带界面截图，
        只有分组标题 + 勾选框方框 + 任务，「标题和内容」再多一行描述和子任务。"""
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        from PySide6.QtGui import QTextDocument
        from PySide6.QtWidgets import QDialog

        doc = QTextDocument()
        doc.setHtml(self._print_html(mode))
        printer = QPrinter()
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle("打印待办")
        if dlg.exec() != QDialog.Accepted:
            return
        doc.print_(printer)

    def _print_html(self, mode: str) -> str:
        """打印正文（不含打印机相关）：单独拆出来是为了能在没有打印机的环境里验。"""
        from html import escape
        groups = self._print_model()
        total = sum(len(items) for _, items in groups)
        rows = []
        for title, items in groups:
            body = []
            for t in items:
                box = "☑" if t["done"] else "☐"
                line = '<div class="t">%s %s</div>' % (box, escape(t["title"]))
                if mode == "full":
                    note = _plain_preview(t.get("note") or "")
                    if note:
                        line += '<div class="n">%s</div>' % escape(note)
                    for s in services.subtask_list(t["id"]):
                        line += '<div class="s">%s %s</div>' % (
                            "☑" if s["done"] else "☐", escape(s["title"]))
                if t.get("due_date"):
                    txt, _state = _date_state(t["due_date"],
                                              t.get("due_time", ""),
                                              end=t.get("end_date") or "")
                    if txt:
                        line += '<div class="d">%s · %s</div>' % (
                            escape(txt), escape(t.get("list_name") or "收集箱"))
                body.append(line)
            rows.append('<h3>%s（%d）</h3>%s' % (escape(title), len(items),
                                                 "".join(body)))
        head = ('<h1>%s</h1><div class="meta">%s · 共 %d 项</div>'
                % (escape(self._view_name(self._view)),
                   QDate.currentDate().toString("yyyy年M月d日"), total))
        css = ("<style>body{font-family:'Microsoft YaHei UI';font-size:12pt;color:#202124}"
               "h1{font-size:18pt;margin:0}h3{font-size:12pt;margin:14pt 0 4pt;"
               "border-bottom:1px solid #ddd;padding-bottom:2pt}"
               ".meta{color:#888;font-size:10pt;margin-bottom:8pt}"
               ".t{margin:3pt 0}.n{color:#666;font-size:10pt;margin-left:16pt}"
               ".s{color:#444;font-size:10pt;margin-left:16pt}"
               ".d{color:#999;font-size:9pt;margin-left:16pt;margin-bottom:4pt}</style>")
        return css + head + "".join(rows)

    def _print_model(self) -> list[tuple[str, list[dict]]]:
        """打印用的 (分组标题, 任务) 列表 —— 和屏幕上看到的分组、排序一致。"""
        buckets, flat = self._collect()
        flat = self._sort_items(flat)
        for k in buckets:
            buckets[k] = self._sort_items(buckets[k])
        if self._view in ("p3", "p2", "p1", "p0", "done", "trash", "archived"):
            return [(self._view_name(self._view), flat)]
        out = []
        for key in self._group_order:
            items = buckets.get(key) or []
            if not items or key == "checkin":     # 打卡是习惯，不进待办清单
                continue
            if key == "done" and not self._show_done_inline:
                continue
            out.append((_group_label(key, items), items))
        return out

    def _row_menu(self, todo_id: int, pos) -> None:
        t = services.todo_get(todo_id)
        if not t:
            return
        row = self._items.get(todo_id)
        anchor = row if row is not None else self
        today = QDate.currentDate()
        occ = row._occ if row is not None else ""
        recurring = bool((t.get("repeat") or "").strip())
        if self._view == "trash":
            # 垃圾桶里只有两件事可做。原来给的是普通行的改日期 / 改优先级菜单，
            # 点「删除」也只是把已经删掉的东西再软删一次。
            def pick_trash(v: object) -> None:
                if v == "restore":
                    services.todo_restore(todo_id)
                elif popups.confirm(self, "彻底删除",
                                    "彻底删除「%s」？不留备份，找不回来。"
                                    % (t.get("title") or "")):
                    services.todo_delete(todo_id, hard=True)
                self.reload()
            self._menu_at([("restore", "restore", "恢复"),
                           ("purge", "trash", "彻底删除")],
                          anchor.mapToGlobal(QPoint(pos.x(), pos.y())),
                          pick_trash, danger=("purge",))
            return
        items = [
            (CAP, "日期"), (STRIP, _IconStrip(_date_strip_specs(
                t.get("due_date") or "", (t.get("repeat") or "").strip()))),
            (CAP, "优先级"), (STRIP, _IconStrip(_prio_strip_specs(
                int(t.get("priority") or 0)))),
            (SEP,),
            ("sub", "subtask_list", "添加子任务"),
            ("pin", "pin", "取消置顶" if t.get("pinned") else "置顶"),
            ("abandon", "abandon", "取消放弃" if t.get("abandoned") else "放弃"),
            ("move", "move_out", "移动到"),
            ("tags", "tag", "标签"),
            (SEP,),
            ("focus", "focus", "开始专注"),
            (SEP,),
            ("copy", "copy", "创建副本"),
            ("link", "link", "复制链接"),
            (SEP,),
            ("sticky", "note", "打开便签"),
            ("tonote", "floppy", "转换为笔记"),
            ("trash", "trash", "删除"),
        ]
        menu = TickMenu(items, anchor, danger=("trash",),
                        arrows=("move", "tags", "focus"))
        # 图标条在菜单里，点一格要走「选完就关」这条路（Popup 里点自己不会自动关）。
        # 反过来先接 menu.pick 再接 pick：先关菜单，避免二级弹层和它抢鼠标抓取。
        for strip in (menu.findChildren(_IconStrip)):
            strip.picked.connect(menu.pick)

        def pick(v: object) -> None:
            v = str(v)
            if v.startswith("d") and v[1:].isdigit():
                days = {"0": 0, "1": 1, "7": 7}[v[1:]]
                new_date = today.addDays(days).toString("yyyy-MM-dd")
                # 重复任务只挪这一个周期；普通任务 occ_move 会直接改截止日
                services.occ_move(todo_id, occ or t["due_date"], new_date)
            elif v == "pick":
                # 推到下一轮再开：现在 TickMenu 这个 Qt.Popup 还握着鼠标抓取，
                # 日历弹层是 Qt.Tool 窗，这时候 show 会被自己那条「没被激活就收起」
                # 的逻辑当场关掉（表现为点了日历那格什么也没发生）。
                QTimer.singleShot(0, lambda: self._pick_row_date(
                    todo_id, occ, t, anchor, pos))
                return
            elif v == "none":
                self._clear_due(todo_id, occ, recurring)
            elif v == "pin":
                services.todo_update(todo_id, pinned=0 if t.get("pinned") else 1)
            elif v.startswith("p") and v[1:].isdigit():
                services.todo_update(todo_id, priority=int(v[1:]))
            elif v == "copy":
                # 副本要连重复 / 提醒 / 时长 / 标签一起抄，原来只抄了标题四件套，
                # 复制一个每天重复的任务出来会变成一次性的
                new_id = services.todo_add(
                    t["title"], note=t.get("note") or "",
                    priority=t.get("priority") or 0,
                    due_date=t.get("due_date") or "",
                    due_time=t.get("due_time") or "",
                    list_name=t.get("list_name") or "收集箱",
                    repeat=t.get("repeat") or "",
                    reminder=t.get("reminder") or "",
                    duration_min=t.get("duration_min") or 0)
                for sub in services.subtask_list(todo_id):
                    sid = services.subtask_add(new_id, sub["title"])
                    if sub["done"]:
                        services.subtask_update(sid, done=1)
                tids = [g["id"] for g in services.todo_tags(todo_id)]
                if tids:
                    services.todo_set_tags(new_id, tids)
            elif v == "abandon":
                services.todo_update(todo_id,
                                     abandoned=0 if t.get("abandoned") else 1,
                                     done=0, completed_at="")
            elif v == "sticky":
                from .. import sticky
                sticky.open_note(todo_id)
                return
            elif v == "tonote":
                self._export_to_note(t)
                return
            elif v == "link":
                QApplication.clipboard().setText("lifeapp://todo/%d" % todo_id)
                return
            elif v == "move":
                self._open_move_menu(anchor, pos, t)
                return
            elif v == "tags":
                self._open_tags_menu(anchor, pos, todo_id)
                return
            elif v == "focus":
                self._open_focus_menu(anchor, pos, t)
                return
            elif v == "sub":
                self._show_detail(todo_id)
                self.detail._toggle_subs()
                return
            elif v == "trash":
                self._delete_task(todo_id, occ)
                return
            self.reload()
        menu.picked.connect(pick)
        menu.exec_at(anchor.mapToGlobal(QPoint(pos.x(), pos.y())))

    def _clear_due(self, todo_id: int, occ: str, recurring: bool) -> None:
        """「清除日期」：普通任务清掉截止日，重复系列是跳过这一周期。

        重复任务没有「这一周期不排日期」这种状态 —— 日期是规则算出来的，
        唯一等价的表达就是这一次不做。
        """
        if not _review_date_guard(self, todo_id):
            return
        if recurring and occ:
            services.occ_delete(todo_id, occ)
        else:
            services.todo_update(todo_id, due_date="", due_time="",
                                 end_date="", end_time="")
        self.reload()

    def _pick_row_date(self, todo_id: int, occ: str, t: dict, anchor, pos) -> None:
        """右键菜单里点「选择日期」那一格：开日历弹层，选完落库。

        重复任务只能改这一个周期（日历里那些日期是排期算出来的，没有存字段），
        所以跨天范围那一档对它没意义，只走 occ_move。
        """
        if not _review_date_guard(self, todo_id):
            return
        init = t.get("due_date") or QDate.currentDate().toString("yyyy-MM-dd")
        pop = DatePickerPopup(init, t.get("due_time") or "",
                              repeat=t.get("repeat") or "",
                              reminder=t.get("reminder") or "",
                              end_date=t.get("end_date") or "",
                              end_time=t.get("end_time") or "",
                              allow_range=not bool(occ))

        def on_ok(d: str, tm: str, end_d: str = "", end_t: str = "") -> None:
            if occ:
                services.occ_move(todo_id, occ, d)
            else:
                services.todo_update(todo_id, due_date=d, due_time=tm,
                                     end_date=end_d, end_time=end_t)
            self.reload()

        pop.accepted.connect(on_ok)
        # 弹层里那颗「清除」不能是死的：和菜单里「清除日期」那一格同一件事
        pop.cleared.connect(lambda: self._clear_due(todo_id, occ, bool(occ)))
        self._row_date_popup = pop        # 不给引用的话会被 Python 提前回收
        pop.adjustSize()
        pop.move(anchor.mapToGlobal(QPoint(pos.x(), pos.y())))
        pop.show()

    def _open_focus_menu(self, anchor, pos, t: dict) -> None:
        """开始专注 ›：两种计时 + 两个「预计」，滴答收在同一个二级里。

        二级菜单不带图标列（icon_kind 给 None），和滴答那张图一样是纯文字。
        """
        dur = int(t.get("duration_min") or 0)
        plan = int(t.get("pomo_plan") or 0)
        items = [
            ("pomodoro", None, "开始番茄专注"),
            ("countup", None, "开始正计时"),
            (SEP,),
            ("plan", None, ("预计番茄：%d 个" % plan) if plan else "预计番茄"),
            ("duration", None, ("时长：" + _human_duration(dur)) if dur else "时长"),
        ]
        menu = TickMenu(items, anchor, width=176)

        def pick(v: object) -> None:
            v = str(v)
            if v in ("pomodoro", "countup"):
                self.focusRequested.emit(t["title"], v)
            elif v == "plan":
                self._open_plan_menu(anchor, pos, t)
            elif v == "duration":
                self._open_duration_menu(anchor, pos, t)
        menu.picked.connect(pick)
        menu.exec_at(anchor.mapToGlobal(QPoint(pos.x(), pos.y())))

    def _open_duration_menu(self, anchor, pos, t: dict) -> None:
        """预计时长：和详情面板 ⋯ 里那一份同一套档位、同一个字段。"""
        cur = int(t.get("duration_min") or 0)
        items = [(m, None, _human_duration(m)) for m in DURATION_OPTIONS]
        items.append((0, None, "无时长"))
        menu = TickMenu(items, anchor, width=176,
                        checked=cur if cur in DURATION_OPTIONS else None)
        menu.picked.connect(lambda v: (
            services.todo_update(t["id"], duration_min=int(v or 0)), self.reload()))
        menu.exec_at(anchor.mapToGlobal(QPoint(pos.x(), pos.y())))

    def _open_plan_menu(self, anchor, pos, t: dict) -> None:
        """预计番茄：几个番茄能做完这条。滴答存在任务上，专注统计按它算完成率。"""
        cur = int(t.get("pomo_plan") or 0)
        items = [(n, None, "%d 个" % n) for n in range(1, 9)]
        items.append((0, None, "不设"))
        menu = TickMenu(items, anchor, width=176,
                        checked=cur if cur in range(1, 9) else None)
        menu.picked.connect(lambda v: (
            services.todo_update(t["id"], pomo_plan=int(v or 0)), self.reload()))
        menu.exec_at(anchor.mapToGlobal(QPoint(pos.x(), pos.y())))

    def _open_move_menu(self, anchor, pos, t: dict) -> None:
        """移动到 ›：换清单。滴答把「移动」和「改期」都放在右键菜单里。"""
        cur = t.get("list_name") or "收集箱"
        names = [x["name"] for x in services.list_all()
                 if x.get("kind") != "folder" and x["name"] != cur]
        items = [("收集箱", "inbox", "收集箱")] if cur != "收集箱" else []
        items += [(n, "list", n) for n in names]
        if not items:
            popups.notify(self, "没有别的清单", "先在左栏建一个清单，再回来移动。")
            return
        menu = TickMenu(items, anchor)
        menu.picked.connect(lambda v: (services.todo_update(t["id"], list_name=str(v)),
                                       self.reload()))
        menu.exec_at(anchor.mapToGlobal(QPoint(pos.x(), pos.y())))

    def _open_tags_menu(self, anchor, pos, todo_id: int) -> None:
        """标签 ›：勾选式，点一下挂上 / 摘掉，菜单不关，可以连着选。"""
        tags = services.tag_all()
        if not tags:
            popups.notify(self, "还没有标签", "在左栏「标签」旁边加一个，再回来挂。")
            return
        mine = {g["id"] for g in services.todo_tags(todo_id)}
        items = [("t%d" % g["id"], "tag", g["name"]) for g in tags]
        menu = TickMenu(items, anchor,
                        checked={"t%d" % i for i in mine})
        menu.picked.connect(lambda v: self._toggle_tag(todo_id, str(v)))
        menu.exec_at(anchor.mapToGlobal(QPoint(pos.x(), pos.y())))

    def _toggle_tag(self, todo_id: int, value: str) -> None:
        tid = int(value[1:])
        mine = {g["id"] for g in services.todo_tags(todo_id)}
        if tid in mine:
            mine.discard(tid)
        else:
            mine.add(tid)
        services.todo_set_tags(todo_id, sorted(mine))
        self.reload()

    def _export_to_note(self, t: dict) -> None:
        """转换为笔记 = 把这条任务写成 Obsidian 库里的一个 md 文件。

        和滴答不同：我们应用内没有笔记，笔记页是 Obsidian 的只读前端，所以「转换」
        只能是导出。**任务本身保留**（不删不标完成），用户不想要可以自己删。
        """
        import os
        from .. import vault
        if not vault.vault_path():
            popups.notify(self, "还没有选笔记库",
                          "在「笔记」页点「选择 Obsidian 库」，再回来转换。")
            return
        root = vault.export_root()
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as exc:
            popups.notify(self, "写不进去", "导出目录建不出来：%s" % exc)
            return
        safe = re.sub(r'[\/:*?"<>|#\^]', "", t["title"]).strip()[:40] or "无标题"
        path = os.path.join(root, "%s.md" % safe)
        n = 2
        while os.path.exists(path):
            path = os.path.join(root, "%s %d.md" % (safe, n))
            n += 1
        lines = ["# %s" % t["title"], ""]
        meta = [x for x in ((t.get("due_date") or ""),
                            (t.get("list_name") or "收集箱"),
                            ("重复" if (t.get("repeat") or "").strip() else "")) if x]
        if meta:
            lines += ["> " + " · ".join(meta), ""]
        lines += [(t.get("note") or "").strip(), ""]
        subs = services.subtask_list(t["id"])
        if subs:
            lines += ["## 检查事项"]
            lines += ["- [%s] %s" % ("x" if x["done"] else " ", x["title"]) for x in subs]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines).strip() + "\n")
        vault.clear_cache()
        popups.notify(self, "已导出为笔记", os.path.basename(path))

    def _handle_drop(self, tid: int, insert_idx: int) -> None:
        """列表内拖放：只在同一分组内、且「自定义排序」下重排。

        insert_idx 的语义是「插到第 insert_idx 个任务行之前」（等于行数=插到末尾），
        由 TaskListArea 用视口坐标算好后传进来，这里不再自己换算坐标 ——
        之前在这里重复算一遍，就是预览线和实际落点不一致的根源。
        """
        if self._sort != "custom":
            return
        rows = [w for w in self.list_widget._widgets
                if getattr(w, "_todo_id", None) is not None]
        src = next((i for i, w in enumerate(rows) if w._todo_id == tid), -1)
        if src < 0:
            return
        dst = max(0, min(int(insert_idx), len(rows)))
        anchor = rows[dst] if dst < len(rows) else rows[dst - 1]
        if getattr(rows[src], "_group_key", "") != getattr(anchor, "_group_key", ""):
            return
        ids = [w._todo_id for w in rows]
        ids.pop(src)
        new_idx = dst if dst < src else dst - 1
        if new_idx == src:
            return
        ids.insert(max(0, min(new_idx, len(ids))), tid)
        services.todo_reorder(ids)
        self.reload()

    # ---- 看板 / 四象限的列内重排 ----
    def _handle_column_drop(self, tid: int, anchor_tid, before: bool) -> None:
        """看板：整块看板的卡片顺序就是全局顺序，列只是它切出来的一段。"""
        if self._sort != "custom":
            return
        rows = [w for w in self.board._widgets
                if getattr(w, "_todo_id", None) is not None]
        self._splice_order(rows, tid, anchor_tid, before)

    def _handle_quad_drop(self, tid: int, anchor_tid, before: bool) -> None:
        """四象限：卡片是塞进各格自己的布局里的（QuadArea._widgets 是空的），
        所以按格的顺序把每格里的卡串起来当全局顺序。"""
        if self._sort != "custom":
            return
        rows = [c for card in self.quad._cards for c in card._cards()]
        self._splice_order(rows, tid, anchor_tid, before)

    def _splice_order(self, rows: list, tid: int, anchor_tid, before: bool) -> None:
        """把 tid 挪到 anchor 前/后，写回 sort_order。

        anchor_tid=None 表示落在本列末尾。落点等价于原顺序时直接返回 ——
        不然「拖一下又松回原地」会白写一遍库、白重建一次整页。
        """
        ids = [w._todo_id for w in rows]
        if tid not in ids:
            return
        src = rows[ids.index(tid)]
        anchor = next((w for w in rows if w._todo_id == anchor_tid), None) \
            if anchor_tid is not None else None
        # 和列表页同一条规矩：只在同一分组内重排。跨组「拖一下」看着像能改清单/
        # 改日期，实际那些语义各有出口（拖左栏、拖别的象限），这里不抢。
        if anchor is not None and getattr(src, "_group_key", None) is not None:
            if getattr(src, "_group_key", "") != getattr(anchor, "_group_key", ""):
                return
        rest = [i for i in ids if i != tid]
        if anchor_tid is None:
            pos = len(rest)
        else:
            if anchor_tid not in rest:
                return
            pos = rest.index(anchor_tid) + (0 if before else 1)
        new_ids = rest[:pos] + [tid] + rest[pos:]
        if new_ids == ids:
            return
        services.todo_reorder(new_ids)
        self.reload()

    def _on_nav_drop(self, key: str, tid: int) -> None:
        """拖到左栏：分优先级 / 改日期 / 标记完成 / 移入清单。"""
        row = self._items.get(tid)
        occ = (row._occ if row is not None else "") or _today_str()
        if key in ("p3", "p2", "p1", "p0"):
            services.todo_update(tid, priority=int(key[1]))
        elif key == "today":
            services.occ_move(tid, occ, _today_str())
        elif key == "soon7":
            services.occ_move(tid, occ, QDate.currentDate().addDays(6)
                              .toString("yyyy-MM-dd"))
        elif key == "done":
            services.todo_update(tid, done=1, completed_at=_now())
            # 拖到「已完成」也是一次勾选，复习大任务同样要问结论
            if not _review_ask_group(self, tid):
                services.todo_update(tid, done=0, completed_at="")
            elif not services.review_kind_of_todo(tid):
                sounds.play("todo_done")
        elif key == "inbox":
            services.todo_update(tid, list_name="收集箱")
        elif key.startswith("list:"):
            services.todo_update(tid, list_name=key[5:])
        elif key.startswith("tag:"):
            current = [t["id"] for t in services.todo_tags(tid)]
            tag_id = int(key[4:])
            if tag_id not in current:
                services.todo_set_tags(tid, current + [tag_id])
        self.reload()

    # ==================================================================
    # 快速添加
    # ==================================================================
    def _known_names(self) -> tuple[list, list]:
        return ([l["name"] for l in services.list_all()
                 if l.get("kind", "list") == "list"],
                [t["name"] for t in services.tag_all()])

    def _on_quick_changed(self, text: str) -> None:
        lists, tags = self._known_names()
        parsed = dateparse.parse_title(text, lists, tags)
        self.quick_input.set_spans(parsed.spans)
        if not text.strip():
            self._pending_date = self._pending_time = ""
            self._date_pinned = False
            # 清空输入框 = 一切退回默认，旗子上手选的优先级 / 清单 / 标签也一样
            self._pending_prio = 0
            self._pending_list = ""
            self._pending_tags = []
            self._pending_end = self._pending_end_time = ""
            self._sync_quick_flag()
        elif parsed.date or parsed.time:
            self._pending_date = parsed.date or self._pending_date
            self._pending_time = parsed.time
            # 文字里已经有日期词了，之前从弹层手选的那次就不作数了
            self._date_pinned = False
            if parsed.date:
                # 手打的日期是单点，跨天范围跟着一起作废
                self._pending_end = self._pending_end_time = ""
        elif not self._date_pinned:
            # 把「明天开会」退格成「开会」，日期 chip 得跟着没；
            # 但用户是从日历弹层手选的日期（_date_pinned）时不能清，
            # 否则选完日期再多打几个字，日期就莫名其妙消失了。
            self._pending_date = self._pending_time = ""
            self._pending_end = self._pending_end_time = ""
        # 这三项只在标题里真写了语法时才覆盖：手选的优先级 / 清单 / 标签
        # 要跟着这一条走到落库，不能因为用户又敲了两个字就没了
        if parsed.list_name:
            self._pending_list = parsed.list_name
        if parsed.tags:
            self._pending_tags = parsed.tags
        if parsed.priority:
            self._set_quick_prio(parsed.priority)
        self._update_chip()

    def _update_chip(self) -> None:
        if self._pending_date:
            end = self._pending_end
            e = QDate.fromString(end, "yyyy-MM-dd") if end else QDate()
            s = QDate.fromString(self._pending_date, "yyyy-MM-dd")
            if e.isValid() and e > s:
                # 跨天的不写「周六-9月30日」这种半截相对半截绝对，两端都写死日期
                parts = [f"{s.month()}月{s.day()}日-"
                         + (f"{e.year()}年" if e.year() != s.year() else "")
                         + f"{e.month()}月{e.day()}日"]
            else:
                parts = [dateparse.human_date(self._pending_date)]
            if self._pending_time:
                parts.append(dateparse.human_time(self._pending_time))
            self.quick_chip.setText(f"{', '.join(parts)}  ⌄")
            self.quick_chip.setStyleSheet("padding-left: 20px;")
            self._chip_icon.move(5, (self.quick_chip.height() - 14) // 2)
            self.quick_chip.show()
        else:
            self.quick_chip.hide()

    def _open_date_popup(self) -> None:
        init = self._pending_date or QDate.currentDate().toString("yyyy-MM-dd")
        pop = DatePickerPopup(init, self._pending_time,
                              repeat=self._pending_repeat,
                              reminder=self._pending_reminder,
                              end_date=self._pending_end,
                              end_time=self._pending_end_time,
                              allow_range=True)
        pop.accepted.connect(self._on_popup_accepted)
        pop.cleared.connect(self._on_popup_cleared)
        pop.repeatPicked.connect(lambda v: setattr(self, '_pending_repeat', v))
        pop.reminderPicked.connect(lambda v: setattr(self, '_pending_reminder', v))
        self._date_popup = pop
        pop.adjustSize()
        anchor = self.quick_input.mapToGlobal(
            self.quick_input.rect().bottomRight())
        pop.move(max(anchor.x() - pop.width() + 10, 8), anchor.y() + 6)
        pop.show()

    def _on_popup_accepted(self, date: str, time_v: str, end_date: str = "",
                           end_time: str = "") -> None:
        self._pending_date, self._pending_time = date, time_v
        self._pending_end, self._pending_end_time = end_date, end_time
        self._date_pinned = bool(date or time_v)
        self._update_chip()

    def _on_popup_cleared(self) -> None:
        self._pending_date = ""
        self._pending_time = ""
        self._pending_end = ""
        self._pending_end_time = ""
        self._date_pinned = False
        self._update_chip()

    # ---- 快速添加：优先级 / 清单 / 标签 ----
    def _sync_quick_flag(self) -> None:
        """旗子跟着待设的优先级上色，「这条设过了」才看得见。"""
        flag = getattr(self, "quick_flag", None)
        if flag is not None:
            flag.set_priority(self._pending_prio)

    def _quick_target_list(self) -> str:
        return self._pending_list or self._default_list_for_view()

    def _open_quick_more(self) -> None:
        pop = QuickMorePopup(self._pending_prio, self._quick_target_list(),
                             self._pending_tags, self)
        pop.prioPicked.connect(self._set_quick_prio)
        pop.listRequested.connect(self._quick_pick_list)
        pop.tagsRequested.connect(self._quick_pick_tags)
        self._quick_more_popup = pop
        popups.place_popup(pop, self.quick_flag)
        pop.show()

    def _set_quick_prio(self, value: int) -> None:
        self._pending_prio = int(value or 0)
        self._sync_quick_flag()

    def _quick_pick_list(self) -> None:
        cur = self._quick_target_list()
        names = [x["name"] for x in services.list_all()
                 if x.get("kind") != "folder"]
        if "收集箱" not in names:
            names.insert(0, "收集箱")
        items = [(n, "inbox" if n == "收集箱" else "list", n) for n in names]
        menu = TickMenu(items, self, checked={cur})
        menu.picked.connect(lambda v: setattr(self, "_pending_list", str(v)))
        popups.place_popup(menu, self.quick_flag)
        menu.show()

    def _quick_pick_tags(self) -> None:
        tags = services.tag_all()
        if not tags:
            popups.notify(self, "还没有标签", "在左栏「标签」旁边加一个，再回来挂。")
            return
        on = {g["id"] for g in tags if g["name"] in self._pending_tags}
        pop = TagPickPopup(tags, on, self)
        pop.toggled.connect(self._quick_toggle_tag)
        self._quick_tag_popup = pop
        popups.place_popup(pop, self.quick_flag)
        pop.show()

    def _quick_toggle_tag(self, tag_id: int) -> None:
        """连选几个标签不用反复开菜单（TagPickPopup 点一项不关）。"""
        g = next((t for t in services.tag_all() if t["id"] == tag_id), None)
        if g is None:
            return
        name = g["name"]
        if name in self._pending_tags:
            self._pending_tags = [t for t in self._pending_tags if t != name]
        else:
            self._pending_tags = self._pending_tags + [name]
        self._quick_tag_popup.set_checked(
            {t["id"] for t in services.tag_all()
             if t["name"] in self._pending_tags})

    def _quick_add(self) -> None:
        text = self.quick_input.text().strip()
        if not text:
            return
        lists, tag_names = self._known_names()
        parsed = dateparse.parse_title(text, lists, tag_names)
        title = parsed.cleaned or text
        view = self._view
        # 优先级 / 清单 / 标签一律读 _pending_*：那是「语法 + 旗子手选」合流后的
        # 那一份，直接读 parsed 会把从弹层选的值丢掉
        priority = self._pending_prio
        if not priority and view in ("p3", "p2", "p1", "p0"):
            priority = int(view[1])
        due = self._pending_date or parsed.date
        if not due:
            if view in ("today", "soon7"):
                due = QDate.currentDate().toString("yyyy-MM-dd")
        list_name = self._pending_list or self._default_list_for_view()
        tid = services.todo_add(title, priority=priority, due_date=due,
                                due_time=self._pending_time or parsed.time,
                                list_name=list_name,
                                repeat=self._pending_repeat,
                                reminder=self._pending_reminder,
                                end_date=self._pending_end,
                                end_time=self._pending_end_time)
        sounds.play("todo_created")
        tag_ids = [t["id"] for t in services.tag_all()
                   if t["name"] in (self._pending_tags or [])]
        if view.startswith("tag:"):
            tag_ids.append(int(view[4:]))
        if tag_ids:
            services.todo_set_tags(tid, tag_ids)
        self._reset_quick_input()
        self.reload()
        # 建完立刻把这条选出来滚到眼前：连加十几条的时候，不然根本不知道
        # 刚那条落到了哪儿
        self._selected_id = tid
        self._mark_selected()
        row = self._items.get(tid)
        if row is not None:
            self.list_widget.ensureWidgetVisible(row, 0, 24)

    def _default_list_for_view(self) -> str:
        view = self._view
        if view.startswith("list:"):
            return view[5:]
        if view.startswith("folder:"):
            first = next((l["name"] for l in services.list_all()
                          if l.get("folder_id") == int(view[7:])), "收集箱")
            return first
        return "收集箱"

    # ==================================================================
    # 左栏增删
    # ==================================================================
    def _nav_add_into(self, key: str) -> None:
        if key.startswith("list:") or key.startswith("folder:"):
            self._quick_input_into(key)
        elif key == "inbox":
            self.quick_input.setFocus()
            self._set_view("inbox")

    def _quick_input_into(self, key: str) -> None:
        self._set_view(key)
        self.quick_input.setFocus()

    def _nav_more(self, key: str, pos) -> None:
        if key.startswith("list:") or key.startswith("folder:"):
            self._list_menu(key, pos)
        elif key.startswith("tag:"):
            self._tag_menu(key, pos)
        elif key.startswith("filter:"):
            self._filter_menu(key, pos)
        elif key == "trash":
            self._trash_menu(pos)

    def _menu_at(self, items, anchor_pos, on_pick, danger=(), checked=None):
        menu = TickMenu(items, self, checked=checked, danger=danger)
        menu.picked.connect(on_pick)
        menu.adjustSize()
        menu.move(anchor_pos)
        menu.show()

    def _list_menu(self, key: str, pos) -> None:
        name = key.split(":", 1)[1]
        kind = "folder" if key.startswith("folder:") else "list"
        lst = next((l for l in services.list_all()
                    if str(l["id"]) == name or l["name"] == name), None)
        if not lst:
            return
        items = [("edit", "edit", "编辑清单"),
                 ("pin", "pin", "取消置顶" if lst.get("pinned") else "置顶清单"),
                 ("add", "plus", "新建清单"),
                 ("archive", "archive", "归档"),
                 ("delete", "trash", "删除清单")]
        if kind == "folder":
            items = [("edit", "edit", "编辑文件夹"),
                     ("add", "plus", "在此新建清单"),
                     ("delete", "trash", "删除文件夹")]

        def pick(v: object) -> None:
            v = str(v)
            if v == "edit":
                from ..todo_dialogs import ListDialog
                ListDialog(self, list_row=lst).exec()
                self._rebuild_nav()
                # 改的正是当前这张清单：导航 key 是 list:{名字}，不跟着换
                # 的话 _collect 按旧名字匹配不到任何一条，视图原地空掉，
                # 快速添加还会把新任务写进已经不存在的清单名里
                new_name = (services.list_get(lst["id"]) or {}).get("name") or ""
                if self._view == "list:%s" % lst["name"] and new_name:
                    self._set_view("list:%s" % new_name)
                else:
                    self.reload()
            elif v == "pin":
                services.list_update(lst["id"], pinned=0 if lst.get("pinned") else 1)
                self._rebuild_nav()
                self._set_view(self._view)
            elif v == "add":
                self._add_list(folder_id=lst["id"] if kind == "folder" else 0)
            elif v == "archive":
                if not popups.confirm(
                        self, "归档清单",
                        "归档「%s」？清单和里面的待办都从列表里消失，"
                        "只能到左栏「已归档清单」里逐条取回。" % lst["name"]):
                    return
                services.list_update(lst["id"], archived=1)
                self._rebuild_nav()
                self._set_view("today")
            elif v == "delete":
                kids = [l for l in services.list_all()
                        if l.get("folder_id") == lst["id"]] if kind == "folder" else []
                if kind == "folder":
                    head = "删除文件夹「%s」？" % lst["name"]
                    tail = ("里面的 %d 个子清单会一起删掉。" % len(kids)) if kids \
                        else "这个文件夹是空的。"
                else:
                    n = sum(1 for t in services.todo_list()
                            if t.get("list_name") == lst["name"])
                    head = "删除清单「%s」？" % lst["name"]
                    tail = ("%d 条待办会一起删掉。" % n) if n else "里面没有待办。"
                if not popups.confirm(self, "删除", head + tail):
                    return
                services.list_delete(lst["id"], lst["name"])
                self._rebuild_nav()
                self._set_view("today")
        self._menu_at(items, pos, pick, danger=("delete",))

    def _archived_lists_menu(self, anchor: QWidget) -> None:
        """左栏「已归档清单」：列出来，点一条就取消归档。"""
        arch = [e for e in services.list_all(include_archived=True)
                if e.get("archived")]
        items = [(str(e["id"]), "restore", "取消归档：%s" % e["name"])
                 for e in arch]

        def pick(v: object) -> None:
            services.list_update(int(v), archived=0)
            self._rebuild_nav()
            self.reload()
        self._menu_at(items, anchor.mapToGlobal(QPoint(0, anchor.height())), pick)

    def _tag_menu(self, key: str, pos) -> None:
        tag = next((t for t in services.tag_all() if str(t["id"]) == key[4:]), None)
        if not tag:
            return
        items = [("edit", "edit", "编辑标签"), ("delete", "trash", "删除标签")]

        def pick(v: object) -> None:
            if str(v) == "edit":
                from ..todo_dialogs import TagDialog
                TagDialog(self, tag_row=tag).exec()
            elif popups.confirm(
                    self, "删除标签",
                    "删除标签「%s」？%d 条待办会去掉这个标签，"
                    "待办本身不删。" % (tag["name"],
                                     len(services.tag_todo_ids(tag["id"])))):
                services.tag_delete(tag["id"])
            self._rebuild_nav()
            self.reload()
        self._menu_at(items, pos, pick, danger=("delete",))

    def _filter_menu(self, key: str, pos) -> None:
        fl = next((f for f in services.filter_all()
                   if str(f["id"]) == key[7:]), None)
        if not fl:
            return
        items = [("edit", "edit", "编辑过滤器"), ("delete", "trash", "删除过滤器")]

        def pick(v: object) -> None:
            if str(v) == "edit":
                from ..todo_dialogs import FilterDialog
                FilterDialog(self, filter_row=fl).exec()
            elif popups.confirm(self, "删除过滤器",
                                "删除过滤器「%s」？" % fl["name"]):
                services.filter_delete(fl["id"])
            self._rebuild_nav()
            self.reload()
        self._menu_at(items, pos, pick, danger=("delete",))

    def _trash_menu(self, pos) -> None:
        items = [("clear", "trash", "清空垃圾桶")]

        def pick(v: object) -> None:
            n = len(services.trash_list())
            # 全应用只有这一处是真的不可逆，以前点一下就全没了，连个问都不问
            if n and popups.confirm(self, "清空垃圾桶",
                                    "彻底删除垃圾桶里的 %d 条？不留备份，找不回来。"
                                    % n):
                services.trash_clear()
                self.reload()
        self._menu_at(items, pos, pick, danger=("clear",))

    def _add_list(self, folder_id: int = 0) -> None:
        from ..todo_dialogs import ListDialog
        if ListDialog(self, folder_id=folder_id).exec():
            self._rebuild_nav()
            self.reload()

    def _add_tag(self) -> None:
        from ..todo_dialogs import TagDialog
        if TagDialog(self).exec():
            self._rebuild_nav()
            self.reload()

    def _add_filter(self) -> None:
        from ..todo_dialogs import FilterDialog
        if FilterDialog(self).exec():
            self._rebuild_nav()
            self.reload()
