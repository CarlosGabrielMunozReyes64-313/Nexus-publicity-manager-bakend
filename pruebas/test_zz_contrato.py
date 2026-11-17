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
    mappings={'empresas':'empresa','contactos':'contacto','segmentos':'segmento','secuencias':'secuencia','campanas':'campana','propuestas':'propuesta','respuestas':'respuesta','oportunidades':'oportunidad','integraciones':'integracion','supresiones':'supresion','duplicados':'duplicado','auditoria':'auditoria','descubrimiento/busquedas':'busqueda','informes/recomendaciones':'recomendacion','informes/metricas':'metrica'}
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
    for path,schema in {'configuracion/marca':'marca','configuracion/limites':'limites','configuracion/cliente-ideal':'clienteIdeal','panel/resumen':'resumen','embudo':'embudo','auth/session':'ok'}.items():
        res=auth.get('/api/'+path)
        assert res.status_code==200,(path,res.text)
        records.append({'ruta':path,'schema':schema,'array':False,'data':res.json()})
    out=Path('pruebas/salidas');out.mkdir(parents=True,exist_ok=True)
    (out/'contratos.json').write_text(json.dumps(records))
