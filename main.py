from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, List
import random

app = FastAPI()

def is_prime(n: int) -> bool:
    if n < 2:
        return False
    for i in range(2, int(n**0.5) + 1):
        if n % i == 0:
            return False
    return True

rooms: Dict[str, dict] = {
    f"room_{i}": {
        "players": [],
        "state": "waiting",
        "deck": [],
        "hands": {},  # player_id -> list of cards
        "field": [],  # 場に出ているカード
        "current_turn_id": None,
        "has_drawn": False
    } for i in range(1, 6)
}

################################################
# カード生成と配布のユーティリティ
################################################
def generate_deck() -> List[dict]:
    deck = [{"suit": suit, "rank": rank} for suit in ["S", "H", "D", "C"] for rank in range(1, 14)]
    # ルールの簡略化のためジョーカーを入れない
    # deck += [{"suit": "J", "rank": 0}, {"suit": "J", "rank": 0}]
    return deck

def shuffle_and_deal(deck: List[dict]) -> (List[dict], List[dict], List[dict]):
    random.shuffle(deck)
    hand1 = deck[:5]
    hand2 = deck[5:10]
    remaining_deck = deck[10:]
    return hand1, hand2, remaining_deck

################################################
# 勝敗判定ロジック
################################################

def check_win_condition(room):
    current_player = room.get("current_turn_id")
    if current_player is not None:
        hand = room["hands"].get(current_player, [])
        if len(hand) == 0:
            return current_player
    return None


