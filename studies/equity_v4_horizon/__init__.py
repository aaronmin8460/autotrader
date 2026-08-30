"""Equity V4 label/horizon study: is the 4-bar horizon why V4 keeps choosing the null?

The SPY/QQQ historical pilot validated the equity research pipeline end to end
and found that V4 selected its class-frequency baseline in 11 of 12
window-models. The single exception rested on two bars of suspicious isotonic
confidence. The pilot's own recommendation was to test the label horizon before
paying for the ten-symbol evaluation, because four 15-minute bars - one trading
hour - is short enough that the label may be mostly microstructure noise.

This study answers exactly that question and nothing else. Four predeclared
horizons (4, 8, 16, 26 base bars), the CURRENT V4 label semantics at every one
of them, the CURRENT selection gate, and a winner rule frozen in
``design.md`` under the study's output directory before the first model was
trained. A negative result - no horizon helps - is a valid outcome with its own
classification, not a reason to search further.

Everything here is research-only. Nothing is deployed, nothing touches a
broker, and production V4 semantics are unchanged: the alternative horizons
exist as ``LabelSpec`` values inside this study alone.

The pilot harness (``studies.equity_v1_v5``) is reused verbatim - its session
calendar snapshot, scoring windows, adapters and scoring machinery were
validated by the pilot and copied unchanged from research SHA ``c98ca36``.
"""
