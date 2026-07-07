# lx-product-m

领星本地产品管理自动化项目。

本项目不是单一脚本，而是一套围绕 **领星 ERP 产品管理** 的自动化维护工具，核心目标是把飞书开发资料、领星本地产品、产品分类、SPU、多属性产品和自定义字段打通，减少手工维护成本，并保留可追溯的写入日志。

当前项目主要覆盖四条主线：

1. **产品自定义字段维护**：把飞书款号资料写入领星产品管理的自定义字段，包括 `开发年份`、`季节`、`品线`、`品类`。
2. **产品分类维护**：从飞书款号表解析目标分类，自动创建缺失分类，并把 SKU 分类写回领星。
3. **SPU 关联维护**：按 SKU 前缀生成 SPU，通过领星「多属性产品」接口创建/编辑 SPU，并把 SKU 挂到对应 SPU 下。
4. **基础数据同步与校验**：同步领星分类、本地产品快照、飞书款号匹配结果，并通过日志表记录接口调用、写入结果和异常。

所有写入型脚本默认只预览或只做单点测试；批量写入必须先验证接口行为，再显式执行 `--confirm`。

---

## 一、项目当前重点

### 1. 产品自定义字段维护

这是当前项目的主要维护方向。

需要维护的领星产品自定义字段：

| 字段 | 写入来源 | 示例 |
|---|---|---|
| 开发年份 | 从飞书开款任务或 `year_group` 解析，`26` 转成 `2026` | `2026` |
| 季节 | 飞书季节字段 | `春夏` |
| 品线 | 从开款任务解析，并清洗 `S-` 前缀 | `基础款` |
| 品类 | 从开款任务最后一段解析 | `背心/吊带` |

关键验证结论：

- 领星 `product/set` 写入 `custom_fields` 时，字段值必须使用 `val`。
- 产品详情读取时，领星返回的是 `val_text`。
- 因此写入结构是：

```json
{
  "sku": "BX547-BK-L",
  "product_name": "BX547-桃心领挂脖露背背心-黑色-L",
  "custom_fields": [
    {"id": "207714670595318273", "name": "开发年份", "val": "2026"},
    {"id": "207714670595318277", "name": "季节", "val": "春夏"},
    {"id": "207714670595318275", "name": "品线", "val": "基础款"},
    {"id": "207714671567742465", "name": "品类", "val": "背心/吊带"}
  ]
}
```

读取详情时会看到：

```json
{"id": "207714670595318277", "name": "季节", "val_text": "春夏"}
```

当前正式脚本：

```text
scripts/apply_product_custom_fields_from_feishu_v4.py
```

辅助探测脚本：

```text
probe_lx_product_custom_labels.py
scripts/probe_lx_custom_field_write_formats.py
```

最近一次 V3 全量结果：

| 批次 | 处理量 | 成功 | 失败 | 用时 |
|---|---:|---:|---:|---:|
| `custom_fields_full_20260706_180154` | 42,477 | 42,461 | 16 | 约 13小时34分 |

V4 修正点：

| 问题 | V3 | V4 |
|---|---|---|
| 写入字段值 | `val_text` 或局部替换 | 固定使用 `val` |
| 默认处理状态 | `matched` | `matched + warning` |
| 开发年份 | `26` | `2026` |
| 品线 | 可能是 `S-基础款` | 清洗成 `基础款` |
| 品类 | 可能取成分类路径末级 `S-基础款` | 从 `task_text` 最后一段取，如 `背心/吊带` |

示例：

```text
26-春夏-REORIA-基础款-背心/吊带
```

会解析为：

```text
开发年份 = 2026
季节 = 春夏
品线 = 基础款
品类 = 背心/吊带
```

---

### 2. 产品分类维护

已完成：

- 同步领星产品分类列表到 `lxpm_category`。
- 同步领星本地产品列表到 `lxpm_product_category_snapshot`。
- 从飞书多维表读取款号、开款任务、品线、季节等字段。
- 解析目标分类路径。
- 自动创建飞书匹配中缺失的领星分类。
- 将飞书款号匹配到本地产品 SKU。
- 批量写入 SKU 产品分类。
- 长任务支持 token 失效自动续约与临时错误重试。

关键结论：

- 分类新增接口 `/erp/sc/routing/storage/category/set` 的请求体必须是 `{"data": [ ... ]}`。
- 分类简码 `category_code` 最长 10 位，只允许数字和字母，不能包含 `_`。
- 产品分类写入使用 `/erp/sc/routing/storage/product/set`，写入字段为 `category_id` / `category`。

