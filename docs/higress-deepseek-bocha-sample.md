# Higress DeepSeek Bocha Sample

This sample shows the minimum gateway contract needed to keep DeepSeek search
results aligned with the official Bocha-backed search path.

The key rule is simple:

- preserve trace headers
- do not rewrite request bodies
- pass `search_engine: bocha` through unchanged

## Request Contract

When the backend uses `capture_mode: gateway_search`, the request body should
already contain the gateway search selector:

```json
{
  "model": "deepseek-v4-flash",
  "messages": [
    {
      "role": "user",
      "content": "your prompt"
    }
  ],
  "enable_search": true,
  "search_options": {
    "forced_search": true
  },
  "search_engine": "bocha"
}
```

Higress must forward that body unchanged. If a plugin or policy removes unknown
JSON keys, disable that transform for this route.

## Route Skeleton

Use a header-preserving reverse proxy route. Keep it conservative.

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

## Gateway Rules

1. Preserve `Authorization`, `Cookie`, `User-Agent`, `Origin`, and `Referer`.
2. Preserve `X-Analysis-Run-Id`, `X-Audit-Id`, and `X-Project-Id`.
3. Do not strip `search_engine` from the JSON body.
4. Do not add retries at the gateway layer for this route.
5. Keep the request body identical so source snapshots stay stable.

## Backend Expectation

The backend already defaults DeepSeek gateway search to `bocha` in
`gateway_search` mode. Higress only needs to forward the field and avoid body
mutation.

