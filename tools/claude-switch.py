#!/usr/bin/env python3
"""
claude-switch — put ALL your Claude sessions on the account you pick.

  claude-switch                 show who the fleet is on, and every account
  claude-switch work          fleet-switch: browser login aimed at work
  claude-switch --undo          fleet-switch back to the previous account
  claude-switch --active        print the fleet's anchor profile name (for scripts)

WHAT A SWITCH IS NOW (4th design, 2026-08-17 evening):
  One `claude auth login` into the SHARED keychain slot ("Claude Code-credentials"),
  run with CLAUDE_CONFIG_DIR scrubbed so it cannot land anywhere else, then
  VERIFIED: the tool checks that the account the browser actually granted is the
  one that was asked for, and says so either way. The browser round trip (~15s)
  is the price of safety: minting a fresh, independent grant is the only way to
  put an account's credential into a second slot without sharing a single-use
  refresh token (which killed seven live sessions on 2026-08-04).

  The shared slot is the FLEET LANE: interactive sessions run with no
  CLAUDE_CONFIG_DIR and read it, and any launchd agents read it
  too (launchd never sources ~/.zshrc). Rewrite that one slot and everything
  follows. Convergence truth (from the 2.1.234 binary, 2026-08-17): sessions in
  the 401 state adopt the new grant immediately (the 401-recovery family
  re-reads the keychain — _from_keychain / _from_disk / _from_rotation);
  healthy or quota-blocked sessions converge at their next token refresh (≤8h).
  A bare /login typed anywhere unpinned does the same thing as this tool,
  minus the aiming and the verification — it is allowed, not a footgun.

  Every account ALSO keeps its own permanent login in its own profile slot
  (its ANCHOR). The fleet grant is therefore always expendable: overwriting it
  can never destroy an account's only credential. That property is what makes
  a login-based switch safe, and it is why fleet_switch() refuses to run for
  an account that has no anchor yet.

DESIGN HISTORY, compressed (full detail in docs/how_it_works.md):
  v1 COPY a credential between slots — killed 7 live sessions (2026-08-04):
     a stored credential holds a SINGLE-USE refresh token, and the refresh
     lock is per-config-dir, so two holders have no mutual exclusion.
  v2 MOVE it; park target from cached identity — destroyed personal@example.com
     (2026-08-05) when the cache desynced from the keychain.
  v3 MOVE it; park target derived structurally ("the one empty slot") — refused
     three times in one afternoon (2026-08-17) because a bare /login breaks the
     invariant, and a brand-new profile impersonates the park target.
  v3.5 write .active, pin NEW terminals via CLAUDE_CONFIG_DIR (2026-08-17 am) —
     safe, but it switched the wrong thing: it never touched the RUNNING
     sessions, and pinning made the user's own /login habit land in private
     slots, silently splitting the fleet. Retired the same day.
  v4 (this) the user's /login habit IS the switch — aimed, then verified.

DESIGN RULE: every account is fleet-switchable, reachable like any other. An account
  blocklist was proposed and rejected. FLEET_BLOCKED below is the documented extension
  point and ships EMPTY.

SYNC WARNING: claude-cockpit.py imports this module (fleet_email, fleet_anchor,
  profile_for_email, profiles, fleet_switch, undo_fleet, FLEET_BLOCKED);
  claude-usage.py inherits those through claude-cockpit.py; claude-autoswitch.py
  imports it as well. verify.sh section D3 runs an AST gate over THIS file
  asserting it contains no keychain write or delete — the only credential
  writer allowed here is the official `claude auth login` subprocess.
"""
import json, os, subprocess, sys, hashlib, datetime, ntpath
# No bytecode cache: importing this module used to create
# ~/.claude-profiles/__pycache__/, and anything that points claude at a dir
# lacking .claude.json INITIALISES a fresh config there — which once turned
# __pycache__ into a fake sixth profile (gotcha #13, second bite 2026-08-17).
sys.dont_write_bytecode = True

IS_WINDOWS = sys.platform == "win32"

BASE = os.path.expanduser("~/.claude-profiles")
if sys.platform == "win32":
    BASE = os.path.normpath(BASE)
UNDO = os.path.join(BASE, ".last-switch.json")
# Claude's own name for the shared keychain slot. It is also the PREFIX of every
# per-config-dir slot (slot_for), so it must never change meaning.
SLOT_PREFIX = "Claude Code-credentials"
USER = os.environ.get("USER", "")


