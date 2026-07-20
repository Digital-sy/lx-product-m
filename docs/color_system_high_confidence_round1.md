# 高置信度 A2023 第一轮写入

## 范围

第一轮固定采用最保守口径：

- 目标值仅 `A2023`；
- `置信度=高`；
- `判定类型=精确唯一匹配`；
- `是否允许未来自动写入=是`；
- P1 有销量优先，P2 无销量但有 Listing 其次；
- 排除 P3 无销量无 Listing；
- 排除多色套装；
- 排除特殊后缀、未收录代码、异常结构；
- 默认排除品名识别到多个颜色；
- 首轮默认 100 条，优先覆盖不同 SPU。

B2024 不在本轮范围内。

## 安全保护

- 必须先从 V3 dry-run Excel 生成独立审阅清单；
- apply 必须提供 `--expected-count`，数量不符立即终止；
- 写前实时读取领星产品详情；
- 实时颜色体系为空才允许写；
- 实时已经是 A2023 则跳过；
- 实时存在 B2024 或其他非空值时拒绝覆盖并进入失败清单；
- 完整保留品名、分类及全部其他自定义字段；
- product/set 后重新实时读取并验证。

## 服务器执行

### 1. 拉取分支

```bash
cd /opt/apps/lx-product-m-color-analysis
git pull --ff-only origin feat/color-system-name-code-matching
```

### 2. 运行测试

```bash
/opt/apps/lx-product-m/.venv/bin/python -m py_compile \
  scripts/color_system_high_confidence_round1.py

/opt/apps/lx-product-m/.venv/bin/python -m unittest \
  tests.test_color_system_high_confidence_round1 -v
```

### 3. 生成 100 条审阅清单

```bash
cd /opt/apps/lx-product-m-color-analysis

ANALYSIS_FILE="reports_analysis/color_system_name_code_analysis_v3_20260720_142911.xlsx"
REVIEW_FILE="reports_analysis/color_system_high_confidence_A2023_round1_100_$(date +%Y%m%d_%H%M%S).xlsx"

/opt/apps/lx-product-m/.venv/bin/python -u \
  scripts/color_system_high_confidence_round1.py prepare \
  --analysis-file "$ANALYSIS_FILE" \
  --limit 100 \
  --output "$REVIEW_FILE"

echo "REVIEW_FILE=$REVIEW_FILE"
```

### 4. 检查审阅清单

```bash
/opt/apps/lx-product-m/.venv/bin/python - "$REVIEW_FILE" <<'PY'
import sys
from collections import Counter
from openpyxl import load_workbook

path = sys.argv[1]
wb = load_workbook(path, read_only=True, data_only=True)
ws = wb["拟打标明细"]
headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
rows = [dict(zip(headers, values)) for values in ws.iter_rows(min_row=2, values_only=True)]

print(f"总数：{len(rows):,}")
print(f"不同SPU：{len({str(r.get('SPU') or '') for r in rows}):,}")
print("目标值：", Counter(str(r.get("拟打值") or "") for r in rows))
print("优先级：", Counter(str(r.get("处理优先级") or "") for r in rows))
print("判定类型：", Counter(str(r.get("判定类型") or "") for r in rows))

for row in rows[:20]:
    print(row["SKU"], row["颜色代码"], row["品名识别颜色"], row["处理优先级"])

wb.close()
PY
```

必须满足：

- 总数 100；
- 拟打值全部 A2023；
- 判定类型全部“精确唯一匹配”；
- 优先级只含 P1/P2；
- 不含特殊后缀、多色套装、未收录代码。

### 5. 后台写入

建议北京时间 00:00–06:00 执行：

```bash
cd /opt/apps/lx-product-m-color-analysis
mkdir -p logs

RUN_TIME="$(date +%Y%m%d_%H%M%S)"
WRITE_LOG="logs/color_system_high_confidence_A2023_round1_100_${RUN_TIME}.log"

nohup /opt/apps/lx-product-m/.venv/bin/python -u \
  scripts/color_system_high_confidence_round1.py apply \
  --review-file "$REVIEW_FILE" \
  --expected-count 100 \
  --field-id 207722905719915521 \
  --batch-size 100 \
  --delay 0.5 \
  --verify-delay 1 \
  > "$WRITE_LOG" 2>&1 < /dev/null &

WRITE_PID=$!
echo "WRITE_PID=$WRITE_PID"
echo "WRITE_LOG=$WRITE_LOG"
```

非低峰时段必须显式增加 `--allow-outside-low-peak`，不建议首轮这样做。

### 6. 查看结果

```bash
tail -f "$WRITE_LOG"
```

完整结束应出现：

```text
执行统计：处理=100 成功=... 失败=0 跳过=...
```

若存在失败，会生成：

```text
reports_analysis/color_system_high_confidence_round1_failures_*.xlsx
```

失败阶段为“写前冲突保护”表示该 SKU 实时已有其他颜色体系，脚本没有覆盖。

## 放大顺序

- 第一轮：100 条不同 SPU；
- 第一轮失败=0、前台抽查正确后：生成 1000 条新清单；
- 1000 条成功后，再处理剩余高置信度 A2023 P1/P2；
- P3 和 B2024 另行审批，不混入本轮。