---

### 3. SPU 关联维护

关键事实：

- 领星 SPU 不是本地产品上的普通文本字段，不能像分类一样直接在 `product/set` 里塞 `spu`。
- `product/set` 的 `model` 字段已经验证为前台「型号」，不是 SPU。
- 正确主链路应走「添加/编辑多属性产品」接口：`/erp/sc/routing/storage/spu/set`。
- `spu/set` 的 `sku_list` 中若提交不存在 SKU，系统会自动创建产品，这是高风险点，正式逻辑必须先校验 SKU 存在。
- SPU 名称仅允许数字、字母、横杠、下划线；如果飞书或 SKU 前缀里含中文/特殊字符，必须先清洗或拦截。
- SKU 已经绑定其他 SPU 时，领星会返回类似 `SKU-xxx当前已经关联了YYY`，不会自动迁移。

已新增正式服务骨架：

```text
lx_product_m/services/spu_service.py     防御性 SPU 服务
probe_spu.py                             SPU 读取/接口路径探测脚本
test_spu_bind.py                         SPU 单点绑定测试脚本
```

同时保留早期脚本：

```text
scripts/apply_lx_spu_set_from_sku_prefix.py
```

---

### 4. 基础数据同步与覆盖范围

当前产品快照和飞书匹配表不是天然等价关系。

最近一次统计：

| 范围 | SKU 数 |
|---|---:|
| 产品快照总 SKU | 93,591 |
| 能关联到飞书匹配表 | 44,733 |
| `matched` | 42,477 |
| `warning` | 2,004 |
| `invalid` | 252 |
| 产品快照有，但飞书匹配表无记录 | 48,858 |

因此，自定义字段脚本默认不会覆盖所有 9.3w SKU，而是覆盖：

```sql
lxpm_product_category_snapshot p
JOIN lxpm_feishu_style_category_match m
  ON p.spu = m.style_no
WHERE m.match_status IN ('matched', 'warning')
```

剩余未覆盖 SKU 需要先判断是否应该进入飞书匹配表，或者是否属于历史款、非开发表款、异常 SKU、未维护款号等。

---

## 二、接口范围

### 1. 查询产品分类列表

```text
POST /erp/sc/routing/data/local_inventory/category
```

### 2. 添加 / 编辑产品分类

```text
POST /erp/sc/routing/storage/category/set
```

请求体结构：

```json
{
  "data": [
    {
      "parent_cid": 0,
      "title": "RS款",
      "category_code": "24SRS"
    }
  ]
}
```

### 3. 查询产品详情

```text
POST /erp/sc/routing/data/local_inventory/batchGetProductInfo
```

请求示例：

```json
{
  "skus": ["BX547-BK-L"]
}
```

### 4. 编辑本地产品

```text
POST /erp/sc/routing/storage/product/set
```

本项目用于写入产品分类和产品自定义字段。

写入分类示例：

```json
{
  "sku": "ZQZ392-AP-L",
  "product_name": "原产品品名",
  "category_id": 123,
  "category": "目标分类名称"
}
```

写入自定义字段示例：

```json
{
  "sku": "BX547-BK-L",
  "product_name": "BX547-桃心领挂脖露背背心-黑色-L",
  "custom_fields": [
    {"id": "207714670595318273", "name": "开发年份", "val": "2026"},
    {"id": "207714670595318277", "name": "季节", "val": "春夏"},
    {"id": "207714670595318275", "name": "品线", "val": "基础款"},
    {"id": "207714671567742465", "name": "品类", "val": "背心/吊带"}
  ]
}
```

### 5. 查询多属性产品列表

```text
POST /erp/sc/routing/storage/spu/spuList
```

### 6. 查询多属性产品详情

```text
POST /erp/sc/routing/storage/spu/info
```

### 7. 添加 / 编辑多属性产品

```text
POST /erp/sc/routing/storage/spu/set
```

核心请求结构：

```json
{
  "spu": "AC1022",
  "spu_name": "AC1022",
  "status": 1,
  "use_spu_template": 0,
  "sku_list": [
    {
      "sku": "AC1022-Y-XS",
      "product_name": "产品名称",
      "attribute": [
        {"pa_id": 340, "pai_id": 3909}
      ]
    }
  ]
}
```

---

