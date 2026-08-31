"""Crypto maker-execution feasibility pilot (research only).

Measures the effective round-trip friction a passive limit-order policy
could achieve on the venue's own historical tick record, for BTC/USD and
ETH/USD, without contacting any order endpoint. Nothing in this package
places, cancels, replaces, or simulates toward a real order; every network
access is an unauthenticated, read-only GET against the venue's historical
market-data host.

Design predeclared in
``$AUTOTRADER_QA/reports/crypto-maker-execution/research-journal.md``.
"""
