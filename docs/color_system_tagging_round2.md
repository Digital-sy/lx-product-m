# 颜色体系批量打标：第二轮待定组消化

脚本：`scripts/color_system_tagging_round2.py`

第二轮只处理第一轮审阅清单中拟打值为 `待定` 的 SKU。默认断言范围为 9,823 个，避免重新计算首销后误入其他 SKU。

## 1. 判定规则

判定前会进行以下清洗：

- Unicode NFKC 归一化，全角数字转半角；
- 去除首尾空格；
- 去除末尾的 `年` 字及其前后空格。

| 清洗后的开发年份 | 第二轮拟打值 | 实际写入行为 |
|---|---|---|
| `历史` | `A2023` | 审阅后可写入 |
| `2000`—`2024` 的四位年份 | `A2023` | 审阅后可写入 |
| `2025`—`2100` 的四位年份 | `待定` | 不写入 |
| 空值 | `待定` | 不写入 |
| 其他值 | `待定` | 不写入，列入意外值清单 |

以下值不会再因为“可转成整数且小于 2024”而被自动打成 A2023：

```text
23、0、999、1999、2101、9999
```

原始值仍会原样保留在 dry-run Excel 的分布中，便于审阅脏空格、全角数字和异常值；判定使用清洗后的值。例如 `2024 `、`２０２４年` 会判为 A2023，但原值分布仍显示原始字符串。

## 2. 生成第二轮 dry-run

```bash
cd /opt/apps/lx-product-m
source .venv/bin/activate

python -u scripts/color_system_tagging_round2.py \
  --first-round-file reports_analysis/color_system_tagging_dryrun_YYYYMMDD.xlsx
```

默认输出：

```text
reports_analysis/color_system_tagging_round2_dryrun_YYYYMMDD.xlsx
```

Excel 只有两个 sheet：

- `拟打标明细`：`SKU｜开发年份原值｜拟打值`
- `汇总`：转 A2023 数、剩余待定数、空值数、原值分布和意外值清单

脚本读取产品快照后，会先在控制台输出开发年份原值的去重计数。输出使用 Python `repr`，因此尾随空格等原始差异可见。

默认要求第一轮文件中恰好有 9,823 个待定 SKU。若业务确认范围数量已经变化，可以显式修改预期值；设为 `0` 可关闭数量断言：

```bash
--expected-pending-count 9823
```

正式写入前应确认：

- 转 A2023、剩余待定数量与预期一致；
- 意外值为 0，或每种意外值均已人工确认；
- 不存在 `23`、`0`、`1999`、`2101` 等异常纯数字被误判为 A2023。

## 3. 写入人工审阅后的清单

```bash
export LX_COLOR_SYSTEM_FIELD_ID='<颜色体系字段ID>'

python -u scripts/color_system_tagging_round2.py \
  --apply \
  --review-file reports_analysis/color_system_tagging_round2_dryrun_YYYYMMDD.xlsx \
  --delay 0.5 \
  --verify-delay 1
```

第二轮写入约束：

- `--apply` 强制要求人工审阅文件。
- 只写入审阅后拟打值为 `A2023` 的行；`待定` 行不会调用领星接口。
- 人工可将意外值行改成 `A2023` 后写入，也可保持 `待定` 继续跳过。
- 写入前重新拉取实时产品，复用 `product_write_guard.py` 合并完整自定义字段、实时品名和当前分类。
- 每批写入成功后重新读取实时产品，复查品名、分类、颜色体系及全部随请求携带的原有自定义字段。
- `code=103` 固定等待 30 秒，最多重试 3 次。
- 最终接口失败或写后复查失败，输出到 `reports_analysis/color_system_tagging_round2_failures_YYYYMMDD_HHMMSS.xlsx`，其他 SKU 继续处理。
- 正式写入默认只允许北京时间 `00:00-06:00`。

默认运行不会写入领星。
