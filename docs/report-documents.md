# Formatted project reports

Accepted prose report files have an authenticated **Download Word document** link.
The server uses the saved source's name and project, formats its headings, paragraphs,
lists, tables and links, and stores an immutable DOCX locally before downloading or
uploading it. The original Markdown/text, task output, and run history stay intact.

Each generated document has a durable `platform.report_document` record linking its
source artifact ID, SHA-256, byte count, renderer version, and generated artifact.
Repeated downloads reuse verified local bytes. Authorization and any named project
copy are checked again even when a generated document is already cached.

Automatic conversion is limited to accepted `.md` and `.txt` report deliverables up
to 1,000,000 source bytes. Internal responses, planning records, drafts, and partial
outputs are excluded. An internal response can become a report only when explicitly
published to a meaningful project filename and that copy still exactly matches the
original. README, repository control files, source code, data files, and existing
PDF/DOCX/binary outputs keep their authored formats.

The Files view includes these explicitly published, verified response-derived reports
alongside authored file outputs. Their original response classification remains in
history; unpromoted responses and changed named copies stay out of the Files filter.

New eligible Drive uploads use the DOCX and a separate stable
`report-document:v<version>:<artifact>:<folder>:<account>` operation key. Local
publication precedes cloud transfer; a lost cloud response reconciles the same
provider-generated ID. Existing successful Markdown/text copies are retained rather
than silently overwritten or duplicated. Replacing those older remote files is a
separate reviewed operation, and remote edits are always preserved.

The API endpoint is
`GET /v1/projects/{project_id}/outputs/{run_id}/{artifact_id}/document`. Its name and
title come from server-owned source metadata, not query parameters. The generated
artifact is a derivative, so historical run/task records are never rewritten to add
it. Generation needs project read access and does not grant new agent or cloud tools.
