#!/bin/bash
DAEMON_HOST="${MONERO_DAEMON_HOST:-node.moneroworld.com}"
DAEMON_PORT="${MONERO_DAEMON_PORT:-18089}"

monero-wallet-rpc \
  --daemon-host="$DAEMON_HOST" \
  --daemon-port="$DAEMON_PORT" \
  --rpc-bind-ip=127.0.0.1 \
  --rpc-bind-port=18083 \
  --wallet-dir=/wallet \
  --disable-rpc-login \
  --non-interactive \
  --trusted-daemon &

exec uvicorn main:app --host 0.0.0.0 --port 8000 --no-access-log
