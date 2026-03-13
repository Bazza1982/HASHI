#!/usr/bin/env bash
#
# Hashi Full Test Suite Runner
# Runs all mock tests and generates comprehensive reports
#
# Usage:
#   ./run_tests.sh              # Run all tests
#   ./run_tests.sh --quick      # Run basic tests only
#   ./run_tests.sh --verbose    # Show detailed output
#   ./run_tests.sh --report     # Generate reports only (from last run)
#

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

# Directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_DIR="$SCRIPT_DIR/tests"
LOG_DIR="/tmp/hashi_test_logs"
REPORT_DIR="$SCRIPT_DIR/test_reports"

# Options
QUICK_MODE=false
VERBOSE=false
REPORT_ONLY=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --quick|-q) QUICK_MODE=true; shift ;;
        --verbose|-v) VERBOSE=true; shift ;;
        --report|-r) REPORT_ONLY=true; shift ;;
        --help|-h)
            echo "Usage: $0 [options]"
            echo "Options:"
            echo "  --quick, -q     Run basic tests only"
            echo "  --verbose, -v   Show detailed output"
            echo "  --report, -r    Generate reports only"
            echo "  --help, -h      Show this help"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Banner
print_banner() {
    echo -e "${CYAN}"
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║                                                               ║"
    echo "║   ${BOLD}HASHI MOCK TEST SUITE${RESET}${CYAN}                                      ║"
    echo "║   Full System Testing without API Keys                        ║"
    echo "║                                                               ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"
    echo -e "${RESET}"
}

# Print section header
section() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${BOLD}  $1${RESET}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
}

# Print success/fail
ok() { echo -e "  ${GREEN}✓${RESET} $1"; }
fail() { echo -e "  ${RED}✗${RESET} $1"; }
info() { echo -e "  ${CYAN}ℹ${RESET} $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET} $1"; }

