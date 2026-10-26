# CRISPResso2 分析平台 — 说明文档

> **维护约定：每次修改代码前，先更新本文档（改「当前任务 / TODO」），完成后再回写状态。**
> 最后更新：2026-09-24

---

## 1. 项目概述

内网部署的 CRISPResso2 分析 Web 平台：上传 metainfo Excel → 从阿里云 OSS 下载数据 → 指定原始 fastq 路径 → 流水线分析（config→amplicons→split→crispresso→collect→pack）→ 在线查进度、下载打包结果。

- 技术栈：Flask 3.x（后端）+ Vue3 + Naive UI 2.45.3 + Vite 6（前端），uv 管理依赖
- 部署形态：**仅公司内网**，无域名/HTTPS；安全靠 IP 白名单 + 信封加密传输凭据
- 运行目录：`/mnt/data1/zhang/projects/crpresso_web`

## 2. 目录结构

```
crpresso_web/
├── app.py                  # Flask 后端入口（全部 REST 端点 + IP 白名单 + config.ini 加载）
├── config.ini              # Web 服务配置：[server] host/port、[security] allowed_ips、[oss] ossutil_bin
├── run.sh                  # 启动脚本（export 环境变量 → uv run python app.py）
├── pyproject.toml / uv.lock
├── .gitignore              # 已忽略 .secrets/、oss_tasks/、jobs/、uploads/
├── backend/
│   ├── db.py               # SQLite 持久层（peewee）：oss_tasks 表，任务/日志元数据落盘
│   ├── keys.py             # RSA 密钥管理：生成/加载 .secrets/oss_rsa_private.pem(600)、公钥 JWK、RSA-OAEP-SHA256 解密
│   ├── oss.py              # OSS 下载：信封解密、解析4行交付信息、写 ~/.ossutilconfig、OssTask/OssManager（含持久化/去重/续传）
│   ├── mailer.py           # 邮件通知（config.ini [mail]）：OSS 下载完成提醒、分析任务完成提醒（日志正文 + pack.zip 附件）
│   ├── inspect.py          # Excel 探测：list_sheets / sheet_preview / check_sheet(s)（严格列校验）
│   ├── runner.py           # 流水线运行器：Job/JobManager（每 job 独立目录+后台线程+日志+缓存挂钩）
│   └── cache.py            # 跨任务缓存：split/crispresso 两层内容寻址缓存（指纹+硬链接树，见 §3.6）
├── frontend/
│   ├── src/AppBody.vue     # 主 UI（5 张卡片，见 §5）
│   ├── src/App.vue         # 壳（NConfigProvider/NMessageProvider）
│   ├── src/api.js          # fetch 封装，BASE='/api'
│   ├── public/sample_meta.xlsx  # yw4 示例 Excel（构建时复制进 dist）
│   └── dist/               # 构建产物（Flask 直接 serve，改前端只需 npm run build）
├── jobs/                   # 每个分析任务一个目录（config.ini/meta.xlsx/split/res/plots/pack.zip/run.log/job_info.json）
├── cache/                  # 跨任务共享缓存（split/ + crispresso/ 指纹目录，硬链接共享产物；config.ini [cache] 可关闭/改路径）
├── oss_tasks/              # OSS 下载任务日志（oss_<ts>/download.log）
├── uploads/                # 上传的 Excel 暂存
├── crpresso.db             # SQLite 任务持久化（运行期生成，已 .gitignore）
└── .secrets/               # RSA 私钥（绝不入库）
```

外部依赖（不在本仓库）：
- `/mnt/data1/zhang/projects/XueChaoYou_WYW/main.py` — 流水线入口（`--config xxx.ini --step <step>`）
- `/mnt/data1/zhang/projects/XueChaoYou_LMY/` — pipeline 包（config.py 的 RECIPES、amplicons.py 等）
- `~/.local/bin/ossutil` — ossutil **v2**（配置格式 `[default]` + `region`，勿用 v1 的 `[Credentials]`/`server`）

## 3. 后端设计

### 3.1 请求处理链
`before_request` IP 白名单校验 → 路由 → JSON 响应。

- 白名单来自 `config.ini [security] allowed_ips`（逗号/空白分隔；`.0` 结尾自动按 `/24`；回环 127.0.0.0/8、::1 恒放行）
- 非法条目启动时打印忽略；白名单外返回 403
- ⚠️ 已知局限：同机自连（无论目的 IP 是什么）内核走 lo，remote_addr 恒为 127.0.0.1，无法在本机模拟"白名单外"测试

