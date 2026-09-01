#!/usr/bin/env python3
"""Постобработка ответа модели. Ни базы, ни сети — но нужен httpx, потому что
ai.py тянет его на уровне модуля (в проде он всё равно стоит):

    python3 tests/test_ai_text.py

Это самое дорогое место в проекте по последствиям ошибки. Текст здесь уже
оплачен: если проверка ошибочно сочтёт готовый ответ браком, бот пойдёт
переспрашивать провайдеров по кругу — до шести запросов к ИИ вместо одного,
и всё равно отдаст тот же текст. Если наоборот пропустит брак — человек
получит за деньги обрывок.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ai  # noqa: E402

FAILURES: list[str] = []
PASSED = 0


def check(name: str, got, expected) -> None:
    global PASSED
    if got == expected:
        PASSED += 1
    else:
        FAILURES.append(f"{name}: получили {got!r}, ждали {expected!r}")


def test_ends_properly() -> None:
    """Ева постоянно заканчивает мысль эмодзи — «Всё получится 🌸». Раньше такой
    ответ считался оборванным на полуслове."""
    for text, expected in (
        ("Всё получится 🌸", True),
        ("Ты справишься ✨", True),
        ("Это твой год 💫", True),
        ("Конец ⭐", True),
        ("Сердце ❤️", True),
        ("Всё хорошо.", True),
        ("Точка с многоточием…", True),
        ("Вопрос?", True),
        ("«Цитата в кавычках»", True),
        ("Оборвалось и теперь ты", False),
        ("Слово без знака", False),
        ("", False),
    ):
        check(f"ends_properly({text!r})", ai.ends_properly(text), expected)


def test_roman_numerals() -> None:
    """Номер аркана римскими — это латинские буквы. Чистка иностранных символов
    их съедала, и в тексте оставалось «Твой Аркан года — . Сила»."""
    out = ai._clean_text("Твой Аркан года — VIII, Сила. Это про опору.")
    check("римские цифры выжили", "VIII" in out, True)
    out2 = ai._clean_text("Ты сильный человек here is your reading готов идти дальше.")
    check("английский хвост всё ещё вырезается", "here is your reading" in out2, False)


def test_dangling_negatives() -> None:
    """«Рядом некому было.» без инфинитива — безграмотно. Но «некому было
    помочь» правильно, и трогать его нельзя."""
    check("оборванное отрицание починено",
          "никого не было" in ai._fix_dangling_negatives("Казалось, что рядом некому было."), True)
    check("корректная конструкция не тронута",
          ai._fix_dangling_negatives("Рядом некому было помочь."), "Рядом некому было помочь.")


def test_structure_complete() -> None:
    """Модель умеет ответить одним последним блоком из восьми. Такой ответ
    нельзя отдавать за деньги."""
    prompt = ("🔮 Первый блок — про это\n💰 Второй блок — про то\n"
              "💕 Третий блок\n🌟 Четвёртый блок")
    full = ("🔮 Первый блок\nтекст.\n\n💰 Второй блок\nтекст.\n\n"
            "💕 Третий блок\nтекст.\n\n🌟 Четвёртый блок\nтекст.")
    part = "🌟 Четвёртый блок\nтолько последний блок и всё."
    check("полный ответ принят", ai.is_structure_complete(prompt, full), True)
    check("огрызок отклонён", ai.is_structure_complete(prompt, part), False)


def test_strip_instruction_tails() -> None:
    """В списке блоков после названия идёт пояснение для модели. Она его
    переписывает в заголовок, и клиент читает «назови его прямо и конкретно».
    Резать надо строго по строке того же блока: в промптах есть примеры вроде
    «Твой Аркан — Колесо Фортуны», и по ним легко отрезать живой текст."""
    prompt = "🔮 Главный страх — назови его прямо и конкретно\n💡 Что делать"
    answer = ("🔮 Главный страх — назови его прямо и конкретно\n"
              "Ты боишься остаться один.\n\n💡 Что делать\nНачни с малого.")
    out = ai.strip_instruction_tails(prompt, answer)
    check("служебный хвост убран", "назови его прямо" in out, False)
    check("заголовок остался", "🔮 Главный страх" in out, True)
    check("текст блока цел", "Ты боишься остаться один." in out, True)

    prompt2 = "🔲 Твой Аркан — например: Колесо Фортуны\n💡 Совет"
    answer2 = "🔲 Твой Аркан — Колесо Фортуны\nЭто про перемены.\n\n💡 Совет\nЖди."
    out2 = ai.strip_instruction_tails(prompt2, answer2)
    check("осмысленный заголовок не срезан", "Колесо Фортуны" in out2, True)


def main() -> int:
    test_ends_properly()
    test_roman_numerals()
    test_dangling_negatives()
    test_structure_complete()
    test_strip_instruction_tails()

    total = PASSED + len(FAILURES)
    print(f"пройдено {PASSED} из {total}")
    for f in FAILURES:
        print("  ✗", f)
    print("ЧИСТО" if not FAILURES else "ЕСТЬ ПРОБЛЕМЫ")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
