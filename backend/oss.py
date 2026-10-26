"""OSS 数据下载管理。

流程：前端将粘贴的 4 行交付信息做信封加密（随机 AES-256-GCM 加密正文 + RSA-OAEP 加密 AES 密钥）
提交到后端；后端 RSA 解出 AES 密钥后解密正文，解析出交付路径/密钥对，登记到 $HOME/.ossutilconfig
（权限 600，旧配置自动备份），随后在后台线程执行 ossutil cp -rf 下载数据并追踪任务状态。

安全：accessKeySecret 仅存在于服务端内存与 $HOME/.ossutilconfig，日志不落任何密钥。
"""
import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend import db as db_mod
from backend import keys
from backend import mailer

OSSUTIL_BIN = "ossutil"                      # 可由 config.ini [oss] ossutil_bin 覆盖
OSS_CONFIG = Path.home() / ".ossutilconfig"
TASKS_ROOT = Path(__file__).resolve().parent.parent / "oss_tasks"


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---- 信封解密：{ek, iv, data}（均为 base64url）----
def decrypt_payload(payload: dict) -> str:
    """RSA 解出 AES 密钥(JWK JSON) → AES-GCM 解密正文，返回 UTF-8 文本。"""
    aes_jwk = json.loads(keys.decrypt_token(payload["ek"]))
    key = _b64d(aes_jwk["k"])
    iv = _b64d(payload["iv"])
    data = _b64d(payload["data"])
    if not (16 <= len(key) <= 32):
        raise ValueError("AES 密钥长度非法")
    return AESGCM(key).decrypt(iv, data, None).decode("utf-8")


# ---- 解析粘贴的交付信息（中英文标签、全/半角冒号均兼容）----
def parse_oss_info(text: str) -> dict:
    fields = {"oss_path": None, "access_key_id": None, "access_key_secret": None, "endpoint": None}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        sep = "：" if "：" in line else ":"
        label, _, value = line.partition(sep)
        label, value = label.strip(), value.strip()
        if not value:
            continue
        low = label.lower()
        if value.startswith("oss://") or "交付路径" in label or "路径" in label:
            fields["oss_path"] = value
        elif "secret" in low:
            fields["access_key_secret"] = value
        elif "key" in low and "id" in low:
            fields["access_key_id"] = value
        elif "endpoint" in low or "server" in low:
            fields["endpoint"] = value
    missing = [k for k, v in fields.items() if not v]
    if missing:
        raise ValueError("缺少字段: " + ", ".join(missing))
    if not fields["oss_path"].startswith("oss://"):
        raise ValueError("交付路径须以 oss:// 开头")
    return fields


# ---- 登记 $HOME/.ossutilconfig（ossutil v2 格式；旧文件自动备份；权限 600）----
def region_from_endpoint(endpoint: str) -> str:
    """http://oss-cn-shanghai.aliyuncs.com -> cn-shanghai"""
    host = endpoint.split("//")[-1].strip("/")
    first = host.split(".")[0]          # oss-cn-shanghai
    return first[4:] if first.lower().startswith("oss-") else first


