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

# NOTA: la documentación de la API ("Documentos APIS QUirofanos.docx") especifica
# método (POST), content-type y los contratos de payload/respuesta de cada
# endpoint, pero no el path literal. Los nombres abajo están inferidos de los
# títulos de cada sección del documento y deben confirmarse con el equipo LOLCLI.
LOLCLI_ENDPOINTS = {
    "validar_medico": "ValidarMedico",              # 2.1 Validar Médico Quirófano
    "listar_quirofanos": "ListarQuirofanos",         # 2.2 Listar Quirófanos
    "listar_turnos": "ListarTurnosDisponibles",      # 2.3 Listar Turnos Disponibles
    "registrar_separacion": "RegistrarSeparacionQuirofano",  # 2.4 Registrar Separación de Quirófano
    "calcular_precio": "CalcularPrecioQuirofano",    # 2.5 Calcular Precio de Quirófano
}


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
    try:
        resp = requests.post(
            f"{g.lolcli_url}/{endpoint}",
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        data = resp.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"ERROR {endpoint}: {e}")
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
        sender = key["remoteJid"].split("@")[0]
        if key["fromMe"]:
            return jsonify({"status": "ignored_from_me"}), 200
        msg = data["data"]["message"]
        msg_id = key.get("id")
    except (KeyError, TypeError):
        return jsonify({"status": "ignored_format"}), 200

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

    if normalized == "retroceder" and session.get("state") not in ["START", "AWAITING_AUTH"]:
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
            "👋 ¡Bienvenido/a al sistema de reservas de quirófanos LOLIMSA!\n\n"
            "Para continuar, ingresa tu *código de médico (medcod)*:",
        )
        session["state"] = "AWAITING_AUTH"

    elif state == "AWAITING_AUTH":
        medcod = message_text.strip().upper()
        if not medcod:
            send_whatsapp_message(phone, "⚠️ Por favor, ingresa tu código de médico.")
        else:
            resp_data, err = _call_lolcli("validar_medico", {"medcod": medcod}, lolcli_headers)
            if err:
                send_whatsapp_message(phone, f"❌ {err}")
            else:
                medicos = resp_data.get("medico", [])
                if isinstance(medicos, dict):
                    medicos = [medicos]
                medicos = [m for m in medicos if m.get("valido", "S") == "S"]
                if medicos:
                    medico = medicos[0]
                    session["medcod"] = medico.get("medcod", medcod)
                    session["mednam"] = medico.get("mednam", medcod)
                    session["regesp"] = medico.get("regesp", "")
                    session.setdefault("history", []).append("AWAITING_AUTH")
                    send_whatsapp_message(phone, f"✅ ¡Hola, {session['mednam']}!")
                    show_main_menu(phone, session)
                else:
                    send_whatsapp_message(
                        phone,
                        "❌ No encontramos ese código de médico. Verifica e inténtalo de nuevo.",
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
        if normalized in ["si", "sí", "confirmar", "confirm"]:
            _confirm_booking(session, phone, lolcli_headers)
        elif normalized in ["no", "retroceder"]:
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
    if state == "AWAITING_QUIROFANO":
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
