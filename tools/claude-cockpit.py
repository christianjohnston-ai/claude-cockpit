#!/usr/bin/env python3
"""
claude-cockpit — live subscription usage across every Claude account, with
one-key account switching. Runs in any terminal pane.

  claude-cockpit                run it

  1-9      FLEET-SWITCH to that row: one aimed browser login (~15s), then
           every shared session, new terminal, and scheduled job follows
  u        fleet-switch back to the previous account
  e        show / hide email addresses (hidden on every launch)
  r        refresh usage now
  q        quit

WHAT DECISION THIS SCREEN SERVES
  "Which account should I be working on right now, and switch me to it."
  Everything on screen exists to answer that: both limit windows, when each
  resets, how fresh the numbers are, and which account is currently active.
  Rows are sorted most-headroom-first, so the answer is always the top row.

COST
  Refreshing is free. `claude -p "/usage"` is a local command that performs an
  authenticated GET to /api/oauth/usage — not an inference call. Verified
  2026-08-04 by running it four times against a completely idle account, which
  stayed at 0% throughout. So auto-refresh costs no quota.

SWITCHING (v4, 2026-08-17 evening — see claude-switch.py's docstring for history)
  Delegates to claude-switch.fleet_switch(): one aimed `claude auth login` into
  the SHARED slot that every session and launchd job reads, then the
  granted account is VERIFIED against the one requested. Convergence truth for
  already-running sessions (confirmed against the 2.1.234 binary):
      401 / 403-revoked ........... re-read the keychain immediately
      429 usage limit ............. NEVER re-reads on its own
      token expiry (~8h) or any
      refreshOAuth:true helper call  re-reads incidentally
  Net: sessions stuck at 'run /login' adopt the new account instantly; healthy
  or limit-blocked ones converge at their next token refresh (≤8h). The switch
  can no longer destroy a credential: every account keeps a permanent anchor
  login in its own profile slot, so the fleet grant is always expendable.
"""
import json, os, subprocess, sys, time, datetime, importlib.util, threading
try:
    import curses
except ImportError:
    raise SystemExit("Cockpit needs curses. On Windows install windows-curses in the cockpit Python environment.") from None
sys.dont_write_bytecode = True  # keeps __pycache__ out of the profiles dir (gotcha #13)
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.expanduser("~/.claude-profiles")
if sys.platform == "win32":
    BASE = os.path.normpath(BASE)
# Never bare `claude`: a PATH lookup can land on a terminal app's shim or a stale install.
# SYNC WARNING: same two-step lookup as claude-switch.py CLAUDE_BIN (the full list of
# copies is in the comment there). Change them together.
CLAUDE = os.environ.get("COCKPIT_CLAUDE_BIN") or os.path.expanduser("~/.local/bin/claude.exe" if sys.platform == "win32" else "~/.local/bin/claude")
if sys.platform == "win32":
    CLAUDE = os.path.normpath(CLAUDE)
SWITCH = os.path.join(BASE, "claude-switch.py")

# Import claude-switch as a module for fleet_anchor() / fleet_switch() /
# undo_fleet(). Hyphenated filename, so spec_from_file_location — the same three
# lines claude-autoswitch.py has used since 2026-08-06. Verified side-effect
# free: every module-level statement there is a constant behind a __main__ guard.
_spec = importlib.util.spec_from_file_location("cswitch", SWITCH)
cswitch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cswitch)
# ── THE GATE ─────────────────────────────────────────────────────────────────
# A credential-mutating action gets NO UI affordance until the underlying
# operation has been run successfully BY HAND. On 2026-08-05 this key ran a
# claude-switch whose defect was already written up as BLOCKING in the plan —
# documenting a hazard is not disarming it. One keypress destroyed an account.
#
# Blank this string and the key goes inert again.
#
# The v2/v3-era re-earn ritual that used to be described here (switch -> verify
# -> undo -> verify, requiring byte-identical token fingerprints either side)
# was DELETED 2026-08-20. Under v4 it is not merely superseded, it is
# UNSATISFIABLE: every switch mints a fresh grant, so the fingerprints can never
# match across a round trip, and an agent trying to obey it would strand this
# key disarmed forever. verify.sh's D3 gates check the same property on every
# run instead. The operative conditions are stated below.
SWITCH_PROVEN_ON = "" if cswitch.IS_WINDOWS else "2026-08-17"   # v4: the key launches an aimed browser login
# COCKPIT v2 gate for the NO-BROWSER swap the 1-9 key now tries first. Same
# rule as above (gotcha #19): earned by a successful run BY HAND before the key
# was wired — `claude-swap ops`, 2026-10-01 20:37, verified by the
# server and an independent slot scan. Blank it and the key reverts to the v4
# browser login only. Any change to claude-swap.py's move/restore code means
# blanking this and re-earning it: run tests/test_claude_swap.py
# (must be all PASS) and one live swap by hand.
SWAP_PROVEN_ON = "" if cswitch.IS_WINDOWS else "2026-10-01"
SWAP = os.path.join(BASE, "claude-swap.py")
# WHAT THIS GATES NOW (v4). The 1-9 key no longer performs a credential
# operation itself — it launches the official `claude auth login`, aimed at the
# expendable FLEET slot, and verifies the granted account afterwards. The risk
# class fell from "an account can be destroyed" (v2, which did exactly that on
# 2026-08-05) to "the browser granted the wrong account" — detected, reported,
# and fixed by pressing the right row again. Every account keeps a permanent
# anchor login in its own profile slot, so no keypress here can take a login
# below one live grant.
#
# STILL RE-EARN THE DATE IF: a direct keychain write ever reappears in
# claude-switch.py (verify.sh D3's AST gate fails — blank this the same day),
# or the suspend/resume flow in confirm() is changed (a broken resume strands
# the terminal, which is a UX failure, not a credential one).
# The curses suspend path had NOT been exercised by a human keypress when this
# shipped — the logic beneath it is unit-tested; the first real press is the
# live test of the suspend/resume dance only.
# ─────────────────────────────────────────────────────────────────────────────

AUTO_REFRESH_SEC = 300
# Staleness thresholds, in minutes. These are deliberately tight: usage can move
# 40 percentage points in 45 minutes under heavy use (measured on side 2026-08-04),
# so anything older than a few minutes is a number worth doubting out loud.
STALE_WARN_MIN = 6     # show the age, dimmed
STALE_BAD_MIN = 15     # show the age in warning colour and stop trusting the row

# EMAILS ARE CENSORED ON SCREEN BY DEFAULT (the original author, 2026-10-08): "censor out
# the emails in the cockpit view", so the cockpit can sit on a shared or
# recorded screen without showing which addresses own the subscriptions.
# Masking happens ONLY at draw time, through UI.em(). r["email"] and s.cur
# keep the REAL address on purpose: load() falls back to matching the live row
# by email when fleet_anchor() cannot answer, and masking the stored value
# would silently break that match. The `e` key reveals the addresses for the
# current process only, so every launch starts hidden again. The profile name
# column already tells the rows apart, and switching verifies the granted
# account with the server (gotcha #28), so hiding the address never weakens a
# switch. The mask is one fixed string: a per-address mask would leak each
# address's length and first letter.
# Out of scope by design: while a 1-9 switch or `u` undo runs, curses is
# suspended and claude-swap / claude-switch print to the bare terminal. Those
# tools still show real addresses there, because the "confirm the consent
# screen shows <email>" line is a safety check (gotchas #28 and #32).
EMAIL_MASK = "••••••@••••••"


def profiles():
    if not os.path.isdir(BASE):
        return []
    return sorted(d for d in os.listdir(BASE)
                  if d != "__pycache__"    # claude plants .claude.json into any dir it's pointed at
                  and os.path.isfile(os.path.join(BASE, d, ".claude.json")))


