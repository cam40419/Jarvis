"""Run a PowerShell task without allocating a visible Windows console."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def powershell_command(script: Path, arguments: list[str]) -> list[str]:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *arguments,
    ]


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if not arguments:
        return 2
    script = Path(arguments.pop(0)).resolve()
    if not script.is_file():
        return 2
    completed = subprocess.run(
        powershell_command(script, arguments),
        cwd=script.parent.parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
