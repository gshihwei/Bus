import re
from typing import Optional, Tuple
from linebot.v3.messaging import TextMessage


QUERY_PATTERN = re.compile(r"往\s*(.+?)\s+([^\s]+)\s+(.+)", re.UNICODE)

BUS_STATUS = {
    0: "正常", 1: "尚未發車", 2: "交管不停靠",
    3: "末班車已過", 4: "今日未營運",
}

CITY_DISPLAY = {
    "Taipei": "台北市", "NewTaipei": "新北市", "Taoyuan": "桃園市",
    "Taichung": "台中市", "Tainan": "台南市", "Kaohsiung": "高雄市",
    "Hsinchu": "新竹市", "HsinchuCounty": "新竹縣", "InterCity": "公路客運",
    "Keelung": "基隆市",
}

# 過時資料門檻（秒）：DataTime 距今超過此值視為殘留記錄，不予顯示
STALE_THRESHOLD_SEC = 600  # 10 分鐘


def parse_query(text: str) -> Optional[Tuple[str, str, str]]:
    match = QUERY_PATTERN.match(text.strip())
    if match:
        return (
            match.group(1).strip(),
            match.group(2).strip(),
            match.group(3).strip().rstrip("站"),
        )
    return None


def _eta_label(seconds: int) -> Tuple[str, str]:
    if seconds == 0:
        return "🔵", "進站中"
    minutes = seconds // 60
    if minutes <= 1:
        return "🟢", "即將到站"
    elif minutes < 60:
        return ("🟡" if minutes <= 5 else "🟠"), f"約 {minutes} 分後到站"
    hrs, mins = divmod(minutes, 60)
    return "🟠", (f"約 {hrs} 時 {mins} 分後到站" if mins else f"約 {hrs} 小時後到站")


def _data_age_seconds(data_time_str: str) -> Optional[float]:
    """Return how many seconds ago DataTime was. None if unparseable."""
    import datetime
    if not data_time_str:
        return None
    try:
        # e.g. "2026-04-10T11:39:40+08:00"
        dt = datetime.datetime.fromisoformat(data_time_str)
        now = datetime.datetime.now(tz=dt.tzinfo)
        return (now - dt).total_seconds()
    except Exception:
        return None


def format_arrival_message(
    direction: str,
    route_name: str,
    stop_name: str,
    result: dict,
) -> TextMessage:
    """
    N1 (EstimatedTimeOfArrival) data model:
      One record per (vehicle × upcoming stop).
      A vehicle at StopSequence N will have records for stops N, N+1, N+2...
      Stops already passed by that vehicle have PlateNumb="-1" (no vehicle bound).

    Strategy:
      1. For each real plate, collect ALL its N1 records for this direction.
      2. Discard stale records (DataTime too old → leftover from previous run).
      3. Find the record whose StopName == target stop → direct ETA.
         If not found, that vehicle has already passed the target stop → skip.
      4. Also resolve CurrentStop StopID → name for display.
    """
    all_n1         = result.get("all_n1", [])
    stopid_to_name = result.get("stopid_to_name", {})
    city           = result.get("city", "")

    lines = [
        f"🚌 {route_name} 路 往{direction}",
        f"📍 {stop_name}站",
        "━━━━━━━━━━━━━━",
    ]

    if not all_n1:
        lines += ["⚠️ 查無到站資料", "（請確認站名或路線是否正確）"]
    else:
        # ── Step 1: Group all records by plate ────────────────────────────
        from collections import defaultdict
        plate_recs: dict[str, list] = defaultdict(list)
        for rec in all_n1:
            plate = rec.get("PlateNumb", "-1")
            if plate and plate != "-1":
                plate_recs[plate].append(rec)

        # ── Step 2: Check global StopStatus from the target stop's "-1" slot ──
        # If StopStatus==3 (末班車已過) on the placeholder row, show that.
        global_status = None
        for rec in all_n1:
            sname = rec.get("StopName", {})
            if isinstance(sname, dict) and sname.get("Zh_tw") == stop_name:
                s = rec.get("StopStatus", 0)
                if s in (1, 3, 4):
                    global_status = s
                break

        # ── Step 3: For each real plate, find ETA to target stop ──────────
        vehicles = []

        for plate, recs in plate_recs.items():
            # Filter stale records (GPS data from a previous trip)
            fresh = [
                r for r in recs
                if (_data_age_seconds(r.get("DataTime", "")) or 0) < STALE_THRESHOLD_SEC
            ]
            if not fresh:
                continue  # All records for this plate are stale → skip

            # Find the record for our target stop
            target_rec = next(
                (r for r in fresh
                 if (r.get("StopName", {}).get("Zh_tw", "") if isinstance(r.get("StopName"), dict) else "") == stop_name),
                None
            )

            if target_rec is None:
                # Vehicle has no upcoming record for this stop →
                # either already passed it, or it's beyond the last stop in N1.
                continue

            eta_sec     = target_rec.get("EstimateTime")
            status      = target_rec.get("StopStatus", 0)
            current_sid = str(target_rec.get("CurrentStop", ""))
            current_stop_name = stopid_to_name.get(current_sid, "")

            if status == 3:
                vehicles.append({"plate": plate, "eta": 99999, "emoji": "⚫", "label": "末班車已過",           "current_stop": current_stop_name})
            elif status == 1 and eta_sec is None:
                # Slot not yet assigned to a vehicle for this stop
                continue
            elif status in (2, 4):
                vehicles.append({"plate": plate, "eta": 99997, "emoji": "⛔", "label": BUS_STATUS.get(status, ""), "current_stop": current_stop_name})
            elif eta_sec is not None:
                emoji, label = _eta_label(int(eta_sec))
                vehicles.append({
                    "plate": plate, "eta": int(eta_sec),
                    "emoji": emoji, "label": label,
                    "current_stop": current_stop_name,
                })

        vehicles.sort(key=lambda v: v["eta"])
        active = [v for v in vehicles if v["eta"] < 90000]

        if not active:
            if global_status == 3:
                lines.append("⚫ 末班車已過")
            elif global_status == 4:
                lines.append("⛔ 今日未營運")
            else:
                lines.append("⚠️ 目前無車輛資料")
                lines.append("（可能尚未發車或非營運時間）")
        else:
            lines += ["🕐 到站預報", ""]
            for i, v in enumerate(active[:4], 1):
                lines.append(f"{v['emoji']} 第 {i} 班  【{v['plate']}】")
                lines.append(f"   ⏱ {v['label']}")
                if v["current_stop"]:
                    lines.append(f"   📌 現於 {v['current_stop']}站")
                if i < len(active[:4]):
                    lines.append("")

    lines += ["", "━━━━━━━━━━━━━━", f"📡 {CITY_DISPLAY.get(city, city)} ｜ 資料：TDX"]
    return TextMessage(text="\n".join(lines))