def parse_usage(d, name):
    """Turn one .claude.json into a row.

    Shared by the per-profile read and the active-account read so those two paths
    can never disagree about shape — they differ only in WHICH file they open.
    """
    oa, cu = d.get("oauthAccount") or {}, d.get("cachedUsageUtilization")
    r = {"name": name, "email": oa.get("emailAddress") or "(not signed in)",
         "five": None, "seven": None, "five_at": None, "seven_at": None,
         "age": None, "scoped": [], "binding": None}
    if not cu:
        return r
    u = cu.get("utilization") or {}
    fh, sd = u.get("five_hour") or {}, u.get("seven_day") or {}
    r["five"], r["seven"] = fh.get("utilization"), sd.get("utilization")
    r["five_at"], r["seven_at"] = fh.get("resets_at"), sd.get("resets_at")
    r["age"] = (time.time() - cu.get("fetchedAtMs", 0) / 1000) / 60

    # WHY limits[] and not a named key (2026-08-14): the PER-MODEL weekly budget is
    # not exposed as utilization.seven_day_<model>. Those keys exist —
    # seven_day_opus, seven_day_sonnet — and are all null. The real figure appears
    # ONLY as a limits[] entry with kind == "weekly_scoped", model name nested at
    # scope.model.display_name. Reading the named keys yields None forever, which is
    # how a Fable meter pinned at 100% stayed invisible on three accounts while this
    # screen recommended one of them.
    #
    # is_active is the SERVER naming the limit that is actually binding that
    # account. It is the single most decision-relevant flag in the payload and was
    # previously discarded: personal showed session 0% / week 80% while the binding
    # limit was Fable at 100%.
    for l in (u.get("limits") or []):
        if not isinstance(l, dict):
            continue
        if l.get("is_active") and r["binding"] is None:
            r["binding"] = l.get("kind")
        if l.get("kind") != "weekly_scoped":
            continue
        nm = ((l.get("scope") or {}).get("model") or {}).get("display_name")
        # Render whatever the server calls it. Hardcoding "Fable" would break
        # silently on a rename ("Fable 5") or when a second scoped model appears.
        r["scoped"].append({"name": nm or "scoped", "pct": l.get("percent"),
                            "severity": l.get("severity"),
                            "resets_at": l.get("resets_at"),
                            "is_active": bool(l.get("is_active"))})
    return r


def primary_scoped(r):
    """The one scoped meter worth a column. Prefers the binding meter, else the
    fullest. None when the account reports no scoped model budget at all."""
    sc = r.get("scoped") or []
    if not sc:
        return None
    pool = [x for x in sc if x["is_active"]] or sc
    return max(pool, key=lambda x: x["pct"] if x["pct"] is not None else -1)


# ── THE ACTIVE ACCOUNT IS SIGNED OUT OF ITS OWN PROFILE, BY DESIGN ───────────
# Under move semantics a switch MOVES the credential into the DEFAULT keychain
# slot, so the active account's own profile slot is EMPTY. Probing that profile
# therefore reports the account you are actively using as "signed out", and its
# profile .claude.json stops being refreshed and freezes.
#
# Measured 2026-08-14: side was the default from before 13:20 until 13:22:04. The
# 13:20:51 refresh cycle succeeded for the other three and failed for side, which
# then displayed a frozen 0% / 0% while the real account was at session 100% and
# was the ONLY account with Fable headroom left. The screen recommended personal, whose
# binding limit was Fable at 100%.
#
# Fix: for the active row, read numbers from ~/.claude.json and probe with
# CLAUDE_CONFIG_DIR POPPED. claude-autoswitch.py has done this since 2026-08-06;
# docs/how_it_works.md documented it as done in the cockpit when it never was.
ACTIVE_TTL_SEC = 60
_ACTIVE = {"name": None, "at": 0.0, "valid": False}


def active_profile(force=False):
    """The FLEET account's anchor profile — the row every shared session, new
    terminal, and cron is on.

    Third implementation of this function today, and it has come full circle:
    v2 interrogated the shared slot (parked_profile), v3.5 read the .active
    text file, and v4 interrogates the shared slot again — because under
    login-based switching the slot IS the truth and there is no file. The
    derivation is claude-switch.fleet_anchor(): `claude auth status` with the
    env scrubbed (a 1-3 s subprocess, hence the 60 s memo so draw() never
    stalls), mapped to whichever profile's identity matches that email.

    Returns None when the fleet is signed out, the probe is slow, or the fleet
    account has no anchor profile. Callers must render None as "unknown/none",
    never as an error, and never guess a row to mark instead.
    """
    now = time.time()
    if not force and _ACTIVE["valid"] and now - _ACTIVE["at"] < ACTIVE_TTL_SEC:
        return _ACTIVE["name"]
    try:
        n = cswitch.fleet_anchor()
    except Exception:
        n = None
    _ACTIVE.update(name=n, at=now, valid=True)
    return n


def credential_status(name):
    """Native stores never fall back to the fleet; corrupt is not signed out."""
    if not cswitch.IS_WINDOWS:
        return None
    try:
        if not cswitch.has_claude_login(cswitch.slot_for(os.path.join(BASE, name))):
            return "missing"
    except cswitch.CredentialReadError:
        return "unreadable"
    return None


def read(name):
    prof_path = os.path.join(BASE, name, ".claude.json")
    try:
        prof = json.load(open(prof_path))
    except Exception:
        return None

    # EVERY row reads its own profile record now, the active one included.
    #
    # DELETED 2026-08-17: a special case that read the active row's numbers from
    # ~/.claude.json instead. It existed because the old switch MOVED the active
    # account's credential into the shared DEFAULT slot and left its own slot
    # empty (gotcha #20), so the profile's own record went stale the moment it
    # became active. Under the rebuilt switch nothing moves: every account stays
    # signed into its own profile permanently, so its own record is always the
    # freshest and most correct source. Keeping the special case would now be
    # actively WRONG — it would show the HEADLESS lane's usage (the permanent
    # DEFAULT slot account, which background launchd jobs use) on whichever row
    # happened to be active.
    #
    # The gotcha #5 poisoned-cache guard went with it: it defended against
    # ~/.claude.json holding one account's numbers under another's accountUuid
    # after a switch whose identity patch lagged its credential move. No switch
    # writes ~/.claude.json any more, so that desync cannot be created.
    # Cockpit v2 exception to the paragraph above: a BORROWED row (see LIVE,
    # refresh_borrowed) shows the live numbers fetched through the shared slot,
    # because its own record stopped updating when its login moved.
    if name in LIVE:
        prof = dict(prof)
        prof["cachedUsageUtilization"] = LIVE[name]
    r = parse_usage(prof, name)
    r["credential_error"] = credential_status(name)
    if r["credential_error"]:
        REFRESH_STATUS[name] = r["credential_error"]
    r["borrowed"] = name in LIVE
    r["login_expires"] = login_deadline(name)
    r["is_active_acct"] = (name == active_profile())
    return r


# WHY this returns a status instead of swallowing errors (2026-08-04): the first
# version did `except Exception: pass`. side's credential was silently emptied, its
# refresh failed every cycle for 46 minutes, and the screen kept showing the last
# good numbers with no indication they were frozen — while the real account had
# climbed from 40% to 80%. A refresh that can fail without saying so is worse than
# no refresh at all, because the operator trusts the number.
#
# Success test is the CONTENT of the report, not the exit code: a signed-out profile
# exits 0 and prints a cost summary instead of the usage report.
REFRESH_STATUS = {}


# ── BORROWED ROWS (cockpit v2, 2026-10-01) ───────────────────────────────────
# claude-swap can put an account on the fleet by MOVING its own private login
# into the shared slot ("borrowed"). Its private slot then has no Claude login,
# so the per-profile probe below cannot refresh it and the row froze at the
# moment of the swap: the original author saw lab "⚠ STALE — 38 min old" while every
# session was running on lab (2026-10-01 21:39; only the borrowed row was
# stale, every other row 1 min old). This is gotcha #20 come back, which v4 had
# correctly deleted because v4 never moved a login.
# Fix: a borrowed row reads its numbers LIVE from the server through the shared
# slot — only after the server confirms the shared login IS this account (a
# borrowed account knocked off the fleet by a /login is genuinely signed out and
# must still show as such). Not via `/usage`: through the shared slot that probe
# ran 7+ min on 2026-10-01 (gotcha #45). The payload is the same shape as
# cachedUsageUtilization["utilization"], so parse_usage() renders it unchanged.
# SYNC WARNING: "borrowed" = has_claude_login() false on the profile slot — the
# same test claude-switch.py's borrowed-fleet guard and claude-swap.py use.
LIVE = {}        # name -> cachedUsageUtilization-shaped dict, borrowed rows only
_spec_sw = importlib.util.spec_from_file_location("cswap", os.path.join(BASE, "claude-swap.py"))
cswap = importlib.util.module_from_spec(_spec_sw)
_spec_sw.loader.exec_module(cswap)

_SWAP_ON = [0.0, False]


