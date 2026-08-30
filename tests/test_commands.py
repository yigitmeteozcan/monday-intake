"""Command grammar tests.

The cases are drawn from subject lines that actually appear in the inbox, so a
regression here means a real forward would break.
"""

import unittest

from intake import commands
from intake.textnorm import fold, normalize_company, strip_subject_prefixes


class TestPosition(unittest.TestCase):
    """The headline requirement: position must not matter."""

    def test_command_at_start_with_name(self):
        result = commands.parse_email("!addp Larkspur")
        self.assertEqual(result.action, "addp")
        self.assertEqual(result.get("action_arg"), "Larkspur")

    def test_command_at_end(self):
        result = commands.parse_email("FW: Application - ACME !addp")
        self.assertEqual(result.action, "addp")
        self.assertEqual(result.leftover, "FW: Application - ACME")

    def test_command_at_start_before_original_subject(self):
        # The subject after the command is the forwarded subject, not a name.
        result = commands.parse_email("!addp FW: Application - ACME")
        self.assertEqual(result.action, "addp")
        self.assertIsNone(result.get("action_arg"))
        self.assertIn("ACME", result.leftover)

    def test_command_in_the_middle(self):
        result = commands.parse_email("Deck geldi !addp Orbit")
        self.assertEqual(result.action, "addp")
        self.assertEqual(result.get("action_arg"), "Orbit")

    def test_investor_command_anywhere(self):
        for subject in ("!addi Sequoia", "FW: intro !addi", "!addi FW: intro"):
            self.assertEqual(commands.parse_email(subject).action, "addi", subject)


class TestTwoActionsOnly(unittest.TestCase):
    def test_actions_are_exactly_addp_and_addi(self):
        actions = {s.name for s in commands.COMMANDS if s.kind == "action"}
        self.assertEqual(actions, {"addp", "addi"})

    def test_retired_commands_are_reported_as_unknown(self):
        for retired in ("!pass", "!meet", "!stage=x", "!owner=someone", "!prio"):
            result = commands.parse_email(f"FW: X {retired}")
            self.assertIsNone(result.action, retired)
            self.assertTrue(result.unknown, retired)

    def test_the_two_actions_do_not_collide(self):
        result = commands.parse_email("!addp Northwind !addi")
        self.assertEqual(result.action, "addp")
        self.assertTrue(result.warnings)


class TestClosingBang(unittest.TestCase):
    """`!addp hockey!` — the closing bang ends the argument.

    This removes the guesswork entirely: no word caps, no heuristics about
    where a company name stops.
    """

    def test_bang_delimits_the_name(self):
        self.assertEqual(commands.parse_email("!addp hockey!").get("action_arg"), "hockey")

    def test_bang_allows_multi_word_names(self):
        result = commands.parse_email("!addp Larkspur AI!")
        self.assertEqual(result.get("action_arg"), "Larkspur AI")

    def test_bang_works_mid_sentence(self):
        result = commands.parse_email("FW: bi girisim var !addp Hero! bakar misin")
        self.assertEqual(result.get("action_arg"), "Hero")
        self.assertNotIn("bakar", result.get("action_arg"))

    def test_without_bang_a_capitalised_run_is_taken(self):
        self.assertEqual(
            commands.parse_email("!addp Larkspur AI").get("action_arg"), "Larkspur AI"
        )

    def test_without_bang_lowercase_prose_is_dropped(self):
        self.assertEqual(commands.parse_email("!addp Hero bakalım").get("action_arg"), "Hero")

    def test_lowercase_single_word_name_still_works(self):
        self.assertEqual(commands.parse_email("!addp lumen").get("action_arg"), "lumen")

    def test_bare_command_takes_no_argument(self):
        result = commands.parse_email("FW: Application - ACME !addp")
        self.assertIsNone(result.get("action_arg"))

    def test_between_the_bangs_is_verbatim(self):
        """Whatever sits between the bangs is what lands on the board.

        No tidying, no case fixing, no dropping words that look descriptive —
        if the sender typed it inside the delimiters, they meant it.
        """
        for subject, expected in [
            ("!addp hwatEver we Wrtoe!", "hwatEver we Wrtoe"),
            ("!addp Hero deck!", "Hero deck"),          # "deck" is NOT trimmed
            ("!addp Northwind yatırım!", "Northwind yatırım"),  # nor is "yatırım"
            ("!addp ÇOK Güzel A.Ş.!", "ÇOK Güzel A.Ş."),  # trailing dot survives
            ("!addp ACME?!", "ACME?"),
            ("!addp x/y & z!", "x/y & z"),
            ("!addp iyi bir startup bence hero!", "iyi bir startup bence hero"),
        ]:
            self.assertEqual(commands.parse_email(subject).get("action_arg"), expected, subject)

    def test_only_outer_whitespace_is_trimmed(self):
        result = commands.parse_email("!addp   bosluklu   isim   !")
        self.assertEqual(result.get("action_arg"), "bosluklu   isim")

    def test_overlong_name_warns(self):
        result = commands.parse_email("!addp one two three four five six seven eight nine!")
        self.assertTrue(any("closing" in w for w in result.warnings))


