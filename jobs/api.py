"""
API REST (Django Ninja). Documentação interativa em /api/v1/docs.

Autenticação: header "Authorization: Bearer <token>" com um dos tokens de
DFT_API_TOKENS. Se a variável estiver vazia, a API fica aberta (só desenvolvimento).
"""

from __future__ import annotations

import hmac
import mimetypes
from datetime import timedelta
from statistics import mean, median

from django.conf import settings
from django.db import connection
from django.db.models import Count
from django.http import FileResponse, HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import File, Form, NinjaAPI, Query, Status
from ninja.files import UploadedFile
from ninja.security import HttpBearer

from . import services, storage
from .dftconfig import DftConfigError, parse_dft_yaml
from .models import Artifact, Job, JobStep, Project
from .schemas import (ErrorOut, GitJobIn, JobCreatedOut, JobOut, ProjectIn, ProjectOut, ProjectPatch,
                      ValidateIn, ValidateOut)


class TokenAuth(HttpBearer):
    def __call__(self, request):
        if not settings.DFT_API_TOKENS:
            return "anonymous"
        return super().__call__(request)

    def authenticate(self, request, token):
        for t in settings.DFT_API_TOKENS:
            if hmac.compare_digest(token, t):
                return "token"
        return None


api = NinjaAPI(
    title="DFT as Code",
    version="1.0.0",
    description="Pipeline de CI para Design for Test: síntese, scan chain, JTAG, ATPG e cobertura por versão do RTL.",
    auth=TokenAuth(),
    urls_namespace="api",
)


@api.exception_handler(DftConfigError)
def _cfg_error(request, exc: DftConfigError):
    return api.create_response(request, {"detail": "dft.yaml inválido", "errors": exc.errors}, status=422)


@api.exception_handler(storage.InputError)
def _input_error(request, exc: storage.InputError):
    return api.create_response(request, {"detail": str(exc), "errors": []}, status=400)


def _job_links(job: Job) -> dict:
    return {"url": f"/api/v1/jobs/{job.id}", "report_url": f"/api/v1/jobs/{job.id}/report"}


def _job_out(job: Job, with_steps: bool = True) -> dict:
    return {
        "id": str(job.id), "status": job.status, "project": job.project.name,
        "commit": job.version.commit_sha, "branch": job.version.branch, "source": job.source,
        "created_at": job.created_at, "started_at": job.started_at, "finished_at": job.finished_at,
        "coverage_pct": job.coverage_pct, "vector_count": job.vector_count, "area_um2": job.area_um2,
        "regression": job.regression or None, "error": job.error or None,
        "steps": [
            {"name": s.name, "status": s.status, "duration_s": s.duration_s,
             "started_at": s.started_at, "finished_at": s.finished_at}
            for s in job.steps.all()
        ] if with_steps else [],
    }


# ======================================================================= saúde
@api.get("/health", auth=None, tags=["sistema"])
def health(request):
    """Verifica banco e fila. Não exige token."""
    out = {"status": "ok", "database": "ok", "queue": "ok"}
    try:
        with connection.cursor() as c:
            c.execute("SELECT 1")
    except Exception as e:  # noqa: BLE001
        out.update(status="degraded", database=str(e))
    if not settings.CELERY_TASK_ALWAYS_EAGER:
        try:
            from dft_backend.celery import app

            with app.connection_for_write() as conn:
                conn.ensure_connection(max_retries=1, timeout=2)
        except Exception as e:  # noqa: BLE001
            out.update(status="degraded", queue=str(e))
    return out


# ======================================================================= dft.yaml
@api.post("/validate", response={200: ValidateOut}, tags=["dft.yaml"])
def validate(request, body: ValidateIn):
    """Valida um dft.yaml sem criar job. Devolve a configuração com todos os padrões preenchidos."""
    try:
        cfg = parse_dft_yaml(body.dft_yaml)
    except DftConfigError as e:
        return {"valid": False, "errors": e.errors, "config": None}
    return {"valid": True, "errors": [], "config": cfg.model_dump()}


# ======================================================================= projetos
@api.get("/projects", response=list[ProjectOut], tags=["projetos"])
def list_projects(request):
    out = []
    for p in Project.objects.annotate(n=Count("jobs")):
        last = p.jobs.select_related("version").order_by("-created_at").first()
        out.append({
            "name": p.name, "repo_url": p.repo_url, "default_branch": p.default_branch,
            "webhook_url": p.webhook_url, "created_at": p.created_at, "jobs": p.n,
            "last_job": {"id": str(last.id), "status": last.status, "coverage_pct": last.coverage_pct,
                         "commit": last.version.commit_sha, "created_at": last.created_at.isoformat()}
            if last else None,
        })
    return out


