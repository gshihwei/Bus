"""
notification_store.py
儲存及管理到站通知任務。
"""
import json
import os
import threading
import uuid
from dataclasses import dataclass, asdict
from typing import Optional

STORE_FILE = os.environ.get("NOTIFY_STORE_PATH", "/tmp/notifications.json")
_lock = threading.Lock()

# 通知階段門檻（分鐘），由大到小依序觸發
NOTIFY_THRESHOLDS = [20, 15, 10, 5]


@dataclass
class NotifyTask:
    task_id: str
    user_id: str
    direction: str
    route_name: str
    stop_name: str
    # 下一次要在幾分鐘時發送（從 NOTIFY_THRESHOLDS[0] 開始往下走）
    next_threshold: int = 20
    cancelled: bool = False


def _load() -> dict[str, "NotifyTask"]:
    if not os.path.exists(STORE_FILE):
        return {}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: NotifyTask(**v) for k, v in raw.items()}
    except Exception:
        return {}


def _save(tasks: dict[str, "NotifyTask"]):
    try:
        with open(STORE_FILE, "w", encoding="utf-8") as f:
            json.dump({k: asdict(v) for k, v in tasks.items()}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_tasks: dict[str, NotifyTask] = _load()


def _pick_start_threshold(current_eta_min: Optional[float]) -> int:
    """
    根據目前已知的 ETA（分鐘）決定從哪個門檻開始通知。
    例：ETA=12 分 → 已過 20/15 分門檻 → 從 10 分開始
        ETA=8  分 → 已過 20/15/10 分門檻 → 從 5 分開始
        ETA=None  → 從最大門檻 20 分開始
    """
    if current_eta_min is None:
        return NOTIFY_THRESHOLDS[0]
    for thr in NOTIFY_THRESHOLDS:
        if current_eta_min > thr:
            return thr
    # ETA 已小於最小門檻（5分），仍從5分開始（讓 scheduler 立即觸發）
    return NOTIFY_THRESHOLDS[-1]


def add_task(
    user_id: str,
    direction: str,
    route_name: str,
    stop_name: str,
    current_eta_min: Optional[float] = None,
) -> NotifyTask:
    """
    新增通知任務，若同一用戶已有相同任務則先移除。
    current_eta_min：建立任務當下查到的最快 ETA（分鐘），
    用來決定從哪個門檻開始，避免跳過已過的門檻。
    """
    start_threshold = _pick_start_threshold(current_eta_min)
    with _lock:
        to_remove = [
            tid for tid, t in _tasks.items()
            if t.user_id == user_id
            and t.direction == direction
            and t.route_name == route_name
            and t.stop_name == stop_name
            and not t.cancelled
        ]
        for tid in to_remove:
            del _tasks[tid]

        task = NotifyTask(
            task_id=str(uuid.uuid4())[:8],
            user_id=user_id,
            direction=direction,
            route_name=route_name,
            stop_name=stop_name,
            next_threshold=start_threshold,
        )
        _tasks[task.task_id] = task
        _save(_tasks)
        return task


def get_active_tasks() -> list[NotifyTask]:
    """回傳所有未取消的任務。"""
    with _lock:
        return [t for t in _tasks.values() if not t.cancelled]


def advance_or_complete(task_id: str) -> Optional[int]:
    """
    推播完一個門檻後呼叫：
    - 若還有下一個門檻 → 更新 next_threshold，回傳新門檻值
    - 若已是最後一個門檻（5分鐘）→ 刪除任務，回傳 None
    """
    with _lock:
        if task_id not in _tasks:
            return None
        task = _tasks[task_id]
        idx = NOTIFY_THRESHOLDS.index(task.next_threshold) if task.next_threshold in NOTIFY_THRESHOLDS else -1
        next_idx = idx + 1
        if next_idx < len(NOTIFY_THRESHOLDS):
            task.next_threshold = NOTIFY_THRESHOLDS[next_idx]
            _save(_tasks)
            return task.next_threshold
        else:
            # 最後一個門檻已發送，刪除任務
            del _tasks[task_id]
            _save(_tasks)
            return None


def cancel_task(task_id: str):
    with _lock:
        if task_id in _tasks:
            del _tasks[task_id]
            _save(_tasks)


def cancel_user_tasks(user_id: str) -> int:
    with _lock:
        to_remove = [tid for tid, t in _tasks.items() if t.user_id == user_id and not t.cancelled]
        for tid in to_remove:
            del _tasks[tid]
        if to_remove:
            _save(_tasks)
        return len(to_remove)


def list_user_tasks(user_id: str) -> list[NotifyTask]:
    with _lock:
        return [t for t in _tasks.values() if t.user_id == user_id and not t.cancelled]
