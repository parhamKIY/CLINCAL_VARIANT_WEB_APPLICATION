import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


# Root directory of the project
BASE_DIR = Path(__file__).resolve().parent
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

# Treat the project's .env file as the single source of configuration.
load_dotenv(BASE_DIR / ".env", override=True)


def _get_required_env(name: str) -> str:
    """
    Read a required environment variable.

    Raises:
        RuntimeError: If the variable is missing or empty.
    """
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Required environment variable '{name}' is missing."
        )

    return value


def _get_positive_int(name: str, default: int) -> int:
    """
    Read a positive integer environment variable.
    """
    raw_value = os.getenv(name, str(default)).strip()

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"Environment variable '{name}' must be an integer."
        ) from exc

    if value <= 0:
        raise RuntimeError(
            f"Environment variable '{name}' must be greater than zero."
        )

    return value


def _get_non_negative_int(name: str, default: int) -> int:
    """
    Read a non-negative integer environment variable.
    """
    raw_value = os.getenv(name, str(default)).strip()

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"Environment variable '{name}' must be an integer."
        ) from exc

    if value < 0:
        raise RuntimeError(
            f"Environment variable '{name}' cannot be negative."
        )

    return value


def _resolve_path(name: str, default: str) -> Path:
    """
    Resolve a path from an environment variable.

    Relative paths are resolved from the project root.
    Absolute paths remain unchanged.
    """
    raw_path = os.getenv(name, "").strip() or default
    path = Path(raw_path).expanduser()

    if not path.is_absolute():
        path = BASE_DIR / path

    return path.resolve()


