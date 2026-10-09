import ast
from pathlib import Path


def test_me_rl_v1_removed_from_generic_observer():
    source = Path("scanner_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    factories = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_get_research_observer"
    ]

    assert len(factories) == 1
    factory = factories[0]
    factory_source = ast.get_source_segment(source, factory)

    assert factory_source is not None
    assert "ME_RL_V1_CONFIG" not in factory_source

    # Other research registrations must remain enabled.
    assert "ME_RL_V2_CONFIG" in factory_source
    assert "SRR_CONFIG" in factory_source
    assert "VC_CONFIG" in factory_source

    # The generic observer itself must remain active.
    assert "ResearchObserver(research_repo, experiments)" in factory_source


def test_me_rl_v1_scanner_and_shadow_control_preserved():
    source = Path("app/scanners/orchestrator.py").read_text(
        encoding="utf-8"
    )

    assert '"MOMENTUM_EXHAUSTION_REVERSE_LONG_V1": MomentumExhaustionReverseLongV1Scanner()' in source
    assert '"MOMENTUM_EXHAUSTION_REVERSE_LONG_V1",' in source
    assert '"ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1": MERLongCloseLocationOOSValidationV1Scanner()' in source
