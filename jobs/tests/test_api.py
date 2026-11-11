"""
Testes da API + fila + worker com um FlowRunner falso (não precisa das ferramentas EDA).
O fluxo real é coberto em test_flow.py.
"""

import io
import json
import tarfile

import pytest
from django.test import Client

from jobs import tasks
from jobs.flow.runner import FlowError, StepPlan
from jobs.models import Job

pytestmark = pytest.mark.django_db(transaction=True)

YAML = """
version: 1
project: demo
top: s27
sources: [rtl/*.v]
clock: CK
reset: reset
coverage: {target: 80}
regression: {max_drop_pp: 0.5}
"""

STEPS = ["synth", "cut", "atpg", "chain", "asm", "tap", "area"]


class FakeRunner:
    """Imita o FlowRunner: emite eventos, grava arquivos e devolve métricas configuráveis."""
    coverage = 86.6667
    fail_at: str | None = None

    def __init__(self, config, sources, work, logs, tech, tools, timeout_s, on_event=None):
        self.config, self.work, self.logs, self.on_event = config, work, logs, on_event

    def plan(self):
        return [StepPlan(n, lambda: {}) for n in STEPS]

    def run(self):
        for n in STEPS:
            self.on_event(n, "start", {})
            (self.logs / f"{n}.log").write_text(f"$ fault {n}\nok\n")
            if n == self.fail_at:
                self.on_event(n, "failed", {"duration_s": 0.1, "error": "boom", "log_tail": "Illegal instruction"})
                raise FlowError(f"'{n}' terminou com código 132", n, "Illegal instruction")
            self.on_event(n, "ok", {"duration_s": 0.5, "summary": {}})
        (self.work / "s27.jtag.v").write_text("module s27(); endmodule")
        (self.work / "s27.tv.json").write_text("{}")
        (self.work / "parser.out").write_text("lixo")
        detected = round(self.coverage * 0.9)
        cov = {"fault_sites": 45, "total_faults": 90, "detected": detected, "coverage_pct": self.coverage,
               "undetected": ["_18_.Y/sa0"], "vectors_generated": 74, "vectors_final": 6, "duration_s": 2.0,
               "vectors_sha256": "abc"}
        return {
            "coverage": {"prng": cov}, "primary_generator": "prng", "primary": cov,
            "area": {"synth": {"netlist": "s27.nl.v", "area_um2": 3264.0, "cells": 15},
                     "scan_jtag": {"netlist": "s27.jtag.v", "area_um2": 57880.0, "cells": 241}},
            "final_area_um2": 57880.0, "scan": {"verified": True, "length": 8}, "jtag": {"tap_verified": True},
            "fingerprint": f"fp-{self.coverage}", "duration_s": 3.5, "final_netlist": "s27.jtag.v",
        }


@pytest.fixture(autouse=True)
def fake_flow(monkeypatch):
    FakeRunner.coverage, FakeRunner.fail_at = 86.6667, None
    monkeypatch.setattr(tasks, "FlowRunner", FakeRunner)
    monkeypatch.setattr(tasks, "tool_versions", lambda tools: {"fault": "0.9.4", "yosys": "fake"})
    return FakeRunner


def rtl_archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"module s27(); endmodule"
        info = tarfile.TarInfo("rtl/s27.v")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def submit(c: Client, commit="a" * 40, branch="main", yaml=YAML, **extra):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return c.post("/api/v1/jobs", {
        "dft_yaml": yaml, "commit_sha": commit, "branch": branch, "message": "msg",
        "archive": SimpleUploadedFile("rtl.tar.gz", rtl_archive()), **extra,
    })


def test_job_aprovado_ponta_a_ponta():
    c = Client()
    r = submit(c)
    assert r.status_code == 202, r.content
    job_id = r.json()["id"]
    j = c.get(f"/api/v1/jobs/{job_id}").json()
    assert j["status"] == "passed"
    assert j["coverage_pct"] == pytest.approx(86.6667)
    assert [s["name"] for s in j["steps"]] == STEPS
    assert all(s["status"] == "ok" for s in j["steps"])

    rep = c.get(f"/api/v1/jobs/{job_id}/report").json()
    assert rep["coverage"]["detected"] == 78 and rep["coverage"]["total_faults"] == 90
    assert rep["regression"]["passed"] and rep["regression"]["baseline_job"] is None
    names = {a["name"] for a in rep["artifacts"]}
    assert {"s27.jtag.v", "s27.tv.json", "report.json", "log/atpg.log"} <= names
    assert "parser.out" not in names

    dl = c.get(f"/api/v1/jobs/{job_id}/artifacts/s27.jtag.v")
    assert dl.status_code == 200 and b"module s27" in b"".join(dl.streaming_content)
    log = c.get(f"/api/v1/jobs/{job_id}/logs/atpg")
    assert b"fault atpg" in b"".join(log.streaming_content)


