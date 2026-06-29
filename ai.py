# ai.py — ИИ-провайдеры: Cerebras → Groq → OpenRouter
# Синхронизировано с актуальным bot.py. Самодостаточный модуль: не импортирует
# ничего из bot.py/config.py, держит свои копии HEADER_EMOJI и хелперов
# постобработки текста — так модуль можно использовать независимо.
import os
import re
import logging
import asyncio
import httpx

# ── СИСТЕМНЫЙ ПРОМПТ ─────────────────────────────────────────────────────────
# Сохраняем конкретность и запрет "воды", но требования к объёму смягчены
# ("около", диапазоны, без жёстких минимумов по словам). Жёсткие минимумы слов
# (раньше: "не менее 1300 слов, каждый блок 4-5 абзацев") статистически
# провоцировали модель (особенно gpt-oss-120b) либо обрывать структуру ради
# скорости, либо повторять последние пункты второй раз, пытаясь "дотянуть"
# объём. Полнота структуры важнее точного количества слов.
SYSTEM_PROMPT = (
    "Ты — Ева, нумеролог с 15-летним опытом практики. "
    "Ты делаешь платные разборы для женщин — каждый разбор должен быть "
    "конкретным и ценным, без лишней воды.\n\n"
    "ГОЛОС И СТИЛЬ:\n"
    "Говори как профессионал на живой консультации — уверенно, без лекций. "
    "Можешь использовать фразы вроде 'Я смотрю на твою дату и вижу...', "
    "'Твои числа говорят очень чётко...'. "
    "Каждый блок — это связный текст из нескольких предложений по делу, "
    "не одна короткая фраза.\n\n"
    "ЗАПРЕЩЁННЫЕ ФРАЗЫ — избегай:\n"
    "не бойся / помни / ты должна / будь открыта / возможно / наверное / "
    "может быть / вероятно / постарайся / стремись / важно понять / "
    "это нормально / у тебя всё получится / верь в себя / ты сильная. "
    "Эти фразы — вода. Вместо них давай конкретику.\n\n"
    "КОНКРЕТНОСТЬ — это главное:\n"
    "Плохо: 'ты сильная и способная'. "
    "Хорошо: 'твоё число 8 даёт деловое мышление — ты видишь где деньги раньше других'. "
    "Плохо: 'в этом году будут перемены'. "
    "Хорошо: 'март-апрель 2026 — пиковый период для карьерных решений'. "
    "Называй конкретные числа, месяцы, паттерны, ситуации.\n\n"
    "ОБЪЁМ И ПОЛНОТА — оба обязательны:\n"
    "Если в запросе дан список emoji-заголовков — ответь по КАЖДОМУ из них, "
    "не пропускай ни один и не объединяй несколько в один. "
    "Каждый блок раскрывай содержательно — минимум 4-6 развёрнутых предложений "
    "на блок, с конкретными числами, месяцами и примерами, а не одной короткой фразой. "
    "Если промпт указывает целевой объём (например 'около 1400 слов') — "
    "это ОБЯЗАТЕЛЬНЫЙ МИНИМУМ, а не рекомендация. Считай слова: "
    "1400 слов — это примерно 9000 символов с пробелами. Каждый блок должен "
    "занимать не менее 150-200 слов. Не останавливайся пока не дойдёшь до "
    "указанного объёма. Не растягивай повторами — расширяй конкретикой: "
    "добавляй примеры, уточнения, практические советы. Каждый "
    "emoji-заголовок встречается в ответе ровно один раз.\n\n"
    "ТЕХНИЧЕСКИЕ ТРЕБОВАНИЯ:\n"
    "Только кириллица — никакого английского, никаких иероглифов, никаких других алфавитов. "
    "Никакого markdown — никаких звёздочек, решёток, подчёркиваний. "
    "Эмодзи только перед заголовком блока. "
    "Обращайся только на ТЫ, только женский род — никогда не пиши 'вы', 'ваш'. "
    "Имя пользователя — используй только то что указано в данных, не придумывай другое. "
    "Упоминай имя не чаще 3 раз за весь текст. "
    "НЕ повторяй инструкцию, не пиши план перед ответом — "
    "сразу начинай с первого emoji-заголовка разбора. "
    "Заканчивай полным предложением."
)

