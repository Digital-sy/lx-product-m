# lx-product-m

领星产品分类维护项目。

第一期目标：

1. 查询领星产品分类列表，落库到 `lxpm_category`。
2. 查询指定 SKU 当前产品分类，落库到 `lxpm_product_category_snapshot`。
3. 修改指定 SKU 的产品分类，并写入 `lxpm_product_category_change_log`。
4. 新增/编辑领星产品分类，并写入 `lxpm_category_change_log`。

## 一、接口范围

### 1. 查询产品分类列表

```text
POST /erp/sc/routing/data/local_inventory/category
```

请求示例：

```json
{
  "offset": 0,
  "length": 1000,
  "ids": [1, 2, 3]
}
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

新增分类不传 `id`；编辑分类传 `id`，该 `id` 对应分类列表接口的 `cid`。

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

### 4. 编辑产品分类

```text
POST /erp/sc/routing/storage/product/set
```

本项目只写入产品分类字段：

```json
{
  "sku": "ZQZ392-AP-L",
  "product_name": "原产品品名",
  "category_id": 123,
  "category": "目标分类名称"
}
```

`category_id` 优先于 `category`。

## 二、服务器部署

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

## 三、运行顺序

### 1. 同步分类列表

```bash
python scripts/sync_categories.py
```

只同步指定分类 ID：

```bash
python scripts/sync_categories.py --ids 1 2 3
```

### 2. 查询 SKU 当前分类

```bash
python scripts/query_product_category.py --sku ZQZ392-AP-L
```

多个 SKU：

```bash
python scripts/query_product_category.py --sku ZQZ392-AP-L --sku ABC123-XX-S
```

从文件读取：

```bash
python scripts/query_product_category.py --file skus.txt
```

### 3. 修改单个 SKU 分类

先预览，不写入：

```bash
python scripts/update_product_category.py --sku ZQZ392-AP-L --category-id 123
```

确认写入：

```bash
python scripts/update_product_category.py --sku ZQZ392-AP-L --category-id 123 --confirm
```

也可以按分类名称写入，但如果名称重复会报错，建议优先用分类 ID：

```bash
python scripts/update_product_category.py --sku ZQZ392-AP-L --category-name 连体衣 --confirm
```

### 4. 新增 / 编辑分类

新增分类先预览：

```bash
python scripts/upsert_category.py --parent-cid 0 --title 连体衣 --category-code BODYSUIT
```

确认新增：

```bash
python scripts/upsert_category.py --parent-cid 0 --title 连体衣 --category-code BODYSUIT --confirm
```

编辑分类：

```bash
python scripts/upsert_category.py --id 123 --parent-cid 0 --title 连体衣 --category-code BODYSUIT --confirm
```

## 四、安全约束

- `update_product_category.py` 和 `upsert_category.py` 默认只预览，不会写领星。
- 必须加 `--confirm` 才会调用写入接口。
- 所有接口调用会写入 `lxpm_api_call_log`。
- 产品分类写入后会再次查询产品详情做复查，复查不一致会记录为 `verify_failed`。
- 分类维护接口令牌桶容量为 1，严禁并发批量调用。

## 五、常用排查 SQL

查看分类：

```sql
SELECT cid, parent_cid, title, category_code, full_path, is_leaf
FROM lxpm_category
ORDER BY parent_cid, cid;
```

查看某 SKU 当前分类快照：

```sql
SELECT *
FROM lxpm_product_category_snapshot
WHERE sku = 'ZQZ392-AP-L';
```

查看产品分类修改日志：

```sql
SELECT id, sku, old_category_id, old_category_name,
       new_category_id, new_category_name,
       verify_category_id, verify_category_name,
       status, error_message, created_at
FROM lxpm_product_category_change_log
ORDER BY id DESC
LIMIT 20;
```

查看 API 调用日志：

```sql
SELECT id, api_path, api_code, api_message, success, elapsed_ms, created_at
FROM lxpm_api_call_log
ORDER BY id DESC
LIMIT 20;
```
