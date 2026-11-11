"""
人格变迁日志（append-only 流水）。

与快照的区别：
- 快照：某一时刻人格的完整状态（一个版本一个文件，只加载最新）
- 日志：状态之间的"为什么变"的因果记录（只追加，永不修改、永不删除）

AI 之所以能回答"我为什么变了"，靠的就是查询这张表。
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional

import config
from cognitive.models import PersonaChange
from cognitive.storage.database import Database


class PersonaChangelogStore:
    """人格变迁日志存储。"""

    def __init__(self, db: Database, space_id: str) -> None:
        """
        :param db: 数据库访问对象
        :param space_id: 记忆空间
        """
        self.db = db
        self.space_id = space_id

    # ------------------------------------------------------------
    # 追加
    # ------------------------------------------------------------
    def append(self, change: PersonaChange) -> None:
        """
        追加一条变迁记录。

        注意：本表不提供 update / delete 接口，保证溯源链不断裂。

        :param change: 变迁记录
        """
        connection = self.db.conn()
        connection.execute(
            """
            insert or ignore into persona_changelog
                (space_id, log_id, topic_id, persona_field, old_value, new_value,
                 reason, source_type, snapshot_from, snapshot_to, create_ts)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.space_id,
                change.log_id,
                change.topic_id,
                change.persona_field,
                change.old_value,
                change.new_value,
                change.reason,
                change.source_type,
                change.snapshot_from,
                change.snapshot_to,
                change.create_ts,
            ),
        )
        connection.commit()

    # ------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------
    def query(self, field: Optional[str] = None, limit: int = 20) -> List[PersonaChange]:
        """
        按时间倒序查询变迁记录。

        :param field: 限定维度（如 kernel:prefer_skepticism 或 self_view）
        :param limit: 返回条数上限
        :return: 变迁记录列表
        """
        connection = self.db.conn()

        if field:
            rows = connection.execute(
                """
                select c.log_id as log_id, c.topic_id as topic_id, c.persona_field as persona_field,
                       c.old_value as old_value, c.new_value as new_value, c.reason as reason,
                       c.source_type as source_type, c.snapshot_from as snapshot_from,
                       c.snapshot_to as snapshot_to, c.create_ts as create_ts
                from persona_changelog as c
                where c.space_id = ? and c.persona_field = ?
                order by c.create_ts desc
                limit ?
                """,
                (self.space_id, field, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                select c.log_id as log_id, c.topic_id as topic_id, c.persona_field as persona_field,
                       c.old_value as old_value, c.new_value as new_value, c.reason as reason,
                       c.source_type as source_type, c.snapshot_from as snapshot_from,
                       c.snapshot_to as snapshot_to, c.create_ts as create_ts
                from persona_changelog as c
                where c.space_id = ?
                order by c.create_ts desc
                limit ?
                """,
                (self.space_id, limit),
            ).fetchall()

        return [self._row_to_change(row) for row in rows]

    @staticmethod
    def _row_to_change(row: sqlite3.Row) -> PersonaChange:
        """把数据行转换为变迁对象。"""
        return PersonaChange(
            log_id=row["log_id"],
            space_id="",
            topic_id=row["topic_id"] or "",
            persona_field=row["persona_field"] or "",
            old_value=row["old_value"] or "",
            new_value=row["new_value"] or "",
            reason=row["reason"] or "",
            source_type=row["source_type"] or "",
            snapshot_from=int(row["snapshot_from"] or 0),
            snapshot_to=int(row["snapshot_to"] or 0),
            create_ts=int(row["create_ts"] or 0),
        )

    # ------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------
    def render_history(self, limit: int = 10, field: Optional[str] = None) -> str:
        """
        渲染为人可读的变迁叙事，供 AI 自我溯源与 Prompt 注入。

        :param limit: 条数上限
        :param field: 限定维度
        :return: 多行文本
        """
        records = self.query(field=field, limit=limit)
        if not records:
            return "（没有任何人格变迁记录）"

        lines: List[str] = []
        for record in records:
            lines.append(
                f"- 维度：{record.persona_field}\n"
                f"  从：{record.old_value}\n"
                f"  到：{record.new_value}\n"
                f"  原因：{record.reason}\n"
                f"  触发事件：{record.topic_id}"
            )
        return "\n".join(lines)

    def count(self) -> int:
        """统计变迁记录总数。"""
        connection = self.db.conn()
        row = connection.execute(
            """
            select count(1) as cnt
            from persona_changelog as c
            where c.space_id = ?
            """,
            (self.space_id,),
        ).fetchone()
        return int(row["cnt"]) if row else 0
