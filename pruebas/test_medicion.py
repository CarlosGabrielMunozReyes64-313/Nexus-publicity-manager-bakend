from datetime import timedelta
from uuid import uuid4

from app.api.datos import ahora
from app.api.medicion import metricas
from app.db import transaccion,insertar,uno
from app.dominio.reglas import BOGOTA
from app.integraciones.analitica import AnaliticaFalsa,sincronizar,canal
from app.tareas.programador import programar
from test_base import client
from test_datos import auth


def test_metricas_costos_y_upsert(auth):
    day=ahora().astimezone(BOGOTA).date()-timedelta(days=1)
    fake=AnaliticaFalsa([{'fecha':day,'canal':'organico_busqueda','fuente':'google','medio':'organic','campana_utm':'','sesiones':12,'usuarios':10,'leads':2,'clics_whatsapp':1}],[])
    with transaccion() as c:
        sincronizar(c,'sincronizar_ga4',fake)
        sincronizar(c,'sincronizar_ga4',fake)
        insertar(c,'costos',{'fecha':day,'categoria':'otro','concepto':'Prueba','moneda':'USD','monto':2})
        insertar(c,'costos',{'fecha':day,'categoria':'otro','concepto':'Prueba COP','moneda':'COP','monto':4000})
        rows=metricas(c,{'desde':day.isoformat(),'hasta':day.isoformat()})
        assert sum(x['visitas'] for x in rows)==12
        assert sum(float(x['costoUSD'] or 0) for x in rows)==3
        assert sum(x['costoUSD'] is not None for x in rows)==1
        assert canal('Email','x')=='correo'
        assert canal('Organic Search','google')=='organico_busqueda'


def test_programador_no_duplica(auth):
    with transaccion() as c:
        t=ahora().replace(hour=8)
        programar(c,t)
        n=uno(c,'SELECT count(*) AS n FROM tareas')['n']
        programar(c,t)
        assert uno(c,'SELECT count(*) AS n FROM tareas')['n']==n
