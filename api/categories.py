from aiohttp import web
from db import supabase, run_db
from schemas import CategoryCreate, parse_json_body
from services.subscriptions import require_plan
from services.analytics_events import track_event


async def match_category_rule(user_uuid: str, text: str) -> str | None:
    """Return matching category_id if user has an auto-categorization rule matching text."""
    if not user_uuid or not text:
        return None
    low = text.lower()
    try:
        rules = await run_db(
            lambda: supabase.table("category_rules")
            .select("keyword, category_id")
            .eq("user_id", user_uuid)
            .execute()
            .data
            or []
        )
        # Prefer longest matching keyword first
        rules.sort(key=lambda r: len(str(r.get("keyword") or "")), reverse=True)
        for r in rules:
            kw = str(r.get("keyword") or "").strip().lower()
            if kw and kw in low:
                return r.get("category_id")
    except Exception:
        pass
    return None


async def handle_get_categories(request: web.Request):
    user_uuid: str = request["user_uuid"]
    try:
        def _q():
            try:
                return (
                    supabase.table("categories")
                    .select("*")
                    .or_(f"user_id.is.null,user_id.eq.{user_uuid}")
                    .order("name")
                    .execute()
                    .data
                    or []
                )
            except Exception:
                return supabase.table("categories").select("*").order("name").execute().data or []

        cats = await run_db(_q)
        has_other_exp = any(c["name"] == "Інше" and c["type"] == "expense" for c in cats)
        has_other_inc = any(c["name"] == "Інше" and c["type"] == "income" for c in cats)
        if not has_other_exp:
            cats.append({"id": "other_expense", "name": "Інше", "type": "expense", "icon": "⚪"})
        if not has_other_inc:
            cats.append({"id": "other_income", "name": "Інше", "type": "income", "icon": "📦"})
        return web.json_response({"categories": cats})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_create_category(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied

    body, err_resp = await parse_json_body(request, CategoryCreate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("categories")
            .insert(
                {
                    "user_id": user_uuid,
                    "name": body.name.strip(),
                    "type": body.type,
                    "icon": body.icon.strip() or "🏷️",
                }
            )
            .execute()
        )
        await track_event(user_uuid, "custom_category_create")
        return web.json_response({"status": "success", "category": res.data[0]})
    except Exception as e:
        return web.json_response({"error": f"Не вдалося створити категорію (перевірте міграцію БД): {e}"}, status=400)


async def handle_delete_category(request: web.Request):
    cat_id = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("categories").delete().eq("id", cat_id).eq("user_id", user_uuid).execute()
        )
        if not res.data:
            return web.json_response({"error": "Категорію не знайдено або вона системна"}, status=404)
        return web.json_response({"status": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def handle_list_category_rules(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    try:
        rules = await run_db(
            lambda: supabase.table("category_rules")
            .select("id, keyword, category_id, categories(name, icon, type)")
            .eq("user_id", user_uuid)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        return web.json_response({"rules": rules})
    except Exception as e:
        return web.json_response({"rules": [], "error": str(e)})


async def handle_create_category_rule(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    keyword = str(body.get("keyword") or "").strip().lower()
    category_id = str(body.get("category_id") or "").strip()
    if not keyword or len(keyword) < 2 or not category_id:
        return web.json_response({"error": "Вкажіть ключове слово (мін. 2 символи) та категорію"}, status=400)

    try:
        res = await run_db(
            lambda: supabase.table("category_rules")
            .upsert(
                {"user_id": user_uuid, "keyword": keyword[:60], "category_id": category_id},
                on_conflict="user_id,keyword",
            )
            .execute()
        )
        await track_event(user_uuid, "category_rule_create")
        return web.json_response({"status": "success", "rule": res.data[0] if res.data else None})
    except Exception as e:
        return web.json_response({"error": f"Помилка збереження правила: {e}"}, status=400)


async def handle_delete_category_rule(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    rule_id = request.match_info.get("id")
    try:
        await run_db(
            lambda: supabase.table("category_rules").delete().eq("id", rule_id).eq("user_id", user_uuid).execute()
        )
        return web.json_response({"status": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
