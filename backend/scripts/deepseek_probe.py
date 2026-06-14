"""Run a single DeepSeek prompt probe with UTF-8-safe input handling.

This script avoids PowerShell pipe/encoding issues by keeping the prompt in a
UTF-8 source file or reading it from a UTF-8 text file.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.adapters.deepseek import DeepSeekAdapter

DEFAULT_PROMPT = "200预算以内，手冲咖啡壶有什么推荐的"
DEFAULT_CHAT_SESSION_ID = "c515607d-e3ba-4586-9473-9fc32951d407"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="Prompt text to send to DeepSeek.",
    )
    parser.add_argument(
        "--prompt-file",
        type=Path,
        help="Optional UTF-8 encoded text file containing the prompt.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    prompt = args.prompt
    if args.prompt_file is not None:
        prompt = args.prompt_file.read_text(encoding="utf-8-sig").strip()

    adapter = DeepSeekAdapter()
    adapter.set_platform_config(
        {
            "capture_mode": "official_web",
            "web": {
                "endpoint": "https://chat.deepseek.com/api/v0/chat/completion",
                "chat_session_id": DEFAULT_CHAT_SESSION_ID,
                "parent_message_id": None,
                "model_type": "default",
                "thinking_enabled": False,
                "search_enabled": True,
                "ref_file_ids": [],
                "preempt": False,
            },
        }
    )
    result = (await adapter.query([prompt]))[0]

    print("PROMPT:", prompt)
    print("SUCCESS:", result.success)
    print("MODEL:", result.response_model)
    print("FINISH:", result.finish_reason)
    print("TEXT:")
    print(result.response_text)
    print("CITATIONS:", result.citations)
    print("SEARCH_METADATA:", result.search_metadata)


if __name__ == "__main__":
    asyncio.run(main())
