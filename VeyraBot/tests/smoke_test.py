"""
冒烟测试：不调用真实模型，验证存储层、人格内核、向量检索、压缩逻辑。

运行：
    python tests/smoke_test.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

# 允许从项目根目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from cognitive.models import KGEntity, KGRelation, SessionContext, Topic
from cognitive.persona.kernel import PersonaKernel
from cognitive.storage.database import Database
from cognitive.storage.kg_store import KGStore
from cognitive.storage.topic_store import TopicStore
from cognitive.storage.vector_store import VectorStore


def _assert(condition: bool, message: str) -> None:
    """简单断言，失败立即抛出。"""
    if not condition:
        raise AssertionError(message)


def test_kg_conflict(tmp_dir: Path) -> None:
    """验证：同一关系维度出现新值时，旧信念被标记失效。"""
    db = Database(tmp_dir / "test.db")
    db.ensure_space("sp", "测试")
    kg = KGStore(db)

    kg.upsert_relation(
        KGRelation(
            rid="r1",
            space_id="sp",
            head_name="项目",
            rel="use_language",
            tail_name="Java",
            confidence=0.8,
            importance=0.6,
        )
    )
    outcome = kg.upsert_relation(
        KGRelation(
            rid="r2",
            space_id="sp",
            head_name="项目",
            rel="use_language",
            tail_name="Python",
            confidence=0.8,
            importance=0.6,
        )
    )

    _assert(len(outcome["conflicts"]) == 1, "应检测到一次信念冲突")

    facts = kg.query_by_text("sp", "项目")
    text = " ".join(facts)
    _assert("Python" in text, "新信念应可被查询")
    _assert("Java" not in text, "旧信念应被过滤")


def test_kg_boost(tmp_dir: Path) -> None:
    """验证：重复确认同一信念时权重上升，不产生重复记录。"""
    db = Database(tmp_dir / "test.db")
    db.ensure_space("sp2", "测试")
    kg = KGStore(db)

    kg.upsert_relation(
        KGRelation(
            rid="r1",
            space_id="sp2",
            head_name="用户",
            rel="prefer",
            tail_name="简洁",
            confidence=0.6,
            importance=0.5,
        )
    )
    before = kg.count_active_relations("sp2")

    kg.upsert_relation(
        KGRelation(
            rid="r2",
            space_id="sp2",
            head_name="用户",
            rel="prefer",
            tail_name="简洁",
            confidence=0.6,
            importance=0.5,
        )
    )
    after = kg.count_active_relations("sp2")

    _assert(before == after == 1, "重复确认不应新增记录")

    beliefs = kg.query_core_beliefs("sp2", importance_threshold=0.5)
    _assert(beliefs and beliefs[0]["confidence"] > 0.6, "置信度应被加固")


def test_kg_boost_production_rid(tmp_dir: Path) -> None:
    """验证：生产路径（同三元组 → 同 rid）重复确认时真的会加固。

    之前的 test_kg_boost 用不同 rid 构造"同 tail"，生产代码（archiver）
    永远是同三元组同 rid，加固分支实际不可达 —— 本项是其真实路径回归。
    """
    from cognitive.cognition.archiver import _stable_id
    from cognitive.storage.kg_store import CONFIDENCE_BOOST, IMPORTANCE_BOOST

    db = Database(tmp_dir / "test.db")
    db.ensure_space("spBoost", "测试")
    kg = KGStore(db)

    def _relation(rid: str) -> KGRelation:
        return KGRelation(
            rid=rid,
            space_id="spBoost",
            head_name="江妍",
            rel="喜欢",
            tail_name="明日方舟",
            confidence=0.7,
            importance=0.6,
        )

    # rid 与 archiver 一致：由三元组稳定生成
    rid = _stable_id("spBoost", "江妍", "喜欢", "明日方舟")
    first = kg.upsert_relation(_relation(rid))
    _assert(first["created"] is True, "首次写入应为新建")
    _assert(first["boosted"] is False, "首次写入不应加固")

    # 同一信念再次出现 → 同 rid 再次写入（生产路径）
    second = kg.upsert_relation(_relation(rid))
    _assert(second["boosted"] is True, "同 rid 重复确认必须触发加固")
    _assert(second["created"] is False, "加固走 update，不应报告新建")

    _assert(
        kg.count_active_relations("spBoost") == 1, "加固不应产生重复记录"
    )

    beliefs = kg.query_core_beliefs("spBoost", importance_threshold=0.5)
    _assert(beliefs, "应能查到该信念")
    _assert(
        abs(beliefs[0]["confidence"] - (0.7 + CONFIDENCE_BOOST)) < 1e-6,
        f"置信度应增加 {CONFIDENCE_BOOST}（实际 {beliefs[0]['confidence']}）",
    )
    _assert(
        abs(beliefs[0]["importance"] - (0.6 + IMPORTANCE_BOOST)) < 1e-6,
        f"重要性应增加 {IMPORTANCE_BOOST}（实际 {beliefs[0]['importance']}）",
    )


def test_vector_search(tmp_dir: Path) -> None:
    """验证：主题摘要可被中文模糊检索命中。"""
    store = VectorStore("sp3")
    store.add("t1", "讨论知识图谱信念存储与冲突检测")
    store.add("t2", "讨论前端页面布局与样式细节")

    hits = store.search("知识图谱怎么处理冲突")
    _assert(hits and hits[0][0] == "t1", "应命中知识图谱主题")


def test_vector_rewrite_recall(tmp_dir: Path) -> None:
    """验证：换种说法（几乎不共享字面）也能召回，且不误伤无关主题。"""
    store = VectorStore("sp3b")
    store.add(
        "cat",
        "三花猫辣椒是 2021 年楼下捡的，五公斤，最黏她。",
        {"keywords": ["猫", "辣椒", "三花", "宠物"]},
    )
    store.add(
        "cos",
        "第一次出角是能天使，妆造全部自己来，假发剪坏两顶。",
        {"keywords": ["cos", "出角", "明日方舟"]},
    )
    store.add(
        "code",
        "Topic-Centric 主题索引与 Graph RAG 的取舍。",
        {"keywords": ["记忆", "架构", "检索"]},
    )

    # 「你养的那只猫」→ 与原文重合只有"猫"一个字
    hits = store.search("你养的那只猫叫什么")
    _assert(
        hits and hits[0][0] == "cat", f"改写式查询应召回猫的主题（实际 {hits[:2]}）"
    )
    _assert(all(hit[0] != "code" for hit in hits), "不应把技术主题召回")

    # 「你第一次出角色是什么感受」→ 用"出角色"而非"出角"
    hits = store.search("你第一次出角色是什么感受")
    _assert(
        hits and hits[0][0] == "cos", f"「出角色」应召回出角主题（实际 {hits[:2]}）"
    )

    # 纯技术问题不应命中她的人设主题
    hits = store.search("embedding 和向量检索的区别")
    _assert(hits and hits[0][0] == "code", f"技术查询应命中技术主题（实际 {hits[:2]}）")


def test_router_consolidated_filter(tmp_dir: Path) -> None:
    """验证：已归并主题不再进入读路径召回；seed 前史例外，仍可召回。"""
    from cognitive.cognition.router import MemoryRouter
    from cognitive.persona.seed import SEED_SESSION_ID

    db = Database(tmp_dir / "route.db")
    db.ensure_space("spRoute", "测试")
    store = TopicStore(db)
    vector = VectorStore("spRoute")

    normal = Topic(
        topic_id="n1",
        space_id="spRoute",
        title="学会烤面包",
        abstract="第一次用烤箱烤出了面包",
        keywords=["烤面包", "烤箱"],
        create_ts=100,
    )
    store.save_topic(normal)
    vector.add(
        "n1", "学会烤面包 第一次用烤箱烤出了面包", {"keywords": ["烤面包", "烤箱"]}
    )

    seed_topic = Topic(
        topic_id="s1",
        space_id="spRoute",
        title="高考前和妈妈吵架",
        abstract="高考前一夜和妈妈大吵了一架",
        keywords=["高考", "吵架"],
        session_id=SEED_SESSION_ID,
        # seed 前史 consolidated=True 只为不参与日结，必须仍可召回
        consolidated=True,
        create_ts=50,
    )
    store.save_topic(seed_topic)
    vector.add(
        "s1", "高考前和妈妈吵架 高考前一夜和妈妈大吵了一架", {"keywords": ["高考", "吵架"]}
    )

    router = MemoryRouter(db)

    recalled = router.route("spRoute", "你会烤面包吗")
    _assert(
        any(t.topic_id == "n1" for t in recalled.topics),
        f"未归并主题应可召回（实际 {[t.topic_id for t in recalled.topics]}）",
    )

    # 归并之后：原主题是"原料"，不应再进入召回
    store.mark_consolidated("spRoute", ["n1"])
    recalled = router.route("spRoute", "你会烤面包吗")
    _assert(
        all(t.topic_id != "n1" for t in recalled.topics),
        f"已归并主题不应再被召回（实际 {[t.topic_id for t in recalled.topics]}）",
    )

    # seed 前史：即使 consolidated=True 也要能召回（前史召回是根基）
    recalled = router.route("spRoute", "高考前你和妈妈吵架了吗")
    _assert(
        any(t.topic_id == "s1" for t in recalled.topics),
        f"seed 前史应仍可召回（实际 {[t.topic_id for t in recalled.topics]}）",
    )


def test_persona_delta(tmp_dir: Path) -> None:
    """验证：人格参数可增量演化，且实质性变更判定正确。"""
    db = Database(tmp_dir / "test.db")
    kernel = PersonaKernel(db, "sp4")

    old = kernel.get_params()
    changes = kernel.apply_deltas({"prefer_skepticism": 0.08})
    _assert(len(changes) == 1, "应产生一条参数变化")

    substantive, reasons = kernel.is_substantive_change(changes, False)
    _assert(substantive, "0.08 的变化应判定为实质性跃迁")
    _assert(reasons, "应给出触发原因")

    # 越界裁剪
    kernel.apply_deltas({"prefer_skepticism": 10.0})
    new = kernel.get_params()
    _assert(new["prefer_skepticism"] <= 1.0, "参数应被裁剪到 0~1")

    # 非法参数应被忽略
    changes = kernel.apply_deltas({"hack_param": 0.1})
    _assert(changes == [], "白名单外参数应被忽略")


def test_mood_decay(tmp_dir: Path) -> None:
    """验证：上层情景状态随时间衰减并最终消失。"""
    db = Database(tmp_dir / "test.db")
    kernel = PersonaKernel(db, "sp5")

    kernel.set_mood("self_reflect", 0.8)
    _assert("self_reflect" in kernel.get_moods(), "情景状态应被激活")

    for _ in range(30):
        kernel.decay_moods()

    _assert("self_reflect" not in kernel.get_moods(), "多次衰减后情景状态应消失")


def test_topic_and_session(tmp_dir: Path) -> None:
    """验证：主题可存取，会话 JSON 可往返序列化。"""
    db = Database(tmp_dir / "test.db")
    store = TopicStore(db)

    topic = Topic(
        topic_id="t1",
        space_id="sp6",
        title="标题",
        abstract="摘要",
        keywords=["a", "b"],
    )
    store.save_topic(topic)

    loaded = store.get_topic("sp6", "t1")
    _assert(loaded is not None and loaded.title == "标题", "主题应可正确读取")

    ctx = SessionContext(session_id="s1", space_id="sp6")
    ctx.append("user", "你好")
    restored = SessionContext.from_dict(ctx.to_dict())
    _assert(len(restored.messages) == 1, "会话应可往返序列化")


def test_rag_loop_bounds(tmp_dir: Path) -> None:
    """验证：检索循环有界 —— 查询判重生效、最后一轮强制收束。"""
    from cognitive.cognition.judge import InfoJudge
    from cognitive.engine import CognitiveAgent

    # 1) 同义改写应被判为重复，换角度不应
    _assert(
        CognitiveAgent._query_too_similar(
            "topic centric graph rag 区别",
            ["topic-centric 与 Graph RAG 区别"],
        ),
        "同义改写应判为重复查询",
    )
    _assert(
        not CognitiveAgent._query_too_similar(
            "Graph RAG 的社区层级如何构建",
            ["topic-centric 与 Graph RAG 区别"],
        ),
        "换角度的查询不应被误判为重复",
    )

    # 2) 到达轮数上限时，判决器必须收束（不调模型也应返回 need_rag=False）
    judge = InfoJudge(client=None)
    result = judge.judge(
        user_message="topic-centric 和 Graph RAG 的区别",
        self_view="测试自我",
        kg_facts=[],
        topics=[],
        experiences=[],
        recent_context="",
        round_index=config.RAG_LOOP_MAX_ROUNDS,
        max_rounds=config.RAG_LOOP_MAX_ROUNDS,
    )
    _assert(result["need_rag"] is False, "最后一轮不得再申请检索")
    _assert(result["is_final_round"] is True, "最后一轮应标记 is_final_round")
    _assert(result["round_index"] == config.RAG_LOOP_MAX_ROUNDS, "轮次信息应透出")


def test_history_window(tmp_dir: Path) -> None:
    """验证：50 轮短期窗口内不压缩，超阈值才摘要化。"""
    from cognitive.session import SessionManager

    db = Database(tmp_dir / "window.db")
    db.ensure_space("sp7", "测试")

    class _FakeClient:
        """离线替身：返回固定摘要，避免真实网络调用。"""

        def chat(self, messages, temperature=0.2, max_tokens=500):
            return "【测试摘要】" + str(messages[-1]["content"])[:200]

    manager = SessionManager(db, client=_FakeClient())
    context = SessionContext(session_id="s7", space_id="sp7")

    # 50 轮（100 条）：必须完全不压缩
    for index in range(config.KEEP_RECENT_ROUNDS):
        context.append("user", f"第{index}轮问题，内容较短。")
        context.append("assistant", f"第{index}轮回答，内容较短。")

    report = manager.maybe_compress(context)
    _assert(report["triggered"] is False, "50 轮以内不应触发压缩")
    _assert(
        len(context.messages) == config.KEEP_RECENT_ROUNDS * 2, "50 轮原文必须完整保留"
    )
    _assert(not context.compressed_summary, "未压缩时不应出现摘要")

    # 继续堆长文本，越过安全阈值（此时才允许摘要化）
    long_text = "很长的内容。" * 400
    for index in range(20):
        context.append("user", long_text)
        context.append("assistant", long_text)

    report = manager.maybe_compress(context)
    _assert(report["triggered"] is True, "超出安全阈值应触发压缩")
    _assert(bool(context.compressed_summary), "压缩后应留下摘要")
    _assert(report["after"] < report["before"], "压缩后 token 应下降")


def test_daily_consolidate(tmp_dir: Path) -> None:
    """验证：日结把散落的往事归并成一条 daily 主题，且原主题被标记已归并。"""
    import cognitive.cognition.consolidator as consolidator_module

    db = Database(tmp_dir / "daily.db")
    db.ensure_space("sp8", "测试")
    store = TopicStore(db)

    for index in range(1, 4):
        store.save_topic(
            Topic(
                topic_id=f"t{index}",
                space_id="sp8",
                title=f"往事{index}",
                abstract=f"第{index}件事的经过",
                keywords=["记忆", "人格"],
                create_ts=1000 + index,
            )
        )

    # 关掉模型调用，走机械摘要分支（离线可跑）
    original = config.DAILY_USE_MODEL
    config.DAILY_USE_MODEL = False
    try:
        consolidator = consolidator_module.Consolidator(db)
        report = consolidator.consolidate_daily("sp8", now_ts=2000)
    finally:
        config.DAILY_USE_MODEL = original

    _assert(report["ok"] is True, f"日结应当成功：{report.get('reason')}")
    _assert(report["degraded"] is True, "关掉模型时应标记降级")
    _assert(report["summary_chars"] > 0, "应产出摘要正文")

    counts = store.count_by_kind("sp8")
    _assert(counts["daily"] == 1, "应生成一条日结主题")
    _assert(counts["pending"] == 0, "原往事应全部标记已归并")

    for index in range(1, 4):
        topic = store.get_topic("sp8", f"t{index}")
        _assert(topic.consolidated is True, "原主题应标记 consolidated=1")

    # 再来一次：没有新往事，必须跳过且不产生日志
    report = consolidator.consolidate_daily("sp8", now_ts=3000)
    _assert(report["ok"] is False, "没有新往事时不应重复巩固")
    _assert("没有新的往事" in report["reason"], "应说明跳过原因")
    _assert(len(consolidator.list_logs("sp8")) == 1, "不应留下空的巩固日志")


def test_big_event_marking(tmp_dir: Path) -> None:
    """验证：大事可被标记并单独沉淀，计数与普通主题分开。"""
    import cognitive.cognition.consolidator as consolidator_module

    db = Database(tmp_dir / "event.db")
    db.ensure_space("sp9", "测试")
    store = TopicStore(db)

    topic = Topic(
        topic_id="ev1",
        space_id="sp9",
        title="决定长期采用 Topic 索引",
        abstract="用户当场拍板：以后都用主题索引做长期记忆主干。",
        keywords=["决策", "架构"],
        create_ts=5000,
    )
    store.save_topic(topic)

    original = config.DAILY_USE_MODEL
    config.DAILY_USE_MODEL = False
    try:
        report = consolidator_module.Consolidator(db).consolidate_event("sp9", topic)
    finally:
        config.DAILY_USE_MODEL = original

    _assert(report["ok"] is True, "大事沉淀应成功")
    counts = store.count_by_kind("sp9")
    _assert(counts["event"] == 1, "应生成一条 event 主题")
    _assert(store.get_topic("sp9", "ev1").is_big_event is True, "原主题应被标记为大事")

    # 幂等：同一主题重复沉淀（既大事又跃迁）不应再落库
    again = consolidator_module.Consolidator(db).consolidate_event("sp9", topic)
    _assert(again["ok"] is False, "同一主题不应重复沉淀")
    _assert("已做过事件沉淀" in again["reason"], "应说明重复原因")
    _assert(store.count_by_kind("sp9")["event"] == 1, "重复沉淀不应新增 event 主题")


def test_schema_migration(tmp_dir: Path) -> None:
    """验证：老库缺列时能自动补齐，且重复初始化幂等。"""
    db = Database(tmp_dir / "migrate.db")
    connection = db.conn()

    # 造一张"老结构"的 topic 表（没有新列）
    connection.execute("drop table if exists topic")
    connection.execute(
        """
        create table topic (
            space_id text, topic_id text, title text, abstract text,
            keywords text, session_id text, snapshot_version integer, create_ts integer,
            primary key (space_id, topic_id)
        )
        """
    )
    connection.commit()

    db.init_schema()
    db.init_schema()

    columns = {
        row["name"] for row in connection.execute("pragma table_info(topic)").fetchall()
    }
    for expected in ("kind", "consolidated", "is_big_event"):
        _assert(expected in columns, f"迁移后应存在 {expected} 列")

    # 老结构的 kg_relation（没有 multi_valued 列）也应被自动补齐
    connection.execute("drop table if exists kg_relation")
    connection.execute(
        """
        create table kg_relation (
            space_id text, rid text, head_name text, rel text, tail_name text,
            confidence real, importance real, is_active integer,
            source_topic_id text, create_ts integer, update_ts integer,
            primary key (space_id, rid)
        )
        """
    )
    connection.commit()

    db.init_schema()
    kg_columns = {
        row["name"]
        for row in connection.execute("pragma table_info(kg_relation)").fetchall()
    }
    _assert("multi_valued" in kg_columns, "迁移后 kg_relation 应存在 multi_valued 列")


def test_persona_baseline(tmp_dir: Path) -> None:
    """验证：基调是锚点 —— 日内漂移被夹在摆动带，且不产生人格版本。"""
    db = Database(tmp_dir / "test.db")
    kernel = PersonaKernel(db, "spBase")
    init_value = float(config.PERSONA_KERNEL_INIT["prefer_skepticism"])

    # 连续 10 次同向推进，当前值必须停在 base + 摆动带
    for _ in range(10):
        kernel.apply_deltas({"prefer_skepticism": 0.05})

    current = kernel.get_params()["prefer_skepticism"]
    _assert(
        current <= init_value + config.PERSONA_DRIFT_LIMIT + 1e-6,
        "当前值应被夹在基调摆动带内（否则人格会被几轮对话拉飞）",
    )
    _assert(kernel.get_current_version() == 0, "日内漂移不应产生人格版本")

    accumulated = kernel.get_pending()["prefer_skepticism"]
    _assert(accumulated > 0, "日内增量应累计为待结算偏移")

    # 日结结算：基调单步位移受上限约束，且当前值复位到新基调
    steps = kernel.settle_daily()
    step = next(item for item in steps if item["param"] == "prefer_skepticism")
    moved = abs(step["base_to"] - step["base_from"])
    _assert(
        moved <= config.PERSONA_DAILY_MAX_STEP + 1e-6,
        f"单日基调位移应受上限约束（实际 {moved:.4f}）",
    )

    after = kernel.get_params()["prefer_skepticism"]
    _assert(abs(after - step["base_to"]) < 1e-6, "结算后当前值应复位到新基调")
    _assert(
        kernel.get_pending().get("prefer_skepticism", 0.0) == 0,
        "结算后待结算偏移应清零",
    )


def test_persona_daily_promotion(tmp_dir: Path) -> None:
    """验证：基调缓慢演化 —— 单日不跃迁，多日同向累计才跨过阈值。"""
    db = Database(tmp_dir / "test.db")
    init_value = float(config.PERSONA_KERNEL_INIT["prefer_skepticism"])

    # 3 天同向：累计 0.03，不该产生新版本
    kernel_short = PersonaKernel(db, "spP3")
    for _ in range(3):
        kernel_short.apply_deltas({"prefer_skepticism": 0.05})
        kernel_short.settle_daily()
    base_short = kernel_short.get_baseline()["prefer_skepticism"]
    substantive_short, _ = kernel_short.is_substantive_change(
        [("prefer_skepticism", init_value, base_short)], False
    )
    _assert(
        not substantive_short,
        "3 天累计不应立即跃迁（这正是'不要变化太快'的关键）",
    )

    # 8 天同向：累计超过 0.06，应判定为实质跃迁
    kernel_long = PersonaKernel(db, "spP8")
    for _ in range(8):
        kernel_long.apply_deltas({"prefer_skepticism": 0.05})
        kernel_long.settle_daily()
    base_long = kernel_long.get_baseline()["prefer_skepticism"]
    _assert(base_long > base_short, "多日同向应持续推动基调")
    substantive_long, reasons = kernel_long.is_substantive_change(
        [("prefer_skepticism", init_value, base_long)], False
    )
    _assert(substantive_long, "8 天累计应跨过实质跃迁阈值")
    _assert(reasons, "跃迁应给出触发原因")


def test_overturned_beliefs(tmp_dir: Path) -> None:
    """验证：被推翻的旧信念能进记忆区块，让 AI 知道自己改过主意。"""
    from cognitive.models import RetrievedMemory

    db = Database(tmp_dir / "test.db")
    db.ensure_space("spOver", "推翻测试")
    kg = KGStore(db)

    kg.upsert_relation(
        KGRelation(
            rid="r1",
            space_id="spOver",
            head_name="记忆主干",
            rel="策略",
            tail_name="向量优先",
            confidence=0.8,
            importance=0.7,
        )
    )
    kg.upsert_relation(
        KGRelation(
            rid="r2",
            space_id="spOver",
            head_name="记忆主干",
            rel="策略",
            tail_name="主题优先",
            confidence=0.9,
            importance=0.8,
        )
    )

    # 生效列表里只有新值
    facts = " ".join(kg.query_by_text("spOver", "记忆主干怎么定的"))
    _assert("向量优先" not in facts, "生效列表不应再出现旧值")

    # 但失效列表里能拿到「原值 → 现为」
    items = kg.query_overturned("spOver", "记忆主干怎么定的")
    _assert(len(items) == 1, f"应召回 1 条被推翻的旧信念（实际 {len(items)}）")
    _assert(items[0]["old"] == "向量优先", "应给出原值")
    _assert(items[0]["new"] == "主题优先", "应给出替代后的新值")

    block = RetrievedMemory(overturned=items).to_prompt_block()
    _assert("曾被推翻" in block, "记忆区块应包含「曾被推翻」小节")
    _assert("向量优先" in block, "被推翻的旧值应出现在记忆区块里")


def test_big_event_gate(tmp_dir: Path) -> None:
    """验证：大事判定要求「涉及自我」，纯技术问答不再被误判。"""
    from cognitive.cognition.archiver import judge_big_event    # 场景 1：高 importance，但讲的是技术概念，与她无关 —— 不应判大事
    tech = [
        {
            "head": "Topic-Centric 索引",
            "rel": "区别于",
            "tail": "Graph RAG",
            "importance": 0.92,
        }
    ]
    is_big, _ = judge_big_event(tech, "帮我讲讲这两种做法的区别")
    _assert(is_big is False, "纯技术问答不应判为大事")

    # 场景 2：高 importance 且说的是她自己 —— 应判大事
    self_view = [
        {
            "head": config.PERSONA_NAME,
            "rel": "不愿被当作",
            "tail": "只会可爱的女生",
            "importance": 0.95,
        }
    ]
    is_big, reason = judge_big_event(self_view, "你是不是不高兴被这么说")
    _assert(is_big is True, "涉及自身定位的高价值信念应判为大事")
    _assert("涉及自身定位" in reason, "应说明判定依据")

    # 场景 3：用"你"作主语（模型常这么写），归一后仍算她本人
    alias_view = [{"head": "你", "rel": "在意", "tail": "被敷衍", "importance": 0.9}]
    _assert(
        judge_big_event(alias_view, "说说你在意什么")[0] is True,
        "自指写法应能识别为本人",
    )

    # 场景 4：长期指令词直接命中，不看 importance
    _assert(
        judge_big_event([], "从今天起，家里的事别往外讲")[0] is True,
        "长期指令词应直接判大事",
    )

    # 场景 5：重要性不够（0.85 < 0.9），即使说的是她，也不判大事
    mid = [
        {"head": config.PERSONA_NAME, "rel": "喜欢", "tail": "吃辣", "importance": 0.85}
    ]
    _assert(judge_big_event(mid, "你爱吃辣吗")[0] is False, "低于阈值不应判大事")


def test_kg_multi_valued(tmp_dir: Path) -> None:
    """验证：多值维度（multi=true）共存；单值维度仍维持新值推翻旧值。"""
    db = Database(tmp_dir / "test.db")
    db.ensure_space("spMV", "多值测试")
    kg = KGStore(db)

    # 单值维度：生日只能有一个 —— 新值推翻旧值（原行为不变）
    kg.upsert_relation(
        KGRelation(
            rid="mv1",
            space_id="spMV",
            head_name="小苹果",
            rel="birth_day",
            tail_name="3月5日",
        )
    )
    outcome = kg.upsert_relation(
        KGRelation(
            rid="mv2",
            space_id="spMV",
            head_name="小苹果",
            rel="birth_day",
            tail_name="4月1日",
        )
    )
    _assert(len(outcome["conflicts"]) == 1, "单值维度新值应推翻旧值")
    _assert(not outcome["coexisted"], "单值维度不应标记共存")

    # 多值维度：室友可以有多个 —— 共存
    kg.upsert_relation(
        KGRelation(
            rid="mv3",
            space_id="spMV",
            head_name="小苹果",
            rel="roommate",
            tail_name="陈婷",
            multi_valued=True,
        )
    )
    outcome = kg.upsert_relation(
        KGRelation(
            rid="mv4",
            space_id="spMV",
            head_name="小苹果",
            rel="roommate",
            tail_name="刘思",
            multi_valued=True,
        )
    )
    _assert(outcome["conflicts"] == [], "多值维度不应产生冲突")
    _assert(outcome["coexisted"], "多值维度应标记共存")
    _assert(outcome["created"], "多值新取值应作为新行写入")

    facts = kg.query_by_text("spMV", "小苹果的室友都有谁")
    text = " ".join(facts)
    _assert("陈婷" in text, "第一个室友应保留（未被推翻）")
    _assert("刘思" in text, "第二个室友应可查询（共存生效）")

    # 列表接口应能看到多值标识
    rows = kg.list_relations("spMV")
    roommates = [r for r in rows if r["rel"] == "roommate"]
    _assert(len(roommates) == 2, "多值维度应有 2 行生效记录")
    _assert(all(r["multi_valued"] for r in roommates), "多值标识应落库")
    _assert(
        all(r["is_active"] for r in roommates),
        "共存的多值行都应保持生效",
    )

    # 任一方多值即共存：旧单值行遇多值新知也不推翻
    kg.upsert_relation(
        KGRelation(
            rid="mv5",
            space_id="spMV",
            head_name="我",
            rel="favorite_game",
            tail_name="明日方舟",
        )
    )
    outcome = kg.upsert_relation(
        KGRelation(
            rid="mv6",
            space_id="spMV",
            head_name="我",
            rel="favorite_game",
            tail_name="原神",
            multi_valued=True,
        )
    )
    _assert(outcome["coexisted"] and outcome["conflicts"] == [], "旧单值行遇多值新知应共存")


def test_affection_state_machine(tmp_dir: Path) -> None:
    """验证：好感度阈值触发冷淡 → 断交 → 和解的完整状态机。"""
    from cognitive.storage.affection_store import AffectionStore

    db = Database(tmp_dir / "test.db")
    db.ensure_space("spAff", "好感度测试")
    store = AffectionStore(db)

    # 基线 50 → 28：跨过冷淡线（30），触发 cold 事件
    out = store.apply_delta("spAff", "王刚", -22, "当众让我难堪")
    _assert(out["events"] == ["cold"], f"应触发 cold 事件（实际 {out['events']}）")
    _assert(store.get("spAff", "王刚")["status"] == "cold", "状态应为 cold")

    # 28 → 10：跨过断交线（12），触发 severed；此后不再主动联系
    out = store.apply_delta("spAff", "王刚", -18, "又骗了我一次")
    _assert(out["events"] == ["severed"], f"应触发 severed 事件（实际 {out['events']}）")
    row = store.get("spAff", "王刚")
    _assert(row["status"] == "broken", "状态应为 broken")
    _assert(abs(row["value"] - 10.0) < 1e-6, "数值应正确累计")

    # 断交后回升但未过和解线（60）：保持 broken，不和解
    out = store.apply_delta("spAff", "王刚", 20, "试图缓和")
    _assert(out["events"] == [], "低于和解线不应触发任何事件")
    _assert(store.get("spAff", "王刚")["status"] == "broken", "应保持 broken")

    # 跨过和解线：触发 reconcile，恢复正常来往
    out = store.apply_delta("spAff", "王刚", 40, "真诚道歉并补救")
    _assert("reconciled" in out["events"], "跨过和解线应触发 reconcile")
    _assert(store.get("spAff", "王刚")["status"] == "normal", "和解后应恢复 normal")

    # 一轮从正常直接跌穿断交线：cold 与 severed 同时跨过
    out = store.apply_delta("spAff", "李雷", -45, "严重背叛")
    _assert(
        out["events"] == ["cold", "severed"],
        f"一轮跌穿应同时产生 cold 与 severed（实际 {out['events']}）",
    )
    _assert(store.get("spAff", "李雷")["status"] == "broken", "应直接进入 broken")

    # 冷淡区回暖：安静恢复，不发消息（避免轰炸）
    store.apply_delta("spAff", "赵六", -22, "有点过分")
    _assert(store.get("spAff", "赵六")["status"] == "cold", "应进入 cold")
    out = store.apply_delta("spAff", "赵六", 10, "及时道歉")
    _assert(out["events"] == [], "回暖出冷淡区应安静恢复")
    _assert(store.get("spAff", "赵六")["status"] == "normal", "应恢复 normal")

    # 列表接口：按好感度升序，最紧张的排前面
    people = store.list_people("spAff")
    _assert(people[0]["person_name"] == "李雷", "好感度最低的人应排第一")


def test_proactive_outbox(tmp_dir: Path) -> None:
    """验证：主动消息发件箱的写入 / 拉取 / 确认闭环。"""
    from cognitive.storage.affection_store import ProactiveOutbox

    db = Database(tmp_dir / "test.db")
    db.ensure_space("spOB", "发件箱测试")
    outbox = ProactiveOutbox(db)

    m1 = outbox.add(
        "spOB",
        kind="severance",
        person_name="李雷",
        content="李雷，我想了很久。你一直这样对我，那就算了吧。",
        affection_value=8,
        topic_id="topic-x",
        session_id="ses-a",
    )
    m2 = outbox.add(
        "spOB",
        kind="reach_out",
        person_name="王刚",
        content="王刚，最近总觉得我们之间有点不对劲。",
        affection_value=28,
        topic_id="topic-y",
        session_id="ses-b",
    )

    # 待处理列表：旧 → 新，两条都在
    pending = outbox.list_messages("spOB", only_pending=True)
    _assert(len(pending) == 2, f"应有 2 条待处理消息（实际 {len(pending)}）")
    _assert(pending[0]["msg_id"] == m1["msg_id"], "应按时间升序排列")

    # 按会话过滤
    only_b = outbox.list_messages("spOB", session_id="ses-b", only_pending=True)
    _assert(len(only_b) == 1 and only_b[0]["person_name"] == "王刚", "应能按会话过滤")

    # 确认后离开待处理列表，但正文仍可查（审计不丢）
    _assert(outbox.ack("spOB", m1["msg_id"]) is True, "ack 应成功")
    pending = outbox.list_messages("spOB", only_pending=True)
    _assert(len(pending) == 1, "ack 后应只剩 1 条待处理")
    all_msgs = outbox.list_messages("spOB", only_pending=False)
    _assert(len(all_msgs) == 2, "已确认消息的正文应仍可查询")

    # 空间隔离
    db.ensure_space("spOB2", "另一个空间")
    _assert(outbox.list_messages("spOB2", only_pending=False) == [], "空间之间应隔离")

    # 重复 ack / 不存在的 id
    _assert(outbox.ack("spOB", m1["msg_id"]) is False, "重复 ack 应返回 False")


def test_proactive_template_fallback(tmp_dir: Path) -> None:
    """验证：模型不可用时，主动消息走模板降级——阈值行为不依赖模型。"""
    from cognitive.cognition.archiver import MemoryArchiver

    db = Database(tmp_dir / "test.db")
    db.ensure_space("spTpl", "模板降级测试")

    archiver = MemoryArchiver(db, client=None)
    outcome = {"before": 50.0, "after": 28.0, "status": "cold", "events": ["cold"]}

    for kind, expect in (
        ("reach_out", "不对劲"),
        ("severance", "算了吧"),
        ("reconcile", "重新开始"),
    ):
        text = archiver._compose_proactive_message(
            kind=kind, person="王刚", outcome=outcome, reason="测试"
        )
        _assert(expect in text, f"{kind} 模板降级文案应包含关键词「{expect}」")
        _assert("王刚" in text, f"{kind} 模板应填入收件人名")


def test_proactive_schedule(tmp_dir: Path) -> None:
    """验证：主动联系时间表的一次性语义（到点兑现 / 收到消息作废）。"""
    from cognitive.storage.affection_store import ProactiveScheduleStore

    db = Database(tmp_dir / "test.db")
    db.ensure_space("spSch", "排期测试")
    store = ProactiveScheduleStore(db)

    due = 1_800_000_000
    row = store.set_pending("spSch", "main", due, reason="聊得正热", source="llm")
    _assert(row["status"] == "pending" and row["due_ts"] == due, "应写入 pending 时间表")

    # due_ts 一起参与匹配：新排期覆盖旧表后，旧表不能兑现
    _assert(store.claim("spSch", "main", due + 1) is False, "due_ts 不符不能兑现")
    _assert(store.claim("spSch", "main", due) is True, "到期应能兑现")
    _assert(store.get("spSch", "main")["status"] == "fired", "兑现后状态应为 fired")
    _assert(store.claim("spSch", "main", due) is False, "同一张表不能兑现两次")

    # 收到用户消息 → 作废；作废后即使到点也不能再兑现
    store.set_pending("spSch", "main", due + 600, reason="过一会儿再找", source="llm")
    _assert(store.cancel("spSch", "main") is True, "pending 表应能作废")
    _assert(store.get("spSch", "main")["status"] == "cancelled", "作废后状态应为 cancelled")
    _assert(store.claim("spSch", "main", due + 600) is False, "作废的表不能兑现")
    _assert(store.cancel("spSch", "main") is False, "重复作废应返回 False")

    # 每轮回答后重新排期：新表覆盖旧表
    store.set_pending("spSch", "main", due + 1200, reason="再排一次")
    _assert(store.get("spSch", "main")["due_ts"] == due + 1200, "新排期应覆盖旧排期")
    _assert(store.get("spSch", "main")["status"] == "pending", "覆盖后应重新变回 pending")


def test_proactive_memory_pick(tmp_dir: Path) -> None:
    """验证：主动消息能从记忆库抽到内容；模型不可用时排期落在兜底区间。"""
    import random as _random

    from cognitive.proactive import ProactiveInitiator

    db = Database(tmp_dir / "test.db")
    db.ensure_space("spPick", "抽样测试")
    TopicStore(db).save_topic(
        Topic(
            topic_id="tpick1",
            space_id="spPick",
            title="宿舍养猫",
            abstract="室友一起养了只橘猫，叫布丁，晚上总踩键盘。",
            keywords=["猫", "室友"],
        )
    )

    initiator = ProactiveInitiator(db, client=None, rng=_random.Random(7))
    memory = initiator.choose_memory("spPick")
    _assert(memory is not None, "有主题记忆时应能抽到内容")
    _assert("布丁" in memory["text"], "抽到的应是主题摘要")

    text = initiator.compose(memory)
    _assert(text and "布丁" in text, "模板降级应带上记忆片段")

    decision = initiator.decide_delay_minutes("user: 晚安\nassistant: 晚安")
    _assert(decision["source"] == "fallback", "无模型时应走兜底排期")
    _assert(
        config.PROACTIVE_FALLBACK_MIN_MINUTES
        <= decision["minutes"]
        <= config.PROACTIVE_FALLBACK_MAX_MINUTES,
        "兜底排期应落在配置区间",
    )


def test_persona_assets(tmp_dir: Path) -> None:
    """验证：人格原始数据集中在 data/persona/，代码读取的就是这些文件。"""
    from cognitive.persona.assets import load_persona_data

    persona_dir = Path(config.PERSONA_DATA_DIR)
    _assert(persona_dir.is_dir(), "人格数据目录应存在")

    for name in ("meta.json", "profile.json", "kernel.json", "seed.json", "avatar.jpg"):
        _assert((persona_dir / name).is_file(), f"data/persona/ 缺少 {name}")

    assets = load_persona_data(persona_dir)
    _assert(
        assets["meta"]["name"] == config.PERSONA_NAME,
        "meta.name 应与 config.PERSONA_NAME 一致",
    )
    _assert(
        assets["profile"] == config.PERSONA_PROFILE,
        "config.PERSONA_PROFILE 应来自 profile.json",
    )
    _assert(
        assets["kernel"]["init"] == config.PERSONA_KERNEL_INIT,
        "config.PERSONA_KERNEL_INIT 应来自 kernel.json",
    )
    _assert(
        assets["kernel"]["description"] == config.PERSONA_PARAM_DESC,
        "config.PERSONA_PARAM_DESC 应来自 kernel.json",
    )
    _assert(
        set(config.PERSONA_KERNEL_INIT) == set(config.PERSONA_PARAM_DESC),
        "参数表与释义表应一一对应",
    )

    seed = assets["seed"]
    _assert(len(seed["entities"]) == 13, "前史实体数应为 13")
    _assert(len(seed["relations"]) == 24, "前史关系数应为 24")
    _assert(len(seed["stories"]) == 6, "成长往事应为 6 段")
    _assert(
        seed["profile_topic"]["id"] == "seed_profile",
        "人物档案主题 id 应为 seed_profile",
    )
    _assert(config.PERSONA_AVATAR_FILE.is_file(), "人格头像应存在")


def main() -> int:
    """执行全部冒烟测试。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="cognitive_test_"))
    try:
        # 向量索引与人格快照一律写进临时目录：测试不得污染 data/（真实人格库）。
        # 漏掉 SNAPSHOT_DIR 会让日结/大事用例在 data/persona_snapshots/ 下留下 sp8、sp9。
        config.VECTOR_DIR = tmp_dir / "vectors"
        config.VECTOR_DIR.mkdir(parents=True, exist_ok=True)
        config.SNAPSHOT_DIR = tmp_dir / "persona_snapshots"
        config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

        tests = [
            ("KG 冲突检测", test_kg_conflict),
            ("KG 权重加固", test_kg_boost),
            ("KG 加固(生产路径)", test_kg_boost_production_rid),
            ("向量检索", test_vector_search),
            ("改写式召回", test_vector_rewrite_recall),
            ("归并后召回过滤", test_router_consolidated_filter),
            ("人格参数演化", test_persona_delta),
            ("情景状态衰减", test_mood_decay),
            ("主题与会话", test_topic_and_session),
            ("检索循环有界", test_rag_loop_bounds),
            ("50轮短期窗口", test_history_window),
            ("日结归并", test_daily_consolidate),
            ("大事标记", test_big_event_marking),
            ("表结构迁移", test_schema_migration),
            ("人格基调抗漂移", test_persona_baseline),
            ("基调日结推进", test_persona_daily_promotion),
            ("曾被推翻展示", test_overturned_beliefs),
            ("大事判定收紧", test_big_event_gate),
            ("多值信念共存", test_kg_multi_valued),
            ("好感度状态机", test_affection_state_machine),
            ("主动消息发件箱", test_proactive_outbox),
            ("主动消息模板降级", test_proactive_template_fallback),
            ("主动联系时间表", test_proactive_schedule),
            ("主动消息记忆抽样", test_proactive_memory_pick),
            ("人格原始数据目录", test_persona_assets),
        ]

        failed = 0
        for name, func in tests:
            try:
                func(tmp_dir)
                print(f"[通过] {name}")
            except AssertionError as exc:
                failed += 1
                print(f"[失败] {name}：{exc}")
            except Exception as exc:
                failed += 1
                print(f"[异常] {name}：{type(exc).__name__}: {exc}")

        print(f"\n共 {len(tests)} 项，失败 {failed} 项")
        return 0 if failed == 0 else 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================
