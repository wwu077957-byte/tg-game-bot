"""Prepare the persistent volume and drop privileges before starting TGVault."""
import os
from pathlib import Path

UID = 10001
GID = 10001
DATA_DIR = Path('/data')

if os.geteuid() != 0:
    raise RuntimeError('Container must start as root to prepare the mounted volume')
DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chown(DATA_DIR, UID, GID)
os.chmod(DATA_DIR, 0o700)
os.setgroups([])
os.setgid(GID)
os.setuid(UID)
os.execvp('python', ['python', '-m', 'tg_manager.bot'])
