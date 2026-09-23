"""日历订阅：拉取外部 .ics（URL 或本地文件），解析成只读的订阅事件。

为什么表建在本模块而不是 lifeapp/db.py：db.py 是并行会话改动最勤的共享文件之一，
整块功能自带 `CREATE TABLE IF NOT EXISTS` 就不用去抢它（同 style.py 不进 theme.py
的理由，见 [[ticktick-parity-inline-styles]]）。init_db 是幂等的，多跑一次 DDL 无害。

已知取舍（都是刻意的，别当 bug 修）：
- 时区只认两种写法：带 Z 的 UTC 会换算成本地时间；带 TZID 的**按墙上时间原样显示**
  （不引第三方时区库）。
- 重复规则只展开 FREQ=DAILY/WEEKLY/MONTHLY/YEARLY + INTERVAL/COUNT/UNTIL/BYDAY(周)，
  解析不了的规则就只落第一条，不猜。
- 订阅事件是只读的：不能拖、不能点开编辑卡。
"""
from __future__ import annotations

import re
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone

from ... import db

COLORS = ("#4772fa", "#f0435f", "#0db987", "#ff9f1a", "#9b59b6", "#00a5ff")
_TIMEOUT = 12


def ensure_schema() -> None:
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cal_feed("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT NOT NULL, url TEXT NOT NULL DEFAULT '',"
            " color TEXT NOT NULL DEFAULT '#4772fa',"
            " visible INTEGER NOT NULL DEFAULT 1,"
            " last_sync TEXT NOT NULL DEFAULT '',"
            " error TEXT NOT NULL DEFAULT '')")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cal_feed_event("
            " feed_id INTEGER NOT NULL, uid TEXT NOT NULL,"
            " title TEXT NOT NULL DEFAULT '',"
            " dstart TEXT NOT NULL, dend TEXT NOT NULL,"
            " timed INTEGER NOT NULL DEFAULT 0,"
            " tstart TEXT NOT NULL DEFAULT '', rend TEXT NOT NULL DEFAULT '',"
            " rrule TEXT NOT NULL DEFAULT '')")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feed_event_day"
            " ON cal_feed_event(dstart)")


# ---------------------------------------------------------------- 订阅增删查
def all_feeds() -> list[dict]:
    ensure_schema()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM cal_feed ORDER BY id ASC").fetchall()]


def add_feed(name: str, url: str, color: str = "") -> int:
    ensure_schema()
    name = (name or "").strip() or "未命名订阅"
    color = color or COLORS[len(all_feeds()) % len(COLORS)]
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO cal_feed(name, url, color) VALUES(?,?,?)",
            (name, url.strip(), color))
        return int(cur.lastrowid)


def set_visible(feed_id: int, on: bool) -> None:
    ensure_schema()
    with db.connect() as conn:
        conn.execute("UPDATE cal_feed SET visible=? WHERE id=?", (int(bool(on)), feed_id))


def remove_feed(feed_id: int) -> None:
    ensure_schema()
    with db.connect() as conn:
        conn.execute("DELETE FROM cal_feed WHERE id=?", (feed_id,))
        conn.execute("DELETE FROM cal_feed_event WHERE feed_id=?", (feed_id,))


# ---------------------------------------------------------------- 抓取 + 解析
def _load(source: str) -> str:
    """URL 走 http，其余当本地路径读。失败抛 RuntimeError，带人能看懂的原因。"""
    src = (source or "").strip()
    if not src:
        raise RuntimeError("订阅地址是空的")
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "LifeSystem/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"服务器返回 {e.code}") from e
        except Exception as e:                      # 超时 / DNS / 连接被拒
            raise RuntimeError(f"取不到这个地址：{e.__class__.__name__}") from e
    else:
        path = src.replace("file:///", "").replace("file://", "")
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            raise RuntimeError(f"读不到这个文件：{e.strerror or e}") from e
    text = raw.decode("utf-8-sig", errors="replace")
    if "BEGIN:VCALENDAR" not in text.upper():
        raise RuntimeError("内容不是 .ics（没找到 VCALENDAR）")
    return text


