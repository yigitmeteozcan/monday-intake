"""Downtime and catch-up behaviour.

The Gmail search is time-boxed. That is safe while the script runs, and unsafe
the moment it stops for longer than the window, so these tests pin the
catch-up rules.
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from intake.state import State
from tests.test_pipeline import make_config


def _state(days_ago=None) -> State:
    state = State(Path(tempfile.mkdtemp()) / "state.json")
    if days_ago is not None:
        state.last_run = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return state


class TestLookbackWindow(unittest.TestCase):
    def test_first_ever_run_uses_the_configured_window(self):
        self.assertEqual(_state().lookback_days(7, 90), 7)

    def test_a_quiet_week_from_the_sender_changes_nothing(self):
        # The script kept running; the window follows the clock, not the
        # message count, so a gap in *their* sending is irrelevant.
        self.assertEqual(_state(days_ago=0).lookback_days(7, 90), 7)

    def test_recent_runs_never_narrow_the_window(self):
        for gap in (0, 1, 3, 5):
            self.assertEqual(_state(days_ago=gap).lookback_days(7, 90), 7)

    def test_downtime_widens_the_window_to_cover_the_gap(self):
        self.assertEqual(_state(days_ago=20).lookback_days(7, 90), 21)
        self.assertEqual(_state(days_ago=45).lookback_days(7, 90), 46)

    def test_the_window_is_capped(self):
        self.assertEqual(_state(days_ago=500).lookback_days(7, 90), 90)

    def test_naive_timestamps_are_tolerated(self):
        state = _state()
        state.last_run = dt.datetime.now() - dt.timedelta(days=30)  # no tzinfo
        self.assertEqual(state.lookback_days(7, 90), 31)


class TestProcessedIds(unittest.TestCase):
    """Second line of defence if the Gmail label fails to apply."""

    def test_marked_messages_are_skipped(self):
        state = _state()
        state.mark("abc")
        self.assertTrue(state.seen("abc"))
        self.assertFalse(state.seen("def"))

    def test_the_id_list_stays_bounded(self):
        state = _state()
        for i in range(6000):
            state.mark(str(i))
        self.assertLessEqual(len(state.processed), 5000)
        self.assertTrue(state.seen("5999"))

    def test_round_trip_through_disk(self):
        state = _state(days_ago=3)
        state.mark("kept")
        state.save()

        reloaded = State.load(state.path)
        self.assertTrue(reloaded.seen("kept"))
        self.assertIsNotNone(reloaded.last_run)
        self.assertEqual(reloaded.lookback_days(7, 90), 7)

    def test_corrupt_state_file_does_not_crash(self):
        path = Path(tempfile.mkdtemp()) / "state.json"
        path.write_text("{not json", encoding="utf-8")
        state = State.load(path)
        self.assertIsNone(state.last_run)
        self.assertEqual(state.processed, [])

    def test_missing_state_file_is_a_fresh_start(self):
        state = State.load(Path(tempfile.mkdtemp()) / "nope.json")
        self.assertIsNone(state.last_run)


class TestQueryWindow(unittest.TestCase):
    def test_query_uses_the_widened_window(self):
        config = make_config()
        self.assertIn("newer_than:7d", config.search_query())
        self.assertIn("newer_than:31d", config.search_query(31))

    def test_catch_up_query_still_excludes_handled_mail(self):
        query = make_config().search_query(60)
        self.assertIn('-label:"Monday/Done"', query)
        self.assertIn("-in:sent", query)


class TestAddressFilter(unittest.TestCase):
    """A plus-alias carves a lane out of a busy inbox without a new account."""

    def test_no_address_filter_by_default(self):
        self.assertNotIn("to:", make_config().search_query())

    def test_address_filter_narrows_the_query(self):
        config = make_config()
        config.to_address = "analyst+monday@gmail.com"
        query = config.search_query()
        self.assertIn("to:analyst+monday@gmail.com", query)
        # and it still composes with everything else
        self.assertIn("-in:sent", query)
        self.assertIn("from:sender@farklabs.com", query)

    def test_address_filter_survives_a_catch_up_window(self):
        config = make_config()
        config.to_address = "analyst+monday@gmail.com"
        query = config.search_query(31)
        self.assertIn("newer_than:31d", query)
        self.assertIn("to:analyst+monday@gmail.com", query)


if __name__ == "__main__":
    unittest.main()
