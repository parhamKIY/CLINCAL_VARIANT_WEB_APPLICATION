"""Safe bridge between Streamlit inputs and the public pipeline."""

from __future__ import annotations

import codecs
import gzip
import json
import os
import re
import threading
import zlib
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, time
from typing import (
    BinaryIO,
    Callable,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    TypedDict,
    NotRequired,
    cast,
)
from uuid import uuid4

from backend.clinical_entities import (
    ClinicalEntity,
    ClinicalEntityError,
    validate_clinical_entities,
)
from backend.database import (
    DatabaseError,
    load_pipeline_state,
    save_pipeline_state,
)
from backend.pipeline import (
    ANALYSIS_ID_PATTERN,
    PipelineProgressCallback,
    PipelineResult,
    PipelineResultError,
    attach_input_preprocessing_results,
    run_annovar_like_input_processing,
    run_analysis,
    validate_analysis_context,
)
from backend.excel_processing import (
    parse_excel_input_records,
    parse_excel_variants,
)
from backend.input_preprocessing import classify_source_representation
from backend.provider_readiness import ProviderReadinessSnapshot
from backend.vcf_processing import (
    VCFProcessingError,
    process_vcf,
)
from config import (
    MAX_VARIANTS_PER_ANALYSIS,
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


SUPPORTED_UPLOAD_SUFFIXES = (".vcf", ".vcf.gz", ".xlsx")
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
ANALYSIS_JOB_TOKEN_PATTERN = re.compile(r"job-[0-9a-f]{32}")
MAX_RECOVERABLE_ANALYSIS_JOBS = 32
RECOVERABLE_ANALYSIS_JOB_TTL_SECONDS = 60 * 60
RECOVERY_REQUEST_SCHEMA_VERSION = 4
MAX_RECOVERY_REQUEST_BYTES = 1024 * 1024
LAST_SESSION_FILENAME = "last_session.json"


class AnalysisRecoveryRequest(TypedDict):
    """Sanitized input sufficient to restart the complete analysis phase."""

    schema_version: int
    manual_variants: list[dict[str, object]]
    excel_input_records: NotRequired[list[dict[str, object]]]
    phenotypes: list[str]
    clinical_entities: list[ClinicalEntity] | None
    llm_model: str | None
    input_type: str
    phenotype_extraction_model: str | None
    phenotype_extraction_provenance: dict[str, object] | None
    analysis_id: str | None
    created_at: float


EXCEL_RECOVERY_RECORD_FIELDS = frozenset(
    {
        "worksheet",
        "row",
        "chrom",
        "start",
        "end",
        "ref",
        "alt",
        "qual",
        "filter",
        "depth",
        "ad",
        "gq",
    }
)


def _validate_excel_recovery_records(
    value: object,
) -> list[dict[str, object]] | None:
    """Accept only the bounded first-sheet source records used by Stage 2B."""

    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_VARIANTS_PER_ANALYSIS
        or any(not isinstance(item, dict) for item in value)
    ):
        return None
    records: list[dict[str, object]] = []
    for item in value:
        if set(item) != EXCEL_RECOVERY_RECORD_FIELDS:
            return None
        if any(
            field_value is not None
            and not isinstance(field_value, (str, int, float))
            or isinstance(field_value, bool)
            for field_value in item.values()
        ):
            return None
        records.append(dict(item))
    return records


def _last_session_path() -> Path | None:
    """Return the path to the last-session file, or None on error."""
    try:
        sentinel = f"job-{'0' * 32}"
        root = _recovery_request_path(sentinel).parent
    except FrontendExecutionError:
        return None
    return root / LAST_SESSION_FILENAME