## 三、服务器部署

```bash
git clone https://github.com/Digital-sy/lx-product-m.git
cd lx-product-m
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
vi .env
```

`.env` 至少配置：

```env
LINGXING_HOST=https://openapi.lingxing.com
LINGXING_APP_ID=你的app_id
LINGXING_APP_SECRET=你的app_secret
DB_HOST=你的数据库地址
DB_PORT=3306
DB_USER=你的数据库用户
DB_PASSWORD=你的数据库密码
DB_DATABASE=lingxing
```

服务器更新代码：

```bash
cd /opt/apps/lx-product-m
git pull origin main
source .venv/bin/activate
python -m compileall -q .
```

---

## 四、运行顺序：产品自定义字段维护

### 1. 更新代码

```bash
cd /opt/apps/lx-product-m
git pull origin main
source .venv/bin/activate
python -m compileall -q scripts/apply_product_custom_fields_from_feishu_v4.py
```

### 2. 单款预览

```bash
python scripts/apply_product_custom_fields_from_feishu_v4.py \
  --style-no BX547 \
  --limit 5 \
  --show 5
```

确认预览中字段类似：

```text
开发年份 = 2026
季节 = 春夏
品线 = 基础款
品类 = 背心/吊带
```

### 3. 单款小批量写入

```bash
python -u scripts/apply_product_custom_fields_from_feishu_v4.py \
  --style-no BX547 \
  --limit 5 \
  --delay 0.5 \
  --confirm
```

### 4. 读取领星详情复查

```bash
python probe_lx_product_custom_labels.py BX547-BK-L
```

确认 `custom_fields` 里 `val_text` 有值。

### 5. 后台全量覆盖 `matched + warning`

```bash
cd /opt/apps/lx-product-m
source .venv/bin/activate
mkdir -p logs

cat > /tmp/run_lx_custom_fields_v4.sh <<'SH'
cd /opt/apps/lx-product-m
source .venv/bin/activate
mkdir -p logs

BATCH_NO="custom_fields_v4_full_$(date +%Y%m%d_%H%M%S)"
LOG="logs/${BATCH_NO}.log"

echo "批次号：${BATCH_NO}"
echo "日志文件：${LOG}"
echo "开始时间：$(date '+%F %T')"

python -u scripts/apply_product_custom_fields_from_feishu_v4.py \
  --batch-no "${BATCH_NO}" \
  --statuses matched warning \
  --delay 0.5 \
  --confirm 2>&1 | tee "${LOG}"

echo "结束时间：$(date '+%F %T')"
SH

chmod +x /tmp/run_lx_custom_fields_v4.sh
tmux new-session -d -s lxpm_custom_fields_v4 '/tmp/run_lx_custom_fields_v4.sh'
```

查看进度：

```bash
tail -f $(ls -t /opt/apps/lx-product-m/logs/custom_fields_v4_full_*.log | head -1)
```

### 6. 查看批次结果

```sql
SELECT batch_no,
       status,
       COUNT(*) AS cnt,
       MIN(created_at) AS start_time,
       MAX(created_at) AS last_time
FROM lxpm_product_custom_field_change_log
WHERE batch_no LIKE 'custom_fields_v4_full_%'
GROUP BY batch_no, status
ORDER BY start_time DESC, status;
```

### 7. 查看失败原因

```sql
SELECT batch_no,
       LEFT(error_message, 1000) AS error_message,
       COUNT(*) AS cnt
FROM lxpm_product_custom_field_change_log
WHERE batch_no = (
    SELECT batch_no
    FROM lxpm_product_custom_field_change_log
    WHERE batch_no LIKE 'custom_fields_v4_full_%'
    ORDER BY id DESC
    LIMIT 1
)
AND status = 'failed'
GROUP BY batch_no, LEFT(error_message, 1000)
ORDER BY cnt DESC
LIMIT 20;
```

---

## 五、运行顺序：产品分类维护

### 1. 同步分类列表

```bash
python scripts/sync_categories.py
```

### 2. 快速同步产品快照

```bash
python scripts/sync_product_list_snapshot_fast.py
```

### 3. 同步飞书款号分类匹配

```bash
python scripts/sync_feishu_style_category_match.py \
  --app-token WD5NbNK4KaXmkgsnrydcGgv9nib \
  --table-id tblzzfcUTcD3YHtM \
  --view-id vewCCPfU7e \
  --limit 0 \
  --show 100 \
  --confirm
```

