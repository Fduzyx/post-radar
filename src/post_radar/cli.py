"""Small command-line interface. Exceptions are rendered as local codes only."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import tempfile
import warnings
from importlib.resources import files
from pathlib import Path
from typing import NoReturn

from post_radar import __version__
from post_radar.config import AppConfig, load_config
from post_radar.credentials import NAMES, WindowsCredentialStore
from post_radar.models import ExternalError


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # Never echo accidentally pasted secrets in invalid command-line arguments.
        self.exit(2, "error: invalid_arguments; use --help\n")


def _parser() -> argparse.ArgumentParser:
    parser = SafeParser(prog="post-radar")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    commands.add_parser("demo", help="Run an offline demonstration using synthetic data")
    init = commands.add_parser("init", help="Write a disabled local configuration template")
    init.add_argument("--config", type=Path, default=Path("config/local.toml"))
    for command in (
        "check",
        "doctor",
        "run",
        "status",
        "retry-delivery",
        "resume-api",
        "resume-mail",
        "resume-source",
    ):
        sub = commands.add_parser(command)
        sub.add_argument("--config", type=Path, default=Path("config/local.toml"))
        if command == "run":
            sub.add_argument("--once", action="store_true")
        elif command == "retry-delivery":
            sub.add_argument("delivery_id", help="Retry an unknown delivery; may send a duplicate")
        elif command == "resume-source":
            sub.add_argument("source_id", help="Resume a source after correcting its configuration")
    secret = commands.add_parser("secret", help="Manage only PostRadar's Windows vault entries")
    secret.add_argument("action", choices=("set", "delete"))
    secret.add_argument("name", choices=sorted(NAMES))
    return parser


async def _run(config: AppConfig, *, once: bool) -> None:
    from post_radar.engine import Engine
    from post_radar.evaluator import ApiEvaluator
    from post_radar.mail import SmtpMailer
    from post_radar.sources import FeedSource
    from post_radar.store import Store

    if not config.api.enabled:
        raise ExternalError("api_disabled", permanent=True)
    if sys.platform != "win32":
        raise ExternalError("credential_store_unavailable", permanent=True)
    if not any(source.enabled for source in config.sources) or not any(
        rule.enabled for rule in config.rules
    ):
        raise ValueError("no_enabled_subscriptions")
    credentials = WindowsCredentialStore()
    store = Store(config.state_dir / "post-radar.sqlite3")
    engine = Engine(
        config,
        store,
        FeedSource(),
        ApiEvaluator(config.api, credentials),
        SmtpMailer(config.smtp, credentials),
    )
    if once:
        await engine.collect_once()
        for _ in range(1000):
            if not await engine.evaluate_once():
                break
        for _ in range(1000):
            if not await engine.notify_once():
                break
        print(json.dumps(store.status(), sort_keys=True))
    else:
        print("PostRadar running. Press Ctrl+C to stop. Use status in another terminal.")
        await engine.run(asyncio.Event())


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "demo":
            from post_radar.demo import run_demo

            with tempfile.TemporaryDirectory(prefix="post-radar-demo-") as temporary:
                status, messages = asyncio.run(run_demo(Path(temporary)))
                print("OFFLINE DEMO: synthetic content, simulated AI, no email sent.")
                print(json.dumps(status, sort_keys=True))
                for message in messages:
                    print(f"\nPreview: {message.subject}\n{message.body}")
                print(f"Mail previews: {len(messages)}")
            return 0
        if args.command == "init":
            path: Path = args.config
            path.parent.mkdir(parents=True, exist_ok=True)
            content = files("post_radar").joinpath("example.toml").read_text(encoding="utf-8")
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
            print("Local configuration created. API and email are disabled.")
            return 0
        if args.command == "secret":
            store_credentials = WindowsCredentialStore()
            if args.action == "set":
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    value = getpass.getpass("Secret (hidden; never saved in configuration): ")
                store_credentials.set(args.name, value)
                value = ""
            else:
                store_credentials.delete(args.name)
            print("PostRadar credential updated.")
            return 0
        config = load_config(args.config)
        if args.command == "check":
            print("Configuration valid. No network or credential access performed.")
        elif args.command == "doctor":
            print(
                json.dumps(
                    {
                        "version": __version__,
                        "windows_vault_supported": sys.platform == "win32",
                        "api_enabled": config.api.enabled,
                        "smtp_enabled": config.smtp.enabled,
                        "sources": len(config.sources),
                        "rules": len(config.rules),
                        "credentials_checked": False,
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "run":
            from post_radar.locking import RuntimeLock

            with RuntimeLock(config.state_dir):
                asyncio.run(_run(config, once=args.once))
        else:
            from post_radar.store import Store

            path = config.state_dir / "post-radar.sqlite3"
            if not path.is_file():
                print("state_not_initialized")
                return 1
            store = Store(path)
            if args.command == "status":
                print(
                    json.dumps(
                        {
                            "counts": store.status(),
                            "unknown_deliveries": store.unknown_deliveries(),
                        },
                        sort_keys=True,
                    )
                )
            elif args.command == "retry-delivery":
                changed = store.retry_unknown(args.delivery_id)
                print("delivery_requeued" if changed else "delivery_not_retryable_or_cooling_down")
                return 0 if changed else 1
            elif args.command == "resume-api":
                store.resume_api()
                print("api_resumed")
            elif args.command == "resume-mail":
                store.resume_mail()
                print("mail_resumed")
            elif args.command == "resume-source":
                if args.source_id not in {source.id for source in config.sources}:
                    raise ValueError("unknown_source")
                store.resume_source(args.source_id)
                print("source_resumed")
        return 0
    except KeyboardInterrupt:
        print("Stopped.")
        return 0
    except ExternalError as exc:
        print(f"error: {exc.code}", file=sys.stderr)
        return 1
    except FileExistsError:
        print("error: configuration_already_exists", file=sys.stderr)
        return 1
    except Exception:
        # Do not let configuration values, post text or provider exceptions reach stderr.
        print("error: operation_failed; check configuration and local state", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