def write_ossutil_config(creds: dict) -> str:
    if OSS_CONFIG.is_file():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = OSS_CONFIG.parent / f"{OSS_CONFIG.name}.{stamp}.bak"
        shutil.copy2(OSS_CONFIG, backup)
        os.chmod(backup, 0o600)
    # ossutil v2 配置格式：[default] + region（v1 的 [Credentials]/server 已废弃）
    content = (
        "[default]\n"
        f"accessKeyID={creds['access_key_id']}\n"
        f"accessKeySecret={creds['access_key_secret']}\n"
        f"region={region_from_endpoint(creds['endpoint'])}\n"
    )
    fd = os.open(OSS_CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(OSS_CONFIG, 0o600)  # 已存在的文件不会自动改权限，显式收紧
    return str(OSS_CONFIG)


def _to_gib(text: str) -> float:
    """'5.103 GiB' / '949.604 KiB' / '26.777 GiB' → GiB 数值。"""
    m = re.match(r"([\d.]+)\s*(B|KiB|MiB|GiB|TiB)", text or "")
    if not m:
        return 0.0
    v, u = float(m.group(1)), m.group(2)
    return v * {"B": 1 / 1024 ** 3, "KiB": 1 / 1024 ** 2,
                "MiB": 1 / 1024, "GiB": 1.0, "TiB": 1024.0}[u]


def _ossutil_running(oss_source: str) -> bool:
    """检测是否已有 ossutil 进程正在下载该来源（避免重启后重复启动）。

    服务重启后原 ossutil 子进程会变孤儿继续运行，此时不应再起一个。
    以命令中的 oss:// 源路径匹配（唯一），而非目的目录（多任务可能共用 raw_root）。
    """
    needle = str(oss_source).rstrip("/").encode("utf-8", "replace")
    if not needle:
        return False
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / "cmdline").read_bytes()
        except OSError:
            continue
        if b"ossutil" in cmd and needle in cmd:
            return True
    return False


