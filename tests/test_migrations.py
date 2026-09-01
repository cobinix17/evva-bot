#!/usr/bin/env python3
"""Проверки миграций на СТАРОЙ схеме базы.

    DATABASE_URL=$(sh tests/pg_start.sh) python3 tests/test_migrations.py

Зачем отдельно от test_money.py: там база создаётся текущим init_db, то есть
уже правильной. А ломаются миграции ровно на том, чего в новой базе нет —
на боевой таблице, созданной год назад. Здесь схема воссоздаётся старой
руками, и init_db гоняется по ней, как при деплое.

Скрипт создаёт и удаляет отдельную базу `legacy` рядом с указанной.
"""
import asyncio
import os
import re
import sys
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import asyncpg  # noqa: E402
import db       # noqa: E402

FAILURES: list[str] = []
PASSED = 0


def check(name: str, got, expected) -> None:
    global PASSED
    if got == expected:
        PASSED += 1
    else:
        FAILURES.append(f"{name}: получили {got!r}, ждали {expected!r}")


# Схема ДО сегодняшних миграций: у generated_readings ключ без даты,
# у coupon_uses нет колонки id, даты рождения записаны без ведущих нулей.
LEGACY_SCHEMA = """
CREATE TABLE users (
    user_id BIGINT PRIMARY KEY, first_name TEXT, free_used BOOLEAN DEFAULT FALSE,
    subscribed_channel BOOLEAN DEFAULT FALSE, birth_date TEXT, destiny_number INTEGER,
    purchased TEXT DEFAULT '[]', waiting TEXT, review_left BOOLEAN DEFAULT FALSE,
    notifications BOOLEAN DEFAULT TRUE, reviews_left TEXT DEFAULT '[]',
    ref_balance INTEGER DEFAULT 0, referred_by BIGINT);
CREATE TABLE generated_readings (
    user_id BIGINT, razbor_key TEXT, title TEXT, text TEXT,
    updated_at TIMESTAMP DEFAULT NOW(), PRIMARY KEY (user_id, razbor_key));
CREATE TABLE coupons (code TEXT PRIMARY KEY, expires_at TIMESTAMP,
    max_uses INTEGER DEFAULT 1, uses_count INTEGER DEFAULT 0, used_by BIGINT);
CREATE TABLE coupon_uses (code TEXT, user_id BIGINT, used_at TIMESTAMP DEFAULT NOW());
"""


async def build_legacy(base_url: str) -> str:
    legacy_url = re.sub(r"/(\w+)\?", "/legacy?", base_url, count=1)
    conn = await asyncpg.connect(base_url)
    await conn.execute("DROP DATABASE IF EXISTS legacy")
    await conn.execute("CREATE DATABASE legacy")
    await conn.close()

    conn = await asyncpg.connect(legacy_url)
    await conn.execute(LEGACY_SCHEMA)
    await conn.execute(
        "INSERT INTO users (user_id, birth_date, destiny_number, first_name) VALUES "
        "(1,'1.3.1995',NULL,'Аня'), (2,'29.11.1989',2,'Борис'),"
        "(3,'5.7.2000',NULL,'Вика'), (4,NULL,NULL,'Гена')")
    await conn.execute(
        "INSERT INTO generated_readings (user_id,razbor_key,title,text) "
        "VALUES (1,'matrix_full','Матрица','текст')")
    # один человек активировал код трижды — так было до появления режимов
    await conn.execute(
        "INSERT INTO coupon_uses (code,user_id) VALUES ('X',1),('X',1),('X',1),('Y',2)")
    await conn.close()
    return legacy_url


async def main() -> int:
    base = os.environ.get("DATABASE_URL")
    if not base:
        print("Нужен DATABASE_URL. Как поднять локальную базу — tests/README.md")
        return 2
    if "railway" in base or "amvera" in base:
        print("Похоже на боевую базу — скрипт создаёт и удаляет базы. Отказываюсь.")
        return 2

    legacy = await build_legacy(base)
    await db.init_db(legacy)

    rows = {r["user_id"]: dict(r) for r in await db.db_pool.fetch(
        "SELECT user_id, birth_date, destiny_number FROM users")}
    check("дата рождения дополнена нулями", rows[1]["birth_date"], "01.03.1995")
    check("однозначный месяц тоже", rows[3]["birth_date"], "05.07.2000")
    check("корректная дата не тронута", rows[2]["birth_date"], "29.11.1989")
    check("пустая дата не сломала миграцию", rows[4]["birth_date"], None)
    check("число судьбы пересчитано", rows[2]["destiny_number"], 4)

    check("ключ generated_readings расширен датой", await db.db_pool.fetchval(
        "SELECT string_agg(a.attname,',' ORDER BY a.attnum) FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
        "WHERE i.indrelid='generated_readings'::regclass AND i.indisprimary"),
        "user_id,razbor_key,date_str")

    # Дедупликация обязана работать без колонки id — на боевой таблице её нет.
    check("активной активации купона осталась одна", await db.db_pool.fetchval(
        "SELECT count(*) FROM coupon_uses WHERE code='X' AND user_id=1 AND once"), 1)
    check("история активаций сохранена", await db.db_pool.fetchval(
        "SELECT count(*) FROM coupon_uses WHERE code='X' AND user_id=1"), 3)
    check("уникальный индекс построен", await db.db_pool.fetchval(
        "SELECT 1 FROM pg_indexes WHERE indexname='coupon_uses_once_uniq'"), 1)

    # Индекс — единственное, что держит гонку: явная проверка в use_coupon
    # читает и пишет двумя запросами, и без индекса параллельные активации
    # проскакивают обе. Раз индекс есть — гонка должна быть закрыта.
    await db.db_pool.execute(
        "INSERT INTO coupons (code, expires_at, max_uses) VALUES ('RACE', $1, 20)",
        db.utc_now() + timedelta(hours=48))
    r = await asyncio.gather(*[db.use_coupon("RACE", 55) for _ in range(10)])
    check("гонка активаций на старой базе", r.count("ok"), 1)

    await db.init_db(legacy)   # второй деплой подряд
    check("повторный запуск не наплодил дублей", await db.db_pool.fetchval(
        "SELECT count(*) FROM coupon_uses WHERE code='X' AND user_id=1 AND once"), 1)
    check("повторный запуск не испортил дату", await db.db_pool.fetchval(
        "SELECT birth_date FROM users WHERE user_id=1"), "01.03.1995")

    total = PASSED + len(FAILURES)
    print(f"пройдено {PASSED} из {total}")
    for f in FAILURES:
        print("  ✗", f)
    print("ЧИСТО" if not FAILURES else "ЕСТЬ ПРОБЛЕМЫ")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
