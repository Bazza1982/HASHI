#!/usr/bin/env python3
"""
Tests for bot commands (/status, /model, /new, /help, etc.)

These tests verify the command handling logic without real Telegram.
"""

import asyncio
import sys
from pathlib import Path
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, List, Dict

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.mocks.mock_adapters import MockBackend, MockScenario, SimpleTestConfig, SimpleGlobalConfig
from tests.mocks.test_logger import TestLogger


# === Mock Runtime for Command Testing ===

class MockAgentRuntime:
    """
    Mock runtime that implements command handlers for testing.
    Simulates the real AgentRuntime's command interface.
    """
    
    def __init__(self, name: str = "test-agent"):
        self.name = name
        self.global_config = SimpleNamespace(
            authorized_id=123456789,
            project_root=Path("/tmp/test"),
        )
        
        # State
        self.current_model = "gemini-2.5-flash"
        self.current_backend = "gemini-cli"
        self.thinking_enabled = False
        self.verbose_enabled = False
        self.voice_enabled = False
        self.is_active = True
        self.session_started = True
        
        # Stats
        self.total_requests = 42
        self.total_tokens = 15000
        self.session_start_time = "2026-03-12T10:00:00"
        
        # Command history for testing
        self.command_log: List[Dict[str, Any]] = []
        
    async def cmd_help(self, update, context):
        """Show available commands."""
        help_text = """Available commands:
/status - Show current status
/model [name] - Show or change model
/backend [name] - Show or change backend
/new - Start new session
/clear - Clear context
/think - Toggle thinking mode
/verbose - Toggle verbose mode
/voice - Toggle voice mode
/stop - Stop the agent
/help - Show this help"""
        await update.message.reply_text(help_text)
        self.command_log.append({"cmd": "help", "args": context.args})
        
    async def cmd_status(self, update, context):
        """Show agent status."""
        status_text = f"""🤖 Agent: {self.name}
📊 Status: {"Active" if self.is_active else "Inactive"}
🧠 Model: {self.current_model}
⚙️ Backend: {self.current_backend}
💭 Thinking: {"ON" if self.thinking_enabled else "OFF"}
📝 Verbose: {"ON" if self.verbose_enabled else "OFF"}
🔊 Voice: {"ON" if self.voice_enabled else "OFF"}
📈 Requests: {self.total_requests}
🎫 Tokens: {self.total_tokens}
⏰ Session: {self.session_start_time}"""
        await update.message.reply_text(status_text)
        self.command_log.append({"cmd": "status", "args": context.args})
        
    async def cmd_model(self, update, context):
        """Show or change model."""
        if context.args:
            new_model = context.args[0]
            old_model = self.current_model
            self.current_model = new_model
            await update.message.reply_text(f"Model changed: {old_model} → {new_model}")
        else:
            await update.message.reply_text(f"Current model: {self.current_model}")
        self.command_log.append({"cmd": "model", "args": context.args})
        
    async def cmd_backend(self, update, context):
        """Show or change backend."""
        if context.args:
            new_backend = context.args[0]
            old_backend = self.current_backend
            self.current_backend = new_backend
            await update.message.reply_text(f"Backend changed: {old_backend} → {new_backend}")
        else:
            await update.message.reply_text(f"Current backend: {self.current_backend}")
        self.command_log.append({"cmd": "backend", "args": context.args})
        
    async def cmd_new(self, update, context):
        """Start new session."""
        self.session_started = True
        self.total_requests = 0
        self.total_tokens = 0
        await update.message.reply_text("✨ New session started! Context cleared.")
        self.command_log.append({"cmd": "new", "args": context.args})
        
    async def cmd_clear(self, update, context):
        """Clear context."""
        await update.message.reply_text("🧹 Context cleared.")
        self.command_log.append({"cmd": "clear", "args": context.args})
        
    async def cmd_think(self, update, context):
        """Toggle thinking mode."""
        self.thinking_enabled = not self.thinking_enabled
        status = "ON" if self.thinking_enabled else "OFF"
        await update.message.reply_text(f"💭 Thinking mode: {status}")
        self.command_log.append({"cmd": "think", "args": context.args})
        
    async def cmd_verbose(self, update, context):
        """Toggle verbose mode."""
        self.verbose_enabled = not self.verbose_enabled
        status = "ON" if self.verbose_enabled else "OFF"
        await update.message.reply_text(f"📝 Verbose mode: {status}")
        self.command_log.append({"cmd": "verbose", "args": context.args})
        
    async def cmd_voice(self, update, context):
        """Toggle voice mode."""
        self.voice_enabled = not self.voice_enabled
        status = "ON" if self.voice_enabled else "OFF"
        await update.message.reply_text(f"🔊 Voice mode: {status}")
        self.command_log.append({"cmd": "voice", "args": context.args})
        
    async def cmd_stop(self, update, context):
        """Stop the agent."""
        self.is_active = False
        await update.message.reply_text("🛑 Agent stopped.")
        self.command_log.append({"cmd": "stop", "args": context.args})
        
    async def cmd_retry(self, update, context):
        """Retry last request."""
        await update.message.reply_text("🔄 Retrying last request...")
        self.command_log.append({"cmd": "retry", "args": context.args})
        
    async def cmd_effort(self, update, context):
        """Set effort level."""
        if context.args:
            level = context.args[0]
            await update.message.reply_text(f"⚡ Effort level set to: {level}")
        else:
            await update.message.reply_text("Current effort: medium")
        self.command_log.append({"cmd": "effort", "args": context.args})


