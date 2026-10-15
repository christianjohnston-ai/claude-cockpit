#!/usr/bin/env python3
"""
claude-meters — usage history + run-out forecast for every account
(cockpit v2, 2026-10-01; the original author: "if I keep my 7-day / 48-hour / 24-hour pace,
will I run out before the accounts refill?").

  claude-meters snapshot     record every account's live meters now (also run by
                             the launchd watcher every 5 min)
  claude-meters tokens       update the hourly token-cost history from local logs
  claude-meters forecast     compute + print the forecast (also writes forecast.json
                             for the cockpit panel)

THE UNIT: "points" = 1% of ONE account's WEEKLY limit. The forecast assumes every
account is on the same plan, so a point is the same size everywhere; N accounts give
N x 100 points a week, and the pace that never runs dry on average is N x 100 / 168
points per hour.

THE PACE (points/hour over 7d / 48h / 24h) comes from two sources:
  1. METERS (truth): meters.jsonl, every account's live weekly % every 5 min. Pace =
     sum of weekly-% increases across ALL accounts (a drop = a weekly refill). Counts
     usage from anywhere — claude.ai, other devices, claude-as sessions — because the
     server's meter does. Used for a window once the log covers >= 90% of it.
  2. TOKENS (estimate, labelled "est."): the API-price-weighted cost of every request in
     the local session logs, bucketed by hour (token_hours.json), converted to points
     with a ratio fitted on the hours where the meter log exists. Fills the 7d/48h/24h
     windows before the log is long enough. Only sees THIS Mac.

THE FORECAST is a simulation, not a division. Each account's weekly room is
use-it-or-lose-it: it refills to 100 at that account's own fixed time and anything
left is lost. Each account can also only be drained so fast: one full 5-hour window
costs K weekly points (K measured from the log; seed 0.30 from 2026-10-01: team
0.33, ops 0.26, lab 0.26). The simulation steps forward 3 minutes at a
time for 7 days, spending each step's demand from the account whose weekly refill
comes SOONEST (earliest-deadline-first is the order that wastes the least), within
each 5-hour cap, refilling at the exact reset times the server reports. Output per
pace: when you first run dry, hours blocked in the next 7 days, points that expire
unused, plus the highest pace that never runs dry.

ACCURACY: every forecast logs what each pace predicts for the next 24 h; a day later
the meter log says what happened, and the miss is reported.

SAFETY: read-only against logins. It reads tokens from keychain slots to call two
read-only server endpoints; it never writes a slot, never renews a token, and never
sends a message (so it never opens an account's 5-hour window). An account whose
token has expired is skipped until something uses it — its meter isn't moving.

SYNC WARNING: imports claude-swap.py (Swapper slots, live_usage_raw, server_whoami)
and claude-switch.py (profile_for_email). claude-autoswitch.py calls tick() every
loop; claude-cockpit.py reads forecast.json. Must stay Python 3.9-compatible: the
watcher runs under /usr/bin/python3 (3.9.6).
"""
import json, os, sys, time, datetime, importlib.util, glob
sys.dont_write_bytecode = True   # gotcha #13

BASE = os.path.expanduser("~/.claude-profiles")
if sys.platform == "win32":
    BASE = os.path.normpath(BASE)
METERS = os.path.join(BASE, "meters.jsonl")
TOKENS = os.path.join(BASE, "token_hours.json")
FORECAST = os.path.join(BASE, "forecast.json")
FHIST = os.path.join(BASE, "forecast_history.jsonl")
PROJECTS = os.path.expanduser("~/.claude/projects")

SNAPSHOT_EVERY = 300          # seconds; same cadence as Claude Code's own usage cache
TOKENS_EVERY = 900            # incremental log scan cadence
KEEP_DAYS = 35                # meter log retention (~10 MB at 1 KB / 5 min)
TOKEN_KEEP_HOURS = 24 * 10
WEEK_POINTS = 100.0
K_SEED = 0.30                 # weekly points per 5-hour point, measured 2026-10-01
HORIZON_H = 168
WINDOWS = {"7d": 168, "48h": 48, "24h": 24}

# API prices, $ per MTok (claude-api skill, cached 2026-09-25). Only the RATIOS matter:
# the meter log fixes the overall points-per-dollar scale. Cache writes: 1.25x input
# (5-minute TTL) and 2x (1-hour TTL); cache read as listed.
PRICES = {
    "opus":   {"in": 4.0,  "out": 20.0, "read": 0.20},
    "sonnet": {"in": 2.0,  "out": 10.0, "read": 0.20},
    "haiku":  {"in": 1.0,  "out": 5.0,  "read": 0.10},
    "fable":  {"in": 10.0, "out": 50.0, "read": 1.00},
}

