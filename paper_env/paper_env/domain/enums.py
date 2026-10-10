from enum import StrEnum


class CellStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    STALE = "stale"
    BLOCKED = "blocked"  # paused by the pre-execute hook (deny / needs_approval)


class Author(StrEnum):
    HUMAN = "human"
    GENERATOR = "generator"
    AGENT = "agent"


class RunMode(StrEnum):
    CELL = "cell"
    UNTIL = "until"
    ALL = "all"
    CLEAN = "clean"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"  # experiment failure (a cell raised)
    INFRA_FAILED = "infra_failed"  # kernel died / OOM / env / backend problem
    BLOCKED = "blocked"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class FailureKind(StrEnum):
    """Distinguishes a cell exception from an infrastructure problem, everywhere."""

    EXPERIMENT = "experiment"
    INFRASTRUCTURE = "infrastructure"


class SourceType(StrEnum):
    HUGGINGFACE = "huggingface"
    KAGGLE = "kaggle"
    URL = "url"
    S3 = "s3"
    USER_UPLOAD = "user_upload"
    LOCAL_PATH = "local_path"


class AccessType(StrEnum):
    PUBLIC = "public"
    GATED = "gated"
    USER_UPLOADED = "user_uploaded"


class DatasetStatus(StrEnum):
    REMOTE_ONLY = "remote_only"
    DOWNLOADING = "downloading"
    CACHED = "cached"
    VERIFIED = "verified"
    CORRUPTED = "corrupted"
    UPSTREAM_CHANGED = "upstream_changed"


class SplitProvenance(StrEnum):
    PAPER_OFFICIAL = "paper_official"
    APPROXIMATE = "approximate"


class DecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


class ActionKind(StrEnum):
    EXECUTE_CELL = "execute_cell"
    INSTALL_PACKAGE = "install_package"
    PIN_VERSION = "pin_version"
    REBUILD_ENV = "rebuild_env"
    FETCH_DATASET = "fetch_dataset"


class BlockedActionStatus(StrEnum):
    PENDING = "pending"
    ALLOWED = "allowed"
    DENIED = "denied"


class EventType(StrEnum):
    CELL_STARTED = "cell_started"
    CELL_OUTPUT = "cell_output"
    CELL_FINISHED = "cell_finished"
    CELL_FAILED = "cell_failed"
    CELL_EDITED = "cell_edited"
    ENV_CHANGED = "env_changed"
    ENV_INSTALL_FAILED = "env_install_failed"
    KERNEL_DIED = "kernel_died"
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    INPUT_REQUESTED = "input_requested"
    COMM_MESSAGE = "comm_message"  # ephemeral (not persisted): widget traffic
    ACTION_BLOCKED = "action_blocked"
    ACTION_RESOLVED = "action_resolved"
    CHECKPOINT_SAVED = "checkpoint_saved"
    CHECKPOINT_RESTORED = "checkpoint_restored"
    DATASET_FETCH_STARTED = "dataset_fetch_started"
    DATASET_FETCH_PROGRESS = "dataset_fetch_progress"
    DATASET_VERIFIED = "dataset_verified"
    DATASET_EVICTED = "dataset_evicted"
    DATASET_CACHED = "dataset_cached"  # fetched, but no expected checksum existed to verify against
    DATASET_FETCH_FAILED = "dataset_fetch_failed"
    DATASET_UPSTREAM_CHANGED = "dataset_upstream_changed"
