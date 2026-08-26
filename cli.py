"""Hermes CLI integration for batch dataset collection."""

from pathlib import Path

from .batch import BatchError, collect_batch, default_run_id, read_cadastral_numbers


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
    parser.set_defaults(func=handle_cli)


def handle_cli(args):
    if getattr(args, "uchastok_command", None) != "batch":
        print("Usage: hermes uchastok batch --input parcels.csv --output ./dataset")
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
