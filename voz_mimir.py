#!/usr/bin/env python3
"""
voz_mimir.py — Pipeline de Escucha Local Robusto para Mimir
==========================================================
- Configuración explícita de codificación UTF-8 para solucionar problemas de tildes y caracteres (ñ, á, é, í, ó, ú).
- Inyección de initial_prompt ("Mimir. Hey Mimir. Oye Mimir.") en Whisper para guiar el reconocimiento hacia Mimir.
- Detección de voz activa con Silero VAD y transcripción precisa con Whisper base en español.
"""

import sys
import os
import io
import re
import json
import queue
import numpy as np
import sounddevice as sd
import torch

# ── Forzar codificación UTF-8 en Windows ──────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

try:
    from faster_whisper import WhisperModel
except ImportError:
    print(json.dumps({"type": "error", "message": "faster-whisper no instalado. Ejecuta: python -m pip install faster-whisper"}), flush=True)
    sys.exit(1)

# ── Configuración ─────────────────────────────────────────────────────────────
SR                   = 16000      # 16 kHz para Whisper y Silero VAD
BLOCK                = 512        # ~32ms por bloque
MODELS_DIR           = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".whisper_models")

VAD_THRESHOLD        = 0.58       # Umbral VAD más estricto para ignorar carraspeos, respiración y ruidos lejanos
WAKE_MIN_BLOCKS      = 12         # Mínimo ~384ms de voz (descarta toses breves, chasquidos e impulsos de <300ms)
WAKE_MAX_BLOCKS      = 75         # Máximo ~2.4s acumulados para la palabra clave
WAKE_SILENCE_BLOCKS  = 10         # ~320ms de silencio para dar por finalizada la frase de activación sin cortar entre palabras

CMD_INITIAL_WAIT_SEC = 4.0        # Tiempo máximo para empezar a hablar tras la activación
CMD_SILENCE_SEC      = 1.4        # Silencio para dar por concluida la orden
CMD_MAX_SEC          = 12.0       # Duración máxima de la orden

# Expresión regular estricta: debe comenzar explícitamente con Mimir o su saludo
# Eliminado 'mimi' y variantes que causan falsos positivos con palabras comunes
WAKE_WORD_RE = re.compile(
    r'^(hey\s+|ey\s+|eh\s+|oye\s+|hola\s+|ok\s+)?m[ií]mir\b',
    re.IGNORECASE
)

def log(msg):
    print(f"[Mimir-Voz] {msg}", file=sys.stderr, flush=True)


def cargar_modelos():
    try:
        dev = sd.query_devices(sd.default.device[0], 'input')
        log(f"🎙️ Micrófono en uso: {dev.get('name', 'Predeterminado')}")
    except Exception:
        log("🎙️ Micrófono predeterminado activo.")

    log("Cargando Silero VAD...")
    try:
        vad_model, _ = torch.hub.load(
            repo_or_dir='snakers4/silero-vad',
            model='silero_vad',
            source='local',
            force_reload=False,
            trust_repo=True
        )
        vad_model.eval()
        log("✅ Silero VAD listo.")
    except Exception:
        try:
            vad_model, _ = torch.hub.load(
                repo_or_dir='snakers4/silero-vad',
                model='silero_vad',
                force_reload=False,
                trust_repo=True
            )
            vad_model.eval()
            log("✅ Silero VAD listo.")
        except Exception as e:
            log(f"❌ Error cargando Silero VAD: {e}")
            sys.exit(1)

    use_cuda = torch.cuda.is_available()
    device = "cuda" if use_cuda else "cpu"
    compute_type = "float16" if use_cuda else "int8"
    log(f"Dispositivo para Whisper: {device.upper()} ({compute_type})")

    log("Cargando Whisper base (detector y transcriptor unificado)...")
    try:
        # Usamos Whisper base para ambos: elimina las alucinaciones de 'tiny' en ruidos y toses
        whisper_model = WhisperModel("base", device=device, compute_type=compute_type, download_root=MODELS_DIR)
        log("✅ Whisper base listo.")
        log("🟢 Sistema listo. Escuchando 'Mimir' / 'Hey Mimir'...")
    except Exception as e:
        log(f"❌ Error cargando Whisper base en {device}, reintentando en CPU int8: {e}")
        try:
            whisper_model = WhisperModel("base", device="cpu", compute_type="int8", download_root=MODELS_DIR)
            log("✅ Whisper base listo (modo CPU fallback).")
            log("🟢 Sistema listo. Escuchando 'Mimir' / 'Hey Mimir'...")
        except Exception as e_cpu:
            log(f"❌ Error fatal cargando Whisper base: {e_cpu}")
            sys.exit(1)

    return vad_model, whisper_model


