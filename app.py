# --- app.py ( Chatbot-Quirofanos para Medicos: Version 0.0.1) ---

# from doctest import NORMALIZE_WHITESPACE
import json
import locale
import os
#import re

# from smtplib import SMTP_PORT
import threading
import time
import unicodedata
import zlib
from collections import deque
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request
from thefuzz import process

try:
    locale.setlocale(locale.LC_TIME, "es_ES.UTF-8")
except (locale.Error, Exception):
    try:
        locale.setlocale(locale.LC_TIME, "Spanish_Spain.1252")
    except (locale.Error, Exception):
        print("ADVERTENCIA: Locale en español no encontrado.")

load_dotenv()
app = Flask(__name__)

EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "").rstrip("/")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY")

# DEMO_MODE=1 simula ÚNICAMENTE los endpoints listados en DEMO_ENDPOINTS, que
# son los que todavía no están operativos en LOLCLI. Todo lo demás sale del
# sistema real. Al quedar los dos pendientes operativos, basta con DEMO_MODE=0.
DEMO_MODE = os.getenv("DEMO_MODE", "0") == "1"

# Estado verificado contra http://15.235.105.124:3011/LolcliApi/api (31/07/2026):
# los 4 endpoints restantes responden correctamente; sólo el de turnos sigue
# devolviendo 404 con cualquier nombre probado.
DEMO_ENDPOINTS = {"listar_turnos"}

CLINICS = {}
user_sessions = {}

# Deduplicación de mensajes entrantes: Evolution/WhatsApp puede reintentar la
# entrega del mismo webhook ante un timeout de red. Sin esto, un reintento
# durante AWAITING_CONFIRMATION podría registrar la misma separación de
# quirófano dos veces.
_DEDUP_MAXLEN = 5000
_dedup_lock = threading.Lock()
_processed_msg_ids = set()
_processed_msg_ids_order = deque()

# Lock por sesión (clinic_id:telefono): serializa los mensajes de UN mismo
# usuario para evitar condiciones de carrera sobre su sesión, sin bloquear a
# los demás médicos que escriben al mismo tiempo.
_session_locks_meta_lock = threading.Lock()
_session_locks = {}

INACTIVITY_REMINDER_PERIOD = 5 * 60
SESSION_EXPIRATION_PERIOD = 15 * 60

DAYS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MONTHS_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

# Endpoints según "Documentos APIS QUirofanos_V2.docx". Los marcados como
# verificados responden 200 en producción; el de turnos sigue inferido del
# título de la sección 2.3 porque el documento no lo nombra y aún da 404.
LOLCLI_ENDPOINTS = {
    "validar_medico": "ValidarMedicoQuirofanoWsp",           # 2.1 — verificado
    "listar_quirofanos": "ListarQuirofanosWsp",              # 2.2 — verificado
    "listar_turnos": "ListarTurnosDisponiblesWsp",           # 2.3 — POR CONFIRMAR (404)
    "registrar_separacion": "RegistrarSeparacionQuirofanoWsp",  # 2.4 — verificado
    "calcular_precio": "CalcularPrecioQuirofanoWsp",         # 2.5 — verificado
    # 2.6 — verificado, pero SIN el sufijo 'Wsp': el documento lo nombra
    # 'ListarSeparacionesPorMedicoWsp' y ese nombre devuelve 404. El que
    # existe en el servidor es 'ListarSeparacionesPorMedico'.
    "listar_separaciones": "ListarSeparacionesPorMedico",    # 2.6 — verificado
}

# --- Pasarela de pagos ---------------------------------------------------
# El flujo de pago queda montado pero DESACTIVADO: todavía no llegan las URLs
# ni las credenciales de la pasarela. Con PAGOS_HABILITADOS=0 el bot confirma
# la reserva y la graba directamente en la base, que es el comportamiento
# acordado para la presentación. Cuando lleguen los datos basta con poner
# PAGOS_HABILITADOS=1 y completar el POST en _iniciar_pago().
PAGOS_HABILITADOS = os.getenv("PAGOS_HABILITADOS", "0") == "1"
PAGOS_URL_BASE = os.getenv("PAGOS_URL_BASE", "").rstrip("/")

# Tipos de documento de identidad aceptados por ValidarMedicoQuirofanoWsp.
# Los códigos fueron verificados uno a uno contra el API (05-09 y 11+ responden
# "CODIGO TIPO DOCUMENTO DE IDENTIDAD NO EXISTE O NO HABILITADO"); las
# descripciones son las de uso habitual y el cliente debe confirmarlas, ya que
# LOLCLI no expone un endpoint que liste el catálogo.
TIPOS_DOCUMENTO = [
    ("01", "DNI"),
    ("02", "Carné de extranjería"),
    ("03", "Pasaporte"),
    ("04", "Partida de nacimiento"),
    ("10", "Carné de identidad"),
    ("00", "Otro documento"),
]


# ------------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------------

def normalize_text(text):
    text = text.lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )
    text = text.replace(".", "").replace(",", "").replace("-", " ")
    return " ".join(text.split())


def format_date_es(date_obj):
    return f"{DAYS_ES[date_obj.weekday()]}, {date_obj.day:02d} de {MONTHS_ES[date_obj.month]}"


def format_duration_es(hours):
    label = f"{hours:g}".replace(".", ",")
    return f"{label} hora" + ("s" if hours != 1 else "")


def _hora_label(slot):
    """16 -> '08:00', 17 -> '08:30' (slot = hora*2 + 1 si es ':30').

    Acepta 48 para representar el fin de una reserva que termina a
    medianoche."""
    hora, resto = divmod(slot, 2)
    return f"{hora:02d}:{30 if resto else 0:02d}"


def _parse_hora_token(token):
    """'8', '08', '08:00', '8h', '8:30', '8h30' -> el slot de 30' (0-47) en el
    que empieza esa hora. None si no es una hora en punto o y media válida
    (00:00 a 23:30): LOLCLI separa la agenda en bloques de 30 minutos, así que
    cualquier otro minuto no corresponde a un turno real.
    """
    token = str(token).strip().lower().replace("hrs", "").replace("h", ":")
    partes = token.split(":")
    hora_txt = partes[0].strip()
    minuto_txt = partes[1].strip() if len(partes) > 1 and partes[1].strip() else "0"
    if not hora_txt.isdigit() or not minuto_txt.isdigit():
        return None
    hora, minuto = int(hora_txt), int(minuto_txt)
    if not (0 <= hora <= 23) or minuto not in (0, 30):
        return None
    return hora * 2 + (1 if minuto == 30 else 0)


def _hora_a_slots(token):
    """Los slots de 30' que representa un token de hora al escribirlo.

    Sin minuto ('8') son las DOS medias horas de esa hora completa, para que
    escribir una hora suelta siga encadenando la hora entera (08:00-09:00)
    como antes de que la agenda se separara en bloques de 30'. Con minuto
    explícito ('8:30') es sólo ese bloque. None si el token no es una hora
    válida.
    """
    limpio = str(token).strip().lower().replace("hrs", "").replace("h", ":")
    tiene_minuto = ":" in limpio and limpio.split(":", 1)[1].strip() != ""
    slot = _parse_hora_token(token)
    if slot is None:
        return None
    return [slot] if tiene_minuto else [slot, slot + 1]


