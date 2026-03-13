from __future__ import annotations
from pathlib import Path
from typing import Any

from orchestrator.agent_directory import AgentDirectory
from orchestrator.bridge_protocol import build_result_reply, validate_reply_payload, validate_request_payload
from orchestrator.conversation_store import ConversationStore

_C_BRIDGE = "\033[38;5;110m"
_C_RESET = "\033[0m"


def _print_bridge_message(from_agent: str, to_agent: str, text: str, kind: str = "request"):
    """Print inter-agent traffic in muted steel blue on the console."""
    preview = text[:200].replace("\n", " ")
    if len(text) > 200:
        preview += "..."
    tag = "reply" if kind == "reply" else "msg"
    line = f"{_C_BRIDGE}[bridge] {from_agent} -> {to_agent} ({tag}) {preview}{_C_RESET}"
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError):
        safe = line.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace")
        print(safe, flush=True)


class ConversationRouter:
    def __init__(self, config_path: Path, capabilities_path: Path, store_path: Path, runtimes: list[Any]):
        self.directory = AgentDirectory(config_path, capabilities_path, runtimes)
        self.store = ConversationStore(store_path)

    def refresh(self, runtimes: list[Any] | None = None) -> None:
        if runtimes is not None:
            self.directory.runtimes = runtimes
        self.directory.refresh()

    async def send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = validate_request_payload(payload)
        self.store.ensure_thread(
            message["thread_id"],
            created_by=message["from_agent"],
            participants=[message["from_agent"], message["to_agent"]],
        )
        self.store.save_message(message, status="received")

        allowed, reason = self.directory.check_permission(message)
        self.store.record_permission_audit(message["message_id"], "allow" if allowed else "deny", reason)
        if not allowed:
            self.store.update_message_status(message["message_id"], "rejected", error_text=reason)
            self.store.update_thread_status(message["thread_id"], "rejected")
            raise PermissionError(reason)

        runtime = self.directory.get_runtime(message["to_agent"])
        bridge_prompt = self._render_prompt(message)
        _print_bridge_message(message["from_agent"], message["to_agent"], message["text"])
        listener_registered = False

        if message["reply_required"]:
            listener_registered = True

            async def _listener(result: dict[str, Any]) -> None:
                await self._handle_runtime_result(message, result)

        request_id = await runtime.enqueue_api_text(
            bridge_prompt,
            source=f"bridge:{message['message_id']}",
            deliver_to_telegram=False,
        )
        if request_id is None:
            failure = "failed to enqueue bridge request"
            self.store.update_message_status(message["message_id"], "failed", error_text=failure)
            self.store.update_thread_status(message["thread_id"], "failed")
            raise RuntimeError(failure)

        if listener_registered:
            runtime.register_request_listener(request_id, _listener)
            self.store.update_message_status(message["message_id"], "queued", result_text=request_id)
            self.store.update_thread_status(message["thread_id"], "waiting_reply")
        else:
            self.store.update_message_status(message["message_id"], "delivered", result_text=request_id)
            self.store.update_thread_status(message["thread_id"], "delivered")

        return {
            "ok": True,
            "message_id": message["message_id"],
            "thread_id": message["thread_id"],
            "request_id": request_id,
            "status": "queued" if message["reply_required"] else "delivered",
        }

    async def submit_reply(self, payload: dict[str, Any]) -> dict[str, Any]:
        reply = validate_reply_payload(payload)
        request_message = self.store.get_message(reply["in_reply_to"])
        if request_message is None:
            raise ValueError(f"in_reply_to message not found: {reply['in_reply_to']}")
        if request_message["thread_id"] != reply["thread_id"]:
            raise ValueError("reply thread_id does not match the original request")

        allowed, reason = self.directory.check_reply_permission(reply, request_message)
        if not allowed:
            self.store.record_permission_audit(reply["message_id"], "deny", reason)
            raise PermissionError(reason)

        _print_bridge_message(
            reply["from_agent"], reply["to_agent"],
            reply.get("result_text") or reply.get("error_text") or "",
            kind="reply",
        )
        self.store.save_message(
            reply,
            status=reply["status"],
            result_text=reply.get("result_text"),
            error_text=reply.get("error_text"),
        )
        request_status = "completed" if reply["status"] == "ok" else reply["status"]
        self.store.update_message_status(
            request_message["message_id"],
            request_status,
            result_text=reply.get("result_text"),
            error_text=reply.get("error_text"),
        )
        self.store.update_thread_status(reply["thread_id"], request_status)
        self.store.record_permission_audit(reply["message_id"], "allow", "manual reply accepted")
        return {
            "ok": True,
            "message_id": reply["message_id"],
            "thread_id": reply["thread_id"],
            "status": reply["status"],
        }

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        return self.store.get_message(message_id)

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        return self.store.get_thread(thread_id)

    def get_capability(self, agent_name: str) -> dict[str, Any] | None:
        return self.directory.capability_view(agent_name)

    async def _handle_runtime_result(self, request_message: dict[str, Any], result: dict[str, Any]) -> None:
        reply = build_result_reply(
            request_message,
            success=bool(result.get("success")),
            text=result.get("text"),
            error=result.get("error"),
        )
        reply = validate_reply_payload(reply)
        reply_text = result.get("text") or result.get("error") or ""
        _print_bridge_message(
            request_message["to_agent"], request_message["from_agent"],
            reply_text, kind="reply",
        )
        self.store.save_message(
            reply,
            status=reply["status"],
            result_text=reply.get("result_text"),
            error_text=reply.get("error_text"),
        )
        self.store.record_permission_audit(reply["message_id"], "allow", "runtime reply captured")
        request_status = "completed" if result.get("success") else "failed"
        self.store.update_message_status(
            request_message["message_id"],
            request_status,
            result_text=reply.get("result_text"),
            error_text=reply.get("error_text"),
        )
        self.store.update_thread_status(request_message["thread_id"], request_status)

    def _render_prompt(self, message: dict[str, Any]) -> str:
        reply_required = "yes" if message["reply_required"] else "no"
        return (
            f"Bridge message from agent `{message['from_agent']}`\n"
            f"Thread: {message['thread_id']}\n"
            f"Bridge message id: {message['message_id']}\n"
            f"Intent: {message['intent']}\n"
            f"Reply required: {reply_required}\n\n"
            "Task:\n"
            f"{message['text']}\n\n"
            "Respond directly to this task. The bridge layer will capture your final response "
            "and route it back to the requesting agent thread."
        )
