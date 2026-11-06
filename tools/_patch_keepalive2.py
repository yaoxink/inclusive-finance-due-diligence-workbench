"""Rework ASR keep-alive: state-driven parking (no stdout-event dependency).

- worker: unchanged keep-alive loop (emit asr_idle stays, harmless)
- supervisor: park_asr_process() moves a live worker into the idle pool and
  marks the process object; _watch's finally skips termination for parked
  processes (cancel-safe). Adoption is unchanged (attach via stdin).
- probes from debugging are removed.
Run once; idempotent-ish via exact-match asserts.
"""
from pathlib import Path
import ast

CORE = Path(r'D:\MBA\AI创业大赛\shared-recording-core\shared_recording_core')
sup = CORE / 'supervisor.py'
text = sup.read_text(encoding='utf-8')


def replace(old, new, label):
    global text
    assert old in text, f'anchor missing: {label}'
    text = text.replace(old, new, 1)
    print('patched:', label)


# 1) drop the stdout idle-branch and probes inside _watch loop
replace('''                try:
                    value = json.loads(line.decode("utf-8"))
                    event = Event.from_dict(value).validate_context(self.config)
                    if event.producer != producer:
                        raise ContractError("worker used an unexpected producer")
                    if producer == "asr" and event.event == "asr_idle":
                        self._state["debug_asr_idle_seen"] = utc_now()
                        import sys as _sys
                        print(f"DEBUG sup idle, keepalive={os.environ.get('DUE_ASR_KEEPALIVE')}", file=_sys.stderr, flush=True)
                        if os.environ.get("DUE_ASR_KEEPALIVE") == "1":
                            parked = True
                            self._state["debug_asr_parked"] = utc_now()
                            break
                        await self._send(process, "exit")
                        continue
                    self._accept(event)
                except (ValueError, TypeError, UnicodeError):
                    self._state[f"{producer}_protocol_warning"] = "invalid worker notification ignored"''',
'''                try:
                    value = json.loads(line.decode("utf-8"))
                    event = Event.from_dict(value).validate_context(self.config)
                    if event.producer != producer:
                        raise ContractError("worker used an unexpected producer")
                    if producer == "asr" and event.event == "asr_idle":
                        # session done and the decoder stays loaded; park the
                        # worker for the next recording instead of letting the
                        # worker process exit (keep-alive deployments only).
                        if os.environ.get("DUE_ASR_KEEPALIVE") == "1":
                            parked = True
                            break
                        await self._send(process, "exit")
                        continue
                    self._accept(event)
                except (ValueError, TypeError, UnicodeError):
                    self._state[f"{producer}_protocol_warning"] = "invalid worker notification ignored"''',
        'watch idle branch')

# 2) remove the watch-line probe header if present
replace('''        stderr_task = asyncio.create_task(stderr())
        parked = False
        try:
            while line := await process.stdout.readline():
                import sys as _s
                if producer == "asr":
                    print(f"DEBUG watch line: {line[:80]!r}", file=_s.stderr, flush=True)''',
'''        stderr_task = asyncio.create_task(stderr())
        parked = False
        try:
            while line := await process.stdout.readline():''',
        'watch probe removal')

# 3) finally: parked (flag or process attribute) -> pool, no terminate
replace('''        finally:
            if parked and process.returncode is None:
                _ASR_IDLE_POOL.append((process, str(self.asr_home)))
            else:
                await terminate_process_tree(process)''',
'''        finally:
            if (parked or getattr(process, "_asr_parked", False)) and process.returncode is None:
                process._asr_parked = True
                _ASR_IDLE_POOL.append((process, str(self.asr_home)))
            else:
                await terminate_process_tree(process)''',
        'finally park')

# 4) public parking helper + adoption clears the flag
replace('''async def drain_asr_pool():''',
'''def park_asr_process(process, home):
    """Move a live finished ASR worker into the idle pool.

    Parking is driven by the persisted asr state (completed) instead of
    worker stdout events, which proved lossy on Windows pipes. The old
    supervisor's watch task may be cancelled afterwards: the parked flag
    keeps its finally-branch from terminating the worker.
    """
    if process is None or process.returncode is not None:
        return False
    process._asr_parked = True
    _ASR_IDLE_POOL.append((process, str(home)))
    return True


async def drain_asr_pool():''',
        'park helper')

replace('''        while _ASR_IDLE_POOL:
            process, home = _ASR_IDLE_POOL.pop()
            if process.returncode is not None:
                continue''',
'''        while _ASR_IDLE_POOL:
            process, home = _ASR_IDLE_POOL.pop()
            if process.returncode is not None:
                continue
            process._asr_parked = False''',
        'adopt clears flag')

ast.parse(text)
sup.write_text(text, encoding='utf-8')
print('supervisor.py reworked')

# 5) due-app: park on completed inside refresh (single authoritative spot)
rec = Path(r'D:\MBA\AI创业大赛\due-diligence-app\due_app\recordings.py')
t2 = rec.read_text(encoding='utf-8')
anchor = "    def refresh(self, recording_id):"
assert anchor in t2
insert_after_anchor = '''
    def _try_park_asr(self, recording_id, value):
        """Park the finished ASR worker so the next recording reuses the model."""
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
t2 = t2.replace(anchor, insert_after_anchor + anchor, 1)
# call it at the end of refresh — find the return of refresh by locating the
# method body's final "return value" within refresh before next def
start = t2.index(anchor)
next_def = t2.index('\n    def ', start + 10)
body = t2[start:next_def]
# append the call before the last 'return value' line of refresh
lines = body.rstrip().splitlines()
assert lines[-1].strip() == 'return value', lines[-1]
lines.insert(-1, '        self._try_park_asr(recording_id, value)')
t2 = t2[:start] + '\n'.join(lines) + '\n' + t2[next_def:]
ast.parse(t2)
rec.write_text(t2, encoding='utf-8')
print('recordings.py parking hook installed')

# 6) strip debug probes from asr_worker trace helper (keep clean behavior)
w = CORE / 'asr_worker.py'
t3 = w.read_text(encoding='utf-8')
t3 = t3.replace('                _trace("emit asr_idle")\n', '')
t3 = t3.replace('                _trace("emit asr_idle done")\n', '')
t3 = t3.replace('        _trace("cmd: " + str(value))\n', '')
t3 = t3.replace('                _trace("stdin EOF")\n', '')
t3 = t3.replace('        _trace("attach wait begin")\n', '')
t3 = t3.replace('        _trace("entering attach wait")\n', '')
t3 = t3.replace('        _trace(f"attach wait got: {command}")\n', '')
t3 = t3.replace('        _trace("run exception: " + type(exc).__name__)\n', '')
# remove the _trace helper itself
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
print('asr_worker.py probes removed')

# 7) remove terminate tracer from processes.py
pr = CORE / 'processes.py'
t4 = pr.read_text(encoding='utf-8')
t4 = t4.replace('''    import sys as _sys, traceback as _tb
    _sys.stderr.write("DEBUG terminate pid=%s\\n%s\\n" % (process.pid, "".join(_tb.format_stack(limit=7)[:-1])))
    _sys.stderr.flush()
''', '')
ast.parse(t4)
pr.write_text(t4, encoding='utf-8')
print('processes.py tracer removed')
print('ALL DONE')
