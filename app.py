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

INACTIVITY_REMINDER_PERIOD = 5 * 60
SESSION_EXPIRATION_PERIOD = 15 * 60

DAYS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MONTHS_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def normalize_text(text):
    text = text.lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )
    text = text.replace(".", "").replace(",", "").replace("-", " ")
    return " ".join(text.split())


def format_date_es(date_obj):
    return f"{DAYS_ES[date_obj.weekday()]}, {date_obj.day:02d} de {MONTHS_ES[date_obj.month]}"


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

def format_menu(title, items, key_id, key_name):
    """Text fallback menu — returns (text, formatted_items)."""
    menu_text = f"{title}\n\n"
    formatted_items = []
    for i, item in enumerate(items, 1):
        display_name = item.get(key_name, "")
        if key_id == "fecha_api":
            try:
                display_name = format_date_es(datetime.strptime(item.get(key_id, ""), "%Y%m%d"))
            except (ValueError, TypeError):
                pass
        menu_text += f"*{i}.* {display_name}\n"
        formatted_items.append({"id": i, "data": item})
    menu_text += "\n_Escribe el número de tu elección o *'retroceder'* / *'salir'*._"
    return menu_text, formatted_items


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
                {"id": "menu_consultar", "title": "📋 Mis reservas",       "description": "Ver tus reservas activas"},
                {"id": "menu_cancelar",  "title": "❌ Cancelar reserva",   "description": "Anular una reserva existente"},
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
        sender = data["data"]["key"]["remoteJid"].split("@")[0]
        if data["data"]["key"]["fromMe"]:
            return jsonify({"status": "ignored_from_me"}), 200
        msg = data["data"]["message"]
    except (KeyError, TypeError):
        return jsonify({"status": "ignored_format"}), 200

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
    session = user_sessions.get(session_key, {"state": "START"})
    session["sender"] = sender
    session["clinic_id"] = clinic_id
    session["evolution_instance"] = g.evolution_instance
    session["last_interaction_time"] = time.time()
    if session.get("reminder_sent"):
        session["reminder_sent"] = False

    phone = sender
    print(f"[{clinic_id}] {sender}: '{message_text}' | id={selected_id}")

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
            try:
                # TODO: confirm endpoint name with LOLCLI team — try ValidarMedico first
                resp = requests.post(
                    f"{g.lolcli_url}/ValidarMedico",
                    json={"medcod": medcod},
                    headers=lolcli_headers,
                    timeout=8,
                )
                if resp.ok:
                    data_med = resp.json()
                    medicos = data_med.get("medicos") or data_med.get("medico") or []
                    if isinstance(medicos, dict):
                        medicos = [medicos]
                    if medicos:
                        medico = medicos[0]
                        session["medcod"] = medcod
                        session["mednam"] = medico.get("mednam") or medico.get("nombre") or medcod
                        session.setdefault("history", []).append("AWAITING_AUTH")
                        send_whatsapp_message(phone, f"✅ ¡Hola, {session['mednam']}!")
                        show_main_menu(phone, session)
                    else:
                        send_whatsapp_message(
                            phone,
                            "❌ No encontramos ese código de médico. Verifica e inténtalo de nuevo.",
                        )
                else:
                    raise Exception(f"HTTP {resp.status_code}")
            except Exception as e:
                print(f"ERROR ValidarMedico: {e}")
                send_whatsapp_message(
                    phone,
                    "😔 No pudimos verificar tu código en este momento. Intenta de nuevo o escribe *'ayuda'*. 🙏",
                )

    elif state == "AWAITING_MAIN_MENU":
        choice = selected_id or normalized
        if choice in ["menu_nueva", "1", "nueva reserva", "nueva", "reservar"]:
            _start_booking_flow(session, phone, lolcli_headers)
        elif choice in ["menu_consultar", "2", "mis reservas", "consultar"]:
            _start_consult_flow(session, phone, lolcli_headers)
        elif choice in ["menu_cancelar", "3", "cancelar reserva", "anular"]:
            _start_cancel_flow(session, phone, lolcli_headers)
        else:
            send_whatsapp_message(phone, "❓ Elige una opción del menú. 😊")

    elif state == "AWAITING_DATE":
        selected = _resolve_selection(message_text, selected_id, session)
        if selected:
            session.setdefault("history", []).append("AWAITING_DATE")
            session["fecha_api"] = selected["fecha_api"]
            session["fecha_user"] = selected["fecha_user"]
            _ask_quirofano(session, phone, lolcli_headers)
        else:
            send_whatsapp_message(phone, "❓ No reconocí esa fecha. Elige una de la lista.")

    elif state == "AWAITING_QUIROFANO":
        selected = _resolve_selection(message_text, selected_id, session)
        if selected:
            session.setdefault("history", []).append("AWAITING_QUIROFANO")
            session["quicod"] = selected["quicod"]
            session["quinam"] = selected["quinam"]
            _ask_time_block(session, phone, lolcli_headers)
        else:
            send_whatsapp_message(phone, "❓ No reconocí ese quirófano. Elige uno de la lista.")

    elif state == "AWAITING_TIME_BLOCK":
        selected = _resolve_selection(message_text, selected_id, session)
        if selected:
            session.setdefault("history", []).append("AWAITING_TIME_BLOCK")
            session["hora_api"] = selected["hora_api"]
            session["hora_user"] = selected["hora_user"]
            send_whatsapp_message(
                phone,
                "📋 Opcionalmente, indica el nombre del *procedimiento o cirugía* a realizar.\n"
                "_Escribe el nombre o *'omitir'* para continuar sin especificarlo._",
            )
            session["state"] = "AWAITING_PROCEDURE"
        else:
            send_whatsapp_message(phone, "❓ No reconocí ese horario. Elige uno de la lista.")

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

    elif state == "AWAITING_RESERVATION_TO_CANCEL":
        selected = _resolve_selection(message_text, selected_id, session)
        if selected:
            session["resid_to_cancel"] = selected.get("resid") or selected.get("id", "")
            session["res_summary"] = selected.get("summary", "")
            send_button_message(
                phone,
                f"¿Confirmas la cancelación de esta reserva?\n\n{session['res_summary']}",
                [{"id": "cancel_si", "title": "✅ Sí, cancelar"},
                 {"id": "cancel_no", "title": "↩️ No, volver"}],
            )
            session["state"] = "AWAITING_CANCEL_CONFIRMATION"
        else:
            send_whatsapp_message(phone, "❓ No reconocí esa opción. Elige una de la lista.")

    elif state == "AWAITING_CANCEL_CONFIRMATION":
        choice = selected_id or normalized
        if choice in ["cancel_si", "si", "sí"]:
            _confirm_cancellation(session, phone, lolcli_headers)
        else:
            send_whatsapp_message(phone, "↩️ Cancelación abortada. Escribe *'salir'* o vuelve al menú con *'hola'*.")
            session["state"] = "AWAITING_POST_FLOW"

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
    days = next_business_days(14)
    rows = []
    formatted = []
    for i, d in enumerate(days):
        fecha_api = d.strftime("%Y%m%d")
        fecha_user = format_date_es(d)
        row_id = f"date_{fecha_api}"
        rows.append({"id": row_id, "title": fecha_user, "description": ""})
        formatted.append({"id": i + 1, "data": {"_id": row_id, "fecha_api": fecha_api, "fecha_user": fecha_user}})
    session["options"] = formatted
    session["state"] = "AWAITING_DATE"
    send_list_message(
        phone,
        "Selecciona la *fecha* para la reserva:",
        sections=[{"title": "Fechas disponibles", "rows": rows}],
        title="Nueva reserva de quirófano",
        button_text="Ver fechas",
    )


