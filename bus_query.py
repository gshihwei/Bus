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

# StopStatus (arrival status) from ETA
STOP_STATUS = {
    0: "正常",
    1: "尚未發車",
    2: "交管不停靠",
    3: "末班車已過",
    4: "今日未營運",
}


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
        # Remove trailing '站' for flexibility
        stop_name_clean = stop_name.rstrip("站")
        return direction, route_name, stop_name_clean
    return None


def format_arrival_message(
    direction: str,
    route_name: str,
    stop_name: str,
    result: dict
) -> TextMessage:
    """Format bus arrival data into a readable LINE message"""

    arrivals = result.get("arrivals", [])
    buses = result.get("buses", [])
    stop_info = result.get("stop_info")
    city = result.get("city", "")

    # Build header
    lines = [
        f"🚌 {route_name} 路 往{direction}",
        f"📍 {stop_name}站",
        "━━━━━━━━━━━━━━",
    ]

    if not arrivals:
        lines.append("⚠️ 目前無到站資料")
        lines.append("（可能為非營運時間）")
    else:
        # Process arrival data
        arrival_info = []
        for item in arrivals:
            status_code = item.get("StopStatus", 0)
            eta_seconds = item.get("EstimatedArrivalTime")  # might be int (seconds)
            plate = item.get("PlateNumb", "-")

            if status_code == 3:
                arrival_info.append({
                    "plate": plate,
                    "label": "末班車已過",
                    "emoji": "⚫",
                    "seconds": 99999,
                })
            elif status_code == 1:
                arrival_info.append({
                    "plate": plate,
                    "label": "尚未發車",
                    "emoji": "⏸️",
                    "seconds": 99998,
                })
            elif status_code in [2, 4]:
                arrival_info.append({
                    "plate": plate,
                    "label": BUS_STATUS.get(status_code, ""),
                    "emoji": "⛔",
                    "seconds": 99997,
                })
            elif eta_seconds is not None:
                minutes = int(eta_seconds) // 60
                seconds_rem = int(eta_seconds) % 60

                if int(eta_seconds) <= 30:
                    label = "進站中"
                    emoji = "🔵"
                elif minutes <= 1:
                    label = "即將到站"
                    emoji = "🟢"
                elif minutes <= 5:
                    label = f"{minutes} 分鐘後到站"
                    emoji = "🟡"
                else:
                    label = f"{minutes} 分鐘後到站"
                    emoji = "🟠"

                arrival_info.append({
                    "plate": plate,
                    "label": label,
                    "emoji": emoji,
                    "seconds": int(eta_seconds),
                })

        # Sort by ETA
        arrival_info.sort(key=lambda x: x["seconds"])

        if not arrival_info:
            lines.append("⚠️ 暫無車輛資料")
        else:
            lines.append("🕐 到站預報")
            lines.append("")

            for i, info in enumerate(arrival_info[:3], 1):  # Show top 3
                plate_str = f"[{info['plate']}]" if info['plate'] and info['plate'] != "-" else "[車牌未回傳]"
                lines.append(f"{info['emoji']} 第 {i} 班")
                lines.append(f"   車牌：{plate_str}")
                lines.append(f"   狀態：{info['label']}")
                if i < len(arrival_info[:3]):
                    lines.append("")

    # Add real-time nearby buses info
    if buses:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━")
        lines.append("🚐 即時車輛位置")
        lines.append("")

        nearby = []
        for bus in buses:
            bus_status = bus.get("BusStatus", 0)
            if bus_status in [0, 1]:  # Normal or approaching
                plate = bus.get("PlateNumb", "-")
                current_stop = bus.get("StopName", {})
                if isinstance(current_stop, dict):
                    current_stop_name = current_stop.get("Zh_tw", "-")
                else:
                    current_stop_name = str(current_stop)
                seq = bus.get("StopSequence", 0)
                nearby.append({
                    "plate": plate,
                    "stop": current_stop_name,
                    "seq": seq,
                })

        if nearby and stop_info:
            target_seq = stop_info.get("sequence", 0)
            # Only show buses before the target stop
            before_target = [b for b in nearby if b["seq"] <= target_seq]
            before_target.sort(key=lambda x: x["seq"], reverse=True)  # Closest first

            if before_target:
                for bus in before_target[:3]:
                    stops_away = target_seq - bus["seq"]
                    plate_str = bus["plate"] if bus["plate"] != "-" else "未知"
                    if stops_away == 0:
                        lines.append(f"🔵 {plate_str}：正在 {bus['stop']}站（即將到站）")
                    elif stops_away == 1:
                        lines.append(f"🟢 {plate_str}：在 {bus['stop']}站（下一站即到）")
                    else:
                        lines.append(f"🟡 {plate_str}：在 {bus['stop']}站（還有 {stops_away} 站）")
            else:
                lines.append("目前無車輛行駛中")
        elif nearby:
            for bus in nearby[:3]:
                plate_str = bus["plate"] if bus["plate"] != "-" else "未知"
                lines.append(f"• {plate_str}：在 {bus['stop']}站")

    # Footer
    lines.append("")
    lines.append("━━━━━━━━━━━━━━")

    city_display = {
        "Taipei": "台北市",
        "NewTaipei": "新北市",
        "Taoyuan": "桃園市",
        "Taichung": "台中市",
        "Tainan": "台南市",
        "Kaohsiung": "高雄市",
        "Hsinchu": "新竹市",
        "HsinchuCounty": "新竹縣",
        "InterCity": "公路客運",
        "Keelung": "基隆市",
    }.get(city, city)

    lines.append(f"📡 {city_display} ｜ 資料：TDX")

    return TextMessage(text="\n".join(lines))
