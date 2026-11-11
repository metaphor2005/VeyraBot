"""
SQLite 连接与建表管理。

设计说明：
- 采用"单库多空间"方案：所有记忆空间共用一张表，用 space_id 列隔离，
  相比按空间拆表更易维护，且天然支持跨空间迁移。
- 每个线程持有独立连接（threading.local），避免 SQLite 跨线程句柄问题。
- 开启 WAL 模式，提升读写并发安全。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

import config


# 所有建表语句集中于此，保证 schema 可审计、可迁移
_SCHEMA_STATEMENTS = [
    # 记忆空间元信息
    """
    create table if not exists memory_space_meta (
        space_id    text primary key,   -- 空间唯一标识
        name        text,               -- 空间可读名称
        create_ts   integer             -- 创建时间
    )
    """,
    # KG 实体
    """
    create table if not exists kg_entity (
        space_id    text,               -- 所属记忆空间
        eid         text,               -- 实体唯一标识
        etype       text,               -- 实体类型（User/Project/Component/Belief/AgentSelf...）
        name        text,               -- 实体名称
        attrs       text,               -- 属性 JSON 字符串
        is_active   integer default 1,  -- 是否生效（0 表示已被新认知推翻）
        create_ts   integer,
        primary key (space_id, eid)
    )
    """,
    # KG 关系（带置信度与重要性权重，是人格演化的载体）
    """
    create table if not exists kg_relation (
        space_id        text,
        rid             text,
        head_name       text,               -- 头实体名称
        rel             text,               -- 关系名
        tail_name       text,               -- 尾实体名称
        confidence      real default 0.7,   -- 置信度
        importance      real default 0.5,   -- 对自我的重要程度
        is_active       integer default 1,  -- 是否生效
        source_topic_id text,               -- 该信念由哪次交互产生
        create_ts       integer,
        update_ts       integer,
        primary key (space_id, rid)
    )
    """,
    # 主题（事件级记忆单元）
    """
    create table if not exists topic (
        space_id         text,
        topic_id         text,
        title            text,
        abstract         text,
        keywords         text,              -- 关键词 JSON 数组
        session_id       text,
        snapshot_version integer,           -- 该事件发生时的人格快照版本
        kind             text default 'normal',  -- normal / daily（日结）/ event（大事）
        consolidated     integer default 0,      -- 是否已被上一层归并（1 = 已归并）
        is_big_event     integer default 0,      -- 是否被判定为大事
        create_ts        integer,
        primary key (space_id, topic_id)
    )
    """,
    # 经验片段（挂在主题下，走向量检索）
    """
    create table if not exists experience (
        space_id    text,
        exp_id      text,
        topic_id    text,                   -- 归属主题
        content     text,                   -- 经验正文
        create_ts   integer,
        primary key (space_id, exp_id)
    )
    """,
    # 人格符号内核：参数
    """
    create table if not exists persona_kernel (
        space_id    text,
        param       text,
        value       real,                   -- 参数值（0~1）
        update_ts   integer,
        primary key (space_id, param)
    )
    """,
    # 人格符号内核：上层情景状态
    """
    create table if not exists persona_mood (
        space_id        text,
        mode            text,               -- 情景模式名
        intensity       real,               -- 当前强度（按时间 + 轮次双段衰减）
        base_intensity  real,               -- 初始强度，用于衰减基准
        decay_rate      real,               -- 每轮衰减比例
        create_ts       integer,
        last_decay_ts   integer,            -- 上次衰减时刻（按真实流逝时间衰减的锚点）
        primary key (space_id, mode)
    )
    """,
    # 人格元信息：当前快照版本
    """
    create table if not exists persona_meta (
        space_id        text primary key,
        current_version integer,            -- 当前快照版本号
        persona_name    text                -- 人格名称
    )
    """,
    # 人格变迁日志（append-only）
    """
    create table if not exists persona_changelog (
        space_id        text,
        log_id          text,
        topic_id        text,               -- 触发变更的事件
        persona_field   text,               -- 变更维度
        old_value       text,
        new_value       text,
        reason          text,               -- 变更原因（由模型基于对话归纳）
        source_type     text,               -- dialogue / self_reflection / user_directive
        snapshot_from   integer,            -- 变更前快照版本
        snapshot_to     integer,            -- 变更后快照版本
        create_ts       integer,
        primary key (space_id, log_id)
    )
    """,
    # 会话持久化
    """
    create table if not exists session_store (
        session_id  text primary key,
        space_id    text,
        payload     text,                   -- SessionContext 的 JSON 序列化
        update_ts   integer
    )
    """,
    # 巩固日志（日结 / 大事），用于"多久没巩固了"的审计与防重复
    """
    create table if not exists consolidation_log (
        space_id    text,
        log_id      text,
        kind        text,                   -- daily / event
        range_from  integer,                -- 覆盖起始时间（秒）
        range_to    integer,                -- 覆盖结束时间（秒）
        topic_ids   text,                   -- 被归并的主题 id，JSON 数组
        summary     text,                   -- 归纳正文
        create_ts   integer,
        primary key (space_id, log_id)
    )
    """,
    # 好感度：对每个对话对象维护一条 0~100 的温度值（社会情感的数值化）
    """
    create table if not exists affection (
        space_id    text,
        person_name text,               -- 对方名称（与 kg_entity.name 对齐）
        value       real default 50,    -- 好感度 0~100，50 为无感基线
        status      text default 'normal',  -- normal / cold（冷淡）/ broken（断交）
        reason      text,               -- 最近一次显著变化的原因
        create_ts   integer,
        update_ts   integer,
        primary key (space_id, person_name)
    )
    """,
    # 主动消息发件箱：好感度阈值触发的主动联系 / 断交 / 和解声明，
    # 由 API 与 UI 层拉取，不阻塞对话主链路
    """
    create table if not exists proactive_message (
        space_id    text,
        msg_id      text,
        kind        text,               -- reach_out（主动联系）/ severance（断交）/ reconcile（和解）
        person_name text,               -- 收件人
        content     text,               -- 消息正文
        affection_value real,            -- 发送时的好感度
        topic_id    text,               -- 触发事件
        session_id  text,               -- 建议投递的会话
        acked       integer default 0,  -- 是否已被外部拉取/确认
        create_ts   integer,
        primary key (space_id, msg_id)
    )
    """,
    # 主动联系时间表：每轮回答后由模型决定"下次什么时候来找你"。
    # 同一空间/会话只保留一条 pending；到点兑现或收到用户消息时作废，
    # 然后由下一轮回答重新生成（一次时间表只能使用一次）。
    """
    create table if not exists proactive_schedule (
        space_id    text,
        session_id  text,
        due_ts      integer,            -- 计划主动联系的时间戳（秒）
        status      text default 'pending',  -- pending / fired / cancelled
        reason      text,               -- 模型给出的排期理由（审计用）
        source      text,               -- llm / fallback / manual
        create_ts   integer,
        update_ts   integer,
        primary key (space_id, session_id)
    )
    """,
]


# 增量列迁移：老库不会因为 create table if not exists 而自动加列，这里显式补齐
_COLUMN_MIGRATIONS = [
    ("topic", "kind", "kind text default 'normal'"),
    ("topic", "consolidated", "consolidated integer default 0"),
    ("topic", "is_big_event", "is_big_event integer default 0"),
    # 人格基调与当日累积偏移：基调只在日结移动，current 只在基调附近摆动
    ("persona_kernel", "base_value", "base_value real"),
    ("persona_kernel", "pending_value", "pending_value real default 0"),
    # 即时自我认知（每轮可更新），版本化仍由日结生成快照完成
    ("persona_meta", "live_self_view", "live_self_view text"),
    # 情景状态最后衰减时刻：用于按真实流逝时间衰减（原先只按轮次衰减，
    # 隔几天不聊天情绪强度一点都不掉，一开口还用旧情绪接话）
    ("persona_mood", "last_decay_ts", "last_decay_ts integer"),
    # 关系维度是否允许多值并存：多值维度（室友/朋友/喜欢的作品）新取值与旧值共存，
    # 单值维度（生日/主用语言）维持"新值推翻旧值"。抽取时由模型按常识判断
    ("kg_relation", "multi_valued", "multi_valued integer default 0"),
]


class Database:
    """SQLite 数据库访问入口。"""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        """
        初始化数据库。

        :param db_path: 数据库文件路径，缺省使用 config.DB_PATH
        """
        self.db_path = Path(db_path or config.DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # 连接注册表：threading.local 无法跨线程枚举，
        # close_all_connections 需要一个可遍历的真实容器
        self._connections: "dict[int, sqlite3.Connection]" = {}
        self._registry_lock = threading.Lock()
        self.init_schema()

    def conn(self) -> sqlite3.Connection:
        """
        获取当前线程的数据库连接（惰性创建）。

        :return: sqlite3 连接对象
        """
        connection = getattr(self._local, "conn", None)
        if connection is not None and connection is self._connections.get(
            threading.get_ident()
        ):
            # 缓存命中：连接仍登记在册（未被 close_all_connections 关闭）
            return connection

        # check_same_thread=False：连接仍只由创建线程使用（threading.local 保证），
        # 但允许程序退出时由任意线程统一 close
        connection = sqlite3.connect(
            str(self.db_path), timeout=30, check_same_thread=False
        )
        # 允许后续用 row_factory 按列名取值
        connection.row_factory = sqlite3.Row
        # WAL 模式：读写并发更安全，断电后可恢复
        connection.execute("pragma journal_mode=wal")
        # 适度的同步级别，兼顾安全与性能
        connection.execute("pragma synchronous=normal")
        self._local.conn = connection
        with self._registry_lock:
            self._connections[threading.get_ident()] = connection
        return connection

    def close_all_connections(self) -> None:
        """关闭所有线程的数据库连接（用于程序退出时清理）。"""
        with self._registry_lock:
            connections = list(self._connections.values())
            self._connections.clear()
        for connection in connections:
            try:
                connection.close()
            except Exception:
                pass  # 忽略关闭时的异常

    def init_schema(self) -> None:
        """执行全部建表语句并补齐增量列（幂等，可重复调用）。"""
        connection = self.conn()
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)

        # 老库升级：缺哪列补哪列
        for table, column, ddl in _COLUMN_MIGRATIONS:
            existing = {
                row["name"]
                for row in connection.execute(f"pragma table_info({table})").fetchall()
            }
            if column not in existing:
                connection.execute(f"alter table {table} add column {ddl}")

        connection.commit()

    def ensure_space(self, space_id: str, name: str = "") -> None:
        """
        注册记忆空间（幂等）。空间内所有数据表随 space_id 自动隔离，
        无需为每个空间单独建物理表。

        :param space_id: 空间标识
        :param name: 空间可读名称
        """
        import time

        connection = self.conn()
        try:
            connection.execute(
                """
                insert or ignore into memory_space_meta (space_id, name, create_ts)
                values (?, ?, ?)
                """,
                (space_id, name or space_id, int(time.time())),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def close(self) -> None:
        """关闭当前线程连接。"""
        connection = getattr(self._local, "conn", None)
        if connection is not None:
            with self._registry_lock:
                if self._connections.get(threading.get_ident()) is connection:
                    del self._connections[threading.get_ident()]
            connection.close()
            self._local.conn = None
