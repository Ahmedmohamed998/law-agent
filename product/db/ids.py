"""ULID-ish identifiers, byte-compatible with the AI service's scheme.

Deliberately duplicated rather than imported from `app.db.ids`: this package
must stay extractable to its own repository, and a shared import would make the
identity service depend on the RAG service's source tree. The format matters
because `public.users.id` becomes `ai.sessions.user_id` on the other side —
same alphabet, same length, same sort order.

48 bits of millisecond timestamp + 80 bits of randomness, Crockford base32.
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
