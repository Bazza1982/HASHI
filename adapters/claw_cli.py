"""Compatibility facade for the former public ``claw-cli`` backend.

HASHI Engine Runtime (HER) is the public HASHI backend. This module deliberately
re-exports private helpers too because older HASHI tests and integrations used
them before the backend was renamed. New code should import :mod:`adapters.her`.
"""

from adapters import her as _her

for _name in dir(_her):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_her, _name)

ClawCLIAdapter = _her.HERAdapter
