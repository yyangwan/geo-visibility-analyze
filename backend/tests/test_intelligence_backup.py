import json
from datetime import date, datetime, time
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import insert

from app.intelligence.backup import (
    BackupInvalid,
    export_bundle,
    packed,
    schema_head,
    tables,
    unpacked,
    verify_bundle,
)
from app.intelligence.blob import LocalBlobStore, digest
from app.intelligence.models import OwnedContentAsset, SourceDocument, SourceSnapshot
from app.intelligence.schema_lock import SchemaLockUnavailable, schema_lock
from app.intelligence.schema_version import SCHEMA_REVISION
from tests.conftest import test_engine


class FakeConnection:
    def __init__(self, *, database="target", acquired=1, released=1):
        self.dialect = type("Dialect", (), {"name": "mysql"})()
        self.database = database
        self.acquired, self.released = acquired, released
        self.events, self.invalidated = [], False

    def in_transaction(self):
        return False

    def scalar(self, query, params=None):
        sql = str(query)
        self.events.append((sql, params))
        if "DATABASE()" in sql:
            return self.database
        if "GET_LOCK" in sql:
            return self.acquired
        if "RELEASE_LOCK" in sql:
            return self.released
        raise AssertionError("Unexpected statement")

    def commit(self):
        self.events.append(("COMMIT", None))

    def rollback(self):
        self.events.append(("ROLLBACK", None))

    def invalidate(self):
        self.invalidated = True


def test_schema_mutex_commits_acquisition_before_body_and_releases_same_lock():
    connection = FakeConnection(database="sensitive-database")
    with schema_lock(connection, timeout=0):
        assert connection.events[-1] == ("COMMIT", None)
        assert not any("RELEASE_LOCK" in sql for sql, _ in connection.events)
    acquired = next(params for sql, params in connection.events if "GET_LOCK" in sql)
    released = next(
        params for sql, params in connection.events if "RELEASE_LOCK" in sql
    )
    assert acquired["name"] == released["name"]
    assert len(acquired["name"]) <= 64 and "sensitive" not in acquired["name"]
    other = FakeConnection(database="other")
    with schema_lock(other):
        pass
    assert (
        next(params for sql, params in other.events if "GET_LOCK" in sql)["name"]
        != acquired["name"]
    )


@pytest.mark.parametrize("acquired", [0, None])
def test_mutex_busy_or_unknown_never_runs_body(acquired):
    connection = FakeConnection(acquired=acquired)
    with pytest.raises(SchemaLockUnavailable), schema_lock(connection):
        pytest.fail("No ownership")
    assert not any("RELEASE_LOCK" in sql for sql, _ in connection.events)


def test_mutex_releases_after_failure_and_never_exposes_sql_credentials():
    connection = FakeConnection()
    with pytest.raises(SchemaLockUnavailable) as error, schema_lock(connection):
        raise RuntimeError("secret-key database password")
    assert "secret-key" not in str(error.value)
    assert any("RELEASE_LOCK" in sql for sql, _ in connection.events)
    connection = FakeConnection(released=None)
    with pytest.raises(SchemaLockUnavailable), schema_lock(connection):
        pass
    assert connection.invalidated


def test_mutex_refuses_existing_transaction_or_other_dialect():
    connection = FakeConnection()
    connection.in_transaction = lambda: True
    with pytest.raises(SchemaLockUnavailable), schema_lock(connection):
        pass
    assert connection.events == []
    connection = FakeConnection()
    connection.dialect.name = "sqlite"
    with pytest.raises(SchemaLockUnavailable), schema_lock(connection):
        pass


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        42,
        0.25,
        "全文🧭",
        datetime(2026, 10, 16, 1, 2, 3, 456),
        date(2026, 10, 16),
        time(1, 2, 3),
        Decimal("1.234567890123456789"),
        b"\x00\xff",
        {"t": "bytes", "v": "not-a-tag", "answer": ["原文", {"t": "date"}]},
    ],
)
def test_typed_database_values_round_trip_without_json_collisions(value):
    assert unpacked(json.loads(json.dumps(packed(value)))) == value


