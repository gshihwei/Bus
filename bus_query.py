import re
from typing import Optional, Tuple
from linebot.v3.messaging import TextMessage


# Regex: 往[目的地] [路線] [站名]
QUERY_PATTERN = re.compile(r"往\s*(.+?)\s+([^\s]+)\s+(.+)", re.UNICODE)

BUS_STATUS = {
    0: "正常",
    1: "尚未發車",
    2: "交管不停靠",
    3: "末班車已過",
    4: "今日未營運",
}

CITY_DISPLAY = {
    "Taipei": "台北市", "NewTaipei": "新北市", "Taoyuan": "桃園市",
    "Taichung": "台中市", "Tainan": "台南市", "Kaohsiung": "高雄市",
    "Hsinchu": "新竹市", "HsinchuCounty": "新竹縣", "InterCity": "公路客運",
    "Keelung": "基隆市",
}


def parse_query(text: str) -> Optional[Tuple[str, str, str]]:
    match = QUERY_PATTERN.match(text.strip())
    if match:
        direction  = match.group(1).strip()
        route_name = match.group(2).strip()
        stop_name  = match.group(3).strip().rstrip("站")
        return direction, route_name, stop_name
    return None


def _eta_label(seconds: int) -> Tuple[str, str]:
    """Return (emoji, label) for ETA in seconds."""
    if seconds == 0:
        return "🔵", "進站中"
    minutes = seconds // 60
    if minutes <= 1:
        return "🟢", "即將到站"
    elif minutes < 60:
        return ("🟡" if minutes <= 5 else "🟠"), f"約 {minutes} 分後到站"
    else:
        hrs, mins = divmod(minutes, 60)
        label = f"約 {hrs} 時 {mins} 分後到站" if mins else f"約 {hrs} 小時後到站"
        return "🟠", label


def format_arrival_message(
    direction: str,
    route_name: str,
    stop_name: str,
    result: dict,
) -> TextMessage:
    """
    Build the LINE reply from N1 (EstimatedTimeOfArrival) data.

    N1 data model — one record per (vehicle × upcoming stop):
      PlateNumb    – plate number; "-1" = no vehicle assigned to this slot
      StopName     – the upcoming stop this record describes
      StopSequence – that stop's sequence on the route
      EstimateTime – seconds until arrival (absent when StopStatus ≠ 0)
      StopStatus   – 0 normal | 1 not yet departed | 2 no service | 3 last bus passed
      CurrentStop  – StopID of where the bus physically is RIGHT NOW
    """

    target_records = result.get("target_records", [])  # N1 rows for queried stop
    stopid_to_name = result.get("stopid_to_name", {})  # StopID → stop name
    city           = result.get("city", "")

    lines = [
        f"🚌 {route_name} 路 往{direction}",
        f"📍 {stop_name}站",
        "━━━━━━━━━━━━━━",
    ]

    if not target_records:
        lines += ["⚠️ 查無到站資料", "（請確認站名或路線是否正確）"]
    else:
        vehicles = []

        for rec in target_records:
            plate       = rec.get("PlateNumb", "-1")
            status      = rec.get("StopStatus", 0)
            eta_sec     = rec.get("EstimateTime")       # N1 uses EstimateTime (not EstimatedArrivalTime)
            current_sid = str(rec.get("CurrentStop", ""))

            # Map StopID to a human-readable stop name
            current_stop_name = stopid_to_name.get(current_sid, "")

            if status == 3:
                vehicles.append({"plate": plate, "eta": 99999, "emoji": "⚫", "label": "末班車已過",       "current_stop": current_stop_name})
            elif status == 1:
                vehicles.append({"plate": plate, "eta": 99998, "emoji": "⏸️", "label": "尚未發車",         "current_stop": current_stop_name})
            elif status in (2, 4):
                vehicles.append({"plate": plate, "eta": 99997, "emoji": "⛔", "label": BUS_STATUS.get(status, ""), "current_stop": current_stop_name})
            elif eta_sec is not None and plate not in ("-1", ""):
                # Real vehicle with a valid ETA
                emoji, label = _eta_label(int(eta_sec))
                vehicles.append({"plate": plate, "eta": int(eta_sec), "emoji": emoji, "label": label, "current_stop": current_stop_name})
            # PlateNumb=="-1" + no ETA → placeholder slot with no bus, skip entirely

        vehicles.sort(key=lambda v: v["eta"])
        active = [v for v in vehicles if v["eta"] < 90000]

        if not active:
            terminal = [v for v in vehicles if v["eta"] == 99999]
            lines.append("⚫ 末班車已過" if terminal else "⚠️ 目前無車輛資料\n（可能尚未發車或非營運時間）")
        else:
            lines += ["🕐 到站預報", ""]
            for i, v in enumerate(active[:4], 1):
                plate_str = v["plate"] if v["plate"] not in ("-1", "-", "") else "車牌未提供"
                lines.append(f"{v['emoji']} 第 {i} 班  【{plate_str}】")
                lines.append(f"   ⏱ {v['label']}")
                if v["current_stop"]:
                    lines.append(f"   📌 現於 {v['current_stop']}站")
                if i < len(active[:4]):
                    lines.append("")

    lines += ["", "━━━━━━━━━━━━━━", f"📡 {CITY_DISPLAY.get(city, city)} ｜ 資料：TDX"]
    return TextMessage(text="\n".join(lines))
