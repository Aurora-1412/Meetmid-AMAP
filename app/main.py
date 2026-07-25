"""
智能中间点推荐系统 v2 — 多 Agent 架构（FastAPI 版）
====================================================
Agent1（规划）:  LLM 理解需求 → 结构化搜索参数
Agent2（搜索）:  LLM + 受控工具 → 搜索候选地点（上下文精简）
路线计算:        纯 Python 直接调高德 API → A/B 分别计算
Agent3（总结）:  LLM 生成推荐文字

原工程 Flask + OpenAI SDK（DeepSeek）→ FastAPI + httpx 直调 Runway 网关
"""

import json
import logging
import asyncio
import re
import time
import uuid

# 路线计算时每个 POI 调用 2 次 amap_get_best_route（A + B），
# 每次内部还可能调多种交通方式。批量计算极易触发高德 3 次/秒限速。
# 此间隔配合 amap_client._amap_get 的重试，双重保障。
_ROUTE_INTER_POI_DELAY = 0.35  # 秒，每个 POI 计算完后等待
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles as _StaticFiles

from app.amap_client import (
    AMAP_KEY,
    amap_geocode,
    amap_get_best_route,
    amap_search_nearby,
    find_balanced_midpoint,
    find_time_midpoint,
    haversine_distance,
)
from app.ai_client import chat_complete, chat_complete_with_tools
from app.config import AMAP_JS_KEY, is_ai_enabled
from candidate_count import parse_candidate_count

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────
# StaticFiles 子类：把 Starlette 默认的绝对 URL 307 改成相对 Location
# 防止 Guard 反代时 Location 暴露 pod 内部地址
# ──────────────────────────────────────────────────────

