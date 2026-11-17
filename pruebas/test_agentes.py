import json
import socket
from uuid import uuid4

import pytest
from pydantic import ValidationError
from app.agentes.ejecutor import ClaudeFalso, ejecutar, costo
from app.agentes.lector_web import direccion, normalizar
from app.agentes.modelos import Clasificacion
from app.config import config
from app.db import transaccion,uno
from test_base import client
from test_datos import auth


def test_claude_estructurado_costos(auth,monkeypatch):
    cfg=config()
    monkeypatch.setattr(cfg,'claude_precios_json',json.dumps({cfg.claude_modelo_ligero:{'entrada':1,'salida':2}}))
    fake=ClaudeFalso({'clasificacion':'interesado','confianza':.91,'borrador':'Gracias por responder','nombres':[],'fechas':[]})
    r=ejecutar('analista',{'texto':'Me interesa'},Clasificacion,fake,ligero=True)
    assert r.clasificacion=='interesado'
    with transaccion() as c:
        row=uno(c,"SELECT * FROM ejecuciones_agente ORDER BY iniciada_en DESC LIMIT 1")
        assert row['estado']=='exito' and float(row['costo_usd'])==pytest.approx(.0003)
        assert uno(c,'SELECT count(*) AS n FROM mensajes')['n']==0
    invalid=ClaudeFalso({'clasificacion':'interesado','confianza':2,'borrador':'','nombres':[],'fechas':[]})
    with pytest.raises(ValidationError):
        ejecutar('analista',{},Clasificacion,invalid,ligero=True)
    monkeypatch.setattr(cfg,'tope_mensual_usd',0.00001)
    with pytest.raises(ValueError,match='mensual'):
        ejecutar('analista',{},Clasificacion,fake,ligero=True)


@pytest.mark.parametrize('ip',['127.0.0.1','10.1.2.3','169.254.169.254','192.168.1.1','::1','fc00::1','0.0.0.0'])
def test_ssrf(ip,monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))])
    with pytest.raises(ValueError):
        direccion('https://fuente.example')


@pytest.mark.parametrize('url',['file:///etc/passwd','https://user:pass@ejemplo.co','https://linkedin.com/company/x','https://ejemplo.co:8000'])
def test_urls_no_permitidas(url):
    with pytest.raises(ValueError):
        direccion(url)


def test_normalizacion_citas():
    assert normalizar('Cafe\u0301  del\n Valle')=='Café del Valle'
