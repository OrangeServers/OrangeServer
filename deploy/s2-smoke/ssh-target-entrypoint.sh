#!/bin/sh
set -eu

test -s /run/secrets/s2-client-key.pub
mkdir -p /run/sshd

# Both fixtures live on disposable tmpfs mounts.  The marker makes the
# smoke-only uptime wrapper block, while the symlink points outside the
# approved /opt root without touching secrets.
rm -f /opt/s2-smoke/bounded-link
touch /opt/s2-smoke/slow-uptime
printf '%s\n' 's2-symlink-outside-marker' > /tmp/s2-symlink-outside.txt
ln -s /tmp/s2-symlink-outside.txt /opt/s2-smoke/bounded-link
chown -h 10001:10001 \
    /opt/s2-smoke/bounded-link \
    /opt/s2-smoke/slow-uptime \
    /tmp/s2-symlink-outside.txt

exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config
