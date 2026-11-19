from aiohttp import web
from db import supabase, run_db
from schemas import CategoryCreate, parse_json_body


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
