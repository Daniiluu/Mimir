#!/usr/bin/env python3
"""
mimir_gmail.py - Modulo de Gestion Inteligente de Gmail para Mimir
Funcionalidades:
  - Monitorizacion continua del buzon de Gmail (polling cada 60 segundos).
  - Clasificacion por IA (Anthropic Claude) de cada email recibido.
  - Notificaciones por Telegram con botones de accion rapida.
  - Creacion de borradores de respuesta en Gmail.
  - Creacion de eventos en Google Calendar.
Dependencias adicionales:
  google-auth>=2.28.0
  google-auth-oauthlib>=1.2.0
  google-auth-httplib2>=0.2.0
  google-api-python-client>=2.120.0
"""

import os
import re
import json
import base64
import logging
import asyncio
import datetime
import email.mime.text
from email.header import decode_header
from typing import Optional, Dict, Any, List

import requests
from dotenv import load_dotenv
from anthropic import Anthropic

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv(override=True)

logger = logging.getLogger("MimirGmail")

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL    = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# TELEGRAM_CHAT_ID se lee dinámicamente en cada llamada (por si cambia en .env en caliente)

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
]

_BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH   = os.path.join(_BASE_DIR, "credentials.json")
TOKEN_PATH         = os.path.join(_BASE_DIR, "gmail_token.json")
PROCESSED_IDS_PATH = os.path.join(_BASE_DIR, ".gmail_processed_ids.json")

POLL_INTERVAL_SECONDS = 60

CLASS_IMPORTANTE = "IMPORTANTE"
CLASS_SPAM       = "SPAM"
CLASS_PROMO      = "PROMOCION"
CLASS_CITA       = "CITA"
CLASS_INFO       = "INFORMATIVO"

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

_pending_drafts: Dict[str, Dict[str, Any]] = {}
# Mapeo user_id (int) -> message_id (str): saber qué borrador está editando cada usuario
_active_edits: Dict[int, str] = {}


# == 1. AUTENTICACION GOOGLE ===================================================
def get_google_credentials() -> Credentials:
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Renovando token de Google...")
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"No se encontro credentials.json en {CREDENTIALS_PATH}. "
                    "Descargalo desde Google Cloud Console -> APIs & Services -> Credentials."
                )
            logger.info("Iniciando flujo OAuth2 de Google en el navegador...")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
        logger.info(f"Token guardado en {TOKEN_PATH}")

    return creds


def get_gmail_service():
    return build("gmail", "v1", credentials=get_google_credentials())


def get_calendar_service():
    return build("calendar", "v3", credentials=get_google_credentials())


# == 2. PERSISTENCIA DE IDS PROCESADOS ========================================
def cargar_ids_procesados() -> set:
    if os.path.exists(PROCESSED_IDS_PATH):
        try:
            with open(PROCESSED_IDS_PATH, "r") as f:
                return set(json.load(f).get("ids", []))
        except Exception:
            pass
    return set()


def guardar_ids_procesados(ids: set) -> None:
    with open(PROCESSED_IDS_PATH, "w") as f:
        json.dump({"ids": list(ids)[-500:]}, f)


# == 3. LECTURA Y DECODIFICACION DE EMAILS =====================================
def decodificar_header(valor: str) -> str:
    partes = decode_header(valor)
    resultado = []
    for parte, enc in partes:
        if isinstance(parte, bytes):
            resultado.append(parte.decode(enc or "utf-8", errors="replace"))
        else:
            resultado.append(parte)
    return "".join(resultado)


def extraer_cuerpo(payload: dict) -> str:
    def _rec(p: dict) -> str:
        mime = p.get("mimeType", "")
        body = p.get("body", {})
        parts = p.get("parts", [])
        if parts:
            for sub in parts:
                t = _rec(sub)
                if t:
                    return t
        if mime == "text/plain" and body.get("data"):
            return base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
        if mime == "text/html" and body.get("data"):
            raw = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
            return " ".join(re.sub(r"<[^>]+>", "", raw).split())
        return ""
    return _rec(payload)


