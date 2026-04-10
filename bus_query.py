import re
from typing import Optional, Tuple
from linebot.v3.messaging import TextMessage


# Regex: 往[目的地] [路線] [站名]
QUERY_PATTERN = re.compile(
    r"往\s*(.+?)\s+([^\s]+)\s+(.+)",
    re.UNICODE
)

# TDX BusStatus codes
BUS_STATUS = {
    0: "正常",
    1: "尚未發車",
    2: "交管不停靠",
    3: "末班車已過",
    4: "今日未營運",
}

# 每站平均行駛秒數（用於 GPS 位置推算 ETA）
INTERCITY_SECS_PER_STOP = 180   # 公路客運約 3 分鐘/站
CITY_SECS_PER_STOP = 120        # 市區公車約 2 分鐘/站


def parse_query(text: str) -> Optional[Tuple[str, str, str]]:
    """
    Parse user input like '往新竹 1728 花開富貴'
    Returns (direction, route_name, stop_name) or None
    """
    match = QUERY_PATTERN.match(text.strip())
    if match:
        direction = match.group(1).strip()
        route_name = match.group(2).strip()
        stop_name = match.group(3).strip()
        stop_name_clean = stop_name.rstrip("站")
        return direction, route_name, stop_name_clean
    return None


def _estimate_eta_from_position(bus_seq: int, target_seq: int, city: str) -> Optional[int]:
    """
    Estimate ETA in seconds using stop count difference.
    Returns None if the bus has already passed the target stop.
    """
    stops_away = target_seq - bus_seq
    if stops_away < 0:
        return None   # already passed
    if stops_away == 0:
        return 0      # at or entering the stop
    secs = INTERCITY_SECS_PER_STOP if city == "InterCity" else CITY_SECS_PER_STOP
    return stops_away * secs


