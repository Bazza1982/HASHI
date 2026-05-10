from __future__ import annotations

import importlib
import logging
from typing import Any


logger = logging.getLogger("FlexRuntime.ticket")


async def cmd_ticket(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    ticket_manager = importlib.import_module("orchestrator.ticket_manager")
    args_text = " ".join(context.args).strip() if context.args else ""

    if not args_text:
        tickets_dir = ticket_manager._resolve_tickets_dir(runtime.global_config.project_root)
        open_tickets = ticket_manager.list_tickets(tickets_dir, "open")
        in_progress_tickets = ticket_manager.list_tickets(tickets_dir, "in_progress")
        lines = []
        if open_tickets:
            lines.append("Open tickets:")
            for ticket in open_tickets:
                lines.append(f"  [{ticket['ticket_id']}] {ticket['source_agent']} — {ticket['summary'][:60]}")
        if in_progress_tickets:
            lines.append("In progress:")
            for ticket in in_progress_tickets:
                lines.append(f"  [{ticket['ticket_id']}] {ticket['source_agent']} — {ticket['summary'][:60]}")
        if not lines:
            lines.append("No open tickets.")
        await runtime._reply_text(update, "\n".join(lines))
        return

    instance = ticket_manager.detect_instance(runtime.global_config.project_root)
    ticket = ticket_manager.create_ticket(
        project_root=runtime.global_config.project_root,
        source_agent=runtime.name,
        source_instance=instance,
        workspace_dir=runtime.workspace_dir,
        summary=args_text,
    )
    await runtime._reply_text(
        update,
        f"🎫 Ticket {ticket['ticket_id']} created.\n"
        f"Arale has been notified and will investigate.",
    )

    notification = ticket_manager.format_ticket_notification(ticket)
    orchestrator = getattr(runtime, "orchestrator", None)
    notified = False

    if orchestrator is not None:
        for other_runtime in getattr(orchestrator, "runtimes", []):
            if getattr(other_runtime, "name", "") == "arale" and hasattr(other_runtime, "enqueue_api_text"):
                try:
                    await other_runtime.enqueue_api_text(
                        f"[TICKET RECEIVED]\n{notification}\n\n"
                        f"Ticket file: {runtime.global_config.project_root / 'tickets' / 'open' / (ticket['ticket_id'] + '.json')}\n"
                        f"Please investigate and resolve per IT support protocol.",
                        source=f"ticket:{ticket['ticket_id']}",
                        deliver_to_telegram=True,
                    )
                    notified = True
                except Exception as exc:
                    logger.warning("Failed to notify arale via bridge: %s", exc)
                break

    if not notified:
        try:
            send_hchat = importlib.import_module("tools.hchat_send").send_hchat
            hchat_text = (
                f"[TICKET RECEIVED]\n{notification}\n\n"
                f"Ticket file: {runtime.global_config.project_root / 'tickets' / 'open' / (ticket['ticket_id'] + '.json')}\n"
                f"Please investigate and resolve per IT support protocol."
            )
            ok = send_hchat("arale", runtime.name, hchat_text)
            if ok:
                notified = True
                logger.info("Ticket %s notified to arale via hchat.", ticket["ticket_id"])
            else:
                logger.warning("Ticket %s hchat delivery to arale failed. Arale may be offline.", ticket["ticket_id"])
        except Exception as exc:
            logger.warning("Failed to notify arale via hchat: %s", exc)

    if not notified:
        logger.warning(
            "Ticket %s created but could not notify arale. She will pick it up on next patrol.",
            ticket["ticket_id"],
        )
