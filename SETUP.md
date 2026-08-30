# Setup — step by step

Written for someone who has never used Terminal. Follow it top to bottom.
About 20 minutes, most of it waiting on Google's website.

**You will need:**
- The Google account of the mailbox that receives the forwarded emails
- Access to the Monday boards
- A Mac (these instructions use the Mac Terminal)

---

## How to use Terminal

You will be asked to "run" commands. That always means the same thing:

1. Open **Terminal** (press `Cmd + Space`, type `Terminal`, press Enter)
2. Copy the grey block from this guide
3. Paste it into Terminal (`Cmd + V`)
4. Press **Enter**

That is all "running a command" means. If nothing visibly happens, that
usually means it worked — Terminal is quiet when things go well.

---

## Step 1 — Put the code somewhere permanent

You were probably given this as a **ZIP file**.

1. Double-click the ZIP to unzip it
2. **Check the folder's name.** It is often something like
   `monday-intake-main` rather than just `monday-intake`. If so, rename it to
   exactly **`monday-intake`** (click the name once, then type the new one) —
   otherwise every command below will say "no such file or directory".
3. Move that `monday-intake` folder into your **home folder** — the one with
   your name on it in Finder's sidebar
4. Open Terminal and run:

```bash
cd ~/monday-intake
ls
```

You should see a list including `README.md`, `intake`, and `requirements.txt`.

> **"no such file or directory"?** The folder isn't in your home folder, or
> its name doesn't match. Redo points 2 and 3.

> **Every command in this guide assumes you are inside that folder.** If you
> close Terminal and come back later, always run `cd ~/monday-intake` first.

> *If you were given a Git repository instead of a ZIP, run
> `git clone <the address you were given>` then `cd monday-intake`.*

## Step 2 — Install the parts it needs

```bash
pip3 install -r requirements.txt
```

You will see a lot of text scroll past. That is normal. If you see a yellow
warning about "urllib3" or "LibreSSL", ignore it — it is harmless.

## Step 3 — Get permission to read the mailbox (Google)

This is the longest step. You are creating a "key" that lets the script read
one specific Gmail account.

1. Go to <https://console.cloud.google.com>
2. At the top, click the project dropdown → **New Project**. Name it anything
   (e.g. `monday-intake`) → **Create**
3. In the search bar at the top, search **Gmail API** → click it → **Enable**
4. In the left menu go to **Google Auth Platform → Audience**
   - Set **User type** to **External**
   - Fill in the app name and your email where asked, and save
5. **On that same page, click "Publish app".** ⚠️ **Do not skip this.**
   If you leave it unpublished, Google stops the script from working after
   exactly 7 days, with no warning.
6. Go to **Google Auth Platform → Clients → Create client**
   - **Application type: Desktop app**
   - Name it anything → **Create**
7. A popup appears showing a **Client ID** and **Client secret**, with a
   download button. **Download it now** — this popup is the only reliable
   place to get it.
8. Find the downloaded file (usually in `Downloads`, named something like
   `client_secret_1234....json`). Run this to move and rename it:

```bash
mv ~/Downloads/client_secret_*.json ~/monday-intake/credentials.json
```

> **Missed the popup?** Don't hunt for it — just delete that client and create
> a new one (repeat 6–7). Takes 30 seconds.

### Is publishing safe?

Yes. This is a "Desktop app" key. The only way anyone gets access is by
signing in with **their own** Google account and clicking Allow — which gives
access to **their own** mailbox, not yours. Nobody can reach your email
without your password. You will still see a scary-looking "unverified app"
warning when you sign in; that is normal for a private script.

## Step 4 — Get the Monday key

This is a password-like code that lets the script add items to your boards.

1. Open Monday in your browser and sign in
2. Click your **profile picture** in the **bottom-left** corner
3. Click **Developers** in the menu that appears
   - A new tab opens called "Developer Center"
4. In its left menu, click **My access tokens**
5. Click **Show** (or **Generate**, if it is your first time) and **copy** the
   long string of letters and numbers

**Treat this like a password.** Anyone with it can read and change your
Monday boards. Do not email it, paste it into a chat, or put it in any file
other than `.env` (which is set up to never leave your computer).

> **No "Developers" option?** Your Monday account may not have permission.
> Ask whoever administers your Monday workspace to either grant it or generate
> a token for you.

You will also need your two **board IDs**. Open each board in Monday and look
at the web address — the long number is the ID:

```
https://yourcompany.monday.com/boards/18146507025
                                      ^^^^^^^^^^^ this part
```

You need one for startups (the deal pipeline) and one for investor leads.

## Step 5 — Fill in the settings file

```bash
cp .env.example .env
open -e .env
```

This opens a text file in TextEdit. Fill in these lines:

- **`MONDAY_API_TOKEN=`** — paste the Monday key from Step 4 right after the `=`
- **`INTAKE_ALLOWED_SENDERS=`** — the email addresses allowed to add items,
  separated by commas with no spaces.
  ⚠️ **This is the #1 reason a test email "does nothing".** If an address is
  not on this list, the script ignores it completely. Add your own address
  here for testing.
