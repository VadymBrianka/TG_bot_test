from datetime import datetime, timezone
from db import supabase, run_db


async def touch_user_active(user_uuid: str) -> None:
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        await run_db(
            lambda: supabase.table("users")
            .update({"last_active_at": now_iso})
            .eq("id", user_uuid)
            .execute()
        )
    except Exception:
        pass


async def track_event(user_uuid: str, event_name: str) -> None:
    if not user_uuid or not event_name:
        return
    try:
        await run_db(
            lambda: supabase.table("analytics_events")
            .insert({"user_id": user_uuid, "event_name": event_name[:64]})
            .execute()
        )
    except Exception:
        pass
