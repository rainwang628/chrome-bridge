#!/usr/bin/env bash
# 启动（或重启）桥接服务，输出状态。
set -euo pipefail
cd "$(dirname "$0")"
PORT="${BRIDGE_PORT:-8777}"
LOG="$PWD/bridge.log"

# 已有实例就先停掉，避免端口占用
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  pkill -f "chrome-bridge/server.py" 2>/dev/null || true
  sleep 0.6
fi

nohup python3 server.py >>"$LOG" 2>&1 &
for _ in $(seq 1 20); do
  sleep 0.2
  if curl -sf --max-time 1 "http://127.0.0.1:${PORT}/status" >/dev/null 2>&1; then break; fi
done

echo "服务：http://127.0.0.1:${PORT}"
curl -s "http://127.0.0.1:${PORT}/status"; echo
python3 ctl.py status
