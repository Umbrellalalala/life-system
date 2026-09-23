"""数据访问服务：封装各模块的增删改查。"""
from __future__ import annotations

import calendar as _calendar
import json
import os
import random
import re
import sqlite3
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Optional

from . import db


def _row_to_dict(row: Any) -> dict:
    return dict(row) if row is not None else {}


# ---------- 待办清单 ----------
def todo_list(include_trash: bool = False, include_archived: bool = False,
              kind: str | None = None) -> list[dict]:
    """常规任务列表。默认排除垃圾桶与归档（滴答语义）。"""
    where, args = [], []
    if not include_trash:
        where.append("COALESCE(deleted_at, '') = ''")
    if not include_archived:
        where.append("COALESCE(archived, 0) = 0")
    if kind:
        where.append("COALESCE(kind, 'task') = ?")
        args.append(kind)
    sql = "SELECT * FROM todos"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sort_order ASC, id ASC"
    with db.connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def todo_get(todo_id: int) -> dict:
    with db.connect() as conn:
        return _row_to_dict(conn.execute(
            "SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone())


def todo_add(title: str, note: str = "", priority: int = 0, due_date: str = "",
             due_time: str = "", list_name: str = "收集箱", reminder: str = "",
             repeat: str = "", kind: str = "task",
             duration_min: int = 0) -> int:
    with db.connect() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM todos"
        ).fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO todos(title, note, priority, due_date, due_time, "
            "sort_order, list_name, reminder, repeat, kind, "
            "duration_min) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (title, note, priority, due_date, due_time, max_order + 1,
             list_name, reminder, repeat, kind, duration_min),
        )
        return cur.lastrowid


def todo_update(todo_id: int, **fields: Any) -> None:
    allowed = {"title", "note", "priority", "due_date", "due_time", "done",
               "completed_at", "sort_order", "list_name", "reminder", "repeat",
               "kind", "deleted_at", "archived", "duration_min",
               "pinned", "abandoned", "sticky"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    with db.connect() as conn:
        conn.execute(
            f"UPDATE todos SET {cols} WHERE id = ?",
            (*updates.values(), todo_id),
        )


def todo_reorder(ordered_ids: list[int]) -> None:
    with db.connect() as conn:
        for i, tid in enumerate(ordered_ids):
            conn.execute("UPDATE todos SET sort_order = ? WHERE id = ?", (i, tid))


def todo_delete(todo_id: int, hard: bool = False) -> None:
    """默认软删除进垃圾桶（滴答行为）；hard=True 才真正删掉。"""
    if hard:
        with db.connect() as conn:
            conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
            conn.execute("DELETE FROM subtasks WHERE todo_id = ?", (todo_id,))
            conn.execute("DELETE FROM todo_tags WHERE todo_id = ?", (todo_id,))
        return
    todo_update(todo_id, deleted_at=_now(), done=0)


def todo_restore(todo_id: int) -> None:
    todo_update(todo_id, deleted_at="")


def sticky_opened(todo_id: int, on: bool) -> None:
    """记下「这条的便签开着没」，重启时 restore_all 照着恢复窗口。"""
    todo_update(todo_id, sticky=1 if on else 0)


def trash_list() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM todos WHERE COALESCE(deleted_at, '') <> '' "
            "ORDER BY deleted_at DESC, id DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def trash_clear() -> int:
    """彻底清空垃圾桶，返回删除条数。"""
    ids = [t["id"] for t in trash_list()]
    with db.connect() as conn:
        conn.execute("DELETE FROM todos WHERE COALESCE(deleted_at, '') <> ''")
        for tid in ids:
            conn.execute("DELETE FROM subtasks WHERE todo_id = ?", (tid,))
            conn.execute("DELETE FROM todo_tags WHERE todo_id = ?", (tid,))
    return len(ids)


def todo_postpone(todo_ids: list[int], days: int = 1) -> int:
    """顺延：把任务的截止日期整体往后推 N 天（无日期的按今天起算）。"""
    from datetime import date, timedelta
    base = date.today()
    n = 0
    with db.connect() as conn:
        for tid in todo_ids:
            row = conn.execute(
                "SELECT due_date FROM todos WHERE id = ?", (tid,)).fetchone()
            if row is None:
                continue
            cur = (row["due_date"] or "").strip()
            try:
                start = date.fromisoformat(cur) if cur else base
            except ValueError:
                start = base
            target = max(start, base) + timedelta(days=days)
            conn.execute("UPDATE todos SET due_date = ? WHERE id = ?",
                         (target.isoformat(), tid))
            n += 1
    return n


# ---------- 清单 / 文件夹管理 ----------
def list_all(include_archived: bool = False) -> list[dict]:
    """清单与文件夹混在一张表里（kind 区分），置顶优先、再按 sort_order。"""
    sql = "SELECT * FROM todo_lists"
    if not include_archived:
        sql += " WHERE COALESCE(archived, 0) = 0"
    sql += " ORDER BY COALESCE(pinned, 0) DESC, sort_order ASC, id ASC"
    with db.connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_get(list_id: int) -> dict:
    with db.connect() as conn:
        return _row_to_dict(conn.execute(
            "SELECT * FROM todo_lists WHERE id = ?", (list_id,)).fetchone())


def list_add(name: str, icon: str = "list", color: str = "",
             folder_id: int = 0, view_kind: str = "list",
             kind: str = "list", hide_in_smart: int = 0) -> int:
    with db.connect() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM todo_lists").fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO todo_lists(name, icon, sort_order, color, folder_id, "
            "view_kind, kind, hide_in_smart) VALUES(?,?,?,?,?,?,?,?)",
            (name, icon, max_order + 1, color, folder_id, view_kind,
             kind, hide_in_smart))
        return cur.lastrowid


def list_update(list_id: int, **fields: Any) -> None:
    allowed = {"name", "icon", "color", "folder_id", "view_kind", "kind",
               "hide_in_smart", "pinned", "sort_order", "archived"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    with db.connect() as conn:
        conn.execute(f"UPDATE todo_lists SET {cols} WHERE id = ?",
                     (*updates.values(), list_id))


def list_rename(list_id: int, name: str, old_name: str = "") -> None:
    """重命名清单，同时把任务上的归属名一起改掉。"""
    with db.connect() as conn:
        if not old_name:
            row = conn.execute(
                "SELECT name FROM todo_lists WHERE id = ?", (list_id,)).fetchone()
            old_name = row["name"] if row else ""
        conn.execute("UPDATE todo_lists SET name = ? WHERE id = ?", (name, list_id))
        if old_name:
            conn.execute("UPDATE todos SET list_name = ? WHERE list_name = ?",
                         (name, old_name))


def list_reorder(ordered_ids: list[int]) -> None:
    with db.connect() as conn:
        for i, lid in enumerate(ordered_ids):
            conn.execute("UPDATE todo_lists SET sort_order = ? WHERE id = ?",
                         (i, lid))


def list_delete(list_id: int, list_name: str) -> None:
    """删清单：文件夹连带把子清单提到顶层；清单下的任务回落收集箱。"""
    with db.connect() as conn:
        conn.execute("DELETE FROM todo_lists WHERE id = ?", (list_id,))
        conn.execute("UPDATE todo_lists SET folder_id = 0 WHERE folder_id = ?",
                     (list_id,))
        if list_name:
            conn.execute("UPDATE todos SET list_name = '收集箱' WHERE list_name = ?",
                         (list_name,))


def smart_hidden_lists() -> set[str]:
    """勾了「不在智能清单中显示」的清单名，智能视图要排除它们。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM todo_lists WHERE COALESCE(hide_in_smart,0)=1"
        ).fetchall()
    return {r["name"] for r in rows}


# ---------- 标签管理 ----------
def tag_all() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tags ORDER BY COALESCE(sort_order,0) ASC, id ASC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def tag_add(name: str, color: str = "blue", hex_value: str = "",
            parent_id: int = 0) -> int:
    with db.connect() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM tags").fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO tags(name, color, hex, parent_id, sort_order) "
            "VALUES(?,?,?,?,?)", (name, color, hex_value, parent_id, max_order + 1))
        return cur.lastrowid


def tag_update(tag_id: int, **fields: Any) -> None:
    allowed = {"name", "color", "hex", "parent_id", "sort_order"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    with db.connect() as conn:
        conn.execute(f"UPDATE tags SET {cols} WHERE id = ?",
                     (*updates.values(), tag_id))


def tag_delete(tag_id: int) -> None:
    """删标签：子标签升为顶层，任务上的关联一并清除。"""
    with db.connect() as conn:
        conn.execute("UPDATE tags SET parent_id = 0 WHERE parent_id = ?", (tag_id,))
        conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        conn.execute("DELETE FROM todo_tags WHERE tag_id = ?", (tag_id,))


def todo_tags(todo_id: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT t.* FROM tags t JOIN todo_tags tt ON tt.tag_id = t.id "
            "WHERE tt.todo_id = ? ORDER BY t.id ASC", (todo_id,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def todo_set_tags(todo_id: int, tag_ids: list[int]) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM todo_tags WHERE todo_id = ?", (todo_id,))
        for tid in tag_ids:
            conn.execute(
                "INSERT OR IGNORE INTO todo_tags(todo_id, tag_id) VALUES(?,?)",
                (todo_id, tid))


def tag_todo_ids(tag_id: int) -> list[int]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT todo_id FROM todo_tags WHERE tag_id = ?", (tag_id,)).fetchall()
    return [r["todo_id"] for r in rows]


# ---------- 任务统计（统计页「任务」标签）----------
# 「存活任务」的过滤条件，和 todo_list() 的默认语义保持一致。
_TODO_ALIVE = ("COALESCE(deleted_at, '') = '' AND COALESCE(archived, 0) = 0 "
               "AND COALESCE(kind, 'task') = 'task'")
_TODO_ALIVE_T = ("COALESCE(todos.deleted_at, '') = '' "
                 "AND COALESCE(todos.archived, 0) = 0 "
                 "AND COALESCE(todos.kind, 'task') = 'task'")

# 完成率取「该周期到期的任务」∪「该周期完成的任务」作为分母。
# 只算 due_date 会漏掉大量「没设截止日期、随手就做完」的任务（分母偏小、
# 完成率虚高甚至爆表）；只算 completed_at 又会让完成率突破 100%。
_TODO_RANGE_WHERE = (
    f"({_TODO_ALIVE}) AND ("
    "(COALESCE(due_date, '') <> '' AND date(due_date) BETWEEN ? AND ?) "
    "OR (COALESCE(completed_at, '') <> '' "
    "    AND date(completed_at) BETWEEN ? AND ?))")


def todo_range_stats(start: str, end: str) -> dict:
    """周期内的完成数与完成率。

    完成率按滴答清单的定义：**实际完成的任务 / 原本安排在该周期的任务**。
    分母为 0 时完成率记 0（不显示 NaN）。
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN COALESCE(completed_at, '') <> '' "
            "         AND date(completed_at) BETWEEN ? AND ? "
            "    THEN 1 ELSE 0 END) AS done "
            f"FROM todos WHERE {_TODO_RANGE_WHERE}",
            [start, end, start, end, start, end]).fetchone()
    total = row["total"] or 0
    done = row["done"] or 0
    return {"done": done, "total": total,
            "rate": (done * 100.0 / total) if total else 0.0}


def todo_status_distribution(start: str, end: str) -> list[dict]:
    """周期内任务按完成状态的分布：已完成 / 未完成 / 已逾期。

    「已逾期」= 周期内到期、但到现在还没完成。周期内的未来日期不算逾期。
    """
    from datetime import date as _date
    today = _date.today().isoformat()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT "
            "SUM(CASE WHEN COALESCE(completed_at, '') <> '' "
            "         AND date(completed_at) BETWEEN ? AND ? "
            "    THEN 1 ELSE 0 END) AS done, "
            "SUM(CASE WHEN NOT (COALESCE(completed_at, '') <> '' "
            "                   AND date(completed_at) BETWEEN ? AND ?) "
            "         AND COALESCE(due_date, '') <> '' AND date(due_date) < ? "
            "    THEN 1 ELSE 0 END) AS overdue, "
            "COUNT(*) AS total "
            f"FROM todos WHERE {_TODO_RANGE_WHERE}",
            [start, end, start, end, today, start, end, start, end]).fetchone()
    done = row["done"] or 0
    overdue = row["overdue"] or 0
    pending = max((row["total"] or 0) - done - overdue, 0)
    out = []
    # color 是主题色键，交给环形图按当前明暗主题取色。
    # 「已逾期」必须是红的 —— 和全局「破坏性/逾期标红」的约定一致。
    for name, v, key in (("已完成", done, "focus"),
                         ("未完成", pending, "muted"),
                         ("已逾期", overdue, "red")):
        if v:
            out.append({"name": name, "count": v, "color": key})
    return out


def todo_completed_breakdown(start: str, end: str, by: str = "list") -> list[dict]:
    """周期内**已完成**任务的分类统计。``by`` 取 list / tag / priority。"""
    if by == "tag":
        sql = ("SELECT COALESCE(NULLIF(t.name, ''), '未分类') AS name, "
               "COUNT(DISTINCT todos.id) AS c "
               "FROM todos JOIN todo_tags tt ON tt.todo_id = todos.id "
               "JOIN tags t ON t.id = tt.tag_id "
               f"WHERE {_TODO_ALIVE_T} AND COALESCE(todos.completed_at, '') <> '' "
               "AND date(todos.completed_at) BETWEEN ? AND ? "
               "GROUP BY name ORDER BY c DESC")
    elif by == "priority":
        sql = ("SELECT CASE COALESCE(priority, 0) WHEN 1 THEN '低' WHEN 2 THEN '中' "
               "WHEN 3 THEN '高' ELSE '无优先级' END AS name, COUNT(*) AS c "
               "FROM todos WHERE (" + _TODO_ALIVE + ") "
               "AND COALESCE(completed_at, '') <> '' "
               "AND date(completed_at) BETWEEN ? AND ? "
               "GROUP BY name ORDER BY c DESC")
    else:
        sql = ("SELECT COALESCE(NULLIF(list_name, ''), '默认清单') AS name, "
               "COUNT(*) AS c FROM todos WHERE (" + _TODO_ALIVE + ") "
               "AND COALESCE(completed_at, '') <> '' "
               "AND date(completed_at) BETWEEN ? AND ? "
               "GROUP BY name ORDER BY c DESC")
    with db.connect() as conn:
        rows = conn.execute(sql, (start, end)).fetchall()
    return [{"name": r["name"], "count": r["c"] or 0} for r in rows]


def achievement_score() -> int:
    """成就值 = 完成任务 × 10 + 番茄 × 5。

    刻意做成**可核对**的加权累计：不像滴答那个不公开公式的「成就值」，
    这里每一项都能在界面上对得上（总已完成、总番茄）。两个分量都是累计
    计数，所以分数单调不减 —— 7 天趋势线只会往上走，不会哪天突然掉下去
    看着像出了 bug。
    """
    tot = todo_overview_totals()
    pomo = pomodoro_total_stats()
    return tot["done"] * 10 + pomo["count"] * 5


def todo_daily_stats(start: str, end: str) -> list[dict]:
    """区间内**按天**聚合的应完成数 / 完成数 / 完成率，缺日补 0、日期升序。

    口径与 ``todo_range_stats`` 完全一致，只是把「周期」换成「一天」：
    分母 = 当天到期 ∪ 当天完成的任务（只算到期会让「没设截止日、随手做完」的
    任务进不了分母，完成率虚高甚至破 100%），分子 = 当天完成。
    一条 SQL 用 UNION ALL 出「到期」和「完成」两种事件，再按天 COUNT(DISTINCT id)
    去掉「当天到期又当天完成」被算两次的行 —— 不是一天查一次。
    """
    from datetime import date as _date, timedelta as _td
    try:
        d0 = _date.fromisoformat(start)
        d1 = _date.fromisoformat(end)
    except ValueError:
        return []
    if d1 < d0:
        d0, d1 = d1, d0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT d, COUNT(DISTINCT id) AS total, "
            "COUNT(DISTINCT CASE WHEN act = 'done' THEN id END) AS done FROM ("
            "  SELECT date(due_date) AS d, id, 'due' AS act FROM todos"
            f"  WHERE ({_TODO_ALIVE}) AND COALESCE(due_date, '') <> ''"
            "    AND date(due_date) BETWEEN ? AND ?"
            "  UNION ALL"
            "  SELECT date(completed_at) AS d, id, 'done' AS act FROM todos"
            f"  WHERE ({_TODO_ALIVE}) AND COALESCE(completed_at, '') <> ''"
            "    AND date(completed_at) BETWEEN ? AND ?"
            ") GROUP BY d", (start, end, start, end)).fetchall()
    agg = {r["d"]: (r["total"] or 0, r["done"] or 0) for r in rows}
    out = []
    cur = d0
    while cur <= d1:
        ds = cur.isoformat()
        total, done = agg.get(ds, (0, 0))
        out.append({"date": ds, "total": total, "done": done,
                    "rate": (done * 100.0 / total) if total else 0.0})
        cur += _td(days=1)
    return out


