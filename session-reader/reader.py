#!/usr/bin/python3 -I
"""
reader.py: the one local server behind the `reader` command. It shows every
Claude Code session in cmux as a live, readable web page, for a cmux browser
pane beside that session.

Why it exists: a terminal is a grid of same-size characters, so no terminal
theme makes a long Claude Code reply read like a document. This draws the same
conversation as a real web page: serif replies, real headings and tables, tool
calls folded into one line per burst, screenshots inline, and decision blocks
as cards whose buttons type the answer into the session's own pane.

How a page finds its session (no hook needed): Claude Code writes
<config>/sessions/<pid>.json for each running process, holding the CURRENT
session id. On every poll the server re-reads that file, so a /clear (a new
session id in the same process) is followed automatically. The transcript is
<config>/projects/<slug>/<sessionId>.jsonl.

Why the server is started by the `reader` command and never at login: cmux
refuses its socket to any process it did not start, unless the process carries
the per-pane key cmux puts in the environment (CMUX_SOCKET_CAPABILITY).
Measured with cmux 0.64.22: a detached process keeps working with that key,
even after the pane that started it closes, and can type into other panes;
without it cmux answers "Access denied - only processes started inside cmux can
connect". If cmux restarts, the key dies and /health reports cmux false; the
next `reader` replaces the server.

Safety rules. Keystrokes are the only thing this tool ever writes, and each
rule below guards them:
- Binds 127.0.0.1 only, and refuses any request whose Host header is not this
  server, so a DNS-rebinding page cannot talk to it.
- One random key in <READER_HOME>/key (mode 0600) gates every /api call.
- The answer POST needs the key in a custom header (X-Reader-Key). A cross-site
  page cannot send a custom header without a CORS preflight, and this server
  never answers a preflight (OPTIONS gets 501). A foreign Origin gets 403.
- An answer is one line of 1 to 300 characters with no control characters,
  and a pane takes at most one answer every 2 seconds.
- An answer goes only to a pane that ran `reader here` itself, or to a terminal
  tab cmux reported in the sidebar's window AND that runs a Claude process with
  its own sessions file. A plain shell is never a target.
- Before the first keystroke, cmux must confirm the registered pane is still
  exactly that surface in that workspace, and the text and the Enter are each
  sent only while cmux keeps replying "OK <surface> <workspace>" for exactly
  that pane. An unchecked send can type into the wrong pane.
- Reads transcripts; never writes them.

The transcript format is Claude Code's internal JSONL, not a published
contract. Every field is read defensively and an unknown entry is skipped,
never guessed.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent


# ---- settings ------------------------------------------------------------------
# SYNC WARNING: the `reader` launcher loads this file to read PORT, STATE, PANES,
# KEYFILE, PIDFILE, LOG, CMUX and CODE_MTIME, so the launcher and the server can
# never disagree about where the server lives. Renaming any of these breaks the
# launcher. uninstall.sh repeats the READER_HOME default in shell; keep it in step.

def _port():
    raw = os.environ.get("READER_PORT") or "8791"
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        raise SystemExit("reader: READER_PORT must be a number from 1024 to 65535 (got %r)." % raw)
    return int(raw)


def _state_dir():
    raw = os.environ.get("READER_HOME") or "~/.claude/reader"
    p = Path(raw).expanduser()
    # A relative path would resolve against whatever folder `reader` happened to
    # run in, so the key and the pane files would scatter across projects.
    if not p.is_absolute():
        raise SystemExit("reader: READER_HOME must be an absolute path (got %r)." % raw)
    return p


def _cmux_path():
    """cmux sets CMUX_BUNDLED_CLI_PATH inside its own panes; prefer it so the CLI
    always matches the running app, then PATH, then the default install."""
    for cand in (os.environ.get("CMUX_BUNDLED_CLI_PATH"), shutil.which("cmux"), "/Applications/cmux.app/Contents/Resources/bin/cmux"):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return "/Applications/cmux.app/Contents/Resources/bin/cmux"


PORT = _port()
STATE = _state_dir()
PANES = STATE / "panes"
KEYFILE = STATE / "key"
PIDFILE = STATE / "server.pid"
LOG = STATE / "server.log"
CMUX = _cmux_path()
# The launcher restarts a running server whose code is older than this file.
CODE_MTIME = int(Path(__file__).stat().st_mtime)


def config_dirs():
    """Every Claude Code config folder a sidebar tab might be running under:
    ~/.claude, $CLAUDE_CONFIG_DIR, and each folder in READER_CONFIG_DIRS
    (colon-separated, for people who run several Claude config folders). Read
    from the server's environment, which it inherits from the pane that ran
    `reader` first."""
    dirs = [Path.home() / ".claude"]
    extra = [os.environ.get("CLAUDE_CONFIG_DIR") or ""] + (os.environ.get("READER_CONFIG_DIRS") or "").split(":")
    for raw in extra:
        if raw.strip():
            p = Path(raw.strip()).expanduser()
            if p.is_absolute() and p not in dirs:
                dirs.append(p)
    return [d for d in dirs if (d / "sessions").is_dir()]


def run_cmux(*args, timeout=10):
    # CMUX_QUIET keeps cmux from adding hints to its output, so replies can be compared exactly.
    return subprocess.run([CMUX, *args], capture_output=True, text=True, env=dict(os.environ, CMUX_QUIET="1"), timeout=timeout)


# ---- transcript ----------------------------------------------------------------
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
OUT_MAX = 5000  # characters of tool output sent to the page per call
DIFF_MAX = 160  # diff lines sent per edit

# The only files served besides the page: an allow-list, so no request path ever
# reaches the disk unchecked.
VENDOR = {
    "/vendor/marked.min.js": ("marked.min.js", "text/javascript"),
    "/vendor/purify.min.js": ("purify.min.js", "text/javascript"),
    "/vendor/highlight.min.js": ("highlight.min.js", "text/javascript"),
    "/vendor/github-dark.min.css": ("github-dark.min.css", "text/css"),
    "/vendor/fonts/inter_variable.woff2": ("fonts/inter_variable.woff2", "font/woff2"),
    "/vendor/fonts/lora_variable.woff2": ("fonts/lora_variable.woff2", "font/woff2"),
    "/vendor/fonts/lora_italic_variable.woff2": ("fonts/lora_italic_variable.woff2", "font/woff2"),
    "/vendor/fonts/jetbrains_mono_regular.woff2": ("fonts/jetbrains_mono_regular.woff2", "font/woff2"),
    "/vendor/fonts/jetbrains_mono_medium.woff2": ("fonts/jetbrains_mono_medium.woff2", "font/woff2"),
}

SYS_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
SHELL_IN_RE = re.compile(r"<bash-input>(.*?)</bash-input>", re.S)
SHELL_OUT_RE = re.compile(r"<bash-(stdout|stderr)>(.*?)</bash-\1>", re.S)
CMD_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
TAG_RE = re.compile(r"</?(command-name|command-message|command-args|local-command-stdout|local-command-caveat)>")

CATEGORY = {
    "Bash": "command", "BashOutput": "command",
    "Read": "read", "Grep": "read", "Glob": "read", "LS": "read", "NotebookRead": "read",
    "Edit": "edit", "MultiEdit": "edit", "Write": "edit", "NotebookEdit": "edit",
    "WebSearch": "web", "WebFetch": "web",
    "Agent": "agent", "Task": "agent",
}

HOME = str(Path.home())


def tidy_path(p):
    """The page header shows the session's folder. Showing the home folder as ~
    keeps the account name out of screenshots and screen recordings."""
    p = str(p or "")
    return "~" + p[len(HOME):] if p == HOME or p.startswith(HOME + "/") else p


def short(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    base = lambda p: os.path.basename(str(p or "")) or str(p or "")
    if name == "Bash":
        return short(inp.get("description") or str(inp.get("command", "")).split("\n")[0], 110), str(inp.get("command", ""))
    if name in ("Read", "Edit", "MultiEdit", "Write", "NotebookEdit"):
        verb = {"Read": "Read", "Write": "Wrote", "NotebookEdit": "Edited"}.get(name, "Edited")
        return "%s %s" % (verb, base(inp.get("file_path") or inp.get("notebook_path"))), str(inp.get("file_path") or "")
    if name in ("Grep", "Glob"):
        return "Searched for %s" % short(inp.get("pattern"), 80), str(inp.get("path") or "")
    if name == "WebSearch":
        return "Searched the web: %s" % short(inp.get("query"), 90), ""
    if name == "WebFetch":
        return "Read %s" % short(urlparse(str(inp.get("url", ""))).netloc or inp.get("url"), 80), str(inp.get("url", ""))
    if name in ("Agent", "Task"):
        return "Agent: %s" % short(inp.get("description"), 90), ""
    if name == "Skill":
        return "Loaded skill %s" % short(inp.get("skill"), 60), ""
    if name == "ToolSearch":
        return "Loaded tools", short(inp.get("query"), 120)
    return name, ""


class Transcript:
    """Parses one session file into page items; re-parses only when it changes."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.version = None
        self.payload = b"{}"
        self.images = {}

    def current(self):
        st = self.path.stat()
        v = "%d:%d" % (st.st_size, st.st_mtime_ns)
        with self.lock:
            if v != self.version:
                try:
                    self.payload, self.images = self.parse(st)
                except Exception:
                    # One malformed line must not take the pane's feed down on every poll.
                    self.payload = json.dumps({"title": None, "cwd": None, "mtime": st.st_mtime, "items": [
                        {"k": "note", "id": "unreadable", "ts": None, "text": "Part of this session file could not be read by the reader."}]}).encode()
                    self.images = {}
                self.version = v
            return self.version, self.payload

    def add_image(self, images, src):
        if not isinstance(src, dict) or src.get("type") != "base64" or not isinstance(src.get("data"), str):
            return None
        # Only real image types are kept. The type from the transcript becomes the Content-Type of a
        # response on this server's own origin, and a click on a screenshot opens that response as a
        # page. A transcript entry (for example from a tool that returned an image) declaring text/html
        # would otherwise open as a script-running page with the page key in its URL.
        media = str(src.get("media_type") or "image/png").lower()
        if media not in IMAGE_TYPES:
            return None
        data = src.get("data") or ""
        iid = hashlib.sha1(data[:4096].encode()).hexdigest()[:16]
        images[iid] = (media, data)
        return iid

    def split_content(self, content, images):
        """(text, [image ids]) from a string or a list of content blocks."""
        if isinstance(content, str):
            return content, []
        text, imgs = [], []
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                text.append(b.get("text") or "")
            elif b.get("type") == "image":
                iid = self.add_image(images, b.get("source"))
                if iid:
                    imgs.append(iid)
        return "\n".join(text), imgs

    def parse(self, st):
        items, tools, images = [], {}, {}
        title = None
        cwd = None
        with open(self.path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                t = d.get("type")
                if t == "ai-title":
                    title = d.get("aiTitle") or title
                    continue
                if d.get("isSidechain"):
                    continue
                cwd = cwd or d.get("cwd")  # the folder the session started in, not where a command last moved
                ts = d.get("timestamp")
                if t == "attachment":
                    # A message typed while Claude was working arrives as a
                    # queued_command attachment, not a user entry.
                    a = d.get("attachment") or {}
                    if isinstance(a, dict) and a.get("type") == "queued_command" and a.get("humanTurn"):
                        text, imgs = self.split_content(a.get("prompt"), images)
                        items.append({"k": "user", "id": d.get("uuid"), "ts": a.get("timestamp") or ts, "text": self.clean(text), "images": imgs, "queued": True})
                    continue
                msg = d.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else None
                if t == "user":
                    if d.get("isMeta") or d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly"):
                        continue
                    has_results = isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                    if has_results:
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_result":
                                self.attach_result(tools.get(b.get("tool_use_id")), b, d.get("toolUseResult"), images)
                    text, imgs = ("", []) if has_results else self.split_content(content, images)
                    if not text.strip() and not imgs:
                        continue
                    if text.lstrip().startswith("<task-notification>"):
                        items.append({"k": "note", "id": d.get("uuid"), "ts": ts, "text": "A background task finished"})
                        continue
                    if "<local-command-stdout>" in text or "<local-command-caveat>" in text:
                        continue
                    # `! cmd` and `/command` arrive as tagged text; show them as one quiet line, never raw tags.
                    m = SHELL_IN_RE.search(text)
                    if m:
                        items.append({"k": "note", "id": d.get("uuid"), "ts": ts, "text": "You ran  ! " + short(m.group(1), 120)})
                        continue
                    m = SHELL_OUT_RE.search(text)
                    if m:
                        out = short(SYS_RE.sub("", m.group(2)), 160)
                        if out:
                            items.append({"k": "note", "id": d.get("uuid"), "ts": ts, "text": out})
                        continue
                    m = CMD_RE.search(text)
                    if m:
                        args = CMD_ARGS_RE.search(text)
                        items.append({"k": "note", "id": d.get("uuid"), "ts": ts, "text": "You ran  " + short(m.group(1) + (" " + args.group(1) if args and args.group(1).strip() else ""), 120)})
                        continue
                    items.append({"k": "user", "id": d.get("uuid"), "ts": ts, "text": self.clean(text), "images": imgs})
                elif t == "assistant" and isinstance(content, list):
                    for n, b in enumerate(content):
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text" and (b.get("text") or "").strip():
                            items.append({"k": "text", "id": "%s:%d" % (d.get("uuid"), n), "ts": ts, "text": b["text"]})
                        elif b.get("type") == "tool_use":
                            summary, detail = summarize(b.get("name"), b.get("input"))
                            item = {"k": "tool", "id": b.get("id"), "ts": ts, "name": b.get("name"), "cat": CATEGORY.get(b.get("name"), "other"), "summary": summary, "detail": detail, "result": None}
                            tools[b.get("id")] = item
                            items.append(item)
        payload = {"title": title, "cwd": tidy_path(cwd), "mtime": st.st_mtime, "items": items}
        return json.dumps(payload).encode(), images

    @staticmethod
    def clean(text):
        text = SYS_RE.sub("", text or "")
        text = TAG_RE.sub("", text)
        return text.strip()

    def attach_result(self, item, block, tur, images):
        if item is None:
            return
        text, imgs = "", []
        c = block.get("content")
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            text, imgs = self.split_content(c, images)
        res = {"error": bool(block.get("is_error")), "text": text[:OUT_MAX], "more": max(0, len(text) - OUT_MAX), "images": imgs}
        if isinstance(tur, dict):
            patch = tur.get("structuredPatch")
            if isinstance(patch, list) and patch:
                lines, add, rem = [], 0, 0
                for hk in patch:
                    for ln in hk.get("lines", []) if isinstance(hk, dict) else []:
                        if not isinstance(ln, str):
                            continue
                        add += ln.startswith("+")
                        rem += ln.startswith("-")
                        if len(lines) < DIFF_MAX:
                            lines.append(ln)
                res["diff"] = {"lines": lines, "add": add, "del": rem}
            elif tur.get("type") == "create" and isinstance(tur.get("content"), str):
                body = tur["content"].split("\n")
                res["diff"] = {"lines": ["+" + x for x in body[:DIFF_MAX]], "add": len(body), "del": 0}
        item["result"] = res


# ---- panes and sessions ----------------------------------------------------------
REF_RE = re.compile(r"surface:\d+")
WS_REF_RE = re.compile(r"workspace:\d+")
TTY_RE = re.compile(r"ttys\d+")
UUID_RE = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")


def load_key():
    """A NEW key on every server start, written to READER_HOME/key (mode 0600). WHY it is not kept
    between runs: an open page keeps polling with its key, and if the server stops, any program that
    binds the port next receives those requests. A key that dies with its server makes whatever it
    captured worthless once the real server is back. The state folder must be a real folder owned by
    this user, so a symlink or a shared folder cannot redirect the key file."""
    STATE.mkdir(parents=True, exist_ok=True)
    st = os.lstat(str(STATE))
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise SystemExit("reader: %s must be a real folder owned by you; nothing was started." % STATE)
    os.chmod(str(STATE), 0o700)
    key = secrets.token_urlsafe(24)
    tmp = STATE / ("key.%d.tmp" % os.getpid())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    os.replace(str(tmp), str(KEYFILE))
    return key


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


class Panes:
    """Registered panes, each resolved to its current session on every call."""

    def __init__(self):
        self.lock = threading.Lock()
        self.paths = {}
        self.transcripts = {}
        self.found = {}  # "surface:N" -> registration, filled by window_view() from what cmux reports
        # Claude Code deletes <config>/sessions/<pid>.json when the process exits, so
        # an ended session can no longer be looked up. The transcript last shown for
        # each pane is kept here so its page still shows the conversation once the
        # session has ended (for as long as this server runs).
        self.last = {}

    def reg(self, surface):
        surface = surface or ""
        if REF_RE.fullmatch(surface):
            return self.found.get(surface)  # discovered by the sidebar during this run; never read from disk
        surface = surface.upper()
        if not UUID_RE.fullmatch(surface):
            return None
        try:
            reg = json.loads((PANES / ("%s.json" % surface)).read_text())
        except (OSError, ValueError):
            return None
        # A registration is only trusted if it still has the shape `reader here` writes.
        if not (isinstance(reg, dict) and REF_RE.fullmatch(str(reg.get("surface_ref", ""))) and WS_REF_RE.fullmatch(str(reg.get("workspace_ref", "")))):
            return None
        return reg

    def resolve(self, surface):
        reg = self.reg(surface)
        if not reg:
            return None, None, False
        cfg = Path(str(reg.get("config_dir") or ""))
        alive = pid_alive(reg.get("claude_pid"))
        sid = None
        try:
            sid = json.loads((cfg / "sessions" / ("%d.json" % int(reg["claude_pid"]))).read_text()).get("sessionId")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        key = str(reg.get("surface"))
        with self.lock:
            if not (sid and UUID_RE.fullmatch(str(sid).upper())):
                last = None if alive else self.last.get(key)
                return reg, (self.transcripts.get(last) if last else None), alive
            path = self.paths.get(sid)
            if path is None:
                hits = list(cfg.glob("projects/*/%s.jsonl" % sid))
                if not hits:
                    return reg, None, alive  # a new session writes its file at the first message
                path = self.paths[sid] = hits[0]
            tr = self.transcripts.get(path)
            if tr is None:
                tr = self.transcripts[path] = Transcript(path)
            self.last[key] = path
        return reg, tr, alive


_tty_cache = {}


def claude_on_tty(tty):
    """The Claude process running on a terminal device, found by Claude Code's own
    <config>/sessions/<pid>.json (so a shell or an MCP child on the same device is
    never mistaken for it). Newest process wins. Cached 5 s."""
    hit = _tty_cache.get(tty)
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    found = None
    try:
        out = subprocess.run(["/bin/ps", "-o", "pid=", "-t", tty], capture_output=True, text=True, timeout=5).stdout
        pids = sorted((int(x) for x in out.split() if x.isdigit()), reverse=True)
    except (OSError, subprocess.SubprocessError):
        pids = []
    dirs = config_dirs()
    for pid in pids:
        for cfg in dirs:
            if (cfg / "sessions" / ("%d.json" % pid)).exists():
                found = (pid, cfg)
                break
        if found:
            break
    _tty_cache[tty] = (time.time(), found)
    return found


_tree_cache = {}


def window_view(panes, wref):
    """What the sidebar for one window needs: its selected workspace, every Claude
    session in it (one per terminal tab), and which one has focus right now."""
    hit = _tree_cache.get(wref)
    if hit and time.time() - hit[0] < 0.8:
        return hit[1]
    try:
        tree = json.loads(run_cmux("tree", "--window", wref, "--json").stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"error": "cmux did not answer; run ! reader again"}
    win = next((w for w in tree.get("windows", []) if w.get("ref") == wref), None)
    if not win:
        return {"error": "this window is gone"}
    ws = next((w for w in win.get("workspaces", []) if w.get("ref") == win.get("selected_workspace_ref")), None)
    if not ws:
        return {"error": "no workspace selected"}
    sessions, focused = [], None
    for pane in ws.get("panes", []):
        for sf in pane.get("surfaces", []):
            tty = sf.get("tty") or ""
            # Only a terminal that cmux itself reports, on a real tty, can be a target.
            if sf.get("type") != "terminal" or not TTY_RE.fullmatch(tty) or not REF_RE.fullmatch(str(sf.get("ref", ""))):
                continue
            found = claude_on_tty(tty)
            if not found:
                continue  # a plain shell: listed nowhere, so never a target
            ref = sf["ref"]
            panes.found[ref] = {"surface": ref, "surface_ref": ref, "workspace_ref": ws["ref"], "claude_pid": found[0], "config_dir": str(found[1]), "cwd": None}
            reg, tr, alive = panes.resolve(ref)
            working = bool(tr and alive and time.time() - tr.path.stat().st_mtime < 8)
            sessions.append({"s": ref, "title": sf.get("title") or ref, "working": working, "alive": alive})
            if pane.get("focused") and sf.get("selected_in_pane"):
                focused = ref
    view = {"workspace": ws.get("title"), "workspace_ref": ws["ref"], "sessions": sessions, "focused": focused}
    _tree_cache[wref] = (time.time(), view)
    return view


_active = [0.0, ""]


def active_window():
    """The number of the focused cmux window ("" if cmux does not say). Cached 0.8 s."""
    if time.time() - _active[0] > 0.8:
        try:
            ref = ((json.loads(run_cmux("identify", "--no-caller", "--json", timeout=5).stdout or "{}").get("focused") or {}).get("window_ref") or "")
        except (OSError, subprocess.SubprocessError, ValueError):
            ref = ""
        _active[:] = [time.time(), ref.split(":")[1] if re.fullmatch(r"window:\d+", ref) else _active[1]]
    return _active[1]


_cmux_ok = [0.0, False]


def cmux_ok():
    """Can this process still use cmux's socket? Cached 10 s."""
    if time.time() - _cmux_ok[0] > 10:
        try:
            _cmux_ok[1] = run_cmux("list-windows", timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            _cmux_ok[1] = False
        _cmux_ok[0] = time.time()
    return _cmux_ok[1]


def pane_confirmed(reg):
    """True only if cmux itself says the registered pane is still exactly where the
    registration says: the same surface, in the same workspace, and a terminal.
    Asked BEFORE anything is typed, because `cmux send` types first and replies after."""
    target = reg.get("surface")
    target = target if UUID_RE.fullmatch(str(target or "").upper()) else reg["surface_ref"]
    try:
        p = run_cmux("identify", "--json", "--workspace", reg["workspace_ref"], "--surface", target)
        c = json.loads(p.stdout or "{}").get("caller") or {}
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return False
    return c.get("surface_ref") == reg["surface_ref"] and c.get("workspace_ref") == reg["workspace_ref"] and c.get("surface_type") == "terminal"


def confirmed(p, expect):
    # Compare whole lines: with a substring test, cmux's reply for workspace twelve
    # would also count as confirmation for workspace one.
    return p.returncode == 0 and expect in [ln.strip() for ln in (p.stdout or "").splitlines()]


TICKET_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")
LSOF = "/usr/sbin/lsof"


def peer_is_this_user(client_port, server_port):
    """True only if the process on the client end of this loopback connection runs as the same user as
    this server. WHY: a launch ticket sits in a command line for a moment, and other accounts on the
    Mac can read command lines with ps. Without this check any of them could redeem the ticket first
    and get the key. Fails closed: if lsof cannot say, the answer is no."""
    try:
        out = subprocess.run([LSOF, "-nP", "-w", "-iTCP@" + "127.0.0.1:%d" % int(client_port), "-Fpun"],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    want = "127.0.0.1:%d->127.0.0.1:%d" % (int(client_port), int(server_port))
    uid = None
    for ln in out.splitlines():
        if ln.startswith("p"):
            uid = None  # a new process record: never carry the previous process's uid forward
        elif ln.startswith("u"):
            uid = ln[1:]
        elif ln.startswith("n") and ln[1:] == want:
            return uid == str(os.getuid())
    return False


CARD_RE = re.compile(r"[0-9]{1,3} \u2192 [A-Z]")
# A line that starts with one of these is a shell escape, a slash command, a background marker or a
# mention in Claude Code. Free text typed into a card may not start that way.
BLOCKED_FIRST = "!/&#@$~-"


def answer_text_ok(raw):
    """An answer is typed into a Claude Code prompt, where a line that starts with ! runs as a shell
    command, so only plainly visible characters are allowed. Refused: a backslash (`cmux send` turns the
    two characters backslash-n and backslash-r into Enter and backslash-t into Tab, so a printable "\\n" would
    submit a second, attacker-chosen line), every Unicode control, format, surrogate, private-use and
    unassigned character (this covers invisible tag characters, zero-width and bidi marks) and the line
    and paragraph separators. All of that is checked on the raw text and again on its NFKC form, because
    some full-width and small-form characters fold to a backslash or to ! / &. Checked before trimming,
    so a trailing newline is refused, not dropped.
    Two shapes are accepted: exactly what a card types ("<n> -> <letter>"), or free text that does not
    start with a character in BLOCKED_FIRST and is not just a number (a lone digit is a menu hotkey)."""
    if not isinstance(raw, str):
        return False
    folded = unicodedata.normalize("NFKC", raw)
    for form in (raw, folded):
        if "\\" in form:
            return False
        for ch in form:
            if unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in ("Zl", "Zp"):
                return False
    t = folded.strip()
    if CARD_RE.fullmatch(t):
        return True
    if not t or t[0] in BLOCKED_FIRST or re.fullmatch(r"[0-9]+", t):
        return False
    return True


def claude_status(reg):
    """Claude Code's own report of what the session is doing, from <config>/sessions/<pid>.json:
    "idle" (waiting at its prompt), "busy", "waiting" (a permission or question dialog is open) or
    "shell". Anything unreadable is None, which callers treat as not idle (fail closed)."""
    try:
        d = json.loads((Path(reg["config_dir"]) / "sessions" / ("%d.json" % int(reg["claude_pid"]))).read_text())
        return d.get("status") if isinstance(d, dict) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def claude_still_there(reg):
    """True only while the Claude process this registration names is alive and still has its own
    sessions file (Claude Code removes the file when a session exits). Without this, an answer for a
    session that has ended would be typed into whatever shell now sits in that pane."""
    try:
        pid = int(reg["claude_pid"])
    except (KeyError, TypeError, ValueError):
        return False
    return pid_alive(pid) and (Path(reg["config_dir"]) / "sessions" / ("%d.json" % pid)).exists()


def send_answer(reg, text):
    ws, surf = reg["workspace_ref"], reg["surface_ref"]
    expect = "OK %s %s" % (surf, ws)
    if not claude_still_there(reg):
        return False, "this Claude session has ended; nothing was typed."
    # WHY idle only: while Claude Code shows a permission or question dialog, a typed digit is a menu
    # hotkey that selects and confirms an option at once (even "Yes, and do not ask again"). A card can
    # still be on screen then, so the answer is typed only while Claude says it is waiting at its prompt.
    if claude_status(reg) != "idle":
        return False, "Claude is not waiting at its prompt (it is working, or showing you a question); nothing was typed. Answer in the terminal."
    if not pane_confirmed(reg):
        return False, "cmux could not confirm this session's pane; nothing was typed. Run ! reader in that session again."
    p = run_cmux("send", "--workspace", ws, "--surface", surf, "--", text)
    if not confirmed(p, expect):
        return False, "cmux did not confirm this session's pane (%s); Enter was not pressed. Run ! reader in that session again." % short(p.stdout or p.stderr, 120)
    time.sleep(0.4)
    if claude_status(reg) != "idle":
        return False, "Claude changed state while the answer was being typed; Enter was not pressed."
    p = run_cmux("send-key", "--workspace", ws, "--surface", surf, "enter")
    if not confirmed(p, expect):
        return False, "text typed but Enter was not confirmed (%s)" % short(p.stdout or p.stderr, 120)
    return True, "sent"


# ---- http ------------------------------------------------------------------------
# The page may load and contact only this server. A reply can contain a markdown
# image or link pointing anywhere; this policy keeps such an image from loading
# (no third-party request reveals that you are reading) and keeps any script from
# sending the page's key or content elsewhere. 'unsafe-inline' is needed because
# the page's own script and style are inline; DOMPurify still strips injected script.
PAGE_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def make_handler(panes, key, port):
    last_send = {}
    send_lock = threading.Lock()
    hosts = ("127.0.0.1:%d" % port, "localhost:%d" % port)
    origins = tuple("http://" + h for h in hosts)

    def key_ok(given):
        return hmac.compare_digest(str(given or "").encode(), key.encode())

    lsof_slots = threading.BoundedSemaphore(4)  # lsof is slow; unauthenticated callers may not pile it up

    def ticket_is_fresh(t):
        t = str(t or "")
        if not TICKET_RE.fullmatch(t):
            return False
        try:
            st = os.lstat(str(STATE / "tickets" / t))
        except OSError:
            return False
        return stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and 0 <= time.time() - st.st_mtime < 20

    def redeem_ticket(t):
        """A launch ticket is a file the launcher wrote in READER_HOME/tickets (mode 0600, owned by this
        user). It is not created over HTTP, so the key never travels to whatever answers on the port, and
        it is valid for 20 seconds and once."""
        t = str(t or "")
        if not TICKET_RE.fullmatch(t):
            return False
        path = STATE / "tickets" / t
        try:
            st = os.lstat(str(path))  # lstat: a symlink is refused, never followed
            ok = stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and 0 <= time.time() - st.st_mtime < 20
            path.unlink()
        except OSError:
            return False
        return ok

    class H(BaseHTTPRequestHandler):
        timeout = 15  # a client that stalls mid-request cannot hold a thread forever

        def log_message(self, *a):
            pass

        def reply(self, code, body, ctype="application/json", csp=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if csp:
                self.send_header("Content-Security-Policy", csp)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self):
            # A page on another site can point its own domain at 127.0.0.1 (DNS
            # rebinding) and then read this server as "same origin". Its requests
            # still carry that domain in Host, so anything but this server is refused.
            return self.headers.get("Host") in hosts

        def origin_ok(self):
            origin = self.headers.get("Origin")
            return not origin or origin in origins

        def do_GET(self):
            if not self.host_ok():
                return self.reply(403, b'{"error":"host"}')
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/health":
                body = {"ok": True, "code": CODE_MTIME, "pid": os.getpid(), "cmux": cmux_ok()}
                nonce = q.get("n", [""])[0]
                if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", nonce):
                    # Proof that this process knows the key, which no other program on the port can
                    # produce. The launcher checks it before it stops, trusts or hands a URL to a server.
                    body["proof"] = hmac.new(key.encode(), nonce.encode(), hashlib.sha256).hexdigest()
                return self.reply(200, json.dumps(body).encode())
            if u.path == "/" or u.path.startswith("/p/") or u.path.startswith("/w/"):
                # Read from disk on every load so a page edit shows on reload, with no restart.
                return self.reply(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8", PAGE_CSP)
            if u.path in VENDOR:
                return self.reply(200, (HERE / "vendor" / VENDOR[u.path][0]).read_bytes(), VENDOR[u.path][1])
            if not u.path.startswith("/api/"):
                return self.reply(404, b"{}")
            if u.path == "/api/redeem":
                # A launcher URL carries a one-time ticket instead of the key, because the URL is
                # passed to cmux on a command line that other local users can read with ps.
                t = q.get("t", [""])[0]
                if not self.origin_ok() or not ticket_is_fresh(t):
                    return self.reply(403, b'{"error":"ticket"}')
                if not lsof_slots.acquire(blocking=False):
                    return self.reply(503, b'{"error":"busy"}')
                try:
                    mine = peer_is_this_user(self.client_address[1], self.server.server_address[1])
                finally:
                    lsof_slots.release()
                if not mine or not redeem_ticket(t):
                    return self.reply(403, b'{"error":"ticket"}')
                return self.reply(200, json.dumps({"k": key}).encode())
            if not key_ok(q.get("k", [""])[0]):
                return self.reply(403, b'{"error":"key"}')
            if not self.origin_ok():
                return self.reply(403, b'{"error":"origin"}')
            if u.path == "/api/window":
                w = q.get("w", [""])[0]
                if w == "active":
                    # The Dock is shown in one window at a time and can move between
                    # windows, so the sidebar follows whichever cmux window has focus.
                    w = active_window()
                if not w.isdigit():
                    return self.reply(400, b'{"error":"window"}')
                return self.reply(200, json.dumps(window_view(panes, "window:" + w)).encode())
            reg, tr, alive = panes.resolve(q.get("s", [""])[0])
            if not reg:
                return self.reply(404, b'{"error":"This pane has no reader yet. Run ! reader in its Claude session."}')
            if u.path == "/api/feed":
                if tr is None:
                    v, payload = "waiting", json.dumps({"title": None, "cwd": tidy_path(reg.get("cwd")), "mtime": 0, "items": []}).encode()
                else:
                    v, payload = tr.current()
                idle = bool(alive and claude_status(reg) == "idle")
                v = "%s|%s|%s" % (v, "live" if alive else "ended", "idle" if idle else "busy")
                if q.get("v", [""])[0] == v:
                    return self.reply(200, json.dumps({"same": True, "v": v}).encode())
                return self.reply(200, b'{"v":' + json.dumps(v).encode() + b',"alive":' + (b"true" if alive else b"false") + b',"idle":' + (b"true" if idle else b"false") + b',"data":' + payload + b"}")
            if u.path == "/api/image" and tr is not None:
                tr.current()
                img = tr.images.get(q.get("id", [""])[0])
                if img:
                    try:
                        raw = base64.b64decode(img[1], validate=True)
                    except ValueError:
                        return self.reply(404, b"{}")
                    # Second layer: even if a wrong type ever got through, a sandboxed document with
                    # no allowed sources cannot run script or read this origin.
                    return self.reply(200, raw, img[0], csp="default-src 'none'; sandbox")
            return self.reply(404, b"{}")

        def do_POST(self):
            if not self.host_ok():
                return self.reply(403, b'{"error":"host"}')
            path = urlparse(self.path).path
            if path != "/api/answer":
                return self.reply(404, b"{}")
            if not key_ok(self.headers.get("X-Reader-Key")):
                return self.reply(403, b'{"error":"key"}')
            if not self.origin_ok():
                return self.reply(403, b'{"error":"origin"}')
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n < 0:
                    raise ValueError
                body = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
            except (ValueError, RecursionError):
                return self.reply(400, b'{"error":"json"}')
            if not isinstance(body, dict):
                return self.reply(400, b'{"error":"json"}')
            # The text is checked before the pane is looked up, so a bad answer is
            # refused the same way whether or not the pane exists. Control characters
            # (C0, DEL, C1) and the Unicode line and paragraph separators are refused
            # anywhere in the raw text, before trimming, so a trailing \r or \n is
            # refused rather than silently dropped.
            raw = body.get("text", "")
            if not answer_text_ok(raw):
                return self.reply(400, b'{"error":"an answer is one line of 1 to 300 characters"}')
            text = raw.strip()
            if not 1 <= len(text) <= 300:
                return self.reply(400, b'{"error":"an answer is one line of 1 to 300 characters"}')
            reg = panes.reg(str(body.get("s", "")))
            if not reg:
                return self.reply(404, b'{"error":"This pane has no reader yet. Run ! reader in its Claude session."}')
            with send_lock:
                if time.time() - last_send.get(reg["surface"], 0) < 2:
                    return self.reply(429, b'{"error":"one answer every 2 seconds"}')
                last_send[reg["surface"]] = time.time()
            ok, why = send_answer(reg, text)
            return self.reply(200 if ok else 502, json.dumps({"ok": ok, "message": why}).encode())

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true", help="run the server (the reader command does this for you)")
    a = ap.parse_args()
    if not a.serve:
        raise SystemExit("reader.py is the server. Open a reader from a Claude session in cmux with: ! reader")
    key = load_key()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(Panes(), key, PORT))
    fd = os.open(str(PIDFILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))
    print("%s reader server pid %d on 127.0.0.1:%d" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), PORT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
