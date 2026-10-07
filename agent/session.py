"""One phone call.

This is the piece that AgentCore Runtime will host. Everything about the call
that must be decided before the model gets involved happens here:

    dialed number -> tenant -> that tenant's listings -> system prompt -> model

The model backend is swappable on purpose. The conversation logic, the tool
layer and the isolation rules are identical whether the words are produced by
Nova Sonic over a phone line or by a text model in a terminal, so they are
tested once, here, without spending anything on audio.

Backends:
  bedrock   Bedrock Converse, text in and text out. Used to rehearse the
            conversation and to run the test suite.
  sonic     Nova 2 Sonic, speech in and speech out, bidirectional stream.
            Same prompt, same tools, audio instead of text.
  script    No model at all. Replays a fixed set of tool calls so the tool
            layer can be tested with no credentials and no network. The test
            suite uses this one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from knowledge.pack import build_pack, pack_as_prompt_block
from .prompts import build_system_prompt, greeting_for
from .store import Store, Tenant
from .tools import TOOL_SPECS, ToolBox


@dataclass
class Turn:
    role: str                   # "caller", "agent", "tool"
    text: str = ""
    tool_name: Optional[str] = None
    tool_args: dict = field(default_factory=dict)
    tool_result: Optional[dict] = None


class CallSession:
    def __init__(self, store: Store, dialed_number: str,
                 inline_listings: int = 3, bookings: Optional[list] = None):
        tenant = store.tenant_for_number(dialed_number)
        if tenant is None:
            # An unrouted number is a configuration bug, and letting the call
            # fall through to a default tenant would mean a caller hearing
            # another store's inventory. Refuse instead.
            raise UnroutedNumber(dialed_number)
        if not tenant.active:
            raise TenantSuspended(tenant.tenant_id)

        self.store = store
        self.tenant: Tenant = tenant
        self.tools = ToolBox(store, tenant, bookings=bookings)
        self.transcript: list[Turn] = []
        self.transferred_to: Optional[str] = None

        # A small book goes straight into the prompt: it is cheaper and faster
        # than a tool round trip, and the first word the caller hears is the
        # thing latency is measured on. A large book does not, or the prompt
        # grows past the point where that trade pays off.
        listings = store.list_listings(tenant.tenant_id)
        if 0 < len(listings) <= inline_listings:
            block = "\n\n".join(pack_as_prompt_block(build_pack(l)) for l in listings)
        else:
            block = (f"This office has {len(listings)} homes for sale. Use "
                     f"search_listings to find them; do not guess at what is "
                     f"available.")
        self.system_prompt = build_system_prompt(tenant, block)

    # ---- transcript helpers ----

    def greeting(self) -> str:
        text = greeting_for(self.tenant)
        self.transcript.append(Turn("agent", text))
        return text

    def record_caller(self, text: str) -> None:
        self.transcript.append(Turn("caller", text))

    def run_tool(self, name: str, args: dict) -> dict:
        result = self.tools.call(name, args)
        self.transcript.append(Turn("tool", tool_name=name, tool_args=args,
                                    tool_result=result))
        if name == "transfer_to_human" and result.get("transferred"):
            self.transferred_to = result.get("to")
        return result

    def record_agent(self, text: str) -> None:
        self.transcript.append(Turn("agent", text))

    def as_text(self) -> str:
        lines = []
        for t in self.transcript:
            if t.role == "tool":
                args = ", ".join(f"{k}={v!r}" for k, v in t.tool_args.items())
                lines.append(f"      [{t.tool_name}({args})]")
                lines.append(f"      -> {json.dumps(t.tool_result, default=str)[:300]}")
            else:
                who = self.tenant.agent_name if t.role == "agent" else "Caller"
                lines.append(f"{who}: {t.text}")
        return "\n".join(lines)


class UnroutedNumber(Exception):
    def __init__(self, number: str):
        super().__init__(f"no tenant owns {number}")
        self.number = number


class TenantSuspended(Exception):
    def __init__(self, tenant_id: str):
        super().__init__(f"tenant {tenant_id} is not active")
        self.tenant_id = tenant_id


# --------------------------------------------------------------------------
# model backends
# --------------------------------------------------------------------------

def bedrock_turn(session: CallSession, caller_text: str, model_id: str,
                 region: str = "ca-central-1", max_tool_hops: int = 4) -> str:
    """One caller utterance in, one spoken reply out, via Bedrock Converse.

    Converse is used rather than the raw model APIs because the tool-calling
    shape is identical across models, so switching model_id is the only change
    needed to compare Nova Lite against Claude Haiku against anything else.
    """
    import boto3

    client = boto3.client("bedrock-runtime", region_name=region)
    session.record_caller(caller_text)

    messages = _converse_messages(session)
    for _ in range(max_tool_hops):
        response = client.converse(
            modelId=model_id,
            system=[{"text": session.system_prompt}],
            messages=messages,
            toolConfig={"tools": [{"toolSpec": s} for s in TOOL_SPECS]},
            inferenceConfig={"maxTokens": 300, "temperature": 0.3},
        )
        out = response["output"]["message"]
        messages.append(out)

        tool_uses = [c["toolUse"] for c in out.get("content", []) if "toolUse" in c]
        if not tool_uses:
            text = " ".join(c["text"] for c in out.get("content", []) if "text" in c)
            session.record_agent(text.strip())
            return text.strip()

        results = []
        for use in tool_uses:
            result = session.run_tool(use["name"], use.get("input", {}))
            results.append({"toolResult": {
                "toolUseId": use["toolUseId"],
                "content": [{"json": result}],
            }})
        messages.append({"role": "user", "content": results})

    text = "Let me get one of our agents to call you back on that."
    session.record_agent(text)
    return text


def _converse_messages(session: CallSession) -> list:
    """Rebuild the Converse message list from the transcript.

    Tool turns are deliberately dropped here and replayed inside the loop
    above. Converse requires a toolResult to immediately follow the toolUse
    that produced it, and a transcript replayed turn by turn does not preserve
    that pairing across calls.
    """
    messages = []
    for turn in session.transcript:
        if turn.role == "caller":
            messages.append({"role": "user", "content": [{"text": turn.text}]})
        elif turn.role == "agent" and turn.text:
            messages.append({"role": "assistant", "content": [{"text": turn.text}]})
    # Converse rejects two messages with the same role in a row.
    merged = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += m["content"]
        else:
            merged.append(m)
    if merged and merged[0]["role"] == "assistant":
        merged.pop(0)        # the greeting; Converse must start with the user
    return merged
