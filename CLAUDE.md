# Orientation for Claude

Read this before changing anything. It is mostly *why*, not *what* — the code
is readable on its own, but several parts look over-engineered until you know
which real bug produced them. Nearly every rule below exists because something
broke in production first.

## If someone has just handed you this project

This repo was handed over by its previous owner, most likely as a ZIP dropped
into a chat. **Assume the person you are talking to is not a developer.** They
may never have opened Terminal.

Walk them through [`SETUP.md`](SETUP.md) conversationally — one step at a
time, waiting for them to confirm each before moving on. Do not paste the
whole guide at them, and do not assume they know what a terminal, a directory,
an API key, an environment variable or a cron job is. When you give a command,
say plainly what it does and what they should expect to see afterwards.

Four things trip up nearly everyone, in this order:

1. **The Google OAuth app must be Published**, not left in Testing — otherwise
   Google silently kills the token after exactly 7 days and the whole thing
   stops with no error anywhere.
2. **The sender's address must be in `INTAKE_ALLOWED_SENDERS`.** This is the
   number one reason a test email appears to do nothing at all.
3. **`python3`, not `python`** on macOS.
4. **The Google client secret is only shown once**, in the popup right after
   creating the client. If they missed it, tell them to delete that client and
   make a new one rather than hunting for it.

Have them run `python3 -m intake.cli run --dry-run` before anything writes to
Monday — it reads their real mail and prints what it *would* do, changing
nothing. It is the safest possible first test.

The previous owner has left the company. Nothing here is tied to them; the
mailbox, Google project and Monday token are all set up fresh by the new owner
during setup.

## What this is

A Gmail → Monday.com intake script for a VC deal pipeline. Someone forwards a
startup or investor email to a watched mailbox with a command in the subject
(or anywhere in the body), and it becomes a board item with the email attached
as an update.

```
!addp Larkspur!      → startup       → deal pipeline board
!addi Northwind Co!  → investor lead  → investor board
!note çok iyi!       → extra text as its own separate update
```

Runs as a cron job every 15 minutes. `SETUP.md` covers installation.

## First: run the tests

```bash
python3 -m unittest discover -s tests -t .
```

153 tests, no network, under a second. They encode almost every rule below —
if you break an invariant you will usually see it here rather than in
production. Run them before and after any change.

`python3 -m intake.cli parse "<subject>"` shows how any subject line parses,
offline, without touching Gmail or Monday. Use it liberally.

## Module map

| File | Owns |
|---|---|
| `commands.py` | The `!command` grammar: registry, tokenising, argument extraction |
| `emailparse.py` | Forward-chain splitting, name resolution, contacts, attachment filtering |
| `textnorm.py` | Turkish-safe case folding, company-name normalisation |
| `config.py` | `.env` loading, per-board targets, the Gmail search query |
| `gmail_client.py` | OAuth, search, MIME walking, labels, replies |
| `monday_client.py` | GraphQL, column encoding, file uploads |
| `pipeline.py` | Orchestration: email → board actions |
| `state.py` | Last-run watermark, downtime catch-up, crash safety |
| `cli.py` | `auth` `check` `inspect-board` `parse` `run` `watch` |

Flow: `cli.run_once` → fetch + sort → `pipeline.Intake.process` →
`commands.parse_email` → `emailparse.resolve_deal_name` → dedupe → Monday
writes → label + confirmation reply.

---

## Invariants — do not break these

**The allowlist is the security boundary.** Only `INTAKE_ALLOWED_SENDERS` /
`INTAKE_ALLOWED_DOMAINS` mail is ever acted on, enforced both in the Gmail
query and again in `pipeline.process`. Without it, anyone who learns the
address could write to the CRM by putting `!addp` in a subject. Never weaken
this for convenience.

**Text between bangs is verbatim.** `!addp ÇOK Güzel A.Ş.!` must produce
exactly `ÇOK Güzel A.Ş.` — final dot intact. Only surrounding whitespace is
stripped. Three separate "helpful" cleanups were removed from this path
(trailing-punctuation trimming, descriptive-word trimming, case fixing)
because each silently corrupted names the user typed deliberately. If the
sender wrote it inside the delimiters, they meant it.

**Never invent a name from prose.** With a bare `!addp`, the subject may
become the name — but only if it looks like a name. `got a startup named hero
- thanks` is a sentence, so nothing is written and the sender gets a reply
asking them to type the name. A wrong item in the CRM is worse than a bounced
email. Guards live in `_looks_like_name`: word count, a required capital, a
prose-word list (English + Turkish), and a generic-business-word list.

