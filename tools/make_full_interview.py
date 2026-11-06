"""Generate a long synthetic interview covering the whole onsite questionnaire.

TTS every line (Huihui), write a real archive, attach to the case's existing
interview, then trigger transcription. Run extraction separately afterwards.
Synthetic workspace only.
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

CASE = sys.argv[1] if len(sys.argv) > 1 else 'case_c26ef7a4eacc4534be776de80fa12e28'
INTERVIEW = sys.argv[2] if len(sys.argv) > 2 else 'interview_3c90c1f0b62e4f058bf9cc4b134612db'

LINES = [
    ('调查员', '李姐你好，我们做贷前尽调，先从家里情况问起。家里一共几口人？'),
    ('农户', '我们家一共四口人，我、我爱人、我母亲，还有个儿子在读高中。'),
    ('调查员', '家里有没有长期需要照料的病人或老人？'),
    ('农户', '我母亲八十岁了，腿脚不方便，平时要人照看，算一个。'),
    ('调查员', '养老和医保的缴费情况说一下。'),
    ('农户', '养老保险我们两口子都在缴，平均档位一年两千四。医保家里四个人都买了。'),
    ('调查员', '你的户籍和婚姻情况？'),
    ('农户', '我就是邛崃本地户籍，结了婚十几年了。'),
    ('调查员', '种地种了多少年？现在经营的主体是什么类型？'),
    ('农户', '我种水稻种了十二年了，就是自家个体农户，没注册公司。'),
    ('调查员', '除了种地还有没有别的经营收入？'),
    ('农户', '农闲的时候我爱人去镇上打点零工，一年大概八千块钱。'),
    ('调查员', '销售的渠道稳定吗？'),
    ('农户', '粮食是跟粮站签了合同的，每年都收，渠道很稳定。'),
    ('调查员', '家里的地是什么情况？'),
    ('农户', '自己家的承包地有六亩，另外流转了二十四亩，一共有三十亩。'),
    ('调查员', '流转的费用和期限说一下。'),
    ('农户', '流转地一亩一年八百块，签了五年合同，从二零二三年到二零二八年。'),
    ('调查员', '主营是什么？面积和产量？'),
    ('农户', '主营是水稻，三十亩，一亩产两千斤谷子。'),
    ('调查员', '水稻卖多少钱一斤？'),
    ('农户', '今年谷子卖一块五一斤，品种是宜香优。'),
    ('调查员', '除了水稻还有没有种别的？'),
    ('农户', '还种了五亩油菜，一亩打三百斤菜籽，菜籽卖三块钱一斤。'),
    ('调查员', '一年下来的成本开销说一下，先说种子。'),
    ('农户', '种子一年一千八，农药九百，肥料四千二。'),
    ('调查员', '水电、人工和农具这些呢？'),
    ('农户', '水电灌溉八百，请人插秧收割人工六千，农机社会化服务一千二，其他杂项五百。'),
    ('调查员', '农业保险买的什么情况？'),
    ('农户', '水稻买了政策性农业保险，在人民财产保险买的，三十亩全覆盖，保费一亩自己出二十块。'),
    ('调查员', '保险连续买了几年？'),
    ('农户', '连着买了三年了，每年都买。'),
    ('调查员', '家里的房产情况？'),
    ('农户', '城头没得商品房，农村宅基地有个自建房，两层，一百二十个平方，二零一五年修的。'),
    ('调查员', '车子、农机这些有没有？'),
    ('农户', '有个三轮车运谷子，买成两万块。还有台小型拖拉机，三万五。存款还有八万块钱。'),
    ('调查员', '现在有没有欠银行的钱？'),
    ('农户', '有兩笔。一笔在农商行借了五万，还剩三万没还；另一笔在农业银行借了八万，还剩六万一。'),
    ('调查员', '亲戚朋友那里有没有借款？'),
    ('农户', '跟我哥借了一万块钱，说是卖了谷子就还他。'),
    ('调查员', '这次申请贷款打算用多少钱，做什么用？'),
    ('农户', '想申请五十万，一半付流转费和买农机，一半做周转。打算贷十二个月，卖了粮就还。'),
    ('调查员', '好，情况我记完了。回去我们汇总测算，有结果会联系你。'),
]
GAP_SECONDS = 0.55
SAMPLE_RATE = 16000


def synthesize(lines, work_dir):
    script = work_dir / 'lines.json'
    script.write_text(json.dumps([t for _, t in lines], ensure_ascii=False), encoding='utf-8')
    ps = work_dir / 'tts.ps1'
    ps.write_text(r"""
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
""", encoding='utf-8-sig')
    subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                    str(ps), str(script), str(work_dir)], check=True, timeout=900)
    blobs = []
    for index in range(len(lines)):
        with wave.open(str(work_dir / f'c_{index:02d}.wav')) as reader:
            assert (reader.getframerate(), reader.getsampwidth(), reader.getnchannels()) == (16000, 2, 1)
            blobs.append(reader.readframes(reader.getnframes()))
    return blobs


def main():
    store = DueStore(ROOT / 'data')
    with tempfile.TemporaryDirectory() as tmp:
        blobs = synthesize(LINES, Path(tmp))
    recording_id = 'rec_full_' + __import__('uuid').uuid4().hex[:10]
    config = SessionConfig(app_id=APP_ID, instance_id='full-interview-seed', session_id=recording_id,
                           output_root=(store.root / 'recordings').resolve(), device_id=0,
                           capture_format={'sample_rate': 16000, 'channels': 1, 'sample_width': 2},
                           asr_config={'engine': 'imported'})
    writer = ArchiveWriter(config, 'full-interview', source='import')
    writer.started()
    gap = b'\x00\x00' * int(SAMPLE_RATE * GAP_SECONDS)
    segments, cursor = [], 0

    def commit_pcm(blob):
        for offset in range(0, len(blob), 32000):
            writer.commit(blob[offset:offset + 32000])

    for index, ((speaker, text), blob) in enumerate(zip(LINES, blobs)):
        start = cursor
        commit_pcm(blob)
        cursor += len(blob) // 2
        commit_pcm(gap)
        cursor += len(gap) // 2
        segments.append({'id': f'full_{index+1:02d}', 'ordinal': index + 1, 'revision': 1, 'stable': True,
                         'text': f'{speaker}：{text}',
                         'start_seconds': round(start / SAMPLE_RATE, 2),
                         'end_seconds': round((start + len(blob) // 2) / SAMPLE_RATE, 2),
                         'source_run_id': 'full-tts'})
    writer.close()
    total = cursor
    stamp = now()
    snapshot = {'id': recording_id, 'case_id': CASE, 'interview_id': INTERVIEW, 'device_id': 0,
                'purpose': 'imported-interview', 'imported_from': '现场访谈完整版.wav',
                'created_at': stamp, 'state': 'saved', 'capture_state': 'saved',
                'saved_frames': total, 'saved_seconds': round(total / SAMPLE_RATE, 2),
                'sample_rate': SAMPLE_RATE, 'started_at': stamp, 'stopped_at': stamp, 'last_saved_at': stamp,
                'audio_available': True, 'error': None, 'data_error': '',
                'asr': {'state': 'failed', 'error': None, 'message': '导入完成，等待转写。', 'transcribed_frames': 0},
                'segments': []}
    context = {'app_id': APP_ID, 'instance_id': config.instance_id, 'session_id': recording_id,
               'output_root': str((store.root / 'recordings').resolve()), 'device_id': 0,
               'capture_format': {'sample_rate': 16000, 'channels': 1, 'sample_width': 2},
               'asr_config': {'engine': 'imported'}}
    with store.lock, store.connect() as connection:
        connection.execute('INSERT INTO recordings VALUES (?,?,?,?,?,?,?,?)',
                           (recording_id, CASE, INTERVIEW, APP_ID, stamp, stamp,
                            json.dumps(context, ensure_ascii=False),
                            json.dumps(snapshot, ensure_ascii=False)))
        for segment in segments:
            connection.execute('INSERT INTO transcript_revisions VALUES (?,?,?,?,?,?,?,?)',
                               (recording_id, segment['id'], 1, 'full-tts', segment['text'],
                                int(segment['start_seconds'] * SAMPLE_RATE),
                                int(segment['end_seconds'] * SAMPLE_RATE), stamp))
    print(json.dumps({'recording_id': recording_id, 'lines': len(LINES),
                      'duration_seconds': snapshot['saved_seconds'],
                      'wav': str((store.root / 'recordings' / recording_id / 'recording.wav'))},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