class StaticFiles(_StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 307 and "location" in response.headers:
            last_seg = scope.get("path", "").rstrip("/").rsplit("/", 1)[-1]
            response.headers["location"] = f"{last_seg}/" if last_seg else "./"
        return response


# ──────────────────────────────────────────────────────
# Session 管理（内存缓存，TTL 1 小时）
# ──────────────────────────────────────────────────────

_sessions: dict[str, dict] = {}
SESSION_TTL = 3600


def session_create(data: dict) -> str:
    sid = str(uuid.uuid4())[:8]
    _sessions[sid] = {"data": data, "expires_at": time.time() + SESSION_TTL}
    _session_cleanup()
    return sid


def session_get(sid: str) -> dict | None:
    s = _sessions.get(sid)
    if not s or s["expires_at"] < time.time():
        return None
    return s["data"]


def session_update(sid: str, data: dict) -> bool:
    if sid not in _sessions:
        return False
    _sessions[sid]["data"].update(data)
    _sessions[sid]["expires_at"] = time.time() + SESSION_TTL
    return True


def _session_cleanup():
    now = time.time()
    expired = [k for k, v in _sessions.items() if v["expires_at"] < now]
    for k in expired:
        del _sessions[k]


# ──────────────────────────────────────────────────────
# 通用工具函数
# ──────────────────────────────────────────────────────

def _extract_json(text: str) -> dict | list | None:
    """从 LLM 回答中提取第一个合法 JSON 对象或数组"""
    m = re.search(r"```json\s*([\s\S]*?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def _compact_poi(poi: dict) -> dict:
    """压缩 POI 字段，减少传给 LLM 的 token 数量"""
    return {
        "name":    poi.get("name", ""),
        "address": poi.get("address", ""),
        "lng":     poi.get("lng", 0),
        "lat":     poi.get("lat", 0),
        "rating":  poi.get("rating", 0),
    }


def _format_route(route: dict) -> dict:
    success = route.get("success", True)
    return {
        "mode":             route.get("mode", "unknown"),
        "success":          success,
        "error":            route.get("error") if not success else None,
        "duration_text":    route.get("duration_text") if success else None,
        "distance_text":    route.get("distance_text") if success else None,
        "line_summary":     route.get("line_summary", ""),
        "duration_minutes": route.get("duration_minutes", 999),
        "all_modes":        route.get("all_modes", {}),
    }


def validate_midpoint_choice(
    midpoint_mode: str, prefer_a: str, prefer_b: str
) -> str | None:
    """校验中点方式与双方出行方式的组合。"""
    if midpoint_mode not in ("geo", "transport"):
        return "中点方式只支持位置中点或交通中点"
    if midpoint_mode == "transport":
        valid = (
            prefer_a == prefer_b
            and prefer_a in ("transit", "driving")
        )
        if not valid:
            return "选择交通中点时，双方必须同时选择公交地铁或同时选择驾车"
    return None


def compute_requested_midpoint(
    lng1: float, lat1: float,
    lng2: float, lat2: float,
    *,
    midpoint_mode: str,
    prefer_a: str,
    prefer_b: str,
    city: str,
    departure_time: str | None,
) -> dict:
    """按用户明确选择的位置/交通中点策略计算搜索中心。"""
    error = validate_midpoint_choice(midpoint_mode, prefer_a, prefer_b)
    if error:
        raise ValueError(error)
    if midpoint_mode == "geo":
        result = find_balanced_midpoint(lng1, lat1, lng2, lat2)
        result["method"] = "geo"
        return result
    return find_time_midpoint(
        lng1, lat1, lng2, lat2,
        city=city,
        departure_time=departure_time,
        mode=prefer_a,
    )


# ──────────────────────────────────────────────────────
# Agent 1：规划 Agent
# 职责：理解用户自然语言需求 → 结构化搜索参数
# ──────────────────────────────────────────────────────

_PLAN_SYSTEM = """\
你是搜索参数提取专家。根据用户的自然语言需求，提取结构化的搜索参数。
只输出一个JSON对象，不要有任何解释：

{
  "keyword": "高德地图搜索关键词（如：鲁菜、火锅、咖啡馆、电影院、KTV、酒吧）",
  "keyword_fallbacks": ["如果keyword搜索结果不足时的备选词1", "备选词2"],
  "min_rating": 4.0,
  "top_n": 20,
  "poi_category": "餐厅/咖啡馆/酒吧/电影院/KTV/购物/其他",
  "notes": "其他特殊需求，如价格范围、环境要求等，没有则空字符串",
  "sort_weights": {
    "rating": 0.15,
    "total_time": 0.05,
    "time_diff": 0.8
  }
}

规则：
- keyword 要精准（"鲁菜"而非"好吃的鲁菜餐厅"）
- min_rating：用户提到"评分高"→4.5，"评分4.5以上"→4.5，未提及→4.0
- top_n 固定为20（前端会提供数量选择器让用户筛选）
- keyword_fallbacks 提供2个备选，从窄到宽（例如"鲁菜"的备选：["山东菜","中餐"]）
- sort_weights 三项之和必须等于1.0，根据用户意图调整：
  * 默认（未明确提及）：rating=0.15, total_time=0.05, time_diff=0.8
  * 用户强调"近"/"不要太远"/"方便"：rating=0.15, total_time=0.4, time_diff=0.45
  * 用户强调"评分高"/"好评"/"口碑"：rating=0.5, total_time=0.05, time_diff=0.45
  * 用户强调"公平"/"两边一样远"/"均衡"：rating=0.05, total_time=0.05, time_diff=0.9
  * 用户同时强调近和公平：rating=0.05, total_time=0.25, time_diff=0.7
"""


def agent_plan(user_query: str) -> dict:
    """
    Agent 1：规划 Agent
    输入：用户需求文字
    输出：结构化搜索参数
    """
    logger.info(f"[Agent1/规划] 分析需求: {user_query}")
    content = chat_complete(
        messages=[{"role": "user", "content": user_query}],
        system=_PLAN_SYSTEM,
        max_tokens=400,
    )
    if content:
        plan = _extract_json(content)
        if isinstance(plan, dict) and "keyword" in plan:
            logger.info(f"[Agent1/规划] 成功提取搜索参数")
            return plan

    # 降级：直接用用户输入作为 keyword
    logger.warning("[Agent1/规划] LLM 提取失败，使用用户输入作为 keyword")
    return {
        "keyword": user_query,
        "keyword_fallbacks": [],
        "min_rating": 4.0,
        "top_n": 20,
        "poi_category": "未知",
        "notes": "",
    }


# ──────────────────────────────────────────────────────
# Agent 2：搜索 Agent
# 职责：调用地图 API 搜索候选地点
# ──────────────────────────────────────────────────────

# Anthropic Messages 格式的工具定义（Bedrock 原生支持 input_schema）
_SEARCH_TOOLS = [
    {
        "name": "find_midpoint",
        "description": "计算 A、B 两点的地理中点和建议搜索半径",
        "input_schema": {
            "type": "object",
            "properties": {
                "lng1": {"type": "number"},
                "lat1": {"type": "number"},
                "lng2": {"type": "number"},
                "lat2": {"type": "number"},
            },
            "required": ["lng1", "lat1", "lng2", "lat2"],
        },
    },
    {
        "name": "search_pois_nearby",
        "description": (
            "在中心点周边搜索地点。"
            "若结果数量 < 3，可用更大 radius 或不同 keyword 重试。最多重试 2 次。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "center_lng": {"type": "number"},
                "center_lat": {"type": "number"},
                "keyword":    {"type": "string", "description": "搜索关键词"},
                "radius":     {"type": "integer", "description": "搜索半径（米）"},
            },
            "required": ["center_lng", "center_lat", "keyword", "radius"],
        },
    },
]

_SEARCH_SYSTEM = """\
你是地图POI搜索专家，负责在两地之间找到候选地点。

工作步骤：
1. 调用 find_midpoint 获取中间点坐标和建议搜索半径
2. 用 search_pois_nearby 搜索（使用给定的keyword和radius）
3. 如果结果 < 3 个：
   - 先尝试扩大radius为原来的1.5倍重试
   - 若还不够，换用备选keyword再试
4. 完成后输出一个JSON：{"found": true, "count": N, "midpoint": {...}, "search_radius_m": N}
   或 {"found": false, "reason": "..."}

重要：不要自行排名或过滤结果，只负责搜索。
"""


def agent_search(
    location_a: dict, location_b: dict,
    plan: dict, city: str, search_ctx: dict,
    departure_time: str | None = None,
    prefer_a: str = "auto", prefer_b: str = "auto",
    midpoint_mode: str = "geo",
) -> dict:
    """
    Agent 2：搜索 Agent（Anthropic Messages 工具调用格式）
    search_ctx: 共享字典，工具执行时会往里写入完整 POI 数据
    返回：{"success": bool, "midpoint": dict, "search_radius_m": int}
    """
    keyword = plan.get("keyword", "餐厅")
    fallbacks = plan.get("keyword_fallbacks", [])
    logger.info(f"[Agent2/搜索] keyword={keyword}, fallbacks={fallbacks}")

    search_ctx["city"] = city
    search_ctx["departure_time"] = departure_time
    search_ctx["prefer_a"] = prefer_a
    search_ctx["prefer_b"] = prefer_b
    search_ctx["midpoint_mode"] = midpoint_mode

    messages = [
        {
            "role": "user",
            "content": (
                f"地点A: ({location_a['lng']}, {location_a['lat']})\n"
                f"地点B: ({location_b['lng']}, {location_b['lat']})\n"
                f"搜索关键词: {keyword}\n"
                f"备选关键词（搜索不到时使用）: {', '.join(fallbacks) or '无'}\n"
                f"城市: {city}\n"
                f"请按步骤完成搜索。"
            ),
        },
    ]

    for round_i in range(10):
        resp = chat_complete_with_tools(
            messages=messages,
            tools=_SEARCH_TOOLS,
            system=_SEARCH_SYSTEM,
            max_tokens=1000,
        )

        if resp.get("error"):
            break

        stop_reason = resp.get("stop_reason", "")
        content_blocks = resp.get("content", [])

        # 把 assistant 的完整回复（含 tool_use blocks）加入 messages
        messages.append({"role": "assistant", "content": content_blocks})

        # 判断是否有工具调用
        tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]

        if stop_reason == "end_turn" or not tool_use_blocks:
            # LLM 已结束，从搜索上下文取最终结果
            midpoint = search_ctx.get("last_midpoint", {
                "lng": (location_a["lng"] + location_b["lng"]) / 2,
                "lat": (location_a["lat"] + location_b["lat"]) / 2,
            })
            radius = search_ctx.get("last_radius", 3000)
            found_count = len(search_ctx.get("pois", []))
            logger.info(f"[Agent2/搜索] 完成，找到 {found_count} 个POI，共 {round_i + 1} 轮")
            return {
                "success": found_count > 0,
                "midpoint": midpoint,
                "midpoint_details": search_ctx.get(
                    "last_midpoint_details", {}
                ),
                "search_radius_m": radius,
            }

        # 执行工具，构造 tool_result 消息（Anthropic Messages 格式）
        tool_results_content = []
        for block in tool_use_blocks:
            tool_id = block.get("id", "")
            tool_name = block.get("name", "")
            tool_input = block.get("input", {})
            result = _exec_search_tool(tool_name, tool_input, search_ctx)
            logger.info(f"[Agent2/搜索] 工具 {tool_name}: {str(result)[:120]}")
            tool_results_content.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": json.dumps(result, ensure_ascii=False),
            })

        messages.append({"role": "user", "content": tool_results_content})

    return {
        "success": len(search_ctx.get("pois", [])) > 0,
        "midpoint": search_ctx.get("last_midpoint", {}),
        "midpoint_details": search_ctx.get("last_midpoint_details", {}),
        "search_radius_m": search_ctx.get("last_radius", 3000),
    }


