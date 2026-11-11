"""
主题与经验片段存储。

主题（topic）是"事件级"记忆单元：
- 一段完整交互归为一个 topic
- 该轮产生的经验片段挂在 topic 下
- 该轮产生的 KG 关系通过 source_topic_id 反向关联
召回时以 topic 为单位成套取回，避免碎片化回忆。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import List, Optional

from cognitive.models import Experience, Topic
from cognitive.storage.database import Database


class TopicStore:
    """主题索引记忆存储。"""

    def __init__(self, db: Database) -> None:
        """
        :param db: 数据库访问对象
        """
        self.db = db

    # ------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------
    def save_topic(self, topic: Topic) -> None:
        """
        写入主题记录（同 space_id + topic_id 幂等）。

        :param topic: 主题对象
        """
        connection = self.db.conn()
        connection.execute(
            """
            insert into topic
                (space_id, topic_id, title, abstract, keywords, session_id,
                 snapshot_version, kind, consolidated, is_big_event, create_ts)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict (space_id, topic_id) do update set
                title = excluded.title,
                abstract = excluded.abstract,
                keywords = excluded.keywords,
                snapshot_version = excluded.snapshot_version,
                kind = excluded.kind,
                is_big_event = excluded.is_big_event
            """,
            (
                topic.space_id,
                topic.topic_id,
                topic.title,
                topic.abstract,
                json.dumps(topic.keywords, ensure_ascii=False),
                topic.session_id,
                topic.snapshot_version,
                topic.kind,
                1 if topic.consolidated else 0,
                1 if topic.is_big_event else 0,
                topic.create_ts or int(time.time()),
            ),
        )
        connection.commit()

    def get_topic(self, space_id: str, topic_id: str) -> Optional[Topic]:
        """
        按 id 取回主题。

        :param space_id: 记忆空间
        :param topic_id: 主题 id
        :return: 主题对象，不存在返回 None
        """
        connection = self.db.conn()
        row = connection.execute(
            """
            select t.topic_id as topic_id, t.title as title, t.abstract as abstract,
                   t.keywords as keywords, t.session_id as session_id,
                   t.snapshot_version as snapshot_version, t.kind as kind,
                   t.consolidated as consolidated, t.is_big_event as is_big_event,
                   t.create_ts as create_ts
            from topic as t
            where t.space_id = ? and t.topic_id = ?
            """,
            (space_id, topic_id),
        ).fetchone()

        if not row:
            return None

        return self._row_to_topic(space_id, row)

    def list_topics(self, space_id: str, limit: int = 100) -> List[Topic]:
        """
        按时间倒序列出主题。

        :param space_id: 记忆空间
        :param limit: 返回上限
        :return: 主题列表
        """
        connection = self.db.conn()
        rows = connection.execute(
            """
            select t.topic_id as topic_id, t.title as title, t.abstract as abstract,
                   t.keywords as keywords, t.session_id as session_id,
                   t.snapshot_version as snapshot_version, t.kind as kind,
                   t.consolidated as consolidated, t.is_big_event as is_big_event,
                   t.create_ts as create_ts
            from topic as t
            where t.space_id = ?
            order by t.create_ts desc
            limit ?
            """,
            (space_id, limit),
        ).fetchall()

        return [self._row_to_topic(space_id, row) for row in rows]

    def list_pending_consolidation(
        self,
        space_id: str,
        since_ts: int = 0,
        limit: int = 40,
    ) -> List[Topic]:
        """
        列出待归并的普通主题（供日结使用）。

        过滤条件：
        - kind = 'normal'（不含日结与大事沉淀本身）
        - consolidated = 0（未被上一层归并过）
        - create_ts > since_ts（上次日结之后产生的）

        :param space_id: 记忆空间
        :param since_ts: 上次日结的结束时间戳
        :param limit: 单次最多取多少条
        :return: 主题列表（按时间正序）
        """
        connection = self.db.conn()
        rows = connection.execute(
            """
            select t.topic_id as topic_id, t.title as title, t.abstract as abstract,
                   t.keywords as keywords, t.session_id as session_id,
                   t.snapshot_version as snapshot_version, t.kind as kind,
                   t.consolidated as consolidated, t.is_big_event as is_big_event,
                   t.create_ts as create_ts
            from topic as t
            where t.space_id = ?
              and t.kind = 'normal'
              and t.consolidated = 0
              and t.create_ts > ?
            order by t.create_ts asc
            limit ?
            """,
            (space_id, since_ts, limit),
        ).fetchall()

        return [self._row_to_topic(space_id, row) for row in rows]

    def mark_consolidated(self, space_id: str, topic_ids: List[str]) -> int:
        """
        把一批主题标记为已归并（之后不再参与主题召回，避免与日结重复）。

        注意：SQLite 的 UPDATE 不能带表别名，这里必须写裸表名。

        :param space_id: 记忆空间
        :param topic_ids: 主题 id 列表
        :return: 实际更新条数
        """
        if not topic_ids:
            return 0

        connection = self.db.conn()
        placeholders = ", ".join("?" for _ in topic_ids)
        cursor = connection.execute(
            f"""
            update topic
            set consolidated = 1
            where space_id = ? and topic_id in ({placeholders})
            """,
            (space_id, *topic_ids),
        )
        connection.commit()
        return cursor.rowcount or 0

    def mark_big_event(self, space_id: str, topic_id: str) -> None:
        """
        标记某个主题为大事（供后续统计与专项巩固使用）。

        :param space_id: 记忆空间
        :param topic_id: 主题 id
        """
        connection = self.db.conn()
        connection.execute(
            """
            update topic
            set is_big_event = 1
            where space_id = ? and topic_id = ?
            """,
            (space_id, topic_id),
        )
        connection.commit()

    def count_by_kind(self, space_id: str) -> dict:
        """
        统计各类型主题数量，用于维护面板。

        :param space_id: 记忆空间
        :return: {"normal": n, "daily": n, "event": n, "pending": n}
        """
        connection = self.db.conn()
        rows = connection.execute(
            """
            select t.kind as kind, count(1) as cnt
            from topic as t
            where t.space_id = ?
            group by t.kind
            """,
            (space_id,),
        ).fetchall()

        counts = {"normal": 0, "daily": 0, "event": 0}
        for row in rows:
            counts[str(row["kind"])] = int(row["cnt"])

        pending = connection.execute(
            """
            select count(1) as cnt
            from topic as t
            where t.space_id = ? and t.kind = 'normal' and t.consolidated = 0
            """,
            (space_id,),
        ).fetchone()
        counts["pending"] = int(pending["cnt"] or 0)
        return counts

    @staticmethod
    def _row_to_topic(space_id: str, row: sqlite3.Row) -> Topic:
        """把数据行转换为 Topic 对象。"""
        try:
            keywords = json.loads(row["keywords"] or "[]")
        except json.JSONDecodeError:
            keywords = []

        # 兼容老行：迁移前的数据可能没有这几列
        keys = row.keys()

        return Topic(
            topic_id=row["topic_id"],
            space_id=space_id,
            title=row["title"] or "",
            abstract=row["abstract"] or "",
            keywords=keywords,
            session_id=row["session_id"] or "",
            snapshot_version=int(row["snapshot_version"] or 0),
            kind=str(row["kind"] or "normal") if "kind" in keys else "normal",
            consolidated=bool(row["consolidated"]) if "consolidated" in keys else False,
            is_big_event=bool(row["is_big_event"]) if "is_big_event" in keys else False,
            create_ts=int(row["create_ts"] or 0),
        )

    # ------------------------------------------------------------
    # 经验片段
    # ------------------------------------------------------------
    def add_experience(self, experience: Experience) -> None:
        """
        写入经验片段。

        :param experience: 经验对象
        """
        connection = self.db.conn()
        connection.execute(
            """
            insert or replace into experience (space_id, exp_id, topic_id, content, create_ts)
            values (?, ?, ?, ?, ?)
            """,
            (
                experience.space_id,
                experience.exp_id,
                experience.topic_id,
                experience.content,
                experience.create_ts or int(time.time()),
            ),
        )
        connection.commit()

    def get_experiences_by_topic(self, space_id: str, topic_id: str) -> List[str]:
        """
        取回某个主题下挂载的全部经验片段。

        :param space_id: 记忆空间
        :param topic_id: 主题 id
        :return: 经验文本列表
        """
        connection = self.db.conn()
        rows = connection.execute(
            """
            select x.content as content
            from experience as x
            where x.space_id = ? and x.topic_id = ?
            order by x.create_ts asc
            """,
            (space_id, topic_id),
        ).fetchall()

        return [row["content"] for row in rows]
