# Hashi Mock Testing System Design

## Overview

A comprehensive testing framework that allows testing all runtime logic, information flow, and orchestration **without real API keys or CLI authentication**.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      TEST HARNESS                                │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │  Mock CLI    │  │  Mock HTTP   │  │  Mock        │          │
│  │  Backends    │  │  Server      │  │  Telegram    │          │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘          │
│         │                 │                 │                   │
│         ▼                 ▼                 ▼                   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              ADAPTER LAYER (Real Code)                   │   │
│  │   GeminiCLI  │  ClaudeCLI  │  CodexCLI  │  OpenRouter   │   │
│  └─────────────────────────────────────────────────────────┘   │
│         │                                                       │
│         ▼                                                       │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              ORCHESTRATOR (Real Code)                    │   │
│  │   AgentRuntime  │  Scheduler  │  SkillManager           │   │
│  └─────────────────────────────────────────────────────────┘   │
│         │                                                       │
│         ▼                                                       │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              TEST ASSERTIONS & METRICS                   │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Component 1: Mock CLI Scripts

Create fake CLI executables that mimic real CLI behavior.

### `tests/mocks/bin/gemini` (Mock Gemini CLI)
```bash
#!/usr/bin/env bash
# Mock Gemini CLI for testing

# Parse arguments
PROMPT=""
MODEL="gemini-2.5-flash"
while [[ $# -gt 0 ]]; do
    case "$1" in
        -m|--model) MODEL="$2"; shift 2 ;;
        -p|--prompt) PROMPT="$2"; shift 2 ;;
        --version) echo "0.33.0 (mock)"; exit 0 ;;
        --sandbox) shift ;;
        -y|--yolo) shift ;;
        *) PROMPT="$1"; shift ;;
    esac
done

# Simulate processing delay
sleep 0.5

# Return canned response based on prompt patterns
if [[ "$PROMPT" == *"hello"* ]] || [[ "$PROMPT" == *"Hello"* ]]; then
    echo "Hello! I'm the mock Gemini CLI. How can I help you today?"
elif [[ "$PROMPT" == *"error"* ]]; then
    echo "ERROR: Simulated error for testing" >&2
    exit 1
elif [[ "$PROMPT" == *"code"* ]] || [[ "$PROMPT" == *"write"* ]]; then
    cat << 'EOF'
Here's the code you requested:

```python
def hello_world():
    print("Hello from mock Gemini!")
    return True
```

This function prints a greeting and returns True.
EOF
elif [[ "$PROMPT" == *"slow"* ]]; then
    # Simulate slow response for timeout testing
    sleep 10
    echo "This was a slow response."
else
    echo "Mock response from Gemini CLI (model: $MODEL). Your prompt was: ${PROMPT:0:50}..."
fi
```

### `tests/mocks/bin/claude` (Mock Claude CLI)
```bash
#!/usr/bin/env bash
# Mock Claude Code CLI for testing

PROMPT=""
PRINT_MODE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) echo "2.1.74 (Claude Code) [mock]"; exit 0 ;;
        -p|--print) PRINT_MODE=true; PROMPT="$2"; shift 2 ;;
        --model) shift 2 ;;
        --allowedTools) shift 2 ;;
        --max-turns) shift 2 ;;
        login) echo "Mock: Would open browser for login"; exit 0 ;;
        *) PROMPT="$PROMPT $1"; shift ;;
    esac
done

sleep 0.3

if [[ "$PROMPT" == *"error"* ]]; then
    echo '{"error": "Simulated error"}' >&2
    exit 1
fi

if $PRINT_MODE; then
    echo "Mock Claude response: Processed your request about: ${PROMPT:0:80}"
else
    # Interactive mode - just echo back
    echo "Claude Code (mock) ready. Prompt: $PROMPT"
fi
```

