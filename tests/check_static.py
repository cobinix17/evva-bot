#!/usr/bin/env python3
"""Статические проверки целостности проекта. Ни базы, ни сети, ни ключей —
запускается где угодно за секунду:

    python3 tests/check_static.py

Каждая проверка здесь появилась из реального бага, который эти проверки бы
поймали. Добавляя новую — пиши в докстроке, что именно она ловит, иначе через
месяц никто не поймёт, можно ли её ослабить.
"""
import ast
import os
import re
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILURES: list[str] = []
PASSED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
    else:
        FAILURES.append(f"{name}{': ' + detail if detail else ''}")


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


# ── КАТАЛОГ РАЗБОРОВ ─────────────────────────────────────────────────────────
def check_catalog() -> None:
    """Разбор без промпта = купленный разбор, который невозможно сгенерировать.
    Промпт без цены = бесплатная выдача платного контента."""
    import config
    import readings

    titles = set(config.TITLES)
    paid = set(config.PAID_RAZBORY)
    prompts = set(readings.PROMPTS)

    check("платный разбор без промпта", not (paid - prompts), str(paid - prompts))
    check("промпт без разбора в каталоге", not (prompts - titles), str(prompts - titles))
    check("платный разбор не значится в TITLES", not (paid - titles), str(paid - titles))
    check("разбор без цены",
          not (paid - set(config.PRICES)), str(paid - set(config.PRICES)))
    check("в TITLES только free сверх платных",
          titles - paid == {"free"}, str(titles - paid))

    ups = getattr(config, "UPSELL", getattr(config, "UPSELLS", {})) or {}
    dead = {k: [v for v in vs if v not in titles] for k, vs in ups.items()
            if any(v not in titles for v in vs)}
    check("апселл ведёт на несуществующий разбор", not dead, str(dead))
    selfup = {k for k, vs in ups.items() if k in vs}
    check("апселл ведёт сам на себя", not selfup, str(selfup))


# ── ЭМОДЗИ-ЗАГОЛОВКИ ─────────────────────────────────────────────────────────
_HEADER_RE = re.compile(r'^([^\w\s"\'#][️]?)\s+([А-ЯЁ][а-яё].*)$')


def _header_emoji() -> list[str]:
    src = read("header_emoji.py")
    body = re.search(r"HEADER_EMOJI = \((.*?)\n\)", src, re.S).group(1)
    return [e.replace("️", "") for e in ast.literal_eval("(" + body + "\n)")]


def check_header_emoji() -> None:
    """Заголовок с эмодзи не из списка не считается блоком: не проходит
    проверку полноты структуры, не выделяется жирным и склеивается с
    предыдущим текстом в PDF. Так молча ломались семь разборов."""
    cov = _header_emoji()
    check("список эмодзи без дублей", len(cov) == len(set(cov)))

    for fname in ("readings.py", "bot.py", "broadcasts.py", "numerology.py"):
        unknown: dict[str, int] = {}
        for i, line in enumerate(read(fname).split("\n"), 1):
            m = _HEADER_RE.match(line.strip())
            if not m:
                continue
            s = line.strip().replace("️", "")
            if not any(s.startswith(e) for e in cov):
                unknown.setdefault(m.group(1), i)
        check(f"{fname}: заголовок с эмодзи вне header_emoji.py",
              not unknown, str(unknown))

    # Внутри одного промпта эмодзи не должны повторяться — иначе блоки
    # неразличимы и подсчёт структуры врёт.
    src = read("readings.py")
    blocks = re.split(r'\n\s{4}"([a-z_0-9]+)":', "\n" + src)
    dupes = {}
    for i in range(1, len(blocks), 2):
        key, body = blocks[i], blocks[i + 1]
        hs = _HEADER_RE.findall(body)
        seen, dup = set(), set()
        for emoji, _ in hs:
            (dup if emoji in seen else seen).add(emoji)
        if dup:
            dupes[key] = sorted(dup)
    check("эмодзи повторяется внутри одного промпта", not dupes, str(dupes))

    # Обе копии списка должны быть импортом, а не своим кортежем.
    for fname in ("ai.py", "pdf.py"):
        src = read(fname)
        check(f"{fname} берёт эмодзи из header_emoji.py",
              "from header_emoji import HEADER_EMOJI" in src)


