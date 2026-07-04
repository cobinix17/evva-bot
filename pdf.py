# pdf.py — генерация PDF-разборов.
#
# Основной рендер — WeasyPrint + Jinja2 (pdf_template.html): полноценная HTML/CSS
# вёрстка A4 (обложка, карта чисел, авто-разбивка текста по секциям, апселлы),
# работает одинаково для любого из 34 разборов.
#
# Резервный рендер — старый fpdf2 (рисование примитивами). Используется
# автоматически, если WeasyPrint недоступен в окружении (не хватает системных
# библиотек pango/cairo/gdk-pixbuf — см. nixpacks.toml) или упал по любой
# другой причине: пользователь должен получить PDF в любом случае, пусть и в
# более простом оформлении, а не остаться совсем без него.
#
# Зависимость внутри проекта — только numerology.py (структурированные числа
# для карты чисел), сам он не тянет ничего из bot.py/config.py/ai.py.
import os
import re
import io
import math
import logging
from datetime import datetime
from fpdf import FPDF

from numerology import numerology_summary

# Эмодзи-маркеры подзаголовков внутри текста разбора — единый источник правды,
# используется и для авто-разбивки на секции (WeasyPrint), и для fpdf-fallback.
HEADER_EMOJI = (
    "🔮","✨","💎","💰","💕","🔴","🌟","📅","🎯","💡","🚧",
    "💪","⚠️","🌱","🎭","💼","🤝","📈","⏰","🗺","🌍","🏆",
    "💚","⚡","🫀","😤","📜","🔄","🌳","❄️","☠️","😔","💔",
    "💑","💘","💍","🌠","🏢","😨","🗓","⚖️","🔗","🪤","🗝",
    "📚","🔍","💹","🌊","⭐","🌙","👤","🌿","🔋","🕳","👥","💬","📡",
)

_VS16 = '️'  # невидимый модификатор "emoji-стиль" — модель иногда его не ставит

def _strip_vs16(s: str) -> str:
    return s.replace(_VS16, '')

def _is_header_line(s: str) -> bool:
    s = _strip_vs16(s)
    return any(s.startswith(_strip_vs16(e)) for e in HEADER_EMOJI)

_TITLE_STRIP_RE = re.compile(r'^[^\w]+')

def _clean_reading_title(title: str) -> str:
    """Убирает ведущий эмодзи-маркер из заголовка. Шрифты Cormorant/Spectral
    не содержат emoji-глифов — оставленный эмодзи показался бы пустым
    квадратом, поэтому в PDF название разбора идёт чистым текстом, а эмодзи
    остаётся только в тексте сообщения в Telegram."""
    return _TITLE_STRIP_RE.sub('', title).strip()

_ROMAN = ["I","II","III","IV","V","VI","VII","VIII","IX","X","XI","XII","XIII","XIV"]

def _split_sections(text: str) -> list[dict]:
    """Режет текст разбора на блоки по строкам-заголовкам (эмодзи-маркер в
    начале строки). Если заголовков нет вообще (нестандартный текст) —
    весь текст идёт одним блоком, чтобы страница не осталась пустой."""
    sections = []
    cur = None
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if _is_header_line(line):
            cur = {"title": _clean_reading_title(line), "body": ""}
            sections.append(cur)
        elif cur is not None:
            cur["body"] = f"{cur['body']} {line}".strip() if cur["body"] else line
    if not sections:
        sections = [{"title": "Разбор", "body": text.strip()}]
    for i, s in enumerate(sections):
        s["rn"] = _ROMAN[i] if i < len(_ROMAN) else str(i + 1)
    return sections

def _paginate(sections: list[dict], char_budget: int = 1400) -> list[list[dict]]:
    """Группирует секции по страницам ПО ФАКТИЧЕСКОМУ ОБЪЁМУ текста, а не по
    фиксированному количеству. Раньше группировка была строго по 3 секции на
    страницу независимо от их длины — на разборах с длинными подробными
    абзацами (обычное дело для дорогих 149⭐-разборов) текст физически не
    помещался в фиксированную высоту страницы и обрезался (см. .page в
    pdf_template.html) — часть текста молча пропадала из PDF. Теперь секция
    добавляется на текущую страницу пока не превышен бюджет символов; если
    даже одна секция сама по себе больше бюджета — она всё равно кладётся
    на свою страницу целиком (не бросаем текст), а CSS-страница ещё и не
    обрезает лишнее (min-height + overflow:visible) на случай если оценка
    объёма всё же ошиблась."""
    pages: list[list[dict]] = []
    current: list[dict] = []
    current_len = 0
    for s in sections:
        s_len = len(s["title"]) + len(s["body"])
        if current and current_len + s_len > char_budget:
            pages.append(current)
            current, current_len = [], 0
        current.append(s)
        current_len += s_len
    if current:
        pages.append(current)
    return pages

