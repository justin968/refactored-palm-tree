"""Offline FBA planning and evidence gates. No Amazon/carrier/network writes.

All identifiers, physical measurements and approvals are supplied evidence;
this module cannot establish that those assertions are true in the warehouse.
Run: python -m diy_growth.fba request.json [--db private-state.sqlite3]
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


class Blocked(ValueError):
    """Missing, conflicting, expired, or unsafe planning inputs."""


def text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Blocked(f"{name}: nonempty text required")
    return value


def number(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise Blocked(f"{name}: numeric value required")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Blocked(f"{name}: invalid number") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise Blocked(f"{name}: finite {'positive' if positive else 'nonnegative'} number required")
    return result


def integer(value: Any, name: str, *, positive: bool = False) -> int:
    result = number(value, name, positive=positive)
    if result != result.to_integral_value():
        raise Blocked(f"{name}: whole units required")
    return int(result)


def instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(text(value, "timestamp").replace("Z", "+00:00"))
    except ValueError:
        raise Blocked("timestamp: ISO-8601 with timezone required") from None
    if result.tzinfo is None:
        raise Blocked("timestamp: timezone required")
    return result.astimezone(timezone.utc)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def dimensions(values: Any, name: str) -> tuple[Decimal, ...]:
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise Blocked(f"{name}: three measured dimensions in inches required")
    return tuple(number(v, name, positive=True) for v in values)


@dataclass(frozen=True)
class PackSpec:
    marketplace_id: str
    msku: str
    asin: str
    fnsku: str
    version: str
    unit_dimensions_in: list[float]
    unit_gross_lb: float
    carton_dimensions_in: list[float]
    carton_gross_lb: float
    units_per_carton: int
    measured_at: str
    measured_by: str
    evidence_ref: str

    def validate(self, now: datetime) -> None:
        for key in ("marketplace_id", "msku", "asin", "fnsku", "version", "measured_by", "evidence_ref"):
            text(getattr(self, key), key)
        unit = dimensions(self.unit_dimensions_in, "unit_dimensions_in")
        carton = dimensions(self.carton_dimensions_in, "carton_dimensions_in")
        qty = integer(self.units_per_carton, "units_per_carton", positive=True)
        unit_lb = number(self.unit_gross_lb, "unit_gross_lb", positive=True)
        carton_lb = number(self.carton_gross_lb, "carton_gross_lb", positive=True)
        if carton_lb < unit_lb * qty:
            raise Blocked("carton gross weight is below contained units' gross weight")
        if any(u > c for u, c in zip(sorted(unit), sorted(carton))):
            raise Blocked("unit cannot fit measured carton")
        if instant(self.measured_at) > now:
            raise Blocked("measurement timestamp is in the future")


def require_gates(gates: dict, spec: PackSpec, now: datetime) -> None:
    # These must come from actual authoritative reads/reviews, not inferred names.
    for name in ("identity", "fba_offer", "inbound_eligibility", "dangerous_goods", "packout", "replenishment_policy"):
        gate = gates.get(name, {})
        if gate.get("approved") is not True:
            raise Blocked(f"{name}: verified approval required")
        for key, expected in (("msku", spec.msku), ("asin", spec.asin), ("marketplace_id", spec.marketplace_id), ("pack_version", spec.version)):
            if gate.get(key) != expected:
                raise Blocked(f"{name}: approval identity/version mismatch")
        text(gate.get("evidence_ref"), f"{name}.evidence_ref")
        if not instant(gate.get("observed_at")) <= now < instant(gate.get("valid_until")):
            raise Blocked(f"{name}: expired or future approval")


def plan_replenishment(spec: PackSpec, snapshot: dict, policy: dict, gates: dict, now: datetime) -> dict:
    spec.validate(now)
    require_gates(gates, spec, now)
    for key in ("msku", "marketplace_id"):
        if snapshot.get(key) != getattr(spec, key):
            raise Blocked(f"snapshot {key} mismatch")
    text(snapshot.get("evidence_ref"), "snapshot.evidence_ref")
    max_age = number(policy.get("max_snapshot_age_hours"), "max_snapshot_age_hours", positive=True)
    age = Decimal(str((now - instant(snapshot.get("observed_at"))).total_seconds())) / 3600
    if age < 0 or age > max_age:
        raise Blocked("inventory/demand snapshot is stale or future-dated")
    if snapshot.get("positions_reconciled") is not True:
        raise Blocked("inbound/local positions must be disjoint and reconciled")
    demand = number(snapshot.get("daily_units"), "daily_units")
    horizon = sum(number(policy.get(k), k) for k in ("lead_days", "review_days", "safety_days"))
    target = int((demand * horizon).to_integral_value(rounding=ROUND_CEILING))
    position = sum(integer(snapshot.get(k), k) for k in ("fba_fulfillable", "on_time_inbound", "open_local_work"))
    needed = max(0, target - position)
    case = integer(spec.units_per_carton, "units_per_carton", positive=True)
    requested = ((needed + case - 1) // case) * case
    # Capacity and cash fields must already be NET of all existing commitments.
    unit_cost = number(snapshot.get("cash_cost_per_unit"), "cash_cost_per_unit", positive=True)
    cash_cap = int((number(snapshot.get("cash_remaining"), "cash_remaining") / unit_cost).to_integral_value(rounding=ROUND_FLOOR))
    cap = min(integer(snapshot.get("warehouse_unallocated"), "warehouse_unallocated"),
              integer(snapshot.get("capacity_remaining_after_commitments"), "capacity_remaining_after_commitments"),
              integer(policy.get("max_order_units"), "max_order_units"), cash_cap)
    units = min(requested, cap // case * case)
    return {"kind": "FBA_INTERNAL_WORK_ORDER_DRAFT", "execution_enabled": False,
            "marketplace_id": spec.marketplace_id, "msku": spec.msku,
            "planned_at": now.isoformat(), "target_units": target, "inventory_position": position,
            "needed_units": needed, "planned_units": units, "cartons": units // case,
            "full_cases_only": True, "constrained": units < requested,
            "estimated_cash": str(unit_cost * units), "pack_spec": asdict(spec),
            "snapshot": snapshot, "policy": policy, "gates": gates}


def validate_parcels(plan: dict, boxes: list[dict], now: datetime) -> dict:
    """One-SKU, full-case parcel check; LTL/BOL and mixed cartons are unsupported."""
    spec = PackSpec(**plan["pack_spec"])
    spec.validate(now)
    require_gates(plan["gates"], spec, now)
    expected = integer(plan["cartons"], "cartons", positive=True)
    if len(boxes) != expected or integer(plan["planned_units"], "planned_units") != expected * spec.units_per_carton:
        raise Blocked("carton/unit total mismatch")
    weight_tolerance = number(plan["policy"].get("weight_tolerance_lb"), "weight_tolerance_lb")
    dimension_tolerance = number(plan["policy"].get("dimension_tolerance_in"), "dimension_tolerance_in")
    seen = {k: set() for k in ("local_box_id", "amazon_box_id", "tracking_number")}
    for box in boxes:
        for key in seen:
            value = text(box.get(key), key)
            if value in seen[key]:
                raise Blocked(f"duplicate {key}")
            seen[key].add(value)
        for key in ("carrier", "lot_ref", "packed_by", "measurement_evidence_ref"):
            text(box.get(key), key)
        if not instant(plan["planned_at"]) <= instant(box.get("packed_at")) <= now:
            raise Blocked("packed timestamp outside plan/current interval")
        if box.get("msku") != spec.msku or box.get("fnsku") != spec.fnsku or box.get("pack_version") != spec.version:
            raise Blocked("box identity or pack version mismatch")
        if integer(box.get("units"), "units", positive=True) != spec.units_per_carton:
            raise Blocked("partial/overfilled carton requires a separate measured pack specification")
        actual_lb = number(box.get("gross_lb"), "gross_lb", positive=True)
        if actual_lb < number(spec.unit_gross_lb, "unit_gross_lb") * spec.units_per_carton:
            raise Blocked("actual carton weight below contained unit weight")
        if abs(actual_lb - number(spec.carton_gross_lb, "carton_gross_lb")) > weight_tolerance:
            raise Blocked("carton weight outside approved tolerance")
        actual = sorted(dimensions(box.get("dimensions_in"), "box.dimensions_in"))
        reference = sorted(dimensions(spec.carton_dimensions_in, "carton_dimensions_in"))
        if any(abs(a - b) > dimension_tolerance for a, b in zip(actual, reference)):
            raise Blocked("carton dimensions outside approved tolerance")
    return {"status": "PARCEL_MANIFEST_VALIDATED", "boxes": len(boxes),
            "carrier_acceptance_verified": False, "amazon_receipt_verified": False,
            "execution_enabled": False}


class DraftStore:
    """Local immutable pack revisions and idempotent draft reservations, not an ERP."""
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=15)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS fba_pack_versions (
          marketplace TEXT, sku TEXT, version TEXT, digest TEXT NOT NULL, body TEXT NOT NULL,
          PRIMARY KEY (marketplace, sku, version));
        CREATE TABLE IF NOT EXISTS fba_work_drafts (
          request_key TEXT PRIMARY KEY, marketplace TEXT NOT NULL, sku TEXT NOT NULL,
          digest TEXT NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS fba_one_active_sku
          ON fba_work_drafts (marketplace, sku) WHERE state = 'DRAFT';
        ''')

    def close(self) -> None:
        self.db.close()

    def save(self, request_key: str, plan: dict, now: datetime) -> dict:
        text(request_key, "request_key")
        integer(plan.get("planned_units"), "planned_units", positive=True)
        spec = PackSpec(**plan["pack_spec"])
        checked = plan_replenishment(spec, plan["snapshot"], plan["policy"], plan["gates"], now)
        # The time of a retry may change; all substantive decisions must still match.
        checked["planned_at"] = plan["planned_at"]
        if canonical(checked) != canonical(plan):
            raise Blocked("plan changed or did not originate from the planner")
        payload = canonical(plan)
        fingerprint = digest({k: v for k, v in plan.items() if k != "planned_at"})
        pack = asdict(spec)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute("SELECT digest, body FROM fba_work_drafts WHERE request_key=?", (request_key,)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise Blocked("request key reused with different content")
                self.db.execute("COMMIT")
                return {"created": False, "request_key": request_key, "plan": json.loads(prior[1])}
            key = (spec.marketplace_id, spec.msku, spec.version)
            previous_pack = self.db.execute("SELECT digest FROM fba_pack_versions WHERE marketplace=? AND sku=? AND version=?", key).fetchone()
            if previous_pack and previous_pack[0] != digest(pack):
                raise Blocked("pack version is immutable; create a new reviewed version")
            self.db.execute("INSERT OR IGNORE INTO fba_pack_versions VALUES (?,?,?,?,?)", (*key, digest(pack), canonical(pack)))
            self.db.execute("INSERT INTO fba_work_drafts VALUES (?,?,?,?,?,?)", (request_key, spec.marketplace_id, spec.msku, fingerprint, "DRAFT", payload))
            self.db.execute("COMMIT")
            return {"created": True, "request_key": request_key, "plan": plan}
        except sqlite3.IntegrityError:
            self.db.execute("ROLLBACK")
            raise Blocked("active draft already exists for this marketplace/SKU; reconcile before replacement") from None
        except Exception:
            self.db.execute("ROLLBACK")
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("--db", help="Optional private SQLite path; reserve a draft only")
    args = parser.parse_args()
    try:
        data = json.loads(args.request.read_text())
        now = datetime.now(timezone.utc)
        plan = plan_replenishment(PackSpec(**data["pack_spec"]), data["snapshot"], data["policy"], data["gates"], now)
        if args.db and plan["planned_units"]:
            store = DraftStore(args.db)
            try:
                plan = store.save(data["request_key"], plan, now)
            finally:
                store.close()
        print(json.dumps(plan, indent=2, allow_nan=False))
    except (Blocked, KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc), "execution_enabled": False}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