def _fleet_config_dir():
    """Which config dir IS "the fleet" on this machine. None = an ordinary machine model:
    the fleet is the shared default slot and ~/.claude.json, reached by ERASING
    CLAUDE_CONFIG_DIR. Set (fleet-dir mode) = the fleet is that one config dir's own
    slot, reached by SETTING CLAUDE_CONFIG_DIR to it.

    WHY this exists: on a machine where another long-running Claude process owns the
    shared default slot, every cockpit "fleet" operation would rewrite THAT process's
    login. Fleet-dir mode points "the fleet" at one named config dir instead, for example
    sessions started with CLAUDE_CONFIG_DIR=/Users/you/.claude, whose slot is
    Claude Code-credentials-<sha8>, and keeps the default slot out of every code path.

    Source order: env COCKPIT_FLEET_CONFIG_DIR, then the one-line file
    ~/.claude-profiles/fleet_config_dir. The string must equal the dir Claude is
    launched with BYTE FOR BYTE: Claude hashes the string, so a trailing slash
    names a different, empty slot (example: /Users/you/.claude -> 34f6278e,
    with a trailing slash -> a3b43677).

    Fails CLOSED. A bad value raises instead of falling back, because an unreadable
    fleet would otherwise look like an EMPTY slot, and empty is a legal swap
    destination (gotcha #43: a slot that cannot be read must never read as empty).
    """
    v = os.environ.get("COCKPIT_FLEET_CONFIG_DIR", "").strip()
    if not v:
        try:
            v = open(os.path.join(BASE, "fleet_config_dir")).read().strip()
        except OSError:
            v = ""
    if not v:
        return None
    if (not os.path.isabs(v) or v != os.path.normpath(v)
            or not os.path.isfile(os.path.join(v, ".claude.json"))):
        raise SystemExit(f"claude-switch: fleet config dir {v!r} is invalid (must be an "
                         f"absolute, normalised path containing .claude.json). Refusing to "
                         f"guess which slot is the fleet.")
    return v


FLEET_DIR = _fleet_config_dir()
CFG = (os.path.join(FLEET_DIR, ".claude.json") if FLEET_DIR
       else os.path.expanduser("~/.claude.json"))


def fleet_env(env):
    """Aim `env` at the fleet, in place. Ordinary machine: erase CLAUDE_CONFIG_DIR (invariant
    #4). Fleet-dir mode: set it to the fleet dir. Returns env for chaining."""
    if FLEET_DIR:
        env["CLAUDE_CONFIG_DIR"] = FLEET_DIR
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    return env


# Same formula as slot_for() below (which is defined later in this file, so it cannot be
# called here yet). test_fleet_dir.py F2/F3 pin that the two agree.
FLEET_SLOT = (f"{SLOT_PREFIX}-{hashlib.sha256(FLEET_DIR.encode()).hexdigest()[:8]}"
              if FLEET_DIR else SLOT_PREFIX)
if IS_WINDOWS:
    FLEET_SLOT = os.path.normpath(os.path.join(
        FLEET_DIR or os.path.expanduser("~/.claude"), ".credentials.json"))
DEFAULT_SLOT = FLEET_SLOT      # legacy name: every existing caller means "the fleet slot"
# Never bare `claude`: a PATH lookup can resolve to a terminal app's shim or to a stale
# install that predates `claude auth status` entirely.
# COCKPIT_CLAUDE_BIN is the override for installs that live somewhere else (npm,
# Homebrew). SYNC WARNING: the same two-step lookup is repeated in claude-cockpit.py,
# claude-usage.py, claude-ping.py (CLAUDE), claude-as.sh (CLAUDE_AS_BIN) and verify.sh.
CLAUDE_BIN = os.environ.get("COCKPIT_CLAUDE_BIN") or os.path.expanduser("~/.local/bin/claude.exe" if IS_WINDOWS else "~/.local/bin/claude")
if sys.platform == "win32":
    CLAUDE_BIN = os.path.normpath(CLAUDE_BIN)


def require_claude():
    """Stop with one clear line when Claude Code is not where we look.

    WHY: every tool here shells out to this binary. Without the check a missing
    binary shows up as a Python traceback, or in the cockpit as every row quietly
    failing to refresh, and neither tells a new user about the one-line fix.
    Called from each tool's entry point, never at import, so the tests and the
    modules that load this file stay importable on a machine without Claude Code."""
    if (os.path.isabs(CLAUDE_BIN) and os.path.isfile(CLAUDE_BIN)
            and (not IS_WINDOWS or CLAUDE_BIN.lower().endswith(".exe"))
            and os.access(CLAUDE_BIN, os.X_OK)):
        return
    if IS_WINDOWS:
        sys.stderr.write("claude-cockpit: set $env:COCKPIT_CLAUDE_BIN to the absolute path "
                         "of the native claude.exe (not an npm .cmd shim).\n")
        sys.exit(2)
    sys.stderr.write(
        "claude-cockpit: Claude Code was not found at %s\n"
        "  If it is installed somewhere else, add this line to ~/.zshrc and open a new terminal:\n"
        "    export COCKPIT_CLAUDE_BIN=/full/path/to/claude\n" % CLAUDE_BIN)
    sys.exit(2)

