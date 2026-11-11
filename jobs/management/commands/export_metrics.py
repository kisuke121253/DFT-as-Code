"""
Exporta um job por linha em CSV, para as tabelas e gráficos da avaliação.

    python manage.py export_metrics --project mac8 -o resultados.csv
"""

import csv
import sys

from django.core.management.base import BaseCommand

from jobs.models import Job

FIELDS = [
    "job_id", "project", "commit", "branch", "status", "worker", "created_at", "queue_s", "run_s", "total_s",
    "fault_sites", "total_faults", "detected", "coverage_pct", "vectors_generated", "vectors_final",
    "quaigh_coverage_pct", "quaigh_vectors", "scan_length", "area_synth", "area_scan", "area_scan_jtag",
    "regression", "verdict", "fingerprint",
    "t_synth", "t_cut", "t_atpg", "t_nl2bench", "t_quaigh", "t_chain", "t_asm", "t_tap", "t_area",
]


class Command(BaseCommand):
    help = "Exporta métricas dos jobs em CSV"

    def add_arguments(self, parser):
        parser.add_argument("--project")
        parser.add_argument("--status", help="ex.: passed,failed")
        parser.add_argument("-o", "--output", help="arquivo de saída (padrão: stdout)")

    def handle(self, *args, **opts):
        qs = Job.objects.select_related("project", "version").prefetch_related("steps", "coverage").order_by("created_at")
        if opts["project"]:
            qs = qs.filter(project__name=opts["project"])
        if opts["status"]:
            qs = qs.filter(status__in=opts["status"].split(","))
        out = open(opts["output"], "w", newline="") if opts["output"] else sys.stdout
        w = csv.DictWriter(out, fieldnames=FIELDS)
        w.writeheader()
        n = 0
        for j in qs:
            cov = {c.generator: c for c in j.coverage.all()}
            p = next((c for c in cov.values() if c.primary), None)
            q = cov.get("quaigh")
            area = (j.report or {}).get("area") or {}
            row = {
                "job_id": j.id, "project": j.project.name, "commit": j.version.commit_sha, "branch": j.version.branch,
                "status": j.status, "worker": j.worker, "created_at": j.created_at.isoformat(),
                "queue_s": j.queue_seconds, "run_s": j.run_seconds, "total_s": j.total_seconds,
                "fault_sites": p and p.fault_sites, "total_faults": p and p.total_faults, "detected": p and p.detected,
                "coverage_pct": p and p.coverage_pct, "vectors_generated": p and p.vectors_generated,
                "vectors_final": p and p.vectors_final,
                "quaigh_coverage_pct": q and q.coverage_pct, "quaigh_vectors": q and q.vectors_final,
                "scan_length": ((j.report or {}).get("scan") or {}).get("length"),
                "area_synth": (area.get("synth") or {}).get("area_um2"),
                "area_scan": (area.get("scan") or {}).get("area_um2"),
                "area_scan_jtag": (area.get("scan_jtag") or {}).get("area_um2"),
                "regression": (j.regression or {}).get("regression"),
                "verdict": (j.regression or {}).get("summary") or j.error[:200],
                "fingerprint": j.result_fingerprint,
            }
            for s in j.steps.all():
                row[f"t_{s.name}"] = s.duration_s
            w.writerow(row)
            n += 1
        if opts["output"]:
            out.close()
            self.stderr.write(f"{n} jobs exportados para {opts['output']}")
