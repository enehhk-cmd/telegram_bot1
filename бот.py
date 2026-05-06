import csv
import html
import httpx
import json
import logging
import os
import random
import re
import string
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8606663935:AAH8NG2r1dG42pMbDKPRs2kHUfj1Of3GnUc")
DATA_FILE = Path(os.getenv("SHOP_DATA_FILE", "shop_data.json"))
ROOT_ADMIN_USERNAMES = {"unison_off", "whooshbuymanager"}
EXPORT_DIR = Path("exports")

PAID_STATUSES = {"paid", "paid_balance", "paid_crypto", "confirmed", "подтвержден", "оплачен с баланса"}
PENDING_STATUSES = {"pending", "awaiting_payment", "awaiting_manual", "awaiting_crypto", "ожидает оплаты"}
LEGACY_CITY_NAME = "\u041c\u043e\u0441\u043a\u0432\u0430"

BUTTON_DEFAULTS = {
    "catalog": "🛴 Арендовать самокат",
    "search": "🔎 Найти тариф",
    "cart": "🧺 Корзина",
    "favorites": "⭐ Избранное",
    "balance": "💳 Баланс",
    "orders": "🧾 Мои поездки",
    "reviews": "💬 Отзывы",
    "support": "🛟 Поддержка",
    "faq": "❓ FAQ",
    "agreement": "📜 Правила аренды",
    "admin": "🛠 РАЗДЕЛ АДМИНОВ",
}

SCOOTER_MODEL = "ninebot 90"
SCOOTER_CODE_RE = re.compile(r"^[A-Z]{2}(?:[0-9]{1,4}|[0-9]{1,3}[A-Z])$")
UNSAFE_MARKERS = {"<script", "javascript:", "onerror=", "onclick=", "onload="}

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def generate_id(prefix: str = "ORD") -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{prefix}-{datetime.now().strftime('%Y%m%d')}-{suffix}"


def h(value: Any) -> str:
    return html.escape(str(value), quote=False)


def clean_username(username: str | None) -> str:
    return (username or "").lstrip("@").strip()


def normalize_status(status: str) -> str:
    mapping = {
        "подтвержден": "paid",
        "оплачен с баланса": "paid_balance",
        "ожидает оплаты": "awaiting_manual",
        "отклонён": "rejected",
        "отклонен": "rejected",
    }
    return mapping.get(status, status or "draft")


def status_label(status: str) -> str:
    labels = {
        "draft": "черновик",
        "awaiting_payment": "ждёт автооплаты",
        "awaiting_manual": "ждёт ручной оплаты",
        "paid": "оплачен",
        "paid_balance": "оплачен балансом",
        "paid_crypto": "оплачен Crypto Bot",
        "awaiting_crypto": "ждёт Crypto Bot",
        "rejected": "отклонён",
        "cancelled": "отменён",
        "refunded": "возврат отмечен",
    }
    return labels.get(normalize_status(status), status)


def normalize_scooter_code(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip().upper())


def is_valid_scooter_code(value: str) -> bool:
    return bool(SCOOTER_CODE_RE.fullmatch(normalize_scooter_code(value)))


def scooter_code_help_table() -> str:
    return (
        "🚫 <b>Самокат не найден</b>\n\n"
        "<pre>"
        "Вариация  Пример\n"
        "AA0000    AC1111\n"
        "AA000A    AC123A"
        "</pre>\n"
        "Введите код английскими буквами и цифрами без пробелов или отправьте фото QR-кода."
    )


def scooter_found_text(rental: dict[str, Any]) -> str:
    scooter_number = rental.get("scooter_code") or "QR-код"
    return (
        "✅ <b>Самокат найден</b>\n\n"
        "<pre>"
        f"Модель: {SCOOTER_MODEL}\n"
        f"Заряд:  {int(rental.get('battery_percent', 50))}%\n"
        f"Номер:  {scooter_number}"
        "</pre>\n"
        "Вы готовы активировать данный самокат?"
    )


def ticket_status_emoji(status: str) -> str:
    return {
        "new": "🆕",
        "new_message": "🔔",
        "open": "🟢",
        "closed": "🔒",
    }.get(status or "new", "🟢")


def payment_proof_label(status: str) -> str:
    return {
        "waiting_screenshot": "⏳ ждёт скрин",
        "new": "📸 новый скрин",
        "confirmed": "✅ подтверждено",
        "rejected": "❌ отклонено",
    }.get(status or "", status or "без скрина")


def first_catalog_category(data: dict[str, Any]) -> str:
    return next(iter(data.get("catalog") or {"Аренда": []}), "Аренда")


def manual_payment_requisites(data: dict[str, Any]) -> str:
    settings = data.get("settings", {})
    return str(
        settings.get("manual_payment_requisites")
        or settings.get("payment_channel_url")
        or "💳 Реквизиты для оплаты пока не настроены."
    )


def default_settings() -> dict[str, Any]:
    return {
        "shop_title": "🛴 Whoosh Buy",
        "main_screen_text": (
            "Аренда самоката по всей стране.\n\n"
            "Выберите тариф, оплатите заказ, отправьте скрин оплаты при ручной оплате и затем номер самоката или QR-код."
        ),
        "main_screen_photo": "",
        "faq": (
            "FAQ\n\n"
            "1. Как начать аренду? Оплатите тариф и нажмите кнопку «Арендовать самокат».\n"
            "2. Что отправить? Номер самоката или фото QR-кода.\n"
            "3. Если самокат недоступен, бот покажет сообщение о невозможности аренды.\n"
            "4. Пополнение баланса оформляется через профиль."
        ),
        "payment_contact_username": "",
        "payment_channel_url": "",
        "topup_requisites": "💳 Реквизиты для пополнения пока не настроены. Их можно изменить в разделе «Оплата».",
        "manual_payment_requisites": "💳 Реквизиты для оплаты аренды пока не настроены. Их можно изменить в разделе «Оплата».",
        "payment_currency": "RUB",
        "crypto_pay_enabled": True,
        "crypto_pay_token": "",
        "crypto_pay_api_url": "https://pay.crypt.bot",
        "crypto_pay_currency_type": "fiat",
        "crypto_pay_fiat": "RUB",
        "crypto_pay_asset": "USDT",
        "crypto_pay_accepted_assets": "USDT,TON,BTC,ETH,LTC,BNB,TRX,USDC",
        "crypto_pay_expires_minutes": 60,
        "auto_payments_enabled": False,
        "manual_payments_enabled": True,
        "balance_enabled": True,
        "sales_enabled": True,
        "maintenance_mode": False,
        "notify_admins": True,
        "moderate_reviews": False,
        "require_agreement": True,
        "cashback_percent": 0,
        "referral_enabled": True,
        "referral_bonus": 50,
        "low_stock_threshold": 3,
        "min_topup_amount": 10,
        "order_ttl_minutes": 120,
        "buttons": deepcopy(BUTTON_DEFAULTS),
    }


def default_agreement() -> str:
    return (
        "Правила аренды самокатов Whoosh Buy\n\n"
        "1. Бот предназначен для оформления заявок на краткосрочную аренду самокатов.\n"
        "2. Пользователь выбирает тариф, оплачивает заказ доступным способом и отправляет номер самоката или QR-код.\n"
        "3. Аренда возможна только для доступных самокатов и только при корректном номере или читаемом QR-коде.\n"
        "4. Если выбранный самокат недоступен, повреждён, заблокирован или данные указаны неверно, аренда может быть невозможна.\n"
        "5. Пользователь обязан соблюдать ПДД, правила парковки, требования безопасности и не передавать самокат третьим лицам.\n"
        "6. Запрещены попытки обхода защиты, вскрытия, несанкционированного доступа и любые незаконные действия с техникой.\n"
        "7. Возвраты и спорные ситуации рассматриваются через поддержку внутри бота."
    )


def scooter_catalog() -> dict[str, list[dict[str, Any]]]:
    return {
        "Аренда": [
            {
                "id": "SCOOTER-TARIFF-MIN",
                "title": "🛴 Тариф Минимум",
                "description": "До 3-х часов аренды",
                "price": 199,
                "photo": "",
                "active": True,
                "stock": -1,
                "delivery_text": "Оплата принята. Нажмите кнопку «🛴 Арендовать самокат» и отправьте номер самоката или QR-код.",
            },
            {
                "id": "SCOOTER-TARIFF-MID",
                "title": "🛴 Тариф Средний",
                "description": "Оптимальный тариф для обычной поездки по делам. Баланс между ценой и временем аренды.",
                "price": 351,
                "photo": "",
                "active": True,
                "stock": -1,
                "delivery_text": "Оплата принята. Нажмите кнопку «🛴 Арендовать самокат» и отправьте номер самоката или QR-код.",
            },
            {
                "id": "SCOOTER-TARIFF-MAX",
                "title": "🛴 Тариф Максимум",
                "description": "Расширенный тариф для долгой поездки. Больше времени на аренду и спокойный запас для маршрута.",
                "price": 703,
                "photo": "",
                "active": True,
                "stock": -1,
                "delivery_text": "Оплата принята. Нажмите кнопку «🛴 Арендовать самокат» и отправьте номер самоката или QR-код.",
            },
        ],
    }


def default_data() -> dict[str, Any]:
    return {
        "users": {},
        "admins": [],
        "catalog": scooter_catalog(),
        "orders": {},
        "tickets": {},
        "reviews": {},
        "rental_requests": {},
        "payments": {},
        "promo_codes": {
            "START10": {
                "code": "START10",
                "type": "percent",
                "amount": 10,
                "active": True,
                "uses": 0,
                "max_uses": 100,
                "created_at": now_iso(),
            }
        },
        "broadcasts": {},
        "audit_log": [],
        "settings": default_settings(),
        "agreement": default_agreement(),
        "schema_version": 12,
    }


def contains_unsafe(value: Any) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in UNSAFE_MARKERS)


def audit(data: dict[str, Any], actor_id: int | str, action: str, details: str = "") -> None:
    data.setdefault("audit_log", [])
    data["audit_log"].append(
        {
            "time": now_iso(),
            "actor_id": str(actor_id),
            "action": action,
            "details": details[:500],
        }
    )
    data["audit_log"] = data["audit_log"][-500:]


def ensure_user_defaults(user: dict[str, Any]) -> bool:
    changed = False
    defaults = {
        "username": "",
        "full_name": "",
        "purchases_count": 0,
        "balance": 0,
        "created_at": now_iso(),
        "agreed": False,
        "favorites": [],
        "cart": {},
        "blocked": False,
        "subscribed": True,
        "bonus_points": 0,
        "active_promo": "",
        "referral_code": "",
        "referred_by": "",
        "notes": "",
        "tags": [],
    }
    for key, value in defaults.items():
        if key not in user:
            user[key] = deepcopy(value)
            changed = True
    if not user.get("referral_code"):
        user["referral_code"] = generate_id("REF")
        changed = True
    return changed


def ensure_product_defaults(category: str, item: dict[str, Any], index: int) -> bool:
    changed = False
    defaults = {
        "id": f"{category[:3].upper()}-{index}",
        "title": f"Цифровой товар {index}",
        "description": "Легальный цифровой товар.",
        "price": 10,
        "model": "",
        "photo": "",
        "active": True,
        "stock": -1,
        "sold_count": 0,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "delivery_text": "Спасибо за покупку. Настройте текст выдачи в админ-панели.",
    }
    for key, value in defaults.items():
        if key not in item:
            item[key] = deepcopy(value)
            changed = True
    try:
        item["price"] = max(1, int(item.get("price", 1)))
    except (TypeError, ValueError):
        item["price"] = 10
        changed = True
    try:
        item["stock"] = int(item.get("stock", -1))
    except (TypeError, ValueError):
        item["stock"] = -1
        changed = True
    if contains_unsafe(item.get("title")) or contains_unsafe(item.get("description")):
        item["title"] = f"Цифровой товар {index}"
        item["description"] = "Легальный цифровой товар. Описание было заменено при безопасной миграции."
        item["photo"] = ""
        item["active"] = True
        item["delivery_text"] = "Спасибо за покупку. Настройте выдачу для этого товара в админ-панели."
        changed = True
    return changed


def migrate_data(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    changed = False
    base = default_data()
    for key, value in base.items():
        if key not in data:
            data[key] = deepcopy(value)
            changed = True

    for key, value in default_settings().items():
        if key not in data["settings"]:
            data["settings"][key] = deepcopy(value)
            changed = True
    if int(data.get("schema_version", 0) or 0) < 5 and int(data["settings"].get("referral_bonus", 0) or 0) == 0:
        data["settings"]["referral_bonus"] = default_settings()["referral_bonus"]
        changed = True
    if int(data.get("schema_version", 0) or 0) < 6:
        data["settings"].update(
            {
                "shop_title": default_settings()["shop_title"],
                "main_screen_text": default_settings()["main_screen_text"],
                "faq": default_settings()["faq"],
                "payment_currency": "RUB",
                "auto_payments_enabled": False,
            }
        )
        data["settings"]["buttons"] = deepcopy(BUTTON_DEFAULTS)
        data["agreement"] = default_agreement()
        data["catalog"] = scooter_catalog()
        data.setdefault("rental_requests", {})
        changed = True
    if data["settings"].pop("telegram_provider_token", None) is not None:
        changed = True
    if int(data.get("schema_version", 0) or 0) < 8:
        data["settings"]["shop_title"] = default_settings()["shop_title"]
        data["settings"]["main_screen_text"] = default_settings()["main_screen_text"]
        data["settings"]["faq"] = default_settings()["faq"]
        data["settings"]["topup_requisites"] = default_settings()["topup_requisites"]
        data["settings"].setdefault("buttons", {})
        data["settings"]["buttons"]["catalog"] = BUTTON_DEFAULTS["catalog"]
        data["catalog"] = scooter_catalog()
        data["agreement"] = default_agreement()
        data.setdefault("rental_requests", {})
        changed = True
    if int(data.get("schema_version", 0) or 0) < 9:
        data["settings"]["shop_title"] = default_settings()["shop_title"]
        data["settings"]["main_screen_text"] = default_settings()["main_screen_text"]
        data["settings"]["faq"] = default_settings()["faq"]
        data["settings"]["topup_requisites"] = data["settings"].get("topup_requisites") or default_settings()["topup_requisites"]
        data["settings"]["manual_payment_requisites"] = (
            data["settings"].get("manual_payment_requisites")
            or data["settings"].get("payment_channel_url")
            or default_settings()["manual_payment_requisites"]
        )
        data["catalog"] = scooter_catalog()
        data["agreement"] = default_agreement()
        changed = True
    if int(data.get("schema_version", 0) or 0) < 10:
        data["settings"]["shop_title"] = default_settings()["shop_title"]
        data["settings"]["main_screen_text"] = default_settings()["main_screen_text"]
        data["settings"]["faq"] = default_settings()["faq"]
        data["settings"]["manual_payment_requisites"] = data["settings"].get("manual_payment_requisites") or default_settings()["manual_payment_requisites"]
        data["settings"].setdefault("buttons", {})
        data["settings"]["buttons"]["catalog"] = BUTTON_DEFAULTS["catalog"]
        data["catalog"] = scooter_catalog()
        data["agreement"] = default_agreement()
        changed = True
    if int(data.get("schema_version", 0) or 0) < 11:
        if "Администратор может" in str(data["settings"].get("topup_requisites", "")):
            data["settings"]["topup_requisites"] = default_settings()["topup_requisites"]
            changed = True
        if "Администратор может" in str(data["settings"].get("manual_payment_requisites", "")):
            data["settings"]["manual_payment_requisites"] = default_settings()["manual_payment_requisites"]
            changed = True
    if int(data.get("schema_version", 0) or 0) < 12:
        current_prices = {
            item.get("id"): int(item.get("price", 0) or 0)
            for items in data.get("catalog", {}).values()
            for item in items
        }
        data["catalog"] = scooter_catalog()
        for items in data["catalog"].values():
            for item in items:
                if current_prices.get(item["id"]):
                    item["price"] = current_prices[item["id"]]
        changed = True
    for order in data.get("orders", {}).values():
        if str(order.get("payment_method", "")).lower() == "telegram":
            order["payment_method"] = "legacy"
            changed = True
        if normalize_status(order.get("status", "")) == "paid_telegram":
            order["status"] = "paid"
            changed = True
    for payment in data.get("payments", {}).values():
        if not payment.get("provider") or payment.get("provider") == "telegram":
            payment["provider"] = "legacy"
            changed = True
        if payment.get("currency") == "XTR":
            payment["currency"] = "RUB"
            changed = True
    for row in data.get("audit_log", []):
        if "telegram" in str(row.get("action", "")).lower():
            row["action"] = "legacy_invoice_created"
            changed = True
    data["settings"].setdefault("buttons", {})
    for key, value in BUTTON_DEFAULTS.items():
        if key not in data["settings"]["buttons"]:
            data["settings"]["buttons"][key] = value
            changed = True

    if contains_unsafe(data["settings"].get("main_screen_text")):
        data["settings"]["main_screen_text"] = default_settings()["main_screen_text"]
        changed = True

    data["settings"]["payment_contact_username"] = "@unison_off"
    if not data["settings"].get("payment_channel_url"):
        data["settings"]["payment_channel_url"] = "https://t.me/unison_off"

    if not data.get("agreement") or contains_unsafe(data.get("agreement")):
        data["agreement"] = default_agreement()
        changed = True

    for user_id, user in list(data["users"].items()):
        user["id"] = str(user.get("id") or user_id)
        if ensure_user_defaults(user):
            changed = True
        if clean_username(user.get("username")).lower() in ROOT_ADMIN_USERNAMES and user_id not in data["admins"]:
            data["admins"].append(str(user_id))
            changed = True

    for category, items in list(data["catalog"].items()):
        if not isinstance(items, list):
            data["catalog"][category] = []
            changed = True
            continue
        for index, item in enumerate(items, start=1):
            if ensure_product_defaults(category, item, index):
                changed = True

    for order in data["orders"].values():
        old_status = order.get("status", "")
        new_status = normalize_status(old_status)
        if old_status != new_status:
            order["status"] = new_status
            changed = True
        if order.get("city") == LEGACY_CITY_NAME:
            order["city"] = ""
            changed = True
        if order.get("category") == LEGACY_CITY_NAME:
            order["category"] = "Аренда"
            changed = True
        for row in order.get("items", []):
            if row.get("category") == LEGACY_CITY_NAME:
                row["category"] = "Аренда"
                changed = True
        if contains_unsafe(order.get("item_title")):
            order["item_title"] = "Цифровой товар"
            changed = True
        order.setdefault("total", order.get("price", 0))
        order.setdefault("items", [])
        order.setdefault("payment_method", "manual")
        order.setdefault("discount", 0)
        order.setdefault("payment_id", "")
        order.setdefault("scooter_model", SCOOTER_MODEL)
        if order.get("scooter_model") == "ninebot90s":
            order["scooter_model"] = SCOOTER_MODEL
            changed = True
        if "ninebot" in str(order.get("item_title", "")).lower():
            order["item_title"] = "Аренда самоката"
            changed = True
        if "ninebot" in str(order.get("delivery_text", "")).lower():
            order["delivery_text"] = str(order.get("delivery_text", "")).replace(" ninebot90s", "").replace("ninebot90s", "самоката")
            changed = True
        for row in order.get("items", []):
            if "ninebot" in str(row.get("title", "")).lower():
                row["title"] = "Аренда самоката"
                changed = True
        order.setdefault("payment_proof_photo_id", "")
        order.setdefault("payment_proof_text", "")
        order.setdefault("payment_proof_status", "")
        order.setdefault("payment_proof_at", "")
        order.setdefault("purchase_counted", new_status in PAID_STATUSES)
        order.setdefault("referral_bonus_applied", False)

    for review in data.get("reviews", {}).values():
        if review.get("city") == LEGACY_CITY_NAME:
            review["city"] = ""
            changed = True
        if review.get("category") == LEGACY_CITY_NAME:
            review["category"] = "Аренда"
            changed = True

    for ticket in data.get("tickets", {}).values():
        if ticket.get("type") != "topup":
            if ticket.get("type") != "support":
                ticket["type"] = "support"
                changed = True
            if not ticket.get("messages"):
                ticket["messages"] = [{"role": "user", "text": ticket.get("text", ""), "created_at": ticket.get("created_at", "")}]
                changed = True
            if ticket.get("status") == "new":
                ticket["status"] = "new_message"
                changed = True
        else:
            if "receipt_text" not in ticket:
                ticket["receipt_text"] = ""
                changed = True
            if "receipt_photo_id" not in ticket:
                ticket["receipt_photo_id"] = ""
                changed = True

    data["schema_version"] = 12
    return data, changed


def load_data() -> dict[str, Any]:
    if not DATA_FILE.exists():
        data = default_data()
        save_data(data)
        return data
    with DATA_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)
    data, changed = migrate_data(data)
    if changed:
        save_data(data)
    return data


