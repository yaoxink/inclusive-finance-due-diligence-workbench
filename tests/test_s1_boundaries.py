"""Independent S1 boundary checks; temporary synthetic data only, no capture/network."""

from __future__ import annotations

import builtins
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap
import uuid

import pytest
from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = APP_ROOT.parent
API = "/api/due-diligence"
PROTECTED_ROOTS = (
    WORKSPACE / "meeting-demo" / "data",
    WORKSPACE / "meeting-demo" / "data-benchmark",
    WORKSPACE / "材料",
    WORKSPACE / "planning",
)


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    # main defines a default ASGI app at import time: isolate before importing it.
    monkeypatch.setenv("DUE_DATA_DIR", str(tmp_path / "import-default"))
    module = importlib.import_module("due_app.main")
    return module.create_app


@pytest.fixture
def client(app_factory, tmp_path):
    with TestClient(app_factory(data_root=tmp_path / "active-due")) as instance:
        yield instance


def create_case(client, title="S1合成农户案件"):
    response = client.post(API + "/cases", json={
        "title": title, "subject_type": "farmer", "product_id": "farmer_demo",
    })
    assert response.status_code == 201, response.text
    return response.json()


def create_interview(client, case_id, title="S1合成访谈"):
    response = client.post(API + f"/cases/{case_id}/interviews", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def test_case_creation_list_detail_and_interview_have_honest_s1_states(client):
    assert client.get(API + "/cases").json() == {"cases": []}
    case = create_case(client)
    assert case["title"] == "S1合成农户案件"
    assert case["subject_type"] == "farmer"
    assert case["product_id"] == "farmer_demo"
    assert case["status"] == "preparing"
    assert case["data_class"] == "synthetic"
    assert case["questionnaire_version"] == "farmer_candidate_v0.1"
    assert case["model_version"] is None
    assert isinstance(case["created_at"], str) and case["created_at"]
    assert isinstance(case["updated_at"], str) and case["updated_at"]
    response = client.get(API + f"/cases/{case['id']}")
    assert response.status_code == 200
    assert response.json()["interviews"] == []
    interview = create_interview(client, case["id"])
    assert interview["case_id"] == case["id"]
    assert interview["title"] == "S1合成访谈"
    assert interview["state"] == "ready"
    assert interview["capture_state"] == "not_implemented"
    assert interview["asr_state"] == "not_implemented"
    detail = client.get(API + f"/cases/{case['id']}").json()
    assert detail["interviews"] == [{**interview, "recordings": [], "active_recording_id": None}]
    own_interview = client.get(API + f"/cases/{case['id']}/interviews/{interview['id']}")
    assert own_interview.status_code == 200
    assert own_interview.json() == {**interview, "recordings": [], "active_recording_id": None}
    listing = client.get(API + "/cases")
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["cases"]] == [case["id"]]


def test_interviews_cannot_be_read_through_a_different_case(client):
    case_a, case_b = create_case(client, "合成甲案"), create_case(client, "合成乙案")
    interview_a = create_interview(client, case_a["id"], "只属于甲案")
    assert client.get(API + f"/cases/{case_b['id']}/interviews/{interview_a['id']}").status_code == 404
    assert client.get(API + f"/cases/{case_b['id']}").json()["interviews"] == []
    missing_case = "case_" + uuid.uuid4().hex
    assert client.post(API + f"/cases/{missing_case}/interviews", json={"title": "不能创建"}).status_code == 404
    assert client.get(API + f"/cases/{missing_case}/interviews/{interview_a['id']}").status_code == 404


@pytest.mark.parametrize("extra", [
    {"app_id": "meeting"}, {"data_root": "synthetic-other-root"},
    {"data_class": "real_business"}, {"model_version": "pretend-calibrated"},
])
def test_case_body_cannot_override_application_scope(client, extra):
    response = client.post(API + "/cases", json={
        "title": "合成越界请求", "subject_type": "farmer", "product_id": "farmer_demo", **extra,
    })
    assert response.status_code == 422
    assert client.get(API + "/cases").json() == {"cases": []}


@pytest.mark.parametrize("subject", ["enterprise", "individual_business", "all", ""])
def test_only_farmer_subject_is_enabled(client, subject):
    response = client.post(API + "/cases", json={
        "title": "合成范围请求", "subject_type": subject, "product_id": "farmer_demo",
    })
    assert response.status_code == 422


