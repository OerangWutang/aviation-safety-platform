from __future__ import annotations

import hashlib
import hmac

from atlas.config import get_settings


def _hmac_sha256(api_key: str, secret: str) -> str:
    return hmac.digest(secret.encode(), api_key.encode(), hashlib.sha256).hex()


def hash_api_key(api_key: str) -> str:
    """Return the storage hash for an API key.

    If API_KEY_HASH_SECRET is configured, Atlas uses HMAC-SHA256 so a stolen
    database cannot be used to cheaply enumerate API keys offline. When the
    secret is not configured, this falls back to the legacy SHA-256 format for
    local development and existing databases.
    """

    secret = get_settings().api_key_hash_secret
    if secret:
        return _hmac_sha256(api_key, secret)
    return hashlib.sha256(api_key.encode()).hexdigest()


def hash_api_key_candidates(api_key: str) -> list[str]:
    """Return accepted hash variants for auth verification.

    Order is stable and intentional:
    1. Current hash (`API_KEY_HASH_SECRET` or legacy SHA-256 fallback)
    2. Previous-secret hash (`API_KEY_HASH_SECRET_PREVIOUS`) when configured

    This enables bounded dual-secret cutovers: clients using keys hashed with
    the previous secret continue to authenticate until operators remove
    `API_KEY_HASH_SECRET_PREVIOUS`.
    """
    settings = get_settings()
    current_secret = settings.api_key_hash_secret
    previous_secret = settings.api_key_hash_secret_previous

    candidates: list[str] = []
    if current_secret:
        candidates.append(_hmac_sha256(api_key, current_secret))
    else:
        candidates.append(hashlib.sha256(api_key.encode()).hexdigest())

    if previous_secret and previous_secret != current_secret:
        candidates.append(_hmac_sha256(api_key, previous_secret))

    return candidates
