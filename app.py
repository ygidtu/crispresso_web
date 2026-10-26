#!/usr/bin/env python3
"""CRISPResso2 分析 Web 平台 —— Flask 后端。

暴露 REST 接口，封装 XueChaoYou_WYW/main.py：上传 metainfo Excel → 指定原始 fastq 路径与参数 →
后台分步跑通 config→amplicons→split→crispresso→collect→pack，并提供日志回采与结果打包下载。
依赖由 uv 管理（见 pyproject.toml）；启动：./run.sh 或 uv run python app.py。
"""
import json
import os
import re
import tempfile
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory, Response

from backend.runner import (JobManager, STEPS,
                              DEFAULT_MAIN_PY, DEFAULT_LMY_DIR, DEFAULT_JOBS_ROOT,
                              ENV_MAIN_PY, ENV_LMY_DIR, ENV_JOBS_ROOT)
from backend import inspect as xl
from backend import db as db_mod
from backend import mailer

BASE = Path(__file__).resolve().parent
FRONTEND_DIR = BASE / "frontend"
FRONTEND_DIST = FRONTEND_DIR / "dist" / "index.html"
UPLOAD_DIR = BASE / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _index_path():
    return FRONTEND_DIST if FRONTEND_DIST.is_file() else FRONTEND_DIR / "index.html"


def _assets_dir():
    return (FRONTEND_DIR / "dist" if FRONTEND_DIST.is_file() else FRONTEND_DIR)


def _env(key, default):
    return os.environ.get(key, default)


# ---- 流水线包位置（import RECIPES 用）----
_LMY_DIR = Path(_env(ENV_LMY_DIR, DEFAULT_LMY_DIR))
MAIN_PY = Path(_env(ENV_MAIN_PY, DEFAULT_MAIN_PY))
JOBS_ROOT = Path(_env(ENV_JOBS_ROOT, DEFAULT_JOBS_ROOT))

sys_path_added = False
if _LMY_DIR.is_dir() and _LMY_DIR not in [Path(p) for p in __import__("sys").path]:
    __import__("sys").path.insert(0, str(_LMY_DIR))
    sys_path_added = True

try:
    from pipeline.config import RECIPES
    BATCHES = {name: rec.get("sheet") for name, rec in RECIPES.items()}
except Exception:  # noqa: BLE001  流水线包不可用时应给出友好提示而非 500
    RECIPES, BATCHES = {}, {}

# ---- Web 服务配置（config.ini）----
import configparser
import ipaddress

WEB_CONFIG_PATH = Path(_env("CRWeb_WEB_CONFIG", BASE / "config.ini"))
_web_cfg = configparser.ConfigParser()
if WEB_CONFIG_PATH.is_file():
    _web_cfg.read(WEB_CONFIG_PATH, encoding="utf-8")


def _cfg(section, key, default):
    return _web_cfg.get(section, key, fallback=_env(f"CRWeb_{key.upper()}", default))


HOST = _cfg("server", "host", "0.0.0.0")
PORT = int(_cfg("server", "port", "5000"))

# ---- 跨任务缓存：config.ini [cache] enabled/cache_dir ----
_CACHE_ENABLED = str(_cfg("cache", "enabled", "true")).lower() == "true"
_CACHE_DIR = Path(_cfg("cache", "cache_dir", str(BASE / "cache")))

manager = JobManager(MAIN_PY, _LMY_DIR, JOBS_ROOT,
                     cache_dir=_CACHE_DIR, cache_enabled=_CACHE_ENABLED)

