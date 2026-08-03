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

# Duración en horas ofrecida al médico para cada reserva.
DURATION_OPTIONS_HOURS = [1, 1.5, 2, 3, 4]

# Endpoints según "Documentos APIS QUirofanos_V2.docx". Los marcados como
# verificados responden 200 en producción; el de turnos sigue inferido del
# título de la sección 2.3 porque el documento no lo nombra y aún da 404.
LOLCLI_ENDPOINTS = {
    "validar_medico": "ValidarMedicoQuirofanoWsp",           # 2.1 — verificado
    "listar_quirofanos": "ListarQuirofanosWsp",              # 2.2 — verificado
    "listar_turnos": "ListarTurnosDisponiblesWsp",           # 2.3 — POR CONFIRMAR (404)
    "registrar_separacion": "RegistrarSeparacionQuirofanoWsp",  # 2.4 — existe, devuelve 500
    "calcular_precio": "CalcularPrecioQuirofanoWsp",         # 2.5 — verificado
}

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

DEMO_HORAS = [f"{h:02d}:00" for h in range(8, 18)]

# Reservas ya registradas en LOLCLI durante esta ejecución:
# (quicod, fecha, hora) -> invnum. Permite que un turno recién reservado
# aparezca ocupado al volver a listar, pese a que la lista sea simulada.
_demo_lock = threading.Lock()
_demo_bookings = {}


def _demo_slot_ocupado(quicod, fecha, hora):
    """Ocupación base determinista (~25%), para que la agenda no salga vacía."""
    seed = sum(ord(c) for c in quicod) + int(fecha.replace("-", "")) + int(hora[:2])
    return seed % 4 == 0


def _demo_slots_cubiertos(horini, horfin):
    """Bloques horarios (HH:00) que cruza el intervalo [horini, horfin)."""
    slots = []
    cursor = horini.replace(minute=0, second=0, microsecond=0)
    while cursor < horfin:
        slots.append((cursor.strftime("%Y-%m-%d"), cursor.strftime("%H:00")))
        cursor += timedelta(hours=1)
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
        print(f"ERROR send_whatsapp_message: {e}")


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
        print(f"ERROR send_button_message: {e} — falling back to text")
        lines = "\n".join(f"*{i+1}.* {b['title']}" for i, b in enumerate(buttons))
        send_whatsapp_message(phone, f"{body}\n\n{lines}", inst)


def send_list_message(phone, body, sections, instance=None, title="", button_text="Ver opciones", footer=""):
    """sections = [{"title": "Sec", "rows": [{"id": "r1", "title": "T", "description": "D"}]}]"""
    time.sleep(1.2)
    inst = _resolve_instance(instance)
    payload = {
        "number": phone,
        "title": title,
        "description": body,
        "buttonText": button_text,
        "footer": footer,
        "sections": sections,
    }
    try:
        requests.post(
            f"{EVOLUTION_API_URL}/message/sendList/{inst}",
            json=payload,
            headers=_evolution_headers(),
        ).raise_for_status()
        print(f"[LIST] → {phone}")
    except requests.exceptions.RequestException as e:
        print(f"ERROR send_list_message: {e} — falling back to text")
        lines = []
        for i, row in enumerate(
            [r for sec in sections for r in sec.get("rows", [])], 1
        ):
            lines.append(f"*{i}.* {row['title']}")
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
                {"id": "menu_consultar", "title": "📋 Mis reservas",       "description": "Próximamente disponible"},
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