def _exec_search_tool(name: str, args: dict, search_ctx: dict) -> dict:
    """执行搜索工具：完整数据存入 search_ctx，压缩版返回给 LLM"""
    if name == "find_midpoint":
        result = compute_requested_midpoint(
            args["lng1"], args["lat1"],
            args["lng2"], args["lat2"],
            midpoint_mode=search_ctx.get("midpoint_mode", "geo"),
            prefer_a=search_ctx.get("prefer_a", "auto"),
            prefer_b=search_ctx.get("prefer_b", "auto"),
            city=search_ctx.get("city", "北京"),
            departure_time=search_ctx.get("departure_time"),
        )
        logger.info(
            "[Agent2/搜索] 实际中点方式=%s %s",
            result.get("method"),
            result.get("note", ""),
        )
        search_ctx["last_midpoint"] = result.get("midpoint", {})
        search_ctx["last_radius"]   = result.get("suggested_search_radius_m", 3000)
        search_ctx["last_midpoint_details"] = result
        return result

    if name == "search_pois_nearby":
        full_result = amap_search_nearby(
            args["center_lng"], args["center_lat"],
            args["keyword"], args.get("radius", 3000),
        )
        full_pois = full_result.get("pois", [])

        # 按名称去重，累积完整数据供后续路线计算
        if full_pois:
            existing = {p["name"] for p in search_ctx.get("pois", [])}
            for p in full_pois:
                if p["name"] not in existing:
                    search_ctx.setdefault("pois", []).append(p)

        # 返回给 LLM 的压缩版（只告知数量，附前 5 条预览）
        return {
            "success": full_result.get("success", False),
            "count":   len(full_pois),
            "keyword": args["keyword"],
            "radius":  args.get("radius", 3000),
            "sample":  [_compact_poi(p) for p in full_pois[:5]],
        }

    return {"success": False, "error": f"未知工具: {name}"}


