import os
import re
import time
import logging
import asyncio
import json
import httpx
import asyncpg
import random
import io
import math
from datetime import datetime, date, timedelta, timezone
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, TelegramObject,
    BufferedInputFile
)
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from fpdf import FPDF
from readings import MATRIX_LITE, PROMPTS
from broadcasts import MORNING

BOT_TOKEN    = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")  # последний бесплатный резерв, не обязателен
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not DATABASE_URL:
    raise EnvironmentError("BOT_TOKEN и DATABASE_URL должны быть установлены!")
if not any([os.getenv("CEREBRAS_API_KEY"), os.getenv("GROQ_API_KEY"), os.getenv("OPENROUTER_API_KEY")]):
    raise EnvironmentError(
        "Нужен хотя бы один ИИ-провайдер: CEREBRAS_API_KEY, GROQ_API_KEY или OPENROUTER_API_KEY."
    )

CHANNEL         = "@eva_numerologg"
REVIEWS_CHANNEL = "@eva_numerolog_otz"
ADMIN_ID        = 5854618444
CONTACT_URL     = "https://t.me/eva_numer"

# Шрифт для PDF — должен лежать в репо как DejaVuSans.ttf
FONT_PATH      = os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf")
BOLD_FONT_PATH = os.path.join(os.path.dirname(__file__), "DejaVuSans-Bold.ttf")

logging.basicConfig(level=logging.INFO)
bot     = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)
db_pool = None

# ─── АНТИФЛУД MIDDLEWARE ─────────────────────────────────────────────────────
class AntiFloodMiddleware(BaseMiddleware):
    def __init__(self, timeout: float = 3.0):
        self.timeout      = timeout
        self.last_request = {}
        self._call_count  = 0

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user:
            now  = time.time()
            last = self.last_request.get(user.id, 0)
            if now - last < self.timeout:
                if isinstance(event, Message):
                    await event.answer("⏳ Не так быстро! Подожди пару секунд.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("⏳ Подожди немного!", show_alert=False)
                return
            self.last_request[user.id] = now
            self._call_count += 1
            if self._call_count >= 500:
                self._call_count = 0
                cutoff = now - 60
                old    = [uid for uid, t in self.last_request.items() if t < cutoff]
                for uid in old:
                    del self.last_request[uid]
        return await handler(event, data)

dp.message.middleware(AntiFloodMiddleware(3.0))
dp.callback_query.middleware(AntiFloodMiddleware(1.0))

# ─── РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ ──────────────────────────────────────────────
async def send_long(chat_id, text: str):
    limit = 4000
    if len(text) <= limit:
        await bot.send_message(chat_id, text)
        return
    parts = []
    while len(text) > limit:
        split_at = text.rfind('\n', 0, limit)
        if split_at == -1:
            split_at = limit
        parts.append(text[:split_at])
        text = text[split_at:].lstrip()
    if text:
        parts.append(text)
    for part in parts:
        await bot.send_message(chat_id, part)
        await asyncio.sleep(0.3)

# ─── PDF ГЕНЕРАЦИЯ ────────────────────────────────────────────────────────────
def _ensure_font() -> str | None:
    """Проверяет шрифт, при необходимости скачивает. Возвращает путь или None."""
    if os.path.exists(FONT_PATH):
        try:
            with open(FONT_PATH, "rb") as f:
                magic = f.read(4)
            if magic[:2] in (b"\x00\x01", b"OT", b"tr", b"\x00\x00"):
                return FONT_PATH
            logging.warning(f"Файл {FONT_PATH} не является TTF, пробую скачать")
        except Exception:
            pass
    try:
        import urllib.request, zipfile
        zip_url = "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip"
        logging.info("Скачиваю шрифт DejaVuSans...")
        resp = urllib.request.urlopen(zip_url, timeout=30)
        zdata = resp.read()
        with zipfile.ZipFile(io.BytesIO(zdata)) as z:
            ttf_name = next(n for n in z.namelist() if "DejaVuSans.ttf" in n and "Bold" not in n and "Oblique" not in n and "Mono" not in n and "Condensed" not in n)
            with z.open(ttf_name) as src, open(FONT_PATH, "wb") as dst:
                dst.write(src.read())
        logging.info(f"Шрифт установлен: {FONT_PATH}")
        return FONT_PATH
    except Exception as e:
        logging.warning(f"Не удалось скачать шрифт: {e}")
        return None

def _ensure_bold_font() -> str | None:
    """Жирное начертание DejaVuSans-Bold — для настоящих жирных заголовков в PDF.
    Необязательно: при неудаче просто используется обычное начертание."""
    if os.path.exists(BOLD_FONT_PATH):
        try:
            with open(BOLD_FONT_PATH, "rb") as f:
                magic = f.read(4)
            if magic[:2] in (b"\x00\x01", b"OT", b"tr", b"\x00\x00"):
                return BOLD_FONT_PATH
        except Exception:
            pass
    try:
        import urllib.request, zipfile
        zip_url = "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip"
        resp = urllib.request.urlopen(zip_url, timeout=30)
        zdata = resp.read()
        with zipfile.ZipFile(io.BytesIO(zdata)) as z:
            ttf_name = next(n for n in z.namelist() if "DejaVuSans-Bold.ttf" in n and "Oblique" not in n and "Mono" not in n and "Condensed" not in n)
            with z.open(ttf_name) as src, open(BOLD_FONT_PATH, "wb") as dst:
                dst.write(src.read())
        return BOLD_FONT_PATH
    except Exception as e:
        logging.warning(f"Не удалось скачать жирный шрифт: {e}")
        return None

# Эмодзи-маркеры подзаголовков внутри текста разбора — единый источник правды
HEADER_EMOJI = (
    "🔮","✨","💎","💰","💕","🔴","🌟","📅","🎯","💡","🚧",
    "💪","⚠️","🌱","🎭","💼","🤝","📈","⏰","🗺","🌍","🏆",
    "💚","⚡","🫀","😤","📜","🔄","🌳","❄️","☠️","😔","💔",
    "💑","💘","💍","🌠","🏢","😨","🗓","⚖️","🔗","🪤","🗝",
)

def _is_header_line(s: str) -> bool:
    return any(s.startswith(e) for e in HEADER_EMOJI)

def strip_preamble(text: str) -> str:
    """Любой из ИИ-провайдеров (Cerebras / Groq / OpenRouter) может изредка
    'проговорить' структуру ответа перед самим ответом — повторить список
    emoji-заголовков несколько раз подряд как план/анализ задачи, и только
    в последнем повторении за каждым заголовком наконец идёт реальный абзац.

    РАНЬШЕ в файле было ДВА разных определения strip_preamble (второе молча
    переопределяло первое — Python не предупреждает об этом), активная версия
    искала только ПЕРВОЕ совпадение emoji-заголовка — то есть отрезала текст
    слишком рано, оставляя кусок преамбулы в начале настоящего ответа. Более
    того, эта функция вообще не вызывалась ни в одном из трёх провайдеров —
    была мёртвым кодом.

    Эта версия ищет последний непрерывный "круг" заголовков (тот, что не
    повторяется снова после себя) и берёт текст начиная с него. Подключена
    во все три провайдера ниже."""
    lines = text.split('\n')
    n = len(lines)

    def has_real_content_after(idx: int) -> bool:
        j = idx + 1
        while j < n:
            nl = lines[j].strip()
            if not nl:
                j += 1
                continue
            return len(nl) > 40 and not _is_header_line(nl)
        return False

    real_header_indices = [
        i for i in range(n)
        if _is_header_line(lines[i].strip()) and has_real_content_after(i)
    ]
    if not real_header_indices:
        return text.strip()

    seen_titles = set()
    start = real_header_indices[-1]
    for idx in reversed(real_header_indices):
        title = lines[idx].strip()
        if title in seen_titles:
            break
        seen_titles.add(title)
        start = idx

    return '\n'.join(lines[start:]).strip()

# ── Карта "лангсвопов" ──
# Языковые модели иногда случайно подменяют кириллическую букву на визуально
# похожую латинскую внутри русского слова. Раньше clean_text просто
# ВЫБРАСЫВАЛ такую латинскую букву как "недопустимый символ", оставляя дырку
# в середине слова ("Твoей" -> "Твй"). Теперь сначала восстанавливаем букву,
# и только потом фильтруем.
_LANGSWAP_MAP = {
    'a': 'а', 'A': 'А',
    'e': 'е', 'E': 'Е',
    'o': 'о', 'O': 'О',
    'p': 'р', 'P': 'Р',
    'c': 'с', 'C': 'С',
    'x': 'х', 'X': 'Х',
    'y': 'у', 'Y': 'У',
    'T': 'Т', 'H': 'Н', 'K': 'К', 'M': 'М', 'B': 'В',
}
_CYR_RE = re.compile(r'[а-яА-ЯёЁ]')

def fix_langswap(text: str) -> str:
    """Заменяет одиночную латинскую букву, зажатую соседством кириллицы,
    на её кириллический аналог. Не трогает целые латинские слова/фразы —
    те будут отброшены has_foreign/clean_text как и раньше."""
    chars = list(text)
    n = len(chars)
    for i, ch in enumerate(chars):
        repl = _LANGSWAP_MAP.get(ch)
        if repl is None:
            continue
        prev_cyr = i > 0 and bool(_CYR_RE.match(chars[i - 1]))
        next_cyr = i < n - 1 and bool(_CYR_RE.match(chars[i + 1]))
        if prev_cyr or next_cyr:
            chars[i] = repl
    return ''.join(chars)

# Цветовая палитра — нумерологическая тема (лаванда / аметист / золото)
C_BG          = (250, 247, 255)
C_BAR         = (157, 117, 196)
C_BORDER      = (197, 165, 224)
C_TITLE       = (104, 52, 158)
C_HEADER      = (122, 64, 172)
C_BODY        = (51, 36, 71)
C_BADGE_FILL  = (234, 224, 248)
C_BADGE_TEXT  = (104, 64, 148)
C_ACCENT      = (172, 130, 212)
C_GOLD        = (193, 154, 76)   # золото для нумерологических символов

def _draw_star_polygon(pdf: FPDF, cx: float, cy: float, r_outer: float, r_inner: float,
                        points: int, color, rotate_deg: float = -90, line_width: float = 0.45):
    """Рисует многолучевую звезду (восьмиконечная — классический нумерологический
    символ октаграммы) чистыми линиями, без внешних иконочных шрифтов."""
    pdf.set_draw_color(*color)
    pdf.set_line_width(line_width)
    coords = []
    total_vertices = points * 2
    for i in range(total_vertices):
        angle = math.radians(rotate_deg + i * 360 / total_vertices)
        r = r_outer if i % 2 == 0 else r_inner
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        coords.append((x, y))
    for i in range(total_vertices):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % total_vertices]
        pdf.line(x1, y1, x2, y2)