# Ships EMPTY by design: every account is fleet-switchable (see DESIGN RULE in the
# docstring). If a future version must fence an account off, the guard is one line:
# {"name": "reason shown to the user"}.
FLEET_BLOCKED = {}

# --- Chrome aiming -------------------------------------------------------
# The login's consent screen is chosen by the BROWSER, not by this file. Chrome
# delivers an `open`ed URL to its LAST-USED profile window, which on a machine
# with many profiles is almost never the one holding the account being switched to.
# Setting BROWSER routes it: the binary spawns `$BROWSER <url>` (2.1.236 zwp()).
# AIMING IS NOT VERIFICATION — the browser still decides which account is
# granted, so fleet_switch's post-login identity check remains the correctness
# mechanism (gotcha #28). This only changes who is OFFERED.
CHROME_STATE = os.path.expanduser("~/Library/Application Support/Google/Chrome/Local State")
CHROME_OVERRIDE = os.path.join(BASE, "chrome-profiles.json")   # optional; ships absent
OPENER = os.path.join(BASE, "claude-open-in-chrome.sh")
AIM_RECEIPT = os.path.join(BASE, ".last-aim")
_EMAIL_FLAG = None   # memoized probe. NEVER filled at import time: verify.sh's
                     # parity gate exec_module()s this file and must stay cheap.

