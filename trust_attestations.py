"""Ed25519 trust attestations for Source Intelligence durable records.

The private key is local infrastructure state, not source-pack data.  Pack
metadata may carry the corresponding public identity for external audit, but
normal verification is pinned to the locally persisted key and never trusts a
public key supplied by the pack being verified.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

try:  # Available on the macOS/Linux deployment targets.
    import fcntl
except ImportError:  # pragma: no cover - defensive portability fallback
    fcntl = None


ATTESTATION_DOMAIN = "mbk.source-intelligence.trust-attestation"
ATTESTATION_VERSION = 1
SIGNATURE_ALGORITHM = "Ed25519"
PUBLIC_KEY_ENCODING = "base64-raw-ed25519"
PRIVATE_KEY_FILENAME = "source-intelligence-ed25519-private.pem"
KEY_ID_FILENAME = "source-intelligence-ed25519-key-id"
PIN_ENVIRONMENT_VARIABLE = "SOURCE_INTELLIGENCE_TRUST_KEY_ID"


def _data_dir() -> Path:
    configured = os.environ.get(
        "SOURCE_INTELLIGENCE_DATA_DIR",
        "~/.source-intelligence/data",
    )
    return Path(configured).expanduser().resolve()


def _private_key_path() -> Path:
    return _data_dir() / PRIVATE_KEY_FILENAME


def _key_id_path() -> Path:
    return _data_dir() / KEY_ID_FILENAME


def _canonical_message(kind: str, payload: Any) -> bytes:
    normalized_kind = str(kind or "").strip()
    if not normalized_kind:
        raise ValueError("Attestation kind is required")
    envelope = {
        "domain": ATTESTATION_DOMAIN,
        "version": ATTESTATION_VERSION,
        "kind": normalized_kind,
        "payload": payload,
    }
    return json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _public_key_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _key_id(public_key: Ed25519PublicKey) -> str:
    return "sha256:" + hashlib.sha256(
        _public_key_bytes(public_key)
    ).hexdigest()


def _assert_expected_identity(key: Ed25519PrivateKey) -> None:
    """Refuse a silent trust-root rotation after initial bootstrap."""
    actual = _key_id(key.public_key())
    configured = str(
        os.environ.get(PIN_ENVIRONMENT_VARIABLE, "") or ""
    ).strip()
    if configured and configured != actual:
        raise RuntimeError(
            "Source Intelligence signing key does not match the pinned "
            f"{PIN_ENVIRONMENT_VARIABLE}"
        )
    marker_path = _key_id_path()
    if marker_path.exists():
        recorded = marker_path.read_text(encoding="utf-8").strip()
        if recorded != actual:
            raise RuntimeError(
                "Source Intelligence signing key does not match its durable "
                "identity marker"
            )


def _read_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(
            f"Source-pack signing key is not a regular file: {path}"
        )
    # Enforce the requested owner-only mode even when restoring an older
    # persistent volume snapshot with broader permissions.
    os.chmod(path, 0o600)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            encoded = handle.read()
    finally:
        os.close(descriptor)
    key = serialization.load_pem_private_key(encoded, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("Configured source-pack signing key is not Ed25519")
    _assert_expected_identity(key)
    return key


def _write_new_private_key(path: Path, key: Ed25519PrivateKey) -> None:
    encoded = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".source-intelligence-ed25519-",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Creation is serialized by the adjacent lock.  os.replace provides
        # an atomic visibility boundary: readers see either no key or the
        # complete PEM, never a partially written private key.
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _write_identity_marker(path: Path, key: Ed25519PrivateKey) -> None:
    """Atomically persist the public fingerprint beside the private key."""
    encoded = (_key_id(key.public_key()) + "\n").encode("ascii")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".source-intelligence-key-id-",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _load_private_key(create_if_missing: bool) -> Ed25519PrivateKey:
    data_dir = _data_dir()
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = _private_key_path()
    try:
        key = _read_private_key(key_path)
        if not _key_id_path().exists():
            _write_identity_marker(_key_id_path(), key)
        return key
    except FileNotFoundError:
        if not create_if_missing:
            raise RuntimeError(
                f"Source-pack signing key is unavailable: {key_path}"
            )
        if _key_id_path().exists() or str(
            os.environ.get(PIN_ENVIRONMENT_VARIABLE, "") or ""
        ).strip():
            raise RuntimeError(
                "Source Intelligence signing key is missing but a durable "
                "trust identity already exists; restore the key instead of "
                "rotating it implicitly"
            )

    lock_path = data_dir / f".{PRIVATE_KEY_FILENAME}.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            return _read_private_key(key_path)
        except FileNotFoundError:
            key = Ed25519PrivateKey.generate()
            _write_new_private_key(key_path, key)
            _write_identity_marker(_key_id_path(), key)
            return _read_private_key(key_path)
    finally:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def signing_identity() -> dict:
    """Return the local public identity, creating the persistent key if needed."""
    public_key = _load_private_key(True).public_key()
    return {
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": _key_id(public_key),
        "public_key_encoding": PUBLIC_KEY_ENCODING,
        "public_key": base64.b64encode(
            _public_key_bytes(public_key)
        ).decode("ascii"),
    }


def public_key_fingerprint() -> str:
    """Return the stable out-of-band fingerprint for the local signing key."""
    return signing_identity()["key_id"]


def sign_attestation(kind: str, payload: Any) -> dict:
    """Sign one domain-separated, canonical JSON payload."""
    key = _load_private_key(True)
    signature = key.sign(_canonical_message(kind, payload))
    return {
        "domain": ATTESTATION_DOMAIN,
        "version": ATTESTATION_VERSION,
        "kind": str(kind or "").strip(),
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": _key_id(key.public_key()),
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def _coerce_public_key(value: Any) -> Ed25519PublicKey:
    if isinstance(value, Ed25519PublicKey):
        return value
    if isinstance(value, dict):
        value = value.get("public_key")
    if isinstance(value, str):
        encoded = value.strip().encode("ascii")
        if encoded.startswith(b"-----BEGIN"):
            key = serialization.load_pem_public_key(encoded)
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError("Trusted public key is not Ed25519")
            return key
        raw = base64.b64decode(encoded, validate=True)
    elif isinstance(value, bytes):
        raw = value
    else:
        raise ValueError("Trusted Ed25519 public key is required")
    if len(raw) != 32:
        raise ValueError("Trusted Ed25519 public key must contain 32 raw bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_attestation(
    kind: str,
    payload: Any,
    attestation: Any,
    trusted_public_key: Any = None,
) -> bool:
    """Verify against a trusted key, defaulting to the pinned local identity.

    A public key embedded in ``attestation`` or in its surrounding source pack
    is never selected implicitly.  External auditors may pass a public key
    obtained through an independent channel via ``trusted_public_key``.
    """
    try:
        if not isinstance(attestation, dict):
            return False
        normalized_kind = str(kind or "").strip()
        if (
            attestation.get("domain") != ATTESTATION_DOMAIN
            or type(attestation.get("version")) is not int
            or attestation.get("version") != ATTESTATION_VERSION
            or attestation.get("kind") != normalized_kind
            or attestation.get("algorithm") != SIGNATURE_ALGORITHM
        ):
            return False
        if trusted_public_key is None:
            public_key = _load_private_key(False).public_key()
        else:
            public_key = _coerce_public_key(trusted_public_key)
        if attestation.get("key_id") != _key_id(public_key):
            return False
        signature = base64.b64decode(
            str(attestation.get("signature") or "").encode("ascii"),
            validate=True,
        )
        public_key.verify(
            signature,
            _canonical_message(normalized_kind, payload),
        )
        return True
    except (
        InvalidSignature,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return False
