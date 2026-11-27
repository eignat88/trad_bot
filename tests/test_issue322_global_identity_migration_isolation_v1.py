"""Issue #322 global identity migration — isolation contract (offline).

Asserts the migration core is DEFAULT-OFF and islanded:

* no production module imports ``scanner_signal_identity_migration_v1``;
* ``git diff`` shows ZERO modified production files (the migration core itself
  is the only allowed change; every other production file stays untouched);
* the frozen files (prospective observer, ME_SHORT #323 adapter, repository,
  scanner_runner, scanners) are not part of any change;
* the default configuration performs no writes, not even through a
  persistence hook.

No database, no network; temp state stays inside pytest's ``tmp_path``.
"""

import subprocess
from pathlib import Path

from app.research.scanner_signal_identity_migration_v1 import (
    MigrationConfig,
    MigrationIdentityCore,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "scanner_signal_identity_migration_v1"

PROTECTED = (
    "app/research/prospective_observer.py",
    "app/research/scanner_signal_integrity_observer.py",
    "app/db/repository.py",
    "scanner_runner.py",
)


def _production_python_files() -> list[Path]:
    files = [p for p in (ROOT / "app").rglob("*.py")]
    for top in ("scanner_runner.py", "bot.py", "paper_runner.py",
                "me_inverse_short_runner.py"):
        candidate = ROOT / top
        if candidate.exists():
            files.append(candidate)
    return files


def test_no_production_module_imports_the_migration_core():
    offenders = []
    for path in _production_python_files():
        if path.name == f"{MODULE_NAME}.py":
            continue  # the module itself
        text = path.read_text(encoding="utf-8", errors="replace")
        if MODULE_NAME in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"production imports of migration core: {offenders}"


def test_git_diff_shows_zero_modified_production_files():
    changed = set()
    for args in (["diff", "--name-only"], ["diff", "--cached", "--name-only"]):
        completed = subprocess.run(
            ["git", *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        changed.update(
            line.strip().replace("\\", "/")
            for line in completed.stdout.splitlines()
            if line.strip()
        )
    production_changed = {
        name for name in changed
        if (name.startswith("app/") and name != f"app/research/{MODULE_NAME}.py")
        or name in {
            "scanner_runner.py", "bot.py", "paper_runner.py",
            "me_inverse_short_runner.py",
        }
    }
    assert production_changed == set(), (
        f"modified production files in working tree: {sorted(production_changed)}"
    )


def test_frozen_files_are_not_part_of_any_change():
    completed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    changed = {
        line.strip().replace("\\", "/")
        for line in completed.stdout.splitlines()
        if line.strip()
    }
    for frozen in PROTECTED:
        assert frozen not in changed, f"frozen file modified: {frozen}"


def test_default_config_makes_no_writes(tmp_path):
    writes = []

    class TripwireHook:
        def save_identity_batch(self, batch):
            writes.append(batch)

    core = MigrationIdentityCore(
        MigrationConfig.default_off(),
        persistence=TripwireHook(),
    )
    assert core.capture_dual(
        "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", "5m", 1795680000000
    ) is None
    assert writes == [], "default-OFF core must never reach persistence"
    assert len(core.aliases) == 0

    # isolated temp state: the test touches only tmp_path
    marker = tmp_path / "isolated.txt"
    marker.write_text("no production writes", encoding="utf-8")
    assert marker.read_text(encoding="utf-8") == "no production writes"


def test_module_import_has_no_side_effects():
    # Re-import in a subprocess: importing must not create files or need a DB.
    completed = subprocess.run(
        [
            __import__("sys").executable,
            "-c",
            "import app.research.scanner_signal_identity_migration_v1 as m;"
            "print(m.MigrationConfig.default_off().phase)",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT),
             "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.stdout.strip() == "0"


def test_identity_wiring_of_me_short_adapter_untouched():
    # The completed #323 adapter remains the ME_SHORT identity owner.
    adapter = (ROOT / "app" / "research" / "scanner_signal_integrity_observer.py")
    text = adapter.read_text(encoding="utf-8")
    assert "issue322:candle:v1" in text
    assert MODULE_NAME not in text
