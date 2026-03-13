#!/usr/bin/env python3
"""Display detailed test report."""

import json
import sys
from pathlib import Path

def main():
    # Use relative path from repository root
    repo_root = Path(__file__).parent.parent
    report_path = repo_root / "test_reports" / "latest" / "hashi_logged_test_suite_report.json"
    
    if not report_path.exists():
        print("No report found. Run ./run_tests.sh first.")
        sys.exit(1)
    
    with open(report_path) as f:
        r = json.load(f)
    
    print("=" * 70)
    print("  DETAILED TEST REPORT")
    print("=" * 70)
    print()
    
    # Summary
    s = r['summary']
    print("📋 SUMMARY")
    print(f"   Run ID:      {s['run_id']}")
    print(f"   Start:       {s['start_time']}")
    print(f"   End:         {s['end_time']}")
    print(f"   Duration:    {s['total_duration_ms']:.1f}ms")
    print(f"   Tests:       {s['passed']}/{s['total_tests']} passed, {s['failed']} failed")
    print(f"   API Calls:   {s['total_api_calls']}")
    print()
    
    # Performance
    p = r['performance']
    print("⚡ PERFORMANCE METRICS")
    print(f"   Total Requests:  {p['total_requests']}")
    print(f"   Avg Response:    {p['avg_duration_ms']:.2f}ms")
    print(f"   Min Response:    {p['min_duration_ms']:.2f}ms")
    print(f"   Max Response:    {p['max_duration_ms']:.2f}ms")
    print(f"   Total Time:      {p['total_duration_ms']:.2f}ms")
    print()
    
    # Test Results Table
    print("🧪 TEST RESULTS")
    print("   " + "-" * 65)
    print(f"   {'Test Name':<42} {'Status':<10} {'Duration':<12}")
    print("   " + "-" * 65)
    for t in s['tests']:
        status = '✓ PASS' if t['passed'] else '✗ FAIL'
        dur = f"{t.get('duration_ms', 0):.1f}ms" if t.get('duration_ms') else '-'
        print(f"   {t['name']:<42} {status:<10} {dur:<12}")
    print("   " + "-" * 65)
    print()
    
    # Request/Response Pairs
    print("📨 REQUEST/RESPONSE LOG (showing 10 of {})".format(len(r['request_response_pairs'])))
    print()
    for i, pair in enumerate(r['request_response_pairs'][:10], 1):
        req_id = pair.get('request_id', 'N/A')
        test = pair.get('test_name', 'N/A')
        dur = pair.get('duration_ms', 0)
        prompt = (pair.get('prompt') or '')[:50]
        response = (pair.get('response') or pair.get('error') or 'N/A')[:50]
        
        print(f"   [{i:2}] {req_id}")
        print(f"       Test:     {test}")
        print(f"       Duration: {dur:.1f}ms")
        print(f"       Prompt:   {prompt}{'...' if len(pair.get('prompt',''))>50 else ''}")
        print(f"       Response: {response}{'...' if len(pair.get('response','') or '')>50 else ''}")
        print()
    
    # Errors
    print("❌ ERRORS ({} total)".format(len(r['errors'])))
    if r['errors']:
        for e in r['errors'][:5]:
            ts = e.get('timestamp', '').split('T')[-1][:12]
            msg = e.get('message', '')[:60]
            print(f"   [{ts}] {msg}")
    else:
        print("   None - all requests successful!")
    print()
    
    # Final Status
    print("=" * 70)
    if s['failed'] == 0:
        print("  ✅ ALL TESTS PASSED!")
    else:
        print(f"  ❌ {s['failed']} TEST(S) FAILED")
    print("=" * 70)

if __name__ == "__main__":
    main()
