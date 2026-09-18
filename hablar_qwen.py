import sys
import torch
import soundfile as sf
from qwen_tts import Qwen3TTSModel

def generar_voz(texto):
    try:
        print("📥 Cargando modelo Qwen3-TTS (CustomVoice)...")
        model = Qwen3TTSModel.from_pretrained(
            "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
            device_map="cuda:0" if torch.cuda.is_available() else "cpu",
            dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32
        )

        archivo_wav = "temp_qwen.wav"

        print("🎙️ Sintetizando voz con Qwen...")
        # Generamos la locución indicando el locutor integrado y el idioma
        # Generamos la locución con el idioma estándar 'spanish' y un locutor neutro por defecto
        wavs, sr = model.generate_custom_voice(
            text=texto,
            language="spanish",
            speaker="dylan" 
        )
        
        # Guardamos el archivo resultante asegurando compatibilidad con soundfile
        if isinstance(wavs, list):
            audio_data = wavs[0]
        else:
            audio_data = wavs
            
        sf.write(archivo_wav, audio_data, sr)
        print("AUDIO_GENERADO_OK")

    except Exception as e:
        print(f"ERROR_QWEN: {str(e)}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        texto_a_decir = sys.argv[1]
        generar_voz(texto_a_decir)