def save_data(data: dict[str, Any]) -> None:
    if DATA_FILE.parent != Path("."):
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_or_create_user(data: dict[str, Any], tg_user) -> dict[str, Any]:
    user_id = str(tg_user.id)
    user = data["users"].get(user_id)
    changed = False
    if not user:
        user = {
            "id": user_id,
            "username": tg_user.username or "",
            "full_name": tg_user.full_name,
            "created_at": now_iso(),
        }
        data["users"][user_id] = user
        changed = True
    if ensure_user_defaults(user):
        changed = True
    if user.get("username") != (tg_user.username or ""):
        user["username"] = tg_user.username or ""
        changed = True
    if user.get("full_name") != tg_user.full_name:
        user["full_name"] = tg_user.full_name
        changed = True
    if clean_username(user.get("username")).lower() in ROOT_ADMIN_USERNAMES and user_id not in data["admins"]:
        data["admins"].append(user_id)
        changed = True
    if changed:
        save_data(data)
    return user


def is_admin(data: dict[str, Any], user_id: int | str) -> bool:
    return str(user_id) in [str(admin_id) for admin_id in data.get("admins", [])]


def is_blocked(user: dict[str, Any]) -> bool:
    return bool(user.get("blocked"))


def clear_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ("state", "support_mode", "search_mode", "leave_review_order"):
        context.user_data.pop(key, None)


def money(amount: int | float, currency: str | None = None) -> str:
    currency = currency or default_settings()["payment_currency"]
    value = int(amount)
    return f"{value} {currency}"


def button_label(data: dict[str, Any], key: str) -> str:
    return str(data.get("settings", {}).get("buttons", {}).get(key) or BUTTON_DEFAULTS.get(key, key))


def amount_to_minor(amount: int | float, currency: str) -> int:
    amount = int(amount)
    return amount * 100


def payment_currency(data: dict[str, Any]) -> str:
    return str(data["settings"].get("payment_currency") or "RUB").upper()


def is_auto_payment_ready(data: dict[str, Any]) -> bool:
    return False


def crypto_pay_token(data: dict[str, Any]) -> str:
    return os.getenv("CRYPTO_PAY_TOKEN") or str(data["settings"].get("crypto_pay_token") or "")


def crypto_pay_api_url(data: dict[str, Any]) -> str:
    return (os.getenv("CRYPTO_PAY_API_URL") or str(data["settings"].get("crypto_pay_api_url") or "https://pay.crypt.bot")).rstrip("/")


def crypto_pay_ready(data: dict[str, Any]) -> bool:
    return bool(data["settings"].get("crypto_pay_enabled", True) and crypto_pay_token(data))


def crypto_pay_display_currency(data: dict[str, Any]) -> str:
    settings = data["settings"]
    if settings.get("crypto_pay_currency_type", "fiat") == "crypto":
        return str(settings.get("crypto_pay_asset") or "USDT").upper()
    return str(settings.get("crypto_pay_fiat") or "RUB").upper()


def decimal_amount(value: Any) -> str:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        amount = Decimal("0")
    return format(amount.quantize(Decimal("0.01")).normalize(), "f")


async def crypto_pay_request(data: dict[str, Any], method: str, payload: dict[str, Any] | None = None) -> Any:
    token = crypto_pay_token(data)
    if not token:
        raise RuntimeError("Crypto Pay token is not configured")
    url = f"{crypto_pay_api_url(data)}/api/{method}"
    headers = {"Crypto-Pay-API-Token": token}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(url, json=payload or {}, headers=headers)
        response.raise_for_status()
        body = response.json()
    if not body.get("ok"):
        raise RuntimeError(str(body.get("error") or body))
    return body.get("result")


def item_key(category: str, item_id: str) -> str:
    return f"{category}::{item_id}"


def split_item_key(key: str) -> tuple[str, str]:
    return key.split("::", 1)


def get_item(data: dict[str, Any], category: str, item_id: str) -> dict[str, Any] | None:
    return next((item for item in data["catalog"].get(category, []) if item.get("id") == item_id), None)


def active_items(data: dict[str, Any], category: str) -> list[dict[str, Any]]:
    return [item for item in data["catalog"].get(category, []) if item.get("active", True)]


def product_available(item: dict[str, Any]) -> bool:
    return item.get("active", True) and int(item.get("stock", -1)) != 0


def get_cart_items(data: dict[str, Any], user: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, qty in list(user.get("cart", {}).items()):
        try:
            category, item_id = split_item_key(key)
        except ValueError:
            continue
        item = get_item(data, category, item_id)
        if item and product_available(item):
            rows.append({"category": category, "item": item, "qty": max(1, int(qty))})
    return rows


def validate_promo(data: dict[str, Any], code: str) -> tuple[bool, str, dict[str, Any] | None]:
    code = code.strip().upper()
    promo = data.get("promo_codes", {}).get(code)
    if not promo:
        return False, "Промокод не найден.", None
    if not promo.get("active", True):
        return False, "Промокод выключен.", None
    max_uses = int(promo.get("max_uses", 0) or 0)
    if max_uses and int(promo.get("uses", 0)) >= max_uses:
        return False, "Лимит промокода уже исчерпан.", None
    return True, "Промокод применён.", promo


def calculate_total(data: dict[str, Any], raw_items: list[dict[str, Any]], user: dict[str, Any]) -> dict[str, Any]:
    subtotal = sum(int(row["item"].get("price", 0)) * int(row.get("qty", 1)) for row in raw_items)
    discount = 0
    promo_code = (user.get("active_promo") or "").strip().upper()
    promo = None
    if promo_code:
        ok, _, promo = validate_promo(data, promo_code)
        if ok and promo:
            if promo.get("type") == "percent":
                discount = subtotal * int(promo.get("amount", 0)) // 100
            else:
                discount = int(promo.get("amount", 0))
            discount = min(discount, subtotal)
        else:
            promo_code = ""
    total = max(0, subtotal - discount)
    return {"subtotal": subtotal, "discount": discount, "total": total, "promo_code": promo_code}


def mark_promo_used(data: dict[str, Any], code: str) -> None:
    if code and code in data.get("promo_codes", {}):
        data["promo_codes"][code]["uses"] = int(data["promo_codes"][code].get("uses", 0)) + 1


def find_user_by_referral_code(data: dict[str, Any], code: str) -> dict[str, Any] | None:
    code = code.strip()
    if not code:
        return None
    return next((user for user in data["users"].values() if user.get("referral_code") == code), None)


def apply_referral(data: dict[str, Any], user: dict[str, Any], payload: str | None) -> bool:
    if not payload or user.get("referred_by"):
        return False
    if not data["settings"].get("referral_enabled", True):
        return False
    code = payload.removeprefix("ref_").strip()
    referrer = find_user_by_referral_code(data, code)
    if not referrer or referrer["id"] == user["id"]:
        return False
    user["referred_by"] = referrer["id"]
    audit(data, user["id"], "referral_attached", referrer["id"])
    return True


def order_items_from_product(category: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "category": category,
            "item_id": item["id"],
            "title": item["title"],
            "price": int(item["price"]),
            "qty": 1,
        }
    ]


def create_order(
    data: dict[str, Any],
    user: dict[str, Any],
    rows: list[dict[str, Any]],
    payment_method: str,
    status: str,
) -> dict[str, Any]:
    totals = calculate_total(data, rows, user)
    order_id = generate_id("ORD")
    first = rows[0]
    items = [
        {
            "category": row["category"],
            "item_id": row["item"]["id"],
            "title": row["item"]["title"],
            "model": row["item"].get("model", ""),
            "price": int(row["item"]["price"]),
            "qty": int(row.get("qty", 1)),
        }
        for row in rows
    ]
    order = {
        "order_id": order_id,
        "user_id": user["id"],
        "username": user.get("username", ""),
        "city": first["category"],
        "item_id": first["item"]["id"],
        "item_title": first["item"]["title"] if len(items) == 1 else f"{len(items)} товаров",
        "scooter_model": first["item"].get("model", SCOOTER_MODEL),
        "items": items,
        "price": totals["subtotal"],
        "discount": totals["discount"],
        "total": totals["total"],
        "promo_code": totals["promo_code"],
        "status": status,
        "payment_method": payment_method,
        "payment_id": "",
        "purchase_counted": False,
        "referral_bonus_applied": False,
        "created_at": now_iso(),
        "confirmed_at": "",
        "paid_at": "",
        "delivered_at": "",
    }
    data["orders"][order_id] = order
    return order


def build_delivery_text(data: dict[str, Any], order: dict[str, Any]) -> str:
    blocks = []
    for row in order.get("items", []):
        item = get_item(data, row["category"], row["item_id"])
        delivery = (item or {}).get("delivery_text") or "Выдача для тарифа пока не настроена. Ответ появится здесь в боте."
        blocks.append(f"<b>{h(row['title'])}</b>\n{h(delivery)}")
    return "\n\n".join(blocks)


def paid_order_buttons(order_id: str) -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton("🛴 Арендовать самокат", callback_data=f"rent:start:{order_id}")],
        [InlineKeyboardButton("💬 Оставить отзыв", callback_data=f"review:add:{order_id}")],
        [InlineKeyboardButton("🧾 Мои поездки", callback_data="menu:orders")],
    ]


def complete_order(data: dict[str, Any], order: dict[str, Any], method: str) -> None:
    order["status"] = method
    order["confirmed_at"] = order.get("confirmed_at") or now_iso()
    order["paid_at"] = order.get("paid_at") or now_iso()
    order["delivered_at"] = order.get("delivered_at") or now_iso()
    order["delivery_text"] = build_delivery_text(data, order)

    if not order.get("purchase_counted"):
        user = data["users"].get(str(order["user_id"]))
        if user:
            user["purchases_count"] = int(user.get("purchases_count", 0)) + sum(int(row.get("qty", 1)) for row in order.get("items", []))
            user["agreed"] = True
            cashback = int(data["settings"].get("cashback_percent", 0) or 0)
            if cashback > 0:
                user["balance"] = int(user.get("balance", 0)) + int(order.get("total", 0)) * cashback // 100
            if data["settings"].get("referral_enabled", True) and user.get("referred_by") and not order.get("referral_bonus_applied"):
                referrer = data["users"].get(str(user["referred_by"]))
                bonus = int(data["settings"].get("referral_bonus", 0) or 0)
                if referrer and bonus > 0:
                    referrer["balance"] = int(referrer.get("balance", 0)) + bonus
                    order["referral_bonus_applied"] = True
                    audit(data, "system", "referral_bonus", f"{referrer['id']} | {bonus} | {order['order_id']}")
            user["active_promo"] = ""
        for row in order.get("items", []):
            item = get_item(data, row["category"], row["item_id"])
            if not item:
                continue
            item["sold_count"] = int(item.get("sold_count", 0)) + int(row.get("qty", 1))
            if int(item.get("stock", -1)) > 0:
                item["stock"] = max(0, int(item["stock"]) - int(row.get("qty", 1)))
            item["updated_at"] = now_iso()
        mark_promo_used(data, order.get("promo_code", ""))
        order["purchase_counted"] = True


def rows_with_home(rows: list[list[InlineKeyboardButton]], admin: bool = False, back: tuple[str, str] | None = None) -> InlineKeyboardMarkup:
    base = list(rows)
    if back:
        base.append([InlineKeyboardButton(back[0], callback_data=back[1])])
    base.append([InlineKeyboardButton("Главный экран", callback_data="menu:home")])
    if admin:
        base.append([InlineKeyboardButton("Админ-панель", callback_data="admin:panel")])
    return InlineKeyboardMarkup(base)


def main_menu(data: dict[str, Any], admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(button_label(data, "catalog"), callback_data="menu:catalog"), InlineKeyboardButton(button_label(data, "search"), callback_data="menu:search")],
        [InlineKeyboardButton(button_label(data, "cart"), callback_data="menu:cart"), InlineKeyboardButton(button_label(data, "favorites"), callback_data="menu:favorites")],
        [InlineKeyboardButton(button_label(data, "balance"), callback_data="menu:balance"), InlineKeyboardButton(button_label(data, "orders"), callback_data="menu:orders")],
        [InlineKeyboardButton(button_label(data, "reviews"), callback_data="menu:reviews"), InlineKeyboardButton(button_label(data, "support"), callback_data="menu:support")],
        [InlineKeyboardButton(button_label(data, "faq"), callback_data="menu:faq"), InlineKeyboardButton(button_label(data, "agreement"), callback_data="menu:agreement")],
    ]
    if admin:
        rows.append([InlineKeyboardButton(button_label(data, "admin"), callback_data="admin:panel")])
    return InlineKeyboardMarkup(rows)


def categories_keyboard(data: dict[str, Any], admin: bool) -> InlineKeyboardMarkup:
    rows = []
    for category, items in data["catalog"].items():
        count = len([item for item in items if product_available(item)])
        rows.append([InlineKeyboardButton(f"{category} ({count})", callback_data=f"cat:{category}")])
    return rows_with_home(rows, admin)


def products_keyboard(data: dict[str, Any], category: str, admin: bool) -> InlineKeyboardMarkup:
    rows = []
    for item in active_items(data, category):
        stock = "" if int(item.get("stock", -1)) < 0 else f" | ост. {item.get('stock', 0)}"
        rows.append([InlineKeyboardButton(f"{item['title']} | {money(item['price'], payment_currency(data))}{stock}", callback_data=f"product:{category}:{item['id']}")])
    if not rows:
        rows.append([InlineKeyboardButton("Нет активных товаров", callback_data="menu:catalog")])
    return rows_with_home(rows, admin, ("🏠 На главный экран", "menu:home"))


def product_keyboard(data: dict[str, Any], category: str, item: dict[str, Any], user: dict[str, Any], admin: bool) -> InlineKeyboardMarkup:
    key = item_key(category, item["id"])
    fav_text = "Убрать из избранного" if key in user.get("favorites", []) else "В избранное"
    rows = [
        [InlineKeyboardButton("💳 Оплатить тариф", callback_data=f"buy:{category}:{item['id']}"), InlineKeyboardButton("🧺 В корзину", callback_data=f"cart:add:{category}:{item['id']}")],
        [InlineKeyboardButton(f"⭐ {fav_text}", callback_data=f"fav:toggle:{category}:{item['id']}")],
        [InlineKeyboardButton("💬 Отзывы", callback_data=f"reviews:item:{category}:{item['id']}")],
    ]
    if admin:
        rows.append([InlineKeyboardButton("Редактировать товар", callback_data=f"admin:item:{category}:{item['id']}")])
    return rows_with_home(rows, admin, ("🛴 К аренде", "menu:catalog"))


def buy_keyboard(data: dict[str, Any], category: str, item_id: str, admin: bool) -> InlineKeyboardMarkup:
    rows = []
    rows.append([InlineKeyboardButton("🎟 Ввести промокод", callback_data="menu:promo")])
    if crypto_pay_ready(data):
        rows.append([InlineKeyboardButton("💎 Оплатить Crypto Bot", callback_data=f"pay:crypto:{category}:{item_id}")])
    if data["settings"].get("balance_enabled", True):
        rows.append([InlineKeyboardButton("💳 Оплатить балансом", callback_data=f"pay:balance:{category}:{item_id}")])
    if data["settings"].get("manual_payments_enabled", True):
        rows.append([InlineKeyboardButton("💳 Оплата по реквизитам", callback_data=f"pay:manual:{category}:{item_id}")])
    return rows_with_home(rows, admin, ("К товару", f"product:{category}:{item_id}"))


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Обзор", callback_data="admin:dashboard"), InlineKeyboardButton("Заказы", callback_data="admin:orders:all")],
        [InlineKeyboardButton("🧾 Оплаты аренды", callback_data="admin:manual_payments")],
        [InlineKeyboardButton("Каталог", callback_data="admin:catalog"), InlineKeyboardButton("Пользователи", callback_data="admin:users")],
        [InlineKeyboardButton("Оплата", callback_data="admin:payments"), InlineKeyboardButton("Промокоды", callback_data="admin:promos")],
        [InlineKeyboardButton("Рефералка", callback_data="admin:referrals"), InlineKeyboardButton("Рассылки", callback_data="admin:marketing")],
        [InlineKeyboardButton("Внешний вид", callback_data="admin:appearance"), InlineKeyboardButton("Тексты", callback_data="admin:content")],
        [InlineKeyboardButton("🛴 Аренды", callback_data="admin:rentals"), InlineKeyboardButton("💳 Пополнения", callback_data="admin:topups")],
        [InlineKeyboardButton("Поддержка", callback_data="admin:tickets")],
        [InlineKeyboardButton("Отзывы", callback_data="admin:reviews")],
        [InlineKeyboardButton("Система", callback_data="admin:settings"), InlineKeyboardButton("Аудит", callback_data="admin:audit")],
    ]
    return rows_with_home(rows, True)


async def send_or_edit(update: Update, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    text = text[:3900] + "\n\n..." if len(text) > 3900 else text
    query = update.callback_query
    if query:
        await query.answer()
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            await query.message.reply_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, data: dict[str, Any], text: str) -> None:
    if not data["settings"].get("notify_admins", True):
        return
    for admin_id in data.get("admins", []):
        try:
            await context.bot.send_message(chat_id=int(admin_id), text=text, parse_mode=ParseMode.HTML)
        except Exception as exc:
            logger.warning("Failed to notify admin %s: %s", admin_id, exc)