def swap_on():
    """cswap.swap_enabled(), remembered for 2 seconds. FOR ON-SCREEN WORDING ONLY.

    WHY it exists: the hint line and the "press N" line used to say "swap" always, which
    is false on a fresh install where the number keys open the browser sign-in instead.
    WHY cached: draw() runs several times a second and swap_enabled() reads a file.
    WHY the key handlers do NOT use this: a decision to move a login must read the
    setting at that moment, never a remembered copy. They call cswap.swap_enabled()."""
    now = time.time()
    if now - _SWAP_ON[0] > 2:
        _SWAP_ON[0], _SWAP_ON[1] = now, cswap.swap_enabled()
    return _SWAP_ON[1]


def login_deadline(name):
    """Epoch seconds when this account's login can no longer renew, or None.
    refreshTokenExpiresAt is an ABSOLUTE deadline — renewing does not move it.
    CONFIRMED 2026-10-01: personal's private login renewed at ~15:47, still died at its
    22:27 deadline, and a claude-as personal session then hit "Login expired" at 23:43.
    So every account needs one browser `claude-login <name>` about monthly; this
    lets the cockpit say so days ahead instead of showing SIGNED OUT after the fact.
    A borrowed account's login is in the shared slot, so read it there."""
    try:
        kc = cswap.Keychain()
        o = (kc.read(cswitch.slot_for(os.path.join(BASE, name))) or {}).get("claudeAiOauth")
        if not (o or {}).get("refreshToken") and name in LIVE:
            o = (kc.read(cswap.SHARED_SLOT) or {}).get("claudeAiOauth")
        e = (o or {}).get("refreshTokenExpiresAt")
        return e / 1000 if e else None
    except Exception:
        return None


def refresh_borrowed(name):
    """Refresh a borrowed row through the shared slot. Returns a REFRESH_STATUS
    value: 'ok', or 'failed' when the shared login is not this account (then the
    row really is signed out, and the normal diagnostics take over)."""
    try:
        o = (cswap.Keychain().read(cswap.SHARED_SLOT) or {}).get("claudeAiOauth")
        who = cswap.server_whoami(o)
        mine = (json.load(open(os.path.join(BASE, name, ".claude.json"))).get("oauthAccount") or {}).get("emailAddress")
        if not who or who[0] != mine:
            LIVE.pop(name, None)
            return "failed"
        # Shared cache + 429 backoff (claude-swap.py USAGE RATE LIMIT). If the
        # server is backing us off, fall back to the fleet sessions' own reading
        # in ~/.claude.json when it is filed under this account. If neither exists
        # the row is RATE-LIMITED, not signed out — the identity check above just
        # proved the login is alive. (22:20 2026-10-01: a 429 here rendered
        # "SIGNED OUT — fix: claude-login lab" while the fleet ran on lab.)
        data = cswap.live_usage_raw(o, name, 240)
        at = time.time() - (cswap.usage_age(name) or 0)
        sdata, sage = cswap.session_cached_usage(who[1])
        if sdata is not None and (data is None or sage < time.time() - at):
            data, at = sdata, time.time() - sage
        if not data:
            LIVE.pop(name, None)
            return "limited"
        LIVE[name] = {"utilization": data, "fetchedAtMs": at * 1000}
        return "ok"
    except Exception:
        return "failed"


def refresh_one(name):
    native_error = credential_status(name)
    if native_error:
        LIVE.pop(name, None)
        REFRESH_STATUS[name] = native_error
        return native_error
    if not cswitch.IS_WINDOWS and not cswitch.has_claude_login(cswitch.slot_for(os.path.join(BASE, name))):
        REFRESH_STATUS[name] = refresh_borrowed(name)
        return REFRESH_STATUS[name]
    LIVE.pop(name, None)
    # Always probe the profile's OWN config dir, active row included.
    #
    # The old code took a `via_default` flag and POPPED CLAUDE_CONFIG_DIR for the
    # active row, because that account's credential had been moved out of its own
    # slot and into the shared DEFAULT one, so probing its profile dir hit an
    # empty slot and reported the account you were actively using as signed out
    # (gotcha #20). Nothing is moved any more, so every profile probes the same
    # way and the flag is gone.
    #
    # NOTE this ALSO has to set the var explicitly rather than inherit it: this
    # cockpit may itself be launched from a shell that already exports
    # CLAUDE_CONFIG_DIR for the active profile, and inheriting it would probe
    # that one account five times instead of probing five accounts once each.
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = os.path.join(BASE, name)
    try:
        # MINIMAL session by contract: disableAllHooks stops hook work
        # (security-guidance's SessionStart pip-installed a ~100MB venv on
        # EVERY probe and was killed mid-install when the seconds-long session
        # exited — ~40k orphaned pip-unpack-* dirs/day in $TMPDIR, found+fixed
        # 2026-08-24), and --strict-mcp-config with no --mcp-config boots zero
        # MCP servers. Verified on claude 2.1.241: report parses, probe ~6s.
        # disableAllHooks is UNDOCUMENTED — if a future claude drops it, the
        # tell is pip-unpack-* dirs reappearing (the 2026-08-24 venv repair
        # makes that a 2s no-op, not a leak). --bare is NOT usable here: it
        # skips keychain/OAuth — exactly the auth /usage needs.
        # --no-session-persistence (added 2026-10-08): without it
        # every 5-minute refresh saved one session transcript per profile —
        # tens of thousands of `/usage` transcripts by 2026-10-08. They
        # pollute any tool that indexes session history, and they slow
        # `/usage` itself, because on claude 2.1.287 `/usage` reads EVERY local
        # transcript (docs/gotchas.md #45). Proven 2026-10-08
        # (test M5): with the flag the report still prints and 0 .jsonl
        # files are written; without it exactly one is.
        # SYNC WARNING: the `/usage` probe argv is duplicated in exactly three
        # places — claude-cockpit.py refresh_one (here), claude-usage.py
        # refresh, and claude-ping.py PROBE_FLAGS — and they change together.
        # claude-autoswitch.py no longer spawns `/usage` (it reads
        # GET /api/oauth/usage via claude-swap).
        r = subprocess.run([CLAUDE, "-p", "/usage",
                            "--settings", '{"disableAllHooks":true}',
                            "--strict-mcp-config",
                            "--max-budget-usd", "0.02",
                            "--no-session-persistence"],
                           env=env, capture_output=True, timeout=90,
                           stdin=subprocess.DEVNULL, text=True)
        ok = "Current session" in (r.stdout or "")
        REFRESH_STATUS[name] = "ok" if ok else "failed"
    except subprocess.TimeoutExpired:
        REFRESH_STATUS[name] = "timeout"
    except Exception:
        REFRESH_STATUS[name] = "failed"
    return REFRESH_STATUS[name]


def signed_in(name):
    """True only if the profile can actually authenticate. A slot can exist and hold
    a record with EMPTY tokens — that reads as 'present' to the keychain but is
    logged out, which is exactly how side went stale unnoticed."""
    env = dict(os.environ, CLAUDE_CONFIG_DIR=os.path.join(BASE, name))
    try:
        r = subprocess.run([CLAUDE, "auth", "status"], env=env,
                           capture_output=True, timeout=20, text=True)
        return json.loads(r.stdout).get("loggedIn") is True
    except Exception:
        return False


def refresh_all(names):
    # force=True so a switch made since the last cycle is picked up immediately.
    # Every profile is probed identically now (see refresh_one).
    active_profile(force=True)
    with ThreadPoolExecutor(max_workers=max(1, len(names))) as ex:
        list(ex.map(refresh_one, names))


AUTOCFG = os.path.join(BASE, "autoswitch.json")
AUTOLOG = os.path.join(BASE, "autoswitch.log")


def auto_armed():
    """True only when the watcher WILL swap by itself: auto_swap AND swap_enabled are both
    JSON true in autoswitch.json.

    WHY this exists: the header used to read "AUTO-SWITCH ● ON" from `enabled` alone.
    `enabled` turns on ALERTS; swapping by itself needs a separate one-shot arm
    (`claude-autoswitch --arm`) that the watcher clears after every swap. So the screen
    said ON for days after the one armed swap had been used, and its user found out
    only when an account ran dry and nothing moved. The header must say which of the
    two it is. SYNC WARNING: same rule as claude-autoswitch.py cfg() (auto_swap is
    forced off unless swap_enabled is true)."""
    if cswitch.IS_WINDOWS:
        return False
    try:
        c = json.load(open(AUTOCFG))
        return c.get("auto_swap") is True and c.get("swap_enabled") is True
    except Exception:
        return False


