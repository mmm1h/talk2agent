from types import SimpleNamespace
import subprocess
import sys

import talk2agent.cli as cli


def test_module_help_runs():
    result = subprocess.run(
        [sys.executable, "-m", "talk2agent", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "harness" in result.stdout
    assert "init" in result.stdout
    assert "start" in result.stdout


def test_main_dispatches_harness(monkeypatch):
    called = SimpleNamespace(count=0)

    def fake_run_harness() -> int:
        called.count += 1
        return 17

    monkeypatch.setattr(cli, "run_harness", fake_run_harness)

    assert cli.main(["harness"]) == 17
    assert called.count == 1
