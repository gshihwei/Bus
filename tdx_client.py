import time
import requests
from typing import Optional


class TDXClient:
    """
    TDX (Transport Data eXchange) API Client
    Handles OAuth2 authentication and bus arrival queries
    """

    TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    BASE_URL = "https://tdx.transportdata.tw/api/basic"

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None
        self._token_expiry = 0

    def _get_token(self) -> str:
        """Get or refresh OAuth2 access token"""
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        response = requests.post(
            self.TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        response.raise_for_status()
        token_data = response.json()

        self._token = token_data["access_token"]
        self._token_expiry = time.time() + token_data.get("expires_in", 3600)
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/json",
        }

    def _get(self, url: str, params: dict = None) -> dict:
        """Make authenticated GET request"""
        response = requests.get(
            url,
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def search_route_city(self, route_name: str) -> Optional[str]:
        """
        Search which city the route belongs to.
        Returns city code like 'Taipei', 'NewTaipei', 'Hsinchu', etc.
        First tries intercity (公路客運), then searches city bus.
        """
        # Check intercity bus first
        try:
            url = f"{self.BASE_URL}/v2/Bus/Route/InterCity"
            params = {
                "$filter": f"RouteName/Zh_tw eq '{route_name}'",
                "$select": "RouteID,RouteName,DepartureStopNameZh,DestinationStopNameZh",
                "$format": "JSON",
                "$top": 1,
            }
            data = self._get(url, params)
            if data:
                return "InterCity"
        except Exception:
            pass

        # Search city buses
        cities = [
            "Taipei", "NewTaipei", "Taoyuan", "Taichung",
            "Tainan", "Kaohsiung", "Keelung", "Hsinchu",
            "HsinchuCounty", "MiaoliCounty", "ChanghuaCounty",
            "NantouCounty", "YunlinCounty", "ChiayiCounty",
            "Chiayi", "PingtungCounty", "YilanCounty",
            "HualienCounty", "TaitungCounty"
        ]

        for city in cities:
            try:
                url = f"{self.BASE_URL}/v2/Bus/Route/City/{city}"
                params = {
                    "$filter": f"RouteName/Zh_tw eq '{route_name}'",
                    "$select": "RouteID,RouteName",
                    "$format": "JSON",
                    "$top": 1,
                }
                data = self._get(url, params)
                if data:
                    return city
            except Exception:
                continue

        return None

    def get_bus_arrival(self, route_name: str, stop_name: str, direction_name: str) -> Optional[dict]:
        """
        Get bus arrival info for a specific route, stop, and direction.

        Direction resolution strategy (most reliable):
          1. Fetch N1 for BOTH directions (Direction=0 and Direction=1) in parallel.
          2. Each N1 record carries DestinationStop (StopID of the terminal stop).
          3. Build a StopID→StopName map from the combined N1 data.
          4. For each direction (0 and 1), look up its terminal stop name and check
             whether direction_name is contained in it.
          5. This avoids relying on Route API field naming inconsistencies.
        """
        city = self.search_route_city(route_name)
        if city is None:
            return {"error": f"找不到路線「{route_name}」，請確認路線名稱是否正確"}

        # Fetch N1 for both directions at once (2 calls, but avoids Route API ambiguity)
        n1_dir0 = self._get_all_n1(route_name, city, 0)
        n1_dir1 = self._get_all_n1(route_name, city, 1)

        # Resolve direction first (uses DestinationStop from both directions' N1)
        # Build a temporary combined map just for direction resolution
        _combined_map: dict[str, str] = {}
        for rec in n1_dir0 + n1_dir1:
            sid = rec.get("StopID", "")
            sname_raw = rec.get("StopName", {})
            sname = sname_raw.get("Zh_tw", "") if isinstance(sname_raw, dict) else str(sname_raw)
            if sid and sname:
                _combined_map[sid] = sname

        direction_value = self._resolve_direction_from_n1(
            n1_dir0, n1_dir1, _combined_map, direction_name
        )

        if direction_value == -1:
            # Could not determine direction — collect all stop names from both
            # directions so we can show the user what keywords to use.
            all_stops_dir0 = sorted({
                r.get("StopName", {}).get("Zh_tw", "") for r in n1_dir0
                if r.get("StopSequence") in (1, 2)  # departure-end stops
                and r.get("StopName", {}).get("Zh_tw")
            })
            all_stops_dir1 = sorted({
                r.get("StopName", {}).get("Zh_tw", "") for r in n1_dir1
                if r.get("StopSequence") in (1, 2)
                and r.get("StopName", {}).get("Zh_tw")
            })
            hint0 = "、".join(all_stops_dir0) or "（無資料）"
            hint1 = "、".join(all_stops_dir1) or "（無資料）"
            msg = (
                "\u7121\u6cd5\u5224\u65b7\u300c"
                + direction_name +
                "\u300d\u7684\u884c\u99db\u65b9\u5411\u3002\n\n"
                "\u8acb\u6539\u7528\u8def\u7dda\u7d42\u9ede\u7ad9\u540d\u7a31\u7684\u95dc\u9375\u5b57\uff1a\n"
                + "\u2022 \u5f80 " + hint0 + " \u65b9\u5411\n"
                + "\u2022 \u5f80 " + hint1 + " \u65b9\u5411"
            )
            return {"error": msg}

        # Filter strictly to the chosen direction — TDX API sometimes returns
        # records from both directions even when filtered by Direction.
        _raw = n1_dir0 if direction_value == 0 else n1_dir1
        all_n1 = [r for r in _raw if r.get("Direction") == direction_value]

        # Build stopid_to_name ONLY from the chosen direction's N1.
        # CurrentStop values in N1 records are StopIDs from the same direction,
        # so mixing both directions would produce wrong stop name lookups.
        stopid_to_name: dict[str, str] = {}
        for rec in all_n1:
            sid = rec.get("StopID", "")
            sname_raw = rec.get("StopName", {})
            sname = sname_raw.get("Zh_tw", "") if isinstance(sname_raw, dict) else str(sname_raw)
            if sid and sname:
                stopid_to_name[sid] = sname

        stop_info = self._get_stop_info(route_name, stop_name, city, direction_value)

        return {
            "city":            city,
            "direction_value": direction_value,
            "all_n1":          all_n1,
            "stopid_to_name":  stopid_to_name,
            "stop_info":       stop_info,
        }

    def _resolve_direction_from_n1(
        self,
        n1_dir0: list,
        n1_dir1: list,
        stopid_to_name: dict,
        direction_name: str,
    ) -> int:
        """
        Determine direction (0 or 1) from N1 data using a two-pass strategy:

        Pass 1 — Terminal stop name match:
          Each record carries DestinationStop (StopID of the last stop).
          If direction_name is contained in either direction's terminal stop name, use it.
          Example: "往新竹" → "新竹" in "新竹轉運站" → Direction=0 ✓

        Pass 2 — All stop names match:
          If the terminal stop name doesn't contain direction_name
          (e.g. "往台北" but terminal is "仁愛敦化路口(圓環)"),
          check whether direction_name appears in ANY stop name in that direction's N1.
          Example: "往台北" → "台北" in "捷運大坪林站"? No...
                             "台北" in "仁愛敦化路口"? No...
          This also fails for "台北", so we add a third pass.

        Pass 3 — Departure stop match:
          The N1 record with the smallest StopSequence is the departure stop.
          Match direction_name against it too. This handles cases like:
          "往台北" where the route starts from a Taipei-area station.

        Final fallback: Direction=0.
        """
        def stop_names(records: list) -> list[str]:
            seen = set()
            names = []
            for r in records:
                raw = r.get("StopName", {})
                name = raw.get("Zh_tw", "") if isinstance(raw, dict) else str(raw)
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
            return names

        def terminal_names(records: list) -> list[str]:
            dest_ids = {str(r.get("DestinationStop", "")) for r in records if r.get("DestinationStop")}
            return [stopid_to_name.get(sid, "") for sid in dest_ids if stopid_to_name.get(sid)]

        # Pass 1: terminal stop name
        t0, t1 = terminal_names(n1_dir0), terminal_names(n1_dir1)
        if any(direction_name in n for n in t0):
            return 0
        if any(direction_name in n for n in t1):
            return 1

        # Pass 2: any stop name in the direction
        s0, s1 = stop_names(n1_dir0), stop_names(n1_dir1)
        if any(direction_name in n for n in s0):
            return 0
        if any(direction_name in n for n in s1):
            return 1

        # Pass 3: departure stop (smallest StopSequence) name
        def departure_name(records: list) -> str:
            valid = [r for r in records if r.get("StopSequence") is not None]
            if not valid:
                return ""
            first = min(valid, key=lambda r: r["StopSequence"])
            raw = first.get("StopName", {})
            return raw.get("Zh_tw", "") if isinstance(raw, dict) else str(raw)

        d0, d1 = departure_name(n1_dir0), departure_name(n1_dir1)
        if d0 and direction_name in d0:
            return 0
        if d1 and direction_name in d1:
            return 1

        # Basic fallback by data presence
        if n1_dir0 and not n1_dir1:
            return 0
        if n1_dir1 and not n1_dir0:
            return 1

        # Pass 4: try matching direction_name against common city aliases
        # This handles cases where users type a city name (e.g. "台北")
        # that doesn't appear verbatim in any stop name on the route.
        # Strategy: the direction whose DEPARTURE stop (seq=1) area matches
        # direction_name wins. We infer this by checking if the OTHER direction's
        # terminal stop name contains direction_name's opposite.
        # 
        # Simpler heuristic: if dir0's all stop names contain more matches for
        # direction_name than dir1's, pick dir0, else dir1.
        # 
        # Most practical fix: check if direction_name partially matches
        # the departure station of either direction using a broad token search.
        # Since "台北" won't match any stop, fall back to checking which direction
        # has its departure stop (StopSequence=1) in the general area.
        # 
        # We expose this as a clear error to the user instead of guessing wrong.
        # Return a special sentinel -1 to signal "ambiguous direction".
        return -1  # signals: could not determine direction from stop names

    def _get_all_n1(self, route_name: str, city: str, direction: int) -> list:
        """
        Fetch the full EstimatedTimeOfArrival (N1) for this route+direction.
        Returns all records (every vehicle × every upcoming stop).
        Each record has:
          PlateNumb     – vehicle plate ("-1" if no vehicle assigned)
          StopName      – upcoming stop name
          StopSequence  – upcoming stop's sequence on the route
          EstimateTime  – seconds until arrival at that stop (absent when StopStatus≠0)
          StopStatus    – 0=normal, 1=not yet departed, 2=no service, 3=last bus passed
          CurrentStop   – StopID of where this vehicle currently is (key field!)
          Direction     – 0 outbound / 1 inbound
        """
        try:
            if city == "InterCity":
                url = f"{self.BASE_URL}/v2/Bus/EstimatedTimeOfArrival/InterCity/{route_name}"
            else:
                url = f"{self.BASE_URL}/v2/Bus/EstimatedTimeOfArrival/City/{city}/{route_name}"
            params = {
                "$filter": f"Direction eq {direction}",
                "$format": "JSON",
            }
            return self._get(url, params) or []
        except Exception:
            return []

    def _get_stop_info(self, route_name: str, stop_name: str, city: str, direction: int) -> Optional[dict]:
        """
        Get stop sequence info AND the full ordered stop list for ETA estimation.
        Returns dict with target stop's sequence, total stops, and all stops in order.
        """
        try:
            if city == "InterCity":
                url = f"{self.BASE_URL}/v2/Bus/StopOfRoute/InterCity/{route_name}"
            else:
                url = f"{self.BASE_URL}/v2/Bus/StopOfRoute/City/{city}/{route_name}"

            params = {
                "$filter": f"Direction eq {direction}",
                "$format": "JSON",
            }
            data = self._get(url, params)

            if data:
                stops = data[0].get("Stops", [])
                # Build ordered list: [{seq, name}, ...]
                ordered = []
                target_seq = None
                for stop in stops:
                    seq = stop.get("StopSequence", 0)
                    name = stop.get("StopName", {}).get("Zh_tw", "")
                    ordered.append({"seq": seq, "name": name})
                    if name == stop_name:
                        target_seq = seq

                ordered.sort(key=lambda x: x["seq"])

                if target_seq is not None:
                    return {
                        "sequence": target_seq,
                        "total": len(stops),
                        "all_stops": ordered,  # full ordered stop list
                    }
        except Exception:
            pass
        return None
