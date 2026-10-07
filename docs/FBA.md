# FBA operating module — implementation checkpoint

## Status

Implemented: offline replenishment calculation, measured unit/carton specification validation,
full-case planning, immutable pack revisions, idempotent local work-order drafts,
and individual-parcel content/measurement/tracking validation. CLI:

```sh
python -m diy_growth.fba /private/fba-request.json --db /private/fba-state.sqlite3
```

The request contains `request_key`, `pack_spec`, `snapshot`, `policy`, and `gates`.
Use the constructor and synthetic test fixtures in `tests/test_fba.py` as the schema reference.
Unknown measurements, quantities or approvals are unknown, NOT zero or an inferred default.
The command returns JSON, exits 2 on blocked input, and makes no network calls.

NOT implemented/deployed: Amazon offer conversion/listing writer, operational inventory ingestion,
inbound-plan/packing/placement/transport orchestration, carrier booking/labels/tracking API,
warehouse UI/scale/scanner integration, automatic ERP work-order release, receipt/claims executor,
and hosted recurring execution. This is a tested planning component, not an operational FBA autopilot.
No paid charge, listing, dimension, shipment, or carrier mutation can be made by this module.

## Data and safety contract

- Identity: marketplace + ASIN + exact seller SKU + FNSKU + approved pack revision.
- Keep a separate FBA seller SKU/offer linked to the exact existing ASIN when preserving FBM.
  Do not destructively convert the only FBM offer, guess a SKU suffix, duplicate ASINs,
  merge sizes/concentrations/grades, or overwrite pricing/content/shipping settings.
- Unit dimensions/gross weight describe one sellable, fully prepared unit.
  Carton dimensions/gross weight describe the entire outbound carton, including packaging.
  Net product weight is not either gross shipping weight. Read measurements from physical evidence.
- Reuse a verified pack revision only for unchanged bottle/bag, closure, protective packaging,
  count, orientation and carton. Re-measure changed configurations; record actual packed carton weight.
- The numeric checks do not prove chemical compatibility, package suitability, drop/leak-test compliance,
  carrier eligibility, or actual physical measurements. Those need authoritative evidence.
- Each gate binds to SKU/ASIN/marketplace/pack version, with an evidence reference and validity interval.
  Six gates are required: identity, FBA offer readback, inbound eligibility, dangerous-goods review,
  packout approval and replenishment-policy approval. A gate for a non-DG item still needs documented
  classification; never approve from a chemical name alone. An Amazon eligibility preview is not
  independent carrier authorization.
- These are caller-supplied assertions, not authentication. Production must ingest trusted signed/authenticated
  records, authorize operators, bind approvals to policy digests, and prevent arbitrary gate edits.
- `daily_units` needs a justified stockout-aware demand estimate from authoritative sales/order data.
  `fba_fulfillable` excludes reserved/unfulfillable stock. `on_time_inbound` excludes late/uncertain arrivals.
  `open_local_work` excludes units already counted as inbound. The three sets MUST be disjoint.
  Missing raw reconciliation evidence must leave `positions_reconciled` false.
- `warehouse_unallocated`, `cash_remaining`, and `capacity_remaining_after_commitments` are NET of every
  existing reservation/commitment. Shared warehouse/cash/capacity reservations across SKUs are not
  implemented: use one SKU at a time until transactional global allocation exists.
- Target = daily units × (production+transit+receiving lead days + review interval + safety days).
  Needed = max(0, target − fulfillable − on-time inbound − open local work).
  Round the requirement UP to full cases, then cap DOWN to full cases for stock, capacity, cash
  and the explicitly supplied maximum-order policy. This is a deterministic heuristic, not an optimized forecast.
- The module reserves an INTERNAL DRAFT, not a sale, accounting invoice, customer order or Amazon shipment.
  Duplicated request keys return the existing record only for identical substantive content.
  A different active draft for the same marketplace/SKU is blocked. Pack revisions cannot silently change.
  There is deliberately no automatic close/cancel/expiry: reconcile before replacing an active draft.