# P0 / P1 增强测试（轮次感知归档、信念溯源、用户纠正）
# ============================================================


def _make_context(tmp_dir: Path, messages: list) -> SessionContext:
    """构造一个会话；消息统一转成 Message，避免用 dict 冒充实体的模型对象。"""
    from cognitive.models import Message

    db = Database(tmp_dir / "p0.db")
    db.ensure_space("sp0", "测试")
    items = [
        Message(role=item["role"], content=item["content"])
        if isinstance(item, dict)
        else item
        for item in messages
    ]
    return SessionContext(session_id="s0", space_id="sp0", messages=items)


def test_p0_turn_aware_transcript(tmp_dir: Path) -> None:
    """验证：多气泡回复能正确回溯本轮文本，用户气泡不被误标为 assistant。"""
    from cognitive.cognition.archiver import MemoryArchiver

    archiver = MemoryArchiver(Database(tmp_dir / "p0.db"), client=None)
    context = _make_context(tmp_dir, [
        {"role": "user", "content": "今天想和她说点什么"},
        {"role": "assistant", "content": "我大概会想一下这件事。"},
        {"role": "assistant", "content": "我也觉得这是值得聊的方向。"},
    ])

    report = archiver.archive(
        context,
        user_message="今天想和她说点什么",
        answer_parts=["我大概会想一下这件事。", "我也觉得这是值得聊的方向。"],
        turn_id="turn_20260101",
    )
    _assert(report["ok"] is True, "归档应成功")
    _assert(report["duplicate"] is False, "首次归档不应标记重复")

    text = archiver._build_transcript(
        context,
        user_message="今天想和她说点什么",
        answer_parts=["我大概会想一下这件事。", "我也觉得这是值得聊的方向。"],
        turn_id="turn_20260101",
    )
    expected = (
        "user: 今天想和她说点什么\n"
        "assistant: 我大概会想一下这件事。\n"
        "assistant: 我也觉得这是值得聊的方向。"
    )
    _assert(text == expected, f"位置回溯应还原本轮气泡，实际：{text!r}")

    fallback = archiver._build_transcript(context, turn_id="turn_20260101")
    _assert(
        fallback.startswith(
            "user: 今天想和她说点什么\nassistant: 我大概会想一下这件事。"
        ),
        "位置兜底也应能回溯本轮气泡",
    )

    mixed = archiver._build_transcript(context, user_message="", answer_parts=["a", "b"])
    _assert(
        "assistant: a" in mixed and "assistant: b" in mixed,
        "answer_parts 应原样注入为回复气泡",
    )


