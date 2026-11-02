"""Native Windows regression for PR #227. Research-only, no network calls."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires native Windows")

from app.models import Candle
from app.research.htf_baseline_short_phase2_incremental_cache_v1 import (
    CacheLockError, IncrementalPhase2CandleSource, _SymbolLock,
)


def _cache():
    return object.__new__(IncrementalPhase2CandleSource)


def _spawn_lock(directory: Path):
    script = """
import sys, time
from pathlib import Path
from app.research.htf_baseline_short_phase2_incremental_cache_v1 import _SymbolLock
with _SymbolLock(Path(sys.argv[1]), 'BTCUSDT'):
    print('LOCKED', flush=True)
    time.sleep(60)
"""
    return subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(directory)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def test_native_cold_incremental_restart_atomic_persistence(tmp_path):
    path = tmp_path / "BTCUSDT.json"
    src = _cache()
    first = [Candle(300000, 10, 11, 9, 10, 1)]
    second = first + [Candle(600000, 10, 12, 9, 11, 2)]
    assert src._read(path, "BTCUSDT") is None
    with _SymbolLock(tmp_path, "BTCUSDT"):
        src._write(path, "BTCUSDT", first)
    assert _cache()._read(path, "BTCUSDT") == first
    with _SymbolLock(tmp_path, "BTCUSDT"):
        src._write(path, "BTCUSDT", second)
    assert _cache()._read(path, "BTCUSDT") == second
    assert not list(tmp_path.glob("*.tmp"))


def test_native_cross_process_contention_and_crash_recovery(tmp_path):
    child = _spawn_lock(tmp_path)
    try:
        assert child.stdout.readline().strip() == "LOCKED", child.stderr.read()
        with pytest.raises(CacheLockError):
            with _SymbolLock(tmp_path, "BTCUSDT", wait_s=0):
                pass
        child.kill()
        child.wait(timeout=10)
        with _SymbolLock(tmp_path, "BTCUSDT", wait_s=2):
            pass
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def test_native_acl_restricted_directory_file_inherits(tmp_path):
    """Check actual Windows DACL behavior in a temporary, isolated directory.

    Does not claim a cross-account access-denial test (no second identity).
    """
    import ctypes
    import getpass

    directory = tmp_path / "private_cache"
    directory.mkdir()
    # icacls: strip inherited ACEs then explicitly permit only this user,
    # SYSTEM and Administrators; never touch the parent / production paths.
    who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                         capture_output=True, text=True, check=True)
    import csv
    sid = next(csv.reader(who.stdout.splitlines()))[1]
    proc = subprocess.run(
        ["icacls", str(directory), "/inheritance:r",
         "/grant:r", f"*{sid}:(OI)(CI)F",
         "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Verify directory is actually protected from inherited ACLs.
    import ctypes.wintypes as wt
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    get_info = advapi.GetNamedSecurityInfoW
    get_info.argtypes = [wt.LPWSTR, wt.DWORD, wt.DWORD,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    get_info.restype = wt.DWORD
    sd = ctypes.c_void_p()
    # SE_FILE_OBJECT=1, DACL_SECURITY_INFORMATION=4
    rc = get_info(str(directory), 1, 4, None, None, None, None,
                  ctypes.byref(sd))
    assert rc == 0, f"GetNamedSecurityInfoW: {rc}"
    try:
        get_control = advapi.GetSecurityDescriptorControl
        get_control.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.WORD),
                                ctypes.POINTER(wt.DWORD)]
        get_control.restype = wt.BOOL
        flags, rev = wt.WORD(), wt.DWORD()
        assert get_control(sd, ctypes.byref(flags), ctypes.byref(rev))
        assert flags.value & 0x1000, "directory DACL is not protected"
    finally:
        ctypes.WinDLL("kernel32").LocalFree(sd)
    cache_file = directory / "BTCUSDT.json"
    src = _cache()
    src._write(cache_file, "BTCUSDT", [Candle(300000, 10, 11, 9, 10, 1)])
    assert _cache()._read(cache_file, "BTCUSDT")
    verified = subprocess.run(["icacls", str(cache_file), "/verify"],
                              capture_output=True, text=True)
    assert verified.returncode == 0, verified.stdout + verified.stderr
