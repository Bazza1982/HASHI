# Hashi Remote File Transfer

Planned upgrade: see
[HASHI_REMOTE_FILE_TRANSFER_AND_ATTACHMENTS_PLAN.md](HASHI_REMOTE_FILE_TRANSFER_AND_ATTACHMENTS_PLAN.md)
for the shared-token HMAC file transfer upgrade and message attachment design.

Hashi Remote supports direct cross-PC file push through the remote API.
When both peers advertise attachment capability, the same trusted Remote layer
also supports combined cross-instance message + attachment delivery.

## API

- `POST /files/push`
  - Auth:
    - shared-token HMAC via Hashi protocol auth headers
    - existing Hashi Remote bearer token
    - LAN mode auto-auth
  - Writes atomically via a temporary file and replace.
  - Refuses overwrite unless `overwrite=true`.
  - Verifies `sha256` when provided.
  - Creates parent directories by default.
  - Rejects relative paths that escape the remote Hashi root.
  - Max payload: 256 MiB.

- `GET /files/stat?path=<path>`
  - Auth:
    - shared-token HMAC via Hashi protocol auth headers
    - existing Hashi Remote bearer token
    - LAN mode auto-auth
  - Returns existence, type, size, and sha256 for files.

## CLI

```bash
python tools/remote_file_transfer.py push ./report.md HASHI9:/tmp/report.md
python tools/remote_file_transfer.py push ./report.md HASHI9:C:\\Users\\me\\Desktop\\report.md --overwrite
python tools/remote_file_transfer.py stat HASHI9:/tmp/report.md
python tools/remote_file_transfer.py --shared-token "$HASHI_REMOTE_SHARED_TOKEN" --from-instance HASHI1 push ./report.md HASHI9:/tmp/report.md
python tools/protocol_send.py --to agent1@INTEL --from zelda --text "hello from HASHI1" --shared-token "$HASHI_REMOTE_SHARED_TOKEN"
python tools/protocol_send.py --to agent1@INTEL --from zelda --text "see attached" --attach ./report.txt --shared-token "$HASHI_REMOTE_SHARED_TOKEN"
```

Auth selection rules:

- if `--token` or `HASHI_REMOTE_TOKEN` is provided, the CLI uses bearer auth
- otherwise, if `--shared-token` or `HASHI_REMOTE_SHARED_TOKEN` is provided, the
  CLI signs `/files/push` and `/files/stat` with shared-token HMAC
- otherwise, the CLI relies on LAN mode auto-auth when enabled on the target

Shared-token mode requires a sender identity:

- pass `--from-instance HASHI1`, or
- set `HASHI_INSTANCE_ID`, or
- keep `global.instance_id` in local `agents.json`

Capability rules:

- plain `/protocol/message` chat does not require attachment support
- attachment send requires the target peer to advertise `message_attachments_v1`
- direct shared-token file transfer requires the target peer to advertise
  `file_transfer_hmac_v1`
- the CLIs now probe live `/protocol/status` before HMAC file-transfer and
  attachment operations so stale peer metadata fails clearly

Windows/LAN stability note:

- Some LAN PCs bind the local Workbench API to the machine's LAN IP instead of
  `127.0.0.1`.
- Hashi Remote now widens its local Workbench host fallback to include real
  interface IPv4 addresses, preventing `local_enqueue_failed` on otherwise
  healthy Windows peers.
- If a peer can handshake but cannot receive plain protocol replies, verify the
  peer has pulled a version that includes this fallback widening.

## Path Rules

Absolute destination paths are interpreted on the target PC. Relative paths are
resolved under the target instance's Hashi root and may not traverse outside it.
