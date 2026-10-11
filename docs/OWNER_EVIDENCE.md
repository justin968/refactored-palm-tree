# Existing evidence connection

`diy --db PRIVATE_DB growth evidence --manifest PRIVATE_MANIFEST --out PRIVATE_VALIDATION`
imports existing owner exports without changing channel production state. The manifest names
exact economics, seller history, Woo identities, Merchant mappings and optional matched economics,
raw SP reports, later source-backed cost scenarios and recovered seller reports.

Imports fingerprint every source file. Exact retries create no additional records. Source-cost
values are deduplicated across changed manifest versions without deleting prior evidence.
Exact SKU/ASIN joins only; incumbent mapping conflicts block historical contribution evaluation.
Woo costs remain separate. Missing spend or cost is unknown rather than zero. Later costs using
September shipping are separate sensitivity analyses, never promoted to July/August actual costs.

July classification is evaluated on August without future selection. Both months contain31days.
The calculation checks retrospective partial-contribution sign stability, not causal effects,
actual refunds-adjusted profit or autonomous action qualification. Policy/write flags remain unchanged.

## October11 hosted execution

The checkout was fast-forwarded to current main b694b544 while preserving the FBA additions.
Eleven source files were uploaded to private storage outside the application's public webroot.
The server imported the manifest and reproduced the local evaluation, then the authenticated HTTPS
endpoint returned the same result. Unauthenticated requests returned401; unknown routes returned404.
An exact repeat import left the server database byte-for-byte unchanged. The public homepage remained
unchanged during the verified deployment. Private exports, credentials and SQLite are not committed.

The hosted gateway is `https://lotproof.com/growth-engine/index.php` with a Bearer token held privately.
It proxies to a Python loopback service; PHP process-launch restrictions remain intact. The service
uses systemd user restart-on-failure, no-new-privileges and read-only filesystem protections.
Linger is enabled, but the unit is currently enabled-runtime: persistent boot registration still
needs the protected application-home `.config/systemd/user` directory created by hosting administration.
No scheduler was added. Hosted live Supermetrics/Woo ingestion is not verified.

Historical coverage remains59exactpairs:13estimated historical cost models,46missing;3evaluable
July/August pairs and56unknown. Four additional pairs have separately labeled later-cost sensitivities.
Fresh targeted Seller Central recovery confirmed July rows already retained and returned no August
rows for the five SKUs missing sales coverage. Report absence is still unknown, never zero.

Private deployment receipt, manifest, exact per-pair missing inputs and completed recovery queries
are in the original engineering workspace `2026-09-27/referenced-chatgpt-conversation-this-is-an/work`.
Future recovery requires changed exact-pair cost/report evidence, not repeated unchanged pulls.

All197repository tests passed on the hosted Linux runtime, including the two file-permission
checks that could not pass under Windows. Focused evidence and private HTTP tests also passed.
