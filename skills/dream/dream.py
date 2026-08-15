#!/usr/bin/env python3
"""Fail-closed compatibility stub for the retired generic Dream skill.

The live runtime routes ``/skill dream`` and legacy ``skill:dream`` schedules
to native HER Habit Dream before this script can run. Keeping this standalone
entry inert also prevents accidental direct execution from writing general
memory, ``AGENT.md``, schedules, snapshots, or legacy undo state.
"""

from __future__ import annotations


LEGACY_DREAM_RETIRED_MESSAGE = (
    "🌙 Legacy Dream is retired. Use the native HER-only /dream command. "
    "No general memory, AGENT.md, schedule, snapshot, or undo data was changed."
)


def main() -> None:
    print(LEGACY_DREAM_RETIRED_MESSAGE)


if __name__ == "__main__":
    main()