def _draw_corner_ornament(pdf: FPDF, x: float, y: float, size: float, color,
                           flip_x: bool = False, flip_y: bool = False):
    """Простой геометрический уголок-орнамент (вложенные L-образные линии) —
    традиционный эзотерический декор страницы, рисуется только линиями."""
    sx = -1 if flip_x else 1
    sy = -1 if flip_y else 1
    pdf.set_draw_color(*color)
    for i, offset in enumerate((0, 2.2, 4.4)):
        lw = 0.6 if i == 0 else 0.3
        pdf.set_line_width(lw)
        ox, oy = x + offset * sx, y + offset * sy
        pdf.line(ox, oy, ox + size * sx, oy)
        pdf.line(ox, oy, ox, oy + size * sy)

def _draw_number_medallion(pdf: FPDF, cx: float, cy: float, radius: float,
                            number: int, font_name: str):
    """Медальон с числом судьбы — круг с золотым ободом и числом внутри,
    традиционная нумерологическая подача 'личного числа'."""
    pdf.set_draw_color(*C_GOLD)
    pdf.set_line_width(0.7)
    pdf.ellipse(cx - radius, cy - radius, radius * 2, radius * 2, style="D")
    pdf.set_draw_color(*C_ACCENT)
    pdf.set_line_width(0.3)
    pdf.ellipse(cx - radius + 1.6, cy - radius + 1.6, (radius - 1.6) * 2, (radius - 1.6) * 2, style="D")
    text = str(number)
    pdf.set_font(font_name, style="B", size=radius * 1.15)
    pdf.set_text_color(*C_TITLE)
    tw = pdf.get_string_width(text)
    pdf.set_xy(cx - tw / 2, cy - radius * 0.62)
    pdf.cell(tw, radius * 1.2, text, align="C")

class NumerologyPDF(FPDF):
    """PDF с фирменным оформлением Евы — фон, рамка, орнаментальные уголки и
    полосы рисуются на КАЖДОЙ странице через header()/footer(), а не только
    на первой (раньше декор рисовался один раз вручную после add_page(), и у
    длинных разборов 2-я+ страницы оставались пустыми и белыми)."""

    def __init__(self, font_name: str = "Helvetica"):
        super().__init__()
        self.font_name = font_name
        self.set_auto_page_break(auto=True, margin=18)

    def header(self):
        W, H = self.w, self.h
        self.set_fill_color(*C_BG)
        self.rect(0, 0, W, H, style="F")
        self.set_fill_color(*C_BAR)
        self.rect(0, 0, W, 9, style="F")
        self.rect(0, H - 9, W, 9, style="F")
        self.set_draw_color(*C_BORDER)
        self.set_line_width(0.5)
        self.rect(12, 13, W - 24, H - 13 - 12)

        # Орнаментальные уголки — традиционный эзотерический декор страницы
        corner_size = 9
        _draw_corner_ornament(self, 14, 16, corner_size, C_GOLD, flip_x=False, flip_y=False)
        _draw_corner_ornament(self, W - 14, 16, corner_size, C_GOLD, flip_x=True, flip_y=False)
        _draw_corner_ornament(self, 14, H - 15, corner_size, C_GOLD, flip_x=False, flip_y=True)
        _draw_corner_ornament(self, W - 14, H - 15, corner_size, C_GOLD, flip_x=True, flip_y=True)

        self.set_font(self.font_name, style="B", size=9)
        self.set_text_color(255, 255, 255)
        self.set_xy(0, 2)
        self.cell(W, 5.5, "✦  EVA NUMEROLOG  ✦", align="C")
        self.set_xy(self.l_margin, 21)

    def footer(self):
        self.set_y(-8.3)
        self.set_font(self.font_name, size=8)
        self.set_text_color(255, 255, 255)
        self.cell(0, 5.5, f"Telegram: @nnumerology_bot   •   стр. {self.page_no()}", align="C")


def generate_pdf(title: str, text: str, user_name: str = "", destiny_number: int | None = None) -> bytes:
    """Красивый PDF для женской аудитории, с нумерологической символикой:
    восьмиконечная звезда-октаграмма в разделителе, орнаментальные уголки
    страницы и медальон с числом судьбы рядом с именем (если оно известно)."""
    font_path = _ensure_font()
    font_name = "DejaVu" if font_path else "Helvetica"

    pdf = NumerologyPDF(font_name=font_name)

    if font_path:
        try:
            bold_path = _ensure_bold_font()
            pdf.add_font("DejaVu", style="", fname=font_path)
            pdf.add_font("DejaVu", style="B", fname=bold_path or font_path)
        except Exception as e:
            logging.warning(f"Не удалось загрузить шрифт: {e}")
            font_name     = "Helvetica"
            pdf.font_name = "Helvetica"

    pdf.set_margins(20, 21, 20)
    pdf.add_page()

    W = pdf.w

    clean_title = re.sub(r"[^\w\s\(\)\-—.,]", "", title, flags=re.UNICODE).strip()
    pdf.set_font(font_name, style="B", size=18)
    pdf.set_text_color(*C_TITLE)
    pdf.multi_cell(0, 9, clean_title.upper(), align="C")
    pdf.ln(2)

    # ── декоративный разделитель с восьмиконечной звездой по центру ──
    y   = pdf.get_y()
    mid = W / 2
    pdf.set_draw_color(*C_ACCENT)
    pdf.set_line_width(0.4)
    pdf.line(pdf.l_margin + 6, y + 3.5, mid - 7, y + 3.5)
    pdf.line(mid + 7, y + 3.5, W - pdf.r_margin - 6, y + 3.5)
    _draw_star_polygon(pdf, mid, y + 3.5, r_outer=4.2, r_inner=1.8, points=8, color=C_GOLD)
    pdf.set_y(y + 9)

    # ── Бейдж: медальон с числом судьбы + имя + дата в одной строке ──
    info_parts = []
    if user_name:
        info_parts.append(f"Для {user_name}")
    info_parts.append(datetime.now().strftime("%d.%m.%Y"))
    info_text = "   •   ".join(info_parts)

    pdf.set_font(font_name, size=10.5)
    medallion_d = 11 if destiny_number is not None else 0
    badge_w = pdf.get_string_width(info_text) + 16 + medallion_d
    badge_h = 8.5
    badge_x = (W - badge_w) / 2
    badge_y = pdf.get_y()
    pdf.set_fill_color(*C_BADGE_FILL)
    pdf.set_draw_color(*C_BORDER)
    pdf.set_line_width(0.3)
    pdf.rect(badge_x, badge_y, badge_w, badge_h, style="DF")

    text_x = badge_x
    if destiny_number is not None:
        _draw_number_medallion(
            pdf, badge_x + medallion_d / 2 + 2, badge_y + badge_h / 2,
            radius=medallion_d / 2, number=destiny_number, font_name=font_name
        )
        text_x = badge_x + medallion_d + 2

    pdf.set_xy(text_x, badge_y + 1.4)
    pdf.set_text_color(*C_BADGE_TEXT)
    pdf.cell(badge_w - (text_x - badge_x), 6, info_text, align="C")
    pdf.set_xy(pdf.l_margin, badge_y + badge_h + 8)

    # ── Основной текст ──
    pdf.set_font(font_name, size=11.5)
    pdf.set_text_color(*C_BODY)

    for paragraph in text.split("\n"):
        line = paragraph.strip()
        if not line:
            pdf.ln(4)
            continue
        clean_line = re.sub(r"[^\w\s\(\)\-—.,!?:;]", "", line, flags=re.UNICODE).strip()
        is_header  = _is_header_line(paragraph)
        if is_header and clean_line:
            pdf.ln(1)
            pdf.set_x(pdf.l_margin)
            pdf.set_font(font_name, style="B", size=12.5)
            pdf.set_text_color(*C_HEADER)
            pdf.multi_cell(0, 8, clean_line)
            pdf.set_font(font_name, size=11.5)
            pdf.set_text_color(*C_BODY)
            pdf.ln(1)
        elif clean_line:
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(0, 7, clean_line)
            pdf.ln(1)

    return bytes(pdf.output())

# ─── НУМЕРОЛОГИЧЕСКИЕ РАСЧЁТЫ ────────────────────────────────────────────────
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

# ─── ЗАЩИТА ОТ ИНОСТРАННЫХ СИМВОЛОВ ─────────────────────────────────────────
FOREIGN_RE = re.compile(
    r'[a-zA-ZÀ-ÿ\u0080-\u024F\u1E00-\u1EFF\u3000-\u9FFF'
    r'\u0250-\u02AF\u0E00-\u0E7F\uAC00-\uD7AF\u4E00-\u9FFF]'
)

def has_foreign(text: str) -> bool:
    return bool(FOREIGN_RE.search(text))

def foreign_ratio(text: str) -> float:
    """Доля иностранных символов от общей длины текста (без пробелов).
    Используется вместо абсолютной длины очищенного текста для решения
    'принять или отбросить ответ' — раньше длинный ответ с остаточным
    мусором (>200 символов очищенного текста) принимался безусловно, даже
    если процент грязи в нём был высоким."""
    stripped = text.replace(" ", "").replace("\n", "")
    if not stripped:
        return 0.0
    foreign_count = len(FOREIGN_RE.findall(text))
    return foreign_count / len(stripped)

