"""Turn a forwarded email into board activity.

Two commands, two destinations:

    !addp  ->  startup goes on the deal pipeline board
    !addi  ->  investor lead goes on the investor board

Everything else about the handling is identical, so the two share one path and
differ only in which board target they resolve to.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
from dataclasses import dataclass, field

from . import commands, emailparse
from .config import BoardTarget, Config
from .monday_client import MondayClient, MondayError, encode_column_value

LOG = logging.getLogger(__name__)

MAX_UPDATE_CHARS = 12000

# Our own replies quote the original subject, commands included, so if one ever
# reaches the watcher it would look like a fresh instruction. Vacation
# responders have the same problem. Both are refused by header before anything
# is parsed.
LOOP_HEADER = "x-monday-intake"
_AUTO_PRECEDENCE = {"bulk", "auto_reply", "junk", "list"}

# Sits at the top of the email update so anyone reading the board can tell at a
# glance that the item arrived through the intake script rather than by hand.
AUTOMATION_BANNER = "✅ Added via email automation"

_HEADINGS = {
    "deal": "📥 Startup intake",
    "investor": "🤝 Investor lead",
}


def automated_reason(headers: dict) -> str:
    """Why this message must not be treated as an instruction, or ''."""
    if headers.get(LOOP_HEADER):
        return "this is one of our own confirmation replies"
    if "auto-replied" in (headers.get("auto-submitted") or "").lower():
        return "automatic reply, ignored"
    if (headers.get("precedence") or "").strip().lower() in _AUTO_PRECEDENCE:
        return "bulk or automated mail, ignored"
    if headers.get("x-autoreply") or headers.get("x-autorespond"):
        return "out-of-office responder, ignored"
    return ""


@dataclass
class Result:
    status: str = "skipped"        # done | skipped | failed
    action: str = ""
    board: str = ""                # human label of the destination board
    deal_name: str = ""
    name_source: str = ""          # typed | subject | domain
    item_id: str = ""
    item_url: str = ""
    created: bool = False
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def note(self, text: str) -> None:
        self.notes.append(text)

    def warn(self, text: str) -> None:
        self.warnings.append(text)


class Intake:
    def __init__(
        self,
        config: Config,
        gmail,
        clients: dict[str, MondayClient] | None = None,
        dry_run: bool = False,
    ):
        self.config = config
        self.gmail = gmail
        self.clients = clients or {}
        self.dry_run = dry_run

    def client_for(self, target: BoardTarget) -> MondayClient | None:
        return self.clients.get(target.key)

    # ------------------------------------------------------------------ main
    def process(self, message: dict) -> Result:
        result = Result()

        loop_reason = automated_reason(message.get("headers", {}))
        if loop_reason:
            result.note(loop_reason)
            return result

        sender = message.get("from_email", "")
        if not self.config.sender_allowed(sender):
            result.note(f"sender {sender} is not on the allowlist")
            return result

        body_text = emailparse.clean_body(message.get("plain", ""), message.get("html", ""))
        parsed_email = emailparse.split_forward_chain(body_text)
        subject = message.get("subject", "")

        parsed = commands.parse_email(
            subject, parsed_email.typed_text, parsed_email.body_text
        )

        if not parsed.action:
            result.note("no command found in subject or body")
            if parsed.unknown:
                result.warn("possible typo: " + ", ".join(parsed.unknown))
            return result

        result.warnings.extend(parsed.warnings)
        if parsed.unknown:
            result.warn("unrecognised command(s) ignored: " + ", ".join(parsed.unknown))

        action = parsed.action
        result.action = action
        if parsed.command_source == "body":
            # Worth saying out loud: the command was not in the subject or at
            # the top, so if it came from text someone else wrote, this is how
            # you find out.
            result.note(f"!{action} was found inside the message, not in the subject")

        target = self.config.target_for(action)
        if target is None or not target.board_id:
            result.status = "failed"
            result.error = f"No board configured for !{action}."
            return result
        result.board = target.label

        guess = emailparse.resolve_deal_name(
            explicit=str(parsed.get("action_arg") or ""),
            subject=parsed.leftover,
            parsed=parsed_email,
        )
        if not guess:
            # Refusing beats guessing. A sentence filed as a company name is
            # worse than an email asking for the name.
            result.status = "failed"
            result.error = (
                "I could not tell the name from this subject, so nothing was added.\n"
                f"Send it again with the name straight after the command:\n"
                f"    !{action} Hero\n"
                f'    !{action}="Hero Robotics"'
            )
            return result

        name = guess.name
        result.deal_name = name
        result.name_source = guess.source
        if guess.source != "typed":
            result.note(f"name taken from the {guess.source}; check it is right")
        if guess.alternatives:
            result.note("other candidates: " + ", ".join(guess.alternatives[:3]))

        notes = [str(n) for n in (parsed.get("notes") or []) if str(n).strip()]

        try:
            return self._add(result, target, parsed_email, message, name, notes)
        except MondayError as exc:
            result.status = "failed"
            result.error = f"Monday API: {exc}"
            LOG.exception("Monday call failed for %s", name)
            return result

    # ----------------------------------------------------------------- write
    def _add(
        self,
        result: Result,
        target: BoardTarget,
        parsed_email: emailparse.ParsedEmail,
        message: dict,
        name: str,
        notes: list[str],
    ) -> Result:
        monday = self.client_for(target)
        contacts = emailparse.extract_contacts(parsed_email)

        item = monday.find_item_by_name(name) if monday else None
        column_values = self._build_columns(target, monday, contacts, result, creating=item is None)

        if item is None:
            if self.dry_run:
                result.note(f"would CREATE {name!r} on {target.label} with {column_values}")
                item = {"id": "dry-run", "name": name}
            else:
                item = monday.create_item(name, target.group_id, column_values)
            result.created = True
        else:
            result.note(f'matched existing "{item["name"]}" on {target.label}')

        result.item_id = str(item["id"])
        result.item_url = "" if self.dry_run else monday.item_url(item["id"])

        # Notes go in first. Monday's activity feed shows the newest update at
        # the top, so posting notes before the main email means the email ends
        # up on top and the notes sit below it — matching reading order rather
        # than looking backwards.
        self._post_notes(monday, item["id"], notes, result)

        body = self._build_update_body(message, parsed_email, contacts, target)
        if self.dry_run:
            result.note(f"would POST update ({len(body)} chars)")
            update_id = "dry-run"
        else:
            update_id = monday.create_update(item["id"], body)["id"]

        self._upload_attachments(message, monday, item["id"], update_id, target, result)
        result.status = "done"
        return result

    def _post_notes(
        self, monday: MondayClient | None, item_id: str, notes: list[str], result: Result
    ) -> None:
        """Each !note becomes its own update, separate from the email itself.

        The very first note an item ever gets is plain "📝 Note". Every one
        after that — whether it's a second !note in the same email or one
        from a follow-up email weeks later — is "📝 Note Update N", counting
        the item's whole history rather than just what's in this message.
        That means the count has to come from Monday itself; nothing local is
        trustworthy across separate runs.
        """
        if not notes:
            return
        existing = 0
        if monday is not None:
            try:
                existing = monday.count_existing_notes(item_id)
            except Exception as exc:
                LOG.warning("could not count existing notes on %s: %s", item_id, exc)

        for offset, note in enumerate(notes, start=1):
            sequence = existing + offset
            heading = "📝 Note" if sequence == 1 else f"📝 Note Update {sequence}"
            text = f"<b>{heading}</b><br/>{html.escape(note).replace(chr(10), '<br/>')}"
            if self.dry_run:
                result.note(f"would POST {heading.lower()}: {note[:60]}")
                continue
            monday.create_update(item_id, text)
            result.note(f"posted {heading.lower()}: {note[:60]}")

    # --------------------------------------------------------------- columns
    def _build_columns(
        self,
        target: BoardTarget,
        monday: MondayClient | None,
        contacts: dict,
        result: Result,
        creating: bool,
    ) -> dict:
        values: dict[str, object] = {}
        if not target.columns or not monday or not creating:
            # Contact details are written on creation only, so a later forward
            # cannot overwrite something a human has since corrected.
            return values

        def put(slot: str, value) -> None:
            column_id = target.columns.get(slot)
            if not column_id or value in (None, "", []):
                return
            encoded = encode_column_value(monday.column_type(column_id), value)
            if encoded is not None:
                values[column_id] = encoded

        put("email", contacts.get("contact_email"))
        put("website", contacts.get("website"))
        put("contact", contacts.get("contact_name"))
        put("phone", contacts.get("phone"))
        put("intake_date", dt.date.today().isoformat())
        return values

    # ---------------------------------------------------------------- update
    def _build_update_body(
        self,
        message: dict,
        parsed_email: emailparse.ParsedEmail,
        contacts: dict,
        target: BoardTarget,
    ) -> str:
        esc = html.escape
        inner = parsed_email.innermost
        lines = [
            f"<b>{AUTOMATION_BANNER}</b>",
            f"<b>{_HEADINGS.get(target.key, 'Intake from email')}</b>",
            "",
        ]

        def row(label: str, value: str) -> None:
            if value:
                lines.append(f"<b>{label}:</b> {esc(str(value))}")

        row("Forwarded by", message.get("from", ""))
        if inner is not None and inner.sender_email:
            row("Original sender", f"{inner.sender_name} <{inner.sender_email}>".strip())
        row("Original subject", parsed_email.original_subject or message.get("subject", ""))
        row("Received", message.get("date", ""))
        row("Contact", contacts.get("contact_email", ""))
        row("Phone", contacts.get("phone", ""))

        website = contacts.get("website")
        if website:
            url = str(website)
            if not url.startswith("http"):
                url = "https://" + url
            display = emailparse.shorten_url(str(website))
            lines.append(f'<b>Website:</b> <a href="{esc(url)}">{esc(display)}</a>')

        # Commands are operational syntax ("!addp Hero! !note good team!"), not
        # deal content — stripped before display so they never show up raw,
        # and so !note text isn't shown here a second time on top of its own
        # dedicated update below.
        typed = commands.strip_commands(parsed_email.typed_text).strip()
        body = commands.strip_commands(parsed_email.body_text).strip()

        # typed_text is always whatever the sender wrote above the quoted
        # chain, which is why body_text starts with it verbatim — once both
        # are stripped of commands, drop that same prefix from body so it is
        # not shown twice.
        if typed and body.startswith(typed):
            body = body[len(typed):].strip()

        if typed:
            typed = emailparse.shorten_long_urls(typed)
            lines += ["", "<b>Forwarder's note</b>", esc(typed).replace("\n", "<br/>")]

        if body:
            body = emailparse.shorten_long_urls(body)
            if len(body) > MAX_UPDATE_CHARS:
                body = body[:MAX_UPDATE_CHARS] + "\n\n[…truncated, see the original email]"
            lines += ["", "<b>Email</b>", esc(body).replace("\n", "<br/>")]

        return "<br/>".join(lines)

    # ----------------------------------------------------------- attachments
    def _upload_attachments(
        self,
        message: dict,
        monday: MondayClient | None,
        item_id: str,
        update_id: str,
        target: BoardTarget,
        result: Result,
    ) -> None:
        """Real documents go to the activity feed as before, and — when a
        pitch-deck column is mapped — also into that column, so a startup's
        deck is a real file on the item, not just something buried in a
        comment. Both destinations are independent; a failure in one (e.g. an
        unmapped column) never blocks the other."""
        if not self.config.upload_attachments:
            return
        keep = emailparse.choose_attachments(
            message.get("attachments", []), message.get("html", "")
        )
        if not keep:
            return

        deck_column = target.columns.get("pitch_deck")
        limit = self.config.max_attachment_mb * 1024 * 1024
        for item in keep:
            name = item["filename"]
            if item.get("size", 0) > limit:
                result.warn(f"{name} is larger than {self.config.max_attachment_mb}MB; skipped.")
                continue
            if self.dry_run:
                result.note(f"would attach {name}" + (" (+ pitch deck column)" if deck_column else ""))
                continue
            try:
                data = self.gmail.get_attachment(message["id"], item["attachmentId"])
            except Exception as exc:  # one bad file must not lose the whole intake
                LOG.warning("attachment %s failed: %s", name, exc)
                result.warn(f"could not attach {name}: {exc}")
                continue

            try:
                monday.add_file_to_update(update_id, name, data, item["mimeType"])
                result.note(f"attached {name}")
            except Exception as exc:
                LOG.warning("attachment %s failed on the update: %s", name, exc)
                result.warn(f"could not attach {name} to the update: {exc}")

            if deck_column:
                try:
                    monday.add_file_to_column(item_id, deck_column, name, data, item["mimeType"])
                    result.note(f"added {name} to the pitch deck column")
                except Exception as exc:
                    LOG.warning("attachment %s failed on the column: %s", name, exc)
                    result.warn(f"could not add {name} to the pitch deck column: {exc}")


# --------------------------------------------------------------------------
def confirmation_subject(result: Result) -> str:
    """A clean subject for the reply — no "Re:", no repeating the messy
    command-laden original subject back at the sender."""
    if result.status == "done":
        verb = "added to" if result.created else "updated on"
        return f"✅ {result.deal_name} {verb} the {result.board}"
    if result.status == "failed":
        return "⚠️ Could not add this email — action needed"
    return "Monday intake"


def confirmation_text(result: Result, subject: str) -> str:
    """The reply that goes back to whoever forwarded the mail.

    The subject line already says what happened (built by
    confirmation_subject); the body doesn't repeat it, just the link.
    """
    if result.status == "done":
        lines = []
        if result.item_url:
            lines.append(result.item_url)
    elif result.status == "failed":
        lines = ["⚠️ Could not process this forward.", "", result.error]
    else:
        lines = [
            "No command found in this email, so nothing was added.",
            "",
            "Put a command anywhere in the subject line:",
            "    !addp   startup  -> deal pipeline",
            "    !addi   investor -> investor leads",
            "",
            "For example:  FW: Application - ACME !addp",
        ]

    if result.notes:
        lines += ["", "Notes:"] + [f"  - {n}" for n in result.notes]
    if result.warnings:
        lines += ["", "Warnings:"] + [f"  - {w}" for w in result.warnings]

    lines += ["", "--", "Sent automatically by the Gmail → Monday intake script."]
    return "\n".join(lines)