### 3.2 端点清单

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` 、`/<path>` | serve 前端 dist（每次请求现读文件，前端改动无需重启） |
| GET | `/api/health` | 健康检查 + 基本信息 |
| GET | `/api/paths?prefix=` | 目录补全：prefix 以 `/` 结尾→列该目录**全部**子目录；否则按 basename 前缀匹配；只返回目录（带尾 `/`） |
| POST | `/api/preview` | 上传 Excel → 逐 sheet 严格校验（REQUIRED_COLS + B5/B7 引物检出）→ 全不合格 400 |
| POST | `/api/jobs` | 创建分析任务（multipart：xlsx + sheet/batch/raw_root/scores/workers/force/ignore_substitutions/quantification_window_center/quantification_window_size/needleman_wunsch_gap_open；三个数值参数留空 = CRISPResso2 原生默认） |
| POST | `/api/jobs/<id>/start` | 启动（可 `{"steps":"config,amplicons"}` 选跑） |
| GET | `/api/jobs` / `/api/jobs/<id>` | 列表 / 状态（**内存优先，磁盘回退**：重启后 `from_disk:true`，steps 由产物推断） |
| GET | `/api/jobs/<id>/log?tail=` | 运行日志（内存/磁盘） |
| GET | `/api/jobs/<id>/download?path=` | 下载任务内文件 |
| GET | `/api/jobs/<id>/pack` | 下载 pack.zip |
| DELETE | `/api/jobs/<id>` | 删除任务 |
| GET | `/api/oss/pubkey` | RSA 公钥 JWK（alg=RSA-OAEP-256，供前端 Web Crypto） |
| POST | `/api/oss/download` | body `{ek,iv,data,raw_root,owner,description}`（base64url 信封密文 + 明文登记字段）→ 校验必填（等级负责人/数据情况说明，缺一 400）→ 解密/解析/写配置 → `submit()` 去重提交（响应附 `action`：created/running/done/resumed） |
| GET | `/api/oss/tasks` 、`/tasks/<id>` 、`/tasks/<id>/log` | OSS 下载任务列表/状态/日志 |
| DELETE | `/api/oss/tasks/<id>` | 删除 OSS 任务（记录+任务目录，不动已下载数据；运行中 409） |
| POST | `/api/oss/merge` | body `{keep,drop}` 合并重复任务（须同一交付信息+目标目录；keep 运行中 409） |

### 3.3 磁盘回退（历史任务查询）
服务重启后内存任务清空；`_disk_job_dir(id)` 校验 `jobs/<id>/` 存在后：
- `_disk_steps(dir)`：按产物推断 6 步状态（barcode→config、amplicons_*.json→amplicons、files.json→split、res/→crispresso、效率表/plots→collect、pack.zip→pack）
- `_disk_job_status`：从 `config.ini` 回读 cfg（scores 拆成数组、workers 转 int）
- log/download/pack 同样支持磁盘目录

### 3.4 backend/oss.py（OSS 下载）
- **服务启动自动续传**：`OssManager(resume_on_start=True)`（config.ini `[oss] resume_on_start`，默认 true）启动时扫描被中断任务（`status=running` 但下载线程随上次进程消失）并自动 `RESUME` 续传；同一交付信息只有多个残留时**按创建时间最早恢复一个**，其余保留待合并/删除，避免重复下载；`failed` 任务不自动恢复（防配置错误反复重试，等用户重新提交走 resumed）
- **任务去重 + 提交续传**：`OssManager.submit()` 按 `source_key = oss_path|endpoint|target` 去重（相同交付信息且同一目标目录 ⇒ 同一任务），返回 `action`：
  - `created` 新任务；`running` 相同任务仍在下载（不重复启动，仅更新登记信息）；`done` 相同数据此前已下载完成（不重复下载，仅更新登记信息）；`resumed` 任务 failed 或中断 → **复用同一任务 ID 续传**
  - 去重索引 `_by_source` 内存维护、启动时从 DB 重建；`id` 同秒冲突自动 +1
  - ossutil 命令带 `--ignore-existing`（跳过目标已完整文件）与 `--checkpoint-dir <task>/ossutil_checkpoint`（大文件中断分片续传），日志以 `RESUME`/`RUN` 区分；实测续传时 `skipped:(13 files,6.321 GiB)` 只下剩余部分
  - ⚠️ **ossutil 目标目录语义**：`ossutil cp -rf oss://bucket/PREFIX DEST/` 会在 DEST 下自动创建"PREFIX 最后一段"的同名目录（dry-run 证实）。因此命令传 `raw_root`（数据目录 `target` 的父目录）而非 `target`，结果正好落在 `target = <raw_root>/<对象名>/`（避免多一层 `<raw_root>/<对象名>/<对象名>/`）；`_ossutil_running` 以命令中的 `oss://` 源路径匹配（目的目录可能被多任务共用，不唯一）
  - 提交时 `owner`/`description` 非空则覆盖更新（登记信息可随时修改）
