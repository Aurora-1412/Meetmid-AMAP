"""
智能中间点推荐系统 v2 — 多 Agent 架构
======================================
Agent1（规划）:  LLM 理解需求 → 结构化搜索参数
Agent2（搜索）:  LLM + 受控工具 → 搜索候选地点（上下文精简）
路线计算:        纯 Python 直接调高德 API → A/B 分别计算
Agent3（总结）:  LLM 生成推荐文字

入口: python app_v2.py
"""

import os
import json
import uuid
import time
import re
import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from openai import OpenAI

# 路线计算时每个 POI 调用 2 次 amap_get_best_route（A + B），
# 每次内部还可能调多种交通方式。批量计算极易触发高德 3 次/秒限速。
# 此间隔配合 amap_client._amap_get 的重试，双重保障。
_ROUTE_INTER_POI_DELAY = 0.35  # 秒，每个 POI 计算完后等待

from amap_client import (
    DEEPSEEK_API_KEY,
    AMAP_KEY,
    AMAP_JS_KEY,
    amap_geocode,
    amap_get_best_route,
    amap_search_nearby,
    haversine_distance,
    find_balanced_midpoint,
)

app = Flask(__name__, static_folder="static")
CORS(app)

llm_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)


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
    # 查询失败时保留 error 字段，duration_text 置为 None（前端据此显示友好提示）
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


# ──────────────────────────────────────────────────────
# Agent 1：规划 Agent
# 职责：理解用户自然语言需求 → 结构化搜索参数
# 特点：无工具调用，上下文极短（~200 tokens in/out）
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
    "rating": 0.6,
    "total_time": 0.3,
    "time_diff": 0.1
  }
}

规则：
- keyword 要精准（"鲁菜"而非"好吃的鲁菜餐厅"）
- min_rating：用户提到"评分高"→4.5，"评分4.5以上"→4.5，未提及→4.0
- top_n 固定为20（前端会提供数量选择器让用户筛选）
- keyword_fallbacks 提供2个备选，从窄到宽（例如"鲁菜"的备选：["山东菜","中餐"]）
- sort_weights 三项之和必须等于1.0，根据用户意图调整：
  * 默认（未明确提及）：rating=0.6, total_time=0.3, time_diff=0.1
  * 用户强调"近"/"不要太远"/"方便"：rating=0.3, total_time=0.6, time_diff=0.1
  * 用户强调"评分高"/"好评"/"口碑"：rating=0.8, total_time=0.15, time_diff=0.05
  * 用户强调"公平"/"两边一样远"/"均衡"：rating=0.4, total_time=0.2, time_diff=0.4
  * 用户同时强调近和公平：rating=0.2, total_time=0.4, time_diff=0.4
