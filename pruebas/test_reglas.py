from datetime import datetime
import pytest
from app.dominio.reglas import coincide,validar_secuencia,siguiente_ventana
from app.integraciones.hunter import traducir
from app.motor.plantillas import render


def test_segmentos():
    ct={'id':'c1','empresaId':'e1','etapa':'identificado','origen':'manual','baseLegal':{'aprobadaPorAdmin':False}}
    empresas=[{'id':'e1','municipio':'Palmira','tamano':'11-50','sector':'Servicios'}]
    for campo,operador,valor,expected in [('municipio','es','Palmira',True),('contactoIds','es','c2,c3',False),('municipio','contiene','PALM',True),('puntaje','mayor_que','-1',True),('puntaje','menor_que','1',True),('baseLegal','es','pendiente',True),('sector','no_es','Servicios',False)]:
        assert coincide(ct,empresas,[{'campo':campo,'operador':operador,'valor':valor}]) is expected


def test_validacion_y_render():
    s={'ventanaEnvio':{'dias':[1,2,3,4,5],'desde':'08:00','hasta':'17:00'},'pasos':[{'orden':1,'asunto':'Hola {{nombre}}','cuerpo':'Hola {{nombre}}, invitamos a {{empresa}}. {{firma}} {{baja}}','esperaDiasHabiles':0,'variables':[]}]}
    assert validar_secuencia(s,[])==[]
    r=render(s,{'nombre':'Ana','evidencias':[]},{'razonSocial':'Empresa sin verificar','evidencias':[]},{'firma':'NEXUS','baja':'Baja'},'https://app.example/baja/token','https://app.example/r/ref')
    assert r['pasos'][0]['asunto']=='Hola equipo'
    assert 'Empresa sin verificar' not in r['pasos'][0]['cuerpo']
    s['pasos'][0]['cuerpo']='Mensaje de prueba sin firma ni baja'
    assert len(validar_secuencia(s,[]))==2


def test_festivo_y_ventanas():
    v={'dias':[1,2,3,4,5],'desde':'08:00','hasta':'17:00'}
    # Monday November 16 2026 is a Colombian holiday.
    t=siguiente_ventana(datetime.fromisoformat('2026-11-13T16:00:00-05:00'),v,v,1)
    assert t.isoformat()=='2026-11-17T16:00:00-05:00'
    assert siguiente_ventana(datetime.fromisoformat('2026-11-13T18:00:00-05:00'),v,v).isoformat()=='2026-11-17T08:00:00-05:00'


@pytest.mark.parametrize('data,correo,estado',[({},'a@ejemplo.co','desconocido'),({'status':'valid'},'a@ejemplo.co','valido'),({'status':'valid'},'ventas@ejemplo.co','riesgoso'),({'status':'accept_all'},'a@ejemplo.co','catch_all'),({'status':'invalid'},'a@ejemplo.co','invalido')])
def test_hunter(data,correo,estado):
    assert traducir(data,correo)==estado