def _ask_quirofano(session, phone, lolcli_headers):
    try:
        # TODO: confirm endpoint name with LOLCLI team
        resp = requests.post(
            f"{g.lolcli_url}/ListaQuirofanos",
            json={"siscod": g.default_siscod, "fecha": session["fecha_api"]},
            headers=lolcli_headers,
            timeout=8,
        )
        quirofanos = resp.json().get("quirofanos", []) if resp.ok else []
    except Exception as e:
        print(f"ERROR ListaQuirofanos: {e}")
        quirofanos = []

    if not quirofanos:
        send_whatsapp_message(
            phone,
            "😔 No hay quirófanos disponibles para esa fecha. Escribe *'retroceder'* para elegir otra.",
        )
        return

    rows = []
    formatted = []
    for i, q in enumerate(quirofanos):
        quicod = q.get("quicod") or q.get("id", str(i))
        quinam = q.get("quinam") or q.get("nombre") or f"Quirófano {i+1}"
        desc = q.get("descripcion") or q.get("tipo") or ""
        row_id = f"qui_{quicod}"
        rows.append({"id": row_id, "title": quinam, "description": desc})
        formatted.append({"id": i + 1, "data": {"_id": row_id, "quicod": quicod, "quinam": quinam}})

    session["options"] = formatted
    session["state"] = "AWAITING_QUIROFANO"
    send_list_message(
        phone,
        f"Quirófanos disponibles para *{session['fecha_user']}*:",
        sections=[{"title": "Quirófanos", "rows": rows}],
        button_text="Ver quirófanos",
    )


