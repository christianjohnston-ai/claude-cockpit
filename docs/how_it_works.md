# How claude-cockpit works

This is the mental model behind the tools. Read it before you change anything that handles a
login. The last section, [Design history](#design-history), explains why three earlier designs
were abandoned. It is the most useful part for anyone who wants to modify the tool.

Facts about Claude Code in this document were measured on the version named next to them. Claude
Code updates often, and any of them can change.

## Words used here

| Word | Meaning |
|---|---|
| profile | A folder, `~/.claude-profiles/<name>/`, that Claude Code uses as its settings folder for one account. |
| slot | A macOS keychain item in which Claude Code keeps one login. |
| login | What a slot holds: an access token, a single-use refresh token and their expiry times. |
| fleet | Every Claude Code process started without `CLAUDE_CONFIG_DIR`, and the one shared slot they all read. |
| anchor | An account's own permanent login, kept in its profile's slot. |
| pinned | A session started with `claude-as <name>`. It uses that profile's slot and ignores the fleet. |
| spare | An extra login for an account, parked in a keychain item that no Claude Code process reads. Only the swap creates one. |
| borrowed | An account is on the fleet with its anchor moved into the shared slot. Its own slot is empty meanwhile. |

## Profiles

Claude Code reads its settings folder from the environment variable `CLAUDE_CONFIG_DIR`. With the
variable unset it uses `~/.claude` and `~/.claude.json`. A profile is a second settings folder
that shares almost everything with the first.

```
~/.claude-profiles/
    claude-as.sh, claude-*.py, new-profile.sh, verify.sh     the tools
    autoswitch.json                                          settings
    work/                                                    one folder per account
        .claude.json                    PRIVATE to this profile
        skills        -> ~/.claude/skills
        commands      -> ~/.claude/commands
        agents        -> ~/.claude/agents
        hooks         -> ~/.claude/hooks
        plugins       -> ~/.claude/plugins
        CLAUDE.md     -> ~/.claude/CLAUDE.md
        settings.json -> ~/.claude/settings.json
        projects      -> ~/.claude/projects
```

A folder counts as a profile only if it contains its own `.claude.json`. Every tool applies that
test before it touches a folder (see [docs/gotchas.md](gotchas.md) #13 and #30 for why).

### What is shared, and why each link matters

Everything except `.claude.json` is a symbolic link back to `~/.claude`. There is one copy of each
thing, so nothing can drift.

| Link | What breaks without it |
|---|---|
| `skills`, `commands`, `agents` | The profile has none of your own skills, commands or agents. A copy would drift from the original. |
| `hooks` | The hook scripts are missing. |
| `settings.json` | The hook wiring, model, effort, permission mode and the list of enabled plugins are gone. The profile falls back to defaults and asks for permissions again. |
| `plugins` | `settings.json` names the enabled plugins, but their files load from this folder. |
| `CLAUDE.md` | Your global instructions do not load. |
| `projects` | Claude Code writes session transcripts to `$CLAUDE_CONFIG_DIR/projects`. Tools that read session history look in `~/.claude/projects`. Without the link, work done under another account is invisible to them. With it, all accounts share one history. |

Claude Code can replace the `settings.json` link with a real file when it writes settings. The
profile then stops following `~/.claude` without any warning. `verify.sh` reports that as
`is a REAL FILE, not a symlink`. The fix is to remove the file and make the link again:

```bash
rm ~/.claude-profiles/work/settings.json
ln -s ~/.claude/settings.json ~/.claude-profiles/work/settings.json
```

### What is private

`.claude.json` holds the profile's identity (which account it is), its cached usage numbers, its
MCP server configuration and the list of folders you have trusted. `new-profile.sh` copies it from
`~/.claude.json`, so you keep your MCP servers and trusted folders, then removes the identity and
every per-account cache from the copy. The folder is created with mode 700 and the file with mode
600.

## The keychain: slots

Claude Code stores a login as a generic password in your login keychain. The item name depends on
the settings folder:

- `CLAUDE_CONFIG_DIR` unset: the item is named `Claude Code-credentials`. This is the shared
  default slot.
- `CLAUDE_CONFIG_DIR` set: the item is named `Claude Code-credentials-` followed by the first 8
  hex digits of the SHA-256 of the folder path string.

So a profile's slot name can be computed, and two different folders cannot share a slot:

```bash
printf '%s' "$HOME/.claude-profiles/work" | shasum -a 256 | cut -c1-8
```

The hash is of the path as text. `/Users/you/.claude` gives `34f6278e`; the same path with a
trailing slash gives `a3b43677`, which is a different and empty slot. For the same reason, renaming
or moving a profile folder changes the slot it maps to: the renamed profile looks signed out, and
its old login stays behind under the old name.

```
macOS login keychain
|
+-- "Claude Code-credentials"               THE FLEET SLOT (shared default)
|       read by every Claude Code process started without CLAUDE_CONFIG_DIR:
|       open sessions, new terminals, background jobs
|
+-- "Claude Code-credentials-9c4dfb07"      anchor of profile "personal"
+-- "Claude Code-credentials-160a26ce"      anchor of profile "work"
|       one per profile; read only by sessions started with
|       CLAUDE_CONFIG_DIR set to that profile (claude-as, and the tools' probes)
|
+-- "claude-cockpit-spare-work"             optional spare for "work"
        created only by a swap; read by no Claude Code process
```

(The two hashes are those of `/Users/you/.claude-profiles/personal` and `.../work`.)

### What a slot holds

The item's value is JSON with up to two top-level keys:

- `claudeAiOauth`: the Claude login. An access token that lasts about 8 hours, a refresh token,
  `expiresAt`, `refreshTokenExpiresAt`, scopes and the subscription type.
- `mcpOAuth`: sign-ins for that settings folder's remote MCP servers.

Three properties of the login drive the whole design.

1. **The refresh token is single use.** Renewing exchanges it for a new pair and the old one stops
   working.
2. **Claude Code's renewal lock is per settings folder** (a lock file inside the folder). Processes
   that share a folder take turns. Processes in different folders do not know about each other.
3. **The login has a hard deadline**, `refreshTokenExpiresAt`, roughly a month after sign-in.
   Renewing does not move it (confirmed 2026-10-01 on Claude Code 2.1.287).

Properties 1 and 2 together give the rule everything else protects: **one login lives in exactly
one slot.** If the same login sits in two slots, two folders will each renew it with no shared
lock. The first renewal spends the refresh token and the other holder is rejected.

The opposite case is safe. **Two different logins for the same account** (the account signed in
twice) are independent. Each renews on its own, and making a new one does not cancel an older one.
That was measured on 2026-08-17 (Claude Code 2.1.234) and is recorded as the "seal test" addendum
in [docs/gotchas.md](gotchas.md). The default design depends on it: an anchor and a fleet login
for one account are exactly this.

## Identity lives in `.claude.json`, tokens live in the keychain

The keychain item holds tokens only. Which account a settings folder is signed in to is recorded
separately, as `oauthAccount` in that folder's `.claude.json`. `claude auth status` reports
`loggedIn` from the keychain and `email` from the file.

Consequences:

- A profile's folder and its slot must always describe the same account. Claude Code keeps them
  together when it signs in. Any tool that moves a token by itself must also update the file, or
  Claude Code shows one account while it bills another.
- The file can be stale. A decision about where a login belongs must never be made from it. The
  swap asks the server who a token belongs to instead.

## Where the usage numbers come from

Claude Code caches each account's usage in the settings folder's `.claude.json`, under
`cachedUsageUtilization`:

```json
"cachedUsageUtilization": {
  "fetchedAtMs": 1700000000000,
  "accountUuid": "...",
  "utilization": {
    "five_hour": { "utilization": 17, "resets_at": "2023-11-15T03:00:00Z" },
    "seven_day": { "utilization": 41, "resets_at": "2023-11-18T00:00:00Z" },
    "limits": [
      { "kind": "session",       "percent": 17, "is_active": false },
      { "kind": "weekly_all",    "percent": 41, "is_active": true },
      { "kind": "weekly_scoped", "percent": 38, "is_active": false,
        "scope": { "model": { "display_name": "Fable" } } }
    ]
  }
}
```

- **Three meters.** `five_hour` is the 5-hour session window. `seven_day` is the weekly limit for
  all models. The per-model weekly limit is **not** a named key: it appears only as an entry in
  `limits[]` with `kind` equal to `weekly_scoped`, with the model name at
  `scope.model.display_name`. The tools show whatever name the server sends.
- **`is_active` marks the binding limit.** It is the server naming the limit that actually holds
  the account back. The cockpit draws it as `●`.
- **`resets_at` is authoritative.** The tools display it and never compute a reset time. The
  "seven day" window is the size of the measurement, not a promise that the reset is seven days
  away.
- **`fetchedAtMs` gives the age.** The cockpit shows the age from 6 minutes and stops trusting a
  row at 15 minutes.

### Refreshing

`claude auth status` does not refresh the cache (observed 2026-08). Running the `/usage` command
does. To refresh one account the tools run, with `CLAUDE_CONFIG_DIR` set to that profile:

```bash
claude -p "/usage" --settings '{"disableAllHooks":true}' --strict-mcp-config \
       --max-budget-usd 0.02 --no-session-persistence
```

`/usage` reads the numbers from Anthropic. It sends no message to a model, spends no usage and
does not open a 5-hour window. The flags keep the short-lived session cheap: no hooks, no MCP
servers, no saved transcript ([docs/gotchas.md](gotchas.md) #34 and #55). Success is judged by the
text of the report, never by the exit code, because a signed-out profile also exits 0 (#3). The
call is given 90 seconds.

Two limits of this method:

- Claude Code writes the cache at most once every 5 minutes, and clears it when the account
  changes (#8). A number on screen can be up to 5 minutes behind the server even right after a
  refresh.
- On recent versions `/usage` also reads every local session transcript, so it can take minutes on
  a large history (#45). The same numbers are available from a direct read of
  `GET /api/oauth/usage`, which the watcher and the swap already use. Moving the refresh to that
  read is not done yet.

## The fleet and the anchors

Every Claude Code process started the normal way has no `CLAUDE_CONFIG_DIR`, so it reads the
shared default slot. That includes background jobs started by `launchd`, which never reads your
shell startup file. This group is the fleet. Whoever is signed in to the shared slot is the
account the fleet is on. There is no file that records it; the tools ask the slot.

Each account also has its own profile with its own login, the anchor. The anchor is made once with
`claude-login <name>` and stays where it is.

The point of the anchors: the login in the shared slot is never an account's only login, so it can
be replaced freely. A switch that goes wrong can put the fleet on the wrong account. It cannot
take an account's last login away. `claude-switch` refuses to switch to an account that has no
anchor, because that would create the one state where the shared slot holds an only copy.

`claude-as <name>` starts one session with `CLAUDE_CONFIG_DIR` set to the profile. That session
uses the anchor. `CLAUDE_CONFIG_DIR` is fixed when a process starts, so a pinned session cannot be
moved by a fleet switch and a fleet session cannot be pinned afterwards.

Do not export `CLAUDE_CONFIG_DIR` from your shell startup file. It makes every new terminal a
pinned one, and then a `/login` typed there lands in a private slot instead of the shared one. The
fleet splits across accounts without any error. That was the fourth abandoned design.

## What a browser switch does

`claude-switch <name>`, and the cockpit's number keys while the swap is off, do this:

1. **Refuse early** if the profile does not exist, has never signed in (there is no email address
   to aim at or check against), or has no anchor. Also refuse if the fleet account is on a
   borrowed login, because the sign-in would overwrite that account's only login.
2. **Note the state before**: a fingerprint of the shared slot and the account it is on.
3. **Aim.** Look up the Chrome profile whose Google account has the same email address as the
   Claude account (from Chrome's `Local State` file, or from `chrome-profiles.json` if you made
   one). Exactly one match is required. Zero or several means no aiming.
4. **Run Claude Code's own sign-in**, `claude auth login`, with `CLAUDE_CONFIG_DIR` removed from
   its environment so it can only write the shared slot. If a Chrome profile was found, the
   `BROWSER` variable is set for that one process to a small script that opens the page in that
   profile. If the Claude Code version accepts `--email`, the address is prefilled.
5. **Verify.** Read the shared slot's fingerprint again. Unchanged means the window was closed and
   nothing happened. Changed means a sign-in happened, so ask Claude Code which account the slot
   now holds and compare it with the one requested.
6. **Report** one of five results (exit codes 0 to 4), and record the previous account in
   `.last-switch.json` so that `--undo` can sign back in to it.

The only thing that writes a login here is Claude Code's own sign-in. `claude-switch.py` contains
no keychain write. `verify.sh` parses the file on every run and fails if one appears.

### Aiming is not verification

The browser decides which account signs in. The tool cannot force it. Aiming only changes which
browser profile is offered the page, and it matches on the Chrome profile's Google account, while
the claude.ai session inside that profile is a third thing the tool cannot see. So a correctly
aimed page can still show the wrong account. Step 5 is what makes a switch correct. Aiming is a
convenience and is built to fail soft: any problem falls through to a plain `open`. The opener
writes a one-line receipt (`.last-aim`) so the switch can say when an announced aim did not happen.

## What a swap does

`claude-swap <name>` is the opt-in alternative. It moves an existing login instead of making a new
one. Nothing here runs unless `"swap_enabled": true` is set.

**Where the incoming login comes from**, in order:

1. the account's spare, if it has one that can still renew;
2. otherwise its anchor. The account is then on the fleet with a borrowed login.

**Where the outgoing login goes:**

- back to its own profile slot, if that slot is empty (it was borrowed);
- otherwise into a spare for that account (its anchor already holds a separate login).

So spares appear by themselves: an account that leaves the fleet while its anchor is intact parks
a spare, and the next swap to it uses the spare and borrows nothing.

**The steps:**

1. Ask the server who the fleet login belongs to (`GET /api/oauth/profile` with that token). If
   the server will not say, or the account matches no profile, stop.
2. Pick the incoming login. It must have at least 6 hours before its hard deadline.
3. Both logins must have at least 30 minutes of access-token life. Claude Code only renews a login
   near its expiry, so with 30 minutes in hand no process that still remembers a moved login will
   try to renew it before it has read the slot again. If a login is too close to expiry, the tool
   sends one word through Claude Code so that Claude Code renews it first. Then ask the server
   that the incoming login really belongs to the target account. A spare cannot be renewed where
   it sits, because nothing reads its item. A spare with an expired access token is therefore
   checked after the move instead, and moved back if the check fails.
4. Re-read both slots. If either changed since step 1 (a session renewed, someone signed in),
   stop with nothing changed.
5. Move, in this order: park the outgoing login, put the incoming login in the shared slot, remove
   the incoming login from where it came from. Only the `claudeAiOauth` key moves. Everything else
   in each item, such as MCP sign-ins, stays where it is. After every write the whole item is read
   back and compared. If a write fails, the original is written back and checked.
6. Update `oauthAccount` in `~/.claude.json` and clear the per-account caches there, so Claude Code
   displays the new account.
7. **Verify**: send one word through the fleet with the real Claude Code binary, ask the server
   who the shared login belongs to, and scan every slot for a login held twice. If the check
   fails, move everything back.
8. Append a line to `.swap-ledger.jsonl` (names and short hash fingerprints, never tokens) and
   record the previous account in `.last-swap.json` for `--undo`.

Exit codes: 0 swapped, 1 refused with nothing changed, 2 the target has no login that can be
moved (the cockpit then falls back to the browser sign-in), 4 something was written and then
undone or could not complete, 5 a keychain item holds unreadable data. `--check` exits 3 when a
login is held twice.

What the swap gives up: while an account is borrowed, its login exists in one place only, the
shared slot. See [docs/safety.md](safety.md#the-one-hazard-a-borrowed-login).

## How running sessions pick up a new fleet login

A running session keeps its tokens in memory. It reads the slot again in these cases:

| Session state | When it picks up the new login |
|---|---|
| Failing with an auth error ("please run /login") | At once. The recovery path re-reads the keychain. |
| Working normally | Measured on Claude Code 2.1.287: on the new account within about a minute. |
| Stopped on a usage limit | Measured on 2.1.287: on its next message. A session waiting to "continue automatically" needs one message from you. |
| Pinned with `claude-as` | Never. It does not read the shared slot. |

An earlier reading of the Claude Code binary (2.1.234) concluded that working and limit-blocked
sessions only move at their next token renewal, up to 8 hours later. The measurements on 2.1.287
were much faster. Treat 8 hours as the worst case. This
is Claude Code's behaviour and can change with any version ([docs/gotchas.md](gotchas.md) #6 and
#47).

## The watcher

`install_watcher.sh` installs `claude-autoswitch.py` as a `launchd` agent. It loops every
`poll_sec` seconds (45 by default) and does three separate jobs.

1. **History, always.** Every 5 minutes it records every account's meters to `meters.jsonl` and
   recomputes the forecast. Every 15 minutes it scans new lines of the local session logs for
   token counts. This runs even when `enabled` is `false`.
2. **Alerts, if `enabled` is `true`.** It reads the fleet account's live usage. When the 5-hour or
   weekly number reaches `switch_at_pct` it looks for a target: an account that is not in
   `exclude_from_auto`, has a login that could be moved, and is under both ceilings. It sends a
   macOS notification naming the best one, then stays quiet for `cooldown_min`.
3. **One swap, if armed.** With `swap_enabled` and `auto_swap` both `true`, it writes
   `auto_swap: false` first and then runs `claude-swap` on the best target. Disarming first means
   a crash in the middle cannot cause a second swap after `launchd` restarts the watcher.

The settings file fails closed. If `autoswitch.json` cannot be read, or `exclude_from_auto` is
missing or is not a list, the watcher refuses to run, so a typing mistake cannot make every
account eligible.

When `enabled` is `false` the watcher idles and keeps touching its log. The cockpit reads the
log's modification time as a heartbeat, so it can tell "alerts on and a watcher is running" from
"alerts on and nothing is watching".

### Live usage reads, the shared cache and backoff

The watcher cannot decide on the cached numbers in `.claude.json`: they can be 5 minutes old, and
a busy fleet was measured using 1 to 2% of a 5-hour window per minute. So it reads
`GET https://api.anthropic.com/api/oauth/usage` directly, with the account's own access token.

That endpoint is rate limited per account. On 2026-10-01 it answered HTTP 429 for the fleet
account when the watcher, the 5-minute snapshots and the running sessions' own polls all asked at
once. So every direct read goes through one cache file, `.usage-cache.json`:

- a reading younger than the caller's limit is reused (4 minutes normally, 1 minute once the fleet
  is at 85% or more);
- a 429 starts a backoff of at least 3 minutes for that account, during which the last good
  reading is returned together with its real age;
- for the fleet there is a second source, the cache its own sessions keep in `~/.claude.json`. It
  is used when it is fresher, and only if it is filed under the same account.

Who a token belongs to (`GET /api/oauth/profile`) is cached per token in memory, since a token
never changes owner.

A borrowed account's cockpit row is read the same way, through the shared slot, because its own
slot is empty and the normal refresh cannot work for it. If the server is rate limiting, the row
says so. It is never shown as signed out for that reason.

## The forecast

`claude-meters.py` answers: at your recent pace, do you run out before the accounts refill?

- **Unit.** One point is 1% of one account's weekly limit. This assumes every account is on the
  same plan.
- **Pace**, in points per hour over the last 7 days, 48 hours and 24 hours. When `meters.jsonl`
  covers at least 90% of a window, the pace is the sum of the rises in every account's weekly
  meter. That counts use from anywhere, including other devices, because it is the server's
  number. Until the history is long enough, the pace is estimated from token counts in the local
  session logs, scaled by a ratio fitted where both sources exist, and labelled `est.`.
- **Simulation, not division.** "Room left divided by pace" overstates how long you last, for two
  reasons. Unused weekly room is lost at each account's own reset. And one account can only be
  drained so fast, because each 5-hour window caps it. So the forecast steps forward 3 minutes at
  a time for 7 days. Each step it spends from the account whose weekly reset comes soonest, within
  that account's 5-hour cap, and refills each account at the reset times the server reported.
- **Output per pace**: when you first run dry, hours blocked in the next 7 days, and which account
  to use first. Plus the highest pace that never runs dry.
- **Accuracy.** Every forecast records what it predicts for the next 24 hours. A day later the
  history shows what happened, and the panel prints the miss.

What it trusts: the server's meters and reset times. What it will not do: send a message or renew
a token. An account whose access token has expired is skipped until something uses it, because
nothing is moving its meter. Renewing it would open that account's 5-hour window as a side effect.

## Files in `~/.claude-profiles`

| File | Written by | Holds |
|---|---|---|
| `autoswitch.json` | `install.sh` once, then you, the cockpit's `a` key and `claude-autoswitch` | Settings. |
| `autoswitch.log` | The watcher | Its log. Also its heartbeat. |
| `chrome-profiles.json` | You (optional) | Email to Chrome profile overrides. |
| `fleet_config_dir` | You (advanced) | One line: the alternative fleet folder. |
| `.last-switch.json` | `claude-switch` | Previous and new account of the last browser switch. Email addresses, no tokens. |
| `.last-aim` | The Chrome opener | Whether the last aimed sign-in really opened the chosen Chrome profile. |
| `.last-swap.json` | `claude-swap` | Previous and current profile of the last swap. |
| `.swap-ledger.jsonl` | `claude-swap` | One line per swap: names, where each login went, short hash fingerprints. No tokens. |
| `.usage-cache.json` | The direct usage reads | Last usage reading per account and any backoff time. |
| `meters.jsonl` | The watcher | A usage snapshot every 5 minutes, kept for 35 days. |
| `token_hours.json` | The watcher | Hourly cost totals from local session logs, and how far into each log file it has read. |
| `forecast.json` | The watcher | The latest forecast. The cockpit reads it. |
| `forecast_history.jsonl` | The watcher | Each forecast's 24-hour prediction, for the accuracy check. |

## Advanced: a different fleet slot

Most people never need this.

By default the fleet is the shared default slot. On a machine where some other long-running Claude
process owns that slot (a job runner that must stay on one account, for example), you can make the
fleet a named settings folder's own slot instead. Set the environment variable
`COCKPIT_FLEET_CONFIG_DIR`, or write the folder path as the only line of
`~/.claude-profiles/fleet_config_dir`. The variable wins over the file.

- The path must be absolute, normalised (no trailing slash) and contain a `.claude.json`. It must
  be the exact string that Claude Code is started with, because the slot name is a hash of that
  string. An invalid value stops the tools. They do not guess.
- "The fleet" then means the slot of that folder. Fleet probes set `CLAUDE_CONFIG_DIR` to it
  instead of removing the variable.
- In this mode `claude-swap` refuses to read, write or delete the bare `Claude Code-credentials`
  item.
- `new-profile.sh` links shared items from that folder instead of `~/.claude`.

This mode has unit tests (`tests/test_fleet_dir.py`). It has **not** been exercised with real
logins yet.

## Design history

Each design below was built, used and replaced. The reasons are the knowledge.

Code comments use short labels for these designs. They map to the sections below like this: v1 is
section 1, v2 is section 2, v3 is section 3, v3.5 is section 4, v4 is section 5, and "cockpit v2"
is the swap in section 6.

### 1. Copy a login between slots

To switch, the first version copied the target account's stored login into the shared slot. The
same login was then in two slots.

It failed because of the two facts in [What a slot holds](#what-a-slot-holds): the refresh token
is single use, and the renewal lock is per settings folder. The first holder to renew spent the
token, and every other holder was rejected. In one incident (2026-08-04) seven running sessions
lost their login within three minutes.

What it taught: one login, one slot, always. Never copy.

### 2. Move a login, choosing the destination from a cached identity

The second version moved logins. The incoming login went into the shared slot, and the outgoing
one was written back to "its" profile slot. Which profile that was came from the email address
recorded in `~/.claude.json`.

That file can disagree with the keychain, because identity and tokens are stored separately. The
tool could create the disagreement itself: when its update of the file failed part-way, it
reported a generic failure, and the caller tried again later on the stale identity. In one
incident (2026-08-05) it wrote one account's login over another account's. The overwritten login
no longer existed anywhere.

What it taught: never decide where a login goes from a cached identity. And a half-finished change
needs its own distinct "stop" result, never a generic failure that something will retry.

### 3. Move a login, deriving the destination from the layout of the slots

The third version consulted no identity at all. It relied on a structural rule: under move
semantics exactly one profile's slot is empty, and that profile is the home of the account on the
fleet.

The rule held only as long as nothing else touched the slots, and two ordinary actions break it.
A plain `/login` writes a new login into the shared slot without emptying any profile slot, so
afterwards no slot is empty and the old shared login, which was that account's only copy, is
gone. And a newly created profile has an empty slot, so it looks exactly like the home of the
fleet account. On the day it was retired (2026-08-17) the tool refused three times in one
afternoon after plain logins, and a new profile was named as the place to park a live login. That
second case did not fire only because the cockpit key had been disarmed first.

What it taught: an invariant that the user's normal habit breaks is not an invariant. And while
the shared slot holds an account's only login, a habit as ordinary as `/login` can destroy it.

### 4. Pin new terminals instead of touching logins

The fourth version stopped moving logins. It wrote the chosen profile name to a file, and the
shell startup file exported `CLAUDE_CONFIG_DIR` for every new terminal.

It was safe and it switched the wrong thing. Running sessions were untouched, because
`CLAUDE_CONFIG_DIR` is fixed when a process starts. And a `/login` typed in a pinned terminal now
landed in that profile's private slot instead of the shared one, so over a day the fleet split
across accounts without any error. It was retired the same day it shipped.

What it taught: the thing to change is the one slot that running sessions already read. Do not
export `CLAUDE_CONFIG_DIR` from a shell startup file. `claude-as.sh` carries a comment saying so,
and `verify.sh` warns if the old `.active` file reappears.

### 5. The sign-in is the switch (the current default)

Every account keeps an anchor. A switch is Claude Code's own sign-in into the shared slot, aimed
first and verified after. The user's old habit of typing `/login` became the mechanism instead of
the threat.

It rests on one measured fact: two separate logins for the same account coexist, and an older one
survives its renewal after newer ones were made. If that were false, making a fleet login would
eventually kill the anchor and the design would be unsafe. It was tested before the design was
adopted (the seal test, 2026-08-17).

Its cost is one browser round trip per switch, about 15 seconds.

### 6. The swap: moving again, with different rules (opt-in)

The swap was added on 2026-10-01 to remove the browser round trip. It moves logins, like designs 2
and 3, so it had to answer each of their failures:

| Earlier failure | What the swap does instead |
|---|---|
| Destination chosen from a cached identity | Asks the server who each token belongs to. |
| A process renews a login that was just moved | Requires 30 minutes of access-token life on both logins before moving. |
| Slot changed between check and write | Re-reads each slot just before writing and stops if it changed. |
| Whole item moved, carrying MCP sign-ins to the wrong slot | Moves only the `claudeAiOauth` key. |
| "The write failed, so the slot is unchanged" | Reads every write back whole. On failure writes the original back and checks it. |
| Success judged by an exit code | Sends a real message through the fleet, then asks the server for the identity. |
| The shared slot holds an only copy | Uses a spare when one exists. Without one, this is still true while borrowed. |

Its first live run found a bug the unit tests had missed: `security -i` cut a long command short,
wrote the cut value and exited with an error, and the caller treated the error as "nothing
changed" ([docs/gotchas.md](gotchas.md) #43). The tests had used small items; the real shared item
was about 40 KB. The write path, the read-back and the tests were all changed. That history is why
the swap is off until you turn it on.

### Two lessons that apply to any change

- **A check that cannot fail proves nothing.** `verify.sh` once printed "all checks passed"
  through an incident, and one of its checks was later found to be unable to fail at all
  ([docs/gotchas.md](gotchas.md) #27 and #33). Prove a new check fails on a known-bad input before
  trusting its pass.
- **Writing a hazard down does not disarm it.** A defect that was already documented fired because
  the key that triggered it was still live (#19). The cockpit's switch keys are gated by the
  constants `SWITCH_PROVEN_ON` and `SWAP_PROVEN_ON` in `claude-cockpit.py`. Blank one and its key
  path goes inert. Do that before changing the code behind it, and restore it after the tests pass
  and one run by hand has worked.
