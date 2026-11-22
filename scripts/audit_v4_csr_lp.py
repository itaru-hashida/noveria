"""Выгрузка расхождений CSR max-flow и exact lexicographic LP."""
from __future__ import annotations

import csv
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from time import perf_counter_ns

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from audit.scenario_runner import build_world
from modules.economy.v4.market import allocate_market_exact, _result_from_x
from scripts.experiment_v4_csr import max_flow_capacity
from scripts.experiment_v4_live_market_pass import prepare_market
from shadow.v4_market_adapter import build_v4_snapshot

SCALE = 1_000_000
TOL = 2.0 / SCALE
DIFF_TOL = 1e-7
OUT = ROOT / "audit" / "_runs" / "v4_csr_lp_audit_73129"


def edge_flow(snapshot, result):
    lookup = {(a.buyer_id, a.seller_id): a.quantity for a in result.allocations}
    return np.array([lookup.get((snapshot.buyer_ids[int(b)], snapshot.seller_ids[int(s)]), 0.0)
                     for b, s in zip(snapshot.pair_buyer, snapshot.pair_seller)])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        db, service = build_world(73129, directory, report_detail="none")
        try:
            engine = service.economy_state().engine
            buys, sells, _ = prepare_market(engine)
            snapshot, _ = build_v4_snapshot(engine, buys,
                (order for group in sells.values() for order in group))
            started = perf_counter_ns()
            exact = allocate_market_exact(snapshot)
            exact_ms = (perf_counter_ns() - started) / 1e6
            flow_result = max_flow_capacity(snapshot, scale=SCALE)
            csr = _result_from_x(snapshot, flow_result["flow"], 0, tolerance=1e-8)
        finally:
            db.close()

    csr_edges = flow_result["flow"]
    lp_edges = edge_flow(snapshot, exact)
    delta = csr.buyer_filled - exact.buyer_filled
    order = np.lexsort((snapshot.buyer_ids, -snapshot.buyer_age, -snapshot.buyer_limit))
    position = np.empty(len(order), dtype=int); position[order] = np.arange(len(order))
    seller_degree = np.bincount(snapshot.pair_seller, minlength=len(snapshot.seller_ids))
    csr_spend = np.bincount(snapshot.pair_buyer,
        weights=csr_edges * snapshot.pair_delivered_price, minlength=len(snapshot.buyer_ids))
    lp_spend = np.bincount(snapshot.pair_buyer,
        weights=lp_edges * snapshot.pair_delivered_price, minlength=len(snapshot.buyer_ids))

    def witness(bi):
        pos = position[bi]
        later = order[pos + 1:][delta[order[pos + 1:]] > DIFF_TOL]
        other = int(later[0]) if delta[bi] < -DIFF_TOL and len(later) else None
        if other is None: return None, ""
        if snapshot.buyer_limit[bi] != snapshot.buyer_limit[other]: reason = "price_limit"
        elif snapshot.buyer_age[bi] != snapshot.buyer_age[other]: reason = "unmet_age"
        else: reason = "ID_tiebreak"
        return reason, snapshot.buyer_ids[other]

    rows = []
    all_records = []
    for bi, buyer_id in enumerate(snapshot.buyer_ids):
        edges = np.flatnonzero(snapshot.pair_buyer == bi)
        available = [{"seller_id": snapshot.seller_ids[int(snapshot.pair_seller[e])],
                      "seller_quantity": float(snapshot.seller_quantity[snapshot.pair_seller[e]]),
                      "delivered_price": float(snapshot.pair_delivered_price[e])} for e in edges]
        shared = any(seller_degree[snapshot.pair_seller[e]] > 1 for e in edges)
        priority_reason, priority_witness = witness(bi)
        if abs(delta[bi]) <= TOL: cause = "rounding"
        elif csr_spend[bi] > snapshot.buyer_budget[bi] + TOL: cause = "budget_constraint"
        elif priority_reason: cause = "priority_" + priority_reason
        elif shared: cause = "common_stock_competition"
        else: cause = "alternative_feasible_distribution"
        record = {"buyer_id": buyer_id, "good": snapshot.good_ids[snapshot.buyer_good[bi]],
            "price_limit": float(snapshot.buyer_limit[bi]), "budget": float(snapshot.buyer_budget[bi]),
            "unmet_age": float(snapshot.buyer_age[bi]), "rank": int(position[bi]),
            "csr_quantity": float(csr.buyer_filled[bi]), "lp_quantity": float(exact.buyer_filled[bi]),
            "absolute_difference": float(abs(delta[bi])),
            "signed_difference_csr_minus_lp": float(delta[bi]), "csr_spending": float(csr_spend[bi]),
            "lp_spending": float(lp_spend[bi]), "shared_stock": shared,
            "priority_witness": priority_witness, "cause": cause,
            "available_sellers_json": json.dumps(available, ensure_ascii=False)}
        all_records.append(record)
        if abs(delta[bi]) > DIFF_TOL:
            rows.append(record)

    fields = list(rows[0])
    for name, selected in (("all_244_differences.csv", rows),
                           ("priority_violations.csv", [r for r in rows if r["cause"].startswith("priority_")])):
        with (OUT / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(selected)

    # Historical report bug: it used delta[index] instead of delta[order[index]].
    legacy_ids = []
    for index in range(len(order)):
        if delta[index] < -DIFF_TOL and np.any(delta[order[index + 1:]] > DIFF_TOL):
            legacy_ids.append(snapshot.buyer_ids[int(order[index])])
    with (OUT / "claimed_44_legacy_priority_rows.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        writer.writerows([r for r in all_records if r["buyer_id"] in legacy_ids])

    subsystems = []
    for row in [r for r in rows if r["cause"].startswith("priority_")][:10]:
        bi = snapshot.buyer_ids.index(row["buyer_id"])
        edge_indices = np.flatnonzero(snapshot.pair_buyer == bi)
        sellers = {int(snapshot.pair_seller[e]) for e in edge_indices}
        buyers = {int(snapshot.pair_buyer[e]) for e in range(len(snapshot.pair_buyer))
                  if int(snapshot.pair_seller[e]) in sellers}
        subsystems.append({"focus_buyer": row["buyer_id"], "cause": row["cause"],
            "buyers": [{"id": snapshot.buyer_ids[i], "limit": float(snapshot.buyer_limit[i]),
                         "budget": float(snapshot.buyer_budget[i]), "unmet_age": float(snapshot.buyer_age[i]),
                         "csr": float(csr.buyer_filled[i]), "lp": float(exact.buyer_filled[i])} for i in sorted(buyers)],
            "sellers": [{"id": snapshot.seller_ids[si], "quantity": float(snapshot.seller_quantity[si]),
                "edges": [{"buyer_id": snapshot.buyer_ids[int(snapshot.pair_buyer[e])],
                    "delivered_price": float(snapshot.pair_delivered_price[e]),
                    "csr_edge": float(csr_edges[e]), "lp_edge": float(lp_edges[e])}
                    for e in np.flatnonzero(snapshot.pair_seller == si)]} for si in sorted(sellers)]})
    (OUT / "subsystems.json").write_text(json.dumps(subsystems, ensure_ascii=False, indent=2), encoding="utf-8")

    excess = csr_spend - snapshot.buyer_budget
    summary = {"buyers": len(snapshot.buyer_ids), "sellers": len(snapshot.seller_ids),
        "edges": len(snapshot.pair_buyer), "csr_volume": float(csr.objective_volume),
        "lp_volume": float(exact.objective_volume), "exact_ms": exact_ms,
        "changed_buyers": len(rows), "priority_violations": sum(r["cause"].startswith("priority_") for r in rows),
        "legacy_buggy_priority_violations": len(legacy_ids),
        "raw_budget_violations_1e-7": int(np.count_nonzero(excess > 1e-7)),
        "max_raw_budget_excess": float(max(0.0, excess.max())),
        "sum_raw_budget_excess": float(excess[excess > 1e-7].sum()),
        "quantisation_only_changed": int(np.count_nonzero((abs(delta) > DIFF_TOL) & (abs(delta) <= TOL))),
        "cause_counts": dict(Counter(r["cause"] for r in rows))}
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2)); print(f"outputs={OUT}")


if __name__ == "__main__": main()
