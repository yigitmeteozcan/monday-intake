"""Tests for the message-processing order in run_once.

Gmail's search returns newest-first. Monday's activity feed shows the
newest-CREATED update first. Processing messages in Gmail's own order would
create the older email's Monday updates most recently, pushing it above a
genuinely newer email — exactly the bug reported from a real run, where a
1-minute-older forward ended up below a newer one instead of above it.
"""

import unittest

from intake.cli import chronological


def msg(id_, minutes_ago):
    # internal_date is epoch millis as a string, same shape Gmail returns.
    return {"id": id_, "internal_date": str(1000000 - minutes_ago * 60_000)}


class TestChronologicalOrder(unittest.TestCase):
    def test_gmails_newest_first_order_is_reversed_to_oldest_first(self):
        # Gmail hands these back newest-first: "later" before "earlier".
        gmail_order = [msg("later", minutes_ago=0), msg("earlier", minutes_ago=1)]
        result = chronological(gmail_order)
        self.assertEqual([m["id"] for m in result], ["earlier", "later"])

    def test_already_chronological_input_is_unaffected(self):
        already_sorted = [msg("first", minutes_ago=5), msg("second", minutes_ago=1)]
        result = chronological(already_sorted)
        self.assertEqual([m["id"] for m in result], ["first", "second"])

    def test_missing_internal_date_does_not_crash(self):
        messages = [{"id": "no-date"}, msg("has-date", minutes_ago=1)]
        result = chronological(messages)  # must not raise
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
