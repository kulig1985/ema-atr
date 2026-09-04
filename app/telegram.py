from __future__ import annotations

import httpx


class TelegramClient:
    def __init__(self, http: httpx.AsyncClient, bot_token: str, chat_id: str):
        self.http = http
        self.bot_token = bot_token
        self.chat_id = chat_id

    async def send(self, text: str) -> None:
        response = await self.http.post(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
