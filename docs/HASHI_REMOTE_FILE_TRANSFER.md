# Hashi Remote File Transfer

Hashi Remote supports direct cross-PC file push through the remote API.

## API

- `POST /files/push`
  - Auth: existing Hashi Remote bearer token, or LAN mode auto-auth.
  - Writes atomically via a temporary file and replace.
  - Refuses overwrite unless `overwrite=true`.
  - Verifies `sha256` when provided.
  - Creates parent directories by default.
  - Rejects relative paths that escape the remote Hashi root.
  - Max payload: 256 MiB.

- `GET /files/stat?path=<path>`
  - Returns existence, type, size, and sha256 for files.

## CLI

```bash
python tools/remote_file_transfer.py push ./report.md HASHI9:/tmp/report.md
python tools/remote_file_transfer.py push ./report.md HASHI9:C:\\Users\\me\\Desktop\\report.md --overwrite
python tools/remote_file_transfer.py stat HASHI9:/tmp/report.md
```

When Hashi Remote LAN mode is off, pass `--token` or set `HASHI_REMOTE_TOKEN`.

## Path Rules

Absolute destination paths are interpreted on the target PC. Relative paths are
resolved under the target instance's Hashi root and may not traverse outside it.
