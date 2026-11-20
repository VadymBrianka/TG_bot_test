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
            "До 2 рахунків та 2 цілей",
            "Ручний облік доходів, витрат і переказів",
            "Швидкий запис у чаті бота",
            "Конвертер валют за курсом НБУ",
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
            "Необмежена кількість рахунків і цілей",
            "📷 ШІ-сканер чеків (камера + фото в чат)",
            "🔮 Прогноз та мультивалютна аналітика",
            "📊 Бюджети з лімітами та 🔁 регулярні платежі",
            "🤝 Трекер боргів та позик",
            "📁 Експорт у Telegram та імпорт CSV",
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
            "📈 Портфель активів (акції, крипта, депозити, ОВДП)",
            "🔄 Автооновлення котирувань з бірж",
            "🚨 Алерти цін на акції та криптовалюту",
            "📈 Симулятор складного відсотка з графіком",
            "📬 Тижневі та місячні авто-дайджести",
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