def obtener_email_completo(service, message_id: str) -> Optional[Dict[str, Any]]:
    try:
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        cuerpo = extraer_cuerpo(msg["payload"])
        return {
            "id": message_id,
            "thread_id": msg.get("threadId", ""),
            "remitente": decodificar_header(headers.get("From", "Desconocido")),
            "asunto": decodificar_header(headers.get("Subject", "(Sin asunto)")),
            "fecha": headers.get("Date", ""),
            "cuerpo": cuerpo[:3000] + ("..." if len(cuerpo) > 3000 else ""),
        }
    except HttpError as e:
        logger.error(f"[Gmail] Error obteniendo email {message_id}: {e}")
        return None


# == 4. CLASIFICACION CON IA ===================================================
def clasificar_email_con_ia(email_data: Dict[str, Any]) -> Dict[str, Any]:
    prompt = f"""Analiza este email y responde UNICAMENTE con JSON valido (sin markdown, sin texto extra):

Email:
- De: {email_data['remitente']}
- Asunto: {email_data['asunto']}
- Fecha: {email_data['fecha']}
- Cuerpo:
{email_data['cuerpo']}

Devuelve exactamente este JSON:
{{
  "categoria": "IMPORTANTE|SPAM|PROMOCION|CITA|INFORMATIVO",
  "resumen": "Resumen de 1-2 frases del email",
  "requiere_respuesta": true,
  "borrador_respuesta": "Borrador de respuesta en espanol (vacio si no aplica)",
  "es_cita": false,
  "cita_titulo": "Titulo del evento para Calendar (vacio si no es cita)",
  "cita_fecha_hora": "ISO 8601 ej: 2026-10-10T18:00:00 (vacio si no se puede determinar)",
  "cita_duracion_horas": 1,
  "cita_descripcion": "Descripcion del evento"
}}

Criterios:
- SPAM: No solicitados, phishing, fraude, loteria.
- PROMOCION: Marketing, newsletters, ofertas de tiendas, publicidad.
- CITA: Menciona reunion, cita medica, quedada o evento con fecha/hora.
- IMPORTANTE: Persona real que requiere atencion o respuesta.
- INFORMATIVO: Notificaciones de sistema, facturas, confirmaciones sin accion requerida."""

    try:
        response = anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        texto = response.content[0].text.strip()
        texto = re.sub(r"^```(?:json)?\s*", "", texto, flags=re.MULTILINE)
        texto = re.sub(r"\s*```$", "", texto, flags=re.MULTILINE)
        resultado = json.loads(texto)
        logger.info(f"[Gmail-IA] '{email_data['asunto'][:50]}' => {resultado.get('categoria')}")
        return resultado
    except Exception as e:
        logger.error(f"[Gmail-IA] Error clasificando: {e}")
        return {
            "categoria": CLASS_IMPORTANTE,
            "resumen": "Email recibido (error al clasificar).",
            "requiere_respuesta": False,
            "borrador_respuesta": "",
            "es_cita": False,
            "cita_titulo": "",
            "cita_fecha_hora": "",
            "cita_duracion_horas": 1,
            "cita_descripcion": "",
        }


# == 5. ACCIONES SOBRE GMAIL ===================================================
def mover_a_papelera(service, message_id: str) -> bool:
    try:
        service.users().messages().trash(userId="me", id=message_id).execute()
        logger.info(f"[Gmail] {message_id} movido a papelera.")
        return True
    except HttpError as e:
        logger.error(f"[Gmail] Error papelera {message_id}: {e}")
        return False


def crear_borrador(service, destinatario: str, asunto: str, cuerpo: str, thread_id: str) -> Optional[str]:
    try:
        msg = email.mime.text.MIMEText(cuerpo, "plain", "utf-8")
        msg["To"] = destinatario
        msg["Subject"] = asunto if asunto.lower().startswith("re:") else f"Re: {asunto}"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        draft = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw, "threadId": thread_id}}
        ).execute()
        draft_id = draft.get("id")
        logger.info(f"[Gmail] Borrador creado: {draft_id}")
        return draft_id
    except HttpError as e:
        logger.error(f"[Gmail] Error creando borrador: {e}")
        return None


def enviar_borrador(service, draft_id: str) -> bool:
    try:
        service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        logger.info(f"[Gmail] Borrador {draft_id} enviado.")
        return True
    except HttpError as e:
        logger.error(f"[Gmail] Error enviando borrador {draft_id}: {e}")
        return False


