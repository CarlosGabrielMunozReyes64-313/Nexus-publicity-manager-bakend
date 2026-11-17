import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Config, config
from app.db import transaccion, uno
from app.esquemas.serializar import serializar
from app.main import crear_app


@pytest.fixture
def client():
    config.cache_clear()
    with TestClient(crear_app(), base_url=config().app_url, follow_redirects=False) as c:
        yield c


def test_sesion_y_logout(client):
    assert client.get('/api/auth/session').status_code == 401
    r = client.get('/api/auth/google')
    assert r.status_code == 302
    assert 'HttpOnly' in r.headers['set-cookie']
    token = client.cookies.get('nexus_sesion')
    assert len(token) >= 43
    with transaccion() as c:
        row = uno(c, 'SELECT * FROM sesiones WHERE token_hash=%s', (hashlib.sha256(token.encode()).digest(),))
        assert row['expira_en'] > row['creada_en']
    assert client.get('/api/auth/session').json() == {'ok': True}
    assert client.get('/api/auth/logout').status_code == 302
    assert client.get('/api/auth/session').status_code == 401


def test_csrf(client):
    assert client.post('/api/no-existe', json={}).status_code == 403
    assert client.post('/api/no-existe', json={}, headers={'Origin':'https://otro.example'}).status_code == 403


def test_oauth_state_rechazado(client):
    r = client.get('/api/auth/google/callback?state=falso&code=falso')
    assert r.status_code == 302
    assert 'acceso_denegado' in r.headers['location']


def test_produccion_prohibe_bypass():
    values = config().model_dump()
    values.update(app_env='production', app_url='https://app.example', auth_dev_bypass=True)
    with pytest.raises(ValidationError):
        Config(**values)


def test_serializador():
    r = serializar({'id':uuid4(), 'campo_opcional':None, 'costoUSD':None, 'evidencias':[{'valor':None}], 'criterios':[{'cumple':None}], 'creado_en':datetime.now(timezone.utc)})
    assert 'campoOpcional' not in r
    assert r['costoUSD'] is None and r['evidencias'][0]['valor'] is None
    assert r['criterios'][0]['cumple'] is None
    assert r['creadoEn'].endswith('+00:00')


def test_frontend_real(client):
    assert client.get('/login').status_code == 200
    assert 'root' in client.get('/contactos').text
    client.get('/api/auth/google')
    assert client.get('/api/no-existe').status_code == 404
