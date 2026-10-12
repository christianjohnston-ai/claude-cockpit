#!/usr/bin/env python3
"""
claude-ping — open the 5-hour session window on accounts that have none.

  claude-ping              ping every COLD account (default)
  claude-ping personal team    ping these accounts regardless of state
  claude-ping --dry-run    show what would be pinged, spawn nothing
  claude-ping --fast       skip the pre-flight /usage refresh, trust the cache

WHY THIS EXISTS
The 5-hour window is not a rolling meter. It opens on an account's FIRST
inference call and expires exactly 5h later regardless of what you do in
between. An account you have not touched has no window open at all, so its
clock is not running — the moment you finally need it, you start a fresh 5h
countdown from zero. Pinging idle accounts early means their windows are
already burning down in wall-clock time while you work elsewhere.

WHY A REAL MESSAGE AND NOT `/usage`
`claude -p "/usage"` is a local authenticated GET to /api/oauth/usage, not an
inference call — it costs no quota AND it does not open a window. Proven
2026-08-27: the cockpit had been probing all five profiles every 300s since
2026-08-25, and work's five_hour.resets_at was still null. Only a real
inference call opens the window, which is why this tool sends a prompt.

WHY THE PREDICATE IS resets_at AND NOT utilization==0
An account reads 0% for the first minutes of an OPEN window, so testing the
percentage re-pings accounts whose clock is already running and burns quota to
achieve nothing. `five_hour.resets_at` is exact: null means no window has ever
been opened (or the cache predates one), a past timestamp means the last window
already expired. Measured 2026-08-27: work read 0% with a CLOSED window at
20:40 UTC and 2% with an OPEN one at 21:05 UTC. Only the timestamp told them
apart.

SYNC WARNING: read/parse_usage/profiles are IMPORTED from claude-cockpit.py,
never reimplemented. Four tools now answer "what is this account's window
state?" and they must not be able to disagree — see the same reasoning in
claude-usage.py's import block.
"""
import json, os, re, sys, subprocess, datetime
import sys as _sys; _sys.dont_write_bytecode = True  # gotcha #13: no __pycache__ in ~/.claude-profiles
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.expanduser("~/.claude-profiles")
if sys.platform == "win32":
    BASE = os.path.normpath(BASE)
# SYNC WARNING: must stay the installer-maintained symlink, never a version path.
# `claude` on PATH can resolve to a terminal app's shim or to a stale install that
# predates `auth status` entirely.
# COCKPIT_CLAUDE_BIN overrides it for installs that live elsewhere; the full list of
# places this lookup is repeated is in claude-switch.py next to CLAUDE_BIN.
CLAUDE = os.environ.get("COCKPIT_CLAUDE_BIN") or os.path.expanduser("~/.local/bin/claude.exe" if sys.platform == "win32" else "~/.local/bin/claude")
if sys.platform == "win32":
    CLAUDE = os.path.normpath(CLAUDE)

# Smallest useful payload. The point is to make ONE billable request, not to get
# an answer, so the prompt asks for a single token back. Deliberately NO --model
# flag: each profile's default is inherited. Model choice stays the
# account owner's decision, not this tool's, so do not hardcode a model here.
PING_PROMPT = "Reply with only the word: ok"

C = {"red": "\033[31m", "yel": "\033[33m", "grn": "\033[32m",
     "dim": "\033[2m", "bold": "\033[1m", "off": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}

# SYNC WARNING: same import shim as claude-usage.py. Side-effect free — curses is
# only initialised inside curses.wrapper, which lives under a __main__ guard.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("ckpit", os.path.join(BASE, "claude-cockpit.py"))
ckpit = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(ckpit)

read = ckpit.read
profiles = ckpit.profiles


def is_cold(r):
    """True when the account has NO live 5-hour window, so a ping would open one.

    Returns True for a missing timestamp (never opened, or the cache was wiped by
    an account change) and for an expired one (window already elapsed). Both are
    states where the clock is not running."""
    if not r or r.get("credential_error"):
        return False
    ra = r.get("five_at")
    if not ra:
        return True
    try:
        return datetime.datetime.fromisoformat(ra).astimezone() <= datetime.datetime.now().astimezone()
    except Exception:
        # An unparseable timestamp is not evidence of a live window. Treat it as
        # cold rather than silently skipping an account forever on a format change.
        return True