def save_last_session_analysis_id(analysis_id: str) -> None:
    """Persist the most recently completed analysis ID for bare-URL recovery."""
    if not isinstance(analysis_id, str) or ANALYSIS_ID_PATTERN.fullmatch(analysis_id) is None:
        return
    path = _last_session_path()
    if path is None:
        return
    payload = json.dumps({"analysis_id": analysis_id}).encode("utf-8")
    temporary_path = path.with_suffix(f".{uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary_path, flags, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def load_last_session_analysis_id() -> str | None:
    """Return the last completed analysis ID from disk, or None."""
    path = _last_session_path()
    if path is None:
        return None
    try:
        if not path.is_file() or path.is_symlink():
            return None
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    analysis_id = value.get("analysis_id")
    if (
        not isinstance(analysis_id, str)
        or ANALYSIS_ID_PATTERN.fullmatch(analysis_id) is None
    ):
        return None
    return analysis_id


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
        self._recovery_token: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="clinical-variant-analysis",
            daemon=True,
        )

    def start(self) -> None:
        """Start the background worker exactly once."""

        self._thread.start()

    def bind_recovery_token(self, token: str) -> None:
        """Attach the registry token used to checkpoint a persisted result."""

        if ANALYSIS_JOB_TOKEN_PATTERN.fullmatch(token) is None:
            raise FrontendExecutionError(
                "Analysis job recovery token is invalid."
            )
        with self._lock:
            self._recovery_token = token

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
            recovery_token = self._recovery_token
        analysis_id = result.get("analysis_id")
        if recovery_token is not None and isinstance(analysis_id, str):
            _mark_recovery_request_persisted(
                recovery_token,
                analysis_id,
            )
        with self._lock:
            self._state = "completed"
            self._latest_result = deepcopy(result)
            self._result = deepcopy(result)


@dataclass(slots=True)
class _RegisteredAnalysisJob:
    job: AnalysisJob
    last_accessed_at: float


_ANALYSIS_JOB_REGISTRY: dict[str, _RegisteredAnalysisJob] = {}
_ANALYSIS_JOB_REGISTRY_LOCK = threading.Lock()


def _recovery_request_path(token: str) -> Path:
    root = Path(settings.DATABASE_PATH).expanduser().parent / "recovery_jobs"
    try:
        if root.is_symlink():
            raise FrontendExecutionError(
                "The recovery request directory is unsafe."
            )
        root.mkdir(
            mode=PRIVATE_DIRECTORY_MODE,
            parents=True,
            exist_ok=True,
        )
        root.chmod(PRIVATE_DIRECTORY_MODE)
        resolved = root.resolve(strict=True)
    except FrontendExecutionError:
        raise
    except OSError as exc:
        raise FrontendExecutionError(
            "The recovery request directory is unavailable."
        ) from exc
    return resolved / f"{token}.json"


