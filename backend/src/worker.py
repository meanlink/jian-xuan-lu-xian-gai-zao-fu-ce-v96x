import time, json, logging
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, JobSegment, Report
from src.services import business, jobs
from src.services.reports import deliver
from src.services.audit import record
logging.basicConfig(level=logging.INFO)


def execute_segment(claim):
    """计算一段并把汇总写回；暂停或重试导致的过期写回不会推进进度。"""
    if claim is None:
        return False
    with Session() as db:
        seg = db.get(JobSegment, claim['segment_id'])
        job = db.get(Job, claim['job_id'])
        if not seg or not job:
            return False
        rows = seg.input or []
        try:
            summary = business.summarize(rows)
            json.dumps(summary, allow_nan=False)
        except Exception as exc:
            logging.exception('Segment failed')
            outcome = jobs.fail_segment(db, claim, str(exc))
            record(db, 'worker', 'job.segment_failed', {'job': job.id, 'segment': seg.segment_index, 'outcome': outcome})
            db.commit()
            return False
        outcome = jobs.complete_segment(db, claim, summary)
        record(db, 'worker', 'job.segment', {'job': job.id, 'segment': seg.segment_index, 'outcome': outcome})
        if outcome == 'saved':
            db.expire_all()
            if db.get(Job, job.id).status == 'completed':
                record(db, 'worker', 'job.completed', {'id': job.id})
        db.commit()
        return outcome == 'saved'


def run_once(max_segments=12, segment_delay=0.0):
    """连续领取并执行若干段。每次领取都重新读库，确保暂停立即生效。

    segment_delay 用于模拟一段的耗时（验收暂停/公平轮转时使用）。
    """
    done = 0
    for _ in range(max_segments):
        with Session() as db:
            claim = jobs.claim_next(db)
            db.commit()
        if not claim:
            break
        if execute_segment(claim):
            done += 1
        if segment_delay > 0:
            time.sleep(segment_delay)
    return done


def tick():
    run_once()
    with Session() as db:
        now = time.time()
        for r in db.scalars(select(Report).where(Report.enabled == True, Report.next_run <= now)):
            d = deliver(db, r)
            if d:
                record(db, 'worker', 'report.delivered', {'id': r.id, 'delivery': d.id})
        db.commit()


def main():
    r = Redis.from_url(REDIS_URL, socket_timeout=3)
    # This Compose stack has a single worker. A crash must not strand running segments.
    with Session() as db:
        for job_id in jobs.recover(db):
            record(db, 'worker', 'job.recovered', {'id': job_id})
        db.commit()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item = r.blpop('analysis:queue', timeout=1)
            if item:
                run_once()
            tick()
        except Exception:
            logging.exception('Worker retry')
            time.sleep(2)


if __name__ == '__main__':
    main()
