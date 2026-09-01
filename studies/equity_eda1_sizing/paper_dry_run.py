"""Phase 5: the read-only dry run against the CURRENT paper broker state.

Connects to the paper broker (proving the environment twice, exactly as the
runtime does), reads the account, positions, open orders and asset metadata,
and computes what the new policy's target portfolio would be RIGHT NOW - the
proposed-order table, the projected exposures, and every activation gate the
migration predeclared. **Nothing is submitted and nothing is written**: no
store is opened for writing, no intent is created, and no broker mutation
exists in this module.

Run on the host, as the equity paper identity, with the runtime's own venv and
the new source tree on PYTHONPATH:

    python -m studies.equity_eda1_sizing.paper_dry_run
"""

from __future__ import annotations

import json
from decimal import Decimal

from autotrader.equity import EQUITY_SYMBOLS
from autotrader.equity.allocation import (
    POLICY_FRACTIONAL_RESERVED_90,
    allocation_policy_for,
    external_exposure_fraction_from,
    plan_allocation,
)
from autotrader.equity.paper import non_equity_exposure, require_paper_account
from autotrader.execution.equity import (
    create_market_data_client as create_equity_data_client,
)
from autotrader.execution.equity import (
    fetch_equity_asset,
    fetch_market_clock,
    fetch_open_paper_orders,
    fetch_reference_price,
)
from autotrader.execution.paper import (
    broker_symbol_key,
    create_paper_trading_client,
    fetch_paper_account_state,
    fetch_paper_positions,
)

_ZERO = Decimal(0)


def main() -> None:
    policy = allocation_policy_for(POLICY_FRACTIONAL_RESERVED_90)
    client = create_paper_trading_client()
    account_number = require_paper_account(client)

    account = fetch_paper_account_state(client)
    positions = fetch_paper_positions(client)
    open_orders = fetch_open_paper_orders(client)
    clock = fetch_market_clock(client)

    prices_client = create_equity_data_client()
    prices = {
        symbol: Decimal(str(fetch_reference_price(prices_client, symbol)))
        for symbol in EQUITY_SYMBOLS
    }
    fractionable = {
        symbol: fetch_equity_asset(client, symbol).fractionable for symbol in EQUITY_SYMBOLS
    }

    equity = Decimal(str(account.equity))
    external_value = Decimal(str(non_equity_exposure(positions)))
    external = external_exposure_fraction_from(
        account_equity=account.equity, non_equity_exposure=external_value
    )

    held = {
        symbol: positions[broker_symbol_key(symbol)].quantity
        if broker_symbol_key(symbol) in positions
        else _ZERO
        for symbol in EQUITY_SYMBOLS
    }
    market_values = {
        symbol: Decimal(str(positions[broker_symbol_key(symbol)].market_value))
        if broker_symbol_key(symbol) in positions
        else _ZERO
        for symbol in EQUITY_SYMBOLS
    }

    # The dry run assumes the live regime: all ten LONG is today's stance and
    # the widest case; the runtime itself will use the stored stance per bar.
    plan = plan_allocation(
        policy,
        active_symbols=EQUITY_SYMBOLS,
        account_equity=equity,
        external_exposure_fraction=external,
        reference_prices=prices,
        actual_quantities={s: q for s, q in held.items() if q > 0},
    )

    rows = []
    projected_equity_book = _ZERO
    max_weight = _ZERO
    for item in plan.allocations:
        current_value = market_values.get(item.symbol, _ZERO)
        target_value = item.target_quantity * item.reference_price
        projected_equity_book += target_value
        weight = target_value / equity
        max_weight = max(max_weight, weight)
        rows.append(
            {
                "symbol": item.symbol,
                "current_qty": str(item.actual_quantity),
                "current_value": str(current_value.quantize(Decimal("0.01"))),
                "current_pct": f"{(current_value / equity):.4%}",
                "target_value": str(target_value.quantize(Decimal("0.01"))),
                "target_pct": f"{weight:.4%}",
                "target_qty": str(item.target_quantity),
                "delta_qty": str(item.delta_quantity),
                "delta_notional": str(
                    (item.delta_quantity * item.reference_price).quantize(Decimal("0.01"))
                ),
                "side": item.side.value if item.side is not None else "NONE",
            }
        )

    current_equity_book = sum(market_values.values(), _ZERO)
    projected_gross = (projected_equity_book + external_value) / equity

    gates = {
        "projected_gross_at_or_under_target": projected_gross
        <= policy.budget_target + Decimal("0.0005"),
        "projected_gross_under_hard_cap": projected_gross < policy.total_cap,
        "every_symbol_under_hard_symbol_cap": max_weight <= policy.per_symbol_cap,
        "no_short": all(item.delta_quantity >= 0 for item in plan.allocations),
        "all_u10_fractionable": all(fractionable.values()),
        "no_open_orders": len(open_orders) == 0,
        "paper_environment_proven": account_number.startswith("PA"),
        "market_open": clock.is_open,
    }

    print(
        json.dumps(
            {
                "account_number": account_number,
                "policy": policy.to_json_dict(),
                "config_hash": policy.config_hash(),
                "account_equity": str(equity),
                "cash": str(Decimal(str(account.cash))),
                "current_equity_exposure": str(current_equity_book.quantize(Decimal("0.01"))),
                "current_equity_pct": f"{(current_equity_book / equity):.4%}",
                "current_non_equity_exposure": str(external_value),
                "current_non_equity_pct": f"{external:.4%}",
                "projected_equity_book": str(projected_equity_book.quantize(Decimal("0.01"))),
                "projected_final_equity_pct": f"{(projected_equity_book / equity):.4%}",
                "projected_final_account_gross_pct": f"{projected_gross:.4%}",
                "projected_cash_reserve_pct": f"{(1 - projected_gross):.4%}",
                "max_projected_symbol_weight": f"{max_weight:.4%}",
                "open_orders": len(open_orders),
                "fractionable": fractionable,
                "orders": rows,
                "buy_orders": sum(1 for row in rows if row["side"] == "BUY"),
                "sell_orders": sum(1 for row in rows if row["side"] == "SELL"),
                "gates": gates,
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
