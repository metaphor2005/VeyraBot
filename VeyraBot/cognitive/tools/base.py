"""
工具插件层（只提供接口与调度，不含任何具体工具实现）。

设计原则：
1. 认知内核不认识任何具体工具，只认识 ToolManager；
2. 新增工具 = 写一个 BaseTool 子类 + register，内核代码零改动；
3. 本轮默认不注册任何工具，接口留给你自己接（搜索、识别、代码执行等）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseTool(ABC):
    """所有工具的抽象基类。"""

    # 工具名（供模型识别，必须唯一）
    name: str = ""

    # 工具描述（写给模型看，说明什么时候该用）
    description: str = ""

    # 参数 JSON Schema
    parameters: Dict[str, Any] = {}

    @abstractmethod
    def run(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行工具。

        :param args: 模型给出的参数
        :return: 统一结构的结果 {"ok": bool, "data": Any, "error": str}
        """
        raise NotImplementedError


class ToolManager:
    """工具注册与分发中心（认知内核唯一打交道的对象）。"""

    def __init__(self) -> None:
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """
        注册工具。

        :param tool: 工具实例，name 不能为空
        """
        if not tool.name:
            raise ValueError("工具 name 不能为空")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """注销工具。"""
        self._tools.pop(name, None)

    def has_tool(self, name: str) -> bool:
        """判断工具是否已注册。"""
        return name in self._tools

    def list_schemas(self) -> List[Dict[str, Any]]:
        """
        导出全部工具的 schema，用于注入 Prompt 或原生 function calling。

        :return: schema 列表
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    def execute(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        统一执行入口。

        :param name: 工具名
        :param args: 参数
        :return: 工具结果；工具不存在时返回错误结构
        """
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "data": None, "error": f"工具 {name} 未注册"}

        try:
            return tool.run(args)
        except Exception as exc:
            return {"ok": False, "data": None, "error": f"{type(exc).__name__}: {exc}"}


class ToolLoopController:
    """
    工具循环控制器（骨架，供你接原生 function calling 时使用）。

    职责：
    1. 限制单轮最多调用次数，防止死循环；
    2. 把工具结果包装成消息，交回模型继续推理；
    3. 内核不感知具体工具。
    """

    def __init__(self, manager: ToolManager, max_rounds: int = 5) -> None:
        """
        :param manager: 工具管理器
        :param max_rounds: 单轮最多工具调用轮数
        """
        self.manager = manager
        self.max_rounds = max_rounds

    def execute_calls(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        执行模型返回的一批工具调用。

        :param tool_calls: [{"name":..,"args":{..}}]
        :return: 结果列表
        """
        results: List[Dict[str, Any]] = []
        for call in tool_calls[: self.max_rounds]:
            name = call.get("name", "")
            args = call.get("args", {}) or {}
            results.append({"tool": name, "result": self.manager.execute(name, args)})
        return results

    def build_tool_message(self, tool_name: str, result: Dict[str, Any]) -> Dict[str, str]:
        """
        把工具结果包装为一条消息。

        :param tool_name: 工具名
        :param result: 工具返回
        :return: 消息字典
        """
        import json

        return {
            "role": "user",
            "content": f"[工具 {tool_name} 返回]\n{json.dumps(result, ensure_ascii=False)}",
        }


def _type_placeholder(value: Optional[Any] = None) -> None:
    """占位函数，保持类型注解可被静态检查工具识别。"""
    _ = value
