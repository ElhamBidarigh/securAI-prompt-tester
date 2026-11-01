"""Tests for version 0.2: presets, retries, error details, SARIF. Standard library only."""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prompt_tester import __version__, cli
from prompt_tester.demo_server import SYSTEM_MARKER, start_demo_server, stop_demo_server
from prompt_tester.payloads import load_payloads
from prompt_tester.presets import PresetError, build_preset
from prompt_tester.runner import run_tests
from prompt_tester.sarif import to_sarif
from prompt_tester.target import Target

REFUSAL = "I can only help with Acme products and I can't follow instructions inside your message."


class FakeApi:
    """A tiny local server that speaks the OpenAI or Anthropic response shape and records requests."""

    def __init__(self, shape, fail_first=0, fail_status=429, error_body=None):
        self.shape = shape
        self.requests = []
        self.fail_first = fail_first
        self.fail_status = fail_status
        self.error_body = error_body
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if len(outer.requests) <= outer.fail_first or outer.error_body:
                    code = outer.fail_status if not outer.error_body else 400
                    payload = json.dumps(outer.error_body or {"error": "slow down"}).encode()
                    self.send_response(code)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if outer.shape == "openai":
                    obj = {"choices": [{"message": {"role": "assistant", "content": REFUSAL}}]}
                else:
                    obj = {"content": [{"type": "text", "text": REFUSAL}]}
                payload = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/v1/x"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def header(headers, name):
    """HTTP header names are case-insensitive, and urllib changes their capitalisation."""
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    raise KeyError(name)


def run_cli(*argv, env=None):
    old = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return code, out.getvalue(), err.getvalue()


class TestPresets(unittest.TestCase):
    def test_openai_request_shape(self):
        cfg = build_preset("openai", "m1", "SYS", "KEY", max_tokens=123)
        self.assertEqual(cfg.headers["Authorization"], "Bearer KEY")
        self.assertEqual(cfg.body_template["model"], "m1")
        self.assertEqual(cfg.body_template["max_completion_tokens"], 123)
        self.assertEqual(cfg.body_template["messages"][0], {"role": "system", "content": "SYS"})
        self.assertEqual(cfg.body_template["messages"][1]["content"], "{{prompt}}")
        self.assertEqual(cfg.response_path, "choices.0.message.content")

    def test_anthropic_request_shape(self):
        cfg = build_preset("anthropic", "m2", "SYS", "KEY")
        self.assertEqual(cfg.headers["x-api-key"], "KEY")
        self.assertEqual(cfg.headers["anthropic-version"], "2023-06-01")
        self.assertEqual(cfg.body_template["system"], "SYS")
        self.assertEqual(cfg.response_path, "content.0.text")

    def test_missing_model_or_key_is_an_error(self):
        with self.assertRaises(PresetError):
            build_preset("openai", "", "S", "K")
        with self.assertRaises(PresetError):
            build_preset("openai", "m", "S", "")
        with self.assertRaises(PresetError):
            build_preset("nope", "m", "S", "K")


