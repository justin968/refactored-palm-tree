# FBA operations v2 — operational clients and durable execution journal

Checkpoint: 2026-10-07. This continues PR #4, not a replacement of the original planner.
`fba.py` is still offline. The new modules add operational API clients and a private CLI;
statements in FBA.md that those adapters are unimplemented are superseded only to the extent below.
No production account was contacted or changed during this tranche. Tests use synthetic transports.

## Implemented

- Amazon North America SP-API client with LWA token refresh/caching, percent-encoded listing reads,
  detailed inventory pagination, inbound plan/item/box reads and asynchronous operation polling.
- Four single-attempt mutation adapters: createInboundPlan, setPackingInformation,
  updateShipmentTrackingDetails and createShipStationShipment. No purchase or listing-write endpoint.
- Physical carton capture BEFORE Amazon IDs or tracking exist, using the saved draft's exact FNSKU,
  pack revision, unit count, measured gross weight/dimensions, lot reference, operator and timestamp.
- Immutable physical scans and separate unique Amazon-box/individual-carrier-tracking links.
- Payloads reconstructed from recorded work and scans; no fake retail sales order, no inferred
  measurement, no use of a net two-pound label as gross packed shipping weight.
- SQLite transactional execution claims, immutable proposal hashes, expiring per-action approvals,
  account binding, a separate explicit runtime operation allowlist, and append-only action events.
- Safe restart behavior: unknown results and interrupted dispatches cannot be resubmitted automatically.
- Create-only recovery via an exact remote object ID, authenticated readback and comparison of the
  actual source/contents or shipment fields. Never search by name and silently adopt the first match.
- Private CLI status/exception output, raw operational snapshots, credential-presence diagnostics,
  explicit approvals, supervised dispatch, result polling and read-only recovery.

## Completion claims to keep separate

STAGED -> APPROVED -> DISPATCHING -> SUBMITTED -> API_SUCCEEDED or REMOTE_VERIFIED.

REJECTED, UNCERTAIN, API_FAILED and interrupted DISPATCHING records require reconciliation.
HTTP 202 means submitted, not API success. API success is not downstream readback, shipping,
receiving or sellable inventory. REMOTE_VERIFIED means matching remote object fields only.
No state sets carrier_acceptance_verified or amazon_receipt_verified to true.

The four mutation adapters are executable code, but they are NOT a complete Send to Amazon
or carrier-booking orchestrator. They require an exact reviewed proposal and a private trusted
runtime. Default configuration cannot dispatch. No current ChatGPT connector token was exported.

## Run on a private machine

Python 3.11+; standard library at runtime. From this repository:

```sh
export PYTHONPATH=src
python -m diy_growth.fba_ops --db /private/fba.sqlite3 doctor
python -m diy_growth.fba /private/fba-request.json --db /private/fba.sqlite3
python -m diy_growth.fba_ops --db /private/fba.sqlite3 capture WORK_KEY /private/box.json
python -m diy_growth.fba_ops --db /private/fba.sqlite3 link LOCAL_BOX_ID /private/link.json
python -m diy_growth.fba_ops --db /private/fba.sqlite3 stage ACTION_KEY /private/proposal.json
python -m diy_growth.fba_ops --db /private/fba.sqlite3 show ACTION_KEY
python -m diy_growth.fba_ops --db /private/fba.sqlite3 status
```

Use config/fba.env.example only as a list of environment variable names. Store actual secrets
outside this public repository. `doctor` reports presence only, NOT successful authentication.
The private Linux CLI sets database/output file mode 0600. Use a dedicated OS account and a 0700
state directory, and back up SQLite using its backup API, not a naked copy while WAL is active.
This is a local trusted-operator CLI, not an authenticated multi-user web application.
Operator names/evidence references are audit fields, not signatures or an identity provider.
A user with database/file access can alter assertions; do not expose this as a public endpoint.

### Operational reads

After configuring the seller-owned runtime application:

```sh
python -m diy_growth.fba_ops --db /private/fba.sqlite3 inventory --out /private/inventory-UNIQUE.json
python -m diy_growth.fba_ops --db /private/fba.sqlite3 inbound-plans --out /private/inbound-UNIQUE.json
python -m diy_growth.fba_ops --db /private/fba.sqlite3 listing EXACT_SELLER_SKU --out /private/listing-UNIQUE.json
```

Outputs never overwrite a previous snapshot. A pagination/schema failure is not empty inventory.
Snapshots include collection start/end timestamps and request IDs; multi-page reads are not atomic.
Inventory rows are raw operational evidence. Demand estimation, on-time inbound classification,
resource reservations and normalization into the v1 replenishment snapshot are NOT automatically done.
The CLI does not create an unattended scheduler or change the previously scheduled correspondence watch.

### Physical box JSON

Required: local_box_id, msku, fnsku, pack_version, units, gross_lb, dimensions_in (L/W/H),
lot_ref, packed_by, packed_at (timezone required), measurement_evidence_ref.
Physical capture rejects shipping fields. Capture the actual gross weight for each carton.
Changed dimensions/weights/lot data on an already recorded barcode do not silently overwrite it.
Corrections require a future explicit revision/reconciliation workflow; editing the SQLite tables
is not the supported operator workflow.