async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    if is_blocked(user):
        await send_or_edit(update, "Доступ к боту ограничен. Напишите в поддержку.", rows_with_home([], admin))
        return
    if data["settings"].get("maintenance_mode") and not admin:
        await send_or_edit(update, "Бот временно на обслуживании. Попробуйте позже.", rows_with_home([], admin))
        return

    text = f"<b>{h(data['settings'].get('shop_title', 'Digital Shop'))}</b>\n\n{h(data['settings']['main_screen_text'])}"
    photo = data["settings"].get("main_screen_photo")
    if update.message and photo:
        await update.message.reply_photo(photo=photo, caption=text, reply_markup=main_menu(data, admin), parse_mode=ParseMode.HTML)
    else:
        await send_or_edit(update, text, main_menu(data, admin))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_state(context)
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    payload = context.args[0] if context.args else ""
    if apply_referral(data, user, payload):
        save_data(data)
    await show_main(update, context)


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    action = update.callback_query.data

    if action == "menu:home":
        clear_state(context)
        await show_main(update, context)
        return
    if is_blocked(user):
        await send_or_edit(update, "Доступ к боту ограничен.", rows_with_home([], admin))
        return
    if action == "menu:catalog":
        category = first_catalog_category(data)
        await send_or_edit(update, "<b>🛴 Арендовать самокат</b>\n\nВыберите тариф аренды.", products_keyboard(data, category, admin))
        return
    if action == "menu:profile":
        text = (
            "<b>👤 Профиль</b>\n\n"
            f"ID: <code>{h(user['id'])}</code>\n"
            f"Username: @{h(user.get('username') or '-')}\n"
            f"Имя: {h(user.get('full_name') or '-')}\n"
            f"Покупок: <b>{int(user.get('purchases_count', 0))}</b>\n"
            f"Баланс: <b>{money(user.get('balance', 0), payment_currency(data))}</b>\n"
            f"Промокод: <b>{h(user.get('active_promo') or 'не применён')}</b>\n"
            f"Рассылки: <b>{'включены' if user.get('subscribed', True) else 'выключены'}</b>\n"
            f"Реф-код: <code>ref_{h(user.get('referral_code'))}</code>"
        )
        rows = [
            [InlineKeyboardButton("🎟 Ввести промокод", callback_data="menu:promo")],
            [InlineKeyboardButton("🔔 Переключить рассылки", callback_data="menu:toggle_subscribe")],
            [InlineKeyboardButton("💳 Пополнить по реквизитам", callback_data="menu:topup")],
        ]
        await send_or_edit(update, text, rows_with_home(rows, admin))
        return
    if action == "menu:toggle_subscribe":
        user["subscribed"] = not user.get("subscribed", True)
        save_data(data)
        await send_or_edit(update, "Настройка рассылок обновлена.", rows_with_home([[InlineKeyboardButton("Профиль", callback_data="menu:profile")]], admin))
        return
    if action == "menu:orders":
        orders = [order for order in data["orders"].values() if str(order.get("user_id")) == user["id"]]
        orders.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        rows = [
            [InlineKeyboardButton(f"{order['order_id']} | {status_label(order.get('status', ''))}", callback_data=f"order:{order['order_id']}")]
            for order in orders[:25]
        ]
        text = "<b>🧾 Мои поездки</b>" if rows else "<b>🧾 Мои поездки</b>\n\nПоездок пока нет."
        await send_or_edit(update, text, rows_with_home(rows, admin))
        return
    if action == "menu:reviews":
        reviews = [review for review in data["reviews"].values() if review.get("status", "approved") == "approved"]
        reviews.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        text = "<b>Последние отзывы</b>\n\n"
        text += "\n".join(
            f"• <b>{h(review.get('author_name', 'Пользователь'))}</b> — {h(review.get('item_title', 'товар'))}: {h(review.get('text', ''))}"
            for review in reviews[:15]
        ) or "Отзывов пока нет."
        await send_or_edit(update, text, rows_with_home([], admin))
        return
    if action == "menu:support":
        clear_state(context)
        context.user_data["state"] = {"name": "support"}
        await send_or_edit(update, "🛟 <b>Поддержка</b>\n\nНапишите вопрос следующим сообщением. Ответ появится здесь в боте.", rows_with_home([], admin))
        return
    if action == "menu:faq":
        await send_or_edit(update, f"<b>FAQ</b>\n\n{h(data['settings'].get('faq', 'FAQ пока не заполнен.'))}", rows_with_home([], admin))
        return
    if action == "menu:agreement":
        await send_or_edit(update, f"<b>Соглашение</b>\n\n{h(data.get('agreement', default_agreement()))}", rows_with_home([], admin))
        return
    if action == "menu:search":
        clear_state(context)
        context.user_data["state"] = {"name": "search"}
        await send_or_edit(update, "Напишите часть названия тарифа следующим сообщением.", rows_with_home([], admin))
        return
    if action == "menu:cart":
        await show_cart(update, context, data, user, admin)
        return
    if action == "menu:favorites":
        rows = []
        for key in user.get("favorites", []):
            try:
                category, item_id = split_item_key(key)
            except ValueError:
                continue
            item = get_item(data, category, item_id)
            if item and product_available(item):
                rows.append([InlineKeyboardButton(f"{item['title']} | {money(item['price'], payment_currency(data))}", callback_data=f"product:{category}:{item_id}")])
        text = "<b>Избранное</b>" if rows else "<b>Избранное</b>\n\nПока пусто."
        await send_or_edit(update, text, rows_with_home(rows, admin))
        return
    if action == "menu:balance":
        text = (
            "<b>💳 Баланс</b>\n\n"
            f"Баланс: <b>{money(user.get('balance', 0), payment_currency(data))}</b>\n"
            f"Пополнение: <b>по реквизитам и скрину оплаты</b>\n"
            f"Валюта: <b>{h(payment_currency(data))}</b>"
        )
        rows = [[InlineKeyboardButton("💳 Пополнить по реквизитам", callback_data="menu:topup")]]
        await send_or_edit(update, text, rows_with_home(rows, admin))
        return
    if action == "menu:topup":
        clear_state(context)
        context.user_data["state"] = {"name": "topup_amount"}
        await send_or_edit(update, f"<b>💳 Пополнение баланса</b>\n\nВведите сумму пополнения от {data['settings'].get('min_topup_amount', 10)} ₽.", rows_with_home([], admin))
        return
    if action == "menu:promo":
        clear_state(context)
        context.user_data["state"] = {"name": "promo"}
        await send_or_edit(update, "Введите промокод следующим сообщением.", rows_with_home([], admin))
        return


async def topup_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    state = context.user_data.get("state") or {}
    if update.callback_query.data == "topup:paid":
        if state.get("name") != "topup_wait_paid" or not state.get("amount"):
            await send_or_edit(update, "💳 Начните пополнение заново через профиль.", rows_with_home([[InlineKeyboardButton("👤 Профиль", callback_data="menu:profile")]], admin))
            return
        context.user_data["state"] = {"name": "topup_receipt", "amount": int(state["amount"])}
        await send_or_edit(
            update,
            "📸 <b>Скрин оплаты</b>\n\nОтправьте скрин оплаты одним фото. После этого заявка попадёт в раздел «💳 Пополнения».",
            rows_with_home([], admin),
        )
        return


async def category_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    category = update.callback_query.data.split(":", 1)[1]
    if category not in data["catalog"]:
        await send_or_edit(update, "Категория не найдена.", rows_with_home([], admin))
        return
    await send_or_edit(update, f"<b>🛴 {h(category)}</b>\n\nВыберите тариф аренды.", products_keyboard(data, category, admin))


async def product_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    _, category, item_id = update.callback_query.data.split(":", 2)
    item = get_item(data, category, item_id)
    if not item:
        await send_or_edit(update, "Товар не найден.", rows_with_home([], admin))
        return
    stock = "без ограничений" if int(item.get("stock", -1)) < 0 else str(item.get("stock", 0))
    text = (
        f"<b>{h(item['title'])}</b>\n\n"
        f"{h(item.get('description', ''))}\n\n"
        f"💰 Цена: <b>{money(item['price'], payment_currency(data))}</b>\n"
        f"🛴 Доступность: <b>{h(stock)}</b>\n"
        f"✅ Аренд оформлено: <b>{int(item.get('sold_count', 0))}</b>"
    )
    await send_or_edit(update, text, product_keyboard(data, category, item, user, admin))


async def favorite_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    _, _, category, item_id = update.callback_query.data.split(":", 3)
    item = get_item(data, category, item_id)
    if not item:
        await send_or_edit(update, "Товар не найден.", rows_with_home([], admin))
        return
    key = item_key(category, item_id)
    user.setdefault("favorites", [])
    if key in user["favorites"]:
        user["favorites"].remove(key)
        text = "Товар убран из избранного."
    else:
        user["favorites"].append(key)
        text = "Товар добавлен в избранное."
    save_data(data)
    await send_or_edit(update, text, rows_with_home([[InlineKeyboardButton("Вернуться к товару", callback_data=f"product:{category}:{item_id}")]], admin))


async def cart_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    parts = update.callback_query.data.split(":")
    action = parts[1]

    if action == "add":
        category, item_id = parts[2], parts[3]
        item = get_item(data, category, item_id)
        if not item or not product_available(item):
            await send_or_edit(update, "Товар недоступен.", rows_with_home([], admin))
            return
        key = item_key(category, item_id)
        user.setdefault("cart", {})
        user["cart"][key] = int(user["cart"].get(key, 0)) + 1
        save_data(data)
        await send_or_edit(
            update,
            "Товар добавлен в корзину.",
            rows_with_home(
                [
                    [InlineKeyboardButton("Открыть корзину", callback_data="menu:cart")],
                    [InlineKeyboardButton("Вернуться к товару", callback_data=f"product:{category}:{item_id}")],
                ],
                admin,
            ),
        )
        return
    if action in {"inc", "dec", "remove"}:
        category, item_id = parts[2], parts[3]
        key = item_key(category, item_id)
        cart = user.setdefault("cart", {})
        if action == "remove":
            cart.pop(key, None)
        elif action == "inc":
            cart[key] = int(cart.get(key, 0)) + 1
        elif action == "dec":
            cart[key] = max(0, int(cart.get(key, 0)) - 1)
            if cart[key] == 0:
                cart.pop(key, None)
        save_data(data)
        await show_cart(update, context, data, user, admin)
        return
    if action == "clear":
        user["cart"] = {}
        save_data(data)
        await show_cart(update, context, data, user, admin)
        return


async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict[str, Any], user: dict[str, Any], admin: bool) -> None:
    rows_data = get_cart_items(data, user)
    if not rows_data:
        await send_or_edit(update, "<b>Корзина</b>\n\nКорзина пуста.", rows_with_home([], admin))
        return
    totals = calculate_total(data, rows_data, user)
    lines = ["<b>Корзина</b>\n"]
    rows = []
    for row in rows_data:
        item = row["item"]
        category = row["category"]
        qty = int(row["qty"])
        lines.append(f"• {h(item['title'])} x{qty} — {money(int(item['price']) * qty, payment_currency(data))}")
        rows.append(
            [
                InlineKeyboardButton("-", callback_data=f"cart:dec:{category}:{item['id']}"),
                InlineKeyboardButton(f"x{qty}", callback_data=f"product:{category}:{item['id']}"),
                InlineKeyboardButton("+", callback_data=f"cart:inc:{category}:{item['id']}"),
                InlineKeyboardButton("Удалить", callback_data=f"cart:remove:{category}:{item['id']}"),
            ]
        )
    lines.append(f"\nИтого: <b>{money(totals['total'], payment_currency(data))}</b>")
    if totals["discount"]:
        lines.append(f"Скидка: <b>{money(totals['discount'], payment_currency(data))}</b>")
    if user.get("active_promo"):
        lines.append(f"Промокод: <b>{h(user['active_promo'])}</b>")
    if data["settings"].get("balance_enabled", True):
        rows.append([InlineKeyboardButton("💳 Оплатить корзину балансом", callback_data="pay:cart_balance")])
    if crypto_pay_ready(data):
        rows.append([InlineKeyboardButton("💎 Оплатить корзину Crypto Bot", callback_data="pay:cart_crypto")])
    rows.append([InlineKeyboardButton("🎟 Промокод", callback_data="menu:promo"), InlineKeyboardButton("🧹 Очистить", callback_data="cart:clear")])
    await send_or_edit(update, "\n".join(lines), rows_with_home(rows, admin))


async def buy_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    _, category, item_id = update.callback_query.data.split(":", 2)
    item = get_item(data, category, item_id)
    if not item or not product_available(item):
        await send_or_edit(update, "Товар недоступен.", rows_with_home([], admin))
        return
    totals = calculate_total(data, [{"category": category, "item": item, "qty": 1}], user)
    promo_line = f"\nПромокод: <b>{h(totals['promo_code'])}</b>, скидка {money(totals['discount'], payment_currency(data))}" if totals["discount"] else ""
    text = (
        "<b>Выберите способ оплаты</b>\n\n"
        f"🛴 Тариф: <b>{h(item['title'])}</b>\n"
        f"💰 Сумма: <b>{money(totals['total'], payment_currency(data))}</b>{promo_line}\n\n"
        "После оплаты появится кнопка «🛴 Арендовать самокат»."
    )
    await send_or_edit(update, text, buy_keyboard(data, category, item_id, admin))


async def pay_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    parts = update.callback_query.data.split(":")
    action = parts[1]

    if action in {"balance", "crypto", "manual"}:
        category, item_id = parts[2], parts[3]
        item = get_item(data, category, item_id)
        if not item or not product_available(item):
            await send_or_edit(update, "Товар недоступен.", rows_with_home([], admin))
            return
        rows = [{"category": category, "item": item, "qty": 1}]
    elif action in {"cart_balance", "cart_crypto"}:
        rows = get_cart_items(data, user)
        if not rows:
            await send_or_edit(update, "Корзина пуста.", rows_with_home([], admin))
            return
        action = {"cart_balance": "balance", "cart_crypto": "crypto"}[action]
    else:
        await send_or_edit(update, "Неизвестный способ оплаты.", rows_with_home([], admin))
        return

    totals = calculate_total(data, rows, user)
    if totals["total"] <= 0:
        await send_or_edit(update, "Сумма заказа должна быть больше нуля.", rows_with_home([], admin))
        return

    if action == "balance":
        if int(user.get("balance", 0)) < totals["total"]:
            await send_or_edit(update, "Недостаточно средств на балансе.", rows_with_home([[InlineKeyboardButton("Пополнить баланс", callback_data="menu:topup")]], admin))
            return
        order = create_order(data, user, rows, "balance", "paid_balance")
        user["balance"] = int(user.get("balance", 0)) - totals["total"]
        complete_order(data, order, "paid_balance")
        if len(rows) > 1:
            user["cart"] = {}
        audit(data, user["id"], "balance_payment", order["order_id"])
        save_data(data)
        await send_or_edit(
            update,
            f"<b>Оплата прошла успешно</b>\n\nID заказа: <code>{order['order_id']}</code>\n\n{order.get('delivery_text', '')}",
            rows_with_home(paid_order_buttons(order["order_id"]), admin),
        )
        await notify_admins(context, data, f"Новый оплаченный заказ балансом: <code>{order['order_id']}</code>")
        return

    if action == "manual":
        if not data["settings"].get("manual_payments_enabled", True):
            await send_or_edit(update, "Ручная оплата сейчас выключена.", rows_with_home([], admin))
            return
        order = create_order(data, user, rows, "manual", "awaiting_manual")
        order["payment_proof_status"] = "waiting_screenshot"
        audit(data, user["id"], "manual_order_created", order["order_id"])
        save_data(data)
        text = (
            "<b>🛴 Ручная оплата аренды</b>\n\n"
            f"ID заказа: <code>{order['order_id']}</code>\n"
            f"Сумма: <b>{money(order['total'], payment_currency(data))}</b>\n\n"
            f"<b>Реквизиты</b>\n{h(manual_payment_requisites(data))}\n\n"
            "После оплаты нажмите «✅ Я оплатил» и отправьте скрин оплаты. Заказ станет доступен после проверки."
        )
        await send_or_edit(
            update,
            text,
            rows_with_home(
                [
                    [InlineKeyboardButton("✅ Я оплатил", callback_data=f"manual_paid:{order['order_id']}")],
                    [InlineKeyboardButton("🧾 Мои поездки", callback_data="menu:orders")],
                ],
                admin,
            ),
        )
        return

    if action == "crypto":
        if not crypto_pay_ready(data):
            await send_or_edit(update, "Crypto Bot не настроен. Добавьте Crypto Pay API token в админке.", rows_with_home([], admin))
            return
        order = create_order(data, user, rows, "crypto_bot", "awaiting_crypto")
        save_data(data)
        await send_crypto_invoice_for_order(update, context, data, order)
        return


async def send_crypto_invoice_for_order(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: dict[str, Any],
    order: dict[str, Any],
) -> None:
    settings = data["settings"]
    payment_id = generate_id("CRP")
    payload = f"crypto:{payment_id}"
    currency_type = str(settings.get("crypto_pay_currency_type") or "fiat").lower()
    request_payload: dict[str, Any] = {
        "currency_type": currency_type,
        "amount": decimal_amount(order["total"]),
        "description": f"Заказ {order['order_id']}",
        "hidden_message": "Оплата получена. Вернитесь в бота и нажмите Проверить оплату.",
        "payload": payload,
        "allow_comments": False,
        "allow_anonymous": False,
        "expires_in": max(60, int(settings.get("crypto_pay_expires_minutes", 60) or 60) * 60),
    }
    if currency_type == "crypto":
        request_payload["asset"] = str(settings.get("crypto_pay_asset") or "USDT").upper()
    else:
        request_payload["fiat"] = str(settings.get("crypto_pay_fiat") or "RUB").upper()
        accepted = str(settings.get("crypto_pay_accepted_assets") or "").strip()
        if accepted:
            request_payload["accepted_assets"] = accepted

    try:
        invoice = await crypto_pay_request(data, "createInvoice", request_payload)
    except Exception as exc:
        order["status"] = "awaiting_manual"
        audit(data, order["user_id"], "crypto_invoice_error", str(exc))
        save_data(data)
        await send_or_edit(update, f"Crypto Bot не создал счёт: {h(exc)}", rows_with_home([[InlineKeyboardButton("К оплате", callback_data="menu:cart")]], is_admin(data, order["user_id"])))
        return

    invoice_id = str(invoice.get("invoice_id"))
    pay_url = invoice.get("bot_invoice_url") or invoice.get("mini_app_invoice_url") or invoice.get("web_app_invoice_url") or invoice.get("pay_url")
    data["payments"][payment_id] = {
        "payment_id": payment_id,
        "provider": "crypto_bot",
        "payload": payload,
        "kind": "order",
        "order_id": order["order_id"],
        "user_id": order["user_id"],
        "amount": int(order["total"]),
        "currency": crypto_pay_display_currency(data),
        "status": "pending",
        "invoice_id": invoice_id,
        "invoice_url": pay_url,
        "raw_invoice": invoice,
        "created_at": now_iso(),
    }
    order["payment_id"] = payment_id
    order["crypto_invoice_id"] = invoice_id
    audit(data, order["user_id"], "crypto_invoice_created", order["order_id"])
    save_data(data)
    rows = []
    if pay_url:
        rows.append([InlineKeyboardButton("Открыть оплату Crypto Bot", url=pay_url)])
    rows.append([InlineKeyboardButton("Проверить оплату", callback_data=f"crypto_check:{payment_id}")])
    rows.append([InlineKeyboardButton("Мои заказы", callback_data="menu:orders")])
    await send_or_edit(
        update,
        (
            "<b>Счёт Crypto Bot создан</b>\n\n"
            f"Заказ: <code>{h(order['order_id'])}</code>\n"
            f"Сумма: <b>{money(order['total'], crypto_pay_display_currency(data))}</b>\n\n"
            "После оплаты нажмите Проверить оплату. Бот также будет проверять ожидающие счета автоматически, если установлен job-queue."
        ),
        rows_with_home(rows, is_admin(data, order["user_id"])),
    )


