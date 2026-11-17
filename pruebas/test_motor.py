from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from uuid import uuid4
import base64
import hashlib
import hmac
import json
import time

from app.api.campanas import nueva_propuesta,aprobar
from app.api.lecturas import secuencia
from app.config import config
from app.db import transaccion,insertar,uno,todos,actualizar
from app.esquemas.serializar import serializar
from app.integraciones.gmail import GmailFalso
from app.motor.envios import programar,enviar,RelojFalso
from app.motor.rastreador import registrar
from app.publico.rutas import firma_baja
from app.seguridad.auth import cifrar
from psycopg.types.json import Jsonb
from test_base import client
from test_datos import auth


def test_mvp_50_contactos(auth,monkeypatch):
    reloj=RelojFalso(datetime.fromisoformat('2026-11-17T09:00:00-05:00'))
    gmail=GmailFalso()
    ids=[]
    with transaccion('admin') as c:
        admin=str(uno(c,'SELECT id FROM administradores WHERE correo=%s',(config().admin_correo,))['id'])
        c.execute("SELECT set_config('nexus.administrador_id',%s,true)",(admin,))
        c.execute("UPDATE campanas SET estado='pausada' WHERE estado='activa'")
        c.execute("UPDATE inscripciones SET estado='pausada',proximo_envio=NULL WHERE estado='activa'")
        e=insertar(c,'empresas',{'razon_social':'Prueba 50','municipio':'Palmira'})
        for i in range(50):
            ct=insertar(c,'contactos',{'empresa_id':e['id'],'nombre':f'Ficticio {i}','correo':f'persona@{uuid4().hex}.example','estado_correo':'valido','origen':'manual'})
            insertar(c,'bases_legales',{'contacto_id':ct['id'],'tipo':'autorizacion_registrada','texto':'Consentimiento ficticio','prueba':'Fixture de prueba','aprobada_por':admin,'aprobada_en':reloj.ahora()})
            ids.append(str(ct['id']))
        seg=insertar(c,'segmentos',{'nombre':'50 ficticios','reglas':Jsonb([{'campo':'contactoIds','operador':'es','valor':','.join(ids)}]),'propuesto_por':'admin','estado':'aprobado','aprobado_por':admin,'aprobado_en':reloj.ahora()})
        seq=insertar(c,'secuencias',{'nombre':'MVP','objetivo':'Diagnóstico','llamada_accion':'Ver diagnóstico'})
        for n in (1,2):
            insertar(c,'pasos',{'secuencia_id':seq['id'],'orden':n,'asunto':'Diagnóstico' if n==1 else None,'cuerpo':'Hola {{nombre}}, le invitamos a {{enlace}}. {{firma}} {{baja}}','espera_dias_habiles':1 if n==2 else 0})
        cam=insertar(c,'campanas',{'nombre':'MVP 50','segmento_id':seg['id'],'secuencia_id':seq['id'],'estado':'en_revision','tope_diario':50})
        prop=nueva_propuesta(c,'estratega_redactor','campana','Prueba MVP 50',serializar(secuencia(c,seq)),{'objetivo_campana_id':cam['id']})
        aprobar(c,prop,admin)
        actualizar(c,'campanas',str(cam['id']),{'estado':'activa'})
        for cid in ids:
            insertar(c,'inscripciones',{'campana_id':cam['id'],'contacto_id':cid,'estado':'activa','proximo_envio':reloj.ahora()})
        lim=uno(c,"SELECT valor FROM configuracion WHERE clave='limites'")['valor']
        lim.update(topeDiarioNuevos=50,topePorDominio=2,separacionSegundos=[1,1])
        c.execute("UPDATE configuracion SET valor=%s WHERE clave='limites'",(Jsonb(lim),))
        actualizar(c,'integraciones','gmail',{'estado':'conectada','credenciales_cifradas':cifrar('falso')})
    programar(reloj,gmail)
    with transaccion() as c:
        msgs=todos(c,'SELECT m.* FROM mensajes m JOIN inscripciones i ON i.id=m.inscripcion_id WHERE i.campana_id=%s ORDER BY programado_para',(cam['id'],))
    assert len(msgs)==50
    assert len({m['programado_para'] for m in msgs})==50
    # Pausing cancels the reservation; resuming reuses its stable identity.
    assert auth.post(f"/api/campanas/{cam['id']}/pausar",json={}).status_code==200
    assert auth.post(f"/api/campanas/{cam['id']}/reanudar",json={}).status_code==200
    programar(reloj,gmail)
    with transaccion() as c:
        regenerated=todos(c,'SELECT m.* FROM mensajes m JOIN inscripciones i ON i.id=m.inscripcion_id WHERE i.campana_id=%s ORDER BY programado_para',(cam['id'],))
    assert {m['id'] for m in regenerated}=={m['id'] for m in msgs}
    msgs=regenerated
    reloj.instante+=timedelta(hours=1)
    # Simulate provider acceptance followed by process loss before DB confirmation.
    from app.integraciones.gmail import mime
    with transaccion() as c:
        actualizar(c,'mensajes',str(msgs[0]['id']),{'estado':'enviando'})
    gmail.enviar(mime(msgs[0],{'nombre':'NEXUS'},'https://app.example/baja/token'),None)
    # Two workers receive the same send request: lock + state prevents duplication.
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _:enviar(str(msgs[0]['id']),reloj,gmail),range(2)))
    enviar(str(msgs[1]["id"]),reloj,gmail)
    assert gmail.llamadas==1  # Delayed workers must respect actual-send spacing.
    for m in msgs[1:]:
        reloj.instante+=timedelta(seconds=2)
        enviar(str(m['id']),reloj,gmail)
    assert gmail.llamadas==50
    with transaccion() as c:
        first=uno(c,'SELECT * FROM mensajes WHERE id=%s',(msgs[0]['id'],))
        gm={'id':'respuesta-'+uuid4().hex,'threadId':first['gmail_thread_id'],'payload':{'mimeType':'text/plain','headers':[{'name':'From','value':first['destinatario']}],'body':{'data':base64.urlsafe_b64encode(b'Me interesa').decode()}}}
        assert registrar(c,gm)
        insertar(c,'eventos_correo',{'tipo':'rebote_duro','contacto_id':msgs[1]['contacto_id']})
    token=firma_baja().dumps(str(msgs[2]['id']))
    assert auth.get('/baja/'+token).status_code==200
    with transaccion() as c:
        assert not uno(c,'SELECT excluido FROM contactos WHERE id=%s',(msgs[2]['contacto_id'],))['excluido']
    assert auth.post('/baja/'+token).status_code==200
    assert auth.post('/baja/'+token).status_code==200
    reloj.instante+=timedelta(days=1)
    programar(reloj,gmail)
    reloj.instante+=timedelta(hours=1)
    with transaccion() as c:
        follow=todos(c,"SELECT m.* FROM mensajes m JOIN inscripciones i ON i.id=m.inscripcion_id WHERE i.campana_id=%s AND m.paso_orden=2 AND m.estado='programado'",(cam['id'],))
    assert len(follow)==47
    assert not ({m['contacto_id'] for m in follow}&{x['contacto_id'] for x in msgs[:3]})
    for m in follow:
        reloj.instante+=timedelta(seconds=2)
        enviar(str(m['id']),reloj,gmail)
        enviar(str(m['id']),reloj,gmail)
    assert gmail.llamadas==97
    with transaccion() as c:
        met=uno(c,'SELECT * FROM v_metricas_campana WHERE campana_id=%s',(cam['id'],))
        assert met['enviados']==97 and met['respuestas']==1
        actualizar(c,'integraciones','gmail',{'estado':'desconectada','credenciales_cifradas':None})


