"""Walk through a phone call in the terminal, with no AWS account needed.

    python demo_call.py                      both tenants, scripted call
    python demo_call.py --number +14165550102
    python demo_call.py --prompt             print the system prompt instead
    python demo_call.py --bedrock            use a real model (needs credentials)

The scripted mode runs the real store, the real tenant routing, the real tools
and the real knowledge pack. The only thing it fakes is the model's choice of
which tool to call, so the data flow you see is exactly the data flow a live
call has. That is the point: you can see and test everything that decides what
the caller hears before spending anything on audio.
"""

from __future__ import annotations

import argparse
import sys

from agent.session import CallSession, UnroutedNumber, bedrock_turn
from agent.store import JsonStore


# What the model would decide to do, written out so the tool layer can be seen
# working. Each step is (what the caller says, the tool the model should pick,
# its arguments).
SCRIPT = [
    ("Hi, I saw a place of yours online. What have you got under four hundred "
     "thousand?", "search_listings", {"max_price": 400000}),
    ("Tell me about that one.", "get_listing_details",
     {"listing_id": "<first>"}),
    ("What is the area like? Is it walkable?", "get_neighbourhood",
     {"listing_id": "<first>"}),
    ("How old is the building?", None, None),
    ("Can I see it Saturday afternoon? Sam Patel, four one six, five five "
     "five, one two one two.", "book_viewing",
     {"listing_id": "<first>", "caller_name": "Sam Patel",
      "callback_number": "4165551212", "preferred_day": "Saturday",
      "preferred_time": "2pm"}),
    ("Actually, would they take three fifty?", "transfer_to_human",
     {"reason": "caller wants to negotiate the price"}),
]


def run_scripted(session: CallSession) -> None:
    print(f"\n{'=' * 72}")
    print(f"  {session.tenant.display_name}   {session.tenant.phone_number}   "
          f"voice: {session.tenant.voice_id}")
    print("=" * 72)
    print(f"{session.tenant.agent_name}: {session.greeting()}")

    first_id = None
    for caller_text, tool_name, tool_args in SCRIPT:
        print(f"Caller: {caller_text}")
        session.record_caller(caller_text)

        if tool_name is None:
            # The "how old is the building" turn. There is no tool for it, and
            # the pack already told the agent it does not know, so the right
            # behaviour is to say so rather than to estimate.
            print(f"{session.tenant.agent_name}: [no tool: the pack lists "
                  f"'the year the building went up' as unknown, so the agent "
                  f"offers to confirm instead of guessing]")
            continue

        args = dict(tool_args)
        for key, value in args.items():
            if value == "<first>":
                if first_id is None:
                    print("   (no listing to follow up on, skipping)")
                    break
                args[key] = first_id
        else:
            result = session.run_tool(tool_name, args)
            print(f"   -> {tool_name}: {_summarise(result)}")
            if tool_name == "search_listings" and result.get("listings"):
                first_id = result["listings"][0]["listing_id"]

    if session.transferred_to:
        print(f"\n   call handed to a person at {session.transferred_to}")
    print(f"   bookings made: {len(session.tools.bookings)}")
    for b in session.tools.bookings:
        print(f"     {b['reference']}  {b['caller_name']}  {b['date']} "
              f"{b['time']}  {b['address']}")


def _summarise(result: dict) -> str:
    if "listings" in result:
        if not result["listings"]:
            return f"nothing matched. says: {result['say_if_empty']}"
        lines = [f"{result['count']} match, showing {result['showing']}"]
        lines += [f"        {l['listing_id']}  {l['summary']}"
                  for l in result["listings"]]
        return "\n".join(lines)
    if "facts" in result:
        facts = ", ".join(f"{k} {v}" for k, v in list(result["facts"].items())[:5])
        unknown = "; ".join(result.get("do_not_know", []))
        return f"{facts}\n        does not know: {unknown or 'nothing'}"
    if "area" in result:
        area = result["area"]
        return (f"{area.get('name', '?')}: "
                f"{(area.get('summary') or 'no profile')[:120]}")
    if "say" in result:
        return result["say"]
    return str(result)[:200]


def run_bedrock(session: CallSession, model_id: str, region: str) -> None:
    print(f"\n{'=' * 72}")
    print(f"  {session.tenant.display_name}   live model: {model_id}")
    print("=" * 72)
    print(f"{session.tenant.agent_name}: {session.greeting()}")
    for caller_text, _, _ in SCRIPT:
        print(f"Caller: {caller_text}")
        reply = bedrock_turn(session, caller_text, model_id=model_id, region=region)
        print(f"{session.tenant.agent_name}: {reply}")
    print()
    print(session.as_text())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--number", help="dialed number; omit to run every tenant")
    ap.add_argument("--prompt", action="store_true",
                    help="print the system prompt and exit")
    ap.add_argument("--bedrock", action="store_true",
                    help="drive a real model through Bedrock Converse")
    ap.add_argument("--model", default="us.amazon.nova-lite-v1:0")
    ap.add_argument("--region", default="ca-central-1")
    args = ap.parse_args()

    store = JsonStore()
    numbers = ([args.number] if args.number
               else [t.phone_number for t in store._tenants.values()])

    for number in numbers:
        try:
            session = CallSession(store, number)
        except UnroutedNumber as exc:
            print(f"{exc.number}: no tenant owns this number. "
                  f"Check config/tenants.json.", file=sys.stderr)
            return 1
        if args.prompt:
            print(session.system_prompt)
        elif args.bedrock:
            run_bedrock(session, args.model, args.region)
        else:
            run_scripted(session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
