# The AWS side

Everything here is done in the browser, in the AWS console. No Terraform, no
CDK, no CloudFormation. Where a step needs the CLI I say so and give the exact
command, because two of the quotas below cannot be seen or changed in the
console at all.

## The shape of it

    a caller dials +1 416 555 0101
        |
    Amazon Connect
        claims and owns the Canadian number
        a single contact flow for every office
        reads the dialed number, looks the office up, hands the call on
        |
    Bedrock AgentCore Runtime
        hosts the Python agent as a container
        bidirectional stream, so audio flows both ways at once
        one session per call, isolated
        |
    Amazon Nova 2 Sonic
        speech in, speech out, in one model
        native turn detection and interruption handling
        calls the tools in agent/tools.py
        |
    DynamoDB            one table: offices, their numbers, their listings
    S3                  the captured pages and the extracted listing JSON
    CloudWatch          transcripts, latency, what each call cost

## Why speech to speech and not speech, then text, then speech

The obvious build is Transcribe, then a text model, then Polly. It works, and
it is the wrong choice for a sales call.

A three stage pipeline adds the three latencies together, and each stage waits
for the one before it to finish. The gap before the agent starts speaking lands
around a second and a half, which on a phone call reads as the other person
having stopped listening. It also throws away everything except the words: the
model never knows the caller sounded annoyed or was talking over it.

Nova 2 Sonic is one model that takes audio and returns audio. Turn detection
and barge-in are inside it rather than bolted on, so when a caller interrupts
mid-sentence it stops and answers the new question. That single behaviour is
most of what makes an agent sound like a person rather than an IVR.

The brief asked about low cost fast models from OpenAI or Gemini, or a
suggestion. My suggestion is Nova 2 Sonic for the voice, for three reasons that
are specific to this project rather than general preference:

1. It is on Bedrock, in the same account, under the same IAM, billed on the
   same invoice, with no second vendor contract and no caller audio leaving
   AWS. See the region section below for where inside AWS it goes, which is not
   where I first assumed.
2. AgentCore Runtime has first class support for its bidirectional stream.
   Hosting someone else's realtime API means building the WebSocket plumbing
   and the reconnect handling yourself.
3. Both OpenAI and Gemini realtime voice are good, and on raw voice quality it
   is close. The deciding factor is the tool layer: this agent lives or dies on
   calling `search_listings` and `get_listing_details` reliably mid-sentence,
   and keeping the model, the tools and the data inside one account with one
   IAM boundary is worth more here than a marginal difference in timbre.

Where a cheap fast text model does earn its place is the work around the call,
not the call: summarising a transcript, extracting the caller's requirements
into a lead record, drafting the follow up. That is batch work, latency does not
matter, and Nova Lite or Nova Micro costs a fraction of a cent per call. The
`bedrock` backend in `agent/session.py` already runs through Converse, so
swapping the model is one argument.

## Amazon Connect for the phone numbers

Canadian numbers are the easy part, and I checked the detail that usually is
not. From the Connect region requirements table:

| Canada | ID requirements | Documents |
| --- | --- | --- |
| Local telephone numbers | No | none |
| Toll-free prefixes | No | none |

So no regulatory paperwork, no proof of address, no waiting on a carrier. You
claim Canadian local and toll-free numbers yourself in the console, same day.
That is unusual, most countries need a local address and an order form.

Porting an existing office number is different: Monday to Friday, 07:00 to
17:00 CST, and they will want the last invoice plus a Letter of Authorization.
Worth knowing now if any office wants to keep a number it already advertises.

## The quotas that decide the schedule

This is the part I would want to know on day one, because two of these numbers
are small enough to stop a hundred office rollout dead, and increases are
reviewed by a human.

| Quota | Default | Adjustable | Matters because |
| --- | --- | --- | --- |
| Phone numbers per Connect instance | **10** | yes | A hundred offices needs a hundred numbers |
| Concurrent active calls per instance | **10** | yes | The eleventh simultaneous caller gets a busy tone |
| Connect instances per region | 2 | yes | |
| Flows per instance | 100 | yes | One shared flow is the design, so this is fine |
| Lambda functions per instance | 50 | yes | |

