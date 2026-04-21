"""
scheduler.py
背景排程執行緒：每分鐘掃描通知任務。
通知節奏：剩 20 → 15 → 10 → 5 分鐘各推播一次，5 分鐘後刪除任務。
"""
import datetime
import logging
import threading
from collections import defaultdict
from typing import Optional

from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi, PushMessageRequest, TextMessage,
)

from notification_store import NOTIFY_THRESHOLDS

logger = logging.getLogger(__name__)

STALE_SEC = 600
TW_TZ = datetime.timezone(datetime.timedelta(hours=8))


def _now_tw() -> datetime.datetime:
    return datetime.datetime.now(tz=TW_TZ)


def _age(data_time_str: str) -> float:
    try:
        dt = datetime.datetime.fromisoformat(data_time_str)
        return (_now_tw() - dt).total_seconds()
    except Exception:
        return 9999.0


def _eta_label(seconds: int) -> str:
    m = seconds // 60
    if m <= 1:
        return "即將到站"
    return f"約 {m} 分鐘後到站"


def _build_push_text(task, eta_sec: int, plate: str, current_stop: str, next_threshold: Optional[int]) -> str:
    plate_str = f"【{plate}】 " if plate and plate not in ("-1", "-", "") else ""
    cur_str   = f"\n📌 現於 {current_stop}站" if current_stop else ""
    if next_threshold:
        footer = f"下次將於剩 {next_threshold} 分鐘時再通知。"
    else:
        footer = "已是最後提醒，監控結束。"

    lines = [
        "🔔 到站提醒",
        "━━━━━━━━━━━━━━",
        f"🚌 {task.route_name} 路 往{task.direction}",
        f"📍 {task.stop_name}站",
        "",
        f"🟡 {plate_str}{_eta_label(eta_sec)}{cur_str}",
        "",
        "━━━━━━━━━━━━━━",
        footer,
    ]
    return "\n".join(lines)


def _find_best_eta(all_n1: list, stopid_to_name: dict, direction_value: int, stop_name: str):
    """從 N1 資料找出最快到達 stop_name 的 ETA（秒）、車牌、現在站名。"""
    # 建立站名→站序對照表
    stop_to_seq: dict[str, int] = {}
    for rec in all_n1:
        if rec.get("Direction") != direction_value:
            continue
        sname = rec.get("StopName", {}).get("Zh_tw", "") if isinstance(rec.get("StopName"), dict) else ""
        seq   = rec.get("StopSequence")
        if sname and seq is not None:
            stop_to_seq[sname] = int(seq)
    target_seq = stop_to_seq.get(stop_name)

    # 依車牌分組（只取同方向）
    plate_recs: dict[str, list] = defaultdict(list)
    for rec in all_n1:
        if rec.get("Direction") != direction_value:
            continue
        p = rec.get("PlateNumb", "-1")
        if p and p != "-1":
            plate_recs[p].append(rec)

    best_eta: Optional[int] = None
    best_plate = ""
    best_stop  = ""

    for plate, recs in plate_recs.items():
        fresh = [r for r in recs if _age(r.get("DataTime", "")) < STALE_SEC]
        if not fresh:
            continue

        # 直接找目標站的 ETA 記錄
        target_rec = next(
            (r for r in fresh
             if (r.get("StopName", {}).get("Zh_tw", "") if isinstance(r.get("StopName"), dict) else "") == stop_name),
            None,
        )
        if target_rec is not None:
                # Validate CurrentStop is not past the target stop
                current_sid = str(target_rec.get("CurrentStop", ""))
                cur = stopid_to_name.get(current_sid, "")
                if target_seq is not None and cur:
                    cur_seq = stop_to_seq.get(cur)
                    if cur_seq is not None and cur_seq > target_seq:
                        continue  # stale — vehicle already passed target
                eta = target_rec.get("EstimateTime")
                if eta is not None:
                    if best_eta is None or eta < best_eta:
                        best_eta, best_plate, best_stop = int(eta), plate, cur
                continue

        # Fallback：用最遠已知站推估
        if target_seq is None:
            continue
        seqed = [
            (stop_to_seq.get(r.get("StopName", {}).get("Zh_tw", "") if isinstance(r.get("StopName"), dict) else ""), r)
            for r in fresh
        ]
        seqed = [(s, r) for s, r in seqed if s is not None]
        if not seqed:
            continue
        max_seq, max_rec = max(seqed, key=lambda x: x[0])
        if max_seq >= target_seq:
            continue  # 已過站

        seqed_s = sorted(seqed, key=lambda x: x[0])
        per_stop = 180.0
        if len(seqed_s) >= 2:
            gaps = [
                (seqed_s[i][1].get("EstimateTime", 0) - seqed_s[i-1][1].get("EstimateTime", 0))
                / (seqed_s[i][0] - seqed_s[i-1][0])
                for i in range(1, len(seqed_s))
                if seqed_s[i][1].get("EstimateTime") and seqed_s[i-1][1].get("EstimateTime")
                and seqed_s[i][0] > seqed_s[i-1][0]
            ]
            if gaps:
                per_stop = sum(gaps) / len(gaps)

        base = max_rec.get("EstimateTime")
        if not base:
            continue
        est = int(base + (target_seq - max_seq) * per_stop)
        cur = stopid_to_name.get(str(max_rec.get("CurrentStop", "")), "")
        if best_eta is None or est < best_eta:
            best_eta, best_plate, best_stop = est, plate, cur

    return best_eta, best_plate, best_stop


