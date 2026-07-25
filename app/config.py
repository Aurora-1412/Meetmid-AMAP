"""
配置加载模块
优先从 ./ai.properties 读取，缺失时回退到环境变量（仅本地开发用）。
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _read_props(path: str) -> dict:
    """解析 Java Properties 格式文件（按 = 分割，不用 configparser）"""
    out: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


# ── AI 配置 ──────────────────────────────────────────────
_ai_props = _read_props("./ai.properties")

AI_BASE_URL: str = (
    _ai_props.get("ai.base_url")
    or os.getenv("AI_BASE_URL", "")
)
AI_API_KEY: str = (
    _ai_props.get("ai.api_key")
    or os.getenv("AI_API_KEY", "")
)


def is_ai_enabled() -> bool:
    return bool(AI_BASE_URL and AI_API_KEY)


# ── 高德地图配置（仍从环境变量读取）──────────────────────────
AMAP_KEY: str    = os.getenv("AMAP_KEY", "")
AMAP_JS_KEY: str = os.getenv("AMAP_JS_KEY", AMAP_KEY)
