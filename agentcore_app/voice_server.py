"""The thing that gets deployed to AgentCore Runtime.

AgentCore Runtime requires exactly two things of a container, and this file is
mostly those two things plus the wiring to the agent that already exists in
this repo:

  GET  /ping   health check. AgentCore polls it, and a runtime that fails it is
               never sent traffic. Must return quickly and must not touch
               Bedrock.
  WS   /ws     the bidirectional stream. Audio in and audio out at the same
               time, which is what makes barge-in possible.

Listening on port 8080 is also required.

The important line in here is the one that picks the tenant. The telephony
bridge opens the WebSocket with the number the caller actually dialled:

    wss://.../ws?to=%2B14165550101

That number, not anything the model decides, selects the office. Tools are
built fresh for that office on each connection, so there is no path by which a
call on one office's line can read another office's listings. Same rule as the
local demo, same code underneath.

Run it locally first. It needs no AgentCore and no container:

    python agentcore_app/voice_server.py
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.prompts import build_system_prompt, greeting_for      # noqa: E402
from agent.session import CallSession, UnroutedNumber, TenantSuspended  # noqa: E402
from agent.store import JsonStore, DynamoDbStore, normalise_e164  # noqa: E402

log = logging.getLogger("voice")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


# Nova 2 Sonic is not available in ca-central-1. See docs/architecture.md: the
# store and the lead data stay in Canada, the model call does not.
MODEL_ID = os.getenv("MODEL_ID", "amazon.nova-2-sonic-v1:0")
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")
DATA_REGION = os.getenv("DATA_REGION", "ca-central-1")
TENANT_TABLE = os.getenv("TENANT_TABLE")        # unset means use the local JSON

# A phone line is 8 kHz, but Sonic wants 16 kHz PCM mono, so the bridge
# upsamples before it reaches us. Getting this wrong does not error, it just
# makes the agent sound like a chipmunk, which is why it is a named constant
# rather than a literal three functions down.
INPUT_SAMPLE_RATE = int(os.getenv("INPUT_SAMPLE_RATE", "16000"))
OUTPUT_SAMPLE_RATE = int(os.getenv("OUTPUT_SAMPLE_RATE", "16000"))
CHANNELS = int(os.getenv("CHANNELS", "1"))
AUDIO_FORMAT = os.getenv("FORMAT", "pcm")

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
    """Required by AgentCore Runtime. Deliberately does nothing but answer: if
    this reached into DynamoDB or Bedrock, a slow dependency would take the
    whole runtime out of rotation rather than degrading one call."""
    return {"status": "Healthy",
            "time_of_last_update": int(datetime.now().timestamp())}


@app.websocket("/ws")
async def voice_call(websocket: WebSocket) -> None:
    dialed = websocket.query_params.get("to") or os.getenv("DEFAULT_DIALED_NUMBER")
    caller_id = websocket.query_params.get("from")

    try:
        session = CallSession(STORE, normalise_e164(dialed or ""))
    except (UnroutedNumber, TenantSuspended) as exc:
        # Refuse rather than fall back to a default office. A caller hearing
        # the wrong office's listings is worse than a caller hearing an error.
        log.warning("refusing call to %r: %s", dialed, exc)
        await websocket.accept()
        await websocket.send_json({"type": "error", "reason": str(exc)})
        await websocket.close(code=1008)
        return

    log.info("call for tenant=%s dialed=%s from=%s",
             session.tenant.tenant_id, dialed, caller_id)

    agent = _build_bidi_agent(session)
    try:
        await websocket.accept()
        await agent.run(inputs=[websocket.receive_json],
                        outputs=[websocket.send_json])
    except WebSocketDisconnect:
        log.info("caller hung up, tenant=%s", session.tenant.tenant_id)
    except Exception:
        log.exception("call failed, tenant=%s", session.tenant.tenant_id)
    finally:
        _log_call_outcome(session)
        try:
            await websocket.close()
        except Exception:
            pass
        try:
            await agent.stop()
        except Exception:
            pass


def _build_bidi_agent(session: CallSession):
    """Imported here rather than at module load so that /ping still answers and
    the local demo still runs on a machine without the voice dependencies."""
    from strands.experimental.bidi import BidiAgent
    from strands.experimental.bidi.models import BidiNovaSonicModel
    from strands.experimental.bidi.tools import stop_conversation

    model = BidiNovaSonicModel(
        model_id=MODEL_ID,
        provider_config={
            "audio": {
                "voice": session.tenant.voice_id,   # per office, from the registry
                "input_rate": INPUT_SAMPLE_RATE,
                "output_rate": OUTPUT_SAMPLE_RATE,
                "channels": CHANNELS,
                "format": AUDIO_FORMAT,
            },
            "inference": {},
        },
        client_config={"region": BEDROCK_REGION},
    )
    return BidiAgent(
        model=model,
        tools=build_tools(session) + [stop_conversation],
        system_prompt=session.system_prompt,
    )


def build_tools(session: CallSession) -> list:
    """Wrap the ToolBox in Strands tools, bound to this call's office.

    The signatures deliberately do not take a tenant id. There is no argument
    the model could get wrong and no sentence a caller could say that would
    move the call to another office's inventory.
    """
    from strands import tool

    box = session.tools

    @tool
    def search_listings(max_price: Optional[float] = None,
                        min_bedrooms: Optional[float] = None,
                        neighbourhood: Optional[str] = None,
                        property_type: Optional[str] = None) -> dict:
        """Find homes this office has for sale that match what the caller
        described. Use it as soon as they mention a budget, a number of
        bedrooms or an area. Returns at most three.

        Args:
            max_price: Top of the caller's budget in dollars.
            min_bedrooms: Fewest bedrooms they will accept. Use 0 for a studio.
            neighbourhood: Area or neighbourhood the caller named.
            property_type: condo, house, townhouse or loft, if they said.
        """
        return session.run_tool("search_listings", {
            "max_price": max_price, "min_bedrooms": min_bedrooms,
            "neighbourhood": neighbourhood, "property_type": property_type})

    @tool
    def get_listing_details(listing_id: str) -> dict:
        """Everything known about one home: price, rooms, fees, taxes,
        amenities and the listing remarks. Call this before answering any
        specific question about a property.

        Args:
            listing_id: The MLS number from a previous search.
        """
        return session.run_tool("get_listing_details", {"listing_id": listing_id})

    @tool
    def get_neighbourhood(listing_id: str) -> dict:
        """What the area around a home is like, plus walk, transit and bike
        scores and local market statistics. Use it when the caller asks about
        the area, schools, transit, or what prices are doing.

        Args:
            listing_id: The MLS number of the home they are asking about.
        """
        return session.run_tool("get_neighbourhood", {"listing_id": listing_id})

    @tool
    def book_viewing(listing_id: str, caller_name: str, callback_number: str,
                     preferred_day: str, preferred_time: str) -> dict:
        """Book the caller in to see a home. Only call this once you have read
        the day, time and their phone number back to them and they confirmed.

        Args:
            listing_id: The MLS number of the home.
            caller_name: Their name.
            callback_number: Their number, digits as they said them.
            preferred_day: A date like 2026-10-11, or a weekday name.
            preferred_time: For example 2pm.
        """
        return session.run_tool("book_viewing", {
            "listing_id": listing_id, "caller_name": caller_name,
            "callback_number": callback_number, "preferred_day": preferred_day,
            "preferred_time": preferred_time})

    @tool
    def transfer_to_human(reason: str) -> dict:
        """Hand the call to a person. Call this the moment the caller asks for
        a human, sounds frustrated, wants to negotiate a price, or asks
        something you were told you do not know.

        Args:
            reason: Why you are transferring, in a few words.
        """
        return session.run_tool("transfer_to_human", {"reason": reason})

    _ = box  # the ToolBox lives on the session; named here for readability
    return [search_listings, get_listing_details, get_neighbourhood,
            book_viewing, transfer_to_human]


def _log_call_outcome(session: CallSession) -> None:
    """One structured line per call, so CloudWatch Logs Insights can answer
    "how many calls booked a viewing" without a separate analytics pipeline."""
    tools_used = [t.tool_name for t in session.transcript if t.role == "tool"]
    log.info("call_end %s", {
        "tenant_id": session.tenant.tenant_id,
        "tools_used": tools_used,
        "bookings": len(session.tools.bookings),
        "transferred": bool(session.transferred_to),
    })


if __name__ == "__main__":
    import uvicorn

    # 0.0.0.0 only inside a container. Binding a dev machine to all interfaces
    # puts an unauthenticated WebSocket on the local network.
    host = "0.0.0.0" if os.getenv("CONTAINER_ENV") else "127.0.0.1"
    print(f"voice server on {host}:8080")
    print(f"  model   {MODEL_ID} in {BEDROCK_REGION}")
    print(f"  tenants {'DynamoDB ' + TENANT_TABLE if TENANT_TABLE else 'local config/tenants.json'}")
    print(f"  offices {', '.join(t.phone_number for t in STORE._tenants.values())}"
          if isinstance(STORE, JsonStore) else "")
    uvicorn.run(app, host=host, port=8080)
