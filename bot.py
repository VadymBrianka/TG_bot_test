import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommandScopeChat

from config import BOT_TOKEN, SERVER_PORT, ADMIN_ID
from middlewares.auth import cors_middleware, auth_middleware
from middlewares.bot_ban import BanCheckMiddleware
from services.scheduler import start_scheduler
from services.trading_bot import run_trading_bots_loop
from bot_handlers.commands import USER_COMMANDS, ADMIN_EXTRA_COMMANDS

from api.dashboard import handle_health, handle_get_currencies, handle_dashboard_summary
from api.transactions import (
    handle_list_transactions,
    handle_create_transaction,
    handle_update_transaction,
    handle_delete_transaction,
)
from api.accounts import handle_create_account, handle_update_account, handle_delete_account
from api.goals import (
    handle_create_goal,
    handle_update_goal,
    handle_delete_goal,
    handle_modify_goal_funds,
    handle_close_goal,
)
from api.investments import (
    handle_create_investment,
    handle_update_investment,
    handle_delete_investment,
    handle_refresh_investment_prices,
)
from api.analytics import handle_analytics_and_forecast
from api.market import handle_market_search, handle_market_quote
from api.categories import handle_get_categories, handle_create_category, handle_delete_category
from api.features import (
    handle_list_budgets,
    handle_create_budget,
    handle_delete_budget,
    handle_list_recurring,
    handle_create_recurring,
    handle_delete_recurring,
    handle_list_alerts,
    handle_create_alert,
    handle_delete_alert,
    handle_update_settings,
    handle_export_csv,
    handle_export_send_telegram,
    handle_import_csv,
)
from api.admin import (
    handle_admin_overview,
    handle_admin_set_role,
    handle_admin_ban_user,
    handle_admin_broadcast,
    handle_admin_generate_broadcast,
    handle_admin_refresh_rates,
    handle_admin_send_dm,
    handle_admin_set_subscription,
    handle_admin_review_payment,
    handle_admin_save_wallets,
    handle_admin_create_giveaway,
    handle_admin_draw_giveaway,
)
from api.receipts import handle_parse_receipt
from api.debts import handle_list_debts, handle_create_debt, handle_settle_debt, handle_delete_debt
from api.subscriptions import handle_get_plans, handle_create_stars_invoice, handle_create_crypto_request
from api.trading import (
    handle_trading_overview,
    handle_trading_demo_reset,
    handle_trading_transfer,
    handle_trading_crypto_deposit,
    handle_trading_save_binance_keys,
    handle_trading_create_bot,
    handle_trading_toggle_bot,
    handle_trading_step_bot,
    handle_trading_delete_bot,
)

from bot_handlers import admin, commands, quick_entry, callbacks, subscriptions

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.message.outer_middleware(BanCheckMiddleware())
dp.callback_query.outer_middleware(BanCheckMiddleware())
dp.include_router(admin.router)
dp.include_router(subscriptions.router)
dp.include_router(commands.router)
dp.include_router(callbacks.router)
dp.include_router(quick_entry.router)


