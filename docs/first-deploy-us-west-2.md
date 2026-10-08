# First deploy, us-west-2

You run these. I cannot log into your AWS account, and you should not give
anyone a login to it. Everything here is on your machine or in your browser.

us-west-2 is the right region. Only three regions run **both** Nova 2 Sonic and
AgentCore Runtime Instances: us-east-1, us-west-2 and Tokyo. Stockholm has Sonic
but cannot host the runtime, and Canada Central has neither.

---

## Step 1, turn on the model. Browser, two minutes

1. Console, **top right, set the region to US West (Oregon) us-west-2**. Almost
   every failure at this stage is being in the wrong region.
2. **Amazon Bedrock**, left menu, **Model access**.
3. **Enable specific models**, tick **Amazon Nova 2 Sonic**, submit.
4. Wait for it to say **Access granted**. It is usually instant.

Nothing below works until that says granted.

## Step 2, get the code. Your PC, two minutes

Install [Node.js](https://nodejs.org) and [Python](https://www.python.org/downloads/)
if you do not have them. On the first screen of the Python installer, tick
**Add python.exe to PATH**.

    git clone https://github.com/anirudhatalmale6-alt/voice-realestate-agentcore
    cd voice-realestate-agentcore

No git? Use the green **Code** button, **Download ZIP**, and unzip it.

## Step 3, prove it works before spending anything

    run_demo.bat

A full phone call prints on screen, for two different offices. No AWS involved.
If this fails, stop here and send me what it printed.

## Step 4, connect your account

    pip install awscli
    aws configure

It asks four things. Region is `us-west-2`, output format `json`. For the two
keys, in the console: **IAM**, **Users**, your user, **Security credentials**,
**Create access key**, choose **Command Line Interface**.

Check it took:

    aws sts get-caller-identity

That prints your account number. Now a real call to the model:

    aws bedrock list-foundation-models --region us-west-2 --query "modelSummaries[?contains(modelId,'sonic')].modelId"

If Sonic is listed, step 1 worked.

## Step 5, deploy

    npm install -g @aws/agentcore
    agentcore import
    agentcore deploy

It builds an ARM64 container in the cloud with CodeBuild, pushes it to ECR and
creates the runtime. **No Docker needed on your PC.** First run takes about ten
minutes, almost all of it the container build.

Then:

    agentcore status

## If it fails

The four that actually happen, in order of likelihood:

1. **Wrong region.** The console remembers the last region per service, so you
   can be in us-west-2 for Bedrock and us-east-1 for ECR without noticing.
2. **Image is not ARM64.** Only bites if you build locally. `agentcore deploy`
   gets it right.
3. **Hyphen in the runtime name.** `realestate_voice` works,
   `realestate-voice` errors. Everything here uses underscores already.
4. **The auto-created IAM role has the wrong ECR path**, so the runtime cannot
   pull the image. Check the role before you look inside the container.

Then `agentcore logs` and send me what it says.

## What this does not give you yet

A phone number. Step 5 gets the agent running and reachable over a WebSocket,
which you can test from a browser. Taking an actual phone call needs the Vonage
side as well, which is the next piece and needs a Vonage account. See
`docs/deploy.md` for why Vonage rather than Twilio.
