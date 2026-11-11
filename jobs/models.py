"""
Modelo de dados.

    Project 1──N Version 1──N Job 1──N JobStep
                                 1──N Artifact
                                 1──N CoverageResult   (prng e/ou quaigh)

Uma Version é um commit (ou um upload sem commit). Rodar o mesmo commit duas
vezes gera dois Jobs na mesma Version, o que é útil para medir reprodutibilidade.
"""

import uuid

from django.db import models
from django.utils import timezone


class Project(models.Model):
    name = models.SlugField(max_length=100, unique=True)
    repo_url = models.URLField(blank=True, default="")
    default_branch = models.CharField(max_length=200, default="main")
    # sobrescreve DFT_WEBHOOK_URL para este projeto
    webhook_url = models.URLField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def github_repo(self) -> str | None:
        """'dono/repo' se o repo_url for do GitHub."""
        from urllib.parse import urlparse

        p = urlparse(self.repo_url or "")
        if p.hostname not in {"github.com", "www.github.com"}:
            return None
        parts = p.path.strip("/").removesuffix(".git").split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else None


class Version(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="versions")
    commit_sha = models.CharField(max_length=64, blank=True, default="")
    branch = models.CharField(max_length=200, blank=True, default="")
    message = models.TextField(blank=True, default="")
    author = models.CharField(max_length=200, blank=True, default="")
    # hash do conteúdo de entrada (fontes + dft.yaml); mesmo hash = mesma entrada
    input_sha256 = models.CharField(max_length=64, db_index=True, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["project", "branch", "created_at"])]

    def __str__(self):
        return f"{self.project.name}@{self.commit_sha[:8] or self.input_sha256[:8]}"


class Job(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "na fila"
        RUNNING = "running", "executando"
        PASSED = "passed", "aprovado"
        FAILED = "failed", "reprovado"      # rodou, mas reprovou (regressão/alvo)
        ERROR = "error", "erro"             # o fluxo quebrou (ferramenta, RTL, timeout)
        CANCELED = "canceled", "cancelado"

    FINAL = {Status.PASSED, Status.FAILED, Status.ERROR, Status.CANCELED}

    class Source(models.TextChoices):
        UPLOAD = "upload", "upload"
        GIT = "git", "git"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="jobs")
    version = models.ForeignKey(Version, on_delete=models.CASCADE, related_name="jobs")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.UPLOAD)
    repo_url = models.URLField(blank=True, default="")
    config_yaml = models.TextField(blank=True, default="")   # o dft.yaml como enviado
    config = models.JSONField(default=dict, blank=True)      # validado e com padrões
    # para jobs git sem dft.yaml no corpo: caminho do dft.yaml dentro do repositório
    config_path = models.CharField(max_length=300, blank=True, default="dft.yaml")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    worker = models.CharField(max_length=200, blank=True, default="")
    error = models.TextField(blank=True, default="")
    # métricas principais (desnormalizadas para consultas de histórico rápidas)
    coverage_pct = models.FloatField(null=True, blank=True)
    vector_count = models.IntegerField(null=True, blank=True)
    area_um2 = models.FloatField(null=True, blank=True)           # netlist final
    tools = models.JSONField(default=dict, blank=True)            # versões das ferramentas
    report = models.JSONField(default=dict, blank=True)           # relatório completo
    # resultado do detector de regressão
    baseline = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    regression = models.JSONField(default=dict, blank=True)
    # assinatura das métricas (cobertura, nº de vetores, área) para reprodutibilidade
    result_fingerprint = models.CharField(max_length=64, blank=True, default="")
    celery_task_id = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["project", "status", "created_at"])]

    def __str__(self):
        return f"job {self.id} ({self.status})"

    @property
    def is_final(self) -> bool:
        return self.status in self.FINAL

    @property
    def queue_seconds(self) -> float | None:
        if self.started_at:
            return (self.started_at - self.created_at).total_seconds()
        return None

    @property
    def run_seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    @property
    def total_seconds(self) -> float | None:
        """Do envio ao veredito: é o 'tempo para detectar uma regressão'."""
        if self.finished_at:
            return (self.finished_at - self.created_at).total_seconds()
        return None


class JobStep(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        OK = "ok"
        FAILED = "failed"
        SKIPPED = "skipped"

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="steps")
    order = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=50)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    command = models.TextField(blank=True, default="")
    return_code = models.IntegerField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_s = models.FloatField(null=True, blank=True)
    log_path = models.CharField(max_length=500, blank=True, default="")  # relativo ao diretório do job
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["job", "order"]
        unique_together = [("job", "name")]

    def __str__(self):
        return f"{self.job_id}:{self.name}"


class CoverageResult(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="coverage")
    generator = models.CharField(max_length=20)       # prng | quaigh
    primary = models.BooleanField(default=False)      # é o que decide aprovação
    fault_sites = models.IntegerField()
    total_faults = models.IntegerField()              # stuck-at 0 + stuck-at 1
    detected = models.IntegerField()
    coverage_pct = models.FloatField()
    vectors_generated = models.IntegerField(null=True, blank=True)
    vectors_final = models.IntegerField(null=True, blank=True)   # após compactação
    undetected = models.JSONField(default=list, blank=True)       # ["_18_.Y/sa0", ...]
    vectors_sha256 = models.CharField(max_length=64, blank=True, default="")  # conjunto canônico
    duration_s = models.FloatField(null=True, blank=True)

    class Meta:
        unique_together = [("job", "generator")]


class Artifact(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="artifacts")
    name = models.CharField(max_length=255)       # nome do arquivo
    kind = models.CharField(max_length=40)        # netlist, vectors, coverage, log, bench...
    path = models.CharField(max_length=500)       # relativo ao diretório do job
    size = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["job", "kind", "name"]
        unique_together = [("job", "name")]

    def __str__(self):
        return self.name
