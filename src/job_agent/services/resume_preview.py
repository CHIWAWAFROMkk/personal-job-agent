"""Ephemeral local PDF rendering. Does not save, approve, or confirm resume facts."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

from job_agent.models.job_record import JobDetail
from job_agent.services.portable_resume import find_profile_photo, _write_pdf
from job_agent.services.resume_editor import ResumeEditorError, validate_resume_content
from job_agent.services.resume_reference_layout import render_reference_pdf
from job_agent.services.resume_render import load_template

_RENDER_LOCK = Lock()


def render_resume_preview(content: object, job: JobDetail, private_dir: Path) -> tuple[bytes, int]:
    """Use the local save layout without a draft; optional Office export may differ.

    This preview always uses the local renderer even if Office export is enabled
    for saved drafts. It therefore does not certify the optional Office output.
    """
    data = deepcopy(validate_resume_content(content))
    target = data["target"]
    if type(target.get("job_id")) is not int or target["job_id"] != job.job_id:
        raise ResumeEditorError("预览内容与当前岗位不一致。")
    data["target"] = {"job_id": job.job_id, "company": job.company,
                      "role": job.title, "location": job.location}
    base = Path(private_dir).resolve()
    # A nonempty path is only the editor's inclusion flag, never a file source.
    include_photo = bool(data["person"].get("photo_path", "").strip())
    photo = find_profile_photo(base) if include_photo else None
    # Never follow a client-provided file path, including UNC/remote paths.
    data["person"]["photo_path"] = ""
    if photo is not None:
        photo = photo.resolve()
        if not photo.is_relative_to(base) or photo.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            raise ResumeEditorError("档案照片位置异常，请重新上传。")
        if photo.stat().st_size > 5 * 1024 * 1024:
            raise ResumeEditorError("档案照片过大，请重新上传。")
        from PIL import Image
        with Image.open(photo) as image:
            if image.width * image.height > 20_000_000:
                raise ResumeEditorError("档案照片尺寸过大，请重新上传。")
            image.verify()
        data["person"]["photo_path"] = str(photo)
    # A dedicated private parent keeps transient PII out of the OS-wide temp area.
    scratch = base / ".resume-preview"
    scratch.mkdir(parents=True, exist_ok=True)
    if scratch.resolve().parent != base:
        raise ResumeEditorError("预览临时目录位置异常。")
    with _RENDER_LOCK, TemporaryDirectory(prefix="preview-", dir=scratch) as temporary:
        pdf = Path(temporary) / "preview.pdf"
        if data["template_id"] == "reference-a4":
            spec = load_template("reference-a4")
            pages = render_reference_pdf(data, spec, pdf, photo)
            if pages != 1:
                data["layout_density"] = "compact"
                pages = render_reference_pdf(data, spec, pdf, photo)
        else:
            pages = _write_pdf(data, pdf, photo)
        return pdf.read_bytes(), pages
