"""Task do Celery executada pelos workers: roda o fluxo DFT de um job."""

from __future__ import annotations

import json
import logging
import socket
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import notify, services, storage
from .dftconfig import DftConfig, DftConfigError, parse_dft_yaml
from .flow.runner import FlowError, FlowRunner, FlowTimeout, Tech, Tools, tool_versions
from .models import CoverageResult, Job, JobStep

log = logging.getLogger(__name__)


class Canceled(Exception):
    pass


def _tools() -> Tools:
    return Tools(fault=settings.DFT_FAULT_BIN, nl2bench=settings.DFT_NL2BENCH_BIN,
                 yosys=settings.DFT_YOSYS_BIN, fault_threads=settings.DFT_FAULT_THREADS)


def _claim(job_id: str, redelivered: bool) -> Job | None:
    """Passa o job de 'queued' para 'running' de forma atômica. None se outro worker já pegou."""
    with transaction.atomic():
        job = Job.objects.select_for_update().select_related("project", "version").filter(pk=job_id).first()
        if job is None:
            log.warning("job %s não existe mais", job_id)
            return None
        allowed = {Job.Status.QUEUED} | ({Job.Status.RUNNING} if redelivered else set())
        if job.status not in allowed:
            log.info("job %s ignorado (status %s)", job_id, job.status)
            return None
        job.status = Job.Status.RUNNING
        job.started_at = timezone.now()
        job.worker = socket.gethostname()
        job.error = ""
        job.save(update_fields=["status", "started_at", "worker", "error"])
    return job


def _prepare_input(job: Job) -> tuple[DftConfig, list[Path]]:
    inp = storage.input_dir(job.id)
    storage.ensure_dirs(job.id)
    if job.source == Job.Source.GIT:
        storage.git_checkout(job.repo_url, job.version.commit_sha, inp)
        if not job.config_yaml:
            cfg_file = inp / job.config_path
            if not storage.is_within(cfg_file, inp) or not cfg_file.is_file():
                raise storage.InputError(f"'{job.config_path}' não encontrado no repositório")
            job.config_yaml = cfg_file.read_text()
    cfg = parse_dft_yaml(job.config_yaml)
    if cfg.project != job.project.name:
        log.info("dft.yaml declara projeto %s, job está em %s; mantendo o do job", cfg.project, job.project.name)
    sources = storage.match_sources(inp, cfg.sources)
    if not sources:
        raise storage.InputError(f"nenhum arquivo casou com sources {cfg.sources}")
    if job.source == Job.Source.GIT:
        v = job.version
        v.input_sha256 = storage.hash_inputs(inp, sources, job.config_yaml)
        v.save(update_fields=["input_sha256"])
    job.config = cfg.model_dump()
    job.save(update_fields=["config", "config_yaml"])
    return cfg, [inp / s for s in sources]


def _make_event_handler(job: Job, order: dict[str, int]):
    def on_event(step: str, event: str, data: dict):
        now = timezone.now()
        st, _ = JobStep.objects.get_or_create(job=job, name=step, defaults={"order": order.get(step, 99)})
        if event == "start":
            # cancelamento cooperativo: verificado antes de cada etapa
            if Job.objects.filter(pk=job.pk, status=Job.Status.CANCELED).exists():
                raise Canceled()
            st.status, st.started_at = JobStep.Status.RUNNING, now
        elif event == "skipped":
            st.status, st.summary = JobStep.Status.SKIPPED, {"reason": data.get("reason", "")}
        else:
            st.status = JobStep.Status.OK if event == "ok" else JobStep.Status.FAILED
            st.finished_at, st.duration_s = now, round(data.get("duration_s", 0), 3)
            st.summary = data.get("summary") or ({"error": data.get("error"), "log_tail": data.get("log_tail")}
                                                 if event == "failed" else {})
            st.command = "\n".join(data.get("commands", []))
            st.return_code = 0 if event == "ok" else 1
        logs = storage.logs_dir(job.id)
        lp = logs / f"{step}.log"
        if step == "area":
            lp = next(iter(sorted(logs.glob("area.*.log"))), lp)
        if lp.exists():
            st.log_path = lp.relative_to(storage.job_dir(job.id)).as_posix()
        st.save()
    return on_event


