# lx-product-m

领星本地产品维护自动化项目。

当前项目覆盖两条主线：

1. **产品分类维护**：从飞书款号表解析目标分类，自动创建缺失分类，并把 SKU 分类写回领星。
2. **SPU 关联维护**：按 SKU 前缀生成 SPU，通过领星「多属性产品」接口创建/编辑 SPU，并把 SKU 挂到对应 SPU 下。

所有写入型脚本默认只预览或只做单点测试；批量写入必须先验证接口行为，再显式执行。

---

## 一、当前进度

### 1. 产品分类维护

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

关键事实：

- 领星 SPU 不是本地产品上的普通文本字段，不应像分类一样直接在 `product/set` 里塞 `spu`。
- `product/set` 的 `model` 字段已经验证为前台「型号」，不是 SPU。
- `product/set` 文档中的 `api_spu` / `api_spu_attribute` 要求同时传，但更适合产品关联补充，不是完整 SPU 主流程。
- 正确主链路应走「添加/编辑多属性产品」接口：`/erp/sc/routing/storage/spu/set`。
- `spu/set` 的 `sku_list` 中若提交不存在 SKU，系统会自动创建产品，这是高风险点，正式逻辑必须先校验 SKU 存在。
- SPU 名称仅允许数字、字母、横杠、下划线；如果飞书或 SKU 前缀里含中文/特殊字符，必须先清洗或拦截。

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

该脚本已验证能调用 `spu/set` 跑通单个 SKU，但它没有完整防御性合并逻辑。若正在全量跑，需重点确认 `spu/set` 编辑已有 SPU 时 `sku_list` 是追加还是全量替换。推荐后续以 `SpuService` 为正式入口。

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

## 五、运行顺序：SPU 关联维护

### 1. 先停止旧全量任务并确认状态

如果正在跑旧的 `apply_lx_spu_set_from_sku_prefix.py` 全量任务，先暂停，避免在未确认 `sku_list` 编辑语义前继续写：

```bash
ps -ef | grep apply_lx_spu_set_from_sku_prefix | grep -v grep
kill 进程号
```

如果在 tmux 里运行：

```bash
tmux attach -t lxpm_spu_set_all
```

进入后按 `Ctrl+C` 停止任务，再 `Ctrl+B`、`D` 退出 tmux。

### 2. 探测读取字段和接口路径

建议先在 ERP 前台手动给一个测试 SKU 挂 SPU，然后执行：

```bash
python probe_spu.py AC1022-Y-XS
```

确认：

- `batchGetProductInfo` 是否返回 `spu / spu_name / spu_attribute`。
- `spuList / spu/info / spu/set` 路径是否与当前文档一致。

### 3. 单点测试正式服务

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

### 4. 旧脚本的单点/批量命令

旧脚本可作为临时验证工具使用：

```bash
python -u scripts/apply_lx_spu_set_from_sku_prefix.py \
  --sku AC1022-Y-XS \
  --attribute-json '[{"pa_id":340,"pai_id":3909}]' \
  --delay 0.2 \
  --max-retries 5 \
  --confirm
```

不建议在未确认 `sku_list` 编辑语义前继续全量运行旧脚本。

---

## 六、安全约束

- 所有写入型脚本默认只预览或单点测试，批量写入前必须先单 SKU 验证。
- `product/set` 的 `model` 字段是前台「型号」，不是 SPU，禁止用于 SPU 写入。
- SPU 关联应使用 `/erp/sc/routing/storage/spu/set`。
- `spu/set` 会自动创建不存在 SKU，正式服务必须先用 `batchGetProductInfo` 校验 SKU 存在。
- 编辑已有 SPU 时，`sku_list` 可能是全量替换，正式服务按防御性合并处理。
- SPU 名称仅允许数字、字母、横杠、下划线。
- `/erp/sc/routing/storage/spu/set` 令牌桶容量为 5，不建议并发多个任务。
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
lxpm_spu_change_log                   正式 SpuService 变更日志
lxpm_spu_set_task                     早期 spu/set 直接写入任务
lxpm_spu_set_change_log               早期 spu/set 直接写入日志
lxpm_product_spu_write_task           早期 product/set SPU 测试任务，已不作为正式流程使用
lxpm_product_spu_change_log           早期 product/set SPU 测试日志，已不作为正式流程使用
```

---

## 八、常用排查 SQL

### 1. 查看产品快照数量

```sql
SELECT COUNT(*) AS sku_cnt,
       COUNT(DISTINCT spu) AS spu_cnt
FROM lxpm_product_category_snapshot;
```

### 2. 查看产品分类上传批次

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

### 3. 查看早期 spu/set 批次

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

### 4. 查看正式 SPU 服务日志

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

### 5. 查看 API 调用日志

```sql
SELECT id, api_path, api_code, api_message, success, elapsed_ms, created_at
FROM lxpm_api_call_log
ORDER BY id DESC
LIMIT 20;
```

---

## 九、当前待办

1. 暂停或检查旧全量 SPU 任务，确认是否存在失败或替换风险。
2. 使用 `probe_spu.py` 确认当前账号开放接口路径和详情返回字段。
3. 使用 `test_spu_bind.py` 完成 5 步单点验证。
4. 确认 `sku_list` 编辑语义：追加还是全量替换。
5. 如 `attribute=[]` 或空值占位不符合业务，再扩展 `SpuService` 支持传入真实属性映射。
6. 单点测试稳定后，再开发基于 `SpuService.bind_batch()` 的飞书/快照批量绑定脚本。
