"""Walkthrough library access with canonical capture-folder validation."""
from __future__ import annotations

import json
from pathlib import Path

from .common import changed, export_result, identifier, paginate, string


def _root(context):
    from services.walkthrough.storage import captures_root
    return Path(getattr(context, "captures_root", None) or captures_root())


def _folder(context, params):
    from services.walkthrough.library import entries
    record_id = identifier(params)
    folder = next((folder for folder in entries(_root(context)) if folder.name == record_id), None)
    if folder is None:
        raise ValueError("Walkthrough was not found.")
    return folder


def _manifest(folder):
    from services.walkthrough.library import read_json, local_file, _seconds, _shot_file
    from services.walkthrough.storage import MANIFEST_NAME
    path = folder / MANIFEST_NAME
    if not path.exists():
        draft = read_json(local_file(folder, "draft.json"))
        return {"started_at": draft.get("started_at", ""), "duration_s": draft.get("duration_s", 0),
                "sections": [], "draft": True}
    value = read_json(local_file(folder, MANIFEST_NAME))
    if value.get("version") != 1 or not isinstance(value.get("sections"), list):
        raise ValueError("Unsupported walkthrough document.")
    _seconds(value.get("duration_s", 0))
    for section in value["sections"]:
        if not isinstance(section, dict) or not isinstance(section.get("text", ""), str):
            raise ValueError("Invalid walkthrough section.")
        _shot_file(folder, section.get("shot"))
        _shot_file(folder, section.get("omitted_shot"), required=False)
    return value


def _summary(folder, manifest):
    sections = manifest.get("sections", [])
    return {"id": folder.name, "title": manifest.get("title") or "Walkthrough " + folder.name.replace("_", " "),
            "started_at": manifest.get("started_at", ""), "timestamp": manifest.get("started_at", ""),
            "duration": manifest.get("duration_s", 0),
            "step_count": sum(bool(s.get("shot")) for s in sections),
            "preview": " ".join(s.get("text", "") for s in sections)[:220],
            "status": "draft" if manifest.get("draft") else "ready",
            "written_up": (folder / "write-up.md").is_file()}


def list_walkthroughs(context, params):
    from services.walkthrough.library import entries
    query = string(params.get("query", ""), "search query", maximum=1000).casefold()
    items, warnings = [], []
    for folder in entries(_root(context)):
        try:
            row = _summary(folder, _manifest(folder))
        except (OSError, ValueError, KeyError, TypeError):
            warnings.append({"id": folder.name, "message": "This document needs recovery."})
            continue
        if not query or query in (row["title"] + " " + row["preview"]).casefold():
            items.append(row)
    return {**paginate(items, params), "warnings": warnings}


def detail(context, params):
    from services.walkthrough.library import local_file
    folder = _folder(context, params)
    manifest = _manifest(folder)
    result = _summary(folder, manifest)
    steps = []
    for index, section in enumerate(manifest["sections"]):
        image = section.get("shot") or section.get("omitted_shot")
        steps.append({"index": index, "title": f"Step {index + 1}", "text": section.get("text", ""),
                      "image_path": str(local_file(folder, image)) if image else None,
                      "included": bool(section.get("shot")), "kept_at_s": section.get("kept_at_s"),
                      "notes": section.get("notes", [])})
    markdown = ""
    for name in ("write-up.md", "walkthrough.md"):
        if (folder / name).is_file():
            markdown = local_file(folder, name).read_text(encoding="utf-8")
            break
    result.update({"markdown": markdown, "steps": steps,
                   "audio_file": str(local_file(folder, "audio.wav")) if (folder / "audio.wav").is_file() else None,
                   "exports": [fmt for fmt in ("md", "pdf", "docx", "json")
                               if (folder / ("walkthrough." + fmt)).is_file()]})
    return result


def review(context, params):
    from services.walkthrough.library import save_review
    folder = _folder(context, params)
    original = _manifest(folder)
    texts, keep, order = params.get("texts"), params.get("keep"), params.get("order")
    count = len(original["sections"])
    if not isinstance(texts, list) or len(texts) != count or not all(isinstance(v, str) and len(v) <= 1_000_000 for v in texts):
        raise ValueError("Supply the text of each saved step.")
    if not isinstance(keep, list) or len(keep) != count or not all(type(v) is bool for v in keep):
        raise ValueError("Choose which screenshots to include.")
    if order is not None and (not isinstance(order, list) or any(type(v) is not int for v in order) or sorted(order) != list(range(count))):
        raise ValueError("Step order must include each step once.")
    save_review(folder, texts, keep, order)
    return changed("walkthroughs.list", id=folder.name, item=detail(context, {"id": folder.name}))


