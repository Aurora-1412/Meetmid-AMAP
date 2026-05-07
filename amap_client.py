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
        }
    return {"success": False, "error": "驾车路线规划失败", "mode": "driving"}


def amap_transit_route(
    origin_lng: float, origin_lat: float,
    dest_lng: float, dest_lat: float,
    city: str = "北京",
    departure_time: str | None = None,
) -> dict:
    """公交 / 地铁路线规划，支持指定出发时间"""
    date_str, time_str = _parse_departure_time(departure_time)
    url = "https://restapi.amap.com/v3/direction/transit/integrated"
    params = {
        "key": AMAP_KEY,
        "origin": f"{origin_lng},{origin_lat}",
        "destination": f"{dest_lng},{dest_lat}",
        "city": city,
        "strategy": 0,
        "nightflag": 0,
        "date": date_str,
        "time": time_str,
        "output": "json",
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("status") == "1" and data.get("route", {}).get("transits"):
        transit = data["route"]["transits"][0]
        duration_s = int(transit.get("duration", 0))

        segments = transit.get("segments", [])
        total_distance_m = 0
        for seg in segments:
            walking = seg.get("walking") or {}
            total_distance_m += int(walking.get("distance", 0))
            bus = seg.get("bus") or {}
            for line in bus.get("buslines", []):
                total_distance_m += int(line.get("distance", 0))

        lines = []
        for seg in segments:
            walking = seg.get("walking") or {}
            walk_s = int(walking.get("duration", 0))
            bus = seg.get("bus") or {}
            if walk_s >= 60:
                lines.append(f"步行{round(walk_s / 60)}分钟")
            for line in bus.get("buslines", []):
                name = line.get("name", "")
                if name:
                    lines.append(name)

        summary = " → ".join(lines) or "公共交通"
        distance_m = total_distance_m or int(data["route"].get("distance", 0))
        return {
            "success": True,
            "mode": "transit",
            "duration_minutes": round(duration_s / 60),
            "distance_km": round(distance_m / 1000, 1),
            "duration_text": f"{round(duration_s / 60)}分钟",
            "distance_text": f"{round(distance_m / 1000, 1)}公里",
            "line_summary": summary,
        }
    return {"success": False, "error": "公交路线规划失败", "mode": "transit"}


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
) -> dict:
    """
    自动对比多种交通方式，返回最快方案。
    prefer: auto | transit | driving | walking | cycling
    """
    dist_km = haversine_distance(origin_lng, origin_lat, dest_lng, dest_lat)

    if prefer == "driving":
        return amap_driving_route(origin_lng, origin_lat, dest_lng, dest_lat)

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
                               city, departure_time)
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
