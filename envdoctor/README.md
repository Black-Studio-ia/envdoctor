# envdoctor

One sentence: **envdoctor statically scans your source code for environment-variable references and tells you which ones are missing from `.env.example` — before a new teammate's first `cp .env.example .env` blows up.**

## The problem

`.env.example` is documentation, and documentation drifts. A developer adds `STRIPE_WEBHOOK_SECRET` to the payments code, the PR gets reviewed for logic, and nobody updates the template — because nothing checks it. Weeks later a new contributor clones the repo, copies the example, runs the app, and it crashes on a variable the file never mentioned. This recurs constantly (see e.g. `future-agi` missing ~30 vars, `riven` shipping wrong var names, `open-wearables` shipping a stale example).

## Why envdoctor instead of …

| Alternative | Friction envdoctor avoids |
|---|---|
| Manual discipline / PR checklists | A policy, not a mechanism — fails exactly when people are busy |
| Schema-based tools (e.g. EnvShield-style `env.schema.toml`) | Requires adopting and maintaining a new schema file |
| Runtime wrappers / validators (`dotenv-safe`, `t3-env`, `envalid`) | Require changing app code and only fail at boot time |
| `.env`↔`.env.example` diff tools (`dotenv-linter diff`, `sync-dotenv`) | Only compare two text files; never look at what the **code** actually uses |
| Heavier code scanners (`envsniff`, `envcheck-sync`) | Real alternatives that also scan code — see note below |

envdoctor's difference: **zero dependencies, zero config, zero code changes** — one stdlib-only Python file that statically scans multi-language source (Python, JS/TS, Go, Rust, Ruby, Shell), needs no network, and exits non-zero so it drops straight into CI or a pre-commit hook.

