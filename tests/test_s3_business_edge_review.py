"""Direct A02/A08 business assertions using isolated synthetic domain fixtures."""
from __future__ import annotations

import copy

from test_s3_domain import env, make_case, source, values


def test_a02_uploaded_not_parsed_material_is_retained_without_cashflow_facts(env):
    cid = make_case(env, "SYN-S0-002")
    state = env.service.workspace(cid)
    original = env.service.fixtures["SYN-S0-002"]["previsit_context"]
    material = next(m for m in state["previsit"]["materials"] if m["material_id"] == "C2-MAT2")
    assert material == next(m for m in original["materials"] if m["material_id"] == "C2-MAT2")
    assert material["kind"] == "land_contract"
    assert material["processing_state"] == "uploaded_not_parsed"
    assert material["access_status"] == "available"
    assert material["verification"]["state"] == "not_checked"
    assert material["verification"]["simulated"] is True
    assert material["original_uri"] is None and material["content_hash"] is None
    cashflow_ids = {"cashflow.bank_files", "cashflow.quarter_interest", "cashflow.annual_inflow"}
    fields = {f["field_id"]: f for f in state["questionnaire"]["fields"]}
    for field_id in cashflow_ids:
        if field_id == "cashflow.quarter_interest":
            # 2026-11-05: asked onsite now (answer-driven calculation input)
            assert fields[field_id]["handling"] == "interview" and fields[field_id]["question_text"]
            continue
        assert fields[field_id]["handling"] == "upstream_only"
        assert fields[field_id]["question_text"] is None
        assert fields[field_id]["completion"] == "missing" and fields[field_id]["fact_ids"] == []
    assert not any(f["field_id"].startswith("cashflow.") for f in state["facts"])
    assert state["credit"]["state"] == "blocked" and state["credit"]["effective_limit"] is None


def _annual_income(env, cid):
    return next(f for f in env.service.workspace(cid)["facts"]
                if f["origin"] == "previsit" and f["field_id"] == "business.other.annual_income")


def _confirm_income(env, cid, original, *, text, value, unit, period):
    statement = source(env, cid, text=text)
    payload = values(statement, field_id=original["field_id"], value_type="decimal", value=value,
                     unit=unit, period=copy.deepcopy(period), subject_id=original["subject_id"],
                     subject_basis="合成原文明示与上游为同一经营单元，保留原单位和期间")
    fact = env.service.create_fact(cid, payload)
    env.service.confirm_statement(cid, statement["id"], statement["revision"])
    return env.service.confirm_fact(cid, fact["id"], fact["revision"], "对照合成原文确认记录准确")


def _unresolved_income_conflict(env, cid, original, fact):
    state = env.service.workspace(cid)
    both = {f["id"]: f for f in state["facts"] if f["field_id"] == original["field_id"]}
    assert set(both) == {original["id"], fact["id"]}
    assert all(f["conflict"] is True for f in both.values())
    assert all(f["calculation_eligibility"] == "blocked_conflict" for f in both.values())
    assert both[fact["id"]]["record_confirmation"] == "record_accurate"
    assert both[fact["id"]]["verification"] == "not_checked"
    conflict = next(c for c in state["conflicts"] if c["field_id"] == original["field_id"])
    assert conflict["unresolved"] is True and set(conflict["fact_ids"]) == set(both)
    assert state["credit"]["state"] == "blocked" and state["credit"]["effective_limit"] is None
    return both, conflict


def test_a08_yuan_and_ten_thousand_yuan_remain_original_unit_conflict(env):
    cid = make_case(env)
    original = _annual_income(env, cid)
    assert original["value"] == "12000" and original["unit"] == "元"
    fact = _confirm_income(env, cid, original, text="合成口述：这项收入是1.2万元，期间和上游相同。",
                           value="1.2", unit="万元", period=original["period"])
    both, conflict = _unresolved_income_conflict(env, cid, original, fact)
    assert (both[original["id"]]["value"], both[original["id"]]["unit"]) == ("12000", "元")
    assert (both[fact["id"]]["value"], both[fact["id"]]["unit"]) == ("1.2", "万元")
    assert both[fact["id"]]["period"] == original["period"]
    assert "unit" in conflict["kinds"] and "period" not in conflict["kinds"]


def test_a08_month_and_year_remain_original_period_conflict_without_annualizing(env):
    cid = make_case(env)
    original = _annual_income(env, cid)
    month = {"kind": "duration", "label": "2026年10月（合成月度口径）",
             "start": "2026-10-01", "end": "2026-10-31"}
    fact = _confirm_income(env, cid, original, text="合成口述：这12000元只属于2026年10月，不是全年收入。",
                           value="12000", unit="元", period=month)
    both, conflict = _unresolved_income_conflict(env, cid, original, fact)
    assert both[original["id"]]["period"]["kind"] == "year"
    assert both[original["id"]]["period"] == original["period"]
    assert both[fact["id"]]["period"] == month
    assert both[original["id"]]["value"] == both[fact["id"]]["value"] == "12000"
    assert both[original["id"]]["unit"] == both[fact["id"]]["unit"] == "元"
    assert conflict["kinds"] == ["period"]
