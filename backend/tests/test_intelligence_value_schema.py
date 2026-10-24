import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from app.database import Base
from app.intelligence.project_context import ContextRequest, receive_context
from app.intelligence.schema_version import SCHEMA_REVISION
from scripts import initialize_value_database as initializer


def test_fresh_baseline_contains_every_latest_model_and_no_drop_operations():
    root = Path(__file__).parents[1]
    config = Config(str(root / "alembic.ini"))
    assert ScriptDirectory.from_config(config).get_heads() == [SCHEMA_REVISION]
    source = (root / "alembic/value_schema/20261024_value_baseline.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    created = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_table"
    }
    assert created == set(Base.metadata.tables)
    assert {
        "intelligence_context_analyses",
        "intelligence_owned_assets",
        "intelligence_action_plan_revisions",
    } <= created
    assert "drop_table" not in source and "existing = set(sa.inspect" in source


def test_initializer_refuses_wrong_target_and_nonempty_database(monkeypatch):
    monkeypatch.setattr(
        initializer.settings, "database_url", "mysql+pymysql://localhost/target"
    )
    engine = MagicMock()
    monkeypatch.setattr(initializer, "create_engine", lambda _: engine)
    with pytest.raises(ValueError, match="explicitly named"):
        initializer.initialize("other")
    engine.connect.assert_not_called()
    monkeypatch.setattr(
        initializer,
        "inspect",
        lambda _: SimpleNamespace(get_table_names=lambda: ["business_table"]),
    )
    monkeypatch.setattr(
        initializer.MigrationContext,
        "configure",
        lambda _: SimpleNamespace(get_current_revision=lambda: None),
    )
    upgrade = MagicMock()
    monkeypatch.setattr(initializer.command, "upgrade", upgrade)
    with pytest.raises(ValueError, match="not empty"):
        initializer.initialize("target")
    upgrade.assert_not_called()
    engine.dispose.assert_called_once()


def test_initialized_contract_check_is_readonly(monkeypatch):
    monkeypatch.setattr(
        initializer.settings, "database_url", "mysql+pymysql://localhost/target"
    )
    engine = MagicMock()
    monkeypatch.setattr(initializer, "create_engine", lambda _: engine)
    inspector = SimpleNamespace(
        get_table_names=lambda: [*Base.metadata.tables, "alembic_version"],
        get_columns=lambda name: [
            {"name": column.name} for column in Base.metadata.tables[name].columns
        ],
    )
    monkeypatch.setattr(initializer, "inspect", lambda _: inspector)
    monkeypatch.setattr(
        initializer.MigrationContext,
        "configure",
        lambda _: SimpleNamespace(get_current_revision=lambda: SCHEMA_REVISION),
    )
    upgrade = MagicMock()
    monkeypatch.setattr(initializer.command, "upgrade", upgrade)
    assert initializer.initialize("target")["status"] == "already_initialized"
    upgrade.assert_not_called()


@pytest.mark.parametrize(
    "preferences",
    [
        {
            "version": "business-preferences-v1",
            "scenario_importance": {"cost": True},
            "reason": "这是业务设置依据",
        },
        {
            "version": "business-preferences-v1",
            "scenario_importance": {"cost": 101},
            "reason": "这是业务设置依据",
        },
        {
            "version": "business-preferences-v1",
            "scenario_importance": {"cost": 0},
            "reason": "这是业务设置依据",
        },
        {
            "version": "business-preferences-v1",
            "scenario_importance": {"cost": 50},
            "reason": "短",
        },
        {
            "version": "business-preferences-v1",
            "scenario_importance": {"cost": 50},
            "reason": "这是业务设置依据",
            "score_weights": [1],
        },
    ],
)
def test_business_importance_cannot_modify_scoring_or_invent_a_priority(preferences):
    with pytest.raises(ValidationError):
        ContextRequest(version=1, brands=[], business_preferences=preferences)


@pytest.mark.asyncio
async def test_preferences_have_a_new_frozen_receipt_without_mutating_prior_context(
    db_session,
):
    brands = [{"id": "brand", "name": "云衡", "aliases": ["Yunheng"]}]
    first = await receive_context(
        db_session, "p1", ContextRequest(version=1, brands=brands), "reviewer"
    )
    assert (
        first.content_hash
        == "75b8c5e3cbd6d5429ec93aabbd3f6b4da0d823d196c51dcd33fa740f7a15475e"
    )
    preference = {
        "version": "business-preferences-v1",
        "scenario_importance": {"cost": 80},
        "reason": "本季度优先回应报价范围问题",
    }
    second = await receive_context(
        db_session,
        "p1",
        ContextRequest(version=2, brands=brands, business_preferences=preference),
        "reviewer",
    )
    assert first.brands == second.brands and first.business_preferences == {}
    assert second.business_preferences == preference
    assert first.content_hash != second.content_hash
