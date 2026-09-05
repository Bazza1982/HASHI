"""Portable HASHI-to-HASHI Agent move support.

The public API is intentionally small.  ``package`` owns the stable wire
format, ``service`` owns target-local mutations, and ``remote_client`` is the
source-side transport used by the Telegram ``/move`` command.
"""

from .package import (
    AGENT_MOVE_CAPABILITY,
    PACKAGE_SCHEMA_VERSION,
    PACKAGE_TYPE,
    AgentMoveArchive,
    AgentMoveError,
    create_agent_move_package,
    read_agent_move_package,
)
from .service import (
    activate_agent_move,
    commit_agent_move,
    deactivate_source_agent,
    get_agent_move_status,
    receiver_capabilities,
    restore_source_agent,
    rollback_agent_move,
    stage_agent_move,
)

__all__ = [
    "AGENT_MOVE_CAPABILITY",
    "PACKAGE_SCHEMA_VERSION",
    "PACKAGE_TYPE",
    "AgentMoveArchive",
    "AgentMoveError",
    "activate_agent_move",
    "commit_agent_move",
    "create_agent_move_package",
    "deactivate_source_agent",
    "get_agent_move_status",
    "read_agent_move_package",
    "receiver_capabilities",
    "restore_source_agent",
    "rollback_agent_move",
    "stage_agent_move",
]
