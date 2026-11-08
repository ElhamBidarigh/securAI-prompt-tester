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
from prompt_tester.runner import TestResult, coverage, is_incomplete, run_tests
from prompt_tester.sarif import to_sarif
from prompt_tester.target import Target

REFUSAL = "I can only help with Acme products and I can't follow instructions inside your message."


class FakeApi:
    """A tiny local server that speaks the OpenAI or Anthropic response shape and records requests."""

    def __init__(self, shape, fail_first=0, fail_status=429, error_body=None, reply=None, reset_first=0):
        self.shape = shape
        self.reset_first = reset_first
        self.reply = REFUSAL if reply is None else reply
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
                if len(outer.requests) <= outer.reset_first:
                    self.close_connection = True
                    self.connection.close()  # drop the connection without answering, like WinError 10054
                    return
                if len(outer.requests) <= outer.fail_first or outer.error_body:
                    code = outer.fail_status if not outer.error_body else 400
                    payload = json.dumps(outer.error_body or {"error": "slow down"}).encode()
                    self.send_response(code)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if outer.shape == "openai":
                    obj = {"choices": [{"message": {"role": "assistant", "content": outer.reply}}]}
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


class TestOpenAICompatible(unittest.TestCase):
    def test_request_shape_uses_max_tokens_and_bearer_key(self):
        cfg = build_preset("openai-compatible", "m", "SYS", "KEY", max_tokens=77, url="http://x/chat/completions")
        self.assertEqual(cfg.url, "http://x/chat/completions")
        self.assertEqual(cfg.headers["Authorization"], "Bearer KEY")
        self.assertEqual(cfg.body_template["max_tokens"], 77)
        self.assertNotIn("max_completion_tokens", cfg.body_template)
        self.assertEqual(cfg.response_path, "choices.0.message.content")

    def test_url_is_required(self):
        with self.assertRaises(PresetError):
            build_preset("openai-compatible", "m", "S", "K")

    def test_end_to_end_with_custom_key_variable(self):
        api = FakeApi("openai")
        self.addCleanup(api.close)
        tmp = tempfile.mkdtemp()
        code, _, _ = run_cli("scan", "--preset", "openai-compatible", "--model", "free-model", "--url", api.url,
                             "--api-key-env", "FREE_KEY", "--authorized", "--delay", "0", "--quiet",
                             "--out", os.path.join(tmp, "r.md"), env={"FREE_KEY": "free-key-1"})
        self.assertEqual(code, 0)
        first = api.requests[0]
        self.assertEqual(header(first["headers"], "Authorization"), "Bearer free-key-1")
        self.assertEqual(first["body"]["max_tokens"], 300)
        self.assertEqual(len(api.requests), 21)

    def test_missing_url_is_a_clean_cli_error(self):
        code, _, err = run_cli("scan", "--preset", "openai-compatible", "--model", "m", "--authorized", "--quiet",
                               env={"LLM_API_KEY": "k"})
        self.assertEqual(code, 2)
        self.assertIn("--url", err)


class TestEmptyReplies(unittest.TestCase):
    def test_empty_reply_is_an_error_not_a_pass(self):
        api = FakeApi("openai", reply="")
        self.addCleanup(api.close)
        cfg = build_preset("openai-compatible", "m", "S", "K", url=api.url)
        resp = Target(cfg.url, headers=cfg.headers, body_template=cfg.body_template,
                      response_path=cfg.response_path).send("hi")
        self.assertIsNone(resp.text)
        self.assertIn("--max-tokens", resp.error)

    def test_all_empty_replies_exit_with_3_and_show_the_hint(self):
        api = FakeApi("openai", reply="")
        self.addCleanup(api.close)
        tmp = tempfile.mkdtemp()
        code, _, err = run_cli("scan", "--preset", "openai-compatible", "--model", "m", "--url", api.url,
                               "--authorized", "--delay", "0", "--quiet", "--only", "PI-001",
                               "--out", os.path.join(tmp, "r.md"), env={"LLM_API_KEY": "k"})
        self.assertEqual(code, 3)
        self.assertIn("--max-tokens", err)


