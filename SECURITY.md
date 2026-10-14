# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Design guarantees

- **Read-only tool.** envdoctor only reads files; it never writes, deletes,
  executes, or transmits anything.
- **No secret values in output.** Reports contain variable *names* and
  `file:line` locations only. This is enforced by the
  `test_values_never_printed` test.
- **No network access and no dependencies.** The only code that runs is
  `envdoctor.py` on your machine plus the Python standard library.

## Reporting a vulnerability

Open a GitHub issue with the `security` label, or contact the maintainer
directly. Please do not include real secrets in any report — redact values
and share key names and reproduction steps only.

## Known non-issues

- envdoctor reads your local `.env` *values* into memory to compare key
  presence/emptiness, but never prints, logs, or transmits them.
- Static regex scanning cannot see dynamically constructed variable names
  (e.g. `os.environ[prefix + name]`). This is a documented limitation,
  not a vulnerability.