### 4. 创建飞书匹配中的缺失分类

```bash
python scripts/create_missing_lx_categories_from_feishu_match.py
python scripts/create_missing_lx_categories_from_feishu_match.py --confirm
```

### 5. 批量上传 SKU 分类

```bash
python scripts/apply_feishu_match_to_lx_products_fast.py \
  --statuses matched \
  --show 100
```

```bash
python -u scripts/apply_feishu_match_to_lx_products_fast.py \
  --statuses matched \
  --delay 0.1 \
  --max-retries 5 \
  --confirm
```

---

## 六、运行顺序：SPU 关联维护

### 1. 探测读取字段和接口路径

建议先在 ERP 前台手动给一个测试 SKU 挂 SPU，然后执行：

```bash
python probe_spu.py AC1022-Y-XS
```

确认：

- `batchGetProductInfo` 是否返回 `spu / spu_name / spu_attribute`。
- `spuList / spu/info / spu/set` 路径是否与当前文档一致。

### 2. 单点测试正式服务

```bash
python test_spu_bind.py AC1022-Y-XS AC1022
```

推荐验证顺序：

```text
1. SPU 不存在 → 应新建 SPU 并绑定成功
2. 同样参数再跑一次 → 应 skipped
3. 同一 SPU 换第二个 SKU → 应追加绑定，第一个 SKU 仍然保留
4. 不存在 SKU → 应 blocked，不应自动创建产品
5. 含中文/非法字符 SPU → 应被本地校验拦截
```

### 3. 旧脚本的单点/批量命令

旧脚本可作为临时验证工具使用：

```bash
python -u scripts/apply_lx_spu_set_from_sku_prefix.py \
  --sku AC1022-Y-XS \
  --attribute-json '[{"pa_id":340,"pai_id":3909}]' \
  --delay 0.2 \
  --max-retries 5 \
  --confirm
```

---

## 七、安全约束

- 所有写入型脚本默认只预览或单点测试，批量写入前必须先单 SKU 验证。
- `product/set` 的 `model` 字段是前台「型号」，不是 SPU，禁止用于 SPU 写入。
- 自定义字段写入使用 `custom_fields[].val`，不要使用 `val_text` 作为写入字段。
- SPU 关联应使用 `/erp/sc/routing/storage/spu/set`。
- `spu/set` 会自动创建不存在 SKU，正式服务必须先用 `batchGetProductInfo` 校验 SKU 存在。
- 编辑已有 SPU 时，`sku_list` 可能存在全量替换风险，正式服务应按防御性合并处理。
- SPU 名称仅允许数字、字母、横杠、下划线。
- `/erp/sc/routing/storage/spu/set` 令牌桶容量为 5，不建议并发多个任务。
- 长任务建议用 `tmux`。
- 所有接口调用会写入 `lxpm_api_call_log`。

---

## 八、核心数据表

### 分类与产品快照

```text
lxpm_category                         领星分类主表
lxpm_category_change_log              分类新增/编辑日志
lxpm_product_category_snapshot        本地产品快照
lxpm_product_category_task            产品分类写入任务
lxpm_product_category_change_log      产品分类写入日志
lxpm_feishu_style_category_match      飞书款号到领星分类/字段匹配表
```

### 自定义字段

```text
lxpm_product_custom_field_change_log  产品自定义字段写入日志
```

### SPU

```text
lxpm_spu_change_log                   正式 SpuService 变更日志
lxpm_spu_set_task                     早期 spu/set 直接写入任务
lxpm_spu_set_change_log               早期 spu/set 直接写入日志
lxpm_product_spu_write_task           早期 product/set SPU 测试任务，已不作为正式流程使用
lxpm_product_spu_change_log           早期 product/set SPU 测试日志，已不作为正式流程使用
```

### API 日志

```text
lxpm_api_call_log                     领星 API 调用日志
```

---

## 九、常用排查 SQL

### 1. 查看产品快照数量

```sql
SELECT COUNT(*) AS sku_cnt,
       COUNT(DISTINCT spu) AS spu_cnt
FROM lxpm_product_category_snapshot;
```

### 2. 查看飞书匹配覆盖情况

