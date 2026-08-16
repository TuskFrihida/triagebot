# TriageBot

Automated triage for customer inquiries submitted through a website form.

Each inquiry is summarised and classified by an AI model, recorded in Google
Sheets, and pushed to a Telegram chat so the team sees it immediately.

```
                  ┌──────────────┐
 inquiry ────────►│   validate   │──► malformed?  reported, run continues
                  └──────┬───────┘
                         ▼
                  ┌──────────────┐
                  │   dedupe     │──► already seen?  skipped, costs nothing
                  └──────┬───────┘
                         ▼
                  ┌──────────────┐
                  │   classify   │  OpenAI, schema-enforced JSON
                  └──────┬───────┘
                         ▼
                  ┌──────────────┐
                  │   Sheets     │  original inquiry + AI result
                  └──────┬───────┘
                         ▼
                  ┌──────────────┐
                  │   Telegram   │  formatted notification
                  └──────────────┘
```

## What it produces

For every inquiry the system generates:

- **Summary** — one or two neutral sentences
- **Category** — exactly one of `Sales`, `Technical Support`, `Billing`, `General Question`
- **Priority** — `Low`, `Medium` or `High`

The category and priority are **schema-enforced**, not requested in a prompt.
The model's output is constrained during generation, so a value outside those
sets cannot be produced. See [Design notes](#design-notes).

---

## Requirements

- Python 3.10 or newer (developed and tested on 3.11)
- An OpenAI API key with credit
- A Google Cloud service account with access to a spreadsheet
- A Telegram bot

---

## Setup

### 1. Install

```bash
git clone https://github.com/TuskFrihida/triagebot.git
cd triagebot

python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\activate

pip install -r requirements.txt
pip install -e .
```

Then create your configuration file:

```bash
cp .env.example .env             # Windows: copy .env.example .env
```

Fill in the values as you work through the next three sections.

### 2. OpenAI

1. Create a key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
2. Put it in `.env` as `OPENAI_API_KEY`

> The API is billed separately from a ChatGPT subscription, and new accounts do
> not include free credit. See [Running costs](#running-costs) — the amounts
> involved are very small.

### 3. Google Sheets

Authentication uses a **service account**: a robot Google identity with its own
email address. Three consequences follow, and the third is where most setup
attempts fail.

1. It is not you. It has its own Drive and starts with access to nothing.
2. Its password is a file. Whoever holds the JSON key *is* that robot.
3. **You must share the spreadsheet with it, by email, like a colleague.**
   A perfectly valid key still returns `403 PERMISSION_DENIED` if you skip this.

**Steps:**

1. At [console.cloud.google.com](https://console.cloud.google.com), create a project.
2. Under **APIs & Services → Library**, enable both:
   - **Google Sheets API**
   - **Google Drive API** *(required — the client requests the Drive scope to locate spreadsheets)*
3. Under **IAM & Admin → Service Accounts**, click **Create service account**.
   Skip the optional "grant this service account access to project" step —
   project roles are not how it gains access to your sheet; sharing is.
4. Open the account → **Keys** → **Add key → Create new key → JSON**.
5. Save the downloaded file as `credentials/google-service-account.json`.
   That directory is gitignored.
6. Create a spreadsheet. Open the JSON key, copy the `client_email` value, and
   **share the spreadsheet with that address as Editor**. Untick "Notify
   people" — it is a robot and the mail will bounce.
7. Copy the id from the spreadsheet URL into `GOOGLE_SHEET_ID`:
   ```
   https://docs.google.com/spreadsheets/d/<THIS_PART>/edit
   ```

The worksheet tab and its header row are created automatically, so a blank
spreadsheet is fine.

### 4. Telegram

The Bot API is plain HTTPS. Two rules are not obvious:

- A bot cannot start a conversation. You must message it first.
- `chat_id` is a number that appears nowhere in the Telegram UI.

**Steps:**

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, and choose a
   name plus a username ending in `bot`. Copy the token into
   `TELEGRAM_BOT_TOKEN`.
2. Open your new bot, press **Start**, and send it any message.
3. Retrieve the chat id:
   ```bash
   python scripts/get_chat_id.py
   ```
   Copy the printed value into `TELEGRAM_CHAT_ID`.

### 5. Verify everything

```bash
python scripts/check_credentials.py
```

This authenticates against all three services and sends you a test Telegram
message. It costs nothing — listing models bills no tokens, and the Google and
Telegram calls are free. Failures name the specific fix, including which email
address to share the spreadsheet with.

---

## Usage

```bash
python -m triagebot.cli --input data/test_inquiries.json
```

**Options**

| Flag | Purpose |
|------|---------|
| `--input`, `-i` | JSON file of inquiries *(required)* |
| `--log-level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` — overrides `LOG_LEVEL` |
| `--log-file` | Also write logs to a file, for unattended runs |

**Input format** — an array of objects, or a single object:

```json
[
  {
    "name": "Dana Whitfield",
    "email": "dana@northwind.example",
    "message": "Our whole team is locked out since this morning's update."
  }
]
```

Unknown fields are ignored, so form payloads carrying honeypot or CSRF fields
work without modification.

**Exit codes** — meaningful, so the command can be scheduled and monitored:

| Code | Meaning |
|------|---------|
| `0` | Every record reached a defined outcome |
| `1` | At least one record failed outright, or the run could not start |

Duplicates and malformed submissions are **expected outcomes, not errors**, and
do not fail the run. Handling them cleanly is a requirement.

---

## Testing

The test suite is entirely offline. OpenAI, Sheets and Telegram are replaced
with fakes, so it needs no credentials and spends nothing:

```bash
pip install -r requirements-dev.txt
pytest
```

Integration checks, which do talk to the real services:

| Command | Verifies | Cost |
|---------|----------|------|
| `python scripts/check_credentials.py` | All three services authenticate | free |
| `python scripts/check_classifier.py` | Schema is strict-mode valid; input validation | free |
| `python scripts/check_classifier.py --live` | One real classification | ~$0.00004 |
| `python scripts/check_sheets.py` | Write access; formula injection inert | free |
| `python scripts/check_telegram.py` | Formatting and delivery | free |
| `python scripts/check_dedupe.py` | Deduplication survives a restart | free |

Each helper is self-contained and imports nothing from `triagebot`, so a
credential problem can never be mistaken for a bug in the application.

---

## Design notes

### Schema-enforced output

`TriageResult` is converted to a JSON Schema and used to constrain the model's
decoding. Tokens that would violate the schema cannot be sampled, so an
out-of-taxonomy category is not merely unlikely — it is impossible. This
removes the entire class of bugs that comes with parsing free-form JSON out of
a chat response: code fences, invented categories, chatty preambles.

### Failure isolation

Two rules govern what happens when a step fails:

1. **An inquiry is marked processed only after the spreadsheet write
   succeeds.** A failure before that point leaves it unmarked, so the next run
   retries it rather than losing it.
2. **A notification failure is a warning, not an error.** By that point the
   record is durable. Treating Telegram as fatal would either discard the
   record or produce a duplicate row on retry.

One inquiry failing never aborts the batch.

### Deduplication

Two submissions are the same when they carry the same email **and** the same
message body. Identity by email alone is rejected — it would silently swallow a
customer's second, different question.

The definition is deliberately conservative because the failure modes are not
symmetric: processing a duplicate costs a fraction of a cent, while suppressing
a genuine inquiry costs a customer. Normalisation absorbs only cosmetic
differences (unicode composition, whitespace, email case).

Processed ids are read back from the spreadsheet rather than a local state
file, so deduplication survives restarts and works from any machine.

### Security

- Credentials are read from the environment only. Nothing is hardcoded, and
  `.gitignore` was created before any credential-bearing file existed.
- Secret fields on `Config` are declared `repr=False`, so a configuration
  object cannot leak a key into a log.
- HTTP-layer loggers are pinned to `WARNING`. A Telegram request URL contains
  the bot token, so INFO-level transport logging would write a live credential
  to disk in plain text.
- Spreadsheet writes use `value_input_option="RAW"`. Under `USER_ENTERED`, a
  message beginning with `=` would be evaluated as a formula, letting a hostile
  form submission plant a live formula in the spreadsheet
  (CSV/formula injection). `scripts/check_sheets.py` asserts this stays inert.
- Telegram messages use `parse_mode=HTML` with every interpolated value
  escaped. All of it originates from an untrusted public form.

### Swappable ingress

`sources.py` is the seam between where inquiries come from and what is done
with them. The pipeline consumes plain dictionaries, so adding an HTTP webhook,
a mailbox poller or a Google Forms responses tab means adding one function
there and changing nothing else.

### Provider independence

Leaving `OPENAI_BASE_URL` blank uses OpenAI's `/v1/responses` endpoint. Setting
it points the same code at any OpenAI-compatible provider via
`/v1/chat/completions`, since `/v1/responses` is OpenAI-only. Both paths send
the identical schema and return the identical validated result.

---

## Running costs

At `gpt-5-nano` rates ($0.05 / $1M input tokens, $0.40 / $1M output), a typical
inquiry costs roughly **$0.00004** — about **25,000 inquiries per dollar**.

Duplicates and malformed submissions are rejected *before* the API call, so
they cost nothing at all. Google Sheets and Telegram are free at this volume.

---

## Known limitations

- **Concurrency.** Deduplication reads the spreadsheet without locking. Two
  processes running simultaneously could both see an inquiry as new and both
  write it. This is fine for a single scheduled runner; a high-throughput
  deployment would need a real datastore.
- **Duplicates never expire.** A customer sending an identical message weeks
  later is suppressed. Whether repeats should be re-processed after some window
  is a business decision, not a technical one.
- **Priority is subjective for sales.** A large opportunity is rated `High` by
  the model, while the documented rubric reserves `High` for customers who are
  blocked or being harmed. Worth agreeing explicitly, since triage only works
  while `High` stays scarce.
- **Rate limits at volume.** Google Sheets throttles above roughly 60 writes
  per minute per user. Batching would be needed for bulk imports.

---

## Project layout

```
src/triagebot/
  cli.py             command-line entrypoint
  config.py          environment loading and validation, one place only
  logging_setup.py   logging configuration
  models.py          Inquiry validation, TriageResult schema
  classifier.py      OpenAI classification and summarisation
  sheets.py          Google Sheets persistence
  notifier.py        Telegram notifications
  dedupe.py          duplicate suppression
  pipeline.py        orchestration and failure isolation
  sources.py         inquiry ingress (the swappable seam)

scripts/             standalone verification helpers
tests/               offline test suite, no credentials required
data/                inquiry fixtures
credentials/         gitignored; service-account key lives here
```