# ──────────────────────────────────────────────────────
# 路线计算（纯 Python，不走 LLM）
# ──────────────────────────────────────────────────────

def calculate_routes(
    pois: list,
    location_a: dict, location_b: dict,
    prefer_a: str = "auto", prefer_b: str = "auto",
    city: str = "北京",
    departure_time: str | None = None,
    sort_weights: dict | None = None,
    geometry_cache: dict | None = None,
) -> list:
    """对每个 POI 分别计算从 A、从 B 出发的最优路线，并综合排名。"""
    w = sort_weights or {}
    w_rating     = float(w.get("rating",     0.15))
    w_total_time = float(w.get("total_time", 0.05))
    w_time_diff  = float(w.get("time_diff",  0.8))
    w_sum = w_rating + w_total_time + w_time_diff or 1.0
    w_rating     /= w_sum
    w_total_time /= w_sum
    w_time_diff  /= w_sum

    logger.info(f"[排序权重] 评分×{w_rating:.2f}  总时间×{w_total_time:.2f}  时间差×{w_time_diff:.2f}")

    # 绝对量纲上限（保证不同指标之间可真实比较，而非相对批次最大值）
    # 评分：高德满分 5.0；时间差：超过 30 分钟体感已经"很不公平"；总时间：超过 90 分钟视为"很远"
    _ABS_MAX_RATING     = 5.0
    _ABS_MAX_TIME_DIFF  = 30.0   # 分钟
    _ABS_MAX_TOTAL_TIME = 90.0   # 分钟

    enriched = []
    for idx, poi in enumerate(pois):
        p = dict(poi)

        route_a = amap_get_best_route(
            location_a["lng"], location_a["lat"],
            poi["lng"], poi["lat"],
            city, prefer_a, departure_time,
        )
        p["transport_from_a"] = _format_route(route_a)

        route_b = amap_get_best_route(
            location_b["lng"], location_b["lat"],
            poi["lng"], poi["lat"],
            city, prefer_b, departure_time,
        )
        p["transport_from_b"] = _format_route(route_b)

        route_id = p.get("route_id")
        if geometry_cache is not None and route_id:
            errors = {}
            if not route_a.get("geometry"):
                errors["a"] = route_a.get("error", "未获得路线轨迹")
            if not route_b.get("geometry"):
                errors["b"] = route_b.get("error", "未获得路线轨迹")
            geometry_cache[route_id] = {
                "route_a": route_a.get("geometry"),
                "route_b": route_b.get("geometry"),
                "errors": errors,
            }

        dur_a = p["transport_from_a"]["duration_minutes"]
        dur_b = p["transport_from_b"]["duration_minutes"]
        p["time_diff_minutes"]  = abs(dur_a - dur_b)
        p["total_time_minutes"] = dur_a + dur_b

        enriched.append(p)

        # 主动限速：每个 POI 计算完后等待，避免连续请求触发高德 3 次/秒限速
        # amap_get_best_route 内部单个 POI 可能产生 2-4 次 API 调用（A/B 各多种交通）
        if idx < len(pois) - 1:
            time.sleep(_ROUTE_INTER_POI_DELAY)

    # 绝对量纲归一化后加权综合评分
    # 各指标除以固定上限而非批次最大值，避免小差距被人为放大
    for p in enriched:
        rating_norm     = min(p.get("rating", 0) / _ABS_MAX_RATING, 1.0)
        total_time_norm = min(p["total_time_minutes"] / _ABS_MAX_TOTAL_TIME, 1.0)
        time_diff_norm  = min(p["time_diff_minutes"]  / _ABS_MAX_TIME_DIFF,  1.0)
        p["_score"] = round(
            rating_norm     *  w_rating
            - total_time_norm *  w_total_time
            - time_diff_norm  *  w_time_diff,
            4,
        )

    enriched.sort(key=lambda x: x["_score"], reverse=True)
    return enriched


