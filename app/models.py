"""
Multi-tenant data model.

Tenant hierarchy:
  Platform (this codebase/business)
    -> Organization  (a licensee company, or your own consumer-facing brand)
        -> User        (staff of that org: org_admin / org_staff)
        -> Client       (an end consumer of that org; sets their own password)
            -> Report        (one uploaded/scanned credit report)
                -> Tradeline      (one parsed account line from that report)
                    -> Violation      (a rule-engine finding on that tradeline)
                        -> DisputeLetter  (a generated letter; may escalate an earlier one, and
                                            carries an outcome once the bureau/furnisher responds)

Every row below the Organization level carries organization_id so row-level
access control can be enforced consistently (see app/deps.py). In Postgres/
Supabase, mirror this with an actual Row-Level Security policy on each table
keyed to organization_id -- don't rely on application code alone.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class UserRole(str, enum.Enum):
    platform_admin = "platform_admin"  # you: manages organizations/licensees
    org_admin = "org_admin"  # a licensee's admin user
    org_staff = "org_staff"  # a licensee's staff user


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False)
    branding_logo_url = Column(String, nullable=True)
    branding_primary_color = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="organization")
    clients = relationship("Client", back_populates="organization")


class User(Base):
    """Staff / admin accounts. NOT the end consumer -- see Client below."""

    __tablename__ = "users"

    id = Column(String, primary_key=True, default=_uuid)
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=True)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.org_staff)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    organization = relationship("Organization", back_populates="users")


class Client(Base):
    """The end consumer whose credit report is being analyzed. Sets their own password."""

    __tablename__ = "clients"

    id = Column(String, primary_key=True, default=_uuid)
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    email = Column(String, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    organization = relationship("Organization", back_populates="clients")
    reports = relationship("Report", back_populates="client")


class Report(Base):
    """One uploaded credit report (a single upload event)."""

    __tablename__ = "reports"

    id = Column(String, primary_key=True, default=_uuid)
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False)
    original_filename = Column(String, nullable=True)
    raw_text = Column(Text, nullable=True)  # extracted text, pre-parse
    status = Column(String, default="uploaded")  # uploaded -> parsed -> scanned
    # "text" = demo pipe-delimited format (app/parsing/extractor.py's regex parser
    # handles it with zero API keys); "pdf" / "other" = a real-world upload that
    # needs extract_with_llm() to turn into structured tradelines. Set at upload
    # time from the file extension/content-type -- see app/routers/reports.py.
    source_format = Column(String, default="text")
    is_paid = Column(Boolean, default=False)  # unlocks full letters for this report
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client", back_populates="reports")
    tradelines = relationship("Tradeline", back_populates="report")


class Tradeline(Base):
    """One parsed account line from a report (structured, not free text)."""

    __tablename__ = "tradelines"

    id = Column(String, primary_key=True, default=_uuid)
    report_id = Column(String, ForeignKey("reports.id"), nullable=False)

    creditor_name = Column(String, nullable=True)
    account_number_last4 = Column(String, nullable=True)
    account_type = Column(String, nullable=True)  # e.g. "collection", "revolving", "installment"
    status_text = Column(String, nullable=True)  # e.g. "paid as agreed", "charged off"
    balance = Column(Integer, nullable=True)  # cents
    credit_limit = Column(Integer, nullable=True)  # cents
    date_opened = Column(String, nullable=True)  # ISO date string, may be unknown/blank
    date_closed = Column(String, nullable=True)
    date_of_first_delinquency = Column(String, nullable=True)
    is_collection = Column(Boolean, default=False)
    original_creditor_name = Column(String, nullable=True)  # for collections
    reported_by = Column(String, nullable=True)  # which bureau this line came from

    # Identity-theft block path (FCRA 605B) -- the client self-flags "this isn't
    # mine," backed by an FTC identity theft report number. We don't try to
    # algorithmically guess identity theft; that's the client's attestation,
    # exactly as the statute requires.
    flagged_identity_theft = Column(Boolean, default=False)
    identity_theft_report_number = Column(String, nullable=True)

    report = relationship("Report", back_populates="tradelines")
    violations = relationship("Violation", back_populates="tradeline")


class Violation(Base):
    """A rule-engine finding tied to one tradeline. Always cites the rule that fired."""

    __tablename__ = "violations"

    id = Column(String, primary_key=True, default=_uuid)
    tradeline_id = Column(String, ForeignKey("tradelines.id"), nullable=False)

    rule_id = Column(String, nullable=False)  # e.g. "metro2_missing_dofd"
    legal_basis = Column(String, nullable=False)  # e.g. "FCRA 611", "Metro 2 field spec"
    severity = Column(String, default="medium")  # low / medium / high
    description = Column(Text, nullable=False)  # human-readable finding, auditable
    letter_type = Column(String, nullable=False)  # which template this maps to
    created_at = Column(DateTime, default=datetime.utcnow)

    tradeline = relationship("Tradeline", back_populates="violations")
    dispute_letters = relationship("DisputeLetter", back_populates="violation")


class DisputeLetter(Base):
    """A generated letter for one violation. The letter text itself is immutable
    once generated (audit trail) -- but its OUTCOME gets recorded after the
    fact, which is what closes the loop the strategy doc calls the single
    biggest missing piece: knowing whether disputes actually work."""

    __tablename__ = "dispute_letters"

    id = Column(String, primary_key=True, default=_uuid)
    violation_id = Column(String, ForeignKey("violations.id"), nullable=False)
    letter_type = Column(String, nullable=False)
    recipient = Column(String, nullable=False)  # e.g. "Equifax", "LexisNexis Risk Solutions"
    body_text = Column(Text, nullable=False)
    generated_at = Column(DateTime, default=datetime.utcnow)

    # Set when this letter is itself an escalation of an earlier one (e.g. a
    # Method of Verification request after the original dispute came back
    # "verified") -- lets the litigation-candidate check below walk the chain.
    escalates_letter_id = Column(String, ForeignKey("dispute_letters.id"), nullable=True)

    violation = relationship("Violation", back_populates="dispute_letters")

    # Outcome tracking -- recorded by the client (or an org's staff) after the
    # bureau/furnisher responds, normally ~30 days later.
    outcome_status = Column(String, nullable=True)  # "deleted" | "updated" | "verified" | "no_response"
    outcome_notes = Column(Text, nullable=True)
    outcome_reported_at = Column(DateTime, nullable=True)


class PartnerOffer(Base):
    """A promoted third-party product (a secured card, a credit-builder loan
    like Kovo, a rent-reporting service, etc.) shown to clients -- almost
    always an affiliate relationship. platform_admin-managed only, and every
    offer carries its own disclosure text so the frontend can render the FTC-
    required "sponsored/affiliate" labeling next to it rather than bury it.

    NOT for tradeline-selling/piggybacking offers without your own legal
    review first -- see the README and the strategy doc for why (issuers
    frequently prohibit it and it's drawn FTC/CFPB scrutiny); this table will
    happily store one, but nothing here vets what you put in it."""

    __tablename__ = "partner_offers"

    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)  # e.g. "credit_builder_loan", "secured_card", "rent_reporting", "other"
    description = Column(Text, nullable=False)
    cta_label = Column(String, default="Learn more")
    affiliate_url = Column(String, nullable=False)
    disclosure_note = Column(
        String,
        default="Sponsored -- BlackBridge may earn a commission if you sign up through this link.",
    )
    is_active = Column(Boolean, default=False)  # off by default: a real admin decision, not silently live
    created_at = Column(DateTime, default=datetime.utcnow)
