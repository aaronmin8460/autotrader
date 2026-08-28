"""Read-only operations harness for the Combined Paper Smoke.

**This package cannot place an order.** Not behind a flag, not behind an
environment variable, not behind a confirmation token - the code that would do
it is not imported and the call sites do not exist. There is no `--execute`, no
`--yes`, and no `--auto-cleanup`, because there is nothing for them to switch
on.

What it does instead is remove the manual work from either side of a paper
smoke, so the two moments that genuinely need a human - placing one BUY and
placing one cleanup SELL - are the only moments that need one:

    autotrader-smoke preflight       may a smoke begin? + write the baseline
    YOU                              one paper BUY
    autotrader-smoke inspect-order   what did the broker actually do?
    autotrader-smoke cleanup-plan    how much to sell, from the real position
    YOU                              one cleanup SELL
    autotrader-smoke final-audit     is exposure back where it started?

`autotrader-smoke sequence` prints the full checklist including the
reconciliation passes.

Three rules run through every module here, and each is enforced by a test in
`tests/test_smoke_harness.py` rather than by this docstring:

**Read and repair stay apart.** Reconciliation may rewrite local state from
broker truth. That is right for reconciliation and wrong for an inspection, so
this harness reads the *latest persisted* run and prints the command when a
fresh pass is needed. It never starts one, and the database connection it uses
is opened `mode=ro` with `query_only` set, so it could not write if it tried.

**The broker's position is the only quantity a cleanup is sized from.** Not the
requested quantity, not the filled quantity, not the local `positions` table. A
crypto BUY of 0.00016705 BTC settles as a position of 0.000166632 BTC once the
taker fee comes out of the base asset, and a cleanup sized from the fill would
try to sell more than the account holds.

**An unanswerable question is never answered.** A lookup that times out reports
`ORDER_TRUTH_UNRESOLVED` and `DO NOT RETRY ORIGINAL ORDER`. It does not guess,
and it never suggests re-sending the original order.
"""

from autotrader.smoke.models import (
    BLOCKED,
    DO_NOT_RETRY_BANNER,
    EXPOSURE_NOT_RESTORED,
    EXPOSURE_RESTORED,
    ORDER_TRUTH_UNRESOLVED,
    READY_FOR_PAPER_SMOKE,
    SMOKE_COMPLETE,
    SMOKE_INCOMPLETE,
    USER_MUST_EXECUTE_BANNER,
    AuditReport,
    BaselineComparison,
    BrokerUnreadableError,
    CheckResult,
    CleanupPlan,
    CleanupVerdict,
    DashboardHealth,
    Freshness,
    GateReport,
    OrderReport,
    PositionSnapshot,
    RuntimeHealth,
    SmokeError,
    SmokeInputError,
    SmokeVerdict,
    StateUnreadableError,
)

__all__ = [
    "BLOCKED",
    "DO_NOT_RETRY_BANNER",
    "EXPOSURE_NOT_RESTORED",
    "EXPOSURE_RESTORED",
    "ORDER_TRUTH_UNRESOLVED",
    "READY_FOR_PAPER_SMOKE",
    "SMOKE_COMPLETE",
    "SMOKE_INCOMPLETE",
    "USER_MUST_EXECUTE_BANNER",
    "AuditReport",
    "BaselineComparison",
    "BrokerUnreadableError",
    "CheckResult",
    "CleanupPlan",
    "CleanupVerdict",
    "DashboardHealth",
    "Freshness",
    "GateReport",
    "OrderReport",
    "PositionSnapshot",
    "RuntimeHealth",
    "SmokeError",
    "SmokeInputError",
    "SmokeVerdict",
    "StateUnreadableError",
]
