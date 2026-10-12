#!/usr/bin/env python3
"""
claude-usage — how much subscription is left on every account, at a glance.

  claude-usage          refresh all accounts, then show   (~5s, costs nothing)
  claude-usage --fast   read the last known numbers off disk, no refresh (instant)

WHY refreshing is free: `claude -p "/usage"` is a local command that does an
authenticated GET to /api/oauth/usage. It is not an inference call. Verified
2026-08-04 by running it four times against a completely idle account
(side@example.com) — it stayed at 0% every time.

WHY it needs a refresh at all: the numbers are cached in each profile's
.claude.json, and an account you have not used in days holds stale numbers.
The cache is also WIPED whenever the signed-in account changes, so a
freshly-signed-in profile has nothing until something fetches.
"""
import json, os, sys, subprocess, datetime
import sys as _sys; _sys.dont_write_bytecode = True  # gotcha #13
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.expanduser("~/.claude-profiles")
# SYNC WARNING: must stay the installer-maintained symlink, never a version path.
# `claude` on PATH can resolve to a terminal app's shim or to a stale install.
# COCKPIT_CLAUDE_BIN overrides it for installs that live elsewhere; the full list of
# places this lookup is repeated is in claude-switch.py next to CLAUDE_BIN.
CLAUDE = os.environ.get("COCKPIT_CLAUDE_BIN") or os.path.expanduser("~/.local/bin/claude.exe" if sys.platform == "win32" else "~/.local/bin/claude")

