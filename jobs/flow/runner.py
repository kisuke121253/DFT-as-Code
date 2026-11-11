"""
Executor do fluxo DFT (versão "job de worker" do etapa1/dft/run_flow.sh).

Etapas, na ordem:
    synth      fault synth   RTL -> <top>.nl.v (Yosys, mapeado na biblioteca)
    cut        fault cut     corta os FFs -> <top>.cut.v (circuito combinacional p/ o ATPG)
    atpg       fault atpg    PRNG + simulação de falhas + compactação -> <top>.tv.json, cobertura
    nl2bench   nl2bench      <top>.cut.v -> <top>.bench (só se usar Quaigh)
    quaigh     fault atpg -g Quaigh  ATPG determinístico (gerador principal ou comparação)
    chain      fault chain   scan chain + boundary scan, verificada por simulação -> <top>.chained.v
    asm        fault asm     vetores e respostas esperadas no formato da cadeia -> .tv.bin/.au.bin
    tap        fault tap     controlador JTAG + vetores aplicados pela TAP -> <top>.jtag.v
    area       yosys stat    área (µm²) e células de cada netlist

O módulo não depende do Django: recebe caminhos e devolve um dicionário de
resultado, chamando `on_event` a cada mudança de etapa.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..dftconfig import DftConfig
from . import parsers

DUMMY_CLOCK = "__dft_clk"
# o `fault chain` 0.9.4 quebra em circuitos sem reset (procura uma porta 'rst'):
# nesses casos injetamos uma entrada de reset fictícia, que nunca é usada
DUMMY_RESET = "__dft_rst"


@dataclass
class Tools:
    fault: str = "fault"
    nl2bench: str = "nl2bench"
    yosys: str = "yosys"
    fault_threads: int = 1


@dataclass
class Tech:
    name: str
    liberty: Path          # .lib para síntese/chain/tap
    cell_model: Path       # .v para simulação
    scl_config: Path       # config.yml do Fault (DFFs e MUX)

    @classmethod
    def load(cls, tech_dir: Path, name: str) -> "Tech":
        root = Path(tech_dir) / name
        if not root.is_dir():
            available = sorted(p.name for p in Path(tech_dir).iterdir() if p.is_dir()) if Path(tech_dir).is_dir() else []
            raise FlowError(f"tecnologia '{name}' não encontrada (disponíveis: {', '.join(available) or 'nenhuma'})")
        manifest = root / "tech.json"
        if manifest.exists():
            m = json.loads(manifest.read_text())
            return cls(name, root / m["liberty"], root / m["cell_model"], root / m["scl_config"])
        libs = sorted(p for p in root.glob("*.lib") if "muxonly" not in p.name and "simple" not in p.name)
        models = sorted(root.glob("*.v"))
        if not libs or not models or not (root / "config.yml").exists():
            raise FlowError(f"tecnologia '{name}' incompleta: precisa de .lib, .v e config.yml (ou tech.json)")
        return cls(name, libs[0], models[0], root / "config.yml")


class FlowError(RuntimeError):
    def __init__(self, message: str, step: str | None = None, log_tail: str = ""):
        self.step = step
        self.log_tail = log_tail
        super().__init__(message)


class FlowTimeout(FlowError):
    pass


@dataclass
class StepPlan:
    name: str
    run: Callable[[], dict]
    enabled: bool = True
    skip_reason: str = ""


EventFn = Callable[[str, str, dict], None]   # (etapa, evento, dados); evento: start|ok|failed|skipped


@dataclass
class FlowRunner:
    config: DftConfig
    sources: list[Path]
    work: Path
    logs: Path
    tech: Tech
    tools: Tools = field(default_factory=Tools)
    timeout_s: float = 7200
    on_event: EventFn | None = None

    def __post_init__(self):
        self.work = Path(self.work)
        self.logs = Path(self.logs)
        self.work.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self._deadline = time.monotonic() + self.timeout_s
        self.result: dict = {"steps": {}, "coverage": {}, "area": {}}
        self._commands: dict[str, list[str]] = {}
        t = self.config.top
        self.f = {
            "nl": f"{t}.nl.v", "cut": f"{t}.cut.v",
            "tv": f"{t}.tv.json", "cov": f"{t}.prng.coverage.yml",
            "bench": f"{t}.bench",
            "qtv": f"{t}.quaigh.tv.json", "qcov": f"{t}.quaigh.coverage.yml",
            "chained": f"{t}.chained.v", "tvbin": f"{t}.tv.bin", "aubin": f"{t}.au.bin",
            "jtag": f"{t}.jtag.v",
        }

    # ------------------------------------------------------------ infraestrutura
    def _emit(self, step: str, event: str, data: dict | None = None):
        if self.on_event:
            self.on_event(step, event, data or {})

    def _remaining(self) -> float:
        return self._deadline - time.monotonic()

    def _env(self) -> dict:
        env = dict(os.environ)
        env["FAULT_THREADS"] = str(self.tools.fault_threads)
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    def _run(self, step: str, argv: list[str], log_name: str | None = None) -> str:
        """Roda um comando no diretório de trabalho, grava o log e devolve a saída."""
        remaining = self._remaining()
        if remaining <= 0:
            raise FlowTimeout("tempo máximo do job esgotado", step)
        log_path = self.logs / (log_name or f"{step}.log")
        cmd = shlex.join(argv)
        with open(log_path, "a", encoding="utf-8", errors="replace") as log:
            log.write(f"$ {cmd}\n")
            log.flush()
            try:
                proc = subprocess.run(
                    argv, cwd=self.work, env=self._env(), stdout=log, stderr=subprocess.STDOUT,
                    timeout=remaining, stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as e:
                raise FlowTimeout(f"tempo máximo do job esgotado durante '{step}'", step, _tail(log_path)) from e
            except FileNotFoundError as e:
                raise FlowError(f"executável não encontrado: {argv[0]}", step) from e
            log.write(f"\n[exit {proc.returncode}]\n")
        text = log_path.read_text(errors="replace")
        self._commands.setdefault(step, []).append(cmd)
        if proc.returncode != 0:
            hint = ""
            if "Illegal instruction" in text:
                hint = " (bug conhecido do Fault com várias threads: use FAULT_THREADS=1)"
            raise FlowError(f"'{step}' terminou com código {proc.returncode}{hint}", step, _tail(log_path))
        return text

    def _need(self, step: str, *names: str):
        for n in names:
            if not (self.work / n).exists():
                raise FlowError(f"'{step}' não gerou {n}", step)

    # ------------------------------------------------------------ argumentos comuns
    @property
    def clock(self) -> str:
        return self.config.clock or DUMMY_CLOCK

    @property
    def reset(self) -> str | None:
        if self.config.reset:
            return self.config.reset
        return None if self.config.is_combinational else DUMMY_RESET

    def _sim_args(self) -> list[str]:
        """--clock/--reset/--activeLow/--bypassing comuns a cut, atpg, chain e tap."""
        a = ["--clock", self.clock]
        if self.reset:
            a += ["--reset", self.reset]
            if self.config.reset and self.config.reset_active_low:
                a.append("--activeLow")
        for b in self.config.bypass:
            a += ["--bypassing", b]
        return a

    def _scan_names(self) -> list[str]:
        s = self.config.scan
        return ["--sin", s.sin, "--sout", s.sout, "--shift", s.shift, "--test", s.test]

    # ------------------------------------------------------------ etapas
    def step_synth(self) -> dict:
        argv = [self.tools.fault, "synth", "-l", str(self.tech.liberty), "-t", self.config.top,
                "-o", self.f["nl"], *map(str, self.sources)]
        self._run("synth", argv)
        self._need("synth", self.f["nl"])
        if self.reset == DUMMY_RESET:
            inject_dummy_port(self.work / self.f["nl"], self.config.top, DUMMY_RESET, step="synth")
        return {"netlist": self.f["nl"]}

    def step_cut(self) -> dict:
        argv = [self.tools.fault, "cut", "-s", str(self.tech.scl_config), *self._sim_args(),
                "-o", self.f["cut"], self.f["nl"]]
        text = self._run("cut", argv)
        self._need("cut", self.f["cut"])
        no_ff = "Failed to detect any flip-flop cells" in text
        if self.config.is_combinational:
            inject_dummy_port(self.work / self.f["cut"], self.config.top, DUMMY_CLOCK)
        elif no_ff:
            raise FlowError(
                "nenhum flip-flop encontrado; se o circuito é combinacional, remova 'clock' do dft.yaml",
                "cut",
            )
        return {"netlist": self.f["cut"], "flip_flops_found": not no_ff}

    def _atpg(self, step: str, generator: str) -> dict:
        a = self.config.atpg
        if generator == "prng":
            out, cov = self.f["tv"], self.f["cov"]
            gen = ["-v", str(a.initial_vectors), "-r", str(a.increment), "-m", str(a.min_coverage),
                   "--rng-seed", a.seed]
            if a.ceiling:
                gen += ["--ceiling", str(a.ceiling)]
        else:
            out, cov = self.f["qtv"], self.f["qcov"]
            gen = ["-g", "Quaigh", "-b", self.f["bench"]]
        argv = [self.tools.fault, "atpg", "-c", str(self.tech.cell_model), *self._sim_args(), *gen,
                "-o", out, "--output-coverage-metadata", cov, self.f["cut"]]
        t0 = time.monotonic()
        text = self._run(step, argv)
        dt = time.monotonic() - t0
        self._need(step, out, cov)
        c = parsers.parse_coverage_yml(self.work / cov)
        log = parsers.parse_atpg_log(text)
        entry = {
            **c.to_dict(),
            "generator": generator,
            "vectors_generated": log.get("vectors_generated"),
            "vectors_final": log.get("vectors_final"),
            "hit_ceiling": log.get("hit_ceiling"),
            "duration_s": round(dt, 3),
            "vectors_file": out,
            "coverage_file": cov,
            "vectors_sha256": vectors_hash(self.work / out),
        }
        self.result["coverage"][generator] = entry
        return {k: entry[k] for k in ("coverage_pct", "detected", "total_faults", "vectors_final")}

    def step_atpg(self) -> dict:
        return self._atpg("atpg", "prng")

    def step_nl2bench(self) -> dict:
        argv = [self.tools.nl2bench, "-l", str(self.tech.liberty), "-o", self.f["bench"]]
        # só clock e reset saem do .bench; os demais 'bypass' continuam como entradas
        # (o Quaigh aborta se uma entrada removida alimentar alguma porta) e são
        # mantidos fixos depois, na simulação de falhas do fault atpg.
        for b in [self.clock, self.reset]:
            if b:
                argv += ["-b", b]
        argv.append(self.f["cut"])
        self._run("nl2bench", argv)
        self._need("nl2bench", self.f["bench"])
        return {"bench": self.f["bench"]}

    def step_quaigh(self) -> dict:
        return self._atpg("quaigh", "quaigh")

    def step_chain(self) -> dict:
        argv = [self.tools.fault, "chain", "-l", str(self.tech.liberty), "-c", str(self.tech.cell_model),
                "-s", str(self.tech.scl_config), *self._sim_args(), *self._scan_names(),
                "-o", self.f["chained"], self.f["nl"]]
        text = self._run("chain", argv)
        self._need("chain", self.f["chained"])
        info = parsers.parse_chain_log(text)
        if not info["verified"]:
            raise FlowError("a verificação da scan chain não confirmou sucesso", "chain", _tail(self.logs / "chain.log"))
        self.result["scan"] = info
        return info

    def step_asm(self) -> dict:
        argv = [self.tools.fault, "asm", self.primary_vectors, self.f["chained"],
                "-o", self.f["tvbin"], "-O", self.f["aubin"]]
        self._run("asm", argv)
        self._need("asm", self.f["tvbin"], self.f["aubin"])
        return parsers.parse_bin_metadata(self.work / self.f["tvbin"])

    def step_tap(self) -> dict:
        argv = [self.tools.fault, "tap", "-l", str(self.tech.liberty), "-c", str(self.tech.cell_model),
                *self._sim_args(), *self._scan_names()]
        if self.config.jtag.apply_vectors:
            argv += ["-t", self.f["tvbin"], "-g", self.f["aubin"]]
        argv += ["-o", self.f["jtag"], self.f["chained"]]
        text = self._run("tap", argv)
        self._need("tap", self.f["jtag"])
        info = parsers.parse_tap_log(text)
        if not info["tap_verified"]:
            raise FlowError("a verificação da TAP não confirmou sucesso", "tap", _tail(self.logs / "tap.log"))
        if self.config.jtag.apply_vectors and not info["vectors_verified"]:
            raise FlowError("os vetores aplicados pela JTAG não bateram com as respostas esperadas", "tap",
                            _tail(self.logs / "tap.log"))
        self.result["jtag"] = info
        return info

    def step_area(self) -> dict:
        stages = [("synth", self.f["nl"]), ("scan", self.f["chained"]), ("scan_jtag", self.f["jtag"])]
        out = {}
        for stage, nl in stages:
            if not (self.work / nl).exists():
                continue
            script = (f"read_liberty -lib {self.tech.liberty}; read_verilog {nl}; "
                      f"hierarchy -top {self.config.top}; stat -liberty {self.tech.liberty}")
            text = self._run("area", [self.tools.yosys, "-p", script], log_name=f"area.{stage}.log")
            stat = parsers.parse_yosys_stat(text, self.config.top)
            if stat:
                out[stage] = {"netlist": nl, **stat}
        self.result["area"] = out
        return {k: v["area_um2"] for k, v in out.items()}

    # ------------------------------------------------------------ orquestração
    @property
    def primary_generator(self) -> str:
        return self.config.atpg.generator

    @property
    def primary_vectors(self) -> str:
        return self.f["tv"] if self.primary_generator == "prng" else self.f["qtv"]

    def plan(self) -> list[StepPlan]:
        c = self.config
        use_prng = c.atpg.generator == "prng" or c.atpg.compare_with_quaigh
        use_quaigh = c.atpg.generator == "quaigh" or c.atpg.compare_with_quaigh
        no_scan = "scan desativado" if not c.scan.enabled else ""
        if c.is_combinational:
            no_scan = "circuito combinacional"
        no_jtag = no_scan or ("jtag desativado" if not c.jtag.enabled else "")
        return [
            StepPlan("synth", self.step_synth),
            StepPlan("cut", self.step_cut),
            StepPlan("atpg", self.step_atpg, use_prng, "gerador PRNG não usado"),
            StepPlan("nl2bench", self.step_nl2bench, use_quaigh, "Quaigh não usado"),
            StepPlan("quaigh", self.step_quaigh, use_quaigh, "Quaigh não usado"),
            StepPlan("chain", self.step_chain, not no_scan, no_scan),
            StepPlan("asm", self.step_asm, not no_jtag and c.jtag.apply_vectors,
                     no_jtag or "vetores pela JTAG desativados"),
            StepPlan("tap", self.step_tap, not no_jtag, no_jtag),
            StepPlan("area", self.step_area),
        ]

    def run(self) -> dict:
        t_start = time.monotonic()
        for p in self.plan():
            if not p.enabled:
                self.result["steps"][p.name] = {"status": "skipped", "reason": p.skip_reason}
                self._emit(p.name, "skipped", {"reason": p.skip_reason})
                continue
            self._emit(p.name, "start", {})
            t0 = time.monotonic()
            try:
                summary = p.run() or {}
            except FlowError as e:
                e.step = e.step or p.name
                dt = time.monotonic() - t0
                self.result["steps"][p.name] = {"status": "failed", "duration_s": round(dt, 3), "error": str(e)}
                self._emit(p.name, "failed", {"duration_s": dt, "error": str(e), "log_tail": e.log_tail,
                                              "commands": self._commands.get(p.name, [])})
                raise
            dt = time.monotonic() - t0
            self.result["steps"][p.name] = {"status": "ok", "duration_s": round(dt, 3), "summary": summary}
            self._emit(p.name, "ok", {"duration_s": dt, "summary": summary,
                                      "commands": self._commands.get(p.name, [])})
        self.result["duration_s"] = round(time.monotonic() - t_start, 3)
        self._finish()
        return self.result

    def _finish(self):
        primary = self.result["coverage"].get(self.primary_generator)
        self.result["primary_generator"] = self.primary_generator
        self.result["primary"] = primary
        area = self.result["area"]
        final_stage = next((s for s in ("scan_jtag", "scan", "synth") if s in area), None)
        self.result["final_area_um2"] = area[final_stage]["area_um2"] if final_stage else None
        self.result["final_netlist"] = area[final_stage]["netlist"] if final_stage else None
        self.result["fingerprint"] = fingerprint(self.result)


# ---------------------------------------------------------------- utilitários
def inject_dummy_port(netlist: Path, top: str, port: str, step: str = "cut") -> None:
    """Acrescenta uma entrada fictícia ao módulo top (clock para o ATPG, reset para o chain)."""
    s = netlist.read_text()
    pat = re.compile(r"(module\s+\\?" + re.escape(top) + r"\s*\()(.*?)(\)\s*;)", re.S)
    m = pat.search(s)
    if not m:
        raise FlowError(f"módulo '{top}' não encontrado na netlist", step)
    if re.search(r"\b" + re.escape(port) + r"\b", m[2]):
        return
    new_header = f"{m[1]}\n  {port},{m[2]}{m[3]}\n  input {port};"
    netlist.write_text(s[: m.start()] + new_header + s[m.end():])


def fingerprint(result: dict) -> str:
    """
    Assinatura das métricas do resultado: cobertura, número de vetores e área.

    Não inclui o conteúdo dos vetores de propósito: no Fault 0.9.4 a compactação do
    PRNG pode escolher conjuntos diferentes (com a mesma cobertura e a mesma
    quantidade) entre execuções idênticas. A reprodutibilidade dos vetores é medida
    à parte, por `vectors_hash`.
    """
    h = hashlib.sha256()
    for gen in sorted(result.get("coverage", {})):
        c = result["coverage"][gen]
        h.update(json.dumps([gen, c["detected"], c["total_faults"], c["vectors_final"]]).encode())
    for stage in sorted(result.get("area", {})):
        h.update(json.dumps([stage, result["area"][stage]["area_um2"]]).encode())
    return h.hexdigest()


def vectors_hash(path: Path) -> str | None:
    """Hash do conjunto de vetores de um .tv.json, independente da ordem de chaves e de vetores."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    names = [i.get("name") for i in data.get("inputs", [])]
    vecs = sorted(json.dumps(e.get("vector")) for e in data.get("coverageList", []))
    return hashlib.sha256(json.dumps([names, vecs]).encode()).hexdigest()


def tool_versions(tools: Tools) -> dict:
    def ver(argv):
        exe = shutil.which(argv[0])
        if not exe:
            return None
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        lines = [l for l in (r.stdout + r.stderr).splitlines() if l.strip() and "WARNING" not in l]
        return lines[0].strip() if lines else None

    return {
        "fault": ver([tools.fault, "--version"]),
        "yosys": ver([tools.yosys, "-V"]),
        "iverilog": ver(["iverilog", "-V"]),
        "quaigh": ver(["quaigh", "--version"]),
    }


def _tail(path: Path, n: int = 40) -> str:
    try:
        return "\n".join(Path(path).read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""
