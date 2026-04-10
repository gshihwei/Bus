import os
import re
import requests
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from tdx_client import TDXClient
from bus_query import parse_query, format_arrival_message

app = Flask(__name__)

# LINE Bot credentials
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# TDX credentials
TDX_CLIENT_ID = os.environ.get("TDX_CLIENT_ID", "")
TDX_CLIENT_SECRET = os.environ.get("TDX_CLIENT_SECRET", "")

tdx = TDXClient(TDX_CLIENT_ID, TDX_CLIENT_SECRET)


@app.route("/", methods=["GET"])
def health_check():
    return "LINE Bot Bus Query Service is running!", 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text.strip()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # Check for help command
        if user_message in ["help", "說明", "Help", "HELP", "使用說明"]:
            reply = get_help_message()
        else:
            # Try to parse bus query
            parsed = parse_query(user_message)
            if parsed:
                direction, route_name, stop_name = parsed
                reply = query_bus(direction, route_name, stop_name)
            else:
                reply = TextMessage(
                    text=(
                        "❓ 無法辨識查詢格式\n\n"
                        "📌 正確格式：\n"
                        "往[目的地] [路線] [站名]\n\n"
                        "💡 範例：\n"
                        "• 往新竹 1728 花開富貴\n"
                        "• 往台北 9 三重國小\n"
                        "• 往板橋 307 西門\n\n"
                        "輸入「說明」查看更多幫助"
                    )
                )

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[reply]
            )
        )


def query_bus(direction: str, route_name: str, stop_name: str):
    """Query bus arrival time from TDX API"""
    try:
        result = tdx.get_bus_arrival(route_name, stop_name, direction)

        if result is None:
            return TextMessage(
                text=f"⚠️ 查無資料\n\n路線 {route_name}、站點「{stop_name}」不存在，請確認資訊是否正確。"
            )

        if result.get("error"):
            return TextMessage(text=f"❌ 查詢失敗：{result['error']}")

        return format_arrival_message(direction, route_name, stop_name, result)

    except Exception as e:
        return TextMessage(text=f"❌ 系統錯誤：{str(e)}\n請稍後再試。")


def get_help_message():
    return TextMessage(
        text=(
            "🚌 公車到站查詢機器人\n"
            "━━━━━━━━━━━━━━\n\n"
            "📌 查詢格式：\n"
            "往[目的地] [路線] [站名]\n\n"
            "💡 使用範例：\n"
            "• 往新竹 1728 花開富貴\n"
            "• 往台北 9 三重國小\n"
            "• 往板橋 307 西門站\n"
            "• 往左營 高鐵快線 左營\n\n"
            "📊 顯示資訊：\n"
            "• 即將到站車輛車牌\n"
            "• 預估到站時間\n"
            "• 目前所在站點\n"
            "• 距離站數\n\n"
            "⏰ 狀態說明：\n"
            "• 🟢 即將到站 (≤1分鐘)\n"
            "• 🔵 進站中\n"
            "• 🟡 X 分鐘後到站\n"
            "• ⚫ 末班車已過\n\n"
            "━━━━━━━━━━━━━━\n"
            "資料來源：交通部 TDX 平台"
        )
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
