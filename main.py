from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict

app = FastAPI()

rooms: Dict[str, dict] = {
    f"room_{i}": {"players": [], "state": "waiting"} for i in range(1, 6)
}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    player = {"ws": websocket, "room": None, "status": "watching", "id": id(websocket)}

    try:
        await websocket.send_json({"type": "your_id", "id": player["id"]})

        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "get_room_counts":
                counts = {room_id: len(room["players"]) for room_id, room in rooms.items()}
                await websocket.send_json({"type": "room_counts", "counts": counts})

            elif msg_type == "join_room":
                room_id = data["room_id"]
                room = rooms[room_id]

                if len(room["players"]) >= 10:
                    await websocket.send_json({"type": "error", "message": "部屋が満員です。"})
                    continue

                room["players"].append(player)
                player["room"] = room_id
                player["status"] = "watching"

                await broadcast_room(room_id)
                await update_status(room_id)

            elif msg_type == "leave_room":
                await leave_room(player)

            elif msg_type == "change_status":
                new_status = data["status"]
                player["status"] = new_status
                room_id = player["room"]
                await broadcast_room(room_id)
                await update_status(room_id)

            elif msg_type == "start_game":
                room_id = player["room"]
                room = rooms[room_id]
                waiting_players = [p for p in room["players"] if p["status"] == "waiting"]

                if len(waiting_players) < 2:
                    await websocket.send_json({"type": "error", "message": "対戦待ちが2人必要です。"})
                    continue

                await start_game(room)

            elif msg_type == "chat":
                room_id = player["room"]
                await broadcast_room(room_id, {"type": "chat", "sender": player["id"], "message": data["message"]})

    except WebSocketDisconnect:
        await leave_room(player)

async def leave_room(player):
    room_id = player["room"]
    if room_id and player in rooms[room_id]["players"]:
        rooms[room_id]["players"].remove(player)
        player["room"] = None
        update_message = {
            "type": "update_room",
            "room_id": room_id,
            "count": len(rooms[room_id]["players"]),
            "player_list": [{"id": p["id"], "status": p["status"]} for p in rooms[room_id]["players"]]
        }
        await broadcast_room(room_id)
        await player["ws"].send_json(update_message)
        await update_status(room_id)

async def broadcast_room(room_id, extra_message=None):
    room = rooms[room_id]
    message = {
        "type": "update_room",
        "room_id": room_id,
        "count": len(room["players"]),
        "player_list": [{"id": p["id"], "status": p["status"]} for p in room["players"]]
    }
    if extra_message:
        message.update(extra_message)

    for p in room["players"]:
        await p["ws"].send_json(message)

async def update_status(room_id):
    room = rooms[room_id]
    waiting_count = len([p for p in room["players"] if p["status"] == "waiting"])
    await broadcast_room(room_id, {"type": "status_update", "waiting_count": waiting_count})

async def start_game(room):
    room["state"] = "playing"
    for p in room["players"]:
        await p["ws"].send_json({"type": "game_start", "message": "ゲーム開始!"})
    room["state"] = "waiting"