_spec = importlib.util.spec_from_file_location("cswap", os.path.join(BASE, "claude-swap.py"))
cswap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cswap)
cswitch = cswap.cswitch


def now_ts():
    return time.time()


def iso_to_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1. METER LOG
# ---------------------------------------------------------------------------

def read_meters(since_ts=0):
    rows = []
    try:
        with open(METERS) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", 0) >= since_ts:
                    rows.append(r)
    except FileNotFoundError:
        pass
    return rows


def snapshot(sw=None):
    """One line in meters.jsonl: every account whose usage can be read without side
    effects. The fleet account is read through the shared slot (a borrowed account's
    own slot is empty — gotcha #50); every other account through its own login, ONLY
    if that token is still fresh. Expired = nothing is using it = its meter is not
    moving, so skipping it loses nothing (its last reading still stands)."""
    sw = sw or cswap.Swapper(out=lambda *a: None)
    fleet_o, _ = sw._oauth(sw.shared)
    who = sw.whoami(fleet_o)
    fleet_name = cswitch.profile_for_email(who[0]) if who else None
    acc = {}
    for n in sw.profiles():
        if n == fleet_name:
            o = fleet_o
        else:
            o, _ = sw._oauth(sw.anchor_slot(n))
            if not o or (cswap.minutes_left(o) or 0) < 2:
                continue
        # Through the shared cache (claude-swap.py USAGE RATE LIMIT): a reading the
        # watcher took < 4 min ago is reused, and "at" records when the server
        # actually produced it, so pace math never treats a reused reading as new.
        d = cswap.live_usage_raw(o, n, 240)
        if not d:
            continue
        f, s = d.get("five_hour") or {}, d.get("seven_day") or {}
        acc[n] = {"five": f.get("utilization"), "seven": s.get("utilization"),
                  "five_resets": f.get("resets_at"), "seven_resets": s.get("resets_at"),
                  "at": round(now_ts() - (cswap.usage_age(n) or 0), 1)}
    rec = {"ts": round(now_ts(), 1), "fleet": fleet_name, "acc": acc}
    with open(METERS, "a") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return rec


