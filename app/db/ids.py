"""ULID-ish identifiers: time-sortable, opaque, URL-safe, no extra dependency.

48 bits of millisecond timestamp + 80 bits of randomness, Crockford base32.
Sorting by id sorts by creation time, which makes index locality good and
makes an id useful as a debugging breadcrumb. Unlike a UUIDv4 primary key it
does not scatter B-tree inserts across the whole index.
"""

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford: no I, L, O, U


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out))


def new_id() -> str:
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode(ms, 10) + _encode(rand, 16)