@pytest_asyncio.fixture
async def backup_connection():
    version = tables()[-1]
    async with test_engine.connect() as connection:
        await connection.run_sync(lambda conn: version.create(conn))
        await connection.execute(insert(version).values(version_num=schema_head()))
        await connection.commit()
        try:
            yield connection
        finally:
            await connection.rollback()
            await connection.run_sync(lambda conn: version.drop(conn))
            await connection.commit()


async def source_fixture(db, store, *, second=False):
    content = b"immutable compressed body bytes"
    sha = digest(content)
    text_key = "projects/p1/text/first.gz"
    raw_key = "projects/p1/raw/first.gz"
    await store.put_immutable(text_key, content)
    await store.put_immutable(raw_key, content)
    doc = SourceDocument(
        project_id="p1", url="https://private.example/article", url_hash="a" * 64
    )
    db.add(doc)
    await db.flush()
    db.add(
        SourceSnapshot(
            project_id="p1",
            document_id=doc.id,
            body_hash="b" * 64,
            representation_hash="c" * 64,
            text_key=text_key,
            text_sha256=sha,
            raw_key=raw_key if second else None,
            raw_sha256=sha if second else None,
            extractor_version="test",
        )
    )
    await db.commit()
    return text_key, raw_key, content


@pytest.mark.asyncio
async def test_full_bundle_includes_all_tables_and_actual_object_bytes(
    backup_connection, db_session, tmp_path
):
    store = LocalBlobStore(tmp_path / "source")
    await source_fixture(db_session, store, second=True)
    target = tmp_path / "backup"
    result = await export_bundle(
        backup_connection, store, target, allow_sqlite_test=True
    )
    assert result["tables"] == len(tables())
    assert result["objects"] == 2 and result["restore_test"] == "not_performed"
    assert verify_bundle(target) == result
    assert len(list((target / "objects").iterdir())) == 1  # Equal bytes stored once.
    assert not (target / "INCOMPLETE").exists()
    with pytest.raises(FileExistsError):
        await export_bundle(backup_connection, store, target, allow_sqlite_test=True)


@pytest.mark.asyncio
async def test_owned_text_is_backed_up_without_a_fake_citation_or_source_snapshot(
    backup_connection, db_session, tmp_path
):
    assert schema_head() == SCHEMA_REVISION
    store = LocalBlobStore(tmp_path / "owned")
    key, data = "projects/p1/owned/specs.gz", b"approved source revision fixture"
    info = await store.put_immutable(key, data)
    db_session.add(
        OwnedContentAsset(
            project_id="p1",
            asset_key="asset1",
            version=1,
            registry_brand_id="own",
            target_id="registry:own",
            title="Owned specs",
            source_kind="text",
            text_key=key,
            text_sha256=info.sha256,
            status="captured",
            actor_id="fixture",
            idempotency_key="owned-fixture",
            request_hash="a" * 64,
        )
    )
    await db_session.commit()
    target = tmp_path / "bundle"
    result = await export_bundle(
        backup_connection, store, target, allow_sqlite_test=True
    )
    assert result["objects"] == 1
    assert verify_bundle(target) == result
    (target / "objects" / (info.sha256 + ".bin")).unlink()
    with pytest.raises((BackupInvalid, FileNotFoundError)):
        verify_bundle(target)


