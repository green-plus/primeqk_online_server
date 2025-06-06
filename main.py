from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, List
import random
import secrets
import uuid
import asyncio
import os, httpx

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
app = FastAPI()

################################################
# 素数判定
################################################

def is_prime(n: int) -> bool:
    if n < 2:
        return False
    for i in range(2, int(n**0.5) + 1):
        if n % i == 0:
            return False
    return True

################################################
# クラス定義
################################################

class Room:
    def __init__(self, room_id: str):
        self.room_id = room_id
        self.players = []    # Playerオブジェクトのリスト
        self.state = "waiting"
        self.deck = []
        self.field = []      # 場に出ているカード
        self.last_number = None     # “場に出ている”最後の数値を保持
        self.current_turn_id = None
        self.has_drawn = False
        self.reverse_order = False

    async def broadcast(self, message: dict):
        for p in self.players:
            await p.send_json(message)

    async def update_room_status(self):
        message = {
            "type": "update_room_status",
            "room_id": self.room_id,
            "count": len(self.players),
            "player_list": [
                {"id": p.id, "name": p.name, "status": p.status}
                for p in self.players
            ],
            "waiting_count": len([p for p in self.players if p.status == "waiting"])
        }
        await self.broadcast(message)

    async def log_chat(self, message: str, sender="system"):
        await self.broadcast({"type": "chat", "sender": sender, "message": message})

    # その他、ルームに関連するロジック（プレイヤー追加、削除、ゲーム開始、次のターンなど）をメソッドとして実装
    async def update_game_state(self):
        current_player = next((p for p in self.players if p.id == self.current_turn_id), None)
        current_name = current_player.name if current_player else None
        state_msg = {
            "type": "game_update",
            "room_id": self.room_id,
            "state": self.state,
            "current_turn": current_name,
            "revolution": self.reverse_order,
            "deck_count": len(self.deck),
            "field": self.field,
            "player_list": [{"id": p.id, "name": p.name, "status": p.status} for p in self.players],
            "hand_counts": [{"id": p.id, "name": p.name, "count": len(p.hand)} for p in self.players]
        }
        await self.broadcast(state_msg)

    async def try_end_game(self) -> bool:
        """勝者がいれば game_over を投げて True、なければ False を返す"""
        winner = check_win_condition(self)
        if winner is not None:
            self.state = "waiting"
            await self.broadcast({"type": "game_over", "winner": winner, "state": self.state})
            await self.log_chat(f"{winner}が勝利しました")
            return True
        return False


# アプリケーションの初期化時にRoomインスタンスを必要な数だけ作成しておく
rooms: Dict[str, Room] = {f"room_{i}": Room(f"room_{i}") for i in range(1, 6)}

class Player:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.id = secrets.token_hex(16)
        suffix = int(self.id, 16) % 10000
        self.name = f"プレイヤー{suffix:04d}"
        self.room = None  # 所属ルーム（Roomオブジェクト）
        self.status = "watching"  # 初期状態は観戦中
        self.hand = []  # プレイヤーが持つカードリスト

    async def send_json(self, message: dict):
        """WebSocketを通じてJSONメッセージを送信する"""
        await self.ws.send_json(message)

    async def send_hand_update(self):
        """手札の変更通知をクライアントに送信する"""
        message = {
            "type": "hand_update",
            "your_hand": self.hand
        }
        await self.send_json(message)

    def sort_hand(self):
        """手札をランク順（必要に応じてスートも考慮）に並び替える"""
        # ここでは単純にカードの"rank"で昇順にソート
        self.hand.sort(key=lambda card: card["rank"])

    def add_card(self, card: dict):
        """手札にカードを追加する"""
        self.hand.append(card)
        self.sort_hand()  # カード追加後に手札を並び替え

    def remove_card(self, card: dict) -> bool:
        """手札から指定のカードを削除する。存在すればTrue、なければFalseを返す"""
        if card in self.hand:
            self.hand.remove(card)
            return True
        return False

    def has_cards(self, cards: List[dict]) -> bool:
        """指定されたカード群が自分の手札に存在するかチェックする"""
        temp = self.hand[:]  # コピーを使ってチェック
        for card in cards:
            if card in temp:
                temp.remove(card)
            else:
                return False
        return True

    def remove_cards(self, cards: List[dict]) -> bool:
        """指定されたカード群を手札から削除する。すべて削除できた場合にTrueを返す"""
        if not self.has_cards(cards):
            return False
        for card in cards:
            self.remove_card(card)
        return True

    def clear_hand(self):
        """手札をクリアする"""
        self.hand = []

