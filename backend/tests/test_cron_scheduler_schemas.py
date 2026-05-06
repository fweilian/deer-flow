from deerflow.persistence.models import CronJobFireRow, CronJobRow
from deerflow.runtime.scheduler.schemas import CronJobCreate, compute_next_fire_at


def test_resume_uses_system_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    payload = CronJobCreate(
        thread_id="thread-1",
        assistant_id="lead_agent",
        cron="0 9 * * *",
        timezone="Asia/Shanghai",
    )

    next_fire = compute_next_fire_at(payload.cron, payload.timezone, now=1_746_500_000)
    assert next_fire > 1_746_500_000


def test_scheduler_models_are_registered():
    assert CronJobRow.__tablename__ == "cron_jobs"
    assert CronJobFireRow.__tablename__ == "cron_job_fires"
    assert any(constraint.name == "uq_cron_job_fires_job_sched" for constraint in CronJobFireRow.__table__.constraints)
