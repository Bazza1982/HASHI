from __future__ import annotations

import argparse
import json
from pathlib import Path

from orchestrator.agent_move.package import create_agent_move_package, package_sha256
from orchestrator.agent_move.service import commit_agent_move, stage_agent_move
from orchestrator.pcm import render_pcm_document
from orchestrator.session_store import SessionStore


OWNER_ID = "user:7"
AGENT_ID = "traveler"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _prepare_root(root: Path, *, instance_id: str, include_agent: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    if include_agent:
        workspace = root / "workspaces" / AGENT_ID
        (workspace / "memory").mkdir(parents=True, exist_ok=True)
        (workspace / "agent.md").write_text(
            render_pcm_document(
                persona="Cross-platform traveler",
                system="Preserve tested conversation continuity.",
                memory="The source and target remain independently owned.",
            ),
            encoding="utf-8",
        )
        rows.append(
            {
                "name": AGENT_ID,
                "display_name": "Cross-platform traveler",
                "type": "flex",
                "workspace_dir": f"workspaces/{AGENT_ID}",
                "active_backend": "codex-cli",
                "allowed_backends": [{"engine": "codex-cli", "model": "gpt-5.5"}],
                "access_scope": "project",
                "is_active": True,
            }
        )
    _write_json(
        root / "agents.json",
        {
            "global": {
                "instance_id": instance_id,
                "authorized_id": 7,
                "agent_move": {"max_access_scope": "project"},
            },
            "agents": rows,
        },
    )
    _write_json(root / "secrets.json", {})
    _write_json(
        root / "tasks.json",
        {"version": 1, "heartbeats": [], "crons": [], "nudges": []},
    )


def _complete_history(
    store: SessionStore,
    *,
    session_id: str,
    request_id: str,
    question: str,
    answer: str,
) -> None:
    accepted = store.accept_run(
        session_id=session_id,
        owner_id=OWNER_ID,
        agent_id=AGENT_ID,
        request_id=request_id,
        text=question,
        source="workbench",
        idempotency_key=request_id,
    )
    store.mark_request_running(accepted.request_id, worker_id="live-canary")
    store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text=answer,
        assistant_source="live-canary",
    )


def _source(args: argparse.Namespace) -> dict:
    root = Path(args.root).expanduser().resolve()
    package_path = Path(args.package).expanduser().resolve()
    _prepare_root(root, instance_id=args.instance, include_agent=True)
    store = SessionStore(root / "state" / "sessions.sqlite3", instance_id=args.instance)
    session = store.resolve_session(
        owner_id=OWNER_ID,
        agent_id=AGENT_ID,
        surface="workbench",
        channel_key="default",
    )
    store.bind_channel(
        owner_id=OWNER_ID,
        agent_id=AGENT_ID,
        surface="telegram",
        channel_key="7",
        session_id=session["session_id"],
    )
    _complete_history(
        store,
        session_id=session["session_id"],
        request_id="source-complete",
        question=args.question,
        answer=args.answer,
    )
    store.accept_run(
        session_id=session["session_id"],
        owner_id=OWNER_ID,
        agent_id=AGENT_ID,
        request_id="source-active",
        text="active work must not cross the Move boundary",
        source="workbench",
        idempotency_key="source-active",
    )
    package = create_agent_move_package(
        root,
        AGENT_ID,
        package_path,
        source_instance=args.instance,
        include_agent_secrets=False,
        transfer_mode="identity_memory",
    )
    return {
        "package_id": package.package_id,
        "sha256": package_sha256(package_path),
        "history_mode": package.manifest.get("history_mode"),
        "eligible_messages": package.conversation_continuity["summary"][
            "eligible_message_count"
        ],
        "source_messages": len(
            store.messages(session["session_id"], owner_id=OWNER_ID)
        ),
    }


def _target(args: argparse.Namespace) -> dict:
    root = Path(args.root).expanduser().resolve()
    package_path = Path(args.package).expanduser().resolve()
    _prepare_root(root, instance_id=args.instance, include_agent=False)
    store = SessionStore(root / "state" / "sessions.sqlite3", instance_id=args.instance)
    session = store.resolve_session(
        owner_id=OWNER_ID,
        agent_id=AGENT_ID,
        surface="workbench",
        channel_key="default",
    )
    store.bind_channel(
        owner_id=OWNER_ID,
        agent_id=AGENT_ID,
        surface="telegram",
        channel_key="7",
        session_id=session["session_id"],
    )
    _complete_history(
        store,
        session_id=session["session_id"],
        request_id="target-complete",
        question=args.question,
        answer=args.answer,
    )
    staged = stage_agent_move(
        root,
        package_path,
        expected_sha256=package_sha256(package_path),
        source_instance=args.source_instance,
        target_instance=args.instance,
        operation="move",
    )
    committed = commit_agent_move(root, staged["package_id"])
    replayed = commit_agent_move(root, staged["package_id"])
    workbench = store.resolve_session(
        owner_id=OWNER_ID,
        agent_id=AGENT_ID,
        surface="workbench",
        channel_key="default",
    )
    telegram = store.resolve_session(
        owner_id=OWNER_ID,
        agent_id=AGENT_ID,
        surface="telegram",
        channel_key="7",
    )
    return {
        "package_id": staged["package_id"],
        "status": committed["status"],
        "replay_status": replayed["status"],
        "imported_messages": committed["conversation_continuity"][
            "imported_messages"
        ],
        "replayed_messages": replayed["conversation_continuity"][
            "imported_messages"
        ],
        "workbench_session_id": workbench["session_id"],
        "telegram_session_id": telegram["session_id"],
        "history_generation": workbench["history_generation"],
        "messages": [
            item["text"]
            for item in store.messages(workbench["session_id"], owner_id=OWNER_ID)
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    source = subparsers.add_parser("source")
    source.add_argument("--root", required=True)
    source.add_argument("--package", required=True)
    source.add_argument("--instance", required=True)
    source.add_argument("--question", required=True)
    source.add_argument("--answer", required=True)
    target = subparsers.add_parser("target")
    target.add_argument("--root", required=True)
    target.add_argument("--package", required=True)
    target.add_argument("--instance", required=True)
    target.add_argument("--source-instance", required=True)
    target.add_argument("--question", required=True)
    target.add_argument("--answer", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = _source(args) if args.operation == "source" else _target(args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
