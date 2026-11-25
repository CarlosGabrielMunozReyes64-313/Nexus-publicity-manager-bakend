import json
from pathlib import Path
from uuid import uuid4

from psycopg.types.json import Jsonb
from app.db import transaccion,insertar,uno
from test_base import client
from test_datos import auth


def test_todas_las_lecturas_con_datos(auth):
    with transaccion() as c:
        ct=uno(c,'SELECT * FROM contactos LIMIT 1')
        b=insertar(c,'busquedas_descubrimiento',{'consulta':'Empresas de prueba','municipios':['Palmira'],'sectores':['Servicios']})
        r=insertar(c,'resultados_descubrimiento',{'busqueda_id':b['id'],'razon_social':'Empresa hallada','municipio':'Palmira'})
        insertar(c,'evidencias',{'resultado_id':r['id'],'campo':'sector','valor':None,'fuente':'manual','estado':'pendiente'})
        insertar(c,'personas_descubiertas',{'resultado_id':r['id'],'nombre':'Persona ficticia','fuente_url':'https://empresa.example'})
        insertar(c,'respuestas',{'contacto_id':ct['id'],'gmail_message_id':uuid4().hex,'gmail_thread_id':uuid4().hex,'remitente':ct['correo'] or 'persona@example.com','recibida_en':ct['creado_en'],'texto':'Respuesta sin campaña'})
        insertar(c,'oportunidades',{'empresa_id':ct['empresa_id'],'etapa':'discovery'})
        insertar(c,'recomendaciones',{'texto':'Muestra insuficiente'})
        insertar(c,'puntajes',{'empresa_id':ct['empresa_id'],'total':0,'incompleto':True,'criterios':Jsonb([{'nombre':'Criterio','cumple':None,'peso':30,'explicacion':'Sin fuente'}]),'version_reglas':1})
        insertar(c,'interacciones',{'contacto_id':ct['id'],'tipo':'nota','resumen':'Nota de prueba'})
    mappings={'empresas':'empresa','contactos':'contacto','segmentos':'segmento','secuencias':'secuencia','campanas':'campana','propuestas':'propuesta','respuestas':'respuesta','oportunidades':'oportunidad','integraciones':'integracion','supresiones':'supresion','duplicados':'duplicado','auditoria':'auditoria','descubrimiento/busquedas':'busqueda','informes/recomendaciones':'recomendacion','informes/metricas':'metrica','agentes/solicitudes':'solicitudAgente','propuestas/solicitudes':'solicitudRuta','propuestas/rutas-correo/segmentos':'segmentoRuta','solicitudes-acceso':'solicitudAcceso'}
    records=[]
    for path,schema in mappings.items():
        res=auth.get('/api/'+path)
        assert res.status_code==200,(path,res.text)
        records.append({'ruta':path,'schema':schema,'array':True,'data':res.json()})
        if res.json() and path in ('empresas','contactos','segmentos','secuencias','campanas','propuestas','respuestas','descubrimiento/busquedas'):
            ident=res.json()[0]['id']
            detail=auth.get('/api/'+path+'/'+ident)
            assert detail.status_code==200,(path,detail.text)
            records.append({'ruta':path+'/'+ident,'schema':schema,'array':False,'data':detail.json()})
            if path in ('contactos','campanas'):
                suffix='interacciones' if path=='contactos' else 'inscripciones'
                detail=auth.get('/api/'+path+'/'+ident+'/'+suffix)
                assert detail.status_code==200
                records.append({'ruta':path+'/'+ident+'/'+suffix,'schema':'interaccion' if path=='contactos' else 'inscripcion','array':True,'data':detail.json()})
    for path,schema in {'configuracion/marca':'marca','configuracion/limites':'limites','configuracion/cliente-ideal':'clienteIdeal','configuracion/cumplimiento':'cumplimiento','descubrimiento/meta':'metaDescubrimiento','terminal':'terminal','panel/resumen':'resumen','embudo':'embudo','embudo?desde=2026-01-01&hasta=2026-12-31':'embudo','auth/session':'sesion','notificaciones':'bandejaNotificaciones','auditoria/pagina?tamano=5&accion=a':'paginaAuditoria'}.items():
        res=auth.get('/api/'+path)
        assert res.status_code==200,(path,res.text)
        records.append({'ruta':path,'schema':schema,'array':False,'data':res.json()})
    # Rutas de correo: revisión y contactos elegibles de la última ruta propuesta.
    rutas=[x for x in auth.get('/api/propuestas').json() if x['tipo']=='campana']
    assert rutas, 'Se requiere al menos una ruta de correo (la crean las pruebas de rutas)'
    for ruta in rutas[:3]:
        for suffix,schema in (('revision','revisionRuta'),('elegibles','contactoElegible')):
            res=auth.get('/api/propuestas/'+ruta['id']+'/'+suffix)
            assert res.status_code==200,(suffix,res.text)
            records.append({'ruta':'propuestas/'+ruta['id']+'/'+suffix,'schema':schema,'array':suffix=='elegibles','data':res.json()})
    # Editor de secuencias: lista (también archivadas), solicitudes y lecturas por secuencia.
    for path in ('secuencias?archivadas=true','secuencias/solicitudes'):
        res=auth.get('/api/'+path)
        assert res.status_code==200,(path,res.text)
        records.append({'ruta':path,'schema':'secuencia' if path.startswith('secuencias?') else 'solicitudSecuencia','array':True,'data':res.json()})
    ids=[x['id'] for x in auth.get('/api/secuencias').json()][:3]
    with transaccion() as c:
        con_sugerencias=uno(c,"SELECT datos->>'secuenciaId' AS id FROM tareas WHERE tipo='reescribir_correo' AND datos ? 'resultado' AND NOT (datos ? 'descartada') ORDER BY creada_en DESC LIMIT 1",(),False)
    assert con_sugerencias, 'Se requiere una sugerencia de «Mejorar con IA» (la crean las pruebas del editor)'
    ids.append(con_sugerencias['id'])
    for ident in ids:
        for suffix,schema,array in (('borrador','borrador',False),('versiones','versionSecuencia',True),('revision','revisionSecuencia',False),('elegibles','contactoElegible',True),('sugerencias','sugerencia',True)):
            res=auth.get(f'/api/secuencias/{ident}/{suffix}')
            assert res.status_code==200,(suffix,res.text)
            records.append({'ruta':f'secuencias/{ident}/{suffix}','schema':schema,'array':array,'data':res.json()})
            if suffix=='elegibles' and res.json():
                previa=auth.post(f'/api/secuencias/{ident}/previsualizar',json={'contactoId':res.json()[0]['id']})
                assert previa.status_code==200,previa.text
                records.append({'ruta':f'secuencias/{ident}/previsualizar','schema':'vistaPrevia','array':False,'data':previa.json()})
    assert any(r['schema']=='sugerencia' and r['data'] for r in records)
    out=Path('pruebas/salidas');out.mkdir(parents=True,exist_ok=True)
    (out/'contratos.json').write_text(json.dumps(records))
