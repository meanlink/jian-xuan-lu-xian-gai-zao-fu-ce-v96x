from sqlalchemy import String, JSON, Double, ForeignKey, Boolean, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
class Job(Base):
    __tablename__ = 'jobs'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default='queued')
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(String(1000), default='')
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    started_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    paused_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    # 每次暂停递增；worker 完成一段时须匹配该值，避免暂停后的迟到写回推进进度。
    pause_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class JobSegment(Base):
    """一次分析的一段。分段结果按 (job_id, segment_index) 唯一保存，
    重试覆盖同一行而不是新增行，因此并发重试不会重复累计人数。"""
    __tablename__ = 'job_segments'
    __table_args__ = (UniqueConstraint('job_id', 'segment_index', name='uq_job_segment'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'), index=True)
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claim_token: Mapped[str] = mapped_column(String(64), default='')
    claimed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    input: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
