#!/bin/sh
# Поднимает одноразовый PostgreSQL для tests/test_money.py и печатает
# DATABASE_URL к нему. База живёт в /tmp и умирает вместе с машиной —
# на боевые данные повлиять не может.
#
# Две неочевидности, на которых легко застрять:
#  • initdb отказывается работать от root — поэтому su nobody;
#  • слушаем только unix-сокет (listen_addresses пустой), чтобы не занимать
#    TCP-порт и не выставлять базу наружу.
set -e

PGDIR=/tmp/pgt
PORT=55432
BIN=$(ls -d /usr/lib/postgresql/*/bin | head -1)

if [ ! -d "$PGDIR/data" ]; then
    rm -rf "$PGDIR"
    mkdir -p "$PGDIR"
    chown nobody "$PGDIR"
    su nobody -s /bin/sh -c "$BIN/initdb -D $PGDIR/data -U postgres" >/dev/null
fi

if ! pg_isready -h "$PGDIR" -p "$PORT" >/dev/null 2>&1; then
    su nobody -s /bin/sh -c \
        "$BIN/pg_ctl -D $PGDIR/data -o '-p $PORT -k $PGDIR -c listen_addresses=' -l $PGDIR/log start" \
        >/dev/null
    sleep 2
fi

pg_isready -h "$PGDIR" -p "$PORT" >/dev/null
echo "postgresql://postgres@/postgres?host=$PGDIR&port=$PORT"
