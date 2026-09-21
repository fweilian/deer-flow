"""Durable per-user preferences; updates touch only explicitly supplied keys."""

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from deerflow.persistence.user.model import UserPreferenceRow


def _preference_upsert_statement(dialect: str, *, user_id: str, key: str, value: object):
    """Build the dialect-specific statement used by the production repository."""
    if dialect == "mysql":
        statement = mysql_insert(UserPreferenceRow).values(user_id=user_id, key=key, value=value)
        return statement.on_duplicate_key_update(value=statement.inserted.value)
    else:
        statement = sqlite_insert(UserPreferenceRow).values(user_id=user_id, key=key, value=value)
    return statement.on_conflict_do_update(index_elements=["user_id", "key"], set_={"value": statement.excluded.value})


class UserPreferencesRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    async def get(self, user_id: str) -> dict:
        async with self.sessions() as session:
            rows = (await session.execute(select(UserPreferenceRow).where(UserPreferenceRow.user_id == user_id))).scalars()
            return {row.key: row.value for row in rows}

    async def patch(self, user_id: str, values: dict) -> None:
        async with self.sessions() as session, session.begin():
            dialect = session.bind.dialect.name
            # Consistent key order also avoids opposite-order row-lock cycles.
            for key, value in sorted(values.items()):
                await session.execute(_preference_upsert_statement(dialect, user_id=user_id, key=key, value=value))