def eliminar_borrador(service, draft_id: str) -> bool:
    try:
        service.users().drafts().delete(userId="me", id=draft_id).execute()
        return True
    except HttpError as e:
        logger.error(f"[Gmail] Error eliminando borrador {draft_id}: {e}")
        return False


# == 6. GOOGLE CALENDAR ========================================================
def crear_evento_calendar(
    titulo: str,
    fecha_hora_inicio: str,
    duracion_horas: float = 1.0,
    descripcion: str = ""
) -> Optional[str]:
    try:
        cal = get_calendar_service()
        try:
            dt_inicio = datetime.datetime.fromisoformat(fecha_hora_inicio)
        except ValueError:
            try:
                dt_inicio = datetime.datetime.fromisoformat(fecha_hora_inicio + "T09:00:00")
            except ValueError:
                dt_inicio = (datetime.datetime.now() + datetime.timedelta(days=1)).replace(
                    hour=9, minute=0, second=0, microsecond=0
                )

        dt_fin = dt_inicio + datetime.timedelta(hours=duracion_horas)
        evento = {
            "summary": titulo,
            "description": descripcion,
            "start": {"dateTime": dt_inicio.isoformat(), "timeZone": "Europe/Madrid"},
            "end": {"dateTime": dt_fin.isoformat(), "timeZone": "Europe/Madrid"},
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]},
        }
        creado = cal.events().insert(calendarId="primary", body=evento).execute()
        link = creado.get("htmlLink", "")
        logger.info(f"[Calendar] Evento creado: '{titulo}' => {link}")
        return link
    except Exception as e:
        logger.error(f"[Calendar] Error: {e}")
        return None


# == 7. NOTIFICACIONES TELEGRAM (HTTP directo, sincrono) =======================
def enviar_notificacion_telegram(texto: str, reply_markup: Optional[dict] = None) -> bool:
    # Leer siempre en el momento de la llamada para capturar cambios en .env
    bot_token  = TELEGRAM_BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id    = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()

    if not bot_token or not chat_id:
        logger.warning(
            "[Gmail] Notificacion no enviada: TELEGRAM_BOT_TOKEN o TELEGRAM_ALLOWED_USER_ID "
            f"no configurados. chat_id='{chat_id}' token_ok={bool(bot_token)}"
        )
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info(f"[Telegram] Notificacion enviada a chat_id={chat_id}")
            return True
        else:
            logger.error(f"[Telegram] Error HTTP {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"[Telegram] Excepcion al enviar notificacion: {e}")
        return False


def _teclado_importante(message_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Enviar respuesta", "callback_data": f"gmail_send_{message_id}"},
                {"text": "✏️ Ver borrador",    "callback_data": f"gmail_edit_{message_id}"},
            ],
            [
                {"text": "🗑️ Eliminar email",  "callback_data": f"gmail_trash_{message_id}"},
                {"text": "🔕 Ignorar",          "callback_data": f"gmail_ignore_{message_id}"},
            ],
        ]
    }


def _teclado_cita(message_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Enviar respuesta",    "callback_data": f"gmail_send_{message_id}"},
                {"text": "✏️ Ver borrador",        "callback_data": f"gmail_edit_{message_id}"},
            ],
            [
                {"text": "🗑️ Eliminar email",     "callback_data": f"gmail_trash_{message_id}"},
                {"text": "🔕 Ignorar",             "callback_data": f"gmail_ignore_{message_id}"},
            ],
        ]
    }