################################################
# 勝敗判定ロジック
################################################

def check_win_condition(room):
    # 1. プレイヤー1人による特殊勝利
    if len(room.players) == 1:
        return room.players[0].name
    # 2. 現在プレイヤーの手札0枚による通常勝利
    current_turn_id = room.current_turn_id
    if current_turn_id is None:
        return None
    # room.playersはPlayerオブジェクトのリストであるとする
    current_player = next((p for p in room.players if p.id == current_turn_id), None)
    if current_player is not None:
        if len(current_player.hand) == 0:
            # 勝利者のIDまたはPlayerオブジェクトそのものを返す（要件に応じて）
            return current_player.name
    return None

################################################
# カード生成と配布のユーティリティ
################################################
def generate_deck() -> List[dict]:
    deck = []
    for suit in ["S","H","D","C"]:
        for rank in range(1,14):
            deck.append({
                "card_id": str(uuid.uuid4()),
                "suit": suit,
                "rank": rank,
                "is_joker": False
            })
    # ジョーカー２枚にも同様にIDを
    for _ in range(2):
        deck.append({
            "card_id": str(uuid.uuid4()),
            "suit": "X",
            "rank": 0,
            "is_joker": True
        })
    random.shuffle(deck)
    return deck

def shuffle_and_deal(deck: List[dict]) -> (List[dict], List[dict], List[dict]):
    random.shuffle(deck)
    hand1 = deck[:5]
    hand2 = deck[5:10]
    remaining_deck = deck[10:]
    return hand1, hand2, remaining_deck

################################################
# Webhook
################################################

async def notify_discord(content: str):
    if not WEBHOOK_URL:
        print("⚠️ Webhook URL が設定されていません")
        return

    try:
        async with httpx.AsyncClient() as client:
            await client.post(WEBHOOK_URL, json={"content": content})
    except Exception as e:
        # エラーをハンドリング
        print("notify_discord failed:", e)

