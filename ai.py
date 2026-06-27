# ai.py — ИИ-провайдеры: Cerebras → Groq → OpenRouter
import os
import re
import logging
import asyncio
import httpx

# ── НОВЫЙ SYSTEM_PROMPT ───────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "Ты — Ева, нумеролог с 15-летним опытом практики. "
    "Ты делаешь платные разборы для женщин — твои клиентки платят за качество, "
    "поэтому каждый разбор должен быть подробным, конкретным и ценным.\n\n"
    "ГОЛОС И СТИЛЬ:\n"
    "Говори как профессионал на живой консультации — уверенно, без лекций. "
    "Используй фразы: 'Я смотрю на твою дату и вижу...', "
    "'Твои числа говорят очень чётко...', 'Первое что бросается в глаза — ...'. "
    "Каждый блок — это 3-4 связных абзаца, не одно предложение.\n\n"
    "ЗАПРЕЩЁННЫЕ ФРАЗЫ — никогда не пиши:\n"
    "не бойся / помни / ты должна / будь открыта / возможно / наверное / "
    "может быть / вероятно / постарайся / стремись / важно понять / "
    "это нормально / у тебя всё получится / верь в себя / ты сильная. "
    "Эти фразы — вода. Вместо них давай конкретику.\n\n"
    "КОНКРЕТНОСТЬ — это главное:\n"
    "Плохо: 'ты сильная и способная'. "
    "Хорошо: 'твоё число 8 даёт деловое мышление — ты видишь где деньги раньше других'. "
    "Плохо: 'в этом году будут перемены'. "
    "Хорошо: 'март-апрель 2026 — пиковый период для карьерных решений, именно тогда действуй'. "
    "Называй конкретные числа, месяцы, паттерны, ситуации.\n\n"
    "ОБЪЁМ — критически важно:\n"
    "Разбор за 149 звёзд = не менее 1300 слов. Каждый блок по 4-5 абзацев. "
    "Разбор за 99 звёзд = не менее 1000 слов. Каждый блок по 3-4 абзаца. "
    "Разбор за 79 звёзд = не менее 700 слов. Каждый блок по 2-3 абзаца. "
    "Разбор за 49 звёзд = не менее 400 слов. Каждый блок по 2 абзаца. "
    "Никогда не обрывай разбор на середине. Заканчивай полным блоком.\n\n"
    "ТЕХНИЧЕСКИЕ ТРЕБОВАНИЯ:\n"
    "Только кириллица — никакого английского, никаких иероглифов, никаких других алфавитов. "
    "Никакого markdown — никаких звёздочек, решёток, подчёркиваний. "
    "Эмодзи только перед заголовком блока. "
    "Обращайся только на ТЫ, только женский род — никогда не пиши 'вы', 'ваш'. "
    "Имя пользователя — используй ТОЛЬКО то имя которое указано в данных разбора, "
    "не придумывай и не подставляй другое имя. "
    "Упоминай имя не чаще 3 раз за весь текст. "
    "НЕ повторяй инструкцию, не пиши план перед ответом — "
    "сразу начинай с первого emoji-заголовка разбора. "
    "Заканчивай полным предложением."
)

# ── ПРОВАЙДЕРЫ ────────────────────────────────────────────────────────────────
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
    'a':'а','A':'А','e':'е','E':'Е','o':'о','O':'О',
    'p':'р','P':'Р','c':'с','C':'С','x':'х','X':'Х',
    'y':'у','Y':'У','T':'Т','H':'Н','K':'К','M':'М','B':'В',
}

import re as _re
_CYR_RE     = _re.compile(r'[а-яА-ЯёЁ]')
_FOREIGN_RE = _re.compile(
    r'[a-zA-ZÀ-ÿ\u0080-\u024F\u1E00-\u1EFF\u3000-\u9FFF'
    r'\u0250-\u02AF\u0E00-\u0E7F\uAC00-\uD7AF\u4E00-\u9FFF]'
)

def _is_header(s: str) -> bool:
    return any(s.startswith(e) for e in _HEADER_EMOJI)

def _header_emoji_of(s: str):
    for e in _HEADER_EMOJI:
        if s.startswith(e):
            return e
    return None

def _fix_langswap(text: str) -> str:
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
    """Убирает повторные секции. Сравниваем по полному тексту заголовка —
    🔮 Матрица судьбы и 🔮 Что говорит дата — разные заголовки с одним эмодзи,
    раньше второй удалялся."""
    lines = text.split('\n')
    n     = len(lines)

    section_starts = []
    for i, line in enumerate(lines):
        if _header_emoji_of(line.strip()):
            section_starts.append((i, line.strip()))

    if not section_starts:
        return text

    seen_headers = set()
    keep_ranges  = []
    for idx, (start_line, header) in enumerate(section_starts):
        end_line = section_starts[idx + 1][0] if idx + 1 < len(section_starts) else n
        if header in seen_headers:
            continue
        seen_headers.add(header)
        keep_ranges.append((start_line, end_line))

    result_lines = lines[:section_starts[0][0]]
    for start, end in keep_ranges:
        result_lines.extend(lines[start:end])
    return '\n'.join(result_lines).strip()

# ── ОРФОГРАФИЯ ────────────────────────────────────────────────────────────────
_SPELL_DICT  = None
_SPELL_READY = None
_WORD_RE     = _re.compile(r'[а-яА-ЯёЁ]+')

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
    raw     = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
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
        "max_tokens":       8000,
        "temperature":      1.0,
        "reasoning_effort": "high",
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, headers=headers, json=data, timeout=45)
            if r.status_code == 429:
                logging.warning("Cerebras 429"); return None
            r.raise_for_status()
            raw    = r.json()["choices"][0]["message"]["content"]
            result = _finalize(raw, "Cerebras")
            if result: logging.info("Cerebras ответил успешно")
            return result
    except Exception as e:
        logging.warning(f"Cerebras failed: {e}"); return None

# ── GROQ ──────────────────────────────────────────────────────────────────────
async def _try_groq(prompt: str) -> str | None:
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
                    "max_tokens": 4000,
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
        "max_tokens":  4000,
        "temperature": 0.7,
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, headers=headers, json=data, timeout=60)
            if r.status_code == 429:
                logging.warning("OpenRouter 429"); return None
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
    result = await _try_cerebras(prompt)
    if result: return result
    logging.warning("Cerebras не дал результат — пробую Groq")
    result = await _try_groq(prompt)
    if result: return result
    logging.warning("Groq не дал результат — пробую OpenRouter")
    result = await _try_openrouter(prompt)
    if result: return result
    raise Exception("Все провайдеры недоступны")
