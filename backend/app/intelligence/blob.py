"""Content-verified private blobs. No implicit local fallback in production."""

import asyncio
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class IntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class BlobInfo:
    key: str
    sha256: str
    size: int
    version_id: str | None = None


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_key(key: str) -> str:
    if not re.fullmatch(r"projects/[a-zA-Z0-9_-]+/[a-zA-Z0-9_./-]+", key):
        raise ValueError("Invalid application object key")
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError("Unsafe object key")
    return key


class BlobStore(Protocol):
    async def put_immutable(self, key: str, data: bytes) -> BlobInfo: ...
    async def get(self, key: str, sha256: str) -> bytes: ...
    async def head(self, key: str) -> BlobInfo: ...


class LocalBlobStore:
    """Development only: exclusive atomic publication; reject symlink escapes."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Object path escapes root")
        return path

    def _put(self, key: str, data: bytes) -> BlobInfo:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # A same-volume hard link publishes the complete fsynced file without overwrite.
        fd, temporary = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            if target.read_bytes() != data:
                raise IntegrityError("Immutable key already contains different bytes")
            return BlobInfo(key, digest(data), len(data))
        finally:
            os.unlink(temporary)

    async def put_immutable(self, key: str, data: bytes) -> BlobInfo:
        return await asyncio.to_thread(self._put, key, data)

    async def get(self, key: str, sha256: str) -> bytes:
        data = await asyncio.to_thread(self.path(key).read_bytes)
        if digest(data) != sha256:
            raise IntegrityError("Blob checksum mismatch")
        return data

    async def head(self, key: str) -> BlobInfo:
        data = await asyncio.to_thread(self.path(key).read_bytes)
        return BlobInfo(key, digest(data), len(data))


class OssBlobStore:
    def __init__(self, bucket):
        self.bucket = bucket

    async def verify_configuration(self):
        acl = await asyncio.to_thread(self.bucket.get_bucket_acl)
        versioning = await asyncio.to_thread(self.bucket.get_bucket_versioning)
        if acl.acl != "private":
            raise ValueError("Citation evidence requires a private bucket")
        # Forbid-overwrite is ineffective on versioned/suspended buckets. Version-ID
        # pinning is a later adapter feature; fail closed rather than pretend immutability.
        if versioning.status not in (None, ""):
            raise ValueError(
                "This adapter requires a bucket that has never enabled versioning"
            )

    def _put(self, key: str, data: bytes) -> BlobInfo:
        import oss2

        validate_key(key)
        expected = digest(data)
        try:
            self.bucket.put_object(
                key,
                data,
                headers={
                    "x-oss-forbid-overwrite": "true",
                    "x-oss-meta-sha256": expected,
                    "Content-Type": "application/octet-stream",
                    "x-oss-server-side-encryption": "AES256",
                },
            )
        except oss2.exceptions.ServerError as error:
            if error.code != "FileAlreadyExists":
                raise
        # Verify real bytes, including existing-key retries; ETag is not SHA-256.
        actual = self.bucket.get_object(key).read()
        if len(actual) != len(data) or digest(actual) != expected:
            raise IntegrityError("OSS object integrity check failed")
        return BlobInfo(key, expected, len(data))

    async def put_immutable(self, key: str, data: bytes) -> BlobInfo:
        return await asyncio.to_thread(self._put, key, data)

    async def get(self, key: str, sha256: str) -> bytes:
        validate_key(key)
        data = await asyncio.to_thread(lambda: self.bucket.get_object(key).read())
        if digest(data) != sha256:
            raise IntegrityError("OSS checksum mismatch")
        return data

    async def head(self, key: str) -> BlobInfo:
        validate_key(key)
        info = await asyncio.to_thread(self.bucket.head_object, key)
        return BlobInfo(
            key, info.headers.get("x-oss-meta-sha256", ""), info.content_length
        )


def configured_store():
    from app.config import settings

    if settings.intelligence_blob_backend == "local":
        if settings.intelligence_environment not in ("development", "test"):
            raise ValueError("Local blobs are forbidden outside development/test")
        return LocalBlobStore(Path(settings.intelligence_blob_root))
    if (
        settings.intelligence_blob_backend != "oss"
        or not settings.intelligence_oss_bucket
    ):
        raise ValueError("A private OSS bucket must be configured")
    import oss2
    from alibabacloud_credentials.client import Client
    from alibabacloud_credentials.models import Config
    from oss2.credentials import Credentials

    class RoleProvider(oss2.CredentialsProvider):
        def __init__(self):
            self.client = Client(
                Config(
                    type="ecs_ram_role",
                    role_name=settings.intelligence_oss_role or None,
                    enable_imds_v2=True,
                    disable_imds_v1=True,
                )
            )

        def get_credentials(self):
            credential = self.client.get_credential()
            return Credentials(
                credential.access_key_id,
                credential.access_key_secret,
                credential.security_token,
            )

    return OssBlobStore(
        oss2.Bucket(
            oss2.ProviderAuth(RoleProvider()),
            settings.intelligence_oss_endpoint,
            settings.intelligence_oss_bucket,
            connect_timeout=30,
        )
    )