- **`INTAKE_ALLOWED_DOMAINS=`** — put your company domain here (e.g.
  `yourcompany.com`) to let the whole team forward directly.
- **`MONDAY_DEAL_BOARD_ID=`** — the board ID for startups (from Step 4)
- **`MONDAY_INVESTOR_BOARD_ID=`** — the board ID for investor leads

⚠️ The two board IDs, and `MONDAY_DEAL_COL_PITCH_DECK`, ship pre-filled with
the values from the setup this was built for. **If you are using the same
Monday boards, leave them alone.** If your boards are different, replace the
IDs and clear `MONDAY_DEAL_COL_PITCH_DECK` (Step 7 shows you the real column
IDs for your own boards).

Everything else can stay blank. Those optional `MONDAY_*_COL_*` lines only
control whether contact details are copied into board *columns*; without them
you still get items, updates and attachments.

Save with **Cmd + S**, then close the window.

> **Worried you'll get this wrong?** You can't break anything here. Step 7
> checks every value and tells you exactly what to fix, and Step 8 lets you
> test without writing to Monday at all.

## Step 6 — Connect to Gmail (one time only)

```bash
python3 -m intake.cli auth
```

Your browser opens.

1. Sign in as the mailbox that receives the forwards
2. You will see **"Google hasn't verified this app"** → click **Advanced** →
   click **Go to (your app name)**
3. Click **Allow**

Back in Terminal you should see:

```
Authorised. Token saved to token.json
```

> **If you see `403: access_denied`** — the app is still unpublished. Go back
> to Step 3.5 and click **Publish app**, then run this command again.

## Step 7 — Check everything is connected

```bash
python3 -m intake.cli check
```

You want to see **"Configuration looks usable."** at the bottom. If it lists
problems instead, it tells you exactly which line in `.env` to fix.

Then:

```bash
python3 -m intake.cli inspect-board
```

This proves the Monday key works. You should see both board names and a long
list of their columns.

## Step 8 — Test it without touching Monday

Send yourself an email — **from an address you put in `INTAKE_ALLOWED_SENDERS`**
— with this subject:

```
test !addp Test Company!
```

Wait about 10 seconds, then run:

```bash
python3 -m intake.cli run --dry-run
```

`--dry-run` means "show me what you would do, but don't actually do it." You
should see a line mentioning **Test Company**.

If it worked, do it for real:

```bash
python3 -m intake.cli run
```

Now check your Monday board — "Test Company" should be there. You should also
get a confirmation email back with a link. Delete the test item afterwards.

## Step 9 — Make it run by itself

Right now it only runs when you type the command. To have it check every 15
minutes on its own:

```bash
export EDITOR=nano
crontab -e
```

A basic text editor opens. Paste this **one line** (change `yourname` to your
Mac username — run `whoami` in another Terminal window if unsure):

```
*/15 * * * * cd /Users/yourname/monday-intake && /usr/bin/python3 -m intake.cli run >> intake.log 2>&1
```

Save with **Ctrl + O**, press **Enter**, then exit with **Ctrl + X**.

Confirm it saved:

```bash
crontab -l
```

You should see your line printed back.

**That's it — it now runs on its own.** You can close Terminal; this keeps
working. It pauses while the Mac is asleep and catches up automatically when
it wakes.

---

## Everyday use

**Is it working?**

```bash
cd ~/monday-intake
tail -30 intake.log
```

New timestamps that you didn't trigger yourself = it's running fine.

**Pause it** (uses no battery at all while paused):

```bash
crontab -l | sed '/intake.cli run/ s/^/#/' | crontab -
```

**Start it again:**

```bash
crontab -l | sed '/intake.cli run/ s/^#//' | crontab -
```

**Check which state it's in:**

```bash
crontab -l
```

A `#` at the start of the line means paused.

**Run it right now** without waiting for the 15-minute mark:

```bash
cd ~/monday-intake && python3 -m intake.cli run
```

---

## If something breaks

**"command not found: python"** — use `python3` instead of `python`.

**A forwarded email did nothing** — check, in order:
1. Was the sender's address in `INTAKE_ALLOWED_SENDERS`? (most common cause)
2. Did the email actually contain `!addp` or `!addi`?
3. Did it land in Spam? The script never looks in Spam.

**It stopped working after about a week** — the Google app was left
unpublished. Step 3.5.

**Nothing in `intake.log` for hours** — the Mac was asleep, or the schedule is
paused. Run `crontab -l` and look for a `#`.

---

## Known gaps, honestly

1. **It fails silently.** If the Google key expires or the schedule breaks,
   nothing tells you — emails keep arriving and quietly do nothing. Adding a
   "warn me if it hasn't run in 6 hours" alert is the most valuable next
   improvement.
2. **It only runs while that one Mac is awake.** Moving it to a always-on
   server (GitHub Actions is free) would fix this properly.
3. **Emails in Spam are never seen.**
