"""
高德地图 API 工具函数集
======================
封装了地理编码、路线规划、POI 搜索等底层高德 REST API 调用。
所有函数均为纯函数，不依赖 Flask 上下文。
"""

import math
import datetime
import os
import requests
from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
AMAP_KEY: str         = os.getenv("AMAP_KEY", "")
AMAP_JS_KEY: str      = os.getenv("AMAP_JS_KEY", AMAP_KEY)


# ─────────────────────────────────────────────
# 地理编码
# ─────────────────────────────────────────────

def amap_geocode(address: str) -> dict:
    """地址 → 经纬度坐标"""
    url = "https://restapi.amap.com/v3/geocode/geo"
    params = {"key": AMAP_KEY, "address": address, "output": "json"}
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("status") == "1" and data.get("geocodes"):
        geocode = data["geocodes"][0]
        lng, lat = geocode["location"].split(",")
        return {
            "success": True,
            "lng": float(lng),
            "lat": float(lat),
            "formatted_address": geocode.get("formatted_address", address),
            "city": geocode.get("city", ""),
        }
    return {"success": False, "error": data.get("info", "地理编码失败")}


# ─────────────────────────────────────────────
# 出发时间解析
# ─────────────────────────────────────────────

def _parse_departure_time(departure_time: str | None) -> tuple[str, str]:
    """
    解析出发时间字符串，返回 (date_str, time_str)。
    格式：'HH:MM' 或 'YYYY-MM-DD HH:MM'，None 时默认下一工作日中午 12:00。
    """
    now = datetime.datetime.now()

    if not departure_time:
        candidate = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if now.hour >= 12:
            candidate += datetime.timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += datetime.timedelta(days=1)
        return candidate.strftime("%Y-%m-%d"), "12:00"

    departure_time = departure_time.strip()
    if len(departure_time) == 5 and ":" in departure_time:
        h, m = map(int, departure_time.split(":"))
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += datetime.timedelta(days=1)
        return candidate.strftime("%Y-%m-%d"), departure_time
    else:
        try:
            dt = datetime.datetime.strptime(departure_time, "%Y-%m-%d %H:%M")
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except ValueError:
            return now.strftime("%Y-%m-%d"), "12:00"


# ─────────────────────────────────────────────
# 路线规划
# ─────────────────────────────────────────────

def parse_polyline(polyline: str) -> list[list[float]]:
    """把高德 polyline 字符串压缩为可直接绘图的坐标数组。"""
    points: list[list[float]] = []
    for item in (polyline or "").split(";"):
        parts = item.split(",")
        if len(parts) != 2:
            continue
        try:
            point = [float(parts[0]), float(parts[1])]
        except (TypeError, ValueError):
            continue
        if not points or points[-1] != point:
            points.append(point)
    return points


def build_step_geometry(steps: list, mode: str) -> list[dict]:
    """从驾车/步行/骑行步骤中提取非空轨迹段。"""
    segments = []
    for step in steps or []:
        path = parse_polyline(step.get("polyline", ""))
        if len(path) >= 2:
            segments.append({"mode": mode, "path": path})
    return segments


def build_route_geometry(mode: str, segments: list[dict]) -> dict | None:
    """生成前端绘图需要的最小路线结构。"""
    valid = [
        segment for segment in segments
        if len(segment.get("path") or []) >= 2
    ]
    return {"mode": mode, "segments": valid} if valid else None


def build_transit_geometry(transit: dict) -> dict | None:
    """提取公交方案里的步行段与公交/地铁段。"""
    segments: list[dict] = []
    for segment in transit.get("segments", []):
        walking = segment.get("walking") or {}
        segments.extend(
            build_step_geometry(walking.get("steps", []), "walking")
        )
        for busline in (segment.get("bus") or {}).get("buslines", []):
            path = parse_polyline(busline.get("polyline", ""))
            if len(path) >= 2:
                segments.append({"mode": "transit", "path": path})
    return build_route_geometry("transit", segments)


