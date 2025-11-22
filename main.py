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

# ========= Gemini 設定 =========
genai.configure(api_key=GEMINI_API_KEY)
# 必要に応じてモデル名は環境に合わせて変更してよい
model = genai.GenerativeModel("gemini-2.0-flash")

# ========= 履歴管理 =========
HISTORY_FILE = "conversation_history.json"
MAX_HISTORY_DAYS = 180          # 半年間保持
MAX_HISTORY_PER_GROUP = 5000    # 1グループあたり最大件数（それ以上は古いものから削る）
MAX_PROMPT_MESSAGES = 600       # Gemini に渡す最大件数（多めにして時系列の変化も見られるように）
MAX_REPLY_LENGTH = 3500         # LINEに返す文字数の上限（安全ライン）


def load_history() -> dict:
    """会話履歴をJSONファイルから読み込む。"""
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        # 壊れていたら作り直し
        pass
    return {}


def save_history(history: dict) -> None:
    """会話履歴をJSONファイルに保存（一時ファイル経由で安全に）。"""
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


# メモリ上の履歴
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
        # 取れなくても致命的ではないので無視
        pass

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

    # 「@調整マン」が含まれていないメッセージは記録だけして返信しない
    if "@調整マン" not in text:
        return

    # メンションを除いた部分が実際の依頼
    query = text.replace("@調整マン", "").strip()

    # このグループの履歴（新しい方から MAX_PROMPT_MESSAGES 件）
    msgs = conversation_history.get(group_id, [])[-MAX_PROMPT_MESSAGES:]

    # 今日の日付
    now = datetime.now()
    today_date = now.date()
    today_str = now.strftime("%Y年%m月%d日")

    # 全体履歴テキスト & 今日分の履歴テキストを作る
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
    today_history_text = "\n".join(today_lines) if today_lines else "（今日はまだ会話が少ない、もしくは保存されていません）"

    # ========= Gemini へのプロンプト =========
    prompt = f"""
あなたは「調整マン」という名前の、家族のLINEグループ専属アシスタントです。
今日は {today_str} です。

あなたには次の2つの顔があります。
1. 予定やタスクを整理してくれる「調整役」
2. 会話全体を俯瞰して、人の状態や変化を見守る「ゆるいメンター」

以下の情報を渡します。

【会話履歴（最大半年分、新しい方から最大 {MAX_PROMPT_MESSAGES} 件）】
{history_text}

【今日 {today_str} の会話だけを抜き出した履歴】
{today_history_text}

【ユーザーからの依頼・質問】
{query}

# あなたの振る舞いのルール

まず、ユーザーの依頼がだいたい次のどのタイプに近いかを考えてください。

A. 今日や特定の日付の「予定・出来事」を整理してほしい  
B. 誰かの「状態・悩み・考え方」を読み取ってコメントしてほしい  
C. 過去と今を比べて「変化」や「一貫している点」を指摘してほしい  
D. 上のどれとも言い切れない／複合している（この場合は予定と心の状態の両方を軽く触れる）

---

## A. 予定・出来事を整理してほしい場合

- 今日、もしくは質問文や会話から読み取れる日付の発言を探し、
  「いつ・誰が・どこで・何をする／した」を抜き出してください。
- 人ごと・時系列に整理し、箇条書きでまとめてください。
- 例：
  - 理貴：10:00 に◯◯へ、15:00 に△△の打ち合わせ
  - ○○：午前中は在宅、夕方スーパーへ … など

## B. 状態・悩み・考え方を読み取る場合

- 履歴全体をざっと眺め、各メンバーについて
  - どんなテーマの発言が多いか
  - どんなことで悩んでいそうか
  - どんな価値観や口ぐせがありそうか
  を「事実 → そこから推測される状態」という順番で書いてください。
- 決めつけにならないように、「〜かもしれない」「〜と感じていそう」などの表現を使ってください。
- 最後に、メンターとして、
  - その人の良さや頑張りを認める一言
  - 無理ない範囲での小さな提案（1〜3個）
  を、やさしく・共感的なトーンで添えてください。

## C. 過去との比較・変化を見てほしい場合

- できる範囲で「古い発言」と「最近の発言」を比べ、
  - 変わってきた点（例：前は〜と言っていたが、最近は〜と言うようになった）
  - 一貫している点（例：ずっと〜を大事にしている）
  を人ごとに整理してください。
- 変化や一貫性がポジティブに見えるところは、ちゃんと理由を添えてほめてください。
- 「前と言っていることが変わってきたね」「この点はずっとブレていないね」など、
  成長や継続をフィードバックするイメージです。

## D. よくわからない / 複合パターン

- 予定の整理が必要そうなら、簡潔に予定をまとめる。
- そのうえで、最近の会話から読み取れる「全体の雰囲気」「それぞれの頑張り」などを、
  一言メンター目線でコメントしてください。

---

## 共通ルール

- 事務的な要約だけでなく、必ず
  - 気持ちに寄り添うひと言
  - 継続して会話を見ているからこそ言えるコメント
  を入れてください。
- 会話に出ていないことは勝手に作らず、
  「履歴からわかる範囲で話すね」と前置きしてから説明してください。
- 日本語で、親しみやすい口調で書いてください。絵文字も適度に使ってOKです😊
- 長くなりすぎないように、読みやすい段落・箇条書きでまとめてください。
"""

    # ========= Gemini で回答生成 =========
    try:
        response = model.generate_content(prompt)
        reply_text = getattr(response, "text", "") or "ごめん、うまく答えを作れなかったみたい…😅"
    except Exception as e:
        reply_text = f"ごめん、Geminiでエラーが出ちゃった…😅\n{e}"

    # LINEの制限対策で長すぎる場合はカット
    if len(reply_text) > MAX_REPLY_LENGTH:
        reply_text = reply_text[:MAX_REPLY_LENGTH]

    # 調整マン自身のメッセージも履歴に追加
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
        # ここで落とすとWebhookが500になるので握りつぶす
        pass


@app.route("/")
def health_check():
    return "調整マン is running! 🤖"


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
