"""跨任务内容寻址缓存：相同输入的产物在 job 之间硬链接共享，避免重复计算。

两层缓存（指纹 = 输入参数的 sha256，产物 = 硬链接目录树）：
  split      指纹 = sha256(meta.xlsx 字节 + sheet + batch + raw_root)
             产物 = job 的 split/（拆分配置 + amplicon json + 拆分后 fastq）
             命中后 config/amplicons/split 三步由 pipeline 自身的断点逻辑跳过。
  crispresso 指纹 = sha256(split 指纹 + sample_rep + amplicon + guide + score +
                     qw_center + qw_size + ignore_substitutions)
             产物 = res/crispresso_manual/score{N}/{batch}/{sample_rep}/
             命中后 _run_one 的 crispresso_done 检测到量化文件自动 CACHED 跳过。

实现要点：
  - 硬链接树（os.link）：目录结构各 job 独立、文件内容零拷贝共享；恢复时
    amplicons_*.json 除外（pipeline 无跳过逻辑、每次会重写，若硬链接会 truncate
    同一 inode 污染缓存），该文件用 copy2 拷贝。
  - 写回原子性：先写 cache/.tmp_<fp>/ 再 os.rename；并发写回时后者 rename 失败
    则丢弃临时目录（已有缓存可用）。
  - force=true 的 job 不恢复 split 缓存：config/splitfq 会强制重写文件，硬链接
    会连带污染缓存。
"""
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

SPLIT_DIR = "split"
CRISPR_DIR = "crispresso"
QUAL_FILE = "CRISPResso_quantification_of_editing_frequency.txt"

# split 解析逻辑版本号：config 解析/barcode 生成逻辑变更时必须 bump，
# 使旧解析产生的 split 缓存全部失效（避免解析变更后误命中旧产物）
SPLIT_PARSE_VERSION = "20261013_L_COL"

# 恢复 split 缓存时不做硬链接的文件（pipeline 会重写，见模块 docstring）
SPLIT_COPY_EXCLUDE = "amplicons_"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def split_fingerprint(xlsx_path, sheet: str, batch: str, raw_root: str) -> str:
    """split 层指纹：解析版本 + meta.xlsx 内容 + 解析参数（不含 workers/force 等执行参数）。"""
    key = "\x00".join([
        SPLIT_PARSE_VERSION, sha256_file(xlsx_path), str(sheet), str(batch), str(raw_root),
    ])
    return _sha256(key.encode())


def crispresso_fingerprint(split_fp: str, sample_rep: str, amplicon: str, guide: str,
                           score: int, qw_center, qw_size,
                           ignore_substitutions: bool, nw_gap_open=None) -> str:
    """单样本×score 的 crispresso 层指纹（上游数据一致性由 split_fp 保证）。

    None 表示使用 CRISPResso2 原生默认（qw_center=-3 / qw_size=1 / nw_gap_open=-20），
    与显式传入数值视为不同设置（序列化为 "None"）。
    """
    key = "\x00".join([
        split_fp, str(sample_rep), str(amplicon), str(guide or ""),
        str(score), str(qw_center), str(qw_size), str(bool(ignore_substitutions)),
        str(nw_gap_open),
    ])
    return _sha256(key.encode())


def _link_tree(src: Path, dst: Path, copy_prefixes=("amplicons_",)):
    """把 src 目录树硬链接到 dst（dst 父目录须存在）。

    copy_prefixes 匹配的文件名用 copy2（会被 pipeline 重写、不能共享 inode）。
    """
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            s, d = Path(root) / name, dst / rel / name
            if any(name.startswith(p) for p in copy_prefixes):
                shutil.copy2(s, d)
            else:
                if d.exists():
                    d.unlink()
                os.link(s, d)


def _atomic_store(cache_root: Path, fp: str, src: Path) -> bool:
    """把 src 写回 cache_root/<fp>/；返回是否真正写入（已存在则丢弃临时目录）。"""
    dest = cache_root / fp
    if dest.exists():
        return False
    tmp = cache_root / f".tmp_{fp}_{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        _link_tree(src, tmp, copy_prefixes=())   # 缓存内不排除，保证与源一致
        try:
            os.rename(tmp, dest)
            return True
        except OSError:                          # 并发写回：目标已被他人创建
            shutil.rmtree(tmp, ignore_errors=True)
            return False
    except Exception:                            # noqa: BLE001 写回失败不影响任务
        shutil.rmtree(tmp, ignore_errors=True)
        return False


