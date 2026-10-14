"""Tests for envdoctor. Run with: python -m unittest discover -s tests -v"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import envdoctor


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


SAMPLE_APP = {
    "app.py": (
        'import os\n'
        'DATABASE_URL = os.environ["DATABASE_URL"]\n'
        'debug = os.getenv("DEBUG", "false")\n'
        'token = os.environ.get("API_TOKEN")\n'
    ),
    "web/index.ts": (
        'const key = process.env.STRIPE_KEY;\n'
        'const hook = process.env["STRIPE_WEBHOOK_SECRET"];\n'
    ),
    "svc/main.go": (
        'package main\nimport "os"\n'
        'var port = os.Getenv("PORT")\n'
        'var host, ok = os.LookupEnv("HOST")\n'
    ),
    "core/lib.rs": 'fn f() { let _ = std::env::var("RUST_LOG"); }\n',
    "job.rb": 'x = ENV["REDIS_URL"]\ny = ENV.fetch("QUEUE", "default")\n',
    "deploy.sh": 'echo "connecting to ${DB_HOST:-localhost}"\n',
    ".env.example": (
        "# template\n"
        "DATABASE_URL=\n"
        "DEBUG=false\n"
        "API_TOKEN=\n"
        "STRIPE_KEY=\n"
        "STRIPE_WEBHOOK_SECRET=\n"
        "PORT=8080\n"
        "HOST=\n"
        "RUST_LOG=info\n"
        "REDIS_URL=\n"
        "QUEUE=\n"
        "DB_HOST=\n"
        'QUOTED="some value"\n'
        "export EXTRA_OK=1\n"
        "EMPTY_VAL=\n"
        "SPACED = spaced value # inline comment\n"
    ),
}


class ParseTests(unittest.TestCase):
    def test_full_featured_template(self):
        with tempfile.NamedTemporaryFile("w", suffix=".example",
                                         delete=False) as tmp:
            tmp.write(SAMPLE_APP[".env.example"])
            path = tmp.name
        try:
            values, errors = envdoctor.parse_dotenv_file(path)
            self.assertEqual(errors, [])
            self.assertEqual(values["DATABASE_URL"], "")
            self.assertEqual(values["QUOTED"], "some value")
            self.assertEqual(values["EXTRA_OK"], "1")
            self.assertEqual(values["SPACED"], "spaced value")
        finally:
            os.unlink(path)

    def test_malformed_lines_reported_not_fatal(self):
        with tempfile.NamedTemporaryFile("w", suffix=".example",
                                         delete=False) as tmp:
            tmp.write("GOOD=1\nNOT A KEY LINE\n9BAD=x\n")
            path = tmp.name
        try:
            values, errors = envdoctor.parse_dotenv_file(path)
            self.assertEqual(values, {"GOOD": "1"})
            self.assertEqual(len(errors), 2)
        finally:
            os.unlink(path)

    def test_missing_file_reports_error(self):
        values, errors = envdoctor.parse_dotenv_file("/nonexistent-xyz/.env")
        self.assertEqual(values, {})
        self.assertTrue(errors)


class ScanTests(unittest.TestCase):
    def test_each_language_pattern(self):
        cases = {
            ("python", 'os.environ["PY_A"]\nos.getenv("PY_B")\n'
             'os.environ.get("PY_C")\n'): {"PY_A", "PY_B", "PY_C"},
            ("js", "process.env.JS_A\nprocess.env['JS_B']\n"): {"JS_A", "JS_B"},
            ("go", 'os.Getenv("GO_A")\nos.LookupEnv("GO_B")\n'): {"GO_A", "GO_B"},
            ("rust", 'std::env::var("RS_A")\nenv!("RS_B")\n'): {"RS_A", "RS_B"},
            ("ruby", 'ENV["RB_A"]\nENV.fetch("RB_B")\n'): {"RB_A", "RB_B"},
            ("shell", "echo ${SH_A:-x} ${SH_B}\n"): {"SH_A", "SH_B"},
        }
        for (language, code), expected in cases.items():
            with tempfile.NamedTemporaryFile("w", suffix=".txt",
                                             delete=False) as tmp:
                tmp.write(code)
                path = tmp.name
            try:
                refs = envdoctor.scan_file(path, language)
                self.assertEqual(set(refs), expected, language)
            finally:
                os.unlink(path)

    def test_dynamic_lookups_ignored(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                         delete=False) as tmp:
            tmp.write('key = "DYN_" + suffix\nos.environ[key]\n')
            path = tmp.name
        try:
            self.assertEqual(envdoctor.scan_file(path, "python"), {})
        finally:
            os.unlink(path)

    def test_lookalike_identifiers_ignored(self):
        cases = [
            ("python", "mygetenv('LIB_LIKE')\n"),
            ("python", "xos.environ['XOS_FAKE']\n"),
            ("js", "myprocess.env.FAKE_ONE\n"),
            ("js", "x = processX.env.FAKE_TWO\n"),
            ("go", "myos.Getenv(\"GO_FAKE\")\n"),
            ("rust", "myenv::var(\"RS_FAKE\")\n"),
            ("ruby", "MYENV[\"RB_FAKE\"]\n"),
        ]
        for language, code in cases:
            with tempfile.NamedTemporaryFile("w", suffix=".txt",
                                             delete=False) as tmp:
                tmp.write(code)
                path = tmp.name
            try:
                self.assertEqual(envdoctor.scan_file(path, language), {},
                                 f"{language}: {code.strip()}")
            finally:
                os.unlink(path)

    def test_from_os_import_environ(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                         delete=False) as tmp:
            tmp.write("from os import environ\n"
                      "a = environ['BARE_SUB']\n"
                      "b = environ.get(\"BARE_GET\")\n")
            path = tmp.name
        try:
            refs = envdoctor.scan_file(path, "python")
            self.assertEqual(set(refs), {"BARE_SUB", "BARE_GET"})
        finally:
            os.unlink(path)

    def test_js_destructuring(self):
        code = ("const { DES_A, DES_B: alias, DES_C = \"dflt\" } = process.env;\n"
                "const {\n  DES_MULTI,\n  ...rest\n} = process.env;\n")
        with tempfile.NamedTemporaryFile("w", suffix=".js",
                                         delete=False) as tmp:
            tmp.write(code)
            path = tmp.name
        try:
            refs = envdoctor.scan_file(path, "js")
            self.assertEqual(set(refs),
                             {"DES_A", "DES_B", "DES_C", "DES_MULTI"})
            self.assertNotIn("rest", refs)
            self.assertNotIn("alias", refs)
            self.assertEqual(refs["DES_A"], [1])
            self.assertEqual(refs["DES_MULTI"], [3])
        finally:
            os.unlink(path)

    def test_js_import_meta_env(self):
        with tempfile.NamedTemporaryFile("w", suffix=".ts",
                                         delete=False) as tmp:
            tmp.write("const a = import.meta.env.VITE_A;\n"
                      "const b = import.meta.env['VITE_B'];\n")
            path = tmp.name
        try:
            refs = envdoctor.scan_file(path, "js")
            self.assertEqual(set(refs), {"VITE_A", "VITE_B"})
        finally:
            os.unlink(path)

    def test_rust_option_env(self):
        with tempfile.NamedTemporaryFile("w", suffix=".rs",
                                         delete=False) as tmp:
            tmp.write('let _ = option_env!("OPT_A");\n')
            path = tmp.name
        try:
            refs = envdoctor.scan_file(path, "rust")
            self.assertEqual(set(refs), {"OPT_A"})
        finally:
            os.unlink(path)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="envdoctor-test-")
        for rel, content in SAMPLE_APP.items():
            write(os.path.join(self.root, rel), content)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_clean_project_passes(self):
        result = envdoctor.check(
            self.root, os.path.join(self.root, ".env.example"), [],
            set(envdoctor.DEFAULT_EXCLUDE_DIRS))
        self.assertEqual(result["missing_from_example"], [])
        self.assertEqual(result["scanned_files"], 6)
        ok, _ = envdoctor.evaluate(result, fail_on_unused=False, strict=False)
        self.assertTrue(ok)

    def test_missing_var_detected(self):
        with open(os.path.join(self.root, "app.py"), "a",
                   encoding="utf-8") as handle:
            handle.write('x = os.getenv("BRAND_NEW_VAR")\n')
        result = envdoctor.check(
            self.root, os.path.join(self.root, ".env.example"), [],
            set(envdoctor.DEFAULT_EXCLUDE_DIRS))
        self.assertEqual(result["missing_from_example"], ["BRAND_NEW_VAR"])
        ok, reasons = envdoctor.evaluate(
            result, fail_on_unused=False, strict=False)
        self.assertFalse(ok)
        self.assertTrue(reasons)
        text = envdoctor.format_text(result, reasons)
        self.assertIn("BRAND_NEW_VAR", text)
        self.assertIn("FAIL", text)

    def test_unused_is_advisory_by_default_but_fails_on_flag(self):
        with open(os.path.join(self.root, ".env.example"), "a",
                   encoding="utf-8") as handle:
            handle.write("NOBODY_USES_THIS=1\n")
        result = envdoctor.check(
            self.root, os.path.join(self.root, ".env.example"), [],
            set(envdoctor.DEFAULT_EXCLUDE_DIRS))
        self.assertIn("NOBODY_USES_THIS", result["unused_in_example"])
        ok, _ = envdoctor.evaluate(result, fail_on_unused=False, strict=False)
        self.assertTrue(ok)
        ok2, _ = envdoctor.evaluate(result, fail_on_unused=True, strict=False)
        self.assertFalse(ok2)

    def test_local_env_gaps_and_empties(self):
        write(os.path.join(self.root, ".env"),
              "DATABASE_URL=postgres://localhost/db\nDEBUG=\n")
        result = envdoctor.check(
            self.root, os.path.join(self.root, ".env.example"),
            [os.path.join(self.root, ".env")],
            set(envdoctor.DEFAULT_EXCLUDE_DIRS))
        self.assertIn("STRIPE_KEY", result["local_missing"])
        self.assertIn("DEBUG", result["local_empty"])
        self.assertNotIn("DATABASE_URL", result["local_missing"])
        ok, _ = envdoctor.evaluate(result, fail_on_unused=False, strict=False)
        self.assertFalse(ok)  # local_missing fails even without --strict

    def test_empty_project_with_empty_template(self):
        empty_root = tempfile.mkdtemp(prefix="envdoctor-empty-")
        self.addCleanup(shutil.rmtree, empty_root, True)
        write(os.path.join(empty_root, ".env.example"), "# nothing yet\n")
        write(os.path.join(empty_root, "main.py"), 'print("hello")\n')
        result = envdoctor.check(
            empty_root, os.path.join(empty_root, ".env.example"), [],
            set(envdoctor.DEFAULT_EXCLUDE_DIRS))
        ok, _ = envdoctor.evaluate(result, fail_on_unused=False, strict=False)
        self.assertTrue(ok)

    def test_excluded_dirs_not_scanned(self):
        write(os.path.join(self.root, "node_modules", "dep.js"),
              "process.env.SHOULD_BE_IGNORED\n")
        write(os.path.join(self.root, ".venv", "lib.py"),
              'os.getenv("ALSO_IGNORED")\n')
        result = envdoctor.check(
            self.root, os.path.join(self.root, ".env.example"), [],
            set(envdoctor.DEFAULT_EXCLUDE_DIRS))
        self.assertNotIn("SHOULD_BE_IGNORED", result["code_vars"])
        self.assertNotIn("ALSO_IGNORED", result["code_vars"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="envdoctor-cli-")
        for rel, content in SAMPLE_APP.items():
            write(os.path.join(self.root, rel), content)
        self.script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "envdoctor.py")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_cli(self, *args):
        proc = subprocess.run(
            [sys.executable, self.script, *args],
            capture_output=True, text=True, cwd=self.root)
        return proc

    def test_cli_pass_clean(self):
        proc = self.run_cli("--root", self.root, "--no-env")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PASS", proc.stdout)

    def test_cli_fail_on_drift(self):
        with open(os.path.join(self.root, "app.py"), "a",
                   encoding="utf-8") as handle:
            handle.write('os.getenv("MISSING_ONE")\n')
        proc = self.run_cli("--root", self.root, "--no-env")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("MISSING_ONE", proc.stdout)
        # Values must never leak: template has values but output shows keys.
        self.assertNotIn("postgres", proc.stdout)

    def test_cli_json_valid(self):
        proc = self.run_cli("--root", self.root, "--no-env",
                            "--format", "json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("DATABASE_URL", payload["code_vars"])

    def test_cli_missing_template_exit_2(self):
        proc = self.run_cli("--root", self.root, "--example",
                            os.path.join(self.root, "nope.example"))
        self.assertEqual(proc.returncode, 2)

    def test_cli_env_and_no_env_conflict_exit_2(self):
        proc = self.run_cli("--root", self.root, "--no-env",
                            "--env", ".env.example")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("mutually exclusive", proc.stderr)

    def test_values_never_printed(self):
        write(os.path.join(self.root, ".env"),
              "API_TOKEN=super-secret-value-12345\n")
        with open(os.path.join(self.root, "app.py"), "a",
                   encoding="utf-8") as handle:
            handle.write('os.getenv("MISSING_TWO")\n')
        proc = self.run_cli("--root", self.root)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("super-secret-value-12345", proc.stdout)
        self.assertNotIn("super-secret-value-12345", proc.stderr)


if __name__ == "__main__":
    unittest.main()