async def send_crypto_invoice_for_topup(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict[str, Any], user: dict[str, Any], amount: int) -> None:
    settings = data["settings"]
    payment_id = generate_id("CRP")
    payload = f"crypto:{payment_id}"
    currency_type = str(settings.get("crypto_pay_currency_type") or "fiat").lower()
    request_payload: dict[str, Any] = {
        "currency_type": currency_type,
        "amount": decimal_amount(amount),
        "description": f"Пополнение баланса {user['id']}",
        "hidden_message": "Оплата получена. Вернитесь в бота и нажмите Проверить оплату.",
        "payload": payload,
        "allow_comments": False,
        "allow_anonymous": False,
        "expires_in": max(60, int(settings.get("crypto_pay_expires_minutes", 60) or 60) * 60),
    }
    if currency_type == "crypto":
        request_payload["asset"] = str(settings.get("crypto_pay_asset") or "USDT").upper()
    else:
        request_payload["fiat"] = str(settings.get("crypto_pay_fiat") or "RUB").upper()
        accepted = str(settings.get("crypto_pay_accepted_assets") or "").strip()
        if accepted:
            request_payload["accepted_assets"] = accepted
    invoice = await crypto_pay_request(data, "createInvoice", request_payload)
    invoice_id = str(invoice.get("invoice_id"))
    pay_url = invoice.get("bot_invoice_url") or invoice.get("mini_app_invoice_url") or invoice.get("web_app_invoice_url") or invoice.get("pay_url")
    data["payments"][payment_id] = {
        "payment_id": payment_id,
        "provider": "crypto_bot",
        "payload": payload,
        "kind": "topup",
        "order_id": "",
        "user_id": user["id"],
        "amount": amount,
        "currency": crypto_pay_display_currency(data),
        "status": "pending",
        "invoice_id": invoice_id,
        "invoice_url": pay_url,
        "raw_invoice": invoice,
        "created_at": now_iso(),
    }
    audit(data, user["id"], "crypto_topup_invoice_created", payment_id)
    save_data(data)
    rows = []
    if pay_url:
        rows.append([InlineKeyboardButton("Открыть оплату Crypto Bot", url=pay_url)])
    rows.append([InlineKeyboardButton("Проверить оплату", callback_data=f"crypto_check:{payment_id}")])
    await update.message.reply_text(
        f"Счёт Crypto Bot создан на {money(amount, crypto_pay_display_currency(data))}.",
        reply_markup=rows_with_home(rows, is_admin(data, user["id"])),
    )


async def refresh_crypto_payment(data: dict[str, Any], context: ContextTypes.DEFAULT_TYPE, payment_id: str) -> tuple[bool, str]:
    payment = data.get("payments", {}).get(payment_id)
    if not payment or payment.get("provider") != "crypto_bot":
        return False, "Платёж не найден."
    if payment.get("status") == "paid":
        return True, "Платёж уже обработан."
    invoice_id = payment.get("invoice_id")
    if not invoice_id:
        return False, "У платежа нет invoice_id."
    result = await crypto_pay_request(data, "getInvoices", {"invoice_ids": str(invoice_id), "count": 1})
    invoices = result.get("items", result) if isinstance(result, dict) else result
    if not invoices:
        return False, "Счёт не найден в Crypto Bot."
    invoice = invoices[0]
    payment["raw_invoice"] = invoice
    crypto_status = invoice.get("status")
    payment["crypto_status"] = crypto_status
    if crypto_status != "paid":
        save_data(data)
        return False, f"Пока не оплачено. Статус Crypto Bot: {crypto_status}."

    payment["status"] = "paid"
    payment["paid_at"] = invoice.get("paid_at") or now_iso()
    payment["paid_asset"] = invoice.get("paid_asset") or invoice.get("asset")
    payment["paid_amount"] = invoice.get("paid_amount") or invoice.get("amount")
    payment["fee_asset"] = invoice.get("fee_asset", "")
    payment["fee_amount"] = invoice.get("fee_amount", "")

    if payment.get("kind") == "order":
        order = data["orders"].get(payment.get("order_id"))
        if not order:
            payment["status"] = "paid_without_order"
            save_data(data)
            return False, "Оплата есть, но заказ не найден."
        complete_order(data, order, "paid_crypto")
        order["payment_id"] = payment_id
        user = data["users"].get(str(order["user_id"]))
        if user:
            user["cart"] = {}
        audit(data, order["user_id"], "crypto_payment_success", order["order_id"])
        save_data(data)
        try:
            await context.bot.send_message(
                chat_id=int(order["user_id"]),
                text=f"Оплата Crypto Bot подтверждена.\n\nЗаказ <code>{h(order['order_id'])}</code>\n\n{order.get('delivery_text', '')}",
                parse_mode=ParseMode.HTML,
                reply_markup=rows_with_home(paid_order_buttons(order["order_id"]), is_admin(data, order["user_id"])),
            )
        except Exception:
            pass
        return True, "Оплата подтверждена, заказ выдан."

    if payment.get("kind") == "topup":
        target = data["users"].get(str(payment["user_id"]))
        if target:
            target["balance"] = int(target.get("balance", 0)) + int(payment["amount"])
        audit(data, payment["user_id"], "crypto_topup_success", payment_id)
        save_data(data)
        return True, "Баланс пополнен."

    save_data(data)
    return True, "Платёж подтверждён."


async def crypto_check_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    payment_id = update.callback_query.data.split(":", 1)[1]
    payment = data.get("payments", {}).get(payment_id)
    if not payment:
        await send_or_edit(update, "Платёж не найден.", rows_with_home([], admin))
        return
    if str(payment.get("user_id")) != user["id"] and not admin:
        await send_or_edit(update, "Нет доступа к этому платежу.", rows_with_home([], admin))
        return
    try:
        paid, message = await refresh_crypto_payment(data, context, payment_id)
    except Exception as exc:
        await send_or_edit(update, f"Ошибка проверки Crypto Bot: {h(exc)}", rows_with_home([[InlineKeyboardButton("Повторить", callback_data=f"crypto_check:{payment_id}")]], admin))
        return
    rows = []
    if not paid:
        invoice_url = payment.get("invoice_url")
        if invoice_url:
            rows.append([InlineKeyboardButton("Открыть оплату", url=invoice_url)])
        rows.append([InlineKeyboardButton("Проверить ещё раз", callback_data=f"crypto_check:{payment_id}")])
    rows.append([InlineKeyboardButton("Мои заказы", callback_data="menu:orders")])
    await send_or_edit(update, message, rows_with_home(rows, admin))


async def crypto_auto_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    pending = [
        payment_id
        for payment_id, payment in data.get("payments", {}).items()
        if payment.get("provider") == "crypto_bot" and payment.get("status") == "pending"
    ]
    for payment_id in pending[:20]:
        try:
            await refresh_crypto_payment(data, context, payment_id)
        except Exception as exc:
            logger.warning("Crypto auto-check failed for %s: %s", payment_id, exc)


async def order_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    order_id = update.callback_query.data.split(":", 1)[1]
    order = data["orders"].get(order_id)
    if not order or (order.get("user_id") != user["id"] and not admin):
        await send_or_edit(update, "Заказ не найден.", rows_with_home([], admin))
        return
    lines = [
        "<b>Заказ</b>",
        f"ID: <code>{h(order['order_id'])}</code>",
        f"Статус: <b>{h(status_label(order.get('status', '')))}</b>",
        f"Сумма: <b>{money(order.get('total', order.get('price', 0)), payment_currency(data))}</b>",
        "",
        "<b>Состав</b>",
    ]
    for row in order.get("items", []):
        lines.append(f"• {h(row['title'])} x{row.get('qty', 1)}")
    if normalize_status(order.get("status", "")) in PAID_STATUSES and order.get("delivery_text"):
        lines.extend(["", "<b>Выдача</b>", order["delivery_text"]])
    rows = []
    if normalize_status(order.get("status", "")) in PAID_STATUSES:
        rows.append([InlineKeyboardButton("🛴 Арендовать самокат", callback_data=f"rent:start:{order_id}")])
        if not any(review.get("order_id") == order_id for review in data["reviews"].values()):
            rows.append([InlineKeyboardButton("💬 Оставить отзыв", callback_data=f"review:add:{order_id}")])
    if normalize_status(order.get("status", "")) == "awaiting_manual":
        if order.get("payment_proof_status") == "new":
            lines.extend(["", "📸 Скрин оплаты уже отправлен на проверку."])
        else:
            rows.append([InlineKeyboardButton("✅ Я оплатил", callback_data=f"manual_paid:{order_id}")])
    payment = data.get("payments", {}).get(order.get("payment_id", ""))
    if payment and payment.get("provider") == "crypto_bot" and payment.get("status") == "pending":
        invoice_url = payment.get("invoice_url")
        if invoice_url:
            rows.append([InlineKeyboardButton("Открыть оплату Crypto Bot", url=invoice_url)])
        rows.append([InlineKeyboardButton("Проверить оплату", callback_data=f"crypto_check:{payment['payment_id']}")])
    if normalize_status(order.get("status", "")) in PENDING_STATUSES:
        rows.append([InlineKeyboardButton("Отменить заказ", callback_data=f"order_cancel:{order_id}")])
    if admin:
        rows.append([InlineKeyboardButton("Открыть в админке", callback_data=f"admin:view_order:{order_id}")])
    await send_or_edit(update, "\n".join(lines), rows_with_home(rows, admin))


async def order_cancel_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    order_id = update.callback_query.data.split(":", 1)[1]
    order = data["orders"].get(order_id)
    if not order or order.get("user_id") != user["id"]:
        await send_or_edit(update, "Заказ не найден.", rows_with_home([], admin))
        return
    if normalize_status(order.get("status", "")) not in PENDING_STATUSES:
        await send_or_edit(update, "Можно отменить только ожидающий заказ.", rows_with_home([], admin))
        return
    order["status"] = "cancelled"
    audit(data, user["id"], "order_cancelled", order_id)
    save_data(data)
    await send_or_edit(update, "Заказ отменён.", rows_with_home([[InlineKeyboardButton("Мои заказы", callback_data="menu:orders")]], admin))


async def notify_rental_request(context: ContextTypes.DEFAULT_TYPE, data: dict[str, Any], rental: dict[str, Any]) -> None:
    text = (
        "<b>🛴 Новая заявка на аренду</b>\n\n"
        f"ID: <code>{h(rental['request_id'])}</code>\n"
        f"Заказ: <code>{h(rental['order_id'])}</code>\n"
        f"Пользователь: <code>{h(rental['user_id'])}</code> @{h(rental.get('username') or '-')}\n"
        f"Модель: <b>{h(rental.get('scooter_model') or SCOOTER_MODEL)}</b>\n"
        f"Заряд: <b>{h(rental.get('battery_percent', '-'))}%</b>\n"
        f"Данные самоката: <b>{h(rental.get('scooter_code') or 'QR-фото')}</b>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Доступен", callback_data=f"admin:rental_ok:{rental['request_id']}"),
                InlineKeyboardButton("❌ Невозможно", callback_data=f"admin:rental_fail:{rental['request_id']}"),
            ],
            [InlineKeyboardButton("🛴 Открыть заявку", callback_data=f"admin:rental:{rental['request_id']}")],
        ]
    )
    for admin_id in data.get("admins", []):
        try:
            await context.bot.send_message(chat_id=int(admin_id), text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            if rental.get("qr_photo_id"):
                await context.bot.send_photo(chat_id=int(admin_id), photo=rental["qr_photo_id"], caption=f"QR для заявки {rental['request_id']}")
        except Exception as exc:
            logger.warning("Failed to notify rental admin %s: %s", admin_id, exc)


async def save_rental_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: dict[str, Any],
    user: dict[str, Any],
    order_id: str,
    scooter_code: str = "",
    qr_photo_id: str = "",
) -> None:
    order = data["orders"].get(order_id)
    if not order or order.get("user_id") != user["id"] or normalize_status(order.get("status", "")) not in PAID_STATUSES:
        await update.message.reply_text("🛴 Для аренды нужен оплаченный заказ.")
        return
    scooter_code = normalize_scooter_code(scooter_code)
    if not qr_photo_id and not is_valid_scooter_code(scooter_code):
        await update.message.reply_text(scooter_code_help_table(), parse_mode=ParseMode.HTML)
        return
    request_id = generate_id("RNT")
    rental = {
        "request_id": request_id,
        "order_id": order_id,
        "user_id": user["id"],
        "username": user.get("username", ""),
        "scooter_model": SCOOTER_MODEL,
        "scooter_code": scooter_code[:300],
        "qr_photo_id": qr_photo_id,
        "battery_percent": random.randint(50, 97),
        "status": "awaiting_activation",
        "created_at": now_iso(),
        "activated_at": "",
        "resolved_at": "",
    }
    data.setdefault("rental_requests", {})[request_id] = rental
    audit(data, user["id"], "rental_found", request_id)
    save_data(data)
    clear_state(context)
    await update.message.reply_text(
        scooter_found_text(rental),
        parse_mode=ParseMode.HTML,
        reply_markup=rows_with_home(
            [[InlineKeyboardButton("🛴 Активировать самокат", callback_data=f"rent:activate:{request_id}")]],
            is_admin(data, user["id"]),
        ),
    )


async def notify_topup_request(context: ContextTypes.DEFAULT_TYPE, data: dict[str, Any], ticket: dict[str, Any]) -> None:
    text = (
        "<b>💳 Новый чек пополнения</b>\n\n"
        f"ID: <code>{h(ticket['ticket_id'])}</code>\n"
        f"Пользователь: <code>{h(ticket['user_id'])}</code> @{h(ticket.get('username') or '-')}\n"
        f"Сумма: <b>{money(ticket.get('amount', 0), payment_currency(data))}</b>\n"
        f"Чек: <b>{'фото' if ticket.get('receipt_photo_id') else 'текст'}</b>"
    )
    if ticket.get("receipt_text"):
        text += f"\n\n{h(ticket.get('receipt_text', ''))}"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Начислить", callback_data=f"admin:topup_approve:{ticket['ticket_id']}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"admin:topup_reject:{ticket['ticket_id']}"),
            ],
            [InlineKeyboardButton("💳 Открыть пополнение", callback_data=f"admin:ticket:{ticket['ticket_id']}")],
        ]
    )
    for admin_id in data.get("admins", []):
        try:
            if ticket.get("receipt_photo_id"):
                await context.bot.send_photo(
                    chat_id=int(admin_id),
                    photo=ticket["receipt_photo_id"],
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await context.bot.send_message(chat_id=int(admin_id), text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception as exc:
            logger.warning("Failed to notify topup admin %s: %s", admin_id, exc)


async def save_topup_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: dict[str, Any],
    user: dict[str, Any],
    amount: int,
    receipt_text: str = "",
    receipt_photo_id: str = "",
) -> None:
    if amount <= 0:
        clear_state(context)
        await update.message.reply_text("💳 Сумма пополнения не найдена. Начните пополнение заново через профиль.")
        return
    if not receipt_photo_id:
        await update.message.reply_text("📸 Для подтверждения пополнения нужен скрин оплаты фотографией.")
        return
    ticket_id = generate_id("TOP")
    data["tickets"][ticket_id] = {
        "ticket_id": ticket_id,
        "type": "topup",
        "amount": amount,
        "user_id": user["id"],
        "username": user.get("username", ""),
        "text": f"Пополнение баланса на {amount} ₽",
        "receipt_text": receipt_text[:1500],
        "receipt_photo_id": receipt_photo_id,
        "status": "new",
        "created_at": now_iso(),
    }
    audit(data, user["id"], "topup_receipt_created", ticket_id)
    save_data(data)
    clear_state(context)
    await update.message.reply_text(
        "💳 <b>Чек принят</b>\n\nЗаявка на пополнение отправлена на проверку. Баланс обновится после подтверждения.",
        parse_mode=ParseMode.HTML,
        reply_markup=rows_with_home([[InlineKeyboardButton("👤 Профиль", callback_data="menu:profile")]], is_admin(data, user["id"])),
    )
    await notify_topup_request(context, data, data["tickets"][ticket_id])


async def notify_manual_order_receipt(context: ContextTypes.DEFAULT_TYPE, data: dict[str, Any], order: dict[str, Any]) -> None:
    text = (
        "<b>🛴 Новый скрин оплаты аренды</b>\n\n"
        f"Заказ: <code>{h(order['order_id'])}</code>\n"
        f"Пользователь: <code>{h(order['user_id'])}</code> @{h(order.get('username') or '-')}\n"
        f"Сумма: <b>{money(order.get('total', order.get('price', 0)), payment_currency(data))}</b>"
    )
    if order.get("payment_proof_text"):
        text += f"\n\n{h(order.get('payment_proof_text', ''))}"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin:confirm:{order['order_id']}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"admin:reject:{order['order_id']}"),
            ],
            [InlineKeyboardButton("🧾 Открыть заказ", callback_data=f"admin:view_order:{order['order_id']}")],
        ]
    )
    for admin_id in data.get("admins", []):
        try:
            await context.bot.send_photo(
                chat_id=int(admin_id),
                photo=order["payment_proof_photo_id"],
                caption=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.warning("Failed to notify manual order admin %s: %s", admin_id, exc)


async def save_manual_order_receipt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: dict[str, Any],
    user: dict[str, Any],
    order_id: str,
    receipt_photo_id: str,
    receipt_text: str = "",
) -> None:
    order = data["orders"].get(order_id)
    if not order or order.get("user_id") != user["id"] or normalize_status(order.get("status", "")) != "awaiting_manual":
        clear_state(context)
        await update.message.reply_text("🧾 Заказ для подтверждения оплаты не найден.")
        return
    order["payment_proof_photo_id"] = receipt_photo_id
    order["payment_proof_text"] = receipt_text[:1500]
    order["payment_proof_status"] = "new"
    order["payment_proof_at"] = now_iso()
    audit(data, user["id"], "manual_order_receipt_created", order_id)
    save_data(data)
    clear_state(context)
    await update.message.reply_text(
        "📸 <b>Скрин оплаты принят</b>\n\nЗаказ отправлен на проверку. После подтверждения появится кнопка аренды.",
        parse_mode=ParseMode.HTML,
        reply_markup=rows_with_home([[InlineKeyboardButton("🧾 Мои поездки", callback_data="menu:orders")]], is_admin(data, user["id"])),
    )
    await notify_manual_order_receipt(context, data, order)