def _extract_phone(key):
    """Devuelve el teléfono real del remitente, o "" si el JID no es de un chat
    individual.

    WhatsApp ya no siempre manda el número en 'remoteJid': para los contactos
    con privacidad activada llega un identificador interno terminado en '@lid'
    (p.ej. '91573131989148@lid'). Si se usa ese valor como número, Evolution
    responde HTTP 400 y el médico nunca recibe la respuesta. En esos casos el
    teléfono verdadero viaja en 'senderPn'.

    También se descartan grupos ('@g.us') y estados ('status@broadcast'), que
    no son números y producirían el mismo 400.
    """
    jid = key.get("remoteJid") or ""

    if jid.endswith("@lid"):
        pn = key.get("senderPn") or key.get("previousRemoteJid") or ""
        if not pn:
            # No se pudo resolver: se vuelca la clave completa para identificar
            # en qué campo viene el número en esta versión de Evolution.
            print(f"ERROR: JID @lid sin teléfono asociado -- key={key}")
        return pn.split("@")[0]

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
        sender = _extract_phone(key)
        if key["fromMe"]:
            return jsonify({"status": "ignored_from_me"}), 200
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
            history.pop()
            prev = history[-1]
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
        elif choice in ["menu_consultar", "2", "mis reservas", "consultar",
                         "menu_cancelar", "3", "cancelar reserva", "anular"]:
            send_whatsapp_message(
                phone,
                "🚧 Esta función estará disponible próximamente.\n"
                "Por ahora, para consultar o cancelar una reserva contacta a nuestro equipo de soporte "
                "escribiendo *'asesor'*.",
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
            _ask_time_block(session, phone, lolcli_headers)
        else:
            send_whatsapp_message(phone, "❓ No reconocí esa fecha. Elige una de la lista.")

    elif state == "AWAITING_TIME_BLOCK":
        selected = _resolve_selection(message_text, selected_id, session)
        if selected:
            session.setdefault("history", []).append("AWAITING_TIME_BLOCK")
            session["hora_api"] = selected["hora_api"]
            session["hora_user"] = selected["hora_api"]
            _ask_duration(session, phone, lolcli_headers)
        else:
            send_whatsapp_message(phone, "❓ No reconocí ese horario. Elige uno de la lista.")

    elif state == "AWAITING_DURATION":
        selected = _resolve_selection(message_text, selected_id, session)
        if selected:
            session.setdefault("history", []).append("AWAITING_DURATION")
            duracion = selected["duracion_horas"]
            horini_dt = datetime.strptime(f"{session['fecha_api']}T{session['hora_api']}", "%Y-%m-%dT%H:%M")
            horfin_dt = horini_dt + timedelta(hours=duracion)
            session["horini"] = horini_dt.strftime("%Y-%m-%dT%H:%M:%S")
            session["horfin"] = horfin_dt.strftime("%Y-%m-%dT%H:%M:%S")
            session["hora_fin_user"] = horfin_dt.strftime("%H:%M")
            session["duracion_horas"] = duracion
            _calcular_precio_y_continuar(session, phone, lolcli_headers)
        else:
            send_whatsapp_message(phone, "❓ No reconocí esa opción. Elige una de la lista.")

    elif state == "AWAITING_PROCEDURE":
        if normalize_text(message_text) == "omitir":
            session["procedimiento"] = ""
        else:
            session["procedimiento"] = message_text.strip()
        session.setdefault("history", []).append("AWAITING_PROCEDURE")
        _show_booking_summary(session, phone)

    elif state == "AWAITING_CONFIRMATION":
        # Al pulsar el botón, Evolution manda selectedButtonId ('conf_si') y como
        # texto la etiqueta completa con emoji ("✅ Sí, confirmar"), que no
        # coincide con ninguna palabra suelta: sin mirar el id, la confirmación
        # caía siempre en el 'else' y la reserva nunca se registraba.
        choice = selected_id or normalized
        if choice in ["conf_si", "si", "sí", "confirmar", "confirm"] or "confirmar" in choice:
            _confirm_booking(session, phone, lolcli_headers)
        elif choice in ["conf_no", "no", "retroceder"] or "retroceder" in choice:
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


def _ask_time_block(session, phone, lolcli_headers):
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
    # turnos del día siguiente y duplicar horas (p.ej. dos "08:00"), arriesgando
    # que el médico reserve sin querer el día equivocado.
    turnos = [
        t for t in resp_data.get("turnos", [])
        if t.get("disponible") == "S" and str(t.get("fecha", "")).startswith(fecha_ini)
    ]
    if not turnos:
        send_whatsapp_message(
            phone,
            "😔 No hay horarios disponibles para ese quirófano en esta fecha. Escribe *'retroceder'* para elegir otra fecha.",
        )
        return

    rows = []
    formatted = []
    for i, t in enumerate(turnos):
        hora = t.get("hora", "")
        row_id = f"hora_{hora}_{i}"
        rows.append({"id": row_id, "title": hora, "description": ""})
        formatted.append({"id": i + 1, "data": {"_id": row_id, "hora_api": hora}})

    session["options"] = formatted
    session["state"] = "AWAITING_TIME_BLOCK"
    send_list_message(
        phone,
        f"Horarios disponibles en *{session['quidel']}* para *{session['fecha_user']}*:",
        sections=[{"title": "Horarios", "rows": rows}],
        button_text="Ver horarios",
    )


def _ask_duration(session, phone, lolcli_headers):
    rows = []
    formatted = []
    for i, h in enumerate(DURATION_OPTIONS_HOURS):
        row_id = f"dur_{h}"
        rows.append({"id": row_id, "title": format_duration_es(h), "description": ""})
        formatted.append({"id": i + 1, "data": {"_id": row_id, "duracion_horas": h}})
    session["options"] = formatted
    session["state"] = "AWAITING_DURATION"
    send_list_message(
        phone,
        f"¿Cuánto tiempo necesitas el quirófano a partir de las *{session['hora_user']}*?",
        sections=[{"title": "Duración", "rows": rows}],
        button_text="Ver opciones",
    )


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

    send_whatsapp_message(
        phone,
        "📋 Opcionalmente, indica el nombre del *procedimiento o cirugía* a realizar.\n"
        "_Escribe el nombre o *'omitir'* para continuar sin especificarlo._",
    )
    session["state"] = "AWAITING_PROCEDURE"


def _show_booking_summary(session, phone):
    proc = session.get("procedimiento") or "No especificado"
    precio = session.get("precio_total", 0)
    summary = (
        f"📋 *Resumen de la reserva:*\n\n"
        f"👨‍⚕️ *Médico:* {session.get('mednam', '')}\n"
        f"🏥 *Quirófano:* {session.get('quidel', '')}\n"
        f"📍 *Ubicación:* {session.get('quidec', '')}\n"
        f"🗓️ *Fecha:* {session.get('fecha_user', '')}\n"
        f"⏰ *Horario:* {session.get('hora_user', '')} – {session.get('hora_fin_user', '')}\n"
        f"🔬 *Procedimiento:* {proc}\n"
        f"💰 *Total a pagar:* S/ {precio:.2f}\n\n"
        f"¿Confirmas la reserva?"
    )
    send_button_message(
        phone,
        summary,
        [{"id": "conf_si", "title": "✅ Confirmar"},
         {"id": "conf_no", "title": "↩️ Retroceder"}],
    )
    session["state"] = "AWAITING_CONFIRMATION"


def _confirm_booking(session, phone, lolcli_headers):
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
        f"✅ *¡Reserva confirmada!*\n\n"
        f"🆔 *N° de intervención:* {invnum}\n"
        f"🏥 *Quirófano:* {session.get('quidel', '')}\n"
        f"🗓️ *Fecha:* {session.get('fecha_user', '')}\n"
        f"⏰ *Horario:* {session.get('hora_user', '')} – {session.get('hora_fin_user', '')}\n"
        f"💰 *Total:* S/ {session.get('precio_total', 0):.2f}\n\n"
        f"¡Hasta pronto! 🙏",
    )
    send_whatsapp_message(
        phone,
        "Escribe *'continuar'* para hacer otra reserva o *'salir'* para cerrar la sesión.",
    )
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
    elif state == "AWAITING_TIME_BLOCK":
        _ask_time_block(session, phone, lolcli_headers)
    elif state == "AWAITING_DURATION":
        _ask_duration(session, phone, lolcli_headers)
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