def _unfold(text: str) -> list[str]:
    """RFC 5545 的折行：下一行以空格或制表符开头就是接在上一行后面。"""
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _unescape(v: str) -> str:
    """RFC 5545 的文本转义：\\, \\; \\n \\\\。"""
    return (v.replace("\\,", ",").replace("\\;", ";")
             .replace("\\n", "\n").replace("\\\\", "\\"))


def _prop(line: str) -> tuple[str, dict, str]:
    """拆 `NAME;P=V:值` → (name, params, value)。值里可能有冒号，只切第一个。"""
    head, _, value = line.partition(":")
    parts = head.split(";")
    name = parts[0].strip().upper()
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.strip().upper()] = v.strip()
    return name, params, value.strip()


def _dt(raw: str, params: dict) -> tuple[date, str, bool]:
    """返回 (日期, "HH:MM", 是否定时)。带 Z 的 UTC 换算成本地；TZID 原样取墙上时间。"""
    s = raw.strip()
    if "T" not in s:
        d = datetime.strptime(s[:8], "%Y%m%d").date()
        return d, "", False
    naive = datetime.strptime(s.replace("Z", "")[:15], "%Y%m%dT%H%M%S")
    if s.endswith("Z"):
        naive = naive.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return naive.date(), naive.strftime("%H:%M"), True


def parse_ics(text: str) -> list[dict]:
    """把 VEVENT 段解成 {uid,title,dstart,dend,timed,tstart,rend,rrule}。"""
    out: list[dict] = []
    inside = False
    cur: dict = {}
    for line in _unfold(text):
        up = line.strip().upper()
        if up == "BEGIN:VEVENT":
            inside, cur = True, {"uid": "", "title": "", "dstart": "", "dend": "",
                                 "timed": False, "tstart": "", "rend": "",
                                 "rrule": "", "dur": ""}
            continue
        if up == "END:VEVENT":
            inside = False
            if cur.get("dstart"):
                out.append(cur)
            cur = {}
            continue
        if not inside or ":" not in line:
            continue
        name, params, value = _prop(line)
        if name == "SUMMARY":
            cur["title"] = _unescape(value)
        elif name == "UID":
            cur["uid"] = value
        elif name == "DTSTART":
            d, t, timed = _dt(value, params)
            cur["dstart"] = d.isoformat()
            cur["tstart"], cur["timed"] = t, timed
        elif name == "DTEND":
            d, t, timed = _dt(value, params)
            cur["dend"] = d.isoformat()
            cur["rend"] = t
            if not cur.get("dstart"):
                cur["dstart"] = d.isoformat()
            # 全天事件按 RFC 的约定，DTEND 是「结束的下一天」，往回收一天
            if not timed:
                cur["dend"] = (d - timedelta(days=1)).isoformat()
        elif name == "DURATION":
            cur["dur"] = value
        elif name == "RRULE":
            cur["rrule"] = value
    for e in out:
        # 有些日历不给 DTEND 而给 DURATION，不接的话多日事件会被画成一天
        if not e["dend"] and e.get("dur") and not e["timed"]:
            days = max(1, _dur_days(e["dur"]))
            e["dend"] = (date.fromisoformat(e["dstart"])
                         + timedelta(days=days - 1)).isoformat()
        if not e["rend"] and e.get("dur") and e["timed"]:
            mins = _dur_minutes(e["dur"])
            if mins:
                h, mi = divmod(int(e["tstart"][:2]) * 60 + int(e["tstart"][3:]) + mins, 60)
                e["rend"] = f"{h % 24:02d}:{mi:02d}"
        if not e["dend"]:
            e["dend"] = e["dstart"]
    return out


def _dur_days(raw: str) -> int:
    """P3D / P2W → 天数。"""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:W(\d+))?", raw or "")
    if not m:
        return 0
    return int(m.group(1) or 0) + int(m.group(2) or 0) * 7


