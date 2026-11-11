"""
环境变量加载器（不引入第三方 dotenv 依赖）。

职责：
1. 读取项目根目录下的 .env 文件（KEY=VALUE 格式，支持 # 注释与空行）
2. 写入 os.environ（不覆盖已存在的系统环境变量，保证外部注入优先）
3. 提供 get(name, default) 统一读取入口
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# 项目根目录：cognitive/env.py -> cognitive -> 项目根
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


def load_dotenv(env_path: Optional[Path] = None, override: bool = False) -> None:
    """
    解析 .env 文件并写入 os.environ。

    :param env_path: .env 文件路径，默认取项目根目录的 .env
    :param override: 是否覆盖已存在的环境变量，默认 False（外部注入优先）
    """
    path = env_path or (PROJECT_ROOT / ".env")

    # 文件不存在时静默返回，允许只靠系统环境变量运行
    if not path.exists():
        return

    # 以 UTF-8 读取，忽略无法解码的异常字节，避免编码问题中断启动
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()

        # 跳过空行与注释行
        if not line or line.startswith("#"):
            continue

        # 跳过不含等号的非法行
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        # 去掉包裹值的引号（单引号或双引号）
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        # 已存在且不要求覆盖时，保留原值
        if not override and key in os.environ:
            continue

        os.environ[key] = value


def get(name: str, default: str = "") -> str:
    """
    读取环境变量。

    :param name: 变量名
    :param default: 缺省值
    :return: 变量值字符串
    """
    return os.environ.get(name, default)


def get_int(name: str, default: int) -> int:
    """读取整型环境变量，解析失败时回退默认值。"""
    try:
        return int(get(name, str(default)))
    except (TypeError, ValueError):
        return default


def get_float(name: str, default: float) -> float:
    """读取浮点型环境变量，解析失败时回退默认值。"""
    try:
        return float(get(name, str(default)))
    except (TypeError, ValueError):
        return default