def todo_overview_totals() -> dict:
    """统计页顶部汇总条：任务总数 / 已完成 / 清单数 / 使用天数。

    「使用天数」取的是有活动记录的自然日数（建过任务、完成过任务、专注过），
    不是安装天数 —— 库里没有安装时间，也不打算凭记忆编一个。
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN COALESCE(completed_at, '') <> '' THEN 1 ELSE 0 END) AS done "
            f"FROM todos WHERE {_TODO_ALIVE}").fetchone()
        lists = conn.execute(
            "SELECT COUNT(*) AS c FROM todo_lists "
            "WHERE COALESCE(archived, 0) = 0 AND COALESCE(kind, 'list') = 'list'"
        ).fetchone()["c"]
        days = conn.execute(
            "SELECT COUNT(DISTINCT d) AS c FROM ("
            "  SELECT date(created_at) AS d FROM todos"
            "    WHERE COALESCE(deleted_at, '') = ''"
            "  UNION SELECT date(completed_at) FROM todos"
            "    WHERE COALESCE(completed_at, '') <> ''"
            "  UNION SELECT date(started_at) FROM pomodoro WHERE completed = 1"
            ")").fetchone()["c"]
    return {"total": row["total"] or 0, "done": row["done"] or 0,
            "lists": lists or 0, "days": days or 0}


# ---------- 自定义过滤器 ----------
def filter_all() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM todo_filters ORDER BY sort_order ASC, id ASC").fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        try:
            d["cond"] = json.loads(d.get("conditions") or "{}")
        except (ValueError, TypeError):
            d["cond"] = {}
        out.append(d)
    return out


def filter_add(name: str, cond: dict, kind: str = "normal") -> int:
    with db.connect() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM todo_filters"
        ).fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO todo_filters(name, kind, conditions, sort_order) "
            "VALUES(?,?,?,?)",
            (name, kind, json.dumps(cond, ensure_ascii=False), max_order + 1))
        return cur.lastrowid


def filter_update(fid: int, **fields: Any) -> None:
    cond = fields.pop("cond", None)
    if cond is not None:
        fields["conditions"] = json.dumps(cond, ensure_ascii=False)
    allowed = {"name", "kind", "conditions", "sort_order"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    with db.connect() as conn:
        conn.execute(f"UPDATE todo_filters SET {cols} WHERE id = ?",
                     (*updates.values(), fid))


def filter_delete(fid: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM todo_filters WHERE id = ?", (fid,))


# ---------- 子任务管理 ----------
def subtask_list(todo_id: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM subtasks WHERE todo_id = ? "
            "ORDER BY sort_order ASC, id ASC", (todo_id,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def subtask_add(todo_id: int, title: str) -> int:
    with db.connect() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM subtasks "
            "WHERE todo_id = ?", (todo_id,)).fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO subtasks(todo_id, title, sort_order) VALUES(?,?,?)",
            (todo_id, title, max_order + 1))
        return cur.lastrowid


def subtask_update(sub_id: int, **fields: Any) -> None:
    allowed = {"title", "done", "sort_order"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    with db.connect() as conn:
        conn.execute(
            f"UPDATE subtasks SET {cols} WHERE id = ?",
            (*updates.values(), sub_id))


def subtask_delete(sub_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM subtasks WHERE id = ?", (sub_id,))


# ---------- 日历 / 重复任务 ----------
# 重复语义与滴答清单一致：系列从 due_date 起算，单个周期可以独立完成、
# 独立删除、独立改期（例外记在 todo_occ，未列出的周期按规则正常展开）。
REPEAT_OPTIONS = [
    ("", "不重复"),
    ("daily", "每天"),
    ("workday", "每个工作日"),
    ("weekly", "每周"),
    ("biweekly", "每两周"),
    ("monthly", "每月"),
    ("yearly", "每年"),
]
REPEAT_NAMES = dict(REPEAT_OPTIONS)


def _iso(d: Any) -> str:
    return d.isoformat() if isinstance(d, date) else (d or "")


def _parse_d(s: str) -> Optional[date]:
    try:
        return date.fromisoformat((s or "").strip())
    except ValueError:
        return None


def _repeat_hit(base: date, rule: str, d: date) -> bool:
    """d 是否落在以 base 为首个周期的重复系列上。"""
    if d < base:
        return False
    step = (d - base).days
    if rule == "daily":
        return True
    if rule == "workday":
        return d.weekday() < 5
    if rule == "weekly":
        return step % 7 == 0
    if rule == "biweekly":
        return step % 14 == 0
    # 31 号 / 2 月 29 日这类「该月没有这一天」的周期，落到当月最后一天，与滴答一致
    last = _calendar.monthrange(d.year, d.month)[1]
    if rule == "monthly":
        return d.day == min(base.day, last)
    if rule == "yearly":
        return d.month == base.month and d.day == min(base.day, last)
    return False


def _occ_map() -> dict[tuple[int, str], dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM todo_occ").fetchall()
    return {(r["todo_id"], r["occ_date"]): dict(r) for r in rows}


def _cal_row(t: dict, occ_s: str, ov: dict | None, tags: list[dict],
             subs: tuple[int, int]) -> dict:
    """一条任务在某个周期上的日历条目（date/time 已套用单周期例外）。"""
    recurring = bool((t.get("repeat") or "").strip())
    return {
        "id": t["id"],
        "occ": occ_s,                                   # 系列中的原始周期日
        "date": (ov["new_date"] if ov and ov["new_date"] else occ_s),
        "time": (ov["new_time"] if ov and ov["new_time"]
                 else (t.get("due_time") or "")),
        "duration": int(t.get("duration_min") or 0),
        "title": t["title"],
        "note": t.get("note") or "",
        "priority": int(t.get("priority") or 0),
        "list_name": t.get("list_name") or "收集箱",
        "kind": t.get("kind") or "task",
        "repeat": t.get("repeat") or "",
        "done": (bool(ov) and ov["status"] == "done") if recurring
                else bool(t.get("done")),
        "moved": bool(ov and ov["new_date"]),
        "tags": tags,
        "sub_done": subs[0],
        "sub_total": subs[1],
        "sort": int(t.get("sort_order") or 0),
    }


def cal_occurrences(start: str, end: str,
                    include_notes: bool = False) -> list[dict]:
    """把 [start, end] 闭区间内的任务（含重复展开）摊平成日历条目。

    日历本质是「任务的时间轴视图」，所以条目按**显示日期**过滤：
    被单周期改期挪出区间的不再出现，从区间外挪进来的会出现。
    """
    s, e = _parse_d(start), _parse_d(end)
    if not s or not e or e < s:
        return []
    todos = [t for t in todo_list(kind=None if include_notes else "task")
             if (t.get("due_date") or "").strip()]
    if not todos:
        return []

    occ = _occ_map()
    tag_map: dict[int, list[dict]] = {}
    sub_map: dict[int, tuple[int, int]] = {}
    with db.connect() as conn:
        for r in conn.execute(
                "SELECT tt.todo_id AS tid, t.name AS name, t.color AS color, "
                "t.hex AS hex FROM todo_tags tt JOIN tags t ON t.id = tt.tag_id "
                "ORDER BY t.id").fetchall():
            tag_map.setdefault(r["tid"], []).append(
                {"name": r["name"], "color": r["color"], "hex": r["hex"]})
        for r in conn.execute(
                "SELECT todo_id, COUNT(*) AS n, COALESCE(SUM(done), 0) AS dn "
                "FROM subtasks GROUP BY todo_id").fetchall():
            sub_map[r["todo_id"]] = (int(r["dn"]), int(r["n"]))

    def emit(t: dict, occ_s: str, ov: dict | None) -> None:
        if ov and ov["status"] == "skipped":
            return
        out.append(_cal_row(t, occ_s, ov, tag_map.get(t["id"], []),
                            sub_map.get(t["id"], (0, 0))))

    out: list[dict] = []
    emitted: set[tuple[int, str]] = set()
    by_id = {t["id"]: t for t in todos}
    for t in todos:
        base = _parse_d(t["due_date"])
        if not base:
            continue
        rule = (t.get("repeat") or "").strip()
        d = s
        while d <= e:
            hit = (d == base) if not rule else _repeat_hit(base, rule, d)
            occ_s = d.isoformat()
            ov = occ.get((t["id"], occ_s))
            # 改过期的周期不留在原位，交给下面的「移入」分支
            if hit and not (ov and ov["new_date"]):
                emit(t, occ_s, ov)
                emitted.add((t["id"], occ_s))
            d += timedelta(days=1)
    # 从区间外「仅此周期改期」进来的周期：区间扫描覆盖不到，单独按规则判定
    for (tid, occ_s), ov in occ.items():
        if (tid, occ_s) in emitted:
            continue
        t = by_id.get(tid)
        nd = _parse_d(ov["new_date"])
        src = _parse_d(occ_s)
        if not t or not nd or not src or not (s <= nd <= e):
            continue
        b = _parse_d(t["due_date"])
        rule = (t.get("repeat") or "").strip()
        if not b:
            continue
        hit = (src == b) if not rule else _repeat_hit(b, rule, src)
        if hit:
            emit(t, occ_s, ov)

    out.sort(key=lambda r: (r["date"], r["time"] or "99:99", r["sort"], r["id"]))
    return out


def cal_unscheduled(include_notes: bool = False) -> list[dict]:
    """未排期任务（安排任务抽屉的数据源）。"""
    rows = [t for t in todo_list(kind=None if include_notes else "task")
            if not (t.get("due_date") or "").strip() and not t.get("done")]
    for t in rows:
        t["tags"] = todo_tags(t["id"])
    return rows


def _upsert_occ(conn, todo_id: int, occ_s: str, **fields: Any) -> None:
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(k)
        vals.append(v)
    marks = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO todo_occ(todo_id, occ_date, {', '.join(cols)}) "
        f"VALUES(?, ?, {marks}) ON CONFLICT(todo_id, occ_date) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in cols),
        (todo_id, occ_s, *vals))


def occ_set_done(todo_id: int, occ_s: str, done: bool) -> None:
    """勾选某个周期。重复任务只影响这一周期，普通任务走 todos.done。"""
    t = todo_get(todo_id)
    if not (t.get("repeat") or "").strip():
        todo_update(todo_id, done=int(done),
                    completed_at=_iso(date.today()) if done else "")
        return
    with db.connect() as conn:
        _upsert_occ(conn, todo_id, occ_s, status="done" if done else "")


def occ_delete(todo_id: int, occ_s: str) -> None:
    """删除某个周期：重复任务=跳过这一周期，普通任务=进垃圾桶。"""
    t = todo_get(todo_id)
    if not (t.get("repeat") or "").strip():
        todo_delete(todo_id)
        return
    with db.connect() as conn:
        _upsert_occ(conn, todo_id, occ_s, status="skipped")


def occ_move(todo_id: int, occ_s: str, new_date: str,
             new_time: str | None = None) -> None:
    """仅此周期改期（重复任务写例外，普通任务直接改截止日）。"""
    t = todo_get(todo_id)
    if not (t.get("repeat") or "").strip():
        fields: dict[str, Any] = {"due_date": new_date}
        if new_time is not None:
            fields["due_time"] = new_time
        todo_update(todo_id, **fields)
        return
    fields = {"new_date": new_date}
    if new_time is not None:
        fields["new_time"] = new_time
    with db.connect() as conn:
        _upsert_occ(conn, todo_id, occ_s, **fields)


def _shift_iso(s: str, delta: int) -> str:
    d = _parse_d(s)
    return (d + timedelta(days=delta)).isoformat() if d else s


def series_shift(todo_id: int, from_occ: str, new_date: str) -> int:
    """所有未完成周期一起移动：整个系列平移，使被拖的那个周期落到 new_date。

    返回平移的天数（0 表示无需改动）。
    """
    t = todo_get(todo_id)
    base = _parse_d(t.get("due_date") or "")
    src = _parse_d(from_occ)
    dst = _parse_d(new_date)
    if not base or not src or not dst or dst == src:
        return 0
    delta = (dst - src).days
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM todo_occ WHERE todo_id = ?", (todo_id,)).fetchall()]
        # 先全删再写回：逐行 UPDATE 会因为「先改到的行撞上还没改的行」触发唯一键
        conn.execute("DELETE FROM todo_occ WHERE todo_id = ?", (todo_id,))
        merged: dict[str, dict] = {}
        for r in rows:
            occ_s, nd, nt, st = r["occ_date"], r["new_date"], r["new_time"], r["status"]
            if occ_s >= from_occ:
                occ_s = _shift_iso(occ_s, delta)
                nd = _shift_iso(nd, delta)
                if r["occ_date"] == from_occ:
                    # 被拖的这个周期随系列走，不再是「单周期例外」
                    nd = nt = ""
            prev = merged.get(occ_s)
            # 撞上时保留带例外状态的那条，覆盖信息更有价值
            if prev is None or st or nd or nt:
                merged[occ_s] = {"status": st, "new_date": nd, "new_time": nt}
        for occ_s, r in merged.items():
            conn.execute(
                "INSERT INTO todo_occ(todo_id, occ_date, status, new_date, new_time) "
                "VALUES(?,?,?,?,?)", (todo_id, occ_s, r["status"],
                                      r["new_date"], r["new_time"]))
        conn.execute("UPDATE todos SET due_date = ? WHERE id = ?",
                     (_shift_iso(t["due_date"], delta), todo_id))
    return delta


def todo_set_repeat(todo_id: int, rule: str) -> None:
    """设置重复规则；关掉重复时清掉例外，否则会留下无意义的单周期状态。"""
    todo_update(todo_id, repeat=rule)
    if not rule:
        with db.connect() as conn:
            conn.execute("DELETE FROM todo_occ WHERE todo_id = ?", (todo_id,))


# ---------- 番茄钟 ----------
def pomodoro_add(duration_min: int, task: str = "", completed: int = 1,
                 started_at: str | None = None, note: str = "") -> None:
    """写入一条专注记录。

    ``started_at`` 形如 ``2026-09-19 06:00:00``；传 None 时用数据库默认值（当前时刻）。
    补录弹窗会显式传入用户选的开始时间，所以这里必须可覆盖。
    """
    with db.connect() as conn:
        if started_at:
            conn.execute(
                "INSERT INTO pomodoro(started_at, duration_min, task, completed, note) "
                "VALUES(?,?,?,?,?)",
                (started_at, duration_min, task, completed, note),
            )
        else:
            conn.execute(
                "INSERT INTO pomodoro(duration_min, task, completed, note) VALUES(?,?,?,?)",
                (duration_min, task, completed, note),
            )


def pomodoro_today_count() -> int:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM pomodoro "
            "WHERE completed = 1 AND date(started_at) = date('now', 'localtime')"
        ).fetchone()
    return row["c"] if row else 0


def pomodoro_total_minutes_today() -> int:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(duration_min), 0) AS s FROM pomodoro "
            "WHERE completed = 1 AND date(started_at) = date('now', 'localtime')"
        ).fetchone()
    return row["s"] if row else 0


def pomodoro_daily_minutes(days: int = 7) -> list[dict]:
    """最近 days 天每日专注分钟数（含 0 填充）。"""
    from datetime import date, timedelta
    today = date.today()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT date(started_at) AS d, COALESCE(SUM(duration_min), 0) AS m "
            "FROM pomodoro WHERE completed = 1 "
            "AND date(started_at) >= date('now', 'localtime', ?) "
            "GROUP BY d",
            (f"-{days - 1} days",),
        ).fetchall()
    m = {r["d"]: r["m"] for r in rows}
    result = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        result.append({"date": ds, "minutes": m.get(ds, 0)})
    return result


def pomodoro_week_stats() -> dict:
    """最近 7 天专注：分钟数与番茄数（供 Mini 悬浮窗统计区）。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(duration_min), 0) AS m, COUNT(*) AS c "
            "FROM pomodoro WHERE completed = 1 "
            "AND date(started_at) >= date('now', 'localtime', '-6 days')"
        ).fetchone()
    return {"minutes": row["m"], "count": row["c"]}


