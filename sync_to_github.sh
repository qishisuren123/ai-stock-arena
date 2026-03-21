#!/bin/bash
# 自动导出数据并推送到 GitHub
# 被 auto_trader_multi.py 调用，也可手动执行

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

LOG="$SCRIPT_DIR/sync.log"
exec >> "$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') 开始同步 ==="

# 激活 conda（非交互 shell 需要 source）
if [ -f /root/miniconda3/etc/profile.d/conda.sh ]; then
    source /root/miniconda3/etc/profile.d/conda.sh
    conda activate stock 2>/dev/null || true
fi

# 设置代理（访问 GitHub 需要）
export http_proxy=http://httpproxy-headless.kubebrain.svc.pjlab.local:3128
export https_proxy=http://httpproxy-headless.kubebrain.svc.pjlab.local:3128
export no_proxy="10.0.0.0/8,100.96.0.0/12,.pjlab.org.cn"

# 1. 导出数据
python "$SCRIPT_DIR/export_data.py"

# 2. Git 提交并推送
cd "$SCRIPT_DIR"
git add docs/data/latest.json docs/data/history.json
if git diff --cached --quiet; then
    echo "无数据变更，跳过推送"
else
    git commit -m "data: update $(date '+%m-%d %H:%M')"
    git push origin main
    echo "推送成功"
fi

echo "=== 同步完成 ==="
