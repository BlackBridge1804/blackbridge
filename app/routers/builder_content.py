"""
Credit Builder add-on: the 90-day playbook, the plain-language agreement, and the
list of helper sites. Content only, no database.

This is for REAL people with a thin or empty credit file, using their OWN real
Social Security number or ITIN. Nothing here (or anywhere in the app) supports a
"CPN", a "new credit identity", or any number that is not the client's own.

No result is promised. Reviewed and approved by the owner's attorney, 2026-11. Not legal advice.
"""

AGREEMENT_VERSION = "2026-11-04-final-1"

TIERS = ("guided", "coaching")
TRACKS = ("no_file", "thin", "stale")

TRACK_LABELS = {
    "no_file": "No credit file yet",
    "thin": "Thin file (one or two accounts)",
    "stale": "Old file with nothing active",
}

TIER_LABELS = {
    "guided": "Guided plan",
    "coaching": "Done-with-you coaching",
}

TIER_SUMMARY = {
    "guided": (
        "A step-by-step 90-day roadmap with dated checklists and check-ins at 30, 60 and 90 days. "
        "You do each step yourself and tick it off here."
    ),
    "coaching": (
        "Everything in the guided plan, plus a coach who reviews your reports, helps you choose "
        "your starter products, checks your progress at each check-in, and answers your questions. "
        "You still apply for every product yourself."
    ),
}

# Fraction of the price, in whole percent, that becomes payable when each step has been completed.
MILESTONES = {
    "guided": [
        ("plan", "Your 90-day plan and checklist delivered", 50),
        ("checkin_90", "90-day check-in completed", 50),
    ],
    "coaching": [
        ("plan", "Your 90-day plan and checklist delivered, and your coach assigned", 50),
        ("checkin_30", "30-day check-in completed", 20),
        ("checkin_60", "60-day check-in completed", 15),
        ("checkin_90", "90-day check-in completed", 15),
    ],
}

CHECKIN_DAYS = (30, 60, 90)

# (key, day, who, title, detail, optional, tiers)  -- who: "client" or "coach"
_COMMON = [
    ("pull_reports", 0, "client", "Pull your free reports from all three bureaus",
     "Go to annualcreditreport.com (the official free site). Download your Equifax, Experian and TransUnion reports "
     "and upload them in the Upload section. If a bureau says it has no file for you, that is useful information. "
     "Upload that result too.", False, ("guided", "coaching")),
    ("check_identity", 0, "client", "Make sure every account on the reports is really yours",
     "If you see an account you did not open, stop and tell us before building anything. A file that looks empty "
     "or strange can mean someone else used your information. We would switch you to the identity-theft steps first.",
     False, ("guided", "coaching")),
    ("coach_review", 1, "coach", "Coach reviews your reports and confirms your starting point",
     "Your coach looks at what each bureau shows and confirms the plan fits your situation.",
     False, ("coaching",)),
    ("bank", 2, "client", "Have a checking account in good standing",
     "Most starter products need one. If you do not have one, open one in your own name first.",
     False, ("guided", "coaching")),
    ("docs", 2, "client", "Gather your documents",
     "Photo ID, proof of address, proof of income, and your own real Social Security number or ITIN. "
     "Only ever use your own number. We never ask for it and never store it.",
     False, ("guided", "coaching")),
    ("funds", 2, "client", "Set aside your starter funds",
     "Starter products usually need money of your own: a secured card deposit, credit-builder loan payments, or small "
     "monthly fees. Keep it in your own bank account until you apply. You pay it directly to the lender or bank, in "
     "your own name. We never hold or collect it.", False, ("guided", "coaching")),
    ("pick_product", 3, "client", "Choose ONE starter product",
     "Pick one secured card or one credit-builder loan. Before you apply, check that it reports to all three "
     "bureaus, that fees are clear, that there is no hard-sounding catch, and that your deposit is held safely. "
     "Credit unions and community banks are often a good place to look. See the tools list below.",
     False, ("guided", "coaching")),
    ("coach_shortlist", 4, "coach", "Coach helps you compare your shortlist",
     "Your coach goes through fees, reporting and fit with you. The decision and the application are yours.",
     False, ("coaching",)),
    ("apply", 7, "client", "Apply for that one product yourself",
     "Apply one at a time. Many applications close together each leave a hard inquiry and can hurt a new file. "
     "Apply directly with the lender, using your own details.",
     False, ("guided", "coaching")),
    ("autopay", 10, "client", "Set up autopay and keep usage low",
     "Turn on autopay for the full statement balance (or the loan payment). Keep card usage under about 10% of "
     "the limit. Never pay late.", False, ("guided", "coaching")),
    ("au", 14, "client", "Optional: ask a trusted family member about being added as an authorized user",
     "Only with their written okay, and only on an old account with on-time history and low balances that reports "
     "to all three bureaus. Ask the issuer how it reports authorized users. If the account ever goes bad, ask to "
     "be removed. Do not pay anyone to place you on their account.", True, ("guided", "coaching")),
    ("rent", 14, "client", "Optional: report your rent or utilities",
     "If you pay rent or utilities on time, ask whether a reporting service your landlord or utility supports "
     "will report it to all three bureaus.", True, ("guided", "coaching")),
    ("second_product", 45, "client", "Add a second kind of account, only after the first one shows on your reports",
     "A secured card and a credit-builder loan together give you two kinds of accounts. Do not apply for the "
     "second until the first appears on at least one report.", True, ("guided", "coaching")),
    ("graduate", 90, "client", "Review where you are and plan what comes next",
     "Pull fresh reports, see what changed, and decide next steps. If you find errors, ask about our credit "
     "repair service.", False, ("guided", "coaching")),
]