def test_webhook_y_baja(auth):
    b={'id':uuid4().hex,'formulario':'diagnostico','nombre':'Persona ficticia','correo':'persona@'+uuid4().hex+'.example','empresa':'Empresa formulario','municipio':'Cali','enviadoEn':'2026-11-17T09:00:00-05:00','autorizacion':{'aceptada':True,'texto':'Autorización ficticia','versionPolitica':'v1'},'respuestas':{}}
    raw=json.dumps(b).encode()
    fecha=str(int(time.time()))
    secret=config().formulario_webhook_secreto.get_secret_value()
    headers={'X-Nexus-Fecha':fecha,'X-Nexus-Firma':hmac.new(secret.encode(),fecha.encode()+b'.'+raw,hashlib.sha256).hexdigest(),'Content-Type':'application/json'}
    assert auth.post('/api/webhooks/formulario',content=raw,headers=headers).status_code==200
    assert auth.post('/api/webhooks/formulario',content=raw,headers=headers).status_code==200
    assert auth.post('/api/webhooks/formulario',content=raw,headers={**headers,'X-Nexus-Firma':'falso'}).status_code==401
    with transaccion() as c:
        assert uno(c,'SELECT count(*) AS n FROM envios_formulario WHERE id_externo=%s',(b['id'],))['n']==1
        ct=uno(c,'SELECT * FROM contactos WHERE correo=%s',(b['correo'],))
        legal=uno(c,'SELECT * FROM bases_legales WHERE contacto_id=%s AND vigente',(ct['id'],))
        assert legal['aprobada_en'] is None and ct['etapa']=='lead'
    assert auth.get('/baja/token-no-valido').status_code==200
    assert auth.post('/baja/token-no-valido').status_code==200