def filter_and_rank_pois(
    pois: list, min_rating: float = 4.0, top_n: int = 20
) -> list:
    """筛选评分 >= min_rating，按评分降序，取前 top_n 个（最多 20）"""
    top_n = min(top_n, 20)
    filtered = [p for p in pois if p.get("rating", 0) >= min_rating]
    filtered.sort(key=lambda x: x.get("rating", 0), reverse=True)
    if len(filtered) < 3 and pois:
        filtered = sorted(pois, key=lambda x: x.get("rating", 0), reverse=True)
    return filtered[:top_n]


# ──────────────────────────────────────────────────────
# Agent 3：总结 Agent
# ──────────────────────────────────────────────────────

_SUMMARY_SYSTEM = """\
你是一个简洁友好的推荐助手。根据给出的地点数据，用2-4句话概括推荐结果。
重点提炼：
- 在哪个区域找到了什么类型的地点
- 评分最高的是哪家，双方交通是否均衡
- 如果有地方双方用时差距大，提醒一下
语气活泼简洁，不要罗列所有细节，不要用"首先其次"等套话。
"""


def agent_summarize(
    query: str,
    location_a: dict, location_b: dict,
    enriched_pois: list,
    prefer_a: str, prefer_b: str,
) -> str:
    """Agent 3：总结 Agent，生成 2-4 句推荐文字"""
    logger.info("[Agent3/总结] 生成推荐文字...")
    mode_label = {
        "auto": "最快方式", "transit": "公交地铁",
        "driving": "驾车", "cycling": "骑行", "walking": "步行",
    }

    pois_summary = []
    for i, p in enumerate(enriched_pois[:5]):
        t_a = p.get("transport_from_a", {})
        t_b = p.get("transport_from_b", {})
        pois_summary.append(
            f"{i + 1}. {p['name']}（评分{p.get('rating', 0):.1f}）"
            f" - A {mode_label.get(t_a.get('mode', '?'), t_a.get('mode', '?'))} {t_a.get('duration_text', '?')}"
            f" / B {mode_label.get(t_b.get('mode', '?'), t_b.get('mode', '?'))} {t_b.get('duration_text', '?')}"
            f"，时间差{p.get('time_diff_minutes', '?')}分钟"
        )

    user_msg = (
        f"用户需求：{query}\n"
        f"A（{location_a.get('name', '地点A')}）出行方式：{mode_label.get(prefer_a, prefer_a)}\n"
        f"B（{location_b.get('name', '地点B')}）出行方式：{mode_label.get(prefer_b, prefer_b)}\n"
        f"找到地点：\n" + "\n".join(pois_summary)
    )

    result = chat_complete(
        messages=[{"role": "user", "content": user_msg}],
        system=_SUMMARY_SYSTEM,
        max_tokens=300,
    )
    return result or f"已找到 {len(enriched_pois)} 个推荐地点。"


# ──────────────────────────────────────────────────────
# 完整流水线
# ──────────────────────────────────────────────────────

def run_pipeline(
    user_query: str,
    location_a: dict, location_b: dict,
    city: str = "北京",
    prefer_a: str = "auto", prefer_b: str = "auto",
    departure_time: str | None = None,
    midpoint_mode: str = "geo",
    candidate_count: int = 10,
) -> dict:
    """运行完整多 Agent 流水线，返回 session_id 及结果。"""
    candidate_count = parse_candidate_count(candidate_count)
    plan = agent_plan(user_query)
    plan["top_n"] = candidate_count

    search_ctx: dict = {"pois": []}
    search_result = agent_search(
        location_a, location_b, plan, city, search_ctx,
        departure_time, prefer_a, prefer_b, midpoint_mode,
    )

    if not search_result.get("success") or not search_ctx.get("pois"):
        return {"success": False, "error": "未能找到符合条件的地点，请尝试修改关键词或扩大范围"}

    midpoint        = search_result.get("midpoint", {})
    midpoint_details = search_result.get("midpoint_details", {})
    search_radius_m = search_result.get("search_radius_m", 3000)
    if midpoint_mode == "geo":
        geo_midpoint_details = midpoint_details
    else:
        geo_midpoint_details = compute_requested_midpoint(
            location_a["lng"], location_a["lat"],
            location_b["lng"], location_b["lat"],
            midpoint_mode="geo",
            prefer_a=prefer_a,
            prefer_b=prefer_b,
            city=city,
            departure_time=departure_time,
        )

    top_pois = filter_and_rank_pois(
        search_ctx["pois"],
        min_rating=plan.get("min_rating", 4.0),
        top_n=candidate_count,
    )
    top_pois = [
        {**poi, "route_id": f"r{index}"}
        for index, poi in enumerate(top_pois)
    ]
    logger.info(f"[Pipeline] 筛选后 {len(top_pois)} 个POI")

    route_geometries: dict = {}
    enriched = calculate_routes(
        top_pois, location_a, location_b,
        prefer_a, prefer_b, city, departure_time,
        sort_weights=plan.get("sort_weights"),
        geometry_cache=route_geometries,
    )

    summary_text = agent_summarize(
        user_query, location_a, location_b, enriched, prefer_a, prefer_b
    )

    session_id = session_create({
        "location_a":     location_a,
        "location_b":     location_b,
        "city":           city,
        "query":          user_query,
        "plan":           plan,
        "candidate_count": candidate_count,
        "midpoint":       midpoint,
        "midpoint_details": midpoint_details,
        "geo_midpoint_details": geo_midpoint_details,
        "midpoint_mode":  midpoint_mode,
        "search_radius_m": search_radius_m,
        "pois_base":      top_pois,
        "departure_time": departure_time,
        "prefer_a":       prefer_a,
        "prefer_b":       prefer_b,
        "route_geometries": route_geometries,
    })

    return {
        "success":         True,
        "session_id":      session_id,
        "summary":         summary_text,
        "plan":            plan,
        "candidate_count": candidate_count,
        "midpoint":        midpoint,
        "midpoint_details": midpoint_details,
        "midpoint_mode":   midpoint_mode,
        "search_radius_m": search_radius_m,
        "pois":            enriched,
        "prefer_a":        prefer_a,
        "prefer_b":        prefer_b,
    }


