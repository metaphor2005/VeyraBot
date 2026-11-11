"""
人格快照管理（顶层叙事）。

关键规则（对应"每个新性格一个独立文件，只使用最新一份"的设计）：
1. 只有发生实质性跃迁时才生成新版本文件，微小措辞调整不新建；
2. 每个版本独立成文件，只读，命名 v{版本号}_{topic_id}.json；
3. 推理时只加载当前版本，"当前版本指针"保存在 persona_meta；
4. 旧版本保留在磁盘用于溯源，但默认不加载。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import List, Optional

import config
from cognitive.models import PersonaSnapshot
from cognitive.persona.kernel import PersonaKernel
from cognitive.storage.database import Database
from cognitive.storage.kg_store import KGStore


class PersonaSnapshotStore:
    """人格快照文件库。"""

    def __init__(self, db: Database, space_id: str, kernel: PersonaKernel, kg: KGStore) -> None:
        """
        :param db: 数据库访问对象
        :param space_id: 记忆空间
        :param kernel: 人格内核（读取参数与情景状态）
        :param kg: KG 存储（读取核心信念）
        """
        self.db = db
        self.space_id = space_id
        self.kernel = kernel
        self.kg = kg
        self.dir = Path(config.SNAPSHOT_DIR) / space_id
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------
    # 生成新版本
    # ------------------------------------------------------------
    def create(self, self_view: str, narrative: str, topic_id: str) -> PersonaSnapshot:
        """
        生成一个新的人格快照版本（独立文件）。

        :param self_view: 新的自我认知一句话
        :param narrative: 面向用户的自我叙事段落
        :param topic_id: 触发本次跃迁的事件 id
        :return: 新快照对象
        """
        version = self.kernel.get_current_version() + 1

        snapshot = PersonaSnapshot(
            version=version,
            space_id=self.space_id,
            self_view=self_view,
            narrative=narrative,
            kernel=self.kernel.get_params(),
            moods=self.kernel.get_moods(),
            core_beliefs=self.kg.query_core_beliefs(self.space_id),
            topic_id=topic_id,
            baseline=self.kernel.get_baseline(),
        )

        file_path = self.dir / f"v{version:03d}_{topic_id or 'init'}.json"
        file_path.write_text(
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 更新"当前版本指针"，下次推理只加载这一份
        self.kernel.set_current_version(version)
        return snapshot

    # ------------------------------------------------------------
    # 读取当前版本
    # ------------------------------------------------------------
    def load_current(self) -> PersonaSnapshot:
        """
        读取当前生效的人格快照。首次调用时自动生成 v000 初始快照。

        :return: 当前快照对象
        """
        version = self.kernel.get_current_version()

        file_path = self._find_file(version)
        if file_path and file_path.exists():
            try:
                return PersonaSnapshot.from_dict(json.loads(file_path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                # 文件损坏时用基线重建，保证系统可用
                pass

        # 没有可用快照：生成 v000 基线版本（不递增版本号）
        snapshot = PersonaSnapshot(
            version=version,
            space_id=self.space_id,
            self_view=config.PERSONA_INITIAL_SELF_VIEW,
            narrative=config.PERSONA_INITIAL_SELF_VIEW,
            kernel=self.kernel.get_params(),
            moods=self.kernel.get_moods(),
            core_beliefs=self.kg.query_core_beliefs(self.space_id),
            topic_id="init",
            baseline=self.kernel.get_baseline(),
        )
        baseline_path = self.dir / f"v{version:03d}_init.json"
        if not baseline_path.exists():
            baseline_path.write_text(
                json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return snapshot

    def current_self_view(self) -> str:
        """
        读取"此刻的自我认知"。

        优先用即时层（每轮可微调，不版本化），没有时才回落到最新快照。
        这样回答里能体现当天的自我表述，而版本号仍按日结推进。

        :return: 自我认知文本
        """
        live = self.kernel.get_live_self_view()
        if live:
            return live
        return self.load_current().self_view

    def _find_file(self, version: int) -> Optional[Path]:
        """
        按版本号查找对应的快照文件。

        :param version: 版本号
        :return: 文件路径，未找到返回 None
        """
        pattern = str(self.dir / f"v{version:03d}_*.json")
        matches = sorted(glob.glob(pattern))
        return Path(matches[0]) if matches else None

    # ------------------------------------------------------------
    # 历史版本
    # ------------------------------------------------------------
    def list_versions(self) -> List[int]:
        """
        列出磁盘上存在的全部快照版本号。

        :return: 版本号升序列表
        """
        versions: List[int] = []
        for path in self.dir.glob("v*_*.json"):
            name = path.name
            try:
                versions.append(int(name[1:4]))
            except (ValueError, IndexError):
                continue
        return sorted(set(versions))
