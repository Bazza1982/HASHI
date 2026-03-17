# HASHI — Windows Offline Installer & Desktop Packaging Plan (v2)

> Purpose: deliver a **user-friendly, offline-capable Windows installer** for HASHI.
> 
> Goal (user experience): **Double-click installer → guided setup → Start Menu/Desktop icon → open & use → repair/diagnostics when broken**.

---

## 1) v2 Core Direction

Compared to v1, v2 focuses less on “how to technically package” and more on a complete, consumer-grade Windows product experience.

### Product principles
1. **Looks like a normal Windows app**
   - Branding, icon, installer wizard, Start Menu entry, uninstall entry.
   - No project folders exposed.

2. **Stable in the background, quiet in the foreground**
   - Backend runs as a Windows Service.
   - Users open **HASHI Desktop**, not Python/cmd/Node consoles.

3. **Windows-native paths**
   - Program files: `C:\Program Files\HASHI\`
   - Data: `C:\ProgramData\HASHI\`

4. **Consumer-style failure handling**
   - Translate technical failures into readable UI messages.
   - Provide **Repair** and **Export diagnostics**.

5. **Advanced features are optional**
   - Codex/Claude/Gemini CLI installation/login should NOT block the main install flow.
   - The “mainline definition of success” is: service + desktop + workbench works.

---

## 2) Target Product Shape (What the user sees)

### User-visible artifacts
- Installer: `HASHI Setup.exe`
- Installed app: `HASHI`
- Start Menu:
  - `HASHI`
  - `HASHI Tools`
  - `Uninstall HASHI`
- Desktop shortcut:
  - `HASHI`

### User should NOT see
- Persistent cmd/PowerShell windows
- `python main.py`, `node server/index.js`, `npm run dev`
- ProgramData paths, logs, ports, or repo structure

---

## 3) Recommended Delivery Architecture

### 3-layer structure
1. **HASHI Backend Service**
   - Freeze Python backend into `hashi-service.exe` (PyInstaller/Nuitka).
   - Install and run as Windows Service.
   - Auto-start + auto-recovery.

2. **HASHI Desktop (Electron)**
   - Desktop entrypoint; renders Workbench UI.
   - Checks service health; translates errors.
   - Hosts/embeds repair/diagnostics UX.

3. **Windows Installer**
   - Delivers a standard `Setup.exe`.
   - Copies files, registers service, writes Start Menu items, first-run self-check.

### Why not “just open a browser to localhost”
It lowers perceived product quality and increases UX risk (tabs closed, browser differences, ports exposed). Electron provides a controlled “real app” surface for status, repair, diagnostics, onboarding, and updates.

---

## 4) Key Reality Check vs Current HASHI9 Repository Structure

### Backend is freeze-friendly
- Entry exists (e.g., `main.py`).
- Dependencies are manageable for PyInstaller/Nuitka.

### Workbench is NOT purely static
`workbench/` includes a local Node layer (`workbench/server/index.js`) responsible for:
- local APIs
- filesystem access
- system probes
- transcript polling

### v2 handling recommendation
Adopt an **Electron main process + preload + IPC** approach and gradually migrate the “local Node capabilities” into the Electron app architecture.

> Note: avoid shipping a release that behaves like “Electron launching a dev-style Node server”.

---

## 5) Installer Choice

Recommended priority:
1. **Inno Setup** (fastest path to a polished traditional Setup.exe)
2. **WiX / MSI** (later, for enterprise deployment & stricter upgrade policies)

Conclusion:
- Phase 1: use **Inno Setup**.
- Phase 2+: consider MSI if needed.

---

## 6) First-Run UX

### What the Desktop app should display
1. Checking HASHI service
2. Starting backend components
3. Connecting workbench
4. Ready

If slow or failed:
- “Still starting…”
- “View details”
- “Repair now”

### What it should not do
- Ask user to open terminals
- Dump raw stack traces
- Expose internal terms like bridge-u-f/PORT=18800

---

## 7) Runtime Strategy

### Backend
Run as **Windows Service**:
- no manual start
- auto recovery
- not tied to user session

Service suggestions:
- Display name: `HASHI Backend Service`
- Startup: `Automatic (Delayed Start)`
- Recovery: restart on first/second/subsequent failure

### Desktop app
- Ensure service running
- Wait for health check
- Enter UI

---

## 8) Data/Logs Layout

- Program: `C:\Program Files\HASHI\`
- Data: `C:\ProgramData\HASHI\`

Suggested folders:
- `config\`
- `logs\`
- `state\`
- `cache\`
- `diagnostics\`

Upgrade behavior:
- upgrade replaces Program Files
- preserves ProgramData
- uninstall asks whether to keep ProgramData

---

## 9) Repair & Diagnostics (Productization)

Provide a separate **HASHI Tools** entry:
- Start HASHI
- Repair HASHI
- View logs
- Export diagnostics
- Re-run initialization

Diagnostics bundle should include:
- version/build info
- service status
- recent logs
- config summary (redacted)
- dependency checks

---

## 10) Technical Implementation Notes

### Backend packaging
- Phase 1: PyInstaller (stability first)
- Phase 2: evaluate Nuitka if size/perf demands it

### Electron structure suggestion
```
desktop/
  electron/
    main.js
    preload.js
    ipc/
```

### Avoid
- shipping dev-style `vite + node server` as the “product runtime”

---

## 11) Security Defaults
- Default bind: `127.0.0.1`
- Do not open LAN/firewall unless user explicitly opts in
- UAC once during install; normal launches should not repeatedly elevate

---

## 12) Release Phases

### Phase 1 (first real user-ready build)
- frozen backend exe
- Windows Service
- Electron Desktop opens reliably
- Inno Setup `Setup.exe`
- install/start/repair/uninstall flows

### Phase 2
- migrate `workbench/server/index.js` capabilities into Electron architecture

### Phase 3
- branding polish, diagnostics UX, repair UX, upgrade strategy

---

## Final recommendation

**Windows Service + Electron Desktop + Inno Setup Setup.exe**

The success metric is not “it runs”, but “a normal Windows user can install, open, and recover it like any other professional desktop product.”
