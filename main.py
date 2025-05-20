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

class Room:
    def __init__(self, room_id: str):
        self.room_id = room_id
        self.players = []    # Playerオブジェクトのリスト
        self.state = "waiting"
        self.deck = []
        self.field = []      # 場に出ているカード
        self.current_turn_id = None
        self.has_drawn = False

    async def broadcast(self, message: dict):
        for p in self.players:
            await p.send_json(message)

    async def update_room_status(self):
        message = {
            "type": "update_room_status",
            "room_id": self.room_id,
            "count": len(self.players),
            "player_list": [{"id": p.id, "status": p.status} for p in self.players],
            "waiting_count": len([p for p in self.players if p.status == "waiting"])
        }
        await self.broadcast(message)

    async def log_chat(self, message: str, sender="system"):
        await self.broadcast({"type": "chat", "sender": sender, "message": message})

    # その他、ルームに関連するロジック（プレイヤー追加、削除、ゲーム開始、次のターンなど）をメソッドとして実装
    async def update_game_state(self):
        state_msg = {
            "type": "game_update",
            "room_id": self.room_id,
            "state": self.state,
            "current_turn": self.current_turn_id,
            "deck_count": len(self.deck),
            "field": self.field,
            "player_list": [{"id": p.id, "status": p.status} for p in self.players],
            "hand_counts": [{"id": p.id, "count": len(p.hand)} for p in self.players]
        }
        await self.broadcast(state_msg)


# アプリケーションの初期化時にRoomインスタンスを必要な数だけ作成しておく
rooms: Dict[str, Room] = {f"room_{i}": Room(f"room_{i}") for i in range(1, 6)}

class Player:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.id = id(ws)  # 現状は接続オブジェクトのidを利用
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
    current_turn_id = room.current_turn_id
    if current_turn_id is None:
        return None
    # room.playersはPlayerオブジェクトのリストであるとする
    current_player = next((p for p in room.players if p.id == current_turn_id), None)
    if current_player is not None:
        if len(current_player.hand) == 0:
            # 勝利者のIDまたはPlayerオブジェクトそのものを返す（要件に応じて）
            return current_player.id
    return None

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

            if msg_type == "get_room_counts":
                counts = {room_id: len(room.players) for room_id, room in rooms.items()}
                await websocket.send_json({"type": "room_counts", "counts": counts})

            elif msg_type == "join_room":
                rid = data["room_id"]
                room = rooms[rid]

                if len(room.players) >= 10:
                    await websocket.send_json({"type": "error", "message": "部屋が満員です。"})
                    continue

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
                ranks_str = "".join(str(c["rank"]) for c in played_cards)
                try:
                    number = int(ranks_str)
                except ValueError:
                    number = -1

                # 手札にあるか検証
                if not player.has_cards(played_cards):
                    await player.ws.send_json({"type": "error", "message": "そのカードは手札にありません。"})
                    continue

                # もしフィールドに既にカードが出ているなら、枚数と数の検証を行う
                if room.field:
                    # ① 枚数チェック
                    if len(played_cards) != len(room.field):
                        await websocket.send_json({"type": "error", "message": "枚数が違います。"})
                        continue

                    # ② 数値チェック：フィールドのカードと比較
                    field_ranks_str = "".join(str(c["rank"]) for c in room.field)
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
                    if room.deck:
                        drawn = room.deck.pop(0)
                        player.add_card(drawn)

                    await player.send_hand_update()

                    # フィールドをリセット（場のカードを消す）2人対戦想定であることに注意
                    room.field = []

                    await room.update_game_state()
                    await room.broadcast( {
                        "type": "penalty",
                        "player_id": player.id,
                        "played_cards": played_cards,
                        "number": number
                    })

                    # チャットにペナルティのログを流す
                    await room.log_chat(f"プレイヤー{player.id}が{number}を出そうとしましたが、{number}は素数ではありません")

                    await next_turn(room)
                    continue

                # 素数なら場に出す
                for c in played_cards:
                    player.remove_card(c)
                room.field = played_cards

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
                await room.log_chat(f"プレイヤー{player.id}が{number}を出しました")
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

                # passの通知
                await room.update_game_state()
                await room.broadcast({
                    "type": "action_result",
                    "action": "pass",
                    "player_id": player.id
                })
                # チャットにパスのログを流す
                await room.log_chat(f"プレイヤー{player.id}がパスしました")
                # 次のターンへ
                await next_turn(room)

            elif msg_type == "chat":
                if not player.room:
                    continue
                await room.broadcast({
                    "type": "chat",
                    "sender": player.id,
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

        # ゲーム中の特別処理

        if room.state == "playing":
            # 現在ターンのプレイヤーが切断した場合、次のターンに進める
            if room.current_turn_id == player.id:
                await next_turn(room)

            # 参加プレイヤーが1人だけになったら、ゲーム終了
            if len(room.players) == 1:
                winner_id = room.players[0].id
                await room.broadcast({"type": "game_over", "winner": winner_id})
                room.state = "waiting"

        # 改めて抜けたプレイヤーには各roomの人数を送る機能を追加
        await room.update_room_status()


################################################
# ゲーム開始処理
################################################
async def start_game(room):
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

    await room.update_game_state()
    # 全体にゲーム開始メッセージ & 現在のターン情報
    await room.broadcast({
        "type": "game_start",
        "message": "ゲーム開始!",
        "current_turn": room.current_turn_id
    })

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

    # 勝利者がいるかチェック
    winner = check_win_condition(room)
    if winner:
        room.state = "waiting"
        # クライアントへ通知
        await room.broadcast({"type": "game_over", "winner": winner, "state": room.state})
        # チャットに勝利ログを流す
        await room.log_chat(f"プレイヤー{winner} が勝利しました。")
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
    await room.broadcast({
        "type": "next_turn",
        "current_turn": room.current_turn_id,
    })