# === Command Executor (from admin_local_testing.py) ===

@dataclass
class CaptureStore:
    messages: List[Dict[str, Any]]

    async def capture_reply(self, text: str, **kwargs):
        self.messages.append({
            "channel": "reply",
            "text": text,
            "meta": kwargs,
        })
        return SimpleNamespace(ok=True)


class FakeMessage:
    def __init__(self, store: CaptureStore):
        self._store = store

    async def reply_text(self, text: str, **kwargs):
        return await self._store.capture_reply(text, **kwargs)


class FakeUpdate:
    def __init__(self, store: CaptureStore):
        self.effective_user = SimpleNamespace(id=123456789)
        self.effective_chat = SimpleNamespace(id=123456789)
        self.message = FakeMessage(store)


async def execute_command(runtime: MockAgentRuntime, command: str) -> Dict[str, Any]:
    """Execute a command and return the result."""
    # Parse command
    parts = command.strip().split()
    if not parts:
        return {"ok": False, "error": "empty command"}
    
    cmd_name = parts[0].lstrip("/")
    args = parts[1:]
    
    # Find handler
    method_name = f"cmd_{cmd_name}"
    method = getattr(runtime, method_name, None)
    
    if method is None:
        return {
            "ok": False, 
            "error": f"unknown command: {cmd_name}",
            "command": cmd_name,
        }
    
    # Execute
    store = CaptureStore(messages=[])
    update = FakeUpdate(store)
    context = SimpleNamespace(args=args)
    
    try:
        await method(update, context)
        return {
            "ok": True,
            "command": cmd_name,
            "args": args,
            "messages": store.messages,
            "response": store.messages[0]["text"] if store.messages else "",
        }
    except Exception as e:
        return {
            "ok": False,
            "command": cmd_name,
            "args": args,
            "error": str(e),
        }


# === Test Cases ===

async def test_help_command():
    """Test /help command."""
    runtime = MockAgentRuntime()
    result = await execute_command(runtime, "/help")
    
    assert result["ok"], f"Help command failed: {result.get('error')}"
    assert "Available commands" in result["response"]
    assert "/status" in result["response"]
    assert "/model" in result["response"]
    print("✓ /help command works")
    return True


async def test_status_command():
    """Test /status command."""
    runtime = MockAgentRuntime()
    result = await execute_command(runtime, "/status")
    
    assert result["ok"], f"Status command failed: {result.get('error')}"
    assert "Agent:" in result["response"]
    assert "Model:" in result["response"]
    assert "gemini-2.5-flash" in result["response"]
    print("✓ /status command works")
    return True


async def test_model_command_show():
    """Test /model command (show current)."""
    runtime = MockAgentRuntime()
    result = await execute_command(runtime, "/model")
    
    assert result["ok"], f"Model command failed: {result.get('error')}"
    assert "gemini-2.5-flash" in result["response"]
    print("✓ /model (show) command works")
    return True


async def test_model_command_change():
    """Test /model command (change model)."""
    runtime = MockAgentRuntime()
    
    # Change model
    result = await execute_command(runtime, "/model claude-sonnet-4")
    
    assert result["ok"], f"Model change failed: {result.get('error')}"
    assert "claude-sonnet-4" in result["response"]
    assert runtime.current_model == "claude-sonnet-4"
    print("✓ /model (change) command works")
    return True


async def test_backend_command():
    """Test /backend command."""
    runtime = MockAgentRuntime()
    
    # Show current
    result = await execute_command(runtime, "/backend")
    assert result["ok"]
    assert "gemini-cli" in result["response"]
    
    # Change backend
    result = await execute_command(runtime, "/backend claude-cli")
    assert result["ok"]
    assert runtime.current_backend == "claude-cli"
    print("✓ /backend command works")
    return True


async def test_new_session_command():
    """Test /new command."""
    runtime = MockAgentRuntime()
    runtime.total_requests = 100
    
    result = await execute_command(runtime, "/new")
    
    assert result["ok"], f"New command failed: {result.get('error')}"
    assert "New session" in result["response"]
    assert runtime.total_requests == 0  # Should be reset
    print("✓ /new command works")
    return True


async def test_clear_command():
    """Test /clear command."""
    runtime = MockAgentRuntime()
    result = await execute_command(runtime, "/clear")
    
    assert result["ok"], f"Clear command failed: {result.get('error')}"
    assert "cleared" in result["response"].lower()
    print("✓ /clear command works")
    return True


