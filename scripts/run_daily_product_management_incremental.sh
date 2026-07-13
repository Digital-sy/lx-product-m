#!/usr/bin/env bash
set -Eeuo pipefail

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export HOME="/root"
umask 022

PROJECT_DIR="/opt/apps/lx-product-m"
PYTHON="${PROJECT_DIR}/.venv/bin/python"
LOG_DIR="${PROJECT_DIR}/logs"
LOCK="/tmp/lxpm_product_management_incremental.lock"
WRITE_GUARD_VERSION="2026-07-13-v1"

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

check_name_guard_failures() {
  local table_name="$1"
  local batch_no="$2"
  local failure_count
  failure_count="$("${PYTHON}" - "${table_name}" "${batch_no}" <<'PY'
import sys
from lx_product_m.db import Database

table_name = sys.argv[1]
batch_no = sys.argv[2]
allowed = {
    "lxpm_product_category_change_log",
    "lxpm_product_custom_field_change_log",
    "lxpm_spu_change_log",
}
if table_name not in allowed:
    raise SystemExit(f"非法日志表：{table_name}")

db = Database()
try:
    row = db.fetch_one(
        f"SELECT COUNT(*) AS cnt FROM `{table_name}` WHERE batch_no=%s AND status='name_verify_failed'",
        (batch_no,),
    )
    print(int((row or {}).get("cnt") or 0))
finally:
    db.close()
PY
)"
  if [[ "${failure_count}" != "0" ]]; then
    echo "[FAILED] 品名防覆盖复查失败：table=${table_name}, batch=${batch_no}, count=${failure_count}"
    return 1
  fi
  echo "name_guard_check=passed table=${table_name} batch=${batch_no}"
}

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
# 但写回代码必须通过安全版本标记校验，旧版代码禁止继续写领星。
echo "===== optional git pull ====="
if ! /usr/bin/timeout 120 /usr/bin/git pull --ff-only origin main; then
  echo "[WARN] git pull 失败或超时，继续检查服务器当前代码版本。"
fi

ACTUAL_GUARD_VERSION=""
if [[ -f "${PROJECT_DIR}/write_guard.version" ]]; then
  ACTUAL_GUARD_VERSION="$(tr -d '\r\n ' < "${PROJECT_DIR}/write_guard.version")"
fi
if [[ "${ACTUAL_GUARD_VERSION}" != "${WRITE_GUARD_VERSION}" ]]; then
  echo "[FAILED] 写回安全版本不符合要求：expected=${WRITE_GUARD_VERSION}, actual=${ACTUAL_GUARD_VERSION:-missing}"
  echo "为防止旧品名或不完整字段覆盖领星，本次任务停止，不执行任何写回。"
  exit 1
fi
echo "write_guard_version=${ACTUAL_GUARD_VERSION}"

echo "===== compile check ====="
"${PYTHON}" -m compileall -q \
  lx_product_m/services/product_write_guard.py \
  lx_product_m/services/product_service.py \
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

CATEGORY_BATCH="category_incremental_${BATCH_TS}"
echo "===== step 5/8 apply category incremental (live-name guarded) ====="
"${PYTHON}" -u scripts/apply_feishu_match_to_lx_products_fast.py \
  --batch-no "${CATEGORY_BATCH}" \
  --statuses matched warning \
  --delay 0.1 \
  --max-retries 5 \
  --confirm
check_name_guard_failures "lxpm_product_category_change_log" "${CATEGORY_BATCH}"

SPU_BATCH="spu_incremental_${BATCH_TS}"
echo "===== step 6/8 apply spu incremental (live-name guarded) ====="
"${PYTHON}" -u scripts/apply_lx_spu_from_sku_prefix_incremental.py \
  --batch-no "${SPU_BATCH}" \
  --delay 1.0 \
  --confirm
check_name_guard_failures "lxpm_spu_change_log" "${SPU_BATCH}"

CUSTOM_FIELDS_BATCH="custom_fields_incremental_${BATCH_TS}"
echo "===== step 7/8 apply custom fields incremental from feishu (live-name guarded) ====="
"${PYTHON}" -u scripts/apply_product_custom_fields_from_feishu_incremental.py \
  --batch-no "${CUSTOM_FIELDS_BATCH}" \
  --statuses matched warning \
  --delay 0.5 \
  --max-retries 5 \
  --confirm
check_name_guard_failures "lxpm_product_custom_field_change_log" "${CUSTOM_FIELDS_BATCH}"

CATEGORY_PATH_BATCH="custom_fields_category_path_${BATCH_TS}"
echo "===== step 8/8 apply custom fields fallback from category path (live-name guarded) ====="
"${PYTHON}" -u scripts/apply_product_custom_fields_from_category_path_incremental.py \
  --batch-no "${CATEGORY_PATH_BATCH}" \
  --delay 0.5 \
  --max-retries 5 \
  --confirm
check_name_guard_failures "lxpm_product_custom_field_change_log" "${CATEGORY_PATH_BATCH}"

{
  echo "status=success"
  echo "batch_ts=${BATCH_TS}"
  echo "success_time=$(date '+%F %T %z')"
  echo "log=${LOG}"
} > "${LAST_SUCCESS}"
rm -f "${LAST_FAILURE}"

echo "end_time=$(date '+%F %T %z')"
echo "===== daily product management incremental finished ====="