# ---- 访问白名单：仅允许 config.ini [security] allowed_ips 内的来源 ----
_LOOPBACK = {ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")}


def _load_allowed_networks():
    nets = set(_LOOPBACK)
    raw = _cfg("security", "allowed_ips", "")
    for item in re.split(r"[,\s;]+", raw):
        item = item.strip()
        if not item:
            continue
        try:
            # x.x.x.0 结尾视为 /24 网段（ip段），否则按单 IP 处理
            if item.endswith(".0") and "/" not in item:
                item += "/24"
            nets.add(ipaddress.ip_network(item, strict=False))
        except ValueError:
            print(f"[config] 忽略非法 IP/网段: {item}")
    return nets


ALLOWED_NETWORKS = _load_allowed_networks()

app = Flask(__name__, static_folder=None)


@app.before_request
def _ip_whitelist():
    ip = request.remote_addr or ""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return jsonify({"error": f"无法识别来源 IP: {ip}"}), 403
    if any(addr in net for net in ALLOWED_NETWORKS):
        return None
    return jsonify({"error": "拒绝访问：来源 IP 不在白名单"}), 403


# ---- OSS 下载管理 ----
from backend import keys as oss_keys
from backend import oss as oss_mod

db_mod.init_db()
oss_manager = oss_mod.OssManager(
    ossutil_bin=_cfg("oss", "ossutil_bin", "ossutil"),
    resume_on_start=str(_cfg("oss", "resume_on_start", "true")).lower() == "true")

# ---- 邮件通知（任务/OSS 下载完成后发送；config.ini [mail]）----
mailer.init({k: _cfg("mail", k, "") for k in
             ("host", "port", "user", "password", "to")},
            enabled=str(_cfg("mail", "enabled", "true")).lower() == "true")


# ============================== 辅助 ==============================
def _parse_scores(raw):
    if raw is None:
        return [25, 30, 35, 40, 45, 50]
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = re.split(r"[,\s]+", str(raw).strip())
    out = []
    for it in items:
        if it in ("", None):
            continue
        out.append(int(it))
    return out or [25, 30, 35, 40, 45, 50]


def _parse_opt_int(raw, minimum=None):
    """可选数值参数：空/缺省/脏值 → None（使用 CRISPResso2 原生默认值）。"""
    if raw is None or not str(raw).strip():
        return None
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if minimum is not None:
        v = max(minimum, v)
    return v


def _safe_join(base, rel):
    target = (Path(base) / rel).resolve()
    base_resolved = Path(base).resolve()
    if str(target).startswith(str(base_resolved) + os.sep):
        return target
    return None


# ============================== 前端 ==============================
@app.route("/")
def index():
    return send_file(_index_path())


@app.route("/<path:filename>")
def serve_static(filename):
    # 优先命中 Vite 构建产物 (dist/assets/...)，否则回退到前端源码目录
    return send_from_directory(_assets_dir(), filename)


# ============================== 元信息接口 ==============================
@app.route("/api/health")
def health():
    return jsonify({"ok": True, "main_py": str(MAIN_PY), "lmy_dir": str(_LMY_DIR),
                    "jobs_root": str(JOBS_ROOT), "recipes": sorted(BATCHES)})


@app.route("/api/batches")
def batches():
    return jsonify({"batches": sorted(BATCHES), "steps": STEPS})


@app.route("/api/paths")
def api_paths():
    """目录自动补全（递归深入，仅目录）：

    - 前缀以 '/' 结尾（如 /mnt/nas/）：列出该目录下所有子目录
    - 前缀不以 '/' 结尾（如 /mnt/nas）：列出父目录下以该后缀开头的子目录
    """
    prefix = request.args.get("prefix", "") or ""
    trimmed = prefix.rstrip("\\/")
    if not trimmed:
        return jsonify([])
    list_dir = trimmed != prefix  # 原始输入带尾斜杠 → 列该层全部子目录
    if list_dir:
        parent, base = trimmed, ""
    else:
        parent = os.path.dirname(trimmed) or os.sep
        base = os.path.basename(trimmed)
    matches = []
    try:
        for name in sorted(os.listdir(parent)):
            if not name.startswith(base):
                continue
            full = os.path.join(parent, name)
            try:
                if os.path.isdir(full):
                    matches.append(full + os.sep)
            except OSError:
                continue
    except OSError:
        pass
    return jsonify(matches[:50])


# ---- raw_root 下拉：runs_root 子文件夹 + fastq 父级自动探测 ----
RUNS_ROOT = Path(_cfg("data", "runs_root", str(BASE / "runs_data")))
FQ_EXTS = (".fq.gz", ".fastq.gz", ".fq", ".fastq")


def _is_fq(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in FQ_EXTS)


def _detect_fastq_root(run_dir: Path):
    """返回满足 `{root}/*/*.fq.gz` 的 root（即 fastq 的父级目录），找不到返回 None。

    逐层探测：L0=run_dir 本身、L1=一级子目录、L2=二级子目录。
    """
    def fq_count(d: Path) -> int:
        try:
            return sum(1 for p in d.iterdir()
                       if p.is_file() and _is_fq(p.name))
        except OSError:
            return 0

    def subdirs(d: Path):
        try:
            return sorted((p for p in d.iterdir() if p.is_dir()), key=lambda p: p.name)
        except OSError:
            return []

    # L1 样本子目录里任意一个含 fastq 即满足（yw4 files.json 用 raw_root/*/*.fq.gz）
    samples = [s for s in subdirs(run_dir) if fq_count(s) > 0]
    if samples:
        return run_dir, len(samples)

    for sub in subdirs(run_dir):                      # L1 探测：<run>/<sub>/*/*.fq.gz
        samples = [s for s in subdirs(sub) if fq_count(s) > 0]
        if samples:
            return sub, len(samples)
        for sub2 in subdirs(sub):                     # L2 探测：<run>/<sub>/<sub2>/*/*.fq.gz
            if fq_count(sub2) > 0:
                return sub2, len(subdirs(sub2))
    return None, 0


@app.route("/api/runs")
def list_runs():
    """raw_root 下拉选项：runs_root 下的一级子文件夹（按修改时间倒序）。"""
    root = RUNS_ROOT
    if not root.is_dir():
        return jsonify({"runs": [], "runs_root": str(root), "error": "数据根目录不存在"})
    entries = []
    for p in root.iterdir():
        if p.is_dir():
            try:
                entries.append({"name": p.name, "path": str(p) + os.sep,
                                "mtime": p.stat().st_mtime})
            except OSError:
                continue
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    for e in entries:
        e.pop("mtime")
    return jsonify({"runs": entries[:200], "runs_root": str(root)})


@app.route("/api/runs/fastq_root")
def runs_fastq_root():
    """探测选中 run 文件夹内 fastq 的父级路径（= pipeline 需要的 raw_root）。"""
    d = (request.args.get("dir", "") or "").strip()
    if not d:
        return jsonify({"error": "缺少 dir"}), 400
    run_dir = Path(d)
    # 安全：仅允许 runs_root 的一级子目录，防目录遍历
    try:
        run_dir.resolve().relative_to(RUNS_ROOT.resolve())
    except ValueError:
        return jsonify({"error": "dir 不在数据根目录内"}), 403
    if not run_dir.is_dir():
        return jsonify({"error": "目录不存在"}), 404
    fastq_root, samples = _detect_fastq_root(run_dir)
    return jsonify({"fastq_root": str(fastq_root) + os.sep if fastq_root else None,
                    "samples": samples})


@app.route("/api/preview", methods=["POST"])
def preview():
    if "xlsx" not in request.files:
        return jsonify({"error": "缺少 xlsx 文件"}), 400
    fp = request.files["xlsx"]
    fd, path = tempfile.mkstemp(suffix=".xlsx", dir=UPLOAD_DIR)
    os.close(fd)
    fp.save(path)
    try:
        sheets = []
        suggestion = None
        checks = {}
        for name in xl.list_sheets(path):
            preview = xl.sheet_preview(path, name, nrows=3)
            checks[name] = xl.check_sheet(path, name)
            sheets.append({"name": name, "columns": preview["columns"], "rows": preview["rows"],
                           "ok": checks[name]["ok"], "errors": checks[name]["errors"]})
            if suggestion is None and checks[name]["ok"]:
                suggestion = name
        if not any(c["ok"] for c in checks.values()) and sheets:
            detail = "；".join(f"{n}: {', '.join(c['errors'])}"
                               for n, c in checks.items() if not c["ok"])
            return jsonify({"error": f"Excel 格式不符合 yw4 解析要求：{detail}"}), 400
        return jsonify({"sheets": sheets, "suggested_sheet": suggestion})
    finally:
        os.path.exists(path) and os.remove(path)


# ============================== 任务 ==============================
@app.route("/api/jobs", methods=["POST"])
def create_job():
    if "xlsx" in request.files and request.files["xlsx"].filename:
        fp = request.files["xlsx"]
        dest = UPLOAD_DIR / f"up_{int(__import__('time').time())}_{fp.filename}"
        fp.save(dest)
        xlsx_abs = str(dest.resolve())
    elif request.get_json(silent=True) and request.get_json(silent=True).get("xlsx_path"):
        xlsx_abs = request.get_json(silent=True)["xlsx_path"]
    else:
        return jsonify({"error": "请上传 metainfo Excel 文件"}), 400

    data = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
    raw_root = (data.get("raw_root") or "").strip()
    if not raw_root:
        return jsonify({"error": "必填: 原始 fastq 根目录 raw_root"}), 400

    cfg = {
        "sheet": (data.get("sheet") or "").strip(),
        "batch": (data.get("batch") or "yw4").strip(),
        "raw_root": raw_root,
        "scores": _parse_scores(data.get("scores")),
        "workers": max(1, int(data.get("workers") or 16)),
        "force": str(data.get("force", "false")).lower() == "true",
        "ignore_substitutions": str(data.get("ignore_substitutions", "false")).lower() == "true",
        # 留空 = CRISPResso2 原生默认（qw_center=-3 / qw_size=1 / nw_gap_open=-20）
        "quantification_window_center": _parse_opt_int(data.get("quantification_window_center")),
        "quantification_window_size": _parse_opt_int(data.get("quantification_window_size"), minimum=1),
        "needleman_wunsch_gap_open": _parse_opt_int(data.get("needleman_wunsch_gap_open")),
        # 额外通知邮箱（可选；任务完成后结果邮件追加发给该邮箱）
        "notify_email": (data.get("notify_email") or "").strip(),
        "xlsx": xlsx_abs,
    }
    try:
        job = manager.create(cfg, xlsx_abs)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"生成配置失败: {exc}"}), 400
    return jsonify({"job_id": job.id, "sheet": cfg["sheet"], "batch": cfg["batch"],
                    "scores": cfg["scores"], "raw_root": raw_root, "log": str(job.log_path)})


