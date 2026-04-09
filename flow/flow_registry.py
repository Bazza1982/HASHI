#!/usr/bin/env python3
"""
HASHI Flow Registry
Reads the Minato/Shimanto/Nagare directory tree and answers hierarchy queries.

Usage:
  python flow/flow_registry.py minato
  python flow/flow_registry.py shimanto <minato_slug>
  python flow/flow_registry.py nagare <shimanto_slug> <minato_slug>
  python flow/flow_registry.py nagare --minato <minato_slug>
"""

import sys
import os
import yaml
from pathlib import Path

BASE = Path(__file__).parent / "minato"


def load_yaml(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def get_minatos() -> list[dict]:
    results = []
    if not BASE.exists():
        return results
    for d in sorted(BASE.iterdir()):
        if not d.is_dir():
            continue
        meta = load_yaml(d / "minato_meta.yaml")
        results.append({
            "slug": d.name,
            "name": meta.get("name", d.name),
            "description": meta.get("description", "—"),
            "status": meta.get("status", "unknown"),
        })
    return results


def get_shimanto(minato_slug: str) -> list[dict]:
    shimanto_root = BASE / minato_slug / "shimanto"
    results = []
    if not shimanto_root.exists():
        return results
    for d in sorted(shimanto_root.iterdir()):
        if not d.is_dir():
            continue
        meta = load_yaml(d / "shimanto_meta.yaml")
        results.append({
            "slug": d.name,
            "name": meta.get("name", d.name),
            "description": meta.get("description", "—"),
            "status": meta.get("status", "unknown"),
            "minato": minato_slug,
        })
    return results


def get_nagares_under_shimanto(shimanto_slug: str, minato_slug: str) -> list[dict]:
    nagare_root = BASE / minato_slug / "shimanto" / shimanto_slug / "nagare"
    results = []
    if not nagare_root.exists():
        return results
    for d in sorted(nagare_root.iterdir()):
        if not d.is_dir():
            continue
        wf = load_yaml(d / "workflow.yaml")
        wf_block = wf.get("workflow", {})
        desc = wf_block.get("description", "—")
        if isinstance(desc, str):
            desc = desc.strip().splitlines()[0]  # first line only
        results.append({
            "slug": d.name,
            "name": wf_block.get("name", d.name),
            "version": wf_block.get("version", "?"),
            "description": desc,
            "shimanto": shimanto_slug,
            "minato": minato_slug,
        })
    return results


def get_nagares_under_minato(minato_slug: str) -> list[dict]:
    results = []
    for s in get_shimanto(minato_slug):
        for n in get_nagares_under_shimanto(s["slug"], minato_slug):
            n["shimanto_name"] = s["name"]
            results.append(n)
    return results


# ── Formatters ────────────────────────────────────────────────────────────────

def fmt_minatos(items):
    if not items:
        print("No Minato found.")
        return
    print(f"\n{'MINATO':<24} {'STATUS':<10} DESCRIPTION")
    print("─" * 80)
    for m in items:
        print(f"  {m['name']:<22} {m['status']:<10} {m['description']}")
    print()


def fmt_shimantos(items, minato_slug):
    if not items:
        print(f"No Shimanto found under minato '{minato_slug}'.")
        return
    print(f"\nMINATO: {minato_slug}")
    print(f"{'  SHIMANTO':<26} {'STATUS':<10} DESCRIPTION")
    print("─" * 80)
    for s in items:
        print(f"  {s['name']:<24} {s['status']:<10} {s['description']}")
    print()


def fmt_nagares(items, context=""):
    if not items:
        print(f"No Nagare found{' under ' + context if context else ''}.")
        return
    if context:
        print(f"\n{context}")
    print(f"  {'NAGARE':<32} {'VER':<8} DESCRIPTION")
    print("─" * 80)
    prev_shimanto = None
    for n in items:
        if "shimanto_name" in n and n["shimanto_name"] != prev_shimanto:
            print(f"  ┌ Shimanto: {n['shimanto_name']}")
            prev_shimanto = n["shimanto_name"]
        print(f"  │ {n['name']:<30} {n['version']:<8} {n['description']}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0].lower()

    if cmd == "minato":
        fmt_minatos(get_minatos())

    elif cmd == "shimanto":
        if len(args) < 2:
            print("Usage: flow_registry.py shimanto <minato_slug>")
            sys.exit(1)
        fmt_shimantos(get_shimanto(args[1]), args[1])

    elif cmd == "nagare":
        if "--minato" in args:
            idx = args.index("--minato")
            minato_slug = args[idx + 1]
            fmt_nagares(get_nagares_under_minato(minato_slug),
                        context=f"MINATO: {minato_slug}")
        elif len(args) >= 3:
            shimanto_slug, minato_slug = args[1], args[2]
            fmt_nagares(get_nagares_under_shimanto(shimanto_slug, minato_slug),
                        context=f"MINATO: {minato_slug}  /  SHIMANTO: {shimanto_slug}")
        else:
            print("Usage: flow_registry.py nagare <shimanto_slug> <minato_slug>")
            print("   or: flow_registry.py nagare --minato <minato_slug>")
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
