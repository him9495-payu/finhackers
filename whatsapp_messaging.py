import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

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

    def _post(self, payload: Dict[str, Any]) -> None:
        if not self.enabled:
            logger.info("[dry-run] %s", payload)
            return

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        response = requests.post(self.base_url, json=payload, headers=headers, timeout=10)
        if not response.ok:
            logger.error(
                "WhatsApp send failed - %s %s", response.status_code, response.text
            )
            response.raise_for_status()

    def send_text(self, to: str, body: str) -> None:
        self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body},
            }
        )

    def send_interactive_buttons(
        self, to: str, body: str, buttons: List[Tuple[str, str]]
    ) -> None:
        action_buttons = [
            {"type": "reply", "reply": {"id": button_id, "title": title[:20]}}
            for button_id, title in buttons[:3]
        ]
        self._post(
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

    def send_flow(
        self,
        to: str,
        language: str,
        flow_id: Optional[str],
        flow_token: Optional[str] = None,
        flow_cta: str = "Open form",
        flow_version: str = "7.2",
        entry_screen: str = "loan_form",
    ) -> None:
        if not (flow_id and flow_token):
            raise RuntimeError("WhatsApp Flow ID and token must be configured")

        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "flow",
                "body": {"text": "PayU Finance Loan Form"},
                "action": {
                    "name": "flow",
                    "parameters": {
                        "flow_message_version": flow_version,
                        "flow_id": flow_id,
                        "flow_token": flow_token,
                        "flow_cta": flow_cta,
                        "flow_action": "navigate",
                        "flow_action_payload": {
                            "screen": entry_screen,
                            "data": {
                                "language": "en" if language == "en" else "hi"
                            },
                        },
                    },
                },
            },
        }
        self._post(payload)
