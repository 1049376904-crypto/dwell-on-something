#!/usr/bin/env bash
set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "[1/5] 检查 GitHub 连通性"
if ! curl -sS -o /dev/null --max-time 15 https://github.com; then
  echo "GitHub 此刻不可达，本次不更新代码。网络恢复后重跑本脚本。"
  exit 1
fi

echo "[2/5] 拉取代码"
pulled=0
for attempt in 1 2 3; do
  if timeout 120 git pull --ff-only; then
    pulled=1
    break
  fi
  echo "  第 ${attempt} 次失败，10 秒后重试"
  sleep 10
done

if [ "$pulled" -ne 1 ]; then
  echo "git pull 多次失败，本次不重启服务。"
  exit 1
fi

echo "[3/5] 当前提交"
git log -1 --oneline

echo "[4/5] 重启 dwell-backend"
if pm2 describe dwell-backend >/dev/null 2>&1; then
  pm2 restart dwell-backend --update-env
else
  pm2 start "$REPO_DIR/backend/run.py" --interpreter python3 --name dwell-backend --cwd "$REPO_DIR/backend"
fi
pm2 save >/dev/null

echo "[5/5] 验证版本与数据库"
sleep 2
curl -fsS --max-time 10 http://127.0.0.1:8888/api/version || true
printf '\n'
curl -fsS --max-time 10 http://127.0.0.1:8888/api/storage || true
printf '\n更新完成\n'
