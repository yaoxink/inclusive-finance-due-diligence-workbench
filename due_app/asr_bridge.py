"""Bounded stable-ASR source ingestion; no model, microphone, or fact writes."""
import hashlib
import json
import sqlite3

from fastapi import HTTPException

from .config import APP_ID
from .diligence import DiligenceError


def current_stable_rows(connection, case_id, limit=4):
    """Committed current stable segments, newest first, tamper-checked.

    Implemented in Python instead of one big SQL join: SQLite re-materialises
    json_each per outer row, which turned a long-lived case into a minute-long
    query holding the store lock. The checks are identical — each snapshot
    segment must be stable, within saved frames, match the current revision
    row verbatim (text, revision, source_run_id), and the recording must be
    free of data errors."""
    import json as _json
    rows = connection.execute('SELECT id,interview_id,snapshot_json,created_at FROM recordings '
                              'WHERE case_id=? AND app_id=? ORDER BY created_at DESC LIMIT 40',
                              (case_id, APP_ID)).fetchall()
    results = []
    for recording_id, interview_id, snapshot_json, created_at in rows:
        snapshot = _json.loads(snapshot_json)
        if str(snapshot.get('data_error') or ''):
            continue
        saved_frames = snapshot.get('saved_frames') or 0
        for segment in snapshot.get('segments', []):
            if not segment.get('stable'):
                continue
            current = connection.execute(
                'SELECT revision,text,source_run_id,start_frame,end_frame FROM transcript_revisions '
                'WHERE recording_id=? AND segment_id=? ORDER BY revision DESC LIMIT 1',
                (recording_id, segment.get('id'))).fetchone()
            if current is None:
                continue
            revision, text, source_run_id, start, end = current
            if start is None or end is None or not (0 <= start < end <= saved_frames):
                continue
            # value comparison, mirroring SQL IS semantics (both None passes)
            if (revision != segment.get('revision') or text != segment.get('text')
                    or segment.get('source_run_id') != source_run_id):
                continue
            results.append({'recording_id': recording_id, 'interview_id': interview_id,
                            'segment_id': segment['id'], 'segment_revision': revision,
                            'text': text, 'start_frame': start, 'end_frame': end,
                            'source_run_id': source_run_id, '_created_at': created_at})
    results.sort(key=lambda row: (row.pop('_created_at') or '', row['end_frame'], row['segment_id']),
                 reverse=True)
    return results[:limit]


class StableAsrBridge:
    window_limit = 4

    def __init__(self, diligence):
        self.diligence, self.store = diligence, diligence.store
        self._refreshed = {}

    def sync(self, case_id):
        with self.store.lock, self.store.connect() as connection:
            self.diligence._access(connection, case_id)
            # Refresh capture/ASR work plus recent paused segments, never every
            # archive in a long-lived case. Closed unchanged rows need no refresh.
            recordings = [dict(r) for r in connection.execute('''SELECT id,interview_id,snapshot_json
                FROM recordings WHERE case_id=? AND app_id=? ORDER BY
                CASE WHEN json_extract(snapshot_json,'$.state') IN ('starting','recording','stopping')
                OR json_extract(snapshot_json,'$.asr.state') IN ('loading','running','draining')
                THEN 0 ELSE 1 END,created_at DESC,id DESC LIMIT 4''',(case_id,APP_ID))]
        issues = []
        for row in recordings:
            raw = json.loads(row['snapshot_json'])
            active = (raw.get('state') in {'starting','recording','stopping'}
                      or raw.get('asr',{}).get('state') in {'loading','running','draining'})
            signature = hashlib.sha256(row['snapshot_json'].encode()).hexdigest()
            identity = (case_id,row['id'])
            if active or self._refreshed.get(identity) != signature:
                try:
                    self.diligence.recordings.get(case_id,row['interview_id'],row['id'])
                    with self.store.lock, self.store.connect() as connection:
                        self.diligence._access(connection,case_id)
                        current=connection.execute('SELECT snapshot_json FROM recordings WHERE id=? AND case_id=? AND app_id=?',
                                                   (row['id'],case_id,APP_ID)).fetchone()
                    if current: self._refreshed[identity]=hashlib.sha256(current[0].encode()).hexdigest()
                except DiligenceError:
                    raise
                except HTTPException as exc:
                    if exc.status_code == 403:
                        raise DiligenceError(403,'previsit_access_denied','当前资料权限拒绝，未读取转写。') from exc
                    if exc.status_code not in {404,409}: raise
                    issues.append({'recording_id':row['id'],'code':'recording_refresh_unavailable'})
                except (OSError,ValueError,sqlite3.DatabaseError):
                    issues.append({'recording_id':row['id'],'code':'recording_refresh_unavailable'})
        with self.store.lock, self.store.connect() as connection:
            self.diligence._access(connection,case_id)
            # Window first, then deduplicate at insertion: polling must not keep
            # backfilling old conversations and flooding the human review list.
            candidates=current_stable_rows(connection,case_id,self.window_limit)
        available = []
        # Insert chronological source order so the downstream latest-two window
        # sees the newest speech, not the oldest item in this limited batch.
        for item in reversed(candidates):
            try:
                request={key:item[key] for key in ('recording_id','interview_id','segment_id','segment_revision')}
                value=self.diligence.automatic_statement_from_segment(case_id,request)
                available.append(value['id'])
            except DiligenceError as exc:
                if exc.status == 403: raise
                issues.append({'recording_id':item['recording_id'],'segment_id':item['segment_id'],'code':exc.code})
            except HTTPException as exc:
                if exc.status_code == 403:
                    raise DiligenceError(403,'previsit_access_denied','当前资料权限拒绝，未读取转写。') from exc
                if exc.status_code not in {404,409}: raise
                issues.append({'recording_id':item['recording_id'],'segment_id':item['segment_id'],'code':'source_unavailable'})
        return {'state':'partial' if issues else 'ready' if candidates else 'empty',
                'window_limit':self.window_limit,'observed_count':len(candidates),
                'statement_ids':available,'issues':issues,
                'message':'最近4段稳定且已写盘的转写自动进入待确认原话；不确认事实。更早段落仍在录音归档，可手工引用。'}
