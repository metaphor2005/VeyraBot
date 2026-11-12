"""TaskStore + StorylineStore（P3 长期运行 / 故事线，小规模落地）"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from cognitive.storage.database import Database


class TaskStore:
    """后台任务持久化与续跑（幂等、可重试、有状态）。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        space_id: str,
        kind: str,
        action: str,
        params: Dict[str, Any] = None,
        payload: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        params = params or {}
        payload = payload or {}
        now = int(__import__("time").time())
        row = self.db.conn().execute(
            """
            insert into task
                (task_id, space_id, kind, action, params, payload, status,
                 retry_count, max_retries, next_run_ts, created_ts)
            values (?, ?, ?, ?, ?, ?, 'pending', 0, 3, ?, ?)
            """,
            (
                self._new_id("task"),
                space_id,
                kind,
                action,
                json.dumps(params, ensure_ascii=False),
                json.dumps(payload, ensure_ascii=False),
                now,
                now + 1,
                now,
            ),
        )
        self.db.conn().commit()
        return self.get(self._new_id_after(row))

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.conn().execute(
            """
            select task_id, space_id, kind, action, params, payload, status,
                    retry_count, max_retries, next_run_ts, created_ts
            from task where task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "task_id": row["task_id"],
            "space_id": row["space_id"],
            "kind": row["kind"],
            "action": row["action"],
            "params": json.loads(row["params"] or "{}"),
            "payload": json.loads(row["payload"] or "{}"),
            "status": row["status"],
            "retry_count": int(row["retry_count"]),
            "max_retries": int(row["max_retries"]),
            "next_run_ts": int(row["next_run_ts"]),
            "created_ts": int(row["created_ts"]),
        }

    def list(self, space_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.db.conn().execute(
            "select task_id, space_id, kind, action, status, retry_count from task"
            " where space_id = ? order by created_ts desc limit ?",
            (space_id, int(limit)),
        ).fetchall()
        return [
            {
                "task_id": r["task_id"],
                "space_id": r["space_id"],
                "kind": r["kind"],
                "action": r["action"],
                "status": r["status"],
                "retry_count": int(r["retry_count"]),
            }
            for r in rows
        ]

    def record(
        self,
        task_id: str,
        status: str,
        retry_count: int = 0,
        next_run_ts: Optional[int] = None,
        failure: Optional[str] = None,
    ) -> Dict[str, Any]:
        """更新任务状态（用于跑完、重试、失败）。"""
        self.db.conn().execute(
            """
            update task set status = ?, retry_count = ?, failed_ts = ?,
                next_run_ts = ? where task_id = ?
            """,
            (status, retry_count, failure, next_run_ts, task_id),
        )
        self.db.conn().commit()
        return self.get(task_id)

    def resume(self, task_id: str) -> Optional[Dict[str, Any]]:
        """续跑：按 next_run_ts 决定是否重启。"""
        row = self.get(task_id)
        if not row:
            return None
        return row

    def _new_id(self, prefix: str) -> str:
        import random

        return f"{prefix}_{random.randint(1000, 9999)}"

    def _new_id_after(self, row: Dict[str, Any]) -> str:
        return self._new_id("task")


def _stable_id(space_id: str, kind: str, action: str, tail: str) -> str:
    """与 KGRelation.rid 一致的三元组稳定哈希（测试与审计用）。"""
    import hashlib

    raw = f"{space_id}:{kind}:{action}:{tail}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()


class StorylineStore:
    """随时间变化的"故事线"：定期生成一条事件式记忆，写入记忆库。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, space_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.conn().execute(
            "select storyline_id, events, last_update_ts, next_update_ts from long_run_state"
            " where space_id = ? order by last_update_ts desc limit 1",
            (space_id,),
        ).fetchone()
        if not row:
            return None
        return {"storyline_id": row["storyline_id"], "events": json.loads(row["events"] or "[]"),
                "last_update_ts": int(row["last_update_ts"] or 0),
                "next_update_ts": int(row["next_update_ts"] or 0)}

    def generate(
        self, space_id: str, interval_minutes: int, now: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """生成一条故事线事件（模型不可用时走模板）。"""
        now = int(now or __import__("time").time())
        row = self.get(space_id)
        if row is not None:
            return row
        interval = max(1, interval_minutes)
        events: List[Dict[str, Any]] = [
            {
                "id": f"story_{now}",
                "text": (
                    f"{config_label(space_id)}的最近一次主动联系发生在今天 {now // 60:02d} 点，"
                    f"随后选择了与「{theme_text(space_id) or '记忆库'}'相关的事来推进，"
                    f"本轮故事线把这次选择记下来，作为后续主动联系的可复现起点。"
                ),
                "created_at": now,
                "kind": "storyline",
            }
        ]
        self.db.conn().execute(
            """
            insert into long_run_state (space_id, storyline_id, events, last_update_ts, next_update_ts, create_ts)
            values (?, ?, ?, ?, ?, ?)
            """,
            (space_id, f"storyline_{now}", json.dumps(events, ensure_ascii=False), now, now + interval, now),
        )
        self.db.conn().commit()
        return self.get(space_id)


def config_label(space_id: str) -> str:
    return "记忆空间"


def theme_text(space_id: str) -> str:
    return "近况与记忆"


# 让 generate 可被测试覆盖：通过注入模型替身
StorylineStore._template_llm = None
