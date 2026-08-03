from pathlib import Path

import yaml


def test_backend_uses_reachable_genilink_jwks_default() -> None:
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    backend = compose["services"]["backend"]

    assert "https://genilink.cn/.well-known/jwks.json" in backend["environment"][
        "AISCOPE_GENILINK_JWKS_URL"
    ]
