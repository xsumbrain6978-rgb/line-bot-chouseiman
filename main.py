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
    raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET が設定されていません。")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY が設定されていません。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ========= Gemini設定 =========
genai.configure(api_key=GEMINI_API_KEY)
# 環境に合わせて必要ならモデル名は変えてOK（最新の flash 系を推奨）
model = genai.GenerativeModel("gemini-2.0-flash")

# ========= 履歴管理 =========
HISTORY_FILE = "conversation_history.json"
MAX_HISTORY_DAYS = 180          # 半年間保持
MAX_HISTORY_PER_GROUP = 5000    # 1グループあたりの最大保存件数（古い順に削除）
MAX_PROMPT_MESSAGES = 400       # Gemini に渡す最大件数
MAX_REPLY_LENGTH = 3500         # LINEに返す文字数の上限（安全ライン）


def load_history() -> dict:
    """JSONファイルから全グループの会話履歴を読み込む。"""
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        # ファイルが壊れていたら作り直す
        pass
    return {}


def save_history(history: dict) -> None:
    """会話履歴をJSONに保存（一時ファイル経由で安全に）。"""
    tmp_file = HISTORY_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, HISTORY_FILE)


def clean_old_history(history: dict, group_id: str) -> None:
    """半年より古い履歴や、件数オーバー分を削除する。"""
    msgs = history.get(group_id, [])
    if not msgs:
        return

    cutoff = datetime.now() - timedelta(days=MAX_HISTORY_DAYS)
    filtered = []
    for msg in msgs:
        ts = msg.get("timestamp")
        try:
            dt = datetime.fromisoformat(ts) if ts else None
        except Exception:
            dt = None
        # 日付が読めないものは念のため残す
        if dt is None or dt >= cutoff:
            filtered.append(msg)

    # 件数が多すぎたら新しい方だけ残す
    if len(filtered) > MAX_HISTORY_PER_GROUP:
        filtered = filtered[-MAX_HISTORY_PER_GROUP:]

    history[group_id] = filtered


# メモリ上に読み込み
conversation_history = load_history()


# ========= LINE Webhook =========
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

    # group / room / user のいずれかでスレッドを識別
    group_id = getattr(source, "group_id", None) or getattr(source, "room_id", None) or source.user_id

    # ユーザー名の取得
    user_name = "不明"
    try:
        if getattr(source, "type", "") == "group" and getattr(source, "user_id", None):
            profile = line_bot_api.get_group_member_profile(group_id, source.user_id)
            user_name = profile.display_name
        elif getattr(source, "user_id", None):
            profile = line_bot_api.get_profile(source.user_id)
            user_name = profile.display_name
    except LineBotApiError:
        pass  # 取れなくても致命的ではないので無視

    # 履歴に追加
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

    # 「@調整マン」が含まれていないメッセージは、記録だけして返事しない
    if "@調整マン" not in text:
        return

    # メンションを除いた部分が質問
    query = text.replace("@調整マン", "").strip()

    # このグループの履歴を取得（新しい方から MAX_PROMPT_MESSAGES 件）
    msgs = conversation_history.get(group_id, [])[-MAX_PROMPT_MESSAGES:]

    # 今日の日付（サーバー時間ベース。必要なら +9 時間の補正を入れてもOK）
    now = datetime.now()
    today_date = now.date()
    today_str = now.strftime("%Y年%m月%d日")

    history_lines = []
    today_lines = []

    for msg in msgs:
        raw_ts = msg.get("timestamp")
        try:
            dt = datetime.fromisoformat(raw_ts) if raw_ts else None
        except Exception:
            dt = None

        if dt is not None:
            ts_str = dt.strftime("%Y年%m月%d日 %H:%M")
            if dt.date() == today_date:
                today_lines.append(f"[{ts_str}] {msg.get('user', '不明')}: {msg.get('message', '')}")
        else:
            ts_str = raw_ts or ""

        history_lines.append(f"[{ts_str}] {msg.get('user', '不明')}: {msg.get('message', '')}")

    history_text = "\n".join(history_lines)
    today_history_text = "\n".join(today_lines) if today_lines else "（今日はまだ予定っぽい発言が見つかっていません）"

    # ========= Gemini へのプロンプト =========
    prompt = f"""
あなたは「調整マン」という名前の、家族のLINEグループ専属アシスタントです。
今日は {today_str} です。

下に、このグループの会話履歴（最大半年分のうち新しい方から最大 {MAX_PROMPT_MESSAGES} 件）を渡します。

【全体の会話履歴】
{history_text}

そのうち、今日 {today_str} の会話だけを抜き出したものがこちらです。

【今日の会話だけの履歴】
{today_history_text}

ユーザーからの依頼・質問は次のとおりです。

【ユーザーからの依頼・質問】
{query}

# あなたのタスク

1. まず今日の日付 ({today_str}) に関する予定・外出・イベントの発言を、上の「今日の会話だけの履歴」から探してください。
   - 例：「○時に〜へ行く」「午後から〜」「今日は〜に行く予定」など。
2. 今日の予定に関する情報が見つかったら、次のフォーマットで、**人ごとに時系列で**整理して答えてください。

【今日のみんなの予定（例）】
- 理貴：10:00 に◯◯へ／15:00 に△△へ
- ○○：午前中は在宅、夕方にスーパーへ
- 情報がない人：×× など

3. ユーザーが「今日」以外の日付（例：「11月25日の予定」「5月3日に誰がどこ行くと言ってた？」）を聞いている場合は、
   会話履歴全体からその日付に近いメッセージを探し、同じように
   「いつ・誰が・どこへ・何をする予定と言っていたか」を整理して答えてください。
4. 会話履歴にその情報が存在しない場合は、でっち上げずに
   「その日付の予定については会話に出ていないみたい」などと正直に伝えてください。
5. 口調はフレンドリーで親しみやすく、絵文字も適度に使ってください😊
6. 情報量は多すぎず少なすぎず、一覧で一目でわかるようにまとめてください。
"""

    # ========= Gemini で回答生成 =========
    try:
        response = model.generate_content(prompt)
        reply_text = getattr(response, "text", "") or "ごめん、うまく答えを作れなかったみたい…😅"
    except Exception as e:
        reply_text = f"ごめん、Geminiでエラーが出ちゃった…😅\n{e}"

    if len(reply_text) > MAX_REPLY_LENGTH:
        reply_text = reply_text[:MAX_REPLY_LENGTH]

    # 調整マン自身の返答も履歴に残しておく
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
        # ここで落とすとWebhook全体が500になるので握りつぶす
        pass


@app.route("/")
def health_check():
    return "調整マン is running! 🤖"


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
