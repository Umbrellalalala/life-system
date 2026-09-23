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
    QColor, QFont, QPainter, QDrag, QDesktopServices, QTextCursor, QPixmap,
    QKeySequence, QShortcut,
    QTextBlockFormat, QTextListFormat,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QLabel, QLineEdit,
    QPushButton, QListWidget, QTextEdit, QScrollArea,
    QSizePolicy, QApplication, QSplitter,
)

from .. import dateparse, db, popups, services, sounds, theme, widgets
from ..focus_ui import POPUP_FLAGS
from ..todo_icons import (TickIcon, ColorDot, PrioCheckBox, FlagButton,
                           PRIO_COLOR_KEY)

# 自定义拖拽 mime：携带任务 id，用于列表内排序 + 拖到左侧导航改属性
MIME_TODO = "application/x-lifesystem-todo"

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


# 待办里可点开的链接：算法复习待办把力扣地址写在备注第一行，这里从备注/标题捞。
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


def _review_ask(parent, todo_id: int, occ: str = "") -> bool:
    """勾掉复习待办后问一句结论，据此重排下一个记忆点（算法 / 八股共用）。

    返回 False 表示用户没答，调用方要把这次勾选退回未完成：结论没记下来就
    不能往前走排期，否则这道题会被默认当成「做出来了」，越排越远。
    """
    kind = services.review_kind_of_todo(todo_id)
    if not kind:
        return True                     # 不是复习待办，正常放行
    ask, opts = _REVIEW_ASK[kind]
    pick, ok = popups.get_item(parent, "复习结论", ask, opts)
    if not ok:
        services.occ_set_done(todo_id, occ, False)
        return False
    services.review_resolve_todo(todo_id, independent=(pick == opts[0]))
    # 复习待办的反馈用「答对/答错」音，调用方就不会再叠一层完成音
    sounds.play("answer_correct" if pick == opts[0] else "answer_wrong")
    return True


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


def _detail_date_label(due: str, due_time: str = "") -> str:
    """详情面板日期胶囊的文案：滴答对过期任务写「5天前, 9月14日」。"""
    d = QDate.fromString(due, "yyyy-MM-dd")
    if not d.isValid():
        return due
    left = QDate.currentDate().daysTo(d)
    date_part = f"{d.month()}月{d.day()}日"
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


def _date_state(due: str, due_time: str = "", countdown: bool = False) -> tuple[str, str]:
    """截止日期/时间 → (展示文本, 状态色键)。

    countdown = ⋯ 菜单里的「显示倒数日」：滴答把「3月5日」换成「剩余N天」，
    逾期那条换成「已逾期N天」。今天/明天本来就是相对说法，不再套一层。
    """
    if not due:
        return "", "normal"
    d = QDate.fromString(due, "yyyy-MM-dd")
    if not d.isValid():
        return due, "normal"
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
        for tg in tags:
            row = _MenuRow("tag", tg["name"], int(tg["id"]) in on_task,
                           False, card)
            row.clicked_.connect(lambda i=tg["id"]: self._fire(i))
            lay.addWidget(row)
        self.setFixedWidth(width)

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