def pomodoro_total_stats() -> dict:
    """累计专注：次数 / 总分钟 / 有记录天数 / 日均分钟。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(duration_min), 0) AS m, "
            "COUNT(DISTINCT date(started_at)) AS d "
            "FROM pomodoro WHERE completed = 1"
        ).fetchone()
    days = row["d"]
    # 无记录时 days 就是 0 —— 界面上「累计 0 天有记录」才是对的。
    # 不要为了防除零写成 max(days, 1)：那会让 0 条记录显示成「累计 1 天有记录」，
    # 和旁边同为 0 的「总番茄 / 总专注时长」自相矛盾。
    return {"count": row["c"], "minutes": row["m"], "days": days,
            "avg": (row["m"] / days) if days else 0.0}


def pomodoro_task_distribution(days: int = 30, limit: int = 5) -> list[dict]:
    """近 days 天按任务的专注时长分布 Top N。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT task, COALESCE(SUM(duration_min), 0) AS m, COUNT(*) AS c "
            "FROM pomodoro WHERE completed = 1 AND task != '' "
            "AND date(started_at) >= date('now', 'localtime', ?) "
            "GROUP BY task ORDER BY m DESC LIMIT ?",
            (f"-{days - 1} days", limit),
        ).fetchall()
    return [{"task": r["task"], "minutes": r["m"], "count": r["c"]} for r in rows]


def pomodoro_records(limit: int = 100) -> list[dict]:
    """最近的专注记录（开始时间 / 时长 / 任务 / 完成状态 / 想法），按开始时间倒序。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, started_at, duration_min, task, completed, note "
            "FROM pomodoro ORDER BY started_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def pomodoro_delete(record_id: int) -> None:
    """删除一条专注记录。"""
    with db.connect() as conn:
        conn.execute("DELETE FROM pomodoro WHERE id = ?", (record_id,))


def pomodoro_get(record_id: int) -> dict | None:
    """按 id 取一条专注记录。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, started_at, duration_min, task, completed, note "
            "FROM pomodoro WHERE id = ?", (record_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def pomodoro_update(record_id: int, **fields: Any) -> None:
    """更新专注记录（duration_min / task / started_at / completed / note）。"""
    allowed = {"duration_min", "task", "started_at", "completed", "note"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k} = ?" for k in sets)
    with db.connect() as conn:
        conn.execute(f"UPDATE pomodoro SET {cols} WHERE id = ?",
                     (*sets.values(), record_id))


# ---------- 专注统计（统计页用） ----------
def pomodoro_day_stat(date_str: str) -> dict:
    """某一天的专注分钟数与番茄数。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(duration_min), 0) AS m, COUNT(*) AS c "
            "FROM pomodoro WHERE completed = 1 AND date(started_at) = ?",
            (date_str,),
        ).fetchone()
    return {"minutes": row["m"] or 0, "count": row["c"] or 0}


def pomodoro_range_daily(start: str, end: str) -> list[dict]:
    """[start, end] 区间内每日专注分钟数（含 0 填充，按日期升序）。"""
    from datetime import date as _date, timedelta as _td
    try:
        d0 = _date.fromisoformat(start)
        d1 = _date.fromisoformat(end)
    except ValueError:
        return []
    if d1 < d0:
        d0, d1 = d1, d0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT date(started_at) AS d, COALESCE(SUM(duration_min), 0) AS m, "
            "COUNT(*) AS c FROM pomodoro WHERE completed = 1 "
            "AND date(started_at) BETWEEN ? AND ? GROUP BY d",
            (start, end),
        ).fetchall()
    agg = {r["d"]: (r["m"], r["c"]) for r in rows}
    out = []
    cur = d0
    while cur <= d1:
        ds = cur.isoformat()
        m, c = agg.get(ds, (0, 0))
        out.append({"date": ds, "minutes": m, "count": c})
        cur += _td(days=1)
    return out


def pomodoro_hourly_profile(start: str, end: str) -> list[int]:
    """[start, end] 区间内按「开始小时」聚合的专注分钟数，返回 24 个元素。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT CAST(strftime('%H', started_at) AS INTEGER) AS h, "
            "COALESCE(SUM(duration_min), 0) AS m FROM pomodoro "
            "WHERE completed = 1 AND date(started_at) BETWEEN ? AND ? "
            "GROUP BY h",
            (start, end),
        ).fetchall()
    out = [0] * 24
    for r in rows:
        h = r["h"]
        if h is not None and 0 <= h < 24:
            out[h] += r["m"] or 0
    return out


def pomodoro_records_range(start: str, end: str, limit: int = 200) -> list[dict]:
    """[start, end] 区间内的专注记录，按开始时间倒序。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, started_at, duration_min, task, completed FROM pomodoro "
            "WHERE date(started_at) BETWEEN ? AND ? "
            "ORDER BY started_at DESC, id DESC LIMIT ?",
            (start, end, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def pomodoro_task_distribution_range(start: str, end: str,
                                     limit: int = 8) -> list[dict]:
    """[start, end] 区间内按任务聚合的专注时长 Top N。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT task, COALESCE(SUM(duration_min), 0) AS m, COUNT(*) AS c "
            "FROM pomodoro WHERE completed = 1 AND date(started_at) BETWEEN ? AND ? "
            "GROUP BY task ORDER BY m DESC LIMIT ?",
            (start, end, limit),
        ).fetchall()
    return [{"task": (r["task"] or "自由专注"), "minutes": r["m"] or 0,
             "count": r["c"] or 0} for r in rows]


def pomodoro_first_date() -> str:
    """最早一条专注记录的日期（无记录返回今天）。"""
    from datetime import date as _date
    with db.connect() as conn:
        row = conn.execute(
            "SELECT MIN(date(started_at)) AS d FROM pomodoro WHERE completed = 1"
        ).fetchone()
    return (row["d"] if row and row["d"] else _date.today().isoformat())


# ---------- 体重管理 ----------
def weight_list() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM weight ORDER BY date ASC, id ASC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def weight_add(date: str, weight: float, body_fat: float | None = None) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO weight(date, weight, body_fat) VALUES(?,?,?)",
            (date, weight, body_fat if body_fat and body_fat > 0 else None),
        )


def weight_delete(weight_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM weight WHERE id = ?", (weight_id,))


def weight_update(weight_id: int, date: str, weight: float,
                  body_fat: float | None = None) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE weight SET date = ?, weight = ?, body_fat = ? WHERE id = ?",
            (date, weight, body_fat if body_fat and body_fat > 0 else None, weight_id),
        )


# ---------- 理财管理 ----------
def finance_list() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM finance ORDER BY date DESC, id DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def finance_add(date: str, ftype: str, category: str, amount: float, note: str = "",
                account: str = "", owner: str = "self", source: str = "") -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO finance(date, type, category, amount, note, account, "
            "owner, source) VALUES(?,?,?,?,?,?,?,?)",
            (date, ftype, category, amount, note, account, owner, source),
        )
        return cur.lastrowid


def finance_update(fid: int, date: str, ftype: str, category: str, amount: float,
                   note: str = "", account: str = "", owner: str = "self") -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE finance SET date=?, type=?, category=?, amount=?, note=?, "
            "account=?, owner=? WHERE id=?",
            (date, ftype, category, amount, note, account, owner, fid),
        )