@pytest.mark.asyncio
async def test_missing_second_key_cannot_be_hidden_by_identical_content(
    backup_connection, db_session, tmp_path
):
    store = LocalBlobStore(tmp_path / "source")
    _, raw_key, _ = await source_fixture(db_session, store, second=True)
    store.path(raw_key).unlink()
    target = tmp_path / "backup"
    with pytest.raises(FileNotFoundError):
        await export_bundle(backup_connection, store, target, allow_sqlite_test=True)
    assert (target / "INCOMPLETE").exists()
    with pytest.raises(BackupInvalid, match="incomplete"):
        verify_bundle(target)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    ["database", "object", "missing_reference", "traversal", "inventory", "revision"],
)
async def test_verifier_rejects_damaged_or_incomplete_bundles(
    backup_connection, db_session, tmp_path, damage
):
    store = LocalBlobStore(tmp_path / "source")
    await source_fixture(db_session, store)
    target = tmp_path / "backup"
    await export_bundle(backup_connection, store, target, allow_sqlite_test=True)
    manifest = json.loads((target / "manifest.json").read_text())
    if damage == "database":
        table = next(
            entry
            for entry in manifest["database"]["tables"]
            if entry["name"] == "intelligence_documents"
        )
        with (target / table["file"]).open("ab") as file:
            file.write(b"[]\n")
    elif damage == "object":
        (target / manifest["objects"][0]["file"]).write_bytes(b"corrupt")
    elif damage == "missing_reference":
        manifest["objects"] = []
    elif damage == "traversal":
        manifest["database"]["tables"][0]["file"] = "../../secret"
    elif damage == "inventory":
        manifest["database"]["tables"].pop()
    elif damage == "revision":
        manifest["database"]["revision"] = "old"
    (target / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BackupInvalid):
        verify_bundle(target)


@pytest.mark.asyncio
async def test_export_refuses_unknown_tables_or_unpinned_source_keys(
    backup_connection, db_session, tmp_path
):
    from sqlalchemy import text

    await backup_connection.execute(text("CREATE TABLE unexpected (id INTEGER)"))
    await backup_connection.commit()
    try:
        with pytest.raises(BackupInvalid, match="tables"):
            await export_bundle(
                backup_connection, object(), tmp_path / "backup", allow_sqlite_test=True
            )
    finally:
        await backup_connection.rollback()
        await backup_connection.execute(text("DROP TABLE unexpected"))
        await backup_connection.commit()


@pytest.mark.asyncio
async def test_cross_project_references_rejected_before_object_reads(
    backup_connection, db_session, tmp_path
):
    from sqlalchemy import select

    store = LocalBlobStore(tmp_path / "source")
    await source_fixture(db_session, store)
    snapshot = await db_session.scalar(select(SourceSnapshot))
    snapshot.text_key = "projects/other-project/text/first.gz"
    await db_session.commit()
    with pytest.raises(BackupInvalid, match="Cross-project"):
        await export_bundle(
            backup_connection, object(), tmp_path / "backup", allow_sqlite_test=True
        )


@pytest.mark.asyncio
async def test_export_checks_bytes_even_with_untrusted_store(
    backup_connection, db_session, tmp_path
):
    store = LocalBlobStore(tmp_path / "source")
    await source_fixture(db_session, store)

    class Broken:
        async def get(self, *_):
            return b"wrong bytes"

    with pytest.raises(BackupInvalid, match="Object bytes"):
        await export_bundle(
            backup_connection, Broken(), tmp_path / "backup", allow_sqlite_test=True
        )
    assert (tmp_path / "backup" / "INCOMPLETE").exists()


@pytest.mark.asyncio
async def test_cli_error_never_prints_keys_or_database_values(monkeypatch, capsys):
    from app.intelligence import backup

    def broken(_):
        raise RuntimeError("access-key database-password private-source-body")

    monkeypatch.setattr(backup, "verify_bundle", broken)
    monkeypatch.setattr("sys.argv", ["backup", "verify", "--directory", "unused"])
    assert await backup.cli() == 1
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "failed"
    assert "access-key" not in output and "private-source-body" not in output


@pytest.mark.asyncio
async def test_unregistered_column_never_silently_disappears_from_backup(
    backup_connection, tmp_path
):
    from sqlalchemy import text

    await backup_connection.execute(
        text("ALTER TABLE intelligence_documents ADD COLUMN unexpected_data TEXT")
    )
    await backup_connection.commit()
    with pytest.raises(BackupInvalid, match="columns"):
        await export_bundle(
            backup_connection, object(), tmp_path / "backup", allow_sqlite_test=True
        )
