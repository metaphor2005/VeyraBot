"""
人格的「既有记忆」注入（seed）。

为什么需要这一层：
`config.PERSONA_PROFILE` 只是 Prompt 里的一段档案——模型"读得到"，但"检索不到"。
被问「你家猫叫什么」「第一次出 cos 是哪个」，它只能靠复述档案硬凑，一旦被追问细节就会露馅。

真人不是这样的。真人有前史：记得某天站在漫展门口没敢进去，记得高考前和妈妈吵过一架。
所以这里把江妍的 18 年写进真正的存储层。

前史数据本身在 `data/persona/seed.json`（与代码分离，升级时保留）：
本模块只负责把 JSON 写进 topic / KG / experience / 向量索引。

- 1 条「人物档案」主题（自然语言摘要，关键词覆盖面广，任何关于她本人的问题都能召回）
- 6 条成长往事（topic，带时间戳，挂在经验片段上）
- 20 余条 KG 关系（家人、室友、猫、偏好、雷区、正在做的事）

写入后，她回答时是"想起来"，而不是"背设定"。

设计约束：
1. **幂等**：topic_id / rid 全部是固定值，重复执行不会重复增长，KG 权重也不会被反复加固。
2. **不参与日结**：成长往事 `consolidated=1`，它们是"18 年的旧事"，不该和今天聊的事一起归并。
3. **权重分层**：身份与雷区 importance 0.85~0.95（会进核心信念区），生活细节 0.6~0.75。
4. **避免 KG 冲突误报**：同一 head + 同一 rel 只能有一条生效关系，
   所以"多个室友/多个喜欢的干员"一律写成反向关系（陈雨 → 是江妍的室友），
   不同维度用不同 rel（怕 / 反感 / 不擅长），否则 seed 自己就会把彼此推翻。
5. **带情绪残留**：每段往事额外挂一条「想起这件事时的感觉：…」的 experience。
   没有它，往事只是资料；有了它，她聊到某段往事时带的是情绪而不只是一个事件记录。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

import config
from cognitive.models import Experience, KGEntity, KGRelation, Topic
from cognitive.storage.database import Database
from cognitive.storage.kg_store import KGStore
from cognitive.storage.topic_store import TopicStore
from cognitive.storage.vector_store import VectorStore

# seed 前史主题挂的会话 id。这些主题 consolidated=True（不参与日结），
# 但读路径必须豁免它们（见 cognition/router.py）：前史是"18 年的旧事"，
# 不是"已被归并的原料"，永远可以召回。
SEED_SESSION_ID = "seed"

# ------------------------------------------------------------
# 人格原始数据：全部来自 data/persona/seed.json
#
# 这里只保留读取入口，不再把前史写死在代码里：
# 升级 / 迁移 / 换人格时只需要替换 data/persona/seed.json。
# 字段说明见 data/README.md。
# ------------------------------------------------------------
SEED_ENTITIES: List[Dict[str, Any]] = list(config.PERSONA_SEED["entities"])

SEED_RELATIONS: List[Tuple[str, str, str, float]] = [
    (str(head), str(rel), str(tail), float(importance))
    for head, rel, tail, importance in config.PERSONA_SEED["relations"]
]

SEED_STORIES: List[Dict[str, Any]] = list(config.PERSONA_SEED["stories"])

SEED_PROFILE_TOPIC: Dict[str, Any] = dict(config.PERSONA_SEED["profile_topic"])


def _to_ts(date_text: str) -> int:
    """
    把 YYYY-MM-DD 转成 Unix 时间戳（本地时区零点）。

    :param date_text: 日期字符串
    :return: 时间戳（秒）
    """
    parsed = datetime.strptime(date_text, "%Y-%m-%d")
    return int(parsed.timestamp())


def seed_persona(db: Database, space_id: str, space_name: str = "") -> Dict[str, Any]:
    """
    把一个空间的记忆预置成"江妍已经有 18 年人生"的状态。

    幂等：已存在的 topic / 关系会被跳过，不会重复写入或反复加固权重。

    :param db: 数据库对象
    :param space_id: 目标空间
    :param space_name: 空间显示名（首次创建时用）
    :return: 写入统计
    """
    db.ensure_space(space_id, space_name or "江妍的记忆空间")
    conn = db.conn()

    kg = KGStore(db)
    topics = TopicStore(db)
    vector = VectorStore(space_id)

    stats = {"entities": 0, "relations": 0, "topics": 0, "experiences": 0, "skipped_topics": 0}

    # ---------- 1. 实体 ----------
    for item in SEED_ENTITIES:
        kg.upsert_entity(
            KGEntity(
                eid=item["eid"],
                etype=item["etype"],
                name=item["name"],
                space_id=space_id,
                attrs=item["attrs"],
            )
        )
        stats["entities"] += 1

    # ---------- 2. 关系（固定 rid，已存在则跳过，避免权重被反复加固）----------
    for index, (head, rel, tail, importance) in enumerate(SEED_RELATIONS, start=1):
        rid = f"seed_r{index:03d}"
        exists = conn.execute(
            "select 1 from kg_relation as r where r.space_id = ? and r.rid = ?",
            (space_id, rid),
        ).fetchone()
        if exists:
            continue
        kg.upsert_relation(
            KGRelation(
                rid=rid,
                space_id=space_id,
                head_name=head,
                rel=rel,
                tail_name=tail,
                confidence=0.85,
                importance=importance,
                source_topic_id="",
            )
        )
        stats["relations"] += 1

    # ---------- 3. 主题（成长往事 + 人物档案）----------
    story_items = list(SEED_STORIES) + [SEED_PROFILE_TOPIC]
    for item in story_items:
        topic_id = item["id"]
        exists = conn.execute(
            "select 1 from topic as t where t.space_id = ? and t.topic_id = ?",
            (space_id, topic_id),
        ).fetchone()
        if exists:
            stats["skipped_topics"] += 1
            continue

        topic = Topic(
            topic_id=topic_id,
            space_id=space_id,
            title=item["title"],
            abstract=item["abstract"],
            keywords=list(item["keywords"]),
            session_id=SEED_SESSION_ID,
            snapshot_version=0,
            kind="normal",
            # 旧事：标记为已归并，不参与日结，但仍留在向量库可召回
            consolidated=True,
            is_big_event=False,
            create_ts=_to_ts(item["date"]),
        )
        topics.save_topic(topic)

        # 向量只索引主题（标题 + 摘要 + 关键词）
        vector.add(topic_id, f"{item['title']} {item['abstract']}", {"keywords": item["keywords"]})

        for exp_text in item.get("experiences", []):
            topics.add_experience(
                Experience(
                    exp_id=f"seed_x_{topic_id}_{stats['experiences']:03d}",
                    space_id=space_id,
                    topic_id=topic_id,
                    content=exp_text,
                    create_ts=_to_ts(item["date"]),
                )
            )
            stats["experiences"] += 1

        # 情绪残留：往事被召回时，她不只是"知道发生过什么"，还"想起来是什么感觉"。
        # 没有这一条，往事就只是资料；有了它，她才可能因为某段回忆而不想多聊。
        emotion = item.get("emotion", "")
        if emotion:
            topics.add_experience(
                Experience(
                    exp_id=f"seed_x_{topic_id}_emotion",
                    space_id=space_id,
                    topic_id=topic_id,
                    content=f"想起这件事时的感觉：{emotion}",
                    create_ts=_to_ts(item["date"]),
                )
            )
            stats["experiences"] += 1

        stats["topics"] += 1

    stats["indexed"] = vector.count()
    return stats


def seed_emotions(db: Database, space_id: str) -> int:
    """
    只补写往事的「情绪残留」片段（幂等）。

    用途：某个空间已经注入过前史，后来才补上 emotion 字段时，
    不必清空空间重跑，直接补这几条 experience 即可。

    :param db: 数据库对象
    :param space_id: 记忆空间
    :return: 补写的条数
    """
    topics = TopicStore(db)
    added = 0
    for item in list(SEED_STORIES) + [SEED_PROFILE_TOPIC]:
        emotion = item.get("emotion", "")
        if not emotion:
            continue
        topics.add_experience(
            Experience(
                exp_id=f"seed_x_{item['id']}_emotion",
                space_id=space_id,
                topic_id=item["id"],
                content=f"想起这件事时的感觉：{emotion}",
                create_ts=_to_ts(item["date"]),
            )
        )
        added += 1
    return added


def persona_seed_overview(db: Database, space_id: str) -> Dict[str, Any]:
    """
    查看某空间是否已经注入过既有记忆（用于维护命令行判断）。

    :param db: 数据库对象
    :param space_id: 记忆空间
    :return: {"seeded": bool, "topics": 条数, "relations": 条数}
    """
    conn = db.conn()
    topic_count = conn.execute(
        "select count(1) as cnt from topic as t where t.space_id = ? and t.session_id = ?",
        (space_id, SEED_SESSION_ID),
    ).fetchone()["cnt"]
    relation_count = conn.execute(
        "select count(1) as cnt from kg_relation as r where r.space_id = ? and r.rid like 'seed_r%'",
        (space_id,),
    ).fetchone()["cnt"]
    return {"seeded": bool(topic_count), "topics": int(topic_count), "relations": int(relation_count)}
