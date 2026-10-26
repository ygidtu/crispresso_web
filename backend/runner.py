"""流水线运行器：隔离地调用 XueChaoYou_WYW/main.py 跑单步/全流程。

每个 job 独占一个目录（{output_root}/{job_id}），内含 config.ini / meta.xlsx / split / res / plots / pack / run.log，
彼此互不干扰。步骤在后台线程中依次执行，状态通过 status() 暴露给前端轮询。

跨任务缓存：相同输入（xlsx/sheet/batch/raw_root + CRISPResso 参数）的产物通过
backend/cache.py 在 job 之间硬链接共享——split 恢复后 config/amplicons/split 由
pipeline 断点逻辑跳过；crispresso 按样本×score 恢复后由 crispresso_done 跳过。
pack 完成后把 config.ini / run.log / job_info.json 追加进 pack.zip，导出即含参数。

历史任务重跑：服务重启后内存任务清空，start 时若磁盘目录存在会从 config.ini
重建内存任务（含 notify_email），因此历史任务也能重新运行。
"""
import configparser
import json
import re
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

from backend import cache as cache_mod
from backend import mailer

STEPS = ["config", "amplicons", "split", "crispresso", "collect", "pack"]

ENV_MAIN_PY = "CRWeb_MAIN_PY"
ENV_LMY_DIR = "CRWeb_LMY_DIR"
ENV_JOBS_ROOT = "CRWeb_JOBS_ROOT"

DEFAULT_MAIN_PY = "/mnt/data1/zhang/projects/XueChaoYou_WYW/main.py"
DEFAULT_LMY_DIR = "/mnt/data1/zhang/projects/XueChaoYou_LMY"
DEFAULT_JOBS_ROOT = str(Path(__file__).resolve().parent.parent / "jobs")


def _env(key, default):
    import os
    return os.environ.get(key, default)


