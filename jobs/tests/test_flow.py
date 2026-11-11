"""
Testes de integração com as ferramentas reais (Fault 0.9.4, Yosys, Icarus).
São pulados se `fault`, `yosys` ou `iverilog` não estiverem no PATH.
Os números esperados são os da etapa 1 (Ubuntu 24.04, Yosys 0.33).
"""

import shutil
from pathlib import Path

import pytest
from django.conf import settings

from jobs.dftconfig import parse_dft_yaml
from jobs.flow.runner import FlowRunner, Tech, Tools

ROOT = Path(settings.BASE_DIR)
pytestmark = [
    pytest.mark.tools,
    pytest.mark.skipif(not all(shutil.which(t) for t in ("fault", "yosys", "iverilog")),
                       reason="ferramentas EDA não instaladas"),
]

C17 = """module c17 (N1,N2,N3,N6,N7,N22,N23);
input N1,N2,N3,N6,N7;
output N22,N23;
wire N10,N11,N16,N19;
nand NAND2_1 (N10, N1, N3);
nand NAND2_2 (N11, N3, N6);
nand NAND2_3 (N16, N2, N11);
nand NAND2_4 (N19, N11, N7);
nand NAND2_5 (N22, N10, N16);
nand NAND2_6 (N23, N16, N19);
endmodule
"""


def run(tmp_path, yaml_text, sources):
    cfg = parse_dft_yaml(yaml_text)
    events = []
    r = FlowRunner(cfg, sources, tmp_path / "work", tmp_path / "logs", Tech.load(ROOT / "tech", cfg.tech),
                   Tools(fault_threads=1), 1800, on_event=lambda s, e, d: events.append((s, e)))
    return r.run(), events


def test_s27_fluxo_completo(tmp_path):
    res, events = run(tmp_path, (ROOT / "examples/s27/dft.yaml").read_text(), [ROOT / "examples/s27/s27.v"])
    p = res["primary"]
    assert (p["fault_sites"], p["total_faults"], p["detected"]) == (45, 90, 78)
    assert p["coverage_pct"] == pytest.approx(86.6667, abs=1e-3)
    # a compactação do PRNG do Fault 0.9.4 não é determinística: 6 ou 7 vetores
    assert p["vectors_final"] in (6, 7)
    if shutil.which("quaigh"):
        assert res["coverage"]["quaigh"]["coverage_pct"] == pytest.approx(86.6667, abs=1e-3)
    else:
        assert ("quaigh", "skipped") in events and "quaigh" not in res["coverage"]
    assert res["scan"] == {"verified": True, "internal_ffs": 3, "boundary_cells": 5, "length": 8}
    assert res["jtag"] == {"tap_verified": True, "vectors_verified": True}
    assert {k: v["area_um2"] for k, v in res["area"].items()} == {"synth": 3264.0, "scan": 10152.0, "scan_jtag": 57880.0}
    assert ("tap", "ok") in events


def test_c17_combinacional(tmp_path):
    src = tmp_path / "c17.v"
    src.write_text(C17)
    res, events = run(tmp_path, "top: c17\nsources: [c17.v]\n", [src])
    assert res["primary"]["coverage_pct"] == 100.0
    assert ("chain", "skipped") in events and ("tap", "skipped") in events


def test_sequencial_sem_reset_usa_reset_ficticio(tmp_path):
    """O `fault chain` 0.9.4 quebra sem reset (ISCAS'89 s298, s344...); o runner injeta um."""
    src = tmp_path / "tgl.v"
    src.write_text("module tgl(input CK, input a, input b, output reg q, output reg r);\n"
                   "  always @(posedge CK) begin q <= q ^ a; r <= q & b; end\nendmodule\n")
    res, events = run(tmp_path, "top: tgl\nsources: [tgl.v]\nclock: CK\natpg: {min_coverage: 100, ceiling: 500}\n", [src])
    assert ("chain", "ok") in events and ("tap", "ok") in events
    assert res["scan"]["internal_ffs"] == 2
    assert res["primary"]["coverage_pct"] == 100.0


def test_quaigh_como_gerador_principal_sem_executavel(tmp_path, monkeypatch):
    from jobs.flow.runner import FlowError

    monkeypatch.setattr(Tools, "has_quaigh", property(lambda self: False))
    with pytest.raises(FlowError, match="não está no PATH"):
        run(tmp_path, "top: c17\nsources: [c17.v]\natpg: {generator: quaigh}\n", [tmp_path / "c17.v"])
