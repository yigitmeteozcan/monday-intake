"""Command grammar for subject lines (and the first lines of a forward).

Design goal, straight from the brief: a command must work wherever it lands in
the subject — start, middle or end — because the sender hits Forward and types
without thinking about position. So we do not anchor anything. We scan the
whole string for `!token`, and whatever text is left over becomes the fallback
deal name.

    "!addp Larkspur"                     -> addp, name=Larkspur
    "FW: Application - ACME !addp"         -> addp, name from subject (ACME)
    "!addp FW: Application - ACME"         -> addp, name from subject (ACME)
    "FW: intro !addp Northwind !src=investor"   -> addp, name=Northwind, source=investor

Ambiguity only arises for a bare argument buried mid-sentence, where we cannot
know where the company name ends. Two guards: the argument stops at the first
strong separator or subject prefix and is capped at a few words, and `!cmd=value`
is always available as the unambiguous form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .textnorm import SUBJECT_PREFIX_RE, fold

# A command is "!" followed (optionally after one stray space, which phone
# keyboards like to insert) by a word. Deliberately permissive: the registry
# lookup below is what decides whether a token is real, so "Merhaba!" or
# "ACME?" can never be mistaken for a command.
TOKEN_RE = re.compile(
    r"!(?P<gap>\s?)(?P<name>[A-Za-zÇĞİÖŞÜçğıöşü][A-Za-zÇĞİÖŞÜçğıöşü0-9_]{0,15})",
    re.UNICODE,
)

_SEPARATOR_RE = re.compile(r"\s*[=:]\s*")
_QUOTES = {'"': '"', "'": "'", "“": "”", "‘": "’"}

# A bare argument stops dead at any of these — they signal "the company name
# ended and the rest of the subject resumed".
_HARD_STOP_RE = re.compile(r"[|»<>\[\]]|--|–|—|»")
_MAX_BARE_ARG_WORDS = 5
_MAX_NAME_WORDS = 8
_MAX_NOTE_CHARS = 1000

NO_ARG, OPTIONAL_ARG, REQUIRED_ARG = "none", "optional", "required"


@dataclass(frozen=True)
class CommandSpec:
    name: str
    aliases: tuple[str, ...]
    kind: str  # "action" (what to do) or "modifier" (extra data)
    arg: str
    help: str
    # For shorthand modifiers such as !inbound, which is just !src=inbound.
    presets: tuple[tuple[str, str], ...] = ()


# --------------------------------------------------------------------------
# The registry. Turkish aliases are first-class: the team writes in both
# languages and nobody should have to remember which one the script speaks.
# --------------------------------------------------------------------------
COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "addp",
        ("add", "deal", "p", "ekle", "girisim", "startup", "yeni"),
        "action",
        OPTIONAL_ARG,
        "Add a startup to the deal pipeline.",
    ),
    CommandSpec(
        "addi",
        ("inv", "investor", "lead", "yatirimci", "yatirim", "addinv"),
        "action",
        OPTIONAL_ARG,
        "Add an investor lead to the investor board.",
    ),
    CommandSpec(
        "note",
        ("n", "not", "yorum", "notu"),
        "modifier",
        REQUIRED_ARG,
        "Post extra text as its own update on the item: !note çok iyi!",
    ),
)


def _build_lookup() -> dict[str, CommandSpec]:
    table: dict[str, CommandSpec] = {}
    for spec in COMMANDS:
        for key in (spec.name, *spec.aliases):
            folded = fold(key)
            if folded in table and table[folded] is not spec:
                raise ValueError(f"duplicate command alias: {key}")
            table[folded] = spec
    return table


LOOKUP = _build_lookup()


@dataclass
class ParsedCommand:
    spec: CommandSpec
    raw: str
    arg: str = ""
    explicit: bool = False  # written as !cmd=value rather than !cmd value


@dataclass
class ParseResult:
    action: str | None = None
    commands: list[ParsedCommand] = field(default_factory=list)
    fields: dict[str, object] = field(default_factory=dict)
    leftover: str = ""
    unknown: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    command_source: str = ""      # subject | typed | body

    @property
    def has_command(self) -> bool:
        return self.action is not None or bool(self.fields)

    def get(self, key: str, default=None):
        return self.fields.get(key, default)


def _real_matches(text: str) -> list[re.Match]:
    """TOKEN_RE hits worth treating as command boundaries.

    A "!" followed by a space/newline and then an ordinary word — with no
    real command by that name — is almost always the closing bang of the
    PRECEDING command's own bang-delimited argument, immediately followed by
    unrelated prose ("!note good team!\\nBegin forwarded message:" reads "!"
    + newline + "Begin" as an attempted command). Left in the match list, it
    would wrongly bound where that preceding argument is allowed to end,
    cutting it one character short and leaving the real closing "!" behind as
    stray text. Excluding it here fixes that at the source, for every caller.
    """
    matches = []
    for match in TOKEN_RE.finditer(text):
        spec = LOOKUP.get(fold(match.group("name")))
        if spec is not None or not match.group("gap"):
            matches.append(match)
    return matches


def _read_quoted(work: str) -> tuple[str, int] | None:
    opener = work[:1]
    closer = _QUOTES.get(opener)
    if not closer:
        return None
    end = work.find(closer, 1)
    if end == -1:
        return None
    return work[1:end], end + 1


def _bare_cut(work: str, spec_name: str) -> int:
    """How much of `work` counts as an argument when there is no closing "!".

    For a name, take the leading run of capitalised words — company names are
    capitalised and the prose that follows them usually is not, so
    "!addp Hero bakalım" yields "Hero" while "!addp Larkspur AI" keeps all
    three. A lowercase first word is taken alone, so "!addp lumen" still works.

    For a note with no closing bang either, take the whole remaining region
    (still bounded by `_MAX_NOTE_CHARS` and by wherever the next real command
    starts, via the caller's `next_start`) — a note is a paragraph, not a
    single line, so it must not be truncated at the first newline.
    """
    limit = len(work)

    if spec_name == "note":
        return min(limit, _MAX_NOTE_CHARS)

    stop = _HARD_STOP_RE.search(work)
    if stop:
        limit = min(limit, stop.start())
    newline = work.find("\n")
    if newline != -1:
        limit = min(limit, newline)

    words = list(re.finditer(r"\S+", work[:limit]))

    taken = 0
    for index, match in enumerate(words[:_MAX_BARE_ARG_WORDS]):
        word = match.group(0)
        # "!addp FW: Application - ACME" -> stop dead; the forwarded subject
        # has resumed and none of it is the name.
        if SUBJECT_PREFIX_RE.match(work[match.start():]):
            break
        first = word.lstrip("(\"'“‘")[:1]
        if index > 0 and not (first.isupper() or first.isdigit()):
            break
        taken = match.end()
        if index == 0 and not (first.isupper() or first.isdigit()):
            break  # lowercase name given on its own, e.g. "!addp lumen"
    return taken


def _extract_arg(spec: CommandSpec, region: str) -> tuple[str, int, bool]:
    """Return (argument, characters consumed from region, was_explicit).

    The closing "!" is the primary delimiter — "!addp Larkspur!" needs no
    guessing about where the name ends. Everything else is a fallback for when
    it is left off.
    """
    if spec.arg == NO_ARG:
        return "", 0, False

    lead = len(region) - len(region.lstrip(" \t"))
    work = region[lead:]

    quoted = _read_quoted(work)
    if quoted is not None:
        value, used = quoted
        return value.strip(), lead + used, True

    # Closing bang: "!addp hockey!" -> "hockey". A bang on the very next
    # character means an empty argument, not a delimiter.
    #
    # Whatever sits between the bangs is taken verbatim — only surrounding
    # whitespace is removed. Case, punctuation and inner spacing are the
    # sender's choice, so "!addp ÇOK Güzel A.Ş.!" keeps its final dot and
    # "!addp Hero deck!" stays "Hero deck" rather than being tidied to "Hero".
    #
    # A company name is never legitimately more than one line, so addp/addi
    # still require the closing bang on the same line as the argument — a
    # stray "!" three paragraphs down must not get treated as the delimiter.
    # A !note is exactly the opposite: real notes are multi-paragraph free
    # text ("Arkadaşlar ekteki Girişim'e, ... Görüşüp bilgi vermeniz mümkün
    # müdür acaba?!"), so its closing bang is allowed to sit past a newline.
    bang = work.find("!")
    spans_lines_ok = spec.name == "note"
    if bang > 0 and (spans_lines_ok or "\n" not in work[:bang]):
        value = work[:bang].strip(" \t")
        if value:
            return value, lead + bang + 1, True

    cut = _bare_cut(work, spec.name)
    value = work[:cut].strip(" \t-–—,;.|/")
    return value, lead + cut, False


def parse(text: str) -> ParseResult:
    """Parse a subject line (or a typed body prefix) into an intent."""
    result = ParseResult()
    if not text:
        return result

    matches = _real_matches(text)
    if not matches:
        result.leftover = text.strip()
        return result

    consumed: list[tuple[int, int]] = []
    notes: list[str] = []

    for index, match in enumerate(matches):
        spec = LOOKUP.get(fold(match.group("name")))
        token_end = match.end()
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)

        if spec is None:
            # "!word" is a plausible typo worth reporting. "! word" is just
            # ordinary punctuation ("Merhaba! Nasilsiniz") — leave it alone.
            if not match.group("gap"):
                result.unknown.append(match.group(0).strip())
                consumed.append((match.start(), token_end))
            continue

        explicit = False
        sep = _SEPARATOR_RE.match(text, token_end)
        if sep and sep.end() <= next_start:
            token_end = sep.end()
            explicit = True

        region = text[token_end:next_start]
        arg, used, quoted = _extract_arg(spec, region)
        explicit = explicit or quoted
        consumed.append((match.start(), token_end + used))

        if spec.arg == REQUIRED_ARG and not arg:
            result.warnings.append(
                f"!{spec.name} needs a value, e.g. !{spec.name}=something — ignored."
            )
            continue

        parsed = ParsedCommand(spec=spec, raw=match.group(0), arg=arg, explicit=explicit)
        result.commands.append(parsed)

        for key, value in spec.presets:
            result.fields.setdefault(key, value)

        if spec.kind == "action":
            if result.action is None:
                result.action = spec.name
                if arg:
                    if len(arg.split()) > _MAX_NAME_WORDS:
                        result.warnings.append(
                            f'"{arg}" is very long for a name — did you forget the '
                            "closing ! ?"
                        )
                    result.fields.setdefault("action_arg", arg)
            elif result.action != spec.name:
                result.warnings.append(
                    f"Two actions in one subject (!{result.action} and !{spec.name}); "
                    f"used !{result.action}."
                )
            continue

        # Modifiers.
        if spec.name == "note":
            notes.append(arg[:_MAX_NOTE_CHARS])
        elif arg:
            result.fields[spec.name] = arg

    if notes:
        result.fields["notes"] = notes
    result.leftover = _leftover(text, consumed)
    return result


def _leftover(text: str, consumed: Iterable[tuple[int, int]]) -> str:
    """Everything the commands did not eat — the fallback deal name."""
    kept = list(text)
    for start, end in consumed:
        for i in range(start, min(end, len(kept))):
            kept[i] = " "
    return re.sub(r"\s+", " ", "".join(kept)).strip(" \t-–—,;:|")


def parse_email(subject: str, typed_body: str = "", full_body: str = "") -> ParseResult:
    """Parse the subject, the text the sender typed, and the rest of the mail.

    Editing a subject on Outlook mobile is fiddly, so a command typed in the
    body — the natural thing on a phone — works just as well. `!note` is
    additionally picked up from anywhere in the message, since a note is inert:
    it only adds text to an item the sender is already creating.
    """
    result = parse(subject or "")
    if result.action:
        result.command_source = "subject"

    typed_result = parse((typed_body or "")[:2000])
    deep_result = parse((full_body or "")[:20000]) if full_body else ParseResult()

    # Subject first, then the sender's own text at the top, then anywhere else
    # in the message. People scribble a command next to the paragraph it refers
    # to, halfway down a forward, and expect it to count.
    for source, label in ((typed_result, "typed"), (deep_result, "body")):
        if result.action or not source.action:
            continue
        result.action = source.action
        result.command_source = label
        if source.get("action_arg"):
            result.fields.setdefault("action_arg", source.get("action_arg"))
        # The item name still comes from the subject when none was typed.
        result.leftover = (subject or "").strip()

    notes: list[str] = []
    for source in (result, typed_result, deep_result):
        for note in source.get("notes") or []:
            if note and note not in notes:
                notes.append(note)
    if notes:
        result.fields["notes"] = notes

    for source in (typed_result, deep_result):
        result.warnings.extend(source.warnings)
    result.unknown.extend(typed_result.unknown)
    return result


def strip_commands(text: str) -> str:
    """Remove recognised "!cmd ...!" sequences from arbitrary text, for display.

    Commands are operational syntax, not deal content — once parsed, showing
    "!addp Hero! !note good team!" verbatim in the update body is just noise,
    and the note text specifically would otherwise appear twice: once raw
    here, once in its own dedicated update. Unlike `parse()`'s `.leftover`
    (built for name resolution), this keeps line breaks intact so a
    multi-paragraph email stays readable once the commands are gone.
    """
    if not text:
        return ""
    matches = _real_matches(text)
    if not matches:
        return text

    consumed: list[tuple[int, int]] = []
    for index, match in enumerate(matches):
        spec = LOOKUP.get(fold(match.group("name")))
        if spec is None:
            continue
        token_end = match.end()
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sep = _SEPARATOR_RE.match(text, token_end)
        if sep and sep.end() <= next_start:
            token_end = sep.end()
        region = text[token_end:next_start]
        _, used, _ = _extract_arg(spec, region)
        consumed.append((match.start(), token_end + used))

    if not consumed:
        return text

    chars = list(text)
    for start, end in consumed:
        for i in range(start, min(end, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    stripped = "".join(chars)
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    stripped = re.sub(r"[ \t]+\n", "\n", stripped)
    stripped = re.sub(r"\n[ \t]+", "\n", stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)  # lines left empty by a removed command
    return stripped.strip()


def cheat_sheet() -> str:
    """Human-readable command list, used by !help and the README."""
    lines = ["Commands (put them anywhere in the subject — start, middle or end):", ""]
    for spec in COMMANDS:
        alias_text = ", ".join(f"!{a}" for a in spec.aliases[:3])
        suffix = f"   (also {alias_text})" if alias_text else ""
        lines.append(f"  !{spec.name:<9} {spec.help}{suffix}")
    lines += [
        "",
        "Anything ambiguous? Use the = form: !addp=\"Larkspur\" !src=investor",
    ]
    return "\n".join(lines)