def _eta_label(seconds: int) -> Tuple[str, str]:
    """Return (emoji, human-readable label) for ETA in seconds."""
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
    """Merge ETA API data + GPS positions into a unified arrival display."""

    arrivals  = result.get("arrivals", [])
    buses     = result.get("buses", [])
    stop_info = result.get("stop_info")
    city      = result.get("city", "")

    # ── 前置：建立「站名 → 排序位置」對照表 ──────────────────────────────
    # stop_info["all_stops"] 是依 StopSequence 排序的完整站列表 [{seq, name}, ...]
    # 用站名在此列表中的 index 做比較，避免跨 API 站序號不同源的問題
    all_stops: list[dict] = (stop_info.get("all_stops") or []) if stop_info else []
    # stop_name_to_rank: 站名 → 在路線中的順序位置（0-based）
    stop_name_to_rank: dict[str, int] = {s["name"]: i for i, s in enumerate(all_stops)}
    target_rank: int | None = stop_name_to_rank.get(stop_name)  # 目標站的排序位置

    # ── Step 1: seed vehicle_map from ETA API ─────────────────────────────
    vehicle_map: dict[str, dict] = {}

    for item in arrivals:
        status  = item.get("StopStatus", 0)
        eta_sec = item.get("EstimatedArrivalTime")
        plate   = item.get("PlateNumb") or "-"

        if status == 3:
            vehicle_map[plate] = {"plate": plate, "eta": 99999, "status": "末班車已過", "emoji": "⚫", "stop": "-", "stops_away": None, "source": "api"}
        elif status == 1:
            vehicle_map[plate] = {"plate": plate, "eta": 99998, "status": "尚未發車",   "emoji": "⏸️", "stop": "-", "stops_away": None, "source": "api"}
        elif status in (2, 4):
            vehicle_map[plate] = {"plate": plate, "eta": 99997, "status": BUS_STATUS[status], "emoji": "⛔", "stop": "-", "stops_away": None, "source": "api"}
        elif eta_sec is not None:
            emoji, label = _eta_label(int(eta_sec))
            vehicle_map[plate] = {"plate": plate, "eta": int(eta_sec), "status": label, "emoji": emoji, "stop": "-", "stops_away": None, "source": "api"}

    # ── Step 2: enrich / fill from GPS real-time positions ────────────────
    for bus in buses:
        bus_status = bus.get("BusStatus", 0)
        plate      = bus.get("PlateNumb") or "-"
        raw_stop   = bus.get("StopName", {})
        cur_stop   = raw_stop.get("Zh_tw", "-") if isinstance(raw_stop, dict) else str(raw_stop)

        # Skip buses clearly not in service
        if bus_status in (2, 3, 4):
            continue

        # ── 用站名排序位置判斷是否已過目標站 ──────────────────────────────
        # 優先用站名比較（不依賴跨 API 的站序號）
        bus_rank = stop_name_to_rank.get(cur_stop)  # 車輛目前站的排序位置

        if target_rank is not None and bus_rank is not None:
            # 兩個站名都在路線表內 → 直接比較排序位置
            if bus_rank > target_rank:
                continue  # 已過目標站，略過
            stops_away = target_rank - bus_rank
        elif target_rank is not None and bus_rank is None:
            # 車輛目前站名不在路線站表內（GPS 定位在站間、或站名略有差異）
            # 退而使用原始站序號做粗略比對
            bus_seq    = bus.get("StopSequence", 0)
            target_seq = stop_info.get("sequence") if stop_info else None
            if target_seq is not None and bus_seq > target_seq:
                continue
            stops_away = (target_seq - bus_seq) if target_seq is not None else None
        else:
            stops_away = None

        if plate in vehicle_map and vehicle_map[plate].get("source") == "api":
            # 已有精確 ETA → 只補上目前站資訊
            vehicle_map[plate]["stop"]       = cur_stop
            vehicle_map[plate]["stops_away"] = stops_away
        else:
            # ETA API 沒有這輛車 → 用站數差估算
            if stops_away is not None:
                eta_est = stops_away * (INTERCITY_SECS_PER_STOP if city == "InterCity" else CITY_SECS_PER_STOP)
                emoji, label = _eta_label(eta_est)
                label += "（估）"
            else:
                eta_est = 88888
                emoji, label = "🟡", "行駛中"

            vehicle_map[plate] = {
                "plate":      plate,
                "eta":        eta_est,
                "status":     label,
                "emoji":      emoji,
                "stop":       cur_stop,
                "stops_away": stops_away,
                "source":     "gps",
            }

    # ── Step 3: sort and render ───────────────────────────────────────────
    sorted_v = sorted(vehicle_map.values(), key=lambda v: v["eta"])
    active   = [v for v in sorted_v if v["eta"] < 90000]

    lines = [
        f"🚌 {route_name} 路 往{direction}",
        f"📍 {stop_name}站",
        "━━━━━━━━━━━━━━",
    ]

    if not active:
        terminal = [v for v in sorted_v if v["eta"] == 99999]
        if terminal:
            lines.append("⚫ 末班車已過")
        else:
            lines.append("⚠️ 目前無車輛資料")
            lines.append("（可能為非營運時間或 GPS 訊號中斷）")
    else:
        lines.append("🕐 到站預報")
        lines.append("")

        for i, v in enumerate(active[:4], 1):
            plate_str  = v["plate"] if v["plate"] != "-" else "車牌未回傳"
            stops_away = v.get("stops_away")
            cur        = v.get("stop", "-")

            lines.append(f"{v['emoji']} 第 {i} 班  【{plate_str}】")
            lines.append(f"   ⏱ {v['status']}")
            if cur and cur != "-":
                if stops_away == 0:
                    lines.append(f"   📌 正在 {cur}站")
                elif stops_away is not None:
                    lines.append(f"   📌 現於 {cur}站（還有 {stops_away} 站）")
                else:
                    lines.append(f"   📌 現於 {cur}站")
            if i < len(active[:4]):
                lines.append("")

    # ── Footer ────────────────────────────────────────────────────────────
    lines.append("")
    lines.append("━━━━━━━━━━━━━━")
    city_display = {
        "Taipei": "台北市", "NewTaipei": "新北市", "Taoyuan": "桃園市",
        "Taichung": "台中市", "Tainan": "台南市", "Kaohsiung": "高雄市",
        "Hsinchu": "新竹市", "HsinchuCounty": "新竹縣", "InterCity": "公路客運",
        "Keelung": "基隆市",
    }.get(city, city)
    has_estimated = any(v.get("source") == "gps" for v in active)
    note = "  ＊估算時間僅供參考" if has_estimated else ""
    lines.append(f"📡 {city_display} ｜ 資料：TDX{note}")

    return TextMessage(text="\n".join(lines))
