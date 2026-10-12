# claude-cockpit

claude-cockpit is a set of command-line tools for people who use Claude Code with more than
one Claude subscription account (or with one account, to watch its usage). It gives each account
its own profile: a settings folder with its own login in the macOS keychain. It shows every
account's usage (the 5-hour window, the weekly limit and the per-model weekly limit) on one
terminal screen. And it lets you move all your running Claude Code sessions onto a different
account.

## Windows preview

Native Windows support is experimental. Start with [the Windows installation and
support guide](docs/windows.md): a per-user PowerShell installer, an isolated Python
environment, the usage dashboard, and separate Claude profile launchers. The macOS
instructions below remain unchanged.

Windows does **not** enable credential hot-swap or automatic fleet switching. A
profile login uses Claude Code's own browser flow; it does not move an existing
session to another account. Offline tests do not establish live Windows login or
terminal compatibility. See the guide for the validation checklist and limits.

Read this before you install:

- **macOS is the original supported platform.** Its tools use the macOS keychain
  (`security`), `launchd` and `open`. Windows uses the separate preview above.
- **Unofficial.** This project is not affiliated with, endorsed by or supported by Anthropic.
- **It depends on Claude Code internals** (keychain item names, the `.claude.json` file, the
  `/usage` report, two OAuth read endpoints). Any Claude Code update can change them and break
  these tools.
- **Tested with** macOS 15.7.3, Claude Code 2.1.296 and Python 3.9.6. Nothing else has been tested.

## What you see

A real capture of the screen, made with `tests/render_cockpit_states.py` from **demo accounts**
(alpha, beta, gamma, delta). Every number on it is invented. Emails are hidden by default.

```text
CLAUDE ACCOUNTS  FLEET: ••••••@••••••  (alpha)
AUTO-SWITCH  ○ OFF  no alerts, no swaps                                                                                         a = toggle
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
   ACCOUNT    EMAIL                     SESSION (5h)    WEEK (7d)       FABLE (7d)      RESETS  5h / week
 1 beta       ••••••@••••••             ░░░░░░░   4%    █░░░░░░  12%    █░░░░░░   9%    1:40am (5h) / wed 10pm
 2 gamma      ••••••@••••••             █░░░░░░   8%    ██░░░░░  22%    ███████ 100% ●  10:15pm (1h) / sun 2am
▸3 alpha      ••••••@••••••             ████░░░  62%    ███░░░░  48%    ██░░░░░  35%    11:05pm (2h) / tue 12am
 4 delta      ••••••@••••••             ███████  97%    █████░░  71%    ████░░░  64%    9:45pm (1h) / sun 11pm

→ most headroom: beta (4% / 12%)
  press 1 — moves everything (opens the normal browser sign-in)
→ for Fable: beta (91% left, resets wed 10pm)
⚠ delta is nearly out for this 5-hour window (97%) — resets 9:45pm (1h)

FORECAST  if you keep this pace, do you run out before refills?   never-runs-dry pace ≤ 3.8 pts/h  (1 pt = 1% of one account's week)
   7d pace   1.9 pts/h est.  enough until the refills
  48h pace   2.8 pts/h       enough until the refills
  24h pace   4.3 pts/h       runs dry Fri 3:24am (in 150h) · blocked 18h of next 7 days
  burn first: gamma (its weekly refill comes soonest — spending it first wastes the least)
  yesterday's forecast vs what happened: 7d -21% · 48h +16% · 24h +78%

1-4 fleet-switch (browser) · p ping cold · u undo · a auto · e emails · r refresh · q quit                                updated 0m01s ago
```

