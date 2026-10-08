# The two hard parts: deploying to AgentCore, and getting a phone onto it

These are the two things you said are the challenge. You are right about both,
and for different reasons. This page is just those two.

---

# 1. How to deploy to AgentCore

## What AgentCore actually asks of your code

Very little, and `agentcore_app/voice_server.py` already does all of it:

- listen on **port 8080**
- answer `GET /ping` with a health status
- expose the **WebSocket** the audio flows over

That is the whole contract. Everything else is packaging.

## First, a trap worth knowing about

Nearly every blog post and tutorial you will find says:

    pip install bedrock-agentcore-starter-toolkit
    agentcore configure --entrypoint app.py
    agentcore launch

**That tool is deprecated.** Install it today and it prints a warning telling
you to stop using it, and `launch` has been renamed to `deploy`. New AgentCore
features only land in the replacement. The current tool is a different one with
the same command name, installed through npm:

    npm install -g @aws/agentcore

I confirmed this by installing both and reading what they print, not from a
blog. If you follow a tutorial from a few months ago you will spend an evening
on a tool that is on its way out.

## Path A, one command

    npm install -g @aws/agentcore
    agentcore import          (points it at this repo)
    agentcore deploy

`deploy` builds an ARM64 container in the cloud with CodeBuild, pushes it to
ECR, and creates the AgentCore Runtime. **No Docker on your Windows machine.**

One honest caveat, because of what you said about Terraform. `agentcore deploy`
uses CDK internally; its own help text says "Deploy project infrastructure to
AWS via CDK". You never write CDK, never read it and never maintain it, so this
is not the same as a Terraform project you have to learn. But if the rule is no
infrastructure-as-code anywhere, even hidden inside a tool, use path B.

## Path B, entirely in the browser

No CLI at all. More clicking, and you see every resource as it is created,
which for learning this is arguably better.

1. **ECR**, console, create a private repository. Call it
   `realestate_voice`.
2. **CodeBuild**, console, create a build project. Source is this repo from
   GitHub, environment is **ARM64**, privileged mode on, and give its service
   role ECR push permission. It builds the Dockerfile in `agentcore_app/` and
   pushes to the repository from step 1.
3. **Bedrock AgentCore**, console, **Runtime**, **Create runtime**.
   - Name it `realestate_voice`.
   - Compute type: **Instances**.
   - Capacity provider: **Quick create** is fine to start.
   - Agent/tool source: **ECR container**, and paste the image URI from step 1,
     which looks like
     `<account>.dkr.ecr.us-east-1.amazonaws.com/realestate_voice:latest`.

## Four things that will bite you, in the order they will

1. **The image must be ARM64.** Build it on an x86 Windows machine without
   `--platform=linux/arm64` and the runtime is created happily, then fails to
   start with very little explanation. The Dockerfile in this repo pins it, and
   CodeBuild on an ARM64 environment gets it right by default.
2. **No hyphens in the runtime name.** `realestate-voice` errors.
   `realestate_voice` is fine. This is why everything here is named with
   underscores.
3. **The auto-created IAM role often gets the ECR resource path wrong.** If the
   runtime cannot pull the image, that is the first thing to check, before
   anything in the container.
4. **`/ping` must never call Bedrock or DynamoDB.** AgentCore polls it and stops
   sending traffic to a runtime that fails it, so one slow dependency would
   take the whole thing out of rotation instead of degrading a single call.
   There is a test for this: `test_ping_does_not_touch_bedrock_or_dynamo`.

## Region

Nova 2 Sonic runs in us-east-1, us-west-2, eu-north-1 and ap-northeast-1 only,
in-region, with no cross-region fallback. **Not Canada.** So the runtime goes in
`us-east-1` and the office records and lead data stay in `ca-central-1`. That
split is already a parameter, `BEDROCK_REGION` and `DATA_REGION`.

---

# 2. Why phone voice on AWS is a challenge

You are right that this is the hard part, and it is worth being precise about
why, because the reason is not the voice.

AgentCore plus Nova 2 Sonic gives you an excellent voice conversation **over a
WebSocket**. Audio in, audio out, interruptions handled, tools called
mid-sentence. What it does not do is answer a telephone. Nothing in that chain
is connected to the phone network at all.

So something has to sit between the public phone network and that WebSocket,
take the call, and pump the audio through. That bridge is the entire problem,
and it is where every voice agent project gets stuck.

There are three ways to build it. All three end at the same WebSocket, so the
agent code in this repo does not change whichever you pick.

## The reason Twilio is out, precisely

Worth writing down because it is not obvious and it decides the provider.

AgentCore Runtime will authenticate a WebSocket three ways: SigV4 headers, a
SigV4 **pre-signed URL** with the signature in the query string, or OAuth.

