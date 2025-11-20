"""
PayU Finance WhatsApp chatbot for bilingual onboarding and support.

This module exposes a FastAPI application (and an AWS Lambda handler via Mangum)
that integrates Meta's WhatsApp Cloud API, DynamoDB for user state, and a
pluggable support assistant. The bot infers whether a customer is new or
existing, runs an onboarding journey with WhatsApp forms, and answers support
queries in English and Hindi, escalating to a human agent when needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, ValidationError, validator

try:
    import boto3
except ImportError:  # pragma: no cover - boto3 not available locally
    boto3 = None

try:
    from mangum import Mangum
except ImportError:  # pragma: no cover - mangum optional for local runs
    Mangum = None


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("payu.loanbot")


app = FastAPI(
    title="PayU Finance WhatsApp Personal Loan Chatbot",
    version="2.0.0",
    summary=(
        "Multilingual onboarding & support assistant for PayU Finance customers "
        "powered by Meta WhatsApp Cloud API."
    ),
)

_lambda_adapter = Mangum(app) if Mangum else None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "payu-verify-token")
BACKEND_DECISION_URL = os.getenv("BACKEND_DECISION_URL")
BACKEND_API_KEY = os.getenv("BACKEND_DECISION_API_KEY")
USER_TABLE_NAME = os.getenv("USER_TABLE_NAME")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
INACTIVITY_MINUTES = int(os.getenv("INACTIVITY_MINUTES", "30"))
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID")
WHATSAPP_FLOW_ID = os.getenv("WHATSAPP_FLOW_ID")
WHATSAPP_FLOW_TOKEN = os.getenv("WHATSAPP_FLOW_TOKEN")
HUMAN_HANDOFF_QUEUE = os.getenv("HUMAN_HANDOFF_QUEUE", "payu-finance-support")
DEFAULT_LANGUAGE = "en"


# ---------------------------------------------------------------------------
# Language packs & prompts
# ---------------------------------------------------------------------------
LANGUAGE_PACKS: Dict[str, Dict[str, str]] = {
    "en": {
        "welcome": "👋 Namaste from PayU Finance! I'm your personal loan assistant.",
        "language_prompt": "Please choose your preferred language.\n1️⃣ English\n2️⃣ हिंदी (Hindi)",
        "language_option_en": "English",
        "language_option_hi": "हिंदी",
        "existing_probe": "Are you already a PayU Finance customer?",
        "existing_yes": "Yes, I have a PayU loan",
        "existing_no": "No, I'm new",
        "intent_prompt_existing": "How can I help you today?",
        "intent_prompt_new": "Thanks! What would you like to do today?",
        "intent_apply": "Apply for a loan",
        "intent_support": "Get help / support",
        "support_prompt_existing": "Please describe the issue or question about your current PayU Finance loan.",
        "support_prompt_new": "Happy to help! Share your question or type APPLY to begin a new loan.",
        "support_handoff": "I'll connect you with a PayU expert so you don't have to wait.",
        "support_closing": "Glad to help! Reply SUPPORT anytime if you need anything else.",
        "support_escalation_ack": "A PayU specialist has been notified. You will hear from us shortly.",
        "onboarding_intro": "Great, let's begin your personal loan journey. This takes under 2 minutes.",
        "flow_sent": "I've shared a quick WhatsApp form. If it doesn't open, just reply with the details here.",
        "dropoff": "It looks like we got disconnected earlier.",
        "resume_prompt": "Reply CONTINUE to resume or APPLY to start again.",
        "decision_submit": "Submitting your details for a quick eligibility check...",
        "decision_approved": (
            "🎉 You're approved!\n"
            "Amount: ₹{amount:,.2f}\nAPR: {apr:.2f}%\nTenure: up to {term} months\n"
            "Reference: {ref}\nReply ACCEPT to proceed or SUPPORT for help."
        ),
        "decision_rejected": (
            "I'm sorry, we couldn't approve the loan right now because {reason}. "
            "Reply SUPPORT if you'd like to talk to an expert."
        ),
        "fallback_intent": "Please let me know if you want to apply for a loan or need support.",
        "invalid_language": "Please reply with 1 for English or 2 for हिंदी.",
        "invalid_existing_choice": "Please pick an option so I know if you are new or existing.",
        "invalid_intent_choice": "Please choose one of the options so I can guide you.",
        "ask_more_help": "Need anything else right now?",
        "text_only_warning": "I currently support text responses only. Please reply using text.",
    },
    "hi": {
        "welcome": "👋 पेयू फाइनेंस से नमस्ते! मैं आपका पर्सनल लोन सहायक हूँ।",
        "language_prompt": "कृपया अपनी पसंदीदा भाषा चुनें।\n1️⃣ English\n2️⃣ हिंदी (Hindi)",
        "language_option_en": "English",
        "language_option_hi": "हिंदी",
        "existing_probe": "क्या आप पहले से PayU Finance ग्राहक हैं?",
        "existing_yes": "हाँ, मेरे पास PayU का लोन है",
        "existing_no": "नहीं, मैं नया हूँ",
        "intent_prompt_existing": "आज मैं आपकी कैसे मदद कर सकता हूँ?",
        "intent_prompt_new": "धन्यवाद! आप आज क्या करना चाहेंगे?",
        "intent_apply": "लोन के लिए आवेदन करें",
        "intent_support": "सपोर्ट / मदद चाहिए",
        "support_prompt_existing": "कृपया अपने मौजूदा PayU लोन से जुड़ा सवाल लिखें।",
        "support_prompt_new": "मैं मदद के लिए तैयार हूँ! अपना सवाल लिखें या नया आवेदन शुरू करने के लिए APPLY लिखें।",
        "support_handoff": "मैं आपको PayU विशेषज्ञ से जोड़ रहा हूँ ताकि आपको सही मदद मिल सके।",
        "support_closing": "मदद करके खुशी हुई! किसी भी समय SUPPORT लिखें।",
        "support_escalation_ack": "PayU विशेषज्ञ को सूचित कर दिया गया है। जल्द ही आपसे संपर्क किया जाएगा।",
        "onboarding_intro": "बहुत बढ़िया, चलिए आपकी पर्सनल लोन यात्रा शुरू करें। यह 2 मिनट से कम लेता है।",
        "flow_sent": "मैंने एक WhatsApp फॉर्म भेजा है। यदि वह नहीं खुलता, तो यहाँ विवरण लिख दें।",
        "dropoff": "लगता है पिछली बार हमारी बात अधूरी रह गई।",
        "resume_prompt": "जारी रखने के लिए CONTINUE लिखें या दोबारा शुरू करने के लिए APPLY लिखें।",
        "decision_submit": "आपकी जानकारी तेज़ अनुमोदन जांच के लिए भेज रहा हूँ...",
        "decision_approved": (
            "🎉 आपका लोन मंज़ूर हो गया!\n"
            "राशि: ₹{amount:,.2f}\nएपीआर: {apr:.2f}%\nअवधि: अधिकतम {term} महीने\n"
            "संदर्भ: {ref}\nआगे बढ़ने के लिए ACCEPT या मदद के लिए SUPPORT लिखें।"
        ),
        "decision_rejected": (
            "क्षमा करें, हम अभी लोन स्वीकृत नहीं कर सके क्योंकि {reason}। सहायता के लिए SUPPORT लिखें।"
        ),
        "fallback_intent": "कृपया बताएं कि आप लोन के लिए आवेदन करना चाहते हैं या मदद चाहिए।",
        "invalid_language": "कृपया 1 (English) या 2 (हिंदी) लिखें।",
        "invalid_existing_choice": "कृपया बताएँ कि आप नए हैं या मौजूदा ग्राहक।",
        "invalid_intent_choice": "कृपया किसी एक विकल्प का चयन करें।",
        "ask_more_help": "क्या आपको अभी और किसी चीज़ की ज़रूरत है?",
        "text_only_warning": "फिलहाल मैं केवल टेक्स्ट संदेश पढ़ सकता हूँ। कृपया टेक्स्ट में जवाब दें।",
    },
}

LANGUAGE_ALIASES = {
    "english": "en",
    "inglish": "en",
    "en": "en",
    "hindi": "hi",
    "hindee": "hi",
    "hin": "hi",
    "hi": "hi",
    "हिंदी": "hi",
    "1": "en",
    "2": "hi",
}

BOOLEAN_SYNONYMS = {
    True: {"yes", "y", "haan", "haanji", "consent", "agree", "ok", "sure", "accept"},
    False: {"no", "n", "nah", "na", "stop", "reject"},
}

INTENT_KEYWORDS = {
    "apply": {"apply", "loan", "new loan", "finance", "onboarding", "start", "continue"},
    "support": {
        "support",
        "help",
        "emi",
        "statement",
        "status",
        "issue",
        "problem",
        "track",
        "agent",
    },
}

SUPPORT_KB = [
    {
        "q": {
            "en": "How can I pay my EMI?",
            "hi": "मैं EMI कैसे भरूँ?",
        },
        "a": {
            "en": "You can pay your EMI via the PayU Finance app, net banking, or UPI. Reply PAY LINK if you need a payment link.",
            "hi": "आप PayU Finance ऐप, नेट बैंकिंग या UPI से EMI भर सकते हैं। यदि आपको पेमेंट लिंक चाहिए तो PAY LINK लिखें।",
        },
    },
    {
        "q": {
            "en": "How do I check my loan status?",
            "hi": "मैं अपना लोन स्टेटस कैसे देखूँ?",
        },
        "a": {
            "en": "You can track your loan status inside the PayU Finance app under 'My Loans'. I can also connect you to an agent for detailed help.",
            "hi": "आप PayU Finance ऐप में 'My Loans' सेक्शन में अपना स्टेटस देख सकते हैं। मैं आपको एजेंट से भी जोड़ सकता हूँ।",
        },
    },
]

FORM_FIELD_MAP = {
    "full_name": "full_name",
    "pan_name": "full_name",
    "name": "full_name",
    "age": "age",
    "employment_status": "employment_status",
    "income": "monthly_income",
    "monthly_income": "monthly_income",
    "loan_amount": "requested_amount",
    "amount": "requested_amount",
    "purpose": "purpose",
    "consent": "consent_to_credit_check",
}

ONBOARDING_FLOW = [
    {
        "field": "full_name",
        "prompts": {
            "en": "Please share your full name (as per PAN).",
            "hi": "कृपया अपना पूरा नाम (PAN के अनुसार) लिखें।",
        },
        "type": "text",
    },
    {
        "field": "age",
        "prompts": {
            "en": "How old are you?",
            "hi": "आपकी आयु कितनी है?",
        },
        "type": "number",
    },
    {
        "field": "employment_status",
        "prompts": {
            "en": "What best describes your employment status? (Salaried, Self-employed, Student, etc.)",
            "hi": "आपका रोजगार दर्जा क्या है? (नौकरीपेशा, स्वरोज़गार, विद्यार्थी आदि)",
        },
        "type": "text",
    },
    {
        "field": "monthly_income",
        "prompts": {
            "en": "What is your average monthly income in INR?",
            "hi": "आपकी औसत मासिक आय (₹) कितनी है?",
        },
        "type": "currency",
    },
    {
        "field": "requested_amount",
        "prompts": {
            "en": "How much would you like to borrow (₹)?",
            "hi": "आप कितनी राशि उधार लेना चाहते हैं (₹)?",
        },
        "type": "currency",
    },
    {
        "field": "purpose",
        "prompts": {
            "en": "What will you use the funds for?",
            "hi": "आप यह राशि किस काम में उपयोग करेंगे?",
        },
        "type": "text",
    },
    {
        "field": "consent_to_credit_check",
        "prompts": {
            "en": "Do you consent to a credit bureau check? Reply YES to continue.",
            "hi": "क्या आप क्रेडिट ब्यूरो जांच के लिए सहमत हैं? आगे बढ़ने के लिए YES लिखें।",
        },
        "type": "boolean",
    },
]


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def now_ts() -> float:
    return time.time()


def iso_timestamp(ts: Optional[float] = None) -> str:
    value = datetime.fromtimestamp(ts or now_ts(), tz=timezone.utc)
    return value.isoformat()


def minutes_since(ts: float) -> float:
    return (now_ts() - ts) / 60.0


def detect_language_choice(text: str) -> Optional[str]:
    normalized = text.strip().lower()
    if normalized in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[normalized]
    return None


def get_language_pack(language: Optional[str]) -> Dict[str, str]:
    return LANGUAGE_PACKS.get(language or DEFAULT_LANGUAGE, LANGUAGE_PACKS[DEFAULT_LANGUAGE])


def normalize_boolean(value: str) -> Optional[bool]:
    candidate = value.strip().lower()
    for bool_value, synonyms in BOOLEAN_SYNONYMS.items():
        if candidate in synonyms:
            return bool_value
    return None


def parse_numeric(value: str, value_type=float) -> float:
    try:
        cleaned = value.replace(",", "").strip()
        return value_type(cleaned)
    except Exception as exc:
        raise ValueError("Please provide a numeric value.") from exc


def intent_from_text(text: str) -> Optional[str]:
    normalized = text.lower()
    for intent, keywords in INTENT_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return intent
    return None


def infer_existing_user(profile: "UserProfile", text: str) -> Optional[bool]:
    normalized = text.lower()
    if profile.is_existing:
        return True
    if any(keyword in normalized for keyword in {"existing", "current", "emi", "payoff", "statement"}):
        return True
    if any(keyword in normalized for keyword in {"new", "apply", "fresh"}):
        return False
    return None


def get_onboarding_prompt(field: str, language: str) -> str:
    for item in ONBOARDING_FLOW:
        if item["field"] == field:
            return item["prompts"][language]
    raise KeyError(f"Unknown field {field}")


def form_answers_from_message(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    interactive = message.get("interactive")
    if not interactive:
        return None
    nfm_reply = interactive.get("nfm_reply")
    if not nfm_reply:
        return None
    response_json = nfm_reply.get("response_json")
    if not response_json:
        return None
    try:
        payload = json.loads(response_json)
    except json.JSONDecodeError:
        logger.warning("Invalid form response JSON: %s", response_json)
        return None

    mapped: Dict[str, Any] = {}
    for key, value in payload.items():
        target = FORM_FIELD_MAP.get(key)
        if target:
            mapped[target] = value
    return mapped


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


@dataclass
class UserProfile:
    phone: str
    language: str = DEFAULT_LANGUAGE
    is_existing: bool = False
    status: str = "prospect"
    stage: str = "discovery"
    last_activity: float = field(default_factory=now_ts)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def touch(self):
        self.last_activity = now_ts()

    def to_item(self) -> Dict[str, Any]:
        return {
            "phone": self.phone,
            "language": self.language,
            "is_existing": self.is_existing,
            "status": self.status,
            "stage": self.stage,
            "last_activity": Decimal(str(self.last_activity)),
            "metadata": self.metadata,
        }

    @classmethod
    def from_item(cls, item: Dict[str, Any]) -> "UserProfile":
        return cls(
            phone=item["phone"],
            language=item.get("language", DEFAULT_LANGUAGE),
            is_existing=item.get("is_existing", False),
            status=item.get("status", "prospect"),
            stage=item.get("stage", "discovery"),
            last_activity=float(item.get("last_activity", now_ts())),
            metadata=item.get("metadata", {}),
        )


@dataclass
class ConversationState:
    language: Optional[str] = None
    journey: Optional[str] = None
    is_existing: Optional[bool] = None
    step_index: int = 0
    answers: Dict[str, Any] = field(default_factory=dict)
    awaiting_support_details: bool = False
    last_prompt: Optional[str] = None

    def reset(self, keep_language: bool = True):
        lang = self.language if keep_language else None
        self.language = lang
        self.journey = None
        self.is_existing = None
        self.step_index = 0
        self.answers.clear()
        self.awaiting_support_details = False
        self.last_prompt = None


class ConversationStore:
    """In-memory conversation store. Swap with Redis for multi-instance deployments."""

    def __init__(self):
        self._store: Dict[str, ConversationState] = {}

    def get_or_create(self, phone: str) -> ConversationState:
        if phone not in self._store:
            self._store[phone] = ConversationState()
        return self._store[phone]

    def clear(self, phone: str):
        self._store.pop(phone, None)


class UserProfileStore:
    """Persist user profiles to DynamoDB with an in-memory fallback."""

    def __init__(self, table_name: Optional[str], region: str):
        self.table_name = table_name
        self.region = region
        self._table = None
        self._fallback: Dict[str, UserProfile] = {}
        if table_name and boto3:
            resource = boto3.resource("dynamodb", region_name=region)
            self._table = resource.Table(table_name)

    @property
    def uses_dynamo(self) -> bool:
        return self._table is not None

    def get(self, phone: str) -> Optional[UserProfile]:
        if self._table:
            try:
                response = self._table.get_item(Key={"phone": phone})
            except Exception as exc:  # pragma: no cover - network error
                logger.error("Dynamo get_item failed: %s", exc)
                return self._fallback.get(phone)
            item = response.get("Item")
            return UserProfile.from_item(item) if item else None
        return self._fallback.get(phone)

    def save(self, profile: UserProfile) -> None:
        profile.touch()
        if self._table:
            try:
                self._table.put_item(Item=profile.to_item())
                return
            except Exception as exc:  # pragma: no cover - network error
                logger.error("Dynamo put_item failed: %s", exc)
        self._fallback[profile.phone] = profile


conversation_store = ConversationStore()
user_store = UserProfileStore(USER_TABLE_NAME, AWS_REGION)


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

    async def _post(self, payload: Dict[str, Any]) -> None:
        if not self.enabled:
            logger.info("[dry-run] %s", payload)
            return

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(self.base_url, json=payload, headers=headers)
            if response.is_error:
                logger.error(
                    "WhatsApp send failed - %s %s", response.status_code, response.text
                )
                response.raise_for_status()

    async def send_text(self, to: str, body: str) -> None:
        await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body},
            }
        )

    async def send_interactive_buttons(self, to: str, body: str, buttons: List[Tuple[str, str]]):
        action_buttons = [
            {"type": "reply", "reply": {"id": button_id, "title": title[:20]}}
            for button_id, title in buttons[:3]
        ]
        await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body},
                    "action": {"buttons": action_buttons},
                },
            }
        )

    async def send_flow(self, to: str, language: str) -> None:
        if not WHATSAPP_FLOW_ID:
            raise RuntimeError("WhatsApp Flow ID not configured")
        await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "flow",
                    "body": {"text": "PayU Finance Loan Form"},
                    "action": {
                        "flow": {
                            "name": "PayU Personal Loan",
                            "language": {"code": "en_US" if language == "en" else "hi_IN"},
                            "flow_id": WHATSAPP_FLOW_ID,
                            "flow_token": WHATSAPP_FLOW_TOKEN or str(uuid.uuid4()),
                        }
                    },
                },
            }
        )


messenger = MetaWhatsAppClient(META_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID)


# ---------------------------------------------------------------------------
# Backend clients
# ---------------------------------------------------------------------------
class CreditDecisionClient:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = base_url
        self.api_key = api_key

    async def evaluate(self, application: LoanApplication) -> DecisionResult:
        if not self.base_url:
            logger.info(
                "Using offline decision rules for application %s", application.application_id
            )
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
            and application.monthly_income >= 20000
            and debt_to_income <= 8
            and application.consent_to_credit_check
        )
        apr = 16.49 if application.monthly_income >= 60000 else 21.99
        offer_amount = min(application.requested_amount, application.monthly_income * 6)
        reason = None
        if not approved:
            if application.age < 21:
                reason = "minimum age is 21"
            elif application.monthly_income < 20000:
                reason = "monthly income below ₹20,000"
            elif debt_to_income > 8:
                reason = "requested amount exceeds allowed ratio"
            elif not application.consent_to_credit_check:
                reason = "consent to credit check not provided"
        return DecisionResult(
            approved=approved,
            offer_amount=round(offer_amount, 2),
            apr=apr,
            max_term_months=60 if approved else 0,
            reason=reason,
            reference_id=application.application_id,
        )


decision_client = CreditDecisionClient(BACKEND_DECISION_URL, BACKEND_API_KEY)


class SupportAssistant:
    def __init__(self, knowledge_base: List[Dict[str, Dict[str, str]]], threshold: float = 0.55):
        self.knowledge_base = knowledge_base
        self.threshold = threshold

    async def answer(self, question: str, language: str) -> Tuple[Optional[str], float]:
        normalized = question.strip().lower()
        best_score = 0.0
        best_answer: Optional[str] = None
        for entry in self.knowledge_base:
            prompt = entry["q"].get(language) or entry["q"]["en"]
            score = similarity_score(normalized, prompt.lower())
            if score > best_score:
                best_score = score
                best_answer = entry["a"].get(language) or entry["a"]["en"]
        return best_answer, best_score


def similarity_score(a: str, b: str) -> float:
    # Simple token overlap score
    set_a = set(a.split())
    set_b = set(b.split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / float(len(set_a | set_b))


support_agent = SupportAssistant(SUPPORT_KB)


# ---------------------------------------------------------------------------
# Chatbot orchestration
# ---------------------------------------------------------------------------
async def prompt_language(phone: str) -> None:
    pack = get_language_pack("en")
    await messenger.send_text(phone, pack["welcome"])
    await messenger.send_interactive_buttons(
        phone,
        pack["language_prompt"],
        [
            ("lang_en", pack["language_option_en"]),
            ("lang_hi", pack["language_option_hi"]),
        ],
    )


async def prompt_existing(phone: str, language: str) -> None:
    pack = get_language_pack(language)
    await messenger.send_interactive_buttons(
        phone,
        pack["existing_probe"],
        [
            ("existing_yes", pack["existing_yes"]),
            ("existing_no", pack["existing_no"]),
        ],
    )


async def prompt_intent(phone: str, language: str, is_existing: bool) -> None:
    pack = get_language_pack(language)
    prompt_key = "intent_prompt_existing" if is_existing else "intent_prompt_new"
    await messenger.send_interactive_buttons(
        phone,
        pack[prompt_key],
        [
            ("intent_apply", pack["intent_apply"]),
            ("intent_support", pack["intent_support"]),
        ],
    )


async def start_onboarding(phone: str, state: ConversationState, language: str) -> None:
    state.journey = "onboarding"
    state.step_index = 0
    state.answers.clear()
    pack = get_language_pack(language)
    await messenger.send_text(phone, pack["onboarding_intro"])
    if WHATSAPP_FLOW_ID:
        try:
            await messenger.send_flow(phone, language)
            await messenger.send_text(phone, pack["flow_sent"])
        except Exception as exc:  # pragma: no cover - flow failures
            logger.warning("Failed to send WhatsApp flow: %s", exc)
    prompt = get_onboarding_prompt(ONBOARDING_FLOW[0]["field"], language)
    await messenger.send_text(phone, prompt)


def validate_onboarding_answer(field: str, raw_value: Any) -> Any:
    if field == "age":
        age = int(parse_numeric(str(raw_value), int))
        if age < 18 or age > 75:
            raise ValueError("Age must be between 18 and 75.")
        return age
    if field in {"monthly_income", "requested_amount"}:
        amount = float(parse_numeric(str(raw_value), float))
        if amount <= 0:
            raise ValueError("Amount must be greater than zero.")
        return round(amount, 2)
    if field == "consent_to_credit_check":
        consent = normalize_boolean(str(raw_value))
        if consent is None:
            raise ValueError("Please reply YES or NO.")
        if not consent:
            raise ValueError("Consent is required to continue.")
        return consent
    return str(raw_value).strip()


async def handle_onboarding_step(
    phone: str,
    text: str,
    state: ConversationState,
    language: str,
    profile: UserProfile,
) -> None:
    if state.step_index >= len(ONBOARDING_FLOW):
        await finalize_onboarding(phone, state, language, profile)
        return
    field_name = ONBOARDING_FLOW[state.step_index]["field"]
    try:
        processed = validate_onboarding_answer(field_name, text)
    except ValueError as exc:
        await messenger.send_text(phone, str(exc))
        return

    state.answers[field_name] = processed
    state.step_index += 1
    if state.step_index >= len(ONBOARDING_FLOW):
        await finalize_onboarding(phone, state, language, profile)
    else:
        next_field = ONBOARDING_FLOW[state.step_index]["field"]
        await messenger.send_text(phone, get_onboarding_prompt(next_field, language))


async def handle_form_submission(
    phone: str,
    form_answers: Dict[str, Any],
    state: ConversationState,
    language: str,
    profile: UserProfile,
) -> None:
    for field, raw_value in form_answers.items():
        if field not in {item["field"] for item in ONBOARDING_FLOW}:
            continue
        try:
            state.answers[field] = validate_onboarding_answer(field, raw_value)
        except ValueError as exc:
            await messenger.send_text(phone, str(exc))
    completed_fields = [item["field"] for item in ONBOARDING_FLOW]
    state.step_index = sum(1 for field in completed_fields if field in state.answers)
    if all(field in state.answers for field in completed_fields):
        await finalize_onboarding(phone, state, language, profile)
    else:
        next_field = ONBOARDING_FLOW[state.step_index]["field"]
        await messenger.send_text(phone, get_onboarding_prompt(next_field, language))


async def finalize_onboarding(
    phone: str,
    state: ConversationState,
    language: str,
    profile: UserProfile,
) -> None:
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
        await messenger.send_text(phone, "Let's collect that information again. Type APPLY to restart.")
        state.reset(keep_language=True)
        return

    pack = get_language_pack(language)
    await messenger.send_text(phone, pack["decision_submit"])
    decision = await decision_client.evaluate(application)

    profile.is_existing = True
    profile.stage = "borrower" if decision.approved else "prospect"
    profile.status = "approved" if decision.approved else "declined"
    profile.metadata["last_application_id"] = decision.reference_id
    user_store.save(profile)

    if decision.approved:
        message = pack["decision_approved"].format(
            amount=decision.offer_amount,
            apr=decision.apr,
            term=decision.max_term_months,
            ref=decision.reference_id,
        )
    else:
        message = pack["decision_rejected"].format(reason=decision.reason or "of internal policies")
    await messenger.send_text(phone, message)
    await messenger.send_text(phone, pack["ask_more_help"])
    state.reset(keep_language=True)


async def handle_support(
    phone: str,
    text: str,
    state: ConversationState,
    language: str,
    profile: UserProfile,
) -> None:
    answer, confidence = await support_agent.answer(text, language)
    pack = get_language_pack(language)
    if not answer or confidence < support_agent.threshold:
        await messenger.send_text(phone, pack["support_handoff"])
        await escalate_to_agent(phone, text, profile)
        await messenger.send_text(phone, pack["support_escalation_ack"])
        state.reset(keep_language=True)
        return

    await messenger.send_text(phone, answer)
    await messenger.send_text(phone, pack["support_closing"])
    profile.metadata["last_support_query"] = text
    user_store.save(profile)
    state.reset(keep_language=True)


async def escalate_to_agent(phone: str, question: str, profile: UserProfile) -> None:
    logger.info(
        "Escalating to human agent: phone=%s question=%s queue=%s",
        phone,
        question,
        HUMAN_HANDOFF_QUEUE,
    )
    profile.metadata["last_escalation"] = {
        "question": question,
        "timestamp": iso_timestamp(),
        "queue": HUMAN_HANDOFF_QUEUE,
    }
    user_store.save(profile)


async def send_dropoff_message(phone: str, language: str) -> None:
    pack = get_language_pack(language)
    await messenger.send_text(phone, pack["dropoff"])
    await messenger.send_text(phone, pack["resume_prompt"])


# ---------------------------------------------------------------------------
# Message ingestion
# ---------------------------------------------------------------------------
def extract_message_text(message: Dict[str, Any]) -> Optional[str]:
    if "text" in message and message["text"].get("body"):
        return message["text"]["body"]
    if "button" in message:
        return message["button"].get("text")
    interactive = message.get("interactive")
    if interactive:
        if interactive.get("type") == "button_reply":
            return interactive["button_reply"].get("title")
        if interactive.get("type") == "list_reply":
            return interactive["list_reply"].get("title")
    return None


async def handle_incoming_message(message: Dict[str, Any]) -> None:
    phone = message.get("from")
    if not phone:
        return

    state = conversation_store.get_or_create(phone)
    profile = user_store.get(phone) or UserProfile(phone=phone)
    profile.touch()
    user_store.save(profile)

    language = state.language or profile.language or DEFAULT_LANGUAGE
    pack = get_language_pack(language)

    form_answers = form_answers_from_message(message)
    text = extract_message_text(message)

    if form_answers:
        state.language = language
        state.journey = state.journey or "onboarding"
        await handle_form_submission(phone, form_answers, state, language, profile)
        return

    if not text:
        await messenger.send_text(phone, pack["text_only_warning"])
        return

    normalized = text.strip().lower()

    if state.language is None:
        lang_choice = detect_language_choice(normalized)
        if lang_choice is None:
            await prompt_language(phone)
            return
        state.language = lang_choice
        profile.language = lang_choice
        user_store.save(profile)
        await prompt_existing(phone, lang_choice)
        return

    language = state.language
    pack = get_language_pack(language)

    if minutes_since(profile.last_activity) > INACTIVITY_MINUTES:
        await send_dropoff_message(phone, language)
        state.journey = None
        state.step_index = 0
        state.answers.clear()

    if state.is_existing is None:
        inferred = infer_existing_user(profile, normalized)
        if inferred is not None:
            state.is_existing = inferred
        else:
            await prompt_existing(phone, language)
            return

    if normalized in {"existing", "old", "current"}:
        state.is_existing = True
    if normalized in {"new", "fresh"}:
        state.is_existing = False

    if state.journey is None:
        intent = intent_from_text(normalized)
        if intent == "apply":
            await start_onboarding(phone, state, language)
            return
        if intent == "support":
            state.journey = "support"
            await messenger.send_text(
                phone,
                pack["support_prompt_existing" if state.is_existing else "support_prompt_new"],
            )
            return
        await prompt_intent(phone, language, state.is_existing or False)
        return

    if normalized in {"apply", "loan"}:
        await start_onboarding(phone, state, language)
        return
    if normalized in {"support", "help"}:
        state.journey = "support"
        await messenger.send_text(
            phone,
            pack["support_prompt_existing" if state.is_existing else "support_prompt_new"],
        )
        return

    if state.journey == "onboarding":
        await handle_onboarding_step(phone, text, state, language, profile)
    elif state.journey == "support":
        await handle_support(phone, text, state, language, profile)


# ---------------------------------------------------------------------------
# FastAPI endpoints
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
async def receive_webhook(payload: Dict[str, Any]):
    messages = extract_messages(payload)
    if not messages:
        return JSONResponse({"status": "ignored"})
    await asyncio.gather(*(handle_incoming_message(msg) for msg in messages))
    return JSONResponse({"status": "processed"})


def extract_messages(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
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


@app.get("/healthz")
async def healthcheck():
    return {
        "status": "ok",
        "messenger_enabled": messenger.enabled,
        "decision_backend": bool(BACKEND_DECISION_URL),
        "dynamo_enabled": user_store.uses_dynamo,
    }


def run():
    """Allow `python finhackers.py` to launch a development server."""
    import uvicorn

    uvicorn.run(
        "finhackers:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=bool(int(os.environ.get("RELOAD", "0"))),
    )


def lambda_handler(event, context):
    if not _lambda_adapter:
        raise RuntimeError("Mangum is not installed. Cannot handle Lambda events.")
    return _lambda_adapter(event, context)


if __name__ == "__main__":
    run()