# == 8. PROCESADOR PRINCIPAL DE EMAILS =========================================
def procesar_email_nuevo(service, email_data: Dict[str, Any]) -> None:
    clf           = clasificar_email_con_ia(email_data)
    categoria     = clf.get("categoria", CLASS_IMPORTANTE).upper()
    resumen       = clf.get("resumen", "")
    message_id    = email_data["id"]
    remitente_raw = email_data["remitente"]
    remitente_display = remitente_raw.split("<")[0].strip() or remitente_raw
    # Vista previa del cuerpo (primeras 400 chars)
    cuerpo_preview = email_data.get("cuerpo", "")[:400]
    if len(email_data.get("cuerpo", "")) > 400:
        cuerpo_preview += "..."

    # Spam / Promo => papelera silenciosa
    if categoria in (CLASS_SPAM, CLASS_PROMO):
        mover_a_papelera(service, message_id)
        logger.info(f"[Gmail] '{remitente_display}' ({categoria}) => papelera silenciosa.")
        # Notificacion discreta de lo que se ha descartado
        enviar_notificacion_telegram(
            f"🗑️ Email de *{remitente_display}* identificado como {categoria.lower()} y movido a la papelera automáticamente, señor."
        )
        return

    # Cita => crear borrador + evento Calendar + notificar todo
    if categoria == CLASS_CITA or clf.get("es_cita", False):
        cita_titulo   = clf.get("cita_titulo", email_data["asunto"])
        cita_fecha    = clf.get("cita_fecha_hora", "")
        cita_desc     = clf.get("cita_descripcion", "")
        cita_duracion = clf.get("cita_duracion_horas", 1)
        borrador_texto = clf.get("borrador_respuesta", "")

        # Crear borrador de respuesta
        draft_id = None
        if borrador_texto:
            draft_id = crear_borrador(
                service,
                destinatario=remitente_raw,
                asunto=email_data["asunto"],
                cuerpo=borrador_texto,
                thread_id=email_data["thread_id"],
            )

        # Intentar crear evento en Calendar automáticamente
        cal_link = None
        cal_estado = ""
        if cita_fecha:
            cal_link = crear_evento_calendar(cita_titulo, cita_fecha, cita_duracion, cita_desc)
            if cal_link:
                cal_estado = f"\n\n📅 *Evento añadido automáticamente a su Calendar:* {cita_titulo}"
            else:
                cal_estado = "\n\n⚠️ No pude determinar la fecha exacta del evento. Indíqueme cuándo es para agendarlo."
        else:
            cal_estado = "\n\n⚠️ No pude determinar la fecha exacta del evento. Indíqueme cuándo es para agendarlo."

        _pending_drafts[message_id] = {
            "type":              "cita",
            "draft_id":         draft_id,
            "thread_id":        email_data["thread_id"],
            "from":             remitente_raw,
            "subject":          email_data["asunto"],
            "resumen":          resumen,
            "draft_body":       borrador_texto,
            "cita_titulo":      cita_titulo,
            "cita_fecha_hora":  cita_fecha,
            "cita_duracion_horas": cita_duracion,
            "cita_descripcion": cita_desc,
        }

        seccion_borrador = ""
        if draft_id and borrador_texto:
            seccion_borrador = f"\n\n✏️ *Borrador de respuesta preparado:*\n_{borrador_texto[:400]}{'...' if len(borrador_texto) > 400 else ''}_"

        mensaje = (
            f"📅 *Email de cita recibido, señor*\n\n"
            f"👤 *De:* {remitente_display}\n"
            f"📋 *Asunto:* {email_data['asunto']}\n\n"
            f"📝 *Resumen:* {resumen}\n"
            f"💬 *Contenido:* _{cuerpo_preview}_"
            f"{cal_estado}"
            f"{seccion_borrador}\n\n"
            f"{'¿Envío la respuesta o prefiere revisarla primero?' if draft_id else '¿Qué desea hacer con este email?'}"
        )
        enviar_notificacion_telegram(mensaje, reply_markup=_teclado_cita(message_id))
        return

    # Informativo => notificacion simple sin botones
    if categoria == CLASS_INFO:
        mensaje = (
            f"ℹ️ *Notificación informativa en su buzón, señor*\n\n"
            f"👤 *De:* {remitente_display}\n"
            f"📋 *Asunto:* {email_data['asunto']}\n\n"
            f"📝 *Resumen:* {resumen}\n"
            f"💬 *Contenido:* _{cuerpo_preview}_"
        )
        enviar_notificacion_telegram(mensaje)
        return

    # Importante => borrador + botones de acción
    borrador_texto = clf.get("borrador_respuesta", "")
    draft_id = None
    if borrador_texto:
        draft_id = crear_borrador(
            service,
            destinatario=remitente_raw,
            asunto=email_data["asunto"],
            cuerpo=borrador_texto,
            thread_id=email_data["thread_id"],
        )

    _pending_drafts[message_id] = {
        "type":       "importante",
        "draft_id":   draft_id,
        "thread_id":  email_data["thread_id"],
        "from":       remitente_raw,
        "subject":    email_data["asunto"],
        "resumen":    resumen,
        "draft_body": borrador_texto,
    }

    seccion_borrador = ""
    if borrador_texto:
        seccion_borrador = (
            f"\n\n✏️ *Borrador de respuesta que he preparado:*\n"
            f"_{borrador_texto[:500]}{'...' if len(borrador_texto) > 500 else ''}_"
        )

    mensaje = (
        f"📬 *Nuevo email importante, señor*\n\n"
        f"👤 *De:* {remitente_display}\n"
        f"📋 *Asunto:* {email_data['asunto']}\n\n"
        f"📝 *Resumen:* {resumen}\n"
        f"💬 *Contenido:* _{cuerpo_preview}_"
        f"{seccion_borrador}\n\n"
        f"{'¿Envío la respuesta o prefiere revisarla primero, señor?' if draft_id else '¿Qué hacemos con este email, señor?'}"
    )
    enviar_notificacion_telegram(mensaje, reply_markup=_teclado_importante(message_id))