# ── ОБРАЩЕНИЯ В ЖЕНСКОМ РОДЕ ─────────────────────────────────────────────────
# Ловим ЛЮБОЙ род в прошедшем времени при обращении на «ты»: замена женского
# на мужской — не решение, читателю с неизвестным полом не подходит ни то, ни
# другое. Нужна формулировка без рода вообще.
_GENDERED = re.compile(
    r"\b[Тт]ы (?:уже |давно |так |ещё )?[а-яё]{3,}(?:ла|лся|лась)\b"
    r"|\b[Тт]ы (?:уже |давно |так |ещё )?(?:пришёл|прошёл|зашёл|нашёл|смог|стал|был|сделал|создал|хотел)\b"
    r"|\bподруг[уаие]\b",
)
# Ева говорит о себе в женском роде — это её голос, а не обращение к читателю.
_EVA_VOICE = re.compile(r"Я (?:посмотрела|разложила|составила|собрала|взяла)")


def check_gender() -> None:
    """Пол читателя по умолчанию неизвестен. Женский род в общих текстах
    половине аудитории читается мимо, а в канале аудитория смешанная."""
    for fname in ("bot.py", "webapp.py", "keyboards.py", "broadcasts.py", "config.py"):
        hits = []
        for i, line in enumerate(read(fname).split("\n"), 1):
            s = line.strip()
            if s.startswith("#") or _EVA_VOICE.search(s):
                continue
            if "is_male" in s:          # осознанная развилка по полу — это ок
                continue
            if _GENDERED.search(s):
                hits.append(i)
        check(f"{fname}: обращение к читателю с указанием пола", not hits, str(hits[:5]))


# ── CALLBACK-КНОПКИ ──────────────────────────────────────────────────────────
def check_callbacks() -> None:
    """Если один префикс — начало другого, обработчики перехватывают чужие
    нажатия: кнопка молча делает не то, что написано."""
    src = read("bot.py")
    prefixes = re.findall(r'F\.data\.startswith\("([^"]+)"\)', src)
    exact = set(re.findall(r'F\.data == "([^"]+)"', src))

    collisions = [(a, b) for a in prefixes for b in prefixes if a != b and b.startswith(a)]
    check("префикс callback перекрывает другой префикс", not collisions, str(collisions))

    # Точное совпадение под чужим префиксом допустимо, только если префиксный
    # обработчик его явно исключает (F.data.in_({...}) в фильтре).
    shadowed = []
    for p in prefixes:
        for e in exact:
            if e.startswith(p) and f'in_({{"{e}"}}' not in src:
                shadowed.append((p, e))
    check("кнопка перехвачена префиксным обработчиком", not shadowed, str(shadowed))


# ── АДМИНКА ──────────────────────────────────────────────────────────────────
def check_admin_guards() -> None:
    """Админский обработчик без проверки ADMIN_ID — это скидки, купоны и
    рассылка в руках любого пользователя."""
    src = read("bot.py")
    parts = re.split(r"\n(?=@dp\.(?:message|callback_query)\()", src)
    names = ("admin", "discount", "revoke_premium", "broadcast", "review_moderation", "coupon_cmd")
    open_handlers = []
    for p in parts:
        m = re.search(r"async def (\w+)", p)
        if not m:
            continue
        if any(n in m.group(1) for n in names) and "ADMIN_ID" not in p:
            open_handlers.append(m.group(1))
    check("админский обработчик без проверки ADMIN_ID",
          not open_handlers, str(open_handlers))


# ── ВЕБ ──────────────────────────────────────────────────────────────────────
def check_web() -> None:
    """Эндпоинт без проверки initData отдаёт чужие данные по подставленному
    user_id. Публичными задуманы только каталог, матрица по дате и статика."""
    src = read("webapp.py")
    routes = re.findall(r'app\.router\.add_(?:get|post)\("([^"]+)", (\w+)\)', src)
    public = {"/api/catalog", "/api/matrix", "/webhook/yookassa", "/app", "/app/", "/app/static"}
    unguarded = []
    for path, fn in routes:
        if path in public:
            continue
        body = re.search(r"async def %s\(request.*?\n(?=async def |\ndef |\Z)" % fn, src, re.S)
        if body and "_authed_user_id" not in body.group(0):
            unguarded.append(path)
    check("эндпоинт без проверки авторизации", not unguarded, str(unguarded))

    js = read("webapp/static/app.js")
    check("escapeHtml экранирует кавычки", '"\'": "&#39;"' in js or "'\\''" in js or "&#39;" in js)

    # Каждый вызов фронта должен попадать в существующий маршрут.
    known = {re.sub(r"\{[^}]+\}", "{x}", r) for r in {p for p, _ in routes}}
    calls = set(re.findall(r"api\(\s*[`\"']([^`\"']+)", js))
    missing = []
    for c in calls:
        norm = re.sub(r"\$\{[^}]+\}", "{x}", c.split("?")[0])
        if norm in known:
            continue
        # /api/buy_rub/premium попадает в /api/buy_rub/{key} — сверяем по основе
        base = norm.rsplit("/", 1)[0] + "/{x}"
        if base not in known:
            missing.append(c)
    check("фронт зовёт несуществующий маршрут", not missing, str(missing))


