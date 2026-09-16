"""Cross-platform mock of the Antigravity CLI (``agy``) for HASHI tests.

Windows-native test runners cannot execute the POSIX shell mock at
``tests/mocks/bin/agy`` directly (CreateProcess raises WinError 193 for
extensionless shell scripts).  This Python implementation mirrors that mock
for the verified agy 1.2.3 headless contract (2026-09-16):

* ``-p/--print <prompt>``, ``--model``, ``--output-format json|stream-json``,
  ``--conversation <id>``, ``--print-timeout``, ``--add-dir``,
  ``--dangerously-skip-permissions``, ``--version``
* Environment test hooks:
  ``AGY_MOCK_LOG <file>``      append the joined argv line per invocation
  ``AGY_MOCK_NOISE_STDOUT``    emit a non-JSON banner line on stdout first

On Windows the test module wraps this file in a generated ``agy.cmd`` so the
adapter's own ``resolve_argv_invocation`` COMSPEC path executes it.
"""

from __future__ import annotations

import os
import sys
import time

MOCK_CID = "11111111-2222-3333-4444-555555555555"
USAGE_JSON = (
    '"input_tokens":10,"output_tokens":3,"thinking_tokens":0,'
    '"cache_read_tokens":0,"total_tokens":13'
)


def main() -> int:
    output_format = "stream-json"
    prompt = ""
    conversation_id = MOCK_CID
    args = sys.argv[1:]
    orig_args = " ".join(args)

    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-p", "--print", "--prompt"):
            prompt = args[index + 1] if index + 1 < len(args) else ""
            index += 2
        elif arg == "--model":
            index += 2
        elif arg == "--conversation":
            if index + 1 < len(args):
                conversation_id = args[index + 1]
            index += 2
        elif arg == "--output-format":
            if index + 1 < len(args):
                output_format = args[index + 1]
            index += 2
        elif arg in ("--print-timeout", "--add-dir"):
            index += 2
        elif arg == "--dangerously-skip-permissions":
            index += 1
        elif arg == "--version":
            print("1.2.3 (mock)")
            return 0
        else:
            prompt = arg
            index += 1

    log_path = os.environ.get("AGY_MOCK_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(orig_args + "\n")

    if os.environ.get("AGY_MOCK_NOISE_STDOUT"):
        print("warning: mock noise line (not JSON)")

    if "error" in prompt:
        # agy 1.2.3 exits 0 while reporting status=ERROR in the payload.
        if output_format == "json":
            print(
                '{"conversation_id":"%s","status":"ERROR","response":"",'
                '"error":"Simulated agy error for testing",'
                '"duration_seconds":0,"num_turns":0,"usage":{%s}}'
                % (conversation_id, USAGE_JSON)
            )
        else:
            print(
                '{"event":"init","conversation_id":"%s",'
                '"init":{"permission_mode":"always-proceed"}}' % conversation_id
            )
            print(
                '{"event":"result","result":{"conversation_id":"%s",'
                '"status":"ERROR","response":"",'
                '"error":"Simulated agy error for testing",'
                '"duration_seconds":0,"num_turns":0,"usage":{%s}}}'
                % (conversation_id, USAGE_JSON)
            )
        return 0

    if "crash" in prompt:
        sys.stderr.write("mock agy crashed unexpectedly\n")
        return 1

    if "slow" in prompt:
        time.sleep(5)

    if output_format == "json":
        print(
            '{"conversation_id":"%s","status":"SUCCESS","response":"PONG",'
            '"duration_seconds":0.5,"num_turns":1,"usage":{%s}}'
            % (conversation_id, USAGE_JSON)
        )
        return 0

    print(
        '{"event":"init","conversation_id":"%s",'
        '"init":{"permission_mode":"always-proceed"}}' % conversation_id
    )
    print(
        '{"event":"step_update","step_update":{"conversation_id":"%s",'
        '"step_index":0,"state":"DONE","step_type":"user_input"}}'
        % conversation_id
    )
    print(
        '{"event":"step_update","step_update":{"conversation_id":"%s",'
        '"step_index":1,"state":"ACTIVE","step_type":"agent_response",'
        '"text_delta":"PONG"}}' % conversation_id
    )
    print(
        '{"event":"step_update","step_update":{"conversation_id":"%s",'
        '"step_index":1,"state":"DONE","step_type":"agent_response",'
        '"text_delta":"\\n","duration_seconds":0.5,"usage":{%s}}}'
        % (conversation_id, USAGE_JSON)
    )
    print(
        '{"event":"result","result":{"conversation_id":"%s",'
        '"status":"SUCCESS","response":"PONG\\n","duration_seconds":0.5,'
        '"num_turns":1,"usage":{%s}}}' % (conversation_id, USAGE_JSON)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())