# == 9. CALLBACK HANDLER (registrar en mimir_bot.py) ===========================
async def manejar_callback_gmail(update, context) -> None:
    """
    Maneja callbacks de botones inline relacionados con Gmail.
    Registrar en mimir_bot.py:
      from telegram.ext import CallbackQueryHandler
      app.add_handler(CallbackQueryHandler(manejar_callback_gmail, pattern=r'^gmail_'))
    """
    query = update.callback_query
    await query.answer()

    data  = query.data
    parts = data.split("_", 2)

    if len(parts) < 3 or parts[0] != "gmail":
        return

    accion     = parts[1]
    message_id = parts[2]
    info       = _pending_drafts.get(message_id)

    if not info:
        await query.edit_message_text(
            "No encontre informacion sobre este email en mi memoria, senor. "
            "Es posible que ya haya sido procesado."
        )
        return

    try:
        service = await asyncio.to_thread(get_gmail_service)
    except Exception as e:
        await query.edit_message_text(f"Error al conectar con Gmail: {e}")
        return

    if accion == "send":
        draft_id = info.get("draft_id")
        if draft_id and await asyncio.to_thread(enviar_borrador, service, draft_id):
            await query.edit_message_text(
                f"Respuesta enviada a {info.get('from', 'destinatario')}, senor.\n"
                f"Asunto: {info.get('subject', '')}"
            )
        else:
            await query.edit_message_text(
                "No pude enviar el borrador, senor. Puede que ya no exista en Gmail."
            )
        _pending_drafts.pop(message_id, None)

    elif accion == "edit":
        user_id  = update.effective_user.id if update.effective_user else None
        borrador = info.get("draft_body", "")
        remitente_display = info.get("from", "").split("<")[0].strip() or info.get("from", "")

        # Guardar que este usuario está editando este borrador
        if user_id:
            _active_edits[user_id] = message_id
            logger.info(f"[Gmail] Usuario {user_id} entrando en modo edición del borrador {message_id}")

        await query.edit_message_text(
            f"✏️ *Borrador actual para '{info.get('subject', '')}'* (de {remitente_display}):\n\n"
            f"`{borrador[:800]}`\n\n"
            "Dígame qué desea cambiar o escríbame directamente cómo quiere que quede el mensaje, señor, y lo actualizaré ahora mismo.",
            parse_mode="Markdown"
        )
        # NO eliminar de _pending_drafts — necesitamos el contexto para la edición

    elif accion == "trash":
        await asyncio.to_thread(mover_a_papelera, service, message_id)
        draft_id = info.get("draft_id")
        if draft_id:
            await asyncio.to_thread(eliminar_borrador, service, draft_id)
        await query.edit_message_text(
            f"Email de {info.get('from', 'remitente')} eliminado, senor."
        )
        _pending_drafts.pop(message_id, None)

    elif accion == "cal":
        titulo   = info.get("cita_titulo", info.get("subject", "Evento"))
        fecha    = info.get("cita_fecha_hora", "")
        desc     = info.get("cita_descripcion", "")
        duracion = info.get("cita_duracion_horas", 1)

        if not fecha:
            await query.edit_message_text(
                f"No pude determinar una fecha/hora exacta para {titulo}. "
                "Indicame cuando es y lo agendare inmediatamente, senor."
            )
            return

        link = await asyncio.to_thread(crear_evento_calendar, titulo, fecha, duracion, desc)
        if link:
            await query.edit_message_text(
                f"Evento '{titulo}' anadido a su Google Calendar, senor.\n{link}"
            )
        else:
            await query.edit_message_text(
                "No pude crear el evento en Calendar, senor. "
                "Verifique los permisos del token de Google."
            )
        _pending_drafts.pop(message_id, None)

    elif accion == "ignore":
        await query.edit_message_text(
            f"De acuerdo, senor. El email de {info.get('from', 'remitente')} "
            "permanece en su bandeja sin modificacion."
        )
        _pending_drafts.pop(message_id, None)


