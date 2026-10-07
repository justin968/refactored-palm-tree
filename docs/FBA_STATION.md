# FBA v3: warehouse station and shared reservations

Checkpoint: October 7, 2026. Continues PRs #4 and #5. This tranche supplies a working
private web interface and local warehouse release layer; it does not deploy a
service to the warehouse or finish the Amazon shipment lifecycle.

## Implemented flow

Import a validated replenishment request -> import reconciled shared-resource
counts -> record a measured resource recipe -> supervisor releases a work order
-> stock/cartons/cash/capacity are reserved in one transaction -> operator scans
physical cartons -> supervisor reviews and seals -> optionally link box labels
and stage an exact Amazon packing proposal using confirmed API identifiers.

The browser cannot approve or execute an Amazon/carrier API action. It never
purchases labels, edits listings, changes chemical classifications, or claims
Amazon receipt. The supervised v2 CLI remains the separate remote execution path.

### Work states

- DRAFT: planner output only, not a released warehouse instruction.
- RELEASED: named owner, deadline, review evidence and shared reservations exist.
- PACKING: one or more immutable physical scans have been captured.
- PACKED: the complete packout passed review. Not carrier acceptance or receipt.
- CANCELLED: untouched work cancelled; its holds returned exactly once.

There is deliberately no automatic shipped/received state or reservation expiry
recycling. Handoff, consumption settlement, receiving and shortage reconciliation
are the next lifecycle integration. Until those are implemented, the station is
suitable for a supervised pilot through sealed packout, NOT indefinite unattended
replenishment. Do not clear holds by editing SQLite or merely waiting for expiry.

## Run a synthetic demonstration

From a full repository checkout, Python 3.11+:

```sh
python -m pip install -e ".[warehouse,test]"
python -m diy_growth.fba_demo --dir /PRIVATE/PATH/fba-demo
```

The demo prints a newly generated password and the exact serve command. It creates
named demo-supervisor and demo-packer users, two synthetic work orders, measured
synthetic recipes and shared resources. It refuses to overwrite existing state.
The database is explicitly marked DEMO; Journal.execute refuses remote mutations
from that database even if real credentials and mutation flags are configured.
Do not load real operational data into a demonstration database.

The default station is at http://127.0.0.1:8765 on the same computer. The CLI binds
only to loopback. A phone/another warehouse computer needs an operator-configured
TLS reverse proxy or private tunnel; this build does not expose the process publicly.

## Private operator setup

Use a separate database and private user file for each Amazon seller account. Do
not put either in GitHub, the web root, or the source directory.

```sh
python -m diy_growth.fba_station --db /PRIVATE/fba.sqlite3 user \
  --users /PRIVATE/users.json --name supervisor-name --role supervisor
python -m diy_growth.fba_station --db /PRIVATE/fba.sqlite3 user \
  --users /PRIVATE/users.json --name packer-name --role packer
python -m diy_growth.fba_station --db /PRIVATE/fba.sqlite3 serve \
  --users /PRIVATE/users.json
```

Passwords are prompted without echo and stored as salted scrypt hashes, never as
plaintext. Roles are supervisor, packer, viewer. Browser permissions are checked on
the server, not just by hiding buttons. Packer/viewer API responses omit cash,
full economics, gate assertions and supervisor-only allocation information.

Opaque sessions expire after four hours. Session tokens are hashed in SQLite;
logout deletes the server session, and changing a password or role invalidates
previous sessions. Login is rate limited in persistent SQLite by username and
source address. Mutation requests require an exact Origin and a CSRF token.
Cookies are HttpOnly/SameSite=Strict and Secure for a configured HTTPS origin.
Host validation, a restrictive CSP, request-body limits and no-store headers are
applied. This is not a third-party security audit or SSO integration.

The serve command disables proxy-header trust. For a TLS proxy, preserve the exact
Host and configure --origin https://YOUR-PRIVATE-HOST without a trailing slash.
Keep the application bound to loopback behind that proxy. Do not run the service
as root. Linux files are created mode 0600; use a 0700 private directory. On Windows,
set NTFS ACLs for the service account because POSIX mode bits are not a substitute.
Protect backups too: they contain operating data and active session records.

## Import contracts

The v1 request JSON remains authoritative for pack_spec, snapshot, policy and gates.
No measurements, chemical approvals, SKU mappings or inventory are inferred here.

```sh
python -m diy_growth.fba_station --db /PRIVATE/fba.sqlite3 request /PRIVATE/request.json --actor supervisor-name
python -m diy_growth.fba_station --db /PRIVATE/fba.sqlite3 count /PRIVATE/count.json --event-id UNIQUE-COUNT-ID --actor supervisor-name
python -m diy_growth.fba_station --db /PRIVATE/fba.sqlite3 recipe /PRIVATE/recipe.json --work-key EXACT-WORK-KEY --actor supervisor-name
```

CLI actor fields are trusted local-operator assertions. Restrict OS access; they do
not independently prove the user's identity. Browser actors come from the session.

### Shared count JSON (synthetic example)

```json
{
  "resource_id": "EXAMPLE-CANONICAL-STOCK",
  "kind": "stock",
  "unit": "sellable_unit",
  "balance_including_local_holds": 120,
  "expected_revision": 0,
  "observed_at": "EXACT_TIME_WITH_TIMEZONE",
  "valid_until": "EXPLICIT_EVIDENCE_EXPIRY",
  "evidence_ref": "PRIVATE_RECONCILED_COUNT_RECORD",
  "includes_local_holds": true
}
```