### `tests/mocks/bin/codex` (Mock Codex CLI)
```bash
#!/usr/bin/env bash
# Mock Codex CLI for testing

ACTION=""
PROMPT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) echo "codex-cli 0.114.0 (mock)"; exit 0 ;;
        exec|e) ACTION="exec"; shift ;;
        login) echo "Mock: Login not required in test mode"; exit 0 ;;
        --json) shift ;;
        --ephemeral) shift ;;
        --skip-git-repo-check) shift ;;
        --dangerously-bypass-*) shift ;;
        --add-dir) shift 2 ;;
        --output-last-message) shift 2 ;;
        --model) shift 2 ;;
        -c) shift 2 ;;
        --) shift; PROMPT="$*"; break ;;
        *) PROMPT="$PROMPT $1"; shift ;;
    esac
done

# Read from stdin if prompt is "-"
if [[ "$PROMPT" == "-" ]] || [[ -z "$PROMPT" ]]; then
    PROMPT=$(cat)
fi

sleep 0.4

# Output JSONL events like real Codex
echo '{"type":"turn.started","item":{"type":"thinking"}}'
sleep 0.1
echo '{"type":"item.completed","item":{"type":"agent_message","text":"Mock Codex processed your request: '"${PROMPT:0:100}"'"}}'

exit 0
```

---

## Component 2: Mock Adapter Classes

Python mock adapters for unit testing.

### `tests/mocks/mock_adapters.py`
```python
"""Mock adapters for testing without real CLI/API backends."""

from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable, List, Dict, Any

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse
from adapters.stream_events import StreamCallback, StreamEvent, KIND_TEXT_DELTA


@dataclass
class MockScenario:
    """Defines a mock response scenario."""
    pattern: str  # regex or substring to match in prompt
    response: str
    delay_ms: int = 100
    error: Optional[str] = None
    exit_code: int = 0


class MockBackend(BaseBackend):
    """
    A fully controllable mock backend for testing.
    
    Usage:
        mock = MockBackend(config, global_config)
        mock.add_scenario(MockScenario(pattern="hello", response="Hi there!"))
        mock.add_scenario(MockScenario(pattern="error", error="Simulated failure"))
    """
    
    DEFAULT_SCENARIOS = [
        MockScenario(pattern="", response="Mock response: I processed your request."),
    ]
    
    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.scenarios: List[MockScenario] = list(self.DEFAULT_SCENARIOS)
        self.call_history: List[Dict[str, Any]] = []
        self.initialized = False
        
    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=True,
            supports_files=True,
            supports_tool_use=True,
            supports_thinking_stream=True,
            supports_headless_mode=True,
        )
    
    def add_scenario(self, scenario: MockScenario):
        """Add a response scenario (checked in order, first match wins)."""
        self.scenarios.insert(0, scenario)  # Higher priority for newer scenarios
        
    def clear_scenarios(self):
        """Reset to default scenarios."""
        self.scenarios = list(self.DEFAULT_SCENARIOS)
        
    def get_call_history(self) -> List[Dict[str, Any]]:
        """Get all calls made to this backend."""
        return self.call_history
    
    def _find_scenario(self, prompt: str) -> MockScenario:
        """Find matching scenario for prompt."""
        import re
        for scenario in self.scenarios:
            if not scenario.pattern:  # Empty pattern = default/fallback
                continue
            try:
                if re.search(scenario.pattern, prompt, re.IGNORECASE):
                    return scenario
            except re.error:
                if scenario.pattern.lower() in prompt.lower():
                    return scenario
        # Return default (last one with empty pattern)
        return self.scenarios[-1]
    
    async def initialize(self) -> bool:
        self.initialized = True
        return True
    
    async def generate_response(
        self,
        prompt: str,
        request_id: str,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event: StreamCallback = None,
    ) -> BackendResponse:
        started = time.perf_counter()
        
        # Record this call
        self.call_history.append({
            "prompt": prompt,
            "request_id": request_id,
            "is_retry": is_retry,
            "timestamp": time.time(),
        })
        
        scenario = self._find_scenario(prompt)
        
        # Simulate delay
        await asyncio.sleep(scenario.delay_ms / 1000)
        
        duration_ms = (time.perf_counter() - started) * 1000
        
        # Check for error scenario
        if scenario.error:
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                error=scenario.error,
                is_success=False,
            )
        
        # Stream events if callback provided
        if on_stream_event:
            words = scenario.response.split()
            for i, word in enumerate(words[:5]):  # Stream first 5 words
                await on_stream_event(StreamEvent(
                    kind=KIND_TEXT_DELTA,
                    summary=word + " ",
                ))
                await asyncio.sleep(0.01)
        
        return BackendResponse(
            text=scenario.response,
            duration_ms=duration_ms,
            is_success=True,
        )
    
    async def shutdown(self):
        self.initialized = False
    
    async def handle_new_session(self) -> bool:
        self.call_history.clear()
        return True


class RecordingBackend(BaseBackend):
    """
    Wraps a real backend and records all interactions.
    Useful for creating test fixtures from real API calls.
    """
    
    def __init__(self, wrapped_backend: BaseBackend):
        self.wrapped = wrapped_backend
        self.recordings: List[Dict[str, Any]] = []
        
    def _define_capabilities(self) -> BackendCapabilities:
        return self.wrapped.capabilities
    
    async def initialize(self) -> bool:
        return await self.wrapped.initialize()
    
    async def generate_response(self, prompt: str, request_id: str, **kwargs) -> BackendResponse:
        response = await self.wrapped.generate_response(prompt, request_id, **kwargs)
        self.recordings.append({
            "prompt": prompt,
            "response": response.text,
            "error": response.error,
            "duration_ms": response.duration_ms,
        })
        return response
    
    async def shutdown(self):
        await self.wrapped.shutdown()
    
    async def handle_new_session(self) -> bool:
        return await self.wrapped.handle_new_session()
    
    def export_recordings(self, path: str):
        """Export recordings as JSON for replay tests."""
        import json
        with open(path, 'w') as f:
            json.dump(self.recordings, f, indent=2)
```