def _ask_time_block(session, phone, lolcli_headers):
    try:
        # TODO: confirm endpoint name with LOLCLI team
        resp = requests.post(
            f"{g.lolcli_url}/ListaHorasQuirofano",
            json={"quicod": session["quicod"], "fecha": session["fecha_api"]},
            headers=lolcli_headers,
            timeout=8,
        )
        horarios = resp.json().get("horarios", []) if resp.ok else []
    except Exception as e:
        print(f"ERROR ListaHorasQuirofano: {e}")
        horarios = []

    if not horarios:
        send_whatsapp_message(
            phone,
            "😔 No hay horarios disponibles para ese quirófano. Escribe *'retroceder'* para elegir otro.",
        )
        return

    rows = []
    formatted = []
    for i, h in enumerate(horarios):
        hora_raw = h.get("hora") or h.get("horinicio") or ""
        hora_fin = h.get("horafin") or ""
        try:
            hora_user = datetime.strptime(hora_raw, "%H%M").strftime("%H:%M")
            label = f"{hora_user}" + (f" – {datetime.strptime(hora_fin, '%H%M').strftime('%H:%M')}" if hora_fin else "")
        except (ValueError, TypeError):
            hora_user = hora_raw
            label = hora_raw
        row_id = f"hora_{hora_raw}_{i}"
        rows.append({"id": row_id, "title": label, "description": h.get("descripcion", "")})
        formatted.append({"id": i + 1, "data": {"_id": row_id, "hora_api": hora_raw, "hora_user": hora_user}})

    session["options"] = formatted
    session["state"] = "AWAITING_TIME_BLOCK"
    send_list_message(
        phone,
        f"Horarios disponibles en *{session['quinam']}*:",
        sections=[{"title": "Bloques horarios", "rows": rows}],
        button_text="Ver horarios",
    )