def test_p0_archiver_idempotent(tmp_dir: Path) -> None:
    """验证：同一轮重复归档只沉淀一次（幂等）。"""
    from cognitive.cognition.archiver import MemoryArchiver

    archiver = MemoryArchiver(Database(tmp_dir / "p0db.db"), client=None)
    context = _make_context(tmp_dir, [
        {"role": "user", "content": "先归档一次"},
        {"role": "assistant", "content": "第一版回答。"},
    ])

    report = archiver.archive(
        context,
        user_message="先归档一次",
        answer_parts=["第一版回答。"],
        turn_id="idem_1",
    )
    _assert(report["ok"] is True, "首次归档应成功")
    _assert(report["duplicate"] is False, "首次不应标记重复")

    again = archiver.archive(
        context,
        user_message="先归档一次",
        answer_parts=["第一版回答。"],
        turn_id="idem_1",
    )
    _assert(again["duplicate"] is True, "重复归档应标记为重复")
    _assert(again["skipped"] is True, "重复归档应标记为已跳过")
    _assert(again["ok"] is True, "幂等返回仍应是成功")

    other = archiver.archive(
        context,
        user_message="换一轮说",
        answer_parts=["换一轮回答。"],
        turn_id="idem_2",
    )
    _assert(other["ok"] is True, "换一轮应能沉淀")
    _assert(other["topic_id"] != report["topic_id"], "不同轮次应得到不同主题")


