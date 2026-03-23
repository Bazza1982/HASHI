#!/usr/bin/env python3
"""
Bridge-U-F zero-install USB launcher
"""
import os
import sys
import shutil
import subprocess
from pathlib import Path

EXE_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent

def ensure_config():
    """若无 agents.json / secrets.json，则从 samples 复制一份到 exe 同目录"""
    agents_dst = EXE_DIR / 'agents.json'
    secrets_dst = EXE_DIR / 'secrets.json'
    agents_sample = EXE_DIR / 'agents.json.sample'
    secrets_sample = EXE_DIR / 'secrets.json.sample'

    if not agents_dst.exists() and agents_sample.exists():
        shutil.copy(agents_sample, agents_dst)
    if not secrets_dst.exists() and secrets_sample.exists():
        shutil.copy(secrets_sample, secrets_dst)

    # 目录若不存在就建
    (EXE_DIR / 'workspaces').mkdir(exist_ok=True)
    (EXE_DIR / 'wa_session').mkdir(exist_ok=True)


def main():
    ensure_config()
    os.chdir(EXE_DIR)
    # 调用真正的 Bridge-U-F
    subprocess.run([sys.executable, '-m', 'main'])


if __name__ == '__main__':
    main()