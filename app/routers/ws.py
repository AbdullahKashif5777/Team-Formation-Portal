from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from app.ws_manager import manager
from app.auth import decode_token

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int, token: str = Query(...)):
    # Validate token
    try:
        payload = decode_token(token)
        token_user_id = int(payload.get("sub", -1))
        if token_user_id != user_id:
            await websocket.close(code=4001)
            return
    except Exception:
        await websocket.close(code=4001)
        return

    await manager.connect(user_id, websocket)
    try:
        while True:
            # Keep-alive: client sends ping, we send pong
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