def _persist_recovery_request(
    token: str,
    request: AnalysisRecoveryRequest,
) -> None:
    try:
        context = validate_analysis_context(
            {
                "input_type": request["input_type"],
                "accepted_hpo_terms": request["phenotypes"],
                "clinical_entities": request.get("clinical_entities"),
                "phenotype_extraction_model": request[
                    "phenotype_extraction_model"
                ],
                "variant_interpretation_model": request["llm_model"],
                "phenotype_extraction_provenance": request[
                    "phenotype_extraction_provenance"
                ],
            }
        )
    except (KeyError, PipelineResultError) as exc:
        raise FrontendExecutionError(
            "The analysis recovery context is invalid."
        ) from exc
    if context["input_type"] is None:
        raise FrontendExecutionError(
            "The analysis recovery context is invalid."
        )
    normalized_request = AnalysisRecoveryRequest(
        schema_version=RECOVERY_REQUEST_SCHEMA_VERSION,
        manual_variants=request["manual_variants"],
        phenotypes=context["accepted_hpo_terms"],
        clinical_entities=context["clinical_entities"],
        llm_model=context["variant_interpretation_model"],
        input_type=context["input_type"],
        phenotype_extraction_model=context[
            "phenotype_extraction_model"
        ],
        phenotype_extraction_provenance=context[
            "phenotype_extraction_provenance"
        ],
        analysis_id=request["analysis_id"],
        created_at=request["created_at"],
    )
    excel_input_records = _validate_excel_recovery_records(
        request.get("excel_input_records")
    )
    if excel_input_records is not None:
        normalized_request["excel_input_records"] = excel_input_records
    payload = json.dumps(
        normalized_request,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_RECOVERY_REQUEST_BYTES:
        raise FrontendExecutionError(
            "The recovery request exceeds the safe storage limit."
        )
    path = _recovery_request_path(token)
    temporary_path = path.with_suffix(f".{uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary_path, flags, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        path.chmod(PRIVATE_FILE_MODE)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise FrontendExecutionError(
            "The analysis recovery checkpoint could not be saved."
        ) from exc


def _load_recovery_request(
    token: str,
) -> AnalysisRecoveryRequest | None:
    try:
        path = _recovery_request_path(token)
        if not path.is_file() or path.is_symlink():
            return None
        payload = path.read_bytes()
    except (FrontendExecutionError, OSError):
        return None
    if not payload or len(payload) > MAX_RECOVERY_REQUEST_BYTES:
        _delete_recovery_request(token)
        return None
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _delete_recovery_request(token)
        return None
    if not isinstance(value, dict):
        _delete_recovery_request(token)
        return None
    recovery_schema_version = value.get("schema_version")
    if recovery_schema_version not in {3, RECOVERY_REQUEST_SCHEMA_VERSION}:
        _delete_recovery_request(token)
        return None
    created_at = value.get("created_at")
    variants = value.get("manual_variants")
    excel_input_records = _validate_excel_recovery_records(
        value.get("excel_input_records")
    )
    phenotypes = value.get("phenotypes")
    clinical_entities = (
        value.get("clinical_entities")
        if recovery_schema_version == RECOVERY_REQUEST_SCHEMA_VERSION
        else None
    )
    llm_model = value.get("llm_model")
    input_type = value.get("input_type")
    phenotype_model = value.get("phenotype_extraction_model")
    phenotype_provenance = value.get(
        "phenotype_extraction_provenance"
    )
    analysis_id = value.get("analysis_id")
    if (
        not isinstance(created_at, (int, float))
        or time() - float(created_at) > RECOVERABLE_ANALYSIS_JOB_TTL_SECONDS
        or not isinstance(variants, list)
        or (not variants and excel_input_records is None)
        or len(variants) > MAX_VARIANTS_PER_ANALYSIS
        or any(not isinstance(item, dict) for item in variants)
        or not isinstance(phenotypes, list)
        or any(not isinstance(item, str) for item in phenotypes)
        or (llm_model is not None and not isinstance(llm_model, str))
        or input_type not in {"vcf", "vcf_gz", "excel", "manual"}
        or (
            phenotype_model is not None
            and not isinstance(phenotype_model, str)
        )
        or (
            phenotype_provenance is not None
            and not isinstance(phenotype_provenance, dict)
        )
        or (
            analysis_id is not None
            and (
                not isinstance(analysis_id, str)
                or ANALYSIS_ID_PATTERN.fullmatch(analysis_id) is None
            )
        )
    ):
        _delete_recovery_request(token)
        return None
    if value.get("excel_input_records") is not None and excel_input_records is None:
        _delete_recovery_request(token)
        return None
    try:
        context = validate_analysis_context(
            {
                "input_type": input_type,
                "accepted_hpo_terms": phenotypes,
                "clinical_entities": clinical_entities,
                "phenotype_extraction_model": phenotype_model,
                "variant_interpretation_model": llm_model,
                "phenotype_extraction_provenance": phenotype_provenance,
            }
        )
    except PipelineResultError:
        _delete_recovery_request(token)
        return None
    request = AnalysisRecoveryRequest(
        schema_version=RECOVERY_REQUEST_SCHEMA_VERSION,
        manual_variants=variants,
        phenotypes=context["accepted_hpo_terms"],
        clinical_entities=context["clinical_entities"],
        llm_model=context["variant_interpretation_model"],
        input_type=cast(str, context["input_type"]),
        phenotype_extraction_model=context[
            "phenotype_extraction_model"
        ],
        phenotype_extraction_provenance=context[
            "phenotype_extraction_provenance"
        ],
        analysis_id=analysis_id,
        created_at=float(created_at),
    )
    if excel_input_records is not None:
        request["excel_input_records"] = excel_input_records
    return request


def _mark_recovery_request_persisted(
    token: str,
    analysis_id: str,
) -> None:
    """Record a durable draft so restart recovery never repeats its LLM work."""

    request = _load_recovery_request(token)
    if request is None or ANALYSIS_ID_PATTERN.fullmatch(analysis_id) is None:
        return
    request["analysis_id"] = analysis_id
    try:
        _persist_recovery_request(token, request)
    except FrontendExecutionError:
        return
    save_last_session_analysis_id(analysis_id)



def _delete_recovery_request(token: str) -> None:
    try:
        _recovery_request_path(token).unlink(missing_ok=True)
    except (FrontendExecutionError, OSError):
        pass


def _prune_stale_recovery_requests() -> None:
    """Remove expired checkpoint and abandoned atomic-temporary files."""

    sentinel = f"job-{'0' * 32}"
    try:
        root = _recovery_request_path(sentinel).parent
        cutoff = time() - RECOVERABLE_ANALYSIS_JOB_TTL_SECONDS
        inspected = 0
        for candidate in root.iterdir():
            if inspected >= 1024:
                break
            inspected += 1
            if candidate.is_symlink() or not candidate.is_file():
                continue
            name = candidate.name
            is_checkpoint = (
                name.endswith(".json")
                and ANALYSIS_JOB_TOKEN_PATTERN.fullmatch(
                    name.removesuffix(".json")
                )
                is not None
            )
            is_temporary = bool(
                re.fullmatch(
                    r"job-[0-9a-f]{32}\.[0-9a-f]{32}\.tmp",
                    name,
                )
            )
            if (
                (is_checkpoint or is_temporary)
                and candidate.stat().st_mtime <= cutoff
            ):
                candidate.unlink(missing_ok=True)
    except (FrontendExecutionError, OSError):
        return


def _prune_analysis_job_registry(now: float) -> None:
    """Remove abandoned terminal jobs while preserving active work."""

    expired = [
        token
        for token, registered in _ANALYSIS_JOB_REGISTRY.items()
        if registered.job.view().state
        in {"completed", "cancelled", "error"}
        and now - registered.last_accessed_at
        >= RECOVERABLE_ANALYSIS_JOB_TTL_SECONDS
    ]
    for token in expired:
        _ANALYSIS_JOB_REGISTRY.pop(token, None)
        _delete_recovery_request(token)


def register_analysis_job(
    job: AnalysisJob,
    *,
    recovery_request: AnalysisRecoveryRequest | None = None,
    token: str | None = None,
) -> str:
    """Register a job under an unguessable refresh-recovery token."""

    if not isinstance(job, AnalysisJob):
        raise FrontendExecutionError("Analysis job registration is invalid.")
    _prune_stale_recovery_requests()
    now = monotonic()
    with _ANALYSIS_JOB_REGISTRY_LOCK:
        _prune_analysis_job_registry(now)
        if len(_ANALYSIS_JOB_REGISTRY) >= MAX_RECOVERABLE_ANALYSIS_JOBS:
            terminal = sorted(
                (
                    (registered.last_accessed_at, token)
                    for token, registered
                    in _ANALYSIS_JOB_REGISTRY.items()
                    if registered.job.view().state
                    in {"completed", "cancelled", "error"}
                )
            )
            if terminal:
                _ANALYSIS_JOB_REGISTRY.pop(terminal[0][1], None)
            else:
                raise FrontendExecutionError(
                    "The server is already processing the maximum "
                    "number of recoverable analyses."
                )
        resolved_token = token or f"job-{uuid4().hex}"
        if ANALYSIS_JOB_TOKEN_PATTERN.fullmatch(resolved_token) is None:
            raise FrontendExecutionError(
                "Analysis job recovery token is invalid."
            )
        if resolved_token in _ANALYSIS_JOB_REGISTRY:
            return resolved_token
        _ANALYSIS_JOB_REGISTRY[resolved_token] = _RegisteredAnalysisJob(
            job=job,
            last_accessed_at=now,
        )
    try:
        if recovery_request is not None:
            _persist_recovery_request(resolved_token, recovery_request)
        job.bind_recovery_token(resolved_token)
    except FrontendExecutionError:
        with _ANALYSIS_JOB_REGISTRY_LOCK:
            _ANALYSIS_JOB_REGISTRY.pop(resolved_token, None)
        raise
    return resolved_token


def get_registered_analysis_job(token: object) -> AnalysisJob | None:
    """Resolve a refresh token without exposing other registered jobs."""

    if (
        not isinstance(token, str)
        or ANALYSIS_JOB_TOKEN_PATTERN.fullmatch(token) is None
    ):
        return None
    now = monotonic()
    with _ANALYSIS_JOB_REGISTRY_LOCK:
        registered = _ANALYSIS_JOB_REGISTRY.get(token)
        if registered is not None:
            registered.last_accessed_at = now
        _prune_analysis_job_registry(now)
        registered = _ANALYSIS_JOB_REGISTRY.get(token)
        if registered is None:
            return None
        return registered.job


def release_registered_analysis_job(token: object) -> None:
    """Forget one terminal or cancelled refresh-recovery token."""

    if (
        not isinstance(token, str)
        or ANALYSIS_JOB_TOKEN_PATTERN.fullmatch(token) is None
    ):
        return
    with _ANALYSIS_JOB_REGISTRY_LOCK:
        _ANALYSIS_JOB_REGISTRY.pop(token, None)
    _delete_recovery_request(token)


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
        "The uploaded file must end in .vcf, .vcf.gz, or .xlsx."
    )


