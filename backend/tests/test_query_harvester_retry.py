"""Tests for QueryHarvester retry logic."""

import pytest
from unittest.mock import AsyncMock, patch
import httpx

from app.services.query_harvester import QueryHarvester


class TestFetchWithRetry:
    """Test _fetch_with_retry method."""

    @pytest.fixture
    def harvester(self):
        """Create a QueryHarvester with max_retries=2 for faster testing."""
        return QueryHarvester(timeout=5.0, max_retries=2)

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self, harvester):
        """Test successful fetch on first attempt."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.text = '{"suggestion": "test"}'

            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_instance.get = AsyncMock(return_value=mock_response)
            mock_client.return_value = mock_instance

            result = await harvester._fetch_with_retry("http://example.com", source_name="test")

            assert result is not None
            assert mock_instance.get.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_timeout(self, harvester):
        """Test retry on timeout error."""
        with patch("httpx.AsyncClient") as mock_client, patch("asyncio.sleep") as mock_sleep:
            mock_response = AsyncMock()
            mock_response.status_code = 200

            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None

            # Create timeout exception
            timeout_request = httpx.Request("GET", "http://example.com")
            timeout_error = httpx.TimeoutException("Request timeout", request=timeout_request)

            # First call raises timeout, second succeeds
            mock_instance.get = AsyncMock(side_effect=[timeout_error, mock_response])
            mock_client.return_value = mock_instance

            result = await harvester._fetch_with_retry("http://example.com", source_name="test")

            assert result is not None
            assert mock_instance.get.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_on_429_rate_limit(self, harvester):
        """Test retry on 429 rate limit error."""
        with patch("httpx.AsyncClient") as mock_client, patch("asyncio.sleep") as mock_sleep:
            mock_success_response = AsyncMock()
            mock_success_response.status_code = 200

            mock_429_response = AsyncMock()
            mock_429_response.status_code = 429

            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None

            # Create HTTP error
            request = httpx.Request("GET", "http://example.com")
            http_error = httpx.HTTPStatusError(
                "Rate limited",
                request=request,
                response=mock_429_response,
            )

            # First call raises 429, second succeeds
            mock_instance.get = AsyncMock(side_effect=[http_error, mock_success_response])
            mock_client.return_value = mock_instance

            result = await harvester._fetch_with_retry("http://example.com", source_name="test")

            assert result is not None
            assert mock_instance.get.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_none_after_max_retries(self, harvester):
        """Test returns None after exhausting all retries."""
        with patch("httpx.AsyncClient") as mock_client, patch("asyncio.sleep"):
            request = httpx.Request("GET", "http://example.com")
            timeout_error = httpx.TimeoutException("Request timeout", request=request)

            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_instance.get = AsyncMock(side_effect=timeout_error)
            mock_client.return_value = mock_instance

            result = await harvester._fetch_with_retry("http://example.com", source_name="test")

            assert result is None
            assert mock_instance.get.call_count == 2  # max_retries

    @pytest.mark.asyncio
    async def test_no_retry_on_non_4xx_http_error(self, harvester):
        """Test no retry on non-429 HTTP errors."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_404_response = AsyncMock()
            mock_404_response.status_code = 404

            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None

            # Create HTTP error
            request = httpx.Request("GET", "http://example.com")
            http_error = httpx.HTTPStatusError(
                "Not found",
                request=request,
                response=mock_404_response,
            )

            mock_instance.get = AsyncMock(side_effect=http_error)
            mock_client.return_value = mock_instance

            result = await harvester._fetch_with_retry("http://example.com", source_name="test")

            assert result is None
            assert mock_instance.get.call_count == 1  # No retry for 404