A telephony provider dialling a `wss://` URL cannot set headers. So the only
option is the pre-signed URL. And **Twilio Media Streams strips query
parameters off the WebSocket URL it dials.** The signature never arrives and
AgentCore rejects the connection. Twilio's own guidance for passing data is to
use the TwiML `<Parameter>` element instead, which does not help, because
AgentCore needs the signature in the URL.

Vonage passes the query string through. That is the whole reason.

You can still use Twilio if you put your own small WebSocket server in between
to re-sign, but that is an extra always-on hop to build, pay for and monitor,
for no gain.

## Option 1, Vonage. There is an official working AWS sample

AWS publishes `aws-samples/sample-vonage-serverless-sonic`, which is exactly
this: phone calls through the Vonage Voice API, audio streamed in real time to
Nova Sonic over an AgentCore Runtime WebSocket, with a small Lambda that issues
the presigned WebSocket URL Vonage connects to.

This is the lowest risk option by a distance, because someone at AWS has
already made it work end to end and published the code.

## Option 2, Twilio

Twilio Media Streams works the same way: the call arrives, Twilio opens a
WebSocket and streams the audio as base64 frames. Enormous amount of community
documentation. Same shape of Lambda, same bridge.

## Option 3, Amazon Connect

Stays entirely inside AWS and inside your bill, and gives you call recording,
queues, reporting and hours of operation for free. It is heavier to set up, and
two of its default quotas will stop a hundred-office rollout dead:

| Quota | Default | Why it matters |
| --- | --- | --- |
| Phone numbers per instance | 10 | A hundred offices needs a hundred numbers |
| Concurrent active calls per instance | 10 | The eleventh caller gets a busy tone |

Both are adjustable but reviewed by a human, and AWS says a large request can
take **up to three weeks**. If Connect is the choice, those two requests go in
before anything else, because they are the only part of this project with a
three week tail.

The good news on Connect: **Canada needs no ID documents at all** for local or
toll-free numbers. You claim them yourself in the console, same day. Most
countries need a local address and an order form.

## What I built: Pipecat on the Vonage path

`agentcore_app/pipecat_server.py`. Pipecat ships a Vonage serializer and a Nova
Sonic service, so the phone audio handling is library code rather than mine, and
we still get speech to speech rather than dropping to Transcribe plus Polly.

Four things in there that are worth knowing, all found by running it rather
than by reading about it:

1. **A Nova Sonic session is capped at about eight minutes.** A buyer asking
   about schools, then parking, then the maintenance fee gets past that easily,
   and without handling it the call dies mid-sentence. Pipecat's session
   continuation opens the next session in the background before the ceiling,
   buffers the caller's audio across the handover, and keeps the conversation
   context. It is on, with the handover starting at six minutes.
2. **The pip extra is `pipecat-ai[aws-nova-sonic]`, not `[aws]`.** `[aws]` gives
   you Transcribe and Polly and silently leaves out Nova Sonic; you find out at
   runtime with a missing `aws_sdk_bedrock_runtime`.
3. **Pin Python 3.12, not 3.13.** Pipecat's audio helpers import `audioop`,
   which was removed from the standard library in 3.13. Pipecat declares the
   backport so a normal install is fine, but the AWS sample Dockerfiles use
   3.13 and that is a trap if you ever vendor packages by hand.
4. **`allow_interruptions` is gone in Pipecat 1.x** and passing it raises. Nova
   Sonic does its own turn detection and barge-in inside the model.

The tool definitions are not restated for Pipecat. `TOOL_SPECS` is converted at
startup, so a tool description cannot drift between the demo, the plain
WebSocket server and the phone. There is a test asserting that.

## What I would do

Start with option 1 or 2 and get a real phone call working this week, because
until you have heard it on an actual handset, everything else is guesswork. Move
to Connect later if you want the call centre features, and start its quota
requests in parallel so the wait is already running.

## The one line that makes it multi-tenant

Whichever bridge you use, it opens the WebSocket with the number the caller
dialled:

    wss://<runtime>/ws?to=%2B14165550101

`voice_server.py` reads that, looks up the office, and loads only that office's
listings. If no office owns the number it **refuses the call** rather than
falling back to a default, because a caller hearing the wrong office's
inventory is worse than a caller hearing an error. Tested by
`test_unrouted_number_is_refused_not_defaulted`.

---

## Running it before any of this

The voice server runs on your own machine, no AWS, no container:

    pip install -r agentcore_app/requirements.txt
    python agentcore_app/voice_server.py

Then `http://127.0.0.1:8080/ping` answers. The WebSocket needs Bedrock
credentials to do anything, but the health check, the office routing and all
five tools work offline, and the test suite covers them.
