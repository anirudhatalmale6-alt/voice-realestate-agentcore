# Voice real estate agent, multi-tenant, on AWS Bedrock AgentCore

## Start here

**Windows: download this repo, then double click `run_demo.bat`.** That is the
whole thing. No AWS account, no API key, no internet needed.

Anything else:

    pip install -r requirements.txt
    python demo_call.py

You will watch a phone call happen. A caller asks what is under $400,000, the
agent finds the home, describes it, describes the neighbourhood, books a
viewing, and hands the call to a person when the caller starts talking about
price. Then the same call runs again against a second office, which has nothing
in that price range, so you can see that the two offices cannot see each other's
listings.

The listings are real Toronto homes, not invented ones.

To check nothing is broken:

    python -m pytest tests -q        # 57 checks, no network

The rest of this page is detail. You do not need it to run the demo.

---

A phone agent that answers calls for a real estate office, talks about the
homes that office has for sale, describes the neighbourhood, and books
viewings. Many offices, one build: each office gets its own Canadian phone
number, its own voice, its own name and its own listings, and no call can ever
reach another office's inventory.

This repository is the part that decides what the caller hears. It runs on your
Windows machine with no AWS account and no API key, so the conversation logic,
the property data and the tenant isolation can be tested and changed for free
before any audio is involved.

## What is here now

| Piece | File | What it does |
| --- | --- | --- |
| Listing schema | `ingest/schema.py` | The one shape every portal is normalised into |
| Page extractor | `ingest/extract.py` | Saved property page to structured listing, four passes |
| Knowledge pack | `knowledge/pack.py` | Listing to what the agent is allowed to say, in speech |
| Tenant registry | `agent/store.py` | Number to office to listings, local JSON or DynamoDB |
| Tools | `agent/tools.py` | search, details, neighbourhood, book, transfer |
| Prompt | `agent/prompts.py` | The rules that make it sound like a phone call |
| Call session | `agent/session.py` | One call: routing, prompt assembly, model backend |
| Demo | `demo_call.py` | A scripted call through the real tool layer |

Six real Toronto listings are included under `samples/`, captured from
remax.ca, extracted with zero warnings, covering a studio condo, a loft, a
bungalow and a $2.7m detached house.

## Property data: why you save the page

The brief named realtor.ca, zolo.ca and zillow.com. All three refuse automated
requests:

| Site | Plain HTTPS request | Real headless Chromium, Windows user agent |
| --- | --- | --- |
| realtor.ca | 403 | 403, Cloudflare "Just a moment..." challenge |
| zolo.ca | 403 | not attempted, same front end |
| zillow.com | 403 | not attempted |
| point2homes.com | 403 | not attempted |
| remax.ca | 200 | 200 |
| royallepage.ca | 200 | 200 |

So the extractor takes a saved page rather than a URL. In Chrome, open the
listing, expand the collapsed sections, then Ctrl+S and choose "Webpage,
Single File". Then:

    python -m ingest.extract "C:\pages\55-stewart.html"

Expanding the sections first matters: the server only sends the accordions
that are open, so a page saved without expanding "Financial Info" has no tax
figure in it. The extractor reports what it could not find rather than
guessing, so you will see exactly which fields a bad capture cost you.

For the two portals that do allow it, the same extractor works on a page
fetched directly. That is how the six samples here were produced.

## The four extraction passes

Best evidence first, and a later pass can only fill a field an earlier pass
left empty.

1. **JSON-LD.** `schema.org/RealEstateListing`, which most portals emit for
   Google. Price, address, bed and bath counts, photo count.
2. **Embedded app state.** `__NEXT_DATA__`, `window.__INITIAL_STATE__`. This is
   where realtor.ca keeps its PascalCase fields.
3. **Label and value pairs.** Both the `<dl>` and two cell `<tr>` kind, and the
   flat "label on one line, value on the next" kind that DDF-fed portals use.
4. **Visible prose.** The listing remarks and the neighbourhood paragraph.

Things that look simple and are not, all of which are covered by a test:

- `"1503 - 38 IANNUZZI ST"` carries the unit inside the street line. Said out
  loud that becomes "fifteen oh three thirty eight Iannuzzi Street".
- `"TORONTO (NIAGARA)"` is city then MLS district. The neighbourhood people
  actually say is "Fort York-Liberty Village", and it is somewhere else on the
  page.
- A "Neighbourhood" heading appears twice: once in the page chrome above
  "CLIMATE RISK FOR M5V 0S2", and once above the real area profile. Taking the
  first one files a heading as the neighbourhood name.
- The area paragraph is split across text nodes, so the sentence starts
  mid-way: `["The character of", "Don Valley Village", "is exemplified by..."]`.
- `"2+1"` is two bedrooms and a den. Adding them is how you lose a buyer at
  the viewing.
- `FTK` is the DDF unit code for square feet.

## Why there is a knowledge pack and not just the listing JSON

Two reasons, both learned the hard way with voice agents.

**Nulls get filled in.** Show a model `"year_built": null` and ask it how old
the building is, and it will tell the caller a number. The pack contains no
empty fields at all: a fact is either stated, or it is named in a
`do_not_know` list together with the sentence to say instead. `test_pack_never_contains_a_null`
enforces that.

**Speech is not text.** `$1,095.27`, `38 IANNUZZI ST` and unit `1503` are all
read badly. The pack carries a spoken form next to the written one, and the
prompt tells the agent to say unit numbers digit by digit.

## Multi-tenancy

The isolation boundary is the dialed number, resolved before the model is given
anything:

    inbound call
      -> Amazon Connect contact flow reads the dialed number
      -> tenant looked up by number
      -> only that tenant's listings loaded into the session
      -> system prompt built with that tenant's name, voice and inventory

It is not a line in the prompt asking the model to behave. `agent/store.py`
refuses to return a listing that is not on the tenant's own list, so a caller
who reads out an MLS number found online gets "I do not have that one on our
books" with no hint of whether it exists elsewhere. Loading two tenants with
the same phone number raises at startup rather than silently sending calls to
whichever row was read first.

Both tests are in the suite: `test_a_tenant_cannot_read_another_tenants_listing`
and `test_prompt_contains_only_this_tenants_listings`.

## Model backends

`agent/session.py` keeps the conversation logic separate from how the words are
produced, so the same prompt and the same tools are used by all three:

- **scripted**, no model, no credentials. What the test suite and `demo_call.py`
  run. Exercises the whole tool layer for free.
- **bedrock**, Bedrock Converse, text in and text out. For rehearsing the
  conversation and comparing models cheaply. `python demo_call.py --bedrock`.
- **sonic**, Amazon Nova 2 Sonic, speech to speech over a bidirectional stream,
  hosted on AgentCore Runtime. The production path.

See `docs/architecture.md` for the AWS side, the console steps, and the service
quotas that decide whether a hundred offices is a week of work or three.

## Layout

    ingest/      schema and the page extractor
    knowledge/   listing to speakable facts
    agent/       tenant registry, tools, prompt, call session
    config/      tenants.json, the local stand-in for DynamoDB
    samples/     six captured pages and the listings extracted from them
    tests/       57 checks, no network, no credentials
    docs/        architecture and the console walkthrough
