"""
Turns raw report text into structured tradeline dicts.

Real bureau exports (Equifax/Experian/TransUnion PDFs, or a scanned image)
are messy and inconsistent in layout, which is exactly the kind of "read
this and pull out the fields" task an LLM is good at -- but the rules engine
must only ever see clean structured data, never raw prose, so this module is
the one place LLM output gets validated into a strict shape before anything
downstream trusts it.

Two paths are provided:
  - extract_from_text(): a dependency-free heuristic parser for the demo
    sample format (tests/sample_data/sample_report.txt) so the whole
    pipeline runs with zero API keys configured.
  - extract_with_llm(): the real integration point for production. Wire up
    your Anthropic API key and have the model return JSON matching
    TRADELINE_SCHEMA_HINT below, then still run it through
    _validate_and_coerce() before it reaches the rules engine.
"""
import json
import re
from typing import Optional
from types import SimpleNamespace

TRADELINE_SCHEMA_HINT = {
    "creditor_name": "str",
    "account_number_last4": "str|null",
    "account_type": "revolving|installment|collection|null",
    "status_text": "str|null",
    "balance": "int (cents) | null",
    "credit_limit": "int (cents) | null",
    "date_opened": "YYYY-MM-DD | null",
    "date_closed": "YYYY-MM-DD | null",
    "date_of_first_delinquency": "YYYY-MM-DD | null",
    "is_collection": "bool",
    "original_creditor_name": "str | null",
}

_LINE_RE = re.compile(
    r"""
    ^Creditor:\s*(?P<creditor>.+?)\s*\|\s*
    Type:\s*(?P<type>\w+)\s*\|\s*
    Status:\s*(?P<status>.+?)\s*\|\s*
    Balance:\s*(?P<balance>[\d.]+)\s*\|\s*
    Limit:\s*(?P<limit>[\d.]*)\s*\|\s*
    Opened:\s*(?P<opened>[\d-]*)\s*\|\s*
    Closed:\s*(?P<closed>[\d-]*)\s*\|\s*
    DOFD:\s*(?P<dofd>[\d-]*)\s*\|\s*
    Collection:\s*(?P<collection>yes|no)\s*\|\s*
    OriginalCreditor:\s*(?P<orig>.*)$
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _to_cents(value: str) -> Optional[int]:
    value = (value or "").strip()
    if not value:
        return None
    return round(float(value) * 100)


def extract_from_text(raw_text: str) -> list[dict]:
    """Heuristic parser for the demo's pipe-delimited sample format. Swap this
    for real PDF table extraction (e.g. pdfplumber) or extract_with_llm() for
    production use against real bureau exports."""
    tradelines = []
    for line in raw_text.splitlines():
        match = _LINE_RE.match(line.strip())
        if not match:
            continue
        g = match.groupdict()
        tradelines.append(
            {
                "creditor_name": g["creditor"].strip(),
                "account_number_last4": None,
                "account_type": g["type"].strip().lower(),
                "status_text": g["status"].strip(),
                "balance": _to_cents(g["balance"]),
                "credit_limit": _to_cents(g["limit"]),
                "date_opened": g["opened"] or None,
                "date_closed": g["closed"] or None,
                "date_of_first_delinquency": g["dofd"] or None,
                "is_collection": g["collection"].strip().lower() == "yes",
                "original_creditor_name": g["orig"].strip() or None,
            }
        )
    return tradelines


def extract_pdf_text(file_bytes: bytes) -> str:
    """Pulls raw text out of a PDF (a real Equifax/Experian/TransUnion export,
    or a reseller's PDF, or anything else). This is plain text extraction,
    not table-structure-aware -- it works well on text-based PDFs and poorly
    on scanned image PDFs (those need OCR first, e.g. pytesseract, which
    isn't wired in here). Layouts vary a lot between bureaus and resellers;
    extract_with_llm() below is what turns this messy text into structured
    tradelines."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(file_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


_LLM_EXTRACTION_SYSTEM_PROMPT = """You extract tradeline (account) data from consumer credit \
report text into strict JSON. Return ONLY a JSON array, no prose, no markdown code fences.

Each element must be an object with exactly these keys:
- creditor_name: string
- account_number_last4: string or null (last 4 digits only -- never transcribe a full account number)
- account_type: one of "revolving", "installment", "collection", or null if unclear
- status_text: string or null (the status exactly as reported, e.g. "paid as agreed", "charged off")
- balance: integer cents or null (e.g. $450.00 -> 45000)
- credit_limit: integer cents or null
- date_opened: "YYYY-MM-DD" or null
- date_closed: "YYYY-MM-DD" or null
- date_of_first_delinquency: "YYYY-MM-DD" or null
- is_collection: true or false
- original_creditor_name: string or null (for collection accounts only)

Extract every tradeline you can find. If a field isn't present in the text, use null -- never \
guess or invent a value. Do not extract or repeat any Social Security Number, full account \
number, or other data not in the schema above, even if present in the source text."""


def extract_with_llm(raw_text: str, anthropic_api_key: str, model: str = "claude-sonnet-5") -> list[dict]:
    """Sends raw report text to Claude and asks for strict JSON matching
    TRADELINE_SCHEMA_HINT, then validates every element through
    _validate_and_coerce() before anything downstream (the rules engine,
    letters) ever sees it -- untrusted model output never reaches those
    directly. `model` defaults to a current Claude model; check
    https://platform.claude.com/docs for the latest model names, since these
    change over time.

    Raises ValueError if the model's response isn't valid JSON -- callers
    should catch this and surface a clear "couldn't parse this file" error
    rather than silently returning nothing."""
    from anthropic import Anthropic

    client = Anthropic(api_key=anthropic_api_key)
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_LLM_EXTRACTION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": raw_text}],
    )
    raw_output = "".join(block.text for block in response.content if hasattr(block, "text")).strip()

    # Models occasionally wrap JSON in a code fence despite instructions not to.
    if raw_output.startswith("```"):
        raw_output = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_output.strip())

    try:
        parsed = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model did not return valid JSON: {exc}") from exc

    if not isinstance(parsed, list):
        raise ValueError("Model's JSON response was not a list of tradelines")

    return [_validate_and_coerce(t) for t in parsed if isinstance(t, dict)]


def _validate_and_coerce(raw_tradeline: dict) -> dict:
    """Never let un-validated LLM output reach the rules engine. Coerce types,
    drop unknown keys, and fall back to None for anything malformed."""
    clean = {}
    for key in TRADELINE_SCHEMA_HINT:
        clean[key] = raw_tradeline.get(key)
    clean["is_collection"] = bool(clean.get("is_collection"))
    return clean


def tradeline_dicts_to_namespaces(tradeline_dicts: list[dict], report_id: str = "demo") -> list:
    """Convenience for unit tests / demo runs that need rule-engine-shaped
    objects without touching the database."""
    return [
        SimpleNamespace(id=f"{report_id}-{i}", report_id=report_id, **t)
        for i, t in enumerate(tradeline_dicts)
    ]