def _dur_minutes(raw: str) -> int:
    """PT1H30M / PT45M / PT2H → 分钟。"""
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", raw or "")
    if not m:
        return 0
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def _span_minutes(ev: dict) -> int:
    """定时事件的时长（分钟）。时间轴按它画色块高度，跨夜的不算负数。"""
    if not ev.get("timed") or not ev.get("rend"):
        return 0
    a = ev["tstart"].split(":")
    b = ev["rend"].split(":")
    if len(a) < 2 or len(b) < 2:
        return 0
    return max(0, (int(b[0]) * 60 + int(b[1])) - (int(a[0]) * 60 + int(a[1])))


# ---------------------------------------------------------------- 重复展开
_FREQ = {"DAILY": 1, "WEEKLY": 2, "MONTHLY": 3, "YEARLY": 4}
# 星期几 → 距离本周周一的天数（Python 的 weekday() 就是周一为 0）
_WD = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _rule_parts(rule: str) -> dict:
    """拆 RRULE 字符串；认不了的键直接忽略，不猜。"""
    out: dict = {"freq": 0, "interval": 1, "count": 0, "until": None,
                 "byday": None, "byday_nth": None, "bymonth": None}
    if "FREQ=" not in rule:
        return out
    out["freq"] = _FREQ.get(re.search(r"FREQ=(\w+)", rule).group(1), 0)
    if m := re.search(r"INTERVAL=(\d+)", rule):
        out["interval"] = max(1, int(m.group(1)))
    if m := re.search(r"COUNT=(\d+)", rule):
        out["count"] = int(m.group(1))
    if m := re.search(r"UNTIL=(\d{8})", rule):
        out["until"] = datetime.strptime(m.group(1), "%Y%m%d").date()
    if m := re.search(r"BYDAY=([A-Z,0-9-]+)", rule):
        pairs: list[tuple[int, int]] = []
        for tok in m.group(1).split(","):
            t = tok.strip().upper()
            if t[-2:] in _WD:
                n = re.match(r"^([+-]?\d+)", t)
                pairs.append((_WD[t[-2:]], int(n.group(1)) if n else 0))
        if pairs:
            out["byday"] = sorted({o for o, _ in pairs})
            # 「每月最后一个周五」这种带序数的写法，RFC 5545 说的是当月第几个，
            # 不是「离 DTSTART 最近的那天」，得单独走一条展开分支。
            out["byday_nth"] = sorted(p for p in pairs if p[1]) or None
    if m := re.search(r"BYMONTH=([0-9,]+)", rule):
        mons = sorted({int(x) for x in m.group(1).split(",") if 1 <= int(x) <= 12})
        out["bymonth"] = mons or None
    return out


def _month_nth(y: int, m: int, offset: int, nth: int) -> date | None:
    """某年某月里第 nth 个「星期 offset」；nth 为负就从月末倒数。"""
    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    if nth > 0:
        hit = first + timedelta(days=(offset - first.weekday()) % 7
                                + 7 * (nth - 1))
    else:
        last = nxt - timedelta(days=1)
        hit = last - timedelta(days=(last.weekday() - offset) % 7
                               + 7 * (-nth - 1))
    return hit if first <= hit < nxt else None


def _nth_slots(freq: int, interval: int, d0: date, months: list[int]):
    """按规则依次交出该看的 (年, 月)。按月就隔 interval 个月，按年就隔 interval 年。"""
    if freq == 3:
        k0 = d0.year * 12 + d0.month - 1
        for n in range(k0, k0 + 4800, interval):
            y, mo = divmod(n, 12)
            yield y, mo + 1
    else:
        for y in range(d0.year, d0.year + 400, interval):
            for mo in months:
                yield y, mo


def _step_days(freq: int, interval: int) -> float:
    """一次间隔大约多少天，只用来估算起始下标，不用来定日期。"""
    if freq == 1:
        return float(interval)
    if freq == 2:
        return 7.0 * interval
    if freq == 3:
        return 30.5 * interval
    return 365.25 * interval


