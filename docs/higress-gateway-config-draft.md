# Higress Gateway Config Draft

This draft is the gateway-side companion to the backend wiring in this repo.
It keeps Higress thin: preserve correlation headers, forward auth/session
headers needed by DeepSeek web capture, and avoid mutating request bodies.

## Goal

Carry one stable analysis correlation id through the gateway path so the
backend can tie:

- one audit
- one DeepSeek request set
- one response snapshot hash

The gateway should not own persistence or retry state.

## Contract

Use the following correlation headers end to end:

- `X-Analysis-Run-Id`
- `X-Audit-Id`
- `X-Project-Id`

These are emitted by the backend adapters and should be preserved by Higress
if it forwards traffic or performs request mirroring/debug routing.

## Minimal Gateway Responsibilities

1. Preserve the incoming correlation headers.
2. Forward DeepSeek web session headers unchanged when present.
3. Avoid rewriting request JSON bodies for the audit path.
4. Include the correlation id in gateway access logs.
5. Keep upstream selection deterministic so retries do not fan out into a new
   gateway-side identity.

## Draft Route Shape

Use this as a gateway-side skeleton, regardless of whether the final deploy
uses Gateway API, Ingress, or a Higress-specific CRD bundle.

```yaml
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: geo-visibility-api
spec:
  hosts:
    - geo-visibility.example.com
  gateways:
    - higress-system/geo-visibility-gateway
  http:
    - name: audit-api
      match:
        - uri:
            prefix: /api/
      route:
        - destination:
            host: geo-visibility-backend.default.svc.cluster.local
            port:
              number: 8000
      headers:
        request:
          set:
            X-Analysis-Run-Id: "%REQ(X-Analysis-Run-Id)%"
            X-Audit-Id: "%REQ(X-Audit-Id)%"
            X-Project-Id: "%REQ(X-Project-Id)%"
      timeout: 60s
      retries:
        attempts: 1
```

Notes:

- This is intentionally conservative. The backend already retries platform
  calls, so the gateway should not multiply retries for this path.
- If your Higress deployment uses a different CRD shape, keep the same header
  contract and route semantics.

## Draft Header Policy

Preserve these headers if present:

- `X-Analysis-Run-Id`
- `X-Audit-Id`
- `X-Project-Id`
- `Authorization`
- `Cookie`
- `User-Agent`
- `Origin`
- `Referer`

Do not strip:

- `Content-Type`
- `Accept`
- `Accept-Language`

If the gateway must add defaults, only do it when the header is missing.
Do not overwrite values that the backend or browser session already set.

## Draft Access Log

Include the correlation id in gateway logs so requests can be joined with the
backend audit record.

Example log fields:

```text
timestamp method path status upstream_host request_id x_analysis_run_id x_audit_id x_project_id latency_ms
```

## DeepSeek Web Path

For DeepSeek web capture, Higress should forward the same session headers the
backend adapter already sends:

- `Authorization`
- `Cookie`
- `Origin`
- `Referer`
- `User-Agent`
- any additional DeepSeek-specific browser headers already configured

For the DeepSeek gateway search path, pass the search engine explicitly so the
gateway can route to the same source pool that DeepSeek uses upstream:

- `search_engine: bocha`

Do not inject snapshot-specific data into the request body. The backend hashes
the persisted response metadata, and changing the body in the gateway would
change the resulting snapshot identity.

## Recommended Deployment Pattern

### Option A: Header-preserving reverse proxy

Use this if Higress only fronts the backend API.

Benefits:

- smallest surface area
- easiest to validate
- no extra request mutation logic

### Option B: Gateway plus debug mirroring

Use this if you want a second upstream for tracing or replay.

Rules:

- mirror only read-only debug traffic
- never mirror writes that create or mutate audits
- keep the same correlation headers on the mirrored request

### Option C: Tool orchestration in gateway

Only use this if a separate product requirement forces tool routing into
Higress.

If you take this path:

- define a strict request/response schema for each tool hop
- pass `X-Analysis-Run-Id` through every hop
- keep tool outputs immutable before they reach the backend

## Verification Checklist

1. Send one audit request with a known `X-Analysis-Run-Id`.
2. Confirm Higress forwards the same header to the backend.
3. Confirm the backend persists the same `analysis_run_id`.
4. Confirm DeepSeek web calls also carry the same trace headers.
5. Confirm `source_snapshot_hash` stays stable when the source payload is
   unchanged.
6. Confirm changing citations, search metadata, or request params changes the
   hash.

## Open Questions For The Gateway Repo

- Which Higress deployment style is used: Gateway API, Ingress, or a custom
  CRD bundle?
- Do you want the gateway to add the correlation header when the client omits
  it, or only preserve what the backend generates?
- Should access logs be shipped to the same sink as backend audit logs?

## Related Work In This Repo

- [Higress + DeepSeek stable association implementation plan](higress-deepseek-stable-association-plan.md)
- [Higress DeepSeek Bocha sample](higress-deepseek-bocha-sample.md)
- [Backend trace propagation](../backend/app/adapters/base.py)
- [Audit execution context injection](../backend/app/services/audit_service.py)
