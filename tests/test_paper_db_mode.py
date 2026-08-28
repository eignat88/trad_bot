import pytest

from app.paper import cli


def test_paper_repository_fails_fast_when_postgres_is_unavailable(monkeypatch):
    def unavailable(**_kwargs):
        raise ConnectionError("database down")

    monkeypatch.setattr(cli, "ScannerRepository", unavailable)
    with pytest.raises(RuntimeError, match="PAPER_RUNNER_STOPPED"):
        cli._get_repo()
