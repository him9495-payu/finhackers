# PayU Finance WhatsApp Loan Chatbot

Comprehensive guide for the bilingual (English/Hindi) personal-loan assistant that runs on Meta's WhatsApp Cloud API. The chatbot lives in `finhackers.py`, exposes FastAPI endpoints, supports AWS Lambda deployments, and orchestrates onboarding plus customer-support journeys for PayU Finance (Indian NBFC).

---

## System Architecture
- **FastAPI surface (`app`)**: Implements `GET /webhook` (Meta verification), `POST /webhook` (event ingestion), and `GET /healthz`. The same app can be hosted on EC2/Fargate or inside AWS Lambda via Mangum (`lambda_handler`).
- **Conversation brain**: `ConversationStore`, `ConversationState`, and language packs route users through language selection, existing/new inference, onboarding, and support. Inactivity handling automatically nudges drop-offs.
- **Persistent user context**: `UserProfileStore` persists structured data (language, stage, status, metadata) to DynamoDB; a local fallback keeps the bot functional when AWS resources are unavailable.
- **Meta Cloud integration**: `MetaWhatsAppClient` sends text, button-based quick replies, and WhatsApp Flow forms (interactive loan intake). Dry-run logging is used when credentials are absent.
- **Decisioning & support**:
  - `CreditDecisionClient` forwards completed `LoanApplication` payloads to a backend underwriting API or a built-in heuristic engine.
  - `SupportAssistant` provides lightweight Q&A from curated knowledge and escalates to a human queue when confidence is low.

---

## Key Journeys
### Onboarding (New or Returning Borrower)
1. **First touch** – chatbot greets the user, requests language preference (English/Hindi) using WhatsApp buttons, and infers whether the customer is new or existing (profile lookup + natural-language cues).
2. **Journey selection** – depending on their status, the bot offers options such as _Apply for a loan_ or _Get help/support_.
3. **WhatsApp Flow form** – when `WHATSAPP_FLOW_ID` is configured, the bot dispatches a native WhatsApp Flow form to capture user data. A text-based fallback mirrors the same prompts:
   - Full name (PAN)
   - Age (18–75)
   - Employment status
   - Monthly income (₹, > 0)
   - Requested amount (₹, > 0)
   - Loan purpose
   - Consent to bureau check
4. **Decision + messaging** – data is persisted to DynamoDB, converted to `LoanApplication`, and evaluated by `CreditDecisionClient`. Users receive localized approval or rejection narratives with reference IDs and next-step CTAs (e.g., reply `ACCEPT` or `SUPPORT`).
5. **Drop-off handling** – inactivity (default 30 min) triggers a reminder (“Reply CONTINUE to resume”). Returning customers resume in the correct language and stage.

### Customer Support (Existing Borrower)
1. User selects _Get help/support_ or sends a support-intent message (e.g., “EMI issue”).
2. `SupportAssistant` searches a bilingual knowledge base (swap with an AWS Bedrock LLM when fine-tuned material is ready).
3. When confidence is below threshold (0.55 by default), the bot:
   - Acknowledges escalation in the user’s language.
   - Logs the ticket metadata (question, timestamp, queue) against the DynamoDB profile.
   - Notifies operators via the configured `HUMAN_HANDOFF_QUEUE`.

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
| `BEDROCK_MODEL_ID` | Optional | Hook to an AWS Bedrock LLM for richer support. |
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
