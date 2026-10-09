# VJudge Job API — how to use it

You give vjudge **a list of broken calls** and **the bot's number**. It reads each
call, writes a test that reproduces the problem using that call's real account
data, phones the bot, and tells you what happened.

You don't write test scripts. You don't need test accounts or fake customer
data. You don't need to run anything locally.

---

## What you need

**The vjudge URL.** Ask whoever runs it — it's a Cloudflare tunnel address that
changes when the service restarts, so confirm it's current rather than reusing an
old one.

```bash
export VJUDGE=https://<the-cloudflare-domain>
```

Check it's alive:

```bash
curl -s $VJUDGE/health
```

**Your broken calls**, as llm-studio links or bare interaction uuids — both work.

**The bot's phone number**, in E.164 (`+18447331982`).

**The region** those calls are in: `us`, `in`, or `ca`.

That's it. No token, no login, no local install.

---

## Step 1 — Create the job

```bash
curl -sX POST $VJUDGE/v1/jobs \
  -H 'content-type: application/json' \
  -d '{
    "calls": [
      "https://llm-studio.vjudge.dev/clients/1e31f185-16ac-4211-827e-46ba19d81a29/conversations/1354?interaction_uuid=c26d1903-1111-2222-3333-444444444444",
      "acb65a70-5555-6666-7777-888888888888"
    ],
    "config": {
      "target": "+18447331982",
      "region": "us",
      "languages": ["en-US", "es-US"],
      "max_concurrency": 2,
      "notes": "the bug is in the closing, not the authentication"
    }
  }'
```

You get a job id back straight away:

```json
{ "job_id": "job_b22e05bdaa24", "status": "created", "scenario_count": 0 }
```

VJudge then works in the background: it pulls each call from OSV, then writes one
test per call.

### The fields

| Field | Required | What it does |
|---|---|---|
| `calls[]` | **yes** | The broken calls. Full llm-studio links or bare uuids. One test is written per call. |
| `config.target` | **yes** | The **bot's** number, E.164. This is who gets phoned. Normally the **imported clone** of the live bot, not the live DID the faulty call came in on — you don't want a test dialling production. VJudge notes the difference and correlates fine either way. |
| `config.region` | **yes-ish** | `us` (default), `in`, or `ca`. **Get this right** — the same call id does not exist in another region, so a wrong region looks like a wrong id. Don't copy it from the `region=` in a link; those are often wrong. |
| `config.languages` | no | Default `["en-US"]`. Use `["en-US","es-US"]` for a bilingual bot so the test checks language handling. |
| `config.max_concurrency` | no | Default 2. How many calls run at once. |
| `config.notes` | no | Free text that steers the test writer. Genuinely useful — *"only the Spanish disclosure wording matters"* produces a sharper test than nothing. |
| `job_id` | no | One is minted for you. Supply your own only if you want to choose it; reusing an existing one is an error. |
| `callback_url` | no | Where progress gets POSTed, if you have somewhere to receive it. Skip it and use step 2 instead. |

**Every test is a real phone call.** Three broken calls means three calls placed.
Start with one.

---

## Step 2 — Wait for the tests to be written

One request that blocks until it's done. No polling loop, no webhook needed:

```bash
curl -s "$VJUDGE/v1/jobs/job_b22e05bdaa24/wait?timeout=300"
```

When `"status": "ready"`, the tests exist but **nothing has been phoned yet**.
That's deliberate — read them first:

```json
{
  "settled": true,
  "status": "ready",
  "scenarios": [
    {
      "scenario_id": "S01-c26d1903",
      "title": "EN closing must not flip to Spanish",
      "description": "Bot switched to Spanish on a short closing phrase.",
      "what_we_tested": "Auth by registered phone + SSN last-4, declined payment, then the short closing.",
      "lint_ok": true
    }
  ]
}
```

If `lint_ok` is `false` on a test, it was malformed and is excluded from the run —
`lint_errors` says why. Other tests still go ahead.

If it comes back `"settled": false`, it just needs longer. Call it again.

---

## Step 3 — Run it

```bash
curl -sX POST $VJUDGE/v1/jobs/job_b22e05bdaa24/run \
  -H 'content-type: application/json' \
  -d '{"eval": false}'
```

Then wait again — this one takes minutes, because it's placing real calls:

```bash
curl -s "$VJUDGE/v1/jobs/job_b22e05bdaa24/wait?timeout=900"
```

### `eval: false` or `eval: true`

| | |
|---|---|
| `"eval": false` | **VJudge phones, you judge.** You get a link to each call and a description of what it tested. You read the transcript and decide. |
| `"eval": true` | VJudge's own AI judge decides, and each result also gets `passed` and `overall_reasoning`. |

Use `false` if you're going to look at the calls yourself — it skips an AI judge
whose accuracy nobody has measured yet.

---

## Step 4 — Read the results