class TestClosingBangBoundaryBug(unittest.TestCase):
    """A phantom match must never truncate a real command's own argument.

    "!note text!\\nBegin ..." was read by the token scanner as TWO attempts:
    the real "!note", and a bogus "!<newline>Begin" (unrecognised, harmless on
    its own) — but the bogus match's mere presence in the list still shifted
    where the real note's closing "!" was searched for, silently dropping it.
    """

    def test_note_argument_is_not_truncated_by_a_following_word(self):
        result = commands.parse("!addp X! !note iyi gitti!\nBaşka bir konu burada")
        self.assertEqual(result.get("notes"), ["iyi gitti"])

    def test_addp_argument_not_truncated_when_followed_by_prose(self):
        result = commands.parse("!addp Hero! Bu cümle komut değil")
        self.assertEqual(result.get("action_arg"), "Hero")


class TestNotes(unittest.TestCase):
    def test_note_alongside_a_command(self):
        result = commands.parse_email("!addp hockey! !note startup güzelmiş!")
        self.assertEqual(result.action, "addp")
        self.assertEqual(result.get("action_arg"), "hockey")
        self.assertEqual(result.get("notes"), ["startup güzelmiş"])

    def test_several_notes_are_kept_in_order(self):
        result = commands.parse_email("!addp x! !note bir! !note iki!")
        self.assertEqual(result.get("notes"), ["bir", "iki"])

    def test_note_from_the_body(self):
        result = commands.parse_email("FW: Orbit !addp", "!note deck geldi!")
        self.assertEqual(result.get("notes"), ["deck geldi"])

    def test_note_deeper_in_the_mail(self):
        result = commands.parse_email("FW: Orbit !addp", "", "bir sey\n!note cok iyi!\n")
        self.assertEqual(result.get("notes"), ["cok iyi"])

    def test_duplicate_notes_collapse(self):
        result = commands.parse_email("FW: X !addp", "!note ayni!", "!note ayni!")
        self.assertEqual(result.get("notes"), ["ayni"])

    def test_note_without_bang_spans_multiple_lines(self):
        # A note is a paragraph, not a single line — with no closing bang it
        # takes everything remaining (bounded by _MAX_NOTE_CHARS), not just
        # the first line. This was a real bug: a multi-paragraph note typed
        # without a trailing "!" got truncated after its first line.
        result = commands.parse_email("FW: X !addp", "!note cok iyi duruyor\nbaska satir")
        self.assertEqual(result.get("notes"), ["cok iyi duruyor\nbaska satir"])

    def test_note_closing_bang_found_across_paragraphs(self):
        # Shape of a real reported case: a multi-paragraph Turkish note whose
        # closing "!" only arrives several lines and a blank line later.
        typed = (
            "!addp Vertex!\n"
            "!note Arkadaşlar ekteki girişimi inceledim, tanıdığımız bir "
            "yatırımcı da turda yer almış.\n"
            "Dolayısıyla yatırım düşünmüyorsak bile görüşüp detaylı bir "
            "feedback vermemiz bizim için çok faydalı olacaktır.\n\n"
            "Görüşüp bilgi vermeniz mümkün müdür acaba?!"
        )
        result = commands.parse_email("FW: something", typed, typed)
        self.assertEqual(result.action, "addp")
        self.assertEqual(result.get("action_arg"), "Vertex")
        notes = result.get("notes")
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].endswith("acaba?"))
        self.assertIn("Dolayısıyla", notes[0])

    def test_addp_still_requires_the_bang_on_the_same_line(self):
        # A company name must never swallow a following paragraph just
        # because a stray "!" appears somewhere further down — only !note
        # is allowed to cross lines looking for its closing bang.
        result = commands.parse_email("!addp Hero\nBaşka bir paragraf burada!")
        self.assertNotIn("\n", result.get("action_arg") or "")
        self.assertNotIn("Başka", result.get("action_arg") or "")

    def test_turkish_note_alias(self):
        self.assertEqual(commands.parse_email("!addp x! !not iyi!").get("notes"), ["iyi"])


class TestArguments(unittest.TestCase):
    def test_explicit_equals_form_wins(self):
        result = commands.parse_email('FW: intro !addp="Larkspur" from Sequoia')
        self.assertEqual(result.get("action_arg"), "Larkspur")

    def test_colon_form(self):
        result = commands.parse_email("!addp:Northwind FW: something")
        self.assertEqual(result.get("action_arg"), "Northwind")

    def test_bare_argument_is_capped(self):
        result = commands.parse_email("!addp one two three four five six seven")
        self.assertLessEqual(len(result.get("action_arg").split()), 5)

    def test_bare_argument_stops_at_separator(self):
        result = commands.parse_email("!addp Northwind Co | Series A deck")
        self.assertEqual(result.get("action_arg"), "Northwind Co")

    def test_investor_name_argument(self):
        result = commands.parse_email('FW: fon görüşmesi !addi="Ar-Ge 500 Firma"')
        self.assertEqual(result.action, "addi")
        self.assertEqual(result.get("action_arg"), "Ar-Ge 500 Firma")


