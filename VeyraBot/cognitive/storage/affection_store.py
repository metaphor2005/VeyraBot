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

    def skip(self, space_id: str, session_id: str) -> bool:
        """
        把当前待兑现的时间表标记为跳过（"这轮先别发"）。

        :param space_id: 记忆空间
        :param session_id: 会话 id
        :return: 是否真的跳过了一张 pending 时间表
        """
        connection = self.db.conn()
        cursor = connection.execute(
            """
            update proactive_schedule
            set status = 'skipped', update_ts = ?
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


class ProactivePolicyStore:
    """主动联系的用户偏好：免打扰时段、每日上限、冷却、跳过本轮。"""

    DEFAULT_DND_START = config.PROACTIVE_DND_START
    DEFAULT_DND_END = config.PROACTIVE_DND_END
    DEFAULT_DAILY_CAP = config.PROACTIVE_DAILY_CAP
    DEFAULT_COOLDOWN_MINUTES = config.PROACTIVE_COOLDOWN_MINUTES

    def __init__(self, db: Database) -> None:
        """
        :param db: 数据库访问对象
        """
        self.db = db

    def get(self, space_id: str) -> Dict[str, Any]:
        """
        读取策略（不存在时返回默认值，不写库）。

        :param space_id: 记忆空间
        :return: 策略字典
        """
        connection = self.db.conn()
        row = connection.execute(
            """
            select dnd_start, dnd_end, daily_cap, cooldown_minutes, skip_next
            from proactive_policy
            where space_id = ?
            """,
            (space_id,),
        ).fetchone()
        if not row:
            return {
                "space_id": space_id,
                "dnd_start": self.DEFAULT_DND_START,
                "dnd_end": self.DEFAULT_DND_END,
                "daily_cap": self.DEFAULT_DAILY_CAP,
                "cooldown_minutes": self.DEFAULT_COOLDOWN_MINUTES,
                "skip_next": False,
            }
        return {
            "space_id": space_id,
            "dnd_start": int(row["dnd_start"]),
            "dnd_end": int(row["dnd_end"]),
            "daily_cap": int(row["daily_cap"]),
            "cooldown_minutes": int(row["cooldown_minutes"]),
            "skip_next": bool(row["skip_next"]),
        }

    def update(
        self,
        space_id: str,
        dnd_start: Optional[int] = None,
        dnd_end: Optional[int] = None,
        daily_cap: Optional[int] = None,
        cooldown_minutes: Optional[int] = None,
        skip_next: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        写入策略（未提供的字段保持不变）。

        :param space_id: 记忆空间
        :param dnd_start/dnd_end: 免打扰时段（0~23）
        :param daily_cap: 每日主动消息上限
        :param cooldown_minutes: 两次主动消息之间的最短间隔
        :param skip_next: 是否跳过下一轮
        :return: 更新后的策略
        """
        current = self.get(space_id)
        now = int(time.time())
        connection = self.db.conn()
        connection.execute(
            """
            insert into proactive_policy
                (space_id, dnd_start, dnd_end, daily_cap, cooldown_minutes,
                 skip_next, create_ts, update_ts)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict (space_id) do update set
                dnd_start = excluded.dnd_start,
                dnd_end = excluded.dnd_end,
                daily_cap = excluded.daily_cap,
                cooldown_minutes = excluded.cooldown_minutes,
                skip_next = excluded.skip_next,
                update_ts = excluded.update_ts
            """,
            (
                space_id,
                int(current["dnd_start"] if dnd_start is None else dnd_start),
                int(current["dnd_end"] if dnd_end is None else dnd_end),
                max(0, int(current["daily_cap"] if daily_cap is None else daily_cap)),
                max(
                    0,
                    int(
                        current["cooldown_minutes"]
                        if cooldown_minutes is None
                        else cooldown_minutes
                    ),
                ),
                1
                if (current["skip_next"] if skip_next is None else skip_next)
                else 0,
                now,
                now,
            ),
        )
        connection.commit()
        return self.get(space_id)

    def consume_skip(self, space_id: str) -> bool:
        """
        消费一次"这轮先别发"。

        :param space_id: 记忆空间
        :return: 本次是否真的跳过了
        """
        policy = self.get(space_id)
        if not policy["skip_next"]:
            return False
        self.update(space_id, skip_next=False)
        return True

    @staticmethod
    def in_dnd(policy: Dict[str, Any], now: Optional[int] = None) -> bool:
        """
        判断当前是否落在免打扰时段（支持跨午夜，如 23 点到次日 8 点）。

        :param policy: 策略字典
        :param now: 时间戳（缺省当前）
        :return: 是否免打扰
        """
        hour = time.localtime(now or time.time()).tm_hour
        start = int(policy.get("dnd_start", 23))
        end = int(policy.get("dnd_end", 8))
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def sent_today(self, space_id: str) -> int:
        """
        统计今天已发送的主动消息条数（按自然日）。

        :param space_id: 记忆空间
        :return: 条数
        """
        local = time.localtime()
        start = int(
            time.mktime(
                (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)
            )
        )
        connection = self.db.conn()
        row = connection.execute(
            "select count(1) as cnt from proactive_message"
            " where space_id = ? and create_ts >= ?",
            (space_id, start),
        ).fetchone()
        return int(row["cnt"]) if row else 0

    def last_sent_ts(self, space_id: str) -> int:
        """
        最近一条主动消息的发送时间。

        :param space_id: 记忆空间
        :return: 时间戳；从未发送返回 0
        """
        connection = self.db.conn()
        row = connection.execute(
            "select max(create_ts) as ts from proactive_message where space_id = ?",
            (space_id,),
        ).fetchone()
        return int(row["ts"]) if row and row["ts"] else 0

    def block_reason(self, space_id: str, now: Optional[int] = None) -> str:
        """
        给出当前应阻止主动联系的原因（空串表示可以发）。

        :param space_id: 记忆空间
        :param now: 时间戳（缺省当前）
        :return: 阻止原因：dnd / daily_cap / cooldown / skip_next / 空串
        """
        policy = self.get(space_id)
        if policy["skip_next"]:
            return "skip_next"
        if self.in_dnd(policy, now):
            return "dnd"
        if policy["daily_cap"] > 0 and self.sent_today(
            space_id
        ) >= policy["daily_cap"]:
            return "daily_cap"
        cooldown = int(policy["cooldown_minutes"]) * 60
        if cooldown > 0:
            last = self.last_sent_ts(space_id)
            if last and int(now or time.time()) - last < cooldown:
                return "cooldown"
        return ""