# ── ПРОВАЙДЕРЫ ────────────────────────────────────────────────────────────────
# Cerebras — основной, быстрый бесплатный тир, отлично держит русский язык
# Groq — резерв, бесплатный но с жёсткими rate limits
# OpenRouter — последний рубеж, платный по токенам но с авто-роутингом

CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY")
CEREBRAS_MODEL   = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODELS  = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
]

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODELS  = [
    "meta-llama/llama-3.3-70b-instruct",
    "google/gemini-flash-1.5",
    "deepseek/deepseek-chat",
    "mistralai/mistral-small-3.1-24b-instruct",
    "qwen/qwen-2.5-72b-instruct",
    "microsoft/phi-4",
    "google/gemini-flash-1.5-8b",
]

MAX_FOREIGN_RATIO = 0.03

# ── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ПОСТОБРАБОТКИ ────────────────────────────────────
_HEADER_EMOJI = (
    "🔮","✨","💎","💰","💕","🔴","🌟","📅","🎯","💡","🚧",
    "💪","⚠️","🌱","🎭","💼","🤝","📈","⏰","🗺","🌍","🏆",
    "💚","⚡","🫀","😤","📜","🔄","🌳","❄️","☠️","😔","💔",
    "💑","💘","💍","🌠","🏢","😨","🗓","⚖️","🔗","🪤","🗝",
)

_LANGSWAP_MAP = {
    'a':'а', 'A':'А', 'e':'е', 'E':'Е', 'o':'о', 'O':'О',
    'p':'р', 'P':'Р', 'c':'с', 'C':'С', 'x':'х', 'X':'Х',
    'y':'у', 'Y':'У', 'T':'Т', 'H':'Н', 'K':'К', 'M':'М', 'B':'В',
}

_CYR_RE     = re.compile(r'[а-яА-ЯёЁ]')
_FOREIGN_RE = re.compile(
    r'[a-zA-ZÀ-ÿ\u0080-\u024F\u1E00-\u1EFF\u3000-\u9FFF'
    r'\u0250-\u02AF\u0E00-\u0E7F\uAC00-\uD7AF\u4E00-\u9FFF]'
)

def _is_header(s: str) -> bool:
    return any(s.startswith(e) for e in _HEADER_EMOJI)

def _header_emoji_of(s: str) -> str | None:
    for e in _HEADER_EMOJI:
        if s.startswith(e):
            return e
    return None

def _fix_langswap(text: str) -> str:
    """Заменяет одиночную латинскую букву, зажатую соседством кириллицы,
    на её кириллический аналог. Не трогает целые латинские слова/фразы —
    те будут отброшены _clean_text как и раньше."""
    chars = list(text)
    n = len(chars)
    for i, ch in enumerate(chars):
        repl = _LANGSWAP_MAP.get(ch)
        if repl is None:
            continue
        prev_cyr = i > 0     and bool(_CYR_RE.match(chars[i - 1]))
        next_cyr = i < n - 1 and bool(_CYR_RE.match(chars[i + 1]))
        if prev_cyr or next_cyr:
            chars[i] = repl
    return ''.join(chars)

def _foreign_ratio(text: str) -> float:
    stripped = text.replace(" ", "").replace("\n", "")
    if not stripped:
        return 0.0
    return len(_FOREIGN_RE.findall(text)) / len(stripped)

def _clean_text(text: str) -> str:
    """Сначала восстанавливаем 'лангсвопы', и только потом отбрасываем
    оставшиеся недопустимые символы."""
    text = _fix_langswap(text)
    result = []
    for char in text:
        cp = ord(char)
        if (0x0400 <= cp <= 0x04FF or 0x2000 <= cp <= 0x206F or
            0x2600 <= cp <= 0x27FF or 0x1F300 <= cp <= 0x1FFFF or
            0x2700 <= cp <= 0x27BF or
            char in '0123456789.,!?:;-—()«»"\'\n\r\t ⭐'):
            result.append(char)
    return ''.join(result)

