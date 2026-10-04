#!/usr/bin/env python3
"""
mimir_informe.py — Módulo de Generación e Informe Matutino en Audio para Mimir
========================================================================
Características:
  1. Clima en tiempo real (Open-Meteo REST API) para hoy y mañana.
  2. Tareas y eventos programados en Notion para hoy y mañana.
  3. Generación de guión conversacional en voz de J.A.R.V.I.S (Claude Haiku).
  4. Síntesis de voz clonada usando XTTS-v2 (`speaker_reference.wav`).
  5. Envío programado cada mañana a las 5:40 AM y bajo demanda (/informe) a Telegram.
"""

import os
import sys
import json
import logging
import asyncio
import datetime
import requests
from typing import Tuple, Dict, Any, Optional

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

logger = logging.getLogger("MimirInformeMatutino")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip()
TELEGRAM_ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()

# Coordenadas por defecto (Madrid, España) — configurables en .env
WEATHER_LAT = float(os.getenv("WEATHER_LAT", "40.4168"))
WEATHER_LON = float(os.getenv("WEATHER_LON", "-3.7038"))
WEATHER_CITY = os.getenv("WEATHER_CITY", "Madrid")


def obtener_tiempo_forecast(lat: float = WEATHER_LAT, lon: float = WEATHER_LON, ciudad: str = WEATHER_CITY) -> str:
    """Obtiene el pronóstico del tiempo para hoy y mañana mediante la API gratuita de Open-Meteo."""
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&current=temperature_2m,weather_code&timezone=auto"
        )
        resp = requests.get(url, timeout=8).json()
        daily = resp.get("daily", {})
        w_codes = daily.get("weather_code", [])
        t_max = daily.get("temperature_2m_max", [])
        t_min = daily.get("temperature_2m_min", [])
        p_prob = daily.get("precipitation_probability_max", [])

        def desc_clima(code: int) -> str:
            if code == 0:
                return "cielos completamente despejados"
            elif code in (1, 2, 3):
                return "cielos parcialmente nublados"
            elif code in (45, 48):
                return "bancos de niebla matutina"
            elif code in (51, 53, 55, 56, 57):
                return "lloviznas ligeras"
            elif code in (61, 63, 65, 66, 67):
                return "lluvia"
            elif code in (71, 73, 75, 77):
                return "nieve"
            elif code in (80, 81, 82):
                return "chubascos dispersos"
            elif code in (95, 96, 99):
                return "tormentas eléctricas"
            return "tiempo variable"

        desc_hoy = desc_clima(w_codes[0]) if w_codes else "despejado"
        t_max_hoy = t_max[0] if t_max else 20
        t_min_hoy = t_min[0] if t_min else 10
        prob_hoy = p_prob[0] if p_prob else 0

        desc_man = desc_clima(w_codes[1]) if len(w_codes) > 1 else "despejado"
        t_max_man = t_max[1] if len(t_max) > 1 else 20
        t_min_man = t_min[1] if len(t_min) > 1 else 10
        prob_man = p_prob[1] if len(p_prob) > 1 else 0

        return (
            f"Pronóstico para {ciudad}:\n"
            f"- Hoy: {desc_hoy}, máxima de {t_max_hoy}°C, mínima de {t_min_hoy}°C, probabilidad de lluvia del {prob_hoy}%.\n"
            f"- Mañana: {desc_man}, máxima de {t_max_man}°C, mínima de {t_min_man}°C, probabilidad de lluvia del {prob_man}%."
        )
    except Exception as e:
        logger.warning(f"Error consultando Open-Meteo: {e}")
        return f"No se pudo consultar el pronóstico meteorológico detallado para {ciudad}."


def obtener_agenda_hoy_y_manana() -> Tuple[str, str]:
    """Consulta Notion para obtener las tareas/eventos programados para hoy y mañana."""
    hoy_iso = datetime.date.today().isoformat()
    manana_iso = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

    texto_hoy = "Sin tareas o planes registrados en Notion para hoy."
    texto_manana = "Sin tareas o planes registrados en Notion para mañana."

    try:
        from mimir_bot import consultar_notion
        res_hoy = consultar_notion(fecha=hoy_iso)
        if "No se encontraron" not in res_hoy:
            texto_hoy = res_hoy

        res_manana = consultar_notion(fecha=manana_iso)
        if "No se encontraron" not in res_manana:
            texto_manana = res_manana
    except Exception as e:
        logger.warning(f"Error consultando Notion para el informe: {e}")

    return texto_hoy, texto_manana


def construir_guion_hablado_claude(clima_info: str, tareas_hoy: str, tareas_manana: str) -> str:
    """Utiliza Claude para redactar un guion impecable en voz de J.A.R.V.I.S destinado a ser sintetizado por voz."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    ahora = datetime.datetime.now()
    fecha_str = ahora.strftime("%A %d de %B de %Y")

    prompt_sistema = """Eres Mimir, el asistente personal de élite de su creador, inspirado en la elegancia y distinción de J.A.R.V.I.S.
Tu tarea es redactar el GUION HABLADO para su informe matutino en audio.

REGLAS ABSOLUTAS PARA EL GUION HABLADO:
1. Dirígete siempre al usuario como 'señor'.
2. El tono debe ser formal, distinguido, refinado y resolutivo. Menciónale que son aproximadamente las 5:40 de la mañana (o el comienzo de la jornada).
3. ESTRUCTURA DEL GUION:
   - Saludo matutino distinguido.
   - Pronóstico meteorológico para el día de hoy.
   - Resumen de tareas y compromisos agendados para HOY.
   - Resumen de planes y compromisos agendados para MAÑANA.
   - Cierre caballeroso y deseos de un día productivo.
