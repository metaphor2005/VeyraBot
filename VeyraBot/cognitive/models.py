"""
数据模型层（全部使用标准库 dataclass，避免额外依赖）。

分层说明：
- Message / SessionContext  短期工作记忆
- Topic / Experience        主题索引记忆
- KGEntity / KGRelation     结构化信念记忆（含人格信念权重）
- PersonaSnapshot / PersonaChange  顶层叙事（人格快照 + 变迁日志）
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


def _now() -> int:
    """返回当前 Unix 时间戳（秒）。"""
    return int(time.time())


def new_id(prefix: str) -> str:
    """
    生成带前缀的短唯一标识。

    :param prefix: 标识前缀，如 topic / exp / plog
    :return: 形如 topic_3f2a1b9c 的字符串
    """
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ============================================================
# 短期工作记忆
# ============================================================
@dataclass
class Message:
    """单条对话消息。"""

    role: str                        # user / assistant / system
    content: str = ""                # 消息正文
    ts: int = field(default_factory=_now)   # 记录时间

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典，用于持久化。"""
        return {"role": self.role, "content": self.content, "ts": self.ts}

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Message":
        """从字典还原消息，缺失字段使用安全默认值。"""
        return Message(
            role=data.get("role", ""),
            content=data.get("content", ""),
            ts=int(data.get("ts", _now())),
        )