The balance is the total resource allocated to this control system INCLUDING its
existing local holds, already reconciled against outside commitments. Do not send
a net-free number: the engine subtracts local holds exactly once. Out-of-band
warehouse/ERP orders are not automatically ingested. If those commitments are not
reconciled, this ledger cannot guarantee facility-wide availability.

Updates require the current expected_revision, monotonically nondecreasing count
timestamp and a unique event ID. Exact event retries do not restore an old count.
A shortage is recorded even if the count falls below existing reservations; it
creates negative availability and blocks new releases/dispatch rather than hiding
shrinkage. No count update automatically cancels existing warehouse work.

Resource kinds: stock, material, carton, packaging, cash, capacity. Quantities are
integer ticks in an explicit unit. Cash is USD_cent. Use a date/shift-qualified
resource key for capacity and a period-qualified key for budgets. Raw-material
units and conversions must come from a measured recipe, not a product-name guess.

### Resource recipes

A recipe has recipe_id, marketplace_id, msku, pack_version, observed_at,
valid_until, evidence_ref, and lines. Every line has resource_id, unit, per_unit,
per_carton, fixed. Required amount = per_unit * planned_units + per_carton * cartons
+ fixed. Coefficients are nonnegative whole ticks; each resulting amount is positive.

A recipe must include stock OR material, cartons, cash and capacity. Finished-stock
lines reserve one sellable_unit per unit. Carton allocation must cover every carton.
Cash allocation cannot fall below the planner's estimated cash. Additional bottle,
cap, bag or absorbent resources can be included as packaging lines. The recipe
review is responsible for completeness and correct physical mapping.

Recipes are immutable and tied to the exact marketplace/SKU/pack revision. Different
Amazon SKUs sharing a physical stock/material/budget resource must use the SAME
canonical resource ID. Inventing separate pools would defeat shared reservations.

Release is all-or-nothing under BEGIN IMMEDIATE, uses the exact reviewed plan hash,
rechecks source freshness and resource availability, and records allocations,
owner, reviewer and deadline. A second open work order for the same marketplace/SKU
is blocked until the prior work is properly reconciled. No expiry frees resources.

## Packout, corrections and label linkage

The operator scans a unique carton barcode and FNSKU, records its lot/handling-unit
reference, unit count, actual gross weight, actual L/W/H and measurement evidence.
Expected sellable-unit dimensions remain distinct from shipping-carton dimensions.
An Enter-terminated keyboard scanner advances across fields; no camera, Bluetooth,
USB scale driver or direct LotProof event ingestion is implemented in this tranche.

Exact duplicate scans return the original record. Different measurements under an
existing barcode are blocked. A supervisor can void a mistaken, unlinked carton
record with evidence; the original record and void event remain. Recapture uses a
new barcode. Linked cartons and packouts with staged packing/shipping actions are
frozen until shipment reconciliation. Partial/mixed cartons remain unsupported.

Sealing validates the complete physical packout. Linking labels requires the actual
Amazon box ID and INDIVIDUAL carrier tracking number for each carton. Duplicate
assignments are blocked; matching retry preserves the original linkage timestamp.
The UI displays saved label linkages; this is not a live carrier tracking status.

A sealed work order can prepare a setPackingInformation proposal from scans and an
explicit confirmed inboundPlanId/packingGroupId. This stages only. v2 hash approval,
account binding, operation allowlist and mutation enablement still apply afterward.
Journal.execute now checks the warehouse release and shared holds inside the same
transaction that claims dispatch. Expired/short resources stop dispatch.

## Compatibility and migration

v2 CLI internal commands now use Warehouse. Journal.execute itself also requires
a released work order; old databases without release records cannot dispatch.
Legacy scans/actions created before release need explicit reconciliation. A release
must precede staging new remote actions; the station will not silently assume an
already-created remote shipment is duplicate-free. Source/gate renewal for expired
in-progress work remains unimplemented; do not extend approvals without evidence.

The original v2 tests now construct valid releases and seal their complete scans
where required. Their assertions remain. All synthetic FBA tests pass locally.
Full-repository CI is recorded in the PR; direct browser HTTP navigation was blocked
by the test environment, so browser layout/interaction was exercised with an offline
DOM and an in-process ASGI TestClient bridge, without changing browser restrictions.
That is not a deployed-network or connected-account acceptance test.

### Backup

```sh
python -m diy_growth.fba_station --db /PRIVATE/fba.sqlite3 backup --out /PRIVATE/BACKUPS/fba-UNIQUE.sqlite3
```

Uses SQLite's online backup API and refuses output overwrite. Preserve the user
file separately. Test restoration before a pilot; no unattended backup scheduler
or cloud runtime was provisioned by this build.

## Still open in the overall FBA mission

Private installed runtime and actual operator onboarding; live stock/ERP and LotProof
integration; Amazon conversions/dimension writers; full packing/placement/transport
options and verification; automated box-ID assignment; carrier label/hazmat/LTL
workflows; consumption settlement and work closure; Amazon receiving/shortage
reconciliation; operational feeds and unattended execution. The existing scheduled
correspondence watch is unchanged and is not a substitute for these operational feeds.

Primary technical references checked during implementation:
- https://starlette.dev/middleware/
- https://flask.palletsprojects.com/en/stable/web-security/ (general browser controls)
- https://docs.python.org/3/library/sqlite3.html
