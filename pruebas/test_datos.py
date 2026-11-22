from uuid import uuid4
import json
from pathlib import Path
import pytest
from app.config import config
from app.db import transaccion, uno
from test_base import client

@pytest.fixture
def auth(client):
    client.get('/api/auth/google')
    client.headers['Origin']=config().app_url
    return client

def test_empresa_contacto_legal_supresion(auth):
    dominio=f'{uuid4().hex}.example'
    b={'id':'nuevo','razonSocial':'Prueba SAS','municipio':'Palmira','sitioWeb':'https://www.'+dominio,'estadoCuenta':'cliente'}
    assert auth.post('/api/empresas',json=b).status_code==200
    assert auth.post('/api/empresas',json=b).status_code==409
    e=next(r for r in auth.get('/api/empresas').json() if r.get('dominio')==dominio)
    # El listado es liviano; las evidencias completas viven en la ficha de detalle.
    assert e['estadoCuenta']=='prospecto' and e['evidencias']==[]
    assert auth.get('/api/empresas/'+e['id']).json()['evidencias']
    body={'id':'nuevo','empresaId':e['id'],'nombre':'Ana','correo':' ANA@'+dominio.upper()+' ','estadoCorreo':'valido','baseLegal':{'tipo':'corporativo_generico','texto':'Publicado','prueba':'https://'+dominio,'aprobadaPorAdmin':True}}
    assert auth.post('/api/contactos',json=body).status_code==200
    cs=auth.get('/api/contactos').json()
    ct=next(r for r in cs if r['empresaId']==e['id'])
    assert ct['correo']=='ana@'+dominio
    assert ct['estadoCorreo']=='desconocido' and not ct['baseLegal']['aprobadaPorAdmin']
    assert auth.post(f"/api/contactos/{ct['id']}/base-legal/aprobar",json={}).status_code==200
    assert auth.get('/api/contactos/'+ct['id']).json()['baseLegal']['aprobadaPorAdmin']
    assert auth.post('/api/supresiones',json={'dominio':dominio,'motivo':'Solicitó baja'}).status_code==200
    assert auth.get('/api/contactos/'+ct['id']).json()['excluido']
    with transaccion() as c:
        assert uno(c,'SELECT count(*) AS n FROM tareas WHERE datos->>\'contactoId\'=%s',(ct['id'],))['n']>=1

def test_formatos_get(auth):
    routes={'empresas':'empresa','contactos':'contacto','integraciones':'integracion','supresiones':'supresion','auditoria':'auditoria','configuracion/marca':'marca','configuracion/limites':'limites','configuracion/cliente-ideal':'clienteIdeal','panel/resumen':'resumen','duplicados':'duplicado'}
    out=Path('pruebas/salidas');out.mkdir(parents=True,exist_ok=True)
    payload=[]
    for route,schema in routes.items():
        r=auth.get('/api/'+route)
        assert r.status_code==200,(route,r.text)
        payload.append({'ruta':route,'schema':schema,'array':isinstance(r.json(),list),'data':r.json()})
    (out/'contratos.json').write_text(json.dumps(payload))

def test_sin_sesion(client):
    for ruta in ('empresas','contactos','integraciones','auditoria','panel/resumen'):
        assert client.get('/api/'+ruta).status_code==401

def test_mutacion_atomica(auth):
    before=len(auth.get('/api/contactos').json())
    r=auth.post('/api/contactos/importar',json={'contactos':[{'empresaId':str(uuid4()),'nombre':'Inexistente'}]})
    assert r.status_code==404
    assert len(auth.get('/api/contactos').json())==before
