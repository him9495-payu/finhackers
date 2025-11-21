from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

try:
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None

logger = logging.getLogger(__name__)


def now_ts() -> float:
    return time.time()


def iso_timestamp(ts: Optional[float] = None) -> str:
    value = datetime.fromtimestamp(ts or now_ts(), tz=timezone.utc)
    return value.isoformat()


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
    language_prompted: bool = False

    def reset(self, keep_language: bool = True):
        lang = self.language if keep_language else None
        self.language = lang
        self.journey = None
        self.is_existing = None
        self.answers.clear()
        self.awaiting_support_details = False
        self.awaiting_flow_completion = False
        if not keep_language:
            self.language_prompted = False


class UserProfileStore:
    """Persist user profiles to DynamoDB (no local fallback)."""

    def __init__(self, table_name: Optional[str], region: str):
        if not table_name:
            raise RuntimeError("USER_TABLE_NAME must be configured for persistent state.")
        if not boto3:
            raise RuntimeError("boto3 is required to access DynamoDB.")
        self.table_name = table_name
        self.region = region
        try:
            resource = boto3.resource("dynamodb", region_name=region)
            self._table = resource.Table(table_name)
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"Failed to connect to DynamoDB table {table_name}") from exc

    @property
    def uses_dynamo(self) -> bool:
        return True

    def get(self, phone: str) -> Optional[UserProfile]:
        try:
            response = self._table.get_item(Key={"phone": phone})
        except Exception as exc:  # pragma: no cover
            logger.error("Dynamo get_item failed: %s", exc)
            raise
        item = response.get("Item")
        return UserProfile.from_item(item) if item else None

    def save(self, profile: UserProfile) -> None:
        profile.touch()
        try:
            self._table.put_item(Item=profile.to_item())
        except Exception as exc:  # pragma: no cover
            logger.error("Dynamo put_item failed: %s", exc)
            raise


class LoanRecordStore:
    """Stores disbursal-level data for post-loan servicing."""

    def __init__(self, table_name: Optional[str], region: str):
        if not table_name:
            raise RuntimeError("LOAN_TABLE_NAME must be configured.")
        if not boto3:
            raise RuntimeError("boto3 is required to access DynamoDB.")
        self.table_name = table_name
        self.region = region
        try:
            resource = boto3.resource("dynamodb", region_name=region)
            self._table = resource.Table(table_name)
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"Failed to connect to DynamoDB table {table_name}") from exc

    def upsert_from_decision(
        self,
        phone: str,
        decision: Any,
        application: Any,
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
        try:
            self._table.put_item(Item=record)
        except Exception as exc:  # pragma: no cover
            logger.error("Dynamo loan_record put_item failed: %s", exc)
            raise

    def get_record(self, phone: str) -> Optional[Dict[str, Any]]:
        try:
            response = self._table.get_item(Key={"phone": phone})
            item = response.get("Item")
            if item:
                return item
        except Exception as exc:  # pragma: no cover
            logger.error("Dynamo loan_record get_item failed: %s", exc)
            raise
        return None


class InteractionStore:
    """Persist every inbound/outbound interaction for auditing and analytics."""

    def __init__(self, table_name: Optional[str], region: str):
        if not table_name:
            raise RuntimeError("INTERACTION_TABLE_NAME must be configured.")
        if not boto3:
            raise RuntimeError("boto3 is required to access DynamoDB.")
        self.table_name = table_name
        self.region = region
        try:
            resource = boto3.resource("dynamodb", region_name=region)
            self._table = resource.Table(table_name)
        except Exception as exc:
            raise RuntimeError(f"Failed to connect to DynamoDB table {table_name}") from exc

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
        try:
            self._table.put_item(Item=item)
        except Exception as exc:  # pragma: no cover
            logger.error("Dynamo interaction put_item failed: %s", exc)
            raise


def _sanitize_for_dynamo(value: Any):
    if value is None:
        return None
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, nested in value.items():
            sanitized = _sanitize_for_dynamo(nested)
            if sanitized is None:
                continue
            cleaned[key] = sanitized
        return cleaned
    if isinstance(value, list):
        cleaned_list = []
        for item in value:
            sanitized = _sanitize_for_dynamo(item)
            if sanitized is not None:
                cleaned_list.append(sanitized)
        return cleaned_list
    return value


def serialize_conversation_state(state: ConversationState) -> Dict[str, Any]:
    raw = asdict(state)
    cleaned = _sanitize_for_dynamo(raw)
    return cleaned or {}
