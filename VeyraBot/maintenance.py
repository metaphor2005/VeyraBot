"""
长期维护入口（供计划任务调用）。

能力：
1. --daily：日结巩固（每天一次，把当天往事归并成主线）
2. --stats：随时查看记忆规模、待归并积压、人格版本
3. --prune-snapshots：快照冷存（只保留最近 N 个版本在当前目录）
4. --persona-info：打印人格原始数据与运行时数据的存放位置（升级/迁移前先看这个）

用法：
    python maintenance.py --daily
    python maintenance.py --daily --space me
    python maintenance.py --stats
    python maintenance.py --prune-snapshots --keep 30
    python maintenance.py --persona-info

配合 Windows 计划任务（见 install_schedule.ps1）每天固定跑一次 --daily。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import List

import config
from cognitive.cognition.consolidator import Consolidator
from cognitive.persona.changelog import PersonaChangelogStore
from cognitive.persona.kernel import PersonaKernel
from cognitive.persona.snapshot import PersonaSnapshotStore
from cognitive.storage.database import Database
from cognitive.storage.kg_store import KGStore
from cognitive.storage.topic_store import TopicStore


class _Tee:
    """把输出同时写到控制台与日志文件（计划任务无法用 shell 重定向）。"""

    def __init__(self, *streams) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def list_spaces(db: Database) -> List[str]:
    """
    列出所有已注册的记忆空间。

    :param db: 数据库对象
    :return: 空间 id 列表
    """
    connection = db.conn()
    rows = connection.execute(
        """
        select m.space_id as space_id
        from memory_space_meta as m
        order by m.create_ts asc
        """
    ).fetchall()

    return [str(row["space_id"]) for row in rows]


def resolve_spaces(db: Database, space: str) -> List[str]:
    """
    确定本次要处理的空间：显式指定则只处理它，否则处理全部。

    :param db: 数据库对象
    :param space: 命令行指定的空间（为空表示全部）
    :return: 空间 id 列表
    """
    if space:
        db.ensure_space(space, space)
        return [space]
    return list_spaces(db) or ["default"]


def cmd_daily(db: Database, spaces: List[str], force: bool) -> int:
    """
    执行日结巩固。

    :param db: 数据库对象
    :param spaces: 目标空间
    :param force: 是否忽略"最少几条"的阈值
    :return: 进程退出码
    """
    consolidator = Consolidator(db)
    failures = 0

    for space_id in spaces:
        report = consolidator.consolidate_daily(space_id, force=force)

        if report.get("ok"):
            flag = "（降级摘要）" if report.get("degraded") else ""
            print(
                f"[日结]{flag} 空间 {space_id}：归并 {report.get('topic_count')} 条往事，"
                f"生成「{report.get('title')}」，沉淀经验 {report.get('lessons')} 条"
            )
            persona = report.get("persona") or {}
            if persona.get("stepped"):
                steps = persona.get("steps") or []
                detail = "，".join(
                    f"{item['param']} {item['base_from']}->{item['base_to']}" for item in steps
                )
                print(f"[基调] 推进 {len(steps)} 个维度：{detail}")
                if persona.get("substantive"):
                    print(f"[基调] 累计变化跨过阈值 → 人格版本 v{persona.get('version')}（已落 event 记忆）")
                else:
                    print(
                        f"[基调] 未跨过阈值，维持 v{persona.get('version')}，"
                        f"变迁日志 +{persona.get('changelog_entries')} 条"
                    )
            else:
                print(f"[基调] 未推进：{persona.get('message') or '—'}")
        else:
            print(f"[跳过] 空间 {space_id}：{report.get('reason')}")
            if report.get("error"):
                failures += 1

    return 0 if failures == 0 else 1


def cmd_stats(db: Database, spaces: List[str]) -> int:
    """
    打印各空间记忆规模，用于长期运行的体检。

    :param db: 数据库对象
    :param spaces: 目标空间
    :return: 进程退出码
    """
    for space_id in spaces:
        topic_counts = TopicStore(db).count_by_kind(space_id)
        kernel = PersonaKernel(db, space_id)
        snapshot = PersonaSnapshotStore(db, space_id, kernel, KGStore(db)).load_current()
        changelog = PersonaChangelogStore(db, space_id)
        consolidator = Consolidator(db)

        last_daily = consolidator.last_consolidated_ts(space_id, "daily")
        last_daily_text = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(last_daily)) if last_daily else "从未巩固"
        )

        connection = db.conn()
        kg_active = connection.execute(
            "select count(1) as cnt from kg_relation as r where r.space_id = ? and r.is_active = 1",
            (space_id,),
        ).fetchone()["cnt"]
        kg_inactive = connection.execute(
            "select count(1) as cnt from kg_relation as r where r.space_id = ? and r.is_active = 0",
            (space_id,),
        ).fetchone()["cnt"]
        experiences = connection.execute(
            "select count(1) as cnt from experience as x where x.space_id = ?",
            (space_id,),
        ).fetchone()["cnt"]

        print(f"=== 空间 {space_id} ===")
        print(f"人格版本：v{snapshot.version}")
        print(f"关系：生效 {kg_active} / 已推翻 {kg_inactive}")
        print(f"主题：普通 {topic_counts['normal']}（待归并 {topic_counts['pending']}）"
              f" / 日结 {topic_counts['daily']} / 大事 {topic_counts['event']}")
        print(f"经验片段：{experiences}")
        print(f"变迁日志：{changelog.count()} 条")
        print(f"上次日结：{last_daily_text}")

        # 人格基调 vs 当前状态：一眼看出"稳不稳"
        baseline = kernel.get_baseline()
        current = kernel.get_params()
        pending = kernel.get_pending()
        drift = {
            param: round(current.get(param, 0.0) - baseline.get(param, 0.0), 3)
            for param in baseline
        }
        max_drift = max((abs(value) for value in drift.values()), default=0.0)
        print(f"人格基调：摆动带上限 ±{config.PERSONA_DRIFT_LIMIT}，当前最大偏移 {max_drift:.3f}")
        steps = {param: round(value, 3) for param, value in pending.items() if value}
        print(f"待结算偏移：{steps if steps else '无'}")

        logs = consolidator.list_logs(space_id, limit=3)
        if logs:
            print("最近巩固：")
            for item in logs:
                stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["create_ts"]))
                print(f"  - [{item['kind']}] {stamp} {item['summary'][:60]}")
        print()

    return 0


def cmd_prune_snapshots(db: Database, spaces: List[str], keep: int) -> int:
    """
    快照冷存：只保留最近 keep 个版本，其余移到 archive/ 子目录（不删除）。

    :param db: 数据库对象
    :param spaces: 目标空间
    :param keep: 当前目录保留的版本数
    :return: 进程退出码
    """
    moved_total = 0

    for space_id in spaces:
        directory = Path(config.SNAPSHOT_DIR) / space_id
        if not directory.exists():
            continue

        files = sorted(directory.glob("v*_*.json"))
        if len(files) <= keep:
            continue

        archive_dir = directory / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        # 保留最新的 keep 个（文件名按版本号排序，v 前缀定长，可直接字典序比较）
        for path in files[: len(files) - keep]:
            target = archive_dir / path.name
            if target.exists():
                target.unlink()
            shutil.move(str(path), str(target))
            moved_total += 1

        print(f"[冷存] 空间 {space_id}：{len(files)} 个快照 → 保留 {keep} 个，归档 {len(files) - keep} 个")

    if moved_total == 0:
        print("[冷存] 没有需要归档的快照（数量未超过保留上限）")
    return 0


def cmd_seed_persona(db: Database, spaces: List[str]) -> int:
    """
    注入人格的既有记忆（前史、关系网、人物档案）。

    用于新建一个"有过去"的空间：注入后她不是从零开始，而是本来就有 18 年人生可以回想。
    幂等，重复执行只会跳过已有条目。

    :param db: 数据库对象
    :param spaces: 目标空间
    :return: 进程退出码
    """
    from cognitive.persona.seed import persona_seed_overview, seed_emotions, seed_persona

    for space_id in spaces:
        before = persona_seed_overview(db, space_id)
        stats = seed_persona(db, space_id, space_id)
        # 情绪残留单独补写：已注入过前史的空间也能增量升级，不必清空重跑
        emotions = seed_emotions(db, space_id)
        print(
            f"[注入] 空间 {space_id}：新增主题 {stats['topics']} 条（跳过 {stats['skipped_topics']} 条）、"
            f"关系 {stats['relations']} 条、实体 {stats['entities']} 个、经验 {stats['experiences']} 条"
        )
        print(
            f"[注入] 现状：既有记忆主题 {before['topics'] + stats['topics']} 条 / "
            f"预置关系 {before['relations'] + stats['relations']} 条 / "
            f"情绪片段 {emotions} 条 / 向量索引 {stats['indexed']} 条"
        )
    return 0


def cmd_persona_info() -> int:
    """
    打印人格原始数据与运行时数据的存放位置，供升级/迁移前核对。

    :return: 进程退出码
    """
    meta = config.PERSONA_ASSETS["meta"]
    profile = config.PERSONA_PROFILE
    kernel = config.PERSONA_KERNEL_INIT
    seed = config.PERSONA_SEED

    print("=== 人格原始数据（升级时保留） ===")
    print(f"目录：{config.PERSONA_DATA_DIR}")
    print(f"人格：{config.PERSONA_NAME}（schema v{meta.get('schema_version')}）")
    print(f"档案：{len(profile)} 组 / {sum(len(v) for v in profile.values())} 条")
    print(f"基线参数：{len(kernel)} 个")
    print(
        f"前史：{len(seed['stories'])} 段往事 + 1 份人物档案 / "
        f"{len(seed['entities'])} 个实体 / {len(seed['relations'])} 条关系"
    )
    avatar_ok = config.PERSONA_AVATAR_FILE.is_file()
    print(
        f"头像：{config.PERSONA_AVATAR_FILE}"
        f"（{'存在' if avatar_ok else '缺失，Web UI 会回退文字头像'}）"
    )
    print("\n=== 运行时数据（用户与机器人产生） ===")
    print(f"记忆库：{config.DB_PATH}")
    print(f"向量索引：{config.VECTOR_DIR}")
    print(f"人格快照：{config.SNAPSHOT_DIR}")
    return 0


def cmd_logs(db: Database, spaces: List[str], limit: int) -> int:
    """
    打印最近的巩固日志。

    :param db: 数据库对象
    :param spaces: 目标空间
    :param limit: 每个空间返回条数
    :return: 进程退出码
    """
    consolidator = Consolidator(db)
    for space_id in spaces:
        print(f"=== 空间 {space_id} 的巩固记录 ===")
        for item in consolidator.list_logs(space_id, limit=limit):
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["create_ts"]))
            print(f"- [{item['kind']}] {stamp}")
            print(f"  {item['summary']}")
        print()
    return 0


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="认知体长期维护入口：日结巩固 / 规模体检 / 快照冷存",
    )
    parser.add_argument("--space", default="", help="只处理指定空间，缺省处理全部")
    parser.add_argument("--daily", action="store_true", help="执行日结巩固")
    parser.add_argument("--stats", action="store_true", help="打印记忆规模")
    parser.add_argument("--logs", action="store_true", help="打印最近巩固记录")
    parser.add_argument("--prune-snapshots", action="store_true", help="快照冷存")
    parser.add_argument("--seed-persona", action="store_true", help="注入人格的既有记忆（前史/关系网/档案）")
    parser.add_argument("--persona-info", action="store_true", help="打印人格原始数据与运行时数据位置")
    parser.add_argument("--keep", type=int, default=config.SNAPSHOT_RETAIN_VERSIONS, help="快照保留版本数")
    parser.add_argument("--force", action="store_true", help="日结时忽略最少条数阈值")
    parser.add_argument("--limit", type=int, default=10, help="日志返回条数")
    parser.add_argument("--log-file", default="", help="同时把输出追加写入该文件（计划任务用）")
    args = parser.parse_args()

    # 无控制台环境（pythonw.exe / 计划任务后台会话）下 sys.stdout 是 None，
    # 任何 print 都会抛异常、脚本当场退出（实测任务返回码 120、日志为空）。
    # 这里先兜一个空设备，后面 --log-file 再把输出 tee 到文件。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")

    # 计划任务里无法用 shell 重定向，这里自己做 tee
    if args.log_file:
        log_path = Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "a", encoding="utf-8")
        sys.stdout = _Tee(sys.stdout, handle)

    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] maintenance 启动，参数：{vars(args)}")

    db = Database()
    spaces = resolve_spaces(db, args.space)

    # 无参数时默认展示体检，避免用户不知道能做什么
    if not any(
        [
            args.daily,
            args.stats,
            args.logs,
            args.prune_snapshots,
            args.seed_persona,
            args.persona_info,
        ]
    ):
        return cmd_stats(db, spaces)

    status = 0
    if args.persona_info:
        status |= cmd_persona_info()
    if args.seed_persona:
        status |= cmd_seed_persona(db, spaces)
    if args.daily:
        status |= cmd_daily(db, spaces, force=args.force)
    if args.stats:
        status |= cmd_stats(db, spaces)
    if args.logs:
        status |= cmd_logs(db, spaces, limit=args.limit)
    if args.prune_snapshots:
        status |= cmd_prune_snapshots(db, spaces, keep=args.keep)

    return status


if __name__ == "__main__":
    sys.exit(main())
