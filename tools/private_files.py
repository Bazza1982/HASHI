"""Platform adaptation for owner-only local credential files."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess


def protect_private_file(path: Path) -> None:
    if os.name != 'nt':
        path.chmod(0o600)
        return
    # icacls applies a real Windows DACL; chmod alone only changes read-only flags.
    system = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
    result = subprocess.run([str(system/'whoami.exe')],capture_output=True,text=True,
                            check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    account = result.stdout.strip()
    if not account or '\n' in account or '\r' in account:
        raise OSError('Unable to identify the credential file owner')
    result = subprocess.run([str(system/'icacls.exe'),str(path),'/inheritance:r','/grant:r',
                             account+':(F)','*S-1-5-18:(F)','*S-1-5-32-544:(F)'],
                            capture_output=True,check=False,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise OSError('Unable to protect the credential file DACL')
