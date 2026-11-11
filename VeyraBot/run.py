"""
交互式命令行入口。

用法：
    python run.py                          # 默认空间 default、会话 cli
    python run.py --space demo --session s1 --debug
    python run.py --state                  # 只打印当前人格状态后退出

支持斜杠命令：
    /state   打印当前人格快照、内核参数、情景状态
    /history 打印人格变迁史
    /memory  打印记忆库总览
    /debug   切换链路调试输出
    /exit    退出
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

import config
from cognitive.engine import CognitiveAgent


def _print_persona(agent: CognitiveAgent, space_id: str) -> None:
    """打印当前人格状态。"""
    state = agent.persona_state(space_id)
    print("\n=== 人格状态 ===")
    print(f"快照版本：v{state['version']}")
    print(f"自我认知：{state['self_view']}")
    print(f"叙事：{state['narrative']}")
    print(f"情景状态：{json.dumps(state['moods'], ensure_ascii=False)}")

    print("内核参数：")
    for param, value in state["kernel"].items():
        print(f"  - {param}: {value:.2f}")

    if state["core_beliefs"]:
        print("核心信念：")
        for belief in state["core_beliefs"]:
            print(f"  - {belief['statement']}（重要性 {belief['importance']:.2f}）")

    print(f"变迁记录数：{state['changelog_count']}")
    print(f"快照文件版本：{state['snapshot_files']}\n")


def _print_archive(debug: Dict[str, Any]) -> None:
    """打印归档结果摘要。"""
    archive = debug.get("archive", {}) or {}

    if not archive.get("ok"):
        print(f"[归档] 跳过：{archive.get('error', '未知原因')}")
        return

    print(
        f"[归档] 主题「{archive.get('topic_title')}」 "
        f"实体 {archive.get('entities')} / 关系 {archive.get('relations')} / "
        f"冲突 {archive.get('conflicts')} / 经验 {archive.get('experiences')}"
    )

    changes = archive.get("param_changes") or []
    if changes:
        detail = "，".join(f"{item['param']} {item['old']}->{item['new']}" for item in changes)
        print(f"[人格] 参数变化：{detail}")

    if archive.get("substantive"):
        print(
            f"[人格] 发生实质性跃迁 → 已生成快照 v{archive.get('snapshot_version')}，"
            f"新增变迁日志 {archive.get('changelog_entries')} 条"
        )
    else:
        print(f"[人格] 无实质跃迁，新增变迁日志 {archive.get('changelog_entries')} 条")


def _print_debug(debug: Dict[str, Any]) -> None:
    """打印完整链路信息。"""
    print("\n--- 链路 ---")
    print(f"压缩：{debug.get('compress')}")
    print(f"记忆：{debug.get('memory')}")
    print(f"判决：{debug.get('judge')}")
    print(f"RAG：{'已使用' if debug.get('rag_used') else '未使用'}")
    print(f"人格版本：v{debug.get('persona_version')}")
    print(f"耗时：{debug.get('elapsed_ms')} ms\n")


def main() -> int:
    """程序入口。"""
    parser = argparse.ArgumentParser(description="具备演化人格的认知主体 CLI")
    parser.add_argument("--space", default="default", help="记忆空间 id")
    parser.add_argument("--session", default="cli", help="会话 id")
    parser.add_argument("--debug", action="store_true", help="是否输出链路调试信息")
    parser.add_argument("--state", action="store_true", help="只打印人格状态后退出")
    args = parser.parse_args()

    try:
        agent = CognitiveAgent()
        agent.ensure_space(args.space, args.space)
    except Exception as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1

    if args.state:
        _print_persona(agent, args.space)
        return 0

    show_debug = args.debug
    print(f"空间：{args.space}　会话：{args.session}　模型：{config.MODEL_NAME}")

    # 空间里没有前史时提示一次：否则她读得到档案，却想不起往事
    try:
        from cognitive.persona.seed import persona_seed_overview

        if not persona_seed_overview(agent.db, args.space)["seeded"]:
            print(
                f"[提示] 空间 {args.space} 还没有注入前史（成长往事 / 关系网 / 人物档案）。"
                f"需要她「记得过去」可执行： python maintenance.py --seed-persona --space {args.space}"
            )
    except Exception:
        pass

    print("输入内容开始对话；/state /history /memory /debug /exit 为命令。\n")

    while True:
        try:
            user_input = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return 0

        if not user_input:
            continue

        # ---- 斜杠命令 ----
        if user_input == "/exit":
            print("已退出。")
            return 0
        if user_input == "/state":
            _print_persona(agent, args.space)
            continue
        if user_input == "/history":
            print("\n=== 人格变迁史 ===")
            print(agent.persona_history(args.space, limit=20))
            print()
            continue
        if user_input == "/memory":
            print("\n=== 记忆总览 ===")
            print(json.dumps(agent.memory_overview(args.space), ensure_ascii=False, indent=2))
            print()
            continue
        if user_input == "/debug":
            show_debug = not show_debug
            print(f"调试输出：{'开' if show_debug else '关'}")
            continue

        # ---- 正常对话 ----
        result = agent.chat(user_input, session_id=args.session, space_id=args.space)
        print(f"\n{config.PERSONA_NAME} > {result['answer']}\n")
        _print_archive(result["debug"])
        if show_debug:
            _print_debug(result["debug"])
        print()


if __name__ == "__main__":
    sys.exit(main())
