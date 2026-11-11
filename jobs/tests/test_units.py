"""Testes de unidade: dft.yaml, parsers, detector de regressão, armazenamento."""

import io
import tarfile
from pathlib import Path

import pytest

from jobs import storage
from jobs.dftconfig import DftConfigError, parse_dft_yaml
from jobs.flow import parsers
from jobs.flow.runner import DUMMY_CLOCK, fingerprint, inject_dummy_port, vectors_hash
from jobs.regression import Metrics, evaluate

FIX = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------------ dft.yaml
def test_config_minimo_preenche_padroes():
    c = parse_dft_yaml("top: mac8\nclock: clk\nreset: rst_n\nreset_active_low: true\n")
    assert c.project == "mac8"
    assert c.sources == ["**/*.v"]
    assert c.scan.enabled and c.jtag.enabled and c.scan.chains == 1
    assert c.atpg.generator == "prng" and c.atpg.seed == "DEADCAFEDEADF00D"
    assert c.regression.baseline == "last_passed"
    assert not c.is_combinational


def test_config_combinacional_desliga_scan_e_jtag():
    c = parse_dft_yaml("top: c17\njtag: {enabled: true}\n")
    assert c.is_combinational
    assert not c.scan.enabled and not c.jtag.enabled


@pytest.mark.parametrize("text,trecho", [
    ("top: x\nclock: clk\nscan: {chains: 2}\n", "1 scan chain"),
    ("top: 9abc\n", "identificador Verilog"),
    ("top: x\nsources: ['../segredo.v']\n", "'..'"),
    ("top: x\nclock: c\nscan: {enabled: false}\n", "jtag.enabled exige scan"),
    ("top: x\natpg: {min_coverage: 0}\n", "atpg.min_coverage"),
    ("top: x\natpg: {seed: '0xZZ'}\n", "hexadecimal"),
    ("top: x\ncampo_que_nao_existe: 1\n", "campo_que_nao_existe"),
    ("top: [\n", "YAML inválido"),
    ("- lista\n", "mapeamento"),
    ("top: x\nbypass: ['A=2']\n", "bypass inválido"),
])
def test_config_invalido(text, trecho):
    with pytest.raises(DftConfigError) as e:
        parse_dft_yaml(text)
    assert trecho in str(e.value)


# ------------------------------------------------------------------ parsers
def test_parser_cobertura_s27_bate_com_o_fault():
    c = parsers.parse_coverage_yml(FIX / "s27.prng.coverage.yml")
    assert (c.fault_sites, c.total_faults, c.detected) == (45, 90, 78)
    assert c.coverage_pct == pytest.approx(86.6667, abs=1e-3)
    assert c.coverage_pct == pytest.approx(100 * c.ratio_reported, abs=1e-3)
    # 12 falhas reais nos 3 inversores do reset; duplicatas escapadas (\_21_.d) ficam de fora
    assert len(c.undetected) == 12
    assert not any(u.startswith("\\") for u in c.undetected)


def test_parser_logs():
    a = parsers.parse_atpg_log((FIX / "atpg.log").read_text())
    assert a["fault_sites"] == 45 and a["vectors_final"] == 6 and a["vectors_generated"] > 6
    ch = parsers.parse_chain_log((FIX / "chain.log").read_text())
    assert ch == {"verified": True, "internal_ffs": 3, "boundary_cells": 5, "length": 8}
    assert parsers.parse_tap_log((FIX / "tap.log").read_text()) == {"tap_verified": True, "vectors_verified": True}
    st = parsers.parse_yosys_stat((FIX / "area.scan_jtag.log").read_text(), "s27")
    assert st == {"area_um2": 57880.0, "cells": 241}


def test_hash_de_vetores_ignora_ordem_de_chaves():
    import json

    d = json.loads((FIX / "s27.tv.json").read_text())
    h1 = vectors_hash(FIX / "s27.tv.json")
    shuffled = {"coverageList": list(reversed([dict(reversed(list(e.items()))) for e in d["coverageList"]])),
                "outputs": d["outputs"], "inputs": d["inputs"]}
    p = FIX.parent / "_tmp_tv.json"
    try:
        p.write_text(json.dumps(shuffled))
        assert vectors_hash(p) == h1
    finally:
        p.unlink(missing_ok=True)


