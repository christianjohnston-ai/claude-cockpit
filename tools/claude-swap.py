#!/usr/bin/env python3
"""
claude-swap — fleet switch WITHOUT a browser (Claude Cockpit v2, 2026-10-01).

  claude-swap                 who the fleet is on (server truth) + where every login is
  claude-swap <name>          put the fleet on <name>, no browser
  claude-swap --undo          swap back to the account the last swap moved away from
  claude-swap --check         integrity scan only: every login in exactly one slot

WHAT A SWAP IS
  The FLEET is the shared keychain slot "Claude Code-credentials": every
  terminal without CLAUDE_CONFIG_DIR, every running session, and any
  launchd agents read it. claude-switch (v4) puts an account there by
  MINTING a new login in the browser. This tool instead MOVES an existing login
  into it and moves the outgoing one out — no browser, ever.

  Measured 2026-10-01 (lab -> personal, 13:01:53): sessions blocked on a usage
  limit ran on the new account on their very next message (3.7-6.1 min later,
  when re-prompted), so changing the shared slot is enough — no restarts.

THE ONE RULE EVERYTHING HERE PROTECTS: a login lives in exactly ONE slot.
  A stored login carries a SINGLE-USE refresh token. Two slots holding the same
  one = two independent renewers with no shared lock (Claude's refresh lock is
  per config dir) = the first renewal kills the other holder. That is the
  2026-08-04 incident (7 live sessions dead in 3 min). So this tool only ever
  MOVES: write destination, read it back, then remove the source. A copy is
  never left behind, and --check proves it after every swap.

WHERE A LOGIN GOES
  Joining the fleet (source, in order of preference):
    1. the account's SPARE  — keychain service "claude-cockpit-spare-<name>", a slot
       no Claude process ever reads (so nothing renews it while it waits).
    2. its ANCHOR (private profile slot) — "borrowed". While borrowed, that
       profile is signed out: `claude-as <name>` and the cockpit's per-profile
       probe will not work for it until it leaves the fleet again.
  Leaving the fleet (destination):
    - back to its ANCHOR if the anchor is empty (it was borrowed), else
    - into its SPARE (the anchor already holds its own independent login).
  Spares therefore accumulate on their own: every account that leaves the fleet
  while its anchor is intact parks a spare, and the next swap to it uses that.

WHY EACH SAFETY CHECK EXISTS
  - IDENTITY IS ASKED OF THE SERVER (GET /api/oauth/profile with the token),
    never read from ~/.claude.json or a ledger. 2026-08-05: a cached identity
    disagreed with the keychain and the v2 switch wrote one account's login over
    another's — personal@example.com destroyed by one keypress (gotcha #17).
  - BOTH TOKENS MUST HAVE >= MIN_FRESH_MIN of access-token life left. A running
    process only spends a refresh token when its access token is about to
    expire. If both logins are fresh, nothing that still remembers a moved login
    (a fleet session's in-memory copy, an in-flight cockpit probe) will try to
    renew it for hours — by which time they have re-read the slot. This is what
    makes a move safe without taking Claude's refresh lock.
  - COMPARE-AND-SWAP: each slot is re-read right before it is written; if its
    fingerprint changed (a session renewed, someone typed /login), abort.
  - ONLY claudeAiOauth MOVES. A slot also holds "mcpOAuth" (MCP-server logins —
    many of them in the shared slot on 2026-10-01). Moving the whole blob, as the
    August move-design did, would carry those MCP logins to the wrong slot.
  - VERIFY AFTER: a real fleet /usage probe (content-checked — exit codes lie,
    gotcha #3), server identity of the new fleet token, and a duplicate scan.
    Any failure rolls the moves back.

WHAT STILL CAN GO WRONG (by design, bounded to one browser login)
  - A bare /login while an account is on duty with its BORROWED anchor erases
    that account's only local login -> `claude-login <name>` to restore it.
  - A spare left idle past its refreshTokenExpiresAt is dead; it is skipped and
    the tool falls back to the anchor (or tells you to use claude-switch).

SYNC WARNING: imports claude-switch.py for profiles(), slot_for(), valid_name(),
  identity() and CLAUDE_BIN. claude-switch.py must stay free of keychain writes
  (verify.sh D3 AST gate) — ALL credential moves live in THIS file on purpose,
  so that gate stays meaningful. verify.sh does not know about spare slots;
  `claude-swap --check` is the scan that covers them.
SYNC WARNING: the probe flags below match claude-cockpit.py / claude-usage.py /
  claude-ping.py (gotcha #34: every probe is a full session; hooks off,
  zero MCP servers).
"""
import json, os, subprocess, sys, hashlib, datetime, time, tempfile, shutil
import urllib.request, urllib.error, importlib.util
sys.dont_write_bytecode = True   # gotcha #13/#30: __pycache__ became a fake profile once

BASE = os.path.expanduser("~/.claude-profiles")
if sys.platform == "win32":
    BASE = os.path.normpath(BASE)
