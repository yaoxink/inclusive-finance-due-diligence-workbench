"""Install the app-side parking hook and strip debug probes (step 2)."""
from pathlib import Path
import ast

rec = Path(r'D:\MBA\AI创业大赛\due-diligence-app\due_app\recordings.py')
t = rec.read_text(encoding='utf-8')
if '_try_park_asr' not in t:
    anchor = '    def refresh(self, recording_id):'
    assert anchor in t
    hook = '''    def _try_park_asr(self, recording_id, value):
        """Park a finished ASR worker so the next recording reuses the model."""
        if value.get('asr', {}).get('state') != 'completed':
            return
        worker = self.supervisors.get(recording_id)
        if worker is None or worker.asr_process is None:
            return
        if getattr(worker.asr_process, '_asr_parked', False):
            return
        from shared_recording_core.supervisor import park_asr_process
        if park_asr_process(worker.asr_process, worker.asr_home):
            worker.asr_process = None
            task = worker._tasks.get('asr')
            if task is not None and not task.done():
                task.get_loop().call_soon_threadsafe(task.cancel)
            self.supervisors.pop(recording_id, None)

'''
    t = t.replace(anchor, hook + anchor, 1)
    start = t.index(anchor)
    next_def = t.index('\n    def ', start + 10)
    body_lines = t[start:next_def].rstrip().splitlines()
    assert body_lines[-1].strip() == 'return value', body_lines[-1]
    body_lines.insert(-1, '        self._try_park_asr(recording_id, value)')
    t = t[:start] + '\n'.join(body_lines) + '\n' + t[next_def:]
    ast.parse(t)
    rec.write_text(t, encoding='utf-8')
    print('parking hook installed')
else:
    print('parking hook already present')

# strip probes
w = Path(r'D:\MBA\AI创业大赛\shared-recording-core\shared_recording_core\asr_worker.py')
t3 = w.read_text(encoding='utf-8')
for probe in ('_trace("emit asr_idle")', '_trace("emit asr_idle done")',
              '_trace("cmd: " + str(value))', '_trace("stdin EOF")',
              '_trace("attach wait begin")', '_trace("entering attach wait")',
              '_trace(f"attach wait got: {command}")',
              '_trace("run exception: " + type(exc).__name__)'):
    t3 = t3.replace(probe + '\n', '')
lines, out, skip = t3.splitlines(), [], 0
for line in lines:
    if line.startswith('def _trace('):
        skip = 4
        continue
    if skip:
        skip -= 1
        continue
    out.append(line)
t3 = '\n'.join(out)
ast.parse(t3)
w.write_text(t3, encoding='utf-8')
print('worker probes stripped')

pr = Path(r'D:\MBA\AI创业大赛\shared-recording-core\shared_recording_core\processes.py')
t4 = pr.read_text(encoding='utf-8')
t4 = t4.replace('''    import sys as _sys, traceback as _tb
    _sys.stderr.write("DEBUG terminate pid=%s\\n%s\\n" % (process.pid, "".join(_tb.format_stack(limit=7)[:-1])))
    _sys.stderr.flush()
''', '')
ast.parse(t4)
pr.write_text(t4, encoding='utf-8')
print('terminate tracer stripped')
