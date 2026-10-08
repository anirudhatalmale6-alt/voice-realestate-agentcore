"""The Vonage path: a real phone call, through Pipecat, to Nova 2 Sonic.

This is the second of two servers in this folder, and the one I would ship.

  voice_server.py   a plain WebSocket. Fewer moving parts, good for a browser
                    client, and you have to write the telephony audio handling
                    yourself.
  pipecat_server.py this one. Pipecat owns the transport and the audio, so the
                    Vonage phone audio, the resampling and the barge-in are
                    library code rather than mine.

Why Vonage and not Twilio
-------------------------
AgentCore Runtime authenticates a WebSocket with SigV4 headers, a SigV4
pre-signed URL (signature in the query string), or OAuth. Twilio Media Streams
strips query parameters off the `wss://` URL it dials and gives you no way to
add headers, so the pre-signed signature never arrives and the connection is
rejected. Vonage passes the query string through. That one detail picks the
telephony provider, and it is not obvious until you have lost a day to it.

The part people hit in production
---------------------------------
A Nova Sonic session has an AWS-imposed limit of roughly eight minutes. A buyer
asking about schools, parking and the maintenance fee gets past that easily, and
without handling it the call simply dies mid-sentence. Pipecat's session
continuation opens the next session in the background before the limit, buffers
the caller's audio across the handover and keeps the conversation context, so
nobody hears it happen. It is on below, deliberately.

Run it locally:

    pip install -r agentcore_app/requirements-pipecat.txt
    python agentcore_app/pipecat_server.py
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.session import CallSession, UnroutedNumber, TenantSuspended  # noqa: E402
from agent.store import JsonStore, DynamoDbStore, normalise_e164        # noqa: E402
from agent.tools import TOOL_SPECS                                       # noqa: E402

log = logging.getLogger("pipecat_voice")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

MODEL_ID = os.getenv("MODEL_ID", "amazon.nova-2-sonic-v1:0")
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")
DATA_REGION = os.getenv("DATA_REGION", "ca-central-1")
TENANT_TABLE = os.getenv("TENANT_TABLE")

# Vonage sends 16 kHz PCM. Pipecat's Vonage serializer defaults to the same, so
# this only needs changing if the Vonage application is configured otherwise.
VONAGE_SAMPLE_RATE = int(os.getenv("VONAGE_SAMPLE_RATE", "16000"))

app = FastAPI()


def build_store():
    if TENANT_TABLE:
        return DynamoDbStore(TENANT_TABLE, region_name=DATA_REGION)
    root = Path(__file__).resolve().parents[1]
    return JsonStore(tenants_path=str(root / "config" / "tenants.json"),
                     listings_dir=str(root / "samples" / "listings"))


STORE = build_store()


@app.get("/ping")
async def ping():
    """AgentCore health check. Touches nothing, for the reason in
    docs/deploy.md: a /ping that calls a dependency turns one slow service into
    a dead runtime."""
    return {"status": "Healthy",
            "time_of_last_update": int(datetime.now().timestamp())}


@app.websocket("/ws")
async def phone_call(websocket: WebSocket) -> None:
    dialed = websocket.query_params.get("to") or os.getenv("DEFAULT_DIALED_NUMBER")
    caller_id = websocket.query_params.get("from")

    try:
        session = CallSession(STORE, normalise_e164(dialed or ""))
    except (UnroutedNumber, TenantSuspended) as exc:
        log.warning("refusing call to %r: %s", dialed, exc)
        await websocket.accept()
        await websocket.close(code=1008)
        return

    log.info("call tenant=%s dialed=%s from=%s",
             session.tenant.tenant_id, dialed, caller_id)

    await websocket.accept()
    try:
        await run_pipeline(websocket, session)
    except WebSocketDisconnect:
        log.info("caller hung up, tenant=%s", session.tenant.tenant_id)
    except Exception:
        log.exception("call failed, tenant=%s", session.tenant.tenant_id)
    finally:
        tools_used = [t.tool_name for t in session.transcript if t.role == "tool"]
        log.info("call_end %s", {
            "tenant_id": session.tenant.tenant_id,
            "tools_used": tools_used,
            "bookings": len(session.tools.bookings),
            "transferred": bool(session.transferred_to),
        })


async def run_pipeline(websocket: WebSocket, session: CallSession) -> None:
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.serializers.vonage import VonageFrameSerializer
    from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService
    from pipecat.services.aws.nova_sonic.session_continuation import (
        SessionContinuationParams)
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams, FastAPIWebsocketTransport)

    serializer = VonageFrameSerializer(
        params=VonageFrameSerializer.InputParams(
            vonage_sample_rate=VONAGE_SAMPLE_RATE))

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    creds = _bedrock_credentials()
    llm = AWSNovaSonicLLMService(
        **creds,
        region=BEDROCK_REGION,
        # Passing system_instruction and voice_id directly still works but is
        # deprecated in Pipecat 1.12 and warns. Settings is the current form.
        settings=AWSNovaSonicLLMService.Settings(
            model=MODEL_ID,
            voice=session.tenant.voice_id,     # per office, from the registry
            system_instruction=session.system_prompt,
            temperature=0.3,
            # A hard ceiling on top of the "one or two sentences" rule in the
            # prompt. A model that rambles on a phone call gets talked over,
            # and the caller loses the thread.
            max_tokens=300,
        ),
        tools=build_function_schemas(session),
        session_continuation=SessionContinuationParams(
            enabled=True,
            # Start looking for a clean handover at six minutes, well inside the
            # roughly eight minute ceiling, and hand over on the next thing the
            # agent says rather than cutting the caller off mid-question.
            transition_threshold_seconds=360.0,
        ),
    )

    # No allow_interruptions flag here on purpose. It existed in Pipecat 0.x
    # and is gone in 1.x, because Nova Sonic does its own turn detection and
    # barge-in inside the model. Passing it raises.
    # enable_usage_metrics is what makes Sonic report tokens per call, which is
    # the only way to know what a call actually costs.
    task = PipelineTask(
        Pipeline([transport.input(), llm, transport.output()]),
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    )
    await PipelineRunner(handle_sigint=False).run(task)


def build_function_schemas(session: CallSession) -> list:
    """Reuse the tool definitions the rest of the repo already uses.

    TOOL_SPECS carries the name, the description the model reads, and the JSON
    schema. Pipecat wants the same thing in its own shape, so this converts
    rather than restating them: one definition, so a tool cannot drift between
    the local demo, the plain WebSocket server and this one.

    The handler closes over `session`, which is bound to one office. There is
    no tenant argument in any schema, so nothing the caller says can move the
    call to another office's listings.
    """
    from pipecat.adapters.schemas.function_schema import FunctionSchema

    def handler_for(tool_name: str):
        async def handler(params):
            # Pipecat hands the call arguments in on `params.arguments` and
            # expects the result through `params.result_callback`.
            result = session.run_tool(tool_name, params.arguments or {})
            await params.result_callback(result)
        return handler

    schemas = []
    for spec in TOOL_SPECS:
        json_schema = spec["inputSchema"]["json"]
        schemas.append(FunctionSchema(
            name=spec["name"],
            description=spec["description"],
            properties=json_schema.get("properties", {}),
            required=json_schema.get("required", []),
            handler=handler_for(spec["name"]),
        ))
    return schemas


def _bedrock_credentials() -> dict:
    """AWSNovaSonicLLMService takes explicit keys rather than a boto3 session.

    Inside AgentCore Runtime the task role's credentials arrive as environment
    variables, so resolve them through boto3 and pass them on rather than asking
    anyone to paste a long lived access key into a config file.
    """
    import boto3

    frozen = boto3.Session().get_credentials()
    if frozen is None:
        raise RuntimeError(
            "No AWS credentials. Locally run `aws configure`; on AgentCore the "
            "runtime's execution role supplies them automatically.")
    frozen = frozen.get_frozen_credentials()
    creds = {"access_key_id": frozen.access_key,
             "secret_access_key": frozen.secret_key}
    if frozen.token:
        creds["session_token"] = frozen.token
    return creds


if __name__ == "__main__":
    import uvicorn

    host = "0.0.0.0" if os.getenv("CONTAINER_ENV") else "127.0.0.1"
    print(f"pipecat voice server on {host}:8080")
    print(f"  model   {MODEL_ID} in {BEDROCK_REGION}")
    print(f"  tenants {'DynamoDB ' + TENANT_TABLE if TENANT_TABLE else 'local config/tenants.json'}")
    uvicorn.run(app, host=host, port=8080)