def vad_score(model, audio_block: np.ndarray) -> float:
    try:
        t = torch.from_numpy(audio_block.astype(np.float32))
        with torch.no_grad():
            return model(t, SR).item()
    except Exception:
        return 0.0


def audio_rms(audio: np.ndarray) -> float:
    """Calcula la energía media cuadrática (RMS) del audio para filtrar ruidos débiles."""
    if len(audio) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio.astype(np.float32)))))


def normalizar_audio(audio: np.ndarray, target=0.90) -> np.ndarray:
    peak = np.max(np.abs(audio))
    if peak > 0.02:
        return (audio / peak) * target
    return audio


audio_q = queue.Queue()

def audio_callback(indata, frames, time_info, status):
    audio_q.put(indata.copy().flatten())


def vaciar_cola():
    while not audio_q.empty():
        try:
            audio_q.get_nowait()
        except queue.Empty:
            break


def evaluar_activacion(segmentos_lista, texto_raw, audio_wake):
    """
    Evalúa estrictamente si el audio corresponde a la invocación intencional de 'Mimir'.
    Filtra toses, ruidos ambientales, murmullos y conversaciones ajenas en la habitación.
    """
    if not texto_raw:
        return False, "Texto vacío"

    # 1. Filtro de energía acústica: descartar murmullos lejanos o ruidos de fondo
    rms = audio_rms(audio_wake)
    if rms < 0.005:
        return False, f"Energía demasiado baja (RMS: {rms:.4f})"

    # 2. Limpiar texto conservando letras y espacios
    texto_limpio = re.sub(r'[^\w\s]', '', texto_raw).lower().strip()
    if not texto_limpio:
        return False, "Texto sin caracteres alfanuméricos"

    palabras = texto_limpio.split()

    # 3. Comprobar que comience estrictamente por la palabra clave
    coincide = WAKE_WORD_RE.match(texto_limpio)
    if not coincide:
        return False, f"No inicia con invocación a Mimir (Oído: '{texto_limpio}')"

    # 4. Si es una conversación larga (> 4 palabras), verificar que las primeras sean la llamada
    if len(palabras) > 4:
        inicio = " ".join(palabras[:2])
        if not ("mimir" in inicio or "mímir" in inicio):
            return False, f"Frase conversacional sin llamada directa ('{texto_limpio}')"

    # 5. Comprobar confianza y métricas de Whisper
    for s in segmentos_lista:
        no_speech = getattr(s, 'no_speech_prob', 0.0)
        avg_logprob = getattr(s, 'avg_logprob', 0.0)

        # Si Whisper detecta alta probabilidad de no-habla (típico en toses, golpes, estornudos)
        if no_speech > 0.60:
            return False, f"Probable sonido no verbal (no_speech_prob: {no_speech:.2f})"

        # Si el logprob medio es muy bajo, Whisper está alucinando con ruido
        if avg_logprob < -1.40:
            return False, f"Baja confianza fonética (avg_logprob: {avg_logprob:.2f})"

    return True, f"Invocación confirmada ('{coincide.group(0)}')"


