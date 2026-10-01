"""Optional real-vase delivery smoke using an isolated account and memory server."""

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from simon.adapters.tool_preflight import INSTALLED_TRANSPORTS
from simon.agent_setup import starter_manifest
from simon.config import Settings
from simon.domain.identity import Membership
from simon.services.agent_platform import AgentPlatformService
from tests.integration.test_agent_work_browser import agent_ui  # noqa: F401

pytestmark = pytest.mark.browser


def test_real_vase_files_preview_download_privacy_and_engineering_catalog(request, tmp_path):
    from playwright.sync_api import expect

    location = os.environ.get("SIMON_VASE_SMOKE_WORKSPACE")
    if not location:
        pytest.skip("Set SIMON_VASE_SMOKE_WORKSPACE to a successful vase_smoke.py workspace")
    workspace = Path(location)
    names = ("lilt-vase.scad", "lilt-vase.stl", "lilt-vase.3mf", "lilt-vase.blend",
             "lilt-vase.png", "geometry-validation.json", "lilt-vase-readme.txt")
    originals = {name: (workspace / name).read_bytes() for name in names}
    geometry = json.loads(originals["geometry-validation.json"])["lilt-vase.stl"]
    assert geometry["watertight"] and geometry["vase"]["open_mouth"]
    assert hashlib.sha256(originals["lilt-vase.stl"]).hexdigest() == geometry["sha256"]

    page, _, container = request.getfixturevalue("agent_ui")
    container.connected.settings = container.connected.settings.model_copy(update={
        "local_files_enabled": True, "local_files_dir": tmp_path / "files",
    })
    origin = container.settings.public_origin
    page.locator("#local-files-open").click()
    panel = page.locator("#local-files-panel")
    for name, content in originals.items():
        expect(panel.locator("#local-file-upload")).to_be_enabled()
        with page.expect_response("**/v1/local-files/upload?*") as uploaded:
            panel.locator("#local-file-upload").set_input_files({
                "name": name, "mimeType": "application/octet-stream", "buffer": content,
            })
        assert uploaded.value.status == 200, uploaded.value.text()
        row = panel.locator("article").filter(has=page.locator("strong", has_text=name))
        expect(row).to_be_visible()
        params = {"root": "workspace", "path": name}
        response = page.request.get(origin + "/v1/local-files/download", params=params)
        assert response.status == 200
        assert hashlib.sha256(response.body()).digest() == hashlib.sha256(content).digest()
        assert response.headers["content-disposition"].startswith("attachment;")
        assert str(workspace) not in response.headers["content-disposition"]

    source_row = panel.locator("article").filter(
        has=page.locator("strong", has_text="lilt-vase.scad"),
    )
    source_row.get_by_role("button", name="Read", exact=True).click()
    expect(source_row.locator("pre")).to_contain_text("height = 180")
    with page.expect_download() as downloaded:
        source_row.get_by_role("link", name="Download", exact=True).click()
    assert Path(downloaded.value.path()).read_bytes() == originals["lilt-vase.scad"]

    image_row = panel.locator("article").filter(
        has=page.locator("strong", has_text="lilt-vase.png"),
    )
    with page.expect_response("**/v1/local-files/preview?*") as preview:
        image_row.get_by_role("button", name="Preview image").click()
    assert preview.value.status == 200
    assert preview.value.body() == originals["lilt-vase.png"]
    assert preview.value.headers["content-type"] == "image/png"
    assert "sandbox;" in preview.value.headers["content-security-policy"]
    image = image_row.get_by_role("img", name="lilt-vase.png")
    expect(image).to_be_visible()
    page.wait_for_function("document.querySelector('.local-file-image')?.naturalWidth === 1200")
    image.scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / "real-vase-preview-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    assert panel.evaluate("el => el.scrollWidth <= el.clientWidth")
    image.scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / "real-vase-preview-mobile.png"), animations="disabled")

    cookie = next(item["value"] for item in page.context.cookies()
                  if item["name"] == "simon_session")
    _, actor = container.identity.resolve(cookie)
    other_id = uuid4()
    container.store.put_membership(Membership(
        actor_id=other_id, household_id=actor.household_id, role="owner",
        display_name="Separate synthetic account", household_name="Test household",
    ))
    other_token, _ = container.identity._issue(other_id, actor.household_id, "development")
    with httpx.Client(base_url=origin, trust_env=False) as anonymous:
        for endpoint in ("download", "preview"):
            path = f"/v1/local-files/{endpoint}"
            params = {"root": "workspace", "path": "lilt-vase.png"}
            assert anonymous.get(path, params=params).status_code == 401
            assert anonymous.get(path, params=params, headers={
                "Cookie": "simon_session=" + other_token,
            }).status_code == 422
    panel.get_by_role("button", name="Close", exact=True).click()
    page.set_viewport_size({"width": 1440, "height": 1080})

    manifest = starter_manifest(Settings(
        _env_file=None, model_provider="local", openai_api_key=None,
    ))
    replacement = AgentPlatformService(
        container.store, manifest, state_dir=tmp_path / "engineering",
        available_transports=INSTALLED_TRANSPORTS, environ={},
    )
    container.agent_platform.__dict__.update(replacement.__dict__)
    with page.expect_response("**/v1/agent-platform/catalog") as catalog_response:
        page.locator("#agent-refresh").click()
    catalog = catalog_response.value.json()
    engineering = next(team for team in catalog["teams"] if team["id"] == "engineering")
    assert engineering["agent_ids"] == ["cad-designer", "pcb-designer", "reviewer"]
    assert {"cad.openscad_export", "cad.mesh_inspect", "cad.render_mesh"} <= {
        tool["id"] for tool in catalog["tool_statuses"]
    }
    page.get_by_role("tab", name="Tools & setup").click()
    page.get_by_role("searchbox", name="Find a tool").fill("cad.openscad_export")
    expect(page.locator("#agent-tool-results .agent-catalog-item")).to_have_count(1)
    expect(page.locator('[data-tool-id="cad.openscad_export"]')).to_contain_text("Can make changes")
    page.get_by_role("button", name="New agent plan", exact=True).click()
    page.get_by_label("Team", exact=True).select_option("engineering")
    expect(page.get_by_label("Agent profile").locator("option")).to_have_count(3)
    page.get_by_label("Agent profile").select_option("cad-designer")
    expect(page.locator(".agent-profile-description")).to_contain_text("parametric CAD")
    page.get_by_label("Agent profile").select_option("pcb-designer")
    expect(page.locator(".agent-profile-description")).to_contain_text("KiCad")
    page.screenshot(path=str(tmp_path / "engineering-plan-desktop.png"), animations="disabled")
    page.keyboard.press("Escape")
