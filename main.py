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
            filtered.appen
