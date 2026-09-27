# DIYChemicals Growth Control Plane

This repository is the durable operating system for DIYChemicals/Chemfulfill growth work.

## Durable rules

- Repository state, data, mappings, policies, action history, and experiment history are authoritative. Conversation memory is not.
- WooCommerce completed/refunded/cancelled orders are commercial truth for DIYChemicals.com. Advertising attribution is evidence, not financial truth.
- Amazon seller/order data is commercial truth for Amazon; Amazon Ads attribution is evidence, not financial truth.
- Never silently guess cross-channel mappings. Mark uncertain mappings unresolved or ambiguous.
- Optimize for expected incremental contribution, not platform ROAS alone.
- Separate observed facts, inferred causes, proposed actions, expected outcomes, and actual outcomes.
- Deterministic code owns calculations, joins, validation, thresholds, policy enforcement, and production writes.
- Model judgment may diagnose, prioritize, resolve ambiguity, and propose copy/creative.
- Production writes are disabled unless an explicit owner-authorized policy allows them.
- Every production write must preserve before state, after state, reason, evidence, expected effect, verification, and rollback data.
- Stale, estimated, conflicting, or missing inputs must reduce confidence or block material actions.
- Respect cooldowns after pricing, listing, creative, feed, or campaign changes.
- Prefer shared abstractions across Google and Amazon; channel adapters feed one canonical economics/opportunity/action/experiment model.

## Owner interface

Primary commands:

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