class DatePickerPopup(QFrame):
    """滴答清单式日期选择弹窗：快捷日期 + 月历 + 时间，确定/清除。"""

    accepted = Signal(str, str)   # (date, time)
    cleared = Signal()
    repeatPicked = Signal(str)      # 重复规则，"" = 不重复
    reminderPicked = Signal(str)    # 提醒的绝对时间串，"" = 无提醒

    def __init__(self, date: str = "", time_v: str = "", parent=None,
                 repeat: str = "", reminder: str = ""):
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

        # 原来这里有个「日期 / 时间段」二级 tab，但时间段只是个「即将上线」的
        # 占位页 —— 点了切过去看到一行灰字，比没有更糟。库里也还没有
        # end_date 字段，等真做持续时间段时再一起加回来。
        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(0, 4, 0, 0)
        ml.setSpacing(8)
        root.addWidget(main, 1)

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
        ml.addLayout(quick_row)

        # 月历
        head = QHBoxLayout()
        self.cal_title = QLabel()
        self.cal_title.setObjectName("CalHeader")
        head.addWidget(self.cal_title)
        head.addStretch(1)
        for icon, cb in (("‹", lambda: self._shift_month(-1)),
                         ("○", self._goto_today),
                         ("›", lambda: self._shift_month(1))):
            b = QPushButton(icon)
            b.setObjectName("CalNav")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(cb)
            head.addWidget(b)
        ml.addLayout(head)

        wd_row = QHBoxLayout()
        wd_row.setSpacing(2)
        for name in ("日", "一", "二", "三", "四", "五", "六"):
            lbl = QLabel(name)
            lbl.setObjectName("CalWd")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            wd_row.addWidget(lbl, 1)
        ml.addLayout(wd_row)

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
        ml.addWidget(grid_holder)

        # 时间行（点击展开/收起选择器）
        self.time_btn = self._popup_row(ml, "clock", "时间", "accent")
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
        ml.addWidget(self.time_panel)

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
        self.cal_title.setText(f"{self._month.month()}月 {self._month.year()}年")
        start = self._month.addDays(-(self._month.dayOfWeek() % 7))
        today = QDate.currentDate()
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

    # ---- 交互 ----
    def _shift_month(self, delta: int) -> None:
        self._month = self._month.addMonths(delta)
        self._refresh_calendar()

    def _goto_today(self) -> None:
        self._selected = QDate.currentDate()
        self._month = QDate(self._selected.year(), self._selected.month(), 1)
        self._refresh_calendar()

    def _pick_day(self, idx: int) -> None:
        self._selected = self._day_btns[idx]._date
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
        self.accepted.emit(self._selected.toString("yyyy-MM-dd"), self._time)
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
        if arrow:
            lay.addWidget(TickIcon("chevron_right", 12, "muted", self))

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

    ``items`` 是 ``(取值, 图标 kind, 文案[, 图标颜色])`` —— 取值和图标名分开，
    是因为优先级 / 重复这类菜单里多行会共用同一个图标（三面旗子），
    若按图标回传就会全部落到第一行；第四位可选，给需要单独上色的图标
    （优先级四档要红 / 黄 / 蓝，见 `_prio_items`）。
    """

    picked = Signal(object)

    def __init__(self, items: list[tuple[object, str, str]], parent=None,
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
                               ("timeline", "timeline", "时间线视图（暂未支持）")):
            b = _ViewBtn(kind, tip, card)
            b.setEnabled(key != "timeline")
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
                w.setParent(None)
                w.deleteLater()
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


class BoardColumn(QFrame):
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
    其它控件都进当前列。代价是看板上没有拖拽排序和键盘导航（那两个是列表的事）。
    """

    emptyClicked = Signal()
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
    def clear_items(self) -> None:
        while self._lay.count() > 1:          # 末尾 stretch 必须留着
            it = self._lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._widgets.clear()
        self._columns.clear()
        self._current = None

    def add_widget(self, widget: QWidget) -> None:
        if isinstance(widget, GroupHeader):
            col = BoardColumn(self.COL_W)
            col.set_header(widget)
            self._lay.insertWidget(self._lay.count() - 1, col)
            self._columns.append(col)
            self._current = col
        else:
            if self._current is None:
                # 「分组=无」时没有组标题，也要有一列来装卡片
                col = BoardColumn(self.COL_W)
                self._lay.insertWidget(self._lay.count() - 1, col)
                self._columns.append(col)
                self._current = col
            self._current.add_card(widget)
        self._widgets.append(widget)

    def finalize_layout(self) -> None:
        self._lay.activate()

    def set_reorder_enabled(self, on: bool) -> None:
        pass          # 看板不支持拖拽重排，接口留着让 reload 少一个分支

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


