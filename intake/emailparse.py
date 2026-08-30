"""Turning a forwarded email into something a CRM can use.

The hard part is that a forward is a stack of emails, and the one we care about
(the founder's) is at the bottom. Every mail client marks the boundaries
differently, and this inbox sees at least four dialects:

    Outlook EN     From: / Sent: / To: / Cc: / Subject:
    Outlook TR     Gönderen: / Gönderildi: / Kime: / Bilgi: / Konu:
    Apple Mail     Begin forwarded message:  /  İleti başlangıcı:
    Gmail          ---------- Forwarded message ---------

So we split the chain into segments, and read the innermost one for the
founder's identity while keeping the whole thing for the update body.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field

from .textnorm import collapse_ws, fold, strip_subject_prefixes, titlecase_name

# Lines that announce "a forwarded message starts here", folded to ASCII so the
# Turkish ones match regardless of how the client cased them.
_FORWARD_MARKERS = (
    "forwarded message",
    "begin forwarded message",
    "yonlendirilmis ileti",
    "ileti baslangici",
    "original message",
    "ozgun ileti",
    "iletilen mesaj",
)

_HEADER_KEYS = {
    "from": "from", "gonderen": "from", "kimden": "from",
    "sent": "date", "date": "date", "gonderildi": "date", "tarih": "date",
    "to": "to", "kime": "to",
    "cc": "cc", "bilgi": "cc",
    "subject": "subject", "konu": "subject",
    "replyto": "reply_to", "reply-to": "reply_to",
}

_HEADER_LINE_RE = re.compile(
    r"^\s*(?P<key>[A-Za-zÇĞİÖŞÜçğıöşü_-]{2,12})\s*:\s*(?P<value>.*)$"
)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*[\w]")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+|(?<![\w@.])www\.[^\s<>\"')\]]+")
_CID_RE = re.compile(r"\[cid:[^\]]+\]")
_QUOTE_RE = re.compile(r"^\s*>+ ?", re.M)

# Free-mail hosts: a founder writing from icloud.com tells us nothing about the
# company name, so we don't guess from the domain in that case.
FREEMAIL = {
    "gmail.com", "googlemail.com", "icloud.com", "me.com", "mac.com",
    "hotmail.com", "outlook.com", "live.com", "msn.com", "yahoo.com",
    "yandex.com", "yandex.com.tr", "protonmail.com", "proton.me", "aol.com",
    "mail.ru", "gmx.de", "web.de", "windowslive.com",
}

# Our own and infrastructure domains — never a deal.
INTERNAL_DOMAINS = {
    "farklabs.com", "farkholding.com", "fplus.ventures", "fark.vc",
}
_NOISE_URL_HOSTS = (
    "teams.microsoft.com", "meet.google.com", "zoom.us", "aka.ms",
    "microsoft.com", "apple.com", "w3.org", "schema.org", "linkedin.com",
    "calendly.com", "docs.google.com", "drive.google.com", "monday.com",
)

# Words that mean the text is a sentence, not a company name. A subject like
# "got a startup named hero - thanks" must never become an item; the script
# bounces it back and asks for the name instead of inventing one.
_PROSE_WORDS = {
    # English
    "a", "an", "the", "and", "or", "but", "got", "get", "have", "has", "had",
    "named", "call", "called", "this", "that", "these", "those", "is", "are",
    "was", "were", "be", "been", "for", "with", "from", "about", "please",
    "thanks", "thank", "regarding", "fyi", "hi", "hello", "dear", "we", "i",
    "you", "our", "your", "my", "can", "could", "would", "should", "will",
    "just", "some", "here", "there", "look", "check", "see", "sent", "sending",
    "attached", "below", "above", "guys", "team", "maybe", "think", "let",
    # Turkish
    "bir", "bi", "bu", "su", "var", "yok", "ve", "veya", "ile", "icin",
    "hakkinda", "merhaba", "selam", "tesekkur", "tesekkurler", "rica", "lutfen",
    "sana", "bana", "bize", "size", "benim", "senin", "bizim", "sizin",
    "gelen", "geldi", "adinda", "isminde", "diye", "sanirim", "bence",
    "bakar", "bakabilir", "misin", "misiniz", "musun", "musunuz", "miyiz",
    "gonderdim", "iletiyorum", "paylasiyorum", "ekliyorum", "bakalim",
    "olabilir", "galiba", "sanki", "gibi", "kadar", "ama", "fakat", "ancak",
    "arkadaslar", "hocam", "abi", "bunu", "sunu", "onu", "birsey", "seyler",
}

# Business vocabulary that describes a deal rather than naming a company. A
# candidate made only of these words is a description, not a name.
_GENERIC_WORDS = {
    "growth", "investment", "investments", "investor", "opportunity",
    "opportunities", "venture", "ventures", "travel", "mobility", "technology",
    "tech", "platform", "solution", "solutions", "application", "meeting",
    "partnership", "collaboration", "proposal", "introduction", "deck",
    "pitch", "presentation", "update", "request", "inquiry", "information",
    "business", "company", "startup", "project", "program", "fund", "funding",
    "round", "seed", "series", "ai", "saas", "report", "summary", "review",
    "discussion", "call", "global", "new", "digital", "innovation",
    "yatirim", "yatirimi", "firsat", "girisim", "teknoloji", "cozum",
    "basvuru", "toplanti", "isbirligi", "teklif", "tanitim", "sunum", "rapor",
    "ozet", "gorusme", "fon", "tur", "yeni", "kuresel", "hakkinda", "onerisi",
}

# Stripped from either end of a candidate: descriptive, not part of the name.
_EDGE_NOISE = {
    "yatirim", "yatirimi", "investment", "sunum", "sunumu", "deck", "pitch",
    "basvuru", "basvurusu", "application", "intro", "introduction", "fon",
    "fund", "gorusme", "gorusmesi", "toplanti", "toplantisi", "meeting",
    "hakkinda", "hk", "bilgi", "bilgisi", "update", "guncelleme", "raporu",
    "rapor", "one", "pager", "onepager", "teklif", "teklifi",
}

# A subject that is only one of these carries no company name.
_GENERIC_SUBJECTS = {
    "application", "basvuru", "intro", "introduction", "hello", "merhaba",
    "meeting", "toplanti", "invitation", "davet", "deck", "pitch", "sunum",
    "fyi", "bilgi", "plan", "update", "guncelleme", "cv", "partnership",
    "isbirligi", "collaboration", "investment", "yatirim", "gorusme",
    "sunum talebi", "bilgilendirme", "tanisma", "no subject", "",
}

# Leading noise that precedes the real name: "Application - ACME".
_LEAD_NOISE_RE = re.compile(
    r"^(?:application|basvuru|başvuru|intro|introduction|invitation|davet|"
    r"tanisma|tanışma|application form|form)\s*[-–—:]\s*",
    re.IGNORECASE,
)

_DASH_SPLIT_RE = re.compile(r"\s+[–—]\s+|\s+-\s+|\s*\|\s*")

DOCUMENT_EXTENSIONS = {
    ".pdf", ".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx", ".csv",
    ".key", ".numbers", ".pages", ".zip", ".txt", ".md",
}


@dataclass
class Segment:
    """One hop in a forward chain."""

    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""

    @property
    def sender_email(self) -> str:
        match = EMAIL_RE.search(self.headers.get("from", ""))
        return match.group(0).lower() if match else ""

    @property
    def sender_name(self) -> str:
        raw = self.headers.get("from", "")
        cleaned = EMAIL_RE.sub("", raw).strip(" <>\"'\t")
        return cleaned


@dataclass
class ParsedEmail:
    typed_text: str = ""          # what the forwarder wrote themselves
    segments: list[Segment] = field(default_factory=list)
    body_text: str = ""           # whole chain, cleaned

    @property
    def innermost(self) -> Segment | None:
        """The deepest hop that has a sender — usually the founder."""
        for segment in reversed(self.segments):
            if segment.sender_email:
                return segment
        return None

    @property
    def original_subject(self) -> str:
        for segment in reversed(self.segments):
            if segment.headers.get("subject"):
                return segment.headers["subject"]
        return ""


def html_to_text(raw: str) -> str:
    """Good-enough HTML to text.

    Gmail hands us a plaintext part for almost every message, so this is a
    fallback for the rare HTML-only mail. Deliberately dependency-free.
    """
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|table)\s*>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ").replace("‌", "")
    return collapse_ws(text)


def clean_body(plain: str = "", html: str = "") -> str:
    """Produce readable text from whichever body parts exist."""
    text = plain or html_to_text(html)
    text = _QUOTE_RE.sub("", text)          # Apple Mail's "> " forward quoting
    text = _CID_RE.sub("", text)            # [cid:image001.png@...] placeholders
    text = text.replace("￼", "")       # object-replacement char from Apple Mail
    return collapse_ws(text)


def _header_key(raw: str) -> str | None:
    return _HEADER_KEYS.get(fold(raw).replace(" ", ""))


def _is_marker(line: str) -> bool:
    folded = fold(line).strip(" -_*=:\t")
    if not folded or len(folded) > 60:
        return False
    return any(marker in folded for marker in _FORWARD_MARKERS)


def split_forward_chain(text: str) -> ParsedEmail:
    """Break a cleaned body into its forward hops."""
    parsed = ParsedEmail(body_text=text)
    lines = text.split("\n")

    segments: list[Segment] = [Segment()]
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]

        if _is_marker(line):
            segments.append(Segment())
            index += 1
            continue

        # A run of two or more header lines also opens a new hop. Outlook does
        # this with no separator line at all.
        headers: dict[str, str] = {}
        look = index
        while look < total:
            match = _HEADER_LINE_RE.match(lines[look])
            if not match:
                break
            key = _header_key(match.group("key"))
            if key is None:
                break
            headers[key] = match.group("value").strip()
            look += 1

        if len(headers) >= 2:
            if segments[-1].headers or segments[-1].body.strip():
                segments.append(Segment())
            segments[-1].headers.update(headers)
            index = look
            continue

        segments[-1].body += line + "\n"
        index += 1

    for segment in segments:
        segment.body = collapse_ws(segment.body)

    parsed.typed_text = segments[0].body if not segments[0].headers else ""
    parsed.segments = [s for s in segments if s.headers or s.body]
    return parsed


def clean_subject_for_name(subject: str) -> str:
    """Strip FW:/RE: layers and lead-in noise, leaving the useful part."""
    text = strip_subject_prefixes(subject or "")
    text = _LEAD_NOISE_RE.sub("", text).strip()
    return collapse_ws(text)


def _trim_edge_noise(candidate: str) -> str:
    """Drop descriptive words from the ends: "Northwind Co yatırım" -> "Northwind Co"."""
    words = candidate.split()
    while words and fold(words[-1]).strip(".,:;-") in _EDGE_NOISE:
        words.pop()
    while words and fold(words[0]).strip(".,:;-") in _EDGE_NOISE:
        words.pop(0)
    return " ".join(words)


def _looks_like_name(candidate: str) -> bool:
    """Is this plausibly a company name, rather than a sentence?

    Deliberately strict. A wrong item on the board is worse than an email
    asking the sender to resend with the name after the command, so anything that
    reads like prose is rejected outright.
    """
    if not candidate:
        return False

    folded = fold(candidate)
    if folded in _GENERIC_SUBJECTS or len(candidate) > 70 or len(folded) < 2:
        return False

    words = candidate.split()
    if len(words) > 5:
        return False
    if any(fold(word).strip(".,:;-!?") in _PROSE_WORDS for word in words):
        return False
    # "Investment", "Opportunity", "Yatırım Fırsatı" — business vocabulary
    # describing a deal, naming no company.
    if _all_generic(candidate):
        return False
    # Company names carry a capital somewhere; running prose in a hurried
    # forward usually does not.
    if not any(ch.isupper() for ch in candidate):
        return False
    return True


def _all_generic(text: str) -> bool:
    """True when every word is a generic business word, so it names nothing.

    "Growth investment" is a description; "Northwind Co" is a company. This is what
    lets us pick the right side of a dash.
    """
    words = [fold(w).strip(".,:;-") for w in text.split()]
    words = [w for w in words if w]
    return bool(words) and all(w in _GENERIC_WORDS for w in words)


def _dash_part(subject: str) -> str:
    """Pick the side of a dash that carries the company name.

    Real subjects put it on either side:

        "Application - ACME"                     -> tail
        "Growth investment – Northwind Co"            -> tail
        "ZENITH — Travel, Mobility and AI ..."   -> head

    So try the head first and fall back to the tail, rejecting whichever side
    is nothing but generic business words.
    """
    parts = [p.strip() for p in _DASH_SPLIT_RE.split(subject) if p.strip()]
    if len(parts) < 2:
        return ""
    for candidate in (parts[0], parts[-1]):
        if len(candidate.split()) <= 4 and _looks_like_name(candidate):
            return candidate
    return ""


def company_from_email(address: str) -> str:
    """Guess a company name from an email domain, when the domain is corporate."""
    if not address or "@" not in address:
        return ""
    domain = address.rsplit("@", 1)[1].lower().strip(">,; ")
    if domain in FREEMAIL or domain in INTERNAL_DOMAINS:
        return ""
    parts = [p for p in domain.split(".") if p not in {"www", "mail"}]
    if not parts:
        return ""
    label = parts[0]
    return titlecase_name(label) if len(label) >= 2 else ""


@dataclass
class NameGuess:
    """A resolved name plus how it was arrived at.

    `source` matters: "typed" means the sender wrote it themselves and is trusted
    without question, while a guess from the subject or a sender domain has to
    clear the prose check first. When nothing clears it, `name` is empty and
    the caller bounces the email back rather than filing a sentence as a deal.
    """

    name: str = ""
    source: str = ""          # typed | subject | domain | ""
    alternatives: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.name)


def resolve_deal_name(
    explicit: str = "",
    subject: str = "",
    parsed: ParsedEmail | None = None,
) -> NameGuess:
    """Decide what to call this item, or decline to guess.

    Order: what the sender typed after the command, the tail of the subject
    after a dash, the cleaned subject, then the sender's email domain. Every
    source except the typed one must look like a name rather than a sentence.
    """
    # A typed name is used exactly as given: no noise trimming, no prose check,
    # no reformatting. If the sender wrote it between bangs, they meant it.
    if explicit.strip():
        return NameGuess(explicit.strip(), "typed", [])

    candidates: list[tuple[str, str]] = []

    # Callers should pass the leftover text from the command parser, but strip
    # stray "!cmd" tokens defensively so a raw subject can never leak into a
    # name.
    subject = re.sub(r"!\s?[\w]{1,16}", " ", subject or "")
    cleaned = _trim_edge_noise(clean_subject_for_name(subject))

    tail = _dash_part(cleaned)
    if tail:
        candidates.append((tail, "subject"))
    if _looks_like_name(cleaned):
        candidates.append((cleaned, "subject"))

    if parsed is not None:
        inner = parsed.innermost
        if inner is not None:
            from_domain = company_from_email(inner.sender_email)
            if from_domain:
                candidates.append((from_domain, "domain"))
            original = _trim_edge_noise(clean_subject_for_name(parsed.original_subject))
            original_tail = _dash_part(original)
            if original_tail:
                candidates.append((original_tail, "subject"))
            if _looks_like_name(original):
                candidates.append((original, "subject"))

    seen: list[tuple[str, str]] = []
    for candidate, source in candidates:
        candidate = candidate.strip(" -–—:|,")
        if candidate and candidate not in [c for c, _ in seen]:
            seen.append((candidate, source))

    if not seen:
        return NameGuess()
    best, source = seen[0]
    return NameGuess(best, source, [c for c, _ in seen[1:]])


def _phones_in(text: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"(?:\+?\d[\d\s().\-]{8,18}\d)", text):
        raw = match.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        # Ten digits or more, and not a year range or a money figure.
        if 10 <= len(digits) <= 15 and raw not in found:
            found.append(re.sub(r"\s{2,}", " ", raw))
    return found


def extract_contacts(parsed: ParsedEmail) -> dict[str, object]:
    """Pull the founder's email, phone and website out of the chain.

    Scans the innermost hop before the rest of the chain, so we record the
    founder's phone number rather than the phone number in the signature of
    whichever colleague forwarded it along.
    """
    inner = parsed.innermost
    body = parsed.body_text
    inner_body = inner.body if inner is not None else ""
    ordered = [inner_body, body] if inner_body else [body]

    emails: list[str] = []
    if inner is not None and inner.sender_email:
        emails.append(inner.sender_email)
    for chunk in ordered:
        for match in EMAIL_RE.finditer(chunk):
            address = match.group(0).lower()
            domain = address.rsplit("@", 1)[-1]
            if domain in INTERNAL_DOMAINS or address in emails:
                continue
            emails.append(address)

    links: list[str] = []
    for chunk in ordered:
        for match in URL_RE.finditer(chunk):
            url = match.group(0).rstrip(".,);")
            if any(host in url for host in _NOISE_URL_HOSTS):
                continue
            if url not in links:
                links.append(url)

    phones = _phones_in(inner_body) or _phones_in(body)

    return {
        "contact_name": inner.sender_name if inner else "",
        "contact_email": emails[0] if emails else "",
        "emails": emails[:5],
        "website": links[0] if links else "",
        "links": links[:5],
        "phone": phones[0] if phones else "",
    }


_LONG_URL_RE = re.compile(r"https?://\S{60,}")


def shorten_url(url: str, keep: int = 60) -> str:
    """Cut a URL down to its host for display, keeping the domain readable.

    Marketing mail and tracking links can run 300+ characters of random
    token — unreadable in a Monday update and nothing like what a human sees
    in the original email. This is display-only; nothing that resolves a
    contact's real website goes through this.
    """
    if len(url) <= keep:
        return url
    match = re.match(r"^(https?://[^/\s]+)(/.*)?$", url)
    if not match:
        return url[:keep] + "…"
    return match.group(1) + "/…"


def shorten_long_urls(text: str) -> str:
    """Apply `shorten_url` to every long URL embedded in a block of text."""
    if not text:
        return text
    return _LONG_URL_RE.sub(lambda m: shorten_url(m.group(0)), text)


def is_inline_image(filename: str, mime_type: str, html_body: str) -> bool:
    """Signature logos masquerading as attachments.

    Outlook signatures arrive as image001.png … image004.png referenced from the
    HTML as `cid:image001.png@…`. Presence of that reference is the reliable
    test; the filename pattern is the backup for plaintext-only mail.
    """
    if not (mime_type or "").startswith("image/"):
        return False
    if filename and f"cid:{filename}" in (html_body or ""):
        return True
    return bool(re.fullmatch(r"(image|oledata|ole)\d{3,4}\.\w{2,4}", filename or "", re.I))


def choose_attachments(attachments: list[dict], html_body: str = "") -> list[dict]:
    """Keep the pitch deck, drop the signature logos."""
    keep = []
    for item in attachments or []:
        name = item.get("filename") or ""
        mime = item.get("mimeType") or ""
        if not name:
            continue
        if is_inline_image(name, mime, html_body):
            continue
        extension = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if extension in DOCUMENT_EXTENSIONS or mime.startswith(("application/", "text/")):
            keep.append(item)
        elif mime.startswith("image/"):
            keep.append(item)  # a real screenshot or product shot
    return keep
