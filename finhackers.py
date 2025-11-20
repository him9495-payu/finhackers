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
import random

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
INTERACTION_TABLE_NAME = os.getenv("INTERACTION_TABLE_NAME")
LOAN_TABLE_NAME = os.getenv("LOAN_TABLE_NAME")
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
        "intent_prompt_existing": "What would you like to do today?",
        "intent_prompt_new": "Welcome! What would you like to do today?",
        "intent_apply": "Apply for a loan",
        "intent_support": "Get support",
        "support_prompt_existing": "Tell me what kind of help you need.",
        "support_prompt_new": "Need help before applying? Let me know.",
        "support_menu_intro": "Pick a support topic:",
        "support_menu_intro_secondary": "More help options:",
        "support_btn_payment": "Pay EMI",
        "support_btn_status": "Loan status",
        "support_btn_docs": "Documents",
        "support_btn_repayment": "Change EMI",
        "support_btn_agent": "Talk to agent",
        "support_text_hint": "Need something else? Type your question.",
        "support_handoff": "I'll connect you with a PayU expert so you don't have to wait.",
        "support_closing": "Glad to help! Tap Support anytime if you need anything else.",
        "support_escalation_ack": "A PayU specialist has been notified. You will hear from us shortly.",
        "onboarding_intro": "Great! I just shared a secure WhatsApp form so you can submit your details.",
        "flow_sent": "Tap the form button to continue. If it closes, you can reopen it below.",
        "flow_button_label": "Open form",
        "support_button_label": "Support",
        "dropoff": "It looks like we got disconnected earlier.",
        "resume_prompt": "Tap Apply to continue your loan or Support if you need help.",
        "decision_submit": "Submitting your details for a quick eligibility check...",
        "decision_approved": (
            "🎉 You're approved!\n"
            "Amount: ₹{amount:,.2f}\nAPR: {apr:.2f}%\nTenure: up to {term} months\n"
            "Reference: {ref}"
        ),
        "decision_rejected": (
            "I'm sorry, we couldn't approve the loan right now because {reason}. "
            "Tap Support if you'd like to talk to an expert."
        ),
        "post_accept_label": "Accept offer",
        "post_support_label": "Need support",
        "accept_ack": "Great! A PayU specialist will share the loan documents shortly.",
        "fallback_intent": "Please let me know if you want to apply for a loan or need support.",
        "invalid_language": "Please tap English or हिंदी.",
        "invalid_intent_choice": "Please pick one of the options so I can guide you.",
        "ask_more_help": "Need anything else right now?",
        "text_only_warning": "I currently support text responses only. Please reply using text.",
    },
    "hi": {
        "welcome": "👋 पेयू फाइनेंस से नमस्ते! मैं आपका पर्सनल लोन सहायक हूँ।",
        "language_prompt": "कृपया अपनी भाषा चुनें।\n1️⃣ English\n2️⃣ हिंदी (Hindi)",
        "language_option_en": "English",
        "language_option_hi": "हिंदी",
        "intent_prompt_existing": "आज आप क्या करना चाहेंगे?",
        "intent_prompt_new": "स्वागत है! आप आज क्या करना चाहेंगे?",
        "intent_apply": "लोन के लिए आवेदन",
        "intent_support": "सपोर्ट / मदद",
        "support_prompt_existing": "कृपया बताएँ आपको किस तरह की मदद चाहिए।",
        "support_prompt_new": "आवेदन से पहले कोई सवाल है? मुझे बताएँ।",
        "support_menu_intro": "किस विषय में मदद चाहिए?",
        "support_menu_intro_secondary": "अन्य सहायता विकल्प:",
        "support_btn_payment": "EMI जमा",
        "support_btn_status": "लोन स्टेटस",
        "support_btn_docs": "डॉक्यूमेंट्स",
        "support_btn_repayment": "EMI बदलें",
        "support_btn_agent": "एजेंट से बात",
        "support_text_hint": "कुछ और चाहिए? अपना सवाल लिखें।",
        "support_handoff": "मैं आपको PayU विशेषज्ञ से जोड़ रहा हूँ ताकि आपको सही मदद मिल सके।",
        "support_closing": "मदद करके खुशी हुई! ज़रूरत हो तो सपोर्ट दबाएँ।",
        "support_escalation_ack": "PayU विशेषज्ञ को सूचित कर दिया गया है। जल्द ही आपसे संपर्क होगा।",
        "onboarding_intro": "बहुत बढ़िया! मैंने अभी एक सुरक्षित WhatsApp फॉर्म भेजा है, कृपया उसे भरें।",
        "flow_sent": "फॉर्म खोलने के लिए नीचे बटन दबाएँ। बंद होने पर भी यहाँ से दोबारा खोल सकते हैं।",
        "flow_button_label": "फॉर्म खोलें",
        "support_button_label": "सपोर्ट",
        "dropoff": "लगता है पिछली बार बात अधूरी रह गई।",
        "resume_prompt": "आगे बढ़ने के लिए APPLY दबाएँ या मदद के लिए SUPPORT दबाएँ।",
        "decision_submit": "आपकी जानकारी तेज़ अनुमोदन जांच के लिए भेज रहा हूँ...",
        "decision_approved": (
            "🎉 आपका लोन मंज़ूर हो गया!\n"
            "राशि: ₹{amount:,.2f}\nएपीआर: {apr:.2f}%\nअवधि: अधिकतम {term} महीने\n"
            "संदर्भ: {ref}"
        ),
        "decision_rejected": (
            "क्षमा करें, हम अभी लोन स्वीकृत नहीं कर सके क्योंकि {reason}। सहायता के लिए सपोर्ट दबाएँ।"
        ),
        "post_accept_label": "ऑफ़र स्वीकारें",
        "post_support_label": "सपोर्ट चाहिए",
        "accept_ack": "बहुत बढ़िया! PayU विशेषज्ञ जल्द ही दस्तावेज़ साझा करेंगे।",
        "fallback_intent": "कृपया बताएँ कि आप लोन लेना चाहते हैं या मदद चाहिए।",
        "invalid_language": "कृपया English या हिंदी चुनें।",
        "invalid_intent_choice": "कृपया उपलब्ध विकल्पों में से किसी एक को चुनें।",
        "ask_more_help": "क्या आपको और किसी चीज़ की ज़रूरत है?",
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
    "post_disbursal": {
        "balance",
        "emi",
        "statement",
        "loan status",
        "loan details",
        "loan doc",
        "document",
        "repayment",
        "pay",
        "disbursal",
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
    {
        "q": {
            "en": "How do I download my loan statement?",
            "hi": "मैं अपना लोन स्टेटमेंट कैसे डाउनलोड करूँ?",
        },
        "a": {
            "en": "You can download loan statements and documents inside the PayU Finance app under My Loans > Documents. I can also email them to you if needed.",
            "hi": "आप PayU Finance ऐप में My Loans > Documents सेक्शन से स्टेटमेंट और डॉक्युमेंट्स डाउनलोड कर सकते हैं। आवश्यकता हो तो मैं इन्हें ईमेल भी कर सकता हूँ।",
        },
    },
    {
        "q": {
            "en": "Can I change my repayment date?",
            "hi": "क्या मैं अपनी EMI तारीख बदल सकता हूँ?",
        },
        "a": {
            "en": "Repayment dates can be changed once every 6 months via the app. Go to My Loans > Repayment Options or request a PayU specialist to assist.",
            "hi": "आप EMI तारीख को हर 6 महीने में एक बार बदल सकते हैं। PayU Finance ऐप में My Loans > Repayment Options पर जाएँ या विशेषज्ञ से मदद लें।",
        },
    },
]

SUPPORT_SHORTCUTS = {
    "support_payment": 0,
    "support_status": 1,
    "support_docs": 2,
    "support_repayment_change": 3,
}

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
    language: Optional[str] = None
    is_existing: bool = False
    status: str = "prospect"
    stage: str = "discovery"
    last_activity: float = field(default_factory=now_ts)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: iso_timestamp())
    updated_at: str = field(default_factory=lambda: iso_timestamp())

    def touch(self):
        self.last_activity = now_ts()
        self.updated_at = iso_timestamp()

    def to_item(self) -> Dict[str, Any]:
        return {
            "phone": self.phone,
            "language": self.language,
            "is_existing": self.is_existing,
            "status": self.status,
            "stage": self.stage,
            "last_activity": Decimal(str(self.last_activity)),
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_item(cls, item: Dict[str, Any]) -> "UserProfile":
        return cls(
            phone=item["phone"],
            language=item.get("language"),
            is_existing=item.get("is_existing", False),
            status=item.get("status", "prospect"),
            stage=item.get("stage", "discovery"),
            last_activity=float(item.get("last_activity", now_ts())),
            metadata=item.get("metadata", {}),
            created_at=item.get("created_at", iso_timestamp()),
            updated_at=item.get("updated_at", iso_timestamp()),
        )


@dataclass
class ConversationState:
    language: Optional[str] = None
    journey: Optional[str] = None
    is_existing: Optional[bool] = None
    answers: Dict[str, Any] = field(default_factory=dict)
    awaiting_support_details: bool = False
    awaiting_flow_completion: bool = False

    def reset(self, keep_language: bool = True):
        lang = self.language if keep_language else None
        self.language = lang
        self.journey = None
        self.is_existing = None
        self.answers.clear()
        self.awaiting_support_details = False
        self.awaiting_flow_completion = False


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


class LoanRecordStore:
    """Stores disbursal-level data for post-loan servicing."""

    def __init__(self, table_name: Optional[str], region: str):
        self.table_name = table_name
        self.region = region
        self._table = None
        self._fallback: Dict[str, Dict[str, Any]] = {}
        if table_name and boto3:
            resource = boto3.resource("dynamodb", region_name=region)
            self._table = resource.Table(table_name)

    def upsert_from_decision(
        self,
        phone: str,
        decision: DecisionResult,
        application: LoanApplication,
    ) -> None:
        existing = self.get_record(phone)
        created_at = existing.get("created_at") if existing else iso_timestamp()
        emi_schedule = existing.get("emi_schedule", []) if existing else []
        now_iso = iso_timestamp()
        record = {
            "phone": phone,
            "reference_id": decision.reference_id,
            "offer_amount": decision.offer_amount,
            "apr": decision.apr,
            "max_term_months": decision.max_term_months,
            "status": "approved" if decision.approved else "declined",
            "purpose": application.purpose,
            "requested_amount": application.requested_amount,
            "monthly_income": application.monthly_income,
            "employment_status": application.employment_status,
            "created_at": created_at,
            "updated_at": now_iso,
            "next_emi_due": application.monthly_income * 0.4 if decision.approved else None,
            "documents_url": None,
            "emi_schedule": emi_schedule,
        }
        if not decision.approved:
            record["reason"] = decision.reason
        self._write_record(record)

    def _write_record(self, record: Dict[str, Any]) -> None:
        if self._table:
            try:
                self._table.put_item(Item=record)
                return
            except Exception as exc:  # pragma: no cover
                logger.error("Dynamo loan_record put_item failed: %s", exc)
        self._fallback[record["phone"]] = record

    def get_record(self, phone: str) -> Optional[Dict[str, Any]]:
        if self._table:
            try:
                response = self._table.get_item(Key={"phone": phone})
                item = response.get("Item")
                if item:
                    return item
            except Exception as exc:  # pragma: no cover
                logger.error("Dynamo loan_record get_item failed: %s", exc)
        return self._fallback.get(phone)



class InteractionStore:
    """Persist every inbound/outbound interaction for auditing and analytics."""

    def __init__(self, table_name: Optional[str], region: str):
        self.table_name = table_name
        self.region = region
        self._table = None
        self._fallback: List[Dict[str, Any]] = []
        if table_name and boto3:
            resource = boto3.resource("dynamodb", region_name=region)
            self._table = resource.Table(table_name)

    def put(self, phone: str, direction: str, category: str, payload: Dict[str, Any]) -> None:
        timestamp = iso_timestamp()
        item = {
            "phone": phone,
            "timestamp": timestamp,
            "direction": direction,
            "category": category,
            "payload": payload,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        if self._table:
            try:
                self._table.put_item(Item=item)
                return
            except Exception as exc:  # pragma: no cover - network errors
                logger.error("Dynamo interaction put_item failed: %s", exc)
        self._fallback.append(item)


conversation_store = ConversationStore()
user_store = UserProfileStore(USER_TABLE_NAME, AWS_REGION)
interaction_store = InteractionStore(INTERACTION_TABLE_NAME, AWS_REGION)
loan_store = LoanRecordStore(LOAN_TABLE_NAME, AWS_REGION)


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
        # Synthetic MVP logic: derive pseudo credit indicators from applicant info.
        random.seed(application.application_id)
        credit_score = max(
            520,
            min(
                850,
                random.randint(600, 780)
                + int(application.monthly_income / 10000) * 5
                - int(application.requested_amount / 50000) * 5,
            ),
        )
        utilization_ratio = round(random.uniform(0.2, 0.85), 2)
        fraud_signal = random.random() < 0.05
        serviceability = application.monthly_income - (application.requested_amount / max(12, application.monthly_income / 1000))

        debt_to_income = application.requested_amount / max(application.monthly_income, 1)
        approved = (
            application.age >= 21
            and application.age <= 70
            and application.monthly_income >= 15000
            and debt_to_income <= 10
            and utilization_ratio <= 0.7
            and credit_score >= 640
            and serviceability > application.requested_amount * 0.01
            and not fraud_signal
            and application.consent_to_credit_check
        )

        apr = 18.99 - min(5, (credit_score - 640) / 50)
        apr = round(max(12.49, apr), 2)
        offer_amount = min(application.requested_amount, application.monthly_income * 6)

        reason = None
        if not approved:
            if not application.consent_to_credit_check:
                reason = "Consent to credit check not provided."
            elif fraud_signal:
                reason = "Automated checks flagged an inconsistency. Please review."
            elif application.age < 21 or application.age > 70:
                reason = "Applicant must be between 21 and 70 years old."
            elif application.monthly_income < 15000:
                reason = "Monthly income below ₹15,000 threshold."
            elif credit_score < 640:
                reason = f"Internal score {credit_score} below minimum requirement."
            elif utilization_ratio > 0.7:
                reason = "Utilization ratio too high."
            elif serviceability <= application.requested_amount * 0.01:
                reason = "Repayment capacity insufficient."
            else:
                reason = "Request exceeds permitted debt-to-income ratio."
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

    def compose_context(self, language: str) -> str:
        sections = []
        for entry in self.knowledge_base:
            question = entry["q"].get(language) or entry["q"]["en"]
            answer = entry["a"].get(language) or entry["a"]["en"]
            sections.append(f"Q: {question}\nA: {answer}")
        return "\n\n".join(sections)


class BedrockSupportResponder:
    def __init__(self, model_id: Optional[str], region: str):
        self.model_id = model_id
        self.region = region
        self._client = None
        if model_id and boto3:
            try:
                self._client = boto3.client("bedrock-runtime", region_name=region)
            except Exception as exc:  # pragma: no cover - network errors
                logger.error("Failed to initialize Bedrock client: %s", exc)
                self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _build_prompt(self, question: str, language: str, context: str) -> str:
        language_name = "English" if language == "en" else "Hindi"
        instructions = (
            "You are PayU Finance's bilingual support copilot. "
            "Answer clearly and concisely using the provided knowledge base. "
            f"Respond in {language_name}. "
            "If the answer is missing, acknowledge lack of information and suggest connecting with a PayU agent."
        )
        return f"{instructions}\n\nKnowledge Base:\n{context}\n\nCustomer question:\n{question}\n\nAnswer:"

    def _invoke(self, body: str):
        return self._client.invoke_model(
            modelId=self.model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )

    async def answer(self, question: str, language: str, context: str) -> Optional[str]:
        if not self.enabled:
            return None

        prompt = self._build_prompt(question, language, context)
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "max_tokens": 400,
            "temperature": 0.3,
        }
        try:
            response = await asyncio.to_thread(self._invoke, json.dumps(payload))
            raw_body = response["body"].read()
            data = json.loads(raw_body.decode("utf-8"))
            if "output" in data:
                # Some models return `output` with `text`
                content = data["output"][0].get("content", [{}])
                return content[0].get("text")
            if "content" in data:
                # Anthropic-compatible structure
                content = data["content"]
                if content and "text" in content[0]:
                    return content[0]["text"]
            if "results" in data:
                return data["results"][0]["outputText"]
        except Exception as exc:
            logger.error("Bedrock response failed: %s", exc)
        return None

    async def classify(self, question: str) -> Optional[str]:
        if not self.enabled:
            return None
        instructions = (
            "Classify the following post-disbursal customer message as one of "
            "Query (informational question), Request (asks for an action such as sending documents), "
            "or Complaint (expresses dissatisfaction). Respond with only one word: Query, Request, or Complaint."
        )
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": f"{instructions}\n\nMessage:\n{question}"}],
                }
            ],
            "max_tokens": 50,
            "temperature": 0,
        }
        try:
            response = await asyncio.to_thread(self._invoke, json.dumps(payload))
            raw_body = response["body"].read()
            data = json.loads(raw_body.decode("utf-8"))
            text = None
            if "output" in data:
                text = data["output"][0].get("content", [{}])[0].get("text")
            elif "content" in data:
                content = data["content"]
                if content and "text" in content[0]:
                    text = content[0]["text"]
            elif "results" in data:
                text = data["results"][0]["outputText"]
            if text:
                normalized = text.strip().lower()
                if "complaint" in normalized:
                    return "Complaint"
                if "request" in normalized:
                    return "Request"
                if "query" in normalized or "question" in normalized:
                    return "Query"
        except Exception as exc:
            logger.error("Bedrock classification failed: %s", exc)
        return None


