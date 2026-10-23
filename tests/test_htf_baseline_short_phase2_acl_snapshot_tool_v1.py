"""Issue #185 — unit and static tests for the Phase-2 ACL snapshot tool and
the snapshot-driven 075 rollback engine (no database required).

Covers: deterministic canonical JSON, payload/file SHA-256 checksums, secret
redaction, exact-ACL comparison semantics (explicit vs effective, tuple level,
representation-only), the restore-plan SQL shape (grantor/grantee/grantable
restoration, function config restore), the audited-075-delta classifier, and
static scope guards (read-only snapshot tool, rollback tool can never name the
Point-B stack or run DROP OWNED BY, runbook contract text).
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "research"))

import htf_baseline_short_phase2_acl_snapshot_v1 as snap  # noqa: E402
import htf_baseline_short_phase2_rollback_075_v1 as rb  # noqa: E402

RUNBOOK = ROOT / "docs" / "research" / "HTF_BASELINE_SHORT_PHASE2_MIGRATION_074_075_RUNBOOK_V1.md"
AUDIT_DOC = ROOT / "docs" / "research" / "HTF_BASELINE_SHORT_PHASE2_MIGRATION_075_PRIVILEGE_DELTA_AUDIT_V1.md"
HARDENING_DOC = ROOT / "docs" / "research" / "HTF_BASELINE_SHORT_PHASE2_MIGRATION_075_ROLLBACK_HARDENING_V1.md"
SNAPSHOT_TOOL = ROOT / "tools" / "research" / "htf_baseline_short_phase2_acl_snapshot_v1.py"
ROLLBACK_TOOL = ROOT / "tools" / "research" / "htf_baseline_short_phase2_rollback_075_v1.py"

OWNER = "postgres"


# --------------------------------------------------------------------------- #
# synthetic states (shape mirrors capture_state output)
# --------------------------------------------------------------------------- #
def _role_attrs(**over) -> dict:
    attrs = {
        "superuser": False, "inherit": False, "createrole": False, "createdb": False,
        "can_login": False, "replication": False, "bypassrls": False,
        "connlimit": -1, "valid_until": None,
    }
    attrs.update(over)
    return attrs


def _acl(*entries):
    return [list(e) for e in entries]


BASE_ACL = _acl((OWNER, OWNER, "SELECT", False))


def _base_state() -> dict:
    tables = [
        {
            "key": key,
            "kind": "relation",
            "identity": {"oid": 20000 + i, "name": key.split(".", 1)[1], "owner": OWNER},
            "affected": True,
            "acl_is_null": True,
            "acl_explicit": [],
            "acl_effective": [list(e) for e in BASE_ACL],
        }
        for i, key in enumerate(sorted(rb.P2_TABLE_KEYS))
    ]
    seq_key = sorted(rb.P2_SEQUENCE_KEYS)[0]
    tables.append(
        {
            "key": seq_key,
            "kind": "sequence",
            "identity": {"oid": 20100, "name": seq_key.split(".", 1)[1], "owner": OWNER},
            "affected": True,
            "acl_is_null": True,
            "acl_explicit": [],
            "acl_effective": _acl((OWNER, OWNER, "USAGE", False)),
        }
    )
    functions = [
        {
            "key": key,
            "kind": "function",
            "identity": {
                "oid": 21000 + i,
                "name": key.split(".", 1)[1].split("(", 1)[0],
                "signature": key.split(":", 1)[1],
                "prosrc_md5": hashlib.md5(key.encode()).hexdigest(),
                "owner": OWNER,
            },
            "config": {"security": "INVOKER", "proconfig": []},
            "affected": True,
            "acl_is_null": True,
            "acl_explicit": [],
            "acl_effective": _acl(
                (OWNER, "PUBLIC", "EXECUTE", False), (OWNER, OWNER, "EXECUTE", False)
            ),
        }
        for i, key in enumerate(sorted(rb.P2_FUNCTION_KEYS))
    ]
    return {
        "database": {
            "key": "database:trad_bot_p2_it_unit",
            "kind": "database",
            "identity": {"oid": 16384, "name": "trad_bot_p2_it_unit", "owner": OWNER},
            "acl_is_null": True,
            "acl_explicit": [],
            "acl_effective": _acl(
                (OWNER, "PUBLIC", "CONNECT", False), (OWNER, OWNER, "CONNECT", False)
            ),
        },
        "schemas": [
            {
                "key": "schema:research",
                "kind": "schema",
                "identity": {"oid": 2200, "name": "research", "owner": OWNER},
                "affected": True,
                "acl_is_null": True,
                "acl_explicit": [],
                "acl_effective": _acl((OWNER, OWNER, "USAGE", False)),
            }
        ],
        "relations": tables,
        "functions": functions,
        "roles": {OWNER: _role_attrs(superuser=True, inherit=True, can_login=True)},
        "memberships": [],
        "default_privileges": [],
        "shdepend": [],
        "effective_privileges": {OWNER: {}},
        "capture_info": {"server_version": "17.11", "server_version_num": 170011},
    }


def _apply_075_delta(state: dict) -> dict:
    """Synthesize the exact audited 075 delta on a copy of ``state``."""
    out = copy.deepcopy(state)
    out["roles"][rb.OBSERVER_ROLE] = dict(rb._EXPECTED_ROLE_ATTRIBUTES[rb.OBSERVER_ROLE])
    out["roles"][rb.ADMIN_ROLE] = dict(rb._EXPECTED_ROLE_ATTRIBUTES[rb.ADMIN_ROLE])
    db = out["database"]["acl_effective"]
    db += _acl(
        (OWNER, rb.OBSERVER_ROLE, "CONNECT", False),
        (OWNER, rb.ADMIN_ROLE, "CONNECT", False),
    )
    sch = out["schemas"][0]["acl_effective"]
    sch += _acl(
        (OWNER, rb.OBSERVER_ROLE, "USAGE", False),
        (OWNER, rb.ADMIN_ROLE, "USAGE", False),
    )
    for rel in out["relations"]:
        if rel["key"] in rb.P2_TABLE_KEYS:
            rel["acl_effective"] += _acl(
                (OWNER, rb.OBSERVER_ROLE, "SELECT", False),
                (OWNER, rb.ADMIN_ROLE, "SELECT", False),
            )
    for fn in out["functions"]:
        key = fn["key"]
        eff = [e for e in fn["acl_effective"] if e[1] != "PUBLIC"]
        if key == rb.INSERT_FN_KEY:
            eff += _acl((OWNER, rb.OBSERVER_ROLE, "EXECUTE", False))
        elif key == rb.ACTIVATE_FN_KEY:
            eff += _acl((OWNER, rb.ADMIN_ROLE, "EXECUTE", False))
        fn["acl_effective"] = eff
        if key in (rb.INSERT_FN_KEY, rb.ACTIVATE_FN_KEY):
            fn["config"] = {
                "security": "DEFINER",
                "proconfig": ["search_path=pg_catalog, research"],
            }
    return out


# --------------------------------------------------------------------------- #
# determinism, checksums, redaction
# --------------------------------------------------------------------------- #
def test_canonical_json_deterministic_and_order_independent():
    a = {"b": 1, "a": [3, 2, 1], "c": {"z": "x", "y": None}}
    b = {"c": {"y": None, "z": "x"}, "a": [3, 2, 1], "b": 1}
    assert snap.canonical_bytes(a) == snap.canonical_bytes(b)
    assert snap.payload_sha256(a) == snap.payload_sha256(b)
    assert len(snap.payload_sha256(a)) == 64


def test_payload_hash_detects_any_state_change():
    state = _base_state()
    base = snap.payload_sha256(state)
    changed = copy.deepcopy(state)
    changed["database"]["acl_effective"][0][2] = "TEMPORARY"
    assert snap.payload_sha256(changed) != base
    changed = copy.deepcopy(state)
    changed["functions"][0]["config"]["security"] = "DEFINER"
    assert snap.payload_sha256(changed) != base


def test_snapshot_file_checksums_and_tamper_detection(tmp_path):
    state = _base_state()
    snapshot = snap.build_snapshot(state, {"database": "unit"})
    path = Path(tmp_path) / "snap.json"
    info = snap.write_snapshot(path, snapshot)
    assert Path(info["sidecar"]).exists()
    snap.verify_snapshot_file(path)  # clean pass

    # missing file
    with pytest.raises(snap.SnapshotIntegrityError) as exc:
        snap.verify_snapshot_file(Path(tmp_path) / "nope.json")
    assert "SNAPSHOT_FILE_MISSING" in str(exc.value)

    # missing sidecar
    snap.sidecar_path(path).unlink()
    with pytest.raises(snap.SnapshotIntegrityError) as exc:
        snap.verify_snapshot_file(path)
    assert "SNAPSHOT_CHECKSUM_FILE_MISSING" in str(exc.value)
    snap.write_snapshot(path, snapshot)

    # file tamper (sidecar stale) -> file-level mismatch
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"htf_p2_acl_snapshot_v1"', '"htf_p2_acl_snapshot_vX"'), encoding="utf-8")
    with pytest.raises(snap.SnapshotIntegrityError) as exc:
        snap.verify_snapshot_file(path)
    assert "SNAPSHOT_FILE_CHECKSUM_MISMATCH" in str(exc.value)

    # payload tamper with refreshed sidecar -> payload-level mismatch
    tampered = copy.deepcopy(snapshot)
    tampered["state"]["roles"]["evil"] = _role_attrs(superuser=True)
    snap.write_snapshot(path, tampered)  # rewrites file+sidecar consistently
    with pytest.raises(snap.SnapshotIntegrityError) as exc:
        snap.verify_snapshot_file(path)
    assert "SNAPSHOT_PAYLOAD_CHECKSUM_MISMATCH" in str(exc.value)


def test_secret_redaction_blocks_credentials():
    with pytest.raises(snap.SnapshotRedactionError):
        snap.assert_no_secrets({"roles": {"postgres": {"rolpassword": "x"}}})
    with pytest.raises(snap.SnapshotRedactionError):
        snap.assert_no_secrets({"note": "SCRAM-SHA-256$4096:abc$def:ghi"})
    with pytest.raises(snap.SnapshotRedactionError):
        snap.assert_no_secrets({"nested": [{"api_key": "abc123"}]})
    with pytest.raises(snap.SnapshotRedactionError):
        snap.assert_no_secrets({"text": "BEGIN RSA PRIVATE KEY\n..."})
    # normal state passes
    snap.assert_no_secrets(_base_state())


# --------------------------------------------------------------------------- #
# comparison semantics: exact ACL, explicit vs effective, representation
# --------------------------------------------------------------------------- #
def test_compare_states_exact_acl_and_representation_semantics():
    before = _base_state()
    after = copy.deepcopy(before)

    # pure representation change: stored ACL materialized, same effective set
    after["database"]["acl_is_null"] = False
    after["database"]["acl_explicit"] = [list(e) for e in after["database"]["acl_effective"]]
    report = snap.compare_states(before, after)
    assert report["acl_equal"] is True
    assert report["representation_only"], "must be reported as representation-only"
    assert report["equal"] is True

    # a real ACL tuple change is an ACL difference, never representation
    after2 = copy.deepcopy(before)
    after2["database"]["acl_effective"].append([OWNER, "trad_bot", "CREATE", False])
    report = snap.compare_states(before, after2)
    assert report["acl_equal"] is False
    assert any(c["kind"] == "acl_entry" for c in report["acl_differences"])

    # grantable-flag and grantor are part of the tuple identity
    after3 = copy.deepcopy(before)
    after3["database"]["acl_effective"] = _acl(
        (OWNER, "PUBLIC", "CONNECT", True), (OWNER, OWNER, "CONNECT", False)
    )
    report = snap.compare_states(before, after3)
    assert report["acl_equal"] is False

    # effective-privilege differences are tracked separately
    after4 = copy.deepcopy(before)
    after4["effective_privileges"] = {OWNER: {"database:trad_bot_p2_it_unit": {"CONNECT": False}}}
    report = snap.compare_states(before, after4)
    assert report["effective_privileges_equal"] is False
    assert report["acl_equal"] is True


# --------------------------------------------------------------------------- #
# restore planning
# --------------------------------------------------------------------------- #
def test_restore_acl_statements_reproduce_exact_tuples():
    statements = rb._restore_acl_statements(
        {
            "kind": "relation",
            "key": "relation:research.htf_baseline_short_phase2_observation",
            "target": _acl(
                (OWNER, "PUBLIC", "SELECT", False),
                (OWNER, "trad_bot", "UPDATE", True),
                ("trad_bot", "analytics_runner", "INSERT", False),
            ),
            "current": _acl((OWNER, rb.OBSERVER_ROLE, "SELECT", False)),
        }
    )
    joined = "\n".join(statements)
    # revoke per grantee — REVOKE ... FROM ALL is not valid SQL
    assert 'REVOKE ALL ON TABLE "research"."htf_baseline_short_phase2_observation" FROM PUBLIC' in joined
    assert 'FROM "trad_bot"' in joined and 'FROM "analytics_runner"' in joined
    assert 'FROM "trad_bot_htf_p2_observer"' in joined
    # grantors and grantable flags are restored explicitly
    assert 'GRANT UPDATE ON TABLE "research"."htf_baseline_short_phase2_observation" TO "trad_bot" WITH GRANT OPTION GRANTED BY "postgres"' in joined
    assert 'GRANT INSERT ON TABLE "research"."htf_baseline_short_phase2_observation" TO "analytics_runner" GRANTED BY "trad_bot"' in joined
    assert 'GRANT SELECT ON TABLE "research"."htf_baseline_short_phase2_observation" TO PUBLIC GRANTED BY "postgres"' in joined
    # PUBLIC never receives grant options (invalid in PostgreSQL)
    assert "TO PUBLIC WITH GRANT OPTION" not in joined


def test_restore_function_config_statements_shape():
    statements = rb._restore_function_config_statements(
        rb.INSERT_FN_KEY, {"security": "INVOKER", "proconfig": []}
    )
    joined = "\n".join(statements)
    assert "SECURITY INVOKER" in joined and "RESET ALL" in joined
    fn_ref = '"research"."htf_baseline_short_phase2_insert_observation"(text'
    assert fn_ref in joined
    statements = rb._restore_function_config_statements(
        rb.ACTIVATE_FN_KEY,
        {"security": "DEFINER", "proconfig": ["search_path=pg_catalog, research"]},
    )
    joined = "\n".join(statements)
    assert "SECURITY DEFINER" in joined
    assert "SET search_path = pg_catalog, research" in joined


def test_proconfig_change_rules_match_075():
    target = rb._SEARCH_PATH_TARGET
    # exactly the 075 change
    assert rb._proconfig_change_explained([], [target]) is True
    # a pre-existing search_path is replaced (075 SET replaces), other GUCs stay
    assert rb._proconfig_change_explained(
        ["search_path=public", "work_mem=1MB"], [target, "work_mem=1MB"]
    ) is True
    # extra GUC changes are not explainable by 075
    assert rb._proconfig_change_explained([], [target, "work_mem=1MB"]) is False
    assert rb._proconfig_change_explained(["work_mem=1MB"], [target]) is False
    # nothing pinned -> not the 075 delta
    assert rb._proconfig_change_explained([], []) is False
    assert rb._proconfig_change_explained([], ["search_path=public"]) is False


def test_classify_changes_accepts_only_the_audited_delta():
    before = _base_state()
    after = _apply_075_delta(before)
    changes = snap.diff_states(before, after)
    explained, derived, unexpected = rb.classify_changes(changes, before, after)
    assert unexpected == [], unexpected
    assert explained, "the synthetic 075 delta must be recognized"

    # a grant to an app role on the Phase-2 surface is NOT the audited delta
    rogue = copy.deepcopy(after)
    rogue["relations"][0]["acl_effective"].append([OWNER, "trad_bot", "SELECT", False])
    changes = snap.diff_states(before, rogue)
    _, _, unexpected = rb.classify_changes(changes, before, rogue)
    assert unexpected, "grant to trad_bot must be refused as conflicting state"

    # role attribute drift is refused too
    rogue = copy.deepcopy(after)
    rogue["roles"][rb.OBSERVER_ROLE]["superuser"] = True
    changes = snap.diff_states(before, rogue)
    _, _, unexpected = rb.classify_changes(changes, before, rogue)
    assert unexpected

    # function identity drift is refused
    rogue = copy.deepcopy(after)
    rogue["functions"][0]["identity"]["prosrc_md5"] = "0" * 32
    changes = snap.diff_states(before, rogue)
    _, _, unexpected = rb.classify_changes(changes, before, rogue)
    assert any(c.get("field") == "prosrc_md5" for c in unexpected)


def test_build_restore_plan_scoped_and_precise():
    before = _base_state()
    after = _apply_075_delta(before)
    plan = rb.build_restore_plan(before, after)
    assert plan["roles_to_drop"] == [rb.ADMIN_ROLE, rb.OBSERVER_ROLE]
    joined = "\n".join(item["sql"] for item in plan["statements"])
    # the plan may only name the audited surface and the two 075 roles
    assert "prospective" not in joined
    assert "DROP OWNED BY" not in joined
    assert joined.count("DROP ROLE") == 2
    assert "SECURITY INVOKER" in joined and "RESET ALL" in joined
    # pre-existing roles must never appear as a DROP target
    assert 'DROP ROLE "postgres"' not in joined
    assert 'DROP ROLE "trad_bot"' not in joined
    # every P2 relation/function that differs is covered
    assert "ON TABLE" in joined and "ON FUNCTION" in joined

    # a plan against an identical state is empty
    empty = rb.build_restore_plan(before, copy.deepcopy(before))
    assert empty["statements"] == []
    assert empty["roles_to_drop"] == []


def test_restore_plan_refuses_missing_grantee_role():
    before = _base_state()
    after = _apply_075_delta(before)
    after["relations"][0]["acl_effective"].append([OWNER, "ghost_role", "SELECT", False])
    with pytest.raises(rb.FailClosed) as exc:
        rb.build_restore_plan(before, after)
    assert "P2_RB_GRANTEE_ROLE_MISSING" in str(exc.value)


# --------------------------------------------------------------------------- #
# static scope guards
# --------------------------------------------------------------------------- #
def _code_only(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    return source.split('"""', 2)[-1]


