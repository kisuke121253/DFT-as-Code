#!/usr/bin/env python3
"""
Envia um lote de benchmarks (ISCAS'85, ISCAS'89, ITC'99 em Verilog) para o pipeline
e, opcionalmente, espera e imprime uma tabela comparável com a literatura.

Para cada arquivo .v, gera um dft.yaml detectando pela lista de portas do top:
  - top:   último módulo do arquivo que não é 'dff'
  - clock: CK / clk / clock, se existir (sem clock = combinacional)
  - reset: reset / rst / rst_n / RST, se existir (rst_n ⇒ ativo baixo)
  - bypass: VDD e GND, se existirem (alimentação não entra como fault site)

Uso:
    # benchmarks do repositório do Fault: git clone https://github.com/AUCOHL/Fault
    python scripts/submit_benchmarks.py Fault/Benchmarks/ISCAS_89/s{27,298,344,1196}.v --wait
    python scripts/submit_benchmarks.py Fault/Benchmarks/ISCAS_85/*.v --no-jtag --wait --csv iscas85.csv

    # throughput: suba N workers e envie o lote sem esperar
    docker compose up -d --scale worker=4
    python scripts/submit_benchmarks.py ... --wait

Requer o dftctl instalado (pip install ./cli) e DFT_API_URL/DFT_API_TOKEN.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import tempfile
import time
from pathlib import Path

try:
    from dftctl.__main__ import Client, main as dftctl_main
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))
    from dftctl.__main__ import Client, main as dftctl_main  # noqa: E402

CLOCKS = ["CK", "clk", "CLK", "clock"]
RESETS = [("reset", False), ("rst", False), ("RST", False), ("rst_n", True), ("resetn", True), ("reset_n", True)]


def detect(path: Path) -> dict:
    text = re.sub(r"//.*", "", path.read_text(errors="replace"))
    mods = re.findall(r"\bmodule\s+(\w+)\s*\((.*?)\)\s*;", text, re.S)
    mods = [(n, p) for n, p in mods if n.lower() != "dff"]
    if not mods:
        raise ValueError(f"{path}: nenhum módulo encontrado")
    top, ports = mods[-1]
    names = {p.strip().split()[-1] for p in ports.replace("\n", " ").split(",") if p.strip()}
    clock = next((c for c in CLOCKS if c in names), None)
    reset = next(((r, low) for r, low in RESETS if r in names), None)
    return {"top": top, "clock": clock, "reset": reset, "bypass": [p for p in ("VDD", "GND") if p in names]}


def make_yaml(info: dict, src: str, args) -> str:
    lines = ["version: 1", f"project: {args.prefix}{info['top']}", f"top: {info['top']}", f"sources: [{src}]"]
    if info["clock"]:
        lines.append(f"clock: {info['clock']}")
    if info["reset"]:
        lines.append(f"reset: {info['reset'][0]}")
        if info["reset"][1]:
            lines.append("reset_active_low: true")
    if info["bypass"]:
        lines.append(f"bypass: [{', '.join(info['bypass'])}]")
    if args.no_jtag:
        lines.append("jtag: {enabled: false}")
    lines.append(f"atpg: {{generator: prng, compare_with_quaigh: {str(args.quaigh).lower()}, "
                 f"min_coverage: {args.min_coverage}, ceiling: {args.ceiling}}}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--prefix", default="bench-", help="prefixo do nome do projeto (padrão bench-)")
    ap.add_argument("--branch", default="benchmarks")
    ap.add_argument("--no-jtag", action="store_true", help="pula a TAP (mais rápido)")
    ap.add_argument("--quaigh", action="store_true", help="roda também o Quaigh para comparação")
    ap.add_argument("--min-coverage", type=float, default=100)
    ap.add_argument("--ceiling", type=int, default=5000)
    ap.add_argument("--wait", action="store_true", help="espera todos e imprime a tabela")
    ap.add_argument("--csv", help="salva a tabela em CSV")
    ap.add_argument("--dry-run", action="store_true", help="só mostra os dft.yaml gerados")
    ap.add_argument("--url")
    ap.add_argument("--token")
    args = ap.parse_args()

    common = [*(["--url", args.url] if args.url else []), *(["--token", args.token] if args.token else [])]
    submitted: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        for f in args.files:
            info = detect(f)
            d = Path(tmp) / info["top"]
            d.mkdir()
            (d / f.name).write_bytes(f.read_bytes())
            (d / "dft.yaml").write_text(make_yaml(info, f.name, args))
            if args.dry_run:
                print(f"# {f}\n{(d / 'dft.yaml').read_text()}")
                continue
            import contextlib
            import io

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = dftctl_main([*common, "run", "-c", str(d / "dft.yaml"), "--no-wait", "-q",
                                  "--branch", args.branch, "--commit", "", "--message", f"benchmark {f.name}"])
            if rc != 0:
                print(f"✖ {f.name}: falha ao enviar", file=sys.stderr)
                continue
            job_id = out.getvalue().strip().splitlines()[-1]
            submitted.append((info["top"], job_id))
            print(f"→ {info['top']:<10} {job_id}", file=sys.stderr)

    if not args.wait or not submitted:
        return 0

    import os

    cl = Client(args.url or os.environ.get("DFT_API_URL", "http://localhost:8000"),
                args.token or os.environ.get("DFT_API_TOKEN"))
    t0 = time.monotonic()
    pending = dict(submitted)
    rows = {}
    while pending:
        for top, jid in list(pending.items()):
            j = cl.get(f"/jobs/{jid}")
            if j["status"] in {"passed", "failed", "error", "canceled"}:
                rows[top] = cl.get(f"/jobs/{jid}/report")
                del pending[top]
                print(f"   {top}: {j['status']}", file=sys.stderr)
        if pending:
            time.sleep(5)
    wall = time.monotonic() - t0

    header = ["circuito", "status", "fault_sites", "falhas", "detectadas", "cobertura_%", "vetores",
              "quaigh_%", "quaigh_vetores", "scan_len", "area_um2", "tempo_s"]
    table = []
    for top, _ in submitted:
        r = rows[top]
        c, q = r.get("coverage") or {}, (r.get("comparison") or {}).get("quaigh") or {}
        area = r.get("area") or {}
        last_area = list(area.values())[-1]["area_um2"] if area else None
        table.append([top, r["job"]["status"], c.get("fault_sites"), c.get("total_faults"), c.get("detected"),
                      c.get("coverage_pct"), c.get("vectors_final"), q.get("coverage_pct"), q.get("vectors_final"),
                      (r.get("scan") or {}).get("length"), last_area, r["job"].get("run_s")])
    widths = [max(len(str(x)) for x in col) for col in zip(header, *table)]
    for row in [header, *table]:
        print("  ".join(str("" if v is None else v).ljust(w) for v, w in zip(row, widths)))
    print(f"\n{len(table)} jobs em {wall:.0f}s de relógio ({len(table) / wall * 3600:.1f} jobs/h)")
    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            csv.writer(fh).writerows([header, *table])
    return 0


if __name__ == "__main__":
    sys.exit(main())
