"""
Armazenamento em disco compartilhado entre API e workers.

    $DFT_DATA_DIR/jobs/<job_id>/
        input/      fontes enviadas (ou clonadas) + dft.yaml
        work/       onde o fluxo roda; netlists, vetores e relatórios ficam aqui
        logs/       um log por etapa
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import zipfile
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from django.conf import settings

MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_FILES = 5000


class InputError(ValueError):
    """Entrada inválida enviada pelo usuário."""


def job_dir(job_id) -> Path:
    return Path(settings.DFT_DATA_DIR) / "jobs" / str(job_id)


def input_dir(job_id) -> Path:
    return job_dir(job_id) / "input"


def work_dir(job_id) -> Path:
    return job_dir(job_id) / "work"


def logs_dir(job_id) -> Path:
    return job_dir(job_id) / "logs"


def ensure_dirs(job_id) -> None:
    for d in (input_dir(job_id), work_dir(job_id), logs_dir(job_id)):
        d.mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_relpath(name: str) -> PurePosixPath:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise InputError(f"caminho inseguro no envio: {name!r}")
    return p


def save_file(dest_root: Path, relname: str, data: bytes) -> Path:
    rel = _safe_relpath(relname)
    dest = dest_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def extract_archive(data: bytes, filename: str, dest_root: Path) -> list[str]:
    """Extrai .tar.gz/.tgz/.tar/.zip com proteção contra path traversal e bombas."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise InputError("arquivo compactado grande demais")
    names: list[str] = []
    total = 0
    lower = filename.lower()
    if lower.endswith(".zip"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise InputError(f"zip inválido: {e}") from e
        members = [m for m in zf.infolist() if not m.is_dir()]
        if len(members) > MAX_ARCHIVE_FILES:
            raise InputError("arquivos demais no zip")
        for m in members:
            total += m.file_size
            if total > MAX_ARCHIVE_BYTES:
                raise InputError("conteúdo descompactado grande demais")
            save_file(dest_root, m.filename, zf.read(m))
            names.append(str(_safe_relpath(m.filename)))
    elif lower.endswith((".tar.gz", ".tgz", ".tar")):
        try:
            tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        except tarfile.TarError as e:
            raise InputError(f"tar inválido: {e}") from e
        members = tf.getmembers()
        if len(members) > MAX_ARCHIVE_FILES:
            raise InputError("arquivos demais no tar")
        for m in members:
            if m.isdir():
                continue
            if not m.isfile():  # links simbólicos, devices etc. são ignorados
                continue
            total += m.size
            if total > MAX_ARCHIVE_BYTES:
                raise InputError("conteúdo descompactado grande demais")
            f = tf.extractfile(m)
            save_file(dest_root, m.name, f.read() if f else b"")
            names.append(str(_safe_relpath(m.name)))
    else:
        raise InputError("formato de arquivo não suportado (use .tar.gz, .tgz, .tar ou .zip)")
    return names


def list_files(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            out.append(Path(dirpath, fn).relative_to(root).as_posix())
    return sorted(out)


def match_sources(root: Path, globs: list[str]) -> list[str]:
    """Aplica os globs do dft.yaml (com suporte a **) e devolve caminhos relativos ordenados."""
    files = list_files(root)
    matched: list[str] = []
    for g in globs:
        g = g.replace("\\", "/")
        hits = [f for f in files if fnmatch(f, g) or (g.startswith("**/") and fnmatch(f, g[3:]))]
        for h in hits:
            if h not in matched:
                matched.append(h)
    return matched


def hash_inputs(root: Path, sources: list[str], config_yaml: str) -> str:
    """Hash determinístico de fontes + dft.yaml: identifica entradas idênticas."""
    h = hashlib.sha256()
    h.update(config_yaml.encode())
    for rel in sorted(sources):
        h.update(b"\0" + rel.encode() + b"\0")
        h.update(sha256_file(root / rel).encode())
    return h.hexdigest()


def git_checkout(repo_url: str, commit: str, dest: Path, timeout: int = 300) -> None:
    """Clona o repositório e faz checkout do commit pedido (usado pelo worker)."""
    url = repo_url
    token = settings.DFT_GIT_TOKEN
    if token and url.startswith("https://"):
        url = url.replace("https://", f"https://x-access-token:{token}@", 1)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    git = settings.DFT_GIT_BIN

    def run(*args):
        r = subprocess.run([git, *args], cwd=dest, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).replace(token, "***") if token else (r.stderr or r.stdout)
            raise InputError(f"git {args[0]} falhou: {msg.strip()[-500:]}")

    run("init", "-q")
    run("remote", "add", "origin", url)
    # fetch só do commit pedido (servidores modernos permitem buscar por SHA)
    try:
        run("fetch", "-q", "--depth", "1", "origin", commit)
        run("checkout", "-q", "FETCH_HEAD")
    except InputError:
        run("fetch", "-q", "origin")
        run("checkout", "-q", commit)


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch(name, p) for p in patterns)