class NotificationScheduler:
    CHECK_INTERVAL = 60  # 秒

    def __init__(self, configuration: Configuration, tdx_client, store_module):
        self._config = configuration
        self._tdx    = tdx_client
        self._store  = store_module
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="NotifyScheduler")
        self._thread.start()
        logger.info("NotificationScheduler started")

    def stop(self):
        self._stop_event.set()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._scan()
            except Exception as e:
                logger.exception(f"Scheduler scan error: {e}")
            self._stop_event.wait(self.CHECK_INTERVAL)

    def _scan(self):
        tasks = self._store.get_active_tasks()
        if not tasks:
            return
        logger.info(f"Scanning {len(tasks)} task(s)")
        for task in tasks:
            try:
                self._check_task(task)
            except Exception as e:
                logger.warning(f"Task {task.task_id} error: {e}")

    def _check_task(self, task):
        result = self._tdx.get_bus_arrival(task.route_name, task.stop_name, task.direction)
        if not result or result.get("error"):
            return

        all_n1          = result.get("all_n1", [])
        stopid_to_name  = result.get("stopid_to_name", {})
        direction_value = result.get("direction_value", 0)

        best_eta, best_plate, best_stop = _find_best_eta(
            all_n1, stopid_to_name, direction_value, task.stop_name
        )

        if best_eta is None:
            logger.info(f"Task {task.task_id}: no ETA yet")
            return

        remaining_min = best_eta / 60
        logger.info(
            f"Task {task.task_id}: ETA={best_eta}s ({remaining_min:.1f}min), "
            f"next_threshold={task.next_threshold}min"
        )

        if remaining_min <= task.next_threshold:
            # 先記錄當前門檻（用於訊息顯示），再推進到下一個門檻
            current_threshold = task.next_threshold
            next_thr = self._store.advance_or_complete(task.task_id)
            self._push(task, best_eta, best_plate, best_stop, next_thr)
            logger.info(
                f"Task {task.task_id}: triggered at {current_threshold}min threshold, "
                f"next={next_thr}min" if next_thr else
                f"Task {task.task_id}: final notification sent, task deleted"
            )

    def _push(self, task, eta_sec: int, plate: str, current_stop: str, next_threshold: Optional[int]):
        text = _build_push_text(task, eta_sec, plate, current_stop, next_threshold)
        try:
            with ApiClient(self._config) as api_client:
                MessagingApi(api_client).push_message(
                    PushMessageRequest(
                        to=task.user_id,
                        messages=[TextMessage(text=text)],
                    )
                )
            status = f"next={next_threshold}min" if next_threshold else "DONE(deleted)"
            logger.info(f"Task {task.task_id}: push sent — {status}")
        except Exception as e:
            logger.error(f"Task {task.task_id}: push FAILED — {e}")
            raise