@pytest.mark.parametrize("extra", [{"app_id": "meeting"}, {"data_root": "other"}, {"case_id": "another-case"}])
def test_interview_body_cannot_override_path_or_scope(client, extra):
    case = create_case(client)
    response = client.post(API + f"/cases/{case['id']}/interviews", json={"title": "合成访谈", **extra})
    assert response.status_code == 422
    assert client.get(API + f"/cases/{case['id']}").json()["interviews"] == []


def test_separate_data_roots_are_isolated_and_explicit_factory_root_wins(app_factory, tmp_path, monkeypatch):
    root_a, root_b = tmp_path / "due-a", tmp_path / "due-b"
    monkeypatch.setenv("DUE_DATA_DIR", str(root_a))
    with TestClient(app_factory(data_root=root_a)) as client_a, TestClient(app_factory(data_root=root_b)) as client_b:
        case_a = create_case(client_a, "根A合成案件")
        interview_a = create_interview(client_a, case_a["id"])
        assert client_b.get(API + "/cases").json() == {"cases": []}
        assert client_b.get(API + f"/cases/{case_a['id']}").status_code == 404
        assert client_b.get(API + f"/cases/{case_a['id']}/interviews/{interview_a['id']}").status_code == 404
        case_b = create_case(client_b, "根B合成案件")
        assert [item["id"] for item in client_a.get(API + "/cases").json()["cases"]] == [case_a["id"]]
        assert [item["id"] for item in client_b.get(API + "/cases").json()["cases"]] == [case_b["id"]]
    with TestClient(app_factory(data_root=root_a)) as reopened:
        assert reopened.get(API + f"/cases/{case_a['id']}").json()["interviews"] == [{**interview_a, "recordings": [], "active_recording_id": None}]
        assert reopened.get(API + f"/cases/{case_b['id']}").status_code == 404


def test_synthetic_legacy_identifiers_are_not_due_cases(client, monkeypatch, tmp_path):
    # These are invented identifiers; no legacy database is opened.
    monkeypatch.setenv("MEETING_DATA_DIR", str(tmp_path / "unopened-legacy"))
    for legacy_id in ("meeting_" + uuid.uuid4().hex, "case_" + uuid.uuid4().hex):
        assert client.get(API + "/cases/" + legacy_id).status_code == 404
    assert client.get(API + "/cases").json() == {"cases": []}
    assert not (tmp_path / "unopened-legacy").exists()


