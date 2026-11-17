from app import db
from app.config import config
from app.demo import cargar
db.abrir(config().database_url.get_secret_value())
try:
    with db.transaccion('sistema',accion='cargar_demo') as c:
        cargar(c)
finally:
    db.cerrar()
