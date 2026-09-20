#!/usr/bin/env python3
"""
mimir_bot.py — Orquestador Inteligente Multi-Dominio de Mimir para Telegram
=============================================================================
Arquitectura:
  - Framework Bot: python-telegram-bot (v20+ async)
  - Motor Cognitivo: Anthropic Claude (claude-haiku-4-5-20251001) con Tool Calling
  - Dominios de Orquestación:
      1. Chronos (Vida y Productividad): Notion, tareas, notas y organización.
      2. Domus (Domótica y Sistemas): Home Assistant REST API.
      3. Mercurio (Negocio y Dropshipping): Búsqueda en internet, tendencias y copy.

Autor: Arquitectura de Agentes Mimir
"""

import os
import sys
import json
import logging
import asyncio
import datetime
import urllib.parse
from typing import Dict, Any, List, Optional

import requests
from dotenv import load_dotenv
from anthropic import Anthropic
# pyrefly: ignore [missing-import]
from telegram import Update
# pyrefly: ignore [missing-import]
from telegram.constants import ChatAction
# pyrefly: ignore [missing-import]
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ── 1. CONFIGURACIÓN Y VARIABLES DE ENTORNO ────────────────────────────────────
load_dotenv()

# Logging estructurado
logging.basicConfig(
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("MimirOrchestrator")

# Credenciales principales
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip()
TELEGRAM_ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()

# Domus (Home Assistant)
HA_URL = os.getenv("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.getenv("HA_TOKEN", "").strip()

# Chronos (Notion)
NOTION_TOKEN = (os.getenv("NOTION_TOKEN") or os.getenv("NOTION_API_KEY", "")).strip()
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "").replace("-", "").strip()

# Validaciones críticas de arranque
if not TELEGRAM_BOT_TOKEN:
    logger.critical("❌ FALTA TELEGRAM_BOT_TOKEN en el archivo .env")
    sys.exit(1)

if not ANTHROPIC_API_KEY:
    logger.critical("❌ FALTA ANTHROPIC_API_KEY en el archivo .env")
    sys.exit(1)

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Historial de conversación en memoria por usuario (sliding window)
conversaciones_usuarios: Dict[int, List[Dict[str, Any]]] = {}
MAX_HISTORIAL_TURNOS = 10


# ── 2. MEMORIA PERSISTENTE (SQLite Opcional) ──────────────────────────────────
def cargar_memoria_local_sqlite() -> str:
    """Carga contexto personal persistente de mimir.db si existe."""
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mimir.db")
    if not os.path.exists(db_path):
        return "Sin memoria local previa registrada."
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT clave, valor FROM memoria")
        filas = cursor.fetchall()
        conn.close()
        if not filas:
            return "Sin memoria local previa registrada."
        return "\n".join(f"- {c}: {v}" for c, v in filas)
    except Exception as e:
        logger.warning(f"No se pudo consultar mimir.db: {e}")
        return "Error al acceder a mimir.db"


# ── 3. HERRAMIENTAS ESPECIALIZADAS (TOOLS IMPLEMENTATION) ─────────────────────

# ── 3.1. DOMUS: Control Domótico (Home Assistant) ──
def controlar_dispositivo_ha(domain: str, service: str, entity_id: str, extra_data: Optional[Dict[str, Any]] = None) -> str:
    """
    Ejecuta llamadas a servicios en Home Assistant mediante su API REST.
    Ejemplo: domain='light', service='turn_on', entity_id='light.salon'
    """
    if not HA_TOKEN:
        return "Aviso de Domus: HA_TOKEN no está configurado en las variables de entorno, señor."

    url = f"{HA_URL}/api/services/{domain}/{service}"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {"entity_id": entity_id}
    if extra_data and isinstance(extra_data, dict):
        payload.update(extra_data)

    try:
        logger.info(f"⚡ [Domus] Invocando {domain}.{service} sobre {entity_id}")
        resp = requests.post(url, headers=headers, json=payload, timeout=8)
        if resp.status_code in (200, 201):
            return f"Acción domótica ejecutada con éxito: {domain}.{service} en '{entity_id}'."
        else:
            return f"Home Assistant respondió con error ({resp.status_code}): {resp.text}"
    except requests.exceptions.RequestException as e:
        logger.error(f"[Domus] Error de conexión: {e}")
        return f"Error de comunicación con Home Assistant: {str(e)}"


# ── 3.2. CHRONOS: Gestión y Productividad (Notion) ──
def guardar_en_notion(titulo: str, contenido: str = "", tipo: str = "Tarea") -> str:
    """
    Crea una nueva página/entrada en Notion con título y contenido estructurado.
    """
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        return "Aviso de Chronos: NOTION_TOKEN o NOTION_DATABASE_ID no configurados en .env, señor."

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    # Inspeccionar esquema de base de datos para detectar propiedad de título
    try:
        db_info = requests.get(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}",
            headers=headers,
            timeout=8
        ).json()
        props_schema = db_info.get("properties", {})
        title_prop_name = next((k for k, v in props_schema.items() if v.get("type") == "title"), "Name")
    except Exception:
        title_prop_name = "Name"

    timestamp_iso = datetime.date.today().isoformat()
    properties_payload: Dict[str, Any] = {
        title_prop_name: {
            "title": [{"text": {"content": titulo}}]
        }
    }

    # Si la base de datos tiene columna 'Tipo' y 'Fecha', enriquecer payload
    if "Tipo" in props_schema and props_schema["Tipo"].get("type") == "select":
        properties_payload["Tipo"] = {"select": {"name": tipo}}
    if "Fecha" in props_schema and props_schema["Fecha"].get("type") == "date":
        properties_payload["Fecha"] = {"date": {"start": timestamp_iso}}

    body: Dict[str, Any] = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": properties_payload,
    }

    if contenido:
        body["children"] = [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": contenido[:2000]}}
                    ]
                }
            }
        ]

    try:
        logger.info(f"📋 [Chronos] Guardando en Notion: '{titulo}'")
        resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=body, timeout=10)
        data = resp.json()
        if resp.status_code in (200, 201):
            return f"Registrado con éxito en Notion bajo el título '{titulo}' ({tipo})."
        else:
            return f"Notion devolvió un error: {data.get('message', resp.text)}"
    except requests.exceptions.RequestException as e:
        logger.error(f"[Chronos] Error con Notion: {e}")
        return f"Error al conectar con Notion: {str(e)}"


