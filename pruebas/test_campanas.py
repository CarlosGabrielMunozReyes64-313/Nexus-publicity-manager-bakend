from uuid import uuid4
import json
from pathlib import Path
from app.db import transaccion,insertar,uno
from app.api.campanas import nueva_propuesta
from app.api.lecturas import secuencia
from app.esquemas.serializar import serializar
from test_datos import auth
from test_base import client


def test_aprobacion_versionado(auth):
    with transaccion('admin') as c:
        seg=insertar(c,'segmentos',{'nombre':'Segmento prueba','propuesto_por':'admin'})
        seq=insertar(c,'secuencias',{'nombre':'Secuencia prueba','objetivo':'Diagnóstico','llamada_accion':'Revisar'})
        insertar(c,'pasos',{'secuencia_id':seq['id'],'orden':1,'asunto':'Hola','cuerpo':'Hola {{nombre}}, le invitamos al diagnóstico. {{firma}} {{baja}}'})
        cam=insertar(c,'campanas',{'nombre':'Campaña prueba','segmento_id':seg['id'],'secuencia_id':seq['id'],'estado':'en_revision'})
        prop=nueva_propuesta(c,'estratega_redactor','campana','Prueba de campaña',serializar(secuencia(c,seq)),{'objetivo_campana_id':cam['id']})
    assert auth.post(f"/api/segmentos/{seg['id']}/aprobar",json={}).status_code==200
    assert auth.post(f"/api/propuestas/{prop['id']}/aprobar",json={}).status_code==200
    r=auth.get(f"/api/campanas/{cam['id']}").json()
    assert r['estado']=='aprobada' and len(r['huellaAprobada'])==64
    assert auth.post(f"/api/propuestas/{prop['id']}/aprobar",json={}).status_code==400
    assert auth.post(f"/api/campanas/{cam['id']}/reanudar",json={}).status_code==400
    s=auth.get(f"/api/secuencias/{seq['id']}").json()
    s['pasos'][0]['cuerpo']+=' Texto actualizado.'
    r=auth.put(f"/api/secuencias/{seq['id']}",json=s)
    assert r.status_code==200,r.text
    # Guardar ahora crea un borrador: lo aprobado y vigente no cambia hasta aprobarlo.
    antes=auth.get(f"/api/campanas/{cam['id']}").json()
    assert antes['estado']=='aprobada' and len(antes['huellaAprobada'])==64
    assert 'Texto actualizado' not in auth.get(f"/api/secuencias/{seq['id']}").json()['pasos'][0]['cuerpo']
    p=auth.get(f"/api/propuestas/{prop['id']}").json()
    assert p['estado']=='pendiente' and p['version']==2
    assert auth.post(f"/api/propuestas/{prop['id']}/aprobar",json={}).status_code==200
    despues=auth.get(f"/api/campanas/{cam['id']}").json()
    assert despues['estado']=='aprobada' and despues['huellaAprobada']!=antes['huellaAprobada']
    assert 'Texto actualizado' in auth.get(f"/api/secuencias/{seq['id']}").json()['pasos'][0]['cuerpo']
    out=Path('pruebas/salidas/contratos.json')
    out.parent.mkdir(parents=True,exist_ok=True)
    records=json.loads(out.read_text()) if out.exists() else []
    for resource,schema,ident in [('segmentos','segmento',seg['id']),('secuencias','secuencia',seq['id']),('campanas','campana',cam['id']),('propuestas','propuesta',prop['id'])]:
        for path in (resource,resource+'/'+str(ident)):
            response=auth.get('/api/'+path)
            assert response.status_code==200
            records.append({'ruta':path,'schema':schema,'array':isinstance(response.json(),list),'data':response.json()})
    out.write_text(json.dumps(records))


def test_reglas_y_correo_inferido(auth):
    assert auth.post('/api/segmentos/previsualizar',json=[{'campo':'noexiste','operador':'es','valor':'x'}]).status_code==400
    assert auth.post('/api/segmentos/previsualizar',json=[]).status_code==200
    with transaccion() as c:
        p=nueva_propuesta(c,'investigador','correo_inferido','Correo deducido',{})
    assert auth.post(f"/api/propuestas/{p['id']}/aprobar",json={}).status_code==400
