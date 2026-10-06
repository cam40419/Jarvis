import base64
import hashlib
import io
import json
import struct
import wave
import zlib
from uuid import uuid4

import httpx
import pytest

from simon.adapters import generative_tools as module
from simon.adapters.generative_tools import (
    AUDIO_ENDPOINT,
    IMAGE_ENDPOINT,
    GenerativeToolTransport,
    TranscribeAudio,
    _read_audio,
    generative_configuration_reason,
    generative_tool_definitions,
)
from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.models import ActorContext, Channel
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionError
from tests.unit.test_git_tools import setup


def image_bytes(width=1024, height=1024):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(
                ">I",
                zlib.crc32(kind + data),
            )
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0),
        )
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00\x80\xff\xff" * width) * height))
        + chunk(
            b"IEND",
            b"",
        )
    )


def wav_bytes(seconds=1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000 * seconds)
    return buffer.getvalue()


@pytest.fixture
def runtime(tmp_path):
    _, lease, _, context, _ = setup()
    lease = lease.model_copy(
        update={
            "plan": lease.plan.model_copy(update={"workspace_path": tmp_path}),
            "definition": lease.definition.model_copy(
                update={
                    "capabilities": frozenset({"workspace.write"}),
                }
            ),
        }
    )
    definitions = generative_tool_definitions(
        image_model="operator-image-model",
        transcription_model="operator-transcription-model",
        enabled=True,
    )
    context = context.model_copy(
        update={
            "allowed_tool_ids": frozenset(item.id for item in definitions),
            "scopes": frozenset({"jobs:read", "jobs:write"}),
            "environment_capabilities": frozenset({"workspace.write"}),
            "authorized_action": "write",
        }
    )
    actor = ActorContext(
        actor_id=context.actor_id,
        workspace_id=context.workspace_id,
        scopes=context.scopes,
        channel=Channel.API,
    )
    requests = []

    def respond(request):
        requests.append(request)
        if str(request.url) == IMAGE_ENDPOINT:
            return httpx.Response(
                200,
                json={
                    "data": [{"b64_json": base64.b64encode(image_bytes()).decode()}],
                },
            )
        assert str(request.url) == AUDIO_ENDPOINT
        return httpx.Response(200, json={"text": "A useful transcript."})

    handler = GenerativeToolTransport(
        lease,
        actor=actor,
        run_id=context.run_id,
        revalidate=lambda: actor,
        environ={"SIMON_OPENAI_API_KEY": "synthetic-key"},
        transport=httpx.MockTransport(respond),
    )
    return handler, definitions, context, requests


def test_generate_single_image_exact_contract_and_new_workspace_file(runtime, tmp_path):
    handler, definitions, context, requests = runtime
    result = handler(definitions[0], {"prompt": "An original blue square"}, context)
    assert len(requests) == 1 and requests[0].method == "POST"
    assert str(requests[0].url) == IMAGE_ENDPOINT
    assert json.loads(requests[0].content) == {
        "model": "operator-image-model",
        "prompt": "An original blue square",
        "size": "1024x1024",
        "quality": "low",
        "n": 1,
        "output_format": "png",
    }
    assert requests[0].headers["Authorization"] == "Bearer synthetic-key"
    assert requests[0].headers["X-Client-Request-Id"] == str(context.invocation_id)
    content = (tmp_path / result["path"]).read_bytes()
    assert result["path"] == f"generated-{context.invocation_id}.png"
    assert content == image_bytes() and result["sha256"] == hashlib.sha256(content).hexdigest()
    assert result["external_cost_usd"] is None and result["media_type"] == "image/png"
    with pytest.raises(ValidationError, match="already"):
        handler(definitions[0], {"prompt": "Do not charge again"}, context)
    assert len(requests) == 1


def test_audio_transcription_uses_validated_bytes_and_writes_artifact(runtime, tmp_path):
    handler, definitions, context, requests = runtime
    raw = wav_bytes()
    (tmp_path / "input-recording.wav").write_bytes(raw)
    result = handler(
        definitions[1],
        {
            "input": "input-recording.wav",
            "expected_sha256": hashlib.sha256(raw).hexdigest(),
        },
        context,
    )
    assert len(requests) == 1 and str(requests[0].url) == AUDIO_ENDPOINT
    assert requests[0].headers["Content-Type"].startswith("multipart/form-data; boundary=")
    content = requests[0].content
    assert b'filename="audio.wav"' in content and raw in content
    assert b"operator-transcription-model" in content and b"json" in content
    assert str(tmp_path).encode() not in content
    assert result["text"] == (tmp_path / result["path"]).read_text() == "A useful transcript."
    assert not result["truncated"]