- **删除 / 合并**：`remove(id)` 删任务记录+任务目录（日志/checkpoint），**不动已下载数据目录**，运行中拒绝；`merge(keep, drop)` 仅允许同 `source_key` 的两个任务合并（owner/description 向 keep 补齐后删 drop）
- **进度展示**：`OssTask.read_progress()` 解析 download.log 尾部 ossutil 进度行，`public()` 返回 `progress={percent,total,done,skipped,speed}`；percent 按 `(已完成+已跳过)/总量` 计算真实累计进度（ossutil 自身的 % 是"本次运行"进度，续传时会重置/倒退，如重启后 42%→0%，故弃用）；前端查询结果用 NProgress 进度条 + 文字（已下/跳过/总/速度），任务列表项显示百分比
- **登记限定条件**：提交时必填 `owner`（等级负责人）与 `description`（数据情况说明），后端缺失返回 400；两者写入 `oss_tasks` 表（owner/description 列）持久化，并写进 download.log 首行留痕；`OssTask.public()` 与任务查询接口返回这两项（前端卡片④/⑤展示）
- **老库迁移**：`db.init_db()` 对已存在的 `oss_tasks` 表按 PRAGMA table_info 检查缺列并自动 `ALTER TABLE ADD COLUMN`（TEXT DEFAULT ''），无需手工迁移
- **信封加密双路径**：`crypto.subtle` 仅安全上下文(HTTPS/localhost)可用——内网 `http://IP:5000` 访问会报 `Cannot read properties of undefined (reading 'importKey')`。现检测后自动降级 node-forge（纯 JS AES-256-GCM + RSA-OAEP-SHA256），两条路径输出结构一致（ek=RSA 加密的 AES JWK JSON、data=ct||tag），后端 `decrypt_payload` 无需改动；已验证 forge 加密→cryptography 解密往返一致
- `decrypt_payload({ek,iv,data})`：信封解密——RSA-OAEP-SHA256 解出 AES-256 密钥(JWK JSON) → AESGCM 解密正文
- `parse_oss_info(text)`：按行解析 4 行交付信息（兼容中英文标签、全/半角冒号），缺字段/非 oss:// 报错
- `region_from_endpoint()`：`http://oss-cn-shanghai.aliyuncs.com` → `cn-shanghai`
- `write_ossutil_config(creds)`：写 **v2 格式** `[default] accessKeyID/accessKeySecret/region`；旧文件自动备份 `.bak`；写后显式 `chmod 600`
- `OssTask.run`：`ossutil cp -rf oss://... <raw_root>/<对象名>/ -e <endpoint> --config-file ~/.ossutilconfig`，stdout 逐行写日志
- `OssManager`：内存任务表 + daemon 线程；⚠️ 任务列表仅在内存（重启后旧 oss 任务 ID 404，但 ossutil 子进程不受影响、日志在 oss_tasks/ 落盘）

### 3.5 backend/mailer.py（邮件通知）与 keys.py（RSA）
- **mailer.py**：`config.ini [mail]`（enabled/host/port/user/password/to，to 支持逗号分隔多人）——
  - `mail_oss_done(task)`：OSS 下载完成/失败提醒（来源/目标/负责人/数据说明/错误 + 日志尾部），`OssTask.run()` 结束时触发
  - `mail_job_done(job)`：分析任务完成/失败提醒——**运行日志作正文**（尾部 400 行，超 512KB 截断并注明）、**pack.zip 作附件**（>45MB 不发附件改为提示平台下载），`JobManager.run_steps` 结束时触发
  - 发送失败仅记日志（`邮件发送失败(...)`)，不影响任务主流程；`enabled=false` 整体关闭
  - ⚠️ 授权码明文存于 config.ini `[mail] password`，该文件勿外传/勿入库
- **keys.py**：
- RSA-2048，私钥 `.secrets/oss_rsa_private.pem`（首次访问自动生成，0600）
- `get_public_jwk()`：`{kty:RSA, n, e, alg:"RSA-OAEP-256", use:"enc"}`——`alg` 必须与前端 importKey 的 hash 匹配，否则 DataError
- `decrypt_token(b64url)`：OAEP-SHA256 解密

