#!/bin/bash
# setup.sh — Stock Analysis Skill 一键安装脚本

set -e

echo "=== Stock Analysis Skill 安装 ==="

# 1. Python 检查
PYTHON=$(command -v python3 || echo "")
if [ -z "$PYTHON" ]; then
    echo "❌ 需要 Python 3.11+"
    exit 1
fi
echo "✅ Python: \$($PYTHON --version 2>&1)"

# 2. 确定 Skill 目录
SKILL_DIR="\$(cd \"\$(dirname \"\$0\")\"; pwd)"
cd "\$SKILL_DIR"

# 3. 创建虚拟环境（如果不存在）
if [ ! -d "venv" ]; then
    echo "创建虚拟环境..."
    \$PYTHON -m venv venv
fi
source venv/bin/activate

# 4. 安装依赖
echo "安装依赖..."
pip install -q -r requirements.txt

# 5. 配置检查
if [ ! -f "config.yaml" ]; then
    echo "⚠️  未找到 config.yaml，将使用内置默认配置"
fi

echo ""
echo "=== 验证 ==="
\$PYTHON -c "
import sys
sys.path.insert(0, '.')
try:
    from stock_skill import StockAnalysisSkill
    skill = StockAnalysisSkill()
    print('✅ StockAnalysisSkill 加载成功（自包含引擎）')
    print('   LLM: 由 VeroRun UnifiedLLM 统一管理')
except Exception as e:
    print(f'❌ 加载失败: {e}')
    sys.exit(1)
"

echo ""
echo "=== 安装完成 ==="
echo "开始使用: python stock_skill.py --help"