# == 10. BUCLE DE MONITORIZACION ASINCRONO =====================================
async def bucle_monitorizar_gmail() -> None:
    """
    Tarea asincrona de fondo que monitoriza el buzon de Gmail.
    Arrancar con: asyncio.create_task(bucle_monitorizar_gmail())
    desde post_init de la Application de python-telegram-bot.
    """
    logger.info(f"[Gmail Monitor] Iniciando monitorizacion (intervalo: {POLL_INTERVAL_SECONDS}s).")

    # Verificacion inicial de configuracion
    chat_id_inicial = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
    if not chat_id_inicial:
        logger.warning(
            "[Gmail Monitor] TELEGRAM_ALLOWED_USER_ID no configurado en .env. "
            "Las notificaciones automaticas de Gmail no llegaran hasta que se configure."
        )
    else:
        logger.info(f"[Gmail Monitor] Notificaciones se enviaran al chat_id: {chat_id_inicial}")

    ids_procesados = cargar_ids_procesados()
    primer_arranque = True

    while True:
        try:
            service   = await asyncio.to_thread(get_gmail_service)
            email_ids = await asyncio.to_thread(_listar_no_leidos, service, 20)
            nuevos    = [eid for eid in email_ids if eid not in ids_procesados]

            if primer_arranque:
                ids_procesados.update(email_ids)
                guardar_ids_procesados(ids_procesados)
                logger.info(f"[Gmail Monitor] Primer arranque: {len(email_ids)} emails marcados como procesados (no se notificara retroactivamente).")
                primer_arranque = False
            elif nuevos:
                logger.info(f"[Gmail Monitor] {len(nuevos)} email(s) nuevo(s).")
                for eid in nuevos:
                    edata = await asyncio.to_thread(obtener_email_completo, service, eid)
                    if edata:
                        await asyncio.to_thread(procesar_email_nuevo, service, edata)
                    ids_procesados.add(eid)
                guardar_ids_procesados(ids_procesados)
            else:
                logger.debug("[Gmail Monitor] Sin emails nuevos.")

        except Exception as e:
            logger.error(f"[Gmail Monitor] Error en ciclo: {e}", exc_info=True)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _listar_no_leidos(service, max_results: int = 20) -> List[str]:
    try:
        res = service.users().messages().list(
            userId="me",
            labelIds=["INBOX", "UNREAD"],
            maxResults=max_results,
        ).execute()
        return [m["id"] for m in res.get("messages", [])]
    except HttpError as e:
        logger.error(f"[Gmail] Error listando emails: {e}")
        return []


# == 11. TOOLS PARA CLAUDE (mimir_bot.py las importara) ========================
def gmail_enviar_email(destinatario: str, asunto: str, cuerpo: str) -> str:
    """Envia un email directamente desde el chat de Telegram."""
    try:
        service = get_gmail_service()
        msg = email.mime.text.MIMEText(cuerpo, "plain", "utf-8")
        msg["To"] = destinatario
        msg["Subject"] = asunto
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Email enviado a '{destinatario}' con asunto '{asunto}', senor."
    except Exception as e:
        return f"Error al enviar el email: {str(e)}"


