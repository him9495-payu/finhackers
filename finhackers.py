"""
FinHackers WhatsApp personal-loan chatbot.

This module exposes a FastAPI application that can be used as the webhook
recipient for Meta's WhatsApp Cloud API. The chatbot guides applicants through
a full loan intake journey, relays the collected information to downstream
credit-decision services, and communicates approvals or denials back to the
applicant over WhatsApp.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, ValidationError, validator


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("finhackers.loanbot")


app = FastAPI(
    title="FinHackers Personal Loan Chatbot",
    version="1.0.0",
    summary="WhatsApp Cloud API chatbot that collects loan applications end-to-end.",
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "finhackers-verify-token")
BACKEND_DECISION_URL = os.getenv("BACKEND_DECISION_URL")
BACKEND_API_KEY = os.getenv("BACKEND_DECISION_API_KEY")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
class LoanApplication(BaseModel):
    application_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    customer_phone: str
    full_name: str
    age: int = Field(ge=18, le=75)
    employment_status: str
    monthly_income: float = Field(gt=0)
    requested_amount: float = Field(gt=0)
    purpose: str
    consent_to_credit_check: bool

    @validator("employment_status")
    def normalize_employment(cls, value: str) -> str:
        return value.strip().title()

    @validator("purpose")
    def normalize_purpose(cls, value: str) -> str:
        return value.strip().capitalize()


class DecisionResult(BaseModel):
    approved: bool
    offer_amount: float
    apr: float
    max_term_months: int
    reason: Optional[str] = None
    reference_id: str


# ---------------------------------------------------------------------------
# Conversation state machine
# ---------------------------------------------------------------------------
QUESTION_FLOW: List[Tuple[str, str]] = [
    ("full_name", "Let's get started! What is your full legal name?"),
    ("age", "How old are you? Applicants must be between 18 and 75."),
    (
        "employment_status",
        "What best describes your employment status? (Employed, Self-employed, "
        "Contractor, Unemployed, Student, Retired)",
    ),
    ("monthly_income", "What is your average monthly income in USD?"),
    ("requested_amount", "How much would you like to borrow (USD)?"),
    ("purpose", "What will you use the funds for?"),
    (
        "consent_to_credit_check",
        "Do you consent to a credit check to continue? Reply YES to proceed.",
    ),
]


def normalize_boolean(value: str) -> Optional[bool]:
    truthy = {"y", "yes", "true", "ok", "sure", "consent", "agree"}
    falsy = {"n", "no", "false", "stop", "decline"}
    val = value.strip().lower()
    if val in truthy:
        return True
    if val in falsy:
        return False
    return None


def parse_numeric(value: str, value_type):
    try:
        return value_type(value.replace(",", "").strip())
    except (ValueError, AttributeError):
        raise ValueError("Please provide a numeric value.")


@dataclass
class ConversationState:
    step_index: int = 0
    answers: Dict[str, str] = field(default_factory=dict)

    def current_field(self) -> Optional[str]:
        if self.step_index < len(QUESTION_FLOW):
            return QUESTION_FLOW[self.step_index][0]
        return None

    def current_prompt(self) -> Optional[str]:
        if self.step_index < len(QUESTION_FLOW):
            return QUESTION_FLOW[self.step_index][1]
        return None

    def advance(self):
        self.step_index += 1

    def reset(self):
        self.step_index = 0
        self.answers.clear()


class ConversationStore:
    """Simple in-memory store. Replace with Redis or a database in production."""

    def __init__(self):
        self._store: Dict[str, ConversationState] = {}

    def get_or_create(self, phone: str) -> ConversationState:
        if phone not in self._store:
            self._store[phone] = ConversationState()
        return self._store[phone]

    def clear(self, phone: str):
        self._store.pop(phone, None)


conversation_store = ConversationStore()


# ---------------------------------------------------------------------------
# Meta WhatsApp integration
# ---------------------------------------------------------------------------
class MetaWhatsAppClient:
    def __init__(self, token: Optional[str], phone_number_id: Optional[str]):
        self.token = token
        self.phone_number_id = phone_number_id
        self.base_url = (
            f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
            if phone_number_id
            else None
        )

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.base_url)

    async def send_text(self, to: str, body: str) -> None:
        if not self.enabled:
            logger.info("[dry-run] -> %s: %s", to, body)
            return

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(self.base_url, json=payload, headers=headers)
            if response.is_error:
                logger.error(
                    "Failed to send message to %s - %s %s",
                    to,
                    response.status_code,
                    response.text,
                )
                response.raise_for_status()


messenger = MetaWhatsAppClient(META_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID)


# ---------------------------------------------------------------------------
# Backend credit decision client
# ---------------------------------------------------------------------------
class CreditDecisionClient:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = base_url
        self.api_key = api_key

    async def evaluate(self, application: LoanApplication) -> DecisionResult:
        if not self.base_url:
            logger.info("Using offline decision rules for application %s", application.application_id)
            return self._local_rules(application)

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/decisions",
                json=application.dict(),
                headers=headers,
            )
            if response.is_error:
                logger.error(
                    "Decision service error (%s): %s", response.status_code, response.text
                )
                response.raise_for_status()
            payload = response.json()
        try:
            return DecisionResult(**payload)
        except ValidationError as exc:
            logger.error("Malformed decision payload: %s", exc)
            raise HTTPException(status_code=500, detail="Invalid decision payload")

    @staticmethod
    def _local_rules(application: LoanApplication) -> DecisionResult:
        debt_to_income = application.requested_amount / max(application.monthly_income, 1)
        approved = (
            application.age >= 21
            and application.monthly_income >= 2000
            and debt_to_income <= 6
            and application.consent_to_credit_check
        )
        apr = 12.99 if application.monthly_income >= 6000 else 18.49
        offer_amount = min(application.requested_amount, application.monthly_income * 5)
        reason = None
        if not approved:
            if application.age < 21:
                reason = "Applicants must be at least 21 years old."
            elif application.monthly_income < 2000:
                reason = "Monthly income below minimum threshold of $2,000."
            elif debt_to_income > 6:
                reason = "Requested amount exceeds permitted debt-to-income ratio."
            elif not application.consent_to_credit_check:
                reason = "Consent to credit check is required."
        return DecisionResult(
            approved=approved,
            offer_amount=round(offer_amount, 2),
            apr=apr,
            max_term_months=60 if approved else 0,
            reason=reason,
            reference_id=application.application_id,
        )


decision_client = CreditDecisionClient(BACKEND_DECISION_URL, BACKEND_API_KEY)


# ---------------------------------------------------------------------------
# Chatbot logic
# ---------------------------------------------------------------------------
WELCOME_MESSAGE = (
    "👋 Hi! I'm Fin, your personal loan assistant. I can help you apply for a "
    "loan in just a few questions."
)


def extract_message_text(message: Dict) -> Optional[str]:
    if "text" in message and message["text"].get("body"):
        return message["text"]["body"]
    if "button" in message:
        return message["button"].get("text")
    return None


async def start_conversation(phone: str, state: ConversationState) -> None:
    state.reset()
    await messenger.send_text(phone, WELCOME_MESSAGE)
    await messenger.send_text(phone, QUESTION_FLOW[0][1])


async def handle_text_response(phone: str, text: str, state: ConversationState) -> None:
    field = state.current_field()
    if not field:
        await messenger.send_text(
            phone,
            "Your application is already submitted. Reply APPLY to start over.",
        )
        return

    try:
        processed_value = validate_response(field, text)
    except ValueError as exc:
        await messenger.send_text(phone, f"{exc} Please try again.")
        return

    state.answers[field] = processed_value
    state.advance()

    if state.current_field() is None:
        await finalize_application(phone, state)
        conversation_store.clear(phone)
        return

    next_prompt = state.current_prompt()
    if next_prompt:
        await messenger.send_text(phone, next_prompt)


def validate_response(field: str, value: str):
    if field == "age":
        age = parse_numeric(value, int)
        if age < 18 or age > 75:
            raise ValueError("Age must be between 18 and 75.")
        return age
    if field in {"monthly_income", "requested_amount"}:
        amount = parse_numeric(value, float)
        if amount <= 0:
            raise ValueError("Amount must be greater than zero.")
        return round(amount, 2)
    if field == "consent_to_credit_check":
        consent = normalize_boolean(value)
        if consent is None:
            raise ValueError("Please reply YES to continue or NO to stop.")
        if not consent:
            raise ValueError("Consent is required to proceed. Reply YES when ready.")
        return consent
    return value.strip()


async def finalize_application(phone: str, state: ConversationState) -> None:
    try:
        application = LoanApplication(
            customer_phone=phone,
            full_name=state.answers["full_name"],
            age=state.answers["age"],
            employment_status=state.answers["employment_status"],
            monthly_income=state.answers["monthly_income"],
            requested_amount=state.answers["requested_amount"],
            purpose=state.answers["purpose"],
            consent_to_credit_check=state.answers["consent_to_credit_check"],
        )
    except KeyError as exc:
        logger.error("Missing field before finalization: %s", exc)
        await messenger.send_text(phone, "We hit a snag collecting your data. Let's start over.")
        conversation_store.clear(phone)
        return

    await messenger.send_text(
        phone,
        "Thanks! I'm submitting your application for a quick review. "
        "This usually takes just a few seconds.",
    )
    decision = await decision_client.evaluate(application)
    if decision.approved:
        message = (
            "🎉 You're approved!\n"
            f"- Offer amount: ${decision.offer_amount:,.2f}\n"
            f"- APR: {decision.apr:.2f}%\n"
            f"- Maximum term: {decision.max_term_months} months\n"
            f"Reference ID: {decision.reference_id}\n"
            "Reply ACCEPT to proceed with documentation or APPLY to submit a new request."
        )
    else:
        message = (
            "We're sorry, but we couldn't approve the loan at this time."
            f"\nReason: {decision.reason or 'Not specified'}\n"
            "Reply APPLY if you'd like to try again with updated details."
        )
    await messenger.send_text(phone, message)


async def handle_incoming_message(message: Dict) -> None:
    from_phone = message.get("from")
    if not from_phone:
        return

    text = extract_message_text(message)
    if not text:
        await messenger.send_text(
            from_phone,
            "I currently support text responses only. Please reply using text.",
        )
        return

    normalized = text.strip().lower()
    state = conversation_store.get_or_create(from_phone)

    if normalized in {"hi", "hello", "start", "apply", "loan"} or not state.answers:
        await start_conversation(from_phone, state)
        return

    if normalized == "accept":
        await messenger.send_text(
            from_phone,
            "Great! A loan specialist will reach out with the contract shortly. "
            "Reply APPLY anytime to submit another request.",
        )
        conversation_store.clear(from_phone)
        return

    await handle_text_response(from_phone, text, state)


# ---------------------------------------------------------------------------
# Webhook endpoints
# ---------------------------------------------------------------------------
@app.get("/webhook")
async def verify_webhook(
    hub_mode: Optional[str] = Query(default=None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(default=None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(default=None, alias="hub.challenge"),
):
    if hub_mode != "subscribe":
        raise HTTPException(status_code=400, detail="Invalid mode")
    if hub_verify_token != META_VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Verification token mismatch")
    return PlainTextResponse(hub_challenge or "")


@app.post("/webhook")
async def receive_webhook(payload: Dict):
    messages = extract_messages(payload)
    if not messages:
        return JSONResponse({"status": "ignored"})
    await asyncio.gather(*(handle_incoming_message(msg) for msg in messages))
    return JSONResponse({"status": "processed"})


def extract_messages(body: Dict) -> List[Dict]:
    messages: List[Dict] = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            contacts = value.get("contacts", [])
            for message in value.get("messages", []):
                payload = {"from": message.get("from"), "id": message.get("id"), **message}
                if contacts:
                    payload["profile"] = contacts[0].get("profile", {})
                messages.append(payload)
    return messages


# ---------------------------------------------------------------------------
# Local development helpers
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthcheck():
    return {"status": "ok", "messenger_enabled": messenger.enabled}


def run():
    """Allow `python finhackers.py` to launch a development server."""
    import uvicorn

    uvicorn.run(
        "finhackers:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=bool(int(os.environ.get("RELOAD", "0"))),
    )


if __name__ == "__main__":
    run()
