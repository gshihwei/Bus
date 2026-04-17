"""
notification_store.py
儲存及管理到站通知任務。
每個任務包含：user_id、查詢參數、通知門檻（分鐘）、狀態。
使用 JSON 檔案做簡單持久化，服務重啟後任務不遺失。
"""
import json
import os
import threading
import uuid
from dataclasses import dataclass, asdict, field
from typing import Optional

# 使用 /tmp 確保 Render 環境可寫入
STORE_FILE = os.environ.get("NOTIFY_STORE_PATH", "/tmp/notifications.json")
_lock = threading.Lock()


@dataclass
class NotifyTask:
    task_id: str
    user_id: str
    direction: str
    route_name: str
    stop_name: str
    threshold_min: int = 15        # 提前幾分鐘通知
    fired: bool = False            # 已發送過通知
    cancelled: bool = False        # 使用者主動取消


def _load() -> dict[str, NotifyTask]:
    """從 JSON 檔載入所有任務"""
    if not os.path.exists(STORE_FILE):
        return {}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: NotifyTask(**v) for k, v in raw.items()}
    except Exception:
        return {}


def _save(tasks: dict[str, NotifyTask]):
    """將所有任務寫回 JSON 檔"""
    try:
        with open(STORE_FILE, "w", encoding="utf-8") as f:
            json.dump({k: asdict(v) for k, v in tasks.items()}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# 全域任務表（記憶體中）
_tasks: dict[str, NotifyTask] = _load()


def add_task(
    user_id: str,
    direction: str,
    route_name: str,
    stop_name: str,
    threshold_min: int = 15,
) -> NotifyTask:
    """新增一筆通知任務，回傳任務物件"""
    with _lock:
        # 同一用戶如有完全相同的未完成任務，先取消舊的
        for t in list(_tasks.values()):
            if (t.user_id == user_id
                    and t.direction == direction
                    and t.route_name == route_name
                    and t.stop_name == stop_name
                    and not t.fired
                    and not t.cancelled):
                t.cancelled = True

        task = NotifyTask(
            task_id=str(uuid.uuid4())[:8],
            user_id=user_id,
            direction=direction,
            route_name=route_name,
            stop_name=stop_name,
            threshold_min=threshold_min,
        )
        _tasks[task.task_id] = task
        _save(_tasks)
        return task


def get_active_tasks() -> list[NotifyTask]:
    """回傳所有尚未發送且未取消的任務"""
    with _lock:
        return [t for t in _tasks.values() if not t.fired and not t.cancelled]


def mark_fired(task_id: str):
    """標記任務已發送通知"""
    with _lock:
        if task_id in _tasks:
            _tasks[task_id].fired = True
            _save(_tasks)


def cancel_task(task_id: str):
    """取消任務"""
    with _lock:
        if task_id in _tasks:
            _tasks[task_id].cancelled = True
            _save(_tasks)


def cancel_user_tasks(user_id: str) -> int:
    """取消某用戶所有進行中的任務，回傳取消數量"""
    with _lock:
        count = 0
        for t in _tasks.values():
            if t.user_id == user_id and not t.fired and not t.cancelled:
                t.cancelled = True
                count += 1
        if count:
            _save(_tasks)
        return count


def list_user_tasks(user_id: str) -> list[NotifyTask]:
    """列出某用戶所有進行中的任務"""
    with _lock:
        return [t for t in _tasks.values()
                if t.user_id == user_id and not t.fired and not t.cancelled]