Ten numbers and ten concurrent calls are the defaults on a new account. AWS
says smaller increase requests clear in hours, larger ones take up to three
weeks, and an extra large worldwide request can take months. So the first thing
to do, before any code is deployed, is raise those two. They cost nothing to
request and nothing to hold.

Phone numbers per instance is a resource level quota, which means it cannot be
seen or raised in the Service Quotas console. It needs AWS CLI v2.13.20 or
newer, and the quota is addressed by the instance ARN rather than by the
account:

    aws service-quotas list-service-quotas ^
      --service-code connect ^
      --context-id arn:aws:connect:ca-central-1:<account>:instance/<instance-id>

That lists the resource level quotas with their codes, which is how you find
the right code to pass to `request-service-quota-increase`. I have not quoted a
quota code here because I have not run this against a real instance yet, and a
wrong code sends the request against the wrong quota.

Concurrent active calls is also resource level and works the same way. Both are
requested against the instance ARN, so the Connect instance has to exist first.

One caveat from the AWS documentation itself: the defaults in the table above
are for new accounts, and "the default and applied quota values for your
account might be lower". So the first thing to do once the instance exists is
read the actual applied values rather than trusting the table.

## Region: Sonic is not in Canada

I checked this rather than assuming it, and the answer changes the design.

Nova 2 Sonic runs in **us-east-1, us-west-2, eu-north-1 and ap-northeast-1**,
and it is in-region only on `bedrock-runtime`, so there is no cross region
inference profile to fall back on. **ca-central-1 is not on the list.** The
older Nova Sonic v1 reached end of life on 14 September 2026, so Nova 2 Sonic
is the only version there is.

**Update, and it closes the question.** AgentCore Runtime *Instances*, which is
the compute type the console asks for when you create a runtime, is also not
available in Canada (Central). So there was never a Canadian option for the
runtime either, only for the stored data. The region table is explicit: Canada
Central has AgentCore harness, Memory, Gateway, Identity and Observability, but
**not Runtime Instances**.

Cross-referencing the two availability lists, exactly **three** regions run both
Nova 2 Sonic and AgentCore Runtime Instances:

| Region | Nova 2 Sonic | Runtime Instances |
| --- | --- | --- |
| us-east-1 | yes | yes |
| **us-west-2** | **yes** | **yes** |
| ap-northeast-1 (Tokyo) | yes | yes |
| eu-north-1 (Stockholm) | yes | **no** |
| ca-central-1 | **no** | **no** |

Stockholm is the trap: it appears on the Sonic list, so it looks like a European
option, and then the runtime cannot be created there.

**The repo now defaults to us-west-2**, which is the region this project is
being built in. `BEDROCK_REGION` and `DATA_REGION` are separate environment
variables, so the office records and bookings can still be pinned to
`ca-central-1` later without touching the runtime.

What follows is the original reasoning, which still applies to where the
*stored* data lives:

**Option A, split.** Connect, DynamoDB and the lead records in `ca-central-1`;
AgentCore Runtime and Sonic in `us-east-1`. Canadian phone numbers, Canadian
listing data, Canadian transcripts. The live audio of each call crosses into
the US while it is being spoken. Adds roughly 15 to 30 ms of network latency
each way between Toronto and Virginia, which is not audible next to the model's
own response time.

**Option B, all in us-east-1.** Simpler, one region, nothing to explain in an
architecture diagram. The phone numbers are still Canadian, because Connect
claims Canadian numbers from a US region perfectly well. But the caller's audio
and the transcript both live in the US.

My recommendation is A. It keeps everything that is stored and searchable in
Canada and limits the US footprint to audio in flight, which is the part that
is hardest to object to and easiest to explain to a real estate office asking
where their callers' details go. It costs one extra region in the console and
nothing in the code, because the region is already a parameter.

What this is not is a reason to go to OpenAI or Gemini instead: neither has a
Canadian region either, and both would mean caller audio leaving AWS entirely
as well as leaving the country.