def _read_upload(uploaded_vcf: UploadedVCF) -> bytes:
    """Read one bounded upload without trusting its reported metadata."""

    try:
        payload = uploaded_vcf.getvalue()
    except Exception as exc:
        raise FrontendExecutionError(
            "The uploaded variant file could not be read."
        ) from exc
    if not isinstance(payload, bytes):
        raise FrontendExecutionError(
            "The uploaded variant-file content is invalid."
        )
    if not payload:
        raise FrontendExecutionError(
            "The uploaded variant file is empty."
        )
    if len(payload) > settings.MAX_UPLOAD_BYTES:
        raise FrontendExecutionError(
            "The uploaded variant file exceeds the configured size limit."
        )
    return payload


def _validate_vcf_stream(stream: BinaryIO) -> None:
    """Validate one UTF-8 VCF within the configured variant limit."""

    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    total_bytes = 0
    text_buffer = ""
    first_line: str | None = None
    has_column_header = False
    data_row_count = 0
    variant_count = 0

    def consume_line(line: str) -> None:
        nonlocal first_line, has_column_header, data_row_count, variant_count
        normalized = line.rstrip("\r")
        if first_line is None:
            first_line = normalized
        if normalized.startswith(VCF_COLUMN_HEADER):
            has_column_header = True
            return
        if normalized and not normalized.startswith("#"):
            data_row_count += 1
            if data_row_count > MAX_VARIANTS_PER_ANALYSIS:
                raise FrontendExecutionError(
                    "The filtered VCF cannot contain more than "
                    f"{MAX_VARIANTS_PER_ANALYSIS} data rows."
                )
            fields = normalized.split("\t")
            if len(fields) >= 5:
                alternates = [
                    value for value in fields[4].split(",") if value
                ]
                variant_count += max(len(alternates), 1)
                if variant_count > MAX_VARIANTS_PER_ANALYSIS:
                    raise FrontendExecutionError(
                        "The filtered VCF cannot produce more than "
                        f"{MAX_VARIANTS_PER_ANALYSIS} variants after "
                        "multiallelic splitting."
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

    if suffix != ".vcf":
        raise FrontendExecutionError(
            "The uploaded variant-file type is unsupported."
        )

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
            "The uploaded variant file could not be stored securely."
        ) from exc


