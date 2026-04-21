import os
import re
import logging
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from tdx_client import TDXClient
from bus_query import parse_query, format_arrival_message
import notification_store as store
from scheduler import NotificationScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET       = os.environ.get("LINE_CHANNEL_SECRET", "")
TDX_CLIENT_ID             = os.environ.get("TDX_CLIENT_ID", "")
TDX_CLIENT_SECRET         = os.environ.get("TDX_CLIENT_SECRET", "")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler       = WebhookHandler(LINE_CHANNEL_SECRET)
tdx           = TDXClient(TDX_CLIENT_ID, TDX_CLIENT_SECRET)

# ── 排程器 ──────────────────────────────────────────────────────────────────
# 用 --preload 模式：module 在 master process import，scheduler 在 master 啟動，
# fork 出 worker 後 thread 仍存活（--workers 1 確保只有一個 worker）。
scheduler = NotificationScheduler(configuration, tdx, store)

def _ensure_scheduler():
    """確保排程器正在執行，若已死則重啟"""
    if not (scheduler._thread and scheduler._thread.is_alive()):
        logger.warning("Scheduler not alive — restarting")
        scheduler.start()

# 在每次 request 前檢查排程器是否存活（防禦性措施）
@app.before_request
def before_request():
    _ensure_scheduler()

# 啟動排程器（在 module import 時執行，配合 --preload）
try:
    scheduler.start()
    logger.info(f"Scheduler started at import time (PID {os.getpid()})")
except Exception as e:
    logger.error(f"Failed to start scheduler: {e}")

# ── 路由 ────────────────────────────────────────────────────────────────────

NOTIFY_PATTERN = re.compile(
    r"^通知(\d+)?\s+往\s*(.+?)\s+([^\s]+)\s+(.+)$",
    re.UNICODE,
)


def parse_notify(text: str):
    m = NOTIFY_PATTERN.match(text.strip())
    if not m:
        return None
    threshold  = int(m.group(1)) if m.group(1) else 10
    direction  = m.group(2).strip()
    route_name = m.group(3).strip()
    stop_name  = m.group(4).strip().rstrip("站")
    return threshold, direction, route_name, stop_name


@app.route("/", methods=["GET"])
def health_check():
    return "LINE Bot Bus Query Service is running!", 200


