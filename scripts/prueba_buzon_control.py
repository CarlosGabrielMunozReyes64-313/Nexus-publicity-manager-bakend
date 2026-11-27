"""Envío real a UN buzón de control autorizado, por el camino completo del producto.

Crea (o reutiliza) la empresa «Buzón de control NEXUS», un contacto con el correo indicado,
una lista estática «Buzón de control» con solo ese contacto y una secuencia de 1 correo con
la ventana del horario global. La aprueba y programa como lo haría el botón Aprobar. Desde
ahí el trabajador la envía por Gmail respetando ventana, cuotas y separación.

Nunca toca otros contactos: la audiencia es una lista con un único miembro. El correo de
control recibe la base legal «autorización registrada» porque usted declara con
--confirmo-buzon-de-control que es un buzón propio y autorizado.

Uso (en el servicio del backend, con las variables de producción):
  python scripts/prueba_buzon_control.py --correo control@su-dominio.com --confirmo-buzon-de-control
Luego abra la secuencia «Prueba buzón de control …» → pestaña Envíos.
"""

import argparse
import sys
from datetime import datetime

from app import db
from app.api import datos, listas, secuencias
from app.config import config
from app.db import transaccion, uno

p = argparse.ArgumentParser()
p.add_argument("--correo", required=True)
p.add_argument("--confirmo-buzon-de-control", action="store_true")
a = p.parse_args()
correo = "".join(a.correo.lower().split())
if not a.confirmo_buzon_de_control:
    sys.exit("Añada --confirmo-buzon-de-control: solo para un buzón propio y autorizado.")
db.abrir(config().database_url.get_secret_value())
try:
    with transaccion("admin", accion="prueba_buzon_control") as c:
        admin = uno(c, "SELECT id FROM administradores WHERE correo=%s", (config().admin_correo,))
        c.execute("SELECT set_config('nexus.administrador_id',%s,true)", (str(admin["id"]),))
        ct = uno(c, "SELECT * FROM contactos WHERE correo=%s", (correo,), False)
        if ct and (
            ct["excluido"] or uno(c, "SELECT contacto_bloqueado(%s) AS b", (ct["id"],))["b"]
        ):
            sys.exit("Ese correo está dado de baja o excluido: NEXUS no le escribirá.")
        if not ct:
            e = uno(
                c, "SELECT * FROM empresas WHERE razon_social='Buzón de control NEXUS'", (), False
            )
            if not e:
                e = datos.guardar_empresa(
                    c, {"razonSocial": "Buzón de control NEXUS", "municipio": ""}
                )
            ct = datos.guardar_contacto(
                c,
                {
                    "empresaId": str(e["id"]),
                    "nombre": "Buzón de control",
                    "correo": correo,
                    "origen": "manual",
                    "baseLegal": {
                        "tipo": "autorizacion_registrada",
                        "texto": "Buzón de control propio para verificar envíos",
                        "prueba": f"Declarado con --confirmo-buzon-de-control el {datetime.now():%Y-%m-%d}",
                    },
                },
            )
        cid = str(ct["id"])
        # Buzón propio: se marca válido y se aprueba su base legal sin esperar a Hunter.
        c.execute("UPDATE contactos SET estado_correo='valido' WHERE id=%s", (cid,))
        base = uno(c, "SELECT * FROM bases_legales WHERE contacto_id=%s AND vigente", (cid,), False)
        if not base or base["tipo"] == "pendiente":
            datos.legal(
                c,
                cid,
                {
                    "tipo": "autorizacion_registrada",
                    "texto": "Buzón de control propio para verificar envíos",
                    "prueba": "Declarado con --confirmo-buzon-de-control",
                },
            )
        if not (base and base["aprobada_en"]):
            datos.aprobar_legal(c, cid, str(admin["id"]))
        sello = datetime.now().strftime("%Y%m%d-%H%M")
        lst = listas.crear(c, {"nombre": f"Buzón de control {sello}", "contactoIds": [cid]})
        s = secuencias.nueva(
            c,
            {
                "nombre": f"Prueba buzón de control {sello}",
                "objetivo": "Verificar envío real",
                "llamadaAccion": "Ver diagnóstico",
                "listaId": lst["id"],
            },
        )
        lim = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
        b = secuencias.borrador(c, s["id"])["contenido"]
        b["ventanaEnvio"] = {k: lim["ventana"][k] for k in ("dias", "desde", "hasta")}
        b["pasos"][0]["cuerpo"] = (
            "Hola {{nombre}}:\n\nEste es un correo de control de NEXUS para verificar el envío "
            "real por Gmail. {{enlace}}\n\n{{firma}}\n{{baja}}"
        )
        secuencias.guardar_borrador(c, s["id"], b)
        r = secuencias.aprobar_secuencia(c, s["id"], str(admin["id"]), {"programar": True})
    prog = r.get("programacion") or {}
    print(f"Secuencia: {config().app_url}/secuencias/{s['id']} (pestaña Envíos)")
    print("Programada:" if prog.get("activada") else "NO programada:", prog)
finally:
    db.cerrar()
