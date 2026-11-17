"""分段推进的验收剧本。

覆盖：分段推进不挡住后来的短分析、暂停保留已完成段、暂停后迟到写回
不推进、并发重试同段不重复累计、恢复与重启续算、各负责人轮流推进、
全部段完成才发布可批准结论、以及工作台可查询的等待原因与分段进度。
"""
import threading
import time

from sqlalchemy import select, func

from src.models import Job, JobSegment, Dataset, Delivery, Report, User
from src.services import business, jobs as jobsvc
from src.services.reports import deliver
from src.worker import run_once, execute_segment, tick


def _new(db, name, owner_role='analyst', params=None):
    from src.schemas.job import JobInput
    user = db.scalar(select(User).where(User.role == owner_role))
    j = jobsvc.create(db, JobInput(name=name, dataset_id=db.scalar(select(Dataset)).id, params=params or {}), user)
    db.commit()
    return j


def test_segmented_job_does_not_block_a_later_short_job(seeded):
    db = seeded
    long_job = _new(db, '整批复测', 'analyst')
    short_job = _new(db, '短分析', 'admin', {'team': '四组'})
    long_total = db.scalar(select(func.count()).select_from(JobSegment).where(JobSegment.job_id == long_job.id))
    short_total = db.scalar(select(func.count()).select_from(JobSegment).where(JobSegment.job_id == short_job.id))
    assert short_total < long_total

    # 如果按任务整批执行，短分析要等长任务全部算完；分段推进后应先完成短分析。
    completion = []
    for _ in range(1000):
        claim = jobsvc.claim_next(db)
        db.commit()
        if not claim:
            break
        assert execute_segment(claim)
        db.expire_all()
        short = db.get(Job, short_job.id)
        if short.status == 'completed' and short_job.id not in [c[0] for c in completion]:
            completion.append((short_job.id, len(completion)))
        if db.get(Job, long_job.id).status == 'completed':
            completion.append((long_job.id, len(completion)))
            break
    assert completion[0][0] == short_job.id
    assert db.get(Job, long_job.id).status == 'completed'


def test_multi_owner_rotation_and_fairness(seeded):
    db = seeded
    owners = {}
    for role in ('analyst', 'admin'):
        owners[role] = _new(db, f'{role}-长任务', role)
        owner_id = owners[role].created_by
        from src.schemas.job import JobInput
        from src.models import User as UserModel
        user = db.get(UserModel, owner_id)
        for n in range(2):
            jobsvc.create(db, JobInput(name=f'{role}-追加{n}', dataset_id=db.scalar(select(Dataset)).id, params={}), user)
    db.commit()
    order = []
    for _ in range(1000):
        claim = jobsvc.claim_next(db)
        db.commit()
        if not claim:
            break
        order.append(db.get(Job, claim['job_id']).created_by)
        time.sleep(0.002)
        assert execute_segment(claim)
    # 严格轮流：前 20 次领取两位负责人各占一半，一方不会挤掉另一方
    from collections import Counter
    counts = Counter(order[:20])
    assert len(counts) == 2
    assert min(counts.values()) == 10
    # 同一负责人有 3 项待办时，仍先让对方推进一段
    for a, b in zip(order, order[1:]):
        assert a != b


def test_pause_preserves_done_segments_and_resume_continues(seeded):
    db = seeded
    j = _new(db, '可暂停任务')
    for _ in range(3):
        claim = jobsvc.claim_next(db)
        db.commit()
        assert execute_segment(claim)
    db.expire_all()
    done = {s.id for s in db.scalars(select(JobSegment).where(JobSegment.job_id == j.id, JobSegment.status == 'completed'))}
    assert len(done) == 3
    claim = jobsvc.claim_next(db)
    db.commit()
    jobsvc.pause(db, db.get(Job, j.id))
    db.commit()
    db.expire_all()
    assert db.get(Job, j.id).status == 'paused'
    # 暂停后迟到写回不推进
    assert jobsvc.complete_segment(db, claim, business.summarize([])) == 'stale'
    db.commit()
    db.expire_all()
    assert {s.id for s in db.scalars(select(JobSegment).where(JobSegment.job_id == j.id, JobSegment.status == 'completed'))} == done
    assert jobsvc.claim_next(db) is None
    assert '暂停' in jobsvc.job_dict(db, db.get(Job, j.id))['waiting_reason']
    # 恢复接着算
    _, changed = jobsvc.resume(db, db.get(Job, j.id))
    assert changed
    db.commit()
    run_once(max_segments=1000)
    db.expire_all()
    final = db.get(Job, j.id)
    assert final.status == 'completed'
    assert final.result['summary'] == business.analyze(db.scalar(select(Dataset)).records, final.params)['summary']