def test_snapshot_tool_is_read_only():
    code = _code_only(SNAPSHOT_TOOL)
    assert not re.search(
        r"(?im)^\s*(grant|revoke|drop|create|alter|insert\s+into|update\s|delete\s+from|truncate)\b",
        code,
    )
    source = SNAPSHOT_TOOL.read_text(encoding="utf-8")
    assert "default_transaction_read_only = on" in source
    assert not re.search(r"(?i)(from|join)\s+pg_authid\b", source), (
        "must never read password hashes"
    )
    assert "rolpassword" not in source


def test_rollback_tool_scope_statics():
    source = ROLLBACK_TOOL.read_text(encoding="utf-8")
    code = _code_only(ROLLBACK_TOOL)
    assert "prospective" not in source, "rollback must never name Point-B objects"
    assert "DROP OWNED BY" not in code, "the destructive legacy recovery is forbidden"
    assert not re.search(
        r"(?im)^\s*(insert\s+into|update\s|delete\s+from|drop\s+table|alter\s+table|truncate)\b",
        code,
    )
    # SQL it generates is limited to GRANT/REVOKE/ALTER FUNCTION/DROP ROLE
    assert "_GRANT_KIND" in source and "DROP ROLE" in source
    assert "APPROVED_FREEZE_TS" not in source
    assert not re.search(r"(?i)(from|join)\s+pg_authid\b", source)


