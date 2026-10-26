# CRISPResso2 分析平台 — 交接摘要（写入新 session 用）

## 项目目标
内网 Web 平台（Flask+Vue）：上传 metainfo Excel → OSS 下载数据 → 分步骤跑 CRISPResso2 流水线 → 在线查进度/下载结果。近期重心：支持新批次数据结构（Sample/L 列目录映射 + 双端 barcode 拆分）、任务结果邮件通知。

## 关键路径/模块
- 平台根：`/mnt/data1/zhang/projects/crpresso_web/`
  - `app.py`（全部 REST 端点）、`config.ini`（[server]/[security]/[oss]/[data]/[mail]）
  - `backend/runner.py`（Job/JobManager、步骤挂钩、失败即中止、历史任务 start 磁盘重建）
  - `backend/cache.py`（跨任务缓存：split/crispresso 指纹+硬链接树、`SPLIT_PARSE_VERSION`、`safe_delete_job`）
  - `backend/oss.py`（去重 `submit()` source_key=oss_path|endpoint|target、断点续传 `--ignore-existing --checkpoint-dir`、merge/remove、`read_progress()`、下载目录语义：命令目标传 raw_root，ossutil 自建对象名层）
  - `backend/mailer.py`（`[mail]` 配置；`mail_oss_done`/`mail_job_done`：日志正文+pack.zip 附件+任务级额外收件人 `_recipients`）
  - `frontend/src/AppBody.vue`（全部 UI：5 卡片、Sample 列表备注、run 下拉 render-label 两行显示负责人/备注）
- 流水线：`/mnt/data1/zhang/projects/XueChaoYou_WYW/main.py`（ini 驱动，`ensure_samples` 已支持字符串 ID）
- pipeline 包：`/mnt/data1/zhang/projects/XueChaoYou_LMY/pipeline/`
  - `config.py::_parse_yw_sample`（Sample 列缺失回退 `L` 列=原始目录名；多值按目录分 lane；输出名 ID 唯一优先；`build()` 多 lane 时 lane_map=identity + 目录映射校验 warning/error）
  - `amplicons.py::parse_yw4`（与 config 同命名规则，按输出名去重）
  - `crispresso.py`/`common.py::build_crispresso_cmd`（可选参数 None=CRISPResso2 默认 -3/1/-20；0 任务 SystemExit）
  - `collect.py`（0 结果 SystemExit；输出统一 xlsx：`editing_efficiency.xlsx`/`_summary.xlsx`）
  - `splitfq.py`（0 组 SystemExit）、`plot.py::pack_cmd`（str(id) + 正则 `^(.+)_(\d+)\.pdf$`）
- CRISPResso2：`/opt/crispresso2/bin/python`；数据根 `/mnt/nas/数据备份/WangYiWen_TnSeq/`

## 已实现功能
任务队列/断点续跑/磁盘回退查询、跨任务缓存、OSS 去重+续传+删除/合并、邮件通知（默认收件人 zhangyiming@ihcams.ac.cn + 任务级 `notify_email` 追加去重）、Sample/L 列目录映射、Cas9 预设按钮、失败即中止流水线、历史任务重跑（start 磁盘重建，notify_email 持久化于 ini）、pack.zip 重建式写入效率表（`_export_job_info`）、前端进度条/删除按钮/额外邮箱输入。

## 未完成事项（按优先级）
1. **batch7 重跑进行中**：job_1791768373（20261001_E261001003，93 样本，9 lane=L1-1…L3-3）。split 步骤正在逐 lane 拆分（fq 文件数增长缓慢，目标 186=93×2），完成后自动 crispresso(744 组合)→collect→pack。**完成时 mail_job_done 会自动发结果邮件到 zhangyiming@ihcams.ac.cn**，无需手动发。
2. 验证 batch7 拆分正确性：抽样品 fq 确认 100% barcode 命中（参考此前 P1 实测方法）。
3. 检查 collect 新 xlsx 输出实际生效（`editing_efficiency.xlsx`/`_summary.xlsx`）。
4. 旧 csv 产物兼容：score_table 不读 efficiency 文件（直算自 crispresso），无需改；但旧 job 的 csv 仍在各自目录，未迁移。
5. README §7 已更新至「pack 补效率表」条目，最新 3 条修复（失败阻断/ID 字符串/邮件）已记录。

