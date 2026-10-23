"""Accept only explicit, complete, single numbered recommendation lists."""

import re


def verified_ranking(text, signals):
    if re.search(r"不分先后|并列|排名不代表|无先后|no particular order", text, re.IGNORECASE):
        return {}
    lines = list(
        re.finditer(r"(?m)^\s*(?:#{1,6}\s*)?(?:\*\*)?(\d{1,3})[.、)]\s*[^\n]+", text)
    )
    if not lines or [int(line[1]) for line in lines] != list(range(1, len(lines) + 1)):
        return {}
    if not re.search(
        r"推荐顺序|优先级从高到低|推荐排名|按推荐优先级", text[: lines[0].start()]
    ):
        return {}
    result = {}
    for rank, line in enumerate(lines, 1):
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
        if len(ids) != 1 or next(iter(ids)) in result:
            return {}
        result[next(iter(ids))] = {
            "rank": rank,
            "list_size": len(lines),
            "start": line.start(),
            "end": line.end(),
            "quote": line[0],
        }
    return result
