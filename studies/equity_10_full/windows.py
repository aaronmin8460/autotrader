"""The full study's common scored region, its twelve windows, and the lookback.

**The scored region is the longest interval every symbol can serve with a full
warm-up.** All ten frames span 2021-01-04..2026-08-28. The measured worst-case
sliding-window lookback (`studies.equity_10_full.warmup`, run on the real
frames before these constants were frozen) is 4,552 base bars - GOOGL, at
2022-01-28 - so the region opens at the first session on which every symbol
has `LOOKBACK_BARS` of history behind it: **2021-09-30**. It closes at the
last downloaded session, 2026-08-28: 1,233 sessions.

**Twelve contiguous chronological windows, the last one a holdout.** The pilot
estimated ~12 walk-forward windows for the full study; the 1,233 sessions are
split into twelve nearly equal runs of 102-103 sessions (~5 months each). V4 is
re-trained at every window boundary on that window's past alone, so the model
serving any bar is at most ~5 months stale - the same cadence the pilot's
six-window plan implied. `HOLDOUT_WINDOW` (w12) is excluded from every
development result; the runner refuses to score it until the development
conclusion is recorded (see `run_study`).

**Window boundaries are dates, not row positions**, so the same window names
the same market days on every symbol even though missing bars shift row
positions between frames.
"""

from __future__ import annotations

from datetime import date

from studies.equity_v1_v5.windows import ScoringWindow

#: The base-bar lookback handed to every engine on every scored bar - for all
#: ten symbols and all five engines uniformly.
#:
#: Not the declared `required_base_bars` (2,834), and not the pilot's 3,000
#: either. Measured worst cases on the real frames: 2,885 for SPY/QQQ/AAPL/
#: MSFT/TSLA, 2,901-2,902 for IWM/NVDA, 2,898 for META - and **3,373 for AMZN**
#: and **4,552 for GOOGL**, whose missing-bar clusters in late 2021 destroy
#: consecutive 4-hour buckets (GOOGL is missing 285 bars over the frame,
#: 0.77%, the most in the universe). The pilot's 3,000 is therefore proven
#: insufficient on this universe, and the study's rule for that case is a
#: uniform increase: 4,750 clears the worst measured case by 4.3%. The study
#: still asserts zero INSUFFICIENT_* reasons on every scored bar rather than
#: trusting this number.
LOOKBACK_BARS = 4750

#: One whole regular session, the embargo between V4 training and scoring -
#: unchanged from the pilot and the horizon study.
EMBARGO_BARS = 26

#: The frozen V4 research horizon. 4 bars, per the horizon study's final
#: classification; the train->score gap is horizon + embargo = 30 bars.
HORIZON_BARS = 4

#: The full window list, oldest first. w12 is the final holdout.
FULL_WINDOWS: tuple[ScoringWindow, ...] = (
    ScoringWindow(
        name="w01",
        start=date(2021, 9, 30),
        end=date(2022, 2, 25),
        covers=(
            "late-2021 top and the first 2022 down-leg; DST fall-back; "
            "Thanksgiving and Christmas early closes"
        ),
    ),
    ScoringWindow(
        name="w02",
        start=date(2022, 2, 28),
        end=date(2022, 7, 26),
        covers=(
            "2022 bear market; DST spring-forward; Good Friday; AMZN 20:1 "
            "(2022-06-06) and GOOGL 20:1 (2022-07-18) splits"
        ),
    ),
    ScoringWindow(
        name="w03",
        start=date(2022, 7, 27),
        end=date(2022, 12, 19),
        covers=(
            "summer-2022 rally and autumn down-leg; TSLA 3:1 split "
            "(2022-08-25); DST fall-back; Thanksgiving early close"
        ),
    ),
    ScoringWindow(
        name="w04",
        start=date(2022, 12, 20),
        end=date(2023, 5, 18),
        covers=(
            "bear-market bottom and early-2023 recovery; Christmas/New "
            "Year; DST spring-forward; Good Friday"
        ),
    ),
    ScoringWindow(
        name="w05",
        start=date(2023, 5, 19),
        end=date(2023, 10, 16),
        covers=(
            "the 2023 large-cap rally; NVDA post-earnings gap regime; "
            "Juneteenth; 2023-07-03 early close"
        ),
    ),
    ScoringWindow(
        name="w06",
        start=date(2023, 10, 17),
        end=date(2024, 3, 13),
        covers=(
            "late-2023 correction and year-end rally; both early-close "
            "clusters; DST both directions"
        ),
    ),
    ScoringWindow(
        name="w07",
        start=date(2024, 3, 14),
        end=date(2024, 8, 9),
        covers=(
            "2024 spring/summer; NVDA 10:1 split (2024-06-10); the "
            "2024-08-05 volatility spike; Good Friday"
        ),
    ),
    ScoringWindow(
        name="w08",
        start=date(2024, 8, 12),
        end=date(2025, 1, 7),
        covers=(
            "autumn 2024 and year-end; the 2024-12-23 partial provider "
            "outage; two early closes; DST fall-back"
        ),
    ),
    ScoringWindow(
        name="w09",
        start=date(2025, 1, 8),
        end=date(2025, 6, 6),
        covers=(
            "the 2025 spring drawdown and April tariff sequence; the full "
            "2025-03-10 provider outage; DST spring-forward"
        ),
    ),
    ScoringWindow(
        name="w10",
        start=date(2025, 6, 9),
        end=date(2025, 11, 3),
        covers="mid-2025; Juneteenth; 2025-07-03 early close; DST fall-back",
    ),
    ScoringWindow(
        name="w11",
        start=date(2025, 11, 4),
        end=date(2026, 4, 1),
        covers="late-2025 and early-2026; both early-close clusters; DST spring-forward",
    ),
    ScoringWindow(
        name="w12",
        start=date(2026, 4, 2),
        end=date(2026, 8, 28),
        covers="FINAL HOLDOUT - the most recent five months, untouched during development",
    ),
)

#: The windows every development figure is computed from.
DEV_WINDOWS: tuple[ScoringWindow, ...] = FULL_WINDOWS[:11]

#: The untouched final chronological holdout.
HOLDOUT_WINDOW: ScoringWindow = FULL_WINDOWS[11]


class WindowError(Exception):
    """A window outside the frozen study set was requested."""


def window_by_name(name: str) -> ScoringWindow:
    """Look one frozen window up, refusing names outside the set."""
    for window in FULL_WINDOWS:
        if window.name == name:
            return window
    raise WindowError(f"{name!r} is not one of the frozen study windows.")


__all__ = [
    "DEV_WINDOWS",
    "EMBARGO_BARS",
    "FULL_WINDOWS",
    "HOLDOUT_WINDOW",
    "HORIZON_BARS",
    "LOOKBACK_BARS",
    "WindowError",
    "window_by_name",
]
