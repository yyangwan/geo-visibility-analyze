"""Query Harvester - Real user search query collection.

Collects authentic user queries from search engine autocomplete APIs.
These represent real questions users actually ask, not synthetic templates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx
from urllib.parse import quote

from .decision_intent_filter import DecisionIntentFilter

logger = logging.getLogger(__name__)


class QueryHarvester:
    """Harvest real user search queries from search engines."""

    def __init__(self, timeout: float = 5.0, max_retries: int = 3) -> None:
        self.timeout = timeout
        self.max_retries = max_retries

    async def _fetch_with_retry(
        self,
        url: str,
        params: dict | None = None,
        source_name: str = "unknown",
    ) -> httpx.Response | None:
        """Fetch with retry logic for transient errors.

        Args:
            url: The URL to fetch
            params: Query parameters
            source_name: Name of the source (for logging)

        Returns:
            Response object or None if all retries exhausted
        """
        last_error = None

        for attempt in range(self.max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(url, params=params, follow_redirects=True)

                    # Log success on retry
                    if attempt > 0:
                        logger.info(
                            f"{source_name} succeeded on attempt {attempt + 1}",
                            extra={"source": source_name, "attempt": attempt + 1},
                        )

                    return resp

            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(
                    f"{source_name} timeout (attempt {attempt + 1}/{self.max_retries})",
                    extra={"source": source_name, "attempt": attempt + 1},
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2**attempt)  # Exponential backoff

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    last_error = e
                    logger.warning(
                        f"{source_name} rate limited (attempt {attempt + 1})",
                        extra={"source": source_name, "status": 429},
                    )
                    if attempt < self.max_retries - 1:
                        # Longer backoff for rate limiting
                        await asyncio.sleep(5 + (5 * attempt))
                else:
                    # Non-retryable HTTP error
                    logger.error(
                        f"{source_name} HTTP {e.response.status_code}: not retrying",
                        extra={"source": source_name, "status": e.response.status_code},
                    )
                    return None

            except Exception as e:
                # Unexpected error - don't retry
                logger.error(
                    f"{source_name} unexpected error: {e}",
                    extra={"source": source_name, "error": str(e)},
                )
                return None

        # All retries exhausted
        logger.error(
            f"{source_name} failed after {self.max_retries} attempts",
            extra={"source": source_name, "attempts": self.max_retries, "last_error": str(last_error)},
        )
        return None

    async def from_baidu(self, keyword: str, count: int = 10) -> list[str]:
        """Fetch query suggestions from Baidu autocomplete.

        Baidu uses GB2312 encoding and returns JSONP-wrapped responses.
        This is the primary source for Chinese user queries.

        Args:
            keyword: Search keyword
            count: Maximum number of suggestions to return

        Returns:
            List of query strings, empty list on failure
        """
        try:
            # Baidu requires GB2312 encoding
            encoded = quote(keyword.encode('gb2312'))
            url = f"https://suggestion.baidu.com/su?wd={encoded}&cb=window.baidu.sug"

            resp = await self._fetch_with_retry(url, source_name="baidu")
            if resp is None:
                return []

            # Strip JSONP wrapper: window.baidu.sug({...});
            json_str = resp.text.replace('window.baidu.sug(', '').rstrip(');')

            # Baidu returns non-standard JSON with single quotes, need to handle it
            # Extract suggestions using regex as fallback
            try:
                data = json.loads(json_str)
                suggestions = data.get('s', [])
            except json.JSONDecodeError:
                # Fallback: extract array content using regex
                import re
                match = re.search(r's:\[(.*?)\]', json_str)
                if match:
                    # Split by comma and clean up quotes
                    raw_items = match.group(1).split(',')
                    suggestions = [item.strip('"\'') for item in raw_items if item.strip()]
                else:
                    suggestions = []
            if not isinstance(suggestions, list):
                return []

            return suggestions[:count]

        except json.JSONDecodeError as e:
            logger.warning(f"Baidu JSON decode failed for '{keyword}': {e}")
            return []
        except httpx.TimeoutError:
            logger.warning(f"Baidu timeout for '{keyword}'")
            return []
        except Exception as e:
            logger.warning(f"Baidu harvest failed for '{keyword}': {e}")
            return []

    async def from_sogou(self, keyword: str, count: int = 10) -> list[str]:
        """Fetch query suggestions from Sogou autocomplete.

        Sogou provides good coverage for Chinese search queries.

        Args:
            keyword: Search keyword
            count: Maximum number of suggestions to return

        Returns:
            List of query strings, empty list on failure
        """
        try:
            url = "https://www.sogou.com/sugg"
            params = {
                "type": "web",
                "keyword": keyword,
            }

            resp = await self._fetch_with_retry(url, params=params, source_name="sogou")
            if resp is None:
                return []

            data = resp.json()
            suggestions = data.get("sugg", [])

            if not isinstance(suggestions, list):
                return []

            return suggestions[:count]

        except json.JSONDecodeError as e:
            logger.warning(f"Sogou JSON decode failed for '{keyword}': {e}")
            return []
        except httpx.TimeoutError:
            logger.warning(f"Sogou timeout for '{keyword}'")
            return []
        except Exception as e:
            logger.warning(f"Sogou harvest failed for '{keyword}': {e}")
            return []

    async def from_bing(self, keyword: str, count: int = 10) -> list[str]:
        """Fetch query suggestions from Bing autocomplete.

        Bing provides global search data, less comprehensive for Chinese queries
        but useful as a fallback.

        Args:
            keyword: Search keyword
            count: Maximum number of suggestions to return

        Returns:
            List of query strings, empty list on failure
        """
        try:
            url = "https://api.bing.com/osjson.aspx"
            params = {
                "query": keyword,
                "mkt": "zh-CN",  # Chinese market
            }

            resp = await self._fetch_with_retry(url, params=params, source_name="bing")
            if resp is None:
                return []

            data = resp.json()
            if not isinstance(data, list) or len(data) < 2:
                return []

            # Bing returns [keyword, [suggestion1, suggestion2, ...]]
            suggestions = data[1] if isinstance(data[1], list) else []
            return suggestions[:count]

        except Exception as e:
            logger.warning(f"Bing harvest failed for '{keyword}': {e}")
            return []

    def _dedupe(self, queries: list[str]) -> list[str]:
        """Remove duplicates while preserving order."""
        seen = set()
        result = []
        for q in queries:
            if q and q not in seen:
                result.append(q)
                seen.add(q)
        return result

    def _filter_quality(self, queries: list[str], min_length: int = 3) -> list[str]:
        """Filter out low-quality suggestions."""
        result = []
        for q in queries:
            q = q.strip()
            if len(q) < min_length:
                continue
            # Filter out purely numeric or special-char strings
            if re.match(r'^[\d\s\W]+$', q):
                continue
            result.append(q)
        return result

    async def harvest(
        self,
        keyword: str,
        count: int = 10,
        sources: list[str] | None = None,
        apply_intent_filter: bool = True,
    ) -> list[str]:
        """Harvest queries from multiple search engines.

        Args:
            keyword: Search keyword (typically product name or category)
            count: Target number of unique queries to return
            sources: List of sources to try ['baidu', 'sogou', 'bing'],
                     defaults to ['baidu', 'sogou']
            apply_intent_filter: Whether to apply decision intent filtering (default: True)

        Returns:
            Deduplicated, quality-filtered, intent-filtered query strings
        """
        if sources is None:
            sources = ['baidu', 'sogou']

        all_results = []

        # Try each source
        for source in sources:
            if source == 'baidu':
                results = await self.from_baidu(keyword, count * 2)  # Fetch extra for dedup
            elif source == 'sogou':
                results = await self.from_sogou(keyword, count * 2)
            elif source == 'bing':
                results = await self.from_bing(keyword, count * 2)
            else:
                logger.warning(f"Unknown source: {source}")
                continue

            all_results.extend(results)

            # If we have enough, stop
            if len(self._dedupe(all_results)) >= count:
                break

        # Post-process
        unique = self._dedupe(all_results)
        filtered = self._filter_quality(unique)

        # Apply decision intent filtering if enabled
        if apply_intent_filter:
            intent_filter = DecisionIntentFilter()
            filtered = intent_filter.filter_batch(filtered)
            logger.info(
                "intent_filter_applied",
                extra={
                    "keyword": keyword,
                    "before_count": len(unique),
                    "after_count": len(filtered),
                },
            )

        return filtered[:count]


async def harvest_queries(
    keyword: str,
    count: int = 10,
    sources: list[str] | None = None,
    timeout: float = 5.0,
) -> list[str]:
    """Convenience function to harvest queries without instantiating class.

    Args:
        keyword: Search keyword
        count: Target number of queries
        sources: Sources to try (default: ['baidu', 'sogou'])
        timeout: Request timeout in seconds

    Returns:
        List of harvested query strings
    """
    harvester = QueryHarvester(timeout=timeout)
    return await harvester.harvest(keyword, count=count, sources=sources)
