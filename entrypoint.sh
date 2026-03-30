#!/bin/bash
export MONERO_DAEMON_HOST="${MONERO_DAEMON_HOST:-node.moneroworld.com}"
export MONERO_DAEMON_PORT="${MONERO_DAEMON_PORT:-18089}"
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
