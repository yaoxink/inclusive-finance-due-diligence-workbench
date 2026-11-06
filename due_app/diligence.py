"""Synthetic-only S3 domain: source text, versioned candidates and review state."""
from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

from jsonschema import Draft7Validator, FormatChecker

from .config import APP_ID
from .store import now

DATA = Path(__file__).resolve().parent / "data"
ACTIVE_QUESTIONNAIRE_FILENAME = "questionnaire_v2_main_crop.json"
MAIN_CROP_CATEGORY_CONTRACT = "explicit-main-crop-scale-category/1"
MAIN_CROP_CATEGORIES = {"fruit", "grain", "herb", "other"}
KNOWN = {"known", "explicit_zero"}
DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
INTEGER = re.compile(r"^-?(?:0|[1-9][0-9]*)$")


def evaluate_condition(condition, source_answers):
    """Conservative tri-state evaluation; source candidates are never executable."""
    if (not isinstance(condition, dict) or condition.get("executable") is not True
            or condition.get("status") not in {"approved", "synthetic-test"}
            or condition.get("op") != "eq" or not isinstance(source_answers, dict)):
        return "unknown"
    key = condition.get("parent_question_key")
    if not isinstance(key, str) or key not in source_answers or "value" not in condition:
        return "unknown"
    answer = source_answers[key]
    if isinstance(answer, dict):
        if (answer.get("presence", "known") not in KNOWN or answer.get("conflict")
                or answer.get("needs_review") or answer.get("state") in {"unknown", "conflict", "pending", "revoked"}):
            return "unknown"
        answer = answer.get("value")
    expected = condition["value"]
    if answer is None or expected is None or isinstance(answer, (list, dict)) or type(answer) is not type(expected):
        return "unknown"
    if not isinstance(answer, (str, bool, int, float)):
        return "unknown"
    if isinstance(answer, float) and (not math.isfinite(answer) or not math.isfinite(expected)):
        return "unknown"
    return "true" if answer == expected else "false"


