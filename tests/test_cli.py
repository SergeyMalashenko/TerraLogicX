import argparse
from pathlib import Path
from unittest.mock import patch

import cli


def test_dashboard_command_starts_streamlit():
    parser = argparse.ArgumentParser()
    cli.setup_cli(parser)
    args = parser.parse_args(["dashboard", "--dataset", "./data", "--port", "9000"])
    with patch("cli.subprocess.call", return_value=0) as call:
        assert cli.handle_cli(args) == 0
    command = call.call_args.args[0]
    assert command[1:4] == ["-m", "streamlit", "run"]
    assert command[-3:] == ["9000", "--server.headless", "true"]
    assert call.call_args.kwargs["env"]["UCHASTOK_DATASET_PATH"] == str(Path("data").resolve())


if __name__ == "__main__":
    test_dashboard_command_starts_streamlit()
