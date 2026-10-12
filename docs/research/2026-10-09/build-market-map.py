"""Build a dependency-free, offline market explorer from the dated research JSON.

Run from any directory: python docs/research/2026-10-09/build-market-map.py
The output embeds its data and works directly through file://. Evidence is a dated
documentation snapshot, not a score, tested product result, or release promise.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AXES = (
    "projectWork",
    "artifactReview",
    "memoryEvidence",
    "durability",
    "costControls",
    "deployment",
)
STATES = {"documented", "announced", "not_established", "not_applicable"}
RELATIONSHIPS = {"direct", "adjacent", "substitute", "infrastructure"}
EXPECTED_FILES = (
    "enterprise-and-infrastructure.json",
    "execution-and-creative-platforms.json",
    "knowledge-and-research-platforms.json",
    "workforce-platforms.json",
)
STREAM_NAMES = {
    "enterprise-and-infrastructure.json": "Enterprise & infrastructure",
    "execution-and-creative-platforms.json": "Execution & creative",
    "knowledge-and-research-platforms.json": "Knowledge & research",
    "workforce-platforms.json": "Workforce & project work",
}


def capability(state: str, note: str, *urls: str) -> dict:
    return {"state": state, "note": note, "urls": list(urls)}


def simon_baseline() -> dict:
    artifacts = "../../runbooks/native-artifacts.md"
    execution = "../../runbooks/native-execution.md"
    models = "../../runbooks/project-models.md"
    projects = "../../runbooks/native-projects.md"
    return {
        "name": "Simon · repository baseline",
        "category": "Local repository baseline",
        "relationship": "direct",
        "customer": (
            "Current repository/pilot operators; a commercial customer profile "
            "has not been validated."
        ),
        "status": (
            "Implemented repository core; not a complete commercial release "
            "or comparative production benchmark."
        ),
        "summary": (
            "Native project work, bounded artifacts, separate review and exact acceptance, "
            "leased model execution and shared resource accounting."
        ),
        "strength": (
            "Explicit artifact/review/owner decisions and current-authority checks, "
            "with durable records and bounded repair tasks."
        ),
        "opportunity": (
            "Validate one useful repeated workflow, comparative output quality and buyer demand; "
            "accepted business tools, broad retrieval, rich validation, physical resource "
            "admission and hosted release remain open."
        ),
        "pricing": (
            "No commercial tariff established. Current model reservations and usage accounting "
            "are not a subscription or complete tool/compute/storage cost model."
        ),
        "capabilities": {
            "projectWork": capability(
                "documented",
                "Native projects, task board, accountable agent roles "
                "and bounded dependency inputs. "
                "Execution completion and manual task Done do not themselves accept a deliverable.",
                projects,
                execution,
                artifacts,
            ),
            "artifactReview": capability(
                "documented",
                "One logical artifact per task; files up to 1 MiB "
                "and up to 100 immutable revisions. "
                "Separate reviewer role, exact revision/hash/task/policy binding and owner or "
                "deliberately configured agent-only acceptance. Automatic review reads bounded "
                "UTF-8 up to 48,000 bytes; this is not broad quality certification.",
                artifacts,
            ),
            "memoryEvidence": capability(
                "documented",
                "Review uses bounded current intake excerpts and repair feedback; stored revisions "
                "and decisions retain history. A comprehensive claim/source-impact graph, broad "
                "permission-aware retrieval and memory revocation "
                "are not established by this baseline.",
                artifacts,
                execution,
            ),
            "durability": capability(
                "documented",
                "PostgreSQL checkpoints, fenced leases, correlated waits, dispatch markers and "
                "unknown-outcome reconciliation. Unknown calls retain liabilities and call slots, "
                "including zero-fee calls. These contracts do not establish exactly-once arbitrary "
                "external business effects.",
                execution,
                models,
            ),
            "costControls": capability(
                "documented",
                "Qualified model routes, project/workspace monetary and concurrency limits, "
                "pre-dispatch reservations and known/unknown usage settlement. Complete tool, "
                "compute, storage and customer billing remain subsequent work.",
                models,
            ),
            "deployment": capability(
                "documented",
                "Repository-controlled PostgreSQL controller and administrator-approved "
                "local/hosted model endpoints. The local flag is an administrator assertion; "
                "runners coordinate server-side model work rather than executing arbitrary "
                "workstation tools or moving inference to the runner host. End-to-end private "
                "hosting is not demonstrated here.",
                execution,
                models,
            ),
        },
        "sources": [
            {"title": "Native projects", "url": projects},
            {"title": "Artifacts and review", "url": artifacts},
            {"title": "Native execution", "url": execution},
            {"title": "Project models and limits", "url": models},
        ],
        "baseline": True,
        "stream": "Simon baseline",
    }


def load_profiles() -> tuple[list[dict], dict]:
    files = sorted(
        set(ROOT.glob("*-platforms.json")) | {ROOT / "enterprise-and-infrastructure.json"}
    )
    profiles = []
    seen = set()
    streams = []
    for source in files:
        if not source.exists():
            continue
        entries = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(entries, list):
            raise ValueError(f"{source.name}: expected an array")
        stream = STREAM_NAMES.get(source.name, source.stem.replace("-", " ").title())
        streams.append({"name": stream, "file": source.name, "count": len(entries)})
        for entry in entries:
            for field in (
                "name",
                "category",
                "relationship",
                "customer",
                "status",
                "summary",
                "strength",
                "opportunity",
                "pricing",
                "capabilities",
                "sources",
            ):
                if field not in entry:
                    raise ValueError(f"{source.name}: {entry.get('name', '?')} lacks {field}")
            if entry["name"] in seen:
                raise ValueError(f"Duplicate product name: {entry['name']}")
            seen.add(entry["name"])
            if entry["relationship"] not in RELATIONSHIPS:
                raise ValueError(f"Invalid relationship for {entry['name']}")
            for axis in AXES:
                item = entry["capabilities"].get(axis, {})
                if (
                    item.get("state") not in STATES
                    or not item.get("note")
                    or not isinstance(item.get("urls"), list)
                ):
                    raise ValueError(f"Invalid {axis} evidence for {entry['name']}")
            profiles.append({**entry, "stream": stream, "baseline": False})
    profiles.sort(key=lambda item: item["name"].casefold())
    urls = {
        link
        for item in profiles
        for link in (
            [source["url"] for source in item["sources"]]
            + [url for evidence in item["capabilities"].values() for url in evidence["urls"]]
        )
        if link.startswith("https://") or link.startswith("http://")
    }
    metadata = {
        "date": "October 9, 2026",
        "productCount": len(profiles),
        "sourceCount": len(urls),
        "streams": streams,
        "missing": [name for name in EXPECTED_FILES if not (ROOT / name).exists()],
    }
    profiles.insert(0, simon_baseline())
    for index, item in enumerate(profiles):
        item["id"] = f"product-{index}"
    return profiles, metadata


HTML = r"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="color-scheme" content="light" />
    <meta
      http-equiv="Content-Security-Policy"
      content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'"
    />
    <title>Simon — market research explorer</title>
    <style>
      :root {
        --ink: #17312f;
        --muted: #5c6e6b;
        --paper: #f5f5ee;
        --panel: #fff;
        --line: #dbe3dd;
        --accent: #1b6151;
        --accent-soft: #e7f3eb;
        --ochre: #946014;
        --unknown: #707676;
        --announced: #875c10;
        --radius: 15px;
      }
      * {
        box-sizing: border-box;
      }
      body {
        margin: 0;
        background: var(--paper);
        color: var(--ink);
        font:
          15px/1.55 -apple-system,
          BlinkMacSystemFont,
          'Segoe UI',
          sans-serif;
      }
      button,
      input,
      select {
        font: inherit;
      }
      button,
      select,
      input[type='checkbox'] {
        cursor: pointer;
      }
      button {
        color: inherit;
      }
      button:disabled {
        cursor: default;
        opacity: 0.55;
      }
      button:focus-visible,
      input:focus-visible,
      select:focus-visible,
      a:focus-visible,
      summary:focus-visible {
        outline: 3px solid #c27c25;
        outline-offset: 3px;
      }
      a {
        color: var(--accent);
        text-underline-offset: 3px;
      }
      a:hover {
        text-decoration-thickness: 2px;
      }
      .shell {
        max-width: 1440px;
        margin: 0 auto;
        padding: 34px 38px 120px;
      }
      .topline {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 18px;
        margin-bottom: 32px;
        font-size: 12px;
        letter-spacing: 0.1em;
        text-transform: uppercase;
      }
      .brand {
        display: flex;
        align-items: center;
        gap: 10px;
        font-weight: 750;
      }
      .brand-mark {
        display: grid;
        place-items: center;
        width: 32px;
        height: 32px;
        border-radius: 9px;
        background: var(--ink);
        color: white;
        font-size: 20px;
        text-transform: none;
        letter-spacing: 0;
      }
      .date {
        color: var(--muted);
        text-align: right;
      }
      .hero {
        display: grid;
        grid-template-columns: 1.7fr 1fr;
        align-items: end;
        gap: 48px;
        margin-bottom: 28px;
      }
      .eyebrow {
        margin: 0 0 9px;
        color: var(--accent);
        font-size: 12px;
        letter-spacing: 0.13em;
        font-weight: 750;
        text-transform: uppercase;
      }
      h1 {
        font-size: clamp(34px, 4vw, 56px);
        line-height: 1.08;
        letter-spacing: -0.045em;
        margin: 0 0 18px;
        font-weight: 700;
      }
      h2,
      h3,
      h4,
      p {
        margin-top: 0;
      }
      .intro {
        max-width: 690px;
        color: var(--muted);
        font-size: 16px;
        margin: 0;
      }
      .stats {
        display: flex;
        justify-content: flex-end;
        gap: 28px;
        padding-bottom: 5px;
      }
      .stat strong {
        display: block;
        font-size: 34px;
        line-height: 1.1;
        font-weight: 650;
        letter-spacing: -0.04em;
      }
      .stat span {
        display: block;
        margin-top: 8px;
        color: var(--muted);
        font-size: 12px;
      }
      .evidence {
        border: 1px solid var(--line);
        border-radius: var(--radius);
        background: #edf1e9;
        padding: 15px 19px;
        margin-bottom: 28px;
      }
      .evidence-top {
        display: flex;
        align-items: baseline;
        flex-wrap: wrap;
        gap: 10px 22px;
      }
      .evidence strong {
        font-weight: 650;
      }
      .evidence p {
        color: var(--muted);
        margin: 5px 0 0;
        font-size: 13px;
      }
      .legend {
        display: flex;
        flex-wrap: wrap;
        gap: 10px 20px;
        font-size: 12px;
      }
      .status-dot {
        display: inline-block;
        width: 8px;
        height: 8px;
        margin-right: 7px;
        border-radius: 50%;
        background: var(--unknown);
      }
      .documented .status-dot {
        background: var(--accent);
      }
      .announced .status-dot {
        background: var(--announced);
      }
      .not_established .status-dot {
        background: transparent;
        border: 1.5px solid var(--unknown);
      }
      .not_applicable .status-dot {
        background: #bfc7c3;
        border-radius: 2px;
      }
      .filters {
        padding: 22px;
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: var(--radius);
        margin-bottom: 23px;
      }
      .filter-grid {
        display: grid;
        grid-template-columns: 1.5fr 1.2fr 1fr 1fr;
        gap: 15px;
      }
      label.control {
        display: grid;
        gap: 7px;
        font-size: 12px;
        font-weight: 650;
      }
      input[type='search'],
      select {
        width: 100%;
        min-width: 0;
        padding: 11px 12px;
        height: 46px;
        color: var(--ink);
        background: #fafbf8;
        border: 1px solid #cdd7d0;
        border-radius: 8px;
        font-size: 14px;
      }
      .relation-line {
        display: flex;
        align-items: center;
        gap: 12px;
        flex-wrap: wrap;
        margin-top: 18px;
      }
      .relation-line > span {
        color: var(--muted);
        font-size: 12px;
      }
      .pills {
        display: flex;
        flex-wrap: wrap;
        gap: 7px;
      }
      .pill,
      .secondary {
        border: 1px solid var(--line);
        border-radius: 7px;
        background: white;
        padding: 7px 11px;
        font-size: 12px;
        font-weight: 600;
      }
      .pill[aria-pressed='true'] {
        background: var(--ink);
        color: white;
        border-color: var(--ink);
      }
      .reset {
        margin-left: auto;
        border: none;
        background: none;
        padding: 7px 2px;
        color: var(--accent);
        font-size: 12px;
        text-decoration: underline;
      }
      .results-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 15px;
        margin-bottom: 15px;
      }
      .results-head h2 {
        font-size: 18px;
        font-weight: 650;
        margin: 0;
      }
      .result-count {
        color: var(--muted);
        font-size: 13px;
      }
      .grid {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 17px;
      }
      .card {
        display: flex;
        flex-direction: column;
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: var(--radius);
        padding: 22px;
        min-width: 0;
        transition:
          border-color 0.16s,
          box-shadow 0.16s;
      }
      .card.selected {
        border-color: var(--accent);
        box-shadow: 0 0 0 1px var(--accent);
      }
      .card.baseline {
        background: #eef5ee;
        border-color: #b2ccba;
      }
      .card-meta {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
        font-size: 11px;
        color: var(--muted);
        margin-bottom: 12px;
      }
      .category {
        max-width: 74%;
      }
      .relationship {
        white-space: nowrap;
        font-weight: 650;
        color: var(--accent);
      }
      .card h3 {
        font-size: 20px;
        line-height: 1.25;
        letter-spacing: -0.02em;
        font-weight: 650;
        margin: 0 0 12px;
      }
      .card-summary {
        font-size: 13px;
        color: var(--muted);
        margin-bottom: 17px;
      }
      .axes {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 6px;
        margin: 0 0 18px;
        padding: 0;
        list-style: none;
      }
      .axis {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 5px;
        min-width: 0;
        background: #f5f7f3;
        border: 1px solid #e6ece6;
        border-radius: 5px;
        padding: 5px 7px;
        font-size: 10px;
      }
      .axis .status-dot {
        width: 6px;
        height: 6px;
        margin-right: 4px;
      }
      .axis-label {
        font-weight: 550;
      }
      .axis-state {
        color: var(--muted);
        white-space: nowrap;
      }
      .card-baseline {
        font-size: 12px;
        color: var(--accent);
        margin: 0 0 15px;
        font-weight: 650;
      }
      .card-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-top: auto;
        padding-top: 15px;
        border-top: 1px solid var(--line);
      }
      .text-button {
        background: none;
        border: none;
        padding: 3px 0;
        color: var(--accent);
        font-size: 12px;
        font-weight: 650;
        text-decoration: underline;
        text-underline-offset: 3px;
      }
      .compare-choice {
        display: flex;
        align-items: center;
        gap: 7px;
        font-size: 12px;
        user-select: none;
      }
      input[type='checkbox'] {
        width: 16px;
        height: 16px;
        margin: 0;
        accent-color: var(--accent);
      }
      .empty {
        grid-column: 1/-1;
        padding: 45px 24px;
        background: white;
        border: 1px dashed var(--line);
        border-radius: var(--radius);
        text-align: center;
      }
      .empty h3 {
        margin-bottom: 7px;
      }
      .empty p {
        color: var(--muted);
        margin: 0;
      }
      .research-notes {
        margin-top: 28px;
        font-size: 13px;
        color: var(--muted);
      }
      .research-notes summary {
        font-weight: 650;
        cursor: pointer;
      }
      .research-links {
        display: flex;
        gap: 15px;
        flex-wrap: wrap;
        margin: 13px 0;
      }
      .research-notes ul {
        padding-left: 20px;
      }
      .tray {
        position: fixed;
        z-index: 5;
        bottom: 0;
        left: 0;
        right: 0;
        background: var(--ink);
        color: white;
        border-top: 1px solid #35524b;
        box-shadow: 0 -5px 25px #17312f16;
      }
      .tray-inner {
        max-width: 1440px;
        margin: 0 auto;
        padding: 16px 38px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 20px;
      }
      .tray-main {
        min-width: 0;
      }
      .tray-label {
        font-size: 12px;
        color: #cddcd5;
        margin-bottom: 8px;
      }
      .tray-items {
        display: flex;
        align-items: center;
        gap: 7px;
        flex-wrap: wrap;
      }
      .tray-placeholder {
        font-size: 12px;
        color: #d5e1d9;
      }
      .selected-chip {
        display: flex;
        align-items: center;
        gap: 8px;
        color: white;
        background: #2b4a41;
        border: 1px solid #567166;
        border-radius: 6px;
        padding: 4px 7px;
        font-size: 11px;
      }
      .selected-chip span {
        max-width: 210px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .selected-chip button {
        background: none;
        border: none;
        color: #dfeae5;
        padding: 1px 4px;
        font-size: 17px;
        line-height: 1;
      }
      .tray-actions {
        display: flex;
        align-items: center;
        gap: 13px;
        flex-shrink: 0;
      }
      .primary {
        border: 1px solid var(--accent);
        background: var(--accent);
        color: white;
        border-radius: 8px;
        padding: 10px 15px;
        font-size: 13px;
        font-weight: 650;
      }
      .tray .primary {
        background: #ecf2db;
        color: var(--ink);
        border-color: #ecf2db;
      }
      .tray-clear {
        color: #e5eee8;
        background: none;
        border: none;
        padding: 6px 0;
        font-size: 12px;
      }
      .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip: rect(0, 0, 0, 0);
        white-space: nowrap;
        border: 0;
      }
      dialog {
        color: var(--ink);
        border: 1px solid var(--line);
        border-radius: 18px;
        padding: 0;
        width: min(950px, calc(100% - 40px));
        max-height: 90vh;
        box-shadow: 0 24px 90px #112b3650;
      }
      dialog::backdrop {
        background: #0c2426aa;
        backdrop-filter: blur(3px);
      }
      .dialog-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 20px;
        padding: 24px 28px 20px;
        border-bottom: 1px solid var(--line);
        background: #f9fbf7;
      }
      .dialog-head h2 {
        font-size: 25px;
        line-height: 1.2;
        letter-spacing: -0.025em;
        margin: 0 0 8px;
      }
      .dialog-head p {
        margin: 0;
        color: var(--muted);
        font-size: 13px;
      }
      .close {
        flex-shrink: 0;
        border: 1px solid var(--line);
        background: white;
        border-radius: 7px;
        padding: 6px 10px;
        font-size: 12px;
      }
      .detail-body {
        padding: 26px 28px;
      }
      .detail-summary {
        color: var(--muted);
        font-size: 15px;
        margin-bottom: 20px;
      }
      .detail-facts {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 16px;
        margin-bottom: 24px;
      }
      .fact h3 {
        margin: 0 0 5px;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.07em;
        color: var(--accent);
      }
      .fact p {
        font-size: 13px;
        margin: 0;
      }
      .detail-axes {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 13px;
      }
      .capability-box {
        border: 1px solid var(--line);
        border-radius: 9px;
        padding: 15px;
        min-width: 0;
      }
      .capability-box h3 {
        font-size: 14px;
        margin: 0 0 7px;
      }
      .state-label {
        color: var(--muted);
        font-size: 11px;
        display: block;
        margin-bottom: 8px;
      }
      .capability-box p {
        font-size: 13px;
        margin: 0 0 8px;
      }
      .source-links {
        display: flex;
        flex-wrap: wrap;
        gap: 5px 12px;
      }
      .source-links a {
        font-size: 11px;
        overflow-wrap: anywhere;
      }
      .full-sources {
        margin-top: 23px;
        font-size: 13px;
      }
      .full-sources h3 {
        font-size: 15px;
      }
      .full-sources ul {
        margin-bottom: 0;
        padding-left: 19px;
      }
      .full-sources li {
        margin-bottom: 6px;
        overflow-wrap: anywhere;
      }
      .detail-actions {
        display: flex;
        align-items: center;
        gap: 15px;
        padding: 18px 28px;
        border-top: 1px solid var(--line);
        background: #f9fbf7;
      }
      #comparison-dialog {
        width: min(1240px, calc(100% - 40px));
      }
      .comparison-scroll {
        overflow: auto;
      }
      .comparison-table {
        width: 100%;
        border-collapse: collapse;
        table-layout: fixed;
      }
      .comparison-table th,
      .comparison-table td {
        text-align: left;
        vertical-align: top;
        border-right: 1px solid var(--line);
        border-bottom: 1px solid var(--line);
        padding: 17px;
        font-size: 13px;
        overflow-wrap: anywhere;
      }
      .comparison-table th:last-child,
      .comparison-table td:last-child {
        border-right: 0;
      }
      .comparison-table .row-label {
        width: 150px;
        color: var(--accent);
        font-size: 12px;
        background: #f7f9f4;
      }
      .comparison-table .product-heading {
        font-size: 17px;
        line-height: 1.3;
        background: #edf3eb;
      }
      .comparison-table p {
        margin: 0 0 8px;
      }
      .comparison-table .source-links {
        margin-top: 9px;
      }
      .comparison-foot {
        padding: 17px 28px;
        color: var(--muted);
        font-size: 12px;
      }
      .partial-warning {
        color: #765015;
        margin-bottom: 15px;
      }
      @media (min-width: 1500px) {
        .shell {
          padding-top: 44px;
        }
      }
      @media (max-width: 1100px) {
        .grid {
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .hero {
          gap: 25px;
        }
        .stats {
          gap: 20px;
        }
      }
      @media (max-width: 760px) {
        .shell {
          padding: 22px 18px 150px;
        }
        .topline {
          margin-bottom: 26px;
          font-size: 10px;
        }
        .hero {
          grid-template-columns: 1fr;
          gap: 23px;
        }
        .stats {
          justify-content: flex-start;
          gap: 32px;
        }
        .stat strong {
          font-size: 29px;
        }
        .filter-grid {
          grid-template-columns: 1fr 1fr;
        }
        .control:first-child {
          grid-column: 1/-1;
        }
        .control:nth-child(2) {
          grid-column: 1/-1;
        }
        .filters {
          padding: 17px;
        }
        .grid {
          gap: 13px;
        }
        .card {
          padding: 17px;
        }
        .card h3 {
          font-size: 18px;
        }
        .axes {
          grid-template-columns: 1fr;
        }
        .tray-inner {
          padding: 12px 18px;
          gap: 15px;
        }
        .selected-chip span {
          max-width: 140px;
        }
        .tray-actions {
          flex-direction: column-reverse;
          align-items: flex-end;
          gap: 4px;
        }
        .evidence-top {
          gap: 9px;
        }
        .detail-facts,
        .detail-axes {
          grid-template-columns: 1fr;
        }
        .dialog-head {
          padding: 20px;
        }
        .detail-body {
          padding: 20px;
        }
        .comparison-table {
          min-width: 760px;
        }
        .comparison-table .row-label {
          width: 120px;
        }
        dialog {
          width: calc(100% - 20px);
        }
        .detail-actions {
          padding: 16px 20px;
        }
      }
      @media (max-width: 520px) {
        .grid {
          grid-template-columns: 1fr;
        }
        .axes {
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .card {
          padding: 21px;
        }
        .intro {
          font-size: 14px;
        }
        .results-head {
          align-items: baseline;
        }
        .results-head h2 {
          font-size: 16px;
        }
        .result-count {
          font-size: 11px;
        }
        .evidence {
          padding: 14px;
        }
        .legend {
          gap: 6px 13px;
          font-size: 11px;
        }
        .relation-line {
          gap: 9px;
        }
        .relation-line > span {
          width: 100%;
        }
        .reset {
          margin-left: 0;
        }
        .tray-inner {
          align-items: flex-end;
        }
        .tray-label {
          font-size: 11px;
        }
        .tray-placeholder {
          font-size: 11px;
        }
        .selected-chip span {
          max-width: 128px;
        }
        .tray .primary {
          padding: 9px 11px;
          font-size: 12px;
        }
        .tray-items {
          max-width: 220px;
          gap: 4px;
        }
      }
      @media (prefers-reduced-motion: reduce) {
        * {
          transition: none !important;
          scroll-behavior: auto !important;
        }
      }
      @media print {
        .filters,
        .tray,
        .card-footer,
        .close,
        .detail-actions {
          display: none !important;
        }
        body {
          background: white;
        }
        .shell {
          padding: 0;
        }
        .grid {
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .card {
          break-inside: avoid;
        }
        dialog[open] {
          position: static;
          max-height: none;
          width: 100%;
          box-shadow: none;
        }
        body:has(dialog[open]) .shell {
          display: none;
        }
        .comparison-scroll {
          overflow: visible;
        }
        .comparison-table {
          min-width: 0;
        }
      }
    </style>
  </head>
  <body>
    <main class="shell">
      <div class="topline">
        <div class="brand">
          <span class="brand-mark" aria-hidden="true">s</span> Simon / Research
        </div>
        <span class="date">Evidence snapshot · <span id="snapshot-date"></span></span>
      </div>
      <header class="hero">
        <div>
          <p class="eyebrow">The competitive landscape</p>
          <h1>Where work agents<br />meet real delivery.</h1>
          <p class="intro">
            Explore comparable tools, the stacks that can substitute for Simon, and the
            infrastructure it can reuse. Read the evidence before choosing a direction.
          </p>
        </div>
        <div class="stats" aria-label="Research coverage">
          <div class="stat"><strong id="product-count"></strong><span>mapped products</span></div>
          <div class="stat">
            <strong id="source-count"></strong><span>distinct source URLs</span>
          </div>
          <div class="stat"><strong>6</strong><span>capability lenses</span></div>
        </div>
      </header>
      <section class="evidence" aria-label="How to interpret evidence">
        <div class="evidence-top">
          <strong>Documentation is evidence, not a test result.</strong>
          <div class="legend">
            <span class="documented"><i class="status-dot" aria-hidden="true"></i>Documented</span
            ><span class="announced"><i class="status-dot" aria-hidden="true"></i>Announced</span
            ><span class="not_established"
              ><i class="status-dot" aria-hidden="true"></i>Not established</span
            ><span class="not_applicable"
              ><i class="status-dot" aria-hidden="true"></i>Not applicable</span
            >
          </div>
        </div>
        <p>
          Not established means unresolved, not absent. Each note describes the narrower supported
          behavior. Preview availability, pricing and configuration vary. A documented approval or
          check does not imply independent exact-version quality acceptance.
        </p>
      </section>
      <p id="missing-warning" class="partial-warning" hidden></p>
      <section class="filters" aria-label="Filter the market">
        <div class="filter-grid">
          <label class="control" for="search"
            >Search products and research notes<input
              id="search"
              type="search"
              placeholder="Try source changes, local, approvals…"
          /></label>
          <label class="control" for="category"
            >Product category<select id="category">
              <option value="">All categories</option>
            </select></label
          >
          <label class="control" for="capability"
            >Capability lens<select id="capability">
              <option value="">Any capability</option>
            </select></label
          >
          <label class="control" for="evidence-state"
            >Documentation state<select id="evidence-state">
              <option value="">All states</option>
              <option value="documented">Documented</option>
              <option value="announced">Announced</option>
              <option value="not_established">Not established</option>
              <option value="not_applicable">Not applicable</option>
            </select></label
          >
        </div>
        <div class="relation-line">
          <span>Relationship to Simon</span>
          <div
            class="pills"
            id="relationship-filters"
            role="group"
            aria-label="Relationship to Simon"
          >
            <button class="pill" data-relationship="" aria-pressed="true">All</button
            ><button class="pill" data-relationship="direct" aria-pressed="false">Direct</button
            ><button class="pill" data-relationship="adjacent" aria-pressed="false">Adjacent</button
            ><button class="pill" data-relationship="substitute" aria-pressed="false">
              Substitute</button
            ><button class="pill" data-relationship="infrastructure" aria-pressed="false">
              Infrastructure
            </button>
          </div>
          <button id="reset" class="reset">Reset filters</button>
        </div>
      </section>
      <section aria-labelledby="results-title">
        <div class="results-head">
          <h2 id="results-title">Explore the landscape</h2>
          <output class="result-count" id="result-count" aria-live="polite"></output>
        </div>
        <div class="grid" id="product-grid"></div>
      </section>
      <details class="research-notes">
        <summary>Research scope, definitions and supporting reports</summary>
        <p>
          Products are alphabetical, with Simon's local baseline separated at the beginning.
          Capability labels are qualitative documentation states, not scores. Relationship labels
          describe the research perspective, not universal market categories.
        </p>
        <p>
          Direct: overlaps Simon's intended work product. Adjacent: solves a nearby job or could
          extend into it. Substitute: an alternative way to achieve the job. Infrastructure: a
          component for implementing the product. Simon is a repository baseline rather than a
          competing commercial release.
        </p>
        <p>
          If a documentation state is selected without a capability lens, products match when at
          least one lens has that state. Pricing units cannot be compared as equivalent task costs.
        </p>
        <div class="research-links">
          <a href="index.md">Research index</a><a href="strategy.md">Strategy</a
          ><a href="opportunity-map.md">Opportunity catalog</a
          ><a href="challenge-product-and-distribution.md">Buyer & distribution challenge</a
          ><a href="challenge-systems-and-economics.md">Systems & economics challenge</a>
        </div>
        <ul id="input-files"></ul>
        <p>
          All data is embedded. This page makes no background network requests and needs no server.
          Rebuild after editing the research JSON with
          <code>python docs/research/2026-10-09/build-market-map.py</code>.
        </p>
      </details>
    </main>
    <aside class="tray" aria-label="Product comparison selection">
      <div class="tray-inner">
        <div class="tray-main">
          <div class="tray-label" id="tray-label">Compare up to three products</div>
          <div class="tray-items" id="tray-items"></div>
        </div>
        <div class="tray-actions">
          <button class="tray-clear" id="clear-selection" hidden>Clear selection</button
          ><button class="primary" id="open-comparison" disabled>Compare evidence</button>
        </div>
      </div>
    </aside>
    <p class="sr-only" id="selection-status" role="status" aria-live="polite"></p>
    <dialog id="detail-dialog" aria-labelledby="detail-title">
      <div class="dialog-head">
        <div>
          <h2 id="detail-title"></h2>
          <p id="detail-subtitle"></p>
        </div>
        <button class="close" data-close="detail-dialog">Close</button>
      </div>
      <div class="detail-body" id="detail-content"></div>
      <div class="detail-actions">
        <button id="detail-compare" class="primary">Add to comparison</button
        ><span class="result-count" id="detail-selection-note"></span>
      </div>
    </dialog>
    <dialog id="comparison-dialog" aria-labelledby="comparison-title">
      <div class="dialog-head">
        <div>
          <h2 id="comparison-title">Compare the documented evidence</h2>
          <p>
            Read notes and sources together. The same state can describe very different
            capabilities.
          </p>
        </div>
        <button class="close" data-close="comparison-dialog">Close</button>
      </div>
      <div class="comparison-scroll" id="comparison-content"></div>
      <div class="comparison-foot">
        Documented does not mean tested or equivalent. Absence of public evidence does not prove
        absence of functionality. Current Simon limits are preserved in its repository baseline.
      </div>
    </dialog>
    <!-- prettier-ignore -->
    <script type="application/json" id="market-data">
__MARKET_DATA__
    </script>
    <script>
      'use strict';
      const market = JSON.parse(document.getElementById('market-data').textContent);
      const products = market.products;
      const axes = [
        { key: 'projectWork', label: 'Project work', short: 'Work' },
        { key: 'artifactReview', label: 'Artifacts & review', short: 'Review' },
        { key: 'memoryEvidence', label: 'Memory & evidence', short: 'Memory' },
        { key: 'durability', label: 'Durability & recovery', short: 'Continuity' },
        { key: 'costControls', label: 'Cost controls', short: 'Costs' },
        { key: 'deployment', label: 'Deployment & models', short: 'Hosting' },
      ];
      const labels = {
        documented: 'Documented',
        announced: 'Announced',
        not_established: 'Not established',
        not_applicable: 'Not applicable',
      };
      const shortLabels = {
        documented: 'Docs',
        announced: 'Announced',
        not_established: 'Unresolved',
        not_applicable: 'N/A',
      };
      const selected = new Set();
      let relationship = '';
      let detailId = null;
      const byId = new Map(products.map((product) => [product.id, product]));
      const $ = (id) => document.getElementById(id);
      const make = (tag, className, text) => {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (text !== undefined) element.textContent = text;
        return element;
      };
      function sourceLink(url, title) {
        const anchor = make('a', '', title);
        if (/^https?:\/\//i.test(url)) {
          anchor.href = url;
          anchor.target = '_blank';
          anchor.rel = 'noopener noreferrer';
        } else if (/^(?:\.\.\/)+(?:runbooks\/)?[a-z0-9/_-]+\.md$/i.test(url)) {
          anchor.href = url;
        } else {
          anchor.removeAttribute('href');
        }
        return anchor;
      }
      function sourceTitle(product, url, index) {
        const named = product.sources.find((source) => source.url === url);
        return named ? named.title : 'Source ' + (index + 1);
      }
      function evidenceLinks(product, evidence) {
        const links = make('div', 'source-links');
        evidence.urls.forEach((url, index) =>
          links.append(sourceLink(url, sourceTitle(product, url, index))),
        );
        return links;
      }
      function statusLabel(evidence) {
        const label = make('span', 'state-label ' + evidence.state);
        const dot = make('i', 'status-dot');
        dot.setAttribute('aria-hidden', 'true');
        label.append(dot, document.createTextNode(labels[evidence.state]));
        return label;
      }
      function searchable(product) {
        return [
          product.name,
          product.category,
          product.relationship,
          product.stream,
          product.customer,
          product.status,
          product.summary,
          product.strength,
          product.opportunity,
          product.pricing,
          ...axes.map((axis) => product.capabilities[axis.key].note),
        ]
          .join(' ')
          .toLocaleLowerCase();
      }
      const searchIndex = new Map(products.map((product) => [product.id, searchable(product)]));
      function filteredProducts() {
        const terms = $('search').value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
        const category = $('category').value,
          axis = $('capability').value,
          state = $('evidence-state').value;
        return products.filter((product) => {
          if (category && product.category !== category) return false;
          if (relationship && (product.baseline || product.relationship !== relationship))
            return false;
          if (terms.some((term) => !searchIndex.get(product.id).includes(term))) return false;
          if (state && axis && product.capabilities[axis].state !== state) return false;
          if (
            state &&
            !axis &&
            !axes.some((item) => product.capabilities[item.key].state === state)
          )
            return false;
          return true;
        });
      }
      function renderCards() {
        const filtered = filteredProducts();
        const fragment = document.createDocumentFragment();
        filtered.forEach((product) => {
          const card = make('article', 'card' + (product.baseline ? ' baseline' : ''));
          card.dataset.id = product.id;
          const meta = make('div', 'card-meta');
          meta.append(
            make('span', 'category', product.category),
            make(
              'span',
              'relationship',
              product.baseline
                ? 'Local baseline'
                : product.relationship[0].toUpperCase() + product.relationship.slice(1),
            ),
          );
          const title = make('h3', '', product.name);
          title.id = product.id + '-title';
          card.setAttribute('aria-labelledby', title.id);
          card.append(meta, title, make('p', 'card-summary', product.summary));
          if (product.baseline)
            card.append(
              make('p', 'card-baseline', 'Repository evidence · not a commercial release'),
            );
          const list = make('ul', 'axes');
          list.setAttribute('aria-label', 'Capability documentation');
          axes.forEach((axis) => {
            const evidence = product.capabilities[axis.key];
            const item = make('li', 'axis ' + evidence.state);
            item.title = axis.label + ': ' + labels[evidence.state] + '. ' + evidence.note;
            item.setAttribute('aria-label', item.title);
            const shortState = make('span', 'axis-state');
            const dot = make('i', 'status-dot');
            dot.setAttribute('aria-hidden', 'true');
            shortState.append(dot, document.createTextNode(shortLabels[evidence.state]));
            item.append(make('span', 'axis-label', axis.short), shortState);
            list.append(item);
          });
          card.append(list);
          const footer = make('div', 'card-footer');
          const detail = make('button', 'text-button', 'Read evidence');
          detail.dataset.detail = product.id;
          detail.setAttribute('aria-label', 'Read evidence for ' + product.name);
          const choice = make('label', 'compare-choice');
          const checkbox = make('input');
          checkbox.type = 'checkbox';
          checkbox.dataset.compare = product.id;
          checkbox.setAttribute('aria-label', 'Compare ' + product.name);
          choice.append(checkbox, make('span', '', 'Compare'));
          footer.append(detail, choice);
          card.append(footer);
          fragment.append(card);
        });
        if (!filtered.length) {
          const empty = make('div', 'empty');
          empty.append(
            make('h3', '', 'No products match these filters'),
            make(
              'p',
              '',
              'Try a broader search or reset the filters. Unknown evidence is kept visible.',
            ),
          );
          fragment.append(empty);
        }
        $('product-grid').replaceChildren(fragment);
        const baselineCount = filtered.filter((product) => product.baseline).length;
        $('result-count').textContent =
          filtered.length -
          baselineCount +
          ' of ' +
          market.metadata.productCount +
          ' products' +
          (baselineCount ? ' · 1 Simon baseline' : '');
        updateSelection();
      }
      function updateSelection() {
        document.querySelectorAll('[data-compare]').forEach((checkbox) => {
          checkbox.checked = selected.has(checkbox.dataset.compare);
          checkbox.disabled = selected.size === 3 && !checkbox.checked;
          checkbox.closest('.card').classList.toggle('selected', checkbox.checked);
        });
        $('tray-label').textContent =
          selected.size === 3
            ? 'Three selected · remove one to choose another'
            : selected.size + ' of 3 selected';
        const chips = document.createDocumentFragment();
        selected.forEach((id) => {
          const product = byId.get(id),
            chip = make('div', 'selected-chip'),
            remove = make('button', '', '\u00d7');
          remove.dataset.remove = id;
          remove.setAttribute('aria-label', 'Remove ' + product.name + ' from comparison');
          chip.append(make('span', '', product.name), remove);
          chips.append(chip);
        });
        if (!selected.size)
          chips.append(
            make(
              'span',
              'tray-placeholder',
              'Choose products on the cards to compare their evidence.',
            ),
          );
        $('tray-items').replaceChildren(chips);
        $('open-comparison').disabled = !selected.size;
        $('clear-selection').hidden = !selected.size;
        if (detailId) {
          const already = selected.has(detailId);
          $('detail-compare').textContent = already
            ? 'Remove from comparison'
            : 'Add to comparison';
          $('detail-compare').disabled = !already && selected.size === 3;
          $('detail-selection-note').textContent =
            selected.size === 3 && !already
              ? 'Remove a selected product first.'
              : selected.size + ' of 3 selected';
        }
      }
      function toggleSelection(id) {
        if (selected.has(id)) {
          selected.delete(id);
          $('selection-status').textContent =
            byId.get(id).name + ' removed. ' + selected.size + ' selected.';
        } else if (selected.size < 3) {
          selected.add(id);
          $('selection-status').textContent =
            byId.get(id).name + ' added. ' + selected.size + ' selected.';
        } else {
          $('selection-status').textContent =
            'Comparison is limited to three products. Remove one first.';
        }
        updateSelection();
      }
      function showDetail(id) {
        const product = byId.get(id);
        detailId = id;
        $('detail-title').textContent = product.name;
        $('detail-subtitle').textContent =
          product.category +
          ' · ' +
          (product.baseline ? 'Local repository baseline' : product.relationship) +
          ' · ' +
          product.stream;
        const content = document.createDocumentFragment();
        content.append(make('p', 'detail-summary', product.summary));
        const facts = make('div', 'detail-facts');
        [
          ['Status', product.status],
          ['Customer', product.customer],
          ['Strongest advantage', product.strength],
          ['Simon opportunity / hypothesis', product.opportunity],
          ['Pricing & conditions', product.pricing],
        ].forEach(([label, value]) => {
          const fact = make('section', 'fact');
          fact.append(make('h3', '', label), make('p', '', value));
          facts.append(fact);
        });
        content.append(facts);
        const evidenceGrid = make('div', 'detail-axes');
        axes.forEach((axis) => {
          const evidence = product.capabilities[axis.key],
            box = make('section', 'capability-box');
          box.append(
            make('h3', '', axis.label),
            statusLabel(evidence),
            make('p', '', evidence.note),
            evidenceLinks(product, evidence),
          );
          evidenceGrid.append(box);
        });
        content.append(evidenceGrid);
        const sourceSection = make('section', 'full-sources');
        sourceSection.append(make('h3', '', 'Research sources'));
        const list = make('ul');
        product.sources.forEach((source) => {
          const item = make('li');
          item.append(sourceLink(source.url, source.title));
          list.append(item);
        });
        sourceSection.append(list);
        content.append(sourceSection);
        $('detail-content').replaceChildren(content);
        updateSelection();
        $('detail-dialog').showModal();
        $('detail-dialog').scrollTop = 0;
      }
      function showComparison() {
        if (!selected.size) return;
        const compared = [...selected].map((id) => byId.get(id));
        const table = make('table', 'comparison-table');
        table.style.minWidth = 150 + compared.length * 250 + 'px';
        const caption = make(
          'caption',
          'sr-only',
          'Selected product capabilities, evidence, pricing and sources',
        );
        table.append(caption);
        const head = make('thead'),
          heading = make('tr'),
          first = make('th', 'row-label', 'Research lens');
        first.scope = 'col';
        heading.append(first);
        compared.forEach((product) => {
          const cell = make('th', 'product-heading', product.name);
          cell.scope = 'col';
          heading.append(cell);
        });
        head.append(heading);
        table.append(head);
        const body = make('tbody');
        function row(label, render) {
          const line = make('tr'),
            heading = make('th', 'row-label', label);
          heading.scope = 'row';
          line.append(heading);
          compared.forEach((product) => {
            const cell = make('td');
            render(cell, product);
            line.append(cell);
          });
          body.append(line);
        }
        [
          [
            'Position',
            (product) =>
              product.category +
              ' · ' +
              (product.baseline ? 'Local baseline' : product.relationship),
          ],
          ['Customer', (product) => product.customer],
          ['Status', (product) => product.status],
          ['Summary', (product) => product.summary],
        ].forEach(([label, value]) =>
          row(label, (cell, product) => cell.append(make('p', '', value(product)))),
        );
        axes.forEach((axis) =>
          row(axis.label, (cell, product) => {
            const evidence = product.capabilities[axis.key];
            cell.append(
              statusLabel(evidence),
              make('p', '', evidence.note),
              evidenceLinks(product, evidence),
            );
          }),
        );
        [
          ['Strongest advantage', (product) => product.strength],
          ['Simon opportunity / hypothesis', (product) => product.opportunity],
          ['Pricing & conditions', (product) => product.pricing],
        ].forEach(([label, value]) =>
          row(label, (cell, product) => cell.append(make('p', '', value(product)))),
        );
        row('All sources', (cell, product) => {
          const links = make('div', 'source-links');
          product.sources.forEach((source) => links.append(sourceLink(source.url, source.title)));
          cell.append(links);
        });
        table.append(body);
        $('comparison-content').replaceChildren(table);
        $('comparison-dialog').showModal();
        $('comparison-dialog').scrollTop = 0;
      }
      $('snapshot-date').textContent = market.metadata.date;
      $('product-count').textContent = market.metadata.productCount;
      $('source-count').textContent = market.metadata.sourceCount;
      [...new Set(products.map((product) => product.category))].sort().forEach((category) => {
        const option = make('option', '', category);
        option.value = category;
        $('category').append(option);
      });
      axes.forEach((axis) => {
        const option = make('option', '', axis.label);
        option.value = axis.key;
        $('capability').append(option);
      });
      market.metadata.streams.forEach((stream) => {
        const item = make('li');
        const link = make('a', '', stream.name + ' — ' + stream.count + ' profiles');
        link.href = stream.file;
        item.append(link);
        $('input-files').append(item);
      });
      if (market.metadata.missing.length) {
        $('missing-warning').hidden = false;
        $('missing-warning').textContent =
          'Partial snapshot: missing ' +
          market.metadata.missing.join(', ') +
          '. Rebuild after those research files are available.';
      }
      $('search').addEventListener('input', renderCards);
      ['category', 'capability', 'evidence-state'].forEach((id) =>
        $(id).addEventListener('change', renderCards),
      );
      $('relationship-filters').addEventListener('click', (event) => {
        const button = event.target.closest('[data-relationship]');
        if (!button) return;
        relationship = button.dataset.relationship;
        document
          .querySelectorAll('[data-relationship]')
          .forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
        renderCards();
      });
      $('reset').addEventListener('click', () => {
        ['search', 'category', 'capability', 'evidence-state'].forEach((id) => ($(id).value = ''));
        relationship = '';
        document
          .querySelectorAll('[data-relationship]')
          .forEach((item) =>
            item.setAttribute('aria-pressed', String(item.dataset.relationship === '')),
          );
        renderCards();
      });
      $('product-grid').addEventListener('click', (event) => {
        const button = event.target.closest('[data-detail]');
        if (button) showDetail(button.dataset.detail);
      });
      $('product-grid').addEventListener('change', (event) => {
        if (event.target.matches('[data-compare]')) toggleSelection(event.target.dataset.compare);
      });
      $('tray-items').addEventListener('click', (event) => {
        const button = event.target.closest('[data-remove]');
        if (button) toggleSelection(button.dataset.remove);
      });
      $('clear-selection').addEventListener('click', () => {
        selected.clear();
        updateSelection();
        $('selection-status').textContent = 'Comparison selection cleared.';
      });
      $('open-comparison').addEventListener('click', showComparison);
      $('detail-compare').addEventListener('click', () => toggleSelection(detailId));
      document
        .querySelectorAll('[data-close]')
        .forEach((button) =>
          button.addEventListener('click', () => $(button.dataset.close).close()),
        );
      renderCards();
    </script>
  </body>
</html>
"""


def main() -> None:
    profiles, metadata = load_profiles()
    payload = json.dumps({"products": profiles, "metadata": metadata}, ensure_ascii=False, indent=2)
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    payload = "\n".join("      " + line for line in payload.splitlines())
    output = ROOT / "market-map.html"
    output.write_text(HTML.replace("__MARKET_DATA__", payload), encoding="utf-8", newline="\n")
    print(
        f"Built {output.name}: {metadata['productCount']} external profiles + 1 local baseline; "
        f"{metadata['sourceCount']} source URLs"
    )
    for stream in metadata["streams"]:
        print(f"  {stream['file']}: {stream['count']}")
    if metadata["missing"]:
        print("Missing inputs: " + ", ".join(metadata["missing"]))


if __name__ == "__main__":
    main()