_STALE_EXTRA = [
    ("review_old", 1, "client", "List your old accounts",
     "Write down each old account on your report, its status and its date. Keep your oldest account open if you "
     "still have it. If something looks wrong, ask us about our dispute service. Do not close old accounts.",
     False, ("guided", "coaching")),
]

_NO_FILE_EXTRA = [
    ("first_account_note", 5, "client", "Know what to expect",
     "A brand new file usually needs a few months of on-time reporting before a score appears. That is normal. "
     "Do not apply for extra products to speed it up.", False, ("guided", "coaching")),
]


def build_tasks(track: str, tier: str) -> list:
    """The task list for a plan. Returns dicts ready to store."""
    rows = list(_COMMON)
    if track == "stale":
        rows += _STALE_EXTRA
    if track == "no_file":
        rows += _NO_FILE_EXTRA
    out = []
    for (key, day, who, title, detail, optional, tiers) in rows:
        if tier not in tiers:
            continue
        out.append({"key": key, "day": day, "who": who, "title": title, "detail": detail, "optional": optional})
    for d in CHECKIN_DAYS:
        out.append({
            "key": f"checkin_{d}", "day": d, "who": "client",
            "title": f"{d}-day check-in",
            "detail": ("Upload fresh reports, confirm each account is reporting, and tell us how it is going. "
                       "Press 'Complete check-in' when you are done."),
            "optional": False,
        })
    out.sort(key=lambda t: (t["day"], t["key"]))
    return out


def agreement_text(business_name: str, tier: str, price_text: str, milestones: list, funds_text: str = "") -> str:
    ms = "; ".join(f'{m["label"]} ({m["amount_text"]})' for m in milestones)
    return (
        f"Credit Builder agreement with {business_name}.\n\n"
        f"What you are buying: the {TIER_LABELS.get(tier, tier)} for a total of {price_text}. "
        f"{TIER_SUMMARY.get(tier, '')}\n\n"
        + (f"Your starter funds: plan on about {funds_text} of your own money for account deposits and fees. You pay it directly to "
           f"the bank or lender, in your own name. {business_name} never holds, collects or spends your starter funds, and does not "
           "buy or pay for accounts or authorized-user spots for you.\n\n" if funds_text else "")
        + "What this is not: we do not guarantee any score, any approval, or any result. Building credit takes time "
        "and depends on what you do and on what lenders and bureaus decide. We do not give legal advice. "
        "We never ask for or store your Social Security number. You apply for every product yourself, using only "
        "your own real Social Security number or ITIN.\n\n"
        f"When you pay: nothing is due until the matching step is finished, and the first payment is not collected until your 3 business days to cancel (below) have passed. {ms}.\n\n"
        "Your right to cancel: you may cancel this agreement without any penalty or obligation within 3 business "
        "days after you accept it, and you will get back anything you paid. You may also cancel at any time "
        "afterwards; steps not yet completed will not be charged. Saturdays and Sundays are not counted as "
        "business days.\n\n"
        "You have the right to dispute inaccurate information in your credit report yourself, directly with the "
        "credit bureau, for free."
    )


# Helper sites (official or stable). Specific products come from the Partner offers you control.
SITES = [
    {"name": "AnnualCreditReport.com", "url": "https://www.annualcreditreport.com",
     "why": "The only official source for your free reports from all three bureaus."},
    {"name": "CFPB: credit reports and scores", "url": "https://www.consumerfinance.gov/consumer-tools/credit-reports-and-scores/",
     "why": "Plain-language government guidance, and where to complain about a bureau or lender."},
    {"name": "IdentityTheft.gov", "url": "https://www.identitytheft.gov",
     "why": "If an account on your file is not yours, start here."},
    {"name": "MyCreditUnion.gov", "url": "https://www.mycreditunion.gov",
     "why": "Find a federally insured credit union. Many offer secured cards and credit-builder loans."},
]

# What to look for, by product type. No brand names on purpose: products and fees change.
PRODUCT_GUIDE = [
    {"category": "secured_card", "name": "Secured credit card",
     "what": "You put down a refundable deposit that becomes your limit. You use it lightly and pay it off.",
     "check": ["Reports to all three bureaus", "No application fee that is not refunded", "Deposit held at a real bank",
               "Can move up to a regular card later"]},
    {"category": "credit_builder_loan", "name": "Credit-builder loan",
     "what": "The lender holds the loan amount in a locked account while you make monthly payments, then releases it.",
     "check": ["Reports to all three bureaus", "Total fees and interest shown up front", "No required fee you cannot see",
               "Payment amount you can always afford"]},
    {"category": "rent_reporting", "name": "Rent or utility reporting",
     "what": "Your on-time rent or bills are reported so they can appear on your file.",
     "check": ["Reports to all three bureaus", "Monthly fee is small and clear", "Your landlord or utility supports it"]},
]