def _save_results(job: Job, cfg: DftConfig, result: dict) -> None:
    for gen, c in result["coverage"].items():
        CoverageResult.objects.update_or_create(
            job=job, generator=gen,
            defaults=dict(
                primary=(gen == result["primary_generator"]), fault_sites=c["fault_sites"],
                total_faults=c["total_faults"], detected=c["detected"], coverage_pct=c["coverage_pct"],
                vectors_generated=c.get("vectors_generated"), vectors_final=c.get("vectors_final"),
                undetected=c.get("undetected", []), duration_s=c.get("duration_s"),
                vectors_sha256=c.get("vectors_sha256") or "",
            ),
        )
    primary = result["primary"]
    job.coverage_pct = primary["coverage_pct"] if primary else None
    job.vector_count = primary.get("vectors_final") if primary else None
    job.area_um2 = result.get("final_area_um2")
    job.result_fingerprint = result.get("fingerprint", "")
    job.report = {k: result.get(k) for k in ("scan", "jtag", "area", "duration_s", "final_netlist")}


def _write_report(job: Job) -> None:
    report = services.build_report(job)
    path = storage.work_dir(job.id) / "report.json"
    path.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False))


@shared_task(bind=True, name="jobs.run_job", acks_late=True)
def run_job(self, job_id: str) -> str:
    redelivered = bool((getattr(self.request, "delivery_info", None) or {}).get("redelivered"))
    job = _claim(job_id, redelivered)
    if job is None:
        return "skipped"
    notify.job_started(job)
    result: dict | None = None
    try:
        job.tools = tool_versions(_tools())
        job.save(update_fields=["tools"])
        cfg, sources = _prepare_input(job)
        timeout_min = min(cfg.timeout_minutes or settings.DFT_JOB_TIMEOUT_MIN, settings.DFT_JOB_TIMEOUT_MIN)
        runner = FlowRunner(
            config=cfg, sources=sources, work=storage.work_dir(job.id), logs=storage.logs_dir(job.id),
            tech=Tech.load(settings.DFT_TECH_DIR, cfg.tech), tools=_tools(), timeout_s=timeout_min * 60,
        )
        plan = runner.plan()
        order = {p.name: i for i, p in enumerate(plan)}
        for name, i in order.items():
            JobStep.objects.update_or_create(job=job, name=name, defaults={"order": i, "status": JobStep.Status.PENDING})
        runner.on_event = _make_event_handler(job, order)
        result = runner.run()

        _save_results(job, cfg, result)
        job.regression = services.judge(job, cfg)
        job.status = Job.Status.PASSED if job.regression["passed"] else Job.Status.FAILED
    except Canceled:
        job.status = Job.Status.CANCELED
        job.error = "cancelado pelo usuário"
    except (DftConfigError, storage.InputError) as e:
        job.status, job.error = Job.Status.ERROR, f"entrada inválida: {e}"
    except FlowTimeout as e:
        job.status, job.error = Job.Status.ERROR, f"timeout na etapa {e.step}: {e}"
    except FlowError as e:
        job.status = Job.Status.ERROR
        job.error = f"etapa {e.step}: {e}" + (f"\n--- fim do log ---\n{e.log_tail}" if e.log_tail else "")
    except Exception as e:  # noqa: BLE001 — qualquer falha inesperada vira status de erro, não job preso
        log.exception("erro inesperado no job %s", job_id)
        job.status, job.error = Job.Status.ERROR, f"erro interno: {type(e).__name__}: {e}"
    finally:
        # o job pode ter sido cancelado durante a última etapa
        if Job.objects.filter(pk=job.pk, status=Job.Status.CANCELED).exists() and job.status != Job.Status.CANCELED:
            job.status, job.error = Job.Status.CANCELED, "cancelado pelo usuário"
        job.finished_at = timezone.now()
        JobStep.objects.filter(job=job, status__in=[JobStep.Status.PENDING, JobStep.Status.RUNNING]).update(
            status=JobStep.Status.SKIPPED)
        job.save()
        try:
            services.collect_artifacts(job)
            _write_report(job)
            services.collect_artifacts(job)  # inclui o report.json
        except Exception:  # noqa: BLE001
            log.exception("falha ao registrar artefatos do job %s", job_id)
        notify.job_finished(job)
    return job.status