# ──────────────────────────────────────────────────────
# FastAPI 应用
# ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(application: FastAPI):
    logger.info("=" * 60)
    logger.info("  智能中间点推荐系统 v2（多 Agent 架构）")
    logger.info("=" * 60)
    logger.info(f"  AI（Runway）: {'已配置' if is_ai_enabled() else '未配置 ⚠️'}")
    logger.info(f"  高德地图 API Key: {'已配置' if AMAP_KEY else '未配置 ⚠️'}")
    logger.info(f"  Session 缓存: 内存（TTL {SESSION_TTL // 3600} 小时）")
    logger.info("=" * 60)
    yield


app = FastAPI(title="智能中间点推荐系统", lifespan=lifespan)


# ──────────────────────────────────────────────────────
# 健康检查（Guard 通过 loopback 127.0.0.1:3000/health 探活）
# 注意：前端请勿直接访问 /health，Guard 反代层会截胡返回自身 JSON
# 前端如需状态信息，使用 /api/status
# ──────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# 前端可访问的状态接口（/health 被 Guard/Agent 反代层保留，不能用）
@app.get("/api/status")
async def api_status():
    return {
        "ai_enabled":    is_ai_enabled(),
        "has_amap_key":  bool(AMAP_KEY),
        "version":       "v2",
    }


# ──────────────────────────────────────────────────────
# API 路由（注意：必须在 StaticFiles mount 之前注册）
# ──────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    """前端初始化时获取配置（高德 JS Key、AI 状态等）"""
    return {
        "amap_key":     AMAP_JS_KEY,
        "has_amap_key": bool(AMAP_JS_KEY),
        "ai_enabled":   is_ai_enabled(),
        "version":      "v2",
    }


@app.post("/api/v2/search")
async def api_v2_search(request: Request):
    """
    主搜索接口：运行完整多 Agent 流水线
    返回 session_id（可供后续调用 /api/v2/routes 重算路线）
    """
    data           = await request.json()
    location_a     = data.get("location_a")
    location_b     = data.get("location_b")
    user_query     = data.get("query", "")
    city           = data.get("city", "北京")
    prefer_a       = data.get("prefer_a", "auto")
    prefer_b       = data.get("prefer_b", "auto")
    departure_time = data.get("departure_time") or None
    midpoint_mode  = data.get("midpoint_mode", "geo")

    try:
        candidate_count = parse_candidate_count(data.get("candidate_count"))
    except ValueError as error:
        return JSONResponse(
            {"success": False, "error": str(error)},
            status_code=400,
        )

    if not location_a or not location_b:
        return JSONResponse({"success": False, "error": "请提供两个地点的坐标"}, status_code=400)
    if not user_query:
        return JSONResponse({"success": False, "error": "请描述您的需求"}, status_code=400)
    midpoint_error = validate_midpoint_choice(
        midpoint_mode, prefer_a, prefer_b
    )
    if midpoint_error:
        return JSONResponse(
            {"success": False, "error": midpoint_error},
            status_code=400,
        )
    if not AMAP_KEY:
        return JSONResponse({"success": False, "error": "高德地图 API Key 未配置"}, status_code=500)
    if not is_ai_enabled():
        return JSONResponse({"success": False, "error": "AI 服务未配置"}, status_code=500)

    try:
        # 流水线含大量同步阻塞调用（requests + time.sleep），丢进线程池
        # 避免占用事件循环，其他用户的请求可并发处理
        result = await asyncio.to_thread(
            run_pipeline,
            user_query, location_a, location_b,
            city, prefer_a, prefer_b, departure_time, midpoint_mode,
            candidate_count,
        )
        status = 200 if result.get("success") else 500
        return JSONResponse(result, status_code=status)
    except Exception:
        logger.exception("[api/v2/search] 流水线执行异常")
        return JSONResponse({"success": False, "error": "服务内部错误，请稍后重试"}, status_code=500)


