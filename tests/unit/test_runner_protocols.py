"""Offline runner boundaries, publication races and machine protocol failures."""

import hashlib
import json
import runpy
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon import creative_bridge, creative_host, desktop_bridge
from simon.adapters import _cad_runner as cad
from simon.adapters import _git_runner as git
from simon.adapters import _pcb_runner as pcb
from simon.adapters import _processing_runner as processing
from tests.unit.test_application_tools import Control

RUNNERS = (cad, processing, pcb, git)


@pytest.fixture
def linux_lease(monkeypatch):
    limits = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(
        sys.modules,
        "resource",
        SimpleNamespace(RLIMIT_FSIZE=1, setrlimit=lambda *args: limits.append(args)),
    )
    monkeypatch.setenv("PRIVATE_PROVIDER_KEY", "synthetic-private-key")
    return limits


def request(operation, source, target=None, **arguments):
    return json.loads(
        json.dumps(
            {
                "operation": operation,
                "arguments": {
                    "input": source,
                    **({"output": target} if target else {}),
                    **arguments,
                },
                "timeout_seconds": 5,
            }
        )
    )


@pytest.mark.parametrize("runner", RUNNERS, ids=lambda module: module.__name__.rsplit(".", 1)[-1])
@pytest.mark.parametrize(
    "payload",
    [None, "{bad-json", "[]", "{}", "null", '{"operation":"private-document-name","arguments":{}}'],
)
def test_runner_entrypoint_rejects_invalid_envelope_without_traceback(
    runner, payload, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", [runner.__file__, *([payload] if payload is not None else [])])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(runner.__file__, run_name="__main__")
    output = capsys.readouterr()
    assert stopped.value.code == 2
    assert not output.out
    assert "rejected" in output.err
    assert "Traceback" not in output.err and "private-document-name" not in output.err


def assert_offline_call(command, options, workspace):
    assert command[0].startswith("/usr/bin/")
    assert options["cwd"] == workspace and options["stdin"] == subprocess.DEVNULL
    assert options["env"]["HOME"] == "/tmp"
    assert "PRIVATE_PROVIDER_KEY" not in options["env"]
    assert options["check"] is False and options.get("shell", False) is False
    assert 0 < options["timeout"] <= 5


def test_document_conversion_publishes_exact_revision_and_clean_environment(
    tmp_path, monkeypatch, capsys, linux_lease
):
    (tmp_path / "source document.md").write_text("# Formal document", encoding="utf-8")
    calls = []
    content = b"synthetic-docx-package"

    def converter(command, **options):
        assert_offline_call(command, options, tmp_path)
        calls.append(command)
        assert "--sandbox" in command and command[-2] == "--"
        destination = Path(
            next(part.partition("=")[2] for part in command if part.startswith("--output="))
        )
        assert destination.parent != tmp_path
        destination.write_bytes(content)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(processing.subprocess, "run", converter)
    job = request("document.convert", "source document.md", "review/document.docx")
    assert processing.run(job, tmp_path) == 0
    assert json.loads(capsys.readouterr().out) == {
        "output_path": "review/document.docx",
        "size_bytes": len(content),
    }
    assert (tmp_path / "review/document.docx").read_bytes() == content
    assert not list(tmp_path.rglob(".simon-processing-*"))
    with pytest.raises(ValueError, match="already exists"):
        processing.run(job, tmp_path)
    assert len(calls) == 1 and linux_lease


@pytest.mark.parametrize(
    "failure", ["exit", "timeout", "missing", "directory", "oversized", "collision"]
)
def test_processing_failure_never_publishes_partial_or_replaces_existing_revision(
    tmp_path, monkeypatch, capsys, failure, linux_lease
):
    (tmp_path / "source.mp4").write_bytes(b"source")
    target = tmp_path / "preview.png"
    monkeypatch.setattr(processing, "OUTPUT_LIMIT", 32)

    def converter(command, **options):
        assert_offline_call(command, options, tmp_path)
        staged = Path(command[-1])
        if failure == "directory":
            staged.mkdir()
        elif failure != "missing":
            staged.write_bytes(b"x" * (33 if failure == "oversized" else 4))
        if failure == "collision":
            target.write_bytes(b"concurrent approved revision")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 5, stderr="private provider detail")
        return SimpleNamespace(returncode=9 if failure == "exit" else 0)

    monkeypatch.setattr(processing.subprocess, "run", converter)
    job = request("media.thumbnail", "source.mp4", target.name)
    if failure in {"exit", "timeout"}:
        assert processing.run(job, tmp_path) == (9 if failure == "exit" else 124)
    else:
        with pytest.raises(FileExistsError if failure == "collision" else ValueError):
            processing.run(job, tmp_path)
    assert (
        target.read_bytes() == b"concurrent approved revision"
        if failure == "collision"
        else not target.exists()
    )
    output = capsys.readouterr()
    assert output.out == "" and "private provider detail" not in output.err
    assert not list(tmp_path.glob(".simon-processing-*"))