def test_p0_judge_uses_explicit_message(tmp_dir: Path) -> None:
    """验证：大事判定优先用显式用户消息，而非回溯最后一条 user。"""
    from cognitive.cognition.archiver import judge_big_event

    tech_relations = [
        {
            "head": "Topic-Centric",
            "rel": "区别于",
            "tail": "Graph RAG",
            "importance": 0.92,
        }
    ]
    self_relations = [
        {"head": "江妍", "rel": "喜欢", "tail": "明日方舟", "importance": 0.92}
    ]

    # 技术问答：无长期指令词、与本人无关 → 不应判大事
    is_big, reason = judge_big_event(tech_relations, "帮我讲讲这两种做法的区别")
    _assert(is_big is False, "技术问答不应判为大事")

    # 长期指令词命中 → 大事
    is_big, reason = judge_big_event(
        tech_relations, "从今天起，家里的事别往外讲", user_text=""
    )
    _assert(is_big is True, "长期指令词应判大事")

    # 高重要度且涉及本人 → 判大事
    is_big, reason = judge_big_event(
        self_relations, "你是不是不高兴被这么说", user_text="x"
    )
    _assert(is_big is True, "涉及本人的高价值信念应判大事")


def test_p1_belief_provenance(tmp_dir: Path) -> None:
    """验证：信念带溯源字段，支持读取、确认、纠正与审计。"""
    from cognitive.storage.kg_store import KGStore

    db = Database(tmp_dir / "p1.db")
    db.ensure_space("sp1", "测试")
    kg = KGStore(db)

    kg.upsert_entity(
        KGEntity(eid="e1", etype="Belief", name="账号", space_id="sp1", attrs={})
    )
    kg.upsert_relation(
        KGRelation(
            rid="r1",
            space_id="sp1",
            head_name="账号",
            rel="喜欢",
            tail_name="明日方舟",
            confidence=0.7,
            importance=0.9,
            source_turn_id="turn_9",
            source_type="user_stated",
            valid_from=100,
            valid_to=1000,
            user_confirmed=False,
        )
    )

    rows = kg.list_beliefs("sp1")
    _assert(len(rows) == 1, "应能列出该信念")
    row = rows[0]
    _assert(row["source_type"] == "user_stated", "溯源类型应为 user_stated")
    _assert(row["source_turn_id"] == "turn_9", "应记录来源轮次")
    _assert(row["valid_to"] == 1000, "应记录生效/失效时间")
    _assert(row["user_confirmed"] is False, "首次确认应为未确认")

    by_status = kg.list_beliefs("sp1", status="active")
    _assert(len(by_status) == 1 and by_status[0]["rid"] == "r1", "按状态过滤应命中")

    result = kg.update_belief(
        "sp1", "r1", status="candidate", user_confirmed=True,
        source_turn_id="turn_9", source_type="user_stated",
    )
    _assert(result["updated"] is True, "应成功更新信念")
    _assert(kg.get_belief("sp1", "r1")["status"] == "candidate", "状态应更新")
    _assert(kg.get_belief("sp1", "r1")["user_confirmed"] is True, "确认状态应落库")

    for action, note in (("confirm", "用户确认"), ("correct", "用户纠正")):
        ledger = kg._append_correction("sp1", action, note)
        _assert(len(ledger) == 1, f"{action} 应写入审计记录")
    _assert(any("clr" in r["correction_id"] for r in ledger), "审计记录应带 id")

    audit = db.conn().execute(
        "select action, note from belief_correction where space_id = 'sp1' order by create_ts"
    ).fetchall()
    _assert(
        {(r["action"], r["note"]) for r in audit}
        == {("confirm", "用户确认"), ("correct", "用户纠正")},
        f"审计表内容不符：{[tuple(r) for r in audit]}",
    )