# ── ЗАГЛУШКИ ВМЕСТО ИМЕНИ ────────────────────────────────────────────────────
def check_subject_name() -> None:
    """Разбор на чужую дату обязан считаться по чужому имени. Пока веб звал
    generate_single без subject_name, числа имени, души и личности брались от
    владельца аккаунта, и разбор для другого человека подписывался его именем."""
    for fname in ("webapp.py", "bot.py"):
        src = read(fname)
        calls = re.findall(r"generate_single\((.*?)\)", src, re.S)
        bad = [c for c in calls if "subject_name" not in c and "def " not in c]
        check(f"{fname}: generate_single без subject_name", not bad,
              str([c.replace("\n", " ")[:70] for c in bad]))


def check_reading_titles() -> None:
    """Название разбора бот шлёт отдельной строкой перед текстом. Если первый
    блок промпта назван так же, человек видит заголовок дважды подряд."""
    import config
    import readings

    norm = lambda x: re.sub(r"\s+", " ", x.replace("\ufe0f", "")).strip().lower()
    clash = []
    for key, prompt in readings.PROMPTS.items():
        title = config.TITLES.get(key, "")
        for line in prompt.split("\n"):
            m = _HEADER_RE.match(line.strip())
            if not m:
                continue
            if norm(line.strip().split("—")[0]) == norm(title):
                clash.append(key)
            break
    check("первый блок повторяет название разбора", not clash, str(clash))


def check_pdf_intro() -> None:
    """Каждый промпт начинается с «Начни так: ...», то есть у любого разбора
    есть вступление до первого заголовка. Раньше оно молча выбрасывалось —
    в PDF разбор начинался сразу со второго абзаца."""
    src = read("pdf.py")
    check("PDF собирает вступление отдельно", "def split_reading" in src)
    check("вступление передаётся в шаблон", "intro          = intro," in src)
    check("шаблон умеет его показать", "{% if intro %}" in read("pdf_template.html"))


def check_gender_note() -> None:
    """Тексты разборов написаны про женщину — «её числа», «что она делает»,
    «передать именно ей». Мужчине модель повторяла женский род за
    инструкцией, поэтому в примечании должно быть явное указание читать
    эти слова в мужском роде."""
    src = read("generation.py")
    note = re.search(r"def _gender_note.*?\n    return \"\"", src, re.S)
    body = note.group(0) if note else ""
    check("_gender_note переопределяет род в самом задании",
          "«она», «её», «ей»" in body and "читай их как" in body)


def check_placeholder_names() -> None:
    """По заглушке нельзя считать числа имени: человек получит числа слова
    «дорогой» вместо своих. Все обращения-заглушки обязаны быть в списке."""
    import numerology

    placeholders = {p.lower() for p in numerology.PLACEHOLDER_NAMES}
    used = set()
    for fname in ("bot.py", "webapp.py", "generation.py", "db.py"):
        used |= {m.lower() for m in re.findall(r'"(дорог(?:ой|ая)(?: человек)?)"', read(fname))}
    check("обращение-заглушка не попало в PLACEHOLDER_NAMES",
          not (used - placeholders), str(used - placeholders))

    for name in ("дорогой", "дорогая", "дорогой человек"):
        ctx = numerology.build_numerology_context(name, "15.03.1995")
        check(f"по заглушке «{name}» считаются числа имени",
              "Число души" not in ctx and f"Имя: {name}" not in ctx)


def main() -> int:
    check_catalog()
    check_header_emoji()
    check_gender()
    check_callbacks()
    check_admin_guards()
    check_web()
    check_subject_name()
    check_reading_titles()
    check_pdf_intro()
    check_gender_note()
    check_placeholder_names()

    total = PASSED + len(FAILURES)
    print(f"пройдено {PASSED} из {total}")
    for f in FAILURES:
        print("  ✗", f)
    print("ЧИСТО" if not FAILURES else "ЕСТЬ ПРОБЛЕМЫ")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
