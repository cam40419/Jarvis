"""Fixed document/media operations, executed only inside an offline worker lease."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

READ_OPERATIONS = frozenset({"document.extract_pdf", "image.ocr", "media.inspect"})
WRITE_OPERATIONS = frozenset(
    {
        "document.convert",
        "media.thumbnail",
        "media.extract_audio",
        "media.transcode",
    }
)
INPUT_LIMIT = 100 * 1024 * 1024
OUTPUT_LIMIT = 100 * 1024 * 1024
MEDIA_FORMATS = {
    ".mp4": "mov",
    ".mov": "mov",
    ".m4a": "mov",
    ".mkv": "matroska",
    ".webm": "matroska",
    ".mp3": "mp3",
    ".wav": "wav",
    ".flac": "flac",
    ".ogg": "ogg",
}
DOCUMENT_FORMATS = {".md": "gfm-raw_html", ".txt": "gfm-raw_html", ".docx": "docx"}
OUTPUT_FORMATS = {".md": "gfm", ".txt": "plain", ".docx": "docx"}


def relative_file(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 500
        or value.startswith(("/", "-"))
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} or part.casefold() == ".git" for part in value.split("/"))
    ):
        raise ValueError("Expected a relative workspace file path without traversal/.git")
    return value


def validate_arguments(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "document.extract_pdf": {"input", "max_pages"},
        "image.ocr": {"input"},
        "media.inspect": {"input"},
        "document.convert": {"input", "output"},
        "media.thumbnail": {"input", "output", "seconds", "width"},
        "media.extract_audio": {"input", "output", "duration_seconds"},
        "media.transcode": {"input", "output", "duration_seconds", "width"},
    }
    if operation not in fields or set(arguments) - fields[operation]:
        raise ValueError("Unsupported processing operation or arguments")
    result = dict(arguments)
    result["input"] = relative_file(arguments.get("input"))
    suffix = Path(result["input"]).suffix.lower()
    if (
        (operation == "document.extract_pdf" and suffix != ".pdf")
        or (operation == "image.ocr" and suffix not in {".png", ".jpg", ".jpeg", ".tiff"})
        or (operation.startswith("media.") and suffix not in MEDIA_FORMATS)
        or (operation == "document.convert" and suffix not in DOCUMENT_FORMATS)
    ):
        raise ValueError("Unsupported input file extension")
    if operation in WRITE_OPERATIONS:
        result["output"] = relative_file(arguments.get("output"))
        extension = Path(result["output"]).suffix.lower()
        extensions = {
            "document.convert": set(OUTPUT_FORMATS),
            "media.thumbnail": {".png"},
            "media.extract_audio": {".wav"},
            "media.transcode": {".mp4"},
        }
        if extension not in extensions[operation] or result["output"] == result["input"]:
            raise ValueError("Output must use a supported extension and differ from its input")
    numeric = {
        "max_pages": (20, 1, 100),
        "seconds": (0, 0, 3600),
        "width": (1280, 64, 1920),
        "duration_seconds": (60, 1, 600),
    }
    for key, (default, minimum, maximum) in numeric.items():
        if key in fields[operation]:
            value = arguments.get(key, default)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
            if key == "width" and value % 2:
                raise ValueError("width must be even")
            result[key] = value
    return result


def workspace_file(workspace: Path, value: str, *, output: bool = False) -> Path:
    current = workspace
    for part in relative_file(value).split("/"):
        current /= part
        if current.is_symlink():
            raise ValueError("Symbolic links are not supported for processing files")
    if not current.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("File escaped its workspace")
    if output:
        if current.exists():
            raise ValueError("Output already exists; choose a new revision filename")
    elif not current.is_file() or current.stat().st_size > INPUT_LIMIT:
        raise ValueError("Input must be a regular file up to 100 MiB")
    return current


def processing_command(
    operation: str,
    arguments: dict[str, Any],
    source: Path,
    destination: Path | None,
) -> list[str]:
    if operation == "document.extract_pdf":
        return [
            "/usr/bin/pdftotext",
            "-f",
            "1",
            "-l",
            str(arguments["max_pages"]),
            "-layout",
            str(source),
            "-",
        ]
    if operation == "image.ocr":
        return ["/usr/bin/tesseract", str(source), "stdout", "-l", "eng"]
    if operation == "document.convert":
        assert destination is not None
        return [
            "/usr/bin/pandoc",
            "--sandbox",
            "--standalone",
            "--from=" + DOCUMENT_FORMATS[source.suffix.lower()],
            "--to=" + OUTPUT_FORMATS[destination.suffix.lower()],
            "--output=" + str(destination),
            "--",
            str(source),
        ]
    media_input = ["-protocol_whitelist", "file", "-f", MEDIA_FORMATS[source.suffix.lower()]]
    if operation == "media.inspect":
        return [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            *media_input,
            "-show_entries",
            "format=duration,size,format_name:"
            "stream=index,codec_name,codec_type,width,height,sample_rate,channels",
            "-of",
            "json",
            str(source),
        ]
    assert destination is not None
    prefix = [
        "/usr/bin/ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-threads",
        "2",
        *media_input,
    ]
    if operation == "media.thumbnail":
        return [
            *prefix,
            "-ss",
            str(arguments["seconds"]),
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            f"scale={arguments['width']}:-2",
            "-threads",
            "2",
            "-f",
            "image2",
            "-update",
            "1",
            str(destination),
        ]
    prefix += [
        "-i",
        str(source),
        "-t",
        str(arguments["duration_seconds"]),
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-fs",
        str(OUTPUT_LIMIT - 65536),
    ]
    if operation == "media.extract_audio":
        return [
            *prefix,
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(destination),
        ]
    if operation == "media.transcode":
        return [
            *prefix,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-vf",
            f"scale={arguments['width']}:-2",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-threads",
            "2",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(destination),
        ]
    raise ValueError("Unsupported processing operation")


def run(request: dict[str, Any], workspace: Path) -> int:
    operation = request["operation"]
    arguments = validate_arguments(operation, request["arguments"])
    source = workspace_file(workspace, arguments["input"])
    target = (
        workspace_file(workspace, arguments["output"], output=True)
        if operation in WRITE_OPERATIONS
        else None
    )
    # Linux resource limit also bounds converter output that cannot expose a
    # command-specific file limit. Process/container limits remain authoritative.
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_LIMIT, OUTPUT_LIMIT))
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "OMP_THREAD_LIMIT": "2",
    }

    def execute(destination: Path | None) -> int:
        command = processing_command(operation, arguments, source, destination)
        try:
            result = subprocess.run(
                command,
                cwd=workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                timeout=request["timeout_seconds"],
                check=False,
            )
        except subprocess.TimeoutExpired:
            print("Processing timed out; inspect the lease before retrying", file=sys.stderr)
            return 124
        return result.returncode

    if target is None:
        return execute(None)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".simon-processing-", dir=target.parent) as temporary:
        staged = Path(temporary) / ("result" + target.suffix.lower())
        code = execute(staged)
        if code:
            return code
        if (
            staged.is_symlink()
            or not staged.is_file()
            or not stat.S_ISREG(staged.stat().st_mode)
            or staged.stat().st_size > OUTPUT_LIMIT
        ):
            raise ValueError("Processor did not produce a valid bounded output")
        # Atomic no-overwrite publication on the same filesystem as the target.
        os.link(staged, target, follow_symlinks=False)
        print(json.dumps({"output_path": arguments["output"], "size_bytes": target.stat().st_size}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run(json.loads(sys.argv[1]), Path("/workspace")))
    except (ValueError, TypeError, KeyError, IndexError, OSError):
        print(
            "Processing rejected: invalid file, unsupported arguments, or output already exists",
            file=sys.stderr,
        )
        sys.exit(2)