> **Honest positioning.** envdoctor is not the only code-scanning tool.
> [`envsniff`](https://pypi.org/project/envsniff/) (AST-based, Python/JS/Go/Shell/Docker, can auto-generate `.env.example`, GitHub Action included)
> and [`envcheck-sync`](https://pypi.org/project/envcheck-sync/) (zero-dependency, stdlib-`ast` based, but Python-only)
> cover the same core check — and their AST parsing avoids the comment/string false positives envdoctor's regexes can produce.
> Choose envdoctor when you want the smallest possible footprint (one file, `curl` it into any repo, Python 3.9+, no install step),
> multi-language coverage including Rust and Ruby, a strictly read-only check that never rewrites your template,
> and local-`.env` coverage reporting. Choose an AST-based scanner when Python-only precision matters most.

## Features (all implemented and tested)

- Static scan of `os.environ` / `os.getenv` / `environ` (Python), `process.env` incl. destructuring and `import.meta.env` (JS/TS), `os.Getenv` / `LookupEnv` (Go), `env::var` / `env!` / `option_env!` (Rust), `ENV[]` / `ENV.fetch` (Ruby), `${VAR}` (Shell)
- Reports vars **used in code but missing from `.env.example`** (fails, exit 1)
- Checks your local `.env`: reports **missing** (fails) and **empty** (advisory unless `--strict`) values
- Reports **unused template entries** (advisory unless `--fail-on-unused` / `--strict`)
- `--format json` for CI pipelines; `--no-env` for CI runners with no local `.env`
- Read-only: never modifies files, never prints secret **values** (only names)
- Skips `node_modules`, `.venv`, `.git`, etc.; skips binary files and files > 1 MB

## Requirements

- Python 3.9+ (standard library only — no `pip install` needed)
- Windows, macOS, or Linux

## Installation

Copy one file into your repo (or anywhere on `PATH`):

```sh
curl -O https://raw.githubusercontent.com/Black-Studio-ia/envdoctor/main/envdoctor.py
```

No dependencies. Verify it runs:

```sh
python envdoctor.py --version
```

## Quick start (under 5 minutes)

```sh
cd your-project
python /path/to/envdoctor.py --root . --no-env
```

Example — `app.py` uses `STRIPE_KEY`, but `.env.example` only documents `DATABASE_URL`:

```
envdoctor: scanned 1 file(s), found 2 distinct env var(s)
MISSING from .env.example (used in code, not documented):
  - STRIPE_KEY
      app.py:3
FAIL: 1 var(s) used in code but missing from .env.example
```

Add `STRIPE_KEY=` to `.env.example` and the check passes with exit code 0.

Also check your own machine's `.env` (default: `./.env` if present):

```sh
python envdoctor.py --root .
```

JSON output for pipelines:

```sh
python envdoctor.py --root . --no-env --format json
```

### Pre-commit hook

```yaml
- repo: local
  hooks:
    - id: envdoctor
      name: envdoctor
      entry: python envdoctor.py --root . --no-env
      language: system
      pass_filenames: false
```

### GitHub Actions

See [`.github/workflows/ci.yml`](.github/workflows/ci.yml) — it runs the test suite and an envdoctor self-check on every push.

## Usage

```
usage: envdoctor [--root DIR] [--example FILE] [--env FILE] [--no-env]
                 [--fail-on-unused] [--strict] [--format text|json]
                 [--exclude DIR] [--version]

  --root DIR        project directory to scan (default: .)
  --example FILE    template file (default: <root>/.env.example)
  --env FILE        local .env file to check; repeatable
                    (default: <root>/.env if it exists)
  --no-env          skip local .env checks (useful in CI)
  --fail-on-unused  fail if the template documents vars nothing uses
  --strict          implies --fail-on-unused; also fails on empty
                    values in the local .env
  --format          text (default) or json
  --exclude DIR     extra directory name to skip; repeatable
```

Exit codes: `0` = no drift, `1` = drift found, `2` = usage/config error (e.g. template file missing).

## Common errors

| Message | Fix |
|---|---|
| `error: template file not found` | Pass `--example path/to/.env.example` or run from the project root |
| `error: --env file not found` | Check the path, or omit `--env` to use `./.env` auto-detection |
| `--env` combined with `--no-env` | These are mutually exclusive (exit 2) — drop one |
| `error: --root is not a directory` | Point `--root` at your project directory |
| A var flagged MISSING that is only in a comment | Known limitation (below) — remove the mention or add the key to the template |

## Security and privacy

- **Read-only.** envdoctor never writes, deletes, or executes anything; it only reads source and `.env`-format files.
- **Never prints values.** Reports contain variable *names* and file locations only — safe to paste into CI logs and issues.
- **No network access.** Fully offline; nothing leaves your machine.
- **No dependencies** means no supply-chain surface beyond Python itself.
- Reviewed scope (v0.1.0): unsafe path handling (none — paths are only read), secret leakage (tested: `test_values_never_printed`), code execution (none — regex scan only, no imports of target code). Not audited by a third party; see [SECURITY.md](SECURITY.md).

## Known limitations

- Quoted literal references only: dynamic lookups like `os.environ[prefix + name]` are invisible to any static scanner (documented, tested).
- References inside comments/strings are counted as usage — `unused` detection is heuristic and therefore advisory by default. (AST-based tools such as `envcheck-sync` avoid this; see positioning note above.)
- Framework helpers are not detected: `django-environ`'s `env("X")`, `dynaconf`, `pydantic-settings` field names, Spring `@Value`, etc. — only the direct access forms listed above are scanned.
- `Dockerfile` `ENV`/`ARG` instructions are not scanned (no file extension to key on; use `envsniff` if you need that).
- Aliased imports (`import os as xos`) are not resolved; only the literal `os.` / `environ` forms match.
- Files larger than 1 MB and binary files are skipped (counted in `skipped_files` in JSON output).
- `$VAR` (unbraced) in shell is intentionally not matched to avoid noise; use `${VAR}`.
- No auto-fix: envdoctor reports drift but never edits your files (by design — templates often need human-written comments).

## Running the tests

```sh
python -m unittest discover -s tests -v
```

22 tests, stdlib only. Covers: `.env` parsing (quotes, `export`, inline comments, malformed lines), per-language scan patterns, lookalike-identifier rejection, JS destructuring / `import.meta.env`, Rust `option_env!`, `from os import environ`, clean/drift/unused/local-gap scenarios, empty projects, excluded directories, CLI exit codes (including the `--env`/`--no-env` conflict), JSON validity, and the never-print-values guarantee.

## Contributing

Bug reports and small PRs welcome. Keep it stdlib-only and add a test for every new pattern. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).

## Support this project

If this tool saves you an onboarding headache, you may optionally support its continued development with USDT.

Network: TRC20 (Tron)

USDT address: TXQCrX61CceFxX5gnFg9N3ssZEC7MavwBQ

Please verify the network and address carefully before sending — USDT on the wrong network can be lost permanently. Contributions are voluntary and do not guarantee specific features, services, or financial returns. The software remains fully usable under its MIT license regardless.

Contact: ia.creative.tn@gmail.com
