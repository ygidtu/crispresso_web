"""邮件通知：OSS 下载完成 / CRISPResso 分析任务完成后发送。

SMTP 配置见 config.ini [mail]（host/port/user/password/to/enabled），
由 app.py 启动时调用 init() 注入；发送失败仅记录日志，不影响主流程。
"""
import re
import smtplib
import ssl
import time
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

_CFG = {}
_ENABLED = False
ATTACH_MAX = 45 * 1024 * 1024      # 附件大小上限（普通附件约 50MB，留余量）
LOG_ATTACH_MAX = 10 * 1024 * 1024  # 日志作附件的大小上限
BODY_MAX = 512 * 1024              # 正文超长则截尾（日志不再进正文，仅作附件）


def init(cfg: dict, enabled: bool = True):
    """注入 [mail] 配置；user/password/to 任一缺失即视为未启用。"""
    global _CFG, _ENABLED
    _CFG = cfg or {}
    _ENABLED = bool(enabled and _CFG.get("user") and _CFG.get("password") and _CFG.get("to"))


def enabled() -> bool:
    return _ENABLED


def send(subject: str, body: str, attachments=(), to=None) -> bool:
    """发送邮件。attachments: [(文件名, bytes)]。返回是否成功。"""
    if not _ENABLED:
        return False
    to_list = [x.strip() for x in str(to or _CFG.get("to", "")).split(",") if x.strip()]
    if not to_list:
        return False
    if len(body.encode("utf-8")) > BODY_MAX:
        body = body[-BODY_MAX // 2:] + ("\n\n……（日志过长已截断前半部分，"
                                        "完整日志请在平台任务详情中下载 run.log）")
    msg = MIMEMultipart()
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = _CFG["user"]
    msg["To"] = ", ".join(to_list)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    for name, data in attachments:
        part = MIMEApplication(data)
        part.add_header("Content-Disposition", "attachment",
                        filename=Header(name, "utf-8").encode())
        msg.attach(part)
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(_CFG.get("host", "smtp.qq.com"),
                              int(_CFG.get("port", 465)), timeout=30, context=ctx) as s:
            s.login(_CFG["user"], _CFG["password"])
            s.sendmail(_CFG["user"], to_list, msg.as_string())
        return True
    except Exception as e:  # noqa: BLE001  邮件失败不影响主流程
        try:
            from loguru import logger
            logger.warning(f"邮件发送失败({subject}): {e}")
        except Exception:
            pass
        return False


def tail(path, n: int = 400) -> str:
    """读日志文件尾部 n 行（正文中展示，避免邮件过大）。"""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        with p.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 1024 * 1024))
            data = fh.read().decode("utf-8", "replace")
        lines = data.splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def log_attachment(path, name: str) -> list:
    """日志文件作附件（不再进正文）；缺失/过大则返回提示条目。"""
    p = Path(path)
    if not p.is_file():
        return []
    size = p.stat().st_size
    if size > LOG_ATTACH_MAX:
        return []
    try:
        return [(name, p.read_bytes())]
    except OSError:
        return []


def _cfg_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " ".join(str(x) for x in v)
    return str(v)


def _recipients(job=None) -> str:
    """收件人 = 默认(config to) + 任务额外邮箱(cfg.notify_email)，去重保序。"""
    base = [x.strip() for x in str(_CFG.get("to", "")).split(",") if x.strip()]
    if job is not None:
        extra = _cfg_str(job.cfg.get("notify_email"))
        for x in [y.strip() for y in extra.split(",") if y.strip()]:
            if x.lower() not in [b.lower() for b in base]:
                base.append(x)
    return ",".join(base)


def mail_oss_done(task):
    """OSS 下载任务完成/失败通知（始终发默认收件人）。"""
    if not _ENABLED:
        return
    verb = "完成" if task.status == "done" else "失败"
    subject = f"[CRISPResso平台] OSS 下载{verb} · {task.id}"
    body = (
        f"OSS 下载任务:{task.id}\n"
        f"状态:{task.status}（{'下载完成' if verb == '完成' else '下载失败'}）\n"
        f"来源:{task.oss_path}\n"
        f"目标目录:{task.target}\n"
        f"等级负责人:{task.owner or '-'}\n"
        f"数据情况说明:{task.description or '-'}\n"
        f"错误:{task.error or '-'}\n"
        f"时间:{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"（下载日志见附件 {task.id}_download.log）"
    )
    send(subject, body, attachments=log_attachment(task.log_path,
                                                   f"{task.id}_download.log"))


def mail_job_done(job):
    """CRISPResso 分析任务完成通知：pack.zip 与 run.log 均作附件（日志不进正文）。"""
    if not _ENABLED:
        return
    to = _recipients(job)
    failed = [s for s, st in job.status.items() if st == "failed"]
    ok = not failed
    subject = (f"[CRISPResso平台] 分析任务{'完成' if ok else '失败'} · "
               f"{job.id} · {job.cfg.get('batch', '')}")
    lines = [
        f"分析任务:{job.id}",
        f"批次:{job.cfg.get('batch', '')} · sheet:{_cfg_str(job.cfg.get('sheet'))}",
        f"scores:{_cfg_str(job.cfg.get('scores'))}",
        f"ignore_substitutions:{job.cfg.get('ignore_substitutions')}",
        (f"定量窗口参数:center={job.cfg.get('quantification_window_center')}, "
         f"size={job.cfg.get('quantification_window_size')}, "
         f"nw_gap_open={job.cfg.get('needleman_wunsch_gap_open')}"
         "（留空=CRISPResso2 默认）"),
        f"创建:{job.created_at}  结束:{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "", "步骤状态:",
    ]
    for s in ("config", "amplicons", "split", "crispresso", "collect", "pack"):
        lines.append(f"  {s}: {job.status.get(s)}")
    if failed:
        lines.append("\n失败步骤:")
        for s in failed:
            lines.append(f"  {s}: {job.error.get(s) or ''}")
    lines.append("\n（完整运行日志见附件 run.log）")
    body = "\n".join(lines)

    attachments = []
    pack = job.dir / "pack.zip"
    if pack.is_file():
        size = pack.stat().st_size
        if size <= ATTACH_MAX:
            attachments.append(("pack.zip", pack.read_bytes()))
        else:
            body += (f"\n\n（打包文件 {size / 1024 / 1024:.1f}MB 超过邮件附件上限，"
                     "请在平台任务详情中下载 pack.zip）")
    attachments += log_attachment(job.log_path, "run.log")
    send(subject, body, attachments=attachments, to=to)
