"""Research-only studies. Not part of the shipped `autotrader` package.

Nothing under here is imported by the trading system, deployed, or reachable
from a runtime. A study reads stored bars and writes reports; it holds no
broker client, submits nothing, and cannot change what the system trades.
"""
