#!/usr/bin/env python3
"""Leak scanner: fails if the repo contains anything that looks personal or secret.

WHY THIS EXISTS
  This code started life on one person's machine, full of real account names, email
  addresses and home-folder paths in its comments. They were removed before the first
  public commit. This scanner is what keeps them (and anyone else's) from coming back:
  it runs before every push (scripts/pre_push_hook.sh) and can be run by hand.

TWO KINDS OF RULES
  1. Generic rules, below. They need no configuration: real-looking email addresses,
     /Users/<name> paths, token shapes, UUIDs, phone numbers, long random strings.
  2. A PRIVATE word list, one case-insensitive regex per line, read from
       $COCKPIT_SCAN_BANNED, else ~/.config/claude_cockpit_publish/banned_strings.txt
     WHY it is not in the repo: the list of words you must never publish is itself
     made of those words. If the file is absent the private rules are skipped, unless
     --require-private is passed, in which case a missing list is a hard failure
     (a maintainer's hook must never pass just because its word list went missing).
  3. An optional PRIVATE allow-list, one exact string per line (not a regex), read from
       $COCKPIT_SCAN_ALLOWED, else allowed_strings.txt next to the word list.
     Each listed string is cut out of a line before the private words are checked, and
     only then. WHY: the repo's own public address (github.com/<account>/...) and the
     account's no-reply commit address can contain words that are banned everywhere
     else, such as the maintainer's own name. The allow-list exempts those exact
     strings and nothing around them. The generic rules never see the allow-list.

A CHECK THAT CANNOT FAIL PROVES NOTHING
  --self-test plants one known-bad file in a temp folder and requires the scanner to
  flag it. Run it before trusting a clean result.

Usage:
  python3 scripts/scan_personal.py                 scan the working tree
  python3 scripts/scan_personal.py --history       also scan every commit (git log -p --all)
  python3 scripts/scan_personal.py --self-test     prove the scanner can fail
  python3 scripts/scan_personal.py --require-private
Exit: 0 clean, 1 hits, 2 could not run as asked.
"""
import math, os, re, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}

# Domains that are placeholders by definition, so an address there is not a leak.
# users.noreply.github.com and anthropic.com cover commit trailers and no-reply authors.
OK_EMAIL_DOMAINS = ("example.com", "example.org", "test.invalid", "users.noreply.github.com", "anthropic.com")

GENERIC = [
    ("home-path", re.compile(r"/Users/(?!you\b|Shared\b)[A-Za-z0-9._-]+")),
    ("token", re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
                         r"|pk_[0-9]{6,}_[A-Z0-9]{8,}|sb_secret_[A-Za-z0-9_-]{8,}|xox[bpa]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{16}")),
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("phone", re.compile(r"\+1[0-9]{10}\b|\([0-9]{3}\) ?[0-9]{3}-[0-9]{4}|\b[0-9]{3}-[0-9]{3}-[0-9]{4}\b")),
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
# The exact shape of a Subresource Integrity value: the algorithm, then base64 of the right length.
# A longer or shorter string that merely starts with "sha384-" does not match and is still scanned.
SRI = re.compile(r"sha256-[A-Za-z0-9+/]{43}=|sha384-[A-Za-z0-9+/]{64}|sha512-[A-Za-z0-9+/]{86}==")
QUOTED = re.compile(r"""['"]([A-Za-z0-9+/_=-]{40,})['"]""")


def entropy(s):
    return -sum((s.count(c) / len(s)) * math.log2(s.count(c) / len(s)) for c in set(s))


def load_private(require):
    path = os.environ.get("COCKPIT_SCAN_BANNED") or os.path.expanduser("~/.config/claude_cockpit_publish/banned_strings.txt")
    if not os.path.isfile(path):
        if require:
            print("scan_personal: --require-private was given but the word list is missing: %s" % path)
            sys.exit(2)
        return [], None
    rules = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            rules.append(re.compile(line, re.I))
    return rules, path


def load_allowed(banned_path):
    path = os.environ.get("COCKPIT_SCAN_ALLOWED") or (
        os.path.join(os.path.dirname(banned_path), "allowed_strings.txt") if banned_path else "")
    if not path or not os.path.isfile(path):
        return []
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    # Longest first, so a longer allowed string is removed whole before a shorter one
    # that it contains could split it.
    return sorted(out, key=len, reverse=True)


