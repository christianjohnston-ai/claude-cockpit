#!/usr/bin/python3 -I
"""
security_check.py: checks a RUNNING reader server against its security rules,
over HTTP, using the same READER_PORT and READER_HOME as your install.

    /usr/bin/python3 -I tests/security_check.py

It sends only requests that the server must refuse, so it never types anything
into a terminal: every answer it posts is either malformed, unauthenticated,
from a foreign origin, or aimed at a pane id that was never registered.

Covered here: reply text cannot draw controls, only real image types are served, localhost-only Host, the key on every /api call, the custom-header
answer POST with no CORS preflight, foreign Origin, the answer text limits,
unregistered panes, the vendor allow-list with matching SRI hashes, and the
page's Content-Security-Policy. The rules that need a real Claude pane (one
answer per pane every 2 seconds, sending only after cmux confirms the pane) are
checked by hand in a throwaway cmux workspace; see README.md.

Prints the key's length, never the key.
"""
import base64
import hashlib
import http.client
import importlib.util
import json
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_settings():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("reader_server", str(ROOT / "reader.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


srv = load_settings()
PORT = srv.PORT
results = []


def req(method, path, headers=None, body=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    h = {"Host": host or "127.0.0.1:%d" % PORT}
    h.update(headers or {})
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in h.items():
        c.putheader(k, v)
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b"")
    if method == "POST":
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", str(len(data)))
    c.endheaders(data if method == "POST" else None)
    r = c.getresponse()
    out = (r.status, dict(r.getheaders()), r.read())
    c.close()
    return out


def check(rule, label, got, want):
    ok = got == want
    results.append(ok)
    print("%s  rule %-2s %-62s got %s, want %s" % ("PASS" if ok else "FAIL", rule, label, got, want))


def main():
    try:
        key = srv.KEYFILE.read_text().strip()
    except OSError:
        sys.exit("No key at %s. Start the server first (run ! reader here in a Claude session in cmux)." % srv.KEYFILE)
    try:
        health = json.loads(req("GET", "/health")[2])
    except OSError:
        sys.exit("No reader server on 127.0.0.1:%d." % PORT)
    print("server pid %s on 127.0.0.1:%d, key length %d\n" % (health.get("pid"), PORT, len(key)))
    ghost = str(uuid.uuid4()).upper()  # a pane id that was never registered
    feed = "/api/feed?s=%s" % ghost

    # 1. localhost only, and the key gates every /api call
    check(1, "GET /api/feed with no key", req("GET", feed)[0], 403)
    check(1, "GET /api/feed with a wrong key", req("GET", feed + "&k=wrong")[0], 403)
    check(1, "GET /api/window with no key", req("GET", "/api/window?w=active")[0], 403)
    check(1, "GET /api/image with no key", req("GET", "/api/image?s=%s&id=x" % ghost)[0], 403)
    check(1, "GET /api/feed with a foreign Host header (DNS rebinding)", req("GET", feed + "&k=" + key, host="attacker.example:%d" % PORT)[0], 403)
    check(1, "GET /health with a foreign Host header", req("GET", "/health", host="attacker.example")[0], 403)

    # 2. answers need the key in X-Reader-Key; a CORS preflight is never answered
    answer = {"s": ghost, "text": "A"}
    check(2, "POST /api/answer with no key", req("POST", "/api/answer", body=answer)[0], 403)
    check(2, "POST /api/answer with the key only in the URL", req("POST", "/api/answer?k=" + key, body=answer)[0], 403)
    check(2, "POST /api/answer with a wrong X-Reader-Key", req("POST", "/api/answer", {"X-Reader-Key": "wrong"}, answer)[0], 403)
    pre = req("OPTIONS", "/api/answer", {"Origin": "https://attacker.example", "Access-Control-Request-Method": "POST",
                                         "Access-Control-Request-Headers": "x-reader-key,content-type"})
    check(2, "OPTIONS preflight from another site", pre[0], 501)
    check(2, "preflight reply grants no CORS access", any(k.lower().startswith("access-control-") for k in pre[1]), False)

    # 3. a foreign Origin is refused
    good = {"X-Reader-Key": key}
    check(3, "POST /api/answer from Origin https://attacker.example", req("POST", "/api/answer", dict(good, Origin="https://attacker.example"), answer)[0], 403)
    check(3, "POST /api/answer from Origin null", req("POST", "/api/answer", dict(good, Origin="null"), answer)[0], 403)
    check(3, "GET /api/feed from a foreign Origin", req("GET", feed + "&k=" + key, {"Origin": "https://attacker.example"})[0], 403)

    # 4. an answer is one line of 1 to 300 characters with no control characters
    for label, text in [("empty answer", ""), ("only spaces", "   "), ("301 characters", "x" * 301),
                        ("a line break", "A\nrm -rf ~"), ("a carriage return", "A\r"), ("an escape character", "A\x1b[2J"),
                        ("a tab", "A\tB"), ("a C1 control character", "A\x9b"), ("a trailing line break", "A\n"),
                        ("a Unicode line separator", "A\u2028B"), ("a Unicode paragraph separator", "A\u2029")]:
        check(4, "answer with %s" % label, req("POST", "/api/answer", good, {"s": "", "text": text})[0], 400)
    check(4, "body that is not JSON", req("POST", "/api/answer", good, b"not json")[0], 400)
    check(4, "body that is a JSON list", req("POST", "/api/answer", good, ["A"])[0], 400)

    # 6. answers go only to a registered pane or a Claude tab cmux reported
    check(6, "valid answer to a pane id that was never registered", req("POST", "/api/answer", good, answer)[0], 404)
    # A well-formed ref far above any number cmux hands out, so it can never be a real tab.
    unreported = "surface:%d" % (10 ** 9)
    check(6, "valid answer to a surface ref no sidebar has reported", req("POST", "/api/answer", good, {"s": unreported, "text": "A"})[0], 404)
    check(6, "valid answer to a path-like pane id", req("POST", "/api/answer", good, {"s": "../../key", "text": "A"})[0], 404)
    check(6, "feed for a pane id that was never registered", req("GET", feed + "&k=" + key)[0], 404)

    # 8. only the allow-listed vendor files are served, and each matches the page's SRI hash
    page = req("GET", "/")
    html = page[2].decode()
    for path in ["/vendor/SHA384.txt", "/vendor/fonts/inter_OFL.txt", "/vendor/../reader.py", "/vendor/%2e%2e/reader.py",
                 "/reader.py", "/index.html", "/README.md", "/vendor/", "/vendor/marked.min.js.map"]:
        check(8, "GET %s" % path, req("GET", path)[0], 404)
    sri = dict(re.findall(r'(?:src|href)="(/vendor/[^"]+)" integrity="(sha384-[^"]+)"', html))
    for path, (name, _ctype) in sorted(srv.VENDOR.items()):
        status, _h, body = req("GET", path)
        check(8, "GET %s" % path, status, 200)
        if path in sri:
            digest = "sha384-" + base64.b64encode(hashlib.sha384(body).digest()).decode()
            check(9, "SRI hash matches for %s" % name, digest, sri[path])
    check(9, "every script and stylesheet in the page carries an SRI hash",
          sorted(re.findall(r'(?:src|href)="(/vendor/[^"]+\.(?:js|css))"', html)), sorted(sri))

    # 9. the page itself loads nothing from another site
    csp = page[1].get("Content-Security-Policy", "")
    check(9, "page sends a Content-Security-Policy", bool(csp), True)
    check(9, "CSP limits connections to this server", "connect-src 'self'" in csp, True)
    external = [u for u in re.findall(r'(?:src|href)\s*=\s*"([^"]+)"', html) + re.findall(r'url\(["\']?([^)"\']+)', html)
                if re.match(r"(?i)^(https?:)?//", u)]
    check(9, "page references no third-party URL", external, [])
    check(9, "page asks browsers for no Referer", 'name="referrer" content="no-referrer"' in html, True)

    # 11. text inside a reply cannot draw controls that type into the terminal
    # The behaviour (hostile HTML in a reply drawing a clickable fake card) needs a browser, so it is
    # checked by hand with a headless browser; these static checks catch the settings being removed.
    # SYNC WARNING: the strings matched below are copied from index.html (PURIFY settings, NONCE guards).
    # If you rename them there, change them here; do not delete a check to make the suite pass.
    check(11, "reply sanitizer forbids button, form and input tags", all("'%s'" % t in html for t in ("button", "form", "input")), True)
    check(11, "reply sanitizer drops data-* attributes", "ALLOW_DATA_ATTR: false" in html, True)
    check(11, "reply sanitizer keeps only language-* classes", "language-[A-Za-z0-9_+-]+" in html, True)
    check(11, "answer buttons and the free-text form need the page's own token", "opt.dataset.n === NONCE" in html and "f.dataset.n !== NONCE" in html, True)

    # 12. a screenshot cannot become a script page on this origin
    tr = srv.Transcript(ROOT / "nonexistent.jsonl")
    for media in ("text/html", "image/svg+xml", "application/javascript", "text/plain", "TEXT/HTML"):
        check(12, "image declared as %s is not kept" % media, tr.add_image({}, {"type": "base64", "media_type": media, "data": "AAAA"}), None)
    check(12, "image declared as image/png is kept", tr.add_image({}, {"type": "base64", "media_type": "image/png", "data": "AAAA"}) is not None, True)

    # 13. what may be typed. A literal backslash-n is two printable characters, but `cmux send` turns it
    # into Enter, so it would submit a second, attacker-chosen line. Invisible characters are refused too.
    for label, text in (("a backslash n", "Yes \\n!curl x | sh"), ("a backslash r", "a\\rb"), ("a backslash t", "a\\tb"),
                        ("a Unicode tag character", "A\U000E0041"), ("a zero-width space", "A\u200bB"),
                        ("a right-to-left override", "A\u202eB"), ("a byte order mark", "\ufeffA"),
                        ("a real newline", "A\nB"), ("a trailing newline", "A\n")):
        check(13, "answer text with %s is refused" % label, srv.answer_text_ok(text), False)
    for text in ("1 \u2192 A", "B", "use the second one, please", "Cafe \u00e9"):
        check(13, "ordinary answer %r is accepted" % text, srv.answer_text_ok(text), True)

    # 14. nothing is typed into a pane whose Claude session is gone
    import tempfile
    gone = {"claude_pid": 2 ** 22 + 12345, "config_dir": tempfile.gettempdir()}
    check(14, "dead Claude pid is not a target", srv.claude_still_there(gone), False)
    check(14, "registration with no pid is not a target", srv.claude_still_there({}), False)
    check(14, "page disables cards when the session has ended", "const openTextId = alive &&" in html, True)
    check(14, "a card types only its number and letter, never the label", "(${o.label})" not in html.split("function decisionHTML")[1].split("pickedLetter")[0], True)

    # 15. free text cannot start a shell escape, a slash command or a bare menu digit, even in disguise
    # (a lone digit selects and confirms an option in a Claude Code permission dialog)
    for label, text in (("a leading !", "!curl x | sh"), ("a leading slash", "/logout"), ("a leading &", "& run it"),
                        ("a leading #", "#note"), ("a leading -", "- item"), ("a lone digit", "1"),
                        ("a full-width !", "\uff01ls"), ("a full-width backslash", "A\uff3cn"),
                        ("a small-form backslash", "A\ufe68n")):
        check(15, "answer text with %s is refused" % label, srv.answer_text_ok(text), False)
    check(15, "a card answer '2 \u2192 B' is accepted", srv.answer_text_ok("2 \u2192 B"), True)
    check(15, "null text is refused", srv.answer_text_ok(None), False)
    check(15, "a JSON null text is a 400, not typed", req("POST", "/api/answer", good, {"s": ghost, "text": None})[0], 400)
    check(15, "negative Content-Length is a 400", req("POST", "/api/answer", dict(good, **{"Content-Length": "-1"}), None)[0] in (400, 403), True)

    # 16. nothing is typed unless Claude says it is idle at its prompt (a dialog turns digits into menu choices)
    import json as _json, os as _os, tempfile as _tf, types as _types
    cfg = Path(_tf.mkdtemp(prefix="reader-status-temp-"))
    (cfg / "sessions").mkdir()
    sent = []
    real_run, real_pane = srv.run_cmux, srv.pane_confirmed
    # Placeholder cmux names for the stub only; built from parts so no real-looking ref sits in the file.
    fs, fw = "%s:%d" % ("surface", 1), "%s:%d" % ("workspace", 1)
    srv.run_cmux = lambda *a, **k: (sent.append(a), _types.SimpleNamespace(returncode=0, stdout="OK %s %s\n" % (fs, fw), stderr=""))[1]
    srv.pane_confirmed = lambda reg: True
    reg = {"claude_pid": _os.getpid(), "config_dir": str(cfg), "surface_ref": fs, "workspace_ref": fw, "surface": fs}
    try:
        for status in ("waiting", "busy", "shell", None, "garbage"):
            (cfg / "sessions" / ("%d.json" % _os.getpid())).write_text(_json.dumps({"status": status}))
            sent.clear()
            ok, _msg = srv.send_answer(reg, "2 \u2192 A")
            check(16, "status %r: nothing typed" % (status,), (ok, len(sent)), (False, 0))
        (cfg / "sessions" / ("%d.json" % _os.getpid())).write_text("not json")
        sent.clear()
        check(16, "unreadable sessions file: nothing typed", (srv.send_answer(reg, "2 \u2192 A")[0], len(sent)), (False, 0))
        (cfg / "sessions" / ("%d.json" % _os.getpid())).write_text(_json.dumps({"status": "idle"}))
        sent.clear()
        ok, _msg = srv.send_answer(reg, "2 \u2192 A")
        check(16, "status 'idle': typed, then Enter", (ok, [a[0] for a in sent]), (True, ["send", "send-key"]))
    finally:
        srv.run_cmux, srv.pane_confirmed = real_run, real_pane
    check(16, "page opens cards only when idle and no tool call is pending", "alive && idle && !pendingTool" in html, True)

    # 17. launch tickets. The page URL never carries the key; a ticket is a private file the launcher
    # writes (never a request to the server, so a program squatting on the port never receives the key),
    # it works once, within 20 seconds, and only for a process running as this user.
    import secrets as _secrets, socket as _socket
    tdir = srv.STATE / "tickets"
    tdir.mkdir(parents=True, exist_ok=True)

    def make_ticket(age=0, link=False):
        t = _secrets.token_urlsafe(24)
        path = tdir / t
        if link:
            other = tdir / (t + "_target")
            other.write_text("x")
            path.symlink_to(other)
        else:
            path.write_text("")
            _os.chmod(str(path), 0o600)
            if age:
                old = time.time() - age
                _os.utime(str(path), (old, old))
        return t

    check(17, "POST /api/ticket no longer exists", req("POST", "/api/ticket", good, {})[0], 404)
    t = make_ticket()
    st, _h, rb = req("GET", "/api/redeem?t=" + t)
    check(17, "a ticket file redeems once and returns the key", (st, _json.loads(rb or b"{}").get("k") == key), (200, True))
    check(17, "the same ticket a second time", req("GET", "/api/redeem?t=" + t)[0], 403)
    check(17, "a ticket older than 20 seconds", req("GET", "/api/redeem?t=" + make_ticket(age=30))[0], 403)
    check(17, "a ticket that is a symbolic link", req("GET", "/api/redeem?t=" + make_ticket(link=True))[0], 403)
    check(17, "a made-up ticket", req("GET", "/api/redeem?t=" + _secrets.token_urlsafe(24))[0], 403)
    check(17, "a ticket with path characters", req("GET", "/api/redeem?t=../key")[0], 403)
    check(17, "a ticket is not a key", req("GET", feed + "&k=" + make_ticket())[0], 403)
    check(17, "redeem from a foreign Origin", req("GET", "/api/redeem?t=" + make_ticket(), {"Origin": "https://attacker.example"})[0], 403)
    # who is on the other end of a loopback connection: real sockets, real lsof
    lsock = _socket.socket(); lsock.bind(("127.0.0.1", 0)); lsock.listen(1)
    csock = _socket.create_connection(lsock.getsockname()); asock, _addr = lsock.accept()
    cport, sport = csock.getsockname()[1], lsock.getsockname()[1]
    check(17, "a connection from this user is recognised as this user", srv.peer_is_this_user(cport, sport), True)
    real_uid = _os.getuid
    _os.getuid = lambda: real_uid() + 1
    try:
        check(17, "a connection from another user is refused", srv.peer_is_this_user(cport, sport), False)
    finally:
        _os.getuid = real_uid
    check(17, "an unknown connection is refused (fails closed)", srv.peer_is_this_user(1, 2), False)
    for so in (csock, asock, lsock):
        so.close()
    launcher = (ROOT / "reader").read_text()
    check(17, "launcher puts a ticket in the page URL, never ?k=", "?t=%s" in launcher and "?k=%s" not in launcher, True)

    # 18. the route is what protects the ticket, so test the route, not only the helper: a second
    # server in this process, built from the same handler, with the same-user check swapped.
    import threading as _th
    from http.server import ThreadingHTTPServer as _HTTPD
    probe = _socket.socket(); probe.bind(("127.0.0.1", 0)); rport = probe.getsockname()[1]; probe.close()
    real_peer = srv.peer_is_this_user
    httpd = _HTTPD(("127.0.0.1", rport), srv.make_handler(srv.Panes(), "route-test-key-" + "x" * 20, rport))
    _th.Thread(target=httpd.serve_forever, daemon=True).start()

    def redeem_on_route(t):
        c = http.client.HTTPConnection("127.0.0.1", rport, timeout=10)
        c.request("GET", "/api/redeem?t=" + t, headers={"Host": "127.0.0.1:%d" % rport})
        r = c.getresponse(); r.read(); c.close()
        return r.status
    try:
        srv.peer_is_this_user = lambda *a: False
        t = make_ticket()
        check(18, "route refuses a ticket when the peer is another user", redeem_on_route(t), 403)
        check(18, "...and the refused ticket was not consumed", (tdir / t).exists(), True)
        srv.peer_is_this_user = lambda *a: True
        check(18, "route accepts the same ticket when the peer is this user", redeem_on_route(t), 200)
        srv.peer_is_this_user = real_peer
        real_lsof = srv.LSOF
        srv.LSOF = "/nonexistent/lsof"
        check(18, "an lsof that cannot run fails closed", srv.peer_is_this_user(cport, sport), False)
        srv.LSOF = real_lsof
        t2 = make_ticket()
        srv.LSOF = "/nonexistent/lsof"
        check(18, "route refuses when lsof cannot run", redeem_on_route(t2), 403)
        srv.LSOF = real_lsof
    finally:
        srv.peer_is_this_user = real_peer
        httpd.shutdown(); httpd.server_close()

    # a ticket name that points at the key file must not delete it, even when the key file is old
    kpath = srv.KEYFILE
    st0 = kpath.stat(); old = time.time() - 60
    _os.utime(str(kpath), (old, old))
    try:
        for name in ("../key", "..%2fkey", "key"):
            req("GET", "/api/redeem?t=" + name)
        check(18, "redeeming '../key' never removes the key file", kpath.exists(), True)
    finally:
        _os.utime(str(kpath), (st0.st_atime, st0.st_mtime))

    # 19. the key is new on every start, and the proof /health gives is not forgeable without it
    import hmac as _hmac, hashlib as _hashlib
    nonce = "n" + _secrets.token_hex(8)
    hs, _hh, hb = req("GET", "/health?n=" + nonce)
    hd = _json.loads(hb or b"{}")
    check(19, "/health proves knowledge of the key for a nonce", hd.get("proof") == _hmac.new(key.encode(), nonce.encode(), _hashlib.sha256).hexdigest(), True)
    check(19, "/health gives no proof without a nonce", "proof" not in _json.loads(req("GET", "/health")[2] or b"{}"), True)
    old_state, old_keyfile = srv.STATE, srv.KEYFILE
    tmp_state = Path(_tf.mkdtemp(prefix="reader-key-temp-"))
    try:
        srv.STATE, srv.KEYFILE = tmp_state, tmp_state / "key"
        k1 = srv.load_key(); k2 = srv.load_key()
        check(19, "a new key on every start", (k1 != k2, len(k1) >= 20, (tmp_state / "key").read_text() == k2), (True, True, True))
        check(19, "key file mode is 0600", oct((tmp_state / "key").stat().st_mode & 0o777), "0o600")
        link = tmp_state.parent / (tmp_state.name + "-link")
        link.symlink_to(tmp_state)
        srv.STATE, srv.KEYFILE = link, link / "key"
        try:
            srv.load_key()
            refused = False
        except SystemExit:
            refused = True
        check(19, "a symlinked state folder is refused", refused, True)
        link.unlink()
    finally:
        srv.STATE, srv.KEYFILE = old_state, old_keyfile

    # 20. the launcher never sends the key anywhere, and does not trust a look-alike server
    import subprocess as _sp, textwrap as _tw
    lport_sock = _socket.socket(); lport_sock.bind(("127.0.0.1", 0)); lport = lport_sock.getsockname()[1]; lport_sock.listen(5)
    lport_sock.settimeout(1.0)
    lhome = Path(_tf.mkdtemp(prefix="reader-launcher-temp-"))
    code = _tw.dedent("""
        import importlib.machinery, importlib.util, os, sys
        sys.dont_write_bytecode = True
        l = importlib.machinery.SourceFileLoader("launcher", sys.argv[1]); sp = importlib.util.spec_from_loader("launcher", l)
        m = importlib.util.module_from_spec(sp); l.exec_module(m)
        t = m.ticket(); print("ticket-len", len(t))
    """)
    r = _sp.run(["/usr/bin/python3", "-I", "-c", code, str(ROOT / "reader")], capture_output=True, text=True,
                env={"READER_PORT": str(lport), "READER_HOME": str(lhome), "PATH": "/usr/bin:/bin", "HOME": str(lhome)}, timeout=20)
    try:
        lport_sock.accept()
        talked = True
    except _socket.timeout:
        talked = False
    check(20, "launcher ticket() makes no network connection at all", (r.returncode, talked), (0, False))
    lport_sock.close()

    # a look-alike /health (right code and pid, no proof) is not "ours"
    class Fake(__import__("http.server").server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            b = _json.dumps({"ok": True, "code": srv.CODE_MTIME, "pid": _os.getpid(), "cmux": True}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    fs = _socket.socket(); fs.bind(("127.0.0.1", 0)); fport = fs.getsockname()[1]; fs.close()
    fake = _HTTPD(("127.0.0.1", fport), Fake); _th.Thread(target=fake.serve_forever, daemon=True).start()
    code2 = _tw.dedent("""
        import importlib.machinery, importlib.util, sys
        sys.dont_write_bytecode = True
        l = importlib.machinery.SourceFileLoader("launcher", sys.argv[1]); sp = importlib.util.spec_from_loader("launcher", l)
        m = importlib.util.module_from_spec(sp); l.exec_module(m)
        h = m.health(); print("answered", bool(h), "ours", m.our_server(h))
    """)
    r2 = _sp.run(["/usr/bin/python3", "-I", "-c", code2, str(ROOT / "reader")], capture_output=True, text=True,
                 env={"READER_PORT": str(fport), "READER_HOME": str(lhome), "PATH": "/usr/bin:/bin", "HOME": str(lhome)}, timeout=20)
    fake.shutdown(); fake.server_close()
    check(20, "a look-alike server with the right code and pid is not ours", r2.stdout.strip(), "answered True ours False")

    print("\n%d of %d checks passed." % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
