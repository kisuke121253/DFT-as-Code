"""
Schema do arquivo dft.yaml.

Exemplo mínimo (circuito sequencial):

    version: 1
    project: mac8
    top: mac8
    sources: [rtl/mac8.v]
    clock: clk
    reset: rst_n
    reset_active_low: true

Exemplo combinacional (ISCAS'85): basta omitir `clock`. O pipeline injeta uma
porta de clock fictícia só para o ATPG e pula scan chain e JTAG.

Todos os outros campos têm padrão; ver `DftConfig` abaixo e docs/dft-yaml.md.
"""

from __future__ import annotations

import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
SEED = re.compile(r"^[0-9A-Fa-f]{1,16}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScanConfig(Strict):
    enabled: bool = True
    # O Fault 0.9.4 só monta uma cadeia; o campo existe para o modelo de dados
    # já suportar várias cadeias quando a ferramenta suportar.
    chains: int = Field(1, ge=1)
    sin: str = "sin"
    sout: str = "sout"
    shift: str = "shift"
    test: str = "test"

    @field_validator("chains")
    @classmethod
    def only_one_chain(cls, v: int) -> int:
        if v != 1:
            raise ValueError("o Fault 0.9.4 só suporta 1 scan chain (scan.chains: 1)")
        return v


class JtagConfig(Strict):
    enabled: bool = True
    # aplica os vetores ATPG pela TAP em simulação (fault asm + fault tap -t/-g)
    apply_vectors: bool = True


class AtpgConfig(Strict):
    generator: Literal["prng", "quaigh"] = "prng"
    # roda o outro gerador também, só para comparação (não decide aprovação)
    compare_with_quaigh: bool = False
    initial_vectors: int = Field(100, ge=1, description="fault atpg -v")
    increment: int = Field(50, ge=1, description="fault atpg -r")
    min_coverage: float = Field(
        80.0, gt=0, le=100,
        description="fault atpg -m. Não use 0: no Fault 0.9.4 quebra na compactação.",
    )
    ceiling: int | None = Field(None, ge=1, description="fault atpg --ceiling")
    seed: str = "DEADCAFEDEADF00D"

    @field_validator("seed")
    @classmethod
    def hex_seed(cls, v: str) -> str:
        if not SEED.match(v):
            raise ValueError("seed deve ser hexadecimal de até 16 dígitos, sem 0x")
        return v.upper()


class CoverageConfig(Strict):
    # cobertura mínima aceitável (%); abaixo disso o job é reprovado
    target: float | None = Field(None, ge=0, le=100)


class RegressionConfig(Strict):
    enabled: bool = True
    # queda máxima tolerada de cobertura em pontos percentuais
    max_drop_pp: float | None = Field(None, ge=0)
    # com quem comparar:
    #   last_passed: último job aprovado do mesmo branch (padrão; evita "catraca"
    #                de pequenas quedas aceitas uma após a outra)
    #   previous:    último job concluído do mesmo branch, aprovado ou não
    baseline: Literal["last_passed", "previous"] = "last_passed"
    # branch de onde tirar a baseline; vazio = mesmo branch do job, com
    # fallback para o branch padrão do projeto
    baseline_branch: str | None = None
    # alertas (não reprovam) para aumento de vetores e de área
    warn_vector_increase_pct: float | None = Field(25.0, ge=0)
    warn_area_increase_pct: float | None = Field(10.0, ge=0)


class DftConfig(Strict):
    version: Literal[1] = 1
    project: str | None = Field(None, description="nome do projeto; padrão = top")
    top: str
    sources: list[str] = Field(default_factory=lambda: ["**/*.v"], min_length=1)
    clock: str | None = None
    reset: str | None = None
    reset_active_low: bool = False
    # entradas mantidas fixas durante as simulações (ex.: VDD, GND do ISCAS'89)
    bypass: list[str] = Field(default_factory=list)
    tech: str = "osu035"
    scan: ScanConfig = Field(default_factory=ScanConfig)
    jtag: JtagConfig = Field(default_factory=JtagConfig)
    atpg: AtpgConfig = Field(default_factory=AtpgConfig)
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)
    regression: RegressionConfig = Field(default_factory=RegressionConfig)
    timeout_minutes: int | None = Field(None, ge=1, le=24 * 60)

    @field_validator("top", "clock", "reset")
    @classmethod
    def verilog_ident(cls, v: str | None) -> str | None:
        if v is not None and not IDENT.match(v):
            raise ValueError(f"'{v}' não é um identificador Verilog válido")
        return v

    @field_validator("bypass")
    @classmethod
    def bypass_idents(cls, v: list[str]) -> list[str]:
        for name in v:
            base = name.split("=")[0]
            if not IDENT.match(base) or name.count("=") > 1 or (
                "=" in name and name.split("=")[1] not in {"0", "1"}
            ):
                raise ValueError(f"bypass inválido: '{name}' (use NOME ou NOME=0/NOME=1)")
        return v

    @field_validator("tech")
    @classmethod
    def tech_name(cls, v: str) -> str:
        if not re.match(r"^[A-Za-z0-9_.-]+$", v):
            raise ValueError("nome de tecnologia inválido")
        return v

    @field_validator("sources")
    @classmethod
    def safe_globs(cls, v: list[str]) -> list[str]:
        for g in v:
            if g.startswith("/") or ".." in g.replace("\\", "/").split("/"):
                raise ValueError(f"source '{g}' precisa ser relativo e não pode conter '..'")
        return v

    @model_validator(mode="after")
    def combinational_rules(self) -> "DftConfig":
        if self.project is None:
            self.project = self.top
        if not re.match(r"^[A-Za-z0-9_.-]{1,100}$", self.project):
            raise ValueError("project deve ter só letras, números, '.', '_' ou '-'")
        if self.clock is None:
            # circuito combinacional: não há flip-flops para encadear
            self.scan.enabled = False
            self.jtag.enabled = False
        if self.jtag.enabled and not self.scan.enabled:
            raise ValueError("jtag.enabled exige scan.enabled (a TAP acessa a scan chain)")
        return self

    @property
    def is_combinational(self) -> bool:
        return self.clock is None


class DftConfigError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def parse_dft_yaml(text: str) -> DftConfig:
    """Lê e valida o conteúdo de um dft.yaml. Levanta DftConfigError com mensagens legíveis."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise DftConfigError([f"YAML inválido: {e}"]) from e
    if not isinstance(data, dict):
        raise DftConfigError(["o dft.yaml deve ser um mapeamento (chave: valor)"])
    try:
        return DftConfig.model_validate(data)
    except ValidationError as e:
        msgs = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "(raiz)"
            msg = err["msg"].removeprefix("Value error, ")
            msgs.append(f"{loc}: {msg}")
        raise DftConfigError(msgs) from e
