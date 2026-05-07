"""
智能中间点地点推荐系统 - 后端服务
使用高德地图API + DeepSeek LLM 实现智能地点推荐
"""

import os
import json
import math
import time as time_module
import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='static')
CORS(app)

# API Keys
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
AMAP_KEY = os.getenv('AMAP_KEY', '')        # 高德 Web服务 Key（后端用）
AMAP_JS_KEY = os.getenv('AMAP_JS_KEY', AMAP_KEY)  # 高德 JS API Key（前端地图用，没配则回退到AMAP_KEY）

# DeepSeek 客户端（兼容OpenAI格式）
llm_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# ─────────────────────────────────────────────
# 高德地图 API 工具函数
# ─────────────────────────────────────────────

def amap_geocode(address: str) -> dict:
    """地理编码：地址转坐标"""
    url = "https://restapi.amap.com/v3/geocode/geo"
    params = {
        "key": AMAP_KEY,
        "address": address,
        "output": "json"
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("status") == "1" and data.get("geocodes"):
        geocode = data["geocodes"][0]
        location = geocode["location"].split(",")
        return {
            "success": True,
            "lng": float(location[0]),
            "lat": float(location[1]),
            "formatted_address": geocode.get("formatted_address", address),
            "city": geocode.get("city", "")
        }
    return {"success": False, "error": data.get("info", "地理编码失败")}


def amap_regeocode(lng: float, lat: float) -> dict:
    """逆地理编码：坐标转地址"""
    url = "https://restapi.amap.com/v3/geocode/regeo"
    params = {
        "key": AMAP_KEY,
        "location": f"{lng},{lat}",
        "output": "json",
        "radius": 1000
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("status") == "1":
        info = data.get("regeocode", {})
        return {
            "success": True,
            "formatted_address": info.get("formatted_address", f"{lng},{lat}"),
            "city": info.get("addressComponent", {}).get("city", "")
        }
    return {"success": False, "error": "逆地理编码失败"}


def calculate_geographic_midpoint(lng1: float, lat1: float, lng2: float, lat2: float) -> dict:
    """计算两点地理中点"""
    mid_lng = (lng1 + lng2) / 2
    mid_lat = (lat1 + lat2) / 2
    return {"lng": mid_lng, "lat": mid_lat}


def amap_driving_route(origin_lng: float, origin_lat: float,
                       dest_lng: float, dest_lat: float, strategy: int = 0) -> dict:
    """驾车路线规划"""
    url = "https://restapi.amap.com/v3/direction/driving"
    params = {
        "key": AMAP_KEY,
        "origin": f"{origin_lng},{origin_lat}",
        "destination": f"{dest_lng},{dest_lat}",
        "strategy": strategy,  # 0=最快, 1=避堵, 10=最短
        "output": "json"
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("status") == "1" and data.get("route", {}).get("paths"):
        path = data["route"]["paths"][0]
        duration_seconds = int(path.get("duration", 0))
        distance_meters = int(path.get("distance", 0))
        return {
            "success": True,
            "mode": "driving",
            "duration_minutes": round(duration_seconds / 60),
            "distance_km": round(distance_meters / 1000, 1),
            "duration_text": f"{round(duration_seconds / 60)}分钟",
            "distance_text": f"{round(distance_meters / 1000, 1)}公里"
        }
    return {"success": False, "error": "驾车路线规划失败", "mode": "driving"}


def _parse_departure_time(departure_time: str | None) -> tuple[str, str]:
    """
    解析出发时间字符串，返回 (date_str, time_str)。
    departure_time 格式: "HH:MM" 或 "YYYY-MM-DD HH:MM"，None 时默认明天中午12:00。
    """
    import datetime
    now = datetime.datetime.now()

    if not departure_time:
        # 默认：下一个工作日12:00
        candidate = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if now.hour >= 12:
            candidate += datetime.timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += datetime.timedelta(days=1)
        return candidate.strftime("%Y-%m-%d"), "12:00"

    departure_time = departure_time.strip()
    if len(departure_time) == 5 and ":" in departure_time:
        # 只传了 HH:MM，日期取今天（若已过则取明天）
        h, m = map(int, departure_time.split(":"))
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += datetime.timedelta(days=1)
        return candidate.strftime("%Y-%m-%d"), departure_time
    else:
        # 完整 datetime 字符串
        try:
            dt = datetime.datetime.strptime(departure_time, "%Y-%m-%d %H:%M")
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except ValueError:
            return now.strftime("%Y-%m-%d"), "12:00"


def amap_transit_route(origin_lng: float, origin_lat: float,
                       dest_lng: float, dest_lat: float,
                       city: str = "北京",
                       departure_time: str | None = None) -> dict:
    """公交/地铁路线规划，支持自定义出发时间（格式 HH:MM），默认工作日中午12:00"""
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
        "output": "json"
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("status") == "1" and data.get("route", {}).get("transits"):
        transit = data["route"]["transits"][0]
        # duration 取自 transit 对象：包含步行+等车+乘车的完整时间，单位秒
        duration_seconds = int(transit.get("duration", 0))

        # 距离：累加各 segment 的步行距离 + 公交线路距离（route.distance 是直线距离，不准确）
        segments = transit.get("segments", [])
        total_distance_m = 0
        for seg in segments:
            walking = seg.get("walking", {})
            total_distance_m += int(walking.get("distance", 0)) if walking else 0
            bus = seg.get("bus", {})
            for line in bus.get("buslines", []):
                total_distance_m += int(line.get("distance", 0))

        lines = []
        for seg in segments:
            walking = seg.get("walking", {})
            walk_sec = int(walking.get("duration", 0)) if walking else 0
            bus = seg.get("bus", {})
            buslines = bus.get("buslines", [])

            # 步行先于本段乘车显示，超过1分钟即显示（起点/换乘/到达统一标准）
            if walk_sec >= 60:
                lines.append(f"步行{round(walk_sec/60)}分钟")

            for line in buslines:
                name = line.get("name", "")
                if name:
                    lines.append(name)

        summary = " → ".join(lines) or "公共交通"
        distance_meters = total_distance_m or int(data["route"].get("distance", 0))
        return {
            "success": True,
            "mode": "transit",
            "duration_minutes": round(duration_seconds / 60),
            "distance_km": round(distance_meters / 1000, 1),
            "duration_text": f"{round(duration_seconds / 60)}分钟",
            "distance_text": f"{round(distance_meters / 1000, 1)}公里",
            "line_summary": summary
        }
    return {"success": False, "error": "公交路线规划失败", "mode": "transit"}


def amap_walking_route(origin_lng: float, origin_lat: float,
                       dest_lng: float, dest_lat: float) -> dict:
    """步行路线规划"""
    url = "https://restapi.amap.com/v3/direction/walking"
    params = {
        "key": AMAP_KEY,
        "origin": f"{origin_lng},{origin_lat}",
        "destination": f"{dest_lng},{dest_lat}",
        "output": "json"
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("status") == "1" and data.get("route", {}).get("paths"):
        path = data["route"]["paths"][0]
        duration_seconds = int(path.get("duration", 0))
        distance_meters = int(path.get("distance", 0))
        return {
            "success": True,
            "mode": "walking",
            "duration_minutes": round(duration_seconds / 60),
            "distance_km": round(distance_meters / 1000, 1),
            "duration_text": f"{round(duration_seconds / 60)}分钟",
            "distance_text": f"{round(distance_meters / 1000, 1)}公里"
        }
    return {"success": False, "error": "步行路线规划失败", "mode": "walking"}


def amap_cycling_route(origin_lng: float, origin_lat: float,
                       dest_lng: float, dest_lat: float) -> dict:
    """骑行路线规划（共享单车/自行车）"""
    url = "https://restapi.amap.com/v4/direction/bicycling"
    params = {
        "key": AMAP_KEY,
        "origin": f"{origin_lng},{origin_lat}",
        "destination": f"{dest_lng},{dest_lat}",
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    # v4 API 响应格式不同
    if data.get("errcode") == 0:
        paths = data.get("data", {}).get("paths", [])
        if paths:
            path = paths[0]
            duration_seconds = int(path.get("duration", 0))
            distance_meters = int(path.get("distance", 0))
            return {
                "success": True,
                "mode": "cycling",
                "duration_minutes": round(duration_seconds / 60),
                "distance_km": round(distance_meters / 1000, 1),
                "duration_text": f"{round(duration_seconds / 60)}分钟",
                "distance_text": f"{round(distance_meters / 1000, 1)}公里"
            }
    return {"success": False, "error": "骑行路线规划失败", "mode": "cycling"}


def amap_get_best_route(origin_lng: float, origin_lat: float,
                        dest_lng: float, dest_lat: float,
                        city: str = "北京",
                        prefer: str = "auto",
                        departure_time: str | None = None) -> dict:
    """
    自动对比多种交通方式，返回最快的方案。
    prefer: auto | transit | driving | walking | cycling
    departure_time: "HH:MM" 或 None（默认12:00），影响公交地铁方案
    """
    dist_km = haversine_distance(origin_lng, origin_lat, dest_lng, dest_lat)

    if prefer == "driving":
        return amap_driving_route(origin_lng, origin_lat, dest_lng, dest_lat)

    results = {}

    # 步行：仅当直线距离 < 2.5km 才尝试
    if dist_km < 2.5:
        r = amap_walking_route(origin_lng, origin_lat, dest_lng, dest_lat)
        if r.get("success"):
            results["walking"] = r

    # 骑行：直线距离 < 8km 才尝试
    if prefer in ("auto", "cycling") and dist_km < 8:
        r = amap_cycling_route(origin_lng, origin_lat, dest_lng, dest_lat)
        if r.get("success"):
            results["cycling"] = r

    # 公交地铁：始终尝试（传入出发时间）
    if prefer in ("auto", "transit"):
        r = amap_transit_route(origin_lng, origin_lat, dest_lng, dest_lat, city, departure_time)
        if r.get("success"):
            results["transit"] = r

    # 驾车（auto模式也查询，作为备选）
    if prefer == "auto":
        r = amap_driving_route(origin_lng, origin_lat, dest_lng, dest_lat)
        if r.get("success"):
            results["driving"] = r

    if not results:
        return {"success": False, "error": "所有交通方式均查询失败", "mode": "unknown"}

    # 选最快的
    best = min(results.values(), key=lambda x: x.get("duration_minutes", 9999))
    best["all_modes"] = {
        mode: {
            "duration_minutes": r.get("duration_minutes"),
            "duration_text": r.get("duration_text"),
            "distance_text": r.get("distance_text"),
            "line_summary": r.get("line_summary", "")
        }
        for mode, r in results.items()
    }
    return best


def amap_search_nearby(center_lng: float, center_lat: float,
                        keyword: str, radius: int = 3000,
                        sort_by: str = "distance") -> dict:
    """
    周边POI搜索
    sort_by: distance(距离) | weight(权重/综合)
    """
    url = "https://restapi.amap.com/v3/place/around"
    params = {
        "key": AMAP_KEY,
        "location": f"{center_lng},{center_lat}",
        "keywords": keyword,
        "radius": radius,
        "sortrule": sort_by,
        "output": "json",
        "offset": 25,  # 每页结果数
        "page": 1,
        "extensions": "all"  # 返回详细信息
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("status") == "1":
        pois = data.get("pois", [])
        results = []
        for poi in pois:
            location = poi.get("location", "").split(",")
            if len(location) == 2:
                try:
                    rating = float(poi.get("biz_ext", {}).get("rating", 0) or 0)
                    cost = poi.get("biz_ext", {}).get("cost", "")
                    results.append({
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
                        "photos": [p.get("url", "") for p in poi.get("photos", [])[:2]]
                    })
                except (ValueError, TypeError):
                    pass
        return {
            "success": True,
            "count": len(results),
            "pois": results
        }
    return {"success": False, "error": data.get("info", "搜索失败"), "pois": []}


def amap_text_search(keyword: str, city: str = "") -> dict:
    """关键词POI搜索（带城市范围）"""
    url = "https://restapi.amap.com/v3/place/text"
    params = {
        "key": AMAP_KEY,
        "keywords": keyword,
        "city": city,
        "output": "json",
        "offset": 20,
        "page": 1,
        "extensions": "all"
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("status") == "1":
        pois = data.get("pois", [])
        results = []
        for poi in pois:
            location = poi.get("location", "").split(",")
            if len(location) == 2:
                try:
                    results.append({
                        "id": poi.get("id", ""),
                        "name": poi.get("name", ""),
                        "address": poi.get("address", ""),
                        "lng": float(location[0]),
                        "lat": float(location[1]),
                        "rating": float(poi.get("biz_ext", {}).get("rating", 0) or 0),
                        "type": poi.get("type", ""),
                    })
                except (ValueError, TypeError):
                    pass
        return {"success": True, "count": len(results), "pois": results}
    return {"success": False, "error": "搜索失败", "pois": []}


def haversine_distance(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """计算两点间直线距离（千米）"""
    R = 6371
    dlng = math.radians(lng2 - lng1)
    dlat = math.radians(lat2 - lat1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def find_balanced_midpoint(lng1: float, lat1: float, lng2: float, lat2: float) -> dict:
    """
    计算两点地理中点（经纬度平均值），并根据两地直线距离给出建议搜索半径。

    说明：
    - 中点 = (lng1+lng2)/2, (lat1+lat2)/2，即地图上的正中间位置
    - 搜索半径建议 = 两地直线距离 × 35%（既能覆盖中间区域，又不会范围过大）
    - 例如两地相距 10km，建议搜索半径 3.5km
    - 后续用 search_pois_nearby 在中点周围按此半径搜索
    """
    geo_mid = calculate_geographic_midpoint(lng1, lat1, lng2, lat2)
    dist_km = haversine_distance(lng1, lat1, lng2, lat2)

    # 建议搜索半径：两地距离的35%，最小500m，最大8km
    suggested_radius_m = int(max(500, min(8000, dist_km * 1000 * 0.35)))

    return {
        "midpoint": geo_mid,
        "total_distance_km": round(dist_km, 1),
        "suggested_search_radius_m": suggested_radius_m,
        "note": (
            f"两地直线距离 {round(dist_km, 1)}km，"
            f"建议以中点为圆心搜索半径 {suggested_radius_m}m 内的地点。"
            f"如果结果不理想可适当扩大半径。"
        )
    }


# ─────────────────────────────────────────────
# LLM 工具定义（OpenAI Function Calling 格式）
# ─────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "geocode",
            "description": "将地址文字转换为经纬度坐标。当需要知道某个地点的坐标时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "需要查询的地址，例如：北京市朝阳区三里屯"}
                },
                "required": ["address"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_midpoint",
            "description": (
                "计算两个地理坐标之间的中点，并返回建议搜索半径。"
                "中点 = 两坐标经纬度的算术平均值。"
                "建议搜索半径 = 两地直线距离的35%（自动计算，可在后续搜索时使用）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lng1": {"type": "number", "description": "地点A的经度"},
                    "lat1": {"type": "number", "description": "地点A的纬度"},
                    "lng2": {"type": "number", "description": "地点B的经度"},
                    "lat2": {"type": "number", "description": "地点B的纬度"}
                },
                "required": ["lng1", "lat1", "lng2", "lat2"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_pois_nearby",
            "description": (
                "在指定中心点周边搜索任意类型的地点（POI）。"
                "支持餐厅（鲁菜/火锅/烤鸭/日料/川菜）、咖啡馆、酒吧、电影院、KTV、"
                "购物中心、公园、博物馆等任何关键词。"
                "radius 建议使用 find_midpoint 返回的 suggested_search_radius_m。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "center_lng": {"type": "number", "description": "搜索中心点经度"},
                    "center_lat": {"type": "number", "description": "搜索中心点纬度"},
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词，例如：鲁菜、火锅、咖啡馆、酒吧、电影院、KTV、购物中心"
                    },
                    "radius": {
                        "type": "integer",
                        "description": "搜索半径（米），使用 find_midpoint 的 suggested_search_radius_m，最大50000"
                    }
                },
                "required": ["center_lng", "center_lat", "keyword", "radius"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_best_route",
            "description": (
                "【推荐使用此工具替代单独调用各交通路线工具】"
                "自动查询多种交通方式（公交地铁、骑行、步行、驾车），返回最快方案及所有方式用时对比。"
                "注意：路线时间基于工作日白天（12:00）计算，不受查询时间影响。"
                "如果用户指定了出行方式（如'驾车'/'开车'），请将 prefer 设为 'driving'。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin_lng": {"type": "number"},
                    "origin_lat": {"type": "number"},
                    "dest_lng": {"type": "number"},
                    "dest_lat": {"type": "number"},
                    "city": {"type": "string", "description": "所在城市，例如：北京、上海", "default": "北京"},
                    "prefer": {
                        "type": "string",
                        "description": "交通偏好：auto（自动选最快）/ transit（公交地铁）/ driving（驾车）/ cycling（骑行）/ walking（步行）",
                        "enum": ["auto", "transit", "driving", "cycling", "walking"],
                        "default": "auto"
                    }
                },
                "required": ["origin_lng", "origin_lat", "dest_lng", "dest_lat"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_transit_route",
            "description": "单独查询公交/地铁路线（一般情况请使用 get_best_route）。路线时间基于工作日白天计算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin_lng": {"type": "number"},
                    "origin_lat": {"type": "number"},
                    "dest_lng": {"type": "number"},
                    "dest_lat": {"type": "number"},
                    "city": {"type": "string", "default": "北京"}
                },
                "required": ["origin_lng", "origin_lat", "dest_lng", "dest_lat"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_driving_route",
            "description": "单独查询驾车路线（一般情况请使用 get_best_route）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin_lng": {"type": "number"},
                    "origin_lat": {"type": "number"},
                    "dest_lng": {"type": "number"},
                    "dest_lat": {"type": "number"}
                },
                "required": ["origin_lng", "origin_lat", "dest_lng", "dest_lat"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_cycling_route",
            "description": "单独查询骑行（共享单车）路线（一般情况请使用 get_best_route）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin_lng": {"type": "number"},
                    "origin_lat": {"type": "number"},
                    "dest_lng": {"type": "number"},
                    "dest_lat": {"type": "number"}
                },
                "required": ["origin_lng", "origin_lat", "dest_lng", "dest_lat"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_walking_route",
            "description": "单独查询步行路线（一般情况请使用 get_best_route）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin_lng": {"type": "number"},
                    "origin_lat": {"type": "number"},
                    "dest_lng": {"type": "number"},
                    "dest_lat": {"type": "number"}
                },
                "required": ["origin_lng", "origin_lat", "dest_lng", "dest_lat"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "filter_and_rank_pois",
            "description": "对搜索到的地点列表按评分从高到低排序，可指定最低评分门槛和返回数量。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pois": {
                        "type": "array",
                        "description": "地点列表（来自 search_pois_nearby 的 pois 字段）",
                        "items": {"type": "object"}
                    },
                    "min_rating": {"type": "number", "description": "最低评分门槛（0-5分），默认4.0", "default": 4.0},
                    "top_n": {"type": "integer", "description": "返回前N个地点", "default": 5}
                },
                "required": ["pois"]
            }
        }
    }
]

# ─────────────────────────────────────────────
# 工具执行函数
# ─────────────────────────────────────────────

def execute_tool(tool_name: str, tool_args: dict, context: dict) -> dict:
    """执行LLM调用的工具"""
    print(f"[TOOL] 执行工具: {tool_name}, 参数: {json.dumps(tool_args, ensure_ascii=False)}")
    city = tool_args.get("city", context.get("city", "北京"))
    departure_time = context.get("departure_time")  # 从上下文获取出发时间

    if tool_name == "geocode":
        return amap_geocode(tool_args["address"])

    elif tool_name == "find_midpoint":
        return find_balanced_midpoint(
            tool_args["lng1"], tool_args["lat1"],
            tool_args["lng2"], tool_args["lat2"]
        )

    elif tool_name in ("search_pois_nearby", "search_restaurants_nearby"):
        # 兼容旧名称
        radius = tool_args.get("radius", 3000)
        result = amap_search_nearby(
            tool_args["center_lng"], tool_args["center_lat"],
            tool_args["keyword"], radius
        )
        if result.get("success"):
            context.setdefault("all_pois", []).extend(result.get("pois", []))
        return result

    elif tool_name == "get_best_route":
        prefer = tool_args.get("prefer", "auto")
        if context.get("transport_prefer") and prefer == "auto":
            prefer = context["transport_prefer"]
        return amap_get_best_route(
            tool_args["origin_lng"], tool_args["origin_lat"],
            tool_args["dest_lng"], tool_args["dest_lat"],
            city, prefer, departure_time
        )

    elif tool_name == "get_transit_route":
        return amap_transit_route(
            tool_args["origin_lng"], tool_args["origin_lat"],
            tool_args["dest_lng"], tool_args["dest_lat"],
            city, departure_time
        )

    elif tool_name == "get_driving_route":
        return amap_driving_route(
            tool_args["origin_lng"], tool_args["origin_lat"],
            tool_args["dest_lng"], tool_args["dest_lat"]
        )

    elif tool_name == "get_cycling_route":
        return amap_cycling_route(
            tool_args["origin_lng"], tool_args["origin_lat"],
            tool_args["dest_lng"], tool_args["dest_lat"]
        )

    elif tool_name == "get_walking_route":
        return amap_walking_route(
            tool_args["origin_lng"], tool_args["origin_lat"],
            tool_args["dest_lng"], tool_args["dest_lat"]
        )

    elif tool_name in ("filter_and_rank_pois", "filter_and_rank_restaurants"):
        # 兼容旧名称
        pois = tool_args.get("pois", tool_args.get("restaurants", []))
        min_rating = tool_args.get("min_rating", 4.0)
        top_n = tool_args.get("top_n", 5)

        filtered = [r for r in pois if r.get("rating", 0) >= min_rating]
        filtered.sort(key=lambda x: x.get("rating", 0), reverse=True)

        # 如果评分筛选后结果太少，降低门槛重试
        if len(filtered) < 3 and min_rating > 0:
            filtered = sorted(pois, key=lambda x: x.get("rating", 0), reverse=True)

        top = filtered[:top_n]
        return {
            "success": True,
            "total_found": len(pois),
            "qualified_count": len(filtered),
            "top_pois": top
        }

    return {"success": False, "error": f"未知工具: {tool_name}"}


# ─────────────────────────────────────────────
# LLM Agent 主流程
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """你是一个智能中间点地点推荐助手，帮助用户找到两地之间双方都方便到达的地点（餐厅、咖啡馆、电影院、酒吧、KTV等均可）。

## 工作流程

1. 坐标已由系统提供，直接跳过geocode步骤
2. 调用 find_midpoint 计算中间区域坐标，注意返回值中的 suggested_search_radius_m（建议搜索半径）
3. 用 search_pois_nearby 在中点周边搜索，keyword 根据用户需求确定，radius 使用建议值
   - 如果搜索结果 < 5 个，自动扩大 radius 重搜一次
   - 如果关键词太窄，尝试更宽泛的词（例如"鲁菜"找不到可改为"山东菜" 或 "中餐"）
4. 调用 filter_and_rank_pois 按评分排名，取 top 5
5. 对每个入选地点，分别用 get_best_route 查询从 A 和 B 的最优路线
   - get_best_route 会自动对比公交/骑行/步行/驾车，返回最快方案
   - 如果用户明确指定了出行方式（如"驾车"/"开车"/"地铁"），传入对应的 prefer 参数
   - 所有路线时间均基于工作日白天（12:00）计算，不受查询时间影响
6. 整理结果，以 JSON 格式输出

## 返回格式（最终回答必须包含此 JSON 代码块）

```json
{
  "summary": "简要说明（例如：在XX区域找到5家高评分鲁菜餐厅）",
  "midpoint": {"lng": 116.4, "lat": 39.9},
  "search_radius_m": 3000,
  "pois": [
    {
      "name": "地点名称",
      "address": "详细地址",
      "lng": 116.4,
      "lat": 39.9,
      "rating": 4.8,
      "cost_per_person": "人均消费（如有）",
      "tel": "电话（如有）",
      "type": "地点类型",
      "transport_from_a": {
        "mode": "transit/driving/cycling/walking",
        "duration_text": "25分钟",
        "distance_text": "8.2公里",
        "line_summary": "地铁1号线→地铁2号线→步行5分钟"
      },
      "transport_from_b": {
        "mode": "transit/driving/cycling/walking",
        "duration_text": "22分钟",
        "distance_text": "6.5公里",
        "line_summary": "骑行约22分钟"
      }
    }
  ]
}
```

## 注意事项
- 评分为0说明暂无评分，仍可推荐，在summary中注明
- 优先推荐评分 4.0 以上，若结果不足则降低门槛
- 交通用时体现"公平性"，A和B到达时间尽量接近
- 搜索关键词要与用户需求匹配，支持任意地点类型
- 路线时间是白天出行的估算值，实际交通状况可能不同
"""


def run_llm_agent(user_query: str, location_a: dict, location_b: dict,
                   city: str = "北京", transport_prefer: str = "auto",
                   departure_time: str | None = None) -> dict:
    """
    运行LLM Agent进行多轮工具调用
    location_a/b: {"lng": float, "lat": float, "name": str}
    transport_prefer: auto | transit | driving | cycling | walking
    departure_time: "HH:MM" 格式，None 时默认12:00
    """
    prefer_label = {
        "auto": "自动选择最快方式",
        "transit": "优先公交/地铁",
        "driving": "驾车",
        "cycling": "骑行",
        "walking": "步行"
    }.get(transport_prefer, "自动选择最快方式")

    # 解析时间显示文本
    date_str, time_str = _parse_departure_time(departure_time)
    time_label = f"{date_str} {time_str}" if departure_time else f"默认（工作日 {time_str}）"

    context = {
        "all_pois": [],
        "city": city,
        "transport_prefer": transport_prefer,
        "departure_time": departure_time  # 存入上下文，execute_tool 会取用
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"用户需求：{user_query}\n\n"
                f"地点A：{location_a.get('name', '地点A')}，"
                f"坐标：({location_a['lng']}, {location_a['lat']})\n"
                f"地点B：{location_b.get('name', '地点B')}，"
                f"坐标：({location_b['lng']}, {location_b['lat']})\n"
                f"城市：{city}\n"
                f"出行方式偏好：{prefer_label}（调用 get_best_route 时 prefer 参数使用 '{transport_prefer}'）\n"
                f"出发时间：{time_label}（此时间已自动应用于所有路线查询，无需在工具参数中指定）\n\n"
                f"请帮我找到两个地点中间符合需求的地点，并规划好交通路线。"
            )
        }
    ]

    max_rounds = 25  # 允许更多工具调用轮次
    round_count = 0
    tool_call_log = []

    while round_count < max_rounds:
        round_count += 1
        print(f"\n[LLM] 第{round_count}轮对话...")

        response = llm_client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.3,
            max_tokens=4096
        )

        message = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        if finish_reason == "stop" or not message.tool_calls:
            print(f"[LLM] Agent完成，共调用{len(tool_call_log)}次工具")
            return {
                "success": True,
                "final_answer": message.content,
                "tool_call_log": tool_call_log
            }

        messages.append(message)

        tool_results = []
        for tc in message.tool_calls:
            tool_name = tc.function.name
            tool_args = json.loads(tc.function.arguments)

            result = execute_tool(tool_name, tool_args, context)
            tool_call_log.append({
                "tool": tool_name,
                "args": tool_args,
                "result_summary": str(result)[:300]
            })

            tool_results.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False)
            })

        messages.extend(tool_results)

    return {
        "success": False,
        "error": "Agent达到最大轮数限制",
        "tool_call_log": tool_call_log
    }


