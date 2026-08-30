"""Text normalisation helpers.

Everything here has to survive Turkish, because the deal flow is half Turkish
and half English. The dotted/dotless i is the usual trap: "İ".lower() in Python
produces "i" plus a combining dot, so naive lowercasing breaks comparisons.
We translate the Turkish letters explicitly before folding.
"""

from __future__ import annotations

import re
import unicodedata

# Applied before .lower(), so both cases of every Turkish letter land on ASCII.
_TR_FOLD = str.maketrans(
    {
        "İ": "i", "I": "i", "ı": "i", "i": "i",
        "Ş": "s", "ş": "s",
        "Ğ": "g", "ğ": "g",
        "Ü": "u", "ü": "u",
        "Ö": "o", "ö": "o",
        "Ç": "c", "ç": "c",
        "Â": "a", "â": "a",
        "Î": "i", "î": "i",
        "Û": "u", "û": "u",
    }
)

# Company-name noise. Stripped only when comparing two names for duplicates,
# never from the name we actually write to Monday.
_LEGAL_SUFFIXES = {
    "as", "a s", "anonim", "sirketi", "sti", "ltd", "limited", "sirket",
    "inc", "llc", "corp", "corporation", "co", "company", "gmbh", "ug",
    "bv", "nv", "sa", "sas", "srl", "spa", "oy", "ab", "aps", "plc",
    "holding", "ventures", "labs", "teknoloji", "teknolojileri", "yazilim",
}

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def fold(text: str) -> str:
    """Casefold to bare ASCII-ish letters. Turkish-safe.

    Used for every case-insensitive comparison in the project: command lookup,
    sender allowlists, duplicate detection.
    """
    if not text:
        return ""
    folded = text.translate(_TR_FOLD).lower()
    # Strip any remaining diacritics (accented Latin from European founders).
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_company(name: str) -> str:
    """Reduce a company name to a comparison key.

    "ACME?" / "Acme" / "acme a.ş." all collapse to "acme", so we post a
    second forward as an update on the existing deal instead of creating a
    duplicate row.
    """
    if not name:
        return ""
    # Collapse single-letter abbreviations before stripping punctuation, so the
    # Turkish "A.Ş." survives as one token ("as") and is recognised as a legal
    # suffix rather than splintering into "a" and "s".
    name = re.sub(r"\b([A-Za-zÇĞİÖŞÜçğıöşü])\.", r"\1", name)
    key = _PUNCT_RE.sub(" ", fold(name))
    words = [w for w in _WS_RE.split(key) if w]
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def collapse_ws(text: str) -> str:
    """Collapse runs of whitespace, preserving paragraph breaks."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Reply/forward markers in every language this inbox actually sees:
# English, Turkish (Outlook "YNT/İLT" and Apple), German ("AW"/"WG" — lumen
# replied with "AW:"), plus the "FW-" form Outlook produces when a colon is
# already present in a nested subject.
SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:\[[^\]]{1,20}\]\s*)?"
    r"(?:fwd?|re|aw|wg|ynt|ilt|sv|vs|tr|enc|rv)\s*[:\-–]\s*",
    re.IGNORECASE,
)


def strip_subject_prefixes(subject: str) -> str:
    """Peel every layer of FW:/RE:/YNT:/AW: off a subject line.

    Real example from the inbox: "FW: FW- Growth investment – Northwind Co"
    becomes "Growth investment – Northwind Co".
    """
    if not subject:
        return ""
    text = subject.strip()
    for _ in range(10):  # nested forwards; bounded so a weird subject can't spin
        stripped = SUBJECT_PREFIX_RE.sub("", text, count=1)
        stripped = stripped.strip()
        if stripped == text:
            break
        text = stripped
    return text


def titlecase_name(name: str) -> str:
    """Title-case a company name guessed from a domain, leaving real names alone.

    Only used for domain-derived guesses ("lumen" -> "Lumen"). Names that
    already carry capitals ("ACME", "LarkSpur") are returned untouched.
    """
    if not name:
        return ""
    if any(ch.isupper() for ch in name):
        return name
    return name[:1].upper() + name[1:]
