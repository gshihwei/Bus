"""
scheduler.py
背景排程執行緒：每分鐘掃描所有進行中的通知任務，
當最快到站的班次剩餘時間 ≤ threshold_min 時，
透過 LINE Push Message API 主動通知用戶。
"""
import logging
import threading
import time
from typing import Optional

from linebot.v3.messaging import ApiClient, Configuration, MessagingApi, PushMessageRequest, TextMessage

logger = logging.getLogger(__name__)


def _build_push_text(task, eta_sec: int, plate: str, current_stop: str) -> str:
    """組裝推播通知訊息內文"""
    minutes = eta_sec // 60
    plate_str = f"【{plate}】" if plate and plate not in ("-1", "-", "") else ""
    cur_str = f"\n📌 現於 {current_stop}站" if current_stop else ""

    lines = [
        f"🔔 到站提醒",
        f"━━━━━━━━━━━━━━",
        f"🚌 {task.route_name} 路 往{task.direction}",
        f"📍 {task.stop_name}站",
        f"",
        f"🟡 {plate_str} 約 {minutes} 分鐘後到站{cur_str}",
        f"",
        f"━━━━━━━━━━━━━━",
        f"（本通知發送後自動取消監控）",
    ]
    return "\n".join(lines)


class NotificationScheduler:
    """
    每 CHECK_INTERVAL 秒執行一次掃描。
    若最近到站班次剩餘時間 ≤ threshold_min 分鐘，
    透過 LINE Push API 發送通知並標記任務完成。
    """

    CHECK_INTERVAL = 60  # 秒

    def __init__(self, configuration: Configuration, tdx_client, store_module):
        self._config = configuration
        self._tdx = tdx_client
        self._store = store_module
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

        logger.info(f"Scanning {len(tasks)} active notification task(s)")

        for task in tasks:
            try:
                self._check_task(task)
            except Exception as e:
                logger.warning(f"Task {task.task_id} check failed: {e}")

    def _check_task(self, task):
        """查詢 TDX，若最快班次 ETA ≤ threshold → push 通知"""
        result = self._tdx.get_bus_arrival(task.route_name, task.stop_name, task.direction)
        if not result or result.get("error"):
            return

        # Replicate the same vehicle-finding logic as bus_query.py
        all_n1          = result.get("all_n1", [])
        stopid_to_name  = result.get("stopid_to_name", {})
        direction_value = result.get("direction_value", 0)

        # Build stop→seq map
        stop_to_seq: dict[str, int] = {}
        for rec in all_n1:
            if rec.get("Direction") != direction_value:
                continue
            sname = rec.get("StopName", {}).get("Zh_tw", "") if isinstance(rec.get("StopName"), dict) else ""
            seq   = rec.get("StopSequence")
            if sname and seq is not None:
                stop_to_seq[sname] = int(seq)
        target_seq_num = stop_to_seq.get(task.stop_name)

        import datetime
        from collections import defaultdict

        STALE = 600
        now = datetime.datetime.now(tz=datetime.timezone(datetime.timedelta(hours=8)))

        def age(s):
            try:
                dt = datetime.datetime.fromisoformat(s)
                return (now - dt).total_seconds()
            except Exception:
                return 9999

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
            fresh = [r for r in recs if age(r.get("DataTime", "")) < STALE]
            if not fresh:
                continue

            target_rec = next(
                (r for r in fresh
                 if (r.get("StopName", {}).get("Zh_tw", "") if isinstance(r.get("StopName"), dict) else "") == task.stop_name),
                None
            )

            if target_rec is not None:
                eta = target_rec.get("EstimateTime")
                if eta is not None:
                    cur = stopid_to_name.get(str(target_rec.get("CurrentStop", "")), "")
                    if best_eta is None or eta < best_eta:
                        best_eta, best_plate, best_stop = int(eta), plate, cur
                continue

            # Fallback estimation
            if target_seq_num is None:
                continue
            seqed = [(stop_to_seq.get(r.get("StopName", {}).get("Zh_tw", "") if isinstance(r.get("StopName"), dict) else ""), r)
                     for r in fresh]
            seqed = [(s, r) for s, r in seqed if s is not None]
            if not seqed:
                continue
            max_seq, max_rec = max(seqed, key=lambda x: x[0])
            if max_seq >= target_seq_num:
                continue

            seqed_s = sorted(seqed, key=lambda x: x[0])
            per_stop = 180.0
            if len(seqed_s) >= 2:
                gaps = []
                for i in range(1, len(seqed_s)):
                    s_a, r_a = seqed_s[i-1]
                    s_b, r_b = seqed_s[i]
                    e_a, e_b = r_a.get("EstimateTime"), r_b.get("EstimateTime")
                    if e_a and e_b and s_b > s_a:
                        gaps.append((e_b - e_a) / (s_b - s_a))
                if gaps:
                    per_stop = sum(gaps) / len(gaps)

            base = max_rec.get("EstimateTime")
            if not base:
                continue
            est = int(base + (target_seq_num - max_seq) * per_stop)
            cur = stopid_to_name.get(str(max_rec.get("CurrentStop", "")), "")
            if best_eta is None or est < best_eta:
                best_eta, best_plate, best_stop = est, plate, cur

        if best_eta is None:
            logger.info(f"Task {task.task_id}: no ETA found yet")
            return

        remaining_min = best_eta / 60
        logger.info(f"Task {task.task_id}: best ETA={best_eta}s ({remaining_min:.1f}min), threshold={task.threshold_min}min")

        if remaining_min <= task.threshold_min:
            self._push(task, best_eta, best_plate, best_stop)
            self._store.mark_fired(task.task_id)

    def _push(self, task, eta_sec: int, plate: str, current_stop: str):
        """透過 LINE Push Message API 發送通知"""
        text = _build_push_text(task, eta_sec, plate, current_stop)
        with ApiClient(self._config) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=task.user_id,
                    messages=[TextMessage(text=text)],
                )
            )
        logger.info(f"Task {task.task_id}: push sent to {task.user_id[:8]}...")
