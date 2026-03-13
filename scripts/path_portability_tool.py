import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from orchestrator.pathing import build_bridge_paths, resolve_path_value, to_home_relative


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _print_effective(paths):
    print(f"code_root    : {paths.code_root}")
    print(f"bridge_home  : {paths.bridge_home}")
    print(f"config_path  : {paths.config_path}")
    print(f"secrets_path : {paths.secrets_path}")
    print(f"tasks_path   : {paths.tasks_path}")
    print(f"state_path   : {paths.state_path}")
    print(f"lock_path    : {paths.lock_path}")
    print(f"pid_path     : {paths.pid_path}")
    print(f"workspaces   : {paths.workspaces_root}")


def cmd_show(args):
    paths = build_bridge_paths(ROOT_DIR, bridge_home=args.bridge_home)
    _print_effective(paths)
    raw = _load_json(paths.config_path)
    print("")
    print("Agent paths:")
    for agent in raw.get("agents", []):
        name = agent.get("name", "<unknown>")
        workspace_dir = resolve_path_value(
            agent.get("workspace_dir"),
            config_dir=paths.config_path.parent,
            bridge_home=paths.bridge_home,
        )
        system_md = resolve_path_value(
            agent.get("system_md"),
            config_dir=paths.config_path.parent,
            bridge_home=paths.bridge_home,
        )
        print(f"- {name}")
        print(f"  workspace_dir raw      : {agent.get('workspace_dir')}")
        print(f"  workspace_dir resolved : {workspace_dir}")
        print(f"  system_md raw          : {agent.get('system_md')}")
        print(f"  system_md resolved     : {system_md}")


def cmd_migrate_copy(args):
    paths = build_bridge_paths(ROOT_DIR, bridge_home=args.bridge_home)
    raw = _load_json(paths.config_path)
    migrated = deepcopy(raw)

    global_cfg = migrated.setdefault("global", {})
    for key in ("base_logs_dir", "base_media_dir"):
        value = raw.get("global", {}).get(key)
        if value is None:
            continue
        resolved = resolve_path_value(value, config_dir=paths.config_path.parent, bridge_home=paths.bridge_home)
        if resolved is not None:
            global_cfg[key] = to_home_relative(resolved, bridge_home=paths.bridge_home)

    for agent in migrated.get("agents", []):
        for key in ("workspace_dir", "system_md"):
            value = agent.get(key)
            if value is None:
                continue
            resolved = resolve_path_value(value, config_dir=paths.config_path.parent, bridge_home=paths.bridge_home)
            if resolved is not None:
                agent[key] = to_home_relative(resolved, bridge_home=paths.bridge_home)

    output_path = Path(args.output) if args.output else paths.config_path.with_name("agents.portable.json")
    output_path.write_text(
        json.dumps(migrated, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8-sig",
        newline="\r\n",
    )
    print(f"Wrote migrated copy to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Bridge-U-F path portability diagnostics and migration helper.")
    parser.add_argument("--bridge-home", help="Override the bridge home directory. Defaults to BRIDGE_HOME or the code root.")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show-effective", help="Show effective bridge-home and agent path resolution.")
    show.set_defaults(func=cmd_show)

    migrate = sub.add_parser("migrate-copy", help="Write a portable config copy using @home/... paths.")
    migrate.add_argument("--output", help="Output file path. Defaults to agents.portable.json next to the active config.")
    migrate.set_defaults(func=cmd_migrate_copy)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
