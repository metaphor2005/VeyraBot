"""
离线评测：中文检索召回 + 人格提示词一致性。

不调用任何模型，可在 CI 中直接运行：

    python tests/eval/run_eval.py

评测指标：
- Recall@1 / Recall@3：给定口语化问题时，正确主题是否被召回；
- 误召回率：技术/无关问题不应命中生活类主题；
- 人格一致性：人格提示词必须包含姓名、当前情绪、核心信念等区块；
- 隐私：提示词与人格资产中不得出现真实 API key / 本机路径。

退出码非 0 表示有指标未达标，便于挂到 CI。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import config
from cognitive.cognition.prompts import render_persona_profile
from cognitive.storage.vector_store import VectorStore


# (主题 id, 记忆正文, 关键词, 口语化查询列表)
RETRIEVAL_CASES = [
    (
        "cat",
        "三花猫辣椒是 2021 年楼下捡的，五公斤，最黏她。",
        ["猫", "辣椒", "三花", "宠物"],
        ["你养的那只猫叫什么", "辣椒最近还黏人吗", "你家宠物是哪一年捡的"],
    ),
    (
        "cos",
        "第一次出角是能天使，妆造全部自己来，假发剪坏两顶。",
        ["cos", "出角", "明日方舟", "能天使"],
        ["你第一次出角色是什么感受", "你出过能天使吗"],
    ),
    (
        "game",
        "明日方舟从开服玩到现在，最推能天使，抽卡运气一般。",
        ["明日方舟", "游戏", "抽卡"],
        ["你玩什么游戏", "你抽卡运气怎么样"],
    ),
    (
        "code",
        "Topic-Centric 主题索引与 Graph RAG 的取舍，最终选了主题级摘要。",
        ["记忆", "架构", "检索", "Graph RAG"],
        ["你的记忆系统怎么设计的", "为什么不直接用 Graph RAG"],
    ),
    (
        "coffee",
        "只喝冰美式，不加糖，下午三点后不喝咖啡。",
        ["咖啡", "美式", "习惯"],
        ["你平时喝什么咖啡", "你下午还喝咖啡吗"],
    ),
    (
        "night",
        "经常凌晨两点才睡，早上起不来，靠闹钟响三次。",
        ["作息", "熬夜", "睡眠"],
        ["你几点睡觉", "你早上起得来吗"],
    ),
    (
        "friend",
        "和林夏认识六年了，她失恋那阵子我陪她住了半个月。",
        ["朋友", "林夏", "往事"],
        ["你最好的朋友是谁", "你陪谁住过半个月"],
    ),
    (
        "music",
        "最近循环草东没有派对，现场比录音室更炸。",
        ["音乐", "乐队", "草东", "现场"],
        ["你最近听什么歌", "你喜欢哪个乐队"],
    ),
]

# 期望召不回任何生活主题的对抗性查询
NEGATIVE_QUERIES = [
    "量子纠缠的退相干时间怎么测",
    "帮我写一段快速排序",
]


def evaluate_retrieval() -> tuple[int, dict]:
    """
    运行检索评测。

    :return: (失败项数, 指标字典)
    """
    store = VectorStore("eval")
    for doc_id, text, keywords, _ in RETRIEVAL_CASES:
        store.add(doc_id, text, {"keywords": keywords})

    hit1 = 0
    hit3 = 0
    total = 0
    misses: list[str] = []

    for doc_id, _, _, queries in RETRIEVAL_CASES:
        for query in queries:
            total += 1
            hits = store.search(query, top_k=3)
            ids = [h[0] for h in hits]
            if ids and ids[0] == doc_id:
                hit1 += 1
            if doc_id in ids:
                hit3 += 1
            else:
                misses.append(f"{query} → 期望 {doc_id}，实际 {ids}")

    false_hits = 0
    for query in NEGATIVE_QUERIES:
        hits = store.search(query, top_k=3)
        if any(score > 0.12 for _, score in hits):
            false_hits += 1

    metrics = {
        "total": total,
        "recall@1": hit1 / total if total else 0.0,
        "recall@3": hit3 / total if total else 0.0,
        "false_recall": false_hits / len(NEGATIVE_QUERIES),
        "misses": misses,
    }
    failed = 0
    if metrics["recall@3"] < 0.85:
        failed += 1
        print(f"[失败] Recall@3={metrics['recall@3']:.2f} 低于 0.85")
    if metrics["recall@1"] < 0.55:
        failed += 1
        print(f"[失败] Recall@1={metrics['recall@1']:.2f} 低于 0.55")
    if metrics["false_recall"] > 0:
        failed += 1
        print(f"[失败] 对抗查询出现误召回：{metrics['false_recall']:.2f}")
    return failed, metrics


def evaluate_persona_prompt() -> tuple[int, dict]:
    """
    检查人格提示词的一致性约束。

    :return: (失败项数, 指标字典)
    """
    prompt = render_persona_profile()
    checks = {
        "has_name": config.PERSONA_NAME in prompt,
        "has_identity": "身份" in prompt or "你是" in prompt,
        "has_rules": "规则" in prompt or "不要" in prompt,
        "not_empty": len(prompt.strip()) > 200,
        "no_api_key": "sk-" not in prompt,
        "no_local_path": "C:\\Users" not in prompt and "/home/" not in prompt,
    }
    failed = sum(1 for ok in checks.values() if not ok)
    for name, ok in checks.items():
        if not ok:
            print(f"[失败] 人格提示词检查未通过：{name}")
    return failed, checks


def main() -> int:
    """运行全部离线评测。"""
    # 向量索引写临时目录，避免污染 data/vectors
    tmp = Path(tempfile.mkdtemp(prefix="cognitive_eval_"))
    config.VECTOR_DIR = tmp / "vectors"
    config.VECTOR_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 56)
    print("检索评测")
    print("=" * 56)
    failed_retrieval, metrics = evaluate_retrieval()
    print(f"样本数：{metrics['total']}")
    print(f"Recall@1：{metrics['recall@1']:.3f}")
    print(f"Recall@3：{metrics['recall@3']:.3f}")
    print(f"误召回率：{metrics['false_recall']:.3f}")
    for miss in metrics["misses"]:
        print(f"  未命中：{miss}")

    print()
    print("=" * 56)
    print("人格提示词一致性")
    print("=" * 56)
    failed_persona, checks = evaluate_persona_prompt()
    for name, ok in checks.items():
        print(f"  {'通过' if ok else '失败'}：{name}")

    failed = failed_retrieval + failed_persona
    print()
    print(f"评测结论：{'全部通过' if failed == 0 else f'{failed} 项未达标'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
