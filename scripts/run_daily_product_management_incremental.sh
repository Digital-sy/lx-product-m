#!/usr/bin/env bash
set -Eeuo pipefail

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export HOME="/root"
umask 022

PROJECT_DIR="/opt/apps/lx-product-m"
PYTHON="${PROJECT_DIR}/.venv/bin/python"
LOG_DIR="${PROJECT_DIR}/logs"
LOCK="/tmp/lxpm_product_management_incremental.lock"

cd "${PROJECT_DIR}"
mkdir -p "${LOG_DIR}"

BATCH_TS="$(date +%Y%m%d_%H%M%S)"
LOG="${LOG_DIR}/product_management_incremental_${BATCH_TS}.log"
LAST_START="${LOG_DIR}/product_management_incremental_last_start.txt"
LAST_SUCCESS="${LOG_DIR}/product_management_incremental_last_success.txt"
LAST_FAILURE="${LOG_DIR}/product_management_incremental_last_failure.txt"

exec 9>"${LOCK}"
if ! /usr/bin/flock -n 9; then
  echo "已有产品管理增量任务正在运行，本次退出：$(date '+%F %T')" | tee -a "${LOG}"
  exit 0
fi

exec > >(tee -a "${LOG}") 2>&1

on_error() {
  local exit_code=$?
  local line_no="${1:-unknown}"
  local failed_cmd="${2:-unknown}"
  {
    echo "status=failed"
    echo "batch_ts=${BATCH_TS}"
    echo "failure_time=$(date '+%F %T %z')"
    echo "exit_code=${exit_code}"
    echo "line=${line_no}"
    echo "command=${failed_cmd}"
    echo "log=${LOG}"
  } > "${LAST_FAILURE}"
  echo "[FAILED] line=${line_no}, exit_code=${exit_code}, command=${failed_cmd}"
  exit "${exit_code}"
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

{
  echo "status=started"
  echo "batch_ts=${BATCH_TS}"
  echo "start_time=$(date '+%F %T %z')"
  echo "log=${LOG}"
} > "${LAST_START}"

if [[ ! -x "${PYTHON}" ]]; then
  echo "虚拟环境 Python 不存在或不可执行：${PYTHON}"
  exit 1
fi

echo "===== daily product management incremental start ====="
echo "batch_ts=${BATCH_TS}"
echo "start_time=$(date '+%F %T %z')"
echo "workdir=$(pwd)"
echo "python=${PYTHON}"
echo "policy=matched/warning 飞书匹配优先；无飞书匹配但已有领星分类路径的 SKU 走分类路径兜底；无飞书匹配且无分类路径的超旧 SKU 忽略"
echo "write_guard=写入前读取实时品名和完整字段；写入后复查品名；实时品名为空时禁止写入"

if /usr/bin/pgrep -f "apply_product_custom_fields_from_feishu_v4.py" >/dev/null; then
  echo "检测到 V4 自定义字段全量任务仍在运行，本次每日增量退出，避免并行写入：$(date '+%F %T')"
  exit 0
fi

if /usr/bin/pgrep -f "apply_lx_spu_from_sku_prefix_incremental.py|apply_feishu_match_to_lx_products_fast.py|apply_product_custom_fields_from_feishu_incremental.py|apply_product_custom_fields_from_category_path_incremental.py" >/dev/null; then
  echo "检测到同类写入任务正在运行，本次每日增量退出，避免并行写入：$(date '+%F %T')"
  exit 0
fi

# 定时数据同步不应因为 GitHub 暂时不可用、SSH 认证异常或本地改动而整体中断。
# 拉取失败时继续使用服务器当前版本执行，并在日志中记录警告。
echo "===== optional git pull ====="
if ! /usr/bin/timeout 120 /usr/bin/git pull --ff-only origin main; then
  echo "[WARN] git pull 失败或超时，继续使用服务器当前代码执行本次数据同步。"
fi

echo "===== compile check ====="
"${PYTHON}" -m compileall -q \
  lx_product_m/services/product_write_guard.py \
  lx_product_m/services/spu_service.py \
  scripts/sync_categories.py \
  scripts/sync_product_list_snapshot_fast.py \
  scripts/sync_feishu_style_category_match.py \
  scripts/create_missing_lx_categories_from_feishu_match.py \
  scripts/apply_feishu_match_to_lx_products_fast.py \
  scripts/apply_lx_spu_from_sku_prefix_incremental.py \
  scripts/apply_product_custom_fields_from_feishu_incremental.py \
  scripts/apply_product_custom_fields_from_category_path_incremental.py

# 分类同步失败时，仍然继续刷新产品快照；但为了避免使用过期分类树写回，完成快照后停止后续写入。
echo "===== step 1/8 sync lx categories ====="
CATEGORY_SYNC_OK=1
if ! "${PYTHON}" -u scripts/sync_categories.py; then
  CATEGORY_SYNC_OK=0
  echo "[WARN] 领星分类同步失败；将继续刷新产品快照，但本次停止后续分类/SPU/自定义字段写回。"
fi

# 分类接口与产品列表接口连续调用会触发领星 code=103 限流，主动留出间隔。
echo "===== rate-limit spacing before product snapshot: 20s ====="
/bin/sleep 20

echo "===== step 2/8 sync product snapshot ====="
"${PYTHON}" -u scripts/sync_product_list_snapshot_fast.py \
  --page-size 1000 \
  --max-retries 8 \
  --retry-base-seconds 10 \
  --retry-max-seconds 120

if [[ "${CATEGORY_SYNC_OK}" -ne 1 ]]; then
  echo "产品快照已刷新，但分类同步失败，本次安全停止后续写回。"
  exit 1
fi

echo "===== step 3/8 sync feishu style category match ====="
"${PYTHON}" -u scripts/sync_feishu_style_category_match.py \
  --app-token WD5NbNK4KaXmkgsnrydcGgv9nib \
  --table-id tblzzfcUTcD3YHtM \
  --view-id vewCCPfU7e \
  --limit 0 \
  --show 20 \
  --confirm

echo "===== step 4/8 create missing lx categories ====="
"${PYTHON}" -u scripts/create_missing_lx_categories_from_feishu_match.py --confirm

echo "===== step 5/8 apply category incremental (live-name guarded) ====="
"${PYTHON}" -u scripts/apply_feishu_match_to_lx_products_fast.py \
  --batch-no "category_incremental_${BATCH_TS}" \
  --statuses matched warning \
  --delay 0.1 \
  --max-retries 5 \
  --confirm

echo "===== step 6/8 apply spu incremental (live-name guarded) ====="
"${PYTHON}" -u scripts/apply_lx_spu_from_sku_prefix_incremental.py \
  --batch-no "spu_incremental_${BATCH_TS}" \
  --delay 1.0 \
  --confirm

echo "===== step 7/8 apply custom fields incremental from feishu (live-name guarded) ====="
"${PYTHON}" -u scripts/apply_product_custom_fields_from_feishu_incremental.py \
  --batch-no "custom_fields_incremental_${BATCH_TS}" \
  --statuses matched warning \
  --delay 0.5 \
  --max-retries 5 \
  --confirm

echo "===== step 8/8 apply custom fields fallback from category path (live-name guarded) ====="
"${PYTHON}" -u scripts/apply_product_custom_fields_from_category_path_incremental.py \
  --batch-no "custom_fields_category_path_${BATCH_TS}" \
  --delay 0.5 \
  --max-retries 5 \
  --confirm

{
  echo "status=success"
  echo "batch_ts=${BATCH_TS}"
  echo "success_time=$(date '+%F %T %z')"
  echo "log=${LOG}"
} > "${LAST_SUCCESS}"
rm -f "${LAST_FAILURE}"

echo "end_time=$(date '+%F %T %z')"
echo "===== daily product management incremental finished ====="