# ── 3.3. MERCURIO: Inteligencia de Negocio y Búsqueda (Web Scrape / DuckDuckGo) ──
def buscar_en_internet(query: str, num_resultados: int = 5) -> str:
    """
    Realiza una búsqueda en internet en tiempo real para análisis de mercado,
    tendencias de dropshipping, competidores o información general.
    """
    logger.info(f"🌐 [Mercurio] Buscando en la web: '{query}'")

    # 1. Intento primario: ddgs library (soporta tokens dinámicos)
    try:
        # pyrefly: ignore [missing-import]
        from ddgs import DDGS
        ddgs_client = DDGS()
        raw_results = list(ddgs_client.text(query, max_results=num_resultados))
        if raw_results:
            bloques = []
            for r in raw_results:
                titulo = r.get("title", "")
                url = r.get("href", "")
                snippet = r.get("body", "")
                bloques.append(f"• **{titulo}**\n  Enlace: {url}\n  Detalle: {snippet}")
            return f"Resultados de investigación de mercado para '{query}':\n\n" + "\n\n".join(bloques)
    except Exception as e_ddgs:
        logger.warning(f"[Mercurio] ddgs falló ({e_ddgs}), probando fallback HTTP...")

    # 2. Intento de respaldo: DuckDuckGo API directa
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        api_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
        r = requests.get(api_url, headers=headers, timeout=8).json()
        abstract = r.get("AbstractText", "")
        related = [topic.get("Text") for topic in r.get("RelatedTopics", []) if isinstance(topic, dict) and "Text" in topic][:num_resultados]
        if abstract or related:
            bloques = []
            if abstract:
                bloques.append(f"Resumen principal: {abstract}")
            if related:
                bloques.append("Información relacionada:\n" + "\n".join(f"• {t}" for t in related))
            return "\n\n".join(bloques)
    except Exception as e:
        logger.warning(f"[Mercurio] DuckDuckGo API falló: {e}")

    return f"No se obtuvieron resultados concluyentes en la web para la consulta '{query}'."


