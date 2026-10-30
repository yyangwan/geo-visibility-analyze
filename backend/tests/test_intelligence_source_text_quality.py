import pytest

from app.intelligence.source_quality import assess_text, usable_snapshot


@pytest.mark.parametrize(
    "text",
    [
        "环境异常\n当前环境异常，完成验证后即可继续访问。\n去验证",
        "Just a moment...\nEnable JavaScript and verify you are human.",
        "安全验证\n请完成验证后继续访问",
    ],
)
def test_known_challenge_shell_is_preserved_but_not_article_evidence(text):
    quality = assess_text(text)
    assert quality["state"] == "access_challenge"
    assert quality["article_completeness"] == "unknown"
    assert not usable_snapshot({"snapshot_id": "saved", "quality": quality})


def test_short_fragment_is_not_promoted_to_a_complete_article():
    quality = assess_text("Practical reviews of named tools - LLMrefs")
    assert quality["state"] == "text_fragment"
    assert quality["evidence_usable"]
    assert quality["evidence_scope"] == "saved_text_only"
    assert quality["article_completeness"] == "unknown"


def test_discussion_of_captcha_inside_a_real_article_is_not_automatically_blocked():
    assert assess_text("如何排查环境异常\n完成验证后即可继续访问。这是案例分析。 ")[
        "evidence_usable"
    ]
    assert assess_text("环境异常\n我们的产品如何处理各种复杂网络环境。 ")[
        "evidence_usable"
    ]
    assert not assess_text(" \n")["evidence_usable"]
    assert not usable_snapshot({"snapshot_id": None})


def test_known_missing_body_navigation_shell_is_flagged_without_discarding_it():
    text = "新榜有赚\n服务\n营销\n行业报告\n暂无数据\n原文链接：https://example.com/story\n<上一篇下一篇>\n马上咨询\n关于我们"
    quality = assess_text(text)
    assert quality["state"] == "suspected_navigation_shell"
    assert not quality["evidence_usable"]
    assert assess_text("行业报告\n暂无数据\n" + "有完整段落和分析的正文" * 20)[
        "evidence_usable"
    ]


def test_explicit_ad_disclosure_is_kept_without_claiming_truth_or_independence():
    quality = assess_text("品牌信息\n【广告】免责声明：本内容为广告。")
    assert quality["evidence_usable"]
    assert quality["disclosures"] == ["advertorial_disclaimer"]