def _strip_preamble(text: str) -> str:
    """Любой из ИИ-провайдеров может изредка 'проговорить' структуру ответа
    перед самим ответом — повторить список emoji-заголовков несколько раз
    подряд как план/анализ задачи, и только в последнем повторении за каждым
    заголовком наконец идёт реальный абзац. Ищет последний непрерывный
    "круг" заголовков (тот, что не повторяется снова после себя) и берёт
    текст начиная с него."""
    lines = text.split('\n')
    n = len(lines)

    def has_real_content_after(idx):
        j = idx + 1
        while j < n:
            nl = lines[j].strip()
            if not nl:
                j += 1; continue
            return len(nl) > 40 and not _is_header(nl)
        return False

    real_idx = [i for i in range(n)
                if _is_header(lines[i].strip()) and has_real_content_after(i)]
    if not real_idx:
        return text.strip()

    seen  = set()
    start = real_idx[-1]
    for idx in reversed(real_idx):
        title = lines[idx].strip()
        if title in seen:
            break
        seen.add(title)
        start = idx
    return '\n'.join(lines[start:]).strip()

def _dedupe_sections(text: str) -> str:
    """Модель иногда честно проходит всю структуру промпта, но ближе к концу
    ответа 'спотыкается' и повторяет последние 1-2 раздела ещё раз —
    пересказывая тот же смысл другими словами под тем же emoji-заголовком.
    _strip_preamble не лечит это: он ищет повтор структуры В НАЧАЛЕ текста
    (план перед ответом), а это повтор В СЕРЕДИНЕ/КОНЦЕ уже сданного
    содержательного ответа. Оставляем только ПЕРВОЕ появление каждого
    раздела — сравниваем по полному заголовку (strip), не только по emoji,
    чтобы два разных блока с одним emoji (💔 у unlucky и ex) не склеивались."""
    lines = text.split('\n')
    n     = len(lines)

    section_starts = []
    for i, line in enumerate(lines):
        if _header_emoji_of(line.strip()):
            section_starts.append((i, line.strip()))   # полный заголовок

    if not section_starts:
        return text

    seen_headings = set()
    keep_ranges   = []
    for idx, (start_line, heading) in enumerate(section_starts):
        end_line = section_starts[idx + 1][0] if idx + 1 < len(section_starts) else n
        if heading in seen_headings:
            continue
        seen_headings.add(heading)
        keep_ranges.append((start_line, end_line))

    result_lines = lines[:section_starts[0][0]]
    for start, end in keep_ranges:
        result_lines.extend(lines[start:end])
    return '\n'.join(result_lines).strip()

# ── ПРОВЕРКА ПОЛНОТЫ СТРУКТУРЫ ───────────────────────────────────────────────
def expected_sections_from_prompt(prompt: str) -> set:
    """Извлекает множество emoji-разделов, которые промпт требует от модели,
    парся строки структуры внутри самого текста промпта (там, где они
    перечислены как план ответа: '🔮 Заголовок — пояснение'). Не хранит
    список вручную — берёт прямо из реального prompt, поэтому не
    рассинхронизируется при правке внешних файлов с шаблонами промптов."""
    found = set()
    for line in prompt.split('\n'):
        emoji = _header_emoji_of(line.strip())
        if emoji:
            found.add(emoji)
    return found

def actual_sections(answer: str) -> set:
    found = set()
    for line in answer.split('\n'):
        emoji = _header_emoji_of(line.strip())
        if emoji:
            found.add(emoji)
    return found

def is_structure_complete(prompt: str, answer: str, min_ratio: float = 0.6) -> bool:
    """True если ответ покрывает хотя бы min_ratio ожидаемых разделов из
    структуры, заданной в промпте. 60%, не 100% — модель иногда объединяет
    два близких пункта в один абзац под одним заголовком, это не дефект.
    Промпты без явной emoji-структуры (compat, дневной пост в канал)
    пропускают проверку (всегда True), так как для них нет фиксированного
    списка разделов для сравнения."""
    expected = expected_sections_from_prompt(prompt)
    if not expected:
        return True
    actual   = actual_sections(answer)
    coverage = len(actual & expected) / len(expected)
    return coverage >= min_ratio

# ── ОРФОГРАФИЯ ────────────────────────────────────────────────────────────────
_SPELL_DICT  = None
_SPELL_READY = None
_WORD_RE     = re.compile(r'[а-яА-ЯёЁ]+')