def prepare_analysis_recovery_request(
    *,
    uploaded_vcf: UploadedVCF | None,
    manual_variants: Sequence[Mapping[str, object]] | None,
    phenotypes: list[str],
    clinical_entities: Sequence[Mapping[str, object]] | None = None,
    llm_model: str | None,
    input_type: str | None = None,
    phenotype_extraction_model: str | None = None,
    phenotype_extraction_provenance: Mapping[str, object] | None = None,
    excel_input_records: Sequence[Mapping[str, object]] | None = None,
) -> AnalysisRecoveryRequest:
    """Normalize input into a restart-safe analysis-phase checkpoint."""

    if uploaded_vcf is not None and (
        manual_variants is not None or excel_input_records is not None
    ):
        raise FrontendExecutionError(
            "Choose either a variant-file upload or manual table rows."
        )
    try:
        normalized_clinical_entities = validate_clinical_entities(
            [] if clinical_entities is None else clinical_entities
        )
    except ClinicalEntityError as exc:
        raise FrontendExecutionError(
            "The clinical entity context is invalid."
        ) from exc
    try:
        if excel_input_records is not None:
            request = AnalysisRecoveryRequest(
                schema_version=RECOVERY_REQUEST_SCHEMA_VERSION,
                manual_variants=[],
                phenotypes=list(phenotypes),
                clinical_entities=normalized_clinical_entities,
                llm_model=llm_model,
                input_type=input_type or "excel",
                phenotype_extraction_model=phenotype_extraction_model,
                phenotype_extraction_provenance=(
                    dict(phenotype_extraction_provenance)
                    if phenotype_extraction_provenance is not None
                    else None
                ),
                analysis_id=None,
                created_at=time(),
            )
            request["excel_input_records"] = [
                dict(record) for record in excel_input_records
            ]
            return request
        if uploaded_vcf is None:
            variants = list(
                process_vcf(manual_variants=manual_variants)
            )
        else:
            suffix = _validate_upload_filename(uploaded_vcf.name)
            payload = _read_upload(uploaded_vcf)
            if suffix == ".xlsx":
                input_records = parse_excel_input_records(payload)
                if all(
                    classify_source_representation(
                        record["ref"], record["alt"]
                    ) == "STANDARD_ALLELE"
                    for record in input_records
                ):
                    variants = parse_excel_variants(payload)
                else:
                    variants = []
                request = AnalysisRecoveryRequest(
                    schema_version=RECOVERY_REQUEST_SCHEMA_VERSION,
                    manual_variants=[dict(variant) for variant in variants],
                    phenotypes=list(phenotypes),
                    clinical_entities=normalized_clinical_entities,
                    llm_model=llm_model,
                    input_type=input_type or "excel",
                    phenotype_extraction_model=phenotype_extraction_model,
                    phenotype_extraction_provenance=(
                        dict(phenotype_extraction_provenance)
                        if phenotype_extraction_provenance is not None
                        else None
                    ),
                    analysis_id=None,
                    created_at=time(),
                )
                request["excel_input_records"] = input_records
                return request
            _validate_upload_content(payload, suffix)
            upload_directory = _prepare_upload_directory()
            with TemporaryDirectory(
                prefix="recovery-",
                dir=upload_directory,
            ) as temporary_directory:
                root = Path(temporary_directory).resolve(strict=True)
                root.chmod(PRIVATE_DIRECTORY_MODE)
                path = root / f"input{suffix}"
                _write_private_upload(
                    path,
                    payload,
                    temporary_directory=root,
                )
                variants = list(process_vcf(vcf_path=path))
    except (VCFProcessingError, OSError) as exc:
        raise FrontendExecutionError(
            "The analysis input could not be prepared for recovery."
        ) from exc
    return AnalysisRecoveryRequest(
        schema_version=RECOVERY_REQUEST_SCHEMA_VERSION,
        manual_variants=[dict(variant) for variant in variants],
        phenotypes=list(phenotypes),
        clinical_entities=normalized_clinical_entities,
        llm_model=llm_model,
        input_type=(
            input_type
            or ("manual" if uploaded_vcf is None else "vcf")
        ),
        phenotype_extraction_model=phenotype_extraction_model,
        phenotype_extraction_provenance=(
            dict(phenotype_extraction_provenance)
            if phenotype_extraction_provenance is not None
            else None
        ),
        analysis_id=None,
        created_at=time(),
    )