def test_concurrent_writes_to_same_segment_do_not_double_count(seeded):
    db = seeded
    j = _new(db, '并发重试')
    total = db.scalar(select(func.count()).select_from(JobSegment).where(JobSegment.job_id == j.id))
    claim = jobsvc.claim_next(db)
    db.commit()
    seg = db.get(JobSegment, claim['segment_id'])
    summary = business.summarize(seg.input)
    results = []
    errors = []
    barrier = threading.Barrier(4)

    def writer():
        from src.config.database import Session
        try:
            barrier.wait()
            with Session() as s:
                results.append(jobsvc.complete_segment(s, claim, summary))
                s.commit()
        except Exception as exc:  # sqlite 下并发写可能报锁；等价于重试未生效
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    db.expire_all()
    seg = db.get(JobSegment, seg.id)
    assert seg.status == 'completed'
    assert results.count('saved') == 1
    assert db.scalar(select(func.count()).select_from(JobSegment).where(JobSegment.job_id == j.id)) == total
    # 段输入没有被重复计数
    run_once(max_segments=1000)
    db.expire_all()
    final = db.get(Job, j.id)
    operators = [r['operator'] for r in final.result['table']['rows']]
    assert len(operators) == len(set(operators))
    assert len(operators) == len(business.prepare(db.scalar(select(Dataset)).records, final.params)[2])


def test_restart_recovers_partial_progress(seeded):
    db = seeded
    j = _new(db, '重启恢复')
    for _ in range(2):
        claim = jobsvc.claim_next(db)
        db.commit()
        assert execute_segment(claim)
    claim = jobsvc.claim_next(db)
    db.commit()
    done = {s.id for s in db.scalars(select(JobSegment).where(JobSegment.job_id == j.id, JobSegment.status == 'completed'))}
    assert jobsvc.recover(db) == [j.id]
    db.commit()
    db.expire_all()
    assert db.get(Job, j.id).status == 'queued'
    assert db.get(JobSegment, claim['segment_id']).status == 'pending'
    assert {s.id for s in db.scalars(select(JobSegment).where(JobSegment.job_id == j.id, JobSegment.status == 'completed'))} == done
    run_once(max_segments=1000)
    db.expire_all()
    final = db.get(Job, j.id)
    assert final.status == 'completed'
    assert final.result['summary'] == business.analyze(db.scalar(select(Dataset)).records, final.params)['summary']


def test_no_approvable_conclusion_until_all_segments_done(seeded):
    db = seeded
    j = _new(db, '半成品')
    claim = jobsvc.claim_next(db)
    db.commit()
    assert execute_segment(claim)
    db.expire_all()
    partial = db.get(Job, j.id)
    assert partial.status in ('queued', 'running')
    assert partial.result == {}
    assert partial.approved is False
    # 半成品即使被强行塞入分发计划也不会产出文件
    report = Report(name='半成品简报', job_id=j.id, destination='组', interval_seconds=60, next_run=0, enabled=True)
    db.add(report)
    db.commit()
    assert deliver(db, report) is None
    tick()
    assert db.scalar(select(func.count()).select_from(Delivery)) == 0


def test_completed_requires_all_segments_and_matches_batch(seeded):
    db = seeded
    j = _new(db, '最终一致')
    expected = business.analyze(db.scalar(select(Dataset)).records, j.params)
    run_once(max_segments=1000)
    db.expire_all()
    j = db.get(Job, j.id)
    assert j.status == 'completed'
    import json
    assert json.dumps(j.result, sort_keys=True, ensure_ascii=False) == json.dumps(expected, sort_keys=True, ensure_ascii=False)


def test_pause_and_claim_do_not_interleave_into_running(seeded):
    """暂停与领取并发时，任务不会停留在 running 且继续被推进。"""
    import threading
    db = seeded
    j = _new(db, '暂停并发')
    outcomes = {}
    for _ in range(10):
        from src.config.database import Session
        def claimer():
            with Session() as s:
                c = jobsvc.claim_next(s)
                s.commit()
                outcomes['claim'] = c is not None
        def pauser():
            with Session() as s:
                try:
                    jobsvc.pause(s, s.get(Job, j.id))
                    s.commit()
                    outcomes['pause'] = 'ok'
                except Exception as exc:
                    outcomes['pause'] = type(exc).__name__
        t1 = threading.Thread(target=claimer)
        t2 = threading.Thread(target=pauser)
        t1.start(); t2.start(); t1.join(); t2.join()
        db.expire_all()
        current = db.get(Job, j.id)
        if current.status == 'paused':
            break
    db.expire_all()
    current = db.get(Job, j.id)
    assert current.status in ('paused', 'completed')
    if current.status == 'paused':
        assert current.result == {}
