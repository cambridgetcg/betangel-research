"""CLASSIC price ladder in integer hundredths, and exact money/price parsing in integer pence.

Prices and stakes never pass through float arithmetic inside the order path: a price is an int
number of hundredths (3.45 -> 345) and a stake an int number of pence (2.00 -> 200). Text is
parsed with a strict regex and Decimal, so "nan", "inf", "1e1", "+1", " 1" and values <= 0 are
refused. Nothing here snaps a price implicitly; snap() exists for callers that ask for it.
Floats appear only at the wire edge (wire_price/wire_size), where x/100 round-trips exactly.
"""

from bisect import bisect_left, bisect_right
from decimal import Decimal, InvalidOperation
import math
import re

from .constants import CLASSIC_BANDS, CLASSIC_LADDER_TICKS, CLASSIC_MAX_PRICE_H, CLASSIC_MIN_PRICE_H

# [0-9], never \d: \d also matches non-ASCII digits, which Decimal would silently accept.
MONEY_RE = r"^[0-9]{1,6}(\.[0-9]{1,2})?$"
_MONEY = re.compile(MONEY_RE)
_HUNDRED = Decimal(100)


def _build_ladder():
    prices = []
    for low, high, step in CLASSIC_BANDS:
        prices.extend(range(low, high, step))
    prices.append(CLASSIC_MAX_PRICE_H)
    ladder = tuple(prices)
    if (len(ladder) != CLASSIC_LADDER_TICKS or ladder[0] != CLASSIC_MIN_PRICE_H
            or any(b <= a for a, b in zip(ladder, ladder[1:]))):
        raise RuntimeError("CLASSIC ladder bands do not give the documented 350 ascending ticks")
    return ladder


LADDER_CLASSIC = _build_ladder()
_INDEX = {price: index for index, price in enumerate(LADDER_CLASSIC)}


def _decimal_text(text, name):
    # fullmatch, so a trailing newline (which "$" alone would allow) is refused too.
    if not isinstance(text, str) or not _MONEY.fullmatch(text):
        raise ValueError(f"{name} must be plain decimal text with at most 2 dp (no sign, exponent, nan or inf)")
    return Decimal(text)


def parse_money(text):
    """Pence from operator text such as "2.00"; <= 0 is refused."""
    pence = int(_decimal_text(text, "money") * _HUNDRED)
    if pence <= 0:
        raise ValueError("money must be greater than zero")
    return pence


def parse_price(text):
    """Hundredths from operator text such as "3.45"; must be exactly on the CLASSIC ladder."""
    price_h = int(_decimal_text(text, "price") * _HUNDRED)
    if price_h not in _INDEX:
        raise ValueError("price must be exactly on the CLASSIC ladder (no implicit snapping)")
    return price_h


def _exact_hundredths(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, not a boolean")
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"{name} must be finite")
        amount = Decimal(repr(value))       # shortest repr: 3.45 -> Decimal("3.45")
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{name} must be finite")
        amount = value
    elif isinstance(value, str):
        amount = _decimal_text(value, name)
    else:
        raise ValueError(f"{name} must be a number")
    if abs(amount) >= Decimal(10) ** 12:
        raise ValueError(f"{name} out of range")
    try:
        scaled = amount * _HUNDRED
        whole = scaled.to_integral_value()
    except InvalidOperation:
        raise ValueError(f"{name} out of range") from None
    if scaled != whole:
        raise ValueError(f"{name} must have at most 2 decimal places")
    return int(whole)


def to_hundredths(value):
    """A decimal price (3.45, "3.45", Decimal) as int hundredths; more than 2 dp raises ValueError."""
    return _exact_hundredths(value, "price")


def to_pence(value):
    """A GBP amount (2.0, "2.00", Decimal) as int pence; more than 2 dp raises ValueError."""
    return _exact_hundredths(value, "money")


def is_on_ladder(price_h):
    return type(price_h) is int and price_h in _INDEX


def tick_index(price_h):
    if not is_on_ladder(price_h):
        raise ValueError("price is not on the CLASSIC ladder")
    return _INDEX[price_h]


def tick_offset(price_h, n):
    """The ladder price n ticks above (n > 0) or below (n < 0) price_h; off the ladder raises."""
    if type(n) is not int:
        raise ValueError("tick offset must be an integer")
    index = tick_index(price_h) + n
    if not 0 <= index < len(LADDER_CLASSIC):
        raise ValueError("tick offset leaves the CLASSIC ladder")
    return LADDER_CLASSIC[index]


def ticks_between(a_h, b_h):
    """Absolute number of ladder ticks between two on-ladder prices."""
    return abs(tick_index(a_h) - tick_index(b_h))


def snap(price_h, direction):
    """Nearest ladder price at or below ("down") or at or above ("up"). Only on explicit request."""
    if type(price_h) is not int:
        raise ValueError("price must be integer hundredths")
    if direction == "down":
        index = bisect_right(LADDER_CLASSIC, price_h) - 1
    elif direction == "up":
        index = bisect_left(LADDER_CLASSIC, price_h)
    else:
        raise ValueError('snap direction must be "down" or "up"')
    if not 0 <= index < len(LADDER_CLASSIC):
        raise ValueError("price lies outside the CLASSIC ladder")
    return LADDER_CLASSIC[index]


def wire_price(price_h):
    """The JSON body value for an on-ladder price (x/100 is exact to 2 dp after repr)."""
    if not is_on_ladder(price_h):
        raise ValueError("wire price must be on the CLASSIC ladder")
    return price_h / 100


def wire_size(size_p):
    """The JSON body value for a stake in pence."""
    if type(size_p) is not int or size_p <= 0:
        raise ValueError("wire size must be a positive integer number of pence")
    return size_p / 100