def test_rollback_transaction_contract_statics():
    """PR #191 atomicity contract, statically enforced: run_rollback owns the
    transaction, the whole restore + verification share one uncommitted
    transaction, exactly one commit exists and it runs AFTER verification."""
    source = ROLLBACK_TOOL.read_text(encoding="utf-8")
    code = _code_only(ROLLBACK_TOOL)

    # _execute_plan must never commit/roll back — the caller owns the lifecycle
    execute_plan = code.split("def _execute_plan", 1)[1].split("\ndef ", 1)[0]
    assert not re.search(r"\.(commit|rollback)\(", execute_plan)

    # exactly ONE commit statement in the module, inside run_rollback only
    commits = re.findall(r"(?m)^\s*conn\.commit\(\)\s*$", source)
    assert len(commits) == 1, f"expected exactly one commit, found {len(commits)}"
    before_run, run_body = source.split("def run_rollback(", 1)
    assert not re.search(r"(?m)^\s*conn\.commit\(\)\s*$", before_run)
    commit_pos = re.search(r"(?m)^\s*conn\.commit\(\)\s*$", run_body).start()

    # _restore_and_verify verifies BEFORE run_rollback commits:
    # execute -> capture -> compare_states, all in the uncommitted transaction
    verify_body = source.split("def _restore_and_verify", 1)[1].split("\ndef ", 1)[0]
    assert not re.search(r"(?m)^\s*conn\.commit\(\)\s*$", verify_body)
    assert "manage_transaction=False" in verify_body
    assert verify_body.index("_execute_plan(") < verify_body.index("compare_states(")
    assert run_body.index("_restore_and_verify(") < commit_pos

    # every non-success exit rolls the transaction back
    assert "except BaseException:" in run_body
    assert run_body.index("except BaseException:") > commit_pos
    assert "conn.rollback()" in run_body.split("except BaseException:", 1)[1]

    # driver/transaction boundary guards
    assert "P2_RB_CONNECTION_AUTOCOMMIT" in source
    assert "P2_RB_TRANSACTION_NOT_IDLE" in source
    assert "_assert_transaction_boundary" in source