class TestAliasesAndLanguage(unittest.TestCase):
    def test_turkish_alias_for_startups(self):
        self.assertEqual(commands.parse_email("!ekle Orbit").action, "addp")

    def test_turkish_alias_for_investors(self):
        self.assertEqual(commands.parse_email("!yatirimci Northwind").action, "addi")

    def test_turkish_dotless_i_alias(self):
        # "!YATIRIMCI" uppercased in Turkish still has to fold to the alias.
        self.assertEqual(commands.parse_email("!YATIRIMCI X").action, "addi")

    def test_mixed_case(self):
        self.assertEqual(commands.parse_email("!AddP Northwind").action, "addp")
        self.assertEqual(commands.parse_email("!ADDI Northwind").action, "addi")


class TestFalsePositives(unittest.TestCase):
    """Exclamation marks are ordinary punctuation in Turkish and English."""

    def test_exclamation_in_prose_is_not_a_command(self):
        result = commands.parse_email("Merhaba! Nasilsiniz")
        self.assertIsNone(result.action)
        self.assertEqual(result.unknown, [])
        self.assertIn("Nasilsiniz", result.leftover)

    def test_unknown_token_is_reported_not_executed(self):
        result = commands.parse_email("FW: deck !addpp Northwind")
        self.assertIsNone(result.action)
        self.assertIn("!addpp", result.unknown)

    def test_no_command_at_all(self):
        self.assertFalse(commands.parse_email("Re: Orbit 2nd Meeting").has_command)


class TestBodyFallback(unittest.TestCase):
    def test_command_typed_in_body_is_honoured(self):
        result = commands.parse_email("FW: Application - ACME", "!addp\n\nbunu ekler misin")
        self.assertEqual(result.action, "addp")
        self.assertEqual(result.leftover, "FW: Application - ACME")

    def test_subject_command_beats_body(self):
        result = commands.parse_email("FW: X !addi", "!addp")
        self.assertEqual(result.action, "addi")


class TestStripCommands(unittest.TestCase):
    def test_removes_addp_and_note(self):
        text = "!addp Hero! !note good team!\nRest of the email."
        result = commands.strip_commands(text)
        self.assertNotIn("!addp", result)
        self.assertNotIn("!note", result)
        self.assertNotIn("Hero", result)
        self.assertNotIn("good team", result)
        self.assertIn("Rest of the email.", result)

    def test_preserves_line_breaks(self):
        text = "!addp X!\n\nParagraph one.\n\nParagraph two."
        result = commands.strip_commands(text)
        self.assertIn("Paragraph one.\n\nParagraph two.", result)

    def test_text_without_commands_is_unchanged(self):
        text = "Just an ordinary paragraph with no commands at all."
        self.assertEqual(commands.strip_commands(text), text)

    def test_regression_closing_bang_followed_by_newline_and_word(self):
        """A real bug: "!note text!\\nBegin forwarded message:" was read as a
        SECOND command attempt ("!" + newline + "Begin"), which — though
        correctly unrecognised — still shifted where the real !note's
        argument was allowed to end, leaving one stray "!" behind."""
        text = "!note startup güzel falan!\nBegin forwarded message:\nMore text."
        result = commands.strip_commands(text)
        self.assertEqual(result, "Begin forwarded message:\nMore text.")

    def test_a_real_second_command_still_bounds_the_first(self):
        text = "!addp Hero! !addi Someone!"
        result = commands.strip_commands(text)
        self.assertEqual(result.strip(), "")


class TestNormalisation(unittest.TestCase):
    def test_turkish_fold(self):
        self.assertEqual(fold("İSTANBUL"), "istanbul")
        self.assertEqual(fold("Şişli"), "sisli")
        self.assertEqual(fold("Iğdır"), "igdir")

    def test_company_key_ignores_legal_suffixes(self):
        self.assertEqual(normalize_company("Acme A.Ş."), normalize_company("ACME?"))
        self.assertEqual(normalize_company("Northwind Ltd. Şti."), "northwind")

    def test_nested_forward_prefixes_stripped(self):
        self.assertEqual(
            strip_subject_prefixes("FW: FW- Growth investment – Northwind Co"),
            "Growth investment – Northwind Co",
        )
        self.assertEqual(strip_subject_prefixes("AW: Fark Labs x lumen"), "Fark Labs x lumen")
        self.assertEqual(strip_subject_prefixes("YNT: Konu"), "Konu")


if __name__ == "__main__":
    unittest.main()
