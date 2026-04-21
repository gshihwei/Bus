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
        Determine direction (0 or 1) by matching direction_name against each
        direction's terminal stop name, derived directly from N1 DestinationStop fields.

        Each N1 record has DestinationStop = StopID of the route's last stop for
        that direction. We collect all distinct terminal StopIDs per direction,
        resolve them to stop names, then check if direction_name is contained in any.

        Example for 1728:
          Direction=0 records all have DestinationStop=300428 → "新竹轉運站"
          Direction=1 records all have DestinationStop=273770 → "仁愛敦化路口(圓環)"
          User says "往新竹" → "新竹" in "新竹轉運站" → Direction=0  ✓
        """
        def terminal_names(records: list) -> list[str]:
            dest_ids = {str(r.get("DestinationStop", "")) for r in records if r.get("DestinationStop")}
            names = []
            for sid in dest_ids:
                name = stopid_to_name.get(sid, "")
                if name:
                    names.append(name)
            return names

        names0 = terminal_names(n1_dir0)
        names1 = terminal_names(n1_dir1)

        # Check direction_name (e.g. "新竹") against terminal stop names
        if any(direction_name in name for name in names0):
            return 0
        if any(direction_name in name for name in names1):
            return 1

        # Fallback: if the queried stop only exists in one direction's N1, use that
        def has_stop(records, stop_name_unused):
            return len(records) > 0

        if n1_dir0 and not n1_dir1:
            return 0
        if n1_dir1 and not n1_dir0:
            return 1

        return 0  # final fallback

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
