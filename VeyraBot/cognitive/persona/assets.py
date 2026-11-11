"""
人格原始数据（"机器人本来的数据"）的加载与校验。

所有原初人格资产集中在 data/persona/，与运行时代码分离：

    data/persona/
    ├── meta.json      # schema 版本、人格名、头像文件名
    ├── profile.json   # 身份档案（Prompt 里的【你的身份档案】）
    ├── kernel.json    # 人格基线参数 + 参数释义
    ├── seed.json      # 前史：实体 / 关系 / 成长往事 / 人物档案主题
    └── avatar.jpg     # 头像

这样升级代码时不必在 config.py / seed.py 里找数据，也不会被代码覆盖；
维护者只需要备份、迁移或替换这个目录。

设计取舍：
- 只做"读取 + 结构校验"，不在加载时写库、不产生副作用；
- 缺文件、JSON 损坏、必填字段缺失时抛出带路径的明确错误，
  避免以"空人格"静默启动；
- 头像缺失不阻塞启动（Web UI 有文字头像兜底），由维护命令报告。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


class PersonaDataError(RuntimeError):
    """人格原始数据缺失或格式错误。"""


def _read_json(path: Path) -> Any:
    """
    读取一个 JSON 文件，失败时给出可直接定位的错误。

    :param path: 文件路径
    :return: 解析后的对象
    """
    if not path.is_file():
        raise PersonaDataError(
            f"人格数据文件缺失：{path}\n"
            "请恢复 data/persona/ 目录，或用 PERSONA_DATA_DIR 指向备份目录。"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PersonaDataError(f"人格数据文件不是合法 JSON：{path}（{exc}）") from exc


def load_persona_data(persona_dir: Path) -> Dict[str, Any]:
    """
    加载并校验 data/persona/ 下的全部人格原始数据。

    :param persona_dir: 人格数据目录
    :return: {"meta","profile","kernel","seed","avatar"}
    """
    persona_dir = Path(persona_dir)
    meta = _read_json(persona_dir / "meta.json")
    profile = _read_json(persona_dir / "profile.json")
    kernel = _read_json(persona_dir / "kernel.json")
    seed = _read_json(persona_dir / "seed.json")

    if not isinstance(meta, dict) or not str(meta.get("name") or "").strip():
        raise PersonaDataError(f"meta.json 缺少有效的 name：{persona_dir / 'meta.json'}")
    if not isinstance(profile, dict) or not profile:
        raise PersonaDataError(f"profile.json 必须是非空对象：{persona_dir / 'profile.json'}")
    if not isinstance(kernel, dict):
        raise PersonaDataError(f"kernel.json 必须是对象：{persona_dir / 'kernel.json'}")

    init = kernel.get("init")
    description = kernel.get("description")
    if not isinstance(init, dict) or not init:
        raise PersonaDataError(f"kernel.json 缺少 init 参数表：{persona_dir / 'kernel.json'}")
    if not isinstance(description, dict) or not description:
        raise PersonaDataError(
            f"kernel.json 缺少 description 参数释义：{persona_dir / 'kernel.json'}"
        )

    missing_desc = sorted(set(init) - set(description))
    if missing_desc:
        raise PersonaDataError(
            f"kernel.json 中这些参数缺少中文释义：{', '.join(missing_desc)}"
        )

    if not isinstance(seed, dict):
        raise PersonaDataError(f"seed.json 必须是对象：{persona_dir / 'seed.json'}")
    for key in ("entities", "relations", "stories", "profile_topic"):
        if key not in seed:
            raise PersonaDataError(
                f"seed.json 缺少字段 {key}：{persona_dir / 'seed.json'}"
            )
    if not isinstance(seed["entities"], list) or not isinstance(seed["stories"], list):
        raise PersonaDataError("seed.json 的 entities / stories 必须是数组")
    if not isinstance(seed["relations"], list):
        raise PersonaDataError("seed.json 的 relations 必须是数组")
    if not isinstance(seed["profile_topic"], dict):
        raise PersonaDataError("seed.json 的 profile_topic 必须是对象")

    avatar_name = str(meta.get("avatar") or "avatar.jpg")
    return {
        "meta": meta,
        "profile": profile,
        "kernel": {"init": init, "description": description},
        "seed": seed,
        "avatar": persona_dir / avatar_name,
    }
