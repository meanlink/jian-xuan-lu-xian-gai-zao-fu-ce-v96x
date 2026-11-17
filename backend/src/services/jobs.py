"""分析任务的创建、分段领取与进度汇总。

分段执行的设计要点：

* 每段单独持久化，按 (job_id, segment_index) 唯一；重试覆盖同一行，
  不会重复累计人数。
* 领取和写回都用条件更新：写回要求该段仍是 running 且 claim_token
  匹配；暂停会把在跑的段退回 pending 并清空令牌，因此暂停成功后
  的迟到写回不会推进进度。
* 领取在负责人之间轮流进行：同一负责人有多项待办时，按该负责人
  最近一次领取时间排序，不会挤掉其他人的执行机会。
* 暂停保留已完成段，恢复从第一个未完成段继续；worker 重启时把
  running 段退回 pending，已完成段与合并进度不受影响。
"""
import time
import uuid

from fastapi import HTTPException
from redis import Redis
from sqlalchemy import select, func, update

from src.config.settings import REDIS_URL
from src.models import Dataset, Job, JobSegment
from src.services import business


def create(db, data, user):
    if not db.get(Dataset, data.dataset_id):
        raise HTTPException(404, '数据批次不存在')
    allowed = {p['key']: p for p in business.metadata().get('parameters', [])}
    if set(data.params) - set(allowed):
        raise HTTPException(422, '包含未知分析参数')
    params = {k: p['default'] for k, p in allowed.items()}
    params.update(data.params)
    for key, p in allowed.items():
        v = params[key]
        if p['type'] == 'number':
            try:
                v = float(v)
                if not (p.get('min', -1e9) <= v <= p.get('max', 1e9)):
                    raise ValueError()
                params[key] = v
            except (ValueError, TypeError):
                raise HTTPException(422, f'{p["label"]} 超出范围或非数值')
        elif str(v) not in [str(o['value']) for o in p.get('options', [])]:
            raise HTTPException(422, f'{p["label"]} 不是有效选项')
    # 先校验并切段，失败时不留下不可推进的半成品任务行。
    try:
        _, _, clean = business.prepare(db.get(Dataset, data.dataset_id).records, params)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    j = Job(name=data.name, dataset_id=data.dataset_id, params=params, created_by=user.id, created_at=time.time(), status='queued')
    db.add(j)
    db.flush()
    build_segments(db, j, clean)
    return j


def build_segments(db, job, clean):
    """按输入快照切段并落库；已有段保持不变，便于恢复原进度。"""
    pieces = business.chunks(clean)
    existing = {s.segment_index for s in db.scalars(select(JobSegment).where(JobSegment.job_id == job.id))}
    for index, rows in enumerate(pieces):
        if index in existing:
            continue
        db.add(JobSegment(job_id=job.id, segment_index=index, status='pending', input=rows))
    db.flush()
    return len(pieces)


def segment_counts(db, job_id):
    rows = db.execute(select(JobSegment.status, func.count()).where(JobSegment.job_id == job_id).group_by(JobSegment.status)).all()
    counts = {status: count for status, count in rows}
    total = sum(counts.values())
    return {'total': total, 'completed': counts.get('completed', 0), 'running': counts.get('running', 0), 'pending': counts.get('pending', 0)}


def segment_counts_many(db, job_ids):
    """一次分组统计多个任务的分段状态，供列表页避免逐条查询。"""
    result = {job_id: {'completed': 0, 'running': 0, 'pending': 0} for job_id in job_ids}
    if not job_ids:
        return {}
    rows = db.execute(select(JobSegment.job_id, JobSegment.status, func.count()).where(JobSegment.job_id.in_(job_ids)).group_by(JobSegment.job_id, JobSegment.status)).all()
    for job_id, status, count in rows:
        result[job_id][status] = count
    return result


def progress(db, job, counts=None):
    counts = dict(counts) if counts is not None else segment_counts(db, job.id)
    counts.setdefault('completed', 0)
    counts.setdefault('running', 0)
    counts.setdefault('pending', 0)
    counts['total'] = counts['completed'] + counts['running'] + counts['pending']
    if counts['total'] == 0 and job.status == 'completed' and job.result:
        # 旧版本已完成的任务没有分段记录，按整批结果展示为单段完成。
        return {'total': 1, 'completed': 1, 'running': 0, 'pending': 0, 'percent': 100.0, 'paused': False, 'legacy': True}
    counts['percent'] = round(counts['completed'] * 100 / counts['total'], 1) if counts['total'] else 0.0
    counts['paused'] = job.status == 'paused'
    return counts