@app.route("/debug", methods=["GET"])
def debug_info():
    """查看排程器狀態與任務清單"""
    tasks = store.get_active_tasks()
    all_tasks = store._tasks
    alive = scheduler._thread.is_alive() if scheduler._thread else False
    logger.info(f"[DEBUG] scheduler_alive={alive}, active={len(tasks)}, pid={os.getpid()}")
    return {
        "scheduler_alive": alive,
        "active_tasks":    len(tasks),
        "total_tasks":     len(all_tasks),
        "pid":             os.getpid(),
        "store_file":      store.STORE_FILE,
        "store_exists":    os.path.exists(store.STORE_FILE),
        "tasks": [
            {
                "task_id":   t.task_id,
                "user_id":   t.user_id[:8] + "...",
                "direction": t.direction,
                "route":     t.route_name,
                "stop":      t.stop_name,
                "next_threshold": t.next_threshold,
                "fired":     t.fired,
                "cancelled": t.cancelled,
            }
            for t in all_tasks.values()
        ],
    }


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
    user_id      = event.source.user_id
    user_message = event.message.text.strip()
    logger.info(f"Message from {user_id[:8]}...: {user_message!r}")

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        if user_message in ["help", "說明", "Help", "HELP", "使用說明"]:
            reply = get_help_message()

        elif user_message in ["取消通知", "取消", "cancel"]:
            count = store.cancel_user_tasks(user_id)
            reply = TextMessage(
                text=f"✅ 已取消 {count} 筆通知任務。" if count
                     else "ℹ️ 目前沒有進行中的通知任務。"
            )

        elif user_message in ["通知列表", "我的通知"]:
            tasks = store.list_user_tasks(user_id)
            if not tasks:
                reply = TextMessage(text="ℹ️ 目前沒有進行中的通知任務。")
            else:
                lines = ["📋 進行中的通知任務：", ""]
                for t in tasks:
                    lines.append(
                        f"🔔 [{t.task_id}] 往{t.direction} {t.route_name} {t.stop_name}站"
                        f"（剩 {t.threshold_min} 分鐘時通知）"
                    )
                reply = TextMessage(text="\n".join(lines))

        elif user_message.startswith("取消") and len(user_message) > 2:
            tid = user_message[2:].strip()
            store.cancel_task(tid)
            reply = TextMessage(text=f"✅ 已取消通知任務 [{tid}]。")

        elif user_message.startswith("通知"):
            parsed = parse_notify(user_message)
            if parsed:
                threshold, direction, route_name, stop_name = parsed

                # 查一次當前 ETA，決定從哪個門檻開始通知
                current_eta_min = None
                try:
                    _result = tdx.get_bus_arrival(route_name, stop_name, direction)
                    if _result and not _result.get("error"):
                        from bus_query import _get_best_eta_min
                        current_eta_min = _get_best_eta_min(_result, stop_name)
                except Exception:
                    pass

                task = store.add_task(user_id, direction, route_name, stop_name, current_eta_min)
                logger.info(
                    f"Notify task added: {task.task_id} for {user_id[:8]}... "
                    f"(ETA={current_eta_min:.1f}min, start_thr={task.next_threshold}min)"
                    if current_eta_min else
                    f"Notify task added: {task.task_id} (ETA unknown, start_thr={task.next_threshold}min)"
                )

                eta_note = (
                    f"目前最快班次約 {int(current_eta_min)} 分鐘後到站\n"
                    f"   將從剩 {task.next_threshold} 分鐘開始通知"
                    if current_eta_min else "（尚無班次資料，等車輛出發後開始監控）"
                )
                reply = TextMessage(
                    text=(
                        f"✅ 通知設定成功！\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"🚌 {route_name} 路 往{direction}\n"
                        f"📍 {stop_name}站\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"⏱ {eta_note}\n"
                        f"🔔 通知節奏：每少 5 分鐘通知一次\n"
                        f"🆔 任務編號：{task.task_id}\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"輸入「取消通知」可取消所有任務。"
                    )
                )
            else:
                reply = TextMessage(
                    text=(
                        "❓ 通知格式錯誤\n\n"
                        "📌 正確格式：\n"
                        "通知 往[目的地] [路線] [站名]\n\n"
                        "💡 範例：\n"
                        "• 通知 往新竹 1728 花開富貴\n"
                        "• 通知5 往新竹 1728 花開富貴\n\n"
                        "預設為到站前 10 分鐘通知。"
                    )
                )

        else:
            parsed = parse_query(user_message)
            if parsed:
                direction, route_name, stop_name = parsed
                reply = query_bus(direction, route_name, stop_name)
            else:
                reply = TextMessage(
                    text=(
                        "❓ 無法辨識查詢格式\n\n"
                        "📌 查詢格式：\n"
                        "往[目的地] [路線] [站名]\n\n"
                        "📌 通知格式：\n"
                        "通知 往[目的地] [路線] [站名]\n\n"
                        "輸入「說明」查看更多幫助"
                    )
                )

        line_bot_api.reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[reply])
        )


def query_bus(direction: str, route_name: str, stop_name: str):
    try:
        result = tdx.get_bus_arrival(route_name, stop_name, direction)
        if result is None:
            return TextMessage(text=f"⚠️ 查無資料\n\n路線 {route_name}、站點「{stop_name}」不存在。")
        if result.get("error"):
            return TextMessage(text=f"❌ 查詢失敗：{result['error']}")
        return format_arrival_message(direction, route_name, stop_name, result)
    except Exception as e:
        logger.exception(f"query_bus error: {e}")
        return TextMessage(text=f"❌ 系統錯誤：{str(e)}\n請稍後再試。")


def get_help_message():
    return TextMessage(
        text=(
            "🚌 公車到站查詢機器人\n"
            "━━━━━━━━━━━━━━\n\n"
            "📌 即時查詢：\n"
            "往[目的地] [路線] [站名]\n"
            "例：往新竹 1728 花開富貴\n\n"
            "🔔 到站通知：\n"
            "通知 往[目的地] [路線] [站名]\n"
            "例：通知 往新竹 1728 花開富貴\n"
            "（預設剩10分鐘時通知）\n\n"
            "通知5 往新竹 1728 花開富貴\n"
            "（自訂剩5分鐘時通知）\n\n"
            "📋 管理通知：\n"
            "• 通知列表 — 查看進行中任務\n"
            "• 取消通知 — 取消所有任務\n\n"
            "━━━━━━━━━━━━━━\n"
            "資料來源：交通部 TDX 平台"
        )
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
