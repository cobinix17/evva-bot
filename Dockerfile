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

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
