from sqlalchemy import inspect, text
from src.config.database import Base, engine
from src import models

# 已有部署的任务表需要补的列；create_all 不会修改旧表。
JOB_COLUMNS = {
    'started_at': 'DOUBLE NULL',
    'paused_at': 'DOUBLE NULL',
    'pause_epoch': 'INTEGER NOT NULL DEFAULT 0',
}


def migrate():
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    if 'jobs' not in inspector.get_table_names():
        return
    present = {c['name'] for c in inspector.get_columns('jobs')}
    with engine.begin() as conn:
        for name, ddl in JOB_COLUMNS.items():
            if name not in present:
                conn.execute(text(f'ALTER TABLE jobs ADD COLUMN {name} {ddl}'))
if __name__=='__main__':migrate()
