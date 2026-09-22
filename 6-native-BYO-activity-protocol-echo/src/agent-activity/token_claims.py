# Copyright (c) Microsoft. All rights reserved.

"""Secret-safe logging of a JWT's claims.

Decodes the token payload WITHOUT verifying the signature and logs only
non-secret claims (audience, scopes, app/upn, expiry). Never logs the raw
token. Used to prove, on the first Teams test, exactly what token was obtained
and forwarded (scope/audience) so the Starburst OBO gap is unambiguous.
"""

from __future__ import annotations

import logging
import time

import jwt

logger = logging.getLogger("agent-activity.claims")


def log_claims(label: str, token: str | None) -> None:
    if not token:
        logger.warning("[CLAIMS] %s: <no token>", label)
        return
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[CLAIMS] %s: could not decode (%s)", label, exc)
        return

    exp = claims.get("exp")
    exp_in = int(exp - time.time()) if isinstance(exp, (int, float)) else None
    logger.info(
        "[CLAIMS] %s: aud=%s scp=%s roles=%s appid=%s upn=%s exp_in=%ss",
        label,
        claims.get("aud"),
        claims.get("scp"),
        claims.get("roles"),
        claims.get("appid") or claims.get("azp"),
        claims.get("upn") or claims.get("preferred_username"),
        exp_in,
    )
