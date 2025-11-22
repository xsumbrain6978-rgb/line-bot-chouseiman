import os
import json
from datetime import datetime, timedelta

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

import google.generativeai as genai

app = Flask(__name__)

# ========= 環境変数 =========
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET が環境変数に設定されていません。")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY が環境変数に設定されていません。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ========= Gemini設定 =========
genai.configure(api_key=GEMINI_API_KEY)
# ここは環境に合わせて必要なら変えてOK
model = genai.GenerativeModel("gemini-2.0-flash")

# ========= 履歴管理 =========
HISTORY_FILE = "conversation_history.json"
MAX_HISTORY_DAYS = 180              # 半年
MAX_MESSAGES_PER_GROUP = 5000       # グループごとの最大保存件数（保険）
MAX_MESSAGES_FOR_PROMPT = 300       # Gemini に渡す最大件数
MAX_REPLY_LENGTH = 3500             # LINEメッセージ長の安全ライン


def load_history() -> dict:
    """JSONファイルから履歴を読み込み"""
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        # 壊れていたら作り直す
        pass
    return {}


def save_history(history: dict) -> None:
    """履歴をJSONに保存（途中で壊れないように一時ファイル経由）"""
    tmp_file = HISTORY_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, HISTORY_FILE)


def clean_old_history(history: dict, group_id: str) -> None:
    """半年より古い履歴と、件数オーバーの古い分を削除"""
    msgs = history.get(group_id, [])
    if not msgs:
        return

    cutoff = datetime.now() - timedelta(days=MAX_HISTORY_DAYS)
    new_msgs = []
    for msg in msgs:
        ts = msg.get("timestamp")
        try:
            dt = datetime.fromisoformat(ts) if ts else None
        except Exception:
            dt = None
        # 日付がパースできないものは念のため残しておく
        if dt is None or dt >= cutoff:
            new_msgs.append(msg)

    # 件数が多すぎるときは新しい方だけ残す
    if len(new_msgs) > MAX_MESSAGES_PER_GROUP:
        new_msgs = new_msgs[-MAX_MESSAGES_PER_GROUP:]

    history[group_id] = new_msgs


# メモリ上にもロードしておく
conversation_history = load_history()


# ========= LINEハンドラ =========
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    global conversation_history

    text = event.message.text
    source = event.source

    # group_id / room_id / user_id のどれかでスレッドを識別
    group_id = getattr(source, "group_id", None) or getattr(source, "room_id", None) or source.user_id

    # 発言者の名前を取得
    user_name = "不明"
    try:
        if getattr(source, "type", "") == "group" and getattr(source, "user_id", None):
            profile = line_bot_api.get_group_member_profile(group_id, source.user_id)
            user_name = profile.display_name
        elif getattr(source, "user_id", None):
            profile = line_bot_api.get_profile(source.user_id)
            user_name = profile.display_name
    except LineBotApiError:
        # 取れなくても致命的ではないので無視
        pass

    # 履歴に追記
    conversation_history.setdefault(group_id, [])
    conversation_history[group_id].append(
        {
            "timestamp": datetime.now().isoformat(),
            "user": user_name,
            "message": text,
        }
    )
    clean_old_history(conversation_history, group_id)
    save_history(conversation_history)

    # メンションされていないときは記録だけして終了
    if "@調整マン" not in text:
        return

    # メンションを取り除いた部分がユーザーの「質問・依頼」
    query = text.replace("@調整マン", "").strip()

    # このグループの履歴から、新しい方 MAX_MESSAGES_FOR_PROMPT 件だけをGeminiに渡す
    msgs = conversation_history.get(group_id, [])[-MAX_MESSAGES_FOR_PROMPT:]

    history_lines = []
    for msg in msgs:
        try:
            ts = datetime.fromisoformat(msg["timestamp"])
            ts_str = ts.strftime("%Y年%m月%d日 %H:%M")
        except Exception:
            ts_str = msg.get("timestamp", "")
        history_lines.append(
            f"[{ts_str}] {msg.get('user', '不明')}: {msg.get('message', '')}"
        )
    history_text = "\n".join(history_lines)

    # Gemini へのプロンプト
    prompt = f"""
あなたは「調整マン」という名前の、家族のLINEグループ専属アシスタントです。
以下はこのグループの過去の会話履歴です（最大半年分・新しい方から最大{MAX_MESSAGES_FOR_PROMPT}件）。

【会話履歴】
{history_text}

ユーザーからの依頼は次のとおりです。

【ユーザーからの依頼・質問】
{query}

# 返答ルール
- 会話履歴の中から「いつ・誰が・何を言ったか／どこへ行くと言っていたか」をできるだけ正確に探します。
- 日付が分かる場合は「YYYY年MM月DD日」「○月○日」の形で、誰が何と言ったかを具体的に書きます。
- 予定（外出・イベント・旅行など）について聞かれた場合は、日付順に整理して一覧にします。
- 履歴に無い情報はでっち上げず、「その情報は履歴には出てきていないみたい」と正直に伝えます。
- 口調はフレンドリーで親しみやすく、絵文字も適度に使ってください😊
- 必要な情報は落とさず、なるべく簡潔にまとめて答えてください。
"""

    try:
        response = model.generate_content(prompt)
        reply_text = getattr(response, "text", None) or "ごめん、うまく答えを作れなかったみたい…😅"
    except Exception as e:
        reply_text = f"ごめん、Geminiでエラーが出ちゃった…😅\n{e}"

    # 長すぎるとLINE側で怒られるのでカット
    if len(reply_text) > MAX_REPLY_LENGTH:
        reply_text = reply_text[:MAX_REPLY_LENGTH]

    # 調整マンの返答も履歴として残す
    conversation_history[group_id].append(
        {
            "timestamp": datetime.now().isoformat(),
            "user": "調整マン",
            "message": reply_text,
        }
    )
    clean_old_history(conversation_history, group_id)
    save_history(conversation_history)

    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text),
        )
    except LineBotApiError:
        # ここで投げるとWebhookが500になるので握りつぶす
        pass


@app.route("/")
def health_check():
    return "調整マン is running! 🤖"


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