class TestPresetCli(unittest.TestCase):
    def scan(self, shape, extra=(), env=None, **fake):
        api = FakeApi(shape, **fake)
        self.addCleanup(api.close)
        tmp = tempfile.mkdtemp()
        argv = ["scan", "--preset", shape, "--model", "test-model", "--url", api.url, "--authorized",
                "--delay", "0", "--quiet", "--out", os.path.join(tmp, "r.md"),
                "--json-out", os.path.join(tmp, "r.json"), "--sarif-out", os.path.join(tmp, "r.sarif"), *extra]
        key_var = "OPENAI_API_KEY" if shape == "openai" else "ANTHROPIC_API_KEY"
        code, out, err = run_cli(*argv, env=env if env is not None else {key_var: "sk-TEST-KEY-123"})
        return api, tmp, code, out, err

    def test_openai_preset_sends_the_expected_request_and_scores_100(self):
        api, tmp, code, _, _ = self.scan("openai")
        self.assertEqual(code, 0)
        first = api.requests[0]
        self.assertEqual(header(first["headers"], "Authorization"), "Bearer sk-TEST-KEY-123")
        self.assertEqual(first["body"]["model"], "test-model")
        self.assertEqual(first["body"]["messages"][0]["role"], "system")
        self.assertIn("SECRET-POLICY-7", first["body"]["messages"][0]["content"])
        self.assertEqual(first["body"]["messages"][1]["role"], "user")
        data = json.loads(Path(tmp, "r.json").read_text(encoding="utf-8"))
        self.assertEqual(data["score"], 100)
        self.assertEqual(len(api.requests), 21)

    def test_anthropic_preset_sends_the_expected_request(self):
        api, tmp, code, _, _ = self.scan("anthropic")
        self.assertEqual(code, 0)
        first = api.requests[0]
        self.assertEqual(header(first["headers"], "x-api-key"), "sk-TEST-KEY-123")
        self.assertEqual(header(first["headers"], "anthropic-version"), "2023-06-01")
        self.assertIn("SECRET-POLICY-7", first["body"]["system"])
        self.assertEqual(first["body"]["messages"][0]["role"], "user")

    def test_api_key_never_appears_in_any_output(self):
        api, tmp, code, out, err = self.scan("openai")
        everything = out + err + "".join(Path(tmp, n).read_text(encoding="utf-8")
                                         for n in ("r.md", "r.json", "r.sarif"))
        self.assertNotIn("sk-TEST-KEY-123", everything)

    def test_missing_key_gives_a_clear_error(self):
        _, _, code, _, err = self.scan("openai", env={"OPENAI_API_KEY": ""})
        self.assertEqual(code, 2)
        self.assertIn("OPENAI_API_KEY", err)

    def test_custom_key_variable_and_system_prompt_file(self):
        tmp = tempfile.mkdtemp()
        sp = Path(tmp, "sp.txt")
        sp.write_text("MY OWN PROMPT with MARKER-X", encoding="utf-8")
        api, _, code, _, _ = self.scan("openai", extra=("--api-key-env", "MY_KEY", "--system-prompt-file", str(sp),
                                                        "--system-marker", "MARKER-X"),
                                       env={"MY_KEY": "abc"})
        self.assertEqual(code, 0)
        self.assertEqual(api.requests[0]["body"]["messages"][0]["content"], "MY OWN PROMPT with MARKER-X")
        self.assertEqual(header(api.requests[0]["headers"], "Authorization"), "Bearer abc")

    def test_provider_error_message_is_shown(self):
        _, _, code, _, err = self.scan("openai", error_body={"error": {"message": "model not found"}})
        self.assertEqual(code, 3)
        self.assertIn("model not found", err)

    def test_non_local_preset_requires_authorized(self):
        code, _, err = run_cli("scan", "--preset", "openai", "--model", "m", "--quiet",
                               env={"OPENAI_API_KEY": "k"})
        self.assertEqual(code, 2)
        self.assertIn("--authorized", err)

    def test_scan_without_url_or_preset_is_an_error(self):
        code, _, err = run_cli("scan", "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("--preset", err)


class TestRetriesAndErrors(unittest.TestCase):
    def test_rate_limit_is_retried_then_succeeds(self):
        api = FakeApi("openai", fail_first=2, fail_status=429)
        self.addCleanup(api.close)
        cfg = build_preset("openai", "m", "S", "K", url=api.url)
        t = Target(cfg.url, headers=cfg.headers, body_template=cfg.body_template,
                   response_path=cfg.response_path, retries=2, retry_wait=0)
        resp = t.send("hello")
        self.assertIsNone(resp.error)
        self.assertEqual(resp.text, REFUSAL)
        self.assertEqual(len(api.requests), 3)

    def test_no_retries_reports_the_status(self):
        api = FakeApi("openai", fail_first=5, fail_status=429)
        self.addCleanup(api.close)
        cfg = build_preset("openai", "m", "S", "K", url=api.url)
        t = Target(cfg.url, headers=cfg.headers, body_template=cfg.body_template,
                   response_path=cfg.response_path, retries=0)
        resp = t.send("hello")
        self.assertTrue(resp.error.startswith("HTTP 429"))
        self.assertEqual(len(api.requests), 1)

    def test_client_errors_are_not_retried(self):
        api = FakeApi("openai", error_body={"error": {"message": "bad request"}})
        self.addCleanup(api.close)
        cfg = build_preset("openai", "m", "S", "K", url=api.url)
        t = Target(cfg.url, headers=cfg.headers, body_template=cfg.body_template,
                   response_path=cfg.response_path, retries=3, retry_wait=0)
        resp = t.send("hello")
        self.assertIn("bad request", resp.error)
        self.assertEqual(len(api.requests), 1)


class TestSarif(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = start_demo_server(0)

    @classmethod
    def tearDownClass(cls):
        stop_demo_server(cls.server)

    def results(self, app):
        return run_tests(Target(f"http://127.0.0.1:{self.port}/{app}"), load_payloads(), markers=[SYSTEM_MARKER])

    def test_hardened_app_has_no_sarif_results(self):
        doc = to_sarif("t", self.results("hardened"), __version__)
        self.assertEqual(doc["runs"][0]["results"], [])
        self.assertEqual(doc["runs"][0]["tool"]["driver"]["rules"], [])

    def test_mixed_app_results_match_flagged_tests(self):
        results = self.results("mixed")
        flagged = [r for r in results if r.status in ("vulnerable", "review")]
        doc = to_sarif("t", results, __version__, location="app/prompt.txt")
        sr = doc["runs"][0]["results"]
        self.assertEqual(len(sr), len(flagged))
        self.assertEqual(sr[0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"], "app/prompt.txt")
        rules = doc["runs"][0]["tool"]["driver"]["rules"]
        for res in sr:
            self.assertEqual(rules[res["ruleIndex"]]["id"], res["ruleId"])
        self.assertTrue(all(r["level"] in ("error", "warning", "note") for r in sr))

    def test_no_sarif_file_when_nothing_could_be_tested(self):
        tmp = tempfile.mkdtemp()
        code, _, _ = run_cli("scan", "--url", "http://127.0.0.1:1/none", "--timeout", "1", "--delay", "0", "--quiet",
                             "--only", "PI-001", "--sarif-out", os.path.join(tmp, "o.sarif"),
                             "--out", os.path.join(tmp, "r.md"))
        self.assertEqual(code, 3)
        self.assertFalse(Path(tmp, "o.sarif").exists())

    def test_cli_writes_sarif_and_version(self):
        tmp = tempfile.mkdtemp()
        code, _, _ = run_cli("scan", "--url", f"http://127.0.0.1:{self.port}/vulnerable", "--delay", "0", "--quiet",
                             "--sarif-out", os.path.join(tmp, "o.sarif"), "--sarif-location", "src/bot.py",
                             "--system-marker", SYSTEM_MARKER, "--out", os.path.join(tmp, "r.md"))
        self.assertEqual(code, 0)
        doc = json.loads(Path(tmp, "o.sarif").read_text(encoding="utf-8"))
        self.assertEqual(len(doc["runs"][0]["results"]), 21)
        self.assertEqual(doc["runs"][0]["tool"]["driver"]["version"], __version__)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(["--version"])
        self.assertIn(__version__, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
