"""
好感度存储与主动消息发件箱。

好感度是"社会情感"的数值化：每个对话对象维护一条 0~100 的温度值，
由归档抽取的 affection_deltas 驱动（AI 基于本轮对话的人际互动给出增量）。

状态机（数值与状态分离，状态只在跨过阈值时迁移）：

    normal  (>= COLD)
      ↓ 好感降到 COLD 以下
    cold    (SEVER <= value < COLD) —— 触发主动联系（reach_out）
      ↓ 好感继续降到 SEVER 以下
    broken  (< SEVER) —— 触发断交声明（severance），此后不再主动联系
      ↑ 好感回升到 RECONCILE 以上
    normal  —— 触发和解消息（reconcile）

设计取舍：
- 状态迁移以"事件"形式返回给调用方，不在存取时偷偷做副作用；
- 断交后好感依然可以继续变化：和解要跨过更高的门槛（RECONCILE > COLD），
  不是回到"不冷"就自动翻篇；
- cold → normal 的恢复是安静的（不发消息），避免消息轰炸；
- 主动消息写入 outbox（proactive_message 表），由 API / UI 层拉取，
  不阻塞对话主链路；
- 模型组稿失败时由上层走文字模板降级，本层不感知模型。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import config
from cognitive.models import new_id
from cognitive.storage.database import Database


class AffectionStore:
    """好感度读取、演化与状态机。"""

    def __init__(self, db: Database) -> None:
        """
        :param db: 数据库访问对象
        """
        self.db = db

    # ------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------
    def get(self, space_id: str, person_name: str) -> Optional[Dict[str, Any]]:
        """
        读取一个人的好感度记录。

        :param space_id: 记忆空间
        :param person_name: 对方名称
        :return: {"person_name","value","status","reason","update_ts"}；不存在返回 None
        """
        row = self.db.conn().execute(
            """
            select person_name, value, status, reason, update_ts
            from affection
            where space_id = ? and person_name = ?
            """,
            (space_id, person_name),
        ).fetchone()
        if row is None:
            return None
        return {
            "person_name": row["person_name"],
            "value": float(row["value"]),
            "status": row["status"],
            "reason": row["reason"] or "",
            "update_ts": int(row["update_ts"] or 0),
        }

    def list_people(self, space_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        列出该空间全部好感度记录（按好感度升序：关系最紧张的排前面）。

        :param space_id: 记忆空间
        :param limit: 条数上限
        :return: 记录列表
        """
        sql = """
            select person_name, value, status, reason, update_ts
            from affection
            where space_id = ?
            order by value asc, update_ts desc
        """
        params: list[Any] = [space_id]
        if limit:
            sql += " limit ?"
            params.append(int(limit))

        rows = self.db.conn().execute(sql, params).fetchall()
        return [
            {
                "person_name": row["person_name"],
                "value": float(row["value"]),
                "status": row["status"],
                "reason": row["reason"] or "",
                "update_ts": int(row["update_ts"] or 0),
            }
            for row in rows
        ]

    # ------------------------------------------------------------
    # 演化（状态机）
    # ------------------------------------------------------------
    def apply_delta(
        self,
        space_id: str,
        person_name: str,
        delta: float,
        reason: str = "",
        topic_id: str = "",
    ) -> Dict[str, Any]:
        """
        对某人应用一次好感度变化，并执行状态机迁移。

        :param space_id: 记忆空间
        :param person_name: 对方名称
        :param delta: 增量（正负皆可；单轮上限由抽取层裁剪，此处只做 0~100 夹取）
        :param reason: 变化原因（来自抽取结果）
        :param topic_id: 触发事件的主题 id
        :return: {"before","after","events"}，
                 events 是本次跨过的阈值事件列表（"cold" / "severed" / "reconciled"）
        """
        connection = self.db.conn()
        now = int(time.time())

        row = connection.execute(
            """
            select value, status, reason from affection
            where space_id = ? and person_name = ?
            """,
            (space_id, person_name),
        ).fetchone()

        if row is None:
            before_value = config.AFFECTION_DEFAULT
            before_status = "normal"
            old_reason = ""
        else:
            before_value = float(row["value"])
            before_status = str(row["status"] or "normal")
            old_reason = row["reason"] or ""

        after_value = max(0.0, min(100.0, before_value + float(delta)))
        after_status, events = self._transition(before_status, before_value, after_value)

        # 原因只在新理由非空时覆盖，小步微调不会抹掉上一次显著变化的记录
        stored_reason = reason if reason else old_reason

        connection.execute(
            """
            insert into affection
                (space_id, person_name, value, status, reason, create_ts, update_ts)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict (space_id, person_name) do update set
                value = excluded.value,
                status = excluded.status,
                reason = excluded.reason,
                update_ts = excluded.update_ts
            """,
            (
                space_id,
                person_name,
                after_value,
                after_status,
                stored_reason,
                now,
                now,
            ),
        )
        connection.commit()

        # 事件的 reason 落库在行上；这里返回完整轨迹供上层组稿与展示
        return {
            "before": before_value,
            "after": after_value,
            "status": after_status,
            "events": events,
        }

    @staticmethod
    def _transition(
        before_status: str, before_value: float, after_value: float
    ) -> Tuple[str, List[str]]:
        """
        计算状态迁移（纯函数，便于单测）。

        :param before_status: 迁移前状态
        :param before_value: 迁移前好感度
        :param after_value: 迁移后好感度
        :return: (新状态, 事件列表)
        """
        cold = config.AFFECTION_COLD_THRESHOLD
        sever = config.AFFECTION_SEVER_THRESHOLD
        reconcile = config.AFFECTION_RECONCILE_THRESHOLD

        if before_status == "broken":
            if after_value >= reconcile:
                # 断交后跨过和解线 → 和解（发出和解消息）
                return "normal", ["reconciled"]
            return "broken", []

        if before_status == "cold":
            if after_value < sever:
                # 冷淡继续恶化 → 断交（cold 已宣布过，这里只发断交）
                return "broken", ["severed"]
            if after_value >= cold:
                # 回暖出冷淡区：安静恢复，不发消息（避免轰炸）
                return "normal", []
            return "cold", []

        # normal
        if after_value < sever:
            # 一轮从正常直接跌穿断交线：cold 与 severed 同时跨过，
            # 消息层面只发断交（断交声明已涵盖"我们有问题"）
            return "broken", ["cold", "severed"]
        if after_value < cold:
            return "cold", ["cold"]
        return "normal", []


