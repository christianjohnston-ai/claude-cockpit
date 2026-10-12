#!/usr/bin/env python3
"""
claude-autoswitch — watch the FLEET account and, when ARMED, swap it ONCE to the
healthiest eligible account with no browser (cockpit v2, 2026-10-01). Otherwise
it only alerts.

  claude-autoswitch --arm       turn on: the next time the fleet crosses
                                switch_at_pct it swaps ONCE, then disarms itself
  claude-autoswitch --off       turn everything off (no swap, no alerts)
  claude-autoswitch --status    live numbers + what it would do right now
  claude-autoswitch --once [--dry-run]   one evaluation, then exit
  claude-autoswitch             the watcher loop (launchd runs this:
                                com.claude-cockpit.autoswitch, KeepAlive)

Config: ~/.claude-profiles/autoswitch.json  (re-read every poll, no restart)
Log:    ~/.claude-profiles/autoswitch.log

HOW THE ONE SWAP WORKS
  Every poll_sec it asks the SERVER for the fleet account's live 5-hour and
  weekly usage (GET /api/oauth/usage with the fleet's own token). When either
  reaches switch_at_pct, it ranks every OTHER account that is (a) not in
  exclude_from_auto, (b) swappable — claude-swap has a login it can move in
  (spare, or the account's private login) — and (c) under BOTH ceilings
  (target_max_session_pct / target_max_week_pct), lowest 5-hour usage first.
  If `auto_swap` is true it runs `claude-swap <best>`; on exit 2 ("no movable
  login") it tries the next. After the FIRST swap attempt — success or failure —
  it writes auto_swap=false: one arming = at most one swap. The original author's rule
  (2026-10-01): "only swaps one time". Alerts keep running after that.

  `enabled` is the master switch (the cockpit's `a` key flips it): with it off,
  nothing is watched and nothing swaps, whatever auto_swap says.

WHAT IT WILL NOT DO
  - Never select a profile in `exclude_from_auto`. The original author's rule 2026-08-05,
    restated 2026-10-01: ops@example.com is never auto-picked. Still
    swappable BY HAND (claude-swap ops). The config FAILS CLOSED if the
    list is missing or unreadable — see cfg().
  - Never decide on cached numbers: the cached usage in .claude.json is written
    at most every 5 min (gotcha #8), and at the fleet's measured burn (~1-2% of a
    5h window per minute, 2026-10-01) that lag eats the whole 97% margin. Live
    server reads only.
  - Never rotate through accounts: one swap per arming, then alerts only.
  - Never swap when no candidate clears both ceilings. It notifies (rate-limited
    by cooldown_min) and stays armed, so it can still fire if one frees up.

WHY THE OLD "ALERT-ONLY FOREVER" RULE CHANGED (2026-08-17 -> 2026-10-01)
  It held because every fleet switch needed a browser login. claude-swap.py
  MOVES an existing login instead (no browser), with server-verified identity,
  whole-blob write checks and rollback — proven live three times on 2026-10-01
  (team->ops 20:37, ops->lab 21:01, plus the incident repair).
  Read docs/gotchas.md #43-#48 before changing anything here.

SYNC WARNING: imports claude-swap.py (Swapper.movable_source / account_usage,
  live_usage, server_whoami) and claude-switch.py (profile_for_email). The
  "swappable" rule must stay identical between a hand swap and this watcher —
  it lives in ONE place, Swapper.movable_source().
SYNC WARNING: claude-cockpit.py reads autoswitch.json `enabled` and this log's
  heartbeat (auto_state()) and flips `enabled` with its `a` key (toggle_auto()).
"""
import json, os, sys, time, datetime, subprocess, importlib.util
import sys as _sys; _sys.dont_write_bytecode = True  # gotcha #13

BASE = os.path.expanduser("~/.claude-profiles")
CFG_FILE = os.path.join(BASE, "autoswitch.json")
LOG = os.path.join(BASE, "autoswitch.log")
SWAP = os.path.join(BASE, "claude-swap.py")
_STDOUT_IS_TTY = sys.stdout.isatty()