################################################
# WebSocket処理
################################################

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    player = Player(websocket)  # 辞書ではなくPlayerクラスのインスタンスを生成

    try:
        # 自分のIDを通知
        await websocket.send_json({"type": "your_id", "id": player.id})

        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            room_id = player.room.room_id if player.room else None

            if msg_type == "set_name":
                # クライアントから名前を受け取る
                player.name = data.get("name", "").strip() or f"プレイヤー{player.id}"
                # 必要なら acknowledgment を返す
                await player.send_json({"type": "name_set", "name": player.name})
                continue
            elif msg_type == "get_room_counts":
                counts = {room_id: len(room.players) for room_id, room in rooms.items()}
                await websocket.send_json({"type": "room_counts", "counts": counts})

            elif msg_type == "join_room":
                rid = data["room_id"]
                room = rooms[rid]

                if len(room.players) >= 10:
                    await websocket.send_json({"type": "error", "message": "部屋が満員です。"})
                    continue

                await room.log_chat(f"{player.name}が入室しました")
                # 同期処理の後で、バックグラウンドに通知タスクを投げる
                asyncio.create_task(
                    notify_discord(f"🎮 {player.name} が {room.room_id} に参加しました")
                )


                room.players.append(player)
                player.room = room
                player.status = "watching"  # 仮に入室したらwatchingに

                await room.update_room_status()
                await room.broadcast({"type": "room_state_initialization", "room_state": room.state})

            elif msg_type == "leave_room":
                await leave_room(player)

            elif msg_type == "change_status":
                if not player.room:  # 部屋にいなければ無視
                    continue
                new_status = data["status"]
                player.status = new_status
                await room.update_room_status()

            elif msg_type == "start_game":
                if not player.room:
                    continue
                room = player.room

                # 対戦待ちプレイヤー確認
                waiting_players = [p for p in room.players if p.status == "waiting"]
                if len(waiting_players) != 2:
                    await websocket.send_json({"type": "error", "message": "対戦待ちが2人必要です。"})
                    continue

                await start_game(room)

            elif msg_type == "play_card":
                if not player.room:
                    continue
                room = player.room
                if player.id != room.current_turn_id:
                    await websocket.send_json({"type": "error", "message": "あなたのターンではありません。"})
                    continue

                played_cards = data.get("cards", [])
                # 手札にあるか検証
                if not player.has_cards(played_cards):
                    await player.ws.send_json({"type": "error", "message": "そのカードは手札にありません。"})
                    continue

                # ジョーカー絡みの処理
                assigned_numbers = data.get("assigned_numbers", [])  # [ "inf" か 0〜13, ... ]
                # ―――――――――――――――――――
                # １）ジョーカーだけを単独で出す (グロタンカット相当)
                jokers = [c for c in played_cards if c["suit"] == "X"]
                if len(jokers) == 1 and len(played_cards) == 1:
                    # ジョーカー1枚だけ → 場を流す
                    player.remove_card(jokers[0])
                    room.field = []
                    room.last_number = None
                    room.has_drawn = False
                    await player.send_hand_update()
                    await room.log_chat(f"{player.name}がジョーカーを出しました、インフィニティ！")
                    await room.update_game_state()
                    if await room.try_end_game():
                        await room.update_room_status()
                    continue  # ターン継続
                # ２）ジョーカーを含む複数枚プレイ時は、置換して number を作成
                if jokers:
                    if len(assigned_numbers) != len(jokers):
                        await websocket.send_json({
                            "type": "error",
                            "message": "ジョーカーの数字指定が不足しています。"
                        })
                        continue
                    if any(v == "inf" for v in assigned_numbers):
                        await websocket.send_json({
                            "type": "error",
                            "message": "複数枚出し時に「∞」指定はできません。"
                        })
                        continue
                    ranks = []
                    joker_i = 0
                    for c in played_cards:
                        if c["suit"] == "X":
                            val = assigned_numbers[joker_i]
                            joker_i += 1
                            ranks.append(str(val))
                        else:
                            ranks.append(str(c["rank"]))
                    ranks_str = "".join(ranks)

                    # 先頭が 0 の数字は許可しない
                    if ranks_str.startswith("0"):
                        await websocket.send_json({
                            "type": "error",
                            "message": "最上位桁が0の数字は出せません。"
                        })
                        continue

                    try:
                        number = int(ranks_str)
                    except ValueError:
                        number = -1
                else:
                    # 通常カードのみ
                    ranks_str = "".join(str(c["rank"]) for c in played_cards)
                    try:
                        number = int(ranks_str)
                    except ValueError:
                        number = -1

                # もしフィールドに既にカードが出ているなら、枚数と数の検証を行う
                if room.field:
                    # ① 枚数チェック
                    if len(played_cards) != len(room.field):
                        await websocket.send_json({"type": "error", "message": "枚数が違います。"})
                        continue

                    # ② 数値チェック：フィールドのカードと比較
                    field_number = room.last_number if room.last_number is not None else -1

                    # 通常は「>」が必要、反転中は「<」を要求
                    if not room.reverse_order:
                        if number <= field_number:
                            await websocket.send_json({"type": "error", "message": "場より大きい数字を出してください。"})
                            continue
                    else:
                        if number >= field_number:
                            await websocket.send_json({"type": "error", "message": "場より小さい数字を出してください。(ラマヌジャン革命中)"})
                            continue

                # グロタンカット
                if number == 57:
                    # 場をリセット
                    for c in played_cards:
                        player.remove_card(c)
                    room.field = []
                    # 自分の手番を継続するため next_turn は呼ばない
                    room.has_drawn = False
                    # クライアントの表示を更新
                    await player.send_hand_update()
                    await room.log_chat(f"{player.name}が57を出しました、グロタンカット！")
                    await room.update_game_state()
                    if await room.try_end_game():
                        await room.update_room_status()
                        continue
                    continue  # 次の処理（素数判定～next_turn）をすべてスキップ
                if number == 1729:
                    # フラグをトグル
                    room.reverse_order = not room.reverse_order
                    # カードを場に出す
                    for c in played_cards:
                        player.remove_card(c)
                    room.field = played_cards
                    room.last_number = number

                    # 手札更新 & ゲーム状態通知
                    await player.send_hand_update()
                    await room.update_game_state()
                    # ログ
                    await room.log_chat(f"{player.name}が1729を出しました、ラマヌジャン革命！")

                    # 通常の素数出しと同じく次のターンへ
                    await next_turn(room)
                    continue
                # 素数判定
                if not is_prime(number):
                    # ペナルティ
                    # 出そうとしたカードを引き直すことはしない(そもそも出されていないため)
                    if room.deck:
                        drawn = room.deck.pop(0)
                        player.add_card(drawn)

                    await player.send_hand_update()

                    # フィールドをリセット（場のカードを消す）2人対戦想定であることに注意
                    room.field = []
                    room.last_number = None

                    await room.update_game_state()
                    await room.broadcast( {
                        "type": "penalty",
                        "player_id": player.id,
                        "played_cards": played_cards,
                        "number": number
                    })

                    # チャットにペナルティのログを流す
                    await room.log_chat(f"{player.name}が{number}を出そうとしましたが、{number}は素数ではありません")

                    await next_turn(room)
                    continue

                # 素数なら場に出す
                for c in played_cards:
                    player.remove_card(c)
                room.field = played_cards
                room.last_number = number

                await player.send_hand_update()

                await room.update_game_state()
                await room.broadcast({
                    "type": "action_result",
                    "action": "play_card",
                    "player_id": player.id,
                    "played_cards": played_cards,
                    "number": number
                })

                # チャットに「素数を出した」ログを流す
                await room.log_chat(f"{player.name}が{number}を出しました")
                await next_turn(room)

            elif msg_type == "draw_card":
                if not player.room:
                    continue
                room = player.room
                if player.id != room.current_turn_id:
                    await websocket.send_json({"type": "error", "message": "あなたのターンではありません。"})
                    continue

                # すでにこのターンでドローしているかチェック
                if room.has_drawn == True:
                    await websocket.send_json({"type": "error", "message": "このターンはすでにドロー済みです。"})
                    continue

                # ドロー処理
                if len(room.deck) > 0:
                    drawn = room.deck.pop(0)
                    player.add_card(drawn)

                    # 自分に手札更新を送る
                    await player.send_hand_update()
                    await room.update_game_state()
                    # ドロー済みフラグを設定（このターンはこれ以上ドローできない）
                    room.has_drawn = True
                    # ※ここでは next_turn(room) は呼ばない → 手番は変わらない

            elif msg_type == "pass":
                if not player.room:
                    continue
                room = player.room
                if player.id != room.current_turn_id:
                    await websocket.send_json({"type": "error", "message": "あなたのターンではありません。"})
                    continue

                await player.send_hand_update()

                # パスの場合もフィールドをリセット
                room.field = []
                room.last_number = None

                # passの通知
                await room.update_game_state()
                await room.broadcast({
                    "type": "action_result",
                    "action": "pass",
                    "player_id": player.id
                })
                # チャットにパスのログを流す
                await room.log_chat(f"{player.name}がパスしました")
                # 次のターンへ
                await next_turn(room)

            elif msg_type == "chat":
                if not player.room:
                    continue
                # 表示用に「プレイヤー」を追加
                display_sender = f"{player.name}"
                await room.broadcast({
                    "type": "chat",
                    "sender": display_sender,
                    "message": data["message"]
                })

    except WebSocketDisconnect:
        await leave_room(player)