4. IMPORTANTE PARA SÍNTESIS DE VOZ (TTS):
   - NO utilices símbolos de markdown (ni asteriscos *, ni almohadillas #, ni guiones de lista -, ni corchetes).
   - Escribe en texto continuo estructurado en párrafos fluidos y naturales para ser leídos en voz alta.
   - Escribe los números y grados de forma clara (ej: "veinte grados centígrados").
   - Mantén una extensión de entre 120 y 250 palabras para un audio de aproximadamente 1 minuto.
"""

    user_prompt = f"""Datos reales para el informe matutino del señor:
- Fecha de hoy: {fecha_str}
- Información del tiempo:
{clima_info}

- Tareas y compromisos para HOY:
{tareas_hoy}

- Tareas y compromisos para MAÑANA:
{tareas_manana}

Por favor, redacta el guion hablado definitivo sin formato markdown para lectura en voz alta."""

    res = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1000,
        system=prompt_sistema,
        messages=[{"role": "user", "content": user_prompt}]
    )
    guion = "".join(b.text for b in res.content if b.type == "text").strip()
    return guion


def generar_informe_matutino_audio() -> Tuple[str, str]:
    """
    Genera el guion y produce el archivo de audio (.wav) usando la voz clonada (`speaker_reference.wav`).
    Retorna (guion_texto, ruta_archivo_wav).
    """
    clima = obtener_tiempo_forecast()
    t_hoy, t_manana = obtener_agenda_hoy_y_manana()
    guion = construir_guion_hablado_claude(clima, t_hoy, t_manana)

    # Crear directorio temporal si no existe
    tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tts_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = os.path.join(tmp_dir, f"informe_matutino_{timestamp}.wav")

    from tts_mimir import generar_audio_clonado
    generar_audio_clonado(guion, out_file)

    return guion, out_file


async def enviar_informe_matutino_telegram(bot, chat_id: int) -> bool:
    """
    Genera el informe matutino y lo envía al usuario por Telegram como mensaje de voz/audio y texto transcripto.
    """
    logger.info(f"🎙️ Generando informe matutino en audio para chat_id {chat_id}...")
    try:
        guion, wav_path = await asyncio.to_thread(generar_informe_matutino_audio)

        # Intentar enviar como nota de voz / audio
        with open(wav_path, "rb") as audio_fp:
            try:
                await bot.send_voice(
                    chat_id=chat_id,
                    voice=audio_fp,
                    caption="🌅 *Informe Matutino de Mimir* (Voz Clonada)",
                    parse_mode="Markdown"
                )
            except Exception as e_voice:
                logger.warning(f"No se pudo enviar como voice, reintentando como send_audio: {e_voice}")
                audio_fp.seek(0)
                await bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_fp,
                    title="Informe Matutino Mimir",
                    performer="Mimir",
                    caption="🌅 *Informe Matutino de Mimir* (Voz Clonada)",
                    parse_mode="Markdown"
                )

        # Enviar transcripción en texto
        await bot.send_message(
            chat_id=chat_id,
            text=f"📜 *Transcripción del Informe Matutino:*\n\n{guion}",
            parse_mode="Markdown"
        )

        # Limpiar archivo temporal
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception:
            pass

        return True
    except Exception as e:
        logger.error(f"❌ Error enviando informe matutino por Telegram: {e}")
        await bot.send_message(
            chat_id=chat_id,
            text=f"Mis disculpas, señor. Ha ocurrido una anomalía al generar su informe matutino en audio: {str(e)}"
        )
        return False


async def bucle_programador_540am(app) -> None:
    """
    Bucle asíncrono que calcula el tiempo hasta las 5:40 AM de cada día y dispara el informe matutino.
    """
    chat_id_str = TELEGRAM_ALLOWED_USER_ID
    if not chat_id_str:
        logger.warning("⚠️ No se ha definido TELEGRAM_ALLOWED_USER_ID en .env. El programador de las 5:40 AM estará inactivo.")
        return

    try:
        target_chat_id = int(chat_id_str)
    except ValueError:
        logger.error(f"TELEGRAM_ALLOWED_USER_ID no es un entero válido: {chat_id_str}")
        return

    logger.info("⏰ Programador matutino de Mimir (05:40 AM) iniciado correctamente.")

    while True:
        ahora = datetime.datetime.now()
        # Calcular próximo objetivo: 5:40 AM de hoy o de mañana
        objetivo = ahora.replace(hour=5, minute=40, second=0, microsecond=0)
        if ahora >= objetivo:
            objetivo += datetime.timedelta(days=1)

        segundos_espera = (objetivo - ahora).total_seconds()
        horas = segundos_espera / 3600
        logger.info(f"⏰ Próximo informe matutino agendado para: {objetivo.strftime('%Y-%m-%d %H:%M:%S')} (en {horas:.2f} horas)")

        await asyncio.sleep(segundos_espera)

        logger.info("⏰ Alcanzada la hora programada (05:40 AM). Disparando informe matutino...")
        await enviar_informe_matutino_telegram(app.bot, target_chat_id)

        # Esperar 60s extra para evitar re-disparos inmediatos en la misma ventana de segundo
        await asyncio.sleep(60)