def clean_text(text: str) -> str:
    """Сначала восстанавливаем 'лангсвопы' (одиночные латинские буквы внутри
    кириллических слов — fix_langswap), и только потом отбрасываем оставшиеся
    недопустимые символы. Раньше латинская буква внутри слова просто
    выбрасывалась посимвольно, оставляя дырку ('Твoей' -> 'Твй')."""
    text = fix_langswap(text)
    result = []
    for char in text:
        cp = ord(char)
        if (
            0x0400 <= cp <= 0x04FF or
            0x2000 <= cp <= 0x206F or
            0x2600 <= cp <= 0x27FF or
            0x1F300 <= cp <= 0x1FFFF or
            0x2700 <= cp <= 0x27BF or
            char in '0123456789.,!?:;-—()«»"\'\n\r\t ⭐'
        ):
            result.append(char)
    return ''.join(result)

# ─── ОРФОГРАФИЧЕСКАЯ КОРРЕКЦИЯ (pyenchant + hunspell, русский словарь) ──────
# Лечит случаи когда сама модель генерирует испорченное русское слово
# ("для создания" -> "дляления") — это НЕ смешение алфавитов (fix_langswap
# тут не помогает, все символы корректно кириллические) и не лишний символ
# (clean_text тут не при делах) — это орфографическая ошибка самой генерации.
# Требует системный словарь hunspell-ru, который ставится через nixpacks.toml
# (см. файл в репозитории). Если словарь недоступен — модуль молча отключается
# и не влияет на работу бота (graceful degradation).
_SPELL_DICT = None
_SPELL_READY = None

def _get_spell_dict():
    global _SPELL_DICT, _SPELL_READY
    if _SPELL_READY is not None:
        return _SPELL_DICT
    try:
        import enchant
        d = enchant.Dict("ru_RU")
        d.check("привет")  # пробный вызов — убеждаемся что словарь реально работает
        _SPELL_DICT = d
        _SPELL_READY = True
        logging.info("Орфографический словарь ru_RU загружен")
    except Exception as e:
        logging.warning(f"Орфографический словарь ru_RU недоступен, спеллчекер отключён: {e}")
        _SPELL_DICT = None
        _SPELL_READY = False
    return _SPELL_DICT

_WORD_RE = re.compile(r'[а-яА-ЯёЁ]+')