### 3.6 backend/cache.py（跨任务缓存）
相同输入的任务自动复用产物，避免重复拆分 fastq / 重复跑 CRISPResso；job 目录结构不变（隔离与导出逻辑不受影响）。

- **两层指纹与产物**
  - split：`sha256(meta.xlsx 字节 + sheet + batch + raw_root)` → 产物 = job 的 `split/`（配置+amplicon json+拆分 fq）。命中后 config/amplicons/split 三步由 pipeline 自身断点逻辑跳过
  - crispresso：`sha256(split 指纹 + sample_rep + amplicon + guide + score + qw_center + qw_size + ignore_substitutions + nw_gap_open)` → 产物 = `res/crispresso_manual/score{N}/{batch}/{sample_rep}/`。命中后 `_run_one` 的 crispresso_done 检测到量化文件自动 CACHED
  - ⚠️ 2026-09-26 起三个 CRISPResso 参数支持 None（=CRISPResso2 原生默认 qw_center=-3 / qw_size=1 / nw_gap_open=-20，序列化为 "None" 参与指纹）；此前以硬编码 -1/5/-30 运行的旧缓存条目与新指纹不匹配（会重算而非复用，属预期），删除旧任务时按旧 job_info.json 指纹查找会 miss、仅删目录
- **实现**：硬链接树（`os.link`）——目录结构各 job 独立可写，文件内容零拷贝共享；写回先写 `.tmp_*` 再原子 `os.rename`，并发写回失败方自动丢弃
- **例外**：恢复 split 时 `amplicons_*.json` 用 copy2 而非硬链接（pipeline 无该步跳过逻辑、每次重写，硬链接会 truncate 同一 inode 污染缓存）；force=true 的 job 不恢复/不写回 split 缓存（config/splitfq 会强制重写文件）
- **语义**：指纹与 force 无关——force 只强制重跑本地前三步，crispresso 缓存仍命中；job 分批跑步骤（split 本地已有）时只算指纹不恢复，crispresso 缓存照常生效
- **导出**：pack 完成后 runner 把 `job_info.json`（参数+步骤状态+缓存命中统计）/`config.ini`/`run.log` 追加进 pack.zip，导出 zip 即自带参数追溯
- **配置**：`config.ini [cache] enabled=true / cache_dir=<项目根>/cache`（均可省略用默认）；关闭后行为与旧版完全一致
- **前端**：卡片③任务详情展示缓存状态（`cache.split` / `cache.crispresso`），随任务状态轮询刷新
- ⚠️ 边界：上游 fastq 在 raw_root 内被原地变更而 xlsx/参数不变时指纹不变，会命中旧缓存——此类数据变更应换目录或手动清 cache/

## 4. 前端设计（AppBody.vue）

### 4.1 naive-ui 关键经验（踩坑记录，勿回退）
- **表单控件绑定**：显示读 `value` prop、更新走 `update:value` 事件（组件直接调 `props["onUpdate:value"]`，不是 emit）→ 统一 `:value="form.x"` + `@update:value="form.x=$event"`；**不要用 v-model**（v-model 绑 model-value 显示不出初始值）
- 显式 `@update:value` 会覆盖 v-model 的 setter → handler 里必须自行写回 form
- **NAutoComplete**：选中后 `canBeActivatedRef=false` 菜单关闭；用暴露的 `handleInput(v)`（ref）重新激活；`onRawValue` 用 `lastProcessed` 防止 handleInput→onRawValue→handleInput 死循环
- **NUpload**：选中文件时 `data.file.raw` 为 null，真实 File 在 `data.file.file` → `(raw || file)`
- JWK `alg` 与 `importKey` hash 不一致 → DataError（曾致提交静默失败）

### 4.2 五张卡片（三标签页组织）
页面顶部 `NTabs` 分三页：

- **分析任务**：左侧卡片 ①（上传配置），右侧纵向堆叠卡片 ②（任务列表，在上）+ ③（任务详情，在下）
- **OSS 数据下载**：卡片 ④，独立整页放大
- **任务查询**：卡片 ⑤，独立整页放大

