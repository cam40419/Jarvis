# Optional image generation and file transcription

Simon provides `generative.image_generate` and `generative.audio_transcribe`; the reusable example manifest leaves them disabled. They use fixed OpenAI API endpoints and the existing server credential reference `SIMON_OPENAI_API_KEY`. Both require an owned Docker task workspace, `workspace.write`, the `jobs:read` and `jobs:write` scopes, and a profile with `max_action: "write"` and permission to use cloud tools.

Copy only the required entries from [generative-tools.example.json](../../examples/agents/generative-tools.example.json) into the operator manifest. Set each entry's `settings.model` to an exact model ID available to your API project, then set `enabled` and `configured` to `true`. No model is chosen implicitly. Grant its ID and scopes to a profile and allow a Docker environment with `workspace.write`. Restart API and dispatcher after manifest changes and create a new plan. Never put a credential directly into tool settings.

Suggested explicit configuration, checked against official documentation on September 30, 2026:

| Tool | Model ID | Compatibility |
| --- | --- | --- |
| Image generation | `gpt-image-2.5-flare-2026-09-08` | The [Flare model documentation](https://developers.openai.com/api/docs/models/gpt-image-2.5-flare) lists this fixed snapshot for everyday generation. The [Image API guide](https://developers.openai.com/api/docs/guides/image-generation) documents the adapter's PNG/base64, standard sizes and low/medium/high settings. |
| File transcription | `gpt-transcribe` | The [file transcription guide](https://developers.openai.com/api/docs/guides/speech-to-text) recommends this model for completed recordings with multipart upload and JSON text output. The [model page](https://developers.openai.com/api/docs/models/gpt-transcribe) lists the undated ID; no dated snapshot is assumed. |

This compatibility assessment uses the documented contracts; it does not verify a particular API project's model access. The existing server key can be referenced without copying its value. For new configuration, prefer these current models over the older GPT Image 1/1.5 and GPT-4o transcription families listed in the [deprecation schedule](https://developers.openai.com/api/docs/deprecations). Provider calls occur only when a queued task invokes its granted tool; they incur separate tool charges described below.

The image tool calls the [Image API generation endpoint](https://developers.openai.com/api/reference/python/resources/images/methods/generate) using a GPT Image-compatible model, one image, and PNG output. It accepts a prompt of up to 4,000 characters, one of the standard square/portrait/landscape sizes, and low/medium/high quality. It requests base64 output through the GPT Image contract, validates the returned PNG size, and writes at most 12 MiB to a new `generated-<invocation>.png` in the task workspace. It does not fetch provider-returned URLs. Legacy DALL-E models use a different output contract and are outside this adapter's supported configuration.

The audio tool uses the [file transcription endpoint](https://developers.openai.com/api/docs/guides/speech-to-text) with multipart audio and JSON output. Its local limits are stricter than the provider upload limit: mono/stereo PCM WAV, at most ten minutes and 24,000,000 bytes. Copy a WAV into the task workspace with `workspace.import_local`; alternatively, use `media.extract_audio` on imported media to produce a root-level WAV. Pass that single filename and optionally the current SHA-256. Absolute paths, subdirectories, symlinks/junctions, special files, malformed audio and stale hashes are rejected before sending bytes. Windows opens the leaf as a reparse point and checks its handle before reading, so a concurrently replaced link cannot redirect the upload.

Example tool arguments:

```json
{"prompt":"An original editorial illustration for a project report","size":"1024x1024","quality":"low"}
```

```json
{"input":"input-INVOCATION_UUID.wav","expected_sha256":"SHA256_FROM_IMPORT"}
```

Each tool returns the generated relative filename, byte count, hash, model and media type. Transcription also returns up to 24,000 characters to the worker and stores the complete bounded transcript as `generated-<invocation>.txt`. Include the returned filename in the worker's final `artifacts` list to publish it through the normal authenticated run download endpoint.

These provider calls are billable tools. Their charges are not included in the run's text-model budget estimate; results report `external_cost_usd: null` rather than guessing a price. Use provider-side spending limits appropriate to the selected account. The adapter sends one request per invocation, follows no redirects, and never automatically retries. A timeout, lost/malformed response, revoked access after dispatch, or failure to publish a paid result produces an unknown outcome requiring inspection. Repeating a successful invocation with an existing output is rejected before another provider request.

Tests use synthetic HTTP responses and local fixtures. Passing them validates request contracts, bounded I/O and authorization behavior; it does not establish model access, provider billing or the availability of a configured account.