---

## Component 3: Mock Telegram Bot

### `tests/mocks/mock_telegram.py`
```python
"""Mock Telegram bot for testing without real Telegram API."""

from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Callable, Awaitable
from unittest.mock import MagicMock, AsyncMock


@dataclass
class MockMessage:
    message_id: int
    chat_id: int
    text: str
    from_user_id: int = 123456789
    reply_to_message_id: Optional[int] = None
    

@dataclass
class MockChat:
    id: int
    type: str = "private"
    

@dataclass
class MockUpdate:
    update_id: int
    message: Optional[MockMessage] = None
    callback_query: Optional[Any] = None
    

class MockTelegramBot:
    """
    Simulates Telegram Bot API for testing.
    
    Usage:
        bot = MockTelegramBot()
        
        # Simulate incoming message
        await bot.simulate_message("Hello bot!", chat_id=12345)
        
        # Check what the bot sent
        assert len(bot.sent_messages) == 1
        assert "response" in bot.sent_messages[0]["text"]
    """
    
    def __init__(self, token: str = "mock_token"):
        self.token = token
        self.sent_messages: List[Dict[str, Any]] = []
        self.sent_photos: List[Dict[str, Any]] = []
        self.sent_documents: List[Dict[str, Any]] = []
        self.sent_voices: List[Dict[str, Any]] = []
        self.message_handlers: List[Callable] = []
        self.command_handlers: Dict[str, Callable] = {}
        self._update_id = 0
        self._message_id = 0
        
    def _next_update_id(self) -> int:
        self._update_id += 1
        return self._update_id
    
    def _next_message_id(self) -> int:
        self._message_id += 1
        return self._message_id
    
    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = None,
        reply_to_message_id: int = None,
        reply_markup: Any = None,
        **kwargs
    ) -> MockMessage:
        """Record a sent message."""
        msg = MockMessage(
            message_id=self._next_message_id(),
            chat_id=chat_id,
            text=text,
        )
        self.sent_messages.append({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_to_message_id": reply_to_message_id,
            "reply_markup": reply_markup,
            **kwargs
        })
        return msg
    
    async def send_photo(self, chat_id: int, photo: Any, caption: str = None, **kwargs):
        self.sent_photos.append({"chat_id": chat_id, "photo": photo, "caption": caption, **kwargs})
        return MockMessage(self._next_message_id(), chat_id, caption or "")
    
    async def send_document(self, chat_id: int, document: Any, caption: str = None, **kwargs):
        self.sent_documents.append({"chat_id": chat_id, "document": document, "caption": caption, **kwargs})
        return MockMessage(self._next_message_id(), chat_id, caption or "")
    
    async def send_voice(self, chat_id: int, voice: Any, **kwargs):
        self.sent_voices.append({"chat_id": chat_id, "voice": voice, **kwargs})
        return MockMessage(self._next_message_id(), chat_id, "")
    
    async def simulate_message(
        self,
        text: str,
        chat_id: int = 123456789,
        user_id: int = 123456789,
    ) -> MockUpdate:
        """Simulate an incoming user message."""
        update = MockUpdate(
            update_id=self._next_update_id(),
            message=MockMessage(
                message_id=self._next_message_id(),
                chat_id=chat_id,
                text=text,
                from_user_id=user_id,
            )
        )
        
        # Dispatch to handlers
        for handler in self.message_handlers:
            await handler(update, None)  # context is None in mock
            
        return update
    
    async def simulate_command(self, command: str, args: str = "", chat_id: int = 123456789):
        """Simulate a command like /status or /model gemini-2.5-flash"""
        text = f"/{command}" + (f" {args}" if args else "")
        return await self.simulate_message(text, chat_id)
    
    def clear_history(self):
        """Clear all sent message history."""
        self.sent_messages.clear()
        self.sent_photos.clear()
        self.sent_documents.clear()
        self.sent_voices.clear()
        
    def get_last_response(self) -> Optional[str]:
        """Get the text of the last sent message."""
        if self.sent_messages:
            return self.sent_messages[-1]["text"]
        return None


def create_mock_application(bot: MockTelegramBot):
    """Create a mock telegram.ext.Application for testing."""
    app = MagicMock()
    app.bot = bot
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.updater = MagicMock()
    app.updater.start_polling = AsyncMock()
    app.updater.stop = AsyncMock()
    return app
```

