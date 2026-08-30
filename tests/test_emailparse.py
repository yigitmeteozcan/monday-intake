"""Forward-chain and name-resolution tests, using real message shapes."""

import unittest

from intake import emailparse

# Verbatim structure of a real "FW: Application - ACME" forward: the sender (Outlook)
# forwarding a colleague (Outlook), who forwarded the founder writing from iCloud.
OUTLOOK_EN = """From: Mehmet Demir <colleague@farklabs.com>
Date: Wednesday, 22 July 2026 at 13:23
To: Ayşe Yılmaz <sender@farklabs.com>
Cc: Zeynep Şahin <manager@farklabs.com>
Subject: FW: Application - ACME

İlgilenirseniz bilginize Arkadaşlar.

Teşekkürler.
[cid:image001.png@01DD19DD.5580E3C0]
Mehmet Demir
Chief Value Enhancer
T. +90 212 244 55 43

From: Kerem Aydın <founder@icloud.com>
Sent: Wednesday, July 22, 2026 12:27 PM
To: info@fplus.ventures
Subject: Application - ACME?

Merhaba,

ACME? için hazırladığımız one-pager'ı ekte iletiyorum.

İyi çalışmalar,
Kerem Aydın
Founder - ACME?
0544 187 05 97
"""

OUTLOOK_TR = """Gönderen: Ali Kaya <colleague2@farklabs.com>
Gönderildi: Monday, 29 June 2026 13:48:38
Kime: Ayşe Yılmaz <sender@farklabs.com>
Konu: Northwind Co

Merhaba, ekte sunum var.
"""

APPLE_MAIL = """> Begin forwarded message:
>
> From: Ayşe Yılmaz <sender@farklabs.com>
> Subject: FW: Application - ACME
> Date: 24 July 2026 at 13:04:21 GMT+3
> To: Deniz Arslan <analyst@gmail.com>
>
> Merhaba, bunu da ekleyelim.
"""

GMAIL_STYLE = """bunu ekler misin

---------- Forwarded message ---------
From: Jonas Weber <founder@lumen.de>
Date: Mon, 20 Jul 2026 at 22:43
Subject: Fark Labs x lumen
To: <analyst@gmail.com>

We raised a sizable Series A in 2024. https://lumen.de
"""


class TestChainSplitting(unittest.TestCase):
    def test_outlook_english_two_hops(self):
        parsed = emailparse.split_forward_chain(emailparse.clean_body(OUTLOOK_EN))
        self.assertEqual(len(parsed.segments), 2)
        self.assertEqual(parsed.innermost.sender_email, "founder@icloud.com")
        self.assertEqual(parsed.original_subject, "Application - ACME?")

    def test_outlook_turkish_headers(self):
        parsed = emailparse.split_forward_chain(emailparse.clean_body(OUTLOOK_TR))
        self.assertEqual(parsed.innermost.sender_email, "colleague2@farklabs.com")
        self.assertEqual(parsed.original_subject, "Northwind Co")

    def test_apple_mail_quote_markers_removed(self):
        parsed = emailparse.split_forward_chain(emailparse.clean_body(APPLE_MAIL))
        self.assertEqual(parsed.innermost.sender_email, "sender@farklabs.com")
        # Angle brackets inside addresses stay; the "> " quote prefixes go.
        self.assertFalse(any(line.startswith(">") for line in parsed.body_text.splitlines()))

    def test_gmail_separator_and_typed_prefix(self):
        parsed = emailparse.split_forward_chain(emailparse.clean_body(GMAIL_STYLE))
        self.assertEqual(parsed.typed_text.strip(), "bunu ekler misin")
        self.assertEqual(parsed.innermost.sender_email, "founder@lumen.de")

    def test_cid_placeholders_stripped(self):
        cleaned = emailparse.clean_body(OUTLOOK_EN)
        self.assertNotIn("cid:image001", cleaned)


class TestNameResolution(unittest.TestCase):
    def test_dash_tail_preferred(self):
        guess = emailparse.resolve_deal_name(subject="FW: Application - ACME")
        self.assertEqual(guess.name, "ACME")
        self.assertEqual(guess.source, "subject")

    def test_nested_prefix_and_en_dash(self):
        guess = emailparse.resolve_deal_name(subject="FW: FW- Growth investment – Northwind Co")
        self.assertEqual(guess.name, "Northwind Co")

    def test_company_can_be_on_either_side_of_the_dash(self):
        """Real subjects put the name on both sides, so try both.

        "Application - ACME" and "Growth investment – Northwind Co" name the
        company after the dash; "ZENITH — Travel, Mobility and AI Venture
        Opportunity" names it before. The side that is nothing but generic
        business words is the description.
        """
        for subject, expected in [
            ("Fw: ZENITH — Travel, Mobility and AI Venture Opportunity", "ZENITH"),
            ("FW: Application - ACME", "ACME"),
            ("FW: FW- Growth investment – Northwind Co", "Northwind Co"),
            ("FW: Switch Mobility - meeting", "Switch Mobility"),
        ]:
            self.assertEqual(emailparse.resolve_deal_name(subject=subject).name, expected, subject)

    def test_a_description_on_both_sides_is_refused(self):
        self.assertFalse(emailparse.resolve_deal_name(subject="FW: Investment - Opportunity"))

    def test_typed_name_is_trusted_without_checks(self):
        guess = emailparse.resolve_deal_name(
            explicit="Larkspur", subject="FW: Application - ACME"
        )
        self.assertEqual(guess.name, "Larkspur")
        self.assertEqual(guess.source, "typed")

    def test_typed_name_is_never_reformatted(self):
        # Noise trimming and the prose check apply to guesses, never to a name
        # the sender typed between bangs.
        for typed in ("Hero deck", "Northwind yatırım", "ÇOK Güzel A.Ş.", "iyi bir startup hero"):
            self.assertEqual(emailparse.resolve_deal_name(explicit=typed).name, typed)

    def test_edge_noise_trimmed(self):
        self.assertEqual(emailparse.resolve_deal_name(subject="Northwind Co yatırım").name, "Northwind Co")
        self.assertEqual(emailparse.resolve_deal_name(subject="Hero deck").name, "Hero")

    def test_corporate_domain_used_when_subject_is_generic(self):
        parsed = emailparse.split_forward_chain(emailparse.clean_body(GMAIL_STYLE))
        guess = emailparse.resolve_deal_name(subject="FW: Intro", parsed=parsed)
        self.assertEqual(guess.name, "Lumen")
        self.assertEqual(guess.source, "domain")

    def test_freemail_domain_is_not_a_company(self):
        self.assertEqual(emailparse.company_from_email("founder@icloud.com"), "")
        self.assertEqual(emailparse.company_from_email("x@farklabs.com"), "")
        self.assertEqual(emailparse.company_from_email("founder@lumen.de"), "Lumen")

    def test_command_tokens_never_leak_into_a_name(self):
        self.assertNotIn("!", emailparse.resolve_deal_name(subject="FW: Northwind Co !addp").name)


