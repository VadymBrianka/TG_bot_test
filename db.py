import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar
from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_KEY, ADMIN_ID

T = TypeVar("T")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


async def run_db(fn: Callable[[], T]) -> T:
    """Execute a synchronous Supabase call in a worker thread without blocking the event loop."""
    return await asyncio.to_thread(fn)


async def get_user_role(tg_id: int) -> str:
    """Return user's role ('admin' or 'user'), falling back to ADMIN_ID if role column is missing."""
    def _q():
        try:
            res = supabase.table("users").select("role").eq("telegram_id", tg_id).limit(1).execute()
            if res.data and res.data[0].get("role"):
                return str(res.data[0]["role"]).lower()
        except Exception:
            pass
        return "admin" if tg_id == ADMIN_ID else "user"
    return await run_db(_q)


async def is_admin(tg_id: int) -> bool:
    return (await get_user_role(tg_id)) == "admin"


async def get_user_by_tg_id(tg_id: int) -> dict[str, Any] | None:
    def _q():
        res = supabase.table("users").select("*").eq("telegram_id", tg_id).limit(1).execute()
        return res.data[0] if res.data else None
    return await run_db(_q)


async def get_user_uuid_by_tg_id(tg_id: int) -> str | None:
    def _q():
        res = supabase.table("users").select("id").eq("telegram_id", tg_id).limit(1).execute()
        return res.data[0]["id"] if res.data else None
    return await run_db(_q)


async def get_exchange_rates() -> dict[str, float]:
    def _q():
        res = supabase.table("exchange_rates").select("currency, rate_to_base").execute()
        rates = {
            r["currency"]: float(r["rate_to_base"])
            for r in (res.data or [])
            if r.get("currency") and r.get("currency") != "USDT"
        }
        rates["UAH"] = 1.0
        return rates
    return await run_db(_q)


async def get_default_account(user_uuid: str) -> dict[str, Any] | None:
    """Return user's preferred default_account_id if set, otherwise first account ordered by created_at."""
    def _q():
        try:
            u_res = supabase.table("users").select("default_account_id").eq("id", user_uuid).limit(1).execute()
            def_id = u_res.data[0].get("default_account_id") if u_res.data else None
            if def_id:
                acc_res = supabase.table("accounts").select("*").eq("id", def_id).eq("user_id", user_uuid).limit(1).execute()
                if acc_res.data:
                    return acc_res.data[0]
        except Exception:
            pass
        res = supabase.table("accounts").select("*").eq("user_id", user_uuid).order("created_at").limit(1).execute()
        return res.data[0] if res.data else None
    return await run_db(_q)


async def create_transaction_atomic(
    user_uuid: str,
    account_id: str,
    to_account_id: str | None,
    category_id: str | None,
    amount: float,
    target_amount: float | None,
    tx_type: str,
    note: str,
    transaction_date: str | None = None,
) -> dict[str, Any]:
    """Create a transaction and update account balance(s) atomically via RPC, with safe fallback."""
    payload = {
        "p_user_id": user_uuid,
        "p_account_id": account_id,
        "p_to_account_id": to_account_id if tx_type == "transfer" else None,
        "p_category_id": category_id if tx_type != "transfer" else None,
        "p_amount": amount,
        "p_target_amount": target_amount if tx_type == "transfer" else None,
        "p_type": tx_type,
        "p_note": note,
        "p_transaction_date": transaction_date or datetime.now(timezone.utc).isoformat(),
    }

    def _try_rpc():
        return supabase.rpc("create_transaction_atomic", payload).execute()

    try:
        rpc_res = await run_db(_try_rpc)
        tx_id = rpc_res.data
        if tx_id:
            def _fetch():
                r = supabase.table("transactions").select("*").eq("id", tx_id).eq("user_id", user_uuid).limit(1).execute()
                return r.data[0] if r.data else {"id": tx_id}
            return await run_db(_fetch)
    except Exception as rpc_err:
        logging.debug(f"RPC create_transaction_atomic unavailable, using fallback: {rpc_err}")

    def _fallback():
        acc_from_res = supabase.table("accounts").select("id, balance").eq("id", account_id).eq("user_id", user_uuid).execute().data
        if not acc_from_res:
            raise ValueError("Рахунок не знайдено або не належить користувачу")
        acc_from_bal = float(acc_from_res[0]["balance"])

        if tx_type == "expense":
            supabase.table("accounts").update({"balance": acc_from_bal - amount}).eq("id", account_id).eq("user_id", user_uuid).execute()
        elif tx_type == "income":
            supabase.table("accounts").update({"balance": acc_from_bal + amount}).eq("id", account_id).eq("user_id", user_uuid).execute()
        elif tx_type == "transfer":
            if not to_account_id or to_account_id == account_id:
                raise ValueError("Оберіть два різних рахунки для переказу")
            acc_to_res = supabase.table("accounts").select("id, balance").eq("id", to_account_id).eq("user_id", user_uuid).execute().data
            if not acc_to_res:
                raise ValueError("Рахунок отримувача не знайдено")
            acc_to_bal = float(acc_to_res[0]["balance"])
            t_amt = target_amount if target_amount is not None else amount
            supabase.table("accounts").update({"balance": acc_from_bal - amount}).eq("id", account_id).eq("user_id", user_uuid).execute()
            supabase.table("accounts").update({"balance": acc_to_bal + t_amt}).eq("id", to_account_id).eq("user_id", user_uuid).execute()

        insert_data: dict[str, Any] = {
            "user_id": user_uuid,
            "account_id": account_id,
            "to_account_id": to_account_id if tx_type == "transfer" else None,
            "category_id": category_id if tx_type != "transfer" else None,
            "amount": amount,
            "target_amount": target_amount if tx_type == "transfer" else None,
            "type": tx_type,
            "note": note,
        }
        if transaction_date:
            insert_data["transaction_date"] = transaction_date
        ins = supabase.table("transactions").insert(insert_data).execute()
        return ins.data[0] if ins.data else {}

    return await run_db(_fallback)


def apply_tx_balance_effect_sync(tx: dict[str, Any], user_uuid: str, reverse: bool = False) -> None:
    """Apply or reverse a transaction's balance effect synchronously (used inside run_db)."""
    sign = -1.0 if reverse else 1.0
    tx_type = tx.get("type")
    amount = float(tx.get("amount") or 0)
    t_amount = float(tx.get("target_amount") or amount)
    acc_id = tx.get("account_id")
    to_acc_id = tx.get("to_account_id")

    if acc_id:
        res = supabase.table("accounts").select("balance").eq("id", acc_id).eq("user_id", user_uuid).limit(1).execute().data
        if res:
            bal = float(res[0]["balance"])
            if tx_type == "expense":
                supabase.table("accounts").update({"balance": bal - sign * amount}).eq("id", acc_id).eq("user_id", user_uuid).execute()
            elif tx_type == "income":
                supabase.table("accounts").update({"balance": bal + sign * amount}).eq("id", acc_id).eq("user_id", user_uuid).execute()
            elif tx_type == "transfer":
                supabase.table("accounts").update({"balance": bal - sign * amount}).eq("id", acc_id).eq("user_id", user_uuid).execute()

    if tx_type == "transfer" and to_acc_id:
        res_to = supabase.table("accounts").select("balance").eq("id", to_acc_id).eq("user_id", user_uuid).limit(1).execute().data
        if res_to:
            bal_to = float(res_to[0]["balance"])
            supabase.table("accounts").update({"balance": bal_to + sign * t_amount}).eq("id", to_acc_id).eq("user_id", user_uuid).execute()