class DiligenceError(ValueError):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def error(status, code, message):
    raise DiligenceError(status, code, message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_id(prefix):
    return prefix + "_" + uuid.uuid4().hex


class DiligenceService:
    def __init__(self, store, recordings, *, catalog_filename=None):
        self.store, self.recordings = store, recordings
        self.schema = json.loads((DATA / "previsit_schema_s0.json").read_text(encoding="utf-8-sig"))
        Draft7Validator.check_schema(self.schema)
        self.validator = Draft7Validator(self.schema, format_checker=FormatChecker())
        bundle = json.loads((DATA / "synthetic_cases_s0.json").read_text(encoding="utf-8-sig"))
        self.fixtures = {item["case_id"]: item for item in bundle["cases"]}
        # catalog injection keeps tests and staged version upgrades explicit;
        # production behaviour is unchanged when omitted.
        self.catalog_filename = catalog_filename or ACTIVE_QUESTIONNAIRE_FILENAME
        catalog_bytes = (DATA / self.catalog_filename).read_bytes()
        self.questionnaire = json.loads(catalog_bytes.decode("utf-8-sig"))
        self.catalog_source_sha256 = hashlib.sha256(catalog_bytes).hexdigest()
        self.fields = {field["field_id"]: field for field in self.questionnaire["fields"]}
        with store.lock, store.connect() as connection:
            connection.executescript('''
              CREATE TABLE IF NOT EXISTS s3_bindings (
                case_id TEXT PRIMARY KEY REFERENCES cases(id),fixture_id TEXT NOT NULL,
                identity_json TEXT NOT NULL,access_state TEXT NOT NULL,active_snapshot_id TEXT,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS s3_previsit_versions (
                case_id TEXT NOT NULL REFERENCES cases(id),snapshot_id TEXT NOT NULL,
                context_version INTEGER NOT NULL,variant TEXT NOT NULL,loaded_at TEXT NOT NULL,
                context_json TEXT NOT NULL,PRIMARY KEY(case_id,snapshot_id));
              CREATE TABLE IF NOT EXISTS s3_statements (
                id TEXT NOT NULL,case_id TEXT NOT NULL REFERENCES cases(id),revision INTEGER NOT NULL,
                payload_json TEXT NOT NULL,PRIMARY KEY(id,revision));
              CREATE INDEX IF NOT EXISTS s3_statement_case ON s3_statements(case_id,id,revision);
              CREATE TABLE IF NOT EXISTS s3_facts (
                id TEXT NOT NULL,case_id TEXT NOT NULL REFERENCES cases(id),revision INTEGER NOT NULL,
                payload_json TEXT NOT NULL,PRIMARY KEY(id,revision));
              CREATE INDEX IF NOT EXISTS s3_fact_case ON s3_facts(case_id,id,revision);
              CREATE TABLE IF NOT EXISTS s3_actions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,case_id TEXT NOT NULL REFERENCES cases(id),
                entity_id TEXT NOT NULL,action TEXT NOT NULL,at TEXT NOT NULL,payload_json TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS s3_questionnaire_snapshots (
                case_id TEXT PRIMARY KEY REFERENCES cases(id),version TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,source_sha256 TEXT NOT NULL,frozen_at TEXT NOT NULL,catalog_json TEXT NOT NULL);
            ''')

    def examples(self):
        return {"examples": [{"fixture_id": item["case_id"], "scenario": item["scenario"],
                             "label": item["case_id"] + " · " + item["scenario"], "description": item["description"]}
                            for item in self.fixtures.values()]}

    def _case(self, connection, case_id):
        row = connection.execute("SELECT * FROM cases WHERE id=? AND app_id=?", (case_id, APP_ID)).fetchone()
        if row is None:
            error(404, "case_not_found", "本尽调应用中不存在该案件。")
        if row["data_class"] != "synthetic" or row["subject_type"] != "farmer" or row["product_id"] != "farmer_demo":
            error(409, "case_scope_changed", "本分支仅允许单农户合成案件，主体或产品范围不符。")
        return row

    @staticmethod
    def _identity(context):
        return {key: context[key] for key in ("case_id", "upstream_case_id", "applicant_party_id", "product_id")}

    def validate_context(self, context, *, expected=None):
        if not isinstance(context, dict):
            error(422, "invalid_context", "事前上下文必须为JSON对象。")
        permission = context.get("permission")
        if not isinstance(permission, dict):
            error(422, "invalid_context", "权限必须为冻结契约中的对象。")
        if permission.get("read_originals") is not False or permission.get("write_upstream") is not False or permission.get("external_transfer") is not False:
            error(403, "prohibited_permission", "本分支不读取原件、不写回上游且不外传。")
        if permission.get("read_fields") is not True or permission.get("read_material_references") is not True:
            error(403, "previsit_access_denied", "当前没有读取事前结构化资料的权限。")
        errors = sorted(self.validator.iter_errors(context), key=lambda item: str(list(item.path)))
        if errors:
            error(422, "invalid_context", "合成上下文不符合冻结S0契约。")
        if expected is not None:
            identity = self._identity(expected) if "permission" in expected else expected
            if any(context.get(key) != value for key, value in identity.items()):
                error(409, "binding_conflict", "案件、上游案件、主体或产品身份与已有绑定不一致。")
        parties = [item["party_id"] for item in context["parties"]]
        households = [item["household_id"] for item in context["households"]]
        units = [item["business_unit_id"] for item in context["business_units"]]
        ids = parties + households + units
        if len(ids) != len(set(ids)) or not any(p["party_id"] == context["applicant_party_id"] and p["role"] == "applicant" for p in context["parties"]):
            error(422, "invalid_context_reference", "合成主体ID必须唯一且申请人必须明确。")
        material_ids = [item["material_id"] for item in context["materials"]]
        fact_ids = [item["fact_id"] for item in context["facts"]]
        if len(material_ids) != len(set(material_ids)) or len(fact_ids) != len(set(fact_ids)):
            error(422, "invalid_context_reference", "合成材料或事实ID重复。")
        if any(any(p not in parties for p in item["member_party_ids"]) for item in context["households"]) or any(item["operator_party_id"] not in parties for item in context["business_units"]):
            error(422, "invalid_context_reference", "家庭或经营单元引用了不存在的主体。")
        for fact in context["facts"]:
            if fact["subject_id"] is not None and fact["subject_id"] not in ids:
                error(422, "invalid_context_reference", "事实主体不在本上下文范围内。")
            if any(source["material_id"] is not None and source["material_id"] not in material_ids for source in fact["source_refs"]):
                error(422, "invalid_context_reference", "事实引用了不存在的材料元数据。")
        return copy.deepcopy(context)

    def _binding(self, connection, case_id):
        return connection.execute("SELECT * FROM s3_bindings WHERE case_id=?", (case_id,)).fetchone()

    def _access(self, connection, case_id):
        self._case(connection, case_id)
        binding = self._binding(connection, case_id)
        if binding is not None and binding["access_state"] == "denied":
            error(403, "previsit_access_denied", "当前读取权限已拒绝，历史与来源内容也不可旁路读取。")
        return binding

    def _context(self, connection, case_id, binding=None):
        binding = binding or self._binding(connection, case_id)
        if binding is None or not binding["active_snapshot_id"]:
            return None
        row = connection.execute("SELECT context_json FROM s3_previsit_versions WHERE case_id=? AND snapshot_id=?",
                                 (case_id, binding["active_snapshot_id"])).fetchone()
        return json.loads(row[0]) if row else None

    def _action(self, connection, case_id, entity_id, action, payload):
        connection.execute("INSERT INTO s3_actions(case_id,entity_id,action,at,payload_json) VALUES (?,?,?,?,?)",
                           (case_id, entity_id, action, now(), encoded(payload)))

    def load_previsit(self, case_id, fixture_id, variant):
        if fixture_id not in self.fixtures or variant not in {"base", "updated", "unavailable", "denied"}:
            error(422, "invalid_fixture", "只能选择预定义合成示例和提供者状态。")
        context = self.validate_context(self.fixtures[fixture_id]["previsit_context"])
        identity = self._identity(context)
        if variant == "updated":
            context["context_version"] += 1
            context["snapshot_id"] += "-UPDATED"
            for fact in context["facts"]:
                if fact["field_id"] == "crop.main.area" and fact["value"] is not None:
                    fact["value"] = str(Decimal(fact["value"]) + 1)
                    fact["revision"] += 1
                    fact["raw_text"] = "合成上游版本更新：主营面积=" + fact["value"] + "亩"
                for source in fact["source_refs"]:
                    source["version"] = "fixture-v2"
            self.validate_context(context, expected=identity)
        with self.store.lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._case(connection, case_id)
            binding = self._binding(connection, case_id)
            if binding and (binding["fixture_id"] != fixture_id or json.loads(binding["identity_json"]) != identity):
                error(409, "binding_conflict", "本案已经绑定其他合成案件、主体或产品，不能换案。")
            active = binding["active_snapshot_id"] if binding else None
            state = "available" if variant in {"base", "updated"} else variant
            if binding and binding["access_state"] == "denied" and state == "unavailable":
                state = "denied"  # transport failure never grants a previously denied permission
            if state == "available":
                if binding and active:
                    previous = self._context(connection, case_id, binding)
                    if context["context_version"] < previous["context_version"]:
                        error(409, "version_regression", "不能用旧来源版本覆盖当前新版本。")
                active = context["snapshot_id"]
                existing = connection.execute("SELECT context_json FROM s3_previsit_versions WHERE case_id=? AND snapshot_id=?", (case_id, active)).fetchone()
                if existing and existing[0] != encoded(context):
                    error(409, "snapshot_identity_conflict", "同一快照ID不可对应不同正文。")
                connection.execute("INSERT OR IGNORE INTO s3_previsit_versions VALUES (?,?,?,?,?,?)",
                                   (case_id, active, context["context_version"], variant, now(), encoded(context)))
            if binding:
                connection.execute("UPDATE s3_bindings SET access_state=?,active_snapshot_id=?,updated_at=? WHERE case_id=?",
                                   (state, active, now(), case_id))
            else:
                connection.execute("INSERT INTO s3_bindings VALUES (?,?,?,?,?,?,?)",
                                   (case_id, fixture_id, encoded(identity), state, active, now(), now()))
            self._case_catalog(connection, case_id, freeze=True)
            self._action(connection, case_id, active or fixture_id, "previsit_" + variant, {"fixture_id": fixture_id, "snapshot_id": active})
        return self.workspace(case_id)

    def _case_catalog(self, connection, case_id, *, freeze=False):
        row = connection.execute("SELECT * FROM s3_questionnaire_snapshots WHERE case_id=?", (case_id,)).fetchone()
        if row is None and freeze:
            text = encoded(self.questionnaire)
            connection.execute("INSERT INTO s3_questionnaire_snapshots VALUES (?,?,?,?,?,?)",
                               (case_id, self.questionnaire["version"], text_hash(text), self.catalog_source_sha256, now(), text))
            connection.execute("UPDATE cases SET questionnaire_version=?,updated_at=? WHERE id=? AND app_id=?",
                               (self.questionnaire["version"], now(), case_id, APP_ID))
            row = connection.execute("SELECT * FROM s3_questionnaire_snapshots WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            return self.questionnaire, {"frozen": False, "content_sha256": None, "source_sha256": None, "frozen_at": None}
        catalog = json.loads(row["catalog_json"])
        if text_hash(encoded(catalog)) != row["content_sha256"] or catalog["version"] != row["version"]:
            error(409, "catalog_snapshot_invalid", "本案冻结题库快照无法核对，请保留数据后复核。")
        return catalog, {"frozen": True, "content_sha256": row["content_sha256"], "source_sha256": row["source_sha256"], "frozen_at": row["frozen_at"]}

    # ---------- master questionnaire administration ----------

    def update_questionnaire_fields(self, updates):
        """Admin adjustment of required/disabled flags on the master catalog.
        The file is rewritten atomically, the version gains an incremental _rN
        suffix, and the in-memory catalog reloads — new cases freeze the new
        content; existing cases keep their snapshots until
        apply_questionnaire_to_cases() runs."""
        import os as _os
        if not isinstance(updates, dict) or not updates:
            error(422, 'no_valid_updates', '没有可应用的题目调整。')
        unknown = set(updates) - set(self.fields)
        if unknown:
            error(422, 'unknown_field', '以下题目不在题库中：' + '、'.join(sorted(unknown)[:5]))
        catalog = json.loads(json.dumps(self.questionnaire))
        changed = set()
        for field in catalog['fields']:
            patch = updates.get(field['field_id'])
            if not isinstance(patch, dict):
                continue
            if isinstance(patch.get('required'), bool) and patch['required'] != bool(field.get('required')):
                field['required'] = patch['required']
                changed.add(field['field_id'])
            if isinstance(patch.get('disabled'), bool) and patch.get('disabled') != bool(field.get('disabled')):
                field['disabled'] = patch['disabled']
                changed.add(field['field_id'])
        if not changed:
            error(422, 'no_valid_updates', '没有实际发生变化的题目调整。')
        match = re.search(r'_r(\d+)$', str(catalog.get('version') or ''))
        base = str(catalog['version'])[:match.start()] if match else str(catalog.get('version') or 'catalog')
        catalog['version'] = f"{base}_r{(int(match.group(1)) + 1) if match else 1}"
        path = DATA / self.catalog_filename
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        _os.replace(tmp, path)
        self._reload_catalog()
        return {'version': catalog['version'], 'updated_fields': sorted(changed)}

    def _reload_catalog(self):
        catalog_bytes = (DATA / self.catalog_filename).read_bytes()
        self.questionnaire = json.loads(catalog_bytes.decode('utf-8-sig'))
        self.catalog_source_sha256 = hashlib.sha256(catalog_bytes).hexdigest()
        self.fields = {field['field_id']: field for field in self.questionnaire['fields']}

    def apply_questionnaire_to_cases(self):
        """Re-freeze every case snapshot from the current master. Frozen
        snapshots otherwise keep the catalog version they were created with;
        this is the explicit admin action to push edits into existing cases."""
        catalog = self.questionnaire
        content = encoded(catalog)
        content_sha = text_hash(content)
        with self.store.lock, self.store.connect() as connection:
            rows = connection.execute('SELECT case_id FROM s3_questionnaire_snapshots').fetchall()
            for row in rows:
                connection.execute(
                    'UPDATE s3_questionnaire_snapshots SET version=?, content_sha256=?, source_sha256=?, frozen_at=?, catalog_json=? WHERE case_id=?',
                    (catalog['version'], content_sha, self.catalog_source_sha256, now(), content, row['case_id']))
                connection.execute('UPDATE cases SET questionnaire_version=?, updated_at=? WHERE id=?',
                                   (catalog['version'], now(), row['case_id']))
            return {'version': catalog['version'], 'cases_updated': len(rows)}

    def previsit_history(self, case_id):
        with self.store.lock, self.store.connect() as connection:
            binding = self._access(connection, case_id)
            rows = connection.execute("SELECT snapshot_id,context_version,variant,loaded_at FROM s3_previsit_versions WHERE case_id=? ORDER BY context_version", (case_id,))
            return {"versions": [dict(row) for row in rows], "state": binding["access_state"] if binding else "unloaded"}

    def previsit_snapshot(self, case_id, snapshot_id):
        with self.store.lock, self.store.connect() as connection:
            binding = self._access(connection, case_id)
            row = connection.execute("SELECT * FROM s3_previsit_versions WHERE case_id=? AND snapshot_id=?", (case_id, snapshot_id)).fetchone()
            if row is None:
                error(404, "snapshot_not_found", "本案不存在该事前快照。")
            return {"context": json.loads(row["context_json"]), "snapshot_id": row["snapshot_id"],
                    "context_version": row["context_version"], "loaded_at": row["loaded_at"],
                    "cached": binding["access_state"] == "unavailable", "source_kind": "synthetic_fixture"}

    def _latest(self, connection, table, case_id, entity_id):
        if table not in {"s3_statements", "s3_facts"}:
            raise ValueError("internal table error")
        row = connection.execute(f"SELECT payload_json FROM {table} WHERE case_id=? AND id=? ORDER BY revision DESC LIMIT 1", (case_id, entity_id)).fetchone()
        if row is None:
            error(404, "entity_not_found", "本案不存在该来源或字段事实。")
        return json.loads(row[0])

    def _all_latest(self, connection, table, case_id):
        if table not in {"s3_statements", "s3_facts"}:
            raise ValueError("internal table error")
        rows = connection.execute(f"SELECT a.payload_json FROM {table} a WHERE a.case_id=? AND a.revision=(SELECT MAX(b.revision) FROM {table} b WHERE b.id=a.id AND b.case_id=a.case_id) ORDER BY a.rowid", (case_id,))
        return [json.loads(row[0]) for row in rows]

    def _insert(self, connection, table, case_id, payload):
        connection.execute(f"INSERT INTO {table} VALUES (?,?,?,?)", (payload["id"], case_id, payload["revision"], encoded(payload)))

    @staticmethod
    def _expected(payload, expected_revision):
        if type(expected_revision) is not int or expected_revision != payload["revision"]:
            error(409, "revision_conflict", "版本已变化，请刷新后再操作，旧请求不会覆盖新版本。")

    def _subject(self, connection, case_id, subject_id, basis):
        if subject_id is None:
            if basis:
                error(422, "invalid_subject", "未知主体不可附加已归属依据。")
            return
        context = self._context(connection, case_id)
        if context is None:
            error(409, "subject_context_missing", "请先读取合成上下文后按ID选择主体。")
        identifiers = [p["party_id"] for p in context["parties"]] + [h["household_id"] for h in context["households"]] + [b["business_unit_id"] for b in context["business_units"]]
        if subject_id not in identifiers:
            error(422, "subject_not_in_case", "所选主体不属于本案绑定的上下文。")
        if not isinstance(basis, str) or not basis.strip():
            error(422, "subject_basis_required", "人工选择主体时必须保留归属依据。")

    def _interview(self, connection, case_id, interview_id):
        if interview_id is not None and connection.execute("SELECT id FROM interviews WHERE id=? AND case_id=? AND app_id=?", (interview_id, case_id, APP_ID)).fetchone() is None:
            error(404, "interview_not_found", "本案不存在该访谈。")

    def _workspace_statements(self, connection, case_id, statement_limit=None):
        values = self._all_latest(connection, "s3_statements", case_id)
        if statement_limit is None:
            return values
        # S4 only needs its bounded recent text window and the sources of current
        # facts. Older sources remain available through the normal S3/history API.
        referenced = {f["statement_id"] for f in self._all_latest(connection, "s3_facts", case_id)}
        recent = {s["id"] for s in values[-statement_limit:]}
        return [s for s in values if s["id"] in recent or s["id"] in referenced]

    def _refresh_sources(self, case_id, statement_limit=None):
        # Lock order is deliberate: collect under store.lock, release it, then
        # enter RecordingManager (which takes its state lock before store.lock).
        with self.store.lock, self.store.connect() as connection:
            self._case(connection, case_id)
            binding = self._binding(connection, case_id)
            if binding is not None and binding["access_state"] == "denied":
                return
            sources = [s.get("source_ref") for s in self._workspace_statements(connection, case_id, statement_limit)]
        refreshed = set()
        for source in sources:
            identity = (source["interview_id"], source["recording_id"]) if source else None
            if source and identity not in refreshed:
                refreshed.add(identity)
                try:
                    self.recordings.get(case_id, source["interview_id"], source["recording_id"])
                except Exception:
                    pass  # SQL revalidation below marks unavailable/changed; no fabricated body.

    def _segment_current(self, connection, case_id, source):
        try:
            row = connection.execute("SELECT context_json,snapshot_json FROM recordings WHERE id=? AND case_id=? AND interview_id=? AND app_id=?",
                                     (source["recording_id"], case_id, source["interview_id"], APP_ID)).fetchone()
            if row is None:
                return False
            snapshot = json.loads(row["snapshot_json"])
            segment = next((item for item in snapshot.get("segments", []) if item.get("id") == source["segment_id"]), None)
            if (not segment or not segment.get("stable") or segment.get("revision") != source["segment_revision"]
                    or snapshot.get("data_error") or text_hash(segment["text"]) != source["text_sha256"]):
                return False
            latest = connection.execute("SELECT * FROM transcript_revisions WHERE recording_id=? AND segment_id=? ORDER BY revision DESC LIMIT 1", (source["recording_id"], source["segment_id"])).fetchone()
            current = bool(latest and latest["revision"] == source["segment_revision"] and text_hash(latest["text"]) == source["text_sha256"]
                        and latest["source_run_id"] == source["source_run_id"] == segment.get("source_run_id")
                        and latest["start_frame"] == source["audio_start_frame"] and latest["end_frame"] == source["audio_end_frame"]
                        and 0 <= latest["start_frame"] < latest["end_frame"] <= snapshot.get("saved_frames", 0))
            if current:
                self._verified_archive(json.loads(row["context_json"]), source["recording_id"],
                                       source["audio_start_frame"], source["audio_end_frame"])
            return current
        except (sqlite3.DatabaseError, KeyError, TypeError, ValueError):
            return False
        except OSError:
            return False

    def _verified_archive(self, context, recording_id, start, end):
        """Validate the app/session binding and committed chunks for this source.

        This reads files directly and never enters RecordingManager's state lock.
        Only the referenced chunks need hashing when a long interview is active.
        """
        from .archive_paths import bound_archive_config
        from shared_recording_core.archive import read_manifest, read_pcm, sha256_file
        config = bound_archive_config(self.store, context, recording_id)
        manifest = read_manifest(config, verify=False)
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= manifest["saved_frames"]:
            raise ValueError("outside committed audio range")
        for chunk in manifest["chunks"]:
            if chunk["end_frame"] > start and chunk["start_frame"] < end:
                path = config.resolve_output(chunk["relative_path"])
                if (sha256_file(path) != chunk["sha256"]
                        or len(read_pcm(path)) // 2 != chunk["end_frame"] - chunk["start_frame"]):
                    raise ValueError("referenced committed audio chunk cannot be verified")
        return manifest

    def _statement_view(self, connection, case_id, statement):
        value = copy.deepcopy(statement)
        value["needs_review"] = bool(value.get("source_ref") and not self._segment_current(connection, case_id, value["source_ref"]))
        return value

    def create_statement(self, case_id, data, *, source=None, automatic=False):
        with self.store.lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._access(connection, case_id)
            self._interview(connection, case_id, data.get("interview_id"))
            self._subject(connection, case_id, data.get("subject_id"), data.get("subject_basis"))
            if source is not None and not self._segment_current(connection, case_id, source):
                error(409, "source_changed", "录音段落已更新或尚未稳定，请重新读取当前版本。")
            if source is not None and automatic:
                # BEGIN IMMEDIATE plus the persisted source key makes concurrent
                # automatic polls idempotent, including separate service objects.
                for existing in self._all_latest(connection, "s3_statements", case_id):
                    ref = existing.get("source_ref") or {}
                    if all(ref.get(k) == source[k] for k in ("recording_id", "segment_id", "segment_revision")):
                        if ref != source or existing["text"] != data["text"]:
                            error(409, "source_changed", "已存原话与当前段落来源不一致，未自动覆盖。")
                        return existing
            value = {"id": new_id("statement"), "case_id": case_id, "revision": 1, "text": data["text"],
                     "kind": "asr_segment" if source else "manual", "interview_id": data.get("interview_id"),
                     "speaker_label": data.get("speaker_label") or "未知说话人", "speaker_role": data.get("speaker_role") or "unknown",
                     "subject_id": data.get("subject_id"), "subject_basis": data.get("subject_basis"),
                     "record_confirmation": "pending", "needs_review": False, "has_audio": source is not None,
                     "source_ref": source, "created_at": now()}
            if automatic:
                value["import_origin"] = "automatic_stable_asr"
            self._insert(connection, "s3_statements", case_id, value)
            self._action(connection, case_id, value["id"], "statement_created", {"revision": 1})
            return value

    def _resolve_segment(self, case_id, data):
        # Never call this while holding store.lock. Client text is not a parameter.
        snapshot = self.recordings.get(case_id, data["interview_id"], data["recording_id"])
        segment = next((item for item in snapshot.get("segments", []) if item.get("id") == data["segment_id"]), None)
        if not segment:
            error(404, "segment_not_found", "本案录音中不存在该段落。")
        if (not segment.get("stable") or snapshot.get("data_error") or segment.get("revision") != data["segment_revision"]
                or not isinstance(segment.get("text"), str) or not segment["text"]):
            error(409, "source_changed", "只接受服务端当前、稳定且已保存的段落修订。")
        row = self.recordings.repository.get(case_id, data["interview_id"], data["recording_id"])
        try:
            start, end = round(segment["start_seconds"] * 16000), round(segment["end_seconds"] * 16000)
            self._verified_archive(row["context"], data["recording_id"], start, end)
        except Exception as exc:
            raise DiligenceError(409, "source_unavailable", "该段音频存档或范围当前无法验证。") from exc
        source = {"recording_id": data["recording_id"], "interview_id": data["interview_id"], "segment_id": data["segment_id"],
                  "segment_revision": data["segment_revision"], "source_run_id": segment.get("source_run_id"),
                  "text_sha256": text_hash(segment["text"]), "audio_start_frame": start, "audio_end_frame": end}
        return segment["text"], source

    def statement_from_segment(self, case_id, data):
        # Fast ownership/permission check occurs before any source-content read.
        with self.store.lock, self.store.connect() as connection:
            self._access(connection, case_id)
        text, source = self._resolve_segment(case_id, data)
        return self.create_statement(case_id, {**data, "text": text}, source=source)

    def automatic_statement_from_segment(self, case_id, data):
        """A stable server segment is source text, never a confirmed fact."""
        with self.store.lock, self.store.connect() as connection:
            self._access(connection, case_id)
        text, source = self._resolve_segment(case_id, data)
        return self.create_statement(case_id, {"text": text, "interview_id": data["interview_id"]},
                                     source=source, automatic=True)

    def statement(self, case_id, statement_id):
        self._refresh_sources(case_id)
        with self.store.lock, self.store.connect() as connection:
            self._access(connection, case_id)
            return self._statement_view(connection, case_id, self._latest(connection, "s3_statements", case_id, statement_id))

    def revise_statement(self, case_id, statement_id, data):
        with self.store.lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._access(connection, case_id)
            value = self._latest(connection, "s3_statements", case_id, statement_id)
            self._expected(value, data["expected_revision"])
            if value["kind"] != "manual" and data["text"] != value["text"]:
                error(409, "asr_source_read_only", "ASR原文由服务端保存，请用重新读取段落更新来源。")
            for key in ("text", "speaker_label", "speaker_role", "subject_id", "subject_basis"):
                if key in data:
                    value[key] = data[key]
            self._subject(connection, case_id, value["subject_id"], value["subject_basis"])
            value.update(revision=value["revision"] + 1, record_confirmation="pending", needs_review=False, updated_at=now())
            self._insert(connection, "s3_statements", case_id, value)
            self._action(connection, case_id, statement_id, "statement_revised", {"revision": value["revision"]})
            return value

    def confirm_statement(self, case_id, statement_id, expected_revision):
        self._refresh_sources(case_id)
        with self.store.lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._access(connection, case_id)
            value = self._latest(connection, "s3_statements", case_id, statement_id)
            self._expected(value, expected_revision)
            if self._statement_view(connection, case_id, value)["needs_review"]:
                error(409, "source_changed", "来源已更新或不可用，不能确认旧原文。")
            value.update(record_confirmation="record_accurate", confirmed_at=now(), confirmation_simulated=True)
            connection.execute("UPDATE s3_statements SET payload_json=? WHERE id=? AND case_id=? AND revision=?",
                               (encoded(value), statement_id, case_id, expected_revision))
            self._action(connection, case_id, statement_id, "statement_record_accurate", {"revision": expected_revision})
            return self._statement_view(connection, case_id, value)

    def refresh_segment(self, case_id, statement_id, expected_revision, segment_revision):
        with self.store.lock, self.store.connect() as connection:
            self._access(connection, case_id)
            current = self._latest(connection, "s3_statements", case_id, statement_id)
            self._expected(current, expected_revision)
            if not current["source_ref"]:
                error(409, "not_asr_source", "手工来源没有录音段可刷新。")
            request = {**current["source_ref"], "segment_revision": segment_revision}
        text, source = self._resolve_segment(case_id, request)
        with self.store.lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._access(connection, case_id)
            current = self._latest(connection, "s3_statements", case_id, statement_id)
            self._expected(current, expected_revision)
            if not self._segment_current(connection, case_id, source):
                error(409, "source_changed", "来源在更新时再次变化，请刷新。")
            current.update(revision=expected_revision + 1, text=text, source_ref=source,
                           record_confirmation="pending", needs_review=False, updated_at=now())
            self._insert(connection, "s3_statements", case_id, current)
            self._action(connection, case_id, statement_id, "segment_refreshed", {"revision": current["revision"]})
            return current

    def statement_history(self, case_id, statement_id):
        self._refresh_sources(case_id)
        with self.store.lock, self.store.connect() as connection:
            self._access(connection, case_id)
            self._latest(connection, "s3_statements", case_id, statement_id)
            rows = connection.execute("SELECT payload_json FROM s3_statements WHERE case_id=? AND id=? ORDER BY revision", (case_id, statement_id))
            return {"statement_id": statement_id, "versions": [self._statement_view(connection, case_id, json.loads(row[0])) for row in rows]}

    def _validate_values(self, connection, case_id, field_id, data):
        if self._binding(connection, case_id) is None:
            error(409, "previsit_binding_required", "请先显式绑定合成示例，冻结本案题库后再记录字段候选。")
        catalog, _ = self._case_catalog(connection, case_id, freeze=True)
        metadata = next((field for field in catalog["fields"] if field["field_id"] == field_id), None)
        if metadata is None:
            error(422, "unknown_field", "字段不在本案冻结题库中。")
        if metadata["handling"] != "interview":
            error(422, "field_not_interview_scalar", "该字段是上游、复杂结构或政策/派生边界，不能作为现场标量填值。")
        expected_type = "text" if metadata["value_type"] == "string" else metadata["value_type"]
        value_type = data.get("value_type")
        if value_type != expected_type or value_type not in {"integer", "decimal", "text", "enum"}:
            error(422, "value_type_mismatch", "值类型必须与题库字段一致，不做隐式类型转换。")
        presence, value = data.get("presence"), data.get("value")
        if presence not in KNOWN | {"missing", "unknown", "not_applicable", "withheld"}:
            error(422, "invalid_presence", "必须明确区分已知、零、未知、缺失与不适用。")
        if presence in KNOWN:
            if type(value) is not str or not value.strip():
                error(422, "invalid_value", "已知值必须为非空字符串，数值须保留十进制字符串。")
            if value_type == "integer" and not INTEGER.fullmatch(value):
                error(422, "invalid_integer", "整数字段只接受十进制整数字符串。")
            if value_type == "decimal" and not DECIMAL.fullmatch(value):
                error(422, "invalid_decimal", "数值字段不接受NaN、科学计数或隐式数词转换。")
            if presence == "explicit_zero" and value != "0":
                error(422, "invalid_zero", "显式零必须为字符串0，不以空值代替。")
        elif value is not None:
            error(422, "invalid_presence_value", "未知、缺失或不适用必须保留null，不能夹带数值。")
        unit_status, unit = data.get("unit_status", "unknown"), data.get("unit")
        period_status, period = data.get("period_status", "unknown"), data.get("period")
        if unit_status not in {"unknown", "explicit", "not_applicable"} or period_status not in {"unknown", "explicit", "not_applicable"}:
            error(422, "invalid_aspect_status", "单位和期间状态不合法。")
        if (unit_status == "explicit" and (type(unit) is not str or not unit.strip())) or (unit_status != "explicit" and unit is not None):
            error(422, "unit_status_mismatch", "单位未知或不适用时保留null，明确时必须填写实际单位。")
        if period_status == "explicit":
            if not isinstance(period, dict) or set(period) != {"kind", "label", "start", "end"} or period["kind"] not in {"year", "season", "as_of", "duration", "purchase_date"} or type(period["label"]) is not str or not period["label"].strip():
                error(422, "invalid_period", "明确期间须含类型、标签和起止日期或null。")
            try:
                for boundary in (period["start"], period["end"]):
                    if boundary is not None:
                        if type(boundary) is not str:
                            raise ValueError("not text")
                        date.fromisoformat(boundary)
                if period["start"] and period["end"] and period["start"] > period["end"]:
                    raise ValueError("reversed")
            except ValueError:
                error(422, "invalid_period", "期间日期须为ISO日期且起点不晚于终点。")
        elif period is not None:
            error(422, "period_status_mismatch", "未知或不适用期间不得隐式填默认年度。")
        subject_status, subject_id = data.get("subject_status", "unknown"), data.get("subject_id")
        if subject_status not in {"unknown", "explicit"} or (subject_status == "unknown" and subject_id is not None) or (subject_status == "explicit" and subject_id is None):
            error(422, "subject_status_mismatch", "主体须明确选择ID或保留未知，不能由说话人自动推断。")
        self._subject(connection, case_id, subject_id, data.get("subject_basis"))
        if (field_id == "crop.main.scale_category"
                and metadata.get("input_binding", {}).get("contract") == MAIN_CROP_CATEGORY_CONTRACT):
            # A B01 score bucket is an explicit human assertion, not a mapping
            # from the unresolved source category tree or a crop-name guess.
            if presence in KNOWN:
                if value_type != "enum" or value not in MAIN_CROP_CATEGORIES:
                    error(422, "invalid_main_crop_category", "请选择已明确的水果、主粮、中草药或其他品类；未知不能当作其他。")
                if (subject_status != "explicit"
                        or self._subject_scope(self._context(connection, case_id), {**data, "field_id": field_id}) != "context_match"):
                    error(422, "main_crop_subject_required", "已知主营品类必须明确归属本案主营经营单元并保留依据。")
                if unit_status != "not_applicable" or unit is not None:
                    error(422, "main_crop_category_unit", "品类是选项，请明确单位不适用，不可填入面积或其他单位。")
        keys = ("statement_id", "statement_revision", "value_type", "value", "presence", "unit", "unit_status",
                "period", "period_status", "subject_id", "subject_status", "subject_basis")
        normalized = {**data, "unit_status": unit_status, "period_status": period_status, "subject_status": subject_status}
        return {key: copy.deepcopy(normalized.get(key)) for key in keys}

    def _source_for_fact(self, connection, case_id, data):
        statement = self._latest(connection, "s3_statements", case_id, data["statement_id"])
        if type(data["statement_revision"]) is not int or data["statement_revision"] != statement["revision"]:
            error(409, "source_changed", "字段候选必须引用当前原文版本，不能接受过期引用。")
        if self._statement_view(connection, case_id, statement)["needs_review"]:
            error(409, "source_changed", "录音来源已变化或不可用，先刷新原文。")
        return statement

    def create_fact(self, case_id, data):
        self._refresh_sources(case_id)
        with self.store.lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            binding = self._access(connection, case_id)
            values = self._validate_values(connection, case_id, data["field_id"], data)
            statement = self._source_for_fact(connection, case_id, values)
            value = None
            if data["field_id"] == "crop.main.scale_category":
                # A retry of the same explicit selection must not produce
                # duplicate sources or silently confirm a candidate.
                for existing in self._all_latest(connection, "s3_facts", case_id):
                    if (existing["field_id"] == data["field_id"]
                            and existing.get("record_confirmation") != "revoked"
                            and all(existing.get(key) == item for key, item in values.items())
                            and existing.get("source_text_sha256") == text_hash(statement["text"])
                            and existing.get("context_snapshot_id") == (binding["active_snapshot_id"] if binding else None)):
                        value = existing
                        break
            if value is None:
                value = {"id": new_id("fact"), "case_id": case_id, "field_id": data["field_id"], "revision": 1,
                         "origin": "onsite", **values, "record_confirmation": "pending", "verification": "not_checked",
                         "needs_review": False, "conflict": False, "source_ref": copy.deepcopy(statement["source_ref"]),
                         "source_text_sha256": text_hash(statement["text"]), "created_at": now(),
                         "context_snapshot_id": binding["active_snapshot_id"] if binding else None}
                self._insert(connection, "s3_facts", case_id, value)
                self._action(connection, case_id, value["id"], "fact_candidate_created", {"revision": 1})
        return self.fact(case_id, value["id"])

    def revise_fact(self, case_id, fact_id, data):
        self._refresh_sources(case_id)
        with self.store.lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            binding = self._access(connection, case_id)
            current = self._latest(connection, "s3_facts", case_id, fact_id)
            self._expected(current, data["expected_revision"])
            values = self._validate_values(connection, case_id, current["field_id"], data)
            statement = self._source_for_fact(connection, case_id, values)
            value = {**current, **values, "revision": current["revision"] + 1, "record_confirmation": "pending",
                     "verification": "not_checked", "needs_review": False, "conflict": False,
                     "source_ref": copy.deepcopy(statement["source_ref"]), "source_text_sha256": text_hash(statement["text"]),
                     "context_snapshot_id": binding["active_snapshot_id"] if binding else None, "updated_at": now()}
            value.pop("confirmation_basis", None)
            self._insert(connection, "s3_facts", case_id, value)
            self._action(connection, case_id, fact_id, "fact_revised", {"revision": value["revision"]})
        return self.fact(case_id, fact_id)

    def confirm_fact(self, case_id, fact_id, expected_revision, basis=None):
        self._refresh_sources(case_id)
        with self.store.lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._access(connection, case_id)
            value = self._latest(connection, "s3_facts", case_id, fact_id)
            self._expected(value, expected_revision)
            statement = self._source_for_fact(connection, case_id, value)
            if self._onsite_view(connection, case_id, value)["needs_review"]:
                error(409, "source_changed", "来源或事前版本已变化，先复核并修订候选再确认。")
            if statement["record_confirmation"] != "record_accurate":
                error(409, "source_confirmation_required", "先确认本版来源原文记录准确，再确认字段候选。")
            if value["record_confirmation"] == "revoked":
                error(409, "revoked_requires_revision", "撤回的字段须先形成新候选，不能直接恢复旧确认。")
            value.update(revision=value["revision"] + 1, record_confirmation="record_accurate",
                         confirmation_basis=basis, confirmed_at=now(), confirmation_simulated=True)
            # Record accuracy never promotes verification beyond not_checked.
            self._insert(connection, "s3_facts", case_id, value)
            self._action(connection, case_id, fact_id, "fact_record_accurate", {"revision": value["revision"]})
        return self.fact(case_id, fact_id)

    def revoke_fact(self, case_id, fact_id, expected_revision, basis=None):
        with self.store.lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._access(connection, case_id)
            value = self._latest(connection, "s3_facts", case_id, fact_id)
            self._expected(value, expected_revision)
            value.update(revision=value["revision"] + 1, record_confirmation="revoked", revoked_at=now(), revocation_basis=basis)
            self._insert(connection, "s3_facts", case_id, value)
            self._action(connection, case_id, fact_id, "fact_confirmation_revoked", {"revision": value["revision"]})
        return self.fact(case_id, fact_id)

    def _onsite_view(self, connection, case_id, fact):
        value = copy.deepcopy(fact)
        binding = self._binding(connection, case_id)
        current_snapshot = binding["active_snapshot_id"] if binding else None
        try:
            source = self._latest(connection, "s3_statements", case_id, value["statement_id"])
            stale = (source["revision"] != value["statement_revision"] or text_hash(source["text"]) != value["source_text_sha256"]
                     or self._statement_view(connection, case_id, source)["needs_review"])
            source_confirmed = source["record_confirmation"] == "record_accurate"
        except DiligenceError:
            source, stale, source_confirmed = None, True, False
        value["needs_review"] = stale or current_snapshot != value.get("context_snapshot_id")
        value["source_record_accurate"] = source_confirmed and not stale
        value["speaker_role"] = source["speaker_role"] if source else "unknown"
        value["verification"] = "not_checked"
        value["has_audio"] = bool(value["source_ref"])
        value["subject_scope"] = self._subject_scope(self._context(connection, case_id), value)
        value.setdefault("conflict", False)
        value["calculation_eligibility"] = self._eligibility(value)
        return value

    @staticmethod
    def _eligibility(value):
        if value.get("needs_review"):
            return "blocked_source_changed"
        if value.get("record_confirmation") != "record_accurate":
            return "pending_confirmation"
        if value.get("subject_scope") in {"unknown", "out_of_scope"}:
            return "blocked_subject"
        if value.get("conflict"):
            return "blocked_conflict"
        if value.get("presence") not in KNOWN:
            return "blocked_missing"
        if value.get("unit_status") == "unknown" or (value.get("value_type") in {"integer", "decimal"} and value.get("unit_status") != "explicit"):
            return "blocked_unit"
        if value.get("period_status") == "unknown":
            return "blocked_period"
        if value.get("subject_status") != "explicit" or (value.get("origin") == "onsite" and value.get("speaker_role") != "applicant"):
            return "blocked_subject"
        return "pending_rule_calibration"

    @staticmethod
    def _subject_scope(context, value):
        if context is None or value.get("subject_status") != "explicit":
            return "unknown"
        applicant, subject, field = context["applicant_party_id"], value.get("subject_id"), value["field_id"]
        households = {item["household_id"] for item in context["households"] if applicant in item["member_party_ids"]}
        units = {item["business_unit_id"]: item for item in context["business_units"] if item["operator_party_id"] == applicant}
        if subject != applicant and subject not in households and subject not in units:
            return "out_of_scope"
        if field.startswith("household."):
            return "context_match" if subject in households else "out_of_scope"
        if field.startswith("operator."):
            return "context_match" if subject == applicant else "out_of_scope"
        for prefix, kind in (("crop.main.", "main_crop"), ("crop.secondary.", "secondary_crop")):
            if field.startswith(prefix):
                return "context_match" if subject in units and units[subject]["kind"] == kind else "out_of_scope"
        # No general ownership/aggregation rule was supplied for other fields.
        return "pending_business_definition"

    def _previsit_facts(self, case_id, context, *, cached=False):
        if context is None:
            return []
        result = []
        for original in context["facts"]:
            value = {**copy.deepcopy(original), "id": "previsit_" + original["fact_id"], "case_id": case_id,
                     "origin": "previsit", "statement_id": None, "statement_revision": None,
                     "subject_basis": "合成上游显式主体ID；未进行业务复核", "has_audio": False,
                     "record_confirmation_meta": copy.deepcopy(original["record_confirmation"]),
                     "verification_meta": copy.deepcopy(original["verification"]),
                     "record_confirmation": original["record_confirmation"]["state"],
                     "verification": original["verification"]["state"], "conflict": False,
                     "needs_review": cached or any(s["availability"] != "available" for s in original["source_refs"]),
                     "source_ref": {"kind": "previsit", "snapshot_id": context["snapshot_id"], "source_refs": copy.deepcopy(original["source_refs"])}}
            value["subject_scope"] = self._subject_scope(context, value)
            value["calculation_eligibility"] = self._eligibility(value)
            result.append(value)
        return result

    @staticmethod
    def _period_key(period):
        if period is None:
            return None
        return (period["kind"], period["start"], period["end"], period["label"] if period["start"] is None and period["end"] is None else None)

    def _conflicts(self, facts):
        groups = {}
        for fact in facts:
            fact["conflict"] = False
            if fact["record_confirmation"] != "revoked" and fact["presence"] in KNOWN:
                groups.setdefault(fact["field_id"], []).append(fact)
        conflicts = []
        for field_id, values in groups.items():
            kinds = set()
            if len(values) < 2:
                continue
            comparable = []
            for value in values:
                typed = Decimal(value["value"]) if value["value_type"] in {"integer", "decimal"} else value["value"]
                comparable.append((value["value_type"], typed))
            if len(set(comparable)) > 1:
                kinds.add("value")
            units = {value["unit"] for value in values if value["unit_status"] == "explicit"}
            periods = {self._period_key(value["period"]) for value in values if value["period_status"] == "explicit"}
            subjects = {value["subject_id"] for value in values if value["subject_status"] == "explicit"}
            if len(units) > 1:
                kinds.add("unit")
            if len(periods) > 1:
                kinds.add("period")
            if len(subjects) > 1:
                kinds.add("subject")
            if kinds:
                for value in values:
                    value["conflict"] = True
                    value["calculation_eligibility"] = self._eligibility(value)
                conflicts.append({"field_id": field_id, "fact_ids": [v["id"] for v in values], "kinds": sorted(kinds), "unresolved": True})
        return conflicts

    def _questionnaire_view(self, facts, catalog=None, snapshot=None):
        catalog = catalog or self.questionnaire
        field_map = {field["field_id"]: field for field in catalog["fields"]}
        answers = {}
        for fact in facts:
            if fact["field_id"] in field_map and fact["record_confirmation"] == "record_accurate":
                for key in field_map[fact["field_id"]].get("question_keys", []):
                    answers[key] = {"value": fact["value"], "presence": fact["presence"], "conflict": fact["conflict"], "needs_review": fact["needs_review"]}
        fields = []
        for metadata in catalog["fields"]:
            conditions = metadata.get("conditions") or []
            evaluations = [evaluate_condition(condition, answers) for condition in conditions]
            state = "not_applicable" if "false" in evaluations else "applicable" if evaluations and all(v == "true" for v in evaluations) else "unknown"
            values = [f for f in facts if f["field_id"] == metadata["field_id"] and f["record_confirmation"] != "revoked"]
            completion = "missing"
            if state == "not_applicable":
                completion = "not_applicable"
            elif any(v["needs_review"] or v["conflict"] for v in values):
                completion = "needs_review"
            elif any(v["record_confirmation"] == "record_accurate" and v["presence"] in KNOWN for v in values):
                completion = "record_accurate"
            elif values:
                completion = "candidate"
            fields.append({**copy.deepcopy(metadata), "applicability": state,
                           "applicability_reason": "规则已启用并已求值" if state != "unknown" else "来源条件或映射仍为候选；不自动判断不适用",
                           "completion": completion, "fact_ids": [v["id"] for v in values]})
        return {"version": catalog["version"], "fields": fields, **(snapshot or {"frozen": False}),
                "progress": {"field_count": len(fields), "interview_count": sum(f["handling"] == "interview" for f in fields),
                             **{key: sum(f["applicability"] == key for f in fields) for key in ("applicable", "unknown", "not_applicable")},
                             **{key: sum(f["completion"] == key for f in fields) for key in ("record_accurate", "needs_review")}}}

    def workspace(self, case_id, *, statement_limit=None):
        self._refresh_sources(case_id, statement_limit)
        with self.store.lock, self.store.connect() as connection:
            self._case(connection, case_id)
            binding = self._binding(connection, case_id)
            catalog, catalog_snapshot = self._case_catalog(connection, case_id, freeze=binding is not None)
            state = binding["access_state"] if binding else "unloaded"
            context = self._context(connection, case_id, binding) if state != "denied" else None
            cached = state == "unavailable" and context is not None
            previsit_facts = self._previsit_facts(case_id, context, cached=cached)
            statements = [self._statement_view(connection, case_id, value) for value in self._workspace_statements(connection, case_id, statement_limit)] if state != "denied" else []
            onsite = [self._onsite_view(connection, case_id, value) for value in self._all_latest(connection, "s3_facts", case_id)] if state != "denied" else []
            facts = previsit_facts + onsite
            conflicts = self._conflicts(facts)
            history = [dict(row) for row in connection.execute("SELECT snapshot_id,context_version,variant,loaded_at FROM s3_previsit_versions WHERE case_id=? ORDER BY context_version", (case_id,))] if state != "denied" else []
            return {"case_id": case_id, "mode": "synthetic_development",
                    "previsit": {"state": state, "fixture_id": binding["fixture_id"] if binding else None,
                                 "context_version": context["context_version"] if context else None, "snapshot_id": context["snapshot_id"] if context else None,
                                 "cached": cached, "parties": context["parties"] if context else [],
                                 "households": context["households"] if context else [], "business_units": context["business_units"] if context else [],
                                 "facts": previsit_facts, "materials": context["materials"] if context else [], "history": history,
                                 "upstream_connection_status": "not_connected", "read_originals": False,
                                 "data_as_of": context["data_as_of"] if context else None},
                    "questionnaire": self._questionnaire_view(facts, catalog, catalog_snapshot), "statements": statements, "facts": facts,
                    "conflicts": conflicts, "credit": {"state": "blocked", "reason": "pending_rule_calibration", "effective_limit": None}}

    def fact(self, case_id, fact_id):
        workspace = self.workspace(case_id)
        if workspace["previsit"]["state"] == "denied":
            error(403, "previsit_access_denied", "当前权限拒绝，字段与来源不能旁路读取。")
        value = next((f for f in workspace["facts"] if f["id"] == fact_id), None)
        if value is None:
            error(404, "entity_not_found", "本案不存在该事实。")
        return value

    def fact_history(self, case_id, fact_id):
        self._refresh_sources(case_id)
        with self.store.lock, self.store.connect() as connection:
            binding = self._access(connection, case_id)
            if fact_id.startswith("previsit_"):
                versions = []
                for row in connection.execute("SELECT context_json FROM s3_previsit_versions WHERE case_id=? ORDER BY context_version", (case_id,)):
                    versions += [f for f in self._previsit_facts(case_id, json.loads(row[0]), cached=binding["access_state"] == "unavailable") if f["id"] == fact_id]
                if not versions:
                    error(404, "entity_not_found", "本案不存在该上游事实。")
            else:
                self._latest(connection, "s3_facts", case_id, fact_id)
                versions = [self._onsite_view(connection, case_id, json.loads(row[0])) for row in connection.execute("SELECT payload_json FROM s3_facts WHERE case_id=? AND id=? ORDER BY revision", (case_id, fact_id))]
            return {"fact_id": fact_id, "versions": versions}
