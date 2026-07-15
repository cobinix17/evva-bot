# yookassa_pay.py — оплата рублями через прямой API ЮKassa (не Telegram
# Payments — тот обрезал виджет до одной банковской карты, не показывал
# СБП/ЮMoney/SberPay/T-Pay, хотя все они подключены в кабинете ЮKassa).
# SDK синхронный (requests внутри) — каждый вызов идёт через asyncio.to_thread,
# чтобы не блокировать event loop.
import asyncio
import logging
import uuid

from yookassa import Configuration, Payment

from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY


async def create_payment(amount_rub: int, description: str, return_url: str,
                          metadata: dict, email: str | None = None) -> tuple[str, str]:
    """Создаёт платёж с confirmation.type=redirect — полноценная страница
    оплаты ЮKassa со ВСЕМИ подключёнными способами (карта/СБП/ЮMoney/...),
    в отличие от Telegram-native invoice. email — обязателен для чека по
    54-ФЗ, раз Telegram сам его для нас не спрашивает (как это делал
    need_email в Telegram Payments). Возвращает (payment_id,
    confirmation_url) — на вторую ссылку отправляем пользователя платить."""
    def _create():
        receipt = {
            "items": [{
                "description": description,
                "quantity": "1.00",
                "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
                "vat_code": 1,
            }]
        }
        if email:
            receipt["customer"] = {"email": email}
        payment = Payment.create({
            "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": return_url},
            "capture": True,
            "description": description,
            "metadata": metadata,
            "receipt": receipt,
        }, uuid.uuid4().hex)
        return payment.id, payment.confirmation.confirmation_url
    return await asyncio.to_thread(_create)


async def fetch_payment(payment_id: str):
    """Переспрашивает статус платежа у самой ЮKassa (сервер-сервер) — не
    доверяем телу вебхука напрямую, его легко подделать POST-запросом."""
    try:
        return await asyncio.to_thread(Payment.find_one, payment_id)
    except Exception as e:
        logging.error(f"yookassa fetch_payment({payment_id}) failed: {e}")
        return None
