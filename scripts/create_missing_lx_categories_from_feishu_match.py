#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从飞书匹配表中找出领星不存在的分类路径，并在领星创建缺失分类。

安全规则：
1. 默认只预览，不创建。
2. 只有加 --confirm 才调用领星分类新增接口。
3. 只处理 lxpm_feishu_style_category_match 中 match_status='not_found' 的目标路径。
4. 创建完成后自动重新同步 lxpm_category。
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lx_product_m.config import settings
from lx_product_m.db import Database
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.category_service import CategoryService

MATCH_TABLE = "lxpm_feishu_style_category_match"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="根据飞书匹配表创建领星缺失分类")
    parser.add_argument("--status", default="not_found", help="要处理的匹配状态，默认 not_found")
    parser.add_argument("--max-create", type=int, default=100, help="本次最多允许创建多少个分类节点，默认100")
    parser.add_argument("--category-code-mode", choices=["blank", "title"], default="blank", help="新增分类的category_code生成方式，默认blank")
    parser.add_argument("--confirm", action="store_true", help="确认创建领星分类；不加则只预览")
    return parser.parse_args()


def split_path(path: str) -> list[str]:
    return [p.strip() for p in str(path or "").split("/") if p.strip()]


def load_existing_categories(db: Database) -> dict[str, dict]:
    rows = db.fetch_all(
        """
        SELECT cid, parent_cid, title, category_code, full_path, is_leaf
        FROM lxpm_category
        WHERE full_path IS NOT NULL AND full_path <> ''
        """
    )
    return {str(row["full_path"]): row for row in rows}


def load_missing_target_paths(db: Database, status: str) -> list[dict]:
    return db.fetch_all(
        f"""
        SELECT target_category_path,
               COUNT(*) AS row_count,
               GROUP_CONCAT(style_no ORDER BY style_no SEPARATOR ',') AS style_samples
        FROM `{MATCH_TABLE}`
        WHERE match_status = %s
          AND target_category_path IS NOT NULL
          AND target_category_path <> ''
        GROUP BY target_category_path
        ORDER BY target_category_path
        """,
        (status,),
    )


def build_create_plan(target_rows: list[dict], existing: dict[str, dict]) -> list[dict]:
    planned_paths: set[str] = set()
    sources_by_path: dict[str, list[str]] = defaultdict(list)

    for row in target_rows:
        target_path = str(row.get("target_category_path") or "").strip()
        parts = split_path(target_path)
        if not parts:
            continue
        parent_path = ""
        for idx, title in enumerate(parts):
            current_path = "/".join(parts[: idx + 1])
            if current_path not in existing and current_path not in planned_paths:
                planned_paths.add(current_path)
            sources_by_path[current_path].append(target_path)
            parent_path = current_path

    actions: list[dict] = []
    for path in sorted(planned_paths, key=lambda p: (len(split_path(p)), p)):
        parts = split_path(path)
        parent_path = "/".join(parts[:-1])
        actions.append(
            {
                "path": path,
                "title": parts[-1],
                "parent_path": parent_path,
                "level_no": len(parts),
                "source_paths": sorted(set(sources_by_path.get(path, []))),
            }
        )
    return actions


def category_code_for(title: str, path: str, mode: str) -> str:
    if mode == "title":
        return title
    return ""


def print_plan(target_rows: list[dict], actions: list[dict]) -> None:
    print("===== 领星缺失分类创建预览 =====")
    print(f"缺失目标路径数：{len(target_rows)}")
    print(f"需要创建分类节点数：{len(actions)}")
    print()
    if target_rows:
        print("--- 缺失目标路径 ---")
        for row in target_rows[:100]:
            samples = str(row.get("style_samples") or "")
            if len(samples) > 80:
                samples = samples[:80] + "..."
            print(f"{row.get('target_category_path')} | 涉及款号数={row.get('row_count')} | 样例={samples}")
        if len(target_rows) > 100:
            print(f"... 仅显示前100条，共{len(target_rows)}条")
        print()

    if actions:
        print("--- 将创建的分类节点，按父级到子级顺序 ---")
        for idx, action in enumerate(actions, 1):
            parent = action["parent_path"] or "ROOT"
            print(f"{idx:03d}. path={action['path']} | parent={parent} | title={action['title']}")


async def create_actions(actions: list[dict], existing: dict[str, dict], args: argparse.Namespace) -> None:
    db = Database()
    client = LingxingClient(db=db)
    service = CategoryService(client, db)
    token = await client.generate_token()

    created = 0
    for action in actions:
        path = action["path"]
        title = action["title"]
        parent_path = action["parent_path"]
        if path in existing:
            print(f"跳过已存在：{path}")
            continue

        parent_cid = 0
        if parent_path:
            parent = existing.get(parent_path)
            if not parent:
                raise RuntimeError(f"父级分类还不存在，无法创建：path={path}, parent_path={parent_path}")
            parent_cid = int(parent["cid"])

        category_code = category_code_for(title, path, args.category_code_mode)
        print(f"创建分类：parent_cid={parent_cid}, title={title}, category_code={category_code!r}, path={path}")
        result = await service.upsert_category(
            token=token,
            title=title,
            category_code=category_code,
            parent_cid=parent_cid,
            cid=None,
        )
        data = result.get("data") or []
        new_cid = None
        if data and isinstance(data[0], dict):
            new_cid = int(data[0].get("id") or data[0].get("cid") or 0) or None
        if not new_cid:
            raise RuntimeError(f"分类创建成功但未返回分类ID：{result}")
        existing[path] = {
            "cid": new_cid,
            "parent_cid": parent_cid,
            "title": title,
            "category_code": category_code,
            "full_path": path,
            "is_leaf": 1,
        }
        created += 1
        await asyncio.sleep(settings.collection_delay_seconds)

    print(f"创建完成：{created} 个分类节点。开始重新同步领星分类列表...")
    rows = await service.fetch_categories(token)
    saved = service.save_categories(rows)
    print(f"分类同步完成：接口返回 {len(rows)} 条，入库 {saved} 条。")


async def main() -> None:
    args = parse_args()
    db = Database()
    existing = load_existing_categories(db)
    target_rows = load_missing_target_paths(db, args.status)
    actions = build_create_plan(target_rows, existing)
    print_plan(target_rows, actions)

    if not actions:
        print("无需创建分类节点。")
        return
    if len(actions) > args.max_create:
        raise SystemExit(f"本次计划创建 {len(actions)} 个节点，超过 --max-create={args.max_create}，请提高上限或先检查数据。")
    if not args.confirm:
        print("\n当前为预览模式，未调用领星。确认无误后加 --confirm 创建分类。")
        return

    await create_actions(actions, existing, args)
    print("\n下一步：重新执行 sync_feishu_style_category_match.py --confirm，把 not_found 重新匹配成 matched/warning。")


if __name__ == "__main__":
    asyncio.run(main())
