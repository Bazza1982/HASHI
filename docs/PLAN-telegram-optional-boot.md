# Plan: Telegram-Optional Agent Boot (Local Mode Support)

**Author**: 小蕾  
**Date**: 2025-07-15  
**Status**: ✅ IMPLEMENTED

---

## Executive Summary

Currently, if Telegram connection fails during agent startup, the entire agent fails to start. This is problematic because:

1. **Workbench** provides a fully functional chat interface that doesn't require Telegram
2. **WhatsApp** transport is independent and may still be connected
3. **Backend** (Gemini/Claude/Codex/OpenRouter) is already initialized and ready

This plan outlines modifications to allow agents to start in "local mode" when Telegram is unavailable, while still providing full functionality through Workbench and WhatsApp.

---

## Current Architecture

### Agent Lifecycle
```
main.py::_start_runtime()
  ├── backend.initialize() ✓
  ├── telegram_preflight() ← BLOCKS if fails
  ├── app.initialize()
  ├── app.start()
  └── app.updater.start_polling()
```

### Status Model (Current)
- `startup_success: bool` — Set to `True` only after successful Telegram polling start
- Used by:
  - `get_runtime_metadata()` → Workbench `/api/agents`
  - `_routing_status()` → WhatsApp `/agent` command
  - `/status` command

### Command Routing (Current)
| Channel | Command Handling |
|---------|------------------|
| Telegram | `CommandHandler` bound via `bind_handlers()` |
| WhatsApp | `/agent`, `/all` at transport level; others forwarded to agent queue |
| Workbench | `execute_local_command()` directly invokes `cmd_*` methods |

---

## Proposed Changes

### 1. Status Model Refactoring

**File**: `orchestrator/agent_runtime.py`, `orchestrator/flexible_agent_runtime.py`

```python
# New state attributes (add to __init__)
self.backend_ready: bool = False
self.telegram_connected: bool = False
self.telegram_app_initialized: bool = False

# startup_success now means "agent is operational" (backend ready)
# NOT "Telegram is connected"
```

**`get_runtime_metadata()` update**:
```python
def get_runtime_metadata(self) -> dict:
    return {
        # ... existing fields ...
        "online": bool(self.backend_ready),  # Changed from startup_success
        "status": self._compute_status_string(),
        "telegram_connected": self.telegram_connected,
        "telegram_status": "connected" if self.telegram_connected else "disconnected",
        "channels": {
            "telegram": self.telegram_connected,
            "workbench": True,  # Always available if agent is in runtimes
            "whatsapp": self._get_whatsapp_status(),
        },
    }

def _compute_status_string(self) -> str:
    if not self.backend_ready:
        return "offline"
    if self.telegram_connected:
        return "online"
    return "local"  # Backend ready, Telegram disconnected
```

---

### 2. Startup Logic Modification

**File**: `main.py`

**Current flow** (lines ~545-590):
```python
preflight_ok = await self.telegram_preflight(rt.token, rt.name)
if not preflight_ok:
    # After 3 retries...
    return False, f"Telegram preflight failed..."  # BLOCKS STARTUP
```

**Proposed flow**:
```python
async def _start_runtime(self, rt) -> tuple[bool, str]:
    # Stage 1: Backend initialization (unchanged)
    backend_ready = await rt.backend.initialize()
    if not backend_ready:
        return False, f"Backend for '{rt.name}' failed to initialize."
    rt.backend_ready = True

    # Stage 2: Telegram connection (no longer blocking)
    telegram_ok = await self._try_telegram_connect(rt)
    
    # Stage 3: Start queue processor regardless of Telegram status
    rt.startup_success = True  # Agent is operational
    rt.process_task = asyncio.create_task(rt.process_queue())
    
    if telegram_ok:
        return True, f"Started agent '{rt.name}'."
    else:
        return True, f"Started '{rt.name}' in LOCAL MODE (Telegram unavailable)."


async def _try_telegram_connect(self, rt) -> bool:
    """Attempt Telegram connection. Returns success status but doesn't block startup."""
    for attempt in range(1, 4):
        preflight_ok = await self.telegram_preflight(rt.token, rt.name)
        if not preflight_ok:
            if attempt < 3:
                main_logger.warning(
                    f"Telegram preflight failed for '{rt.name}' (attempt {attempt}/3). "
                    f"Retrying in 5s..."
                )
                await asyncio.sleep(5)
                continue
            # All attempts failed - continue in local mode
            main_logger.warning(
                f"⚠️ Telegram unavailable for '{rt.name}'. "
                f"Agent will run in LOCAL MODE (Workbench + WhatsApp only)."
            )
            rt.telegram_connected = False
            return False
        
        try:
            await rt.app.initialize()
            await rt.app.start()
            await rt.app.updater.start_polling(
                drop_pending_updates=True,
                error_callback=getattr(rt, "handle_polling_error", None),
            )
            rt.telegram_connected = True
            rt.telegram_app_initialized = True
            await rt.app.bot.set_my_commands(rt.get_bot_commands())
            return True
        except Exception as e:
            if attempt < 3:
                await self._cleanup_telegram_partial(rt)
                await asyncio.sleep(5)
                continue
            main_logger.warning(f"Telegram start failed for '{rt.name}': {e}")
            rt.telegram_connected = False
            return False
    
    return False
```

