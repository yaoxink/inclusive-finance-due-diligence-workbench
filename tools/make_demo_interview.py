"""Seed a realistic synthetic farmer-interview demo into a case.

Generates spoken audio with the local Windows SAPI voice (Huihui, zh-CN,
16k/16bit/mono), writes a REAL recording archive through ArchiveWriter (so the
audio plays back and the manifest verifies), and commits matching stable
transcript segments plus tamper-check revision rows. The result behaves on the
onsite page exactly like a finished recording: playable audio, readable
transcript, extraction candidates, followup bridge.

Usage:
  python tools/make_demo_interview.py [case_id]
Synthetic data only — never real client material.
"""
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / 'shared-recording-core'))

from due_app.store import APP_ID, DueStore, now
from shared_recording_core import SessionConfig
from shared_recording_core.archive import ArchiveWriter

LINES = [
    ('调查员', '王老师你好，我们做贷前尽调，先了解一下家里的基本情况。家里一共几口人？'),
    ('农户', '我们家一共五口人，两个老人跟我们住，娃儿在读初中。'),
    ('调查员', '今年地里种的是什么？面积有多少？'),
    ('农户', '今年种了十二亩柑橘，这个柑橘我已经种了八年了。'),
    ('农户', '亩产大概四千斤，今年收购价两块五一斤。'),
    ('调查员', '一年下来成本开销大概说一下？'),
    ('农户', '买种子花了三千块，农药两千块，肥料五千块，人工请人摘果子另算。'),
    ('调查员', '家里面现在有没有银行贷款？'),
    ('农户', '银行贷款没得，不欠银行的钱，亲戚那边也没有借款。'),
    ('调查员', '农业保险买的有吗？'),
    ('农户', '买了的，农业保险连续买了两年了。'),
    ('调查员', '家里的房产车子情况说一下？'),
    ('农户', '城头没得商品房，就是一个电车，买成五万块钱。'),
    ('调查员', '要得，情况我们记完了，后续有需要再跟你联系。'),
]
GAP_SECONDS = 0.6
SAMPLE_RATE = 16000


def synthesize(lines, work_dir):
    script = work_dir / 'lines.json'
    script.write_text(json.dumps([text for _, text in lines], ensure_ascii=False), encoding='utf-8')
    ps = r"""
param($jsonPath,$outDir)
Add-Type -AssemblyName System.Speech
$lines = Get-Content -LiteralPath $jsonPath -Encoding UTF8 | ConvertFrom-Json
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('Microsoft Huihui Desktop')
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,[System.Speech.AudioFormat.AudioChannel]::Mono)
for ($i=0; $i -lt $lines.Count; $i++) {
  $s.SetOutputToWaveFile((Join-Path $outDir ('c_{0:d2}.wav' -f $i)), $fmt)
  $s.Speak([string]$lines[$i])
}
$s.Dispose()
"""
    ps_path = work_dir / 'tts.ps1'
    ps_path.write_text(ps, encoding='utf-8-sig')
    subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                    '-File', str(ps_path), str(script), str(work_dir)], check=True, timeout=300)
    blobs = []
    for index in range(len(lines)):
        path = work_dir / f'c_{index:02d}.wav'
        with wave.open(str(path)) as reader:
            assert (reader.getframerate(), reader.getsampwidth(), reader.getnchannels()) == (16000, 2, 1)
            blobs.append(reader.readframes(reader.getnframes()))
    return blobs


def main(case_id):
    store = DueStore(ROOT / 'data')
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        blobs = synthesize(LINES, work)
    interview = store.create_interview(case_id, '现场访谈演示·合成语音')
    recording_id = 'rec_demo_' + __import__('uuid').uuid4().hex[:12]
    config = SessionConfig(app_id=APP_ID, instance_id='demo-interview-seed', session_id=recording_id,
                           output_root=(store.root / 'recordings').resolve(), device_id=1,
                           capture_format={'sample_rate': 16000, 'channels': 1, 'sample_width': 2},
                           asr_config={'engine': 'demo-tts'})
    def commit_pcm(blob):
        # ArchiveWriter accepts at most 16000 PCM16 frames per commit
        step = 16000 * 2
        for offset in range(0, len(blob), step):
            writer.commit(blob[offset:offset + step])

    writer = ArchiveWriter(config, 'demo-interview', source='synthetic')
    writer.started()
    gap = b'\x00\x00' * int(SAMPLE_RATE * GAP_SECONDS)
    segments, cursor = [], 0
    for index, ((speaker, text), blob) in enumerate(zip(LINES, blobs)):
        start = cursor
        commit_pcm(blob)
        cursor += len(blob) // 2
        commit_pcm(gap)
        cursor += len(gap) // 2
        segments.append({'id': f'demo_seg_{index+1:02d}', 'ordinal': index + 1, 'revision': 1,
                         'stable': True, 'text': f'{speaker}：{text}',
                         'start_seconds': round(start / SAMPLE_RATE, 2),
                         'end_seconds': round((start + len(blob) // 2) / SAMPLE_RATE, 2),
                         'source_run_id': 'demo-tts'})
    writer.close()
    total_frames = cursor
    stamp = now()
    snapshot = {'id': recording_id, 'case_id': case_id, 'interview_id': interview['id'], 'device_id': 1,
                'purpose': 'demo-interview-synthetic', 'created_at': stamp, 'state': 'saved',
                'capture_state': 'saved', 'saved_frames': total_frames,
                'saved_seconds': round(total_frames / SAMPLE_RATE, 2), 'sample_rate': SAMPLE_RATE,
                'started_at': stamp, 'stopped_at': stamp, 'last_saved_at': stamp,
                'audio_available': True, 'error': None, 'data_error': '',
                'asr': {'state': 'completed', 'error': None, 'message': '合成演示转写。',
                        'transcribed_frames': total_frames,
                        'transcribed_seconds': round(total_frames / SAMPLE_RATE, 2)},
                'segments': segments}
    with store.lock, store.connect() as connection:
        context = {'app_id': APP_ID, 'instance_id': 'demo-interview-seed', 'session_id': recording_id,
                   'output_root': str((store.root / 'recordings').resolve()), 'device_id': 1,
                   'capture_format': {'sample_rate': 16000, 'channels': 1, 'sample_width': 2},
                   'asr_config': {'engine': 'demo-tts'}}
        connection.execute('INSERT INTO recordings VALUES (?,?,?,?,?,?,?,?)',
                           (recording_id, case_id, interview['id'], APP_ID, stamp, stamp,
                            json.dumps(context, ensure_ascii=False),
                            json.dumps(snapshot, ensure_ascii=False)))
        for segment in segments:
            connection.execute('INSERT INTO transcript_revisions VALUES (?,?,?,?,?,?,?,?)',
                               (recording_id, segment['id'], 1, 'demo-tts', segment['text'],
                                int(segment['start_seconds'] * SAMPLE_RATE),
                                int(segment['end_seconds'] * SAMPLE_RATE), stamp))
    print(json.dumps({'case_id': case_id, 'interview_id': interview['id'],
                      'recording_id': recording_id, 'lines': len(LINES),
                      'duration_seconds': round(total_frames / SAMPLE_RATE, 1),
                      'wav': str((store.root / 'recordings' / recording_id / 'recording.wav'))},
                     ensure_ascii=False))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'case_db1b7b112c474cf3876b89c2871f1f2b')
