import bisect
import contextlib
import gzip
import itertools
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TRACES_FILE = Path.home() / ".config" / "hl-guard-dev" / "traces.txt"
ALLOWED_ADDRESSES_FILE = Path(__file__).with_name("allowed_addresses.txt")
ALLOW_MARKER = "check-private-traces: allow"

TELEGRAM_ID = r"(?:-\d{6,13}|\d{6,12})(?!\d)"
NOT_PERSONAL = "".join(
    f"(?<!{word})(?<!{word}[_-])" for word in ("message", "msg", "update", "completion")
)
GENERIC_RULES = {
    "home path": re.compile(r"/(?:users|home)\\?/[\w.-]+"),
    "windows home path": re.compile(r"[a-z]:\\+users\\+[\w.-]+"),
    "telegram id": re.compile(
        rf"(?:chat|telegram|operator)[a-z0-9_-]{{0,40}}?(?=[uic]){NOT_PERSONAL}"
        rf"(?:u?ids?|users?|channels?)[\s\"':=\[_]+{TELEGRAM_ID}"
    ),
}
MULTILINE_RULES = {
    "telegram id": re.compile(
        rf"[\"'](?:chat|from|user|sender_chat)[\"']\s*:\s*\{{[^{{}}]*?[\"']id[\"']\s*:\s*{TELEGRAM_ID}"
    ),
}
LINE_BREAK = re.compile(r"\r\n|\r|\n")
EVM_ADDRESS = re.compile(r"(?<![0-9a-f])0x[0-9a-f]{40}(?![0-9a-f])")
INVISIBLE = dict.fromkeys(map(ord, "­​‌‍⁠﻿"))
WIDE_CODECS = ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")


@dataclass(frozen=True, order=True)
class Hit:
    path: str
    line: int
    rule: str


@dataclass(frozen=True)
class Rules:
    private: list[str]
    allowed_addresses: frozenset[str]

    def match(self, line: str, honor_marker: bool = True) -> set[str]:
        rules = {f"private trace #{i}" for i, t in enumerate(self.private, start=1) if t in line}
        if honor_marker and ALLOW_MARKER in line:
            return rules
        rules |= {rule for rule, rx in GENERIC_RULES.items() if rx.search(line)}
        if any(m not in self.allowed_addresses for m in EVM_ADDRESS.findall(line)):
            rules.add("evm address")
        return rules


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(INVISIBLE).casefold()


def load_lines(path: Path) -> list[str]:
    return [normalize(line.strip()) for line in path.read_text().splitlines() if line.strip()]


def load_private(configured: str | None) -> list[str]:
    if configured is not None:
        path = Path(configured).expanduser()
    elif DEFAULT_TRACES_FILE.is_file():
        path = DEFAULT_TRACES_FILE
    else:
        problem = "is a broken symlink" if DEFAULT_TRACES_FILE.is_symlink() else "does not exist"
        print(
            f"check_private_traces: the private traces file {problem}, generic rules only",
            file=sys.stderr,
        )
        return []
    private = load_lines(path)
    if not private:
        raise ValueError("the file holds no traces")
    return private


def decoded_views(data: bytes) -> list[str]:
    views = [data.decode("utf-8", errors="replace"), data.decode("latin-1")]
    if b"\0" in data or data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        views += [data.decode(codec, errors="replace") for codec in WIDE_CODECS]
    if data[:2] == b"\x1f\x8b":
        with contextlib.suppress(OSError, EOFError):
            views += decoded_views(gzip.decompress(data))
    return views


def file_views(path: str) -> list[str]:
    if os.path.islink(path):
        return [os.readlink(path)]
    return decoded_views(Path(path).read_bytes())


def scan_file(path: str, rules: Rules) -> set[Hit]:
    hits = {Hit(path, 0, rule) for rule in rules.match(normalize(path), honor_marker=False)}
    for view in file_views(path):
        lines = LINE_BREAK.split(normalize(view))
        for number, line in enumerate(lines, start=1):
            hits |= {Hit(path, number, rule) for rule in rules.match(line)}
        text = "\n".join(lines)
        starts = list(itertools.accumulate((len(line) + 1 for line in lines), initial=0))
        marked = {number for number, line in enumerate(lines, start=1) if ALLOW_MARKER in line}
        for rule, rx in MULTILINE_RULES.items():
            for found in rx.finditer(text):
                number = bisect.bisect_right(starts, found.end())
                if number not in marked:
                    hits.add(Hit(path, number, rule))
    return hits


def repo_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted({p for p in out.split("\0") if p and os.path.lexists(p)})


def main(argv: list[str]) -> int:
    try:
        private = load_private(os.environ.get("HL_GUARD_TRACES_FILE"))
    except (OSError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) else str(exc)
        print(
            f"check_private_traces: cannot use the private traces file ({reason}); "
            "fix the file or HL_GUARD_TRACES_FILE",
            file=sys.stderr,
        )
        return 2
    rules = Rules(private, frozenset(load_lines(ALLOWED_ADDRESSES_FILE)))
    paths = argv or repo_files()
    hits: set[Hit] = set()
    for path in paths:
        hits |= scan_file(path, rules)

    named = {hit.path for hit in hits if hit.line == 0}
    for hit in sorted(hits):
        shown = "*" * len(hit.path) if hit.path in named else hit.path
        print(f"{shown}:{hit.line}: {hit.rule}")
    print(
        f"check_private_traces: {len(paths)} files, {len(private)} private traces loaded, "
        f"{len(hits)} hits"
    )
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
