"""
Comprehensive test logging system for Hashi.

Produces structured, inspectable logs for debugging and analysis.
"""

from __future__ import annotations
import json
import time
import logging
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from enum import Enum


class LogLevel(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass
class TestLogEntry:
    """A single log entry with full context."""
    timestamp: str
    level: str
    category: str
    message: str
    test_name: Optional[str] = None
    request_id: Optional[str] = None
    duration_ms: Optional[float] = None
    prompt: Optional[str] = None
    response: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Remove None values for cleaner output
        return {k: v for k, v in d.items() if v is not None}
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass  
class TestRunSummary:
    """Summary of a test run."""
    run_id: str
    start_time: str
    end_time: Optional[str] = None
    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total_duration_ms: float = 0
    total_api_calls: int = 0
    tests: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TestLogger:
    """
    Comprehensive logger for test inspection.
    
    Features:
    - Structured JSON logs
    - Console + file output
    - Request/response pairs
    - Performance metrics
    - Test run summaries
    
    Usage:
        logger = TestLogger(log_dir="/tmp/hashi_test_logs")
        logger.start_run("my_test_suite")
        
        logger.log_request("req-001", "Hello world", test_name="test_basic")
        logger.log_response("req-001", "Hi there!", duration_ms=150.5)
        
        logger.end_run()
        logger.export_report()
    """
    
    def __init__(
        self,
        log_dir: str | Path = "/tmp/hashi_test_logs",
        console_output: bool = True,
        json_output: bool = True,
        log_level: LogLevel = LogLevel.DEBUG,
    ):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.console_output = console_output
        self.json_output = json_output
        self.log_level = log_level
        
        self.entries: List[TestLogEntry] = []
        self.current_run: Optional[TestRunSummary] = None
        self.current_test: Optional[str] = None
        self._request_start_times: Dict[str, float] = {}
        
        # Setup Python logging
        self._setup_logging()
        
    def _setup_logging(self):
        """Configure Python logging handlers."""
        self.logger = logging.getLogger("hashi.test")
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers.clear()
        
        # Console handler with colors
        if self.console_output:
            console = logging.StreamHandler(sys.stdout)
            console.setLevel(logging.DEBUG)
            console.setFormatter(ColoredFormatter())
            self.logger.addHandler(console)
    
    def _timestamp(self) -> str:
        return datetime.now().isoformat(timespec='milliseconds')
    
    def _add_entry(self, entry: TestLogEntry):
        """Add entry to log and optionally write to file."""
        self.entries.append(entry)
        
        if self.json_output and self.current_run:
            log_file = self.log_dir / f"{self.current_run.run_id}.jsonl"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(entry.to_json() + "\n")
    
    # === Run Lifecycle ===
    
    def start_run(self, run_name: str = None):
        """Start a new test run."""
        run_id = run_name or f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.current_run = TestRunSummary(
            run_id=run_id,
            start_time=self._timestamp()
        )
        self.entries.clear()
        
        self._log(LogLevel.INFO, "run", f"Test run started: {run_id}")
        return run_id
    
    def end_run(self):
        """End the current test run."""
        if self.current_run:
            self.current_run.end_time = self._timestamp()
            
            # Calculate duration
            start = datetime.fromisoformat(self.current_run.start_time)
            end = datetime.fromisoformat(self.current_run.end_time)
            self.current_run.total_duration_ms = (end - start).total_seconds() * 1000
            
            self._log(
                LogLevel.INFO, "run",
                f"Test run ended: {self.current_run.passed}/{self.current_run.total_tests} passed "
                f"({self.current_run.total_duration_ms:.1f}ms)"
            )
    
    # === Test Lifecycle ===
    
    def start_test(self, test_name: str):
        """Mark the start of a test."""
        self.current_test = test_name
        self._log(LogLevel.INFO, "test", f"Starting: {test_name}")
        
        if self.current_run:
            self.current_run.total_tests += 1
    
    def end_test(self, test_name: str, passed: bool, error: str = None, duration_ms: float = None):
        """Mark the end of a test."""
        status = "PASSED" if passed else "FAILED"
        
        if self.current_run:
            if passed:
                self.current_run.passed += 1
            else:
                self.current_run.failed += 1
            
            self.current_run.tests.append({
                "name": test_name,
                "passed": passed,
                "error": error,
                "duration_ms": duration_ms,
            })
        
        level = LogLevel.INFO if passed else LogLevel.ERROR
        msg = f"{status}: {test_name}"
        if duration_ms:
            msg += f" ({duration_ms:.1f}ms)"
        if error:
            msg += f" - {error}"
            
        self._log(level, "test", msg, test_name=test_name, error=error)
        self.current_test = None
    
    # === Request/Response Logging ===
    
    def log_request(
        self,
        request_id: str,
        prompt: str,
        test_name: str = None,
        metadata: Dict[str, Any] = None,
    ):
        """Log an outgoing request."""
        self._request_start_times[request_id] = time.perf_counter()
        
        entry = TestLogEntry(
            timestamp=self._timestamp(),
            level=LogLevel.DEBUG.value,
            category="request",
            message=f"Request {request_id}: {prompt[:100]}{'...' if len(prompt) > 100 else ''}",
            test_name=test_name or self.current_test,
            request_id=request_id,
            prompt=prompt,
            metadata=metadata or {},
        )
        self._add_entry(entry)
        
        if self.current_run:
            self.current_run.total_api_calls += 1
    
    def log_response(
        self,
        request_id: str,
        response: str,
        duration_ms: float = None,
        error: str = None,
        metadata: Dict[str, Any] = None,
    ):
        """Log an incoming response."""
        # Calculate duration if not provided
        if duration_ms is None and request_id in self._request_start_times:
            start = self._request_start_times.pop(request_id)
            duration_ms = (time.perf_counter() - start) * 1000
        
        is_error = error is not None
        level = LogLevel.ERROR if is_error else LogLevel.DEBUG
        
        entry = TestLogEntry(
            timestamp=self._timestamp(),
            level=level.value,
            category="response",
            message=f"Response {request_id}: {'ERROR: ' + error if is_error else response[:100]}",
            test_name=self.current_test,
            request_id=request_id,
            response=response if not is_error else None,
            error=error,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )
        self._add_entry(entry)
    
    # === General Logging ===
    
    def _log(
        self,
        level: LogLevel,
        category: str,
        message: str,
        test_name: str = None,
        error: str = None,
        metadata: Dict[str, Any] = None,
    ):
        """Internal logging method."""
        entry = TestLogEntry(
            timestamp=self._timestamp(),
            level=level.value,
            category=category,
            message=message,
            test_name=test_name or self.current_test,
            error=error,
            metadata=metadata or {},
        )
        self._add_entry(entry)
        
        # Also log to Python logger
        log_fn = getattr(self.logger, level.value.lower())
        log_fn(f"[{category}] {message}")
    
    def debug(self, message: str, **kwargs):
        self._log(LogLevel.DEBUG, "debug", message, **kwargs)
        
    def info(self, message: str, **kwargs):
        self._log(LogLevel.INFO, "info", message, **kwargs)
        
    def warning(self, message: str, **kwargs):
        self._log(LogLevel.WARNING, "warning", message, **kwargs)
        
    def error(self, message: str, **kwargs):
        self._log(LogLevel.ERROR, "error", message, **kwargs)
    
    # === Analysis & Export ===
    
    def get_entries(
        self,
        category: str = None,
        level: LogLevel = None,
        test_name: str = None,
        request_id: str = None,
    ) -> List[TestLogEntry]:
        """Filter and retrieve log entries."""
        results = self.entries
        
        if category:
            results = [e for e in results if e.category == category]
        if level:
            results = [e for e in results if e.level == level.value]
        if test_name:
            results = [e for e in results if e.test_name == test_name]
        if request_id:
            results = [e for e in results if e.request_id == request_id]
            
        return results
    
    def get_request_response_pairs(self) -> List[Dict[str, Any]]:
        """Get all request/response pairs for inspection."""
        requests = {e.request_id: e for e in self.entries if e.category == "request" and e.request_id}
        responses = {e.request_id: e for e in self.entries if e.category == "response" and e.request_id}
        
        pairs = []
        for req_id, req in requests.items():
            resp = responses.get(req_id)
            pairs.append({
                "request_id": req_id,
                "test_name": req.test_name,
                "prompt": req.prompt,
                "response": resp.response if resp else None,
                "error": resp.error if resp else None,
                "duration_ms": resp.duration_ms if resp else None,
                "request_time": req.timestamp,
                "response_time": resp.timestamp if resp else None,
            })
        
        return pairs
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """Get performance statistics."""
        response_times = [
            e.duration_ms for e in self.entries 
            if e.category == "response" and e.duration_ms
        ]
        
        if not response_times:
            return {"total_requests": 0}
        
        return {
            "total_requests": len(response_times),
            "avg_duration_ms": sum(response_times) / len(response_times),
            "min_duration_ms": min(response_times),
            "max_duration_ms": max(response_times),
            "total_duration_ms": sum(response_times),
        }
    
    def get_errors(self) -> List[TestLogEntry]:
        """Get all error entries."""
        return [e for e in self.entries if e.error or e.level == LogLevel.ERROR.value]
    
    def export_report(self, format: str = "json") -> Path:
        """Export a full test report."""
        if not self.current_run:
            raise ValueError("No test run to export")
        
        report = {
            "summary": self.current_run.to_dict(),
            "performance": self.get_performance_stats(),
            "request_response_pairs": self.get_request_response_pairs(),
            "errors": [e.to_dict() for e in self.get_errors()],
            "all_entries": [e.to_dict() for e in self.entries],
        }
        
        if format == "json":
            report_path = self.log_dir / f"{self.current_run.run_id}_report.json"
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
        elif format == "markdown":
            report_path = self.log_dir / f"{self.current_run.run_id}_report.md"
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(self._generate_markdown_report(report))
        else:
            raise ValueError(f"Unknown format: {format}")
        
        self.info(f"Report exported to: {report_path}")
        return report_path
    
    def _generate_markdown_report(self, report: Dict[str, Any]) -> str:
        """Generate a markdown report."""
        summary = report["summary"]
        perf = report["performance"]
        
        lines = [
            f"# Test Report: {summary['run_id']}",
            "",
            "## Summary",
            "",
            f"- **Start Time:** {summary['start_time']}",
            f"- **End Time:** {summary['end_time']}",
            f"- **Duration:** {summary['total_duration_ms']:.1f}ms",
            f"- **Tests:** {summary['passed']}/{summary['total_tests']} passed",
            f"- **API Calls:** {summary['total_api_calls']}",
            "",
            "## Performance",
            "",
        ]
        
        if perf.get("total_requests"):
            lines.extend([
                f"- **Average Response Time:** {perf['avg_duration_ms']:.1f}ms",
                f"- **Min Response Time:** {perf['min_duration_ms']:.1f}ms",
                f"- **Max Response Time:** {perf['max_duration_ms']:.1f}ms",
                "",
            ])
        
        lines.extend([
            "## Test Results",
            "",
            "| Test | Status | Duration | Error |",
            "|------|--------|----------|-------|",
        ])
        
        for test in summary.get("tests", []):
            status = "✓ PASS" if test["passed"] else "✗ FAIL"
            duration = f"{test.get('duration_ms', 0):.1f}ms" if test.get("duration_ms") else "-"
            error = test.get("error", "-") or "-"
            lines.append(f"| {test['name']} | {status} | {duration} | {error[:50]} |")
        
        lines.extend([
            "",
            "## Request/Response Log",
            "",
        ])
        
        for pair in report.get("request_response_pairs", [])[:20]:  # Limit to 20
            lines.extend([
                f"### {pair['request_id']}",
                f"- **Test:** {pair.get('test_name', 'N/A')}",
                f"- **Duration:** {pair.get('duration_ms', 'N/A')}ms",
                "",
                "**Prompt:**",
                "```",
                pair.get("prompt", "")[:500],
                "```",
                "",
                "**Response:**",
                "```",
                (pair.get("response") or pair.get("error") or "N/A")[:500],
                "```",
                "",
            ])
        
        if report.get("errors"):
            lines.extend([
                "## Errors",
                "",
            ])
            for err in report["errors"]:
                lines.append(f"- [{err['timestamp']}] {err['message']}")
        
        return "\n".join(lines)
    
    def print_summary(self):
        """Print a summary to console."""
        if not self.current_run:
            print("No test run data.")
            return
        
        s = self.current_run
        perf = self.get_performance_stats()
        errors = self.get_errors()
        
        print("\n" + "=" * 60)
        print(f"  TEST RUN SUMMARY: {s.run_id}")
        print("=" * 60)
        print(f"  Tests:     {s.passed}/{s.total_tests} passed, {s.failed} failed")
        print(f"  Duration:  {s.total_duration_ms:.1f}ms")
        print(f"  API Calls: {s.total_api_calls}")
        
        if perf.get("total_requests"):
            print(f"  Avg Time:  {perf['avg_duration_ms']:.1f}ms")
        
        if errors:
            print(f"  Errors:    {len(errors)}")
            for err in errors[:3]:
                print(f"    - {err.message[:60]}")
        
        print("=" * 60 + "\n")


class ColoredFormatter(logging.Formatter):
    """Colored console output formatter."""
    
    COLORS = {
        'DEBUG': '\033[36m',     # Cyan
        'INFO': '\033[32m',      # Green
        'WARNING': '\033[33m',   # Yellow
        'ERROR': '\033[31m',     # Red
        'CRITICAL': '\033[35m',  # Magenta
    }
    RESET = '\033[0m'
    
    def format(self, record):
        color = self.COLORS.get(record.levelname, self.RESET)
        timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        return f"{color}{timestamp} [{record.levelname:5}]{self.RESET} {record.getMessage()}"


# === Global Logger Instance ===

_default_logger: Optional[TestLogger] = None


def get_logger() -> TestLogger:
    """Get the default test logger instance."""
    global _default_logger
    if _default_logger is None:
        _default_logger = TestLogger()
    return _default_logger


def set_logger(logger: TestLogger):
    """Set the default test logger instance."""
    global _default_logger
    _default_logger = logger