def test_p1_user_correction_api_shape(tmp_dir: Path) -> None:
    """验证：API 层能为信念提供确认/纠正/否决三类操作。"""
    from cognitive.storage.kg_store import KGStore

    db = Database(tmp_dir / "p1api.db")
    db.ensure_space("sp1api", "测试")
    kg = KGStore(db)
    kg.upsert_relation(
        KGRelation(rid="r9", space_id="sp1api", head_name="用户", rel="prefer", tail_name="简洁",
                   confidence=0.6, importance=0.5)
    )
    _assert(kg.confirm_belief("sp1api", "r9") is True, "确认应成功")
    _assert(kg.correct_belief("sp1api", "r9") is True, "纠正应成功")
    _assert(kg.reject_belief("sp1api", "r9") is True, "否决应成功")


def test_p2_persona_freeze_reset(tmp_dir: Path) -> None:
    """验证：人格可冻结、可回退到基线，记忆不被牵连。"""
    db = Database(tmp_dir / "p2.db")
    db.ensure_space("sp2p", "测试")
    kernel = PersonaKernel(db, "sp2p")

    _assert(kernel.is_frozen() is False, "默认不应冻结")
    kernel.set_frozen(True)
    _assert(kernel.is_frozen() is True, "冻结应落库")
    kernel.set_frozen(False)
    _assert(kernel.is_frozen() is False, "解冻应落库")

    # 演化一次，再回退
    kernel.apply_deltas({"prefer_decompose": 0.2})
    changed = kernel.get_params()["prefer_decompose"]
    _assert(
        abs(changed - config.PERSONA_KERNEL_INIT["prefer_decompose"]) > 1e-6,
        "参数应已发生变化",
    )
    result = kernel.reset_to_baseline()
    _assert(
        abs(
            kernel.get_params()["prefer_decompose"]
            - config.PERSONA_KERNEL_INIT["prefer_decompose"]
        )
        < 1e-6,
        "回退后参数应等于基线",
    )
    _assert(result["after"]["prefer_decompose"] == result["baseline"]["prefer_decompose"],
            "回退摘要应与基线一致")


