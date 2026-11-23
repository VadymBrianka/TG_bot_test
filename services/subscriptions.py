import copy
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from aiohttp import web

from config import ADMIN_ID
from db import supabase, run_db

PLAN_ORDER = {"free": 0, "pro": 1, "vip": 2}

PLANS: dict[str, dict[str, Any]] = {
    "free": {
        "name": "Free",
        "stars_month": 0,
        "stars_year": 0,
        "usd_month": 0.0,
        "usd_year": 0.0,
        "max_accounts": 2,
        "max_goals": 2,
        "features": [
            "До 2 рахунків (картки, готівка, крипто) та 2 фінансових цілей",
            "Ручний облік доходів, витрат і мультивалютних переказів",
            "⚡ Швидкий текстовий запис у чаті бота (напр. «Кава 75»)",
            "💱 Автоматичний конвертер валют за курсом НБУ",
            "📋 Базові звіти в чаті (/balance, /today, /month, /stats)",
            "🎨 Підтримка світлої та темної теми в Mini App",
        ],
    },
    "pro": {
        "name": "Pro",
        "stars_month": 149,
        "stars_year": 1190,
        "usd_month": 2.99,
        "usd_year": 23.99,
        "max_accounts": 999,
        "max_goals": 999,
        "features": [
            "Усі можливості тарифу Free",
            "♾️ Необмежена кількість рахунків і фінансових цілей",
            "🎙️ Голосовий швидкий ввід у чаті бота (ШІ розпізнає суму, валюту та категорію)",
            "📷 ШІ-сканер чеків і накладних (камера, галерея, PDF/документ або фото в чат)",
            "🧠 ШІ-фінансовий радник (/advice у чаті та персональний розбір у Mini App)",
            "📅 Календар грошових потоків та аналітика за будь-який вибраний період",
            "🔮 Прогноз балансу та мультивалютна аналітика по категоріях",
            "📊 Місячні бюджети з лімітами (автосповіщення) та 🔁 регулярні платежі",
            "🤝 Трекер боргів і позик",
            "👨‍👩‍👧‍👦 Приєднання до сімейної групи / спільного капіталу",
            "📁 Експорт виписки в Telegram (CSV) та імпорт банківських CSV",
        ],
    },
    "vip": {
        "name": "VIP Investor",
        "stars_month": 299,
        "stars_year": 2390,
        "usd_month": 5.99,
        "usd_year": 47.99,
        "max_accounts": 999,
        "max_goals": 999,
        "features": [
            "Усі можливості тарифу Pro",
            "🤖 Автономний крипто-трейдинг бот (демо-рахунок, профілі ризику, крива капіталу Equity Curve, історія угод та VIP Лідерборд)",
            "👨‍👩‍👧‍👦 Створення власної сімейної групи та управління спільним капіталом",
            "📈 Повний інвестиційний портфель (акції, криптовалюта, депозити, ОВДП)",
            "🔄 Автоматичне оновлення ринкових котирувань з бірж",
            "🚨 Персональні алерти цін на акції та криптовалюту (/alert)",
            "📈 Симулятор складного відсотка з інтерактивним графіком",
            "📬 Щоденні нагадування, тижневі та місячні авто-дайджести в Telegram",
        ],
    },
}


async def get_user_subscription(tg_id: int) -> dict[str, Any]:
    def _q():
        try:
            res = (
                supabase.table("users")
                .select("id, role, subscription_plan, subscription_expires_at")
                .eq("telegram_id", tg_id)
                .limit(1)
                .execute()
                .data
            )
            return res[0] if res else None
        except Exception:
            return None

    row = await run_db(_q)
    if tg_id == ADMIN_ID or (row and str(row.get("role", "")).lower() == "admin"):
        return {"plan": "vip", "expires_at": None, "is_active": True, "is_admin_override": True}

    if not row:
        return {"plan": "free", "expires_at": None, "is_active": True}

    plan = str(row.get("subscription_plan") or "free").lower()
    if plan not in PLAN_ORDER:
        plan = "free"

    expires_raw = row.get("subscription_expires_at")
    if plan != "free" and expires_raw:
        try:
            exp_dt = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
            if exp_dt < datetime.now(timezone.utc):
                plan = "free"
                expires_raw = None
        except Exception:
            pass

    return {"plan": plan, "expires_at": expires_raw, "is_active": True}


def has_plan(current_plan: str, required_plan: str) -> bool:
    return PLAN_ORDER.get(current_plan, 0) >= PLAN_ORDER.get(required_plan, 0)


