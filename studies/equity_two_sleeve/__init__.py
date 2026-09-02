"""Two-sleeve portfolio blend research: EDA-1 U10 + A1-B U30.

Everything here is predeclared in the program's search ledger
(`/Volumes/AUTOTRADER_QA/reports/equity-two-sleeve-blend/search-ledger.md`)
before its first result-producing run. Research only: nothing in this package
touches a runtime, a service, or a broker order path.
"""

from __future__ import annotations

REPORT_ROOT = "/Volumes/AUTOTRADER_QA/reports/equity-two-sleeve-blend"
TWO_SLEEVE_DATASETS = "/Volumes/AUTOTRADER_QA/datasets/equity-two-sleeve"

__all__ = ["REPORT_ROOT", "TWO_SLEEVE_DATASETS"]