USER = os.environ.get("USER", "")
SHARED_SLOT = "Claude Code-credentials"
SPARE_PREFIX = "claude-cockpit-spare-"
LEDGER = os.path.join(BASE, ".swap-ledger.jsonl")
LAST = os.path.join(BASE, ".last-swap.json")
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
# The one opt-in for everything that rewrites a keychain login. It lives in the same
# file the watcher reads, so there is a single switch to find.
SWAP_CFG = os.path.join(BASE, "autoswitch.json")
# Access-token life both logins must have left before a move. Claude renews only
# near expiry; 30 min is far longer than the few minutes a running session takes
# to re-read the shared slot (measured 2026-10-01), so no holder of a moved login
# will want to renew it before it has noticed the move.
MIN_FRESH_MIN = 30
# A refresh token past refreshTokenExpiresAt cannot renew. Require a margin so a
# login is never moved into the fleet hours before it dies.
MIN_REFRESH_LIFE_H = 6
# Keys in ~/.claude.json that belong to the ACCOUNT, not the machine. Cleared on a
# swap so the new account never displays the old one's cached data (gotcha #5:
# usage once filed under the wrong accountUuid). Same list the August move-design
# used and proved.
ACCOUNT_SCOPED = [
    "cachedUsageUtilization", "modelAccessCache", "orgModelDefaultCache",
    "clientDataCache", "clientDataCacheSlots", "additionalModelOptionsCache",
    "additionalModelCostsCache", "autoCompactWindowsCache",
    "lastSeenOrgDefaultUpdatedAt", "cachedExtraUsageDisabledReason",
]

_spec = importlib.util.spec_from_file_location("cswitch", os.path.join(BASE, "claude-switch.py"))
cswitch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cswitch)
# The fleet slot is decided in ONE place (claude-switch.py _fleet_config_dir). On an
# ordinary machine it is the shared default slot, exactly as above; in fleet-dir mode it
# is the named config dir's own slot, so the default slot (owned by another Claude
# process there) is never named by any swap. SYNC WARNING: do not hard-code a slot name in this file.
SHARED_SLOT = cswitch.FLEET_SLOT