def similarity_score(a: str, b: str) -> float:
    # Simple token overlap score
    set_a = set(a.split())
    set_b = set(b.split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / float(len(set_a | set_b))


support_agent = SupportAssistant(SUPPORT_KB)
bedrock_responder = BedrockSupportResponder(BEDROCK_MODEL_ID, AWS_REGION)


async def classify_post_disbursal_category(question: str) -> str:
    if not bedrock_responder.enabled:
        return "Query"
    label = await bedrock_responder.classify(question)
    return label or "Query"


# ---------------------------------------------------------------------------
# Chatbot orchestration
# ---------------------------------------------------------------------------
async def prompt_language(phone: str) -> None:
    english_pack = get_language_pack("en")
    hindi_pack = get_language_pack("hi")
    await messenger.send_text(phone, english_pack["welcome"])
    await messenger.send_text(phone, hindi_pack["welcome"])
    await messenger.send_interactive_buttons(
        phone,
        english_pack["language_prompt"],
        [
            ("lang_en", english_pack["language_option_en"]),
            ("lang_hi", english_pack["language_option_hi"]),
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
    record_interaction(
        phone,
        "outbound",
        "intent_prompt",
        {"language": language, "is_existing": is_existing},
    )


async def start_onboarding(phone: str, state: ConversationState, language: str) -> None:
    state.journey = "onboarding"
    state.answers.clear()
    state.awaiting_flow_completion = True
    pack = get_language_pack(language)
    await messenger.send_text(phone, pack["onboarding_intro"])
    await prompt_loan_flow(phone, language)
    record_interaction(
        phone,
        "system",
        "start_onboarding",
        {"language": language, "known_profile": bool(state.is_existing)},
    )


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


async def handle_form_submission(
    phone: str,
    form_answers: Dict[str, Any],
    state: ConversationState,
    language: str,
    profile: UserProfile,
) -> None:
    for field_name, raw_value in form_answers.items():
        if field_name not in {item["field"] for item in ONBOARDING_FLOW}:
            continue
        try:
            state.answers[field_name] = validate_onboarding_answer(field_name, raw_value)
        except ValueError as exc:
            await messenger.send_text(phone, str(exc))
    required_fields = [item["field"] for item in ONBOARDING_FLOW]
    if all(field in state.answers for field in required_fields):
        await finalize_onboarding(phone, state, language, profile)
        return

    missing = ", ".join(field for field in required_fields if field not in state.answers)
    logger.info("Form submission missing fields [%s] for %s", missing, phone)
    await messenger.send_text(phone, "It looks like we still need a few details. Please reopen the form.")
    await prompt_loan_flow(phone, language)
    record_interaction(
        phone,
        "system",
        "incomplete_flow_submission",
        {"missing_fields": missing.split(", ") if missing else []},
    )


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
        await messenger.send_text(phone, "Let's collect that information again. Tap Apply to restart the loan journey.")
        state.reset(keep_language=True)
        return

    record_interaction(
        phone,
        "system",
        "loan_application",
        application.dict(),
    )

    pack = get_language_pack(language)
    await messenger.send_text(phone, pack["decision_submit"])
    decision = await decision_client.evaluate(application)

    profile.is_existing = True
    profile.stage = "borrower" if decision.approved else "prospect"
    profile.status = "approved" if decision.approved else "declined"
    profile.metadata["last_application_id"] = decision.reference_id
    user_store.save(profile)
    loan_store.upsert_from_decision(profile.phone, decision, application)

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
    record_interaction(
        phone,
        "outbound",
        "loan_decision",
        {
            "approved": decision.approved,
            "offer_amount": decision.offer_amount,
            "apr": decision.apr,
            "max_term_months": decision.max_term_months,
            "reference_id": decision.reference_id,
            "reason": decision.reason,
        },
    )
    await send_post_decision_options(phone, language)
    state.awaiting_flow_completion = False
    state.reset(keep_language=True)


async def handle_support(
    phone: str,
    text: str,
    state: ConversationState,
    language: str,
    profile: UserProfile,
) -> None:
    pack = get_language_pack(language)
    context = support_agent.compose_context(language)
    loan_context = loan_store.get_record(phone)
    combined_context = context
    if loan_context:
        loan_snippet = (
            f"\n\nLoan details:\n"
            f"- Reference ID: {loan_context.get('reference_id')}\n"
            f"- Status: {loan_context.get('status')}\n"
            f"- Amount: ₹{loan_context.get('offer_amount')}\n"
            f"- APR: {loan_context.get('apr')}%\n"
            f"- Tenure: {loan_context.get('max_term_months')} months\n"
            f"- Next EMI: ₹{loan_context.get('next_emi_due')}\n"
        )
        combined_context = f"{context}{loan_snippet}"
    bedrock_answer = await bedrock_responder.answer(text, language, combined_context)
    if bedrock_answer:
        await messenger.send_text(phone, bedrock_answer)
        await messenger.send_text(phone, pack["support_closing"])
        profile.metadata["last_support_query"] = text
        user_store.save(profile)
        record_interaction(
            phone,
            "outbound",
            "support_answer",
            {"source": "bedrock", "question": text},
        )
        state.awaiting_support_details = False
        state.reset(keep_language=True)
        return

    answer, confidence = await support_agent.answer(text, language)
    if not answer or confidence < support_agent.threshold:
        await messenger.send_text(phone, pack["support_handoff"])
        await escalate_to_agent(phone, text, profile)
        await messenger.send_text(phone, pack["support_escalation_ack"])
        record_interaction(
            phone,
            "system",
            "support_escalation",
            {"reason": "low_confidence", "question": text},
        )
        state.awaiting_support_details = False
        state.reset(keep_language=True)
        return

    await messenger.send_text(phone, answer)
    await messenger.send_text(phone, pack["support_closing"])
    profile.metadata["last_support_query"] = text
    user_store.save(profile)
    record_interaction(
        phone,
        "outbound",
        "support_answer",
        {"source": "kb", "question": text, "confidence": confidence},
    )
    state.awaiting_support_details = False
    state.reset(keep_language=True)


async def handle_support_shortcut(
    phone: str,
    language: str,
    profile: UserProfile,
    shortcut_id: int,
) -> None:
    if shortcut_id >= len(SUPPORT_KB):
        return
    entry = SUPPORT_KB[shortcut_id]
    pack = get_language_pack(language)
    answer = entry["a"].get(language) or entry["a"]["en"]
    await messenger.send_text(phone, answer)
    await messenger.send_text(phone, pack["support_closing"])
    profile.metadata["last_support_query"] = entry["q"].get(language) or entry["q"]["en"]
    user_store.save(profile)
    record_interaction(
        phone,
        "outbound",
        "support_answer",
        {"source": "button_shortcut", "shortcut_id": shortcut_id},
    )


async def handle_post_disbursal(phone: str, language: str, normalized_query: str) -> None:
    record = loan_store.get_record(phone)
    pack = get_language_pack(language)
    if not record:
        await messenger.send_text(phone, pack["support_handoff"])
        await escalate_to_agent(phone, "No loan record found", user_store.get(phone))
        return

    payload = {
        "reference_id": record.get("reference_id"),
        "offer_amount": record.get("offer_amount"),
        "apr": record.get("apr"),
        "max_term_months": record.get("max_term_months"),
        "next_emi_due": record.get("next_emi_due"),
        "status": record.get("status"),
        "documents_url": record.get("documents_url"),
    }
    category_label = await classify_post_disbursal_category(normalized_query)
    record_interaction(
        phone,
        "system",
        "post_disbursal_query",
        {
            "query": normalized_query,
            "loan_reference": payload["reference_id"],
            "classification": category_label,
        },
    )

    if "balance" in normalized_query or "emi" in normalized_query:
        response = (
            f"Loan reference {payload['reference_id']} is currently {payload['status']}. "
            f"Outstanding amount is approx ₹{payload['offer_amount']:.2f} with APR {payload['apr']}% "
            f"for up to {payload['max_term_months']} months. "
            f"Your next EMI is around ₹{payload['next_emi_due']:.2f}."
        )
    elif "status" in normalized_query or "loan details" in normalized_query:
        response = (
            f"Loan reference {payload['reference_id']} is {payload['status']}. "
            f"Approved amount ₹{payload['offer_amount']:.2f} with APR {payload['apr']}% "
            f"over {payload['max_term_months']} months."
        )
    elif "doc" in normalized_query or "statement" in normalized_query:
        doc_link = payload.get("documents_url") or "the PayU Finance app under My Loans > Documents"
        response = f"You can download your documents from {doc_link}."
    elif "repayment" in normalized_query or "pay" in normalized_query:
        response = (
            "You can change repayment options or prepay via My Loans > Repayment Options in the PayU Finance app. "
            "Let me know if you'd like a specialist to help."
        )
    else:
        response = pack["support_closing"]

    await messenger.send_text(phone, response)
    record_interaction(
        phone,
        "outbound",
        "post_disbursal_response",
        {"response": response, "query": normalized_query, "classification": category_label},
    )


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
    record_interaction(
        phone,
        "system",
        "agent_handoff",
        {"question": question, "queue": HUMAN_HANDOFF_QUEUE},
    )


async def send_dropoff_message(phone: str, language: str) -> None:
    pack = get_language_pack(language)
    await messenger.send_text(phone, pack["dropoff"])
    await messenger.send_text(phone, pack["resume_prompt"])
    record_interaction(
        phone,
        "outbound",
        "dropoff_nudge",
        {"language": language},
    )


def record_interaction(
    phone: str,
    direction: str,
    category: str,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    payload = payload or {}
    interaction_store.put(phone, direction, category, payload)


async def prompt_loan_flow(phone: str, language: str, pack: Optional[Dict[str, str]] = None) -> None:
    pack = pack or get_language_pack(language)
    if not WHATSAPP_FLOW_ID:
        await messenger.send_text(phone, "Loan form is currently unavailable. Please try again later.")
        return
    try:
        await messenger.send_flow(phone, language)
    except Exception as exc:  # pragma: no cover - flow failures
        logger.warning("Failed to send WhatsApp flow: %s", exc)
        await messenger.send_text(phone, "I'm having trouble opening the form. Please try again in a moment.")
        return
    await messenger.send_interactive_buttons(
        phone,
        pack["flow_sent"],
        [
            ("flow_open", pack["flow_button_label"]),
            ("intent_support", pack["support_button_label"]),
        ],
    )
    record_interaction(
        phone,
        "outbound",
        "whatsapp_flow",
        {"flow_id": WHATSAPP_FLOW_ID, "language": language},
    )


async def prompt_support_menu(phone: str, language: str) -> None:
    pack = get_language_pack(language)
    await messenger.send_interactive_buttons(
        phone,
        pack["support_menu_intro"],
        [
            ("support_payment", pack["support_btn_payment"]),
            ("support_status", pack["support_btn_status"]),
            ("support_docs", pack["support_btn_docs"]),
        ],
    )
    await messenger.send_interactive_buttons(
        phone,
        pack["support_menu_intro_secondary"],
        [
            ("support_repayment_change", pack["support_btn_repayment"]),
            ("support_btn_agent", pack["support_btn_agent"]),
        ],
    )
    await messenger.send_text(phone, pack["support_text_hint"])
    record_interaction(
        phone,
        "outbound",
        "support_menu",
        {"language": language},
    )


async def send_post_decision_options(phone: str, language: str) -> None:
    pack = get_language_pack(language)
    await messenger.send_interactive_buttons(
        phone,
        pack["ask_more_help"],
        [
            ("post_accept", pack["post_accept_label"]),
            ("intent_support", pack["post_support_label"]),
        ],
    )
    record_interaction(
        phone,
        "outbound",
        "post_decision_cta",
        {"language": language},
    )


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


def extract_button_reply_id(message: Dict[str, Any]) -> Optional[str]:
    interactive = message.get("interactive")
    if interactive and interactive.get("type") == "button_reply":
        return interactive["button_reply"].get("id")
    return None


async def handle_incoming_message(message: Dict[str, Any]) -> None:
    phone = message.get("from")
    if not phone:
        return

    state = conversation_store.get_or_create(phone)
    profile = user_store.get(phone) or UserProfile(phone=phone)
    previous_activity = profile.last_activity
    profile.touch()
    user_store.save(profile)

    reply_id = extract_button_reply_id(message)
    form_answers = form_answers_from_message(message)
    text = extract_message_text(message)
    normalized = text.strip().lower() if text else ""

    if state.language is None and profile.language:
        state.language = profile.language

    language = state.language or profile.language or DEFAULT_LANGUAGE
    pack = get_language_pack(language)
    state.is_existing = profile.is_existing

    record_interaction(
        phone,
        "inbound",
        "whatsapp_message",
        {
            "message_id": message.get("id"),
            "text": text,
            "reply_id": reply_id,
            "has_form": bool(form_answers),
            "language": language,
            "profile_exists": bool(profile.is_existing),
        },
    )

    if form_answers:
        record_interaction(
            phone,
            "inbound",
            "flow_submission",
            {"fields": list(form_answers.keys())},
        )
        state.language = language
        state.journey = "onboarding"
        await handle_form_submission(phone, form_answers, state, language, profile)
        return

    if state.language is None:
        lang_choice = None
        if reply_id in {"lang_en", "lang_hi"}:
            lang_choice = "en" if reply_id.endswith("en") else "hi"
        elif normalized:
            lang_choice = detect_language_choice(normalized)
        if lang_choice:
            state.language = lang_choice
            profile.language = lang_choice
            user_store.save(profile)
            await prompt_intent(phone, lang_choice, profile.is_existing)
            return
        await prompt_language(phone)
        return

    language = state.language
    pack = get_language_pack(language)

    if minutes_since(previous_activity) > INACTIVITY_MINUTES and state.journey:
        await send_dropoff_message(phone, language)
        state.journey = None
        state.answers.clear()
        state.awaiting_flow_completion = False
        state.awaiting_support_details = False

    if reply_id == "flow_open":
        await prompt_loan_flow(phone, language, pack)
        return
    if reply_id == "intent_apply":
        await start_onboarding(phone, state, language)
        return
    if reply_id == "intent_support":
        state.journey = "support"
        state.awaiting_flow_completion = False
        state.awaiting_support_details = True
        state.answers.clear()
        await messenger.send_text(
            phone,
            pack["support_prompt_existing" if profile.is_existing else "support_prompt_new"],
        )
        await prompt_support_menu(phone, language)
        return
    if reply_id == "post_accept":
        await messenger.send_text(phone, pack["accept_ack"])
        record_interaction(
            phone,
            "inbound",
            "post_accept",
            {"source": "button"},
        )
        return
    if reply_id and reply_id in SUPPORT_SHORTCUTS:
        await handle_support_shortcut(phone, language, profile, SUPPORT_SHORTCUTS[reply_id])
        state.reset(keep_language=True)
        return
    if reply_id == "support_btn_agent":
        await messenger.send_text(phone, pack["support_handoff"])
        await escalate_to_agent(phone, "Agent requested", profile)
        await messenger.send_text(phone, pack["support_escalation_ack"])
        state.reset(keep_language=True)
        return

    if not text:
        await messenger.send_text(phone, pack["text_only_warning"])
        return

    if normalized in {"accept", "accepted", "accept offer"}:
        await messenger.send_text(phone, pack["accept_ack"])
        record_interaction(
            phone,
            "inbound",
            "post_accept",
            {"source": "text"},
        )
        return

    if state.journey is None:
        intent = intent_from_text(normalized)
        if intent == "apply":
            await start_onboarding(phone, state, language)
            return
        if intent == "support":
            state.journey = "support"
            state.awaiting_support_details = True
            state.answers.clear()
            await messenger.send_text(
                phone,
                pack["support_prompt_existing" if profile.is_existing else "support_prompt_new"],
            )
            await prompt_support_menu(phone, language)
            return
        if intent == "post_disbursal" and loan_store.get_record(phone):
            await handle_post_disbursal(phone, language, normalized)
            return
        await prompt_intent(phone, language, profile.is_existing)
        return

    if state.journey == "onboarding":
        if normalized in {"support", "help"}:
            state.journey = "support"
            state.awaiting_flow_completion = False
            state.awaiting_support_details = True
            state.answers.clear()
            await messenger.send_text(
                phone,
                pack["support_prompt_existing" if profile.is_existing else "support_prompt_new"],
            )
            await prompt_support_menu(phone, language)
            return
        if state.awaiting_flow_completion:
            await messenger.send_text(phone, pack["flow_sent"])
            await prompt_loan_flow(phone, language, pack)
        else:
            await messenger.send_text(phone, pack["fallback_intent"])
        return

    if state.journey == "support":
        if normalized in {"apply", "loan"}:
            await start_onboarding(phone, state, language)
            return
        if normalized in {"support", "help"}:
            await prompt_support_menu(phone, language)
            state.awaiting_support_details = True
            return
        if loan_store.get_record(phone) and normalized in {"balance", "emi", "statement", "docs", "document", "repayment"}:
            await handle_post_disbursal(phone, language, normalized)
            return
        state.awaiting_support_details = True
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
