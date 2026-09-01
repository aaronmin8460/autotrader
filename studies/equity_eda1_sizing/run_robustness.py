"""The predeclared robustness attacks on the sizing policies (ledger §L5, 1-2, 7, 9-11).

Run strictly after `primary.json` exists. Nothing here changes a criterion or
adds a policy; it evaluates the criteria the primary stage could not.
"""

from __future__ import annotations

import json
import random
import time
from decimal import Decimal
from pathlib import Path

import pandas as pd

from autotrader.equity.allocation import (
    POLICY_IDS,
    AllocationPolicy,
    plan_allocation,
)
from autotrader.execution.paper import PaperAccountState, PaperPosition, build_risk_context
from autotrader.research.costs import EQUITY_COST
from autotrader.risk import RiskRequest, RiskSide, evaluate_risk
from studies.equity_eda1_sizing import STUDY_SYMBOLS, WINDOW_NAMES
from studies.equity_eda1_sizing.evidence import stance_frame, verify_wiring
from studies.equity_eda1_sizing.run_sizing import (
    price_frames,
    window_bounds,
    window_returns,
    write_json,
)
from studies.equity_eda1_sizing.simulate import RebalanceRule, simulate

#: The ten-symbol study's luckiest symbol, named in the deep-architecture
#: program's own leave-one-out attack. Dropping it is criterion 9.
STRONGEST_SYMBOL = "NVDA"


