import argparse


def _cmd_run(_: argparse.Namespace) -> int:
    from rebalance_trading_equity import main_cli

    main_cli()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grvt-transfer", add_help=True)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run the rebalance loop (starts Telegram bot)")
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return int(args.func(args))

