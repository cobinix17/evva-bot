FROM python:3.12-slim

# Системные библиотеки для WeasyPrint (см. nixpacks.toml — та же причина:
# apt-пакеты попадают в системные пути, которые находит ctypes/ldconfig,
# в отличие от Nix-пакетов).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi8 \
    shared-mime-info \
    libenchant-2-2 \
    hunspell-ru \
    && rm -rf /var/lib/apt/lists/*

# Всё расписание в боте считается в UTC (utc_now, рассылка в 08:00 UTC =
# 11:00 МСК), но numerology берёт текущую дату через datetime.now() — то есть
# по часовому поясу контейнера. На Railway он был UTC; чтобы при переезде на
# другой хостинг личный день и личный месяц не поехали на сутки, фиксируем
# пояс явно, а не полагаемся на дефолт площадки.
ENV TZ=UTC

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
