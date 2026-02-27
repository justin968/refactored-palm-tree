# Traceability Prototype (In-place Refactor)

This repository now runs a multi-route web app that keeps the existing route patterns and extends the prototype to a ledger-first traceability model.

## Run
```bash
npm run dev
```
Open:
- `/login`
- `/totes/receive`
- `/drawdown`
- `/assign`
- `/totes/lookup`
- `/products`
- `/returns`
- `/repack`
- `/audit`
- `/admin/rebuild`

## What this implements

### Operator-friendly UI updates
- Receive screen simplified for floor use (core fields first, advanced fields collapsible).
- CoA/SDS/BOL/photo attachments can be added directly during receiving and linked to lot/HU.


### Operator lock-down
- Users must log in from `/login` before using app routes.
- Operator workflows are limited to operations (receive, drawdown, assign, returns, repack, lookup).
- Product/SKU/settings changes are ADMIN-only on backend APIs.
- Non-admin users are prevented from forcing `RELEASED` at receive (auto-downgraded to `PENDING`).

### Ledger-first traceability
- Append-only `transactions` ledger in `data/db.json`.
- No update/delete endpoints for ledger rows.
- Corrections are modeled as new events (`REVERSE`, `ADJUST`, etc.).
- Containment tree and lineage graph are separated:
  - Containment: `HandlingUnit.parentHuId`
  - Lineage: `Transaction` event chain

### Tamper-evident model
Each transaction stores:
- `seq` (monotonic)
- `prevHash`
- `payloadHash` (SHA-256 of canonical stable JSON payload)
- `chainHash` (SHA-256 of `prevHash + payloadHash`)
- `signature` (HMAC-SHA256 of `chainHash`, server secret)

Secret is server-side only via `AUDIT_HMAC_SECRET` env variable.

`/audit` verifies by re-hashing and can export audit bundle JSON.

### Roles
Header user switch simulates RBAC:
- `OPERATOR`
- `QA`
- `ADMIN`

Sensitive actions are policy-gated (Tier3-style checks for release/reverse/reauth metadata).

### Policies and settings
- `allowReshipSealedReturns`
- `allowSmallToBigOnlyViaRepack`
- `requireReasonForExceptions`
- `requireReleaseForFoodGrade`
- `enforceReauthOnSensitiveActions`

### Projections vs source of truth
- `HandlingUnit.qtyCurrentBase` is a cached projection.
- Ledger is source of truth.
- `/admin/rebuild` recomputes HU projections from the ledger if drift occurs.

Use rebuild when:
- projections drift from expected values,
- after manual recovery tasks,
- as a periodic integrity check in demo mode.

## Immutable vs mutable

Immutable (append-only intent):
- `transactions` ledger rows (no API update/delete path)

Mutable with audit log:
- `products`, `skus`, `settings`.
- changes recorded in `auditLogs` with before/after snapshots.

## Corrections model
Do **not** edit prior transactions.
Use correction events:
- `REVERSE` referencing `supersedesTransactionId`
- `ADJUST` with reason code and metadata
- status changes via dedicated events

## Seed/demo flow
1. Go to `/products` and create product/SKUs (or use `/api/seed` via curl).
2. Receive source HU at `/totes/receive`.
3. Drawdown to packaged units at `/drawdown`.
4. Attach CoA/SDS/photos during receipt as needed.
5. Assign packaged unit to order at `/assign`.
6. Receive/release return at `/returns`.
7. Repack conversions at `/repack`.
8. Verify hash chain at `/audit`.

## Important scope note
This is an **audit-defensible prototype** implementation (tamper-evident ledger, RBAC controls, correction semantics). It does **not** claim regulatory certification/compliance.

## Assumptions made
- Single-tenant org model.
- JSON-file persistence for speed (`data/db.json`) instead of Prisma/Postgres in this prototype pass.
- Production DB-level immutability trigger is documented target; app-level immutability is enforced in this implementation by design (append-only API paths).


## Vercel deployment note
- Added `vercel.json` rewrite routing all paths to `api/index`.
- `api/index.js` invokes the same app handler from `server.js` so `/` and all existing routes work on Vercel.
- On serverless runtimes, writes to project files are not guaranteed. The app now falls back to `/tmp/traceability-db.json` when `data/db.json` is not writable.
- Data on Vercel is therefore ephemeral by default unless you move persistence to an external database/storage.