def test_docs_cover_transaction_atomicity_contract():
    hardening = HARDENING_DOC.read_text(encoding="utf-8")
    for needle in (
        "Transaction atomicity",
        "P2_RB_CONNECTION_AUTOCOMMIT",
        "P2_RB_TRANSACTION_NOT_IDLE",
        "autocommit",
        "fail_point",
    ):
        assert needle in hardening, f"hardening doc must document: {needle}"


def test_runbook_documents_corrected_procedure():
    doc = RUNBOOK.read_text(encoding="utf-8")
    # the destructive legacy claim is gone from every executable SQL block
    sql_blocks = re.findall(r"```sql\n(.*?)```", doc, re.S)
    assert sql_blocks
    for block in sql_blocks:
        assert "DROP OWNED BY" not in block, "legacy recovery leaked into a SQL block"
    # preflight, strict order, checksums, STOP conditions, separate approval
    for needle in (
        "Preflight",
        "pre-075 ACL snapshot",
        ".sha256",
        "server_version_num",
        "git rev-parse HEAD",
        "STOP",
        "P2_RB_",
        "RESTORED_AND_VERIFIED",
        "P2_RB_NOTHING_TO_ROLL_BACK",
        "ABORTED_NO_CHANGES",
    ):
        assert needle in doc, f"runbook must document: {needle}"
    # strict order 074 -> snapshot -> 075 is stated and is the apply order
    assert "074  ->  pre-075 ACL snapshot" in doc
    section1 = doc.split("## 1. Apply order", 1)[1].split("## 2.", 1)[0]
    i_074 = section1.index("074_htf_baseline_short_phase2_registration.sql")
    i_snap = section1.index("snapshot of §0.4")
    i_075 = section1.index("075_htf_baseline_short_phase2_privilege_hardening.sql")
    assert i_074 < i_snap < i_075
    # rollback requires its own operator approval
    assert re.search(r"rollback.*approval|approval.*rollback", doc, re.I | re.S)
    # emergency observer stop stays a separate action
    assert "Emergency observer stop" in doc or "is NOT a rollback" in doc


def test_runbook_verification_contract_still_machine_parsable():
    doc = RUNBOOK.read_text(encoding="utf-8")
    block = doc.split("-- VERIFICATION_SQL_BEGIN", 1)[1].split("-- VERIFICATION_SQL_END", 1)[0]
    statements = [s for s in block.split(";") if s.strip() and not s.strip().startswith("--")]
    assert statements
    for stmt in statements:
        assert "AS check" in stmt and "AS expected" in stmt and "AS actual" in stmt


def test_docs_cover_audit_and_matrix():
    audit = AUDIT_DOC.read_text(encoding="utf-8")
    for needle in (
        "privilege delta",
        "pre-existing",
        "SECURITY DEFINER",
        "DROP OWNED BY",
        "WITH GRANT OPTION",
    ):
        assert needle in audit
    hardening = HARDENING_DOC.read_text(encoding="utf-8")
    for test_id in (
        "T01", "T02", "T03", "T04", "T05", "T06", "T07", "T08", "T09", "T10",
        "T11", "T12", "T13", "T14", "T15", "T16", "T17", "T18", "T19", "T20",
    ):
        assert test_id in hardening, f"matrix must document {test_id}"
    assert "FAIL CLOSED" in hardening
