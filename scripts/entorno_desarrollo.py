"""Prepara el entorno local: .env con claves aleatorias y ubicación del frontend.

- Crea .env desde .env.example (no sobrescribe un .env existente) y restringe sus permisos.
- Detecta la carpeta del frontend junto a este backend, con el nombre que deja el ZIP
  (Nexus-publicity-manager-main), el de un clon de Git o cualquier carpeta cuyo
  package.json se llame «nexus-publicity-manager», y escribe FRONTEND_DIR y FRONTEND_DIST.
"""

import base64
import json
import os
import re
import secrets
import sys
from pathlib import Path, PurePath

RAIZ = Path(__file__).resolve().parents[1]
ENV = RAIZ / ".env"


def es_frontend(carpeta: Path) -> bool:
    try:
        datos = json.loads((carpeta / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return datos.get("name") == "nexus-publicity-manager"


def detectar_frontend() -> Path | None:
    candidatos: list[Path] = []
    if os.environ.get("FRONTEND_DIR"):
        candidatos.append(Path(os.environ["FRONTEND_DIR"]))
    candidatos += [
        RAIZ / "frontend",
        RAIZ.parent / "Nexus-publicity-manager-main",
        RAIZ.parent / "Nexus-publicity-manager",
        RAIZ.parent / "Nexus-publicity-manager-front",
    ]
    try:
        candidatos += sorted(p for p in RAIZ.parent.iterdir() if p.is_dir() and p != RAIZ)
    except OSError:
        pass
    for candidato in candidatos:
        ruta = candidato if candidato.is_absolute() else RAIZ / candidato
        if ruta.is_dir() and es_frontend(ruta):
            return ruta.resolve()
    return None


def relativa(ruta: Path) -> str:
    # Barras normales: válidas para Docker Compose en Linux, macOS y Windows.
    return PurePath(os.path.relpath(ruta, RAIZ)).as_posix()


def fijar(texto: str, clave: str, valor: str) -> str:
    linea = f"{clave}={valor}"
    if re.search(rf"^{clave}=.*$", texto, flags=re.M):
        return re.sub(rf"^{clave}=.*$", linea, texto, flags=re.M)
    return texto.rstrip("\n") + "\n" + linea + "\n"


def main() -> int:
    frontend = detectar_frontend()
    if ENV.exists():
        texto = ENV.read_text(encoding="utf-8")
        creado = False
    else:
        texto = (RAIZ / ".env.example").read_text(encoding="utf-8")
        valores = {
            "CLAVE_CIFRADO": base64.urlsafe_b64encode(os.urandom(32)).decode(),
            "SESION_SECRETO": secrets.token_urlsafe(48),
            "DB_OWNER_PASSWORD": secrets.token_urlsafe(24),
            "DB_APP_PASSWORD": secrets.token_urlsafe(24),
            "FORMULARIO_WEBHOOK_SECRETO": secrets.token_urlsafe(32),
        }
        for clave, valor in valores.items():
            texto = re.sub(rf"^{clave}=$", f"{clave}={valor}", texto, flags=re.M)
        texto = texto.replace(
            "nexus_app:CAMBIAR@localhost", "nexus_app:" + valores["DB_APP_PASSWORD"] + "@localhost"
        )
        creado = True
    if frontend:
        texto = fijar(texto, "FRONTEND_DIR", relativa(frontend))
        texto = fijar(texto, "FRONTEND_DIST", relativa(frontend / "dist"))
    ENV.write_text(texto, encoding="utf-8")
    try:
        ENV.chmod(0o600)
    except OSError:
        pass
    print(".env creado." if creado else ".env existente: solo se actualizó la ruta del frontend.")
    if frontend:
        print(f"Frontend detectado en {frontend} (FRONTEND_DIR={relativa(frontend)}).")
    else:
        print(
            "AVISO: no se encontró el frontend. Extraiga Nexus-publicity-manager-main.zip en la "
            "misma carpeta que este backend o defina FRONTEND_DIR y FRONTEND_DIST en .env.",
            file=sys.stderr,
        )
    print("Revise ADMIN_CORREO y las integraciones antes de producción.")
    return 0 if frontend else 1


if __name__ == "__main__":
    raise SystemExit(main())
