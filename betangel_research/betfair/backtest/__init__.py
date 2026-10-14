"""Offline backtesting of Betfair strategies on explicitly supplied local historical, recorded or synthetic files.

Strategies may generate simulated order intents only inside this package, and only the offline simulator may
consume them; nothing here submits an order. The package must not import, directly or transitively, the order
gateway, account/session/credential clients, network transports or the trading CLI, and must not read account
profiles or live order state (AGENTS.md; checked statically by tests/test_betfair_backtest_boundaries.py,
which is an architectural rule, not a sandbox). Every run starts from an experiment contract (contract.py)
meant to fix its choices before outcomes are inspected; validation cannot show when a contract was written,
and the P0 contract is still a draft. Nothing here is evidence of profitability.
"""
