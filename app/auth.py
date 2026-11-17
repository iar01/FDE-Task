"""AUTH STUB. Identity = the X-User-Id request header, looked up in approvers.csv.

THIS IS NOT AUTHENTICATION: anyone can send any header. It exists so the role checks can be shown.
Production: SSO/OIDC (e.g. Entra ID) -> signed JWT; user id and roles come from verified token claims
(IdP groups), approvers.csv is reconciled against the IdP, and the API never trusts a raw header.
"""
from __future__ import annotations

from app.directory import Directory
from app.reporting import Approver

USER_HEADER = "X-User-Id"


def identify(user_id: str | None, directory: Directory) -> Approver | None:
    """STUB: header value -> approvers.csv row, or None if missing/unknown."""
    if not user_id:
        return None
    return directory.approvers.get(user_id.strip())
