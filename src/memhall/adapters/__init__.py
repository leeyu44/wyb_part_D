"""适配器注册表：name → 适配器类的唯一映射。

CLI（cmd_run）与 Web UI（/api/start worker）此前各持一份逐字复制的
if/elif 导入链，加适配器要同步改两处——本模块收敛为单源。

值是 "模块:类名" 字符串，create_adapter 时才 import（保持 lazy，
mock 之外的依赖仅在真用时加载）。
"""

from __future__ import annotations

from importlib import import_module

from memhall.adapters.base import AgentAdapter

ADAPTERS: dict[str, str] = {
    "mock": "memhall.adapters.mock:MockAdapter",
    "hermes": "memhall.adapters.hermes:HermesAdapter",
    "kylinbot": "memhall.adapters.kylinbot:KylinBotAdapter",
    "hermes-local": "memhall.adapters.hermes_local:LocalHermesAdapter",
    "claude-local": "memhall.adapters.claude_local:LocalClaudeAdapter",
    "qwen-local": "memhall.adapters.qwen_local:LocalQwenAdapter",
    "opencode": "memhall.adapters.opencode:OpenCodeAdapter",
}


def create_adapter(name: str) -> AgentAdapter:
    """按名实例化适配器；未知名抛 ValueError（调用方转用户错误）。"""
    try:
        path = ADAPTERS[name]
    except KeyError:
        raise ValueError(
            f"未知适配器: {name}（可选: {', '.join(ADAPTERS)}）") from None
    module, cls = path.split(":")
    return getattr(import_module(module), cls)()