@app.route("/api/jobs/<job_id>/start", methods=["POST"])
def start_job(job_id):
    body = request.get_json(silent=True) or {}
    steps = body.get("steps")
    if steps is not None:
        steps = [s.strip() for s in re.split(r"[,\s]+", str(steps)) if s.strip()]
        invalid = [s for s in steps if s not in STEPS]
        if invalid:
            return jsonify({"error": f"未知步骤: {invalid} (可选 {STEPS})"}), 400
    job = manager.start(job_id, steps)
    if job is None:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({"job_id": job_id, "started": True, "steps": steps or STEPS})


def _disk_job_dir(job_id):
    """任务不在内存（如服务重启后）时，从磁盘目录校验其存在。"""
    d = JOBS_ROOT / job_id
    return d if d.is_dir() else None


def _disk_steps(d):
    """从产物目录推断各步骤状态（供重启后查询历史任务）。"""
    def anyglob(pat):
        return bool(list(d.glob(pat)))
    return {
        "config": "done" if (anyglob("split/barcode_*.json") or anyglob("split/*/barcode.json")) else "pending",
        "amplicons": "done" if anyglob("split/amplicons_*.json") else "pending",
        "split": "done" if (anyglob("split/*/files.json") or anyglob("split/files_*.json")
                            or (d / "split/samples_list.csv").exists()
                            or (d / "split/editing_efficiency.csv").exists()) else "pending",
        "crispresso": "done" if anyglob("res/crispresso_manual/*") else "pending",
        "collect": "done" if ((d / "编辑效率_with_score.xlsx").exists() or anyglob("plots/*")) else "pending",
        "pack": "done" if (d / "pack.zip").exists() else "pending",
    }


