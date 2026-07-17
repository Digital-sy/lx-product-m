# 颜色体系补标：按最早 Listing 创建时间判定 A2023

脚本：`scripts/color_system_tagging_listing_time.py`

## 口径

- 范围：`lxpm_product_category_snapshot` 中颜色体系为空的 SKU。
- 关联：按 `lingxing.listing` 的 SKU 反推该 SKU 在所有 Listing 中的最早创建时间。
- 最早 Listing 创建时间不晚于 `2024-06-30`：拟打 `A2023`。
- 晚于截止日期、没有 Listing 创建时间或时间无法解析：维持 `待定`。
- 当前已经是 `A2023` 或 `B2024` 的 SKU 不进入补标；其他非空异常值也不覆盖。
- `XH`、`FAB`、`LCS`、`PF` 只从“最终剩余不确定数量”中剔除，不影响已经由 Listing 时间明确判定为 A2023 的 SKU。

## 执行前刷新产品管理快照

```bash
python -u scripts/sync_product_list_snapshot_fast.py --page-size 1000
```

## 生成 dry-run

```bash
python -u scripts/color_system_tagging_listing_time.py
```

脚本会从 `information_schema.columns` 自动识别 Listing 创建时间字段，并在控制台打印实际采用的字段。如果自动识别失败或存在歧义，先查看提示中的日期类字段，再显式指定：

```bash
python -u scripts/color_system_tagging_listing_time.py \
  --listing-created-column listing_create_time
```

默认输出：

```text
reports_analysis/color_system_tagging_listing_time_dryrun_YYYYMMDD_HHMMSS.xlsx
```

Excel 包含：

- `拟打标明细`：所有颜色体系为空 SKU 的 Listing 时间、拟打值和判定原因；正式写入只处理其中的 A2023。
- `汇总`：已有标签、按 Listing 时间新增 A2023、剩余不确定、四类前缀数量及剔除后的最终不确定数量。
- `剩余不确定`：剔除四类前缀后的待人工处理 SKU。
- `前缀剔除明细`：待定且以 XH/FAB/LCS/PF 开头的 SKU。

## 正式写入

人工确认 Excel 后执行：

```bash
export LX_COLOR_SYSTEM_FIELD_ID='207722905719915521'

python -u scripts/color_system_tagging_listing_time.py \
  --apply \
  --review-file reports_analysis/color_system_tagging_listing_time_dryrun_YYYYMMDD_HHMMSS.xlsx \
  --delay 0.5 \
  --verify-delay 1 \
  --allow-outside-low-peak
```

正式写入只提交拟打值为 `A2023` 的行；`待定` 不调用写接口。写入沿用第一轮的写前实时详情、实时品名与分类保留、完整非空自定义字段合并、写后批量复查和失败清单。
