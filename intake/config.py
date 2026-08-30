"""Configuration, read from .env plus the environment.

Two commands, two boards: !addp writes startups to the deal pipeline, !addi
writes investor leads to the investor board. Each board gets its own group and
its own column mapping, because the two boards do not have the same columns.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Column slots we know how to fill. All optional — an unmapped slot is skipped.
COLUMN_FIELDS = (
    "email", "website", "contact", "phone", "intake_date", "notes", "pitch_deck",
)

# Which board each action writes to.
ACTION_TARGET = {"addp": "deal", "addi": "investor"}


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env reader. Real environment variables always win."""
    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "evet"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "") or ""
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


@dataclass
class BoardTarget:
    """One destination board."""

    key: str            # "deal" | "investor"
    label: str          # for log lines and confirmation emails
    board_id: str = ""
    group_id: str = ""
    columns: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, key: str, label: str, prefix: str) -> "BoardTarget":
        columns = {}
        for slot in COLUMN_FIELDS:
            value = os.environ.get(f"{prefix}_COL_{slot.upper()}", "").strip()
            if value:
                columns[slot] = value
        return cls(
            key=key,
            label=label,
            board_id=os.environ.get(f"{prefix}_BOARD_ID", "").strip(),
            group_id=os.environ.get(f"{prefix}_GROUP_ID", "").strip(),
            columns=columns,
        )


@dataclass
class Config:
    credentials_file: str = "credentials.json"
    token_file: str = "token.json"
    allowed_senders: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    to_address: str = ""
    lookback_days: int = 7
    max_lookback_days: int = 90
    poll_seconds: int = 120
    label_prefix: str = "Monday"
    state_file: str = "state.json"

    monday_token: str = ""
    api_version: str = "2024-10"
    targets: dict[str, BoardTarget] = field(default_factory=dict)

    reply_on_success: bool = True
    reply_on_error: bool = True
    upload_attachments: bool = True
    max_attachment_mb: int = 25

    @classmethod
    def load(cls) -> "Config":
        load_dotenv()
        return cls(
            credentials_file=os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json"),
            token_file=os.environ.get("GMAIL_TOKEN_FILE", "token.json"),
            allowed_senders=_csv("INTAKE_ALLOWED_SENDERS"),
            allowed_domains=_csv("INTAKE_ALLOWED_DOMAINS"),
            to_address=os.environ.get("INTAKE_TO_ADDRESS", "").strip(),
            lookback_days=_int("INTAKE_LOOKBACK_DAYS", 7),
            max_lookback_days=_int("INTAKE_MAX_LOOKBACK_DAYS", 90),
            poll_seconds=_int("INTAKE_POLL_SECONDS", 120),
            state_file=os.environ.get("INTAKE_STATE_FILE", "state.json"),
            label_prefix=os.environ.get("INTAKE_LABEL_PREFIX", "Monday").strip("/"),
            monday_token=os.environ.get("MONDAY_API_TOKEN", "").strip(),
            api_version=os.environ.get("MONDAY_API_VERSION", "2024-10").strip(),
            targets={
                "deal": BoardTarget.from_env("deal", "deal pipeline", "MONDAY_DEAL"),
                "investor": BoardTarget.from_env(
                    "investor", "investor leads", "MONDAY_INVESTOR"
                ),
            },
            reply_on_success=_bool("REPLY_ON_SUCCESS", True),
            reply_on_error=_bool("REPLY_ON_ERROR", True),
            upload_attachments=_bool("UPLOAD_ATTACHMENTS", True),
            max_attachment_mb=_int("MAX_ATTACHMENT_MB", 25),
        )

    def target_for(self, action: str) -> BoardTarget | None:
        return self.targets.get(ACTION_TARGET.get(action, ""))

    # -- labels -------------------------------------------------------------
    @property
    def label_done(self) -> str:
        return f"{self.label_prefix}/Done"

    @property
    def label_failed(self) -> str:
        return f"{self.label_prefix}/Failed"

    @property
    def label_skipped(self) -> str:
        return f"{self.label_prefix}/NoCommand"

    def sender_allowed(self, address: str) -> bool:
        """Allowlist check. Without this, anyone who learns the address could
        write straight into the pipeline by putting !addp in a subject."""
        address = (address or "").lower().strip()
        if not address:
            return False
        if address in self.allowed_senders:
            return True
        domain = address.rsplit("@", 1)[-1]
        return domain in self.allowed_domains

    def search_query(self, lookback_days: int | None = None) -> str:
        """The Gmail query used to find work.

        Restricted to allowed senders at the query level so we never even
        download mail we are not going to act on. `lookback_days` widens the
        window after downtime; without it the configured default applies.
        """
        override = os.environ.get("INTAKE_SEARCH_QUERY", "").strip()
        if override:
            # Escape hatch. It replaces the generated query wholesale, which
            # means the downtime catch-up window does not apply to it.
            return override
        window = lookback_days or self.lookback_days
        senders = list(self.allowed_senders)
        senders += [f"*@{domain}" for domain in self.allowed_domains]
        from_clause = " OR ".join(f"from:{s}" for s in senders) if senders else ""
        # -in:sent matters: our own confirmation replies quote the original
        # subject, commands and all, and the mailbox owner is normally on the
        # allowlist. Without this they would match the query, be reprocessed,
        # and reply again — a mail loop.
        parts = [
            f"newer_than:{window}d",
            "-in:draft",
            "-in:chats",
            "-in:sent",
        ]
        if from_clause:
            parts.append(f"({from_clause})")
        # Narrows the search to mail addressed to one specific address, so a
        # plus-alias like you+monday@gmail.com can carve a lane out of a busy
        # inbox: ordinary team mail is never matched, never labelled, never
        # even downloaded.
        if self.to_address:
            parts.append(f"to:{self.to_address}")
        parts.append(f'-label:"{self.label_done}"')
        parts.append(f'-label:"{self.label_failed}"')
        parts.append(f'-label:"{self.label_skipped}"')
        return " ".join(parts)

    def validate(self) -> list[str]:
        problems = []
        if not self.monday_token:
            problems.append("MONDAY_API_TOKEN is not set.")
        for target in self.targets.values():
            if not target.board_id:
                problems.append(
                    f"MONDAY_{target.key.upper()}_BOARD_ID is not set "
                    f"({target.label})."
                )
        if not self.allowed_senders and not self.allowed_domains:
            problems.append(
                "No INTAKE_ALLOWED_SENDERS or INTAKE_ALLOWED_DOMAINS — refusing to "
                "process mail from everyone."
            )
        return problems