async def require_plan(request: web.Request, min_plan: str) -> web.Response | None:
    sub = await get_user_subscription(request["tg_id"])
    if not has_plan(sub["plan"], min_plan):
        plan_name = PLANS[min_plan]["name"]
        return web.json_response(
            {
                "error": f"Ця функція доступна на тарифі {plan_name} або вище. Оновіть підписку у вкладці «💎 Підписка».",
                "required_plan": min_plan,
            },
            status=402,
        )
    return None


async def activate_subscription(
    user_uuid: str,
    plan: str,
    period: str = "month",
    custom_days: int | None = None,
) -> str:
    days = custom_days if (custom_days and custom_days > 0) else (365 if period == "year" else 30)

    def _update():
        now = datetime.now(timezone.utc)
        base_dt = now
        try:
            u = supabase.table("users").select("subscription_plan, subscription_expires_at").eq("id", user_uuid).limit(1).execute().data
            if u and u[0].get("subscription_expires_at") and u[0].get("subscription_plan") == plan:
                cur_exp = datetime.fromisoformat(str(u[0]["subscription_expires_at"]).replace("Z", "+00:00"))
                if cur_exp > now:
                    base_dt = cur_exp
        except Exception:
            pass

        new_exp = (base_dt + timedelta(days=days)).isoformat()
        supabase.table("users").update(
            {"subscription_plan": plan, "subscription_expires_at": new_exp}
        ).eq("id", user_uuid).execute()
        return new_exp

    return await run_db(_update)


DEFAULT_WALLETS = {
    "USDT_TRC20": os.getenv("CRYPTO_WALLET_USDT_TRC20", ""),
    "USDT_TON": os.getenv("CRYPTO_WALLET_TON", ""),
    "USDC_BEP20": os.getenv("CRYPTO_WALLET_EVM", ""),
    "BTC": os.getenv("CRYPTO_WALLET_BTC", ""),
}


async def get_crypto_wallets() -> dict[str, str]:
    wallets = dict(DEFAULT_WALLETS)
    try:
        rows = await run_db(
            lambda: supabase.table("app_settings").select("key, value").like("key", "wallet_%").execute().data or []
        )
        for r in rows:
            k = str(r["key"]).replace("wallet_", "", 1)
            if r.get("value"):
                wallets[k] = r["value"]
    except Exception:
        pass
    return wallets


async def save_crypto_wallets(wallets: dict[str, str]) -> None:
    records = [{"key": f"wallet_{k}", "value": str(v or "").strip()} for k, v in wallets.items()]
    await run_db(lambda: supabase.table("app_settings").upsert(records, on_conflict="key").execute())


