# yookassa_pay.py — оплата рублями через ЮKassa (пока только премиум-подписка).
# Тонкая обёртка над официальным SDK: он синхронный (requests внутри), поэтому
# каждый вызов идёт через asyncio.to_thread, чтобы не блокировать event loop.
import asyncio
import logging
import uuid

from yookassa import Configuration, Payment

from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY


async def create_payment(amount_rub: int, description: str, return_url: str,
                          metadata: dict) -> tuple[str, str]:
    """Создаёт платёж, возвращает (payment_id, confirmation_url) — на вторую
    ссылку отправляем пользователя оплачивать (редирект-форма ЮKassa)."""
    def _create():
        payment = Payment.create({
            "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": return_url},
            "capture": True,
            "description": description,
            "metadata": metadata,
        }, uuid.uuid4().hex)
        return payment.id, payment.confirmation.confirmation_url
    return await asyncio.to_thread(_create)


async def fetch_payment(payment_id: str):
    """Переспрашивает статус платежа У САМОЙ ЮKassa (не доверяем телу вебхука
    напрямую — по нему легко подделать 'succeeded' — сверяем сервер-сервер)."""
    try:
        return await asyncio.to_thread(Payment.find_one, payment_id)
    except Exception as e:
        logging.error(f"yookassa fetch_payment({payment_id}) failed: {e}")
        return None