def watcher_alive():
    """The watcher touches its log on every poll, even with alerts off (it keeps the
    usage history for the forecast either way). A log written in the last 10 minutes
    therefore means a watcher is installed and running."""
    try:
        return time.time() - os.path.getmtime(AUTOLOG) < 600
    except Exception:
        return False


def auto_state():
    """Truth about auto-switch, in four distinguishable states.

    A config flag says NOTHING about whether a watcher is actually running — the
    watcher is a separate launchd process. A toggle that reads 'ON' while nothing
    is watching is a lying indicator, and this system already shipped one of
    those (the cockpit sat 46 minutes on frozen numbers with no warning). So
    liveness comes from the log's heartbeat, never from the config alone.

    Returns (enabled, status) where status is one of:
      'off' · 'up' · 'dead' (enabled but no recent heartbeat) · 'halted'
    """
    try:
        enabled = bool(json.load(open(AUTOCFG)).get("enabled"))
    except Exception:
        return None, "badcfg"          # fail-closed: unreadable config is not "off"

    if not enabled:
        return False, "off"

    try:
        poll = int(json.load(open(AUTOCFG)).get("poll_sec", 90))
    except Exception:
        poll = 90
    try:
        tail = open(AUTOLOG, errors="replace").read()[-4000:]
        age = time.time() - os.path.getmtime(AUTOLOG)
    except Exception:
        return True, "dead"

    if "HALT" in tail.split("\n")[-1] or any("HALT" in l for l in tail.split("\n")[-3:]):
        return True, "halted"
    # 3x the poll interval: one missed cycle is noise, three is a stopped watcher.
    return True, ("up" if age < poll * 3 else "dead")


def toggle_auto():
    """Flip `enabled`, preserving every other key AND the _-prefixed comments."""
    try:
        with open(AUTOCFG) as f:
            d = json.load(f)
    except Exception as e:
        return f"cannot read autoswitch.json ({e}) — not toggling"
    d["enabled"] = not bool(d.get("enabled"))
    tmp = AUTOCFG + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, AUTOCFG)       # atomic
    except Exception as e:
        return f"write failed ({e})"
    return None


def active_email():
    try:
        d = json.load(open(cswitch.CFG))     # the FLEET's config (see claude-switch.py _fleet_config_dir)
        return (d.get("oauthAccount") or {}).get("emailAddress")
    except Exception:
        return None


def when(iso, hours=True):
    """Reset time in local time. `hours=False` drops the countdown — used for the
    weekly window, where the clock time is enough and the countdown overflowed
    the column."""
    if not iso:
        return "—"
    try:
        t = datetime.datetime.fromisoformat(iso).astimezone()
    except Exception:
        return "—"
    h = (t - datetime.datetime.now().astimezone()).total_seconds() / 3600
    if h < 0:
        return "due"
    stamp = t.strftime("%#I:%M%p" if sys.platform == "win32" else "%-I:%M%p").lower() if h < 24 else t.strftime("%a %#I%p" if sys.platform == "win32" else "%a %-I%p").lower()
    return f"{stamp} ({h:.0f}h)" if hours else stamp


# Severity drives colour everywhere, so the eye lands on the account in trouble
# before it reads a single number.
def sev(p):
    if p is None:
        return 0
    return 3 if p >= 90 else 2 if p >= 70 else 1 if p >= 40 else 0


def bar(p, w=14):
    if p is None:
        return "·" * w
    f = min(w, int(round(p / 100 * w)))
    return "█" * f + "░" * (w - f)