@api.post("/projects", response={201: ProjectOut, 409: ErrorOut}, tags=["projetos"])
def create_project(request, body: ProjectIn):
    """Cadastro explícito (opcional: o projeto também é criado no primeiro job)."""
    if Project.objects.filter(name=body.name).exists():
        return Status(409, {"detail": "projeto já existe"})
    p = Project.objects.create(**body.dict())
    return Status(201, {**body.dict(), "created_at": p.created_at, "jobs": 0, "last_job": None})


@api.patch("/projects/{name}", response=ProjectOut, tags=["projetos"])
def update_project(request, name: str, body: ProjectPatch):
    p = get_object_or_404(Project, name=name)
    for k, v in body.dict(exclude_none=True).items():
        setattr(p, k, v)
    p.save()
    return {"name": p.name, "repo_url": p.repo_url, "default_branch": p.default_branch,
            "webhook_url": p.webhook_url, "created_at": p.created_at, "jobs": p.jobs.count()}


@api.get("/projects/{name}/history", tags=["projetos"])
def project_history(request, name: str, branch: str | None = None, limit: int = Query(100, le=1000)):
    """Cobertura, vetores e área por versão, do mais recente para o mais antigo."""
    p = get_object_or_404(Project, name=name)
    return {"project": p.name, "branch": branch, "items": services.history(p, branch, limit)}


# ======================================================================= jobs
@api.post("/jobs", response={202: JobCreatedOut, 400: ErrorOut, 422: ErrorOut}, tags=["jobs"])
def submit_job(
    request: HttpRequest,
    dft_yaml: str = Form(None, description="conteúdo do dft.yaml (ou envie o arquivo em dft_yaml_file)"),
    commit_sha: str = Form(""),
    branch: str = Form(""),
    message: str = Form(""),
    author: str = Form(""),
    repo_url: str = Form(""),
    dft_yaml_file: UploadedFile = File(None),
    files: list[UploadedFile] = File(None, description="arquivos Verilog avulsos"),
    archive: UploadedFile = File(None, description=".tar.gz/.tgz/.tar/.zip com o RTL"),
):
    """
    Envia RTL + dft.yaml (multipart/form-data) e cria um job na fila.

    O RTL pode vir como arquivos avulsos (`files`, um ou mais) ou compactado (`archive`).
    Os caminhos dentro do envio são casados com `sources` do dft.yaml.
    """
    text = dft_yaml or (dft_yaml_file.read().decode("utf-8", "replace") if dft_yaml_file else "")
    if not text.strip():
        return Status(400, {"detail": "envie o dft.yaml (campo dft_yaml ou dft_yaml_file)"})
    if not files and not archive:
        return Status(400, {"detail": "envie o RTL em 'files' ou 'archive'"})
    job = services.create_upload_job(
        config_yaml=text,
        files=[(f.name, f.read()) for f in (files or [])],
        archives=[(archive.name, archive.read())] if archive else [],
        commit_sha=commit_sha, branch=branch, message=message, author=author, repo_url=repo_url,
    )
    return Status(202, {"id": str(job.id), "status": job.status, "project": job.project.name, **_job_links(job)})


@api.post("/jobs/git", response={202: JobCreatedOut, 400: ErrorOut, 422: ErrorOut}, tags=["jobs"])
def submit_git_job(request, body: GitJobIn):
    """Cria um job a partir de um commit; o worker clona o repositório."""
    if not body.repo_url.startswith(("https://", "http://")):
        return Status(400, {"detail": "repo_url precisa ser HTTP(S)"})
    job = services.create_git_job(
        project_name=body.project, repo_url=body.repo_url, commit_sha=body.commit_sha, branch=body.branch,
        message=body.message, author=body.author, config_yaml=body.dft_yaml, config_path=body.config_path,
    )
    return Status(202, {"id": str(job.id), "status": job.status, "project": job.project.name, **_job_links(job)})


