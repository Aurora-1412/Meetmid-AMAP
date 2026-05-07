#!/bin/bash
# 智能中间点餐厅推荐系统 - 启动脚本

cd "$(dirname "$0")"

# 检查虚拟环境
if [ ! -d "venv" ]; then
  echo "正在创建虚拟环境（Python 3.10）..."
  uv venv --python 3.10 venv
  source venv/bin/activate
  echo "正在安装依赖..."
  uv pip install -r requirements.txt
else
  source venv/bin/activate
fi

# 检查 .env
if ! grep -q "AMAP_KEY" .env 2>/dev/null || grep -q "AMAP_KEY=$" .env 2>/dev/null; then
  echo ""
  echo "⚠️  警告：未检测到 AMAP_KEY"
  echo "   请在 .env 文件中添加："
  echo "   AMAP_KEY=你的高德地图API Key"
  echo ""
fi

echo ""
echo "启动服务中（多Agent v2 版本）..."
python app_v2.py
