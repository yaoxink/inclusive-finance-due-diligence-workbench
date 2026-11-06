"""S1 application-owned case/interview storage. No recording or credit decisions."""
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import APP_ID


def now():
    return datetime.now(timezone.utc).isoformat()


class DueStore:
    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / '.due-app-data-root').write_text(APP_ID, encoding='utf-8')
        for folder in ('recordings', 'exports', 'logs'):
            (root / folder).mkdir(exist_ok=True)
        self.path = root / 'due.sqlite3'
        self.lock = threading.RLock()
        with self.connect() as connection:
            connection.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cases (
                  id TEXT PRIMARY KEY, app_id TEXT NOT NULL CHECK(app_id='due-diligence'),
                  title TEXT NOT NULL,subject_type TEXT NOT NULL,product_id TEXT NOT NULL,
                  status TEXT NOT NULL,data_class TEXT NOT NULL CHECK(data_class='synthetic'),
                  questionnaire_version TEXT NOT NULL, model_version TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS interviews (
                  id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),
                  app_id TEXT NOT NULL CHECK(app_id='due-diligence'),title TEXT NOT NULL,
                  state TEXT NOT NULL,capture_state TEXT NOT NULL,asr_state TEXT NOT NULL,
                  created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS interview_case ON interviews(case_id,created_at);
            ''')
            meta = dict(connection.execute('SELECT key,value FROM app_meta').fetchall())
            if meta and meta.get('app_id') != APP_ID:
                raise ValueError('数据库不属于尽调应用。')
            connection.execute('INSERT OR IGNORE INTO app_meta VALUES (?,?)', ('app_id', APP_ID))
            connection.execute('INSERT OR IGNORE INTO app_meta VALUES (?,?)', ('schema_version', '1'))

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def public(row):
        if row is None:
            return None
        return {key: value for key, value in dict(row).items() if key != 'app_id'}

    def cases(self):
        with self.lock, self.connect() as connection:
            return [self.public(row) for row in connection.execute(
                'SELECT * FROM cases WHERE app_id=? ORDER BY updated_at DESC,id', (APP_ID,))]

    def case(self, case_id, include_interviews=True):
        with self.lock, self.connect() as connection:
            result = self.public(connection.execute('SELECT * FROM cases WHERE id=? AND app_id=?', (case_id, APP_ID)).fetchone())
            if result is not None and include_interviews:
                result['interviews'] = [self.public(row) for row in connection.execute(
                    'SELECT * FROM interviews WHERE case_id=? AND app_id=? ORDER BY created_at,id', (case_id, APP_ID))]
            return result

    def create_case(self, title, subject_type, product_id):
        case_id, stamp = 'case_' + uuid.uuid4().hex, now()
        with self.lock, self.connect() as connection:
            connection.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (case_id, APP_ID, title, subject_type, product_id, 'preparing', 'synthetic',
                 'farmer_candidate_v0.1', None, stamp, stamp))
        return self.case(case_id)

    def create_interview(self, case_id, title):
        with self.lock:
            if self.case(case_id) is None:
                return None
            interview_id, stamp = 'interview_' + uuid.uuid4().hex, now()
            with self.connect() as connection:
                connection.execute('INSERT INTO interviews VALUES (?,?,?,?,?,?,?,?)',
                    (interview_id, case_id, APP_ID, title or '现场访谈', 'ready', 'not_implemented', 'not_implemented', stamp))
                connection.execute('UPDATE cases SET updated_at=? WHERE id=? AND app_id=?', (stamp, case_id, APP_ID))
            return self.interview(case_id, interview_id)

    def interview(self, case_id, interview_id):
        with self.lock, self.connect() as connection:
            if not self.case(case_id, include_interviews=False):
                return None
            return self.public(connection.execute('SELECT * FROM interviews WHERE id=? AND case_id=? AND app_id=?',
                                                 (interview_id, case_id, APP_ID)).fetchone())