# WHY these flags and not the /usage probe's: measured 2026-08-28. The default
# system prompt carries the global CLAUDE.md, the project CLAUDE.md and every
# skill description — a plain `-p "hi"` billed 116,456 cache-creation tokens to
# say one word. Replacing the system prompt and limiting setting sources took
# that to 0 with the window still opening. A ping is a doorbell, not a session;
# it needs no project context at all.
#
# NO --max-budget-usd HERE, deliberately. It is a PRE-FLIGHT ESTIMATE, not a
# spend cap, and the estimate is account-dependent: measured 2026-08-28, the
# SAME flags at 0.50 were accepted on work and REJECTED on ops
# ("Error: Exceeded USD budget (0.5)"). Worse, a rejection exits 0 and prints
# nothing to stderr, so the feature would silently no-op on whichever accounts
# happened to price high — exactly the accounts most worth warming. A cap that
# fires unpredictably is worse than none when it caps nothing real.
#
# What actually bounds this call: the replaced system prompt (no CLAUDE.md, no
# skills, no tools), a one-word prompt, single-shot -p, zero MCP servers, and
# the 90s timeout in _run(). Measured burn is under the 1% rounding floor of
# the 5h meter. If a future claude gains a real turn/token cap, add it here.
# --setting-sources "" (empty) loads NO settings sources at all, which is what
# finally drops the global CLAUDE.md. Measured 2026-08-28: with "user" the reply
# still followed instructions from the global CLAUDE.md, proving it was
# being read; with "" the reply is a bare "Ok". A doorbell needs no house rules.
PING_FLAGS = ["--system-prompt", "Reply with one word.",
              "--setting-sources", "",
              "--settings", '{"disableAllHooks":true}',
              "--strict-mcp-config",
              "--no-session-persistence"]

# SYNC WARNING: the `/usage` probe argv lives in exactly three places —
# claude-cockpit.py refresh_one(), claude-usage.py refresh() and PROBE_FLAGS
# here — and they change together. claude-autoswitch.py no longer spawns
# `/usage` (it reads GET /api/oauth/usage via claude-swap). PING_FLAGS above
# already carried --no-session-persistence; PROBE_FLAGS gained it 2026-10-08
# without it every `/usage` probe saved a session transcript —
# tens of thousands of them by 2026-10-08 — which pollutes any tool that indexes
# session history and slows `/usage` itself (it reads every transcript, cockpit gotcha #45).
# Full WHY: claude-cockpit.py refresh_one(). disableAllHooks stops the security-guidance
# SessionStart hook, which pip-installed a ~100MB venv on EVERY spawn and was
# killed mid-install when the seconds-long session exited (~40k orphaned
# pip-unpack-* dirs/day in $TMPDIR, tens of GB steady state, found+fixed
# 2026-08-24). --strict-mcp-config with no --mcp-config boots zero MCP servers.
# --bare is NOT usable: it skips keychain/OAuth, which is the auth we need.
PROBE_FLAGS = ["--settings", '{"disableAllHooks":true}',
               "--strict-mcp-config",
               "--max-budget-usd", "0.02",
               "--no-session-persistence"]


def _run(name, args):
    """One MINIMAL claude session against ONE profile, fully reaped.

    subprocess.run (not Popen) is load-bearing: it waits and reaps, so no child
    can outlive this call or linger as a zombie. The timeout is the only thing
    between a hung session and an accumulating process pile.

    cwd is forced to $HOME so no project CLAUDE.md is picked up from wherever the
    caller happened to be standing — a ping fired from a repo would silently cost
    that repo's whole context."""
    if ckpit.credential_status(name):
        raise RuntimeError("Profile credential store is missing or unreadable; no ping was sent.")
    env = dict(os.environ)
    # Set explicitly rather than inherit: this may run from a shell that already
    # exports CLAUDE_CONFIG_DIR (a claude-as session), which would otherwise ping
    # that one account N times instead of N accounts once each.
    env["CLAUDE_CONFIG_DIR"] = os.path.join(BASE, name)
    return subprocess.run([CLAUDE, "-p"] + args,
                          env=env, cwd=os.path.expanduser("~"),
                          capture_output=True, timeout=90,
                          stdin=subprocess.DEVNULL, text=True)


SESSION_LINE = re.compile(r"Current session:\s*(.+)")