def test_p2_proactive_policy(tmp_dir: Path) -> None:
    """验证：免打扰 / 每日上限 / 冷却 / 跳过本轮 都按用户偏好生效。"""
    from cognitive.storage.affection_store import (
        ProactiveOutbox,
        ProactivePolicyStore,
    )

    db = Database(tmp_dir / "p2policy.db")
    db.ensure_space("sp2policy", "测试")
    store = ProactivePolicyStore(db)
    outbox = ProactiveOutbox(db)

    policy = store.get("sp2policy")
    _assert(policy["daily_cap"] >= 1, "默认应有每日上限")

    # 冷却：刚发过一条 → 阻止
    outbox.add("sp2policy", "initiative", "你", "在吗", session_id="main")
    _assert(store.block_reason("sp2policy") == "cooldown", "刚发送应处于冷却")

    # 关闭冷却与上限后可通过
    store.update("sp2policy", cooldown_minutes=0, daily_cap=0, dnd_start=0, dnd_end=0)
    _assert(store.block_reason("sp2policy") == "", "无关卡时应允许发送")

    # 免打扰：23 点开始、8 点结束，凌晨 3 点应命中
    store.update("sp2policy", dnd_start=23, dnd_end=8)
    midnight = int(
        __import__("time").mktime(
            (2026, 1, 1, 3, 0, 0, 0, 0, -1)
        )
    )
    _assert(
        ProactivePolicyStore.in_dnd(store.get("sp2policy"), midnight) is True,
        "凌晨 3 点应在免打扰时段",
    )
    noon = int(
        __import__("time").mktime(
            (2026, 1, 1, 12, 0, 0, 0, 0, -1)
        )
    )
    _assert(
        ProactivePolicyStore.in_dnd(store.get("sp2policy"), noon) is False,
        "中午不应在免打扰时段",
    )

    # 跳过本轮：标记 → 阻止 → 消费一次后恢复
    store.update("sp2policy", skip_next=True, dnd_start=0, dnd_end=0)
    _assert(store.block_reason("sp2policy") == "skip_next", "应阻止一次")
    _assert(store.consume_skip("sp2policy") is True, "应消费掉跳过标记")
    _assert(store.block_reason("sp2policy") == "", "消费后应恢复")