class QuadCard(QFrame):
    """四象限页里的一格：彩色罗马数字角标 + 标题，下面按日期分组列任务。

    和 BoardColumn 一样只暴露 `add_widget`，reload 里那条「组标题 → 行」的
    调用序列不用为这一页再写一遍。
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

    def add_widget(self, w: QWidget) -> None:
        w.setParent(self.body)
        bl = self.body.layout()
        bl.insertWidget(bl.count() - 1, w)
        w.show()

    # ---- 拖进来就是改优先级 ----
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(MIME_TODO):
            widgets._apply_property(self, "drop", "true")
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        widgets._apply_property(self, "drop", "false")

    def dropEvent(self, event) -> None:  # noqa: N802
        widgets._apply_property(self, "drop", "false")
        if not event.mimeData().hasFormat(MIME_TODO):
            return
        raw = bytes(event.mimeData().data(MIME_TODO)).decode()
        self.dropped.emit(int(raw), self._prio)
        event.acceptProposedAction()


class QuadArea(QScrollArea):
    """四象限页：2×2 四格，整页纵向滚动。

    同一行两格等高（滴答也是这样：空的另一格会撑出「没有任务」那一行），
    所以格子只给最小高度、由内容把行撑开，而不是各自限死一个高度。
    """

    emptyClicked = Signal()

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
                w.setParent(None)
                w.deleteLater()
        self._widgets.clear()
        self._cards.clear()

    def add_card(self, idx: int, card: QuadCard) -> None:
        self._grid.addWidget(card, idx // 2, idx % 2)
        self._cards.append(card)

    def add_widget(self, w: QWidget) -> None:
        self._widgets.append(w)

    def finalize_layout(self) -> None:
        self._grid.activate()

    def set_reorder_enabled(self, on: bool) -> None:
        pass          # 象限页不支持拖拽重排

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


class _RowSub(QFrame):
    """「显示检查事项」打开时，行内列出的那条子任务。

    只想要一个能点的小勾选框 + 一行字，所以不复用详情面板里的 _SubRow（那套带拖拽
    排序、删除按钮和输入态，塞进列表行会把行高和事件都搞乱）。
    """

    toggled_sub = Signal(object, bool)

    def __init__(self, sub: dict, indent: int = 0, parent=None):
        super().__init__(parent)
        self.sub = sub
        self.setObjectName("RowSub")
        self.setFixedHeight(22)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(indent, 0, 0, 0)
        lay.setSpacing(7)
        self.check = PrioCheckBox(bool(sub["done"]), 14)
        self.check.toggled.connect(lambda c: self.toggled_sub.emit(sub, c))
        lay.addWidget(self.check)
        self.lbl = QLabel(sub["title"])
        self.lbl.setObjectName("RowSubText")
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
                                  bool(disp.get("countdown")))
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
            for s in (data.get("subs") or []):
                row = _RowSub(s, 27)
                row.toggled_sub.connect(self.subToggled.emit)
                outer.addWidget(row)
                extra += 22
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
        pix = self.grab()
        drag.setPixmap(pix)
        drag.setHotSpot(QPoint(24, pix.height() // 2))
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
        if checked and not _review_ask(self, self._todo_id, self._occ):
            self.data["done"] = 0       # 复习结论没答，这条已经退回未完成
        elif checked and not services.review_kind_of_todo(self._todo_id):
            sounds.play("todo_done")
        self._refresh_style()
        self.changed.emit()

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

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
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
    addRequested = Signal(str)
    moreRequested = Signal(str, object)
    expandToggled = Signal(str)

    def __init__(self, key: str, text: str, icon_kind: str = "list",
                 icon_key: str = "muted", icon_hex: str = "", tint: str = "",
                 count: int | str = "", indent: int = 0, expandable: bool = False,
                 expanded: bool = True, show_add: bool = False,
                 dot_hex: str = "", parent=None):
        super().__init__(parent)
        self.key = key
        self._checked = False
        self._hover = False
        self._expandable = expandable
        self._expanded = expanded
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

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        if self._expandable and event.position().x() < 26:
            self.set_expanded(not self._expanded)
            self.expandToggled.emit(self.key)
            return
        self.picked.emit(self.key)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(MIME_TODO):
            event.acceptProposedAction()
            self._set_drop_hot(True)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(MIME_TODO):
            event.acceptProposedAction()
            self._set_drop_hot(True)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._set_drop_hot(False)

    def dropEvent(self, event) -> None:  # noqa: N802
        self._set_drop_hot(False)
        if event.mimeData().hasFormat(MIME_TODO):
            tid = int(bytes(event.mimeData().data(MIME_TODO)).decode())
            self.dropped.emit(self.key, tid)
            event.acceptProposedAction()

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

    taskSaved = Signal(int)
    taskDeleted = Signal(int)
    tagsChanged = Signal()        # 只改标签：刷角标就够，别整页重建
    backRequested = Signal()      # 窄屏整页化时的「← 返回列表」

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
        if not dirty_title:
            self.title_input.setPlainText(task["title"])
        if not dirty_note:
            self._load_md(self.note_input, task.get("note") or "")
        self._link_url = _first_url(task)
        self.link_btn.setVisible(bool(self._link_url))
        if self._link_url:
            self.link_btn.setToolTip("在浏览器打开\n%s" % self._link_url)
        self.flag.set_priority(task.get("priority", 0))
        self.done_check.set_checked(bool(task.get("done")))
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
        self.date_chip.text.setText(_detail_date_label(date, time_v))
        widgets._apply_property(self.date_chip.text, "placeholder", "false")
        left = QDate.currentDate().daysTo(QDate.fromString(date, "yyyy-MM-dd"))
        self.date_chip.icon.set_color_key("red" if left < 0 else "accent")
        self.date_chip.text.setStyleSheet(
            f"color: {theme.get('red') if left < 0 else theme.get('text')};")
        self.date_chip.repeat_icon.setVisible(bool((t.get("repeat") or "").strip()))

    def _sync_list_rows(self) -> None:
        """底栏显示所属清单的图标 + 名字（顶栏那个位置现在是完成勾选框）。"""
        name = self._task.get("list_name") or "收集箱"
        hex_value, icon_kind = "", "inbox"
        if name != "收集箱":
            for lst in services.list_all():
                if lst["name"] == name:
                    hex_value = lst.get("color") or ""
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
        if hex_value:
            ic.set_color_hex(hex_value)
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
        self._task["done"] = int(checked)
        services.occ_set_done(self._task["id"], self._occ, checked)
        if checked and not _review_ask(self, self._task["id"], self._occ):
            self._task["done"] = 0      # 复习结论没答，这条已经退回未完成
        elif checked and not services.review_kind_of_todo(self._task["id"]):
            sounds.play("todo_done")
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
            ("archive", "archive", "归档任务"),
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
            services.todo_update(tid, archived=1)
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
                              reminder=t.get("reminder") or "")
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

    def _on_date_picked(self, date: str, time_v: str) -> None:
        if not self._task:
            return
        if self._occ:
            # 从周期打开的就只挪这个周期，其它周期留在系列原位
            services.occ_move(self._task["id"], self._occ, date, time_v)
            self._disp_date, self._disp_time = date, time_v
        else:
            self._task["due_date"], self._task["due_time"] = date, time_v
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
                # setParent(None) 之后 item 就不再持有该控件，必须只取一次
                w.setParent(None)
                w.deleteLater()
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
        if checked:
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
                w.setParent(None)
                w.deleteLater()
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
        self.title.clicked.connect(self._rename)
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
        # 直接 grab() 会把底色烤成黑色（这行自己是 transparent 的），先铺一层
        pm = QPixmap(self.size())
        pm.fill(QColor(theme.get("bg_alt")))
        self.render(pm)
        drag.setPixmap(pm)
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
    openReview = Signal(str, int)   # (kind, 题目 id) 点复习待办 → 跳到刷题页那道题

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
        self._date_popup: DatePickerPopup | None = None
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
        self._show_checks = db.get_setting("todo_show_checks") == "1"
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
        try:
            i = int(ident)
        except ValueError:
            return False
        if kind == "list" or kind == "folder":
            return services.list_get(i) is not None
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
        self._sync_detail_mode()

    def _sync_detail_mode(self) -> None:
        """在「分割器里的一栏」和「浮在右侧的抽屉」之间切换。"""
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
            if self._detail_open():
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
        self._nav_lay.setSpacing(1)
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
        row = SideRow(key, text, **kw)
        # 只有 _nav_more 认得的 key 才有右键菜单，别给智能视图挂空按钮
        row.set_has_menu(key.startswith(("list:", "folder:", "tag:", "filter:"))
                         or key == "trash")
        row.picked.connect(self._set_view)
        row.dropped.connect(self._on_nav_drop)
        row.moreRequested.connect(self._nav_more)
        row.addRequested.connect(self._nav_add_into)
        row.expandToggled.connect(self._toggle_folder)
        self._nav_rows[key] = row
        self._nav_insert(row)
        return row

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
        plain = lambda t: (not t["done"] and not t.get("abandoned")   # noqa: E731
                           and not (t.get("repeat") or "").strip()
                           and (t.get("list_name") or "收集箱") not in hidden)
        spans = {"today": (today_s, today_s), "soon7": (today_s, in7_s)}
        bounds = {"today": lambda d: d <= today_s, "soon7": lambda d: d <= in7_s}
        out: dict[str, int] = {}
        for view, (start, end) in spans.items():
            n = sum(1 for t in todos
                    if plain(t) and t["due_date"] and bounds[view](t["due_date"]))
            n += sum(1 for o in services.cal_occurrences(start, end)
                     if o["repeat"] and not o["done"]
                     and (o["list_name"] or "收集箱") not in hidden)
            out[view] = n
        out["inbox"] = sum(1 for t in todos if not t["done"]
                           and (t.get("list_name") or "收集箱") == "收集箱")
        out["done"] = sum(1 for t in todos if t["done"])
        out["trash"] = len(services.trash_list())
        return out

    def _rebuild_nav(self) -> None:
        """整栏重建。清单/标签/过滤器会随数据变，逐条 diff 不划算。"""
        while self._nav_lay.count() > 1:
            it = self._nav_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._nav_rows.clear()

        todos = services.todo_list()
        sc = self._smart_counts()
        self._side_row("soon7", "最近7天", icon_kind="week", count=sc["soon7"])
        self._side_row("today", "今天", icon_kind="today", count=sc["today"])
        # 滴答把这个入口放在左侧图标栏，我们沿用文字导航，就排在「今天」下面。
        # 四格全铺时中栏要占满宽度，详情走抽屉（见 _want_overlay）。
        self._side_row("quad", "四象限", icon_kind="quad", icon_hex="#3d8bff",
                       icon_key="accent")
        # 滴答这两个日历图标格子里分别写着今天的日期号和星期两字母缩写（9/19 周六
        # 就是「19」和「Sa」），是这两个入口最主要的辨识点。格子要容得下两位数字，
        # 所以比其它导航图标略大一号。
        _today = QDate.currentDate()
        for _k, _g in (("today", str(_today.day())),
                       ("soon7", _WEEK_ABBR[_today.dayOfWeek() - 1])):
            _row = self._nav_rows[_k]
            _row.icon.setFixedSize(17, 17)
            _row.icon.set_glyph(_g)
        self._side_row("inbox", "收集箱", icon_kind="inbox", count=sc["inbox"])

        # ---- 清单（含文件夹）----
        self._nav_insert(self._section_header("清单", self._add_list))
        entries = services.list_all()
        folders = [e for e in entries if e.get("kind") == "folder"]
        for f in folders:
            kids = [e for e in entries
                    if e.get("kind") != "folder" and e.get("folder_id") == f["id"]]
            opened = f["id"] in self._open_folders
            fcount = sum(1 for t in todos if not t["done"]
                         and t.get("list_name") in {k["name"] for k in kids})
            self._side_row(f"folder:{f['id']}", f["name"], icon_kind="folder_open"
                           if opened else "folder", icon_hex=f.get("color") or "",
                           expandable=True, expanded=opened, count=fcount,
                           show_add=True)
            if opened:
                for k in kids:
                    self._list_row(k, todos, indent=18)
        for e in entries:
            if e.get("kind") != "folder" and not e.get("folder_id"):
                self._list_row(e, todos, indent=0)
        # 归档掉的清单默认不列出来，原来就没有任何入口能把它取回来 ——
        # 归档等于永久删除。这里补一行，点开逐个取消归档。
        arch = [e for e in services.list_all(include_archived=True)
                if e.get("archived")]
        if arch:
            row = SideRow("archived_lists", "已归档清单", icon_kind="archive",
                          icon_key="muted", count=len(arch))
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
        for key, name, color, square in QUADRANTS:
            self._side_row(key, name, icon_kind="quad", icon_hex=square,
                           tint=color,
                           count=sum(1 for t in todos if not t["done"]
                                     and t["priority"] == int(key[1:])))
        for fl in services.filter_all():
            self._side_row(f"filter:{fl['id']}", fl["name"], icon_kind="filter",
                           count=self._filter_count(fl, todos))
        self._side_row("done", "已完成", icon_kind="done")
        self._side_row("trash", "垃圾桶", icon_kind="trash")

    def _list_row(self, lst: dict, todos: list, indent: int) -> None:
        count = sum(1 for t in todos if not t["done"]
                    and t.get("list_name") == lst["name"])
        self._side_row(f"list:{lst['name']}", lst["name"],
                       icon_kind=lst.get("icon") or "list",
                       icon_hex=lst.get("color") or "",
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
        self.search_input.returnPressed.connect(self._toggle_search)
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
        lay.addWidget(quick)

        self.list_widget = TaskListArea()
        self.list_widget.dropRequested.connect(self._handle_drop)
        self.list_widget.emptyClicked.connect(self._close_detail)
        self.list_widget.keyMove.connect(self._on_key_move)
        self.list_widget.keyActivate.connect(self._on_key_activate)
        self.list_widget.keyDelete.connect(self._on_key_delete)
        self.list_widget.keyEscape.connect(
            lambda: self._close_detail(keep_cursor=True))
        lay.addWidget(self.list_widget, 1)

        # 看板：⋯ 菜单「视图」第二档。和中栏列表占同一个格子，按视图切换显隐
        self.board = BoardArea()
        self.board.emptyClicked.connect(self._close_detail)
        self.board.hide()
        lay.addWidget(self.board, 1)

        # 四象限：左栏那个 tab 的整页版式，同样占中栏这一格
        self.quad = QuadArea()
        self.quad.emptyClicked.connect(self._close_detail)
        self.quad.hide()
        lay.addWidget(self.quad, 1)
        return canvas

    @property
    def _view_mode(self) -> str:
        """当前该用哪个视图。

        滴答把「列表 / 看板」存在清单自己身上（清单对话框里那排「视图」），
        所以进了某个清单就以它自己的 view_kind 为准；智能清单 / 文件夹 / 标签 /
        过滤器没有这个字段，才用 ⋯ 菜单存的全局那档。
        """
        if self._view.startswith("list:"):
            try:
                lid = int(self._view[5:])
            except ValueError:
                return self._mode_pref
            vk = (services.list_get(lid) or {}).get("view_kind") or ""
            if vk == "kanban":
                return "board"
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
        return self.board if self._view_mode == "board" else self.list_widget

    def _toggle_nav(self) -> None:
        # 收起时 Qt 只把宽度压到 0，isVisible() 依旧为 True，所以判据用尺寸
        self._set_nav_visible(self._pane_real_w(0) <= 0)

    def _toggle_search(self) -> None:
        if self.search_row.isVisible():
            self.search_input.clear()
            self.search_row.hide()
        else:
            self._focus_search()

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
                    hex_value = lst.get("color") or ""
                    if hex_value:
                        self.title_icon.set_color_hex(hex_value)
                    else:
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
            return "overdue"
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
                   if not t["done"] and self._match_filter(t, fl["cond"]))

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
        base = {t["id"]: t for t in services.todo_list()}
        rows = []
        for o in services.cal_occurrences(start, end, include_notes=True):
            if not o["repeat"]:
                continue            # 非重复任务仍由原始 todo 那条路径处理
            src = base.get(o["id"], {})
            rows.append({
                "id": o["id"], "occ": o["occ"],
                "title": o["title"], "note": o["note"],
                "priority": o["priority"], "list_name": o["list_name"],
                "due_date": o["date"], "due_time": o["time"],
                "done": int(o["done"]), "repeat": o["repeat"],
                "reminder": src.get("reminder") or "",
                "kind": o["kind"], "created_at": src.get("created_at") or "",
                "sub_done": o["sub_done"], "sub_total": o["sub_total"],
                "sort_order": o["sort"],
            })
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
                if t["done"] or t.get("abandoned"):
                    flat.append(t)
                continue
            if t["done"] or t.get("abandoned"):
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
                if t["done"]:
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
        board = self._view_mode == "board" and not quad
        self.list_widget.setVisible(not board and not quad)
        self.board.setVisible(board)
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
        for lst in list(buckets.values()) + [flat]:
            for t in lst:
                subs = services.subtask_list(t["id"])
                t["sub_total"] = len(subs)
                t["sub_done"] = sum(1 for s in subs if s["done"])
                if self._show_checks:
                    t["subs"] = subs     # 行内列出检查事项要用

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
            if not items:
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
            header = GroupHeader(key, _group_label(key, items), len(items),
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
        (into if into is not None else self._area).add_widget(w)
        self._items[t["id"]] = w
        return w

    def _on_row_sub_toggle(self, sub: dict, checked: bool) -> None:
        """行内勾选子任务：写库 + 只改这一条，不整页 reload（reload 会打断连续勾）。"""
        services.subtask_update(sub["id"], done=int(checked))
        sub["done"] = int(checked)
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
            out.append(w)
        return out

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

    def _refresh_nav_counts(self) -> None:
        todos = services.todo_list()
        counts = self._smart_counts()
        for k, row in self._nav_rows.items():
            if k in counts:
                row.set_count(counts[k])
            elif k in ("p3", "p2", "p1", "p0"):
                row.set_count(sum(1 for t in todos if not t["done"]
                                  and t["priority"] == int(k[1])))
            elif k.startswith("list:"):
                row.set_count(sum(1 for t in todos if not t["done"]
                                  and t.get("list_name") == k[5:]))
            elif k.startswith("folder:"):
                names = {l["name"] for l in services.list_all()
                         if l.get("folder_id") == int(k[7:])}
                row.set_count(sum(1 for t in todos if not t["done"]
                                  and t.get("list_name") in names))
            elif k.startswith("tag:"):
                ids = {int(k[4:])} | {c["id"] for c in services.tag_all()
                                      if c.get("parent_id") == int(k[4:])}
                linked = set()
                for tid in ids:
                    linked.update(services.tag_todo_ids(tid))
                row.set_count(sum(1 for t in todos if not t["done"]
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

    def _mark_selected(self) -> None:
        """把选中底色落到「当前打开详情那一条」上。reload 会重建行，所以
        建完行也要再调一次。"""
        for tid, row in self._items.items():
            row.set_selected(tid == self._selected_id)
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

    def _show_detail(self, todo_id: int, occ: str = "", disp_date: str = "",
                     disp_time: str = "") -> None:
        # 复习待办不是一条能编辑的任务：在待办里改它的标题、备注都毫无意义，
        # 真正的「做完」发生在刷题页那边，做完会自动把这条勾掉。
        # 所以点它 = 跳到那道题。勾选项（复选框）不受影响，仍然能就地判定。
        kind, item_id = services.review_owner_of_todo(todo_id)
        if kind:
            self.openReview.emit(kind, item_id)
            return
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
        menu.viewPicked.connect(self._on_view_mode)
        menu.toggled.connect(self._on_display_toggle)
        menu.picked.connect(self._on_more_action)
        menu.printRequested.connect(self._open_print_menu)
        menu.exec_at(self.more_btn.mapToGlobal(QPoint(0, self.more_btn.height())))

    def _on_view_mode(self, mode: str) -> None:
        if self._view_mode == mode:
            return
        if self._view.startswith("list:"):
            # 在某个清单里切视图 = 改这个清单自己的设置，下次进来还是它
            try:
                services.list_update(int(self._view[5:]),
                                     view_kind="kanban" if mode == "board" else "list")
            except ValueError:
                self._mode_pref = mode
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
            for t in services.todo_list():
                if t["done"]:
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
                    txt, _state = _date_state(t["due_date"], t.get("due_time", ""))
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
                else:
                    services.todo_delete(todo_id, hard=True)
                self.reload()
            self._menu_at([("restore", "restore", "恢复"),
                           ("purge", "trash", "彻底删除")],
                          anchor.mapToGlobal(QPoint(pos.x(), pos.y())),
                          pick_trash, danger=("purge",))
            return
        items = [
            ("d0", "today", "今天"),
            ("d1", "calendar", "明天"),
            ("d2", "calendar", "后天"),
        ]
        if recurring:
            # 系列没有「移除日期」这个状态，能做的只有跳过这一周期
            items.append(("skip", "close", "跳过此周期"))
        else:
            items.append(("none", "circle", "移除日期"))
        # 四档旗子的颜色和行上勾选框描边同源，见 _prio_items
        items += [("p%d" % v, kind, label, color)
                  for v, kind, label, color in _prio_items()]
        items += [("sub", "subtask_list", "添加子任务"),
                  ("pin", "pin", "取消置顶" if t.get("pinned") else "置顶"),
                  ("abandon", "restore" if t.get("abandoned") else "close",
                   "取消放弃" if t.get("abandoned") else "放弃"),
                  ("move", "folder_open", "移动到"),
                  ("tags", "tag", "标签"),
                  ("sticky", "note", "打开便签"),
                  ("tonote", "edit", "转换为笔记"),
                  ("link", "link", "复制链接")]
        # 归档任务在归档视图里要能退回来；平时则给一个归档入口。
        # 原来「归档任务」写进去之后全项目没有任何地方读，等于把任务丢进黑洞。
        if t.get("archived"):
            items.append(("unarchive", "restore", "取消归档"))
        else:
            items.append(("archive", "archive", "归档"))
        items.append(("copy", "copy", "创建副本"))
        items.append(("trash", "trash", "删除此周期" if recurring else "删除"))
        menu = TickMenu(items, anchor, danger=("trash",),
                        arrows=("move", "tags"))

        def pick(v: object) -> None:
            v = str(v)
            if v.startswith("d"):
                new_date = today.addDays(int(v[1:])).toString("yyyy-MM-dd")
                # 重复任务只挪这一个周期；普通任务 occ_move 会直接改截止日
                services.occ_move(todo_id, occ or t["due_date"], new_date)
            elif v == "skip":
                services.occ_delete(todo_id, occ)
                self.reload()
                return
            elif v == "none":
                services.todo_update(todo_id, due_date="", due_time="")
            elif v == "pin":
                services.todo_update(todo_id, pinned=0 if t.get("pinned") else 1)
            elif v.startswith("p"):
                services.todo_update(todo_id, priority=int(v[1:]))
            elif v == "archive":
                services.todo_update(todo_id, archived=1)
            elif v == "unarchive":
                services.todo_update(todo_id, archived=0)
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
            # 拖到「已完成」也是一次勾选，复习待办同样要问结论
            _review_ask(self, tid, occ)
            if not services.review_kind_of_todo(tid):
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
        elif parsed.date or parsed.time:
            self._pending_date = parsed.date or self._pending_date
            self._pending_time = parsed.time
            # 文字里已经有日期词了，之前从弹层手选的那次就不作数了
            self._date_pinned = False
        elif not self._date_pinned:
            # 把「明天开会」退格成「开会」，日期 chip 得跟着没；
            # 但用户是从日历弹层手选的日期（_date_pinned）时不能清，
            # 否则选完日期再多打几个字，日期就莫名其妙消失了。
            self._pending_date = self._pending_time = ""
        self._pending_list = parsed.list_name
        self._pending_tags = parsed.tags
        self._pending_prio = parsed.priority
        self._update_chip()

    def _update_chip(self) -> None:
        if self._pending_date:
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
                              reminder=self._pending_reminder)
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

    def _on_popup_accepted(self, date: str, time_v: str) -> None:
        self._pending_date, self._pending_time = date, time_v
        self._date_pinned = bool(date or time_v)
        self._update_chip()

    def _on_popup_cleared(self) -> None:
        self._pending_date = ""
        self._pending_time = ""
        self._date_pinned = False
        self._update_chip()

    def _quick_add(self) -> None:
        text = self.quick_input.text().strip()
        if not text:
            return
        lists, tag_names = self._known_names()
        parsed = dateparse.parse_title(text, lists, tag_names)
        title = parsed.cleaned or text
        view = self._view
        priority = parsed.priority
        if not priority and view in ("p3", "p2", "p1", "p0"):
            priority = int(view[1])
        due = self._pending_date or parsed.date
        if not due:
            if view in ("today", "soon7"):
                due = QDate.currentDate().toString("yyyy-MM-dd")
        list_name = parsed.list_name or self._default_list_for_view()
        tid = services.todo_add(title, priority=priority, due_date=due,
                                due_time=self._pending_time or parsed.time,
                                list_name=list_name,
                                repeat=self._pending_repeat,
                                reminder=self._pending_reminder)
        sounds.play("todo_created")
        tag_ids = [t["id"] for t in services.tag_all()
                   if t["name"] in (parsed.tags or [])]
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
                self.reload()
            elif v == "pin":
                services.list_update(lst["id"], pinned=0 if lst.get("pinned") else 1)
                self._rebuild_nav()
                self._set_view(self._view)
            elif v == "add":
                self._add_list(folder_id=lst["id"] if kind == "folder" else 0)
            elif v == "archive":
                services.list_update(lst["id"], archived=1)
                self._rebuild_nav()
                self._set_view("today")
            elif v == "delete":
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
            else:
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
            else:
                services.filter_delete(fl["id"])
            self._rebuild_nav()
            self.reload()
        self._menu_at(items, pos, pick, danger=("delete",))

    def _trash_menu(self, pos) -> None:
        items = [("clear", "trash", "清空垃圾桶")]

        def pick(v: object) -> None:
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
