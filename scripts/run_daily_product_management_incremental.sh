#!/usr/bin/env bash
set -euo pipefail

cd /opt/apps/lx-product-m
source .venv/bin/activate
mkdir -p logs

BATCH_TS="$(date +%Y%m%d_%H%M%S)"
LOG="logs/product_management_incremental_${BATCH_TS}.log"
LOCK="/tmp/lxpm_product_management_incremental.lock"

{
  echo "===== daily product management incremental start ====="
  echo "batch_ts=${BATCH_TS}"
  echo "start_time=$(date '+%F %T')"
  echo "workdir=$(pwd)"

  flock -n 9 || {
    echo "已有产品管理增量任务正在运行，本次退出：$(date '+%F %T')"
    exit 0
  }

  if pgrep -f "apply_product_custom_fields_from_feishu_v4.py" >/dev/null; then
    echo "检测到 V4 自定义字段全量任务仍在运行，本次每日增量退出，避免并行写入：$(date '+%F %T')"
    exit 0
  fi

  if pgrep -f "apply_lx_spu|apply_feishu_match_to_lx_products|apply_product_custom_fields_from_feishu" >/dev/null; then
    echo "检测到同类写入任务正在运行，本次每日增量退出，避免并行写入：$(date '+%F %T')"
    exit 0
  fi

  echo "===== git pull ====="
  git pull origin main

  echo "===== compile check ====="
  python -m compileall -q \
    scripts/sync_categories.py \
    scripts/sync_product_list_snapshot_fast.py \
    scripts/sync_feishu_style_category_match.py \
    scripts/create_missing_lx_categories_from_feishu_match.py \
    scripts/apply_feishu_match_to_lx_products_fast.py \
    scripts/apply_lx_spu_from_sku_prefix_incremental.py \
    scripts/apply_product_custom_fields_from_feishu_incremental.py

  echo "===== step 1/7 sync lx categories ====="
  python -u scripts/sync_categories.py

  echo "===== step 2/7 sync product snapshot ====="
  python -u scripts/sync_product_list_snapshot_fast.py

  echo "===== step 3/7 sync feishu style category match ====="
  python -u scripts/sync_feishu_style_category_match.py \
    --app-token WD5NbNK4KaXmkgsnrydcGgv9nib \
    --table-id tblzzfcUTcD3YHtM \
    --view-id vewCCPfU7e \
    --limit 0 \
    --show 20 \
    --confirm

  echo "===== step 4/7 create missing lx categories ====="
  python -u scripts/create_missing_lx_categories_from_feishu_match.py --confirm

  echo "===== step 5/7 apply category incremental ====="
  python -u scripts/apply_feishu_match_to_lx_products_fast.py \
    --batch-no "category_incremental_${BATCH_TS}" \
    --statuses matched warning \
    --delay 0.1 \
    --max-retries 5 \
    --confirm

  echo "===== step 6/7 apply spu incremental ====="
  python -u scripts/apply_lx_spu_from_sku_prefix_incremental.py \
    --batch-no "spu_incremental_${BATCH_TS}" \
    --delay 1.0 \
    --confirm

  echo "===== step 7/7 apply custom fields incremental ====="
  python -u scripts/apply_product_custom_fields_from_feishu_incremental.py \
    --batch-no "custom_fields_incremental_${BATCH_TS}" \
    --statuses matched warning \
    --delay 0.5 \
    --confirm

  echo "end_time=$(date '+%F %T')"
  echo "===== daily product management incremental finished ====="
} 9>"${LOCK}" 2>&1 | tee "${LOG}"
