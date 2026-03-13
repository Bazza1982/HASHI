# bridge-u-f Workbench

This is the local Node/React workbench bundled inside `bridge-u-f`.

## Purpose

The workbench is a second operator interface for the same in-process agent runtimes used by Telegram.

It provides:

- a local multi-agent chat UI
- transcript polling for fixed and flex agents
- local system status display
- text and file/media send support through the bridge workbench API

## Runtime Shape

- backend server: `workbench/server/index.js`
- frontend app: `workbench/src/App.jsx`
- local bridge API target: `http://127.0.0.1:18800`

## Start / Stop

From the repo root:

- `workbench.bat` starts the supervised workbench services
- `stop_workbench.bat` stops the managed workbench services
- `restart_workbench.bat` restarts and health-checks them

Logs and PID files are written under:

- `state/workbench/`
- `state/workbench/logs/`

## Notes

- This workbench is self-contained inside `bridge-u-f`; it should not depend on any external `bridge-u-workbench` folder.
- The workbench and Telegram share the same per-agent runtime queues and backend sessions. See `WORKBENCH_NOTES.md` for the shared-session behavior details.