# Setup environment
setup_environment() {
    section "Environment Setup"
    
    # Create directories
    mkdir -p "$LOG_DIR" "$REPORT_DIR"
    ok "Log directory: $LOG_DIR"
    ok "Report directory: $REPORT_DIR"
    
    # Check Python
    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 --version)
        ok "Python: $PYTHON_VERSION"
    else
        fail "Python3 not found!"
        exit 1
    fi
    
    # Check mock CLI scripts
    MOCK_BIN="$TEST_DIR/mocks/bin"
    if [[ -x "$MOCK_BIN/gemini" ]] && [[ -x "$MOCK_BIN/claude" ]] && [[ -x "$MOCK_BIN/codex" ]]; then
        ok "Mock CLI scripts: Ready"
    else
        warn "Making mock CLI scripts executable..."
        chmod +x "$MOCK_BIN"/* 2>/dev/null || true
    fi
    
    # Verify mock CLIs work
    if "$MOCK_BIN/gemini" --version 2>/dev/null | grep -q "mock"; then
        ok "Mock Gemini CLI: Working"
    else
        fail "Mock Gemini CLI: Not working"
    fi
    
    if "$MOCK_BIN/claude" --version 2>/dev/null | grep -q "mock"; then
        ok "Mock Claude CLI: Working"
    else
        fail "Mock Claude CLI: Not working"
    fi
    
    if "$MOCK_BIN/codex" --version 2>/dev/null | grep -q "mock"; then
        ok "Mock Codex CLI: Working"
    else
        fail "Mock Codex CLI: Not working"
    fi
}

# Run basic tests
run_basic_tests() {
    section "Running Basic Tests"
    
    cd "$SCRIPT_DIR"
    
    if $VERBOSE; then
        python3 tests/test_harness.py
    else
        python3 tests/test_harness.py 2>&1 | tail -20
    fi
    
    BASIC_EXIT_CODE=${PIPESTATUS[0]}
    
    if [[ $BASIC_EXIT_CODE -eq 0 ]]; then
        ok "Basic tests: All passed"
    else
        fail "Basic tests: Some failed"
    fi
    
    return $BASIC_EXIT_CODE
}

# Run logged tests
run_logged_tests() {
    section "Running Logged Tests"
    
    cd "$SCRIPT_DIR"
    
    if $VERBOSE; then
        python3 tests/test_with_logging.py
    else
        python3 tests/test_with_logging.py 2>&1
    fi
    
    LOGGED_EXIT_CODE=${PIPESTATUS[0]}
    
    if [[ $LOGGED_EXIT_CODE -eq 0 ]]; then
        ok "Logged tests: All passed"
    else
        fail "Logged tests: Some failed"
    fi
    
    return $LOGGED_EXIT_CODE
}

# Run adapter unit tests
run_adapter_tests() {
    section "Running Adapter Tests"
    
    cd "$SCRIPT_DIR"
    
    python3 -c "
import asyncio
import sys
sys.path.insert(0, '.')

from tests.mocks.mock_adapters import MockBackend, MockScenario, SimpleTestConfig, SimpleGlobalConfig

async def test_adapters():
    passed = 0
    failed = 0
    
    # Test 1: Initialize
    print('  Testing adapter initialization...')
    config = SimpleTestConfig('adapter-test')
    backend = MockBackend(config, SimpleGlobalConfig())
    if await backend.initialize():
        print('    ✓ Initialization')
        passed += 1
    else:
        print('    ✗ Initialization')
        failed += 1
    
    # Test 2: Generate response
    print('  Testing response generation...')
    response = await backend.generate_response('Hello', 'test-001')
    if response.is_success and response.text:
        print('    ✓ Response generation')
        passed += 1
    else:
        print('    ✗ Response generation')
        failed += 1
    
    # Test 3: Custom scenario
    print('  Testing custom scenarios...')
    backend.add_scenario(MockScenario(pattern='custom', response='Custom reply!'))
    response = await backend.generate_response('Test custom scenario', 'test-002')
    if 'Custom' in response.text:
        print('    ✓ Custom scenarios')
        passed += 1
    else:
        print('    ✗ Custom scenarios')
        failed += 1
    
    # Test 4: Error scenario
    print('  Testing error handling...')
    backend.add_scenario(MockScenario(pattern='error', response='', error='Test error'))
    response = await backend.generate_response('Trigger error', 'test-003')
    if not response.is_success and response.error:
        print('    ✓ Error handling')
        passed += 1
    else:
        print('    ✗ Error handling')
        failed += 1
    
    # Test 5: Shutdown
    print('  Testing shutdown...')
    await backend.shutdown()
    if not backend.initialized:
        print('    ✓ Shutdown')
        passed += 1
    else:
        print('    ✗ Shutdown')
        failed += 1
    
    print(f'  Results: {passed}/{passed+failed} passed')
    return failed == 0

success = asyncio.run(test_adapters())
sys.exit(0 if success else 1)
"
    ADAPTER_EXIT_CODE=$?
    
    if [[ $ADAPTER_EXIT_CODE -eq 0 ]]; then
        ok "Adapter tests: All passed"
    else
        fail "Adapter tests: Some failed"
    fi
    
    return $ADAPTER_EXIT_CODE
}

# Run CLI mock tests
run_cli_tests() {
    section "Running CLI Mock Tests"
    
    MOCK_BIN="$TEST_DIR/mocks/bin"
    CLI_PASSED=0
    CLI_FAILED=0
    
    # Test Gemini CLI
    echo "  Testing Mock Gemini CLI..."
    
    if "$MOCK_BIN/gemini" --version | grep -q "mock"; then
        ok "gemini --version"
        ((CLI_PASSED++))
    else
        fail "gemini --version"
        ((CLI_FAILED++))
    fi
    
    GEMINI_RESP=$("$MOCK_BIN/gemini" -p "hello world" 2>&1)
    if [[ -n "$GEMINI_RESP" ]]; then
        ok "gemini -p 'hello world'"
        ((CLI_PASSED++))
    else
        fail "gemini -p 'hello world'"
        ((CLI_FAILED++))
    fi
    
    # Test Claude CLI
    echo "  Testing Mock Claude CLI..."
    
    if "$MOCK_BIN/claude" --version | grep -q "mock"; then
        ok "claude --version"
        ((CLI_PASSED++))
    else
        fail "claude --version"
        ((CLI_FAILED++))
    fi
    
    CLAUDE_RESP=$("$MOCK_BIN/claude" -p "test prompt" "test" 2>&1)
    if [[ -n "$CLAUDE_RESP" ]]; then
        ok "claude -p 'test'"
        ((CLI_PASSED++))
    else
        fail "claude -p 'test'"
        ((CLI_FAILED++))
    fi
    
    # Test Codex CLI
    echo "  Testing Mock Codex CLI..."
    
    if "$MOCK_BIN/codex" --version | grep -q "mock"; then
        ok "codex --version"
        ((CLI_PASSED++))
    else
        fail "codex --version"
        ((CLI_FAILED++))
    fi
    
    CODEX_RESP=$("$MOCK_BIN/codex" exec -- "test task" 2>&1)
    if echo "$CODEX_RESP" | grep -q "item.completed"; then
        ok "codex exec -- 'test'"
        ((CLI_PASSED++))
    else
        fail "codex exec -- 'test'"
        ((CLI_FAILED++))
    fi
    
    echo ""
    echo "  Results: $CLI_PASSED/$((CLI_PASSED + CLI_FAILED)) passed"
    
    [[ $CLI_FAILED -eq 0 ]]
}

# Run command tests
run_command_tests() {
    section "Running Command Tests"
    
    cd "$SCRIPT_DIR"
    
    python3 tests/test_commands.py
    CMD_EXIT_CODE=$?
    
    if [[ $CMD_EXIT_CODE -eq 0 ]]; then
        ok "Command tests: All passed"
    else
        fail "Command tests: Some failed"
    fi
    
    return $CMD_EXIT_CODE
}

# Collect and copy reports
collect_reports() {
    section "Collecting Reports"
    
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    FINAL_REPORT_DIR="$REPORT_DIR/$TIMESTAMP"
    mkdir -p "$FINAL_REPORT_DIR"
    
    # Copy log files
    if [[ -d "$LOG_DIR" ]] && [[ -n "$(ls -A "$LOG_DIR" 2>/dev/null)" ]]; then
        cp -r "$LOG_DIR"/* "$FINAL_REPORT_DIR/" 2>/dev/null || true
        ok "Copied logs to: $FINAL_REPORT_DIR"
    fi
    
    # Generate summary
    SUMMARY_FILE="$FINAL_REPORT_DIR/SUMMARY.md"
    cat > "$SUMMARY_FILE" << EOF
# Hashi Test Run Summary

**Date:** $(date '+%Y-%m-%d %H:%M:%S')
**Host:** $(hostname)
**Python:** $(python3 --version)

## Test Results

| Suite | Status |
|-------|--------|
| Basic Tests | ${BASIC_RESULT:-Unknown} |
| Logged Tests | ${LOGGED_RESULT:-Unknown} |
| Adapter Tests | ${ADAPTER_RESULT:-Unknown} |
| CLI Mock Tests | ${CLI_RESULT:-Unknown} |
| Command Tests | ${CMD_RESULT:-Unknown} |

## Files Generated

$(ls -la "$FINAL_REPORT_DIR" 2>/dev/null | tail -n +4 | awk '{print "- " $NF " (" $5 " bytes)"}')

## How to Inspect

### View JSONL Logs
\`\`\`bash
# Pretty print
cat $FINAL_REPORT_DIR/*.jsonl | python3 -m json.tool

# Filter by category
grep '"category": "request"' $FINAL_REPORT_DIR/*.jsonl | head
\`\`\`

### View Markdown Report
\`\`\`bash
cat $FINAL_REPORT_DIR/*_report.md
\`\`\`

### Analyze in Python
\`\`\`python
import json
with open('$FINAL_REPORT_DIR/hashi_logged_test_suite_report.json') as f:
    report = json.load(f)
    print(f"Tests: {report['summary']['passed']}/{report['summary']['total_tests']}")
    print(f"Avg Response: {report['performance']['avg_duration_ms']:.1f}ms")
\`\`\`
EOF
    
    ok "Generated summary: $SUMMARY_FILE"
    
    # Create latest symlink
    ln -sfn "$FINAL_REPORT_DIR" "$REPORT_DIR/latest"
    ok "Latest reports: $REPORT_DIR/latest"
}

# Print final summary
print_summary() {
    section "Test Summary"
    
    TOTAL_PASSED=0
    TOTAL_FAILED=0
    
    echo -e "  ${BOLD}Suite                  Status${RESET}"
    echo "  ─────────────────────────────────"
    
    if [[ "${BASIC_RESULT:-}" == "PASSED" ]]; then
        echo -e "  Basic Tests          ${GREEN}✓ PASSED${RESET}"
        ((TOTAL_PASSED++))
    else
        echo -e "  Basic Tests          ${RED}✗ FAILED${RESET}"
        ((TOTAL_FAILED++))
    fi
    
    if [[ "${LOGGED_RESULT:-}" == "PASSED" ]]; then
        echo -e "  Logged Tests         ${GREEN}✓ PASSED${RESET}"
        ((TOTAL_PASSED++))
    else
        echo -e "  Logged Tests         ${RED}✗ FAILED${RESET}"
        ((TOTAL_FAILED++))
    fi
    
    if [[ "${ADAPTER_RESULT:-}" == "PASSED" ]]; then
        echo -e "  Adapter Tests        ${GREEN}✓ PASSED${RESET}"
        ((TOTAL_PASSED++))
    else
        echo -e "  Adapter Tests        ${RED}✗ FAILED${RESET}"
        ((TOTAL_FAILED++))
    fi
    
    if [[ "${CLI_RESULT:-}" == "PASSED" ]]; then
        echo -e "  CLI Mock Tests       ${GREEN}✓ PASSED${RESET}"
        ((TOTAL_PASSED++))
    else
        echo -e "  CLI Mock Tests       ${RED}✗ FAILED${RESET}"
        ((TOTAL_FAILED++))
    fi
    
    if [[ "${CMD_RESULT:-}" == "PASSED" ]]; then
        echo -e "  Command Tests        ${GREEN}✓ PASSED${RESET}"
        ((TOTAL_PASSED++))
    else
        echo -e "  Command Tests        ${RED}✗ FAILED${RESET}"
        ((TOTAL_FAILED++))
    fi
    
    echo "  ─────────────────────────────────"
    echo -e "  ${BOLD}Total: $TOTAL_PASSED passed, $TOTAL_FAILED failed${RESET}"
    echo ""
    
    if [[ -d "$REPORT_DIR/latest" ]]; then
        info "Reports saved to: $REPORT_DIR/latest"
    fi
    
    echo ""
    
    if [[ $TOTAL_FAILED -eq 0 ]]; then
        echo -e "${GREEN}╔═══════════════════════════════════════╗${RESET}"
        echo -e "${GREEN}║  ${BOLD}ALL TESTS PASSED!${RESET}${GREEN}                    ║${RESET}"
        echo -e "${GREEN}╚═══════════════════════════════════════╝${RESET}"
        return 0
    else
        echo -e "${RED}╔═══════════════════════════════════════╗${RESET}"
        echo -e "${RED}║  ${BOLD}SOME TESTS FAILED${RESET}${RED}                     ║${RESET}"
        echo -e "${RED}╚═══════════════════════════════════════╝${RESET}"
        return 1
    fi
}

# Main execution
main() {
    print_banner
    
    if $REPORT_ONLY; then
        collect_reports
        exit 0
    fi
    
    setup_environment
    
    # Run test suites
    BASIC_RESULT="FAILED"
    LOGGED_RESULT="FAILED"
    ADAPTER_RESULT="FAILED"
    CLI_RESULT="FAILED"
    CMD_RESULT="FAILED"
    
    if run_basic_tests; then
        BASIC_RESULT="PASSED"
    fi
    
    if ! $QUICK_MODE; then
        if run_logged_tests; then
            LOGGED_RESULT="PASSED"
        fi
        
        if run_adapter_tests; then
            ADAPTER_RESULT="PASSED"
        fi
    else
        LOGGED_RESULT="SKIPPED"
        ADAPTER_RESULT="SKIPPED"
    fi
    
    if run_cli_tests; then
        CLI_RESULT="PASSED"
    fi
    
    if run_command_tests; then
        CMD_RESULT="PASSED"
    fi
    
    collect_reports
    print_summary
}

# Run main
main "$@"
