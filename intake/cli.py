"""Command line entry points.

    python -m intake.cli auth            one-time Google authorisation
    python -m intake.cli check           validate configuration
    python -m intake.cli inspect-board   list Monday columns/groups/users
    python -m intake.cli parse "<subj>"  offline: show how a subject is read
    python -m intake.cli run             process new mail once
    python -m intake.cli watch           keep processing on an interval
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import commands, emailparse
from .config import ACTION_TARGET, COLUMN_FIELDS, Config
from .pipeline import Intake, confirmation_subject, confirmation_text
from .state import State

LOG = logging.getLogger("intake")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("googleapiclient").setLevel(logging.ERROR)


# --------------------------------------------------------------------------
def cmd_auth(args) -> int:
    from .gmail_client import GmailClient

    config = Config.load()
    client = GmailClient(config.credentials_file, config.token_file)
    client.authorize(interactive=True)
    print(f"Authorised. Token saved to {config.token_file}")
    print(f"Mailbox: {client.profile_address()}")
    return 0


def cmd_check(args) -> int:
    config = Config.load()
    problems = config.validate()

    print("Gmail query:")
    print(f"  {config.search_query()}\n")
    print(f"Allowed senders: {', '.join(config.allowed_senders) or '(none)'}")
    print(f"Allowed domains: {', '.join(config.allowed_domains) or '(none)'}")
    print(
        "Addressed to:    "
        + (config.to_address or "(any — all recent team mail is examined and labelled)")
        + "\n"
    )

    for action, key in ACTION_TARGET.items():
        target = config.targets[key]
        columns = ", ".join(target.columns) or "(none — only the name is written)"
        print(f"!{action:<6} -> {target.label}")
        print(f"          board   {target.board_id or '(unset)'}")
        print(f"          group   {target.group_id or '(board default)'}")
        print(f"          columns {columns}")

    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  ! {problem}")
        return 1
    print("\nConfiguration looks usable.")
    return 0


def cmd_inspect_board(args) -> int:
    from .monday_client import MondayClient

    config = Config.load()
    wanted = args.target or list(config.targets)

    for key in wanted:
        target = config.targets.get(key)
        if target is None:
            print(f"Unknown target {key!r}; expected one of {', '.join(config.targets)}")
            return 1
        if not target.board_id:
            print(f"\n=== {target.label}: no board id configured, skipping ===")
            continue

        monday = MondayClient(config.monday_token, target.board_id, config.api_version)
        board = monday.board_meta()
        prefix = f"MONDAY_{key.upper()}"

        print(f"\n=== {target.label} — !{[a for a, t in ACTION_TARGET.items() if t == key][0]} ===")
        print(f"Board: {board['name']}  (id {board['id']})")
        print(f"URL:   {board.get('url', '')}\n")

        print(f"Groups ({prefix}_GROUP_ID):")
        for group in board.get("groups", []):
            print(f"  {group['id']:<24} {group['title']}")

        print("\nColumns:")
        for column in board.get("columns", []):
            labels = monday.status_labels(column["id"])
            suffix = f"   labels: {', '.join(labels)}" if labels else ""
            print(f"  {column['id']:<24} {column['type']:<16} {column['title']}{suffix}")

        print("\nPaste the ids you want into .env:")
        for slot in COLUMN_FIELDS:
            print(f"  {prefix}_COL_{slot.upper()}=")
    return 0


def cmd_parse(args) -> int:
    """Dry parsing with no network at all — the safe way to try command ideas."""
    subject = " ".join(args.subject)
    body = ""
    if args.body_file:
        with open(args.body_file, encoding="utf-8") as handle:
            body = handle.read()

    parsed_email = emailparse.split_forward_chain(emailparse.clean_body(body)) if body else emailparse.ParsedEmail()
    parsed = commands.parse_email(subject, parsed_email.typed_text)
    guess = emailparse.resolve_deal_name(
        explicit=str(parsed.get("action_arg") or ""),
        subject=parsed.leftover,
        parsed=parsed_email if body else None,
    )

    config = Config.load()
    target = config.target_for(parsed.action or "")

    print(f"subject   : {subject}")
    print(f"action    : {parsed.action or '(none — nothing would happen)'}")
    if target is not None:
        print(f"board     : {target.label} ({target.board_id or 'board id unset'})")
    if guess:
        print(f"name      : {guess.name}   (from the {guess.source})")
    else:
        print("name      : REFUSED — reads like a sentence, nothing would be added")
        print("            resend as: !%s Hero" % (parsed.action or "addp"))
    if guess.alternatives:
        print(f"            alternatives: {', '.join(guess.alternatives)}")
    print(f"leftover  : {parsed.leftover!r}")
    if parsed.unknown:
        print(f"unknown   : {', '.join(parsed.unknown)}")
    for warning in parsed.warnings:
        print(f"warning   : {warning}")

    if body:
        print(f"\nchain hops: {len(parsed_email.segments)}")
        inner = parsed_email.innermost
        if inner:
            print(f"founder   : {inner.sender_name} <{inner.sender_email}>")
        print(f"contacts  : {emailparse.extract_contacts(parsed_email)}")
    return 0


def _build_intake(config: Config, dry_run: bool):
    from .gmail_client import GmailClient
    from .monday_client import MondayClient

    gmail = GmailClient(config.credentials_file, config.token_file)
    clients = {}
    if config.monday_token:
        for key, target in config.targets.items():
            if target.board_id:
                clients[key] = MondayClient(
                    config.monday_token, target.board_id, config.api_version
                )
    if not clients and not dry_run:
        raise SystemExit("MONDAY_API_TOKEN and the board ids are required. Run `check`.")
    return gmail, Intake(config, gmail, clients, dry_run=dry_run)


def chronological(messages: list[dict]) -> list[dict]:
    """Oldest first, by actual send time.

    Gmail's search returns newest-first. Monday's activity feed shows the
    newest-CREATED update first, so processing in Gmail's order creates the
    older email's updates most recently — pushing it above a genuinely newer
    email in the feed. Sorting here first means updates get created in the
    same order the mail actually arrived, so Monday's newest-first display
    lines up with reality instead of running backwards.
    """
    return sorted(messages, key=lambda m: int(m.get("internal_date") or 0))


def run_once(config: Config, dry_run: bool, limit: int) -> int:
    gmail, intake = _build_intake(config, dry_run)
    state = State.load(config.state_file)

    # Widen the search to cover any downtime, so mail that arrived while the
    # script was stopped does not age out of the window and get lost.
    window = state.lookback_days(config.lookback_days, config.max_lookback_days)
    if window > config.lookback_days:
        LOG.warning(
            "last run was %d days ago — searching back %d days to catch up",
            state.gap_days(), window,
        )
    query = config.search_query(window)
    LOG.info("Searching: %s", query)

    to_process = []
    fetched = 0
    for message_id in gmail.search(query, limit=limit):
        fetched += 1
        if state.seen(message_id):
            LOG.debug("· already handled in a previous pass: %s", message_id)
            continue
        to_process.append(gmail.get_message(message_id))
    to_process = chronological(to_process)

    handled = 0
    for message in to_process:
        message_id = message["id"]
        subject = message.get("subject", "")[:70]
        result = intake.process(message)
        handled += 1

        if result.status == "done":
            LOG.info("✓ %-22s → %-16s %s", result.deal_name, result.board, subject)
        elif result.status == "failed":
            LOG.error("✗ %-22s %s — %s", result.deal_name or result.action, subject, result.error)
        else:
            LOG.debug("· skipped: %s (%s)", subject, "; ".join(result.notes))

        for note in result.notes:
            LOG.debug("    %s", note)
        for warning in result.warnings:
            LOG.warning("    %s", warning)

        if dry_run:
            continue

        # Record it before labelling: if the label call fails, the local state
        # still stops the message being written to Monday a second time.
        state.mark(message_id)
        state.save()

        label = {
            "done": config.label_done,
            "failed": config.label_failed,
        }.get(result.status, config.label_skipped)
        try:
            gmail.add_label(message_id, label)
        except Exception as exc:
            LOG.error("could not label %s: %s", message_id, exc)

        should_reply = (result.status == "done" and config.reply_on_success) or (
            result.status == "failed" and config.reply_on_error
        )
        if should_reply:
            sent_id = gmail.reply(
                message,
                confirmation_text(result, subject),
                subject=confirmation_subject(result),
            )
            # Belt and braces: the reply is already excluded by -in:sent and by
            # its marker header, but labelling it means even a hand-edited
            # INTAKE_SEARCH_QUERY cannot pull it back in.
            if sent_id:
                try:
                    gmail.add_label(sent_id, config.label_done)
                except Exception as exc:
                    LOG.debug("could not label sent reply: %s", exc)

    if not dry_run:
        state.finish_pass()
        state.save()
    LOG.info("Looked at %d message(s).", handled)

    # Never leave mail behind without saying so. On a manual catch-up run this
    # is the difference between "done" and "silently skipped the rest".
    if fetched >= limit:
        LOG.warning(
            "Stopped at the --limit of %d; there may be more waiting. "
            "Run again to continue.",
            limit,
        )
    return 0


def cmd_run(args) -> int:
    config = Config.load()
    if not args.dry_run:
        problems = config.validate()
        if problems:
            for problem in problems:
                LOG.error(problem)
            return 1
    return run_once(config, args.dry_run, args.limit)


def cmd_watch(args) -> int:
    config = Config.load()
    problems = config.validate()
    if problems and not args.dry_run:
        for problem in problems:
            LOG.error(problem)
        return 1

    LOG.info("Watching every %ss. Ctrl-C to stop.", config.poll_seconds)
    while True:
        try:
            run_once(config, args.dry_run, args.limit)
        except KeyboardInterrupt:
            LOG.info("Stopped.")
            return 0
        except Exception as exc:  # keep the watcher alive through transient faults
            LOG.exception("poll failed: %s", exc)
        try:
            time.sleep(config.poll_seconds)
        except KeyboardInterrupt:
            LOG.info("Stopped.")
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="intake", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("auth", help="one-time Google authorisation").set_defaults(func=cmd_auth)
    subparsers.add_parser("check", help="validate configuration").set_defaults(func=cmd_check)
    inspect_parser = subparsers.add_parser(
        "inspect-board", help="list Monday columns and groups for both boards"
    )
    # No choices= here: argparse's own choice-checking misbehaves on the
    # empty-list default nargs="*" produces when no target is given ("invalid
    # choice: []"). cmd_inspect_board already validates each name manually.
    inspect_parser.add_argument(
        "target", nargs="*",
        help="limit to one or more boards: deal, investor (default: both)",
    )
    inspect_parser.set_defaults(func=cmd_inspect_board)

    parse_parser = subparsers.add_parser("parse", help="offline: show how a subject is read")
    parse_parser.add_argument("subject", nargs="+")
    parse_parser.add_argument("--body-file", help="optional file with the email body")
    parse_parser.set_defaults(func=cmd_parse)

    for name, func, helptext in (
        ("run", cmd_run, "process new mail once"),
        ("watch", cmd_watch, "process new mail on an interval"),
    ):
        sub = subparsers.add_parser(name, help=helptext)
        sub.add_argument("--dry-run", action="store_true", help="change nothing, just report")
        sub.add_argument(
            "--limit", type=int, default=200,
            help="max messages per pass (default 200; sized for a manual catch-up)",
        )
        sub.set_defaults(func=func)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
