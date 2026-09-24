"""自然语言日期/时间识别（滴答清单式）。

从任务标题中识别「明天八点 / 周五下午3点半 / 9月16日 8:00 / 3天后」等表达，
返回：
- cleaned：去掉时间表达后的标题
- date  ：yyyy-MM-dd（无法识别为 ""）
- time  ：HH:MM（无法识别为 ""）
- spans ：原文中匹配到的区间列表（用于输入框内蓝色高亮）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from PySide6.QtCore import QDate

# ---------- 中文数字 ----------
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_WEEKDAY = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}


def _cn_to_int(s: str) -> int:
    """中文/阿拉伯数字 → int（支持 十 / 十五 / 二十 / 二十三 / 零五），失败返回 -1。"""
    if not s:
        return -1
    if s.isdigit():
        return int(s)
    if s in _CN_DIGIT:
        return _CN_DIGIT[s]
    if s == "十":
        return 10
    if s.startswith("零") and len(s) > 1:
        return _cn_to_int(s[1:])
    m = re.fullmatch(r"([一二两三四五六七八九]?)十([一二三四五六七八九]?)", s)
    if m:
        tens = _CN_DIGIT.get(m.group(1), 1)
        ones = _CN_DIGIT.get(m.group(2), 0)
        return tens * 10 + ones
    return -1


_NUM = r"(?:\d{1,2}|[零一二两三四五六七八九十]{1,3})"
_DP = r"(?:凌晨|早上|早晨|上午|中午|午后|下午|傍晚|晚上|夜里)"

# ---------- 日期模式（所有候选中取最靠左、最长者） ----------
_DATE_RES = [
    # 相对日：这几个词自带时段（晚=20:00 / 早=08:00），后面还跟着具体点钟时
    # 要把那个点钟挪到同一时段 —— 「今晚八点」是 20:00，不是 08:00
    re.compile(r"(?P<t>今晚|明晚|明早|明晨)"),
    re.compile(r"(?P<t>今天|今日|明天|明日|后天|大后天|大大后天|前天)"),
    # 周几：下下周X / 下周X / 周X / 星期X / 礼拜X（「每周五」不当作日期）
    re.compile(r"(?<!每)(?P<pre>下下|下)?(?:周|星期|礼拜)(?P<wd>[一二三四五六日天])"),
    # N 天后
    re.compile(r"(?P<n>\d{1,3}|[零一二两三四五六七八九十]{1,4})\s*天后"),
    # 带年份：2027年11月1日 / 2027-11-1 / 2027.11.1 / 2027/11/1
    # 没有这条的话 "2027年11月1日" 会被下面「M月D日」吃掉 "11月1日"，
    # 年份静默丢掉还留在标题里 —— 用户写 2027 拿到的却是 2026。
    # 候选按「最靠左 + 最长」排序，这条起点更靠前，天然压过那条。
    re.compile(r"(?P<yr>[12]\d{3})\s*[年./\-]\s*"
               r"(?P<mo>\d{1,2})\s*[月./\-]\s*"
               r"(?P<da>\d{1,2})\s*[日号]?(?!\d)"),
    # 9月16日 / 十六号
    re.compile(rf"(?P<mo>{_NUM})\s*月\s*(?P<da>{_NUM})\s*[日号](?!\d)"),
    re.compile(rf"(?P<da>{_NUM})\s*[日号](?!\d)"),
]

_EVENING = ("今晚", "明晚")      # 自带晚上语义的相对日
_MORNING = ("明早", "明晨")      # 自带早上语义的相对日

_REL_OFFSET = {"今天": 0, "今日": 0, "今晚": 0, "明天": 1, "明日": 1,
               "明早": 1, "明晨": 1, "明晚": 1, "后天": 2, "大后天": 3,
               "大大后天": 4, "前天": -2}

# ---------- 时间模式 ----------
# 冒号式：8:30 / 晚上8:00 / ２０：１０
_TIME_COLON = re.compile(
    rf"(?P<dp>{_DP})?\s*(?P<h>\d{{1,2}}|[零一二两三四五六七八九十]{{1,3}})\s*(?::|：)\s*(?P<mi>\d{{1,2}})")
# 点式：八点 / 8点半 / 下午3点一刻 / 10点20分 / 8点钟
_TIME_OCLOCK = re.compile(
    rf"(?P<dp>{_DP})?\s*(?P<h>\d{{1,2}}|[零一二两三四五六七八九十]{{1,3}})\s*"
    r"(?P<unit>点|(?<![小时])时)钟?"
    r"(?:\s*(?:(?P<half>半)|(?P<qu>一刻|三刻)|(?:(?P<m2>\d{1,2})|(?P<m3>[零一二三四五六七八九十]{1,3}))分?))?")


@dataclass
class Parsed:
    cleaned: str = ""
    date: str = ""   # yyyy-MM-dd
    time: str = ""   # HH:MM
    spans: list = field(default_factory=list)  # [(start, end), ...]
    list_name: str = ""                # 「#清单名」
    tags: list = field(default_factory=list)  # 「@标签名」
    priority: int = 0                  # 「!」/「!!」/「!!!」→ 1/2/3

    @property
    def found(self) -> bool:
        return bool(self.date or self.time)


def _resolve_abs(today: QDate, mo: int, da: int) -> QDate | None:
    """「9月16日」：过去则按明年处理。"""
    if not (1 <= mo <= 12 and 1 <= da <= 31):
        return None
    d = QDate(today.year(), mo, da)
    if not d.isValid():
        return None
    if d < today:
        d = QDate(today.year() + 1, mo, da)
    return d if d.isValid() else None


def _resolve_day_only(today: QDate, da: int) -> QDate | None:
    """「16号」：本月已过则按下月处理。"""
    if not (1 <= da <= 31):
        return None
    d = QDate(today.year(), today.month(), da)
    if not d.isValid() or d < today:
        d = QDate(today.year(), today.month(), 1).addMonths(1)
        d = QDate(d.year(), d.month(), da)
    return d if d.isValid() else None


def _merge_spans(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """排序并合并相邻/仅隔空白的区间，便于整段高亮。"""
    spans = sorted(s for s in spans if s[1] > s[0])
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged:
            ps, pe = merged[-1]
            gap = text[pe:s]
            if s <= pe or not gap.strip():
                merged[-1] = (ps, max(pe, e))
                continue
        merged.append((s, e))
    return merged


def _pick_date(text: str, today: QDate) -> tuple[str, tuple[int, int] | None,
                                                 str, str]:
    """返回 (date, span, default_time, period)。

    period 是 "pm"/"am"：像「今晚」「明早」这种词自带时段，后面紧跟的
    「八点」得跟着挪到 20:00，不能留在 08:00。
    """
    cands: list[tuple[int, int, re.Match]] = []
    for rx in _DATE_RES:
        for m in rx.finditer(text):
            cands.append((m.start(), m.end(), m))
    cands.sort(key=lambda c: (c[0], -(c[1] - c[0])))
    for s, e, m in cands:
        g = m.groupdict()
        d: QDate | None = None
        default_time = ""
        period = ""
        if g.get("t"):
            t = g["t"]
            d = today.addDays(_REL_OFFSET[t])
            if t in _EVENING:
                default_time, period = "20:00", "pm"
            elif t in _MORNING:
                default_time, period = "08:00", "am"
        elif g.get("wd"):
            ahead = (_WEEKDAY[g["wd"]] - today.dayOfWeek()) % 7
            ahead += {"下": 7, "下下": 14}.get(g.get("pre") or "", 0)
            d = today.addDays(ahead)
        elif g.get("n") is not None:
            n = _cn_to_int(g["n"])
            if n < 0:
                continue
            d = today.addDays(n)
        elif g.get("mo"):
            if g.get("yr"):
                # 写了年份就照搬，不做「过期顺延到明年」那套推断
                mo, da = _cn_to_int(g["mo"]), _cn_to_int(g["da"])
                d = QDate(int(g["yr"]), mo, da) if 1 <= mo <= 12 else None
            else:
                d = _resolve_abs(today, _cn_to_int(g["mo"]), _cn_to_int(g["da"]))
        elif g.get("da"):
            d = _resolve_day_only(today, _cn_to_int(g["da"]))
        if d is not None and d.isValid():
            return d.toString("yyyy-MM-dd"), (s, e), default_time, period
    return "", None, "", ""


def _pick_time(text: str, period: str = "") -> tuple[str, tuple[int, int] | None]:
    cands: list[tuple[int, int, re.Match]] = []
    for rx in (_TIME_COLON, _TIME_OCLOCK):
        for m in rx.finditer(text):
            cands.append((m.start(), m.end(), m))
    cands.sort(key=lambda c: (c[0], -(c[1] - c[0])))
    for s, e, m in cands:
        h = _cn_to_int(m.group("h"))
        if not (0 <= h <= 23):
            continue
        g = m.groupdict()
        if g.get("mi") is not None:            # 冒号式
            minute = int(g["mi"])
        elif g.get("half"):                     # 点半
            minute = 30
        elif g.get("qu"):                       # 一刻 / 三刻
            minute = 15 if g["qu"] == "一刻" else 45
        elif g.get("m2") is not None:
            minute = int(g["m2"])
        elif g.get("m3"):
            minute = _cn_to_int(g["m3"])
        else:
            minute = 0
        if not (0 <= minute <= 59):
            continue
        dp = g.get("dp") or ""
        if not dp:
            # 「今晚八点」的时段来自日期词而不是 dp，得同样挪
            dp = {"pm": "晚上", "am": "早上"}.get(period, "")
        if dp in ("中午", "下午", "午后", "傍晚", "晚上", "夜里") and h < 12:
            h += 12
        elif dp == "凌晨" and h == 12:
            h = 0
        return f"{h:02d}:{minute:02d}", (s, e)
    return "", None


def parse_datetime(text: str) -> Parsed:
    """解析标题中的日期与时间。只识别一个日期 + 一个时间。"""
    today = QDate.currentDate()
    date, date_span, default_time, period = _pick_date(text, today)
    time_v, time_span = _pick_time(text, period)

    # 只写了时间（「八点起床」）→ 视为今天
    if time_v and not date:
        date = today.toString("yyyy-MM-dd")
    # 只写了日期且带「今晚/明晚」→ 默认 20:00
    if date and not time_v and default_time:
        time_v = default_time

    spans = _merge_spans(text, [x for x in (date_span, time_span) if x])

    cleaned = text
    if spans:
        pieces, last = [], 0
        for s, e in spans:
            pieces.append(text[last:s])
            last = e
        pieces.append(text[last:])
        cleaned = re.sub(r"\s+", " ", "".join(pieces)).strip()
        cleaned = re.sub(r"^[\s，,。.、;；:：!！?？～~]+|[\s，,。.、;；:：!！?？～~]+$", "", cleaned)
    if (date or time_v) and not cleaned:
        cleaned = text  # 全被识别时保留原标题，避免空标题

    return Parsed(cleaned=cleaned, date=date, time=time_v, spans=spans)


# ---------- 快速添加：清单 / 标签 / 优先级语法 ----------
# 滴答的快速添加除日期外还支持 #清单、!优先级（! 低 / !! 中 / !!! 高）。
# 标签用 @，避开和 #清单 抢同一个前缀。
_RX_LIST = re.compile(r"#(\S+)")
_RX_TAG = re.compile(r"@(\S+)")
_RX_PRIO = re.compile(r"(?<!\S)(!{1,3})(?=\s*$|\s)")


def parse_title(text: str, lists=(), tags=()) -> Parsed:
    """一次解析日期 + 时间 + #清单 + @标签 + !优先级。

    先按原文记录各片段的位置，再把清单/标签/优先级片段「挖空」成空格后交给
    日期解析器 —— 保持字符下标不变，两类片段的区间才能合并回原文坐标。
    """
    known_lists = set(lists)
    known_tags = set(tags)
    today = QDate.currentDate()

    extra: list[tuple[tuple[int, int], str, object]] = []
    list_name = ""
    for m in _RX_LIST.finditer(text):
        if m.group(1) in known_lists:
            list_name = m.group(1)
            extra.append(((m.start(), m.end()), "list", list_name))
            break
    picked_tags: list[str] = []
    for m in _RX_TAG.finditer(text):
        if m.group(1) in known_tags and m.group(1) not in picked_tags:
            picked_tags.append(m.group(1))
            extra.append(((m.start(), m.end()), "tag", m.group(1)))
    priority = 0
    pm = _RX_PRIO.search(text)
    if pm:
        priority = len(pm.group(1))
        extra.append(((pm.start(), pm.end()), "prio", priority))

    masked = list(text)
    for (s, e), _, _ in extra:
        for i in range(s, e):
            masked[i] = " "
    mtext = "".join(masked)

    date, date_span, default_time, period = _pick_date(mtext, today)
    time_v, time_span = _pick_time(mtext, period)
    if time_v and not date:
        date = today.toString("yyyy-MM-dd")
    if date and not time_v and default_time:
        time_v = default_time

    date_spans = _merge_spans(mtext, [x for x in (date_span, time_span) if x])
    all_spans = _merge_spans(text, date_spans + [s for s, _, _ in extra])

    cleaned = text
    if all_spans:
        pieces, last = [], 0
        for s, e in all_spans:
            pieces.append(text[last:s])
            last = e
        pieces.append(text[last:])
        cleaned = re.sub(r"\s+", " ", "".join(pieces)).strip()
        cleaned = re.sub(r"^[\s，,。.、;；:：～~]+|[\s，,。.、;；:：～~]+$", "", cleaned)
    if (date or time_v or list_name or picked_tags or priority) and not cleaned:
        cleaned = text

    return Parsed(cleaned=cleaned, date=date, time=time_v, spans=all_spans,
                  list_name=list_name, tags=picked_tags, priority=priority)


# ---------- 展示辅助 ----------
def human_date(date: str) -> str:
    """yyyy-MM-dd → 今天 / 明天 / 周四 / 9月16日 / 2027年1月1日。"""
    d = QDate.fromString(date, "yyyy-MM-dd")
    if not d.isValid():
        return date
    today = QDate.currentDate()
    left = today.daysTo(d)
    if left == 0:
        return "今天"
    if left == 1:
        return "明天"
    if left == 2:
        return "后天"
    if 0 < left < 7:
        return "周" + "日一二三四五六"[d.dayOfWeek() % 7]
    if d.year() != today.year():
        return f"{d.year()}年{d.month()}月{d.day()}日"
    return f"{d.month()}月{d.day()}日"


def human_time(time_v: str) -> str:
    """08:00 → 8:00（小时去前导零）。"""
    try:
        h, m = time_v.split(":")
        return f"{int(h)}:{int(m):02d}"
    except Exception:
        return time_v
