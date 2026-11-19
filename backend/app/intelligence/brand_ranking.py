"""Accept only explicit, complete numbered recommendation lists."""

import re

NUMBERED_LINE = re.compile(r"(?m)^\s*(?:#{1,6}\s*)?(?:\*\*)?(\d{1,3})[.、)]\s*[^\n]+")
ORDER_CUE = re.compile(r"推荐顺序|优先级从高到低|推荐排名|按推荐优先级")
NO_ORDER = r"不分先后|排名不代表|无先后|no particular order"


def declares_ranking(text, *, allow_ties=False):
    no_order = NO_ORDER if allow_ties else NO_ORDER + r"|并列"
    first_item = NUMBERED_LINE.search(text)
    heading = text[: first_item.start()] if first_item else text
    return not re.search(no_order, text, re.IGNORECASE) and bool(
        ORDER_CUE.search(heading)
    )


def verified_ranking(text, signals, *, allow_ties=False):
    if not declares_ranking(text, allow_ties=allow_ties):
        return {}
    result = {}
    next_rank = 1
    for line in NUMBERED_LINE.finditer(text):
        rank = int(line[1])
        if rank != next_rank:
            return {}
        candidates = [
            signal
            for signal in signals
            if signal.get("entity_id")
            and signal.get("explicit_rank") == rank
            and signal.get("recommendation") in (2, 3, 4)
            and signal["observed_name"] in line[0]
            and signal["start"] < line.end()
            and signal["end"] > line.start()
        ]
        ids = {signal["entity_id"] for signal in candidates}
        if not ids or ids & result.keys():
            return {}
        if len(ids) > 1 and (
            not allow_ties or not re.search(r"并列|同列|tied", line[0], re.IGNORECASE)
        ):
            return {}
        for entity_id in sorted(ids):
            result[entity_id] = {
                "rank": rank,
                "start": line.start(),
                "end": line.end(),
                "quote": line[0],
            }
        next_rank += len(ids)
    for row in result.values():
        row["list_size"] = len(result)
    return result
