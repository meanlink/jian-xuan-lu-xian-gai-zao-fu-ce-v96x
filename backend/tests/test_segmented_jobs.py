import json
import time

import pytest

from sqlalchemy import select, func
from fastapi.testclient import TestClient

from src.models import User, Dataset, Job, JobSegment, Report
from src.services import business, jobs as jobsvc
from src.worker import run_once, execute_segment, tick


def _client():
    import src.main as main
    return TestClient(main.app)


def _login(client, role):
    r = client.post('/auth/login', json={'email': role + '@demo.local', 'password': 'Demo123!'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['access_token']}


def _dataset(db):
    return db.scalar(select(Dataset))


def _user(db, role):
    return db.scalar(select(User).where(User.role == role))


def _new_job(db, name, owner_role='analyst', params=None):
    from src.schemas.job import JobInput
    j = jobsvc.create(db, JobInput(name=name, dataset_id=_dataset(db).id, params=params or {}), _user(db, owner_role))
    db.commit()
    return j


def test_segments_built_and_not_completed_until_all_done(seeded):
    db = seeded
    j = _new_job(db, '分段任务')
    segs = db.scalars(select(JobSegment).where(JobSegment.job_id == j.id).order_by(JobSegment.segment_index)).all()
    assert len(segs) == len(business.chunks(business.prepare(_dataset(db).records, j.params)[2]))
    assert all(s.status == 'pending' for s in segs)
    assert j.status == 'queued'

    claim = jobsvc.claim_next(db)
    db.commit()
    assert claim['job_id'] == j.id
    execute_segment(claim)
    db.expire_all()
    j = db.get(Job, j.id)
    assert j.status in ('running', 'queued')
    assert j.result == {}  # 半成品不发布结论
    completed = db.scalars(select(JobSegment).where(JobSegment.job_id == j.id, JobSegment.status == 'completed')).all()
    assert len(completed) == 1


def test_run_to_completion_matches_whole_batch(seeded):
    db = seeded
    j = _new_job(db, '完整任务')
    expected = business.analyze(_dataset(db).records, j.params)
    run_once(max_segments=1000)
    db.expire_all()
    j = db.get(Job, j.id)
    assert j.status == 'completed', j.error
    assert json.dumps(j.result, sort_keys=True, ensure_ascii=False) == json.dumps(expected, sort_keys=True, ensure_ascii=False)
    assert j.approved is False


def test_pause_keeps_completed_segments_and_resume_continues(seeded):
    db = seeded
    j = _new_job(db, '暂停任务')
    # 先推进两段
    for _ in range(2):
        claim = jobsvc.claim_next(db)
        db.commit()
        assert execute_segment(claim)
    db.expire_all()
    j = db.get(Job, j.id)
    done_before = {s.id for s in db.scalars(select(JobSegment).where(JobSegment.job_id == j.id, JobSegment.status == 'completed'))}
    assert len(done_before) == 2

    # 领取第三段后暂停（模拟跑到一半）
    claim = jobsvc.claim_next(db)
    db.commit()
    jobsvc.pause(db, db.get(Job, j.id))
    db.commit()
    db.expire_all()
    j = db.get(Job, j.id)
    assert j.status == 'paused'
    assert db.get(JobSegment, claim['segment_id']).status == 'pending'
    still = {s.id for s in db.scalars(select(JobSegment).where(JobSegment.job_id == j.id, JobSegment.status == 'completed'))}
    assert still == done_before

    # 暂停后的迟到写回被丢弃
    assert jobsvc.complete_segment(db, claim, business.summarize([])) == 'stale'
    db.commit()
    db.expire_all()
    assert {s.id for s in db.scalars(select(JobSegment).where(JobSegment.job_id == j.id, JobSegment.status == 'completed'))} == done_before

    # 暂停时 worker 不领取该任务
    assert jobsvc.claim_next(db) is None

    jobsvc.resume(db, db.get(Job, j.id))
    db.commit()
    run_once(max_segments=1000)
    db.expire_all()
    j = db.get(Job, j.id)
    assert j.status == 'completed'
    assert j.result['summary'] == business.analyze(_dataset(db).records, j.params)['summary']


def test_retry_same_segment_does_not_double_count(seeded):
    db = seeded
    j = _new_job(db, '重试任务')
    claim = jobsvc.claim_next(db)
    db.commit()
    seg = db.get(JobSegment, claim['segment_id'])
    summary = business.summarize(seg.input)
    assert jobsvc.complete_segment(db, claim, summary) == 'saved'
    # 同一 claim 重复写回不生效
    assert jobsvc.complete_segment(db, claim, summary) == 'stale'
    # 重新领取同一段（例如崩溃恢复）再写回，段数不变
    db.commit()
    total = db.scalar(select(func.count()).select_from(JobSegment).where(JobSegment.job_id == j.id))
    run_once(max_segments=1000)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(JobSegment).where(JobSegment.job_id == j.id)) == total
    j = db.get(Job, j.id)
    assert j.status == 'completed'
    assert j.result['table']['rows'].__len__() == len(business.prepare(_dataset(db).records, j.params)[2])