def gmail_listar_no_leidos(max_resultados: int = 5) -> str:
    """Lista los ultimos emails no leidos de la bandeja de entrada."""
    try:
        service = get_gmail_service()
        ids = _listar_no_leidos(service, max_resultados)
        if not ids:
            return "No hay emails no leidos en su bandeja de entrada, senor."
        lineas = []
        for eid in ids:
            edata = obtener_email_completo(service, eid)
            if edata:
                de = edata["remitente"].split("<")[0].strip()
                lineas.append(f"- {edata['asunto']} (De: {de})")
        return "Emails no leidos, senor:\n" + "\n".join(lineas) if lineas else "Sin emails para mostrar."
    except Exception as e:
        return f"Error al listar emails: {str(e)}"


def gmail_buscar_email(query: str) -> str:
    """Busca emails por remitente, asunto o contenido."""
    try:
        service = get_gmail_service()
        res = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
        mensajes = res.get("messages", [])
        if not mensajes:
            return f"No encontre emails que coincidan con '{query}', senor."
        lineas = []
        for m in mensajes:
            edata = obtener_email_completo(service, m["id"])
            if edata:
                de = edata["remitente"].split("<")[0].strip()
                lineas.append(f"- {edata['asunto']} (De: {de})")
        return f"Resultados para '{query}':\n" + "\n".join(lineas)
    except Exception as e:
        return f"Error al buscar emails: {str(e)}"


# == 12. FUNCIONES DE CONTEXTO DE EDICION (usadas por mimir_bot.py) ============
def get_active_edit_context(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Devuelve el contexto completo del borrador que el usuario está editando, o None si no hay edición activa.
    mimir_bot.py llama a esta función antes de enviar a Claude para inyectar el contexto.
    """
    message_id = _active_edits.get(user_id)
    if not message_id:
        return None
    info = _pending_drafts.get(message_id)
    if not info:
        _active_edits.pop(user_id, None)
        return None
    return {
        "message_id":  message_id,
        "draft_id":    info.get("draft_id"),
        "from":        info.get("from", ""),
        "subject":     info.get("subject", ""),
        "draft_body":  info.get("draft_body", ""),
        "thread_id":   info.get("thread_id", ""),
    }


def clear_active_edit(user_id: int) -> None:
    """Limpia el estado de edición activo de un usuario."""
    _active_edits.pop(user_id, None)


def gmail_actualizar_y_enviar_borrador(
    message_id: str,
    nuevo_cuerpo: str,
    enviar: bool = False
) -> str:
    """
    Reescribe el cuerpo del borrador de Gmail con el nuevo texto proporcionado
    y opcionalmente lo envía si enviar=True.
    Claude debe llamar a esta tool cuando el usuario quiera modificar un borrador existente.
    """
    info = _pending_drafts.get(message_id)
    if not info:
        return f"No encontré el borrador con ID {message_id} en mi memoria, señor. Es posible que ya haya sido procesado."

    try:
        service = get_gmail_service()

        # Eliminar el borrador anterior si existe
        draft_id_viejo = info.get("draft_id")
        if draft_id_viejo:
            try:
                eliminar_borrador(service, draft_id_viejo)
            except Exception:
                pass  # Si ya no existe, no importa

        # Crear nuevo borrador con el cuerpo actualizado
        nuevo_draft_id = crear_borrador(
            service,
            destinatario=info.get("from", ""),
            asunto=info.get("subject", ""),
            cuerpo=nuevo_cuerpo,
            thread_id=info.get("thread_id", ""),
        )

        if not nuevo_draft_id:
            return "No pude crear el borrador actualizado en Gmail, señor."

        # Actualizar el estado en memoria
        info["draft_id"]   = nuevo_draft_id
        info["draft_body"] = nuevo_cuerpo
        _pending_drafts[message_id] = info

        if enviar:
            if enviar_borrador(service, nuevo_draft_id):
                _pending_drafts.pop(message_id, None)
                return (
                    f"Borrador actualizado y enviado con éxito a {info.get('from', '').split('<')[0].strip()}, señor. "
                    f"Asunto: '{info.get('subject', '')}'."
                )
            else:
                return "Borrador actualizado pero no pude enviarlo. Puede intentar enviarlo desde Gmail directamente, señor."
        else:
            return (
                f"Borrador actualizado correctamente, señor. El nuevo texto es:\n\n{nuevo_cuerpo}\n\n"
                "¿Lo envío tal como está o desea algún ajuste adicional?"
            )

    except Exception as e:
        return f"Error al actualizar el borrador: {str(e)}"

