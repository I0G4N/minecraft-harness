#!/bin/sh
set -eu
if [ "${ACCEPT_EULA:-false}" != "true" ]; then
    echo 'Set ACCEPT_EULA=true only after accepting https://aka.ms/MinecraftEULA' >&2
    exit 64
fi
if [ ! -f /data/server.properties ]; then
    cp /opt/minecraft/server.properties /data/server.properties
    case "${MC_SEED:-42}" in
        ''|*[!0-9-]*) echo 'MC_SEED must be a signed integer' >&2; exit 64 ;;
    esac
    printf '\nlevel-seed=%s\n' "${MC_SEED:-42}" >> /data/server.properties
    if [ -n "${MAX_PLAYERS:-}" ]; then
        case "$MAX_PLAYERS" in
            ''|*[!0-9]*) echo 'MAX_PLAYERS must be a positive integer' >&2; exit 64 ;;
        esac
        [ "$MAX_PLAYERS" -ge 1 ] || { echo 'MAX_PLAYERS must be a positive integer' >&2; exit 64; }
        if grep -q '^max-players=' /data/server.properties; then
            sed -i "s/^max-players=.*/max-players=$MAX_PLAYERS/" /data/server.properties
        else
            printf 'max-players=%s\n' "$MAX_PLAYERS" >> /data/server.properties
        fi
    fi
fi
if [ ! -f /data/spigot.yml ] && [ -f /opt/minecraft/spigot.yml ]; then
    cp /opt/minecraft/spigot.yml /data/spigot.yml
fi
printf 'eula=true\n' > /data/eula.txt
control=${HARNESS_CONTROL_DIR:-/tmp/harness-control}
mkdir -p "$control"
chmod 0700 "$control"
rm -f "$control/console"
mkfifo -m 0600 "$control/console"
# The read/write descriptor prevents FIFO open from blocking before Java starts.
# Only this server container contains the FIFO; players never receive its mount.
exec 3<> "$control/console"
printf '%s\n' "$$" > "$control/java.pid"
touch "$control/started"
exec java -Xms${JAVA_XMS:-512M} -Xmx${JAVA_XMX:-1536M} -jar /opt/minecraft/server.jar nogui < "$control/console"