class Settings:
    """
    Central configuration for the application.

    All project modules should import configuration values from this class
    instead of reading environment variables directly.
    """

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------

    APP_NAME: str = os.getenv(
        "APP_NAME",
        "Clinical Variant Interpretation",
    ).strip()

    # ------------------------------------------------------------------
    # LLM service
    # ------------------------------------------------------------------

    LLM_PROVIDER: str = os.getenv(
        "LLM_PROVIDER",
        "openai_compatible",
    ).strip().casefold()

    LLM_BASE_URL: str = _get_required_env(
        "LLM_BASE_URL"
    ).rstrip("/")

    LLM_API_KEY: str = _get_required_env(
        "LLM_API_KEY"
    )

    LLM_MODEL: str = _get_required_env(
        "LLM_MODEL"
    )

    LLM_TIMEOUT: int = _get_positive_int(
        "LLM_TIMEOUT",
        30,
    )

    # ------------------------------------------------------------------
    # External bioinformatics services
    # ------------------------------------------------------------------

    VEP_BASE_URL: str = os.getenv(
        "VEP_BASE_URL",
        "https://rest.ensembl.org",
    ).strip().rstrip("/")

    MYVARIANT_BASE_URL: str = os.getenv(
        "MYVARIANT_BASE_URL",
        "https://myvariant.info/v1",
    ).strip().rstrip("/")

    CLINVAR_BASE_URL: str = os.getenv(
        "CLINVAR_BASE_URL",
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
    ).strip().rstrip("/")

    CLINGEN_BASE_URL: str = os.getenv(
        "CLINGEN_BASE_URL",
        "https://genome-euro.ucsc.edu/cgi-bin/hubApi",
    ).strip().rstrip("/")

    HPO_ONTOLOGY_URL: str = os.getenv(
        "HPO_ONTOLOGY_URL",
        "https://purl.obolibrary.org/obo/hp.obo",
    ).strip()

    HPO_GENE_ASSOCIATIONS_URL_TEMPLATE: str = os.getenv(
        "HPO_GENE_ASSOCIATIONS_URL_TEMPLATE",
        (
            "https://github.com/obophenotype/"
            "human-phenotype-ontology/releases/download/"
            "v{release}/"
            "phenotype_to_genes.txt"
        ),
    ).strip()

    HPO_DISEASE_ANNOTATIONS_URL_TEMPLATE: str = os.getenv(
        "HPO_DISEASE_ANNOTATIONS_URL_TEMPLATE",
        (
            "https://github.com/obophenotype/"
            "human-phenotype-ontology/releases/download/"
            "v{release}/phenotype.hpoa"
        ),
    ).strip()

    # Genome assembly must remain explicit when coordinates are sent to
    # external annotation services.
    GENOME_ASSEMBLY: str = _get_required_env(
        "GENOME_ASSEMBLY"
    )

    VEP_BATCH_SIZE: int = _get_positive_int(
        "VEP_BATCH_SIZE",
        50,
    )

    ANNOTATION_MAX_RETRIES: int = _get_non_negative_int(
        "ANNOTATION_MAX_RETRIES",
        2,
    )

    # ------------------------------------------------------------------
    # Request settings
    # ------------------------------------------------------------------

    REQUEST_TIMEOUT: int = _get_positive_int(
        "REQUEST_TIMEOUT",
        30,
    )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    LOG_LEVEL: str = os.getenv(
        "LOG_LEVEL",
        "INFO",
    ).strip().upper()

    LOG_PATH: Path = _resolve_path(
        "LOG_PATH",
        "storage/logs/clinical_variant.log",
    )

    LOG_MAX_BYTES: int = _get_positive_int(
        "LOG_MAX_BYTES",
        5_000_000,
    )

    LOG_BACKUP_COUNT: int = _get_non_negative_int(
        "LOG_BACKUP_COUNT",
        3,
    )

    # ------------------------------------------------------------------
    # Storage paths
    # ------------------------------------------------------------------

    UPLOAD_DIR: Path = _resolve_path(
        "UPLOAD_DIR",
        "storage/uploads",
    )

    MAX_UPLOAD_BYTES: int = _get_positive_int(
        "MAX_UPLOAD_BYTES",
        25_000_000,
    )

    MAX_UNCOMPRESSED_VCF_BYTES: int = _get_positive_int(
        "MAX_UNCOMPRESSED_VCF_BYTES",
        100_000_000,
    )

    REPORT_DIR: Path = _resolve_path(
        "REPORT_DIR",
        "storage/reports",
    )

    DATABASE_PATH: Path = _resolve_path(
        "DATABASE_PATH",
        "storage/database/clinical_variant.sqlite3",
    )

    CACHE_DIR: Path = _resolve_path(
        "CACHE_DIR",
        "data/cache",
    )

    HPO_DATA_DIR: Path = _resolve_path(
        "HPO_DATA_DIR",
        "data/hpo",
    )

    # ------------------------------------------------------------------
    # Pipeline settings
    # ------------------------------------------------------------------

    TOP_VARIANTS: int = _get_positive_int(
        "TOP_VARIANTS",
        10,
    )

    @classmethod
    def create_directories(cls) -> None:
        """
        Create required application directories if they do not exist.
        """
        directories = (
            cls.UPLOAD_DIR,
            cls.REPORT_DIR,
            cls.DATABASE_PATH.parent,
            cls.CACHE_DIR,
            cls.HPO_DATA_DIR,
            cls.LOG_PATH.parent,
        )

        for directory in directories:
            directory.mkdir(
                mode=PRIVATE_DIRECTORY_MODE,
                parents=True,
                exist_ok=True,
            )
            directory.chmod(PRIVATE_DIRECTORY_MODE)

    @classmethod
    def validate(cls) -> None:
        """
        Validate central configuration values.

        Required LLM variables are already validated while loading the class.
        This method checks URL formats and other configuration constraints.
        """
        url_settings = {
            "LLM_BASE_URL": cls.LLM_BASE_URL,
            "VEP_BASE_URL": cls.VEP_BASE_URL,
            "MYVARIANT_BASE_URL": cls.MYVARIANT_BASE_URL,
            "CLINVAR_BASE_URL": cls.CLINVAR_BASE_URL,
            "CLINGEN_BASE_URL": cls.CLINGEN_BASE_URL,
            "HPO_ONTOLOGY_URL": cls.HPO_ONTOLOGY_URL,
            "HPO_GENE_ASSOCIATIONS_URL_TEMPLATE": (
                cls.HPO_GENE_ASSOCIATIONS_URL_TEMPLATE
            ),
            "HPO_DISEASE_ANNOTATIONS_URL_TEMPLATE": (
                cls.HPO_DISEASE_ANNOTATIONS_URL_TEMPLATE
            ),
        }

        for name, value in url_settings.items():
            parsed_url = urlsplit(value)

            if (
                parsed_url.scheme not in {"http", "https"}
                or not parsed_url.netloc
            ):
                raise RuntimeError(
                    f"Environment variable '{name}' must be a valid "
                    "HTTP or HTTPS URL."
                )

        if urlsplit(cls.HPO_ONTOLOGY_URL).scheme != "https":
            raise RuntimeError(
                "HPO_ONTOLOGY_URL must use HTTPS."
            )

        if (
            urlsplit(
                cls.HPO_GENE_ASSOCIATIONS_URL_TEMPLATE
            ).scheme
            != "https"
            or "{release}"
            not in cls.HPO_GENE_ASSOCIATIONS_URL_TEMPLATE
        ):
            raise RuntimeError(
                "HPO_GENE_ASSOCIATIONS_URL_TEMPLATE must use HTTPS "
                "and contain {release}."
            )

        if (
            urlsplit(
                cls.HPO_DISEASE_ANNOTATIONS_URL_TEMPLATE
            ).scheme
            != "https"
            or "{release}"
            not in cls.HPO_DISEASE_ANNOTATIONS_URL_TEMPLATE
        ):
            raise RuntimeError(
                "HPO_DISEASE_ANNOTATIONS_URL_TEMPLATE must use HTTPS "
                "and contain {release}."
            )

        if not cls.APP_NAME:
            raise RuntimeError(
                "APP_NAME cannot be empty."
            )

        if not cls.LLM_PROVIDER:
            raise RuntimeError(
                "LLM_PROVIDER cannot be empty."
            )

        if cls.GENOME_ASSEMBLY not in {"GRCh37", "GRCh38"}:
            raise RuntimeError(
                "GENOME_ASSEMBLY must be GRCh37 or GRCh38."
            )

        if cls.VEP_BATCH_SIZE > 200:
            raise RuntimeError(
                "VEP_BATCH_SIZE cannot exceed Ensembl's limit of 200."
            )

        if cls.LOG_LEVEL not in {
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
        }:
            raise RuntimeError(
                "LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, "
                "or CRITICAL."
            )

        if cls.MAX_UPLOAD_BYTES > 100_000_000:
            raise RuntimeError(
                "MAX_UPLOAD_BYTES cannot exceed 100000000."
            )

        if cls.MAX_UNCOMPRESSED_VCF_BYTES > 500_000_000:
            raise RuntimeError(
                "MAX_UNCOMPRESSED_VCF_BYTES cannot exceed 500000000."
            )

        if cls.DATABASE_PATH.exists() and cls.DATABASE_PATH.is_dir():
            raise RuntimeError(
                "DATABASE_PATH must point to a file, not a directory."
            )

        if cls.LOG_PATH.exists() and cls.LOG_PATH.is_dir():
            raise RuntimeError(
                "LOG_PATH must point to a file, not a directory."
            )

    @classmethod
    def initialize(cls) -> None:
        """
        Validate the configuration and prepare required directories.
        """
        cls.validate()
        cls.create_directories()


settings = Settings()
