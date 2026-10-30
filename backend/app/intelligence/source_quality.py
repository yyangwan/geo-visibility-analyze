"""Versioned saved-text suitability; HTTP 200 never proves an article is complete."""

VERSION = "source-quality-v1"


def assess_text(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact = " ".join(lines).casefold()
    heading = lines[0].casefold() if lines else ""
    state, codes = "text_available", []
    if not compact:
        state, codes = "empty", ["empty_saved_text"]
    elif len(text) < 3000 and (
        heading == "环境异常"
        and "完成验证后即可继续访问" in compact
        or heading
        in ("just a moment...", "just a moment", "attention required! | cloudflare")
        and any(
            marker in compact
            for marker in (
                "enable javascript",
                "verify you are human",
                "checking your browser",
            )
        )
        or heading in ("安全验证", "访问验证")
        and any(marker in compact for marker in ("完成验证", "验证码", "验证后继续"))
    ):
        state, codes = "access_challenge", ["access_challenge_saved_instead_of_article"]
    elif (
        len(text) < 1000
        and len(lines) >= 8
        and "暂无数据" in lines
        and any("上一篇" in line and "下一篇" in line for line in lines)
        and any(line.startswith("原文链接：") for line in lines)
        and not any(len(line) > 100 for line in lines)
    ):
        state, codes = (
            "suspected_navigation_shell",
            ["article_body_not_observed_in_saved_navigation"],
        )
    elif len(compact) < 300:
        state, codes = "text_fragment", ["short_saved_text_article_coverage_unknown"]
    return {
        "version": VERSION,
        "state": state,
        "characters": len(text),
        "article_completeness": "unknown",
        "evidence_scope": "saved_text_only",
        "evidence_usable": state
        not in ("empty", "access_challenge", "suspected_navigation_shell"),
        "disclosures": ["advertorial_disclaimer"]
        if "【广告】" in text and "免责声明" in text
        else [],
        "reason_codes": codes,
    }


def usable_snapshot(source):
    return (
        bool(source.get("snapshot_id"))
        and source.get("quality", {}).get("evidence_usable") is not False
    )