---

### 3. Message Sending Protection

**File**: `orchestrator/agent_runtime.py`

All methods that interact with Telegram API need guards:

```python
async def send_long_message(self, chat_id, text, request_id, purpose):
    if not self.telegram_connected:
        self.logger.info(
            f"Telegram disconnected — skipping send for {request_id} "
            f"(text will still appear in transcript for Workbench)"
        )
        # Still log to transcript so Workbench can display it
        return 0.0, 0
    
    # ... existing send logic ...


async def typing_loop(self, chat_id, stop_event):
    if not self.telegram_connected:
        return
    # ... existing logic ...


async def _send_voice_reply(self, chat_id, text, request_id):
    if not self.telegram_connected:
        return
    # ... existing logic ...
```

**Methods requiring guards**:
- `send_long_message()`
- `typing_loop()`
- `_send_voice_reply()`
- `_escalating_placeholder_loop()`
- `_streaming_display_loop()`
- `_thinking_flush_loop()`
- `_flush_thinking()`

---

### 4. Queue Processing Update

**File**: `orchestrator/agent_runtime.py`

The queue processing logic needs to handle `deliver_to_telegram` more carefully:

```python
async def process_queue(self):
    while True:
        item = await self.queue.get()
        # ...
        
        # If Telegram is disconnected, force deliver_to_telegram = False
        # but still process the request (for Workbench polling)
        effective_deliver_to_telegram = (
            item.deliver_to_telegram and self.telegram_connected
        )
        
        if not item.silent and effective_deliver_to_telegram:
            # Telegram UI: placeholders, typing, etc.
            ...
        
        # Backend generation always happens
        response = await self.backend.generate_response(...)
        
        # Log to transcript (always, for Workbench)
        self._log_to_transcript(...)
        
        if not item.silent and effective_deliver_to_telegram:
            # Telegram delivery
            await self.send_long_message(...)
```

---

### 5. WhatsApp Status Display Update

**File**: `transports/whatsapp.py`

```python
def _routing_status(self, chat_key: str) -> str:
    """Build a status string: current routing + all agents with channel status."""
    # ... existing routing section ...

    lines.append("")
    lines.append("Agents:")
    running_names = {rt.name for rt in self.orchestrator.runtimes}
    
    for name in self.orchestrator.configured_agent_names():
        rt = self._get_runtime(name)
        if rt is None:
            lines.append(f"  ✗ {name} (stopped)")
        elif not getattr(rt, 'backend_ready', False):
            lines.append(f"  ✗ {name} (backend error)")
        elif not getattr(rt, 'telegram_connected', True):
            lines.append(f"  ⚡ {name} (local mode)")  # New status!
        else:
            lines.append(f"  ✓ {name}")

    return "\n".join(lines)

def _get_runtime(self, name: str):
    for rt in self.orchestrator.runtimes:
        if rt.name == name:
            return rt
    return None
```

---

### 6. Workbench API Update

**File**: `orchestrator/workbench_api.py`

```python
def _metadata_for_agent(self, agent_row: dict, runtime) -> dict:
    if runtime is not None:
        metadata = runtime.get_runtime_metadata()
        # Add channel availability
        metadata["channels"] = {
            "telegram": getattr(runtime, 'telegram_connected', False),
            "workbench": True,
            "whatsapp": self._is_whatsapp_available(),
        }
    else:
        # ... offline agent metadata ...
        metadata["channels"] = {
            "telegram": False,
            "workbench": False,
            "whatsapp": False,
        }
    return metadata

def _is_whatsapp_available(self) -> bool:
    if self.orchestrator is None:
        return False
    wa = getattr(self.orchestrator, 'whatsapp', None)
    return wa is not None and getattr(wa, '_client', None) is not None
```

---

### 7. /status Command Update

**File**: `orchestrator/agent_runtime.py`

```python
def _build_status_text(self, detailed: bool = False) -> str:
    lines = [
        f"Agent: {self.name}",
        f"Engine: {self.config.engine}",
        f"Model: {self.config.model}",
    ]
    
    # Channel status section (NEW)
    lines.append("")
    lines.append("Channels:")
    lines.append(f"  Telegram: {'✓ connected' if self.telegram_connected else '✗ disconnected'}")
    lines.append(f"  Workbench: ✓ available")
    wa_status = self._get_whatsapp_status_text()
    lines.append(f"  WhatsApp: {wa_status}")
    
    # ... rest of status ...
    
    return "\n".join(lines)

def _get_whatsapp_status_text(self) -> str:
    orchestrator = getattr(self, 'orchestrator', None)
    if orchestrator is None:
        return "N/A"
    wa = getattr(orchestrator, 'whatsapp', None)
    if wa is None:
        return "✗ not configured"
    if getattr(wa, '_client', None) is None:
        return "✗ disconnected"
    return "✓ connected"
```