# ── Bucle Principal ───────────────────────────────────────────────────────────
def main():
    vad_model, whisper_model = cargar_modelos()
    vad_model.reset_states()

    with sd.InputStream(samplerate=SR, blocksize=BLOCK, channels=1, dtype='float32', callback=audio_callback):

        while True:
            voice_blocks = []
            preroll = []
            silence_blocks = 0

            while True:
                try:
                    block = audio_q.get(timeout=0.5)
                except queue.Empty:
                    continue

                preroll.append(block)
                if len(preroll) > 6:
                    preroll.pop(0)

                score = vad_score(vad_model, block)

                if score >= VAD_THRESHOLD:
                    if not voice_blocks and len(preroll) > 1:
                        voice_blocks.extend(preroll[:-1])
                    voice_blocks.append(block)
                    silence_blocks = 0
                elif voice_blocks:
                    # Permitir breve pausa entre palabras (ej: "Hey" ... "Mimir")
                    voice_blocks.append(block)
                    silence_blocks += 1
                    if silence_blocks >= WAKE_SILENCE_BLOCKS:
                        # Si tras el silencio acumulado tenemos suficiente voz neta
                        if (len(voice_blocks) - silence_blocks) >= WAKE_MIN_BLOCKS:
                            break
                        else:
                            voice_blocks = []
                            silence_blocks = 0

                if len(voice_blocks) >= WAKE_MAX_BLOCKS:
                    break

            if not voice_blocks:
                continue

            audio_wake = normalizar_audio(np.concatenate(voice_blocks))

            try:
                # Transcribir con Whisper base guiando el reconocimiento hacia Mimir
                segs, _ = whisper_model.transcribe(
                    audio_wake,
                    language="es",
                    initial_prompt="Mimir. Hey Mimir. Oye Mimir.",
                    beam_size=2,
                    temperature=0.0,
                    vad_filter=False,
                    without_timestamps=True,
                )
                segmentos_lista = list(segs)
                texto_raw = " ".join(s.text for s in segmentos_lista).strip()
            except Exception as e:
                log(f"Error procesando audio de activación: {e}")
                continue

            es_valido, motivo = evaluar_activacion(segmentos_lista, texto_raw, audio_wake)
            log(f"Oído: \"{texto_raw}\" ➔ {motivo}")

            if not es_valido:
                continue

            # ⚡ WAKE WORD CONFIRMADO
            print(json.dumps({"type": "wakeword"}, ensure_ascii=False), flush=True)
            log(f"⚡ [ACTIVADO] {motivo}. Escuchando orden...")

            vaciar_cola()
            vad_model.reset_states()

            # ═══════════════════════════════════════════════════════════════
            # FASE 2 — CAPTURA Y GRABACIÓN DE LA ORDEN
            # ═══════════════════════════════════════════════════════════════
            cmd_blocks = []
            silence_blocks = 0
            max_silence_blocks = int(CMD_SILENCE_SEC * SR / BLOCK)
            max_initial_wait_blocks = int(CMD_INITIAL_WAIT_SEC * SR / BLOCK)
            max_total_blocks = int(CMD_MAX_SEC * SR / BLOCK)

            speech_started = False
            wait_count = 0

            for _ in range(max_total_blocks):
                try:
                    block = audio_q.get(timeout=0.5)
                except queue.Empty:
                    break

                score = vad_score(vad_model, block)
                cmd_blocks.append(block)

                if score >= VAD_THRESHOLD:
                    speech_started = True
                    silence_blocks = 0
                elif speech_started:
                    silence_blocks += 1
                    if silence_blocks >= max_silence_blocks:
                        break
                else:
                    wait_count += 1
                    if wait_count >= max_initial_wait_blocks:
                        log("Tiempo de espera agotado sin detectar orden.")
                        break

            if not speech_started or len(cmd_blocks) < 8:
                log("Comando no detectado. Volviendo a espera pasiva.")
                vad_model.reset_states()
                vaciar_cola()
                continue

            audio_cmd = normalizar_audio(np.concatenate(cmd_blocks))
            dur = len(audio_cmd) / SR
            log(f"Transcribiendo orden ({dur:.1f}s) con Whisper base...")

            try:
                # Prompt inicial para Whisper base garantizando tildes y ortografía impecable en español
                segs_cmd, _ = whisper_model.transcribe(
                    audio_cmd,
                    language="es",
                    initial_prompt="Asistente virtual Mimir. Transcripción precisa en español con tildes y buena ortografía.",
                    beam_size=3,
                    temperature=0.0,
                    vad_filter=False,
                    without_timestamps=True,
                )
                texto_cmd = " ".join(s.text.strip() for s in segs_cmd).strip()
                # Limpiar prefijo de invocación si el usuario dijo "Mimir abre el navegador"
                texto_cmd = re.sub(r'^(hey\s+|ey\s+|oye\s+|hola\s+)?m[ií]?mir[,\.\s]*', '', texto_cmd, flags=re.IGNORECASE).strip()
            except Exception as e:
                log(f"Error transcribiendo orden: {e}")
                texto_cmd = ""

            vad_model.reset_states()
            vaciar_cola()

            if texto_cmd:
                print(json.dumps({"type": "transcript", "text": texto_cmd, "status": "ok"}, ensure_ascii=False), flush=True)
                log(f"✅ Transcripción: \"{texto_cmd}\"")
            else:
                log("⚠️ No se pudo entender la orden.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Sistema detenido.")
        sys.exit(0)
    except Exception as e:
        print(json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False), flush=True)
        sys.exit(1)
