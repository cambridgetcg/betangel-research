"""Single-use order capabilities bound to the sha256 of one exact request body.

Only risk.RiskGuard.issue may call _mint, and only for an approved live decision (a boundary test
checks the call sites). A capability lives five seconds, can be consumed once, and only for the
method and body digest it was minted for; any consume attempt burns it. It cannot be copied or
pickled, so a second unconsumed twin cannot exist.

A successful consume records the body digest in a small module-private, single-use set; the
transport's digest gate takes it out again (take_consumed) and refuses any non-read JSON-RPC body
whose sha256 was not recorded there, so a body digest alone is not enough to pass the gate. This
is an in-process check against mistakes: code running in the same process could still reach the
private set, so the order gateway, the risk guard and the operator echo remain the real controls.
"""

from collections import OrderedDict
import hmac
import re
import threading

from .constants import BETTING_METHOD_PREFIX, READ_METHODS
from .errors import OrderRefused

CAPABILITY_TTL_NS = 5_000_000_000
CONSUMED_DIGESTS_MAX = 16          # consumed digests waiting for the transport (oldest dropped first)
_MINT_TOKEN = object()
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CONSUMED = OrderedDict()          # body sha256 -> True, single use
_CONSUMED_LOCK = threading.Lock()


def _record_consumed(digest):
    with _CONSUMED_LOCK:
        _CONSUMED.pop(digest, None)
        _CONSUMED[digest] = True
        while len(_CONSUMED) > CONSUMED_DIGESTS_MAX:
            _CONSUMED.popitem(last=False)


def take_consumed(digest):
    """True (and forgotten) if a capability was consumed for this body digest and not yet taken."""
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        return False
    with _CONSUMED_LOCK:
        return _CONSUMED.pop(digest, None) is not None


class OrderCapability:
    __slots__ = ("decision_id", "method", "body_sha256", "mode", "expires_monotonic_ns", "_consumed")

    def __init__(self, *args, _token=None, **kw):
        if _token is not _MINT_TOKEN:
            raise TypeError("OrderCapability is minted only through RiskGuard.issue")
        values = dict(zip(("decision_id", "method", "body_sha256", "mode", "expires_monotonic_ns"), args))
        values.update(kw)
        for name in ("decision_id", "method", "body_sha256", "mode", "expires_monotonic_ns"):
            object.__setattr__(self, name, values[name])
        object.__setattr__(self, "_consumed", False)

    def __setattr__(self, name, value):
        raise AttributeError("OrderCapability is immutable")

    def __delattr__(self, name):
        raise AttributeError("OrderCapability is immutable")

    def __repr__(self):
        return f"<OrderCapability decision={self.decision_id} method={self.method} mode={self.mode}>"

    def __reduce__(self):
        raise TypeError("OrderCapability cannot be pickled")

    def __reduce_ex__(self, protocol):
        raise TypeError("OrderCapability cannot be pickled")

    def __copy__(self):
        raise TypeError("OrderCapability cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("OrderCapability cannot be copied")

    @property
    def consumed(self):
        return self._consumed

    def consume(self, *, method, body_sha256, now_monotonic_ns):
        """Burn the capability and return body_sha256, or raise OrderRefused with the reason."""
        if self._consumed:
            raise OrderRefused("capability_reused")
        object.__setattr__(self, "_consumed", True)
        if self.mode != "live":
            raise OrderRefused("capability_mode")
        # The shape check comes first: compare_digest raises TypeError on non-ASCII text.
        if (not isinstance(method, str) or not isinstance(body_sha256, str) or method != self.method
                or not _SHA256_RE.fullmatch(body_sha256)
                or not hmac.compare_digest(body_sha256, self.body_sha256)):
            raise OrderRefused("capability_mismatch")
        if type(now_monotonic_ns) is not int or now_monotonic_ns >= self.expires_monotonic_ns:
            raise OrderRefused("capability_expired")
        _record_consumed(body_sha256)
        return body_sha256


def _mint(decision_id, method, body_sha256, mode, now_monotonic_ns, ttl_ns=CAPABILITY_TTL_NS):
    """Called only from risk.py. Refuses anything but a live, non-read betting method."""
    if mode != "live":
        raise OrderRefused("capability_mode")
    if not isinstance(decision_id, str) or not decision_id:
        raise OrderRefused("capability_decision_missing")
    if (not isinstance(method, str) or not method.startswith(BETTING_METHOD_PREFIX) or method in READ_METHODS):
        raise OrderRefused("capability_method_invalid")
    if not isinstance(body_sha256, str) or not _SHA256_RE.fullmatch(body_sha256):
        raise OrderRefused("capability_digest_invalid")
    if type(now_monotonic_ns) is not int or type(ttl_ns) is not int or not 0 < ttl_ns <= CAPABILITY_TTL_NS:
        raise OrderRefused("capability_ttl_invalid")
    return OrderCapability(decision_id, method, body_sha256, mode, now_monotonic_ns + ttl_ns, _token=_MINT_TOKEN)
