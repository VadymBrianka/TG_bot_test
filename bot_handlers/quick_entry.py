import io
import re
from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from db import (
    supabase,
    run_db,
    get_user_uuid_by_tg_id,
    get_default_account,
    get_exchange_rates,
    create_transaction_atomic,
)
from services.budget_alerts import check_budget_threshold
from services.receipt_ai import parse_receipt_bytes, match_category_id

router = Router()

INCOME_RE = re.compile(
    r"^\+\s*(\d+(?:[.,]\d+)?)\s*(UAH|USD|EUR|GBP|PLN|грн|\$|€)?\s*(.*)$",
    re.IGNORECASE,
)
EXPENSE_RE = re.compile(
    r"^\s*([^\d\n][^\n]*?)\s+(\d+(?:[.,]\d+)?)\s*(UAH|USD|EUR|GBP|PLN|грн|\$|€)?\s*$",
    re.IGNORECASE,
)

CURRENCY_ALIASES = {
    "ГРН": "UAH",
    "$": "USD",
    "€": "EUR",
}

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "продукти": ["сільпо", "атб", "novus", "фора", "варус", "ашан", "метро", "продукт", "їжа", "молоко", "хліб", "ринок"],
    "кафе": ["кава", "кафе", "ресторан", "мак", "kfc", "пузата", "glovo", "bolt food", "піца", "суші", "обід", "сніданок"],
    "транспорт": ["uber", "uklon", "bolt", "таксі", "метро", "автобус", "тролейбус", "потяг", "укрзалізниця", "бензин", "окко", "wog", "пальне"],
    "здоров": ["аптека", "ліки", "лікар", "стоматолог", "аналіз", "медиц"],
    "комунал": ["світло", "вода", "газ", "опалення", "інтернет", "київстар", "vodafone", "lifecell", "комунал", "оренда"],
    "розваг": ["кіно", "netflix", "spotify", "youtube", "ігри", "steam", "підписк"],
    "одяг": ["одяг", "взуття", "zara", "hm", "rozetka"],
    "зарплат": ["зарплат", "аванс", "зп", "salary"],
}


def _normalize_currency(raw: str | None) -> str | None:
    if not raw:
        return None
    up = raw.strip().upper()
    return CURRENCY_ALIASES.get(up, up)


async def _match_category(user_uuid: str, text: str, tx_type: str) -> tuple[str | None, str]:
    lower = text.lower()
    cats = await run_db(lambda: supabase.table("categories").select("id, name, icon, type").execute().data or [])
    typed_cats = [c for c in cats if c.get("type") == tx_type]

    for c in typed_cats:
        cname = str(c.get("name", "")).lower()
        if cname and cname in lower:
            return c["id"], f"{c.get('icon', '🏷️')} {c['name']}"

    for kw_group, keywords in CATEGORY_KEYWORDS.items():
        if any(k in lower for k in keywords):
            for c in typed_cats:
                if kw_group in str(c.get("name", "")).lower():
                    return c["id"], f"{c.get('icon', '🏷️')} {c['name']}"
    return None, "⚪ Інше"


async def _pick_account_and_amount(user_uuid: str, raw_amount: float, explicit_curr: str | None):
    accs = await run_db(
        lambda: supabase.table("accounts").select("*").eq("user_id", user_uuid).order("created_at").execute().data or []
    )
    if not accs:
        return None, raw_amount, "UAH", ""

    default_acc = await get_default_account(user_uuid) or accs[0]

    if explicit_curr:
        matching = next((a for a in accs if str(a.get("currency", "UAH")).upper() == explicit_curr), None)
        if matching:
            return matching, raw_amount, explicit_curr, ""

        acc_curr = str(default_acc.get("currency", "UAH")).upper()
        if explicit_curr != acc_curr:
            rates = await get_exchange_rates()
            converted = round(raw_amount * (rates.get(explicit_curr, 1.0) / rates.get(acc_curr, 1.0)), 2)
            note_suffix = f" ({raw_amount:g} {explicit_curr})"
            return default_acc, converted, acc_curr, note_suffix

    acc_curr = str(default_acc.get("currency", "UAH")).upper()
    return default_acc, raw_amount, acc_curr, ""


def _tx_actions_keyboard(tx_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🏷 Змінити категорію", callback_data=f"pick_cat:{tx_id}"),
                InlineKeyboardButton(text="↩️ Скасувати", callback_data=f"undo_tx:{tx_id}"),
            ]
        ]
    )


