# numerology.py — нумерологические расчёты и валидация даты.
# Чистые функции без побочных эффектов, не зависит от bot.py/config.py/ai.py/pdf.py.
from datetime import datetime, date

def is_valid_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
        return True
    except ValueError:
        return False

def calculate_destiny(date_str: str) -> int:
    digits = [int(d) for d in date_str if d.isdigit()]
    total  = sum(digits)
    while total > 9 and total not in (11, 22, 33):
        total = sum(int(d) for d in str(total))
    return total

def calculate_personal_year(date_str: str) -> int:
    parts = date_str.split(".")
    day, month = int(parts[0]), int(parts[1])
    current_year = datetime.now().year
    total = (sum(int(d) for d in str(day)) +
             sum(int(d) for d in str(month)) +
             sum(int(d) for d in str(current_year)))
    while total > 9 and total not in (11, 22, 33):
        total = sum(int(d) for d in str(total))
    return total

def calculate_karmic_numbers(date_str: str) -> list:
    digits_present = set(int(d) for d in date_str if d.isdigit() and d != '0')
    return sorted(set(range(1, 10)) - digits_present)

def calculate_matrix(date_str: str) -> dict:
    parts   = date_str.split(".")
    day     = int(parts[0])
    month   = int(parts[1])
    destiny = calculate_destiny(date_str)

    def reduce(n):
        while n > 22:
            n = sum(int(d) for d in str(n))
        return n

    a = day
    b = month
    c = sum(int(d) for d in str(int(parts[2])))
    while c > 22:
        c = sum(int(d) for d in str(c))
    d = reduce(a + b + c)
    e = reduce(a + b + c + d)
    return {"день": a, "месяц": b, "год": c,
            "первое_число": d, "второе_число": e, "число_судьбы": destiny}

def calculate_name_number(name: str) -> int:
    ru_alphabet = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    total = 0
    for ch in name.lower():
        if ch in ru_alphabet:
            total += ru_alphabet.index(ch) + 1
    while total > 9 and total not in (11, 22, 33):
        total = sum(int(d) for d in str(total))
    return total if total > 0 else 0

def calculate_day_number(today: date) -> int:
    total = sum(int(d) for d in str(today.day) + str(today.month) + str(today.year))
    while total > 9 and total not in (11, 22, 33):
        total = sum(int(d) for d in str(total))
    return total

def build_numerology_context(name: str, date_str: str) -> str:
    destiny     = calculate_destiny(date_str)
    personal_yr = calculate_personal_year(date_str)
    karmic      = calculate_karmic_numbers(date_str)
    matrix      = calculate_matrix(date_str)
    name_number = calculate_name_number(name)
    karmic_str  = ", ".join(map(str, karmic)) if karmic else "отсутствуют"
    return (
        f"Пол: женский. Всегда обращайся в женском роде.\n"
        f"Имя: {name}\n"
        f"Дата рождения: {date_str}\n"
        f"Число судьбы: {destiny}\n"
        f"Число имени: {name_number}\n"
        f"Личный год ({datetime.now().year}): {personal_yr}\n"
        f"Кармические числа (отсутствующие): {karmic_str}\n"
        f"Матрица судьбы — день: {matrix['день']}, месяц: {matrix['месяц']}, "
        f"год: {matrix['год']}, первое число: {matrix['первое_число']}, "
        f"второе число: {matrix['второе_число']}\n"
    )
