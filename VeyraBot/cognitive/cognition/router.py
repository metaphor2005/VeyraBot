"""
记忆路由：按用户输入，召回用于本轮推理的记忆材料。

召回优先级（与"老 RAG 前置"相反）：
1. 结构化信念（KG 精确查询，只返回当前生效项）
2. 曾被推翻的旧信念（失效项，作为"我改过主意"的对照）
3. 主题记忆（事件级召回，成套取回该事件下的经验片段）
4. RAG 兜底（不在这里执行，由 judge 判决后决定，见 engine）

注意：向量只索引"主题摘要"，不索引零散对话碎片，
召回单位是"一件事"而不是"一段文字"。
"""
from __future__ import annotations

from typing import List

from cognitive.models import RetrievedMemory, Topic
from cognitive.persona.seed import SEED_SESSION_ID
from cognitive.storage.database import Database
from cognitive.storage.kg_store import KGStore
from cognitive.storage.topic_store import TopicStore
from cognitive.storage.vector_store import VectorStore


class MemoryRouter:
    """记忆路由器。"""

    def __init__(self, db: Database) -> None:
        """
        :param db: 数据库访问对象
        """
        self.db = db

    def route(self, space_id: str, query: str) -> RetrievedMemory:
        """
        执行一次记忆路由。

        :param space_id: 记忆空间
        :param query: 用户当前输入
        :return: 汇总后的记忆材料
        """
        result = RetrievedMemory()

        # ---------- 1. KG 精确查询 ----------
        kg = KGStore(self.db)
        result.kg_facts = kg.query_by_text(space_id, query)

        # ---------- 2. 曾被推翻的旧信念 ----------
        # 只过滤不展示，会让 AI 感知不到"自己改过主意"（模型自己指出过这个漏洞）
        result.overturned = kg.query_overturned(space_id, query)

        # ---------- 2. 主题记忆召回 ----------
        vector = VectorStore(space_id)
        topics_store = TopicStore(self.db)

        hits = vector.search(query)
        topics: List[Topic] = []
        experiences: List[str] = []

        for topic_id, _score, _meta in hits:
            topic = topics_store.get_topic(space_id, topic_id)
            if not topic:
                continue
            # 已被上层（日结/大事）归并的主题是"原料"：细节已被抽走，
            # 读路径只保留未归并主题，避免原料和成品同时出现、互相稀释。
            # seed 前史例外 —— 它们 consolidated=1 只是为了不参与日结，
            # 本身就是可召回的成品记忆，必须保留（否则 demo 的"前史召回"全灭）。
            if topic.consolidated and topic.session_id != SEED_SESSION_ID:
                continue
            topics.append(topic)
            experiences.extend(topics_store.get_experiences_by_topic(space_id, topic_id))

        result.topics = topics
        result.experiences = experiences

        return result