```sql
SELECT m.match_status,
       COUNT(*) AS sku_cnt,
       COUNT(DISTINCT m.style_no) AS style_cnt
FROM lxpm_feishu_style_category_match m
JOIN lxpm_product_category_snapshot p
  ON p.spu = m.style_no
WHERE p.sku IS NOT NULL
  AND p.sku <> ''
  AND p.product_name IS NOT NULL
  AND p.product_name <> ''
GROUP BY m.match_status
ORDER BY sku_cnt DESC;
```

### 3. 查看产品快照中未进入飞书匹配表的 SKU

```sql
SELECT COUNT(*) AS no_match_table_sku,
       COUNT(DISTINCT p.spu) AS no_match_table_spu
FROM lxpm_product_category_snapshot p
LEFT JOIN lxpm_feishu_style_category_match m
  ON p.spu = m.style_no
WHERE p.sku IS NOT NULL
  AND p.sku <> ''
  AND p.product_name IS NOT NULL
  AND p.product_name <> ''
  AND m.style_no IS NULL;
```

### 4. 查看某个 SKU 为什么没进入自定义字段任务

```sql
SELECT
    p.sku,
    p.spu AS snapshot_spu,
    SUBSTRING_INDEX(p.sku, '-', 1) AS sku_prefix_spu,
    p.product_name,
    CASE
        WHEN p.sku IS NULL THEN '产品快照表不存在该SKU'
        WHEN p.sku = '' THEN 'SKU为空'
        WHEN p.product_name IS NULL OR p.product_name = '' THEN 'product_name为空'
        WHEN m_by_spu.style_no IS NOT NULL THEN CONCAT('按 p.spu 命中匹配表，状态=', m_by_spu.match_status)
        WHEN m_by_prefix.style_no IS NOT NULL THEN CONCAT('按SKU前缀命中匹配表，状态=', m_by_prefix.match_status, '；但p.spu没有命中')
        ELSE '飞书匹配表无对应style_no'
    END AS not_processed_reason,
    m_by_spu.style_no AS matched_by_snapshot_spu,
    m_by_spu.match_status AS status_by_snapshot_spu,
    m_by_prefix.style_no AS matched_by_sku_prefix,
    m_by_prefix.match_status AS status_by_sku_prefix
FROM lxpm_product_category_snapshot p
LEFT JOIN lxpm_feishu_style_category_match m_by_spu
    ON p.spu = m_by_spu.style_no
LEFT JOIN lxpm_feishu_style_category_match m_by_prefix
    ON SUBSTRING_INDEX(p.sku, '-', 1) = m_by_prefix.style_no
WHERE p.sku = 'BX547-BK-L';
```

### 5. 查看自定义字段写入批次

```sql
SELECT batch_no,
       status,
       COUNT(*) AS cnt,
       MIN(created_at) AS start_time,
       MAX(created_at) AS last_time
FROM lxpm_product_custom_field_change_log
GROUP BY batch_no, status
ORDER BY start_time DESC, status
LIMIT 50;
```

### 6. 查看自定义字段失败原因

```sql
SELECT batch_no,
       LEFT(error_message, 1000) AS error_message,
       COUNT(*) AS cnt
FROM lxpm_product_custom_field_change_log
WHERE status = 'failed'
GROUP BY batch_no, LEFT(error_message, 1000)
ORDER BY batch_no DESC, cnt DESC
LIMIT 50;
```

### 7. 查看正式 SPU 服务日志

```sql
SELECT batch_no,
       sku,
       spu,
       status,
       LEFT(error_message, 1000) AS error_message,
       created_at
FROM lxpm_spu_change_log
ORDER BY id DESC
LIMIT 50;
```

### 8. 查看 API 调用日志

```sql
SELECT id, api_path, api_code, api_message, success, elapsed_ms, created_at
FROM lxpm_api_call_log
ORDER BY id DESC
LIMIT 20;
```

---

## 十、当前待办

1. 今晚执行 V4 自定义字段覆盖：`matched + warning`。
2. V4 跑完后补跑失败 SKU，重点处理连接异常、握手超时、空响应、领星内部错误。
3. 单独分析 `invalid` 的 252 个 SKU，不直接全量写入。
4. 分析产品快照中有、但飞书匹配表没有的 48,858 个 SKU，判断是否需要补充飞书款号匹配来源。
5. 检查本地产品快照是否存在 SKU 编码滞后问题，例如领星有 `BX547-BW-XL`，但本地快照只有 `BX547-WH-XL`。
6. 将稳定后的自定义字段维护流程纳入定期任务，优先每日或每周同步飞书匹配表、产品快照，再写入变更字段。
