"""Research-only single-file integrity-checked checkpoint with atomic replacement.

This is file-level atomicity, not power-loss durability without filesystem guarantees.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path

SCHEMA = "HTF_176_COMBINED_CHECKPOINT_V1"

def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

def write_checkpoint(path, payload, *, simulate_interrupt=False):
    destination = Path(path)
    body = {"schema":SCHEMA, "payload":payload}
    envelope = {**body, "sha256":hashlib.sha256(canonical(body)).hexdigest()}
    fd, temporary = tempfile.mkstemp(prefix=destination.name+".",suffix=".tmp",dir=destination.parent)
    try:
        with os.fdopen(fd,"wb") as stream:
            stream.write(canonical(envelope))
            stream.flush()
            os.fsync(stream.fileno())
        if simulate_interrupt:
            raise RuntimeError("SIMULATED_BEFORE_ATOMIC_REPLACE")
        os.replace(temporary,destination)
        # Sync the directory metadata when filesystem supports it.
        directory_fd = os.open(str(destination.parent),os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)

def read_checkpoint(path):
    envelope=json.loads(Path(path).read_bytes())
    if envelope.get("schema")!=SCHEMA:raise ValueError("checkpoint schema mismatch")
    body={"schema":envelope["schema"],"payload":envelope["payload"]}
    actual=hashlib.sha256(canonical(body)).hexdigest()
    if actual!=envelope.get("sha256"):raise ValueError("checkpoint integrity mismatch")
    return body["payload"]
