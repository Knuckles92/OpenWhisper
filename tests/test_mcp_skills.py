"""SEP-2640 Skills over MCP (io.modelcontextprotocol/skills), skill:// resources, and ZIP bundle."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from services.agent_mcp import setup, skills_ext
from services.agent_mcp.app import create_app
from services.database import DatabaseManager
from services.settings import SettingsManager

TOKEN = "mcp-skills-test-" + "b" * 32
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture
def mcp_client(tmp_path):
    db_path = tmp_path / "skills.db"
    db = DatabaseManager(str(db_path))
    settings = SettingsManager(str(tmp_path / "settings.json"))
    app = create_app(db_path, TOKEN, settings=settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client
    db.close()


def _rpc(client: TestClient, method: str, params: dict | None = None, req_id: int = 1):
    payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    response = client.post("/mcp", json=payload, headers=HEADERS)
    assert response.status_code == 200
    return response.json()


def test_initialize_advertises_skills_extension_and_resources(mcp_client):
    body = _rpc(
        mcp_client,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "skills-test", "version": "1"},
        },
    )
    caps = body["result"]["capabilities"]
    assert "resources" in caps
    assert skills_ext.EXTENSION_ID in caps["extensions"]
    assert caps["extensions"][skills_ext.EXTENSION_ID] == {}


def test_skills_list_get_and_resource_integrity(mcp_client):
    listed = _rpc(mcp_client, "skills/list", {})["result"]
    assert listed["resultType"] == "complete"
    assert listed["cacheScope"] == "private"
    assert listed["ttlMs"] > 0
    skills = listed["skills"]
    assert len(skills) == 1

    skill = skills[0]
    assert skill["uri"] == "skill://openwhisper/SKILL.md"
    assert skill["frontmatter"]["name"] == "openwhisper"
    assert "OpenWhisper" in skill["frontmatter"]["description"]
    uris = [r["uri"] for r in skill["resources"]]
    assert uris[0] == "skill://openwhisper/SKILL.md"
    assert "skill://openwhisper/references/tools.md" in uris
    assert "skill://openwhisper/references/organizing-and-notes.md" in uris
    assert "skill://openwhisper/references/workflows.md" in uris

    # skills/get returns the exact same manifest entry.
    fetched = _rpc(
        mcp_client, "skills/get", {"uri": "skill://openwhisper/SKILL.md"}
    )["result"]
    assert fetched["resultType"] == "complete"
    assert fetched["skill"] == skill

    # Unknown or missing URIs return JSON-RPC -32602.
    bad = _rpc(mcp_client, "skills/get", {"uri": "skill://missing/SKILL.md"})
    assert bad["error"]["code"] == skills_ext.INVALID_PARAMS
    missing = _rpc(mcp_client, "skills/get", {})
    assert missing["error"]["code"] == skills_ext.INVALID_PARAMS

    # Every file in the manifest is readable via resources/read and matches its SHA-256 + byte size.
    res_list = _rpc(mcp_client, "resources/list", {})["result"]["resources"]
    listed_uris = {r["uri"] for r in res_list}
    assert skills_ext.INDEX_URI in listed_uris
    for entry in skill["resources"]:
        assert entry["uri"] in listed_uris
        content_res = _rpc(mcp_client, "resources/read", {"uri": entry["uri"]})["result"]
        text = content_res["contents"][0]["text"]
        raw = text.encode("utf-8")
        assert len(raw) == entry["size"]
        assert "sha256:" + hashlib.sha256(raw).hexdigest() == entry["digest"]

    # skill://index.json parses cleanly and lists the skill.
    index_res = _rpc(mcp_client, "resources/read", {"uri": skills_ext.INDEX_URI})["result"]
    index_data = json.loads(index_res["contents"][0]["text"])
    assert index_data["extension"] == skills_ext.EXTENSION_ID
    assert index_data["skills"][0]["uri"] == "skill://openwhisper/SKILL.md"


def test_http_skill_catalog_and_zip_bundle_require_auth(mcp_client):
    assert mcp_client.get("/v1/skills").status_code == 401
    assert mcp_client.get("/v1/skills/openwhisper.zip").status_code == 401

    auth = {"Authorization": f"Bearer {TOKEN}"}
    catalog = mcp_client.get("/v1/skills", headers=auth)
    assert catalog.status_code == 200
    payload = catalog.json()
    assert payload["extension"] == skills_ext.EXTENSION_ID
    assert payload["skills"][0]["download_path"] == "/v1/skills/openwhisper.zip"

    archive_resp = mcp_client.get("/v1/skills/openwhisper.zip", headers=auth)
    assert archive_resp.status_code == 200
    assert archive_resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive_resp.content)) as zf:
        names = set(zf.namelist())
        assert "openwhisper/SKILL.md" in names
        assert "openwhisper/references/tools.md" in names
        assert "openwhisper/references/organizing-and-notes.md" in names
        assert "openwhisper/references/workflows.md" in names
        skill_md = zf.read("openwhisper/SKILL.md").decode("utf-8")
        assert "name: openwhisper" in skill_md
        assert "organize_records" in skill_md

    assert mcp_client.get("/v1/skills/unknown.zip", headers=auth).status_code == 404


def test_setup_prompts_reference_mcp_skill_and_bundle():
    prompt = setup.agent_prompt("http://127.0.0.1:8767/mcp", token=TOKEN)
    assert "skill://openwhisper/SKILL.md" in prompt
    assert "/v1/skills/openwhisper.zip" in prompt
    assert "get_history_digest" in prompt

    install_prompt = setup.skill_install_prompt("http://127.0.0.1:8767/mcp", token=TOKEN)
    assert f"Authorization: Bearer {TOKEN}" in install_prompt
    assert "skills/list" in install_prompt
    assert "http://127.0.0.1:8767/v1/skills/openwhisper.zip" in install_prompt
    assert "get_capabilities" in install_prompt