def _get_spell_dict():
    global _SPELL_DICT, _SPELL_READY
    if _SPELL_READY is not None:
        return _SPELL_DICT
    try:
        import enchant
        d = enchant.Dict("ru_RU"); d.check("привет")
        _SPELL_DICT = d; _SPELL_READY = True
        logging.info("Орфографический словарь ru_RU загружен")
    except Exception as e:
        logging.warning(f"Спеллчекер отключён: {e}")
        _SPELL_DICT = None; _SPELL_READY = False
    return _SPELL_DICT

def _similar_enough(a, b):
    if abs(len(a) - len(b)) > max(2, len(a) // 3):
        return False
    common = sum(1 for ch in set(a.lower()) if ch in b.lower())
    return common >= max(1, len(set(a.lower())) // 2)

def _fix_spelling(text: str) -> str:
    """Проверяет каждое кириллическое слово длиннее 3 букв через hunspell;
    если слово отсутствует в словаре и первый вариант исправления похож по
    написанию — тихо заменяет."""
    spell = _get_spell_dict()
    if spell is None:
        return text
    def _replace(match):
        word = match.group(0)
        if len(word) <= 3: return word
        try:
            if spell.check(word): return word
            suggestions = spell.suggest(word)
            if not suggestions: return word
            best = suggestions[0]
            if " " in best or "-" in best: return word
            if not _similar_enough(word, best): return word
            if word[0].isupper(): best = best[0].upper() + best[1:]
            return best
        except Exception: return word
    try:
        return _WORD_RE.sub(_replace, text)
    except Exception as e:
        logging.warning(f"fix_spelling упал: {e}"); return text

def _finalize(raw: str, source: str) -> str | None:
    """Общий пайплайн постобработки для всех трёх провайдеров:
    1) убираем <think> блоки, 2) отрезаем преамбулу, 3) убираем повторные
    появления одного и того же emoji-раздела, 4) восстанавливаем лангсвопы
    и фильтруем недопустимые символы, 5) решаем принять/отбросить по ДОЛЕ
    иностранных символов, 6) исправляем орфографические опечатки модели
    через словарь (если доступен). Проверка ПОЛНОТЫ структуры (все ли
    ожидаемые разделы присутствуют) выполняется отдельно в ask_ai, потому
    что для неё нужен оригинальный prompt, а не только ответ."""
    raw     = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    raw     = _strip_preamble(raw)
    raw     = _dedupe_sections(raw)
    ratio   = _foreign_ratio(raw)
    cleaned = _clean_text(raw)
    if not cleaned.strip():
        logging.warning(f"{source} — пустой текст после очистки"); return None
    if ratio > MAX_FOREIGN_RATIO:
        logging.warning(f"{source} — {ratio:.1%} иностранных символов"); return None
    return _fix_spelling(cleaned)

# ── CEREBRAS ──────────────────────────────────────────────────────────────────
async def _try_cerebras(prompt: str) -> str | None:
    """Cerebras — основной провайдер. reasoning_effort='high' + max_tokens
    увеличен — даёт модели больше пространства довести структуру до конца,
    но НЕ гарантирует полноту (gpt-oss-120b всё равно может срезать путь на
    отдельных запросах) — поэтому финальная проверка полноты в ask_ai.
    timeout 90 секунд: высокий reasoning_effort плюс больший целевой объём
    ответа (4-6 предложений на блок) требуют больше времени и на
    размышление, и на сам текст."""
    if not CEREBRAS_API_KEY:
        return None
    url     = "https://api.cerebras.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"}
    data    = {
        "model": CEREBRAS_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens":  8000,
        "temperature": 0.8,
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, headers=headers, json=data, timeout=90)
            if r.status_code == 429:
                logging.warning("Cerebras 429 rate limit"); return None
            r.raise_for_status()
            msg    = r.json()["choices"][0]["message"]
            raw    = msg.get("content") or ""
            if not raw:
                logging.warning("Cerebras — пустой content в ответе"); return None
            result = _finalize(raw, "Cerebras")
            if result: logging.info("Cerebras ответил успешно")
            return result
    except Exception as e:
        logging.warning(f"Cerebras failed: {e}"); return None

# ── GROQ ──────────────────────────────────────────────────────────────────────
async def _try_groq(prompt: str) -> str | None:
    """Groq — второй в цепочке. Бесплатный, но с rate limits."""
    if not GROQ_API_KEY:
        return None
    url     = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    for model in GROQ_MODELS:
        for attempt in range(2):
            try:
                data = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user",   "content": prompt},
                    ],
                    "max_tokens": 4096,
                }
                async with httpx.AsyncClient() as client:
                    r = await client.post(url, headers=headers, json=data, timeout=45)
                    if r.status_code == 429:
                        retry_after = int(r.headers.get("retry-after", 5))
                        logging.warning(f"Groq {model} 429, retry-after={retry_after}s")
                        await asyncio.sleep(min(retry_after, 10)); break
                    if r.status_code == 400:
                        logging.warning(f"Groq {model} 400: {r.text[:300]}"); break
                    r.raise_for_status()
                    raw    = r.json()["choices"][0]["message"]["content"]
                    result = _finalize(raw, f"Groq {model}")
                    if result is None: continue
                    logging.info(f"Groq {model} ответил успешно")
                    return result
            except Exception as e:
                logging.warning(f"Groq {model} attempt {attempt+1} failed: {e}"); break
    return None