async def downgrade_expired_subscriptions(bot=None) -> int:
    """Downgrade users whose paid subscription has expired back to 'free'."""
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        expired = await run_db(
            lambda: supabase.table("users")
            .select("id, telegram_id, subscription_plan, subscription_expires_at")
            .neq("subscription_plan", "free")
            .lt("subscription_expires_at", now_iso)
            .execute()
            .data
            or []
        )
    except Exception:
        return 0

    count = 0
    for u in expired:
        try:
            await run_db(
                lambda uid=u["id"]: supabase.table("users")
                .update({"subscription_plan": "free", "subscription_expires_at": None})
                .eq("id", uid)
                .execute()
            )
            count += 1
            if bot and u.get("telegram_id"):
                try:
                    await bot.send_message(
                        u["telegram_id"],
                        f"⏳ <b>Термін вашої підписки {str(u.get('subscription_plan', '')).upper()} завершився.</b>\n"
                        "Акаунт автоматично переведено на базовий тариф <b>Free</b>.\n"
                        "Поновити підписку: /subscribe",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        except Exception:
            pass
    return count


async def get_effective_plans() -> dict[str, dict[str, Any]]:
    """Return PLANS merged with dynamic admin-configured prices from app_settings."""
    plans = copy.deepcopy(PLANS)
    try:
        rows = await run_db(
            lambda: supabase.table("app_settings")
            .select("value")
            .eq("key", "subscription_prices")
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows and rows[0].get("value"):
            overrides = json.loads(rows[0]["value"])
            for p_key in ("pro", "vip"):
                if isinstance(overrides.get(p_key), dict):
                    for field in ("stars_month", "stars_year"):
                        if field in overrides[p_key]:
                            val = int(overrides[p_key][field])
                            if val > 0:
                                plans[p_key][field] = val
                    for field in ("usd_month", "usd_year"):
                        if field in overrides[p_key]:
                            val_f = float(overrides[p_key][field])
                            if val_f > 0:
                                plans[p_key][field] = round(val_f, 2)
    except Exception:
        pass
    return plans


async def save_subscription_prices(prices: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    clean: dict[str, dict[str, Any]] = {}
    for p_key in ("pro", "vip"):
        src = prices.get(p_key) or {}
        clean[p_key] = {
            "stars_month": max(1, int(src.get("stars_month", PLANS[p_key]["stars_month"]))),
            "stars_year": max(1, int(src.get("stars_year", PLANS[p_key]["stars_year"]))),
            "usd_month": round(max(0.1, float(src.get("usd_month", PLANS[p_key]["usd_month"]))), 2),
            "usd_year": round(max(0.1, float(src.get("usd_year", PLANS[p_key]["usd_year"]))), 2),
        }
    await run_db(
        lambda: supabase.table("app_settings")
        .upsert({"key": "subscription_prices", "value": json.dumps(clean)}, on_conflict="key")
        .execute()
    )
    return await get_effective_plans()


async def get_user_active_promo(user_uuid: str) -> dict[str, Any] | None:
    try:
        u_rows = await run_db(
            lambda: supabase.table("users")
            .select("active_promo_code")
            .eq("id", user_uuid)
            .limit(1)
            .execute()
            .data
            or []
        )
        code = (u_rows[0].get("active_promo_code") if u_rows else None) or ""
        if not code:
            return None
        p_rows = await run_db(
            lambda: supabase.table("promo_codes")
            .select("*")
            .eq("code", code)
            .eq("is_active", True)
            .eq("promo_type", "discount")
            .limit(1)
            .execute()
            .data
            or []
        )
        if not p_rows:
            return None
        promo = p_rows[0]
        if promo.get("expires_at"):
            exp = datetime.fromisoformat(str(promo["expires_at"]).replace("Z", "+00:00"))
            if exp < datetime.now(timezone.utc):
                return None
        return promo
    except Exception:
        return None


async def redeem_promo_code(user_uuid: str, code_raw: str) -> dict[str, Any]:
    code = (code_raw or "").strip().upper()
    if not code:
        raise ValueError("Введіть промокод.")

    def _process():
        rows = supabase.table("promo_codes").select("*").eq("code", code).limit(1).execute().data or []
        if not rows:
            raise ValueError("Промокод не знайдено.")
        promo = rows[0]
        if not promo.get("is_active"):
            raise ValueError("Цей промокод деактивовано.")
        if promo.get("expires_at"):
            exp = datetime.fromisoformat(str(promo["expires_at"]).replace("Z", "+00:00"))
            if exp < datetime.now(timezone.utc):
                raise ValueError("Термін дії промокоду минув.")
        if int(promo.get("used_count") or 0) >= int(promo.get("max_uses") or 0):
            raise ValueError("Ліміт активацій цього промокоду вичерпано.")

        existing = (
            supabase.table("promo_redemptions")
            .select("id")
            .eq("promo_id", promo["id"])
            .eq("user_id", user_uuid)
            .limit(1)
            .execute()
            .data
        )
        if existing:
            raise ValueError("Ви вже активували цей промокод раніше.")

        supabase.table("promo_redemptions").insert({"promo_id": promo["id"], "user_id": user_uuid}).execute()
        supabase.table("promo_codes").update({"used_count": int(promo.get("used_count") or 0) + 1}).eq("id", promo["id"]).execute()
        return promo

    promo = await run_db(_process)
    if promo["promo_type"] == "trial":
        days = int(promo.get("duration_days") or 14)
        plan = str(promo.get("plan") or "pro").lower()
        exp_iso = await activate_subscription(user_uuid, plan=plan, custom_days=days)
        return {
            "status": "trial_activated",
            "plan": plan,
            "days": days,
            "expires_at": exp_iso,
            "message": f"🎉 Промокод активовано! Вам надано тариф {plan.upper()} на {days} дн.",
        }
    else:
        pct = int(promo.get("discount_percent") or 0)
        await run_db(lambda: supabase.table("users").update({"active_promo_code": code}).eq("id", user_uuid).execute())
        return {
            "status": "discount_activated",
            "discount_percent": pct,
            "plan": promo.get("plan"),
            "code": code,
            "message": f"🏷️ Промокод {code} активовано! Знижка -{pct}% застосована до оплати підписки.",
        }
