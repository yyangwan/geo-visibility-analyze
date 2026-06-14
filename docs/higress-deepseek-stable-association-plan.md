# Higress + DeepSeek Stable Association Implementation Plan

## Objective

Make every DeepSeek answer traceable to one exact source snapshot, even when the request goes through Higress and the backend retries the work.

This plan is executable. It splits the work into concrete backend changes, gateway wiring, and verification steps.

## Current Baseline

What already exists in this repo:

- DeepSeek web capture is implemented in [backend/app/adapters/deepseek_web.py](../backend/app/adapters/deepseek_web.py).
- The backend persists `analysis_run_id` on [Audit](../backend/app/models/models.py).
- The backend persists `source_snapshot_hash` on [PlatformResponseRecord](../backend/app/models/models.py).
- Audit APIs now expose `analysis_run_id` in [backend/app/api/audits.py](../backend/app/api/audits.py) and [backend/app/api/schemas.py](../backend/app/api/schemas.py).

What does not exist here:

- No Higress route or plugin config.
- No gateway-side request correlation propagation.
- No gateway-side tool orchestration code.
- No end-to-end smoke test that exercises Higress plus backend together.

## Decision

Keep Higress thin unless you have a hard requirement for tool orchestration in the gateway.

Recommended split:

- Higress handles routing, auth/header forwarding, and request correlation propagation.
- Backend handles persistence, source snapshot hashing, retries, and traceability.

This keeps the backend as the source of truth and avoids hidden state in the gateway.

## Data Contract

Use one stable parent id for the whole analysis and one snapshot hash per platform response.

### Backend-owned fields

- `analysis_run_id`: one per audit, generated when the audit is claimed or lazily assigned at execution start.
- `source_snapshot_hash`: one per persisted platform response, derived from the source payload.

### Snapshot hash inputs

Hash the following together:

- citations
- search metadata
- request params

Do not hash the raw answer text alone. The point is to tie the answer to the exact retrieved source set.

### Suggested wire contract through Higress

If the gateway needs to carry trace data, pass a correlation header or request field with the same id:

```text
X-Analysis-Run-Id: <analysis_run_id>
```

If the gateway must keep more context, it can also forward:

```json
{
  "analysis_run_id": "uuid-or-hex",
  "prompt": "user prompt",
  "chat_session_id": "optional",
  "parent_message_id": "optional",
  "request_params": {},
  "gateway_metadata": {}
}
```

## Work Plan

### Phase 1: Correlation Contract

Goal: make one request, one parent analysis id.

Tasks:

- Add or confirm the correlation header format used by Higress.
- Ensure the backend can log and persist the same id.
- Keep the id stable across retries.

Files likely touched:

- `backend/app/services/audit_service.py`
- `backend/app/api/audits.py`
- `backend/app/api/schemas.py`
- `backend/app/models/models.py`

Acceptance:

- One audit maps to one `analysis_run_id`.
- Retries stay under the same parent id.
- The id appears in serialized audit payloads.

### Phase 2: Snapshot Boundaries

Goal: make each response point to the exact source snapshot that produced it.

Tasks:

- Build the digest from citations, search metadata, and request params.
- Persist the digest on `platform_response_records`.
- Keep raw response archiving intact for replay/debugging.

Files likely touched:

- `backend/app/services/audit_service.py`
- `backend/app/models/models.py`
- `backend/alembic/versions/add_analysis_run_snapshot_hash.py`

Acceptance:

- Different source payloads produce different hashes.
- Same payload produces the same hash.
- The hash is stored with the response record.

### Phase 3: Higress Wiring

Goal: make the gateway carry the trace context without owning persistence.

Tasks:

- Add a route or plugin config that forwards the correlation id.
- Forward headers needed by DeepSeek web auth and search.
- Preserve the request payload shape expected by the backend or upstream.
- If Higress is doing tool orchestration, define the exact request/response contract for the search tool and final model answer.

Files likely touched outside this repo:

- Higress route config
- Higress plugin config
- Gateway logs / tracing config

Acceptance:

- Higress forwards the correlation id unchanged.
- Higress does not mutate the source snapshot payload.
- The backend can still compute the same `source_snapshot_hash` from the stored request metadata.

### Phase 4: Verification

Goal: prove the contract works end to end.

Tasks:

- Add or run a smoke test that exercises the DeepSeek web path.
- Verify the backend persists `analysis_run_id` and `source_snapshot_hash`.
- Verify retry behavior keeps the same parent id.
- Verify payload changes change the snapshot hash.

Files likely touched:

- `backend/tests/test_audit_workflow.py`
- `backend/tests/test_deepseek_web_adapter.py`
- `backend/tests/test_response_parser.py`

Acceptance:

- Backend tests pass.
- Smoke test passes against the gateway path or a mocked Higress contract.
- There is no ambiguity about which source snapshot produced an answer.

## Concrete Execution Order

1. Finalize the correlation header name and payload contract.
2. Apply the backend schema and API changes.
3. Wire the gateway to pass the correlation id and required headers.
4. Add smoke coverage for Higress plus DeepSeek web.
5. Validate retries, hashes, and traceability.

## Non-Goals

- Do not move persistence into Higress.
- Do not add a new top-level run table unless the gateway repo proves it needs one.
- Do not expose these ids to the UI unless a downstream consumer needs them.

## Risks

- If Higress mutates the payload, the snapshot hash will not match the stored response context.
- If the correlation id is regenerated in the gateway, retries will look like separate analyses.
- If the gateway keeps hidden state, debugging will become slow.

## Done Means

This work is complete when all of the following are true:

- One audit has one stable `analysis_run_id`.
- Each persisted platform response has one `source_snapshot_hash`.
- Higress forwards the correlation id unchanged.
- The backend can explain which source snapshot produced a given answer.
- A smoke test covers the DeepSeek web path with the gateway contract.

## Recommended Next Step

Implement Phase 1 and Phase 2 in the backend repo first, then add the Higress wiring in its own repo or config bundle. That keeps the contract stable before you touch gateway behavior.

For the gateway-side draft configuration, see [Higress Gateway Config Draft](higress-gateway-config-draft.md).
