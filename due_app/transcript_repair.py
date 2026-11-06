"""Proxy for the local Hermes transcript-repair agent.

The page cannot call http://127.0.0.1:8081 directly (cross-origin), so the
service proxies the batch and enforces the same fixed-endpoint policy used
elsewhere: loopback host only, no redirects, no proxies, bounded body and
timeout. Original transcripts are never modified — the repaired text is a
side-by-side reading aid only.
"""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_BASE = 'http://127.0.0.1:8081'
DEFAULT_MODEL = 'due-transcript-repairer'
ALLOWED_REPAIR_HOSTS = ('127.0.0.1', 'localhost')
MAX_SEGMENTS_PER_BATCH = 60
MAX_TEXT_CHARS = 8000


class RepairSegment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=2000)


class RepairRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    segments: list[RepairSegment] = Field(min_length=1, max_length=MAX_SEGMENTS_PER_BATCH)


def _repair_endpoint():
    base = os.getenv('DUE_REPAIR_BASE_URL') or DEFAULT_BASE
    model = os.getenv('DUE_REPAIR_MODEL') or DEFAULT_MODEL
    parsed = urlparse(base)
    if parsed.scheme != 'http' or parsed.hostname not in ALLOWED_REPAIR_HOSTS or parsed.username:
        raise HTTPException(503, '修复端点仅允许本机回环地址。')
    return base.rstrip('/'), model


def call_transcript_repair(segments):
    base, model = _repair_endpoint()
    total = sum(len(segment.text) for segment in segments)
    if total > MAX_TEXT_CHARS:
        raise HTTPException(422, '单批修复文本过长，请减小批量。')

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    payload = json.dumps({'model': model, 'temperature': 0, 'stream': False,
                          'messages': [{'role': 'user', 'content': json.dumps(
                              {'segments': [{'id': s.id, 'text': s.text} for s in segments]},
                              ensure_ascii=False)}]},
                         ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(base + '/v1/chat/completions', data=payload,
                                     headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with opener.open(request, timeout=90) as response:
            body = json.loads(response.read(262145))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise HTTPException(503, f'本地修复智能体不可用或超时（{type(exc).__name__}）；原文字记录不受影响。') from exc
    content = (body.get('choices') or [{}])[0].get('message', {}).get('content')
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(502, '修复智能体返回为空；原文字记录不受影响。')
    text = content.strip()
    if text.startswith('```'):
        text = text.strip('`')
        if text.startswith('json'):
            text = text[4:]
        text = text.strip()
    start, end = text.find('{'), text.rfind('}')
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise HTTPException(502, '修复智能体返回不是有效JSON；原文字记录不受影响。') from exc
    repaired = parsed.get('repaired')
    if not isinstance(repaired, list):
        raise HTTPException(502, '修复结果缺少 repaired 列表；原文字记录不受影响。')
    known = {segment.id for segment in segments}
    clean = [{'id': str(item.get('id')), 'text': str(item.get('text') or '')[:2000]}
             for item in repaired
             if isinstance(item, dict) and item.get('id') in known and item.get('text')]
    return {'repaired': clean, 'notes': [str(n)[:300] for n in parsed.get('notes', [])][:20],
            'requested': len(segments), 'accepted': len(clean)}


def install_transcript_repair_routes(app):
    from fastapi import APIRouter
    router = APIRouter(prefix='/api/due-diligence')

    @router.post('/transcript-repair')
    def transcript_repair(body: RepairRequest):
        return call_transcript_repair(body.segments)

    app.include_router(router)
