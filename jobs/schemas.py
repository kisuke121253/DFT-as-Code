from __future__ import annotations

from datetime import datetime
from typing import Any

from ninja import Schema
from pydantic import Field


class ErrorOut(Schema):
    detail: str
    errors: list[str] = []


class ProjectIn(Schema):
    name: str = Field(..., pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    repo_url: str = ""
    default_branch: str = "main"
    webhook_url: str = ""


class ProjectPatch(Schema):
    repo_url: str | None = None
    default_branch: str | None = None
    webhook_url: str | None = None


class ProjectOut(Schema):
    name: str
    repo_url: str
    default_branch: str
    webhook_url: str
    created_at: datetime
    jobs: int = 0
    last_job: dict | None = None


class GitJobIn(Schema):
    repo_url: str = Field(..., description="URL HTTPS do repositório")
    commit_sha: str = Field(..., min_length=7, max_length=64)
    branch: str = ""
    message: str = ""
    author: str = ""
    project: str | None = Field(None, description="obrigatório se dft_yaml não for enviado")
    dft_yaml: str = Field("", description="conteúdo do dft.yaml; vazio = ler do repositório")
    config_path: str = "dft.yaml"


class JobCreatedOut(Schema):
    id: str
    status: str
    project: str
    url: str
    report_url: str


class StepOut(Schema):
    name: str
    status: str
    duration_s: float | None
    started_at: datetime | None
    finished_at: datetime | None


class JobOut(Schema):
    id: str
    status: str
    project: str
    commit: str
    branch: str
    source: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    coverage_pct: float | None
    vector_count: int | None
    area_um2: float | None
    regression: dict | None
    error: str | None
    steps: list[StepOut] = []


class ValidateIn(Schema):
    dft_yaml: str


class ValidateOut(Schema):
    valid: bool
    errors: list[str] = []
    config: dict[str, Any] | None = None