def test_processing_observation_keeps_provider_stdout_and_exit_status(
    tmp_path, monkeypatch, capsys, linux_lease
):
    (tmp_path / "source.pdf").write_bytes(b"%PDF-synthetic")

    def extractor(command, **options):
        assert_offline_call(command, options, tmp_path)
        assert command[-1] == "-" and command[command.index("-l") + 1] == "3"
        print("Extracted source text")
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(processing.subprocess, "run", extractor)
    assert processing.run(request("document.extract_pdf", "source.pdf", max_pages=3), tmp_path) == 7
    assert capsys.readouterr().out == "Extracted source text\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.pdf"]


def test_cad_render_publishes_scene_and_image_with_verifiable_hashes(
    tmp_path, monkeypatch, capsys, linux_lease
):
    (tmp_path / "design.stl").write_bytes(b"mesh fixture")
    monkeypatch.setattr(
        cad,
        "load_mesh",
        lambda _: SimpleNamespace(export=lambda path: path.write_bytes(b"validated mesh")),
    )

    def blender(command, **options):
        assert_offline_call(command, options, tmp_path)
        assert "--disable-autoexec" in command and "--factory-startup" in command
        parameters = json.loads(command[-1])
        assert parameters["resolution"] == 512 and parameters["samples"] == 16
        assert Path(parameters["input"]).read_bytes() == b"validated mesh"
        Path(parameters["output"]).write_bytes(b"rendered image")
        Path(parameters["scene"]).write_bytes(b"editable scene")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cad.subprocess, "run", blender)
    assert (
        cad.run(
            request(
                "cad.render_mesh",
                "design.stl",
                "review.png",
                save_scene=True,
                resolution=512,
                samples=16,
            ),
            tmp_path,
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert {item["path"] for item in result["files"]} == {"review.png", "review.blend"}
    for item in result["files"]:
        content = (tmp_path / item["path"]).read_bytes()
        assert item["bytes"] == len(content)
        assert item["sha256"] == hashlib.sha256(content).hexdigest()
    assert not list(tmp_path.glob(".simon-cad-*"))


@pytest.mark.parametrize("outcome", ["timeout", "nonzero", "oversized"])
def test_cad_export_failure_keeps_source_and_no_published_output(
    tmp_path, monkeypatch, capsys, linux_lease, outcome
):
    source = tmp_path / "design.scad"
    source.write_bytes(b"cube(2);")
    monkeypatch.setattr(cad, "FILE_LIMIT", 16)

    def openscad(command, **options):
        assert_offline_call(command, options, tmp_path)
        assert "--hardwarnings" in command and "binstl" in command
        Path(command[command.index("-o") + 1]).write_bytes(
            b"x" * (17 if outcome == "oversized" else 4)
        )
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 5, stderr="private failure")
        return SimpleNamespace(returncode=3 if outcome == "nonzero" else 0)

    monkeypatch.setattr(cad.subprocess, "run", openscad)
    job = request("cad.openscad_export", source.name, "out.stl")
    if outcome == "oversized":
        with pytest.raises(ValueError, match="bounded"):
            cad.run(job, tmp_path)
    else:
        assert cad.run(job, tmp_path) == (124 if outcome == "timeout" else 3)
    assert source.read_bytes() == b"cube(2);" and not (tmp_path / "out.stl").exists()
    output = capsys.readouterr()
    assert not output.out and "private failure" not in output.err
    assert not list(tmp_path.glob(".simon-cad-*"))


def test_gerber_export_bundles_both_commands_in_stable_filename_order(
    tmp_path, monkeypatch, capsys, linux_lease
):
    (tmp_path / "board.kicad_pcb").write_text("synthetic board")
    calls = []

    def kicad(command, **options):
        assert_offline_call(command, options, tmp_path)
        calls.append(command)
        target = Path(command[command.index("--output") + 1])
        (target / ("copper.gbr" if command[3] == "gerbers" else "board.drl")).write_bytes(
            b"fabrication data"
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pcb.subprocess, "run", kicad)
    assert pcb.run(request("pcb.gerbers", "board.kicad_pcb", "fabrication.zip"), tmp_path) == 0
    artifact = tmp_path / "fabrication.zip"
    with zipfile.ZipFile(artifact) as archive:
        assert archive.namelist() == ["board.drl", "copper.gbr"]
        assert all(archive.read(name) == b"fabrication data" for name in archive.namelist())
    assert json.loads(capsys.readouterr().out) == {
        "output_path": artifact.name,
        "size_bytes": artifact.stat().st_size,
        "violations_present": None,
    }
    assert len(calls) == 2 and not list(tmp_path.glob(".simon-pcb-*"))


def test_gerber_deadline_is_shared_by_commands_and_partial_outputs_stay_private(
    tmp_path, monkeypatch, linux_lease
):
    (tmp_path / "board.kicad_pcb").write_text("synthetic board")
    moments = iter([0.0, 0.1, 6.0])
    monkeypatch.setattr(pcb.time, "monotonic", lambda: next(moments))
    calls = []

    def kicad(command, **options):
        calls.append(command)
        assert options["timeout"] == 4.9
        Path(command[command.index("--output") + 1], "partial.gbr").write_bytes(b"partial")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pcb.subprocess, "run", kicad)
    assert pcb.run(request("pcb.gerbers", "board.kicad_pcb", "fabrication.zip"), tmp_path) == 124
    assert len(calls) == 1 and not (tmp_path / "fabrication.zip").exists()
    assert not list(tmp_path.glob(".simon-pcb-*"))