ALLOWED = []


def scan_text(label, text, private):
    hits = []
    for n, line in enumerate(text.split("\n"), 1):
        for name, rx in GENERIC:
            if rx.search(line):
                hits.append((label, n, name, line))
        for m in EMAIL.finditer(line):
            if not m.group(1).lower().endswith(OK_EMAIL_DOMAINS):
                hits.append((label, n, "email", line))
        for m in QUOTED.finditer(line):
            s = m.group(1)
            # A Subresource Integrity value (integrity="sha384-...") is a public fingerprint of a
            # file, printed in the page on purpose so browsers can check it. It is not a secret.
            if SRI.fullmatch(s):
                continue
            # An ordered alphabet (a character whitelist) is long but not random.
            if entropy(s) > 4.2 and "abcdefghij" not in s and "ABCDEFGHIJ" not in s:
                hits.append((label, n, "random-string", line))
        checked = line
        for a in ALLOWED:
            checked = checked.replace(a, " ")
        for rx in private:
            if rx.search(checked):
                hits.append((label, n, "private-word", line))
    return hits


def scan_tree(root, private):
    hits = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            p = os.path.join(dp, f)
            try:
                text = open(p, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue        # binary or unreadable: nothing textual to leak
            hits += scan_text(os.path.relpath(p, root), text, private)
    return hits


def scan_history(root, private):
    # --all + -p + author/committer lines: a leak can live in an old commit, in a
    # commit message, or in the author field, none of which the working tree shows.
    r = subprocess.run(["git", "-C", root, "log", "--all", "-p", "--format=commit %H%nAuthor: %an <%ae>%nCommitter: %cn <%ce>%n%B"],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print("scan_personal: git log failed (not a git repo yet?): %s" % r.stderr.strip()[:200])
        sys.exit(2)
    return scan_text("<git history>", r.stdout, private)


def report(hits):
    for label, n, name, line in hits:
        print("%s:%d  [%s]  %s" % (label, n, name, line.strip()[:160]))
    print("\n%d hit(s)" % len(hits))


def self_test():
    with tempfile.TemporaryDirectory() as d:
        # Built from pieces on purpose: written out whole, these lines would be
        # flagged in THIS file every time the real scan runs.
        canary = ("contact someone" + "@" + "real-company.io\n"
                  "path /Us" + "ers/somebody/.claude\nword CANARYWORD\n")
        open(os.path.join(d, "canary.txt"), "w").write(canary)
        got = {name for _, _, name, _ in scan_tree(d, [re.compile("canaryword", re.I)])}
    want = {"email", "home-path", "private-word"}
    ok = want <= got
    # The allow-list must exempt the exact string and nothing else on the line.
    ALLOWED[:] = ["CANARYWORD-public"]
    rx = [re.compile("canaryword", re.I)]
    exempt = not scan_text("t", "url CANARYWORD-public", rx)
    still_caught = bool(scan_text("t", "url CANARYWORD-public and CANARYWORD", rx))
    ALLOWED[:] = []
    print("self-test: allow-list exempts only the exact string -> %s" % ("PASS" if exempt and still_caught else "FAIL"))
    ok = ok and exempt and still_caught
    print("self-test: planted %s, scanner flagged %s -> %s" % (sorted(want), sorted(got), "PASS (the scanner can fail)" if ok else "FAIL"))
    return 0 if ok else 2


def main():
    args = set(sys.argv[1:])
    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)
    if "--self-test" in args:
        sys.exit(self_test())
    private, path = load_private("--require-private" in args)
    ALLOWED[:] = load_allowed(path)
    print("generic rules: %d + email + random-string   private word list: %s" % (
        len(GENERIC), ("%d rules from %s" % (len(private), path)) if path else "not present (skipped)"))
    if ALLOWED:
        print("allowed exact strings: %d" % len(ALLOWED))
    hits = scan_tree(ROOT, private)
    if "--history" in args:
        hits += scan_history(ROOT, private)
    report(hits)
    sys.exit(1 if hits else 0)


if __name__ == "__main__":
    main()
