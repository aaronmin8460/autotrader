"""Crypto new-information & execution feasibility study (research only).

Read-only probes against public market-data endpoints, plus validation of
small downloaded samples. Nothing here places, cancels, or replaces an order,
touches a trading endpoint, or imports any execution/risk/runtime module.

Scripts:

``probe_dump_coverage``     enumerate the public futures-data dump catalog and
                            pin earliest-available dates per dataset class.
``validate_funding_samples`` schema/timestamp/gap validation of downloaded
                            funding, premium-index, and metrics samples, plus
                            a causal ``merge_asof`` join demo onto the 15m
                            decision grid.
``probe_venue_spread``      tick-median BBO spread on the execution venue's
                            own historical L1 quotes, sampled across eras.
``crossvenue_funding_check`` settled-funding agreement between the deep
                            historical source and the accessible live source
                            over their overlap window.

All outputs land in
``$AUTOTRADER_QA/reports/crypto-new-information-feasibility/``.
"""
