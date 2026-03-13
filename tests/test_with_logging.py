#!/usr/bin/env python3
"""
Test harness with comprehensive logging.

Demonstrates the full logging capabilities for inspection.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.mocks.mock_adapters import MockBackend, MockScenario, SimpleTestConfig, SimpleGlobalConfig
from tests.mocks.test_logger import TestLogger, get_logger, set_logger


class LoggingMockBackend(MockBackend):
    """MockBackend with integrated logging."""
    
    def __init__(self, agent_config, global_config, api_key: str = None, logger: TestLogger = None):
        super().__init__(agent_config, global_config, api_key)
        self.test_logger = logger or get_logger()
    
    async def generate_response(
        self,
        prompt: str,
        request_id: str,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event=None,
    ):
        # Log the request
        self.test_logger.log_request(
            request_id=request_id,
            prompt=prompt,
            metadata={"is_retry": is_retry, "backend": self.config.name}
        )
        
        # Call parent
        response = await super().generate_response(
            prompt, request_id, is_retry, silent, on_stream_event
        )
        
        # Log the response
        self.test_logger.log_response(
            request_id=request_id,
            response=response.text,
            duration_ms=response.duration_ms,
            error=response.error,
            metadata={"is_success": response.is_success}
        )
        
        return response


async def test_basic_with_logging(logger: TestLogger):
    """Basic test with full logging."""
    logger.start_test("test_basic_with_logging")
    start = time.perf_counter()
    
    try:
        config = SimpleTestConfig("logged-agent", "/tmp/hashi_test/logged")
        global_config = SimpleGlobalConfig()
        
        backend = LoggingMockBackend(config, global_config, logger=logger)
        await backend.initialize()
        
        # Make several requests
        logger.debug("Making first request...")
        r1 = await backend.generate_response("Hello, how are you?", "req-001")
        
        logger.debug("Making second request...")
        r2 = await backend.generate_response("Tell me about testing", "req-002")
        
        logger.debug("Making third request...")
        r3 = await backend.generate_response("Generate some code please", "req-003")
        
        assert r1.is_success and r2.is_success and r3.is_success
        
        duration = (time.perf_counter() - start) * 1000
        logger.end_test("test_basic_with_logging", passed=True, duration_ms=duration)
        
    except Exception as e:
        duration = (time.perf_counter() - start) * 1000
        logger.end_test("test_basic_with_logging", passed=False, error=str(e), duration_ms=duration)
        raise


async def test_scenarios_with_logging(logger: TestLogger):
    """Scenario test with logging."""
    logger.start_test("test_scenarios_with_logging")
    start = time.perf_counter()
    
    try:
        config = SimpleTestConfig("scenario-agent", "/tmp/hashi_test/scenario")
        global_config = SimpleGlobalConfig()
        
        backend = LoggingMockBackend(config, global_config, logger=logger)
        
        # Add custom scenarios
        backend.add_scenario(MockScenario(
            pattern="weather",
            response="The weather today is sunny with a high of 25°C.",
            delay_ms=80
        ))
        backend.add_scenario(MockScenario(
            pattern="joke",
            response="Why did the programmer quit? Because he didn't get arrays! 😄",
            delay_ms=50
        ))
        
        await backend.initialize()
        
        r1 = await backend.generate_response("What's the weather like?", "req-weather")
        r2 = await backend.generate_response("Tell me a joke", "req-joke")
        
        assert "sunny" in r1.text
        assert "programmer" in r2.text
        
        duration = (time.perf_counter() - start) * 1000
        logger.end_test("test_scenarios_with_logging", passed=True, duration_ms=duration)
        
    except Exception as e:
        duration = (time.perf_counter() - start) * 1000
        logger.end_test("test_scenarios_with_logging", passed=False, error=str(e), duration_ms=duration)
        raise


async def test_error_handling_with_logging(logger: TestLogger):
    """Error handling test with logging."""
    logger.start_test("test_error_handling_with_logging")
    start = time.perf_counter()
    
    try:
        config = SimpleTestConfig("error-agent", "/tmp/hashi_test/error")
        global_config = SimpleGlobalConfig()
        
        backend = LoggingMockBackend(config, global_config, logger=logger)
        backend.add_scenario(MockScenario(
            pattern="fail",
            response="",
            error="Intentional test failure"
        ))
        
        await backend.initialize()
        
        # This should fail
        r1 = await backend.generate_response("Please fail now", "req-fail")
        
        assert not r1.is_success
        assert r1.error == "Intentional test failure"
        
        logger.info("Error was correctly captured and handled")
        
        duration = (time.perf_counter() - start) * 1000
        logger.end_test("test_error_handling_with_logging", passed=True, duration_ms=duration)
        
    except Exception as e:
        duration = (time.perf_counter() - start) * 1000
        logger.end_test("test_error_handling_with_logging", passed=False, error=str(e), duration_ms=duration)
        raise


async def test_multiple_backends_with_logging(logger: TestLogger):
    """Test multiple backends with logging."""
    logger.start_test("test_multiple_backends_with_logging")
    start = time.perf_counter()
    
    try:
        # Create multiple backends
        backends = {}
        for name in ["gemini", "claude", "codex"]:
            config = SimpleTestConfig(f"{name}-agent", f"/tmp/hashi_test/{name}")
            backends[name] = LoggingMockBackend(config, SimpleGlobalConfig(), logger=logger)
            backends[name].add_scenario(MockScenario(
                pattern=".*",
                response=f"Response from {name} backend",
                delay_ms=50
            ))
            await backends[name].initialize()
        
        # Query each backend
        for name, backend in backends.items():
            logger.debug(f"Querying {name} backend...")
            response = await backend.generate_response(
                f"Hello {name}!",
                f"req-{name}"
            )
            assert name in response.text.lower()
        
        duration = (time.perf_counter() - start) * 1000
        logger.end_test("test_multiple_backends_with_logging", passed=True, duration_ms=duration)
        
    except Exception as e:
        duration = (time.perf_counter() - start) * 1000
        logger.end_test("test_multiple_backends_with_logging", passed=False, error=str(e), duration_ms=duration)
        raise


async def test_performance_with_logging(logger: TestLogger):
    """Performance test with detailed timing."""
    logger.start_test("test_performance_with_logging")
    start = time.perf_counter()
    
    try:
        config = SimpleTestConfig("perf-agent", "/tmp/hashi_test/perf")
        global_config = SimpleGlobalConfig()
        
        backend = LoggingMockBackend(config, global_config, logger=logger)
        backend.add_scenario(MockScenario(
            pattern=".*",
            response="Quick response",
            delay_ms=25
        ))
        await backend.initialize()
        
        # Make many requests
        num_requests = 20
        logger.info(f"Making {num_requests} requests for performance testing...")
        
        for i in range(num_requests):
            await backend.generate_response(f"Request {i+1}", f"perf-req-{i+1:03d}")
        
        duration = (time.perf_counter() - start) * 1000
        logger.info(f"Completed {num_requests} requests in {duration:.1f}ms")
        logger.end_test("test_performance_with_logging", passed=True, duration_ms=duration)
        
    except Exception as e:
        duration = (time.perf_counter() - start) * 1000
        logger.end_test("test_performance_with_logging", passed=False, error=str(e), duration_ms=duration)
        raise


async def run_all_logged_tests():
    """Run all tests with comprehensive logging."""
    
    # Create logger with output directory
    log_dir = Path("/tmp/hashi_test_logs")
    logger = TestLogger(log_dir=log_dir, console_output=True, json_output=True)
    set_logger(logger)
    
    # Start test run
    run_id = logger.start_run("hashi_logged_test_suite")
    
    tests = [
        ("Basic Logging", test_basic_with_logging),
        ("Scenario Logging", test_scenarios_with_logging),
        ("Error Handling", test_error_handling_with_logging),
        ("Multiple Backends", test_multiple_backends_with_logging),
        ("Performance", test_performance_with_logging),
    ]
    
    for name, test_fn in tests:
        try:
            print(f"\n▶ Running: {name}...")
            await test_fn(logger)
            print(f"  ✓ {name} passed")
        except Exception as e:
            print(f"  ✗ {name} failed: {e}")
    
    # End run and export reports
    logger.end_run()
    logger.print_summary()
    
    # Export reports
    json_report = logger.export_report(format="json")
    md_report = logger.export_report(format="markdown")
    
    print(f"\n📁 Log files saved to: {log_dir}")
    print(f"   - JSONL log: {log_dir}/{run_id}.jsonl")
    print(f"   - JSON report: {json_report}")
    print(f"   - Markdown report: {md_report}")
    
    # Show some inspection examples
    print("\n📊 Inspection Examples:")
    
    pairs = logger.get_request_response_pairs()
    print(f"   - Total request/response pairs: {len(pairs)}")
    
    perf = logger.get_performance_stats()
    if perf.get("total_requests"):
        print(f"   - Average response time: {perf['avg_duration_ms']:.1f}ms")
        print(f"   - Min/Max: {perf['min_duration_ms']:.1f}ms / {perf['max_duration_ms']:.1f}ms")
    
    errors = logger.get_errors()
    print(f"   - Logged errors: {len(errors)}")
    
    return logger.current_run.failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all_logged_tests())
    sys.exit(0 if success else 1)
