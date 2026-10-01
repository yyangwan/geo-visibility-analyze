from pathlib import Path

import yaml


def test_backend_uses_reachable_genilink_jwks_default() -> None:
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    backend = compose["services"]["backend"]

    assert "https://genilink.cn/.well-known/jwks.json" in backend["environment"][
        "AISCOPE_GENILINK_JWKS_URL"
    ]


def test_production_database_can_be_overridden_without_changing_development() -> None:
    root = Path(__file__).resolve().parents[1]
    development = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    production = yaml.safe_load((root / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    local_url = "mysql+aiomysql://aiscope:aiscope@mysql:3306/aiscope"

    assert development["services"]["backend"]["environment"]["AISCOPE_DATABASE_URL"] == local_url
    assert production["services"]["backend"]["environment"]["AISCOPE_DATABASE_URL"] == (
        "${AISCOPE_DATABASE_URL:-" + local_url + "}"
    )
