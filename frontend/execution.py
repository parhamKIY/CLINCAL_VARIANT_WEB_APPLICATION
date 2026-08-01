"""Safe bridge between Streamlit inputs and the public pipeline."""

from __future__ import annotations

import codecs
import gzip
import os
import threading
import zlib
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import (
    BinaryIO,
    Callable,
    Literal,
    Mapping,
    Protocol,
    Sequence,
)

from backend.pipeline import (
    PipelineProgressCallback,
    PipelineResult,
    run_analysis,
)
from backend.vcf_processing import MAX_FILTERED_VCF_ROWS
from config import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    settings,
)


class FrontendExecutionError(RuntimeError):
    """Raised when a frontend input cannot be prepared safely."""


class AnalysisCancelled(BaseException):
    """Stop one cooperative background analysis without error output."""


class UploadedVCF(Protocol):
    """Minimal uploaded-file contract required by the execution bridge."""

    name: str

    def getvalue(self) -> bytes:
        """Return the uploaded file contents."""


SUPPORTED_UPLOAD_SUFFIXES = (".vcf", ".vcf.gz")
MAX_UPLOAD_FILENAME_CHARACTERS = 255
UPLOAD_VALIDATION_CHUNK_BYTES = 64 * 1024
VCF_COLUMN_HEADER = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO"

AnalysisJobState = Literal[
    "running",
    "cancelling",
    "cancelled",
    "completed",
    "error",
]
AnalysisRunner = Callable[
    [PipelineProgressCallback],
    PipelineResult,
]


@dataclass(frozen=True, slots=True)
class AnalysisJobView:
    """Thread-safe public view of one frontend analysis job."""

    state: AnalysisJobState
    latest_result: PipelineResult | None
    result: PipelineResult | None
    error_message: str | None
    cleanup_warning: str | None


def _existing_report_paths(
    report_directory: Path,
) -> frozenset[Path]:
    """Snapshot existing managed reports before a cancellable run."""

    try:
        resolved_directory = report_directory.resolve(strict=True)
    except (OSError, RuntimeError):
        return frozenset()
    if (
        not resolved_directory.is_dir()
        or resolved_directory.is_symlink()
    ):
        return frozenset()

    existing: set[Path] = set()
    try:
        candidates = resolved_directory.glob(
            "clinical-report-*.txt"
        )
        for candidate in candidates:
            if candidate.is_file() and not candidate.is_symlink():
                existing.add(candidate.resolve(strict=True))
    except (OSError, RuntimeError):
        return frozenset()
    return frozenset(existing)