@api.get("/jobs", response=list[JobOut], tags=["jobs"])
def list_jobs(request, project: str | None = None, status: str | None = None, branch: str | None = None,
              commit: str | None = None, limit: int = Query(50, le=500)):
    qs = Job.objects.select_related("project", "version").prefetch_related("steps")
    if project:
        qs = qs.filter(project__name=project)
    if status:
        qs = qs.filter(status=status)
    if branch:
        qs = qs.filter(version__branch=branch)
    if commit:
        qs = qs.filter(version__commit_sha__startswith=commit)
    return [_job_out(j, with_steps=False) for j in qs[:limit]]


@api.get("/jobs/{job_id}", response=JobOut, tags=["jobs"])
def get_job(request, job_id: str):
    """Status do job e de cada etapa (use para acompanhar a execução)."""
    job = get_object_or_404(Job.objects.select_related("project", "version"), pk=job_id)
    return _job_out(job)


@api.get("/jobs/{job_id}/report", tags=["jobs"])
def get_report(request, job_id: str):
    """Relatório completo: cobertura, comparação PRNG × Quaigh, scan, JTAG, área, regressão, artefatos."""
    job = get_object_or_404(Job.objects.select_related("project", "version"), pk=job_id)
    return services.build_report(job)


@api.post("/jobs/{job_id}/cancel", response={200: JobOut, 409: ErrorOut}, tags=["jobs"])
def cancel_job(request, job_id: str):
    """Cancela um job na fila ou em execução (em execução, para antes da próxima etapa)."""
    job = get_object_or_404(Job.objects.select_related("project", "version"), pk=job_id)
    if job.is_final:
        return Status(409, {"detail": f"job já terminou ({job.status})"})
    was_queued = job.status == Job.Status.QUEUED
    Job.objects.filter(pk=job.pk).update(status=Job.Status.CANCELED)
    if was_queued:
        Job.objects.filter(pk=job.pk).update(finished_at=timezone.now(), error="cancelado antes de iniciar")
        if job.celery_task_id:
            from dft_backend.celery import app

            app.control.revoke(job.celery_task_id)
    job.refresh_from_db()
    return Status(200, _job_out(job))


@api.post("/jobs/{job_id}/retry", response={202: JobCreatedOut, 409: ErrorOut}, tags=["jobs"])
def retry_job(request, job_id: str):
    """Roda de novo a mesma entrada (mesmo commit/arquivos) em um job novo. Útil para medir reprodutibilidade."""
    old = get_object_or_404(Job.objects.select_related("project", "version"), pk=job_id)
    if not old.is_final:
        return Status(409, {"detail": "job ainda não terminou"})
    if old.source == Job.Source.GIT:
        job = services.create_git_job(
            project_name=old.project.name, repo_url=old.repo_url, commit_sha=old.version.commit_sha,
            branch=old.version.branch, message=old.version.message, author=old.version.author,
            config_yaml=old.config_yaml, config_path=old.config_path)
    else:
        src = storage.input_dir(old.id)
        files = [(rel, (src / rel).read_bytes()) for rel in storage.list_files(src) if rel != ".dft.yaml"]
        job = services.create_upload_job(
            config_yaml=old.config_yaml, files=files, archives=[], commit_sha=old.version.commit_sha,
            branch=old.version.branch, message=old.version.message, author=old.version.author,
            repo_url=old.repo_url)
    return Status(202, {"id": str(job.id), "status": job.status, "project": job.project.name, **_job_links(job)})


@api.get("/jobs/{job_id}/logs/{step}", tags=["jobs"])
def get_log(request, job_id: str, step: str):
    """Log de uma etapa em texto puro (synth, cut, atpg, nl2bench, quaigh, chain, asm, tap, area)."""
    job = get_object_or_404(Job, pk=job_id)
    st = get_object_or_404(JobStep, job=job, name=step)
    if not st.log_path:
        return api.create_response(request, {"detail": "etapa sem log"}, status=404)
    path = storage.job_dir(job.id) / st.log_path
    if not storage.is_within(path, storage.job_dir(job.id)) or not path.exists():
        return api.create_response(request, {"detail": "log não encontrado"}, status=404)
    return FileResponse(open(path, "rb"), content_type="text/plain; charset=utf-8")


@api.get("/jobs/{job_id}/artifacts", tags=["artefatos"])
def list_artifacts(request, job_id: str):
    job = get_object_or_404(Job, pk=job_id)
    return [{"name": a.name, "kind": a.kind, "size": a.size, "sha256": a.sha256,
             "url": services.artifact_url(job, a.name)} for a in job.artifacts.all()]


