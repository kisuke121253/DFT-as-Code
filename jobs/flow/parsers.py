"""
Parsers das saídas do Fault 0.9.4 e do Yosys.

Formato do *.coverage.yml (fault atpg --output-coverage-metadata):

    ratio: 0.8666...
    faultPoints: [lista de fault sites]
    sa0Covered / sa1Covered: sites com stuck-at-0 / stuck-at-1 detectado
    sa0Uncovered / sa1Uncovered: não detectados

Regras de contagem (conferidas contra o 'ratio' do próprio Fault no s27):
    total de falhas = 2 * len(faultPoints)
    detectadas      = len(sa0Covered) + len(sa1Covered)
Nomes escapados como '\\_21_.d' aparecem nas listas de não cobertas, mas são
duplicatas do nome do flip-flop cortado e não entram na contagem; por isso são
filtrados da lista de falhas não detectadas.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml


@dataclass
class Coverage:
    fault_sites: int
    total_faults: int
    detected: int
    coverage_pct: float
    ratio_reported: float | None
    undetected: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def parse_coverage_yml(path: Path) -> Coverage:
    data = yaml.safe_load(Path(path).read_text()) or {}
    points = data.get("faultPoints") or []
    sa0c = data.get("sa0Covered") or []
    sa1c = data.get("sa1Covered") or []
    total = 2 * len(points)
    detected = len(sa0c) + len(sa1c)
    undetected = [f"{p}/sa0" for p in (data.get("sa0Uncovered") or []) if not str(p).startswith("\\")]
    undetected += [f"{p}/sa1" for p in (data.get("sa1Uncovered") or []) if not str(p).startswith("\\")]
    ratio = data.get("ratio")
    pct = round(100.0 * detected / total, 4) if total else 0.0
    return Coverage(
        fault_sites=len(points),
        total_faults=total,
        detected=detected,
        coverage_pct=pct,
        ratio_reported=float(ratio) if ratio is not None else None,
        undetected=sorted(undetected),
    )


_RE_SITES = re.compile(r"Found (\d+) fault sites in (\d+) gates and (\d+) ports")
_RE_TV = re.compile(r"Initial TV Count:\s*(\d+)\.\s*Compacted TV Count:\s*(\d+)")
_RE_COV = re.compile(r"Simulations concluded: Coverage ([\d.]+)%")
_RE_ELAPSED = re.compile(r"Time elapsed:\s*([\d.]+)s")


def parse_atpg_log(text: str) -> dict:
    out: dict = {}
    if m := _RE_SITES.search(text):
        out.update(fault_sites=int(m[1]), gates=int(m[2]), ports=int(m[3]))
    if m := _RE_TV.search(text):
        out.update(vectors_generated=int(m[1]), vectors_final=int(m[2]))
    if cov := _RE_COV.findall(text):
        out["coverage_pct_log"] = float(cov[-1])
    if m := _RE_ELAPSED.search(text):
        out["simulation_s"] = float(m[1])
    out["hit_ceiling"] = "Hit ceiling" in text
    return out


_RE_INTERNAL = re.compile(r"Internal scan chain successfully constructed\. Length:\s*(\d+)")
_RE_BOUNDARY = re.compile(r"Boundary scan cells successfully chained\. Length:\s*(\d+)")
_RE_TOTAL = re.compile(r"Total scan-chain length:\s*(\d+)")


def parse_chain_log(text: str) -> dict:
    out: dict = {"verified": "Scan chain verified successfully" in text}
    if m := _RE_INTERNAL.search(text):
        out["internal_ffs"] = int(m[1])
    if m := _RE_BOUNDARY.search(text):
        out["boundary_cells"] = int(m[1])
    if m := _RE_TOTAL.search(text):
        out["length"] = int(m[1])
    return out


def parse_tap_log(text: str) -> dict:
    return {
        "tap_verified": "Tap port verified successfully" in text,
        "vectors_verified": "Test vectors verified successfully" in text,
    }


_RE_AREA = re.compile(r"Chip area for (?:module|top module) '\\?([^']+)':\s*([\d.]+)")
_RE_CELLS = re.compile(r"Number of cells:\s*(\d+)")


def parse_yosys_stat(text: str, top: str | None = None) -> dict:
    """Extrai área e número de células da saída do 'stat -liberty' do Yosys."""
    areas = {m[1]: float(m[2]) for m in _RE_AREA.finditer(text)}
    if not areas:
        return {}
    area = areas.get(top) if top and top in areas else list(areas.values())[-1]
    cells = _RE_CELLS.findall(text)
    return {"area_um2": area, "cells": int(cells[-1]) if cells else None}


_RE_BIN_META = re.compile(r"FAULT METADATA: '(\{.*?\})' END FAULT METADATA")


def parse_bin_metadata(path: Path) -> dict:
    """Metadados do .tv.bin gerado pelo fault asm: {'count': N, 'length': L}."""
    import json

    head = Path(path).read_text(errors="replace")[:2000]
    if m := _RE_BIN_META.search(head):
        try:
            return json.loads(m[1])
        except ValueError:
            return {}
    return {}


def count_vectors_json(path: Path) -> int | None:
    """Número de vetores em um .tv.json do Fault."""
    import json

    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    tvs = data.get("coverageList") if isinstance(data, dict) else None
    return len(tvs) if isinstance(tvs, list) else None