C = {"grn": "\033[32m", "red": "\033[31m", "yel": "\033[33m", "dim": "\033[2m",
     "bold": "\033[1m", "off": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def swap_enabled(path=None):
    """True only when autoswitch.json says "swap_enabled": true (JSON true, nothing else).

    WHY this exists: a swap MOVES login tokens between keychain items. It is tested and
    it rolls back on failure, but its first live run corrupted a login slot (the
    truncated-write incident described further down). On a fresh install it must be
    something the user turns on after reading docs/safety.md, never the default behind
    a keypress.
    FAILS CLOSED: a missing file, unreadable JSON, a missing key and a non-boolean
    value (the string "true", the number 1) all mean OFF.
    SYNC WARNING: three callers enforce this and must keep agreeing: main() below,
    claude-cockpit.py (number keys skip the swap and use the browser login) and
    claude-autoswitch.py cfg() (auto_swap is forced off)."""
    if cswitch.IS_WINDOWS:
        return False  # Native credential moves have not been validated. No opt-in bypass.
    try:
        return json.load(open(path or SWAP_CFG)).get("swap_enabled") is True
    except Exception:
        return False


def _require_enabled():
    """Exit 1 (refused, nothing changed) when the user has not opted in to swapping."""
    if cswitch.IS_WINDOWS:
        print("Native Windows credential swapping is unsupported; use claude-login or claude-as.", file=sys.stderr)
        sys.exit(1)
    if swap_enabled():
        return
    print("Swapping is turned off, so nothing was changed.\n"
          "  A swap moves login tokens between keychain items. Read docs/safety.md, then\n"
          "  turn it on by setting  \"swap_enabled\": true  in %s\n"
          "  Until then use:  claude-switch <name>   (the normal browser sign-in)" % SWAP_CFG,
          file=sys.stderr)
    sys.exit(1)


class SwapError(Exception):
    """Refused or aborted. `moved` says whether any slot was written.
    `no_source` = refused ONLY because the target has no login that can be moved;
    the one case where a browser login (claude-switch) is the right fallback.
    SYNC WARNING: exit codes in main() are read by claude-cockpit.py confirm():
    0 swapped, 1 refused (nothing changed), 2 no movable login (browser fallback
    allowed), 4 a write happened then was undone or failed, 5 damaged slot."""
    def __init__(self, msg, moved=False, no_source=False):
        super().__init__(msg)
        self.moved = moved
        self.no_source = no_source


# ---------------------------------------------------------------------------
# Keychain I/O. Nothing here prints or logs a token.
#
# INCIDENT 2026-10-01 20:10 — WHY write() has two paths:
#   The first version always wrote via `security -i` (command on STDIN, so the
#   secret never appears in argv). `security -i` reads each command into a ~4 KB
#   line buffer and SILENTLY TRUNCATES longer lines — it wrote the first 2013
#   bytes of the shared slot's JSON, then exited non-zero. The shared slot is
#   ~40 KB because it also carries mcpOAuth (many MCP-server logins). Result: the
#   fleet slot held unparseable JSON, the caller assumed "write failed = slot
#   untouched", and the MCP logins plus personal's fleet grant were lost. Unit
#   tests had passed because their blobs were tiny.
#   Claude Code itself (2.1.287, extracted from the binary) does exactly this:
#   `security -i` when the command fits the stdin limit, otherwise argv, logging
#   "Keychain payload exceeds security -i stdin limit; using argv". We mirror it.
#   The argv path exposes the hex payload to same-user `ps` for the ~50 ms the
#   `security` process lives — the same trade-off Claude Code makes.
# ---------------------------------------------------------------------------

SECURITY_STDIN_MAX = 3800   # conservative: truncation was observed at ~4026 chars of command


class CorruptSlot(Exception):
    pass


def _guard_slot(svc):
    """Fleet-dir mode only: the bare `Claude Code-credentials` slot belongs to another
    long-running Claude process on that machine. No swap, probe or rollback may read, write
    or delete it. Raises (never returns False) so no caller can treat a refusal as
    "write failed, restore the original" and then write it anyway — a restore into
    that process's slot is exactly the damage this guard exists to prevent."""
    if cswitch.FLEET_DIR and svc == cswitch.SLOT_PREFIX:
        raise SwapError(f"refused: {svc!r} belongs to another Claude process on this "
                        f"machine; the fleet here is {cswitch.FLEET_SLOT!r}")


class Keychain:
    def read(self, svc):
        if cswitch.IS_WINDOWS:
            try:
                return cswitch.read_windows_credential(svc)
            except cswitch.CredentialReadError:
                raise CorruptSlot("Windows credential file is unreadable") from None
        _guard_slot(svc)
        r = subprocess.run(["security", "find-generic-password", "-a", USER, "-s", svc, "-w"],
                           capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        try:
            return json.loads(r.stdout.strip())
        except ValueError:
            # Never let a damaged slot look like an empty one — an empty slot is a
            # valid move destination, a damaged one must stop everything.
            raise CorruptSlot(svc)

    def write(self, svc, blob):
        if cswitch.IS_WINDOWS:
            raise SwapError("Native Windows credential writes are unsupported; nothing changed.")
        _guard_slot(svc)
        hexed = json.dumps(blob, separators=(",", ":")).encode().hex()
        cmd = f'add-generic-password -U -a "{USER}" -s "{svc}" -X "{hexed}"'
        if len(cmd) <= SECURITY_STDIN_MAX:
            r = subprocess.run(["security", "-i"], text=True, capture_output=True, input=cmd + "\n")
        else:
            r = subprocess.run(["security", "add-generic-password", "-U", "-a", USER, "-s", svc,
                                "-X", hexed], text=True, capture_output=True)
        return r.returncode == 0

    def delete(self, svc):
        if cswitch.IS_WINDOWS:
            raise SwapError("Native Windows credential writes are unsupported; nothing changed.")
        _guard_slot(svc)
        r = subprocess.run(["security", "delete-generic-password", "-a", USER, "-s", svc],
                           capture_output=True, text=True)
        return r.returncode == 0


def fp(oauth):
    """Fingerprint of a login = sha256 prefix of its REFRESH token. The refresh
    token is what must never exist twice, so it is what we compare."""
    if not oauth or not oauth.get("refreshToken"):
        return None
    return hashlib.sha256(oauth["refreshToken"].encode()).hexdigest()[:12]


def minutes_left(oauth):
    exp = (oauth or {}).get("expiresAt")
    return None if not exp else (exp / 1000 - time.time()) / 60


def refresh_hours_left(oauth):
    exp = (oauth or {}).get("refreshTokenExpiresAt")
    return None if not exp else (exp / 1000 - time.time()) / 3600


# ---------------------------------------------------------------------------
# Server truth + probes (injectable so the test suite never touches real logins)
# ---------------------------------------------------------------------------

_WHO = {}   # sha256(access token)[:16] -> (email, uuid); a token never changes owner


def server_whoami(oauth):
    """(email, uuid) the SERVER says this access token belongs to, or None.
    Read-only GET; the same endpoint Claude Code itself calls after login.
    Cached per TOKEN in-process: a token's owner can never change, so the cache is
    exactly as truthful as the call, and a long-running watcher stops asking the
    server the same question every 45 s (see USAGE RATE LIMIT below)."""
    tok = (oauth or {}).get("accessToken")
    if not tok or (minutes_left(oauth) or 0) <= 1:
        return None
    h = hashlib.sha256(tok.encode()).hexdigest()[:16]
    if h in _WHO:
        return _WHO[h]
    r = _whoami_call(tok)
    if r:
        _WHO[h] = r
    return r


def _whoami_call(tok):
    req = urllib.request.Request(PROFILE_URL, headers={
        "Authorization": f"Bearer {tok}", "Content-Type": "application/json",
        "User-Agent": "claude-swap (claude-cockpit)"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=15))
        a = d.get("account") or {}
        return (a.get("email"), a.get("uuid"))
    except Exception:
        return None


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


# USAGE RATE LIMIT (measured 2026-10-01 22:20): /api/oauth/usage answers
# HTTP 429 "Rate limited" (retry-after: 0) when one account is asked too often.
# It tripped on the FLEET account once the watcher polled it every 45 s on top of
# 5-minute snapshots, test calls, and the running sessions' own usage polls. Inference
# was unaffected (normal replies, no errors in that window); the cockpit's
# borrowed row read "SIGNED OUT" because its usage read failed.
# So every caller goes through ONE on-disk cache per account (.usage-cache.json):
# a reading younger than the caller's max_age is reused; a 429 sets a >= 3-minute
# backoff for that account, during which the last good reading is returned (callers
# see its age via usage_age()). The watcher, snapshots, cockpit and swap all share it.
USAGE_CACHE = os.path.join(BASE, ".usage-cache.json")
BACKOFF_S = 180


def _ucache_load():
    try:
        return json.load(open(USAGE_CACHE))
    except Exception:
        return {}


def _ucache_save(c):
    try:
        tmp = USAGE_CACHE + f".{os.getpid()}.tmp"
        json.dump(c, open(tmp, "w"), separators=(",", ":"))
        os.replace(tmp, USAGE_CACHE)
    except Exception:
        pass


def usage_age(key):
    """Seconds since `key`'s cached reading was fetched, or None."""
    e = _ucache_load().get(key or "")
    return None if not e or not e.get("at") else time.time() - e["at"]


def live_usage_raw(oauth, key=None, max_age=0):
    """The server's FULL usage payload for this token, or None. Same shape as
    .claude.json's cachedUsageUtilization["utilization"] (verified key-for-key
    2026-10-01), so claude-cockpit.py parse_usage() can render it unchanged.
    `key` (the account's profile name) routes the read through the shared cache;
    without a key the server is called directly (tests, one-off use).
    SYNC WARNING: claude-cockpit.py refresh_borrowed(), claude-meters.py snapshot(),
    claude-autoswitch.py fleet_now() and Swapper.account_usage() all pass a key."""
    now = time.time()
    cache = _ucache_load() if key else {}
    ent = cache.get(key) if key else None
    if ent and ent.get("data") is not None and now - ent.get("at", 0) < max_age:
        return ent["data"]
    if ent and ent.get("backoff_until", 0) > now:
        return ent.get("data")
    tok = (oauth or {}).get("accessToken")
    if not tok or (minutes_left(oauth) or 0) <= 1:
        return ent.get("data") if ent else None
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json", "User-Agent": "claude-swap (claude-cockpit)"})
    try:
        data = json.load(urllib.request.urlopen(req, timeout=15))
    except urllib.error.HTTPError as e:
        if e.code == 429 and key:
            try:
                ra = float(e.headers.get("retry-after") or 0)
            except ValueError:
                ra = 0
            cache = _ucache_load()
            ent = cache.get(key) or {}
            ent["backoff_until"] = now + max(ra, BACKOFF_S)
            cache[key] = ent
            _ucache_save(cache)
            return ent.get("data")
        return ent.get("data") if ent else None
    except Exception:
        return ent.get("data") if ent else None
    if key:
        cache = _ucache_load()
        cache[key] = {"at": now, "data": data}
        _ucache_save(cache)
    return data


def session_cached_usage(uuid):
    """(payload, age_s) from ~/.claude.json — the reading the fleet's own sessions
    keep (written at most every 5 min, gotcha #8) — only if it is filed under this
    account (gotcha #5: a cache under the wrong accountUuid must never be used)."""
    try:
        cu = json.load(open(cswitch.CFG)).get("cachedUsageUtilization") or {}
    except Exception:
        return None, None
    if not cu.get("utilization") or cu.get("accountUuid") != uuid:
        return None, None
    return cu["utilization"], time.time() - (cu.get("fetchedAtMs") or 0) / 1000


def live_usage(oauth, key=None, max_age=0):
    """{"five": %, "seven": %, "five_resets": iso} straight from the server, or None.
    Read-only GET — the same endpoint `/usage` calls, without `/usage`'s
    minutes-long scan of local transcripts (gotcha #45) and without the 5-minute
    write throttle on the cached copy in .claude.json (gotcha #8). At the fleet's
    measured burn (~1-1.5% of a 5h window per minute, 2026-10-01) a 5-minute-old
    cached number can be 7% low, which is the whole margin of a 97% trigger."""
    return summarize_usage(live_usage_raw(oauth, key, max_age))


def summarize_usage(d):
    """{"five", "seven", "five_resets"} from a full usage payload, or None."""
    if not d:
        return None
    try:
        f, s = d.get("five_hour") or {}, d.get("seven_day") or {}
        return {"five": f.get("utilization") or 0.0, "seven": s.get("utilization") or 0.0,
                "five_resets": f.get("resets_at")}
    except Exception:
        return None


def probe(config_dir=None):
    """Prove the REAL Claude binary can authenticate with a slot (renewing it if
    the access token expired) by sending one tiny real message.
    config_dir=None = the FLEET: env scrubbed, so a claude-as-pinned shell cannot
    redirect it (gotcha #29). Success is judged on CONTENT, never exit code.

    WHY NOT `/usage` (the cockpit's probe): on claude 2.1.287 `/usage` also builds
    a "what's contributing to your usage" report by reading EVERY local transcript
    (on a large history: over a hundred thousand requests). Measured 2026-10-01: >7 min per probe,
    so the first live swap timed out its verification and rolled back a good move.
    A one-word message with the default system prompt, settings, hooks and MCP all
    stripped (gotcha #36 flags; cwd=$HOME so no repo CLAUDE.md loads) took 18 s.
    It costs a few tokens on the account being verified. No --max-budget-usd:
    on an inference call it refuses at unpredictable, account-dependent
    thresholds with exit 0 (gotcha #35)."""
    env = dict(os.environ)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        cswitch.fleet_env(env)
    for attempt in range(2):
        try:
            r = subprocess.run([cswitch.CLAUDE_BIN, "-p", "Reply with only the word: ok",
                                "--system-prompt", "Reply with one word.",
                                "--setting-sources", "",
                                "--settings", '{"disableAllHooks":true}',
                                "--strict-mcp-config", "--no-session-persistence"],
                               env=env, capture_output=True, timeout=75, cwd=os.path.expanduser("~"),
                               stdin=subprocess.DEVNULL, text=True)
            out = (r.stdout or "").strip().lower()
            if r.returncode == 0 and out.startswith("ok") and len(out) < 40:
                return True
        except Exception:
            pass
        time.sleep(4)
    return False


# ---------------------------------------------------------------------------
# The swapper. Every external effect is a constructor argument so the test suite
# (tests/test_claude_swap.py) can run the real logic against
# throwaway keychain items and a temp config file.
# ---------------------------------------------------------------------------

class Swapper:
    def __init__(self, kc=None, shared_slot=SHARED_SLOT, anchor_slot=None, spare_slot=None,
                 profiles=None, profile_identity=None, whoami=server_whoami, probe=probe,
                 config_path=cswitch.CFG, ledger=LEDGER, last=LAST,
                 out=print):
        self.kc = kc or Keychain()
        self.shared = shared_slot
        self.anchor_slot = anchor_slot or (lambda n: cswitch.slot_for(os.path.join(BASE, n)))
        self.spare_slot = spare_slot or (lambda n: SPARE_PREFIX + n)
        self.profiles = profiles or cswitch.profiles
        self.profile_identity = profile_identity or (
            lambda n: cswitch.identity(os.path.join(BASE, n, ".claude.json")))
        self.whoami = whoami
        self.probe = probe
        self.cfg = config_path
        self.ledger = ledger
        self.last = last
        self.out = out

    # --- small helpers -----------------------------------------------------
    def _oauth(self, svc):
        blob = self.kc.read(svc)
        return (blob or {}).get("claudeAiOauth"), blob

    def _name_for(self, email):
        for n in self.profiles():
            if self.profile_identity(n).get("emailAddress") == email:
                return n
        return None

    def _write_exact(self, svc, blob, original):
        """Write `blob` (None = delete the item) and prove the slot now holds EXACTLY
        it — whole-blob equality, not a fingerprint, so a truncated or partial write
        can never pass (the 2026-10-01 truncation kept no claudeAiOauth at all, but a
        cut inside mcpOAuth would have kept a matching fingerprint).
        On ANY failure the slot is put back to `original` and re-proven. A failed
        write is never assumed to have left the slot untouched — that assumption is
        exactly what turned the 20:10 truncation into data loss."""
        ok = self.kc.delete(svc) if blob is None else self.kc.write(svc, blob)
        try:
            back = self.kc.read(svc)
        except CorruptSlot:
            back, ok = "<corrupt>", False
        if ok and back == blob:
            return True
        # Restore.
        if original is None:
            self.kc.delete(svc)
        else:
            self.kc.write(svc, original)
        try:
            restored = self.kc.read(svc)
        except CorruptSlot:
            restored = "<corrupt>"
        if restored != original:
            raise SwapError(f"SLOT DAMAGED: a write to '{svc}' failed and the original could not be "
                            "restored. Do NOT /login. Run: claude-swap   (status) and get help.", moved=True)
        return False

    def _put(self, svc, oauth, meta=None):
        """Set claudeAiOauth in `svc`, PRESERVING every other key (mcpOAuth).
        False = the write failed AND the slot is proven back to what it was."""
        original = self.kc.read(svc)
        blob = dict(original or {})
        blob["claudeAiOauth"] = oauth
        if meta is not None:
            blob["_cockpit"] = meta
        return self._write_exact(svc, blob, original)

    def _take(self, svc, expect_fp, is_spare):
        """Remove a login from `svc` after verifying it is the one we moved.
        Spares are ours alone -> delete the item. Anchors may hold mcpOAuth for
        that profile -> remove only claudeAiOauth and keep the rest."""
        original = self.kc.read(svc)
        if fp((original or {}).get("claudeAiOauth")) != expect_fp:
            return False
        rest = {k: v for k, v in original.items() if k not in ("claudeAiOauth", "_cockpit")}
        return self._write_exact(svc, None if (is_spare or not rest) else rest, original)

    # --- what can move, and how much quota an account has left -----------------
    def movable_source(self, name):
        """(slot, kind, oauth) of the login a swap to `name` would move in, or
        (None, None, None). Spare first (it costs the account nothing — its private
        login stays put), else the private login (borrowed). Either must have
        >= MIN_REFRESH_LIFE_H of refresh life, so a swap never installs a login
        that dies within hours.
        SYNC WARNING: claude-autoswitch.py uses this to decide which accounts it
        may auto-swap to — the watcher and a hand swap must agree on "swappable"."""
        spare_o, _ = self._oauth(self.spare_slot(name))
        if spare_o and (refresh_hours_left(spare_o) or 0) >= MIN_REFRESH_LIFE_H:
            return self.spare_slot(name), "spare", spare_o
        anc, _ = self._oauth(self.anchor_slot(name))
        if anc and (refresh_hours_left(anc) or 0) >= MIN_REFRESH_LIFE_H:
            return self.anchor_slot(name), "anchor", anc
        return None, None, None

    def account_usage(self, name):
        """Live {five, seven} for a NON-fleet account, from the server, or None.
        Usage belongs to the ACCOUNT (gotcha #9), so any token of it will do: the
        private login if present (renewed first by a one-word probe if its access
        token has expired — the cockpit's /usage probes no longer keep it fresh,
        gotcha #45), else its spare if that token is still fresh."""
        anc, _ = self._oauth(self.anchor_slot(name))
        if anc:
            if (minutes_left(anc) or 0) < 2:
                self.probe(os.path.join(BASE, name))
                anc, _ = self._oauth(self.anchor_slot(name))
            return live_usage(anc, name, 120)
        spare_o, _ = self._oauth(self.spare_slot(name))
        return live_usage(spare_o, name, 120) if spare_o else None

    # --- inventory -----------------------------------------------------------
    def inventory(self):
        """Every slot this system knows, with fingerprints. Used by status and by
        the duplicate scan."""
        rows = []
        o, _ = self._oauth(self.shared)
        rows.append(("FLEET", self.shared, o))
        for n in self.profiles():
            a, _ = self._oauth(self.anchor_slot(n))
            rows.append((f"{n} private", self.anchor_slot(n), a))
            s, _ = self._oauth(self.spare_slot(n))
            if s:
                rows.append((f"{n} spare", self.spare_slot(n), s))
        return rows

    def duplicates(self):
        seen = {}
        for label, _, o in self.inventory():
            f = fp(o)
            if f:
                seen.setdefault(f, []).append(label)
        return {k: v for k, v in seen.items() if len(v) > 1}

    # --- config identity -------------------------------------------------------
    def patch_config(self, ident):
        """oauthAccount in ~/.claude.json is what Claude DISPLAYS (the keychain holds
        only tokens — gotcha #5). Optimistic concurrency: every running session writes this
        file; if its mtime moves between our read and our replace, re-read and
        retry rather than clobber their write."""
        for _ in range(8):
            before = os.stat(self.cfg).st_mtime_ns
            with open(self.cfg) as f:
                d = json.load(f)
            d["oauthAccount"] = ident
            for k in ACCOUNT_SCOPED:
                d.pop(k, None)
            tmp = tempfile.NamedTemporaryFile("w", dir=os.path.dirname(self.cfg),
                                              prefix=".claude.json.swap.", delete=False)
            try:
                json.dump(d, tmp, indent=2)
                tmp.flush(); os.fsync(tmp.fileno()); tmp.close()
                if os.stat(self.cfg).st_mtime_ns != before:
                    os.unlink(tmp.name)
                    time.sleep(0.3)
                    continue
                shutil.copymode(self.cfg, tmp.name)
                os.replace(tmp.name, self.cfg)
                return True
            except Exception:
                if os.path.exists(tmp.name):
                    os.unlink(tmp.name)
                raise
        return False

    def config_email(self):
        try:
            return (json.load(open(self.cfg)).get("oauthAccount") or {}).get("emailAddress")
        except Exception:
            return None

    # --- who is on the fleet -----------------------------------------------------
    def fleet_truth(self):
        """(name, email, oauth) of the fleet login, identity from the SERVER. If the
        access token is too close to expiry to ask, run one fleet probe so Claude
        renews it, then re-read."""
        o, _ = self._oauth(self.shared)
        if not o:
            raise SwapError("The fleet slot holds no login. Use: claude-switch <name>")
        if (minutes_left(o) or 0) < MIN_FRESH_MIN:
            self.out(f"{C['dim']}fleet login renews soon - letting Claude renew it first...{C['off']}")
            self.probe(None)
            o, _ = self._oauth(self.shared)
        who = self.whoami(o)
        if not who:
            raise SwapError("The server would not say who the fleet login belongs to "
                            "(offline, or the login is dead). Nothing changed.")
        name = self._name_for(who[0])
        if not name:
            raise SwapError(f"The fleet is on {who[0]}, which matches no profile. "
                            "Refusing to move a login I cannot put back. Nothing changed.")
        return name, who[0], o

    # --- the swap ----------------------------------------------------------------
    def swap(self, target, dry_run=False):
        if cswitch.IS_WINDOWS:
            raise SwapError("Native Windows swapping (including dry-run) is unsupported; use claude-as.")
        if not cswitch.valid_name(target) or target not in self.profiles():
            raise SwapError(f"No such profile: {target}")
        target_ident = self.profile_identity(target)
        target_email = target_ident.get("emailAddress")
        if not target_email:
            raise SwapError(f"{target}'s profile records no account. Run: claude-login {target}")

        # 1. Who is on the fleet now — asked of the server.
        cur_name, cur_email, g_out = self.fleet_truth()
        if cur_name == target:
            self.out(f"{C['grn']}The fleet is already on {target} ({target_email}). Nothing to do.{C['off']}")
            return {"noop": True}

        # 2. Where the incoming login comes from: spare first, then borrow the anchor.
        spare_o, _ = self._oauth(self.spare_slot(target))
        if spare_o and (refresh_hours_left(spare_o) or 0) < MIN_REFRESH_LIFE_H:
            self.out(f"{C['yel']}{target}'s spare can no longer renew (expired); skipping it.{C['off']}")
        src, src_kind, g_in = self.movable_source(target)
        if not src:
            raise SwapError(f"{target} has no login I can move (no spare, and its private login is "
                            f"missing or near its renewal deadline). Use: claude-switch {target}  (browser)",
                            no_source=True)

        # 3. The incoming login must be fresh and must really be the target account.
        #    A borrowed anchor is renewed by a profile probe if it is close to expiry;
        #    a spare cannot be (nothing reads its slot), so a stale spare is verified
        #    after the move instead, and rolled back if it fails.
        if src_kind == "anchor" and (minutes_left(g_in) or 0) < MIN_FRESH_MIN:
            self.out(f"{C['dim']}{target}'s private login renews soon - renewing it first...{C['off']}")
            self.probe(os.path.join(BASE, target))
            g_in, _ = self._oauth(src)
        if (minutes_left(g_in) or 0) >= MIN_FRESH_MIN:
            who = self.whoami(g_in)
            if not who or who[0] != target_email:
                raise SwapError(f"The login in {src_kind} slot for {target} belongs to "
                                f"{who[0] if who else 'nobody the server recognises'}, not {target_email}. "
                                "Refusing. Nothing changed.")
        elif src_kind == "spare":
            pass   # expired access token: identity is proven by the post-move probe + whoami, or rolled back
        else:
            raise SwapError(f"{target}'s private login would not renew. Run: claude-login {target}")

        # 4. Where the outgoing login goes back to.
        cur_anchor, _ = self._oauth(self.anchor_slot(cur_name))
        if cur_anchor is None:
            dest, dest_kind = self.anchor_slot(cur_name), "anchor"
        else:
            dest, dest_kind = self.spare_slot(cur_name), "spare"
        old_spare, _ = self._oauth(self.spare_slot(cur_name)) if dest_kind == "spare" else (None, None)

        f_out, f_in = fp(g_out), fp(g_in)
        plan = (f"fleet {cur_name} -> {target} | {cur_name}'s login -> its {dest_kind} | "
                f"{target}'s login <- its {src_kind}")
        if dry_run:
            self.out(plan)
            return {"dry_run": True, "plan": plan}
        self.out(f"{C['dim']}{plan}{C['off']}")

        # 5. Compare-and-swap moves. Order: park the outgoing login, install the
        #    incoming one, then remove it from its source. Each login is in two slots
        #    only for the milliseconds between a write and the matching removal, and
        #    step 3 guaranteed nobody holding it will renew in that window.
        now_o, _ = self._oauth(self.shared)
        if fp(now_o) != f_out:
            raise SwapError("The fleet login changed while I was checking (a session renewed it, "
                            "or someone logged in). Nothing changed - run it again.")
        now_i, _ = self._oauth(src)
        if fp(now_i) != f_in:
            raise SwapError(f"{target}'s login changed while I was checking. Nothing changed - run it again.")

        meta_out = {"email": cur_email, "parked_at": datetime.datetime.now().isoformat(timespec="seconds")}
        if not self._put(dest, g_out, meta_out if dest_kind == "spare" else None):
            raise SwapError(f"Could not write {cur_name}'s login to its {dest_kind}. Fleet untouched.", moved=True)
        if not self._put(self.shared, g_in):
            # _put proved the fleet slot is back to its original contents; now undo
            # the park so the outgoing login is not held twice.
            self._take(dest, f_out, dest_kind == "spare")
            raise SwapError("Could not write the fleet slot; it was verified restored and the move "
                            "undone. Fleet unchanged.", moved=True)
        if not self._take(src, f_in, src_kind == "spare"):
            # The incoming login is in the fleet AND still in its source. Put the
            # outgoing login back in the fleet and un-park it, so every login is
            # back in exactly the slot it started in.
            self._put(self.shared, g_out)
            self._take(dest, f_out, dest_kind == "spare")
            raise SwapError(f"Could not remove {target}'s login from its {src_kind}; the swap was "
                            "undone. Run: claude-swap --check", moved=True)
        # The outgoing login is now held by `dest` alone; the shared slot was overwritten.
        if not self.patch_config(target_ident):
            self.out(f"{C['yel']}~/.claude.json was too busy to update; Claude will show the old account "
                     f"name until it refreshes. The login itself moved correctly.{C['off']}")

        # 6. Verify with a REAL call through the fleet, then server identity.
        ok = self.probe(None)
        now_fleet, _ = self._oauth(self.shared)
        who = self.whoami(now_fleet) if ok else None
        if not ok or not who or who[0] != target_email:
            self.out(f"{C['red']}Verification failed ({'probe failed' if not ok else 'wrong identity'}). "
                     f"Rolling back...{C['off']}")
            self._rollback(src, src_kind, dest, dest_kind, cur_name, target)
            raise SwapError(f"Swap to {target} failed verification and was rolled back to {cur_name}. "
                            f"Use: claude-switch {target}  (browser)", moved=True)

        dups = self.duplicates()
        rec = {"at": datetime.datetime.now().isoformat(timespec="seconds"),
               "from": cur_name, "to": target, "src": src_kind, "dest": dest_kind,
               "fp_out": f_out, "fp_in": f_in, "fp_fleet_now": fp(now_fleet),
               "discarded_old_spare": fp(old_spare), "duplicates": dups}
        try:
            with open(self.ledger, "a") as f:
                f.write(json.dumps(rec) + "\n")
            json.dump({"previous": cur_name, "current": target, "at": rec["at"]}, open(self.last, "w"))
        except Exception:
            pass
        if dups:
            self.out(f"{C['red']}DUPLICATE LOGIN DETECTED: {dups}. Do NOT /login; run claude-swap --check.{C['off']}")
        self.out(f"{C['grn']}{C['bold']}Fleet is on {target} ({target_email}) - verified by the server.{C['off']}")
        if src_kind == "anchor":
            self.out(f"{C['yel']}{target} is on duty with its OWN login, so while it is on the fleet: "
                     f"`claude-as {target}` won't work, and a bare /login would erase it. "
                     f"Swapping away puts it back.{C['off']}")
        return rec

    def _rollback(self, src, src_kind, dest, dest_kind, cur_name, target):
        """Reverse a swap whose verification failed. The fleet probe may have renewed
        the incoming login, so move back whatever the fleet slot holds NOW."""
        in_now, _ = self._oauth(self.shared)
        out_back, _ = self._oauth(dest)
        if in_now:
            meta = {"email": self.profile_identity(target).get("emailAddress"), "note": "rolled back",
                    "parked_at": datetime.datetime.now().isoformat(timespec="seconds")}
            self._put(src, in_now, meta if src_kind == "spare" else None)
        if out_back:
            self._put(self.shared, out_back)
            self._take(dest, fp(out_back), dest_kind == "spare")
        self.patch_config(self.profile_identity(cur_name))

    # --- status --------------------------------------------------------------------
    def status(self):
        name = None
        try:
            name, email, _ = self.fleet_truth()
            self.out(f"{C['bold']}FLEET: {name} ({email}){C['off']}  {C['dim']}identity from the server{C['off']}")
        except SwapError as e:
            self.out(f"{C['red']}FLEET: {e}{C['off']}")
        cfg = self.config_email()
        self.out(f"{C['dim']}~/.claude.json displays: {cfg}{C['off']}\n")
        self.out(f"{'ACCOUNT':10} {'PRIVATE LOGIN':16} {'SPARE':22} SWAPPABLE")
        for n in self.profiles():
            a, _ = self._oauth(self.anchor_slot(n))
            s, _ = self._oauth(self.spare_slot(n))
            a_txt = "present" if a else "BORROWED/empty"
            s_life = refresh_hours_left(s)
            s_txt = f"yes ({s_life/24:.0f}d left)" if s and s_life and s_life > 0 else ("expired" if s else "-")
            can = (s and (s_life or 0) >= MIN_REFRESH_LIFE_H) or (a and (refresh_hours_left(a) or 0) >= MIN_REFRESH_LIFE_H)
            verdict = "ON THE FLEET" if n == name else ("yes" if can else "no - claude-switch " + n)
            self.out(f"{n:10} {a_txt:16} {s_txt:22} {verdict}")
        dups = self.duplicates()
        self.out(f"\nlogins held twice: {C['red'] + str(dups) + C['off'] if dups else C['grn'] + 'none' + C['off']}")


def main():
    args = sys.argv[1:]
    s = Swapper()
    if cswitch.IS_WINDOWS and not args:
        print("Native Windows hot-swap is unsupported. Default-login status follows.")
        try:
            return cswitch.show()
        except cswitch.CredentialReadError as error:
            print(str(error), file=sys.stderr)
            sys.exit(5)
    try:
        if not args:
            s.status()
            if not swap_enabled():
                print("\nSwapping is OFF on this machine (swap_enabled is false in autoswitch.json). "
                      "The status above is read-only.")
        elif args[0] == "--check":
            dups = s.duplicates()
            print("logins held twice:", dups or "none")
            sys.exit(3 if dups else 0)
        elif args[0] == "--undo":
            _require_enabled()
            if not os.path.exists(LAST):
                # Was an uncaught FileNotFoundError traceback on any machine that had
                # never swapped, which is every fresh install.
                print("Nothing to undo: no swap has been recorded on this machine.", file=sys.stderr)
                sys.exit(1)
            last = json.load(open(LAST))
            s.swap(last["previous"])
        elif args[0] == "--dry-run" and len(args) == 2:
            s.swap(args[1], dry_run=True)
        else:
            _require_enabled()
            s.swap(args[0])
    except SwapError as e:
        print(f"{C['red']}{e}{C['off']}", file=sys.stderr)
        sys.exit(4 if e.moved else (2 if e.no_source else 1))
    except CorruptSlot as e:
        print(f"{C['red']}SLOT DAMAGED: keychain item '{e}' holds unreadable data. Nothing was moved. "
              f"Do NOT /login until it is repaired.{C['off']}", file=sys.stderr)
        sys.exit(5)


if __name__ == "__main__":
    main()
