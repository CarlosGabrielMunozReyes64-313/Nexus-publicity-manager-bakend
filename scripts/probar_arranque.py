"""Prueba de arranque desde la estructura que recibe el usuario.

Extrae los dos ZIP (o copia dos carpetas) en una carpeta temporal tal como lo haría el
usuario, prepara .env con scripts/entorno_desarrollo.py, ejecuta las pruebas del backend y
del frontend sobre esa copia, compila el frontend, migra una base temporal, arranca la API
y comprueba /health, la aplicación y la API.

Uso:
  TEST_ADMIN_DATABASE_URL=postgresql://usuario:clave@localhost:5432/postgres \\
    python scripts/probar_arranque.py backend.zip frontend.zip [--conservar]

Requiere Node/npm y las dependencias de Python del backend instaladas en el entorno activo.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def extraer(origen: Path, destino: Path) -> Path:
    if origen.is_dir():
        copia = destino / origen.name
        shutil.copytree(
            origen,
            copia,
            ignore=shutil.ignore_patterns(
                "node_modules", "dist", ".env", ".venv", "__pycache__", "*.egg-info"
            ),
        )
        return copia
    with zipfile.ZipFile(origen) as z:
        raices = {n.split("/")[0] for n in z.namelist() if n.strip("/")}
        z.extractall(destino)
    if len(raices) != 1:
        raise SystemExit(
            f"{origen.name} debe contener una sola carpeta raíz; tiene {sorted(raices)}"
        )
    return destino / raices.pop()


def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def paso(texto: str) -> None:
    print(f"→ {texto}", flush=True)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    conservar = "--conservar" in sys.argv
    if len(args) != 2:
        print(__doc__)
        return 2
    admin_url = os.environ.get("TEST_ADMIN_DATABASE_URL")
    if not admin_url:
        raise SystemExit(
            "Defina TEST_ADMIN_DATABASE_URL (instancia de pruebas con permiso CREATEDB)"
        )
    trabajo = Path(tempfile.mkdtemp(prefix="nexus-arranque-"))
    params = conninfo_to_dict(admin_url)
    params["dbname"] = "postgres"
    admin = make_conninfo(**params)
    base = "nexus_arranque_" + uuid4().hex[:10]
    servidor: subprocess.Popen[bytes] | None = None
    fallos: list[str] = []
    try:
        paso(f"Extrayendo en {trabajo}")
        backend = extraer(Path(args[0]).resolve(), trabajo)
        frontend = extraer(Path(args[1]).resolve(), trabajo)
        print(f"   backend: {backend.name} · frontend: {frontend.name}")

        paso("Preparando .env y detectando el frontend")
        subprocess.run([sys.executable, "scripts/entorno_desarrollo.py"], cwd=backend, check=True)
        env_texto = (backend / ".env").read_text(encoding="utf-8")
        esperado = f"FRONTEND_DIR=../{frontend.name}"
        if esperado not in env_texto:
            fallos.append(f".env no contiene {esperado}")

        if shutil.which("docker"):
            paso("Validando docker compose config (sin construir)")
            r = subprocess.run(
                ["docker", "compose", "config"], cwd=backend, capture_output=True, text=True
            )
            if r.returncode or str(frontend) not in r.stdout:
                fallos.append(
                    "docker compose config no resuelve el contexto del frontend: "
                    + r.stderr.strip()[:300]
                )

        paso("Pruebas del backend sobre la copia extraída (pytest)")
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=backend,
            check=True,
            env={**os.environ, "TEST_ADMIN_DATABASE_URL": admin_url},
        )

        paso("Compilando el frontend (npm ci + build)")
        npm = shutil.which("npm") or "npm"
        env_web = {**os.environ, "VITE_USE_MOCKS": "false", "VITE_API_URL": "/api"}
        subprocess.run(
            [npm, "ci", "--no-audit", "--no-fund"], cwd=frontend, check=True, env=env_web
        )
        paso("Pruebas del frontend sobre la copia extraída (npm test)")
        subprocess.run([npm, "test"], cwd=frontend, check=True, env=env_web)
        subprocess.run([npm, "run", "build"], cwd=frontend, check=True, env=env_web)

        paso(f"Creando la base temporal {base} y migrando")
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(base)))
        database_url = (
            f"postgresql://{quote(params.get('user', ''), safe='')}:"
            f"{quote(params.get('password', ''), safe='')}@"
            f"{params.get('host', 'localhost')}:{params.get('port', '5432')}/{base}"
        )
        puerto = puerto_libre()
        env = {
            **os.environ,
            "DATABASE_URL": database_url,
            "APP_URL": f"http://127.0.0.1:{puerto}",
            "APP_ENV": "development",
        }
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=backend, check=True, env=env
        )

        paso("Arrancando la API")
        log = (trabajo / "api.log").open("wb")
        servidor = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:crear_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(puerto),
            ],
            cwd=backend,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{puerto}"
        for _ in range(150):
            try:
                if httpx.get(url + "/health", timeout=2, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        cliente = httpx.Client(base_url=url, timeout=10, trust_env=False, follow_redirects=False)
        comprobaciones = [
            ("/health", lambda r: r.status_code == 200 and r.json() == {"estado": "ok"}),
            ("/", lambda r: r.status_code == 200 and 'id="root"' in r.text),
            ("/configuracion", lambda r: r.status_code == 200 and 'id="root"' in r.text),
            ("/api/auth/session", lambda r: r.status_code == 401 and "message" in r.json()),
            ("/baja/enlace-invalido", lambda r: r.status_code == 400),
        ]
        indice = cliente.get("/").text
        recurso = next(
            (p.split('"')[0] for p in indice.split('src="')[1:] if p.startswith("/assets/")), None
        )
        if recurso:
            comprobaciones.append(
                (
                    recurso,
                    lambda r: (
                        r.status_code == 200 and "javascript" in r.headers.get("content-type", "")
                    ),
                )
            )
        else:
            fallos.append("index.html no referencia ningún recurso /assets/")
        for ruta, ok in comprobaciones:
            r = cliente.get(ruta)
            bien = ok(r)
            print(f"   {'✓' if bien else '✗'} GET {ruta} → {r.status_code}")
            if not bien:
                fallos.append(f"GET {ruta} respondió {r.status_code}")
    finally:
        if servidor:
            servidor.terminate()
            servidor.wait(timeout=15)
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(base))
            )
        if conservar:
            print(f"Carpeta conservada: {trabajo}")
        else:
            shutil.rmtree(trabajo, ignore_errors=True)
    if fallos:
        print("\n✗ Arranque con problemas:\n  - " + "\n  - ".join(fallos))
        return 1
    print(
        "\n✓ Arranque correcto desde la estructura de los ZIP (frontend compilado, migraciones, API y aplicación)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
