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
        Get bus arrival estimate for a specific route, stop, and direction.
        Returns dict with arrival info or None if not found.
        """
        city = self.search_route_city(route_name)
        if city is None:
            return {"error": f"找不到路線「{route_name}」，請確認路線名稱是否正確"}

        # Determine direction value (0=去程, 1=返程) based on direction_name
        direction_value = self._resolve_direction(route_name, direction_name, city)

        # Get arrival estimates (EstimatedTimeOfArrival)
        arrivals = self._get_arrivals(route_name, stop_name, city, direction_value)

        # Get real-time bus positions
        buses = self._get_real_time_buses(route_name, city, direction_value)

        # Get stop sequence info
        stop_info = self._get_stop_info(route_name, stop_name, city, direction_value)

        return {
            "city": city,
            "direction_value": direction_value,
            "arrivals": arrivals,
            "buses": buses,
            "stop_info": stop_info,
        }

    def _resolve_direction(self, route_name: str, direction_name: str, city: str) -> int:
        """
        Determine direction (0 or 1) based on destination name.
        Compares against route's DestinationStopNameZh.
        """
        try:
            if city == "InterCity":
                url = f"{self.BASE_URL}/v2/Bus/Route/InterCity"
            else:
                url = f"{self.BASE_URL}/v2/Bus/Route/City/{city}"

            params = {
                "$filter": f"RouteName/Zh_tw eq '{route_name}'",
                "$select": "SubRoutes,DepartureStopNameZh,DestinationStopNameZh",
                "$format": "JSON",
                "$top": 5,
            }
            data = self._get(url, params)

            if data:
                route = data[0]
                dest = route.get("DestinationStopNameZh", "")
                dept = route.get("DepartureStopNameZh", "")

                # If direction matches destination → direction 1 (return)
                # Otherwise default to 0 (outbound)
                if direction_name and direction_name in dest:
                    return 1
                elif direction_name and direction_name in dept:
                    return 0

                # Check SubRoutes
                sub_routes = route.get("SubRoutes", [])
                for sr in sub_routes:
                    sr_dest = sr.get("DestinationStopNameZh", "")
                    sr_dept = sr.get("DepartureStopNameZh", "")
                    if direction_name in sr_dest:
                        return sr.get("Direction", 1)
                    if direction_name in sr_dept:
                        return sr.get("Direction", 0)

        except Exception:
            pass

        return 0  # Default to outbound

    def _get_arrivals(self, route_name: str, stop_name: str, city: str, direction: int) -> list:
        """Get ETA data for the stop"""
        try:
            if city == "InterCity":
                url = f"{self.BASE_URL}/v2/Bus/EstimatedTimeOfArrival/InterCity/{route_name}"
                params = {
                    "$filter": f"StopName/Zh_tw eq '{stop_name}' and Direction eq {direction}",
                    "$format": "JSON",
                }
            else:
                url = f"{self.BASE_URL}/v2/Bus/EstimatedTimeOfArrival/City/{city}/{route_name}"
                params = {
                    "$filter": f"StopName/Zh_tw eq '{stop_name}' and Direction eq {direction}",
                    "$format": "JSON",
                }
            return self._get(url, params) or []
        except Exception:
            return []

    def _get_real_time_buses(self, route_name: str, city: str, direction: int) -> list:
        """Get real-time bus positions (vehicle plates and current stop)"""
        try:
            if city == "InterCity":
                url = f"{self.BASE_URL}/v2/Bus/RealTimeByFrequency/InterCity/{route_name}"
                params = {
                    "$filter": f"Direction eq {direction}",
                    "$select": "PlateNumb,StopName,StopSequence,BusStatus,GPSTime",
                    "$format": "JSON",
                }
            else:
                url = f"{self.BASE_URL}/v2/Bus/RealTimeByFrequency/City/{city}/{route_name}"
                params = {
                    "$filter": f"Direction eq {direction}",
                    "$select": "PlateNumb,StopName,StopSequence,BusStatus,GPSTime",
                    "$format": "JSON",
                }
            return self._get(url, params) or []
        except Exception:
            # Try RealTimeNearStop
            try:
                if city == "InterCity":
                    url = f"{self.BASE_URL}/v2/Bus/RealTimeNearStop/InterCity/{route_name}"
                else:
                    url = f"{self.BASE_URL}/v2/Bus/RealTimeNearStop/City/{city}/{route_name}"
                params = {
                    "$filter": f"Direction eq {direction}",
                    "$select": "PlateNumb,StopName,StopSequence,BusStatus",
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