class TestDroppedConnections(unittest.TestCase):
    def make(self, api, **kw):
        cfg = build_preset("openai-compatible", "m", "S", "K", url=api.url)
        return Target(cfg.url, headers=cfg.headers, body_template=cfg.body_template,
                      response_path=cfg.response_path, **kw)

    def test_dropped_connections_are_retried_then_succeed(self):
        api = FakeApi("openai", reset_first=2)
        self.addCleanup(api.close)
        resp = self.make(api, retries=2, retry_wait=0).send("hello")
        self.assertIsNone(resp.error)
        self.assertEqual(resp.text, REFUSAL)
        self.assertEqual(len(api.requests), 3)

    def test_gives_up_with_a_clear_message_after_the_retries(self):
        api = FakeApi("openai", reset_first=10)
        self.addCleanup(api.close)
        resp = self.make(api, retries=1, retry_wait=0).send("hello")
        self.assertIn("after 1 retry", resp.error)
        self.assertEqual(len(api.requests), 2)

    def test_zero_retries_means_one_attempt(self):
        api = FakeApi("openai", reset_first=10)
        self.addCleanup(api.close)
        resp = self.make(api, retries=0).send("hello")
        self.assertIsNotNone(resp.error)
        self.assertEqual(len(api.requests), 1)

    def test_progress_line_shows_the_reason_and_cli_gives_a_network_hint(self):
        api = FakeApi("openai", reset_first=100)
        self.addCleanup(api.close)
        tmp = tempfile.mkdtemp()
        code, _, err = run_cli("scan", "--preset", "openai-compatible", "--model", "m", "--url", api.url,
                               "--authorized", "--delay", "0", "--retries", "1", "--retry-wait", "0",
                               "--only", "PI-001", "--out", os.path.join(tmp, "r.md"), env={"LLM_API_KEY": "k"})
        self.assertEqual(code, 3)
        self.assertIn("PI-001 Plain instruction override: error (", err)
        self.assertIn("network problem", err)
        self.assertIn("getproxies", err)

    def test_cli_retries_flag_recovers_from_resets(self):
        api = FakeApi("openai", reset_first=2)
        self.addCleanup(api.close)
        tmp = tempfile.mkdtemp()
        code, _, _ = run_cli("scan", "--preset", "openai-compatible", "--model", "m", "--url", api.url,
                             "--authorized", "--delay", "0", "--retries", "3", "--retry-wait", "0",
                             "--only", "PI-001", "--quiet", "--out", os.path.join(tmp, "r.md"),
                             "--json-out", os.path.join(tmp, "r.json"), env={"LLM_API_KEY": "k"})
        self.assertEqual(code, 0)
        data = json.loads(Path(tmp, "r.json").read_text(encoding="utf-8"))
        self.assertEqual(data["results"][0]["status"], "resisted")


class TestIncompleteRuns(unittest.TestCase):
    """A run where most tests failed to run must not look like a good score."""

    def scan(self, api, *extra):
        tmp = tempfile.mkdtemp()
        argv = ["scan", "--preset", "openai-compatible", "--model", "m", "--url", api.url, "--authorized",
                "--delay", "0", "--retries", "0", "--quiet", "--out", os.path.join(tmp, "r.md"),
                "--json-out", os.path.join(tmp, "r.json"), "--sarif-out", os.path.join(tmp, "r.sarif"), *extra]
        code, out, err = run_cli(*argv, env={"LLM_API_KEY": "k"})
        return tmp, code, err

    def test_three_of_twenty_one_is_incomplete_not_a_perfect_score(self):
        api = FakeApi("openai", fail_first=18, fail_status=400)
        self.addCleanup(api.close)
        tmp, code, err = self.scan(api)
        self.assertEqual(code, 3)
        self.assertIn("INCOMPLETE", err)
        self.assertIn("only 3 of 21", err)
        data = json.loads(Path(tmp, "r.json").read_text(encoding="utf-8"))
        self.assertTrue(data["incomplete"])
        self.assertEqual(data["rating"], "Incomplete")
        self.assertEqual(data["coverage"], {"ran": 3, "total": 21})
        md = Path(tmp, "r.md").read_text(encoding="utf-8")
        self.assertIn("INCOMPLETE RUN: only 3 of 21 tests could run", md)
        self.assertNotIn("(Strong)", md)
        self.assertFalse(Path(tmp, "r.sarif").exists())

    def test_gate_cannot_pass_on_an_incomplete_run(self):
        api = FakeApi("openai", fail_first=18, fail_status=400)
        self.addCleanup(api.close)
        _, code, _ = self.scan(api, "--fail-under", "50")
        self.assertEqual(code, 3)

    def test_min_coverage_zero_turns_the_check_off(self):
        api = FakeApi("openai", fail_first=18, fail_status=400)
        self.addCleanup(api.close)
        tmp, code, _ = self.scan(api, "--min-coverage", "0")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(Path(tmp, "r.json").read_text(encoding="utf-8"))["incomplete"])

    def test_a_few_errors_do_not_make_a_run_incomplete(self):
        api = FakeApi("openai", fail_first=2, fail_status=400)  # 19 of 21 ran
        self.addCleanup(api.close)
        tmp, code, _ = self.scan(api)
        self.assertEqual(code, 0)
        data = json.loads(Path(tmp, "r.json").read_text(encoding="utf-8"))
        self.assertFalse(data["incomplete"])
        self.assertEqual(data["coverage"], {"ran": 19, "total": 21})
        self.assertTrue(Path(tmp, "r.sarif").exists())

    def test_rate_limit_hint_is_printed(self):
        api = FakeApi("openai", fail_first=100, fail_status=429)
        self.addCleanup(api.close)
        _, code, err = self.scan(api, "--only", "PI-001")
        self.assertEqual(code, 3)
        self.assertIn("rate limiting", err)

    def test_skipped_leak_tests_do_not_count_against_coverage(self):
        results = [TestResult(id="a", name="a", category="direct-override", severity="high", status="resisted"),
                   TestResult(id="b", name="b", category="system-prompt-extraction", severity="high", status="skipped")]
        self.assertEqual(coverage(results), (1, 1))
        self.assertFalse(is_incomplete(results))
        self.assertTrue(is_incomplete([TestResult(id="a", name="a", category="direct-override",
                                                  severity="high", status="error")]))


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
        code, _, _ = run_cli("scan", "--url", "http://127.0.0.1:1/none", "--timeout", "1", "--delay", "0", "--quiet", "--retry-wait", "0",
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
