"""Betfair Exchange client: delayed-key reads, a stream recorder and one paper-default order gateway.

Standard library only. Paper mode is the default; live orders need every arming condition in
docs/betfair-order-path.md, including the operator retyping each order at a terminal, and no
strategy may submit orders automatically. Credentials live only in the macOS Keychain; live order
state lives outside every Git worktree. Nothing here is evidence of profitability.

"spec section N" in the docstrings and comments of this package and its tests refers to the binding
implementation spec committed at .team-work/betfair-exchange-20261010/final.txt (its amendments are
listed in that directory's brief.md); the docs/ files number their own sections independently.
"""
