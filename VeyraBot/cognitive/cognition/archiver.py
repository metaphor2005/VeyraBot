"""
归档控制器：每轮对话结束后的主动认知沉淀。

这是整套系统"像人一样记住"的核心环节，与 token 压缩完全独立：
- 不管 token 是否超限，每轮都跑；
- 不修改短期上下文；
- 产出：主题记忆 + 结构化信念 + 经验片段 + 人格参数演化 + 顶层叙事快照 + 变迁日志。

流程固定为 8 步（见 archive() 注释），任何一步失败都不阻塞对外响应。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional, Tuple

import config
from cognitive.cognition.extractors import ArchiveExtractor, NarrativeGenerator
from cognitive.cognition import prompts
from cognitive.llm import DeepSeekClient, LLMError
from cognitive.models import (
    Experience,
    KGEntity,
    KGRelation,
    PersonaChange,
    SessionContext,
    Topic,
    new_id,
)
from cognitive.persona.changelog import PersonaChangelogStore
from cognitive.persona.kernel import PersonaKernel
from cognitive.persona.snapshot import PersonaSnapshotStore
from cognitive.storage.affection_store import AffectionStore, ProactiveOutbox
from cognitive.storage.database import Database
from cognitive.storage.kg_store import KGStore
from cognitive.storage.topic_store import TopicStore
from cognitive.storage.vector_store import VectorStore


# 主动消息的模板降级文案（模型不可用时的兜底，保证阈值事件一定有消息可发）
_PROACTIVE_TEMPLATES = {
    "reach_out": (
        "{person}，最近总觉得我们之间有点不对劲。是不是我哪里让你不舒服了？"
        "有什么话直说就好，别憋着。"
    ),
    "severance": (
        "{person}，我想了很久。你一直这样对我，那就算了吧。"
        "以后各走各的路，互不打扰。"
    ),
    "reconcile": (
        "{person}，这段时间我想了不少。之前的事，也许有些话说重了。"
        "如果你愿意，我们重新开始吧。"
    ),
}


# 把模型可能写出的"自己"的各种称呼，统一映射到人格实体名
_SELF_ALIASES = ("我", "自己", "你", "本人", "本助手", "助手")


def _mentions_self(name: str) -> bool:
    """判断某个实体名是不是在指人格本身（含"我/你/助手"这类自指写法）。"""
    text = str(name or "").strip()
    if not text:
        return False
    if config.PERSONA_NAME in text:
        return True
    return any(alias in text for alias in _SELF_ALIASES)


def judge_big_event(relations: List[dict], user_text: str) -> Tuple[bool, str]:
    """
    大事判定（纯函数，便于测试，不依赖模型）。

    规则：
    1. 用户消息命中长期指令词 → 大事；
    2. 某条信念 importance ≥ BIG_EVENT_IMPORTANCE 且关系头/尾是她 → 大事；
       若关掉 BIG_EVENT_REQUIRE_SELF，则退回纯阈值判定。

    :param relations: 抽取出的关系列表，每项含 head / tail / importance
    :param user_text: 本轮用户消息
    :return: (是否大事, 判定理由)
    """
    max_importance = max(
        (float(item.get("importance", 0.0)) for item in relations), default=0.0
    )
    hint_hit = any(hint in (user_text or "") for hint in config.BIG_EVENT_HINTS)

    self_related = any(
        _mentions_self(item.get("head")) or _mentions_self(item.get("tail"))
        for item in relations
    )
    by_importance = max_importance >= config.BIG_EVENT_IMPORTANCE and (
        self_related or not config.BIG_EVENT_REQUIRE_SELF
    )

    if hint_hit:
        return True, "命中长期指令词"
    if by_importance:
        reason = (
            f"importance={max_importance:.2f} ≥ {config.BIG_EVENT_IMPORTANCE}，且涉及自身定位"
            if config.BIG_EVENT_REQUIRE_SELF
            else f"importance={max_importance:.2f} ≥ {config.BIG_EVENT_IMPORTANCE}"
        )
        return True, reason
    return False, ""


# 人格更新准入守卫：只有对话里出现"塑造自我"的迹象，才允许修改人格
# 目的：防止模型把"回答风格"误判为"人格变化"，导致人格每轮剧变、快照爆炸
_SELF_SHAPING_HINTS = (
    "你应该",
    "你可以",
    "你是",
    "你希望",
    "你的性格",
    "你的人格",
    "你自己",
    "你怎么看",
    "你觉得",
    "我希望你",
    "我认为你",
    "你怎么理解",
    "你是谁",
    "你不再是",
    "你不只是",
    "你的想法",
    "你的价值",
    "你的定位",
    # 日常口语里"被说中性格"的说法（江妍这类人格设定下更常见的触发方式）
    "被当成",
    "在意别人",
    "你其实",
    "你老说",
    "你怎么想",
    "你的存在",
    "你不是工具",
)


def _stable_id(*parts: str) -> str:
    """
    生成稳定的短 id（同样输入必得同样输出，用于实体/关系去重）。

    :param parts: 参与哈希的字段
    :return: 短 id
    """
    raw = "|".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


class MemoryArchiver:
    """记忆归档控制器。"""

    def __init__(self, db: Database, client: Optional[DeepSeekClient] = None) -> None:
        """
        :param db: 数据库访问对象
        :param client: 模型客户端
        """
        self.db = db
        self.client = client
        self.extractor = ArchiveExtractor(client)
        self.narrator = NarrativeGenerator(client)

    # ------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------
    def archive(self, context: SessionContext) -> Dict[str, Any]:
        """
        执行一次完整归档。

        :param context: 当前会话（取最后若干轮对话作为本轮）
        :return: 归档报告（用于终端观测与调试）
        """
        report: Dict[str, Any] = {
            "ok": False,
            "topic_id": "",
            "topic_title": "",
            "entities": 0,
            "relations": 0,
            "conflicts": 0,
            "experiences": 0,
            "param_changes": [],
            "substantive": False,
            "persona_gate": False,
            "degraded": False,
            "is_big_event": False,
            "big_event_reason": "",
            "snapshot_version": 0,
            "changelog_entries": 0,
            "affection": [],
            "proactive_messages": [],
            "error": "",
        }

        if not config.ENABLE_ARCHIVE:
            report["error"] = "归档已在配置中禁用"
            return report

        space_id = context.space_id

        # ---------- 步骤 0：准备本轮文本 ----------
        transcript = self._build_transcript(context)
        if not transcript:
            report["error"] = "本轮没有可归档的对话内容"
            return report

        # ---------- 步骤 1：读取当前人格状态 ----------
        kernel = PersonaKernel(self.db, space_id)
        kg = KGStore(self.db)
        snapshots = PersonaSnapshotStore(self.db, space_id, kernel, kg)
        changelog = PersonaChangelogStore(self.db, space_id)
        topics_store = TopicStore(self.db)
        vectors = VectorStore(space_id)

        current_snapshot = snapshots.load_current()
        old_self_view = current_snapshot.self_view
        params_before = kernel.get_params()
        moods_before = kernel.get_moods()
        version_before = kernel.get_current_version()

        # ---------- 步骤 2：结构化抽取 ----------
        try:
            extracted = self.extractor.extract(
                transcript=transcript,
                self_view=old_self_view,
                params=params_before,
                moods=moods_before,
            )
        except LLMError as exc:
            report["error"] = f"抽取失败：{exc}"

            # 降级归档：抽取模型不可用时，仍保存一条最小主题，
            # 保证这轮对话不会从记忆里彻底消失（后续可被召回）
            fallback = self._save_fallback_topic(context, space_id, transcript)
            if fallback is not None:
                report["ok"] = True
                report["degraded"] = True
                report["topic_id"] = fallback.topic_id
                report["topic_title"] = fallback.title
            return report

        # ---------- 步骤 2.5：人格更新准入守卫 ----------
        # 本轮对话没有"塑造自我"的迹象时，丢弃模型给出的人格更新，
        # 只保留事实与经验沉淀（人格必须缓慢演化，不能每轮剧变）
        persona_gate = self._persona_update_allowed(transcript)
        report["persona_gate"] = persona_gate
        if not persona_gate:
            extracted["persona_deltas"] = {}
            extracted["persona_mood"] = None
            extracted["self_view_update"] = None

        # ---------- 步骤 3：写入主题记忆（事件级） ----------
        topic_data = extracted["topic"]
        topic = Topic(
            topic_id=new_id("topic"),
            space_id=space_id,
            title=topic_data["title"],
            abstract=topic_data["abstract"],
            keywords=topic_data["keywords"],
            session_id=context.session_id,
            snapshot_version=version_before,
        )
        topics_store.save_topic(topic)

        # 向量索引只索引"主题摘要"，不索引对话碎片
        vector_text = f"{topic.title}。{topic.abstract}"
        vectors.add(
            doc_id=topic.topic_id,
            text=vector_text,
            meta={
                "title": topic.title,
                "keywords": topic.keywords,
                "session_id": context.session_id,
            },
        )

        report["topic_id"] = topic.topic_id
        report["topic_title"] = topic.title

        # ---------- 步骤 4：写入结构化信念 ----------
        conflict_records: List[str] = []

        for entity in extracted["entities"]:
            name = self._normalize_self_name(entity["name"])
            kg.upsert_entity(
                KGEntity(
                    eid=_stable_id(space_id, entity["etype"], name),
                    etype=entity["etype"],
                    name=name,
                    space_id=space_id,
                    attrs=entity.get("attrs", {}),
                )
            )
            report["entities"] += 1

        for relation in extracted["relations"]:
            head = self._normalize_self_name(relation["head"])
            tail = self._normalize_self_name(relation["tail"])

            outcome = kg.upsert_relation(
                KGRelation(
                    rid=_stable_id(space_id, head, relation["rel"], tail),
                    space_id=space_id,
                    head_name=head,
                    rel=relation["rel"],
                    tail_name=tail,
                    confidence=relation["confidence"],
                    importance=relation["importance"],
                    multi_valued=bool(relation.get("multi")),
                    source_topic_id=topic.topic_id,
                )
            )
            report["relations"] += 1
            for old_value, new_value in outcome["conflicts"]:
                conflict_records.append(
                    f"{head} {relation['rel']}：{old_value} -> {new_value}"
                )
                report["conflicts"] += 1

                # 核心信念被覆盖 → 人格层也要留一笔。
                # 原先信念更替只写进 experience，persona_changelog 里查不到，
                # 于是"你为什么改了想法"只能答参数漂移，答不到信念本身。
                #
                # 两档门槛（别只按"涉及自我"放行，否则"我更喜欢橘子了"也会天天进日志）：
                #   1) 重要度够高，不管涉及谁；
                #   2) 涉及她自己，且重要度过第二道下限。
                importance_now = float(relation["importance"])
                involves_self = _mentions_self(head) or _mentions_self(tail)
                is_core_belief = (
                    importance_now >= config.PERSONA_BELIEF_LOG_IMPORTANCE
                    or (
                        involves_self
                        and importance_now >= config.PERSONA_BELIEF_LOG_SELF_IMPORTANCE
                    )
                )
                if is_core_belief:
                    changelog.append(
                        PersonaChange(
                            log_id=new_id("plog"),
                            space_id=space_id,
                            topic_id=topic.topic_id,
                            # 维度名用"槽位"（谁 + 哪个关系）：可读，也能按槽位过滤。
                            # belief_id 落地后可平滑换成 belief:{13位编号}
                            persona_field=f"belief:{head}·{relation['rel']}",
                            old_value=old_value,
                            new_value=new_value,
                            reason=topic.abstract[:200] or "对话中改了想法",
                            source_type="dialogue",
                            snapshot_from=version_before,
                            snapshot_to=version_before,
                        )
                    )
                    report["changelog_entries"] += 1

        # ---------- 步骤 4b：好感度演化与主动消息 ----------
        # 好感度不属于"人格"（人格必须缓慢演化），属于对具体人的情感温度，
        # 所以不受步骤 2.5 的人格准入守卫限制——被冒犯了就该立刻记下；
        # 但单轮增量已在抽取层封顶，断交只能靠多次负面累积，不是一句话的事。
        proactive_messages: List[Dict[str, Any]] = []

        if config.AFFECTION_ENABLED:
            affection_store = AffectionStore(self.db)
            outbox = ProactiveOutbox(self.db)

            for item in extracted.get("affection_deltas", []):
                person = self._normalize_self_name(item["name"])
                # 对自己的"好感度"没有意义，跳过
                if not person or _mentions_self(person):
                    continue

                outcome = affection_store.apply_delta(
                    space_id,
                    person,
                    item["delta"],
                    reason=item.get("reason", ""),
                    topic_id=topic.topic_id,
                )
                report["affection"].append(
                    {
                        "person": person,
                        "before": outcome["before"],
                        "after": outcome["after"],
                        "status": outcome["status"],
                        "events": outcome["events"],
                    }
                )

                events = outcome["events"]
                if not events:
                    # 未跨阈值：只记录数值，不主动发消息
                    continue

                # 一轮同时跨过 cold 和 severed 时只发断交（断交声明已涵盖"我们有问题"）
                if "severed" in events:
                    kind = "severance"
                elif "reconciled" in events:
                    kind = "reconcile"
                else:
                    kind = "reach_out"

                content = self._compose_proactive_message(
                    kind=kind,
                    person=person,
                    outcome=outcome,
                    reason=item.get("reason", ""),
                )
                message = outbox.add(
                    space_id,
                    kind=kind,
                    person_name=person,
                    content=content,
                    affection_value=outcome["after"],
                    topic_id=topic.topic_id,
                    session_id=context.session_id,
                )
                proactive_messages.append(message)

        report["proactive_messages"] = proactive_messages

        # ---------- 步骤 5：写入经验片段 ----------
        for content in extracted["experiences"]:
            topics_store.add_experience(
                Experience(
                    exp_id=new_id("exp"),
                    space_id=space_id,
                    topic_id=topic.topic_id,
                    content=content,
                )
            )
            report["experiences"] += 1

        # 信念更替也留痕（写入经验，避免污染人格日志）
        for conflict in conflict_records:
            topics_store.add_experience(
                Experience(
                    exp_id=new_id("exp"),
                    space_id=space_id,
                    topic_id=topic.topic_id,
                    content=f"【信念更替】{conflict}",
                )
            )

        # ---------- 步骤 6：人格符号层演化 ----------
        # 抽取结果为 {参数名: {"delta":.., "reason":..}}，这里只把数值交给内核
        persona_deltas = extracted.get("persona_deltas") or {}
        delta_map = {
            name: float((item or {}).get("delta", 0.0))
            for name, item in persona_deltas.items()
        }
        param_changes = kernel.apply_deltas(delta_map)
        report["param_changes"] = [
            {"param": param, "old": round(old_value, 3), "new": round(new_value, 3)}
            for param, old_value, new_value in param_changes
        ]

        mood_data = extracted["persona_mood"]
        if mood_data:
            kernel.set_mood(mood_data["mode"], mood_data["intensity"])

        # 自我认知更新
        self_view_update = extracted["self_view_update"]
        new_self_view = (
            self_view_update["new_value"] if self_view_update else old_self_view
        )
        self_view_changed = (
            bool(new_self_view.strip())
            and new_self_view.strip() != old_self_view.strip()
        )

        # ---------- 步骤 7：本轮不跃迁，只登记变化理由 ----------
        # 人格版本改由"日结结算"产生（与一天一变同步）。本轮只负责三件事：
        # 1) apply_deltas 已把增量记入 pending（基调未动）
        # 2) 把新的自我认知写进即时层，等日结固化
        # 3) 记录本轮变化理由，供日结写变迁日志时引用
        report["substantive"] = False
        report["snapshot_version"] = version_before

        reason_text = ""
        if self_view_update and self_view_update.get("reason"):
            reason_text = self_view_update["reason"]
        elif param_changes:
            reason_text = "；".join(
                f"{param} {new_value - old_value:+.3f}"
                for param, old_value, new_value in param_changes
            )
        else:
            reason_text = topic.abstract[:160]
        if extracted["conflict_note"]:
            reason_text = f"{reason_text}（冲突说明：{extracted['conflict_note']}）"
        report["reason_text"] = reason_text

        if self_view_changed:
            # 即时自我认知（不版本化），日结时才写进快照
            kernel.set_live_self_view(new_self_view)
            report["self_view_live"] = new_self_view

        # ---------- 步骤 8：情景状态衰减 ----------
        kernel.decay_moods()

        # ---------- 步骤 9：大事判定 ----------
        # 两类信号：
        # 1. 用户消息里出现明确的长期指令词（"从今天起""以后都""定下来"…）→ 直接算大事
        # 2. 某条信念 importance 达标 **且** 涉及人格自身（关系头/尾是她）→ 算大事
        #    只按 importance 判会把"查资料/讲技术"这类高价值但与她无关的内容也沉淀成事件记忆，
        #    实测 6 轮里误判 3~4 次，所以默认要求"涉及自我"（BIG_EVENT_REQUIRE_SELF）
        user_text = ""
        for msg in reversed(context.messages):
            if msg.role == "user":
                user_text = msg.content or ""
                break

        report["is_big_event"], report["big_event_reason"] = judge_big_event(
            extracted["relations"], user_text
        )

        if report["is_big_event"]:
            topics_store.mark_big_event(space_id, topic.topic_id)

        report["ok"] = True
        return report

    def _compose_proactive_message(
        self,
        kind: str,
        person: str,
        outcome: Dict[str, Any],
        reason: str,
    ) -> str:
        """
        组稿一条主动消息：优先模型生成，失败走模板降级。

        与归档其它环节一致的取舍：阈值事件的消息是"一定会发生"的行为，
        模型不可用不能吞掉这条消息，模板是兜底而不是可选项。

        :param kind: reach_out（主动联系）/ severance（断交）/ reconcile（和解）
        :param person: 收件人
        :param outcome: apply_delta 的返回（含 before / after / status）
        :param reason: 好感变化原因（来自抽取结果）
        :return: 消息正文
        """
        if self.client is not None:
            try:
                messages = prompts.build_proactive_messages(
                    persona_name=config.PERSONA_NAME,
                    person=person,
                    kind=kind,
                    value_before=float(outcome["before"]),
                    value_after=float(outcome["after"]),
                    status=str(outcome["status"]),
                    reason=reason,
                )
                content = self.client.chat(
                    messages,
                    temperature=0.8,
                    max_tokens=200,
                ).strip()
                if content:
                    return content
            except LLMError:
                # 组稿失败 → 模板降级，消息照发
                pass

        template = _PROACTIVE_TEMPLATES.get(kind) or _PROACTIVE_TEMPLATES["reach_out"]
        return template.format(person=person)

    # ------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------
    def _save_fallback_topic(
        self, context: SessionContext, space_id: str, transcript: str
    ) -> Optional[Topic]:
        """
        抽取失败时的降级归档：只保存一条最小主题，保证记忆链不断。

        :param context: 会话对象
        :param space_id: 记忆空间
        :param transcript: 本轮对话文本
        :return: 生成的主题对象；失败返回 None
        """
        try:
            # 取最后一条用户消息作为标题来源
            user_text = ""
            for msg in reversed(context.messages):
                if msg.role == "user" and msg.content:
                    user_text = msg.content
                    break

            if not user_text:
                return None

            title = user_text.strip().replace("\n", " ")[:22]
            topic = Topic(
                topic_id=new_id("topic"),
                space_id=space_id,
                title=title,
                abstract=transcript[:300],
                keywords=[],
                session_id=context.session_id,
                snapshot_version=PersonaKernel(self.db, space_id).get_current_version(),
            )

            TopicStore(self.db).save_topic(topic)
            VectorStore(space_id).add(
                doc_id=topic.topic_id,
                text=f"{topic.title}。{topic.abstract}",
                meta={
                    "title": topic.title,
                    "degraded": True,
                    "session_id": context.session_id,
                },
            )
            return topic
        except Exception:
            # 降级本身失败不影响主流程
            return None

    @staticmethod
    def _build_transcript(context: SessionContext) -> str:
        """
        取出本轮对话文本（最后一条 user + 最后一条 assistant）。

        :param context: 会话对象
        :return: 对话文本
        """
        messages = context.messages
        if not messages:
            return ""

        # 找到最后一条 assistant，回溯一条 user
        end_index = len(messages) - 1
        if messages[end_index].role != "assistant":
            return ""

        start_index = end_index - 1
        if start_index < 0:
            return ""

        lines = [
            f"user: {messages[start_index].content}",
            f"assistant: {messages[end_index].content}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _normalize_self_name(name: str) -> str:
        """
        把指代"自己"的各种说法统一为人格实体名，保证图谱节点不分裂。

        :param name: 原始名称
        :return: 规范化名称
        """
        clean = (name or "").strip()
        if clean in _SELF_ALIASES or clean.startswith("认知体"):
            return config.PERSONA_NAME
        return clean

    @staticmethod
    def _persona_update_allowed(transcript: str) -> bool:
        """
        判断本轮是否允许修改人格。

        判定标准：对话中出现"对自我的讨论或塑造"，例如"你希望""你觉得""你的定位"。

        :param transcript: 本轮完整对话
        :return: 是否允许
        """
        return any(hint in transcript for hint in _SELF_SHAPING_HINTS)