def finance_delete(fid: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM finance WHERE id = ?", (fid,))


def finance_get(fid: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM finance WHERE id = ?", (fid,)).fetchone()
    return _row_to_dict(row)


def finance_categories_used() -> list[str]:
    """历史中使用过的分类（用于筛选下拉，按最近使用排序不合适，取字典序）。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM finance WHERE category <> ''").fetchall()
    return sorted(r["category"] for r in rows)


def finance_monthly_totals() -> list[dict]:
    """按月份聚合收支：[{'month': '2026-09', 'income': x, 'expense': y}]（升序）。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT substr(date, 1, 7) AS m, "
            "SUM(CASE WHEN type='income' THEN amount ELSE 0 END) AS income, "
            "SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) AS expense "
            "FROM finance GROUP BY m ORDER BY m").fetchall()
    return [_row_to_dict(r) for r in rows]


def finance_range_totals(start: str, end: str) -> dict:
    """闭区间 [start, end] 内的收入/支出合计。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE 0 END), 0) AS income, "
            "COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END), 0) AS expense "
            "FROM finance WHERE date >= ? AND date <= ?",
            (start, end),
        ).fetchone()
    return _row_to_dict(row)


def finance_summary() -> dict:
    with db.connect() as conn:
        income = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM finance WHERE type = 'income'"
        ).fetchone()["s"]
        expense = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM finance WHERE type = 'expense'"
        ).fetchone()["s"]
    return {"income": income, "expense": expense, "balance": income - expense}


def finance_daily_totals(days: int = 30) -> list[dict]:
    """最近 days 天每日收支（含 0 填充）。"""
    from datetime import date, timedelta
    today = date.today()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT date AS d, "
            "SUM(CASE WHEN type='income' THEN amount ELSE 0 END) AS income, "
            "SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) AS expense "
            "FROM finance WHERE date >= date('now', 'localtime', ?) "
            "GROUP BY date",
            (f"-{days - 1} days",),
        ).fetchall()
    m = {r["d"]: r for r in rows}
    result = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        row = m.get(ds)
        result.append({
            "date": ds,
            "income": row["income"] if row else 0,
            "expense": row["expense"] if row else 0,
        })
    return result


# ---------- 资产总览 ----------
ASSET_CHANNELS = ["微信", "支付宝", "银行卡", "现金", "其他"]
ASSET_OWNERS = [("self", "我"), ("partner", "伴侣")]


def asset_records() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM assets ORDER BY date ASC, id ASC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def asset_add(owner: str, channel: str, amount: float, date: str, note: str = "") -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO assets(owner, channel, amount, date, note) VALUES(?,?,?,?,?)",
            (owner, channel, amount, date, note),
        )


def asset_delete(asset_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))


def asset_latest_balances() -> dict:
    """返回 {('self','微信'): amount, ...}：每个 (owner, channel) 的最新一次快照。"""
    latest: dict[tuple, float] = {}
    for r in asset_records():  # 已按 date,id 升序，后者覆盖前者即为最新
        latest[(r["owner"], r["channel"])] = r["amount"]
    return latest


def asset_accounts() -> list[str]:
    """账户（渠道）列表：自定义设置 ∪ 历史记录中出现过的渠道，确保旧数据不丢。"""
    raw = db.get_setting("asset_accounts", "")
    names: list[str] = []
    if raw:
        try:
            names = [str(n).strip() for n in json.loads(raw) if str(n).strip()]
        except Exception:
            names = []
    if not names:
        names = list(ASSET_CHANNELS)
    for _owner, ch in asset_latest_balances():
        if ch and ch not in names:
            names.append(ch)
    return names


def set_asset_accounts(names: list[str]) -> None:
    db.set_setting("asset_accounts", json.dumps(names, ensure_ascii=False))


def asset_account_colors() -> dict:
    """账户自定义配色：{账户名: 颜色键}；空键表示用默认。"""
    raw = db.get_setting("asset_account_colors", "{}")
    try:
        data = json.loads(raw)
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except Exception:
        return {}


def set_asset_account_color(name: str, color: str) -> None:
    colors = asset_account_colors()
    if color:
        colors[name] = color
    else:
        colors.pop(name, None)
    db.set_setting("asset_account_colors", json.dumps(colors, ensure_ascii=False))


def asset_balance(owner: str, account: str) -> float | None:
    """账户最新余额；从未记录过返回 None（区别于余额为 0）。"""
    val: float | None = None
    for r in asset_records():
        if r["owner"] == owner and r["channel"] == account:
            val = float(r["amount"])
    return val


def asset_set_balance(owner: str, account: str, amount: float, date: str,
                      note: str = "") -> int:
    """写入一条余额快照（asset_add 的语义化别名）。"""
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO assets(owner, channel, amount, date, note) VALUES(?,?,?,?,?)",
            (owner, account, amount, date, note),
        )
        return cur.lastrowid


def asset_rename_channel(old: str, new: str) -> int:
    """重命名账户：同步历史余额记录的渠道名，返回受影响行数。"""
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE assets SET channel = ? WHERE channel = ?", (new, old))
        return cur.rowcount


def asset_records_with_delta() -> list[dict]:
    """余额快照流：每条附带 delta（本次相对同账户上一次的变动）。

    首条 delta 为 None（表示"初始余额"）。按日期倒序返回以便列表展示。
    """
    recs = sorted(asset_records(), key=lambda r: (r["date"], r["id"]))
    prev: dict[tuple, float] = {}
    out: list[dict] = []
    for r in recs:
        key = (r["owner"], r["channel"])
        amt = float(r["amount"])
        item = dict(r)
        item["delta"] = None if key not in prev else amt - prev[key]
        out.append(item)
        prev[key] = amt
    out.reverse()
    return out


def asset_summary() -> dict:
    """当前总资产：我 / 伴侣 / 合计，以及各渠道分项。"""
    latest = asset_latest_balances()
    channels = asset_accounts()
    def by_channel(owner: str) -> dict:
        return {c: latest.get((owner, c), 0.0) for c in channels}
    self_c = by_channel("self")
    partner_c = by_channel("partner")
    self_total = sum(self_c.values())
    partner_total = sum(partner_c.values())
    return {
        "self": self_total,
        "partner": partner_total,
        "combined": self_total + partner_total,
        "self_by_channel": self_c,
        "partner_by_channel": partner_c,
    }


def asset_trend(owner_filter: str) -> dict:
    """渠道趋势：{dates, series:{channel:[value|None]}}，值做前向填充。

    owner_filter: 'self' / 'partner' / 'combined'。
    """
    channels = asset_accounts()
    owners = ("self", "partner") if owner_filter == "combined" else (owner_filter,)
    recs = [r for r in asset_records() if r["owner"] in owners]
    dates = sorted({r["date"] for r in recs})
    if not dates:
        return {"dates": [], "series": {c: [] for c in channels}}

    bal = {(o, c): None for o in owners for c in channels}
    idx = 0
    series: dict[str, list] = {c: [] for c in channels}
    for d in dates:
        while idx < len(recs) and recs[idx]["date"] == d:
            r = recs[idx]
            if r["channel"] in channels:
                bal[(r["owner"], r["channel"])] = r["amount"]
            idx += 1
        for c in channels:
            vals = [bal[(o, c)] for o in owners if bal[(o, c)] is not None]
            series[c].append(sum(vals) if vals else None)
    return {"dates": dates, "series": series}


def asset_compare_trend() -> dict:
    """两人总资产对比：{dates, self:[...], partner:[...]}。"""
    channels = asset_accounts()
    recs = asset_records()
    dates = sorted({r["date"] for r in recs})
    if not dates:
        return {"dates": [], "self": [], "partner": []}

    bal_self = {c: None for c in channels}
    bal_partner = {c: None for c in channels}
    idx = 0
    self_vals: list = []
    partner_vals: list = []
    for d in dates:
        while idx < len(recs) and recs[idx]["date"] == d:
            r = recs[idx]
            if r["channel"] in channels:
                target = bal_self if r["owner"] == "self" else bal_partner
                target[r["channel"]] = r["amount"]
            idx += 1
        sv = [v for v in bal_self.values() if v is not None]
        pv = [v for v in bal_partner.values() if v is not None]
        self_vals.append(sum(sv) if sv else None)
        partner_vals.append(sum(pv) if pv else None)
    return {"dates": dates, "self": self_vals, "partner": partner_vals}


# ---------- 科研管理（研究课题） ----------
# 六步科研路线 + 已收尾。文案就是给用户看的「现在该干什么」。
ROUTE_STEPS = [
    ("s1", "广泛阅读",
     "读前沿论文、感兴趣的与相关方向的论文。先用 Arxiver 每天扫，值得跟的挂到本课题下。"),
    ("s2", "定缺口与任务",
     "收窄到一个小的缺口、定一个小任务，并找到这个任务能直接跑的 benchmark。"),
    ("s3", "baseline 评测",
     "找相关模型作为 baseline 做评测，把指标口径和复现流程先跑通。"),
    ("s4", "数据 pipeline",
     "搭建数据收集与清理的 pipeline，必要时自建数据集。"),
    ("s5", "模型创新",
     "针对数据对模型框架做修改和创新。这一步多跟老师交流 —— "
     "老师一般会有科研上的直觉，能判断出某些设计是否合理。"),
    ("s6", "写作与投稿",
     "benchmark 刷高后开始计划撰写论文、投递 A 会。"),
    ("done", "已收尾", "已投稿或已结题，不再推进。"),
]
ROUTE_KEYS = [k for k, _, _ in ROUTE_STEPS]
ROUTE_ACTIVE = ROUTE_KEYS[:-1]          # 不含「已收尾」
DDL_LIST_NAME = "科研DDL"               # 同步到日历的待办所属清单


def route_index(key: str) -> int:
    """路线步骤下标；未知值按第 1 步。"""
    return ROUTE_KEYS.index(key) if key in ROUTE_KEYS else 0


def route_label(key: str) -> str:
    return ROUTE_STEPS[route_index(key)][1]


def route_desc(key: str) -> str:
    return ROUTE_STEPS[route_index(key)][2]


def research_list() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM research ORDER BY "
            "CASE WHEN status = 'done' THEN 1 ELSE 0 END, "
            "priority DESC, "
            "CASE status WHEN 's1' THEN 1 WHEN 's2' THEN 2 WHEN 's3' THEN 3 "
            "WHEN 's4' THEN 4 WHEN 's5' THEN 5 WHEN 's6' THEN 6 ELSE 7 END, "
            "id DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def research_get(rid: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM research WHERE id = ?", (rid,)).fetchone()
    return _row_to_dict(row)


def research_add(title: str, field: str = "", status: str = "s1",
                 priority: int = 1, notes: str = "", due_date: str = "",
                 focus_keywords: str = "", venue: str = "",
                 venue_deadline: str = "", role: str = "") -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO research(title, field, status, priority, notes, due_date, "
            "focus_keywords, venue, venue_deadline, role) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (title, field, status, priority, notes, due_date, focus_keywords,
             venue, venue_deadline, role),
        )
        return cur.lastrowid


def research_update(rid: int, **fields: Any) -> None:
    allowed = {"title", "field", "status", "priority", "notes", "due_date",
               "focus_keywords", "venue", "venue_deadline", "role"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in updates)
    with db.connect() as conn:
        conn.execute(f"UPDATE research SET {cols} WHERE id = ?", (*updates.values(), rid))


def research_delete(rid: int) -> None:
    """删课题：连带清掉论文关联和它同步出去的 DDL 待办（Arxiver 那边不动）。"""
    with db.connect() as conn:
        todos = [r["todo_id"] for r in conn.execute(
            "SELECT todo_id FROM research_milestones WHERE project_id = ? "
            "AND todo_id != 0", (rid,)).fetchall()]
        conn.execute("DELETE FROM research WHERE id = ?", (rid,))
        conn.execute("DELETE FROM research_papers WHERE project_id = ?", (rid,))
        conn.execute("DELETE FROM research_milestones WHERE project_id = ?", (rid,))
    for tid in todos:
        todo_delete(tid, hard=True)


# ---------- 课题 DDL（自定义里程碑） ----------
def milestone_list(project_id: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM research_milestones WHERE project_id = ? "
            "ORDER BY done ASC, CASE WHEN due_date = '' THEN 1 ELSE 0 END, "
            "due_date ASC, id ASC", (project_id,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def milestone_get(mid: int) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM research_milestones WHERE id = ?", (mid,)).fetchone()
    return _row_to_dict(row)


def milestone_add(project_id: int, title: str, due_date: str = "",
                  note: str = "", sync: bool = False) -> int:
    with db.connect() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM research_milestones "
            "WHERE project_id = ?", (project_id,)).fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO research_milestones(project_id, title, due_date, note, "
            "sort_order) VALUES(?,?,?,?,?)",
            (project_id, title, due_date, note, max_order + 1))
        mid = cur.lastrowid
    if sync:
        sync_milestone_todo(mid)
    return mid


def milestone_update(mid: int, **fields: Any) -> None:
    allowed = {"title", "due_date", "note", "done", "sort_order", "todo_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    with db.connect() as conn:
        conn.execute(f"UPDATE research_milestones SET {cols} WHERE id = ?",
                     (*updates.values(), mid))


def milestone_delete(mid: int) -> None:
    ms = milestone_get(mid)
    if not ms:
        return
    with db.connect() as conn:
        conn.execute("DELETE FROM research_milestones WHERE id = ?", (mid,))
    if ms.get("todo_id"):
        todo_delete(ms["todo_id"], hard=True)


def milestone_counts() -> dict[int, dict]:
    """{课题 id: {'total': n, 'done': m, 'open': k, 'overdue': x}}。"""
    today = date.today().isoformat()
    out: dict[int, dict] = {}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT project_id, COUNT(*) AS total, "
            "SUM(done) AS done FROM research_milestones GROUP BY project_id"
        ).fetchall()
        for r in rows:
            out[r["project_id"]] = {"total": r["total"], "done": r["done"] or 0,
                                    "open": r["total"] - (r["done"] or 0),
                                    "overdue": 0}
        over = conn.execute(
            "SELECT project_id, COUNT(*) AS c FROM research_milestones "
            "WHERE done = 0 AND due_date != '' AND due_date < ? GROUP BY project_id",
            (today,)).fetchall()
        for r in over:
            if r["project_id"] in out:
                out[r["project_id"]]["overdue"] = r["c"]
    return out


def milestone_open_all(limit: int = 30) -> list[dict]:
    """跨课题的全部未完成 DDL，按日期升序（看板「接下来要交的」卡）。

    已收尾课题下面的未完成 DDL 不算：结题了就不该再拿逾期来催。
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT m.*, r.title AS project_title, r.venue AS project_venue "
            "FROM research_milestones m JOIN research r ON r.id = m.project_id "
            "WHERE m.done = 0 AND r.status != 'done' "
            "ORDER BY CASE WHEN m.due_date = '' THEN 1 ELSE 0 END, "
            "m.due_date ASC LIMIT ?", (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def _ensure_ddl_list() -> None:
    """同步到日历需要一个固定清单，第一次用时建出来。"""
    if any(l["name"] == DDL_LIST_NAME for l in list_all()):
        return
    list_add(DDL_LIST_NAME, icon="⏰")


def brief_title(title: str, cap: int = 14) -> str:
    """课题短名：只取冒号/破折号前那截。长标题在待办和浓缩行里会糊成一片。"""
    t = (title or "").strip()
    for sep in ("：", ":", " — ", "（"):
        i = t.find(sep)
        if i > 0:
            t = t[:i].strip()
    return t[:cap] + ("…" if len(t) > cap else "")


def sync_milestone_todo(mid: int) -> None:
    """把一条 DDL 落成/更新成一条待办（日历页就是待办的时间轴视图）。

    没填日期的 DDL 不生成待办 —— 日历上没法摆一条没有日期的事。
    """
    ms = milestone_get(mid)
    if not ms:
        return
    proj = research_get(ms["project_id"])
    title = f"{brief_title(proj.get('title', ''))} · {ms['title']}"
    tid = ms.get("todo_id") or 0
    if not ms["due_date"]:
        if tid:
            todo_delete(tid, hard=True)
            milestone_update(mid, todo_id=0)
        return
    if tid:
        todo_update(tid, title=title, due_date=ms["due_date"],
                    note=ms.get("note") or "", done=int(ms.get("done") or 0),
                    list_name=DDL_LIST_NAME)
        return
    _ensure_ddl_list()
    new_id = todo_add(title=title, note=ms.get("note") or "",
                      due_date=ms["due_date"], list_name=DDL_LIST_NAME)
    milestone_update(mid, todo_id=new_id)


def unsync_milestone_todo(mid: int) -> None:
    ms = milestone_get(mid)
    if not ms or not ms.get("todo_id"):
        return
    todo_delete(ms["todo_id"], hard=True)
    milestone_update(mid, todo_id=0)


def _now() -> str:
    with db.connect() as conn:
        return conn.execute("SELECT datetime('now', 'localtime') AS n").fetchone()["n"]



# ---------- Arxiver 论文库（只读接入） ----------
# Arxiver 自己维护 ~/.arxiver/library.db（论文元数据 + 下载归档）。本应用一律用
# SQLite 的 mode=ro 只读连接访问：既不写它的库，也不参与它的 WAL 写锁，
# 两个应用各管各的数据。取不到（没装/没抓到）就安静降级成「未连接」。
ARXIVER_FIELDS = ("arxiv_id, title, title_zh, authors, published, categories, "
                  "abs_url, pdf_url, local_path, tags, status, upvotes, citations")

RESEARCH_KEYWORDS_DEFAULT = "论文,文献,精读,读paper,实验,跑实验,复现,训练,写作,科研"


def arxiver_db_path() -> str:
    home = os.environ.get("ARXIVER_HOME") or os.path.join(
        os.path.expanduser("~"), ".arxiver")
    return os.path.join(home, "library.db")


def arxiver_available() -> bool:
    return os.path.exists(arxiver_db_path())


def _arxiver_connect():
    path = arxiver_db_path()
    if not os.path.exists(path):
        return None
    try:
        uri = "file:" + path.replace("\\", "/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 3000")
        return conn
    except sqlite3.Error:
        return None


def arxiver_search(q: str = "", limit: int = 60, only_local: bool = False) -> list[dict]:
    """按标题 / 中文标题 / 作者 / 标签 / arXiv ID 搜 Arxiver 论文库。"""
    conn = _arxiver_connect()
    if conn is None:
        return []
    try:
        sql = f"SELECT {ARXIVER_FIELDS} FROM papers"
        cond: list[str] = []
        args: list[Any] = []
        # 按空格切词再 AND：整句 LIKE 的话「agent planning」只能匹配到这个原短语，
        # 标题里分别含这两个词的全部漏掉。
        for tok in q.split():
            like = f"%{tok}%"
            cond.append("(title LIKE ? OR title_zh LIKE ? OR authors LIKE ? "
                        "OR tags LIKE ? OR arxiv_id LIKE ?)")
            args += [like] * 5
        if only_local:
            cond.append("local_path IS NOT NULL AND local_path <> ''")
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY fetched_at DESC LIMIT ?"
        args.append(int(limit))
        return [_row_to_dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def arxiver_get(arxiv_ids: list[str]) -> dict[str, dict]:
    """批量按 arxiv_id 取元数据：{arxiv_id: row}。库不在返回空 dict。"""
    ids = [i for i in arxiv_ids if i]
    if not ids:
        return {}
    conn = _arxiver_connect()
    if conn is None:
        return {}
    try:
        marks = ", ".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT {ARXIVER_FIELDS} FROM papers WHERE arxiv_id IN ({marks})",
            ids).fetchall()
        return {r["arxiv_id"]: _row_to_dict(r) for r in rows}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


# ---------- 课题 ↔ Arxiver 论文关联 ----------
def research_paper_add(project_id: int, arxiv_id: str, title: str = "") -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO research_papers(project_id, arxiv_id, title) "
            "VALUES(?,?,?)", (project_id, arxiv_id, title))


def research_paper_delete(link_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM research_papers WHERE id = ?", (link_id,))


def research_paper_list(project_id: int) -> list[dict]:
    """某课题关联的论文，元数据实时取自 Arxiver 库。

    Arxiver 里查不到（用户在那边删了、或还没抓到）时回退到关联时存的标题快照，
    并标 ``missing=True`` 让界面提示，而不是让整行凭空消失。
    """
    with db.connect() as conn:
        links = [_row_to_dict(r) for r in conn.execute(
            "SELECT * FROM research_papers WHERE project_id = ? "
            "ORDER BY id DESC", (project_id,)).fetchall()]
    if not links:
        return []
    meta = arxiver_get([l["arxiv_id"] for l in links])
    out = []
    for l in links:
        m = meta.get(l["arxiv_id"]) or {}
        item = dict(m)
        item.update({"id": l["id"], "project_id": l["project_id"],
                     "arxiv_id": l["arxiv_id"], "added_at": l["added_at"],
                     "title": m.get("title") or l["title"], "missing": not m})
        out.append(item)
    return out


def research_paper_counts() -> dict[int, int]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT project_id, COUNT(*) AS c FROM research_papers GROUP BY project_id"
        ).fetchall()
    return {r["project_id"]: r["c"] for r in rows}


def research_status_counts() -> dict[str, int]:
    """各路线步骤的课题数量：{'s1': 2, 's5': 1, 'done': 3, ...}。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM research GROUP BY status"
        ).fetchall()
    return {r["status"]: r["c"] for r in rows}


# ---------- 科研专注投入（与番茄钟联动） ----------
def research_keywords() -> list[str]:
    """全局「算科研」的任务关键词（逗号分隔，存 settings）。"""
    raw = db.get_setting("research_focus_keywords", RESEARCH_KEYWORDS_DEFAULT)
    return [k.strip() for k in raw.split(",") if k.strip()]


def set_research_keywords(keywords: list[str]) -> None:
    db.set_setting("research_focus_keywords", ",".join(keywords))


def _focus_where(keywords: list[str]) -> tuple[str, list[Any]]:
    """把关键词拼成 ``AND (task LIKE ? OR ...)``；空表表示不过滤。"""
    if not keywords:
        return "", []
    conds = " OR ".join(["task LIKE ?"] * len(keywords))
    return f" AND ({conds})", [f"%{k}%" for k in keywords]


def research_focus_daily(days: int = 42,
                         keywords: Optional[list[str]] = None) -> list[dict]:
    """近 days 天每天投入在科研上的专注分钟数（含 0 填充，日期升序）。"""
    kw = research_keywords() if keywords is None else keywords
    where, args = _focus_where(kw)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT date(started_at) AS d, COALESCE(SUM(duration_min), 0) AS m, "
            "COUNT(*) AS c FROM pomodoro WHERE completed = 1 "
            "AND date(started_at) >= date('now', 'localtime', ?)"
            + where + " GROUP BY d",
            (f"-{days - 1} days", *args),
        ).fetchall()
    agg = {r["d"]: (r["m"], r["c"]) for r in rows}
    today = date.today()
    out = []
    for i in range(days - 1, -1, -1):
        ds = (today - timedelta(days=i)).isoformat()
        m, c = agg.get(ds, (0, 0))
        out.append({"date": ds, "minutes": m, "count": c})
    return out


def research_focus_weekly(weeks: int = 6,
                          keywords: Optional[list[str]] = None) -> list[dict]:
    """近 weeks 周每周科研投入（周一为一周起点，含本周）。"""
    daily = research_focus_daily(weeks * 7, keywords)
    out = []
    for i in range(weeks):
        chunk = daily[i * 7:(i + 1) * 7]
        if not chunk:
            continue
        out.append({
            "label": f"{chunk[0]['date'][5:7]}/{chunk[0]['date'][8:10]}",
            "start": chunk[0]["date"], "end": chunk[-1]["date"],
            "minutes": sum(d["minutes"] for d in chunk),
            "count": sum(d["count"] for d in chunk),
        })
    return out


def research_focus_summary(keywords: Optional[list[str]] = None) -> dict:
    """本周 / 上周 / 近 30 天科研投入分钟数（看板 KPI 与环比用）。"""
    kw = research_keywords() if keywords is None else keywords
    daily = research_focus_daily(21, kw)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())

    def total(start: date, days: int) -> int:
        want = {(start + timedelta(days=i)).isoformat() for i in range(days)}
        return sum(d["minutes"] for d in daily if d["date"] in want)

    return {
        "week": total(week_start, (today - week_start).days + 1),
        "prev_week": total(week_start - timedelta(days=7), 7),
        "month": sum(d["minutes"] for d in daily[-30:]) if daily else 0,
        "count_week": sum(d["count"] for d in daily
                          if d["date"] >= week_start.isoformat()),
    }


def research_focus_top_tasks(days: int = 30, limit: int = 6) -> list[dict]:
    """近 days 天科研类专注的任务分布 Top N（看板的「时间花在哪」卡）。"""
    where, args = _focus_where(research_keywords())
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(NULLIF(task, ''), '未命名专注') AS t, "
            "COALESCE(SUM(duration_min), 0) AS m, COUNT(*) AS c "
            "FROM pomodoro WHERE completed = 1 "
            "AND date(started_at) >= date('now', 'localtime', ?)"
            + where + " GROUP BY t ORDER BY m DESC LIMIT ?",
            (f"-{days - 1} days", *args, limit),
        ).fetchall()
    return [{"task": r["t"], "minutes": r["m"], "count": r["c"]} for r in rows]


def project_focus_minutes(project: dict, days: int = 30) -> int:
    """单课题的科研投入：命中「自定义关键词 + 研究方向」的专注分钟数。

    没配关键词时不按课题标题模糊匹配 —— 番茄任务名很少和课题全名一致，
    猜着匹配会把别的课题的时长算进来，宁可显示 0 并提示去补关键词。
    """
    kw = [k.strip() for k in (project.get("focus_keywords") or "").split(",")
          if k.strip()]
    if project.get("field"):
        kw.append(project["field"].strip())
    if not kw:
        return 0
    where, args = _focus_where(kw)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(duration_min), 0) AS m FROM pomodoro "
            "WHERE completed = 1 AND date(started_at) >= date('now', 'localtime', ?)"
            + where,
            (f"-{days - 1} days", *args),
        ).fetchone()
    return row["m"] or 0

# ---------- 记账自定义分类 / 周期账单 ----------
def get_custom_categories(kind: str) -> list[str]:
    raw = db.get_setting(f"custom_cats_{kind}", "[]")
    try:
        return [c for c in json.loads(raw) if c.strip()]
    except Exception:
        return []


def set_custom_categories(kind: str, cats: list[str]) -> None:
    db.set_setting(f"custom_cats_{kind}", json.dumps(cats, ensure_ascii=False))


def custom_category_styles() -> dict:
    """自定义分类的图标/配色：{分类名: {"icon": str, "color": str}}。"""
    raw = db.get_setting("custom_category_styles", "{}")
    try:
        data = json.loads(raw)
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        return {}


def set_custom_category_style(name: str, icon: str, color: str) -> None:
    styles = custom_category_styles()
    if icon or color:
        styles[name] = {"icon": icon or "", "color": color or ""}
    else:
        styles.pop(name, None)
    db.set_setting("custom_category_styles",
                   json.dumps(styles, ensure_ascii=False))


def get_recurring_bills() -> list[dict]:
    raw = db.get_setting("recurring_bills", "[]")
    try:
        return json.loads(raw)
    except Exception:
        return []


def set_recurring_bills(bills: list[dict]) -> None:
    db.set_setting("recurring_bills", json.dumps(bills, ensure_ascii=False))


# ---------- 习惯打卡 ----------
def habit_list(archived: Optional[int] = None) -> list[dict]:
    q = "SELECT * FROM habits"
    if archived is not None:
        q += f" WHERE archived = {int(archived)}"
    q += " ORDER BY sort_order ASC, id ASC"
    with db.connect() as conn:
        rows = conn.execute(q).fetchall()
    return [_row_to_dict(r) for r in rows]


def habit_get(habit_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
    return _row_to_dict(row)


# 习惯数据的版本号。习惯页和待办页底部的「今日打卡」各自缓存了一份界面，
# 在任意一边打了卡，切到另一边时看到的还是旧勾选 —— 两边都拿这个数判过期。
_habit_rev = 0


def habit_rev() -> int:
    return _habit_rev


def bump_habit_rev() -> None:
    global _habit_rev
    _habit_rev += 1


def habit_add(name: str, icon: str = "😊", color: str = "green",
              freq_type: str = "daily", freq_days: str = "",
              goal_type: str = "check", goal_per_day: int = 1,
              goal_auto: int = 1, goal_each: int = 1,
              start_date: str = "", target_days: int = 0,
              group_name: str = "其他", reminder: str = "",
              auto_log: int = 0) -> int:
    with db.connect() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM habits").fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO habits(name, icon, color, freq_type, freq_days, goal_type, "
            "goal_per_day, goal_auto, goal_each, start_date, target_days, group_name, "
            "reminder, auto_log, sort_order) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, icon, color, freq_type, freq_days, goal_type, goal_per_day,
             goal_auto, goal_each, start_date, target_days, group_name,
             reminder, auto_log, max_order + 1))
        hid = cur.lastrowid
    bump_habit_rev()
    return hid


_HABIT_FIELDS = {"name", "icon", "color", "freq_type", "freq_days", "goal_type",
                 "goal_per_day", "goal_auto", "goal_each", "start_date",
                 "target_days", "group_name", "reminder", "auto_log", "archived",
                 "sort_order"}


def habit_update(habit_id: int, **fields: Any) -> None:
    updates = {k: v for k, v in fields.items() if k in _HABIT_FIELDS}
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    with db.connect() as conn:
        conn.execute(f"UPDATE habits SET {cols} WHERE id = ?",
                     (*updates.values(), habit_id))
    bump_habit_rev()


def habit_list_grouped(archived: Optional[int] = None
                       ) -> list[tuple[str, list[dict]]]:
    """按所属分组分好返回 ``[(组名, [习惯…])]``；组间按组内最小 sort_order 排。

    ``group_name`` 以前只写进库、界面上从不参与排布 —— 表单里能选分组、还能
    「添加分组」，建完什么也不会发生。这里给它一个真正生效的地方。
    """
    buckets: dict[str, list[dict]] = {}
    for h in habit_list(archived=archived):
        buckets.setdefault(str(h.get("group_name") or "其他"), []).append(h)
    return sorted(buckets.items(),
                  key=lambda kv: min(x["sort_order"] for x in kv[1]))


def habit_move(habit_id: int, delta: int) -> bool:
    """在同一分组内上移 / 下移一位（交换 sort_order）。

    列表一直是按 sort_order 排的，但除了新建时没人写过这个字段，
    所以顺序一旦定了就再也改不了。到组边界时返回 False，调用方据此决定刷不刷新。
    """
    op, order = ("<", "DESC") if delta < 0 else (">", "ASC")
    with db.connect() as conn:
        me = conn.execute(
            "SELECT id, archived, sort_order, "
            "IFNULL(group_name, '其他') AS g FROM habits WHERE id = ?",
            (habit_id,)).fetchone()
        if not me:
            return False
        nb = conn.execute(
            f"SELECT id, sort_order FROM habits "
            f"WHERE archived = ? AND IFNULL(group_name, '其他') = ? "
            f"AND sort_order {op} ? "
            f"ORDER BY sort_order {order} LIMIT 1",
            (me["archived"], me["g"], me["sort_order"])).fetchone()
        if not nb:
            return False
        conn.execute("UPDATE habits SET sort_order = ? WHERE id = ?",
                     (nb["sort_order"], me["id"]))
        conn.execute("UPDATE habits SET sort_order = ? WHERE id = ?",
                     (me["sort_order"], nb["id"]))
    bump_habit_rev()
    return True


def habit_delete(habit_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
        conn.execute("DELETE FROM habit_checks WHERE habit_id = ?", (habit_id,))
    bump_habit_rev()


def habit_checks_map(habit_id: int) -> dict:
    """{date: {'count': n, 'note': str}}，某习惯全部打卡记录。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT date, count, note FROM habit_checks WHERE habit_id = ?",
            (habit_id,)).fetchall()
    return {r["date"]: {"count": r["count"], "note": r["note"]} for r in rows}


