"""Command-line interface: serve, chat, doctor, search, tailor, track, eval."""

from __future__ import annotations

import argparse
import sys
import time

from jobpilot import __version__
from jobpilot.bootstrap import bootstrap, bootstrap_workspace
from jobpilot.core.auth import AuthStore, User
from jobpilot.core.config import get_settings
from jobpilot.core.models import ApplicationStatus, JobQuery
from jobpilot.core.user_context import set_current_user
from jobpilot.harness.loop import AgentRunner
from jobpilot.harness.permissions import get_broker
from jobpilot.services.doctor import get_doctor
from jobpilot.services.matching import get_matching
from jobpilot.services.profile import ProfileService
from jobpilot.services.tailoring import get_tailoring
from jobpilot.services.tracking import get_tracking


def _settings():
    return get_settings()


def _select_user(identifier: str = "", *, required: bool = True) -> User | None:
    """Resolve the account a CLI command should operate on.

    With one account on the server it is used automatically. With several, the
    caller must pass ``--user`` (email or id) so a command never guesses.
    """
    bootstrap()
    store = AuthStore()
    if identifier:
        user = store.get(identifier) or store.get_by_email(identifier)
        if user is None:
            raise SystemExit(f"No account matches {identifier!r}.")
    else:
        users = store.list_users()
        if not users:
            if required:
                raise SystemExit(
                    "No accounts yet. Run `jobpilot serve`, open the URL and register first."
                )
            return None
        if len(users) > 1:
            listing = ", ".join(user.email for user in users)
            raise SystemExit(f"Multiple accounts exist ({listing}). Pass --user EMAIL.")
        user = users[0]
    set_current_user(user.id)
    bootstrap_workspace(user.id)
    return user


def cmd_serve(args: argparse.Namespace) -> int:
    from jobpilot.ui.app import create_app

    settings = _settings()
    host = args.host or settings.host
    port = args.port or settings.port
    app = create_app(settings)
    url = f"http://{host}:{port}"
    print(f"JobPilot {__version__} ({settings.mode}) -> {url}")
    if not settings.is_live:
        print(
            "Setup required: add OPENAI_API_KEY to .env, then restart. Open the URL for instructions."
        )
    if args.open:
        import webbrowser

        webbrowser.open(url)
    if args.debug:
        app.run(host=host, port=port, debug=True, use_reloader=False, threaded=True)
        return 0
    from waitress import serve

    serve(app, host=host, port=port, threads=8, ident="JobPilot")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    _select_user(getattr(args, "user", ""), required=False)
    report = get_doctor().run(probe_sources=args.sources, check_mcp=args.mcp)
    print(f"JobPilot {report.version} | mode={report.mode}")
    failures = 0
    for check in report.checks:
        marker = "OK  " if check.ok else "FAIL"
        latency = f" ({check.latency_ms} ms)" if check.latency_ms else ""
        print(f"[{marker}] {check.name}: {check.detail}{latency}")
        failures += 0 if check.ok else 1
    return 0 if failures == 0 else 1