def delete(context, params):
    from services.walkthrough.library import delete_entry
    folder = _folder(context, params)
    session = getattr(context.controller.transcription_runtime, "recording_walkthrough", None)
    if session is not None and Path(session.folder).resolve() == folder.resolve():
        raise ValueError("Finish this walkthrough before deleting it.")
    delete_entry(folder)
    return changed("walkthroughs.list", id=folder.name)


def export(context, params):
    from services.walkthrough.library import local_file
    folder = _folder(context, params)
    fmt = params.get("format", "md")
    if fmt not in ("md", "markdown", "pdf", "docx", "json", "txt", "zip"):
        raise ValueError("Choose Markdown, text, JSON, PDF, Word or ZIP.")
    if fmt == "zip":
        import tempfile
        import zipfile
        from services.walkthrough.library import _SHOT
        # Stage outside the capture: a partially written archive can never
        # masquerade as one of the user's saved documents.
        with tempfile.NamedTemporaryFile(prefix="openwhisper-walkthrough-", suffix=".zip", delete=False) as temporary:
            archive = Path(temporary.name)
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                for path in sorted(folder.iterdir()):
                    if path.name in {"walkthrough.json", "walkthrough.md", "walkthrough.pdf", "walkthrough.docx",
                                     "write-up.md", "write-up.pdf", "write-up.docx"} or _SHOT.fullmatch(path.name):
                        output.write(local_file(folder, path.name), path.name)
            return export_result(params, "", f"{folder.name}.zip", "application/zip", source=archive)
        except Exception:
            archive.unlink(missing_ok=True)
            raise
    if fmt == "txt":
        text = "\n\n".join(step["text"] for step in detail(context, params)["steps"])
        return export_result(params, text, f"{folder.name}.txt")
    if fmt == "json":
        text = json.dumps(_manifest(folder), ensure_ascii=False, indent=2)
        return export_result(params, text, f"{folder.name}.json", "application/json")
    extension = "md" if fmt == "markdown" else fmt
    name = "write-up." + extension
    if not (folder / name).is_file():
        name = "walkthrough." + extension
    source = local_file(folder, name)
    if extension == "md":
        return _export_markdown(folder, source, params)
    mime = "application/pdf" if extension == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return export_result(params, "", f"{folder.name}.{extension}", mime, source=source)


def _export_markdown(folder, source, params):
    import shutil
    from services.walkthrough.library import local_file, _SHOT
    from services.walkthrough.write_up import _IMAGE
    destination = Path(params["path"]) if params.get("path") else None
    asset_directory = (destination.stem if destination else folder.name) + "-assets"
    assets = {}
    def image(match):
        name = Path(match[2].strip("<>")).name
        if not _SHOT.fullmatch(name):
            return ""
        path = local_file(folder, name)
        assets[name] = path
        return f"![{match[1]}](<{asset_directory}/{name}>)"
    text = _IMAGE.sub(image, source.read_text(encoding="utf-8"))
    asset_folder = None
    if destination and assets:
        if not destination.is_absolute() or not destination.parent.is_dir():
            raise ValueError("Choose an absolute export path in an existing folder.")
        if destination.exists():
            raise FileExistsError("That export file already exists.")
        asset_folder = destination.parent / asset_directory
        asset_folder.mkdir()
        try:
            for name, path in assets.items():
                shutil.copyfile(path, asset_folder / name)
        except Exception:
            shutil.rmtree(asset_folder)
            raise
    try:
        result = export_result(params, text, f"{folder.name}.md", "text/markdown")
    except Exception:
        if asset_folder:
            shutil.rmtree(asset_folder)
        raise
    if assets:
        result["asset_directory"] = asset_directory
        result["assets"] = [{"filename": name, "source_path": str(path)} for name, path in assets.items()]
    return result


def retention(context, params):
    from services.walkthrough.library import retention_days, set_retention
    from .common import integer
    if "days" in params:
        set_retention(_root(context), integer(params["days"], "retention days", high=36500))
    return {"days": retention_days(_root(context))}