---

### 8. Lifecycle Commands via Alternative Channels

These commands need to work from Workbench and WhatsApp even when Telegram is down:

| Command | Current Status | Notes |
|---------|---------------|-------|
| `/status` | ✓ Works via `execute_local_command` | No changes needed |
| `/model` | ✓ Works | No changes needed |
| `/new` | ✓ Works | No changes needed |
| `/stop` | ✓ Works | No changes needed |
| `/reboot` | ⚠️ Needs review | Uses `orchestrator.request_restart()` |
| `/terminate` | ⚠️ Needs review | Uses `orchestrator.stop_agent()` |
| `/start` | ⚠️ Needs review | Uses `orchestrator.start_agent()` |

**For `/reboot`, `/terminate`, `/start`**:

These rely on callbacks/keyboards which are Telegram-specific. However:
- The underlying orchestrator methods work independently
- WhatsApp can invoke them via transport-level handling
- Workbench can invoke them via `/api/admin/*` endpoints

**Recommendation**: Add WhatsApp transport-level handling for lifecycle commands:

```python
# transports/whatsapp.py

async def _handle_routing_command(self, chat_key: str, text: str):
    first_word = text.split()[0].lower()
    
    if first_word == "/reboot":
        # Handle at transport level
        await self._handle_reboot_command(chat_key, text)
        return
    
    if first_word == "/terminate":
        await self._handle_terminate_command(chat_key, text)
        return
    
    # ... existing /agent, /all handling ...
```

---

### 9. Optional: Telegram Reconnection

**File**: `orchestrator/agent_runtime.py` or `main.py`

```python
async def _telegram_reconnect_loop(self, rt, interval_seconds: int = 300):
    """Periodically attempt to reconnect Telegram if disconnected."""
    while True:
        await asyncio.sleep(interval_seconds)
        
        if rt.telegram_connected:
            continue
        
        if self.shutdown_event.is_set():
            break
        
        main_logger.info(f"Attempting Telegram reconnect for '{rt.name}'...")
        success = await self._try_telegram_connect(rt)
        
        if success:
            main_logger.info(f"✓ Telegram reconnected for '{rt.name}'!")
            # Optionally notify via WhatsApp/Workbench
```

---

## Implementation Order

### Phase 1: Core Changes (Required)
1. Add new state attributes to `agent_runtime.py` and `flexible_agent_runtime.py`
2. Modify `main.py::_start_runtime()` to not block on Telegram
3. Add Telegram guards to message sending methods
4. Update `get_runtime_metadata()` 

### Phase 2: UI Updates
5. Update `_routing_status()` in WhatsApp transport
6. Update `_build_status_text()` for `/status` command
7. Update Workbench API metadata

### Phase 3: Lifecycle Commands
8. Add WhatsApp transport-level handling for `/reboot`, `/terminate`
9. Test lifecycle commands from all channels

### Phase 4: Polish (Optional)
10. Implement Telegram reconnection loop
11. Add reconnection status to `/status`
12. Console banner update for local-mode agents

---

## Testing Checklist

- [x] Agent starts successfully when Telegram API is unreachable
- [x] Agent starts successfully when Telegram token is invalid
- [x] Workbench chat works in local mode
- [x] WhatsApp routing works in local mode
- [x] `/status` shows correct channel status
- [x] `/agent` (WhatsApp) shows "local mode" indicator
- [x] Workbench `/api/agents` shows channel breakdown
- [x] `/reboot` works from Workbench (via existing `/api/admin/*`)
- [x] `/reboot` works from WhatsApp
- [x] `/terminate` works from Workbench (via existing `/api/admin/*`)
- [x] `/terminate` works from WhatsApp
- [ ] Hot restart preserves local-mode status (needs runtime testing)
- [ ] Telegram reconnection works (Phase 4 — deferred)

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Transcript logging changes | Ensure transcript writes happen regardless of Telegram status |
| WhatsApp command forwarding | Agent queue still processes; just skip Telegram delivery |
| Workbench polling | No change needed — reads from transcript files |
| Voice replies | Guard with `telegram_connected` check |
| Startup announcement | Send via WhatsApp if Telegram unavailable |

---

## Files to Modify

| File | Changes |
|------|---------|
| `main.py` | `_start_runtime()`, `_try_telegram_connect()` |
| `orchestrator/agent_runtime.py` | State attrs, guards, `get_runtime_metadata()`, `_build_status_text()` |
| `orchestrator/flexible_agent_runtime.py` | Same as above |
| `orchestrator/workbench_api.py` | `_metadata_for_agent()` |
| `transports/whatsapp.py` | `_routing_status()`, lifecycle command handling |

---

## Questions for Review

1. Should we implement Telegram reconnection in Phase 1 or defer to Phase 4?
2. Should local-mode agents still attempt to send startup announcements via WhatsApp?
3. Should the console banner show a different indicator for local-mode agents?
4. Do we need a `/reconnect` command to manually trigger Telegram reconnection?

---

*Plan prepared by 小蕾 for 爸爸's review* 🌸
