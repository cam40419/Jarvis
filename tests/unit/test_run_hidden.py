from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_hidden.py"
SPEC = importlib.util.spec_from_file_location("run_hidden", SCRIPT)
assert SPEC and SPEC.loader
run_hidden = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_hidden)


def test_launcher_uses_no_window_and_propagates_exit_code(tmp_path, monkeypatch):
    script = tmp_path / "task.ps1"
    script.write_text("exit 17", encoding="utf-8")
    observed = {}

    def run(command, **options):
        observed.update(command=command, options=options)
        return SimpleNamespace(returncode=17)

    monkeypatch.setattr(run_hidden.subprocess, "run", run)

    assert run_hidden.main([str(script), "-Example"]) == 17
    assert observed["command"][-2:] == [str(script.resolve()), "-Example"]
    assert observed["options"]["creationflags"] == getattr(
        run_hidden.subprocess, "CREATE_NO_WINDOW", 0
    )
    assert observed["options"]["stdout"] is run_hidden.subprocess.DEVNULL


def test_launcher_rejects_a_missing_script(tmp_path):
    assert run_hidden.main([str(tmp_path / "missing.ps1")]) == 2
