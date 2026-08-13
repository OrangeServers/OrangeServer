#!/bin/sh
set -eu

# Smoke-only deterministic long-running implementation for the existing
# production `system.load` probe.  No product whitelist or reader semantics
# are weakened to create the cancellation fixture.
if [ -e /opt/s2-smoke/slow-uptime ]; then
    exec sleep 300
fi
exec /usr/bin/uptime "$@"