Separate link JSON: amazon_box_id, carrier, tracking_number, evidence_ref, linked_by, linked_at.
Scan the actual Amazon and child tracking labels; never align two lists merely by array position.
Manufacturing lot codes and expiration values must be supplied explicitly to payload builders.
An internal LotProof lot reference is not automatically a manufacturer lot code or expiration date.

### Proposal JSON and approval

Exactly these fields are required:

```json
{
  "scope": "amazon:NA:EXACT_SELLER_ID:EXACT_MARKETPLACE_ID",
  "operation": "createInboundPlan",
  "work_key": "EXISTING_SAVED_DRAFT_KEY",
  "ids": {},
  "body": {},
  "evidence_ref": "PRIVATE_CURRENT_PREP_DESTINATION_AND_ASSIGNMENT_EVIDENCE",
  "valid_until": "EXPLICIT_FUTURE_ISO_TIMESTAMP_WITH_TIMEZONE"
}
```

`body` must be built with fba_packout.inbound_plan_payload, packing_payload, tracking_payload
or shipstation_payload and must exactly match recorded work/scans. Tests supply runnable
synthetic examples for every supported operation; empty body above is intentionally not valid.
Packing requires exactly one actual confirmed packingGroupId or shipmentId. Both must NOT be sent.
The current inbound API plan/shipment IDs are distinct from older FBA confirmation/label IDs.

Approve the exact hash returned by stage/show with the local trusted operator:

```sh
python -m diy_growth.fba_ops --db /private/fba.sqlite3 approve ACTION_KEY \
  --digest EXACT_REVIEWED_HASH --operator OPERATOR --evidence-ref PRIVATE_REVIEW_REF \
  --valid-until EXPLICIT_EXPIRY
```

Dispatch additionally requires FBA_ENABLE_MUTATIONS=yes AND an explicit comma-separated
FBA_ALLOWED_OPERATIONS allowlist. Neither is enabled by this code or by the example config.
Then the explicit `execute ACTION_KEY` command can make the single approved mutation.
`refresh ACTION_KEY` polls its result using reads. Existing cross-channel policies are unchanged;
this standalone FBA CLI has its own explicit, per-action authorization boundary.

For interrupted or uncertain creates, stop the old worker, identify the exact remote ID,
and use `adopt ACTION_KEY --remote-id ... --operator ... --evidence-ref ...`.
A five-minute engineering grace interval prevents recovery while the old 20-second HTTP call
might still be active. This is not a carrier/Amazon SLA. Adoption performs only reads and
blocks mismatches. Unknown packing/tracking results still require manual operation-ID
reconciliation; the CLI cannot safely recover those without the missing operation evidence.
No blind retry, auto-expiry/requeue, cancellation, shipment deletion or refund is implemented.

ShipStation v2 external_shipment_id is NOT guaranteed unique remotely. This journal enforces
one operation per work order and a unique remote-object association locally. Out-of-band
Seller Central/ShipStation changes still require fresh reconciliation before approval.
Creating a pending ShipStation shipment creates no label and no retail sales order. It does
not establish hazmat carrier support or protect against unrelated existing account automations;
inspect account rules before first production dispatch.

## Still unfinished

- Seller-owned runtime authorization and an actual private deployment; no Docker/cloud runtime
  was provisioned or tested here. Private secrets, backups, logging, operator auth and hosting remain.
- FBM-preserving FBA offer creation/conversion, product-type-schema validation, dimensions listing
  submission and live listing readback. Current listing support is read-only.
- Full packing/placement/transport option generation, cost decisions, confirmations, labels,
  carrier hazmat service configuration, LTL/BOL/pallet appointments and label purchases.
- Automatic Amazon box-ID reconciliation, warehouse scan/scale UI, ERP/work-order release,
  scan corrections and per-lot/per-expiration mixed-case support.
- Cross-SKU warehouse/cash/capacity reservation, live demand normalization and automatic replenishment.
- Carrier scans, Amazon receipt/shortage claims, notification delivery/deduplication and scheduler.
- Downstream verification for normal Amazon writes beyond async operation success; created-object
  adoption verifies its source/items, while successful routine submits still need downstream checks.

Existing v1 approvals are caller assertions; current prep/eligibility, exact shipment membership,
warehouse reservations, carrier support and economics must be independently verified before release.
Until these gaps and one supervised physical shipment are closed, this is not an unattended autopilot.

## Official contracts checked October 7, 2026

- https://developer-docs.amazon/sp-api/docs/connecting-to-the-selling-partner-api
- https://developer-docs.amazon/sp-api/reference/getinventorysummaries
- https://developer-docs.amazon/sp-api/reference/createinboundplan
- https://developer-docs.amazon/sp-api/reference/setpackinginformation
- https://developer-docs.amazon/sp-api/reference/getinboundoperationstatus
- https://developer-docs.amazon/sp-api/docs/create-non-partnered-carrier-shipment
- https://docs.shipstation.com/shipments/create

Only generic code, documentation and synthetic test fixtures belong in the public repository.
The prior private 15-SKU measurement queue remains private and unmodified; no measurements were invented.