@app.post("/api/v2/routes")
async def api_v2_routes(request: Request):
    """
    路线重算接口：复用缓存的搜索结果，只重新计算路线。
    适合切换出行方式的场景，无需重新搜索。
    """
    data           = await request.json()
    session_id     = data.get("session_id")
    prefer_a       = data.get("prefer_a", "auto")
    prefer_b       = data.get("prefer_b", "auto")
    departure_time = data.get("departure_time") or None

    if not session_id:
        return JSONResponse({"success": False, "error": "缺少 session_id，请先执行搜索"}, status_code=400)

    session = session_get(session_id)
    if not session:
        return JSONResponse({"success": False, "error": "会话已过期，请重新搜索"}, status_code=404)

    pois_base = session.get("pois_base", [])
    if not pois_base:
        return JSONResponse({"success": False, "error": "缓存的搜索结果为空"}, status_code=400)

    if departure_time is None:
        departure_time = session.get("departure_time")
    midpoint_mode = data.get(
        "midpoint_mode", session.get("midpoint_mode", "geo")
    )
    midpoint_error = validate_midpoint_choice(
        midpoint_mode, prefer_a, prefer_b
    )
    if midpoint_error:
        return JSONResponse(
            {"success": False, "error": midpoint_error},
            status_code=400,
        )

    try:
        location_a = session["location_a"]
        location_b = session["location_b"]
        city = session.get("city", "北京")
        if midpoint_mode == "transport":
            midpoint_details = await asyncio.to_thread(
                compute_requested_midpoint,
                location_a["lng"], location_a["lat"],
                location_b["lng"], location_b["lat"],
                midpoint_mode=midpoint_mode,
                prefer_a=prefer_a,
                prefer_b=prefer_b,
                city=city,
                departure_time=departure_time,
            )
            midpoint = midpoint_details.get(
                "midpoint", session.get("midpoint", {})
            )
            search_radius_m = midpoint_details.get(
                "suggested_search_radius_m",
                session.get("search_radius_m", 3000),
            )
        else:
            midpoint_details = session.get("geo_midpoint_details")
            if not midpoint_details:
                midpoint_details = compute_requested_midpoint(
                    location_a["lng"], location_a["lat"],
                    location_b["lng"], location_b["lat"],
                    midpoint_mode="geo",
                    prefer_a=prefer_a,
                    prefer_b=prefer_b,
                    city=city,
                    departure_time=departure_time,
                )
            midpoint = midpoint_details.get("midpoint", {})
            search_radius_m = midpoint_details.get(
                "suggested_search_radius_m", 3000
            )

        route_geometries: dict = {}
        # 路线计算同样阻塞，丢进线程池
        enriched = await asyncio.to_thread(
            calculate_routes,
            pois_base,
            location_a, location_b,
            prefer_a, prefer_b,
            city,
            departure_time,
            geometry_cache=route_geometries,
        )
        session_update(session_id, {
            "prefer_a": prefer_a,
            "prefer_b": prefer_b,
            "departure_time": departure_time,
            "midpoint": midpoint,
            "midpoint_details": midpoint_details,
            "geo_midpoint_details": (
                midpoint_details if midpoint_mode == "geo"
                else session.get("geo_midpoint_details", {})
            ),
            "midpoint_mode": midpoint_mode,
            "search_radius_m": search_radius_m,
            "route_geometries": route_geometries,
        })
        return JSONResponse({
            "success":         True,
            "session_id":      session_id,
            "pois":            enriched,
            "prefer_a":        prefer_a,
            "prefer_b":        prefer_b,
            "midpoint":        midpoint,
            "midpoint_details": midpoint_details,
            "midpoint_mode":   midpoint_mode,
            "search_radius_m": search_radius_m,
        })
    except Exception:
        logger.exception("[api/v2/routes] 路线重算异常")
        return JSONResponse({"success": False, "error": "路线计算失败，请稍后重试"}, status_code=500)


@app.get("/api/v2/session/{session_id}/routes/{route_id}")
async def api_v2_cached_routes(session_id: str, route_id: str):
    """读取单个推荐地点的缓存路线，不再次调用高德。"""
    session = session_get(session_id)
    if not session:
        return JSONResponse(
            {"success": False, "error": "会话已过期，请重新搜索"},
            status_code=404,
        )
    cached = session.get("route_geometries", {}).get(route_id)
    if cached is None:
        return JSONResponse(
            {"success": False, "error": "未找到该地点的路线缓存"},
            status_code=404,
        )
    return {"success": True, "route_id": route_id, **cached}


