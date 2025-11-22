import os
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai

app = Flask(__name__)

# 環境変数から設定を取得
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)

# 会話履歴を保存（メモリ内、1年分）
conversation_history = []
MAX_HISTORY_DAYS = 365

# Geminiモデルを設定
model = genai.GenerativeModel('gemini-1.5-flash')

# システムプロンプト
SYSTEM_PROMPT = """
あなたは家族3人（67歳父、62歳母、32歳息子まさき）のLINEグループのアシスタントAI「調整マン」です。
フランクで親しみやすく、家族の一員として振る舞ってください。

【あなたの役割】
1. 家族の会話から重要な情報を抽出・整理する
2. 予定やTODOを見逃さずキャッチする
3. 過去の会話を検索して「いつ話したか」を教える
4. 必要に応じて会話をわかりやすくまとめる

【対応する情報の優先順位】
最優先: 
- 予定・スケジュール（病院、外出、イベントなど）
- TODO・お願い事（買い物、用事など）
- 重要な決定事項（家族で決めたこと）

重要:
- 健康・体調に関する話題
- お金に関する話題

【会話のルール】
- 敬語は使わず、親しみやすい口調で話す
- 絵文字を適度に使う（😊👍📅など）
- 簡潔でわかりやすく答える
- 高齢の両親にも理解しやすい表現を使う

【まとめる時のフォーマット】
📅 **予定・スケジュール**
（箇条書き）

✅ **TODO・やること**
（箇条書き）

💡 **決まったこと**
（箇条書き）

💬 **その他の話題**
（簡潔に）
"""

def save_message(user_name, message_text):
    """メッセージを履歴に保存"""
    global conversation_history
    
    conversation_history.append({
        'timestamp': datetime.now().isoformat(),
        'user': user_name,
        'message': message_text
    })
    
    # 1年以上前のメッセージを削除
    cutoff_date = datetime.now() - timedelta(days=MAX_HISTORY_DAYS)
    conversation_history = [
        msg for msg in conversation_history 
        if datetime.fromisoformat(msg['timestamp']) > cutoff_date
    ]

def get_recent_messages(hours=24):
    """最近のメッセージを取得"""
    cutoff_time = datetime.now() - timedelta(hours=hours)
    recent = [
        msg for msg in conversation_history
        if datetime.fromisoformat(msg['timestamp']) > cutoff_time
    ]
    return recent

def format_messages_for_ai(messages):
    """メッセージをAI用にフォーマット"""
    formatted = ""
    for msg in messages:
        timestamp = datetime.fromisoformat(msg['timestamp'])
        formatted += f"[{timestamp.strftime('%Y-%m-%d %H:%M')}] {msg['user']}: {msg['message']}\n"
    return formatted

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
    user_message = event.message.text
    
    # ユーザー名を取得
    try:
        profile = line_bot_api.get_profile(event.source.user_id)
        user_name = profile.display_name
    except:
        user_name = "不明"
    
    # メッセージを保存
    save_message(user_name, user_message)
    
    # @調整マン で呼ばれた場合のみ反応
    if user_message.startswith('@調整マン'):
        command = user_message.replace('@調整マン', '').strip()
        
        # デフォルトの返信
        reply = f"呼んだ？何か手伝えることある？😊\n\n使い方：\n・@調整マン まとめ → 最近の会話をまとめるよ\n・@調整マン 予定 → 今日の予定を教えるよ"
        
        if 'まとめ' in command:
            recent = get_recent_messages(hours=24)
            if recent:
                messages_text = format_messages_for_ai(recent)
                prompt = f"{SYSTEM_PROMPT}\n\n以下の会話をまとめてください：\n{messages_text}"
                try:
                    response = model.generate_content(prompt)
                    reply = response.text
                except:
                    reply = "ごめん、まとめ中にエラーが出ちゃった... 😅"
            else:
                reply = "最近の会話がないよ〜 😅"
        
        elif '予定' in command:
            reply = "予定の機能は開発中だよ！もう少し待ってね 😊"
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )

@app.route("/")
def health_check():
    return "調整マン is running! 🤖"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
