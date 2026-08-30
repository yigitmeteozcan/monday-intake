"""End-to-end tests of the intake flow with stubbed Gmail and Monday."""

import unittest

from intake.config import BoardTarget, Config
from intake.pipeline import Intake, confirmation_subject, confirmation_text
from tests.test_emailparse import OUTLOOK_EN

DEAL_BOARD = "18146507025"
INVESTOR_BOARD = "18146373310"


class FakeMonday:
    """One board, with a couple of typed columns."""

    COLUMN_TYPES = {
        "email_col": "email",
        "site_col": "link",
        "date_col": "date",
        "contact_col": "text",
    }

    def __init__(self, board_id, items=None):
        self.board_id = board_id
        self.items = list(items or [])
        self.updates = []
        self.column_writes = []
        self.files = []
        self.column_files = []
        self._next_id = 100

    def column_type(self, column_id):
        return self.COLUMN_TYPES.get(column_id, "text")

    def find_item_by_name(self, name):
        # Same matching the real client uses, so the fake cannot drift from it.
        from intake.monday_client import match_item

        return match_item(self.items, name)

    def create_item(self, name, group_id="", column_values=None):
        item = {"id": str(self._next_id), "name": name}
        self._next_id += 1
        self.items.append(item)
        self.column_writes.append((group_id, column_values or {}))
        return item

    def create_update(self, item_id, body):
        self.updates.append((item_id, body))
        return {"id": f"u{len(self.updates)}"}

    def count_existing_notes(self, item_id):
        return sum(1 for iid, body in self.updates if iid == item_id and "📝 Note" in body)

    def add_file_to_update(self, update_id, filename, content, mime):
        self.files.append(filename)

    def add_file_to_column(self, item_id, column_id, filename, content, mime):
        self.column_files.append((item_id, column_id, filename))

    def item_url(self, item_id):
        return f"https://farklabs.monday.com/boards/{self.board_id}/pulses/{item_id}"


class FakeGmail:
    def get_attachment(self, message_id, attachment_id):
        return b"%PDF-1.4 fake"