def habit_checks_maps(habit_ids: list[int]) -> dict[int, dict]:
    """一次查回多个习惯的打卡记录，``{habit_id: {date: {'count','note'}}}``。

    列表页原先每个习惯各开一次连接，60 个习惯就是 60 次 connect/close + 120 次
    PRAGMA（实测占整次 reload 的一半以上）；换成一条 IN 查询后连接数与习惯数无关。
    """
    out: dict[int, dict] = {hid: {} for hid in habit_ids}
    if not habit_ids:
        return out
    marks = ",".join("?" * len(habit_ids))
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT habit_id, date, count, note FROM habit_checks "
            f"WHERE habit_id IN ({marks})",
            tuple(habit_ids)).fetchall()
    for r in rows:
        out.setdefault(r["habit_id"], {})[r["date"]] = {
            "count": r["count"], "note": r["note"]}
    return out


def habit_set_count(habit_id: int, date: str, count: int) -> None:
    """写入某日打卡次数；次数 <=0 时删除该日记录。"""
    with db.connect() as conn:
        if count <= 0:
            conn.execute("DELETE FROM habit_checks WHERE habit_id = ? AND date = ?",
                         (habit_id, date))
        else:
            conn.execute(
                "INSERT INTO habit_checks(habit_id, date, count) VALUES(?,?,?) "
                "ON CONFLICT(habit_id, date) DO UPDATE SET count = excluded.count",
                (habit_id, date, count))
    bump_habit_rev()


def habit_set_note(habit_id: int, date: str, note: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO habit_checks(habit_id, date, count, note) VALUES(?,?,1,?) "
            "ON CONFLICT(habit_id, date) DO UPDATE SET note = excluded.note",
            (habit_id, date, note))
    bump_habit_rev()


def habit_groups() -> list[str]:
    """习惯分组：内置四项 + 用户自定义（settings.habit_groups）。"""
    builtin = ["上午", "下午", "晚上", "其他"]
    raw = db.get_setting("habit_groups", "[]")
    try:
        extra = [g for g in json.loads(raw) if isinstance(g, str) and g.strip()]
    except Exception:
        extra = []
    out = list(builtin)
    for g in extra:
        if g not in out:
            out.append(g)
    return out


def habit_add_group(name: str) -> None:
    raw = db.get_setting("habit_groups", "[]")
    try:
        extra = json.loads(raw)
    except Exception:
        extra = []
    if name and name not in extra:
        extra.append(name)
        db.set_setting("habit_groups", json.dumps(extra, ensure_ascii=False))
        bump_habit_rev()


def habit_due_on(habit: dict, qdate) -> bool:
    """qdate(QDate) 是否为该习惯的计划打卡日（考虑开始日期与频率）。"""
    from PySide6.QtCore import QDate
    start = QDate.fromString(habit.get("start_date") or "", "yyyy-MM-dd")
    if start.isValid() and qdate < start:
        return False
    if habit.get("freq_type", "daily") == "weekly":
        days = {int(x) for x in (habit.get("freq_days") or "").split(",") if x.strip()}
        return qdate.dayOfWeek() in days
    return True


def habit_goal_count(habit: dict) -> int:
    """当日打卡完成所需次数。"""
    if habit.get("goal_type", "check") == "amount":
        return max(int(habit.get("goal_per_day") or 1), 1) * \
            max(int(habit.get("goal_each") or 1), 1)
    return 1


def habit_is_done(habit: dict, checks: dict, date_str: str) -> bool:
    rec = checks.get(date_str)
    return bool(rec) and rec["count"] >= habit_goal_count(habit)


def habit_stats(habit: dict, checks: dict) -> dict:
    """统计：月打卡天数 / 总打卡天数 / 月完成率 / 当前连续（QDate 计算）。"""
    from PySide6.QtCore import QDate
    from datetime import date as _date
    today = QDate.currentDate()
    month_start = QDate(today.year(), today.month(), 1)

    def done_on(qd) -> bool:
        return habit_is_done(habit, checks, qd.toString("yyyy-MM-dd"))

    # 月打卡 / 总打卡
    month_days = 0
    d = month_start
    while d <= today:
        if done_on(d):
            month_days += 1
        d = d.addDays(1)
    total_days = 0
    for ds, rec in checks.items():
        qd = QDate.fromString(ds, "yyyy-MM-dd")
        if qd.isValid() and rec["count"] >= habit_goal_count(habit):
            total_days += 1

    # 月完成率：本月计划日中已完成比例（分母 = max(开始日,月初) → min(今天,月末)）
    planned = done = 0
    start = QDate.fromString(habit.get("start_date") or "", "yyyy-MM-dd")
    first = month_start
    if start.isValid() and start > first:
        first = start
    last = today
    d = first
    while d.isValid() and d <= last:
        if habit_due_on(habit, d):
            planned += 1
            if done_on(d):
                done += 1
        d = d.addDays(1)
    rate = round(done / planned * 100) if planned else 0

    # 当前连续：从今天往回，符合频率且完成 +1；今天未完成不算断签；漏打即停
    streak = 0
    d = today
    while d.isValid():
        if start.isValid() and d < start:
            break
        if habit_due_on(habit, d):
            if done_on(d):
                streak += 1
            elif d != today:
                break
        d = d.addDays(-1)

    # 最高连续：从开始日扫到今天，频率排到的日子里连续完成的最长一段。
    # 和当前连续一样按「漏打才算断」，频率没排到的那天直接跳过。
    longest = run = 0
    if start.isValid():
        d = start
        while d <= today:
            if habit_due_on(habit, d):
                if done_on(d):
                    run += 1
                    longest = max(longest, run)
                elif d != today:
                    run = 0
            d = d.addDays(1)

    # 目标进度（target_days）
    target = int(habit.get("target_days") or 0)
    return {"month_days": month_days, "total_days": total_days,
            "month_rate": rate, "streak": streak,
            "longest_streak": longest,
            "target_reached": bool(target and total_days >= target)}


# ---------------------------------------------------------------------------
# 习惯：打卡设置 + 导出
# ---------------------------------------------------------------------------
_CHECKIN_SMART_KEY = "habit_checkin_in_smart"


def habit_checkin_in_smart() -> bool:
    """「今日打卡」是否出现在「今天」「最近7天」两个智能清单里（滴答同名开关）。"""
    return db.get_setting(_CHECKIN_SMART_KEY, "1") != "0"


def habit_set_checkin_in_smart(on: bool) -> None:
    db.set_setting(_CHECKIN_SMART_KEY, "1" if on else "0")
    bump_habit_rev()


_FREQ_NAMES = {"1": "一", "2": "二", "3": "三", "4": "四",
               "5": "五", "6": "六", "7": "日"}


def habit_freq_text(habit: dict) -> str:
    if habit.get("freq_type", "daily") == "weekly":
        days = [(_FREQ_NAMES.get(x, x))
                for x in (habit.get("freq_days") or "").split(",") if x]
        return "每周 " + "、".join(days) if days else "每周"
    return "每天"


def habit_goal_text(habit: dict) -> str:
    return f"{habit_goal_count(habit)} 次/天"


