"""Checks for the Vonage telephony path.

Skips when Pipecat is not installed, so the plain demo still runs on a bare
machine:

    pip install -r agentcore_app/requirements-pipecat.txt
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def _pipecat_or_skip():
    pytest.importorskip("pipecat", reason="pip install -r agentcore_app/requirements-pipecat.txt")
    pytest.importorskip("pipecat.services.aws.nova_sonic.llm",
                        reason="needs the [aws-nova-sonic] extra, not [aws]")


@pytest.fixture
def session():
    from agentcore_app.pipecat_server import STORE
    from agent.session import CallSession
    return CallSession(STORE, "+14165550101")


def test_tool_definitions_are_not_restated_for_pipecat(session):
    """One definition for every runtime. If Pipecat had its own copy of the
    tool list, a description could drift and the model would behave differently
    on the phone than in the demo."""
    _pipecat_or_skip()
    from agentcore_app.pipecat_server import build_function_schemas
    from agent.tools import TOOL_SPECS

    schemas = build_function_schemas(session)
    assert [s.name for s in schemas] == [t["name"] for t in TOOL_SPECS]
    for schema, spec in zip(schemas, TOOL_SPECS):
        assert schema.description == spec["description"]
        assert schema.properties == spec["inputSchema"]["json"].get("properties", {})
        assert schema.required == spec["inputSchema"]["json"].get("required", [])


def test_no_pipecat_tool_exposes_a_tenant_argument(session):
    _pipecat_or_skip()
    from agentcore_app.pipecat_server import build_function_schemas
    for schema in build_function_schemas(session):
        assert not any("tenant" in p.lower() or "office" in p.lower()
                       for p in schema.properties)


@pytest.mark.asyncio
async def test_tool_handler_runs_against_this_offices_inventory(session):
    """The handler closes over one office. North York's listing must not be
    reachable from Harbourfront's call."""
    _pipecat_or_skip()
    from agentcore_app.pipecat_server import build_function_schemas

    by_name = {s.name: s for s in build_function_schemas(session)}
    captured = {}

    class Params:
        arguments = {"listing_id": "C13870448"}     # North York's

        async def result_callback(self, result):
            captured["result"] = result

    await by_name["get_listing_details"].handler(Params())
    assert captured["result"]["error"] == "not_found"


def test_sonic_service_builds_for_every_office():
    """Catches a bad voice id, a malformed prompt or a tool schema Sonic will
    not accept, without making a single phone call."""
    _pipecat_or_skip()
    from agentcore_app.pipecat_server import (build_function_schemas, STORE,
                                              MODEL_ID)
    from agent.session import CallSession
    from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService

    for tenant in STORE._tenants.values():
        s = CallSession(STORE, tenant.phone_number)
        service = AWSNovaSonicLLMService(
            access_key_id="test", secret_access_key="test", region="us-east-1",
            settings=AWSNovaSonicLLMService.Settings(
                model=MODEL_ID, voice=tenant.voice_id,
                system_instruction=s.system_prompt,
                temperature=0.3, max_tokens=300),
            tools=build_function_schemas(s),
        )
        assert service is not None


def test_settings_form_is_the_non_deprecated_one():
    """Pipecat 1.12 deprecated passing system_instruction and voice_id straight
    to the constructor. Using the old form still works and warns, and warnings
    get ignored until they become errors."""
    _pipecat_or_skip()
    import warnings
    from agentcore_app.pipecat_server import (build_function_schemas, STORE,
                                              MODEL_ID)
    from agent.session import CallSession
    from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService

    s = CallSession(STORE, "+14165550101")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        AWSNovaSonicLLMService(
            access_key_id="test", secret_access_key="test", region="us-east-1",
            settings=AWSNovaSonicLLMService.Settings(
                model=MODEL_ID, voice=s.tenant.voice_id,
                system_instruction=s.system_prompt),
            tools=build_function_schemas(s),
        )
    ours = [str(w.message) for w in caught
            if "system_instruction" in str(w.message) or "voice_id" in str(w.message)]
    assert ours == [], ours


def test_session_continuation_is_on_and_inside_the_eight_minute_ceiling():
    """A Nova Sonic session is capped at roughly eight minutes. A buyer asking
    about schools, parking and the fee gets past that, and without continuation
    the call dies mid-sentence."""
    _pipecat_or_skip()
    import inspect
    from agentcore_app import pipecat_server

    source = inspect.getsource(pipecat_server.run_pipeline)
    assert "SessionContinuationParams" in source
    assert "enabled=True" in source
    threshold = 360.0
    assert f"transition_threshold_seconds={threshold}" in source
    assert threshold < 8 * 60, "transition must start before the AWS ceiling"


def test_vonage_not_twilio():
    """Twilio Media Streams strips query parameters off the WebSocket URL, so
    the SigV4 pre-signed signature AgentCore needs never arrives. If someone
    swaps the serializer to Twilio, this is the reminder why not."""
    _pipecat_or_skip()
    import inspect
    from agentcore_app import pipecat_server

    source = inspect.getsource(pipecat_server)
    assert "VonageFrameSerializer" in source
    assert "TwilioFrameSerializer" not in source