class ProactiveOutbox:
    """主动消息发件箱（append-only，ack 后不再出现在 pending 列表）。"""

    def __init__(self, db: Database) -> None:
        """
        :param db: 数据库访问对象
        """
        self.db = db

    def add(
        self,
        space_id: str,
        kind: str,
        person_name: str,
        content: str,
        affection_value: float = 0.0,
        topic_id: str = "",
        session_id: str = "",
    ) -> Dict[str, Any]:
        """
        写入一条主动消息。

        :param space_id: 记忆空间
        :param kind: reach_out（主动联系）/ severance（断交）/ reconcile（和解）
        :param person_name: 收件人
        :param content: 消息正文
        :param affection_value: 发送时的好感度（审计用）
        :param topic_id: 触发事件主题
        :param session_id: 建议投递的会话
        :return: 完整消息字典
        """
        msg_id = new_id("pmsg")
        now = int(time.time())
        connection = self.db.conn()
        connection.execute(
            """
            insert into proactive_message
                (space_id, msg_id, kind, person_name, content, affection_value,
                 topic_id, session_id, acked, create_ts)
            values (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                space_id,
                msg_id,
                kind,
                person_name,
                content,
                affection_value,
                topic_id,
                session_id,
                now,
            ),
        )
        connection.commit()

        return {
            "msg_id": msg_id,
            "space_id": space_id,
            "kind": kind,
            "person_name": person_name,
            "content": content,
            "affection_value": affection_value,
            "topic_id": topic_id,
            "session_id": session_id,
            "acked": False,
            "create_ts": now,
        }

    def list_messages(
        self,
        space_id: str,
        session_id: Optional[str] = None,
        only_pending: bool = True,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        列出主动消息。

        :param space_id: 记忆空间
        :param session_id: 限定投递会话（缺省全部会话）
        :param only_pending: 只取未被确认的
        :param limit: 条数上限
        :return: 消息字典列表（旧→新）
        """
        sql = """
            select msg_id, kind, person_name, content, affection_value,
                   topic_id, session_id, acked, create_ts
            from proactive_message
            where space_id = ?
        """
        params: list[Any] = [space_id]
        if session_id:
            sql += " and session_id = ?"
            params.append(session_id)
        if only_pending:
            sql += " and acked = 0"
        # rowid 作次级排序：同一秒写入的多条消息保持插入顺序（旧 → 新）
        sql += " order by create_ts asc, rowid asc limit ?"
        params.append(int(limit))

        rows = self.db.conn().execute(sql, params).fetchall()
        return [
            {
                "msg_id": row["msg_id"],
                "space_id": space_id,
                "kind": row["kind"],
                "person_name": row["person_name"],
                "content": row["content"],
                "affection_value": float(row["affection_value"] or 0),
                "topic_id": row["topic_id"] or "",
                "session_id": row["session_id"] or "",
                "acked": bool(row["acked"]),
                "create_ts": int(row["create_ts"] or 0),
            }
            for row in rows
        ]

    def ack(self, space_id: str, msg_id: str) -> bool:
        """
        确认一条消息（ack 后不再出现在 pending 列表；正文仍可查）。

        :param space_id: 记忆空间
        :param msg_id: 消息 id
        :return: 是否成功（消息不存在或已确认过返回 False）
        """
        connection = self.db.conn()
        # 只匹配未确认的行：SQLite 的 rowcount 按"匹配行"计数，
        # 不加 acked=0 条件的话重复 ack 也会返回成功
        cursor = connection.execute(
            """
            update proactive_message set acked = 1
            where space_id = ? and msg_id = ? and acked = 0
            """,
            (space_id, msg_id),
        )
        connection.commit()
        return cursor.rowcount > 0


class ProactiveScheduleStore:
    """
    主动联系时间表。

    语义（与产品约定严格对应）：
    - 每轮回答结束后调用 set_pending()，写入模型决定的 due_ts；
    - 到点前用户先开口 → cancel()，这张表作废，回答结束后重新排；
    - 到点后由调度器 claim() 原子兑现（status = fired）；
    - 一次时间表只能兑现一次：claim/cancel 都带 status = 'pending' 条件，
      并发下不会出现"同一张表既发了消息又取消了"。

    同一空间/会话只保留一行，新的排期覆盖旧的——历史排期不参与业务判断，
    只用于观测当前这张表什么时候兑现。
    """

    def __init__(self, db: Database) -> None:
        """
        :param db: 数据库访问对象
        """
        self.db = db

    def set_pending(
        self,
        space_id: str,
        session_id: str,
        due_ts: int,
        reason: str = "",
        source: str = "llm",
    ) -> Dict[str, Any]:
        """
        写入（或覆盖）一张待兑现的时间表。

        :param space_id: 记忆空间
        :param session_id: 会话 id
        :param due_ts: 计划时间戳（秒）
        :param reason: 排期理由（模型给出的简短说明）
        :param source: 来源（llm / fallback / manual）
        :return: 当前时间表字典
        """
        now = int(time.time())
        connection = self.db.conn()
        connection.execute(
            """
            insert into proactive_schedule
                (space_id, session_id, due_ts, status, reason, source,
                 create_ts, update_ts)
            values (?, ?, ?, 'pending', ?, ?, ?, ?)
            on conflict (space_id, session_id) do update set
                due_ts = excluded.due_ts,
                status = 'pending',
                reason = excluded.reason,
                source = excluded.source,
                create_ts = excluded.create_ts,
                update_ts = excluded.update_ts
            """,
            (space_id, session_id, int(due_ts), reason, source, now, now),
        )
        connection.commit()
        return self.get(space_id, session_id) or {}

    def get(self, space_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        """
        读取当前时间表。

        :param space_id: 记忆空间
        :param session_id: 会话 id
        :return: {"space_id","session_id","due_ts","status","reason","source",
                  "create_ts","update_ts"}；不存在返回 None
        """
        row = self.db.conn().execute(
            """
            select space_id, session_id, due_ts, status, reason, source,
                   create_ts, update_ts
            from proactive_schedule
            where space_id = ? and session_id = ?
            """,
            (space_id, session_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "space_id": row["space_id"],
            "session_id": row["session_id"],
            "due_ts": int(row["due_ts"] or 0),
            "status": str(row["status"] or "pending"),
            "reason": row["reason"] or "",
            "source": row["source"] or "",
            "create_ts": int(row["create_ts"] or 0),
            "update_ts": int(row["update_ts"] or 0),
        }

    def list_due(self, now: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """
        列出已经到期、还没兑现的时间表（后台调度器用）。

        :param now: 当前时间戳（缺省取系统时间）
        :param limit: 单次最多处理几张表
        :return: 时间表字典列表（按到期时间升序）
        """
        current = int(time.time()) if now is None else int(now)
        rows = self.db.conn().execute(
            """
            select space_id, session_id, due_ts, status, reason, source,
                   create_ts, update_ts
            from proactive_schedule
            where status = 'pending' and due_ts <= ?
            order by due_ts asc
            limit ?
            """,
            (current, int(limit)),
        ).fetchall()
        return [
            {
                "space_id": row["space_id"],
                "session_id": row["session_id"],
                "due_ts": int(row["due_ts"] or 0),
                "status": str(row["status"] or "pending"),
                "reason": row["reason"] or "",
                "source": row["source"] or "",
                "create_ts": int(row["create_ts"] or 0),
                "update_ts": int(row["update_ts"] or 0),
            }
            for row in rows
        ]

    def cancel(self, space_id: str, session_id: str) -> bool:
        """
        作废当前待兑现的时间表（收到用户消息时调用）。

        :param space_id: 记忆空间
        :param session_id: 会话 id
        :return: 是否真的作废了一张 pending 时间表
        """
        connection = self.db.conn()
        cursor = connection.execute(
            """
            update proactive_schedule
            set status = 'cancelled', update_ts = ?
            where space_id = ? and session_id = ? and status = 'pending'
            """,
            (int(time.time()), space_id, session_id),
        )
        connection.commit()
        return cursor.rowcount > 0

    def claim(self, space_id: str, session_id: str, due_ts: int) -> bool:
        """
        原子兑现时间表：只有"仍 pending 且 due_ts 未被覆盖"时才成功。

        due_ts 一起参与匹配，防止模型组稿期间用户先开口：
        用户开口会 cancel 并把同一行改成新排期，此时旧的 due_ts 已对不上，
        这次组稿出的消息直接丢弃，不会在用户刚说完话时插一条旧消息。

        :param space_id: 记忆空间
        :param session_id: 会话 id
        :param due_ts: 调度器读到的到期时间戳
        :return: 是否兑现成功
        """
        connection = self.db.conn()
        cursor = connection.execute(
            """
            update proactive_schedule
            set status = 'fired', update_ts = ?
            where space_id = ? and session_id = ?
              and status = 'pending' and due_ts = ?
            """,
            (int(time.time()), space_id, session_id, int(due_ts)),
        )
        connection.commit()
        return cursor.rowcount > 0
