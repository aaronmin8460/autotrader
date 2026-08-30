"""The SPY/QQQ historical pilot for Decision Engines V1-V5.

A *pilot*, not the study. Its question is whether the historical research
pipeline is correct for US equities - trustworthy bars, honest session
semantics, causally valid engine inputs, a walk-forward that cannot see
forward - before the ten-symbol evaluation is paid for. Two symbols are enough
to answer that and are deliberately not enough to rank an engine.

Nothing here trades, deploys, or writes to operational state. Every module is
read-only with respect to the broker: bars and the session calendar are fetched
with GET requests and nothing else is ever sent.
"""

#: The pilot universe. Two symbols, on purpose (docs: the pilot is a
#: correctness proof, and a two-symbol sample cannot rank an engine).
PILOT_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ")

__all__ = ["PILOT_SYMBOLS"]
