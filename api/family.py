import uuid
from aiohttp import web
from db import supabase, run_db, get_exchange_rates
from services.subscriptions import require_plan


async def handle_family_overview(request: web.Request):
    user_uuid: str = request["user_uuid"]
    try:
        def _load():
            mem = supabase.table("family_members").select("group_id").eq("user_id", user_uuid).limit(1).execute().data
            if not mem:
                return None, [], 0.0
            gid = mem[0]["group_id"]
            grp_res = supabase.table("family_groups").select("*").eq("id", gid).limit(1).execute().data
            if not grp_res:
                return None, [], 0.0
            grp = grp_res[0]
            all_mems = supabase.table("family_members").select("user_id").eq("group_id", gid).execute().data or []
            uids = [m["user_id"] for m in all_mems]
            users = supabase.table("users").select("id, first_name, username").in_("id", uids).execute().data or []
            accs = supabase.table("accounts").select("user_id, balance, currency").in_("user_id", uids).execute().data or []
            return grp, users, accs

        grp, users, accs = await run_db(_load)
        if not grp:
            return web.json_response({"group": None, "members": [], "total_family_uah": 0})

        rates = await get_exchange_rates()
        bal_by_user: dict[str, float] = {}
        total_uah = 0.0
        for a in accs:
            uah = float(a.get("balance") or 0) * rates.get(str(a.get("currency") or "UAH").upper(), 1.0)
            bal_by_user[a["user_id"]] = bal_by_user.get(a["user_id"], 0.0) + uah
            total_uah += uah

        members = [
            {
                "id": u["id"],
                "name": u.get("first_name") or u.get("username") or "Учасник",
                "username": u.get("username"),
                "balance_uah": round(bal_by_user.get(u["id"], 0.0), 2),
                "is_owner": u["id"] == grp["owner_id"],
            }
            for u in users
        ]
        return web.json_response({"group": grp, "members": members, "total_family_uah": round(total_uah, 2)})
    except Exception:
        return web.json_response({"group": None, "members": [], "total_family_uah": 0, "migration_required": True})


async def handle_family_create(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    name = "Наша сімʼя"
    try:
        body = await request.json()
        if body.get("name"):
            name = str(body["name"]).strip()[:80]
    except Exception:
        pass

    invite_code = uuid.uuid4().hex[:8].upper()
    try:
        def _create():
            grp = (
                supabase.table("family_groups")
                .insert({"name": name, "invite_code": invite_code, "owner_id": user_uuid})
                .execute()
                .data[0]
            )
            supabase.table("family_members").upsert(
                {"group_id": grp["id"], "user_id": user_uuid}, on_conflict="user_id"
            ).execute()
            return grp

        grp = await run_db(_create)
        return web.json_response({"status": "created", "group": grp})
    except Exception as e:
        return web.json_response({"error": f"Помилка створення групи: {e}"}, status=400)


async def handle_family_join(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    code = str(body.get("invite_code") or "").strip().upper()
    if not code:
        return web.json_response({"error": "Введіть код запрошення"}, status=400)

    grp_res = await run_db(
        lambda: supabase.table("family_groups").select("*").eq("invite_code", code).limit(1).execute().data
    )
    if not grp_res:
        return web.json_response({"error": "Групу з таким кодом не знайдено"}, status=404)

    grp = grp_res[0]
    await run_db(
        lambda: supabase.table("family_members")
        .upsert({"group_id": grp["id"], "user_id": user_uuid}, on_conflict="user_id")
        .execute()
    )
    return web.json_response({"status": "joined", "group": grp})


async def handle_family_leave(request: web.Request):
    user_uuid: str = request["user_uuid"]
    await run_db(lambda: supabase.table("family_members").delete().eq("user_id", user_uuid).execute())
    return web.json_response({"status": "left"})