class ResultCache:
    """每个 JobManager 持有一个实例（跨 job 共享缓存目录，无 per-job 状态）。

    命中/写回统计由调用方（Job）自行维护，避免跨 job 串数。
    """

    def __init__(self, cache_dir, enabled: bool = True):
        self.dir = Path(cache_dir) if enabled else None
        if self.dir is not None:
            (self.dir / SPLIT_DIR).mkdir(parents=True, exist_ok=True)
            (self.dir / CRISPR_DIR).mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.dir is not None

    # ---------------- split 层 ----------------
    def split_fingerprint(self, xlsx_path, sheet: str, batch: str, raw_root: str):
        """计算 split 层指纹（幂等、与 force 无关）；缓存禁用时返回 None。"""
        if not self.enabled:
            return None
        return split_fingerprint(xlsx_path, sheet, batch, raw_root)

    def restore_split(self, fp: str, job_split_dir, batch: str) -> bool:
        """本地尚无拆分配置时，从缓存恢复 split/ 目录树；命中并恢复返回 True。"""
        if not self.enabled:
            return False
        job_split_dir = Path(job_split_dir)
        if any((job_split_dir / batch).glob("barcode.json")) or \
           any((job_split_dir / batch).glob("*/barcode.json")):
            return False                                 # 本地已存在，勿覆盖
        cached = self.dir / SPLIT_DIR / fp
        if not cached.is_dir():
            return False
        _link_tree(cached, job_split_dir)
        return True

    def store_split(self, fp: str, job_split_dir) -> bool:
        if not self.enabled:
            return False
        return _atomic_store(self.dir / SPLIT_DIR, fp, Path(job_split_dir))

    # ---------------- crispresso 层 ----------------
    def restore_crispresso(self, split_fp, sample_rep, amplicon, guide, score,
                           qw_center, qw_size, ignore_substitutions, dest_dir,
                           nw_gap_open=None):
        """命中则把缓存目录硬链接恢复到 dest_dir（只读消费）；返回命中的指纹或 None。"""
        if not self.enabled or split_fp is None:
            return None
        fp = crispresso_fingerprint(split_fp, sample_rep, amplicon, guide,
                                    score, qw_center, qw_size,
                                    ignore_substitutions, nw_gap_open)
        cached = self.dir / CRISPR_DIR / fp
        if not cached.is_dir():
            return None
        dest = Path(dest_dir)
        if dest.exists():                        # 本 job 已跑过该样本，无需恢复
            return None
        _link_tree(cached, dest, copy_prefixes=())
        return fp

    def store_crispresso(self, split_fp, sample_rep, amplicon, guide, score,
                         qw_center, qw_size, ignore_substitutions, src_dir,
                         nw_gap_open=None):
        """写回缓存；返回 (指纹, 是否本次真正写入)。"确认已存在"也返回指纹
        （该条目与本 job 关联，删除任务时需一并检查引用）。"""
        if not self.enabled or split_fp is None:
            return None, False
        fp = crispresso_fingerprint(split_fp, sample_rep, amplicon, guide,
                                    score, qw_center, qw_size,
                                    ignore_substitutions, nw_gap_open)
        if _atomic_store(self.dir / CRISPR_DIR, fp, Path(src_dir)):
            return fp, True
        return (fp, False) if (self.dir / CRISPR_DIR / fp).is_dir() else (None, False)


# --------------------------------------------------------------------------- #
# 任务枚举（与 pipeline.crispresso.discover_from_split 对齐的轻量实现）
# --------------------------------------------------------------------------- #
import re as _re


def _strip_rep(sample_rep: str) -> str:
    return _re.sub(r"_Rep\d+$", "", sample_rep)


def iter_crispresso_tasks(job_dir, batch: str, scores):
    """枚举 (sample_rep, amplicon, guide, score, out_dir)。

    barcode.json 兼容单 lane（split/{batch}/）与多 lane（split/{batch}/{lane}/）；
    fastq 存在性不在此校验（缺失样本 pipeline 自身会 SKIP-NFQ）。
    """
    job_dir = Path(job_dir)
    split_dir = job_dir / "split"
    amp_file = split_dir / f"amplicons_{batch}.json"
    if not amp_file.is_file():
        return []
    amp = {}
    with open(amp_file) as fh:
        for ad in json.load(fh):
            amp[ad["sample"]] = ad
    bc_paths = sorted((split_dir / batch).glob("barcode.json")) + \
        sorted((split_dir / batch).glob("*/barcode.json"))
    pat = _re.compile(r"^(.+)_R1\.(fq|fastq)(\.gz)?$")
    seen, tasks = set(), []
    for bc_path in bc_paths:
        lane_dir = bc_path.parent
        with open(bc_path) as fh:
            barcode = json.load(fh)
        for sr in barcode:
            sample = _strip_rep(sr)
            ad = amp.get(sample)
            if not ad or sr in seen:
                continue
            # fastq 实际存在性：lane 目录下 split/ 或 output/ 中 {sr}_R1.*
            fq_ok = False
            for sub in ("split", "output"):
                root = lane_dir / sub
                if not root.is_dir():
                    continue
                if any(pat.match(p.name) and (p.parent / p.name.replace("_R1", "_R2")).exists()
                       for p in root.rglob("*") if p.is_file()):
                    fq_ok = True
                    break
            if not fq_ok:
                continue
            seen.add(sr)
            for sc in scores:
                tasks.append({
                    "sample_rep": sr, "amplicon": ad["sequence"],
                    "guide": ad.get("spacer", ""), "score": int(sc),
                    "out_dir": job_dir / "res" / "crispresso_manual" /
                               f"score{sc}" / batch / sr,
                })
    return tasks