def habit_check_rows(habit_id: int) -> list[dict]:
    """打卡明细（含首次打卡时刻），导出用。按日期倒序。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT date, count, note, created_at FROM habit_checks "
            "WHERE habit_id = ? ORDER BY date DESC", (habit_id,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def habit_export_sheets(include_archived: bool = True
                        ) -> tuple[list[tuple[str, list[list]]], int, int]:
    """按滴答「习惯导出」的格式组装工作表：每个习惯一个 sheet。

    返回 (sheets, 习惯数, 打卡条数)。
    """
    from datetime import datetime

    from .xlsx import safe_sheet_name

    used: set[str] = set()
    sheets: list[tuple[str, list[list]]] = []
    n_checks = 0
    for h in habit_list():
        if not include_archived and h.get("archived"):
            continue
        rows = habit_check_rows(h["id"])
        n_checks += len(rows)
        goal = habit_goal_count(h)
        dates = [r["date"] for r in rows]
        lo = min(dates) if dates else (h.get("start_date") or "")
        hi = max(dates) if dates else ""
        info = "\n".join([
            "基本信息",
            f"习惯名称：{h['name']}",
            "鼓励语：",
            f"习惯状态：{'已归档' if h.get('archived') else '进行中'}",
            f"目标：{habit_goal_text(h)}",
            f"所属分组：{h.get('group_name') or '其他'}",
            f"提醒时间：{h.get('reminder') or ''} ",
            f"频率：{habit_freq_text(h)}",
        ])
        table: list[list] = [[info], [f"日期:{lo}~{hi}"],
                             ["日期", "时间", "完成情况", "完成量", "心情", "日志"]]
        for r in rows:
            try:
                d = datetime.strptime(r["date"], "%Y-%m-%d")
                shown = f"{d.year}年{d.month:02d}月{d.day:02d}日"
            except ValueError:
                shown = r["date"]
            ts = (r.get("created_at") or "")[11:16]
            table.append([shown, ts, "完成" if r["count"] >= goal else "未完成",
                          r["count"], "", r.get("note") or ""])
        name = safe_sheet_name(h["name"], used)
        sheets.append((name, table))
    return sheets, len(sheets), n_checks

# ---------------------------------------------------------------------------
# 提醒去重（待办 / 习惯共用一张 notified 表）
# ---------------------------------------------------------------------------

def mark_notified(kind: str, ref_id: int, slot: str) -> bool:
    """记下「这条提醒已经响过」。返回 True 表示是第一次，调用方据此决定弹不弹。"""
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notified(kind, ref_id, slot) VALUES (?,?,?)",
            (kind, int(ref_id), slot))
        return cur.rowcount > 0


def prune_notified(keep_days: int = 14) -> None:
    """清掉久远的去重记录，否则用得越久表越大。"""
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM notified WHERE created_at < "
            "datetime('now', 'localtime', ?)", ("-%d days" % int(keep_days),))


# ---------------------------------------------------------------------------
# 算法刷题：我的题库记录 + 艾宾浩斯复习排期
# ---------------------------------------------------------------------------
# 这里只存我自己录入的东西（题目、题解、备注、写作记录），不含任何第三方正文。
# 复习不单独弹通知：每个记忆点落成「算法复习」清单里的一条待办，跟着待办页走，
# 勾掉时问一句「独立做出来了吗」，答案决定下一档是往后走还是退回来。

ALGO_LIST_NAME = "算法复习"
# 艾宾浩斯记忆点：在第 n 档复习完之后，隔这么多天再复习一次。
ALGO_INTERVALS = [1, 2, 4, 7, 15, 30]
ALGO_GRADUATED = len(ALGO_INTERVALS)      # 越过最后一档 = 记牢了，不再排


def solution_language_label(key: str) -> str:
    for k, label in SOLUTION_LANGUAGES:
        if k == key:
            return label
    return (key or "").strip() or "其他"


# 解题代码的语言。默认 C++（面试手写最常见），但同一题允许同时存多语言、
# 同一种语言也能存多版解法。
SOLUTION_LANGUAGES = [
    ("cpp", "C++"), ("python", "Python"), ("java", "Java"),
    ("go", "Go"), ("other", "其他"),
]
SOLUTION_DEFAULT_LANG = "cpp"
SOLUTION_LANG_LABEL = {k: label for k, label in SOLUTION_LANGUAGES}


def _algo_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def algo_url(ref: str, url: str = "") -> str:
    """有链接就用链接，否则按题号推（数字走搜索页，slug 拼题面）。"""
    url = (url or "").strip()
    if url:
        return url
    return leetcode_link(ref)


def algo_display(p: dict) -> str:
    """标题可以留空：只记了题号时列表和待办显示「力扣 15」。"""
    t = (p.get("title") or "").strip()
    if t:
        return t
    ref = (p.get("lc_ref") or "").strip()
    if ref:
        return "力扣 %s" % ref
    return "未命名题目"


def algo_stage_date(stage: int, base: str = "") -> str:
    """第 stage 档落在哪一天；越过最后一档返回空串（毕业，不再排复习）。"""
    return review_stage_date(stage, base)


def algo_ensure_list() -> None:
    """建「算法复习」清单（幂等）。用户自己建过同名清单也不会重复建。"""
    _review_ensure_list("algo")


# ---- 题目本体 -------------------------------------------------------------

def algo_problem_list(archived: int = 0, keyword: str = "", tag: str = "",
                      due_only: bool = False) -> list[dict]:
    """archived: 0=只在刷 / 1=只归档 / -1=全部。到期的排前面，其余按下次复习日。"""
    sql = "SELECT * FROM algo_problems WHERE 1=1"
    args: list[Any] = []
    if archived != -1:
        sql += " AND archived = ?"
        args.append(int(archived))
    if keyword:
        sql += " AND (title LIKE ? OR tags LIKE ? OR lc_ref LIKE ? OR note LIKE ?)"
        args.extend([f"%{keyword}%"] * 4)
    if tag:
        # 逗号包裹再匹配，避免「树」命中「二叉树」这种子串误伤
        sql += " AND ',' || tags || ',' LIKE ?"
        args.append(f"%,{tag},%")
    if due_only:
        sql += " AND next_review <> '' AND next_review <= date('now','localtime')"
    sql += (" ORDER BY CASE WHEN next_review = '' THEN 1 ELSE 0 END, "
            "next_review, id DESC")
    with db.connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def algo_problem_get(pid: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM algo_problems WHERE id = ?",
                           (int(pid),)).fetchone()
    return _row_to_dict(row)


def algo_problem_add(title: str, tags: str = "", lc_ref: str = "",
                     url: str = "", note: str = "") -> int:
    """新录一道题：立刻按第 0 档排出第一个复习点（也就是明天）。

    标题可以留空 —— 只记个力扣题号（或粘个链接）也算一道题，
    名字由 algo_display 兜出来，不逼人先起标题。
    """
    title = (title or "").strip()
    lc_ref = (lc_ref or "").strip()
    url = (url or "").strip()
    if not (title or lc_ref or url):
        return 0
    nxt = algo_stage_date(0)
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO algo_problems(title, tags, lc_ref, url, note, "
            "stage, next_review) VALUES(?,?,?,?,?,?,?)",
            (title, tags.strip(), lc_ref, algo_url(lc_ref, url),
             note, 0, nxt))
        pid = int(cur.lastrowid)
    _review_ensure_open("algo", pid)
    return pid


def algo_problem_update(pid: int, **fields: Any) -> None:
    allowed = {"title", "tags", "lc_ref", "url", "note", "solved",
               "last_written_at", "stage", "next_review", "archived"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    # 只填了题号却删掉标题时，得保证还有东西能认得出这道题
    if "title" in updates or "lc_ref" in updates:
        cur = algo_problem_get(pid)
        t = updates.get("title", cur.get("title") or "").strip()
        ref = updates.get("lc_ref", cur.get("lc_ref") or "").strip()
        u = updates.get("url", cur.get("url") or "").strip()
        if not (t or ref or u):
            updates.pop("title", None)
            updates.pop("lc_ref", None)
            if not updates:
                return
        if "lc_ref" in updates or "url" in updates:
            updates["url"] = u or algo_url(ref, u)
    updates.setdefault("updated_at", _algo_now())
    cols = ", ".join(f"{k} = ?" for k in updates)
    with db.connect() as conn:
        conn.execute(f"UPDATE algo_problems SET {cols} WHERE id = ?",
                     (*updates.values(), int(pid)))
    if "title" in fields:
        _review_sync_title("algo", pid)


def algo_problem_delete(pid: int) -> None:
    """连题解、写作记录、复习映射一起删；挂在待办里的复习也一并撤掉。

    已完成的那几条也要删 —— 它们是这一页生成的合成待办，题目本体没了之后
    留在清单里只会是「复习算法题：某道已经不存在的题」这种认不出来源的残骸。
    """
    pid = int(pid)
    for r in algo_review_list(pid):
        if r["todo_id"]:
            todo_delete(r["todo_id"], hard=True)
    with db.connect() as conn:
        conn.execute("DELETE FROM algo_problems WHERE id = ?", (pid,))
        conn.execute("DELETE FROM algo_solutions WHERE problem_id = ?", (pid,))
        conn.execute("DELETE FROM algo_writes WHERE problem_id = ?", (pid,))
        conn.execute("DELETE FROM algo_reviews WHERE problem_id = ?", (pid,))


def algo_problem_set_archived(pid: int, on: bool) -> dict:
    """归档 = 停止排复习但记录全留；取消归档按当前档期重新排一次。"""
    pid = int(pid)
    algo_problem_update(pid, archived=int(on))
    if on:
        _review_close("algo", pid, "dropped")
    else:
        p = algo_problem_get(pid)
        if p and not p.get("next_review"):
            algo_problem_update(pid, next_review=algo_stage_date(
                int(p.get("stage") or 0)))
        _review_ensure_open("algo", pid)
    return algo_problem_get(pid)


def algo_tags() -> list[str]:
    """所有用过的标签（去重、按用得多少排），给筛选下拉用。"""
    with db.connect() as conn:
        rows = conn.execute("SELECT tags FROM algo_problems").fetchall()
    counter: dict[str, int] = {}
    for r in rows:
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t:
                counter[t] = counter.get(t, 0) + 1
    return [t for t, _n in sorted(counter.items(), key=lambda x: (-x[1], x[0]))]


# ---- 题解（一题可多条）-----------------------------------------------------

def algo_solution_list(pid: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM algo_solutions WHERE problem_id = ? "
            "ORDER BY CASE WHEN language = ? THEN 0 ELSE 1 END, language, id",
            (int(pid), SOLUTION_DEFAULT_LANG)).fetchall()
    return [_row_to_dict(r) for r in rows]


def algo_solution_add(pid: int, body: str = "", idea: str = "",
                      language: str = SOLUTION_DEFAULT_LANG,
                      source: str = "", allow_empty: bool = False) -> int:
    """加一版解法。默认 C++，同一语言也能再存几版（暴力 / 最优）。"""
    lang = (language or SOLUTION_DEFAULT_LANG).strip().lower()
    if not allow_empty and not (body or "").strip() and not (idea or "").strip():
        return 0
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO algo_solutions(problem_id, language, idea, body, source) "
            "VALUES(?,?,?,?,?)",
            (int(pid), lang, idea or "", body or "", (source or "").strip()))
        return int(cur.lastrowid)


def algo_solution_update(sid: int, **fields: Any) -> None:
    updates = {k: v for k, v in fields.items()
               if k in ("body", "source", "idea", "language")}
    if not updates:
        return
    if "language" in updates:
        updates["language"] = (str(updates["language"]) or "cpp").strip().lower()
    updates["updated_at"] = _algo_now()
    cols = ", ".join(f"{k} = ?" for k in updates)
    with db.connect() as conn:
        conn.execute(f"UPDATE algo_solutions SET {cols} WHERE id = ?",
                     (*updates.values(), int(sid)))


def algo_solution_delete(sid: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM algo_solutions WHERE id = ?", (int(sid),))


def algo_solution_map() -> dict:
    return _solution_map("algo_solutions")


def _solution_map(table: str) -> dict:
    """{题目 id: "C++×2 · Python"} —— 给列表行用，一次查完不逐题问。"""
    if table not in ("algo_solutions", "interview_solutions"):
        raise ValueError(table)
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT problem_id, language, COUNT(*) AS n FROM {table} "
            "GROUP BY problem_id, language ORDER BY problem_id, language").fetchall()
    grouped: dict[int, list[str]] = {}
    for r in rows:
        label = solution_language_label(r["language"])
        n = int(r["n"] or 0)
        grouped.setdefault(r["problem_id"], []).append(
            f"{label}×{n}" if n > 1 else label)
    return {pid: " · ".join(parts) for pid, parts in grouped.items()}


# ---- 写作历史 -------------------------------------------------------------

def algo_write_list(pid: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM algo_writes WHERE problem_id = ? "
            "ORDER BY written_at DESC, id DESC", (int(pid),)).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---- 复习排期与待办的对账（实现见文件末尾的共用内核）------------------------

def algo_review_list(pid: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM algo_reviews WHERE problem_id = ? "
            "ORDER BY id DESC", (int(pid),)).fetchall()
    return [_row_to_dict(r) for r in rows]


def algo_open_review(pid: int) -> dict:
    return _review_open("algo", pid)


def algo_review_by_todo(todo_id: int) -> dict:
    return _review_by_todo("algo", todo_id)


def algo_log_write(pid: int, independent: bool = True, note: str = "",
                   when: str = "") -> dict:
    """记一次「我写了这道题」，并按是否独立做出来推进档期。

    独立做出来 → 进下一档（间隔拉长）；没独立做出来 → 退回一档重记，
    免得自以为会了把复习点排得太远。越过最后一档就不再排复习。
    """
    pid = int(pid)
    if not algo_problem_get(pid):
        return {}
    when = (when or _algo_now()).strip()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO algo_writes(problem_id, written_at, independent, note) "
            "VALUES(?,?,?,?)", (pid, when, int(bool(independent)), note))
    _review_reschedule("algo", pid, independent, when)
    algo_problem_update(pid, solved=1, last_written_at=when)
    return algo_problem_get(pid)


def algo_resolve_review_todo(todo_id: int, independent: bool) -> dict:
    """待办页勾掉复习待办时调这个：记进写作历史并重排下一档。"""
    r = _review_by_todo("algo", todo_id)
    if not r:
        return {}
    return algo_log_write(r["problem_id"], independent=independent)


def algo_set_next_review(pid: int, due: str) -> dict:
    """手动指定下次复习日（改档期）。旧的复习待办撤掉，按新日期重建。"""
    return _review_set_next("algo", pid, due)


def algo_sync_reviews() -> int:
    """启动对账：把每道在刷题缺的复习待办补齐。返回补建/推进的条数。"""
    return _review_sync("algo")


def algo_stats() -> dict:
    """页面顶部统计卡要的数。"""
    with db.connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM algo_problems").fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) c FROM algo_problems WHERE archived = 0").fetchone()["c"]
        due = conn.execute(
            "SELECT COUNT(*) c FROM algo_problems WHERE archived = 0 "
            "AND next_review <> '' AND next_review <= date('now','localtime')"
        ).fetchone()["c"]
        solved = conn.execute(
            "SELECT COUNT(*) c FROM algo_problems WHERE solved = 1").fetchone()["c"]
        writes = conn.execute(
            "SELECT COUNT(*) c FROM algo_writes").fetchone()["c"]
        grads = conn.execute(
            "SELECT COUNT(*) c FROM algo_problems WHERE stage >= ?",
            (ALGO_GRADUATED,)).fetchone()["c"]
    return {"total": total, "active": active, "due": due, "solved": solved,
            "writes": writes, "graduated": grads}


# ---------------------------------------------------------------------------
# 八股刷题：自己上传的题库 + 抽题自答 + 艾宾浩斯复习
# ---------------------------------------------------------------------------
# 玩法照搬用户自己写的 tkinter 版 AnswerMachine：加权随机抽题 → 自己写答案 →
# 跟标准答案比相似度打分并把差异标出来 → 手动判对/错/跳过。
# 复习排期跟算法页共用同一套内核（见文件末尾的 _review_*）。

INTERVIEW_LIST_NAME = "八股复习"
# 抽题权重：AnswerMachine 原式 max(1.5*(错+1) - 对, 1) + (从没答对过 ? 50 : 0)
_PICK_SMOOTH = 1.5
_PICK_NEW_BONUS = 50
# 一轮 = 每道题都至少答对过一次
_ROUND_COL = "has_been_correct"

_QQ_RE = re.compile(r"^【Q\s*\d*】\s*(.*)$")
_AA_RE = re.compile(r"^【A\s*\d*】\s*(.*)$")
_RAWQ_RE = re.compile(r"^【([^】]*)】\s*(.*)$")


def interview_parse_deck(text: str) -> list[tuple[str, str]]:
    """把题库文本解析成 [(问题, 答案)]，两种格式都吃。

    1) clean.py 产出的成对格式：``【Q1】问题`` / ``【A1】答案``（答案可跨行）
    2) raw_text 的原始格式：``【问题】`` 单独一行，后面普通行都是它的答案

    两种混在同一个文件里也能解析；问题为空的条目直接丢。
    """
    items: list[list[str]] = []

    def append_answer(part: str) -> None:
        if not items or not part:
            return
        items[-1][1] = (items[-1][1] + "\n" + part).strip()

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _QQ_RE.match(line)
        if m:
            items.append([m.group(1).strip(), ""])
            continue
        m = _AA_RE.match(line)
        if m:
            append_answer(m.group(1).strip())
            continue
        m = _RAWQ_RE.match(line)
        if m and not re.match(r"^[QA]\s*\d*$", m.group(1).strip()):
            # 原始格式：问题在括号里；括号后还跟着字的话那部分是答案首行
            q = m.group(1).strip()
            items.append([q or m.group(2).strip(), "" if q else m.group(2).strip()])
            continue
        append_answer(line)
    return [(q, a.strip()) for q, a in items if q]


# 列表排序口径。默认按复习日，但攒题库的人最常问的是「我哪几道最弱」。
INTERVIEW_ORDERS = {
    "review": ("CASE WHEN next_review = '' THEN 1 ELSE 0 END, "
               "next_review, id DESC"),
    "weak": "incorrect_count DESC, correct_count ASC, seen_count ASC, id DESC",
    "stale": ("CASE WHEN last_seen_at = '' THEN '0000-01-01' "
              "ELSE last_seen_at END ASC, id DESC"),
    "score": "best_score ASC, incorrect_count DESC, id DESC",
    "new": "id DESC",
}


def leetcode_link(ref: str) -> str:
    """题号 → 力扣地址。

    数字题号拼不出题面 URL（力扣的题面用的是英文 slug，如
    ``hopper-company-queries-i``，跟数字 ID 不是一回事），
    所以走搜索页 ``/search/?q=<数字>``，点开就是那道题；
    给了 slug 才能直接拼题面地址。
    """
    ref = (ref or "").strip().strip("/")
    if not ref:
        return ""
    if ref.isdigit():
        return "https://leetcode.cn/search/?q=%s" % ref
    return "https://leetcode.cn/problems/%s/" % ref.lower()


def interview_url(ref: str, url: str = "") -> str:
    """用户自己粘过的链接优先；没粘才按题号推。"""
    url = (url or "").strip()
    if url:
        return url
    return leetcode_link(ref)


def interview_display(p: dict) -> str:
    """列表、待办标题、刷题页都显示这个：题干可以留空，只记题号也算一道题。"""
    q = (p.get("question") or "").strip()
    if q:
        return q
    ref = (p.get("lc_ref") or "").strip()
    if ref:
        return "力扣 %s" % ref
    return "未命名题目"


def interview_ref_is_numeric(p: dict) -> bool:
    """题号是纯数字时，打开的是力扣搜索页而不是题面页，界面上说明一句。"""
    return (p.get("lc_ref") or "").strip().isdigit()


def interview_problem_list(archived: int = 0, keyword: str = "", tag: str = "",
                           due_only: bool = False,
                           order: str = "review") -> list[dict]:
    # no_answer 是算出来的列：没写标准答案的题打不了分，界面上必须看得出来
    # sol_count 同理，省掉列表里逐题查代码的 N+1
    sql = ("SELECT *, (answer = '') AS no_answer, "
           "(SELECT COUNT(*) FROM interview_solutions s "
           " WHERE s.problem_id = interview_problems.id) AS sol_count "
           "FROM interview_problems WHERE 1=1")
    args: list[Any] = []
    if archived != -1:
        sql += " AND archived = ?"
        args.append(int(archived))
    if keyword:
        sql += (" AND (question LIKE ? OR answer LIKE ? OR tags LIKE ?"
                " OR note LIKE ? OR lc_ref LIKE ?)")
        args.extend([f"%{keyword}%"] * 5)
    if tag:
        sql += " AND ',' || tags || ',' LIKE ?"
        args.append(f"%,{tag},%")
    if due_only:
        sql += " AND next_review <> '' AND next_review <= date('now','localtime')"
    # 排序串全部来自上面写死的 INTERVIEW_ORDERS，不接受外部输入
    sql += " ORDER BY " + INTERVIEW_ORDERS.get(order, INTERVIEW_ORDERS["review"])
    with db.connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def interview_problem_get(iid: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM interview_problems WHERE id = ?",
                           (int(iid),)).fetchone()
    return _row_to_dict(row)


def interview_problem_add(question: str, answer: str = "", tags: str = "",
                          url: str = "", note: str = "",
                          lc_ref: str = "") -> int:
    """新录一道题：立刻排出第一个复习点（明天）并挂上待办。

    题干允许留空 —— 只记个力扣题号（或粘个链接）也算一道题，
    名字由 interview_display 兜出来，不逼用户先起标题。
    """
    question = (question or "").strip()
    lc_ref = (lc_ref or "").strip()
    url = (url or "").strip()
    if not (question or lc_ref or url):
        return 0
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO interview_problems(question, answer, tags, url, note, "
            "lc_ref, stage, next_review) VALUES(?,?,?,?,?,?,?,?)",
            (question, (answer or "").strip(), tags.strip(),
             interview_url(lc_ref, url), note, lc_ref, 0,
             review_stage_date(0)))
        iid = int(cur.lastrowid)
    _review_ensure_open("interview", iid)
    return iid


def interview_problem_update(iid: int, **fields: Any) -> None:
    allowed = {"question", "answer", "tags", "url", "note", "lc_ref", "stage",
               "next_review", "archived"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    # 只填了题号却删掉题干时，得保证还有东西能认得出这道题
    if "question" in updates or "lc_ref" in updates:
        cur = interview_problem_get(iid)
        q = updates.get("question", cur.get("question") or "").strip()
        ref = updates.get("lc_ref", cur.get("lc_ref") or "").strip()
        u = updates.get("url", cur.get("url") or "").strip()
        if not (q or ref or u):
            updates.pop("question", None)
            updates.pop("lc_ref", None)
            if not updates:
                return
        # 题号变了就重算链接（用户手粘过的链接优先保留）
        if "lc_ref" in updates or "url" in updates:
            updates["url"] = u or interview_url(ref, u)
    updates.setdefault("updated_at", _algo_now())
    cols = ", ".join(f"{k} = ?" for k in updates)
    with db.connect() as conn:
        conn.execute(f"UPDATE interview_problems SET {cols} WHERE id = ?",
                     (*updates.values(), int(iid)))
    if "question" in updates or "lc_ref" in updates:
        _review_sync_title("interview", iid)


def interview_problem_delete(iid: int) -> None:
    """作答历史和复习待办一起清掉：合成待办留在清单里只会变成认不出来源的残骸。"""
    iid = int(iid)
    with db.connect() as conn:
        rows = conn.execute("SELECT todo_id FROM interview_reviews "
                            "WHERE problem_id = ?", (iid,)).fetchall()
    for r in rows:
        if r["todo_id"]:
            todo_delete(r["todo_id"], hard=True)
    with db.connect() as conn:
        conn.execute("DELETE FROM interview_problems WHERE id = ?", (iid,))
        conn.execute("DELETE FROM interview_solutions WHERE problem_id = ?", (iid,))
        conn.execute("DELETE FROM interview_attempts WHERE problem_id = ?", (iid,))
        conn.execute("DELETE FROM interview_reviews WHERE problem_id = ?", (iid,))


def interview_problem_set_archived(iid: int, on: bool) -> dict:
    iid = int(iid)
    interview_problem_update(iid, archived=int(on))
    if on:
        _review_close("interview", iid, "dropped")
    else:
        p = interview_problem_get(iid)
        if p and not p.get("next_review"):
            interview_problem_update(iid, next_review=review_stage_date(
                int(p.get("stage") or 0)))
        _review_ensure_open("interview", iid)
    return interview_problem_get(iid)


def interview_import_text(text: str, tags: str = "") -> dict:
    """批量导入题库文本，返回 {"created", "skipped"}。

    重复导入同一份是安全的：问题文本（忽略空白差异）已存在的条目跳过，
    这样误点两次导入不会把题库翻一倍。
    """
    parsed = interview_parse_deck(text)
    created = skipped = 0
    with db.connect() as conn:
        known = {(r["question"] or "").replace(" ", "").replace("\n", "")
                 for r in conn.execute("SELECT question FROM interview_problems")}
    for q, a in parsed:
        key = q.replace(" ", "").replace("\n", "")
        if key in known:
            skipped += 1
            continue
        known.add(key)
        if interview_problem_add(q, a, tags):
            created += 1
    return {"created": created, "skipped": skipped}


def _merge_tags(existing: str, add=(), remove=()) -> str:
    """标签合并：去重、保持原有顺序，新增的追加在末尾。"""
    out: list[str] = []
    for t in (existing or "").split(","):
        t = t.strip()
        if t and t not in out:
            out.append(t)
    for t in add:
        t = (t or "").strip()
        if t and t not in out:
            out.append(t)
    drop = {(t or "").strip() for t in remove}
    return ",".join(t for t in out if t and t not in drop)


def interview_archive_many(ids, on: bool) -> int:
    """批量归档 / 取消归档。逐条走单条入口，归档时撤合成待办那套逻辑才不会漏。"""
    for iid in ids:
        interview_problem_set_archived(iid, on)
    return len(list(ids))


def interview_delete_many(ids) -> int:
    n = 0
    for iid in ids:
        interview_problem_delete(iid)
        n += 1
    return n


def interview_tags_add(ids, raw: str) -> int:
    """给选中的题批量加标签（一次可以填多个，逗号分隔）。"""
    add = [t for t in (raw or "").split(",") if t.strip()]
    if not add:
        return 0
    for iid in ids:
        p = interview_problem_get(iid)
        if p:
            interview_problem_update(iid, tags=_merge_tags(p.get("tags"), add))
    return len(list(ids))


def interview_tags_remove(ids, tag: str) -> int:
    tag = (tag or "").strip()
    if not tag:
        return 0
    for iid in ids:
        p = interview_problem_get(iid)
        if p:
            interview_problem_update(iid, tags=_merge_tags(p.get("tags"), remove=[tag]))
    return len(list(ids))


# ---- 面经批量粘贴：一组标题 + 编号问题 + 可选的代码题 ----------------------
# 形状是这样的（用户实际会贴的东西）：
#     字节llm算法一面：
#     1. 用户的画像是怎么获取的？
#     2. ……
#     代码：二叉树的最大宽度，bfs 层序遍历……
# 只有问题、没有答案，所以组名转成标签，答案留空由「⚠ 缺标准答案」提示补。

_NOTES_GROUP_RE = re.compile(r"^\s*([^\n：:]{1,40}?)\s*[：:]\s*$")
_NOTES_NUM_RE = re.compile(r"^\s*(\d{1,3})\s*[\.、．)）]\s*(.+?)\s*$")
_NOTES_CODE_RE = re.compile(r"^\s*(代码|编程|手写|算法题)\s*[：:]\s*(.*)$")
_NOTES_BLANK = {"", "无", "没有", "-", "—", "/", "n/a", "na"}


def interview_parse_notes(text: str) -> list[tuple[str, str]]:
    """解析面经文本，返回 [(组名, 问题)]。

    容错：编号写成 1. / 1、/ 1）都行；「代码：无」这类空占位丢掉；
    问题被折行拆成两行时接到上一题后面；整段没有组名时组名为空串。
    """
    items: list[list[str]] = []
    group = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _NOTES_NUM_RE.match(line)
        if m:
            items.append([group, m.group(2).strip()])
            continue
        m = _NOTES_CODE_RE.match(line)
        if m:
            body = m.group(2).strip()
            if body.lower() not in _NOTES_BLANK:
                items.append([group, "代码：%s" % body])
            continue
        m = _NOTES_GROUP_RE.match(line)
        if m:
            group = m.group(1).strip()
            continue
        if items and items[-1][0] == group and items[-1][1]:
            items[-1][1] = (items[-1][1] + " " + line).strip()
        else:
            items.append([group, line])
    return [(g, q) for g, q in items if q]


def interview_import_notes(text: str, extra_tag: str = "") -> dict:
    """导入面经：组名当标签，问题入库，答案留空。返回分组明细供预览/回执。"""
    parsed = interview_parse_notes(text)
    extra = [t.strip() for t in (extra_tag or "").split(",") if t.strip()]
    created = skipped = 0
    per_group: dict[str, int] = {}
    with db.connect() as conn:
        known = {(r["question"] or "").replace(" ", "").replace("\n", "")
                 for r in conn.execute("SELECT question FROM interview_problems")}
    for group, q in parsed:
        key = q.replace(" ", "").replace("\n", "")
        if key in known:
            skipped += 1
            continue
        known.add(key)
        tags = _merge_tags("", (group,) if group else ())
        tags = _merge_tags(tags, extra)
        if interview_problem_add(q, "", tags):
            created += 1
            per_group[group or "（无组名）"] = per_group.get(group or "（无组名）", 0) + 1
    return {"created": created, "skipped": skipped, "groups": per_group,
            "total": len(parsed)}


def interview_tags() -> list[str]:
    with db.connect() as conn:
        rows = conn.execute("SELECT tags FROM interview_problems").fetchall()
    counter: dict[str, int] = {}
    for r in rows:
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t:
                counter[t] = counter.get(t, 0) + 1
    return [t for t, _n in sorted(counter.items(), key=lambda x: (-x[1], x[0]))]


# ---- 打分与差异高亮 --------------------------------------------------------

def interview_score(ref: str, user: str) -> int:
    """用户答案与标准答案的相似度，0-100。跟 AnswerMachine 同一个算法。"""
    a = re.sub(r"\s+", " ", ref or "").strip()
    b = re.sub(r"\s+", " ", user or "").strip()
    if not a:
        return 0
    return round(SequenceMatcher(None, a, b).ratio() * 100)


def interview_highlight(ref: str, user: str) -> str:
    """把标准答案里跟用户答案对不上的部分用 [] 框出来，其余原样保留。"""
    matcher = SequenceMatcher(None, ref or "", user or "")
    out = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        piece = (ref or "")[i1:i2]
        if tag == "equal" or not piece:
            out.append(piece)
        else:
            out.append("[" + piece + "]")
    return "".join(out)


# ---- 抽题与作答 ------------------------------------------------------------

def interview_pick(exclude_id: int = 0, tag: str = "",
                   keyword: str = "") -> dict:
    """抽一题：先清到期队列，再按错误率加权。

    到期优先是必须的 —— 记忆点排好了却迟迟抽不到，整套艾宾浩斯就白做了。
    没有到期题时才回到全量按权重抽：错得越多越常抽，从没答对过的给大权重
    （AnswerMachine 原式 max(1.5*(错+1)-对, 1) + 未答对过 ? 50 : 0）。
    exclude_id 用来避开刚考过的那题，原程序会连着抽到同一道。
    """
    rows = interview_problem_list(archived=0, tag=tag, keyword=keyword)
    if not rows:
        return {}
    today = date.today().isoformat()
    due = [r for r in rows
           if r.get("next_review") and r["next_review"] <= today
           and r["id"] != int(exclude_id)]
    pool = due or rows
    if len(pool) > 1:
        pool = [r for r in pool if r["id"] != int(exclude_id)]
    weights = [
        max(_PICK_SMOOTH * (int(r.get("incorrect_count") or 0) + 1)
            - int(r.get("correct_count") or 0), 1)
        + (0 if int(r.get(_ROUND_COL) or 0) else _PICK_NEW_BONUS)
        for r in pool]
    return random.choices(pool, weights=weights, k=1)[0]


# ---- 解题代码：一题多语言、同语言多方案 ------------------------------------

def interview_solution_list(pid: int, language: str = "") -> list[dict]:
    sql = "SELECT * FROM interview_solutions WHERE problem_id = ?"
    args: list[Any] = [int(pid)]
    if language:
        sql += " AND language = ?"
        args.append(language)
    sql += " ORDER BY CASE WHEN language = ? THEN 0 ELSE 1 END, language, id"
    args.append(SOLUTION_DEFAULT_LANG)
    with db.connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def interview_solution_get(sid: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM interview_solutions WHERE id = ?",
                           (int(sid),)).fetchone()
    return _row_to_dict(row)


def interview_solution_add(pid: int, body: str = "", idea: str = "",
                           language: str = SOLUTION_DEFAULT_LANG,
                           allow_empty: bool = False) -> int:
    """加一版解法。默认 C++，但同一语言可以再存几版（比如暴力 + 最优）。

    allow_empty 只给界面上「先建一张空卡再往里写」用；批量导入走默认值，
    免得把空解法塞进库。
    """
    lang = (language or SOLUTION_DEFAULT_LANG).strip().lower()
    if not allow_empty and not (body or "").strip() and not (idea or "").strip():
        return 0
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO interview_solutions(problem_id, language, body, idea) "
            "VALUES(?,?,?,?)", (int(pid), lang, body or "", idea or ""))
        return int(cur.lastrowid)


def interview_solution_update(sid: int, **fields: Any) -> None:
    updates = {k: v for k, v in fields.items()
               if k in ("body", "idea", "language")}
    if not updates:
        return
    if "language" in updates:
        updates["language"] = (str(updates["language"]) or "cpp").strip().lower()
    updates["updated_at"] = _algo_now()
    cols = ", ".join(f"{k} = ?" for k in updates)
    with db.connect() as conn:
        conn.execute(f"UPDATE interview_solutions SET {cols} WHERE id = ?",
                     (*updates.values(), int(sid)))


def interview_solution_delete(sid: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM interview_solutions WHERE id = ?", (int(sid),))


def interview_solution_map() -> dict:
    """{题目 id: "C++×2 · Python"} —— 给列表行用，一次查完不逐题问。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT problem_id, language, COUNT(*) AS n FROM interview_solutions "
            "GROUP BY problem_id, language ORDER BY problem_id, language").fetchall()
    grouped: dict[int, list[str]] = {}
    for r in rows:
        label = solution_language_label(r["language"])
        n = int(r["n"] or 0)
        grouped.setdefault(r["problem_id"], []).append(
            f"{label}×{n}" if n > 1 else label)
    return {pid: " · ".join(parts) for pid, parts in grouped.items()}


