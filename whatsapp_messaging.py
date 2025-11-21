import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


class MetaWhatsAppClient:
    """Thin wrapper around Meta's WhatsApp Cloud API."""

    def __init__(
        self,
        token: Optional[str],
        phone_number_id: Optional[str],
        api_version: str = "v18.0",
    ):
        self.token = token
        self.phone_number_id = phone_number_id
        self.api_version = api_version
        self.base_url = (
            f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
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

    async def send_interactive_buttons(
        self, to: str, body: str, buttons: List[Tuple[str, str]]
    ) -> None:
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

    async def send_flow(
        self,
        to: str,
        language: str,
        flow_id: Optional[str],
        flow_token: Optional[str] = None,
    ) -> None:
        if not flow_id:
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
                            "language": {
                                "code": "en_US" if language == "en" else "hi_IN"
                            },
                            "flow_id": flow_id,
                            "flow_token": flow_token,
                        }
                    },
                },
            }
        )
