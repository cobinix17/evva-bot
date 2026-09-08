"""Список близких: лимит, дедупликация, изоляция между владельцами.

Нужен PostgreSQL — как поднять, см. tests/README.md:
    DATABASE_URL=$(sh tests/pg_start.sh) python3 tests/test_people.py
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DSN = os.getenv("DATABASE_URL", "")
if not DSN:
    print("нужен DATABASE_URL — см. tests/README.md")
    sys.exit(1)
# Та же защита, что в test_money.py: тесты чистят таблицы, боевой базе это смерть.
if any(host in DSN for host in ("railway", "amvera")):
    print("боевой DSN — тест не запускается")
    sys.exit(1)

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("GROQ_API_KEY", "x")

import db  # noqa: E402

ok = 0
fail = 0

def check(cond, name):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print(f"  ПРОВАЛ: {name}")

async def main():
    await db.init_db(DSN)
    await db.db_pool.execute("DELETE FROM people WHERE owner_id IN (901, 902)")

    # ─── лимит ───────────────────────────────────────────────────────────────
    for i in range(3):
        p = await db.add_person(901, f"Имя{i}", f"0{i+1}.01.2000", "child", limit=3)
        check(p is not None, f"добавление {i+1}-го в пределах лимита")
    over = await db.add_person(901, "Лишний", "09.09.1999", "friend", limit=3)
    check(over is None, "четвёртый сверх лимита не добавляется")
    check(await db.count_people(901) == 3, "в списке ровно 3 человека")

    # Премиум (limit=None) лимитом не ограничен.
    p = await db.add_person(901, "Четвёртый", "10.10.1990", None, limit=None)
    check(p is not None, "без лимита добавляется сверх трёх")
    check(await db.count_people(901) == 4, "теперь 4 человека")

    # ─── дедупликация ────────────────────────────────────────────────────────
    again = await db.add_person(901, "Имя0", "01.01.2000", "child", limit=None)
    check(again is not None, "повторное добавление не ошибка")
    check(await db.count_people(901) == 4, "повтор не создал дубль")

    # Регистр имени не делает человека новым.
    case = await db.add_person(901, "имя0", "01.01.2000", "child", limit=None)
    check(case is not None, "другой регистр — тот же человек")
    check(await db.count_people(901) == 4, "регистр не создал дубль")

    # Повтор при полном списке — не отказ: человек уже там, лимит не тратится.
    await db.db_pool.execute("DELETE FROM people WHERE owner_id = 902")
    await db.add_person(902, "Аня", "03.09.1994", "partner", limit=1)
    dup_full = await db.add_person(902, "Аня", "03.09.1994", "partner", limit=1)
    check(dup_full is not None, "повтор при полном списке возвращает человека")
    check(await db.count_people(902) == 1, "повтор при полном списке не задвоил")

    # ─── гонка: лимит держится при параллельных вставках ─────────────────────
    await db.db_pool.execute("DELETE FROM people WHERE owner_id = 902")
    results = await asyncio.gather(*[
        db.add_person(902, f"Гонка{i}", f"1{i}.02.1988", "friend", limit=3)
        for i in range(10)
    ])
    added = sum(1 for r in results if r is not None)
    total = await db.count_people(902)
    check(total <= 3, f"параллельные вставки не пробили лимит (в базе {total})")
    check(added == total, f"вернули столько же, сколько записали ({added} vs {total})")

    # ─── изоляция владельцев ─────────────────────────────────────────────────
    mine = await db.list_people(901)
    check(all(p["name"] != "Гонка0" for p in mine), "чужие люди не видны в списке")
    someone = (await db.list_people(902))[0]
    check(await db.get_person(901, someone["id"]) is None,
          "get_person не отдаёт чужого человека по id")
    check(await db.delete_person(901, someone["id"]) is False,
          "delete_person не удаляет чужого человека")
    check(await db.rename_person(901, someone["id"], "Взлом") is False,
          "rename_person не переименовывает чужого человека")

    # ─── удаление и переименование своих ─────────────────────────────────────
    own = (await db.list_people(901))[0]
    check(await db.rename_person(901, own["id"], "Новое") is True, "переименование своего")
    renamed = await db.get_person(901, own["id"])
    check(renamed["name"] == "Новое", "имя действительно изменилось")
    check(await db.delete_person(901, own["id"]) is True, "удаление своего")
    check(await db.get_person(901, own["id"]) is None, "удалённого больше нет")

    # ─── смена роли ──────────────────────────────────────────────────────────
    who = await db.add_person(901, "Сестра", "05.05.2010", "other", limit=None)
    check(db.person_label(who).startswith("👤"), "сохранено как «другое»")
    check(await db.set_person_relation(901, who["id"], "sibling") is True, "роль меняется")
    after = await db.get_person(901, who["id"])
    check(db.person_label(after).startswith("👫"), "значок роли обновился")
    check(await db.set_person_relation(902, who["id"], "child") is False,
          "чужому человеку роль не поменять")

    # ─── подпись для кнопки ──────────────────────────────────────────────────
    check(db.person_label({"name": "Соня", "birth_date": "12.05.2015", "relation": "child"})
          == "👧 Соня · 12.05.2015", "подпись с известной ролью")
    check(db.person_label({"name": "Х", "birth_date": "01.01.2000", "relation": None})
          .startswith("👤"), "роль не указана — нейтральный эмодзи")

    await db.db_pool.execute("DELETE FROM people WHERE owner_id IN (901, 902)")
    await db.db_pool.close()

asyncio.run(main())
print(f"пройдено {ok} из {ok + fail}")
print("ЧИСТО" if not fail else "ЕСТЬ ПРОВАЛЫ")
sys.exit(1 if fail else 0)
