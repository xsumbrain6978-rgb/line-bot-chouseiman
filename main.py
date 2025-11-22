import os
import json
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai

app = Flask(__name__)

# 環境変数から取得
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Gemini API設定
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash-exp")

# 会話履歴を保存するファイル
HISTORY_FILE = "conversation_history.json"
MAX_HISTORY_DAYS = 180  # 半年間

# 会話履歴をファイルから読み込む
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

# 会話履歴をファイルに保存
def save_history(history):
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

# 古い履歴を削除
def clean_old_history(history, group_id):
    if group_id not in history:
        return history
    
    cutoff_date = datetime.now() - timedelta(days=MAX_HISTORY_DAYS)
    history[group_id] = [
        msg for msg in history[group_id]
        if datetime.fromisoformat(msg['timestamp']) > cutoff_date
    ]
    return history

# グローバル変数として履歴を読み込む
conversation_history = load_history()

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    global conversation_history
    
    user_message = event.message.text
    group_id = event.source.group_id if hasattr(event.source, 'group_id') else event.source.user_id
    
    # ユーザー名を取得
    try:
        if hasattr(event.source, 'group_id'):
            profile = line_bot_api.get_group_member_profile(group_id, event.source.user_id)
        else:
            profile = line_bot_api.get_profile(event.source.user_id)
        user_name = profile.display_name
    except:
        user_name = "不明"
    
    # 会話履歴を初期化（グループごと）
    if group_id not in conversation_history:
        conversation_history[group_id] = []
    
    # ユーザーのメッセージを履歴に追加（日時、ユーザー名、メッセージ）
    conversation_history[group_id].append({
        'timestamp': datetime.now().isoformat(),
        'user': user_name,
        'message': user_message
    })
    
    # 古い履歴を削除（半年以上前）
    conversation_history = clean_old_history(conversation_history, group_id)
    
    # ファイルに保存
    save_history(conversation_history)
    
    # @調整マン が含まれている場合のみ反応
    if "@調整マン" in user_message:
        # @調整マンを削除
        query = user_message.replace("@調整マン", "").strip()
        
        # 会話履歴を整形（最新100件）
        recent_history = conversation_history[group_id][-100:]
        history_text = ""
        for msg in recent_history:
            timestamp = datetime.fromisoformat(msg['timestamp'])
            date_str = timestamp.strftime('%Y年%m月%d日 %H:%M')
            history_text += f"[{date_str}] {msg['user']}: {msg['message']}\n"
        
        # Geminiに送るプロンプト
        prompt = f"""
あなたは「調整マン」という名前の、家族のLINEグループのアシスタントです。
以下の会話履歴を参考にして、ユーザーの質問に答えてください。

【会話履歴（直近100件、最大半年間）】
{history_text}

【ユーザーの質問】
{query}

【返答のルール】
- フレンドリーで親しみやすい口調で話してください
- 会話履歴から関連する情報を探して、具体的に答えてください
- 「いつ」「誰が」「何を」言ったかを明確に伝えてください
- 予定やイベントについて聞かれた場合は、日付と内容を整理して答えてください
- 絵文字を適度に使ってください😊
- 履歴に情報がない場合は、正直に「わからない」と答えてください

【回答例】
- 「○月○日に、○○さんが『△△に行く』って言ってたよ！」
- 「最近の予定をまとめると...」
- 「ごめん、そのことについては会話に出てないみたい...」
"""
        
        try:
            # Gemini APIで返答を生成
            response = model.generate_content(prompt)
            reply_text = response.text
            
            # 調整マンの返答を履歴に追加
            conversation_history[group_id].append({
                'timestamp': datetime.now().isoformat(),
                'user': '調整マン',
                'message': reply_text
            })
            
            # ファイルに保存
            save_history(conversation_history)
            
        except Exception as e:
            reply_text = f"ごめん、エラーが出ちゃった...😅\nエラー: {str(e)}"
        
        # LINEに返信
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )

@app.route("/")
def health_check():
    return "調整マン is running! 🤖"

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
