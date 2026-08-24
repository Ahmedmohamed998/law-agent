"""
Password hashing.

Argon2id with the library's defaults, which track the current OWASP guidance.
Two behaviours here are less obvious than the hashing itself and matter more:

  * `verify` never raises to the caller. A wrong password, a corrupt hash, and
    an unparseable stored value all mean the same thing at the API boundary —
    the credential is not good — and letting them differ leaks which case a
    given account is in.

  * `dummy_verify` exists so that logging in with an unknown email costs the
    same as logging in with a known one. Without it, response time answers
    "does this address have an account?" for anyone who cares to measure, which
    is a real disclosure for a legal service where the client list is sensitive.
"""

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

_hasher = PasswordHasher()

# Argon2id hash of a throwaway value, used only to burn the same CPU on the
# unknown-user path. Computed once at import so it costs nothing per request.
_DUMMY_HASH = _hasher.hash("timing-equalisation-placeholder")


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(stored_hash: str | None, plaintext: str) -> bool:
    """True if the password matches. Never raises."""
    if not stored_hash:
        # An anonymous user has no password. Still burn the time.
        dummy_verify()
        return False
    try:
        _hasher.verify(stored_hash, plaintext)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def dummy_verify() -> None:
    """Spend the same time as a real verification, for an account that does
    not exist."""
    try:
        _hasher.verify(_DUMMY_HASH, "not-the-password")
    except Exception:
        pass


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash was made with weaker parameters than current policy.

    Call it after a successful verify: that is the only moment the plaintext is
    in hand to re-hash with, so it is the only chance to upgrade an old hash
    without asking the user to change their password.
    """
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except Exception:
        return False