@dataclass
class SessionContext:
    """
    一段会话的短期工作记忆。

    说明：
    - messages 只保存未被压缩的近期对话；
    - compressed_summary 保存兜底压缩产生的历史摘要（不进入 messages，避免二次压缩时被再次摘要）；
    - 该对象每次变更都同步写入 SQLite，实现内存 + 持久双写。
    """

    session_id: str
    space_id: str
    messages: List[Message] = field(default_factory=list)
    compressed_summary: str = ""
    create_ts: int = field(default_factory=_now)
    update_ts: int = field(default_factory=_now)

    def append(self, role: str, content: str) -> Message:
        """
        追加一条消息并刷新更新时间。

        :param role: 角色
        :param content: 正文
        :return: 新建的消息对象
        """
        msg = Message(role=role, content=content)
        self.messages.append(msg)
        self.update_ts = _now()
        return msg

    def recent_text(self, rounds: Optional[int] = None, max_chars: int = 2000) -> str:
        """
        取出最近的对话文本，用于判决、归档等环节。

        :param rounds: 只取最近 N 轮（None 表示全部）
        :param max_chars: 截断长度，防止注入过长
        :return: 拼接后的文本
        """
        msgs = self.messages
        if rounds is not None and rounds > 0:
            msgs = msgs[-rounds * 2:]

        lines = [f"{m.role}: {m.content}" for m in msgs if m.content]
        text = "\n".join(lines)
        return text[-max_chars:] if len(text) > max_chars else text

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {
            "session_id": self.session_id,
            "space_id": self.space_id,
            "messages": [m.to_dict() for m in self.messages],
            "compressed_summary": self.compressed_summary,
            "create_ts": self.create_ts,
            "update_ts": self.update_ts,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "SessionContext":
        """从字典还原会话。"""
        return SessionContext(
            session_id=data.get("session_id", ""),
            space_id=data.get("space_id", ""),
            messages=[Message.from_dict(m) for m in data.get("messages", [])],
            compressed_summary=data.get("compressed_summary", ""),
            create_ts=int(data.get("create_ts", _now())),
            update_ts=int(data.get("update_ts", _now())),
        )


# ============================================================
# 主题索引记忆
# ============================================================
@dataclass
class Topic:
    """
    一件"往事"的主记录（主题索引记忆的核心单元）。

    一次完整交互归为一个 topic，后续经验片段、KG 关系都挂在该 topic_id 下，
    召回时以 topic 为单位成套取回，避免碎片化。
    """

    topic_id: str
    space_id: str
    title: str
    abstract: str
    keywords: List[str] = field(default_factory=list)
    session_id: str = ""
    snapshot_version: int = 0
    kind: str = "normal"          # normal（普通事件）/ daily（日结）/ event（大事沉淀）
    consolidated: bool = False    # 是否已被上一层（日结）归并
    is_big_event: bool = False    # 是否被判定为大事
    create_ts: int = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return asdict(self)


@dataclass
class Experience:
    """挂在某个主题下的松散经验片段（走向量检索）。"""

    exp_id: str
    space_id: str
    topic_id: str
    content: str
    create_ts: int = field(default_factory=_now)


# ============================================================
# 结构化信念记忆（KG）
# ============================================================
@dataclass
class KGEntity:
    """知识图谱实体。"""

    eid: str
    etype: str                        # User / Project / Component / Belief / AgentSelf ...
    name: str
    space_id: str
    attrs: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = True
    create_ts: int = field(default_factory=_now)


@dataclass
class KGRelation:
    """
    知识图谱关系（带权重，是人格演化的主要载体之一）。

    - confidence：对该信念的置信度
    - importance：该信念对"自我"的重要程度（核心自我信念接近 1）
    - multi_valued：该关系维度是否允许多个取值并存。
      由归档抽取时模型按常识判断（"室友"可以有好几个，"生日"只能有一个）。
      多值维度出现新取值时与旧值共存；单值维度仍然"新值推翻旧值"。
    """

    rid: str
    space_id: str
    head_name: str
    rel: str
    tail_name: str
    confidence: float = 0.7
    importance: float = 0.5
    multi_valued: bool = False
    is_active: bool = True
    source_topic_id: str = ""
    create_ts: int = field(default_factory=_now)
    update_ts: int = field(default_factory=_now)


# ============================================================
# 顶层叙事：人格快照 + 变迁日志
# ============================================================
@dataclass
class PersonaSnapshot:
    """
    人格快照（一个版本的完整自我状态，独立成文件，只读）。

    这是"顶层叙事"，是内核变化的文字镜像，不参与逻辑计算。
    """

    version: int
    space_id: str
    self_view: str
    narrative: str                       # 面向用户的自我叙事段落
    kernel: Dict[str, float]             # 该版本的人格参数快照
    moods: Dict[str, float]              # 该版本处于活跃状态的情景倾向
    core_beliefs: List[Dict[str, Any]]   # 该版本的核心自我信念（name + confidence + importance）
    topic_id: str = ""
    baseline: Dict[str, float] = field(default_factory=dict)  # 该版本的人格基调
    create_ts: int = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典（写入快照文件）。"""
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "PersonaSnapshot":
        """从字典还原快照。"""
        return PersonaSnapshot(
            version=int(data.get("version", 0)),
            space_id=data.get("space_id", ""),
            self_view=data.get("self_view", ""),
            narrative=data.get("narrative", ""),
            kernel=dict(data.get("kernel", {})),
            moods=dict(data.get("moods", {})),
            core_beliefs=list(data.get("core_beliefs", [])),
            topic_id=data.get("topic_id", ""),
            baseline=dict(data.get("baseline", {})),
            create_ts=int(data.get("create_ts", _now())),
        )


@dataclass
class PersonaChange:
    """
    人格变迁日志（append-only，永不修改、永不删除）。

    一条记录回答："因为哪件事、哪个维度、从什么变成什么、为什么变"。
    """

    log_id: str
    space_id: str
    topic_id: str
    persona_field: str                   # 变更维度：kernel:{param} / self_view / mood:{mode} / belief:{name}
    old_value: str
    new_value: str
    reason: str                          # 由模型基于本轮对话归纳的因果理由
    source_type: str = "dialogue"        # dialogue / self_reflection / user_directive
    snapshot_from: int = 0               # 变更前快照版本
    snapshot_to: int = 0                 # 变更后快照版本
    create_ts: int = field(default_factory=_now)


# ============================================================
# 检索结果聚合
# ============================================================
@dataclass
class RetrievedMemory:
    """
    一次记忆路由的汇总结果，直接用于 Prompt 组装。

    :param kg_facts: 结构化信念文本行
    :param overturned: 已被推翻的旧信念（原值），用于让 AI 知道自己改过主意
    :param topics: 命中的主题
    :param experiences: 命中主题下挂载的经验片段
    :param rag_snippets: RAG 兜底检索结果
    :param rag_used: 本轮是否真的调用了 RAG
    """

    kg_facts: List[str] = field(default_factory=list)
    overturned: List[Dict[str, Any]] = field(default_factory=list)
    topics: List[Topic] = field(default_factory=list)
    experiences: List[str] = field(default_factory=list)
    rag_snippets: List[str] = field(default_factory=list)
    rag_used: bool = False

    def to_prompt_block(self) -> str:
        """
        渲染为注入 Prompt 的记忆区块文本。
        """
        blocks: List[str] = []

        if self.kg_facts:
            blocks.append("【结构化信念（知识图谱）】\n" + "\n".join(f"- {line}" for line in self.kg_facts))

        # 「曾被推翻」必须单独一块：否则 AI 只看到当前值，感知不到自己改过主意
        if self.overturned:
            lines: List[str] = []
            for item in self.overturned:
                head = item.get("head", "")
                rel = item.get("rel", "")
                old = item.get("old", "")
                new = item.get("new", "")
                ts = int(item.get("ts") or 0)
                when = ""
                if ts:
                    when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                if new:
                    tail = f"{old}（原值，{when or '近期'}被替换为「{new}」）"
                else:
                    tail = f"{old}（原值，{when or '近期'}已失效，暂无替代值）"
                lines.append(f"- {head} {rel}：{tail}")
            blocks.append(
                "【曾被推翻的信念（原值，仅作对照，不要当作当前结论）】\n" + "\n".join(lines)
            )

        if self.topics:
            topic_lines = []
            for topic in self.topics:
                topic_lines.append(f"- [{topic.title}] {topic.abstract}")
            blocks.append("【相关往事（主题记忆）】\n" + "\n".join(topic_lines))

        if self.experiences:
            blocks.append("【经验片段】\n" + "\n".join(f"- {line}" for line in self.experiences))

        if self.rag_snippets:
            blocks.append("【外部资料（RAG）】\n" + "\n".join(f"- {line}" for line in self.rag_snippets))

        return "\n\n".join(blocks) if blocks else "（本轮没有召回到任何记忆）"