def pause(db, job):
    """暂停：状态切到 paused，在跑的段退回 pending 并清空领取令牌。
    已完成段保留；恢复后从未完成段继续。

    状态切换用条件更新。若 worker 恰好在同一瞬间完成最后一段，
    任务已是 completed，暂停请求应失败而不是回退一个已完成结论。
    """
    if job.status == 'completed':
        raise HTTPException(409, '已完成的分析无需暂停')
    if job.status == 'failed':
        raise HTTPException(409, '失败的分析请重新发起')
    if job.status == 'paused':
        return job
    changed = db.execute(update(Job).where(Job.id == job.id, Job.status.in_(('queued', 'running'))).values(
        status='paused', paused_at=time.time(), pause_epoch=Job.pause_epoch + 1), execution_options={'synchronize_session': False}).rowcount
    if changed != 1:
        db.expire_all()
        current = db.get(Job, job.id)
        if current and current.status == 'completed':
            raise HTTPException(409, '任务刚刚完成，无需暂停')
        return current or job
    db.execute(update(JobSegment).where(JobSegment.job_id == job.id, JobSegment.status == 'running').values(status='pending', claim_token='', claimed_at=None), execution_options={'synchronize_session': False})
    db.expire_all()
    return db.get(Job, job.id)


def resume(db, job):
    """恢复：仅把暂停任务放回队列，已完成段保留，worker 从未完成段继续。"""
    if job.status == 'completed':
        raise HTTPException(409, '已完成的分析无需恢复')
    if job.status == 'failed':
        raise HTTPException(409, '失败的分析请重新发起')
    if job.status != 'paused':
        return job, False
    changed = db.execute(update(Job).where(Job.id == job.id, Job.status == 'paused').values(
        status='queued', paused_at=None), execution_options={'synchronize_session': False}).rowcount
    db.expire_all()
    return db.get(Job, job.id), changed == 1


def enqueue(job_id):
    try:
        r = Redis.from_url(REDIS_URL, socket_timeout=1)
        r.rpush('analysis:queue', job_id)
        r.close()
    except Exception:
        pass  # SQL queued status is the durable fallback


def queue_position(db, job):
    """排在当前负责人之前的其他负责人批次数；用于说明等待原因。"""
    ahead = db.scalar(select(func.count(func.distinct(Job.created_by))).where(
        Job.status == 'queued', Job.created_by != job.created_by, Job.id != job.id,
        Job.created_at < job.created_at))
    return int(ahead or 0)


def waiting_reason(db, job):
    if job.status == 'paused':
        return '已暂停；已完成段保留，恢复后从未完成段继续。'
    if job.status == 'queued':
        position = queue_position(db, job)
        if position:
            return f'等待中；前面还有 {position} 位负责人的批次，按负责人轮流执行。'
        return '等待中；worker 即将领取下一段。'
    if job.status == 'running':
        return '正在分段计算。'
    return ''


def job_dict(db, job, counts=None):
    data = {k: getattr(job, k) for k in ['id', 'name', 'dataset_id', 'params', 'status', 'result', 'error', 'approved', 'created_at', 'finished_at', 'started_at', 'paused_at']}
    data['progress'] = progress(db, job, counts)
    data['waiting_reason'] = waiting_reason(db, job)
    return data


def job_dicts(db, job_list):
    counts = segment_counts_many(db, [j.id for j in job_list])
    return [job_dict(db, j, counts.get(j.id)) for j in job_list]


def _owner_last_claim(db, owner):
    return db.scalar(select(func.max(JobSegment.claimed_at)).join(Job, Job.id == JobSegment.job_id).where(Job.created_by == owner)) or 0.0


def claim_next(db):
    """按负责人轮流领取下一段。

    每个负责人取最早的可推进任务的一段；负责人按最近一次领取时间
    排序（从未领取的排最前），因此持续接单时各负责人轮流推进，
    同一负责人的多项任务只在轮到自己时依次处理。
    """
    candidates = db.scalars(select(Job).where(Job.status.in_(('queued', 'running'))).order_by(Job.created_at, Job.id)).all()
    if not candidates:
        return None
    by_owner = {}
    for job in candidates:
        by_owner.setdefault(job.created_by, []).append(job)
    ready = []
    for owner, jobs in by_owner.items():
        for job in jobs:
            seg = db.scalar(select(JobSegment).where(JobSegment.job_id == job.id, JobSegment.status == 'pending').order_by(JobSegment.segment_index).limit(1))
            if seg is not None:
                ready.append((_owner_last_claim(db, owner), job.created_at, job.id, job, seg))
                break
            # 没有未完成段：可能刚写完最后一段但尚未合成（例如暂停后恢复）。
            finalize_if_done(db, job)
    if not ready:
        return None
    ready.sort(key=lambda item: (item[0], item[1], item[2]))
    # 多 worker 竞争同一段时由条件更新裁决；抢输的重试下一顺位，而不是空转。
    for _, _, _, job, seg in ready:
        token = uuid.uuid4().hex
        now = time.time()
        claimed = db.execute(update(JobSegment).where(JobSegment.id == seg.id, JobSegment.status == 'pending').values(
            status='running', attempt=JobSegment.attempt + 1, claim_token=token, claimed_at=now), execution_options={'synchronize_session': False}).rowcount
        if claimed != 1:
            continue
        # 与暂停并发时，只有任务仍可推进才保留领取；否则释放该段让暂停生效。
        advanced = db.execute(update(Job).where(Job.id == job.id, Job.status.in_(('queued', 'running'))).values(
            status='running', started_at=func.coalesce(Job.started_at, now), error=''), execution_options={'synchronize_session': False}).rowcount
        if advanced != 1:
            db.execute(update(JobSegment).where(JobSegment.id == seg.id, JobSegment.claim_token == token).values(
                status='pending', claim_token='', claimed_at=None), execution_options={'synchronize_session': False})
            db.expire_all()
            continue
        db.expire_all()
        epoch = db.scalar(select(Job.pause_epoch).where(Job.id == job.id)) or 0
        attempt = db.scalar(select(JobSegment.attempt).where(JobSegment.id == seg.id)) or 0
        return {'job_id': job.id, 'segment_id': seg.id, 'token': token, 'epoch': epoch, 'attempt': attempt}
    return None


