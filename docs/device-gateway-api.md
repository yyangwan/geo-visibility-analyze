# Device Gateway Task API

The visibility service owns the durable task queue. Device gateways establish
outbound HTTPS connections to claim work, so Appium and ADB never need public
inbound ports.

## Authentication

Business endpoints use the existing GeniLink project-scoped JWT.

Gateway endpoints require:

```http
Authorization: Bearer <AISCOPE_DEVICE_GATEWAY_TOKEN>
X-Gateway-Id: CHAO
```

Production also restricts IDs through `AISCOPE_DEVICE_GATEWAY_IDS`.

## Business Endpoints

- `POST /api/device-tasks`: enqueue an idempotent task.
- `GET /api/device-tasks?project_id=...`: list project tasks.
- `GET /api/device-tasks/{task_id}`: inspect status and result.

Supported task types:

- `gateway.healthcheck`
- `appium.prompt`

An `appium.prompt` task requires `platform`, `surface` (`web` or `app`), and
`payload.prompt`.

## Gateway Endpoints

- `POST /api/device-gateway/heartbeat`
- `POST /api/device-gateway/tasks/claim`
- `POST /api/device-gateway/tasks/{task_id}/heartbeat`
- `POST /api/device-gateway/tasks/{task_id}/complete`
- `POST /api/device-gateway/tasks/{task_id}/fail`

Claim returns a one-time lease token. Only its SHA-256 hash is stored. The
gateway must renew long-running tasks before the lease expires. Expired tasks
can be claimed again until `max_attempts` is reached.

## Example

```json
{
  "project_id": "project-id",
  "task_type": "appium.prompt",
  "target_gateway_id": "CHAO",
  "platform": "doubao",
  "surface": "app",
  "payload": {
    "prompt": "请推荐适合办公室使用的咖啡机"
  },
  "idempotency_key": "audit-42-doubao-app-prompt-7"
}
```

Do not place platform passwords, cookies, or session tokens in task payloads.
Those credentials belong on the gateway with local ACL protection.
