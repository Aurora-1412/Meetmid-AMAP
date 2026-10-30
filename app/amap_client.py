"""
高德地图 API 工具函数集
======================
封装了地理编码、路线规划、POI 搜索等底层高德 REST API 调用。
所有函数均为纯函数，不依赖 FastAPI 上下文。
配置从 app/config.py 读取，由 Guard 通过环境变量注入。
"""

import math
import time
import datetime
import logging
import requests

from app.config import AMAP_KEY

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 限速重试辅助
# 高德 Web 服务 API 并发上限约 3 次/秒，路线批量计算极易触发限速。
# 遇到超时/连接错误/限速响应时，自动指数退避重试，最多 3 次。
# ─────────────────────────────────────────────

_RATE_LIMIT_KEYWORDS = ("LIMIT", "QPS", "EXCEEDED", "OVER", "BEYOND")

def _amap_get(url: str, params: dict, timeout: int = 10,
              max_retries: int = 3, base_delay: float = 1.0) -> dict:
    """
    带重试的高德 API GET 请求。
    - 网络超时 / 连接错误：自动重试
    - API 返回限速错误（info 含 QPS/LIMIT/EXCEEDED 等）：自动重试
    - 其他业务错误（地址不存在等）：直接返回，不重试
    退避策略：第 1 次失败等 1s，第 2 次等 2s，第 3 次等 3s。
    """
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            data = resp.json()

            # 判断是否限速错误
            info = str(data.get("info", "")).upper()
            is_rate_limited = any(kw in info for kw in _RATE_LIMIT_KEYWORDS)

            if is_rate_limited and attempt < max_retries:
                delay = base_delay * (attempt + 1)
                logger.warning(f"[amap] 限速（{data.get('info')}），{delay:.0f}s 后重试（第 {attempt + 1} 次）")
                time.sleep(delay)
                continue

            return data

        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            if attempt < max_retries:
                delay = base_delay * (attempt + 1)
                logger.warning(f"[amap] 请求异常（{e}），{delay:.0f}s 后重试（第 {attempt + 1} 次）")
                time.sleep(delay)

    # 全部重试耗尽，返回失败结构
    logger.error(f"[amap] 请求最终失败（已重试 {max_retries} 次）: {last_err}")
    return {"status": "0", "info": f"请求失败: {last_err}"}



# ─────────────────────────────────────────────
# 地理编码
# ─────────────────────────────────────────────

def amap_geocode(address: str) -> dict:
    """地址 → 经纬度坐标"""
    url = "https://restapi.amap.com/v3/geocode/geo"
    params = {"key": AMAP_KEY, "address": address, "output": "json"}
    data = _amap_get(url, params, timeout=10)
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
    data = _amap_get(url, params, timeout=10)
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
    data = _amap_get(url, params, timeout=10)
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
    data = _amap_get(url, params, timeout=10)
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
    # v4 接口响应格式为 errcode，_amap_get 的限速检测逻辑同样适用
    data = _amap_get(url, params, timeout=10)
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
    data = _amap_get(url, params, timeout=10)
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


def _parse_location(loc: str) -> tuple[float, float] | None:
    """解析高德 'lng,lat' 字符串。"""
    if not loc or not isinstance(loc, str):
        return None
    parts = loc.split(",")
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except (TypeError, ValueError):
        return None


def _iter_polyline_points(polyline: str):
    for pair in (polyline or "").split(";"):
        point = _parse_location(pair)
        if point:
            yield point


def _build_transit_timeline(transit: dict) -> list[dict]:
    """把公交方案展开成带累计耗时的轨迹点。"""
    timeline: list[dict] = []
    cumulative = 0.0
    for segment in transit.get("segments", []):
        walking = segment.get("walking") or {}
        cumulative += int(walking.get("duration", 0) or 0)
        for line in (segment.get("bus") or {}).get("buslines", []):
            departure = line.get("departure_stop") or {}
            arrival = line.get("arrival_stop") or {}
            duration = int(line.get("duration", 0) or 0)

            departure_location = _parse_location(
                departure.get("location", "")
            )
            if departure_location:
                timeline.append({
                    "lng": departure_location[0],
                    "lat": departure_location[1],
                    "cum_seconds": cumulative,
                })

            points = list(_iter_polyline_points(line.get("polyline", "")))
            if points and duration > 0:
                per_point = duration / len(points)
                for point in points:
                    cumulative += per_point
                    timeline.append({
                        "lng": point[0],
                        "lat": point[1],
                        "cum_seconds": cumulative,
                    })
            else:
                cumulative += duration

            arrival_location = _parse_location(arrival.get("location", ""))
            if arrival_location:
                timeline.append({
                    "lng": arrival_location[0],
                    "lat": arrival_location[1],
                    "cum_seconds": cumulative,
                })
    return timeline


