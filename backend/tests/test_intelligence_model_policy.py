import json

import httpx
import pytest

from app.intelligence.evidence import verify_pair
from app.intelligence.model_policy import structured_options
from app.intelligence.semantics import (
    SemanticOutputError,
    analyze_text,
    checked_result,
    completed_content,
    decode_semantic,
)


def entity(quote="品牌", **changes):
    return {
        "name": "品牌",
        "kind": "brand",
        "quote": quote,
        "start": 0,
        "end": 0,
        "sentiment": "unknown",
        "attributes": [],
        **changes,
    }


def test_zero_or_negative_proposed_offsets_can_only_be_repaired_by_a_unique_quote():
    result = checked_result(
        "开头：品牌适用范围待核实。",
        {"entities": [entity("品牌适用范围待核实。", start=-5)]},
    )
    assert result["entities"][0]["start"] == 3
    assert result["entities"][0]["end"] == len("开头：品牌适用范围待核实。")
    assert result["span_repairs"][0]["method"] == "unique_verbatim"
    assert result["span_repairs"][0]["original_end"] == 0
    ambiguous = checked_result("品牌与品牌", {"entities": [entity()]})
    assert ambiguous["entities"] == [] and ambiguous["rejected_spans"] == 1
    assert ambiguous["rejected_items"][0]["code"] == "verbatim_span_ambiguous"
    assert ambiguous["rejected_items"][0]["candidate"]["name"] == "品牌"


def test_negative_python_indexing_is_never_accepted_as_an_exact_span():
    result = checked_result("品牌", {"entities": [entity(start=-2, end=2)]})
    assert result["entities"][0]["start"] == 0
    assert result["span_repairs"]


@pytest.mark.parametrize("finish", [None, "length", "content_filter", "tool_calls"])
def test_truncated_or_unknown_finish_never_becomes_a_checked_empty_result(finish):
    with pytest.raises(SemanticOutputError) as raised:
        completed_content(
            {"choices": [{"finish_reason": finish, "message": {"content": "{}"}}]}
        )
    assert raised.value.fields == [{"path": [], "type": "incomplete_completion"}]


def test_vendor_switch_cannot_send_deepseek_thinking_options_to_another_host():
    assert structured_options("https://api.deepseek.com/v1", "deepseek-v4-flash")[
        "thinking"
    ] == {"type": "disabled"}
    assert "thinking" not in structured_options(
        "https://other.invalid/v1", "deepseek-v4-flash"
    )
    assert (
        structured_options(
            "https://api.deepseek.com", "deepseek-flash", verification=True
        )["max_tokens"]
        == 2048
    )


@pytest.mark.asyncio
async def test_both_extraction_and_verification_use_the_bounded_non_thinking_profile(
    monkeypatch,
):
    requests = []

    class Client:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, url, **kwargs):
            requests.append(kwargs["json"])
            content = (
                {"entities": [], "claims": [], "topics": [], "brand_signals": [], "information_units": []}
                if len(requests) == 1
                else {"relation": "supports", "source_quote": "来源"}
            )
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(content)},
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    result, _ = await analyze_text(
        "无品牌文字", "fake", "https://api.deepseek.com", "deepseek-v4-flash"
    )
    verdict, usage = await verify_pair(
        "观点", "来源", "fake", "https://api.deepseek.com", "deepseek-v4-flash"
    )
    assert result["entities"] == [] and verdict["relation"] == "supports"
    assert usage["completion_tokens"] == 20
    assert [row["max_tokens"] for row in requests] == [8192, 2048]
    assert all(row["thinking"] == {"type": "disabled"} for row in requests)


def test_boolean_offsets_still_cannot_be_repaired_into_valid_evidence():
    with pytest.raises(SemanticOutputError):
        decode_semantic(
            "品牌",
            json.dumps(
                {
                    "entities": [entity(start=False)],
                    "claims": [],
                    "topics": [],
                    "brand_signals": [],
                }
            ),
        )