def _disk_job_status(job_id, d):
    cfg = {}
    ini = d / "config.ini"
    if ini.is_file():
        cp = configparser.ConfigParser()
        cp.read(ini, encoding="utf-8")
        for sec in ("pipeline", "inputs", "analysis"):
            for k, v in cp.items(sec):
                cfg[k] = v
    steps = _disk_steps(d)
    scores = cfg.get("scores", "")
    return {"id": job_id, "finished": steps["pack"] == "done", "from_disk": True,
            "steps": steps, "error": {},
            "cfg": {"sheet": cfg.get("sheet", ""), "batch": cfg.get("batch", ""),
                    "raw_root": cfg.get("raw_root", ""), "scores": scores.split(),
                    "workers": int(cfg["workers"]) if cfg.get("workers", "").isdigit() else 0,
                    "force": cfg.get("force", "false") == "true",
                    "ignore_substitutions": cfg.get("ignore_substitutions", "false") == "true",
                    "quantification_window_center": _parse_opt_int(cfg.get("quantification_window_center")),
                    "quantification_window_size": _parse_opt_int(cfg.get("quantification_window_size"), minimum=1),
                    "needleman_wunsch_gap_open": _parse_opt_int(cfg.get("needleman_wunsch_gap_open"))}}


@app.route("/api/jobs/<job_id>")
def job_status(job_id):
    job = manager.get(job_id)
    if job is not None:
        return jsonify(job.public_status())
    d = _disk_job_dir(job_id)
    if d is None:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(_disk_job_status(job_id, d))


