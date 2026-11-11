"""
主动发起对话：从记忆库里随机取一段旧事，组稿成一条"突然想跟你说句话"。

与好感度阈值触发的主动消息（reach_out / severance / reconcile）的区别：
- 那种是"关系事件"，必须发；
- 这种是日常行为，由模型在每轮回答后决定下一次什么时候发。

本模块只负责两件事（不碰会话与发件箱）：
1. choose_memory：从主题 / 经验 / 结构化信念里随机抽一段；
2. compose / decide_delay_minutes：把这段记忆和"下次什么时候来"交给模型。

真正的"到点兑现 / 收到消息作废"由 engine + ProactiveScheduleStore 负责，
这样存储层保持无副作用，调度语义集中在一处。
"""
from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional

import config
from cognitive.cognition import prompts
from cognitive.llm import DeepSeekClient, LLMError
from cognitive.storage.database import Database
from cognitive.storage.kg_store import KGStore
from cognitive.storage.topic_store import TopicStore


# 模型不可用时的主动消息兜底文案（记忆片段会填进 {memory}）
_INITIATIVE_TEMPLATES = (
    "突然想起{memory}。也没什么，就是想起来跟你说一声。",
    "刚发了会儿呆，脑子里冒出{memory}。你那边最近怎么样？",
)

# 各类记忆的抽样权重：往事最有"聊天感"，经验次之，结构化事实兜底
_MEMORY_WEIGHTS = {"topic": 5.0, "experience": 3.0, "relation": 2.0}

# 兜底文案里最多展示多少个字，避免把整段摘要塞进一句话
_FALLBACK_MEMORY_CHARS = 36

# 主动消息正文长度上限（模型偶尔会写长，截断防止刷屏）
_INITIATIVE_MAX_CHARS = 300