@pytest.mark.parametrize(
    "input_name",
    [
        "../outside.wav",
        "folder/audio.wav",
        "C:/audio.wav",
        ".env",
        "audio.mp3",
        "folder\\audio.wav",
    ],
)
def test_audio_rejects_host_or_nested_paths_before_network(runtime, input_name):
    handler, definitions, context, requests = runtime
    with pytest.raises((AuthorizationError, ValidationError)):
        handler(definitions[1], {"input": input_name}, context)
    assert not requests


@pytest.mark.parametrize(
    "raw",
    [
        b"not audio",
        b"RIFF\x00\x00\x00\x00WAVE",
        wav_bytes()[:-10],
    ],
    ids=["invalid", "header_only", "truncated"],
)
def test_audio_rejects_invalid_or_truncated_input_without_cost(runtime, tmp_path, raw):
    handler, definitions, context, requests = runtime
    (tmp_path / "input.wav").write_bytes(raw)
    with pytest.raises(ValidationError):
        handler(definitions[1], {"input": "input.wav"}, context)
    assert not requests


def test_audio_hash_and_duration_limits(runtime, tmp_path):
    handler, definitions, context, requests = runtime
    path = tmp_path / "input.wav"
    path.write_bytes(wav_bytes())
    with pytest.raises(ValidationError, match="changed"):
        handler(definitions[1], {"input": "input.wav", "expected_sha256": "0" * 64}, context)
    path.write_bytes(wav_bytes(seconds=601))
    with pytest.raises(ValidationError, match="ten minutes"):
        handler(definitions[1], {"input": "input.wav"}, context)
    assert not requests


def test_audio_reparse_race_cannot_follow_host_link(tmp_path, monkeypatch):
    outside = tmp_path / "outside.wav"
    outside.write_bytes(wav_bytes())
    workspace = tmp_path / "task"
    workspace.mkdir()
    link = workspace / "input.wav"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("OS requires symlink privileges")
    # Simulate the path being replaced after the pre-open lstat check. The OS-level
    # no-follow open must still reject the redirected leaf before reading bytes.
    monkeypatch.setattr(module, "reject_links", lambda path: None)
    with pytest.raises(ValidationError, match="redirects"):
        _read_audio(workspace, TranscribeAudio(input="input.wav"))


@pytest.mark.parametrize(
    "changes",
    [
        {"actor_id": uuid4()},
        {"workspace_id": uuid4()},
        {"run_id": uuid4()},
        {"agent_id": "other"},
        {"authorized_action": "read"},
        {"scopes": frozenset({"jobs:read"})},
        {"allowed_tool_ids": frozenset()},
    ],
)
def test_no_call_outside_bound_assignment(runtime, changes):
    handler, definitions, context, requests = runtime
    with pytest.raises(AuthorizationError):
        handler(definitions[0], {"prompt": "A picture"}, context.model_copy(update=changes))
    assert not requests


def test_operator_model_key_and_policy_required_before_call(runtime):
    handler, definitions, context, requests = runtime
    for changes in (
        {"settings": {"model": "", "network": True}},
        {"endpoint": "https://other.example.test/v1/images/generations"},
        {"side_effect": False, "action_policy": "read"},
        {"settings": {"model": "selected", "network": False}},
        {"credential_env": "UNSET"},
    ):
        with pytest.raises(ToolCatalogError):
            handler(definitions[0].model_copy(update=changes), {"prompt": "A picture"}, context)
    assert not requests


def test_no_audio_is_sent_after_revocation_while_reading(runtime, tmp_path, monkeypatch):
    handler, definitions, context, requests = runtime
    (tmp_path / "input.wav").write_bytes(wav_bytes())
    original = module._read_audio

    def read_then_revoke(workspace, request):
        raw = original(workspace, request)
        handler.revalidate = lambda: handler.actor.model_copy(update={"scopes": frozenset()})
        return raw

    monkeypatch.setattr(module, "_read_audio", read_then_revoke)
    with pytest.raises(AuthorizationError):
        handler(definitions[1], {"input": "input.wav"}, context)
    assert not requests