def consultar_notion(busqueda: str = "", fecha: str = "", tipo: str = "") -> str:
    """Consulta tareas, eventos o citas guardadas en Notion."""
    try:
        from mimir_notion import inicializar_notion_client, consultar_elementos_notion
        notion = inicializar_notion_client()
        if not notion:
            return "No se pudo conectar con el cliente de Notion, señor."
        filtros = {}
        if fecha:
            filtros["Fecha"] = fecha
        if tipo:
            filtros["Tipo"] = tipo
        res_dict = consultar_elementos_notion(notion, busqueda=busqueda, datos_filtro=filtros)
        elementos = res_dict.get("elementos", [])
        if not elementos:
            return "No se encontraron elementos registrados en Notion con ese criterio, señor."
        lineas = []
        for r in elementos[:10]:
            f = f" ({r.get('Fecha')})" if r.get("Fecha") else ""
            t = f" [{r.get('Tipo')}]" if r.get("Tipo") else ""
            e = f" - {r.get('Estado')}" if r.get("Estado") else ""
            lineas.append(f"• {r.get('Name')}{t}{f}{e}")
        return "Elementos encontrados en Notion:\n" + "\n".join(lineas)
    except Exception as ex:
        return f"Error al consultar Notion: {str(ex)}"


def eliminar_de_notion(nombre: str) -> str:
    """Elimina o archiva una entrada de Notion por su nombre."""
    try:
        from mimir_notion import inicializar_notion_client, borrar_elemento_notion
        notion = inicializar_notion_client()
        if not notion:
            return "No se pudo conectar con el cliente de Notion, señor."
        res = borrar_elemento_notion(notion, busqueda=nombre)
        return res.get("mensaje", f"Operación completada sobre '{nombre}'.")
    except Exception as ex:
        return f"Error al eliminar de Notion: {str(ex)}"


