"""
Token 估算工具。

设计说明：
- 目标模型为 DeepSeek，官方未提供本地 tokenizer，tiktoken 也未安装。
- 因此采用启发式估算：CJK 字符按 0.75 token/字，其余字符按 4 字符/token。
- 该估算只用于"安全阈值判定"，误差通过 SAFE_TOKEN_MARGIN 预留缓冲吸收。
- 若后续安装 tiktoken，可把 TOKEN_BACKEND 切为 "tiktoken" 获得精确计数。
"""
from __future__ import annotations

import re
from typing import Optional

# 统计 CJK 统一表意文字（含扩展 A 区）的字符数量
_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# 每个 CJK 字符折算的 token 数
_CJK_TOKEN_RATIO: float = 0.75

# 每个非 CJK 字符折算的 token 数（约 4 字符 1 token）
_LATIN_TOKEN_RATIO: float = 0.25

# tiktoken 编码器缓存（首次使用时惰性加载）
_TIKTOKEN_ENCODER = None


def _count_cjk(text: str) -> int:
    """统计字符串中的 CJK 字符数。"""
    return len(_CJK_PATTERN.findall(text))


def estimate_tokens(text: str, backend: str = "heuristic") -> int:
    """
    估算一段文本的 token 数量。

    :param text: 待估算文本
    :param backend: "heuristic" 使用启发式估算；"tiktoken" 使用精确编码器（需已安装）
    :return: token 估算值（整数，最小为 0）
    """
    global _TIKTOKEN_ENCODER

    if not text:
        return 0

    if backend == "tiktoken":
        try:
            if _TIKTOKEN_ENCODER is None:
                import tiktoken  # type: ignore

                _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
            return len(_TIKTOKEN_ENCODER.encode(text))
        except Exception:
            # 未安装或初始化失败时自动降级为启发式
            pass

    cjk_chars = _count_cjk(text)
    other_chars = max(len(text) - cjk_chars, 0)
    total = cjk_chars * _CJK_TOKEN_RATIO + other_chars * _LATIN_TOKEN_RATIO
    return max(int(total), 0)


def estimate_messages_tokens(messages, extra_text: Optional[str] = None, backend: str = "heuristic") -> int:
    """
    估算一组消息的总 token（含角色标记的固定开销）。

    :param messages: Message 对象列表，或含 role/content 属性的对象
    :param extra_text: 额外拼接的文本（如 system 人格文本）
    :param backend: token 计数后端
    :return: 总 token 估算值
    """
    total = 0

    # 每条消息按 "role: content" 拼接后估算，并追加 4 token 的结构开销
    for msg in messages:
        content = getattr(msg, "content", "") or ""
        role = getattr(msg, "role", "") or ""
        total += estimate_tokens(f"{role}: {content}", backend) + 4

    if extra_text:
        total += estimate_tokens(extra_text, backend)

    return total
