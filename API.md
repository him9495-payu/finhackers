# PayU Finance WhatsApp Loan Chatbot

Comprehensive guide for the bilingual (English/Hindi) personal-loan assistant that runs on Meta's WhatsApp Cloud API. The chatbot lives in `finhackers.py`, exposes FastAPI endpoints, supports AWS Lambda deployments, and orchestrates onboarding plus customer-support journeys for PayU Finance (Indian NBFC).

---

## System Architecture
- **FastAPI surface (`app`)**: Implements `GET /webhook` (Meta verification), `POST /webhook` (event ingestion), and `GET /healthz`. The same app can be hosted on EC2/Fargate or inside AWS Lambda via Mangum (`lambda_handler`).
- **Conversation brain**: `ConversationStore`, `ConversationState`, and language packs drive language selection, infer customer status from DynamoDB (no customer-facing questions), and steer users via WhatsApp buttons/flows rather than free text.
- **Persistent user context**: `UserProfileStore` persists structured data (language, stage, status, metadata) to DynamoDB; a local fallback keeps the bot functional when AWS resources are unavailable.
- **Meta Cloud integration**: `MetaWhatsAppClient` sends text, button-based quick replies, and WhatsApp Flow forms (interactive loan intake). Dry-run logging is used when credentials are absent.
- **Decisioning & support**:
  - `CreditDecisionClient` forwards completed `LoanApplication` payloads to a backend underwriting API or a built-in heuristic engine.
  - `SupportAssistant` provides lightweight Q&A from curated knowledge and escalates to a human queue when confidence is low.

---

## Key Journeys
### Onboarding (New or Returning Borrower)
1. **First touch** – chatbot greets the user, requests language preference (English/Hindi) using WhatsApp buttons, and determines whether the customer is new or existing purely from DynamoDB history (no question is asked in chat). At any point customers can type `language` to reopen the selector; this is the only free-text trigger for changing languages.
2. **Journey selection** – once language is set, the bot presents button-based options such as _Apply for a loan_ or _Get support_, eliminating manual typing wherever possible.
3. **WhatsApp Flow form** – when `WHATSAPP_FLOW_ID` is configured, the bot dispatches a native WhatsApp Flow form to capture user data. The form is the only channel for collecting application details; if it closes, the bot re-opens the same flow via buttons instead of reverting to inline questions:
   - Full name (PAN)
   - Age (18–75)
   - Employment status
   - Monthly income (₹, > 0)
   - Requested amount (₹, > 0)
   - Loan purpose
   - Consent to bureau check
4. **Decision + messaging** – data is persisted to DynamoDB, converted to `LoanApplication`, and evaluated by `CreditDecisionClient`. Users receive localized approval or rejection narratives with reference IDs and interactive buttons (Accept offer / Need support) so they never have to type follow-up commands.
5. **Drop-off handling** – inactivity (default 30 min) triggers a reminder (“Tap Apply to continue or Support for help”). Returning customers resume in the correct language and stage.

### Customer Support (Existing Borrower)
1. User taps _Get support_ (button) or sends a support-intent message (e.g., “EMI issue”). The bot already knows from DynamoDB whether they hold an active PayU loan and adjusts tone and CTAs automatically.
2. The bot surfaces button-based support categories (Pay EMI, Loan status, documents, change EMIs, talk to agent). Selecting a category routes the intent to Amazon Bedrock (seeded with PayU’s KB snippets plus any `loan_records` data) and returns the generated answer; “Talk to agent” skips straight to escalation.
3. When the user needs something outside the options or types free-form text, the full question plus curated KB context and loan metadata is sent to the configured Bedrock LLM. If Bedrock is unavailable or confidence remains low (<0.55), the bot:
   - Acknowledges escalation in the user’s language.
   - Logs ticket metadata (question, timestamp, queue) against the DynamoDB profile.
   - Notifies operators via the configured `HUMAN_HANDOFF_QUEUE`.
4. Every post-disbursal query is also classified by Bedrock into **Query / Request / Complaint** and saved in `interaction_events` for downstream triage and analytics.

---

## Environment & Deployment

| Variable | Required | Description |
| --- | --- | --- |
| `META_ACCESS_TOKEN` | Yes (prod) | Permanent token from Meta Developer Portal. |
| `WHATSAPP_PHONE_NUMBER_ID` | Yes (prod) | Business number ID tied to the WhatsApp Cloud API app. |
| `META_VERIFY_TOKEN` | Yes | Shared secret for webhook validation (defaults to `payu-verify-token`). |
| `BACKEND_DECISION_URL` | Optional | Base URL of PayU’s credit-decision microservice. |
| `BACKEND_DECISION_API_KEY` | Optional | Bearer token for the decision service. |
| `USER_TABLE_NAME` | Optional | DynamoDB table storing user profiles (key: `phone`). |
| `AWS_REGION` | Optional | Region for DynamoDB/Bedrock (`ap-south-1` default). |
| `INACTIVITY_MINUTES` | Optional | Minutes before drop-off reminders (default `30`). |
| `BEDROCK_MODEL_ID` | Optional (recommended) | Amazon Bedrock model ID (e.g., `anthropic.claude-3-haiku-20240307`) used for answering support queries. |
| `INTERACTION_TABLE_NAME` | Optional | DynamoDB table storing every inbound/outbound interaction for auditing and segmentation. |
| `LOAN_TABLE_NAME` | Optional (recommended) | DynamoDB table containing disbursal records (`phone` as key) used for post-loan servicing answers. |
| `WHATSAPP_FLOW_ID` | Optional | WhatsApp Flow identifier for onboarding forms. |
| `WHATSAPP_FLOW_TOKEN` | Optional | Token required by Flow submissions (auto-randomized if omitted). |
| `HUMAN_HANDOFF_QUEUE` | Optional | Queue/topic name for agent escalations. |
| `PORT`, `RELOAD`, `LOG_LEVEL` | Optional | Local FastAPI settings. |