---

## Component 4: Test Fixtures & Configuration

### `tests/fixtures/test_config.json`
```json
{
  "global": {
    "project_root": "/tmp/hashi_test",
    "gemini_cmd": "tests/mocks/bin/gemini",
    "claude_cmd": "tests/mocks/bin/claude",
    "codex_cmd": "tests/mocks/bin/codex",
    "workbench_port": 28800,
    "api_gateway_port": 28801,
    "authorized_id": 123456789
  },
  "agents": [
    {
      "name": "test-agent",
      "type": "flex",
      "display_name": "Test Agent",
      "workspace_dir": "/tmp/hashi_test/workspaces/test",
      "active_backend": "gemini-cli",
      "allowed_backends": [
        {"engine": "gemini-cli", "model": "gemini-2.5-flash"},
        {"engine": "claude-cli", "model": "claude-sonnet-4-5"}
      ],
      "telegram_token_key": "test_agent_token",
      "is_active": true
    }
  ]
}
```

### `tests/fixtures/test_secrets.json`
```json
{
  "test_agent_token": "123456789:MOCK_TELEGRAM_TOKEN_FOR_TESTING",
  "openrouter_key": "sk-mock-openrouter-key"
}
```

---

## Component 5: Integration Test Harness

### `tests/test_harness.py`
```python
"""
Integration test harness for Hashi.

Runs the full orchestrator with mock backends and validates behavior.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.mocks.mock_adapters import MockBackend, MockScenario
from tests.mocks.mock_telegram import MockTelegramBot, create_mock_application


class TestHarness:
    """
    Orchestrates end-to-end testing with mocked externals.
    """
    
    def __init__(self, config_path: Optional[Path] = None):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="hashi_test_"))
        self.config_path = config_path or Path(__file__).parent / "fixtures" / "test_config.json"
        self.mock_bots: dict[str, MockTelegramBot] = {}
        self.mock_backends: dict[str, MockBackend] = {}
        
    async def setup(self):
        """Initialize test environment."""
        # Create workspace directories
        (self.temp_dir / "workspaces" / "test").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "state").mkdir(parents=True, exist_ok=True)
        
        # Copy and patch config
        config = json.loads(self.config_path.read_text())
        config["global"]["project_root"] = str(self.temp_dir)
        
        for agent in config["agents"]:
            agent["workspace_dir"] = str(self.temp_dir / "workspaces" / agent["name"])
            Path(agent["workspace_dir"]).mkdir(parents=True, exist_ok=True)
            
        self.runtime_config_path = self.temp_dir / "agents.json"
        self.runtime_config_path.write_text(json.dumps(config, indent=2))
        
        print(f"[TestHarness] Initialized in {self.temp_dir}")
        
    async def teardown(self):
        """Cleanup test environment."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        print("[TestHarness] Cleaned up")
        
    def create_mock_bot(self, agent_name: str) -> MockTelegramBot:
        """Create a mock Telegram bot for an agent."""
        bot = MockTelegramBot()
        self.mock_bots[agent_name] = bot
        return bot
    
    def create_mock_backend(self, agent_name: str, config, global_config) -> MockBackend:
        """Create a mock backend for an agent."""
        backend = MockBackend(config, global_config)
        self.mock_backends[agent_name] = backend
        return backend
    
    async def simulate_conversation(
        self,
        agent_name: str,
        messages: list[str],
        expected_patterns: list[str] = None,
    ) -> list[str]:
        """
        Simulate a conversation and return responses.
        
        Args:
            agent_name: Name of the agent to test
            messages: List of user messages to send
            expected_patterns: Optional regex patterns to validate responses
            
        Returns:
            List of bot responses
        """
        bot = self.mock_bots.get(agent_name)
        if not bot:
            raise ValueError(f"No mock bot for agent {agent_name}")
            
        responses = []
        for msg in messages:
            bot.clear_history()
            await bot.simulate_message(msg)
            await asyncio.sleep(0.1)  # Allow processing
            response = bot.get_last_response()
            responses.append(response)
            
            if expected_patterns:
                import re
                pattern = expected_patterns[len(responses) - 1]
                if pattern and not re.search(pattern, response or ""):
                    raise AssertionError(
                        f"Response '{response}' doesn't match pattern '{pattern}'"
                    )
                    
        return responses


# === Test Cases ===

async def test_basic_message_flow():
    """Test that messages flow through the system correctly."""
    harness = TestHarness()
    await harness.setup()
    
    try:
        bot = harness.create_mock_bot("test-agent")
        
        # Simulate user message
        await bot.simulate_message("Hello, how are you?")
        
        # The mock backend should have been called
        # (In full integration, we'd verify the backend received the message)
        print("✓ Basic message flow test passed")
        
    finally:
        await harness.teardown()


async def test_error_handling():
    """Test that errors are handled gracefully."""
    harness = TestHarness()
    await harness.setup()
    
    try:
        from tests.mocks.mock_adapters import MockBackend, MockScenario
        
        # Create a backend that will error
        class ErrorConfig:
            name = "error-test"
            workspace_dir = harness.temp_dir / "workspaces" / "error"
            extra = {}
            def resolve_access_root(self): return self.workspace_dir
            
        class ErrorGlobalConfig:
            pass
            
        backend = MockBackend(ErrorConfig(), ErrorGlobalConfig())
        backend.add_scenario(MockScenario(
            pattern=".*",
            response="",
            error="Simulated backend failure"
        ))
        
        response = await backend.generate_response("test", "req-1")
        
        assert not response.is_success
        assert response.error == "Simulated backend failure"
        print("✓ Error handling test passed")
        
    finally:
        await harness.teardown()


async def test_backend_switching():
    """Test switching between different backends."""
    harness = TestHarness()
    await harness.setup()
    
    try:
        # Create mock backends for different engines
        class TestConfig:
            name = "switch-test"
            workspace_dir = harness.temp_dir / "workspaces" / "switch"
            model = "test-model"
            extra = {}
            def resolve_access_root(self): return self.workspace_dir
            
        class TestGlobal:
            pass
        
        gemini_backend = MockBackend(TestConfig(), TestGlobal())
        gemini_backend.add_scenario(MockScenario(
            pattern=".*",
            response="Response from Gemini mock"
        ))
        
        claude_backend = MockBackend(TestConfig(), TestGlobal())
        claude_backend.add_scenario(MockScenario(
            pattern=".*",
            response="Response from Claude mock"
        ))
        
        # Test both backends
        r1 = await gemini_backend.generate_response("test", "req-1")
        r2 = await claude_backend.generate_response("test", "req-2")
        
        assert "Gemini" in r1.text
        assert "Claude" in r2.text
        print("✓ Backend switching test passed")
        
    finally:
        await harness.teardown()


async def test_streaming_responses():
    """Test streaming response events."""
    harness = TestHarness()
    await harness.setup()
    
    try:
        from tests.mocks.mock_adapters import MockBackend
        
        class TestConfig:
            name = "stream-test"
            workspace_dir = harness.temp_dir / "workspaces" / "stream"
            extra = {}
            def resolve_access_root(self): return self.workspace_dir
            
        backend = MockBackend(TestConfig(), type("GlobalConfig", (), {})())
        backend.add_scenario(MockScenario(
            pattern=".*",
            response="This is a streaming test response"
        ))
        
        events = []
        async def on_event(event):
            events.append(event)
            
        response = await backend.generate_response(
            "test streaming",
            "req-stream",
            on_stream_event=on_event
        )
        
        assert len(events) > 0, "Should have received stream events"
        assert response.is_success
        print(f"✓ Streaming test passed ({len(events)} events)")
        
    finally:
        await harness.teardown()


async def test_call_history_recording():
    """Test that backends record call history for analysis."""
    harness = TestHarness()
    await harness.setup()
    
    try:
        class TestConfig:
            name = "history-test"
            workspace_dir = harness.temp_dir / "workspaces" / "history"
            extra = {}
            def resolve_access_root(self): return self.workspace_dir
            
        backend = MockBackend(TestConfig(), type("GlobalConfig", (), {})())
        
        # Make several calls
        await backend.generate_response("First message", "req-1")
        await backend.generate_response("Second message", "req-2")
        await backend.generate_response("Third message", "req-3")
        
        history = backend.get_call_history()
        
        assert len(history) == 3
        assert history[0]["prompt"] == "First message"
        assert history[2]["request_id"] == "req-3"
        print("✓ Call history recording test passed")
        
    finally:
        await harness.teardown()


# === Run All Tests ===

async def run_all_tests():
    """Run all tests and report results."""
    tests = [
        ("Basic Message Flow", test_basic_message_flow),
        ("Error Handling", test_error_handling),
        ("Backend Switching", test_backend_switching),
        ("Streaming Responses", test_streaming_responses),
        ("Call History Recording", test_call_history_recording),
    ]
    
    print("\n" + "=" * 60)
    print("  HASHI MOCK TEST SUITE")
    print("=" * 60 + "\n")
    
    passed = 0
    failed = 0
    
    for name, test_fn in tests:
        try:
            print(f"Running: {name}...")
            await test_fn()
            passed += 1
        except Exception as e:
            print(f"✗ {name} FAILED: {e}")
            failed += 1
            
    print("\n" + "=" * 60)
    print(f"  Results: {passed} passed, {failed} failed")
    print("=" * 60 + "\n")
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
```