- Parcel validation is one SKU/full cases. Partial cartons and mixed-SKU cartons require a new supported flow.
  Each local carton has a unique Amazon box ID and individual child tracking number, actual measurements,
  FNSKU, pack revision, operator and lot evidence. A multi-piece master tracking number is not reused for all boxes.
- Dimensions/weight tolerances are explicit company policy, not represented as universal Amazon rules.
  The current Amazon/carrier limits must be checked separately at release time.
- LTL/pallet shipments need a separate pallet/BOL/appointment workflow. Do not apply parcel-only rules to LTL.
- Manifest validated ≠ carrier accepted; carrier delivered ≠ Amazon checked in/received.
  Never synthesize receipt or claim evidence. The returned receipt flags deliberately remain false.
- Retrying after an ambiguous external timeout must reconcile remote state, not blindly create another plan,
  purchase another label or accept a placement/transport charge again.

## Operational integration sequence (remaining)

1. Run in a private, authenticated service with persistent database, backups, audit log, secret storage,
   health checks and a real scheduler. Never put operational manifests, emails or credentials in this public repo.
2. Authorize a seller-owned Amazon SP-API application with only necessary roles and its own runtime credentials.
   A ChatGPT reporting connector is not runtime authentication and must not be exported as if it were.
3. Read current listings, actual SKU mapping, inventory positions, capacity and applicable fees/prep rules.
   Retrieve the current product-type schema, prepare the smallest offer update, and retain before/after evidence.
   Capture submission/processing/issue results and read the offer back; HTTP acceptance is not completion.
4. For approved stocked offers, create the inbound plan; persist its operation and plan identifiers immediately.
   Follow the CURRENT packing/placement workflow and poll operation results to completion. Enumerate valid options,
   apply an owner-approved all-in cost policy, and do not accept placement or transportation charges without it.
5. After actual packing, submit packing information; reconcile plan IDs, shipment UUIDs, confirmation IDs and
   Amazon box IDs. These are different identifiers; legacy `FBA...` labels are not API UUIDs.
6. Obtain proper carrier labels for the actual compliant packout, retrieve the Amazon box labels and FNSKU labels,
   join each box to its carrier tracking record, and update Amazon shipment tracking.
7. Release an ERP/warehouse task only once with an idempotent external reference. Capture actual lot/box scans.
   Do not pass a fake retail order through WooCommerce just to make ShipStation see the work.
8. Watch carrier acceptance, delivery, Amazon receiving, shortages, reconciliations and sellable inventory.
   Escalate on documented promised dates/current rules rather than fabricated universal deadlines.
9. Replay real historical shipments in read-only shadow mode, reconcile expected vs actual output, then authorize
   a bounded initial SKU/budget policy. Only after live end-to-end evidence should unattended writes be enabled.

## Current official API references (checked October 7, 2026)

- Listings Items / current schemas / processing and readback:
  https://developer-docs.amazon/sp-api/docs/listings-items-api
- FBA eligibility preview (preview, not comprehensive clearance):
  https://developer-docs.amazon/sp-api/reference/getitemeligibilitypreview
- Inbound plans v2024-03-20:
  https://developer-docs.amazon/sp-api/reference/createinboundplan
- Box packing information:
  https://developer-docs.amazon/sp-api/reference/setpackinginformation
- Shipment tracking updates:
  https://developer-docs.amazon/sp-api/reference/updateshipmenttrackingdetails

US inbound-plan documentation currently says `AMAZON` is not accepted for `labelOwner`.
Resolve permitted preparation/label ownership from current item requirements rather than promising
Amazon will perform preparation. Poll async operation results and verify downstream state.

## Next completion gate

Connect the operational Amazon + carrier adapters and private runtime, load trusted physical packaging
specifications, and complete one supervised shipment through Amazon receiving without duplicate orders
or unexplained box discrepancies. Until then this component is explicitly offline and draft-only.
