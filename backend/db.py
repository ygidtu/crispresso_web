"""SQLite 持久层（peewee）：统一存储服务中各种任务/日志的元数据。

服务原本把任务与 OSS 下载任务都放在内存里，重启后全部清空（OSS 任务尤其明显——
重启后旧 oss_ 任务 ID 直接 404）。本模块用 peewee 把这类「任务表」落盘到单一
SQLite 文件，服务重启后可按 ID 继续查询历史任务的最后状态；下载日志仍在各自目录的
download.log / run.log 落盘，此处只存元数据与路径，避免大日志塞进数据库。
"""
import os
import time
from pathlib import Path

from peewee import (BooleanField, CharField, DateTimeField, Model,
                    SqliteDatabase, TextField)

BASE = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("CRWeb_DB_PATH", str(BASE / "crpresso.db"))

database = SqliteDatabase(DB_PATH, pragmas={
    "journal_mode": "wal",       # 读写并发更友好，避免 WAL 下 busy error
    "foreign_keys": 1,
    "busy_timeout": 5000,
})


class BaseModel(Model):
    class Meta:
        database = database


class OssTaskModel(BaseModel):
    """OSS 下载任务持久记录（重启后仍可查）。"""
    id = CharField(primary_key=True)
    oss_path = TextField()
    target = TextField()
    endpoint = TextField(default="")
    status = CharField(default="running")        # running/done/failed
    finished = BooleanField(default=False)
    error = TextField(null=True, default=None)
    log_path = TextField(default="")             # 实际日志文件路径（仍落在 oss_tasks/）
    owner = TextField(default="")                # 等级负责人（必填登记）
    description = TextField(default="")          # 数据情况说明（必填登记）
    created_at = DateTimeField(default=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    updated_at = DateTimeField(default=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    class Meta:
        table_name = "oss_tasks"


_MODELS = [OssTaskModel]


def init_db():
    """创建数据文件与所有表（幂等），并为已有表补缺失列（轻量迁移）。"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    if not database.is_connection_usable():
        database.connect()
    try:
        database.create_tables(_MODELS, safe=True)
        # 已存在的旧表缺新列时逐个补齐（SQLite ALTER ADD COLUMN）
        for model in _MODELS:
            table = model._meta.table_name
            existing = {row[1] for row in
                        database.execute_sql(f"PRAGMA table_info({table})").fetchall()}
            cols = model._meta.sorted_fields
            for f in cols:
                if f.name not in existing:
                    kind = "TEXT" if isinstance(f, (TextField, CharField)) else "NUMERIC"
                    default = "''" if isinstance(f, (TextField, CharField)) else "0"
                    database.execute_sql(
                        f"ALTER TABLE {table} ADD COLUMN {f.name} {kind} DEFAULT {default}")
    finally:
        database.close()


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")
