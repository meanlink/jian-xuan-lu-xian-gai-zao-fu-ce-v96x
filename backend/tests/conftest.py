import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False).name
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _db)
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')
os.environ.setdefault('ARTIFACT_DIR', tempfile.mkdtemp())

import pytest
from src.config.database import Session, engine, Base
from src import models  # noqa
_ = models
from src.models import User, Dataset
from src.services.business import seed_records
from src.utils.security import hash_password
from src.utils.serialization import checksum
import time


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as s:
        yield s


@pytest.fixture()
def seeded(db):
    for role in ('admin', 'analyst', 'viewer'):
        user = User(email=role + '@demo.local', name=role, role=role, password=hash_password('Demo123!'))
        db.add(user)
    db.flush()
    rows = seed_records()
    d = Dataset(name='测试批次', source='t.csv', records=rows, checksum=checksum(rows), created_at=time.time())
    db.add(d)
    db.commit()
    return db
