"""
SARIF 2.1.0 export for prompt injection test results.

Only tests that found something become SARIF results: `vulnerable` (level error
for high severity payloads, warning otherwise) and `review` (level note). Tests that
resisted, were skipped or could not run are not reported.

SARIF results must point at a file. A runtime test has no file of its own, so the
location is a path you choose with --sarif-location. Point it at the file that holds
your system prompt or chat handler, so the alert lands where the fix belongs.
The default is README.md, which always exists.
"""
from __future__ import annotations

import hashlib
import json

from .report import CATEGORY_INFO
from .runner import TestResult

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
INFORMATION_URI = "https://github.com/ElhamBidarigh/securAI-prompt-tester"

_SEVERITY_SCORE = {"high": "8.0", "medium": "5.5", "low": "2.5"}


def _level(r: TestResult) -> str:
    if r.status == "vulnerable":
        return "error" if r.severity == "high" else "warning"
    return "note"


def _security_severity(r: TestResult) -> str:
    return "3.0" if r.status == "review" else _SEVERITY_SCORE.get(r.severity, "5.5")


def to_sarif(target_label: str, results: list[TestResult], tool_version: str,
             location: str = "README.md") -> dict:
    flagged = [r for r in results if r.status in ("vulnerable", "review")]
    rules, rule_index = [], {}
    for r in flagged:
        if r.id in rule_index:
            continue
        rule_index[r.id] = len(rules)
        label, meaning, fix = CATEGORY_INFO[r.category]
        rules.append({
            "id": r.id,
            "name": r.id.replace("-", ""),
            "shortDescription": {"text": r.name},
            "fullDescription": {"text": f"{label}. {meaning}"},
            "help": {"text": fix},
            "defaultConfiguration": {"level": "error" if r.severity == "high" else "warning"},
            "properties": {"tags": ["security", "llm", "prompt-injection", r.category],
                           "security-severity": _SEVERITY_SCORE.get(r.severity, "5.5")},
        })
    out = []
    for r in flagged:
        verb = "The app followed the injected instruction" if r.status == "vulnerable" else \
            "The reply needs a manual check"
        text = f"{r.name}: {verb}."
        if r.evidence:
            text += f" {r.evidence}."
        digest = hashlib.sha256(f"{r.id}|{target_label}".encode("utf-8")).hexdigest()[:32]
        out.append({
            "ruleId": r.id,
            "ruleIndex": rule_index[r.id],
            "level": _level(r),
            "message": {"text": text},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": location.replace("\\", "/")},
                "region": {"startLine": 1},
            }}],
            "partialFingerprints": {"promptTesterFinding/v1": digest},
            "properties": {"category": r.category, "status": r.status,
                           "security-severity": _security_severity(r)},
        })
    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "SecurAI Prompt Tester", "version": tool_version,
                                "informationUri": INFORMATION_URI, "rules": rules}},
            "results": out,
        }],
    }


def to_sarif_json(target_label: str, results: list[TestResult], tool_version: str,
                  location: str = "README.md") -> str:
    return json.dumps(to_sarif(target_label, results, tool_version, location), indent=2, ensure_ascii=False)