def execute_analysis(
    *,
    uploaded_vcf: UploadedVCF | None,
    manual_variants: Sequence[Mapping[str, object]] | None,
    phenotypes: list[str],
    clinical_entities: Sequence[Mapping[str, object]] | None = None,
    llm_model: str | None = None,
    input_type: str | None = None,
    phenotype_extraction_model: str | None = None,
    phenotype_extraction_provenance: Mapping[str, object] | None = None,
    excel_input_records: Sequence[Mapping[str, object]] | None = None,
    readiness_snapshot: ProviderReadinessSnapshot | None = None,
    progress_callback: PipelineProgressCallback | None = None,
) -> PipelineResult:
    """Execute one manual or temporary-upload analysis request."""

    if uploaded_vcf is not None and (
        manual_variants is not None or excel_input_records is not None
    ):
        raise FrontendExecutionError(
            "Choose either a variant-file upload or manual table rows."
        )

    if excel_input_records is not None:
        if manual_variants is not None:
            raise FrontendExecutionError(
                "Excel source records cannot be combined with manual rows."
            )
        prepared = run_annovar_like_input_processing(
            excel_input_records,
            phenotypes=phenotypes,
            clinical_entities=clinical_entities,
        )
        if not prepared["variants"]:
            return prepared
        canonical_variants = [
            {
                key: value
                for key, value in variant.items()
                if key != "input_index"
            }
            for variant in prepared["variants"]
        ]
        result = run_analysis(
            vcf_path=None,
            manual_variants=canonical_variants,
            phenotypes=phenotypes,
            clinical_entities=clinical_entities,
            llm_model=llm_model,
            input_type=input_type or "excel",
            phenotype_extraction_model=phenotype_extraction_model,
            phenotype_extraction_provenance=phenotype_extraction_provenance,
            readiness_snapshot=readiness_snapshot,
            progress_callback=progress_callback,
        )
        if (
            len(result["variants"]) != len(prepared["variants"])
            or not result["variant_integrity_records"]
        ):
            return result
        attached = attach_input_preprocessing_results(
            result,
            prepared["input_preprocessing_results"],
        )
        if attached["analysis_id"] is not None:
            save_pipeline_state(attached)
        return attached

    if uploaded_vcf is None:
        return run_analysis(
            vcf_path=None,
            manual_variants=manual_variants,
            phenotypes=phenotypes,
            clinical_entities=clinical_entities,
            llm_model=llm_model,
            input_type=input_type or "manual",
            phenotype_extraction_model=phenotype_extraction_model,
            phenotype_extraction_provenance=(
                phenotype_extraction_provenance
            ),
            readiness_snapshot=readiness_snapshot,
            progress_callback=progress_callback,
        )

    suffix = _validate_upload_filename(
        getattr(uploaded_vcf, "name", None)
    )
    payload = _read_upload(uploaded_vcf)
    if suffix == ".xlsx":
        try:
            input_records = parse_excel_input_records(payload)
        except VCFProcessingError as exc:
            raise FrontendExecutionError(str(exc)) from exc
        return execute_analysis(
            uploaded_vcf=None,
            manual_variants=None,
            phenotypes=phenotypes,
            clinical_entities=clinical_entities,
            llm_model=llm_model,
            input_type=input_type or "excel",
            phenotype_extraction_model=phenotype_extraction_model,
            phenotype_extraction_provenance=phenotype_extraction_provenance,
            excel_input_records=input_records,
            readiness_snapshot=readiness_snapshot,
            progress_callback=progress_callback,
        )
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
                clinical_entities=clinical_entities,
                llm_model=llm_model,
                input_type=(
                    input_type
                    or ("vcf_gz" if suffix == ".vcf.gz" else "vcf")
                ),
                phenotype_extraction_model=phenotype_extraction_model,
                phenotype_extraction_provenance=(
                    phenotype_extraction_provenance
                ),
                readiness_snapshot=readiness_snapshot,
                progress_callback=progress_callback,
            )
    except FrontendExecutionError:
        raise
    except OSError as exc:
        raise FrontendExecutionError(
            "The uploaded variant file could not be prepared for analysis."
        ) from exc