################################################
# 部屋からの退出
################################################
async def leave_room(player):
    # デバッグ用にplayer.roomの状態を出力
    print(f"DEBUG: player.room before leave_room: {player.room}")

    # もしNoneの場合は注意喚起のログも出す
    if player.room is None:
        print("DEBUG: player.room is None; cannot proceed with leave_room processing")

    room_id = player.room.room_id
    if room_id and player in rooms[room_id].players:
        room = player.room
        rooms[room_id].players.remove(player)
        player.room = None

        # 退出通知
        await room.log_chat(f"{player.name}が退室しました")
        # ゲーム中の特別処理
        if room.state == "playing":
            # 現在ターンのプレイヤーが切断した場合、次のターンに進める
            if room.current_turn_id == player.id:
                await next_turn(room)
            # 参加プレイヤーが1人だけになったら、ゲーム終了
            if len(room.players) == 1:
                winner_name = room.players[0].name
                await room.broadcast({"type": "game_over", "winner": winner_name})
                await room.log_chat(f"{winner_name}が勝利しました")
                room.state = "waiting"


        # 改めて抜けたプレイヤーには各roomの人数を送る機能を追加
        await room.update_room_status()


################################################
# ゲーム開始処理
################################################
async def start_game(room):
    room.reverse_order = False     # 革命向きは通常に戻す
    room.has_drawn = False         # ドロー済みフラグもクリア
    # 1) デッキを生成＆シャッフル＆配布
    deck = generate_deck()
    hand1, hand2, remaining_deck = shuffle_and_deal(deck)

    # 対戦待ちのプレイヤーのみから2人選ぶ
    waiting_players = [p for p in room.players if p.status == "waiting"]
    if len(waiting_players) < 2:
        # ここは通常、start_gameを呼ぶ前にチェックしているので安全策ですが
        return

    # 対戦待ちからランダムに2人選択
    p1, p2 = random.sample(waiting_players, 2)

    room.deck = remaining_deck
    p1.hand = hand1
    p2.hand = hand2
    room.field = []  # 場のカードは空
    room.last_number = None

    p1.sort_hand()
    p2.sort_hand()

    room.state = "playing"

    # ランダムに先攻プレイヤー決定
    room.current_turn_id = random.choice([p1.id, p2.id])

    # プレイヤーそれぞれに手札情報を送信
    await p1.ws.send_json({
        "type": "deal",
        "your_hand": p1.hand
    })
    await p2.ws.send_json({
        "type": "deal",
        "your_hand": p2.hand
    })

    # 全体にゲーム開始 & 現在のターン情報
    await room.broadcast({
        "type": "game_start",

    })
    await room.update_game_state()
    # チャットにログを流す
    await room.log_chat("ゲーム開始！")


