from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr

from .models import UserRole


class OrganizationCreate(BaseModel):
    name: str
    slug: str


class OrganizationOut(BaseModel):
    id: str
    name: str
    slug: str
    is_active: bool

    class Config:
        from_attributes = True


class StaffCreate(BaseModel):
    """Platform-admin-only: issues a licensee's first login for the operator
    console. role must be org_admin or org_staff -- platform_admin can't be
    assigned this way."""

    email: EmailStr
    password: str
    role: UserRole = UserRole.org_admin


class StaffOut(BaseModel):
    id: str
    email: str
    role: UserRole
    organization_id: Optional[str] = None

    class Config:
        from_attributes = True


class ClientSignup(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    org_slug: str  # which licensee's branded instance the client is signing up under


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TradelineOut(BaseModel):
    id: str
    creditor_name: Optional[str]
    account_type: Optional[str]
    status_text: Optional[str]
    balance: Optional[int]
    is_collection: bool

    class Config:
        from_attributes = True


class ViolationOut(BaseModel):
    id: str
    tradeline_id: str
    rule_id: str
    legal_basis: str
    severity: str
    description: str
    letter_type: str

    class Config:
        from_attributes = True


class ReportSummaryOut(BaseModel):
    """What the FREE tier sees: counts and categories, no letters."""

    report_id: str
    status: str
    tradeline_count: int
    violation_count: int
    violations_by_type: dict
    is_paid: bool


class RecommendationOut(BaseModel):
    """One 'what to do next' item -- either a dispute (category='remove') or a
    credit-building move (category='add'). impact_tier is qualitative
    (High/Medium/Low), never a specific score-point prediction -- see
    app/scoring/factors.py for why."""

    suggestion_id: str
    category: str  # "remove" | "add"
    factor: str
    factor_label: str
    impact_tier: str  # "high" | "medium" | "low"
    title: str
    description: str
    legal_basis: Optional[str] = None


class RecommendationsSummaryOut(BaseModel):
    """FREE tier: counts only, no descriptions -- same paywall pattern as reports."""

    total: int
    by_impact_tier: dict
    by_category: dict
    disclaimer: str


class DisputeLetterOut(BaseModel):
    id: str
    letter_type: str
    recipient: str
    body_text: str
    generated_at: datetime
    escalates_letter_id: Optional[str] = None
    outcome_status: Optional[str] = None
    outcome_reported_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class OutcomeIn(BaseModel):
    """Reported by the client (or org staff) after the bureau/furnisher responds
    -- this is what closes the loop described in the strategy doc."""

    outcome_status: str  # "deleted" | "updated" | "verified" | "no_response"
    outcome_notes: Optional[str] = None


class IdentityTheftBlockIn(BaseModel):
    identity_theft_report_number: str  # from the consumer's FTC IdentityTheft.gov report


class LitigationCandidateOut(BaseModel):
    violation_id: str
    rule_id: str
    creditor_name: Optional[str] = None
    reason: str
    detail: str
    statutory_note: str


class ProgressStepOut(BaseModel):
    key: str
    label: str
    done: bool


class ReportProgressOut(BaseModel):
    """Deterministic pipeline status -- same shape used by the client's own
    progress view and by the operator console (app/routers/staff.py), so
    both sides of the product always agree on where a report stands."""

    report_id: str
    status: str
    created_at: datetime
    is_paid: bool
    current_stage: str
    current_stage_label: str
    steps: list[ProgressStepOut]
    tradeline_count: int
    violation_count: int
    letters_generated: int
    letters_with_outcome: int
    letters_pending_outcome: int
    litigation_candidates: Optional[int] = None


class StaffClientOut(BaseModel):
    """One row in the operator console's client list."""

    id: str
    email: str
    full_name: Optional[str] = None
    created_at: datetime
    report_count: int
    latest_report_progress: Optional[ReportProgressOut] = None


class AssistantChatIn(BaseModel):
    """report_id is optional -- general "how does this work" questions don't
    need one; questions about a specific account should include it so the
    assistant is grounded in that report's own already-computed data."""

    message: str
    report_id: Optional[str] = None
    history: list[dict] = []  # [{"role": "user"|"assistant", "content": "..."}], most recent last


class AssistantChatOut(BaseModel):
    reply: str
    grounded: bool  # true if a report_id's real data was available and used


class PartnerOfferCreate(BaseModel):
    name: str
    category: str
    description: str
    cta_label: str = "Learn more"
    affiliate_url: str
    disclosure_note: str = "Sponsored -- BlackBridge may earn a commission if you sign up through this link."
    is_active: bool = False


class PartnerOfferUpdate(BaseModel):
    """All optional -- only the fields sent get changed."""

    name: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    cta_label: Optional[str] = None
    affiliate_url: Optional[str] = None
    disclosure_note: Optional[str] = None
    is_active: Optional[bool] = None


class PartnerOfferOut(BaseModel):
    id: str
    name: str
    category: str
    description: str
    cta_label: str
    affiliate_url: str
    disclosure_note: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class OutcomeStatsOut(BaseModel):
    """Platform-admin analytics: dispute success rate, sliced by rule and by
    letter type, from real recorded outcomes -- not a guess."""

    total_letters_with_outcome: int
    success_rate: Optional[float]  # (deleted + updated) / total_with_outcome
    by_outcome_status: dict
    by_rule_id: dict
