from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.models import Job
from src.schemas.job import JobInput
from src.middlewares.auth import current_user, can_write, admin_only
from src.services import jobs
from src.services.jobs import job_dict, job_dicts
from src.services.audit import record
router=APIRouter(prefix='/jobs')
@router.get('')
def listing(db=Depends(get_db),u=Depends(current_user)):
    return job_dicts(db,list(db.scalars(select(Job).order_by(Job.id.desc()).limit(100))))
@router.post('',status_code=201)
def create(data:JobInput,db=Depends(get_db),u=Depends(can_write)):
    j=jobs.create(db,data,u); record(db,u.email,'job.create',{'id':j.id,'params':j.params})
    db.commit(); jobs.enqueue(j.id); return job_dict(db,j)
@router.get('/{id}')
def detail(id:int,db=Depends(get_db),u=Depends(current_user)):
    j=db.get(Job,id)
    if not j: raise HTTPException(404,'分析任务不存在')
    return job_dict(db,j)
@router.post('/{id}/pause')
def pause(id:int,db=Depends(get_db),u=Depends(can_write)):
    j=db.get(Job,id)
    if not j: raise HTTPException(404,'分析任务不存在')
    if j.status not in ('queued','running','paused'): raise HTTPException(409,'当前状态无法暂停')
    j=jobs.pause(db,j); record(db,u.email,'job.pause',{'id':id}); db.commit()
    return job_dict(db,j)
@router.post('/{id}/resume')
def resume(id:int,db=Depends(get_db),u=Depends(can_write)):
    j=db.get(Job,id)
    if not j: raise HTTPException(404,'分析任务不存在')
    if j.status not in ('paused','queued','running'): raise HTTPException(409,'当前状态无法恢复')
    j,changed=jobs.resume(db,j)
    if changed: record(db,u.email,'job.resume',{'id':id}); db.commit(); jobs.enqueue(j.id)
    return job_dict(db,j)
@router.post('/{id}/approve')
def approve(id:int,db=Depends(get_db),u=Depends(admin_only)):
    j=db.get(Job,id)
    if not j: raise HTTPException(404,'分析任务不存在')
    if j.status!='completed':raise HTTPException(409,'仅已完成结果可以批准')
    j.approved=True; record(db,u.email,'job.approve',{'id':id}); db.commit()
    return job_dict(db,j)
