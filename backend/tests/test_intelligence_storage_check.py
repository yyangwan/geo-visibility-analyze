import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.intelligence.blob import BlobInfo, IntegrityError, LocalBlobStore, OssBlobStore
from app.intelligence.storage_check import StorageCheckFailed, check_storage, cli


@pytest.mark.asyncio
async def test_local_probe_verifies_bytes_and_retains_test_object(tmp_path):
    store = LocalBlobStore(tmp_path)
    result = await check_storage(store, backend="local", write_probe=True)
    assert result["status"] == "storage_probe_verified"
    assert result["configuration"] == "development_local"
    assert result["release_eligible"] is False
    assert len(result["checks"]) == 7
    info = result["probe_object"]
    assert info["key"].startswith(
        "projects/storage-acceptance/operations/storage-probes/"
    )
    assert await store.get(info["key"], info["sha256"])


@pytest.mark.asyncio
async def test_configuration_only_does_not_write():
    store = SimpleNamespace(verify_configuration=AsyncMock(), put_immutable=AsyncMock())
    result = await check_storage(store, backend="oss")
    store.verify_configuration.assert_awaited_once()
    store.put_immutable.assert_not_awaited()
    assert result["write_probe"] == "not_performed"
    assert result["configuration"] == "private_never_versioned"


@pytest.mark.asyncio
async def test_oss_adapter_probe_checks_headers_and_existing_key():
    import io

    from oss2.exceptions import ServerError

    class Bucket:
        def __init__(self):
            self.objects = {}
            self.headers = {}
            self.puts = 0

        def get_bucket_acl(self):
            return SimpleNamespace(acl="private")

        def get_bucket_versioning(self):
            return SimpleNamespace(status=None)

        def put_object(self, key, data, headers):
            self.puts += 1
            assert headers["x-oss-forbid-overwrite"] == "true"
            assert headers["x-oss-server-side-encryption"] == "AES256"
            if key in self.objects:
                raise ServerError(409, {}, b"", {"Code": "FileAlreadyExists"})
            self.objects[key] = data
            self.headers[key] = headers

        def get_object(self, key):
            return io.BytesIO(self.objects[key])

        def head_object(self, key):
            return SimpleNamespace(
                headers=self.headers[key], content_length=len(self.objects[key])
            )

    bucket = Bucket()
    result = await check_storage(OssBlobStore(bucket), backend="oss", write_probe=True)
    assert result["configuration"] == "private_never_versioned"
    assert result["status"] == "storage_probe_verified"
    assert bucket.puts == 3
    assert len(bucket.objects) == 1


@pytest.mark.asyncio
async def test_invalid_configuration_never_writes():
    store = SimpleNamespace(
        verify_configuration=AsyncMock(side_effect=ValueError("not private")),
        put_immutable=AsyncMock(),
    )
    with pytest.raises(ValueError):
        await check_storage(store, backend="oss", write_probe=True)
    store.put_immutable.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_id", ["../real-project", "A" * 32, "", "x" * 32])
async def test_invalid_probe_id_has_no_storage_effect(probe_id):
    store = SimpleNamespace(verify_configuration=AsyncMock())
    with pytest.raises(StorageCheckFailed):
        await check_storage(store, backend="oss", probe_id=probe_id)
    store.verify_configuration.assert_not_awaited()


@pytest.mark.asyncio
async def test_head_mismatch_fails(tmp_path):
    store = LocalBlobStore(tmp_path)
    store.head = AsyncMock(return_value=BlobInfo("wrong", "bad", 0))
    with pytest.raises(StorageCheckFailed):
        await check_storage(store, backend="local", write_probe=True)


@pytest.mark.asyncio
async def test_store_accepting_wrong_checksum_fails(tmp_path):
    store = LocalBlobStore(tmp_path)
    original = store.get

    async def broken_get(key, sha):
        if sha == "0" * 64:
            return b"unchecked"
        return await original(key, sha)

    store.get = broken_get
    with pytest.raises(StorageCheckFailed, match="Wrong checksum"):
        await check_storage(store, backend="local", write_probe=True)


@pytest.mark.asyncio
async def test_store_silently_accepting_changed_put_fails(tmp_path):
    store = LocalBlobStore(tmp_path)
    original = store.put_immutable

    async def broken_put(key, data):
        if data == b"different acceptance bytes":
            return None
        return await original(key, data)

    store.put_immutable = broken_put
    with pytest.raises(StorageCheckFailed, match="overwrite"):
        await check_storage(store, backend="local", write_probe=True)


@pytest.mark.asyncio
async def test_store_corrupting_before_rejecting_is_detected(tmp_path):
    store = LocalBlobStore(tmp_path)
    original = store.put_immutable

    async def broken_put(key, data):
        if data == b"different acceptance bytes":
            store.path(key).write_bytes(data)
            raise IntegrityError()
        return await original(key, data)

    store.put_immutable = broken_put
    with pytest.raises(IntegrityError):
        await check_storage(store, backend="local", write_probe=True)


@pytest.mark.asyncio
async def test_cli_existing_report_blocks_all_storage(monkeypatch, tmp_path, capsys):
    import app.intelligence.storage_check as module

    output = tmp_path / "report.json"
    output.write_text("existing", encoding="utf-8")
    create = AsyncMock()
    monkeypatch.setattr(module, "configured_store", create)
    monkeypatch.setattr(
        sys, "argv", ["check", "--write-probe", "--output", str(output)]
    )
    assert await cli() == 1
    create.assert_not_called()
    assert output.read_text(encoding="utf-8") == "existing"
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


@pytest.mark.asyncio
async def test_cli_redacts_sdk_errors(monkeypatch, tmp_path, capsys):
    import app.intelligence.storage_check as module

    def fail():
        raise RuntimeError("Authorization secret-token secret-key private-body")

    output = tmp_path / "report.json"
    monkeypatch.setattr(module, "configured_store", fail)
    monkeypatch.setattr(
        sys, "argv", ["check", "--write-probe", "--output", str(output)]
    )
    assert await cli() == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["planned_object_key"].startswith("projects/storage-acceptance/")
    assert report["object_presence"] == "may_have_been_written"
    assert "secret" not in output.read_text(encoding="utf-8") + capsys.readouterr().out
