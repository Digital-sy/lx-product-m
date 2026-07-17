# 颜色体系批量打标：第一轮 A2023

脚本：`scripts/color_system_tagging.py`

## 1. 生成 dry-run 清单

```bash
cd /opt/apps/lx-product-m
source .venv/bin/activate
python -u scripts/color_system_tagging.py
```

默认输出：

```text
reports_analysis/color_system_tagging_dryrun_YYYYMMDD.xlsx
```

Excel 只有两个 sheet：

- `拟打标明细`：SKU、关联 MSKU、SPU、开售日期、首销店铺、拟打值、近半年是否有销量。
- `汇总`：按拟打值的 SKU 数，以及按 SPU 聚合的 A2023/待定分布。

判定口径：

- 开售日期不晚于 `2024-06-30`：拟打 `A2023`。
- 2024—2026 数据窗口内没有首销记录：拟打 `待定`。
- 开售日期晚于 `2024-06-30`：不进入清单，本轮跳过。

人工审阅时可以删除不应写入的行，也可以在 `A2023`、`待定` 之间修正拟打值；不要增加重复 SKU。

## 2. 写入人工审阅后的清单

`--apply` 不会重新生成清单，必须显式传入人工审阅后的文件：

```bash
export LX_COLOR_SYSTEM_FIELD_ID='<颜色体系字段ID>'

python -u scripts/color_system_tagging.py \
  --apply \
  --review-file reports_analysis/color_system_tagging_dryrun_YYYYMMDD.xlsx \
  --delay 0.5 \
  --verify-delay 1
```

第一轮正式写入**只提交拟打值为 `A2023` 的行**。即使审阅文件同时包含 `待定`，待定行也不会调用领星写接口。

字段 ID 未显式提供时，脚本会依次尝试从产品快照和领星实时产品详情发现。正式执行仍建议通过 `LX_COLOR_SYSTEM_FIELD_ID` 或 `--field-id` 固定字段 ID。

安全约束：

- 只接受 `A2023`、`待定` 两个拟打值；重复 SKU 或非法值会在任何写入前终止。
- 每批写入前重新读取领星实时产品详情。
- 复用 `product_write_guard.py` 合并实时完整自定义字段，保留实时品名和当前分类后再提交 `product/set`。
- 实时值已经等于目标值的 SKU 计为跳过，不重复写入。
- 每批 `product/set` 成功后重新读取实时产品详情，复查：SKU、产品品名、分类、颜色体系以及写入请求中携带的全部原有自定义字段。
- 接口返回成功但写后复查不一致时，不计成功，写入失败清单并返回非 0 退出码。
- `code=103` 固定等待 30 秒，最多重试 3 次；最终失败写入 `reports_analysis/color_system_tagging_failures_YYYYMMDD_HHMMSS.xlsx`，其余 SKU 继续。
- 控制台输出处理、成功、失败、跳过数量。

## 3. 单条生产验证

批量执行前建议复制审阅文件，只保留 1 条 `A2023` 记录后先执行一次。脚本自身会写后复查，仍建议再刷新产品快照并人工核对：

- 颜色体系精确等于 `A2023`；
- 产品品名未变化；
- 分类 ID、分类名称未变化；
- 其他自定义字段未丢失、未改变；
- API 日志中 `code=0`，并保留 request_id。

## 4. 执行时间

正式写入默认只允许北京时间 `00:00-06:00`，与现有 `22:30` 定时任务错开。建议人工审阅后安排在 `02:00` 左右执行。

只有在明确确认不会与其他写入任务冲突时，才可使用：

```bash
--allow-outside-low-peak
```

该参数只解除时间保护，不会绕过 A2023 过滤、审阅清单、安全合并和写后复查。
