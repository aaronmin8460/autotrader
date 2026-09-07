"""Real-money operational safety: the gates that stand between code and capital.

Nothing in this package constructs a broker client, submits an order, or moves
money. It holds the three things a real-money runtime needs that a paper one
does not, and it holds them apart from the execution boundary on purpose:

* `armstate` - the durable arm switch. Two states, DISARMED by default, and
  the default survives a restart because it is a row on disk rather than a
  variable in a process.
* `identity` - the account pin. A fingerprint of the account number this
  system is authorized to operate, checked against the account that actually
  answers, so a credential swap cannot silently retarget a live runtime.
* `budget` - the small-capital ceiling arithmetic, resolved against the
  balance the broker actually reports, plus the guard that stops a deposit
  from being read as a profitable day.

The separation is what makes each one auditable on its own. A reader who wants
to know "what stops this from trading" reads `armstate`; a reader who wants to
know "what stops this from trading *the wrong account*" reads `identity`.
Neither answer is tangled up in the fifteen other things an execution boundary
has to do.
"""

from autotrader.live.armstate import (
    ARM_CONFIRMATION_TOKEN,
    LIVE_ARMED_ENV,
    LIVE_ARMED_VALUE,
    STATE_ARMED,
    STATE_DISARMED,
    ArmState,
    LiveDisarmedError,
    arm_live_trading,
    create_arm_state_table,
    disarm_live_trading,
    environment_arm_gate_open,
    read_arm_state,
    require_armed,
)
from autotrader.live.budget import (
    CashFlowEvent,
    LiveBudgetError,
    LiveExposureCeilings,
    cash_flow_block_reason,
    effective_ceilings,
)
from autotrader.live.identity import (
    LIVE_ACCOUNT_FINGERPRINT_ENV,
    AccountIdentityError,
    account_fingerprint,
    configured_account_fingerprint,
    require_expected_account,
)

__all__ = [
    "ARM_CONFIRMATION_TOKEN",
    "LIVE_ACCOUNT_FINGERPRINT_ENV",
    "LIVE_ARMED_ENV",
    "LIVE_ARMED_VALUE",
    "STATE_ARMED",
    "STATE_DISARMED",
    "AccountIdentityError",
    "ArmState",
    "CashFlowEvent",
    "LiveBudgetError",
    "LiveExposureCeilings",
    "LiveDisarmedError",
    "account_fingerprint",
    "arm_live_trading",
    "cash_flow_block_reason",
    "configured_account_fingerprint",
    "create_arm_state_table",
    "disarm_live_trading",
    "effective_ceilings",
    "environment_arm_gate_open",
    "read_arm_state",
    "require_armed",
    "require_expected_account",
]
