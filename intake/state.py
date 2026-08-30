"""Run state, so a gap in uptime does not silently lose mail.

The Gmail query is time-boxed (`newer_than:7d`). That is fine while the script
is running — a quiet week from the sender changes nothing, because the window
follows the clock, not the message count. It is *not* fine if the script itself
stops for longer than the window: anything that arrived meanwhile ages out and
would never be looked at again.

So we remember when the last complete pass finished and widen the window to
cover the gap on the next start.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

LOG = logging.getLogger(__name__)

# Keep the id list bounded; it exists to survive a crash, not to be an archive.
MAX_PROCESSED_IDS = 5000


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.last_run: dt.datetime | None = None
        self.processed: list[str] = []

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: str | Path = "state.json") -> "State":
        state = cls(Path(path))
        if not state.path.exists():
            return state
        try:
            raw = json.loads(state.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            LOG.warning("could not read %s (%s); starting fresh", state.path, exc)
            return state

        stamp = raw.get("last_run")
        if stamp:
            try:
                state.last_run = dt.datetime.fromisoformat(stamp)
            except ValueError:
                LOG.warning("bad last_run in state file, ignoring")
        state.processed = [str(i) for i in raw.get("processed", [])][-MAX_PROCESSED_IDS:]
        return state

    def save(self) -> None:
        payload = {
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "processed": self.processed[-MAX_PROCESSED_IDS:],
        }
        try:
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            LOG.error("could not write %s: %s", self.path, exc)

    # --------------------------------------------------------------- window
    def lookback_days(self, configured: int, cap: int) -> int:
        """How far back the next search should reach.

        Never less than the configured window, so recent mail is always
        re-examined (labels stop it being handled twice). Never more than the
        cap, because an enormous Gmail query helps nobody.
        """
        if self.last_run is None:
            return configured
        gap = (dt.datetime.now(dt.timezone.utc) - self._aware(self.last_run)).days
        return max(configured, min(gap + 1, cap))

    def gap_days(self) -> int:
        if self.last_run is None:
            return 0
        return (dt.datetime.now(dt.timezone.utc) - self._aware(self.last_run)).days

    @staticmethod
    def _aware(value: dt.datetime) -> dt.datetime:
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)

    # ------------------------------------------------------------ processed
    def seen(self, message_id: str) -> bool:
        """Second line of defence behind the Gmail labels.

        If the script dies between writing to Monday and applying the label,
        the label alone would let the message be handled again on the next
        pass and post the email a second time.
        """
        return message_id in self.processed

    def mark(self, message_id: str) -> None:
        if message_id not in self.processed:
            self.processed.append(message_id)
            if len(self.processed) > MAX_PROCESSED_IDS:
                del self.processed[:-MAX_PROCESSED_IDS]

    def finish_pass(self) -> None:
        self.last_run = dt.datetime.now(dt.timezone.utc)
