"""Patch terminate_process_tree with a stderr stack tracer (diagnostic only)."""
from pathlib import Path
import ast

p = Path(r'D:\MBA\AI创业大赛\shared-recording-core\shared_recording_core\processes.py')
lines = p.read_text(encoding='utf-8').splitlines()

out, i, dropped = [], 0, 0
while i < len(lines):
    line = lines[i]
    if 'DEBUG terminate' in line:
        j = i
        while j < len(lines):
            if 'process.stdin.close()' in lines[j] and j > i:
                break
            j += 1
        i = j  # resume at the stdin.close line (kept below)
        dropped += 1
        continue
    out.append(line)
    i += 1

text = '\n'.join(out)
anchor = '''    if process.stdin:
        process.stdin.close()
    job = getattr(process, "_recording_job", None)'''
probe = (
    '    import sys as _sys, traceback as _tb\n'
    '    _sys.stderr.write("DEBUG terminate pid=%s\\n%s\\n" % (process.pid, "".join(_tb.format_stack(limit=7)[:-1])))\n'
    '    _sys.stderr.flush()\n'
    '    if process.stdin:\n'
    '        process.stdin.close()\n'
    '    job = getattr(process, "_recording_job", None)'
)
assert anchor in text, 'anchor missing'
text = text.replace(anchor, probe, 1)
ast.parse(text)
p.write_text(text, encoding='utf-8')
print('tracer installed, dropped broken probes:', dropped)