class AnalysisJob:
    """Run one analysis in the background with cooperative cancellation."""

    def __init__(
        self,
        runner: AnalysisRunner,
        *,
        report_dir: str | Path | None = None,
    ) -> None:
        self._runner = runner
        self._report_directory = Path(
            settings.REPORT_DIR if report_dir is None else report_dir
        ).expanduser()
        self._existing_reports = _existing_report_paths(
            self._report_directory
        )
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._state: AnalysisJobState = "running"
        self._latest_result: PipelineResult | None = None
        self._result: PipelineResult | None = None
        self._error_message: str | None = None
        self._cleanup_warning: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="clinical-variant-analysis",
            daemon=True,
        )

    def start(self) -> None:
        """Start the background worker exactly once."""

        self._thread.start()

    def request_cancel(self) -> bool:
        """Request cancellation while the job is still active."""

        with self._lock:
            if self._state not in {"running", "cancelling"}:
                return False
            if (
                self._latest_result is not None
                and self._latest_result["current_stage"] == "completed"
            ):
                return False
            self._state = "cancelling"
            self._cancel_event.set()
            return True

    def view(self) -> AnalysisJobView:
        """Return an isolated snapshot safe for Streamlit rendering."""

        with self._lock:
            return AnalysisJobView(
                state=self._state,
                latest_result=deepcopy(self._latest_result),
                result=deepcopy(self._result),
                error_message=self._error_message,
                cleanup_warning=self._cleanup_warning,
            )

    def join(self, timeout: float | None = None) -> None:
        """Wait for completion in tests or controlled shutdown paths."""

        self._thread.join(timeout)

    def _capture_progress(self, result: PipelineResult) -> None:
        with self._lock:
            self._latest_result = deepcopy(result)
        if self._cancel_event.is_set():
            raise AnalysisCancelled

    def _cancelled_report_path(
        self,
        result: PipelineResult | None,
    ) -> Path | None:
        candidate_result = result
        if candidate_result is None:
            with self._lock:
                candidate_result = deepcopy(self._latest_result)
        if candidate_result is None:
            return None
        raw_path = candidate_result.get("report_path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        return Path(raw_path)

    def _remove_cancelled_report(
        self,
        result: PipelineResult | None,
    ) -> str | None:
        candidate = self._cancelled_report_path(result)
        if candidate is None:
            return None
        try:
            if candidate.is_symlink():
                raise OSError
            resolved_directory = self._report_directory.resolve(
                strict=True
            )
            resolved_candidate = candidate.resolve(strict=True)
            if resolved_candidate in self._existing_reports:
                return None
            if (
                resolved_candidate.parent != resolved_directory
                or resolved_candidate.suffix.casefold() != ".txt"
                or not resolved_candidate.name.startswith(
                    "clinical-report-"
                )
                or not resolved_candidate.is_file()
            ):
                raise OSError
            resolved_candidate.unlink()
        except (OSError, RuntimeError):
            return (
                "The analysis was cancelled, but one generated draft "
                "could not be removed."
            )
        return None

    def _mark_cancelled(
        self,
        result: PipelineResult | None = None,
    ) -> None:
        cleanup_warning = self._remove_cancelled_report(result)
        with self._lock:
            self._state = "cancelled"
            self._latest_result = None
            self._result = None
            self._cleanup_warning = cleanup_warning

    def _run(self) -> None:
        try:
            if self._cancel_event.is_set():
                raise AnalysisCancelled
            result = self._runner(self._capture_progress)
            if self._cancel_event.is_set():
                self._mark_cancelled(result)
                return
        except AnalysisCancelled:
            self._mark_cancelled()
            return
        except FrontendExecutionError as exc:
            with self._lock:
                self._state = "error"
                self._error_message = str(exc)
            return
        except Exception:
            with self._lock:
                self._state = "error"
                self._error_message = (
                    "An unexpected internal error stopped the analysis."
                )
            return

        with self._lock:
            self._state = "completed"
            self._latest_result = deepcopy(result)
            self._result = deepcopy(result)


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
    """Validate one UTF-8 filtered VCF containing one to five rows."""

    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    total_bytes = 0
    text_buffer = ""
    first_line: str | None = None
    has_column_header = False
    data_row_count = 0

    def consume_line(line: str) -> None:
        nonlocal first_line, has_column_header, data_row_count
        normalized = line.rstrip("\r")
        if first_line is None:
            first_line = normalized
        if normalized.startswith(VCF_COLUMN_HEADER):
            has_column_header = True
            return
        if normalized and not normalized.startswith("#"):
            data_row_count += 1
            if data_row_count > MAX_FILTERED_VCF_ROWS:
                raise FrontendExecutionError(
                    "The filtered VCF cannot contain more than "
                    f"{MAX_FILTERED_VCF_ROWS} data rows."
                )

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
            text_buffer += decoder.decode(chunk)
            while "\n" in text_buffer:
                line, text_buffer = text_buffer.split("\n", 1)
                consume_line(line)
        text_buffer += decoder.decode(b"", final=True)
        if text_buffer:
            consume_line(text_buffer)
    except UnicodeDecodeError as exc:
        raise FrontendExecutionError(
            "The uploaded VCF must contain valid UTF-8 text."
        ) from exc

    if total_bytes == 0:
        raise FrontendExecutionError(
            "The uploaded VCF is empty."
        )
    if first_line is None or not first_line.startswith(
        "##fileformat=VCFv"
    ):
        raise FrontendExecutionError(
            "The uploaded file does not contain a valid VCF header."
        )
    if not has_column_header:
        raise FrontendExecutionError(
            "The uploaded file does not contain the required VCF "
            "column header."
        )
    if data_row_count == 0:
        raise FrontendExecutionError(
            "The filtered VCF must contain at least one data row."
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
    manual_variants: Sequence[Mapping[str, object]] | None,
    phenotypes: list[str],
    llm_model: str | None = None,
    progress_callback: PipelineProgressCallback | None = None,
) -> PipelineResult:
    """Execute one manual or temporary-upload analysis request."""

    if uploaded_vcf is not None and manual_variants is not None:
        raise FrontendExecutionError(
            "Choose either a VCF upload or manual table rows."
        )

    if uploaded_vcf is None:
        return run_analysis(
            vcf_path=None,
            manual_variants=manual_variants,
            phenotypes=phenotypes,
            llm_model=llm_model,
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
                manual_variants=None,
                phenotypes=phenotypes,
                llm_model=llm_model,
                progress_callback=progress_callback,
            )
    except FrontendExecutionError:
        raise
    except OSError as exc:
        raise FrontendExecutionError(
            "The uploaded VCF could not be prepared for analysis."
        ) from exc


__all__ = [
    "AnalysisCancelled",
    "AnalysisJob",
    "AnalysisJobState",
    "AnalysisJobView",
    "FrontendExecutionError",
    "UploadedVCF",
    "execute_analysis",
]
