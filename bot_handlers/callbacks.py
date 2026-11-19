from datetime import datetime, timezone, date
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from db import supabase, run_db, get_user_uuid_by_tg_id, apply_tx_balance_effect_sync, create_transaction_atomic

router = Router()


@router.callback_query(F.data.startswith("undo_tx:"))
async def cb_undo_tx(callback: CallbackQuery):
    tx_id = callback.data.split(":", 1)[1]
    user_uuid = await get_user_uuid_by_tg_id(callback.from_user.id)
    if not user_uuid:
        await callback.answer("Користувача не знайдено", show_alert=True)
        return

    def _undo():
        res = supabase.table("transactions").select("*").eq("id", tx_id).eq("user_id", user_uuid).limit(1).execute().data
        if not res:
            return False
        tx = res[0]
        apply_tx_balance_effect_sync(tx, user_uuid, reverse=True)
        supabase.table("transactions").delete().eq("id", tx_id).eq("user_id", user_uuid).execute()
        return True

    ok = await run_db(_undo)
    if ok:
        await callback.message.edit_text("↩️ <b>Транзакцію скасовано</b>, баланс рахунку відновлено.", parse_mode="HTML")
        await callback.answer("Скасовано")
    else:
        await callback.answer("Транзакцію вже видалено", show_alert=True)


@router.callback_query(F.data.startswith("pick_cat:"))
async def cb_pick_cat(callback: CallbackQuery):
    tx_id = callback.data.split(":", 1)[1]
    user_uuid = await get_user_uuid_by_tg_id(callback.from_user.id)
    if not user_uuid:
        return

    def _load():
        tx_res = supabase.table("transactions").select("type").eq("id", tx_id).eq("user_id", user_uuid).limit(1).execute().data
        if not tx_res:
            return None, []
        tx_type = tx_res[0]["type"]
        cats = supabase.table("categories").select("id, name, icon, type").eq("type", tx_type).execute().data or []
        return tx_type, cats

    tx_type, cats = await run_db(_load)
    if not tx_type:
        await callback.answer("Транзакцію не знайдено", show_alert=True)
        return

    rows = []
    row = []
    for c in cats[:12]:
        row.append(InlineKeyboardButton(text=f"{c.get('icon', '')} {c['name']}", callback_data=f"set_cat:{tx_id}:{c['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer("Оберіть категорію")


@router.callback_query(F.data.startswith("set_cat:"))
async def cb_set_cat(callback: CallbackQuery):
    _, tx_id, cat_id = callback.data.split(":", 2)
    user_uuid = await get_user_uuid_by_tg_id(callback.from_user.id)
    if not user_uuid:
        return

    def _update():
        supabase.table("transactions").update({"category_id": cat_id}).eq("id", tx_id).eq("user_id", user_uuid).execute()
        cat = supabase.table("categories").select("name, icon").eq("id", cat_id).limit(1).execute().data
        return cat[0] if cat else {"name": "Категорія", "icon": "🏷️"}

    cat = await run_db(_update)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer(f"Категорію змінено на {cat.get('icon', '')} {cat['name']}")


def _advance_month(day_of_month: int) -> str:
    today = datetime.now(timezone.utc).date()
    month = today.month + 1
    year = today.year
    if month > 12:
        month = 1
        year += 1
    return date(year, month, min(day_of_month, 28)).isoformat()


@router.callback_query(F.data.startswith("rec_ok:"))
async def cb_recurring_ok(callback: CallbackQuery):
    rec_id = callback.data.split(":", 1)[1]
    user_uuid = await get_user_uuid_by_tg_id(callback.from_user.id)
    if not user_uuid:
        return

    rec_res = await run_db(
        lambda: supabase.table("recurring_payments").select("*").eq("id", rec_id).eq("user_id", user_uuid).limit(1).execute().data
    )
    if not rec_res:
        await callback.answer("Платіж не знайдено", show_alert=True)
        return
    rec = rec_res[0]

    if rec.get("account_id"):
        await create_transaction_atomic(
            user_uuid=user_uuid,
            account_id=rec["account_id"],
            to_account_id=None,
            category_id=rec.get("category_id"),
            amount=float(rec["amount"]),
            target_amount=None,
            tx_type=rec.get("type", "expense"),
            note=f"🔁 {rec['title']}",
        )

    next_date = _advance_month(int(rec.get("day_of_month") or 1))
    await run_db(
        lambda: supabase.table("recurring_payments").update({"next_run_date": next_date}).eq("id", rec_id).execute()
    )
    await callback.message.edit_text(
        f"✅ <b>Регулярний платіж підтверджено:</b> {rec['title']} ({float(rec['amount']):,.2f})",
        parse_mode="HTML",
    )
    await callback.answer("Проведено!")


@router.callback_query(F.data.startswith("rec_skip:"))
async def cb_recurring_skip(callback: CallbackQuery):
    rec_id = callback.data.split(":", 1)[1]
    user_uuid = await get_user_uuid_by_tg_id(callback.from_user.id)
    if not user_uuid:
        return

    rec_res = await run_db(
        lambda: supabase.table("recurring_payments").select("day_of_month, title").eq("id", rec_id).eq("user_id", user_uuid).limit(1).execute().data
    )
    if not rec_res:
        await callback.answer("Не знайдено")
        return
    rec = rec_res[0]
    next_date = _advance_month(int(rec.get("day_of_month") or 1))
    await run_db(
        lambda: supabase.table("recurring_payments").update({"next_run_date": next_date}).eq("id", rec_id).execute()
    )
    await callback.message.edit_text(f"⏭ <b>Пропущено:</b> {rec['title']} (наступного разу {next_date})", parse_mode="HTML")
    await callback.answer("Пропущено")
