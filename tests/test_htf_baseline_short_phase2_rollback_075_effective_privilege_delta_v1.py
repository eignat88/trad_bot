"""Issue #181 (HTF_PHASE2_ROLLBACK_075_EFFECTIVE_PRIVILEGE_DELTA_FIX_V1) —
regression tests for the effective-privilege delta classification of the
migration 075 rollback tool.

The production-like shape (mirrors the VPS IT evidence
``/var/lib/postgresql/17/issue181_it/htf_p2_parity_*``):

* application roles ``trad_bot``, ``analytics_runner``, ``analytics_agent``;
* default ACL of schema ``research`` (migrations 049/050):
  ``trad_bot=arw`` on tables and ``trad_bot=rU`` on sequences, materialized
  on the Phase-2 objects at CREATE time (074);
* ``EXECUTE`` on the Phase-2 functions through the PUBLIC default
  (``acldefault('f')``).

Migration 075 then revokes exactly those grants, producing 26 expected
effective-privilege changes (15 EXECUTE + 9 SELECT/INSERT/UPDATE + 2
SELECT/USAGE).  All of them must be recognized as the audited 075 ACL delta
(unexplained = 0), and anything the delta does not imply — foreign GRANT,
foreign REVOKE, role drift, membership drift, unknown object, orphan
effective-privilege flip, corrupted snapshot — must keep failing closed.

Part A (no database) exercises the classifier on synthetic states; Part B
runs the same contract end-to-end on a disposable ``trad_bot_p2_it_*``
database and skips when no isolated test server is reachable.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "research"))

import htf_baseline_short_phase2_acl_snapshot_v1 as snap  # noqa: E402
import htf_baseline_short_phase2_rollback_075_v1 as rb  # noqa: E402

OWNER = "postgres"
APP_ROLES = ("trad_bot", "analytics_runner", "analytics_agent")
TABLE_KEYS = sorted(rb.P2_TABLE_KEYS)
SEQ_KEY = sorted(rb.P2_SEQUENCE_KEYS)[0]
FN_KEYS = sorted(rb.P2_FUNCTION_KEYS)
DB_KEY = "database:trad_bot_p2_it_effdelta_unit"
SCH_KEY = "schema:research"

# the 075 delta fields required by the acceptance criteria
N_EXEC = 15  # 5 Phase-2 functions x 3 application roles (EXECUTE via PUBLIC)
N_TABLE = 9  # 3 Phase-2 tables x 3 privileges (trad_bot=arw default ACL)
N_SEQ = 2  # sequence x 2 privileges (trad_bot=rU default ACL)
N_EFFECTIVE = N_EXEC + N_TABLE + N_SEQ  # 26 expected changes


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


# --------------------------------------------------------------------------- #
# Part A — synthetic production-like states (no database)
# --------------------------------------------------------------------------- #
def _prodlike_pre_state(*, memberships: list | None = None) -> dict:
    """Pre-075 state with production-like application roles and ACL.

    Mirrors ``htf_p2_parity_pre075_acl.json``: materialized table/sequence
    ACLs carrying ``trad_bot=arw`` / ``trad_bot=rU`` (the 049/050 default
    privileges of schema research), PUBLIC EXECUTE on the functions, and the
    per-role effective-privilege view for ``trad_bot`` /
    ``analytics_runner`` / ``analytics_agent`` / ``postgres``.
    """
    owner_table_acl = _acl(
        (OWNER, OWNER, "DELETE", False), (OWNER, OWNER, "INSERT", False),
        (OWNER, OWNER, "MAINTAIN", False), (OWNER, OWNER, "REFERENCES", False),
        (OWNER, OWNER, "SELECT", False), (OWNER, OWNER, "TRIGGER", False),
        (OWNER, OWNER, "TRUNCATE", False), (OWNER, OWNER, "UPDATE", False),
    )
    tables = [
        {
            "key": key,
            "kind": "relation",
            "identity": {"oid": 20000 + i, "name": key.split(".", 1)[1], "owner": OWNER},
            "affected": True,
            "acl_is_null": False,
            "acl_explicit": [list(e) for e in owner_table_acl]
            + _acl((OWNER, "trad_bot", "INSERT", False),
                   (OWNER, "trad_bot", "SELECT", False),
                   (OWNER, "trad_bot", "UPDATE", False)),
            "acl_effective": [list(e) for e in owner_table_acl]
            + _acl((OWNER, "trad_bot", "INSERT", False),
                   (OWNER, "trad_bot", "SELECT", False),
                   (OWNER, "trad_bot", "UPDATE", False)),
        }
        for i, key in enumerate(TABLE_KEYS)
    ]
    tables.append(
        {
            "key": SEQ_KEY,
            "kind": "sequence",
            "identity": {"oid": 20100, "name": SEQ_KEY.split(".", 1)[1], "owner": OWNER},
            "affected": True,
            "acl_is_null": False,
            "acl_explicit": _acl(
                (OWNER, OWNER, "SELECT", False), (OWNER, OWNER, "UPDATE", False),
                (OWNER, OWNER, "USAGE", False),
                (OWNER, "trad_bot", "SELECT", False), (OWNER, "trad_bot", "USAGE", False),
            ),
            "acl_effective": _acl(
                (OWNER, OWNER, "SELECT", False), (OWNER, OWNER, "UPDATE", False),
                (OWNER, OWNER, "USAGE", False),
                (OWNER, "trad_bot", "SELECT", False), (OWNER, "trad_bot", "USAGE", False),
            ),
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
            # EXECUTE for PUBLIC is the PostgreSQL function default
            "acl_effective": _acl(
                (OWNER, "PUBLIC", "EXECUTE", False), (OWNER, OWNER, "EXECUTE", False)
            ),
        }
        for i, key in enumerate(FN_KEYS)
    ]

    def eff_tables(role: str, value: bool) -> dict:
        return {key: {"SELECT": value, "INSERT": value, "UPDATE": value, "DELETE": False}
                for key in TABLE_KEYS}

    effective = {
        OWNER: {
            **{key: {"SELECT": True, "INSERT": True, "UPDATE": True, "DELETE": True}
               for key in TABLE_KEYS},
            SEQ_KEY: {"USAGE": True, "SELECT": True},
            **{key: {"EXECUTE": True} for key in FN_KEYS},
            DB_KEY: {"CONNECT": True, "CREATE": True, "TEMP": True},
            SCH_KEY: {"USAGE": True, "CREATE": True},
        },
        "trad_bot": {
            **eff_tables("trad_bot", True),
            SEQ_KEY: {"USAGE": True, "SELECT": True},
            **{key: {"EXECUTE": True} for key in FN_KEYS},
            DB_KEY: {"CONNECT": True, "CREATE": False, "TEMP": True},
            SCH_KEY: {"USAGE": True, "CREATE": False},
        },
        "analytics_runner": {
            **eff_tables("analytics_runner", False),
            SEQ_KEY: {"USAGE": False, "SELECT": False},
            **{key: {"EXECUTE": True} for key in FN_KEYS},
            DB_KEY: {"CONNECT": True, "CREATE": False, "TEMP": True},
            SCH_KEY: {"USAGE": True, "CREATE": False},
        },
        "analytics_agent": {
            **eff_tables("analytics_agent", False),
            SEQ_KEY: {"USAGE": False, "SELECT": False},
            **{key: {"EXECUTE": True} for key in FN_KEYS},
            DB_KEY: {"CONNECT": True, "CREATE": False, "TEMP": True},
            SCH_KEY: {"USAGE": True, "CREATE": False},
        },
    }
    return {
        "database": {
            "key": DB_KEY,
            "kind": "database",
            "identity": {"oid": 16384, "name": DB_KEY.split(":", 1)[1], "owner": OWNER},
            "acl_is_null": False,
            "acl_explicit": _acl(
                (OWNER, "PUBLIC", "CONNECT", False), (OWNER, "PUBLIC", "TEMPORARY", False),
                (OWNER, OWNER, "CONNECT", False), (OWNER, OWNER, "CREATE", False),
                (OWNER, OWNER, "TEMPORARY", False), (OWNER, "trad_bot", "CONNECT", False),
            ),
            "acl_effective": _acl(
                (OWNER, "PUBLIC", "CONNECT", False), (OWNER, "PUBLIC", "TEMPORARY", False),
                (OWNER, OWNER, "CONNECT", False), (OWNER, OWNER, "CREATE", False),
                (OWNER, OWNER, "TEMPORARY", False), (OWNER, "trad_bot", "CONNECT", False),
            ),
        },
        "schemas": [
            {
                "key": SCH_KEY,
                "kind": "schema",
                "identity": {"oid": 2200, "name": "research", "owner": OWNER},
                "affected": True,
                "acl_is_null": False,
                "acl_explicit": _acl(
                    (OWNER, OWNER, "CREATE", False), (OWNER, OWNER, "USAGE", False),
                    (OWNER, "trad_bot", "USAGE", False),
                ),
                "acl_effective": _acl(
                    (OWNER, OWNER, "CREATE", False), (OWNER, OWNER, "USAGE", False),
                    (OWNER, "trad_bot", "USAGE", False),
                ),
            }
        ],
        "relations": tables,
        "functions": functions,
        "roles": {
            OWNER: _role_attrs(superuser=True, inherit=True, can_login=True),
            "trad_bot": _role_attrs(),
            "analytics_runner": _role_attrs(),
            "analytics_agent": _role_attrs(),
        },
        "memberships": [list(m) for m in (memberships or [])],
        "default_privileges": [
            {
                "identity": {"oid": 16460, "grantee_role": OWNER, "schema": "research",
                             "object_type": "r"},
                "acl_is_null": False,
                "acl_explicit": _acl(
                    (OWNER, "trad_bot", "INSERT", False), (OWNER, "trad_bot", "SELECT", False),
                    (OWNER, "trad_bot", "UPDATE", False),
                ),
            },
            {
                "identity": {"oid": 16461, "grantee_role": OWNER, "schema": "research",
                             "object_type": "S"},
                "acl_is_null": False,
                "acl_explicit": _acl(
                    (OWNER, "trad_bot", "SELECT", False), (OWNER, "trad_bot", "USAGE", False),
                ),
            },
        ],
        "shdepend": [],
        "effective_privileges": effective,
        "capture_info": {"server_version": "17.11", "server_version_num": 170011},
    }


def _apply_075_delta_prodlike(state: dict) -> dict:
    """Synthesize the EXACT audited 075 delta (ACL + effective) on ``state``.

    26 effective-privilege changes are produced exactly as migration 075 does
    on a production-like database:
      * 15 x EXECUTE (5 functions x 3 app roles) via the PUBLIC default,
      * 9 x SELECT/INSERT/UPDATE (3 tables x trad_bot=arw),
      * 2 x SELECT/USAGE (sequence x trad_bot=rU).
    """
    out = copy.deepcopy(state)
    out["roles"][rb.OBSERVER_ROLE] = dict(rb._EXPECTED_ROLE_ATTRIBUTES[rb.OBSERVER_ROLE])
    out["roles"][rb.ADMIN_ROLE] = dict(rb._EXPECTED_ROLE_ATTRIBUTES[rb.ADMIN_ROLE])
    db = out["database"]["acl_effective"]
    db += _acl(
        (OWNER, rb.OBSERVER_ROLE, "CONNECT", False),
        (OWNER, rb.ADMIN_ROLE, "CONNECT", False),
    )
    out["database"]["acl_explicit"] = [list(e) for e in db]
    sch = out["schemas"][0]["acl_effective"]
    sch += _acl(
        (OWNER, rb.OBSERVER_ROLE, "USAGE", False),
        (OWNER, rb.ADMIN_ROLE, "USAGE", False),
    )
    out["schemas"][0]["acl_explicit"] = [list(e) for e in sch]
    for rel in out["relations"]:
        if rel["key"] in rb.P2_TABLE_KEYS:
            rel["acl_effective"] = [
                e for e in rel["acl_effective"] if e[1] != "trad_bot"
            ] + _acl(
                (OWNER, rb.OBSERVER_ROLE, "SELECT", False),
                (OWNER, rb.ADMIN_ROLE, "SELECT", False),
            )
            rel["acl_explicit"] = [list(e) for e in rel["acl_effective"]]
        elif rel["key"] == SEQ_KEY:
            rel["acl_effective"] = [e for e in rel["acl_effective"] if e[1] != "trad_bot"]
            rel["acl_explicit"] = [list(e) for e in rel["acl_effective"]]
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
    # the 26 expected effective-privilege consequences
    eff = out["effective_privileges"]
    for role in APP_ROLES:
        for key in FN_KEYS:
            eff[role][key]["EXECUTE"] = False
    for key in TABLE_KEYS:
        for priv in ("SELECT", "INSERT", "UPDATE"):
            eff["trad_bot"][key][priv] = False
    eff["trad_bot"][SEQ_KEY]["USAGE"] = False
    eff["trad_bot"][SEQ_KEY]["SELECT"] = False
    return out


def _classify(before: dict, after: dict):
    changes = snap.diff_states(before, after)
    explained, derived, unexpected = rb.classify_changes(changes, before, after)
    return changes, explained, derived, unexpected


def _effective_changes(changes: list) -> list:
    return [c for c in changes if c["kind"] == "effective_privilege"]


def test_prodlike_all_26_changes_confirmed_by_the_075_acl_delta():
    before = _prodlike_pre_state()
    after = _apply_075_delta_prodlike(before)
    changes, explained, derived, unexpected = _classify(before, after)

    # the acceptance contract: nothing unexplained, nothing unclassified
    assert unexpected == [], unexpected
    assert derived == [], derived
    eff = [c for c in explained if c["kind"] == "effective_privilege"]
    assert len(eff) == N_EFFECTIVE, Counter((c["privilege"], c["role"]) for c in eff)

    # every one is confirmed by the exact ACL delta and carries its derivation
    for change in eff:
        assert change["classification"] == "confirmed_by_075_acl_delta"
        assert change["covered_by"], change
        assert change["before"] is True and change["after"] is False

    # exact breakdown demanded by the issue: 15 EXECUTE / 9 SIU / 2 seq
    per_priv = Counter(c["privilege"] for c in eff)
    assert per_priv["EXECUTE"] == N_EXEC
    assert per_priv["SELECT"] == 4  # 3 tables + 1 sequence privilege
    assert per_priv["INSERT"] == 3
    assert per_priv["UPDATE"] == 3
    assert per_priv["USAGE"] == 1
    per_role = Counter(c["role"] for c in eff)
    assert per_role == Counter({"trad_bot": 16, "analytics_runner": 5, "analytics_agent": 5})

    # and the derivation names real 075 ACL delta entries
    joined = json.dumps(eff, sort_keys=True)
    assert "PUBLIC" in joined  # EXECUTE revoked from PUBLIC covers the 15 flips
    assert "trad_bot" in joined  # the default-ACL grants cover the 11 flips

    # the rest of the delta is still recognized
    explained_kinds = Counter(c["kind"] for c in explained)
    assert explained_kinds["role"] == 2
    assert explained_kinds["acl_entry"] >= 1
    assert explained_kinds["function_config"] == 4


def test_prodlike_snapshot_matches_vps_artifact_shape():
    """Guard: the synthetic fixture keeps mirroring the VPS parity evidence."""
    before = _prodlike_pre_state()
    keys = snap._objects_by_key(before)
    for key in TABLE_KEYS:
        entry = [e for e in keys[key]["acl_effective"] if e[1] == "trad_bot"]
        assert {e[2] for e in entry} == {"SELECT", "INSERT", "UPDATE"}  # trad_bot=arw
    seq = [e for e in keys[SEQ_KEY]["acl_effective"] if e[1] == "trad_bot"]
    assert {e[2] for e in seq} == {"USAGE", "SELECT"}  # trad_bot=rU
    for key in FN_KEYS:
        assert [e for e in keys[key]["acl_effective"] if e[1] == "PUBLIC"] == [
            [OWNER, "PUBLIC", "EXECUTE", False]
        ]  # EXECUTE via PUBLIC


def test_orphan_effective_flip_without_acl_delta_is_refused():
    """A privilege flip the 075 ACL delta does not imply must FAIL CLOSED —
    it may never be accepted as silently 'derived'."""
    before = _prodlike_pre_state()
    after = _apply_075_delta_prodlike(before)

    # (a) True -> False with no covering removal
    rogue = copy.deepcopy(after)
    rogue["effective_privileges"]["analytics_agent"][DB_KEY]["CONNECT"] = False
    _, _, _, unexpected = _classify(before, rogue)
    assert any(c["kind"] == "effective_privilege" for c in unexpected), unexpected

    # (b) False -> True with no covering addition (privilege appears from nowhere)
    rogue = copy.deepcopy(after)
    rogue["effective_privileges"]["analytics_runner"][TABLE_KEYS[0]]["DELETE"] = True
    _, _, _, unexpected = _classify(before, rogue)
    assert any(c["kind"] == "effective_privilege" for c in unexpected), unexpected

    # (c) wrong direction: the covering 075 entries are REMOVALS, but the
    # flip claims a privilege appeared — only an addition could explain it
    memberships = [["trad_bot", "analytics_agent", False]]
    before_inh = _prodlike_pre_state(memberships=memberships)
    after_inh = _apply_075_delta_prodlike(before_inh)
    after_inh["effective_privileges"]["analytics_agent"][TABLE_KEYS[0]]["SELECT"] = True
    _, _, _, unexpected = _classify(before_inh, after_inh)
    assert any(
        c["kind"] == "effective_privilege" and c["role"] == "analytics_agent"
        for c in unexpected
    ), unexpected


def test_foreign_grant_and_revoke_are_refused():
    before = _prodlike_pre_state()
    after = _apply_075_delta_prodlike(before)

    # (a) посторонний GRANT: SELECT to analytics_agent on a Phase-2 table
    rogue = copy.deepcopy(after)
    rogue["relations"][0]["acl_effective"].append([OWNER, "analytics_agent", "SELECT", False])
    rogue["effective_privileges"]["analytics_agent"][rogue["relations"][0]["key"]]["SELECT"] = True
    _, _, _, unexpected = _classify(before, rogue)
    kinds = {c["kind"] for c in unexpected}
    assert "acl_entry" in kinds and "effective_privilege" in kinds

    # (b) посторонний REVOKE: the owner's own table grant is removed
    rogue = copy.deepcopy(after)
    removed = rogue["relations"][0]["acl_effective"].pop(0)
    rogue["effective_privileges"][OWNER][rogue["relations"][0]["key"]][removed[2]] = False
    _, _, _, unexpected = _classify(before, rogue)
    kinds = {c["kind"] for c in unexpected}
    assert "acl_entry" in kinds and "effective_privilege" in kinds

    # (c) посторонний REVOKE of a pre-075 grant (049's trad_bot schema USAGE)
    rogue = copy.deepcopy(after)
    rogue["schemas"][0]["acl_effective"] = [
        e for e in rogue["schemas"][0]["acl_effective"]
        if not (e[1] == "trad_bot" and e[2] == "USAGE")
    ]
    rogue["effective_privileges"]["trad_bot"][SCH_KEY]["USAGE"] = False
    _, _, _, unexpected = _classify(before, rogue)
    kinds = {c["kind"] for c in unexpected}
    assert "acl_entry" in kinds and "effective_privilege" in kinds


def test_missing_075_grant_fails_closed_delta_completeness():
    """A post-075 foreign REVOKE of a grant 075 created removes a delta
    addition — invisible to a before-vs-current diff.  The completeness gate
    must still refuse: a post-075 state always carries the full 075 surface."""
    before = _prodlike_pre_state()
    after = _apply_075_delta_prodlike(before)

    # the observer's SELECT on one table was revoked again after 075
    rogue = copy.deepcopy(after)
    key = TABLE_KEYS[0]
    rogue["relations"][0]["acl_effective"] = [
        e for e in rogue["relations"][0]["acl_effective"]
        if not (e[1] == rb.OBSERVER_ROLE and e[2] == "SELECT")
    ]
    _, explained, _, unexpected = _classify(before, rogue)
    assert any(
        c["kind"] == "delta_completeness"
        and c["action"] == "missing_expected_acl_entry"
        and c["expected"] == [rb.OBSERVER_ROLE, "SELECT"]
        and c["object"] == key
        for c in unexpected
    ), unexpected

    # the same state also passes through run_rollback's fail-closed refusal
    # contract: nothing is ever planned from an unexplained state
    assert not any(c["kind"] == "delta_completeness" for c in explained)

    # while the COMPLETE 075 surface passes the gate
    _, _, _, unexpected = _classify(before, after)
    assert unexpected == [], unexpected

    # a state identical to the snapshot does NOT trigger the gate (that is a
    # NothingToRollBack, not a conflicting state)
    _, _, _, unexpected = _classify(before, copy.deepcopy(before))
    assert unexpected == [], unexpected


def test_role_drift_membership_drift_and_unknown_object_refused():
    before = _prodlike_pre_state()
    after = _apply_075_delta_prodlike(before)

    # (a) role attribute drift on an application role
    rogue = copy.deepcopy(after)
    rogue["roles"]["analytics_runner"]["can_login"] = True
    _, _, _, unexpected = _classify(before, rogue)
    assert any(c["kind"] == "role" for c in unexpected), unexpected

    # (b) role membership drift
    rogue = copy.deepcopy(after)
    rogue["memberships"] = [["trad_bot", "analytics_agent", False]]
    _, _, _, unexpected = _classify(before, rogue)
    assert any(c["kind"] == "membership" for c in unexpected), unexpected

    # (c) неизвестный объект: a new research relation appears
    rogue = copy.deepcopy(after)
    rogue["relations"].append(
        {
            "key": "relation:research.p2hardening_effdelta_decoy",
            "kind": "relation",
            "identity": {"oid": 29999, "name": "p2hardening_effdelta_decoy", "owner": OWNER},
            "affected": False,
            "acl_is_null": True,
            "acl_explicit": [],
            "acl_effective": _acl((OWNER, OWNER, "SELECT", False)),
        }
    )
    rogue["effective_privileges"]["trad_bot"]["relation:research.p2hardening_effdelta_decoy"] = {
        "SELECT": True
    }
    _, _, _, unexpected = _classify(before, rogue)
    kinds = {c["kind"] for c in unexpected}
    assert "object_presence" in kinds and "effective_privilege" in kinds


def test_effective_flip_via_role_inheritance_is_confirmed():
    """Grants revoked from trad_bot also revoke the privileges analytics_agent
    inherits from it — the delta implies those flips through pg_auth_members."""
    memberships = [["trad_bot", "analytics_agent", False]]
    before = _prodlike_pre_state(memberships=memberships)
    for key in TABLE_KEYS:
        for priv in ("SELECT", "INSERT", "UPDATE"):
            before["effective_privileges"]["analytics_agent"][key][priv] = True
    after = _apply_075_delta_prodlike(before)
    for key in TABLE_KEYS:
        for priv in ("SELECT", "INSERT", "UPDATE"):
            after["effective_privileges"]["analytics_agent"][key][priv] = False
    changes, explained, derived, unexpected = _classify(before, after)
    assert unexpected == [], unexpected
    inherited = [
        c for c in explained
        if c["kind"] == "effective_privilege" and c["role"] == "analytics_agent"
    ]
    assert len(inherited) == N_TABLE + 5  # 9 inherited + 5 own EXECUTE

    # without the membership the very same flips are NOT explainable
    rogue_before = _prodlike_pre_state()
    for key in TABLE_KEYS:
        for priv in ("SELECT", "INSERT", "UPDATE"):
            rogue_before["effective_privileges"]["analytics_agent"][key][priv] = True
    rogue_after = _apply_075_delta_prodlike(rogue_before)
    for key in TABLE_KEYS:
        for priv in ("SELECT", "INSERT", "UPDATE"):
            rogue_after["effective_privileges"]["analytics_agent"][key][priv] = False
    _, _, _, unexpected = _classify(rogue_before, rogue_after)
    assert any(
        c["kind"] == "effective_privilege" and c["role"] == "analytics_agent"
        for c in unexpected
    ), unexpected


class _StubConnection:
    """Minimal connection for the snapshot gate (no driver, no database)."""

    autocommit = False
    info = None


def test_corrupted_snapshot_fails_closed(tmp_path):
    before = _prodlike_pre_state()
    snapshot = snap.build_snapshot(before, {"database": "unit"})
    path = Path(tmp_path) / "effdelta.json"
    snap.write_snapshot(path, snapshot)

    # (a) file tamper with stale sidecar
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"trad_bot"', '"trad_bot_tampered"', 1), encoding="utf-8")
    with pytest.raises(snap.SnapshotIntegrityError) as exc:
        rb.run_rollback(_StubConnection(), path, dry_run=True)
    assert "SNAPSHOT_FILE_CHECKSUM_MISMATCH" in str(exc.value)

    # (b) payload tamper with refreshed sidecar
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["state"]["effective_privileges"]["trad_bot"][SEQ_KEY]["USAGE"] = True
    snap.write_snapshot(path, tampered)
    with pytest.raises(snap.SnapshotIntegrityError) as exc:
        rb.run_rollback(_StubConnection(), path, dry_run=True)
    assert "SNAPSHOT_PAYLOAD_CHECKSUM_MISMATCH" in str(exc.value)

    # (c) missing sidecar
    snap.write_snapshot(path, snapshot)
    snap.sidecar_path(path).unlink()
    with pytest.raises(snap.SnapshotIntegrityError) as exc:
        rb.run_rollback(_StubConnection(), path, dry_run=True)
    assert "SNAPSHOT_CHECKSUM_FILE_MISSING" in str(exc.value)

    # (d) missing snapshot file is a refusal as well
    with pytest.raises(snap.SnapshotIntegrityError) as exc:
        rb.run_rollback(_StubConnection(), Path(tmp_path) / "gone.json", dry_run=True)
    assert "SNAPSHOT_FILE_MISSING" in str(exc.value)


def test_report_contract_zero_unexplained_is_machine_readable():
    """The dry-run/rollback report must expose the classification contract
    (unexplained = 0) that the FAIL-CLOSED acceptance gate consumes."""
    source = (ROOT / "tools" / "research" / (
        "htf_baseline_short_phase2_rollback_075_v1.py")).read_text(encoding="utf-8")
    assert '"changes_unexplained"' in source
    assert '"effective_privilege_confirmed"' in source
    assert '"unexplained": 0' in source
    # the classifier never accepts effective flips without the ACL delta proof
    assert "_effective_change_explained" in source
    assert "confirmed_by_075_acl_delta" in source


# --------------------------------------------------------------------------- #
# Part B — end-to-end on a disposable trad_bot_p2_it_* database
# --------------------------------------------------------------------------- #
DB_HOST = os.environ.get("P2_TEST_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("P2_TEST_DB_PORT", "5432"))
DB_USER = os.environ.get("P2_TEST_DB_USER", "postgres")

_DB_PATTERN = re.compile(r"\Atrad_bot_p2_it_[a-z0-9_]+\Z")
_BLOCKED = frozenset({"trad_bot", "postgres", "template0", "template1", "srr", "test"})
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

MIGRATION_074 = ROOT / "sql" / "migrations" / "074_htf_baseline_short_phase2_registration.sql"
MIGRATION_075 = ROOT / "sql" / "migrations" / "075_htf_baseline_short_phase2_privilege_hardening.sql"

if DB_HOST not in _LOOPBACK and os.environ.get("P2_TEST_DB_ISOLATION_ACK") != "yes":
    raise RuntimeError(
        "P2_TEST_DB_HOST is not loopback and isolation is not confirmed; "
        "refusing to run database-creating tests"
    )

_server_up = True
try:
    import psycopg2  # noqa: E402

    _probe = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, database="postgres", user=DB_USER, connect_timeout=4
    )
    _probe.close()
except Exception:
    _server_up = False
    psycopg2 = None  # noqa: N816

itmark = pytest.mark.skipif(
    not _server_up,
    reason=f"no isolated test PostgreSQL at {DB_HOST}:{DB_PORT}; integration skipped",
)


def _assert_safe_database(name: str) -> None:
    if name in _BLOCKED or not _DB_PATTERN.match(name):
        raise RuntimeError(f"refusing unsafe test database name: {name}")


def _admin_connect():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, database="postgres", user=DB_USER, connect_timeout=5
    )


@contextmanager
def _tracked_resources():
    created_databases: list[str] = []
    created_roles: list[str] = []
    connections: list = []
    try:
        yield {
            "databases": created_databases,
            "roles": created_roles,
            "connections": connections,
        }
    finally:
        for conn in connections:
            try:
                conn.close()
            except Exception:
                pass
        admin = _admin_connect()
        admin.autocommit = True
        try:
            with admin.cursor() as cur:
                for dbname in created_databases:
                    for attempt in range(4):
                        cur.execute(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = %s AND pid <> pg_backend_pid()",
                            (dbname,),
                        )
                        try:
                            cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
                            break
                        except Exception:
                            admin.rollback()
                            time.sleep(0.75)
                for role in list(created_roles) + [rb.ADMIN_ROLE, rb.OBSERVER_ROLE]:
                    try:
                        cur.execute(f'DROP ROLE IF EXISTS "{role}"')
                    except Exception:
                        admin.rollback()
        finally:
            admin.close()


def _ensure_role(resources, conn, role: str, ddl: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        exists = cur.fetchone() is not None
    if not exists:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
        resources["roles"].append(role)


def _provision_prodlike(resources):
    """074 under production-like roles + default ACL (049/050 semantics)."""
    name = f"trad_bot_p2_it_effdelta_{uuid.uuid4().hex[:8]}"
    _assert_safe_database(name)
    admin = _admin_connect()
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    resources["databases"].append(name)
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=name, user=DB_USER)
    conn.autocommit = False
    resources["connections"].append(conn)
    for role in APP_ROLES:
        _ensure_role(resources, conn, role, f'CREATE ROLE "{role}" NOLOGIN')
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS research")
        cur.execute(f'GRANT USAGE ON SCHEMA research TO "{APP_ROLES[0]}"')
        cur.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA research "
            "GRANT SELECT, INSERT, UPDATE ON TABLES TO trad_bot"
        )
        cur.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA research "
            "GRANT USAGE, SELECT ON SEQUENCES TO trad_bot"
        )
        cur.execute(f'GRANT CONNECT ON DATABASE "{name}" TO trad_bot')
        cur.execute(MIGRATION_074.read_text(encoding="utf-8"))
    conn.commit()
    return conn, name


def _apply_075(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(MIGRATION_075.read_text(encoding="utf-8"))
    conn.commit()


def _capture(conn) -> dict:
    return snap.capture_state(conn)


def _snapshot_file(tmp_path, state: dict, tag: str) -> Path:
    path = Path(tmp_path) / f"{tag}.json"
    snap.write_snapshot(path, snap.build_snapshot(state, {"database": tag}))
    return path


def _app_effective_breakdown(changes: list) -> Counter:
    return Counter(
        (c["privilege"], c["role"], c["object"].split(":", 1)[0])
        for c in changes
        if c["kind"] == "effective_privilege" and c["role"] in APP_ROLES
    )


@itmark
def test_it_prodlike_dry_run_confirms_the_26_expected_changes(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_prodlike(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "effdelta_pre")
        _apply_075(conn)
        report = rb.run_rollback(conn, snap_path, dry_run=True)

        # Acceptance: dry-run PASS, all 26 expected changes classified, 0 unexplained
        assert report["verdict"] == "DRY_RUN"
        assert report["changes_unexplained"] == []
        assert report["classification"]["unexplained"] == 0
        assert report["roles_to_drop"] == [rb.ADMIN_ROLE, rb.OBSERVER_ROLE]

        confirmed = [
            c for c in report["changes_explained_by_075"]
            if c["kind"] == "effective_privilege" and c["role"] in APP_ROLES
        ]
        assert report["classification"]["effective_privilege_confirmed"] >= N_EFFECTIVE
        breakdown = _app_effective_breakdown(report["changes_explained_by_075"])
        expected = Counter()
        for key in snap.PHASE2_FUNCTIONS:
            for role in APP_ROLES:
                expected[("EXECUTE", role, "function")] += 1
        for table in snap.PHASE2_RELATIONS:
            if table.endswith("_seq"):
                continue
            for priv in ("SELECT", "INSERT", "UPDATE"):
                expected[(priv, "trad_bot", "relation")] += 1
        expected[("USAGE", "trad_bot", "relation")] += 1
        expected[("SELECT", "trad_bot", "relation")] += 1
        assert breakdown == expected, breakdown
        assert sum(breakdown.values()) == N_EFFECTIVE
        assert len(confirmed) >= N_EFFECTIVE
        for change in confirmed:
            assert change["classification"] == "confirmed_by_075_acl_delta"
            assert change["covered_by"]
        # nothing is left in the opaque derived bucket either
        assert report["changes_derived"] == [
            c for c in report["changes_derived"] if c["kind"] == "acl_representation"
        ]
        conn.close()


@itmark
def test_it_prodlike_rollback_restores_and_verifies(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_prodlike(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "effdelta_restore_pre")
        _apply_075(conn)
        report = rb.run_rollback(conn, snap_path)
        assert report["verdict"] == "RESTORED_AND_VERIFIED"
        assert report["full_state_equal"] is True
        assert report["verification"]["acl_equal"] is True
        assert report["verification"]["effective_privileges_equal"] is True
        assert report["changes_unexplained"] == []

        post = _capture(conn)
        # exact ACL equality over the whole captured surface
        pre_keys = snap._objects_by_key(pre)
        post_keys = snap._objects_by_key(post)
        for key, entry in pre_keys.items():
            assert entry["acl_effective"] == post_keys[key]["acl_effective"], key

        # application roles preserved WITH their original privileges
        for role in APP_ROLES:
            assert role in post["roles"]
            assert post["roles"][role] == pre["roles"][role]
        eff = post["effective_privileges"]
        for table in snap.PHASE2_RELATIONS:
            if table.endswith("_seq"):
                continue
            for priv in ("SELECT", "INSERT", "UPDATE"):
                assert eff["trad_bot"][f"relation:{table}"][priv] is True, (table, priv)
        assert eff["trad_bot"][f"relation:{snap.PHASE2_RELATIONS[3]}"]["USAGE"] is True
        assert eff["trad_bot"][f"relation:{snap.PHASE2_RELATIONS[3]}"]["SELECT"] is True
        for fn in snap.PHASE2_FUNCTIONS:
            for role in APP_ROLES:
                assert eff[role][f"function:{fn}"]["EXECUTE"] is True, (role, fn)
        # and the 075 roles are gone
        assert rb.OBSERVER_ROLE not in post["roles"]
        assert rb.ADMIN_ROLE not in post["roles"]
        conn.close()


@itmark
def test_it_prodlike_isolation_and_data_survive_the_rollback(tmp_path):
    """The revocations of 075 stay effective until the rollback, and the
    rollback restores privileges without touching Phase-2 data."""
    with _tracked_resources() as res:
        conn, _ = _provision_prodlike(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "effdelta_iso_pre")
        _apply_075(conn)
        mid = _capture(conn)
        for role in APP_ROLES:
            for table in snap.PHASE2_RELATIONS:
                if table.endswith("_seq"):
                    continue
                for priv in ("SELECT", "INSERT", "UPDATE"):
                    eff = mid["effective_privileges"][role][f"relation:{table}"]
                    assert eff.get(priv) is False, (role, table, priv)
            for fn in snap.PHASE2_FUNCTIONS:
                eff = mid["effective_privileges"][role][f"function:{fn}"]
                assert eff.get("EXECUTE") is False, (role, fn)
        with conn.cursor() as cur:
            rows = {}
            for table in snap.PHASE2_RELATIONS:
                if table.endswith("_seq"):
                    continue
                cur.execute(f"SELECT count(*) FROM {table}")
                rows[table] = cur.fetchone()[0]
        conn.commit()  # close the read transaction; run_rollback owns the next one
        report = rb.run_rollback(conn, snap_path)
        assert report["verdict"] == "RESTORED_AND_VERIFIED"
        with conn.cursor() as cur:
            for table, count in rows.items():
                cur.execute(f"SELECT count(*) FROM {table}")
                assert cur.fetchone()[0] == count, f"rows of {table} changed"
        conn.close()


@itmark
def test_it_foreign_grant_blocks_rollback(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_prodlike(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "effdelta_grant_pre")
        _apply_075(conn)
        with conn.cursor() as cur:
            cur.execute(
                "GRANT SELECT ON research.htf_baseline_short_phase2_observation "
                "TO analytics_agent"
            )
        conn.commit()
        before = _capture(conn)  # state WITH the foreign grant
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(conn, snap_path)
        assert "P2_RB_CONFLICTING_STATE" in str(exc.value)
        after = _capture(conn)
        assert snap.payload_sha256(after) == snap.payload_sha256(before)
        conn.close()


@itmark
def test_it_foreign_revoke_blocks_rollback(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_prodlike(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "effdelta_revoke_pre")
        _apply_075(conn)
        with conn.cursor() as cur:
            cur.execute(
                "REVOKE SELECT ON research.htf_baseline_short_phase2_experiment "
                f'FROM "{rb.OBSERVER_ROLE}"'
            )
        conn.commit()
        before = _capture(conn)  # state WITH the foreign revoke
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(conn, snap_path)
        assert "P2_RB_CONFLICTING_STATE" in str(exc.value)
        after = _capture(conn)
        assert snap.payload_sha256(after) == snap.payload_sha256(before)
        conn.close()


@itmark
def test_it_role_drift_blocks_rollback(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_prodlike(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "effdelta_drift_pre")
        _apply_075(conn)
        before = _capture(conn)

        # (a) role attribute drift on trad_bot
        with conn.cursor() as cur:
            cur.execute("ALTER ROLE trad_bot NOINHERIT")
        conn.commit()
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(conn, snap_path)
        assert "P2_RB_CONFLICTING_STATE" in str(exc.value)
        with conn.cursor() as cur:
            cur.execute("ALTER ROLE trad_bot INHERIT")
        conn.commit()

        # (b) membership drift between application roles
        with conn.cursor() as cur:
            cur.execute('GRANT "trad_bot" TO "analytics_agent"')
        conn.commit()
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(conn, snap_path)
        assert "P2_RB_CONFLICTING_STATE" in str(exc.value)
        with conn.cursor() as cur:
            cur.execute('REVOKE "trad_bot" FROM "analytics_agent"')
        conn.commit()

        after = _capture(conn)
        assert snap.payload_sha256(after) == snap.payload_sha256(before)
        conn.close()


@itmark
def test_it_unknown_object_blocks_rollback(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_prodlike(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "effdelta_decoy_pre")
        _apply_075(conn)
        before = _capture(conn)
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE research.p2hardening_effdelta_decoy(x int)")
        conn.commit()
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(conn, snap_path)
        assert "P2_RB_CONFLICTING_STATE" in str(exc.value)
        with conn.cursor() as cur:
            cur.execute("DROP TABLE research.p2hardening_effdelta_decoy")
        conn.commit()
        after = _capture(conn)
        assert snap.payload_sha256(after) == snap.payload_sha256(before)
        conn.close()


@itmark
def test_it_corrupted_snapshot_blocks_rollback(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_prodlike(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "effdelta_tamper_pre")
        _apply_075(conn)
        before = _capture(conn)
        payload = json.loads(snap_path.read_text(encoding="utf-8"))
        payload["state"]["effective_privileges"]["trad_bot"] = {}
        snap.write_snapshot(snap_path, payload)  # consistent sidecar, wrong payload
        with pytest.raises(snap.SnapshotIntegrityError) as exc:
            rb.run_rollback(conn, snap_path)
        assert "SNAPSHOT_PAYLOAD_CHECKSUM_MISMATCH" in str(exc.value)
        after = _capture(conn)
        assert snap.payload_sha256(after) == snap.payload_sha256(before)
        conn.close()