@app.get("/api/v2/session/{session_id}")
async def api_v2_session_info(session_id: str):
    """查看 Session 摘要（调试用）"""
    s = session_get(session_id)
    if not s:
        return JSONResponse({"exists": False}, status_code=404)
    return {
        "exists":    True,
        "query":     s.get("query"),
        "city":      s.get("city"),
        "poi_count": len(s.get("pois_base", [])),
        "prefer_a":  s.get("prefer_a"),
        "prefer_b":  s.get("prefer_b"),
        "plan":      s.get("plan"),
        "candidate_count": s.get("candidate_count", 10),
    }


@app.post("/api/geocode")
async def api_geocode(request: Request):
    data = await request.json()
    address = data.get("address", "")
    if not address:
        return JSONResponse({"success": False, "error": "请提供地址"}, status_code=400)
    return await asyncio.to_thread(amap_geocode, address)


@app.post("/api/nearby-search")
async def api_nearby_search(request: Request):
    """
    查询某坐标附近的 POI（用于卡片内"附近搜索"功能）。
    请求：{ lng, lat, keyword, radius_m (可选，默认 1000) }
    返回：{ success, pois: [{name, address, distance_m, rating, lng, lat, type}] }
    """
    data     = await request.json()
    lng      = data.get("lng")
    lat      = data.get("lat")
    keyword  = data.get("keyword", "").strip()
    radius_m = min(int(data.get("radius_m", 1000)), 5000)

    if not lng or not lat or not keyword:
        return JSONResponse({"success": False, "error": "缺少参数 lng/lat/keyword"}, status_code=400)

    url = "https://restapi.amap.com/v3/place/around"
    params = {
        "key":       AMAP_KEY,
        "location":  f"{lng},{lat}",
        "keywords":  keyword,
        "radius":    radius_m,
        "offset":    10,
        "page":      1,
        "extensions": "base",
        "output":    "json",
    }
    try:
        resp   = await asyncio.to_thread(requests.get, url, params=params, timeout=8)
        result = resp.json()
    except Exception:
        logger.exception("[api/nearby-search] 高德 API 请求失败")
        return JSONResponse({"success": False, "error": "地图服务请求失败", "pois": []})

    if result.get("status") != "1":
        return JSONResponse({"success": False, "error": "搜索失败", "pois": []})

    pois = []
    for p in result.get("pois", []):
        loc = p.get("location", "").split(",")
        if len(loc) != 2:
            continue
        try:
            plng, plat = float(loc[0]), float(loc[1])
            dist       = int(haversine_distance(lng, lat, plng, plat) * 1000)
            biz_ext    = p.get("biz_ext") or {}
            rating     = float(biz_ext.get("rating", 0) or 0) if isinstance(biz_ext, dict) else 0.0
            pois.append({
                "name":       p.get("name", ""),
                "address":    p.get("address", "") if isinstance(p.get("address"), str) else "",
                "type":       p.get("type", ""),
                "distance_m": dist,
                "rating":     rating,
                "lng":        plng,
                "lat":        plat,
            })
        except (ValueError, TypeError):
            continue

    pois.sort(key=lambda x: x["distance_m"])
    return {"success": True, "pois": pois[:10]}


@app.post("/api/geocode-suggest")
async def api_geocode_suggest(request: Request):
    """
    地点输入提示接口 — 前端搜索框下拉候选。
    调用高德 /v3/assistant/inputtips，返回带坐标的候选列表。
    """
    data    = await request.json()
    keyword = data.get("keyword", "").strip()
    if not keyword:
        return {"tips": []}

    url = "https://restapi.amap.com/v3/assistant/inputtips"
    params = {
        "key":      AMAP_KEY,
        "keywords": keyword,
        "datatype": "all",
        "output":   "json",
    }
    try:
        resp   = await asyncio.to_thread(requests.get, url, params=params, timeout=8)
        result = resp.json()
    except Exception:
        logger.exception("[api/geocode-suggest] 高德 API 请求失败")
        return {"tips": []}

    tips = []
    if result.get("status") == "1":
        for tip in result.get("tips", []):
            location = tip.get("location", "")
            if not location or location == "[]":
                continue
            try:
                lng_s, lat_s = location.split(",")
                tips.append({
                    "name":     tip.get("name", ""),
                    "district": tip.get("district", ""),
                    "address":  tip.get("address", "") if isinstance(tip.get("address"), str) else "",
                    "lng":      float(lng_s),
                    "lat":      float(lat_s),
                })
            except (ValueError, TypeError):
                continue

    return {"tips": tips[:6]}


# ──────────────────────────────────────────────────────
# 静态文件挂载（必须放在所有 API 路由注册之后）
# ──────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory="static", html=True), name="static")
