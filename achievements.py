# achievements.py — лёгкая геймификация: значки-достижения. Считаются на лету
# из уже имеющихся данных пользователя (кол-во разборов, рефералы, премиум,
# онбординг и т.д.) — отдельной таблицы под них не нужно. Общий источник для
# бота (команда /achievements) и веб-кабинета (карточка достижений).

def compute_achievements(purchased_count: int, ref_count: int, is_premium: bool,
                         has_birthdate: bool, has_spun: bool, digest_ready: bool) -> list[dict]:
    """Возвращает список достижений с флагом earned и прогрессом. Порядок —
    от простого к сложному, чтобы в UI сначала шли уже открытые."""
    defs = [
        # Формулировки без рода: пол пользователя мы не знаем, а «указала» /
        # «пригласила подругу» половине читателей звучит мимо.
        ("start",   "🎁", "Первый шаг",      "Дата рождения указана",          has_birthdate),
        ("spin",    "🎰", "Везение",         "Ежедневный бонус забран",        has_spun),
        ("first",   "📚", "Первый разбор",   "Первый разбор открыт",           purchased_count >= 1),
        ("ref",     "👥", "Проводник",       "Кто-то пришёл по твоей ссылке",  ref_count >= 1),
        ("collect", "🔮", "Коллекция",       "5 разборов в копилке",           purchased_count >= 5),
        ("digest",  "📋", "Целостный портрет","Общий портрет собран",          digest_ready),
        ("premium", "💎", "Премиум",         "Подписка оформлена",             is_premium),
        ("expert",  "🌟", "Глубина",         "10 разборов в копилке",          purchased_count >= 10),
    ]
    return [
        {"id": i, "emoji": e, "title": t, "desc": d, "earned": bool(earned)}
        for (i, e, t, d, earned) in defs
    ]


def achievements_summary(items: list[dict]) -> str:
    """Короткий текст для бота: сетка значков + сколько открыто."""
    earned = [a for a in items if a["earned"]]
    lines = ["🏆 Твои достижения\n"]
    for a in items:
        mark = a["emoji"] if a["earned"] else "🔒"
        title = a["title"] if a["earned"] else f"{a['title']} — {a['desc']}"
        lines.append(f"{mark} {title}")
    lines.append(f"\nОткрыто: {len(earned)} из {len(items)}")
    return "\n".join(lines)
