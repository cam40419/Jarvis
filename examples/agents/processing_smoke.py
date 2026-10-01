"""Exercise every processing tool against synthetic inputs in an offline Docker lease."""

from pathlib import Path
from uuid import uuid4

from simon.adapters.processing_tools import ProcessingToolTransport, processing_tool_definitions
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.execution import EnvironmentDefinition, EnvironmentRequest, ExecutionCommand
from simon.domain.tool_catalog import ToolExecutionContext
from simon.services.execution import EnvironmentManager

_FIXTURES = r"""
from pathlib import Path
Path('/workspace/source.md').write_text('# Example report\n\nSynthetic processing test.\n')
stream = b'BT /F1 24 Tf 72 720 Td (SIMON PDF TEST) Tj ET'
objects = [
    b'<< /Type /Catalog /Pages 2 0 R >>',
    b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
    b'/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream',
]
body = bytearray(b'%PDF-1.4\n')
offsets = [0]
for number, obj in enumerate(objects, start=1):
    offsets.append(len(body))
    body.extend(str(number).encode() + b' 0 obj\n' + obj + b'\nendobj\n')
xref = len(body)
body.extend(b'xref\n0 6\n0000000000 65535 f \n')
for offset in offsets[1:]:
    body.extend(f'{offset:010d} 00000 n \n'.encode())
body.extend(b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n')
body.extend(str(xref).encode() + b'\n%%EOF\n')
Path('/workspace/source.pdf').write_bytes(body)
"""


def main() -> None:
    state = (Path(".local/agents") / ("processing-smoke-" + uuid4().hex)).resolve()
    capabilities = frozenset({"python", "pdf", "ocr", "media", "documents"})
    manager = EnvironmentManager(
        [
            EnvironmentDefinition(
                id="processing-smoke",
                kind="docker",
                enabled=True,
                container_image="simon-processing:local",
                capabilities=capabilities,
                network="none",
            )
        ],
        state_path=state / "leases.sqlite3",
        workspace_root=state / "workspaces",
    )
    request = EnvironmentRequest(
        workspace_id=uuid4(),
        agent_id="processing-smoke",
        task_id=uuid4(),
        attempt_id=uuid4(),
        capabilities=capabilities,
        os="linux",
    )
    lease = manager.allocate(request, environment_id="processing-smoke")
    ownership = {"attempt_id": request.attempt_id, "fencing_token": lease.fencing_token}
    context = ToolExecutionContext(
        actor_id=uuid4(),
        household_id=request.workspace_id,
        run_id=uuid4(),
        agent_id="processing-smoke",
        scopes=frozenset({"jobs:read", "jobs:write"}),
        allowed_tool_ids=frozenset(item.id for item in processing_tool_definitions()),
        environment_capabilities=capabilities,
        authorized_action="write",
    )
    registry = TransportRegistry()
    registry.register(
        "processing",
        ProcessingToolTransport(
            manager,
            lease,
            actor_id=context.actor_id,
            run_id=context.run_id,
        ),
    )
    definitions = {item.id: item for item in processing_tool_definitions(enabled=True)}
    try:
        for argv in (
            ("/usr/local/bin/python3", "-I", "-c", _FIXTURES),
            (
                "/usr/bin/ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-n",
                "-f",
                "lavfi",
                "-i",
                "color=c=white:s=640x360:r=10:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-vf",
                "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
                "text=SIMON TEST:fontcolor=black:fontsize=40:x=80:y=150",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-shortest",
                "/workspace/source.mp4",
            ),
        ):
            result = manager.execute(lease.id, ExecutionCommand(argv=argv), **ownership)
            if result.exit_code:
                raise RuntimeError("Synthetic input creation failed: " + result.stderr)
        operations = (
            ("document.extract_pdf", {"input": "source.pdf"}),
            ("document.convert", {"input": "source.md", "output": "report.docx"}),
            ("document.convert", {"input": "report.docx", "output": "roundtrip.md"}),
            ("media.inspect", {"input": "source.mp4"}),
            ("media.thumbnail", {"input": "source.mp4", "output": "preview.png", "width": 640}),
            ("image.ocr", {"input": "preview.png"}),
            ("media.extract_audio", {"input": "source.mp4", "output": "audio.wav"}),
            ("media.transcode", {"input": "source.mp4", "output": "render.mp4", "width": 640}),
        )
        for operation, arguments in operations:
            result = registry.execute(definitions[operation], arguments, context)
            if result.output["exit_code"]:
                raise RuntimeError(f"{operation}: {result.output['stderr']}")
            if (
                operation == "document.extract_pdf"
                and "SIMON PDF TEST" not in result.output["stdout"]
            ):
                raise RuntimeError("PDF extraction did not return fixture text")
            if operation == "image.ocr" and "SIMON TEST" not in result.output["stdout"]:
                raise RuntimeError("OCR did not return fixture text")
            print(f"{operation}: passed")
        collision = registry.execute(
            definitions["document.convert"],
            {"input": "source.md", "output": "report.docx"},
            context,
        )
        if collision.output["exit_code"] == 0:
            raise RuntimeError("Existing output was overwritten")
        print("Existing output preserved: passed")
    finally:
        manager.release(lease.id, **ownership)
    print(f"Lease released. Synthetic workspace retained at {state}")


if __name__ == "__main__":
    main()
