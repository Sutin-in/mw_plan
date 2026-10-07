"""Alert thresholds (Wave 8A, D-33). Alerts themselves are derived, never stored."""

from __future__ import annotations

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ppr.domain.alerts import SETTING_DEFAULTS, AlertSetting
from ppr.infrastructure.db.schema import alert_setting
from ppr.ports import AlertSettingRecord


class SqlAlertSettingRepository:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def all(self) -> dict[AlertSetting, AlertSettingRecord]:
        out = {
            code: AlertSettingRecord(code, value, None, None)
            for code, value in SETTING_DEFAULTS.items()
        }
        for r in self._c.execute(select(alert_setting)):
            code = AlertSetting(r.code)
            out[code] = AlertSettingRecord(code, int(r.value), r.updated_at, r.updated_by_user_id)
        return out

    def set(self, code: AlertSetting, value: int, by: int) -> None:
        stmt = pg_insert(alert_setting).values(code=code.value, value=value, updated_by_user_id=by)
        self._c.execute(
            stmt.on_conflict_do_update(
                index_elements=[alert_setting.c.code],
                set_={"value": value, "updated_by_user_id": by, "updated_at": func.now()},
            )
        )