@app.route("/api/jobs")
def list_jobs():
    return jsonify({"jobs": manager.list()})


@app.route("/api/jobs/<job_id>/log")
def job_log(job_id):
    job = manager.get(job_id)
    log_path = job.log_path if job is not None else None
    if log_path is None:
        d = _disk_job_dir(job_id)
        if d is None:
            return jsonify({"error": "任务不存在"}), 404
        log_path = d / "run.log"
    tail = int(request.args.get("tail", 2000))
    text = ""
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        text = "\n".join(lines[-tail:])
    return Response(text, mimetype="text/plain; charset=utf-8")


@app.route("/api/jobs/<job_id>/download")
def job_download(job_id):
    job = manager.get(job_id)
    d = job.dir if job is not None else _disk_job_dir(job_id)
    if d is None:
        return jsonify({"error": "任务不存在"}), 404
    rel = request.args.get("path", "")
    target = _safe_join(d, rel)
    if target is None or not target.is_file():
        return jsonify({"error": f"文件不存在: {rel}"}), 404
    return send_file(target, as_attachment=True,
                     download_name=Path(rel).name or "download")


@app.route("/api/jobs/<job_id>/pack")
def job_pack(job_id):
    job = manager.get(job_id)
    d = job.dir if job is not None else _disk_job_dir(job_id)
    if d is None:
        return jsonify({"error": "任务不存在"}), 404
    zip_path = d / "pack.zip"
    if not zip_path.is_file():
        return jsonify({"error": "尚未生成打包产物（pack.zip），请先跑通 pack 步骤"}), 404
    return send_file(zip_path, as_attachment=True, download_name=f"{job_id}_pack.zip")


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    """删除任务：仅物理释放"没有被其他任务引用"的产物（共享缓存链接只解除引用）。

    返回报告：freed_exclusive_bytes（真正释放）、cache_pruned（连带清理的缓存条目）、
    cache_kept（仍被其他任务引用而保留的缓存条目）。
    """
    report = manager.delete(job_id)
    if report is None:
        return jsonify({"error": "任务不存在"}), 404
    if report.get("error"):
        return jsonify(report), 409
    return jsonify(report)


