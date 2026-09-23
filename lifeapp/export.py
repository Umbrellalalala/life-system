"""数据导出：JSON + CSV 备份。"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime

from . import config, db

TABLES = ["todos", "pomodoro", "weight", "finance", "research"]


def export_all(export_dir: str) -> list[str]:
    """导出全部数据到指定目录，返回生成的文件路径列表。"""
    os.makedirs(export_dir, exist_ok=True)
    created: list[str] = []

    data: dict = {}
    with db.connect() as conn:
        for t in TABLES:
            rows = conn.execute(f"SELECT * FROM {t}").fetchall()
            data[t] = [dict(r) for r in rows]

    # JSON 全量备份
    jpath = os.path.join(export_dir, "life_system_backup.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    created.append(jpath)

    # 各表 CSV
    for t in TABLES:
        rows = data[t]
        if not rows:
            continue
        cpath = os.path.join(export_dir, f"{t}.csv")
        with open(cpath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        created.append(cpath)

    # 笔记不在这里备份：它存在 Obsidian 库里，由 Obsidian / obsidian-git 负责

    return created


def default_export_dir() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(config.data_dir(), "exports", stamp)