@pytest.mark.parametrize("invalid", ["empty", "too-many", "directory", "oversized"])
def test_gerber_bundle_rejects_unbounded_or_nonregular_provider_outputs(
    tmp_path, monkeypatch, invalid
):
    directory = tmp_path / "gerbers"
    directory.mkdir()
    if invalid == "too-many":
        for index in range(65):
            (directory / f"layer-{index}.gbr").write_bytes(b"layer")
    elif invalid == "directory":
        (directory / "nested").mkdir()
    elif invalid == "oversized":
        monkeypatch.setattr(pcb, "FILE_LIMIT", 65540)
        (directory / "layer.gbr").write_bytes(b"large")
    with pytest.raises(ValueError):
        pcb.bundle_gerbers(directory, tmp_path / "staged.zip")


@pytest.mark.parametrize("outcome", ["timeout", "failure"])
def test_git_runner_fences_inherited_configuration_and_propagates_safe_failure(
    tmp_path, monkeypatch, capsys, outcome
):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.sshCommand")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "untrusted helper")
    monkeypatch.setenv("PRIVATE_PROVIDER_KEY", "synthetic-private-key")

    def execute(command, **options):
        assert options["stdin"] == subprocess.DEVNULL
        environment = options["env"]
        assert (
            not {
                "GIT_CONFIG_COUNT",
                "GIT_CONFIG_KEY_0",
                "GIT_CONFIG_VALUE_0",
                "PRIVATE_PROVIDER_KEY",
            }
            & environment.keys()
        )
        assert environment["GIT_ALLOW_PROTOCOL"] == "" and environment["GIT_TERMINAL_PROMPT"] == "0"
        assert command[0] == "/usr/bin/git" and "init" in command
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 5, stderr="private repository metadata")
        return SimpleNamespace(returncode=17)

    monkeypatch.setattr(git.subprocess, "run", execute)
    job = {
        "operation": "init",
        "arguments": {"repository": "new repository"},
        "author_name": "Project worker",
        "author_email": "worker@example.test",
        "timeout_seconds": 5,
    }
    assert git.run(json.loads(json.dumps(job)), tmp_path) == (124 if outcome == "timeout" else 17)
    assert "private repository metadata" not in capsys.readouterr().err


