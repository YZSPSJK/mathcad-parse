from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, ForeignKey, LargeBinary, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from backend.config import settings


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    name: Mapped[str]
    source_path: Mapped[str]
    sha256: Mapped[str] = mapped_column(index=True)
    size: Mapped[int]
    uploaded_at: Mapped[str] = mapped_column(default=now)
    app_info: Mapped[dict] = mapped_column(JSON, default=dict)
    parse_status: Mapped[str] = mapped_column(default="parsed")
    diagnostics: Mapped[list] = mapped_column(JSON, default=list)
    draft_version: Mapped[int] = mapped_column(default=0)
    latest_revision: Mapped[int | None]
    revision_counter: Mapped[int] = mapped_column(default=0)
    active_job: Mapped[str | None]


class SourcePart(Base):
    __tablename__ = "source_parts"
    __table_args__ = (UniqueConstraint("document_id", "snapshot", "path"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    snapshot: Mapped[str]
    path: Mapped[str]
    content: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str]


class Region(Base):
    __tablename__ = "regions"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    region_id: Mapped[str]
    result_ref: Mapped[str | None]
    kind: Mapped[str]
    geometry: Mapped[dict] = mapped_column(JSON)
    expression_xml: Mapped[str]


class IOField(Base):
    __tablename__ = "io_fields"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    region_id: Mapped[str]
    result_ref: Mapped[str | None]
    direction: Mapped[str]
    alias: Mapped[str]
    display_name: Mapped[str]
    value_type: Mapped[str]
    units: Mapped[str]
    saved_value: Mapped[dict] = mapped_column(JSON)
    supported: Mapped[bool]
    reason: Mapped[str | None]
    expression_xml: Mapped[str]


class DraftInput(Base):
    __tablename__ = "draft_inputs"
    field_id: Mapped[str] = mapped_column(ForeignKey("io_fields.id"), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    value: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[str] = mapped_column(default=now)


class CalculationRun(Base):
    __tablename__ = "calculation_runs"
    __table_args__ = (UniqueConstraint("document_id", "revision"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    revision: Mapped[int]
    draft_version: Mapped[int]
    inputs: Mapped[dict] = mapped_column(JSON)
    outputs: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(default="queued")
    artifact_status: Mapped[str] = mapped_column(default="pending")
    error: Mapped[dict | None] = mapped_column(JSON)
    configuration: Mapped[dict] = mapped_column(JSON)
    engine: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(default=now)
    completed_at: Mapped[str | None]


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("calculation_runs.id"))
    kind: Mapped[str]
    status: Mapped[str] = mapped_column(default="queued", index=True)
    stage: Mapped[str] = mapped_column(default="queued")
    error: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    retry_of: Mapped[str | None]
    created_at: Mapped[str] = mapped_column(default=now)
    started_at: Mapped[str | None]
    completed_at: Mapped[str | None]


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("run_id", "kind"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("calculation_runs.id"))
    revision: Mapped[int]
    kind: Mapped[str]
    path: Mapped[str]
    sha256: Mapped[str]
    size: Mapped[int]
    cache_key: Mapped[str]
    created_at: Mapped[str] = mapped_column(default=now)


def make_engine():
    engine = create_engine(f"sqlite:///{settings().directory() / 'mathcad.sqlite3'}",
                           connect_args={"check_same_thread": False, "timeout": 15})

    @event.listens_for(engine, "connect")
    def pragmas(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA journal_mode=WAL")

    return engine


engine = make_engine()
Session = sessionmaker(engine, expire_on_commit=False)


def migrate():
    # 每次升级均在独立事务中登记版本；未知版本拒绝启动。
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        version = connection.exec_driver_sql("SELECT max(version) FROM schema_version").scalar() or 0
        if version > 1:
            raise RuntimeError("数据库版本高于程序版本，请先升级程序")
        if version < 1:
            Base.metadata.create_all(connection)
            connection.exec_driver_sql("INSERT INTO schema_version VALUES (1)")


if __name__ == "__main__":
    migrate()
    print("SQLite schema v1 已就绪")