def complete_segment(db, claim, summary):
    """写回一段。状态或令牌不匹配（暂停后迟到、重复重试）直接丢弃。"""
    saved = db.execute(update(JobSegment).where(
        JobSegment.id == claim['segment_id'], JobSegment.status == 'running', JobSegment.claim_token == claim['token']).values(
        summary=summary, status='completed', claim_token='', claimed_at=time.time()), execution_options={'synchronize_session': False}).rowcount
    if saved != 1:
        return 'stale'
    db.expire_all()  # 批量更新不刷新会话内对象，避免用旧状态合成结论
    job = db.get(Job, claim['job_id'])
    db.flush()
    if job is None:
        return 'stale'
    finalize_if_done(db, job)
    return 'saved'


MAX_SEGMENT_ATTEMPTS = 3


def fail_segment(db, claim, error):
    """一段计算失败：未达重试上限则退回 pending，否则整批失败。
    失败的任务不再被领取，避免永久失败的段拖住 worker。"""
    seg = db.get(JobSegment, claim['segment_id'])
    job = db.get(Job, claim['job_id'])
    if not seg or not job:
        return 'stale'
    if not (seg.status == 'running' and seg.claim_token == claim['token']):
        return 'stale'
    if (seg.attempt or 0) >= MAX_SEGMENT_ATTEMPTS:
        seg.status = 'pending'
        seg.claim_token = ''
        seg.claimed_at = None
        db.execute(update(Job).where(Job.id == job.id, Job.status.in_(('queued', 'running'))).values(
            status='failed', error=('分段计算失败：' + str(error))[:990], finished_at=time.time()), execution_options={'synchronize_session': False})
        db.expire_all()
        return 'failed'
    seg.status = 'pending'
    seg.claim_token = ''
    seg.claimed_at = None
    db.flush()
    return 'pending'


def finalize_if_done(db, job):
    """全部段完成才合成并发布可批准结论；合成失败则整批失败。

    发布用条件更新（仅 queued/running），与暂停并发时由行锁裁决；
    暂停胜出则半成品不会写入结果。
    """
    segments = db.scalars(select(JobSegment).where(JobSegment.job_id == job.id).order_by(JobSegment.segment_index)).all()
    if not segments or any(s.status != 'completed' for s in segments):
        return False
    if job.status in ('completed', 'paused', 'failed'):
        return False
    try:
        team, confidence = business.parse_params(job.params)
        result = business.build_result([business.with_rows(s.summary, s.input) for s in segments], team, confidence)
    except ValueError as exc:
        db.execute(update(Job).where(Job.id == job.id, Job.status.in_(('queued', 'running'))).values(
            status='failed', error=str(exc)[:990], finished_at=time.time()), execution_options={'synchronize_session': False})
        db.expire_all()
        return False
    published = db.execute(update(Job).where(Job.id == job.id, Job.status.in_(('queued', 'running'))).values(
        result=result, status='completed', finished_at=time.time()), execution_options={'synchronize_session': False}).rowcount
    db.expire_all()
    return published == 1


def recover(db):
    """进程重启：在跑的段退回 pending、任务退回 queued，已完成段保留。"""
    recovered = list(db.scalars(select(Job.id).where(Job.status == 'running')))
    db.execute(update(JobSegment).where(JobSegment.status == 'running').values(status='pending', claim_token='', claimed_at=None), execution_options={'synchronize_session': False})
    db.expire_all()
    for job in db.scalars(select(Job).where(Job.status == 'running')):
        job.status = 'queued'
    db.flush()
    return recovered
