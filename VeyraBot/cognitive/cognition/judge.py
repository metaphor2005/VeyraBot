"""
信息充足度判决器。

作用：在"人格 + KG + 主题记忆"都就位之后，判断现有材料是否够用，
决定要不要动用 RAG 这个兜底插件。

重要定位：这是"独立一次小模型调用 + 程序侧硬规则兜底"，
不是让模型原生调用函数；真正的执行动作永远由本工程的 Python 代码完成。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import config
from cognitive.cognition import prompts
from cognitive.llm import DeepSeekClient, LLMError
from cognitive.models import SessionContext


# 自我溯源类问题的特征词：这类问题只要记忆里有记录，就不该动用 RAG
_SELF_REFERENCE_HINTS = (
    "你为什么",
    "你自己",
    "你的性格",
    "你的人格",
    "你怎么变",
    "你的自我",
    "你是谁",
    "你的记忆",
    "你之前说",
    "你的想法",
    "你觉得自己",
)


class InfoJudge:
    """信息充足度判决器。"""

    def __init__(self, client: Optional[DeepSeekClient] = None) -> None:
        """
        :param client: 模型客户端
        """
        self.client = client

    def judge(
        self,
        user_message: str,
        self_view: str,
        kg_facts: List[str],
        topics: List[Any],
        experiences: List[str],
        recent_context: str,
        round_index: int = 1,
        max_rounds: int = 1,
        retrieved_snippets: Optional[List[str]] = None,
        tried_queries: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        判决是否需要（再）进行一次 RAG 检索。

        这是有界循环的一环：调用方每轮传 round_index / max_rounds，
        判决器据此知道"这是第几次、还剩几次"；到达上限的那一轮强制收束。

        :param user_message: 用户当前问题
        :param self_view: 当前自我认知
        :param kg_facts: 已召回的结构化信念
        :param topics: 已召回的主题
        :param experiences: 已召回的经验片段
        :param recent_context: 最近对话文本
        :param round_index: 当前是第几次判断（从 1 开始）
        :param max_rounds: 本次对话允许的最大判断次数
        :param retrieved_snippets: 已检索到的片段（用于避免重复检索）
        :param tried_queries: 已尝试过的查询词
        :return: {"is_info_enough","need_rag","rag_query","reason","round_index","max_rounds","is_final_round"}
        """
        retrieved_snippets = retrieved_snippets or []
        tried_queries = tried_queries or []

        # 是否为最后一轮：最后一轮只判断"材料够不够"，不再申请检索
        is_final = config.RAG_LOOP_ENABLED and round_index >= max_rounds

        def _wrap(payload: Dict[str, Any]) -> Dict[str, Any]:
            """统一补齐循环元信息，保证上层拿到的结构一致。"""
            payload["round_index"] = round_index
            payload["max_rounds"] = max_rounds
            payload["is_final_round"] = is_final
            return payload

        # ---------- 硬规则 1：RAG 总开关关闭 ----------
        if not config.ENABLE_RAG:
            return _wrap(
                {
                    "is_info_enough": True,
                    "need_rag": False,
                    "rag_query": "",
                    "reason": "RAG 已在配置中禁用",
                }
            )

        # ---------- 硬规则 2：自我溯源类问题且记忆有料 ----------
        has_self_hint = any(hint in user_message for hint in _SELF_REFERENCE_HINTS)
        has_memory = bool(kg_facts) or bool(topics) or bool(experiences)
        if has_self_hint and has_memory:
            return _wrap(
                {
                    "is_info_enough": True,
                    "need_rag": False,
                    "rag_query": "",
                    "reason": "自我溯源问题，记忆已有对应记录",
                }
            )

        # ---------- 硬规则 3：已到最后一轮，直接收束（不再调模型，省一轮开销） ----------
        if is_final:
            return _wrap(
                {
                    "is_info_enough": False,
                    "need_rag": False,
                    "rag_query": "",
                    "reason": f"已达检索上限（第 {round_index}/{max_rounds} 轮），停止检索，基于现有材料作答",
                }
            )

        # ---------- 模型判决 ----------
        if self.client is None:
            # 无客户端时按保守策略：不启用 RAG
            return _wrap(
                {
                    "is_info_enough": True,
                    "need_rag": False,
                    "rag_query": "",
                    "reason": "判决器未配置模型客户端，按保守策略跳过",
                }
            )

        topic_memory = "\n".join(f"- [{t.title}] {t.abstract}" for t in topics)
        if experiences:
            topic_memory += "\n" + "\n".join(f"- {item}" for item in experiences[:5])

        # 让模型明确知道自己在循环的第几步
        if config.RAG_LOOP_ENABLED:
            loop_state = (
                f"这是第 {round_index} 次判断，本对话最多 {max_rounds} 次判断，"
                f"还剩 {max_rounds - round_index} 次检索机会。若现有材料已够，直接判定无需检索。"
            )
        else:
            loop_state = "（单轮判决，只有一次检索机会）"

        retrieved_block = "\n".join(f"- {item[:120]}" for item in retrieved_snippets) or ""
        queries_text = " / ".join(tried_queries) or ""

        messages = prompts.build_judge_messages(
            user_message=user_message,
            self_view=self_view,
            kg_facts=kg_facts,
            topic_memory=topic_memory,
            recent_context=recent_context,
            loop_state=loop_state,
            retrieved_block=retrieved_block,
            tried_queries=queries_text,
        )

        try:
            raw = self.client.chat_json(
                messages,
                temperature=config.JUDGE_TEMPERATURE,
                max_tokens=config.JUDGE_MAX_TOKENS,
            )
        except LLMError:
            # 判决失败不应阻塞主流程：默认认为材料够用，跳过 RAG
            return _wrap(
                {
                    "is_info_enough": True,
                    "need_rag": False,
                    "rag_query": "",
                    "reason": "判决调用失败，跳过 RAG 以保证响应稳定",
                }
            )

        result = self._normalize(raw, user_message)

        # 查询词与已尝试过的完全重复 → 原地打转，取消本轮检索
        query = result.get("rag_query", "")
        if query and query in tried_queries:
            result["need_rag"] = False
            result["rag_query"] = ""
            result["reason"] = "查询词与已尝试过的重复，判定为原地打转"

        return _wrap(result)

    @staticmethod
    def _normalize(raw: Dict[str, Any], user_message: str) -> Dict[str, Any]:
        """
        校验模型判决，避免出现"要 RAG 但没给查询词"这类无效指令。

        :param raw: 模型原始输出
        :param user_message: 用户原问题
        :return: 规范化判决
        """
        need_rag = bool(raw.get("need_rag", False))
        rag_query = str(raw.get("rag_query") or "").strip()
        reason = str(raw.get("reason") or "").strip()[:120]

        # 请求 RAG 但查询词为空：用用户原问题兜底，否则直接取消
        if need_rag and not rag_query:
            rag_query = user_message.strip()
        if not rag_query:
            need_rag = False

        is_enough = bool(raw.get("is_info_enough", not need_rag))

        return {
            "is_info_enough": is_enough,
            "need_rag": need_rag,
            "rag_query": rag_query,
            "reason": reason,
        }


def build_recent_context(context: SessionContext, rounds: int = 3) -> str:
    """
    生成判决用的最近对话文本。

    :param context: 会话对象
    :param rounds: 取最近多少轮
    :return: 文本
    """
    return context.recent_text(rounds=rounds)
