"""Hermes CLI integration for collection and dataset exploration."""

import os
import subprocess
import sys
from pathlib import Path

try:
    from .batch import BatchError, collect_batch, default_run_id, read_cadastral_numbers
except ImportError:  # Direct execution from a source checkout.
    from batch import BatchError, collect_batch, default_run_id, read_cadastral_numbers


def setup_cli(parser):
    commands = parser.add_subparsers(dest="uchastok_command")
    batch = commands.add_parser("batch", help="Collect a cadastral dataset")
    batch.add_argument("--input", required=True, type=Path, help="TXT or CSV input")
    batch.add_argument("--output", required=True, type=Path, help="Dataset directory")
    batch.add_argument("--run-id", help="Stable run id; required to resume a prior run")
    batch.add_argument("--dataset-version", help="Immutable annotation dataset version")
    batch.add_argument("--depth", choices=["minimal", "standard", "full"], default="standard")
    batch.add_argument("--workers", type=int, default=1, choices=range(1, 5))
    batch.add_argument("--resume", action="store_true")
    dashboard = commands.add_parser("dashboard", help="Open the dataset dashboard")
    dashboard.add_argument("--dataset", required=True, type=Path, help="Dataset directory or DuckDB file")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8501)
    parser.set_defaults(func=handle_cli)


def handle_cli(args):
    command = getattr(args, "uchastok_command", None)
    if command == "dashboard":
        dashboard_path = Path(__file__).with_name("dashboard.py")
        environment = os.environ.copy()
        environment["UCHASTOK_DATASET_PATH"] = str(args.dataset.resolve())
        return subprocess.call([
            sys.executable, "-m", "streamlit", "run", str(dashboard_path),
            "--server.address", args.host, "--server.port", str(args.port),
            "--server.headless", "true",
        ], env=environment)
    if command != "batch":
        print("Usage: hermes uchastok {batch|dashboard}")
        return 2
    try:
        numbers = read_cadastral_numbers(args.input)
        run_id = args.run_id or default_run_id()
        if args.resume and not args.run_id:
            raise BatchError("Для --resume укажите прежний --run-id")
        dataset_version = args.dataset_version or f"investment-{run_id}"
        manifest = collect_batch(
            numbers,
            args.output,
            run_id=run_id,
            dataset_version=dataset_version,
            depth=args.depth,
            workers=args.workers,
            resume=args.resume,
        )
        counts = manifest["counts"]
        print("\nСбор завершён")
        print(f"Run ID: {run_id}")
        print(f"Успешно: {counts.get('completed', 0)}")
        print(f"Частично: {counts.get('partial', 0)}")
        print(f"Ошибки: {counts.get('failed', 0)}")
        print(f"Задания: {manifest['annotation_tasks']}")
        return 0
    except BatchError as exc:
        print(f"Ошибка: {exc}")
        return 2