def make_config(**overrides):
    columns = {
        "email": "email_col",
        "website": "site_col",
        "contact": "contact_col",
        "intake_date": "date_col",
        "pitch_deck": "deck_col",
    }
    config = Config(
        allowed_senders=["sender@farklabs.com"],
        allowed_domains=["farklabs.com"],
        monday_token="x",
        targets={
            "deal": BoardTarget("deal", "deal pipeline", DEAL_BOARD, "topics", dict(columns)),
            "investor": BoardTarget(
                "investor", "investor leads", INVESTOR_BOARD, "", dict(columns)
            ),
        },
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def make_message(subject, body=OUTLOOK_EN, sender="sender@farklabs.com", attachments=None):
    return {
        "id": "msg1",
        "thread_id": "t1",
        "subject": subject,
        "from": f"Ayşe Yılmaz <{sender}>",
        "from_email": sender,
        "date": "Fri, 24 Jul 2026 13:04:21 +0300",
        "headers": {"subject": subject, "from": sender, "message-id": "<abc@mail>"},
        "plain": body,
        "html": "",
        "attachments": attachments or [],
    }


def build(config=None, deal_items=None, investor_items=None, dry_run=False):
    deal = FakeMonday(DEAL_BOARD, deal_items)
    investor = FakeMonday(INVESTOR_BOARD, investor_items)
    intake = Intake(
        config or make_config(),
        FakeGmail(),
        {"deal": deal, "investor": investor},
        dry_run=dry_run,
    )
    return intake, deal, investor


class TestRouting(unittest.TestCase):
    """!addp and !addi must land on different boards."""

    def test_addp_goes_to_the_deal_board_only(self):
        intake, deal, investor = build()
        result = intake.process(make_message("FW: Application - ACME !addp"))
        self.assertEqual(result.status, "done")
        self.assertEqual(result.deal_name, "ACME")
        self.assertEqual(result.board, "deal pipeline")
        self.assertEqual([i["name"] for i in deal.items], ["ACME"])
        self.assertEqual(investor.items, [])

    def test_addi_goes_to_the_investor_board_only(self):
        intake, deal, investor = build()
        result = intake.process(make_message("FW: Northwind Co !addi"))
        self.assertEqual(result.status, "done")
        self.assertEqual(result.board, "investor leads")
        self.assertEqual([i["name"] for i in investor.items], ["Northwind Co"])
        self.assertEqual(deal.items, [])

    def test_item_url_points_at_the_right_board(self):
        intake, _, _ = build()
        result = intake.process(make_message("FW: Northwind Co !addi"))
        self.assertIn(INVESTOR_BOARD, result.item_url)

    def test_group_id_is_per_board(self):
        intake, deal, investor = build()
        intake.process(make_message("FW: Application - ACME !addp"))
        intake.process(make_message("FW: Northwind Co !addi"))
        self.assertEqual(deal.column_writes[0][0], "topics")
        self.assertEqual(investor.column_writes[0][0], "")

    def test_same_name_on_both_boards_is_not_a_duplicate(self):
        # A company can be both a portfolio prospect and a potential LP.
        intake, deal, investor = build()
        intake.process(make_message("FW: Northwind Co !addp"))
        result = intake.process(make_message("FW: Northwind Co !addi"))
        self.assertTrue(result.created)
        self.assertEqual(len(deal.items), 1)
        self.assertEqual(len(investor.items), 1)


class TestIntake(unittest.TestCase):
    def setUp(self):
        self.intake, self.deal, self.investor = build(
            deal_items=[{"id": "1", "name": "Orbit"}]
        )

    def test_email_update_opens_with_the_automation_banner(self):
        self.intake.process(make_message("FW: Application - ACME !addp"))
        body = self.deal.updates[0][1]
        self.assertTrue(body.startswith("<b>✅ Added via email automation</b>"))

    def test_banner_is_on_both_boards(self):
        self.intake.process(make_message("FW: Northwind Co !addi"))
        self.assertIn("Added via email automation", self.investor.updates[0][1])

    def test_note_updates_do_not_repeat_the_banner(self):
        self.intake.process(make_message("!addp hockey! !note iyi!"))
        bodies = [b for _, b in self.deal.updates]
        self.assertTrue(any("Added via email automation" in b for b in bodies))
        note_bodies = [b for b in bodies if "Added via email automation" not in b]
        self.assertTrue(note_bodies)
        self.assertTrue(all("📝 Note" in b for b in note_bodies))

    def test_update_carries_the_email_and_founder(self):
        self.intake.process(make_message("FW: Application - ACME !addp"))
        body = self.deal.updates[0][1]
        self.assertIn("Kerem Aydın", body)
        self.assertIn("Startup intake", body)

    def test_investor_update_has_its_own_heading(self):
        self.intake.process(make_message("FW: Northwind Co !addi"))
        self.assertIn("Investor lead", self.investor.updates[0][1])

    def test_second_forward_lands_on_the_existing_item(self):
        self.intake.process(make_message("FW: Application - ACME !addp"))
        result = self.intake.process(make_message("FW: ACME follow up !addp"))
        self.assertEqual(result.status, "done")
        self.assertFalse(result.created)
        self.assertEqual(len([i for i in self.deal.items if "acme" in i["name"].lower()]), 1)

    def test_contact_columns_written_on_creation(self):
        self.intake.process(make_message("FW: Application - ACME !addp"))
        _, values = self.deal.column_writes[0]
        self.assertEqual(values["email_col"]["email"], "founder@icloud.com")
        self.assertIn("date_col", values)

    def test_columns_not_overwritten_on_a_later_forward(self):
        self.intake.process(make_message("FW: Application - ACME !addp"))
        writes_before = len(self.deal.column_writes)
        self.intake.process(make_message("FW: ACME follow up !addp"))
        self.assertEqual(len(self.deal.column_writes), writes_before)

    def test_attachments_filtered_and_uploaded(self):
        message = make_message(
            "FW: Application - ACME !addp",
            attachments=[
                {"filename": "image001.png", "mimeType": "image/png", "attachmentId": "a", "size": 100},
                {"filename": "ACME One-pager.pdf", "mimeType": "application/pdf", "attachmentId": "b", "size": 5000},
            ],
        )
        self.intake.process(message)
        self.assertEqual(self.deal.files, ["ACME One-pager.pdf"])

    def test_oversized_attachment_skipped_with_warning(self):
        message = make_message(
            "FW: Application - ACME !addp",
            attachments=[
                {"filename": "huge.pdf", "mimeType": "application/pdf", "attachmentId": "b",
                 "size": 99 * 1024 * 1024},
            ],
        )
        result = self.intake.process(message)
        self.assertEqual(self.deal.files, [])
        self.assertTrue(any("huge.pdf" in w for w in result.warnings))

    def test_real_documents_also_go_to_the_pitch_deck_column(self):
        """A deck must land in BOTH places: the activity-feed comment (as
        before) and the mapped file column — neither replaces the other."""
        message = make_message(
            "FW: Application - ACME !addp",
            attachments=[
                {"filename": "image001.png", "mimeType": "image/png", "attachmentId": "a", "size": 100},
                {"filename": "ACME One-pager.pdf", "mimeType": "application/pdf", "attachmentId": "b", "size": 5000},
            ],
        )
        result = self.intake.process(message)
        self.assertEqual(self.deal.files, ["ACME One-pager.pdf"])  # still in the comment
        self.assertEqual(len(self.deal.column_files), 1)
        item_id, column_id, filename = self.deal.column_files[0]
        self.assertEqual(filename, "ACME One-pager.pdf")
        self.assertEqual(column_id, "deck_col")
        self.assertEqual(item_id, result.item_id)
        self.assertTrue(any("pitch deck column" in n for n in result.notes))

    def test_signature_logo_never_reaches_the_pitch_deck_column(self):
        message = make_message(
            "FW: Application - ACME !addp",
            attachments=[
                {"filename": "image001.png", "mimeType": "image/png", "attachmentId": "a", "size": 100},
            ],
        )
        self.intake.process(message)
        self.assertEqual(self.deal.column_files, [])

    def test_no_pitch_deck_column_mapped_still_attaches_to_the_comment(self):
        """If MONDAY_*_COL_PITCH_DECK is left blank, attachments still work
        exactly as before — this is additive, never a hard requirement."""
        config = make_config()
        config.targets["deal"].columns.pop("pitch_deck")
        intake, deal, _ = build(config)
        message = make_message(
            "FW: Application - ACME !addp",
            attachments=[
                {"filename": "ACME One-pager.pdf", "mimeType": "application/pdf",
                 "attachmentId": "b", "size": 5000},
            ],
        )
        intake.process(message)
        self.assertEqual(deal.files, ["ACME One-pager.pdf"])
        self.assertEqual(deal.column_files, [])

    def test_oversized_attachment_never_reaches_the_pitch_deck_column_either(self):
        message = make_message(
            "FW: Application - ACME !addp",
            attachments=[
                {"filename": "huge.pdf", "mimeType": "application/pdf", "attachmentId": "b",
                 "size": 99 * 1024 * 1024},
            ],
        )
        self.intake.process(message)
        self.assertEqual(self.deal.column_files, [])


class TestNotes(unittest.TestCase):
    """!note text! posts its own update, separate from the email."""

    def setUp(self):
        self.intake, self.deal, self.investor = build()

    def test_note_is_a_second_update(self):
        result = self.intake.process(
            make_message("!addp hockey! !note startup güzelmiş!")
        )
        self.assertEqual(result.status, "done")
        self.assertEqual(len(self.deal.updates), 2)
        bodies = [b for _, b in self.deal.updates]
        self.assertTrue(any("güzelmiş" in b and "Note" in b for b in bodies))

    def test_note_attaches_to_the_same_item(self):
        self.intake.process(make_message("!addp hockey! !note iyi!"))
        item_ids = {item_id for item_id, _ in self.deal.updates}
        self.assertEqual(len(item_ids), 1)
        self.assertEqual([i["name"] for i in self.deal.items], ["hockey"])

    def test_several_notes_become_several_updates(self):
        self.intake.process(make_message("!addp hockey! !note bir! !note iki!"))
        self.assertEqual(len(self.deal.updates), 3)  # email + two notes

    def test_note_typed_in_the_body(self):
        self.intake.process(
            make_message("FW: Application - ACME !addp", body="!note deck geldi!\n\n" + OUTLOOK_EN)
        )
        self.assertEqual(len(self.deal.updates), 2)
        bodies = [b for _, b in self.deal.updates]
        self.assertTrue(any("deck geldi" in b for b in bodies))

    def test_note_html_is_escaped(self):
        self.intake.process(make_message("!addp x! !note <script>alert(1)</script>!"))
        bodies = [b for _, b in self.deal.updates]
        note_body = next(b for b in bodies if "📝 Note" in b)
        self.assertNotIn("<script>", note_body)
        self.assertIn("&lt;script&gt;", note_body)

    def test_no_note_means_one_update(self):
        self.intake.process(make_message("!addp hockey!"))
        self.assertEqual(len(self.deal.updates), 1)

    def test_note_alone_does_nothing(self):
        """An item name only ever comes from !addp or !addi.

        !note on its own has nothing to attach to, so it must not create an
        item, guess a name, or touch either board.
        """
        for subject in ("FW: Orbit !note cok iyi!", "!note güzelmiş!"):
            intake, deal, investor = build()
            result = intake.process(make_message(subject))
            self.assertEqual(result.status, "skipped", subject)
            self.assertEqual(deal.items, [], subject)
            self.assertEqual(deal.updates, [], subject)
            self.assertEqual(investor.items, [], subject)

    def test_commands_buried_anywhere_in_the_message(self):
        """The sender scribbles !addp next to the paragraph it refers to.

        Neither command is in the subject, and neither is in the clean space
        at the top — both sit down inside the forwarded text.
        """
        body = (
            "From: Mehmet Demir <colleague@farklabs.com>\n"
            "Date: Wednesday, 22 July 2026 at 13:23\n"
            "To: Ayşe Yılmaz <sender@farklabs.com>\n"
            "Subject: FW: Application - CERO\n\n"
            "Ilgilenirseniz bilginize.\n\n"
            "!addp cero!\n\n"
            "From: Founder <founder@cero.io>\n"
            "Sent: Wednesday, July 22, 2026 12:27 PM\n"
            "Subject: Application - CERO\n\n"
            "Merhaba, ekte one-pager var.\n\n"
            "!note iyiymis!\n"
        )
        intake, deal, investor = build()
        result = intake.process(make_message("FW: Application - CERO", body=body))

        self.assertEqual(result.status, "done")
        self.assertEqual([i["name"] for i in deal.items], ["cero"])
        self.assertEqual(len(deal.updates), 2)
        bodies = [b for _, b in deal.updates]
        self.assertTrue(any("iyiymis" in b for b in bodies))
        # And it says so, so a stray !addp in someone else's text is visible.
        self.assertTrue(any("inside the message" in n for n in result.notes))

    def test_name_pushed_to_monday_is_the_verbatim_typed_text(self):
        self.intake.process(make_message("!addp hwatEver we Wrtoe! !note ok!"))
        self.assertEqual([i["name"] for i in self.deal.items], ["hwatEver we Wrtoe"])

    def test_email_update_lands_on_top_of_the_feed(self):
        """Monday shows the newest update first, so the email content — the
        primary thing to read — has to be created LAST, after any notes,
        or it ends up buried under them instead of on top."""
        self.intake.process(make_message("!addp hockey! !note iyi!"))
        creation_order = [b for _, b in self.deal.updates]
        self.assertIn("📝 Note", creation_order[0])
        self.assertIn("Added via email automation", creation_order[1])

    def test_first_note_ever_is_unnumbered(self):
        self.intake.process(make_message("!addp x! !note tek not!"))
        bodies = [b for _, b in self.deal.updates]
        self.assertTrue(any("📝 Note</b>" in b for b in bodies))
        self.assertFalse(any("Note Update" in b for b in bodies))

    def test_second_note_in_the_same_email_is_note_update_2(self):
        intake, deal, _ = build()
        intake.process(make_message("!addp y! !note bir! !note iki!"))
        bodies = [b for _, b in deal.updates]
        self.assertTrue(any("📝 Note</b>" in b for b in bodies))
        self.assertTrue(any("📝 Note Update 2" in b for b in bodies))

    def test_note_from_a_follow_up_email_continues_the_count(self):
        """A follow-up email arrives in a separate run entirely. The second
        note ever on this item must still read "Note Update 2", not restart
        at "Note" just because it came from a different message."""
        intake, deal, _ = build()
        intake.process(make_message("!addp Hero! !note güzel gibi!"))
        intake.process(make_message("!addp Hero! !note ilk görüşme yapılacak!"))
        bodies = [b for _, b in deal.updates]
        self.assertTrue(any("📝 Note</b>" in b and "güzel gibi" in b for b in bodies))
        self.assertTrue(
            any("📝 Note Update 2" in b and "ilk görüşme yapılacak" in b for b in bodies)
        )

    def test_third_follow_up_note_is_update_3(self):
        intake, deal, _ = build()
        intake.process(make_message("!addp Hero! !note bir!"))
        intake.process(make_message("!addp Hero! !note iki!"))
        intake.process(make_message("!addp Hero! !note uc!"))
        bodies = [b for _, b in deal.updates]
        self.assertTrue(any("📝 Note Update 3" in b for b in bodies))

    def test_commands_do_not_appear_raw_in_the_email_body(self):
        """Raw "!addp X! !note Y!" syntax is operational, not deal content —
        it must not show up verbatim in the readable update."""
        self.intake.process(make_message("!addp hockey! !note fena değil!"))
        email_body = next(
            b for _, b in self.deal.updates if "Added via email automation" in b
        )
        self.assertNotIn("!addp", email_body)
        self.assertNotIn("!note", email_body)

    def test_typed_scribble_is_not_shown_twice(self):
        """typed_text is always a prefix of body_text, so once both have their
        commands stripped, the same leftover text must not appear both under
        "Forwarder's note" and again under "Email"."""
        body = (
            "!addp örnek test!\n!note startup güzel falan!\n"
            "Begin forwarded message:\n\n"
            "From: Claude Team <no-reply@example.com>\n"
            "Subject: Hello\n\n"
            "Actual message content here."
        )
        self.intake.process(make_message("test", body=body))
        email_body = next(
            b for _, b in self.deal.updates if "Added via email automation" in b
        )
        # The whole point: no leftover fragment of the stripped commands, and
        # no repeated block of text.
        self.assertNotIn("örnek test", email_body)
        self.assertEqual(email_body.count("Actual message content here"), 1)

    def test_long_tracking_urls_are_shortened_in_the_body(self):
        # The website field's href legitimately keeps the full URL so the
        # link still works — only the *visible* text is shortened, so the
        # full string appears once (in href), not a second time as free text.
        long_url = "https://links.email.example.com/" + "x" * 300
        body = f"Merhaba, detaylar icin: {long_url} tesekkurler"
        self.intake.process(make_message("!addp x!", body=body))
        email_body = next(
            b for _, b in self.deal.updates if "Added via email automation" in b
        )
        self.assertEqual(email_body.count(long_url), 1)
        self.assertIn("links.email.example.com/…", email_body)


class TestGuards(unittest.TestCase):
    def setUp(self):
        self.intake, self.deal, self.investor = build()

    def test_stranger_is_ignored_even_with_a_valid_command(self):
        result = self.intake.process(make_message("!addp Acme", sender="attacker@example.com"))
        self.assertEqual(result.status, "skipped")
        self.assertEqual(self.deal.items, [])

    def test_no_command_does_nothing(self):
        result = self.intake.process(make_message("Re: Orbit 2nd Meeting"))
        self.assertEqual(result.status, "skipped")
        self.assertEqual(self.deal.updates, [])

    def test_retired_command_does_nothing_and_warns(self):
        result = self.intake.process(make_message("FW: Orbit !pass"))
        self.assertEqual(result.status, "skipped")
        self.assertEqual(self.deal.items, [])

    def test_unresolvable_name_fails_cleanly(self):
        result = self.intake.process(make_message("FW: Merhaba !addp", body="Selam"))
        self.assertEqual(result.status, "failed")
        self.assertIn("name", result.error.lower())

    def test_missing_board_id_fails_cleanly(self):
        config = make_config()
        config.targets["investor"].board_id = ""
        intake, _, _ = build(config)
        result = intake.process(make_message("FW: Northwind !addi"))
        self.assertEqual(result.status, "failed")
        self.assertIn("!addi", result.error)

    def test_dry_run_writes_nothing(self):
        intake, deal, _ = build(dry_run=True)
        result = intake.process(make_message("FW: Application - ACME !addp"))
        self.assertEqual(result.status, "done")
        self.assertEqual(deal.updates, [])
        self.assertEqual(deal.items, [])


class TestMailLoop(unittest.TestCase):
    """A confirmation reply quotes the original subject, commands and all.

    If one were ever treated as a fresh instruction the script would reply to
    itself forever, which is the one realistic way this could get the Gmail
    account rate-limited.
    """

    def setUp(self):
        self.intake, self.deal, _ = build()

    def test_our_own_reply_is_refused(self):
        message = make_message("Re: FW: Application - ACME !addp")
        message["headers"]["x-monday-intake"] = "reply"
        self.assertEqual(self.intake.process(message).status, "skipped")
        self.assertEqual(self.deal.updates, [])

    def test_vacation_responder_is_refused(self):
        message = make_message("Re: FW: Application - ACME !addp")
        message["headers"]["auto-submitted"] = "auto-replied"
        self.assertEqual(self.intake.process(message).status, "skipped")

    def test_bulk_mail_is_refused(self):
        message = make_message("FW: newsletter !addp")
        message["headers"]["precedence"] = "bulk"
        self.assertEqual(self.intake.process(message).status, "skipped")

    def test_an_ordinary_forward_still_passes(self):
        self.assertEqual(
            self.intake.process(make_message("FW: Application - ACME !addp")).status, "done"
        )


class TestConfirmation(unittest.TestCase):
    def test_success_names_the_board_in_the_subject_and_links_in_the_body(self):
        # Name and board belong in the subject (confirmation_subject); the
        # body doesn't repeat them, it's just the link.
        intake, _, _ = build()
        result = intake.process(make_message("FW: Northwind Co !addi"))
        subj = confirmation_subject(result)
        self.assertIn("Northwind Co", subj)
        self.assertIn("investor leads", subj)
        text = confirmation_text(result, "FW: Northwind Co !addi")
        self.assertIn(INVESTOR_BOARD, text)

    def test_skipped_reply_explains_both_commands(self):
        intake, _, _ = build()
        result = intake.process(make_message("Re: nothing here"))
        text = confirmation_text(result, "Re: nothing here")
        self.assertIn("!addp", text)
        self.assertIn("!addi", text)

    def test_success_subject_is_clean_not_a_messy_re(self):
        """The reply subject must not echo the noisy original ("Re: FW: ...
        !addp") back at the sender — it should read as a standalone result."""
        intake, _, _ = build()
        result = intake.process(make_message("FW: Application - ACME !addp"))
        subj = confirmation_subject(result)
        self.assertNotIn("Re:", subj)
        self.assertNotIn("!addp", subj)
        self.assertIn("ACME", subj)
        self.assertIn("✅", subj)

    def test_success_subject_distinguishes_created_from_updated(self):
        intake, _, _ = build()
        intake.process(make_message("FW: Application - ACME !addp"))
        result = intake.process(make_message("FW: ACME follow up !addp"))
        subj = confirmation_subject(result)
        self.assertIn("updated on", subj)

    def test_failed_subject_uses_a_warning_not_a_tick(self):
        intake, _, _ = build()
        result = intake.process(make_message("FW: Merhaba !addp", body="Selam"))
        subj = confirmation_subject(result)
        self.assertIn("⚠️", subj)
        self.assertNotIn("✅", subj)

    def test_success_body_does_not_repeat_the_subject(self):
        # The subject already says "✅ ACME added to the deal pipeline" —
        # the body shouldn't say it again, just the link.
        intake, _, _ = build()
        result = intake.process(make_message("FW: Application - ACME !addp"))
        text = confirmation_text(result, "FW: Application - ACME !addp")
        self.assertNotIn("added to the deal pipeline", text)
        self.assertIn(DEAL_BOARD, text)

    def test_footer_is_impersonal(self):
        # The footer goes out on every confirmation email, so it must not be
        # tied to whoever happens to be running the script.
        intake, _, _ = build()
        result = intake.process(make_message("FW: Application - ACME !addp"))
        text = confirmation_text(result, "x")
        self.assertIn("Sent automatically by the Gmail → Monday intake script.", text)


class TestSearchQuery(unittest.TestCase):
    def test_query_scopes_to_allowed_senders_and_excludes_handled(self):
        query = make_config().search_query()
        self.assertIn("from:sender@farklabs.com", query)
        self.assertIn('-label:"Monday/Done"', query)
        self.assertIn("newer_than:7d", query)

    def test_query_excludes_our_own_sent_mail(self):
        self.assertIn("-in:sent", make_config().search_query())


if __name__ == "__main__":
    unittest.main()
