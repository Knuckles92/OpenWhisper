"""SEP-2640 MCP Skills extension (``io.modelcontextprotocol/skills``) and skill bundle delivery.

Exposes OpenWhisper's bundled Agent Skill package (``skills/openwhisper/``)
through three complementary mechanisms so every MCP client can use it:

1. **SEP-2640 Skills over MCP** (``io.modelcontextprotocol/skills``):
   - Advertises ``capabilities.resources = {}`` and
     ``capabilities.extensions["io.modelcontextprotocol/skills"] = {}`` in
     ``initialize`` and ``server/discover``.
   - Serves ``skills/list`` and ``skills/get`` with parsed YAML frontmatter and
     a deterministic file manifest containing ``uri``, ``digest``
     (``sha256:<hex>``), and exact UTF-8 byte ``size``.
2. **Standard MCP Resources** (``skill://`` URI scheme):
   - Registers every skill file (``skill://openwhisper/SKILL.md``,
     ``skill://openwhisper/references/*.md``) plus ``skill://index.json`` on
     the MCP server so clients with standard ``resources/list`` and
     ``resources/read`` support can read the skill even before implementing
     SEP-2640.
3. **Authenticated HTTP Skill Bundle** (``/v1/skills`` and
   ``/v1/skills/openwhisper.zip``):
   - Lets an agent or user download the ready-to-install skill directory archive
     in one request using the same Bearer token.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from config import bundle_root

logger = logging.getLogger(__name__)

EXTENSION_ID = "io.modelcontextprotocol/skills"
SKILL_SLUG = "openwhisper"
INDEX_URI = "skill://index.json"
DEFAULT_TTL_MS = 5 * 60 * 1000
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class SkillResourceFile:
    """One file inside a bundled skill package."""

    rel_path: str
    uri: str
    mime_type: str
    content: bytes
    size: int
    digest: str

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "digest": self.digest,
            "size": self.size,
        }


@dataclass(frozen=True)
class SkillBundle:
    """One loaded Agent Skill directory (``SKILL.md`` + supporting files)."""

    slug: str
    uri: str
    frontmatter: Mapping[str, Any]
    files: tuple[SkillResourceFile, ...]

    @property
    def name(self) -> str:
        return str(self.frontmatter.get("name") or self.slug)

    @property
    def description(self) -> str:
        return str(self.frontmatter.get("description") or "")

    def to_sep_entry(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "frontmatter": dict(self.frontmatter),
            "resources": [f.manifest_entry() for f in self.files],
        }


def _unquote_yaml_scalar(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        inner = value[1:-1]
        if value[0] == '"':
            return inner.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        return inner.replace("''", "'")
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("null", "~"):
        return None
    return value


def parse_frontmatter(markdown: str) -> dict[str, Any]:
    """Parse simple YAML frontmatter from ``SKILL.md`` into a JSON-safe dict.

    Supports scalar keys, folded/literal multiline blocks (``>``, ``>-``, ``|``,
    ``|-``), and one level of nested mapping (such as ``metadata:``), with zero
    external YAML dependency.
    """
    match = _FRONTMATTER_RE.match(markdown)
    if not match:
        return {}
    block = match.group(1)
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(block)
        if isinstance(loaded, dict):
            return _json_safe(loaded)
    except Exception:
        pass

    lines = block.splitlines()
    result: dict[str, Any] = {}
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if not line.strip() or line.lstrip().startswith("#"):
            idx += 1
            continue
        if ":" not in line or line.startswith((" ", "\t")):
            idx += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest_stripped = rest.strip()
        idx += 1
        if rest_stripped in (">", ">-", "|", "|-"):
            folded = rest_stripped.startswith(">")
            buf: list[str] = []
            while idx < len(lines) and (lines[idx].startswith((" ", "\t")) or not lines[idx].strip()):
                buf.append(lines[idx].strip())
                idx += 1
            result[key] = (" ".join(part for part in buf if part) if folded else "\n".join(buf)).strip()
        elif not rest_stripped:
            nested: dict[str, Any] = {}
            while idx < len(lines) and (lines[idx].startswith((" ", "\t")) or not lines[idx].strip()):
                sub = lines[idx].strip()
                idx += 1
                if not sub or sub.startswith("#") or ":" not in sub:
                    continue
                sub_key, _, sub_val = sub.partition(":")
                nested[sub_key.strip()] = _unquote_yaml_scalar(sub_val)
            result[key] = nested
        else:
            result[key] = _unquote_yaml_scalar(rest_stripped)
    return result


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _mime_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return "text/markdown"
    if suffix == ".json":
        return "application/json"
    if suffix in (".txt", ".py", ".sh", ".ps1", ".yaml", ".yml"):
        return "text/plain"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def skills_root(root: str | Path | None = None) -> Path:
    """Directory holding bundled Agent Skill folders (``<bundle_root>/skills``)."""
    if root is not None:
        return Path(root)
    return Path(bundle_root()) / "skills"


def load_skills(root: str | Path | None = None) -> tuple[SkillBundle, ...]:
    """Discover and load all valid Agent Skill directories under ``root``."""
    base = skills_root(root)
    if not base.is_dir():
        return ()

    bundles: list[SkillBundle] = []
    for child in sorted(base.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.is_symlink() or not _SLUG_RE.match(child.name):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file() or skill_md.is_symlink():
            continue

        slug = child.name
        files: list[SkillResourceFile] = []
        for path in sorted(child.rglob("*"), key=lambda p: p.relative_to(child).as_posix()):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(child).as_posix()
            # Never expose hidden/dotfiles or compiled artifacts.
            if any(part.startswith(".") or part == "__pycache__" for part in rel.split("/")):
                continue
            raw = path.read_bytes()
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            uri = f"skill://{slug}/{rel}"
            files.append(
                SkillResourceFile(
                    rel_path=rel,
                    uri=uri,
                    mime_type=_mime_type_for(path),
                    content=raw,
                    size=len(raw),
                    digest=digest,
                )
            )
        if not files:
            continue
        # Ensure SKILL.md is always first in the manifest.
        files.sort(key=lambda item: (0 if item.rel_path == "SKILL.md" else 1, item.rel_path))
        frontmatter = parse_frontmatter(files[0].text)
        frontmatter.setdefault("name", slug)
        bundles.append(
            SkillBundle(
                slug=slug,
                uri=f"skill://{slug}/SKILL.md",
                frontmatter=frontmatter,
                files=tuple(files),
            )
        )
    bundles.sort(key=lambda b: b.uri)
    return tuple(bundles)


def build_index_json(bundles: tuple[SkillBundle, ...]) -> str:
    """Build the ``skill://index.json`` catalog document."""
    payload = {
        "extension": EXTENSION_ID,
        "skills": [bundle.to_sep_entry() for bundle in bundles],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def build_skill_zip(bundle: SkillBundle) -> bytes:
    """Build a deterministic ZIP archive containing ``<slug>/SKILL.md`` and its files."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in bundle.files:
            info = zipfile.ZipInfo(
                filename=f"{bundle.slug}/{item.rel_path}",
                date_time=(2026, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, item.content)
    return buffer.getvalue()


def install(mcp: Any, app: Any = None, *, root: str | Path | None = None) -> tuple[SkillBundle, ...]:
    """Install SEP-2640 Skills over MCP, ``skill://`` resources, and optional HTTP bundle routes."""
    from mcp.shared.exceptions import MCPError
    from mcp.types import RequestParams
    from pydantic import ConfigDict

    bundles = load_skills(root)
    if not bundles:
        logger.debug("No bundled OpenWhisper skills found under %s", skills_root(root))
        return ()

    by_uri = {bundle.uri: bundle for bundle in bundles}
    by_slug = {bundle.slug: bundle for bundle in bundles}
    index_text = build_index_json(bundles)

    # 1. Standard MCP Resources (skill:// URI scheme) so clients with standard
    #    resources/list & resources/read can read every skill file and index.json.
    def _register_text_resource(uri: str, name: str, description: str, mime_type: str, text: str) -> None:
        def reader() -> str:
            return text

        reader.__name__ = f"read_{re.sub(r'[^a-zA-Z0-9_]', '_', name)}"
        mcp.resource(
            uri,
            name=name,
            description=description,
            mime_type=mime_type,
        )(reader)

    _register_text_resource(
        INDEX_URI,
        "skills-index",
        "Catalog of bundled OpenWhisper Agent Skills with SHA-256 digests.",
        "application/json",
        index_text,
    )
    for bundle in bundles:
        for item in bundle.files:
            desc = (
                bundle.description
                if item.rel_path == "SKILL.md"
                else f"Reference document for the {bundle.name} skill ({item.rel_path})."
            )
            _register_text_resource(
                item.uri,
                f"{bundle.slug}/{item.rel_path}",
                desc,
                item.mime_type,
                item.text,
            )

    # 2. SEP-2640 JSON-RPC handlers: skills/list and skills/get.
    class SkillsParams(RequestParams):
        model_config = ConfigDict(extra="allow")

    async def handle_skills_list(_ctx: Any, params: SkillsParams) -> dict[str, Any]:
        body = dict(params.model_extra or {})
        cursor = body.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or cursor != ""):
            raise MCPError(INVALID_PARAMS, "Invalid skills/list cursor.")
        return {
            "resultType": "complete",
            "skills": [bundle.to_sep_entry() for bundle in bundles],
            "ttlMs": DEFAULT_TTL_MS,
            "cacheScope": "private",
        }

    async def handle_skills_get(_ctx: Any, params: SkillsParams) -> dict[str, Any]:
        body = dict(params.model_extra or {})
        uri = body.get("uri")
        if not isinstance(uri, str) or not uri.strip():
            raise MCPError(INVALID_PARAMS, "skills/get requires a skill://.../SKILL.md uri.")
        bundle = by_uri.get(uri.strip())
        if bundle is None:
            raise MCPError(INVALID_PARAMS, f"Unknown skill URI: {uri[:120]}")
        return {
            "resultType": "complete",
            "skill": bundle.to_sep_entry(),
            "ttlMs": DEFAULT_TTL_MS,
            "cacheScope": "private",
        }

    server = mcp._lowlevel_server
    server.add_request_handler("skills/list", SkillsParams, handle_skills_list)
    server.add_request_handler("skills/get", SkillsParams, handle_skills_get)

    # 3. Advertise io.modelcontextprotocol/skills in initialize & server/discover.
    async def advertise_skills(ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        if (
            getattr(ctx, "method", "") in ("server/discover", "initialize")
            and isinstance(result, dict)
            and isinstance(result.get("capabilities"), dict)
        ):
            caps = dict(result["capabilities"])
            extensions = dict(caps.get("extensions") or {})
            extensions[EXTENSION_ID] = {}
            caps["extensions"] = extensions
            caps.setdefault("resources", {})
            result = {**result, "capabilities": caps}
        return result

    mcp.middleware.append(advertise_skills)

    # 4. Authenticated HTTP download endpoints on the FastAPI app for easy one-step install.
    if app is not None:
        from fastapi.responses import JSONResponse, Response

        @app.get("/v1/skills", include_in_schema=False)
        def http_list_skills():
            return JSONResponse(
                {
                    "extension": EXTENSION_ID,
                    "skills": [
                        {
                            **bundle.to_sep_entry(),
                            "slug": bundle.slug,
                            "download_path": f"/v1/skills/{bundle.slug}.zip",
                        }
                        for bundle in bundles
                    ],
                }
            )

        @app.get("/v1/skills/{slug}.zip", include_in_schema=False)
        def http_download_skill_zip(slug: str):
            bundle = by_slug.get(slug)
            if bundle is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": {"code": "not_found", "message": "Skill package not found."}},
                )
            archive = build_skill_zip(bundle)
            return Response(
                content=archive,
                media_type="application/zip",
                headers={
                    "Content-Disposition": f'attachment; filename="{bundle.slug}-skill.zip"',
                },
            )

    return bundles