def stage_robustness(datasets: Path, decisions: Path, output: Path) -> None:
    started = time.perf_counter()
    eda1, v3, summary = stance_frame(datasets, decisions)
    verify_wiring(summary)
    index = pd.DatetimeIndex(eda1.index)
    frames = price_frames(datasets, index)
    bounds = window_bounds(decisions)
    buy_hold = pd.DataFrame(True, index=eda1.index, columns=list(STUDY_SYMBOLS)).astype("boolean")

    results: dict[str, object] = {"wiring_check": "PASS", "policies": {}}
    primary = json.loads((output / "primary.json").read_text())

    for policy_id in POLICY_IDS:
        policy = AllocationPolicy(policy_id=policy_id)
        entry: dict[str, object] = {}

        # --- criterion 1: tuple-order invariance, on the realized curve ------
        reference = simulate(
            label=f"{policy_id}/order/forward",
            stances=eda1,
            frames=frames,
            policy=policy,
            cost_model=EQUITY_COST,
            cost_label="equity-marketable",
            external_exposure_fraction=Decimal(0),
            symbols=STUDY_SYMBOLS,
        )
        rng = random.Random(20260831)
        shuffled = list(STUDY_SYMBOLS)
        rng.shuffle(shuffled)
        variants = {
            "reversed": tuple(reversed(STUDY_SYMBOLS)),
            "shuffled": tuple(shuffled),
        }
        order_checks: dict[str, object] = {"shuffled_order": list(shuffled)}
        for name, order in variants.items():
            other = simulate(
                label=f"{policy_id}/order/{name}",
                stances=eda1,
                frames=frames,
                policy=policy,
                cost_model=EQUITY_COST,
                cost_label="equity-marketable",
                external_exposure_fraction=Decimal(0),
                symbols=order,
            )
            order_checks[name] = {
                "equity_curve_identical": list(other.equity_curve) == list(reference.equity_curve),
                "net_return": other.net_return,
                "fill_count": other.fill_count,
            }
        entry["criterion_1_order_invariance"] = order_checks

        # --- criterion 2 and 7: symmetry and realized ceilings ---------------
        entry["criterion_2_symmetry"] = {"weight_asymmetry_bars": reference.weight_asymmetry_bars}
        entry["criterion_7_concentration"] = {
            "max_assigned_symbol_weight": str(reference.max_symbol_weight),
            "max_assigned_total_weight": str(reference.max_total_weight),
            "max_realized_symbol_fraction": reference.max_realized_symbol_fraction,
            "max_realized_total_fraction": reference.max_realized_total_fraction,
        }

        # --- criterion 9: leave the strongest symbol out ---------------------
        reduced = tuple(s for s in STUDY_SYMBOLS if s != STRONGEST_SYMBOL)
        loso: dict[str, object] = {"dropped": STRONGEST_SYMBOL}
        loso_windows: dict[str, dict[str, float]] = {}
        for name, stance in (("EDA1", eda1), ("V3", v3), ("BUY_AND_HOLD", buy_hold)):
            result = simulate(
                label=f"{policy_id}/loso/{name}",
                stances=stance,
                frames=frames,
                policy=policy,
                cost_model=EQUITY_COST,
                cost_label="equity-marketable",
                external_exposure_fraction=Decimal(0),
                symbols=reduced,
            )
            metrics = result.metrics()
            loso_windows[name] = window_returns(result, bounds)
            loso[name] = {
                "net_return": result.net_return,
                "sharpe_ratio": metrics.sharpe_ratio,
                "max_drawdown": metrics.max_drawdown,
            }
        from studies.equity_eda1_sizing.run_sizing import capture

        loso["capture"] = {
            "EDA1_vs_BH": capture(loso_windows["EDA1"], loso_windows["BUY_AND_HOLD"]),
            "V3_vs_BH": capture(loso_windows["V3"], loso_windows["BUY_AND_HOLD"]),
        }
        entry["criterion_9_strongest_symbol_removed"] = loso

        # --- criterion 10: drop the strongest window -------------------------
        # Computed from the primary stage's stored continuous-curve window
        # returns rather than re-scored: dropping a window is a question about
        # the compounded contribution of the windows that remain, and the curve
        # already answers it exactly.
        scenario = primary["policies"][policy_id]["scenarios"]["0.00"]
        eda1_windows = scenario["EDA1"]["equity-marketable"]["window_returns"]
        strongest = max(WINDOW_NAMES, key=lambda name: eda1_windows[name])
        compounded = 1.0
        for name in WINDOW_NAMES:
            if name == strongest:
                continue
            compounded *= 1.0 + eda1_windows[name]
        entry["criterion_10_strongest_window_removed"] = {
            "dropped": strongest,
            "dropped_window_return": eda1_windows[strongest],
            "net_return_without": compounded - 1.0,
        }

        # --- the L2 rebalancing-rule variant, reported not gated -------------
        weight_change = simulate(
            label=f"{policy_id}/rule/weight-change",
            stances=eda1,
            frames=frames,
            policy=policy,
            cost_model=EQUITY_COST,
            cost_label="equity-marketable",
            external_exposure_fraction=Decimal(0),
            rule=RebalanceRule.WEIGHT_CHANGE,
        )
        wc_metrics = weight_change.metrics()
        entry["rebalance_rule_sensitivity"] = {
            "whole_share": {
                "net_return": reference.net_return,
                "turnover": reference.metrics().turnover,
                "fill_count": reference.fill_count,
            },
            "weight_change": {
                "net_return": weight_change.net_return,
                "turnover": wc_metrics.turnover,
                "fill_count": weight_change.fill_count,
            },
        }
        results["policies"][policy_id] = entry  # type: ignore[index]

    results["criterion_11_risk_replay"] = risk_replay()
    results["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    write_json(output / "robustness.json", results)
    print(f"robustness: {results['elapsed_seconds']}s -> {output / 'robustness.json'}")


def risk_replay() -> dict[str, object]:
    """Criterion 11: does the real Risk Engine approve what the allocator asks for?

    The allocator's job is to ask for something Risk will grant. The previous
    design asked for a billion shares and used Risk as the sizing rule, and the
    signature of that was every symbol coming back clamped. So: build the live
    account state - the real broker figures from the pre-repair snapshot,
    including the crypto position - run every policy's ten targets through
    `evaluate_risk`, and require APPROVED on all of them.
    """
    equity = 99824.63
    prices = dict(
        zip(
            STUDY_SYMBOLS,
            (765.56, 714.25, 293.21, 314.13, 510.35, 219.65, 261.24, 338.51, 571.20, 366.47),
            strict=True,
        )
    )
    crypto_value = 4997.40
    account = PaperAccountState(
        equity=equity,
        cash=94827.23,
        status="ACTIVE",
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
    )
    external = Decimal(str(crypto_value)) / Decimal(str(equity))

    report: dict[str, object] = {
        "account_equity": equity,
        "external_exposure_fraction": str(external),
    }
    for policy_id in POLICY_IDS:
        policy = AllocationPolicy(policy_id=policy_id)
        plan = plan_allocation(
            policy,
            active_symbols=STUDY_SYMBOLS,
            account_equity=Decimal(str(equity)),
            external_exposure_fraction=external,
            reference_prices={s: Decimal(str(p)) for s, p in prices.items()},
            actual_quantities={},
        )
        # Positions accumulate as the plan is executed, so risk is evaluated
        # against the account each order would actually meet - which is where
        # the tuple-order failure showed up before.
        positions: dict[str, PaperPosition] = {
            "ETHUSD": PaperPosition(
                symbol="ETHUSD",
                quantity=Decimal("2.029253431"),
                market_value=crypto_value,
                average_entry_price=2462.0,
            )
        }
        rows: list[dict[str, object]] = []
        for item in plan.allocations:
            context = build_risk_context(
                account,
                positions,
                item.symbol,
                daily_baseline_equity=Decimal(str(equity)),
            )
            decision = evaluate_risk(
                RiskRequest(
                    symbol=item.symbol,
                    side=RiskSide.BUY,
                    reference_price=float(item.reference_price),
                    requested_quantity=item.delta_quantity,
                ),
                context,
            )
            rows.append(
                {
                    "symbol": item.symbol,
                    "requested": str(item.delta_quantity),
                    "approved": str(decision.approved_quantity),
                    "reason_code": decision.reason_code,
                    "fully_approved": decision.approved_quantity >= item.delta_quantity,
                }
            )
            filled = min(decision.approved_quantity, item.delta_quantity)
            if filled > 0:
                positions[item.symbol] = PaperPosition(
                    symbol=item.symbol,
                    quantity=filled,
                    market_value=float(filled * item.reference_price),
                    average_entry_price=float(item.reference_price),
                )
        report[policy_id] = {
            "targets": rows,
            "all_approved": all(row["reason_code"] == "APPROVED" for row in rows),
            "all_fully_funded": all(row["fully_approved"] for row in rows),
            "funded_symbols": sum(1 for row in rows if Decimal(str(row["approved"])) > 0),
        }
    return report


__all__ = ["STRONGEST_SYMBOL", "risk_replay", "stage_robustness"]
