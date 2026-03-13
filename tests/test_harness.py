#!/usr/bin/env python3
"""
Integration test harness for Hashi.

Runs tests with mock backends and validates behavior without real API keys.
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.mocks.mock_adapters import MockBackend, MockScenario, SimpleTestConfig, SimpleGlobalConfig


class TestHarness:
    """Orchestrates testing with mocked externals."""
    
    def __init__(self):
        self.test_dir = Path("/tmp/hashi_test")
        
    async def setup(self):
        self.test_dir.mkdir(parents=True, exist_ok=True)
        (self.test_dir / "workspaces").mkdir(exist_ok=True)
        print(f"[TestHarness] Setup complete: {self.test_dir}")
        
    async def teardown(self):
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)
        print("[TestHarness] Cleanup complete")


# === Test Cases ===

async def test_mock_backend_basic():
    """Test basic mock backend functionality."""
    config = SimpleTestConfig("test-basic", "/tmp/hashi_test/basic")
    global_config = SimpleGlobalConfig()
    
    backend = MockBackend(config, global_config)
    await backend.initialize()
    
    response = await backend.generate_response("Hello world", "req-001")
    
    assert response.is_success, "Response should be successful"
    assert response.text, "Response should have text"
    assert response.duration_ms > 0, "Duration should be recorded"
    
    print("✓ Basic mock backend test passed")


async def test_mock_backend_scenarios():
    """Test scenario-based responses."""
    config = SimpleTestConfig("test-scenarios", "/tmp/hashi_test/scenarios")
    global_config = SimpleGlobalConfig()
    
    backend = MockBackend(config, global_config)
    backend.add_scenario(MockScenario(
        pattern="greeting",
        response="Hello! Nice to meet you!",
        delay_ms=50
    ))
    backend.add_scenario(MockScenario(
        pattern="weather",
        response="The weather is sunny today.",
        delay_ms=50
    ))
    
    await backend.initialize()
    
    r1 = await backend.generate_response("What's a greeting?", "req-001")
    r2 = await backend.generate_response("How's the weather?", "req-002")
    
    assert "Hello" in r1.text, f"Should match greeting scenario, got: {r1.text}"
    assert "sunny" in r2.text, f"Should match weather scenario, got: {r2.text}"
    
    print("✓ Scenario-based responses test passed")


async def test_mock_backend_error():
    """Test error handling."""
    config = SimpleTestConfig("test-error", "/tmp/hashi_test/error")
    global_config = SimpleGlobalConfig()
    
    backend = MockBackend(config, global_config)
    backend.add_scenario(MockScenario(
        pattern="fail",
        response="",
        error="Simulated failure for testing"
    ))
    
    await backend.initialize()
    
    response = await backend.generate_response("Please fail", "req-err")
    
    assert not response.is_success, "Response should indicate failure"
    assert response.error == "Simulated failure for testing"
    
    print("✓ Error handling test passed")


async def test_call_history():
    """Test that call history is recorded."""
    config = SimpleTestConfig("test-history", "/tmp/hashi_test/history")
    global_config = SimpleGlobalConfig()
    
    backend = MockBackend(config, global_config)
    await backend.initialize()
    
    await backend.generate_response("First message", "req-1")
    await backend.generate_response("Second message", "req-2")
    await backend.generate_response("Third message", "req-3")
    
    history = backend.get_call_history()
    
    assert len(history) == 3, f"Should have 3 calls, got {len(history)}"
    assert history[0]["prompt"] == "First message"
    assert history[1]["request_id"] == "req-2"
    assert history[2]["prompt"] == "Third message"
    
    print("✓ Call history test passed")


async def test_streaming_events():
    """Test streaming event emission."""
    config = SimpleTestConfig("test-stream", "/tmp/hashi_test/stream")
    global_config = SimpleGlobalConfig()
    
    backend = MockBackend(config, global_config)
    backend.add_scenario(MockScenario(
        pattern="stream",
        response="This is a streaming response test",
        delay_ms=50
    ))
    
    await backend.initialize()
    
    events = []
    async def on_event(event):
        events.append(event)
    
    response = await backend.generate_response(
        "Test streaming please",
        "req-stream",
        on_stream_event=on_event
    )
    
    assert response.is_success
    assert len(events) > 0, f"Should have streaming events, got {len(events)}"
    
    print(f"✓ Streaming events test passed ({len(events)} events)")


async def test_new_session():
    """Test session reset."""
    config = SimpleTestConfig("test-session", "/tmp/hashi_test/session")
    global_config = SimpleGlobalConfig()
    
    backend = MockBackend(config, global_config)
    await backend.initialize()
    
    await backend.generate_response("Message 1", "req-1")
    await backend.generate_response("Message 2", "req-2")
    
    assert len(backend.get_call_history()) == 2
    
    await backend.handle_new_session()
    
    assert len(backend.get_call_history()) == 0, "History should be cleared"
    
    print("✓ Session reset test passed")


async def test_mock_cli_scripts():
    """Test that mock CLI scripts work correctly."""
    import subprocess
    
    mock_bin = Path(__file__).parent / "mocks" / "bin"
    
    # Test mock gemini
    result = subprocess.run(
        [str(mock_bin / "gemini"), "--version"],
        capture_output=True, text=True
    )
    assert "mock" in result.stdout.lower(), f"Gemini mock should identify itself, got: {result.stdout}"
    
    # Test mock claude
    result = subprocess.run(
        [str(mock_bin / "claude"), "--version"],
        capture_output=True, text=True
    )
    assert "mock" in result.stdout.lower(), f"Claude mock should identify itself, got: {result.stdout}"
    
    # Test mock codex
    result = subprocess.run(
        [str(mock_bin / "codex"), "--version"],
        capture_output=True, text=True
    )
    assert "mock" in result.stdout.lower(), f"Codex mock should identify itself, got: {result.stdout}"
    
    print("✓ Mock CLI scripts test passed")


async def test_mock_cli_response():
    """Test mock CLI produces expected responses."""
    import subprocess
    
    mock_bin = Path(__file__).parent / "mocks" / "bin"
    
    result = subprocess.run(
        [str(mock_bin / "gemini"), "-p", "hello there"],
        capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "mock" in result.stdout.lower() or "hello" in result.stdout.lower()
    
    print("✓ Mock CLI response test passed")


# === Run All Tests ===

async def run_all_tests():
    """Run all tests and report results."""
    tests = [
        ("Mock Backend Basic", test_mock_backend_basic),
        ("Scenario-Based Responses", test_mock_backend_scenarios),
        ("Error Handling", test_mock_backend_error),
        ("Call History Recording", test_call_history),
        ("Streaming Events", test_streaming_events),
        ("Session Reset", test_new_session),
        ("Mock CLI Scripts", test_mock_cli_scripts),
        ("Mock CLI Response", test_mock_cli_response),
    ]
    
    print("\n" + "=" * 60)
    print("  HASHI MOCK TEST SUITE")
    print("=" * 60 + "\n")
    
    harness = TestHarness()
    await harness.setup()
    
    passed = 0
    failed = 0
    
    for name, test_fn in tests:
        try:
            print(f"Running: {name}...")
            await test_fn()
            passed += 1
        except Exception as e:
            import traceback
            print(f"✗ {name} FAILED: {e}")
            traceback.print_exc()
            failed += 1
    
    await harness.teardown()
    
    print("\n" + "=" * 60)
    print(f"  Results: {passed} passed, {failed} failed")
    print("=" * 60 + "\n")
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
