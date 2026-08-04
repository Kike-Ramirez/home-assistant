"""PostgREST client (schema `home` — see db/schema.sql) built on `postgrest-py`.

We don't reimplement query building or error parsing: `postgrest-py` (the
official PostgREST/Supabase client) already does both — it respects the
resource embedding we use (`device_standard(standard(...))`) and turns
PostgREST errors into `postgrest.APIError` with code/message/hint/detail
instead of a generic `HTTPStatusError`.

Methods keep the same signature they had before (home project, a single
service role — see the comment in db/schema.sql) so call sites don't need
touching: filters are still passed as `{"column": "operator.value"}` (e.g.
`{"status": "eq.active"}`), and get translated here into query-builder calls
(`.eq()`, `.lte()`, ...).
"""

from __future__ import annotations

from typing import Any

from postgrest import AsyncPostgrestClient

_FILTER_METHODS = {"eq", "neq", "gt", "gte", "lt", "lte", "like", "ilike", "is", "in"}


class PostgrestClient:
    def __init__(self, base_url: str, schema: str = "home") -> None:
        self._client = AsyncPostgrestClient(base_url, schema=schema)

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _apply_filters(query: Any, filters: dict[str, Any] | None) -> Any:
        for column, raw in (filters or {}).items():
            if column == "select":
                continue
            operator, _, value = str(raw).partition(".")
            if operator not in _FILTER_METHODS:
                raise ValueError(f"Unsupported PostgREST operator: {operator!r} (column {column!r})")
            method = getattr(query, operator if operator not in ("is", "in") else f"{operator}_")
            query = method(column, value)
        return query

    async def select(self, table: str, params: dict[str, Any] | None = None) -> list[dict]:
        columns = (params or {}).get("select", "*")
        query = self._client.from_(table).select(columns)
        query = self._apply_filters(query, params)
        resp = await query.execute()
        return resp.data

    async def insert(self, table: str, payload: dict[str, Any]) -> dict:
        resp = await self._client.from_(table).insert(payload).execute()
        return resp.data[0]

    async def upsert_on_conflict(self, table: str, payload: dict[str, Any], on_conflict: str) -> dict:
        resp = await self._client.from_(table).upsert(payload, on_conflict=on_conflict).execute()
        return resp.data[0]

    async def patch(self, table: str, params: dict[str, Any], payload: dict[str, Any]) -> dict:
        query = self._client.from_(table).update(payload)
        query = self._apply_filters(query, params)
        resp = await query.execute()
        return resp.data[0]

    async def patch_if_match(self, table: str, params: dict[str, Any], payload: dict[str, Any]) -> dict | None:
        """Same as `patch()`, but returns `None` instead of raising when the
        filter matches zero rows, rather than assuming exactly one row always
        matches. For optimistic-concurrency callers (e.g.
        `conversation.py::update_state`) that add an `updated_at=eq....`
        filter alongside the row id — a `None` here means someone else's
        write landed first, not a real error."""
        query = self._client.from_(table).update(payload)
        query = self._apply_filters(query, params)
        resp = await query.execute()
        return resp.data[0] if resp.data else None

    async def rpc(self, function_name: str, payload: dict[str, Any]) -> Any:
        resp = await self._client.rpc(function_name, payload).execute()
        return resp.data
