"""
全局配置中心。

约定：所有可调参数必须前置到这个文件，业务代码中禁止散落魔法数字。
优先级：系统环境变量 / .env > 本文件默认值。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Tuple

from cognitive.env import load_dotenv, get, get_int, get_float
from cognitive.persona.assets import load_persona_data

# 启动时加载 .env（在读取任何参数之前完成，保证默认值被覆盖）
load_dotenv()

# ============================================================
# 一、路径配置
# ============================================================
PROJECT_ROOT: Path = Path(__file__).resolve().parent

# 运行数据根目录
DATA_DIR: Path = PROJECT_ROOT / "data"

# SQLite 记忆库文件（KG、主题、经验、人格、会话共用同一库，不同表隔离）
# 可用环境变量 DB_PATH 覆盖：多实例部署 / 隔离测试库时不需要改代码
DB_PATH: Path = Path(get("DB_PATH", str(DATA_DIR / "memory.db")))

# 向量索引持久化目录（每个记忆空间一个 json 文件）
VECTOR_DIR: Path = Path(get("VECTOR_DIR", str(DATA_DIR / "vectors")))

# 人格快照文件目录（每个记忆空间一个子目录，每次实质变更生成一个独立文件）
SNAPSHOT_DIR: Path = DATA_DIR / "persona_snapshots"

# 人格原始数据目录（身份档案 / 基线参数 / 前史 / 头像）。
# 与运行时代码分离：升级代码时保留 data/persona/ 即可带走"她本来是谁"。
PERSONA_DATA_DIR: Path = Path(
    get("PERSONA_DATA_DIR", str(DATA_DIR / "persona"))
)

# 运行日志目录
LOG_DIR: Path = DATA_DIR / "logs"

# RAG 本地语料目录（无外部搜索时的兜底资料库，放 .md / .txt）
# 也归入 data/：机器人本来的资料与运行时数据放在同一个根目录下
RAG_CORPUS_DIR: Path = DATA_DIR / "corpus"

# ============================================================
# 二、模型配置（DeepSeek）
# ============================================================
# 密钥：优先环境变量 / .env，缺省时使用 .env 内的值
DEEPSEEK_API_KEY: str = get("DEEPSEEK_API_KEY", "")

# 接口地址（OpenAI 兼容）
DEEPSEEK_BASE_URL: str = get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# 主模型名称
MODEL_NAME: str = get("MODEL_NAME", "deepseek-flash")

# 运行模式：api 走真实接口；mock 走本地模拟（离线调试用）
LLM_MODE: str = get("LLM_MODE", "api").strip().lower()

# 生成回答的温度（需要思辨与表达，稍高）
ANSWER_TEMPERATURE: float = get_float("ANSWER_TEMPERATURE", 0.7)

# 生成回答的最大输出 token（deepseek-flash 含 reasoning 阶段，需留足空间）
ANSWER_MAX_TOKENS: int = get_int("ANSWER_MAX_TOKENS", 2600)

# 回答因长度被截断时，是否放大预算重试（避免回答被腰斩，代价是可能重复计费）
ANSWER_RETRY_ON_TRUNCATE: bool = (
    get("ANSWER_RETRY_ON_TRUNCATE", "true").strip().lower() == "true"
)

# 结构化抽取的温度（要求稳定 JSON，必须低温）
EXTRACT_TEMPERATURE: float = get_float("EXTRACT_TEMPERATURE", 0.2)

# 结构化抽取的最大输出 token（抽取 + reasoning 需要更多余量）
EXTRACT_MAX_TOKENS: int = get_int("EXTRACT_MAX_TOKENS", 2600)

# 判决环节的温度（判决要求确定性）
JUDGE_TEMPERATURE: float = get_float("JUDGE_TEMPERATURE", 0.1)

# 判决环节的最大输出 token
JUDGE_MAX_TOKENS: int = get_int("JUDGE_MAX_TOKENS", 800)

# HTTP 超时（秒）
LLM_TIMEOUT: int = get_int("LLM_TIMEOUT", 120)

# 失败重试次数（不含首次）；长推理模型容易被网络中断，默认给 4 次
LLM_MAX_RETRY: int = get_int("LLM_MAX_RETRY", 4)

# 重试退避基数（秒），第 n 次重试等待 BASE * 2^n
LLM_RETRY_BACKOFF: float = get_float("LLM_RETRY_BACKOFF", 2.0)

# ============================================================
# 三、会话（短期工作记忆）
# ============================================================
# 安全阈值：估算 token 超过该值时触发兜底压缩（不是模型硬上限）
# 50 轮原文按每轮 2 条、平均 120 token 估算约 12000 token，留足余量取 30000
MAX_SAFE_TOKENS: int = get_int("MAX_SAFE_TOKENS", 30000)

# 压缩时保留的最近完整轮数（一轮 = user + assistant）
# 50 轮意味着：50 轮以内永不触发压缩，超出才把更早的部分摘要化
KEEP_RECENT_ROUNDS: int = get_int("KEEP_RECENT_ROUNDS", 50)

# 每轮实际注入回答 Prompt 的历史轮数（可小于 KEEP_RECENT_ROUNDS，控制成本）
HISTORY_INJECT_ROUNDS: int = get_int("HISTORY_INJECT_ROUNDS", 50)

# 历史摘要的最大输出 token
SUMMARY_MAX_TOKENS: int = get_int("SUMMARY_MAX_TOKENS", 500)

# 压缩后仍超阈值时，最多连续压缩的次数（防止死循环）
MAX_COMPRESS_PASSES: int = get_int("MAX_COMPRESS_PASSES", 2)

# 会话持久化表名（单一库内隔离）
SESSION_TABLE: str = "session_store"

# ============================================================
# 四、记忆检索配置
# ============================================================
# 主题记忆召回条数上限
TOPIC_TOP_K: int = get_int("TOPIC_TOP_K", 3)

# 主题召回的最低余弦相似度
TOPIC_MIN_SCORE: float = get_float("TOPIC_MIN_SCORE", 0.12)

# KG 事实查询返回的最大关系条数
KG_MAX_ROWS: int = get_int("KG_MAX_ROWS", 15)

# 向量切词：中文使用字符 n-gram 的 n 值
VECTOR_NGRAM: int = get_int("VECTOR_NGRAM", 2)

# 查询扩展：用同义词表补"换种说法"的召回（替代本地 embedding 模型，见 storage/synonyms.py）
# 扩展词在查询向量里的权重：调高会把不相干的记忆也拉进来，调低则等于没加
VECTOR_EXPAND_WEIGHT: float = get_float("VECTOR_EXPAND_WEIGHT", 0.55)

# 关键词直接命中加成：查询里出现该主题的关键词时，相似度额外加这么多分
VECTOR_KEYWORD_BOOST: float = get_float("VECTOR_KEYWORD_BOOST", 0.06)

# 是否启用查询扩展（false 即回到纯字面 TF-IDF，便于对比效果）
VECTOR_EXPAND_ENABLED: bool = (
    get("VECTOR_EXPAND_ENABLED", "true").strip().lower() == "true"
)

# ============================================================
# 四之二、RAG 检索循环（有界多轮检索）
# ============================================================
# 是否启用多轮检索循环；关闭后退化为「判决一次、检索一次」
RAG_LOOP_ENABLED: bool = get("RAG_LOOP_ENABLED", "true").strip().lower() == "true"

# 单轮对话内最多进行几次「判断 → 检索」，含最后一次判断
# 到达上限的那一轮只做判断、不再检索（"最后一次必须出结论"）
RAG_LOOP_MAX_ROUNDS: int = get_int("RAG_LOOP_MAX_ROUNDS", 3)

# 某一轮检索零命中时立即收束，不再消耗后续轮次
RAG_LOOP_STOP_ON_EMPTY: bool = (
    get("RAG_LOOP_STOP_ON_EMPTY", "true").strip().lower() == "true"
)

# 新一轮查询词与已尝试查询的相似度超过该值 → 判定为原地打转，停止循环
# 实测：同义改写约 0.58~0.74，换角度检索约 0.23，故取 0.65
RAG_LOOP_DUP_RATIO: float = get_float("RAG_LOOP_DUP_RATIO", 0.65)

# 单轮检索至少新增几条片段才算"有进展"，否则提前收束
RAG_LOOP_MIN_NEW_HITS: int = get_int("RAG_LOOP_MIN_NEW_HITS", 1)

# 累积检索片段的总条数上限（避免多轮叠加撑爆上下文）
RAG_LOOP_MAX_SNIPPETS: int = get_int("RAG_LOOP_MAX_SNIPPETS", 6)

# ============================================================
# 四之三、长期运行节奏（日结 / 大事 / 跃迁）
# ============================================================
# 每天固定一次的巩固任务是否启用（由 maintenance.py --daily 驱动）
DAILY_CONSOLIDATE_ENABLED: bool = (
    get("DAILY_CONSOLIDATE_ENABLED", "true").strip().lower() == "true"
)

# 日结建议执行时刻（文档用途；实际由计划任务决定，脚本不依赖它）
DAILY_CONSOLIDATE_HOUR: int = get_int("DAILY_CONSOLIDATE_HOUR", 23)

# 日结时至少积累多少个未归并主题才值得跑（太少不值一次模型调用）
CONSOLIDATE_MIN_TOPICS: int = get_int("CONSOLIDATE_MIN_TOPICS", 2)

# 单次日结最多处理多少个主题（防止一次吞太多，超出留到下次）
CONSOLIDATE_MAX_TOPICS: int = get_int("CONSOLIDATE_MAX_TOPICS", 40)

# 日结正文最大字符数（写入 topic.abstract）
CONSOLIDATE_SUMMARY_MAX_CHARS: int = get_int("CONSOLIDATE_SUMMARY_MAX_CHARS", 400)

# 大事阈值：归档中任一关系的 importance 达到该值，才可能判定为"大事"
# 0.9：身份 / 价值观 / 长期约定一般在 0.9~0.95，纯技术问答很少到这个区间
BIG_EVENT_IMPORTANCE: float = get_float("BIG_EVENT_IMPORTANCE", 0.90)

# 是否要求"涉及自我"才按重要性判大事
# true  → importance 达标 **且** 关系头/尾是人格本身（江妍），才落事件记忆
#         （实测旧规则 0.85 纯阈值会把"帮我查资料"这类技术问答判成大事，6 轮里误判 3~4 次）
# false → 回到纯阈值判定，容易误判，仅在需要"任何高价值信息都单独沉淀"时使用
BIG_EVENT_REQUIRE_SELF: bool = (
    get("BIG_EVENT_REQUIRE_SELF", "true").strip().lower() == "true"
)

# 大事是否立即触发一次事件级巩固（与日结并行，不等日结）
BIG_EVENT_TRIGGER: bool = get("BIG_EVENT_TRIGGER", "true").strip().lower() == "true"

# 大事触发词：用户消息命中即判定为"大事"，交由事件级巩固处理
BIG_EVENT_HINTS: tuple = (
    "从今天起",
    "以后都",
    "以后只",
    "定下来",
    "最终决定",
    "重大",
    "重要决定",
    "记住这条",
    "这是铁律",
)

# 快照文件冷存：只保留最近 N 个版本在当前目录，更早的移到 archive/ 子目录
SNAPSHOT_RETAIN_VERSIONS: int = get_int("SNAPSHOT_RETAIN_VERSIONS", 30)

# 日结后的原主题是否标记为已归并（标记后不再参与主题召回，避免重复）
CONSOLIDATE_MARK_CONSUMED: bool = (
    get("CONSOLIDATE_MARK_CONSUMED", "true").strip().lower() == "true"
)

# 巩固任务是否调用模型（false 时退化为机械摘要，便于离线跑通与测试）
DAILY_USE_MODEL: bool = get("DAILY_USE_MODEL", "true").strip().lower() == "true"

# 巩固任务的最大输出 token（输出是 JSON，需留足）
CONSOLIDATE_MAX_TOKENS: int = get_int("CONSOLIDATE_MAX_TOKENS", 1200)

# ============================================================
# 五、人格内核配置
# ============================================================
# ------------------------------------------------------------
# 人格设定（基调 = "这个人是谁"，不随对话改写；后续演化都在这个基调上发生）
#
# 结构说明：分组 + 条目。渲染时每组一个小标题、组内一条一行，
# 这样 Prompt 里读起来像「一份人物档案」，而不是一堆并列标签。
# ------------------------------------------------------------
# 人格原始数据统一从 data/persona/ 读取（不再是写死在 config.py 里的字面量）。
# 这样升级代码、迁移仓库或替换人格时，只需要处理 data/persona/ 一个目录。
PERSONA_ASSETS: Dict[str, Any] = load_persona_data(PERSONA_DATA_DIR)
PERSONA_NAME: str = str(PERSONA_ASSETS["meta"]["name"])
PERSONA_PROFILE: Dict[str, Any] = PERSONA_ASSETS["profile"]
PERSONA_KERNEL_INIT: Dict[str, float] = PERSONA_ASSETS["kernel"]["init"]
PERSONA_AVATAR_FILE: Path = PERSONA_ASSETS["avatar"]

# 前史（实体 / 关系 / 成长往事 / 人物档案主题）也来自 data/persona/seed.json，
# 由 cognitive/persona/seed.py 负责写入存储层。
PERSONA_SEED: Dict[str, Any] = PERSONA_ASSETS["seed"]

# 人格参数的中文释义，注入抽取 Prompt，约束模型只能使用这些键（同样来自 kernel.json）
PERSONA_PARAM_DESC: Dict[str, str] = PERSONA_ASSETS["kernel"]["description"]

# 参数取值范围（写入时强制裁剪，避免被模型推到极端）
PERSONA_PARAM_MIN: float = 0.0
PERSONA_PARAM_MAX: float = 1.0

# 判定"实质性人格跃迁"的累计变化阈值：相对上一版快照的累计变化 >= 该值才生成新版本
# 注意：这是"多天累积"的判定，不是单轮判定（单轮已不可能超过日结步长）
PERSONA_SUBSTANTIVE_DELTA: float = 0.06

# ============================================================
# 人格基调与演化节奏（基调 + 日内摆动，日结推进基调）
# ============================================================
# 人格基调：current 只能在 base ± 该值内摆动。基调本身不每轮移动，
# 所以人格不会因为一两轮对话就跑偏——"基调"就是这个人的稳定内核。
PERSONA_DRIFT_LIMIT: float = 0.15

# 日结结算时，当天累积偏移折算成基调位移的学习率（0.35 表示只采纳约三分之一的净偏移）
PERSONA_DAILY_LEARNING_RATE: float = 0.35

# 当日累积偏移（pending）的绝对值上限。pending 记的是"未夹制的原始力度"，
# 极端对话里一天被同向顶几十次也不该无限累加，这里兜一个物理上限，
# 真正的节奏控制仍由 PERSONA_DAILY_MAX_STEP 负责。
PERSONA_PENDING_CAP: float = 3.0

# 单参数单日基调位移上限。0.01 意味着同一方向要连续积累约 6 天才够一次实质跃迁，
# 这样"一天一变"的节奏与日结同步，同时以年为单位时整体演变仍然可见。
PERSONA_DAILY_MAX_STEP: float = 0.01

# 是否启用日结结算（false 时基调永不移动，只保留日内摆动）
PERSONA_DAILY_SETTLE_ENABLED: bool = (
    get("PERSONA_DAILY_SETTLE_ENABLED", "true").strip().lower() == "true"
)

# 一次性事件（大事）允许多推进几倍日结步长（当天就有明确因果，允许快一点）
PERSONA_EVENT_STEP_MULTIPLIER: float = 2.0

# 记忆区块是否展示"曾被推翻"的旧信念（让 AI 知道自己改过主意）
SHOW_OVERTURNED_BELIEFS: bool = (
    get("SHOW_OVERTURNED_BELIEFS", "true").strip().lower() == "true"
)

# 一次最多展示几条被推翻的信念（避免污染记忆区块）
OVERTURNED_MAX_ITEMS: int = 3

# ============================================================
# 八、好感度与主动消息（社会情感的数值化）
# ============================================================
# 好感度范围 0~100，默认 50（无感基线）。
# 数值由归档抽取的 affection_deltas 驱动（AI 基于本轮对话的人际互动给出增量）。
AFFECTION_DEFAULT: float = 50.0

# 单轮好感度变化上限：一轮对话再糟糕也不能把好感打穿，
# 断交必须是多次负面累积的结果，不是一句话的事
AFFECTION_MAX_DELTA_PER_TURN: float = 15.0

# 好感度低于该值 → 进入 cold（冷淡）：主动发消息试探/表达不满
AFFECTION_COLD_THRESHOLD: float = 30.0

# 好感度低于该值 → 断绝关系：发出断交消息，此后不再主动联系这个人
AFFECTION_SEVER_THRESHOLD: float = 12.0

# 断交后好感度回升到该值以上 → 和解：发出和解消息，恢复正常来往。
# 和解线比冷淡线高：和好要跨过更高的门槛，不是回到"不冷"就自动翻篇
AFFECTION_RECONCILE_THRESHOLD: float = 60.0

# 回答 Prompt 的"关系温度"区块最多注入几个人（防止挤占上下文）
AFFECTION_PROMPT_MAX_ITEMS: int = 5

# 是否启用好感度系统（关闭后抽取结果中的 affection_deltas 被丢弃）
AFFECTION_ENABLED: bool = (
    get("AFFECTION_ENABLED", "true").strip().lower() == "true"
)

# ============================================================
# 八b、主动发起对话（她决定下次什么时候来找你）
# ============================================================
# 与"好感度阈值触发的主动消息"不同：这里是日常的"突然想起一件事，来找你说说话"。
# 调度权交给模型：每轮回答结束后，由模型结合刚才的对话决定下一次主动联系的时间；
# 到点前如果用户先开口，这张时间表作废并重新生成（一次时间表只能兑现一次）。
PROACTIVE_ENABLED: bool = (
    get("PROACTIVE_ENABLED", "true").strip().lower() == "true"
)

# 模型给出的间隔会被夹在这个区间内（分钟）。下限防止刚道别就追问，上限防止一睡不醒
PROACTIVE_SCHEDULE_MIN_MINUTES: int = get_int("PROACTIVE_SCHEDULE_MIN_MINUTES", 10)
PROACTIVE_SCHEDULE_MAX_MINUTES: int = get_int("PROACTIVE_SCHEDULE_MAX_MINUTES", 24 * 60)

# 模型不可用时（网络失败 / mock 模式）的兜底间隔区间（分钟），随机取值
PROACTIVE_FALLBACK_MIN_MINUTES: int = get_int("PROACTIVE_FALLBACK_MIN_MINUTES", 45)
PROACTIVE_FALLBACK_MAX_MINUTES: int = get_int("PROACTIVE_FALLBACK_MAX_MINUTES", 180)

# 后台调度线程的轮询间隔（秒）
PROACTIVE_TICK_SECONDS: int = get_int("PROACTIVE_TICK_SECONDS", 30)

# 未读主动消息堆积上限：界面一直没人看时不再继续生成，避免无限堆积
PROACTIVE_MAX_PENDING: int = get_int("PROACTIVE_MAX_PENDING", 3)

# Web UI 固定会话（主动消息写进这个会话，刷新后能出现在聊天记录里）
PROACTIVE_SESSION_ID: str = get("PROACTIVE_SESSION_ID", "main").strip() or "main"

# ============================================================
# 九、开发者 API 与 Web UI
# ============================================================
# HTTP API 监听地址（127.0.0.1 只允许本机访问，对外暴露请改 0.0.0.0 并自行加鉴权）
API_HOST: str = get("API_HOST", "127.0.0.1")

# HTTP API 端口
API_PORT: int = get_int("API_PORT", 8000)

# 可选 API 令牌：留空表示不鉴权（默认只监听 127.0.0.1）。
# 对外暴露时务必设置，并在请求头带 X-API-Token（或 ?token=）。
API_TOKEN: str = get("API_TOKEN", "").strip()

# 主动联系默认偏好（可在 Web UI「功能」/ 开发者接口按空间覆盖）
PROACTIVE_DND_START: int = get_int("PROACTIVE_DND_START", 23)
PROACTIVE_DND_END: int = get_int("PROACTIVE_DND_END", 8)
PROACTIVE_DAILY_CAP: int = get_int("PROACTIVE_DAILY_CAP", 3)
PROACTIVE_COOLDOWN_MINUTES: int = get_int("PROACTIVE_COOLDOWN_MINUTES", 60)

# 关键参数集合：这些参数即使变化较小，也视为对自我认知有实质影响
PERSONA_CORE_PARAMS: Tuple[str, ...] = (
    "prefer_skepticism",
    "self_reflect_threshold",
    "goal_self_understanding",
    "novelty_bias",
)

# 变迁日志写入阈值：参数绝对变化小于该值时视为噪声，只更新数值、不写日志
PERSONA_CHANGELOG_MIN_DELTA: float = 0.02

# ============================================================
# 人格倾向描述的分级阈值（describe() 用，原先是写死在函数里的 0.6 / 0.8）
# ============================================================
# 为什么改成三级：参数是连续变化的，但描述是阶跃的，0.59 和 0.61 会突然跳一句。
# 而且原实现只描述"高值"，"极不爱拆解""几乎不迁就气氛"这类同样有辨识度的
# 反向特征完全不被表达。
PERSONA_DESC_STRONG: float = 0.72  # >= 该值：显著倾向
PERSONA_DESC_MID: float = 0.45  # [MID, STRONG)：一般倾向；[WEAK, MID)：不描述
PERSONA_DESC_WEAK: float = 0.28  # <= 该值：反向倾向

# 倾向描述最多输出几行（防止把 Prompt 挤满）
PERSONA_DESC_MAX_LINES: int = 8

# ============================================================
# 信念变更进入人格变迁日志的门槛
# ============================================================
# 只有"核心信念"被覆盖时才写 belief: 维度日志：
# 1) importance 达到 PERSONA_BELIEF_LOG_IMPORTANCE；或
# 2) 关系任一端指人格自身，且 importance 达到 PERSONA_BELIEF_LOG_SELF_IMPORTANCE
# 为什么加第二档下限：只按"涉及自我"放行的话，"我更喜欢橘子了"这种琐碎偏好
# 也会天天写进人格日志，把真正重要的因果淹掉。
PERSONA_BELIEF_LOG_IMPORTANCE: float = 0.90
PERSONA_BELIEF_LOG_SELF_IMPORTANCE: float = 0.60

# 上层情景状态：默认衰减率（每轮对话强度衰减比例）
MOOD_DECAY_PER_TURN: float = 0.18

# 上层情景状态：按"真实流逝时间"衰减的速率（每小时指数衰减系数）
# 为什么另加这一条：轮次衰减只在有对话时生效。隔三天不聊天，情绪强度一点都不掉，
# 一开口还用三天前那股情绪接话——不像人。这里按 wall clock 补一次衰减。
# 0.02 对应半衰期约 35 小时：一晚过去掉三成，三天过去基本归于平静。
MOOD_DECAY_LAMBDA_HOUR: float = 0.02

# 少于该时长（小时）不做时间衰减，避免每轮重复折算同一小段时间
MOOD_TIME_DECAY_MIN_HOURS: float = 0.5

# 上层情景状态：强度低于该值即视为消退
MOOD_MIN_INTENSITY: float = 0.15

# 上层情景状态：允许的模式白名单（防止模型自由发挥导致状态无限增长）
MOOD_MODE_WHITELIST: Tuple[str, ...] = (
    "self_reflect",  # 自省：更易检查自身矛盾
    "skeptical",  # 审慎：对新信息更挑剔
    "exploratory",  # 探索：更愿意发散、追问
    "analytic",  # 分析：更倾向分步拆解
    "empathetic",  # 共情：更关注对方感受
)

# 初始叙事（人格快照 v0 的自我描述，随时可被覆盖）
PERSONA_INITIAL_SELF_VIEW: str = (
    "我叫江妍，朋友叫我小苹果。18 岁，东北电力大学计算机院大一，从湘西来的。"
    "喜欢出 cos、看《声之形》、打明日方舟，说话不冲，但别把我当成只会笑的人。"
)

# 人格名称（用于快照文件命名与提示词）——已在上方人格设定处定义

# ============================================================
# 六、归档配置（每轮结束的主动认知归档）
# ============================================================
# 是否启用归档（关闭后仅保留短期上下文与兜底压缩）
ENABLE_ARCHIVE: bool = get("ENABLE_ARCHIVE", "true").strip().lower() == "true"

# 线索：以前/曾经/过去…（用于把"过去值"也纳入检索，回答"以前喜欢吃什么"）
PAST_CUE_PATTERN = re.compile(r"以前|曾经|过去|上次|曾|从前|之前|以前说|以前说过")

# 旧信念"以前说过"的线索：命中后把"过去值"也纳入召回（用于回答"以前/曾经/过去"）
PAST_CUE_PATTERN = re.compile(
    r"以前|曾经|过去|上次|曾|以前说|以前说过|过去说过|第一次|以前爱|以前喜欢|曾经喜欢|从前|之前"
)

# 影响身份/长期偏好、因此必须进入候选状态的高价值关系阈值（重要性）
HIGH_IMPORTANCE: float = get_float("HIGH_IMPORTANCE", 0.85)

# 高价值关系是否默认强制进入候选状态（用户确认后才生效；不是必须全部覆盖）
CANDIDATE_GATE_ENABLED: bool = get("CANDIDATE_GATE_ENABLED", "true").strip().lower() == "true"

# 记忆检索规模上限：超过该条数时停止向量召回并给出明确提示（防止索引膨胀）
MEMORY_CAP_NORMAL: int = get_int("MEMORY_CAP_NORMAL", 800)

# 主动联系：默认是否允许"断交等强烈表达"（开关，不写成专业术语）
SEVERANCE_OPT_IN: bool = get("SEVERANCE_OPT_IN", "true").strip().lower() == "true"

# 主动联系：随时间变化的"故事线"（间隔更新、写入记忆库）
STORYLINE_INTERVAL_MINUTES: int = get_int("STORYLINE_INTERVAL_MINUTES", 60)

# 主动联系：最近使用过的主题/记忆冷却窗口（避免重复抽到同一件事）
REMEMBER_COOLDOWN_MINUTES: int = get_int("REMEMBER_COOLDOWN_MINUTES", 120)

# 限流：单接口最大请求数（滑动窗口）
RATE_LIMIT_MAX: int = get_int("RATE_LIMIT_MAX", 20)

# 来源限制：允许访问的来源白名单（默认只允许本地来源）
PRIVACY_ALLOWED_ORIGINS: tuple = tuple(get("PRIVACY_ALLOWED_ORIGINS", "127.0.0.1,localhost").split(","))

# 记忆库最大向量条数（超限即停止向量召回并标记）
MEMORY_MAX_VECTORS: int = get_int("MEMORY_MAX_VECTORS", 300)

# 单轮最多入库的经验片段条数
ARCHIVE_MAX_EXPERIENCES: int = get_int("ARCHIVE_MAX_EXPERIENCES", 5)

# 单条经验片段最大字符数（过长会稀释向量检索效果）
EXPERIENCE_MAX_CHARS: int = get_int("EXPERIENCE_MAX_CHARS", 300)

# 归档是否同步执行（False 时放到后台线程，避免阻塞用户响应）
ARCHIVE_SYNC: bool = get("ARCHIVE_SYNC", "true").strip().lower() == "true"

# ============================================================
# 七、RAG 兜底插件配置
# ============================================================
# 是否允许在信息不足时启用 RAG
ENABLE_RAG: bool = get("ENABLE_RAG", "true").strip().lower() == "true"

# RAG 返回片段条数上限
RAG_TOP_K: int = get_int("RAG_TOP_K", 3)

# RAG 片段最低相似度
RAG_MIN_SCORE: float = get_float("RAG_MIN_SCORE", 0.15)

# 单个 RAG 片段注入时的最大字符数
RAG_MAX_CHARS: int = get_int("RAG_MAX_CHARS", 800)

# ============================================================
# 十、运行开关
# ============================================================
# 是否在终端打印链路调试信息（silent / normal / verbose）
VERBOSITY: str = get("VERBOSITY", "normal").strip().lower()


# ============================================================
# 十一、配置验证
# ============================================================
def validate_config() -> None:
    """
    验证配置项的有效性和一致性。

    :raises ValueError: 当配置项无效或冲突时
    """
    # 检查必需的环境变量（测试环境跳过）
    if not os.environ.get("TESTING", False):
        required_env_vars = ["DEEPSEEK_API_KEY"]
        for var in required_env_vars:
            # globals() 返回的是 dict，必须用 .get 取键；
            # 之前写成 getattr(globals(), var) 是对 dict 对象查属性，永远取不到值
            if not globals().get(var):
                raise ValueError(f"Required environment variable {var} is missing")

    # 验证数值范围
    if MAX_SAFE_TOKENS < 1000:
        raise ValueError("MAX_SAFE_TOKENS must be at least 1000")

    if KEEP_RECENT_ROUNDS < 1:
        raise ValueError("KEEP_RECENT_ROUNDS must be at least 1")

    if HISTORY_INJECT_ROUNDS < 1:
        raise ValueError("HISTORY_INJECT_ROUNDS must be at least 1")

    if HISTORY_INJECT_ROUNDS > KEEP_RECENT_ROUNDS:
        raise ValueError(
            "HISTORY_INJECT_ROUNDS cannot be greater than KEEP_RECENT_ROUNDS"
        )

    if ANSWER_MAX_TOKENS < 100:
        raise ValueError("ANSWER_MAX_TOKENS must be at least 100")

    if EXTRACT_MAX_TOKENS < 100:
        raise ValueError("EXTRACT_MAX_TOKENS must be at least 100")

    # 人格原始数据（data/persona/）的基本一致性
    if not PERSONA_NAME.strip():
        raise ValueError("PERSONA_NAME must not be empty")
    if not PERSONA_PROFILE:
        raise ValueError("PERSONA_PROFILE must not be empty")
    if set(PERSONA_KERNEL_INIT) != set(PERSONA_PARAM_DESC):
        raise ValueError(
            "PERSONA_KERNEL_INIT and PERSONA_PARAM_DESC must define the same parameters"
        )

    # 验证人格参数范围
    for param, value in PERSONA_KERNEL_INIT.items():
        if not (0.0 <= value <= 1.0):
            raise ValueError(
                f"PERSONA_KERNEL_INIT parameter {param} must be between 0.0 and 1.0"
            )

    # 验证阈值设置
    if PERSONA_DESC_STRONG <= PERSONA_DESC_MID:
        raise ValueError("PERSONA_DESC_STRONG must be greater than PERSONA_DESC_MID")

    if PERSONA_DESC_MID <= PERSONA_DESC_WEAK:
        raise ValueError("PERSONA_DESC_MID must be greater than PERSONA_DESC_WEAK")

    if PERSONA_SUBSTANTIVE_DELTA <= 0:
        raise ValueError("PERSONA_SUBSTANTIVE_DELTA must be positive")

    if PERSONA_DRIFT_LIMIT <= 0:
        raise ValueError("PERSONA_DRIFT_LIMIT must be positive")

    if PERSONA_DAILY_MAX_STEP <= 0:
        raise ValueError("PERSONA_DAILY_MAX_STEP must be positive")

    # 好感度阈值必须构成"断交 < 冷淡 < 和解"的阶梯，否则状态机会来回抖动
    if not (
        0.0 <= AFFECTION_SEVER_THRESHOLD < AFFECTION_COLD_THRESHOLD <= 100.0
    ):
        raise ValueError("AFFECTION thresholds must satisfy 0 <= SEVER < COLD <= 100")
    if not (
        AFFECTION_COLD_THRESHOLD < AFFECTION_RECONCILE_THRESHOLD <= 100.0
    ):
        raise ValueError(
            "AFFECTION_RECONCILE_THRESHOLD must be between COLD and 100"
        )

    # 主动消息的模型间隔与兜底间隔都必须落在合法区间内
    if PROACTIVE_SCHEDULE_MIN_MINUTES < 1:
        raise ValueError("PROACTIVE_SCHEDULE_MIN_MINUTES must be at least 1")
    if PROACTIVE_SCHEDULE_MAX_MINUTES < PROACTIVE_SCHEDULE_MIN_MINUTES:
        raise ValueError(
            "PROACTIVE_SCHEDULE_MAX_MINUTES must be >= PROACTIVE_SCHEDULE_MIN_MINUTES"
        )
    if PROACTIVE_FALLBACK_MIN_MINUTES < 1:
        raise ValueError("PROACTIVE_FALLBACK_MIN_MINUTES must be at least 1")
    if PROACTIVE_FALLBACK_MAX_MINUTES < PROACTIVE_FALLBACK_MIN_MINUTES:
        raise ValueError(
            "PROACTIVE_FALLBACK_MAX_MINUTES must be >= PROACTIVE_FALLBACK_MIN_MINUTES"
        )


# 配置验证延迟到实际使用时执行
# 这样可以避免在导入时立即失败，允许测试环境正常运行


# ============================================================
# 运行时开关落盘：data/ui_settings.json → config 全局覆盖
# ============================================================
def apply_runtime_toggles() -> None:
    """
    读取 data/ui_settings.json 里的 toggles，覆盖同名大写配置项。

    为什么放在这里：设置页的开关由普通用户操作，不应该要求改 .env 或重启代码。
    只在值存在时覆盖，缺失项保持 .env / 默认值。
    """
    import json

    file = DB_PATH.parent / "ui_settings.json"
    if not file.exists():
        return
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except Exception:
        return
    toggles = data.get("toggles") or {}
    if not isinstance(toggles, dict):
        return
    for key, value in toggles.items():
        if not isinstance(key, str) or not key.isupper():
            continue
        if key in globals():
            globals()[key] = bool(value)


apply_runtime_toggles()