def _similar_enough(a: str, b: str) -> bool:
    """Грубая защита от того чтобы спеллчекер заменял слово на что-то
    совсем другое по смыслу — длина похожа и хотя бы половина букв общая."""
    if abs(len(a) - len(b)) > max(2, len(a) // 3):
        return False
    common = sum(1 for ch in set(a.lower()) if ch in b.lower())
    return common >= max(1, len(set(a.lower())) // 2)

def fix_spelling(text: str) -> str:
    """Проверяет каждое кириллическое слово длиннее 3 букв через hunspell;
    если слово отсутствует в словаре и первый вариант исправления похож по
    написанию — тихо заменяет. Слова из имён/чисел/эмодзи не затрагиваются,
    т.к. regex выбирает только буквенные кириллические последовательности.
    Если словарь недоступен — возвращает текст без изменений."""
    spell = _get_spell_dict()
    if spell is None:
        return text

    def _replace(match: re.Match) -> str:
        word = match.group(0)
        if len(word) <= 3:
            return word
        try:
            if spell.check(word):
                return word
            suggestions = spell.suggest(word)
            if not suggestions:
                return word
            best = suggestions[0]
            if " " in best or "-" in best:
                return word  # не подставляем словосочетания вместо одного слова
            if not _similar_enough(word, best):
                return word
            # сохраняем регистр первой буквы исходного слова
            if word[0].isupper():
                best = best[0].upper() + best[1:]
            return best
        except Exception:
            return word

    try:
        return _WORD_RE.sub(_replace, text)
    except Exception as e:
        logging.warning(f"fix_spelling упал, возвращаю текст без изменений: {e}")
        return text

# ─── ЦЕНЫ И МЕТАДАННЫЕ ───────────────────────────────────────────────────────
TITLES = {
    "free":            "💫 Матрица судьбы",
    "matrix_full":     "🔮 Матрица судьбы (Полная)",
    "finance":         "💹 Финансовый прогноз",
    "wealth_blocks":   "🚧 Блоки богатства",
    "freedom_path":    "🗺 Путь к свободе",
    "calling":         "🌠 Призвание",
    "promotion":       "📈 Повышение",
    "own_business":    "🏢 Свой бизнес",
    "hidden_talents":  "✨ Скрытые таланты",
    "main_fear":       "😨 Главный страх",
    "forecast_2026":   "🗓 Прогноз на 2026 год",
    "strong_weak":     "⚖️ Сильная и слабая сторона",
    "compat":          "💑 Совместимость двух людей",
    "when":            "💘 Когда встретишь того самого",
    "portrait":        "💍 Портрет идеального партнёра",
    "unlucky":         "💔 Почему не везёт в любви",
    "mission":         "🌟 Предназначение и миссия",
    "karma":           "🔴 Кармический долг",
    "career":          "💼 Карьерный путь",
    "money":           "💰 Денежный код",
    "days":            "🌙 Сильные и слабые дни",
    "ex":              "💔 Вернётся ли бывший",
    "cold":            "❄️ Почему он охладел",
    "toxic":           "☠️ Токсичная или кармическая связь",
    "lonely":          "😔 Почему ты одинока",
    "breakup":         "💔 Разбор после расставания",
    "health_code":     "💚 Код здоровья",
    "energy_drain":    "⚡ Что крадёт энергию",
    "body_message":    "🫀 Послания тела",
    "stress_number":   "😤 Число стресса",
    "intuition":       "🔮 Интуиция и внутренний голос",
    "past_life":       "📜 Прошлые жизни",
    "future_portal":   "🌟 Прогноз на 3 года",
    "turning_point":   "🔄 Поворотные точки судьбы",
    "ancestor_code":   "🌳 Родовой код",
}

PRICES = {
    "matrix_full":   149,
    "forecast_2026": 149,
    "wealth_blocks": 149,
    "freedom_path":  149,
    "mission":       99,
    "karma":         99,
    "compat":        99,
    "own_business":  99,
    "finance":       99,
    "promotion":     99,
    "calling":       79,
    "career":        79,
    "money":         79,
    "when":          79,
    "portrait":      79,
    "breakup":       79,
    "toxic":         79,
    "hidden_talents":79,
    "days":          79,
    "unlucky":       49,
    "ex":            49,
    "cold":          49,
    "lonely":        49,
    "main_fear":     49,
    "strong_weak":   49,
    "health_code":   79,
    "energy_drain":  49,
    "body_message":  49,
    "stress_number": 49,
    "intuition":     79,
    "past_life":     99,
    "future_portal": 149,
    "turning_point": 79,
    "ancestor_code": 99,
}

# Разборы для которых генерируется PDF (79⭐ и выше)
PDF_KEYS = {k for k, v in PRICES.items() if v >= 79}

UPSELLS = {
    "matrix_full":   ("forecast_2026", "mission"),
    "forecast_2026": ("matrix_full",   "karma"),
    "finance":       ("wealth_blocks", "freedom_path"),
    "wealth_blocks": ("finance",       "own_business"),
    "freedom_path":  ("calling",       "own_business"),
    "calling":       ("career",        "own_business"),
    "career":        ("promotion",     "money"),
    "money":         ("finance",       "wealth_blocks"),
    "karma":         ("mission",       "matrix_full"),
    "mission":       ("karma",         "hidden_talents"),
    "hidden_talents":("calling",       "strong_weak"),
    "promotion":     ("career",        "own_business"),
    "own_business":  ("freedom_path",  "finance"),
    "compat":        ("when",          "portrait"),
    "when":          ("portrait",      "compat"),
    "portrait":      ("when",          "unlucky"),
    "unlucky":       ("ex",            "lonely"),
    "ex":            ("toxic",         "compat"),
    "cold":          ("toxic",         "ex"),
    "toxic":         ("cold",          "breakup"),
    "lonely":        ("unlucky",       "portrait"),
    "breakup":       ("ex",            "toxic"),
    "days":          ("finance",       "forecast_2026"),
    "strong_weak":   ("hidden_talents","main_fear"),
    "main_fear":     ("strong_weak",   "karma"),
    "health_code":   ("energy_drain",  "intuition"),
    "energy_drain":  ("health_code",   "stress_number"),
    "body_message":  ("energy_drain",  "health_code"),
    "stress_number": ("energy_drain",  "body_message"),
    "intuition":     ("health_code",   "past_life"),
    "past_life":     ("ancestor_code", "karma"),
    "future_portal": ("turning_point", "forecast_2026"),
    "turning_point": ("future_portal", "past_life"),
    "ancestor_code": ("past_life",     "karma"),
}

# Разделы меню
SECTION_DESTINY = ["matrix_full", "mission", "hidden_talents", "strong_weak", "main_fear", "karma", "forecast_2026"]
SECTION_MONEY   = ["finance", "wealth_blocks", "freedom_path", "calling", "promotion", "own_business", "career", "money", "days"]
SECTION_LOVE    = ["compat", "when", "portrait", "unlucky", "ex", "cold", "toxic", "lonely", "breakup"]
SECTION_HEALTH  = ["health_code", "energy_drain", "body_message", "stress_number", "intuition"]
SECTION_PAST    = ["past_life", "future_portal", "turning_point", "ancestor_code"]

PAID_RAZBORY = {k: v for k, v in TITLES.items() if k != "free"}

# Разборы которые могут быть бесплатными (до 99⭐ включительно)
FREE_ELIGIBLE = {k for k, v in PRICES.items() if v <= 99}

# ─── DB ──────────────────────────────────────────────────────────────────────
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id            BIGINT PRIMARY KEY,
            first_name         TEXT,
            free_used          BOOLEAN DEFAULT FALSE,
            subscribed_channel BOOLEAN DEFAULT FALSE,
            birth_date         TEXT,
            destiny_number     INTEGER,
            purchased          TEXT DEFAULT '[]',
            waiting            TEXT,
            review_left        BOOLEAN DEFAULT FALSE,
            notifications      BOOLEAN DEFAULT TRUE,
            reviews_left       TEXT DEFAULT '[]'
        )
    ''')
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS coupons (
            code       TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT NOW(),
            expires_at TIMESTAMP,
            max_uses   INTEGER DEFAULT 1,
            uses_count INTEGER DEFAULT 0
        )
    ''')
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS coupon_uses (
            id         SERIAL PRIMARY KEY,
            code       TEXT NOT NULL,
            user_id    BIGINT NOT NULL,
            used_at    TIMESTAMP DEFAULT NOW()
        )
    ''')
    for col, definition in [
        ("first_name",    "TEXT"),
        ("notifications", "BOOLEAN DEFAULT TRUE"),
        ("reviews_left",  "TEXT DEFAULT '[]'"),
    ]:
        try:
            await db_pool.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {definition}")
        except Exception:
            pass
    for col, definition in [
        ("max_uses",   "INTEGER DEFAULT 1"),
        ("uses_count", "INTEGER DEFAULT 0"),
    ]:
        try:
            await db_pool.execute(f"ALTER TABLE coupons ADD COLUMN IF NOT EXISTS {col} {definition}")
        except Exception:
            pass
    try:
        await db_pool.execute("ALTER TABLE coupons DROP COLUMN IF EXISTS used_by")
    except Exception:
        pass

async def get_user(user_id: int) -> dict:
    row = await db_pool.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
    if not row:
        await db_pool.execute('INSERT INTO users (user_id) VALUES ($1)', user_id)
        row = await db_pool.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
    user = dict(row)
    user['purchased']    = json.loads(user['purchased'] or '[]')
    user['reviews_left'] = json.loads(user.get('reviews_left') or '[]')
    if user.get('notifications') is None:
        user['notifications'] = True
    if user_id == ADMIN_ID:
        user['free_used']          = True
        user['subscribed_channel'] = True
        for r in list(PAID_RAZBORY.keys()):
            if r not in user['purchased']:
                user['purchased'].append(r)
    return user

async def save_user(user_id: int, user: dict):
    await db_pool.execute('''
        UPDATE users SET
            first_name         = $1,
            free_used          = $2,
            subscribed_channel = $3,
            birth_date          = $4,
            destiny_number      = $5,
            purchased           = $6,
            waiting             = $7,
            review_left         = $8,
            notifications       = $9,
            reviews_left        = $10
        WHERE user_id = $11
    ''',
        user.get('first_name'),
        user['free_used'],
        user['subscribed_channel'],
        user.get('birth_date'),
        user.get('destiny_number'),
        json.dumps(user['purchased']),
        user.get('waiting'),
        user.get('review_left', False),
        user.get('notifications', True),
        json.dumps(user.get('reviews_left', [])),
        user_id
    )

# ─── FSM ─────────────────────────────────────────────────────────────────────
class Form(StatesGroup):
    waiting_name        = State()
    waiting_birth_date  = State()
    waiting_date        = State()
    waiting_second_date = State()
    waiting_review      = State()
    waiting_free_date   = State()

# ─── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────────────────────────────────────
def is_valid_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
        return True
    except ValueError:
        return False

async def check_subscription(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ─── ИИ-ПРОВАЙДЕРЫ: CEREBRAS → GROQ → OPENROUTER ───────────────────────────
# Cerebras — быстрый, бесплатный тир, отлично держит русский язык
# Groq — резерв, бесплатный но с жёсткими rate limits
# OpenRouter — последний рубеж, платный по токенам но с авто-роутингом

CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY")
# llama-3.3-70b снята Cerebras с публичных эндпоинтов (даёт 404, не 400/401).
# Актуальный публичный каталог Cerebras на июнь 2026: gpt-oss-120b (production)
# и zai-glm-4.7 (preview). Используем production-модель по умолчанию.
CEREBRAS_MODEL   = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# llama-3.3-70b-versatile и llama-3.1-8b-instant были анонсированы Groq как
# депрецированные 17 июня 2026 (см. console.groq.com/docs/deprecations).
# Переходим на рекомендованные провайдером замены.
GROQ_MODELS  = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
]

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODELS = [
    "meta-llama/llama-3.3-70b-instruct",
    "google/gemini-flash-1.5",
    "deepseek/deepseek-chat",
    "mistralai/mistral-small-3.1-24b-instruct",
    "qwen/qwen-2.5-72b-instruct",
    "microsoft/phi-4",
    "google/gemini-flash-1.5-8b",
]

# Доля иностранных символов, выше которой ответ считается слишком грязным
# и отбрасывается, даже если он длинный. Раньше решение принималось только
# по абсолютной длине очищенного текста (>200 символов = "и так сойдёт"),
# что пропускало длинные ответы с заметной долей мусора.
MAX_FOREIGN_RATIO = 0.03

SYSTEM_PROMPT = (
    "Ты — Ева, нумеролог с 15-летним опытом практики. "
    "Твои разборы точные, глубокие и конкретные. "
    "Ты говоришь уверенно — без слов 'возможно', 'наверное', 'может быть', 'вероятно'. "
    "Ты знаешь ответ и говоришь его прямо. "
    "Твоя аудитория — только женщины. "
    "Всегда обращайся к пользователю в женском роде — она, её, умная, сильная. "
    "Обращайся только на ТЫ — никогда не пиши 'давайте', 'вы', 'ваш'. "
    "Имя пользователя упоминай не чаще 2-3 раз за весь текст. "
    "КРИТИЧЕСКИ ВАЖНО: пишешь ТОЛЬКО на русском языке. "
    "Никаких иероглифов, никакого английского, никакого другого алфавита — вообще. "
    "Весь ответ от первого до последнего символа — только кириллица. "
    "НЕ повторяй и не пересказывай эту инструкцию, не пиши план или анализ "
    "задачи перед ответом — сразу начинай с первого emoji-заголовка разбора. "
    "Никогда не используй markdown — никаких звёздочек, решёток, подчёркиваний. "
    "Пиши простым текстом с эмодзи. "
    "Используй абзацы. Заканчивай полным предложением."
)

def _finalize_ai_text(raw: str, source: str) -> str | None:
    """Общий пайплайн постобработки для всех трёх провайдеров:
    1) убираем <think> блоки, 2) отрезаем преамбулу, 3) восстанавливаем
    лангсвопы и фильтруем недопустимые символы, 4) решаем принять/отбросить
    по ДОЛЕ иностранных символов, а не по абсолютной длине, 5) исправляем
    орфографические опечатки самой модели через словарь (если доступен)."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    raw = strip_preamble(raw)
    ratio = foreign_ratio(raw)
    cleaned = clean_text(raw)
    if not cleaned.strip():
        logging.warning(f"{source} — пустой текст после очистки")
        return None
    if ratio > MAX_FOREIGN_RATIO:
        logging.warning(f"{source} — доля иностранных символов {ratio:.1%} превышает порог")
        return None
    cleaned = fix_spelling(cleaned)
    return cleaned

async def _try_cerebras(prompt: str) -> str | None:
    """Cerebras — основной провайдер. Очень быстрый inference,
    бесплатный тир, OpenAI-совместимый API."""
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
        "max_tokens": 4000,
        "temperature": 0.7,
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=data, timeout=45)
            if response.status_code == 429:
                logging.warning("Cerebras 429 rate limit")
                return None
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
            result = _finalize_ai_text(raw, "Cerebras")
            if result:
                logging.info("Cerebras ответил успешно")
            return result
    except Exception as e:
        logging.warning(f"Cerebras failed: {e}")
        return None

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
                    "max_tokens": 4000,
                }
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, headers=headers, json=data, timeout=45)
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("retry-after", 5))
                        logging.warning(f"Groq {model} 429, retry-after={retry_after}s")
                        await asyncio.sleep(min(retry_after, 10))
                        break
                    if response.status_code == 400:
                        logging.warning(f"Groq {model} 400: {response.text[:300]}")
                        break
                    response.raise_for_status()
                    raw    = response.json()["choices"][0]["message"]["content"]
                    result = _finalize_ai_text(raw, f"Groq {model}")
                    if result is None:
                        continue
                    logging.info(f"Groq {model} ответил успешно")
                    return result
            except Exception as e:
                logging.warning(f"Groq {model} attempt {attempt+1} failed: {e}")
                break
    return None

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
        "max_tokens": 4000,
        "temperature": 0.7,
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=data, timeout=60)
            if response.status_code == 429:
                logging.warning("OpenRouter 429 rate limit")
                return None
            response.raise_for_status()
            body       = response.json()  # один раз — раньше .json() вызывался дважды
            raw        = body["choices"][0]["message"]["content"]
            model_used = body.get("model", "unknown")
            result     = _finalize_ai_text(raw, "OpenRouter")
            if result:
                logging.info(f"OpenRouter ответил успешно (модель: {model_used})")
            return result
    except Exception as e:
        logging.warning(f"OpenRouter failed: {e}")
        return None

async def ask_ai(prompt: str) -> str:
    """Cerebras → Groq → OpenRouter."""
    result = await _try_cerebras(prompt)
    if result:
        return result
    logging.warning("Cerebras не дал результат — пробую Groq")
    result = await _try_groq(prompt)
    if result:
        return result
    logging.warning("Groq не дал результат — пробую OpenRouter")
    result = await _try_openrouter(prompt)
    if result:
        return result
    raise Exception("Все провайдеры недоступны или вернули иностранные символы")

def build_prompt(key: str, **kwargs) -> str:
    """Собирает промпт по ключу. Бросает ValueError если ключ не найден —
    раньше отсутствующий ключ тихо давал пустой промпт и мусорный ответ AI."""
    kwargs.setdefault("year", datetime.now().year)
    template = PROMPTS.get(key)
    if not template:
        logging.error(f"build_prompt: промпт не найден для ключа '{key}'")
        raise ValueError(f"Промпт '{key}' не существует в PROMPTS")
    return template.format(**kwargs)

# ─── КЛАВИАТУРЫ ──────────────────────────────────────────────────────────────
def check_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я подписалась!", callback_data="check_sub")],
    ])

def date_choice_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Для себя",    callback_data="use_my_date")],
        [InlineKeyboardButton(text="📅 Другая дата", callback_data="use_new_date")],
    ])

def notifications_menu(notifications_on: bool) -> InlineKeyboardMarkup:
    if notifications_on:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔕 Отключить уведомления", callback_data="notif_off")],
            [InlineKeyboardButton(text="🔮 Меню разборов", callback_data="show_menu")],
        ])
    else:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Включить уведомления", callback_data="notif_on")],
            [InlineKeyboardButton(text="🔮 Меню разборов", callback_data="show_menu")],
        ])

def main_menu(user=None) -> InlineKeyboardMarkup:
    buttons = []

    if user and not user.get("free_used"):
        buttons.append([InlineKeyboardButton(
            text="🎁 Бесплатный разбор на выбор",
            callback_data="free_choose"
        )])

    purchased = user.get("purchased", []) if user else []
    if purchased:
        count = len(purchased)
        buttons.append([InlineKeyboardButton(
            text=f"📚 Мои разборы ({count})",
            callback_data="my_readings"
        )])

    buttons.append([InlineKeyboardButton(text="── Выбери тему ──", callback_data="noop")])
    buttons.append([InlineKeyboardButton(text="🔮 Судьба и личность",    callback_data="section_destiny")])
    buttons.append([InlineKeyboardButton(text="💰 Деньги и карьера",     callback_data="section_money")])
    buttons.append([InlineKeyboardButton(text="💑 Любовь и отношения",   callback_data="section_love")])
    buttons.append([InlineKeyboardButton(text="🌙 Здоровье и энергия",   callback_data="section_health")])
    buttons.append([InlineKeyboardButton(text="✨ Прошлое и будущее",    callback_data="section_past")])
    buttons.append([InlineKeyboardButton(
        text="🌸 Личный разбор от Евы (за рубли)",
        url=CONTACT_URL
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def free_choose_menu() -> InlineKeyboardMarkup:
    buttons = []
    for key in sorted(FREE_ELIGIBLE):
        title = TITLES.get(key, key)
        price = PRICES.get(key, 0)
        buttons.append([InlineKeyboardButton(
            text=f"{title} ({price} ⭐ — бесплатно)",
            callback_data=f"free_pick_{key}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def section_destiny_menu(user=None) -> InlineKeyboardMarkup:
    purchased = user.get("purchased", []) if user else []
    buttons = []
    items = [
        ("matrix_full",   "🔮 Матрица судьбы — 149 ⭐"),
        ("mission",       "🌟 Предназначение и миссия — 99 ⭐"),
        ("hidden_talents","✨ Скрытые таланты — 79 ⭐"),
        ("strong_weak",   "⚖️ Сильная/слабая сторона — 49 ⭐"),
        ("main_fear",     "😨 Главный страх — 49 ⭐"),
        ("karma",         "🔴 Кармический долг — 99 ⭐"),
        ("forecast_2026", "🗓 Прогноз на 2026 год — 149 ⭐"),
    ]
    for key, label in items:
        prefix = "✅ " if key in purchased else ""
        buttons.append([InlineKeyboardButton(text=prefix + label, callback_data=f"buy_{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def section_money_menu(user=None) -> InlineKeyboardMarkup:
    purchased = user.get("purchased", []) if user else []
    buttons = []
    items = [
        ("finance",       "💹 Финансовый прогноз — 99 ⭐"),
        ("wealth_blocks", "🚧 Блоки богатства — 149 ⭐"),
        ("freedom_path",  "🗺 Путь к финансовой свободе — 149 ⭐"),
        ("calling",       "🌠 Призвание — 79 ⭐"),
        ("promotion",     "📈 Повышение — 99 ⭐"),
        ("own_business",  "🏢 Свой бизнес — 99 ⭐"),
        ("career",        "💼 Карьерный путь — 79 ⭐"),
        ("money",         "💰 Денежный код — 79 ⭐"),
        ("days",          "🌙 Сильные и слабые дни — 79 ⭐"),
    ]
    for key, label in items:
        prefix = "✅ " if key in purchased else ""
        buttons.append([InlineKeyboardButton(text=prefix + label, callback_data=f"buy_{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def section_love_menu(user=None) -> InlineKeyboardMarkup:
    purchased = user.get("purchased", []) if user else []
    buttons = []
    items = [
        ("compat",   "💑 Совместимость двух людей — 99 ⭐"),
        ("when",     "💘 Когда встретишь того самого — 79 ⭐"),
        ("portrait", "💍 Портрет идеального партнёра — 79 ⭐"),
        ("unlucky",  "💔 Почему не везёт в любви — 49 ⭐"),
        ("ex",       "💔 Вернётся ли бывший — 49 ⭐"),
        ("cold",     "❄️ Почему он охладел — 49 ⭐"),
        ("toxic",    "☠️ Токсичная связь — 79 ⭐"),
        ("lonely",   "😔 Почему ты одинока — 49 ⭐"),
        ("breakup",  "💔 Разбор после расставания — 79 ⭐"),
    ]
    for key, label in items:
        prefix = "✅ " if key in purchased else ""
        buttons.append([InlineKeyboardButton(text=prefix + label, callback_data=f"buy_{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def section_health_menu(user=None) -> InlineKeyboardMarkup:
    purchased = user.get("purchased", []) if user else []
    buttons = []
    items = [
        ("health_code",   "💚 Код здоровья — 79 ⭐"),
        ("energy_drain",  "⚡ Что крадёт энергию — 49 ⭐"),
        ("body_message",  "🫀 Послания тела — 49 ⭐"),
        ("stress_number", "😤 Число стресса — 49 ⭐"),
        ("intuition",     "🔮 Интуиция и внутренний голос — 79 ⭐"),
    ]
    for key, label in items:
        prefix = "✅ " if key in purchased else ""
        buttons.append([InlineKeyboardButton(text=prefix + label, callback_data=f"buy_{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def section_past_menu(user=None) -> InlineKeyboardMarkup:
    purchased = user.get("purchased", []) if user else []
    buttons = []
    items = [
        ("past_life",     "📜 Прошлые жизни — 99 ⭐"),
        ("future_portal", "🌟 Прогноз на 3 года — 149 ⭐"),
        ("turning_point", "🔄 Поворотные точки судьбы — 79 ⭐"),
        ("ancestor_code", "🌳 Родовой код — 99 ⭐"),
    ]
    for key, label in items:
        prefix = "✅ " if key in purchased else ""
        buttons.append([InlineKeyboardButton(text=prefix + label, callback_data=f"buy_{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def my_readings_menu(user: dict) -> InlineKeyboardMarkup:
    purchased = user.get("purchased", [])
    buttons   = []
    for key in purchased:
        title = TITLES.get(key, key)
        buttons.append([InlineKeyboardButton(
            text=f"✅ {title}",
            callback_data=f"buy_{key}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def upsell_menu(key: str, user: dict) -> InlineKeyboardMarkup:
    buttons     = []
    suggestions = UPSELLS.get(key, ())
    for s in suggestions:
        if s not in user.get("purchased", []):
            title = TITLES.get(s, s)
            price = PRICES.get(s, 49)
            buttons.append([InlineKeyboardButton(
                text=f"{title} — {price} ⭐",
                callback_data=f"buy_{s}"
            )])
    reviews_left = user.get("reviews_left", [])
    if key not in reviews_left:
        buttons.append([InlineKeyboardButton(
            text="😍 Оставить отзыв",
            callback_data=f"leave_review_{key}"
        )])
    buttons.append([InlineKeyboardButton(text="🔮 Все разборы", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def retry_menu(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Попробовать ещё раз", callback_data=f"buy_{key}")],
        [InlineKeyboardButton(text="🔮 Меню", callback_data="show_menu")],
    ])

def coupon_razboy_menu(code: str, user: dict = None) -> InlineKeyboardMarkup:
    """Код промокода зашит прямо в callback_data каждой кнопки
    (coupon::КОД::ключ) — списание происходит при реальном выборе разбора."""
    purchased = user.get("purchased", []) if user else []
    buttons   = []
    for key, title in PAID_RAZBORY.items():
        prefix = "✅ " if key in purchased else ""
        buttons.append([InlineKeyboardButton(
            text=prefix + title,
            callback_data=f"coupon::{code}::{key}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def notif_off_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔕 Отключить уведомления", callback_data="notif_off")],
        [InlineKeyboardButton(text="🔮 Меню разборов",          callback_data="show_menu")],
    ])

# ─── КУПОНЫ ──────────────────────────────────────────────────────────────────
async def create_coupon(code: str, max_uses: int = 1) -> str:
    """Возвращает: 'ok', 'exists', 'error'"""
    expires = utc_now() + timedelta(hours=48)
    try:
        await db_pool.execute(
            'INSERT INTO coupons (code, expires_at, max_uses) VALUES ($1, $2, $3)',
            code.upper(), expires, max_uses
        )
        return 'ok'
    except asyncpg.UniqueViolationError:
        return 'exists'
    except Exception as e:
        logging.error(f"create_coupon error: {e}", exc_info=True)
        return 'error'

async def use_coupon(code: str, user_id: int) -> str:
    row = await db_pool.fetchrow('SELECT * FROM coupons WHERE code = $1', code.upper())
    if not row:
        return 'not_found'
    if row['expires_at'] and row['expires_at'] < utc_now():
        return 'expired'
    if row['uses_count'] >= row['max_uses']:
        return 'limit'
    updated = await db_pool.fetchval(
        '''UPDATE coupons SET uses_count = uses_count + 1
           WHERE code = $1 AND uses_count < max_uses
           RETURNING uses_count''',
        code.upper()
    )
    if updated is None:
        return 'limit'
    await db_pool.execute(
        'INSERT INTO coupon_uses (code, user_id) VALUES ($1, $2)',
        code.upper(), user_id
    )
    return 'ok'

async def coupon_remaining(code: str) -> int:
    row = await db_pool.fetchrow('SELECT max_uses, uses_count FROM coupons WHERE code = $1', code.upper())
    if not row:
        return 0
    return max(0, row['max_uses'] - row['uses_count'])

# ─── УВЕДОМЛЕНИЯ ─────────────────────────────────────────────────────────────
@dp.message(Command("notifications"), StateFilter("*"))
async def notifications_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    notif_on = user.get("notifications", True)
    status = "включены 🔔" if notif_on else "отключены 🔕"
    await message.answer(
        f"Утренние уведомления сейчас {status}.\n\nУправляй настройкой 👇",
        reply_markup=notifications_menu(notif_on)
    )

@dp.callback_query(F.data == "notif_off")
async def notif_off(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    user["notifications"] = False
    await save_user(callback.from_user.id, user)
    await callback.answer("🔕 Уведомления отключены", show_alert=True)
    await callback.message.answer(
        "🔕 Утренние уведомления отключены.\n\nВключить обратно: /notifications",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔮 Меню разборов", callback_data="show_menu")]
        ])
    )

@dp.callback_query(F.data == "notif_on")
async def notif_on(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    user["notifications"] = True
    await save_user(callback.from_user.id, user)
    await callback.answer("🔔 Уведомления включены!", show_alert=True)
    await callback.message.answer(
        "🔔 Утренние уведомления включены!\n\nКаждое утро буду присылать нумерологический прогноз 🌅",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔮 Меню разборов", callback_data="show_menu")]
        ])
    )

# ─── РАЗДЕЛЫ МЕНЮ ────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "section_destiny")
async def section_destiny(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.message.answer("🔮 Судьба и личность — выбери разбор:", reply_markup=section_destiny_menu(user))
    await callback.answer()

@dp.callback_query(F.data == "section_money")
async def section_money(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.message.answer("💰 Деньги и карьера — выбери разбор:", reply_markup=section_money_menu(user))
    await callback.answer()

@dp.callback_query(F.data == "section_love")
async def section_love(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.message.answer("💑 Любовь и отношения — выбери разбор:", reply_markup=section_love_menu(user))
    await callback.answer()

@dp.callback_query(F.data == "section_health")
async def section_health(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.message.answer("🌙 Здоровье и энергия — выбери разбор:", reply_markup=section_health_menu(user))
    await callback.answer()

@dp.callback_query(F.data == "section_past")
async def section_past(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.message.answer("✨ Прошлое и будущее — выбери разбор:", reply_markup=section_past_menu(user))
    await callback.answer()

# ─── МОИ РАЗБОРЫ ─────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "my_readings")
async def my_readings(callback: CallbackQuery):
    user      = await get_user(callback.from_user.id)
    purchased = user.get("purchased", [])
    if not purchased:
        await callback.answer("У тебя пока нет купленных разборов 🔮", show_alert=True)
        return
    await callback.message.answer(
        f"📚 Твои разборы ({len(purchased)}) — нажми на любой чтобы получить снова 👇",
        reply_markup=my_readings_menu(user)
    )
    await callback.answer()

# ─── БЕСПЛАТНЫЙ РАЗБОР НА ВЫБОР ─────────────────────────────────────────────
@dp.callback_query(F.data == "free_choose")
async def free_choose_handler(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if not user["subscribed_channel"]:
        is_sub = await check_subscription(callback.from_user.id)
        if not is_sub:
            await callback.message.answer(
                f"💫 Подпишись на {CHANNEL} чтобы получить бесплатный разбор 👇",
                reply_markup=check_menu()
            )
            await callback.answer()
            return
        user["subscribed_channel"] = True
        await save_user(callback.from_user.id, user)

    if user["free_used"]:
        await callback.answer("Бесплатный разбор уже использован 🔮", show_alert=True)
        return

    if not user.get("first_name"):
        await callback.message.answer("✨ Как мне тебя называть? Введи своё имя 👇")
        await state.set_state(Form.waiting_name)
        await callback.answer()
        return

    await callback.message.answer(
        "🎁 Выбери любой разбор — он будет бесплатным!\n\n"
        "Это твой подарок за подписку на канал 💫",
        reply_markup=free_choose_menu()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("free_pick_"))
async def free_pick_handler(callback: CallbackQuery, state: FSMContext):
    key  = callback.data.replace("free_pick_", "")
    user = await get_user(callback.from_user.id)

    if user["free_used"]:
        await callback.answer("Бесплатный разбор уже использован!", show_alert=True)
        return

    if key not in FREE_ELIGIBLE:
        await callback.answer("Этот разбор не входит в бесплатные!", show_alert=True)
        return

    user["waiting"] = key
    await save_user(callback.from_user.id, user)
    await callback.answer()

    if key == "compat":
        await callback.message.answer(
            "💑 Введи две даты через запятую:\nНапример: 15.03.1995, 22.07.1998"
        )
        await state.set_state(Form.waiting_second_date)
    else:
        await _ask_date(callback.message, user)
        await state.set_state(Form.waiting_free_date)

@dp.message(StateFilter(Form.waiting_free_date))
async def handle_free_date(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    text = message.text.strip()
    if not is_valid_date(text):
        await message.answer("❌ Неверная дата. Введи в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        return
    user["free_used"] = True
    await save_user(message.from_user.id, user)
    await _process_date(message, message.from_user.id, user, text, state, is_free=True)

# ─── ОНБОРДИНГ ───────────────────────────────────────────────────────────────
@dp.message(Command("start"), StateFilter("*"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    if not user.get("first_name") and message.from_user.first_name:
        user["first_name"] = message.from_user.first_name
        await save_user(message.from_user.id, user)
    if not user["subscribed_channel"]:
        is_sub = await check_subscription(message.from_user.id)
        if is_sub:
            user["subscribed_channel"] = True
            await save_user(message.from_user.id, user)
    if not user["subscribed_channel"]:
        await message.answer(
            "🔮 Привет! Я Ева — твой личный нумеролог.\n\n"
            "✨ Что я умею:\n\n"
            "• Бесплатный разбор на выбор (любой до 99 ⭐)\n"
            "• Полная матрица судьбы и кармический долг\n"
            "• Финансовый прогноз и блоки богатства\n"
            "• Путь к своему делу и призванию\n"
            "• Совместимость, любовь, отношения\n"
            "• Здоровье, энергия и интуиция\n"
            "• Прошлые жизни, родовой код, прогноз на 3 года\n\n"
            "Всё это по твоей дате рождения — точно и личностно 🌸\n\n"
            f"Подпишись на {CHANNEL} и получи бесплатный разбор на выбор 👇",
            reply_markup=check_menu()
        )
        return
    if not user["free_used"]:
        name     = user.get("first_name") or ""
        greeting = f"✨ Привет, {name}! " if name else "✨ Привет! "
        await message.answer(
            greeting + "Давай познакомимся.\n\nКак мне тебя называть? Введи своё имя 👇"
        )
        await state.set_state(Form.waiting_name)
        return
    await message.answer("🔮 Выбери свой разбор 👇", reply_markup=main_menu(user))

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery, state: FSMContext):
    user   = await get_user(callback.from_user.id)
    is_sub = await check_subscription(callback.from_user.id)
    if not is_sub:
        await callback.answer("❌ Ты ещё не подписалась!", show_alert=True)
        return
    user["subscribed_channel"] = True
    await save_user(callback.from_user.id, user)
    await callback.answer()
    if user["free_used"]:
        await callback.message.answer("✅ Подписка подтверждена!", reply_markup=main_menu(user))
        return
    await callback.message.answer("✅ Отлично! Как мне тебя называть? Введи своё имя 👇")
    await state.set_state(Form.waiting_name)

@dp.message(StateFilter(Form.waiting_name))
async def handle_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2 or len(name) > 30:
        await message.answer("Введи настоящее имя (от 2 до 30 символов) 😊")
        return
    user = await get_user(message.from_user.id)
    user["first_name"] = name
    await save_user(message.from_user.id, user)

    if not user.get("free_used"):
        await message.answer(
            f"Приятно познакомиться, {name}! 🌸\n\n"
            "🎁 Выбери любой разбор — он будет бесплатным!\n\n"
            "Это твой подарок за подписку на канал 💫",
            reply_markup=free_choose_menu()
        )
        return

    await message.answer(
        f"Приятно познакомиться, {name}! 🌸\n\n"
        "Введи свою дату рождения в формате ДД.ММ.ГГГГ\n"
        "Например: 15.03.1995"
    )
    await state.set_state(Form.waiting_birth_date)

@dp.message(StateFilter(Form.waiting_birth_date))
async def handle_birth_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if not is_valid_date(text):
        await message.answer("❌ Неверная дата. Введи в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        return
    user   = await get_user(message.from_user.id)
    number = calculate_destiny(text)
    user["birth_date"]     = text
    user["destiny_number"] = number
    user["free_used"]      = True
    user["waiting"]        = None
    await save_user(message.from_user.id, user)
    name = user.get("first_name") or "дорогая"
    await message.answer(f"⏳ Составляю твой разбор, {name}... Подожди немного ✨")
    try:
        template = MATRIX_LITE.get(number, MATRIX_LITE.get(9, ""))
        answer   = template.format(name=name)
        await send_long(message.chat.id, f"💫 Матрица судьбы\nЧисло судьбы: {number}\n\n{answer}")
        await message.answer(
            "✨ Это был бесплатный разбор!\n\nВыбери полный разбор и узнай всё о своей судьбе 🔮",
            reply_markup=main_menu(user)
        )
    except Exception as e:
        logging.error(f"Onboarding error: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка, попробуй ещё раз /start")
    await state.clear()

# ─── КОМАНДЫ ─────────────────────────────────────────────────────────────────
@dp.message(Command("menu"), StateFilter("*"))
async def menu_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    await message.answer("🔮 Выбери разбор:", reply_markup=main_menu(user))

@dp.message(Command("promo"), StateFilter("*"))
async def promo_cmd(message: Message, state: FSMContext):
    """Списание происходит при выборе конкретного разбора (coupon_razboy_handler),
    не здесь — здесь только проверка что код существует, не истёк, не исчерпан."""
    await state.clear()
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Введи промокод так: /promo КОД")
        return
    code = parts[1].upper()
    row  = await db_pool.fetchrow('SELECT * FROM coupons WHERE code = $1', code)
    if not row:
        await message.answer("❌ Такого промокода не существует.")
        return
    if row['expires_at'] and row['expires_at'] < utc_now():
        await message.answer("❌ Этот промокод уже истёк.")
        return
    remaining = row['max_uses'] - row['uses_count']
    if remaining <= 0:
        await message.answer("❌ Этот промокод исчерпан — все использования закончились.")
        return
    user = await get_user(message.from_user.id)
    await message.answer(
        f"🎁 Промокод активирован! Доступно бесплатных разборов: {remaining}.\n\n"
        "Выбирай из списка — после каждого выбора будет списываться одно "
        "использование промокода 👇",
        reply_markup=coupon_razboy_menu(code, user)
    )

@dp.message(Command("coupon"), StateFilter("*"))
async def coupon_cmd(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer(
            "Использование:\n"
            "/coupon КОД — создать на 1 использование\n"
            "/coupon КОД 20 — создать на 20 использований\n\n"
            "Один код можно вводить /promo несколько раз — каждый выбранный "
            "разбор спишет одно использование, пока не закончится лимит.\n\n"
            "Пример: /coupon FRIEND20 20"
        )
        return
    code     = parts[1].upper()
    max_uses = 1
    if len(parts) >= 3:
        try:
            max_uses = max(1, int(parts[2]))
        except ValueError:
            await message.answer("❌ Число использований должно быть целым числом.\nПример: /coupon FRIEND20 20")
            return
    result = await create_coupon(code, max_uses)
    if result == 'ok':
        expires  = (utc_now() + timedelta(hours=48)).strftime("%d.%m.%Y %H:%M")
        uses_str = f"{max_uses} раз" if max_uses > 1 else "1 раз"
        await message.answer(
            f"✅ Промокод создан!\n\n"
            f"Код: <code>{code}</code>\n"
            f"Лимит использований: {uses_str}\n"
            f"Действует до: {expires}\n\n"
            f"Юзер вводит: /promo {code} — и выбирает разборы из списка, "
            f"пока не закончится лимит.",
            parse_mode="HTML"
        )
    elif result == 'exists':
        await message.answer("❌ Такой промокод уже существует.")
    else:
        await message.answer("❌ Ошибка создания промокода — проверь логи Railway.")

@dp.message(Command("coupon_stat"), StateFilter("*"))
async def coupon_stat_cmd(message: Message, state: FSMContext):
    """Статистика по купону: /coupon_stat КОД"""
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Использование: /coupon_stat КОД")
        return
    code = parts[1].upper()
    row  = await db_pool.fetchrow('SELECT * FROM coupons WHERE code = $1', code)
    if not row:
        await message.answer(f"❌ Промокод {code} не найден.")
        return
    uses = await db_pool.fetch(
        'SELECT user_id, used_at FROM coupon_uses WHERE code = $1 ORDER BY used_at DESC LIMIT 20',
        code
    )
    expires_str = row['expires_at'].strftime("%d.%m.%Y %H:%M") if row['expires_at'] else "бессрочно"
    lines = [
        f"📊 Промокод: <code>{code}</code>",
        f"Использований: {row['uses_count']} / {row['max_uses']}",
        f"Действует до: {expires_str}",
    ]
    if uses:
        lines.append("\nПоследние активации:")
        for u in uses:
            dt = u['used_at'].strftime("%d.%m %H:%M")
            lines.append(f"  • user_id {u['user_id']} — {dt}")
    await message.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("admin"), StateFilter("*"))
async def admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    total         = await db_pool.fetchval('SELECT COUNT(*) FROM users')
    free_used     = await db_pool.fetchval('SELECT COUNT(*) FROM users WHERE free_used = TRUE')
    reviews       = await db_pool.fetchval("SELECT COUNT(*) FROM users WHERE reviews_left != '[]'")
    notif_on      = await db_pool.fetchval('SELECT COUNT(*) FROM users WHERE notifications = TRUE')
    coupons_total = await db_pool.fetchval('SELECT COUNT(*) FROM coupons')
    coupons_used  = await db_pool.fetchval('SELECT COUNT(*) FROM coupon_uses')
    rows = await db_pool.fetch('SELECT purchased FROM users WHERE user_id != $1', ADMIN_ID)
    total_purch = 0
    razbory_cnt = {}
    bought      = 0
    stars_total = 0
    for row in rows:
        p = json.loads(row['purchased'])
        if p:
            bought      += 1
            total_purch += len(p)
            for r in p:
                razbory_cnt[r] = razbory_cnt.get(r, 0) + 1
                stars_total   += PRICES.get(r, 49)
    top      = sorted(razbory_cnt.items(), key=lambda x: x[1], reverse=True)
    top_text = "\n".join([f"  {TITLES.get(k,k)}: {v}" for k, v in top[:5]]) if top else "  нет"
    await message.answer(
        f"📊 Статистика бота Ева\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"💫 Прошли онбординг: {free_used}\n"
        f"💳 Купили хотя бы раз: {bought}\n"
        f"🛒 Всего покупок: {total_purch}\n"
        f"⭐ Примерная выручка: ~{stars_total} Stars\n"
        f"🔔 Уведомления включены: {notif_on}\n"
        f"🎟 Купонов: создано {coupons_total} / активаций {coupons_used}\n"
        f"📝 Оставили отзывы: {reviews}\n\n"
        f"🏆 Топ разборов:\n{top_text}"
    )

# ─── КУПОН — ВЫБОР РАЗБОРА ───────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("coupon::"))
async def coupon_razboy_handler(callback: CallbackQuery, state: FSMContext):
    """Код промокода приходит прямо в callback_data (coupon::КОД::ключ), и
    списание use_coupon() происходит ИМЕННО ЗДЕСЬ, в момент выбора
    конкретного разбора — а не при вводе /promo."""
    try:
        _, code, key = callback.data.split("::", 2)
    except ValueError:
        await callback.answer("Ошибка промокода.", show_alert=True)
        return

    user = await get_user(callback.from_user.id)

    if key in user["purchased"]:
        user["waiting"] = key
        await save_user(callback.from_user.id, user)
        await callback.answer("Этот разбор уже у тебя — пришлю заново 🔮")
    else:
        result = await use_coupon(code, callback.from_user.id)
        if result == 'not_found':
            await callback.answer("❌ Промокод не найден.", show_alert=True)
            return
        if result == 'expired':
            await callback.answer("❌ Промокод истёк.", show_alert=True)
            return
        if result == 'limit':
            await callback.answer("❌ Лимит этого промокода исчерпан.", show_alert=True)
            return
        user["purchased"].append(key)
        user["waiting"] = key
        await save_user(callback.from_user.id, user)
        remaining = await coupon_remaining(code)
        await callback.answer(f"✅ Добавлено! Осталось использований промокода: {remaining}")

    if key == "compat":
        await callback.message.answer(
            "💑 Введи две даты через запятую:\nНапример: 15.03.1995, 22.07.1998"
        )
        await state.set_state(Form.waiting_second_date)
    else:
        await _ask_date(callback.message, user)
        await state.set_state(Form.waiting_date)

# ─── УМНАЯ ДАТА ──────────────────────────────────────────────────────────────
async def _ask_date(message: Message, user: dict):
    if user.get("birth_date"):
        await message.answer(
            f"Делаешь разбор для себя ({user['birth_date']}) или введёшь другую дату?",
            reply_markup=date_choice_menu()
        )
    else:
        await message.answer("📅 Введи дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")

@dp.callback_query(F.data == "use_my_date")
async def use_my_date(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    await callback.answer()
    await _process_date(callback.message, callback.from_user.id, user, user["birth_date"], state)

@dp.callback_query(F.data == "use_new_date")
async def use_new_date(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("📅 Введи дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
    await state.set_state(Form.waiting_date)

# ─── ПОКУПКИ ─────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "noop")
async def noop_handler(callback: CallbackQuery):
    await callback.answer()

@dp.callback_query(F.data == "free")
async def free_handler(callback: CallbackQuery, state: FSMContext):
    await free_choose_handler(callback, state)

async def send_invoice(chat_id, title, description, payload, amount):
    await bot.send_invoice(
        chat_id=chat_id, title=title, description=description,
        payload=payload, currency="XTR",
        prices=[LabeledPrice(label=title, amount=amount)],
    )

@dp.callback_query(F.data.startswith("buy_"))
async def buy_handler(callback: CallbackQuery, state: FSMContext):
    key  = callback.data.replace("buy_", "")
    user = await get_user(callback.from_user.id)
    if key in user["purchased"]:
        user["waiting"] = key
        await save_user(callback.from_user.id, user)
        await callback.answer()
        if key == "compat":
            await callback.message.answer("💑 Введи две даты через запятую:\nНапример: 15.03.1995, 22.07.1998")
            await state.set_state(Form.waiting_second_date)
        else:
            await _ask_date(callback.message, user)
            await state.set_state(Form.waiting_date)
        return
    if key in PAID_RAZBORY:
        price = PRICES.get(key, 49)
        title = PAID_RAZBORY[key]
        await send_invoice(callback.message.chat.id, title, TITLES.get(key, title), key, price)
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message, state: FSMContext):
    user    = await get_user(message.from_user.id)
    payload = message.successful_payment.invoice_payload
    if payload not in user["purchased"]:
        user["purchased"].append(payload)
    user["waiting"] = payload
    await save_user(message.from_user.id, user)
    if payload == "compat":
        await message.answer("✅ Оплата прошла! Введи две даты через запятую:\nНапример: 15.03.1995, 22.07.1998")
        await state.set_state(Form.waiting_second_date)
    else:
        await _ask_date(message, user)
        await state.set_state(Form.waiting_date)

# ─── ОБРАБОТКА ДАТ ───────────────────────────────────────────────────────────
async def _process_date(message: Message, user_id: int, user: dict, date_str: str,
                        state: FSMContext, is_free: bool = False):
    number  = calculate_destiny(date_str)
    waiting = user.get("waiting")
    name    = user.get("first_name") or "дорогая"
    if not waiting:
        await message.answer("Выбери разбор из меню 👇", reply_markup=main_menu(user))
        await state.clear()
        return
    if not user.get("birth_date"):
        user["birth_date"]     = date_str
        user["destiny_number"] = number
        await save_user(user_id, user)

    wait_msg = await message.answer(f"⏳ Ева составляет разбор для {name}... Подожди немного ✨")

    async def send_intermediate():
        await asyncio.sleep(20)
        try:
            await bot.edit_message_text(
                "⏳ Ева углубляется в твои числа... Ещё немного, разбор почти готов 🔮",
                chat_id=message.chat.id,
                message_id=wait_msg.message_id
            )
        except Exception:
            pass

    intermediate_task = asyncio.create_task(send_intermediate())

    try:
        context = build_numerology_context(name, date_str)
        prompt  = build_prompt(waiting, name=name, context=context, date=date_str)
        answer  = await ask_ai(prompt)
        intermediate_task.cancel()

        title = TITLES.get(waiting, "🔮 Разбор")
        await send_long(message.chat.id, f"{title}\n\n{answer}")

        if waiting in PDF_KEYS:
            try:
                pdf_bytes = generate_pdf(title, answer, user_name=name, destiny_number=number)
                pdf_file  = BufferedInputFile(pdf_bytes, filename=f"{title}.pdf")
                await bot.send_document(
                    message.chat.id,
                    pdf_file,
                    caption="📄 Твой разбор в PDF — сохрани себе!"
                )
            except Exception as pdf_err:
                logging.warning(f"PDF generation failed for {waiting}: {pdf_err}")

        await message.answer("✨ Тебе также может подойти 👇", reply_markup=upsell_menu(waiting, user))
        await state.clear()
    except Exception as e:
        intermediate_task.cancel()
        logging.error(f"Date handler error [{waiting}]: {e}", exc_info=True)
        await message.answer(
            "❌ Что-то пошло не так. Твоя покупка сохранена — нажми кнопку и попробуй снова 👇",
            reply_markup=retry_menu(waiting)
        )
        await state.clear()

@dp.message(StateFilter(Form.waiting_second_date))
async def handle_two_dates(message: Message, state: FSMContext):
    user  = await get_user(message.from_user.id)
    text  = message.text.strip()
    if "," not in text:
        await message.answer("❌ Введи две даты через запятую.\nНапример: 15.03.1995, 22.07.1998")
        return
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2 or not all(is_valid_date(p) for p in parts):
        await message.answer("❌ Неверный формат. Используй ДД.ММ.ГГГГ, ДД.ММ.ГГГГ")
        return
    name = user.get("first_name") or "дорогая"

    wait_msg = await message.answer("⏳ Ева составляет разбор совместимости...")

    async def send_intermediate():
        await asyncio.sleep(20)
        try:
            await bot.edit_message_text(
                "⏳ Разбираю энергетику двух людей... Ещё немного 🔮",
                chat_id=message.chat.id,
                message_id=wait_msg.message_id
            )
        except Exception:
            pass

    intermediate_task = asyncio.create_task(send_intermediate())

    try:
        n1      = calculate_destiny(parts[0])
        n2      = calculate_destiny(parts[1])
        context = build_numerology_context(name, parts[0])
        prompt  = build_prompt("compat", name=name, context=context, date1=parts[0], date2=parts[1], n2=n2)
        answer  = await ask_ai(prompt)
        intermediate_task.cancel()

        await send_long(message.chat.id, f"💑 Совместимость\n\n{answer}")

        try:
            pdf_bytes = generate_pdf("💑 Совместимость", answer, user_name=name, destiny_number=n1)
            pdf_file  = BufferedInputFile(pdf_bytes, filename="Совместимость.pdf")
            await bot.send_document(message.chat.id, pdf_file, caption="📄 Разбор в PDF — сохрани себе!")
        except Exception as pdf_err:
            logging.warning(f"PDF compat error: {pdf_err}")

        await message.answer("✨ Тебе также может подойти 👇", reply_markup=upsell_menu("compat", user))
        await state.clear()
    except Exception as e:
        intermediate_task.cancel()
        logging.error(f"Compat error: {e}", exc_info=True)
        await message.answer(
            "❌ Что-то пошло не так. Твоя покупка сохранена — нажми кнопку и попробуй снова 👇",
            reply_markup=retry_menu("compat")
        )
        await state.clear()

@dp.message(StateFilter(Form.waiting_date))
async def handle_date(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    text = message.text.strip()
    if not is_valid_date(text):
        await message.answer("❌ Неверная дата. Введи в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        return
    await _process_date(message, message.from_user.id, user, text, state)

# ─── ОТЗЫВЫ ──────────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("leave_review_"))
async def leave_review(callback: CallbackQuery, state: FSMContext):
    key  = callback.data.replace("leave_review_", "")
    user = await get_user(callback.from_user.id)
    if key not in user.get("purchased", []):
        await callback.answer("Отзыв можно оставить только после покупки!", show_alert=True)
        return
    if key in user.get("reviews_left", []):
        await callback.answer("Ты уже оставила отзыв по этому разбору 💫", show_alert=True)
        return
    await state.update_data(review_key=key)
    await callback.message.answer("💬 Напиши свой отзыв — опубликую его в канале!")
    await state.set_state(Form.waiting_review)
    await callback.answer()

@dp.message(StateFilter(Form.waiting_review))
async def handle_review(message: Message, state: FSMContext):
    user        = await get_user(message.from_user.id)
    name        = user.get("first_name") or "Аноним"
    data        = await state.get_data()
    review_key  = data.get("review_key", "")
    title       = TITLES.get(review_key, "разбор")
    review_text = f"⭐ Отзыв о боте @nnumerology_bot\n👤 {name}\n💫 Разбор: {title}\n\n{message.text}"
    reviews_left = user.get("reviews_left", [])
    if review_key and review_key not in reviews_left:
        reviews_left.append(review_key)
    user["reviews_left"] = reviews_left
    user["review_left"]  = True
    await save_user(message.from_user.id, user)
    try:
        await bot.send_message(REVIEWS_CHANNEL, review_text)
        await message.answer("✅ Спасибо! Твой отзыв опубликован 💫")
    except Exception as e:
        logging.error(f"Review channel error: {e}")
        await message.answer("✅ Спасибо за отзыв!")
    await state.clear()

@dp.callback_query(F.data == "show_menu")
async def show_menu(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.message.answer("🔮 Выбери разбор:", reply_markup=main_menu(user))
    await callback.answer()

# ─── РАССЫЛКИ ────────────────────────────────────────────────────────────────
async def send_daily_horoscope():
    """UTC 8:00 = Москва 11:00 — утренняя рассылка из статичных шаблонов."""
    while True:
        now    = utc_now()
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            rows = await db_pool.fetch(
                'SELECT user_id, first_name, destiny_number FROM users '
                'WHERE birth_date IS NOT NULL AND destiny_number IS NOT NULL '
                'AND notifications = TRUE'
            )
            for row in rows:
                try:
                    number   = row['destiny_number']
                    name     = row['first_name'] or "дорогая"
                    variants = MORNING.get(number, MORNING.get(9, []))
                    text     = random.choice(variants).format(name=name)
                    await bot.send_message(row['user_id'], text, reply_markup=notif_off_menu())
                    await asyncio.sleep(0.05)
                except TelegramForbiddenError:
                    await db_pool.execute(
                        'UPDATE users SET notifications = FALSE WHERE user_id = $1',
                        row['user_id']
                    )
                    logging.info(f"User {row['user_id']} blocked bot, notifications disabled")
                except TelegramBadRequest:
                    pass
                except Exception as e:
                    logging.error(f"Horoscope error {row['user_id']}: {e}")
        except Exception as e:
            logging.error(f"Horoscope batch error: {e}")

async def send_daily_channel_post():
    """UTC 7:00 = Москва 10:00 — пост в канал."""
    while True:
        now    = utc_now()
        target = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            today   = date.today()
            day_num = calculate_day_number(today)
            prompt  = (
                f"Напиши нумерологический пост для Телеграм канала на {today.strftime('%d.%m.%Y')}. "
                f"Число дня по нумерологии: {day_num}. "
                f"Расскажи что означает число {day_num}, какая энергия сегодня, дай практичные советы на день. "
                "Обращайся к читательницам на ВЫ, уважительно и тепло. "
                "Не используй фамильярные обращения. "
                "Пиши красиво, с эмодзи, атмосферно. 150-200 слов. Только кириллица."
            )
            post = await ask_ai(prompt)
            await bot.send_message(
                CHANNEL,
                f"🔮 Нумерология дня — {today.strftime('%d.%m.%Y')}\n"
                f"Число дня: {day_num}\n\n"
                f"{post}\n\n"
                f"✨ Узнайте свой личный разбор → @nnumerology_bot"
            )
        except Exception as e:
            logging.error(f"Channel post error: {e}")
        await asyncio.sleep(60)

# ─── WEB ─────────────────────────────────────────────────────────────────────
async def healthcheck(request):
    return web.Response(text="OK")

async def run_web():
    app    = web.Application()
    app.router.add_get("/", healthcheck)
    port   = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    asyncio.create_task(run_web())
    asyncio.create_task(send_daily_horoscope())
    asyncio.create_task(send_daily_channel_post())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
