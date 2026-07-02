# Product Website Visibility Operations

This feature analyzes a project product website for AI visibility readiness. It can run with native HTML fetch only, or with Firecrawl and real domestic AI citation checks when those capabilities are enabled.

## Crawler Provider

Default:

```env
AISCOPE_PRODUCT_WEBSITE_CRAWLER_PROVIDER=native
AISCOPE_PRODUCT_WEBSITE_CRAWLER_TIMEOUT_SECONDS=30
```

Firecrawl:

```env
AISCOPE_PRODUCT_WEBSITE_CRAWLER_PROVIDER=firecrawl
AISCOPE_FIRECRAWL_API_KEY=...
AISCOPE_FIRECRAWL_BASE_URL=https://api.firecrawl.dev
AISCOPE_FIRECRAWL_WAIT_FOR_MS=1000
AISCOPE_FIRECRAWL_MAX_AGE_MS=172800000
```

If Firecrawl is selected but `AISCOPE_FIRECRAWL_API_KEY` is empty, the service falls back to the native crawler. Per-run UI options may request Firecrawl, but the service still requires the API key.

## Real AI Citations

Default:

```env
AISCOPE_PRODUCT_WEBSITE_AI_CITATION_ENABLED=false
AISCOPE_PRODUCT_WEBSITE_AI_CITATION_PLATFORMS=deepseek,doubao,hunyuan,qwen,kimi
AISCOPE_PRODUCT_WEBSITE_AI_CITATION_PROMPT_LIMIT=2
```

The platform list is intentionally restricted to the domestic model adapters already used by the visibility service:

- `deepseek`
- `doubao`
- `hunyuan`
- `qwen`
- `kimi`

Citation checks can be enabled globally by environment variable or per run by the platform request payload. A citation-check failure is recorded in the result snapshot and event log, but it does not fail the whole product website analysis.

## Result Snapshot Fields

The completed analysis stores:

- `score`: overall grade and dimension scores.
- `page`: extracted title, headings, links, schema, and content metrics.
- `diagnostics`: crawler provider, status code, duration, and crawler metadata.
- `recommendations`: prioritized website optimization actions.
- `aiCitations`: prompts, per-platform citation counts, own-domain citation counts, and product mention status when citation checks are enabled.