def iter_crispresso_outputs(job_dir):
    """扫描 job 的 crispresso 输出，返回 [(sample_rep, score, out_dir)]（含量化文件）。"""
    job_dir = Path(job_dir)
    root = job_dir / "res" / "crispresso_manual"
    if not root.is_dir():
        return []
    out = []
    for score_dir in sorted(root.glob("score*")):
        m = _re.match(r"^score(\d+)$", score_dir.name)
        if not m:
            continue
        score = int(m.group(1))
        for sample_dir in score_dir.glob("*/*"):
            if sample_dir.is_dir() and any(sample_dir.glob(f"CRISPResso_on_*/{QUAL_FILE}")):
                out.append((sample_dir.name, score, sample_dir))
    return out


# --------------------------------------------------------------------------- #
# 任务删除：仅真正释放"没有被任何其他任务引用"的产物
# --------------------------------------------------------------------------- #
def job_fps_from_dir(job_dir):
    """从 job 目录的 job_info.json 读取缓存指纹；无记录返回 None。"""
    f = Path(job_dir) / "job_info.json"
    if not f.is_file():
        return None
    try:
        with open(f) as fh:
            info = json.load(fh)
        fps = info.get("cache_fps") or {}
        return {"split": fps.get("split"),
                "crispresso": [x for x in fps.get("crispresso", []) if x]}
    except Exception:  # noqa: BLE001
        return None


def _dir_size(job_dir):
    """返回 (表观字节数, 独占字节数, 文件数)。独占 = nlink==1（删除即物理释放）。"""
    apparent = exclusive = nfiles = 0
    for root, _dirs, files in os.walk(job_dir):
        for name in files:
            p = Path(root) / name
            try:
                st = p.stat()
            except OSError:
                continue
            nfiles += 1
            apparent += st.st_size
            if st.st_nlink <= 1:
                exclusive += st.st_size
    return apparent, exclusive, nfiles


def _entry_ref_state(entry: Path):
    """统计缓存条目的引用状态：返回 (文件总数, 无引用文件数[nlink==1])。

    任何 job 通过硬链接引用该文件都会抬高 nlink，故 nlink==1 即"无任何任务引用"。
    """
    total = unref = 0
    for root, _dirs, files in os.walk(entry):
        for name in files:
            try:
                st = (Path(root) / name).stat()
            except OSError:
                continue
            total += 1
            if st.st_nlink <= 1:
                unref += 1
    return total, unref


def prune_entry(entry: Path):
    """若缓存条目完全没有被任何任务引用（所有文件 nlink==1）则删除。

    返回 {"pruned": bool, "freed": int, "files": int, "total": int}。
    """
    entry = Path(entry)
    if not entry.is_dir():
        return {"pruned": False, "freed": 0, "files": 0, "total": 0}
    total, unref = _entry_ref_state(entry)
    if total == 0 or unref < total:              # 存在被引用文件 → 保留
        return {"pruned": False, "freed": 0, "files": unref, "total": total}
    size = sum(st.st_size for root, _d, fs in os.walk(entry)
               for f in fs if (st := (Path(root) / f).stat()))
    shutil.rmtree(entry, ignore_errors=True)
    return {"pruned": True, "freed": size, "files": unref, "total": total}


def safe_delete_job(job_dir, cache_root=None, fps_override=None):
    """删除任务目录，并清理因删除而失去全部引用的缓存条目。

    必须在 rmtree 之前读取指纹、之后检查引用（job 自身硬链接会抬高 nlink）。
    fps_override：内存任务可直接传入 job.cache_fps（未跑 pack 时无 job_info.json）。
    返回删除报告 dict。
    """
    job_dir = Path(job_dir)
    fps = fps_override or job_fps_from_dir(job_dir)
    fps = {"split": fps.get("split"), "crispresso": list(fps.get("crispresso", []))} \
        if fps else None
    apparent, exclusive, nfiles = _dir_size(job_dir)
    shutil.rmtree(job_dir, ignore_errors=True)

    report = {"deleted": job_dir.name, "apparent_bytes": apparent,
              "freed_exclusive_bytes": exclusive, "files": nfiles,
              "cache_checked": 0, "cache_pruned": [], "cache_kept": 0}
    if not cache_root or not fps:
        report["cache_note"] = "无缓存指纹记录（老任务或未启用缓存），未做缓存清理"
        return report
    cache_root = Path(cache_root)
    for layer, key in ((SPLIT_DIR, fps["split"]), (CRISPR_DIR, None)):
        fps_list = [key] if key else fps["crispresso"]
        for fp in fps_list:
            entry = cache_root / layer / fp
            if not entry.is_dir():
                continue
            report["cache_checked"] += 1
            r = prune_entry(entry)
            if r["pruned"]:
                report["cache_pruned"].append(f"{layer}/{fp[:12]}")
            else:
                report["cache_kept"] += 1
    return report


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
