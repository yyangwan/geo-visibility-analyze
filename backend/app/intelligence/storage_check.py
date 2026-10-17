"""Operator storage acceptance; explicit write probe, no database/model/delete calls."""

import argparse
import asyncio
import gzip
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from app.intelligence.blob import IntegrityError, configured_store, digest


class StorageCheckFailed(RuntimeError):
    pass


def require(condition):
    if not condition:
        raise StorageCheckFailed("Storage check failed")


def probe_key(probe_id):
    require(bool(re.fullmatch(r"[a-f0-9]{32}", probe_id)))
    return f"projects/storage-acceptance/operations/storage-probes/{probe_id}/probe.gz"


async def check_storage(store, *, backend, write_probe=False, probe_id=None):
    require(backend in {"oss", "local"})
    probe_id = uuid4().hex if probe_id is None else probe_id
    # A fixed non-business namespace prevents probes from targeting real evidence.
    key = probe_key(probe_id)
    result = {
        "status": "configuration_checked",
        "backend": backend,
        "configuration": "development_local",
        "write_probe": "not_performed",
        "release_eligible": False,
    }
    if backend == "oss":
        await store.verify_configuration()
        result["configuration"] = "private_never_versioned"
    if not write_probe:
        return result
    data = gzip.compress(
        ("Genilink storage acceptance / 非业务测试对象 / " + probe_id).encode(),
        mtime=0,
    )
    expected = digest(data)
    first = await store.put_immutable(key, data)
    require(first.key == key and first.sha256 == expected and first.size == len(data))
    retry = await store.put_immutable(key, data)
    require(retry == first)
    require(await store.get(key, expected) == data)
    metadata = await store.head(key)
    require(
        metadata.key == key
        and metadata.sha256 == expected
        and metadata.size == len(data)
    )
    try:
        await store.get(key, "0" * 64)
    except IntegrityError:
        pass
    else:
        raise StorageCheckFailed("Wrong checksum was accepted")
    try:
        await store.put_immutable(key, b"different acceptance bytes")
    except IntegrityError:
        pass
    else:
        raise StorageCheckFailed("Immutable overwrite was accepted")
    require(await store.get(key, expected) == data)
    result.update(
        status="storage_probe_verified",
        write_probe="verified",
        checks=[
            "immutable_put",
            "same_bytes_retry",
            "read_back_sha256",
            "head_size_and_sha256",
            "wrong_checksum_rejected",
            "different_bytes_rejected",
            "original_bytes_preserved",
        ],
        probe_object={"key": key, "sha256": expected, "size": len(data)},
        retention="retained_no_delete_attempted",
    )
    return result


async def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-probe", action="store_true")
    parser.add_argument("--output", required=True, help="New private JSON report")
    args = parser.parse_args()
    # Reserve the report before any paid/storage operation; never overwrite a report.
    output = Path(args.output)
    try:
        from app.config import settings

        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as report:
            result = {"status": "incomplete", "release_eligible": False}
            probe_id = uuid4().hex
            if args.write_probe:
                result["planned_object_key"] = probe_key(probe_id)
                result["object_presence"] = "not_verified"
            report.write(json.dumps(result))
            report.flush()
            try:
                result = await check_storage(
                    configured_store(),
                    backend=settings.intelligence_blob_backend,
                    write_probe=args.write_probe,
                    probe_id=probe_id,
                )
            except Exception:
                # SDK errors can contain headers/credentials/URLs; report no raw errors.
                result = {"status": "failed", "release_eligible": False}
                if args.write_probe:
                    result.update(
                        planned_object_key=probe_key(probe_id),
                        object_presence="may_have_been_written",
                        retention="no_delete_attempted",
                    )
            report.seek(0)
            report.write(json.dumps(result, ensure_ascii=False, indent=2))
            report.truncate()
        print(json.dumps({"status": result["status"], "release_eligible": False}))
        return 0 if result["status"] != "failed" else 1
    except Exception:
        print(json.dumps({"status": "failed", "release_eligible": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(cli()))
