# Growth Control Plane Progress

Updated: 2026-09-27

## DONE

- Shared Google + Amazon canonical architecture
- SQLite state / action / experiment / creative / competitor ledgers
- Unit economics model and policy engine
- Secret-safe environment configuration
- Supermetrics API client
- WooCommerce REST client
- Persisted sync runs and raw snapshots
- Owner sync commands
- Google Ads campaign query profile — validated against live DIY Chemicals account
- Google Ads search-term query profile — validated live
- Google Ads Shopping product query profile — validated live
- GA4 item-commerce query profile — validated live
- Amazon Seller order query profile — validated live
- Amazon Ads campaign/search-term profiles defined
- Amazon Seller ASIN performance profile — validated live
- Search-term waste detector
- Campaign scale-candidate detector
- Opportunity refresh from latest snapshots
- Live writes disabled by default

## IN PROGRESS

- Validate slow Amazon Ads campaign/search-term queries end-to-end
- CI on live-connectors-v1 branch — green
- Expand opportunity generators beyond waste/scale

## BLOCKED / INPUT REQUIRED

### Runtime authentication
ChatGPT/Supermetrics connector authentication cannot be silently exported into the repository runtime.
The deployed control-plane process still needs its own SUPERMETRICS_API_KEY and account IDs in environment variables.

### WooCommerce runtime access
WPVibe is connected read-only inside ChatGPT, but the standalone repository process needs WooCommerce REST credentials (consumer key/secret) or a dedicated service bridge.

### True SKU economics
Need authoritative per-SKU COGS, packaging, fulfillment, shipping subsidy/cost, marketplace fees, payment fees and refund allowance before cross-channel capital allocation can claim contribution-profit impact.

### Amazon write path
Supermetrics currently provides the live Amazon reporting source used here, but production Amazon Ads mutation support is not yet wired into the repo. Keep Amazon recommendations approval-only until a supported write API/credential path is installed.

### Remaining engines
- canonical Woo ↔ Merchant ↔ Google ↔ Amazon mapping importer/reviewer
- keyword expansion and bid optimization beyond search-term waste
- listing/PDP structured audits
- competitor collectors for Alliance Chemical, Lab Alley, Level 7 and discovered competitors
- versioned copy experiments
- creative generation/experiment orchestration
- cross-channel marginal capital allocator
- historical backtest on real data
- approval queue execution + verification + rollback
- scheduler/hosting for unattended daily runs

## NEXT GATE

The next useful milestone is a read-only live daily operator:

1. sync Google Ads, GA4, Amazon Ads, Amazon Seller, WooCommerce;
2. reconcile identifiers and commercial truth;
3. generate ranked opportunities;
4. persist experiments/actions;
5. produce one owner snapshot.

Only after backtesting that layer should autonomous writes be enabled.