C = {"red": "\033[31m", "yel": "\033[33m", "grn": "\033[32m",
     "dim": "\033[2m", "bold": "\033[1m", "off": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def colour(pct):
    if pct is None:
        return C["dim"]
    return C["red"] if pct >= 80 else C["yel"] if pct >= 50 else C["grn"]


def bar(pct, width=10):
    if pct is None:
        return C["dim"] + "·" * width + C["off"]
    filled = min(width, int(round(pct / 100 * width)))
    return colour(pct) + "█" * filled + C["dim"] + "░" * (width - filled) + C["off"]


def when(iso):
    """Reset time as something a human can act on, in local time."""
    if not iso:
        return "—"
    try:
        t = datetime.datetime.fromisoformat(iso).astimezone()
    except Exception:
        return "—"
    hrs = (t - datetime.datetime.now().astimezone()).total_seconds() / 3600
    if hrs < 0:
        return "due"
    stamp = t.strftime("%#I:%M%p" if sys.platform == "win32" else "%-I:%M%p").lower() if hrs < 24 else t.strftime("%a %#I%p" if sys.platform == "win32" else "%a %-I%p").lower()
    return f"{stamp} ({hrs:.0f}h)"


def profiles():
    if not os.path.isdir(BASE):
        return []
    return sorted(d for d in os.listdir(BASE)
                  if d != "__pycache__"    # claude plants .claude.json into dirs it's pointed at (gotcha #13)
                  and os.path.isfile(os.path.join(BASE, d, '.claude.json')))


REFRESH_STATUS = {}

# SYNC WARNING: read/parse/active-detection are IMPORTED from claude-cockpit.py,
# not reimplemented here. Two tools that answer "is this account signed out?"
# differently is exactly the defect being fixed — on 2026-08-14 the cockpit was
# taught that the ACTIVE account is signed out of its own profile by design while
# this file was not, so they would have disagreed about the same account in the
# same second. One implementation, two front ends. Changing read() there changes
# it here; that is the point. Importing is side-effect free (curses is only
# initialised inside curses.wrapper, which lives under a __main__ guard).
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("ckpit", os.path.join(BASE, "claude-cockpit.py"))
ckpit = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(ckpit)


def refresh(name):
    """Record whether the refresh actually WORKED. The exit code proves nothing —
    a signed-out profile exits 0 and prints a cost summary instead of the report,
    so success must be judged on the CONTENT. The earlier `except: pass` here let
    side sit 46 minutes on frozen numbers with nothing on screen to say so
    (2026-08-04). The cockpit was fixed then; this file was not, until now.

    The `via_default` parameter was REMOVED 2026-08-17. It popped
    CLAUDE_CONFIG_DIR to probe the shared DEFAULT slot for the ACTIVE account,
    which was necessary only because the old switch MOVED that account's
    credential out of its own profile slot. Under v4 every account stays signed
    into its own profile (its anchor), so every row probes its own dir. The
    shared slot is the FLEET lane; its usage belongs to whichever account is
    fleet right now and shows on that account's own row (usage is per-account,
    gotcha #9), so nothing needs to read the shared lane here at all.

    Sets the var explicitly rather than inheriting: this tool may be run from a
    shell that already exports CLAUDE_CONFIG_DIR for the active profile, and
    inheriting it would probe one account N times instead of N accounts once."""
    native_error = ckpit.credential_status(name)
    if native_error:
        REFRESH_STATUS[name] = native_error
        return
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = os.path.join(BASE, name)
    try:
        # MINIMAL probe: hooks off + zero MCP servers — see claude-cockpit.py
        # refresh_one() for the full story (2026-08-24 pip temp-dir leak fix).
        # --bare would break OAuth. --no-session-persistence (2026-10-08):
        # without it every probe saved a session transcript — tens of thousands of
        # `/usage` transcripts by 2026-10-08, which pollute any tool that indexes session
        # history and slow `/usage` itself (it reads every transcript, cockpit
        # gotcha #45). WHY in full: claude-cockpit.py refresh_one().
        # SYNC WARNING: the `/usage` probe argv lives in exactly three places —
        # claude-cockpit.py refresh_one, claude-usage.py refresh (here) and
        # claude-ping.py PROBE_FLAGS — and they change together.
        # claude-autoswitch.py no longer spawns `/usage` (it reads
        # GET /api/oauth/usage via claude-swap).
        r = subprocess.run([CLAUDE, "-p", "/usage",
                            "--settings", '{"disableAllHooks":true}',
                            "--strict-mcp-config",
                            "--max-budget-usd", "0.02",
                            "--no-session-persistence"],
                           env=env, capture_output=True, timeout=90,
                           stdin=subprocess.DEVNULL, text=True)
        REFRESH_STATUS[name] = "ok" if "Current session" in (r.stdout or "") else "failed"
    except subprocess.TimeoutExpired:
        REFRESH_STATUS[name] = "timeout"
    except Exception:
        REFRESH_STATUS[name] = "failed"


read = ckpit.read
primary_scoped = ckpit.primary_scoped
active_profile = ckpit.active_profile
signed_in = ckpit.signed_in


def main():
    ckpit.cswitch.require_claude()
    fast = "--fast" in sys.argv
    names = profiles()
    if not names:
        print("No profiles. Create one: " + ("claude-new-profile <name>" if sys.platform == "win32" else
                                             "bash ~/.claude-profiles/new-profile.sh <name>"))
        return
    # force=True picks up a switch made since this process started. Still needed
    # for the active-row MARKER even though refresh() no longer varies by it.
    active_profile(force=True)
    if not fast:
        print(f"{C['dim']}refreshing {len(names)} accounts (free — no quota used)…{C['off']}",
              end="\r", flush=True)
        with ThreadPoolExecutor(max_workers=len(names)) as ex:
            list(ex.map(refresh, names))
        print(" " * 60, end="\r")

    rows = [r for r in (read(n) for n in names) if r]
    # Most headroom first — the top row is the answer to "which account do I use now".
    rows.sort(key=lambda r: (max(r["five"] or 0, r["seven"] or 0), r["name"]))

    # Scoped-meter label comes from the payload, never hardcoded. See the same
    # reasoning in claude-cockpit.draw().
    snames = {p["name"] for p in (primary_scoped(r) for r in rows) if p and p.get("name")}
    slabel = list(snames)[0] if len(snames) == 1 else "MODEL"

    print(f"\n{C['bold']}{'ACCOUNT':<11} {'EMAIL':<38} {'SESSION (5h)':<20} {'WEEK':<20} "
          f"{slabel.upper()+' (7d)':<20} {'RESETS (5h / week)'}{C['off']}")
    print(C["dim"] + "─" * 148 + C["off"])
    for r in rows:
        f, s = r["five"], r["seven"]
        ps = primary_scoped(r)
        pv = ps["pct"] if ps else None
        def cell(v, kind):
            # ● marks the limit the SERVER says is binding this account. Without it
            # a row reading "0% session / 80% week" looks healthy while the account
            # cannot run the scoped model at all.
            if v is None:
                return f"{C['dim']} — {C['off']} "
            dot = "●" if r.get("binding") == kind else " "
            return f"{colour(v)}{v:>3.0f}%{C['off']}{dot}"
        fp, sp, pp = cell(f, "session"), cell(s, "weekly_all"), cell(pv, "weekly_scoped")
        stale = ""
        st = r.get("credential_error") or REFRESH_STATUS.get(r["name"])
        # Never advise /login without checking. (Historical note: this comment
        # once claimed a second login kills the first grant — DISPROVEN
        # 2026-08-17, two independent grants per account coexist. The rule
        # survives because the advice would still be aimed at the wrong lane.)
        if st == "unreadable":
            stale = "  Credential store unreadable — usage is unverified; inspect permissions/data"
        elif st == "missing":
            stale = f"  No stored profile login — claude-login {r['name']}"
        elif st == "timeout":
            stale = f"  {C['yel']}⚠ TIMED OUT (>90s) — numbers are old; do NOT /login{C['off']}"
        elif st == "failed":
            # The is_active_acct branch here is gone (2026-08-17). It existed
            # because the active account was read through the shared DEFAULT slot
            # and its own profile dir was signed out by design. Every profile
            # now owns its credential and is probed the same way, so a failure on
            # the active row is a real failure and gets the same diagnosis.
            if signed_in(r["name"]):
                stale = f"  {C['yel']}⚠ signed in but /usage returned nothing — transient{C['off']}"
            else:
                stale = f"  {C['red']}⚠ SIGNED OUT — claude-login {r['name']}{C['off']}"
        elif r.get("poisoned"):
            stale = f"  {C['yel']}⚠ cache filed under another account — ignored{C['off']}"
        elif r["age"] is None:
            stale = f"  {C['dim']}(no data — run without --fast){C['off']}"
        elif r["age"] > 15:
            stale = f"  {C['red']}⚠ {r['age']:.0f}min old{C['off']}"
        elif r["age"] > 6:
            stale = f"  {C['yel']}{r['age']:.0f}min old{C['off']}"
        mark = "▸" if r.get("is_active_acct") else " "
        print(f"{mark}{r['name']:<10} {r['email']:<38} {bar(f)} {fp}  {bar(s)} {sp}  "
              f"{bar(pv)} {pp}  {when(r['five_at']):<16} {when(r['seven_at'])}{stale}")

    live = [r for r in rows if r["five"] is not None and not r.get("credential_error")]
    print()
    if not live:
        print(f"{C['dim']}No usage data yet. Run without --fast.{C['off']}")
    else:
        best = live[0]
        worst = max(live, key=lambda r: max(r["five"] or 0, r["seven"] or 0))
        # ADVICE REWRITTEN 2026-08-17. It used to say `claude-as <best>`, and had
        # a special case emitting bare `claude` when best was the active account
        # — because that account's credential had been moved into the shared
        # DEFAULT slot, leaving its own profile dir signed out, so claude-as on it
        # opened a session with no credential at all.
        #
        # Neither applies now. The right answer to "which account should I use" is
        # to SELECT it, which points every new terminal at it and costs nothing,
        # rather than to launch one session against it. claude-as still works and
        # is still right for a one-off, so it is offered as the secondary form.
        if best.get("is_active_acct"):
            print(f"{C['bold']}→ Stay on{C['off']} {C['grn']}{best['name']}{C['off']}"
                  f"  ({best['five']:.0f}% session, {best['seven']:.0f}% week)"
                  f"  {C['dim']}(already the fleet){C['off']}")
        else:
            print(f"{C['bold']}→ Switch:{C['off']} {C['grn']}claude-switch {best['name']}{C['off']}"
                  f"  ({best['five']:.0f}% session, {best['seven']:.0f}% week)"
                  f"  {C['dim']}· one-off instead: claude-as {best['name']}{C['off']}")
        # Second answer, for the scoped model. Deliberately separate: a scoped
        # meter at 100% does not block the other models, so folding it into one
        # headroom score would steer you off a usable account.
        sc = [(r, primary_scoped(r)) for r in live]
        sc = [(r, p) for r, p in sc if p and p["pct"] is not None]
        if sc:
            br, bp = min(sc, key=lambda t: t[1]["pct"])
            col = C["red"] if bp["pct"] >= 95 else C["grn"]
            print(f"{C['bold']}→ For {bp['name']}:{C['off']} {col}{br['name']}{C['off']}"
                  f"  ({100 - bp['pct']:.0f}% left, resets {when(bp['resets_at'])})")
        # Name the meter that ACTUALLY tripped. Reporting "0% session" for an
        # account whose WEEK is at 99% reads as a false alarm and trains you to
        # ignore the warning.
        wf, ws = worst["five"] or 0, worst["seven"] or 0
        if max(wf, ws) >= 90:
            if ws >= wf:
                print(f"{C['red']}⚠ {worst['name']} week is nearly out{C['off']} "
                      f"({ws:.0f}%) — resets {when(worst['seven_at'])}")
            else:
                print(f"{C['red']}⚠ {worst['name']} session is nearly out{C['off']} "
                      f"({wf:.0f}%) — resets {when(worst['five_at'])}")
    print()


if __name__ == "__main__":
    main()
