import time
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from backend.config import settings
from backend.models import Schedule,ScheduleOccurrence,Worker
from backend.queue import locked_db,enqueue_db,recover_db


def next_run(timezone,hour,minute,weekdays,after=None):
    now=datetime.fromtimestamp(after if after is not None else time.time(),ZoneInfo(timezone))
    for offset in range(9):
        day=now.date()+timedelta(days=offset)
        candidate=datetime(day.year,day.month,day.day,hour,minute,tzinfo=ZoneInfo(timezone))
        # Skip nonexistent wall times on DST spring-forward; ambiguous times run once.
        actual=datetime.fromtimestamp(candidate.timestamp(),ZoneInfo(timezone))
        if actual.hour!=hour or actual.minute!=minute:continue
        if day.weekday() in weekdays and candidate.timestamp()>now.timestamp():return candidate.timestamp()
    raise ValueError('未来 9 天没有可用执行时间')


def tick(now=None):
    now=now if now is not None else time.time()
    with locked_db() as db:
        worker=db.get(Worker,settings.worker_id)
        if not worker:worker=Worker(id=settings.worker_id,heartbeat_at=now,status='ONLINE');db.add(worker)
        worker.heartbeat_at=now
        if not settings.execution_enabled:return
        recover_db(db,now)
        for schedule in db.scalars(select(Schedule).where(Schedule.enabled==True,Schedule.next_run_at<=now).limit(100)):
            due=schedule.next_run_at
            status='QUEUED';execution_id=None;message=''
            if now-due>300 and schedule.misfire_policy=='skip':status='SKIPPED';message='错过计划超过 5 分钟，按策略跳过'
            else:
                try:
                    e,created=enqueue_db(db,schedule.task_id)
                    execution_id=e.id
                    if not created:status='ALREADY_DONE';message='相同业务周期任务已存在'
                except ValueError as exc:status='MANUAL_REQUIRED';message=str(exc)
            db.add(ScheduleOccurrence(schedule_id=schedule.id,due_at=due,status=status,execution_id=execution_id,message=message))
            schedule.last_run_at=due
            schedule.next_run_at=next_run(schedule.timezone,schedule.hour,schedule.minute,schedule.weekdays,now)
