from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from deerflow.persistence.models import CronJobFireRow, CronJobRow
from deerflow.runtime.scheduler.schemas import CronJobCreate, compute_next_fire_at


def test_compute_next_fire_at_returns_expected_utc_timestamp():
    payload = CronJobCreate(
        thread_id="thread-1",
        assistant_id="lead_agent",
        cron="0 9 * * *",
        timezone="Asia/Shanghai",
    )

    now = datetime(2025, 1, 1, 0, 30, tzinfo=UTC)
    expected = datetime(2025, 1, 1, 1, 0, tzinfo=UTC).timestamp()

    next_fire = compute_next_fire_at(payload.cron, payload.timezone, now=now)

    assert next_fire == expected


def test_cron_job_create_rejects_invalid_cron():
    with pytest.raises(ValidationError, match=r"Invalid cron expression: not-a-cron"):
        CronJobCreate(
            thread_id="thread-1",
            assistant_id="lead_agent",
            cron="not-a-cron",
            timezone="Asia/Shanghai",
        )


def test_cron_job_create_rejects_invalid_timezone():
    with pytest.raises(ValidationError, match=r"Unknown timezone: Mars/Olympus"):
        CronJobCreate(
            thread_id="thread-1",
            assistant_id="lead_agent",
            cron="0 9 * * *",
            timezone="Mars/Olympus",
        )


def test_compute_next_fire_at_rejects_invalid_cron():
    with pytest.raises(ValueError, match=r"Invalid cron expression: not-a-cron"):
        compute_next_fire_at("not-a-cron", "Asia/Shanghai", now=datetime(2025, 1, 1, tzinfo=UTC))


def test_compute_next_fire_at_rejects_invalid_timezone():
    with pytest.raises(ValueError, match=r"Unknown timezone: Mars/Olympus"):
        compute_next_fire_at("0 9 * * *", "Mars/Olympus", now=datetime(2025, 1, 1, tzinfo=UTC))


def test_scheduler_models_are_registered():
    assert CronJobRow.__tablename__ == "cron_jobs"
    assert CronJobFireRow.__tablename__ == "cron_job_fires"
    assert any(constraint.name == "uq_cron_job_fires_job_sched" for constraint in CronJobFireRow.__table__.constraints)