class Job:
    def __init__(self, job_id, cfg, main_py, lmy_dir, jobs_root, cache=None):
        self.id = job_id
        self.cfg = cfg                                  # sheet/batch/raw_root/scores/workers/force/ignore_substitutions/quantification_window_*/output_root/xlsx
        self.main_py = Path(main_py)
        self.lmy_dir = Path(lmy_dir)
        self.dir = Path(jobs_root) / job_id
        self.ini = self.dir / "config.ini"
        self.meta_xlsx = self.dir / "meta.xlsx"
        self.log_path = self.dir / "run.log"
        self.cache = cache                              # ResultCache | None
        self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.cache_stat = {"split_hit": False, "split_local": False,
                           "restored": 0, "total": 0, "stored": 0}  # per-job 统计
        self.cache_fps = {"split": None, "crispresso": []}  # 供删除时定向清理缓存
        self.status = {s: "pending" for s in STEPS}     # pending/running/done/failed
        self.error = {}
        self.thread = None
        self.finished = False
        self._lock = threading.Lock()
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- 配置 ----
    @staticmethod
    def _opt_num(v):
        """None → 空字符串（CRISPResso2 默认）；数值 → 规范化字符串。"""
        return "" if v is None else str(int(v))

    def write_config(self, xlsx_abs_path):
        batch = self.cfg["batch"]
        scores = " ".join(str(int(s)) for s in self.cfg["scores"])
        ignore_subs = str(bool(self.cfg.get("ignore_substitutions", False))).lower()
        self.meta_xlsx.write_bytes(Path(xlsx_abs_path).read_bytes())
        self.ini.write_text(
            "[pipeline]\n"
            f"lmy_dir = {self.lmy_dir}\n"
            f"xlsx = meta.xlsx\n"
            f"sheet = {self.cfg['sheet']}\n"
            f"batch = {batch}\n\n"
            "[inputs]\n"
            f"raw_root = {self.cfg['raw_root']}\n\n"
            "[analysis]\n"
            f"scores = {scores}\n"
            f"workers = {int(self.cfg['workers'])}\n"
            f"force = {str(self.cfg['force']).lower()}\n"
            f"ignore_substitutions = {ignore_subs}\n"
            f"notify_email = {self.cfg.get('notify_email', '')}\n"
            "# 留空 = 使用 CRISPResso2 原生默认（qw_center=-3 / qw_size=1 / nw_gap_open=-20）\n"
            f"quantification_window_center = {self._opt_num(self.cfg.get('quantification_window_center'))}\n"
            f"quantification_window_size = {self._opt_num(self.cfg.get('quantification_window_size'))}\n"
            f"needleman_wunsch_gap_open = {self._opt_num(self.cfg.get('needleman_wunsch_gap_open'))}\n\n"
            "[output]\n"
            "split_dir = split\n"
            "crispresso_dir = res/crispresso_manual\n"
            "plot_dir = plots\n"
            f"amplicons = split/amplicons_{batch}.json\n"
            "efficiency_csv = split/editing_efficiency.xlsx\n"
            "score_xlsx = 编辑效率_with_score.xlsx\n"
            "pack_dir = pack\n"
            "samples = samples_list.csv\n"
            "log = run.log\n",
            encoding="utf-8",
        )

    # ---- 日志/状态 ----
    def log(self, msg):
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] {msg}\n")

    def public_status(self):
        with self._lock:
            return {
                "id": self.id,
                "finished": self.finished,
                "steps": dict(self.status),
                "error": dict(self.error),
                "cfg": {k: self.cfg.get(k) for k in ("sheet", "batch", "raw_root",
                                                     "scores", "workers", "force",
                                                     "ignore_substitutions",
                                                     "quantification_window_center",
                                                     "quantification_window_size",
                                                     "needleman_wunsch_gap_open",
                                                     "notify_email")},
                "cache": self._cache_status(),
            }

    # ---- 执行 ----
    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    _PROGRESS_RE = re.compile(r"^\s*Reading\b")

    def _filter_line(self, line):
        """去 ANSI 颜色码、剔除 splitFq 进度条，保持 run.log 干净。"""
        line = self._ANSI_RE.sub("", line)
        if self._PROGRESS_RE.match(line):
            return None
        return line

    def run_step(self, step, force):
        self.status[step] = "running"
        self.error.pop(step, None)                      # 清除该步上次的旧错误
        cmd = [sys.executable, str(self.main_py), "--config", str(self.ini), "--step", step]
        if force:
            cmd.append("--force")
        self.log(f"RUN  {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(cmd, cwd=str(self.dir),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            with self.log_path.open("a", encoding="utf-8") as lf:
                for line in proc.stdout:
                    filtered = self._filter_line(line)
                    if filtered is not None:
                        lf.write(filtered)
                proc.stdout.close()
            proc.wait()
            if proc.returncode == 0:
                self.status[step] = "done"
                self.log(f"OK   {step} (exit=0)")
            else:
                self.status[step] = "failed"
                self.error[step] = f"main.py 退出码 {proc.returncode}"
                self.log(f"FAIL {step} (exit={proc.returncode})")
        except Exception as exc:  # noqa: BLE001
            self.status[step] = "failed"
            self.error[step] = str(exc)
            self.log(f"ERR  {step}: {exc}")

    def run_steps(self, steps):
        force = bool(self.cfg["force"])
        self.finished = False                           # 重跑时清除上次完成标志
        self.log(f"JOB START steps={steps} force={force}")
        self._maybe_restore_split(force)
        aborted = None
        for step in steps:
            if aborted:
                # 前置步骤失败即中止：后续步骤保持 pending，避免空跑产生无意义结果
                self.status[step] = "pending"
                self.log(f"SKIP {step}（前置步骤 {aborted} 失败，已中止流水线）")
                continue
            if step == "crispresso":
                self._restore_crispresso_cache()
            self.run_step(step, force)
            if step == "split" and self.status["split"] == "done":
                self._store_split_cache(force)
            elif step == "crispresso" and self.status["crispresso"] == "done":
                self._store_crispresso_cache()
            elif step == "pack" and self.status["pack"] == "done":
                self._export_job_info()
            if self.status[step] == "failed":
                aborted = step
                self.log(f"ABORT 步骤 {step} 失败，中止后续步骤: "
                         f"{[s for s in steps[steps.index(step)+1:]]}")
        self.finished = True
        self.log(f"JOB DONE cache={json.dumps(self._cache_status(), ensure_ascii=False)}")
        # 任务完成邮件提醒（pack.zip + run.log 作附件、日志不进正文；mailer 未启用时静默跳过）
        try:
            mailer.mail_job_done(self)
        except Exception as exc:  # noqa: BLE001
            self.log(f"WARN 邮件提醒发送失败: {exc}")

    # ---- 跨任务缓存挂钩 ----
    def _cache_status(self):
        if self.cache is None or not self.cache.enabled:
            return {"enabled": False}
        h = self.cache_stat
        return {"enabled": True,
                "split": "hit" if h["split_hit"] else ("local" if h["split_local"] else "miss"),
                "crispresso": (f"{h['restored']}/{h['total']} restored, "
                               f"{h['stored']} stored")}

    def _maybe_restore_split(self, force):
        """就绪 split 指纹；本地缺拆分配置时尝试从全局缓存恢复。命中后
        config/amplicons/split 三步会被 pipeline 自身的"已存在则跳过"逻辑秒过。
        指纹与 force 无关：force 只强制重跑本地前三步，crispresso 缓存仍有效。"""
        if self.cache is None or not self.cache.enabled:
            return
        try:
            fp = self.cache.split_fingerprint(
                self.meta_xlsx, self.cfg["sheet"], self.cfg["batch"],
                self.cfg["raw_root"])
            if fp is None:
                return
            self.cache_fps["split"] = fp
            split_dir = self.dir / cache_mod.SPLIT_DIR
            has_local = (list((split_dir / self.cfg["batch"]).glob("barcode.json")) or
                         list((split_dir / self.cfg["batch"]).glob("*/barcode.json")))
            if has_local:
                self.cache_stat["split_local"] = True
                self.log("split 产物本地已存在（不覆盖，前 3 步将自动跳过）")
                return
            hit = self.cache.restore_split(fp, split_dir, self.cfg["batch"])
            self.cache_stat["split_hit"] = hit
            self.log("缓存命中: split（已恢复，前 3 步将自动跳过）" if hit
                     else "缓存未命中: split（将正常执行前 3 步）")
        except Exception as exc:  # noqa: BLE001 缓存故障不阻塞任务
            self.log(f"WARN 恢复 split 缓存失败（忽略）: {exc}")

    def _store_split_cache(self, force):
        if self.cache is None or not self.cache.enabled or force:
            return
        try:
            # config 失败/零拆分时产物为空，不能写入缓存（否则污染全局缓存）
            if not (list((self.dir / "split" / self.cfg["batch"]).glob("barcode.json")) or
                list((self.dir / "split" / self.cfg["batch"]).glob("*/barcode.json"))):
                self.log("split 产物为空（无 barcode.json），不写回全局缓存")
                return
            fp = self.cache.split_fingerprint(
                self.meta_xlsx, self.cfg["sheet"], self.cfg["batch"],
                self.cfg["raw_root"])
            if fp and self.cache.store_split(fp, self.dir / "split"):
                self.log("split 产物已写回全局缓存")
        except Exception as exc:  # noqa: BLE001
            self.log(f"WARN 写回 split 缓存失败（忽略）: {exc}")

    def _restore_crispresso_cache(self):
        """crispresso 步骤前：逐样本×score 恢复命中的输出目录。"""
        if self.cache is None or not self.cache.enabled:
            return
        try:
            fp = self.cache.split_fingerprint(
                self.meta_xlsx, self.cfg["sheet"], self.cfg["batch"],
                self.cfg["raw_root"])
            tasks = cache_mod.iter_crispresso_tasks(
                self.dir, self.cfg["batch"], self.cfg["scores"])
            self.cache_stat["total"] = len(tasks)
            self.log(f"crispresso 缓存检查: {len(tasks)} 个样本×score 组合")
            for t in tasks:
                rfp = self.cache.restore_crispresso(
                    fp, t["sample_rep"], t["amplicon"], t["guide"], t["score"],
                    self.cfg.get("quantification_window_center"),
                    self.cfg.get("quantification_window_size"),
                    self.cfg.get("ignore_substitutions", False),
                    t["out_dir"],
                    nw_gap_open=self.cfg.get("needleman_wunsch_gap_open"))
                if rfp:
                    self.cache_stat["restored"] += 1
                    self.cache_fps["crispresso"].append(rfp)
            h = self.cache_stat
            self.log(f"crispresso 缓存命中: {h['restored']}/{h['total']}"
                     f"（命中样本将被 pipeline 自动跳过）")
        except Exception as exc:  # noqa: BLE001
            self.log(f"WARN 恢复 crispresso 缓存失败（忽略）: {exc}")

    def _store_crispresso_cache(self):
        if self.cache is None or not self.cache.enabled:
            return
        try:
            fp = self.cache.split_fingerprint(
                self.meta_xlsx, self.cfg["sheet"], self.cfg["batch"],
                self.cfg["raw_root"])
            for sample_rep, score, out_dir in cache_mod.iter_crispresso_outputs(self.dir):
                tasks = cache_mod.iter_crispresso_tasks(self.dir, self.cfg["batch"], [score])
                meta = next((t for t in tasks if t["sample_rep"] == sample_rep), None)
                if meta is None:
                    continue
                sfp, written = self.cache.store_crispresso(
                    fp, sample_rep, meta["amplicon"], meta["guide"], score,
                    self.cfg.get("quantification_window_center"),
                    self.cfg.get("quantification_window_size"),
                    self.cfg.get("ignore_substitutions", False),
                    out_dir,
                    nw_gap_open=self.cfg.get("needleman_wunsch_gap_open"))
                if sfp:
                    if sfp not in self.cache_fps["crispresso"]:
                        self.cache_fps["crispresso"].append(sfp)
                if written:
                    self.cache_stat["stored"] += 1
            self.log(f"crispresso 新产物写回缓存: {self.cache_stat['stored']} 个")
        except Exception as exc:  # noqa: BLE001
            self.log(f"WARN 写回 crispresso 缓存失败（忽略）: {exc}")

    def _export_job_info(self):
        """pack.zip 追加参数/日志/编辑效率表：导出结果即自带可追溯信息与核心结果。

        采用「重建式替换」写入（同一 arcname 只保留最新一份），避免重复追加导致 zip 膨胀。
        """
        info = {
            "job_id": self.id, "created_at": self.created_at,
            "steps_status": dict(self.status), "cfg": self.cfg,
            "cache": self._cache_status(),
            "cache_fps": self.cache_fps,   # 删除任务时定向清理缓存用
        }
        try:
            (self.dir / "job_info.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
            zip_path = self.dir / "pack.zip"
            if zip_path.is_file():
                entries = {}
                with zipfile.ZipFile(zip_path) as zin:
                    for item in zin.infolist():
                        entries[item.filename] = zin.read(item.filename)

                def put(src, arcname):
                    p = Path(src)
                    if p.is_file():
                        entries[arcname] = p.read_bytes()

                put(self.dir / "job_info.json", "job_info.json")
                put(self.ini, "config.ini")
                if self.log_path.is_file():
                    put(self.log_path, "run.log")
                # 编辑效率表（核心结果）
                put(self.dir / "编辑效率_with_score.xlsx", "编辑效率_with_score.xlsx")
                for name in ("editing_efficiency.xlsx", "editing_efficiency_summary.xlsx"):
                    put(self.dir / "split" / name, name)

                tmp = zip_path.with_suffix(".zip.tmp")
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                    for name, data in entries.items():
                        zout.writestr(name, data)
                tmp.replace(zip_path)
                self.log("pack.zip 已写入 job_info.json / config.ini / run.log / 编辑效率表")
        except Exception as exc:  # noqa: BLE001
            self.log(f"WARN 导出参数信息失败（忽略）: {exc}")


class JobManager:
    def __init__(self, main_py, lmy_dir, jobs_root, cache_dir=None, cache_enabled=True):
        self.main_py = Path(main_py)
        self.lmy_dir = Path(lmy_dir)
        self.jobs_root = Path(jobs_root)
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.cache = cache_mod.ResultCache(cache_dir, enabled=cache_enabled) \
            if cache_enabled else None
        self._jobs = {}
        self._lock = threading.Lock()

    def _next_id(self):
        """job_<秒级时间戳>；同秒创建/磁盘已有同名校验，+1 递增保证唯一。"""
        n = int(time.time())
        while True:
            jid = f"job_{n}"
            if jid not in self._jobs and not (self.jobs_root / jid).exists():
                return jid
            n += 1

    def create(self, cfg, xlsx_abs_path):
        with self._lock:
            job = Job(self._next_id(), cfg, self.main_py, self.lmy_dir,
                      str(self.jobs_root), cache=self.cache)
            self._jobs[job.id] = job
        job.write_config(xlsx_abs_path)
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def list(self):
        with self._lock:
            return [j.public_status() for j in sorted(self._jobs.values(), key=lambda j: j.id)]

    def start(self, job_id, steps=None):
        job = self.get(job_id)
        if job is None:
            # 服务重启后内存任务清空：磁盘目录存在时从 config.ini 重建，使历史任务可重跑
            cfg = self._disk_cfg(job_id)
            if cfg is not None:
                with self._lock:
                    job = Job(job_id, cfg, self.main_py, self.lmy_dir,
                              str(self.jobs_root), cache=self.cache)
                    self._jobs[job_id] = job
                job.write_config(job.dir / "meta.xlsx")
        if job is None:
            return None
        if steps is None:
            steps = STEPS
        job.thread = threading.Thread(target=job.run_steps, args=(steps,), daemon=True)
        job.thread.start()
        return job.public_status()

    def _disk_cfg(self, job_id):
        """从磁盘任务的 config.ini 重建 cfg（供 start 重跑历史任务）；无则 None。"""
        ini = self.jobs_root / job_id / "config.ini"
        if not ini.is_file():
            return None
        cp = configparser.ConfigParser()
        cp.read(ini, encoding="utf-8")

        def g(section, key, default=None):
            v = cp.get(section, key, fallback="")
            v = v.strip()
            return v if v else default

        try:
            scores = [int(x) for x in str(g("analysis", "scores", "25")).split()]
        except ValueError:
            scores = [25]

        def opt_int(key, default=None):
            v = g("analysis", key)
            try:
                return int(v) if v is not None else default
            except ValueError:
                return default

        return {
            "sheet": g("pipeline", "sheet", ""),
            "batch": g("pipeline", "batch", "yw4"),
            "raw_root": g("inputs", "raw_root", ""),
            "scores": scores,
            "workers": int(g("analysis", "workers", "16") or 16),
            "force": str(g("analysis", "force", "false")).lower() == "true",
            "ignore_substitutions": str(g("analysis", "ignore_substitutions", "false")).lower() == "true",
            "quantification_window_center": opt_int("quantification_window_center"),
            "quantification_window_size": opt_int("quantification_window_size"),
            "needleman_wunsch_gap_open": opt_int("needleman_wunsch_gap_open"),
            "notify_email": g("analysis", "notify_email", "") or "",
            "xlsx": str(self.jobs_root / job_id / "meta.xlsx"),
        }

    def delete(self, job_id):
        """删除任务：内存/磁盘任务均可删；运行中拒绝。

        产物以硬链接与全局缓存共享，删除目录只解除本任务的链接；
        缓存条目若因此失去全部引用（文件 nlink 全为 1）则一并清理，真正释放空间。
        返回删除报告 dict；任务不存在返回 None。
        """
        with self._lock:
            job = self._jobs.pop(job_id, None)
        d = self.jobs_root / job_id
        if job is None and not d.is_dir():
            return None
        if job is not None and job.thread is not None and job.thread.is_alive():
            with self._lock:                     # 放回，保持可查询
                self._jobs[job_id] = job
            return {"error": "任务正在运行，无法删除"}
        cache_root = self.cache.dir if (self.cache and self.cache.enabled) else None
        fps_override = dict(job.cache_fps) if job is not None else None
        try:
            return cache_mod.safe_delete_job(d, cache_root=cache_root,
                                             fps_override=fps_override)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"删除失败: {exc}"}
