"""Create a reviewable code-only recovery point, never a customer-data package."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


def digest(content):
    return hashlib.sha256(content).hexdigest()


def build(destination: Path):
    workspace = Path(__file__).resolve().parents[2]
    destination = destination.resolve()
    allowed = (workspace / 'planning' / '2026-10-31' / 'implementation').resolve()
    if not destination.is_relative_to(allowed) or destination == allowed:
        raise ValueError('Use a new package directory inside the implementation evidence folder.')
    destination.mkdir(parents=True, exist_ok=False)
    files = {}

    def add(path):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Source package cannot contain links.')
        relative = path.relative_to(workspace).as_posix()
        if path.name.startswith('.env') or path.suffix.lower() in {'.sqlite', '.sqlite3', '.db', '.pem', '.key'}:
            raise ValueError('Private configuration/data cannot enter a source package: ' + relative)
        files[relative] = path.read_bytes()

    def tree(base, skip):
        for path in sorted(base.rglob('*')):
            parts = path.relative_to(base).parts
            if any(p in skip for p in parts):
                continue
            if path.is_file():
                add(path)

    tree(workspace / 'due-diligence-app', {'data', 'frontend-dist', '__pycache__', '.pytest_cache', '.venv'})
    # The application data catalog is code-owned, distinct from due-diligence-app/data.
    tree(workspace / 'due-diligence-app' / 'due_app' / 'data', {'__pycache__'})
    tree(workspace / 'shared-recording-core', {'__pycache__', '.pytest_cache', '.venv', 'node_modules'})
    frontend = workspace / 'meeting-demo' / 'frontend'
    tree(frontend / 'src', {'__pycache__'})
    for path in frontend.iterdir():
        if path.is_file() and path.name in {'package.json', 'package-lock.json', 'tsconfig.json', 'tsconfig.app.json',
                'tsconfig.node.json', 'vite.config.ts', 'vite.due.config.ts', 'due-diligence.html'}:
            add(path)
    dist = workspace / 'due-diligence-app' / 'frontend-dist'
    page = dist / 'due-diligence.html'
    add(page)
    pending = list(re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page.read_text(encoding='utf-8')))
    seen = set()
    while pending:
        relative = pending.pop()
        if relative in seen:
            continue
        if not re.fullmatch(r'/assets/[A-Za-z0-9_.-]+', relative):
            raise ValueError('Unexpected compiled asset path.')
        seen.add(relative)
        path = dist / relative.lstrip('/')
        add(path)
        if path.suffix in {'.js', '.css'}:
            pending.extend('/assets/' + item for item in re.findall(r'(?:["\'(]|/assets/)(farmland-cover-[A-Za-z0-9_.-]+\.png)', path.read_text(encoding='utf-8')))
    manifest = {'schema': 'due-source-package/1', 'data_included': False, 'dependencies_included': False,
                'scope': 'due workbench source, shared recording core, frontend source and active compiled assets',
                'files': [{'path': name, 'bytes': len(content), 'sha256': digest(content)} for name, content in sorted(files.items())]}
    archive = destination / 'source.zip'
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as output:
        for name, content in sorted(files.items()):
            output.writestr(name, content)
        output.writestr('SOURCE_MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(archive) as check:
        if check.testzip() is not None:
            raise ValueError('Source ZIP CRC failed.')
        for item in manifest['files']:
            if digest(check.read(item['path'])) != item['sha256']:
                raise ValueError('Source ZIP digest failed.')
    manifest['archive_sha256'] = digest(archive.read_bytes())
    manifest['archive_bytes'] = archive.stat().st_size
    manifest['verification'] = 'all members CRC and SHA256 checked'
    (destination / 'SOURCE_FREEZE.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'archive': str(archive), 'files': len(files), 'sha256': manifest['archive_sha256'], 'bytes': manifest['archive_bytes']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    build(parser.parse_args().destination)