def _parse_horas_texto(text):
    """Horas que pide un texto libre, como lista ordenada de slots de 30'.

    Acepta una hora suelta ('8' = la hora completa 08:00-09:00, '08:30' = sólo
    esa media hora), una lista ('8,9,10', '8 y 9') o un rango ('8-11', '8 a
    11'). Devuelve [] si no se entiende.

    Los rangos son inclusivos *en bloques*: '8 a 11' son las horas 8, 9, 10 y
    11 completas, o sea el quirófano de 08:00 a 12:00. Es ambiguo a propósito
    de leer, así que quien llama debe mostrarle al médico el horario final
    resultante.
    """
    limpio = str(text).strip().lower()
    for sep in [" a ", " al ", " hasta ", "-", "–"]:
        if sep in limpio:
            partes = [p for p in limpio.split(sep) if p.strip()]
            if len(partes) != 2:
                return []
            ini_slots, fin_slots = _hora_a_slots(partes[0]), _hora_a_slots(partes[1])
            if ini_slots is None or fin_slots is None:
                return []
            ini, fin = ini_slots[0], fin_slots[-1]
            if ini > fin:
                return []
            return list(range(ini, fin + 1))

    horas = []
    for token in limpio.replace(";", ",").replace(" y ", ",").split(","):
        if not token.strip():
            continue
        slots = _hora_a_slots(token)
        if slots is None:
            return []
        horas.extend(slots)
    return sorted(set(horas))


def _rango_label(horas):
    """[16,17,18,19] -> '08:00–10:00' (el bloque 09:30 ocupa hasta las 10:00)."""
    return f"{_hora_label(horas[0])}–{_hora_label(horas[-1] + 1)}"


def _fmt_hora_iso(valor):
    """'2026-08-04T13:00:00.000Z' -> '13:00'.

    La cadena se corta a propósito en vez de parsearla como UTC: LOLCLI
    devuelve la hora local con una 'Z' de más, así que convertir la zona
    horaria restaría 5 horas y mostraría un horario equivocado.
    """
    texto = str(valor or "")
    return texto[11:16] if len(texto) >= 16 else ""


def _fmt_fecha_iso(valor):
    texto = str(valor or "")
    try:
        return format_date_es(datetime.strptime(texto[:10], "%Y-%m-%d").date())
    except ValueError:
        return texto[:10]


def next_business_days(n=14):
    days = []
    current = date.today() + timedelta(days=1)
    while len(days) < n:
        if current.weekday() < 6:  # Mon–Sat
            days.append(current)
        current += timedelta(days=1)
    return days


def load_clinics():
    global CLINICS
    try:
        with open("clinics.json", "r", encoding="utf-8") as f:
            CLINICS = json.load(f)
        print(f"INFO: {len(CLINICS)} clínica(s) cargada(s): {list(CLINICS.keys())}")
    except Exception as e:
        print(f"ERROR: No se pudo cargar clinics.json: {e}")


def _mark_processed_if_new(msg_id):
    """True si es la primera vez que se ve este id de mensaje de WhatsApp.

    Sin id (payload inesperado) se deja pasar — no hay forma de deduplicar.
    """
    if not msg_id:
        return True
    with _dedup_lock:
        if msg_id in _processed_msg_ids:
            return False
        _processed_msg_ids.add(msg_id)
        _processed_msg_ids_order.append(msg_id)
        if len(_processed_msg_ids_order) > _DEDUP_MAXLEN:
            oldest = _processed_msg_ids_order.popleft()
            _processed_msg_ids.discard(oldest)
        return True


def _get_session_lock(session_key):
    with _session_locks_meta_lock:
        lock = _session_locks.get(session_key)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_key] = lock
        return lock


# ------------------------------------------------------------------------
# Modo demo (DEMO_MODE=1)
# ------------------------------------------------------------------------
# Sólo cubre los endpoints de DEMO_ENDPOINTS. Las respuestas replican la
# estructura de "Documentos APIS QUirofanos_V2.docx" (mismos nombres de campo y
# mismo sobre status/code/message), para que al quedar operativos los servicios
# reales el resto del bot no necesite ningún cambio.

# La agenda simulada cubre el día completo en bloques de 30 minutos (01:00 a
# 23:30), porque el médico puede encadenar todos los bloques seguidos que
# necesite y no sólo el horario de oficina. Se usan bloques de 30' y no de
# hora completa porque así es como LOLCLI entrega los turnos reales.
DEMO_HORAS = [f"{h:02d}:{m:02d}" for h in range(1, 24) for m in (0, 30)]

# Reservas ya registradas en LOLCLI durante esta ejecución:
# (quicod, fecha, hora) -> invnum. Permite que un turno recién reservado
# aparezca ocupado al volver a listar, pese a que la lista sea simulada.
_demo_lock = threading.Lock()
_demo_bookings = {}


def _demo_slot_ocupado(quicod, fecha, hora):
    """Ocupación base determinista (~20%), para que la agenda no salga vacía.

    Los tres valores se mezclan con un CRC32 en vez de sumarse: si el número de
    hora entrara de forma lineal, las horas ocupadas caerían en un patrón
    regular (una de cada N) y nunca quedarían bloques largos seguidos que
    reservar, que es justo lo que el médico necesita poder hacer. Se usa CRC32
    y no hash() porque hash() de un str cambia en cada arranque del proceso.
    """
    return zlib.crc32(f"{quicod}|{fecha}|{hora}".encode()) % 5 == 0


def _demo_slots_cubiertos(horini, horfin):
    """Bloques de 30' (HH:00/HH:30) que cruza el intervalo [horini, horfin)."""
    slots = []
    cursor = horini.replace(second=0, microsecond=0)
    while cursor < horfin:
        slots.append((cursor.strftime("%Y-%m-%d"), cursor.strftime("%H:%M")))
        cursor += timedelta(minutes=30)
    return slots


def _demo_error(code, message, container, empty):
    return {"status": "error", "code": code, "message": message, container: empty}


def _demo_response(endpoint_key, payload):
    if endpoint_key == "listar_turnos":
        quicod = payload.get("xxquicod", "")
        fecha = str(payload.get("xxfechaini", ""))
        turnos = []
        with _demo_lock:
            for hora in DEMO_HORAS:
                invnum = _demo_bookings.get((quicod, fecha, hora))
                ocupado = invnum is not None or _demo_slot_ocupado(quicod, fecha, hora)
                turnos.append({
                    "fecha": f"{fecha}T00:00:00.000Z",
                    "hora": hora,
                    "intcod1": f"INT-{invnum}" if invnum else "",
                    "medcod": "",
                    "sepcon": "OCUPADO" if ocupado else "",
                    "invnum": invnum or 0,
                    "disponible": "N" if ocupado else "S",
                })
        return {"status": "success", "code": 200, "message": "OK", "turnos": turnos}

    return _demo_error(500, "Error al obtener los registros", "data", [])


def _demo_marcar_reservado(payload, invnum):
    """Refleja en la agenda simulada una separación que sí se grabó en LOLCLI.

    Las reservas son reales, pero la lista de turnos todavía se simula. Sin
    esto, un turno recién reservado seguiría apareciendo libre al volver atrás.
    """
    try:
        ini = datetime.strptime(payload["xxhorini"], "%Y-%m-%dT%H:%M:%S")
        fin = datetime.strptime(payload["xxhorfin"], "%Y-%m-%dT%H:%M:%S")
    except (KeyError, ValueError):
        return
    quicod = payload.get("xxquicod", "")
    with _demo_lock:
        for fecha, hora in _demo_slots_cubiertos(ini, fin):
            _demo_bookings[(quicod, fecha, hora)] = invnum


# ------------------------------------------------------------------------
# LOLCLI API client
# ------------------------------------------------------------------------

