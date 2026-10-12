"""Secret values, a process-wide registry of them, and write-time leak checks.

A Secret never renders its value (repr/str/format all say "<redacted>") and refuses pickling, so
it cannot slip into JSON, logs or exception text by accident. Every value wrapped in a Secret is
registered (except the username and the key passphrase, see Secret); assert_clean scans bytes about to be written for any registered value, and
refuse_forbidden rejects records that carry credential-shaped keys at any depth. This blocks
mistakes, not a malicious process running as the same user.
"""

import json

from .errors import SecretLeak

_REGISTRY = set()
# Shorter values are not scanned: they would match ordinary betIds, refs and hex digests and refuse
# legitimate writes. Every scanned secret is at least this long (app keys, tokens, Betfair passwords).
_MIN_REGISTERED_LEN = 8


def register(value):
    """Remember a secret value so assert_clean can find it. Values under 8 characters are not scanned."""
    if not isinstance(value, str):
        raise TypeError("secret values must be strings")
    if len(value) >= _MIN_REGISTERED_LEN:
        _REGISTRY.add(value)


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value, *, register_value=True):
        """register_value=False keeps the value out of assert_clean's substring scan. The Betfair
        username uses it: a short user-chosen identifier that can legitimately appear in a profile
        name or a home-directory path, where scanning for it would block the registry, the journal
        and the kill path while protecting nothing ("username" is a forbidden key everywhere and the
        account fingerprint is its only stored derivative). So does the certificate key passphrase:
        it only decrypts the key file in this process and is never sent or written, and a short one
        would match betIds and hashes and refuse journal events after a real send. It still never
        renders or pickles."""
        if not isinstance(value, str) or value == "":
            raise ValueError("secret value must be a non-empty string")
        object.__setattr__(self, "_value", value)
        if register_value:
            register(value)

    def reveal(self):
        return self._value

    def __setattr__(self, name, value):
        raise AttributeError("Secret is immutable")

    def __delattr__(self, name):
        raise AttributeError("Secret is immutable")

    def __repr__(self):
        return "<redacted>"

    def __str__(self):
        return "<redacted>"

    def __format__(self, spec):
        return "<redacted>"

    def __reduce__(self):
        raise TypeError("Secret cannot be pickled")

    def __reduce_ex__(self, protocol):
        raise TypeError("Secret cannot be pickled")

    def __getstate__(self):
        raise TypeError("Secret cannot be pickled")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


def _variants(value):
    # The raw value plus its JSON-escaped forms, so an escaped copy inside a JSON document is found.
    forms = {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
    return [f for f in forms if f]


def contains_registered(text):
    """True if any registered secret value (or its JSON-escaped form) occurs in text (str)."""
    if not isinstance(text, str):
        raise TypeError("contains_registered scans str only")
    return any(form in text for value in list(_REGISTRY) for form in _variants(value))


def assert_clean(data, *, where):
    """Raise SecretLeak(where) if any registered secret occurs in data (bytes or str).

    The exception names only the location, never the value.
    """
    if isinstance(data, (bytes, bytearray, memoryview)):
        haystack = bytes(data)
        for value in list(_REGISTRY):
            for form in _variants(value):
                if form.encode("utf-8") in haystack:
                    raise SecretLeak(f"secret_value_in_output:{where}")
        return
    if isinstance(data, str):
        for value in list(_REGISTRY):
            for form in _variants(value):
                if form in data:
                    raise SecretLeak(f"secret_value_in_output:{where}")
        return
    raise TypeError("assert_clean scans bytes or str only")


FORBIDDEN_KEYS = frozenset({"session", "sessiontoken", "token", "ssoid", "password", "username", "appkey",
                            "xauthentication", "xapplication", "headers", "authorization", "cookie", "setcookie",
                            "sourceidkey", "errordetails", "firstname", "lastname"})


def normalise_key(key):
    return key.lower().replace("-", "").replace("_", "").replace(" ", "")


def refuse_forbidden(value, *, where):
    """Reject any credential-shaped key (case and separator insensitive) or Secret, at any depth."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, Secret):
            raise SecretLeak(f"secret_object_in_output:{where}")
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str) and normalise_key(key) in FORBIDDEN_KEYS:
                    raise SecretLeak(f"forbidden_key_in_output:{where}")
                stack.append(child)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)


def scrub(value):
    """Recursive copy without forbidden keys or Secret objects (used by response normalisers)."""
    if isinstance(value, Secret):
        return None
    if isinstance(value, dict):
        return {key: scrub(child) for key, child in value.items()
                if not (isinstance(key, str) and normalise_key(key) in FORBIDDEN_KEYS)
                and not isinstance(child, Secret)}
    if isinstance(value, (list, tuple)):
        return [scrub(child) for child in value if not isinstance(child, Secret)]
    return value