def _show_booking_summary(session, phone):
    proc = session.get("procedimiento") or "No especificado"
    summary = (
        f"📋 *Resumen de la reserva:*\n\n"
        f"👨‍⚕️ *Médico:* {session.get('mednam', '')}\n"
        f"🏥 *Quirófano:* {session.get('quinam', '')}\n"
        f"🗓️ *Fecha:* {session.get('fecha_user', '')}\n"
        f"⏰ *Hora:* {session.get('hora_user', '')}\n"
        f"🔬 *Procedimiento:* {proc}\n\n"
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
    try:
        fecref = datetime.strptime(
            session["fecha_api"] + session["hora_api"], "%Y%m%d%H%M"
        ).strftime("%d-%m-%Y %H:%M")

        payload = {
            "quicod": session["quicod"],
            "medcod": session["medcod"],
            "siscod": g.default_siscod,
            "fecref": fecref,
            "procedimiento": session.get("procedimiento", ""),
        }
        # TODO: confirm endpoint name with LOLCLI team
        resp = requests.post(
            f"{g.lolcli_url}/RegistroReservaQuirofano",
            json=payload,
            headers=lolcli_headers,
            timeout=10,
        )
        result = resp.json()
        if resp.ok and result.get("status") == "success":
            resid = result.get("resid") or result.get("id") or "—"
            session["resid"] = resid
            send_whatsapp_message(
                phone,
                f"✅ *¡Reserva confirmada!*\n\n"
                f"🆔 *N° de reserva:* {resid}\n"
                f"🏥 *Quirófano:* {session['quinam']}\n"
                f"🗓️ *Fecha:* {session['fecha_user']}\n"
                f"⏰ *Hora:* {session['hora_user']}\n\n"
                f"¡Hasta pronto! 🙏",
            )
            send_whatsapp_message(
                phone,
                "Escribe *'continuar'* para hacer otra reserva o *'salir'* para cerrar la sesión.",
            )
            session["state"] = "AWAITING_POST_FLOW"
        else:
            msg = result.get("message", "error desconocido")
            send_whatsapp_message(phone, f"❌ No se pudo registrar la reserva: {msg}. Intenta de nuevo.")
    except Exception as e:
        print(f"ERROR _confirm_booking: {e}")
        send_whatsapp_message(phone, "😔 Ocurrió un error al registrar la reserva. Intenta de nuevo. 🙏")


def _start_consult_flow(session, phone, lolcli_headers):
    try:
        # TODO: confirm endpoint name with LOLCLI team
        resp = requests.post(
            f"{g.lolcli_url}/ListaReservasQuirofano",
            json={"medcod": session["medcod"]},
            headers=lolcli_headers,
            timeout=8,
        )
        reservas = resp.json().get("reservas", []) if resp.ok else []
    except Exception as e:
        print(f"ERROR ListaReservasQuirofano: {e}")
        reservas = []

    if not reservas:
        send_whatsapp_message(phone, "📋 No tienes reservas activas en este momento. 😊")
        send_whatsapp_message(phone, "Escribe *'continuar'* para volver al menú.")
        session["state"] = "AWAITING_POST_FLOW"
        return

    msg = "📋 *Tus reservas activas:*\n\n"
    for i, r in enumerate(reservas, 1):
        fecha = r.get("fecha") or r.get("fecref") or ""
        hora = r.get("hora") or ""
        quinam = r.get("quinam") or r.get("quirofano") or ""
        proc = r.get("procedimiento") or ""
        msg += (
            f"*{i}.* 🏥 {quinam}\n"
            f"   🗓️ {fecha}  ⏰ {hora}\n"
            f"   🔬 {proc}\n\n"
        )
    msg += "_Escribe *'continuar'* para volver al menú._"
    send_whatsapp_message(phone, msg)
    session["state"] = "AWAITING_POST_FLOW"


def _start_cancel_flow(session, phone, lolcli_headers):
    try:
        # TODO: confirm endpoint name with LOLCLI team
        resp = requests.post(
            f"{g.lolcli_url}/ListaReservasQuirofano",
            json={"medcod": session["medcod"]},
            headers=lolcli_headers,
            timeout=8,
        )
        reservas = resp.json().get("reservas", []) if resp.ok else []
    except Exception as e:
        print(f"ERROR ListaReservasQuirofano (cancel): {e}")
        reservas = []

    if not reservas:
        send_whatsapp_message(phone, "📋 No tienes reservas activas para cancelar. 😊")
        send_whatsapp_message(phone, "Escribe *'continuar'* para volver al menú.")
        session["state"] = "AWAITING_POST_FLOW"
        return

    rows = []
    formatted = []
    for i, r in enumerate(reservas):
        resid = r.get("resid") or r.get("id") or str(i)
        quinam = r.get("quinam") or r.get("quirofano") or f"Reserva {i+1}"
        fecha = r.get("fecha") or r.get("fecref") or ""
        hora = r.get("hora") or ""
        row_id = f"res_{resid}"
        label = f"{quinam} — {fecha} {hora}".strip()
        rows.append({"id": row_id, "title": quinam, "description": f"{fecha} {hora}".strip()})
        formatted.append({
            "id": i + 1,
            "data": {"_id": row_id, "resid": resid, "quinam": quinam, "summary": label},
        })

    session["options"] = formatted
    session["state"] = "AWAITING_RESERVATION_TO_CANCEL"
    send_list_message(
        phone,
        "¿Cuál reserva deseas cancelar?",
        sections=[{"title": "Reservas activas", "rows": rows}],
        button_text="Ver reservas",
    )


def _confirm_cancellation(session, phone, lolcli_headers):
    try:
        # TODO: confirm endpoint name with LOLCLI team
        resp = requests.post(
            f"{g.lolcli_url}/AnularReservaQuirofano",
            json={"resid": session["resid_to_cancel"]},
            headers=lolcli_headers,
            timeout=10,
        )
        result = resp.json()
        if resp.ok and result.get("status") == "success":
            send_whatsapp_message(phone, "✅ Reserva cancelada exitosamente. 👋")
        else:
            msg = result.get("message", "error desconocido")
            send_whatsapp_message(phone, f"❌ No se pudo cancelar: {msg}.")
    except Exception as e:
        print(f"ERROR _confirm_cancellation: {e}")
        send_whatsapp_message(phone, "😔 Error al cancelar la reserva. Intenta de nuevo. 🙏")
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
    if state == "AWAITING_DATE":
        _start_booking_flow(session, phone, lolcli_headers)
    elif state == "AWAITING_QUIROFANO":
        _ask_quirofano(session, phone, lolcli_headers)
    elif state == "AWAITING_TIME_BLOCK":
        _ask_time_block(session, phone, lolcli_headers)
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
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)