def test_revocation_after_paid_result_is_unknown_and_not_published(runtime, tmp_path):
    handler, definitions, context, _ = runtime

    def respond(request):
        handler.revalidate = lambda: handler.actor.model_copy(update={"scopes": frozenset()})
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(image_bytes()).decode()}],
            },
        )

    handler._transport = httpx.MockTransport(respond)
    with pytest.raises(ToolExecutionError) as raised:
        handler(definitions[0], {"prompt": "A picture"}, context)
    assert raised.value.unknown and not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "status,unknown", [(400, False), (401, False), (429, False), (500, True), (307, True)]
)
def test_provider_rejection_is_not_retried_or_redirected(runtime, status, unknown):
    handler, definitions, context, requests = runtime

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://other.example.test/"},
            json={"error": {"message": "do not expose sensitive details"}},
        )

    handler._transport = httpx.MockTransport(respond)
    with pytest.raises(ToolExecutionError) as raised:
        handler(definitions[0], {"prompt": "A picture"}, context)
    assert raised.value.unknown is unknown and len(requests) == 1
    assert "sensitive" not in str(raised.value)


@pytest.mark.parametrize(
    "data",
    [
        {"data": [{"url": "https://other.example.test/image.png"}]},
        {"data": [{"b64_json": "not valid base64"}]},
        {"data": []},
        {"data": [{"b64_json": base64.b64encode(b"not PNG").decode()}]},
        {"data": [{"b64_json": base64.b64encode(image_bytes(1, 1)).decode()}]},
    ],
)
def test_bad_image_response_is_unknown_and_no_provider_urls_are_followed(runtime, tmp_path, data):
    handler, definitions, context, requests = runtime

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=data)

    handler._transport = httpx.MockTransport(respond)
    with pytest.raises(ToolExecutionError) as raised:
        handler(definitions[0], {"prompt": "A picture"}, context)
    assert raised.value.unknown and len(requests) == 1 and not list(tmp_path.iterdir())


def test_connection_loss_does_not_retry_or_leak_provider_details(runtime):
    handler, definitions, context, requests = runtime

    def respond(request):
        requests.append(request)
        raise httpx.ReadTimeout("Bearer sensitive-token", request=request)

    handler._transport = httpx.MockTransport(respond)
    with pytest.raises(ToolExecutionError) as raised:
        handler(definitions[0], {"prompt": "A picture"}, context)
    assert raised.value.unknown and len(requests) == 1 and "sensitive" not in str(raised.value)


def test_output_collision_after_provider_does_not_overwrite(runtime, tmp_path):
    handler, definitions, context, _ = runtime
    destination = tmp_path / f"generated-{context.invocation_id}.png"

    def respond(request):
        destination.write_bytes(b"Earlier output")
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(image_bytes()).decode()}],
            },
        )

    handler._transport = httpx.MockTransport(respond)
    with pytest.raises(ToolExecutionError) as raised:
        handler(definitions[0], {"prompt": "A picture"}, context)
    assert raised.value.unknown and destination.read_bytes() == b"Earlier output"


def test_templates_have_no_implicit_model_or_enabled_paid_tools():
    for definition in generative_tool_definitions():
        assert not definition.enabled and not definition.configured
        assert definition.settings == {"model": "", "network": True}
        assert definition.side_effect and definition.action_policy == "write"
        assert generative_configuration_reason(definition) == "Configure an exact provider model ID"


@pytest.mark.parametrize("operation", ["image", "audio"])
def test_decoded_output_limits_report_unknown_paid_outcome(
    runtime,
    tmp_path,
    monkeypatch,
    operation,
):
    handler, definitions, context, requests = runtime
    if operation == "image":
        monkeypatch.setattr(module, "MAX_IMAGE_BYTES", 100)
        tool, arguments = definitions[0], {"prompt": "A picture"}
    else:
        monkeypatch.setattr(module, "MAX_TRANSCRIPT_BYTES", 8)
        (tmp_path / "input.wav").write_bytes(wav_bytes())
        tool, arguments = definitions[1], {"input": "input.wav"}
    with pytest.raises(ToolExecutionError) as raised:
        handler(tool, arguments, context)
    assert raised.value.unknown and len(requests) == 1
    assert not list(tmp_path.glob("generated-*"))


def test_audio_byte_limit_prevents_network(runtime, tmp_path, monkeypatch):
    handler, definitions, context, requests = runtime
    (tmp_path / "input.wav").write_bytes(wav_bytes())
    monkeypatch.setattr(module, "MAX_AUDIO_BYTES", 100)
    with pytest.raises(ValidationError, match="24 MB"):
        handler(definitions[1], {"input": "input.wav"}, context)
    assert not requests


def test_response_deadline_prevents_infinite_stream(runtime, monkeypatch):
    handler, definitions, context, requests = runtime
    times = iter([0, 500])
    monkeypatch.setattr(module, "monotonic", lambda: next(times))
    with pytest.raises(ToolExecutionError) as raised:
        handler(definitions[0], {"prompt": "A picture"}, context)
    assert raised.value.unknown and len(requests) == 1