---

## Component 6: HTTP Mock Server (for OpenRouter API testing)

### `tests/mocks/mock_http_server.py`
```python
"""Mock HTTP server for testing API-based backends."""

import asyncio
import json
from aiohttp import web
from typing import Dict, List, Any


class MockOpenRouterServer:
    """
    Mock OpenRouter API server for testing.
    
    Mimics the OpenRouter API at /api/v1/chat/completions
    """
    
    def __init__(self, port: int = 28888):
        self.port = port
        self.app = web.Application()
        self.app.router.add_post("/api/v1/chat/completions", self.handle_chat)
        self.app.router.add_get("/api/v1/models", self.handle_models)
        self.runner = None
        self.site = None
        self.requests: List[Dict[str, Any]] = []
        self.response_override: str = None
        
    async def handle_chat(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(body)
        
        messages = body.get("messages", [])
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"),
            "No message"
        )
        
        response_text = self.response_override or f"Mock response to: {last_user[:50]}"
        
        if body.get("stream"):
            # SSE streaming response
            async def stream_response():
                chunks = response_text.split()
                for i, word in enumerate(chunks):
                    chunk = {
                        "id": "mock-chat-123",
                        "object": "chat.completion.chunk",
                        "choices": [{
                            "index": 0,
                            "delta": {"content": word + " "},
                            "finish_reason": None if i < len(chunks) - 1 else "stop"
                        }]
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.01)
                yield "data: [DONE]\n\n"
                
            return web.Response(
                body=b"".join([chunk.encode() async for chunk in stream_response()]),
                content_type="text/event-stream"
            )
        else:
            return web.json_response({
                "id": "mock-chat-123",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": response_text},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
            })
    
    async def handle_models(self, request: web.Request) -> web.Response:
        return web.json_response({
            "data": [
                {"id": "openai/gpt-4o", "name": "GPT-4o"},
                {"id": "anthropic/claude-3.5-sonnet", "name": "Claude 3.5 Sonnet"},
                {"id": "google/gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
            ]
        })
    
    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", self.port)
        await self.site.start()
        print(f"[MockOpenRouter] Listening on http://127.0.0.1:{self.port}")
        
    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
        print("[MockOpenRouter] Stopped")
        
    def set_response(self, text: str):
        """Set a custom response for all requests."""
        self.response_override = text
        
    def clear_requests(self):
        """Clear recorded requests."""
        self.requests.clear()
```