async def manual_payment_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    order_id = update.callback_query.data.split(":", 1)[1]
    order = data["orders"].get(order_id)
    if not order or order.get("user_id") != user["id"] or normalize_status(order.get("status", "")) != "awaiting_manual":
        await send_or_edit(update, "🧾 Заказ для ручной оплаты не найден.", rows_with_home([], admin))
        return
    clear_state(context)
    context.user_data["state"] = {"name": "manual_order_receipt", "order_id": order_id}
    await send_or_edit(
        update,
        "📸 <b>Скрин оплаты аренды</b>\n\nОтправьте скрин оплаты одним фото. После проверки заказ станет доступен для аренды.",
        rows_with_home([], admin, ("🧾 К заказу", f"order:{order_id}")),
    )


async def rental_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    parts = update.callback_query.data.split(":")
    if parts[:2] == ["rent", "activate"]:
        request_id = parts[2]
        rental = data.get("rental_requests", {}).get(request_id)
        if not rental or rental.get("user_id") != user["id"]:
            await send_or_edit(update, "🛴 Самокат не найден. Отправьте номер ещё раз.", rows_with_home([], admin))
            return
        if rental.get("status") != "awaiting_activation":
            await send_or_edit(update, "🛴 Заявка уже отправлена.", rows_with_home([[InlineKeyboardButton("🧾 Мои поездки", callback_data="menu:orders")]], admin))
            return
        rental["status"] = "new"
        rental["activated_at"] = now_iso()
        audit(data, user["id"], "rental_activation_requested", request_id)
        save_data(data)
        await send_or_edit(
            update,
            "🛴 <b>Активация отправлена</b>\n\nМы проверим доступность самоката. Ответ появится здесь.",
            rows_with_home([[InlineKeyboardButton("🧾 Мои поездки", callback_data="menu:orders")]], admin),
        )
        await notify_rental_request(context, data, rental)
        return
    if parts[:2] == ["rent", "start"]:
        order_id = parts[2]
        order = data["orders"].get(order_id)
        if not order or order.get("user_id") != user["id"] or normalize_status(order.get("status", "")) not in PAID_STATUSES:
            await send_or_edit(update, "🛴 Для аренды нужен оплаченный заказ.", rows_with_home([], admin))
            return
        clear_state(context)
        context.user_data["state"] = {"name": "rental_request", "order_id": order_id}
        await send_or_edit(
            update,
            "<b>🛴 Арендовать самокат</b>\n\nОтправьте номер самоката текстом или фото QR-кода.",
            rows_with_home([], admin, ("🧾 К заказу", f"order:{order_id}")),
        )


async def reviews_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    admin = is_admin(data, update.effective_user.id)
    parts = update.callback_query.data.split(":")
    if parts[:2] == ["reviews", "item"]:
        category, item_id = parts[2], parts[3]
        item = get_item(data, category, item_id)
        reviews = [
            review
            for review in data["reviews"].values()
            if review.get("category", review.get("city")) == category and review.get("item_id") == item_id and review.get("status", "approved") == "approved"
        ]
        text = f"<b>Отзывы о {h(item['title'] if item else 'товаре')}</b>\n\n"
        text += "\n".join(f"• <b>{h(review.get('author_name', 'Пользователь'))}</b>: {h(review.get('text', ''))}" for review in reviews[:15]) or "Пока отзывов нет."
        await send_or_edit(update, text, rows_with_home([], admin, ("К товару", f"product:{category}:{item_id}")))
        return
    if parts[:2] == ["review", "add"]:
        order_id = parts[2]
        order = data["orders"].get(order_id)
        if not order or order.get("user_id") != str(update.effective_user.id):
            await send_or_edit(update, "Нельзя оставить отзыв для этого заказа.", rows_with_home([], admin))
            return
        if normalize_status(order.get("status", "")) not in PAID_STATUSES:
            await send_or_edit(update, "Отзыв можно оставить только после оплаты.", rows_with_home([], admin))
            return
        if any(review.get("order_id") == order_id for review in data["reviews"].values()):
            await send_or_edit(update, "Отзыв по этому заказу уже оставлен.", rows_with_home([], admin))
            return
        clear_state(context)
        context.user_data["state"] = {"name": "review", "order_id": order_id}
        await send_or_edit(update, "Напишите отзыв следующим сообщением.", rows_with_home([], admin, ("К заказу", f"order:{order_id}")))


def dashboard_text(data: dict[str, Any]) -> str:
    orders = list(data["orders"].values())
    users = list(data["users"].values())
    paid_orders = [order for order in orders if normalize_status(order.get("status", "")) in PAID_STATUSES]
    pending_orders = [order for order in orders if normalize_status(order.get("status", "")) in PENDING_STATUSES]
    revenue = sum(int(order.get("total", order.get("price", 0))) for order in paid_orders)
    products = sum(len(items) for items in data["catalog"].values())
    active_products = sum(1 for items in data["catalog"].values() for item in items if item.get("active", True))
    return (
        "<b>Дашборд</b>\n\n"
        f"Пользователи: <b>{len(users)}</b>\n"
        f"Заказы: <b>{len(orders)}</b>\n"
        f"Ожидают оплаты: <b>{len(pending_orders)}</b>\n"
        f"Оплачены: <b>{len(paid_orders)}</b>\n"
        f"Оборот: <b>{money(revenue, payment_currency(data))}</b>\n"
        f"Категории: <b>{len(data['catalog'])}</b>\n"
        f"Товары: <b>{active_products}/{products}</b>\n"
        f"Платежи: <b>{len(data.get('payments', {}))}</b>\n"
        f"Тикеты: <b>{len(data.get('tickets', {}))}</b>"
    )


def users_list_keyboard(data: dict[str, Any]) -> InlineKeyboardMarkup:
    users = list(data["users"].values())
    users.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    rows = [
        [InlineKeyboardButton(f"@{user.get('username') or '-'} | {user.get('full_name', '-')[:20]}", callback_data=f"admin:user:{user['id']}")]
        for user in users[:30]
    ]
    rows.append([InlineKeyboardButton("Поиск пользователя", callback_data="admin:search_user")])
    return rows_with_home(rows, True)


def export_orders_csv(data: dict[str, Any]) -> Path:
    EXPORT_DIR.mkdir(exist_ok=True)
    path = EXPORT_DIR / f"orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["order_id", "user_id", "username", "status", "total", "payment_method", "created_at", "paid_at", "items"])
        for order in data["orders"].values():
            items = "; ".join(f"{row.get('title')} x{row.get('qty', 1)}" for row in order.get("items", []))
            writer.writerow(
                [
                    order.get("order_id"),
                    order.get("user_id"),
                    order.get("username"),
                    status_label(order.get("status", "")),
                    order.get("total", order.get("price", 0)),
                    order.get("payment_method"),
                    order.get("created_at"),
                    order.get("paid_at"),
                    items,
                ]
            )
    return path