def _call_lolcli(endpoint_key, payload, headers, timeout=8):
    """POST a un endpoint LOLCLI.

    Contrato (según Documentos APIS QUirofanos.docx, sección 3):
    el bot debe interceptar siempre `status`; si es "error" (negocio o HTTP 500
    unificado) se debe imprimir directamente el `message` devuelto al usuario.

    Retorna (data, error_message). error_message es None si status == "success".
    """
    endpoint = LOLCLI_ENDPOINTS[endpoint_key]

    if DEMO_MODE and endpoint_key in DEMO_ENDPOINTS:
        data = _demo_response(endpoint_key, payload)
        print(f"DEMO {endpoint}: payload={payload} -> {data.get('status')} ({data.get('message')})")
        if data.get("status") == "error":
            return data, data.get("message", "Ocurrió un error inesperado.")
        return data, None

    url = f"{g.lolcli_url}/{endpoint}"
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        # Falla de red/timeout: no hubo respuesta del servidor.
        print(f"ERROR {endpoint}: sin respuesta de {url} -- {type(e).__name__}: {e}")
        return None, "No pudimos conectar con el servidor en este momento. Intenta de nuevo en unos minutos."

    print(f"INFO {endpoint}: POST {url} payload={payload} -> HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        # Hubo respuesta, pero no es JSON: típicamente un 404 (nombre de
        # endpoint incorrecto -- los paths no están en la documentación) o una
        # página de error del servidor. Se registra el cuerpo para poder
        # distinguirlo de una caída de red.
        print(f"ERROR {endpoint}: respuesta no-JSON (HTTP {resp.status_code}): {resp.text[:500]}")
        return None, "No pudimos conectar con el servidor en este momento. Intenta de nuevo en unos minutos."

    if data.get("status") == "error":
        return data, data.get("message", "Ocurrió un error inesperado.")
    return data, None


# ---------------------------------------------------------------------------
# Messaging — Evolution API
# ---------------------------------------------------------------------------

def _evolution_headers():
    return {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}


def _resolve_instance(instance):
    if instance:
        return instance
    try:
        return g.evolution_instance
    except RuntimeError:
        return os.getenv("EVOLUTION_INSTANCE_NAME", "")


def _log_evolution_error(label, e):
    """Registra el error real de una llamada a Evolution.

    El texto de RequestException por sí solo ('400 Client Error: Bad Request
    for url: ...') no dice qué campo rechazó la API; el cuerpo de la
    respuesta sí trae el detalle (p.ej. qué propiedad falta o sobra), así que
    se imprime también cuando hay una respuesta HTTP disponible.
    """
    resp = getattr(e, "response", None)
    detalle = f" -- body: {resp.text[:500]}" if resp is not None else ""
    print(f"ERROR {label}: {e}{detalle}")


def send_whatsapp_message(phone, text, instance=None):
    time.sleep(1.2)
    inst = _resolve_instance(instance)
    try:
        requests.post(
            f"{EVOLUTION_API_URL}/message/sendText/{inst}",
            json={"number": phone, "text": text},
            headers=_evolution_headers(),
        ).raise_for_status()
        print(f"[TEXT] → {phone}")
    except requests.exceptions.RequestException as e:
        _log_evolution_error("send_whatsapp_message", e)


def send_button_message(phone, body, buttons, instance=None, title="", footer="LOLIMSA Quirófanos"):
    """buttons = [{"id": "btn_id", "title": "Label"}, ...]  — max 3"""
    time.sleep(1.2)
    inst = _resolve_instance(instance)
    payload = {
        "number": phone,
        "title": title,
        "description": body,
        "footer": footer,
        "buttons": [
            {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
            for b in buttons
        ],
    }
    try:
        requests.post(
            f"{EVOLUTION_API_URL}/message/sendButtons/{inst}",
            json=payload,
            headers=_evolution_headers(),
        ).raise_for_status()
        print(f"[BUTTONS] → {phone}")
    except requests.exceptions.RequestException as e:
        _log_evolution_error("send_button_message", e)
        print("  -- falling back to text")
        lines = "\n".join(f"*{i+1}.* {b['title']}" for i, b in enumerate(buttons))
        send_whatsapp_message(phone, f"{body}\n\n{lines}", inst)


def send_list_message(phone, body, sections, instance=None, title="", button_text="Ver opciones", footer=""):
    """sections = [{"title": "Sec", "rows": [{"id": "r1", "title": "T", "description": "D"}]}]

    Una fila puede traer además un 'number' opcional (sólo lo usa el fallback
    de texto plano, ver más abajo).

    A Evolution se le manda 'footerText' y 'rowId' (no 'footer'/'id'): son los
    nombres que espera el endpoint /message/sendList; con los nombres viejos
    la API devolvía 400 Bad Request y el mensaje nunca llegaba como lista
    interactiva, siempre por el texto de respaldo.
    """
    time.sleep(1.2)
    inst = _resolve_instance(instance)
    api_sections = [
        {
            "title": sec.get("title", ""),
            "rows": [
                {"title": row["title"], "description": row.get("description", ""), "rowId": row["id"]}
                for row in sec.get("rows", [])
            ],
        }
        for sec in sections
    ]
    payload = {
        "number": phone,
        "title": title,
        "description": body,
        "buttonText": button_text,
        "footerText": footer,
        "sections": api_sections,
    }
    try:
        requests.post(
            f"{EVOLUTION_API_URL}/message/sendList/{inst}",
            json=payload,
            headers=_evolution_headers(),
        ).raise_for_status()
        print(f"[LIST] → {phone}")
    except requests.exceptions.RequestException as e:
        _log_evolution_error("send_list_message", e)
        print("  -- falling back to text")
        lines = []
        counter = 0
        for row in [r for sec in sections for r in sec.get("rows", [])]:
            counter += 1
            # Por defecto el texto numera por posición (1, 2, 3...), que es lo
            # que 'process_user_choice' espera al leer la respuesta. Algunas
            # pantallas (como la de horas) necesitan que el número mostrado
            # coincida con otro valor (la hora real) en vez de la posición, o
            # que la fila no lleve número porque se responde con una palabra;
            # para esas, la fila puede traer su propio 'number' (o None).
            numero = row["number"] if "number" in row else counter
            if numero is None:
                lines.append(f"• {row['title']}")
            else:
                lines.append(f"*{numero}.* {row['title']}")
        send_whatsapp_message(phone, f"{body}\n\n" + "\n".join(lines), inst)


# ---------------------------------------------------------------------------
# Menu helpers
# ---------------------------------------------------------------------------

def process_user_choice(user_input, options, key_name=None):
    try:
        idx = int(user_input) - 1
        if 0 <= idx < len(options):
            return options[idx]["data"]
    except (ValueError, IndexError):
        if not key_name:
            return None
        normalized = normalize_text(user_input)
        for opt in options:
            if normalize_text(opt["data"].get(key_name, "")) == normalized:
                return opt["data"]
        names = [opt["data"].get(key_name, "") for opt in options]
        best, score = process.extractOne(user_input, names)
        if score > 75:
            for opt in options:
                if opt["data"].get(key_name, "") == best:
                    return opt["data"]
    return None


def show_main_menu(phone, session, instance=None):
    send_list_message(
        phone,
        "¿Qué deseas hacer?",
        sections=[{
            "title": "Opciones",
            "rows": [
                {"id": "menu_nueva",     "title": "🗓️ Nueva reserva",     "description": "Reservar un quirófano"},
                {"id": "menu_consultar", "title": "📋 Mis reservas",       "description": "Ver tus reservas programadas"},
                {"id": "menu_cancelar",  "title": "❌ Cancelar reserva",   "description": "Próximamente disponible"},
                {"id": "menu_asesor",    "title": "👤 Hablar con un asesor", "description": "Conectar con personal de soporte"},
            ],
        }],
        instance=instance,
        title="Menú principal",
        button_text="Ver opciones",
        footer="LOLIMSA Quirófanos",
    )
    session["state"] = "AWAITING_MAIN_MENU"


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

def session_cleanup_task():
    while True:
        time.sleep(60)
        now = time.time()
        for key in list(user_sessions.keys()):
            session = user_sessions.get(key)
            if not session or session.get("state") == "START":
                continue
            inactive = now - session.get("last_interaction_time", now)
            phone = key.split(":", 1)[1]
            inst = session.get("evolution_instance", "")
            if inactive > SESSION_EXPIRATION_PERIOD:
                print(f"INFO: Sesión expirada para {phone}.")
                send_whatsapp_message(
                    phone,
                    "⏰ Tu sesión ha cerrado por inactividad. Cuando quieras continuar, escríbenos y estaremos listos. 😊",
                    inst,
                )
                user_sessions.pop(key, None)
            elif inactive > INACTIVITY_REMINDER_PERIOD and not session.get("reminder_sent"):
                send_whatsapp_message(
                    phone,
                    "👋 ¿Sigues ahí? Dejaste tu reserva a medias. Responde para continuar o tu sesión cerrará pronto. 🕐",
                    inst,
                )
                session["reminder_sent"] = True


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

# Campos donde Evolution/Baileys pueden traer el número real cuando el
# remoteJid es un '@lid'. El nombre cambió entre versiones (Baileys pasó a
# 'remoteJidAlt'), así que se prueban todos en orden.
_LID_PHONE_FIELDS = ("senderPn", "remoteJidAlt", "participantAlt", "previousRemoteJid")


def _extract_phone(key):
    """Devuelve el destinatario al que hay que responder, o "" si el JID no es
    de un chat individual.

    WhatsApp ya no siempre manda el número en 'remoteJid': desde que existen
    los nombres de usuario (@miusuario), los contactos que ocultan su teléfono
    llegan con un identificador interno terminado en '@lid' (p.ej.
    '91573131989148@lid'). Si se usa ese valor como número, Evolution responde
    HTTP 400 y el médico nunca recibe la respuesta.

    En la mayoría de los casos el teléfono verdadero sí viaja en el mensaje,
    en alguno de los campos de _LID_PHONE_FIELDS. Cuando no viaja en ninguno
    se responde al propio '@lid': puede fallar, pero callarse falla siempre.

    También se descartan grupos ('@g.us') y estados ('status@broadcast'), que
    no son números y producirían el mismo 400.
    """
    jid = key.get("remoteJid") or ""

    if jid.endswith("@lid"):
        for field in _LID_PHONE_FIELDS:
            pn = (key.get(field) or "").split("@")[0]
            if pn.isdigit():
                return pn
        # Se vuelca la clave completa para poder identificar en qué campo viene
        # el número en esta versión de Evolution, si es que viene.
        print(f"ADVERTENCIA: JID @lid sin teléfono asociado, se responderá al propio lid -- key={key}")
        return jid

    if jid.endswith("@g.us") or jid == "status@broadcast":
        return ""

    return jid.split("@")[0]


@app.route("/test", methods=["GET"])
def test():
    return "OK", 200


@app.route("/webhook/<clinic_id>", methods=["POST"])
def webhook_handler(clinic_id):
    if clinic_id not in CLINICS:
        return jsonify({"status": "unknown_clinic"}), 404

    config = CLINICS[clinic_id]
    g.clinic_id = clinic_id
    g.lolcli_url = config["lolcli_url"]
    g.lolcli_token = config["lolcli_token"]
    g.lolcli_entidad = config["lolcli_entidad"]
    g.evolution_instance = config["evolution_instance"]
    g.default_siscod = config.get("default_siscod", 1)
    g.staff_phone = config.get("staff_phone", "")
    g.support_hours = config.get("support_hours", "")

    data = request.json
    try:
        key = data["data"]["key"]
        # El descarte de los mensajes propios va antes de resolver el número:
        # así no se ensucia el log con advertencias de '@lid' por los envíos
        # que hace el propio bot.
        if key["fromMe"]:
            return jsonify({"status": "ignored_from_me"}), 200
        sender = _extract_phone(key)
        msg = data["data"]["message"]
        msg_id = key.get("id")
    except (KeyError, TypeError):
        return jsonify({"status": "ignored_format"}), 200

    if not sender:
        print(f"INFO: mensaje ignorado, remoteJid no es un chat individual ({key.get('remoteJid')})")
        return jsonify({"status": "ignored_not_a_user"}), 200

    if not _mark_processed_if_new(msg_id):
        print(f"INFO: mensaje duplicado ignorado (id={msg_id}, sender={sender})")
        return jsonify({"status": "duplicate_ignored"}), 200

    # Normalize input — support plain text, button replies, and list replies
    message_text = (
        msg.get("conversation")
        or msg.get("extendedTextMessage", {}).get("text", "")
        or msg.get("buttonsResponseMessage", {}).get("selectedDisplayText", "")
        or msg.get("listResponseMessage", {}).get("title", "")
    ).strip()

    selected_id = (
        msg.get("buttonsResponseMessage", {}).get("selectedButtonId")
        or msg.get("listResponseMessage", {}).get("singleSelectReply", {}).get("selectedRowId")
    )

    lolcli_headers = {
        "Authorization": f"Basic {g.lolcli_token}",
        "Content-Type": "application/json",
    }

    session_key = f"{clinic_id}:{sender}"
    phone = sender
    print(f"[{clinic_id}] {sender}: '{message_text}' | id={selected_id}")

    # Serializa los mensajes de UN mismo usuario (evita condiciones de carrera
    # sobre su sesión) sin bloquear a otros médicos que escriben al mismo
    # tiempo — el servidor corre con threaded=True para atender ~300 usuarios/día.
    with _get_session_lock(session_key):
        return _handle_message(session_key, phone, message_text, selected_id, config, lolcli_headers)


def _handle_message(session_key, phone, message_text, selected_id, config, lolcli_headers):
    session = user_sessions.get(session_key, {"state": "START"})
    session["sender"] = phone
    session["clinic_id"] = g.clinic_id
    session["evolution_instance"] = g.evolution_instance
    session["last_interaction_time"] = time.time()
    if session.get("reminder_sent"):
        session["reminder_sent"] = False

    # --- Global commands ---
    normalized = normalize_text(message_text)

    if normalized in ["salir", "cancelar"]:
        user_sessions.pop(session_key, None)
        send_whatsapp_message(phone, "✅ Proceso cancelado. Escríbenos cuando quieras continuar. 👋")
        return jsonify({"status": "cancelled"})

    if normalized in ["ayuda", "hablar con alguien", "hablar con asesor", "asesor"] or selected_id == "menu_asesor":
        _trigger_human_handoff(session, phone, config, lolcli_headers)
        user_sessions[session_key] = session
        return jsonify({"status": "handoff"})

    if session.get("state") == "HUMAN_HANDOFF":
        if normalized in ["bot", "volver", "asistente"]:
            send_whatsapp_message(phone, "🤖 De vuelta con el asistente. Escribe *'hola'* para ver el menú.")
            session["state"] = "START"
        else:
            send_whatsapp_message(
                phone,
                f"Un asesor ha sido notificado y se pondrá en contacto pronto.\n"
                f"📞 También puedes llamarnos durante: {g.support_hours}\n\n"
                f"Escribe *'bot'* para volver al asistente automático.",
            )
        user_sessions[session_key] = session
        return jsonify({"status": "handoff_active"})

    if normalized == "retroceder" and session.get("state") not in ["START", "AWAITING_TIDCOD"]:
        history = session.get("history", [])
        if len(history) > 1:
            # 'history' guarda los pasos ya respondidos, no el actual. Retroceder
            # es volver a preguntar el último respondido, así que se saca de la
            # lista y se reproduce ese mismo. Antes se sacaba uno y se reproducía
            # el anterior, con lo que cada 'retroceder' saltaba dos pasos: desde
            # el resumen se volvía a preguntar la fecha en vez del horario.
            prev = history.pop()
            session["state"] = prev
            _replay_state(prev, session, phone, lolcli_headers)
        else:
            send_whatsapp_message(phone, "🔄 Ya estás en el primer paso. Escribe *'salir'* para cancelar.")
        user_sessions[session_key] = session
        return jsonify({"status": "reverted"})

    # --- State machine ---
    state = session.get("state", "START")

    if state == "START":
        session.clear()
        session["history"] = ["START"]
        send_whatsapp_message(
            phone,
            "👋 ¡Bienvenido/a al sistema de reservas de quirófanos LOLIMSA!",
        )
        _ask_tipo_documento(session, phone)

    elif state == "AWAITING_TIDCOD":
        tidcod = _resolve_tidcod(message_text, selected_id)
        if not tidcod:
            send_whatsapp_message(phone, "⚠️ Elige tu tipo de documento de la lista.")
        else:
            existe, err = _tidcod_existe(tidcod, lolcli_headers)
            if err:
                send_whatsapp_message(phone, f"❌ {err}")
            elif not existe:
                send_whatsapp_message(
                    phone,
                    f"❌ El tipo de documento *{tidcod}* no existe o no está habilitado.",
                )
                _ask_tipo_documento(session, phone)
            else:
                session["tidcod"] = tidcod
                session["tidnam"] = dict(TIPOS_DOCUMENTO).get(tidcod, tidcod)
                session.setdefault("history", []).append("AWAITING_TIDCOD")
                _ask_meddoc(session, phone)

    elif state == "AWAITING_MEDDOC":
        meddoc = message_text.strip()
        if not meddoc:
            send_whatsapp_message(phone, "⚠️ Por favor, ingresa tu número de documento.")
        else:
            resp_data, err = _call_lolcli(
                "validar_medico",
                {"tidcod": session.get("tidcod", ""), "meddoc": meddoc},
                lolcli_headers,
            )
            if err:
                # El documento manda imprimir el 'message' del API tal cual.
                send_whatsapp_message(phone, f"❌ {err}\n\nVerifica tu número e inténtalo de nuevo.")
            else:
                medicos = resp_data.get("medico", [])
                if isinstance(medicos, dict):
                    medicos = [medicos]
                medicos = [m for m in medicos if m.get("valido", "S") == "S"]
                if medicos:
                    medico = medicos[0]
                    session["meddoc"] = meddoc
                    session["medcod"] = medico.get("medcod", "")
                    session["mednam"] = medico.get("mednam", "")
                    session["regesp"] = medico.get("regesp", "")
                    session.setdefault("history", []).append("AWAITING_MEDDOC")
                    send_whatsapp_message(phone, f"✅ ¡Hola, {session['mednam']}!")
                    show_main_menu(phone, session)
                else:
                    send_whatsapp_message(
                        phone,
                        "❌ No encontramos un médico registrado con ese documento. "
                        "Verifica el número e inténtalo de nuevo.",
                    )

    elif state == "AWAITING_MAIN_MENU":
        choice = selected_id or normalized
        if choice in ["menu_nueva", "1", "nueva reserva", "nueva", "reservar"]:
            _start_booking_flow(session, phone, lolcli_headers)
        elif choice in ["menu_consultar", "2", "mis reservas", "consultar"]:
            _show_mis_reservas(session, phone, lolcli_headers)
        elif choice in ["menu_cancelar", "3", "cancelar reserva", "anular"]:
            # LOLCLI todavía no expone un endpoint de anulación (no aparece en
            # ninguna de las dos versiones del documento), así que se deriva a
            # soporte en vez de prometer algo que el API no puede hacer.
            send_whatsapp_message(
                phone,
                "🚧 La cancelación desde el chat estará disponible próximamente.\n"
                "Por ahora, para anular una reserva escribe *'asesor'* y te ayudamos.",
            )
            send_whatsapp_message(phone, "Escribe *'continuar'* para volver al menú.")
            session["state"] = "AWAITING_POST_FLOW"
        else:
            send_whatsapp_message(phone, "❓ Elige una opción del menú. 😊")

    elif state == "AWAITING_QUIROFANO":
        selected = _resolve_selection(message_text, selected_id, session)
        if selected:
            session.setdefault("history", []).append("AWAITING_QUIROFANO")
            session["quicod"] = selected["quicod"]
            session["quidel"] = selected["quidel"]
            session["quidec"] = selected["quidec"]
            session["prisal_hora"] = selected["prisal_hora"]
            _ask_date(session, phone, lolcli_headers)
        else:
            send_whatsapp_message(phone, "❓ No reconocí ese quirófano. Elige uno de la lista.")

    elif state == "AWAITING_DATE":
        selected = _resolve_selection(message_text, selected_id, session)
        if selected:
            session.setdefault("history", []).append("AWAITING_DATE")
            session["fecha_api"] = selected["fecha_api"]
            session["fecha_user"] = selected["fecha_user"]
            _ask_horas(session, phone, lolcli_headers)
        else:
            send_whatsapp_message(phone, "❓ No reconocí esa fecha. Elige una de la lista.")

    elif state == "AWAITING_HORAS":
        _handle_horas(session, phone, message_text, selected_id, normalized, lolcli_headers)

    elif state == "AWAITING_CONFIRMATION":
        # Al pulsar el botón, Evolution manda selectedButtonId ('conf_si') y como
        # texto la etiqueta completa con emoji ("✅ Sí, confirmar"), que no
        # coincide con ninguna palabra suelta: sin mirar el id, la confirmación
        # caía siempre en el 'else' y la reserva nunca se registraba.
        # Cuando el envío de botones falla, send_button_message reenvía como
        # texto numerado por posición ("1. ✅ Confirmar", "2. ↩️ Retroceder"),
        # así que también se acepta "1"/"2": si no, el médico escribe el
        # número que ve y el flujo se queda trabado sin avanzar.
        choice = selected_id or normalized
        if choice in ["conf_si", "si", "sí", "confirmar", "confirm", "1"] or "confirmar" in choice:
            _confirm_booking(session, phone, lolcli_headers)
        elif choice in ["conf_no", "no", "retroceder", "2"] or "retroceder" in choice:
            send_whatsapp_message(
                phone,
                "↩️ Escribe *'retroceder'* para corregir un paso o *'salir'* para cancelar.",
            )
        else:
            send_button_message(
                phone,
                "¿Confirmas la reserva?",
                [{"id": "conf_si", "title": "✅ Sí, confirmar"},
                 {"id": "conf_no", "title": "↩️ Retroceder"}],
            )

    elif state == "AWAITING_POST_FLOW":
        if normalized in ["continuar", "continue", "hola", "menu", "menú"]:
            session.clear()
            session["medcod"] = user_sessions.get(session_key, {}).get("medcod", "")
            session["mednam"] = user_sessions.get(session_key, {}).get("mednam", "")
            show_main_menu(phone, session)
        else:
            send_whatsapp_message(
                phone,
                "Escribe *'continuar'* para volver al menú o *'salir'* para cerrar la sesión. 😊",
            )

    user_sessions[session_key] = session
    return jsonify({"status": "processed"})


# ---------------------------------------------------------------------------
# Flow helpers
# ---------------------------------------------------------------------------

def _resolve_selection(message_text, selected_id, session):
    """Match a button/list ID or text number against session options."""
    options = session.get("options", [])
    if selected_id:
        for opt in options:
            if opt["data"].get("_id") == selected_id:
                return opt["data"]
    return process_user_choice(message_text, options)


# --- Autenticación: tipo de documento (tidcod) + número de documento (meddoc) ---

def _ask_tipo_documento(session, phone):
    rows = [
        {"id": f"tid_{code}", "title": label, "description": f"Código {code}"}
        for code, label in TIPOS_DOCUMENTO
    ]
    send_list_message(
        phone,
        "Para identificarte, elige tu *tipo de documento*:",
        sections=[{"title": "Tipo de documento", "rows": rows}],
        title="Identificación",
        button_text="Ver tipos",
    )
    session["state"] = "AWAITING_TIDCOD"


def _ask_meddoc(session, phone):
    send_whatsapp_message(
        phone,
        f"Ahora ingresa tu *número de documento* ({session.get('tidnam', '')}):",
    )
    session["state"] = "AWAITING_MEDDOC"


def _resolve_tidcod(message_text, selected_id):
    """Obtiene el tidcod de la selección de lista o de lo que el médico escribió.

    Acepta el código con o sin cero a la izquierda ("1" -> "01") y el nombre del
    documento ("dni" -> "01"), porque no todos los teléfonos renderizan la lista
    interactiva y el médico puede terminar escribiendo la respuesta.
    """
    if selected_id and selected_id.startswith("tid_"):
        return selected_id[4:]

    texto = message_text.strip()
    if not texto:
        return ""
    if texto.isdigit():
        return texto.zfill(2)

    objetivo = normalize_text(texto)
    for code, label in TIPOS_DOCUMENTO:
        if normalize_text(label) == objetivo:
            return code
    return texto


def _tidcod_existe(tidcod, lolcli_headers):
    """(existe, error) para un tipo de documento.

    LOLCLI no publica un endpoint que liste el catálogo, pero
    ValidarMedicoQuirofanoWsp sí distingue los dos casos: ante un tidcod
    inhabilitado responde "... NO EXISTE O NO HABILITADO", y ante uno válido con
    un documento inexistente responde "MEDICO NO SE ENCUENTRA REGISTRADO". Para
    los códigos ya verificados se evita la llamada; cualquier otro se consulta
    con un documento centinela.
    """
    if tidcod in dict(TIPOS_DOCUMENTO):
        return True, None

    resp_data, _ = _call_lolcli(
        "validar_medico", {"tidcod": tidcod, "meddoc": "0"}, lolcli_headers
    )
    if resp_data is None:
        return False, "No pudimos validar tu tipo de documento en este momento. Intenta de nuevo en unos minutos."
    return "NO EXISTE O NO HABILITADO" not in (resp_data.get("message") or "").upper(), None


def _start_booking_flow(session, phone, lolcli_headers):
    resp_data, err = _call_lolcli("listar_quirofanos", {"xxsiscod": g.default_siscod}, lolcli_headers)
    if err:
        send_whatsapp_message(phone, f"❌ {err}")
        return

    quirofanos = resp_data.get("quirofanos", [])
    if not quirofanos:
        send_whatsapp_message(phone, "😔 No hay quirófanos disponibles en este momento.")
        send_whatsapp_message(phone, "Escribe *'continuar'* para volver al menú.")
        session["state"] = "AWAITING_POST_FLOW"
        return

    rows = []
    formatted = []
    for i, q in enumerate(quirofanos):
        quicod = q.get("quicod", "")
        quidel = q.get("quidel") or f"Quirófano {i+1}"
        quidec = q.get("quidec", "")
        prisal_hora = float(q.get("prisal_hora") or 0)
        row_id = f"qui_{quicod}"
        rows.append({"id": row_id, "title": quidel, "description": f"{quidec} — S/ {prisal_hora:.2f}/hora"})
        formatted.append({
            "id": i + 1,
            "data": {"_id": row_id, "quicod": quicod, "quidel": quidel, "quidec": quidec, "prisal_hora": prisal_hora},
        })

    session["options"] = formatted
    session["state"] = "AWAITING_QUIROFANO"
    send_list_message(
        phone,
        "Selecciona el *quirófano* que deseas reservar:",
        sections=[{"title": "Quirófanos disponibles", "rows": rows}],
        title="Nueva reserva de quirófano",
        button_text="Ver quirófanos",
    )


def _ask_date(session, phone, lolcli_headers):
    days = next_business_days(14)
    rows = []
    formatted = []
    for i, d in enumerate(days):
        fecha_api = d.strftime("%Y-%m-%d")
        fecha_user = format_date_es(d)
        row_id = f"date_{fecha_api}"
        rows.append({"id": row_id, "title": fecha_user, "description": ""})
        formatted.append({"id": i + 1, "data": {"_id": row_id, "fecha_api": fecha_api, "fecha_user": fecha_user}})
    session["options"] = formatted
    session["state"] = "AWAITING_DATE"
    send_list_message(
        phone,
        f"Selecciona la *fecha* para *{session['quidel']}*:",
        sections=[{"title": "Fechas disponibles", "rows": rows}],
        title="Nueva reserva de quirófano",
        button_text="Ver fechas",
    )


def _ask_horas(session, phone, lolcli_headers):
    """Pide los turnos libres del día y arranca la selección de horas.

    Ya no se pregunta por la duración: el médico va sumando bloques de 30
    minutos seguidos y la duración sale de cuántos eligió.
    """
    fecha_ini = session["fecha_api"]
    fecha_fin = (datetime.strptime(fecha_ini, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    payload = {
        "xxsiscod": g.default_siscod,
        "xxfechaini": fecha_ini,
        "xxfechafin": fecha_fin,
        "xxquicod": session["quicod"],
    }
    resp_data, err = _call_lolcli("listar_turnos", payload, lolcli_headers)
    if err:
        send_whatsapp_message(phone, f"❌ {err}")
        return

    # Se filtra también por fecha exacta, no solo por 'disponible': si el rango
    # [xxfechaini, xxfechafin) resulta inclusivo en el backend, podría devolver
    # turnos del día siguiente y duplicar bloques (p.ej. dos "08:00"), arriesgando
    # que el médico reserve sin querer el día equivocado.
    #
    # LOLCLI trae un registro por cada bloque de 30 minutos (":00" y ":30"), no
    # uno por hora, así que _parse_hora_token conserva el minuto en vez de
    # truncarlo: si sólo se guardara la hora, un bloque libre de media hora
    # bastaría para ofrecer la hora entera como disponible aunque el otro
    # bloque ya tuviera una cirugía asignada, y la reserva chocaría con ella.
    turnos = [
        t for t in resp_data.get("turnos", [])
        if t.get("disponible") == "S" and str(t.get("fecha", "")).startswith(fecha_ini)
    ]
    horas = sorted({
        h for h in (_parse_hora_token(t.get("hora", "")) for t in turnos) if h is not None
    })
    if not horas:
        send_whatsapp_message(
            phone,
            "😔 No hay horarios disponibles para ese quirófano en esta fecha. Escribe *'retroceder'* para elegir otra fecha.",
        )
        return

    session["horas_disponibles"] = horas
    session["horas_sel"] = []
    session["state"] = "AWAITING_HORAS"
    _send_horas_list(session, phone)


def _send_horas_list(session, phone):
    """Muestra la lista de horas según lo que el médico lleve elegido.

    Cada opción es un bloque de 30 minutos, igual que en la agenda real.
    Mientras no haya nada elegido se ofrecen todos los bloques libres. En
    cuanto hay una selección sólo se ofrecen los dos bloques pegados (el
    anterior y el siguiente): así el horario no puede quedar con huecos, que
    es lo que la base necesita para grabarlo como un único intervalo.
    """
    disponibles = session.get("horas_disponibles", [])
    sel = session.get("horas_sel", [])
    rows = []

    def add(row_id, title, description=""):
        # 'number': None para que, si el mensaje de lista falla y WhatsApp cae
        # a texto plano, no se numere por posición. En esta pantalla el médico
        # responde con la hora misma ('8:30' para un bloque, '8' para
        # encadenar la hora completa) y no con la fila en la lista — un número
        # de posición ahí le haría reservar un horario distinto del que ve.
        rows.append({"id": row_id, "title": title, "description": description, "number": None})

    if sel:
        add("horas_listo", f"✅ Listo — {_rango_label(sel)}",
            f"Reservar {format_duration_es(len(sel) / 2)} y continuar")
        for slot in (sel[0] - 1, sel[-1] + 1):
            if slot in disponibles:
                add(f"hora_{slot}", f"➕ {_hora_label(slot)}", "Sumar este bloque al horario")
        add("horas_reset", "🔄 Empezar de nuevo", "Borrar las horas elegidas")
        body = (
            f"Llevas *{_rango_label(sel)}* ({format_duration_es(len(sel) / 2)}).\n\n"
            "Suma otro bloque escribiendo su hora (por ejemplo *8:30*, o *9* para sumar "
            "la hora completa), o escribe *listo* para continuar."
        )
    else:
        for slot in disponibles:
            add(f"hora_{slot}", _hora_label(slot))
        body = (
            f"Elige la *hora de inicio* en *{session['quidel']}* para *{session['fecha_user']}*.\n\n"
            "_Escribe una hora suelta para encadenar la hora completa (por ejemplo *8*), "
            "o con minutos para un solo bloque de 30 min (por ejemplo *8:30*). "
            "También puedes escribir un rango, por ejemplo *8 a 11*._"
        )

    send_list_message(
        phone,
        body,
        sections=[{"title": "Horarios", "rows": rows}],
        button_text="Ver horarios",
    )


def _agregar_horas(session, nuevas):
    """Suma bloques de 30' a la selección. Devuelve (ok, mensaje_de_error)."""
    disponibles = set(session.get("horas_disponibles", []))
    ocupadas = [h for h in nuevas if h not in disponibles]
    if ocupadas:
        return False, (
            "Estos horarios no están disponibles: "
            + ", ".join(_hora_label(h) for h in sorted(ocupadas))
        )

    combinadas = sorted(set(session.get("horas_sel", [])) | set(nuevas))
    if any(b - a != 1 for a, b in zip(combinadas, combinadas[1:])):
        return False, "Los bloques deben ser seguidos, sin saltos entre ellos."

    session["horas_sel"] = combinadas
    return True, None


def _handle_horas(session, phone, message_text, selected_id, normalized, lolcli_headers):
    """Procesa una respuesta en la pantalla de selección de horas.

    Aquí NO se usa _resolve_selection: esa función empareja por número de
    opción, y en esta pantalla un "8" significa la hora 08:00 (completa), no
    la octava fila de la lista. Confundirlos reservaría el horario equivocado.
    """
    if selected_id == "horas_listo" or normalized in ["listo", "ok", "continuar", "terminar", "ya"]:
        _cerrar_seleccion_horas(session, phone, lolcli_headers)
        return

    if selected_id == "horas_reset" or normalized in ["empezar de nuevo", "reiniciar", "borrar"]:
        session["horas_sel"] = []
        _send_horas_list(session, phone)
        return

    if selected_id and selected_id.startswith("hora_"):
        nuevas = [int(selected_id.split("_", 1)[1])]
    else:
        nuevas = _parse_horas_texto(message_text)

    if not nuevas:
        send_whatsapp_message(
            phone,
            "❓ No entendí ese horario. Elige una hora de la lista, o escribe algo como *8* o *8 a 11*.",
        )
        return

    ok, err = _agregar_horas(session, nuevas)
    if not ok:
        send_whatsapp_message(phone, f"⚠️ {err}")
    # En ambos casos se reenvía la lista: si salió bien muestra el bloque
    # acumulado (para que el médico vea la hora de fin real antes de seguir), y
    # si falló vuelve a ofrecer las opciones válidas.
    _send_horas_list(session, phone)


def _cerrar_seleccion_horas(session, phone, lolcli_headers):
    sel = session.get("horas_sel", [])
    if not sel:
        send_whatsapp_message(phone, "⚠️ Primero elige al menos una hora.")
        _send_horas_list(session, phone)
        return

    horini_dt = datetime.strptime(f"{session['fecha_api']}T{_hora_label(sel[0])}", "%Y-%m-%dT%H:%M")
    # Cada elemento de 'sel' es un bloque de 30 minutos, no una hora.
    horfin_dt = horini_dt + timedelta(minutes=30 * len(sel))
    session["horini"] = horini_dt.strftime("%Y-%m-%dT%H:%M:%S")
    session["horfin"] = horfin_dt.strftime("%Y-%m-%dT%H:%M:%S")
    session["hora_user"] = horini_dt.strftime("%H:%M")
    session["hora_fin_user"] = _hora_label(sel[-1] + 1)
    session["duracion_horas"] = len(sel) / 2
    session.setdefault("history", []).append("AWAITING_HORAS")
    _calcular_precio_y_continuar(session, phone, lolcli_headers)


def _calcular_precio_y_continuar(session, phone, lolcli_headers):
    payload = {
        "xxquicod": session["quicod"],
        "xxfechaini": session["horini"],
        "xxfechafin": session["horfin"],
        "xxprisal_hora": session["prisal_hora"],
    }
    resp_data, err = _call_lolcli("calcular_precio", payload, lolcli_headers, timeout=10)
    if err:
        send_whatsapp_message(phone, f"❌ {err}")
        return

    cotizaciones = resp_data.get("cotizacion", [])
    if not cotizaciones:
        send_whatsapp_message(phone, "😔 No pudimos calcular el precio. Intenta de nuevo.")
        return

    cot = cotizaciones[0]
    session["precio_total"] = float(cot.get("precio_total") or 0)
    session["horas_cobradas"] = float(cot.get("horas") or session["duracion_horas"])

    # Ya no se pregunta por el procedimiento: el nombre del quirófano indica su
    # especialidad y su uso, así que preguntarlo era un paso de más.
    _show_booking_summary(session, phone)


def _show_booking_summary(session, phone):
    precio = session.get("precio_total", 0)
    summary = (
        f"📋 *Resumen de la reserva:*\n\n"
        f"👨‍⚕️ *Médico:* {session.get('mednam', '')}\n"
        f"🏥 *Quirófano:* {session.get('quidel', '')}\n"
        f"📍 *Ubicación:* {session.get('quidec', '')}\n"
        f"🗓️ *Fecha:* {session.get('fecha_user', '')}\n"
        f"⏰ *Horario:* {session.get('hora_user', '')} – {session.get('hora_fin_user', '')}\n"
        f"⏱️ *Duración:* {format_duration_es(session.get('duracion_horas', 0))}\n"
        f"💰 *Total:* S/ {precio:.2f}\n\n"
        f"Al confirmar, la reserva queda registrada en el sistema.\n"
        f"_El pago se coordina por separado._\n\n"
        f"¿Confirmas la reserva?"
    )
    send_button_message(
        phone,
        summary,
        [{"id": "conf_si", "title": "✅ Confirmar"},
         {"id": "conf_no", "title": "↩️ Retroceder"}],
    )
    session["state"] = "AWAITING_CONFIRMATION"


def _build_pago_payload(session):
    """Datos de la reserva que necesitará la pasarela de pagos.

    Se arma desde ya para que el día que lleguen las credenciales sólo haya que
    hacer el POST. Hoy nadie lo consume: sólo se registra en el log.
    """
    return {
        "medcod": session.get("medcod", ""),
        "mednam": session.get("mednam", ""),
        "quicod": session.get("quicod", ""),
        "quidel": session.get("quidel", ""),
        "fecha": session.get("fecha_api", ""),
        "horini": session.get("horini", ""),
        "horfin": session.get("horfin", ""),
        "horas": session.get("duracion_horas", 0),
        "moneda": "PEN",
        "importe": round(float(session.get("precio_total") or 0), 2),
    }


def _iniciar_pago(session, phone):
    """Punto de enganche de la pasarela de pagos.

    Devuelve True si el cobro quedó pendiente (el flujo debe esperar al pago) y
    False si hay que seguir y grabar la reserva sin cobrar.

    Mientras PAGOS_HABILITADOS sea 0 siempre devuelve False: la reserva se graba
    directo en la base y el resumen del chat hace de comprobante. Ante cualquier
    problema de configuración también devuelve False, para que una pasarela mal
    configurada nunca deje al médico sin poder reservar.
    """
    payload = _build_pago_payload(session)

    if not PAGOS_HABILITADOS:
        print(f"INFO: pago omitido (PAGOS_HABILITADOS=0) -- payload listo: {payload}")
        return False

    if not PAGOS_URL_BASE:
        print("ADVERTENCIA: PAGOS_HABILITADOS=1 pero falta PAGOS_URL_BASE; se continúa sin cobrar.")
        return False

    # TODO: POST a la pasarela con `payload`, enviar el link de pago al médico y
    # pasar la sesión a AWAITING_PAGO. Falta la URL y las credenciales.
    print(f"ADVERTENCIA: integración de pagos pendiente; se continúa sin cobrar -- {payload}")
    return False


def _confirm_booking(session, phone, lolcli_headers):
    # El cobro va antes de grabar. Hoy _iniciar_pago siempre devuelve False y
    # se pasa de largo, que es lo acordado para esta etapa.
    if _iniciar_pago(session, phone):
        return

    payload = {
        "xxsiscod": g.default_siscod,
        "xxquicod": session["quicod"],
        "xxsepdat": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "xxhorini": session["horini"],
        "xxhorfin": session["horfin"],
        "xxmedcod": session["medcod"],
    }
    resp_data, err = _call_lolcli("registrar_separacion", payload, lolcli_headers, timeout=10)
    if err:
        send_whatsapp_message(phone, f"❌ No se pudo registrar la reserva: {err}. Intenta de nuevo.")
        return

    invnum = resp_data.get("invnum", "—")
    session["invnum"] = invnum
    if DEMO_MODE and "listar_turnos" in DEMO_ENDPOINTS:
        _demo_marcar_reservado(payload, invnum)
    send_whatsapp_message(
        phone,
        f"✅ *¡Reserva registrada!*\n\n"
        f"🆔 *N° de intervención:* {invnum}\n"
        f"👨‍⚕️ *Médico:* {session.get('mednam', '')}\n"
        f"🏥 *Quirófano:* {session.get('quidel', '')}\n"
        f"🗓️ *Fecha:* {session.get('fecha_user', '')}\n"
        f"⏰ *Horario:* {session.get('hora_user', '')} – {session.get('hora_fin_user', '')}\n"
        f"⏱️ *Duración:* {format_duration_es(session.get('duracion_horas', 0))}\n"
        f"💰 *Total:* S/ {session.get('precio_total', 0):.2f}\n\n"
        f"Puedes verla cuando quieras en *Mis reservas*.\n"
        f"¡Hasta pronto! 🙏",
    )
    send_whatsapp_message(
        phone,
        "Escribe *'continuar'* para hacer otra reserva o *'salir'* para cerrar la sesión.",
    )
    session["state"] = "AWAITING_POST_FLOW"


def _show_mis_reservas(session, phone, lolcli_headers):
    """Sección 2.6: reservas del médico desde hoy en adelante."""
    resp_data, err = _call_lolcli(
        "listar_separaciones", {"xxmedcod": session.get("medcod", "")}, lolcli_headers
    )

    if err:
        # "No registra reservas" llega como un 400 de negocio, no como un fallo:
        # se imprime el propio mensaje del API, según la sección 3 del documento.
        send_whatsapp_message(phone, f"📭 {err}")
    else:
        separaciones = resp_data.get("separaciones", [])
        if not separaciones:
            send_whatsapp_message(phone, "📭 No tienes reservas programadas de hoy en adelante.")
        else:
            bloques = [f"📋 *Tus reservas programadas* ({len(separaciones)}):"]
            for s in separaciones:
                bloques.append(
                    f"\n🆔 *N° {s.get('invnum', '—')}*\n"
                    f"🏥 {s.get('quirofano_nombre') or s.get('quicod', '')}\n"
                    f"🗓️ {_fmt_fecha_iso(s.get('fecha_separacion'))}\n"
                    f"⏰ {_fmt_hora_iso(s.get('hora_inicio'))} – {_fmt_hora_iso(s.get('hora_fin'))}"
                )
            send_whatsapp_message(phone, "\n".join(bloques))

    send_whatsapp_message(phone, "Escribe *'continuar'* para volver al menú.")
    session["state"] = "AWAITING_POST_FLOW"


def _trigger_human_handoff(session, phone, config, lolcli_headers):
    staff_phone = config.get("staff_phone", "")
    support_hours = config.get("support_hours", "")
    inst = config.get("evolution_instance", "")

    send_whatsapp_message(
        phone,
        f"👤 Conectando con un asesor...\n\n"
        f"Nuestro equipo ha sido notificado y se pondrá en contacto pronto.\n"
        f"⏰ Horario de atención: {support_hours}\n\n"
        f"Escribe *'bot'* en cualquier momento para volver al asistente automático.",
        inst,
    )
    if staff_phone:
        send_whatsapp_message(
            staff_phone,
            f"🔔 *Solicitud de asesor*\nMédico: {session.get('mednam', 'desconocido')} "
            f"({session.get('medcod', '')})\nTeléfono: {phone}",
            inst,
        )
    session["state"] = "HUMAN_HANDOFF"


def _replay_state(state, session, phone, lolcli_headers):
    if state == "AWAITING_TIDCOD":
        _ask_tipo_documento(session, phone)
    elif state == "AWAITING_MEDDOC":
        _ask_meddoc(session, phone)
    elif state == "AWAITING_QUIROFANO":
        _start_booking_flow(session, phone, lolcli_headers)
    elif state == "AWAITING_DATE":
        _ask_date(session, phone, lolcli_headers)
    elif state == "AWAITING_HORAS":
        _ask_horas(session, phone, lolcli_headers)
    elif state == "AWAITING_MAIN_MENU":
        show_main_menu(phone, session)
    else:
        send_whatsapp_message(phone, "↩️ Volviendo al menú principal.")
        show_main_menu(phone, session)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

load_clinics()
if DEMO_MODE:
    print(f"INFO: DEMO_MODE activo -- simulando sólo: {', '.join(sorted(DEMO_ENDPOINTS))}.")
    print("INFO: el resto (validar médico, listar quirófanos, calcular precio) usa el API real.")
threading.Thread(target=session_cleanup_task, daemon=True).start()

if __name__ == "__main__":
    # waitress es un servidor WSGI de producción (a diferencia de app.run(),
    # pensado solo para desarrollo). WAITRESS_THREADS controla cuántas
    # peticiones puede atender en simultáneo; con un solo proceso,
    # user_sessions / los locks de sesión / el dedup de mensajes siguen
    # viviendo en memoria compartida entre esos hilos, así que no hace falta
    # moverlos a un store externo (Redis) a menos que en el futuro se corra
    # más de un proceso worker.
    from waitress import serve

    _port = int(os.getenv("PORT", 5000))
    _threads = int(os.getenv("WAITRESS_THREADS", 4))
    print(f"INFO: Iniciando servidor waitress en http://0.0.0.0:{_port} ({_threads} hilos)")
    serve(
        app,
        host="0.0.0.0",
        port=_port,
        threads=_threads,
    )
