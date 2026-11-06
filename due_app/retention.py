"""Explicit case erasure and seven-day restricted whole-backup retention.

Only owned synthetic workspaces are supported. No filesystem scan outside them,
no physical-wipe claim, and no implicit deletion of existing acceptance cases.
"""
from __future__ import annotations

from contextlib import closing, ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import uuid
import zipfile

from .config import APP_ID
from .diligence import DiligenceError
from .instance_lock import InstanceLock, InstanceBusy
from .maintenance import IDENTIFIER, MAX_BYTES, MAX_FILES, checked_member, inspect_database, owned_path, sha

POLICY = 'case-delete-backup-seven-days/1'
CASE_TABLES = ('s6_cover_settings','s6_reports','s5v2_runs','s5v2_inputs','s5_decision_reviews','s5_runs',
    's4_events','s4_cards','s4_analysis','s4_settings','s3_actions','s3_facts',
    's3_statements','s3_questionnaire_snapshots','s3_previsit_versions','s3_bindings',
    's2_answers','s2_customer_info','s4x_candidates','s4x_runs',
    'recordings','interviews')
KNOWN_TABLES = set(CASE_TABLES) | {'cases','app_meta','recording_events','transcript_revisions',
    's7_deleted_cases','s7_retained_backups'}
SAFE_ID = re.compile(r'^[A-Za-z0-9_.-]{1,128}$')
ASSET = re.compile(r'^[a-f0-9]{64}\.png$')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':'))


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def fail(code, message, status=409):
    raise DiligenceError(status, code, message)