C = {"grn": "\033[32m", "red": "\033[31m", "yel": "\033[33m",
     "dim": "\033[2m", "bold": "\033[1m", "off": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


# ---------------------------------------------------------------------------
# READ-ONLY helpers. Nothing in this file writes a keychain item.
# ---------------------------------------------------------------------------

def chrome_profile_for_email(email):
    """The Chrome profile DIRECTORY (e.g. 'Profile 6') signed into `email`, else None.

    DERIVED, never stored. Chrome's Local State already records each profile's
    Google account, and on the machine this was built on every Claude account matched
    exactly one (verified 2026-08-19). A hand-kept map would be one more thing to desync, which is the
    exact class of bug that let park_target() destroy an account (gotcha #17).

    Returns None on ZERO matches AND on TWO OR MORE. Refusing to guess costs one
    manual profile pick; guessing wrong walks you into a consent screen for the
    wrong account, which is how gotcha #28 happens.

    CAVEAT worth knowing: this joins the Claude account to Chrome's GOOGLE
    sign-in. The claude.ai session inside that profile's cookie jar is a THIRD
    identity nothing here can see, so a correctly-aimed profile can still offer
    the wrong account. That is why the caller prints a confirm-the-consent-screen
    line rather than implying the aim guarantees anything.

    Fails soft on EVERY error. Chrome writes Local State non-atomically while many
    profiles churn it, so a mid-write read is expected, not exceptional; a
    cosmetic feature must never take the switch down with it.
    """
    if IS_WINDOWS or not email:
        return None                      # "" would otherwise match every
                                         # blank-user_name profile (a typical
                                         # machine has several), and the 2+ rule
                                         # would save us only by luck.
    try:
        ov = json.load(open(CHROME_OVERRIDE))
        if isinstance(ov.get(email), str) and ov[email]:
            return ov[email]
    except Exception:
        pass
    try:
        info = json.load(open(CHROME_STATE))["profile"]["info_cache"]
        want = email.strip().lower()
        hits = [d for d, v in info.items()
                if (v.get("user_name") or "").strip().lower() == want]
        return hits[0] if len(hits) == 1 else None
    except Exception:
        return None


def chrome_label(dirname):
    """Chrome's human name for a profile dir, for display only."""
    try:
        return json.load(open(CHROME_STATE))["profile"]["info_cache"][dirname].get("name") or ""
    except Exception:
        return ""


def slot_for(path):
    """Claude Code derives a config dir's slot as sha256(dir)[:8]. Recomputed,
    never stored, so it cannot drift if a profile is renamed."""
    if IS_WINDOWS:
        return os.path.normpath(os.path.join(path, ".credentials.json"))
    return f"{SLOT_PREFIX}-{hashlib.sha256(path.encode()).hexdigest()[:8]}"


class CredentialReadError(Exception):
    """An unreadable store is never an empty/signed-out store."""


def read_windows_credential(slot):
    """Read Claude's native file, never write/copy tokens or inspect Credential Manager.

    Source: code.claude.com/docs/en/authentication#credential-management.
    A spare is a macOS-only concept. Unknown slots are refused, not guessed.
    """
    if slot.startswith("claude-cockpit-spare-"):
        return None
    allowed = {FLEET_SLOT} | {slot_for(os.path.join(BASE, n)) for n in profiles()}
    # expanduser on Windows can preserve '/' while pathlib/CLI callers use '\\'.
    # Match native separator/case semantics, but always open the allowlisted path,
    # never the caller's spelling (or an arbitrary path made absolute for it).
    by_key = {ntpath.normcase(ntpath.normpath(path)): path for path in allowed}
    trusted_path = by_key.get(ntpath.normcase(ntpath.normpath(slot)))
    if trusted_path is None:
        raise CredentialReadError("Unrecognized credential store; refusing to guess.")
    try:
        with open(trusted_path, encoding="utf-8-sig") as f:
            value = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise CredentialReadError("Credential store is unreadable; no login was changed.") from None
    if not isinstance(value, dict):
        raise CredentialReadError("Credential store is malformed; no login was changed.")
    oauth = value.get("claudeAiOauth")
    if oauth is not None and not isinstance(oauth, dict):
        raise CredentialReadError("Credential store is malformed; no login was changed.")
    return value


def read_cred_hex(slot):
    """Read a credential hex-encoded. The plaintext is never printed, never
    written to a file, and never returned to a caller that prints it."""
    if IS_WINDOWS:
        value = read_windows_credential(slot)
        return json.dumps(value, sort_keys=True).encode().hex() if value is not None else None
    r = subprocess.run(["security", "find-generic-password", "-a", USER, "-s", slot, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.strip().encode().hex()


def slot_fingerprint(slot):
    """SHA-256 prefix of a slot's credential, or None if empty. The only
    credential-truthful local comparison; used to detect that a login actually
    wrote something, and by verify.sh's shared-token check."""
    hexed = read_cred_hex(slot)
    if not hexed:
        return None
    return hashlib.sha256(hexed.encode()).hexdigest()[:16]


def has_claude_login(slot):
    """True only if the slot holds a CLAUDE login (claudeAiOauth with a refresh
    token). Read-only.

    WHY NOT slot_fingerprint() is not None: a slot also stores "mcpOAuth" (that
    profile's MCP-server logins). claude-swap.py moves ONLY claudeAiOauth out of a
    borrowed profile slot and deliberately leaves the item — and its MCP logins —
    in place, so "the item exists" no longer means "this profile is signed in".
    Measured 2026-10-01: ops's slot kept 3 MCP logins while its Claude login
    was on the fleet, and the borrowed-fleet guard below passed when it had to
    refuse (caught by a stubbed login, no browser opened)."""
    hexed = read_cred_hex(slot)
    if not hexed:
        return False
    try:
        o = (json.loads(bytes.fromhex(hexed).decode()) or {}).get("claudeAiOauth") or {}
    except Exception:
        return False
    return bool(o.get("refreshToken"))


def identity(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return (json.load(f).get("oauthAccount") or {})
    except Exception:
        return {}


def profiles():
    """A profile is a directory containing its own .claude.json — testing for
    the file is what keeps __pycache__ from being reported as a profile."""
    if not os.path.isdir(BASE):
        return []
    # __pycache__ excluded by NAME as well as by the .claude.json test: claude
    # itself PLANTS a .claude.json into any config dir it is pointed at, so the
    # file test alone stopped being sufficient the day a tool iterated raw dirs.
    return sorted(d for d in os.listdir(BASE)
                  if d != "__pycache__"
                  and os.path.isfile(os.path.join(BASE, d, '.claude.json')))


def authed_email(config_dir=None):
    """Who a config dir authenticates as; None = signed out, offline, or slow.
    config_dir=None means THE FLEET: the env is scrubbed, so this is correct
    even when the calling shell is pinned by claude-as (the bug class that made
    verify.sh lie when run from a pinned shell, found 2026-08-17)."""
    env = dict(os.environ)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        fleet_env(env)
    try:
        r = subprocess.run([CLAUDE_BIN, "auth", "status"],
                           env=env, capture_output=True, text=True, timeout=25)
        d = json.loads(r.stdout)
        return d.get("email") if d.get("loggedIn") else None
    except Exception:
        return None


_NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def valid_name(n):
    """Explicit ASCII set, NOT str.isalnum(): isalnum() accepts Cyrillic and
    fullwidth letters while the shell rule in new-profile.sh is ASCII-only, and
    that gap once let a homoglyph profile name split the tools' view of reality.
    SYNC WARNING: keep identical to new-profile.sh's `case *[!a-zA-Z0-9_-]*`.
    verify.sh D3 re-runs the parity check."""
    if IS_WINDOWS and n.startswith("-"):
        return False
    if IS_WINDOWS and (n.upper() in {"CON", "PRN", "AUX", "NUL", "CLOCK$", "__PYCACHE__"}
                       or n.upper() in {f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)}):
        return False
    return bool(n) and all(ch in _NAME_OK for ch in n)


def fleet_email():
    """The account the FLEET (shared slot) is on right now, or None."""
    return authed_email(None)


def profile_for_email(email):
    """Which profile anchors this account, or None (an unanchored account)."""
    if not email:
        return None
    for p in profiles():
        if identity(os.path.join(BASE, p, ".claude.json")).get("emailAddress") == email:
            return p
    return None


def fleet_anchor():
    """The fleet account's anchor profile name, or None."""
    return profile_for_email(fleet_email())


def expected_email(name):
    """The address `claude-switch <name>` aims the browser at. Comes from the
    profile's own signed-in identity, which is why an unanchored profile cannot
    be a switch target — there is nothing to aim at or verify against."""
    return identity(os.path.join(BASE, name, ".claude.json")).get("emailAddress")


# ---------------------------------------------------------------------------
# The switch. The ONLY credential writer in this design is the official
# `claude auth login` flow, aimed at the shared slot by scrubbing the env.
# ---------------------------------------------------------------------------

def _login_supports_email():
    """Does this build accept `auth login --email`? PROBED, never assumed.
    Present in 2.1.236, but the binary auto-updates daily and passing an unknown
    flag would abort the login instantly — indistinguishable from the user
    closing the window (rc=2). Memoized: one ~1s local call against a ~15s
    browser round trip."""
    global _EMAIL_FLAG
    if _EMAIL_FLAG is None:
        try:
            r = subprocess.run([CLAUDE_BIN, "auth", "login", "--help"],
                               capture_output=True, text=True, timeout=25)
            _EMAIL_FLAG = "--email" in (r.stdout or "")
        except Exception:
            _EMAIL_FLAG = False
    return _EMAIL_FLAG


def login_env(email=None):
    """The environment for ONE aimed login.

    FLEET LANE ONLY. This pops CLAUDE_CONFIG_DIR, which is correct for a fleet
    switch and WRONG for an anchor login — routing claude-login through here
    would write the FLEET slot while its own check reported on the untouched
    anchor, switching the fleet silently and unverified. claude-as.sh sets its
    own env for that reason; do not "DRY" the two together.

    Pure enough to unit test: reads Local State, launches nothing.
    """
    env = dict(os.environ)
    fleet_env(env)     # invariant #4: aim at the FLEET slot (the shared default slot, or the named config dir in fleet-dir mode)
    prof = chrome_profile_for_email(email)
    if prof and os.access(OPENER, os.X_OK):
        env["BROWSER"] = OPENER
        # COCKPIT_ and not CLAUDE_: 2.1.236's PTY-host env builder deletes every
        # CLAUDE_* var but three whitelisted names. Not this code path today,
        # but we do not own that namespace and the binary updates daily.
        env["COCKPIT_CHROME_PROFILE_DIR"] = prof
    # NOT aiming deliberately leaves any inherited BROWSER alone: that is the
    # user's own configuration and this feature has no business overriding it.
    return env


def _aim_since(mark):
    """(status, profile) from the opener's receipt if it was written after
    `mark`, else None. The switch ANNOUNCES a profile before the opener runs, so
    without this a silently-failed aim would be reported as an achieved one
    forever — precisely what invariant #8 forbids."""
    try:
        parts = open(AIM_RECEIPT).read().split()
        if len(parts) >= 2 and float(parts[0]) >= mark - 1:
            return parts[1], " ".join(parts[2:])
    except Exception:
        pass
    return None


def _run_login(email=None):
    """Interactive on purpose: inherits the terminal so the login flow can print
    its URL and prompts; the browser opens; nothing is captured or parsed. The
    return code is treated as advisory — success is judged from STATE
    (fingerprint + identity), never from an exit code (gotcha #3's lesson).

    `email` aims the login twice: --email prefills login_hint on the page, and
    BROWSER routes the URL into that account's own Chrome profile. Both are
    AIMING ONLY."""
    argv = [CLAUDE_BIN, "auth", "login"]
    if email and _login_supports_email():
        argv += ["--email", email]
    try:
        return subprocess.run(argv, env=login_env(email)).returncode
    except KeyboardInterrupt:
        return 130


def fleet_switch(name, runner=None, quiet=False):
    """Aimed, verified fleet switch. Returns:
       0  fleet verified on the requested account
       1  refused before any login ran — nothing changed
       2  login ran but the credential did not change (closed / aborted)
       3  credential CHANGED but the browser granted a DIFFERENT account
       4  credential CHANGED but identity could not be read (slow probe)
    Codes 3 and 4 mean a login HAPPENED. Never present them as "nothing
    happened" — that exact framing let a watcher retry a half-done switch into
    a destroyed account on 2026-08-05."""
    say = (lambda *a: None) if quiet else print
    if IS_WINDOWS:
        return windows_browser_switch(name, runner, say)

    if name in FLEET_BLOCKED:
        say(f"{C['red']}{name} is blocked from the fleet lane: {FLEET_BLOCKED[name]}{C['off']}")
        say(f"  One-off session instead: claude-as {name}")
        return 1

    pdir = os.path.join(BASE, name)
    if not valid_name(name) or not os.path.isfile(os.path.join(pdir, ".claude.json")):
        say(f"{C['red']}no such profile: {name}{C['off']}")
        say(f"  create it: bash ~/.claude-profiles/new-profile.sh {name}")
        return 1

    expected = expected_email(name)
    if not expected:
        say(f"{C['red']}{name} has never signed in, so there is no address to aim the "
            f"browser at.{C['off']}  First: claude-login {name}")
        return 1

    # Anchor requirement — the safety condition of the whole design. Without an
    # anchor, this account's ONLY grant would live in the fleet slot, where the
    # very next switch overwrites it. Recovery is only a re-login, but it is the
    # one destroy-shaped state left, so it is refused rather than warned.
    if not has_claude_login(slot_for(pdir)):
        say(f"{C['red']}{name} has no anchor login of its own — refusing to put its only "
            f"credential in the fleet slot.{C['off']}  First: claude-login {name}  (~15s), then re-run.")
        return 1

    # BORROWED-FLEET GUARD (added 2026-10-01 with claude-swap.py, cockpit v2).
    # claude-swap can put an account on the fleet by MOVING its own private
    # login into the shared slot ("borrowed"), leaving its profile slot empty.
    # While that is true the shared slot holds that account's ONLY local login,
    # and this function's browser login would overwrite it — the destroy-shaped
    # state the v4 anchor rule exists to prevent. So refuse, and point at the
    # no-browser swap, which puts the borrowed login back home as it moves.
    # Read-only check: this file must stay free of keychain writes (verify.sh D3).
    # SYNC WARNING: "borrowed" = fleet account's profile slot is empty; the same
    # definition lives in claude-swap.py (swap() step 4 / status()).
    cur_email = fleet_email()
    cur_prof = profile_for_email(cur_email)
    if cur_prof and not has_claude_login(slot_for(os.path.join(BASE, cur_prof))):
        say(f"{C['red']}The fleet is running on {cur_prof}'s OWN login (moved there by claude-swap). "
            f"A browser switch would erase it.{C['off']}")
        say(f"  Use instead:  claude-swap {name}    (no browser; puts {cur_prof}'s login back home)")
        # Not "swap to another account first": with no spares, every swap leaves
        # the fleet borrowed again, so that advice loops. The way in for an
        # account whose own login is missing or near its renewal deadline is to
        # renew that login in its OWN slot (claude-login never touches the fleet).
        say(f"  If {name} cannot be swapped (its own login is missing or near expiry):")
        say(f"     claude-login {name}     (browser; renews {name}'s own login only)")
        say(f"     claude-swap {name}")
        return 1

    fp_before = slot_fingerprint(DEFAULT_SLOT)
    prev = fleet_email()          # may be None on a slow probe; tolerated

    say(f"\n{C['bold']}Fleet switch → {name}{C['off']}  ({expected})")
    say(f"{C['yel']}A browser window will open. Sign in as: {expected}{C['off']}")
    _aim_prof = chrome_profile_for_email(expected)
    if _aim_prof:
        _lbl = chrome_label(_aim_prof)
        say(f"{C['dim']}Aiming at Chrome profile: {_aim_prof}"
            + (f" ({_lbl})" if _lbl else "") + f".{C['off']}")
        # The Chrome profile is matched on its GOOGLE account; the claude.ai
        # session inside it is a third identity we cannot see. Aiming removes a
        # profile hop, NOT the need to read the consent screen.
        say(f"{C['yel']}Confirm the consent screen shows {expected} before clicking "
            f"Authorize.{C['off']}")
    else:
        say(f"{C['dim']}No Chrome profile matches {expected} — this opens in whichever "
            f"Chrome window was last used. Pick the right account.{C['off']}")
    say(f"{C['dim']}Everything follows this one login: shared sessions, new terminals, "
        f"and scheduled jobs.{C['off']}\n")

    _aim_mark = datetime.datetime.now().timestamp()
    # Zero-arg seam preserved so injected test runners keep working.
    (runner or (lambda: _run_login(expected)))()

    fp_after = slot_fingerprint(DEFAULT_SLOT)
    if fp_after == fp_before:
        say(f"{C['yel']}Nothing changed — the login window closed without completing. "
            f"The fleet is still on {prev or 'the previous account'}.{C['off']}")
        return 2

    _aim = _aim_since(_aim_mark)
    if _aim_prof and (_aim is None or _aim[0] != "aimed"):
        say(f"{C['yel']}note: the aim did not fire — that login opened in the default "
            f"browser, not {_aim_prof}. Aiming is cosmetic, so the switch itself is "
            f"unaffected; check {OPENER} exists and is executable.{C['off']}")

    got = fleet_email()
    try:
        json.dump({"previous_email": prev, "previous_profile": profile_for_email(prev),
                   "to_email": got, "requested": name,
                   "at": datetime.datetime.now().isoformat()},
                  open(UNDO, "w"), indent=2)
        os.chmod(UNDO, 0o600)
    except Exception:
        pass

    if got == expected:
        say(f"\n{C['grn']}Fleet is now {name}{C['off']}  ({got})")
        say(f"  {C['bold']}New terminals{C['off']}            {name}, immediately.")
        say(f"  {C['bold']}Sessions saying 'run /login'{C['off']}  adopt it instantly.")
        # Wording follows the measurement in gotcha #47 (Claude Code 2.1.287): about a
        # minute, not "at the next token refresh". The 8 h figure is the worst case.
        say(f"  {C['bold']}Healthy sessions{C['off']}          follow within about a minute (8 h at worst).")
        say(f"  {C['bold']}Limit-blocked sessions{C['off']}    follow on their next message.")
        say(f"  {C['bold']}Scheduled jobs{C['off']}            follow the fleet.")
        say(f"  {C['dim']}Panes opened with claude-as keep their own account (that is what "
            f"claude-as is for).{C['off']}")
        return 0

    if got:
        anchor = profile_for_email(got)
        say(f"\n{C['red']}⚠ The browser granted {got} — NOT {expected}.{C['off']}")
        say(f"  The whole fleet is now on {got}" + (f" (profile: {anchor})." if anchor else "."))
        if not anchor:
            say(f"  {C['yel']}That account has NO anchor profile on this machine, so its only "
                f"local grant lives in the fleet slot and the next switch overwrites it. "
                f"To anchor it: new-profile.sh <name>, then claude-login <name>.{C['off']}")
        say(f"  To land on {expected}: run  claude-switch {name}  again and pick that "
            f"account in the browser this time.")
        return 3

    say(f"\n{C['yel']}⚠ The fleet credential changed, but who it belongs to could not be "
        f"read (slow probe). Run  claude-switch  with no arguments in a moment to see "
        f"where the fleet landed.{C['off']}")
    return 4


def windows_browser_switch(name, runner, say):
    """Explicit fresh login into the default store; no claim about running sessions."""
    if (not valid_name(name) or name not in profiles() or name in FLEET_BLOCKED):
        say("No eligible profile. Create it with claude-new-profile, then claude-login.")
        return 1
    expected = expected_email(name)
    try:
        if not expected or not has_claude_login(slot_for(os.path.join(BASE, name))):
            say(f"First sign this profile in separately: claude-login {name}")
            return 1
        before = slot_fingerprint(FLEET_SLOT)
        previous = fleet_email()
        say(f"Default login → {name} ({expected}). Choose this account in your browser.")
        say("Running sessions are not switched or stopped; Windows convergence is unverified.")
        (runner or (lambda: _run_login(expected)))()
        after = slot_fingerprint(FLEET_SLOT)
    except CredentialReadError as error:
        say(str(error))
        return 4
    got = fleet_email()
    if after == before:
        say("No credential change detected. Login was not verified.")
        return 2
    if not got:
        say("Default login changed but identity could not be verified. Run claude-switch again.")
        return 4
    try:
        with open(UNDO, "w", encoding="utf-8") as f:
            json.dump({"previous_email": previous, "previous_profile": profile_for_email(previous),
                       "to_email": got, "requested": name}, f)
    except OSError:
        say("Could not save undo history.")
    if got != expected:
        say(f"Browser signed in as {got}, not {expected}. Default login has changed.")
        return 3
    say(f"Verified default login: {got}. Start a new session to use it.")
    return 0


def undo_fleet(runner=None, quiet=False):
    """Fleet-switch back to the previous account — which, in this design, is
    just another aimed browser login."""
    say = (lambda *a: None) if quiet else print
    try:
        d = json.load(open(UNDO))
    except Exception:
        say(f"{C['red']}no previous fleet account recorded{C['off']}")
        return 1
    prev_profile, prev_email = d.get("previous_profile"), d.get("previous_email")
    if prev_profile:
        return fleet_switch(prev_profile, runner=runner, quiet=quiet)
    if prev_email:
        say(f"The previous fleet account was {prev_email}, which has no profile here to "
            f"aim at. Run  claude-login --fleet  and pick it in the browser.")
        return 1
    say(f"{C['red']}no previous fleet account recorded{C['off']}")
    return 1


def _process_lanes():
    """(shared_count, pinned{dir:count}) across running claude processes.
    Display-only; any failure returns None and the caller omits the line."""
    if IS_WINDOWS:
        return None  # No reliable Windows environment inspection; never guess or kill sessions.
    script = r"""ps -eo pid,command | grep -E 'claude' | grep -v grep \
      | grep -vE 'claude-cockpit|claude-switch|claude-usage|autoswitch|Claude Helper|Claude\.app|claudefordesktop|ShipIt' \
      | awk '{print $1}' | while read p; do
          d=$(ps eww -o command= -p "$p" 2>/dev/null | tr ' ' '\n' | grep '^CLAUDE_CONFIG_DIR=' | head -1)
          echo "${d:-SHARED}"
        done"""
    try:
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return None
    shared, pinned = 0, {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line == "SHARED":
            shared += 1
        else:
            d = line.split("=", 1)[-1]
            pinned[d] = pinned.get(d, 0) + 1
    return shared, pinned


def show():
    fe = fleet_email()
    anchor = profile_for_email(fe)
    if IS_WINDOWS:
        print("Windows: default-login identity only; running-session adoption is unverified.")
    print("DEFAULT LOGIN" if IS_WINDOWS else
          f"\n{C['bold']}FLEET{C['off']} — the account all shared sessions, new terminals, and scheduled jobs are on")
    if fe:
        print(f"   {C['grn']}{fe}{C['off']}" + (f"  (profile: {anchor})" if anchor else
              f"  {C['yel']}(no anchor profile — next switch overwrites its only local grant){C['off']}"))
    else:
        print(f"   {C['red']}signed out or unreadable — fix: claude-switch <name>{C['off']}")
    lanes = _process_lanes()
    if lanes:
        shared, pinned = lanes
        extra = "".join(f" · {v} pinned to {os.path.basename(k)}" for k, v in sorted(pinned.items()))
        print(f"   {C['dim']}{shared} running sessions follow the fleet{extra}{C['off']}")
    print(f"\n{C['bold']}Accounts{C['off']}")
    for p in profiles():
        pdir = os.path.join(BASE, p)
        em = identity(os.path.join(pdir, ".claude.json")).get("emailAddress") or "?"
        mark = "▸" if p == anchor else " "
        state = "anchored" if slot_fingerprint(slot_for(pdir)) else \
                f"{C['red']}NO LOGIN — claude-login {p}{C['off']}"
        blocked = f"  {C['yel']}[fleet-blocked: {FLEET_BLOCKED[p]}]{C['off']}" if p in FLEET_BLOCKED else ""
        print(f" {mark} {p:<12} {em:<40} {state}{blocked}")
    if IS_WINDOWS:
        print("\nChange default login: claude-switch <name>. Pinned session: claude-as <name>.")
        return 0
    print(f"\n{C['dim']}Switch everything:  claude-switch <name>   (one browser login, ~15s)"
          f"{C['off']}")
    print(f"{C['dim']}One-off session:    claude-as <name>{C['off']}\n")
    return 0


def main():
    require_claude()
    a = [x for x in sys.argv[1:] if x]
    if not a:
        try:
            return show()
        except CredentialReadError as error:
            print(str(error), file=sys.stderr)
            return 4
    if a[0] in ("--undo", "-u"):
        return undo_fleet()
    if a[0] in ("--active", "--current"):
        n = fleet_anchor()
        print(n or "")
        return 0 if n else 1
    if a[0] == "--email-for":
        # Companion to --chrome-profile-for, so the shell never has to parse a
        # profile's .claude.json itself. Name arrives via argv, never string
        # interpolation.
        if len(a) < 2:
            return 1
        em = expected_email(a[1])
        if em:
            print(em)
            return 0
        return 1
    if a[0] == "--chrome-profile-for":
        # Single implementation of the aiming rule, called by claude-as.sh too:
        # two copies of one rule drifted on a Cyrillic homoglyph once (gotcha #26).
        if len(a) < 2:
            return 1
        d = chrome_profile_for_email(expected_email(a[1]))
        if d:
            print(d)
            return 0
        return 1
    if a[0].startswith("-"):
        print(__doc__.split("DESIGN HISTORY")[0].strip())
        return 1
    return fleet_switch(a[0], quiet="--quiet" in a)


if __name__ == "__main__":
    sys.exit(main())