def interview_attempt_list(iid: int, limit: int = 20) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM interview_attempts WHERE problem_id = ? "
            "ORDER BY answered_at DESC, id DESC LIMIT ?",
            (int(iid), int(limit))).fetchall()
    return [_row_to_dict(r) for r in rows]


def interview_log_attempt(iid: int, user_answer: str = "", score: int = 0,
                          decision: str = "skip") -> dict:
    """记一次作答，并按判定重排复习档期。

    correct → 进下一档；incorrect → 退一档重记；skip 只算见过，不动档期。
    """
    iid = int(iid)
    p = interview_problem_get(iid)
    if not p:
        return {}
    when = _algo_now()
    decision = decision if decision in ("correct", "incorrect", "skip") else "skip"
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO interview_attempts(problem_id, answered_at, "
            "user_answer, score, decision) VALUES(?,?,?,?,?)",
            (iid, when, user_answer or "", int(score), decision))
        conn.execute(
            "UPDATE interview_problems SET seen_count = seen_count + 1, "
            "last_seen_at = ?, best_score = MAX(best_score, ?), "
            "correct_count = correct_count + ?, "
            "incorrect_count = incorrect_count + ?, "
            "has_been_correct = MAX(has_been_correct, ?) WHERE id = ?",
            (when, int(score), int(decision == "correct"),
             int(decision == "incorrect"), int(decision == "correct"), iid))
    if decision != "skip":
        _review_reschedule("interview", iid, decision == "correct", when)
    return interview_problem_get(iid)


