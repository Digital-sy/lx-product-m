# lx-product-m

领星本地产品维护自动化项目。

当前项目已覆盖两条主线：

1. **产品分类维护**：从飞书款号表解析目标分类，自动创建缺失分类，并把 SKU 分类写回领星。
2. **SPU 关联维护**：按 SKU 前缀生成 SPU，通过领星多属性产品接口创建/编辑 SPU，并把 SKU 挂到对应 SPU 下。

所有写入型脚本默认只预览，必须显式加 `--confirm` 才会调用领星写接口。

---

## 一、当前进度

### 1. 领星产品分类维护

已完成：

- 同步领星产品分类列表到 `lxpm_category`。
- 同步领星本地产品列表到 `lxpm_product_category_snapshot`。
- 从飞书多维表读取款号、开款任务、品线、季节等字段。
- 解析目标分类路径：`年份 / 季节 / 品线`。
- 自动创建飞书匹配中缺失的领星分类。
- 将飞书款号匹配到本地产品 SKU。
- 快速批量写入 SKU 产品分类。
- 长任务支持 token 失效自动续约与临时错误重试。

重要结论：

- 分类新增接口 `/erp/sc/routing/storage/category/set` 的请求体必须是 `{"data": [ ... ]}`。
- 分类简码 `category_code` 最长 10 位，只允许数字和字母，不能包含 `_`。
- 产品分类写入使用 `/erp/sc/routing/storage/product/set`，写入字段为 `category_id` / `category`。

### 2. SPU 关联维护

已完成：

- 发现 `product/set` 中的 `model` 字段实际写入前台「型号」，不是 SPU。
- 确认 `product/set` 文档中的 `api_spu` / `api_spu_attribute` 用于产品 SPU 关联，但必须同时传入。
- 最终确认应使用多属性产品接口 `/erp/sc/routing/storage/spu/set`。
- 已新增脚本 `scripts/apply_lx_spu_set_from_sku_prefix.py`。
- 单个 SKU 测试已跑通。
- 当前进入全量写入阶段。

SPU 写入逻辑：

```text
AC1022-Y-XS   -> SPU = AC1022
ZSY961-SC-XS  -> SPU = ZSY961
```

`spu/set` 核心请求结构：

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

当前已验证的属性参数：

```json
[{"pa_id": 340, "pai_id": 3909}]
```

注意：如果该属性代表具体颜色/尺码，而不是通用属性，后续需要改成按 SKU 解析不同属性值。

---

## 二、接口范围

### 1. 查询产品分类列表

```text
POST /erp/sc/routing/data/local_inventory/category
```

核心返回字段：

```text
cid            分类ID
parent_cid     父级分类ID
title          分类名称
category_code  分类简码
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
  "skus": ["ZQZ392-AP-L"]
}
```

### 4. 编辑本地产品分类

```text
POST /erp/sc/routing/storage/product/set
```

本项目用于写入产品分类：

```json
{
  "sku": "ZQZ392-AP-L",
  "product_name": "原产品品名",
  "category_id": 123,
  "category": "目标分类名称"
}
```

`category_id` 优先于 `category`。

### 5. 添加 / 编辑多属性产品 SPU

```text
POST /erp/sc/routing/storage/spu/set
```

本项目用于创建/编辑 SPU 并关联 SKU：

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
DB_HOST=rm-wz91237y91oasq45fco.mysql.rds.aliyuncs.com
DB_PORT=3306
DB_USER=SYSJ001
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

## 四、运行顺序：分类维护

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

先预览：

```bash
python scripts/create_missing_lx_categories_from_feishu_match.py
```

确认创建：

```bash
python scripts/create_missing_lx_categories_from_feishu_match.py --confirm
```

### 5. 批量上传 SKU 分类

先预览：

```bash
python scripts/apply_feishu_match_to_lx_products_fast.py \
  --statuses matched \
  --show 100
```

小批量测试：

```bash
python -u scripts/apply_feishu_match_to_lx_products_fast.py \
  --statuses matched \
  --limit 100 \
  --delay 0.1 \
  --max-retries 5 \
  --confirm
```

全量写入：

```bash
python -u scripts/apply_feishu_match_to_lx_products_fast.py \
  --statuses matched \
  --delay 0.1 \
  --max-retries 5 \
  --confirm
```

处理 warning：

```bash
python -u scripts/apply_feishu_match_to_lx_products_fast.py \
  --statuses warning \
  --delay 0.1 \
  --max-retries 5 \
  --confirm
```

---

## 五、运行顺序：SPU 关联维护

### 1. 单个 SKU 预览

```bash
python scripts/apply_lx_spu_set_from_sku_prefix.py \
  --sku AC1022-Y-XS \
  --attribute-json '[{"pa_id":340,"pai_id":3909}]' \
  --show 20
```

### 2. 单个 SKU 写入测试

```bash
python -u scripts/apply_lx_spu_set_from_sku_prefix.py \
  --sku AC1022-Y-XS \
  --attribute-json '[{"pa_id":340,"pai_id":3909}]' \
  --delay 0.2 \
  --max-retries 5 \
  --confirm
```

### 3. 小批量测试

```bash
python -u scripts/apply_lx_spu_set_from_sku_prefix.py \
  --limit 20 \
  --attribute-json '[{"pa_id":340,"pai_id":3909}]' \
  --delay 0.2 \
  --max-retries 5 \
  --confirm
```

