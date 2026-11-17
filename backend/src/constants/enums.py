from enum import Enum
class Role(str, Enum):
    admin = 'admin'
    analyst = 'analyst'
    viewer = 'viewer'
class JobStatus(str, Enum):
    queued = 'queued'
    running = 'running'
    paused = 'paused'
    completed = 'completed'
    failed = 'failed'
class SegmentStatus(str, Enum):
    pending = 'pending'
    running = 'running'
    completed = 'completed'
