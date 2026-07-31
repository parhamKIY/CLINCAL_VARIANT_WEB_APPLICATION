"""Safe bridge between Streamlit inputs and the public pipeline."""

from __future__ import annotations

import codecs
import gzip
import os
import zlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO, Protocol

from backend.pipeline import (
    PipelineProgressCallback,
    PipelineResult,
    run_analysis,
)
from config import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    settings,
)


class FrontendExecutionError(RuntimeError):
    """Raised when a frontend input cannot be prepared safely."""


class UploadedVCF(Protocol):
    """Minimal uploaded-file contract required by the execution bridge."""

    name: str

    def getvalue(self) -> bytes:
        """Return the uploaded file contents."""


SUPPORTED_UPLOAD_SUFFIXES = (".vcf", ".vcf.gz")
MAX_UPLOAD_FILENAME_CHARACTERS = 255
MAX_VCF_HEADER_BYTES = 1_000_000
UPLOAD_VALIDATION_CHUNK_BYTES = 64 * 1024
VCF_COLUMN_HEADER = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO"


def _validate_upload_filename(filename: object) -> str:
    """Return the supported suffix for one untrusted upload name."""

    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > MAX_UPLOAD_FILENAME_CHARACTERS
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 for character in filename)
    ):
        raise FrontendExecutionError(
            "The uploaded filename is invalid."
        )
    lowered_name = filename.casefold()
    for suffix in reversed(SUPPORTED_UPLOAD_SUFFIXES):
        if lowered_name.endswith(suffix):
            return suffix
    raise FrontendExecutionError(
        "The uploaded file must end in .vcf or .vcf.gz."
    )


def _read_upload(uploaded_vcf: UploadedVCF) -> bytes:
    """Read one bounded upload without trusting its reported metadata."""

    try:
        payload = uploaded_vcf.getvalue()
    except Exception as exc:
        raise FrontendExecutionError(
            "The uploaded VCF could not be read."
        ) from exc
    if not isinstance(payload, bytes):
        raise FrontendExecutionError(
            "The uploaded VCF content is invalid."
        )
    if not payload:
        raise FrontendExecutionError(
            "The uploaded VCF is empty."
        )
    if len(payload) > settings.MAX_UPLOAD_BYTES:
        raise FrontendExecutionError(
            "The uploaded VCF exceeds the configured size limit."
        )
    return payload


def _validate_vcf_stream(stream: BinaryIO) -> None:
    """Validate bounded UTF-8 VCF content and required header lines."""

    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    prefix = bytearray()
    total_bytes = 0
    try:
        while True:
            chunk = stream.read(UPLOAD_VALIDATION_CHUNK_BYTES)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise FrontendExecutionError(
                    "The uploaded VCF content is invalid."
                )
            total_bytes += len(chunk)
            if total_bytes > settings.MAX_UNCOMPRESSED_VCF_BYTES:
                raise FrontendExecutionError(
                    "The uncompressed VCF exceeds the configured "
                    "size limit."
                )
            if b"\0" in chunk:
                raise FrontendExecutionError(
                    "The uploaded VCF contains invalid binary data."
                )
            decoder.decode(chunk)
            remaining = MAX_VCF_HEADER_BYTES - len(prefix)
            if remaining > 0:
                prefix.extend(chunk[:remaining])
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise FrontendExecutionError(
            "The uploaded VCF must contain valid UTF-8 text."
        ) from exc

    if total_bytes == 0:
        raise FrontendExecutionError(
            "The uploaded VCF is empty."
        )
    header_text = prefix.decode("utf-8")
    if not header_text.startswith("##fileformat=VCFv"):
        raise FrontendExecutionError(
            "The uploaded file does not contain a valid VCF header."
        )
    if not any(
        line.startswith(VCF_COLUMN_HEADER)
        for line in header_text.splitlines()
    ):
        raise FrontendExecutionError(
            "The uploaded file does not contain the required VCF "
            "column header."
        )