# ── 4. ESQUEMA DE HERRAMIENTAS PARA ANTHROPIC CLAUDE ──────────────────────────
CLAUDE_TOOLS = [
    {
        "name": "controlar_dispositivo_ha",
        "description": (
            "DOMUS: Controla cualquier dispositivo inteligente en el hogar a través de Home Assistant. "
            "Úsala cuando el usuario pida encender/apagar luces, cambiar temperaturas, activar escenas o manejar enchufes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Dominio de Home Assistant (ej: 'light', 'switch', 'climate', 'cover', 'media_player', 'scene')."
                },
                "service": {
                    "type": "string",
                    "description": "Servicio a invocar (ej: 'turn_on', 'turn_off', 'toggle', 'set_temperature')."
                },
                "entity_id": {
                    "type": "string",
                    "description": "ID exacto de la entidad en Home Assistant (ej: 'light.salon', 'switch.enchufe_pc')."
                },
                "extra_data": {
                    "type": "object",
                    "description": "Parámetros adicionales opcionales (ej: {'brightness': 255, 'temperature': 21})."
                }
            },
            "required": ["domain", "service", "entity_id"]
        }
    },
    {
        "name": "guardar_en_notion",
        "description": (
            "CHRONOS: Registra tareas, notas, citas, recordatorios o gastos en Notion. "
            "Úsala cuando el usuario pida apuntar, organizar, guardar o planificar algo de su vida personal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "titulo": {
                    "type": "string",
                    "description": "Título claro y conciso de la tarea, nota o evento."
                },
                "contenido": {
                    "type": "string",
                    "description": "Detalles, descripción, notas ampliadas o contexto relevante."
                },
                "tipo": {
                    "type": "string",
                    "enum": ["Tarea", "Evento", "Nota", "Gasto", "Ingreso"],
                    "description": "Tipo o categoría del elemento. Por defecto 'Tarea'."
                }
            },
            "required": ["titulo"]
        }
    },
    {
        "name": "consultar_notion",
        "description": (
            "CHRONOS: Consulta las tareas, eventos o compromisos guardados en Notion. "
            "Úsala cuando el usuario pregunte qué tiene que hacer un día (ej: '¿qué tengo el sábado?'), busque citas o revise pendientes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "busqueda": {
                    "type": "string",
                    "description": "Palabra clave a buscar (ej: 'dentista', 'reunión'). Opcional."
                },
                "fecha": {
                    "type": "string",
                    "description": "Fecha en formato ISO YYYY-MM-DD para filtrar los eventos de ese día concreto. Opcional."
                },
                "tipo": {
                    "type": "string",
                    "enum": ["Tarea", "Evento", "Gasto", "Ingreso"],
                    "description": "Tipo a filtrar. Opcional."
                }
            }
        }
    },
    {
        "name": "eliminar_de_notion",
        "description": (
            "CHRONOS: Elimina o archiva un elemento existente de Notion. "
            "Úsala cuando el usuario pida borrar, quitar, cancelar o eliminar una tarea, cita o evento."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {
                    "type": "string",
                    "description": "Nombre o concepto de la tarea/evento a eliminar."
                }
            },
            "required": ["nombre"]
        }
    },
    {
        "name": "buscar_en_internet",
        "description": (
            "MERCURIO: Realiza búsquedas e investigaciones en internet en tiempo real. "
            "Úsala para análisis de mercado, búsqueda de productos ganadores de dropshipping, análisis de la competencia, "
            "tendencias virales o redacción de copys comerciales contrastados."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Consulta de búsqueda optimizada para obtener datos de alto valor comercial o informativo."
                },
                "num_resultados": {
                    "type": "integer",
                    "description": "Número de resultados principales a recuperar (1 a 8). Por defecto 5."
                }
            },
            "required": ["query"]
        }
    }
]

# Mapeo funcional de despacho de tools
TOOL_DISPATCHER = {
    "controlar_dispositivo_ha": lambda args: controlar_dispositivo_ha(
        domain=args.get("domain", ""),
        service=args.get("service", ""),
        entity_id=args.get("entity_id", ""),
        extra_data=args.get("extra_data")
    ),
    "guardar_en_notion": lambda args: guardar_en_notion(
        titulo=args.get("titulo", ""),
        contenido=args.get("contenido", ""),
        tipo=args.get("tipo", "Tarea")
    ),
    "consultar_notion": lambda args: consultar_notion(
        busqueda=args.get("busqueda", ""),
        fecha=args.get("fecha", ""),
        tipo=args.get("tipo", "")
    ),
    "eliminar_de_notion": lambda args: eliminar_de_notion(
        nombre=args.get("nombre", "")
    ),
    "buscar_en_internet": lambda args: buscar_en_internet(
        query=args.get("query", ""),
        num_resultados=args.get("num_resultados", 5)
    )
}