DEFAULTS = {
    "enabled": True,
    "auto_swap": False,
    "swap_enabled": False,
    "exclude_from_auto": [],
    "switch_at_pct": 97,
    "target_max_session_pct": 60,
    "target_max_week_pct": 85,
    "poll_sec": 45,
    "cooldown_min": 20,
    "notify": True,
}

_spec = importlib.util.spec_from_file_location("cswitch", os.path.join(BASE, "claude-switch.py"))
cswitch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cswitch)
_spec2 = importlib.util.spec_from_file_location("cswap", SWAP)
cswap = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(cswap)
# Usage history + forecast (claude-meters.py). The watcher is the always-running
# process, so it owns the 5-minute meter snapshot and the forecast refresh even when
# auto-swap and alerts are OFF — the history must keep accruing either way.
_spec3 = importlib.util.spec_from_file_location("cmeters", os.path.join(BASE, "claude-meters.py"))
cmeters = importlib.util.module_from_spec(_spec3)
_spec3.loader.exec_module(cmeters)


def cfg():
    """FAIL CLOSED. An unreadable config is a STOP, never a fallback.

    WHY (found in review): DEFAULTS carries an EMPTY exclude_from_auto. The
    original version fell back to it whenever autoswitch.json failed to parse —
    so a single JSON typo would silently make `ops` auto-selectable and
    defeat the one rule the user actually set. A rule that a typo can erase is
    not a rule.
    """
    try:
        with open(CFG_FILE, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        log(f"FATAL: {CFG_FILE} is unreadable ({e}).")
        log("       Refusing to run — without a valid exclusion list every account "
            "would become auto-selectable.")
        sys.exit(1)

    if "exclude_from_auto" not in raw:
        log(f"FATAL: 'exclude_from_auto' is missing from {CFG_FILE}. Refusing to run.")
        log("       Set it explicitly, even to [], so the intent is recorded.")
        sys.exit(1)
    if not isinstance(raw["exclude_from_auto"], list):
        log(f"FATAL: 'exclude_from_auto' must be a list, got {type(raw['exclude_from_auto']).__name__}.")
        sys.exit(1)

    c = dict(DEFAULTS)
    c.update({k: v for k, v in raw.items() if not k.startswith("_")})

    # swap_enabled is the ONE opt-in for anything that rewrites a keychain login
    # (claude-swap by hand, the cockpit number keys, auto-swap here). It ships false.
    # WHY enforced here and not only in --arm: a config edited by hand could say
    # auto_swap true while swap_enabled is false. Forcing auto_swap off in the one
    # place every caller reads the config from makes that combination inert.
    # `is True` on purpose: the string "true" or the number 1 is not consent.
    # SYNC WARNING: same rule as claude-swap.py swap_enabled().
    c["swap_enabled"] = not cswitch.IS_WINDOWS and raw.get("swap_enabled") is True
    if not c["swap_enabled"]:
        c["auto_swap"] = False

    # Names are compared case-insensitively against directory names, so a config
    # typo in case cannot quietly un-exclude a profile.
    c["exclude_from_auto"] = [str(x).strip().lower() for x in c["exclude_from_auto"]]

    known = set(profiles())
    unknown = [x for x in c["exclude_from_auto"] if x not in known]
    if unknown:
        # A renamed/typo'd profile in the exclusion list means the rule silently
        # protects nothing. Loud, but not fatal — the remaining names still apply.
        log(f"⚠ exclude_from_auto names unknown profiles: {unknown} — they protect NOTHING. "
            f"Known profiles: {sorted(known)}")
    return c


def set_cfg(**updates):
    """Change keys in autoswitch.json, preserving every other key AND the
    _-prefixed comments; atomic replace (same method as the cockpit's
    toggle_auto(), which writes this file too)."""
    with open(CFG_FILE) as f:
        d = json.load(f)
    d.update(updates)
    tmp = CFG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, CFG_FILE)


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    # Under launchd, StandardOutPath ALREADY redirects stdout into this same file,
    # so writing it again duplicates every line. Only write directly when stdout is
    # a terminal (interactive run). Detected once at import, not per call.
    if _STDOUT_IS_TTY:
        try:
            with open(LOG, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


def notify(title, body):
    if cswitch.IS_WINDOWS:
        log(f"ALERT: {title}: {body}")  # foreground/log alerts, no macOS executable
        return
    # osascript, not a dependency. Failure here must never break the watcher.
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification {json.dumps(body)} with title {json.dumps(title)}'],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def profiles():
    return sorted(d for d in os.listdir(BASE)
                  if d != "__pycache__"    # claude plants .claude.json into dirs it's pointed at (gotcha #13)
                  and os.path.isfile(os.path.join(BASE, d, ".claude.json")))


def run_swap(target):
    """Run claude-swap in its own process and return its exit code.
    SYNC WARNING: codes defined in claude-swap.py SwapError docstring —
    0 swapped, 1 refused, 2 no movable login, 4 written then undone, 5 damaged slot."""
    r = subprocess.run([sys.executable, SWAP, target], capture_output=True, text=True, timeout=600)
    for line in ((r.stdout or "") + (r.stderr or "")).strip().splitlines():
        log(f"     swap| {line}")
    return r.returncode


_LAST_WORST = {}   # profile -> last seen max(five, seven); sets the fleet read cadence


class Watcher:
    """One decision cycle and its inputs. Every external effect is injectable so
    tests/test_autoswap.py can drive it without touching a real
    login or the real config."""

    def __init__(self, sw=None, swap_runner=run_swap, notifier=notify, cfg_writer=set_cfg,
                 profile_lister=profiles):
        self.sw = sw or cswap.Swapper(out=lambda *a: None)
        self.swap_runner = swap_runner
        self.notifier = notifier
        self.cfg_writer = cfg_writer
        self.profiles = profile_lister

    def fleet_now(self):
        """(profile, email, {five, seven}) for the fleet, from the SERVER, or None.
        Rate-limit aware (claude-swap.py USAGE RATE LIMIT): the shared cache is
        reused for up to 4 min while the fleet is under 85%, and only 1 min once it
        is close to the 97% trigger, where freshness matters. If the server is
        backing us off, the sessions' own reading in ~/.claude.json is used when it
        is fresher and filed under the same account."""
        o, _ = self.sw._oauth(self.sw.shared)
        who = self.sw.whoami(o)
        if not who:
            return None
        name = cswitch.profile_for_email(who[0])
        max_age = 60 if _LAST_WORST.get(name, 0) >= 85 else 240
        u = cswap.live_usage(o, name, max_age)
        age = cswap.usage_age(name)
        if u is None or age is None or age > 300:
            sdata, sage = cswap.session_cached_usage(who[1])
            if sdata is not None and (u is None or age is None or sage < age):
                u = cswap.summarize_usage(sdata)
        if not u:
            return None
        _LAST_WORST[name] = max(u["five"], u["seven"])
        return name, who[0], u

    def candidates(self, c, active):
        """Eligible accounts, best first, plus the reason every other one was skipped."""
        excluded = set(c["exclude_from_auto"])
        ok, rejected = [], []
        for p in self.profiles():
            if p == active:
                continue
            # Exclusion is checked FIRST and by lower-cased name, so a manual-only
            # account is never even read, let alone chosen.
            if p.strip().lower() in excluded:
                rejected.append((p, "never auto-picked (exclude_from_auto)")); continue
            if not self.sw.movable_source(p)[0]:
                rejected.append((p, "no movable login (private login missing or near its renewal deadline)")); continue
            u = self.sw.account_usage(p)
            if not u:
                rejected.append((p, "usage unreadable")); continue
            if u["five"] >= c["target_max_session_pct"]:
                rejected.append((p, f"session {u['five']:.0f}% ≥ {c['target_max_session_pct']}%")); continue
            if u["seven"] >= c["target_max_week_pct"]:
                rejected.append((p, f"week {u['seven']:.0f}% ≥ {c['target_max_week_pct']}%")); continue
            ok.append((p, u))
        ok.sort(key=lambda t: (t[1]["five"], t[1]["seven"]))
        return ok, rejected

    def evaluate(self, c, do_switch=True):
        """One cycle. Returns 'swapped' / 'alerted' / None (nothing to do)."""
        f = self.fleet_now()
        if not f:
            log("⚠ could not read the fleet's live usage — skipping this cycle")
            return None
        active, email, u = f
        worst = max(u["five"], u["seven"])
        log(f"fleet {email} ({active or 'no profile'}) — session {u['five']:.0f}%  week {u['seven']:.0f}%"
            f"  [auto_swap {'ARMED' if c['auto_swap'] else 'off'}]")
        if worst < c["switch_at_pct"]:
            return None

        log(f"→ {worst:.0f}% ≥ {c['switch_at_pct']}% — looking for a target")
        ok, rejected = self.candidates(c, active)
        for p, why in rejected:
            log(f"     skip {p}: {why}")
        if not ok:
            log("⚠ NO ELIGIBLE ACCOUNT. Not swapping, not rotating through exhausted ones.")
            if c["notify"]:
                self.notifier("Claude: nothing to switch to",
                              f"{email} at {worst:.0f}% and no eligible account.")
            return "alerted"

        if not (c["auto_swap"] and do_switch):
            t, tu = ok[0]
            log(f"→ {'[dry run] would swap' if c['auto_swap'] else 'ALERT (auto_swap off)'}: recommend {t} "
                f"(session {tu['five']:.0f}%, week {tu['seven']:.0f}%)")
            if c["notify"] and do_switch:
                # Name the command that will actually work: with the swap opt-in off,
                # claude-swap refuses, so the alert must point at the browser switch.
                cmd = "claude-swap" if c.get("swap_enabled") else "claude-switch"
                self.notifier("Claude fleet nearly spent",
                              f"{email} at {worst:.0f}%. Run: {cmd} {t}")
            return "alerted"

        # ONE-SHOT: disarm BEFORE swapping. If this process dies mid-swap (launchd
        # restart, crash), the restarted watcher must not fire a second swap.
        self.cfg_writer(auto_swap=False)
        for t, tu in ok:
            log(f"→ AUTO-SWAP {active} -> {t} (session {tu['five']:.0f}%, week {tu['seven']:.0f}%)")
            rc = self.swap_runner(t)
            if rc == 0:
                log(f"✓ fleet is on {t}. auto_swap is now OFF (one swap per arming).")
                self.notifier("Claude: auto-swapped",
                              f"{active} was at {worst:.0f}%. All sessions now on {t}. "
                              f"Auto-swap is OFF until you arm it again. Sessions already "
                              f"stopped on the limit need one message to continue.")
                return "swapped"
            if rc == 2:
                log(f"     {t} had no movable login after all — trying the next account")
                continue
            log(f"⚠ swap to {t} failed (exit {rc}) — not trying others. auto_swap is OFF.")
            self.notifier("Claude: auto-swap FAILED",
                          f"Swap to {t} failed (exit {rc}). Fleet unchanged. See autoswitch.log.")
            return "alerted"
        log("⚠ every candidate refused (no movable login). auto_swap is OFF.")
        self.notifier("Claude: auto-swap could not run",
                      "No account had a login it could move. See autoswitch.log.")
        return "alerted"


def status():
    c = cfg()
    w = Watcher()
    armed = c["enabled"] and c["auto_swap"]
    print(f"\nwatcher            {'ON' if c['enabled'] else 'OFF'}   (cockpit `a` key = master switch)")
    print(f"auto-swap          {'ARMED — will swap ONCE, then disarm' if armed else 'off (alerts only)' if c['enabled'] else 'off'}")
    print(f"no-browser swap    {'allowed' if c['swap_enabled'] else 'OFF (set swap_enabled true in autoswitch.json to allow it)'}")
    print(f"triggers at        {c['switch_at_pct']}% of the 5-hour OR weekly limit (live server numbers)")
    print(f"target must be     < {c['target_max_session_pct']}% session AND < {c['target_max_week_pct']}% week, and swappable")
    print(f"never auto-picked  {', '.join(c['exclude_from_auto']) or '(none)'}   ← still swappable by hand")
    print(f"checks every       {c['poll_sec']}s\n")
    f = w.fleet_now()
    if not f:
        print("FLEET  (live usage unreadable)\n"); return
    active, email, u = f
    print(f"FLEET  {active} ({email})  session {u['five']:.0f}%  week {u['seven']:.0f}%\n")
    ok, rejected = w.candidates(c, active)
    for p, tu in ok:
        print(f"  ✓ {p:<10} session {tu['five']:.0f}%  week {tu['seven']:.0f}%" + ("   ← would pick" if p == ok[0][0] else ""))
    for p, why in rejected:
        print(f"  · {p:<10} {why}")
    print()


def main():
    a = sys.argv[1:]
    if "--status" in a:
        status(); return
    if "--arm" in a:
        c = cfg()   # fail closed on a bad config BEFORE arming anything
        if not c["swap_enabled"]:
            print("Auto-swap was NOT armed: the no-browser swap is turned off.\n"
                  "  Read docs/safety.md, then set  \"swap_enabled\": true  in %s" % CFG_FILE)
            sys.exit(1)
        set_cfg(enabled=True, auto_swap=True)
        log("ARMED by hand: one auto-swap at the next threshold crossing")
        print("Auto-swap ARMED: the next time the fleet crosses the threshold it swaps ONCE, then disarms.")
        status(); return
    if "--off" in a:
        set_cfg(enabled=False, auto_swap=False)
        log("turned OFF by hand (watcher + auto-swap)")
        print("Auto-swap and alerts are OFF."); return
    if "--once" in a:
        Watcher().evaluate(cfg(), do_switch="--dry-run" not in a); return
    dry = "--dry-run" in a
    log(f"watcher started (dry_run={dry})")
    idle_logged = False
    while True:
        c = cfg()
        cmeters.tick(log)     # due-checked inside; cheap when nothing is due
        # Disabled means SLEEP, not exit. Under launchd KeepAlive an exit would be
        # respawned immediately, so exiting would fight the cockpit's toggle and
        # burn a process launch every second. Sleeping also keeps the heartbeat
        # fresh, which is what the cockpit reads to say "watcher up".
        if not c["enabled"]:
            if not idle_logged:
                log("disabled in config — idling (toggle with `a` in claude-cockpit)")
                idle_logged = True
            else:
                try: os.utime(LOG, None)
                except Exception: pass
            time.sleep(c["poll_sec"])
            continue
        idle_logged = False
        try:
            if Watcher().evaluate(c, do_switch=not dry):
                log(f"cooldown {c['cooldown_min']}min")
                # The cooldown silences ALERTS, not the meter log. One long sleep
                # here left 20-minute holes in meters.jsonl and a stale forecast
                # all night on 2026-10-01 (the fleet sat >= 97% from ~23:00, so
                # the watcher alerted and slept every 20 min — 9 gaps in 3 h).
                # Sleep in poll-sized steps and keep the meters ticking.
                until = time.time() + c["cooldown_min"] * 60
                while time.time() < until:
                    time.sleep(min(c["poll_sec"], max(1, until - time.time())))
                    cmeters.tick(log)
                continue
        except Exception as e:
            log(f"cycle error: {e}")
        time.sleep(c["poll_sec"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("watcher stopped")
