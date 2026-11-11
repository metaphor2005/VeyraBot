"""
结构化信念存储（轻量属性图 KG）。

核心能力：
1. 实体/关系写入（幂等 upsert）
2. 同一 (头实体, 关系) 下的冲突检测：新值出现时，旧值自动标记失效
3. 重复出现的信念自动加权（confidence / importance 递增），实现"多次验证→信念加固"
4. 按实体名做精确/包含匹配，支持"配置、偏好、约定"类查询
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

import config
from cognitive.models import KGEntity, KGRelation, Topic
from cognitive.storage.database import Database


# 关系写回时的权重增量（代码层演化，不依赖模型）
CONFIDENCE_BOOST: float = 0.05
IMPORTANCE_BOOST: float = 0.03


class KGStore:
    """知识图谱存储。"""

    def __init__(self, db: Database) -> None:
        """
        :param db: 数据库访问对象
        """
        self.db = db

    # ------------------------------------------------------------
    # 实体
    # ------------------------------------------------------------
    def upsert_entity(self, entity: KGEntity) -> None:
        """
        写入或更新实体（同 space_id + eid 视为同一实体）。

        :param entity: 实体对象
        """
        connection = self.db.conn()
        connection.execute(
            """
            insert into kg_entity (space_id, eid, etype, name, attrs, is_active, create_ts)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict (space_id, eid) do update set
                etype = excluded.etype,
                name = excluded.name,
                attrs = excluded.attrs,
                is_active = excluded.is_active
            """,
            (
                entity.space_id,
                entity.eid,
                entity.etype,
                entity.name,
                json.dumps(entity.attrs, ensure_ascii=False),
                1 if entity.is_active else 0,
                entity.create_ts or int(time.time()),
            ),
        )
        connection.commit()

    def list_entities(self, space_id: str, etype: Optional[str] = None) -> List[sqlite3.Row]:
        """
        列出空间内实体。

        :param space_id: 记忆空间
        :param etype: 限定实体类型，None 表示全部
        :return: 数据行列表
        """
        connection = self.db.conn()
        if etype:
            rows = connection.execute(
                """
                select e.eid as eid, e.etype as etype, e.name as name, e.attrs as attrs
                from kg_entity as e
                where e.space_id = ? and e.etype = ? and e.is_active = 1
                """,
                (space_id, etype),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                select e.eid as eid, e.etype as etype, e.name as name, e.attrs as attrs
                from kg_entity as e
                where e.space_id = ? and e.is_active = 1
                """,
                (space_id,),
            ).fetchall()
        return rows

    # ------------------------------------------------------------
    # 关系（含冲突与权重演化）
    # ------------------------------------------------------------
    def upsert_relation(self, relation: KGRelation) -> Dict[str, Any]:
        """
        写入关系，并处理与既有信念的冲突。

        规则：
        - 若同一 (head_name, rel) 下已存在生效且 tail 不同的旧关系：
          · 双方都允许多值（multi_valued）→ 共存，互不推翻
            （"室友/朋友/喜欢的作品"这类维度天然可以有多个取值）
          · 任一方是单值 → 旧关系标记失效（被推翻）
            （"生日/主用语言"这类维度只能有一个当前值）
        - 若 tail 相同（重复确认同一信念）→ 置信度与重要性加权提升，本次不重复插入
          生产路径（archiver）的 rid 由三元组稳定生成，同一信念再次确认必然是 rid 命中，
          同样走这条加固分支
        - rid 已存在（含失效行）时 → on conflict 复活/更新该行，created=False

        :param relation: 关系对象
        :return: {"conflicts": [(旧值, 新值)], "boosted": bool, "created": bool, "coexisted": bool}
        """
        connection = self.db.conn()
        now = int(time.time())

        conflicts: List[Tuple[str, str]] = []
        boosted = False
        created = False
        coexisted = False

        # 查询同头同关系下已生效的记录
        existing = connection.execute(
            """
            select r.rid as rid, r.tail_name as tail_name, r.confidence as confidence,
                   r.importance as importance, r.multi_valued as multi_valued
            from kg_relation as r
            where r.space_id = ? and r.head_name = ? and r.rel = ? and r.is_active = 1
            """,
            (relation.space_id, relation.head_name, relation.rel),
        ).fetchall()

        for row in existing:
            if row["rid"] == relation.rid:
                # rid 命中 = 同一三元组再次确认（archiver 的 rid 由三元组决定）。
                # 之前这里是 continue —— 死分支：生产路径永远不会走进下面的
                # "tail 相同"加固逻辑，on conflict 又把权重原样覆盖回去，
                # 导致"重复确认会加固"只在测试里成立。
                if row["tail_name"] == relation.tail_name:
                    confidence = min(1.0, float(row["confidence"]) + CONFIDENCE_BOOST)
                    importance = min(1.0, float(row["importance"]) + IMPORTANCE_BOOST)
                    # 同一信念再次被确认时，多值属性按"或"合并：
                    # 一旦这个维度被判定过多值，就不再退化回单值推翻
                    multi = 1 if (relation.multi_valued or row["multi_valued"]) else 0
                    connection.execute(
                        """
                        update kg_relation
                        set confidence = ?, importance = ?, multi_valued = ?, update_ts = ?
                        where space_id = ? and rid = ?
                        """,
                        (confidence, importance, multi, now, relation.space_id, row["rid"]),
                    )
                    boosted = True
                # rid 相同但 tail 不同（手动复用 rid 的写法）：留给下方 on conflict 更新
                continue

            if row["tail_name"] == relation.tail_name:
                # 同一信念被再次确认：权重加固，本次不重复插入
                confidence = min(1.0, float(row["confidence"]) + CONFIDENCE_BOOST)
                importance = min(1.0, float(row["importance"]) + IMPORTANCE_BOOST)
                multi = 1 if (relation.multi_valued or row["multi_valued"]) else 0
                connection.execute(
                    """
                    update kg_relation
                    set confidence = ?, importance = ?, multi_valued = ?, update_ts = ?
                    where space_id = ? and rid = ?
                    """,
                    (confidence, importance, multi, now, relation.space_id, row["rid"]),
                )
                boosted = True
            elif relation.multi_valued or row["multi_valued"]:
                # 多值维度：不同取值合法共存，旧行保持生效，本次照常插入新行
                # （新行 rid 由三元组生成，tail 不同则 rid 不同，天然不会互相覆盖）
                coexisted = True
            else:
                # 单值维度出现不同取值：旧信念被推翻
                connection.execute(
                    """
                    update kg_relation
                    set is_active = 0, update_ts = ?
                    where space_id = ? and rid = ?
                    """,
                    (now, relation.space_id, row["rid"]),
                )
                conflicts.append((row["tail_name"], relation.tail_name))

        # 仅在没有被"重复确认"时才插入/更新本次记录，避免同一信念双份存在
        if not boosted:
            # on conflict (space_id, rid) 命中旧行（含失效行）时是"复活/更新"而非新建，
            # created 必须如实反映，否则统计会把旧信念复活当成新信念
            rid_existed = connection.execute(
                "select 1 from kg_relation as r where r.space_id = ? and r.rid = ?",
                (relation.space_id, relation.rid),
            ).fetchone()
            connection.execute(
                """
                insert into kg_relation
                    (space_id, rid, head_name, rel, tail_name, confidence, importance,
                     multi_valued, is_active, source_topic_id, create_ts, update_ts)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict (space_id, rid) do update set
                    tail_name = excluded.tail_name,
                    confidence = excluded.confidence,
                    importance = excluded.importance,
                    multi_valued = excluded.multi_valued,
                    is_active = excluded.is_active,
                    source_topic_id = excluded.source_topic_id,
                    update_ts = excluded.update_ts
                """,
                (
                    relation.space_id,
                    relation.rid,
                    relation.head_name,
                    relation.rel,
                    relation.tail_name,
                    max(0.0, min(1.0, relation.confidence)),
                    max(0.0, min(1.0, relation.importance)),
                    1 if relation.multi_valued else 0,
                    1 if relation.is_active else 0,
                    relation.source_topic_id,
                    relation.create_ts or now,
                    now,
                ),
            )
            created = not rid_existed

        connection.commit()
        return {
            "conflicts": conflicts,
            "boosted": boosted,
            "created": created,
            "coexisted": coexisted,
        }

    def list_relations(
        self, space_id: str, only_active: bool = True, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """
        列出该空间的 KG 关系（API 展示用）。

        :param space_id: 记忆空间
        :param only_active: 只取生效关系（False 时包含被推翻的历史行）
        :param limit: 条数上限
        :return: [{"rid","head","rel","tail","confidence","importance","multi_valued","is_active","source_topic_id"}]
        """
        sql = """
            select rid, head_name, rel, tail_name, confidence, importance,
                   multi_valued, is_active, source_topic_id, update_ts
            from kg_relation
            where space_id = ?
        """
        if only_active:
            sql += " and is_active = 1"
        sql += " order by importance desc, confidence desc limit ?"

        rows = self.db.conn().execute(sql, (space_id, int(limit))).fetchall()
        return [
            {
                "rid": row["rid"],
                "head": row["head_name"],
                "rel": row["rel"],
                "tail": row["tail_name"],
                "confidence": float(row["confidence"]),
                "importance": float(row["importance"]),
                "multi_valued": bool(row["multi_valued"]),
                "is_active": bool(row["is_active"]),
                "source_topic_id": row["source_topic_id"] or "",
                "update_ts": int(row["update_ts"] or 0),
            }
            for row in rows
        ]

    def query_by_text(self, space_id: str, text: str, limit: Optional[int] = None) -> List[str]:
        """
        根据用户输入做信念查询（实体名包含匹配）。

        这是"精确查询"而非向量相似：只要是已登记的实体/关系，就一定能召回，
        且只返回 is_active = 1 的当前有效信念。

        :param space_id: 记忆空间
        :param text: 用户输入文本
        :param limit: 返回条数上限
        :return: 可直接注入 Prompt 的事实文本行
        """
        connection = self.db.conn()
        limit = limit or config.KG_MAX_ROWS

        # 取出全部生效实体，用于在文本中做包含匹配
        entities = connection.execute(
            """
            select e.name as name, e.etype as etype
            from kg_entity as e
            where e.space_id = ? and e.is_active = 1
            """,
            (space_id,),
        ).fetchall()

        matched_names = {row["name"] for row in entities if row["name"] and row["name"] in text}

        # 用户提到实体名时，按实体精确查；否则回退到重要性最高的"自身核心信念"
        rows: List[sqlite3.Row]
        if matched_names:
            placeholders = ",".join(["?"] * len(matched_names))
            sql = f"""
                select r.head_name as head_name, r.rel as rel, r.tail_name as tail_name,
                       r.confidence as confidence, r.importance as importance
                from kg_relation as r
                where r.space_id = ? and r.is_active = 1
                  and (r.head_name in ({placeholders}) or r.tail_name in ({placeholders}))
                order by r.importance desc, r.confidence desc
                limit ?
            """
            params = [space_id, *matched_names, *matched_names, limit]
            rows = connection.execute(sql, params).fetchall()
        else:
            rows = connection.execute(
                """
                select r.head_name as head_name, r.rel as rel, r.tail_name as tail_name,
                       r.confidence as confidence, r.importance as importance
                from kg_relation as r
                where r.space_id = ? and r.is_active = 1
                order by r.importance desc, r.confidence desc
                limit ?
                """,
                (space_id, min(limit, 5)),
            ).fetchall()

        return [
            f"{row['head_name']} {row['rel']} {row['tail_name']}"
            f"（置信度 {float(row['confidence']):.2f}，重要性 {float(row['importance']):.2f}）"
            for row in rows
        ]

    def query_overturned(self, space_id: str, text: str = "", limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        查询"最近被推翻的信念"（is_active = 0 的历史关系）。

        为什么要单独查这一路：
        - 生效关系只呈现"我现在信什么"，AI 感知不到"我改过主意"
        - 旧值虽然失效，但它解释了"为什么现在是这个值"，是因果链的一部分
        - 模型曾自己指出这个缺口：图里的信念在掉，但没有被推翻的标记

        取值策略：
        1. 优先取与本次输入相关的（头实体出现在用户问题里）
        2. 没有相关的，则取最近被推翻的几条（让 AI 至少知道自己最近改过什么）
        3. 每条都尽量补上"现在的值"，形成「曾被推翻：X（原值）→ 现为 Y」

        :param space_id: 记忆空间
        :param text: 用户输入文本（用于相关性筛选）
        :param limit: 返回条数上限
        :return: [{"head","rel","old","new","ts"}] 字典列表
        """
        if not config.SHOW_OVERTURNED_BELIEFS:
            return []

        limit = limit or config.OVERTURNED_MAX_ITEMS
        connection = self.db.conn()

        rows = connection.execute(
            """
            select r.head_name as head_name, r.rel as rel, r.tail_name as tail_name,
                   r.update_ts as update_ts
            from kg_relation as r
            where r.space_id = ? and r.is_active = 0
            order by r.update_ts desc
            limit 40
            """,
            (space_id,),
        ).fetchall()

        if not rows:
            return []

        # 先按相关性筛（头实体出现在输入里）
        related = [row for row in rows if row["head_name"] and row["head_name"] in text]
        candidates = related[:limit] if related else rows[:limit]

        connection = self.db.conn()
        items: List[Dict[str, Any]] = []
        for row in candidates:
            # 补齐"现在的值"：同头实体同关系的生效行
            current = connection.execute(
                """
                select r.tail_name as tail_name
                from kg_relation as r
                where r.space_id = ? and r.head_name = ? and r.rel = ? and r.is_active = 1
                order by r.update_ts desc
                limit 1
                """,
                (space_id, row["head_name"], row["rel"]),
            ).fetchone()

            items.append(
                {
                    "head": row["head_name"] or "",
                    "rel": row["rel"] or "",
                    "old": row["tail_name"] or "",
                    "new": (current["tail_name"] if current else "") or "",
                    "ts": int(row["update_ts"] or 0),
                }
            )

        return items

    def query_core_beliefs(self, space_id: str, importance_threshold: float = 0.75) -> List[Dict[str, Any]]:
        """
        查询"对自我最重要"的核心信念，用于生成人格快照。

        :param space_id: 记忆空间
        :param importance_threshold: 重要性阈值
        :return: 信念字典列表
        """
        connection = self.db.conn()
        rows = connection.execute(
            """
            select r.head_name as head_name, r.rel as rel, r.tail_name as tail_name,
                   r.confidence as confidence, r.importance as importance
            from kg_relation as r
            where r.space_id = ?
              and r.is_active = 1
              and r.importance >= ?
            -- 排序必须带上 confidence：只按 importance 排，三个月前那条 0.9
            -- 会永远占住快照前排，新近的高置信信念挤不进来，
            -- 快照反映的就成了"曾经的她"而不是"现在的她"。
            order by (r.confidence * r.importance) desc, r.confidence desc
            limit 12
            """,
            (space_id, importance_threshold),
        ).fetchall()

        return [
            {
                "statement": f"{row['head_name']} {row['rel']} {row['tail_name']}",
                "confidence": float(row["confidence"]),
                "importance": float(row["importance"]),
            }
            for row in rows
        ]

    def count_active_relations(self, space_id: str) -> int:
        """统计当前生效的关系条数，用于健康度观测。"""
        connection = self.db.conn()
        row = connection.execute(
            """
            select count(1) as cnt
            from kg_relation as r
            where r.space_id = ? and r.is_active = 1
            """,
            (space_id,),
        ).fetchone()
        return int(row["cnt"]) if row else 0