def _build_driving_timeline(path: dict) -> list[dict]:
    """把驾车方案展开成带累计耗时的轨迹点。"""
    timeline: list[dict] = []
    cumulative = 0.0
    for step in path.get("steps", []):
        duration = int(step.get("duration", 0) or 0)
        points = list(_iter_polyline_points(step.get("polyline", "")))
        if not points:
            cumulative += duration
            continue
        per_point = duration / len(points)
        for point in points:
            cumulative += per_point
            timeline.append({
                "lng": point[0],
                "lat": point[1],
                "cum_seconds": cumulative,
            })
    return timeline


def find_time_midpoint(
    lng1: float, lat1: float,
    lng2: float, lat2: float,
    city: str = "北京",
    departure_time: str | None = None,
    mode: str = "driving",
) -> dict:
    """沿公交或驾车实际路线计算行程时间中点。"""
    geo = find_balanced_midpoint(lng1, lat1, lng2, lat2)
    geo_midpoint = geo["midpoint"]
    total_distance_km = geo["total_distance_km"]
    max_offset_km = max(0.5, total_distance_km * 0.6)
    mode_label = "公交/地铁" if mode == "transit" else "驾车"

    try:
        if mode == "transit":
            date_str, time_str = _parse_departure_time(departure_time)
            data = _amap_get(
                "https://restapi.amap.com/v3/direction/transit/integrated",
                {
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
            routes = (
                data.get("route", {}).get("transits")
                if data.get("status") == "1" else None
            )
            route = routes[0] if routes else None
            timeline = _build_transit_timeline(route) if route else []
        else:
            data = _amap_get(
                "https://restapi.amap.com/v3/direction/driving",
                {
                    "key": AMAP_KEY,
                    "origin": f"{lng1},{lat1}",
                    "destination": f"{lng2},{lat2}",
                    "strategy": 0,
                    "extensions": "all",
                    "output": "json",
                },
                timeout=10,
            )
            routes = (
                data.get("route", {}).get("paths")
                if data.get("status") == "1" else None
            )
            route = routes[0] if routes else None
            timeline = _build_driving_timeline(route) if route else []
    except Exception as error:
        geo["method"] = "geo_fallback"
        geo["fallback_reason"] = f"{mode_label}路线请求失败：{error}"
        return geo

    total_seconds = int((route or {}).get("duration", 0) or 0)
    if total_seconds <= 0 or not timeline:
        geo["method"] = "geo_fallback"
        geo["fallback_reason"] = (
            f"A、B 之间无可用{mode_label}路线，已退回地理中点"
        )
        return geo

    halfway = total_seconds / 2
    midpoint = min(
        timeline,
        key=lambda node: abs(node["cum_seconds"] - halfway),
    )
    offset_km = haversine_distance(
        midpoint["lng"], midpoint["lat"],
        geo_midpoint["lng"], geo_midpoint["lat"],
    )
    method = f"{mode}_time"
    if offset_km > max_offset_km:
        midpoint = min(
            timeline,
            key=lambda node: haversine_distance(
                node["lng"], node["lat"],
                geo_midpoint["lng"], geo_midpoint["lat"],
            ),
        )
        method = f"{mode}_nearest_geo"

    total_minutes = round(total_seconds / 60)
    midpoint_minutes = round(midpoint["cum_seconds"] / 60)
    return {
        "midpoint": {"lng": midpoint["lng"], "lat": midpoint["lat"]},
        "total_distance_km": total_distance_km,
        "suggested_search_radius_m": geo["suggested_search_radius_m"],
        "method": method,
        "transport_total_minutes": total_minutes,
        "midpoint_at_minutes": midpoint_minutes,
        "note": (
            f"{mode_label}全程约 {total_minutes} 分钟，"
            f"中点取在从 A 出发约 {midpoint_minutes} 分钟处。"
        ),
    }