class ProactiveInitiator:
    """从记忆库抽样 + 组稿 + 排期决策。"""

    def __init__(
        self,
        db: Database,
        client: Optional[DeepSeekClient] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        """
        :param db: 数据库访问对象
        :param client: 模型客户端（缺省时全部走模板降级）
        :param rng: 随机源（测试可注入固定种子）
        """
        self.db = db
        self.client = client
        self.rng = rng or random.Random()

    # ------------------------------------------------------------
    # 记忆抽样
    # ------------------------------------------------------------
    def choose_memory(self, space_id: str) -> Optional[Dict[str, Any]]:
        """
        从记忆库里随机抽一段可以用来"搭话"的内容。

        三类来源：
        - topic：事件级往事（标题 + 摘要）；
        - experience：挂在主题下的经验/结论；
        - relation：结构化信念（谁 的 什么 是 什么）。

        没有可用记忆时返回 None——宁可这次不发，也不硬编一句空话。

        :param space_id: 记忆空间
        :return: {"source","text","topic_id","title"} 或 None
        """
        candidates: List[Dict[str, Any]] = []
        weights: List[float] = []

        # 1) 主题往事
        try:
            topics = TopicStore(self.db).list_topics(space_id, limit=200)
        except Exception:
            topics = []
        for topic in topics:
            text = (topic.abstract or topic.title or "").strip()
            if len(text) < 8:
                continue
            candidates.append(
                {
                    "source": "topic",
                    "text": text,
                    "topic_id": topic.topic_id,
                    "title": topic.title,
                }
            )
            weights.append(_MEMORY_WEIGHTS["topic"])

        # 2) 经验片段
        try:
            rows = self.db.conn().execute(
                """
                select exp_id, topic_id, content
                from experience
                where space_id = ?
                order by create_ts desc
                limit 200
                """,
                (space_id,),
            ).fetchall()
        except Exception:
            rows = []
        for row in rows:
            text = (row["content"] or "").strip()
            if len(text) < 8:
                continue
            candidates.append(
                {
                    "source": "experience",
                    "text": text,
                    "topic_id": row["topic_id"] or "",
                    "title": "",
                }
            )
            weights.append(_MEMORY_WEIGHTS["experience"])

        # 3) 结构化信念（只取重要度较高的，避免把技术细节当谈资）
        try:
            relations = KGStore(self.db).list_relations(space_id, limit=200)
        except Exception:
            relations = []
        for rel in relations:
            if float(rel.get("importance") or 0.0) < 0.5:
                continue
            head = (rel.get("head") or "").strip()
            tail = (rel.get("tail") or "").strip()
            dimension = (rel.get("rel") or "").replace("_", " ").strip()
            if not head or not tail:
                continue
            candidates.append(
                {
                    "source": "relation",
                    "text": f"{head}的{dimension}是{tail}",
                    "topic_id": rel.get("source_topic_id") or "",
                    "title": "",
                }
            )
            weights.append(_MEMORY_WEIGHTS["relation"])

        if not candidates:
            return None

        return self.rng.choices(candidates, weights=weights, k=1)[0]

    # ------------------------------------------------------------
    # 组稿
    # ------------------------------------------------------------
    def compose(
        self,
        memory: Dict[str, Any],
        recent_context: str = "",
        self_view: str = "",
        profile_block: str = "",
    ) -> str:
        """
        把抽中的记忆写成一条主动消息（模型优先，失败走模板）。

        :param memory: choose_memory 的返回
        :param recent_context: 最近对话
        :param self_view: 当前自我认知
        :param profile_block: 身份档案区块
        :return: 消息正文
        """
        memory_text = str(memory.get("text") or "").strip()

        if self.client is not None:
            try:
                messages = prompts.build_initiative_messages(
                    persona_name=config.PERSONA_NAME,
                    memory_text=memory_text,
                    recent_context=recent_context,
                    self_view=self_view,
                    profile_block=profile_block,
                )
                content = self.client.chat(
                    messages,
                    temperature=0.85,
                    max_tokens=240,
                ).strip()
                if content:
                    return content[:_INITIATIVE_MAX_CHARS]
            except LLMError:
                # 组稿失败 → 模板降级，这条主动消息照发
                pass

        return self._fallback(memory_text)

    def _fallback(self, memory_text: str) -> str:
        """模板降级：把记忆片段压短后套进一句日常话。"""
        short = " ".join((memory_text or "").split())
        if len(short) > _FALLBACK_MEMORY_CHARS:
            short = short[:_FALLBACK_MEMORY_CHARS] + "…"
        if not short:
            short = "你"
        return self.rng.choice(_INITIATIVE_TEMPLATES).format(memory=short)

    # ------------------------------------------------------------
    # 排期决策
    # ------------------------------------------------------------
    def decide_delay_minutes(self, recent_context: str = "") -> Dict[str, Any]:
        """
        让模型决定"隔多久再主动找对方说话"。

        模型不可用或输出不合法时，在兜底区间里随机取一个值。
        返回的 minutes 一定落在 PROACTIVE_SCHEDULE_MIN/MAX_MINUTES 之间。

        :param recent_context: 刚才的对话
        :return: {"minutes": int, "reason": str, "source": "llm"/"fallback"}
        """
        low = int(config.PROACTIVE_SCHEDULE_MIN_MINUTES)
        high = int(config.PROACTIVE_SCHEDULE_MAX_MINUTES)
        if high < low:
            low, high = high, low

        if self.client is not None:
            try:
                messages = prompts.build_schedule_messages(
                    persona_name=config.PERSONA_NAME,
                    recent_context=recent_context,
                    min_minutes=low,
                    max_minutes=high,
                    now_text=time.strftime("%Y-%m-%d %H:%M（%A）", time.localtime()),
                )
                data = self.client.chat_json(
                    messages,
                    temperature=0.4,
                    max_tokens=200,
                )
                raw = data.get("delay_minutes")
                if raw is None:
                    raw = data.get("minutes")
                minutes = int(round(float(raw)))
                clamped = max(low, min(high, minutes))
                return {
                    "minutes": clamped,
                    "reason": str(data.get("reason") or "")[:40],
                    "source": "llm",
                    "raw_minutes": minutes,
                }
            except (LLMError, TypeError, ValueError):
                pass

        # 兜底区间必须落在硬性范围内，防止配置互相打架
        fallback_low = max(low, int(config.PROACTIVE_FALLBACK_MIN_MINUTES))
        fallback_high = min(high, int(config.PROACTIVE_FALLBACK_MAX_MINUTES))
        if fallback_high < fallback_low:
            fallback_low, fallback_high = low, high
        minutes = self.rng.randint(fallback_low, fallback_high)
        return {
            "minutes": minutes,
            "reason": "模型不可用，随机排期",
            "source": "fallback",
        }
