"""Checks for the thing that gets deployed to AgentCore Runtime.

These skip when the voice dependencies are not installed, so the plain demo
still runs on a machine with nothing but beautifulsoup and boto3. Install them
with:

    pip install -r agentcore_app/requirements.txt
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


@pytest.fixture(scope="module")
def client():
    pytest.importorskip("fastapi", reason="pip install -r agentcore_app/requirements.txt")
    from fastapi.testclient import TestClient
    from agentcore_app.voice_server import app
    return TestClient(app)


def test_ping_answers_without_any_voice_dependency(client):
    """AgentCore polls /ping and stops routing traffic to a runtime that fails
    it. It must therefore answer even when Bedrock is unreachable, which is why
    the Strands import is deferred into the WebSocket handler rather than done
    at module load."""
    r = client.get("/ping")
    assert r.status_code == 200
    assert r.json()["status"] == "Healthy"


def test_ping_does_not_touch_bedrock_or_dynamo(client, monkeypatch):
    """A health check that calls a dependency turns one slow service into a
    dead runtime."""
    import boto3
    def explode(*a, **k):
        raise AssertionError("/ping must not open an AWS client")
    monkeypatch.setattr(boto3, "client", explode)
    monkeypatch.setattr(boto3, "resource", explode)
    assert client.get("/ping").status_code == 200


def test_unrouted_number_is_refused_not_defaulted(client):
    """The failure mode this guards: a misconfigured number silently falling
    through to whichever office loaded first, so a caller hears the wrong
    inventory."""
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?to=%2B14165559999") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error"
            raise RuntimeError("closed")


def test_every_tool_is_registered_with_strands():
    pytest.importorskip("strands", reason="pip install -r agentcore_app/requirements.txt")
    from agentcore_app.voice_server import STORE, build_tools
    from agent.session import CallSession

    tools = build_tools(CallSession(STORE, "+14165550101"))
    names = {t.tool_name for t in tools}
    assert names == {"search_listings", "get_listing_details",
                     "get_neighbourhood", "book_viewing", "transfer_to_human"}


def test_no_tool_exposes_a_tenant_argument():
    """If the model can name the office, a caller can talk it into naming a
    different one. The office comes from the dialed number only."""
    pytest.importorskip("strands", reason="pip install -r agentcore_app/requirements.txt")
    from agentcore_app.voice_server import STORE, build_tools
    from agent.session import CallSession

    for t in build_tools(CallSession(STORE, "+14165550101")):
        props = t.tool_spec["inputSchema"]["json"].get("properties", {})
        assert not any("tenant" in p.lower() or "office" in p.lower() for p in props)


def test_tools_are_bound_to_the_dialed_offices_inventory():
    pytest.importorskip("strands", reason="pip install -r agentcore_app/requirements.txt")
    from agentcore_app.voice_server import STORE, build_tools
    from agent.session import CallSession

    harbourfront = {t.tool_name: t for t in
                    build_tools(CallSession(STORE, "+14165550101"))}
    northyork = {t.tool_name: t for t in
                 build_tools(CallSession(STORE, "+14165550102"))}

    # C13870448 is North York's. Asking Harbourfront's tools for it must fail.
    assert harbourfront["get_listing_details"](listing_id="C13870448")["error"] \
        == "not_found"
    assert northyork["get_listing_details"](listing_id="C13870448")["listing_id"] \
        == "C13870448"


def test_each_office_gets_its_own_voice():
    """The voice id is read from the office record and passed to Sonic, so two
    offices do not answer the phone sounding identical."""
    from agentcore_app.voice_server import STORE
    assert STORE.tenant_for_number("+14165550101").voice_id == "matthew"
    assert STORE.tenant_for_number("+14165550102").voice_id == "tiffany"


def test_voice_ids_are_ones_nova_sonic_actually_has():
    """Nova 2 Sonic accepts a fixed set of voice names. A typo here is not an
    error at deploy time, it is a failed call."""
    from agentcore_app.voice_server import STORE
    known = {"matthew", "tiffany"}
    for tenant in STORE._tenants.values():
        assert tenant.voice_id in known, (
            f"{tenant.tenant_id} asks for voice {tenant.voice_id!r}, "
            f"which Nova 2 Sonic does not have")
