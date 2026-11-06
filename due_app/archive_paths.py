"""Bind immutable recording identities to this workspace after a verified restore."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import APP_ID

RESTORED_ROOTS_KEY = 'verified_restore_archive_roots'


def path_key(value):
    """Compare stored absolute paths without following the old location's links."""
    if not isinstance(value, (str, Path)) or not Path(value).is_absolute():
        raise ValueError('recording output root must be absolute')
    return os.path.normcase(os.path.normpath(str(value)))


def bound_archive_config(store, context, recording_id):
    """Preserve owner/source data; only a verified restore can rebind an old root.

    Ordinary databases still reject contexts for another root. Restore metadata
    is written only after every archive member and the database were verified.
    No file in the former workspace is opened, including when that workspace
    no longer exists.
    """
    from shared_recording_core import SessionConfig

    root = store.root.resolve()
    expected = root / 'recordings'
    if expected.resolve() != expected or (hasattr(expected, 'is_junction') and expected.is_junction()):
        raise ValueError('recording directory must not redirect to another location')
    if not isinstance(context, dict):
        raise ValueError('recording context must be an object')
    original_root = path_key(context.get('output_root'))
    target_root = path_key(expected)
    if original_root != target_root:
        with store.lock, store.connect() as connection:
            row = connection.execute('SELECT value FROM app_meta WHERE key=?', (RESTORED_ROOTS_KEY,)).fetchone()
        restored = json.loads(row[0]) if row else None
        if (not isinstance(restored, dict) or restored.get('schema') != 'due-archive-relocation/1'
                or path_key(restored.get('target_root')) != target_root
                or original_root not in restored.get('original_roots', [])):
            raise ValueError('recording context does not belong to this application workspace')
    config = SessionConfig.from_dict({**context, 'output_root': str(expected)})
    if config.app_id != APP_ID or config.session_id != recording_id:
        raise ValueError('recording context does not belong to this application session')
    return config


def register_restored_archive_roots(connection, destination):
    """Add location metadata without rewriting recordings or source snapshots."""
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recordings'").fetchone()
    if not exists:
        return None
    from shared_recording_core import SessionConfig
    target = Path(destination).resolve() / 'recordings'
    originals = set()
    for recording_id, app_id, payload in connection.execute('SELECT id,app_id,context_json FROM recordings'):
        context = json.loads(payload)
        config = SessionConfig.from_dict({**context, 'output_root': str(target)})
        if app_id != APP_ID or config.app_id != APP_ID or config.session_id != recording_id:
            raise ValueError('recording identity does not match restored database')
        original = path_key(context.get('output_root'))
        if original != path_key(target):
            originals.add(original)
    if not originals:
        return None
    metadata = {'schema': 'due-archive-relocation/1', 'target_root': path_key(target),
                'original_roots': sorted(originals)}
    connection.execute('INSERT OR REPLACE INTO app_meta(key,value) VALUES (?,?)',
                       (RESTORED_ROOTS_KEY, json.dumps(metadata, ensure_ascii=False, sort_keys=True)))
    return metadata