# ============================== OSS 数据下载 ==============================
@app.route("/api/oss/pubkey")
def oss_pubkey():
    """前端用该公钥做信封加密（RSA-OAEP 包裹随机 AES 密钥）。"""
    return jsonify(oss_keys.get_public_jwk())


@app.route("/api/oss/download", methods=["POST"])
def oss_download():
    payload = request.get_json(silent=True) or {}
    # 登记限定条件：等级负责人 + 数据情况说明（必填，落库并在下载日志留痕）
    owner = (payload.get("owner") or "").strip()
    description = (payload.get("description") or "").strip()
    if not owner:
        return jsonify({"error": "必填: 等级负责人"}), 400
    if not description:
        return jsonify({"error": "必填: 数据情况说明"}), 400
    # OSS 下载基座固定为数据根目录（与检测出的 fastq raw_root 解耦）
    raw_root = (payload.get("raw_root") or "").strip() or str(RUNS_ROOT)
    if not Path(raw_root).is_dir():
        return jsonify({"error": f"raw_root 目录不存在: {raw_root}"}), 400
    try:
        text = oss_mod.decrypt_payload(payload)
        creds = oss_mod.parse_oss_info(text)
        oss_mod.write_ossutil_config(creds)
    except Exception as exc:  # noqa: BLE001  解密失败/信息不完整时统一 400，不泄露细节
        return jsonify({"error": f"OSS 信息解密或解析失败: {exc}"}), 400
    # 按交付信息去重：相同任务不重复下载（运行时返回 running，已完成返回 done，
    # 失败/中断则复用原任务续传）；登记信息每次可更新
    task, action = oss_manager.submit(creds, raw_root, owner=owner, description=description)
    resp = task.public()
    resp["action"] = action
    return jsonify(resp)


@app.route("/api/oss/tasks")
def oss_tasks():
    return jsonify({"tasks": oss_manager.list()})


@app.route("/api/oss/tasks/<task_id>")
def oss_task(task_id):
    t = oss_manager.get(task_id)
    if t is None:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(t.public())


@app.route("/api/oss/tasks/<task_id>", methods=["DELETE"])
def oss_task_delete(task_id):
    """删除 OSS 任务（记录+任务目录；不动已下载数据）。运行中拒绝。"""
    try:
        deleted = oss_manager.remove(task_id)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    if deleted is None:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({"deleted": deleted})


@app.route("/api/oss/merge", methods=["POST"])
def oss_task_merge():
    """合并重复任务：body {keep, drop}（须为同一交付信息 + 同一目标目录）。"""
    body = request.get_json(silent=True) or {}
    keep = (body.get("keep") or "").strip()
    drop = (body.get("drop") or "").strip()
    if not keep or not drop:
        return jsonify({"error": "需要 keep 与 drop 两个任务 ID"}), 400
    report = oss_manager.merge(keep, drop)
    if report.get("error"):
        return jsonify(report), 409
    return jsonify(report)


@app.route("/api/oss/tasks/<task_id>/log")
def oss_task_log(task_id):
    t = oss_manager.get(task_id)
    if t is None:
        return jsonify({"error": "任务不存在"}), 404
    tail = int(request.args.get("tail", 200))
    text = ""
    if t.log_path.exists():
        lines = t.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        text = "\n".join(lines[-tail:])
    return Response(text, mimetype="text/plain; charset=utf-8")


if __name__ == "__main__":
    print(f"[config] 白名单网段: {sorted(str(n) for n in ALLOWED_NETWORKS)}")
    app.run(host=HOST, port=PORT, threaded=True)