### Local execution
```bash
pip install fastapi uvicorn httpx pydantic mangum
python finhackers.py
```
Expose the server via `ngrok http 8000` (or similar) and register the HTTPS endpoint inside Meta Cloud settings when testing WhatsApp callbacks.

### AWS Lambda
- Package dependencies (FastAPI, Mangum, httpx, boto3) with the codebase.
- Deploy behind API Gateway HTTP API; set handler to `finhackers.lambda_handler`.
- Grant IAM permissions for DynamoDB (if used) and outbound HTTPS access (Meta + credit backend).

---

## WhatsApp Cloud Contract
- **Verification** `GET /webhook`: validates `hub.mode=subscribe`, `hub.verify_token`, and echoes `hub.challenge`.
- **Events** `POST /webhook`: consumes standard `entry[].changes[].value.messages[]` payloads. Supported message types:
  - Text replies
  - Button/list replies (`interactive.button_reply`, `interactive.list_reply`)
  - WhatsApp Flow / NFM submissions (`interactive.nfm_reply.response_json`)
- **Health** `GET /healthz`: returns status plus toggles for messenger, decision backend, and DynamoDB availability.

---

## Data & Decisioning
- `LoanApplication` → `DecisionResult` mirrors backend contracts. When no backend is configured, the offline heuristic enforces NBFC guardrails (age ≥ 21, income ≥ ₹20k, DTI ≤ 8, consent required, offer cap `min(requested_amount, monthly_income * 6)`).
- User lifecycle and loan metadata (reference IDs, last support query, escalations) are written to DynamoDB and can be streamed into CRM or analytics pipelines.

### Recommended DynamoDB Tables
- **`user_profiles`**
  - Partition key: `phone` (string)
  - Attributes: `language`, `is_existing`, `status`, `stage`, `last_activity` (float seconds), `metadata` (map storing last application ID, last support query, escalation info), `created_at`, `updated_at`
  - Used to determine whether a user is new or existing before each interaction.
- **`interaction_events`**
  - Partition key: `phone`, sort key: `timestamp` (ISO string generated per event)
  - Attributes: `direction` (`inbound`, `outbound`, `system`), `category` (e.g., `whatsapp_message`, `loan_decision`, `support_answer`), `payload` (arbitrary JSON storing message text, Flow fields, decision offers, etc.), `created_at`, `updated_at`
  - Captures every inbound WhatsApp message, Flow submission, system action, and outbound response so journeys can be replayed, audited, or exported to analytics.
- **`loan_records`**
  - Partition key: `phone`
  - Attributes: `reference_id`, `offer_amount`, `apr`, `max_term_months`, `status`, `next_emi_due`, `documents_url`, `emi_schedule`, repayment flags, `created_at`, `updated_at`
  - Queried whenever borrowers ask for balance, EMI details, statements, documents, repayment changes, etc.; the same data is injected into Bedrock prompts to personalize both answers and Query/Request/Complaint classifications.

---

## Testing Recipes
1. **Webhook validation**
   ```bash
   curl "http://localhost:8000/webhook?hub.mode=subscribe&hub.verify_token=payu-verify-token&hub.challenge=test"
   ```
2. **Kick off a conversation**
   ```bash
   curl -X POST http://localhost:8000/webhook \
     -H "Content-Type: application/json" \
     -d '{
       "entry": [{
         "changes": [{
           "value": {
             "messages": [{
               "from": "919999000111",
               "text": { "body": "Hi" },
               "id": "wamid.MOCK"
             }]
           }
         }]
       }]
     }'
   ```
3. **Simulate Flow submission** – craft a payload with `interactive.nfm_reply.response_json` containing keys defined in `FORM_FIELD_MAP`.

Logs show routing decisions, DynamoDB persistence (or local fallback), and the decision payload produced for underwriting.

---

## Operational Checklist
- [ ] Register webhook + Flow inside Meta Developer Portal; keep credentials inside AWS Secrets Manager.
- [ ] Provision DynamoDB table (`phone` as partition key) with on-demand capacity and allow Lambda/containers to read/write.
- [ ] Configure CI/CD to package dependencies and run `python3 -m compileall finhackers.py`.
- [ ] Monitor logs/metrics for Meta send failures, decision-service timeouts, and support escalations.
- [ ] Extend `SupportAssistant` with PayU-specific KB or integrate AWS Bedrock when training data is ready.