| # | 卡片 | 功能 |
|---|---|---|
| ① | 上传 metainfo & 配置 | Excel 上传+严格校验（不合格 sheet 红标列出并自动排除）、示例下载、sheet 多选、raw_root（**下拉选 run 文件夹**，自动探测 fastq 父级，见 §4.3）、scores 多选(25-50)、workers(32)、force、ignore_substitutions(NSwitch)、**CRISPResso 对齐/定量参数三件套（qw_center/qw_size/nw_gap_open，均可留空=CRISPResso2 原生默认 -3/1/-20；「Cas9 参数一键填充」按钮填 qw_center=-3+qw_size=10，「重置为 CRISPResso2 默认」清空三者）**、创建并运行 |
| ② | 任务列表 | 内存任务实时列表（右侧列上部），点击进 ③ |
| ③ | 进度+日志 | 任务列表正下方（右侧列下部）；6 步骤卡片、下载 CSV/Excel/pack.zip、**任一步骤 failed 时出现「下载错误日志（run.log）」**、运行日志轮询 |
| ④ | OSS 数据下载 | **登记限定条件：等级负责人 + 数据情况说明（均必填，前端校验 + 后端 400）**；textarea 粘贴 4 行交付信息 → 信封加密提交 → 状态标签+**进度条（百分比/已下/跳过/总量/速度，续传时按累计进度显示不回退）**+登记信息展示+日志轮询；下载完毕出现「用刚下载的数据创建并运行」→ 自动切到「分析任务」页；**下方另设「OSS 下载任务查询」区（ID 查询 + 进度标签 + 负责人/说明展示 + 最近任务列表（含 ID/进度/删除），点击即查/下日志）**；下载到 `<raw_root>/<对象名>/`；登记信息持久化（oss_tasks 表 owner/description 列）并在 download.log 头部留痕 |
| ⑤ | 任务查询 / 下载 | 输入任务 ID → 查询（支持重启后历史任务，自动 2s 轮询直到完成）→ 下载 zip/CSV/Excel；**查询到 failed 时同样提供「下载错误日志（run.log）」** |

### 4.3 raw_root 下拉 + fastq 父级自动探测
- 数据根目录 `config.ini [data] runs_root`（默认 `/mnt/nas/数据备份/WangYiWen_TnSeq/`）
- `GET /api/runs`：一级子文件夹（run），按 mtime 倒序；`GET /api/runs/fastq_root?dir=<run>`：L0(`dir/*/*.fq.gz`)→L1(一级子目录)→L2(二级子目录) 逐层探测满足 `{root}/*/*.fq.gz` 的 root（yw4 files.json 的 glob），即 fastq 父级
- 前端 NSelect 选中 run → 自动填充 `form.raw_root` 并显示「N 个样本目录」；`dir` 参数限 runs_root 内（403 防目录遍历）
- OSS 下载基座与此解耦：固定用 runs_root（下载到 `<runs_root>/<对象名>/`），避免数据下进 RawData
- 实测：选中 `20260908_E260908002_U6347_RELE` → 探测出 `.../RawData/`（1 个样本目录）→ 创建任务 config/amplicons 通过

## 5. OSS 信封加密方案

```
前端                                     后端
─────────────────────────────           ─────────────────────────────
GET /api/oss/pubkey → JWK
random AES-256 key + IV(12B)
cipher = AES-GCM(text, key)       ──▶   RSA 解 ek → AES key
ek = RSA-OAEP-SHA256(AES jwk)     ──▶   AESGCM(key).decrypt(iv, data)
POST {ek, iv, data, raw_root}           parse_oss_info → 写 ~/.ossutilconfig
                                        后台 ossutil cp -rf
```
- 明文不限长（AES 承载）；私钥只在服务端 `.secrets/`
- ⚠️ 提交过的 accessKeySecret 已在对话/文档外泄，**建议尽快到阿里云控制台轮换**

## 6. 开发/运维备忘

- 前端改动：`cd frontend && npm run build`（Flask 现读 dist，无需重启）
- 后端改动：需重启（无 reloader）。重启用**独立短命令**，勿与 pkill/heredoc 混在一行（会自杀/超时）：
  `pkill -f "python app[.]py"` 单独执行 → `cd crpresso_web && setsid uv run python app.py >/tmp/crw_server.log 2>&1 < /dev/null &`
- ossutil 在 `/home/zym/.local/bin/ossutil`（PATH 已含）
- 依赖新增用 `uv add <pkg>`（已装 cryptography 50.0.1）
- Playwright（/tmp/pwtest）：定位 naive-ui 选项用 `p.locator('.n-base-select-option').filter({hasText:...})`；坐标点击不可靠；`$(curl ...)` 命令替换会截尾换行导致 md5 误报，比对文件请 `curl -o` 落盘
- cryptography 50.x API：`RSAPublicNumbers(e, n)`（注意顺序）、OAEP 在 `asymmetric.padding`、AESGCM 在 `ciphers.aead`
- 已知限制：同机无法模拟白名单外 IP 测试；oss 任务表在内存（重启后旧 ID 404）；Flask dev server（生产建议 gunicorn）

