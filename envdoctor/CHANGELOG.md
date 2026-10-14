# Changelog

## Unreleased (pre-publication review fixes, 2026-10-14)

- Fixed false positives from lookalike identifiers (`mygetenv`, `myprocess.env`, `MYENV`, `xos.environ`, `myenv::var`) via word-boundary guards.
- Added detection: JS destructuring (`const { A } = process.env`), Vite `import.meta.env`, Rust `option_env!`, Python `from os import environ` forms.
- `--env` combined with `--no-env` is now a usage error (exit 2) instead of silently ignoring `--env`.
- README: qualified the differentiation claim against `envsniff` and `envcheck-sync`; documented framework-helper, Dockerfile, and aliased-import gaps.

## 0.1.0 — 2026-10-14

Initial release.

- Static env-var reference scan for Python, JavaScript/TypeScript, Go, Rust,
  Ruby, and Shell (stdlib only, offline, read-only).
- Three-way drift report: code vs `.env.example` (missing fails),
  local `.env` coverage (missing fails, empty advisory),
  unused template entries (advisory).
- `--fail-on-unused`, `--strict`, `--format json`, `--no-env`,
  `--exclude`, `--example`, `--env` flags; exit codes 0/1/2.
- Never prints secret values (only key names); covered by a dedicated test.
- 16-test stdlib `unittest` suite; CI workflow with self-check.