def test_fingerprint_depende_das_metricas():
    base = {"coverage": {"prng": {"detected": 78, "total_faults": 90, "vectors_final": 6}},
            "area": {"synth": {"area_um2": 3264.0}}}
    other = {"coverage": {"prng": {"detected": 77, "total_faults": 90, "vectors_final": 6}},
             "area": {"synth": {"area_um2": 3264.0}}}
    assert fingerprint(base) == fingerprint(dict(base)) != fingerprint(other)


def test_injeta_clock_ficticio(tmp_path):
    nl = tmp_path / "c17.cut.v"
    nl.write_text("module c17\n(\n  N1,\n  N22\n);\n  input N1;\n  output N22;\nendmodule\n")
    inject_dummy_port(nl, "c17", DUMMY_CLOCK)
    inject_dummy_port(nl, "c17", DUMMY_CLOCK)  # idempotente
    s = nl.read_text()
    assert s.count(f"input {DUMMY_CLOCK};") == 1
    assert s.index(DUMMY_CLOCK) < s.index("N1")


# ------------------------------------------------------------------ regressão
def test_regressao_reprova_queda_acima_do_limite():
    v = evaluate(Metrics(83.67, 7, 58904), Metrics(86.67, 6, 57880), max_drop_pp=0.5)
    assert not v.passed and v.regression
    assert "caiu 3.00 p.p." in v.summary


def test_regressao_tolera_queda_dentro_do_limite():
    v = evaluate(Metrics(99.6), Metrics(100.0), max_drop_pp=0.5)
    assert v.passed and not v.regression


def test_regressao_alvo_absoluto_sem_baseline():
    v = evaluate(Metrics(84.0), None, max_drop_pp=0.5, target=85)
    assert not v.passed and not v.regression
    assert {c.name for c in v.checks} == {"coverage_target", "coverage_drop"}


def test_regressao_alertas_nao_reprovam():
    v = evaluate(Metrics(100, 40, 200), Metrics(100, 20, 100), max_drop_pp=0.5,
                 warn_vector_increase_pct=25, warn_area_increase_pct=10)
    assert v.passed
    assert v.summary.startswith("aprovado com alertas")
    assert {c.name for c in v.checks if not c.ok} == {"vector_increase", "area_increase"}


def test_regressao_desligada():
    v = evaluate(Metrics(10), Metrics(100), max_drop_pp=0.5, enabled=False)
    assert v.passed and v.checks == []


# ------------------------------------------------------------------ armazenamento
def _tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_extrai_tar_e_casa_sources(tmp_path):
    names = storage.extract_archive(_tar({"rtl/mac8.v": b"module mac8; endmodule", "README": b"x"}), "a.tar.gz", tmp_path)
    assert sorted(names) == ["README", "rtl/mac8.v"]
    assert storage.match_sources(tmp_path, ["rtl/*.v"]) == ["rtl/mac8.v"]
    assert storage.match_sources(tmp_path, ["**/*.v"]) == ["rtl/mac8.v"]


@pytest.mark.parametrize("evil", ["../fora.v", "/etc/passwd", "a/../../b.v"])
def test_rejeita_path_traversal(tmp_path, evil):
    with pytest.raises(storage.InputError):
        storage.extract_archive(_tar({evil: b"x"}), "a.tgz", tmp_path)


def test_hash_de_entrada_estavel(tmp_path):
    (tmp_path / "a.v").write_text("module a; endmodule")
    h1 = storage.hash_inputs(tmp_path, ["a.v"], "top: a\n")
    assert h1 == storage.hash_inputs(tmp_path, ["a.v"], "top: a\n")
    assert h1 != storage.hash_inputs(tmp_path, ["a.v"], "top: a\nclock: c\n")
