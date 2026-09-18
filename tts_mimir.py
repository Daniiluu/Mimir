#!/usr/bin/env python3
"""
tts_mimir.py - Servidor TTS Local Persistente para Mimir (XTTS-v2)
==================================================================
Protocolo stdin/stdout JSON (una linea por mensaje):
  Entrada:  {"text": "Texto a sintetizar", "language": "es"}
  Salida:   {"status": "done"} | {"status": "error", "msg": "..."}
  Control:  {"text": "__PING__"} -> {"status": "pong"}
            {"text": "__QUIT__"} -> cierra el proceso

El modelo XTTS-v2 (~1.8 GB) se descarga automaticamente en la primera
ejecucion en: ~/.local/share/tts/
Se carga UNA sola vez al arrancar y se reutiliza para todas las peticiones.

Voz:
  - Si existe 'speaker_reference.wav' en el directorio del script,
    se usa para clonar la voz (voice cloning).
  - Si no existe, se usa el speaker por defecto en espanol del modelo.
"""

import sys
import os
import io
import json
import re
import numpy as np

# Aceptar automaticamente la licencia de Coqui XTTS (evita prompt interactivo en stdin)
os.environ["COQUI_TOS_AGREED"] = "1"

# -- Forzar codificacion UTF-8 en Windows
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

def log(msg):
    """Log al stderr (visible en la consola de Node.js)."""
    print(f"[Mimir-TTS] {msg}", file=sys.stderr, flush=True)

def emit(obj):
    """Emitir un objeto JSON por stdout a Node.js."""
    print(json.dumps(obj, ensure_ascii=False), flush=True)


# -- Carga de dependencias
try:
    import torch
except ImportError:
    emit({"status": "error", "msg": "torch no instalado. Ejecuta: pip install torch"})
    sys.exit(1)

try:
    import sounddevice as sd
except ImportError:
    emit({"status": "error", "msg": "sounddevice no instalado. Ejecuta: pip install sounddevice"})
    sys.exit(1)

# Compatibilidad con versiones recientes de transformers
try:
    import transformers.pytorch_utils
    if not hasattr(transformers.pytorch_utils, 'isin_mps_friendly'):
        transformers.pytorch_utils.isin_mps_friendly = torch.isin
except Exception:
    pass

try:
    from TTS.api import TTS
except ImportError:
    emit({"status": "error", "msg": "TTS no instalado. Ejecuta: pip install TTS"})
    sys.exit(1)


# -- Configuracion
MODEL_NAME      = "tts_models/multilingual/multi-dataset/xtts_v2"
LANGUAGE        = "es"
SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
SPEAKER_WAV     = os.path.join(SCRIPT_DIR, "speaker_reference.wav")
DEFAULT_SPEAKER = "Claribel Dervla"   # Speaker integrado del modelo si no hay referencia WAV


def limpiar_texto_para_tts(texto):
    """Elimina markdown y caracteres problematicos para el TTS."""
    texto = re.sub(r'\*{1,3}', '', texto)
    texto = re.sub(r'`{1,3}[^`]*`{1,3}', '', texto)
    texto = re.sub(r'#+\s?', '', texto)
    texto = re.sub(r'[-*+]\s+', '', texto)
    texto = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', texto)
    texto = re.sub(r'["`]', '', texto)
    texto = re.sub(r'[\r\n]+', ' ', texto)
    return texto.strip()


def cargar_modelo():
    """Carga el modelo XTTS-v2. Descarga automatica en primera ejecucion."""
    log("Cargando modelo XTTS-v2... (puede tardar 10-30 segundos la primera vez)")

    use_gpu = torch.cuda.is_available()
    device  = "cuda" if use_gpu else "cpu"
    if use_gpu:
        log(f"Dispositivo: GPU ({torch.cuda.get_device_name(0)})")
    else:
        log("Dispositivo: CPU (considera instalar torch con CUDA para mayor velocidad)")

    tts = TTS(model_name=MODEL_NAME).to(device)

    if os.path.exists(SPEAKER_WAV):
        log(f"Referencia de voz encontrada: {SPEAKER_WAV} (voice cloning activo)")
        speaker_wav  = SPEAKER_WAV
        speaker_name = None
    else:
        log(f"Sin referencia de voz - usando speaker: '{DEFAULT_SPEAKER}'")
        speaker_wav  = None
        speaker_name = DEFAULT_SPEAKER

    log("Modelo XTTS-v2 listo.")
    return tts, speaker_wav, speaker_name


def obtener_sample_rate(tts):
    """Obtiene la frecuencia de muestreo del sintetizador."""
    try:
        return tts.synthesizer.output_sample_rate
    except Exception:
        return 24000


def sintetizar_y_reproducir(tts, texto, speaker_wav, speaker_name, sample_rate):
    """Sintetiza el texto y reproduce el audio directamente por sounddevice."""
    texto_limpio = limpiar_texto_para_tts(texto)
    if not texto_limpio:
        return

    if speaker_wav:
        wav = tts.tts(
            text=texto_limpio,
            language=LANGUAGE,
            speaker_wav=speaker_wav
        )
    else:
        wav = tts.tts(
            text=texto_limpio,
            language=LANGUAGE,
            speaker=speaker_name
        )

    # Convertir a float32 normalizado
    audio = np.array(wav, dtype=np.float32)
    peak  = max(abs(audio.max()), abs(audio.min()), 1e-6)
    if peak > 1.0:
        audio = audio / peak

    # Reproducir y esperar a que termine
    sd.play(audio, samplerate=sample_rate)
    sd.wait()


def main():
    log("Iniciando servidor TTS Mimir (XTTS-v2)...")

    try:
        tts, speaker_wav, speaker_name = cargar_modelo()
        sample_rate = obtener_sample_rate(tts)
    except Exception as e:
        emit({"status": "error", "msg": f"Error al cargar XTTS-v2: {e}"})
        log(f"Error fatal al cargar el modelo: {e}")
        sys.exit(1)

    # Senial de listo para Node.js
    emit({"status": "ready"})

    # Bucle principal: leer comandos JSON de stdin
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            emit({"status": "error", "msg": f"JSON invalido recibido"})
            continue

        texto = cmd.get("text", "")

        if texto == "__PING__":
            emit({"status": "pong"})
            continue
        if texto == "__QUIT__":
            log("Cerrando servidor TTS por peticion de Node.js.")
            break

        try:
            log(f"Sintetizando: {texto[:60]}{'...' if len(texto) > 60 else ''}")
            sintetizar_y_reproducir(tts, texto, speaker_wav, speaker_name, sample_rate)
            emit({"status": "done"})
        except Exception as e:
            log(f"Error en sintesis: {e}")
            emit({"status": "error", "msg": str(e)})


if __name__ == "__main__":
    main()