"""


def agent_plan(user_query: str) -> dict:
    """
    Agent 1：规划 Agent
    输入：用户需求文字
    输出：结构化搜索参数
    """
    print(f"[Agent1/规划] 分析需求: {user_query}")
    try:
        resp = llm_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": _PLAN_SYSTEM},
                {"role": "user",   "content": user_query},
            ],
            temperature=0.1,
            max_tokens=400,
        )
        content = resp.choices[0].message.content or ""
        plan = _extract_json(content)
        if isinstance(plan, dict) and "keyword" in plan:
            print(f"[Agent1/规划] 结果: {plan}")
            return plan
    except Exception as e:
        print(f"[Agent1/规划] 错误: {e}")

    # 降级：直接用用户输入作为 keyword
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
# 特点：
#   - 只暴露 find_midpoint + search_pois_nearby 两个工具
#   - 工具返回给 LLM 的是压缩版，完整数据存 Python 侧 search_ctx
#   - 最多 10 轮工具调用
# ──────────────────────────────────────────────────────

_SEARCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_midpoint",
            "description": "计算 A、B 两点的地理中点和建议搜索半径",
            "parameters": {
                "type": "object",
                "properties": {
                    "lng1": {"type": "number"}, "lat1": {"type": "number"},
                    "lng2": {"type": "number"}, "lat2": {"type": "number"},
                },
                "required": ["lng1", "lat1", "lng2", "lat2"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_pois_nearby",
            "description": (
                "在中心点周边搜索地点。"
                "若结果数量 < 3，可用更大 radius 或不同 keyword 重试。最多重试 2 次。"
            ),
            "parameters": {
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
) -> dict:
    """
    Agent 2：搜索 Agent
    search_ctx: 共享字典，工具执行时会往里写入完整 POI 数据
    返回：{"success": bool, "midpoint": dict, "search_radius_m": int}
    """
    keyword = plan.get("keyword", "餐厅")
    fallbacks = plan.get("keyword_fallbacks", [])
    print(f"[Agent2/搜索] keyword={keyword}, fallbacks={fallbacks}")

    messages = [
        {"role": "system", "content": _SEARCH_SYSTEM},
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
        resp = llm_client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            tools=_SEARCH_TOOLS,
            tool_choice="auto",
            temperature=0.1,
            max_tokens=1000,
        )
        msg = resp.choices[0].message
        finish = resp.choices[0].finish_reason

        if finish == "stop" or not msg.tool_calls:
            midpoint = search_ctx.get("last_midpoint", {
                "lng": (location_a["lng"] + location_b["lng"]) / 2,
                "lat": (location_a["lat"] + location_b["lat"]) / 2,
            })
            radius = search_ctx.get("last_radius", 3000)
            found_count = len(search_ctx.get("pois", []))
            print(f"[Agent2/搜索] 完成，找到 {found_count} 个POI，共 {round_i + 1} 轮")
            return {
                "success": found_count > 0,
                "midpoint": midpoint,
                "search_radius_m": radius,
            }

        messages.append(msg)

        tool_results = []
        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)
            result = _exec_search_tool(name, args, search_ctx)
            print(f"[Agent2/搜索] 工具 {name}: {str(result)[:120]}")
            tool_results.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
        messages.extend(tool_results)

    return {
        "success": len(search_ctx.get("pois", [])) > 0,
        "midpoint": search_ctx.get("last_midpoint", {}),
        "search_radius_m": search_ctx.get("last_radius", 3000),
    }


def _exec_search_tool(name: str, args: dict, search_ctx: dict) -> dict:
    """执行搜索工具：完整数据存入 search_ctx，压缩版返回给 LLM"""
    if name == "find_midpoint":
        result = find_balanced_midpoint(
            args["lng1"], args["lat1"],
            args["lng2"], args["lat2"],
        )
        search_ctx["last_midpoint"] = result.get("midpoint", {})
        search_ctx["last_radius"]   = result.get("suggested_search_radius_m", 3000)
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
# 职责：A/B 分别独立计算，支持不同出行方式，可重复调用
# ──────────────────────────────────────────────────────

def calculate_routes(
    pois: list,
    location_a: dict, location_b: dict,
    prefer_a: str = "auto", prefer_b: str = "auto",
    city: str = "北京",
    departure_time: str | None = None,
    sort_weights: dict | None = None,
) -> list:
    """
    对每个 POI 分别计算从 A、从 B 出发的最优路线，并综合排名。
    sort_weights 由 Agent1 从 query 中提取：
        {"rating": 0.6, "total_time": 0.3, "time_diff": 0.1}
    """
    w = sort_weights or {}
    w_rating     = float(w.get("rating",     0.6))
    w_total_time = float(w.get("total_time", 0.3))
    w_time_diff  = float(w.get("time_diff",  0.1))
    w_sum = w_rating + w_total_time + w_time_diff or 1.0
    w_rating     /= w_sum
    w_total_time /= w_sum
    w_time_diff  /= w_sum

    print(f"[排序权重] 评分×{w_rating:.2f}  总时间×{w_total_time:.2f}  时间差×{w_time_diff:.2f}")

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

        dur_a = p["transport_from_a"]["duration_minutes"]
        dur_b = p["transport_from_b"]["duration_minutes"]
        p["time_diff_minutes"]  = abs(dur_a - dur_b)
        p["total_time_minutes"] = dur_a + dur_b

        enriched.append(p)

        # 主动限速：每个 POI 计算完后等待，避免连续请求触发高德 3 次/秒限速
        # amap_get_best_route 内部单个 POI 可能产生 2-4 次 API 调用（A/B 各多种交通）
        if idx < len(pois) - 1:
            time.sleep(_ROUTE_INTER_POI_DELAY)

    # 归一化后加权综合评分
    max_total  = max((p["total_time_minutes"] for p in enriched), default=1) or 1
    max_diff   = max((p["time_diff_minutes"]  for p in enriched), default=1) or 1
    max_rating = max((p.get("rating", 0)      for p in enriched), default=5) or 5

    for p in enriched:
        p["_score"] = round(
            (p.get("rating", 0) / max_rating)       *  w_rating
            - (p["total_time_minutes"] / max_total)  *  w_total_time
            - (p["time_diff_minutes"]  / max_diff)   *  w_time_diff,
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
# 职责：根据结构化数据生成自然语言推荐文字
# 特点：无工具，上下文可控（只传摘要）
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
    """
    Agent 3：总结 Agent
    输入：结构化数据（压缩摘要）
    输出：2-4 句推荐文字
    """
    print("[Agent3/总结] 生成推荐文字...")
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

    try:
        resp = llm_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.5,
            max_tokens=300,
        )
        return resp.choices[0].message.content or "已找到推荐地点，请查看地图标注。"
    except Exception as e:
        print(f"[Agent3/总结] 错误: {e}")
        return f"已找到 {len(enriched_pois)} 个推荐地点。"


# ──────────────────────────────────────────────────────
# 完整流水线（供 /api/v2/search 调用）
# ──────────────────────────────────────────────────────

def run_pipeline(
    user_query: str,
    location_a: dict, location_b: dict,
    city: str = "北京",
    prefer_a: str = "auto", prefer_b: str = "auto",
    departure_time: str | None = None,
) -> dict:
    """
    运行完整多 Agent 流水线。
    返回 session_id，前端可用此 ID 再次调用路线计算。
    """
    # ── Agent 1：规划 ──
    plan = agent_plan(user_query)

    # ── Agent 2：搜索 ──
    search_ctx: dict = {"pois": []}
    search_result = agent_search(location_a, location_b, plan, city, search_ctx)

    if not search_result.get("success") or not search_ctx.get("pois"):
        return {"success": False, "error": "未能找到符合条件的地点，请尝试修改关键词或扩大范围"}

    midpoint        = search_result.get("midpoint", {})
    search_radius_m = search_result.get("search_radius_m", 3000)

    # ── 筛选排名（Python 直接处理）──
    top_pois = filter_and_rank_pois(
        search_ctx["pois"],
        min_rating=plan.get("min_rating", 4.0),
        top_n=plan.get("top_n", 20),
    )
    print(f"[Pipeline] 筛选后 {len(top_pois)} 个POI")

    # ── 路线计算（纯 Python，A/B 分别计算）──
    enriched = calculate_routes(
        top_pois, location_a, location_b,
        prefer_a, prefer_b, city, departure_time,
        sort_weights=plan.get("sort_weights"),
    )

    # ── Agent 3：总结 ──
    summary_text = agent_summarize(
        user_query, location_a, location_b, enriched, prefer_a, prefer_b
    )

    # ── 存入 Session（供后续路线重算使用）──
    session_id = session_create({
        "location_a":     location_a,
        "location_b":     location_b,
        "city":           city,
        "query":          user_query,
        "plan":           plan,
        "midpoint":       midpoint,
        "search_radius_m": search_radius_m,
        "pois_base":      top_pois,
        "departure_time": departure_time,
        "prefer_a":       prefer_a,
        "prefer_b":       prefer_b,
    })

    return {
        "success":        True,
        "session_id":     session_id,
        "summary":        summary_text,
        "plan":           plan,
        "midpoint":       midpoint,
        "search_radius_m": search_radius_m,
        "pois":           enriched,
        "prefer_a":       prefer_a,
        "prefer_b":       prefer_b,
    }


# ──────────────────────────────────────────────────────
# Flask 路由
# ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/config")
def get_config():
    return jsonify({
        "amap_key":     AMAP_JS_KEY,
        "has_amap_key": bool(AMAP_JS_KEY),
        "version":      "v2",
    })


@app.route("/api/v2/search", methods=["POST"])
def api_v2_search():
    """
    主搜索接口：运行完整多 Agent 流水线
    返回 session_id（可供后续调用 /api/v2/routes 重算路线）
    """
    data           = request.json or {}
    location_a     = data.get("location_a")
    location_b     = data.get("location_b")
    user_query     = data.get("query", "")
    city           = data.get("city", "北京")
    prefer_a       = data.get("prefer_a", "auto")
    prefer_b       = data.get("prefer_b", "auto")
    departure_time = data.get("departure_time") or None

    if not location_a or not location_b:
        return jsonify({"success": False, "error": "请提供两个地点的坐标"}), 400
    if not user_query:
        return jsonify({"success": False, "error": "请描述您的需求"}), 400
    if not AMAP_KEY:
        return jsonify({"success": False, "error": "高德地图 API Key 未配置"}), 500
    if not DEEPSEEK_API_KEY:
        return jsonify({"success": False, "error": "DeepSeek API Key 未配置"}), 500

    try:
        result = run_pipeline(
            user_query, location_a, location_b,
            city, prefer_a, prefer_b, departure_time,
        )
        return jsonify(result), (200 if result.get("success") else 500)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/v2/routes", methods=["POST"])
def api_v2_routes():
    """
    路线重算接口：复用缓存的搜索结果，只重新计算路线。
    适合切换出行方式的场景，无需重新搜索。
    """
    data           = request.json or {}
    session_id     = data.get("session_id")
    prefer_a       = data.get("prefer_a", "auto")
    prefer_b       = data.get("prefer_b", "auto")
    departure_time = data.get("departure_time") or None

    if not session_id:
        return jsonify({"success": False, "error": "缺少 session_id，请先执行搜索"}), 400

    session = session_get(session_id)
    if not session:
        return jsonify({"success": False, "error": "会话已过期，请重新搜索"}), 404

    pois_base = session.get("pois_base", [])
    if not pois_base:
        return jsonify({"success": False, "error": "缓存的搜索结果为空"}), 400

    if departure_time is None:
        departure_time = session.get("departure_time")

    try:
        enriched = calculate_routes(
            pois_base,
            session["location_a"], session["location_b"],
            prefer_a, prefer_b,
            session.get("city", "北京"),
            departure_time,
        )
        session_update(session_id, {
            "prefer_a": prefer_a,
            "prefer_b": prefer_b,
            "departure_time": departure_time,
        })
        return jsonify({
            "success":        True,
            "session_id":     session_id,
            "pois":           enriched,
            "prefer_a":       prefer_a,
            "prefer_b":       prefer_b,
            "midpoint":       session.get("midpoint", {}),
            "search_radius_m": session.get("search_radius_m", 3000),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/v2/session/<session_id>", methods=["GET"])
def api_v2_session_info(session_id):
    """查看 Session 摘要（调试用）"""
    s = session_get(session_id)
    if not s:
        return jsonify({"exists": False}), 404
    return jsonify({
        "exists":    True,
        "query":     s.get("query"),
        "city":      s.get("city"),
        "poi_count": len(s.get("pois_base", [])),
        "prefer_a":  s.get("prefer_a"),
        "prefer_b":  s.get("prefer_b"),
        "plan":      s.get("plan"),
    })


@app.route("/api/geocode", methods=["POST"])
def api_geocode():
    data = request.json or {}
    address = data.get("address", "")
    if not address:
        return jsonify({"success": False, "error": "请提供地址"}), 400
    return jsonify(amap_geocode(address))


@app.route("/api/nearby-search", methods=["POST"])
def api_nearby_search():
    """
    查询某坐标附近的 POI（用于卡片内"附近搜索"功能）。
    请求：{ lng, lat, keyword, radius_m (可选，默认 1000) }
    返回：{ success, pois: [{name, address, distance_m, rating, lng, lat, type}] }
    """
    data     = request.json or {}
    lng      = data.get("lng")
    lat      = data.get("lat")
    keyword  = data.get("keyword", "").strip()
    radius_m = min(int(data.get("radius_m", 1000)), 5000)

    if not lng or not lat or not keyword:
        return jsonify({"success": False, "error": "缺少参数 lng/lat/keyword"}), 400

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
        resp   = requests.get(url, params=params, timeout=8)
        result = resp.json()
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "pois": []})

    if result.get("status") != "1":
        return jsonify({"success": False, "error": result.get("info", "搜索失败"), "pois": []})

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
    return jsonify({"success": True, "pois": pois[:10]})


@app.route("/api/geocode-suggest", methods=["POST"])
def api_geocode_suggest():
    """
    地点输入提示接口 — 前端搜索框下拉候选。
    调用高德 /v3/assistant/inputtips，返回带坐标的候选列表。
    """
    data    = request.json or {}
    keyword = data.get("keyword", "").strip()
    if not keyword:
        return jsonify({"tips": []})

    url = "https://restapi.amap.com/v3/assistant/inputtips"
    params = {
        "key":      AMAP_KEY,
        "keywords": keyword,
        "datatype": "all",
        "output":   "json",
    }
    try:
        resp   = requests.get(url, params=params, timeout=8)
        result = resp.json()
    except Exception as e:
        return jsonify({"tips": [], "error": str(e)})

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

    return jsonify({"tips": tips[:6]})


if __name__ == "__main__":
    print("=" * 60)
    print("  智能中间点推荐系统 v2（多 Agent 架构）")
    print("=" * 60)
    print(f"  DeepSeek API Key: {'已配置' if DEEPSEEK_API_KEY else '未配置 ⚠️'}")
    print(f"  高德地图 API Key:  {'已配置' if AMAP_KEY else '未配置 ⚠️'}")
    print(f"  Session 缓存: 内存（TTL {SESSION_TTL // 3600} 小时）")
    print("=" * 60)
    print("  访问地址: http://localhost:5000")
    print("=" * 60)
    # threaded=True：每个请求在独立线程中处理，允许多用户同时访问/打开多个网页
    # 不加此参数（或设为 False）时，Flask 单线程串行处理，一个请求卡住会阻塞所有人
    app.run(debug=True, host="127.0.0.1", port=5000, threaded=True)