**Turkish folding is not optional.** `"İ".lower()` is `"i̇"` (i + combining
dot), not `"i"`, which breaks every case-insensitive comparison. `textnorm.fold`
translates explicitly before lowering. This is why `!EKLE`, `!ekle` and `!İSİM`
all resolve, and why `Acme A.Ş.` deduplicates against `ACME?`. Do not
replace it with `.lower()` or `.casefold()`.

**No mail loops.** A confirmation reply quotes the original subject, commands
and all, and the mailbox owner is usually on the allowlist — so a reply could
be read as a fresh instruction and answered forever. Three independent guards:
`-in:sent` in the query, an `X-Monday-Intake` header refused on the way in,
and the reply is labelled `Monday/Done` as it is sent. The same header check
also refuses vacation responders and bulk mail. Keep all three.

**Only `!addp` / `!addi` create items.** `!note` alone must do nothing — it
has no item to attach to. An item name comes only from an action command.

## Ordering and numbering (both were real bugs)

**Notes are posted before the email update, deliberately.** Monday's activity
feed shows the newest-*created* update first. Posting the email last puts it
on top, with its notes beneath — matching reading order. Reversing this looks
harmless and puts the feed backwards.

**Messages are processed oldest-first** (`cli.chronological`). Gmail's search
returns newest-first; processing in that order creates the older email's
updates most recently, pushing it *above* a newer email in the feed. Two
forwards a minute apart came out in the wrong order in production because of
this.

**Note numbering counts from Monday, not locally**
(`monday_client.count_existing_notes`). First note ever on an item is plain
`📝 Note`; every one after is `📝 Note Update N`, continuing across separate
emails sent weeks apart. Local counting restarts at 1 on every message, which
was the bug.

## Parsing subtleties

**`!note` may span lines; `!addp` / `!addi` may not.** A note is free-text and
legitimately runs to several paragraphs, so its closing `!` is searched for
across newlines. A company name is never multi-line, so allowing it there
would let a stray `!` three paragraphs down swallow half the email. This
asymmetry is intentional — see `_extract_arg` and `_bare_cut`.

**`_real_matches` filters phantom tokens.** `"!note text!\nBegin forwarded
message:"` matches `TOKEN_RE` twice: the real `!note`, and a bogus
`"!" + newline + "Begin"`. The bogus one is correctly unrecognised, but its
mere presence in the match list shifted where the real note's closing `!` was
allowed to end, truncating it and leaving a stray `!` in the output. Matches
that are both unrecognised *and* preceded by whitespace are excluded. Removing
this filter reintroduces the truncation.

**`typed_text` is always a literal prefix of `body_text`.** That is how it is
captured. Displaying both sections without removing the shared prefix shows
the same text twice — `_build_update_body` strips it after both have had
commands removed.

**Commands are stripped from displayed content** (`commands.strip_commands`).
Raw `!addp X! !note Y!` is operational syntax, not deal content, and the note
text would otherwise appear twice: once raw in the email body, once in its own
update.

## Other behaviour worth knowing

- **Deduplication is per-board.** The same company can be a portfolio prospect
  *and* a potential LP without the two interfering. Matching is on a normalised
  name (`monday_client.match_item`).
- **Contact columns are written on creation only**, so a later forward cannot
  overwrite something a human has since corrected on the board.
- **Signature logos are filtered out** by their `cid:` reference in the HTML
  (`image001.png`…), so only real documents are attached.
- **Attachments go to two places** when `MONDAY_*_COL_PITCH_DECK` is mapped:
  the activity feed *and* the file column. Independent — one failing never
  blocks the other.
- **Downtime is covered.** `state.json` records the last complete pass and
  widens the next search to cover the gap (capped by
  `INTAKE_MAX_LOOKBACK_DAYS`), so mail cannot age out of a fixed 7-day window
  while the script is off. The window never narrows below the configured value.
- **Long tracking URLs are shortened to their host** for display; the `href`
  keeps the full URL so links still work.

## Deliberately absent

- **More commands.** It was cut from ~15 to three on purpose. The pitch is
  "remember two things"; every addition erodes it. The registry and argument
  machinery are general, so adding one back is a single `COMMANDS` entry — but
  ask first.
- **Two-way sync with Monday**, **deck content parsing**, **LinkedIn scraping**.

## Secrets

`.env`, `credentials.json`, `token.json`, `state.json` and `*.log` are
gitignored and have never been committed — verified against full history. If
you add config, put the secret in `.env` and a placeholder in `.env.example`.
Never commit a real token, and never print one in logs.

## Known gaps

1. **Silent failure** — if the OAuth token expires or cron breaks, nothing
   reports it. A dead-man's-switch alert is the highest-value next addition.
2. **Single-machine dependency** — only runs while that laptop is awake.
   GitHub Actions or a small VPS would make it genuinely always-on.
3. **Spam** — Gmail search excludes spam by default and this is not
   overridden, so a forward landing there is never seen.
