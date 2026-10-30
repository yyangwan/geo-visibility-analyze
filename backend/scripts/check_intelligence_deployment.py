"""Secret-free production database guard and deployment-specific worker probe."""

import argparse
import json
import os
import re
import sys
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def guarded_database_url():
    # Parse the effective settings, including the same env-file conventions as API.
    from app.config import settings

    url = make_url(settings.database_url)
    if url.drivername != "mysql+aiomysql" or url.database != "intelligence_value_20261030":
        raise RuntimeError("Refusing a database outside intelligence_value_20261030")
    if os.getenv("AISCOPE_DEBUG") != "0":
        raise RuntimeError("Production requires explicit AISCOPE_DEBUG=0")
    if not settings.intelligence_enabled:
        raise RuntimeError("Production intelligence must be explicitly enabled")
    if settings.intelligence_environment != "production" or settings.intelligence_blob_backend != "oss":
        raise RuntimeError("Production requires the OSS storage adapter")
    return url.set(drivername="mysql+pymysql")


def worker_ready(connection, since, worker_id):
    if not isinstance(worker_id, str) or not re.fullmatch(r"[a-f0-9]{32}", worker_id):
        raise ValueError("Candidate worker identity is required")
    return bool(connection.execute(text("""
        SELECT COUNT(*) FROM intelligence_worker_heartbeats
        WHERE id = :worker_id AND started_at > :since
          AND last_seen_at >= CURRENT_TIMESTAMP - INTERVAL 45 SECOND
          AND state IN ('idle', 'working')
          AND storage_status = 'configuration_verified'
          AND disk_status = 'ok'
          AND storage_checked_at >= started_at AND disk_checked_at >= started_at
    """), {"since": since, "worker_id": worker_id}).scalar_one())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["validate", "watermark", "worker", "health"])
    parser.add_argument("--since")
    parser.add_argument("--worker-id")
    args = parser.parse_args()
    if args.mode == "health":
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=5) as response:
            data = json.load(response)
        if data.get("status") != "ok" or data.get("db") != "connected" or data.get("deploymentVersion") != os.environ.get("DEPLOYMENT_VERSION"):
            raise RuntimeError("Candidate health or deployment revision mismatch")
        print("health_verified")
        return
    url = guarded_database_url()
    if args.mode == "validate":
        print("production_database_guard_verified")
        return
    engine = create_engine(url, connect_args={"connect_timeout": 5, "init_command": "SET time_zone = '+00:00'"})
    try:
        with engine.connect() as connection:
            if args.mode == "watermark":
                # Heartbeat columns have second precision; strict greater-than
                # and the pre-start delay in the deploy script exclude old rows.
                print(connection.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one().strftime("%Y-%m-%d %H:%M:%S"))
            else:
                since = datetime.strptime(args.since or "", "%Y-%m-%d %H:%M:%S")
                if not worker_ready(connection, since, args.worker_id):
                    raise RuntimeError("Candidate worker has no healthy new heartbeat")
                print("candidate_worker_verified")
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Configuration/driver exceptions can contain connection details.
        print(f"deployment_probe_failed class={type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
