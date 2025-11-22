from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import os
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
model = genai.GenerativeModel('gemini-pro')

# 会話履歴を保存する辞書（グループIDごと）
conversation_history = {}

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
    group_id = event.source.group_id if hasattr(event.source, 'group_id') else event.source.user_id
    
    # 会話履歴を初期化（グループごと）
    if group_id not in conversation_history:
        conversation_history[group_id] = []
    
    # ユーザーのメッセージを履歴に追加
    conversation_history[group_id].append(f"ユーザー: {user_message}")
    
    # 履歴が長すぎる場合は古いものを削除（最新20件のみ保持）
    if len(conversation_history[group_id]) > 20:
        conversation_history[group_id] = conversation_history[group_id][-20:]
    
    # @調整マン が含まれている場合のみ反応
    if "@調整マン" in user_message:
        # @調整マンを削除
        user_message = user_message.replace("@調整マン", "").strip()
        
        # 会話履歴を文字列に変換
        history_text = "\n".join(conversation_history[group_id][-10:])  # 最新10件
        
        # Geminiに送るプロンプト
        prompt = f"""
あなたは「調整マン」という名前のLINEグループのアシスタントです。
以下の会話履歴を参考にして、ユーザーの質問に答えてください。

【会話履歴】
{history_text}

【ユーザーの質問】
{user_message}

【返答のルール】
- フレンドリーで親しみやすい口調で話してください
- 会話の流れを理解して、文脈に沿った返答をしてください
- 絵文字を適度に使ってください😊
- 短く、わかりやすく答えてください
"""
        
        try:
            # Gemini APIで返答を生成
            response = model.generate_content(prompt)
            reply_text = response.text
            
            # 調整マンの返答を履歴に追加
            conversation_history[group_id].append(f"調整マン: {reply_text}")
            
        except Exception as e:
            reply_text = f"ごめん、エラーが出ちゃった...😅\nエラー: {str(e)}"
        
        # LINEに返信
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