class TestRefusesToGuessFromProse(unittest.TestCase):
    """A sentence must never become an item name.

    Filing "got a startup named hero - thanks" as a company is worse than
    bouncing the email back and asking, so anything that reads like prose is
    refused outright.
    """

    PROSE = [
        "FW: got a startup named hero - thanks",
        "FW: bi girisim var hero diye bakar misin",
        "FW: sana bi startup atiyorum bakar misin",
        "FW: arkadaslar bunu ekleyelim lutfen",
        "FW: can you check this one for me",
        "FW: Merhaba",
    ]

    def test_prose_subjects_are_refused(self):
        for subject in self.PROSE:
            guess = emailparse.resolve_deal_name(subject=subject)
            self.assertFalse(guess, f"should have refused: {subject}")
            self.assertEqual(guess.name, "")

    def test_typing_the_name_rescues_a_prose_subject(self):
        guess = emailparse.resolve_deal_name(
            explicit="Hero", subject="FW: got a startup named hero - thanks"
        )
        self.assertEqual(guess.name, "Hero")

    def test_real_company_subjects_still_pass(self):
        for subject, expected in [
            ("FW: Application - ACME", "ACME"),
            ("FW: Skyline AI", "Skyline AI"),
            ("FW: Orbit", "Orbit"),
            ("FW: FW- Growth investment – Northwind Co", "Northwind Co"),
        ]:
            self.assertEqual(emailparse.resolve_deal_name(subject=subject).name, expected)

    def test_all_lowercase_subject_is_refused(self):
        # Company names carry a capital; a hurried lowercase note does not.
        self.assertFalse(emailparse.resolve_deal_name(subject="FW: bunu ekle"))


class TestContacts(unittest.TestCase):
    def test_founder_phone_beats_forwarder_signature(self):
        parsed = emailparse.split_forward_chain(emailparse.clean_body(OUTLOOK_EN))
        contacts = emailparse.extract_contacts(parsed)
        self.assertEqual(contacts["contact_email"], "founder@icloud.com")
        self.assertIn("0544", contacts["phone"])
        self.assertNotIn("212", contacts["phone"])

    def test_internal_addresses_excluded(self):
        parsed = emailparse.split_forward_chain(emailparse.clean_body(OUTLOOK_EN))
        contacts = emailparse.extract_contacts(parsed)
        self.assertTrue(all("farklabs.com" not in e for e in contacts["emails"]))

    def test_website_found(self):
        parsed = emailparse.split_forward_chain(emailparse.clean_body(GMAIL_STYLE))
        self.assertEqual(emailparse.extract_contacts(parsed)["website"], "https://lumen.de")


class TestUrlShortening(unittest.TestCase):
    def test_short_url_untouched(self):
        self.assertEqual(emailparse.shorten_url("https://lumen.de"), "https://lumen.de")

    def test_long_tracking_url_shortened_to_host(self):
        url = "https://links.email.claude.com/s/c/" + "x" * 300
        self.assertEqual(emailparse.shorten_url(url), "https://links.email.claude.com/…")

    def test_shorten_long_urls_in_text_leaves_short_ones_alone(self):
        text = "See https://lumen.de and also https://tracking.example.com/" + "y" * 200
        result = emailparse.shorten_long_urls(text)
        self.assertIn("https://lumen.de", result)
        self.assertIn("https://tracking.example.com/…", result)
        self.assertNotIn("y" * 200, result)


class TestAttachments(unittest.TestCase):
    HTML = '<img src="cid:image001.png@01DD19DD"><img src="cid:image002.png@01DD19DD">'
    FILES = [
        {"filename": "image001.png", "mimeType": "image/png"},
        {"filename": "image002.png", "mimeType": "image/png"},
        {"filename": "ACME One-pager.pdf", "mimeType": "application/pdf"},
    ]

    def test_signature_logos_dropped_deck_kept(self):
        kept = emailparse.choose_attachments(self.FILES, self.HTML)
        self.assertEqual([f["filename"] for f in kept], ["ACME One-pager.pdf"])

    def test_generated_names_dropped_without_html(self):
        kept = emailparse.choose_attachments(self.FILES, "")
        self.assertEqual([f["filename"] for f in kept], ["ACME One-pager.pdf"])

    def test_real_screenshot_kept(self):
        kept = emailparse.choose_attachments(
            [{"filename": "product-shot.png", "mimeType": "image/png"}], ""
        )
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