## 7. 当前任务 / TODO

### 进行中
（无）

### 待办
- [ ] OSS 任务表持久化（SQLite/peewee：`backend/db.py` 的 `oss_tasks` 表，重启后旧 oss_ 任务仍可查；见 §8）
- [x] OSS 下载完成事件联动：下载完毕提示可直接「创建并运行」（raw_root 指向新数据）
- [ ] IP 白名单生效性测试（需另一台非白名单内网机器发请求验证 403）
- [x] `run.sh` 提示语更新为从 config.ini 读取端口
- [ ] 生产化：gunicorn + 常驻（systemd 用户单元）
- [x] 密钥轮换提醒处理：换新 accessKeySecret 后重新走 OSS 下载登记即可

### 已完成（近期）
- [x] pack.zip 补齐编辑效率表（用户反馈缺失）：`_export_job_info` 改为「重建式替换」写入 `编辑效率_with_score.xlsx` / `editing_efficiency.csv` / `editing_efficiency_summary.csv`（同时消除重复追加条目导致的 zip 膨胀）；已对 batch6（job_1790646396）重新打包并邮件发送（含 523 图 + 样本清单 + 效率表 + 参数/日志）；20261001（job_1791643705）因 Sample 列未修正仍无结果，待用户修正表后重跑
- [x] 流水线失败阻断机制（job_1791643705 排查引出）：① `runner.run_steps` 某步骤失败即中止后续（剩余步骤保持 pending 并记 `SKIP/ABORT` 日志，不再空跑）；② split 产物为空（无 barcode.json）不写回全局缓存（修复空产物污染缓存）；③ `splitfq` 0 组 / `crispresso` 0 任务 / `collect` 0 结果均改为非零退出阻断。根因：用户表 Sample 列填 `Sample1-1…Sample3-3`，实际数据目录为 `L1-1…L3-3`，9 个 Sample 全部映射不上被 config 校验拦截（校验按设计工作）
- [x] 邮件通知功能（backend/mailer.py + config.ini [mail]）：OSS 下载完成/失败提醒、分析任务完成/失败提醒（参数+步骤状态+运行日志为正文、pack.zip 为附件，>45MB 改为平台下载提示）；发件 zhym1992@foxmail.com（smtp.qq.com 465）→ 收件 zhangyiming@ihcams.ac.cn；发送失败不影响主流程；真实发送测试与端到端（含附件）验证通过
- [x] 修复字符串样本 ID 导致的 collect/pack 失败：`XueChaoYou_WYW/main.py::ensure_samples` 与 `pipeline/plot.py::pack_cmd` 原假设 id 为整数（`int(df["id"].max())`、`int(r["id"])`、`^\d+_\d+\.pdf$`），新表 ID 为字符串（如 `pcsk9-ST1_P1`）即崩；ensure_samples 改为「与已有 id 不冲突的自动编号」（已有数字 id 从其最大值续），pack 用 `str(id)` 与放宽正则 `^(.+)_(\d+)\.pdf$`；已对 job_1790646396 补跑 collect/pack 验证通过
- [x] 前端两项体验修复：① 任务详情在完成后不再自动清空 `selectedId`（原 `poll()` finished 时置 null 导致“几秒后详情消失”）；② 失败任务也显示「重新运行（补跑失败步骤）」按钮（原 `finished` 后按钮隐藏、失败无法重跑）；③ 步骤错误文本仅在 `steps[s]==='failed'` 时显示（避免重跑成功后旧错误残留）；后端 `run_step` 重跑时清除该步旧 error
- [x] yw 表单新增 Sample 列映射（`pipeline/config.py::_parse_yw_sample` + `amplicons.py::parse_yw4`）：Sample=原始数据目录名——多值时按 Sample 分 lane（files.json 只取 `<raw_root>/<Sample>/*.fq.gz`，一个目录内含多样品由 B5引物/B7引物 组合经 splitFq 拆分），单值/缺列时沿用 `*/*.fq.gz` 通配；barcode 取 B5引物/B7引物（`_primer_col` 自动识别序列列）；输出样本名 ID 唯一则优先 ID，否则多 Sample 用「样品名_Sample」、单 Sample 用「样品名」，且 config 与 amplicons 命名严格一致（修复 P1/P2 同名样品 `seen` 去重互相覆盖）；映射不上的 Sample warning 跳过、全部映射不上 SystemExit。实测 P1 拆分 11 样品各 100% barcode 命中- [x] 修复 ossutil 下载多一层目录（`<raw_root>/<对象名>/<对象名>/RawData`）：ossutil `cp -rf oss://b/PREFIX DEST/` 会在 DEST 下自动建 PREFIX 同名目录，原命令 DEST 传了已含对象名的 target 故重复；改为传 `raw_root`（target 父目录），落点即为 `<raw_root>/<对象名>/`（dry-run 验证）；`_ossutil_running` 改用 `oss://` 源路径匹配（不再依赖唯一性不足的目的目录）
- [x] 修复 OSS 任务列表不显示任务 ID + 无进度：根因 `api.ossTaskList` 未解包 `{tasks:[]}`（v-for 遍历对象致 id/status 全 undefined、只剩删除按钮且误显"暂无任务"），改为 `.then(r=>r.tasks||[])`；新增下载进度展示——`read_progress()` 解析日志尾行，`public().progress` 返回 percent/total/done/skipped/speed，percent 按累计口径（续传不回退），查询结果 NProgress 进度条 + 明细、列表项显示百分比
- [x] OSS 下载「服务启动自动续传」+ 任务删除/合并：启动时自动 RESUME 被中断任务（同源只恢复一个；`[oss] resume_on_start` 可关，默认开）；`remove`/`merge` API + 前端任务列表/查询页删除按钮（不动数据目录）；实际合并 oss_1790578318/oss_1790585600 重复任务（保留 318 续传，`skipped 6.321GiB` 只下剩余）
- [x] OSS 下载去重 + 自动续传：相同交付信息（oss_path+endpoint）且同一目标目录只保留一个任务——重复提交时运行中返回 running（不重复启动）、已完成返回 done（不重复下载）、失败/中断（含服务重启残留 running）复用同一任务 ID 续传（ossutil `--ignore-existing` + `--checkpoint-dir`）；登记信息每次可更新；去重索引随 DB 重建；前端按 action 提示
- [x] 修复 OSS 下载「Cannot read properties of undefined (reading 'importKey')」：`crypto.subtle` 在非安全上下文（内网 http://IP）为 undefined，`encryptOssInfo` 检测后自动降级 node-forge（纯 JS AES-256-GCM + RSA-OAEP-SHA256，输出结构与 WebCrypto 完全一致），后端解密零改动；已验证 forge 加密 ↔ cryptography 解密往返一致
- [x] OSS 下载登记限定条件：新增必填「等级负责人」+「数据情况说明」（前端校验+后端 400），随任务持久化（oss_tasks 表新列 + init_db 自动补列迁移）、download.log 留痕、卡片④提交区/状态区与卡片⑤查询区/任务列表展示
- [x] CRISPResso 参数默认值语义改造 + Cas9 预设：qw_center/qw_size/nw_gap_open 三参数可留空（=CRISPResso2 原生默认 -3/1/-20，不再硬编码 -1/5/-30；plot_window_size 仍固定 25 仅影响绘图）；前端「Cas9 参数一键填充」（qw_center=-3 + qw_size=10）与「重置为 CRISPResso2 默认」按钮；改动链 AppBody.vue→app.py(_parse_opt_int)→runner.py(ini 留空)→main.py(opt_int None→不加 CLI)→crispresso.py(click default=None)→build_crispresso_cmd(None→不传参数)；指纹新增 nw_gap_open 维度
- [x] 任务删除功能（前后端）：DELETE /api/jobs/<id> 增强——运行中 409 拒绝；删除仅物理释放"没有被其他任务引用"的产物（硬链接 nlink 判定），失去全部引用的缓存条目连带清理并返回报告（freed_exclusive_bytes/cache_pruned/cache_kept）；内存与磁盘（重启后）任务均可删，指纹来自内存 job.cache_fps 或 job_info.json；前端卡片②任务列表 + 卡片⑤任务查询页均有 NPopconfirm 删除按钮与结果提示
- [x] 跨任务缓存（方案 C）：backend/cache.py 两层内容寻址缓存（split/crispresso 指纹+硬链接树），相同参数任务零重复计算、参数差异只重算差异层；pack.zip 追加 job_info.json/config.ini/run.log 实现参数随包导出；job_id 同秒冲突防护、重跑时 finished 标志重置；见 §3.6
- [x] CRISPResso 高级参数透传（六层链路）：前端卡片① 新增 `ignore_substitutions`（NSwitch）/ `quantification_window_center`（NInputNumber，默认 -1）/ `quantification_window_size`（NInputNumber，默认 5，最小 1）→ app.py（`_parse_int` 解析+`_disk_job_status` 回读）→ runner.py write_config `[analysis]` 段 → WYW/main.py step_crispresso → LMY pipeline `crispresso run --quantification-window-center/--quantification-window-size` → `build_crispresso_cmd()`；旧 ini 缺键自动回退默认值，行为与历史一致
- [x] raw_root 改造为下拉菜单：`GET /api/runs`（runs_root 一级子文件夹，按 mtime 倒序）+ `GET /api/runs/fastq_root`（L0→L2 逐层探测 `*/*.fq.gz` 所在目录=fastq 父级，自动填充 raw_root 并显示样本目录数；dir 参数限制在 runs_root 内防遍历）；OSS 下载基座与 fastq raw_root 解耦（固定 runs_root）
- [x] 持久化层：SQLite（peewee）统一管理任务/日志元数据，`crpresso.db` 随启动自动建表
- [x] OSS 任务表持久化：`OssManager` 启动从 DB 重建内存任务表；每次状态变更 write-through；`run()` 后台线程落盘 → 重启后旧 oss_ 任务不再 404
- [x] OSS 下载完成联动：卡片④下载完毕后出现「用刚下载的数据创建并运行」按钮，直接复用卡片①的 raw_root 创建任务
- [x] `run.sh` 访问主机/端口改为从 `config.ini [server]` 读取（与 app.py 启动保持一致，仍可用环境变量覆盖）
- [x] `crpresso.db` 加入 `.gitignore`（运行期产物）
- [x] UI 重构为三标签页：「分析任务」（上传/任务列表/进度）、「OSS 数据下载」和「任务查询」各自独立整页放大；失败步骤/查询失败时提供 `run.log` 错误日志下载
- [x] 「分析任务」页布局：卡片③（任务详情）移至卡片②（任务列表）正下方，共用右侧一列（左侧为卡片①）
- [x] OSS 下载页新增「OSS 下载任务查询」区：ID 查询 + 进度标签 + 最近任务列表（点击即查/下日志），与任务查询页对称
- [x] config.ini 集中配置（host/port/allowed_ips/ossutil_bin）+ IP 白名单中间件
- [x] OSS 下载功能全链路（textarea→信封加密→解密→写配置→后台下载），实测 2.0G 数据落盘
- [x] 任务查询面板：按 ID 查询（内存+磁盘回退）、进度标签、pack.zip/CSV/Excel 下载
- [x] raw_root 递归补全（NAutoComplete，4 层验证）；raw_root 默认固定值
- [x] 批次下拉移除（固定 yw4）、示例 Excel + 严格列校验（缺列/引物异常返回中文错误）
- [x] naive-ui 表单绑定修复（:value + @update:value）、上传 data.file.file 修复

