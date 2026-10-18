"""Conservative rejection of UI progress labels, never a completeness inference."""

import re

_STATUS_ONLY = re.compile(
    r"(?:已完成思考|思考已完成|正在思考|思考中|正在搜索|搜索中)"
    r"(?:[，,。\s]*(?:参考了\s*\d+\s*篇资料|已阅读\s*\d+\s*个网页))?[。\s]*"
    r"|参考了\s*\d+\s*篇资料[。\s]*"
)


def is_status_only_answer(text):
    return bool(_STATUS_ONLY.fullmatch(text.strip()))