| On screen | Meaning |
|---|---|
| `FLEET:` on the top line | The account every normally started session is on right now, with its profile name in parentheses. Those sessions together are "the fleet". More under [Switching accounts](#switching-accounts). |
| `••••••@••••••` | An email address. Addresses are hidden on every launch. Press `e` to show them. |
| `AUTO-SWITCH` | What the optional watcher will do: `OFF` (nothing), `ALERTS ONLY` (it notifies you when the account in use is nearly out, but will **not** switch for you), `ARMED` (it will switch once by itself, then go back to alerts only), or `ON` with a warning that no watcher is running. |
| The number at the start of a row | The key that switches the fleet to that account. Rows are re-sorted on every refresh, so read the name before you press. |
| `▸` | This row is the fleet account. |
| `ACCOUNT` | The profile name you chose. |
| `SESSION (5h)` | How much of the 5-hour window is used. |
| `WEEK (7d)` | How much of the weekly limit (all models) is used. |
| The third meter | How much of the weekly limit for one model is used. The heading is the model name the server sends. It reads `MODEL` when accounts report different names. |
| `●` after a percentage | The server says this is the limit that binds the account right now. |
| `RESETS 5h / week` | When each window resets, in local time, as reported by the server. |
| Green, yellow, red | Under 70%, 70 to 89%, 90% and over. |
| A dash where a number should be | No data for that meter. |
| A second line under a row | Why that row's numbers should be doubted: the refresh timed out, the account is signed out, the numbers are old, or the login is close to its expiry. |
| `most headroom:` | The account with the most room in both the 5-hour and the weekly window. Only rows with fresh numbers can be recommended. |
| `for <model>:` | The best account for the per-model limit. It is a separate line on purpose: a full per-model meter does not block the other models. |
| `FORECAST` | Whether you run out before the accounts refill at your recent pace. It needs the optional watcher. |
| Bottom right | How long ago the numbers were refreshed. |

Rows are sorted with the most room on top. A row whose numbers cannot be trusted sinks to the
bottom and is left out of the recommendation. The layout changes with the width of the terminal
(at 118 and at 96 columns), but no meter is ever dropped to fit.

## Is this safe for my logins?

Short answer: by design, the worst the default setup does to an account is make it need one more
browser sign-in. The one feature that moves login tokens around is off until you turn it on.

- `install.sh` never touches the keychain.
- Every account gets its own separate login, made by Claude Code's own sign-in. Your existing
  login is not moved or copied.
- The default way to switch (`claude-switch`, or a number key in the cockpit) runs Claude Code's
  own sign-in again and then checks which account the browser really granted. It contains no
  keychain write of its own. `verify.sh` checks that on every run.
- The no-browser swap (`claude-swap`) does move login tokens between keychain items. It is tested
  and it rolls back on failure, but its first live run damaged a keychain item (a truncated
  write, since fixed and covered by a test). It ships **off**. You turn it on knowingly.
- This was built and used by one person on one Mac. No real sign-in has been tested on any other
  machine yet.

The full account, including what each command can change and what to do when something goes
wrong, is in [docs/safety.md](docs/safety.md).

## Requirements

- macOS.
- Python 3.9 or newer as `python3`. The `/usr/bin/python3` that comes with the Xcode Command Line
  Tools is enough. No third-party packages are used.
- Claude Code, installed, started and signed in at least once, so that `~/.claude.json` exists.
  A new profile starts as a copy of that file. The tools look for Claude Code at
  `~/.local/bin/claude` (where Anthropic's native installer puts it). For any other install, see
  [Settings](#settings).
- zsh as your shell. `install.sh` adds the commands to `~/.zshrc`.
- At least one Claude subscription account that you own.
- Optional: Google Chrome, if you want the sign-in page to open in the right browser profile.

## Set up in 5 minutes

1. Get the code.

   ```bash
   git clone https://github.com/christianjohnston-ai/claude-cockpit.git
   cd claude-cockpit
   ```

2. Install.

   ```bash
   bash install.sh
   ```

   It checks that this is macOS, that `python3` is 3.9 or newer, that the `security` command
   exists, and where Claude Code is. It copies the tools into `~/.claude-profiles/`. It creates
   `~/.claude-profiles/autoswitch.json` from `config/autoswitch.example.json` only if that file
   does not exist. It backs up `~/.zshrc`, then appends a marker comment and a line that loads
   `~/.claude-profiles/claude-as.sh`. If Claude Code is not at `~/.local/bin/claude` but `claude`
   is on your `PATH`, it also records that path as `COCKPIT_CLAUDE_BIN`. It never touches the
   keychain. Running it twice is safe: the second run changes nothing.

   | Flag | Effect |
   |---|---|
   | `--update` | Replace tools that already exist and differ. A copy of each replaced file is kept under `~/.claude-profiles-backups/<timestamp>/`. |
   | `--no-shell` | Do not touch `~/.zshrc`. |

   Without `--update` it refuses to overwrite an existing tool file that differs, and lists the
   files.

3. Open a **new** terminal, so the commands load.

4. Add each account. A profile name may contain letters, digits, `-` and `_`.

   ```bash
   bash ~/.claude-profiles/new-profile.sh personal
   claude-login personal
   ```

   `new-profile.sh` creates the profile folder. `claude-login` opens the browser and signs that
   profile in. The first sign-in of a new profile cannot be aimed at a browser profile, because
   the tools do not know its email address yet. Pick the right account in the browser yourself,
   then read the last line: `OK: personal is signed in as <email>`.

   Repeat for every account, **including the one you are already signed in to**. Until an account
   has a profile of its own, its only login on this Mac is the shared one, and the first switch
   to another account replaces it.

5. Open the cockpit.

   ```bash
   claude-cockpit
   ```

6. Optional: run the read-only health check.

   ```bash
   bash ~/.claude-profiles/verify.sh
   ```

## Every command

"Free" means no subscription usage is spent. A refresh runs Claude Code's `/usage` command, which
reads your usage from Anthropic and sends no message to a model.

| Command | What it does | What it costs |
|---|---|---|
| `claude-cockpit` | Live screen: every account's usage, the recommendation, the forecast, one-key switching. Refreshes every 5 minutes. | Free |
| `claude-usage` | Refreshes every account once and prints a table. | Free |
| `claude-usage --fast` | Prints the last numbers saved on disk, without refreshing. | Free |
| `claude-profiles` | Lists each profile and the account it is signed in to, then the shared default. | Free |
| `bash ~/.claude-profiles/new-profile.sh <name>` | Creates a profile folder for one more account. Refuses if the folder exists. | Free |
| `claude-login <name>` | Signs that account in to its own slot (the keychain item that holds its login). Affects no other account. | A browser sign-in |
| `claude-login --fleet` | Signs in to the shared slot without aiming or checking. Prefer `claude-switch <name>`. | A browser sign-in |
| `claude-as <name> [claude args]` | Starts one Claude Code session on that profile's own login. It ignores fleet switches. | Whatever that session uses |
| `claude-switch` | Shows which account the fleet is on, how many running sessions follow it, and each account's login state. | Free |
| `claude-switch <name>` | Fleet switch by browser sign-in: aimed at that account, then verified. | A browser sign-in |
| `claude-switch --undo` | Fleet switch back to the previous account. | A browser sign-in |
| `claude-switch --active` | Prints the profile name of the fleet account, for scripts. | Free |
| `claude-swap` | Status: who the fleet is on according to the server, where every login is, and whether any login is held twice. Works while the swap is off. | Free (see note 1) |
| `claude-swap --check` | Scan only: exits 3 if any login is held in two slots. | Free |
| `claude-swap --dry-run <name>` | Prints what a swap to `<name>` would move. Changes nothing. Works while the swap is off. | Free (see note 1) |
| `claude-swap <name>` | Opt-in. Fleet switch with no browser: moves an existing login into the shared slot and verifies it. | A few tokens |
| `claude-swap --undo` | Opt-in. Swaps back to the account the last swap moved away from. | A few tokens |
| `claude-ping` | Opens the 5-hour window on every account that has none, by sending one word. | A few tokens per account |
| `claude-ping <name> ...` | Pings the named accounts whatever their state. | A few tokens per account |
| `claude-ping --dry-run` | Shows which accounts would be pinged. Sends nothing. | Free |
| `claude-ping --fast` | Skips the refresh before deciding which accounts are idle. | A few tokens per account |
| `claude-autoswitch --status` | Shows the watcher's settings, the fleet's live numbers and which account it would pick. | Free (see note 2) |
| `claude-autoswitch --arm` | Opt-in. Arms one automatic swap at the next threshold crossing. Needs the watcher and `swap_enabled`. | Free to arm |
| `claude-autoswitch --off` | Turns alerts and auto-swap off. | Free |
| `claude-autoswitch --once [--dry-run]` | Runs one evaluation and exits. With `--dry-run` it changes nothing. Without it, it acts as the watcher would: it alerts, or swaps if armed. | Free (see note 2) |
| `claude-autoswitch` | The watcher loop itself. `launchd` runs this; you normally do not. | Free |
| `python3 ~/.claude-profiles/claude-meters.py forecast` | Computes and prints the run-out forecast. `snapshot` records the meters now. `tokens` updates the token history. | Free |
| `bash ~/.claude-profiles/verify.sh` | Read-only health check. Every line is PASS, FAIL, WARN or info. | Free |
| `bash ~/.claude-profiles/verify.sh --simulate-missing <name>` | Pretends that profile has no login, to show the check can fail. Changes nothing. | Free |

Note 1: if the fleet login (or, for a dry run, the login that would move in) is within 30 minutes
of needing renewal, `claude-swap` first sends one word through Claude Code so that Claude Code
renews it. That costs a few tokens.

Note 2: when the fleet is over the threshold, the watcher reads the usage of the other accounts.
If one of them has an expired access token it sends one word to renew it. That costs a few tokens
and opens that account's 5-hour window. `claude-autoswitch --status` always does this check.

Why `claude-ping` exists: the 5-hour window opens on an account's first message and ends five
hours later whatever you do. An idle account has no window, so its clock is not running. Pinging
idle accounts early means their windows are already counting down while you work on another one.

## Cockpit keys

| Key | What it does |
|---|---|
| `1` to `9` | Switch the fleet to that row. There is no yes/no prompt. With the swap off (the default) this opens the browser sign-in. With the swap on it tries the no-browser swap first. |
| `u` | Undo: switch the fleet back to the previous account. |
| `p` | Ping every account that has no live 5-hour window. No prompt. |
| `a` | Turn the watcher's alerts on or off (`enabled` in `autoswitch.json`). Has no effect unless the watcher is installed. It does not arm a swap: that is `claude-autoswitch --arm`. |
| `e` | Show or hide email addresses. They are hidden again on the next launch. |
| `r` | Refresh usage now. |
| `q` or `Esc` | Quit. |

The footer names the mode the number keys are in: `fleet-switch (browser)` while `swap_enabled`
is false, `fleet-switch (swap)` once you turn it on.

During a switch the cockpit hands the terminal to the switch tool, which prints real email
addresses even when the cockpit hides them. The line asking you to confirm the address on the
consent screen is a safety check.

## Switching accounts

Three words first.

- A **profile** is a folder, `~/.claude-profiles/<name>/`, that Claude Code uses as its settings
  folder for one account.
- A **slot** is the keychain item where Claude Code keeps one login.
- The **fleet** is every Claude Code process that was started the normal way: your open sessions,
  every new terminal, and background jobs. They all read one shared slot. Put a different login in
  that slot and they all follow.

Each account also keeps its own permanent login in its profile's slot. That login is the account's
**anchor**. Because the anchor exists, the login in the shared slot is always replaceable.

### The default: a browser sign-in

```bash
claude-switch work
```

1. It refuses, and changes nothing, if `work` has never signed in or has no anchor.
2. It starts Claude Code's own sign-in for the shared slot. If Chrome has a profile signed in to
   the same email address, the page opens there. The address is prefilled when your Claude Code
   version supports it.
3. You confirm on the consent screen that it shows the right address, and authorize.
4. It checks that the shared slot changed and asks Claude Code which account it now holds.
5. It reports one of five results.

| Exit code | Meaning |
|---|---|
| 0 | The fleet is on the account you asked for. |
| 1 | Refused before any sign-in. Nothing changed. |
| 2 | The sign-in window was closed. Nothing changed. |
| 3 | The browser granted a different account. The fleet is on that one. Run the command again. |
| 4 | The login changed but its owner could not be read. Run `claude-switch` in a moment. |

A plain `/login` in a normal session does the same thing without the aiming and the check. If you
do that, run `claude-switch` with no arguments afterwards and read the `FLEET` line.

Sessions started with `claude-as <name>` are **pinned** to that profile and never follow a fleet
switch. That is what `claude-as` is for.

How fast running sessions follow: measured on Claude Code 2.1.287, sessions that were working were
on the new account within about a minute, and sessions stopped on a usage limit continued on it at
their next message. The worst case is the life of an access token, about 8 hours.

### Opt-in: the swap, with no browser

`claude-swap <name>` does not sign in. It **moves** a login that already exists into the shared
slot and moves the outgoing login back to where it belongs, then checks the result with the
server. It takes about 20 seconds.

It ships off. While `"swap_enabled"` is `false` in `~/.claude-profiles/autoswitch.json`:

- `claude-swap <name>` and `claude-swap --undo` refuse and change nothing,
- the cockpit's number keys and `u` key use the browser sign-in,
- `claude-autoswitch --arm` refuses.

To turn it on, read [docs/safety.md](docs/safety.md), then set the value to JSON `true`:

```json
"swap_enabled": true
```

The text `"true"` and the number `1` do not count.

Two more words come with the swap.

- A **spare** is an extra login for an account, parked in a keychain item that no Claude Code
  process reads. A swap uses the spare first.
- With no spare, the swap moves the account's anchor into the shared slot. The account is then on
  the fleet with a **borrowed** login, and its own slot is empty until you swap away.

The one hazard: while an account is on the fleet with a borrowed login, that login exists in one
place only. A plain `/login`, or a browser fleet switch, would overwrite it, and that account
would need one browser sign-in again. `claude-switch` refuses in that state and tells you to use
`claude-swap`.

## The optional watcher and forecast

The watcher is a background job. Without it the cockpit works; the `FORECAST` panel has nothing
to show.

```bash
bash install_watcher.sh
```

This writes `~/Library/LaunchAgents/com.claude-cockpit.autoswitch.plist` from
`launchd/autoswitch.plist.template` and loads it as a `launchd` agent labelled
`com.claude-cockpit.autoswitch`.

| Flag | Effect |
|---|---|
| `--print` | Show the plist it would write. Change nothing. |
| `--remove` | Unload the agent and delete the plist. |

If you set `COCKPIT_CLAUDE_BIN` after installing the watcher, run `install_watcher.sh` again. The
watcher does not read your shell startup file, so the value is written into the plist.

What the watcher does:

- **Every 5 minutes** it records each account's usage and recomputes the forecast. It does this
  even when alerts are off.
- **Alerts**, if `enabled` is `true` (the cockpit's `a` key): a macOS notification when the fleet
  account reaches `switch_at_pct` of its 5-hour or weekly limit, at most once per `cooldown_min`.
  The notification names the command to run: `claude-switch <name>`, or `claude-swap <name>` if
  you turned the swap on.
- **One automatic swap**, only if `swap_enabled` is `true` and you ran `claude-autoswitch --arm`.
  It swaps once at the next crossing and disarms itself before it swaps. **After that it is back
  to alerts only** until you arm it again; the cockpit's top line then reads `ALERTS ONLY`. This is
  deliberate: a fault can never turn into a chain of swaps. It never picks a profile
  listed in `exclude_from_auto`.

The forecast panel answers one question: if you keep your pace of the last 7 days, 48 hours or 24
hours, do you run out before the accounts refill? For each pace it shows either
`enough until the refills` or the time you run dry and for how many hours of the next 7 days you
would be blocked. `burn first:` names the account to use first (the one whose weekly refill comes
soonest). `est.` marks a pace estimated from local session logs because the usage history is not
long enough yet. From the second day it also shows how far yesterday's forecast was off.

## Settings

### `~/.claude-profiles/autoswitch.json`

`install.sh` creates it once and never overwrites it. The file is read again on every poll, so an
edit needs no restart. Keys that start with `_` are notes.

| Key | Shipped value | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch for the watcher's alerts and auto-swap. The cockpit's `a` key flips it. |
| `swap_enabled` | `false` | Allows the no-browser swap: `claude-swap <name>`, the cockpit's number keys, auto-swap. Must be JSON `true`. |
| `auto_swap` | `false` | Armed for one automatic swap. Set it with `claude-autoswitch --arm`, not by hand. Ignored while `swap_enabled` is `false`. |
| `exclude_from_auto` | `[]` | Profiles the watcher may never pick by itself. The key must exist, even as an empty list, or the watcher refuses to run. |
| `switch_at_pct` | `97` | Act when the fleet account's 5-hour or weekly usage reaches this. |
| `target_max_session_pct` | `60` | A target account must be under this in its 5-hour window. |
| `target_max_week_pct` | `85` | A target account must be under this in its week. |
| `poll_sec` | `45` | Seconds between checks of the fleet's usage. |
| `cooldown_min` | `20` | Minimum minutes between alerts. |
| `notify` | `true` | Send macOS notifications. |

### `~/.claude-profiles/chrome-profiles.json` (optional)

Normally `claude-switch` finds the right Chrome profile by itself: it matches the Claude account's
email address to the Google account each Chrome profile is signed in to. Create this file only
when those two addresses differ. It maps a Claude account email to a Chrome profile folder name
(see `chrome://version`, "Profile Path").

```json
{ "account@example.com": "Profile 3" }
```

Start from `config/chrome_profiles.example.json`. Note the names: the example file has an
underscore, the live file must be named `chrome-profiles.json` with a hyphen. Check a mapping
with:

```bash
python3 ~/.claude-profiles/claude-switch.py --chrome-profile-for <name>
```

A blank answer means the sign-in page will open in whichever browser window was used last.

### Environment variables

| Variable | Who sets it | Meaning |
|---|---|---|
| `COCKPIT_CLAUDE_BIN` | You, if needed | Full path to the Claude Code binary. Default: `~/.local/bin/claude`. |
| `COCKPIT_FLEET_CONFIG_DIR` | Advanced | Makes a named settings folder the fleet. See [docs/how_it_works.md](docs/how_it_works.md#advanced-a-different-fleet-slot). Most people never need it. |
| `COCKPIT_CHROME_PROFILE_DIR` | The tools | Set for one subprocess during an aimed sign-in. Never set it yourself. |
| `COCKPIT_DEV_TOOLS` | Tests | Points the test runner at another copy of the tools. |
| `COCKPIT_SCAN_BANNED` | Maintainers | Path of the leak scanner's private word list. |

If Claude Code was installed another way (npm, Homebrew), add this to `~/.zshrc` and open a new
terminal:

```bash
export COCKPIT_CLAUDE_BIN=/full/path/to/claude
```

When the binary is missing, `claude-as`, `claude-login`, `claude-switch`, `claude-cockpit`,
`claude-usage` and `claude-ping` stop with a three-line message that names the path they tried and
shows this line.

## Known limits

1. **A refresh can time out on a large history.** Refreshing an account's numbers runs
   `claude -p "/usage"`. On recent Claude Code versions that command also reads every local
   session transcript. With a very large history one refresh can take minutes, and the cockpit's
   90-second timeout then shows the row as timed out. The numbers on that row are old, not wrong,
   and the account is not signed out.
2. **Every login expires.** Each login has a hard expiry, roughly monthly. After it, that account
   needs one browser sign-in again (`claude-login <name>`). Renewing a login does not move the
   deadline. The cockpit warns on the row 72 hours ahead.
3. **Tested on one Mac.** No real sign-in has been tested on a Mac other than the author's. The
   test files cover the logic with fake logins.
4. **Chrome only for aiming.** Opening the sign-in page in the right browser profile works for
   Google Chrome. Other browsers still work, unaimed: you pick the account yourself.
5. **The forecast assumes every account is on the same plan.** Its unit is 1% of one account's
   weekly limit, so accounts on different plans are counted as if they were equal.
6. **A profile links only what `~/.claude` had when the profile was made.** `new-profile.sh`
   links `skills`, `commands`, `agents`, `hooks`, `plugins`, `CLAUDE.md`, `settings.json` and
   `projects` from `~/.claude`, and skips any that do not exist. If you start using one of them
   later, existing profiles do not get the link by themselves. `verify.sh` reports it as MISSING
   and prints the `ln -s` command that fixes it.
7. **zsh is the only shell that was used.** In another shell, load
   `~/.claude-profiles/claude-as.sh` from your own startup file. That is untested.
8. **Small terminals.** The layout is made for 100 columns or more. At 80 columns long notes are
   cut at the end (the important part of each note comes first), and an 80 by 24 terminal cannot
   show four accounts and the recommendation at once. Make the window taller or wider.
9. **How fast running sessions follow a switch was measured on one Claude Code version.** It is
   Claude Code's behaviour, not something these tools control.

## Troubleshooting

**1. `Claude Code was not found at ...`**
Claude Code is not at `~/.local/bin/claude`. Add
`export COCKPIT_CLAUDE_BIN=/full/path/to/claude` to `~/.zshrc`, open a new terminal, and run
`install_watcher.sh` again if you use the watcher.

**2. A row says the refresh timed out (over 90 seconds).**
Your session history is large (Known limit 1). Do not sign in again: the account already holds a
login. Press `r` to retry, or run `claude-usage --fast` to see the last saved numbers.

**3. A row says `SIGNED OUT`, or that its login cannot renew after a date.**
Run `claude-login <name>`. It is one browser sign-in and it touches that account's slot only.
The cockpit shows `SIGNED OUT` only after it has asked Claude Code and been told the profile is
not logged in.

**4. The switch reports that the browser granted a different account.**
The browser decides which account signs in, and it offered another one. The fleet is now on that
account. Run `claude-switch <name>` again and pick the right account. If the page keeps opening in
the wrong Chrome profile, add the account to `chrome-profiles.json` (see [Settings](#settings)).

**5. `claude-switch <name>` refuses.**
If it says the account has no anchor login, run `claude-login <name>` and try again. If it says
the fleet is running on an account's own login, a swap put it there: use `claude-swap <name>`, or
follow the two commands it prints.

**6. `FORECAST` says it is not computed yet or is old, or `AUTO-SWITCH` says no watcher is
running.**
The watcher is not installed or has stopped. Run `bash install_watcher.sh`, check it with
`launchctl list | grep autoswitch`, and read `~/.claude-profiles/autoswitch.log`.

For anything else, run `bash ~/.claude-profiles/verify.sh` and read
[docs/gotchas.md](docs/gotchas.md). Each failing line names its own fix.

## Uninstall

```bash
bash uninstall.sh          # shows what it would remove, changes nothing
bash uninstall.sh --yes    # does it
```

With `--yes` it stops and removes the watcher if installed, removes the lines `install.sh` added
to `~/.zshrc` (after a backup), and removes the tool files from `~/.claude-profiles/`.

It leaves your profile folders, settings and logs in place. It does not touch any login. For each
profile it prints the `security delete-generic-password ...` command that would remove that login
from the keychain, and it never runs them. Removing a login signs that account out on this Mac, so
it stays a separate step that you take yourself.

## For contributors

- **Tests.** Run them from inside `tests/`, with the interpreter the watcher uses:

  ```bash
  cd tests
  /usr/bin/python3 run_dev.py test_meters.py
  ```

  `run_dev.py` makes the test load the tools from `../tools` instead of the installed copy in
  `~/.claude-profiles`. The files are `test_meters.py` (forecast maths and the usage cache),
  `test_swap_enabled.py` (the swap opt-in), `test_autoswap.py` (the one-shot auto-swap),
  `test_fleet_dir.py` (the alternative fleet slot) and `test_claude_swap.py` (the swap itself).
  Only `test_claude_swap.py` uses the real keychain, and only for throwaway items named
  `cockpit-swaptest-*` that hold fake tokens.
- **Screen captures.** `tests/render_cockpit_states.py` draws every cockpit state as text from
  demo accounts, with every call to Claude Code, the keychain and the network replaced, so you can
  see a change without touching a real login. It needs the `pyte` package
  (`pip install pyte` in a virtual environment); the tools themselves still need nothing extra.
  `python3 tests/render_cockpit_states.py --list` shows the states.
- **Leak scanner.** `python3 scripts/scan_personal.py --self-test` proves the scanner can fail.
  `python3 scripts/scan_personal.py` scans the tree. `--history` also scans every commit.
  `scripts/pre_push_hook.sh` runs it before every push. Install it once per clone:

  ```bash
  ln -s ../../scripts/pre_push_hook.sh .git/hooks/pre-push
  ```

- **"gotcha #N" in a code comment** refers to the entry with that number in
  [docs/gotchas.md](docs/gotchas.md). The numbers are stable. "invariant #N" refers to the
  numbered list in [docs/safety.md](docs/safety.md).
- **`SYNC WARNING:` in a code comment** names other places that must change together.
- Keep the tools compatible with Python 3.9. The watcher runs under `/usr/bin/python3`.
- Read the design history in [docs/how_it_works.md](docs/how_it_works.md) before changing how
  logins are handled. Three earlier designs failed in ways that are not obvious from the code.

## Disclaimer

You are responsible for using your own accounts within Anthropic's terms. This tool only works
with accounts you own and sign in to yourself. It is provided as is, without warranty of any kind;
see [LICENSE](LICENSE).

---

Made by Christian Johnston · [github.com/christianjohnston-ai](https://github.com/christianjohnston-ai)
