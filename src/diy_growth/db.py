from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Iterable
from .models import Opportunity

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS canonical_products (sku TEXT PRIMARY KEY, woo_variation_id TEXT, merchant_offer_id TEXT, google_product_id TEXT, amazon_seller_sku TEXT, asin TEXT, mapping_status TEXT NOT NULL DEFAULT 'CURRENT', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS sourced_values (id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, metric TEXT NOT NULL, value REAL, source TEXT NOT NULL, observed_at TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1.0, status TEXT NOT NULL DEFAULT 'CURRENT', estimated INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS opportunities (id TEXT PRIMARY KEY, entity TEXT NOT NULL, channel TEXT NOT NULL, opportunity_type TEXT NOT NULL, observed_problem TEXT NOT NULL, evidence_json TEXT NOT NULL, inferred_cause TEXT NOT NULL, recommended_action TEXT NOT NULL, expected_incremental_revenue REAL, expected_incremental_contribution REAL, confidence REAL NOT NULL, implementation_cost REAL NOT NULL DEFAULT 0, risk TEXT NOT NULL, time_to_effect_days INTEGER, reversibility TEXT NOT NULL, action_class TEXT NOT NULL, dependencies_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'OPEN', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, opportunity_id TEXT, entity TEXT NOT NULL, channel TEXT NOT NULL, action_type TEXT NOT NULL, action_class TEXT NOT NULL, previous_state_json TEXT NOT NULL, proposed_state_json TEXT NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL, expected_effect_json TEXT NOT NULL, verification_json TEXT, rollback_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PROPOSED', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, executed_at TEXT, FOREIGN KEY(opportunity_id) REFERENCES opportunities(id));
CREATE TABLE IF NOT EXISTS experiments (id TEXT PRIMARY KEY, hypothesis TEXT NOT NULL, affected_entity TEXT NOT NULL, channel TEXT NOT NULL, baseline_start TEXT, baseline_end TEXT, change_json TEXT NOT NULL, implementation_timestamp TEXT, expected_outcome_json TEXT NOT NULL, measurement_start TEXT, measurement_end TEXT, actual_outcome_json TEXT, confounders_json TEXT NOT NULL DEFAULT '[]', decision TEXT, learning TEXT, status TEXT NOT NULL DEFAULT 'PLANNED', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS listing_audits (id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT NOT NULL, channel TEXT NOT NULL, audit_json TEXT NOT NULL, commercial_priority REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS creative_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT NOT NULL, channel TEXT NOT NULL, asset_type TEXT NOT NULL, trigger TEXT NOT NULL, hypothesis TEXT NOT NULL, source_experiment_id TEXT, priority REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'QUEUED', filename TEXT, provenance_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS competitor_products (id INTEGER PRIMARY KEY AUTOINCREMENT, our_sku TEXT NOT NULL, competitor TEXT NOT NULL, competitor_product_ref TEXT NOT NULL, match_quality TEXT NOT NULL, snapshot_json TEXT NOT NULL, observed_at TEXT NOT NULL, UNIQUE(our_sku, competitor, competitor_product_ref, observed_at));
"""

def connect(path: str | Path = "growth.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn

def save_opportunities(conn: sqlite3.Connection, opportunities: Iterable[Opportunity]) -> None:
    for o in opportunities:
        conn.execute("""INSERT OR REPLACE INTO opportunities (id, entity, channel, opportunity_type, observed_problem, evidence_json, inferred_cause, recommended_action, expected_incremental_revenue, expected_incremental_contribution, confidence, implementation_cost, risk, time_to_effect_days, reversibility, action_class, dependencies_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (o.id, o.entity, o.channel.value, o.opportunity_type, o.observed_problem, json.dumps(o.evidence), o.inferred_cause, o.recommended_action, o.expected_incremental_revenue, o.expected_incremental_contribution, o.confidence, o.implementation_cost, o.risk, o.time_to_effect_days, o.reversibility, o.action_class.value, json.dumps(o.dependencies)))
    conn.commit()
