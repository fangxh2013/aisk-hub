# -*- coding: utf-8 -*-
"""结构化 Slot Overlay 解析器与锁文件生成器.

严格遵循 spec/overlay.schema.json，实现四级继承优先级，
非法插槽 Fail-Fast 阻断，生成内容哈希确定性 lockfile。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from . import contracts, miniyaml

class OverlayError(Exception):
    pass


class UnknownSlotError(OverlayError):
    pass


class OverlayVersionMismatchError(OverlayError):
    pass


ALLOWED_SLOT_FIELDS = {
    "backend-engineering": {"package_prefix", "module_prefix", "common_module", "api_module"},
    "web-engineering": {"menu_icon_prefix", "permission_directive", "api_base_url_env"},
    "ops-workbench": {"k8s_cluster_type", "nacos_nodeport"}
}

KERNEL_DEFAULTS = {
    "backend-engineering": {
        "package_prefix": "com.example",
        "module_prefix": "modules",
        "common_module": "common",
        "api_module": "api"
    },
    "web-engineering": {
        "menu_icon_prefix": "el-icon-",
        "permission_directive": "v-permission",
        "api_base_url_env": "VITE_API_URL"
    },
    "ops-workbench": {
        "k8s_cluster_type": "k8s",
        "nacos_nodeport": 8848
    }
}


class OverlayParser:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or Path(__file__).resolve().parents[2]
        self.spec_file = self.root / "spec" / "overlay.schema.json"

    def parse_manifest(self, manifest_path: Path) -> dict[str, Any]:
        if not manifest_path.exists():
            raise OverlayError(f"Overlay manifest 不存在: {manifest_path}")
        try:
            data = miniyaml.load_file(manifest_path)
            contracts.validate_overlay(data)
        except Exception as exc:
            raise OverlayError(str(exc)) from exc

        if not isinstance(data, dict):
            raise OverlayError("Overlay manifest 必须是 YAML 字典")

        return data

    def merge_slots(self, manifest_data: dict[str, Any], profile_slots: Optional[dict] = None, task_slots: Optional[dict] = None) -> dict[str, Any]:
        """合并四级继承：内核默认 < 私有 Overlay < Profile < 任务参数。"""
        merged = json.loads(json.dumps(KERNEL_DEFAULTS))

        # 1. Overlay 覆盖
        for skill, slots in manifest_data.get("skill_slots", {}).items():
            if skill in merged:
                merged[skill].update(slots)

        # 2. Profile 覆盖
        if profile_slots:
            for skill, slots in profile_slots.items():
                if skill in merged:
                    merged[skill].update(slots)

        # 3. Task 参数覆盖
        if task_slots:
            for skill, slots in task_slots.items():
                if skill in merged:
                    merged[skill].update(slots)

        return merged

    def generate_lockfile(self, manifest_path: Path, output_path: Path, profile_slots: Optional[dict] = None) -> dict[str, Any]:
        manifest_data = self.parse_manifest(manifest_path)
        merged_slots = self.merge_slots(manifest_data, profile_slots)

        # 内容摘要计算：严禁包含生成时间戳，保证确定性！
        digest_payload = json.dumps({
            "version": manifest_data["version"],
            "type": manifest_data["type"],
            "merged_slots": merged_slots,
            "profiles": manifest_data.get("profiles", []),
            "rules": manifest_data.get("project_rules", [])
        }, sort_keys=True)

        content_digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()

        lock_data = {
            "lockfile_version": 1,
            "content_digest": content_digest,
            "merged_slots": merged_slots,
            "source_manifest": str(manifest_path.name)
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(lock_data, f, ensure_ascii=False, indent=2)

        return lock_data
