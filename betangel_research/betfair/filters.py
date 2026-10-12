"""A neutral market filter, mapped separately to the REST and stream filter shapes.

The two Betfair shapes use different key names (REST marketCountries/marketTypeCodes vs stream
countryCodes/marketTypes) and only REST takes a start-time range, so callers keep one neutral
filter and convert at the edge. The stream data-field rules keep recordings replayable by
stream.messages() and, with motif_compatible, by the microstructure motifs (ADV-like fields).
"""

import re

from .errors import ConfigError
from .receipts import parse_utc_ns, utc_ms_from_ns

MARKET_ID_RE = re.compile(r"1\.[0-9]{1,12}")      # ASCII digits only, never \d
NEUTRAL_KEYS = frozenset({"event_type_ids", "country_codes", "market_types", "market_ids"})
_ITEM_RULES = {
    "event_type_ids": re.compile(r"[0-9]{1,12}"),
    "country_codes": re.compile(r"[A-Z]{2}"),
    "market_types": re.compile(r"[A-Z0-9_]{1,40}"),
    "market_ids": MARKET_ID_RE,
}
MAX_FILTER_ITEMS = 1000
_KEY_TEXT_RE = re.compile(r"[A-Za-z0-9_\-.]{1,80}")

STREAM_DATA_FIELDS = frozenset({"EX_BEST_OFFERS_DISP", "EX_BEST_OFFERS", "EX_ALL_OFFERS", "EX_TRADED",
                                "EX_TRADED_VOL", "EX_LTP", "EX_MARKET_DEF", "SP_TRADED", "SP_PROJECTED"})
MARKET_DATA_FILTER_KEYS = frozenset({"fields", "ladderLevels"})


def validate_neutral_filter(f):
    """Exact keys from NEUTRAL_KEYS; lists of unique, well-formed strings; at least one constraint."""
    if not isinstance(f, dict):
        raise ConfigError("market_filter_must_be_object")
    unknown = set(f) - NEUTRAL_KEYS
    if unknown:
        names = sorted(k if isinstance(k, str) and _KEY_TEXT_RE.fullmatch(k) else "<malformed_key>" for k in unknown)
        raise ConfigError("market_filter_unknown_key:" + ",".join(names))
    cleaned = {}
    for key in sorted(f):
        values = f[key]
        if not isinstance(values, list) or len(values) > MAX_FILTER_ITEMS:
            raise ConfigError(f"market_filter_{key}_must_be_list")
        if len(set(map(str, values))) != len(values):
            raise ConfigError(f"market_filter_{key}_duplicates")
        for item in values:
            if not isinstance(item, str) or not _ITEM_RULES[key].fullmatch(item):
                raise ConfigError(f"market_filter_{key}_invalid_item")
        cleaned[key] = list(values)
    if not any(cleaned.values()):
        # An empty filter would ask for every market on the exchange.
        raise ConfigError("market_filter_empty")
    return cleaned


def _wire_time(text, name):
    try:
        return utc_ms_from_ns(parse_utc_ns(text))
    except ValueError:
        raise ConfigError(f"{name}_must_be_utc_iso_timestamp") from None


def to_rest_market_filter(f, *, start_from_utc=None, start_to_utc=None):
    """REST MarketFilter; empty keys omitted; marketStartTime only when a bound is given."""
    f = validate_neutral_filter(f)
    out = {}
    for neutral, rest in (("event_type_ids", "eventTypeIds"), ("country_codes", "marketCountries"),
                          ("market_types", "marketTypeCodes"), ("market_ids", "marketIds")):
        if f.get(neutral):
            out[rest] = list(f[neutral])
    window = {}
    if start_from_utc is not None:
        window["from"] = _wire_time(start_from_utc, "start_from_utc")
    if start_to_utc is not None:
        window["to"] = _wire_time(start_to_utc, "start_to_utc")
    if "from" in window and "to" in window and parse_utc_ns(window["from"]) > parse_utc_ns(window["to"]):
        raise ConfigError("market_start_window_reversed")
    if window:
        out["marketStartTime"] = window
    return out


def to_stream_market_filter(f):
    """Stream MarketFilter; empty keys omitted; never any time key."""
    f = validate_neutral_filter(f)
    out = {}
    for neutral, stream in (("event_type_ids", "eventTypeIds"), ("country_codes", "countryCodes"),
                            ("market_types", "marketTypes"), ("market_ids", "marketIds")):
        if f.get(neutral):
            out[stream] = list(f[neutral])
    return out


MOTIF_REFUSED_FIELDS = ("EX_ALL_OFFERS", "SP_TRADED", "SP_PROJECTED")


def validate_market_data_filter(d, *, motif_compatible):
    """Exact keys {fields, ladderLevels}. EX_MARKET_DEF is required (replay needs marketTime);
    EX_BEST_OFFERS_DISP is refused (virtual prices are rejected by the motif reader).

    motif_compatible also requires what microstructure.sample_features accepts: EX_BEST_OFFERS
    (batb/batl, its "ADV" format), no PRO fields (EX_ALL_OFFERS gives atb/atl, SP_TRADED spb/spl,
    SP_PROJECTED spn/spf, each marking a record unsupported) and at most 3 ladder levels."""
    if type(motif_compatible) is not bool:
        raise ConfigError("motif_compatible_must_be_boolean")
    if not isinstance(d, dict) or set(d) != MARKET_DATA_FILTER_KEYS:
        raise ConfigError("market_data_filter_keys_must_be_fields_and_ladderLevels")
    fields = d["fields"]
    if (not isinstance(fields, list) or not fields or any(not isinstance(x, str) for x in fields)
            or len(set(fields)) != len(fields)):
        raise ConfigError("market_data_filter_fields_must_be_unique_strings")
    unknown = set(fields) - STREAM_DATA_FIELDS
    if unknown:
        raise ConfigError("market_data_filter_unknown_field:" + ",".join(sorted(unknown)))
    if "EX_BEST_OFFERS_DISP" in fields:
        raise ConfigError("market_data_filter_ex_best_offers_disp_refused")
    if "EX_MARKET_DEF" not in fields:
        raise ConfigError("market_data_filter_ex_market_def_required")
    levels = d["ladderLevels"]
    if type(levels) is not int or not 1 <= levels <= 10:
        raise ConfigError("market_data_filter_ladder_levels_out_of_range")
    if motif_compatible and levels > 3:
        raise ConfigError("market_data_filter_ladder_levels_above_3_not_motif_compatible")
    if motif_compatible:
        for field in MOTIF_REFUSED_FIELDS:
            if field in fields:
                raise ConfigError("market_data_filter_fields_not_motif_compatible:" + field)
        if "EX_BEST_OFFERS" not in fields:
            raise ConfigError("market_data_filter_fields_not_motif_compatible:EX_BEST_OFFERS_required")
    return {"fields": list(fields), "ladderLevels": levels}
