#!/bin/sh
set -eu

bus_socket=${DBUS_SESSION_BUS_ADDRESS#unix:path=}
rm -f "$bus_socket"
dbus-daemon \
    --session \
    --fork \
    --nopidfile \
    --address="$DBUS_SESSION_BUS_ADDRESS"

# 전용 Docker 볼륨의 로그인 키링을 빈 로컬 암호로 잠금 해제한다.
# 볼륨은 agy OAuth 토큰만 저장하며 호스트 사용자 키링과 공유하지 않는다.
printf '\n' | gnome-keyring-daemon --unlock --components=secrets >/dev/null

exec "$@"