@pytest.mark.parametrize("failure", [None, "disabled", "lease", "fence", "process", "provider"])
def test_desktop_entrypoint_binds_json_request_to_owned_window_before_observation(
    tmp_path, monkeypatch, capsys, failure
):
    window = Control(name="private document")
    payload = {
        "version": 1,
        "operation": "inspect",
        "arguments": {},
        "invocation_id": str(uuid4()),
        "lease_id": str(uuid4()),
        "fencing_token": 4,
    }
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    for key, value in {
        "ENABLED": "0" if failure == "disabled" else "1",
        "LEASE_ID": str(uuid4()) if failure == "lease" else payload["lease_id"],
        "FENCING_TOKEN": "3" if failure == "fence" else "4",
        "WINDOW_HANDLE": "42",
        "PROCESS_ID": "321" if failure == "process" else "123",
    }.items():
        monkeypatch.setenv("SIMON_DESKTOP_" + key, value)
    original = desktop_bridge.importlib.import_module
    observed = []

    def import_provider(name, *args, **kwargs):
        if name != "pywinauto":
            return original(name, *args, **kwargs)
        observed.append(name)
        if failure == "provider":
            raise RuntimeError("private document provider failure")
        return SimpleNamespace(
            Desktop=lambda **_: SimpleNamespace(
                window=lambda **kw: SimpleNamespace(wrapper_object=lambda: window)
            )
        )

    monkeypatch.setattr(desktop_bridge.importlib, "import_module", import_provider)
    monkeypatch.setattr(sys, "argv", [desktop_bridge.__file__, json.dumps(payload)])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(desktop_bridge.__file__, run_name="__main__")
    output = capsys.readouterr()
    if failure is None:
        assert stopped.value.code == 0
        assert json.loads(output.out)["window"] == 42
        assert not output.err
    else:
        assert stopped.value.code == 2 and not output.out
        assert "private document" not in output.err and "Traceback" not in output.err
        if failure in {"disabled", "lease", "fence"}:
            assert not observed
    assert window.calls == []


@pytest.mark.parametrize("provider_failure", [False, True])
def test_creative_entrypoint_uses_durable_mailbox_and_redacts_host_failures(
    tmp_path, monkeypatch, capsys, provider_failure
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    monkeypatch.chdir(workspace)
    session = {
        "lease_id": str(uuid4()),
        "fencing_token": 2,
        "application": "houdini",
        "workspace": str(workspace),
        "expires_at": time.time() + 60,
    }
    (mailbox / "session.json").write_text(json.dumps(session), encoding="utf-8")
    payload = {
        "version": 1,
        "lease_id": session["lease_id"],
        "fencing_token": 2,
        "invocation_id": str(uuid4()),
        "operation": "inspect",
        "arguments": {},
    }

    def host(application, operation, arguments):
        assert (application, operation, arguments) == ("houdini", "inspect", {})
        if provider_failure:
            raise RuntimeError("private scene document contents")
        return {"document": "review.hip"}

    monkeypatch.setattr(creative_host, "native", host)
    monkeypatch.setattr(
        creative_bridge.time, "sleep", lambda _: creative_host.pump(str(mailbox), "houdini")
    )
    monkeypatch.setenv("SIMON_CREATIVE_MAILBOX", str(mailbox))
    monkeypatch.setattr(sys, "argv", [creative_bridge.__file__, "houdini", json.dumps(payload)])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(creative_bridge.__file__, run_name="__main__")
    output = capsys.readouterr()
    assert (mailbox / (payload["invocation_id"] + ".claimed")).is_file()
    assert not (mailbox / "request.json").exists()
    if provider_failure:
        assert stopped.value.code == 2 and not output.out
        assert (mailbox / "busy").exists()
        assert "private scene" not in output.err
    else:
        assert stopped.value.code == 0
        assert json.loads(output.out) == {"ok": True, "result": {"document": "review.hip"}}
        assert not (mailbox / "busy").exists()
