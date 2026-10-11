"""Small shared helpers. No networking, disk caches, or third-party packages."""
import hashlib
import json
import os
from pathlib import Path

BASE = Path.home() / '.config/uc-sync'
SOURCE = Path.home() / 'UC'
REPORT_NAMES = {'Latest sync.md', 'Sync log.md'}
INDEX_NAMES = {'Upload dates.csv', 'Upload dates.md'}

def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.uc-sync-part')
    try:
        with tmp.open('w', encoding='utf-8', newline='') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()
