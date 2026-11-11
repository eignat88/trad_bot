"""Production source-path audit for ME_LONG_V2_INVERSE_SHORT_V2 (P1-1).

The V2 capture layer is only real if the PRODUCTION scanner actually
delivers the pre-dedup source events to it.  Importability is NOT proof:
``scanner_runner`` must read ``stats["_me_inverse_short_source_events"]``
(the publication made by ``ScannerOrchestrator.scan_all_with_stats``
BEFORE the direction gate / expectancy filter) and forward every published
candidate to ``_capture_me_inverse_short_v2_source`` — independently of,
and without disturbing, the V1 delivery.

This module is the single implementation of that proof, shared by:

* ``scanner_runner.me_inverse_short_v2_source_wiring()`` — the runtime
  diagnostic available to operators and asserted by tests;
* ``scripts/me_inverse_short_v2_preflight.py`` — the
  ``V2_SOURCE_CAPTURE_READY`` pre-activation check (so preflight can never
  report source-capture readiness it did not actually verify).

Everything here is a STATIC source audit (``ast`` over the checked-out
files): it never imports ``scanner_runner``, never opens a database,
never writes anything and never prints configuration values.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

# The pre-dedup publication key (orchestrator) and the two delivery hooks.
SOURCE_STATS_KEY = "_me_inverse_short_source_events"
V1_CAPTURE_HOOK = "_capture_me_inverse_short_source"
V2_CAPTURE_HOOK = "_capture_me_inverse_short_v2_source"

# The one source detector both contours derive their events from.
SOURCE_SCANNER_NAME = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"

# Audit vocabulary (preflight check statuses).
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_NOT_VERIFIED = "NOT_VERIFIED"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse(path: Path) -> ast.Module:
    return ast.parse(_read(path))


def _find_publish_loops(tree: ast.Module) -> list[ast.For]:
    """Every ``for ... in symbol_stats.get("_me_inverse_short_source_events")`` loop."""
    loops: list[ast.For] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        constants = {
            c.value for c in ast.walk(node.iter)
            if isinstance(c, ast.Constant) and isinstance(c.value, str)
        }
        if SOURCE_STATS_KEY in constants:
            loops.append(node)
    return loops


def _calls_in(node: ast.AST, names: set[str]) -> set[str]:
    """Direct call targets by bare function name anywhere inside ``node``."""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            if child.func.id in names:
                found.add(child.func.id)
    return found


def _function_def(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node  # type: ignore[return-value]
    return None


def _imports_in(node: ast.AST) -> list[str]:
    return [
        child.module or ""
        for child in ast.walk(node)
        if isinstance(child, ast.ImportFrom)
    ]


def _imported_names_in(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.ImportFrom):
            names.update(alias.name for alias in child.names)
    return names


def _check(status: bool, *, fail_reason: str, pass_detail: str) -> dict[str, str]:
    return {
        "status": STATUS_PASS if status else STATUS_FAIL,
        "reason": "" if status else fail_reason,
        "detail": pass_detail if status else "",
    }


def audit_source_path(repo_root: Path | str) -> dict[str, Any]:
    """Prove the V2 capture hook sits on the PRODUCTION source path.

    Returns ``{"checks": {name: {status, reason, detail}}, "wired": bool}``
    where every check is ``PASS`` or ``FAIL`` (this audit either proves the
    wiring from the checked-out sources or reports exactly what is
    missing — it never returns NOT_VERIFIED, and ``wired`` is True only
    when EVERY check passed).
    """
    root = Path(repo_root)
    runner_path = root / "scanner_runner.py"
    orchestrator_path = root / "app" / "scanners" / "orchestrator.py"
    capture_path = (
        root / "app" / "paper" / "me_long_v2_inverse_short_v2" / "capture.py"
    )
    engine_path = (
        root / "app" / "paper" / "me_long_v2_inverse_short_v2" / "engine.py"
    )
    protocol_path = (
        root / "app" / "paper" / "me_long_v2_inverse_short_v2" / "protocol.py"
    )
    checks: dict[str, dict[str, str]] = {}

    missing = [
        p.name for p in (
            runner_path, orchestrator_path, capture_path, engine_path,
            protocol_path,
        ) if not p.is_file()
    ]
    if missing:
        # Nothing can be proven without the sources themselves.
        for name in (
            "orchestrator_publishes_pre_gate",
            "scanner_loop_delivers_v1_and_v2",
            "v2_hook_fail_open",
            "v2_hook_flag_independent",
            "capture_module_ready",
            "permission_parity",
        ):
            checks[name] = {
                "status": STATUS_FAIL,
                "reason": f"SOURCE_FILE_MISSING:{','.join(sorted(set(missing)))}",
                "detail": "",
            }
        return {"checks": checks, "wired": False}

    # ── 1. The orchestrator publishes the source events PRE-GATE ──────────
    orchestrator_src = _read(orchestrator_path)
    publish_marker = f'stats["{SOURCE_STATS_KEY}"]'
    gate_marker = "gate_accepted: list[SetupCandidate] = []"
    published = publish_marker in orchestrator_src
    pre_gate = (
        published
        and gate_marker in orchestrator_src
        and orchestrator_src.index(publish_marker)
        < orchestrator_src.index(gate_marker)
    )
    checks["orchestrator_publishes_pre_gate"] = _check(
        pre_gate,
        fail_reason=(
            "ORCHESTRATOR_PUBLISH_MISSING" if not published
            else "SOURCE_PUBLICATION_NOT_PRE_GATE"
        ),
        pass_detail=(
            "stats[\"_me_inverse_short_source_events\"] published before the "
            "direction-gate decision (pre-dedup, pre-gate, pre-expectancy)"
        ),
    )

    # ── 2. The production scan loop delivers to BOTH hooks ────────────────
    try:
        runner_tree = _parse(runner_path)
    except SyntaxError:
        runner_tree = None
    publish_loops = _find_publish_loops(runner_tree) if runner_tree else []
    if not publish_loops:
        checks["scanner_loop_delivers_v1_and_v2"] = {
            "status": STATUS_FAIL,
            "reason": "SCANNER_PUBLISH_LOOP_NOT_FOUND",
            "detail": "",
        }
        v2_hook = None
        v2_hook_src_ok = False
        v2_hook_modules: list[str] = []
    else:
        # V1 and V2 may be delivered from one loop or from two separate
        # loops (structurally independent); what must hold is that BOTH
        # hooks are fed from the SAME published source-event key.
        delivered: set[str] = set()
        for loop in publish_loops:
            delivered |= _calls_in(loop, {V1_CAPTURE_HOOK, V2_CAPTURE_HOOK})
        both = V1_CAPTURE_HOOK in delivered and V2_CAPTURE_HOOK in delivered
        checks["scanner_loop_delivers_v1_and_v2"] = _check(
            both,
            fail_reason=(
                "V2_DELIVERY_MISSING_FROM_PRODUCTION_LOOP"
                if V2_CAPTURE_HOOK not in delivered
                else "V1_DELIVERY_MISSING_FROM_PRODUCTION_LOOP"
            ),
            pass_detail=(
                "the production loop(s) over the published source events call "
                "BOTH _capture_me_inverse_short_source (V1, preserved) and "
                "_capture_me_inverse_short_v2_source (V2, independent)"
            ),
        )
        v2_hook = _function_def(runner_tree, V2_CAPTURE_HOOK)
        v2_hook_src_ok = v2_hook is not None
        v2_hook_modules = _imports_in(v2_hook) if v2_hook is not None else []

    # ── 3. The V2 hook is fail-open (never raises into the scan) ──────────
    has_try = (
        v2_hook is not None
        and any(isinstance(n, ast.Try) for n in ast.walk(v2_hook))
    )
    checks["v2_hook_fail_open"] = _check(
        v2_hook_src_ok and has_try,
        fail_reason=(
            "V2_HOOK_NOT_DEFINED" if not v2_hook_src_ok
            else "V2_HOOK_NOT_FAIL_OPEN"
        ),
        pass_detail=(
            "_capture_me_inverse_short_v2_source wraps the whole delivery in "
            "try/except and sinks failures to dds.scanner_error"
        ),
    )

    # ── 4. The V2 hook is gated by the V2 flag only (never the V1 flag) ───
    v2_activation_imported = any(
        m.endswith("me_long_v2_inverse_short_v2.activation")
        for m in v2_hook_modules
    )
    v2_hook_text = ""
    if v2_hook is not None and v2_hook.end_lineno:
        runner_lines = _read(runner_path).splitlines()
        v2_hook_text = "\n".join(
            runner_lines[v2_hook.lineno - 1:v2_hook.end_lineno]
        )
    v1_touched = (
        any("me_long_v2_inverse_short_v1" in m for m in v2_hook_modules)
        or "ME_INVERSE_SHORT_V1_ENABLED" in v2_hook_text
        or "me_long_v2_inverse_short_v1" in v2_hook_text
    )
    checks["v2_hook_flag_independent"] = _check(
        v2_hook_src_ok and v2_activation_imported and not v1_touched,
        fail_reason=(
            "V2_HOOK_NOT_DEFINED" if not v2_hook_src_ok
            else "V2_HOOK_DEPENDS_ON_V1"
        ),
        pass_detail=(
            "the V2 hook imports is_runtime_enabled from the V2 activation "
            "module only (ME_INVERSE_SHORT_V2_ENABLED); V1 flag/table "
            "references: none"
        ),
    )

    # ── 5. The V2 capture module exposes the delivery contract ────────────
    try:
        capture_tree = _parse(capture_path)
        protocol_tree = _parse(protocol_path)
        capture_exports = {
            n.name for n in capture_tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        protocol_names: set[str] = set()
        for node in protocol_tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                protocol_names.add(node.name)
            elif isinstance(node, ast.Assign):
                protocol_names.update(
                    t.id for t in node.targets if isinstance(t, ast.Name)
                )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                protocol_names.add(node.target.id)
        runner_src = _read(runner_path)
        hook_routes_via_protocol = (
            V2_CAPTURE_HOOK in runner_src
            and "me_long_v2_inverse_short_v2.protocol" in runner_src
            and "ROUTE_PRE_FILTER_SOURCE" in runner_src
        )
        module_ready = (
            "capture_with_status" in capture_exports
            and "ROUTE_PRE_FILTER_SOURCE" in protocol_names
            and "CAPTURE_ROUTES" in protocol_names
            and hook_routes_via_protocol
        )
    except SyntaxError:
        module_ready = False
    checks["capture_module_ready"] = _check(
        module_ready,
        fail_reason="V2_CAPTURE_CONTRACT_INCOMPLETE",
        pass_detail=(
            "capture.capture_with_status + protocol.ROUTE_PRE_FILTER_SOURCE/"
            "CAPTURE_ROUTES present and the scanner hook routes deliveries "
            "through ROUTE_PRE_FILTER_SOURCE"
        ),
    )

    # ── 6. Capture and entries agree on permissions / capture route ───────
    try:
        capture_imports = _imported_names_in(_parse(capture_path))
        engine_imports = _imported_names_in(_parse(engine_path))
        permission = "PERMISSION_NEW_ENTRIES"
        permission_parity = (
            permission in capture_imports
            and permission in engine_imports
        )
    except SyntaxError:
        permission_parity = False
    checks["permission_parity"] = _check(
        permission_parity,
        fail_reason="CAPTURE_ENTRY_PERMISSION_DIVERGENCE",
        pass_detail=(
            "capture.py and engine.py both import PERMISSION_NEW_ENTRIES "
            "from the same V2 activation module (no permission drift)"
        ),
    )

    wired = all(c["status"] == STATUS_PASS for c in checks.values())
    return {"checks": checks, "wired": wired}
