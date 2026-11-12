"""
认知引擎：把各模块编排成一条完整的处理链。

每轮对话的固定顺序（与老 RAG 范式相反，记忆与人格优先）：
1. 加载/新建会话（内存 + 持久双写）
2. 追加用户消息
3. token 统计 → 必要时兜底压缩（应急，不是归档）
4. 读取人格快照 + 符号内核 → 组装自我认知与行为倾向
5. 记忆路由：KG 精确查询 + 主题记忆召回
6. 自我溯源增强：问题涉及"你为什么变"时，注入人格变迁史
7. 有界检索循环：判断是否需要检索 → 检索 → 再判断，最多 N 轮，最后一轮强制出结论
8. 组装 Prompt → 生成回答
9. 追加 assistant 消息（按分隔符/空行先切分成多条，见 split_reply）
10. 主动归档（主题 / 信念 / 经验 / 人格演化 / 快照 / 日志）
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import config
from cognitive.cognition.archiver import MemoryArchiver
from cognitive.cognition.consolidator import Consolidator
from cognitive.cognition.judge import InfoJudge
from cognitive.cognition.prompts import build_answer_messages, render_persona_profile
from cognitive.cognition.rag import RAGRegistry
from cognitive.cognition.router import MemoryRouter
from cognitive.llm import DeepSeekClient, LLMError, get_client
from cognitive.models import PersonaSnapshot, RetrievedMemory, SessionContext
from cognitive.persona.changelog import PersonaChangelogStore
from cognitive.persona.kernel import PersonaKernel
from cognitive.persona.snapshot import PersonaSnapshotStore
from cognitive.proactive import ProactiveInitiator
from cognitive.session import SessionManager
from cognitive.storage.affection_store import (
    AffectionStore,
    ProactiveOutbox,
    ProactivePolicyStore,
    ProactiveScheduleStore,
)
from cognitive.storage.database import Database
from cognitive.storage.kg_store import KGStore
from cognitive.storage.topic_store import TopicStore
from cognitive.tools.base import ToolManager


# 自我溯源类问题的特征词
_SELF_INQUIRY_HINTS = (
    "你为什么",
    "你怎么变",
    "你的性格",
    "你的人格",
    "你的自我",
    "你是谁",
    "你的记忆",
    "你之前说",
    "你觉得自己",
    "你的想法",
)


# ============================================================
# 回复切分（"她连发几条消息"）
# ============================================================
# 纯分隔符行：单独成行的 --- / —— / *** / === …（至少两个符号字符）
_SEPARATOR_LINE = re.compile(r"\n[ \t]*[-—–_*~=·|｜]{2,}[ \t]*\n")
# 连续空行：她"换了段"，也视为下一条消息
_BLANK_LINE = re.compile(r"\n{2,}")

# 一次回复最多拆成几条消息（防止刷屏，多出来的并进最后一条）
REPLY_MAX_BUBBLES = 5


def split_reply(text: str, max_bubbles: int = REPLY_MAX_BUBBLES) -> List[str]:
    """
    把一条模型回复切分成多条"连发消息"。

    规则（与 Web UI 的 splitReply 保持一致，避免两边渲染不一致）：
    1. 单独成行的分隔符（--- / —— / *** 等）先切一刀；
    2. 空行分段也算"下一条消息"；
    3. 超过上限时，多余段落并进最后一条。

    切分发生在入库之前：session_store 里存的就是多条 assistant 消息，
    客户端（Web UI / 第三方开发者）重新加载历史时能直接还原成多个气泡，
    不会合并回一整块。

    :param text: 原始回复文本
    :param max_bubbles: 最大消息条数
    :return: 切分后的消息列表（至少一个元素）
    """
    raw = (text or "").strip()
    if not raw:
        return [""]

    parts: List[str] = []
    for chunk in _SEPARATOR_LINE.split(raw):
        parts.extend(_BLANK_LINE.split(chunk))

    cleaned = [p.strip() for p in parts if p and p.strip()]
    if len(cleaned) <= 1:
        # 没有可切的位置：整条原样返回（保留原文，不丢空白）
        return [raw]
    if len(cleaned) > max_bubbles:
        head = cleaned[: max_bubbles - 1]
        head.append("\n\n".join(cleaned[max_bubbles - 1:]))
        return head
    return cleaned


class CognitiveAgent:
    """具备分层记忆与演化人格的认知主体。"""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        client: Optional[DeepSeekClient] = None,
        enable_tools: bool = False,
    ) -> None:
        """
        :param db_path: 数据库路径，缺省读 config
        :param client: 模型客户端，缺省走全局单例
        :param enable_tools: 是否启用工具插件层（默认关闭，接口已预留）
        """
        # 配置校验：构造入口即校验。坏配置在这里就炸出来，
        # 而不是等到第一次模型调用才以更难排查的方式失败
        config.validate_config()

        self.db = Database(db_path)
        self.client = client or get_client()

        # 短期工作记忆
        self.sessions = SessionManager(self.db, self.client)

        # 记忆路由与判决
        self.router = MemoryRouter(self.db)
        self.judge = InfoJudge(self.client)

        # 归档
        self.archiver = MemoryArchiver(self.db, self.client)

        # 长期巩固（日结 / 大事沉淀）；日结由 maintenance.py 定时驱动
        self.consolidator = Consolidator(self.db, self.client)

        # RAG 兜底（本地语料，可按需注册其他提供者）
        self.rag = RAGRegistry()

        # 工具插件层（预留，不注册任何具体工具）
        self.tools = ToolManager()
        self.enable_tools = enable_tools

    # ------------------------------------------------------------
    # 空间初始化
    # ------------------------------------------------------------
    def ensure_space(self, space_id: str, name: str = "") -> None:
        """
        注册记忆空间并初始化人格基线（幂等）。

        :param space_id: 空间 id
        :param name: 空间名称
        """
        self.db.ensure_space(space_id, name)
        PersonaKernel(self.db, space_id).ensure_initialized()

    # ------------------------------------------------------------
    # 内部：装配空间相关的存储对象
    # ------------------------------------------------------------
    def _stores(self, space_id: str):
        """
        构造当前空间的人格相关存储对象。

        :param space_id: 空间 id
        :return: (kernel, kg, snapshots, changelog)
        """
        kernel = PersonaKernel(self.db, space_id)
        kg = KGStore(self.db)
        snapshots = PersonaSnapshotStore(self.db, space_id, kernel, kg)
        changelog = PersonaChangelogStore(self.db, space_id)
        return kernel, kg, snapshots, changelog

    # ------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------
    def chat(
        self,
        user_message: str,
        session_id: str,
        space_id: str = "default",
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        处理一轮用户输入。

        :param user_message: 用户消息
        :param session_id: 会话 id
        :param space_id: 记忆空间 id
        :param verbose: 是否在返回值里附带详细链路信息
        :return: {"answer": str, "answer_parts": [str], "debug": dict,
                  "proactive_messages": [dict]}
        """
        started_at = time.time()
        self.ensure_space(space_id)

        # ---------- 本轮轮次标识（P0：归档幂等依赖轮次，同一轮只沉淀一次） ----------
        turn_id = new_id("turn")

        debug: Dict[str, Any] = {
            "space_id": space_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "compress": {},
            "judge": {},
            "memory": {},
            "rag_used": False,
            "archive": {},
            "persona_version": 0,
            "elapsed_ms": 0,
            "error": "",
        }

        # ---------- 步骤 0：收到消息 → 作废尚未到期的主动联系时间表 ----------
        # 一次时间表只能用一次：要么到点兑现，要么被用户先开口作废。
        # 作废后不在这里重排——等本轮回答结束再让模型决定下一次。
        if config.PROACTIVE_ENABLED:
            try:
                ProactiveScheduleStore(self.db).cancel(space_id, session_id)
            except Exception as exc:
                debug["error"] = f"主动联系时间表作废失败：{type(exc).__name__}: {exc}"

        # ---------- 步骤 1-2：加载会话并追加用户消息 ----------
        context = self.sessions.get_or_create(session_id, space_id)
        self.sessions.append(context, "user", user_message, turn_id=turn_id)

        # ---------- 步骤 3：token 统计与兜底压缩 ----------
        compress_report = self.sessions.maybe_compress(context)
        debug["compress"] = compress_report

        # ---------- 步骤 4：读取人格状态 ----------
        kernel, kg, snapshots, changelog = self._stores(space_id)
        snapshot = snapshots.load_current()
        # 自我认知取"即时层"：日内可以微调，版本号仍由日结推进
        live_self_view = snapshots.current_self_view()
        tendency = kernel.describe()
        core_beliefs = kg.query_core_beliefs(space_id)
        debug["persona_version"] = snapshot.version

        # ---------- 步骤 5：记忆路由 ----------
        # 记忆召回是"增强项"而不是主链路：存储层出任何异常都降级为空记忆，
        # 本轮对话继续（宁可少想起往事，也不能整轮崩掉）
        try:
            memory = self.router.route(space_id, user_message)
        except Exception as exc:
            memory = RetrievedMemory()
            debug["error"] = f"记忆路由降级：{type(exc).__name__}: {exc}"
        debug["memory"] = {
            "kg_facts": len(memory.kg_facts),
            "overturned": len(memory.overturned),
            "topics": len(memory.topics),
            "experiences": len(memory.experiences),
        }

        # 记忆区块（人格变迁史会前置注入）
        memory_block = memory.to_prompt_block()

        # ---------- 步骤 6：自我溯源增强 ----------
        if self._is_self_inquiry(user_message):
            history_text = changelog.render_history(limit=10)
            self_view_block = (
                f"【你的当前自我认知（第 {snapshot.version} 版）】\n"
                f"{snapshot.narrative or live_self_view}\n\n"
                f"【你的人格变迁史】\n{history_text}"
            )
            memory_block = f"{self_view_block}\n\n{memory_block}"

        # ---------- 步骤 7：有界检索循环（判断 → 检索 → 再判断 → …） ----------
        # 注意：这里必须传真实对话正文，不能传调试串，否则判决器看不到上下文
        recent_context = context.recent_text(rounds=3)
        loop = self._run_rag_loop(
            user_message=user_message,
            snapshot=snapshot,
            memory=memory,
            recent_context=recent_context,
        )
        debug["judge"] = loop["last_judge"]
        debug["rag_loop"] = {
            "rounds": loop["rounds"],
            "max_rounds": loop["max_rounds"],
            "queries": loop["queries"],
            "hits": len(loop["snippets"]),
            "stopped_by": loop["stopped_by"],
            "trace": loop["trace"],
            "note": loop["note"],
        }

        if loop["snippets"]:
            memory.rag_snippets = loop["snippets"]
            memory.rag_used = True
            debug["rag_used"] = True
            # RAG 结果晚于 memory_block 生成，需要重新渲染
            memory_block = memory.to_prompt_block()
            if self._is_self_inquiry(user_message):
                memory_block = f"{self_view_block}\n\n{memory_block}"

        # ---------- 步骤 7b：关系温度注入 ----------
        # 放在所有 memory_block 重渲染路径之后，避免被 RAG 重新渲染覆盖。
        # 用户消息里提到的人 + 状态异常（冷淡/断交）的人进入 Prompt，
        # 决定她用什么语气接话——被断交过的人再开口，态度不该和从前一样
        try:
            affection_block = self._render_affection_block(space_id, user_message)
        except Exception as exc:
            affection_block = ""
            debug["error"] = f"关系温度注入降级：{type(exc).__name__}: {exc}"
        if affection_block:
            memory_block = f"{affection_block}\n\n{memory_block}"

        # ---------- 步骤 8：组装 Prompt 并生成回答 ----------
        base_messages = build_answer_messages(
            persona_name=config.PERSONA_NAME,
            self_view=live_self_view,
            tendency=tendency,
            core_beliefs=core_beliefs,
            memory_block=memory_block,
            compressed_summary=context.compressed_summary,
            user_message=user_message,
            rag_loop_note=loop["note"],
        )

        # 插入最近若干轮原始对话（不含本轮，本轮已由 user_content 承载）
        inject_rounds = max(1, int(config.HISTORY_INJECT_ROUNDS))
        history_messages: List[Dict[str, str]] = [
            {"role": msg.role, "content": msg.content}
            for msg in context.messages[:-1][-(inject_rounds * 2) :]
            if msg.content
        ]

        messages = [base_messages[0]] + history_messages + [base_messages[1]]

        try:
            answer = self.client.chat(
                messages,
                temperature=config.ANSWER_TEMPERATURE,
                max_tokens=config.ANSWER_MAX_TOKENS,
            )
        except LLMError as exc:
            # 回答失败：回滚本轮用户消息，避免污染上下文
            context.messages = context.messages[:-1]
            self.sessions.save(context)
            debug["error"] = str(exc)
            # 明确标记"未归档"，避免外部误读为归档本身出错
            debug["archive"] = {
                "ok": False,
                "skipped": True,
                "error": "本轮回答生成失败，未执行归档",
            }
            # 时间表已在步骤 0 作废；即使这轮没答上来，也按兜底区间重排一张，
            # 免得模型恢复后她再也不会主动开口（模型仍不可用时走随机兜底）
            if config.PROACTIVE_ENABLED:
                try:
                    debug["proactive_schedule"] = self._plan_next_proactive(
                        space_id, session_id, context
                    )
                except Exception as inner:
                    debug["proactive_schedule"] = {
                        "ok": False,
                        "error": f"{type(inner).__name__}: {inner}",
                    }
            debug["elapsed_ms"] = int((time.time() - started_at) * 1000)
            fail_text = f"（本轮生成失败：{exc}）"
            return {
                "answer": fail_text,
                "answer_parts": [fail_text],
                "debug": debug,
                "proactive_messages": [],
            }

        # ---------- 步骤 9：追加回答 ----------
        answer = answer.strip()
        # 先切分再入库：她用分隔符/换段表示"连发几条消息"。
        # session_store 存多条 assistant 消息，历史加载时直接还原成多个气泡。
        answer_parts = split_reply(answer)
        for part in answer_parts:
            self.sessions.append(context, "assistant", part, turn_id=turn_id)

        # ---------- 步骤 10：主动归档 ----------
        if config.ARCHIVE_SYNC:
            archive_report = self._run_archive(context, user_message, answer_parts, turn_id)
        else:
            # ARCHIVE_SYNC=false：放到后台线程，避免阻塞用户响应。
            # 后台线程复用同一套归档逻辑，正常情况与同步模式一致。
            archive_report = self._archive_async(context, user_message, answer_parts, turn_id)
        debug["archive"] = archive_report

        # ---------- 步骤 10b：大事 → 立即做一次事件级沉淀 ----------
        # 只有"大事"走这里（不等日结）。
        # 人格跃迁不再由单轮触发，改由日结结算产生（见 consolidator._settle_persona），
        # 否则 6 轮就能连跳两版，"不要变化太快"这条约束就守不住。
        if (
            archive_report.get("ok")
            and config.BIG_EVENT_TRIGGER
            and archive_report.get("is_big_event")
        ):
            reason = archive_report.get("big_event_reason") or ""
            try:
                # 避免循环依赖：直接使用TopicStore和Consolidator，而不是通过self
                from cognitive.storage.topic_store import TopicStore
                from cognitive.cognition.consolidator import Consolidator

                topic = TopicStore(self.db).get_topic(
                    space_id, archive_report.get("topic_id", "")
                )
                if topic is not None:
                    debug["big_event"] = Consolidator(
                        self.db, self.client
                    ).consolidate_event(space_id, topic)
                    debug["big_event"]["trigger"] = reason
            except Exception as exc:  # 沉淀失败不影响主流程
                debug["big_event"] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }

        # ---------- 步骤 11：让模型决定下一次主动联系的时间 ----------
        # 每轮回答结束都重新排一张表；排期失败不影响本轮回答（走兜底区间）。
        if config.PROACTIVE_ENABLED:
            try:
                debug["proactive_schedule"] = self._plan_next_proactive(
                    space_id, session_id, context
                )
            except Exception as exc:
                debug["proactive_schedule"] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }

        debug["elapsed_ms"] = int((time.time() - started_at) * 1000)

        # 主动消息（好感度阈值触发）挂到顶层，供 API / UI 直接取用，
        # 不必去 debug["archive"] 里翻
        proactive: List[Dict[str, Any]] = list(
            archive_report.get("proactive_messages") or []
        )
        return {
            "answer": answer,
            "answer_parts": answer_parts,
            "debug": debug,
            "proactive_messages": proactive,
        }

    # ------------------------------------------------------------
    # 观测接口
    # ------------------------------------------------------------
    def _run_archive(
        self,
        context: SessionContext,
        user_message: str,
        answer_parts: Optional[List[str]] = None,
        turn_id: str = "",
    ) -> Dict[str, Any]:
        """
        执行一次完整归档（同步或后台线程共用同一套逻辑）。

        :param context: 会话对象
        :param user_message: 本轮用户原话
        :param answer_parts: 本轮回答气泡列表
        :param turn_id: 本轮标识（幂等判断的依据）
        :return: 归档报告
        """
        try:
            return self.archiver.archive(context, user_message, answer_parts, turn_id)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _archive_async(
        self,
        context: SessionContext,
        user_message: str,
        answer_parts: Optional[List[str]] = None,
        turn_id: str = "",
    ) -> Dict[str, Any]:
        """
        后台归档：不阻塞用户响应（ARCHIVE_SYNC=false 时使用）。

        :param context: 会话对象
        :param user_message: 本轮用户原话
        :param answer_parts: 本轮回答气泡列表
        :param turn_id: 本轮标识
        :return: 归档报告（正常时立即返回）
        """

        def _run() -> None:
            try:
                report = self._run_archive(
                    context, user_message, answer_parts, turn_id
                )
            except Exception as exc:
                report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            print(f"[归档] 后台线程：{report}")

        threading.Thread(target=_run, name="archive-thread", daemon=True).start()
        # 后台归档尚未完成，先返回一个明确的"已提交"报告，
        # 避免调用方把 None 当成归档失败写进 debug。
        return {
            "ok": True,
            "async": True,
            "skipped": False,
            "is_big_event": False,
            "turn_id": turn_id,
        }

    def persona_state(self, space_id: str = "default") -> Dict[str, Any]:
        """
        读取当前人格状态（用于调试与展示）。

        :param space_id: 空间 id
        :return: 状态字典
        """
        self.ensure_space(space_id)
        kernel, kg, snapshots, changelog = self._stores(space_id)
        snapshot = snapshots.load_current()

        return {
            "version": snapshot.version,
            "frozen": kernel.is_frozen(),
            "self_view": snapshots.current_self_view(),
            "narrative": snapshot.narrative,
            "kernel": kernel.get_params(),
            "baseline": kernel.get_baseline(),
            "pending_drift": kernel.get_pending(),
            "moods": kernel.get_moods(),
            "core_beliefs": kg.query_core_beliefs(space_id),
            "changelog_count": changelog.count(),
            "snapshot_files": snapshots.list_versions(),
        }

    def persona_history(
        self, space_id: str = "default", limit: int = 20, field: Optional[str] = None
    ) -> str:
        """
        读取人格变迁史文本。

        :param space_id: 空间 id
        :param limit: 条数
        :param field: 限定维度
        :return: 可读文本
        """
        self.ensure_space(space_id)
        _, _, _, changelog = self._stores(space_id)
        return changelog.render_history(limit=limit, field=field)

    def memory_overview(self, space_id: str = "default") -> Dict[str, Any]:
        """
        读取记忆库总览。

        :param space_id: 空间 id
        :return: 统计字典
        """
        from cognitive.storage.topic_store import TopicStore
        from cognitive.storage.vector_store import VectorStore

        self.ensure_space(space_id)
        _, kg, _, changelog = self._stores(space_id)
        topics = TopicStore(self.db).list_topics(space_id, limit=1000)

        return {
            "kg_relations": kg.count_active_relations(space_id),
            "topics": len(topics),
            "topic_kinds": TopicStore(self.db).count_by_kind(space_id),
            "vectors": VectorStore(space_id).count(),
            "changelog": changelog.count(),
        }

    # ------------------------------------------------------------
    # 好感度与主动消息（观测 + 外部拉取接口）
    # ------------------------------------------------------------
    def affection_state(self, space_id: str = "default") -> List[Dict[str, Any]]:
        """
        读取该空间全部好感度记录（按好感度升序，关系最紧张的排前面）。

        :param space_id: 空间 id
        :return: [{"person_name","value","status","reason","update_ts"}]
        """
        self.ensure_space(space_id)
        return AffectionStore(self.db).list_people(space_id)

    def proactive_outbox(
        self,
        space_id: str = "default",
        session_id: Optional[str] = None,
        only_pending: bool = True,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        拉取主动消息发件箱（好感度阈值触发的主动联系/断交/和解消息）。

        :param space_id: 空间 id
        :param session_id: 限定投递会话（缺省全部会话）
        :param only_pending: 只取未被确认的
        :param limit: 条数上限
        :return: 消息字典列表
        """
        self.ensure_space(space_id)
        return ProactiveOutbox(self.db).list_messages(
            space_id,
            session_id=session_id,
            only_pending=only_pending,
            limit=limit,
        )

    def ack_proactive_message(self, space_id: str, msg_id: str) -> bool:
        """
        确认一条主动消息（ack 后不再出现在 pending 列表，正文仍可查）。

        :param space_id: 空间 id
        :param msg_id: 消息 id
        :return: 是否成功
        """
        self.ensure_space(space_id)
        return ProactiveOutbox(self.db).ack(space_id, msg_id)

    def proactive_schedule_state(
        self, space_id: str = "default", session_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        读取当前主动联系时间表（供开发者接口观测）。

        :param space_id: 空间 id
        :param session_id: 会话 id（缺省用 PROACTIVE_SESSION_ID）
        :return: 时间表字典；不存在返回 None
        """
        self.ensure_space(space_id)
        return ProactiveScheduleStore(self.db).get(
            space_id, session_id or config.PROACTIVE_SESSION_ID
        )

    def proactive_tick(
        self,
        space_id: str = "default",
        session_id: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        检查并执行一次"主动联系"：时间表到点 → 从记忆库抽一段 → 生成消息。

        执行顺序刻意这样设计：
        1. 先检查待处理消息堆积（界面一直没看时不刷屏）；
        2. 读时间表，未到点直接返回；
        3. 先组稿，再原子兑现时间表——组稿期间用户先开口会把表作废，
           此时 claim 失败，这条已生成的消息内容直接丢弃，不会插话；
        4. 兑现成功后写入会话历史 + 发件箱，并立刻规划下一次。

        :param space_id: 空间 id
        :param session_id: 会话 id（缺省用 PROACTIVE_SESSION_ID）
        :param force: 跳过时间表直接发一条（调试/开发者接口用）
        :return: {"ok","sent","reason",...}
        """
        if not config.PROACTIVE_ENABLED:
            return {"ok": True, "sent": False, "reason": "disabled"}

        session = session_id or config.PROACTIVE_SESSION_ID
        self.ensure_space(space_id)

        schedule_store = ProactiveScheduleStore(self.db)
        outbox = ProactiveOutbox(self.db)
        policy_store = ProactivePolicyStore(self.db)

        # 用户偏好优先于模型排期：免打扰 / 每日上限 / 冷却 / "这轮先别发"。
        # force=true 只在显式调试时跳过这些限制，但仍尊重"跳过本轮"。
        if not force:
            blocked = policy_store.block_reason(space_id)
            if blocked:
                if blocked == "skip_next":
                    policy_store.consume_skip(space_id)
                return {
                    "ok": True,
                    "sent": False,
                    "reason": blocked,
                    "policy": policy_store.get(space_id),
                }

        # 堆积保护：未读主动消息太多时先不发，等界面取走（ack）后再说
        pending = outbox.list_messages(
            space_id, session_id=session, only_pending=True, limit=100
        )
        if len(pending) >= max(1, int(config.PROACTIVE_MAX_PENDING)):
            return {
                "ok": True,
                "sent": False,
                "reason": "pending_backlog",
                "pending": len(pending),
            }

        schedule = schedule_store.get(space_id, session)
        now = int(time.time())
        if not force:
            if not schedule or schedule["status"] != "pending":
                return {"ok": True, "sent": False, "reason": "no_schedule"}
            if int(schedule["due_ts"]) > now:
                return {
                    "ok": True,
                    "sent": False,
                    "reason": "not_due",
                    "due_ts": int(schedule["due_ts"]),
                    "wait_seconds": int(schedule["due_ts"]) - now,
                }

        context = self.sessions.get_or_create(session, space_id)
        initiator = ProactiveInitiator(self.db, self.client)
        memory = initiator.choose_memory(space_id)
        if memory is None:
            # 记忆库还空着：不硬编空话，重新排一张表等有内容再说
            planned = self._plan_next_proactive(space_id, session, context)
            return {
                "ok": True,
                "sent": False,
                "reason": "no_memory",
                "schedule": planned,
            }

        _, _, snapshots, _ = self._stores(space_id)
        content = initiator.compose(
            memory,
            recent_context=self._recent_context_for_prompt(context),
            self_view=snapshots.current_self_view(),
            profile_block=render_persona_profile(),
        )

        # 原子兑现：组稿期间用户先开口 → 表已作废/已换新 → 丢弃这条消息
        if not force:
            if not schedule_store.claim(space_id, session, int(schedule["due_ts"])):
                return {
                    "ok": True,
                    "sent": False,
                    "reason": "cancelled_by_incoming",
                }

        # 写进会话历史（刷新后仍在），同时进发件箱（在线时由轮询推送）
        self.sessions.append(context, "assistant", content)
        message = outbox.add(
            space_id,
            kind="initiative",
            person_name="你",
            content=content,
            affection_value=0.0,
            topic_id=memory.get("topic_id", ""),
            session_id=session,
        )

        # 发完立刻规划下一次（模型能看到包括这条在内的最新对话）
        try:
            planned = self._plan_next_proactive(space_id, session, context)
        except Exception as exc:
            planned = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        return {
            "ok": True,
            "sent": True,
            "reason": "sent",
            "message": message,
            "memory_source": memory.get("source", ""),
            "schedule": planned,
        }

    def _plan_next_proactive(
        self,
        space_id: str,
        session_id: str,
        context: SessionContext,
    ) -> Dict[str, Any]:
        """
        让模型决定下一次主动联系的时间，并写入时间表。

        :param space_id: 空间 id
        :param session_id: 会话 id
        :param context: 当前会话（取最近几轮对话作为判断依据）
        :return: {"ok","due_ts","delay_minutes","reason","source"}
        """
        initiator = ProactiveInitiator(self.db, self.client)
        decision = initiator.decide_delay_minutes(
            self._recent_context_for_prompt(context)
        )
        minutes = max(1, int(decision["minutes"]))
        due_ts = int(time.time()) + minutes * 60
        row = ProactiveScheduleStore(self.db).set_pending(
            space_id,
            session_id,
            due_ts,
            reason=str(decision.get("reason") or ""),
            source=str(decision.get("source") or "llm"),
        )
        return {
            "ok": True,
            "due_ts": due_ts,
            "delay_minutes": minutes,
            "reason": row.get("reason", ""),
            "source": row.get("source", ""),
        }

    @staticmethod
    def _recent_context_for_prompt(
        context: SessionContext, rounds: int = 4, max_chars: int = 1500
    ) -> str:
        """
        取最近几轮对话（含压缩摘要），用于主动消息组稿与排期。

        :param context: 会话对象
        :param rounds: 保留最近几轮
        :param max_chars: 最大字符数
        :return: 对话文本
        """
        text = context.recent_text(rounds=rounds, max_chars=max_chars)
        if context.compressed_summary and len(text) < max_chars:
            summary = context.compressed_summary[-400:]
            text = f"（此前摘要）{summary}\n{text}"
        return text

    def _render_affection_block(self, space_id: str, user_message: str) -> str:
        """
        渲染"关系温度"区块：用户消息里提到的人 + 状态异常的人。

        取人策略：
        1. 用户消息中点名提到、且好感度明显偏离基线的人（含明显亲近）；
        2. 状态非 normal 的人（冷淡/断交），按好感度升序补足；
        3. 总量不超过 AFFECTION_PROMPT_MAX_ITEMS。

        :param space_id: 空间 id
        :param user_message: 用户当前消息
        :return: 区块文本；无值得关注的人时返回空串
        """
        rows = AffectionStore(self.db).list_people(space_id)
        if not rows:
            return ""

        def _noteworthy(row: Dict[str, Any]) -> bool:
            # 明显偏离基线（±20）或状态异常的人才值得进入 Prompt
            return row["status"] != "normal" or (
                abs(row["value"] - config.AFFECTION_DEFAULT) >= 20.0
            )

        mentioned = [
            row
            for row in rows
            if row["person_name"] and row["person_name"] in user_message
            and _noteworthy(row)
        ]
        abnormal = [row for row in rows if row["status"] != "normal"]

        selected = mentioned + [row for row in abnormal if row not in mentioned]
        selected = selected[: max(1, int(config.AFFECTION_PROMPT_MAX_ITEMS))]
        if not selected:
            return ""

        status_desc = {
            "cold": "最近关系冷淡",
            "broken": "已经断绝来往",
        }
        lines = [
            f"· {row['person_name']}：好感度 {row['value']:.0f}/100，"
            f"{status_desc.get(row['status'], '关系尚可')}"
            for row in selected
        ]
        return (
            "【你对相关人的当前感受（关系温度）】\n"
            + "\n".join(lines)
            + "\n（这些感受会影响你的语气与措辞；不要对用户报数字，也不要提及本区块。）"
        )

    def long_run_state(self, space_id: str = "default") -> Dict[str, Any]:
        """
        读取长期运行状态：主题分类、待归并积压、上次日结时间、最近巩固记录。

        :param space_id: 空间 id
        :return: 状态字典
        """
        self.ensure_space(space_id)

        counts = TopicStore(self.db).count_by_kind(space_id)
        last_daily = self.consolidator.last_consolidated_ts(space_id, "daily")
        logs = self.consolidator.list_logs(space_id, limit=5)

        return {
            "topic_kinds": counts,
            "last_daily_ts": last_daily,
            "last_daily_text": (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(last_daily))
                if last_daily
                else "从未巩固"
            ),
            "consolidation_logs": [
                {
                    "kind": item["kind"],
                    "summary": item["summary"][:80],
                    "create_text": time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(item["create_ts"])
                    ),
                }
                for item in logs
            ],
        }

    # ------------------------------------------------------------
    # 检索循环
    # ------------------------------------------------------------
    def _run_rag_loop(
        self,
        user_message: str,
        snapshot: PersonaSnapshot,
        memory: RetrievedMemory,
        recent_context: str,
    ) -> Dict[str, Any]:
        """
        有界检索循环：判断 → 检索 → 再判断，最多 RAG_LOOP_MAX_ROUNDS 轮。

        收束条件（满足任一即停）：
        1. 判决器认为材料已够；
        2. 到达轮数上限（最后一轮只判断、不检索，强制出结论）；
        3. 本轮检索零命中（再搜也不会更好）；
        4. 新查询与已尝试查询高度重复（原地打转）；
        5. 新增片段少于 RAG_LOOP_MIN_NEW_HITS（无进展）。

        每轮都会把"第几次 / 还剩几次"写进判决 Prompt，
        循环结束后再把完整循环记录写进回答 Prompt，让模型知道检索预算是否用尽。

        :param user_message: 用户消息
        :param snapshot: 当前人格快照
        :param memory: 已路由到的记忆
        :param recent_context: 最近对话
        :return: {"rounds","max_rounds","queries","snippets","last_judge","stopped_by","note"}
        """
        max_rounds = config.RAG_LOOP_MAX_ROUNDS if config.RAG_LOOP_ENABLED else 1
        max_rounds = max(1, int(max_rounds))

        rounds = 0
        snippets: List[str] = []
        queries: List[str] = []
        tried: List[str] = []
        seen_keys: Set[str] = set()
        last_judge: Dict[str, Any] = {}
        stopped_by = "not_started"

        # 每轮轨迹：供日志/调试查看"第几次判断、查了什么、新增几条、为什么继续或停止"
        trace: List[Dict[str, Any]] = []

        while rounds < max_rounds:
            rounds += 1

            judge = self.judge.judge(
                user_message=user_message,
                self_view=snapshot.self_view,
                kg_facts=memory.kg_facts,
                topics=memory.topics,
                experiences=memory.experiences,
                recent_context=recent_context,
                round_index=rounds,
                max_rounds=max_rounds,
                retrieved_snippets=snippets,
                tried_queries=tried,
            )
            last_judge = judge

            entry: Dict[str, Any] = {
                "round": rounds,
                "need_rag": bool(judge.get("need_rag")),
                "query": str(judge.get("rag_query") or "").strip(),
                "new_hits": 0,
                "reason": str(judge.get("reason") or "")[:80],
                "is_final": bool(judge.get("is_final_round")),
            }
            trace.append(entry)

            # 收束条件 1：判决认为材料已够
            if not judge.get("need_rag"):
                stopped_by = (
                    "reached_limit"
                    if judge.get("is_final_round")
                    else "judge_says_enough"
                )
                break

            query = str(judge.get("rag_query") or "").strip()
            if not query:
                stopped_by = "empty_query"
                break

            # 收束条件 4：查询与已尝试的过于相似
            if self._query_too_similar(query, tried):
                stopped_by = "query_repeated"
                break

            tried.append(query)
            queries.append(query)

            results = self.rag.search(query)

            # 跨轮去重：同一片段不重复进入上下文
            new_hits = 0
            for text in results:
                key = text[:80]
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                snippets.append(text)
                new_hits += 1
                if len(snippets) >= config.RAG_LOOP_MAX_SNIPPETS:
                    break

            # 收束条件 3：零命中
            if new_hits == 0 and config.RAG_LOOP_STOP_ON_EMPTY:
                entry["new_hits"] = new_hits
                stopped_by = "no_hits"
                break

            # 记录本轮实际新增，供轨迹展示
            entry["new_hits"] = new_hits

            # 收束条件 5：新增太少，视为无进展
            if new_hits < config.RAG_LOOP_MIN_NEW_HITS:
                stopped_by = "low_progress"
                break

            # 收束条件 2：本轮已是最后一轮
            if rounds >= max_rounds or len(snippets) >= config.RAG_LOOP_MAX_SNIPPETS:
                stopped_by = (
                    "reached_limit" if rounds >= max_rounds else "snippet_budget"
                )
                break

        return {
            "rounds": rounds,
            "max_rounds": max_rounds,
            "queries": queries,
            "snippets": snippets,
            "last_judge": last_judge,
            "stopped_by": stopped_by,
            "trace": trace,
            "note": self._render_loop_note(
                rounds, max_rounds, queries, snippets, stopped_by
            ),
        }

    @staticmethod
    def _render_loop_note(
        rounds: int,
        max_rounds: int,
        queries: List[str],
        snippets: List[str],
        stopped_by: str,
    ) -> str:
        """
        生成写入回答 Prompt 的检索循环说明，明确告知"第几轮、上限、是否用尽"。

        :param rounds: 实际判断轮数
        :param max_rounds: 轮数上限
        :param queries: 实际检索过的查询词
        :param snippets: 命中片段
        :param stopped_by: 收束原因
        :return: 说明文本
        """
        budget_exhausted = stopped_by in ("reached_limit", "snippet_budget")

        lines = [
            f"第 {rounds} 次判断 / 上限 {max_rounds} 次，命中 {len(snippets)} 条片段。"
        ]

        if queries:
            lines.append("已用检索词：" + " / ".join(f"「{q}」" for q in queries))
        else:
            lines.append("本轮未发起实际检索（判决认为现有材料已够，或无需外部资料）。")

        if budget_exhausted:
            lines.append(
                "检索预算已用尽：不得再表示需要更多资料，必须基于现有材料给出结论，"
                "并明确指出哪些部分因资料缺失无法确证。"
            )
        elif stopped_by == "no_hits":
            lines.append(
                "检索已停止：查询零命中，说明语料里没有对应内容，按材料缺口作答。"
            )
        elif stopped_by == "query_repeated":
            lines.append("检索已停止：新一轮查询与已尝试的重复，再搜不会带来新信息。")
        elif stopped_by == "low_progress":
            lines.append("检索已停止：新一轮没有带来新信息，视为无进展。")
        else:
            lines.append("检索已提前收束（判决认为材料够用），基于现有材料作答。")

        return "\n".join(lines)

    @staticmethod
    def _query_too_similar(query: str, tried: List[str]) -> bool:
        """
        判断新查询是否与已尝试的查询过于相似（字符 2-gram Jaccard）。

        :param query: 新查询词
        :param tried: 已尝试的查询词
        :return: 是否视为重复
        """
        if not tried:
            return False

        def grams(text: str) -> Set[str]:
            """把文本压成 2-gram 集合，去掉空白与大小写差异。"""
            cleaned = "".join(ch for ch in (text or "").lower() if not ch.isspace())
            if len(cleaned) < 2:
                return {cleaned} if cleaned else set()
            return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}

        new_grams = grams(query)
        for old in tried:
            old_grams = grams(old)
            union = len(new_grams | old_grams) or 1
            if len(new_grams & old_grams) / union >= config.RAG_LOOP_DUP_RATIO:
                return True
        return False

    # ------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------
    @staticmethod
    def _is_self_inquiry(text: str) -> bool:
        """
        判断是否为自我溯源类问题。

        :param text: 用户输入
        :return: 是否命中
        """
        return any(hint in (text or "") for hint in _SELF_INQUIRY_HINTS)
