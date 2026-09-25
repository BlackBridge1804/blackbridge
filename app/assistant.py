"""
The in-app help assistant / walkthrough chatbot.

Same boundary as everywhere else in this codebase: the assistant NEVER
decides what's a violation and never invents facts about a client's report
-- it only explains, in plain language, whatever the deterministic rules
engine, the letter generator, and the recommendations engine already
computed. All of that gets handed to it as grounding context (see
build_context() in app/routers/assistant.py); the system prompt below is
what keeps it from wandering outside that, and outside the same legal
guardrails the rest of the product follows.

Without ANTHROPIC_API_KEY configured, get_assistant_reply() falls back to a
small built-in FAQ so the widget still does something useful -- consistent
with this codebase's "runs with zero API keys configured" philosophy.
"""
import json
import re

SYSTEM_PROMPT = """You are the in-app help assistant for BlackBridge, a credit-report dispute \
platform. You help people understand how the product works and what's happening with their own \
account. Follow these rules without exception:

1. You are not a lawyer and this is not legal advice. Never state a legal conclusion about a \
person's specific situation. If asked something that needs real legal advice, say so plainly and \
suggest a consumer-finance attorney.
2. Never state or imply a specific numeric credit score prediction or point change (e.g. "your \
score will go up 50 points"). You may describe qualitative, factor-based impact ONLY if it is \
directly present in the recommendations data given to you below -- never invent a number.
3. Never describe UCC codes, "strawman"/"redemption" theories, or an FCRA §609 letter as a way to \
delete or remove items from a credit report -- none of that has a legal basis. If asked about \
these, say plainly that they don't work and explain what actually does: FCRA §611 accuracy \
disputes, §605B identity theft blocks, and Metro 2 field-level accuracy challenges.
4. Never invent a violation, finding, account, or letter that is not present in the CONTEXT JSON \
given to you. If someone asks about something not in their data, say you don't see that and \
suggest what to do next (upload a report, rescan, or unlock the full breakdown).
5. Never promise or guarantee an outcome. Results depend entirely on how the furnisher/bureau \
responds and are never guaranteed -- say so if someone asks "will this work."
6. Keep answers short, plain-English, and specific to what's actually in the context. Use the \
person's own data when you have it rather than generic advice.

CONTEXT (this person's own account/report data, or null if none is available yet):
"""

# A tiny, keyword-matched fallback so the chat widget still works with zero
# configuration -- mirrors extract_from_text()'s role for parsing: not the
# real thing, just enough to demo the product end to end without any API key.
_FAQ = [
    (("upload", "pdf", "file"), "Upload a credit report from the dashboard -- a real PDF export, the demo .txt sample, or basically any other file works. We read it and pull out every account automatically."),
    (("free", "cost", "price", "pay", "paywall", "unlock"), "Uploading and scanning is free -- you'll see how many accounts and findings we detected. Unlocking the full breakdown (every finding's legal basis, ready-to-mail dispute letters, and the full action plan) is a one-time fee per report."),
    (("605b", "identity theft", "id theft"), "If an account isn't yours, an FCRA §605B identity theft block forces the bureau to block it within 4 business days -- much faster than a normal dispute. You'll need your own report number from identitytheft.gov; the platform never guesses at this for you."),
    (("mov", "method of verification", "verified"), "If a bureau reports a dispute back as 'verified,' you can escalate to a Method of Verification request under FCRA §611(a)(7), which asks exactly how they verified it. Look for the 'Escalate to Method of Verification' button on that letter."),
    (("outcome", "response", "result"), "After you mail a letter, record what the bureau/furnisher actually says (deleted, updated, verified, or no response) using the outcome form under that letter. That's what powers your own progress view and, in aggregate, the platform's real success-rate numbers."),
    (("609",), "A §609 letter only requests your file be disclosed to you -- it isn't a deletion tool, despite what you may have read online. The dispute mechanism that actually works is FCRA §611."),
    (("ucc",), "UCC filings and 'strawman'/'redemption' theories aren't a real way to remove items from a credit report -- they have no legal basis. What actually works is disputing inaccurate or unverifiable information under FCRA §611, or a §605B block for identity theft."),
    (("score", "points"), "We never predict a specific score number or point change -- no one outside the bureaus' own models can do that honestly. What you'll see instead is which FICO factor (payment history, utilization, etc.) an item affects and roughly how much that factor typically weighs."),
    (("secondary", "specialty bureau"), "Beyond the big three bureaus, about 10 smaller specialty/secondary agencies (like LexisNexis or Innovis) can carry their own records. The secondary bureau sweep generates a dispute letter to each of them at once."),
]

_GENERIC_FALLBACK = (
    "I can answer that with full AI assistance once ANTHROPIC_API_KEY is configured on this "
    "deployment. In the meantime, try asking about uploading, the paywall, identity theft blocks, "
    "Method of Verification, or recording outcomes."
)


def _faq_fallback(message: str) -> str:
    lower = message.lower()
    for keywords, answer in _FAQ:
        # Word-boundary match, not substring -- otherwise short keywords like
        # "mov" false-positive inside ordinary words (e.g. "remove").
        if any(re.search(r"\b" + re.escape(kw) + r"\b", lower) for kw in keywords):
            return answer
    return _GENERIC_FALLBACK


def get_assistant_reply(message: str, history: list, context: dict, anthropic_api_key: str, model: str = "claude-sonnet-5") -> str:
    """history: list of {"role": "user"|"assistant", "content": str}, most recent last.
    context: whatever build_context() in routers/assistant.py assembled -- may be None."""
    if not anthropic_api_key:
        return _faq_fallback(message)

    from anthropic import Anthropic

    client = Anthropic(api_key=anthropic_api_key)
    system = SYSTEM_PROMPT + json.dumps(context, default=str)

    messages = list(history) + [{"role": "user", "content": message}]

    try:
        response = client.messages.create(
            model=model,
            max_tokens=600,
            system=system,
            messages=messages,
        )
    except Exception:
        # Never let a flaky upstream call break the chat widget -- fall back
        # to the FAQ rather than surfacing a raw API error to the user.
        return _faq_fallback(message)

    reply = "".join(block.text for block in response.content if hasattr(block, "text")).strip()
    return reply or _faq_fallback(message)
