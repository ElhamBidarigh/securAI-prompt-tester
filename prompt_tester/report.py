"""
Report generation. The Markdown report is written for a business owner or
product manager first, and for an engineer second: plain language summary on
top, technical evidence below.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .payloads import CATEGORIES
from .runner import TestResult, counts, rating, score

CATEGORY_INFO = {
    "direct-override": (
        "Direct instruction override",
        "A user types instructions that tell the AI to ignore its rules.",
        "Keep your instructions and the user's text in separate message roles. Never paste user text into the system prompt. "
        "Validate the model's output before using it, and give the model only the permissions it needs.",
    ),
    "role-play": (
        "Persona and role-play tricks",
        "A user convinces the AI to become a different character that ignores the app's rules.",
        "State in the system prompt that the assistant keeps its role no matter what the user says, and check replies "
        "against an allow-list of expected behaviour.",
    ),
    "system-prompt-extraction": (
        "System prompt leakage",
        "A user gets the AI to reveal its hidden instructions.",
        "Assume the system prompt can be read by anyone. Never put secrets, keys or private business rules in it. "
        "Filter replies that quote your own instructions.",
    ),
    "indirect-injection": (
        "Instructions hidden inside content",
        "The AI reads a document, web page, email or record that contains hidden instructions and obeys them. "
        "This is the most dangerous kind for apps that read outside content.",
        "Treat everything the AI reads as data, never as instructions. Wrap outside content in clear delimiters, label it as "
        "untrusted, strip hidden text, and require human confirmation before the AI takes any action based on it.",
    ),
    "encoding": (
        "Disguised instructions",
        "The attack is hidden with encoding or odd spelling so simple filters miss it.",
        "Run safety checks after decoding and normalising the input, not before. Do not rely on keyword filters alone.",
    ),
    "multilingual": (
        "Attacks in other languages",
        "The same override is written in Turkish, Arabic or Persian, where English-only defences often fail.",
        "Make sure your guardrails and filters are tested in every language your users speak, or use a model-based "
        "check that is multilingual.",
    ),
    "output-handling": (
        "Unsafe model output",
        "The AI can be made to produce active content (scripts, or images that leak data) that your app might run or render.",
        "Escape or sanitise all model output before showing it. Do not render images or links from model output without "
        "an allow-list, and use a strict Content Security Policy.",
    ),
}

STATUS_LABEL = {
    "vulnerable": "VULNERABLE",
    "review": "NEEDS REVIEW",
    "resisted": "Resisted",
    "skipped": "Skipped",
    "error": "Could not test",
}


def _clip(text: str, limit: int) -> str:
    text = text.replace("```", "` ` `")
    return text if len(text) <= limit else text[:limit] + " ... (truncated)"


def to_json(target_label: str, results: list[TestResult]) -> str:
    value = score(results)
    return json.dumps({
        "target": target_label,
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "score": value,
        "rating": rating(value),
        "counts": counts(results),
        "results": [r.to_dict() for r in results],
    }, indent=2, ensure_ascii=False)


def to_markdown(target_label: str, results: list[TestResult], client_name: str = "",
                consultant_name: str = "", consultant_contact: str = "") -> str:
    value = score(results)
    c = counts(results)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    L: list[str] = []

    L.append("# Prompt Injection Resistance Report")
    if client_name:
        L.append(f"### Prepared for: {client_name}")
    L.append(f"**Target:** {target_label}  ")
    L.append(f"**Date:** {today}  ")
    if consultant_name:
        who = consultant_name + (f", {consultant_contact}" if consultant_contact else "")
        L.append(f"**Prepared by:** {who}  ")
    L.append("")
    L.append("---")
    L.append("")

    L.append("## Summary")
    L.append("")
    if value is None:
        L.append("**No test could be completed.** See the notes at the end of this report.")
    else:
        L.append(f"**Resistance score: {value}/100 ({rating(value)})**")
        L.append("")
        tested = c["vulnerable"] + c["review"] + c["resisted"]
        L.append(
            f"We sent {tested} safe, well-known attack prompts to the application. "
            f"It resisted {c['resisted']}, clearly fell for {c['vulnerable']}, and gave {c['review']} reply(ies) that need a human look."
        )
        L.append("")
        if c["vulnerable"] == 0 and c["review"] == 0:
            L.append("In plain terms: the app held up against every basic attack we tried. This is a good sign, "
                     "but not proof of safety (see the limits section).")
        elif c["vulnerable"] > 0:
            L.append("In plain terms: a person with no special skills could make this app ignore its own rules. "
                     "The findings below explain what happened and how to fix it.")
        else:
            L.append("In plain terms: the app did not clearly fall for any attack, but some replies need a manual check.")
    L.append("")
    L.append("| Outcome | Count |")
    L.append("|---|---|")
    L.append(f"| Vulnerable | {c['vulnerable']} |")
    L.append(f"| Needs review | {c['review']} |")
    L.append(f"| Resisted | {c['resisted']} |")
    L.append(f"| Skipped | {c['skipped']} |")
    L.append(f"| Could not test | {c['error']} |")
    L.append("")

    L.append("## Results by attack type")
    L.append("")
    L.append("| Attack type | Tested | Resisted | Needs review | Vulnerable |")
    L.append("|---|---|---|---|---|")
    for cat in CATEGORIES:
        rows = [r for r in results if r.category == cat]
        if not rows:
            continue
        label = CATEGORY_INFO[cat][0]
        scored = [r for r in rows if r.status in ("vulnerable", "review", "resisted")]
        L.append(f"| {label} | {len(scored)} | {sum(r.status == 'resisted' for r in rows)} | "
                 f"{sum(r.status == 'review' for r in rows)} | {sum(r.status == 'vulnerable' for r in rows)} |")
    L.append("")

    problems = [r for r in results if r.status in ("vulnerable", "review")]
    problems.sort(key=lambda r: (r.status != "vulnerable", {"high": 0, "medium": 1, "low": 2}.get(r.severity, 3), r.id))
    if problems:
        L.append("## Findings")
        L.append("")
        for i, r in enumerate(problems, 1):
            label, meaning, fix = CATEGORY_INFO[r.category]
            L.append(f"### {i}. [{r.id}] {r.name}")
            L.append("")
            L.append(f"- **Outcome:** {STATUS_LABEL[r.status]}")
            L.append(f"- **Severity if real:** {r.severity.title()}")
            L.append(f"- **Attack type:** {label}")
            if r.runs > 1:
                L.append(f"- **Repeats:** vulnerable in {r.vulnerable_runs} of {r.runs} runs")
            L.append("")
            L.append(f"**What this means:** {meaning}")
            L.append("")
            L.append("**What we sent:**")
            L.append("```")
            L.append(_clip(r.prompt, 600))
            L.append("```")
            L.append("")
            L.append("**What the app replied:**")
            L.append("```")
            L.append(_clip(r.response, 400))
            L.append("```")
            L.append("")
            if r.evidence:
                L.append(f"**Why we flagged it:** {r.evidence}.")
                L.append("")
            L.append(f"**How to fix it:** {fix}")
            L.append("")
            L.append("---")
            L.append("")

    passed = [r for r in results if r.status == "resisted"]
    if passed:
        L.append("## Attacks the app resisted")
        L.append("")
        for r in passed:
            L.append(f"- [{r.id}] {r.name}")
        L.append("")

    notes = [r for r in results if r.status in ("skipped", "error") or r.error]
    if notes:
        L.append("## Notes")
        L.append("")
        for r in notes:
            detail = r.error or ("not run" if r.status == "skipped" else "")
            if r.status == "skipped":
                detail = "skipped, give --system-marker with a phrase from your system prompt to enable"
            L.append(f"- [{r.id}] {r.name}: {detail}")
        L.append("")

    L.append("## Limits of this test")
    L.append("")
    L.append(
        "This report covers a fixed set of basic and well-known prompt injection attacks, checked with exact "
        "string matching rather than by judging meaning. Language models answer differently from one run to the next, "
        "so a different run can produce a different result. A high score means the app handled these attacks, "
        "not that it is safe against a determined attacker. Treat it as a first check, and combine it with a manual "
        "review of what the AI is allowed to do (the tools, data and accounts it can reach)."
    )
    L.append("")
    return "\n".join(L)
