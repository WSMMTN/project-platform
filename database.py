"""数据库模型与会话管理。

本地开发用 SQLite（默认），线上 Render 用 PostgreSQL，
由环境变量 DATABASE_URL 决定，模型代码完全一致。
"""
import os
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
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")

# Render 可能下发 postgres:// 前缀，SQLAlchemy 2.x 只认 postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine_kwargs = {}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


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
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
