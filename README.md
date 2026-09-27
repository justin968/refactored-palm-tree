# DIY Growth Control Plane

Unified growth-control-plane scaffold for DIYChemicals.com and Amazon.

## v0 capabilities

- Canonical cross-channel product mapping schema
- Data provenance / freshness model
- Unit economics with break-even CAC and ROAS
- Shared opportunity model ranked by risk-adjusted expected contribution
- Action classes: AUTO_ELIGIBLE, APPROVAL_REQUIRED, OWNER_DECISION, INVESTIGATE
- Portfolio-level guardrails; production writes disabled by default
- Persistent SQLite ledgers for opportunities, actions, experiments, listing audits, creative work, and competitor snapshots
- Shared channel-adapter interface with Google and Amazon placeholders
- Owner CLI matching the intended operating language
- Basic economics and policy tests

## Install

1. Create a Python 3.11+ virtual environment.
2. Install with: pip install -e .
3. Initialize state with: diy init

## Owner commands

- diy growth status
- diy opportunities
- diy ads optimize
- diy amazon optimize
- diy google optimize
- diy listings audit
- diy competitors refresh
- diy creative next 20
- diy experiments review
- diy scale opportunities

All optimization commands are currently read-only.

## Architecture

Channel APIs -> canonical normalized state -> unit economics -> observations/diagnosis -> opportunities -> policy check -> approval/action -> verification -> experiment learning.

Google and Amazon are adapters, not separate optimization systems.

## Next integration work

1. Wire WooCommerce orders/refunds/cancellations and product/variation catalog.
2. Wire Google Ads + GA4 + Merchant Center using existing account credentials.
3. Wire Amazon Ads and Seller/SP-API.
4. Load actual COGS, packaging, fulfillment, shipping, marketplace/payment fees.
5. Build deterministic cross-channel mapping importer and unresolved-mapping review.
6. Add search-term/keyword/negative/bid/budget opportunity generators.
7. Add listing/PDP audit extractors and structured scoring evidence.
8. Add competitor collectors for Alliance Chemical, Lab Alley, Level 7, then discovery.
9. Add creative/copy candidate generation tied to measurable hypotheses.
10. Add historical replay/backtest before any autonomous-write policy can be enabled.

## Safety

config/policies.json ships with writes_enabled=false and auto_eligible_enabled=false. Implementing a write-capable adapter does not authorize execution.
