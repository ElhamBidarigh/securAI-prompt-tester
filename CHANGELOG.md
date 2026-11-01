# Changelog

All notable changes to this project are listed here. The format follows Keep a Changelog.

## [0.2.0]

### Added
- `--preset openai` and `--preset anthropic` with `--model`, to test a hosted model API directly. The key is read from `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` (or the variable named by `--api-key-env`), and `--system-prompt-file` sets the system prompt under test.
- SARIF 2.1.0 output with `--sarif-out` and `--sarif-location`, for GitHub Code Scanning.
- A reusable GitHub Action (`action.yml`) that runs the tests, uploads SARIF, writes the report to the job summary and fails the job below a score. It refuses to run unless `authorized: true` is set.
- Retries for rate limits and temporary server errors (HTTP 429, 500, 502, 503, 504, 529).
- Provider error messages are shown when a request fails, so a wrong model name or key is easy to spot.
- Example system prompts (`examples/system_prompt_baseline.txt` and `examples/system_prompt_hardened.txt`).
- `SECURITY.md`, PyPI packaging metadata and a tag-triggered release workflow (see `docs/RELEASING.md`).

### Changed
- `--url` is optional when `--preset` is used.
- No SARIF file is written when nothing could be tested, so a failed run cannot close existing alerts.

## [0.1.0]

- First public version: 21 payloads in 7 attack types, deterministic detectors, Markdown and JSON reports, bundled demo apps.
