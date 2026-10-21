"""#176 read-only single-writer checkpoint lock gate (research-only)."""
import argparse
import json
import os
from contextlib import contextmanager
from pathlib import Path
from tools.research.htf_baseline_short_atomic_checkpoint_v1 import write_checkpoint

@contextmanager
def writer_lock(path):
    lock=Path(str(path)+".writer.lock")
    try:
        descriptor=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("OBSERVER_WRITER_ALREADY_ACTIVE")
    try:
        os.write(descriptor,str(os.getpid()).encode())
        os.close(descriptor)
        yield
    finally:
        try:
            lock.unlink(missing_ok=True)
        except FileNotFoundError:
            pass

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--phase",required=True,choices=("claim","release","write"))
    p.add_argument("--path",required=True,type=Path)
    p.add_argument("--payload",default='{"cursor":1}')
    a=p.parse_args()
    if a.phase=="claim":
        with writer_lock(a.path):
            print("WRITER_CLAIM_PASS",os.getpid())
    elif a.phase=="release":
        with writer_lock(a.path):
            pass
        print("WRITER_RELEASE_PASS")
    else:
        with writer_lock(a.path):
            write_checkpoint(a.path,json.loads(a.payload))
        print("LOCKED_WRITE_PASS")

if __name__=="__main__":main()
