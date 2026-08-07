"""Skill registry: scan catalog + skill-home, list/load/install/create."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SkillError(Exception):
    def __init__(self, message: str, code: str = "skill_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class SkillNotFound(SkillError):
    def __init__(self, name: str) -> None:
        super().__init__(f"skill not found: {name}", code="not_found")


_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    source: str  # "catalog" | "skill-home"
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "path": self.path,
        }


def _parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """Parse optional YAML-like frontmatter + body from SKILL.md (stdlib only)."""
    # Strip UTF-8 BOM if present (Windows editors / PowerShell Set-Content)
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    text = text.lstrip()
    if not text.startswith("---"):
        return {}, text
    rest = text[3:]
    # Allow optional whitespace after opening ---
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    end = rest.find("\n---")
    if end < 0:
        return {}, text
    meta_raw = rest[:end].strip()
    body = rest[end + 4 :]
    # closing --- may be followed by newline
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    body = body.lstrip("\n")
    meta: dict[str, Any] = {}
    for line in meta_raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        meta[key.strip()] = val
    return meta, body


def _read_text(path: Path) -> str:
    """Read skill markdown; utf-8-sig strips BOM."""
    return path.read_text(encoding="utf-8-sig")


def _validate_name(name: str) -> str:
    name = name.strip().lower()
    if not _NAME_RE.fullmatch(name):
        raise SkillError(
            f"invalid skill name: {name!r} (use lowercase, digits, hyphens)",
            code="invalid_name",
        )
    return name


class SkillRegistry:
    """
    Progressive skills: catalog packages ship with the app; installed skills
    live under ``skill_home``.
    """

    def __init__(
        self,
        catalog_dir: Path,
        skill_home: Path,
    ) -> None:
        self.catalog_dir = Path(catalog_dir).resolve()
        self.skill_home = Path(skill_home).resolve()
        self.skill_home.mkdir(parents=True, exist_ok=True)

    def _scan_dir(self, root: Path, source: str) -> dict[str, SkillInfo]:
        found: dict[str, SkillInfo] = {}
        if not root.is_dir():
            return found
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                text = _read_text(skill_md)
                meta, _body = _parse_skill_md(text)
                name = str(meta.get("name") or child.name).strip().lower()
                desc = str(meta.get("description") or "").strip()
                found[name] = SkillInfo(
                    name=name,
                    description=desc,
                    source=source,
                    path=str(skill_md),
                )
            except OSError:
                continue
        return found

    def list_skills(self) -> list[SkillInfo]:
        catalog = self._scan_dir(self.catalog_dir, "catalog")
        home = self._scan_dir(self.skill_home, "skill-home")
        # skill-home overrides catalog for same name
        merged = {**catalog, **home}
        return [merged[k] for k in sorted(merged.keys())]

    def load_skill(self, name: str) -> dict[str, Any]:
        name = _validate_name(name)
        for root, source in (
            (self.skill_home, "skill-home"),
            (self.catalog_dir, "catalog"),
        ):
            skill_md = root / name / "SKILL.md"
            if skill_md.is_file():
                text = _read_text(skill_md)
                meta, body = _parse_skill_md(text)
                return {
                    "name": str(meta.get("name") or name),
                    "description": str(meta.get("description") or ""),
                    "source": source,
                    "path": str(skill_md),
                    "body": body,
                    "raw": text.lstrip("\ufeff"),
                }
        raise SkillNotFound(name)

    def install_from_catalog(self, name: str) -> dict[str, Any]:
        """Copy a catalog skill into skill-home."""
        name = _validate_name(name)
        src = self.catalog_dir / name
        if not (src / "SKILL.md").is_file():
            raise SkillNotFound(name)
        dest = self.skill_home / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        loaded = self.load_skill(name)
        return {
            "ok": True,
            "name": name,
            "source": "skill-home",
            "path": loaded["path"],
        }

    def install_from_path(
        self, package_dir: Path, name: str | None = None
    ) -> dict[str, Any]:
        """Install a skill package directory into skill-home (local path only)."""
        package_dir = Path(package_dir).resolve()
        if not (package_dir / "SKILL.md").is_file():
            raise SkillError(
                f"no SKILL.md in {package_dir}", code="invalid_package"
            )
        text = _read_text(package_dir / "SKILL.md")
        meta, _ = _parse_skill_md(text)
        skill_name = _validate_name(
            name or str(meta.get("name") or package_dir.name)
        )
        dest = self.skill_home / skill_name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(package_dir, dest)
        return {
            "ok": True,
            "name": skill_name,
            "source": "skill-home",
            "path": str(dest / "SKILL.md"),
        }

    def create_skill(
        self,
        name: str,
        description: str,
        body: str,
        *,
        into: str = "skill-home",
    ) -> dict[str, Any]:
        """Write a new SKILL.md package (skill-creator surface)."""
        name = _validate_name(name)
        if into not in ("skill-home",):
            raise SkillError(f"unsupported target: {into}", code="invalid_target")
        root = self.skill_home / name
        root.mkdir(parents=True, exist_ok=True)
        desc = description.replace("\\", "\\\\").replace('"', '\\"')
        content = (
            f"---\n"
            f"name: {name}\n"
            f'description: "{desc}"\n'
            f"---\n\n"
            f"{body.rstrip()}\n"
        )
        skill_md = root / "SKILL.md"
        skill_md.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "name": name,
            "path": str(skill_md),
            "source": "skill-home",
        }

    def scaffold_plugin(
        self,
        target_dir: Path,
        *,
        name: str,
        version: str = "0.1.0",
        description: str = "",
    ) -> dict[str, Any]:
        """
        Create `.codex-plugin/plugin.json` under ``target_dir`` (plugin-creator).

        ``target_dir`` must already exist; caller enforces allowed roots.
        """
        name = _validate_name(name)
        target_dir = Path(target_dir).resolve()
        if not target_dir.is_dir():
            raise SkillError(
                f"target is not a directory: {target_dir}", code="invalid_target"
            )
        plugin_dir = target_dir / ".codex-plugin"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "name": name,
            "version": version,
            "description": description or f"{name} plugin",
            "schema_version": 1,
        }
        path = plugin_dir / "plugin.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        readme = target_dir / "README.md"
        if not readme.exists():
            readme.write_text(
                f"# {name}\n\nScaffolded by OpenPrism plugin-creator.\n",
                encoding="utf-8",
            )
        return {
            "ok": True,
            "name": name,
            "manifest_path": str(path),
            "target_dir": str(target_dir),
        }
