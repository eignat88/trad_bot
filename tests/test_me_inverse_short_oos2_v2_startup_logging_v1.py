"""Regression: OOS-2 V2 startup log must format its guard payload cleanly."""

import ast
import logging
from pathlib import Path


def test_oos2_v2_startup_log_formats_all_six_arguments():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "paper"
        / "me_long_v2_inverse_short_oos2_v2"
        / "production_runtime.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    startup_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "info"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and "me inverse short oos2 v2 runtime starting:" in node.args[0].value
    ]

    assert len(startup_calls) == 1
    call = startup_calls[0]
    assert len(call.args) == 7  # Message template plus six logging arguments.

    guards = {
        "max_open_positions": 12,
        "single_active_symbol": True,
        "symbol_entry_cooldown_hours": 12,
    }
    record = logging.LogRecord(
        name="oos2_v2",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=call.args[0].value,
        args=("0", False, "NONE", "DISABLED_FLAG", 10.0, guards),
        exc_info=None,
    )
    message = record.getMessage()

    assert "interval=10.0s" in message
    assert "guards=" in message
    assert "'max_open_positions': 12" in message
    assert "'symbol_entry_cooldown_hours': 12" in message