def test_regressao_reprova_e_baseline_e_o_ultimo_aprovado(fake_flow):
    c = Client()
    first = submit(c, commit="1" * 40).json()["id"]
    fake_flow.coverage = 84.0          # queda de 2,67 p.p.
    second = submit(c, commit="2" * 40).json()["id"]
    j2 = c.get(f"/api/v1/jobs/{second}").json()
    assert j2["status"] == "failed" and j2["regression"]["regression"]
    assert j2["regression"]["baseline_job"] == first

    # a próxima versão compara com o último APROVADO (first), não com o reprovado
    fake_flow.coverage = 86.5
    third = submit(c, commit="3" * 40).json()["id"]
    j3 = c.get(f"/api/v1/jobs/{third}").json()
    assert j3["status"] == "passed"
    assert j3["regression"]["baseline_job"] == first

    hist = c.get("/api/v1/projects/demo/history").json()["items"]
    assert [h["status"] for h in hist] == ["passed", "failed", "passed"]


def test_pr_compara_com_branch_padrao(fake_flow):
    c = Client()
    main_job = submit(c, commit="1" * 40, branch="main").json()["id"]
    fake_flow.coverage = 80.5
    pr = submit(c, commit="2" * 40, branch="feature/x").json()["id"]
    j = c.get(f"/api/v1/jobs/{pr}").json()
    assert j["regression"]["baseline_job"] == main_job
    assert j["status"] == "failed"


def test_erro_do_fluxo_vira_status_error(fake_flow):
    fake_flow.fail_at = "atpg"
    c = Client()
    job_id = submit(c).json()["id"]
    j = c.get(f"/api/v1/jobs/{job_id}").json()
    assert j["status"] == "error"
    assert "atpg" in j["error"] and "Illegal instruction" in j["error"]
    st = {s["name"]: s["status"] for s in j["steps"]}
    assert st["atpg"] == "failed" and st["chain"] == "skipped"


def test_validacao_do_yaml():
    c = Client()
    r = submit(c, yaml="top: s27\nscan: {chains: 3}\nclock: CK\n")
    assert r.status_code == 422
    assert any("scan.chains" in e for e in r.json()["errors"])
    v = c.post("/api/v1/validate", json.dumps({"dft_yaml": YAML}), content_type="application/json").json()
    assert v["valid"] and v["config"]["atpg"]["generator"] == "prng"


def test_sources_sem_arquivos_da_400():
    c = Client()
    r = submit(c, yaml=YAML.replace("rtl/*.v", "src/*.sv"))
    assert r.status_code == 400 and "nenhum arquivo casou" in r.json()["detail"]
    assert Job.objects.count() == 0


def test_token_obrigatorio_quando_configurado(settings):
    settings.DFT_API_TOKENS = ["segredo"]
    c = Client()
    assert c.get("/api/v1/jobs").status_code == 401
    assert c.get("/api/v1/jobs", HTTP_AUTHORIZATION="Bearer errado").status_code == 401
    assert c.get("/api/v1/jobs", HTTP_AUTHORIZATION="Bearer segredo").status_code == 200
    assert c.get("/api/v1/health").status_code == 200   # health é público


def test_retry_e_metricas_de_reprodutibilidade():
    c = Client()
    first = submit(c).json()["id"]
    second = c.post(f"/api/v1/jobs/{first}/retry").json()["id"]
    assert c.get(f"/api/v1/jobs/{second}").json()["status"] == "passed"
    m = c.get("/api/v1/metrics").json()
    assert m["jobs"] == 2 and m["by_status"] == {"passed": 2}
    assert m["reproducibility"]["inputs_run_more_than_once"] == 1
    assert m["reproducibility"]["metrics_rate"] == 1.0
    assert m["steps_s"]["atpg"]["n"] == 2


def test_cancelar_job_na_fila(monkeypatch):
    from jobs import services

    monkeypatch.setattr(services, "enqueue", lambda job: None)   # fica na fila
    c = Client()
    job_id = submit(c).json()["id"]
    assert c.get(f"/api/v1/jobs/{job_id}").json()["status"] == "queued"
    r = c.post(f"/api/v1/jobs/{job_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "canceled"
    # se a mensagem chegar ao worker depois, ele ignora
    assert tasks.run_job.apply(args=[job_id]).get() == "skipped"
    assert c.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 409


def test_export_metrics_csv(tmp_path):
    from django.core.management import call_command

    submit(Client())
    out = tmp_path / "m.csv"
    call_command("export_metrics", "-o", str(out))
    lines = out.read_text().splitlines()
    assert lines[0].startswith("job_id,project") and len(lines) == 2
    assert ",86.6667," in lines[1]


def test_status_no_github_e_webhook(settings, monkeypatch, fake_flow):
    from jobs import notify

    calls = []
    monkeypatch.setattr(notify, "_post", lambda url, payload, headers=None, timeout=10: calls.append((url, payload)))
    settings.DFT_GITHUB_TOKEN = "ghp_teste"
    settings.DFT_WEBHOOK_URL = "https://hooks.example/x"
    c = Client()
    sha = "f" * 40
    submit(c, commit=sha, repo_url="https://github.com/joao/tcc-dft.git")
    fake_flow.coverage = 50.0
    submit(c, commit="e" * 40, repo_url="https://github.com/joao/tcc-dft")

    gh = [(u, p) for u, p in calls if "api.github.com" in u]
    assert gh[0][0] == f"https://api.github.com/repos/joao/tcc-dft/statuses/{sha}"
    assert [p["state"] for _, p in gh] == ["pending", "success", "pending", "failure"]
    assert all(p["context"] == "dft-as-code/coverage" for _, p in gh)
    hooks = [p for u, p in calls if u.startswith("https://hooks.example")]
    assert len(hooks) == 1 and "reprovado" in hooks[0]["text"]
