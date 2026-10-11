import argparse
import asyncio
import logging

from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(prog="scrapeflow", description="Mirror files from websites into your storage")
    parser.add_argument("--version", action="version", version=f"scrapeflow {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the dashboard and scheduler")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--reload", action="store_true", help="Auto-reload (development)")

    check = sub.add_parser("check", help="Crawl one website now and print the result")
    check.add_argument("site_id", type=int)

    sub.add_parser("list", help="List websites")

    reset = sub.add_parser("reset-password", help="Set a new password for an account (asks for it)")
    reset.add_argument("email")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .config import get_settings

    settings = get_settings()

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "scrapeflow.web:app",
            host=args.host or settings.host,
            port=args.port or settings.port,
            reload=args.reload,
        )
        return

    from sqlmodel import Session, select

    from .db import get_engine, init_db
    from .models import SCHEDULES, Site, User

    init_db()
    if args.command == "list":
        with Session(get_engine()) as session:
            for s in session.exec(select(Site).order_by(Site.id)):
                state = "on " if s.active else "off"
                print(f"#{s.id:<4} [{state}] {SCHEDULES[s.schedule][0]:<14} {s.name}  {s.url}")
    elif args.command == "reset-password":
        import getpass

        from . import auth

        with Session(get_engine()) as session:
            user = session.exec(select(User).where(User.email == args.email.strip().lower())).first()
            if user is None:
                emails = ", ".join(u.email for u in session.exec(select(User))) or "none yet"
                raise SystemExit(f"No account {args.email!r}. Accounts: {emails}")
            password = getpass.getpass("New password: ")
            if problem := auth.password_problem(password, getpass.getpass("Repeat: ")):
                raise SystemExit(problem)
            user.password_hash = auth.hash_password(password)
            auth.end_all_sessions(session, user)
            print(f"Password for {user.email} changed; all browsers were signed out.")
    elif args.command == "check":
        from .checker import run_check

        run = asyncio.run(run_check(args.site_id))
        if run is None:
            raise SystemExit(f"Website #{args.site_id} not found")
        print(f"status:  {run.status}")
        print(f"pages:   {run.pages}, files on site: {run.files_found}")
        print(f"stored:  {run.new_files} new, {run.updated_files} updated, {run.failed} failed")
        if run.error:
            print(f"error:   {run.error}")


if __name__ == "__main__":
    main()