# ── WEASYPRINT (основной рендер) ──────────────────────────────────────────────
_TEMPLATE_DIR  = os.path.dirname(__file__)
_TEMPLATE_NAME = "pdf_template.html"

def _generate_pdf_weasy(title: str, text: str, user_name: str, destiny_number: int | None,
                         birth_date: str | None, upsells: list[dict] | None) -> bytes:
    from jinja2 import Environment, FileSystemLoader
    from weasyprint import HTML

    numbers = None
    if birth_date:
        try:
            numbers = numerology_summary(user_name, birth_date)
        except Exception as e:
            logging.warning(f"numerology_summary failed for PDF card page: {e}")

    clean_upsells = [
        {**u, "title": _clean_reading_title(u["title"])} for u in (upsells or [])
    ]

    env      = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), autoescape=True)
    template = env.get_template(_TEMPLATE_NAME)
    html_str = template.render(
        reading_title  = _clean_reading_title(title),
        name           = user_name or None,
        birthdate      = birth_date,
        destiny_number = destiny_number,
        numbers        = numbers,
        section_pages  = _paginate(_split_sections(text)),
        upsells        = clean_upsells,
        bot_handle     = "@nnumerology_bot",
    )
    return HTML(string=html_str, base_url=_TEMPLATE_DIR).write_pdf()

# ── FPDF (резервный рендер) ───────────────────────────────────────────────────
# Шрифт должен лежать в репо как DejaVuSans.ttf / DejaVuSans-Bold.ttf,
# рядом с этим файлом. При отсутствии/повреждении — скачивается автоматически.
FONT_PATH      = os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf")
BOLD_FONT_PATH = os.path.join(os.path.dirname(__file__), "DejaVuSans-Bold.ttf")

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

# ── ЦВЕТОВАЯ ПАЛИТРА ─────────────────────────────────────────────────────────
# Нумерологическая тема (лаванда / аметист / золото)
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
C_WATERMARK   = (241, 235, 249)  # едва заметная октаграмма на фоне страницы
C_HEAD_BAND   = (238, 229, 249)  # плашка под заголовком темы
C_SUBTITLE    = (150, 120, 186)  # подзаголовок под названием разбора

# ── ДЕКОРАТИВНЫЕ ЭЛЕМЕНТЫ ────────────────────────────────────────────────────
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

# ── КЛАСС PDF ─────────────────────────────────────────────────────────────────
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

        corner_size = 9
        _draw_corner_ornament(self, 14, 16, corner_size, C_GOLD, flip_x=False, flip_y=False)
        _draw_corner_ornament(self, W - 14, 16, corner_size, C_GOLD, flip_x=True, flip_y=False)
        _draw_corner_ornament(self, 14, H - 15, corner_size, C_GOLD, flip_x=False, flip_y=True)
        _draw_corner_ornament(self, W - 14, H - 15, corner_size, C_GOLD, flip_x=True, flip_y=True)

        # едва заметная октаграмма-водяной знак по центру страницы —
        # рисуется тонкими линиями почти в цвет фона, не мешает чтению
        _draw_star_polygon(self, W / 2, H / 2, r_outer=62, r_inner=27,
                           points=8, color=C_WATERMARK, line_width=0.3)
        _draw_star_polygon(self, W / 2, H / 2, r_outer=40, r_inner=17,
                           points=8, color=C_WATERMARK, rotate_deg=-67.5, line_width=0.3)

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

