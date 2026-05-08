from typing import Dict
from fastapi import WebSocket
import json
import asyncio


class ConnectionManager:
    def __init__(self):
        # user_id -> list of WebSocket (allow multiple tabs)
        self.active: Dict[int, list] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active.setdefault(user_id, []).append(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket):
        conns = self.active.get(user_id, [])
        if websocket in conns:
            conns.remove(websocket)
        if not conns:
            self.active.pop(user_id, None)

    async def send_to(self, user_id: int, event: str, data: dict):
        """Send JSON event to a specific user (all their open tabs)."""
        payload = json.dumps({"event": event, "data": data})
        dead = []
        for ws in self.active.get(user_id, []):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(user_id, ws)

    async def broadcast_to_many(self, user_ids: list, event: str, data: dict):
        await asyncio.gather(*(self.send_to(uid, event, data) for uid in user_ids))

    def fire(self, coro):
        """Schedule a coroutine without blocking the HTTP response (same sends, async delivery)."""
        asyncio.create_task(coro)


manager = ConnectionManager()
