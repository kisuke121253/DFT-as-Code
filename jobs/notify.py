"""
Notificações de resultado.

- Webhook (Slack, Discord, Teams, Mattermost... qualquer endpoint que aceite JSON):
  enviado quando o job é reprovado, dá erro ou tem regressão.
- Commit status no GitHub: se DFT_GITHUB_TOKEN estiver definido e o projeto tiver
  repo_url do GitHub, publica pending/success/failure/error no commit, com o
  contexto "dft-as-code/coverage". Isso aparece como check no PR mesmo sem o
  GitHub Actions. O workflow com `dftctl` é a outra forma (ver ci-template/).

Falhas de notificação nunca derrubam o job: só são registradas no log.
"""

from __future__ import annotations

import json
import logging
import urllib.request

from django.conf import settings

log = logging.getLogger(__name__)

GITHUB_CONTEXT = "dft-as-code/coverage"


def _post(url: str, payload: dict, headers: dict | None = None, timeout: int = 10) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "dft-as-code", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def job_url(job) -> str:
    return f"{settings.DFT_PUBLIC_URL.rstrip('/')}/api/v1/jobs/{job.id}"


def github_status(job, state: str, description: str) -> None:
    token = settings.DFT_GITHUB_TOKEN
    repo = job.project.github_repo or _github_repo_from(job.repo_url)
    sha = job.version.commit_sha
    if not (token and repo and sha and len(sha) >= 7):
        return
    url = f"{settings.DFT_GITHUB_API.rstrip('/')}/repos/{repo}/statuses/{sha}"
    try:
        _post(url, {
            "state": state,                      # pending | success | failure | error
            "description": description[:140],
            "context": GITHUB_CONTEXT,
            "target_url": job_url(job),
        }, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    except Exception as e:  # noqa: BLE001
        log.warning("falha ao publicar status no GitHub (%s): %s", repo, e)


def _github_repo_from(url: str) -> str | None:
    from .models import Project

    return Project(repo_url=url or "").github_repo


def webhook(job, text: str) -> None:
    url = job.project.webhook_url or settings.DFT_WEBHOOK_URL
    if not url:
        return
    try:
        _post(url, {
            "text": text,           # Slack/Mattermost
            "content": text,        # Discord
            "job": {
                "id": str(job.id), "project": job.project.name, "status": job.status,
                "commit": job.version.commit_sha, "branch": job.version.branch,
                "coverage_pct": job.coverage_pct, "url": job_url(job),
                "regression": job.regression,
            },
        })
    except Exception as e:  # noqa: BLE001
        log.warning("falha ao enviar webhook: %s", e)


def job_started(job) -> None:
    github_status(job, "pending", "fluxo DFT em execução")


def job_finished(job) -> None:
    ref = job.version.commit_sha[:8] or str(job.id)[:8]
    summary = (job.regression or {}).get("summary", "")
    if job.status == job.Status.PASSED:
        cov = f"{job.coverage_pct:.2f}%" if job.coverage_pct is not None else "?"
        github_status(job, "success", f"cobertura {cov} — {summary}")
        if "alertas" in summary:
            webhook(job, f"⚠️ {job.project.name}@{ref}: {summary}")
    elif job.status == job.Status.FAILED:
        github_status(job, "failure", summary or "reprovado")
        webhook(job, f"❌ {job.project.name}@{ref} reprovado: {summary}\n{job_url(job)}")
    elif job.status == job.Status.ERROR:
        github_status(job, "error", (job.error or "erro no fluxo")[:140])
        webhook(job, f"💥 {job.project.name}@{ref}: erro no fluxo DFT: {job.error[:300]}\n{job_url(job)}")
    elif job.status == job.Status.CANCELED:
        github_status(job, "error", "job cancelado")
