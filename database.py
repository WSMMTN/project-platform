"""数据库模型与会话管理。

- 本地开发：SQLite（默认 ./app.db）
- SCF 云函数：设 DB_PATH=/tmp/app.db，且每次提交后把 SQLite 快照上传到 COS，
  冷启动时若无本地库则从 COS 恢复（云函数无持久盘）
- 传统服务器/Render：设 DATABASE_URL 用 PostgreSQL，模型代码完全一致
"""
import os
import threading
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from s3_client import download_object, s3_configured, upload_bytes

DB_PATH = os.getenv("DB_PATH", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    DATABASE_URL = f"sqlite:///{DB_PATH}" if DB_PATH else "sqlite:///./app.db"

# Render 可能下发 postgres:// 前缀，SQLAlchemy 2.x 只认 postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine_kwargs = {}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ---------- SQLite 快照（SCF 无持久盘场景） ----------
# 仅当显式设置 DB_PATH（云函数等临时盘环境）时才启用，
# 本地开发默认 ./app.db 永不触发快照，避免覆盖线上快照。

DB_SNAPSHOT_KEY = "db/app.sqlite3"
_backup_lock = threading.Lock()


def _sqlite_file() -> str:
    if DATABASE_URL.startswith("sqlite:///"):
        return DATABASE_URL[len("sqlite:///"):]
    return ""


def _snapshot_enabled() -> bool:
    return bool(DB_PATH) and s3_configured()


def _restore_from_snapshot() -> None:
    """冷启动时若本地库不存在且 COS 有快照，则恢复。"""
    path = _sqlite_file()
    if not path or not _snapshot_enabled():
        return
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    try:
        data = download_object(DB_SNAPSHOT_KEY)
    except Exception as e:  # COS 异常不阻塞启动，退化为空库
        print(f"[db] 读取 COS 快照失败: {e}")
        return
    if data:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        print(f"[db] 已从 COS 快照恢复数据库（{len(data)} 字节）")


def _backup_snapshot() -> None:
    path = _sqlite_file()
    if not path or not _snapshot_enabled() or not os.path.exists(path):
        return
    with _backup_lock:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            if not upload_bytes(DB_SNAPSHOT_KEY, data, "application/x-sqlite3"):
                print("[db] 数据库快照上传失败")
        except Exception as e:
            print(f"[db] 数据库快照上传失败: {e}")


def _after_commit(session) -> None:  # noqa: ARG001
    if _snapshot_enabled():
        _backup_snapshot()


event.listen(SessionLocal.class_, "after_commit", _after_commit)


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    hashed_password = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=utcnow)

    projects = relationship("Project", back_populates="owner", cascade="all, delete-orphan")


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, default="")
    tech_stack = Column(String(500), default="")
    share_uuid = Column(String(64), unique=True, index=True, nullable=False)
    share_enabled = Column(Boolean, default=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=utcnow)

    owner = relationship("User", back_populates="projects")
    files = relationship("ProjectFile", back_populates="project", cascade="all, delete-orphan")


class ProjectFile(Base):
    __tablename__ = "project_files"

    id = Column(Integer, primary_key=True, index=True)
    file_name = Column(String(255), nullable=False)
    file_type = Column(String(16), nullable=False)  # video / ppt / pdf
    # 只存 R2 Object Key，不存带签名参数的临时 URL（预签名最长 7 天，存库会失效）
    r2_key = Column(Text, nullable=False)
    size = Column(Integer, default=0)
    content_type = Column(String(128), default="application/octet-stream")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    uploaded_at = Column(DateTime, default=utcnow)

    project = relationship("Project", back_populates="files")


def create_db():
    _restore_from_snapshot()  # SCF 场景：先从 COS 恢复再建表
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