def warm(name):
    """Ping an account, then read the LIVE meter back. Returns (name, ok, detail).

    WHY the proof comes from /usage STDOUT and not the profile's .claude.json:
    the cached copy is write-throttled to once per 300s (gotcha #8), so an
    immediate re-read after a ping is stale BY CONSTRUCTION and reports every
    success as a failure. Measured 2026-08-28 — ops's cache still said
    `resets: None` while the live meter already said `1% used, resets 3:30pm`.
    The first version of this function scored that as "no window after ping".

    Success is judged on the ping's own reply, never the exit code: a budget
    rejection AND a signed-out profile both exit 0 (gotcha #3), the first
    printing `Error: Exceeded USD budget`, the second a cost footer."""
    try:
        r = _run(name, [PING_PROMPT] + PING_FLAGS)
    except subprocess.TimeoutExpired:
        return (name, False, "ping exceeded 90s")
    except Exception as e:
        return (name, False, f"ping crashed: {type(e).__name__}")

    said = ((r.stdout or "") + (r.stderr or "")).strip()
    if not said:
        return (name, False, "ping returned nothing — profile may be signed out")
    if "Exceeded USD budget" in said or said.startswith("Error:"):
        return (name, False, said.replace("\n", " ")[:90])

    # Free, and it is the only unthrottled view of the meter we can get.
    try:
        u = _run(name, ["/usage"] + PROBE_FLAGS)
        m = SESSION_LINE.search(u.stdout or "")
        if m:
            return (name, True, m.group(1).strip())
    except Exception:
        pass
    return (name, True, "pinged (meter unreadable — check claude-usage)")


def ckpit_when(iso):
    """Local-time reset stamp. Small enough to inline rather than couple to the
    cockpit's display helper, which formats for a fixed-width TUI column."""
    if not iso:
        return "—"
    try:
        t = datetime.datetime.fromisoformat(iso).astimezone()
    except Exception:
        return "—"
    hrs = (t - datetime.datetime.now().astimezone()).total_seconds() / 3600
    return f"{t.strftime('%#I:%M%p' if sys.platform == 'win32' else '%-I:%M%p').lower()} ({hrs:.1f}h)"


def refresh(name):
    """Free /usage fetch so the cold/warm decision is made on current data.

    Without this the tool acts on whatever the cockpit last wrote, which can be
    5 minutes stale — long enough to miss a window that just expired."""
    try:
        _run(name, ["/usage"] + PROBE_FLAGS)
    except Exception:
        pass


def main():
    ckpit.cswitch.require_claude()
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry = "--dry-run" in sys.argv or "-n" in sys.argv
    fast = "--fast" in sys.argv

    names = profiles()
    if not names:
        print("No profiles. Create one: " + ("claude-new-profile <name>" if sys.platform == "win32" else
                                             "bash ~/.claude-profiles/new-profile.sh <name>"))
        return 1

    if argv:
        # Explicit targets are pinged whether or not they are cold — the caller
        # named them, so they get what they asked for.
        unknown = [a for a in argv if a not in names]
        if unknown:
            print(f"no such profile: {', '.join(unknown)}")
            return 1
        targets = argv
        why = "named on the command line"
    else:
        if not fast:
            print(f"{C['dim']}checking {len(names)} accounts (free — no quota used)…{C['off']}",
                  end="\r", flush=True)
            with ThreadPoolExecutor(max_workers=len(names)) as ex:
                list(ex.map(refresh, names))
            print(" " * 60, end="\r")
        targets = [n for n in names if is_cold(read(n))]
        why = "no live 5h window"

    if not targets:
        print(f"{C['grn']}Nothing to do{C['off']} — every account already has a live 5h window.")
        for n in names:
            r = read(n)
            print(f"  {C['dim']}{n:<11} resets {ckpit_when(r.get('five_at'))}{C['off']}")
        return 0

    print(f"{C['bold']}Pinging {len(targets)}{C['off']} ({why}): {', '.join(targets)}")
    if dry:
        print(f"{C['yel']}--dry-run: nothing spawned{C['off']}")
        return 0

    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        results = list(ex.map(warm, targets))

    bad = 0
    for name, ok, detail in results:
        if ok:
            print(f"  {C['grn']}✓ {name:<11}{C['off']} {detail}")
        else:
            bad += 1
            print(f"  {C['red']}✗ {name:<11}{C['off']} {detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