def create_web_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware, auth_middleware])
    app["bot"] = bot

    # Health & Public
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/v1/currencies", handle_get_currencies)
    app.router.add_get("/api/v1/market/search", handle_market_search)
    app.router.add_get("/api/v1/market/quote", handle_market_quote)

    # Dashboard
    app.router.add_get("/api/v1/dashboard/summary", handle_dashboard_summary)

    # Transactions
    app.router.add_get("/api/v1/transactions", handle_list_transactions)
    app.router.add_post("/api/v1/transactions", handle_create_transaction)
    app.router.add_put("/api/v1/transactions/{id}", handle_update_transaction)
    app.router.add_delete("/api/v1/transactions/{id}", handle_delete_transaction)

    # Categories
    app.router.add_get("/api/v1/categories", handle_get_categories)
    app.router.add_post("/api/v1/categories", handle_create_category)
    app.router.add_delete("/api/v1/categories/{id}", handle_delete_category)

    # Accounts
    app.router.add_post("/api/v1/accounts", handle_create_account)
    app.router.add_put("/api/v1/accounts/{id}", handle_update_account)
    app.router.add_delete("/api/v1/accounts/{id}", handle_delete_account)

    # Goals
    app.router.add_post("/api/v1/goals", handle_create_goal)
    app.router.add_put("/api/v1/goals/{id}", handle_update_goal)
    app.router.add_post("/api/v1/goals/funds", handle_modify_goal_funds)
    app.router.add_post("/api/v1/goals/{id}/close", handle_close_goal)
    app.router.add_delete("/api/v1/goals/{id}", handle_delete_goal)

    # Investments
    app.router.add_post("/api/v1/investments", handle_create_investment)
    app.router.add_put("/api/v1/investments/{id}", handle_update_investment)
    app.router.add_delete("/api/v1/investments/{id}", handle_delete_investment)
    app.router.add_post("/api/v1/investments/refresh-prices", handle_refresh_investment_prices)

    # Analytics
    app.router.add_get("/api/v1/analytics/forecast", handle_analytics_and_forecast)

    # Budgets, Recurring, Alerts, Settings, CSV
    app.router.add_get("/api/v1/budgets", handle_list_budgets)
    app.router.add_post("/api/v1/budgets", handle_create_budget)
    app.router.add_delete("/api/v1/budgets/{id}", handle_delete_budget)

    app.router.add_get("/api/v1/recurring", handle_list_recurring)
    app.router.add_post("/api/v1/recurring", handle_create_recurring)
    app.router.add_delete("/api/v1/recurring/{id}", handle_delete_recurring)

    app.router.add_get("/api/v1/alerts", handle_list_alerts)
    app.router.add_post("/api/v1/alerts", handle_create_alert)
    app.router.add_delete("/api/v1/alerts/{id}", handle_delete_alert)

    app.router.add_put("/api/v1/settings", handle_update_settings)
    app.router.add_get("/api/v1/export/csv", handle_export_csv)
    app.router.add_post("/api/v1/export/send-telegram", handle_export_send_telegram)
    app.router.add_post("/api/v1/import/csv", handle_import_csv)

    # Receipts AI
    app.router.add_post("/api/v1/receipts/parse", handle_parse_receipt)

    # Debts
    app.router.add_get("/api/v1/debts", handle_list_debts)
    app.router.add_post("/api/v1/debts", handle_create_debt)
    app.router.add_post("/api/v1/debts/{id}/settle", handle_settle_debt)
    app.router.add_delete("/api/v1/debts/{id}", handle_delete_debt)

    # Subscriptions
    app.router.add_get("/api/v1/subscriptions/plans", handle_get_plans)
    app.router.add_post("/api/v1/subscriptions/stars-invoice", handle_create_stars_invoice)
    app.router.add_post("/api/v1/subscriptions/crypto-request", handle_create_crypto_request)

    # VIP Trading Bot
    app.router.add_get("/api/v1/trading/overview", handle_trading_overview)
    app.router.add_post("/api/v1/trading/demo/reset", handle_trading_demo_reset)
    app.router.add_post("/api/v1/trading/transfer", handle_trading_transfer)
    app.router.add_post("/api/v1/trading/crypto-deposit", handle_trading_crypto_deposit)
    app.router.add_put("/api/v1/trading/binance-keys", handle_trading_save_binance_keys)
    app.router.add_post("/api/v1/trading/bots", handle_trading_create_bot)
    app.router.add_post("/api/v1/trading/bots/{id}/toggle", handle_trading_toggle_bot)
    app.router.add_post("/api/v1/trading/bots/{id}/step", handle_trading_step_bot)
    app.router.add_delete("/api/v1/trading/bots/{id}", handle_trading_delete_bot)

    # Admin
    app.router.add_get("/api/v1/admin/overview", handle_admin_overview)
    app.router.add_put("/api/v1/admin/users/{id}/role", handle_admin_set_role)
    app.router.add_put("/api/v1/admin/users/{id}/ban", handle_admin_ban_user)
    app.router.add_put("/api/v1/admin/users/{id}/subscription", handle_admin_set_subscription)
    app.router.add_post("/api/v1/admin/users/{id}/dm", handle_admin_send_dm)
    app.router.add_post("/api/v1/admin/payments/{id}/review", handle_admin_review_payment)
    app.router.add_put("/api/v1/admin/wallets", handle_admin_save_wallets)
    app.router.add_post("/api/v1/admin/giveaways", handle_admin_create_giveaway)
    app.router.add_post("/api/v1/admin/giveaways/{id}/draw", handle_admin_draw_giveaway)
    app.router.add_post("/api/v1/admin/broadcast", handle_admin_broadcast)
    app.router.add_post("/api/v1/admin/broadcast/generate", handle_admin_generate_broadcast)
    app.router.add_post("/api/v1/admin/refresh-rates", handle_admin_refresh_rates)

    return app


async def main():
    asyncio.create_task(start_scheduler(bot))
    asyncio.create_task(run_trading_bots_loop(bot))

    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", SERVER_PORT)
    await site.start()
    logging.info(f"API сервер запущено на порті {SERVER_PORT}...")

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await bot.set_my_commands(USER_COMMANDS)
        await bot.set_my_commands(
            USER_COMMANDS + ADMIN_EXTRA_COMMANDS,
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )
    except Exception as e:
        logging.warning(f"Failed to set bot commands: {e}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
