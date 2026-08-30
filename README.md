# Gmail → Monday deal intake

Watches a Gmail inbox for forwarded emails carrying a command in the subject
line, and files them onto the right Monday board.

**New here?** → [`SETUP.md`](SETUP.md) to install it,
[`COMMANDS.md`](COMMANDS.md) for the team-facing cheat sheet,
[`CLAUDE.md`](CLAUDE.md) if you are an AI agent picking this up.

Two commands, two boards:

| Command | Goes to | Board |
|---|---|---|
| `!addp` | startup → deal pipeline | `18146507025` |
| `!addi` | investor lead → investor board | `18146373310` |

```
Someone hits Forward, types a command in the subject, sends it to the
watched mailbox
        │
        ▼
FW: Application - ACME !addp
        │
        ▼
Deal pipeline board: item "ACME" created
        ├─ the whole email chain posted as an update
        ├─ ACME One-pager.pdf attached (signature logos filtered out)
        ├─ founder's email + phone + website filled into columns
        └─ confirmation replied to the sender with the item link
```

`!addi` does exactly the same thing against the investor board instead.

This replaces the manual step of forwarding to Monday's email-to-board address,
which can only ever use the raw subject as the item name and cannot set columns,
deduplicate, or attach the founder's contact details.

---

## Why a command grammar

The requirement was that a command works wherever it lands in the subject,
because nobody wants to think about cursor position while forwarding. So the
parser does not anchor: it scans the whole subject for `!token`, and whatever
text is left over becomes the fallback deal name.

The closing `!` is what makes this work. `!addp hockey!` needs no guessing
about where the name ends, so a command can sit inside a sentence:

```
!addp hockey!                                          → "hockey"
!addp Larkspur AI!                                 → "Larkspur AI"
FW: bi girisim var !addp Hero! bakar misin             → "Hero"
FW: Application - ACME !addp                          → "ACME" (from the subject)
!addp FW: Application - ACME                          → "ACME" (from the subject)
```

Commands are looked for in the subject, then in the text the sender typed at
the top, then **anywhere else in the message** — people scribble a command next
to the paragraph it refers to, halfway down a forward, and expect it to count.
When a command is found buried in the body the confirmation reply says so, so a
stray `!addp` inside someone else's text is visible rather than silent.

An item name only ever comes from `!addp` or `!addi`. `!note text!` rides along
and posts as its own separate update on the same item — but on its own it does
nothing, because there is no item for it to attach to:

```
!addp hockey! !note startup güzelmiş!
    → item "hockey", the email as one update, "startup güzelmiş" as another
```

Turkish aliases work throughout: `!ekle` `!girisim` `!startup` for `!addp`,
`!yatirimci` `!lead` `!inv` for `!addi`, `!not` `!yorum` for `!note`.

Four things make that safe in practice:

- **A closed registry.** Only known words count. `Merhaba!` and `ACME?` are
  never commands; `!addpp` is reported back as a probable typo rather than
  silently doing nothing.
- **A closing `!` ends the argument, and what is inside it is taken verbatim.**
  No word caps, no boundary guessing, and no tidying either: case, punctuation
  and inner spacing are the sender's choice. `!addp Hero deck!` stays
  "Hero deck", `!addp ÇOK Güzel A.Ş.!` keeps its final dot.
- **Forgetting the closing `!` degrades sensibly.** Without it, a name takes the
  leading run of capitalised words and stops at the first lowercase one, so
  `!addp Hero bakalım` is "Hero" and `!addp Larkspur AI` keeps all three. A
  lowercase word on its own is still taken, so `!addp lumen` works. A `FW:` or
  `RE:` in the way stops it dead — the forwarded subject is never eaten.
- **It refuses to guess from prose.** With a bare `!addp` the subject becomes
  the name — but only if it reads like a name. `got a startup named hero -
  thanks` is a sentence, so nothing is written and the reply asks for
  `!addp Hero!`. A name given between bangs is trusted and skips every check.
- **Nothing is silent.** Every processed mail gets a reply saying which board it
  landed on, what name was used and where that name came from, with a link.

The registry is deliberately two entries long. Adding a command back — a stage
setter, an owner assignment — is one entry in `COMMANDS` in `intake/commands.py`;
the parser, aliasing and argument handling are already general.

See [COMMANDS.md](COMMANDS.md) for the bilingual cheat sheet to send to the team.

## What it handles that a naive script would not

