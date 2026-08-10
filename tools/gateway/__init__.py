"""Protocol adapters for HASHI's canonical :class:`ToolRegistry`."""

from .context import GatewayContext, load_gateway_context, write_gateway_context

__all__ = ["GatewayContext", "load_gateway_context", "write_gateway_context"]
