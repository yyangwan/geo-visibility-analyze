"""Publish persisted, local recommendation evidence without inventing scores."""

from app.intelligence.semantics import stable_hash

BRAND_SIGNAL_VERSION = "brand-signals-v1"


def summarize_brand_signals(applications, responses, mentions, context_analysis=None):
    observations = []
    expected, extracted, rejected = 0, 0, 0
    for application, segment, cache in applications:
        response = responses.get(segment.owner_id)
        if segment.owner_kind != "answer" or not response or response.get("error"):
            continue
        expected += 1
        signals = cache.result.get("brand_signals") if cache else None
        if signals is None:
            continue
        rejected += cache.result.get("rejected_spans", 0)
        if application.status == "completed":
            extracted += 1
        for signal in signals:
            start = segment.start_offset + signal["start"]
            end = segment.start_offset + signal["end"]
            # Bind only to verified mentions within this exact evidence span.
            # Same-name entities elsewhere in a response cannot supply identity.
            matched = [
                mention for mention in mentions
                if mention["segment_id"] == segment.id
                and mention["observed_entity_name"] == signal["name"]
                and start <= mention["start"] < mention["end"] <= end
                and not mention["identity_ambiguity"]
            ]
            entity_ids = sorted({mention["entity_id"] for mention in matched})
            requirement_evidence = []
            if context_analysis is not None:
                from app.intelligence.information_units import local_units

                units = {item["id"]: item for item in local_units(cache.result)}
                for context_row in context_analysis["observations"]:
                    if (context_row["status"] != "completed"
                        or context_row["owner_kind"] != "answer"
                        or context_row["owner_id"] != segment.owner_id
                        or context_row["segment_id"] != segment.id):
                        continue
                    for match in context_row["interpretation"]["requirement_matches"]:
                        unit = units[match["unit_id"]]
                        if (match["relation"] == "addresses" and unit.get("subject") == signal["name"]
                            and signal["start"] <= match["start"] < match["end"] <= signal["end"]
                            and not any(other["unit_id"] == match["unit_id"] and other["relation"] == "contradicts"
                                        for other in context_row["interpretation"]["requirement_matches"])):
                            requirement_evidence.append({**match, "context_analysis_id": context_row["id"]})
            reason_level = signal["reason_level"]
            if context_analysis is not None:
                # Owner-free extraction cannot establish a question-specific match.
                if reason_level == "need_matched":
                    reason_level = "specific"
                if requirement_evidence and reason_level == "specific":
                    reason_level = "need_matched"
            observations.append({
                "id": stable_hash([BRAND_SIGNAL_VERSION, segment.id, signal]),
                "response_id": segment.owner_id,
                "segment_id": segment.id,
                "platform": response["platform"],
                "prompt_hash": response["prompt_hash"],
                "observed_name": signal["name"],
                "entity_id": entity_ids[0] if len(entity_ids) == 1 else None,
                "mention_ids": sorted(mention["id"] for mention in matched),
                "quote": signal["quote"], "start": start, "end": end,
                "recommendation": signal["recommendation"],
                "explicit_rank": signal["explicit_rank"],
                "reason_level": reason_level,
                "requirement_evidence": requirement_evidence,
                "analysis_status": application.status,
                "review_state": "unreviewed",
                "scope": "segment",
            })
    return {
        "version": BRAND_SIGNAL_VERSION,
        "status": "extracted" if expected and extracted == expected else "partial" if observations else "pending",
        "answer_segments": expected,
        "extracted_segments": extracted,
        "rejected_spans": rejected,
        "observations": sorted(observations, key=lambda item: (item["response_id"], item["start"], item["id"])),
        # Scoring additionally needs frozen competitive prompts and sample weights,
        # whole-answer aggregation and confirmed identity. This is not a score.
        "score_status": "awaiting_evaluation_cohort",
    }