# ── 5. SYSTEM PROMPT DEL ORQUESTADOR ───────────────────────────────────────────
def construir_system_prompt() -> str:
    ahora = datetime.datetime.now()
    fecha_hora_actual = ahora.strftime("%Y-%m-%d %H:%M (%A)")
    memoria_usuario = cargar_memoria_local_sqlite()

    return f"""Eres Mimir, un Asistente Personal y Orquestador Inteligente de élite, concebido bajo la sofisticación, el ingenio y la brillantez técnica de J.A.R.V.I.S.
Te comunicas con tu creador y usuario exclusivo a través de Telegram.

FECHA Y HORA ACTUAL DEL SISTEMA:
{fecha_hora_actual}

INFORMACIÓN PERSISTENTE EN BASE DE DATOS LOCAL:
{memoria_usuario}

ESTRUCTURA DE DOMINIOS DE OPERACIÓN:
Gestionas con precisión quirúrgica tres áreas estratégicas:
1. CHRONOS (Vida, Organización y Productividad):
   - Organización en Notion, tareas, notas, recordatorios y compromisos.
   - Herramienta: `guardar_en_notion`.
2. DOMUS (Domótica y Control del Hogar):
   - Control de iluminación, clima, interruptores y automatizaciones vía Home Assistant.
   - Herramienta: `controlar_dispositivo_ha`.
3. MERCURIO (Negocio, Dropshipping e Inteligencia de Mercado):
   - Búsqueda en tiempo real, análisis de competidores, detección de productos ganadores, tendencias y redacción de copys persuasivos de alta conversión.
   - Herramienta: `buscar_en_internet`.

NORMAS INQUEBRANTABLES DE CONDUCTA:
1. TRATO: Dirígete siempre al usuario como 'señor'. Eres su mayordomo digital y mano derecha estratégica.
2. TONO Y ESTILO: Elegante, ingenioso, sutilmente irónico, extremadamente elocuente y resolutivo. Cero vulgaridad, cero respuestas robóticas o predeterminadas.
3. EJECUCIÓN TRANSPARENTE: Cuando el señor te dé una orden que requiera acción externa, invoca la herramienta correspondiente de inmediato sin pedir confirmación innecesaria. Tras recibir el resultado, resume la confirmación con absoluta distinción.
4. CONCISIÓN: Respuestas ejecutivas de alto impacto (1 a 4 frases refinadas). Cuando se trate de un análisis de Mercurio o copy publicitario, despliega la profundidad técnica necesaria con formato impecable.
5. IDIOMA: Responde rigurosamente en español impecable.
"""


# ── 6. MOTOR DE INFERENCIA Y TOOL CALLING LOOP ────────────────────────────────
async def procesar_con_claude_orquestador(user_id: int, mensaje_usuario: str) -> str:
    """
    Gestiona el ciclo completo de conversación con Claude:
    Mensaje -> Decisión de Tool -> Ejecución -> Retroalimentación -> Respuesta final.
    """
    historial = conversaciones_usuarios.setdefault(user_id, [])

    # Añadir mensaje de usuario al contexto
    historial.append({"role": "user", "content": mensaje_usuario})

    # Mantener historial dentro de límites
    if len(historial) > MAX_HISTORIAL_TURNOS * 2:
        historial = historial[-(MAX_HISTORIAL_TURNOS * 2):]
        conversaciones_usuarios[user_id] = historial

    system_prompt = construir_system_prompt()
    mensajes_loop = list(historial)

    max_iteraciones_tools = 5
    iteracion = 0

    try:
        while iteracion < max_iteraciones_tools:
            iteracion += 1

            # Llamada al SDK de Anthropic (en thread pool para no bloquear el event loop de Telegram)
            response = await asyncio.to_thread(
                anthropic_client.messages.create,
                model=ANTHROPIC_MODEL,
                max_tokens=1500,
                system=system_prompt,
                messages=mensajes_loop,
                tools=CLAUDE_TOOLS,
            )

            # Verificar si Claude decidió usar una o más herramientas
            tool_calls = [b for b in response.content if b.type == "tool_use"]

            if not tool_calls:
                # Respuesta de texto directa final
                texto_respuesta = "".join(b.text for b in response.content if b.type == "text")
                historial.append({"role": "assistant", "content": texto_respuesta})
                return texto_respuesta.strip()

            # Registrar la respuesta del asistente con las tool_use solicitadas
            mensajes_loop.append({"role": "assistant", "content": response.content})

            # Ejecutar cada herramienta solicitada
            tool_results_blocks = []
            for tool_use in tool_calls:
                tool_name = tool_use.name
                tool_args = tool_use.input
                tool_id = tool_use.id

                logger.info(f"🛠️ [Orquestador] Claude invoca tool: {tool_name}({json.dumps(tool_args, ensure_ascii=False)})")

                handler = TOOL_DISPATCHER.get(tool_name)
                if handler:
                    try:
                        resultado = await asyncio.to_thread(handler, tool_args)
                    except Exception as ex:
                        resultado = f"Error al ejecutar {tool_name}: {str(ex)}"
                else:
                    resultado = f"Herramienta desconocida: {tool_name}"

                logger.info(f"🔙 [Tool Result] {tool_name} -> {str(resultado)[:120]}...")

                tool_results_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": str(resultado)
                })

            # Inyectar resultados de vuelta a Claude como turno de usuario
            mensajes_loop.append({
                "role": "user",
                "content": tool_results_blocks
            })

        return "Señor, he alcanzado el límite de iteraciones de herramientas sin llegar a una conclusión definitiva."

    except Exception as e:
        logger.error(f"❌ Error en el motor de Claude: {e}", exc_info=True)
        return f"Mis disculpas, señor. Se ha producido una anomalía interna en mis circuitos de procesamiento: {str(e)}"