class RetentionService:
    def __init__(self, store, recordings, clock=None):
        self.store, self.recordings = store, recordings
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with store.lock, store.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS s7_deleted_cases (
                case_id TEXT PRIMARY KEY,deleted_at TEXT NOT NULL,expires_at TEXT NOT NULL,
                policy TEXT NOT NULL,counts_json TEXT NOT NULL,backup_ids_json TEXT NOT NULL,
                job_id TEXT NOT NULL,state TEXT NOT NULL,last_error TEXT);
              CREATE TABLE IF NOT EXISTS s7_retained_backups (
                backup_id TEXT PRIMARY KEY,expires_at TEXT NOT NULL,deleted_case_ids_json TEXT NOT NULL,
                state TEXT NOT NULL,last_error TEXT);''')
        self.jobs = owned_path(store.root,store.root/'retention-jobs')
        self.jobs.mkdir(exist_ok=True)

    def now(self):
        value = self.clock()
        if value.tzinfo is None: raise ValueError('retention clock must be timezone-aware')
        return value.astimezone(timezone.utc)

    def policy(self):
        return {'version':POLICY,'backup_retention_days':7,'active_copy':'immediate_application_erasure',
            'retained_backup_access':'download_and_restore_blocked','cleanup_trigger':'startup_periodic_and_manual_retry',
            'offline_copies':'已下载至其他位置的副本由持有人另行处置；本应用不扫描这些位置。',
            'whole_backup_deadline':'earliest_deleted_case_deadline','physical_wipe':False}

    @staticmethod
    def _tables(c):
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    def _schema(self,c):
        unknown = sorted(self._tables(c)-KNOWN_TABLES)
        if unknown: fail('unknown_case_tables','发现未登记的数据表，无法证明删除覆盖：'+', '.join(unknown))

    def _case(self,c,case_id):
        r=c.execute('SELECT * FROM cases WHERE id=? AND app_id=?',(case_id,APP_ID)).fetchone()
        if r is None: fail('case_not_found','本应用中不存在该案件。',404)
        if r['data_class']!='synthetic': fail('synthetic_only','删除功能仅在本版合成资料中启用。')
        return dict(r)

    def _checked_tree(self,path):
        owned_path(self.store.root,path)
        if not path.exists(): return []
        result=[]
        for p in [path,*sorted(path.rglob('*'))] if path.is_dir() else [path]:
            owned_path(self.store.root,p)
            if p.is_file(): result.append({'path':p.relative_to(self.store.root).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)})
            elif not p.is_dir(): fail('unknown_file_type','资料包含不支持的文件类型。')
        return result

    def _backup_cases(self,path):
        owned_path(self.store.root,path)
        with zipfile.ZipFile(path) as z:
            infos=z.infolist()
            if len(infos)>MAX_FILES or sum(i.file_size for i in infos)>MAX_BYTES or len({i.filename for i in infos})!=len(infos):
                fail('unsafe_backup_inventory','备份文件清单超出范围或含重复条目。')
            if z.getinfo('manifest.json').file_size>20*1024**2: fail('unsafe_backup_inventory','备份清单超出范围。')
            m=json.loads(z.read('manifest.json'))
            if m.get('app_id')!=APP_ID or m.get('schema')!='due-backup/1': fail('unknown_backup','备份不属于本应用。')
            entries=m.get('files')
            if not isinstance(entries,list) or len(entries)!=len(infos)-1: fail('unknown_backup','备份清单不完整。')
            names={e['path'] for e in entries}
            if len(names)!=len(entries) or names!={i.filename for i in infos if i.filename!='manifest.json'}:
                fail('unknown_backup','备份清单与文件不一致。')
            for e in entries:
                checked_member(e['path'])
                i=z.getinfo(e['path'])
                if i.is_dir() or ((i.external_attr>>16)&0o170000)==0o120000 or i.file_size!=e['bytes']:
                    fail('unknown_backup','备份包含不支持的文件类型。')
            db=next(e for e in entries if e['path']=='due.sqlite3')
            if db['bytes']>512*1024**2: fail('backup_database_too_large','备份数据库超出本版安全检查范围。')
            raw=z.read('due.sqlite3')
            if hashlib.sha256(raw).hexdigest()!=db['sha256']: fail('backup_hash_mismatch','备份数据库哈希不一致。')
            if not raw.startswith(b'SQLite format 3\x00'): fail('unknown_backup','备份数据库格式未知。')
            # Inspect in memory: preview must not create a plaintext full-database
            # temporary copy that could survive a crash and escape deletion.
            # A copied WAL-mode header is changed only in this disposable byte
            # buffer so SQLite does not look for a filesystem WAL beside :memory:.
            memory=bytearray(raw);memory[18:20]=b'\x01\x01'
            with closing(sqlite3.connect(':memory:')) as c:
                c.deserialize(memory)
                c.execute('PRAGMA query_only=ON')
                self._schema(c)
                if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or c.execute('PRAGMA foreign_key_check').fetchone():
                    fail('unknown_backup','备份数据库完整性不符合要求。')
                if dict(c.execute('SELECT key,value FROM app_meta')).get('app_id')!=APP_ID:
                    fail('unknown_backup','备份数据库应用身份不一致。')
                if c.execute("SELECT COUNT(*) FROM cases WHERE app_id IS NULL OR data_class IS NULL OR app_id!=? OR data_class!='synthetic'",(APP_ID,)).fetchone()[0]:
                    fail('unknown_backup','备份数据库不只包含本应用合成案件。')
                return sorted(r[0] for r in c.execute('SELECT id FROM cases'))

    def _export_moves(self,c,case_id,blockers):
        tables=self._tables(c)
        refs={}
        if 's6_reports' in tables:
            for r in c.execute('SELECT case_id,payload_json FROM s6_reports'):
                asset=json.loads(r['payload_json']).get('snapshot',{}).get('cover_asset')
                if asset:
                    rel=asset.get('relative_path')
                    if rel!='exports/report-assets/'+str(asset.get('sha256'))+'.png':
                        fail('unknown_report_asset','报告资产引用格式未知。')
                    refs.setdefault(rel,set()).add(r['case_id'])
        exclusive,shared=[],[]
        folder=owned_path(self.store.root,self.store.root/'exports')
        for p in sorted(folder.rglob('*')):
            owned_path(self.store.root,p)
            if p.is_dir():
                if p!=folder/'report-assets': blockers.append({'code':'unknown_export_directory','path':p.relative_to(self.store.root).as_posix()})
                continue
            rel=p.relative_to(self.store.root).as_posix()
            if p.parent!=folder/'report-assets' or not ASSET.fullmatch(p.name) or rel not in refs:
                blockers.append({'code':'unknown_export_file','path':rel});continue
            if case_id in refs[rel]:
                (exclusive if refs[rel]=={case_id} else shared).append(rel)
        return exclusive,shared

    def _preview(self,c,case_id):
        case=self._case(c,case_id)
        self._schema(c)
        blockers=[]
        for pending in self.jobs.iterdir():
            owned_path(self.store.root,pending)
            blockers.append({'code':'unresolved_retention_files','path':pending.relative_to(self.store.root).as_posix()})
        tables=self._tables(c)
        counts={t:c.execute('SELECT COUNT(*) FROM '+t+' WHERE case_id=?',(case_id,)).fetchone()[0] for t in CASE_TABLES if t in tables}
        recs=[r[0] for r in c.execute('SELECT id FROM recordings WHERE case_id=?',(case_id,))] if 'recordings' in tables else []
        allrecs={r[0] for r in c.execute('SELECT id FROM recordings')} if 'recordings' in tables else set()
        for table in ('recording_events','transcript_revisions'):
            if table in tables:
                counts[table]=sum(c.execute('SELECT COUNT(*) FROM '+table+' WHERE recording_id=?',(rid,)).fetchone()[0] for rid in recs)
        moves=[]
        recfolder=owned_path(self.store.root,self.store.root/'recordings')
        for p in sorted(recfolder.iterdir()):
            owned_path(self.store.root,p)
            if not p.is_dir() or p.name not in allrecs or not SAFE_ID.fullmatch(p.name):
                blockers.append({'code':'unknown_recording_directory','path':p.relative_to(self.store.root).as_posix()})
            elif p.name in recs: moves.append(p.relative_to(self.store.root).as_posix())
        exclusive,shared=self._export_moves(c,case_id,blockers)
        moves+=exclusive
        backups=[]
        backupfolder=owned_path(self.store.root,self.store.root/'backups')
        if backupfolder.exists():
            for p in sorted(backupfolder.iterdir()):
                owned_path(self.store.root,p)
                if not p.is_file() or p.suffix!='.zip' or not IDENTIFIER.fullmatch(p.stem):
                    blockers.append({'code':'unknown_backup_file','path':p.relative_to(self.store.root).as_posix()});continue
                ids=self._backup_cases(p)
                if case_id in ids: backups.append({'id':p.stem,'sha256':sha(p),'case_ids':ids})
        restores=[]
        base=owned_path(self.store.root,self.store.root/'restores')
        if base.exists():
            for p in sorted(base.iterdir()):
                owned_path(self.store.root,p)
                if not p.is_dir(): blockers.append({'code':'unknown_restore_file','path':p.relative_to(self.store.root).as_posix()});continue
                retired=p/'.retention-retired.json'
                if retired.is_file() and set(x.name for x in p.iterdir())<={'.due-instance.lock','.retention-retired.json'}: continue
                db=p/'due.sqlite3'
                if not db.is_file(): blockers.append({'code':'unknown_restore_directory','path':p.relative_to(self.store.root).as_posix()});continue
                try:
                    with InstanceLock(p):
                        inspect_database(db)
                        with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as rc:
                            self._schema(rc)
                            ids=sorted(r[0] for r in rc.execute('SELECT id FROM cases'))
                        if case_id in ids:
                            entries=[]
                            for child in sorted(p.iterdir()):
                                if child.name=='.due-instance.lock': continue
                                owned_path(self.store.root,child)
                                # Starting an owned restored workspace creates
                                # these service directories even without work.
                                # Only exact, real, empty directories are known;
                                # nested backups/jobs need separate coverage.
                                known_empty_service=(child.name in {'backups','retention-jobs'}
                                    and child.is_dir() and not any(child.iterdir()))
                                if child.name not in {'.due-app-data-root','due.sqlite3','due.sqlite3-wal','due.sqlite3-shm','server.json','recordings','exports','logs'} and not known_empty_service:
                                    blockers.append({'code':'unknown_restore_content','path':child.relative_to(self.store.root).as_posix()})
                                entries.append(child.relative_to(self.store.root).as_posix())
                            restores.append({'relative_path':p.relative_to(self.store.root).as_posix(),'case_ids':ids,'will_remove_entire_copy':True,'entries':entries})
                            moves+=entries
                except InstanceBusy:
                    blockers.append({'code':'restore_in_use','path':p.relative_to(self.store.root).as_posix()})
        inventory=[]
        for rel in moves: inventory+=self._checked_tree(self.store.root/rel)
        # All rows participate so a new shared-asset reference cannot race the preview.
        db_state={t:[dict(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in sorted(tables)}
        content={'case_id':case_id,'counts':counts,'moves':moves,'files':inventory,'backups':backups,'restore_copies':restores,
            'blockers':blockers,'database':fingerprint(db_state),'policy':POLICY}
        return {'case_id':case_id,'case_title':case['title'],'fingerprint':fingerprint(content),'counts':counts,
            'recording_directories':[m for m in moves if m.startswith('recordings/')],
            'exclusive_report_assets':exclusive,'shared_report_assets':shared,'backups':backups,'restore_copies':restores,
            'blockers':blockers,'can_delete':not blockers,'policy':self.policy(),'_moves':moves}

    def preview(self,case_id):
        try:
            with self.store.lock,self.store.connect() as c: result=self._preview(c,case_id)
            return {k:v for k,v in result.items() if k!='_moves'}
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,zipfile.BadZipFile) as exc:
            if isinstance(exc,DiligenceError): raise
            fail('coverage_check_failed','无法完整核对资料覆盖，未执行删除。')

    def _require_guard(self):
        if hasattr(self.recordings,'maintenance_guard') and not getattr(self.recordings,'_maintenance_held',False):
            fail('maintenance_guard_required','删除需进入本机维护保护。')
        if self.recordings.active() or self.recordings.asr_busy_count(): fail('recording_busy','请先停止录音并等待转写完成。')

    def _move(self,source,target):
        source=owned_path(self.store.root,source);target=owned_path(self.store.root,target)
        if source==self.store.root or target==self.store.root: fail('unsafe_delete_path','不能操作数据根目录。')
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists(): fail('delete_target_exists','删除准备目标已存在，未覆盖。')
        os.replace(source,target)

    def _journal(self,folder,value):
        target=owned_path(self.store.root,folder/'journal.json')
        temp=target.with_suffix('.partial')
        temp.write_text(encoded(value),encoding='utf-8');os.replace(temp,target)

    def _validated_journal(self,folder,journal):
        if (journal.get('schema')!='due-delete-job/1' or journal.get('job_id')!=folder.name
                or not re.fullmatch(r'delete_[a-f0-9]{16}',folder.name)
                or not isinstance(journal.get('moves'),list)):
            fail('unknown_delete_job','删除任务身份不一致。')
        for i,entry in enumerate(journal['moves']):
            if set(entry)!={'source','staged'} or entry['staged']!='payload/'+str(i):
                fail('unknown_delete_job','删除任务文件清单未知。')
            path=owned_path(self.store.root,self.store.root/entry['source'])
            parts=path.relative_to(self.store.root).parts
            if not (len(parts)==2 and parts[0]=='recordings' and SAFE_ID.fullmatch(parts[1])
                or len(parts)==3 and parts[:2]==('exports','report-assets') and ASSET.fullmatch(parts[2])
                or len(parts)>=3 and parts[0]=='restores' and re.fullmatch(r'r_[a-f0-9]{12}',parts[1])):
                fail('unknown_delete_job','删除任务路径不属于允许的案件资料。')
        return fingerprint({'moves':journal['moves'],'restore_directories':journal.get('restore_directories',[])})

    def delete(self,case_id,expected_fingerprint,confirm_title):
        self._require_guard()
        job='delete_'+uuid.uuid4().hex[:16]
        folder=owned_path(self.store.root,self.jobs/job)
        journal=None; committed=False;postcommit_error=None
        with self.store.lock,ExitStack() as locks:
            try:
                with self.store.connect() as c:
                    c.execute('PRAGMA secure_delete=ON');c.execute('BEGIN IMMEDIATE')
                    before=self._preview(c,case_id)
                    if confirm_title!=before['case_title']: fail('case_confirmation_mismatch','输入的名称与本案不一致，未删除。',422)
                    if expected_fingerprint!=before['fingerprint']: fail('coverage_changed','覆盖预览已变化，请重新检查后确认。')
                    if before['blockers']: fail('coverage_blocked','覆盖预览有阻断项，未执行删除。')
                    for copy in before['restore_copies']:
                        try: locks.enter_context(InstanceLock(owned_path(self.store.root,self.store.root/copy['relative_path'])))
                        except InstanceBusy: fail('restore_in_use','恢复副本正在使用，未执行删除。')
                        # Recheck the newly admitted empty service folders after
                        # taking the copy's final lease, before moving anything.
                        for rel in copy['entries']:
                            path=owned_path(self.store.root,self.store.root/rel)
                            if path.name in {'backups','retention-jobs'} and (not path.is_dir() or any(path.iterdir())):
                                fail('coverage_changed','恢复副本的维护目录已新增内容，请重新检查覆盖范围。')
                    stamp=self.now();expiry=stamp+timedelta(days=7)
                    journal={'schema':'due-delete-job/1','job_id':job,'case_id':case_id,'state':'preparing',
                        'moves':[{'source':rel,'staged':'payload/'+str(i)} for i,rel in enumerate(before['_moves'])],
                        'restore_directories':[r['relative_path'] for r in before['restore_copies']]}
                    journal_hash=self._validated_journal(folder,journal)
                    folder.mkdir();self._journal(folder,journal)
                    moved=[]
                    try:
                        for entry in journal['moves']:
                            self._move(self.store.root/entry['source'],folder/entry['staged']);moved.append(entry)
                        for table in ('recording_events','transcript_revisions'):
                            if table in self._tables(c): c.execute('DELETE FROM '+table+' WHERE recording_id IN (SELECT id FROM recordings WHERE case_id=?)',(case_id,))
                        for table in CASE_TABLES:
                            if table in self._tables(c): c.execute('DELETE FROM '+table+' WHERE case_id=?',(case_id,))
                        c.execute('DELETE FROM cases WHERE id=? AND app_id=?',(case_id,APP_ID))
                        ids=[b['id'] for b in before['backups']]
                        c.execute('INSERT INTO s7_deleted_cases VALUES (?,?,?,?,?,?,?,?,?)',
                            (case_id,stamp.isoformat(),expiry.isoformat(),POLICY,
                             encoded({**before['counts'],'_journal_fingerprint':journal_hash}),encoded(ids),job,'cleanup_pending',None))
                        for bid in ids:
                            old=c.execute('SELECT * FROM s7_retained_backups WHERE backup_id=?',(bid,)).fetchone()
                            cases=sorted(set((json.loads(old['deleted_case_ids_json']) if old else [])+[case_id]))
                            until=min(expiry.isoformat(),old['expires_at']) if old else expiry.isoformat()
                            c.execute('INSERT OR REPLACE INTO s7_retained_backups VALUES (?,?,?,?,?)',(bid,until,encoded(cases),'restricted',None))
                        journal['state']='prepared';self._journal(folder,journal)
                        # Establish the commit boundary before later filesystem
                        # bookkeeping can fail. A committed deletion is never
                        # described as rolled back to the caller.
                        c.commit();committed=True
                        publish=getattr(self.recordings,'mark_case_deleted',None)
                        if publish is not None: publish(case_id)
                    except Exception:
                        if not committed:
                            for entry in reversed(moved): self._move(folder/entry['staged'],self.store.root/entry['source'])
                            journal['state']='rolled_back';self._journal(folder,journal)
                        raise
                journal['state']='committed';self._journal(folder,journal)
            except Exception as exc:
                if committed:
                    postcommit_error=exc
                elif journal:
                    # A failed SQLite commit also needs file rollback.
                    for entry in reversed(journal['moves']):
                        staged=folder/entry['staged']
                        if staged.exists(): self._move(staged,self.store.root/entry['source'])
                    journal['state']='rolled_back';self._journal(folder,journal)
                if not committed:
                    if isinstance(exc,DiligenceError): raise
                    fail('delete_failed','删除未提交；文件准备步骤未完成，请检查待恢复任务后重试。')
        # No store.lock is held when taking the manager's state lock. Publishing
        # the ID at commit already prevents older refreshes from refilling cache.
        try:
            forget=getattr(self.recordings,'forget_deleted_recordings',None)
            if forget is not None: forget(case_id)
        except Exception as exc:
            postcommit_error=postcommit_error or exc
        if postcommit_error:
            return self._postcommit_pending(case_id,before,stamp,expiry,ids,postcommit_error)
        try:
            cleanup=self.cleanup()
            with self.store.lock,self.store.connect() as c:
                row=dict(c.execute('SELECT * FROM s7_deleted_cases WHERE case_id=?',(case_id,)).fetchone())
        except Exception as exc:
            return self._postcommit_pending(case_id,before,stamp,expiry,ids,exc)
        return {'case_id':case_id,'state':'deleted' if row['state']=='completed' else 'cleanup_pending',
            'deleted_at':row['deleted_at'],'backup_expires_at':row['expires_at'],'counts':before['counts'],
            'restricted_backups':ids,'policy':self.policy(),'cleanup':cleanup}

    def _postcommit_pending(self,case_id,before,stamp,expiry,ids,exc):
        # The audit was committed with the data erasure. Its existing pending
        # state is sufficient for recovery even if this additional error write
        # also fails; the HTTP response must still clear the removed case view.
        try:
            with self.store.lock,self.store.connect() as c:
                c.execute("UPDATE s7_deleted_cases SET last_error=? WHERE case_id=? AND state!='completed'",
                    (type(exc).__name__,case_id))
        except Exception:
            pass
        return {'case_id':case_id,'state':'cleanup_pending','active_case_closed':True,
            'deleted_at':stamp.isoformat(),'backup_expires_at':expiry.isoformat(),'counts':before['counts'],
            'restricted_backups':ids,'policy':self.policy(),
            'cleanup':{'jobs_completed':0,'backups_purged':0,
                'errors':[{'kind':'postcommit','id':case_id,'error':type(exc).__name__}]}}

    def backup_metadata(self,identifier):
        with self.store.lock,self.store.connect() as c:
            row=c.execute('SELECT * FROM s7_retained_backups WHERE backup_id=?',(identifier,)).fetchone()
        if not row: return {'restricted':False,'status':'available','expires_at':None}
        expired=datetime.fromisoformat(row['expires_at'])<=self.now()
        return {'restricted':True,'status':'expired_cleanup_pending' if expired and row['state']!='purged' else row['state'],
            'expires_at':row['expires_at'],'last_error':row['last_error']}

    def assert_backup_access(self,identifier):
        meta=self.backup_metadata(identifier)
        if meta['restricted']: fail('backup_retained_restricted','该备份含已删除案件；保留期内禁止下载和恢复，到期清理。',403)

    def _remove(self,path):
        path=owned_path(self.store.root,path)
        if path==self.store.root: fail('unsafe_delete_path','不能清理数据根目录。')
        self._checked_tree(path)
        if path.is_dir(): shutil.rmtree(path)
        elif path.exists(): path.unlink()

    def cleanup(self):
        """Idempotent startup/periodic/manual trigger; expired access stays denied on failure."""
        result={'jobs_completed':0,'backups_purged':0,'errors':[]}
        with self.store.lock,self.store.connect() as c:
            for folder in sorted(self.jobs.glob('delete_*')):
                try:
                    owned_path(self.store.root,folder)
                    if not folder.is_dir(): fail('unknown_delete_job','删除任务格式未知。')
                    journal=json.loads((folder/'journal.json').read_text(encoding='utf-8'))
                    journal_hash=self._validated_journal(folder,journal)
                    row=c.execute('SELECT * FROM s7_deleted_cases WHERE job_id=?',(folder.name,)).fetchone()
                    if not row:
                        # An interrupted uncommitted transaction must restore its files.
                        for entry in reversed(journal['moves']):
                            staged=owned_path(self.store.root,folder/entry['staged'])
                            original=owned_path(self.store.root,self.store.root/entry['source'])
                            if staged.exists(): self._move(staged,original)
                        self._remove(folder);continue
                    if (row['case_id']!=journal.get('case_id')
                            or json.loads(row['counts_json']).get('_journal_fingerprint')!=journal_hash):
                        fail('delete_journal_changed','删除任务清单与已提交记录不同，停止清理。')
                    payload=owned_path(self.store.root,folder/'payload')
                    if payload.exists(): self._remove(payload)
                    for rel in journal.get('restore_directories',[]):
                        target=owned_path(self.store.root,self.store.root/rel)
                        if target.exists():
                            if set(p.name for p in target.iterdir())- {'.due-instance.lock','.retention-retired.json'}:
                                fail('restore_cleanup_changed','恢复目录已新增内容，停止清理。')
                            (target/'.retention-retired.json').write_text(encoded({'policy':POLICY,'job_id':folder.name}),encoding='utf-8')
                    checkpoint=c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                    if checkpoint and checkpoint[0]: fail('database_checkpoint_busy','活动库日志尚未完成截断，请稍后重试。')
                    c.execute("UPDATE s7_deleted_cases SET state='completed',last_error=NULL WHERE job_id=?",(folder.name,))
                    c.commit();self._remove(folder);result['jobs_completed']+=1
                except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
                    c.execute("UPDATE s7_deleted_cases SET state='cleanup_pending',last_error=? WHERE job_id=?",(type(exc).__name__,folder.name))
                    c.commit();result['errors'].append({'kind':'delete_job','id':folder.name,'error':type(exc).__name__})
            rows=list(c.execute("SELECT * FROM s7_retained_backups WHERE state!='purged'"))
            for row in rows:
                if datetime.fromisoformat(row['expires_at'])>self.now(): continue
                try:
                    bid=row['backup_id']
                    if not IDENTIFIER.fullmatch(bid): fail('unsafe_backup_identifier','备份标识无效。')
                    path=owned_path(self.store.root,self.store.root/'backups'/(bid+'.zip'))
                    self._remove(path)
                    c.execute("UPDATE s7_retained_backups SET state='purged',last_error=NULL WHERE backup_id=?",(bid,))
                    c.commit();result['backups_purged']+=1
                except (OSError,ValueError,sqlite3.Error) as exc:
                    c.execute("UPDATE s7_retained_backups SET state='cleanup_failed',last_error=? WHERE backup_id=?",(type(exc).__name__,row['backup_id']))
                    c.commit();result['errors'].append({'kind':'backup','id':row['backup_id'],'error':type(exc).__name__})
        return result

    def status(self):
        with self.store.lock,self.store.connect() as c:
            deleted=[{'case_id':r['case_id'],'deleted_at':r['deleted_at'],'expires_at':r['expires_at'],'state':r['state'],'last_error':r['last_error']}
                for r in c.execute('SELECT * FROM s7_deleted_cases ORDER BY deleted_at DESC')]
        return {'policy':self.policy(),'deletions':deleted}