def cmd_chat(args: argparse.Namespace) -> int:
    user = _select_user(args.user)
    settings = _settings()
    if not settings.is_live:
        print("OPENAI_API_KEY is not configured. Add it to .env and restart.")
        return 1
    assert user is not None
    profile = ProfileService().get()
    print(f"JobPilot {__version__} | {settings.mode} mode | {user.email}")
    if not profile.full_name:
        print("Your profile is empty. Fill it in on the Profile page before ranking jobs.")
    print("Type your request. Commands: /exit, /new, /doctor")
    session_id = ""
    while True:
        try:
            message = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not message:
            continue
        if message in {"/exit", "/quit"}:
            break
        if message == "/new":
            session_id = ""
            print("Started a new session.")
            continue
        if message == "/doctor":
            cmd_doctor(argparse.Namespace(sources=False, mcp=False, user=args.user))
            continue

        runner = AgentRunner(user_id=user.id)
        print("jobpilot> ", end="", flush=True)
        for event in runner.stream(session_id, message):
            if event.type == "status":
                print(f"\n  | {event.data.get('text')}", end="", flush=True)
            elif event.type == "token":
                print(event.data.get("text", ""), end="", flush=True)
            elif event.type == "tool_call":
                print(
                    f"\n  -> {event.data.get('name')}({event.data.get('arguments')})",
                    end="",
                    flush=True,
                )
            elif event.type == "tool_result":
                state = "ok" if event.data.get("ok") else "failed"
                print(
                    f"  <- {event.data.get('name')} [{state}] {event.data.get('summary')}",
                    end="",
                    flush=True,
                )
            elif event.type == "approval_required":
                data = event.data
                print(f"\n\n  APPROVAL NEEDED: {data.get('tool')} {data.get('arguments')}")
                answer = input("  Approve? [y/N] ").strip().lower()
                get_broker().resolve(data["id"], answer in {"y", "yes"})
            elif event.type == "error":
                print(f"\n  ! {event.data.get('text')}", end="", flush=True)
            elif event.type == "done":
                session_id = event.data.get("session_id", session_id)
                print(
                    f"\n  ({event.data.get('steps')} steps | ${event.data.get('cost_usd', 0):.4f})"
                )
        print()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    _select_user(args.user)
    query = JobQuery(query=args.query, location=args.location, remote=args.remote, limit=args.limit)
    print("Fetching and ranking jobs...")
    started = time.time()
    ranked = get_matching().rank_jobs(query, limit=args.limit, refresh=not args.no_refresh)
    print(f"{len(ranked)} matches in {time.time() - started:.1f}s\n")
    for index, item in enumerate(ranked, 1):
        job = item.job
        print(
            f"{index:2}. {job.title} - {job.company} | {job.location} | {item.match.score:.0f}/100"
        )
        print(f"    {job.id}")
        if item.match.matched_skills:
            print(f"    matched: {', '.join(item.match.matched_skills[:6])}")
        if item.match.missing_skills:
            print(f"    gaps:    {', '.join(item.match.missing_skills[:6])}")
    return 0


def cmd_tailor(args: argparse.Namespace) -> int:
    _select_user(args.user)
    result = get_tailoring().tailor_cv(args.job_id, use_llm=not args.no_llm)
    print(result.model_dump_json(indent=2))
    print(f"\nSaved: {result.cv_path}")
    return 0


def cmd_track(args: argparse.Namespace) -> int:
    _select_user(args.user)
    application = get_tracking().add(job_id=args.job_id, status=args.status, notes=args.notes)
    print(
        f"{application.id}: {application.job_title} at {application.company} [{application.status}]"
    )
    return 0


def cmd_eval(_args: argparse.Namespace) -> int:
    from jobpilot.evals.run_eval import REPORT_DIR, run_eval

    report = run_eval()
    print("\nOffline evaluation")
    for name, passed in report["checks"].items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print(f"\nOverall: {'PASS' if report['passed'] else 'FAIL'}")
    print(f"Report: {REPORT_DIR / 'latest.md'}")
    return 0 if report["passed"] else 1


def _add_user_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--user",
        default="",
        help="Account email or id. Required when the server has more than one account.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobpilot", description="MCP-powered job search agent")
    parser.add_argument("--version", action="version", version=f"jobpilot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the Flask web app")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--debug", action="store_true")
    serve.add_argument("--open", action="store_true")
    serve.set_defaults(func=cmd_serve)

    doctor = sub.add_parser("doctor", help="Check configuration, sources and MCP servers")
    doctor.add_argument("--sources", action="store_true", help="Probe every job source live")
    doctor.add_argument("--mcp", action="store_true", help="Start the MCP servers and count tools")
    _add_user_argument(doctor)
    doctor.set_defaults(func=cmd_doctor)

    chat = sub.add_parser("chat", help="Chat with the agent in the terminal")
    _add_user_argument(chat)
    chat.set_defaults(func=cmd_chat)

    search = sub.add_parser("search", help="Fetch and rank jobs")
    search.add_argument("query", nargs="?", default="AI Engineer")
    search.add_argument("--location", default="")
    search.add_argument("--remote", action="store_true", default=None)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--no-refresh", action="store_true")
    _add_user_argument(search)
    search.set_defaults(func=cmd_search)

    tailor = sub.add_parser("tailor", help="Tailor a CV for a stored job")
    tailor.add_argument("job_id")
    tailor.add_argument("--no-llm", action="store_true")
    _add_user_argument(tailor)
    tailor.set_defaults(func=cmd_tailor)

    track = sub.add_parser("track", help="Add a job to the tracker")
    track.add_argument("job_id")
    track.add_argument(
        "--status", choices=[status.value for status in ApplicationStatus], default="saved"
    )
    track.add_argument("--notes", default="")
    _add_user_argument(track)
    track.set_defaults(func=cmd_track)

    eval_parser = sub.add_parser("eval", help="Run the offline evaluation harness")
    eval_parser.set_defaults(func=cmd_eval)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
