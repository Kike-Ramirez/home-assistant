"""Flow 4: replacement / new purchase.

The inventory (with supported standards/protocols) and the user's full
request get passed to Claude — it reasons about compatibility and uses
`web_search` for popularity/pricing, with no ranking logic of our own.
"""

from __future__ import annotations

from typing import Any

import aiomqtt

from shared.message import NormalizedMessage
from shared.postgrest_client import PostgrestClient

from ..claude_client import ask_replacement
from ..messaging import reply
from ..registry import list_devices_with_standards


async def handle_replacement(
    pg: PostgrestClient,
    mqtt: aiomqtt.Client,
    conversation: dict[str, Any],
    msg: NormalizedMessage,
) -> None:
    devices = await list_devices_with_standards(pg)
    answer = ask_replacement(devices, msg.content or "")
    await reply(mqtt, msg, answer)
