# FinHackers WhatsApp Loan Chatbot

End-to-end documentation for the personal-loan WhatsApp chatbot powered by the Meta Cloud API. The chatbot lives entirely inside `finhackers.py` and exposes a FastAPI application with production-ready endpoints plus local development helpers.

---

## Architecture Overview
- **FastAPI app (`app`)**: Hosts WhatsApp webhook verification (`GET /webhook`), message ingestion (`POST /webhook`), and operational endpoint (`GET /healthz`).
- **Conversation engine**: `ConversationStore`, `ConversationState`, and `QUESTION_FLOW` guide applicants through a deterministic intake sequence that captures name, age, employment, income, requested amount, purpose, and consent.
- **Meta Cloud integration**: `MetaWhatsAppClient` wraps the `v18.0/{phone-number-id}/messages` endpoint for sending replies. When no credentials are provided, it falls back to dry-run logging for local testing.
- **Credit decision client**: `CreditDecisionClient` pushes completed applications to an external underwriting service (`POST /decisions`) or falls back to built-in decision rules to simulate approvals/denials.

---

## Environment Configuration
Set the following variables before deploying:

| Variable | Required | Description |
| --- | --- | --- |
| `META_ACCESS_TOKEN` | Yes (prod) | Permanent token created in Meta App dashboard. |
| `WHATSAPP_PHONE_NUMBER_ID` | Yes (prod) | ID of the approved WhatsApp Business number. |
| `META_VERIFY_TOKEN` | Optional | Shared secret used during webhook verification (defaults to `finhackers-verify-token`). |
| `BACKEND_DECISION_URL` | Optional | Base URL for the credit decision microservice (e.g., `https://credit.example.com`). |
| `BACKEND_DECISION_API_KEY` | Optional | Bearer token attached to outbound underwriting requests. |
| `PORT`, `RELOAD`, `LOG_LEVEL` | Optional | FastAPI/Uvicorn runtime knobs for local development. |

---

## Running Locally
```bash
pip install fastapi uvicorn httpx pydantic
export META_ACCESS_TOKEN=...
export WHATSAPP_PHONE_NUMBER_ID=...
python finhackers.py
```
If the Meta credentials are omitted, outbound messages are logged instead of sent, allowing you to exercise the conversation flow with mocked webhooks.

Use ngrok (or similar) to expose your local server and register the URL inside the WhatsApp Cloud API dashboard:
```bash
ngrok http 8000
```

---

## Webhook Contract

### Verify endpoint
- **Method**: `GET /webhook`
- **Query params**: `hub.mode`, `hub.verify_token`, `hub.challenge`
- **Behavior**: Validates `hub.verify_token` against `META_VERIFY_TOKEN` and echoes `hub.challenge`. Required by Meta when registering or refreshing the webhook.

### Message receiver
- **Method**: `POST /webhook`
- **Payload**: Standard WhatsApp Cloud `entry[].changes[].value.messages[]` events.
- **Flow**:
  1. Extracts each customer message (currently supports text and quick-reply button payloads).
  2. Routes the user through the intake state machine.
  3. On completion, builds a `LoanApplication` object and forwards it to `CreditDecisionClient`.
  4. Delivers approval/denial messages with loan terms and a reference ID.

### Health probe
- **Method**: `GET /healthz`
- **Response**: `{ "status": "ok", "messenger_enabled": true|false }`

---

## Conversation Journey
Order of prompts and validations:

1. `full_name` – captures full legal name (free text).
2. `age` – numeric validation between 18 and 75.
3. `employment_status` – normalized to Title Case; free text options shared in prompt.
4. `monthly_income` – numeric (USD), must be > 0.
5. `requested_amount` – numeric (USD), must be > 0.
6. `purpose` – free text.
7. `consent_to_credit_check` – expects “YES/NO”; conversation halts without consent.

Keywords such as `hi`, `hello`, `start`, `loan`, or `apply` reset the journey. After approval, replying `ACCEPT` marks the deal for follow-up and clears the session; `APPLY` starts a new intake at any time.

---

## Backend Decision Workflow
Completed applications are represented by the `LoanApplication` model and dispatched to `/decisions` on the configured backend. Responses must match the `DecisionResult` schema:

```json
{
  "approved": true,
  "offer_amount": 5000,
  "apr": 15.75,
  "max_term_months": 48,
  "reason": null,
  "reference_id": "991ceef6-..."
}
```

When `BACKEND_DECISION_URL` is absent, a built-in heuristic is used:
- Minimum age 21, monthly income ≥ $2,000, debt-to-income ratio ≤ 6, and consent required.
- APR tiers at 12.99% and 18.49% depending on monthly income.
- Offer amount capped at `min(requested_amount, monthly_income * 5)`.

---

## Testing the Flow
1. **Webhook verification**: `curl "http://localhost:8000/webhook?hub.mode=subscribe&hub.verify_token=finhackers-verify-token&hub.challenge=check"`
2. **Simulate inbound WhatsApp message**:
```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "entry": [{
      "changes": [{
        "value": {
          "messages": [{
            "from": "15551234567",
            "text": { "body": "Apply" },
            "id": "wamid.HBg..."
          }]
        }
      }]
    }]
  }'
```
3. Observe replies in logs (dry-run) or on WhatsApp (when credentials are supplied).

---

## Deployment Checklist
- [ ] Configure Meta webhook URL to point to your hosted `/webhook`.
- [ ] Store access tokens and API keys in your secret manager.
- [ ] Scale the FastAPI app behind HTTPS (e.g., via AWS ALB, Cloud Run, Azure Web Apps).
- [ ] Swap `ConversationStore` with a shared cache (Redis/Dynamo) for multi-instance deployments.
- [ ] Replace or extend the offline rules engine with live underwriting services as needed.
