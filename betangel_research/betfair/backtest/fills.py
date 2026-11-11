"""Fill models of the research lane: when a simulated resting order counts as filled (pure).

An "order" here is a simulated intent inside the backtest package. One order is judged once per fill model, as one
lp.Leg per model. The models differ only in the queue: how much of the size shown at placement, at the order's price
on its own side, stands ahead of it. The fractions come from a registration (fill_models.queue_fraction), not from
this module: optimistic is the front of the queue, central is behind half the size shown, pessimistic is behind all
of it (lp-v1's join rule).

The rules, for every model:
1. Messages that share a pt are one batch. A fill is judged when a batch is complete.
2. A batch is open when the market status in force before its first message is OPEN (is_open). So the batch that
   suspends a market is open, and the batch that reopens it is not.
3. A rise of traded volume counts only in an open batch, and only when the batch also carries an atb or atl entry
   at that price for that runner (counted_rises; lp-v1's counting rule). A rise in a batch that is not open is
   never counted later: the traded fold has already taken it as the price's last published value.
4. judge gives the first kind that holds, in lp.FILL_KINDS' order. trade_through: the batch counted a rise at a
   price beyond the leg's (below a LAY, above a BACK), whatever its size. quote_cross: the size shown on the
   opposite side at or through the leg's price is at least the stake. volume_at_price: the counted volume at the
   leg's own price has risen since placement by at least divisor x (queue + stake).
5. One fill, in full, at the leg's own price. Nothing at or before placement fills a leg, and no fill comes from a
   batch that is not open (judged).
6. A leg that lapses is not judged in a batch when inPlay was true before that batch (judged).

The cross test differs from lp-v1's on purpose. lp-v1 asked for queue + stake on the opposite side. By price-time
priority an opposing order can come to rest at or through our price only after every order ahead of ours at that
price is matched or pulled, so asking for the queue again would count it twice. The stake is still needed, because
a fill is all or nothing.

What this does not do: no latency, no partial fill, no queue position beyond the three fractions, no re-pricing.
It reads no file and no clock and keeps no state. Standard library only; it imports lp and rulefile.
"""

from .lp import CROSS, FILL_KINDS, THROUGH, VOLUME
from .rulefile import LAPSE

OPTIMISTIC, CENTRAL, PESSIMISTIC = MODELS = ("optimistic", "central", "pessimistic")
OPEN = "OPEN"


def queue_ahead(fraction, shown):
    """The queue of one model: fraction (exact, 0 to 1) of the size shown at placement at the leg's price on its
    own side (exact, GBP of backer's stake; 0 when the level is empty)."""
    return fraction * shown


def is_open(status_before):
    """Rule 2: whether a batch is open, from the market status in force before its first message."""
    return status_before == OPEN


def counted_rises(batch, was_open):
    """Rule 3: the (runner, price, rise) of an lp._Batch that count as traded volume."""
    if not was_open:
        return []
    return [(rid, price, rise) for rid, price, rise in batch.rises if (rid, price) in batch.entries]


def judged(leg, pt, was_open, was_in_play, persist):
    """Rules 5 and 6: whether a leg can fill in the batch at pt. was_open and was_in_play describe the market just
    before the batch; persist is the order's "lapse" or "keep"."""
    return leg.live(pt) and was_open and not (persist == LAPSE and was_in_play)


def judge(leg, stake, divisor, counted_prices, counted_at_price, shown):
    """Rule 4: the fill kind of a judged leg in one batch, or None. counted_prices: the prices at which the batch
    counted a rise for the leg's runner. counted_at_price: that runner's counted volume at the leg's price now, in
    the files' units. shown: lp.facing(ladders, leg) after the batch when the batch carried an image or a ladder key
    for the runner, else None."""
    kinds = set()
    if any(leg.beyond(price) for price in counted_prices):
        kinds.add(THROUGH)
    if shown is not None and shown >= stake:
        kinds.add(CROSS)
    if leg.price in counted_prices and counted_at_price - leg.base.get(leg.price, 0) >= divisor * (leg.queue + stake):
        kinds.add(VOLUME)
    return next((kind for kind in FILL_KINDS if kind in kinds), None)
