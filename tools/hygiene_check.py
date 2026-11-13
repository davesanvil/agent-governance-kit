#!/usr/bin/env python3
"""Pre-publish hygiene check for this repository. Stdlib only.

Scans the current content of every text file git tracks, and every commit's metadata, for things that should
not be in a public repository: non-documentation IP addresses, personal email addresses, URLs to hosts outside an
allowlist, home-directory paths, and secret-shaped strings. Commits must use a noreply author and committer email
and carry no co-author or generator trailers. An optional private denylist (kept outside the repo) adds exact
terms of your own.

This is a lint for mechanical leaks. It does not read binary files or the content of older revisions (scan
before every commit and history stays covered), and it cannot judge meaning: read what you publish.

    python3 tools/hygiene_check.py [--denylist PATH] [ROOT]
Exit status: 0 clean, 1 findings, 2 usage error.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOWED_HOSTS = {"localhost", "example.com", "example.org", "example.net", "registry.npmjs.org"}
ALLOWED_EMAIL = re.compile(r"(?:^noreply@github\.com|@(?:users\.noreply\.github\.com|example\.(?:com|org|net)))$", re.I)
DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")]

IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.]*\d)")
IPV6 = re.compile(r"(?<![\w:])(?=[0-9a-f:]*::|(?:[0-9a-f]{1,4}:){7})[0-9a-f:]{3,39}(?![\w:])", re.I)
RULES: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("url", re.compile(r"\b[a-z][a-z0-9+.-]*://\[?([^\s/:\]\"'<>)]+)", re.I)),
    ("home-path", re.compile(r"/home/[a-z_][\w-]*|/Users/[\w-]+|[A-Z]:\\Users\\[\w-]+")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("token", re.compile(r"\b(?:ghp|gho|ghs|github_pat)_\w{20,}|\bsk-[\w-]{20,}|\bxox[abp]-[\w-]{10,}|\bAKIA[0-9A-Z]{16}\b")),
    ("secret", re.compile(r"\b(?:api[_-]?key|token|secret|passw(?:or)?d)\b\s*[:=]\s*['\"]?[\w\-./+]{12,}", re.I)),
]
TRAILER = re.compile(r"^(?:co-authored-by|signed-off-by|generated[- ]with|on-behalf-of)\s*:", re.I | re.M)


@dataclass(frozen=True)
class Finding:
    where: str
    kind: str
    text: str

    def __str__(self) -> str:
        shown = self.text if len(self.text) <= 3 else self.text[:2] + "…"
        return f"{self.where}: {self.kind} ({shown}, {len(self.text)} chars)"


def _public_ip(token: str) -> bool:
    try:
        addr = ipaddress.ip_address(token)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_unspecified:
        return False
    return not any(addr in net for net in DOC_NETS)


def scan_text(text: str, where: str, deny: list[re.Pattern[str]]) -> list[Finding]:
    found: list[Finding] = []
    for n, line in enumerate(text.splitlines(), 1):
        loc = f"{where}:{n}"
        for rx in (IPV4, IPV6):
            for m in rx.finditer(line):
                if _public_ip(m.group(0)) and not (rx is IPV6 and _is_slice(line, m)):
                    found.append(Finding(loc, "ip-address", m.group(0)))
        for kind, rx in RULES:
            for m in rx.finditer(line):
                token = m.group(m.lastindex or 0)
                if kind == "email" and ALLOWED_EMAIL.search(token):
                    continue
                if kind == "url" and (token.lower() in ALLOWED_HOSTS or _ip_literal(token)):
                    continue
                found.append(Finding(loc, kind, token))
        found += [Finding(loc, "denylist", m.group(0)) for rx in deny for m in rx.finditer(line)]
    return found


def _is_slice(line: str, m: re.Match[str]) -> bool:
    """xs[1::2] is indexing, not an address: digits and colons right after `name[`, `)[` or `][`."""
    start = m.start()
    return (
        re.fullmatch(r"[\d:]+", m.group(0)) is not None
        and start >= 2
        and line[start - 1] == "["
        and re.match(r"[\w)\]]", line[start - 2]) is not None
    )


def _ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True  # judged by the ip-address rule instead


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def scan_repo(root: Path, deny: list[re.Pattern[str]]) -> list[Finding]:
    found: list[Finding] = []
    for rel in _git(root, "ls-files", "-z").split("\0"):
        if not rel:
            continue
        found += scan_text(rel, f"{rel}:name", deny)
        data = (root / rel).read_bytes()
        if b"\0" not in data[:8192]:
            found += scan_text(data.decode("utf-8", "replace"), rel, deny)
    log = _git(root, "log", "--all", "--format=%h%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e")
    for record in filter(str.strip, log.split("\x1e")):
        short, an, ae, cn, ce, body = record.strip("\n").split("\x1f", 5)
        where = f"commit {short}"
        found += [Finding(where, "commit-email", e) for e in {ae, ce} if not ALLOWED_EMAIL.search(e)]
        found += [Finding(where, "commit-trailer", m.group(0)) for m in TRAILER.finditer(body)]
        found += scan_text(f"{an}\n{cn}\n{body}", where, deny)
    return found


def load_denylist(path: Path | None) -> list[re.Pattern[str]]:
    if path is None:
        return []
    terms = [t.strip() for t in path.read_text(encoding="utf-8").splitlines()]
    return [re.compile(rf"(?<!\w){re.escape(t)}(?!\w)", re.I) for t in terms if t and not t.startswith("#")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default=".", type=Path)
    ap.add_argument("--denylist", type=Path, help="private file of exact terms, one per line; keep it out of the repo")
    args = ap.parse_args(argv)
    try:
        deny = load_denylist(args.denylist)
        findings = scan_repo(args.root, deny)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for f in findings:
        print(f)
    print(f"{len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