Everything below was found by reading the actual forwards in this inbox rather
than guessing:

- **Turkish and English forward headers.** Outlook writes `Gönderen:/Gönderildi:/Kime:/Konu:`
  or `From:/Sent:/To:/Subject:`; Apple Mail writes `Begin forwarded message:` or
  `İleti başlangıcı:`; Gmail writes `---------- Forwarded message ----------`.
  All four are parsed.
- **Nested forwards.** `FW: FW- Growth investment – Northwind Co` — the chain is
  split into hops and the *innermost* sender is treated as the founder, so the
  contact recorded is the founder and not the colleague who forwarded it.
- **The Turkish dotted i.** `"İ".lower()` in Python is not `"i"`, which quietly
  breaks every case-insensitive comparison. Folding is explicit, so `!EKLE`,
  `!ekle` and `!İSİM` all resolve, and `Acme A.Ş.` deduplicates against `ACME?`.
- **Signature logos.** Outlook signatures arrive as `image001.png`…`image004.png`.
  They are detected by their `cid:` reference in the HTML and dropped, so only
  the real deck lands on Monday.
- **Duplicates.** A follow-up forward finds the item the first one created and
  posts an update to it instead of creating a second row. Deduplication is
  per-board, so the same company can be a portfolio prospect *and* a potential
  investor without the two interfering.

---

## Setting it up

**Never used Terminal? Follow [`SETUP.md`](SETUP.md)** — it is written for a
complete beginner and explains every step, including what should appear on
screen after each one. The summary below is the same path in short form.

1. **Put the folder in your home folder** and open Terminal there:
   `cd ~/monday-intake`
2. **Install what it needs:** `pip3 install -r requirements.txt`
3. **Create a Google key** so it can read the mailbox — Google Cloud →
   new project → enable **Gmail API** → **Publish app** → create a
   **Desktop app** client → download the JSON as `credentials.json` into
   this folder.
   ⚠️ Publishing is not optional: skip it and Google silently cuts access
   after 7 days.
4. **Get your Monday key:** Monday → profile picture → Developers →
   My access tokens.
5. **Fill in the settings:** `cp .env.example .env` then `open -e .env`.
   Paste the Monday key into `MONDAY_API_TOKEN`, and list every address
   allowed to add items in `INTAKE_ALLOWED_SENDERS`.
   ⚠️ An address missing from that list is the usual reason a test email
   appears to do nothing.
6. **Connect to Gmail (once):** `python3 -m intake.cli auth` — a browser opens;
   accept the "unverified app" warning and click Allow.
7. **Check it:** `python3 -m intake.cli check` should end with
   "Configuration looks usable."
8. **Try it safely:** email yourself with the subject `test !addp Test Company!`,
   then run `python3 -m intake.cli run --dry-run` — this shows what it *would*
   do without changing anything. Drop `--dry-run` to do it for real.
9. **Make it automatic:** add a cron job so it checks every 15 minutes without
   you. Exact line and pause/resume commands are in [`SETUP.md`](SETUP.md).

Day to day:

```bash
python3 -m intake.cli run             # check for new mail now
python3 -m intake.cli run --dry-run   # ...but change nothing
python3 -m intake.cli check           # is the configuration valid?
python3 -m intake.cli inspect-board   # list Monday columns and groups
python3 -m intake.cli parse "<subj>"  # how would this subject be read?
tail -30 intake.log                   # what has it been doing?
```


## Is there any risk to the Gmail account?

**No, and you do not need a second Gmail account for this.**

- It uses the **official Gmail API with OAuth**, which is the sanctioned way to
  automate a mailbox. There is no password anywhere, and no IMAP scraping —
  scraping is what actually gets accounts flagged.
- One scope, `gmail.modify`: read messages, apply labels, send replies. It
  cannot permanently delete anything.
- **Quota is a non-issue.** Gmail allows a billion quota units per day; reading
  a message costs 5. Polling every 2 minutes uses a rounding error's worth.
- **Sending** is the only part with a meaningful cap — 500 messages/day on a
  free account. This sends a handful of confirmation replies a day. Set
  `REPLY_ON_SUCCESS=false` if you would rather it stayed silent.

The one thing that genuinely bites people is step 1.4 above: leaving the OAuth
consent screen in *Testing* mode, where refresh tokens die after 7 days. Publish
the app and the token lasts until you revoke it.

