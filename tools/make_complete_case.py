"""Create a fully-populated demo case (answers + valid calculation + report).

Business-like second demo case so the workbench shows a complete end-to-end
record: presale basics, onsite answers, a valid model run (score/grade/limit)
and an exportable report. Built entirely through the public HTTP API so every
contract check runs. Fictional/test wording from the bundled example inputs is
rewritten to business wording. Synthetic workspace only.
"""
import json
import re
import sys
import urllib.request

BASE = 'http://127.0.0.1:8769/api/due-diligence'

WORDING = [
    ('虚构演示经办人', '客户经理'),
    ('虚构演示复核人', '风控复核岗'),
    ('虚构合成资料：', '现场核实：'),
    ('虚构明确完整会计年度', '2025完整会计年度'),
    ('纯虚构演示，已对照合成预期；', ''),
    ('纯虚构演示，已对照合成预期。', ''),
    ('虚构演示：', ''),
    ('虚构演示', '本案'),
    ('虚构', ''),
]


def call(method, path, body=None, expect=200):
    data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
    response = urllib.request.urlopen(request, timeout=60)
    payload = json.loads(response.read())
    if not (200 <= response.status < 300) and response.status != expect:
        raise SystemExit(f'{method} {path} -> {response.status}')
    return payload


def clean(value):
    text = value
    for old, new in WORDING:
        text = text.replace(old, new)
    return text


def deep_clean(node):
    if isinstance(node, str):
        return clean(node)
    if isinstance(node, list):
        return [deep_clean(item) for item in node]
    if isinstance(node, dict):
        return {key: deep_clean(item) for key, item in node.items()}
    return node


YEAR = {'kind': 'year', 'start': '2025-01-01', 'end': '2025-12-31', 'label': '2025年度'}


def answer(case, field_id, number=None, unit=None, text=None):
    value = ({'number': number, 'unit': unit} if number is not None
             else {'text': text} if text else None)
    call('POST', f'/cases/{case}/answers',
         {'field_id': field_id, 'status': 'confirmed', 'actor': '客户经理',
          'value': value, 'period': YEAR if number is not None else None,
          'basis': [{'type': 'manual', 'id': 'interview', 'note': '现场访谈核实'}]})


def main():
    case = call('POST', '/cases', {'title': '李秀芳-水稻种植贷'})['id']
    call('PUT', f'/cases/{case}/customer-info', {
        'fields': {'customer_name': '李秀芳', 'id_type': '居民身份证',
                   'id_number': '510124197809205623', 'phone': '13982015678',
                   'home_address': '四川省成都市邛崃市固驿镇春江村2组',
                   'contact_address': '四川省成都市邛崃市固驿镇春江村2组', 'note': None},
        'expected_revision': 0, 'actor': '客户经理'})
    call('POST', f'/cases/{case}/previsit/load', {'fixture_id': 'SYN-S0-001', 'variant': 'base'})

    # onsite answers for the visible questionnaire (presale-covered fields excluded)
    answer(case, 'crop.main.area', 30, '亩')
    answer(case, 'crop.main.yield', 2000, '斤/亩')
    answer(case, 'crop.main.price', 5, '元/斤')
    answer(case, 'crop.main.experience_years', 12, '年')
    answer(case, 'cost.seed', 1800, '元')
    answer(case, 'cost.pesticide', 900, '元')
    answer(case, 'cost.fertilizer', 4200, '元')
    answer(case, 'business.sales_stability', text='收购商长期合作，渠道稳定')
    answer(case, 'operator.marital_status', text='已婚')

    # calculation: use the bundled complete example, business wording only
    examples = call('GET', f'/cases/{case}/calculation/v2/examples')['examples']
    complete = next(item for item in examples if item['id'] == 'V2-COMPLETE')
    inputs = deep_clean(complete['inputs'])
    saved = call('PUT', f'/cases/{case}/calculation/v2/inputs',
                 {'expected_revision': 0, 'inputs': inputs})
    revision = saved['draft']['revision']
    run = call('POST', f'/cases/{case}/calculation/v2/run', {'expected_revision': revision})
    result = run if 'score' in run else call('GET', f'/cases/{case}/calculation/v2')['latest_run']

    # report with the run attached
    report = call('POST', f'/cases/{case}/reports', {}, expect=201)
    print(json.dumps({'case': '李秀芳-水稻种植贷', 'case_id': case,
                      'run_state': result.get('state'),
                      'score': result.get('score'), 'grade': result.get('grade'),
                      'effective_limit': result.get('effective_limit') or result.get('current_effective_limit'),
                      'report_id': report.get('id'), 'report_revision': report.get('revision')},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