def interview_round_complete() -> bool:
    """一轮 = 每道在刷的题都至少答对过一次。"""
    rows = interview_problem_list(archived=0)
    return bool(rows) and all(int(r.get(_ROUND_COL) or 0) for r in rows)


def interview_reset_round() -> int:
    """重开一轮：只清「本轮已答对」，历次统计全部保留。"""
    with db.connect() as conn:
        cur = conn.execute("UPDATE interview_problems SET has_been_correct = 0 "
                           "WHERE archived = 0")
        return cur.rowcount


# ---- 统计 ------------------------------------------------------------------

def interview_stats() -> dict:
    """页面统计 + 刷题模式的进度面板（正确次数分布照 AnswerMachine 的口径）。"""
    with db.connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM interview_problems").fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) c FROM interview_problems WHERE archived = 0"
        ).fetchone()["c"]
        due = conn.execute(
            "SELECT COUNT(*) c FROM interview_problems WHERE archived = 0 "
            "AND next_review <> '' AND next_review <= date('now','localtime')"
        ).fetchone()["c"]
        mastered = conn.execute(
            "SELECT COUNT(*) c FROM interview_problems WHERE archived = 0 "
            "AND has_been_correct = 1").fetchone()["c"]
        attempts = conn.execute(
            "SELECT COUNT(*) c FROM interview_attempts").fetchone()["c"]
        dist = {n: 0 for n in (0, 1, 2, 3, 4)}
        for r in conn.execute(
                "SELECT correct_count AS c, COUNT(*) AS n "
                "FROM interview_problems WHERE archived = 0 GROUP BY c"):
            dist[4 if int(r["c"] or 0) > 3 else int(r["c"] or 0)] += r["n"]
    return {"total": total, "active": active, "due": due,
            "mastered": mastered, "attempts": attempts, "dist": dist}


# ---- 复习待办（薄封装，实现都在文件末尾的共用内核里）------------------------

def interview_open_review(iid: int) -> dict:
    return _review_open("interview", iid)


def interview_review_list(iid: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM interview_reviews WHERE problem_id = ? "
            "ORDER BY id DESC", (int(iid),)).fetchall()
    return [_row_to_dict(r) for r in rows]


def interview_ensure_list() -> None:
    _review_ensure_list("interview")


def interview_sync_reviews() -> int:
    return _review_sync("interview")


def interview_set_next_review(iid: int, due: str) -> dict:
    return _review_set_next("interview", iid, due)


# ---------------------------------------------------------------------------
# 复习排期内核：算法页与八股页共用一份实现
# ---------------------------------------------------------------------------
# 两页的排期规则完全一样：一道活跃题在清单里恰好挂着一条未完成的复习待办，
# 勾掉时按「独立做出来 / 答对了」推进一档，反之退一档重记。差别只有表名、
# 清单名和待办标题前缀 —— 所以参数化成一份，免得两份各自演化后悄悄跑偏。
# 放在文件末尾是因为这里要引用上面两节定义的表名常量。

_REVIEW_KINDS = {
    "algo": {
        "problems": "algo_problems",
        "reviews": "algo_reviews",
        "list_name": ALGO_LIST_NAME,
        # 清单名已经写了「算法复习」，标题再重复一遍只会把行撑成两行
        "prefix": "复习",
        "label_col": "title",
        "label_max": 26,
        "display": algo_display,
    },
    "interview": {
        "problems": "interview_problems",
        "reviews": "interview_reviews",
        "list_name": INTERVIEW_LIST_NAME,
        "prefix": "复习",
        "label_col": "question",
        "label_max": 26,
        "display": interview_display,
    },
}


def _shorten(text: str, limit: int = 40) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def review_stage_date(stage: int, base: str = "") -> str:
    """第 stage 档的复习日期；越过最后一档返回空串（毕业，不再排）。"""
    if stage >= len(ALGO_INTERVALS) or stage < 0:
        return ""
    base_d = _parse_d(base) or date.today()
    return (base_d + timedelta(days=ALGO_INTERVALS[stage])).isoformat()


def _review_problem(kind: str, pid: int) -> dict:
    tbl = _REVIEW_KINDS[kind]["problems"]
    with db.connect() as conn:
        return _row_to_dict(conn.execute(
            f"SELECT * FROM {tbl} WHERE id = ?", (int(pid),)).fetchone())


def _review_patch(kind: str, pid: int, **fields: Any) -> None:
    """排期内核只允许改这两列；其余字段由各页自己的 update 负责。"""
    fields = {k: v for k, v in fields.items() if k in ("stage", "next_review")}
    if not fields:
        return
    tbl = _REVIEW_KINDS[kind]["problems"]
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as conn:
        conn.execute(f"UPDATE {tbl} SET {cols} WHERE id = ?",
                     (*fields.values(), int(pid)))


def _review_active(kind: str) -> list[dict]:
    tbl = _REVIEW_KINDS[kind]["problems"]
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM {tbl} WHERE archived = 0 ORDER BY id").fetchall()
    return [_row_to_dict(r) for r in rows]


def _review_ensure_list(kind: str) -> None:
    name = _REVIEW_KINDS[kind]["list_name"]
    if any(l["name"] == name and l.get("kind") != "folder"
           for l in list_all(include_archived=True)):
        return
    list_add(name, color="")


def _review_open(kind: str, pid: int) -> dict:
    tbl = _REVIEW_KINDS[kind]["reviews"]
    with db.connect() as conn:
        return _row_to_dict(conn.execute(
            f"SELECT * FROM {tbl} WHERE problem_id = ? AND status = 'open' "
            "LIMIT 1", (int(pid),)).fetchone())


def _review_update(kind: str, rid: int, **fields: Any) -> None:
    tbl = _REVIEW_KINDS[kind]["reviews"]
    fields = {k: v for k, v in fields.items()
              if k in ("status", "due_date", "todo_id", "stage")}
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as conn:
        conn.execute(f"UPDATE {tbl} SET {cols} WHERE id = ?",
                     (*fields.values(), int(rid)))


def _review_by_todo(kind: str, todo_id: int) -> dict:
    tbl = _REVIEW_KINDS[kind]["reviews"]
    with db.connect() as conn:
        return _row_to_dict(conn.execute(
            f"SELECT * FROM {tbl} WHERE todo_id = ? AND status = 'open' LIMIT 1",
            (int(todo_id),)).fetchone())


def _review_todo_title(kind: str, p: dict) -> str:
    spec = _REVIEW_KINDS[kind]
    name = (p.get(spec["label_col"]) or "").strip()
    # 题干/标题空着时用兜底名，否则待办会变成「复习：（力扣 15）」
    label = _shorten(name or spec["display"](p), spec["label_max"])
    ref = (p.get("lc_ref") or "").strip()
    if ref and name:
        return "%s：%s（力扣 %s）" % (spec["prefix"], label, ref)
    return "%s：%s" % (spec["prefix"], label)


def _review_new_todo(kind: str, p: dict, due: str) -> int:
    """建复习待办。链接写在备注第一行，待办页的跳转图标从那儿取。"""
    _review_ensure_list(kind)
    lines = []
    if p.get("url"):
        lines.append(p["url"])
    if p.get("tags"):
        lines.append("标签：" + p["tags"])
    if p.get("note"):
        lines.append(p["note"])
    return int(todo_add(title=_review_todo_title(kind, p),
                        note="\n".join(lines), due_date=due,
                        list_name=_REVIEW_KINDS[kind]["list_name"]))


def _review_sync_title(kind: str, pid: int) -> None:
    """题干改了就把当前那条复习待办的标题一起改掉。

    待办标题是建的时候从题干拼出来的，不同步就会留下「复习八股：旧问题」
    这种指向已改名条目的待办。
    """
    r = _review_open(kind, pid)
    if not r or not r["todo_id"]:
        return
    p = _review_problem(kind, pid)
    if not p:
        return
    todo_update(r["todo_id"], title=_review_todo_title(kind, p))


def _review_close(kind: str, pid: int, status: str) -> None:
    """结掉当前挂着的那条复习，并把待办本身一起落定。

    只改映射不够：done_* 说明这次真做了，待办要勾掉；dropped 说明排期作废，
    合成待办要直接撤掉，否则清单里会永远挂着一条没人认领的复习。
    """
    r = _review_open(kind, pid)
    if not r:
        return
    _review_update(kind, r["id"], status=status)
    tid = r["todo_id"]
    if not tid:
        return
    if status.startswith("done"):
        t = todo_get(tid)
        if t and not int(t.get("done") or 0):
            todo_update(tid, done=1, completed_at=_algo_now())
    elif status == "dropped":
        todo_delete(tid, hard=True)


def _review_ensure_open(kind: str, pid: int) -> None:
    """守住不变量：有下次复习日 ↔ 待办里挂着一条没勾掉的复习待办。

    只在缺的时候补建，不改已有待办的日期 —— 用户在待办页手动顺延复习日是
    合法操作，反向覆盖它只会跟用户打架。
    """
    p = _review_problem(kind, pid)
    if not p or p.get("archived") or not p.get("next_review"):
        return
    r = _review_open(kind, pid)
    if r and r["todo_id"] and todo_get(r["todo_id"]):
        return
    if r:
        _review_update(kind, r["id"], status="dropped")
    tid = _review_new_todo(kind, p, p["next_review"])
    tbl = _REVIEW_KINDS[kind]["reviews"]
    with db.connect() as conn:
        conn.execute(
            f"INSERT INTO {tbl}(problem_id, todo_id, stage, due_date) "
            "VALUES(?,?,?,?)",
            (int(pid), tid, int(p.get("stage") or 0), p["next_review"]))


def _review_reschedule(kind: str, pid: int, independent: bool,
                       when: str) -> str:
    """按本次结果推进档期并换一条新的复习待办，返回新的下次复习日。

    调用方负责往自己的历史表里记这一次（algo_writes / interview_attempts）。
    """
    p = _review_problem(kind, pid)
    if not p:
        return ""
    stage = int(p.get("stage") or 0)
    stage = stage + 1 if independent else max(0, stage - 1)
    _review_close(kind, pid, "done_indep" if independent else "done_struggle")
    due = review_stage_date(stage, (when or "")[:10])
    _review_patch(kind, pid, stage=stage, next_review=due)
    _review_ensure_open(kind, pid)
    return due


def _review_set_next(kind: str, pid: int, due: str) -> dict:
    """手动指定下次复习日：撤掉旧待办，按新日期重建。"""
    r = _review_open(kind, pid)
    if r:
        if r["todo_id"]:
            todo_delete(r["todo_id"], hard=True)
        _review_update(kind, r["id"], status="dropped")
    _review_patch(kind, pid, next_review=(due or "").strip())
    _review_ensure_open(kind, pid)
    return _review_problem(kind, pid)


def _review_sync(kind: str) -> int:
    """启动对账：补齐缺失的复习待办。返回补建/推进的条数。

    待办是「活」的 —— 用户可能删了它，或者没走询问流程就勾掉（拖到已完成），
    也可能排期那天压根没开过应用。
    """
    _review_ensure_list(kind)
    fixed = 0
    for p in _review_active(kind):
        pid = p["id"]
        r = _review_open(kind, pid)
        if r and r["todo_id"]:
            t = todo_get(r["todo_id"])
            if not t or t.get("deleted_at"):
                _review_update(kind, r["id"], status="dropped")
            elif int(t.get("done") or 0):
                # 勾掉了却没走询问：按「做出来了」推进，别把这道题卡死
                _review_update(kind, r["id"], status="done_indep")
                _review_reschedule(kind, pid, True,
                                   t.get("completed_at") or _algo_now())
                fixed += 1
                continue
        if not _review_open(kind, pid):
            _review_ensure_open(kind, pid)
            if _review_open(kind, pid):
                fixed += 1
    return fixed


def review_resolve_todo(todo_id: int, independent: bool) -> tuple[str, dict]:
    """待办页勾掉复习待办时的统一入口：先查是哪一页的复习，再重排。

    直接复用各页自己的「记一次」函数，而不是在这儿重写一遍它们的 SQL ——
    否则以后任何一页改了记账口径，这条路径就会悄悄漏字段。
    返回 (kind, 题目行)；kind 为空串表示这条待办不属于任何复习。
    """
    for kind in ("algo", "interview"):
        r = _review_by_todo(kind, todo_id)
        if not r:
            continue
        pid = r["problem_id"]
        if kind == "algo":
            return kind, algo_log_write(pid, independent=independent)
        return kind, interview_log_attempt(
            pid, "", 0, "correct" if independent else "incorrect")
    return "", {}


def review_kind_of_todo(todo_id: int) -> str:
    """这条待办是不是复习待办、是哪一页的。待办页据此决定要不要问结论。"""
    for kind in ("algo", "interview"):
        if _review_by_todo(kind, todo_id):
            return kind
    return ""


def review_owner_of_todo(todo_id: int) -> tuple[str, int]:
    """返回 (kind, 题目 id)；kind 为空串表示这不是复习待办。

    待办页点一行要用它决定「打开详情编辑」还是「跳到刷题页那道题」——
    复习待办是跳转入口，不是一条能编辑的任务。
    """
    for kind in ("algo", "interview"):
        r = _review_by_todo(kind, todo_id)
        if r:
            return kind, int(r["problem_id"])
    return "", 0