def _validate_upload_content(payload: bytes, suffix: str) -> None:
    """Require content that matches its plain or gzip VCF extension."""

    gzip_magic = payload.startswith(b"\x1f\x8b")
    if suffix == ".vcf.gz":
        if not gzip_magic:
            raise FrontendExecutionError(
                "The .vcf.gz upload is not a valid gzip file."
            )
        try:
            with gzip.GzipFile(
                fileobj=BytesIO(payload),
                mode="rb",
            ) as stream:
                _validate_vcf_stream(stream)
        except FrontendExecutionError:
            raise
        except (EOFError, OSError, zlib.error) as exc:
            raise FrontendExecutionError(
                "The .vcf.gz upload is damaged or invalid."
            ) from exc
        return

    if gzip_magic:
        raise FrontendExecutionError(
            "Compressed VCF content must use the .vcf.gz extension."
        )
    _validate_vcf_stream(BytesIO(payload))


def _prepare_upload_directory() -> Path:
    """Create one non-symlinked private upload root."""

    configured = Path(settings.UPLOAD_DIR).expanduser()
    try:
        if configured.is_symlink():
            raise FrontendExecutionError(
                "The configured upload directory is unsafe."
            )
        configured.mkdir(
            mode=PRIVATE_DIRECTORY_MODE,
            parents=True,
            exist_ok=True,
        )
        configured.chmod(PRIVATE_DIRECTORY_MODE)
        resolved = configured.resolve(strict=True)
    except FrontendExecutionError:
        raise
    except OSError as exc:
        raise FrontendExecutionError(
            "The upload directory could not be prepared securely."
        ) from exc
    if not resolved.is_dir():
        raise FrontendExecutionError(
            "The configured upload path is not a directory."
        )
    return resolved


def _write_private_upload(
    path: Path,
    payload: bytes,
    *,
    temporary_directory: Path,
) -> None:
    """Create one exclusive private file inside its temporary directory."""

    if (
        path.parent != temporary_directory
        or not temporary_directory.is_dir()
        or temporary_directory.is_symlink()
    ):
        raise FrontendExecutionError(
            "The temporary upload path is unsafe."
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(
            path,
            flags,
            PRIVATE_FILE_MODE,
        )
        with os.fdopen(file_descriptor, "wb") as upload_file:
            file_descriptor = None
            upload_file.write(payload)
            upload_file.flush()
            os.fsync(upload_file.fileno())
        path.chmod(PRIVATE_FILE_MODE)
    except OSError as exc:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise FrontendExecutionError(
            "The uploaded VCF could not be stored securely."
        ) from exc


def execute_analysis(
    *,
    uploaded_vcf: UploadedVCF | None,
    manual_variant: str | None,
    phenotypes: list[str],
    progress_callback: PipelineProgressCallback | None = None,
) -> PipelineResult:
    """Execute one manual or temporary-upload analysis request."""

    if uploaded_vcf is not None and manual_variant is not None:
        raise FrontendExecutionError(
            "Choose either a VCF upload or a manual variant."
        )

    if uploaded_vcf is None:
        return run_analysis(
            vcf_path=None,
            manual_variant=manual_variant,
            phenotypes=phenotypes,
            progress_callback=progress_callback,
        )

    suffix = _validate_upload_filename(
        getattr(uploaded_vcf, "name", None)
    )
    payload = _read_upload(uploaded_vcf)
    _validate_upload_content(payload, suffix)
    upload_directory = _prepare_upload_directory()
    try:
        with TemporaryDirectory(
            prefix="analysis-",
            dir=upload_directory,
        ) as temporary_directory:
            temporary_root = Path(temporary_directory).resolve(
                strict=True
            )
            if temporary_root.parent != upload_directory:
                raise FrontendExecutionError(
                    "The temporary upload directory is unsafe."
                )
            temporary_root.chmod(PRIVATE_DIRECTORY_MODE)
            temporary_path = temporary_root / f"input{suffix}"
            _write_private_upload(
                temporary_path,
                payload,
                temporary_directory=temporary_root,
            )
            return run_analysis(
                vcf_path=temporary_path,
                manual_variant=None,
                phenotypes=phenotypes,
                progress_callback=progress_callback,
            )
    except FrontendExecutionError:
        raise
    except OSError as exc:
        raise FrontendExecutionError(
            "The uploaded VCF could not be prepared for analysis."
        ) from exc


__all__ = [
    "FrontendExecutionError",
    "UploadedVCF",
    "execute_analysis",
]
