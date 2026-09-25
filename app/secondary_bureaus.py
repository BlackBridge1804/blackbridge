"""
The "big three" (Equifax, Experian, TransUnion) aren't the only consumer
reporting agencies. This list -- drawn from the CFPB's published list of
consumer reporting companies -- covers the specialty/secondary agencies most
consumers never check or dispute. Addresses are placeholders: pull current
mailing addresses from each agency's own consumer-disclosure page (they
change) before sending anything for real, and keep this list itself on a
periodic refresh cycle rather than treating it as permanent.
"""

SECONDARY_BUREAUS = [
    {
        "name": "LexisNexis Risk Solutions",
        "reports_on": "Public-records data used by lenders, insurers, healthcare, and government agencies",
        "mailing_address_placeholder": "LexisNexis Consumer Center -- confirm current address before mailing",
    },
    {
        "name": "Innovis",
        "reports_on": "General credit and identity-verification data",
        "mailing_address_placeholder": "Innovis Consumer Assistance -- confirm current address before mailing",
    },
    {
        "name": "SageStream, LLC",
        "reports_on": "Supplementary reports for auto lenders, card issuers, retailers, utilities",
        "mailing_address_placeholder": "SageStream Consumer Inquiry -- confirm current address before mailing",
    },
    {
        "name": "ChexSystems, Inc.",
        "reports_on": "Checking account applications and closures",
        "mailing_address_placeholder": "ChexSystems Consumer Relations -- confirm current address before mailing",
    },
    {
        "name": "Certegy Payment Solutions, LLC",
        "reports_on": "Check and ACH verification",
        "mailing_address_placeholder": "Certegy Consumer Services -- confirm current address before mailing",
    },
    {
        "name": "Early Warning Services, LLC",
        "reports_on": "Bank-account-related fraud detection",
        "mailing_address_placeholder": "Early Warning Services -- confirm current address before mailing",
    },
    {
        "name": "TeleCheck Services, Inc.",
        "reports_on": "Check-writing risk for merchants",
        "mailing_address_placeholder": "TeleCheck Consumer Services -- confirm current address before mailing",
    },
    {
        "name": "LexisNexis C.L.U.E.",
        "reports_on": "Up to 7 years of auto/property insurance claims history",
        "mailing_address_placeholder": "LexisNexis C.L.U.E. Consumer Center -- confirm current address before mailing",
    },
    {
        "name": "Arity",
        "reports_on": "Telematics-based driving-behavior scores",
        "mailing_address_placeholder": "Arity Consumer Inquiry -- confirm current address before mailing",
    },
    {
        "name": "MIB, Inc.",
        "reports_on": "Medical condition data for life/health insurance underwriting",
        "mailing_address_placeholder": "MIB Consumer File Disclosure -- confirm current address before mailing",
    },
]
