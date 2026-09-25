"""
Renders a letter for one Violation. Every fact in the letter comes from the
Violation/Tradeline rows -- i.e. from the rules engine -- never invented at
render time. If you add an LLM polish pass later, run it only on tone/wording
and diff the facts before/after to make sure nothing was added.
"""
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(disabled_extensions=("jinja",)),
    trim_blocks=True,
    lstrip_blocks=True,
)

_TEMPLATE_BY_LETTER_TYPE = {
    "fcra_611": "fcra_611_dispute.txt.jinja",
    "fcra_623_direct": "fcra_623_direct_dispute.txt.jinja",
    "fdcpa_809": "fdcpa_809_validation.txt.jinja",
    "fcra_609": "fcra_609_disclosure.txt.jinja",
    "secondary_bureau": "secondary_bureau_dispute.txt.jinja",
    "fcra_605b": "fcra_605b_identity_theft_block.txt.jinja",
    "fcra_mov": "fcra_mov_request.txt.jinja",
}

# Default recipient for each letter type when the dispute is bureau-directed
# rather than furnisher/collector-directed. Real addresses change -- verify
# against each bureau's current dispute-mailing address before sending.
_BUREAU_RECIPIENTS = {
    "Equifax": "Equifax Information Services LLC -- confirm current dispute address before mailing",
    "Experian": "Experian -- confirm current dispute address before mailing",
    "TransUnion": "TransUnion Consumer Solutions -- confirm current dispute address before mailing",
}


def render_letter(
    *,
    letter_type: str,
    client_name: str,
    client_address: str,
    creditor_name: str,
    finding_description: str,
    recipient_name: str,
    recipient_address: str = "",
    account_number_last4: str | None = None,
    original_creditor_name: str | None = None,
    identity_theft_report_number: str | None = None,
    original_dispute_date: str | None = None,
) -> str:
    template_name = _TEMPLATE_BY_LETTER_TYPE.get(letter_type)
    if not template_name:
        raise ValueError(f"Unknown letter_type: {letter_type}")

    template = _env.get_template(template_name)
    return template.render(
        client_name=client_name,
        client_address=client_address,
        today=date.today().isoformat(),
        creditor_name=creditor_name,
        finding_description=finding_description,
        recipient_name=recipient_name,
        recipient_address=recipient_address or "(confirm current mailing address before sending)",
        account_number_last4=account_number_last4,
        original_creditor_name=original_creditor_name,
        identity_theft_report_number=identity_theft_report_number,
        original_dispute_date=original_dispute_date,
    )