### 4. 全量写入

建议使用 tmux，避免 SSH/浏览器断开导致任务中断：

```bash
tmux new-session -d -s lxpm_spu_set_all '
cd /opt/apps/lx-product-m &&
source .venv/bin/activate &&
mkdir -p logs &&
LOG="logs/spu_set_all_$(date +%Y%m%d_%H%M%S).log" &&
python -u scripts/apply_lx_spu_set_from_sku_prefix.py \
  --attribute-json '\''[{"pa_id":340,"pai_id":3909}]'\'' \
  --delay 0.2 \
  --max-retries 5 \
  --confirm 2>&1 | tee "$LOG"
'
```

查看任务：

```bash
tmux attach -t lxpm_spu_set_all
```

退出但不停止：

```text
Ctrl + B，然后按 D
```

查看日志：

```bash
tail -f /opt/apps/lx-product-m/logs/spu_set_all_*.log
```

---

## 六、安全约束

- 所有写入型脚本默认只预览，不会写领星。
- 必须加 `--confirm` 才会调用领星写入接口。
- 分类写入、SPU 写入都需要先单个或小批量测试。
- `product/set` 的 `model` 字段是前台「型号」，不是 SPU，禁止用于 SPU 写入。
- SPU 关联应使用 `/erp/sc/routing/storage/spu/set`。
- `/erp/sc/routing/storage/spu/set` 令牌桶容量为 5，不建议并发多个全量任务。
- 长任务建议用 `tmux`。
- 所有接口调用会写入 `lxpm_api_call_log`。

---

## 七、核心数据表

### 分类相关

```text
lxpm_category                         领星分类主表
lxpm_category_change_log              分类新增/编辑日志
lxpm_product_category_snapshot        本地产品快照
lxpm_product_category_task            产品分类写入任务
lxpm_product_category_change_log      产品分类写入日志
lxpm_feishu_style_category_match      飞书款号到领星分类匹配表
lxpm_api_call_log                     领星 API 调用日志
```

### SPU 相关

```text
lxpm_spu_set_task                     spu/set 写入任务
lxpm_spu_set_change_log               spu/set 写入日志
lxpm_product_spu_write_task           早期 product/set SPU 测试任务，已不作为正式流程使用
lxpm_product_spu_change_log           早期 product/set SPU 测试日志，已不作为正式流程使用
```

---

## 八、常用排查 SQL

### 1. 查看分类

```sql
SELECT cid, parent_cid, title, category_code, full_path, is_leaf
FROM lxpm_category
ORDER BY parent_cid, cid;
```

### 2. 查看产品快照数量

```sql
SELECT COUNT(*) AS sku_cnt,
       COUNT(DISTINCT spu) AS spu_cnt
FROM lxpm_product_category_snapshot;
```

### 3. 查看飞书匹配状态

```sql
SELECT match_status, COUNT(*) AS cnt
FROM lxpm_feishu_style_category_match
GROUP BY match_status
ORDER BY cnt DESC;
```

### 4. 查看产品分类上传批次

```sql
SELECT batch_no,
       COUNT(*) AS total,
       SUM(status = 'pending') AS pending_cnt,
       SUM(status = 'running') AS running_cnt,
       SUM(status = 'write_success') AS write_success_cnt,
       SUM(status = 'success') AS success_cnt,
       SUM(status = 'failed') AS failed_cnt,
       SUM(status = 'verify_failed') AS verify_failed_cnt,
       MIN(created_at) AS start_time,
       MAX(updated_at) AS last_update_time
FROM lxpm_product_category_task
GROUP BY batch_no
ORDER BY last_update_time DESC
LIMIT 10;
```

### 5. 查看 SPU 写入批次

```sql
SELECT batch_no,
       COUNT(*) AS total,
       SUM(status = 'pending') AS pending_cnt,
       SUM(status = 'running') AS running_cnt,
       SUM(status = 'write_success') AS write_success_cnt,
       SUM(status = 'success') AS success_cnt,
       SUM(status = 'failed') AS failed_cnt,
       SUM(status = 'verify_failed') AS verify_failed_cnt,
       MIN(created_at) AS start_time,
       MAX(updated_at) AS last_update_time
FROM lxpm_spu_set_task
GROUP BY batch_no
ORDER BY last_update_time DESC
LIMIT 10;
```

### 6. 查看 SPU 写入失败原因

```sql
SELECT batch_no,
       spu,
       sku,
       status,
       LEFT(error_message, 1000) AS error_message,
       updated_at
FROM lxpm_spu_set_task
WHERE status IN ('failed', 'verify_failed')
ORDER BY updated_at DESC
LIMIT 50;
```

### 7. 查看 API 调用日志

```sql
SELECT id, api_path, api_code, api_message, success, elapsed_ms, created_at
FROM lxpm_api_call_log
ORDER BY id DESC
LIMIT 20;
```

---

## 九、当前待办

1. 等待 SPU 全量任务完成。
2. 检查 `lxpm_spu_set_task` 最近批次的 `failed` / `verify_failed`。
3. 如失败少量，按失败 SKU 补跑。
4. 如果发现 `[{"pa_id":340,"pai_id":3909}]` 不是通用属性，需要按 SKU 解析真实属性值后重构 SPU 写入逻辑。
5. 分类 warning 状态可在分类全量完成后单独评估是否批量处理。
