#!/usr/bin/env bash
# CRISPResso2 分析平台启动脚本（uv 管理依赖）
set -e
cd "$(dirname "$0")"

export CRWeb_MAIN_PY="${CRWeb_MAIN_PY:-/mnt/data1/zhang/projects/XueChaoYou_WYW/main.py}"
export CRWeb_LMY_DIR="${CRWeb_LMY_DIR:-/mnt/data1/zhang/projects/XueChaoYou_LMY}"
export CRWeb_JOBS_ROOT="${CRWeb_JOBS_ROOT:-$(pwd)/jobs}"
# 访问主机/端口取自 config.ini [server]（app.py 亦据此启动，保持一致）；仍可用环境变量覆盖。
if [ -x "$(pwd)/.venv/bin/python" ]; then
  CRWeb_HOST="$((pwd)/.venv/bin/python -c "import configparser,sys;c=configparser.ConfigParser();c.read(sys.argv[1]);print(c.get('server','host',fallback='0.0.0.0'))" config.ini 2>/dev/null || echo "0.0.0.0")"
  CRWeb_PORT="$((pwd)/.venv/bin/python -c "import configparser,sys;c=configparser.ConfigParser();c.read(sys.argv[1]);print(c.get('server','port',fallback='5000'))" config.ini 2>/dev/null || echo "5000")"
fi

# 首次运行用 uv 根据 pyproject.toml 创建 venv 并安装依赖
[ -d .venv ] || uv sync
source .venv/bin/activate

echo "启动 CRISPResso2 分析平台:"
echo "  主程序 main.py : $CRWeb_MAIN_PY"
echo "  流水线包目录  : $CRWeb_LMY_DIR"
echo "  任务目录      : $CRWeb_JOBS_ROOT"
echo "  访问地址      : http://$CRWeb_HOST:$CRWeb_PORT"
exec python app.py