@router.message(F.text.regexp(INCOME_RE))
async def handle_quick_income(message: Message):
    if not message.from_user or not message.text:
        return
    m = INCOME_RE.match(message.text.strip())
    if not m:
        return

    raw_amount = float(m.group(1).replace(",", "."))
    if raw_amount <= 0:
        return
    explicit_curr = _normalize_currency(m.group(2))
    desc = (m.group(3) or "").strip() or "Дохід"

    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        return

    acc, final_amt, final_curr, suffix = await _pick_account_and_amount(user_uuid, raw_amount, explicit_curr)
    if not acc:
        return

    cat_id, cat_label = await _match_category(user_uuid, desc, "income")
    tx = await create_transaction_atomic(
        user_uuid=user_uuid,
        account_id=acc["id"],
        to_account_id=None,
        category_id=cat_id,
        amount=final_amt,
        target_amount=None,
        tx_type="income",
        note=f"{desc}{suffix}",
    )
    tx_id = tx.get("id", "")
    kb = _tx_actions_keyboard(tx_id) if tx_id else None
    await message.reply(
        f"🟢 <b>Дохід додано:</b> +{final_amt:,.2f} {final_curr}\n"
        f"💳 Рахунок: <i>{acc['name']}</i> | {cat_label}\n"
        f"📝 Опис: <i>{desc}{suffix}</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.message(F.text.regexp(EXPENSE_RE))
async def handle_quick_expense(message: Message):
    if not message.from_user or not message.text:
        return
    m = EXPENSE_RE.match(message.text.strip())
    if not m:
        return

    desc = m.group(1).strip()
    num_str = m.group(2).replace(",", ".")
    explicit_curr = _normalize_currency(m.group(3))

    # Guard against accidental year numbers like "Борг 2026" without currency
    if not explicit_curr and num_str.isdigit() and len(num_str) == 4 and 2000 <= int(num_str) <= 2099:
        return

    raw_amount = float(num_str)
    if raw_amount <= 0:
        return

    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        return

    acc, final_amt, final_curr, suffix = await _pick_account_and_amount(user_uuid, raw_amount, explicit_curr)
    if not acc:
        return

    cat_id, cat_label = await _match_category(user_uuid, desc, "expense")
    tx = await create_transaction_atomic(
        user_uuid=user_uuid,
        account_id=acc["id"],
        to_account_id=None,
        category_id=cat_id,
        amount=final_amt,
        target_amount=None,
        tx_type="expense",
        note=f"{desc}{suffix}",
    )
    tx_id = tx.get("id", "")
    kb = _tx_actions_keyboard(tx_id) if tx_id else None
    await message.reply(
        f"🔴 <b>Витрату записано:</b> -{final_amt:,.2f} {final_curr}\n"
        f"💳 Рахунок: <i>{acc['name']}</i> | {cat_label}\n"
        f"📝 Опис: <i>{desc}{suffix}</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )
    if cat_id:
        await check_budget_threshold(message.bot, user_uuid, message.from_user.id, cat_id)


@router.message(F.photo)
async def handle_receipt_photo(message: Message):
    if not message.from_user or not message.photo:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        await message.answer("Спочатку натисніть /start")
        return

    status_msg = await message.reply("🤖 Аналізую чек через ШІ...")
    try:
        photo = message.photo[-1]
        buf = io.BytesIO()
        await message.bot.download(photo, destination=buf)
        parsed = await parse_receipt_bytes(buf.getvalue(), mime_type="image/jpeg")

        raw_amount = float(parsed["total_amount"])
        explicit_curr = parsed.get("currency") or "UAH"
        acc, final_amt, final_curr, suffix = await _pick_account_and_amount(user_uuid, raw_amount, explicit_curr)
        if not acc:
            await status_msg.edit_text("❌ У вас немає жодного рахунку. Створіть рахунок у Mini App.")
            return

        cat_id, cat_label = await match_category_id(parsed.get("category_hint", ""))
        merchant = parsed.get("merchant") or "Чек"
        items_summary = parsed.get("items_summary") or ""
        note = f"🧾 {merchant}" + (f" ({items_summary})" if items_summary else "") + suffix

        tx = await create_transaction_atomic(
            user_uuid=user_uuid,
            account_id=acc["id"],
            to_account_id=None,
            category_id=cat_id,
            amount=final_amt,
            target_amount=None,
            tx_type="expense",
            note=note[:450],
        )
        tx_id = tx.get("id", "")
        kb = _tx_actions_keyboard(tx_id) if tx_id else None
        await status_msg.edit_text(
            f"🧾 <b>Чек розпізнано та записано!</b>\n"
            f"🔴 Сума: <b>-{final_amt:,.2f} {final_curr}</b>\n"
            f"🏪 Заклад: <b>{merchant}</b>\n"
            f"💳 Рахунок: <i>{acc['name']}</i> | {cat_label}\n"
            + (f"🛒 Позиції: <i>{items_summary}</i>" if items_summary else ""),
            reply_markup=kb,
            parse_mode="HTML",
        )
        if cat_id:
            await check_budget_threshold(message.bot, user_uuid, message.from_user.id, cat_id)
    except ValueError as ve:
        await status_msg.edit_text(f"⚠️ {ve}")
    except Exception as e:
        await status_msg.edit_text(f"❌ Не вдалося розпізнати чек: {e}")