## 8. 持久化层（SQLite / peewee）

服务此前把 OSS 下载任务放在内存里，重启后旧 `oss_` 任务 ID 直接 404。现引入 peewee + SQLite 统一
管理这类「任务表」的元数据，日志正文仍在各自目录的 `download.log`/`run.log` 落盘，此处只存元数据与路径。

- 数据文件：项目根 `crpresso.db`（`.gitignore` 已忽略，运行期自动生成，可用 `CRWeb_DB_PATH` 覆盖路径）
- 模型定义：`backend/db.py` —— `OssTaskModel`（`oss_tasks` 表）：id/oss_path/target/endpoint/status/
  finished/error/log_path/created_at/updated_at；`init_db()` 幂等建表（WAL 模式）
- 写入时机：`OssManager.__init__` 启动时 `_load()` 从 DB 重建内存任务表；`OssTask._save()` 在
  `create_download()`（初始）与 `run()` 每次状态变更（running/done/failed/finished）write-through（upsert，
  不覆盖 created_at）
- 效果：重启后按 ID 仍可查询历史 oss 任务的最后状态（列表/日志接口不变）；下载日志磁盘文件不受影响
- 线程安全：peewee 连接为 thread-local，后台下载线程各自懒连接；`busy_timeout=5000` + WAL 缓解并发写
- 待扩展：后续可把 Job/日志也纳入同一库（`backend/db.py` 已预留 `BaseModel` 与 `_MODELS` 扩展点）
