"""Point-in-time joins with explicit publication and identity-link availability."""

from datetime import datetime, timezone
import math


def timestamp_ms(value):
    """Require a timezone; never silently interpret local/naive timestamps as UTC."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 string with timezone")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond % 1000:
        raise ValueError("sub-millisecond timestamps are unsupported; do not round availability backward")
    delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def iso_time(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


def join_asof(entity_id, decision_time, observations, links, delay_seconds=0, max_age_seconds=None):
    """Join explicit IDs only. Inputs are iterable dictionaries; see data-contract.md.

    Later corrections and identity mappings are invisible before known_at/available_at.
    Returns source-qualified features and their evidence timestamps, never fuzzy links.
    """
    if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, (int, float)) or not math.isfinite(delay_seconds) or delay_seconds < 0:
        raise ValueError("delay_seconds must be finite and nonnegative")
    if max_age_seconds is not None and (isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or not math.isfinite(max_age_seconds) or max_age_seconds < 0):
        raise ValueError("max_age_seconds must be finite and nonnegative")
    cutoff = timestamp_ms(decision_time) - delay_seconds * 1000
    latest_links = {}
    link_versions = {}
    for link in links:
        known = timestamp_ms(link["known_at"])
        if known > cutoff:
            continue
        key = (link["source"], link["source_entity_id"])
        version = (*key, known)
        if version in link_versions and link_versions[version] != link["entity_id"]:
            raise ValueError(f"ambiguous identity mapping: {key}")
        link_versions[version] = link["entity_id"]
        previous = latest_links.get(key)
        if previous is None or known > previous[0]:
            latest_links[key] = (known, link["entity_id"])
    chosen = {}
    seen = {}
    for obs in observations:
        event = timestamp_ms(obs["event_time"])
        available = timestamp_ms(obs["available_at"])
        if event > available:
            raise ValueError("measurement event_time cannot follow available_at")
        value = float(obs["value"])
        if not math.isfinite(value):
            raise ValueError("feature values must be finite")
        key = (obs["source"], obs["source_entity_id"])
        link = latest_links.get(key)
        if event > cutoff or available > cutoff or link is None or link[1] != entity_id:
            continue
        if max_age_seconds is not None and cutoff - event > max_age_seconds * 1000:
            continue
        feature = f"{obs['source']}:{obs['feature']}"
        version = (feature, event, available)
        if version in seen and seen[version] != (key, value):
            raise ValueError(f"ambiguous feature version: {feature}")
        seen[version] = (key, value)
        if feature not in chosen or (event, available) > chosen[feature][0]:
            chosen[feature] = ((event, available), {
                "value": value, "event_time": iso_time(event),
                "available_at": iso_time(available), "link_known_at": iso_time(link[0]),
                "source_entity_id": obs["source_entity_id"],
            })
    return {"entity_id": entity_id, "cutoff": iso_time(cutoff),
            "features": {name: item[1] for name, item in sorted(chosen.items())}}
