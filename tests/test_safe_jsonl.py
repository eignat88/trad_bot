from concurrent.futures import ThreadPoolExecutor
import json

from app.storage.safe_jsonl import append_record, atomic_rewrite, read_records


def test_concurrent_append_produces_complete_records(tmp_path):
    path = tmp_path / "events.jsonl"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: append_record(path, {"id": i}), range(100)))
    records = read_records(path)
    assert len(records) == 100
    assert {r["id"] for r in records} == set(range(100))


def test_interrupted_final_line_is_ignored_on_restart(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"id": 1}\n{"id":', encoding="utf-8")
    assert read_records(path) == [{"id": 1}]


def test_atomic_rewrite_preserves_previous_file_if_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    atomic_rewrite(path, [{"id": 1}])

    def fail_replace(*_args):
        raise OSError("simulated interruption")

    monkeypatch.setattr("app.storage.safe_jsonl.os.replace", fail_replace)
    try:
        atomic_rewrite(path, [{"id": 2}])
    except OSError:
        pass
    assert [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == [{"id": 1}]
