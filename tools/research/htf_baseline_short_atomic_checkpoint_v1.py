"""Research-only single-file integrity-checked checkpoint with atomic replacement.

This is file-level atomicity, not power-loss durability without filesystem guarantees.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path

SCHEMA = "HTF_176_COMBINED_CHECKPOINT_V2"

def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

def _directory_fsync(path):
    # Opening a directory read-only is not portable on Windows; a failed
    # directory sync must not invalidate an otherwise committed checkpoint.
    try:
        directory_fd = os.open(str(path),os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return True

def write_checkpoint(path, payload, *, simulate_interrupt=False, fault=None, provenance_envelope=None):
    destination = Path(path)
    body = {"schema":SCHEMA, "payload":payload}
    generated = {**body, "sha256":hashlib.sha256(canonical(body)).hexdigest()}
    if provenance_envelope is None:
        provenance_envelope = generated
    elif not isinstance(provenance_envelope,dict):
        raise TypeError("provenance_envelope must be an object")
    if provenance_envelope.get("schema")!=SCHEMA:
        raise ValueError("checkpoint provenance schema mismatch")
    if provenance_envelope.get("sha256") != hashlib.sha256(canonical(
            {"schema":provenance_envelope["schema"],"payload":provenance_envelope["payload"]}
    )).hexdigest():
        raise ValueError("checkpoint provenance integrity mismatch")
    if fault not in (None,"write","replace"):
        raise ValueError("unknown checkpoint fault")
    raw_envelope = canonical(provenance_envelope)
    fd, temporary = tempfile.mkstemp(prefix=destination.name+".",suffix=".tmp",dir=destination.parent)
    try:
        with os.fdopen(fd,"wb") as stream:
            if fault=="write":
                stream.write(b"{")
            else:
                stream.write(raw_envelope)
            stream.flush()
            os.fsync(stream.fileno())
        if simulate_interrupt or fault=="write":
            raise RuntimeError("SIMULATED_BEFORE_ATOMIC_REPLACE")
        if fault=="replace":
            raise RuntimeError("SIMULATED_AFTER_TEMP_FSYNC_BEFORE_REPLACE")
        os.replace(temporary,destination)
        _directory_fsync(destination.parent)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)

def checkpoint_envelope(path):
    envelope=json.loads(Path(path).read_bytes())
    if not isinstance(envelope,dict):raise ValueError("checkpoint envelope must be an object")
    if envelope.get("schema")!=SCHEMA:raise ValueError("checkpoint schema mismatch")
    if "payload" not in envelope or "sha256" not in envelope:
        raise ValueError("checkpoint integrity mismatch")
    body={"schema":envelope["schema"],"payload":envelope["payload"]}
    actual=hashlib.sha256(canonical(body)).hexdigest()
    if actual!=envelope.get("sha256"):raise ValueError("checkpoint integrity mismatch")
    return envelope

def read_checkpoint(path):
    return checkpoint_envelope(path)["payload"]
