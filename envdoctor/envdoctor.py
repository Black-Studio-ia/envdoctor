#!/usr/bin/env python3
"""envdoctor: catch environment-variable drift before it breaks onboarding.

Statically scans source code for environment variable references and compares
them against your ``.env.example`` template (and optionally your local
``.env``), reporting:

* variables used in code but missing from ``.env.example`` (fails CI),
* variables in your local ``.env`` that are missing or empty,
* template entries nothing references any more (advisory by default).

Zero dependencies, offline, read-only. Never prints secret *values* --
only variable *names*.

Supported source languages: Python, JavaScript/TypeScript, Go, Rust,
Ruby, and Shell.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

__version__ = "0.1.0"

# Extensions mapped to the language group whose patterns apply.
EXTENSION_LANGUAGE = {
    ".py": "python",
    ".js": "js",
    ".jsx": "js",
    ".ts": "js",
    ".tsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".sh": "shell",
    ".bash": "shell",
}

DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    ".tox",
    "__pycache__",
    "dist",
    "build",
    "vendor",
    "target",
    ".idea",
    ".vscode",
}

# Binary-file sniffing: skip files containing NUL in the first chunk.
_BINARY_PROBE_BYTES = 8192
# Files larger than this are skipped (documented limitation).
_MAX_FILE_BYTES = 1024 * 1024

# One named group ``var`` per pattern. Kept deliberately conservative
# (quoted literals only) to avoid false positives from dynamic lookups.
# ``(?<!\w)`` guards keep lookalike identifiers (``mygetenv``,
# ``myprocess.env``, ``MYENV``) from matching.
_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "python": [
        re.compile(r"""(?<!\w)os\.environ\s*\[\s*['"](?P<var>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
        re.compile(r"""(?<!\w)os\.environ\.get\s*\(\s*['"](?P<var>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
        re.compile(r"""(?<!\w)os\.getenv\s*\(\s*['"](?P<var>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
        re.compile(r"""(?<!\w)getenv\s*\(\s*['"](?P<var>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
        # ``from os import environ`` style. The ``(?<![\w.])`` guard keeps
        # dotted access (``os.environ`` / ``xos.environ``) on the patterns
        # above so aliased modules do not match here.
        re.compile(r"""(?<![\w.])environ\s*\[\s*['"](?P<var>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
        re.compile(r"""(?<![\w.])environ\.get\s*\(\s*['"](?P<var>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
    ],
    "js": [
        re.compile(r"""(?<!\w)process\.env\.(?P<var>[A-Za-z_][A-Za-z0-9_]*)"""),
        re.compile(r"""(?<!\w)process\.env\[\s*['"](?P<var>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
        # Vite / modern bundler convention.
        re.compile(r"""(?<!\w)import\.meta\.env\.(?P<var>[A-Za-z_][A-Za-z0-9_]*)"""),
        re.compile(r"""(?<!\w)import\.meta\.env\[\s*['"](?P<var>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
    ],
    "go": [
        re.compile(r"""(?<!\w)os\.Getenv\s*\(\s*"(?P<var>[A-Za-z_][A-Za-z0-9_]*)\""""),
        re.compile(r"""(?<!\w)os\.LookupEnv\s*\(\s*"(?P<var>[A-Za-z_][A-Za-z0-9_]*)\""""),
    ],
    "rust": [
        re.compile(r"""(?<!\w)(?:std::)?env::var\s*\(\s*"(?P<var>[A-Za-z_][A-Za-z0-9_]*)\""""),
        re.compile(r"""(?<!\w)env!\s*\(\s*"(?P<var>[A-Za-z_][A-Za-z0-9_]*)\""""),
        re.compile(r"""(?<!\w)option_env!\s*\(\s*"(?P<var>[A-Za-z_][A-Za-z0-9_]*)\""""),
    ],
    "ruby": [
        re.compile(r"""(?<!\w)ENV\s*\[\s*['"](?P<var>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
        re.compile(r"""(?<!\w)ENV\.fetch\s*\(\s*['"](?P<var>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
    ],
    # Braced form only (${VAR} / ${VAR:-default}) to avoid noise from
    # ordinary shell words. $VAR without braces is intentionally ignored.
    "shell": [
        re.compile(r"""\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)(?::?[-=?+][^}]*)?\}"""),
    ],
}

# Backwards-compatible alias (tests and external code may use PATTERNS).
PATTERNS = _PATTERNS

# ``const { A, B: alias, C = "default" } = process.env`` destructuring.
# Each comma-separated entry contributes its leading key; renames (``B`` in
# ``B: alias``) report the template key, spread (``...rest``) is ignored.
_DESTRUCTURE_RE = re.compile(
    r"\{(?P<body>[^{}]*)\}\s*=\s*process\.env(?![\w])")
_DESTRUCTURE_ENTRY_RE = re.compile(
    r"""^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)
        (?:\s*:\s*[A-Za-z_][A-Za-z0-9_]*)?
        (?:\s*=.*)?\s*$""", re.VERBOSE)


def _extract_destructured(body: str, base_offset: int,
                          content: str) -> dict[str, list[int]]:
    """Map destructured env keys to line numbers within *content*."""
    refs: dict[str, list[int]] = {}
    cursor = 0
    for entry in body.split(","):
        match = _DESTRUCTURE_ENTRY_RE.match(entry)
        if not match:
            cursor += len(entry) + 1
            continue
        key = match.group("key")
        # Locate this occurrence after the previous one so repeated keys
        # across entries still resolve to distinct lines.
        pos = content.find(key, base_offset + cursor,
                           base_offset + cursor + len(entry) + 1)
        if pos == -1:
            pos = base_offset
        lineno = content.count("\n", 0, pos) + 1
        refs.setdefault(key, [])
        if lineno not in refs[key]:
            refs[key].append(lineno)
        cursor += len(entry) + 1
    return refs

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXPORT_PREFIX_RE = re.compile(r"^export\s+", re.IGNORECASE)


def parse_dotenv_file(path: str) -> tuple[dict[str, str], list[str]]:
    """Parse a ``.env``-format file.

    Returns ``(values, errors)`` where ``values`` maps KEY -> raw string
    value (quotes stripped) and ``errors`` describes malformed non-comment
    lines as ``"line N: ..."``. Handles ``export KEY=...``, single/double
    quotes, inline comments after unquoted values, and empty values.
    """
    values: dict[str, str] = {}
    errors: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError as exc:
        return {}, [f"cannot read {path}: {exc.strerror or exc}"]
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = _EXPORT_PREFIX_RE.sub("", line).strip()
        if "=" not in line:
            errors.append(f"line {lineno}: no '=' found, skipped")
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not _KEY_RE.match(key):
            errors.append(f"line {lineno}: invalid key {key!r}, skipped")
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # Strip an inline comment only when it is outside quotes and
            # preceded by whitespace ("KEY=val # comment").
            hash_pos = value.find(" #")
            if hash_pos != -1:
                value = value[:hash_pos].rstrip()
        values.setdefault(key, value)
    return values, errors


def _is_binary(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return b"\0" in handle.read(_BINARY_PROBE_BYTES)
    except OSError:
        return True  # unreadable: treat as uninteresting, caller counts skip


def iter_source_files(root: str, exclude_dirs: set[str]) -> tuple[list[str], int]:
    """Walk *root*, returning ``(source_files, skipped_count)``."""
    found: list[str] = []
    skipped = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in place so os.walk does not descend.
        dirnames[:] = sorted(d for d in dirnames if d not in exclude_dirs)
        for name in sorted(filenames):
            ext = os.path.splitext(name)[1].lower()
            if ext not in EXTENSION_LANGUAGE:
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.getsize(full) > _MAX_FILE_BYTES:
                    skipped += 1
                    continue
            except OSError:
                skipped += 1
                continue
            if _is_binary(full):
                skipped += 1
                continue
            found.append(full)
    return found, skipped


def scan_file(path: str, language: str) -> dict[str, list[int]]:
    """Return ``{VAR: [line numbers]}`` referenced in one source file."""
    refs: dict[str, list[int]] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return refs
    for pattern in PATTERNS[language]:
        for match in pattern.finditer(content):
            var = match.group("var")
            lineno = content.count("\n", 0, match.start()) + 1
            refs.setdefault(var, [])
            if lineno not in refs[var]:
                refs[var].append(lineno)
    if language == "js":
        for match in _DESTRUCTURE_RE.finditer(content):
            for var, lines in _extract_destructured(
                    match.group("body"), match.start("body"),
                    content).items():
                refs.setdefault(var, [])
                for lineno in lines:
                    if lineno not in refs[var]:
                        refs[var].append(lineno)
    return refs


def check(
    root: str,
    example_path: str,
    env_paths: list[str],
    exclude_dirs: set[str],
) -> dict:
    """Run the full drift check. Pure orchestration; no I/O besides reads."""
    example_values, example_errors = parse_dotenv_file(example_path)
    example_keys = set(example_values)

    source_files, skipped_files = iter_source_files(root, exclude_dirs)
    occurrences: dict[str, list[str]] = {}
    for path in source_files:
        language = EXTENSION_LANGUAGE[os.path.splitext(path)[1].lower()]
        for var, lines in scan_file(path, language).items():
            rel = os.path.relpath(path, root)
            occurrences.setdefault(var, [])
            for line in sorted(lines):
                label = f"{rel}:{line}"
                if label not in occurrences[var]:
                    occurrences[var].append(label)
    code_vars = set(occurrences)

    missing_from_example = sorted(code_vars - example_keys)
    unused_in_example = sorted(example_keys - code_vars)

    local_missing: list[str] = []
    local_empty: list[str] = []
    local_notes: list[str] = []
    env_checked: list[str] = []
    for env_path in env_paths:
        env_values, env_errors = parse_dotenv_file(env_path)
        if env_errors and not env_values:
            local_notes.append(f"{env_path}: {'; '.join(env_errors)}")
            continue
        env_checked.append(env_path)
        for var in sorted(code_vars):
            if var not in env_values:
                local_missing.append(var)
        for var in sorted(code_vars & set(env_values)):
            if env_values[var] == "":
                local_empty.append(var)
        for err in env_errors:
            local_notes.append(f"{env_path}: {err}")

    return {
        "root": os.path.abspath(root),
        "example": os.path.abspath(example_path),
        "scanned_files": len(source_files),
        "skipped_files": skipped_files,
        "code_vars": sorted(code_vars),
        "example_keys": sorted(example_keys),
        "example_errors": example_errors,
        "occurrences": {k: occurrences[k] for k in sorted(occurrences)},
        "missing_from_example": missing_from_example,
        "unused_in_example": unused_in_example,
        "env_checked": [os.path.abspath(p) for p in env_checked],
        "local_missing": sorted(set(local_missing)),
        "local_empty": sorted(set(local_empty)),
        "local_notes": local_notes,
    }


def evaluate(result: dict, *, fail_on_unused: bool, strict: bool) -> tuple[bool, list[str]]:
    """Decide pass/fail. Returns ``(ok, failing_reasons)``."""
    reasons: list[str] = []
    if result["missing_from_example"]:
        reasons.append(
            f"{len(result['missing_from_example'])} var(s) used in code "
            "but missing from .env.example"
        )
    if result["local_missing"]:
        reasons.append(
            f"{len(result['local_missing'])} var(s) missing from local .env file(s)"
        )
    if fail_on_unused or strict:
        if result["unused_in_example"]:
            reasons.append(
                f"{len(result['unused_in_example'])} template var(s) "
                "not referenced in code"
            )
    if strict and result["local_empty"]:
        reasons.append(
            f"{len(result['local_empty'])} var(s) set but empty in local .env"
        )
    return (not reasons, reasons)


def format_text(result: dict, reasons: list[str], *, show_occurrences: bool = True) -> str:
    """Human-readable report. Never includes secret values, only key names."""
    out: list[str] = []
    out.append(
        f"envdoctor: scanned {result['scanned_files']} file(s), "
        f"found {len(result['code_vars'])} distinct env var(s)"
    )
    if result["example_errors"]:
        out.append("template warnings:")
        for err in result["example_errors"]:
            out.append(f"  ! {err}")
    if result["missing_from_example"]:
        out.append("MISSING from .env.example (used in code, not documented):")
        for var in result["missing_from_example"]:
            out.append(f"  - {var}")
            if show_occurrences:
                locs = result["occurrences"].get(var, [])[:5]
                for loc in locs:
                    out.append(f"      {loc}")
                extra = len(result["occurrences"].get(var, [])) - len(locs)
                if extra > 0:
                    out.append(f"      ... and {extra} more")
    else:
        out.append("OK: every code-referenced variable is documented in .env.example")
    if result["unused_in_example"]:
        out.append("UNUSED template entries (advisory; nothing references them):")
        for var in result["unused_in_example"]:
            out.append(f"  ~ {var}")
    if result["env_checked"]:
        if result["local_missing"]:
            out.append("LOCAL .env missing (present in code, absent locally):")
            for var in result["local_missing"]:
                out.append(f"  - {var}")
        if result["local_empty"]:
            out.append("LOCAL .env empty (advisory unless --strict):")
            for var in result["local_empty"]:
                out.append(f"  ~ {var} (empty value)")
        if not result["local_missing"] and not result["local_empty"]:
            out.append("OK: local .env covers every code-referenced variable")
    for note in result["local_notes"]:
        out.append(f"note: {note}")
    if reasons:
        out.append("FAIL: " + "; ".join(reasons))
    else:
        out.append("PASS: no drift found")
    return "\n".join(out) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="envdoctor",
        description=(
            "Detect environment-variable drift: variables used in source code "
            "but missing from .env.example, and gaps in your local .env. "
            "Read-only; never prints secret values."
        ),
    )
    parser.add_argument("--root", default=".",
                        help="project directory to scan (default: .)")
    parser.add_argument("--example", default=None,
                        help="path to template file (default: <root>/.env.example)")
    parser.add_argument("--env", dest="env_paths", action="append", default=[],
                        help="local .env file to check (repeatable; "
                             "default: <root>/.env if present)")
    parser.add_argument("--no-env", action="store_true",
                        help="skip local .env checks (useful in CI)")
    parser.add_argument("--fail-on-unused", action="store_true",
                        help="fail if the template documents vars nothing uses")
    parser.add_argument("--strict", action="store_true",
                        help="implies --fail-on-unused and also fails on "
                             "empty values in local .env")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="output format (default: text)")
    parser.add_argument("--exclude", action="append", default=[],
                        help="extra directory name to skip (repeatable)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.no_env and args.env_paths:
        parser.error("--env and --no-env are mutually exclusive")

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"envdoctor: error: --root is not a directory: {args.root}",
              file=sys.stderr)
        return 2
    example = (os.path.abspath(args.example)
               if args.example else os.path.join(root, ".env.example"))
    if not os.path.isfile(example):
        print(f"envdoctor: error: template file not found: {example}",
              file=sys.stderr)
        return 2

    env_paths: list[str] = []
    if not args.no_env:
        if args.env_paths:
            for given in args.env_paths:
                resolved = (given if os.path.isabs(given)
                            else os.path.join(root, given))
                if not os.path.isfile(resolved):
                    print(f"envdoctor: error: --env file not found: {given}",
                          file=sys.stderr)
                    return 2
                env_paths.append(resolved)
        else:
            default_env = os.path.join(root, ".env")
            if os.path.isfile(default_env):
                env_paths.append(default_env)

    exclude_dirs = set(DEFAULT_EXCLUDE_DIRS) | set(args.exclude)

    result = check(root, example, env_paths, exclude_dirs)
    ok, reasons = evaluate(result, fail_on_unused=args.fail_on_unused,
                           strict=args.strict)

    if args.format == "json":
        payload = dict(result)
        payload["ok"] = ok
        payload["reasons"] = reasons
        print(json.dumps(payload, indent=2))
    else:
        print(format_text(result, reasons), end="")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
