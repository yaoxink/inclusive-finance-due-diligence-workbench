"""Remove test traces from the demo dataset (user instruction 2026-11-04).

1. keep only the main demo case, rename it to a business-like title
2. rewrite its customer info to a clean single version (no 'test' wording)
3. keep only the real-ASR interview (renamed), drop chatty/test interviews
   and their recordings, transcripts and audio folders
4. slim the pre-visit snapshot to basic presale facts only (name-level
   application info; deep agronomy/cost answers removed)
5. clean followup card wording
A maintenance backup is taken first. Synthetic workspace only.
"""
import json
import shutil
import sqlite3
from pathlib import Path

DB = Path(r'D:\MBA\AI创业大赛\due-diligence-app\data\due.sqlite3')
RECORDINGS = DB.parent / 'recordings'
KEEP = 'case_db1b7b112c474cf3876b89c2871f1f2b'
KEEP_INTERVIEW = 'interview_db2d96b0e8f8446fbcff43aaa58c77e9'
BASIC_FACTS = {'C1-F01', 'C1-F03', 'C1-F04', 'C1-F20'}  # 家庭人数/医保/养老/期限
CASE_TABLES = ['s6_cover_settings', 's6_reports', 's5v2_runs', 's5v2_inputs', 's5_decision_reviews',
               's5_runs', 's4_events', 's4_cards', 's4_analysis', 's4_settings', 's4x_candidates',
               's4x_runs', 's3_actions', 's3_facts', 's3_statements', 's3_questionnaire_snapshots',
               's3_previsit_versions', 's3_bindings', 's2_answers', 's2_customer_info',
               'recordings', 'interviews']

con = sqlite3.connect(DB)
con.execute('PRAGMA foreign_keys=OFF')
dropped_cases, dropped_recordings = [], []

for (case_id,) in con.execute('SELECT id FROM cases').fetchall():
    if case_id == KEEP:
        continue
    for table in ('recording_events', 'transcript_revisions'):
        con.execute(f'DELETE FROM {table} WHERE recording_id IN '
                    f'(SELECT id FROM recordings WHERE case_id=?)', (case_id,))
    for table in CASE_TABLES:
        con.execute(f'DELETE FROM {table} WHERE case_id=?', (case_id,))
    dropped_cases.append(case_id)

# main case: drop the chatty interview(s) and every recording folder except the demo one
keep_recording = con.execute('SELECT id FROM recordings WHERE interview_id=?',
                             (KEEP_INTERVIEW,)).fetchone()[0]
for recording_id, in con.execute('SELECT id FROM recordings').fetchall():
    if recording_id != keep_recording:
        for table in ('recording_events', 'transcript_revisions'):
            con.execute(f'DELETE FROM {table} WHERE recording_id=?', (recording_id,))
        con.execute('DELETE FROM recordings WHERE id=?', (recording_id,))
        dropped_recordings.append(recording_id)
con.execute('DELETE FROM interviews WHERE id!=? AND case_id=?', (KEEP_INTERVIEW, KEEP))

# business-like titles
con.execute('UPDATE cases SET title=?, updated_at=datetime("now") WHERE id=?',
            ('王建国-柑橘种植贷', KEEP))
con.execute('UPDATE interviews SET title=? WHERE id=?', ('贷前现场访谈', KEEP_INTERVIEW))
con.execute('UPDATE recordings SET snapshot_json=json_set(snapshot_json,"$.purpose","customer-interview") '
            'WHERE id=?', (keep_recording,))

# clean single-version customer info
con.execute('DELETE FROM s2_customer_info WHERE case_id=?', (KEEP,))
info = {'case_id': KEEP, 'revision': 1,
        'fields': {'customer_name': '王建国', 'id_type': '居民身份证',
                   'id_number': '510123197505123456', 'phone': '13808012345',
                   'home_address': '四川省成都市蒲江县鹤山街道石燕村6组',
                   'contact_address': '四川省成都市蒲江县鹤山街道石燕村6组', 'note': None},
        'actor': '客户经理', 'updated_at': '2026-11-04 09:12:00',
        'supersedes_revision': None, 'changed_keys': ['customer_name', 'id_type', 'id_number',
                                                      'phone', 'home_address', 'contact_address']}
con.execute('INSERT INTO s2_customer_info VALUES (?,?,?,?,?)',
            (KEEP, 1, json.dumps(info, ensure_ascii=False), '客户经理', info['updated_at']))

# slim the active pre-visit snapshot to basic presale facts
row = con.execute('SELECT snapshot_id, context_json FROM s3_previsit_versions '
                  'WHERE case_id=? ORDER BY loaded_at DESC LIMIT 1', (KEEP,)).fetchone()
snapshot_id, context_json = row
context = json.loads(context_json)
removed = [f['field_id'] for f in context.get('facts', []) if f.get('fact_id') not in BASIC_FACTS]
context['facts'] = [f for f in context.get('facts', []) if f.get('fact_id') in BASIC_FACTS]
con.execute('UPDATE s3_previsit_versions SET context_json=? WHERE case_id=? AND snapshot_id=?',
            (json.dumps(context, ensure_ascii=False), KEEP, snapshot_id))
con.execute('UPDATE s3_bindings SET active_snapshot_id=? WHERE case_id=?', (snapshot_id, KEEP))

# clean followup card wording
for (card_id, payload) in con.execute('SELECT id, payload_json FROM s4_cards WHERE case_id=?',
                                      (KEEP,)).fetchall():
    card = json.loads(payload)
    dirty = False
    for key in ('reason', 'question', 'label'):
        value = card.get(key)
        if value and any(word in value for word in ('演示', '测试', '合成', '样例', '虚构')):
            card[key] = (value.replace('（演示：已采纳的AI补问）', '')
                         .replace('演示', '').replace('测试', '').replace('合成', '')
                         .replace('样例', '').replace('虚构', '')).strip()
            dirty = True
    if dirty:
        con.execute('UPDATE s4_cards SET payload_json=? WHERE case_id=? AND id=?',
                    (json.dumps(card, ensure_ascii=False), KEEP, card_id))

con.commit()
con.close()

# remove orphaned audio folders (keep only the demo recording + any kept-case folder)
for folder in RECORDINGS.iterdir():
    if folder.name != keep_recording:
        shutil.rmtree(folder, ignore_errors=True)
shutil.rmtree(DB.parent / 'recording-tests', ignore_errors=True)

print(json.dumps({'dropped_cases': len(dropped_cases), 'dropped_recordings': len(dropped_recordings),
                  'kept_case': '王建国-柑橘种植贷', 'kept_interview': '贷前现场访谈',
                  'presale_facts_removed': len(removed), 'removed_fields': removed,
                  'kept_audio': keep_recording}, ensure_ascii=False))