async def test_think_toggle():
    """Test /think toggle command."""
    runtime = MockAgentRuntime()
    assert runtime.thinking_enabled == False
    
    # Toggle on
    result = await execute_command(runtime, "/think")
    assert result["ok"]
    assert runtime.thinking_enabled == True
    assert "ON" in result["response"]
    
    # Toggle off
    result = await execute_command(runtime, "/think")
    assert result["ok"]
    assert runtime.thinking_enabled == False
    assert "OFF" in result["response"]
    
    print("✓ /think toggle works")
    return True


async def test_verbose_toggle():
    """Test /verbose toggle command."""
    runtime = MockAgentRuntime()
    assert runtime.verbose_enabled == False
    
    result = await execute_command(runtime, "/verbose")
    assert result["ok"]
    assert runtime.verbose_enabled == True
    
    print("✓ /verbose toggle works")
    return True


async def test_voice_toggle():
    """Test /voice toggle command."""
    runtime = MockAgentRuntime()
    assert runtime.voice_enabled == False
    
    result = await execute_command(runtime, "/voice")
    assert result["ok"]
    assert runtime.voice_enabled == True
    
    print("✓ /voice toggle works")
    return True


async def test_stop_command():
    """Test /stop command."""
    runtime = MockAgentRuntime()
    assert runtime.is_active == True
    
    result = await execute_command(runtime, "/stop")
    assert result["ok"]
    assert runtime.is_active == False
    assert "stopped" in result["response"].lower()
    
    print("✓ /stop command works")
    return True


async def test_retry_command():
    """Test /retry command."""
    runtime = MockAgentRuntime()
    result = await execute_command(runtime, "/retry")
    
    assert result["ok"]
    assert "retry" in result["response"].lower()
    print("✓ /retry command works")
    return True


async def test_effort_command():
    """Test /effort command."""
    runtime = MockAgentRuntime()
    
    # Show current
    result = await execute_command(runtime, "/effort")
    assert result["ok"]
    
    # Set level
    result = await execute_command(runtime, "/effort high")
    assert result["ok"]
    assert "high" in result["response"].lower()
    
    print("✓ /effort command works")
    return True


async def test_unknown_command():
    """Test handling of unknown command."""
    runtime = MockAgentRuntime()
    result = await execute_command(runtime, "/foobar")
    
    assert not result["ok"]
    assert "unknown" in result["error"].lower()
    print("✓ Unknown command handled correctly")
    return True


async def test_command_logging():
    """Test that commands are logged."""
    runtime = MockAgentRuntime()
    
    await execute_command(runtime, "/status")
    await execute_command(runtime, "/model gemini-pro")
    await execute_command(runtime, "/think")
    
    assert len(runtime.command_log) == 3
    assert runtime.command_log[0]["cmd"] == "status"
    assert runtime.command_log[1]["cmd"] == "model"
    assert runtime.command_log[1]["args"] == ["gemini-pro"]
    assert runtime.command_log[2]["cmd"] == "think"
    
    print("✓ Command logging works")
    return True


async def test_command_sequence():
    """Test a realistic command sequence."""
    runtime = MockAgentRuntime()
    
    # User workflow
    commands = [
        "/status",
        "/model claude-sonnet-4",
        "/think",
        "/verbose",
        "/new",
        "/status",
    ]
    
    for cmd in commands:
        result = await execute_command(runtime, cmd)
        assert result["ok"], f"Command {cmd} failed: {result.get('error')}"
    
    # Verify final state
    assert runtime.current_model == "claude-sonnet-4"
    assert runtime.thinking_enabled == True
    assert runtime.verbose_enabled == True
    assert runtime.total_requests == 0
    
    print("✓ Command sequence works")
    return True


# === Run All Tests ===

async def run_command_tests():
    """Run all command tests."""
    tests = [
        ("Help Command", test_help_command),
        ("Status Command", test_status_command),
        ("Model Show", test_model_command_show),
        ("Model Change", test_model_command_change),
        ("Backend Command", test_backend_command),
        ("New Session", test_new_session_command),
        ("Clear Context", test_clear_command),
        ("Think Toggle", test_think_toggle),
        ("Verbose Toggle", test_verbose_toggle),
        ("Voice Toggle", test_voice_toggle),
        ("Stop Command", test_stop_command),
        ("Retry Command", test_retry_command),
        ("Effort Command", test_effort_command),
        ("Unknown Command", test_unknown_command),
        ("Command Logging", test_command_logging),
        ("Command Sequence", test_command_sequence),
    ]
    
    print("\n" + "=" * 60)
    print("  COMMAND TESTS")
    print("=" * 60 + "\n")
    
    passed = 0
    failed = 0
    
    for name, test_fn in tests:
        try:
            await test_fn()
            passed += 1
        except AssertionError as e:
            print(f"✗ {name} FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {name} ERROR: {e}")
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"  Results: {passed} passed, {failed} failed")
    print("=" * 60 + "\n")
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_command_tests())
    sys.exit(0 if success else 1)
