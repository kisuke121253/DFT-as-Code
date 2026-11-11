"""
Detector de regressão de testabilidade.

Compara as métricas do job atual com as da baseline e decide aprovação.

Verificações que reprovam (severity="error"):
  coverage_drop    queda de cobertura > max_drop_pp pontos percentuais
  coverage_target  cobertura abaixo do alvo absoluto (coverage.target)

Verificações que só alertam (severity="warning"):
  vector_increase  número de vetores cresceu mais que warn_vector_increase_pct %
  area_increase    área final cresceu mais que warn_area_increase_pct %
  new_undetected   falhas que eram detectadas na baseline e deixaram de ser
                   (nomes de células mudam entre sínteses, então é só informativo)

Sem baseline (primeiro job do projeto/branch), só o alvo absoluto é verificado.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Metrics:
    coverage_pct: float
    vector_count: int | None = None
    area_um2: float | None = None
    undetected: list[str] = field(default_factory=list)


@dataclass
class Check:
    name: str
    ok: bool
    severity: str          # error | warning | info
    message: str
    current: float | None = None
    baseline: float | None = None
    delta: float | None = None
    limit: float | None = None


@dataclass
class Verdict:
    passed: bool
    regression: bool       # houve queda de cobertura além do limite
    checks: list[Check]
    baseline_job: str | None = None

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "regression": self.regression,
            "baseline_job": self.baseline_job,
            "checks": [asdict(c) for c in self.checks],
            "summary": self.summary,
        }

    @property
    def summary(self) -> str:
        failed = [c for c in self.checks if not c.ok and c.severity == "error"]
        warns = [c for c in self.checks if not c.ok and c.severity == "warning"]
        if failed:
            return "; ".join(c.message for c in failed)
        if warns:
            return "aprovado com alertas: " + "; ".join(c.message for c in warns)
        return "aprovado"


def _pct_increase(cur: float, base: float) -> float | None:
    if base in (None, 0) or cur is None:
        return None
    return 100.0 * (cur - base) / base


def evaluate(
    current: Metrics,
    baseline: Metrics | None,
    *,
    max_drop_pp: float,
    target: float | None = None,
    warn_vector_increase_pct: float | None = None,
    warn_area_increase_pct: float | None = None,
    baseline_job: str | None = None,
    enabled: bool = True,
) -> Verdict:
    checks: list[Check] = []
    regression = False
    eps = 1e-9

    if target is not None:
        ok = current.coverage_pct + eps >= target
        checks.append(Check(
            "coverage_target", ok, "error",
            f"cobertura {current.coverage_pct:.2f}% {'≥' if ok else 'abaixo do alvo de'} {target:.2f}%",
            current=current.coverage_pct, limit=target,
            delta=round(current.coverage_pct - target, 4),
        ))

    if enabled and baseline is not None:
        drop = baseline.coverage_pct - current.coverage_pct
        ok = drop <= max_drop_pp + eps
        regression = not ok
        if drop > eps:
            msg = f"cobertura caiu {drop:.2f} p.p. ({baseline.coverage_pct:.2f}% → {current.coverage_pct:.2f}%)"
            msg += "" if ok else f", acima do limite de {max_drop_pp:.2f} p.p."
        elif drop < -eps:
            msg = f"cobertura subiu {-drop:.2f} p.p. ({baseline.coverage_pct:.2f}% → {current.coverage_pct:.2f}%)"
        else:
            msg = f"cobertura estável em {current.coverage_pct:.2f}%"
        checks.append(Check(
            "coverage_drop", ok, "error", msg,
            current=current.coverage_pct, baseline=baseline.coverage_pct,
            delta=round(-drop, 4), limit=max_drop_pp,
        ))

        inc = _pct_increase(current.vector_count, baseline.vector_count)
        if inc is not None and warn_vector_increase_pct is not None:
            ok = inc <= warn_vector_increase_pct + eps
            checks.append(Check(
                "vector_increase", ok, "warning",
                f"vetores {baseline.vector_count} → {current.vector_count} ({inc:+.1f}%)",
                current=current.vector_count, baseline=baseline.vector_count,
                delta=round(inc, 2), limit=warn_vector_increase_pct,
            ))

        inc = _pct_increase(current.area_um2, baseline.area_um2)
        if inc is not None and warn_area_increase_pct is not None:
            ok = inc <= warn_area_increase_pct + eps
            checks.append(Check(
                "area_increase", ok, "warning",
                f"área {baseline.area_um2:.0f} → {current.area_um2:.0f} µm² ({inc:+.1f}%)",
                current=current.area_um2, baseline=baseline.area_um2,
                delta=round(inc, 2), limit=warn_area_increase_pct,
            ))

        newly = sorted(set(current.undetected) - set(baseline.undetected))
        if newly:
            shown = ", ".join(newly[:10]) + ("…" if len(newly) > 10 else "")
            checks.append(Check(
                "new_undetected", True, "info",
                f"{len(newly)} falha(s) não detectada(s) que não estavam na baseline: {shown}",
                current=len(current.undetected), baseline=len(baseline.undetected),
            ))
    elif enabled:
        checks.append(Check("coverage_drop", True, "info", "sem baseline: primeiro resultado deste branch"))

    passed = all(c.ok for c in checks if c.severity == "error")
    return Verdict(passed=passed, regression=regression, checks=checks, baseline_job=baseline_job)
