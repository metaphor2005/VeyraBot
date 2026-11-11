"""
演示脚本：一次性跑完一条完整链路，验证各模块是否真的工作。

演示内容（6 轮，覆盖全部核心能力）：
1. 自我介绍（KG 实体与属性写入，精确查询可召回）
2. 聊 cos 与游戏（主题记忆 + 经验沉淀）
3. 被说中性格（人格日内漂移，受基调约束，不产生新版本）
4. 自我溯源（读取变迁史回答"你为什么这样"）
5. 课程项目查资料（触发有界检索循环，末轮强制收束）
6. 长期指令（大事判定 + 事件级沉淀）

用法：
    python demo.py              # 使用独立演示空间 demo_space，可重复运行（数据累积）
    python demo.py --fresh      # 清空演示空间后重跑
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import List

import config
from cognitive.engine import CognitiveAgent


# 演示对话脚本（对话者是朋友，被聊的对象是江妍本人）
DEMO_DIALOGUE: List[str] = [
    "小苹果，先正经介绍下你自己吧，我想记住你的事：叫什么、多大、在哪上学、老家哪儿的。",
    "你还记得第一次出 cos 出的谁吗？那时候是什么感觉？",
    "你老说自己只是爱玩，其实你挺在意别人怎么看你的吧？被当成只会笑的那种女生，你会怎么反应？",
    "你为什么觉得自己不只是个「可爱的女孩子」？把原因和当时的经过说清楚。",
    "我在写课程项目，要讲清楚 topic-centric 记忆架构和 Graph RAG 的区别，你帮我查资料说明一下。",
    "从今天起，家里的事别随便往外讲，只有我主动问你才说。这条定下来，以后都按这个执行。",
]


def _clean_space(space_id: str, session_id: str = "") -> None:
    """
    清理演示空间的所有数据（文件 + 数据库记录）。

    :param space_id: 空间 id
    :param session_id: 会话 id（该会话可能属于旧空间，需一并清掉）
    """
    from cognitive.storage.database import Database

    db = Database()
    connection = db.conn()
    for table in ("kg_entity", "kg_relation", "topic", "experience", "persona_kernel",
                  "persona_mood", "persona_meta", "persona_changelog", "consolidation_log"):
        connection.execute(f"delete from {table} where space_id = ?", (space_id,))
    connection.execute("delete from session_store where space_id = ?", (space_id,))
    if session_id:
        connection.execute("delete from session_store where session_id = ?", (session_id,))
    connection.commit()

    snapshot_dir = Path(config.SNAPSHOT_DIR) / space_id
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)

    vector_file = Path(config.VECTOR_DIR) / f"{space_id}.json"
    if vector_file.exists():
        vector_file.unlink()


def main() -> int:
    """演示主流程。"""
    parser = argparse.ArgumentParser(description="认知主体全链路演示")
    parser.add_argument("--fresh", action="store_true", help="清空演示空间后重跑")
    parser.add_argument("--space", default="demo_space", help="演示空间 id")
    parser.add_argument("--rounds", type=int, default=len(DEMO_DIALOGUE), help="只跑前 N 轮（调试用）")
    parser.add_argument(
        "--with-past",
        action="store_true",
        help="先注入江妍的既有记忆（成长往事 / 关系网 / 人物档案），让她一开口就有前史",
    )
    args = parser.parse_args()

    session_id = "demo_session"

    if args.fresh:
        _clean_space(args.space, session_id=session_id)
        print(f"[准备] 已清空演示空间 {args.space}\n")

    agent = CognitiveAgent()
    agent.ensure_space(args.space, "演示空间")

    # 注入既有记忆：让她不是从空白开始，而是本来就有 18 年可以回想
    if args.with_past:
        from cognitive.persona.seed import seed_emotions, seed_persona

        stats = seed_persona(agent.db, args.space)
        emotions = seed_emotions(agent.db, args.space)
        print(
            f"[前史] 已注入：往事/档案 {stats['topics']} 条（跳过 {stats['skipped_topics']} 条）"
            f" / 关系 {stats['relations']} 条 / 经验 {stats['experiences']} 条"
            f" / 情绪片段 {emotions} 条 / 向量索引 {stats['indexed']} 条\n"
        )


    for index, message in enumerate(DEMO_DIALOGUE[: max(1, args.rounds)], start=1):
        print("=" * 70)
        print(f"第 {index} 轮")
        print(f"用户 > {message}")
        print("-" * 70)

        result = agent.chat(message, session_id=session_id, space_id=args.space)
        print(f"{config.PERSONA_NAME} > {result['answer']}\n")
        debug = result["debug"]
        archive = debug.get("archive", {}) or {}

        print(
            f"[记忆] KG事实 {debug['memory'].get('kg_facts')} 条 / "
            f"曾被推翻 {debug['memory'].get('overturned')} 条 / "
            f"主题 {debug['memory'].get('topics')} 个 / "
            f"经验 {debug['memory'].get('experiences')} 条"
        )
        print(f"[判决] need_rag={debug['judge'].get('need_rag')} 理由={debug['judge'].get('reason')}")

        # 检索循环状态：第几轮 / 上限 / 为何收束
        rag_loop = debug.get("rag_loop", {}) or {}
        loop_note = (
            f"{rag_loop.get('rounds')} 轮判断 / 上限 {rag_loop.get('max_rounds')}，"
            f"命中 {rag_loop.get('hits')} 条，收束：{rag_loop.get('stopped_by')}"
        )
        if rag_loop.get("queries"):
            loop_note += "，检索词：" + " / ".join(rag_loop["queries"])
        print(f"[RAG] {'已启用' if debug['rag_used'] else '未启用'}（{loop_note}）")

        # 每轮轨迹：第几次判断、查了什么、新增几条
        for item in rag_loop.get("trace", []):
            mark = "末轮" if item.get("is_final") else f"第{item.get('round')}轮"
            hits_text = f"新增 {item.get('new_hits')} 条" if item.get("need_rag") else "未检索"
            print(
                f"[RAG]   {mark} need_rag={item.get('need_rag')} "
                f"查询「{item.get('query') or '—'}」{hits_text} —— {item.get('reason')}"
            )

        if archive.get("skipped"):
            print(f"[归档] 未执行：{archive.get('error')}")
        elif archive.get("ok"):
            prefix = "降级归档" if archive.get("degraded") else "归档"
            print(
                f"[{prefix}] 主题「{archive.get('topic_title')}」 "
                f"关系 {archive.get('relations')} / 冲突 {archive.get('conflicts')} / "
                f"经验 {archive.get('experiences')} / 变迁日志 {archive.get('changelog_entries')}"
            )
            # 人格：只移动"当日状态"，版本由日结推进（这里不再出现跃迁）
            changes = archive.get("param_changes") or []
            if changes:
                detail = "，".join(f"{item['param']} {item['old']}->{item['new']}" for item in changes)
                print(f"[人格] 日内漂移（受基调约束，不产生新版本）：{detail}")
            else:
                print("[人格] 本轮未触发人格更新（准入守卫拦下或增量过小）")
            if archive.get("self_view_live"):
                print(f"[人格] 自我认知即时更新（日结时才固化）：{archive.get('self_view_live')}")

            # 大事判定与事件级沉淀（不等日结）
            if archive.get("is_big_event"):
                print(f"[大事] 判定命中（{archive.get('big_event_reason')}）")
            event = debug.get("big_event") or {}
            if event:
                if event.get("ok"):
                    print(
                        f"[大事] 已沉淀「{event.get('title')}」"
                        f"（触发：{event.get('trigger') or '—'}，影响：{event.get('impact') or '未给出'}）"
                    )
                elif event.get("error"):
                    print(f"[大事] 沉淀失败：{event.get('error')}")
                else:
                    print(f"[大事] 未沉淀：{event.get('reason')}")
        else:
            print(f"[归档] 失败：{archive.get('error')}")

        print(f"[耗时] {debug.get('elapsed_ms')} ms\n")

        # 轮间短暂停顿：连续长请求容易触发网关侧的连接重置
        if index < len(DEMO_DIALOGUE[: max(1, args.rounds)]):
            time.sleep(1.5)

    # ---------- 收尾：展示最终状态 ----------
    print("=" * 70)
    print("最终人格状态")
    print("=" * 70)
    state = agent.persona_state(args.space)
    print(f"快照版本：v{state['version']}")
    print(f"自我认知：{state['self_view']}")
    print(f"叙事：{state['narrative']}\n")
    print("内核参数（当前状态 / 人格基调）：")
    for param, value in state["kernel"].items():
        base = state.get("baseline", {}).get(param, value)
        print(f"  - {param}: {value:.2f}（基调 {base:.2f}，日内偏移 {value - base:+.3f}）")
    pending = {key: round(val, 3) for key, val in (state.get("pending_drift") or {}).items()}
    print(f"\n待日结结算的偏移：{json.dumps(pending, ensure_ascii=False)}")
    print(f"\n情景状态：{json.dumps(state['moods'], ensure_ascii=False)}")
    print(f"快照文件：{state['snapshot_files']}")

    print("\n人格变迁史：")
    print(agent.persona_history(args.space, limit=15))

    print("\n记忆库总览：")
    print(json.dumps(agent.memory_overview(args.space), ensure_ascii=False, indent=2))

    # 长期运行节奏：主题分类 / 待归并积压 / 上次日结 / 最近巩固
    long_run = agent.long_run_state(args.space)
    print("\n长期运行状态：")
    print(json.dumps(long_run, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