If you do decide to use a separate address later, nothing in the code assumes an
account — point `.env` at the new one and run `auth` again.

---

## Safety properties

- **Allowlist.** Only mail from `INTAKE_ALLOWED_SENDERS` / `INTAKE_ALLOWED_DOMAINS`
  is ever acted on, enforced both in the Gmail query and again before anything is
  written. A stranger who learns the address cannot put `!addp` in a subject and
  write into the pipeline.
- **Commands are only read from the sender's own text** — the subject, and the
  lines they typed above the forwarded content. A founder writing `!pass` inside
  their pitch cannot drive the board.
- **Explicit by default.** No command means nothing happens. `DEFAULT_COMMAND`
  exists if you want every forward from the team to become a deal, but it is off.
- **Idempotent.** Each message is labelled `Monday/Done`, `Monday/Failed` or
  `Monday/NoCommand` once handled, and those labels are excluded from the search
  query, so a crash mid-run cannot double-post.
- **No mail loops.** A confirmation reply quotes the original subject, commands
  included, and the mailbox owner is normally on the allowlist — so a reply
  could otherwise be read as a fresh instruction and answered forever. Three
  independent guards prevent it: sent mail is excluded from the query, every
  reply carries an `X-Monday-Intake` header that is refused on the way back in,
  and the reply is labelled `Monday/Done` as it is sent. Vacation responders and
  bulk mail are refused by the same check.

Because the query matches every recent mail from allowed senders, ordinary
conversation from the team gets labelled `Monday/NoCommand` and skipped. That is
just bookkeeping in your own mailbox, and it is what keeps each message from
being re-examined forever.

On a busy inbox, set `INTAKE_TO_ADDRESS` to a plus-alias and have the team
forward there instead:

```
INTAKE_TO_ADDRESS=youraddress+monday@gmail.com
```

Gmail delivers `you+anything@gmail.com` to the same mailbox with no setup, but
the script then only matches mail addressed to that alias — ordinary team mail
is never examined, never downloaded and never labelled. Pair it with a Gmail
filter on the same address to auto-label and skip the inbox, and the intake
lane stays out of the way entirely. A second Google account buys nothing over
this: Gmail search is server-side and indexed, so inbox size costs neither time
nor quota.

### Downtime and catch-up

The Gmail search is time-boxed (`newer_than:7d`). While the script is running
that is safe — the window follows the clock, so the sender going quiet for a week
and then sending changes nothing. It stops being safe the moment the *script*
is down for longer than the window: anything that arrived meanwhile would age
out and never be looked at again.

So `state.json` records when the last complete pass finished, and the next run
widens its search to cover the gap — off for three weeks means the next start
searches back 22 days, capped by `INTAKE_MAX_LOOKBACK_DAYS`. The window never
narrows below `INTAKE_LOOKBACK_DAYS`, so recent mail is always re-examined and
the labels stop it being handled twice.

The same file records handled message ids. Labels are the primary guard, but if
the script dies between writing to Monday and applying the label, the local
record still stops that email being posted a second time. Deleting `state.json`
is safe — the script falls back to the configured window and the labels still
hold.

---

## Layout

| File | |
|---|---|
| `intake/commands.py` | Command registry and the position-independent parser |
| `intake/emailparse.py` | Forward-chain splitting, name resolution, contacts, attachments |
| `intake/textnorm.py` | Turkish-safe folding and company-name normalisation |
| `intake/monday_client.py` | GraphQL client, column encoding, file upload |
| `intake/gmail_client.py` | OAuth, search, MIME walking, labels, replies |
| `intake/pipeline.py` | Email → board actions |
| `intake/config.py` | Per-board targets, allowlist, Gmail query |
| `intake/state.py` | Last-run watermark, downtime catch-up, crash safety |
| `intake/cli.py` | `auth` `check` `inspect-board` `parse` `run` `watch` |
| `SETUP.md` | First-time installation, step by step |
| `CLAUDE.md` | Orientation for an AI agent picking this up |
| `COMMANDS.md` | Bilingual cheat sheet to hand to the team |

## Tests

```bash
python -m unittest discover -s tests -t .
```

153 tests, no network required. The fixtures are the real message shapes from
this inbox — Outlook Turkish, Outlook English, Apple Mail and Gmail forwards —
so a regression means a real forward would have broken.

`python -m intake.cli parse "<subject>"` does the same thing interactively and
is the quickest way to try out a command idea without touching Monday.