async def admin_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    if not is_admin(data, update.effective_user.id):
        await send_or_edit(update, "Нет доступа.", rows_with_home([], False))
        return
    parts = update.callback_query.data.split(":")
    action = update.callback_query.data

    if action == "admin:panel":
        clear_state(context)
        await send_or_edit(update, "<b>Админ-панель</b>\n\nВсе основные действия доступны кнопками.", admin_panel_keyboard())
        return
    if action == "admin:dashboard":
        await send_or_edit(update, dashboard_text(data), rows_with_home([[InlineKeyboardButton("Экспорт заказов CSV", callback_data="admin:export_orders")]], True))
        return
    if action == "admin:export_orders":
        path = export_orders_csv(data)
        audit(data, user["id"], "export_orders", str(path))
        save_data(data)
        await send_or_edit(update, f"CSV экспорт создан:\n<code>{h(path.resolve())}</code>", rows_with_home([], True))
        return
    if action == "admin:manual_payments":
        orders = [
            order for order in data["orders"].values()
            if order.get("payment_method") == "manual" and order.get("payment_proof_status")
        ]
        orders.sort(
            key=lambda order: (
                {"new": 0, "waiting_screenshot": 1, "confirmed": 2, "rejected": 3}.get(order.get("payment_proof_status"), 4),
                order.get("payment_proof_at") or order.get("created_at", ""),
            ),
        )
        rows = [
            [
                InlineKeyboardButton(
                    f"{payment_proof_label(order.get('payment_proof_status'))} | {order['order_id']} | {money(order.get('total', 0), payment_currency(data))}",
                    callback_data=f"admin:view_order:{order['order_id']}",
                )
            ]
            for order in orders[:40]
        ]
        text = (
            "<b>🧾 Оплаты аренды</b>\n\n"
            "📸 новый скрин — можно проверять\n"
            "⏳ ждёт скрин — пользователь ещё не отправил фото\n"
            "✅ подтверждено / ❌ отклонено — обработано"
        )
        await send_or_edit(update, text, rows_with_home(rows, True, ("🛠 К админке", "admin:panel")))
        return
    if parts[:2] == ["admin", "orders"]:
        filter_name = parts[2] if len(parts) > 2 else "all"
        orders = list(data["orders"].values())
        if filter_name == "pending":
            orders = [order for order in orders if normalize_status(order.get("status", "")) in PENDING_STATUSES]
        elif filter_name == "paid":
            orders = [order for order in orders if normalize_status(order.get("status", "")) in PAID_STATUSES]
        elif filter_name == "rejected":
            orders = [order for order in orders if normalize_status(order.get("status", "")) in {"rejected", "cancelled", "refunded"}]
        orders.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        rows = [
            [InlineKeyboardButton(f"{order['order_id']} | {status_label(order.get('status', ''))}", callback_data=f"admin:view_order:{order['order_id']}")]
            for order in orders[:30]
        ]
        rows.append(
            [
                InlineKeyboardButton("Все", callback_data="admin:orders:all"),
                InlineKeyboardButton("Ожидают", callback_data="admin:orders:pending"),
                InlineKeyboardButton("Оплачены", callback_data="admin:orders:paid"),
            ]
        )
        rows.append([InlineKeyboardButton("Отклонённые", callback_data="admin:orders:rejected")])
        await send_or_edit(update, f"<b>Заказы</b>\n\nФильтр: {h(filter_name)}", rows_with_home(rows, True))
        return
    if parts[:2] == ["admin", "view_order"]:
        order = data["orders"].get(parts[2])
        if not order:
            await send_or_edit(update, "Заказ не найден.", admin_panel_keyboard())
            return
        lines = [
            "<b>Заказ</b>",
            f"ID: <code>{h(order['order_id'])}</code>",
            f"Пользователь: <code>{h(order.get('user_id'))}</code> @{h(order.get('username') or '-')}",
            f"Статус: <b>{h(status_label(order.get('status', '')))}</b>",
            f"Сумма: <b>{money(order.get('total', order.get('price', 0)), payment_currency(data))}</b>",
            f"Метод: <b>{h(order.get('payment_method', '-'))}</b>",
            f"Промокод: <b>{h(order.get('promo_code') or '-')}</b>",
        ]
        if order.get("payment_method") == "manual":
            lines.append(f"Скрин оплаты: <b>{h(payment_proof_label(order.get('payment_proof_status')))}</b>")
        if order.get("payment_proof_text"):
            lines.extend(["", h(order.get("payment_proof_text", ""))])
        rows = []
        if order.get("payment_method") == "manual" and normalize_status(order.get("status", "")) == "awaiting_manual":
            if order.get("payment_proof_photo_id"):
                rows.append(
                    [
                        InlineKeyboardButton("✅ Подтвердить оплату", callback_data=f"admin:confirm:{order['order_id']}"),
                        InlineKeyboardButton("❌ Отклонить оплату", callback_data=f"admin:reject:{order['order_id']}"),
                    ]
                )
            else:
                rows.append([InlineKeyboardButton("⏳ Ждём скрин оплаты", callback_data=f"admin:view_order:{order['order_id']}")])
        else:
            rows.extend(
                [
                    [InlineKeyboardButton("Подтвердить", callback_data=f"admin:confirm:{order['order_id']}"), InlineKeyboardButton("Отклонить", callback_data=f"admin:reject:{order['order_id']}")],
                    [InlineKeyboardButton("Отметить возврат", callback_data=f"admin:refund:{order['order_id']}")],
                ]
            )
        back = ("🧾 К оплатам аренды", "admin:manual_payments") if order.get("payment_method") == "manual" else ("К заказам", "admin:orders:all")
        await send_or_edit(update, "\n".join(lines), rows_with_home(rows, True, back))
        if order.get("payment_proof_photo_id"):
            try:
                await update.callback_query.message.reply_photo(photo=order["payment_proof_photo_id"], caption=f"Скрин оплаты {order['order_id']}")
            except Exception:
                pass
        return
    if parts[:2] in (["admin", "confirm"], ["admin", "reject"], ["admin", "refund"]):
        order = data["orders"].get(parts[2])
        if not order:
            await send_or_edit(update, "Заказ не найден.", admin_panel_keyboard())
            return
        if parts[1] == "confirm":
            if order.get("payment_method") == "manual" and normalize_status(order.get("status", "")) == "awaiting_manual" and not order.get("payment_proof_photo_id"):
                await send_or_edit(update, "Сначала пользователь должен отправить скрин оплаты через кнопку «Я оплатил».", rows_with_home([[InlineKeyboardButton("К заказу", callback_data=f"admin:view_order:{order['order_id']}")]], True))
                return
            complete_order(data, order, "paid")
            if order.get("payment_method") == "manual":
                order["payment_proof_status"] = "confirmed"
            audit(data, user["id"], "order_confirmed", order["order_id"])
            save_data(data)
            try:
                await context.bot.send_message(
                    chat_id=int(order["user_id"]),
                    text=f"✅ <b>Оплата подтверждена</b>\n\nЗаказ <code>{h(order['order_id'])}</code>\n\n{order.get('delivery_text', '')}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=rows_with_home(paid_order_buttons(order["order_id"]), False),
                )
            except Exception:
                pass
            if order.get("payment_method") == "manual":
                await send_or_edit(
                    update,
                    "Оплата аренды подтверждена.",
                    rows_with_home([[InlineKeyboardButton("🧾 К оплатам аренды", callback_data="admin:manual_payments")]], True),
                )
            else:
                await send_or_edit(update, "Заказ подтверждён.", admin_panel_keyboard())
        elif parts[1] == "reject":
            order["status"] = "rejected"
            if order.get("payment_method") == "manual":
                order["payment_proof_status"] = "rejected"
            audit(data, user["id"], "order_rejected", order["order_id"])
            save_data(data)
            try:
                await context.bot.send_message(
                    chat_id=int(order["user_id"]),
                    text=f"❌ <b>Оплата не подтверждена</b>\n\nЗаказ <code>{h(order['order_id'])}</code> отклонён. Создайте новый заказ или обратитесь в поддержку внутри бота.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=rows_with_home([[InlineKeyboardButton("🛟 Поддержка", callback_data="menu:support")]], False),
                )
            except Exception:
                pass
            if order.get("payment_method") == "manual":
                await send_or_edit(
                    update,
                    "Оплата аренды отклонена.",
                    rows_with_home([[InlineKeyboardButton("🧾 К оплатам аренды", callback_data="admin:manual_payments")]], True),
                )
            else:
                await send_or_edit(update, "Заказ отклонён.", admin_panel_keyboard())
        else:
            order["status"] = "refunded"
            audit(data, user["id"], "order_refunded_marked", order["order_id"])
            save_data(data)
            await send_or_edit(update, "Локальная отметка возврата поставлена.", admin_panel_keyboard())
        return
    if action == "admin:users":
        await send_or_edit(update, "<b>Пользователи</b>", users_list_keyboard(data))
        return
    if action == "admin:search_user":
        clear_state(context)
        context.user_data["state"] = {"name": "admin_search_user"}
        await send_or_edit(update, "Введите username или ID пользователя.", rows_with_home([], True))
        return
    if parts[:2] == ["admin", "user"]:
        await show_admin_user(update, data, parts[2])
        return
    if parts[:2] == ["admin", "toggle_block"]:
        target = data["users"].get(parts[2])
        if target:
            target["blocked"] = not target.get("blocked", False)
            audit(data, user["id"], "toggle_block_user", parts[2])
            save_data(data)
        await show_admin_user(update, data, parts[2])
        return
    if parts[:2] == ["admin", "toggle_admin"]:
        target_id = parts[2]
        if target_id == user["id"]:
            await send_or_edit(update, "Нельзя снять админку с самого себя.", rows_with_home([], True))
            return
        if target_id in data["admins"]:
            data["admins"].remove(target_id)
            audit(data, user["id"], "remove_admin", target_id)
        else:
            data["admins"].append(target_id)
            audit(data, user["id"], "make_admin", target_id)
        save_data(data)
        await show_admin_user(update, data, target_id)
        return
    if parts[:2] == ["admin", "balance"]:
        clear_state(context)
        context.user_data["state"] = {"name": f"admin_{parts[2]}_balance", "user_id": parts[3]}
        verb = "начисления" if parts[2] == "add" else "списания"
        await send_or_edit(update, f"Введите сумму {verb}.", rows_with_home([], True))
        return
    if parts[:2] == ["admin", "reply_user"]:
        clear_state(context)
        context.user_data["state"] = {"name": "admin_reply_user", "user_id": parts[2]}
        await send_or_edit(update, "Введите сообщение пользователю.", rows_with_home([], True))
        return
    if parts[:2] == ["admin", "note_user"]:
        clear_state(context)
        context.user_data["state"] = {"name": "admin_note_user", "user_id": parts[2]}
        await send_or_edit(update, "Введите заметку о пользователе.", rows_with_home([], True))
        return
    if action == "admin:catalog":
        rows = [[InlineKeyboardButton(category, callback_data=f"admin:category:{category}")] for category in data["catalog"]]
        rows.append([InlineKeyboardButton("Добавить категорию", callback_data="admin:add_category")])
        await send_or_edit(update, "<b>Каталог</b>", rows_with_home(rows, True))
        return
    if action == "admin:add_category":
        clear_state(context)
        context.user_data["state"] = {"name": "admin_add_category"}
        await send_or_edit(update, "Введите название новой категории.", rows_with_home([], True))
        return
    if parts[:2] == ["admin", "category"]:
        category = parts[2]
        rows = [
            [InlineKeyboardButton("Добавить товар", callback_data=f"admin:add_item:{category}")],
            [InlineKeyboardButton("Переименовать", callback_data=f"admin:rename_category:{category}"), InlineKeyboardButton("Удалить", callback_data=f"admin:delete_category:{category}")],
        ]
        for item in data["catalog"].get(category, []):
            marker = "on" if item.get("active", True) else "off"
            rows.append([InlineKeyboardButton(f"{marker} {item['title']} | {money(item['price'], payment_currency(data))}", callback_data=f"admin:item:{category}:{item['id']}")])
        await send_or_edit(update, f"<b>Категория: {h(category)}</b>", rows_with_home(rows, True, ("К каталогу", "admin:catalog")))
        return
    if parts[:2] == ["admin", "rename_category"]:
        clear_state(context)
        context.user_data["state"] = {"name": "admin_rename_category", "category": parts[2]}
        await send_or_edit(update, "Введите новое название категории.", rows_with_home([], True))
        return
    if parts[:2] == ["admin", "delete_category"]:
        category = parts[2]
        data["catalog"].pop(category, None)
        audit(data, user["id"], "delete_category", category)
        save_data(data)
        await send_or_edit(update, "Категория удалена.", admin_panel_keyboard())
        return
    if parts[:2] == ["admin", "add_item"]:
        clear_state(context)
        context.user_data["state"] = {"name": "admin_add_item", "category": parts[2]}
        await send_or_edit(update, "Введите товар в формате:\nНазвание|Цена|Описание", rows_with_home([], True))
        return
    if parts[:2] == ["admin", "item"]:
        category, item_id = parts[2], parts[3]
        item = get_item(data, category, item_id)
        if not item:
            await send_or_edit(update, "Товар не найден.", admin_panel_keyboard())
            return
        text = (
            "<b>Редактирование товара</b>\n\n"
            f"ID: <code>{h(item['id'])}</code>\n"
            f"Название: <b>{h(item['title'])}</b>\n"
            f"Цена: <b>{money(item['price'], payment_currency(data))}</b>\n"
            f"Остаток: <b>{h(item.get('stock', -1))}</b>\n"
            f"Активен: <b>{'да' if item.get('active', True) else 'нет'}</b>\n"
            f"Продано: <b>{int(item.get('sold_count', 0))}</b>\n\n"
            f"{h(item.get('description', ''))}"
        )
        rows = [
            [InlineKeyboardButton("Название", callback_data=f"admin:item_edit:title:{category}:{item_id}"), InlineKeyboardButton("Описание", callback_data=f"admin:item_edit:description:{category}:{item_id}")],
            [InlineKeyboardButton("Цена", callback_data=f"admin:item_edit:price:{category}:{item_id}"), InlineKeyboardButton("Остаток", callback_data=f"admin:item_edit:stock:{category}:{item_id}")],
            [InlineKeyboardButton("Фото", callback_data=f"admin:item_edit:photo:{category}:{item_id}"), InlineKeyboardButton("Выдача", callback_data=f"admin:item_edit:delivery_text:{category}:{item_id}")],
            [InlineKeyboardButton("Вкл/выкл", callback_data=f"admin:item_toggle:{category}:{item_id}"), InlineKeyboardButton("Дублировать", callback_data=f"admin:item_duplicate:{category}:{item_id}")],
            [InlineKeyboardButton("Удалить", callback_data=f"admin:item_delete:{category}:{item_id}")],
        ]
        await send_or_edit(update, text, rows_with_home(rows, True, ("К категории", f"admin:category:{category}")))
        return
    if parts[:2] == ["admin", "item_edit"]:
        field, category, item_id = parts[2], parts[3], parts[4]
        clear_state(context)
        context.user_data["state"] = {"name": "admin_item_edit", "field": field, "category": category, "item_id": item_id}
        prompts = {
            "title": "Введите новое название.",
            "description": "Введите новое описание.",
            "price": "Введите новую цену числом.",
            "stock": "Введите остаток числом. -1 значит без ограничений.",
            "photo": "Отправьте фото товара.",
            "delivery_text": "Введите текст выдачи после оплаты.",
        }
        await send_or_edit(update, prompts.get(field, "Введите новое значение."), rows_with_home([], True))
        return
    if parts[:2] == ["admin", "item_toggle"]:
        item = get_item(data, parts[2], parts[3])
        if item:
            item["active"] = not item.get("active", True)
            item["updated_at"] = now_iso()
            audit(data, user["id"], "toggle_item", item["id"])
            save_data(data)
        await send_or_edit(update, "Статус товара обновлён.", rows_with_home([[InlineKeyboardButton("К товару", callback_data=f"admin:item:{parts[2]}:{parts[3]}")]], True))
        return
    if parts[:2] == ["admin", "item_duplicate"]:
        category, item_id = parts[2], parts[3]
        item = get_item(data, category, item_id)
        if item:
            copy_item = deepcopy(item)
            copy_item["id"] = generate_id("ITEM")
            copy_item["title"] = f"{copy_item['title']} копия"
            copy_item["created_at"] = now_iso()
            copy_item["updated_at"] = now_iso()
            data["catalog"][category].append(copy_item)
            audit(data, user["id"], "duplicate_item", item_id)
            save_data(data)
        await send_or_edit(update, "Товар продублирован.", rows_with_home([[InlineKeyboardButton("К категории", callback_data=f"admin:category:{category}")]], True))
        return
    if parts[:2] == ["admin", "item_delete"]:
        category, item_id = parts[2], parts[3]
        data["catalog"][category] = [item for item in data["catalog"].get(category, []) if item["id"] != item_id]
        audit(data, user["id"], "delete_item", item_id)
        save_data(data)
        await send_or_edit(update, "Товар удалён.", rows_with_home([[InlineKeyboardButton("К категории", callback_data=f"admin:category:{category}")]], True))
        return
    if action == "admin:payments":
        settings = data["settings"]
        text = (
            "<b>Платежи</b>\n\n"
            f"Crypto Bot: <b>{'готов' if crypto_pay_ready(data) else 'не настроен'}</b>\n"
            f"Crypto API: <code>{h(crypto_pay_api_url(data))}</code>\n"
            f"Crypto валюта: <b>{h(crypto_pay_display_currency(data))}</b>\n"
            f"Crypto token: <b>{'задан' if crypto_pay_token(data) else 'не задан'}</b>\n\n"
            f"Ручная оплата: <b>{'вкл' if settings.get('manual_payments_enabled') else 'выкл'}</b>\n"
            f"Баланс: <b>{'вкл' if settings.get('balance_enabled') else 'выкл'}</b>\n"
            f"Валюта баланса: <b>{h(payment_currency(data))}</b>\n"
            f"Реквизиты аренды: <b>{h(manual_payment_requisites(data))}</b>\n\n"
            f"<b>Реквизиты пополнения</b>\n{h(settings.get('topup_requisites') or '')}"
        )
        rows = [
            [InlineKeyboardButton("Crypto Bot вкл/выкл", callback_data="admin:toggle_setting:crypto_pay_enabled"), InlineKeyboardButton("Crypto token", callback_data="admin:set_crypto_token")],
            [InlineKeyboardButton("Crypto test/main", callback_data="admin:set_crypto_api_url"), InlineKeyboardButton("Crypto валюта", callback_data="admin:set_crypto_currency")],
            [InlineKeyboardButton("Проверить Crypto API", callback_data="admin:crypto_test")],
            [InlineKeyboardButton("Ручная", callback_data="admin:toggle_setting:manual_payments_enabled")],
            [InlineKeyboardButton("Баланс", callback_data="admin:toggle_setting:balance_enabled"), InlineKeyboardButton("Валюта", callback_data="admin:set_currency")],
            [InlineKeyboardButton("Реквизиты аренды", callback_data="admin:set_manual_requisites")],
            [InlineKeyboardButton("Реквизиты пополнения", callback_data="admin:set_topup_requisites")],
            [InlineKeyboardButton("Журнал платежей", callback_data="admin:payment_log")],
        ]
        await send_or_edit(update, text, rows_with_home(rows, True))
        return
    if action == "admin:crypto_test":
        try:
            app_info = await crypto_pay_request(data, "getMe")
            await send_or_edit(update, f"Crypto Bot подключен.\n\n<code>{h(app_info)}</code>", rows_with_home([], True, ("К оплате", "admin:payments")))
        except Exception as exc:
            await send_or_edit(update, f"Crypto Bot не отвечает: {h(exc)}", rows_with_home([], True, ("К оплате", "admin:payments")))
        return
    if action == "admin:payment_log":
        payments = list(data.get("payments", {}).values())
        payments.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        text = "<b>Журнал платежей</b>\n\n" + (
            "\n".join(f"• {h(p['payment_id'])} | {h(p.get('provider', 'manual'))} | {h(p.get('kind'))} | {h(p.get('status'))} | {money(p.get('amount', 0), p.get('currency', payment_currency(data)))}" for p in payments[:25])
            or "Платежей пока нет."
        )
        await send_or_edit(update, text, rows_with_home([], True))
        return
    if parts[:2] == ["admin", "toggle_setting"]:
        key = parts[2]
        if key in data["settings"] and isinstance(data["settings"][key], bool):
            data["settings"][key] = not data["settings"][key]
            audit(data, user["id"], "toggle_setting", key)
            save_data(data)
        await send_or_edit(update, "Настройка переключена.", rows_with_home([[InlineKeyboardButton("К платежам", callback_data="admin:payments"), InlineKeyboardButton("К настройкам", callback_data="admin:settings")]], True))
        return
    if action in {"admin:set_currency", "admin:set_payment_contact", "admin:set_payment_channel", "admin:set_crypto_token", "admin:set_crypto_api_url", "admin:set_crypto_currency", "admin:set_topup_requisites", "admin:set_manual_requisites"}:
        prompts = {
            "admin:set_currency": ("admin_set_currency", "Введите валюту баланса, например RUB."),
            "admin:set_payment_contact": ("admin_set_payment_contact", "Введите username для ручной оплаты, например @unison_off."),
            "admin:set_payment_channel": ("admin_set_payment_channel", "Введите ссылку на канал/реквизиты."),
            "admin:set_crypto_token": ("admin_set_crypto_token", "Введите Crypto Pay API token из @CryptoBot → Crypto Pay → Create App."),
            "admin:set_crypto_api_url": ("admin_set_crypto_api_url", "Введите API URL: https://pay.crypt.bot или https://testnet-pay.crypt.bot"),
            "admin:set_crypto_currency": ("admin_set_crypto_currency", "Введите RUB для fiat-счёта или crypto:USDT / crypto:TON для счёта в криптовалюте."),
            "admin:set_topup_requisites": ("admin_set_topup_requisites", "Введите реквизиты для пополнения баланса. Пользователь увидит этот текст после ввода суммы."),
            "admin:set_manual_requisites": ("admin_set_manual_requisites", "Введите реквизиты для ручной оплаты аренды. Пользователь увидит их перед кнопкой «Я оплатил»."),
        }
        state_name, prompt = prompts[action]
        clear_state(context)
        context.user_data["state"] = {"name": state_name}
        await send_or_edit(update, prompt, rows_with_home([], True))
        return
    if action == "admin:promos":
        rows = [[InlineKeyboardButton(f"{code} | {promo.get('amount')}{'%' if promo.get('type') == 'percent' else ''}", callback_data=f"admin:promo:{code}")] for code, promo in data.get("promo_codes", {}).items()]
        rows.append([InlineKeyboardButton("Создать промокод", callback_data="admin:create_promo")])
        await send_or_edit(update, "<b>Промокоды</b>", rows_with_home(rows, True))
        return
    if action == "admin:create_promo":
        clear_state(context)
        context.user_data["state"] = {"name": "admin_create_promo"}
        await send_or_edit(update, "Введите: CODE|percent|10|100 или CODE|fixed|50|20", rows_with_home([], True))
        return
    if parts[:2] == ["admin", "promo"]:
        code = parts[2]
        promo = data.get("promo_codes", {}).get(code)
        if not promo:
            await send_or_edit(update, "Промокод не найден.", rows_with_home([], True))
            return
        text = (
            "<b>Промокод</b>\n\n"
            f"Код: <code>{h(code)}</code>\n"
            f"Тип: <b>{h(promo.get('type'))}</b>\n"
            f"Размер: <b>{h(promo.get('amount'))}</b>\n"
            f"Активен: <b>{'да' if promo.get('active', True) else 'нет'}</b>\n"
            f"Использований: <b>{promo.get('uses', 0)}/{promo.get('max_uses', 0)}</b>"
        )
        rows = [
            [InlineKeyboardButton("Вкл/выкл", callback_data=f"admin:promo_toggle:{code}")],
            [InlineKeyboardButton("Удалить", callback_data=f"admin:promo_delete:{code}")],
        ]
        await send_or_edit(update, text, rows_with_home(rows, True, ("К промокодам", "admin:promos")))
        return
    if parts[:2] == ["admin", "promo_toggle"]:
        promo = data.get("promo_codes", {}).get(parts[2])
        if promo:
            promo["active"] = not promo.get("active", True)
            audit(data, user["id"], "toggle_promo", parts[2])
            save_data(data)
        await send_or_edit(update, "Промокод обновлён.", rows_with_home([[InlineKeyboardButton("К промокоду", callback_data=f"admin:promo:{parts[2]}")]], True))
        return
    if parts[:2] == ["admin", "promo_delete"]:
        data.get("promo_codes", {}).pop(parts[2], None)
        audit(data, user["id"], "delete_promo", parts[2])
        save_data(data)
        await send_or_edit(update, "Промокод удалён.", rows_with_home([[InlineKeyboardButton("К промокодам", callback_data="admin:promos")]], True))
        return
    if action == "admin:marketing":
        text = (
            "<b>Рассылки</b>\n\n"
            "Здесь можно отправить сообщение всем пользователям или только тем, кто не отключил рассылки."
        )
        rows = [
            [InlineKeyboardButton("Рассылка всем", callback_data="admin:broadcast:all"), InlineKeyboardButton("Подписчикам", callback_data="admin:broadcast:subscribed")],
        ]
        await send_or_edit(update, text, rows_with_home(rows, True, ("К админке", "admin:panel")))
        return
    if action == "admin:referrals":
        text = (
            "<b>Реферальная система</b>\n\n"
            f"Статус: <b>{'включена' if data['settings'].get('referral_enabled', True) else 'выключена'}</b>\n"
            f"Бонус рефереру: <b>{money(data['settings'].get('referral_bonus', 0), payment_currency(data))}</b>\n"
            f"Кэшбек покупателю: <b>{data['settings'].get('cashback_percent', 0)}%</b>\n\n"
            "Пользователь видит свой код в профиле. Формат запуска: <code>/start ref_CODE</code>."
        )
        rows = [
            [InlineKeyboardButton("Рефералка вкл/выкл", callback_data="admin:toggle_setting:referral_enabled")],
            [InlineKeyboardButton("Бонус рефереру", callback_data="admin:set_ref_bonus"), InlineKeyboardButton("Кэшбек", callback_data="admin:set_cashback")],
        ]
        await send_or_edit(update, text, rows_with_home(rows, True, ("К админке", "admin:panel")))
        return
    if parts[:2] == ["admin", "broadcast"]:
        clear_state(context)
        context.user_data["state"] = {"name": "admin_broadcast", "target": parts[2]}
        await send_or_edit(update, "Введите текст рассылки.", rows_with_home([], True))
        return
    if action in {"admin:set_cashback", "admin:set_ref_bonus"}:
        clear_state(context)
        context.user_data["state"] = {"name": "admin_set_cashback" if action.endswith("cashback") else "admin_set_ref_bonus"}
        await send_or_edit(update, "Введите число.", rows_with_home([], True))
        return
    if action == "admin:rentals":
        rentals = [rental for rental in data.get("rental_requests", {}).values() if rental.get("status") != "awaiting_activation"]
        rentals.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        rows = [
            [InlineKeyboardButton(f"{r['request_id']} | {r.get('status', 'new')} | {r.get('scooter_code') or 'QR'}", callback_data=f"admin:rental:{r['request_id']}")]
            for r in rentals[:30]
        ]
        await send_or_edit(update, "<b>🛴 Заявки на аренду</b>", rows_with_home(rows, True, ("🛠 К админке", "admin:panel")))
        return
    if parts[:2] == ["admin", "rental"]:
        rental = data.get("rental_requests", {}).get(parts[2])
        if not rental:
            await send_or_edit(update, "Заявка не найдена.", rows_with_home([], True, ("🛴 К арендам", "admin:rentals")))
            return
        text = (
            "<b>🛴 Заявка на аренду</b>\n\n"
            f"ID: <code>{h(rental['request_id'])}</code>\n"
            f"Заказ: <code>{h(rental.get('order_id'))}</code>\n"
            f"Пользователь: <code>{h(rental.get('user_id'))}</code> @{h(rental.get('username') or '-')}\n"
            f"Статус: <b>{h(rental.get('status', 'new'))}</b>\n"
            f"Модель: <b>{h(rental.get('scooter_model') or SCOOTER_MODEL)}</b>\n"
            f"Заряд: <b>{h(rental.get('battery_percent', '-'))}%</b>\n"
            f"Самокат/QR: <b>{h(rental.get('scooter_code') or 'QR-фото')}</b>"
        )
        rows = [
            [
                InlineKeyboardButton("✅ Доступен", callback_data=f"admin:rental_ok:{rental['request_id']}"),
                InlineKeyboardButton("❌ Невозможно", callback_data=f"admin:rental_fail:{rental['request_id']}"),
            ],
        ]
        await send_or_edit(update, text, rows_with_home(rows, True, ("🛴 К арендам", "admin:rentals")))
        return
    if parts[:2] in (["admin", "rental_ok"], ["admin", "rental_fail"]):
        rental = data.get("rental_requests", {}).get(parts[2])
        if not rental:
            await send_or_edit(update, "Заявка не найдена.", rows_with_home([], True, ("🛴 К арендам", "admin:rentals")))
            return
        ok = parts[1] == "rental_ok"
        rental["status"] = "ready" if ok else "failed"
        rental["resolved_at"] = now_iso()
        rental["resolved_by"] = user["id"]
        audit(data, user["id"], "rental_ready" if ok else "rental_failed", rental["request_id"])
        save_data(data)
        try:
            if ok:
                await context.bot.send_message(
                    chat_id=int(rental["user_id"]),
                    text="✅ <b>Самокат готов к аренде</b>\n\n🛴 Приятной поездки!",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await context.bot.send_message(
                    chat_id=int(rental["user_id"]),
                    text="🚫 <b>Невозможно арендовать самокат</b>\n\nПопробуйте другой номер или QR позже.",
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            pass
        await send_or_edit(update, "Статус заявки обновлён.", rows_with_home([[InlineKeyboardButton("🛴 К арендам", callback_data="admin:rentals")]], True))
        return
    if action == "admin:appearance":
        text = (
            "<b>Внешний вид</b>\n\n"
            f"Название: <b>{h(data['settings'].get('shop_title'))}</b>\n"
            "Можно менять название магазина, главный текст/фото и подписи кнопок главного меню."
        )
        rows = [
            [InlineKeyboardButton("Название", callback_data="admin:edit_shop_title"), InlineKeyboardButton("Главный текст", callback_data="admin:edit_main_text")],
            [InlineKeyboardButton("Главное фото", callback_data="admin:edit_main_photo")],
            [InlineKeyboardButton("Кнопки главного меню", callback_data="admin:buttons")],
        ]
        await send_or_edit(update, text, rows_with_home(rows, True, ("К админке", "admin:panel")))
        return
    if action == "admin:buttons":
        rows = [
            [InlineKeyboardButton(f"{key}: {data['settings']['buttons'].get(key, value)}", callback_data=f"admin:button_edit:{key}")]
            for key, value in BUTTON_DEFAULTS.items()
        ]
        await send_or_edit(update, "<b>Кнопки главного меню</b>\n\nВыберите кнопку, чтобы изменить подпись.", rows_with_home(rows, True, ("К внешнему виду", "admin:appearance")))
        return
    if parts[:2] == ["admin", "button_edit"]:
        key = parts[2]
        if key not in BUTTON_DEFAULTS:
            await send_or_edit(update, "Кнопка не найдена.", rows_with_home([], True, ("К кнопкам", "admin:buttons")))
            return
        clear_state(context)
        context.user_data["state"] = {"name": "admin_edit_button", "key": key}
        await send_or_edit(update, f"Введите новую подпись для кнопки {h(key)}.", rows_with_home([], True, ("К кнопкам", "admin:buttons")))
        return
    if action == "admin:tickets":
        tickets = [ticket for ticket in data["tickets"].values() if ticket.get("type", "support") == "support"]
        tickets.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        rows = [
            [
                InlineKeyboardButton(
                    f"{ticket_status_emoji(ticket.get('status', 'new'))} {ticket['ticket_id']} | @{ticket.get('username') or '-'}",
                    callback_data=f"admin:ticket:{ticket['ticket_id']}",
                )
            ]
            for ticket in tickets[:30]
        ]
        text = "<b>🛟 Поддержка</b>\n\n🔔 новое сообщение · 🟢 открыт · 🔒 закрыт"
        await send_or_edit(update, text, rows_with_home(rows, True))
        return
    if action == "admin:topups":
        tickets = [ticket for ticket in data["tickets"].values() if ticket.get("type") == "topup"]
        tickets.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        rows = [
            [
                InlineKeyboardButton(
                    f"{ticket_status_emoji(ticket.get('status', 'new'))} {ticket['ticket_id']} | {money(ticket.get('amount', 0), payment_currency(data))}",
                    callback_data=f"admin:ticket:{ticket['ticket_id']}",
                )
            ]
            for ticket in tickets[:30]
        ]
        text = "<b>💳 Пополнения баланса</b>\n\n🆕 новый чек · 🔒 закрыт"
        await send_or_edit(update, text, rows_with_home(rows, True))
        return
    if parts[:2] == ["admin", "ticket"]:
        ticket = data["tickets"].get(parts[2])
        if not ticket:
            await send_or_edit(update, "Тикет не найден.", rows_with_home([], True))
            return
        ticket_type = ticket.get("type", "support")
        back = ("💳 К пополнениям", "admin:topups") if ticket_type == "topup" else ("🛟 К поддержке", "admin:tickets")
        rows = []
        if ticket_type == "topup":
            text = (
                "<b>💳 Пополнение баланса</b>\n\n"
                f"ID: <code>{h(ticket['ticket_id'])}</code>\n"
                f"Пользователь: <code>{h(ticket['user_id'])}</code> @{h(ticket.get('username') or '-')}\n"
                f"Статус: <b>{ticket_status_emoji(ticket.get('status', 'new'))} {h(ticket.get('status', 'new'))}</b>\n"
                f"Сумма: <b>{money(ticket.get('amount', 0), payment_currency(data))}</b>\n"
                f"Чек: <b>{'фото' if ticket.get('receipt_photo_id') else 'текст'}</b>\n\n"
                f"{h(ticket.get('receipt_text') or ticket.get('text', ''))}"
            )
        else:
            if ticket.get("status") == "new_message":
                ticket["status"] = "open"
                save_data(data)
            messages = ticket.get("messages") or [{"role": "user", "text": ticket.get("text", ""), "created_at": ticket.get("created_at", "")}]
            history = []
            for message in messages[-8:]:
                icon = "🛠" if message.get("role") == "admin" else "👤"
                history.append(f"{icon} {h(message.get('text', ''))}")
            text = (
                "<b>🛟 Тикет поддержки</b>\n\n"
                f"ID: <code>{h(ticket['ticket_id'])}</code>\n"
                f"Пользователь: <code>{h(ticket['user_id'])}</code> @{h(ticket.get('username') or '-')}\n"
                f"Статус: <b>{ticket_status_emoji(ticket.get('status', 'open'))} {h(ticket.get('status', 'open'))}</b>\n\n"
                + ("\n\n".join(history) or h(ticket.get("text", "")))
            )
            rows.append([InlineKeyboardButton("🛟 Ответить в тикет", callback_data=f"admin:ticket_reply:{ticket['ticket_id']}")])
        if ticket_type == "topup" and ticket.get("status") != "closed":
            rows.append(
                [
                    InlineKeyboardButton("💳 Начислить", callback_data=f"admin:topup_approve:{ticket['ticket_id']}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"admin:topup_reject:{ticket['ticket_id']}"),
                ]
            )
        rows.append([InlineKeyboardButton("🔒 Закрыть", callback_data=f"admin:ticket_close:{ticket['ticket_id']}")])
        await send_or_edit(update, text, rows_with_home(rows, True, back))
        if ticket_type == "topup" and ticket.get("receipt_photo_id"):
            try:
                await update.callback_query.message.reply_photo(photo=ticket["receipt_photo_id"], caption=f"Чек {ticket['ticket_id']}")
            except Exception:
                pass
        return
    if parts[:2] == ["admin", "ticket_reply"]:
        ticket = data["tickets"].get(parts[2])
        if not ticket or ticket.get("type", "support") != "support":
            await send_or_edit(update, "Тикет поддержки не найден.", rows_with_home([], True, ("🛟 К поддержке", "admin:tickets")))
            return
        clear_state(context)
        context.user_data["state"] = {"name": "admin_ticket_reply", "ticket_id": ticket["ticket_id"]}
        await send_or_edit(update, "Введите ответ пользователю. Сообщение придёт ему внутри бота.", rows_with_home([], True, ("🛟 К тикету", f"admin:ticket:{ticket['ticket_id']}")))
        return
    if parts[:2] in (["admin", "topup_approve"], ["admin", "topup_reject"]):
        ticket = data["tickets"].get(parts[2])
        if not ticket or ticket.get("type") != "topup":
            await send_or_edit(update, "Заявка пополнения не найдена.", rows_with_home([], True, ("💳 К пополнениям", "admin:topups")))
            return
        approve = parts[1] == "topup_approve"
        target = data["users"].get(str(ticket["user_id"]))
        if approve and target:
            amount = int(ticket.get("amount", 0) or 0)
            target["balance"] = int(target.get("balance", 0)) + amount
            ticket["status"] = "closed"
            ticket["resolved_at"] = now_iso()
            audit(data, user["id"], "topup_approved", f"{ticket['ticket_id']} | {amount}")
            save_data(data)
            try:
                await context.bot.send_message(chat_id=int(ticket["user_id"]), text=f"💳 <b>Баланс пополнен</b>\n\nНачислено: <b>{amount} ₽</b>", parse_mode=ParseMode.HTML)
            except Exception:
                pass
            await send_or_edit(update, "Баланс начислен.", rows_with_home([[InlineKeyboardButton("💳 К пополнениям", callback_data="admin:topups")]], True))
            return
        ticket["status"] = "closed"
        ticket["resolved_at"] = now_iso()
        audit(data, user["id"], "topup_rejected", ticket["ticket_id"])
        save_data(data)
        try:
            await context.bot.send_message(chat_id=int(ticket["user_id"]), text="💳 <b>Пополнение не выполнено</b>\n\nПопробуйте позже или обратитесь в поддержку.", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await send_or_edit(update, "Заявка отклонена.", rows_with_home([[InlineKeyboardButton("💳 К пополнениям", callback_data="admin:topups")]], True))
        return
    if parts[:2] == ["admin", "ticket_close"]:
        ticket = data["tickets"].get(parts[2])
        back_callback = "admin:tickets"
        if ticket:
            ticket["status"] = "closed"
            back_callback = "admin:topups" if ticket.get("type") == "topup" else "admin:tickets"
            audit(data, user["id"], "close_ticket", parts[2])
            save_data(data)
        await send_or_edit(update, "Тикет закрыт.", rows_with_home([[InlineKeyboardButton("Назад", callback_data=back_callback)]], True))
        return
    if action == "admin:reviews":
        reviews = list(data["reviews"].values())
        reviews.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        rows = [[InlineKeyboardButton(f"{review.get('status', 'approved')} | {review.get('item_title', '-')[:25]}", callback_data=f"admin:review:{review['id']}")] for review in reviews[:30]]
        await send_or_edit(update, "<b>Отзывы</b>", rows_with_home(rows, True))
        return
    if parts[:2] == ["admin", "review"]:
        review = data["reviews"].get(parts[2])
        if not review:
            await send_or_edit(update, "Отзыв не найден.", rows_with_home([], True))
            return
        text = f"<b>Отзыв</b>\n\n{h(review.get('text', ''))}\n\nСтатус: <b>{h(review.get('status', 'approved'))}</b>"
        rows = [
            [InlineKeyboardButton("Одобрить", callback_data=f"admin:review_approve:{review['id']}"), InlineKeyboardButton("Удалить", callback_data=f"admin:review_delete:{review['id']}")],
        ]
        await send_or_edit(update, text, rows_with_home(rows, True, ("К отзывам", "admin:reviews")))
        return
    if parts[:2] == ["admin", "review_approve"]:
        if parts[2] in data["reviews"]:
            data["reviews"][parts[2]]["status"] = "approved"
            save_data(data)
        await send_or_edit(update, "Отзыв одобрен.", rows_with_home([[InlineKeyboardButton("К отзывам", callback_data="admin:reviews")]], True))
        return
    if parts[:2] == ["admin", "review_delete"]:
        data["reviews"].pop(parts[2], None)
        save_data(data)
        await send_or_edit(update, "Отзыв удалён.", rows_with_home([[InlineKeyboardButton("К отзывам", callback_data="admin:reviews")]], True))
        return
    if action == "admin:content":
        rows = [
            [InlineKeyboardButton("Главный текст", callback_data="admin:edit_main_text"), InlineKeyboardButton("Главное фото", callback_data="admin:edit_main_photo")],
            [InlineKeyboardButton("FAQ", callback_data="admin:edit_faq"), InlineKeyboardButton("Соглашение", callback_data="admin:edit_agreement")],
            [InlineKeyboardButton("Название магазина", callback_data="admin:edit_shop_title")],
        ]
        await send_or_edit(update, "<b>Контент</b>", rows_with_home(rows, True))
        return
    if action in {"admin:edit_main_text", "admin:edit_main_photo", "admin:edit_faq", "admin:edit_agreement", "admin:edit_shop_title"}:
        state_map = {
            "admin:edit_main_text": ("admin_edit_setting", "main_screen_text", "Введите новый главный текст."),
            "admin:edit_main_photo": ("admin_edit_photo", "main_screen_photo", "Отправьте новое главное фото."),
            "admin:edit_faq": ("admin_edit_setting", "faq", "Введите новый FAQ."),
            "admin:edit_agreement": ("admin_edit_agreement", "agreement", "Введите новый текст соглашения."),
            "admin:edit_shop_title": ("admin_edit_setting", "shop_title", "Введите название магазина."),
        }
        name, key, prompt = state_map[action]
        clear_state(context)
        context.user_data["state"] = {"name": name, "key": key}
        await send_or_edit(update, prompt, rows_with_home([], True))
        return
    if action == "admin:settings":
        text = (
            "<b>Настройки</b>\n\n"
            f"Продажи: <b>{'вкл' if data['settings'].get('sales_enabled') else 'выкл'}</b>\n"
            f"Обслуживание: <b>{'вкл' if data['settings'].get('maintenance_mode') else 'выкл'}</b>\n"
            f"Уведомления админам: <b>{'вкл' if data['settings'].get('notify_admins') else 'выкл'}</b>\n"
            f"Модерация отзывов: <b>{'вкл' if data['settings'].get('moderate_reviews') else 'выкл'}</b>\n"
            f"Мин. пополнение: <b>{data['settings'].get('min_topup_amount')}</b>\n"
            f"Порог остатков: <b>{data['settings'].get('low_stock_threshold')}</b>\n"
            f"TTL заказа: <b>{data['settings'].get('order_ttl_minutes')} мин.</b>"
        )
        rows = [
            [InlineKeyboardButton("Продажи", callback_data="admin:toggle_setting:sales_enabled"), InlineKeyboardButton("Обслуживание", callback_data="admin:toggle_setting:maintenance_mode")],
            [InlineKeyboardButton("Уведомления", callback_data="admin:toggle_setting:notify_admins"), InlineKeyboardButton("Модерация отзывов", callback_data="admin:toggle_setting:moderate_reviews")],
            [InlineKeyboardButton("Мин. пополнение", callback_data="admin:set_number:min_topup_amount"), InlineKeyboardButton("Порог остатков", callback_data="admin:set_number:low_stock_threshold")],
            [InlineKeyboardButton("TTL заказа", callback_data="admin:set_number:order_ttl_minutes")],
        ]
        await send_or_edit(update, text, rows_with_home(rows, True))
        return
    if parts[:2] == ["admin", "set_number"]:
        key = parts[2]
        if key not in {"min_topup_amount", "low_stock_threshold", "order_ttl_minutes"}:
            await send_or_edit(update, "Настройка не найдена.", rows_with_home([], True, ("К системе", "admin:settings")))
            return
        clear_state(context)
        context.user_data["state"] = {"name": "admin_set_number", "key": key}
        await send_or_edit(update, "Введите число.", rows_with_home([], True, ("К системе", "admin:settings")))
        return
    if action == "admin:audit":
        logs = data.get("audit_log", [])[-30:]
        text = "<b>Аудит</b>\n\n" + ("\n".join(f"• {h(row['time'])} | {h(row['action'])} | {h(row.get('details', ''))}" for row in reversed(logs)) or "Пока пусто.")
        await send_or_edit(update, text, rows_with_home([], True))
        return

async def show_admin_user(update: Update, data: dict[str, Any], user_id: str) -> None:
    target = data["users"].get(user_id)
    if not target:
        await send_or_edit(update, "Пользователь не найден.", rows_with_home([], True))
        return
    orders = [order for order in data["orders"].values() if str(order.get("user_id")) == user_id]
    paid = [order for order in orders if normalize_status(order.get("status", "")) in PAID_STATUSES]
    text = (
        "<b>Пользователь</b>\n\n"
        f"ID: <code>{h(target['id'])}</code>\n"
        f"Username: @{h(target.get('username') or '-')}\n"
        f"Имя: {h(target.get('full_name') or '-')}\n"
        f"Баланс: <b>{money(target.get('balance', 0), payment_currency(data))}</b>\n"
        f"Покупок: <b>{target.get('purchases_count', 0)}</b>\n"
        f"Оплаченных заказов: <b>{len(paid)}</b>\n"
        f"Заблокирован: <b>{'да' if target.get('blocked') else 'нет'}</b>\n"
        f"Админ: <b>{'да' if user_id in data.get('admins', []) else 'нет'}</b>\n"
        f"Реф-код: <code>ref_{h(target.get('referral_code', ''))}</code>\n"
        f"Пришёл от: <code>{h(target.get('referred_by') or '-')}</code>\n"
        f"Заметка: {h(target.get('notes') or '-')}"
    )
    rows = [
        [InlineKeyboardButton("Написать", callback_data=f"admin:reply_user:{user_id}"), InlineKeyboardButton("Заметка", callback_data=f"admin:note_user:{user_id}")],
        [InlineKeyboardButton("Начислить", callback_data=f"admin:balance:add:{user_id}"), InlineKeyboardButton("Списать", callback_data=f"admin:balance:sub:{user_id}")],
        [InlineKeyboardButton("Блок/разблок", callback_data=f"admin:toggle_block:{user_id}"), InlineKeyboardButton("Админ вкл/выкл", callback_data=f"admin:toggle_admin:{user_id}")],
    ]
    await send_or_edit(update, text, rows_with_home(rows, True, ("К пользователям", "admin:users")))


async def handle_admin_state(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict[str, Any], user: dict[str, Any], state: dict[str, Any], text: str) -> bool:
    if not is_admin(data, update.effective_user.id):
        return False
    name = state.get("name")

    if name == "admin_search_user":
        query = text.lstrip("@").lower()
        target = None
        for candidate in data["users"].values():
            if candidate["id"] == query or clean_username(candidate.get("username")).lower() == query:
                target = candidate
                break
        clear_state(context)
        if not target:
            await update.message.reply_text("Пользователь не найден.")
            return True
        await show_admin_user(update, data, target["id"])
        return True
    if name in {"admin_add_balance", "admin_sub_balance"}:
        try:
            amount = int(text)
        except ValueError:
            await update.message.reply_text("Введите число.")
            return True
        target = data["users"].get(state["user_id"])
        if target:
            if name == "admin_add_balance":
                target["balance"] = int(target.get("balance", 0)) + amount
                action = "add_balance"
            else:
                target["balance"] = max(0, int(target.get("balance", 0)) - amount)
                action = "sub_balance"
            audit(data, user["id"], action, f"{state['user_id']} | {amount}")
            save_data(data)
        clear_state(context)
        await update.message.reply_text("Баланс обновлён.")
        return True
    if name == "admin_reply_user":
        clear_state(context)
        try:
            await context.bot.send_message(chat_id=int(state["user_id"]), text=f"Сообщение администрации:\n\n{text}")
            audit(data, user["id"], "reply_user", state["user_id"])
            save_data(data)
            await update.message.reply_text("Сообщение отправлено.")
        except Exception as exc:
            await update.message.reply_text(f"Ошибка отправки: {exc}")
        return True
    if name == "admin_ticket_reply":
        ticket = data["tickets"].get(state["ticket_id"])
        if not ticket or ticket.get("type", "support") != "support":
            clear_state(context)
            await update.message.reply_text("Тикет поддержки не найден.")
            return True
        ticket.setdefault("messages", [])
        ticket["messages"].append({"role": "admin", "text": text[:1500], "created_at": now_iso()})
        ticket["status"] = "open"
        audit(data, user["id"], "reply_support_ticket", ticket["ticket_id"])
        save_data(data)
        clear_state(context)
        try:
            await context.bot.send_message(
                chat_id=int(ticket["user_id"]),
                text=f"🛟 <b>Ответ поддержки</b>\n\n{h(text)}",
                parse_mode=ParseMode.HTML,
                reply_markup=rows_with_home([[InlineKeyboardButton("🛟 Написать ещё", callback_data="menu:support")]], False),
            )
            await update.message.reply_text("Ответ отправлен в тикет.")
        except Exception as exc:
            await update.message.reply_text(f"Ошибка отправки: {exc}")
        return True
    if name == "admin_note_user":
        target = data["users"].get(state["user_id"])
        if target:
            target["notes"] = text[:1000]
            audit(data, user["id"], "note_user", state["user_id"])
            save_data(data)
        clear_state(context)
        await update.message.reply_text("Заметка сохранена.")
        return True
    if name == "admin_add_category":
        if text in data["catalog"]:
            await update.message.reply_text("Такая категория уже есть.")
            return True
        data["catalog"][text] = []
        audit(data, user["id"], "add_category", text)
        save_data(data)
        clear_state(context)
        await update.message.reply_text("Категория добавлена.")
        return True
    if name == "admin_rename_category":
        old = state["category"]
        if text in data["catalog"]:
            await update.message.reply_text("Такая категория уже есть.")
            return True
        data["catalog"][text] = data["catalog"].pop(old)
        for order in data["orders"].values():
            if order.get("city") == old:
                order["city"] = text
            for row in order.get("items", []):
                if row.get("category") == old:
                    row["category"] = text
        audit(data, user["id"], "rename_category", f"{old} -> {text}")
        save_data(data)
        clear_state(context)
        await update.message.reply_text("Категория переименована.")
        return True
    if name == "admin_add_item":
        try:
            title, price, description = [part.strip() for part in text.split("|", 2)]
            price = int(price)
        except ValueError:
            await update.message.reply_text("Формат: Название|Цена|Описание")
            return True
        if contains_unsafe(title) or contains_unsafe(description):
            await update.message.reply_text("Этот товар не похож на легальный цифровой товар. Измените название/описание.")
            return True
        category = state["category"]
        data["catalog"].setdefault(category, [])
        data["catalog"][category].append(
            {
                "id": generate_id("ITEM"),
                "title": title,
                "description": description,
                "price": max(1, price),
                "photo": "",
                "active": True,
                "stock": -1,
                "sold_count": 0,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "delivery_text": "Спасибо за покупку. Настройте выдачу товара.",
            }
        )
        audit(data, user["id"], "add_item", title)
        save_data(data)
        clear_state(context)
        await update.message.reply_text("Товар добавлен.")
        return True
    if name == "admin_item_edit":
        item = get_item(data, state["category"], state["item_id"])
        if not item:
            clear_state(context)
            await update.message.reply_text("Товар не найден.")
            return True
        field = state["field"]
        if field in {"price", "stock"}:
            try:
                item[field] = int(text)
            except ValueError:
                await update.message.reply_text("Введите число.")
                return True
            if field == "price":
                item[field] = max(1, item[field])
        elif field in {"title", "description", "delivery_text"}:
            if field in {"title", "description"} and contains_unsafe(text):
                await update.message.reply_text("Значение не похоже на легальный цифровой товар.")
                return True
            item[field] = text
        item["updated_at"] = now_iso()
        audit(data, user["id"], f"edit_item_{field}", item["id"])
        save_data(data)
        clear_state(context)
        await update.message.reply_text("Товар обновлён.")
        return True
    if name in {"admin_set_currency", "admin_set_payment_contact", "admin_set_payment_channel", "admin_set_crypto_token", "admin_set_crypto_api_url", "admin_set_crypto_currency", "admin_set_topup_requisites", "admin_set_manual_requisites"}:
        key_map = {
            "admin_set_currency": "payment_currency",
            "admin_set_payment_contact": "payment_contact_username",
            "admin_set_payment_channel": "payment_channel_url",
            "admin_set_crypto_token": "crypto_pay_token",
            "admin_set_crypto_api_url": "crypto_pay_api_url",
            "admin_set_topup_requisites": "topup_requisites",
            "admin_set_manual_requisites": "manual_payment_requisites",
        }
        if name == "admin_set_crypto_currency":
            value = text.strip().upper()
            if value.startswith("CRYPTO:"):
                data["settings"]["crypto_pay_currency_type"] = "crypto"
                data["settings"]["crypto_pay_asset"] = value.split(":", 1)[1] or "USDT"
            else:
                data["settings"]["crypto_pay_currency_type"] = "fiat"
                data["settings"]["crypto_pay_fiat"] = value or "RUB"
            key = "crypto_pay_currency"
        else:
            key = key_map[name]
            data["settings"][key] = text.upper() if key == "payment_currency" else text
        audit(data, user["id"], "edit_payment_setting", key)
        save_data(data)
        clear_state(context)
        await update.message.reply_text("Настройка оплаты сохранена.")
        return True
    if name == "admin_create_promo":
        try:
            code, promo_type, amount, max_uses = [part.strip() for part in text.split("|", 3)]
            code = code.upper()
            amount = int(amount)
            max_uses = int(max_uses)
        except ValueError:
            await update.message.reply_text("Формат: CODE|percent|10|100 или CODE|fixed|50|20")
            return True
        if promo_type not in {"percent", "fixed"}:
            await update.message.reply_text("Тип должен быть percent или fixed.")
            return True
        data.setdefault("promo_codes", {})[code] = {
            "code": code,
            "type": promo_type,
            "amount": max(1, amount),
            "active": True,
            "uses": 0,
            "max_uses": max_uses,
            "created_at": now_iso(),
        }
        audit(data, user["id"], "create_promo", code)
        save_data(data)
        clear_state(context)
        await update.message.reply_text("Промокод создан.")
        return True
    if name == "admin_broadcast":
        targets = []
        for candidate in data["users"].values():
            if state.get("target") == "subscribed" and not candidate.get("subscribed", True):
                continue
            if candidate.get("blocked"):
                continue
            targets.append(candidate["id"])
        sent = 0
        for target_id in targets:
            try:
                await context.bot.send_message(chat_id=int(target_id), text=text)
                sent += 1
            except Exception:
                pass
        data["broadcasts"][generate_id("BRC")] = {"text": text, "target": state.get("target"), "sent": sent, "created_at": now_iso()}
        audit(data, user["id"], "broadcast", f"{state.get('target')} | {sent}")
        save_data(data)
        clear_state(context)
        await update.message.reply_text(f"Рассылка отправлена: {sent}.")
        return True
    if name in {"admin_set_cashback", "admin_set_ref_bonus"}:
        try:
            value = int(text)
        except ValueError:
            await update.message.reply_text("Введите число.")
            return True
        key = "cashback_percent" if name == "admin_set_cashback" else "referral_bonus"
        data["settings"][key] = max(0, value)
        audit(data, user["id"], "edit_marketing_setting", key)
        save_data(data)
        clear_state(context)
        await update.message.reply_text("Настройка сохранена.")
        return True
    if name == "admin_set_number":
        try:
            value = int(text)
        except ValueError:
            await update.message.reply_text("Введите число.")
            return True
        key = state["key"]
        if key in {"min_topup_amount", "low_stock_threshold", "order_ttl_minutes"}:
            data["settings"][key] = max(0, value)
            audit(data, user["id"], "edit_number_setting", key)
            save_data(data)
        clear_state(context)
        await update.message.reply_text("Настройка сохранена.")
        return True
    if name in {"admin_edit_setting", "admin_edit_agreement"}:
        key = state["key"]
        if name == "admin_edit_agreement":
            data["agreement"] = text
        else:
            data["settings"][key] = text
        audit(data, user["id"], "edit_content", key)
        save_data(data)
        clear_state(context)
        await update.message.reply_text("Контент обновлён.")
        return True
    if name == "admin_edit_button":
        key = state["key"]
        if key in BUTTON_DEFAULTS:
            data["settings"].setdefault("buttons", {})[key] = text[:40]
            audit(data, user["id"], "edit_button_label", key)
            save_data(data)
        clear_state(context)
        await update.message.reply_text("Подпись кнопки обновлена.")
        return True

    return False


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    text = update.message.text.strip()
    state = context.user_data.get("state") or {}

    if is_blocked(user):
        await update.message.reply_text("Доступ к боту ограничен.")
        return

    if state and await handle_admin_state(update, context, data, user, state, text):
        return

    if state.get("name") == "support":
        ticket_id = generate_id("TIC")
        data["tickets"][ticket_id] = {
            "ticket_id": ticket_id,
            "type": "support",
            "user_id": user["id"],
            "username": user.get("username", ""),
            "text": text,
            "messages": [{"role": "user", "text": text[:1500], "created_at": now_iso()}],
            "status": "new_message",
            "created_at": now_iso(),
        }
        audit(data, user["id"], "ticket_created", ticket_id)
        save_data(data)
        clear_state(context)
        await update.message.reply_text(
            f"🛟 <b>Тикет открыт</b>\n\nID: <code>{ticket_id}</code>\nОтвет появится здесь в боте.",
            parse_mode=ParseMode.HTML,
            reply_markup=rows_with_home([], admin),
        )
        await notify_admins(context, data, f"🔔 Новый тикет поддержки: <code>{ticket_id}</code>\nПользователь: <code>{user['id']}</code>")
        return
    if state.get("name") == "search":
        query = text.lower()
        rows = []
        for category, items in data["catalog"].items():
            for item in items:
                if product_available(item) and query in item.get("title", "").lower():
                    rows.append([InlineKeyboardButton(f"{item['title']} | {money(item['price'], payment_currency(data))}", callback_data=f"product:{category}:{item['id']}")])
        clear_state(context)
        await update.message.reply_text("Результаты поиска:", reply_markup=rows_with_home(rows, admin))
        return
    if state.get("name") == "promo":
        ok, message, promo = validate_promo(data, text)
        if ok:
            user["active_promo"] = promo["code"]
            save_data(data)
        clear_state(context)
        await update.message.reply_text(message)
        return
    if state.get("name") == "topup_amount":
        try:
            amount = int(text)
        except ValueError:
            await update.message.reply_text("Введите сумму числом.")
            return
        min_amount = int(data["settings"].get("min_topup_amount", 10) or 10)
        if amount < min_amount:
            await update.message.reply_text(f"Минимальная сумма: {min_amount}.")
            return
        context.user_data["state"] = {"name": "topup_wait_paid", "amount": amount}
        requisites = data["settings"].get("topup_requisites") or default_settings()["topup_requisites"]
        await update.message.reply_text(
            f"💳 <b>Реквизиты для пополнения</b>\n\n{h(requisites)}\n\nПосле оплаты нажмите кнопку «✅ Я оплатил», затем отправьте скрин оплаты.",
            parse_mode=ParseMode.HTML,
            reply_markup=rows_with_home([[InlineKeyboardButton("✅ Я оплатил", callback_data="topup:paid")]], admin),
        )
        return
    if state.get("name") == "topup_receipt":
        await update.message.reply_text("📸 Пришлите скрин оплаты фотографией. Текстом чек не подтверждается.")
        return
    if state.get("name") == "topup_wait_paid":
        await update.message.reply_text("После оплаты нажмите кнопку «✅ Я оплатил», затем отправьте скрин оплаты.")
        return
    if state.get("name") == "manual_order_receipt":
        await update.message.reply_text("📸 Пришлите скрин оплаты аренды фотографией. Текстом чек не подтверждается.")
        return
    if state.get("name") == "rental_request":
        await save_rental_request(update, context, data, user, state["order_id"], scooter_code=text)
        return
    if state.get("name") == "review":
        order_id = state["order_id"]
        order = data["orders"].get(order_id)
        if not order or order.get("user_id") != user["id"]:
            clear_state(context)
            await update.message.reply_text("Заказ не найден.")
            return
        review_id = generate_id("REV")
        first = order.get("items", [{}])[0]
        data["reviews"][review_id] = {
            "id": review_id,
            "order_id": order_id,
            "user_id": user["id"],
            "author_name": user.get("full_name") or user.get("username") or "Пользователь",
            "category": first.get("category", order.get("city", "")),
            "city": first.get("category", order.get("city", "")),
            "item_id": first.get("item_id", order.get("item_id", "")),
            "item_title": first.get("title", order.get("item_title", "")),
            "text": text[:1000],
            "status": "pending" if data["settings"].get("moderate_reviews") else "approved",
            "created_at": now_iso(),
        }
        audit(data, user["id"], "review_created", review_id)
        save_data(data)
        clear_state(context)
        await update.message.reply_text("Отзыв сохранён." if data["reviews"][review_id]["status"] == "approved" else "Отзыв отправлен на модерацию.")
        return

    await update.message.reply_text("Откройте меню кнопкой /start.", reply_markup=main_menu(data, admin))


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    state = context.user_data.get("state") or {}
    if state.get("name") == "topup_wait_paid":
        await update.message.reply_text("Сначала нажмите кнопку «✅ Я оплатил», затем отправьте скрин оплаты.")
        return
    if state.get("name") == "topup_receipt":
        photo_id = update.message.photo[-1].file_id
        caption = (update.message.caption or "").strip()
        await save_topup_request(
            update,
            context,
            data,
            user,
            int(state.get("amount", 0) or 0),
            receipt_text=caption,
            receipt_photo_id=photo_id,
        )
        return
    if state.get("name") == "manual_order_receipt":
        photo_id = update.message.photo[-1].file_id
        caption = (update.message.caption or "").strip()
        await save_manual_order_receipt(update, context, data, user, state["order_id"], photo_id, caption)
        return
    if state.get("name") == "rental_request":
        photo_id = update.message.photo[-1].file_id
        caption = (update.message.caption or "").strip()
        await save_rental_request(update, context, data, user, state["order_id"], scooter_code=caption, qr_photo_id=photo_id)
        return
    if not is_admin(data, update.effective_user.id):
        return
    photo_id = update.message.photo[-1].file_id
    if state.get("name") == "admin_item_edit" and state.get("field") == "photo":
        item = get_item(data, state["category"], state["item_id"])
        if item:
            item["photo"] = photo_id
            item["updated_at"] = now_iso()
            audit(data, user["id"], "edit_item_photo", item["id"])
            save_data(data)
        clear_state(context)
        await update.message.reply_text("Фото товара обновлено.")
        return
    if state.get("name") == "admin_edit_photo":
        data["settings"][state["key"]] = photo_id
        audit(data, user["id"], "edit_photo", state["key"])
        save_data(data)
        clear_state(context)
        await update.message.reply_text("Фото обновлено.")


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    get_or_create_user(data, update.effective_user)
    if not is_admin(data, update.effective_user.id):
        await update.message.reply_text("Нет доступа.")
        return
    await update.message.reply_text("Админ-панель", reply_markup=admin_panel_keyboard())


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    text = (
        "<b>Профиль</b>\n\n"
        f"ID: <code>{h(user['id'])}</code>\n"
        f"Username: @{h(user.get('username') or '-')}\n"
        f"Имя: {h(user.get('full_name') or '-')}\n"
        f"Покупок: <b>{int(user.get('purchases_count', 0))}</b>\n"
        f"Баланс: <b>{money(user.get('balance', 0), payment_currency(data))}</b>\n"
        f"Реф-код: <code>ref_{h(user.get('referral_code'))}</code>"
    )
    await update.message.reply_text(
        text,
        reply_markup=rows_with_home(
            [
                [InlineKeyboardButton("💳 Пополнить по реквизитам", callback_data="menu:topup")],
                [InlineKeyboardButton("🎟 Промокод", callback_data="menu:promo")],
            ],
            admin,
        ),
        parse_mode=ParseMode.HTML,
    )


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    orders = [order for order in data["orders"].values() if str(order.get("user_id")) == user["id"]]
    orders.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    rows = [
        [InlineKeyboardButton(f"{order['order_id']} | {status_label(order.get('status', ''))}", callback_data=f"order:{order['order_id']}")]
        for order in orders[:25]
    ]
    await update.message.reply_text("🧾 Мои поездки" if rows else "🧾 Поездок пока нет.", reply_markup=rows_with_home(rows, admin))


async def cart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    await show_cart(update, context, data, user, is_admin(data, update.effective_user.id))


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    admin = is_admin(data, update.effective_user.id)
    clear_state(context)
    context.user_data["state"] = {"name": "support"}
    await update.message.reply_text("🛟 Напишите вопрос следующим сообщением. Ответ появится здесь в боте.", reply_markup=rows_with_home([], admin))


async def promo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    admin = is_admin(data, update.effective_user.id)
    clear_state(context)
    context.user_data["state"] = {"name": "promo"}
    await update.message.reply_text("Введите промокод следующим сообщением.", reply_markup=rows_with_home([], admin))


async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_or_create_user(data, update.effective_user)
    admin = is_admin(data, update.effective_user.id)
    await show_cart(update, context, data, user, admin)


async def setup_bot_commands(app: Application) -> None:
    try:
        await app.bot.set_my_name("Whoosh Buy")
        await app.bot.set_my_short_description("🛴 Аренда самокатов")
        await app.bot.set_my_description("🛴 Whoosh Buy — аренда самоката: оплата, скрин подтверждения, номер или QR-код и статусы внутри бота.")
    except Exception as exc:
        logger.warning("Failed to set bot profile text: %s", exc)
    await app.bot.set_my_commands(
        [
            BotCommand("start", "главное меню"),
            BotCommand("menu", "открыть меню"),
            BotCommand("profile", "профиль и реф-код"),
            BotCommand("orders", "мои поездки"),
            BotCommand("cart", "корзина"),
            BotCommand("pay", "оплата тарифа"),
            BotCommand("promo", "ввести промокод"),
            BotCommand("support", "поддержка"),
            BotCommand("admin", "админ-панель"),
        ]
    )


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise RuntimeError("Укажите BOT_TOKEN в bot.py или переменной окружения BOT_TOKEN.")
    app = Application.builder().token(BOT_TOKEN).post_init(setup_bot_commands).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("orders", orders_command))
    app.add_handler(CommandHandler("cart", cart_command))
    app.add_handler(CommandHandler("pay", pay_command))
    app.add_handler(CommandHandler("promo", promo_command))
    app.add_handler(CommandHandler("support", support_command))
    app.add_handler(CallbackQueryHandler(menu_router, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(topup_router, pattern=r"^topup:"))
    app.add_handler(CallbackQueryHandler(category_router, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(product_router, pattern=r"^product:"))
    app.add_handler(CallbackQueryHandler(cart_router, pattern=r"^cart:"))
    app.add_handler(CallbackQueryHandler(favorite_router, pattern=r"^fav:"))
    app.add_handler(CallbackQueryHandler(buy_router, pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(pay_router, pattern=r"^pay:"))
    app.add_handler(CallbackQueryHandler(crypto_check_router, pattern=r"^crypto_check:"))
    app.add_handler(CallbackQueryHandler(manual_payment_router, pattern=r"^manual_paid:"))
    app.add_handler(CallbackQueryHandler(rental_router, pattern=r"^rent:"))
    app.add_handler(CallbackQueryHandler(order_router, pattern=r"^order:"))
    app.add_handler(CallbackQueryHandler(order_cancel_router, pattern=r"^order_cancel:"))
    app.add_handler(CallbackQueryHandler(reviews_router, pattern=r"^(reviews:|review:)"))
    app.add_handler(CallbackQueryHandler(admin_router, pattern=r"^admin:"))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    if app.job_queue:
        app.job_queue.run_repeating(crypto_auto_check_job, interval=60, first=20)
    app.run_polling()


if __name__ == "__main__":
    main()