# ── OPENROUTER ────────────────────────────────────────────────────────────────
async def _try_openrouter(prompt: str) -> str | None:
    """OpenRouter — последний рубеж. Платный по токенам, но умеет
    автоматически роутить между моделями, перебирая список по порядку."""
    if not OPENROUTER_API_KEY:
        return None
    url     = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://t.me/nnumerology_bot",
        "X-Title":       "Eva Numerolog Bot",
    }
    data = {
        "models": OPENROUTER_MODELS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens":  6000,
        "temperature": 0.7,
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, headers=headers, json=data, timeout=60)
            if r.status_code == 429:
                logging.warning("OpenRouter 429 rate limit"); return None
            r.raise_for_status()
            body       = r.json()
            raw        = body["choices"][0]["message"]["content"]
            model_used = body.get("model", "unknown")
            result     = _finalize(raw, "OpenRouter")
            if result: logging.info(f"OpenRouter ответил успешно (модель: {model_used})")
            return result
    except Exception as e:
        logging.warning(f"OpenRouter failed: {e}"); return None

# ── ГЛАВНАЯ ФУНКЦИЯ ───────────────────────────────────────────────────────────
async def ask_ai(prompt: str) -> str:
    """Cerebras → Groq → OpenRouter, с проверкой ПОЛНОТЫ структуры ответа.

    Если провайдер дал ответ, но он покрывает меньше 60% ожидаемых
    emoji-разделов из промпта (как в реальном случае с 'matrix_full', когда
    gpt-oss-120b отвечал только последним блоком из восьми) — результат не
    принимается, и бот переходит к следующему провайдеру, как при полном
    отказе. Каждый провайдер также получает одну повторную попытку САМ С
    СОБОЙ перед тем как сдаться — иногда у той же модели со второй попытки
    структура получается полной. Если все варианты неполные — отдаётся
    лучший доступный результат, а не отказ."""
    providers = [
        ("Cerebras",   _try_cerebras),
        ("Groq",       _try_groq),
        ("OpenRouter", _try_openrouter),
    ]
    last_incomplete: str | None = None

    for name, fn in providers:
        for attempt in range(2):  # сама попытка + один повтор у того же провайдера
            result = await fn(prompt)
            if not result:
                break  # этот провайдер недоступен вовсе — переходим к следующему
            if is_structure_complete(prompt, result):
                return result
            logging.warning(
                f"{name} попытка {attempt + 1}: ответ неполный по структуре, "
                f"{'повторяю' if attempt == 0 else 'перехожу к следующему провайдеру'}"
            )
            last_incomplete = result
        # переходим к следующему провайдеру во внешнем цикле

    if last_incomplete:
        logging.warning("Все провайдеры дали неполную структуру — отдаю последний доступный результат")
        return last_incomplete
    raise Exception("Все провайдеры недоступны или вернули иностранные символы")
