#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "[1/4] 拉取代码"
git pull --ff-only

echo "[2/4] 当前提交"
git log -1 --oneline

echo "[3/4] 重启 dwell-backend"
if pm2 describe dwell-backend >/dev/null 2>&1; then
  pm2 restart dwell-backend
else
  cd "$REPO_DIR/backend"
  pm2 start run.py --interpreter python3 --name dwell-backend
fi
pm2 save >/dev/null

echo "[4/4] 验证版本与前端来源"
sleep 1
curl -fsS http://127.0.0.1:8888/api/version
printf '\n更新完成\n'
