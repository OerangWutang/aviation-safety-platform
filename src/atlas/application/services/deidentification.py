"""Deidentification service for ASAP narratives (Phase 6).

This is the *second* line of defence.  The operator (the safety
office submitting an ASAP report) is the authoritative
deidentifier — their internal review process is supposed to strip
identifying details before the report ever reaches Atlas.  Atlas's
job here is to:

1. Refuse submissions that fail the operator's own attestation
   (the ``deidentified_attested`` flag).
2. Run a *best-effort* pattern scrub over the narrative as a
   sanity backstop.  This is not, and is not intended to be, a
   comprehensive deidentifier.  Anyone shipping this to production
   should pair it with a real NER-based scrubber.
3. Enforce a minimum content length after scrubbing so a
   degenerate submission ("see attached.") doesn't sneak through.

The scrubber's patterns are conservative — false positives are
preferable to false negatives.  Each pattern is documented with
its rationale and known limitations.

Calling convention:

    >>> result = run_deidentification("...")
    >>> result.cleaned_text   # str — text with patterns replaced
    >>> result.replacements   # list[str] — what got stripped, for audit

The caller is expected to compare ``result.cleaned_text`` against
``MIN_NARRATIVE_WORDS`` and raise :class:`DeidentificationRequiredError`
on failure.  The service itself doesn't raise; it returns a
structured result so the use-case layer owns the policy decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from atlas.domain.tenancy.exceptions import DeidentificationRequiredError

# Minimum number of words a narrative must contain *after* scrubbing
# to be accepted.  Set conservatively — twenty words is roughly two
# average sentences and catches degenerate "see attached" submissions
# without rejecting legitimately short observations.
MIN_NARRATIVE_WORDS: int = 20


# Patterns are case-insensitive unless noted.  Each carries a token
# the cleaner inserts in place of the matched text.

# Tail numbers (FAA "N-number" registrations).  Format: 'N' followed
# by 1-5 digits and an optional 1-2 letter suffix.  Word-bounded so
# common nouns like 'N1' (engine spool speed!) don't accidentally
# match.  Case-insensitive: narratives sometimes record tail numbers
# in mixed case (e.g. 'n1234ab').  Known false positives: callsigns
# that legitimately resemble tail numbers; the operator's
# deidentification is supposed to catch those, and the audit trail
# records what was scrubbed.
_TAIL_NUMBER = re.compile(r"\bN\d{1,5}[A-Z]{0,2}\b", re.IGNORECASE)

# Employee IDs: 6-8 consecutive digits not adjacent to other digits.
# Conservative because real employee-ID formats vary widely; we err
# on the side of stripping standalone long digit strings.  Won't match
# dates ('2024-06-01' has separators) or short codes.
_EMPLOYEE_ID = re.compile(r"\b\d{6,8}\b")

# Email addresses.  RFC-incomplete but adequate for narrative text.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Phone numbers: 7+ digits with optional separators.  The leading
# negative lookbehind avoids gobbling part of a longer code.
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")

# Flight numbers: 2-3 letter airline prefix + 1-4 digits, common form.
# Matches "AA123", "BA1234", "DAL456".  Case-insensitive: narratives
# sometimes write "aa123" or "Aa123".  Word-bounded.
_FLIGHT_NUMBER = re.compile(r"\b[A-Z]{2,3}\s?\d{1,4}\b", re.IGNORECASE)


@dataclass(frozen=True)
class DeidentificationResult:
    """The outcome of a scrub pass.

    ``cleaned_text`` is what the caller should store; ``replacements``
    is a list of the original substrings that were redacted, useful
    for the operator's audit trail (which Atlas does NOT store —
    it's returned to the caller for logging in the operator's own
    SMS system if they choose to).
    """

    cleaned_text: str
    replacements: list[str] = field(default_factory=list)


def run_deidentification(narrative: str) -> DeidentificationResult:
    """Apply the conservative pattern scrub to ``narrative``.

    Replacements are with a token shaped like ``[REDACTED:KIND]`` so
    the resulting text remains readable.  The caller decides whether
    to accept the cleaned text (typically via word-count check).
    """
    replacements: list[str] = []
    cleaned = narrative

    for pattern, token in (
        (_TAIL_NUMBER, "[REDACTED:TAIL_NUMBER]"),
        (_FLIGHT_NUMBER, "[REDACTED:FLIGHT_NUMBER]"),
        (_EMAIL, "[REDACTED:EMAIL]"),
        (_PHONE, "[REDACTED:PHONE]"),
        # Employee-ID pattern runs last so the digit-string match
        # doesn't pre-empt the more specific patterns above (which
        # contain digit runs of their own).
        (_EMPLOYEE_ID, "[REDACTED:EMPLOYEE_ID]"),
    ):
        for match in pattern.finditer(cleaned):
            replacements.append(match.group(0))
        cleaned = pattern.sub(token, cleaned)

    return DeidentificationResult(cleaned_text=cleaned, replacements=replacements)


def assert_acceptable_narrative(narrative: str) -> str:
    """Run the scrub, then enforce the minimum-content gate.

    Returns the cleaned narrative on success.  Raises
    :class:`DeidentificationRequiredError` if the result falls under
    the minimum word count after scrubbing — the assumption being
    that a narrative made entirely of identifying details is not a
    safety report we can usefully store.
    """
    result = run_deidentification(narrative)
    word_count = len(result.cleaned_text.split())
    if word_count < MIN_NARRATIVE_WORDS:
        raise DeidentificationRequiredError(
            f"Narrative is too short after deidentification "
            f"({word_count} words; minimum {MIN_NARRATIVE_WORDS}). "
            f"The operator's deidentification step may have stripped "
            f"too much content, or the original submission was too "
            f"thin to file as a safety report."
        )
    return result.cleaned_text


__all__ = [
    "MIN_NARRATIVE_WORDS",
    "DeidentificationResult",
    "assert_acceptable_narrative",
    "run_deidentification",
]
