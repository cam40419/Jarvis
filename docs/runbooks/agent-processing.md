# Offline document and media processing

The `processing` transport runs seven fixed document/media tools in a leased
Docker worker. It uses local CPU processing and does not contact cloud services.

Build the worker image before enabling these tools:

```powershell
docker build --file deploy/Dockerfile.processing-worker --tag simon-processing:local .
python examples/agents/processing_smoke.py
```

The smoke script creates synthetic PDF, Markdown, and video inputs, exercises every
tool through a real lease, checks PDF/OCR text and output preservation, then releases
the container. Its synthetic files/journal remain under `.local/agents` for review.

Configure a Linux Docker environment using image `simon-processing:local`,
`network: "none"`, and capabilities `python`, `pdf`, `documents`, `ocr`, and `media`.
The tools require the relevant capability and Python, so narrower environments
can grant just the applications they install. The transport rejects environments
that permit network access. Follow the UID/GID and workspace instructions in
[agent environments](agent-environments.md).

Copy selected [definitions](../../examples/agents/processing-tools.example.json)
into the platform manifest and enable/configure them after provisioning. Grant
their IDs, environment ID, and the existing `jobs:read`/`jobs:write` scopes to the
agent profile. Writes require `max_action: "write"` or stronger. The actor also
needs the corresponding scope. Definitions can be generated using
`simon.adapters.processing_tools.processing_tool_definitions(enabled=True)`.

| Tool | Required application capability | Arguments and result |
| --- | --- | --- |
| `document.extract_pdf` | `pdf` | `input`, optional `max_pages` (20 default, 100 maximum); PDF text on stdout |
| `document.convert` | `documents` | `input`, `output`; Markdown/text/DOCX to a new Markdown/text/DOCX file |
| `image.ocr` | `ocr` | PNG/JPEG/TIFF `input`; English text on stdout |
| `media.inspect` | `media` | `input`; JSON stream metadata on stdout |
| `media.thumbnail` | `media` | `input`, PNG `output`, optional `seconds` and `width`; one video frame |
| `media.extract_audio` | `media` | `input`, WAV `output`, optional `duration_seconds`; mono 16 kHz PCM |
| `media.transcode` | `media` | `input`, MP4 `output`, optional `duration_seconds` and `width`; H.264/AAC video |

Inputs must be regular files inside the current lease workspace and at most
100 MiB. Paths cannot contain traversal, symlinks, or Git metadata. MP4, MOV, M4A,
MKV, WebM, MP3, WAV, FLAC, and OGG are the supported media inputs. Media demuxers
are selected explicitly from the extension; playlists and remote URLs are rejected.

New outputs are capped at 100 MiB and published only after the command succeeds.
Existing files are preserved; use a new filename for revisions. The tool result
contains an exit code, stdout, stderr, and truncation flag. A successful write's
stdout includes `output_path` and `size_bytes`. Include the relative output path in
the agent's final artifact list to publish it into Simon's durable artifact library.
Nonzero exit codes must be handled as failures.

Commands default to a 60-second timeout and 64 KiB of retained stdout/stderr.
Media durations default to 60 seconds, with a maximum of 600; width defaults to
1280 pixels and must be even, from 64 to 1920. The CPU and memory limits of the
environment remain in force. Long render supervision, GPU rendering, arbitrary
FFmpeg filters, and bulk full-length transcoding are outside these bounded tools.

The image uses the official Pandoc 3.11 static binary with an architecture-specific
release SHA256 check (amd64/arm64). Its embedded document data is needed for DOCX
support with `--sandbox`; the Debian Pandoc package lacks those embedded templates.
Pandoc uses its sandbox and fixed readers/writers; it does not accept filters,
custom templates, PDF engines, or external image embedding. OCR currently ships
English data only. PDF extraction reads embedded text; scanned PDFs first need a
separately approved page rasterization workflow. These tools do not provide speech
recognition, image generation, or cloud AI media generation.

References: [Pandoc sandbox](https://pandoc.org/MANUAL.html#option--sandbox),
[FFmpeg options](https://ffmpeg.org/ffmpeg.html), and
[Tesseract command usage](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html).