def test_recovery_requeues_running_but_keeps_completed(seeded):
    db = seeded
    j = _new_job(db, '恢复任务')
    claim = jobsvc.claim_next(db)
    db.commit()
    assert execute_segment(claim)
    claim2 = jobsvc.claim_next(db)
    db.commit()
    recovered = jobsvc.recover(db)
    db.commit()
    assert j.id in recovered
    db.expire_all()
    j = db.get(Job, j.id)
    assert j.status == 'queued'
    assert db.get(JobSegment, claim2['segment_id']).status == 'pending'
    run_once(max_segments=1000)
    db.expire_all()
    assert db.get(Job, j.id).status == 'completed'


def test_fair_rotation_between_owners(seeded):
    db = seeded
    analyst_jobs = [_new_job(db, f'分析员任务{i}', 'analyst') for i in range(3)]
    admin_jobs = [_new_job(db, f'负责人任务{i}', 'admin') for i in range(2)]
    order = []
    for _ in range(1000):
        claim = jobsvc.claim_next(db)
        db.commit()
        if not claim:
            break
        job = db.get(Job, claim['job_id'])
        order.append(job.created_by)
        # 模拟计算耗时，确保领取时间戳递增
        time.sleep(0.003)
        assert execute_segment(claim)
    db.expire_all()
    # 两个负责人都应在自己第二项任务开始前先各推进一项
    first_analyst = order.index(_user(db, 'analyst').id)
    first_admin = order.index(_user(db, 'admin').id)
    assert abs(first_analyst - first_admin) == 1
    # 前十次领取中两方都不少于 4 次，说明没有一方挤掉另一方
    head = order[:10]
    assert head.count(_user(db, 'analyst').id) >= 4
    assert head.count(_user(db, 'admin').id) >= 4
    for j in analyst_jobs + admin_jobs:
        assert db.get(Job, j.id).status == 'completed'


def test_waiting_reason_and_progress(seeded):
    db = seeded
    j = _new_job(db, '等待任务')
    info = jobsvc.job_dict(db, db.get(Job, j.id))
    assert info['progress']['total'] > 0
    assert '等待' in info['waiting_reason']
    jobsvc.pause(db, db.get(Job, j.id))
    db.commit()
    info = jobsvc.job_dict(db, db.get(Job, j.id))
    assert info['progress']['paused'] is True
    assert '暂停' in info['waiting_reason']


def test_report_not_delivered_until_completed_and_approved(seeded):
    db = seeded
    j = _new_job(db, '分发门禁')
    r = Report(name='半成品', job_id=j.id, destination='组', interval_seconds=60, next_run=0, enabled=True)
    db.add(r)
    db.commit()
    assert tick  # ensure import
    from src.services.reports import deliver
    assert deliver(db, r) is None
    run_once(max_segments=1000)
    db.expire_all()
    j = db.get(Job, j.id)
    assert j.status == 'completed'
    assert deliver(db, r) is None  # 未批准仍不分发
    j.approved = True
    db.commit()
    assert deliver(db, r) is not None


def test_api_pause_resume_and_old_results_visible(seeded):
    client = _client()
    headers = _login(client, 'analyst')
    admin = _login(client, 'admin')
    dataset_id = _dataset(seeded).id
    created = client.post('/jobs', headers=headers, json={'name': 'API 分段', 'dataset_id': dataset_id, 'params': {'team': '全部', 'confidence': '0.95'}})
    assert created.status_code == 201, created.text
    job = created.json()
    assert job['progress']['total'] > 0
    paused = client.post(f"/jobs/{job['id']}/pause", headers=headers)
    assert paused.status_code == 200
    assert paused.json()['status'] == 'paused'
    resumed = client.post(f"/jobs/{job['id']}/resume", headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()['status'] == 'queued'
    # 观察员不能暂停
    viewer = _login(client, 'viewer')
    assert client.post(f"/jobs/{job['id']}/pause", headers=viewer).status_code == 403
    # 未完成不能批准
    assert client.post(f"/jobs/{job['id']}/approve", headers=admin).status_code == 409


def test_invalid_input_does_not_leave_job_row(seeded):
    from src.schemas.job import JobInput
    from fastapi import HTTPException
    from sqlalchemy import func
    db = seeded
    before = db.scalar(select(func.count()).select_from(Job))
    with pytest.raises(HTTPException) as info:
        jobsvc.create(db, JobInput(name='非法参数', dataset_id=_dataset(db).id, params={'team': '九组'}), _user(db, 'analyst'))
    assert info.value.status_code == 422
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Job)) == before


def test_segment_retry_after_failure_then_caps_out(seeded):
    db = seeded
    j = _new_job(db, '失败重试')
    for attempt in range(1, jobsvc.MAX_SEGMENT_ATTEMPTS + 1):
        claim = jobsvc.claim_next(db)
        db.commit()
        outcome = jobsvc.fail_segment(db, claim, '模拟失败')
        db.commit()
        if attempt < jobsvc.MAX_SEGMENT_ATTEMPTS:
            assert outcome == 'pending'
            db.expire_all()
            assert db.get(Job, j.id).status in ('queued', 'running')
        else:
            assert outcome == 'failed'
    db.expire_all()
    failed = db.get(Job, j.id)
    assert failed.status == 'failed'
    assert '模拟失败' in failed.error
    # 失败的任务不再被领取
    claim = jobsvc.claim_next(db)
    assert claim is None or db.get(Job, claim['job_id']).id != j.id
    assert db.get(JobSegment, db.scalar(select(JobSegment.id).where(JobSegment.job_id == j.id, JobSegment.segment_index == 0))).status == 'pending'