def amap_driving_route(
    origin_lng: float, origin_lat: float,
    dest_lng: float, dest_lat: float,
    strategy: int = 0,
) -> dict:
    """驾车路线规划"""
    url = "https://restapi.amap.com/v3/direction/driving"
    params = {
        "key": AMAP_KEY,
        "origin": f"{origin_lng},{origin_lat}",
        "destination": f"{dest_lng},{dest_lat}",
        "strategy": strategy,
        "extensions": "all",
        "output": "json",
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("status") == "1" and data.get("route", {}).get("paths"):
        path = data["route"]["paths"][0]
        duration_s = int(path.get("duration", 0))
        distance_m = int(path.get("distance", 0))
        return {
            "success": True,
            "mode": "driving",
            "duration_minutes": round(duration_s / 60),
            "distance_km": round(distance_m / 1000, 1),
            "duration_text": f"{round(duration_s / 60)}分钟",
            "distance_text": f"{round(distance_m / 1000, 1)}公里",
            "geometry": build_route_geometry(
                "driving",
                build_step_geometry(path.get("steps", []), "driving"),
            ),
        }
    return {"success": False, "error": "驾车路线规划失败", "mode": "driving"}


def amap_transit_route(
    origin_lng: float, origin_lat: float,
    dest_lng: float, dest_lat: float,
    city: str = "北京",
    departure_time: str | None = None,
    strategy: int = 0,
    origin_poi: str | None = None,
    destination_poi: str | None = None,
) -> dict:
    """V5 公交 / 地铁路线规划，支持 0-8 换乘策略。"""
    if strategy == 6 and (not origin_poi or not destination_poi):
        return {"success": False, "error": "地铁图模式需要起终点 POI ID", "mode": "transit"}

    city_codes = {
        "北京": "110000", "上海": "310000", "广州": "440100",
        "深圳": "440300", "杭州": "330100",
    }
    city_code = city_codes.get(city, city)
    date_str, time_str = _parse_departure_time(departure_time)
    url = "https://restapi.amap.com/v5/direction/transit/integrated"
    params = {
        "key": AMAP_KEY,
        "origin": f"{origin_lng},{origin_lat}",
        "destination": f"{dest_lng},{dest_lat}",
        "city1": city_code,
        "city2": city_code,
        "strategy": strategy,
        "AlternativeRoute": 1,
        "nightflag": 0,
        "date": date_str,
        "time": time_str,
        "show_fields": "cost,navi",
        "output": "json",
    }
    if strategy == 6:
        params.update({"originpoi": origin_poi, "destinationpoi": destination_poi})
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    transits = data.get("route", {}).get("transits", [])
    if data.get("status") == "1" and transits:
        transit = transits[0]
        cost = transit.get("cost") or {}
        duration_s = int(cost.get("duration", 0) or transit.get("duration", 0))

        lines = []
        for seg in transit.get("segments", []):
            walking = seg.get("walking") or {}
            walk_s = int(
                (walking.get("cost") or {}).get("duration", 0)
                or walking.get("duration", 0)
            )
            if walk_s >= 60:
                lines.append(f"步行{round(walk_s / 60)}分钟")
            for line in (seg.get("bus") or {}).get("buslines", []):
                if line.get("name"):
                    lines.append(line["name"])

        distance_m = int(transit.get("distance", 0) or 0)
        return {
            "success": True,
            "mode": "transit",
            "strategy": strategy,
            "duration_minutes": round(duration_s / 60),
            "distance_km": round(distance_m / 1000, 1),
            "duration_text": f"{round(duration_s / 60)}分钟",
            "distance_text": f"{round(distance_m / 1000, 1)}公里",
            "line_summary": " → ".join(lines) or "公共交通",
            "geometry": build_transit_geometry(transit),
        }
    return {
        "success": False,
        "error": data.get("info", "公交路线规划失败"),
        "mode": "transit",
    }


def amap_walking_route(
    origin_lng: float, origin_lat: float,
    dest_lng: float, dest_lat: float,
) -> dict:
    """步行路线规划"""
    url = "https://restapi.amap.com/v3/direction/walking"
    params = {
        "key": AMAP_KEY,
        "origin": f"{origin_lng},{origin_lat}",
        "destination": f"{dest_lng},{dest_lat}",
        "extensions": "all",
        "output": "json",
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("status") == "1" and data.get("route", {}).get("paths"):
        path = data["route"]["paths"][0]
        duration_s = int(path.get("duration", 0))
        distance_m = int(path.get("distance", 0))
        return {
            "success": True,
            "mode": "walking",
            "duration_minutes": round(duration_s / 60),
            "distance_km": round(distance_m / 1000, 1),
            "duration_text": f"{round(duration_s / 60)}分钟",
            "distance_text": f"{round(distance_m / 1000, 1)}公里",
            "geometry": build_route_geometry(
                "walking",
                build_step_geometry(path.get("steps", []), "walking"),
            ),
        }
    return {"success": False, "error": "步行路线规划失败", "mode": "walking"}


def amap_cycling_route(
    origin_lng: float, origin_lat: float,
    dest_lng: float, dest_lat: float,
) -> dict:
    """骑行（共享单车 / 自行车）路线规划"""
    url = "https://restapi.amap.com/v4/direction/bicycling"
    params = {
        "key": AMAP_KEY,
        "origin": f"{origin_lng},{origin_lat}",
        "destination": f"{dest_lng},{dest_lat}",
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("errcode") == 0:
        paths = data.get("data", {}).get("paths", [])
        if paths:
            path = paths[0]
            duration_s = int(path.get("duration", 0))
            distance_m = int(path.get("distance", 0))
            return {
                "success": True,
                "mode": "cycling",
                "duration_minutes": round(duration_s / 60),
                "distance_km": round(distance_m / 1000, 1),
                "duration_text": f"{round(duration_s / 60)}分钟",
                "distance_text": f"{round(distance_m / 1000, 1)}公里",
                "geometry": build_route_geometry(
                    "cycling",
                    build_step_geometry(path.get("steps", []), "cycling"),
                ),
            }
    return {"success": False, "error": "骑行路线规划失败", "mode": "cycling"}


def haversine_distance(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """计算两点间 Haversine 直线距离（千米）"""
    R = 6371
    dlng = math.radians(lng2 - lng1)
    dlat = math.radians(lat2 - lat1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


def amap_get_best_route(
    origin_lng: float, origin_lat: float,
    dest_lng: float, dest_lat: float,
    city: str = "北京",
    prefer: str = "auto",
    departure_time: str | None = None,
    transit_strategy: int = 0,
    origin_poi: str | None = None,
    destination_poi: str | None = None,
) -> dict:
    """
    自动对比多种交通方式，返回最快方案。
    prefer: auto | transit | driving | walking | cycling
    """
    dist_km = haversine_distance(origin_lng, origin_lat, dest_lng, dest_lat)

    if prefer == "driving":
        return amap_driving_route(origin_lng, origin_lat, dest_lng, dest_lat)
    if prefer == "walking":
        return amap_walking_route(origin_lng, origin_lat, dest_lng, dest_lat)
    if prefer == "cycling":
        return amap_cycling_route(origin_lng, origin_lat, dest_lng, dest_lat)

    results: dict[str, dict] = {}

    if dist_km < 2.5:
        r = amap_walking_route(origin_lng, origin_lat, dest_lng, dest_lat)
        if r.get("success"):
            results["walking"] = r

    if prefer in ("auto", "cycling") and dist_km < 8:
        r = amap_cycling_route(origin_lng, origin_lat, dest_lng, dest_lat)
        if r.get("success"):
            results["cycling"] = r

    if prefer in ("auto", "transit"):
        r = amap_transit_route(origin_lng, origin_lat, dest_lng, dest_lat,
                               city, departure_time, transit_strategy,
                               origin_poi, destination_poi)
        if r.get("success"):
            results["transit"] = r

    if prefer == "auto":
        r = amap_driving_route(origin_lng, origin_lat, dest_lng, dest_lat)
        if r.get("success"):
            results["driving"] = r

    if not results:
        return {"success": False, "error": "所有交通方式均查询失败", "mode": "unknown"}

    best = min(results.values(), key=lambda x: x.get("duration_minutes", 9999))
    best["all_modes"] = {
        mode: {
            "duration_minutes": r.get("duration_minutes"),
            "duration_text": r.get("duration_text"),
            "distance_text": r.get("distance_text"),
            "line_summary": r.get("line_summary", ""),
        }
        for mode, r in results.items()
    }
    return best


# ─────────────────────────────────────────────
# POI 搜索
# ─────────────────────────────────────────────

def amap_search_nearby(
    center_lng: float, center_lat: float,
    keyword: str,
    radius: int = 3000,
    sort_by: str = "distance",
) -> dict:
    """周边 POI 搜索"""
    url = "https://restapi.amap.com/v3/place/around"
    params = {
        "key": AMAP_KEY,
        "location": f"{center_lng},{center_lat}",
        "keywords": keyword,
        "radius": radius,
        "sortrule": sort_by,
        "output": "json",
        "offset": 25,
        "page": 1,
        "extensions": "all",
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("status") == "1":
        pois = []
        for poi in data.get("pois", []):
            location = poi.get("location", "").split(",")
            if len(location) != 2:
                continue
            try:
                rating = float(poi.get("biz_ext", {}).get("rating", 0) or 0)
                cost = poi.get("biz_ext", {}).get("cost", "")
                pois.append({
                    "id": poi.get("id", ""),
                    "name": poi.get("name", ""),
                    "address": poi.get("address", ""),
                    "lng": float(location[0]),
                    "lat": float(location[1]),
                    "rating": rating,
                    "cost_per_person": cost,
                    "type": poi.get("type", ""),
                    "tel": poi.get("tel", ""),
                    "distance": int(poi.get("distance", 0)),
                    "photos": [p.get("url", "") for p in poi.get("photos", [])[:2]],
                })
            except (ValueError, TypeError):
                pass
        return {"success": True, "count": len(pois), "pois": pois}
    return {"success": False, "error": data.get("info", "搜索失败"), "pois": []}


# ─────────────────────────────────────────────
# 中点计算
# ─────────────────────────────────────────────

def find_balanced_midpoint(
    lng1: float, lat1: float, lng2: float, lat2: float
) -> dict:
    """
    计算两点地理中点（经纬度平均值），并根据两地直线距离给出建议搜索半径。
    建议半径 = 两地距离 × 35%，最小 500 m，最大 8 km。
    """
    mid_lng = (lng1 + lng2) / 2
    mid_lat = (lat1 + lat2) / 2
    dist_km = haversine_distance(lng1, lat1, lng2, lat2)
    radius_m = int(max(500, min(8000, dist_km * 1000 * 0.35)))
    return {
        "midpoint": {"lng": mid_lng, "lat": mid_lat},
        "total_distance_km": round(dist_km, 1),
        "suggested_search_radius_m": radius_m,
        "note": (
            f"两地直线距离 {round(dist_km, 1)}km，"
            f"建议以中点为圆心搜索半径 {radius_m}m 内的地点。"
            f"如果结果不理想可适当扩大半径。"
        ),
    }


def _normalize_prefer(prefer: str | None) -> str:
    return (prefer or "auto").lower()


def should_force_geo_midpoint(prefer_a: str, prefer_b: str) -> bool:
    """
    A/B 出行方式不一致，或双方都选骑行/步行/其他时，固定使用地理中点。
    """
    a = _normalize_prefer(prefer_a)
    b = _normalize_prefer(prefer_b)
    return a != b or a not in ("auto", "driving", "transit")


def inspect_water_or_sea_point(lng: float, lat: float) -> dict:
    """
    轻量判断坐标是否明显落在水域/海域。

    高德逆地理编码在海域场景可能返回 seaArea；湖泊/河流则更常出现在
    formatted_address / AOI 名称中。这里采用保守规则：只有明确出现水域信号时
    才触发交通中点，避免把临水道路误判为水中。
    """
    water_keywords = ("海", "湾", "湖", "河", "江", "水库", "水域", "港", "渠")
    url = "https://restapi.amap.com/v3/geocode/regeo"
    params = {
        "key": AMAP_KEY,
        "location": f"{lng},{lat}",
        "radius": 50,
        "extensions": "all",
        "output": "json",
    }
    try:
        data = requests.get(url, params=params, timeout=8).json()
    except Exception as e:
        return {"success": False, "is_water": False, "reason": f"逆地理编码失败：{e}"}

    if data.get("status") != "1":
        return {
            "success": False,
            "is_water": False,
            "reason": data.get("info", "逆地理编码失败"),
        }

    regeocode = data.get("regeocode") or {}
    component = regeocode.get("addressComponent") or {}
    sea_area = component.get("seaArea")
    if sea_area:
        return {"success": True, "is_water": True, "reason": f"命中海域：{sea_area}"}

    formatted = str(regeocode.get("formatted_address", ""))
    aois = regeocode.get("aois") or []
    water_names = [str(aoi.get("name", "")) for aoi in aois]
    has_water_name = any(
        any(keyword in name for keyword in water_keywords)
        for name in [formatted, *water_names]
    )
    has_land_signal = bool(component.get("township") or component.get("streetNumber"))
    if has_water_name and not has_land_signal:
        return {"success": True, "is_water": True, "reason": "逆地理编码显示为水域"}

    return {"success": True, "is_water": False, "reason": "未命中明确水域信号"}


def transit_duration_to_point(
    origin_lng: float, origin_lat: float,
    dest_lng: float, dest_lat: float,
    city: str = "北京",
    departure_time: str | None = None,
) -> dict:
    """返回公共交通到某坐标点的耗时，用于中点公平性判断。"""
    route = amap_transit_route(
        origin_lng, origin_lat,
        dest_lng, dest_lat,
        city=city,
        departure_time=departure_time,
    )
    if not route.get("success"):
        return route
    return {
        "success": True,
        "duration_minutes": route.get("duration_minutes", 0),
        "duration_text": route.get("duration_text", ""),
        "line_summary": route.get("line_summary", ""),
    }


def _is_subway_line(line: dict) -> bool:
    text = " ".join(
        str(line.get(key, ""))
        for key in ("name", "type", "bus_type", "line_type")
    )
    subway_keywords = ("地铁", "轨道交通", "轻轨", "磁悬浮", "市域铁路", "城际")
    return any(keyword in text for keyword in subway_keywords)


def _station_from_stop(stop: dict, line_name: str, cum_seconds: float) -> dict | None:
    loc = _parse_location(stop.get("location", ""))
    if not loc:
        return None
    return {
        "name": stop.get("name", ""),
        "lng": loc[0],
        "lat": loc[1],
        "line_name": line_name,
        "cum_seconds": cum_seconds,
    }


def _build_subway_station_timeline(transit: dict) -> list[dict]:
    """
    从高德公交方案里提取地铁/轨道交通站点时间轴。

    高德公交路线只给整段线路 duration，没有每站耗时；这里按站间数把线路
    duration 均分到 departure_stop / via_stops / arrival_stop 上。这个近似
    对“取半程附近前后站”足够稳定，也避免把中点落在隧道/线路 polyline 上。
    """
    timeline: list[dict] = []
    cum = 0.0

    for seg in transit.get("segments", []):
        walking = seg.get("walking") or {}
        cum += int(walking.get("duration", 0) or 0)

        bus = seg.get("bus") or {}
        for line in bus.get("buslines", []):
            line_duration = int(line.get("duration", 0) or 0)
            line_name = line.get("name", "")

            if not _is_subway_line(line):
                cum += line_duration
                continue

            stops = []
            dep = line.get("departure_stop") or {}
            arr = line.get("arrival_stop") or {}
            if dep:
                stops.append(dep)
            via_stops = line.get("via_stops") or []
            if isinstance(via_stops, list):
                stops.extend(via_stops)
            if arr:
                stops.append(arr)

            stops = [stop for stop in stops if _parse_location(stop.get("location", ""))]
            if len(stops) < 2:
                cum += line_duration
                continue

            interval_count = len(stops) - 1
            for idx, stop in enumerate(stops):
                station_time = cum + (line_duration * idx / interval_count)
                station = _station_from_stop(stop, line_name, station_time)
                if station:
                    timeline.append(station)

            cum += line_duration

    timeline.sort(key=lambda station: station["cum_seconds"])
    return timeline


def find_transit_route_subway_midpoint(
    lng1: float, lat1: float,
    lng2: float, lat2: float,
    city: str = "北京",
    departure_time: str | None = None,
    geo_midpoint: dict | None = None,
    search_radius_m: int = 3000,
) -> dict:
    """
    沿 A->B 公共交通方案取半程附近地铁站：
      1. 调高德 A->B 公交路线；
      2. 提取地铁/轨道交通站点时间轴；
      3. 取半程时间之前最近的站，以及刚过半程时间的站；
      4. 返回两个站坐标的地理中点。
    """
    geo = geo_midpoint or find_balanced_midpoint(lng1, lat1, lng2, lat2)
    date_str, time_str = _parse_departure_time(departure_time)
    try:
        data = requests.get(
            "https://restapi.amap.com/v3/direction/transit/integrated",
            params={
                "key": AMAP_KEY,
                "origin": f"{lng1},{lat1}",
                "destination": f"{lng2},{lat2}",
                "city": city,
                "strategy": 0,
                "nightflag": 0,
                "date": date_str,
                "time": time_str,
                "output": "json",
            },
            timeout=10,
        ).json()
    except Exception as e:
        return {"success": False, "error": f"公交路线请求失败：{e}"}

    transits = (
        data.get("route", {}).get("transits")
        if data.get("status") == "1" else None
    )
    if not transits:
        return {"success": False, "error": data.get("info", "无可用公共交通路线")}

    transit = transits[0]
    total_seconds = int(transit.get("duration", 0) or 0)
    stations = _build_subway_station_timeline(transit)
    if total_seconds <= 0 or not stations:
        return {"success": False, "error": "公共交通路线中未提取到可用地铁站"}

    half_seconds = total_seconds / 2.0
    before_candidates = [
        station for station in stations
        if station["cum_seconds"] <= half_seconds
    ]
    after_candidates = [
        station for station in stations
        if station["cum_seconds"] > half_seconds
    ]

    before = before_candidates[-1] if before_candidates else stations[0]
    after = after_candidates[0] if after_candidates else before

    midpoint = {
        "lng": (before["lng"] + after["lng"]) / 2,
        "lat": (before["lat"] + after["lat"]) / 2,
    }
    total_min = round(total_seconds / 60)
    before_min = round(before["cum_seconds"] / 60)
    after_min = round(after["cum_seconds"] / 60)
    radius_m = max(800, min(search_radius_m, 3000))

    if before["name"] == after["name"]:
        station_text = f"「{before['name']}」"
    else:
        station_text = f"「{before['name']}」与「{after['name']}」"

    return {
        "success": True,
        "midpoint": midpoint,
        "total_distance_km": geo.get("total_distance_km"),
        "suggested_search_radius_m": radius_m,
        "method": "transit_route_subway_midpoint",
        "transport_total_minutes": total_min,
        "before_station": before,
        "after_station": after,
        "station_pair": [before.get("name", ""), after.get("name", "")],
        "midpoint_at_minutes": round(half_seconds / 60),
        "note": (
            f"A 到 B 公共交通全程约 {total_min} 分钟，"
            f"已取半程前后地铁站{station_text}的地理中点作为交通中点"
            f"（约第 {before_min} 分钟 / 第 {after_min} 分钟）。"
            f"建议以此为圆心搜索半径 {radius_m}m 内的地点。"
        ),
    }


def find_adaptive_midpoint(
    lng1: float, lat1: float,
    lng2: float, lat2: float,
    city: str = "北京",
    departure_time: str | None = None,
    prefer_a: str = "auto",
    prefer_b: str = "auto",
    max_transit_diff_minutes: int = 30,
) -> dict:
    """
    app_v2 搜索中点策略：
      - 默认使用地理中点；
      - A/B 方式不一致，或双方都是骑行/步行/其他时，固定地理中点；
      - 否则若地理中点落在水域/海域，或 A/B 到地理中点的公共交通耗时差
        超过阈值，则改用 A->B 公共交通路线半程附近的地铁站中点。
    """
    geo = find_balanced_midpoint(lng1, lat1, lng2, lat2)
    geo["method"] = "geo"

    if should_force_geo_midpoint(prefer_a, prefer_b):
        geo["decision_reason"] = "出行方式不一致或不适合公共交通兜底，使用地理中点"
        return geo

    center = geo["midpoint"]
    triggers: list[str] = []

    water = inspect_water_or_sea_point(center["lng"], center["lat"])
    if water.get("is_water"):
        triggers.append(water.get("reason", "地理中点位于水域/海域"))

    route_a = transit_duration_to_point(
        lng1, lat1, center["lng"], center["lat"], city, departure_time
    )
    route_b = transit_duration_to_point(
        lng2, lat2, center["lng"], center["lat"], city, departure_time
    )
    if route_a.get("success") and route_b.get("success"):
        minutes_a = int(route_a.get("duration_minutes", 0) or 0)
        minutes_b = int(route_b.get("duration_minutes", 0) or 0)
        diff = abs(minutes_a - minutes_b)
        geo["geo_transit_minutes_a"] = minutes_a
        geo["geo_transit_minutes_b"] = minutes_b
        geo["geo_transit_time_diff_minutes"] = diff
        if diff > max_transit_diff_minutes:
            triggers.append(
                f"双方到地理中点的公共交通时间差 {diff} 分钟，超过 "
                f"{max_transit_diff_minutes} 分钟"
            )
    elif not triggers:
        geo["decision_reason"] = "无法评估到地理中点的公共交通时间，使用地理中点"
        return geo

    if not triggers:
        geo["decision_reason"] = "地理中点未命中水域，且公共交通时间差未超过阈值"
        return geo

    station_result = find_transit_route_subway_midpoint(
        lng1, lat1, lng2, lat2,
        city=city,
        departure_time=departure_time,
        geo_midpoint=geo,
        search_radius_m=geo.get("suggested_search_radius_m", 3000),
    )
    if station_result.get("success"):
        station_result["decision_reason"] = "；".join(triggers)
        return station_result

    geo["fallback_reason"] = (
        "；".join(triggers)
        + f"，但未找到可用交通中点：{station_result.get('error', '未知原因')}"
    )
    return geo


def _parse_location(loc: str) -> tuple[float, float] | None:
    """解析高德 'lng,lat' 字符串为 (lng, lat)，失败返回 None"""
    if not loc or not isinstance(loc, str):
        return None
    parts = loc.split(",")
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except (ValueError, TypeError):
        return None


def _iter_polyline_points(polyline: str):
    """把高德 'lng,lat;lng,lat;...' 坐标串逐点 yield 出 (lng, lat)"""
    if not polyline or not isinstance(polyline, str):
        return
    for pair in polyline.split(";"):
        pt = _parse_location(pair)
        if pt:
            yield pt


def _build_transit_timeline(transit: dict) -> list[dict]:
    """
    把一条公交/地铁方案展开成按时间顺序排列的「轨迹节点」序列。
    每个节点形如：{"lng", "lat", "cum_seconds"}
    cum_seconds 为从 A 出发到达该轨迹点的累计耗时（秒）。

    沿 segments 顺序累加每段耗时。对每条公交线路：
      - 优先用该段的 polyline（乘车轨迹坐标串），把乘车耗时均摊到每个坐标点，
        得到沿线路连续稠密的「时间-坐标」节点。这样时间中点能精确落在
        「全程时间一半」对应的轨迹位置，而不是只能在上/下车站之间二选一。
      - 若 polyline 缺失，退化为只在上车站、下车站打点（坐标取站点 location）。
    步行段只贡献耗时（其两端通常紧邻公交站，不单独打点）。
    """
    timeline: list[dict] = []
    cum = 0.0

    for seg in transit.get("segments", []):
        walking = seg.get("walking") or {}
        cum += int(walking.get("duration", 0) or 0)

        bus = seg.get("bus") or {}
        for line in bus.get("buslines", []):
            dep = line.get("departure_stop") or {}
            arr = line.get("arrival_stop") or {}
            line_duration = int(line.get("duration", 0) or 0)

            # 上车站打点（乘车开始时刻）
            dep_loc = _parse_location(dep.get("location", ""))
            if dep_loc:
                timeline.append({
                    "lng": dep_loc[0], "lat": dep_loc[1], "cum_seconds": cum,
                })

            # 乘车段：优先用 polyline 把耗时均摊到每个坐标点（连续稠密）
            points = list(_iter_polyline_points(line.get("polyline", "")))
            if points and line_duration > 0:
                per_point = line_duration / len(points)
                for pt in points:
                    cum += per_point
                    timeline.append({
                        "lng": pt[0], "lat": pt[1], "cum_seconds": cum,
                    })
            else:
                # 无 polyline：只能整体累加乘车耗时
                cum += line_duration

            # 下车站打点（乘车结束时刻）
            arr_loc = _parse_location(arr.get("location", ""))
            if arr_loc:
                timeline.append({
                    "lng": arr_loc[0], "lat": arr_loc[1], "cum_seconds": cum,
                })

    return timeline


def _build_driving_timeline(path: dict) -> list[dict]:
    """
    把一条驾车 path 展开成按时间顺序排列的「轨迹节点」序列。
    每个节点形如：{"lng", "lat", "cum_seconds"}
    cum_seconds 为从 A 出发到达该轨迹点的累计耗时（秒）。

    高德 driving 的 steps 里每段有 duration（秒）和 polyline（坐标串）。
    我们把每段的耗时按该段内的坐标点数均摊，从而得到沿路线连续的「时间-坐标」序列。
    这样时间中点会落在真实行车路径上，而不是某个绕路的换乘大站。
    """
    timeline: list[dict] = []
    cum = 0.0

    for step in path.get("steps", []):
        step_duration = int(step.get("duration", 0) or 0)
        points = list(_iter_polyline_points(step.get("polyline", "")))
        if not points:
            cum += step_duration
            continue

        # 把本段耗时均摊到本段的每个坐标点上
        per_point = step_duration / len(points)
        for pt in points:
            cum += per_point
            timeline.append({"lng": pt[0], "lat": pt[1], "cum_seconds": cum})

    return timeline


def find_time_midpoint(
    lng1: float, lat1: float,
    lng2: float, lat2: float,
    city: str = "北京",
    departure_time: str | None = None,
    mode: str = "driving",
) -> dict:
    """
    计算 A→B 行程的「时间中点」：在 A→B 的实际路线上，找到从 A 出发
    耗时约为总时长一半处所对应的坐标点。

    mode="driving"（默认）：沿驾车路线取时间中点；
    mode="transit"：沿公交/地铁路线取时间中点。
    路线贴合 A、B 走向，时间过半处即为时间中点。

    选点策略（结果始终落在真实路线上，不会出现在河里/楼里）：
      1. 优先取「时间过半」的轨迹点 / 站点
      2. 偏移保护：若时间中点离「几何中点」过远（> 两地直线距离的 60%），
         判定为路线绕路，改取「路线上离几何中点最近」的点——
         它仍在路线上，又尽量贴近 A、B 的几何中心
      3. 仅当路线完全查不到 / 无轨迹点时，才退回地理中点 find_balanced_midpoint
         （这是兜底，几何中点理论上可能落在河/楼里）

    返回结构与 find_balanced_midpoint 一致，额外带 method 字段：
        method = "{mode}_time" | "{mode}_nearest_geo" | "geo_fallback"
    """
    geo = find_balanced_midpoint(lng1, lat1, lng2, lat2)
    geo_mid = geo["midpoint"]
    total_dist_km = geo["total_distance_km"]
    # 偏移阈值：时间中点离几何中点的直线距离不应超过两地直线距离的 60%
    max_offset_km = max(0.5, total_dist_km * 0.6)
    mode_label = "公交/地铁" if mode == "transit" else "驾车"

    # ── 按 mode 获取路线，构建「时间-坐标」timeline ──
    total_seconds = 0
    timeline: list[dict] = []
    try:
        if mode == "transit":
            date_str, time_str = _parse_departure_time(departure_time)
            resp = requests.get(
                "https://restapi.amap.com/v3/direction/transit/integrated",
                params={
                    "key": AMAP_KEY,
                    "origin": f"{lng1},{lat1}",
                    "destination": f"{lng2},{lat2}",
                    "city": city,
                    "strategy": 0,
                    "nightflag": 0,
                    "date": date_str,
                    "time": time_str,
                    "output": "json",
                },
                timeout=10,
            )
            data = resp.json()
            transits = (
                data.get("route", {}).get("transits")
                if data.get("status") == "1" else None
            )
            if transits:
                total_seconds = int(transits[0].get("duration", 0) or 0)
                timeline = _build_transit_timeline(transits[0])
        else:  # driving
            resp = requests.get(
                "https://restapi.amap.com/v3/direction/driving",
                params={
                    "key": AMAP_KEY,
                    "origin": f"{lng1},{lat1}",
                    "destination": f"{lng2},{lat2}",
                    "strategy": 0,
                    "extensions": "all",  # 必须 all 才返回 steps 的 polyline
                    "output": "json",
                },
                timeout=10,
            )
            data = resp.json()
            paths = (
                data.get("route", {}).get("paths")
                if data.get("status") == "1" else None
            )
            if paths:
                total_seconds = int(paths[0].get("duration", 0) or 0)
                timeline = _build_driving_timeline(paths[0])
    except Exception as e:
        geo["method"] = "geo_fallback"
        geo["fallback_reason"] = f"{mode_label}路线请求失败：{e}"
        return geo

    if total_seconds <= 0 or not timeline:
        geo["method"] = "geo_fallback"
        geo["fallback_reason"] = f"A、B 之间无可用{mode_label}路线，已退回地理中点"
        return geo

    # ── 候选 1：时间过半的轨迹点 ──
    half_seconds = total_seconds / 2.0
    best = min(timeline, key=lambda node: abs(node["cum_seconds"] - half_seconds))
    offset_km = haversine_distance(
        best["lng"], best["lat"], geo_mid["lng"], geo_mid["lat"]
    )

    total_min = round(total_seconds / 60)
    radius_m = geo["suggested_search_radius_m"]

    # 时间中点贴近几何中心 → 直接采用
    if offset_km <= max_offset_km:
        at_min = round(best["cum_seconds"] / 60)
        return {
            "midpoint": {"lng": best["lng"], "lat": best["lat"]},
            "total_distance_km": total_dist_km,
            "suggested_search_radius_m": radius_m,
            "method": f"{mode}_time",
            "transport_total_minutes": total_min,
            "midpoint_at_minutes": at_min,
            "note": (
                f"{mode_label}全程约 {total_min} 分钟，"
                f"时间中点取在从 A 出发约 {at_min} 分钟处（落在实际路线上）。"
                f"建议以此为圆心搜索半径 {radius_m}m 内的地点。"
            ),
        }

    # ── 候选 2（绕路保护）：路线上离几何中点最近的点 ──
    # 仍在真实路线上（不会落在河/楼里），又尽量贴近 A、B 几何中心
    nearest = min(
        timeline,
        key=lambda node: haversine_distance(
            node["lng"], node["lat"], geo_mid["lng"], geo_mid["lat"]
        ),
    )
    at_min = round(nearest["cum_seconds"] / 60)
    return {
        "midpoint": {"lng": nearest["lng"], "lat": nearest["lat"]},
        "total_distance_km": total_dist_km,
        "suggested_search_radius_m": radius_m,
        "method": f"{mode}_nearest_geo",
        "transport_total_minutes": total_min,
        "midpoint_at_minutes": at_min,
        "note": (
            f"{mode_label}时间中点偏离几何中点 {round(offset_km, 1)}km（路线绕路），"
            f"已改取路线上离几何中点最近的位置（从 A 出发约 {at_min} 分钟处），"
            f"该点仍在实际路线上。建议以此为圆心搜索半径 {radius_m}m 内的地点。"
        ),
    }


def decide_midpoint_mode(prefer_a: str, prefer_b: str) -> str:
    """
    根据 A、B 双方的出行方式选择，决定中点的计算方式。

    规则：
      - 双方都选「公交地铁(transit)」      → "transit"（公交时间中点）
      - 双方都选「最快(auto)」或都「驾车」  → "driving"（驾车时间中点）
      - 双方都选「骑行/步行」              → "geo"（几何中点）
      - 双方选择不一致                    → "geo"（几何中点，无统一行程基准）

    返回："transit" | "driving" | "geo"
    """
    a = (prefer_a or "auto").lower()
    b = (prefer_b or "auto").lower()

    if a != b:
        return "geo"
    if a == "transit":
        return "transit"
    if a in ("auto", "driving"):
        return "driving"
    # cycling / walking 及其他
    return "geo"