# ---- 后台下载任务 ----
class OssTask:
    def __init__(self, task_id: str, oss_path: str, target: str, ossutil_bin: str,
                 endpoint: str, owner: str = "", description: str = ""):
        self.id = task_id
        self.oss_path = oss_path
        self.target = target
        self.ossutil_bin = ossutil_bin
        self.endpoint = endpoint
        self.owner = owner                # 等级负责人（登记必填）
        self.description = description    # 数据情况说明（登记必填）
        self.dir = TASKS_ROOT / task_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "download.log"
        self.status = "running"
        self.error = None
        self.finished = False
        self.created_at = db_mod.now_str()
        self.thread = None
        self.is_resume = False            # 本次 run 是否为续传（仅运行时语义，不入库）
        self._lock = threading.Lock()

    @property
    def checkpoint_dir(self):
        return self.dir / "ossutil_checkpoint"

    @classmethod
    def from_model(cls, row, ossutil_bin: str):
        """从持久化记录重建内存任务对象（服务重启后用，不拉起下载线程）。"""
        task = cls(row.id, row.oss_path, row.target, ossutil_bin, row.endpoint,
                   owner=getattr(row, "owner", "") or "",
                   description=getattr(row, "description", "") or "")
        task.status = row.status
        task.finished = row.finished
        task.error = row.error
        task.created_at = row.created_at or task.created_at
        return task

    def _save(self):
        """写回 SQLite（upsert）：created_at 仅在插入时设置，后续更新不覆盖。"""
        payload = dict(id=self.id, oss_path=self.oss_path, target=self.target,
                       endpoint=self.endpoint, status=self.status, finished=self.finished,
                       error=self.error, log_path=str(self.log_path),
                       owner=self.owner, description=self.description,
                       created_at=self.created_at, updated_at=db_mod.now_str())
        conflict_update = {k: v for k, v in payload.items() if k != "created_at"}
        db_mod.OssTaskModel.insert(**payload).on_conflict(
            update=conflict_update, conflict_target="id").execute()

    def log(self, msg: str):
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

    def read_progress(self):
        """解析 download.log 尾部的 ossutil 进度行，返回进度详情 dict 或 None。

        形如：Total 23 objects,26.777 GiB, Copy... done:(13 files,6.321 GiB), 42.111%, avg 1.612 MiB/s
        """
        if self.finished and self.status == "done":
            return {"percent": 100.0, "total": "", "done": "", "speed": "", "text": "已完成"}
        if not self.log_path.is_file():
            return None
        try:
            with self.log_path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 8192))
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            return None
        for line in reversed(tail.splitlines()):
            if "%" not in line or "avg" not in line:
                continue
            m = re.search(r"([\d.]+)%", line)
            total = re.search(r"Total\s+[^,]+,([^,]+),", line)
            done = re.search(r"(?:done|skipped):\(([^)]*)\)", line)
            skipped = re.search(r"skipped:\(([^)]*)\)", line)
            speed = re.search(r"avg\s+(.+?)\s*$", line)
            total_s = total.group(1).strip() if total else ""
            done_s = done.group(1).strip() if done else ""
            skipped_s = skipped.group(1).strip() if skipped else ""
            # ossutil 的 % 是"本次运行"进度，续传时会重置；改用 (已完成+已跳过)/总量
            # 作为真实进度（如 13 files,6.32GiB 已存在 → 约 42% 而非从 0 开始）
            percent = float(m.group(1)) if m else None
            total_gib = _to_gib(total_s)
            if total_gib > 0:
                done_gib = _to_gib(done_s.split(",")[-1] if "," in done_s else done_s) + \
                    _to_gib(skipped_s.split(",")[-1] if "," in skipped_s else skipped_s)
                if done_gib > 0:
                    percent = round(min(100.0, done_gib / total_gib * 100), 1)
            return {
                "percent": percent,
                "total": total_s,
                "done": done_s,
                "skipped": skipped_s,
                "speed": speed.group(1).strip() if speed else "",
            }
        return None

    def public(self):
        with self._lock:
            return {"id": self.id, "oss_path": self.oss_path, "target": self.target,
                    "status": self.status, "finished": self.finished, "error": self.error,
                    "owner": self.owner, "description": self.description,
                    "progress": self.read_progress()}

    def run(self):
        self._save()
        # 登记信息留痕（审计：谁申请、数据用途）
        self.log(f"INFO 等级负责人: {self.owner or '-'}；数据情况说明: {self.description or '-'}")
        # 目标带尾杠：ossutil 会把对象（目录）拷贝为 target/<对象名>/
        # -e 指定用户提供的 endpoint（去协议），region 已登记在配置文件
        # --ignore-existing：目标已存在的完整文件跳过（断点续传/不重复下载）
        # --checkpoint-dir：大文件中途被打断后的分片进度，续传时从断点继续
        #
        # 注意：ossutil 会在目的目录下自动创建"源前缀最后一段"的同名目录
        # （dry-run 证实：cp oss://b/PREFIX DEST/ → DEST/PREFIX/…）。故这里传
        # raw_root（即数据目录 target 的父目录），结果正好是 target=PREFIX 所在层：
        #   <raw_root>/<对象名>/RawData/…  而不是 <raw_root>/<对象名>/<对象名>/RawData/…
        copy_root = str(Path(self.target).parent) + os.sep
        endpoint_host = self.endpoint.split("//")[-1].strip("/")
        cmd = [self.ossutil_bin, "cp", "-rf", self.oss_path, copy_root,
               "-e", endpoint_host, "--config-file", str(OSS_CONFIG),
               "--ignore-existing", "--checkpoint-dir", str(self.checkpoint_dir)]
        self.log(("RESUME " if self.is_resume else "RUN  ") + " ".join(cmd))
        try:
            proc = subprocess.Popen(cmd, cwd=str(self.dir),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                for line in proc.stdout:
                    fh.write(line)
            proc.stdout.close()
            proc.wait()
            if proc.returncode == 0:
                self.status = "done"
                self._save()
                self.log("OK   下载完成 (exit=0)")
            else:
                self.status = "failed"
                self.error = f"ossutil 退出码 {proc.returncode}"
                self._save()
                self.log("FAIL " + self.error)
        except FileNotFoundError:
            self.status = "failed"
            self.error = f"未找到 ossutil（{self.ossutil_bin}），请安装或修改 config.ini"
            self._save()
            self.log("ERR  " + self.error)
        except Exception as exc:  # noqa: BLE001
            self.status = "failed"
            self.error = str(exc)
            self._save()
            self.log("ERR  " + self.error)
        finally:
            self.finished = True
            self._save()
            self.log("TASK DONE")
            # 下载完成/失败邮件提醒（mailer 未启用时静默跳过）
            try:
                mailer.mail_oss_done(self)
            except Exception as exc:  # noqa: BLE001
                self.log(f"WARN 邮件提醒发送失败: {exc}")


class OssManager:
    def __init__(self, ossutil_bin: str = OSSUTIL_BIN, resume_on_start: bool = True):
        self.ossutil_bin = ossutil_bin
        self.resume_on_start = resume_on_start
        self._tasks = {}
        self._by_source = {}      # 去重键 (oss_path|endpoint|target) -> task_id
        self._lock = threading.Lock()
        self._load()
        if self.resume_on_start:
            self._resume_interrupted()

    @staticmethod
    def source_key(oss_path: str, endpoint: str, target: str) -> str:
        """相同交付信息（路径+endpoint）且下载到同一目标目录 ⇒ 视为同一任务。"""
        return "|".join([str(oss_path).strip().rstrip("/"),
                         str(endpoint).strip().rstrip("/"),
                         str(target).strip().rstrip("/")])

    def _load(self):
        """从 SQLite 重建内存任务表与去重索引（服务重启后仍可按 ID 查询历史任务）。"""
        self._tasks = {}
        self._by_source = {}
        for row in db_mod.OssTaskModel.select().order_by(db_mod.OssTaskModel.id):
            task = OssTask.from_model(row, self.ossutil_bin)
            self._tasks[row.id] = task
            self._by_source[self.source_key(task.oss_path, task.endpoint, task.target)] = row.id

    def _resume_interrupted(self):
        """服务启动时自动续传被中断的任务（status=running 但下载线程已随上次进程消失）。

        同一交付信息（source_key 相同）的多个残留任务只恢复一个（按创建时间最早），
        其余保留待人工合并/删除，避免重复下载。failed 任务不自动恢复（可能是配置错误，
        等用户重新提交时再走 resumed）。
        """
        resumed_keys = set()
        for task in sorted(self._tasks.values(), key=lambda t: (t.created_at, t.id)):
            if task.status != "running" or task.finished:
                continue
            if task.thread is not None and task.thread.is_alive():
                continue
            key = self.source_key(task.oss_path, task.endpoint, task.target)
            if key in resumed_keys:
                continue
            if _ossutil_running(task.oss_path):
                # 上次进程的 ossutil 仍在跑（服务重启后的孤儿进程），不重复启动；
                # 起一个看护线程，待其结束后校验完整性并按需续传，使状态能收敛到 done
                self._by_source[key] = task.id
                task.log("INFO 服务启动: 该目标已有遗留 ossutil 在下载,不重复启动；结束后自动校验续传")
                self._watch_orphan(task)
                resumed_keys.add(key)
                continue
            resumed_keys.add(key)
            self._by_source[key] = task.id
            task.log("INFO 服务启动: 检测到被中断的任务,自动续传（跳过已下载文件）")
            self._spawn(task, resume=True)

    def _watch_orphan(self, task):
        """等待遗留 ossutil 进程结束后，重新跑一次续传以校验完整性并更新状态
        （已下载完整的文件会被 --ignore-existing 全部跳过，秒完成；若有缺失则补齐）。"""
        def _wait():
            while _ossutil_running(task.oss_path):
                time.sleep(30)
            task.log("INFO 遗留 ossutil 已结束,校验完整性并续传（已下载文件将跳过）")
            self._spawn(task, resume=True)
        threading.Thread(target=_wait, daemon=True).start()

    def _new_id(self) -> str:
        n = int(time.time())
        while f"oss_{n}" in self._tasks:
            n += 1
        return f"oss_{n}"

    def _target_of(self, creds: dict, raw_root: str) -> str:
        obj_name = creds["oss_path"].rstrip("/").split("/")[-1]
        return str(Path(raw_root) / obj_name) + os.sep

    def _spawn(self, task: OssTask, resume: bool = False) -> OssTask:
        task.status = "running"
        task.error = None
        task.finished = False
        task.is_resume = resume
        task._save()
        with self._lock:
            self._tasks[task.id] = task
        task.thread = threading.Thread(target=task.run, daemon=True)
        task.thread.start()
        return task

    def submit(self, creds: dict, raw_root: str, owner: str = "",
               description: str = ""):
        """提交下载，按交付信息去重；返回 (task, action)。

        action: created=新建 / running=相同任务正在下载（不重复启动）/
                done=相同数据此前已下载完成（不重复下载）/
                resumed=此前失败或中断，复用同一任务续传。
        """
        target = self._target_of(creds, raw_root)
        key = self.source_key(creds["oss_path"], creds["endpoint"], target)
        with self._lock:
            existing = self._tasks.get(self._by_source.get(key))

        if existing is None:
            task = OssTask(self._new_id(), creds["oss_path"], target, self.ossutil_bin,
                           creds["endpoint"], owner=owner, description=description)
            with self._lock:
                self._by_source[key] = task.id
            self._spawn(task, resume=False)
            return task, "created"

        # 命中同一任务：可更新登记信息（等级负责人/数据情况说明）
        if owner:
            existing.owner = owner
        if description:
            existing.description = description
        still_running = existing.thread is not None and existing.thread.is_alive()
        if still_running:
            existing._save()
            return existing, "running"
        if existing.status == "done":
            existing._save()
            return existing, "done"
        # 其余情况（failed，或 running 但线程已消失——如服务重启后残留）：复用原任务续传；
        # 若该目标已有 ossutil 进程在跑（重启后的孤儿进程），视为仍在下载
        if _ossutil_running(existing.oss_path):
            existing._save()
            return existing, "running"
        self._spawn(existing, resume=True)
        return existing, "resumed"

    def get(self, task_id: str):
        return self._tasks.get(task_id)

    def list(self):
        return [t.public() for t in sorted(self._tasks.values(), key=lambda t: t.id)]

    # ---------------- 删除 / 合并 ----------------
    def remove(self, task_id: str):
        """删除 OSS 任务记录与任务目录（日志/checkpoint），不动已下载的数据目录。

        正在下载的任务拒绝删除。返回被删任务 id；不存在返回 None。
        """
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return None
        if task.thread is not None and task.thread.is_alive():
            raise RuntimeError("任务正在下载，无法删除")
        with self._lock:
            self._tasks.pop(task_id, None)
            key = self.source_key(task.oss_path, task.endpoint, task.target)
            if self._by_source.get(key) == task_id:
                del self._by_source[key]
        db_mod.OssTaskModel.delete().where(db_mod.OssTaskModel.id == task_id).execute()
        if task.dir.exists():
            shutil.rmtree(task.dir, ignore_errors=True)
        return task_id

    def merge(self, keep_id: str, drop_id: str):
        """合并重复任务：keep 保留，drop 删除。

        仅允许合并同一交付信息（source_key 相同，即同一数据下到同一目录）的任务；
        owner/description 若 keep 为空则从 drop 补齐。返回报告 dict。
        """
        keep, drop = self._tasks.get(keep_id), self._tasks.get(drop_id)
        if keep is None or drop is None:
            return {"error": "任务不存在"}
        k1 = self.source_key(keep.oss_path, keep.endpoint, keep.target)
        k2 = self.source_key(drop.oss_path, drop.endpoint, drop.target)
        if k1 != k2:
            return {"error": "两个任务交付信息或目标目录不同，不能合并"}
        if keep.thread is not None and keep.thread.is_alive():
            return {"error": "保留任务正在下载，无法合并"}
        if keep_id == drop_id:
            return {"error": "不能与自身合并"}
        if not keep.owner:
            keep.owner = drop.owner
        if not keep.description:
            keep.description = drop.description
        keep._save()
        self.remove(drop_id)
        return {"kept": keep_id, "dropped": drop_id,
                "owner": keep.owner, "description": keep.description}
