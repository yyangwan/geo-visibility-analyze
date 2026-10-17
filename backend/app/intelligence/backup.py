"""Private full-database + immutable-object bundles; no restore writes or deletion.

CLI: python -m app.intelligence.backup export --output <new-private-directory>
     python -m app.intelligence.backup verify --directory <bundle>
"""

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Column, MetaData, String, Table, inspect, select, text

from app.database import Base
from app.intelligence import models as intelligence_models  # noqa: F401
from app.intelligence.blob import digest, validate_key
from app.intelligence.schema_lock import schema_lock
from app.models import device_gateway, models  # noqa: F401
from app.utils.timezone import utc_isoformat, utcnow

FORMAT = "citation-backup-1"


class BackupInvalid(RuntimeError):
    pass


def schema_head():
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.set_main_option("version_locations", str(backend / "alembic" / "schema"))
    return ScriptDirectory.from_config(config).get_current_head()


def tables():
    version = Table(
        "alembic_version",
        MetaData(),
        Column("version_num", String(32), primary_key=True),
    )
    return [*Base.metadata.sorted_tables, version]


def packed(value):
    """Explicit tags preserve SQL scalar types without colliding with source JSON."""
    if isinstance(value, Enum):
        return packed(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return {"t": "scalar", "v": value}
    if isinstance(value, float) and math.isfinite(value):
        return {"t": "scalar", "v": value}
    if isinstance(value, (datetime, date, time)):
        return {"t": type(value).__name__, "v": value.isoformat()}
    if isinstance(value, Decimal):
        return {"t": "decimal", "v": str(value)}
    if isinstance(value, bytes):
        return {"t": "bytes", "v": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {"t": "dict", "v": {key: packed(item) for key, item in value.items()}}
    if isinstance(value, list):
        return {"t": "list", "v": [packed(item) for item in value]}
    raise BackupInvalid("Unsupported database value")


def unpacked(value):
    if not isinstance(value, dict) or set(value) != {"t", "v"}:
        raise BackupInvalid("Invalid typed database value")
    tag, item = value["t"], value["v"]
    if tag == "scalar" and (
        item is None
        or isinstance(item, (str, bool, int))
        or isinstance(item, float)
        and math.isfinite(item)
    ):
        return item
    if tag == "dict" and isinstance(item, dict):
        return {key: unpacked(child) for key, child in item.items()}
    if tag == "list" and isinstance(item, list):
        return [unpacked(child) for child in item]
    if isinstance(item, str):
        if tag == "datetime":
            return datetime.fromisoformat(item)
        if tag == "date":
            return date.fromisoformat(item)
        if tag == "time":
            return time.fromisoformat(item)
        if tag == "decimal":
            result = Decimal(item)
            if result.is_finite():
                return result
        if tag == "bytes":
            return base64.b64decode(item, validate=True)
    raise BackupInvalid("Invalid typed database value")


def json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def write_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def reference_pairs(row):
    project = row["project_id"]
    for key_field, hash_field in [
        ("text_key", "text_sha256"),
        ("raw_key", "raw_sha256"),
    ]:
        key, checksum = row[key_field], row[hash_field]
        if key is None and checksum is None and key_field == "raw_key":
            continue
        if (
            not isinstance(key, str)
            or not isinstance(checksum, str)
            or len(checksum) != 64
            or any(c not in "0123456789abcdef" for c in checksum)
        ):
            raise BackupInvalid("Incomplete object reference")
        validate_key(key)
        if not key.startswith(f"projects/{project}/"):
            raise BackupInvalid("Cross-project object reference")
        yield key, checksum


def export_database(connection, directory, *, allow_sqlite_test=False):
    if connection.in_transaction():
        raise BackupInvalid("Backup requires a fresh connection")
    if connection.dialect.name == "sqlite" and allow_sqlite_test:
        from contextlib import nullcontext

        guard = nullcontext()
    else:
        if connection.dialect.name != "mysql":
            raise BackupInvalid("Production database export requires MySQL")
        connection = connection.execution_options(isolation_level="REPEATABLE READ")
        guard = schema_lock(connection)
    inventory, refs = [], {}
    with guard, connection.begin():
        expected = {table.name for table in tables()}
        inspector = inspect(connection)
        if set(inspector.get_table_names()) != expected:
            raise BackupInvalid("Database tables do not match the dedicated schema")
        for table in tables():
            actual_columns = {
                column["name"] for column in inspector.get_columns(table.name)
            }
            if actual_columns != set(table.columns.keys()):
                raise BackupInvalid(
                    "Database columns do not match the dedicated schema"
                )
        if connection.dialect.name == "mysql":
            engines = (
                connection.execute(
                    text(
                        "SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'"
                    )
                )
                .scalars()
                .all()
            )
            if not engines or any(engine != "InnoDB" for engine in engines):
                raise BackupInvalid(
                    "Consistent export requires all tables to use InnoDB"
                )
        # The first data SELECT establishes one repeatable-read view. Every table,
        # object reference and Alembic version below belongs to this same view.
        revision = (
            connection.execute(text("SELECT version_num FROM alembic_version"))
            .scalars()
            .all()
        )
        if revision != [schema_head()]:
            raise BackupInvalid("Database revision does not match this exporter")
        for table in tables():
            filename = f"database/{table.name}.jsonl"
            path = directory / filename
            checksum, count = hashlib.sha256(), 0
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                result = connection.execution_options(stream_results=True).execute(
                    select(table)
                )
                try:
                    for values in result:
                        row = dict(zip(table.columns.keys(), values, strict=True))
                        if table.name == "intelligence_snapshots":
                            for key, sha in reference_pairs(row):
                                if key in refs and refs[key] != sha:
                                    raise BackupInvalid(
                                        "Conflicting immutable object reference"
                                    )
                                refs[key] = sha
                        line = json_bytes([packed(value) for value in values]) + b"\n"
                        file.write(line)
                        checksum.update(line)
                        count += 1
                finally:
                    result.close()
                file.flush()
                os.fsync(file.fileno())
            inventory.append(
                {
                    "name": table.name,
                    "file": filename,
                    "columns": list(table.columns.keys()),
                    "rows": count,
                    "sha256": checksum.hexdigest(),
                }
            )
    return {"revision": revision[0], "tables": inventory}, refs


async def export_bundle(connection, blobs, directory, *, allow_sqlite_test=False):
    directory = Path(directory)
    directory.mkdir(mode=0o700)  # Exclusive new directory; never overwrite a backup.
    (directory / "database").mkdir(mode=0o700)
    (directory / "objects").mkdir(mode=0o700)
    write_private(directory / "INCOMPLETE", b"Backup has not completed verification\n")
    database, refs = await connection.run_sync(
        lambda conn: export_database(
            conn, directory, allow_sqlite_test=allow_sqlite_test
        )
    )
    objects, copied = [], set()
    for key, sha in sorted(refs.items()):
        filename = f"objects/{sha}.bin"
        if sha not in copied:
            data = await blobs.get(key, sha)
            if digest(data) != sha:
                raise BackupInvalid("Object bytes do not match database reference")
            write_private(directory / filename, data)
            copied.add(sha)
        else:
            # A second key with identical expected bytes must still exist and be
            # verified. A valid first copy cannot conceal a missing referenced key.
            data = await blobs.get(key, sha)
            if digest(data) != sha:
                raise BackupInvalid("Object bytes do not match database reference")
        objects.append({"key": key, "sha256": sha, "file": filename, "size": len(data)})
    manifest = {
        "format": FORMAT,
        "backup_id": uuid4().hex,
        "created_at": utc_isoformat(utcnow()),
        "database": database,
        "objects": objects,
        "restore_test": "not_performed",
    }
    # Publish the completion marker last. Errors leave a visible partial bundle;
    # this tool does not recursively remove sensitive evidence or operator files.
    write_private(directory / "manifest.json", json_bytes(manifest))
    verify_bundle(directory, allow_incomplete=True)
    (directory / "INCOMPLETE").unlink()
    return {
        "backup_id": manifest["backup_id"],
        "tables": len(database["tables"]),
        "objects": len(objects),
        "status": "bundle_verified",
        "restore_test": "not_performed",
    }


def safe_file(directory, relative):
    if not isinstance(relative, str):
        raise BackupInvalid("Invalid bundle path")
    root = directory.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path.is_symlink():
        raise BackupInvalid("Unsafe bundle path")
    return path


def verify_bundle(directory, *, allow_incomplete=False):
    directory = Path(directory)
    if not allow_incomplete and (directory / "INCOMPLETE").exists():
        raise BackupInvalid("Backup is incomplete")
    manifest = json.loads(
        safe_file(directory, "manifest.json").read_text(encoding="utf-8")
    )
    if (
        manifest.get("format") != FORMAT
        or manifest.get("restore_test") != "not_performed"
        or manifest["database"]["revision"] != schema_head()
    ):
        raise BackupInvalid("Unsupported backup format or revision")
    expected = {table.name: table for table in tables()}
    names, refs, version = set(), {}, []
    for entry in manifest["database"]["tables"]:
        name = entry["name"]
        if (
            name not in expected
            or name in names
            or entry["file"] != f"database/{name}.jsonl"
            or entry["columns"] != list(expected[name].columns.keys())
        ):
            raise BackupInvalid("Invalid table inventory")
        names.add(name)
        count, checksum = 0, hashlib.sha256()
        with safe_file(directory, entry["file"]).open("rb") as file:
            for line in file:
                checksum.update(line)
                values = json.loads(line)
                if not isinstance(values, list) or len(values) != len(entry["columns"]):
                    raise BackupInvalid("Invalid database row")
                row = dict(
                    zip(
                        entry["columns"],
                        [unpacked(value) for value in values],
                        strict=True,
                    )
                )
                if name == "intelligence_snapshots":
                    for key, sha in reference_pairs(row):
                        if key in refs and refs[key] != sha:
                            raise BackupInvalid("Conflicting object reference")
                        refs[key] = sha
                if name == "alembic_version":
                    version.append(row["version_num"])
                count += 1
        if count != entry["rows"] or checksum.hexdigest() != entry["sha256"]:
            raise BackupInvalid("Database file checksum or count mismatch")
    if names != set(expected) or version != [schema_head()]:
        raise BackupInvalid("Incomplete table inventory or revision")
    verified = {}
    for entry in manifest["objects"]:
        key, sha = entry["key"], entry["sha256"]
        if (
            refs.get(key) != sha
            or key in verified
            or entry["file"] != f"objects/{sha}.bin"
        ):
            raise BackupInvalid("Invalid object inventory")
        data = safe_file(directory, entry["file"]).read_bytes()
        if len(data) != entry["size"] or digest(data) != sha:
            raise BackupInvalid("Object checksum or size mismatch")
        verified[key] = sha
    if verified != refs:
        raise BackupInvalid("Referenced objects are missing")
    return {
        "status": "bundle_verified",
        "backup_id": manifest["backup_id"],
        "tables": len(names),
        "objects": len(verified),
        "restore_test": "not_performed",
    }


async def cli():
    parser = argparse.ArgumentParser(
        description="Private evidence backup; never restores or deletes database rows"
    )
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("export").add_argument("--output", required=True)
    commands.add_parser("verify").add_argument("--directory", required=True)
    args = parser.parse_args()
    try:
        if args.action == "verify":
            result = await asyncio.to_thread(verify_bundle, args.directory)
        else:
            from app.database import engine
            from app.intelligence.blob import configured_store

            try:
                blobs = configured_store()
                if hasattr(blobs, "verify_configuration"):
                    await blobs.verify_configuration()
                async with engine.connect() as connection:
                    result = await export_bundle(connection, blobs, args.output)
            finally:
                await engine.dispose()
        print(json.dumps(result))
    except Exception:
        # SDK/SQL/parser exceptions may contain credentials, object keys or source
        # text. Keep the private partial bundle for local investigation only.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "Backup operation failed; inspect the private bundle and runtime configuration",
                }
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(cli()))
