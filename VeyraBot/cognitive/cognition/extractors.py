"""
抽取器：把一轮对话转成结构化记忆。

设计取舍：
- 三个抽取（主题 / 信念 / 经验）合并为一次模型调用，降低延迟与成本；
- 但对外仍暴露统一的结果结构，便于将来拆分；
- 所有字段都做程序侧校验与裁剪，不信任模型输出（防幻觉、防越界）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import config
from cognitive.cognition import prompts
from cognitive.llm import DeepSeekClient, LLMError


# 允许的实体类型白名单（防止模型自造类型导致图谱失控）
ENTITY_TYPE_WHITELIST = ("User", "Project", "Component", "Belief", "AgentSelf")


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    """
    把模型给出的数值裁剪到合法区间。

    :param value: 原始值
    :param low: 下界
    :param high: 上界
    :param default: 无法解析时的缺省值
    :return: 裁剪后的浮点数
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _as_text(value: Any, max_chars: int = 0) -> str:
    """
    把任意值转为字符串并按需截断。

    :param value: 原始值
    :param max_chars: 最大长度，0 表示不截断
    :return: 字符串
    """
    text = "" if value is None else str(value).strip()
    if max_chars and len(text) > max_chars:
        return text[:max_chars]
    return text


class ArchiveExtractor:
    """归档抽取器。"""

    def __init__(self, client: Optional[DeepSeekClient] = None) -> None:
        """
        :param client: 模型客户端，缺省由调用方注入
        """
        self.client = client

    def extract(
        self,
        transcript: str,
        self_view: str,
        params: Dict[str, float],
        moods: Dict[str, float],
        extra_note: str = "",
    ) -> Dict[str, Any]:
        """
        执行一次归档抽取，并返回经过校验的结构化结果。

        :param transcript: 本轮完整对话
        :param self_view: 当前自我认知
        :param params: 当前人格参数
        :param moods: 当前情景状态
        :param extra_note: 额外约束提示
        :return: 规范化后的抽取结果字典
        """
        if self.client is None:
            raise LLMError("ArchiveExtractor 缺少模型客户端")

        messages = prompts.build_archive_messages(
            transcript=transcript,
            self_view=self_view,
            params=params,
            moods=moods,
            extra_note=extra_note,
        )

        raw = self.client.chat_json(messages, temperature=config.EXTRACT_TEMPERATURE)
        return self._normalize(raw)

    # ------------------------------------------------------------
    # 规范化
    # ------------------------------------------------------------
    def _normalize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        对模型输出做结构校验与取值裁剪。

        :param raw: 模型原始字典
        :return: 规范化结果
        """
        return {
            "topic": self._normalize_topic(raw.get("topic")),
            "entities": self._normalize_entities(raw.get("entities")),
            "relations": self._normalize_relations(raw.get("relations")),
            "experiences": self._normalize_experiences(raw.get("experiences")),
            "affection_deltas": self._normalize_affection_deltas(
                raw.get("affection_deltas")
            ),
            "persona_deltas": self._normalize_deltas(raw.get("persona_deltas")),
            "persona_mood": self._normalize_mood(raw.get("persona_mood")),
            "self_view_update": self._normalize_self_view(raw.get("self_view_update")),
            "conflict_note": _as_text(raw.get("conflict_note"), 500),
        }

    @staticmethod
    def _normalize_topic(value: Any) -> Dict[str, Any]:
        """规范化主题字段。"""
        if not isinstance(value, dict):
            value = {}

        keywords = value.get("keywords") or []
        if not isinstance(keywords, list):
            keywords = []

        return {
            "title": _as_text(value.get("title"), 40) or "未命名事件",
            "abstract": _as_text(value.get("abstract"), 400),
            "keywords": [_as_text(item, 20) for item in keywords[:8] if _as_text(item, 20)],
        }

    @staticmethod
    def _normalize_entities(value: Any) -> List[Dict[str, Any]]:
        """规范化实体列表。"""
        if not isinstance(value, list):
            return []

        entities: List[Dict[str, Any]] = []
        for item in value[:15]:
            if not isinstance(item, dict):
                continue

            name = _as_text(item.get("name"), 60)
            if not name:
                continue

            etype = _as_text(item.get("etype"), 30) or "Component"
            if etype not in ENTITY_TYPE_WHITELIST:
                etype = "Component"

            attrs = item.get("attrs") if isinstance(item.get("attrs"), dict) else {}

            entities.append({"etype": etype, "name": name, "attrs": attrs})

        return entities

    @staticmethod
    def _normalize_relations(value: Any) -> List[Dict[str, Any]]:
        """规范化关系列表。"""
        if not isinstance(value, list):
            return []

        relations: List[Dict[str, Any]] = []
        for item in value[:20]:
            if not isinstance(item, dict):
                continue

            head = _as_text(item.get("head"), 60)
            rel = _as_text(item.get("rel"), 40)
            tail = _as_text(item.get("tail"), 120)

            # 头尾关系三者缺一不可
            if not head or not rel or not tail:
                continue

            relations.append(
                {
                    "head": head,
                    "rel": rel,
                    "tail": tail,
                    # 多值标识：模型按常识判断该维度是否可以有多个并存取值。
                    # 不给该字段时按单值处理（保守：宁可推翻，不可失控膨胀）
                    "multi": bool(item.get("multi")),
                    "confidence": _clamp(item.get("confidence"), 0.0, 1.0, 0.7),
                    "importance": _clamp(item.get("importance"), 0.0, 1.0, 0.5),
                }
            )

        return relations

    @staticmethod
    def _normalize_affection_deltas(value: Any) -> List[Dict[str, Any]]:
        """
        规范化好感度增量列表。

        :param value: 模型原始输出
        :return: [{"name": 人名, "delta": float, "reason": str}]
        """
        if not isinstance(value, list):
            return []

        deltas: List[Dict[str, Any]] = []
        for item in value[:10]:
            if not isinstance(item, dict):
                continue

            name = _as_text(item.get("name"), 60)
            if not name:
                continue

            reason = _as_text(item.get("reason"), 300)

            # 单轮增量封顶：一轮对话不能把好感打穿，
            # 断交必须是多次负面累积的结果（不是一句话的事）
            raw_delta = _clamp(
                item.get("delta"),
                -config.AFFECTION_MAX_DELTA_PER_TURN,
                config.AFFECTION_MAX_DELTA_PER_TURN,
                0.0,
            )
            if abs(raw_delta) < 0.5:
                continue

            deltas.append({"name": name, "delta": raw_delta, "reason": reason})

        return deltas

    @staticmethod
    def _normalize_experiences(value: Any) -> List[str]:
        """规范化经验片段列表。"""
        if not isinstance(value, list):
            return []

        result: List[str] = []
        for item in value[: config.ARCHIVE_MAX_EXPERIENCES]:
            text = _as_text(item, config.EXPERIENCE_MAX_CHARS)
            if text:
                result.append(text)
        return result

    @staticmethod
    def _normalize_deltas(value: Any) -> Dict[str, Dict[str, Any]]:
        """
        规范化人格参数增量。

        兼容两种输入：
        - 推荐格式：{"param": {"delta": 0.05, "reason": "..."}}
        - 简写格式：{"param": 0.05}（模型偷懒时仍可解析，reason 留空由上层兜底）

        :param value: 模型原始值
        :return: {参数名: {"delta": float, "reason": str}}
        """
        if not isinstance(value, dict):
            return {}

        deltas: Dict[str, Dict[str, Any]] = {}
        for key, raw in value.items():
            # 只接受白名单参数，拒绝模型自创维度
            if key not in config.PERSONA_KERNEL_INIT:
                continue

            reason = ""
            if isinstance(raw, dict):
                raw_delta = raw.get("delta", 0)
                reason = _as_text(raw.get("reason"), 200)
            else:
                raw_delta = raw

            try:
                delta = float(raw_delta)
            except (TypeError, ValueError):
                continue

            # 单轮增量限制在 ±0.15，防止模型一次把人格推飞
            delta = max(-0.15, min(0.15, delta))
            if abs(delta) < 0.005:
                continue

            deltas[key] = {"delta": delta, "reason": reason}

        return deltas

    @staticmethod
    def _normalize_mood(value: Any) -> Optional[Dict[str, Any]]:
        """规范化情景状态。"""
        if not isinstance(value, dict):
            return None

        mode = _as_text(value.get("mode"), 30)
        if mode not in config.MOOD_MODE_WHITELIST:
            return None

        intensity = _clamp(value.get("intensity"), 0.0, 1.0, 0.6)
        if intensity < 0.2:
            return None

        return {"mode": mode, "intensity": intensity}

    @staticmethod
    def _normalize_self_view(value: Any) -> Optional[Dict[str, str]]:
        """规范化自我认知更新。"""
        if not isinstance(value, dict):
            return None

        new_value = _as_text(value.get("new_value"), 300)
        reason = _as_text(value.get("reason"), 300)

        if not new_value:
            return None

        return {"new_value": new_value, "reason": reason or "本轮对话改变了我对自身的理解。"}


class NarrativeGenerator:
    """人格叙事生成器（仅在实质性跃迁时调用）。"""

    def __init__(self, client: Optional[DeepSeekClient] = None) -> None:
        """
        :param client: 模型客户端
        """
        self.client = client

    def generate(
        self,
        topic_title: str,
        topic_abstract: str,
        old_self_view: str,
        old_params: Dict[str, float],
        new_self_view: str,
        new_params: Dict[str, float],
        change_lines: List[str],
        reason: str,
    ) -> str:
        """
        生成新的人格快照叙事段落。

        :return: 叙事文本
        """
        if self.client is None:
            raise LLMError("NarrativeGenerator 缺少模型客户端")

        messages = prompts.build_narrative_messages(
            topic_title=topic_title,
            topic_abstract=topic_abstract,
            old_self_view=old_self_view,
            old_params=old_params,
            new_self_view=new_self_view,
            new_params=new_params,
            change_lines=change_lines,
            reason=reason,
        )

        return self.client.chat(
            messages,
            temperature=0.5,
            max_tokens=400,
        ).strip()
