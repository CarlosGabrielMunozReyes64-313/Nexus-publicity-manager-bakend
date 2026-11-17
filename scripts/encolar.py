"""Encola una tarea administrativa; no envía ni aprueba campañas."""
import argparse
import json
from app import db
from app.config import config
parser=argparse.ArgumentParser()
parser.add_argument('tipo',choices=['proponer_segmentos','proponer_campana','enriquecer_empresa','buscar_duplicados','calcular_puntaje','informe_semanal'])
parser.add_argument('--datos',default='{}',help='Objeto JSON, por ejemplo {"segmentoId":"UUID"}')
args=parser.parse_args()
db.abrir(config().database_url.get_secret_value())
try:
    with db.transaccion() as c:
        admin=db.uno(c,'SELECT id FROM administradores WHERE correo=%s AND activo',(config().admin_correo,))
    with db.transaccion('admin',str(admin['id']),'encolar_agente') as c:
        db.encolar(c,args.tipo,json.loads(args.datos))
        db.auditar(c,'encolar_'+args.tipo,'tareas','cola')
finally:
    db.cerrar()