@api.get("/jobs/{job_id}/artifacts/{path:name}", tags=["artefatos"])
def download_artifact(request, job_id: str, name: str):
    """Baixa um artefato (netlist com scan/JTAG, vetores, cobertura, logs, report.json)."""
    job = get_object_or_404(Job, pk=job_id)
    a = get_object_or_404(Artifact, job=job, name=name)
    root = storage.job_dir(job.id)
    path = root / a.path
    if not storage.is_within(path, root) or not path.exists():
        return api.create_response(request, {"detail": "arquivo não está mais no disco"}, status=410)
    ctype = mimetypes.guess_type(a.name)[0] or "application/octet-stream"
    if a.name.endswith((".v", ".log", ".yml", ".bench", ".bin")):
        ctype = "text/plain; charset=utf-8"
    return FileResponse(open(path, "rb"), as_attachment=True, filename=a.name.split("/")[-1], content_type=ctype)


# ======================================================================= métricas
@api.get("/metrics", tags=["métricas"])
def metrics(request, project: str | None = None, since_hours: float | None = None):
    """
    Métricas de avaliação do pipeline (capítulo de avaliação do TCC):
    tempos por etapa, fila, throughput, taxa de sucesso, reprodutibilidade e
    tempo até detectar regressões.
    """
    qs = Job.objects.select_related("version")
    if project:
        qs = qs.filter(project__name=project)
    if since_hours:
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(hours=since_hours))
    jobs = list(qs)
    done = [j for j in jobs if j.finished_at and j.started_at]

    def stats(values):
        values = [v for v in values if v is not None]
        if not values:
            return None
        values.sort()
        p95 = values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]
        return {"n": len(values), "mean": round(mean(values), 3), "median": round(median(values), 3),
                "min": round(values[0], 3), "max": round(values[-1], 3), "p95": round(p95, 3)}

    steps: dict[str, list[float]] = {}
    for s in JobStep.objects.filter(job__in=done, status=JobStep.Status.OK):
        steps.setdefault(s.name, []).append(s.duration_s)

    throughput = None
    if len(done) >= 2:
        first = min(j.started_at for j in done)
        last = max(j.finished_at for j in done)
        span_h = max((last - first).total_seconds() / 3600, 1e-9)
        throughput = {"jobs": len(done), "window_h": round(span_h, 4), "jobs_per_hour": round(len(done) / span_h, 3),
                      "workers_seen": len({j.worker for j in done if j.worker})}

    # reprodutibilidade: jobs com a mesma entrada (fontes + dft.yaml) devem dar o mesmo resultado.
    # Dois níveis: métricas (cobertura, nº de vetores, área) e conjunto de vetores do gerador principal.
    from .models import CoverageResult

    vec_hash = dict(CoverageResult.objects.filter(job__in=done, primary=True).values_list("job_id", "vectors_sha256"))
    groups: dict[str, list[Job]] = {}
    for j in done:
        if j.version.input_sha256 and j.result_fingerprint:
            groups.setdefault(j.version.input_sha256, []).append(j)
    repeated = {k: v for k, v in groups.items() if len(v) > 1}
    same_metrics = sum(1 for v in repeated.values() if len({j.result_fingerprint for j in v}) == 1)
    same_vectors = sum(1 for v in repeated.values() if len({vec_hash.get(j.id) for j in v}) == 1)

    regressions = [j for j in done if (j.regression or {}).get("regression")]
    by_status: dict[str, int] = {}
    for j in jobs:
        by_status[j.status] = by_status.get(j.status, 0) + 1

    return {
        "jobs": len(jobs),
        "by_status": by_status,
        "queue_s": stats([j.queue_seconds for j in done]),
        "run_s": stats([j.run_seconds for j in done]),
        "total_s": stats([j.total_seconds for j in done]),
        "steps_s": {k: stats(v) for k, v in sorted(steps.items())},
        "throughput": throughput,
        "reproducibility": {
            "inputs_run_more_than_once": len(repeated),
            "runs": sum(len(v) for v in repeated.values()),
            "identical_metrics": same_metrics,
            "identical_vectors": same_vectors,
            "metrics_rate": round(same_metrics / len(repeated), 4) if repeated else None,
            "vectors_rate": round(same_vectors / len(repeated), 4) if repeated else None,
        },
        "regression_detection": {
            "regressions_detected": len(regressions),
            "time_to_detect_s": stats([j.total_seconds for j in regressions]),
        },
    }