def prune_meters():
    rows = read_meters(now_ts() - KEEP_DAYS * 86400)
    tmp = METERS + ".tmp"
    with open(tmp, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(tmp, METERS)


def increments(rows):
    """[(t0, t1, points)] between consecutive readings of the SAME account, summed
    over accounts per interval. A drop in weekly % means the account refilled in
    between: count its new value (usage since the refill) and nothing before it —
    a slight undercount across a refill, never an overcount."""
    out = []
    last = {}
    for r in rows:
        for n, a in (r.get("acc") or {}).items():
            v = a.get("seven")
            t = a.get("at", r["ts"])
            if v is None:
                continue
            if n in last:
                t0, v0 = last[n]
                if t <= t0:          # the same cached reading logged twice
                    continue
                d = v - v0 if v >= v0 else v
                if d > 0:
                    out.append((t0, t, d))
            last[n] = (t, v)
    return out


def estimate_k(rows):
    """Weekly points per 5-hour point. Only intervals where both meters rose on the
    same account and neither reset. Needs >= 20 five-points of evidence, else seed."""
    dw = df = 0.0
    last = {}
    for r in rows:
        for n, a in (r.get("acc") or {}).items():
            f, s = a.get("five"), a.get("seven")
            if f is None or s is None:
                continue
            if n in last:
                f0, s0 = last[n]
                if f > f0 and s >= s0:
                    df += f - f0
                    dw += s - s0
            last[n] = (f, s)
    return (dw / df, df) if df >= 20 else (K_SEED, df)


# ---------------------------------------------------------------------------
# 2. TOKEN HISTORY (local session logs)
# ---------------------------------------------------------------------------

def price_for(model):
    m = (model or "").lower()
    for key in ("opus", "sonnet", "haiku", "fable"):
        if key in m:
            return PRICES[key]
    return None


def usage_cost(model, u):
    """API-equivalent dollars of one request's `usage` block."""
    p = price_for(model)
    if not p or not u:
        return 0.0
    cc = u.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens")
    w1 = cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = u.get("cache_creation_input_tokens") or 0, 0
    return ((u.get("input_tokens") or 0) * p["in"]
            + (u.get("output_tokens") or 0) * p["out"]
            + (u.get("cache_read_input_tokens") or 0) * p["read"]
            + (w5 or 0) * p["in"] * 1.25
            + (w1 or 0) * p["in"] * 2.0) / 1e6


def scan_file(path, offset, last_id, hours):
    """Add the cost of every NEW request in one transcript to hourly buckets.
    WHY dedupe by message.id: Claude Code writes one line per content block of an
    assistant message, each repeating the same usage block — counting lines would
    double- or triple-count. Lines of one message are contiguous, so remembering the
    last id per file is enough to resume mid-file without recounting."""
    seen_last = last_id
    with open(path, "rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if b'"usage"' not in raw or b'"assistant"' not in raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if e.get("type") != "assistant":
                continue
            m = e.get("message") or {}
            mid = m.get("id") or e.get("requestId")
            if not mid or mid == seen_last:
                continue
            seen_last = mid
            ts = iso_to_ts(e.get("timestamp"))
            if ts is None:
                continue
            c = usage_cost(m.get("model"), m.get("usage"))
            if c:
                h = str(int(ts // 3600))
                hours[h] = hours.get(h, 0.0) + c
        end = fh.tell()
    return end, seen_last


def update_tokens(max_age_days=8):
    """Incremental: only files modified since the last scan, only bytes after the
    stored offset. First run reads the last 8 days.
    Single-writer: a lock file (O_EXCL) stops the watcher and a hand-run
    `claude-meters tokens` from scanning at once and overwriting each other's
    offsets. A lock older than 1 h is a dead run's leftover and is taken over."""
    lock = TOKENS + ".lock"
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        try:
            if now_ts() - os.path.getmtime(lock) < 3600:
                return None
        except OSError:
            pass
        try:
            os.utime(lock, None)
        except OSError:
            return None
    try:
        return _update_tokens(max_age_days)
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def _update_tokens(max_age_days):
    try:
        st = json.load(open(TOKENS))
    except Exception:
        st = {"files": {}, "hours": {}, "scanned_at": 0}
    files, hours = st["files"], st["hours"]
    cutoff = now_ts() - max_age_days * 86400
    since = st.get("scanned_at", 0) - 120
    started = now_ts()
    for path in glob.iglob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
        try:
            s = os.stat(path)
        except OSError:
            continue
        if s.st_mtime < max(cutoff, since):
            continue
        off, last = files.get(path, [0, None])
        if off > s.st_size:          # file was rewritten: start over
            off, last = 0, None
        if off == s.st_size:
            continue
        try:
            files[path] = list(scan_file(path, off, last, hours))
        except OSError:
            continue
    keep_from = int((now_ts() - TOKEN_KEEP_HOURS * 3600) // 3600)
    st["hours"] = {h: v for h, v in hours.items() if int(h) >= keep_from}
    st["files"] = {p: v for p, v in files.items() if os.path.exists(p)}
    st["scanned_at"] = started
    tmp = TOKENS + ".tmp"
    json.dump(st, open(tmp, "w"), separators=(",", ":"))
    os.replace(tmp, TOKENS)
    return st


def dollars_between(hours, t0, t1):
    """Cost in [t0, t1], pro-rating partial hours."""
    total = 0.0
    h = int(t0 // 3600)
    while h * 3600 < t1:
        a, b = max(t0, h * 3600), min(t1, (h + 1) * 3600)
        if b > a:
            total += hours.get(str(h), 0.0) * (b - a) / 3600
        h += 1
    return total


# ---------------------------------------------------------------------------
# 3. PACE
# ---------------------------------------------------------------------------

def calibration(rows, hours):
    """Points per API-dollar, from every hour the meter log and the token history
    both cover. None until there is enough evidence (>= 1 h and >= 5 points), because
    a ratio fitted on minutes of data is noise."""
    inc = increments(rows)
    if not inc:
        return None
    t0, t1 = inc[0][0], inc[-1][1]
    pts = sum(d for _, _, d in inc)
    usd = dollars_between(hours, t0, t1)
    if t1 - t0 < 3600 or pts < 5 or usd <= 0:
        return {"ready": False, "hours": (t1 - t0) / 3600, "points": pts}
    return {"ready": True, "pts_per_usd": pts / usd, "hours": (t1 - t0) / 3600,
            "points": pts, "usd": usd}


def paces(rows, hours, cal, at=None):
    at = at or now_ts()
    out = {}
    inc = increments(rows)
    log_start = rows[0]["ts"] if rows else None
    for name, h in WINDOWS.items():
        t0 = at - h * 3600
        if log_start is not None and log_start <= t0 + 0.1 * h * 3600:
            pts = sum(d * max(0.0, min(b, at) - max(a, t0)) / max(1e-9, b - a)
                      for a, b, d in inc if b > t0)
            out[name] = {"v": pts / h, "src": "meters"}
        elif cal and cal.get("ready"):
            out[name] = {"v": dollars_between(hours, t0, at) * cal["pts_per_usd"] / h, "src": "est"}
        else:
            out[name] = {"v": None, "src": "calibrating"}
    return out


# ---------------------------------------------------------------------------
# 4. SIMULATION
# ---------------------------------------------------------------------------

def current_state(rows, sw=None):
    """Latest reading per account from the log; accounts never logged (idle, token
    expired) fall back to the cockpit's cached copy in their profile file — fine,
    because an idle account's meter is not moving."""
    st = {}
    for r in rows:
        for n, a in (r.get("acc") or {}).items():
            st[n] = dict(a, ts=r["ts"])
    for n in (sw.profiles() if sw else []):
        if n in st:
            continue
        try:
            cu = (json.load(open(os.path.join(BASE, n, ".claude.json"))).get("cachedUsageUtilization") or {})
            u = cu.get("utilization") or {}
            f, s = u.get("five_hour") or {}, u.get("seven_day") or {}
            st[n] = {"five": f.get("utilization"), "seven": s.get("utilization"),
                     "five_resets": f.get("resets_at"), "seven_resets": s.get("resets_at"),
                     "ts": (cu.get("fetchedAtMs") or 0) / 1000, "cached": True}
        except Exception:
            pass
    return st


def simulate(state, rate, k, at=None, horizon=HORIZON_H, dt=0.05):
    """Spend `rate` points/hour for `horizon` hours. Returns first_dry (hours from now
    or None), blocked hours, points that expired unused, and the account drawn first."""
    at = at or now_ts()
    acc = {}
    for n, a in state.items():
        R = iso_to_ts(a.get("seven_resets"))
        F = iso_to_ts(a.get("five_resets"))
        acc[n] = {"wk": a.get("seven") or 0.0, "f": a.get("five") or 0.0,
                  "R": None if R is None else (R - at) / 3600,
                  "F": None if F is None or F <= at else (F - at) / 3600}
        if acc[n]["R"] is not None and acc[n]["R"] <= 0:   # stale reading past its refill
            acc[n]["wk"], acc[n]["R"] = 0.0, acc[n]["R"] + 168
    t, first, blocked, wasted, first_acct = 0.0, None, 0.0, 0.0, None
    while t < horizon:
        for v in acc.values():
            if v["R"] is not None and t >= v["R"]:
                wasted += max(0.0, WEEK_POINTS - v["wk"])
                v["wk"], v["R"] = 0.0, v["R"] + 168
            if v["F"] is not None and t >= v["F"]:
                v["f"], v["F"] = 0.0, None
        need = rate * dt
        for n, v in sorted(acc.items(), key=lambda kv: kv[1]["R"] if kv[1]["R"] is not None else 1e9):
            if need <= 1e-9:
                break
            room = min(WEEK_POINTS - v["wk"], (100.0 - v["f"]) * k)
            if room <= 1e-9:
                continue
            x = min(need, room)
            v["wk"] += x
            v["f"] += x / k
            need -= x
            if v["F"] is None:
                v["F"] = t + 5
            if first_acct is None:
                first_acct = n
        if need > 1e-6:
            blocked += dt
            if first is None:
                first = t
        t += dt
    return {"first_dry_h": first, "blocked_h": blocked, "wasted": wasted, "use_first": first_acct}


def safe_rate(state, k, at=None):
    """Highest pace with zero blocked time over the next 7 days (bisection)."""
    lo, hi = 0.0, 60.0
    for _ in range(18):
        mid = (lo + hi) / 2
        if simulate(state, mid, k, at, dt=0.1)["blocked_h"] > 0:
            hi = mid
        else:
            lo = mid
    return lo


# ---------------------------------------------------------------------------
# 5. FORECAST + ACCURACY
# ---------------------------------------------------------------------------

def accuracy(rows):
    """Compare each forecast made 24-36 h ago with what the meters then showed."""
    try:
        hist = [json.loads(l) for l in open(FHIST)]
    except Exception:
        return None
    now = now_ts()
    cand = [h for h in hist if 24 * 3600 <= now - h["ts"] <= 36 * 3600]
    if not cand:
        return None
    h = cand[-1]
    inc = increments([r for r in rows if h["ts"] - 600 <= r["ts"] <= h["ts"] + 24 * 3600])
    actual = sum(d for _, _, d in inc)
    out = {"made_at": h["ts"], "actual_points": actual, "by_pace": {}}
    for name, pred in (h.get("predicted_24h") or {}).items():
        if pred:
            out["by_pace"][name] = {"predicted": pred, "miss_pct": (pred - actual) / max(actual, 1e-9) * 100}
    return out


def forecast(write=True):
    sw = cswap.Swapper(out=lambda *a: None)
    rows = read_meters(now_ts() - 8 * 86400)
    try:
        hours = json.load(open(TOKENS)).get("hours", {})
    except Exception:
        hours = {}
    k, k_evidence = estimate_k(rows)
    cal = calibration(rows, hours)
    pc = paces(rows, hours, cal)
    state = current_state(rows, sw)
    scen = {}
    for name, p in pc.items():
        if p["v"] is not None:
            scen[name] = simulate(state, p["v"], k)
    out = {"ts": now_ts(), "k": k, "k_evidence_pts": k_evidence, "calibration": cal,
           "paces": pc, "sustainable": max(1, len(state)) * WEEK_POINTS / 168, "safe_rate": safe_rate(state, k),
           "scenarios": scen, "state": state, "log_hours": ((rows[-1]["ts"] - rows[0]["ts"]) / 3600) if rows else 0,
           "accuracy": accuracy(rows)}
    if write:
        tmp = FORECAST + ".tmp"
        json.dump(out, open(tmp, "w"), separators=(",", ":"))
        os.replace(tmp, FORECAST)
        with open(FHIST, "a") as fh:
            fh.write(json.dumps({"ts": out["ts"], "predicted_24h": {
                n: (p["v"] * 24 if p["v"] is not None else None) for n, p in pc.items()}}) + "\n")
    return out


_LAST = {"snap": 0.0, "tok": 0.0, "prune": 0.0}


def tick(log=lambda m: None):
    """Called by the watcher every loop (claude-autoswitch.py). Cheap when nothing is
    due. Every failure is logged and swallowed — the watcher must never die here."""
    now = now_ts()
    try:
        if now - _LAST["tok"] >= TOKENS_EVERY:
            _LAST["tok"] = now
            update_tokens()
        if now - _LAST["snap"] >= SNAPSHOT_EVERY:
            _LAST["snap"] = now
            snapshot()
            forecast()
        if now - _LAST["prune"] >= 86400:
            _LAST["prune"] = now
            prune_meters()
    except Exception as e:
        log(f"meters/forecast error: {e}")


def fmt_when(h, at=None):
    if h is None:
        return "never (next 7 days)"
    t = datetime.datetime.fromtimestamp((at or now_ts()) + h * 3600)
    return f"{t.strftime('%a %#I:%M%p' if sys.platform == 'win32' else '%a %-I:%M%p').replace('AM', 'am').replace('PM', 'pm')} (in {h:.0f}h)"


def main():
    a = sys.argv[1:]
    cmd = a[0] if a else "forecast"
    if cmd == "snapshot":
        r = snapshot(); print(json.dumps(r, indent=1)); return
    if cmd == "tokens":
        t = time.time(); st = update_tokens()
        if st is None:
            print("another token scan is running (lock held) — skipped"); return
        print(f"token history: {len(st['hours'])} hours, {len(st['files'])} files, {time.time()-t:.0f}s"); return
    f = forecast(write="--no-write" not in a)
    cal = f["calibration"] or {}
    print(f"\nlog: {f['log_hours']:.1f} h of meter readings | K = {f['k']:.2f} weekly pts per 5h pt"
          f"{' (seed)' if f['k_evidence_pts'] < 20 else ''}")
    print(f"calibration: " + (f"{cal['pts_per_usd']:.3f} points per API-$ over {cal['hours']:.1f} h"
                               if cal.get("ready") else f"collecting ({cal.get('hours', 0):.1f} h, {cal.get('points', 0):.0f} pts so far)"))
    print(f"sustainable pace {f['sustainable']:.2f} pts/h | highest pace that never runs dry: {f['safe_rate']:.2f} pts/h\n")
    for name, p in f["paces"].items():
        s = f["scenarios"].get(name)
        if p["v"] is None:
            print(f"  {name:>3} pace: {p['src']}"); continue
        print(f"  {name:>3} pace {p['v']:5.2f} pts/h ({p['src']}): runs dry {fmt_when(s['first_dry_h'])}, "
              f"blocked {s['blocked_h']:.0f} h of next 7 days, use first: {s['use_first']}")


if __name__ == "__main__":
    main()