def _generate_pdf_fpdf(title: str, text: str, user_name: str = "", destiny_number: int | None = None) -> bytes:
    """Резервный рендер (см. docstring модуля): рисование примитивами через fpdf2 —
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
    pdf.ln(0.5)

    pdf.set_font(font_name, size=9)
    pdf.set_text_color(*C_SUBTITLE)
    pdf.cell(0, 5, "✦  Н У М Е Р О Л О Г И Ч Е С К И Й   Р А З Б О Р  ✦", align="C")
    pdf.ln(6)

    y   = pdf.get_y()
    mid = W / 2
    pdf.set_draw_color(*C_ACCENT)
    pdf.set_line_width(0.4)
    pdf.line(pdf.l_margin + 6, y + 3.5, mid - 7, y + 3.5)
    pdf.line(mid + 7, y + 3.5, W - pdf.r_margin - 6, y + 3.5)
    _draw_star_polygon(pdf, mid, y + 3.5, r_outer=4.2, r_inner=1.8, points=8, color=C_GOLD)
    pdf.set_y(y + 9)

    # Без предлога "Для" — оно требует родительного падежа ("Для Анны", а
    # не "Для Анна"), а склонять произвольное имя корректно без библиотеки
    # морфологии нельзя. Голое имя рядом с датой читается однозначно и без
    # предлога.
    info_parts = []
    if user_name:
        info_parts.append(user_name)
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

    pdf.set_font(font_name, size=11.5)
    pdf.set_text_color(*C_BODY)

    for paragraph in text.split("\n"):
        line = paragraph.strip()
        if not line:
            pdf.ln(4)
            continue
        clean_line = re.sub(r"[^\w\s\(\)\-—.,!?:;%№]", "", line, flags=re.UNICODE).strip()
        is_header  = _is_header_line(paragraph)
        if is_header and clean_line:
            _draw_header_band(pdf, clean_line, font_name)
        elif clean_line:
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(0, 7, clean_line)
            pdf.ln(1.5)

    return bytes(pdf.output())

def _draw_header_band(pdf: FPDF, text: str, font_name: str):
    """Заголовок темы на лавандовой плашке: золотая полоса слева, векторная
    4-лучевая звёздочка-маркер (вместо вырезанного эмодзи) и жирный текст.
    Высота плашки считается по числу строк, чтобы длинные заголовки не
    обрезались, и плашка не разрывалась переносом страницы."""
    pdf.ln(2.5)
    text_indent = 9.0
    band_x = pdf.l_margin
    band_w = pdf.w - pdf.l_margin - pdf.r_margin
    avail  = band_w - text_indent - 3

    pdf.set_font(font_name, style="B", size=12.5)
    try:
        lines = pdf.multi_cell(avail, 7, text, dry_run=True, output="LINES")
        n_lines = max(1, len(lines))
    except Exception:
        approx = pdf.get_string_width(text)
        n_lines = max(1, int(approx / avail) + 1)

    band_h = n_lines * 7 + 3.5
    band_y = pdf.get_y()

    # чтобы плашка не оказалась разорванной внизу страницы
    if band_y + band_h > pdf.h - pdf.b_margin:
        pdf.add_page()
        band_y = pdf.get_y()

    pdf.set_fill_color(*C_HEAD_BAND)
    pdf.rect(band_x, band_y, band_w, band_h, style="F")
    pdf.set_fill_color(*C_GOLD)
    pdf.rect(band_x, band_y, 1.6, band_h, style="F")
    _draw_star_polygon(pdf, band_x + 5.2, band_y + band_h / 2,
                       r_outer=2.3, r_inner=1.0, points=4, color=C_GOLD, line_width=0.5)

    pdf.set_xy(band_x + text_indent, band_y + 1.75)
    pdf.set_text_color(*C_HEADER)
    pdf.multi_cell(avail, 7, text)

    pdf.set_y(band_y + band_h + 2.5)
    pdf.set_font(font_name, size=11.5)
    pdf.set_text_color(*C_BODY)

# ── ТОЧКА ВХОДА ────────────────────────────────────────────────────────────────
def generate_pdf(title: str, text: str, user_name: str = "", destiny_number: int | None = None,
                  birth_date: str | None = None, upsells: list[dict] | None = None) -> bytes:
    """Генерирует PDF разбора. birth_date и upsells — необязательные: если дата
    рождения передана, на второй странице появляется карта чисел; если передан
    список апселлов ([{'title','desc','price'}, ...]) — добавляется страница CTA.
    См. docstring модуля про основной/резервный рендер."""
    try:
        return _generate_pdf_weasy(title, text, user_name, destiny_number, birth_date, upsells)
    except Exception as e:
        logging.warning(f"WeasyPrint PDF failed ({e}), falling back to fpdf")
        return _generate_pdf_fpdf(title, text, user_name=user_name, destiny_number=destiny_number)
