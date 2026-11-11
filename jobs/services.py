"""Regras de negócio usadas pela API e pelo worker."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from . import storage
from .dftconfig import DftConfig, parse_dft_yaml
from .models import Artifact, Job, Project, Version
from .regression import Metrics, evaluate

# o que vira artefato para download (padrões casados contra o nome do arquivo)
ARTIFACT_KINDS = [
    ("*.jtag.v", "netlist"), ("*.chained.v", "netlist"), ("*.cut.v", "netlist"), ("*.nl.v", "netlist"),
    ("*.tv.json", "vectors"), ("*.tv.bin", "vectors"), ("*.au.bin", "golden"), ("*.tv.svf", "vectors"),
    ("*.coverage.yml", "coverage"), ("*.bench", "bench"), ("report.json", "report"),
]
# arquivos auxiliares que o Fault deixa na pasta de trabalho e não interessam
ARTIFACT_IGNORE = ["parser.out", "parsetab.py", "*.raw_tv.json", "*.v+attrs", "*.tb.sv", "*.tb.sv.log"]


# ------------------------------------------------------------------ criação de jobs
def get_or_create_project(name: str, repo_url: str = "", default_branch: str | None = None) -> Project:
    project, created = Project.objects.get_or_create(
        name=name, defaults={"repo_url": repo_url or "", "default_branch": default_branch or "main"}
    )
    changed = False
    if repo_url and not project.repo_url:
        project.repo_url, changed = repo_url, True
    if default_branch and created is False and project.default_branch != default_branch and not project.jobs.exists():
        project.default_branch, changed = default_branch, True
    if changed:
        project.save()
    return project


def get_or_create_version(project: Project, *, commit_sha: str, branch: str, message: str,
                          author: str, input_sha256: str) -> Version:
    if commit_sha:
        v, _ = Version.objects.get_or_create(
            project=project, commit_sha=commit_sha,
            defaults=dict(branch=branch, message=message, author=author, input_sha256=input_sha256),
        )
    else:
        v, _ = Version.objects.get_or_create(
            project=project, commit_sha="", input_sha256=input_sha256,
            defaults=dict(branch=branch, message=message, author=author),
        )
    fields = []
    for f, val in (("branch", branch), ("message", message), ("author", author), ("input_sha256", input_sha256)):
        if val and getattr(v, f) != val:
            setattr(v, f, val)
            fields.append(f)
    if fields:
        v.save(update_fields=fields)
    return v


def enqueue(job: Job) -> None:
    from .tasks import run_job

    def _send():
        res = run_job.delay(str(job.id))
        Job.objects.filter(pk=job.pk).update(celery_task_id=res.id or "")

    transaction.on_commit(_send)


def create_upload_job(*, config_yaml: str, files: list[tuple[str, bytes]], archives: list[tuple[str, bytes]],
                      commit_sha: str = "", branch: str = "", message: str = "", author: str = "",
                      repo_url: str = "") -> Job:
    """Cria um job a partir de arquivos enviados. Levanta DftConfigError/InputError."""
    cfg = parse_dft_yaml(config_yaml)
    with transaction.atomic():
        project = get_or_create_project(cfg.project, repo_url)
        job = Job(project=project, source=Job.Source.UPLOAD, repo_url=repo_url,
                  config_yaml=config_yaml, config=cfg.model_dump())
        inp = storage.input_dir(job.id)
        storage.ensure_dirs(job.id)
        try:
            for name, data in archives:
                storage.extract_archive(data, name, inp)
            for name, data in files:
                storage.save_file(inp, name, data)
            sources = storage.match_sources(inp, cfg.sources)
            if not sources:
                raise storage.InputError(
                    f"nenhum arquivo casou com sources {cfg.sources}; recebidos: {storage.list_files(inp)[:20]}"
                )
            (inp / ".dft.yaml").write_text(config_yaml)
            digest = storage.hash_inputs(inp, sources, config_yaml)
        except Exception:
            import shutil

            shutil.rmtree(storage.job_dir(job.id), ignore_errors=True)
            raise
        job.version = get_or_create_version(project, commit_sha=commit_sha, branch=branch or project.default_branch,
                                            message=message, author=author, input_sha256=digest)
        job.save()
        enqueue(job)
    return job


def create_git_job(*, project_name: str | None, repo_url: str, commit_sha: str, branch: str = "",
                   message: str = "", author: str = "", config_yaml: str = "",
                   config_path: str = "dft.yaml") -> Job:
    """Cria um job que o worker vai clonar. O dft.yaml pode vir no corpo ou do repositório."""
    cfg: DftConfig | None = parse_dft_yaml(config_yaml) if config_yaml else None
    name = (cfg.project if cfg else None) or project_name
    if not name:
        raise storage.InputError("informe 'project' ou envie o dft.yaml no corpo da requisição")
    if config_path.startswith("/") or ".." in config_path.split("/"):
        raise storage.InputError("config_path inválido")
    with transaction.atomic():
        project = get_or_create_project(name, repo_url)
        version = get_or_create_version(project, commit_sha=commit_sha, branch=branch or project.default_branch,
                                        message=message, author=author, input_sha256="")
        job = Job.objects.create(
            project=project, version=version, source=Job.Source.GIT, repo_url=repo_url,
            config_yaml=config_yaml, config=cfg.model_dump() if cfg else {}, config_path=config_path,
        )
        storage.ensure_dirs(job.id)
        enqueue(job)
    return job


# ------------------------------------------------------------------ regressão
def find_baseline(job: Job, cfg: DftConfig) -> Job | None:
    statuses = [Job.Status.PASSED] if cfg.regression.baseline == "last_passed" else [Job.Status.PASSED, Job.Status.FAILED]
    qs = (Job.objects.filter(project=job.project, status__in=statuses, coverage_pct__isnull=False,
                             created_at__lt=job.created_at)
          .exclude(pk=job.pk).select_related("version").order_by("-created_at"))
    branches = [cfg.regression.baseline_branch] if cfg.regression.baseline_branch else \
        [b for b in dict.fromkeys([job.version.branch, job.project.default_branch]) if b]
    for b in branches:
        found = qs.filter(version__branch=b).first()
        if found:
            return found
    return None


def judge(job: Job, cfg: DftConfig) -> dict:
    baseline = find_baseline(job, cfg) if cfg.regression.enabled else None
    primary = job.coverage.filter(primary=True).first()

    def metrics(j: Job, cov) -> Metrics:
        return Metrics(coverage_pct=j.coverage_pct, vector_count=j.vector_count, area_um2=j.area_um2,
                       undetected=list(cov.undetected) if cov else [])

    base_metrics = metrics(baseline, baseline.coverage.filter(primary=True).first()) if baseline else None
    verdict = evaluate(
        metrics(job, primary), base_metrics,
        max_drop_pp=cfg.regression.max_drop_pp if cfg.regression.max_drop_pp is not None
        else settings.DFT_DEFAULT_MAX_DROP_PP,
        target=cfg.coverage.target,
        warn_vector_increase_pct=cfg.regression.warn_vector_increase_pct,
        warn_area_increase_pct=cfg.regression.warn_area_increase_pct,
        baseline_job=str(baseline.id) if baseline else None,
        enabled=cfg.regression.enabled,
    )
    job.baseline = baseline
    return verdict.to_dict()


# ------------------------------------------------------------------ artefatos e relatório
def collect_artifacts(job: Job) -> None:
    root = storage.job_dir(job.id)
    work, logs = storage.work_dir(job.id), storage.logs_dir(job.id)
    found: list[tuple[Path, str]] = []
    for p in sorted(work.iterdir()) if work.exists() else []:
        if not p.is_file() or storage.matches_any(p.name, ARTIFACT_IGNORE):
            continue
        kind = next((k for pat, k in ARTIFACT_KINDS if storage.matches_any(p.name, [pat])), None)
        if kind:
            found.append((p, kind))
    for p in sorted(logs.glob("*.log")) if logs.exists() else []:
        found.append((p, "log"))
    for p, kind in found:
        name = p.name if kind != "log" else f"log/{p.name}"
        Artifact.objects.update_or_create(
            job=job, name=name,
            defaults=dict(kind=kind, path=p.relative_to(root).as_posix(), size=p.stat().st_size,
                          sha256=storage.sha256_file(p)),
        )


def artifact_url(job: Job, name: str) -> str:
    return f"/api/v1/jobs/{job.id}/artifacts/{name}"


def build_report(job: Job) -> dict:
    """Relatório JSON do job (é o que GET /jobs/{id}/report devolve)."""
    cov = {c.generator: c for c in job.coverage.all()}
    primary = next((c for c in cov.values() if c.primary), None)

    def cov_dict(c):
        return {
            "generator": c.generator, "fault_sites": c.fault_sites, "total_faults": c.total_faults,
            "detected": c.detected, "undetected_count": c.total_faults - c.detected,
            "coverage_pct": c.coverage_pct, "vectors_generated": c.vectors_generated,
            "vectors_final": c.vectors_final, "duration_s": c.duration_s, "vectors_sha256": c.vectors_sha256,
            "undetected": c.undetected,
        }

    r = job.report or {}
    return {
        "job": {
            "id": str(job.id), "status": job.status, "project": job.project.name, "source": job.source,
            "commit": job.version.commit_sha, "branch": job.version.branch, "message": job.version.message,
            "input_sha256": job.version.input_sha256,
            "created_at": job.created_at, "started_at": job.started_at, "finished_at": job.finished_at,
            "queue_s": job.queue_seconds, "run_s": job.run_seconds, "total_s": job.total_seconds,
            "worker": job.worker, "error": job.error or None,
        },
        "coverage": cov_dict(primary) if primary else None,
        "comparison": {g: cov_dict(c) for g, c in cov.items() if not c.primary},
        "scan": r.get("scan"),
        "jtag": r.get("jtag"),
        "area": r.get("area", {}),
        "regression": job.regression or None,
        "steps": [
            {"name": s.name, "status": s.status, "duration_s": s.duration_s, "summary": s.summary,
             "log": artifact_url(job, f"log/{Path(s.log_path).name}") if s.log_path else None}
            for s in job.steps.all()
        ],
        "tools": job.tools,
        "config": job.config,
        "fingerprint": job.result_fingerprint or None,
        "artifacts": [
            {"name": a.name, "kind": a.kind, "size": a.size, "sha256": a.sha256, "url": artifact_url(job, a.name)}
            for a in job.artifacts.all()
        ],
    }


def history(project: Project, branch: str | None = None, limit: int = 100) -> list[dict]:
    qs = (Job.objects.filter(project=project).filter(~Q(status__in=[Job.Status.QUEUED, Job.Status.RUNNING]))
          .select_related("version").order_by("-created_at"))
    if branch:
        qs = qs.filter(version__branch=branch)
    out = []
    for j in qs[:limit]:
        out.append({
            "job_id": str(j.id), "status": j.status, "commit": j.version.commit_sha, "branch": j.version.branch,
            "message": j.version.message.splitlines()[0] if j.version.message else "",
            "created_at": j.created_at, "coverage_pct": j.coverage_pct, "vector_count": j.vector_count,
            "area_um2": j.area_um2, "run_s": j.run_seconds, "regression": bool((j.regression or {}).get("regression")),
            "summary": (j.regression or {}).get("summary") or (j.error[:200] if j.error else ""),
        })
    return out