def parse_final_json(final_answer: str) -> dict | None:
    """从LLM最终回答中提取JSON结果"""
    import re
    # 找 ```json ... ``` 代码块
    pattern = r'```json\s*([\s\S]*?)\s*```'
    matches = re.findall(pattern, final_answer)
    if matches:
        try:
            return json.loads(matches[-1])
        except json.JSONDecodeError:
            pass
    # 尝试直接解析整个回答
    try:
        return json.loads(final_answer)
    except json.JSONDecodeError:
        return None


# ─────────────────────────────────────────────
# HTTP API 路由
# ─────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/config')
def get_config():
    """返回前端需要的配置（高德JS Key）"""
    return jsonify({
        "amap_key": AMAP_JS_KEY,       # 前端地图用 JS API Key
        "has_amap_key": bool(AMAP_JS_KEY)
    })


@app.route('/api/geocode', methods=['POST'])
def api_geocode():
    """地理编码接口"""
    data = request.json
    address = data.get('address', '')
    if not address:
        return jsonify({"success": False, "error": "请提供地址"}), 400
    return jsonify(amap_geocode(address))


@app.route('/api/search', methods=['POST'])
def api_search():
    """
    主搜索接口：LLM Agent驱动的智能地点推荐
    """
    data = request.json
    location_a = data.get('location_a')
    location_b = data.get('location_b')
    user_query = data.get('query', '')
    city = data.get('city', '北京')
    transport_prefer = data.get('transport_prefer', 'auto')
    departure_time = data.get('departure_time') or None  # "HH:MM" 或 None

    if not location_a or not location_b:
        return jsonify({"success": False, "error": "请提供两个地点的坐标"}), 400
    if not user_query:
        return jsonify({"success": False, "error": "请描述您的需求"}), 400
    if not AMAP_KEY:
        return jsonify({"success": False, "error": "高德地图API Key未配置，请在.env中添加AMAP_KEY"}), 500
    if not DEEPSEEK_API_KEY:
        return jsonify({"success": False, "error": "DeepSeek API Key未配置"}), 500

    try:
        result = run_llm_agent(user_query, location_a, location_b, city, transport_prefer, departure_time)

        if result.get("success"):
            final_answer = result.get("final_answer", "")
            parsed = parse_final_json(final_answer)
            # 兼容新旧字段名：pois / restaurants
            if parsed and "pois" in parsed and "restaurants" not in parsed:
                parsed["restaurants"] = parsed["pois"]
            return jsonify({
                "success": True,
                "raw_answer": final_answer,
                "data": parsed,
                "tool_calls": len(result.get("tool_call_log", []))
            })
        else:
            return jsonify({
                "success": False,
                "error": result.get("error", "分析失败"),
                "tool_calls": len(result.get("tool_call_log", []))
            }), 500

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/route', methods=['POST'])
def api_route():
    """路线查询接口（给前端地图使用）"""
    data = request.json
    mode = data.get('mode', 'transit')
    origin = data.get('origin')
    dest = data.get('dest')
    city = data.get('city', '北京')

    if not origin or not dest:
        return jsonify({"success": False, "error": "请提供起点和终点"}), 400

    if mode == 'transit':
        return jsonify(amap_transit_route(
            origin['lng'], origin['lat'],
            dest['lng'], dest['lat'],
            city
        ))
    elif mode == 'driving':
        return jsonify(amap_driving_route(
            origin['lng'], origin['lat'],
            dest['lng'], dest['lat']
        ))
    elif mode == 'walking':
        return jsonify(amap_walking_route(
            origin['lng'], origin['lat'],
            dest['lng'], dest['lat']
        ))
    return jsonify({"success": False, "error": "不支持的交通模式"}), 400


if __name__ == '__main__':
    print("=" * 60)
    print("  智能中间点餐厅推荐系统")
    print("=" * 60)
    print(f"  DeepSeek API Key: {'已配置' if DEEPSEEK_API_KEY else '未配置 ⚠️'}")
    print(f"  高德地图 API Key:  {'已配置' if AMAP_KEY else '未配置 ⚠️'}")
    print("=" * 60)
    print("  访问地址: http://localhost:5000")
    print("=" * 60)
    app.run(debug=True, host='127.0.0.1', port=5000)
