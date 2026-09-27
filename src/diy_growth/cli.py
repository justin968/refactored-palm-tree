from __future__ import annotations
import argparse
import json
from datetime import date
from .db import connect, save_snapshot_rows
from .live import health as live_health, pull_supermetrics, pull_woo_products

def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]

def cmd_status(args):
    conn = connect(args.db)
    open_ops = conn.execute("SELECT COUNT(*) c FROM opportunities WHERE status='OPEN'").fetchone()["c"]
    approvals = conn.execute("SELECT COUNT(*) c FROM actions WHERE status='PROPOSED' AND action_class IN ('APPROVAL_REQUIRED','OWNER_DECISION')").fetchone()["c"]
    experiments = conn.execute("SELECT COUNT(*) c FROM experiments WHERE status IN ('PLANNED','RUNNING')").fetchone()["c"]
    queued_creative = conn.execute("SELECT COUNT(*) c FROM creative_queue WHERE status='QUEUED'").fetchone()["c"]
    latest_syncs = _rows(conn, "SELECT source, profile, status, row_count, finished_at, error FROM sync_runs ORDER BY id DESC LIMIT 10")
    print(json.dumps({"open_opportunities": open_ops, "approvals_needed": approvals, "active_experiments": experiments, "creative_queue": queued_creative, "integrations": [x.__dict__ for x in live_health()], "latest_syncs": latest_syncs}, indent=2))

def cmd_sync_status(args):
    conn = connect(args.db)
    print(json.dumps({"integrations":[x.__dict__ for x in live_health()], "recent_runs":_rows(conn, "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 25")}, indent=2))

def _start_run(conn, source, profile):
    cur = conn.execute("INSERT INTO sync_runs(source,profile) VALUES (?,?)", (source,profile))
    conn.commit()
    return cur.lastrowid

def _finish_run(conn, run_id, status, row_count=0, error=None):
    conn.execute("UPDATE sync_runs SET status=?, row_count=?, error=?, finished_at=CURRENT_TIMESTAMP WHERE id=?", (status,row_count,error,run_id))
    conn.commit()

def cmd_sync_run(args):
    conn = connect(args.db)
    profile = args.profile or args.source
    run_id = _start_run(conn, args.source, profile)
    try:
        if args.source == "woocommerce":
            rows = pull_woo_products()
            snapshot_date = date.today().isoformat()
            save_snapshot_rows(conn, "woocommerce", "products", snapshot_date, rows)
        else:
            if not args.start or not args.end:
                raise RuntimeError("--start and --end are required for Supermetrics profiles")
            rows = pull_supermetrics(args.source, args.start, args.end)
            save_snapshot_rows(conn, "supermetrics", args.source, args.end, rows)
        _finish_run(conn, run_id, "SUCCESS", len(rows))
        print(json.dumps({"status":"SUCCESS","source":args.source,"rows":len(rows)}, indent=2))
    except Exception as exc:
        _finish_run(conn, run_id, "FAILED", 0, str(exc))
        raise

def cmd_opportunities(args):
    conn = connect(args.db)
    print(json.dumps(_rows(conn, """SELECT id, entity, channel, opportunity_type, expected_incremental_contribution, confidence, risk, action_class, recommended_action FROM opportunities WHERE status='OPEN' ORDER BY COALESCE(expected_incremental_contribution,0)*confidence DESC LIMIT ?""", (args.limit,)), indent=2))

def cmd_optimize(args):
    conn = connect(args.db)
    where = "" if args.channel == "all" else "WHERE channel=?"
    params = () if args.channel == "all" else (args.channel,)
    rows = _rows(conn, f"""SELECT * FROM opportunities {where} ORDER BY COALESCE(expected_incremental_contribution,0)*confidence DESC LIMIT 50""", params)
    print(json.dumps({"mode":"READ_ONLY","channel":args.channel,"opportunities":rows}, indent=2))

def cmd_listing_audit(args):
    conn = connect(args.db); print(json.dumps(_rows(conn, "SELECT * FROM listing_audits ORDER BY commercial_priority DESC LIMIT ?", (args.limit,)), indent=2))