## 关键决策
- Sample 列=原始数据目录名：多值→按目录分 lane（lane 内 barcode 唯一），单值→通配全部 fq；`L` 列为其别名。
- 结果统一 xlsx：`editing_efficiency.xlsx`/`_summary.xlsx` 替代 csv；ini 键 `efficiency_csv` 名不变值改。
- split 指纹含 `SPLIT_PARSE_VERSION="20261013_L_COL"`：解析逻辑变更必须 bump，否则旧缓存误命中（曾致解析变更后被旧产物覆盖，已清理 `cache/split/*`、`cache/crispresso/*`）。
- 邮件收件人=默认 to + 任务 `cfg.notify_email` 去重合并；OSS 提醒固定默认收件人。
- 流水线任一步骤失败 → 中止后续（剩余 pending + SKIP/ABORT 日志）。

## 技术栈/配置
- 后端 Flask3+peewee(SQLite `crpresso.db`)+loguru；前端 Vue3+naive-ui+Vite6（`npm run build` 产物 dist 由 Flask 直读）；uv 管理。
- OSS 通知邮箱配置在 `config.ini [mail]`（zhym1992@foxmail.com/smtp.qq.com:465/授权码明文/to=zhangyiming@ihcams.ac.cn）。
- naive-ui 绑定约定：`:value` + `@update:value`，勿用 v-model。

## 重要命令
```bash
# 重启服务（pkill 与启动必须分两次执行，同条会超时且 kill 进程树）
pkill -f "python app[.]py"
cd /mnt/data1/zhang/projects/crpresso_web && setsid --fork uv run python app.py >/tmp/crw_server.log 2>&1 < /dev/null
# 前端构建
cd frontend && npm run build
# 手动跑某步骤（--config 必须绝对路径！相对路径会解析到 WYW 目录）
.venv/bin/python /mnt/data1/zhang/projects/XueChaoYou_WYW/main.py --config <abs>/config.ini --step <step>
# API 触发重跑（服务重启后历史任务也可用）
curl -X POST http://127.0.0.1:5000/api/jobs/<id>/start -H 'Content-Type: application/json' -d '{"steps":"..."}'
# UI 测试：playwright-core 在 /tmp/pwtest（executablePath=/usr/sbin/chromium-browser，脚本须放 /tmp/pwtest/ 下）
```

## 已知问题/风险
- **pkill -f 模式含未转义关键词会自杀 shell**（如 CRISPResso），需用 `CRISPRESS[o]` 转义。
- 后台长命令在 bash 工具里可能超时被杀整树；启动服务用 `setsid --fork` 单独执行。
- 孤儿 ossutil/CRISPResso 进程：服务重启后残留运行，`_ossutil_running`（/proc 扫描）与 watcher 已防 ossutil 重复；CRISPResso 无此防护。
- 手动调试误用相对 --config 会重建 WYW 目录汇总产物（幂等可恢复，已告知用户）。
- plot.py generate 图名用 `{id}_{score}.pdf`，id 含下划线/连字符时 pack 正则已放宽匹配。
- `config.ini [mail] password` 明文，勿外传。

## 下一步计划
1. 轮询 job_1791768373：确认 split 完成 186 fq、crispresso 744 组合进度、collect/pack 完成；完成后确认自动邮件已发（batch7）。
2. 抽验 batch7 拆分正确性：任选样品 fq 统计 read 前 8bp 是否 100% 等于其 B5引物/B7引物 之一。
3. 验证 collect 输出 `editing_efficiency.xlsx`/`_summary.xlsx` 存在且 pack.zip 已含。
4. 若用户对 batch6 邮件中 9 个 FAIL-GUIDE 样本有异议，引导核对 spacer 与转染 guide。
5. 用户确认 `SampleN-M→LN-M` 映射规则是否长期有效；若命名规则再变需同步 `_parse_yw_sample`。

## 需要补充的信息
- batch7 任务若中途失败：读 `jobs/job_1791768373/run.log` 定位（config/amplicons/split 已 OK，crispresso 运行中）。
- 用户是否需要 OSS 下载提醒也支持额外收件人：未知/待确认。
