"""S3 domain invariants against isolated synthetic storage; no devices or models."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "shared-recording-core"))

from due_app.config import APP_ID
from due_app.diligence import DiligenceError, DiligenceService, evaluate_condition
from due_app.recordings import RecordingRepository
from due_app.store import DueStore, now
from shared_recording_core import SessionConfig
from shared_recording_core.archive import ArchiveWriter


class SyntheticRecordings:
    def __init__(self, store):
        self.repository = RecordingRepository(store)

    def get(self, case_id, interview_id, recording_id):
        return self.repository.get(case_id, interview_id, recording_id)["snapshot"]


@pytest.fixture
def env(tmp_path):
    store = DueStore(tmp_path / "synthetic-only")
    recordings = SyntheticRecordings(store)
    return SimpleNamespace(store=store, recordings=recordings,
                           service=DiligenceService(store, recordings))


def make_case(env, fixture="SYN-S0-001", *, bind=True):
    cid = env.store.create_case("S3隔离合成", "farmer", "farmer_demo")["id"]
    if bind:
        env.service.load_previsit(cid, fixture, "base")
    return cid


def source(env, cid, text="我们家有四口人。", role="applicant"):
    return env.service.create_statement(cid, {"text": text, "speaker_role": role})


def values(statement, **changes):
    return {"field_id": "household.member_count", "statement_id": statement["id"],
            "statement_revision": statement["revision"], "value_type": "integer", "value": "4",
            "presence": "known", "unit": "人", "unit_status": "explicit",
            "period": {"kind": "as_of", "label": "2026-10-30", "start": "2026-10-30", "end": "2026-10-30"},
            "period_status": "explicit", "subject_id": "C1-HOUSEHOLD", "subject_status": "explicit",
            "subject_basis": "合成原话明确为申请人家庭", **changes}


def raised(code, fn, *args):
    with pytest.raises(DiligenceError) as exc:
        fn(*args)
    assert exc.value.code == code
    return exc.value


def confirmed_fact(env, cid, payload=None, *, role="applicant"):
    statement = source(env, cid, role=role)
    fact = env.service.create_fact(cid, values(statement, **(payload or {})))
    env.service.confirm_statement(cid, statement["id"], 1)
    return statement, env.service.confirm_fact(cid, fact["id"], 1, "合成人工复核")


def archive(env, cid, *, app_id=APP_ID, session_id=None, output_root=None):
    interview = env.store.create_interview(cid, "无麦克风合成存档")
    rid = "recording_synthetic"
    config = SessionConfig(app_id=app_id, instance_id="synthetic-instance",
                           session_id=session_id or rid,
                           output_root=output_root or (env.store.root / "recordings").resolve(),
                           device_id=1, capture_format={"sample_rate": 16000, "channels": 1, "sample_width": 2},
                           asr_config={"engine": "disabled"})
    writer = ArchiveWriter(config, "synthetic-run", source="synthetic")
    writer.started()
    writer.commit(b"\x01\x00" * 16000)
    writer.close()
    segment = {"id": "synthetic-segment", "text": "合成原话：四口人。", "revision": 1,
               "stable": True, "start_seconds": 0, "end_seconds": 1, "source_run_id": "synthetic-run"}
    snapshot = {"id": rid, "case_id": cid, "interview_id": interview["id"], "created_at": now(),
                "saved_frames": 16000, "segments": [segment], "data_error": None}
    env.recordings.repository.create(config.to_dict(), snapshot)
    env.recordings.repository.save_segment(rid, segment, "synthetic-run")
    request = {"recording_id": rid, "interview_id": interview["id"],
               "segment_id": segment["id"], "segment_revision": 1}
    return config, request


@pytest.mark.parametrize("number", range(1, 6))
def test_all_fixtures_preserve_unknown_and_frozen_identity(env, number):
    cid = make_case(env, f"SYN-S0-00{number}")
    state = env.service.workspace(cid)
    assert state["previsit"]["state"] == "available"
    assert len(state["previsit"]["facts"]) == 20
    assert state["questionnaire"]["frozen"] is True
    assert state["questionnaire"]["progress"]["interview_count"] == 34  # +5 answer-driven calculation questions (2026-11-05)
    assert state["questionnaire"]["progress"]["unknown"] == 57
    assert state["credit"]["effective_limit"] is None
    assert all(f["verification"] != "verified" for f in state["facts"])
    if number == 2:
        facts = {f["field_id"]: f for f in state["facts"]}
        assert facts["cost.seed"]["presence"] == "missing" and facts["cost.seed"]["value"] is None
        assert facts["cost.fertilizer"]["unit_status"] == "unknown" and facts["cost.fertilizer"]["unit"] is None
    zero = next(f for f in state["facts"] if f["field_id"] == "household.dependent_count")
    assert zero["presence"] == "explicit_zero" and zero["value"] == "0"


def test_questionnaire_is_frozen_per_case_and_survives_restart(env):
    old = make_case(env)
    frozen = env.service.workspace(old)["questionnaire"]
    untouched = make_case(env, bind=False)
    assert env.store.case(untouched)["questionnaire_version"] == "farmer_candidate_v0.1"
    env.service.questionnaire = copy.deepcopy(env.service.questionnaire)
    env.service.questionnaire["version"] = "synthetic-future-catalog"
    env.service.questionnaire["fields"][0]["question_text"] = "未来测试问题"
    assert env.service.workspace(old)["questionnaire"] == frozen
    assert env.service.workspace(untouched)["questionnaire"]["frozen"] is False
    newer = make_case(env)
    assert env.service.workspace(newer)["questionnaire"]["version"] == "synthetic-future-catalog"
    assert env.store.case(old)["questionnaire_version"] == frozen["version"]
    restarted = DiligenceService(DueStore(env.store.root), env.recordings)
    assert restarted.workspace(old)["questionnaire"] == frozen


def test_denied_is_latched_until_explicit_available_context(env):
    cid = make_case(env)
    statement, fact = confirmed_fact(env, cid)
    snapshot = env.service.workspace(cid)["previsit"]["snapshot_id"]
    for variant in ("denied", "unavailable"):
        state = env.service.load_previsit(cid, "SYN-S0-001", variant)
        assert state["previsit"]["state"] == "denied"
        assert state["facts"] == [] and state["statements"] == []
        assert state["previsit"]["parties"] == [] and state["previsit"]["history"] == []
        for fn, args in ((env.service.previsit_snapshot, (cid, snapshot)),
                         (env.service.statement_history, (cid, statement["id"])),
                         (env.service.fact_history, (cid, fact["id"]))):
            assert raised("previsit_access_denied", fn, *args).status == 403
    restored = env.service.load_previsit(cid, "SYN-S0-001", "base")
    assert any(f["id"] == fact["id"] for f in restored["facts"])


def test_binding_and_cross_case_history_do_not_follow_names(env):
    a, b = make_case(env), make_case(env)
    statement, fact = confirmed_fact(env, a)
    raised("binding_conflict", env.service.load_previsit, a, "SYN-S0-002", "base")
    for fn, entity in ((env.service.statement_history, statement["id"]), (env.service.fact_history, fact["id"])):
        assert raised("entity_not_found", fn, b, entity).status == 404
    raised("entity_not_found", env.service.create_fact, b, values(statement))


@pytest.mark.parametrize("mutation,code", [
    (lambda c: c["permission"].update(read_originals=True), "prohibited_permission"),
    (lambda c: c["permission"].update(external_transfer=True), "prohibited_permission"),
    (lambda c: c["permission"].update(read_fields=False), "previsit_access_denied"),
    (lambda c: c["facts"][0].update(subject_id="another-case-subject"), "invalid_context_reference"),
    (lambda c: c["business_units"][0].update(operator_party_id="another-case-applicant"), "invalid_context_reference"),
])
def test_context_permissions_and_internal_references(env, mutation, code):
    context = copy.deepcopy(env.service.fixtures["SYN-S0-001"]["previsit_context"])
    mutation(context)
    raised(code, env.service.validate_context, context)


def test_record_accuracy_is_separate_from_factual_verification(env):
    cid = make_case(env)
    statement = source(env, cid, text="  我家四口人。\n这是合成原文。  ")
    fact = env.service.create_fact(cid, values(statement))
    assert statement["text"] == "  我家四口人。\n这是合成原文。  "
    assert statement["has_audio"] is False and statement["source_ref"] is None
    raised("source_confirmation_required", env.service.confirm_fact, cid, fact["id"], 1)
    env.service.confirm_statement(cid, statement["id"], 1)
    assert env.service.fact(cid, fact["id"])["record_confirmation"] == "pending"
    confirmed = env.service.confirm_fact(cid, fact["id"], 1)
    assert confirmed["revision"] == 2 and confirmed["record_confirmation"] == "record_accurate"
    assert confirmed["verification"] == "not_checked"
    revoked = env.service.revoke_fact(cid, fact["id"], 2, "合成撤回")
    raised("revoked_requires_revision", env.service.confirm_fact, cid, fact["id"], revoked["revision"])
    assert [v["record_confirmation"] for v in env.service.fact_history(cid, fact["id"])["versions"]] == ["pending", "record_accurate", "revoked"]


def test_revision_and_context_updates_invalidate_dependents_without_overwriting(env):
    cid = make_case(env, "SYN-S0-005")
    statement, fact = confirmed_fact(env, cid, {"subject_id": "C5-HOUSEHOLD"})
    revised = env.service.revise_statement(cid, statement["id"], {"expected_revision": 1, "text": "合成更正：五口人。"})
    assert revised["record_confirmation"] == "pending"
    assert env.service.fact(cid, fact["id"])["needs_review"] is True
    raised("revision_conflict", env.service.revise_statement, cid, statement["id"], {"expected_revision": 1, "text": "过期写入"})
    changed = env.service.load_previsit(cid, "SYN-S0-005", "updated")
    assert changed["previsit"]["context_version"] == 2
    old_snapshot = env.service.previsit_history(cid)["versions"][0]["snapshot_id"]
    assert env.service.previsit_snapshot(cid, old_snapshot)["context_version"] == 1
    assert env.service.fact_history(cid, fact["id"])["versions"][1]["value"] == "4"
    raised("version_regression", env.service.load_previsit, cid, "SYN-S0-005", "base")


@pytest.mark.parametrize("role", ["unknown", "household_member", "investigator", "third_party"])
def test_other_speakers_cannot_qualify_as_applicant(env, role):
    cid = make_case(env, "SYN-S0-004")
    _, fact = confirmed_fact(env, cid, {"field_id": "crop.main.experience_years", "value": "5",
                                      "unit": "年", "subject_id": "C4-MAIN"}, role=role)
    assert fact["calculation_eligibility"] == "blocked_subject"


def test_applicant_speech_about_third_party_keeps_subject_out_of_scope(env):
    cid = make_case(env, "SYN-S0-004")
    _, fact = confirmed_fact(env, cid, {"field_id": "crop.main.experience_years", "value": "5",
                                      "unit": "年", "subject_id": "C4-THIRD",
                                      "subject_basis": "申请人明确说邻居的情况"})
    assert fact["subject_id"] == "C4-THIRD"
    assert fact["calculation_eligibility"] == "blocked_subject"


@pytest.mark.parametrize("changes,code", [
    ({"value": 4}, "invalid_value"),
    ({"value": "四"}, "invalid_integer"),
    ({"presence": "missing", "value": "0"}, "invalid_presence_value"),
    ({"presence": "explicit_zero", "value": "4"}, "invalid_zero"),
    ({"unit_status": "unknown", "unit": "人"}, "unit_status_mismatch"),
    ({"period_status": "unknown"}, "period_status_mismatch"),
    ({"subject_id": "C2-HOUSEHOLD"}, "subject_not_in_case"),
    ({"period": {"kind": "year", "label": "坏日期", "start": "2026-02-30", "end": None}}, "invalid_period"),
])
def test_candidate_values_do_not_guess_units_periods_or_numbers(env, changes, code):
    cid = make_case(env)
    statement = source(env, cid)
    raised(code, env.service.create_fact, cid, values(statement, **changes))


def test_missing_aspects_have_consistent_unknown_defaults_in_domain_calls(env):
    cid = make_case(env)
    statement = source(env, cid)
    payload = values(statement)
    for key in ("unit", "unit_status", "period", "period_status", "subject_id", "subject_status", "subject_basis"):
        payload.pop(key)
    fact = env.service.create_fact(cid, payload)
    assert fact["unit"] is None and fact["unit_status"] == "unknown"
    assert fact["period"] is None and fact["period_status"] == "unknown"
    assert fact["subject_id"] is None and fact["subject_status"] == "unknown"


@pytest.mark.parametrize("missing", ["manifest.json", "chunks/00000001.wav"])
def test_committed_audio_loss_invalidates_source_and_confirmation(env, missing):
    cid = make_case(env)
    config, request = archive(env, cid)
    statement = env.service.statement_from_segment(cid, request)
    config.resolve_output(missing).unlink()
    assert env.service.statement(cid, statement["id"])["needs_review"] is True
    raised("source_changed", env.service.confirm_statement, cid, statement["id"], 1)


@pytest.mark.parametrize("changes", [{"app_id": "other-app"}, {"session_id": "other-session"}, {"external_root": True}])
def test_archive_context_must_match_application_session_and_root(env, changes, tmp_path):
    cid = make_case(env)
    options = dict(changes)
    if options.pop("external_root", False):
        options["output_root"] = (tmp_path / "other-app-recordings").resolve()
    _, request = archive(env, cid, **options)
    raised("source_unavailable", env.service.statement_from_segment, cid, request)


def test_asr_is_server_owned_and_old_segment_revision_cannot_confirm(env):
    cid = make_case(env)
    _, request = archive(env, cid)
    statement = env.service.statement_from_segment(cid, {**request, "text": "客户端伪造正文"})
    assert statement["text"] == "合成原话：四口人。"
    raised("asr_source_read_only", env.service.revise_statement, cid, statement["id"], {"expected_revision": 1, "text": "改写原文"})
    persisted = env.recordings.repository.get(cid, request["interview_id"], request["recording_id"])
    segment = persisted["snapshot"]["segments"][0]
    segment.update(text="合成修订：五口人。", revision=2)
    env.recordings.repository.save_segment(request["recording_id"], segment, "synthetic-run")
    with env.store.connect() as connection:
        connection.execute("UPDATE recordings SET snapshot_json=? WHERE id=?", (json.dumps(persisted["snapshot"]), request["recording_id"]))
    assert env.service.statement(cid, statement["id"])["needs_review"] is True
    raised("source_changed", env.service.confirm_statement, cid, statement["id"], 1)
    refreshed = env.service.refresh_segment(cid, statement["id"], 1, 2)
    assert refreshed["revision"] == 2 and refreshed["text"] == "合成修订：五口人。"


def test_confirmed_facts_and_source_history_survive_restart(env):
    cid = make_case(env)
    statement, fact = confirmed_fact(env, cid)
    restarted_store = DueStore(env.store.root)
    restarted = DiligenceService(restarted_store, SyntheticRecordings(restarted_store))
    saved = restarted.fact(cid, fact["id"])
    assert saved["record_confirmation"] == "record_accurate" and saved["verification"] == "not_checked"
    assert restarted.statement_history(cid, statement["id"])["versions"][0]["text"] == statement["text"]
    assert [v["revision"] for v in restarted.fact_history(cid, fact["id"])["versions"]] == [1, 2]


def test_two_competing_fact_revisions_cannot_overwrite_each_other(env):
    cid = make_case(env)
    statement = source(env, cid)
    fact = env.service.create_fact(cid, values(statement))

    def revise(value):
        payload = values(statement, value=value, expected_revision=1)
        payload.pop("field_id")
        try:
            return env.service.revise_fact(cid, fact["id"], payload)["revision"]
        except DiligenceError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(revise, ["5", "6"]))
    assert sorted(outcomes, key=str) == [2, "revision_conflict"]
    assert len(env.service.fact_history(cid, fact["id"])["versions"]) == 2


def test_upstream_conflict_retains_both_values_and_blocks_calculation(env):
    cid = make_case(env, "SYN-S0-003")
    _, fact = confirmed_fact(env, cid, {"field_id": "crop.main.area", "value_type": "decimal", "value": "35",
                                      "unit": "亩", "subject_id": "C3-MAIN"})
    state = env.service.workspace(cid)
    assert {f["value"] for f in state["facts"] if f["field_id"] == "crop.main.area"} == {"32", "35"}
    assert fact["conflict"] is True and fact["calculation_eligibility"] == "blocked_conflict"
    assert any(c["field_id"] == "crop.main.area" and "value" in c["kinds"] for c in state["conflicts"])


def test_source_refresh_never_calls_recording_manager_under_store_lock(env):
    cid = make_case(env)
    _, request = archive(env, cid)
    statement = env.service.statement_from_segment(cid, request)
    get = env.recordings.get
    lock_observations = []

    def check_lock(*args):
        lock_observations.append(env.store.lock._is_owned())
        return get(*args)

    env.recordings.get = check_lock
    assert env.service.statement(cid, statement["id"])["needs_review"] is False
    assert lock_observations and not any(lock_observations)
    env.service.load_previsit(cid, "SYN-S0-001", "denied")
    denied_reads = []

    def forbidden(*args):
        denied_reads.append(args)
        raise AssertionError("denied source content must not be read")

    env.recordings.get = forbidden
    assert env.service.workspace(cid)["statements"] == []
    assert denied_reads == []


@pytest.mark.parametrize("answer", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_condition_values_remain_unknown(answer):
    condition = {"executable": True, "status": "synthetic-test", "op": "eq", "parent_question_key": "q", "value": answer}
    assert evaluate_condition(condition, {"q": answer}) == "unknown"
