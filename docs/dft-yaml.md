# Referência do `dft.yaml`

Valide sem rodar nada com `dftctl validate dft.yaml -v` (mostra a configuração com todos os padrões) ou `POST /api/v1/validate`. Campos desconhecidos são rejeitados, para pegar erros de digitação.

| Campo | Padrão | Descrição |
|---|---|---|
| `version` | `1` | Versão do formato. |
| `project` | = `top` | Nome do projeto no histórico (`[A-Za-z0-9_.-]`). |
| `top` | **obrigatório** | Módulo top. |
| `sources` | `["**/*.v"]` | Globs relativos à raiz do envio/repositório. `**` vale para subpastas. |
| `clock` | — | Sinal de clock. **Omitido = circuito combinacional** (ISCAS'85): o pipeline injeta uma porta de clock fictícia só para o ATPG e pula scan e JTAG. |
| `reset` | — | Sinal de reset. Em circuito sequencial sem reset (s298, s344…), o pipeline injeta um reset fictício porque o `fault chain` 0.9.4 exige um. |
| `reset_active_low` | `false` | `true` para `rst_n`. |
| `bypass` | `[]` | Entradas mantidas fixas nas simulações (`NOME` = 1, `NOME=0` = 0). Use `[VDD, GND]` nos ISCAS'89, que declaram a alimentação como entrada: sem isso o s27 dá 82,98% (78/94) em vez de 86,67% (78/90). |
| `tech` | `osu035` | Pasta em `tech/` com `.lib`, `.v` e `config.yml` do Fault (ou um `tech.json` apontando para eles). |
| `scan.enabled` | `true` | Insere a scan chain (`fault chain`). |
| `scan.chains` | `1` | O Fault 0.9.4 só monta uma cadeia; outros valores são rejeitados. |
| `scan.sin/sout/shift/test` | `sin/sout/shift/test` | Nomes das portas de scan. |
| `jtag.enabled` | `true` | Insere a TAP IEEE 1149.1 (`fault tap`). Exige scan. |
| `jtag.apply_vectors` | `true` | Monta os vetores (`fault asm`) e verifica, em simulação, que aplicados pela JTAG eles dão as respostas esperadas. |
| `atpg.generator` | `prng` | Gerador que decide aprovação: `prng` (interno do Fault) ou `quaigh` (determinístico). |
| `atpg.compare_with_quaigh` | `false` | Roda os dois geradores e guarda o outro como comparação. |
| `atpg.initial_vectors` | `100` | `fault atpg -v` |
| `atpg.increment` | `50` | `fault atpg -r` |
| `atpg.min_coverage` | `80` | `fault atpg -m`. **Não aceita 0**: no Fault 0.9.4 `-m 0` não roda nenhum vetor e quebra na compactação. |
| `atpg.ceiling` | — | `fault atpg --ceiling` (teto de vetores). |
| `atpg.seed` | `DEADCAFEDEADF00D` | Semente do PRNG (hex, sem `0x`). |
| `coverage.target` | — | Cobertura mínima absoluta (%). Abaixo dela o job é **reprovado**. |
| `regression.enabled` | `true` | Liga o detector de regressão. |
| `regression.max_drop_pp` | `DFT_DEFAULT_MAX_DROP_PP` (0,5) | Queda máxima tolerada em pontos percentuais. |
| `regression.baseline` | `last_passed` | `last_passed`: compara com o último job **aprovado**, para que várias quedas pequenas seguidas não se acumulem. `previous`: com o último concluído. |
| `regression.baseline_branch` | — | Branch da baseline. Vazio = mesmo branch do job e, se não houver, o branch padrão do projeto (um PR é comparado com `main`). |
| `regression.warn_vector_increase_pct` | `25` | Alerta (não reprova) se o número de vetores crescer mais que isso. |
| `regression.warn_area_increase_pct` | `10` | Alerta (não reprova) se a área final crescer mais que isso. |
| `timeout_minutes` | `DFT_JOB_TIMEOUT_MIN` (120) | Tempo máximo do job (limitado pelo valor do servidor). |

## Exemplos

**Sequencial com reset ativo baixo (MAC):** ver [`examples/mac8/dft.yaml`](../examples/mac8/dft.yaml).

**ISCAS'89:** ver [`examples/s27/dft.yaml`](../examples/s27/dft.yaml).

**ISCAS'85 (combinacional):**

```yaml
top: c432
sources: [c432.v]
coverage: {target: 99}
```
