import http.client
import ipaddress
import socket
import ssl
import threading
import time
import unicodedata
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from app.config import config

BLOQUEADOS = {"linkedin.com", "facebook.com", "instagram.com", "tiktok.com"}
_lock = threading.Lock()
_ultimo: dict[str, float] = {}


def direccion(url: str) -> tuple[str, str, int, str]:
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise ValueError("URL no permitida")
    host = u.hostname.lower().rstrip(".")
    if any(host == d or host.endswith("." + d) for d in BLOQUEADOS):
        raise ValueError("Dominio no permitido")
    port = u.port or (443 if u.scheme == "https" else 80)
    if port not in (80, 443):
        raise ValueError("Puerto no permitido")
    ips = {str(r[4][0]) for r in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("Dirección privada o reservada bloqueada")
    return host, sorted(ips)[0], port, u.scheme


class ConexionHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int) -> None:
        super().__init__(host, port, timeout=15, context=ssl.create_default_context())
        self.ip = ip
        self.contexto_ssl = ssl.create_default_context()

    def connect(self) -> None:
        sock = socket.create_connection((self.ip, self.port), self.timeout)
        self.sock = self.contexto_ssl.wrap_socket(sock, server_hostname=self.host)


def obtener(url: str, robots: bool = True, redirects: int = 0) -> str:
    if redirects > 3:
        raise ValueError("Demasiadas redirecciones")
    host, ip, port, scheme = direccion(url)
    agent = "NexusPublicity/1.0 (contacto: " + config().app_url + ")"
    if robots:
        rules = RobotFileParser()
        robot_url = f"{scheme}://{host}/robots.txt"
        try:
            rules.parse(obtener(robot_url, robots=False).splitlines())
        except FileNotFoundError:
            rules.parse([])
        if not rules.can_fetch(agent, url):
            raise ValueError("robots.txt no permite la consulta")
    with _lock:
        wait = 2 - (time.monotonic() - _ultimo.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        _ultimo[host] = time.monotonic()
    u = urlsplit(url)
    conn: http.client.HTTPConnection
    if scheme == "https":
        conn = ConexionHTTPS(host, ip, port)
    else:
        conn = http.client.HTTPConnection(ip, port, timeout=15)
    started = time.monotonic()
    try:
        conn.request(
            "GET",
            (u.path or "/") + ("?" + u.query if u.query else ""),
            headers={
                "Host": host,
                "User-Agent": agent,
                "Accept": "text/html,text/plain",
                "Accept-Encoding": "identity",
            },
        )
        response = conn.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            return obtener(
                urljoin(url, response.getheader("Location") or ""), robots, redirects + 1
            )
        if response.status == 404:
            raise FileNotFoundError("Fuente no encontrada")
        if response.status != 200:
            raise ValueError("La fuente no está disponible")
        if response.getheader("Content-Type", "").split(";")[0] not in ("text/html", "text/plain"):
            raise ValueError("Tipo de contenido no permitido")
        chunks = []
        size = 0
        while True:
            if time.monotonic() - started > 15:
                raise TimeoutError("La lectura excedió 15 segundos")
            chunk = response.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > 2 * 1024 * 1024:
                raise ValueError("Fuente de más de 2 MB")
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        conn.close()


def leer(url: str) -> str:
    soup = BeautifulSoup(obtener(url), "html.parser")
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    return soup.get_text(" ", strip=True)


def normalizar(texto: str) -> str:
    return " ".join(unicodedata.normalize("NFC", texto).split())


def verificar_cita(url: str, cita: str) -> bool:
    return bool(cita.strip()) and normalizar(cita) in normalizar(leer(url))


def leer_empresa(url: str) -> list[dict[str, str]]:
    host = urlsplit(url).hostname
    root = urlsplit(url)
    base = f"{root.scheme}://{root.netloc}/"
    candidatos = [url] + [
        urljoin(base, p)
        for p in (
            "nosotros",
            "servicios",
            "clientes",
            "contacto",
            "empresa",
            "sostenibilidad",
            "rse",
            "about",
            "contact",
        )
    ]
    paginas = []
    for candidate in dict.fromkeys(candidatos):
        if urlsplit(candidate).hostname != host:
            continue
        try:
            texto = leer(candidate)
            paginas.append({"url": candidate, "texto": texto[:12000]})
        except (ValueError, OSError):
            continue
    if not paginas:
        raise ValueError("Sin fuente: no fue posible consultar el sitio de la empresa")
    return paginas