class UI:
    def __init__(s, scr):
        s.scr = scr
        # last=0 means NEVER REFRESHED IN THIS PROCESS, and draw() must render it
        # as such. It used to fall through to the ordinary "updated Nm ago"
        # arithmetic, which printed `updated 29783402m15s ago` (the age of the
        # unix epoch) for the entire 45-60 s startup refresh. A staleness
        # indicator that lies is the one thing this cockpit must never do.
        # refreshing/dirty drive the non-blocking refresh added 2026-08-17.
        s.rows, s.msg, s.last = [], "", 0
        s.refreshing, s.dirty = False, False
        # Separate from `refreshing`: a ping SPENDS quota, so a repeated keypress
        # must not be able to stack spawns the way a free refresh harmlessly can.
        s.pinging = False
        # Hidden on every launch; `e` flips it for this process only (see EMAIL_MASK).
        s.show_emails = False
        curses.curs_set(0)
        scr.nodelay(True)
        curses.use_default_colors()
        for i, c in enumerate([curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED,
                               curses.COLOR_CYAN, curses.COLOR_WHITE], 1):
            curses.init_pair(i, c, -1)
        s.OK, s.WARN, s.BAD, s.ACC, s.DIM = (curses.color_pair(i) for i in range(1, 6))

    def load(s):
        # A borrowed row has no fresh numbers on disk at all, so without this it
        # shows "STALE" for the ~75 s the first full refresh takes (the other rows'
        # /usage probes are slow, gotcha #45) — seen in the 2026-10-01 22:20 capture.
        # Its live read is 0.7 s, so do it once, up front. Once per process: after
        # that, refresh_one() keeps it current with every other row.
        if not getattr(s, "_borrow_primed", False):
            s._borrow_primed = True
            for n in profiles():
                if cswitch.IS_WINDOWS:
                    error = credential_status(n)
                    if error:
                        REFRESH_STATUS[n] = error
                    continue
                if n not in LIVE and not cswitch.has_claude_login(cswitch.slot_for(os.path.join(BASE, n))):
                    REFRESH_STATUS[n] = refresh_borrowed(n)
        s.rows = [r for r in (read(n) for n in profiles()) if r]
        for r in s.rows:
            r["status"] = REFRESH_STATUS.get(r["name"])
            # Distinguishing "actually signed out" from "the probe did not come
            # back" costs one subprocess per BROKEN row, and it happens HERE in
            # load() — never in draw(), which runs several times a second and would
            # stall on a 20 s auth check.
            # The `and not is_active_acct` exemption was removed 2026-08-17. It
            # existed because the active profile's slot was deliberately empty,
            # so signed_in() on it was guaranteed-false and its answer meant
            # nothing. Every profile now holds its own credential, so a failed
            # refresh on the active row is a REAL failure worth diagnosing, and
            # exempting it would hide a genuinely signed-out active account.
            r["auth"] = (signed_in(r["name"])
                         if r["status"] == "failed"
                         else None)
            # A row whose numbers cannot be trusted must not be sorted as if they
            # were live — a stale 40% would outrank a real 60% and send the operator
            # to the wrong account. Broken rows sink to the bottom.
            r["trust"] = (r["status"] in (None, "ok")) and (r["age"] is not None and r["age"] <= STALE_BAD_MIN)
        s.rows.sort(key=lambda r: (not r["trust"], max(r["five"] or 0, r["seven"] or 0), r["name"]))
        s.cur = active_email()
        # Mark the active row STRUCTURALLY. is_active_acct comes from
        # fleet_anchor(), which probes the shared slot itself; s.cur comes from
        # ~/.claude.json's cached identity, which can lag the credential it claims
        # to describe. Fall back to the email comparison only when the live check
        # could not answer at all.
        known = any(r.get("is_active_acct") for r in s.rows)
        for r in s.rows:
            r["live"] = r.get("is_active_acct") if known else (r["email"] == s.cur)

    def col(s, p):
        return [s.OK, s.OK, s.WARN, s.BAD][sev(p)]

    def em(s, email):
        """The address as it may appear on screen. Every place draw()/confirm()
        shows an email goes through here, never r["email"] or s.cur directly.
        A value with no "@" is a status such as "(not signed in)", not an
        address, and passes through so the row still says why it is empty."""
        if s.show_emails or not email or "@" not in email:
            return email
        return EMAIL_MASK

    def rowstate(s, r):
        """The one-line truth about whether this row's numbers can be believed.
        Returns (text, attr) or None when the row is fresh and healthy."""
        st, age = r.get("status"), r.get("age")
        if st == "unreadable":
            return ("Credential store unreadable — usage is unverified; inspect permissions/data", s.BAD | curses.A_BOLD)
        if st == "missing":
            return (f"No stored profile login — claude-login {r['name']}", s.BAD | curses.A_BOLD)

        # WHY three messages where there was one (2026-08-14): the original
        # collapsed "failed" and "timeout" into a single line asserting
        # "signed out?" — a cause it never checked. signed_in() was defined and
        # never called. That mattered twice over. A 90 s timeout on a box running
        # dozens of sessions with plugins and MCP servers is an ordinary false
        # positive, and its "fix: /login" advice is wrong there. The reason first
        # given for that (a second login invalidates the first) was later measured
        # false: two logins for one account coexist (gotcha #1, correction). The
        # advice is still harmful for a different reason: a bare /login overwrites
        # whatever login is in the shared slot, and when that is an account's OWN
        # login (borrowed, gotcha #48) the account is signed out. Never tell the
        # operator to /login unless we checked that the row really is signed out.
        if st == "timeout":
            # Advice first, explanation after: a narrow terminal cuts the END of a line,
            # and the part that must survive is "do NOT /login".
            return ("⚠ do NOT /login: still signed in. Refresh timed out (>90s), "
                    "numbers above are old, press r",
                    s.WARN | curses.A_BOLD)
        if st == "limited":
            return ("do NOT /login: not signed out. The server is rate-limiting usage reads; "
                    "numbers return in a few minutes", s.WARN | curses.A_BOLD)
        if st == "failed":
            if r.get("auth"):
                return ("⚠ signed in, but /usage returned no report — transient, press r",
                        s.WARN | curses.A_BOLD)
            # Safe advice now, and it was not before. Under the old design this
            # line could fire on the ACTIVE account (whose own slot was empty by
            # design) and tell you to /login, which minted a second token for an
            # account that already had a live one and invalidated the first
            # (gotcha #1 by a different road). Every profile now owns its
            # credential, so a failed auth check here means genuinely signed out,
            # and claude-login writes that profile's slot and no other.
            return (f"⚠ SIGNED OUT — fix: claude-login {r['name']}",
                    s.BAD | curses.A_BOLD)
        if r.get("poisoned"):
            return ("⚠ usage cache is filed under a different account — ignored. "
                    "Self-heals on the next refresh", s.WARN | curses.A_BOLD)
        if age is None:
            return ("no data yet — press r", s.DIM)
        if age > STALE_BAD_MIN:
            return (f"⚠ STALE — {age:.0f} min old, do not trust these numbers", s.BAD | curses.A_BOLD)
        if age > STALE_WARN_MIN:
            return (f"{age:.0f} min old", s.WARN)
        le = r.get("login_expires")
        if le and 0 < le - time.time() < 72 * 3600:
            d = datetime.datetime.fromtimestamp(le).strftime("%a %#I:%M%p" if sys.platform == "win32" else "%a %-I:%M%p").replace("AM", "am").replace("PM", "pm")
            return (f"⚠ run claude-login {r['name']} before {d} ({(le - time.time()) / 3600:.0f}h): "
                    f"this login can't renew after that", s.WARN | curses.A_BOLD)
        if r.get("borrowed"):
            # Tell the operator WHY this row behaves differently and what not to do:
            # its only local login is in the shared slot, so /login would erase it.
            return ("don't /login until you swap away: on the fleet with its OWN login "
                    "(claude-swap), live numbers", s.DIM)
        return None

    def put(s, y, x, t, a=0):
        h, w = s.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                s.scr.addstr(y, x, t[:max(0, w - x - 1)], a)
            except curses.error:
                pass

    def draw(s):
        s.scr.erase()
        h, w = s.scr.getmaxyx()
        wide = w >= 96
        # Three meters need a third column. Tiers differ in WIDTH only, never in
        # completeness: a narrower terminal loses the bars, never a number.
        # Dropping a meter to fit is precisely how a scoped limit pinned at 100%
        # stayed invisible on three accounts.
        xwide = w >= 118

        s.put(0, 0, "CLAUDE ACCOUNTS", curses.A_BOLD)
        # One lane again: the shared slot is the FLEET — sessions, new terminals
        # and scheduled jobs all follow it, so the header names exactly that.
        _an = active_profile()
        s.put(0, 17, f"{'DEFAULT' if cswitch.IS_WINDOWS else 'FLEET'}: {s.em(s.cur) or '(signed out?)'}"
                     + (f"  ({_an})" if _an else "  (no anchor profile)"), s.ACC)

        # AUTO-SWITCH line. Must never imply it is working when no watcher runs.
        en, st = auto_state()
        # Three things a user needs to know, never merged into one "ON": is a watcher
        # running, will it alert, and will it SWAP by itself (see auto_armed()). The
        # texts stay short enough to end before "a = toggle" at 80 columns.
        if st == "off":
            s.put(1, 0, "AUTO-SWITCH  ○ OFF", s.DIM)
            s.put(1, 20, "no alerts, no swaps", s.DIM)
        elif st == "up" and auto_armed():
            s.put(1, 0, "AUTO-SWITCH  ● ARMED", s.OK | curses.A_BOLD)
            s.put(1, 22, "will swap ONCE at the threshold, then disarm", s.DIM)
        elif st == "up":
            s.put(1, 0, "AUTO-SWITCH  ● ALERTS ONLY", s.WARN | curses.A_BOLD)
            s.put(1, 28, "will NOT swap by itself", s.DIM)
        elif st == "dead":
            s.put(1, 0, "AUTO-SWITCH  ● ON", s.WARN | curses.A_BOLD)
            s.put(1, 20, "⚠ NO WATCHER RUNNING: no alerts, no swaps", s.BAD | curses.A_BOLD)
        elif st == "halted":
            s.put(1, 0, "AUTO-SWITCH  ⚠ HALTED", s.BAD | curses.A_BOLD)
            s.put(1, 24, "manual repair needed — see autoswitch.log", s.BAD)
        else:
            s.put(1, 0, "AUTO-SWITCH  ⚠ CONFIG UNREADABLE — watcher will refuse to run", s.BAD | curses.A_BOLD)
        s.put(1, max(0, w - 12), "a = toggle", s.DIM)
        s.put(2, 0, "─" * (w - 1), s.DIM)

        # The scoped-meter label comes from the PAYLOAD, never a constant. The
        # server calls it whatever it calls it ("Fable" today); hardcoding the
        # name would quietly mislabel the column after a rename to "Fable 5" or
        # when a second scoped model appears.
        snames = set()
        for r in s.rows:
            ps = primary_scoped(r)
            if ps and ps.get("name"):
                snames.add(ps["name"])
        slabel = list(snames)[0] if len(snames) == 1 else "MODEL"

        y = 3
        if xwide:
            s.put(y, 0, f"{'':3}{'ACCOUNT':<11}{'EMAIL':<26}{'SESSION (5h)':<16}"
                        f"{'WEEK (7d)':<16}{slabel.upper()+' (7d)':<16}{'RESETS  5h / week'}",
                  curses.A_BOLD | curses.A_DIM)
            y += 1
        elif wide:
            s.put(y, 0, f"{'':3}{'ACCOUNT':<11}{'EMAIL':<22}{'SESSION':<12}{'WEEK':<12}"
                        f"{slabel.upper():<12}{'RESETS  5h / week'}", curses.A_BOLD | curses.A_DIM)
            y += 1

        for i, r in enumerate(s.rows, 1):
            f, sv = r["five"], r["seven"]
            ps = primary_scoped(r)
            pv = ps["pct"] if ps else None
            live = r.get("live")
            mark = "▸" if live else " "
            s.put(y, 0, f"{mark}{i} ", s.ACC | curses.A_BOLD if live else s.DIM)
            s.put(y, 3, f"{r['name']:<11}", curses.A_BOLD if live else 0)

            if wide:
                # Email on the row, not in a separate list: two profile names can
                # differ by one character, so the profile name alone is not enough to
                # confirm which account you are about to switch to.
                em, elen = s.em(r["email"]), 25 if xwide else 21
                s.put(y, 14, (em[:elen - 1] + "…") if len(em) > elen else em, s.DIM)

                # ● marks the limit the SERVER says is currently binding this
                # account (limits[].is_active). It is the difference between
                # "0% session, plenty left" and "you cannot run Fable on this
                # account until Wednesday".
                if xwide:
                    cols, bw = ((40, f, "session"), (56, sv, "weekly_all"),
                                (72, pv, "weekly_scoped")), 7
                    for c, pct, kind in cols:
                        s.put(y, c, bar(pct, bw), s.col(pct))
                        s.put(y, c + bw + 1, f"{pct:>3.0f}%" if pct is not None else "  —",
                              s.col(pct) | curses.A_BOLD)
                        if r.get("binding") == kind:
                            s.put(y, c + bw + 6, "●", s.col(pct) | curses.A_BOLD)
                    cres = 88
                else:
                    # No bars at this width, but every meter still shows its number
                    # and its label. Geometry: label at c, percent at c+6, binding
                    # dot at c+10, next column at c+12 — ends at 93 so it survives
                    # a terminal exactly at the 96-column threshold.
                    for c, lab, pct, kind in ((36, "5h", f, "session"),
                                              (48, "wk", sv, "weekly_all"),
                                              (60, slabel[:5], pv, "weekly_scoped")):
                        s.put(y, c, lab, s.DIM)
                        s.put(y, c + 6, f"{pct:>3.0f}%" if pct is not None else "  —",
                              s.col(pct) | curses.A_BOLD)
                        if r.get("binding") == kind:
                            s.put(y, c + 10, "●", s.col(pct) | curses.A_BOLD)
                    cres = 72
                s.put(y, cres, f"{when(r['five_at'])} / {when(r['seven_at'], False)}", s.DIM)
                y += 1
                # Every row that cannot be trusted says WHY, on its own line, in
                # colour. Silence here is what let a 46-minute-old number pass for live.
                note = s.rowstate(r)
                if note:
                    s.put(y, 14, note[0], note[1])
                    y += 1
            else:
                # Narrow dock: stack ALL THREE meters, each LABELLED. Completeness
                # is never traded for fit — only layout changes. Two bare
                # percentages with nothing saying which window they belong to is
                # exactly the "fast but wrong" failure this screen must avoid, and
                # omitting the scoped meter entirely is a worse version of it.
                s.put(y, 14, s.em(r["email"])[:26], s.DIM)
                y += 1
                for lab, pct, kind, at, hrs in (
                        ("5h", f, "session", r["five_at"], True),
                        ("wk", sv, "weekly_all", r["seven_at"], False),
                        (slabel[:5].lower(), pv, "weekly_scoped",
                         (ps or {}).get("resets_at"), False)):
                    s.put(y, 3, lab, s.DIM)
                    s.put(y, 9, f"{pct:>3.0f}%" if pct is not None else "  —",
                          s.col(pct) | curses.A_BOLD)
                    s.put(y, 14, bar(pct, 10), s.col(pct))
                    if r.get("binding") == kind:
                        s.put(y, 25, "●", s.col(pct) | curses.A_BOLD)
                    s.put(y, 27, when(at, hrs), s.DIM)
                    y += 1
                y += 1

            if not wide:
                note = s.rowstate(r)
                if note:
                    s.put(y - 1, 3, note[0][:w - 4], note[1])
                    y += 1

        # The recommendation, stated outright — the screen answers the question,
        # it does not merely present data and leave the reader to do the work.
        y += 1
        # Only rows we actually trust may produce a recommendation. Recommending
        # from a stale or unrefreshable row is the fast-and-wrong failure mode.
        live = [r for r in s.rows if r["five"] is not None and r["trust"]]
        broken = [r for r in s.rows if not r["trust"]]
        if broken:
            names = ", ".join(r["name"] for r in broken)
            s.put(y, 0, f"⚠ excluded from the recommendation (stale or not refreshing): {names}",
                  s.BAD | curses.A_BOLD)
            y += 1
        if live:
            best = live[0]
            s.put(y, 0, "→ most headroom: ", curses.A_BOLD)
            s.put(y, 17, f"{best['name']} ({best['five']:.0f}% / {best['seven']:.0f}%)", s.OK | curses.A_BOLD)
            if not best.get("live"):
                how = ("no-browser swap; browser only if it must" if swap_on()
                       else "opens the normal browser sign-in")
                advice = (f"  new pinned session: claude-as {best['name']}" if cswitch.IS_WINDOWS else
                          f"  press {s.rows.index(best)+1} — moves everything ({how})")
                s.put(y + 1, 0, advice, s.ACC)
                y += 1
            y += 1

            # A SECOND recommendation, deliberately not merged into the first.
            # A scoped meter at 100% does not block Opus or Sonnet work, so folding
            # it into one headroom score would steer you off a perfectly usable
            # account. But it is invisible in the 5h/7d numbers, and on 2026-08-14
            # it was the BINDING limit on three of four accounts while every one of
            # them displayed 0% session.
            sc = [(r, primary_scoped(r)) for r in live]
            sc = [(r, p) for r, p in sc if p and p["pct"] is not None]
            if sc:
                br, bp = min(sc, key=lambda t: t[1]["pct"])
                lbl, left = bp["name"], 100 - bp["pct"]
                if bp["pct"] >= 95:
                    s.put(y, 0, f"⚠ {lbl} is exhausted on every account — best is "
                                f"{br['name']} at {left:.0f}% left, resets {when(bp['resets_at'], False)}",
                          s.BAD | curses.A_BOLD)
                else:
                    s.put(y, 0, f"→ for {lbl}: {br['name']} ({left:.0f}% left, "
                                f"resets {when(bp['resets_at'], False)})",
                          s.OK | curses.A_BOLD)
                y += 1
            worst = max(live, key=lambda r: max(r["five"] or 0, r["seven"] or 0))
            if max(worst["five"] or 0, worst["seven"] or 0) >= 90:
                # Name the limit that actually binds. This line used to print the
                # 5-hour % and reset even when the WEEKLY meter was the one at the
                # top — "side is out (0%) — resets —" while side sat at week 98%
                # (seen 2026-10-02 02:03).
                if (worst["seven"] or 0) >= (worst["five"] or 0):
                    s.put(y, 0, f"⚠ {worst['name']} is {'out' if (worst['seven'] or 0) >= 100 else 'nearly out'} "
                                f"for the week ({worst['seven']:.0f}%) — "
                                f"refills {when(worst['seven_at'], False)}", s.BAD)
                else:
                    s.put(y, 0, f"⚠ {worst['name']} is {'out' if (worst['five'] or 0) >= 100 else 'nearly out'} "
                                f"for this 5-hour window ({worst['five']:.0f}%) — "
                                f"resets {when(worst['five_at'])}", s.BAD)
                y += 1

        y = s.draw_forecast(y + 1)

        # The word in brackets follows the swap_enabled setting. Changing this text moves
        # the width at which the footer wraps to two rows (gotcha #56), so re-render
        # just under that width after any edit: tests/render_cockpit_states.py.
        keys = "1" if len(s.rows) == 1 else f"1-{len(s.rows)}"
        hint = (f"{keys} fleet-switch ({'swap' if swap_on() else 'browser'}) · p ping cold · u undo · "
                f"a auto · e emails · r refresh · q quit")
        if len(hint) > w - 1:
            # Short form for narrow terminals. WHY: put() cuts the end of a line, and
            # the end of the long form is "q quit".
            hint = f"{keys} switch · u undo · p ping · a auto · e emails · r refresh · q quit"
        if cswitch.IS_WINDOWS:
            hint = "p ping · a alerts · e emails · r refresh · q quit · login: use CLI"
        if s.refreshing:
            stamp = "refreshing… (free, no quota)"
        elif not s.last:
            stamp = "not refreshed yet — numbers are from disk"
        else:
            age = int(time.time() - s.last)
            stamp = f"updated {age//60}m{age%60:02d}s ago"
        one_row = w >= len(hint) + len(stamp) + 4
        # The message sits directly ABOVE the rule, wherever the rule lands. It
        # used to be pinned at h-3, which is exactly where the two-row footer
        # draws its rule, so on any terminal too narrow for a one-row footer the
        # rule overwrote it: switch results and refusal reasons were invisible.
        # Found 2026-10-08 when adding "e emails" to the hint widened the
        # one-row threshold from 121 to 132 columns and hid the "emails SHOWN"
        # warning at 130 columns. That warning must stay readable at any width.
        if s.msg:
            s.put(h - (3 if one_row else 4), 0, s.msg[:w - 1], s.WARN | curses.A_BOLD)
        # Narrow docks cannot fit both on one line, so the footer grows to two
        # rows and the rule moves up with it rather than being written over.
        if one_row:
            s.put(h - 2, 0, "─" * (w - 1), s.DIM)
            s.put(h - 1, 0, hint, s.DIM)
            s.put(h - 1, max(0, w - len(stamp) - 1), stamp, s.DIM)
        else:
            s.put(h - 3, 0, "─" * (w - 1), s.DIM)
            s.put(h - 2, 0, stamp, s.DIM)
            s.put(h - 1, 0, hint, s.DIM)
        s.scr.refresh()

    def draw_forecast(s, y):
        """The run-out forecast (claude-meters.py, refreshed by the watcher every
        5 min into forecast.json — read here, never computed in draw()).
        Answers the original author's question first ("if I keep this pace, do I run out
        before the accounts refill?") for each of the three paces, then the one
        action that helps (which account to burn first), then how far to trust it.
        A forecast file older than 15 min means the watcher is not refreshing it,
        and says so rather than showing old numbers as current (invariant #8)."""
        try:
            f = json.load(open(os.path.join(BASE, "forecast.json")))
        except Exception:
            s.put(y, 0, "FORECAST  not computed yet — the watcher writes it every 5 min" if watcher_alive()
                  else "FORECAST  needs the optional watcher (install_watcher.sh, see the README)", s.DIM)
            return y + 1
        age_min = (time.time() - f.get("ts", 0)) / 60
        s.put(y, 0, "FORECAST", curses.A_BOLD)
        s.put(y, 9, f" if you keep this pace, do you run out before refills?   "
                    f"never-runs-dry pace ≤ {f.get('safe_rate', 0):.1f} pts/h  "
                    f"(1 pt = 1% of one account's week)", s.DIM)
        y += 1
        if age_min > 15:
            s.put(y, 2, f"⚠ forecast is {age_min:.0f} min old — is the watcher running? "
                        f"(launchctl list | grep autoswitch)", s.BAD | curses.A_BOLD)
            return y + 1
        for name in ("7d", "48h", "24h"):
            p = (f.get("paces") or {}).get(name) or {}
            sc = (f.get("scenarios") or {}).get(name)
            if p.get("v") is None or not sc:
                s.put(y, 2, f"{name:>3} pace   — calibrating (needs ~1 h of meter history)", s.DIM)
                y += 1
                continue
            tag = "" if p.get("src") == "meters" else " est."
            if sc.get("first_dry_h") is None:
                txt, col = "enough until the refills", s.OK
            else:
                dry = datetime.datetime.fromtimestamp(f["ts"] + sc["first_dry_h"] * 3600)
                txt = (f"runs dry {dry.strftime('%a %#I:%M%p' if sys.platform == 'win32' else '%a %-I:%M%p').replace('AM','am').replace('PM','pm')} "
                       f"(in {sc['first_dry_h']:.0f}h) · blocked {sc['blocked_h']:.0f}h of next 7 days")
                col = s.BAD if sc["first_dry_h"] < 24 else s.WARN
            s.put(y, 2, f"{name:>3} pace {p['v']:5.1f} pts/h{tag}", curses.A_BOLD)
            s.put(y, 29, txt, col | curses.A_BOLD)
            y += 1
        sc = (f.get("scenarios") or {}).get("24h") or (f.get("scenarios") or {}).get("48h")
        if sc and sc.get("use_first"):
            s.put(y, 2, f"burn first: {sc['use_first']} (its weekly refill comes soonest — "
                        f"spending it first wastes the least)", s.ACC)
            y += 1
        acc = f.get("accuracy")
        if acc and acc.get("by_pace"):
            parts = [f"{n} {v['miss_pct']:+.0f}%" for n, v in acc["by_pace"].items()]
            s.put(y, 2, f"yesterday's forecast vs what happened: {' · '.join(parts)}", s.DIM)
        else:
            s.put(y, 2, f"accuracy check: after 24 h of history ({f.get('log_hours', 0):.1f} h so far)"
                        f" · 'est.' = from session logs, becomes exact as history fills", s.DIM)
        return y + 1

    def confirm(s, r):
        """Fleet-switch to this row: suspend curses, run the aimed browser login
        in THIS terminal, resume, report.

        Suspend-and-resume rather than a y/n prompt or a background subprocess:
        the login flow is interactive by design (it prints its URL and prompts,
        the browser opens, the user finishes there), so it needs the real tty.
        curses.endwin() hands the terminal back; the next refresh() restores the
        TUI. Everything inside try/finally so a login that dies mid-flow cannot
        strand the terminal in cooked mode with a dead cockpit."""
        s.msg = f"fleet-switching to {r['name']}…"
        s.draw()
        curses.endwin()
        print("\n" + "─" * 60)
        try:
            # COCKPIT v2 (2026-10-01): try the NO-BROWSER swap first. claude-swap
            # moves an existing login into the fleet slot (spare first, else the
            # account's own private login) and verifies it with the server.
            # Proven live 2026-10-01 20:37 (team -> ops, 22 s).
            # Fall back to the aimed browser login ONLY on exit 2 ("this account
            # has no login that can be moved"). Every other non-zero code means a
            # swap refused or was undone for a reason a browser login would not
            # fix — showing it is the right move, not retrying another way.
            # SYNC WARNING: exit codes defined in claude-swap.py SwapError docstring.
            rc = -1
            swap_rc = None
            # swap_enabled() is the user's opt-in (autoswitch.json, ships false). While it
            # is off, the number key goes straight to the browser login below: the one
            # path that never writes a keychain item itself. SYNC WARNING: the same
            # switch is enforced in claude-swap.py main() and claude-autoswitch.py cfg().
            if SWAP_PROVEN_ON and cswap.swap_enabled():
                swap_rc = subprocess.run([sys.executable, SWAP, r["name"]]).returncode
            if swap_rc == 0:
                rc = 0
            elif swap_rc in (None, 2):
                if swap_rc == 2:
                    print("\nNo movable login for this account — falling back to the browser login.\n")
                rc = cswitch.fleet_switch(r["name"])
            else:
                rc = 10 + swap_rc
        except Exception as e:
            rc = -1
            print(f"fleet_switch crashed: {e}")
        finally:
            print("─" * 60 + "\npress any key to return to the cockpit…")
            s.scr.nodelay(False)
            s.scr.getch()
            s.scr.nodelay(True)
            curses.flushinp()
            s.scr.refresh()
        # Codes from claude-switch.fleet_switch(). 3 and 4 mean a login HAPPENED
        # — never phrase them as "nothing changed" (the 2026-08-05 lesson).
        active_profile(force=True)
        if rc == 0:
            s.msg = f"FLEET is now {r['name']} ({s.em(r['email'])}) — everything follows"
        elif rc == 1:
            s.msg = f"refused before login — see the message above (nothing changed)"
        elif rc == 2:
            s.msg = "login window closed — fleet unchanged"
        elif rc == 3:
            s.msg = "⚠ browser granted a DIFFERENT account — fleet moved there; press the right row again"
        elif rc == 4:
            s.msg = "⚠ login happened but identity unreadable — press r to re-check"
        elif rc == 11:
            s.msg = "swap refused — nothing changed (reason printed above)"
        elif rc == 14:
            s.msg = "⚠ swap was undone after a failed check — fleet unchanged; see above"
        elif rc == 15:
            s.msg = "⚠ a keychain slot is DAMAGED — do NOT /login; run claude-swap for status"
        else:
            s.msg = "⚠ switch errored — run claude-switch from a plain terminal"
        s.load()

    def bg_refresh(s, names):
        """Refresh every account off the UI thread.

        Only ever touches primitives the draw loop reads (last/refreshing/dirty)
        and never calls curses or load() itself — see the note in run()."""
        if s.refreshing:
            return
        s.refreshing = True

        def work():
            try:
                refresh_all(names)
            except Exception:
                pass
            finally:
                s.last = time.time()
                s.refreshing = False
                s.dirty = True
        threading.Thread(target=work, daemon=True).start()

    def bg_ping(s):
        """Open the 5h window on every account that has none. Off the UI thread.

        WHY this exists: the 5-hour window opens on an account's FIRST inference
        call and expires exactly 5h later regardless of what happens in between.
        An idle account has no window at all, so its clock is not running — the
        moment you finally need it you start a fresh 5h countdown from zero.
        Pinging cold accounts now means their windows burn down in wall-clock
        time while you work on a different account.

        WHY it shells out instead of importing claude-ping: claude-ping imports
        THIS module (for read/parse_usage/profiles), so importing it back here
        is a cycle. A subprocess also means a crash in the ping path cannot take
        down the TUI, and the button provably runs the same code as the CLI.

        --fast is deliberate: this reuses the rows the cockpit already refreshed,
        so the button acts on exactly the state the operator is looking at rather
        than re-deriving a possibly different one behind their back."""
        if s.pinging or s.refreshing:
            return
        # Same predicate as claude-ping.is_cold(): no timestamp (never opened, or
        # the cache was wiped by an account change) OR one already in the past.
        # Duplicated rather than imported because importing claude-ping here is a
        # cycle. SYNC WARNING: if is_cold() changes, change this with it.
        def _cold(r):
            ra = r.get("five_at")
            if not ra:
                return True
            try:
                return datetime.datetime.fromisoformat(ra).astimezone() <= datetime.datetime.now().astimezone()
            except Exception:
                return True
        if not [r for r in s.rows if _cold(r)]:
            s.msg = "every account already has a live 5h window — nothing to ping"
            return
        s.pinging = True
        s.msg = "pinging cold accounts (opening their 5h windows)…"

        def work():
            try:
                r = subprocess.run([sys.executable, os.path.join(BASE, "claude-ping.py"), "--fast"],
                                   capture_output=True, text=True, timeout=240,
                                   stdin=subprocess.DEVNULL)
                out = (r.stdout or "") + (r.stderr or "")
                ok = [l for l in out.splitlines() if l.strip().startswith("\u2713")]
                bad = [l for l in out.splitlines() if l.strip().startswith("\u2717")]
                if not ok and not bad:
                    s.msg = "every account already has a live 5h window — nothing to ping"
                else:
                    names_ok = " ".join(l.split()[1] for l in ok)
                    names_bad = " ".join(l.split()[1] for l in bad)
                    s.msg = (f"5h window opened: {names_ok}" if ok else "")
                    if bad:
                        s.msg += f"{'  ·  ' if ok else ''}FAILED: {names_bad} — run claude-ping to see why"
            except subprocess.TimeoutExpired:
                s.msg = "ping timed out (>240s) — run claude-ping in a shell to see what stalled"
            except Exception as e:
                s.msg = f"ping failed: {type(e).__name__} — run claude-ping in a shell"
            finally:
                s.pinging = False
                s.dirty = True
                # Pull the meters immediately so the RESETS column reflects the
                # windows we just opened, instead of waiting out AUTO_REFRESH_SEC.
                # NOT `s.last = 0` — that value is the "never refreshed in this
                # process" sentinel and draw() renders it as such, so reusing it
                # here would print a staleness lie for the whole next cycle.
                try:
                    s.bg_refresh(profiles())
                except Exception:
                    pass
        threading.Thread(target=work, daemon=True).start()

    def run(s):
        names = profiles()
        if not names:
            # First launch on a fresh install lands here. It used to print "No profiles
            # yet." and quit after 3 seconds: a dead end with no next step.
            for i, line in enumerate([
                    "No accounts yet.",
                    "",
                    "Add each Claude account you use, one at a time:",
                    ("  claude-new-profile personal" if cswitch.IS_WINDOWS else
                     "  bash ~/.claude-profiles/new-profile.sh personal"),
                    "  claude-login personal",
                    "",
                    "Then run claude-cockpit again.   Press any key to close."]):
                s.put(i, 0, line, curses.A_BOLD if i == 0 else 0)
            s.scr.nodelay(False); s.scr.refresh(); s.scr.getch(); return
        # NON-BLOCKING startup refresh (2026-08-17). It used to run refresh_all()
        # inline before entering the key loop. refresh_all() probes every account
        # with a 90 s timeout, so the cockpit was unresponsive for 45-60 s after
        # launch and SILENTLY SWALLOWED the keypress you opened it to make — the
        # switch key did nothing and gave no feedback. Cached numbers are drawn
        # from disk immediately (clearly labelled as not-yet-refreshed) and the
        # live probe lands when it lands.
        s.load()
        s.bg_refresh(names)
        while True:
            # Pick up a completed background refresh. Done HERE rather than in the
            # worker thread because load() calls signed_in(), which shells out —
            # curses is not thread-safe and a redraw from a worker corrupts the
            # screen.
            if s.dirty and not s.refreshing:
                s.dirty = False; s.load()
            s.draw()
            k = s.scr.getch()
            if k in (ord("q"), 27):
                return
            if k in (ord("a"), ord("A")):
                err = toggle_auto()
                en, st = auto_state()
                s.msg = err or ("auto-switch ON — but NO WATCHER is running, so nothing will happen"
                                if st == "dead" else
                                "auto-switch ARMED: it will swap once at the threshold" if en and auto_armed() else
                                "alerts ON; Windows auto-swap is unsupported" if en and cswitch.IS_WINDOWS else
                                "alerts ON. It will NOT swap by itself: arm once with claude-autoswitch --arm"
                                if en else "auto-switch OFF")
            elif k in (ord("e"), ord("E")):
                # Reveal is a deliberate, visible act: the message stays on
                # screen while addresses are shown, so nobody records them by
                # accident after forgetting they pressed it.
                s.show_emails = not s.show_emails
                s.msg = ("emails SHOWN — press e to hide them again" if s.show_emails
                         else "emails hidden")
            elif k in (ord("r"), ord("R")):
                s.msg = ""; s.bg_refresh(profiles())
            elif k in (ord("p"), ord("P")):
                # No confirmation by design (the original author, 2026-08-28): "just type p
                # and it starts the timer". Safe to make unconfirmed because it
                # only touches accounts with NO live window, writes no credential,
                # and costs less than the 1% rounding floor of the 5h meter.
                s.bg_ping()
            elif k in (ord("u"), ord("U")):
                if cswitch.IS_WINDOWS:
                    s.msg = "Windows: use claude-switch --undo in a plain terminal; cockpit login keys are unverified"
                    continue
                # Undo is a fleet switch back to the previous account, which in
                # this design is another aimed browser login — same suspend
                # dance as confirm(), same "a login may have happened" honesty.
                s.msg = "fleet-switching back — finishing in the browser…"
                s.draw()
                curses.endwin()
                print("\n" + "─" * 60)
                try:
                    # COCKPIT v2: if the most recent fleet change was a no-browser
                    # swap, undo it the same way (moves the logins back). Only a
                    # browser switch being the latest change uses the v4 undo.
                    # SYNC WARNING: .last-swap.json is written by claude-swap.py,
                    # .last-switch.json by claude-switch.py.
                    _ls = os.path.join(BASE, ".last-swap.json")
                    _lw = os.path.join(BASE, ".last-switch.json")
                    # swap_enabled(): with the opt-in off, undo must not reach the
                    # token-moving path either, even if an earlier swap left a
                    # .last-swap.json behind. It falls through to the browser undo.
                    # (tests/test_swap_enabled.py S5 checks every launch of claude-swap
                    # in this file is guarded; it found this one unguarded.)
                    if SWAP_PROVEN_ON and cswap.swap_enabled() and os.path.exists(_ls) and (
                            not os.path.exists(_lw) or os.path.getmtime(_ls) > os.path.getmtime(_lw)):
                        rc = subprocess.run([sys.executable, SWAP, "--undo"]).returncode
                    else:
                        rc = cswitch.undo_fleet()
                except Exception as e:
                    rc = -1
                    print(f"undo crashed: {e}")
                finally:
                    print("─" * 60 + "\npress any key to return to the cockpit…")
                    s.scr.nodelay(False); s.scr.getch(); s.scr.nodelay(True)
                    curses.flushinp(); s.scr.refresh()
                active_profile(force=True)
                s.msg = ("fleet restored to the previous account" if rc == 0
                         else "undo did not complete — see above; fleet may be unchanged")
                s.load()
            elif ord("1") <= k <= ord("9"):
                if not SWITCH_PROVEN_ON:
                    s.msg = ("Windows: use claude-switch <name> in a plain terminal" if cswitch.IS_WINDOWS else
                             "switching is DISABLED — a credential mutation was "
                             "reintroduced into claude-switch.py. Use claude-as <name>, "
                             "and run verify.sh (D3) to see what tripped.")
                else:
                    i = k - ord("1")
                    if i < len(s.rows):
                        s.confirm(s.rows[i])
            if time.time() - s.last > AUTO_REFRESH_SEC:
                s.bg_refresh(profiles())
            time.sleep(0.15)


if __name__ == "__main__":
    cswitch.require_claude()    # before curses takes the screen, so the message is readable
    try:
        curses.wrapper(lambda scr: UI(scr).run())
    except KeyboardInterrupt:
        pass
