"""Mock adapters for testing without real CLI/API backends."""

from __future__ import annotations
import asyncio
import time
import sys
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

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
        self.scenarios.insert(0, scenario)
        
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
            if not scenario.pattern:
                continue
            try:
                if re.search(scenario.pattern, prompt, re.IGNORECASE):
                    return scenario
            except re.error:
                if scenario.pattern.lower() in prompt.lower():
                    return scenario
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
        
        self.call_history.append({
            "prompt": prompt,
            "request_id": request_id,
            "is_retry": is_retry,
            "timestamp": time.time(),
        })
        
        scenario = self._find_scenario(prompt)
        await asyncio.sleep(scenario.delay_ms / 1000)
        duration_ms = (time.perf_counter() - started) * 1000
        
        if scenario.error:
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                error=scenario.error,
                is_success=False,
            )
        
        if on_stream_event:
            words = scenario.response.split()
            for word in words[:5]:
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
        # Don't call super().__init__ - we're a wrapper
        
    def _define_capabilities(self) -> BackendCapabilities:
        return self.wrapped.capabilities
    
    @property
    def capabilities(self):
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


class SimpleTestConfig:
    """Simple config object for testing."""
    def __init__(self, name: str = "test-agent", workspace_dir: str = "/tmp/test"):
        self.name = name
        self.workspace_dir = Path(workspace_dir)
        self.model = "test-model"
        self.extra = {}
        
    def resolve_access_root(self):
        return self.workspace_dir


class SimpleGlobalConfig:
    """Simple global config for testing."""
    def __init__(self):
        self.project_root = Path("/tmp/test")
        self.gemini_cmd = "gemini"
        self.claude_cmd = "claude"
        self.codex_cmd = "codex"