def _expand(ev: dict, start: date, end: date) -> list[tuple[date, date]]:
    """把一条（可能重复的）事件展开成落在 [start, end] 里的 (首日, 末日) 列表。

    下标 i 是「从 DTSTART 算起的第几次」，所以 COUNT 数的是绝对次数
    （以前数的是「当前窗口内第几条」，同一个订阅在月视图和周视图里
    会各出 COUNT 条）；同时先把 i 估算到窗口附近再走，2010 年起的
    每周一订阅在 2026 年照样出得来（以前从 d0 一步步走，走不到就放弃了）。
    """
    d0 = date.fromisoformat(ev["dstart"])
    d1 = date.fromisoformat(ev["dend"] or ev["dstart"])
    span = max(0, (d1 - d0).days)
    rule = _rule_parts(ev.get("rrule") or "")
    freq, interval = rule["freq"], rule["interval"]
    count, until = rule["count"], rule["until"]
    if not freq:
        return [] if d1 < start or d0 > end else [(d0, d1)]

    def too_far(hit: date) -> bool:
        return bool(until and hit > until) or hit > end

    out: list[tuple[date, date]] = []
    i0 = max(0, int((start - d0).days / _step_days(freq, interval)) - 3)

    if freq == 2 and rule["byday"]:
        week0 = d0 - timedelta(days=d0.weekday())
        offs = rule["byday"]
        first_w = max(0, int((start - week0).days / (7 * interval)) - 2)
        # 到第 first_w 周**之前**一共出现过几次 —— COUNT 数的是绝对次数。
        # first_w 为 0 时必须从 0 起算：那一周里的命中下面还要自己走一遍，
        # 先把它们计进去会把 4/1 数两次，COUNT=4 就只出 3 条了。
        in_week0 = len([o for o in offs if week0 + timedelta(days=o) >= d0])
        seen = 0 if first_w == 0 else in_week0 + (first_w - 1) * len(offs)
        for w in range(first_w, 1200):
            base = week0 + timedelta(days=7 * interval * w)
            for off in offs:
                hit = base + timedelta(days=off)
                if hit < d0:
                    continue
                if too_far(hit):
                    return out
                if count and seen + 1 > count:
                    return out
                seen += 1
                last = hit + timedelta(days=span)
                if last >= start and last <= end:
                    out.append((hit, last))
                elif last > end:
                    return out
            if base > end:
                break
        return out

    if freq in (3, 4) and rule["byday_nth"]:
        # 「每月最后一个周五」「每年 11 月第四个周四」这类带序数的写法：到每个月里
        # 把命中的那天算出来。以前走通用分支，等于按 DTSTART 是几号来重复，日期全偏。
        seen = 0
        for y, mo in _nth_slots(freq, interval, d0,
                                rule["bymonth"] or list(range(1, 13))):
            first = date(y, mo, 1)
            if too_far(first):        # 这个月的一号都已越过窗口/UNTIL
                break
            hits = sorted(h for h in (_month_nth(y, mo, o, n)
                                      for o, n in rule["byday_nth"]) if h)
            for hit in hits:
                if hit < d0:
                    continue          # 首个系列日之前不算次数
                if too_far(hit):
                    return out
                if count and seen + 1 > count:
                    return out
                seen += 1
                last = hit + timedelta(days=span)
                if last >= start:
                    out.append((hit, last))
        return out

    for i in range(i0, 6000):
        hit = _nth(freq, interval, d0, i)
        if too_far(hit):
            break
        if count and i + 1 > count:
            break
        last = hit + timedelta(days=span)
        if last >= start:
            out.append((hit, last))
        if hit > end:
            break
    return out


