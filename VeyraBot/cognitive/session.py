"""
短期工作记忆（SessionContext）管理。

两个动作要严格区分（本模块只负责第一个）：
1. 兜底压缩：token 逼近安全阈值时，把旧历史摘要化，只为让本轮推理能跑通
   —— 这是物理约束下的应急保护，不产生长期记忆
2. 主动归档：每轮结束后的认知沉淀，由 archiver 负责，与 token 无关

双写策略：每次变更立即写内存 + 立即落库（会话崩溃后可完整恢复）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

import config
from cognitive.llm import DeepSeekClient, LLMError, get_client
from cognitive.models import Message, SessionContext
from cognitive.storage.database import Database
from cognitive.tokenizer import estimate_messages_tokens


# 压缩用的系统提示：只保留可执行信息，禁止编造
_SUMMARY_SYSTEM_PROMPT = (
    "你是对话历史压缩器。请把给出的多轮对话压缩成一段简短摘要，"
    "必须保留：用户的目标、已确认的方案与结论、明确的约束与参数、尚未解决的问题。"
    "删除寒暄、重复表述、失败尝试的细节。"
    "严禁编造对话中不存在的信息。输出纯文本摘要，不要标题、不要序号、不要 Markdown。"
)


class SessionManager:
    """会话短期记忆的加载、追加、压缩与持久化。"""

    def __init__(self, db: Database, client: Optional[DeepSeekClient] = None) -> None:
        """
        :param db: 数据库访问对象
        :param client: 模型客户端（压缩摘要时使用），缺省走全局单例
        """
        self.db = db
        self.client = client
        self._memory: Dict[str, SessionContext] = {}
        self._last_cleanup = 0  # 上次清理时间戳
        self._cleanup_interval = 3600  # 清理间隔（秒）

    def _get_client(self) -> DeepSeekClient:
        """惰性获取模型客户端，避免构造阶段就失败。"""
        if self.client is None:
            self.client = get_client()
        return self.client

    def _cleanup_expired_sessions(self) -> None:
        """清理不活跃的会话，防止内存泄漏。"""
        import time

        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return

        # 清理超过24小时未活跃的会话
        expired_sessions = [
            session_id
            for session_id, context in self._memory.items()
            if now - context.update_ts > 86400  # 24小时
        ]

        for session_id in expired_sessions:
            del self._memory[session_id]

        self._last_cleanup = now

    # ------------------------------------------------------------
    # 加载 / 持久化
    # ------------------------------------------------------------
    def get_or_create(self, session_id: str, space_id: str) -> SessionContext:
        """
        取会话：内存优先 → 数据库 → 新建。

        :param session_id: 会话 id
        :param space_id: 记忆空间 id
        :return: 会话对象
        """
        # 定期清理不活跃的会话
        self._cleanup_expired_sessions()

        # 1. 内存命中
        if session_id in self._memory:
            return self._memory[session_id]

        # 2. 数据库命中
        connection = self.db.conn()
        row = connection.execute(
            """
            select s.payload as payload
            from session_store as s
            where s.session_id = ?
            """,
            (session_id,),
        ).fetchone()

        if row and row["payload"]:
            try:
                context = SessionContext.from_dict(json.loads(row["payload"]))
            except (json.JSONDecodeError, KeyError, TypeError):
                # 数据损坏则重建空会话，不阻塞对话
                context = None

            if context is not None:
                # 会话与记忆空间是绑定的：space 不一致说明这是另一个"身份"的会话，
                # 沿用它会让本轮读写落到旧空间（表现为"刚写入却检索不到"），必须重建。
                if context.space_id != space_id:
                    context = SessionContext(session_id=session_id, space_id=space_id)
                    self.save(context)
                self._memory[session_id] = context
                return context

        # 3. 新建
        context = SessionContext(session_id=session_id, space_id=space_id)
        self._memory[session_id] = context
        self.save(context)
        return context

    def save(self, context: SessionContext) -> None:
        """
        双写：更新内存 + 立刻落库。

        :param context: 会话对象
        """
        self._memory[context.session_id] = context
        context.update_ts = int(time.time())

        connection = self.db.conn()
        connection.execute(
            """
            insert into session_store (session_id, space_id, payload, update_ts)
            values (?, ?, ?, ?)
            on conflict (session_id) do update set
                space_id = excluded.space_id,
                payload = excluded.payload,
                update_ts = excluded.update_ts
            """,
            (
                context.session_id,
                context.space_id,
                json.dumps(context.to_dict(), ensure_ascii=False),
                context.update_ts,
            ),
        )
        connection.commit()

    def append(self, context: SessionContext, role: str, content: str) -> None:
        """
        追加一条消息并立即持久化。

        :param context: 会话对象
        :param role: 角色
        :param content: 正文
        """
        context.append(role, content)
        self.save(context)

    # ------------------------------------------------------------
    # token 统计
    # ------------------------------------------------------------
    def count_tokens(self, context: SessionContext) -> int:
        """
        统计当前会话的总 token（含历史摘要的开销）。

        :param context: 会话对象
        :return: token 估算值
        """
        total = estimate_messages_tokens(context.messages)
        if context.compressed_summary:
            total += estimate_messages_tokens([], extra_text=context.compressed_summary)
        return total

    # ------------------------------------------------------------
    # 兜底压缩
    # ------------------------------------------------------------
    def maybe_compress(self, context: SessionContext) -> Dict[str, object]:
        """
        判断并按需执行兜底压缩。

        :param context: 会话对象
        :return: 压缩报告 {"triggered": bool, "before": int, "after": int, "passes": int}
        """
        before = self.count_tokens(context)
        report: Dict[str, object] = {
            "triggered": False,
            "before": before,
            "after": before,
            "passes": 0,
        }

        if before <= config.MAX_SAFE_TOKENS:
            return report

        report["triggered"] = True
        passes = 0

        # 最多连续压缩若干次，防止无限循环
        while passes < config.MAX_COMPRESS_PASSES:
            passes += 1
            try:
                compressed = self._compress_once(context)
                if not compressed:
                    break
            except Exception:
                # 压缩失败，尝试硬截断
                dropped = self._hard_truncate(context)
                if dropped == 0:
                    break

            current = self.count_tokens(context)

            if current <= config.MAX_SAFE_TOKENS:
                break

            # 已经没有可压缩的旧历史（只剩保留轮次）时，走滑动窗口硬兜底
            keep_count = config.KEEP_RECENT_ROUNDS * 2
            if len(context.messages) <= keep_count:
                dropped = self._hard_truncate(context)
                if dropped == 0:
                    break

        report["passes"] = passes
        report["after"] = self.count_tokens(context)
        self.save(context)
        return report

    def _compress_once(self, context: SessionContext) -> bool:
        """
        执行一次摘要式压缩。

        切分规则：
        - system 消息永不压缩
        - 最近 KEEP_RECENT_ROUNDS 轮原样保留
        - 更早的全部内容（含上一次的摘要）合并后重新摘要

        :param context: 会话对象
        :return: 是否真的发生了压缩
        """
        keep_count = config.KEEP_RECENT_ROUNDS * 2
        messages = context.messages

        # 消息不足，无法压缩
        if len(messages) <= keep_count:
            return False

        to_compress = messages[:-keep_count]
        keep_part = messages[-keep_count:]

        # 拼接待压缩文本（system 消息也参与摘要，但压缩后不再保留原文）
        parts: List[str] = []
        if context.compressed_summary:
            parts.append(f"【此前摘要】{context.compressed_summary}")
        for msg in to_compress:
            if msg.role == "system":
                parts.append(f"【初始设定】{msg.content}")
            else:
                parts.append(f"{msg.role}: {msg.content}")

        dialogue_text = "\n".join(parts)

        try:
            summary = self._get_client().chat(
                messages=[
                    {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": dialogue_text},
                ],
                temperature=0.2,
                max_tokens=config.SUMMARY_MAX_TOKENS,
            )
            summary = summary.strip()
        except LLMError:
            # 摘要失败：降级为机械摘要，保证流程不中断（信息会有损，但可追溯）
            summary = (
                "【压缩摘要（模型摘要失败，以下为原始摘录）】\n" + dialogue_text[-1500:]
            )

        context.compressed_summary = summary
        context.messages = keep_part
        return True

    def _hard_truncate(self, context: SessionContext) -> int:
        """
        极端兜底：丢弃最老的一轮对话，直到低于阈值或只剩一轮。

        :param context: 会话对象
        :return: 丢弃的消息条数
        """
        dropped = 0
        while (
            self.count_tokens(context) > config.MAX_SAFE_TOKENS
            and len(context.messages) > 2
        ):
            # 检查是否有可删除的有效消息
            if all(not msg.content for msg in context.messages[:-2]):
                break
            # 一次丢弃一对（一轮）
            del context.messages[0:2]
            dropped += 2
        return dropped

    # ------------------------------------------------------------
    # 调试辅助
    # ------------------------------------------------------------
    def snapshot_debug(self, context: SessionContext) -> str:
        """
        输出会话状态摘要，用于终端观测。

        :param context: 会话对象
        :return: 可读字符串
        """
        return (
            f"session={context.session_id} space={context.space_id} "
            f"messages={len(context.messages)} tokens={self.count_tokens(context)} "
            f"has_summary={bool(context.compressed_summary)}"
        )