################################################
# WebSocket処理
################################################

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    player = {"ws": websocket, "room": None, "status": "watching", "id": id(websocket)}

    try:
        # 自分のIDを通知
        await websocket.send_json({"type": "your_id", "id": player["id"]})

        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            room_id = player["room"] if player["room"] else None

            if msg_type == "get_room_counts":
                counts = {rid: len(r["players"]) for rid, r in rooms.items()}
                await websocket.send_json({"type": "room_counts", "counts": counts})

            elif msg_type == "join_room":
                rid = data["room_id"]
                room = rooms[rid]

                if len(room["players"]) >= 10:
                    await websocket.send_json({"type": "error", "message": "部屋が満員です。"})
                    continue

                room["players"].append(player)
                player["room"] = rid
                player["status"] = "watching"  # 仮に入室したらwatchingに

                await broadcast_room(rid)
                await update_status(rid)
                await broadcast_room(rid, {"type": "state_update", "room_state": room["state"]})

            elif msg_type == "leave_room":
                await leave_room(player)

            elif msg_type == "change_status":
                if not room_id:  # 部屋にいなければ無視
                    continue
                new_status = data["status"]
                player["status"] = new_status
                await broadcast_room(room_id)
                await update_status(room_id)

            elif msg_type == "start_game":
                if not room_id:
                    continue
                room = rooms[room_id]

                # 対戦待ちプレイヤー確認
                waiting_players = [p for p in room["players"] if p["status"] == "waiting"]
                if len(waiting_players) != 2:
                    await websocket.send_json({"type": "error", "message": "対戦待ちが2人必要です。"})
                    continue

                await start_game(room)

            elif msg_type == "play_card":
                if not room_id:
                    continue
                room = rooms[room_id]
                if player["id"] != room["current_turn_id"]:
                    await websocket.send_json({"type": "error", "message": "あなたのターンではありません。"})
                    continue

                played_cards = data.get("cards", [])
                ranks_str = "".join(str(c["rank"]) for c in played_cards)
                try:
                    number = int(ranks_str)
                except ValueError:
                    number = -1

                # 手札にあるか検証
                temp_hand = room["hands"][player["id"]][:]
                can_play = True
                for c in played_cards:
                    if c in temp_hand:
                        temp_hand.remove(c)
                    else:
                        can_play = False
                        break
                if not can_play:
                    await websocket.send_json({"type": "error", "message": "そのカードは手札にありません。"})
                    continue

                # もしフィールドに既にカードが出ているなら、枚数と数の検証を行う
                if room["field"]:
                    # ① 枚数チェック
                    if len(played_cards) != len(room["field"]):
                        await websocket.send_json({"type": "error", "message": "枚数が違います。"})
                        continue

                    # ② 数値チェック：フィールドのカードと比較
                    field_ranks_str = "".join(str(c["rank"]) for c in room["field"])
                    try:
                        field_number = int(field_ranks_str)
                    except ValueError:
                        field_number = -1

                    if number <= field_number:
                        await websocket.send_json({"type": "error", "message": "場より小さい。"})
                        continue

                # 素数判定
                if not is_prime(number):
                    # ペナルティ
                    # 出そうとしたカードを引き直すことはしない(そもそも出されていないため)
                    if room["deck"]:
                        drawn = room["deck"].pop(0)
                        room["hands"][player["id"]].append(drawn)

                    await player["ws"].send_json({
                        "type": "hand_update",
                        "your_hand": room["hands"][player["id"]]
                    })

                    # フィールドをリセット（場のカードを消す）2人対戦想定であることに注意
                    room["field"] = []

                    await broadcast_room(room_id, {
                        "type": "penalty",
                        "player_id": player["id"],
                        "played_cards": played_cards,
                        "number": number
                    })

                    # チャットにペナルティのログを流す
                    await log_chat(room, f"プレイヤー{player['id']}が{number}を出そうとしましたが、{number}は素数ではありません")

                    await next_turn(room)
                    continue

                # 素数なら場に出す
                for c in played_cards:
                    room["hands"][player["id"]].remove(c)
                room["field"] = played_cards

                await player["ws"].send_json({
                    "type": "hand_update",
                    "your_hand": room["hands"][player["id"]]
                })


                await broadcast_room(room_id, {
                    "type": "action_result",
                    "action": "play_card",
                    "player_id": player["id"],
                    "played_cards": played_cards,
                    "number": number
                })

                # チャットに「素数を出した」ログを流す
                await log_chat(room, f"プレイヤー{player['id']}が{number}を出しました")
                await next_turn(room)

            elif msg_type == "draw_card":
                if not room_id:
                    continue
                room = rooms[room_id]
                if player["id"] != room["current_turn_id"]:
                    await websocket.send_json({"type": "error", "message": "あなたのターンではありません。"})
                    continue

                # すでにこのターンでドローしているかチェック
                if room.get("has_drawn", False):
                    await websocket.send_json({"type": "error", "message": "このターンはすでにドロー済みです。"})
                    continue

                # ドロー処理
                if len(room["deck"]) > 0:
                    drawn = room["deck"].pop(0)
                    room["hands"][player["id"]].append(drawn)

                    # 自分に手札更新を送る
                    await player["ws"].send_json({
                        "type": "hand_update",
                        "your_hand": room["hands"][player["id"]]
                    })

                # ドロー済みフラグを設定（このターンはこれ以上ドローできない）
                room["has_drawn"] = True
                # ※ここでは next_turn(room) は呼ばない → 手番は変わらない

            elif msg_type == "pass":
                if not room_id:
                    continue
                room = rooms[room_id]
                if player["id"] != room["current_turn_id"]:
                    await websocket.send_json({"type": "error", "message": "あなたのターンではありません。"})
                    continue

                await player["ws"].send_json({
                    "type": "hand_update",
                    "your_hand": room["hands"][player["id"]]
                })

                # パスの場合もフィールドをリセット
                room["field"] = []

                # passの通知
                await broadcast_room(room_id, {
                    "type": "action_result",
                    "action": "pass",
                    "player_id": player["id"]
                })
                # チャットにパスのログを流す
                await log_chat(room, f"プレイヤー{player['id']}がパスしました")
                # 次のターンへ
                await next_turn(room)

            elif msg_type == "chat":
                if not room_id:
                    continue
                await broadcast_room(room_id, {
                    "type": "chat",
                    "sender": player["id"],
                    "message": data["message"]
                })

    except WebSocketDisconnect:
        await leave_room(player)

################################################
# 部屋からの退出
################################################
async def leave_room(player):
    room_id = player["room"]
    if room_id and player in rooms[room_id]["players"]:
        rooms[room_id]["players"].remove(player)
        player["room"] = None

        # ゲーム中の特別処理
        room = rooms[room_id]
        if room["state"] == "playing":
            # 現在ターンのプレイヤーが切断した場合、次のターンに進める
            if room["current_turn_id"] == player["id"]:
                await next_turn(room)

            # 参加プレイヤーが1人だけになったら、ゲーム終了
            if len(room["players"]) == 1:
                winner_id = room["players"][0]["id"]
                await broadcast_room(room_id, {"type": "game_over", "winner": winner_id})
                room["state"] = "waiting"

        update_message = {
            "type": "update_room",
            "room_id": room_id,
            "count": len(rooms[room_id]["players"]),
            "player_list": [{"id": p["id"], "status": p["status"]} for p in rooms[room_id]["players"]]
        }
        await broadcast_room(room_id)
        await player["ws"].send_json(update_message)
        await update_status(room_id)