```json
{
  "status": "completed",
  "eval": false,
  "results": [
    {
      "scenario_id": "S01-c26d1903",
      "title": "EN closing must not flip to Spanish",
      "description": "Bot switched to Spanish on a short closing phrase.",
      "what_we_tested": "Auth by registered phone + SSN last-4, declined payment, then the short closing.",
      "source_interaction_uuid": "c26d1903-…",
      "call_link": "https://llm-studio.vjudge.dev/clients/00e217e9-…/conversations?interaction_uuid=a02fd6ec-…",
      "interaction_uuid": "a02fd6ec-…",
      "vjudge_session_id": "dc8747f0fa0a4a29bc6ffa3107e84e10",
      "call_status": "completed",
      "disposition": "bot_initiated_transfer",
      "bot_turns": 5,
      "caller_turns": 5
    }
  ]
}
```

**`call_link` is the thing you actually want** — open it to hear and read the test
call. `source_interaction_uuid` is the original broken call, so you can compare
the two side by side.

### Check `caller_turns` before you believe anything

**`caller_turns: 0` means the test did not happen.** The call connected, ran, and
was marked complete — but our caller never said a word, so nothing was tested.
Under `eval: false` the run still looks successful, because "success" there only
means the call connected. This is the single most important field to glance at.

A healthy result has `caller_turns` roughly matching `bot_turns`. If it's 0, the
generated persona was broken; re-create the job, and tell whoever runs vjudge.

`disposition` is the bot's own verdict on the call and is often the clearest
evidence — `bot_initiated_transfer` on a "why did it transfer me" bug is the
reproduction, right there. It's written by the platform *after* the call ends, so
vjudge waits for it; if it still comes back `null`, open the `call_link` and read
the call itself rather than concluding nothing happened.

If `call_link` and `interaction_uuid` are `null` the run could not be matched back
to an interaction. Check `call_status` first; if that says `completed`, send the
`vjudge_session_id` to whoever runs vjudge.

### `warnings`

A job may come back with a `warnings` array alongside the results. These are not
failures — the most common one notes that the number dialled differs from the DID
the faulty call arrived on, which is **expected** when testing an imported clone
of the live bot. Read them, don't panic at them.

---

## Receiving callbacks instead of waiting

If you have somewhere to receive HTTP, pass `callback_url` on create or run and
you'll get POSTed at each stage: `seeding`, `generating`, `ready`, then
`running`, `correlating`, `completed`.

If you don't, vjudge can hold them for you. Point `callback_url` at vjudge
itself:

```bash
# on create/run:
"callback_url": "'$VJUDGE'/v1/hooks/my-sink"

# then read the history, oldest first:
curl -s $VJUDGE/v1/hooks/my-sink
```

---

## The whole thing, copy-pasteable

```bash
export VJUDGE=https://<the-cloudflare-domain>

JOB=$(curl -sX POST $VJUDGE/v1/jobs -H 'content-type: application/json' -d '{
  "calls": ["<llm-studio link or uuid>"],
  "config": {"target": "+18447331982", "region": "us", "languages": ["en-US"]}
}' | jq -r .job_id)

echo "job: $JOB"
curl -s "$VJUDGE/v1/jobs/$JOB/wait?timeout=300" | jq '.status, .scenarios'

curl -sX POST $VJUDGE/v1/jobs/$JOB/run -H 'content-type: application/json' \
  -d '{"eval": false}'

curl -s "$VJUDGE/v1/jobs/$JOB/wait?timeout=900" | jq '.status, .results'
```

---

## When something goes wrong

| What you see | What it means |
|---|---|
| `400 job mode requires VJUDGE_CALL_TRANSPORT=sip` | Server-side config. Tell whoever runs vjudge; you can't fix it from here. |
| `400 unknown region '…'` | Use `us`, `in` or `ca`. |
| `400 calls must not be empty` | `calls[]` was empty. |
| `409 job … already exists` | You reused a `job_id`. Leave it out. |
| `409 … has no generated campaign to run` | No test came out valid. Check `scenarios[].lint_errors`. |
| status `failed`, *no call could be seeded* | Usually **the wrong region**, or the call id doesn't exist. The error names the reason per call. Try another region. |
| status `failed`, *no OSV token* / *OSV 401* | The server's OSV token expired. Ask the operator to refresh it — nothing you can do. |
| Results come back but `call_link` is null | The run could not be matched to an interaction. Check `call_status`; if it says `completed`, send the `vjudge_session_id` to the operator. |
| `caller_turns: 0` | The call connected but our caller never spoke, so nothing was tested. Re-create the job; report it. |
| Connection refused / DNS failure | The tunnel URL is stale. Get the current one. |

---

## Two things to be careful about

**Real calls, real money, real bot.** Each test phones a production bot. Don't
loop this, and don't send twenty calls to see what happens.

**Real customer data.** The tests are built from real interaction metadata —
names, account numbers, DOB, SSN last-4 — because that's what lets the test
authenticate and reach the bug. Treat job output as sensitive: don't paste it
into public tickets or chats.