---

## Usage Examples

### Running Unit Tests
```bash
cd /path/to/hashi
python -m pytest tests/ -v
```

### Running Integration Tests
```bash
cd /path/to/hashi
python tests/test_harness.py
```

### Using Mock CLI in Development
```bash
# Make mock CLIs executable
chmod +x tests/mocks/bin/*

# Set PATH to use mocks
export PATH="$(pwd)/tests/mocks/bin:$PATH"

# Now gemini/claude/codex commands use mocks
gemini --version  # → "0.33.0 (mock)"
```

### Running with Mock Config
```bash
# Use test configuration
python main.py --bridge-home tests/fixtures
```

---

## Test Coverage Goals

| Component | Coverage Target | Mock Strategy |
|-----------|-----------------|---------------|
| Adapters (CLI) | 90% | Mock CLI scripts |
| Adapters (API) | 90% | Mock HTTP server |
| Orchestrator | 85% | MockBackend + MockTelegram |
| AgentRuntime | 85% | Full mock injection |
| Scheduler | 80% | Time mocking |
| Workbench API | 75% | Integration tests |
| API Gateway | 80% | Mock HTTP server |

---

## Benefits of This Design

1. **No External Dependencies**: Tests run without internet, API keys, or CLI auth
2. **Deterministic**: Mock responses are predictable and reproducible
3. **Fast**: No network latency or real API calls
4. **Comprehensive**: Can test error paths, timeouts, edge cases
5. **CI/CD Ready**: Can run in any environment (GitHub Actions, etc.)
6. **Recording Mode**: Can capture real API calls to create fixtures

---

*Designed by 小蕾 for 爸爸's hashi project* 🌸
