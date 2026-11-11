"""
DeepSeek 客户端封装。

关键处理：
1. deepseek-flash 会先输出 reasoning_content，若 max_tokens 过小，正式 content 可能为空
   —— 此时检测 finish_reason == "length" 并自动放大 max_tokens 重试。
2. 结构化抽取统一走 chat_json，内建 JSON 修复与容错解析。
3. 所有调用失败都会抛 LLMError，由上层决定降级策略（不允许静默返回空字符串）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

import requests

import config


class LLMError(RuntimeError):
    """模型调用失败异常（网络、鉴权、解析）。"""


# 匹配 ```json ... ``` 围栏
_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)

# 用于从噪声文本中抠出第一个 JSON 对象
_OBJECT_PATTERN = re.compile(r"\{.*\}", re.S)


class DeepSeekClient:
    """DeepSeek Chat Completions 客户端（OpenAI 兼容协议）。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retry: Optional[int] = None,
    ) -> None:
        """
        初始化客户端。

        :param api_key: 密钥，缺省读 config
        :param base_url: 接口地址，缺省读 config
        :param model: 模型名，缺省读 config
        :param timeout: 超时秒数，缺省读 config
        :param max_retry: 重试次数，缺省读 config
        """
        self.api_key = api_key or config.DEEPSEEK_API_KEY
        self.base_url = (base_url or config.DEEPSEEK_BASE_URL).rstrip("/")
        self.model = model or config.MODEL_NAME
        self.timeout = timeout or config.LLM_TIMEOUT
        self.max_retry = config.LLM_MAX_RETRY if max_retry is None else max_retry

        # 密钥缺失时直接失败，避免后续出现难以定位的 401
        if not self.api_key and config.LLM_MODE == "api":
            raise LLMError("未配置 DEEPSEEK_API_KEY，请检查 .env 文件")

        # 复用连接池，减少握手开销，降低长连接被中断的概率
        self._session = self._build_session()

    # ------------------------------------------------------------
    # 基础调用
    # ------------------------------------------------------------
    def _build_session(self) -> requests.Session:
        """
        构造带鉴权头的 HTTP 会话（连接被重置时用于重建）。

        :return: requests.Session 实例
        """
        session = requests.Session()
        session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        )
        # 适配器层再兜一次网络重试，覆盖代理/网关侧的瞬时断连
        adapter = requests.adapters.HTTPAdapter(max_retries=0, pool_maxsize=4)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> str:
        """
        发起一次对话补全，返回正式回答文本。

        :param messages: OpenAI 格式消息列表
        :param temperature: 采样温度
        :param max_tokens: 最大输出 token
        :param json_mode: 是否强制模型输出 JSON
        :return: 回答文本
        """
        # mock 模式用于完全离线调试：不发起网络请求，返回占位内容
        if config.LLM_MODE == "mock":
            return '{"mock": true}' if json_mode else "（mock 模式回答）"

        temperature = config.ANSWER_TEMPERATURE if temperature is None else temperature
        max_tokens = config.ANSWER_MAX_TOKENS if max_tokens is None else max_tokens

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

        # DeepSeek 支持 response_format，用于约束 JSON 输出
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: Optional[str] = None

        for attempt in range(self.max_retry + 1):
            try:
                resp = self._session.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    timeout=(10, self.timeout),
                )

                # 鉴权失败、余额不足等无需重试，直接抛出
                if resp.status_code in (401, 403):
                    raise LLMError(
                        f"鉴权失败 HTTP {resp.status_code}: {resp.text[:300]}"
                    )

                # 其他错误进入重试分支
                if resp.status_code != 200:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                else:
                    data = resp.json()
                    choice = data.get("choices", [{}])[0]
                    message = choice.get("message", {}) or {}
                    content = (message.get("content") or "").strip()
                    finish_reason = choice.get("finish_reason", "")

                    # JSON 输出被截断（reasoning 占用预算）→ 放大预算重试，避免拿到半截 JSON
                    if (
                        finish_reason == "length"
                        and json_mode
                        and payload["max_tokens"] < 8000
                    ):
                        payload["max_tokens"] = min(payload["max_tokens"] * 2, 8000)
                        last_error = "JSON 输出被截断，已放大 max_tokens 重试"
                    # 普通回答被截断：内容不完整，放大预算重新生成（可通过配置关闭）
                    elif (
                        finish_reason == "length"
                        and content
                        and config.ANSWER_RETRY_ON_TRUNCATE
                        and payload["max_tokens"] < 5000
                    ):
                        payload["max_tokens"] = min(payload["max_tokens"] * 2, 5000)
                        last_error = "回答被截断，已放大 max_tokens 重试"
                    # reasoning 占满了预算导致正式内容为空：放大预算后重试一次
                    elif (
                        not content
                        and finish_reason == "length"
                        and payload["max_tokens"] < 6000
                    ):
                        payload["max_tokens"] = min(payload["max_tokens"] * 2, 6000)
                        last_error = (
                            "content 为空（reasoning 占用预算），已放大 max_tokens 重试"
                        )
                    elif content:
                        return content
                    else:
                        last_error = "模型返回空内容"

            except LLMError:
                raise
            except requests.Timeout:
                last_error = "请求超时"
            except Exception as exc:  # 网络异常、JSON 解析异常等
                last_error = f"{type(exc).__name__}: {exc}"

                # 连接被重置/断开：丢弃损坏的连接池，重建会话后重试
                if isinstance(exc, (requests.ConnectionError, ConnectionError)):
                    try:
                        self._session.close()
                    except Exception:
                        pass
                    self._session = self._build_session()

            # 指数退避，最后一次失败后不再等待
            if attempt < self.max_retry:
                time.sleep(config.LLM_RETRY_BACKOFF * (2**attempt))

        raise LLMError(f"模型调用失败（{self.model}）：{last_error}")

    # ------------------------------------------------------------
    # 结构化调用
    # ------------------------------------------------------------
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        发起一次结构化抽取调用，返回解析后的字典。

        :param messages: 消息列表
        :param temperature: 温度（抽取建议 0.1~0.3）
        :param max_tokens: 最大输出 token
        :return: 解析后的 JSON 字典
        """
        if config.LLM_MODE == "mock":
            return {}

        text = self.chat(
            messages=messages,
            temperature=config.EXTRACT_TEMPERATURE
            if temperature is None
            else temperature,
            max_tokens=config.EXTRACT_MAX_TOKENS if max_tokens is None else max_tokens,
            json_mode=True,
        )
        return parse_json_object(text)


def parse_json_object(text: str) -> Dict[str, Any]:
    """
    从模型输出中解析 JSON 对象（容忍围栏、前后废话、尾随逗号）。

    :param text: 模型原始输出
    :return: 解析后的字典；失败抛出 LLMError
    """
    if not text or text is None:
        raise LLMError("待解析的模型输出为空")

    candidate = text.strip()

    # 若被 markdown 围栏包裹，先取围栏内部
    fence_match = _FENCE_PATTERN.search(candidate)
    if fence_match:
        candidate = fence_match.group(1).strip()

    # 直接解析
    try:
        result = json.loads(candidate)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # 从噪声文本中抠出第一个完整对象
    object_match = _OBJECT_PATTERN.search(text)
    if object_match:
        raw = object_match.group(0)
        # 修复常见问题：对象/数组末尾多余的逗号
        repaired = re.sub(r",\s*(\]|\})", r"\1", raw)
        try:
            # strict=False 允许字符串内出现未转义的控制字符
            result = json.loads(repaired, strict=False)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError as exc:
            raise LLMError(f"JSON 解析失败：{exc}；原始输出片段：{raw[:300]}") from exc

    raise LLMError(f"模型未返回合法 JSON：{text[:300]}")


# 全局单例，避免重复创建连接配置
_CLIENT: Optional[DeepSeekClient] = None


def get_client() -> DeepSeekClient:
    """获取全局客户端单例。"""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = DeepSeekClient()
    return _CLIENT
