"""Production deployment must never migrate the legacy application database."""

import pytest

from scripts.check_intelligence_deployment import guarded_database_url


@pytest.mark.parametrize("identity", ["", "short", "A" * 32, "a" * 31, "a" * 33, "../worker"])
def test_worker_identity_rejects_invalid_values(identity):
    from app.intelligence.monitor import WorkerMonitor
    from scripts.check_intelligence_deployment import worker_ready

    with pytest.raises(ValueError):
        WorkerMonitor(None, None, identity)
    with pytest.raises(ValueError):
        worker_ready(None, None, identity)


def test_worker_identity_defaults_are_unique_and_candidate_can_be_explicit():
    from app.intelligence.monitor import WorkerMonitor

    first, second = WorkerMonitor(None, None), WorkerMonitor(None, None)
    assert len(first.id) == 32 and first.id != second.id
    assert WorkerMonitor(None, None, "a" * 32).id == "a" * 32


@pytest.mark.parametrize("url", [
    "mysql+aiomysql://user:pass@mysql/aiscope",
    "mysql+aiomysql://user:pass@mysql/intelligence_ci",
    "mysql+aiomysql://user:pass@mysql/intelligence_production_suffix",
    "sqlite:///intelligence_production",
])
def test_guard_rejects_any_database_other_than_exact_new_production_schema(monkeypatch, url):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", url)
    with pytest.raises(RuntimeError, match="outside intelligence_production"):
        guarded_database_url()


def test_guard_allows_only_explicit_production_oss_configuration(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", "mysql+aiomysql://user:pass@mysql/intelligence_production")
    monkeypatch.setenv("AISCOPE_DEBUG", "0")
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    monkeypatch.setattr(settings, "intelligence_environment", "production")
    monkeypatch.setattr(settings, "intelligence_blob_backend", "oss")
    assert guarded_database_url().drivername == "mysql+pymysql"
    monkeypatch.setattr(settings, "intelligence_blob_backend", "local")
    with pytest.raises(RuntimeError, match="OSS"):
        guarded_database_url()


@pytest.mark.parametrize("debug", [None, "1", "true", "", "false"])
def test_production_guard_requires_explicit_debug_off(monkeypatch, debug):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", "mysql+aiomysql://user:pass@mysql/intelligence_production")
    if debug is None:
        monkeypatch.delenv("AISCOPE_DEBUG", raising=False)
    else:
        monkeypatch.setenv("AISCOPE_DEBUG", debug)
    with pytest.raises(RuntimeError, match="AISCOPE_DEBUG=0"):
        guarded_database_url()
