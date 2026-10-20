import json

import pytest

from app.intelligence import semantics


def test_prompt_example_obeys_strict_attribute_schema():
    example = next(
        line for line in semantics.SYSTEM.splitlines() if line.startswith('{"entities"')
    )
    result = semantics.SemanticResult.model_validate(json.loads(example))
    assert result.entities[0].attributes == []
    assert 'attributes' in semantics.SYSTEM
    assert '["' in semantics.SYSTEM


def test_object_attributes_are_not_coerced_or_silently_dropped():
    text = "Acme kettle costs 99."
    content = {
        "entities": [{
            "name": "Acme", "kind": "brand", "quote": "Acme", "start": 0,
            "end": 4, "sentiment": "neutral", "attributes": {"price": "99"},
        }],
        "claims": [], "topics": [],
    }
    with pytest.raises(semantics.SemanticOutputError) as caught:
        semantics.decode_semantic(text, json.dumps(content))
    assert caught.value.fields == [
        {"path": ["entities", 0, "attributes"], "type": "list_type"}
    ]
    content["entities"][0]["attributes"] = ["costs 99"]
    assert semantics.decode_semantic(text, json.dumps(content))["entities"][0]["attributes"] == ["costs 99"]


def test_prompt_version_changes_invalidate_analysis_signature(monkeypatch):
    original = semantics.analysis_signature("same-model", "https://provider.example")
    monkeypatch.setattr(semantics, "SYSTEM", semantics.SYSTEM + "\nDifferent prompt.")
    assert semantics.analysis_signature("same-model", "https://provider.example") != original
