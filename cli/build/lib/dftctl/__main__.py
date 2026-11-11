"""
dftctl — cliente de linha de comando do DFT as Code.

    dftctl run                      envia o RTL do dft.yaml local, espera e mostra o veredito
    dftctl run --no-wait            só enfileira e imprime o ID
    dftctl status <job>             status e etapas
    dftctl report <job> [--json]    relatório de cobertura
    dftctl logs <job> <etapa>       log de uma etapa
    dftctl artifacts <job> [-d DIR] lista ou baixa artefatos
    dftctl history <projeto>        cobertura por versão
    dftctl validate [dft.yaml]      valida o dft.yaml no servidor
    dftctl cancel <job> | retry <job>
    dftctl metrics [--project P]

Configuração: DFT_API_URL (padrão http://localhost:8000) e DFT_API_TOKEN.
Dentro do GitHub Actions, commit, branch, mensagem e repositório são detectados
pelas variáveis GITHUB_*.

Códigos de saída: 0 aprovado, 1 reprovado (regressão/alvo), 2 erro no fluxo,
3 timeout ou cancelado, 4 erro de uso/rede.
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

__version__ = "1.0.0"

EXIT = {"passed": 0, "failed": 1, "error": 2, "canceled": 3}
FINAL = set(EXIT)
ICON = {"passed": "✅", "failed": "❌", "error": "💥", "canceled": "⏹", "queued": "⏳", "running": "⚙️",
        "ok": "✔", "skipped": "·", "pending": "…"}


class CliError(Exception):
    pass


# ---------------------------------------------------------------- HTTP
class Client:
    def __init__(self, url: str, token: str | None, timeout: float = 60):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method: str, path: str, body: bytes | None = None, ctype: str | None = None, raw=False):
        url = path if path.startswith("http") else f"{self.url}/api/v1{path}"
        headers = {"User-Agent": f"dftctl/{__version__}", "Accept": "application/json"}
        if ctype:
            headers["Content-Type"] = ctype
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            try:
                j = json.loads(detail)
                msg = j.get("detail", detail)
                if j.get("errors"):
                    msg += "\n  - " + "\n  - ".join(j["errors"])
            except ValueError:
                msg = detail[:500]
            raise CliError(f"HTTP {e.code} em {method} {path}: {msg}") from None
        except urllib.error.URLError as e:
            raise CliError(f"não consegui falar com {self.url}: {e.reason}") from None
        return data if raw else json.loads(data or b"null")

    def get(self, path, raw=False):
        return self._req("GET", path, raw=raw)

    def post_json(self, path, obj):
        return self._req("POST", path, json.dumps(obj).encode(), "application/json")

    def post_multipart(self, path, fields: dict[str, str], files: list[tuple[str, str, bytes]]):
        boundary = f"----dftctl{uuid.uuid4().hex}"
        buf = io.BytesIO()
        for k, v in fields.items():
            buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode())
            buf.write(str(v).encode() + b"\r\n")
        for field, filename, data in files:
            buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                      f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode())
            buf.write(data + b"\r\n")
        buf.write(f"--{boundary}--\r\n".encode())
        return self._req("POST", path, buf.getvalue(), f"multipart/form-data; boundary={boundary}")


def client(args) -> Client:
    return Client(args.url or os.environ.get("DFT_API_URL", "http://localhost:8000"),
                  args.token or os.environ.get("DFT_API_TOKEN"))


# ---------------------------------------------------------------- contexto git / CI
def _git(*a, cwd=None) -> str:
    try:
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def git_context(root: Path) -> dict:
    env = os.environ
    ctx = {"commit_sha": "", "branch": "", "message": "", "author": "", "repo_url": ""}
    if env.get("GITHUB_ACTIONS") == "true":
        ctx["commit_sha"] = env.get("GITHUB_SHA", "")
        # em pull_request, GITHUB_SHA é o merge commit; o commit da cabeça do PR vem no evento
        ev_path = env.get("GITHUB_EVENT_PATH")
        if env.get("GITHUB_EVENT_NAME", "").startswith("pull_request") and ev_path and Path(ev_path).exists():
            ev = json.loads(Path(ev_path).read_text())
            ctx["commit_sha"] = ev.get("pull_request", {}).get("head", {}).get("sha", ctx["commit_sha"])
        ctx["branch"] = env.get("GITHUB_HEAD_REF") or env.get("GITHUB_REF_NAME", "")
        if env.get("GITHUB_REPOSITORY"):
            ctx["repo_url"] = f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{env['GITHUB_REPOSITORY']}"
        ctx["author"] = env.get("GITHUB_ACTOR", "")
    ctx["commit_sha"] = ctx["commit_sha"] or _git("rev-parse", "HEAD", cwd=root)
    ctx["branch"] = ctx["branch"] or _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    if ctx["branch"] == "HEAD":
        ctx["branch"] = ""
    if ctx["commit_sha"]:
        ctx["message"] = _git("log", "-1", "--format=%B", ctx["commit_sha"], cwd=root)
        ctx["author"] = ctx["author"] or _git("log", "-1", "--format=%an", ctx["commit_sha"], cwd=root)
    if not ctx["repo_url"]:
        remote = _git("remote", "get-url", "origin", cwd=root)
        if remote.startswith("git@github.com:"):
            remote = "https://github.com/" + remote.split(":", 1)[1]
        ctx["repo_url"] = remote.removesuffix(".git") if remote.startswith("http") else ""
    return ctx


# ---------------------------------------------------------------- empacotamento
def load_sources(config_path: Path) -> list[str]:
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        raise CliError("instale o PyYAML (pip install pyyaml)") from None
    cfg = yaml.safe_load(config_path.read_text()) or {}
    return cfg.get("sources") or ["**/*.v"]


def pack(root: Path, globs: list[str]) -> tuple[bytes, list[str]]:
    files: list[str] = []
    for g in globs:
        for p in sorted(glob.glob(str(root / g), recursive=True)):
            rel = Path(p).relative_to(root).as_posix()
            if Path(p).is_file() and rel not in files and "/.git/" not in f"/{rel}":
                files.append(rel)
    if not files:
        raise CliError(f"nenhum arquivo casou com sources {globs} em {root}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel in files:
            info = tf.gettarinfo(str(root / rel), arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            with open(root / rel, "rb") as f:
                tf.addfile(info, f)
    return buf.getvalue(), files


# ---------------------------------------------------------------- saída
def fmt_pct(v):
    return f"{v:.2f}%" if isinstance(v, (int, float)) else "—"


def print_report(r: dict) -> None:
    j = r["job"]
    print(f"\n{ICON.get(j['status'], '?')}  {j['project']} @ {(j['commit'] or '')[:10] or 'upload'}"
          f"  [{j['branch'] or '-'}]  status: {j['status'].upper()}")
    if j.get("error"):
        print(f"   erro: {j['error'].splitlines()[0]}")
    c = r.get("coverage")
    if c:
        print(f"   cobertura ({c['generator']}): {fmt_pct(c['coverage_pct'])}  "
              f"({c['detected']}/{c['total_faults']} falhas stuck-at, {c['fault_sites']} sites)")
        print(f"   vetores: {c['vectors_generated']} → {c['vectors_final']} após compactação")
    for g, o in (r.get("comparison") or {}).items():
        print(f"   comparação {g}: {fmt_pct(o['coverage_pct'])} com {o['vectors_final']} vetores")
    if r.get("scan"):
        s = r["scan"]
        print(f"   scan chain: {s.get('internal_ffs')} FFs + {s.get('boundary_cells')} boundary = {s.get('length')}"
              f" ({'verificada' if s.get('verified') else 'NÃO verificada'})")
    if r.get("jtag"):
        t = r["jtag"]
        print(f"   JTAG: TAP {'ok' if t.get('tap_verified') else 'falhou'}, vetores via TAP "
              f"{'ok' if t.get('vectors_verified') else 'não aplicados'}")
    if r.get("area"):
        print("   área: " + " → ".join(f"{v['area_um2']:.0f}" for v in r["area"].values()) + " µm²  ("
              + " → ".join(r["area"].keys()) + ")")
    reg = r.get("regression")
    if reg:
        for ch in reg.get("checks", []):
            mark = "ℹ" if ch["severity"] == "info" else "✔" if ch["ok"] else ("✖" if ch["severity"] == "error" else "⚠")
            print(f"   {mark} {ch['message']}")
    if j.get("run_s") is not None:
        print(f"   tempo: fila {j['queue_s']:.1f}s, execução {j['run_s']:.1f}s")
    print(f"   relatório: /api/v1/jobs/{j['id']}/report")


def markdown_summary(r: dict, base_url: str) -> str:
    j, c, reg = r["job"], r.get("coverage"), r.get("regression") or {}
    lines = [f"## {ICON.get(j['status'], '')} DFT as Code — {j['project']}", "",
             f"**Status:** `{j['status']}` &nbsp; **Commit:** `{(j['commit'] or '')[:10]}` &nbsp; "
             f"**Branch:** `{j['branch']}`", ""]
    if j.get("error"):
        lines += ["```", j["error"][:1500], "```", ""]
    if c:
        lines += ["| Métrica | Valor |", "|---|---|",
                  f"| Cobertura stuck-at ({c['generator']}) | **{fmt_pct(c['coverage_pct'])}** |",
                  f"| Falhas detectadas | {c['detected']} / {c['total_faults']} |",
                  f"| Vetores (gerados → compactados) | {c['vectors_generated']} → {c['vectors_final']} |"]
        for g, o in (r.get("comparison") or {}).items():
            lines.append(f"| Cobertura {g} (comparação) | {fmt_pct(o['coverage_pct'])} com {o['vectors_final']} vetores |")
        if r.get("scan"):
            lines.append(f"| Scan chain | {r['scan'].get('length')} células, "
                         f"{'verificada' if r['scan'].get('verified') else 'não verificada'} |")
        for stage, a in (r.get("area") or {}).items():
            lines.append(f"| Área {stage} | {a['area_um2']:.0f} µm² |")
        if j.get("run_s") is not None:
            lines.append(f"| Tempo de execução | {j['run_s']:.1f} s |")
        lines.append("")
    if reg.get("checks"):
        lines.append("**Verificações**\n")
        for ch in reg["checks"]:
            mark = "ℹ️" if ch["severity"] == "info" else "✅" if ch["ok"] else ("❌" if ch["severity"] == "error" else "⚠️")
            lines.append(f"- {mark} {ch['message']}")
        lines.append("")
    if c and c.get("undetected"):
        shown = ", ".join(f"`{x}`" for x in c["undetected"][:30])
        lines += [f"<details><summary>{len(c['undetected'])} falhas não detectadas</summary>\n\n{shown}\n</details>", ""]
    lines.append(f"[Relatório JSON]({base_url}/api/v1/jobs/{j['id']}/report)")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- comandos
def wait(cl: Client, job_id: str, timeout: float, quiet: bool) -> dict:
    t0, last, delay = time.monotonic(), None, 2.0
    while True:
        j = cl.get(f"/jobs/{job_id}")
        running = next((s["name"] for s in j["steps"] if s["status"] == "running"), None)
        state = (j["status"], running)
        if not quiet and state != last:
            print(f"   {ICON.get(j['status'], '')} {j['status']}" + (f" — etapa {running}" if running else ""),
                  file=sys.stderr, flush=True)
            last = state
        if j["status"] in FINAL:
            return j
        if time.monotonic() - t0 > timeout:
            raise TimeoutError
        time.sleep(delay)
        delay = min(delay * 1.3, 10)


def cmd_run(args) -> int:
    cl = client(args)
    cfg_path = Path(args.config).resolve()
    if not cfg_path.exists():
        raise CliError(f"{cfg_path} não existe")
    root = Path(args.root).resolve() if args.root else cfg_path.parent
    globs = load_sources(cfg_path)
    archive, files = pack(root, globs)
    ctx = git_context(root)
    for k in ("commit_sha", "branch", "message", "repo_url"):
        if getattr(args, k, None):
            ctx[k] = getattr(args, k)
    print(f"→ enviando {len(files)} arquivo(s) ({len(archive) / 1024:.1f} KiB) para {cl.url}"
          + (f", commit {ctx['commit_sha'][:10]}" if ctx["commit_sha"] else ""), file=sys.stderr)
    created = cl.post_multipart("/jobs", {**ctx, "dft_yaml": cfg_path.read_text()},
                                [("archive", "rtl.tar.gz", archive)])
    job_id = created["id"]
    print(f"   job {job_id} na fila", file=sys.stderr)
    if args.no_wait:
        print(job_id)
        return 0
    try:
        wait(cl, job_id, args.timeout, args.quiet)
    except TimeoutError:
        print(f"⏱  tempo de espera esgotado ({args.timeout}s); o job continua: dftctl status {job_id}",
              file=sys.stderr)
        return 3
    return finish(cl, job_id, args)


def finish(cl: Client, job_id: str, args) -> int:
    r = cl.get(f"/jobs/{job_id}/report")
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        print_report(r)
    summary = getattr(args, "summary", None)
    if not summary and os.environ.get("GITHUB_ACTIONS") == "true":
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(markdown_summary(r, cl.url))
    if getattr(args, "report_out", None):
        Path(args.report_out).write_text(json.dumps(r, indent=2, ensure_ascii=False))
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out and os.environ.get("GITHUB_ACTIONS") == "true":
        with open(gh_out, "a") as f:
            f.write(f"job_id={job_id}\nstatus={r['job']['status']}\n")
            if r.get("coverage"):
                f.write(f"coverage={r['coverage']['coverage_pct']}\n")
    return EXIT.get(r["job"]["status"], 2)


def cmd_status(args) -> int:
    j = client(args).get(f"/jobs/{args.job}")
    if args.json:
        print(json.dumps(j, indent=2, ensure_ascii=False))
        return EXIT.get(j["status"], 0)
    print(f"{ICON.get(j['status'], '')} {j['id']}  {j['project']}@{j['commit'][:10]}  {j['status']}  "
          f"cobertura {fmt_pct(j['coverage_pct'])}")
    for s in j["steps"]:
        d = f"{s['duration_s']:.1f}s" if s["duration_s"] is not None else ""
        print(f"   {ICON.get(s['status'], '?')} {s['name']:<9} {s['status']:<8} {d}")
    if j.get("error"):
        print(f"\n{j['error']}")
    return EXIT.get(j["status"], 0) if j["status"] in FINAL else 0


def cmd_report(args) -> int:
    return finish(client(args), args.job, args)


def cmd_logs(args) -> int:
    sys.stdout.write(client(args).get(f"/jobs/{args.job}/logs/{args.step}", raw=True).decode("utf-8", "replace"))
    return 0


def cmd_artifacts(args) -> int:
    cl = client(args)
    items = cl.get(f"/jobs/{args.job}/artifacts")
    if not args.download:
        for a in items:
            print(f"{a['kind']:<9} {a['size']:>10}  {a['name']}")
        return 0
    out = Path(args.download)
    for a in items:
        if args.kind and a["kind"] not in args.kind.split(","):
            continue
        dest = out / a["name"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(cl.get(f"/jobs/{args.job}/artifacts/{urllib.parse.quote(a['name'])}", raw=True))
        print(f"↓ {dest}")
    return 0


def cmd_history(args) -> int:
    q = f"?limit={args.limit}" + (f"&branch={urllib.parse.quote(args.branch)}" if args.branch else "")
    h = client(args).get(f"/projects/{args.project}/history{q}")
    if args.json:
        print(json.dumps(h, indent=2, ensure_ascii=False))
        return 0
    print(f"{'quando':<17} {'commit':<10} {'branch':<14} {'status':<8} {'cobertura':>9} {'vetores':>7} "
          f"{'área µm²':>10}  resumo")
    for it in h["items"]:
        when = it["created_at"][:16].replace("T", " ")
        area = f"{it['area_um2']:.0f}" if it["area_um2"] else "—"
        print(f"{when:<17} {it['commit'][:10]:<10} {it['branch'][:14]:<14} {it['status']:<8} "
              f"{fmt_pct(it['coverage_pct']):>9} {str(it['vector_count'] or '—'):>7} {area:>10}  {it['summary'][:60]}")
    return 0


def cmd_validate(args) -> int:
    r = client(args).post_json("/validate", {"dft_yaml": Path(args.config).read_text()})
    if r["valid"]:
        print("✔ dft.yaml válido")
        if args.verbose:
            print(json.dumps(r["config"], indent=2, ensure_ascii=False))
        return 0
    print("✖ dft.yaml inválido:\n  - " + "\n  - ".join(r["errors"]))
    return 4


def cmd_cancel(args) -> int:
    j = client(args).post_json(f"/jobs/{args.job}/cancel", {})
    print(f"{j['id']}: {j['status']}")
    return 0


def cmd_retry(args) -> int:
    cl = client(args)
    j = cl.post_json(f"/jobs/{args.job}/retry", {})
    print(f"novo job {j['id']}", file=sys.stderr)
    if args.no_wait:
        print(j["id"])
        return 0
    wait(cl, j["id"], args.timeout, args.quiet)
    return finish(cl, j["id"], args)


def cmd_metrics(args) -> int:
    q = f"?project={urllib.parse.quote(args.project)}" if args.project else ""
    print(json.dumps(client(args).get(f"/metrics{q}"), indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dftctl", description="Cliente do pipeline DFT as Code")
    p.add_argument("--url", help="URL da API (ou DFT_API_URL)")
    p.add_argument("--token", help="token da API (ou DFT_API_TOKEN)")
    p.add_argument("--version", action="version", version=f"dftctl {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def waiting(sp):
        sp.add_argument("--no-wait", action="store_true", help="não espera terminar")
        sp.add_argument("--timeout", type=float, default=3600, help="espera máxima em segundos (padrão 3600)")
        sp.add_argument("-q", "--quiet", action="store_true")
        sp.add_argument("--json", action="store_true", help="imprime o relatório em JSON")
        sp.add_argument("--summary", help="acrescenta um resumo Markdown a este arquivo ($GITHUB_STEP_SUMMARY)")
        sp.add_argument("--report-out", help="salva o relatório JSON neste arquivo")

    sp = sub.add_parser("run", help="envia o RTL e roda o fluxo DFT")
    sp.add_argument("-c", "--config", default="dft.yaml")
    sp.add_argument("--root", help="raiz dos caminhos em 'sources' (padrão: pasta do dft.yaml)")
    sp.add_argument("--commit", dest="commit_sha")
    sp.add_argument("--branch")
    sp.add_argument("--message")
    sp.add_argument("--repo-url", dest="repo_url")
    waiting(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("status", help="status de um job")
    sp.add_argument("job")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("report", help="relatório de um job")
    sp.add_argument("job")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--summary")
    sp.add_argument("--report-out")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("logs", help="log de uma etapa")
    sp.add_argument("job")
    sp.add_argument("step", help="synth, cut, atpg, nl2bench, quaigh, chain, asm, tap, area")
    sp.set_defaults(func=cmd_logs)

    sp = sub.add_parser("artifacts", help="lista ou baixa artefatos")
    sp.add_argument("job")
    sp.add_argument("-d", "--download", metavar="DIR")
    sp.add_argument("-k", "--kind", help="filtra por tipo: netlist,vectors,coverage,log,report...")
    sp.set_defaults(func=cmd_artifacts)

    sp = sub.add_parser("history", help="cobertura por versão de um projeto")
    sp.add_argument("project")
    sp.add_argument("-b", "--branch")
    sp.add_argument("-n", "--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("validate", help="valida um dft.yaml")
    sp.add_argument("config", nargs="?", default="dft.yaml")
    sp.add_argument("-v", "--verbose", action="store_true", help="mostra a configuração completa")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("cancel", help="cancela um job")
    sp.add_argument("job")
    sp.set_defaults(func=cmd_cancel)

    sp = sub.add_parser("retry", help="roda de novo a mesma entrada")
    sp.add_argument("job")
    waiting(sp)
    sp.set_defaults(func=cmd_retry)

    sp = sub.add_parser("metrics", help="métricas agregadas do pipeline")
    sp.add_argument("--project")
    sp.set_defaults(func=cmd_metrics)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as e:
        print(f"dftctl: {e}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
