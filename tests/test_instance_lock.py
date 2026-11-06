"""Data-root ownership: isolated OS processes and synthetic application roots."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from due_app.instance_lock import InstanceBusy, InstanceLock


APP_ROOT = Path(__file__).resolve().parents[1]
ATTEMPT = '''
import sys
from due_app.instance_lock import InstanceLock, InstanceBusy
try:
    with InstanceLock(sys.argv[1]):
        print('acquired')
except InstanceBusy:
    print('busy')
    raise SystemExit(7)
'''
HOLDER = '''
import sys, time
from pathlib import Path
from due_app.instance_lock import InstanceLock
with InstanceLock(sys.argv[1]):
    Path(sys.argv[2]).write_text('ready', encoding='utf-8')
    time.sleep(60)
'''


def attempt(root):
    return subprocess.run([sys.executable, '-c', ATTEMPT, str(root)], cwd=APP_ROOT,
                          capture_output=True, text=True, timeout=10)


def test_same_process_same_canonical_root_rejects_second_owner(tmp_path):
    with InstanceLock(tmp_path):
        with pytest.raises(InstanceBusy):
            InstanceLock(tmp_path / '.').acquire()
    with InstanceLock(tmp_path):
        pass


def test_different_data_roots_can_be_owned_together(tmp_path):
    with InstanceLock(tmp_path / 'one'), InstanceLock(tmp_path / 'two'):
        assert attempt(tmp_path / 'one').returncode == 7
        assert attempt(tmp_path / 'two').returncode == 7
        assert attempt(tmp_path / 'three').returncode == 0


def test_stale_lock_file_does_not_imply_live_owner(tmp_path):
    lock_file = tmp_path / '.due-instance.lock'
    lock_file.write_bytes(b'old-lock-file')
    with InstanceLock(tmp_path):
        assert lock_file.is_file()
    # It is intentionally retained; deleting it would split POSIX ownership.
    assert lock_file.read_bytes() == b'old-lock-file'
    assert attempt(tmp_path).returncode == 0


def test_context_exception_releases_ownership(tmp_path):
    with pytest.raises(ValueError):
        with InstanceLock(tmp_path):
            raise ValueError('synthetic failure')
    assert attempt(tmp_path).returncode == 0


def test_other_process_rejected_until_owner_is_forcibly_terminated(tmp_path):
    root, ready = tmp_path / 'data', tmp_path / 'ready'
    owner = subprocess.Popen([sys.executable, '-c', HOLDER, str(root), str(ready)],
                             cwd=APP_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not ready.is_file() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.is_file(), owner.communicate(timeout=1) if owner.poll() is not None else 'holder did not acquire'
        denied = attempt(root)
        assert denied.returncode == 7 and denied.stdout.strip() == 'busy', denied.stderr
        owner.kill()
        owner.wait(timeout=5)
        allowed = attempt(root)
        assert allowed.returncode == 0 and allowed.stdout.strip() == 'acquired', allowed.stderr
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    monkeypatch.setenv('DUE_DATA_DIR', str(tmp_path / 'isolated-import'))
    return importlib.import_module('due_app.main').create_app


def lifecycle_app(factory, root):
    app = factory(data_root=root, capture_lease_probe=lambda: None)
    app.state.recordings.recover_interrupted = AsyncMock()
    app.state.recordings.shutdown = AsyncMock()
    return app


def test_second_lifespan_rejected_before_recovery_and_pid_write(app_factory, tmp_path):
    root = tmp_path / 'same-root'
    first = lifecycle_app(app_factory, root)
    second = lifecycle_app(app_factory, root)
    with TestClient(first):
        first_pid = json.loads((root / 'server.json').read_text(encoding='utf-8'))
        with pytest.raises(InstanceBusy):
            with TestClient(second):
                pytest.fail('second service unexpectedly started')
        assert second.state.recordings.recover_interrupted.await_count == 0
        assert second.state.recordings.shutdown.await_count == 0
        assert json.loads((root / 'server.json').read_text(encoding='utf-8')) == first_pid
    assert not (root / 'server.json').exists()
    with TestClient(second):
        assert second.state.recordings.recover_interrupted.await_count == 1
    assert first.state.recordings.shutdown.await_count == 1


@pytest.mark.parametrize('failure', ['recovery', 'shutdown', 'body'])
def test_lifespan_exception_still_releases_lock(app_factory, tmp_path, failure):
    root = tmp_path / ('failed-' + failure)
    broken = lifecycle_app(app_factory, root)
    if failure == 'recovery':
        broken.state.recordings.recover_interrupted.side_effect = ValueError('synthetic recovery failure')
    elif failure == 'shutdown':
        broken.state.recordings.shutdown.side_effect = ValueError('synthetic shutdown failure')
    with pytest.raises(ValueError):
        with TestClient(broken):
            if failure == 'body':
                raise ValueError('synthetic context failure')
    assert not (root / 'server.json').exists()
    with TestClient(lifecycle_app(app_factory, root)) as reopened:
        assert reopened.get('/api/due-diligence/health').status_code == 200


def test_lifespans_on_different_roots_can_run_together(app_factory, tmp_path):
    first = lifecycle_app(app_factory, tmp_path / 'one')
    second = lifecycle_app(app_factory, tmp_path / 'two')
    with TestClient(first) as one, TestClient(second) as two:
        assert one.get('/api/due-diligence/health').status_code == 200
        assert two.get('/api/due-diligence/health').status_code == 200
        assert first.state.recordings.recover_interrupted.await_count == 1
        assert second.state.recordings.recover_interrupted.await_count == 1