def main() -> int:
    """执行全部冒烟测试。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="cognitive_test_"))
    try:
        # 向量索引与人格快照一律写进临时目录：测试不得污染 data/（真实人格库）。
        config.VECTOR_DIR = tmp_dir / "vectors"
        config.VECTOR_DIR.mkdir(parents=True, exist_ok=True)
        config.SNAPSHOT_DIR = tmp_dir / "persona_snapshots"
        config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

        tests = [
            ("KG 冲突检测", test_kg_conflict),
            ("KG 权重加固", test_kg_boost),
            ("KG 加固(生产路径)", test_kg_boost_production_rid),
            ("向量检索", test_vector_search),
            ("改写式召回", test_vector_rewrite_recall),
            ("归并后召回过滤", test_router_consolidated_filter),
            ("人格参数演化", test_persona_delta),
            ("情景状态衰减", test_mood_decay),
            ("主题与会话", test_topic_and_session),
            ("检索循环有界", test_rag_loop_bounds),
            ("50轮短期窗口", test_history_window),
            ("日结归并", test_daily_consolidate),
            ("大事标记", test_big_event_marking),
            ("表结构迁移", test_schema_migration),
            ("人格基调抗漂移", test_persona_baseline),
            ("基调日结推进", test_persona_daily_promotion),
            ("曾被推翻展示", test_overturned_beliefs),
            ("大事判定收紧", test_big_event_gate),
            ("多值信念共存", test_kg_multi_valued),
            ("好感度状态机", test_affection_state_machine),
            ("主动消息发件箱", test_proactive_outbox),
            ("主动消息模板降级", test_proactive_template_fallback),
            ("主动联系时间表", test_proactive_schedule),
            ("主动消息记忆抽样", test_proactive_memory_pick),
            ("人格原始数据目录", test_persona_assets),
            ("P0 轮次感知归档", test_p0_turn_aware_transcript),
            ("P0 归档幂等", test_p0_archiver_idempotent),
            ("P0 大事判定显式消息", test_p0_judge_uses_explicit_message),
            ("P1 信念溯源", test_p1_belief_provenance),
            ("P1 用户纠正", test_p1_user_correction_api_shape),
            ("P2 人格冻结与回退", test_p2_persona_freeze_reset),
            ("P2 主动联系偏好", test_p2_proactive_policy),
        ]

        failed = 0
        for name, func in tests:
            try:
                func(tmp_dir)
                print(f"[通过] {name}")
            except AssertionError as exc:
                failed += 1
                print(f"[失败] {name}：{exc}")
            except Exception as exc:
                failed += 1
                print(f"[异常] {name}：{type(exc).__name__}: {exc}")

        print(f"\n共 {len(tests)} 项，失败 {failed} 项")
        return 0 if failed == 0 else 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
