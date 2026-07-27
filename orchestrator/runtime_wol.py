from __future__ import annotations

import asyncio
from typing import Any

from orchestrator.private_wol import (
    describe_wol_targets,
    private_wol_available,
    run_private_wol,
)


async def cmd_wol(runtime: Any, update: Any, context: Any) -> None:
    """Run the optional instance-aware local Wake-on-LAN adapter."""
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    project_root = runtime.global_config.project_root
    instance_id = getattr(runtime.global_config, "instance_id", None)
    if not private_wol_available(project_root, instance_id):
        await runtime._reply_text(update, "⚪ /wol is not enabled on this instance.")
        return

    arg = context.args[0].strip().lower() if context.args else ""
    if not arg or arg in {"list", "status", "help"}:
        targets = describe_wol_targets(project_root)
        lines = ["🪄 Private WoL targets on this instance:"]
        for row in targets:
            description = f" — {row['description']}" if row["description"] else ""
            lines.append(f"- {row['name']} ({row['label']}){description}")
        lines.extend(["", "Usage: /wol <pc_name>"])
        await runtime._reply_text(update, "\n".join(lines))
        return

    await runtime._reply_text(update, f"🪄 Sending Wake-on-LAN packet for `{arg}`…")
    result = await asyncio.to_thread(
        run_private_wol,
        project_root,
        arg,
        configured_instance_id=instance_id,
    )
    if result.get("ok"):
        output = str(result.get("stdout") or "").strip()
        if len(output) > 2500:
            output = output[:2500] + "\n...[truncated]"
        lines = [f"✅ WoL completed for {result.get('label') or arg}."]
        if output:
            lines.extend(["", output])
        await runtime._reply_text(update, "\n".join(lines))
        return

    error = result.get("error") or result.get("stderr") or "unknown error"
    lines = [f"❌ WoL failed for {arg}: {error}"]
    available = result.get("available_targets") or []
    if available:
        lines.append(f"Available targets: {', '.join(available)}")
    await runtime._reply_text(update, "\n".join(lines))
