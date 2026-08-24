"""
Signing keys and the JWKS document.

This service is the only holder of a private key in the system, so the whole
trust boundary rests on one property: the private half never leaves this
process, and the AI service only ever sees the public half over `/.well-known/
jwks.json`. A shared symmetric secret would have collapsed that — either side
could then mint tokens for the other's domain, and rotation would become a
coordinated deploy of two services instead of a file drop in one.

Keys live as PEM files in `keys_dir`, one per key, named by their `kid`. The
`kid` is the RFC 7638 JWK thumbprint rather than a filename or a counter, so it
is derived from the key material itself: the same key always gets the same id,
and two different keys can never collide on one.

Rotation, which is the operation this layout exists to make boring:

  1. `python -m product.cli keygen` writes a second PEM into keys_dir.
  2. JWKS now publishes BOTH keys. Verifiers cache it and pick by `kid`, so
     tokens signed with either one validate.
  3. Set PRODUCT_ACTIVE_KID to the new kid and restart. New tokens use it.
  4. Once every token signed by the old key has expired, delete its PEM.

Step 2 is the one people skip. Pull the old public key before its tokens
expire and every request in flight fails verification at once — the AI service
caches JWKS for an hour, so the blast radius is larger than it looks.
"""

import base64
import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    RSAPublicKey,
)

from product.config import settings

KEY_SIZE = 2048
PEM_SUFFIX = ".pem"


class NoSigningKey(RuntimeError):
    """No usable key on disk. The service cannot issue tokens."""


def _b64u(data: bytes) -> str:
    """base64url without padding, which is what JWK expects."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_to_b64u(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return _b64u(value.to_bytes(length, "big"))


def thumbprint(public_key: RSAPublicKey) -> str:
    """RFC 7638 JWK thumbprint — the canonical id for this key.

    The member order below is not stylistic: RFC 7638 requires the required
    members lexicographically ordered with no whitespace, and any deviation
    produces a different hash and therefore a `kid` no other implementation
    agrees with.
    """
    numbers = public_key.public_numbers()
    canonical = json.dumps(
        {
            "e": _int_to_b64u(numbers.e),
            "kty": "RSA",
            "n": _int_to_b64u(numbers.n),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return _b64u(hashlib.sha256(canonical.encode("ascii")).digest())


@dataclass(frozen=True, slots=True)
class SigningKey:
    kid: str
    private_key: RSAPrivateKey
    path: Path

    @property
    def public_key(self) -> RSAPublicKey:
        return self.private_key.public_key()

    def jwk(self) -> dict:
        numbers = self.public_key.public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": settings().jwt_algorithm,
            "kid": self.kid,
            "n": _int_to_b64u(numbers.n),
            "e": _int_to_b64u(numbers.e),
        }


_keys: dict[str, SigningKey] | None = None
_lock = threading.Lock()


def generate(keys_dir: Path | None = None) -> SigningKey:
    """Create a new keypair and write it to disk. Returns the new key."""
    directory = keys_dir or settings().keys_dir
    directory.mkdir(parents=True, exist_ok=True)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    kid = thumbprint(private_key.public_key())
    path = directory / f"{kid}{PEM_SUFFIX}"

    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        # Unencrypted: the file itself is the secret, and a passphrase this
        # process would have to hold in the environment anyway adds ceremony
        # rather than protection. Protect it with file permissions and by
        # keeping keys_dir out of the image and out of git.
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    try:
        path.chmod(0o600)  # no-op on Windows, correct everywhere else
    except OSError:
        pass

    reload()
    return load()[kid]


def reload() -> None:
    """Drop the cache so the next call re-reads keys_dir."""
    global _keys
    with _lock:
        _keys = None


def load() -> dict[str, SigningKey]:
    """Every key on disk, by kid. Cached; call `reload()` after a keygen."""
    global _keys
    with _lock:
        if _keys is not None:
            return _keys

        directory = settings().keys_dir
        found: dict[str, SigningKey] = {}
        if directory.is_dir():
            for path in sorted(directory.glob(f"*{PEM_SUFFIX}")):
                try:
                    private_key = serialization.load_pem_private_key(
                        path.read_bytes(), password=None
                    )
                except Exception:
                    # A malformed file must not take the whole service down —
                    # the other keys are still valid and still verify tokens.
                    continue
                if not isinstance(private_key, RSAPrivateKey):
                    continue
                kid = thumbprint(private_key.public_key())
                found[kid] = SigningKey(kid=kid, private_key=private_key, path=path)

        _keys = found
        return _keys


def active() -> SigningKey:
    """The key that signs new tokens.

    An explicitly configured kid that is missing is a hard error, never a
    silent fallback: quietly signing with a different key than the one
    operations intended is exactly the confusion a rotation must not create.
    """
    keys = load()
    if not keys:
        raise NoSigningKey(
            f"no signing key in {settings().keys_dir} — run: python -m product.cli keygen"
        )

    configured = settings().active_kid
    if configured:
        if configured not in keys:
            raise NoSigningKey(
                f"PRODUCT_ACTIVE_KID={configured} is not in {settings().keys_dir}"
            )
        return keys[configured]

    # Unset: newest file wins. Fine for development, which is why production
    # is expected to pin it.
    return max(keys.values(), key=lambda k: k.path.stat().st_mtime)


def jwks() -> dict:
    """The public document. Every key, so a rotation verifies both ways."""
    return {"keys": [key.jwk() for key in load().values()]}
