"""Owned local backup and isolated recovery. Never changes device security settings."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile

from .config import APP_ID
from .archive_paths import register_restored_archive_roots
from .diligence import DiligenceError

IDENTIFIER = re.compile(r'^backup_[0-9]{8}T[0-9]{6}_[a-f0-9]{12}$')
MAX_BYTES = 4 * 1024 ** 3
MAX_FILES = 100000


def sha(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def owned_path(root, path):
    """Reject existing symlink/junction parents, including Windows reparse points."""
    root, path = Path(root).absolute(), Path(path).absolute()
    if not path.is_relative_to(root):
        raise DiligenceError(409, 'unsafe_data_path', '资料路径不属于本应用。')
    for item in [path, *path.parents]:
        if item == root.parent:
            break
        if item.exists() or item.is_symlink():
            if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
                raise DiligenceError(409, 'unsafe_data_path', '资料目录包含链接，不能用于备份或恢复。')
    if not path.resolve().is_relative_to(root.resolve()):
        raise DiligenceError(409, 'unsafe_data_path', '资料路径越过本应用目录。')
    return path


def checked_member(name):
    item = PurePosixPath(name)
    if not name or '\\' in name or item.is_absolute() or any(p in ('..', '.', '') for p in item.parts) or ':' in name:
        raise DiligenceError(422, 'invalid_backup', '备份内包含不允许的文件路径。')
    if name not in ('due.sqlite3', '.due-app-data-root') and item.parts[0] not in ('recordings', 'exports'):
        raise DiligenceError(422, 'invalid_backup', '备份内包含本应用不接收的内容。')
    return item


def inspect_database(path):
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
            if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('integrity')
            if dict(connection.execute('SELECT key,value FROM app_meta'))['app_id'] != APP_ID:
                raise ValueError('identity')
            if connection.execute("SELECT COUNT(*) FROM cases WHERE app_id IS NULL OR data_class IS NULL OR app_id!=? OR data_class!='synthetic'", (APP_ID,)).fetchone()[0]:
                raise ValueError('data_class')
            if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('foreign_keys')
            retention = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='s7_deleted_cases'").fetchone()
            if retention and connection.execute("SELECT 1 FROM s7_deleted_cases WHERE state!='completed' LIMIT 1").fetchone():
                raise DiligenceError(409, 'backup_retention_pending', '资料存在未完成的删除任务；请在原工作空间清理完成后重新备份，不能恢复缺少任务文件的旧包。')
            counts = {name: connection.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]
                      for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
                      if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name)}
            return counts
    except DiligenceError:
        raise
    except (sqlite3.Error, KeyError, ValueError) as exc:
        raise DiligenceError(422, 'invalid_backup_database', '备份数据库身份、完整性或资料用途不符合当前应用。') from exc


def restore_archive(archive_path, destination, allowed_parent):
    """Restore into an empty new child only. Does not start or replace an application."""
    destination, allowed_parent = Path(destination).resolve(), Path(allowed_parent).resolve()
    if destination == allowed_parent or not destination.is_relative_to(allowed_parent):
        raise DiligenceError(422, 'invalid_restore_target', '恢复位置必须是指定目录内的新子目录。')
    if destination.exists() and any(destination.iterdir()):
        raise DiligenceError(409, 'restore_target_not_empty', '恢复目录已有内容，未覆盖任何文件。')
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_BYTES:
                raise DiligenceError(422, 'backup_too_large', '备份超过本版恢复范围。')
            if len({i.filename for i in infos}) != len(infos):
                raise DiligenceError(422, 'invalid_backup', '备份含重复文件。')
            if archive.getinfo('manifest.json').file_size > 20 * 1024 * 1024:
                raise DiligenceError(422, 'invalid_backup', '备份清单超出限制。')
            manifest = json.loads(archive.read('manifest.json'))
            if manifest.get('app_id') != APP_ID or manifest.get('schema') != 'due-backup/1':
                raise DiligenceError(422, 'invalid_backup', '不是受支持的尽调备份。')
            entries = manifest.get('files')
            if not isinstance(entries, list) or len(entries) != len(infos) - 1:
                raise DiligenceError(422, 'invalid_backup', '备份清单与实际内容不一致。')
            names = {e['path'] for e in entries}
            if len(names) != len(entries) or names != {i.filename for i in infos if i.filename != 'manifest.json'}:
                raise DiligenceError(422, 'invalid_backup', '备份清单与实际文件不一致。')
            for entry in entries:
                checked_member(entry['path'])
                info = archive.getinfo(entry['path'])
                if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000 or info.file_size != entry['bytes']:
                    raise DiligenceError(422, 'invalid_backup', '备份文件类型或长度不符合清单。')
            destination.mkdir(parents=True, exist_ok=True)
            for entry in entries:
                target = destination.joinpath(*PurePosixPath(entry['path']).parts)
                if not target.resolve().is_relative_to(destination):
                    raise DiligenceError(422, 'invalid_backup', '恢复路径越界。')
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry['path']) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                if sha(target) != entry['sha256']:
                    raise DiligenceError(422, 'backup_hash_mismatch', '备份文件校验失败；没有切换当前工作空间。')
            marker = destination / '.due-app-data-root'
            if marker.read_text(encoding='utf-8').strip() != APP_ID:
                raise DiligenceError(422, 'invalid_backup', '恢复内容的应用身份不一致。')
            counts = inspect_database(destination / 'due.sqlite3')
            if counts != manifest['table_counts']:
                raise DiligenceError(422, 'backup_count_mismatch', '恢复后的对象数量与备份不一致。')
            database = destination / 'due.sqlite3'
            source_hash = sha(database)
            with closing(sqlite3.connect(database)) as connection:
                relocation = register_restored_archive_roots(connection, destination)
                connection.commit()
            restored_counts = inspect_database(database)
            return {'state': 'verified', 'backup_id': manifest['backup_id'], 'files_verified': len(entries),
                    'table_counts': counts, 'restored_table_counts': restored_counts,
                    'database_migration': {'kind': 'archive_location_metadata', 'performed': relocation is not None,
                        'source_sha256': source_hash, 'restored_sha256': sha(database),
                        'source_contexts_rewritten': False},
                    'restored_directory': str(destination),
                    'active_workspace_changed': False, 'encryption': 'not_established_by_restore'}
    except DiligenceError:
        raise
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        raise DiligenceError(422, 'invalid_backup', '备份无法读取或校验；当前工作空间未改变。') from exc


class MaintenanceService:
    def __init__(self, store, recordings):
        self.store, self.recordings = store, recordings
        self.folder = owned_path(store.root, store.root / 'backups')
        self.folder.mkdir(exist_ok=True)

    def _idle(self):
        # Read capture state before taking store.lock (capture lock order).
        if self.recordings.active() or self.recordings.asr_busy_count():
            raise DiligenceError(409, 'recording_busy', '请先停止录音并等待转写完成，再创建备份。')

    def _access(self):
        owned_path(self.store.root, self.folder)
        owned_path(self.store.root, self.store.root / '.due-app-data-root')
        with self.store.lock, self.store.connect() as connection:
            table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='s3_bindings'").fetchone()
            if table and connection.execute("SELECT 1 FROM s3_bindings WHERE access_state='denied' LIMIT 1").fetchone():
                raise DiligenceError(403, 'backup_access_denied', '有案件的资料权限已被拒绝，不能通过整库备份读取或恢复正文。请先核对资料权限。')

    def _path(self, identifier):
        self._access()
        if not IDENTIFIER.fullmatch(identifier):
            raise DiligenceError(404, 'backup_not_found', '没有找到该备份。')
        if self._retention_info(identifier)['restricted']:
            raise DiligenceError(403, 'backup_retained_restricted', '该备份含已删除案件；保留期内禁止下载和恢复，到期清理。')
        path = self.folder / (identifier + '.zip')
        if not path.is_file() or path.is_symlink():
            raise DiligenceError(404, 'backup_not_found', '没有找到该备份。')
        return path

    def _retention_info(self, identifier):
        service = getattr(self, 'retention', None)
        if service is not None:
            return service.backup_metadata(identifier)
        # Keep restrictions effective even for an internal service constructed
        # before the retention router was installed.
        with self.store.lock, self.store.connect() as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='s7_retained_backups'").fetchone()
            row = connection.execute('SELECT * FROM s7_retained_backups WHERE backup_id=?', (identifier,)).fetchone() if exists else None
        return {'restricted': bool(row), 'status': row['state'] if row else 'available',
                'expires_at': row['expires_at'] if row else None}

    def list(self):
        owned_path(self.store.root, self.folder)
        result = []
        for path in sorted(self.folder.glob('backup_*.zip'), reverse=True):
            if path.is_symlink() or not IDENTIFIER.fullmatch(path.stem):
                continue
            try:
                with zipfile.ZipFile(path) as archive:
                    if archive.getinfo('manifest.json').file_size > 20 * 1024 * 1024:
                        continue
                    meta = json.loads(archive.read('manifest.json'))
                    if meta.get('app_id') == APP_ID:
                        result.append({'id': path.stem, 'created_at': meta['created_at'], 'bytes': path.stat().st_size,
                                       'file_count': len(meta['files']), 'data_class': 'synthetic',
                                       **self._retention_info(path.stem)})
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                continue
        return {'backups': result, 'policy': {'data_class': 'synthetic', 'encrypted': False,
                'restore_mode': 'isolated_new_directory', 'logs_included': False,
                'retention': 'deleted_case_backups_restricted_for_seven_days_then_cleanup',
                'retention_days_after_case_deletion': 7, 'offline_copies': '用户另行保管的副本不受本应用控制'}}

    def create(self):
        if hasattr(self.recordings, 'maintenance_guard') and not getattr(self.recordings, '_maintenance_held', False):
            raise DiligenceError(409, 'maintenance_guard_required', '创建备份需要先进入本机维护保护。')
        self._idle()
        stamp = datetime.now(timezone.utc)
        identifier = 'backup_' + stamp.strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:12]
        work = Path(tempfile.mkdtemp(prefix='staging-', dir=self.folder)).resolve()
        assert work.is_relative_to(self.folder.resolve()) and work != self.folder.resolve()
        final = self.folder / (identifier + '.zip')
        pending = self.folder / (identifier + '.partial')
        try:
            with self.store.lock:
                self._access()
                jobs = owned_path(self.store.root, self.store.root / 'retention-jobs')
                if jobs.exists() and any(jobs.iterdir()):
                    raise DiligenceError(409, 'backup_retention_pending', '仍有删除准备或清理文件；请先完成清理重试，再创建备份。')
                with self.store.connect() as check:
                    exists = check.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='s7_deleted_cases'").fetchone()
                    if exists and check.execute("SELECT 1 FROM s7_deleted_cases WHERE state!='completed' LIMIT 1").fetchone():
                        raise DiligenceError(409, 'backup_retention_pending', '仍有未完成的删除任务；请先完成清理，再创建备份。')
                with self.store.connect() as source, closing(sqlite3.connect(work / 'due.sqlite3')) as destination:
                    source.backup(destination)
                counts = inspect_database(work / 'due.sqlite3')
                with closing(sqlite3.connect((work / 'due.sqlite3').as_uri() + '?mode=ro', uri=True)) as copied:
                    case_ids = sorted(r[0] for r in copied.execute('SELECT id FROM cases'))
                entries = []
                files = [(work / 'due.sqlite3', 'due.sqlite3'), (self.store.root / '.due-app-data-root', '.due-app-data-root')]
                for folder in ('recordings', 'exports'):
                    owned_path(self.store.root, self.store.root / folder)
                    for path in sorted((self.store.root / folder).rglob('*')):
                        owned_path(self.store.root, path)
                        if path.is_symlink():
                            raise DiligenceError(409, 'backup_unsafe_link', '资料目录包含链接，未创建备份。')
                        if path.is_file():
                            if not path.resolve().is_relative_to(self.store.root.resolve()):
                                raise DiligenceError(409, 'backup_unsafe_link', '资料文件不在本应用目录内。')
                            files.append((path, path.relative_to(self.store.root).as_posix()))
                if len(files) > MAX_FILES - 1 or sum(p.stat().st_size for p, _ in files) > MAX_BYTES:
                    raise DiligenceError(409, 'backup_too_large', '资料超过本版备份大小限制。')
                with zipfile.ZipFile(pending, 'x', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    for path, relative in files:
                        before = sha(path)
                        archive.write(path, relative)
                        if sha(path) != before:
                            raise DiligenceError(409, 'backup_source_changed', '备份期间文件发生变化，请重试。')
                        entries.append({'path': relative, 'bytes': path.stat().st_size, 'sha256': before})
                    archive.writestr('manifest.json', json.dumps({'schema': 'due-backup/1', 'app_id': APP_ID,
                        'backup_id': identifier, 'created_at': stamp.isoformat(), 'files': entries,
                        'table_counts': counts, 'case_ids': case_ids, 'data_class': 'synthetic', 'encrypted': False}, ensure_ascii=False))
                with zipfile.ZipFile(pending) as archive:
                    if archive.testzip() is not None:
                        raise DiligenceError(409, 'backup_integrity_failed', '备份文件完整性校验失败。')
                    for entry in entries:
                        digest = hashlib.sha256()
                        with archive.open(entry['path']) as handle:
                            for block in iter(lambda: handle.read(1024 * 1024), b''):
                                digest.update(block)
                        if digest.hexdigest() != entry['sha256']:
                            raise DiligenceError(409, 'backup_integrity_failed', '备份内容与来源哈希不一致。')
                os.replace(pending, final)
            return {'id': identifier, 'created_at': stamp.isoformat(), 'bytes': final.stat().st_size,
                    'files': len(entries), 'sha256': sha(final), 'encrypted': False, 'data_class': 'synthetic'}
        finally:
            # This is our freshly generated staging child, never a caller-supplied path.
            assert work.is_relative_to(self.folder.resolve()) and work.name.startswith('staging-')
            shutil.rmtree(work)
            if pending.exists():
                pending.unlink()

    def verify(self, identifier):
        path = self._path(identifier)
        # Keep generated paths short enough for Windows hosts with MAX_PATH APIs.
        base = owned_path(self.store.root, self.store.root / 'restores')
        target = base / ('r_' + uuid.uuid4().hex[:12])
        return restore_archive(path, target, base)
