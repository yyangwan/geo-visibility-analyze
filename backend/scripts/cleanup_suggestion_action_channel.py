"""Remove legacy `detail.action_channel` from suggestion records.

This strips the old field from existing suggestion JSON payloads so only the
new evidence/action split remains.
"""

import asyncio

from sqlalchemy import text

from app.database import async_session, engine


async def main() -> None:
    try:
        async with async_session() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE suggestions
                    SET detail = JSON_REMOVE(detail, '$.action_channel')
                    WHERE JSON_EXTRACT(detail, '$.action_channel') IS NOT NULL
                    """
                )
            )
            await session.commit()
            print(f"Updated {result.rowcount or 0} suggestion rows")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
