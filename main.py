import os
import json
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

def generate_summary(messages):
    """会話をまとめる"""
    if not messages:
        return "最近の会話がないよ〜 😅"
    
    messages_text = format_messages_for_ai(messages)
    prompt = f"""
{SYSTEM_PROMPT}

以下は家族のLINEグループの会話履歴です。
重要な情報を以下の形式でまとめてください：

【会話履歴】
{messages_text}

【まとめ方】
- 予定・スケジュールを日時順に整理
- TODO・お願い事を箇条書き
- 重要な決定事項をピックアップ
- 日常会話は簡潔に要約

フランクで親しみやすい口調でまとめてね！
"""
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"まとめ中にエラーが起きちゃった... 😅\nエラー: {str(e)}"

def search_conversation(keyword):
    """過去の会話を検索"""
    results = []
    for msg in conversation_history:
        if keyword.lower() in msg['message'].lower():
            results.append(msg)
    
    if not results:
        return f"「{keyword}」に関する会話は見つからなかったよ〜 😅"
    
    # 新しい順にソート
    results.sort(key=lambda x: x['timestamp'], reverse=True)
    
    # 結果をフォーマット
    output = f"📌 「{keyword}」に関する会話を見つけたよ！\n\n"
    for msg in results[:10]:  # 最大10件
        timestamp = datetime.fromisoformat(msg['timestamp'])
        output += f"• {timestamp.strftime('%Y年%m月%d日 %H:%M')}\n"
        output += f"  {msg['user']}: {msg['message']}\n\n"
    
    if len(results) > 10:
        output += f"他にも{len(results) - 10}件見つかったよ！"
    
    return output

def get_today_schedule():
    """今日の予定を抽出"""
    today = datetime.now().date()
    today_messages = [
        msg for msg in conversation_history
        if datetime.fromisoformat(msg['timestamp']).date() == today
    ]
    
    if not today_messages:
        return "今日は特に予定の話は出てないよ〜 😊"
    
    messages_text = format_messages_for_ai(today_messages)
    prompt = f"""
{SYSTEM_PROMPT}

以下は今日の会話履歴です。
今日の予定・スケジュールを抽出して教えてください。

【会話履歴】
{messages_text}

【回答フォーマット】
📅 今日の予定だよ！

- 時間: ○時 / 誰: ○○さん / 予定: ○○

予定がない場合は「今日は特に予定の話は出てないよ〜」って答えてね。
"""
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"予定を確認中にエラーが起きちゃった... 😅"

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
    
    # ユーザー名を取得（実際のLINE表示名を取得）
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
        
        if 'まとめ' in command or 'まとめて' in command:
            # 最近24時間の会話をまとめる
            recent = get_recent_messages(hours=24)
            reply = generate_summary(recent)
        
        elif '予定' in command or 'スケジュール' in command:
            # 今日の予定を表示
            reply = get_today_schedule()
        
        elif 'いつ' in command or '検索' in command:
            # キーワードを抽出して検索
            keyword = command.replace('いつ', '').replace('検索', '').replace('?', '').replace('？', '').strip()
            if keyword:
                reply = search_conversation(keyword)
            else:
                reply = "何を検索したいか教えてね！\n例: @調整マン 旅行の話いつだっけ？"
        
        else:
            # その他の質問はGeminiに投げる
            prompt = f"{SYSTEM_PROMPT}\n\n質問: {command}"
            try:
                response = model.generate_content(prompt)
                reply = response.text
            except Exception as e:
                reply = "ごめん、ちょっとわからなかった... 😅"
        
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
