"""Portable HASHI-to-HASHI Agent move support.

The public API is intentionally small.  ``package`` owns the stable wire
format, ``service`` owns target-local mutations, and ``remote_client`` is the
source-side transport used by the Telegram ``/move`` command.
"""

from .package import (
    AGENT_MOVE_CAPABILITY,
    AGENT_TRANSFER_LIFECYCLE_CAPABILITY,
    PACKAGE_SCHEMA_MIN_VERSION,
    PACKAGE_SCHEMA_VERSION,
    PACKAGE_TYPE,
    RETAINED_IDENTITY_CAPABILITY,
    AgentMoveArchive,
    AgentMoveError,
    create_agent_move_package,
    read_agent_move_package,
)
from .service import (
    activate_agent_move,
    cleanup_source_agent,
    commit_agent_move,
    deactivate_source_agent,
    get_agent_move_status,
    finalize_agent_move,
    receiver_capabilities,
    moved_agent_destination,
    restore_source_agent,
    resolve_agent_transfer_target,
    rollback_agent_move,
    stage_agent_move,
)

__all__ = [
    "AGENT_MOVE_CAPABILITY",
    "AGENT_TRANSFER_LIFECYCLE_CAPABILITY",
    "PACKAGE_SCHEMA_MIN_VERSION",
    "PACKAGE_SCHEMA_VERSION",
    "PACKAGE_TYPE",
    "RETAINED_IDENTITY_CAPABILITY",
    "AgentMoveArchive",
    "AgentMoveError",
    "activate_agent_move",
    "cleanup_source_agent",
    "commit_agent_move",
    "create_agent_move_package",
    "deactivate_source_agent",
    "finalize_agent_move",
    "get_agent_move_status",
    "moved_agent_destination",
    "read_agent_move_package",
    "receiver_capabilities",
    "restore_source_agent",
    "resolve_agent_transfer_target",
    "rollback_agent_move",
    "stage_agent_move",
]