def recover_analysis_job(token: object) -> AnalysisJob | None:
    """Restart an interrupted job from its sanitized local checkpoint."""

    if (
        not isinstance(token, str)
        or ANALYSIS_JOB_TOKEN_PATTERN.fullmatch(token) is None
    ):
        return None
    existing = get_registered_analysis_job(token)
    if existing is not None:
        return existing
    request = _load_recovery_request(token)
    if request is None:
        return None

    persisted_analysis_id = request["analysis_id"]
    if persisted_analysis_id is not None:
        try:
            persisted_result = load_pipeline_state(persisted_analysis_id)
        except DatabaseError:
            def persisted_runner(
                _progress: PipelineProgressCallback,
            ) -> PipelineResult:
                raise FrontendExecutionError(
                    "The saved analysis is unavailable or invalid."
                )
        else:
            def persisted_runner(
                _progress: PipelineProgressCallback,
            ) -> PipelineResult:
                return persisted_result
        job = AnalysisJob(persisted_runner)
        try:
            register_analysis_job(job, token=token)
            job.start()
        except (FrontendExecutionError, RuntimeError):
            release_registered_analysis_job(token)
            return None
        return job

    def runner(
        progress_callback: PipelineProgressCallback,
    ) -> PipelineResult:
        return execute_analysis(
            uploaded_vcf=None,
            manual_variants=(
                None
                if "excel_input_records" in request
                else request["manual_variants"]
            ),
            phenotypes=request["phenotypes"],
            clinical_entities=request["clinical_entities"],
            llm_model=request["llm_model"],
            input_type=request["input_type"],
            phenotype_extraction_model=request[
                "phenotype_extraction_model"
            ],
            phenotype_extraction_provenance=request[
                "phenotype_extraction_provenance"
            ],
            excel_input_records=request.get("excel_input_records"),
            progress_callback=progress_callback,
        )

    job = AnalysisJob(runner)
    try:
        register_analysis_job(job, token=token)
        job.start()
    except (FrontendExecutionError, RuntimeError):
        release_registered_analysis_job(token)
        return None
    return job


__all__ = [
    "AnalysisCancelled",
    "AnalysisJob",
    "AnalysisJobState",
    "AnalysisJobView",
    "FrontendExecutionError",
    "UploadedVCF",
    "execute_analysis",
    "get_registered_analysis_job",
    "prepare_analysis_recovery_request",
    "recover_analysis_job",
    "register_analysis_job",
    "release_registered_analysis_job",
    "save_last_session_analysis_id",
    "load_last_session_analysis_id",
]
