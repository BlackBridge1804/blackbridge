"""
Text of the documents a client signs before the full breakdown unlocks.

THIS IS A DRAFT FOR ATTORNEY REVIEW. It is kept in its own file on purpose:
when your attorney changes the wording, only this file needs to be replaced.
Bump VERSION whenever the wording changes. Every signature stores the exact
text that was shown, plus this version, so old signatures are never affected
by later edits.

The consumer rights statement below is the wording of 15 U.S.C. 1679c(a),
copied from the statute. Do not edit it without your attorney.
"""

VERSION = "2026-10-29-draft-1"

CONTACT_EMAIL = "BlackBridgeoffice@Gmail.com"
CONTACT_PHONE = "(669) 272-2854"
PRICE_SINGLE = "$49.99"
PRICE_ALL_THREE = "$99.00"

DISCLOSURE_TITLE = "Consumer Credit File Rights Under State and Federal Law"

DISCLOSURE_TEXT = """You have a right to dispute inaccurate information in your credit report by contacting the credit bureau directly. However, neither you nor any 'credit repair' company or credit repair organization has the right to have accurate, current, and verifiable information removed from your credit report. The credit bureau must remove accurate, negative information from your report only if it is over 7 years old. Bankruptcy information can be reported for 10 years.

You have a right to obtain a copy of your credit report from a credit bureau. You may be charged a reasonable fee. There is no fee, however, if you have been turned down for credit, employment, insurance, or a rental dwelling because of information in your credit report within the preceding 60 days. The credit bureau must provide someone to help you interpret the information in your credit file. You are entitled to receive a free copy of your credit report if you are unemployed and intend to apply for employment in the next 60 days, if you are a recipient of public welfare assistance, or if you have reason to believe that there is inaccurate information in your credit report due to fraud.

You have a right to sue a credit repair organization that violates the Credit Repair Organization Act. This law prohibits deceptive practices by credit repair organizations.

You have the right to cancel your contract with any credit repair organization for any reason within 3 business days from the date you signed it.

Credit bureaus are required to follow reasonable procedures to ensure that the information they report is accurate. However, mistakes may occur.

You may, on your own, notify a credit bureau in writing that you dispute the accuracy of information in your credit file. The credit bureau must then reinvestigate and modify or remove inaccurate or incomplete information. The credit bureau may not charge any fee for this service. Any pertinent information and copies of all documents you have concerning an error should be given to the credit bureau.

If the credit bureau's reinvestigation does not resolve the dispute to your satisfaction, you may send a brief statement to the credit bureau, to be kept in your file, explaining why you think the record is inaccurate. The credit bureau must include a summary of your statement about disputed information with any report it issues about you.

The Federal Trade Commission regulates credit bureaus and credit repair organizations. For more information contact:

The Public Reference Branch
Federal Trade Commission
Washington, D.C. 20580"""

CANCEL_STATEMENT = (
    "You may cancel this contract without penalty or obligation at any time before midnight of the 3rd "
    "business day after the date on which you signed the contract. See the attached notice of "
    "cancellation form for an explanation of this right."
)


def agreement_title():
    return "Service Agreement"


def agreement_text(provider_name, business_address, waiting_period):
    """The contract itself. `provider_name` is the business the client signed up
    under (BlackBridge LLC for the main site, or a licensee's business name)."""
    if waiting_period:
        timing = (
            "Services are delivered digitally. We will not begin the paid services until the 3-business-day "
            "cancellation period described below has ended. Your full breakdown and dispute letters will be "
            "available within 1 business day after the later of (a) the end of that cancellation period and "
            "(b) the date we confirm your payment."
        )
    else:
        timing = (
            "Services are delivered digitally. Your full breakdown and dispute letters will be available "
            "within 1 business day after we confirm your payment."
        )
    if waiting_period:
        cancel_effect = "If you cancel, we will not provide the paid services and you will owe nothing."
    else:
        cancel_effect = (
            "If you cancel, you will owe nothing and we will refund any payment you made. Because the paid "
            "services can be delivered as soon as you have signed and paid, your access to them ends when you cancel."
        )
    return f"""1. Who this agreement is between
This agreement is between you (the "client") and {provider_name} ("we" or "us"). Our principal business address is: {business_address}. You can reach us at {CONTACT_EMAIL} or {CONTACT_PHONE}.

2. The services we will provide
We provide an online software service. When you upload your own credit report, we will:
(a) Read the report and show you a free summary of how many accounts and possible reporting issues we found.
(b) After you request the full breakdown and we confirm your payment, show you each finding with the reason it may be inaccurate or incomplete.
(c) Prepare dispute letters for the findings you choose, based on your own information and mailing address.
(d) Give you an action plan and a place to record the responses you receive.
You review, sign and mail the letters yourself. We do not contact any credit bureau, creditor or collector for you, we do not give legal advice, and we are not a law firm.

3. No guarantees
We make no guarantee that any item will be removed, corrected or updated, that your credit score will change, or that any credit bureau, creditor or collector will respond in a particular way. Those decisions belong to them.

4. When the services will be complete
{timing}

5. Fees and payment
The total amount you will pay is {PRICE_SINGLE} for one credit report, or {PRICE_ALL_THREE} for the full breakdown and letters for reports from all three credit bureaus (Experian, Equifax and TransUnion). There are no other fees. The fee is for the software service and the letters we deliver. It does not depend on any result. We will tell you how to pay when you request the full breakdown.

6. Your right to cancel
You may cancel this agreement without penalty or obligation at any time before midnight of the 3rd business day after the date you sign it. To cancel, use the Cancel button in your account, or send a written notice to the address or email in section 1. A Notice of Cancellation is attached to this agreement. {cancel_effect}

7. Refunds
Our Refund Policy at https://blackbridge-credit-repair.onrender.com/static/refunds.html explains refunds after the paid services are delivered. It is part of this agreement.

8. Your responsibilities
You agree to upload only your own credit report, to give us accurate information including your mailing address, to dispute only items you believe in good faith are inaccurate, incomplete or unverifiable, and to review every letter before you send it. You may dispute any item with the credit bureaus yourself for free.

9. Our Terms and Privacy Policy
This agreement works together with our Terms of Service and Privacy Policy. If they conflict with this agreement about the services above, this agreement controls.

10. Records
We keep a copy of your signed documents for at least 2 years. You will receive a copy of this agreement and of the Consumer Credit File Rights statement when you sign."""


def cancellation_notice(provider_name, business_address, deadline_text):
    """Notice of Cancellation, using the wording of 15 U.S.C. 1679e(b)."""
    return (
        "NOTICE OF CANCELLATION\n\n"
        "You may cancel this contract, without any penalty or obligation, at any time before midnight of the "
        "3rd day which begins after the date the contract is signed by you.\n\n"
        f"To cancel this contract, mail or deliver a signed, dated copy of this cancellation notice, or any "
        f"other written notice to {provider_name} at {business_address} before midnight on {deadline_text}.\n"
        f"You can also cancel with the Cancel button in your account.\n\n"
        "I hereby cancel this transaction,\n\n"
        "Date: ______________________\n\n"
        "Signature: ______________________"
    )
