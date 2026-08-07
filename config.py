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


def _get_bool(name: str, default: bool) -> bool:
    """Read a strict boolean environment variable."""

    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(
        f"Environment variable '{name}' must be a boolean."
    )


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


def _validate_secure_url(name: str, value: str) -> None:
    """Require a credential-free HTTPS endpoint."""
    try:
        parsed_url = urlsplit(value)
        parsed_url.port
    except ValueError as exc:
        raise RuntimeError(
            f"Environment variable '{name}' must be a secure HTTPS URL."
        ) from exc

    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or bool(parsed_url.query)
        or bool(parsed_url.fragment)
        or any(
            character.isspace() or ord(character) < 32
            for character in value
        )
    ):
        raise RuntimeError(
            f"Environment variable '{name}' must be a secure HTTPS URL "
            "without credentials, query parameters, or fragments."
        )


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

    LLM_MODEL_LIGHT: str = os.getenv(
        "LLM_MODEL_LIGHT",
        LLM_MODEL,
    ).strip()

    LLM_MODEL_STRONG: str = os.getenv(
        "LLM_MODEL_STRONG",
        LLM_MODEL,
    ).strip()

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

    GENEBE_BASE_URL: str = os.getenv(
        "GENEBE_BASE_URL",
        "https://api.genebe.net/cloud",
    ).strip().rstrip("/")

    GENEBE_EMAIL: str | None = (
        os.getenv("GENEBE_EMAIL", "").strip() or None
    )

    GENEBE_API_KEY: str | None = (
        os.getenv("GENEBE_API_KEY", "").strip() or None
    )

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

    CSPEC_BASE_URL: str = os.getenv(
        "CSPEC_BASE_URL",
        "https://cspec.clinicalgenome.org/cspec",
    ).strip().rstrip("/")

    PHEN2GENE_BASE_URL: str = os.getenv(
        "PHEN2GENE_BASE_URL",
        "https://phen2gene.wglab.org/api",
    ).strip().rstrip("/")

    MONARCH_BASE_URL: str = os.getenv(
        "MONARCH_BASE_URL",
        "https://api-v3.monarchinitiative.org/v3/api",
    ).strip().rstrip("/")

    MYDISEASE_BASE_URL: str = os.getenv(
        "MYDISEASE_BASE_URL",
        "https://mydisease.info/v1",
    ).strip().rstrip("/")

    MYDISEASE_TIMEOUT: int = _get_positive_int(
        "MYDISEASE_TIMEOUT",
        30,
    )

    MYDISEASE_MAX_RETRIES: int = _get_non_negative_int(
        "MYDISEASE_MAX_RETRIES",
        2,
    )

    MYDISEASE_CACHE_SIZE: int = _get_positive_int(
        "MYDISEASE_CACHE_SIZE",
        128,
    )

    MYDISEASE_MAX_DISEASES_PER_GENE: int = _get_positive_int(
        "MYDISEASE_MAX_DISEASES_PER_GENE",
        20,
    )

    MYDISEASE_MAX_HPO_TERMS_PER_DISEASE: int = _get_positive_int(
        "MYDISEASE_MAX_HPO_TERMS_PER_DISEASE",
        50,
    )

    GNOMAD_BASE_URL: str = os.getenv(
        "GNOMAD_BASE_URL",
        "https://gnomad.broadinstitute.org/api",
    ).strip().rstrip("/")

    GNOMAD_DATASET_GRCH37: str = os.getenv(
        "GNOMAD_DATASET_GRCH37",
        "gnomad_r2_1",
    ).strip()

    GNOMAD_DATASET_GRCH38: str = os.getenv(
        "GNOMAD_DATASET_GRCH38",
        "gnomad_r4",
    ).strip()

    ENSEMBL_VARIATION_BASE_URL: str = os.getenv(
        "ENSEMBL_VARIATION_BASE_URL",
        "https://rest.ensembl.org",
    ).strip().rstrip("/")

    LITVAR_BASE_URL: str = os.getenv(
        "LITVAR_BASE_URL",
        "https://www.ncbi.nlm.nih.gov/research/litvar2-api",
    ).strip().rstrip("/")

    EUROPE_PMC_BASE_URL: str = os.getenv(
        "EUROPE_PMC_BASE_URL",
        "https://www.ebi.ac.uk/europepmc/webservices/rest",
    ).strip().rstrip("/")

    PUBMED_BASE_URL: str = os.getenv(
        "PUBMED_BASE_URL",
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
    ).strip().rstrip("/")

    CONDITIONAL_ENRICHMENT_TIMEOUT: int = _get_positive_int(
        "CONDITIONAL_ENRICHMENT_TIMEOUT",
        20,
    )

    CONDITIONAL_ENRICHMENT_MAX_RETRIES: int = _get_non_negative_int(
        "CONDITIONAL_ENRICHMENT_MAX_RETRIES",
        1,
    )

    CONDITIONAL_ENRICHMENT_MAX_VARIANTS: int = _get_positive_int(
        "CONDITIONAL_ENRICHMENT_MAX_VARIANTS",
        5,
    )

    CONDITIONAL_ENRICHMENT_MAX_ARTICLES: int = _get_positive_int(
        "CONDITIONAL_ENRICHMENT_MAX_ARTICLES",
        10,
    )

    ENABLE_GNOMAD_DEEP_LOOKUP: bool = _get_bool(
        "ENABLE_GNOMAD_DEEP_LOOKUP",
        True,
    )

    ENABLE_LITERATURE_ENRICHMENT: bool = _get_bool(
        "ENABLE_LITERATURE_ENRICHMENT",
        True,
    )

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

    LLM_MAX_RETRIES: int = _get_non_negative_int(
        "LLM_MAX_RETRIES",
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
        This method checks secure URL formats and other configuration
        constraints.
        """
        url_settings = {
            "LLM_BASE_URL": cls.LLM_BASE_URL,
            "VEP_BASE_URL": cls.VEP_BASE_URL,
            "GENEBE_BASE_URL": cls.GENEBE_BASE_URL,
            "MYVARIANT_BASE_URL": cls.MYVARIANT_BASE_URL,
            "CLINVAR_BASE_URL": cls.CLINVAR_BASE_URL,
            "CLINGEN_BASE_URL": cls.CLINGEN_BASE_URL,
            "CSPEC_BASE_URL": cls.CSPEC_BASE_URL,
            "PHEN2GENE_BASE_URL": cls.PHEN2GENE_BASE_URL,
            "MONARCH_BASE_URL": cls.MONARCH_BASE_URL,
            "MYDISEASE_BASE_URL": cls.MYDISEASE_BASE_URL,
            "GNOMAD_BASE_URL": cls.GNOMAD_BASE_URL,
            "ENSEMBL_VARIATION_BASE_URL": (
                cls.ENSEMBL_VARIATION_BASE_URL
            ),
            "LITVAR_BASE_URL": cls.LITVAR_BASE_URL,
            "EUROPE_PMC_BASE_URL": cls.EUROPE_PMC_BASE_URL,
            "PUBMED_BASE_URL": cls.PUBMED_BASE_URL,
            "HPO_ONTOLOGY_URL": cls.HPO_ONTOLOGY_URL,
            "HPO_GENE_ASSOCIATIONS_URL_TEMPLATE": (
                cls.HPO_GENE_ASSOCIATIONS_URL_TEMPLATE
            ),
            "HPO_DISEASE_ANNOTATIONS_URL_TEMPLATE": (
                cls.HPO_DISEASE_ANNOTATIONS_URL_TEMPLATE
            ),
        }

        for name, value in url_settings.items():
            _validate_secure_url(name, value)

        if (
            "{release}"
            not in cls.HPO_GENE_ASSOCIATIONS_URL_TEMPLATE
        ):
            raise RuntimeError(
                "HPO_GENE_ASSOCIATIONS_URL_TEMPLATE must contain "
                "{release}."
            )

        if (
            "{release}"
            not in cls.HPO_DISEASE_ANNOTATIONS_URL_TEMPLATE
        ):
            raise RuntimeError(
                "HPO_DISEASE_ANNOTATIONS_URL_TEMPLATE must contain "
                "{release}."
            )

        if not cls.APP_NAME:
            raise RuntimeError(
                "APP_NAME cannot be empty."
            )

        if not cls.LLM_PROVIDER:
            raise RuntimeError(
                "LLM_PROVIDER cannot be empty."
            )

        if not cls.LLM_MODEL_LIGHT or not cls.LLM_MODEL_STRONG:
            raise RuntimeError(
                "LLM_MODEL_LIGHT and LLM_MODEL_STRONG cannot be empty."
            )

        if not isinstance(cls.ENABLE_GNOMAD_DEEP_LOOKUP, bool) or not isinstance(
            cls.ENABLE_LITERATURE_ENRICHMENT,
            bool,
        ):
            raise RuntimeError(
                "Conditional-enrichment feature flags must be booleans."
            )

        if cls.GENOME_ASSEMBLY not in {"GRCh37", "GRCh38"}:
            raise RuntimeError(
                "GENOME_ASSEMBLY must be GRCh37 or GRCh38."
            )

        if (cls.GENEBE_EMAIL is None) != (
            cls.GENEBE_API_KEY is None
        ):
            raise RuntimeError(
                "GENEBE_EMAIL and GENEBE_API_KEY must be configured "
                "together."
            )

        if cls.VEP_BATCH_SIZE > 200:
            raise RuntimeError(
                "VEP_BATCH_SIZE cannot exceed Ensembl's limit of 200."
            )

        if cls.MYDISEASE_TIMEOUT > 120:
            raise RuntimeError(
                "MYDISEASE_TIMEOUT cannot exceed 120."
            )

        if cls.MYDISEASE_MAX_RETRIES > 10:
            raise RuntimeError(
                "MYDISEASE_MAX_RETRIES cannot exceed 10."
            )

        if cls.MYDISEASE_CACHE_SIZE > 1000:
            raise RuntimeError(
                "MYDISEASE_CACHE_SIZE cannot exceed 1000."
            )

        if cls.MYDISEASE_MAX_DISEASES_PER_GENE > 100:
            raise RuntimeError(
                "MYDISEASE_MAX_DISEASES_PER_GENE cannot exceed 100."
            )

        if cls.MYDISEASE_MAX_HPO_TERMS_PER_DISEASE > 200:
            raise RuntimeError(
                "MYDISEASE_MAX_HPO_TERMS_PER_DISEASE cannot exceed 200."
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
