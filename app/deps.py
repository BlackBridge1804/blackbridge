"""
Tenant-isolation helpers. Every query for org-scoped data should be filtered
through these rather than trusting a client-supplied organization_id.

In production (Postgres/Supabase), back this up with an actual Row-Level
Security policy on every table keyed to organization_id -- these helpers are
the application-layer half of "defense in depth," not a substitute for it.
"""
from fastapi import Depends, HTTPException, status

from .auth import get_current_principal
from .models import Client, User, UserRole


def require_platform_admin(principal=Depends(get_current_principal)):
    if not isinstance(principal, User) or principal.role != UserRole.platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform admin only")
    return principal


def require_org_staff(principal=Depends(get_current_principal)):
    if not isinstance(principal, User):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organization staff only")
    return principal


def require_client(principal=Depends(get_current_principal)):
    if not isinstance(principal, Client):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client account only")
    return principal


def assert_same_org(principal, organization_id: str):
    """Raise 403 unless the principal belongs to the given organization (or is platform admin)."""
    if isinstance(principal, User) and principal.role == UserRole.platform_admin:
        return
    if getattr(principal, "organization_id", None) != organization_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-tenant access denied")
