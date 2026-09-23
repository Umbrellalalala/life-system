"""SQLite 数据库初始化与连接管理。

可靠性设计：
- WAL 模式：崩溃恢复能力更强，读写并发不互相阻塞；
- busy_timeout：多连接并发写时等待而非直接抛「database is locked」；
- synchronous=NORMAL：WAL 下既安全又快；
- 每次启动自动备份（每天最多一份，保留最近 7 份），防止单文件损坏全丢。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    note TEXT DEFAULT '',
    priority INTEGER DEFAULT 0,
    due_date TEXT DEFAULT '',
    due_time TEXT DEFAULT '',
    done INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    list_name TEXT DEFAULT '收集箱',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    completed_at TEXT DEFAULT '',
    reminder TEXT DEFAULT '',          -- 'yyyy-MM-dd HH:mm'，空=无提醒
    repeat TEXT DEFAULT '',            -- ''/daily/weekly/biweekly/monthly/yearly/workday
    kind TEXT DEFAULT 'task',          -- task=任务 note=笔记（滴答的两种条目）
    deleted_at TEXT DEFAULT '',        -- 非空=在垃圾桶里（软删除时间）
    archived INTEGER DEFAULT 0,        -- 归档：不在常规视图出现
    duration_min INTEGER DEFAULT 0,    -- 时长（分钟），时间轴视图排块用
    pinned INTEGER DEFAULT 0,          -- 置顶：排在进行中的最前面
    abandoned INTEGER DEFAULT 0,       -- 放弃：不做了，既不算完成也不算未完成
    sticky INTEGER DEFAULT 0           -- 便签开着：重启后自动把便签窗放回来
);

CREATE TABLE IF NOT EXISTS todo_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    icon TEXT DEFAULT 'list',
    sort_order INTEGER DEFAULT 0,
    color TEXT DEFAULT '',             -- 十六进制色值，空=默认灰
    folder_id INTEGER DEFAULT 0,       -- 所属文件夹（todo_lists.id，0=无）
    view_kind TEXT DEFAULT 'list',     -- list/kanban/timeline
    kind TEXT DEFAULT 'list',          -- list=清单 folder=文件夹
    hide_in_smart INTEGER DEFAULT 0,   -- 不在智能清单（今天/最近7天…）中显示
    pinned INTEGER DEFAULT 0,          -- 置顶
    archived INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    color TEXT DEFAULT 'blue',         -- QSS 语义色键（chip 配色用）
    hex TEXT DEFAULT '',               -- 导航行圆点的实际色值，空=color 键对应的色
    parent_id INTEGER DEFAULT 0,       -- 父标签（滴答支持层级标签）
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS todo_filters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT DEFAULT 'normal',        -- normal=普通 advanced=高级
    conditions TEXT DEFAULT '{}',      -- JSON：lists/tags/date/priority/keyword/type
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS todo_tags (
    todo_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (todo_id, tag_id)
);

-- 重复任务的「单周期例外」：occ_date 是系列中该周期的原始日期。
-- 只存被改过的周期，未列出的周期按系列规则正常展开。
CREATE TABLE IF NOT EXISTS todo_occ (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id INTEGER NOT NULL,
    occ_date TEXT NOT NULL,                  -- 系列中该周期的原始日期 yyyy-MM-dd
    status TEXT DEFAULT '',                  -- ''=正常 done=该周期已完成 skipped=该周期已删除
    new_date TEXT DEFAULT '',                -- 仅此周期改期到某天，空=未改期
    new_time TEXT DEFAULT '',                -- 仅此周期的时间覆盖
    UNIQUE(todo_id, occ_date)
);

CREATE TABLE IF NOT EXISTS subtasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    done INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pomodoro (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT DEFAULT (datetime('now', 'localtime')),
    duration_min INTEGER DEFAULT 25,
    task TEXT DEFAULT '',
    completed INTEGER DEFAULT 1,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS weight (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    weight REAL NOT NULL,
    body_fat REAL,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS finance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    category TEXT DEFAULT '',
    amount REAL NOT NULL,
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS research (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    field TEXT DEFAULT '',
    -- 科研路线步骤：s1..s6 + done（旧值 idea/reading/… 由 init_db 一次性迁移）
    status TEXT DEFAULT 's1',
    priority INTEGER DEFAULT 1,
    notes TEXT DEFAULT '',
    due_date TEXT DEFAULT '',
    -- 投稿目标：会议/期刊名 + 截稿日期 + 本人作者角色
    venue TEXT DEFAULT '',
    venue_deadline TEXT DEFAULT '',
    role TEXT DEFAULT '',
    -- 逗号分隔：番茄钟里任务名命中这些词的专注记录算作该课题的投入
    focus_keywords TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 课题下的自定义 DDL（实验/消融/baseline/开会汇报…），可勾选同步成一条待办
-- todo_id 记录同步出去的待办主键，改期/删除时按它回写，0 表示没同步
CREATE TABLE IF NOT EXISTS research_milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    due_date TEXT DEFAULT '',
    note TEXT DEFAULT '',
    done INTEGER DEFAULT 0,
    todo_id INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 课题 ↔ Arxiver 论文的关联（只存 arxiv_id 与标题快照，
-- 摘要/中文标题/本地 PDF 等一律实时只读 ~/.arxiver/library.db，避免两处数据打架）
CREATE TABLE IF NOT EXISTS research_papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    arxiv_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    added_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(project_id, arxiv_id)
);

-- 算法刷题：完全是我自己录入的记录，不含任何抓来的第三方正文。
-- 四张表分工：题目本体 / 多条题解 / 历次写作 / 复习点与待办的对应关系。
CREATE TABLE IF NOT EXISTS algo_problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    tags TEXT DEFAULT '',              -- 逗号分隔，如「二叉树,递归」
    lc_ref TEXT DEFAULT '',            -- 力扣题号或 slug（题号「15」或「three-sum」）
    url TEXT DEFAULT '',               -- 题目链接，待办和详情页都靠它跳浏览器
    note TEXT DEFAULT '',              -- 备注
    solved INTEGER DEFAULT 0,          -- 写过没有
    last_written_at TEXT DEFAULT '',   -- 最后一次写的时间 yyyy-MM-dd HH:MM
    stage INTEGER DEFAULT 0,           -- 当前处在艾宾浩斯第几档
    next_review TEXT DEFAULT '',       -- 下次复习日期 yyyy-MM-dd，空=不再排
    archived INTEGER DEFAULT 0,        -- 归档：不再排复习，记录仍保留
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS algo_solutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL,
    language TEXT DEFAULT 'cpp',       -- 见 services.SOLUTION_LANGUAGES
    idea TEXT DEFAULT '',              -- 思路：怎么想到的、复杂度、坑
    body TEXT NOT NULL DEFAULT '',     -- 解法正文 / 代码（我自己写的）
    source TEXT DEFAULT '',            -- 这解法的出处说明或链接
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 历次写作：次数就是行数，时间线靠 written_at。复习结论也记在这里。
CREATE TABLE IF NOT EXISTS algo_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL,
    written_at TEXT NOT NULL,          -- yyyy-MM-dd HH:MM
    independent INTEGER DEFAULT 1,     -- 1=独立做出来，0=没独立做出来
    note TEXT DEFAULT ''
);

-- 复习点 ↔ 待办 一一对应。todo_id 是排期落地的凭据：勾掉了哪条待办，
-- 才知道该按「独立/不独立」重排下一档，也才不会重复建待办。
CREATE TABLE IF NOT EXISTS algo_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL,
    todo_id INTEGER NOT NULL DEFAULT 0,
    stage INTEGER DEFAULT 0,
    due_date TEXT DEFAULT '',
    status TEXT DEFAULT 'open',        -- open/done_indep/done_struggle/dropped
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 一道题同一时刻只允许挂着一条未完成的复习待办（艾宾浩斯只排下一个点）。
CREATE UNIQUE INDEX IF NOT EXISTS algo_reviews_open
    ON algo_reviews(problem_id) WHERE status = 'open';

-- 八股刷题：题库同样是用户自己上传/录入的，一题一条标准答案。
-- 比算法页多一组「答题表现」字段（对/错/已见次数、最高相似度分、上次见到时间），
-- 因为这一页的核心是抽题自答，不只是记录。
CREATE TABLE IF NOT EXISTS interview_problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL DEFAULT '',   -- 可以留空：只记题号也算一道题
    answer TEXT NOT NULL DEFAULT '',     -- 标准答案（文字版），打分拿它当参照
    lc_ref TEXT DEFAULT '',              -- 力扣题号或 slug
    tags TEXT DEFAULT '',
    url TEXT DEFAULT '',                 -- 参考链接（博客/文档），可点跳浏览器
    note TEXT DEFAULT '',
    correct_count INTEGER DEFAULT 0,
    incorrect_count INTEGER DEFAULT 0,
    seen_count INTEGER DEFAULT 0,
    best_score INTEGER DEFAULT 0,        -- 历次作答的最高相似度分
    last_seen_at TEXT DEFAULT '',        -- yyyy-MM-dd HH:MM
    has_been_correct INTEGER DEFAULT 0,  -- 本轮是否已答对过（重开一轮会清零）
    stage INTEGER DEFAULT 0,
    next_review TEXT DEFAULT '',
    archived INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 解题代码：一题可以有多种语言、同一种语言也可以有多版解法。
-- idea 是这版解法的思路，跟代码存在一起，复习时不用再来回翻。
CREATE TABLE IF NOT EXISTS interview_solutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL,
    language TEXT DEFAULT 'cpp',         -- 见 services.SOLUTION_LANGUAGES
    body TEXT DEFAULT '',
    idea TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 每次作答都留一条：用户自己写的答案原文要存，回头才知道当时错在哪。
CREATE TABLE IF NOT EXISTS interview_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL,
    answered_at TEXT NOT NULL,           -- yyyy-MM-dd HH:MM
    user_answer TEXT DEFAULT '',
    score INTEGER DEFAULT 0,
    decision TEXT DEFAULT 'skip'         -- correct/incorrect/skip
);

-- 复习点 ↔ 待办。与算法复习各用各的清单，互不干扰。
CREATE TABLE IF NOT EXISTS interview_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL,
    todo_id INTEGER NOT NULL DEFAULT 0,
    stage INTEGER DEFAULT 0,
    due_date TEXT DEFAULT '',
    status TEXT DEFAULT 'open',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS interview_reviews_open
    ON interview_reviews(problem_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'self',
    channel TEXT NOT NULL,
    amount REAL NOT NULL,
    date TEXT NOT NULL,
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    icon TEXT DEFAULT '😊',
    color TEXT DEFAULT 'green',
    freq_type TEXT DEFAULT 'daily',      -- daily=每天 / weekly=每周指定日
    freq_days TEXT DEFAULT '',           -- weekly 时逗号分隔 1-7（周一=1）
    goal_type TEXT DEFAULT 'check',      -- check=当天完成打卡 / amount=当天完成一定量
    goal_per_day INTEGER DEFAULT 1,      -- 每天 N 次
    goal_auto INTEGER DEFAULT 1,         -- 打卡时自动记录（1）/ 手动记录（0）
    goal_each INTEGER DEFAULT 1,         -- 每次记录 N 次
    start_date TEXT DEFAULT '',          -- yyyy-MM-dd
    target_days INTEGER DEFAULT 0,       -- 0=永远，否则坚持 N 天
    group_name TEXT DEFAULT '其他',
    reminder TEXT DEFAULT '',            -- HH:MM 或空
    auto_log INTEGER DEFAULT 0,          -- 打卡后自动弹出日志
    archived INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS habit_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id INTEGER NOT NULL,
    date TEXT NOT NULL,                  -- yyyy-MM-dd
    count INTEGER DEFAULT 1,             -- 当日打卡次数
    note TEXT DEFAULT '',                -- 打卡日志
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(habit_id, date)
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    date TEXT NOT NULL,                  -- yyyy-MM-dd
    start_time TEXT DEFAULT '',          -- HH:MM 或空（全天事件）
    end_time TEXT DEFAULT '',            -- HH:MM 或空
    category TEXT DEFAULT 'default',     -- 事件分类（配色分组）
    note TEXT DEFAULT '',
    completed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);

-- 提醒去重：一条提醒（待办）或某天打卡提醒（习惯）只响一次。
-- slot 对待办存 'yyyy-MM-dd HH:mm'，对习惯存 'yyyy-MM-dd'。
-- 存库而不是存内存，是为了重启后不把已经响过的提醒再响一遍。
CREATE TABLE IF NOT EXISTS notified (
    kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    slot TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (kind, ref_id, slot)
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(config.db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发写不立即报错，等待锁释放（最多 5 秒）
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        # 事务内出错回滚，避免半写状态污染数据；异常继续上抛交由调用方处理
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_columns(conn, table: str, cols: dict) -> None:
    """给已存在的旧表补列（``CREATE TABLE IF NOT EXISTS`` 不会改已有表）。"""
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_research_route(conn) -> None:
    """把旧的阶段值（idea/reading/…）映射到 6 步科研路线。

    一次性：迁完在 settings 里记个标记，避免每次启动都改写用户后来存的值。
    """
    done = conn.execute(
        "SELECT value FROM settings WHERE key='research_route_migrated'"
    ).fetchone()
    if done:
        return
    mapping = {"idea": "s1", "reading": "s2", "experiment": "s3",
               "writing": "s6", "done": "done"}
    for old, new in mapping.items():
        conn.execute("UPDATE research SET status = ? WHERE status = ?", (new, old))
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('research_route_migrated', '1')")


def init_db() -> None:
    with connect() as conn:
        # 切换 WAL 需在无事务状态下执行
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_SCHEMA)
        _ensure_columns(conn, "todos", {
            "sort_order": "INTEGER DEFAULT 0",
            "list_name": "TEXT DEFAULT '收集箱'",
            "due_time": "TEXT DEFAULT ''",
            "created_at": "TEXT DEFAULT (datetime('now', 'localtime'))",
            "reminder": "TEXT DEFAULT ''",
            "repeat": "TEXT DEFAULT ''",
            "kind": "TEXT DEFAULT 'task'",
            "deleted_at": "TEXT DEFAULT ''",
            "archived": "INTEGER DEFAULT 0",
            "duration_min": "INTEGER DEFAULT 0",
            "pinned": "INTEGER DEFAULT 0",
            "abandoned": "INTEGER DEFAULT 0",
            "sticky": "INTEGER DEFAULT 0",
        })
        _ensure_columns(conn, "todo_lists", {
            "color": "TEXT DEFAULT ''",
            "folder_id": "INTEGER DEFAULT 0",
            "view_kind": "TEXT DEFAULT 'list'",
            "kind": "TEXT DEFAULT 'list'",
            "hide_in_smart": "INTEGER DEFAULT 0",
            "pinned": "INTEGER DEFAULT 0",
            "archived": "INTEGER DEFAULT 0",
        })
        _ensure_columns(conn, "tags", {
            "hex": "TEXT DEFAULT ''",
            "parent_id": "INTEGER DEFAULT 0",
            "sort_order": "INTEGER DEFAULT 0",
        })
        # 老库的 papers 表留着不删（历史数据），但代码已不再读写它：
        # 论文库改由 Arxiver 负责，这里只补课题需要的列。
        _ensure_columns(conn, "research", {
            "focus_keywords": "TEXT DEFAULT ''",
            "venue": "TEXT DEFAULT ''",
            "venue_deadline": "TEXT DEFAULT ''",
            "role": "TEXT DEFAULT ''",
        })
        _ensure_columns(conn, "interview_problems", {
            # 老库里 question 是 NOT NULL 且没有 lc_ref；空串满足 NOT NULL，
            # 所以「只标题号」不需要重建表，只要补上这一列
            "lc_ref": "TEXT DEFAULT ''",
        })
        _ensure_columns(conn, "algo_solutions", {
            "language": "TEXT DEFAULT 'cpp'",
            "idea": "TEXT DEFAULT ''",
        })
        _migrate_research_route(conn)
        wcols = {r["name"] for r in conn.execute("PRAGMA table_info(weight)")}
        if "body_fat" not in wcols:
            conn.execute("ALTER TABLE weight ADD COLUMN body_fat REAL")
        fcols = {r["name"] for r in conn.execute("PRAGMA table_info(finance)")}
        if "account" not in fcols:
            conn.execute("ALTER TABLE finance ADD COLUMN account TEXT DEFAULT ''")
        if "source" not in fcols:
            conn.execute("ALTER TABLE finance ADD COLUMN source TEXT DEFAULT ''")
        if "owner" not in fcols:
            conn.execute("ALTER TABLE finance ADD COLUMN owner TEXT DEFAULT 'self'")
        # 补录专注记录支持「专注笔记」（旧库没有这一列）
        mcols = {r["name"] for r in conn.execute("PRAGMA table_info(pomodoro)")}
        if "note" not in mcols:
            conn.execute("ALTER TABLE pomodoro ADD COLUMN note TEXT DEFAULT ''")
        # 一次性迁移：优先级从三档(0低/1中/2高)扩为四档(0无/1低/2中/3高)
        migrated = conn.execute(
            "SELECT value FROM settings WHERE key = 'todo_p4_migrated'").fetchone()
        if not migrated:
            conn.execute("UPDATE todos SET priority = priority + 1")
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('todo_p4_migrated', '1')")
        _merge_legacy_calendar_events(conn)


# 旧版日历用独立的 calendar_events 表，与待办完全不通；
# 日历改成「任务的时间轴视图」后，这些事件必须并入 todos 才不会凭空消失。
_LEGACY_CATEGORY_LISTS = {
    "default": "收集箱",
    "work": "工作",
    "personal": "个人",
    "study": "学习",
    "health": "健康",
}


def _merge_legacy_calendar_events(conn) -> None:
    """把 calendar_events 里的历史事件一次性搬进 todos（分类转成同名清单）。"""
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='calendar_events'").fetchone()
    done_flag = conn.execute(
        "SELECT value FROM settings WHERE key='cal_events_merged'").fetchone()
    if not has_table or done_flag:
        return
    rows = conn.execute(
        "SELECT * FROM calendar_events ORDER BY date, start_time").fetchall()
    for r in rows:
        list_name = _LEGACY_CATEGORY_LISTS.get(r["category"], "收集箱")
        if list_name != "收集箱" and not conn.execute(
                "SELECT id FROM todo_lists WHERE name=?", (list_name,)).fetchone():
            conn.execute(
                "INSERT INTO todo_lists(name, icon, sort_order) "
                "VALUES(?, 'list', 0)", (list_name,))
        duration = 0
        if r["start_time"] and r["end_time"]:
            try:
                sh, sm = (int(x) for x in r["start_time"].split(":")[:2])
                eh, em = (int(x) for x in r["end_time"].split(":")[:2])
                duration = max(0, (eh * 60 + em) - (sh * 60 + sm))
            except ValueError:
                duration = 0
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM todos").fetchone()["m"]
        conn.execute(
            "INSERT INTO todos(title, note, priority, due_date, due_time, done, "
            "sort_order, list_name, duration_min, completed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (r["title"], r["note"], 0, r["date"], r["start_time"],
             int(r["completed"] or 0), max_order + 1, list_name, duration,
             r["date"] if r["completed"] else ""))
    # 改名保留而不是删表：迁移若有偏差还能人工回查原始事件
    conn.execute("ALTER TABLE calendar_events RENAME TO calendar_events_legacy")
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('cal_events_merged', '1')")


def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# 自动备份
# ---------------------------------------------------------------------------
_BACKUP_KEEP = 7  # 保留最近 N 份


def backup_dir() -> str:
    path = os.path.join(config.data_dir(), "backups")
    os.makedirs(path, exist_ok=True)
    return path


def backup_db() -> str | None:
    """备份数据库到 data/backups/，每天最多一份，保留最近 7 份。

    用 SQLite 官方 backup API 做在线热备份，即使应用运行中也能保证一致性。
    返回本次生成的备份路径；若当天已有备份则返回 None。
    """
    src = config.db_path()
    if not os.path.exists(src):
        return None
    stamp = datetime.now().strftime("%Y%m%d")
    dest = os.path.join(backup_dir(), f"life_{stamp}.db")
    if os.path.exists(dest):
        return None  # 今天已备份过
    src_conn = None
    dest_conn = None
    try:
        dest_conn = sqlite3.connect(dest)
        src_conn = sqlite3.connect(src)
        src_conn.execute("PRAGMA busy_timeout = 5000")
        src_conn.backup(dest_conn)
        # 必须先关目标连接、再关源连接，否则源库的 WAL 会残留
        # wal/shm 文件被锁定，影响后续损坏检测与隔离。
        dest_conn.close()
        dest_conn = None
        src_conn.close()
        src_conn = None
        _prune_backups()
        return dest
    except Exception:
        # 备份失败不应阻断启动；清理可能产生的半成品文件
        if dest_conn is not None:
            try:
                dest_conn.close()
            except Exception:
                pass
        if src_conn is not None:
            try:
                src_conn.close()
            except Exception:
                pass
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
        return None


def _prune_backups() -> None:
    """只保留最近 _BACKUP_KEEP 份备份，删除更早的。"""
    try:
        files = [f for f in os.listdir(backup_dir()) if f.startswith("life_") and f.endswith(".db")]
        files.sort(reverse=True)
        for f in files[_BACKUP_KEEP:]:
            try:
                os.remove(os.path.join(backup_dir(), f))
            except OSError:
                pass
    except OSError:
        pass


def _clear_sidecars(db_path: str) -> None:
    """删除 WAL 模式下的 -wal / -shm 附属文件（恢复/隔离前清理，避免污染）。"""
    for suffix in ("-wal", "-shm"):
        try:
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def restore_latest_backup() -> bool:
    """从最新备份恢复数据库（当前库损坏时的兜底）。返回是否成功。"""
    try:
        files = [f for f in os.listdir(backup_dir()) if f.startswith("life_") and f.endswith(".db")]
        if not files:
            return False
        files.sort(reverse=True)
        src = os.path.join(backup_dir(), files[0])
        if not os.path.exists(src):
            return False
        # 恢复前清理残留的 WAL/SHM，避免旧日志污染恢复后的主库
        _clear_sidecars(config.db_path())
        shutil.copy2(src, config.db_path())
        return True
    except OSError:
        return False


def _integrity_ok(path: str) -> bool:
    """对指定数据库文件做完整性检查（PRAGMA integrity_check）。"""
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return row is not None and str(row[0]).lower() == "ok"
        finally:
            conn.close()
    except Exception:
        return False


def ensure_healthy() -> None:
    """启动时检查主库完整性；损坏则尝试从最新备份自动恢复。

    必须在 ``init_db()`` 之前调用（此时文件可能已存在但尚无表结构）。
    恢复流程：
    1. 主库完好 → 直接返回；
    2. 主库损坏且有备份 → 从最新备份恢复；
    3. 主库损坏且无备份 → 把损坏文件隔离为 ``.corrupt``，让 ``init_db``
       重建空库，保证应用至少能启动，而不是直接崩溃。
    """
    src = config.db_path()
    if not os.path.exists(src):
        return
    if _integrity_ok(src):
        return
    if restore_latest_backup():
        return
    # 无备份可恢复：隔离损坏文件并清理 WAL/SHM，让 init_db 重建空库
    _clear_sidecars(src)
    try:
        os.replace(src, src + ".corrupt")
    except OSError:
        pass