def _nth(freq: int, interval: int, d0: date, i: int) -> date:
    """第 i 次（从 0 起）发生的首日。"""
    if freq == 1:
        return d0 + timedelta(days=interval * i)
    if freq == 2:
        return d0 + timedelta(weeks=interval * i)
    return _add_months(d0, interval * i * (1 if freq == 3 else 12))


def _add_months(d: date, n: int) -> date:
    """加 n 个月，月末的日子往下夹（1/31 + 1 月 = 2/28，不滚到 3 月）。"""
    y, m = divmod(d.year * 12 + (d.month - 1) + n, 12)
    m += 1
    leap = (y % 4 == 0 and y % 100 != 0) or y % 400 == 0
    last = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


# ---------------------------------------------------------------- 同步 / 取用
def sync(feed_id: int) -> tuple[bool, str]:
    """抓一次并整批替换该订阅的事件。返回 (成功?, 说明)。"""
    ensure_schema()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM cal_feed WHERE id=?", (feed_id,)).fetchone()
    if not row:
        return False, "订阅不存在"
    row = dict(row)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        events = parse_ics(_load(row["url"]))
    except RuntimeError as e:            # 抓取失败 / 不是 .ics，文案直接给人看
        with db.connect() as conn:
            conn.execute("UPDATE cal_feed SET error=?, last_sync=? WHERE id=?",
                         (str(e), stamp, feed_id))
        return False, str(e)
    except Exception as e:               # 解析里任何意外都不能把异常抛回 UI 线程
        with db.connect() as conn:
            conn.execute("UPDATE cal_feed SET error=?, last_sync=? WHERE id=?",
                         (f"解析失败：{e}", stamp, feed_id))
        return False, f"解析失败：{e.__class__.__name__} {e}"
    with db.connect() as conn:
        conn.execute("DELETE FROM cal_feed_event WHERE feed_id=?", (feed_id,))
        for ev in events:
            conn.execute(
                "INSERT INTO cal_feed_event(feed_id, uid, title, dstart, dend,"
                " timed, tstart, rend, rrule) VALUES(?,?,?,?,?,?,?,?,?)",
                (feed_id, ev["uid"], ev["title"], ev["dstart"], ev["dend"],
                 int(ev["timed"]), ev["tstart"], ev["rend"], ev["rrule"]))
        conn.execute("UPDATE cal_feed SET error='', last_sync=? WHERE id=?",
                     (stamp, feed_id))
    return True, f"{len(events)} 条事件"


def events_in(start_iso: str, end_iso: str) -> dict[str, list[dict]]:
    """{日期: [订阅事件]}，给月视图的 provider 合并用。事件是只读的。"""
    ensure_schema()
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    out: dict[str, list[dict]] = {}
    with db.connect() as conn:
        feeds = [dict(r) for r in conn.execute(
            "SELECT * FROM cal_feed WHERE visible=1").fetchall()]
        if not feeds:
            return out
        rows = [dict(r) for r in conn.execute(
            "SELECT rowid AS id, * FROM cal_feed_event").fetchall()]
    for f in feeds:
        for ev in rows:
            if ev["feed_id"] != f["id"]:
                continue
            for d0, d1 in _expand(ev, start, end):
                cur = d0
                while cur <= d1 and cur <= end:
                    if cur >= start:
                        out.setdefault(cur.isoformat(), []).append({
                            "id": int(ev["id"]), "occ": "", "date": cur.isoformat(),
                            "title": ev["title"] or "（无标题）",
                            "priority": 0, "done": False, "feed": True,
                            "feed_color": f["color"], "list_name": f["name"],
                            "time": ev["tstart"] if ev["timed"] else "",
                            "duration": _span_minutes(ev),
                            "repeat": "", "kind": "task",
                            # 排在本机任务后面；events_in 里已按（时间,标题）排好，
                            # 外层 sort 是稳定的，这个相对顺序不会被打乱
                            "sort": 10 ** 9})
                        if cur == d1:
                            break
                    cur += timedelta(days=1)
    for day in out:
        out[day].sort(key=lambda r: (r["time"] or "99:99", r["title"]))
    return out