################################################
# 部屋情報一括通知
################################################
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

################################################
# 対戦待ち人数などを更新通知
################################################
async def update_status(room_id):
    room = rooms[room_id]
    waiting_count = len([p for p in room["players"] if p["status"] == "waiting"])
    await broadcast_room(room_id, {"type": "status_update", "waiting_count": waiting_count})

################################################
# ゲーム開始処理
################################################
async def start_game(room):
    # 1) デッキを生成＆シャッフル＆配布
    deck = generate_deck()
    hand1, hand2, remaining_deck = shuffle_and_deal(deck)

    # 対戦待ちのプレイヤーのみから2人選ぶ
    waiting_players = [p for p in room["players"] if p["status"] == "waiting"]
    if len(waiting_players) < 2:
        # ここは通常、start_gameを呼ぶ前にチェックしているので安全策ですが
        return

    # 対戦待ちからランダムに2人選択
    p1, p2 = random.sample(waiting_players, 2)

    room["deck"] = remaining_deck
    room["hands"][p1["id"]] = hand1
    room["hands"][p2["id"]] = hand2
    room["field"] = []  # 場のカードは空

    room["state"] = "playing"

    # ランダムに先攻プレイヤー決定
    room["current_turn_id"] = random.choice([p1["id"], p2["id"]])

    # プレイヤーそれぞれに手札情報を送信
    await p1["ws"].send_json({
        "type": "deal",
        "your_hand": room["hands"][p1["id"]]
    })
    await p2["ws"].send_json({
        "type": "deal",
        "your_hand": room["hands"][p2["id"]]
    })

    # 全体にゲーム開始メッセージ & 現在のターン情報
    for p in room["players"]:
        await p["ws"].send_json({
            "type": "game_start",
            "message": "ゲーム開始!",
            "current_turn": room["current_turn_id"]
        })

    # チャットにログを流す
    await log_chat(room, "ゲーム開始！")

################################################
# 次のターンに移る
################################################
async def next_turn(room):
    # ターンが変わるので、ドロー済みフラグをリセットする
    room["has_drawn"] = False

    # 対戦に参加している（statusが"waiting"の）プレイヤーだけを対象とする
    active_players = [p for p in room["players"] if p["status"] == "waiting"]
    if len(active_players) < 2:
        return

    # 勝利者がいるかチェック
    winner = check_win_condition(room)
    if winner:
        room["state"] = "waiting"
        # 正しい部屋IDを使ってクライアントへ通知（例：最初のプレイヤーの room キーを利用）
        await broadcast_room(active_players[0]["room"], {"type": "game_over", "winner": winner, "state": room["state"]})
        # チャットに勝利ログを流す
        await log_chat(room, f"プレイヤー {winner} が勝利しました。")
        return

    current_turn_id = room["current_turn_id"]
    # 現在の手番プレイヤーが active_players の中にいるかを確認
    idx = [i for i, p in enumerate(active_players) if p["id"] == current_turn_id]
    if not idx:
        # もし現在の手番プレイヤーが active でなければ、先頭のプレイヤーに設定
        room["current_turn_id"] = active_players[0]["id"]
    else:
        # 元の順番を無視しているようだが2人対戦の間は大丈夫か？
        current_idx = idx[0]
        next_idx = (current_idx + 1) % len(active_players)
        room["current_turn_id"] = active_players[next_idx]["id"]

    await broadcast_room(active_players[0]["room"], {
        "type": "next_turn",
        "current_turn": room["current_turn_id"],
        "deck_count": len(room["deck"])  # 山札の枚数を追加
    })

################################################
# ログをチャットに送信
################################################
async def log_chat(room, message, sender="system"):
    # 部屋内の全プレイヤーへチャットメッセージをブロードキャスト
    room_id = room["players"][0]["room"] if room["players"] else None
    if room_id:
        await broadcast_room(room_id, {
            "type": "chat",
            "sender": sender,
            "message": message
        })