If Canadian-only processing turns out to be a hard requirement rather than a
preference, say so and I will cost out the alternative honestly: Transcribe and
Polly, both of which do run in ca-central-1, wired as a three stage pipeline.
It would be fully in Canada and it would sound worse, and you should hear both
before choosing.

## One contact flow, every office

The thing that makes a hundred offices maintainable is that there is only ever
one contact flow. It does not know about any particular office.

1. **Set logging behaviour** on, so a misrouted call can be traced.
2. **Invoke AWS Lambda**, passing `$.SystemEndpoint.Address`, which is the
   number the caller dialled. The Lambda looks it up in DynamoDB and returns
   the office's id, display name, agent name and voice.
3. **Set contact attributes** from the response.
4. **Check contact attributes**: if no office owns the number, play a message
   and disconnect. Never fall through to a default office, or a caller hears
   the wrong inventory.
5. Hand the call to the agent, with the office id as a session attribute.
6. **Error branch** on every block, to a short apology and a transfer to the
   office's handoff number. A flow with unconnected error branches drops calls
   silently.

Adding office number 47 is then one DynamoDB row and one number claimed in the
console. No flow edit, no deploy.

## The DynamoDB table

One table, `voice_agent_tenants`, in ca-central-1 (it holds stored data, so it
stays in Canada under either region option).

| | Attribute | Example |
| --- | --- | --- |
| Partition key | `tenant_id` | `tenant#harbourfront` |
| Sort key | `sk` | `meta`, `listing#C13868410` |
| GSI `number-index` | `phone_number` | `+14165550101` |

The partition key is the office, so one query returns an office and all of its
listings, and no query can be written that spans two offices by accident. That
also lets an IAM policy pin a session to one office with a
`dynamodb:LeadingKeys` condition, so the isolation holds even if the agent code
is wrong.

`agent/store.py` already has both backends behind one interface. Moving from
the local JSON file to DynamoDB is one line in `demo_call.py`.

## What I need from you

These five change the build, so I would rather ask than assume. The work
continues either way: everything above runs locally today, and none of it is
blocked on an answer.

1. **Region, option A or B above.** Sonic is not in Canada, so this is a data
   residency choice, not a technical one. My recommendation is A: stored data
   in ca-central-1, live audio processed in us-east-1. Say "A" and I carry on.
2. **Account.** Do you have an AWS account for this already, and is Bedrock
   model access enabled on it? Nova models need to be requested once per
   region, in the console, and it is instant. Tell me when you have done it and
   I will give you the exact clicks.
3. **How many offices for real.** The brief says 0 to 100+. If the first
   version is three offices I can skip the number quota request for now; if it
   is genuinely a hundred, the two quota increases should go in this week
   because of the three week review window.
4. **Voice.** Nova 2 Sonic has several voices. I will record the same greeting
   in each against your real listing data and send them as audio files so you
   pick by ear rather than from a name in a table. Any accent preference,
   Canadian English, or French for Quebec?
5. **Who is the caller.** The brief says "sell homes". Is this inbound, a buyer
   phoning about a listing they saw, which is what I have built so far? Or
   outbound to a lead list? Outbound changes a lot: consent, CRTC
   telemarketing rules, a do not call list check before dialling, and calling
   hour restrictions. Worth knowing before I build the wrong one.

## What I would build next

In this order, with the reason each one is before the next:

1. The two quota increase requests, because they are the only thing with a
   three week tail.
2. Nova 2 Sonic over a microphone, locally, with these six listings. Hearing it
   is the only way to judge the voice, and it needs no Connect instance.
3. Voice samples sent to you as audio files, for the voice choice.
4. The DynamoDB table and the switch from the JSON store, which is one line.
5. AgentCore Runtime deployment, container and bidirectional stream.
6. Connect instance, one number, the shared contact flow, a real phone call.
7. The second office, which proves the multi-tenant part with a real number.
8. The written walkthrough, every console screen, so you can do all of it
   yourself from an empty account.
