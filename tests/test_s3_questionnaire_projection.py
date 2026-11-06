"""S3 static source projection checks; parsed specifications and synthetic inputs only.

No app, database, workbook, microphone or ASR imports are needed for these checks.
Runtime condition checks import the formal S3 domain only, never the app or a store.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re

import pytest


APP = Path(__file__).resolve().parents[1]
WORKSPACE = APP.parent
ANALYSIS = WORKSPACE / "planning" / "2026-10-30" / "material-analysis"
QUESTIONNAIRE_PATH = APP / "due_app" / "data" / "questionnaire_v1.json"
COVERAGE_PATH = WORKSPACE / "planning" / "2026-10-30" / "implementation" / "s3" / "FIELD_COVERAGE.json"
QUESTIONNAIRE = json.loads(QUESTIONNAIRE_PATH.read_text(encoding="utf-8"))
SOURCE = json.loads((ANALYSIS / "questionnaire-fields.json").read_text(encoding="utf-8"))
CANONICAL = json.loads((ANALYSIS / "canonical-field-map.json").read_text(encoding="utf-8"))
FIELDS = {f["field_id"]: f for f in QUESTIONNAIRE["fields"]}
SOURCE_ROWS = {q["question_id"]: q for q in SOURCE["question_records"]}
CANONICAL_FIELDS = {f["canonical_id"]: f for f in CANONICAL["fields"]}


def test_54_unique_candidates_and_28_interview_fields_have_honest_status():
    assert QUESTIONNAIRE["version"] == "farmer_questionnaire_v1"
    assert QUESTIONNAIRE["status"] == "candidate_static_projection_not_business_confirmed"
    assert len(QUESTIONNAIRE["fields"]) == len(FIELDS) == 54
    assert set(FIELDS) == set(CANONICAL_FIELDS)
    assert Counter(f["handling"] for f in FIELDS.values()) == {
        "interview": 28, "extension_required": 17, "upstream_only": 8, "not_supported": 1,
    }
    assert all(f["mapping_status"] == "candidate_not_business_approved" for f in FIELDS.values())
    assert all(f["technical_integration_status"] == "static_projection_only" for f in FIELDS.values())


def test_provenance_hashes_refer_only_to_the_two_parsed_inputs():
    expected = {
        "planning/2026-10-30/material-analysis/questionnaire-fields.json",
        "planning/2026-10-30/material-analysis/canonical-field-map.json",
    }
    assert set(QUESTIONNAIRE["source_hashes"]) == expected
    for relative, expected_hash in QUESTIONNAIRE["source_hashes"].items():
        assert hashlib.sha256((WORKSPACE / relative).read_bytes()).hexdigest() == expected_hash
    assert QUESTIONNAIRE["source_workbooks_read_by_this_projection"] is False
    assert QUESTIONNAIRE["customer_sample_values_included"] is False


def test_field_question_references_and_original_keys_are_preserved():
    for field_id, field in FIELDS.items():
        original = CANONICAL_FIELDS[field_id]
        assert field["question_keys"] == original["question_keys"]
        assert field["source_question_ids"] == [q["question_id"] for q in original["question_sources"]]
        for source_id in field["source_question_ids"]:
            assert source_id in SOURCE_ROWS
        references = [x for x in field["source_locations"] if x["kind"] == "questionnaire"]
        assert {r["source_question_id"] for r in references} == set(field["source_question_ids"])
        for reference in references:
            source = SOURCE_ROWS[reference["source_question_id"]]
            assert reference["location"] == source["source_range"]
            assert reference["raw_key"] == source["field_key_raw"]
            assert reference["question_key"] == source["field_key_candidate"]


def test_all_278_source_records_have_one_traceable_disposition():
    projection = QUESTIONNAIRE["source_projection"]
    assert len(projection) == len({p["source_question_id"] for p in projection}) == 278
    assert {p["source_question_id"] for p in projection} == set(SOURCE_ROWS)
    for row in projection:
        source = SOURCE_ROWS[row["source_question_id"]]
        assert row["raw_key"] == source["field_key_raw"]
        assert row["source_range"] == source["source_range"]
        assert row["display_condition_texts"] == source["display_condition_texts"]
        assert row["source_conditions_executable"] is False
        assert row["active_interview_candidate"] is (row["handling"] == "interview")
        assert row["handling"] in {"interview", "upstream_only", "extension_required", "not_supported"}
        for field_id in row["mapped_field_ids"]:
            assert row["source_question_id"] in FIELDS[field_id]["source_question_ids"]


def test_upload_photo_credential_and_device_actions_cannot_be_interview_records():
    for row in QUESTIONNAIRE["source_projection"]:
        content = " ".join(str(row.get(k) or "") for k in ("label", "question_key", "question_type_raw"))
        if re.search(r"unzip|解压码|解密|密码", content, re.I):
            assert row["handling"] == "not_supported"
            assert not row["active_interview_candidate"]
        elif re.search(r"上传|拍照|照相|影像|照片|截图|查询凭证", content):
            assert row["handling"] == "upstream_only"
            assert not row["active_interview_candidate"]
        if row["question_type_raw"] in {"点击按钮，实时定位-调用接口返显", "反显不可修改"}:
            assert row["handling"] == "upstream_only"
        if row["question_key"] == "bank_statement_type":
            assert row["handling"] == "upstream_only"


def test_only_interview_scalar_candidates_have_a_question_text():
    forbidden = re.compile(r"上传|拍照|照相|OCR|解压|解密|密码|调用接口|证件影像", re.I)
    for field in FIELDS.values():
        if field["handling"] == "interview":
            assert field["value_type"] in {"integer", "decimal", "string", "enum"}
            assert field["source_question_ids"]
            assert field["question_text"] and not forbidden.search(field["question_text"])
            assert all(SOURCE_ROWS[q]["field_key_kind"] == "scalar_identifier" for q in field["source_question_ids"])
        else:
            assert field["question_text"] is None


@pytest.mark.parametrize("field_id", [
    "household.member_list", "land.transfers", "insurance.policies", "assets.vehicles",
    "assets.machinery", "assets.financial", "debt.bank_contracts", "debt.other_contracts",
    "crop.main.category",
])
def test_collections_and_unresolved_taxonomy_cannot_be_accepted_as_scalar_questions(field_id):
    assert FIELDS[field_id]["handling"] == "extension_required"
    assert FIELDS[field_id]["value_type"] in {"array", "object", "unresolved"}
    assert FIELDS[field_id]["question_text"] is None


def test_upstream_and_policy_inputs_are_not_replaced_with_interview_or_llm_defaults():
    for field_id in (
        "business.location", "cashflow.bank_files", "cashflow.quarter_interest",
        "cashflow.annual_inflow", "loan.term", "policy.cashflow_margin",
        "policy.industry_route_margins", "policy.deposit_rate",
    ):
        assert FIELDS[field_id]["handling"] == "upstream_only"
        assert FIELDS[field_id]["question_text"] is None
        assert "default_value" not in FIELDS[field_id]
    assert FIELDS["output.pricing"]["handling"] == "not_supported"
    assert "default_unit" not in FIELDS["loan.term"]


def test_no_unresolved_period_or_original_asset_unit_is_silently_normalized():
    assert FIELDS["assets.house_declared_value"]["default_unit"] == "万元"
    assert FIELDS["cost.other"]["default_unit"] == "元"
    assert FIELDS["cost.service"]["default_unit"] == "元"
    assert FIELDS["business.other.annual_income"]["default_unit"] == "元"
    assert FIELDS["crop.main.yield"]["default_unit"] == "斤/亩"
    for field in FIELDS.values():
        assert "default_period" not in field and "default_subject" not in field
        assert "default_value" not in field
        assert field["period_requirement"]


def test_required_flags_preserve_when_applicable_scope_and_source_markers():
    for field in FIELDS.values():
        rows = [SOURCE_ROWS[q] for q in field["source_question_ids"]]
        assert field["required"] is any(r["required_raw"] == "是" for r in rows)
        assert field["required_scope"] == "source_marker_when_applicable_only_not_unconditional_business_rule"
        assert field["required_source_markers"] == [
            {"source_question_id": r["question_id"], "raw": r["required_raw"], "interpretation": r["required_interpretation"]}
            for r in rows
        ]
    assert FIELDS["cost.other"]["required"] is False
    assert FIELDS["cost.other"]["required_state"] == "source_optional"


def test_conditions_only_quote_actual_source_and_preserve_unresolved_child_triggers():
    count = 0
    for field in FIELDS.values():
        for condition in field["conditions"]:
            count += 1
            row = SOURCE_ROWS[condition["source_question_id"]]
            assert condition["source_question_id"] in field["source_question_ids"]
            originals = {(c["source"], c["text"]) for c in row["display_condition_texts"]}
            if condition["reason"] == "child_trigger_not_machine_defined":
                assert "child_trigger_not_machine_defined" in row["review_flags"]
                assert condition["text"] == row["question_type_raw"]
                assert condition["source_location"] == row["question_type_source"]
            elif condition["reason"] == "parent_relation_candidate_not_executable":
                assert row["parent_question_candidate"]
                assert condition["text"] == row["parent_relation_status"]
                assert condition["source_location"] == row["source_range"]
                assert condition["op"] == "unresolved"
                assert condition["evidence_kind"] == "parsed_parent_relation_candidate_not_source_rule"
            else:
                assert (condition["source_location"], condition["text"]) in originals
            assert condition["parent_question_id"] == row["parent_question_candidate"]
            parent = SOURCE_ROWS.get(condition["parent_question_id"])
            assert condition["parent_question_key"] == (parent["field_key_candidate"] if parent else None)
            assert condition["status"] == "candidate_not_executable"
            assert condition["executable"] is False
            if condition["op"] == "eq":
                assert condition["value"] in [o["label_candidate"] for o in parent["option_lines"]]
            else:
                assert condition["op"] == "unresolved"
    assert count == 61
    assert FIELDS["crop.main.area"]["conditions"], "A missing explicit trigger must not become unconditional"
    for field in FIELDS.values():
        guarded = {c["source_question_id"] for c in field["conditions"]}
        for source_id in field["source_question_ids"]:
            if SOURCE_ROWS[source_id]["parent_question_candidate"]:
                assert source_id in guarded, "Source-specific candidate parent links must not become unconditional"


def test_source_variant_parent_relationships_are_not_promoted_to_approved_rules():
    assert Counter(r["parent_relation_status"] for r in SOURCE_ROWS.values()) == {
        "candidate_from_serial_and_position_not_executable": 184, "not_established": 94,
    }
    assert all(not c["executable"] for f in FIELDS.values() for c in f["conditions"])
    assert QUESTIONNAIRE["condition_policy"]["candidate_or_unresolved_result"] == "unknown"
    assert QUESTIONNAIRE["condition_policy"]["source_answer_keys"]


def test_known_insurance_reference_gap_is_explicit_and_not_silently_claimed_as_integrated():
    gap = next(g for g in QUESTIONNAIRE["known_gaps"] if g.get("field_id") == "insurance.policies")
    assert gap["source_question_ids"] == ["Q-S01-R123", "Q-S01-R124", "Q-S01-R125", "Q-S01-R126"]
    projection = {p["source_question_id"]: p for p in QUESTIONNAIRE["source_projection"]}
    for source_id in gap["source_question_ids"]:
        assert source_id in SOURCE_ROWS
        assert source_id not in FIELDS["insurance.policies"]["source_question_ids"]
        assert projection[source_id]["handling"] == "extension_required"
    assert FIELDS["assets.financial"]["value_type"] == "unresolved"
    assert set(FIELDS["assets.financial"]["value_type_candidates"]) == {"object", "array"}


def test_every_candidate_has_a_locator_even_without_a_direct_question():
    for field in FIELDS.values():
        assert field["source_locations"]
        mapping = [r for r in field["source_locations"] if r["kind"] == "canonical_mapping"]
        assert len(mapping) == 1
        position = int(mapping[0]["location"].rsplit("/", 1)[1])
        assert CANONICAL["fields"][position]["canonical_id"] == field["field_id"]
        if not field["source_question_ids"]:
            assert not field["question_keys"]
            assert field["handling"] != "interview"


def test_coverage_manifest_matches_questionnaire_without_claiming_runtime_or_business_approval():
    coverage = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
    assert coverage["questionnaire_sha256"] == hashlib.sha256(QUESTIONNAIRE_PATH.read_bytes()).hexdigest()
    assert coverage["canonical_field_count"] == 54 and coverage["source_record_count"] == 278
    assert coverage["source_projection_complete"] is True
    assert coverage["source_hashes"] == QUESTIONNAIRE["source_hashes"]
    assert coverage["counts_by_handling"] == QUESTIONNAIRE["counts_by_handling"]
    assert {f["field_id"] for f in coverage["fields"]} == set(FIELDS)
    assert coverage["business_confirmation"] == "not_obtained"
    assert coverage["S2_status_not_changed"] is True
    assert all(f["technical_integration_status"] == "static_projection_only" for f in coverage["fields"])


@pytest.mark.parametrize("answer,expected", [
    ("是", "true"),
    ("否", "false"),
    (None, "unknown"),
    ({"value": "是", "presence": "known", "conflict": False, "needs_review": False}, "true"),
    ({"value": "否", "presence": "known", "conflict": False, "needs_review": False}, "false"),
    ({"value": "是", "presence": "unknown"}, "unknown"),
    ({"value": "是", "presence": "known", "conflict": True}, "unknown"),
    ({"value": "是", "presence": "known", "needs_review": True}, "unknown"),
    (["是"], "unknown"),
    (True, "unknown"),
])
def test_condition_evaluator_enabled_synthetic_rule_positive_negative_unknown(answer, expected):
    from due_app.diligence import evaluate_condition

    condition = {"parent_question_key": "SYNTHETIC.has_secondary", "op": "eq", "value": "是",
                 "status": "synthetic-test", "executable": True}
    assert evaluate_condition(condition, {"SYNTHETIC.has_secondary": answer}) == expected
    assert evaluate_condition(condition, {}) == "unknown"


@pytest.mark.parametrize("expected_value,answer,expected", [
    (0, 0, "true"),
    (0, 1, "false"),
    (0, {"value": 0, "presence": "explicit_zero", "conflict": False, "needs_review": False}, "true"),
    (0, None, "unknown"),
    (0, False, "unknown"),
    (False, False, "true"),
    (False, True, "false"),
])
def test_condition_evaluator_preserves_zero_boolean_and_unknown_types(expected_value, answer, expected):
    from due_app.diligence import evaluate_condition

    condition = {"parent_question_key": "SYNTHETIC.control", "op": "eq", "value": expected_value,
                 "status": "synthetic-test", "executable": True}
    assert evaluate_condition(condition, {"SYNTHETIC.control": answer}) == expected


@pytest.mark.parametrize("patch", [
    {"status": "candidate_not_executable"},
    {"status": "unresolved"},
    {"executable": False},
    {"executable": 1},
    {"executable": "true"},
    {"op": "unresolved"},
])
def test_condition_evaluator_does_not_promote_unapproved_or_ambiguous_rules(patch):
    from due_app.diligence import evaluate_condition

    condition = {"parent_question_key": "SYNTHETIC.control", "op": "eq", "value": "是",
                 "status": "synthetic-test", "executable": True, **patch}
    assert evaluate_condition(condition, {"SYNTHETIC.control": "是"}) == "unknown"


@pytest.mark.parametrize("source_answer", ["是", "否", None])
def test_every_real_candidate_condition_stays_unknown_for_positive_negative_or_missing_answers(source_answer):
    from due_app.diligence import evaluate_condition

    for field in FIELDS.values():
        for condition in field["conditions"]:
            answers = {condition["parent_question_key"]: source_answer} if condition["parent_question_key"] else {}
            assert evaluate_condition(condition, answers) == "unknown"
