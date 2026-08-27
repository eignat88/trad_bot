from app.config import Settings
import paper_runner


def test_emergency_stop_file_blocks_new_paper_entries(monkeypatch):
    monkeypatch.setattr(paper_runner.Path, "exists", lambda _path: True)

    assert paper_runner._emergency_stop_requested(Settings())


def test_missing_emergency_stop_file_allows_entries(monkeypatch):
    monkeypatch.setattr(paper_runner.Path, "exists", lambda _path: False)

    assert not paper_runner._emergency_stop_requested(Settings())