def cmd_competitors(args):
    conn = connect(args.db); print(json.dumps({"mode":"READ_ONLY","snapshots":_rows(conn, "SELECT * FROM competitor_products ORDER BY observed_at DESC LIMIT ?", (args.limit,))}, indent=2))

def cmd_creative(args):
    conn = connect(args.db); print(json.dumps(_rows(conn, "SELECT * FROM creative_queue WHERE status='QUEUED' ORDER BY priority DESC LIMIT ?", (args.count,)), indent=2))

def cmd_experiments(args):
    conn = connect(args.db); print(json.dumps(_rows(conn, "SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?", (args.limit,)), indent=2))

def cmd_scale(args):
    conn = connect(args.db); print(json.dumps({"mode":"READ_ONLY","scale_opportunities":_rows(conn, "SELECT * FROM opportunities WHERE opportunity_type IN ('BUDGET_SCALE','BID_SCALE','CROSS_CHANNEL_REALLOCATION') AND status='OPEN' ORDER BY COALESCE(expected_incremental_contribution,0)*confidence DESC LIMIT ?", (args.limit,))}, indent=2))

def cmd_init(args):
    connect(args.db).close(); print(f"Initialized {args.db}")

def main():
    p=argparse.ArgumentParser(prog="diy"); p.add_argument("--db",default="growth.db"); sub=p.add_subparsers(dest="command",required=True)
    g=sub.add_parser("growth"); gs=g.add_subparsers(dest="growth_command",required=True); s=gs.add_parser("status"); s.set_defaults(func=cmd_status)
    sy=sub.add_parser("sync"); sys=sy.add_subparsers(dest="sync_command",required=True); ss=sys.add_parser("status"); ss.set_defaults(func=cmd_sync_status); sr=sys.add_parser("run"); sr.add_argument("source",choices=["google_campaigns","ga4_products","amazon_seller_asin","amazon_orders","woocommerce"]); sr.add_argument("--profile"); sr.add_argument("--start"); sr.add_argument("--end"); sr.set_defaults(func=cmd_sync_run)
    o=sub.add_parser("opportunities"); o.add_argument("--limit",type=int,default=10); o.set_defaults(func=cmd_opportunities)
    ads=sub.add_parser("ads"); adss=ads.add_subparsers(dest="ads_command",required=True); ao=adss.add_parser("optimize"); ao.set_defaults(func=cmd_optimize,channel="all")
    for name,channel in [("amazon","amazon"),("google","google")]:
        c=sub.add_parser(name); cs=c.add_subparsers(dest=f"{name}_command",required=True); op=cs.add_parser("optimize"); op.set_defaults(func=cmd_optimize,channel=channel)
    l=sub.add_parser("listings"); ls=l.add_subparsers(dest="listings_command",required=True); la=ls.add_parser("audit"); la.add_argument("--limit",type=int,default=20); la.set_defaults(func=cmd_listing_audit)
    c=sub.add_parser("competitors"); cs=c.add_subparsers(dest="competitors_command",required=True); cr=cs.add_parser("refresh"); cr.add_argument("--limit",type=int,default=50); cr.set_defaults(func=cmd_competitors)
    c=sub.add_parser("creative"); cs=c.add_subparsers(dest="creative_command",required=True); cn=cs.add_parser("next"); cn.add_argument("count",type=int,nargs="?",default=20); cn.set_defaults(func=cmd_creative)
    e=sub.add_parser("experiments"); es=e.add_subparsers(dest="experiments_command",required=True); er=es.add_parser("review"); er.add_argument("--limit",type=int,default=20); er.set_defaults(func=cmd_experiments)
    s=sub.add_parser("scale"); ss=s.add_subparsers(dest="scale_command",required=True); so=ss.add_parser("opportunities"); so.add_argument("--limit",type=int,default=20); so.set_defaults(func=cmd_scale)
    i=sub.add_parser("init"); i.set_defaults(func=cmd_init)
    args=p.parse_args(); args.func(args)

if __name__=="__main__": main()
