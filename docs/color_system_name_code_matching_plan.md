# 颜色体系：SKU颜色代码 + 品名颜色只读诊断

## 1. 安全边界

- 主分析范围：`lxpm_product_category_snapshot` 中“颜色体系”为空的全部 SKU。
- 第一版只生成诊断 Excel，不包含 `--apply` 参数。
- 不调用领星 `product/set`，不会修改任何 SKU。
- 已有 A2023 SKU 只用于回测；其他非空标签不覆盖。
- 无销量且无 Listing 的历史 SKU 保留在结果中，但排序为最低优先级 P3。

## 2. 内嵌颜色映射

用户提供的《颜色编制表.xlsx》已转换并内嵌到：

```text
lx_product_m/color_system_mapping_data.py
```

运行时不再需要上传 Excel。

映射基线：

- 共 393 行：A2023 194 行，B2024 199 行。
- B2024 有 199 个唯一代码，无同体系重复代码。
- A2023 有 177 个唯一代码，17 个代码存在重复行。
- A2023/B2024 有 58 个重叠代码。
- 同一体系同一代码按原 Excel 行序处理：第一条为主映射，后续为次级历史映射。
- 命中主映射可进入高置信度；只命中次级映射进入中置信度审阅。

## 3. SKU结构

支持：

1. 常规：`款号-颜色代码-尺码`
2. 颜色尺码粘连：`款号-颜色代码尺码`
3. 版型段：`款号-SHORT-颜色代码-尺码`
4. 同样兼容 `LONG/TALL/PETITE`
5. PCS多色套装：`NKW033-5PCS-WH-BE-SD-GY-MGR-XL`

示例：

```text
NY006-SN-40DD                  -> SN / 40DD
KZ291-SHORT-BK-S               -> SHORT / BK / S
ABC-SN40DD                     -> SN / 40DD
NKW033-5PCS-WH-BE-SD-GY-MGR-XL -> WH,BE,SD,GY,MGR / XL
```

颜色代码是否合法以内嵌颜色表为准，不只依赖固定位置。

## 4. 品名颜色规则

- Unicode全角/半角和空格统一。
- 中文颜色末尾“色”可省略。
- 最长词优先，避免把“藏蓝色”截成“蓝色”。
- `米色（米黄）`、`橙色/橘色`会拆成多个可匹配标签。
- 不把净色、纯色、撞色、拼色、花色、多色、有色、无色当作具体颜色。
- 单字错别字只生成模糊候选，不进入高置信度。

## 5. 判定规则

### 高置信度

同时满足：

1. 单颜色SKU且解析唯一；
2. 颜色代码 + 品名颜色只命中A/B其中一套的主映射；
3. 另一套不匹配。

### 中置信度

- 只命中同体系次级历史映射；
- 颜色代码只存在于一个体系，但品名没有验证；
- 多色套装的多个代码证据一致指向同一体系。

### 待定或冲突

- A/B代码和品名同时匹配；
- A/B同代码同颜色，无法区分；
- SKU代码与品名识别颜色不一致；
- SKU结构无法解析；
- PCS套装跨体系或存在未知代码。

## 6. 优先级

脚本自动发现 `sy_order.total_order_YYYY` 年度表并计算是否有历史销量：

- P1：有销量；
- P2：无销量但仍有 Listing；
- P3：无销量且无 Listing，历史老旧 SKU，最低优先级。

历史销量扫描失败时，脚本不会中断，会把销量标记为未知并继续输出。

## 7. A2023回测

当前已经写入的 A2023 SKU 会单独重新推断，并输出：

```text
A2023回测结果
```

用于观察：

- SKU结构解析成功率；
- A2023重新命中率；
- 被推断为B2024或待定的异常记录；
- 主映射、次级映射和品名冲突情况。

B2024目前没有正式真值，首次结果出来后应人工抽查高置信度B2024候选。

## 8. 输出Sheet

1. `汇总`
2. `全量判定明细`
3. `高置信度_A2023`
4. `高置信度_B2024`
5. `中置信度待审阅`
6. `冲突清单`
7. `多色套装`
8. `SKU结构异常`
9. `映射表质量`
10. `A2023回测结果`

## 9. 服务器执行

### 拉取独立分支

```bash
cd /opt/apps/lx-product-m

git fetch origin
git switch feat/color-system-name-code-matching
git pull --ff-only origin feat/color-system-name-code-matching

source .venv/bin/activate
```

### 运行测试

```bash
python -m py_compile \
  lx_product_m/color_system_mapping_data.py \
  scripts/color_system_name_code_analysis.py

python -m unittest tests.test_color_system_name_code_analysis -v
```

### 先做100条快速试跑

```bash
TEST_FILE="reports_analysis/color_system_name_code_test_$(date +%Y%m%d_%H%M%S).xlsx"

python -u scripts/color_system_name_code_analysis.py \
  --skip-sales \
  --limit 100 \
  --output "$TEST_FILE" \
  --show 20

ls -lh "$TEST_FILE"
```

### 全量dry-run

```bash
OUTPUT_FILE="reports_analysis/color_system_name_code_analysis_$(date +%Y%m%d_%H%M%S).xlsx"

python -u scripts/color_system_name_code_analysis.py \
  --output "$OUTPUT_FILE" \
  --show 30

ls -lh "$OUTPUT_FILE"
```

全量默认扫描所有可用订单年度表。只想先看匹配结果、不计算销量优先级时，可加：

```text
--skip-sales
```

## 10. 正式写入前置条件

第一版不具备写入能力。后续写入脚本仍必须满足：

- 只读取人工审阅后的Excel；
- 当前颜色体系为空才允许写；
- 当前值相同跳过、不同则冲突退出；
- 写前实时读取和完整备份；
- 保留产品名称、分类及全部自定义字段；
- 写后实时复查；
- 单条、100条、1000条、全量逐级验证。
