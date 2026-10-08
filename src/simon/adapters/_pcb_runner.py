"""Fixed KiCad CLI operations, executed inside an offline Linux worker lease."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

OPERATIONS = {
    "pcb.erc": (".kicad_sch", ".json"),
    "pcb.schematic_pdf": (".kicad_sch", ".pdf"),
    "pcb.netlist": (".kicad_sch", ".net"),
    "pcb.bom": (".kicad_sch", ".csv"),
    "pcb.drc": (".kicad_pcb", ".json"),
    "pcb.board_svg": (".kicad_pcb", ".svg"),
    "pcb.gerbers": (".kicad_pcb", ".zip"),
}
FILE_LIMIT = 40 * 1024 * 1024


def relative_file(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 500
        or value.startswith(("/", "-"))
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(
            part in {"", ".", ".."} or part.casefold() in {".git", ".ssh", ".aws"}
            for part in value.split("/")
        )
    ):
        raise ValueError("Expected a relative workspace file path without traversal")
    return value


def validate_arguments(operation: str, arguments: dict[str, Any]) -> dict[str, str]:
    if operation not in OPERATIONS or set(arguments) != {"input", "output"}:
        raise ValueError("Unsupported PCB operation or arguments")
    checked = {name: relative_file(arguments[name]) for name in ("input", "output")}
    if (
        tuple(Path(checked[name]).suffix.lower() for name in ("input", "output"))
        != OPERATIONS[operation]
    ):
        raise ValueError("PCB input or output file extension does not match the operation")
    return checked


def workspace_file(workspace: Path, value: str, *, output: bool = False) -> Path:
    current = workspace
    for part in relative_file(value).split("/"):
        current /= part
        if current.is_symlink():
            raise ValueError("PCB paths cannot contain symbolic links")
    if not current.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("PCB file escaped its workspace")
    if output:
        if current.exists():
            raise ValueError("Output exists; choose a new revision filename")
    elif (
        not current.is_file()
        or not stat.S_ISREG(current.stat().st_mode)
        or current.stat().st_size > FILE_LIMIT
    ):
        raise ValueError("PCB input must be a regular file up to 40 MiB")
    return current


def pcb_commands(operation: str, source: Path, destination: Path) -> list[list[str]]:
    cli = "/usr/bin/kicad-cli"
    if operation in {"pcb.erc", "pcb.drc"}:
        mode = ["sch", "erc"] if operation == "pcb.erc" else ["pcb", "drc"]
        flags = [] if operation == "pcb.erc" else ["--schematic-parity", "--all-track-errors"]
        return [
            [
                cli,
                *mode,
                "--format",
                "json",
                "--severity-all",
                "--exit-code-violations",
                *flags,
                "--output",
                str(destination),
                str(source),
            ]
        ]
    if operation == "pcb.schematic_pdf":
        return [[cli, "sch", "export", "pdf", "--output", str(destination), str(source)]]
    if operation == "pcb.netlist":
        return [
            [
                cli,
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "--output",
                str(destination),
                str(source),
            ]
        ]
    if operation == "pcb.bom":
        return [
            [
                cli,
                "sch",
                "export",
                "bom",
                "--fields",
                "Reference,Value,Footprint,${QUANTITY}",
                "--labels",
                "Reference,Value,Footprint,Quantity",
                "--output",
                str(destination),
                str(source),
            ]
        ]
    if operation == "pcb.board_svg":
        return [
            [
                cli,
                "pcb",
                "export",
                "svg",
                "--layers",
                "F.Cu,F.Silkscreen,Edge.Cuts",
                "--page-size-mode",
                "2",
                "--exclude-drawing-sheet",
                "--mode-single",
                "--output",
                str(destination),
                str(source),
            ]
        ]
    if operation == "pcb.gerbers":
        return [
            [cli, "pcb", "export", "gerbers", "--output", str(destination) + "/", str(source)],
            [
                cli,
                "pcb",
                "export",
                "drill",
                "--format",
                "excellon",
                "--output",
                str(destination) + "/",
                str(source),
            ],
        ]
    raise ValueError("Unsupported PCB operation")


def bundle_gerbers(directory: Path, target: Path) -> None:
    files = sorted(directory.iterdir())
    if not 1 <= len(files) <= 64:
        raise ValueError("Gerber package must contain at most 64 files")
    total = 0
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_STORED) as archive:
        for path in files:
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("Gerber package contains a nonregular file")
            total += path.stat().st_size
            if total > FILE_LIMIT - 65536:
                raise ValueError("Gerber package exceeds the output limit")
            archive.write(path, arcname=path.name)


def run(request: dict[str, Any], workspace: Path) -> int:
    operation = request["operation"]
    arguments = validate_arguments(operation, request["arguments"])
    source = workspace_file(workspace, arguments["input"])
    if operation == "pcb.drc":
        workspace_file(workspace, str(Path(arguments["input"]).with_suffix(".kicad_sch")))
    target = workspace_file(workspace, arguments["output"], output=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_LIMIT, FILE_LIMIT))
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "QT_QPA_PLATFORM": "offscreen",
    }
    deadline = time.monotonic() + request["timeout_seconds"]
    with tempfile.TemporaryDirectory(prefix=".simon-pcb-", dir=target.parent) as temporary:
        directory = Path(temporary)
        staged = directory / ("result" + target.suffix)
        destination = directory / "gerbers" if operation == "pcb.gerbers" else staged
        if operation == "pcb.gerbers":
            destination.mkdir()
        code = 0
        for command in pcb_commands(operation, source, destination):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 124
            try:
                result = subprocess.run(
                    command,
                    cwd=workspace,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    timeout=remaining,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                print("KiCad timed out; inspect the lease before retrying", file=sys.stderr)
                return 124
            code = result.returncode
            if code and not (code == 5 and operation in {"pcb.erc", "pcb.drc"}):
                return code
        if operation == "pcb.gerbers":
            bundle_gerbers(destination, staged)
        if (
            staged.is_symlink()
            or not staged.is_file()
            or not stat.S_ISREG(staged.stat().st_mode)
            or staged.stat().st_size > FILE_LIMIT
        ):
            raise ValueError("KiCad did not produce a bounded regular output")
        os.link(staged, target, follow_symlinks=False)
        print(
            json.dumps(
                {
                    "output_path": arguments["output"],
                    "size_bytes": target.stat().st_size,
                    "violations_present": code == 5
                    if operation
                    in {
                        "pcb.erc",
                        "pcb.drc",
                    }
                    else None,
                }
            )
        )
        return code


if __name__ == "__main__":
    try:
        sys.exit(run(json.loads(sys.argv[1]), Path("/workspace")))
    except (ValueError, TypeError, KeyError, IndexError, OSError):
        print(
            "KiCad rejected: invalid file, unsupported arguments, or output already exists",
            file=sys.stderr,
        )
        sys.exit(2)
