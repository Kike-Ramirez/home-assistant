"""Flow 2: troubleshooting.

The full home device inventory gets passed along, and Claude decides which
one the question is about (no matching heuristics of our own).
"""

from __future__ import annotations

from typing import Any

from shared.message import NormalizedMessage
from shared.mqtt_client import ManagedMqttConnection
from shared.postgrest_client import PostgrestClient

from ..claude_client import ask_troubleshooting
from ..messaging import reply
from ..registry import list_devices


async def handle_question(pg: PostgrestClient, mqtt: ManagedMqttConnection, conversation: dict[str, Any], msg: NormalizedMessage) -> None:
    devices = await list_devices(pg)
    answer = await ask_troubleshooting(devices, msg.content or "")
    await reply(mqtt, msg, answer)
