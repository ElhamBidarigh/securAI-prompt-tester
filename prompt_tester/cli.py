"""
Command line interface.

  prompt-tester scan --url URL [options]     test your own LLM app
  prompt-tester demo                         run against the bundled demo apps
  prompt-tester serve-demo                   start the demo apps on your machine
  prompt-tester list-payloads                show the built-in payloads
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .demo_server import DEMO_APPS, SYSTEM_MARKER, start_demo_server, stop_demo_server
from .payloads import CATEGORIES, load_payloads
from .report import to_json, to_markdown
from .runner import counts, rating, run_tests, score
from .target import Target, TargetError, is_local_url


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="prompt-tester",
        description="Quick prompt injection resistance check for LLM-powered apps. Only test apps you own or have permission to test.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="Test an LLM app over HTTP.")
    s.add_argument("--url", required=True, help="Endpoint of the app, for example https://myapp.com/api/chat")
    s.add_argument("--method", default="POST", choices=["POST", "GET"])
    s.add_argument("--header", action="append", default=[], metavar="'Name: value'",
                   help="Extra HTTP header, for example 'Authorization: Bearer TOKEN'. Repeatable.")
    s.add_argument("--body-template", default=None,
                   help='JSON body with the placeholder {{prompt}}. Default: {"message": "{{prompt}}"}')
    s.add_argument("--body-template-file", default=None, help="Same as --body-template, read from a file.")
    s.add_argument("--query-param", default=None, help="For GET targets: query parameter that carries the prompt.")
    s.add_argument("--response-path", default=None,
                   help="Dotted path to the reply text in the JSON response, for example choices.0.message.content. Auto-detected if omitted.")
    s.add_argument("--system-marker", action="append", default=[], metavar="TEXT",
                   help="A phrase that appears in your real system prompt. Enables the leak tests. Repeatable.")
    s.add_argument("--category", action="append", default=[], choices=CATEGORIES, help="Only run these categories. Repeatable.")
    s.add_argument("--only", action="append", default=[], metavar="PI-001", help="Only run these payload ids. Repeatable.")
    s.add_argument("--payloads", default=None, help="Path to your own payload JSON file instead of the built-in set.")
    s.add_argument("--repeat", type=int, default=1, help="Send each payload this many times (models vary). Default 1.")
    s.add_argument("--delay", type=float, default=0.5, help="Seconds to wait between requests. Default 0.5.")
    s.add_argument("--timeout", type=float, default=30.0, help="Seconds before a request times out. Default 30.")
    s.add_argument("--authorized", action="store_true",
                   help="Confirm you own this app or have written permission to test it. Required for non-local URLs.")
    s.add_argument("--out", default=None, help="Write the Markdown report to this file.")
    s.add_argument("--json-out", default=None, help="Write the JSON report to this file.")
    s.add_argument("--client-name", default="")
    s.add_argument("--consultant-name", default="")
    s.add_argument("--consultant-contact", default="")
    s.add_argument("--fail-under", type=int, default=None, metavar="SCORE",
                   help="Exit with code 1 if the score is below this number (for CI).")
    s.add_argument("--quiet", action="store_true", help="No progress lines.")

    d = sub.add_parser("demo", help="Run the tester against the bundled demo apps and write sample reports.")
    d.add_argument("--out-dir", default="examples", help="Where to write the sample reports. Default: examples")
    d.add_argument("--client-name", default="Acme Shop (demo app)")
    d.add_argument("--consultant-name", default="")
    d.add_argument("--consultant-contact", default="")

    sd = sub.add_parser("serve-demo", help="Start the demo apps so you can point 'scan' at them.")
    sd.add_argument("--port", type=int, default=8765)

    sub.add_parser("list-payloads", help="Print the built-in payloads.")
    return p


def _parse_headers(items: list[str]) -> dict:
    headers = {}
    for item in items:
        if ":" not in item:
            raise TargetError(f"bad --header value {item!r}, expected 'Name: value'")
        name, value = item.split(":", 1)
        headers[name.strip()] = value.strip()
    return headers


def _load_template(args) -> object:
    raw = None
    if args.body_template_file:
        raw = Path(args.body_template_file).read_text(encoding="utf-8")
    elif args.body_template:
        raw = args.body_template
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise TargetError(f"the body template is not valid JSON: {e}")


def _progress(quiet: bool):
    if quiet:
        return None

    def show(i, total, r):
        print(f"[{i:>2}/{total}] {r.id} {r.name}: {r.status}", file=sys.stderr)
    return show


def cmd_scan(args) -> int:
    if not is_local_url(args.url) and not args.authorized:
        print("error: this URL is not local. Add --authorized to confirm you own the app or have written "
              "permission to test it.", file=sys.stderr)
        return 2
    try:
        target = Target(args.url, method=args.method, headers=_parse_headers(args.header),
                        body_template=_load_template(args), response_path=args.response_path,
                        query_param=args.query_param, timeout=args.timeout)
        payloads = load_payloads(args.payloads, args.category or None, args.only or None)
    except (TargetError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not payloads:
        print("error: no payloads selected", file=sys.stderr)
        return 2

    results = run_tests(target, payloads, repeat=args.repeat, delay=args.delay,
                        markers=args.system_marker, progress=_progress(args.quiet))
    return _finish(args.url, results, args)


def _finish(label: str, results, args) -> int:
    value = score(results)
    md = to_markdown(label, results, args.client_name, args.consultant_name, args.consultant_contact)
    if getattr(args, "out", None):
        Path(args.out).write_text(md, encoding="utf-8")
    if getattr(args, "json_out", None):
        Path(args.json_out).write_text(to_json(label, results), encoding="utf-8")
    if not getattr(args, "out", None):
        print(md)
    c = counts(results)
    shown = "n/a" if value is None else f"{value}/100"
    print(f"\n[prompt-tester] score {shown} ({rating(value)}), vulnerable={c['vulnerable']} "
          f"review={c['review']} resisted={c['resisted']} skipped={c['skipped']} errors={c['error']}", file=sys.stderr)
    if value is None:
        print("[prompt-tester] nothing could be tested. Check the URL, headers and body template.", file=sys.stderr)
        return 3
    fail_under = getattr(args, "fail_under", None)
    if fail_under is not None and value < fail_under:
        return 1
    return 0


def cmd_demo(args) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    server, port = start_demo_server(0)
    try:
        payloads = load_payloads()
        print(f"{'app':<12} {'score':>5}  rating     vulnerable  review  resisted")
        for app in DEMO_APPS:
            target = Target(f"http://127.0.0.1:{port}/{app}")
            results = run_tests(target, payloads, markers=[SYSTEM_MARKER])
            value = score(results)
            c = counts(results)
            print(f"{app:<12} {value:>5}  {rating(value):<9}  {c['vulnerable']:>10}  {c['review']:>6}  {c['resisted']:>8}")
            label = f"demo app '{app}' (bundled simulation, not a real model)"
            (out_dir / f"sample_report_{app}.md").write_text(
                to_markdown(label, results, args.client_name, args.consultant_name, args.consultant_contact),
                encoding="utf-8")
            (out_dir / f"sample_report_{app}.json").write_text(to_json(label, results), encoding="utf-8")
        print(f"\nReports written to {out_dir}/")
    finally:
        stop_demo_server(server)
    return 0


def cmd_serve_demo(args) -> int:
    server, port = start_demo_server(args.port)
    print(f"Demo apps running on http://127.0.0.1:{port}  (Ctrl+C to stop)")
    for app in DEMO_APPS:
        print(f"  http://127.0.0.1:{port}/{app}")
    print(f"System prompt marker for the leak tests: --system-marker {SYSTEM_MARKER}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_demo_server(server)
    return 0


def cmd_list(_args) -> int:
    for p in load_payloads():
        print(f"{p['id']}  {p['category']:<26} {p['severity']:<7} {p['name']}")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "scan":
        return cmd_scan(args)
    if args.command == "demo":
        return cmd_demo(args)
    if args.command == "serve-demo":
        return cmd_serve_demo(args)
    if args.command == "list-payloads":
        return cmd_list(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