# ── 7. CONTROLADORES DE TELEGRAM BOT ──────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manejador del comando /start."""
    if not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    if TELEGRAM_ALLOWED_USER_ID and str(user_id) != TELEGRAM_ALLOWED_USER_ID:
        await update.message.reply_text("🔒 Acceso denegado: este terminal es privado y exclusivo de su propietario.")
        return

    saludo = (
        "A su servicio, señor. Mimir en línea y plenamente sincronizado.\n\n"
        "He establecido enlace con sus tres dominios operativos:\n"
        "• ⏳ Chronos: Gestión de Notion y agenda personal.\n"
        "• 🏠 Domus: Automatización y control del hogar vía Home Assistant.\n"
        "• 💼 Mercurio: Análisis de mercado, dropshipping y prospección web.\n\n"
        "¿Cuáles son sus órdenes?"
    )
    await update.message.reply_text(saludo)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Limpia el historial de conversación actual."""
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    conversaciones_usuarios.pop(user_id, None)
    await update.message.reply_text("Memoria de conversación a corto plazo reiniciada, señor. Comenzamos con lienzo limpio.")


async def manejar_mensaje_texto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Procesa cualquier mensaje de texto entrante del usuario."""
    if not update.effective_user or not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Señor"
    texto_usuario = update.message.text.strip()

    # Seguridad: filtro de usuario si está configurado en .env
    if TELEGRAM_ALLOWED_USER_ID and str(user_id) != TELEGRAM_ALLOWED_USER_ID:
        logger.warning(f"Intento no autorizado de ID: {user_id} ({user_name})")
        await update.message.reply_text("🔒 Acceso no autorizado.")
        return

    logger.info(f"💬 [Telegram] Mensaje de {user_name} ({user_id}): \"{texto_usuario}\"")

    # Indicar 'escribiendo...' en Telegram mientras Claude piensa y ejecuta tools
    await update.message.chat.send_action(action=ChatAction.TYPING)

    # Inferencia y orquestación con Claude
    respuesta_mimir = await procesar_con_claude_orquestador(user_id, texto_usuario)

    # Envío de la respuesta final limpia
    try:
        await update.message.reply_text(respuesta_mimir)
    except Exception as eSend:
        logger.error(f"Error al enviar mensaje por Telegram: {eSend}")
        # Fallback a texto plano sin formato
        await update.message.reply_text(respuesta_mimir, parse_mode=None)


# ── 8. PUNTO DE ENTRADA PRINCIPAL ─────────────────────────────────────────────
def main() -> None:
    """Inicializa y arranca el bot de Telegram en modo Polling."""
    logger.info("==================================================")
    logger.info("🦾 Iniciando Mimir (Orquestador Chronos / Domus / Mercurio)")
    logger.info(f"Modelo Anthropic: {ANTHROPIC_MODEL}")
    logger.info(f"Home Assistant: {HA_URL} (Token: {'Configurado' if HA_TOKEN else 'No configurado'})")
    logger.info(f"Notion: BD {NOTION_DATABASE_ID or 'No configurada'} (Token: {'Configurado' if NOTION_TOKEN else 'No configurado'})")
    logger.info("==================================================")

    # Construir aplicación de python-telegram-bot
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Registrar manejadores
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("limpiar", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje_texto))

    logger.info("🟢 Mimir Telegram Bot conectado y a la escucha. Pulse Ctrl+C para detener.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