@pytest.mark.parametrize("path", [
    "/api/meetings", "/api/benchmark", "/api/benchmark/models", "/api/upload",
    "/unimplemented-page.html", "/asr-benchmark", "/api/due-diligence/cases/%2e%2e%2fmeeting-demo%2fdata",
])
def test_old_or_unimplemented_routes_never_fall_back_to_another_app(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("host", ["evil.invalid", "localhost.evil.invalid", "127.0.0.1.evil.invalid", "0.0.0.0:8769"])
def test_nonlocal_host_is_rejected(client, host):
    assert client.get(API + "/runtime-policy", headers={"host": host}).status_code == 403


@pytest.mark.parametrize("origin", ["https://evil.invalid", "http://127.0.0.1:8767", "null"])
def test_cross_origin_including_meeting_origin_is_rejected(client, origin):
    assert client.get(API + "/cases", headers={"origin": origin}).status_code == 403


def test_same_origin_has_a_positive_control(client):
    assert client.get(API + "/runtime-policy", headers={"host": "127.0.0.1:8769", "origin": "http://127.0.0.1:8769"}).status_code == 200


def test_runtime_policy_ignores_injected_cloud_and_legacy_settings(client, monkeypatch, tmp_path):
    secret = "S1_SYNTHETIC_SECRET_NOT_A_REAL_CREDENTIAL"
    for name in ("MEETING_LLM_API_KEY", "MEETING_LLM_BASE_URL", "OPENAI_API_KEY", "LARK_APP_SECRET", "HF_TOKEN"):
        monkeypatch.setenv(name, secret)
    response = client.get(API + "/runtime-policy")
    assert response.status_code == 200
    assert secret not in response.text
    assert response.json() == {
        "app_id": "due-diligence", "local_only": True, "data_class": "synthetic",
        "external_services_enabled": False, "config_source": "due_app_only", "data_namespace": "due-diligence",
    }


def test_capabilities_do_not_claim_completed_recording_acceptance_or_calibrated_model(client):
    response = client.get(API + "/capabilities")
    assert response.status_code == 200
    entries = response.json()["capabilities"]
    assert len(entries) == len({item["id"] for item in entries}) == 9
    assert {item["id"]: item["status"] for item in entries} == {
        "cases": "ready", "capture": "under_verification", "asr": "under_verification",
        "previsit": "under_verification", "questionnaire": "under_verification", "facts": "under_verification",
        "followup": "under_verification", "model": "under_verification", "report": "under_verification",
    }


def _within(path, roots):
    if not isinstance(path, (str, os.PathLike)):
        return False
    resolved = Path(path).resolve()
    return any(resolved == root or root in resolved.parents for root in roots)


@pytest.mark.parametrize("base", PROTECTED_ROOTS)
@pytest.mark.parametrize("nested", [False, True])
def test_protected_roots_rejected_before_any_content_access(app_factory, monkeypatch, base, nested):
    forbidden = base / "S1-MUST-NOT-BE-CREATED" if nested else base
    roots = tuple(root.resolve() for root in PROTECTED_ROOTS)
    original_open, original_path_open, original_mkdir, original_connect = builtins.open, Path.open, Path.mkdir, sqlite3.connect

    def guard(path):
        if _within(path, roots):
            raise AssertionError("protected data was accessed before configuration rejection")

    def safe_open(path, *args, **kwargs):
        guard(path)
        return original_open(path, *args, **kwargs)

    def safe_path_open(path, *args, **kwargs):
        guard(path)
        return original_path_open(path, *args, **kwargs)

    def safe_mkdir(path, *args, **kwargs):
        guard(path)
        return original_mkdir(path, *args, **kwargs)

    def safe_connect(path, *args, **kwargs):
        guard(path)
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", safe_open)
    monkeypatch.setattr(Path, "open", safe_path_open)
    monkeypatch.setattr(Path, "mkdir", safe_mkdir)
    monkeypatch.setattr(sqlite3, "connect", safe_connect)
    # Guard failures are AssertionError, not accepted as the required ValueError.
    with pytest.raises(ValueError, match="隔离"):
        app_factory(data_root=forbidden)


def test_import_in_clean_process_never_loads_legacy_modules_or_initializes_legacy_data(tmp_path):
    legacy_data, legacy_env = tmp_path / "legacy-data-must-not-exist", tmp_path / "legacy.env"
    legacy_env.write_text("MEETING_LLM_API_KEY=S1_FAKE_ONLY\n", encoding="utf-8")
    code = textwrap.dedent("""
        import importlib.abc, json, os, pathlib, sys
        workspace, due_root, legacy_data, legacy_env = map(pathlib.Path, sys.argv[1:])
        sys.path.insert(0, str(workspace / 'meeting-demo'))
        sys.path.insert(0, str(due_root))
        forbidden = {'backend.app', 'backend.db', 'backend.config'}
        attempted = []
        class NoLegacy(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname in forbidden:
                    attempted.append(fullname)
                    raise AssertionError('legacy import attempted: ' + fullname)
        sys.meta_path.insert(0, NoLegacy())
        protected = [workspace/'meeting-demo'/'data', legacy_data, legacy_env, workspace/'meeting-demo'/'.env']
        def audit(event, args):
            if event not in ('open', 'os.mkdir') or not args or not isinstance(args[0], (str, bytes, os.PathLike)):
                return
            raw = os.fsdecode(args[0])
            target = pathlib.Path(raw).resolve()
            if any(target == p.resolve() or p.resolve() in target.parents for p in protected):
                raise AssertionError('legacy data/config I/O attempted')
        sys.addaudithook(audit)
        import due_app.main
        assert not attempted
        assert not (forbidden & set(sys.modules))
        assert not legacy_data.exists()
        print(json.dumps({'legacy_imports': [], 'legacy_data_initialized': False}))
    """)
    environment = os.environ.copy()
    environment.update(DUE_DATA_DIR=str(tmp_path / "due-import"), MEETING_DATA_DIR=str(legacy_data),
                       MEETING_ENV_FILE=str(legacy_env), PYTHONDONTWRITEBYTECODE="1",
                       MEETING_LLM_API_KEY="S1_FAKE_ONLY", OPENAI_API_KEY="S1_FAKE_ONLY")
    result = subprocess.run([sys.executable, "-B", "-c", code, str(WORKSPACE), str(APP_ROOT), str(legacy_data), str(legacy_env)],
                            cwd=str(APP_ROOT), env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"legacy_imports": [], "legacy_data_initialized": False}
    assert not legacy_data.exists()
    assert legacy_env.read_text(encoding="utf-8") == "MEETING_LLM_API_KEY=S1_FAKE_ONLY\n"
