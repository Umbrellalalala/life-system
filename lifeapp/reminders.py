"""到点提醒：把「存了但从来不响」的 todos.reminder / habits.reminder 接上。

之前这两个字段只写库、只在界面上显示一枚图标，用户以为设了提醒就会被告知，
实际什么都不会发生 —— 属于「界面在撒谎」那一类。

设计上的两个约束：
  * 只依赖托盘的 showMessage，所以主窗口收进托盘时照样能弹；
  * 去重记在 notified 表里（不是内存集合），重启后不会把响过的再响一遍。
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDate, QObject, QTimer, Signal

from . import services

CHECK_MS = 30 * 1000
# 启动时补发错过的提醒，但只补这段时间内的，免得把上周的旧账一次性弹出来
GRACE_HOURS = 6


def _at(text: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime((text or "").strip(), fmt)
    except (ValueError, TypeError):
        return None


class ReminderService(QObject):
    """每 30 秒扫一遍待办和习惯的提醒时间点。"""

    fired = Signal(str, int, str, str)      # (kind, id, title, body)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_MS)
        self._timer.timeout.connect(self.tick)

    def start(self) -> None:
        self._timer.start()
        self.tick()                          # 启动即补一次错过的

    def tick(self, now: datetime | None = None) -> int:
        now = now or datetime.now()
        hits = 0
        hits += self._scan_todos(now)
        hits += self._scan_habits(now)
        if hits:
            services.prune_notified()
        return hits

    def _scan_todos(self, now: datetime) -> int:
        hits = 0
        today = now.strftime("%Y-%m-%d")
        # 只有真带重复规则的待办才需要「今天在不在系列上」这张表，懒着建
        todays: dict[int, dict] | None = None
        for t in services.todo_list():
            slot = (t.get("reminder") or "").strip()
            when = _at(slot, "%Y-%m-%d %H:%M")
            if when is None:
                continue
            if int(t.get("done") or 0) or int(t.get("abandoned") or 0):
                # 放弃的和做完的一样：这事已经翻篇了，别再催
                continue
            if (t.get("repeat") or "").strip():
                # 重复任务的提醒必须每天重新锚一次。reminder 存的是绝对时刻
                # （'2026-09-24 09:00'），而 notified 按这个字符串去重 —— 于是
                # 「每天 9 点催我」只在第一天响过一次，往后每天都撞在同一个键上
                # 被吞掉，用户只会以为提醒坏了。日期换成今天，点钟留着。
                if todays is None:
                    todays = {r["id"]: r for r in
                              services.cal_occurrences(today, today)}
                occ = todays.get(int(t["id"]))
                if occ is None or occ["done"]:
                    continue        # 今天不是目标日，或这一周期已经勾掉了
                slot = "%s %s" % (today, slot[11:16])
                when = _at(slot, "%Y-%m-%d %H:%M")
            if when is None or when > now:
                continue
            if (now - when).total_seconds() > GRACE_HOURS * 3600:
                continue
            if services.mark_notified("todo", t["id"], slot):
                self.fired.emit("todo", t["id"], "待办提醒",
                                "%s（%s）" % (t["title"], slot[5:]))
                hits += 1
        return hits

    def _scan_habits(self, now: datetime) -> int:
        hits = 0
        today = now.strftime("%Y-%m-%d")
        qday = QDate(now.year, now.month, now.day)   # habit_due_on 要的是 QDate
        due = []
        for h in services.habit_list():
            if h.get("archived"):
                continue
            hhmm = (h.get("reminder") or "").strip()
            when = _at("%s %s" % (today, hhmm), "%Y-%m-%d %H:%M")
            if when is None or when > now:
                continue
            if (now - when).total_seconds() > GRACE_HOURS * 3600:
                continue            # 和待办同一条规矩：太旧的旧账不补
            # 不是目标日（比如每周一三五的习惯）就不催
            if not services.habit_due_on(h, qday):
                continue
            due.append(h)
        if not due:
            return hits
        # 打过卡了还催就是骚扰。以前只按点到没点到判，晚上十一点还在弹早上
        # 那个点，而那件事其实下午就做完了
        checks = services.habit_checks_maps([h["id"] for h in due])
        for h in due:
            if services.habit_is_done(h, checks.get(h["id"], {}), today):
                continue
            if services.mark_notified("habit", h["id"], today):
                self.fired.emit("habit", h["id"], "习惯打卡提醒", h["name"])
                hits += 1
        return hits