################################################
# 次のターンに移る
################################################
async def next_turn(room):
    # ターンが変わるので、ドロー済みフラグをリセットする
    room.has_drawn = False

    # 対戦に参加している（statusが"waiting"の）プレイヤーだけを対象とする
    active_players = [p for p in room.players if p.status == "waiting"]
    if len(active_players) < 2:
        return

    if await room.try_end_game():
        await room.update_room_status()
        return

    current_turn_id = room.current_turn_id
    # 現在の手番プレイヤーが active_players の中にいるかを確認
    idx = [i for i, p in enumerate(active_players) if p.id == current_turn_id]
    if not idx:
        # もし現在の手番プレイヤーが active でなければ、先頭のプレイヤーに設定
        room.current_turn_id = active_players[0].id
    else:
        # 元の順番を無視しているようだが2人対戦の間は大丈夫か？
        current_idx = idx[0]
        next_idx = (current_idx + 1) % len(active_players)
        room.current_turn_id = active_players[next_idx].id

    # await room.update_game_state() それぞれのアクションで既に呼び出されているので省略
    # 次のプレイヤー名を取得して送信
    next_player = next((p for p in room.players if p.id == room.current_turn_id), None)
    await room.broadcast({
        "type": "next_turn",
        "current_turn": next_player.name if next_player else None,
    })
