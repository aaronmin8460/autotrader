"""Equity defensive short-sleeve research: can a constrained, DEFENSIVE-only
short sleeve improve the EDA-1 U10 frontier?

Everything here is predeclared in the program's search ledger
(`/Volumes/AUTOTRADER_QA/reports/equity-defensive-short-sleeve/search-ledger.md`)
before its first result-producing run. Research only: nothing in this package
touches a runtime, a service, an operational database, or a broker order
path. No module here can submit an order — there is no execution seam.
"""

from __future__ import annotations

REPORT_ROOT = "/Volumes/AUTOTRADER_QA/reports/equity-defensive-short-sleeve"
SHORT_DATASETS = "/Volumes/AUTOTRADER_QA/datasets/equity-short-sleeve"

#: The frozen prior artifacts this program reads, read-only.
CHARACTER_REPORTS = "/Volumes/AUTOTRADER_QA/reports/equity-eda1-asset-character"
NEXTGEN_REPORTS = "/Volumes/AUTOTRADER_QA/reports/equity-eda1-next-generation"
CHARACTER_DATASETS = "/Volumes/AUTOTRADER_QA/datasets/equity-asset-character"

__all__ = [
    "CHARACTER_DATASETS",
    "CHARACTER_REPORTS",
    "NEXTGEN_REPORTS",
    "REPORT_ROOT",
    "SHORT_DATASETS",
]
