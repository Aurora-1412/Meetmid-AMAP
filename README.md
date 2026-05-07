# 智能中间点地点推荐

> 输入两个出发地，描述你们的需求，AI 自动找到中间位置的最优地点，并分别规划双方交通路线。

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![Flask](https://img.shields.io/badge/Flask-3.0-green) ![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek-purple) ![高德地图](https://img.shields.io/badge/地图-高德-red)

---

## 功能特性

- **自然语言理解**：直接输入"找一家安静的咖啡馆"、"公平的位置，两边都不要太远"等口语需求
- **智能中间点计算**：自动计算两点的地理中心，动态调整搜索半径
- **A/B 独立路线规划**：双方可选择不同出行方式（公交地铁 / 驾车 / 骑行 / 步行 / 最快）
- **智能排序**：从 query 中提取排序偏好，自动调整评分、总时间、公平性的权重
- **附近搜索**：对每个结果地点一键搜索周边咖啡馆、书店、地铁站等配套设施
- **多结果展示**：最多20个候选地点，地图标记默认半透明，点击卡片后高亮选中
- **路线重算**：切换出行方式后无需重新搜索，直接对缓存数据重算路线
- **可拖拽布局**：左右面板分界线可拖动调节宽度

---

## 技术架构

### 多 Agent 流水线（`app_v2.py`）

```
用户输入
   │
   ▼
Agent1 规划（LLM，无工具）
   提取：keyword / min_rating / sort_weights
   │
   ▼
Agent2 搜索（LLM + 2个工具）
   工具：find_midpoint / search_pois_nearby
   LLM 只看压缩摘要，完整数据存 Python 侧
   │
   ▼
Python 筛选排名
   评分过滤 → 取前20个
   │
   ▼
Python 路线计算
   A/B 分别调高德路线 API
   综合评分 = 评分×w₁ - 总时间×w₂ - 时间差×w₃
   权重由 Agent1 从 query 中提取
   │
   ▼
Agent3 总结（LLM，无工具）
   生成 2-4 句推荐文字
   │
   ▼
返回结果 + Session ID
```

### 为什么拆分 Agent？

| 问题 | 解决方案 |
|---|---|
| 单 Agent 上下文过长（POI 原始数据很大） | Agent2 工具返回给 LLM 的是压缩摘要，完整数据存 Python 侧 |
| 路线计算不需要 LLM | 纯 Python 直接调高德 API，速度快、结果确定 |
| A/B 切换出行方式需要重算 | Session 缓存 POI 列表，重算只跑路线，不重新搜索 |

---

## 快速开始

### 1. 准备 API Key

需要两个 Key，在项目根目录创建 `.env` 文件：

```
DEEPSEEK_API_KEY=你的DeepSeek API Key
AMAP_KEY=你的高德Web服务Key
AMAP_JS_KEY=你的高德JS API Key
```

**获取方式：**
- DeepSeek：[platform.deepseek.com](https://platform.deepseek.com)
- 高德地图：[console.amap.com](https://console.amap.com)
  - `AMAP_KEY`：创建「Web 服务」类型应用
  - `AMAP_JS_KEY`：创建「Web 端（JS API）」类型应用，需在控制台配置允许的域名白名单（加入 `localhost` 和 `127.0.0.1`）

### 2. 启动服务

```bash
bash start.sh
```

脚本会自动：
1. 创建 Python 3.10 虚拟环境（`uv` 管理）
2. 安装依赖
3. 启动 `app_v2.py`（多 Agent 版本）

访问 [http://localhost:5000](http://localhost:5000)

### 3. 手动启动（可选）

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app_v2.py
```

---

## 使用说明

### 选择出发地（A/B）

- **搜索框输入**：输入地址后从下拉候选中选择（走后端代理，结果稳定）
- **点击地图**：点击左上角「选地点A/B」按钮后在地图上直接点选

### 填写需求

支持自然语言，例如：
- `找一家评分高的火锅店`
- `安静的咖啡馆，最好两边路上时间差不多`
- `附近有公园的中餐厅，不要太远`
- `找个 KTV，价格实惠`

### 出行方式

A 和 B 可以分别选择不同的出行方式：
- ⚡ 最快（自动比较所有方式，取最短）
- 🚇 公交地铁
- 🚲 骑行
- 🚗 驾车
- 🚶 步行

### 查看结果

- 点击卡片：地图标记高亮，地图平移到该地点
- 点击地图标记：弹出信息窗口，显示双方路线详情
- 「🔍 搜索此地点附近」：弹出附近搜索面板，可搜咖啡馆、书店、地铁站等

### 切换出行方式重算

结果出来后，可在底部「切换出行方式重算路线」面板调整 A/B 的出行方式，点击「重新计算路线」后**无需重新搜索**，直接对已有地点重算路线并重新排序。

---

## 项目结构

```
.
├── app.py            # 原版单 Agent（保留备用，不建议直接使用）
├── app_v2.py         # 多 Agent 版本（当前主版本）
├── requirements.txt
├── start.sh          # 一键启动脚本
├── .env              # API Key 配置（不提交 git）
└── static/
    └── index.html    # 单页前端（纯 HTML/CSS/JS，无构建工具）
```

---

## 后端 API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v2/search` | 完整流水线搜索，返回 `session_id` |
| `POST` | `/api/v2/routes` | 路线重算（基于 session_id，不重新搜索） |
| `GET`  | `/api/v2/session/<id>` | 查看 session 详情（调试用） |
| `POST` | `/api/geocode` | 地址→坐标（正地理编码） |
| `POST` | `/api/geocode-suggest` | 地点搜索下拉候选（基于高德 inputtips） |
| `POST` | `/api/nearby-search` | 查询某坐标附近的 POI |
| `GET`  | `/api/config` | 获取前端所需配置（含 JS API Key） |

### `/api/v2/search` 请求示例

```json
{
  "location_a": { "lng": 116.397, "lat": 39.908, "name": "天安门" },
  "location_b": { "lng": 116.469, "lat": 39.995, "name": "望京" },
  "query": "找一家评分高的火锅店，两边路上时间要差不多",
  "city": "北京",
  "prefer_a": "transit",
  "prefer_b": "auto",
  "departure_time": "09:30"
}
```

### `/api/v2/routes` 请求示例

```json
{
  "session_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "prefer_a": "cycling",
  "prefer_b": "driving",
  "departure_time": null
}
```

---

## 常见问题

**Q：地点搜索下拉没有结果？**
后端服务是否正常运行，检查 `AMAP_KEY` 是否配置正确。

**Q：地图不显示？**
检查 `AMAP_JS_KEY` 是否配置，以及高德控制台是否将 `localhost` 加入域名白名单。

**Q：搜索结果路线显示"计算中..."？**
高德路线 API 限制（免费版 QPS 较低），结果较多时部分路线可能超时。可以减少显示数量（下拉选 5 个或 10 个）后重新搜索。

**Q：公交路线摘要看起来不对？**
高德公交 API 使用固定时间（工作日中午 12:00）规划路线，避免末班车影响。可在「出发时间」处手动指定时间。

---

## 依赖版本

```
flask==3.0.3
flask-cors==4.0.1
requests==2.31.0
openai>=2.0.0
httpx>=0.27.0
python-dotenv==1.0.1
```

Python 版本：**3.10+**（使用了 `str | None` 类型注解语法）
